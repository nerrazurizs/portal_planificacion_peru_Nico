"""
Plan de Compras — Visualización de tránsitos, proyección de compras por SKU/proveedor
y proyección de inventario mensual (Stock + Tránsito) para métricas de CCC (Finanzas).

Lógica central de inventario mes a mes:
  Inv. cierre mes N = Inv. cierre mes N-1
                    + Recepciones ETA en mes N   (compras ya puestas)
                    - Venta costo mes N           (del forecast de proyección)

Inventario financiero (para CCC):
  Total = On Hand (CLP) + En Agua (CLP)
"""

import io
import calendar
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from datetime import datetime
from pathlib import Path
from pandas.tseries.offsets import MonthEnd

from db.queries import QUERY_PLAN_COMPRAS, QUERY_VENTA_COSTO_HIST
from db.cache import cached_query as cq, TTL_DIARIO
from utils.filters import norm_cols, human_format
from utils.export import download_buttons
from utils.budget import load_budget
from utils.ui_animations import lottie_spinner, show_empty_state, show_success
from config import COLORS, TC_USD_DEFAULT, dorel_layout, apply_pm_filter

# ─── Constantes ────────────────────────────────────────────────────────────────
def _get_tc():
    """TC USD/PEN desde sidebar (session_state) o default del config."""
    return st.session_state.get("tc_usd_clp", TC_USD_DEFAULT)
TRANSIT_DAYS = 47         # Días tránsito marítimo (consistente con SQL DATEADD(day,47,...))
MESES_ES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr",  5: "May",  6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}

STATUS_COLORS = {
    "PO Recepcionada":  "#43A047",
    "Confirmada":       "#1E88E5",
    "En Tránsito":      "#FB8C00",
    "Abierta":          "#9E9E9E",
    "Cancelada":        "#E53935",
}


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_mm(v):
    """Formato millones con 1 decimal."""
    if pd.isna(v) or v == 0:
        return "—"
    if abs(v) >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f}B"
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    return f"${v:,.0f}"


def _periodo_label(periodo):
    """'2026-02-01' → 'Feb 26'"""
    try:
        p = pd.Timestamp(periodo)
        return f"{MESES_ES[p.month]} {str(p.year)[2:]}"
    except Exception:
        return str(periodo)


def _apply_factor_fallback(df, maestra):
    """
    Aplica árbol comercial para FACTOR_IMPORTACION faltante.
    Cadena: SKU → SUBLINEA-MARCA → LINEA-MARCA → AREA-MARCA → SUBLINEA → LINEA → AREA
    (misma lógica que proyeccion.py)
    """
    req = ["FACTOR_IMPORTACION", "SUBLINEA", "LINEA", "AREA", "MARCA"]
    if not all(c in maestra.columns for c in req):
        return df

    _m = maestra.copy()
    _m["FACTOR_IMPORTACION"] = pd.to_numeric(_m["FACTOR_IMPORTACION"], errors="coerce")
    _m_valid = _m[_m["FACTOR_IMPORTACION"] > 0]

    hierarchy = [
        ("SUBLINEA_MARCA", ["SUBLINEA", "MARCA"]),
        ("LINEA_MARCA",    ["LINEA",    "MARCA"]),
        ("AREA_MARCA",     ["AREA",     "MARCA"]),
        ("SUBLINEA",       ["SUBLINEA"]),
        ("LINEA",          ["LINEA"]),
        ("AREA",           ["AREA"]),
    ]

    factor_avg = {}
    for level_name, group_cols in hierarchy:
        agg = _m_valid.groupby(group_cols, as_index=False)["FACTOR_IMPORTACION"].mean()
        agg = agg.rename(columns={"FACTOR_IMPORTACION": f"FACTOR_AVG_{level_name}"})
        factor_avg[level_name] = (group_cols, agg)

    if "FACTOR_IMPORTACION" not in df.columns:
        df["FACTOR_IMPORTACION"] = np.nan

    df["FACTOR_IMPORTACION"] = pd.to_numeric(df["FACTOR_IMPORTACION"], errors="coerce")

    for sc in ["SUBLINEA", "LINEA", "AREA", "MARCA"]:
        if sc not in df.columns:
            df[sc] = ""

    for level_name, (group_cols, agg_df) in factor_avg.items():
        missing = df["FACTOR_IMPORTACION"].isna() | (df["FACTOR_IMPORTACION"] <= 0)
        if not missing.any():
            break
        col_avg = f"FACTOR_AVG_{level_name}"
        df = df.merge(agg_df, on=group_cols, how="left")
        fill = missing & df[col_avg].notna() & (df[col_avg] > 0)
        df.loc[fill, "FACTOR_IMPORTACION"] = df.loc[fill, col_avg]
        df.drop(columns=[col_avg], inplace=True)

    return df


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _load_plan_compras_pos(_conn_id, _conn=None):
    """POs — overnight load, cached 24h."""
    df = pd.read_sql(QUERY_PLAN_COMPRAS, _conn)
    return norm_cols(df)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _load_venta_costo(_conn_id, _conn=None):
    """Venta costo historica — overnight load, cached 24h."""
    df = pd.read_sql(QUERY_VENTA_COSTO_HIST, _conn)
    return norm_cols(df)


def _load_data(conn, _query_hash=None):
    """Carga POs, stock on hand, maestra y venta costo histórica.
    Maestra + stock use centralized cache; POs + COGS use local cache.
    """
    _cid = id(conn)
    with lottie_spinner("snowflake"):
        df_pos = _load_plan_compras_pos(_cid, _conn=conn)
        df_stock = cq.stock_onhand(conn)
        maestra = cq.maestra(conn)
        df_vcosto = _load_venta_costo(_cid, _conn=conn)
    return df_pos, df_stock, maestra, df_vcosto


# Hash del query para invalidar caché si cambia el SQL
_PC_QUERY_HASH = hash(QUERY_PLAN_COMPRAS + QUERY_VENTA_COSTO_HIST)


def _enrich_pos(df_pos, maestra):
    """
    Enriquece las POs con nombre SKU, factor importación, dimensiones y costo CLP.
    """
    # Peru's _COMPRAS wrapper sets area, linea, sublinea, marca, modelo,
    # factor_importacion to NULL. Drop those all-NULL columns before merge
    # so we can fill them from maestra without suffix conflicts (_x/_y).
    _wrapper_null_cols = [
        "AREA", "LINEA", "SUBLINEA", "MARCA", "MODELO", "FACTOR_IMPORTACION",
        "NOM_PRODUCTO",
    ]
    for col in _wrapper_null_cols:
        if col in df_pos.columns and df_pos[col].isna().all():
            df_pos = df_pos.drop(columns=[col])

    # Merge con maestra para dimensiones, factor importación y nombre
    maestra_cols = ["SKU_PRODUCTO"]
    for c in ["SKU_NOM_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA",
              "MARCA", "MODELO", "FACTOR_IMPORTACION", "ULTIMO_COSTO",
              "COSTO_FOB_USD", "PROCEDENCIA"]:
        if c in maestra.columns:
            maestra_cols.append(c)

    m_dedup = maestra[maestra_cols].drop_duplicates("SKU_PRODUCTO")
    df = df_pos.merge(m_dedup, on="SKU_PRODUCTO", how="left")

    # Fallback: completar AREA/LINEA/SUBLINEA/MARCA desde db_dimensiones.dim.dt_producto
    # Aplicar árbol comercial para factor faltante
    df = _apply_factor_fallback(df, maestra)

    # Calcular monto CLP si no viene ya calculado o viene 0
    mask_sin_clp = df["MONTO_CLP"].isna() | (df["MONTO_CLP"] <= 0)
    if mask_sin_clp.any():
        factor = df["FACTOR_IMPORTACION"].fillna(1.0)
        tc = df["TC_PO"].fillna(_get_tc())
        df.loc[mask_sin_clp, "MONTO_CLP"] = (
            df.loc[mask_sin_clp, "MONTO_MONEDA_ORIG"] * factor * tc
        )

    # Recalcular monto pendiente CLP consistente con monto total CLP
    df["MONTO_PENDIENTE_CLP"] = np.where(
        df["QTY_ORDENADA"] > 0,
        (df["QTY_PENDIENTE"] / df["QTY_ORDENADA"]) * df["MONTO_CLP"].fillna(0),
        0,
    )

    # Fecha ETA como timestamp
    df["PERIODO_ETA"] = pd.to_datetime(df["PERIODO_ETA"], errors="coerce")
    df["PERIODO_RECEPCION"] = pd.to_datetime(df["PERIODO_RECEPCION"], errors="coerce")

    return df


def _build_inv_projection(df_pos, df_stock, maestra, df_proy=None, df_vcosto_hist=None, n_meses=12):
    """
    Construye tabla de inventario proyectado mes a mes por Área/Línea.

    Columnas resultado:
      AREA, LINEA, PERIODO, LABEL_MES,
      STOCK_INICIAL_CLP, RECEPCIONES_CLP, VENTA_COSTO_CLP, STOCK_CIERRE_CLP,
      AGUAS_COMEX_CLP, AGUAS_FC_CLP, AGUAS_CLP (total), TOTAL_INV_CLP (stock + aguas)

    Returns (df_inv, debug_info) where debug_info is a dict with diagnostic data.
    """
    debug = {}  # diagnostic info for the expander
    hoy = pd.Timestamp.today().normalize()
    periodos = pd.date_range(
        hoy.to_period("M").to_timestamp(), periods=n_meses, freq="MS"
    )

    # ── Stock on hand enriquecido con maestra ──────────────────────────────
    mc = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
          "ULTIMO_COSTO", "COSTO_FOB_USD", "FACTOR_IMPORTACION"]
    mc = [c for c in mc if c in maestra.columns]
    stock = df_stock.merge(
        maestra[mc].drop_duplicates("SKU_PRODUCTO"), on="SKU_PRODUCTO", how="left"
    )
    stock = _apply_factor_fallback(stock, maestra)

    # Valorización stock: preferir STOCK_COSTO_TOTAL del warehouse (ft_in_stock)
    if "STOCK_COSTO_TOTAL" in stock.columns:
        stock["STOCK_TOTAL_CLP"] = pd.to_numeric(
            stock["STOCK_COSTO_TOTAL"], errors="coerce"
        ).fillna(0)
    else:
        # Fallback: recalcular si la columna no viene en el query
        stock["COSTO_UNIT"] = pd.to_numeric(stock.get("ULTIMO_COSTO", 0), errors="coerce").fillna(0)
        mask = stock["COSTO_UNIT"] <= 0
        if mask.any() and "COSTO_FOB_USD" in stock.columns:
            factor = stock["FACTOR_IMPORTACION"].fillna(1.0)
            stock.loc[mask, "COSTO_UNIT"] = stock.loc[mask, "COSTO_FOB_USD"] * factor * _get_tc()
        stock["STOCK_TOTAL_CLP"] = stock["STOCK_TOTAL"] * stock["COSTO_UNIT"]

    stock_por_linea = (
        stock.groupby(["AREA", "LINEA"], as_index=False)["STOCK_TOTAL_CLP"].sum()
        .rename(columns={"STOCK_TOTAL_CLP": "STOCK_ONHAND_CLP"})
    )

    # ── Recepciones por mes (ETA) ─────────────────────────────────────────
    # Solo POs con ETA >= inicio del mes actual (excluir historia antigua)
    inicio_mes = hoy.to_period("M").to_timestamp()

    # Filtrar POs pendientes con ETA >= mes actual
    df_pendiente = df_pos[df_pos["QTY_PENDIENTE"] > 0].copy()
    df_pendiente["PERIODO_ETA"] = pd.to_datetime(df_pendiente["PERIODO_ETA"], errors="coerce").dt.normalize()
    df_pendiente = df_pendiente[
        df_pendiente["PERIODO_ETA"].notna() & (df_pendiente["PERIODO_ETA"] >= inicio_mes)
    ]

    # Recepciones = todas las POs pendientes con ETA futura (llegarán al inventario)
    rec_por_mes = (
        df_pendiente
        .groupby(["AREA", "LINEA", "PERIODO_ETA"], as_index=False)["MONTO_PENDIENTE_CLP"].sum()
        .rename(columns={"MONTO_PENDIENTE_CLP": "RECEPCIONES_CLP"})
    )

    # ── En Agua dinámico por periodo ──────────────────────────────────────
    # Para cada periodo P: "En Agua" = POs con ETD ≤ fin(P) AND ETA > P
    # Captura POs ya navegando + POs que zarparán durante el mes P.
    if "ETD_CALC" in df_pendiente.columns:
        df_pendiente["_ETD"] = pd.to_datetime(df_pendiente["ETD_CALC"], errors="coerce")
    else:
        # Fallback: estimar ETD como ETA - TRANSIT_DAYS
        df_pendiente["_ETD"] = df_pendiente["PERIODO_ETA"] - pd.Timedelta(days=TRANSIT_DAYS)

    # Dict de fin-de-mes para cada periodo (para comparar ETD ≤ fin del mes)
    _fin_periodo = {p: (p + MonthEnd(0)).normalize() for p in periodos}

    # ── En Agua FC: compras proyectadas que estarían en agua ─────────────
    # FORECAST_COMPRA del df_proy valorizado en CLP, con ETD estimado
    # (arribo - TRANSIT_DAYS). Solo para meses sin cobertura COMEX por AREA/LINEA.
    _fc_aguas = pd.DataFrame()
    if df_proy is not None and not df_proy.empty:
        _req = ["FORECAST_COMPRA", "COSTO_UNITARIO", "PERIODO", "AREA", "LINEA"]
        _has_tipo = "TIPO_DATO" in df_proy.columns
        if all(c in df_proy.columns for c in _req):
            _df_fc_a = df_proy[df_proy["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])].copy() \
                if _has_tipo else df_proy.copy()
            _df_fc_a["FORECAST_COMPRA"] = pd.to_numeric(_df_fc_a["FORECAST_COMPRA"], errors="coerce").fillna(0)
            _df_fc_a["COSTO_UNITARIO"] = pd.to_numeric(_df_fc_a["COSTO_UNITARIO"], errors="coerce").fillna(0)
            _df_fc_a["PERIODO"] = pd.to_datetime(_df_fc_a["PERIODO"], errors="coerce")
            _df_fc_a["PERIODO_MES"] = _df_fc_a["PERIODO"].dt.to_period("M").dt.to_timestamp()
            for _col in ["AREA", "LINEA"]:
                if _col in _df_fc_a.columns:
                    _df_fc_a[_col] = _df_fc_a[_col].astype(str).str.strip().str.upper()
            _df_fc_a = _df_fc_a[_df_fc_a["FORECAST_COMPRA"] > 0]

            # Valorizar a nivel SKU PRIMERO, luego agregar a AREA/LINEA
            _df_fc_a["FC_CLP_SKU"] = _df_fc_a["FORECAST_COMPRA"] * _df_fc_a["COSTO_UNITARIO"]
            _fc_aguas = (
                _df_fc_a.groupby(["AREA", "LINEA", "PERIODO_MES"], as_index=False)
                .agg(FC_UND=("FORECAST_COMPRA", "sum"),
                     FC_CLP=("FC_CLP_SKU", "sum"))
            )
            # ETD estimado = mes de arribo - TRANSIT_DAYS
            _fc_aguas["_ETD_FC"] = _fc_aguas["PERIODO_MES"] - pd.Timedelta(days=TRANSIT_DAYS)

            # Anti doble-conteo: solo FC cuyo arribo > última ETA COMEX por AREA/LINEA
            if not df_pendiente.empty:
                _max_eta = (
                    df_pendiente.groupby(["AREA", "LINEA"], as_index=False)["PERIODO_ETA"].max()
                    .rename(columns={"PERIODO_ETA": "_MAX_ETA"})
                )
                _fc_aguas = _fc_aguas.merge(_max_eta, on=["AREA", "LINEA"], how="left")
                _fc_aguas["_MAX_ETA"] = _fc_aguas["_MAX_ETA"].fillna(pd.Timestamp("1900-01-01"))
                _fc_aguas = _fc_aguas[_fc_aguas["PERIODO_MES"] > _fc_aguas["_MAX_ETA"]]

    debug["fc_aguas_rows"] = len(_fc_aguas) if not _fc_aguas.empty else 0

    # ── Venta costo: COGS de proyección stock ─────────────────────────────
    #
    # Prioridad:
    #   1. df_proy de session_state (COGS_RES_TOTAL de Proyección Stock)
    #   2. df_vcosto_hist (promedio mensual últimos 6 meses desde Snowflake)
    #
    vcosto_source = "empty"
    if df_proy is not None and not df_proy.empty:
        debug["proy_shape"] = df_proy.shape
        debug["proy_columns"] = [c for c in df_proy.columns if "COGS" in c or "TIPO" in c or c in ("AREA", "LINEA", "PERIODO")]
        debug["proy_tipo_dato"] = df_proy["TIPO_DATO"].value_counts().to_dict() if "TIPO_DATO" in df_proy.columns else "N/A"

        # Filtrar PROYECCION + REAL+FC
        df_fc = df_proy[df_proy["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])].copy() \
            if "TIPO_DATO" in df_proy.columns else df_proy.copy()
        debug["fc_rows"] = len(df_fc)
        debug["has_cogs"] = "COGS_RES_TOTAL" in df_fc.columns
        debug["has_periodo"] = "PERIODO" in df_fc.columns

        if "COGS_RES_TOTAL" in df_fc.columns and "PERIODO" in df_fc.columns:
            debug["cogs_sum_raw"] = float(df_fc["COGS_RES_TOTAL"].sum())

            # Rellenar AREA/LINEA faltantes desde maestra
            if "SKU_PRODUCTO" in df_fc.columns:
                for _col in ["AREA", "LINEA"]:
                    if _col in df_fc.columns and _col in maestra.columns:
                        _null = df_fc[_col].isna() | (df_fc[_col].astype(str).str.strip() == "")
                        if _null.any():
                            _map = maestra.drop_duplicates("SKU_PRODUCTO").set_index("SKU_PRODUCTO")[_col]
                            df_fc.loc[_null, _col] = df_fc.loc[_null, "SKU_PRODUCTO"].map(_map)

            # Normalizar AREA/LINEA (strip whitespace, upper)
            for _col in ["AREA", "LINEA"]:
                if _col in df_fc.columns:
                    df_fc[_col] = df_fc[_col].astype(str).str.strip().str.upper()

            df_fc = df_fc.dropna(subset=["AREA", "LINEA"])
            debug["fc_rows_after_dropna"] = len(df_fc)
            debug["fc_areas"] = sorted(df_fc["AREA"].unique().tolist()) if len(df_fc) > 0 else []

            df_fc["PERIODO"] = pd.to_datetime(df_fc["PERIODO"], errors="coerce")
            df_fc["PERIODO_MES"] = df_fc["PERIODO"].dt.to_period("M").dt.to_timestamp()
            df_vcosto = (
                df_fc.groupby(["AREA", "LINEA", "PERIODO_MES"], as_index=False)["COGS_RES_TOTAL"].sum()
                .rename(columns={"COGS_RES_TOTAL": "VENTA_COSTO_CLP", "PERIODO_MES": "PERIODO_ETA"})
            )
            # Normalizar PERIODO_ETA a tz-naive Timestamp
            _pe = pd.to_datetime(df_vcosto["PERIODO_ETA"])
            if hasattr(_pe.dt, "tz") and _pe.dt.tz is not None:
                _pe = _pe.dt.tz_localize(None)
            df_vcosto["PERIODO_ETA"] = _pe.dt.normalize()

            vcosto_source = "proyeccion"
            debug["vcosto_shape"] = df_vcosto.shape
            debug["vcosto_sum"] = float(df_vcosto["VENTA_COSTO_CLP"].sum())
            debug["vcosto_periodos"] = sorted(df_vcosto["PERIODO_ETA"].unique().astype(str).tolist())
            debug["vcosto_areas"] = sorted(df_vcosto["AREA"].unique().tolist())
        else:
            df_vcosto = pd.DataFrame(columns=["AREA", "LINEA", "PERIODO_ETA", "VENTA_COSTO_CLP"])

    elif df_vcosto_hist is not None and not df_vcosto_hist.empty:
        df_vcosto_hist = df_vcosto_hist.copy()
        df_vcosto_hist["PERIODO"] = pd.to_datetime(df_vcosto_hist["PERIODO"], errors="coerce")
        df_vcosto_hist["VENTA_COSTO_CLP"] = pd.to_numeric(
            df_vcosto_hist["VENTA_COSTO_CLP"], errors="coerce"
        ).fillna(0)
        df_vcosto = df_vcosto_hist.rename(columns={"PERIODO": "PERIODO_ETA"})[
            ["AREA", "LINEA", "PERIODO_ETA", "VENTA_COSTO_CLP"]
        ]
        vcosto_source = "historico"
    else:
        df_vcosto = pd.DataFrame(columns=["AREA", "LINEA", "PERIODO_ETA", "VENTA_COSTO_CLP"])

    debug["vcosto_source"] = vcosto_source
    debug["vcosto_empty"] = df_vcosto.empty

    # Normalizar AREA/LINEA en todas las fuentes para evitar mismatches
    for _df in [stock_por_linea, rec_por_mes, df_pendiente, df_vcosto]:
        for _col in ["AREA", "LINEA"]:
            if _col in _df.columns:
                _df[_col] = _df[_col].astype(str).str.strip().str.upper()

    debug["stock_areas"] = sorted(stock_por_linea["AREA"].unique().tolist())
    debug["periodos_range"] = [str(p.date()) for p in periodos]

    # ── Construir tabla mes × área/línea ──────────────────────────────────
    areas_lineas = stock_por_linea[["AREA", "LINEA"]].drop_duplicates()
    rows = []

    for _, al in areas_lineas.iterrows():
        area, linea = al["AREA"], al["LINEA"]
        stock_ini = float(
            stock_por_linea.loc[
                (stock_por_linea["AREA"] == area) & (stock_por_linea["LINEA"] == linea),
                "STOCK_ONHAND_CLP"
            ].sum()
        )

        stock_cierre_prev = stock_ini

        for periodo in periodos:
            # Recepciones COMEX: POs con ETA en este mes
            rec_comex = float(
                rec_por_mes.loc[
                    (rec_por_mes["AREA"] == area)
                    & (rec_por_mes["LINEA"] == linea)
                    & (rec_por_mes["PERIODO_ETA"] == periodo),
                    "RECEPCIONES_CLP"
                ].sum()
            )
            # Recepciones FC: compras proyectadas que llegan este mes
            rec_fc = 0.0
            if not _fc_aguas.empty:
                rec_fc = float(
                    _fc_aguas.loc[
                        (_fc_aguas["AREA"] == area)
                        & (_fc_aguas["LINEA"] == linea)
                        & (_fc_aguas["PERIODO_MES"] == periodo),
                        "FC_CLP"
                    ].sum()
                )
            rec = rec_comex + rec_fc

            vcosto = float(
                df_vcosto.loc[
                    (df_vcosto["AREA"] == area)
                    & (df_vcosto["LINEA"] == linea)
                    & (df_vcosto["PERIODO_ETA"] == periodo),
                    "VENTA_COSTO_CLP"
                ].sum()
            ) if not df_vcosto.empty else 0

            stock_cierre = max(stock_cierre_prev + rec - vcosto, 0)

            # En Agua COMEX: POs con ETD ≤ fin del mes P y ETA > P
            fin_p = _fin_periodo[periodo]
            agua_comex = float(
                df_pendiente.loc[
                    (df_pendiente["AREA"] == area)
                    & (df_pendiente["LINEA"] == linea)
                    & (df_pendiente["_ETD"].notna())
                    & (df_pendiente["_ETD"] <= fin_p)
                    & (df_pendiente["PERIODO_ETA"] > periodo),
                    "MONTO_PENDIENTE_CLP"
                ].sum()
            ) if not df_pendiente.empty else 0

            # En Agua FC: compras proyectadas con ETD_est ≤ fin(P) y arribo > P
            agua_fc = 0.0
            if not _fc_aguas.empty:
                agua_fc = float(
                    _fc_aguas.loc[
                        (_fc_aguas["AREA"] == area)
                        & (_fc_aguas["LINEA"] == linea)
                        & (_fc_aguas["_ETD_FC"] <= fin_p)
                        & (_fc_aguas["PERIODO_MES"] > periodo),
                        "FC_CLP"
                    ].sum()
                )

            agua_total = agua_comex + agua_fc

            # Pendiente Zarpe: POs confirmadas con ETD > fin(P) — aún no zarpan
            pend_zarpe = float(
                df_pendiente.loc[
                    (df_pendiente["AREA"] == area)
                    & (df_pendiente["LINEA"] == linea)
                    & (df_pendiente["_ETD"].notna())
                    & (df_pendiente["_ETD"] > fin_p),
                    "MONTO_PENDIENTE_CLP"
                ].sum()
            ) if not df_pendiente.empty else 0

            rows.append({
                "AREA": area,
                "LINEA": linea,
                "PERIODO": periodo,
                "LABEL_MES": _periodo_label(periodo),
                "STOCK_INICIAL_CLP": stock_cierre_prev,
                "RECEPCIONES_COMEX_CLP": rec_comex,
                "RECEPCIONES_FC_CLP": rec_fc,
                "RECEPCIONES_CLP": rec,
                "VENTA_COSTO_CLP": vcosto,
                "STOCK_CIERRE_CLP": stock_cierre,
                "AGUAS_COMEX_CLP": agua_comex,
                "AGUAS_FC_CLP": agua_fc,
                "AGUAS_CLP": agua_total,
                "PEND_ZARPE_CLP": pend_zarpe,
                "TOTAL_INV_CLP": stock_cierre + agua_total + pend_zarpe,
            })

            stock_cierre_prev = stock_cierre

    debug["fc_aguas_df"] = _fc_aguas if not _fc_aguas.empty else pd.DataFrame()

    return pd.DataFrame(rows), debug


# ─── Vistas ────────────────────────────────────────────────────────────────────

def _render_resumen_financiero(df_pos, periodos_label):
    """Vista 1: Resumen financiero mensual (Tabla Resumen del Excel)."""
    st.markdown("#### Compras comprometidas por mes y origen")

    df_p = df_pos[df_pos["PERIODO_ETA"].notna()].copy()
    df_p["LABEL_MES"] = df_p["PERIODO_ETA"].apply(_periodo_label)
    df_p["ORIGEN"] = df_p["MONEDA"].apply(
        lambda m: "Importado" if str(m).upper() in ("USD", "EUR") else "Nacional"
    )

    # Usar MONTO_PENDIENTE_CLP para el resumen (lo que falta recibir/pagar)
    pivot = (
        df_p.groupby(["LABEL_MES", "ORIGEN"], as_index=False)["MONTO_PENDIENTE_CLP"].sum()
        .pivot(index="LABEL_MES", columns="ORIGEN", values="MONTO_PENDIENTE_CLP")
        .fillna(0)
        .reset_index()
    )
    # Ordenar por periodo real
    orden = {v: i for i, v in enumerate(periodos_label)}
    pivot["_ord"] = pivot["LABEL_MES"].map(orden).fillna(99)
    pivot = pivot.sort_values("_ord").drop(columns=["_ord"])

    # Totales
    for c in [c for c in pivot.columns if c != "LABEL_MES"]:
        pivot[c] = pd.to_numeric(pivot[c], errors="coerce").fillna(0)
    pivot["TOTAL"] = pivot[[c for c in pivot.columns if c != "LABEL_MES"]].sum(axis=1)

    # KPIs resumen
    k1, k2, k3, k4 = st.columns(4)
    total_import = df_p[df_p["ORIGEN"] == "Importado"]["MONTO_PENDIENTE_CLP"].sum()
    total_nac    = df_p[df_p["ORIGEN"] == "Nacional"]["MONTO_PENDIENTE_CLP"].sum()
    # En Agua KPI: POs que ya zarparon (ETD <= hoy) y aún no llegan (ETA > hoy)
    hoy = pd.Timestamp.today().normalize()
    if "ETD_CALC" in df_pos.columns:
        _etd_kpi = pd.to_datetime(df_pos["ETD_CALC"], errors="coerce")
        _eta_kpi = pd.to_datetime(
            df_pos["ETA_CALC"] if "ETA_CALC" in df_pos.columns else df_pos.get("PERIODO_ETA"),
            errors="coerce",
        )
        _mask_agua = (
            _etd_kpi.notna()
            & (_etd_kpi <= hoy)
            & (_eta_kpi > hoy)
            & (df_pos["QTY_PENDIENTE"] > 0)
        )
        en_agua = float(df_pos.loc[_mask_agua, "MONTO_PENDIENTE_CLP"].sum())
    else:
        en_agua = 0
    n_pos        = df_pos["N_PO"].nunique()

    k1.metric("Total Importado", _fmt_mm(total_import))
    k2.metric("Total Nacional",  _fmt_mm(total_nac))
    k3.metric("En Agua", _fmt_mm(en_agua))
    k4.metric("N° POs", f"{n_pos:,}")

    st.html("<br>")

    # Tabla pivoteada estilizada
    fmt_cols = {c: "${:,.0f}" for c in pivot.columns if c != "LABEL_MES"}
    st.dataframe(
        pivot.rename(columns={"LABEL_MES": "Mes"})
             .style.format(fmt_cols, na_rep="—")
             .background_gradient(subset=["TOTAL"], cmap="Blues"),
        use_container_width=True,
        height=min(len(pivot) * 38 + 60, 500),
    )

    # Gráfico de barras apiladas por mes (monto pendiente por recibir)
    st.markdown("**Monto pendiente por recibir por mes (CLP)**")
    df_chart = df_p.groupby(["LABEL_MES", "ORIGEN"], as_index=False)["MONTO_PENDIENTE_CLP"].sum()
    df_chart["_ord"] = df_chart["LABEL_MES"].map(orden).fillna(99)
    df_chart = df_chart.sort_values("_ord")

    color_map = {"Importado": "#065E8B", "Nacional": "#23CED3"}
    fig = go.Figure()
    for origen in ["Importado", "Nacional"]:
        sub = df_chart[df_chart["ORIGEN"] == origen]
        fig.add_trace(go.Bar(
            x=sub["LABEL_MES"],
            y=sub["MONTO_PENDIENTE_CLP"],
            name=origen,
            marker_color=color_map[origen],
            hovertemplate="Mes: %{x}<br>Origen: " + origen + "<br>Pendiente CLP: $%{y:,.0f}<extra></extra>",
        ))
    fig.update_layout(**dorel_layout(
        barmode="stack",
        xaxis=dict(title="Mes", categoryorder="array", categoryarray=periodos_label),
        yaxis=dict(title="CLP", tickformat="$~s"),
        legend=dict(title_text="Origen"),
        height=320,
    ))
    st.plotly_chart(fig, use_container_width=True)


def _render_proyeccion_inventario(df_inv):
    """Vista 2: Proyección de inventario mes a mes (Stock + Tránsito = Total para CCC)."""
    st.markdown("#### Proyección de inventario mensual — Stock + Tránsito")
    st.caption(
        "Inventario financiero (CCC) = Stock On Hand + En Agua + Pendiente Zarpe. "
        "Stock cierre = Stock anterior + Recepciones ETA del mes − Venta costo (forecast)."
    )

    if df_inv.empty:
        show_empty_state(
            "No hay datos de proyección",
            "Genera una proyección en 📈 Proyección de Stock y vuelve aquí.",
        )
        return

    periodos = sorted(df_inv["PERIODO"].unique())
    labels   = [_periodo_label(p) for p in periodos]

    # Agregar por mes (total empresa)
    _agg_dict = {
        "STOCK_INICIAL": ("STOCK_INICIAL_CLP", "sum"),
        "REC_COMEX": ("RECEPCIONES_COMEX_CLP", "sum"),
        "RECEPCIONES": ("RECEPCIONES_CLP", "sum"),
        "VENTA_COSTO": ("VENTA_COSTO_CLP", "sum"),
        "STOCK_CIERRE": ("STOCK_CIERRE_CLP", "sum"),
        "AGUAS_COMEX": ("AGUAS_COMEX_CLP", "sum"),
        "AGUAS": ("AGUAS_CLP", "sum"),
    }
    # Agregar FC solo si las columnas existen
    if "RECEPCIONES_FC_CLP" in df_inv.columns:
        _agg_dict["REC_FC"] = ("RECEPCIONES_FC_CLP", "sum")
    if "AGUAS_FC_CLP" in df_inv.columns:
        _agg_dict["AGUAS_FC"] = ("AGUAS_FC_CLP", "sum")
    if "PEND_ZARPE_CLP" in df_inv.columns:
        _agg_dict["PEND_ZARPE"] = ("PEND_ZARPE_CLP", "sum")
    inv_total = df_inv.groupby("PERIODO", as_index=False).agg(**_agg_dict)
    for _c in ["REC_FC", "AGUAS_FC", "PEND_ZARPE"]:
        if _c not in inv_total.columns:
            inv_total[_c] = 0
    inv_total["TOTAL_INV"] = inv_total["STOCK_CIERRE"] + inv_total["AGUAS"] + inv_total["PEND_ZARPE"]
    inv_total["LABEL_MES"] = inv_total["PERIODO"].apply(_periodo_label)

    # ── Días de Inventario (DOI) ─────────────────────────────────────────
    # DOI = Total Inv / (Venta Costo prom. diario últimos 3 meses)
    #      = Total Inv × 30 / avg_mensual_COGS_3m
    # Para Feb 26: usa Dec 25 + Jan 26 (histórico real) + Feb 26 (proyección)
    _cogs_hist = {}
    _df_proy = st.session_state.get("df_proy")
    if _df_proy is not None and not _df_proy.empty and "COGS_RES_TOTAL" in _df_proy.columns:
        _hist = _df_proy[_df_proy["TIPO_DATO"] == "HISTORICO"].copy() \
            if "TIPO_DATO" in _df_proy.columns else pd.DataFrame()
        if not _hist.empty and "PERIODO" in _hist.columns:
            _hist["PERIODO"] = pd.to_datetime(_hist["PERIODO"], errors="coerce")
            _hist["COGS_RES_TOTAL"] = pd.to_numeric(_hist["COGS_RES_TOTAL"], errors="coerce").fillna(0)
            _cogs_hist = (
                _hist.groupby(_hist["PERIODO"].dt.to_period("M").dt.to_timestamp())["COGS_RES_TOTAL"]
                .sum().to_dict()
            )

    # Construir serie completa de COGS mensual (histórico + proyección)
    _all_cogs = {}
    _all_cogs.update(_cogs_hist)  # Dec 25, Jan 26, etc.
    for _, row in inv_total.iterrows():
        _all_cogs[row["PERIODO"]] = row["VENTA_COSTO"]

    # Rolling 3 meses: para cada periodo, promedio del actual y 2 anteriores
    _doi_values = []
    for p in inv_total["PERIODO"]:
        _m0 = p
        _m1 = (_m0 - pd.DateOffset(months=1)).normalize()
        _m2 = (_m0 - pd.DateOffset(months=2)).normalize()
        _cogs_3m = [_all_cogs.get(m, 0) for m in [_m2, _m1, _m0] if _all_cogs.get(m, 0) > 0]
        _avg_cogs = sum(_cogs_3m) / len(_cogs_3m) if _cogs_3m else 0
        _total = inv_total.loc[inv_total["PERIODO"] == p, "TOTAL_INV"].iloc[0]
        doi = (_total / (_avg_cogs / 30)) if _avg_cogs > 0 else 0
        _doi_values.append(round(doi))
    inv_total["DOI"] = _doi_values

    # ── Versión Budget: reemplazar Venta Costo por COGS Budget 2026 ────────
    _df_budget = load_budget()
    _has_budget = not _df_budget.empty and "COGS_BUDGET" in _df_budget.columns
    inv_budget = None

    if _has_budget:
        _bdg_total = _df_budget[_df_budget["CANAL"].str.upper() == "TOTAL"].copy()
        _bdg_total["PERIODO"] = pd.to_datetime(_bdg_total["PERIODO"], errors="coerce")
        _bdg_total["PERIODO"] = _bdg_total["PERIODO"].dt.to_period("M").dt.to_timestamp()
        _bdg_map = _bdg_total.set_index("PERIODO")["COGS_BUDGET"].to_dict()

        inv_budget = inv_total.copy()
        inv_budget["VENTA_COSTO"] = inv_budget["PERIODO"].map(
            lambda p: _bdg_map.get(p, 0)
        )

        # Recalcular Stock Cierre iterativamente con COGS Budget
        _stock_prev_bdg = inv_budget["STOCK_INICIAL"].iloc[0]
        _cierre_bdg = []
        for _, row in inv_budget.iterrows():
            _sc = max(_stock_prev_bdg + row["RECEPCIONES"] - row["VENTA_COSTO"], 0)
            _cierre_bdg.append(_sc)
            _stock_prev_bdg = _sc
        inv_budget["STOCK_CIERRE"] = _cierre_bdg

        # Recalcular Stock Inicial (shift de Stock Cierre)
        _ini_bdg = [inv_budget["STOCK_INICIAL"].iloc[0]] + _cierre_bdg[:-1]
        inv_budget["STOCK_INICIAL"] = _ini_bdg

        # Total Inv = Stock Cierre + En Agua + Pend Zarpe
        inv_budget["TOTAL_INV"] = inv_budget["STOCK_CIERRE"] + inv_budget["AGUAS"] + inv_budget["PEND_ZARPE"]

        # DOI Budget: rolling 3m con COGS Budget
        _doi_bdg = []
        _all_cogs_bdg = {}
        _all_cogs_bdg.update(_cogs_hist)  # meses históricos reales
        for _, row in inv_budget.iterrows():
            _all_cogs_bdg[row["PERIODO"]] = row["VENTA_COSTO"]
        for p in inv_budget["PERIODO"]:
            _m0 = p
            _m1 = (_m0 - pd.DateOffset(months=1)).normalize()
            _m2 = (_m0 - pd.DateOffset(months=2)).normalize()
            _cogs_3 = [_all_cogs_bdg.get(m, 0) for m in [_m2, _m1, _m0] if _all_cogs_bdg.get(m, 0) > 0]
            _avg_c = sum(_cogs_3) / len(_cogs_3) if _cogs_3 else 0
            _tot = inv_budget.loc[inv_budget["PERIODO"] == p, "TOTAL_INV"].iloc[0]
            _doi_bdg.append(round((_tot / (_avg_c / 30)) if _avg_c > 0 else 0))
        inv_budget["DOI"] = _doi_bdg

    # KPIs del primer mes
    r0 = inv_total.iloc[0]
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Stock On Hand", _fmt_mm(r0["STOCK_CIERRE"]))
    k2.metric("En Agua",       _fmt_mm(r0["AGUAS"]))
    k3.metric("Pend. Zarpe",   _fmt_mm(r0["PEND_ZARPE"]))
    k4.metric("Total Inv CCC", _fmt_mm(r0["TOTAL_INV"]))
    k5.metric("DOI",           f"{int(r0['DOI']):,} días")

    st.html("<br>")

    # Gráfico área apilada: stock + en agua + pendiente zarpe
    df_area = inv_total.sort_values("PERIODO").copy()
    for col in ["STOCK_CIERRE", "AGUAS", "PEND_ZARPE"]:
        if col not in df_area.columns:
            df_area[col] = 0

    _y_stock = df_area["STOCK_CIERRE"]
    _y_agua  = _y_stock + df_area["AGUAS"]
    _y_zarpe = _y_agua + df_area["PEND_ZARPE"]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df_area["LABEL_MES"], y=_y_stock,
        name="Stock On Hand", mode="lines",
        fill="tozeroy", fillcolor="rgba(6,94,139,0.85)", line=dict(color="#065E8B"),
        hovertemplate="Mes: %{x}<br>Stock On Hand: $%{y:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=df_area["LABEL_MES"], y=_y_agua,
        name="En Agua", mode="lines",
        fill="tonexty", fillcolor="rgba(244,165,40,0.75)", line=dict(color="#F4A528"),
        hovertemplate="Mes: %{x}<br>En Agua: $%{customdata:,.0f}<extra></extra>",
        customdata=df_area["AGUAS"],
    ))
    fig.add_trace(go.Scatter(
        x=df_area["LABEL_MES"], y=_y_zarpe,
        name="Pendiente Zarpe", mode="lines",
        fill="tonexty", fillcolor="rgba(251,140,0,0.55)", line=dict(color="#FB8C00"),
        hovertemplate="Mes: %{x}<br>Pend. Zarpe: $%{customdata:,.0f}<extra></extra>",
        customdata=df_area["PEND_ZARPE"],
    ))
    fig.update_layout(**dorel_layout(
        title="Pipeline de Inventario: Stock + En Agua + Pendiente Zarpe",
        xaxis=dict(title="Mes", categoryorder="array", categoryarray=labels),
        yaxis=dict(title="CLP", tickformat="$~s"),
        legend=dict(title_text=""),
        height=340,
    ))
    st.plotly_chart(fig, use_container_width=True)

    # ── Helper: formatear tabla de inventario ─────────────────────────────
    def _build_inv_table(inv_df):
        """Build a styled display table from an inventory total DataFrame."""
        _tbl_cols = ["LABEL_MES", "STOCK_INICIAL",
                     "REC_COMEX", "REC_FC", "RECEPCIONES",
                     "VENTA_COSTO", "STOCK_CIERRE",
                     "AGUAS_COMEX", "AGUAS_FC", "AGUAS",
                     "PEND_ZARPE",
                     "TOTAL_INV", "DOI"]
        _tbl = inv_df[[c for c in _tbl_cols if c in inv_df.columns]].copy()
        _ren = {
            "LABEL_MES": "Mes", "STOCK_INICIAL": "Stock Inicial",
            "REC_COMEX": "+ ETA COMEX", "REC_FC": "+ ETA Proyectada",
            "RECEPCIONES": "= Recepciones", "VENTA_COSTO": "- Venta Costo",
            "STOCK_CIERRE": "= Stock Cierre",
            "AGUAS_COMEX": "+ En Agua COMEX", "AGUAS_FC": "+ En Agua FC",
            "AGUAS": "= En Agua Total",
            "PEND_ZARPE": "+ Pend. Zarpe",
            "TOTAL_INV": "Total Inv (CCC)",
            "DOI": "Días Inv.",
        }
        _tbl = _tbl.rename(columns=_ren)
        _f = {c: "${:,.0f}" for c in _tbl.columns if c not in ("Mes", "Días Inv.")}
        if "Días Inv." in _tbl.columns:
            _f["Días Inv."] = "{:,.0f}"
        _hl = [c for c in ["= Stock Cierre", "= En Agua Total", "+ Pend. Zarpe"] if c in _tbl.columns]
        _sty = _tbl.style.format(_f, na_rep="—")
        if _hl:
            _sty = _sty.set_properties(subset=_hl,
                                       **{"background-color": "#E3F2FD", "font-weight": "bold"})
        if "Total Inv (CCC)" in _tbl.columns:
            _sty = _sty.set_properties(subset=["Total Inv (CCC)"],
                                       **{"background-color": "#BBDEFB", "font-weight": "bold"})
        if "Días Inv." in _tbl.columns:
            _sty = _sty.set_properties(subset=["Días Inv."],
                                       **{"background-color": "#FFF3E0", "font-weight": "bold",
                                          "color": "#E65100"})
        return _tbl, _sty

    # Tabla resumen mensual — Forecast
    st.markdown("**Detalle mensual — Venta Costo Forecast**")
    tbl_fc, styler_fc = _build_inv_table(inv_total)
    st.dataframe(styler_fc, use_container_width=True,
                 height=min(len(tbl_fc) * 38 + 60, 450))

    # Tabla resumen mensual — Budget 2026
    tbl_bdg = None
    if inv_budget is not None:
        st.markdown("**Detalle mensual — Venta Costo Budget 2026**")
        tbl_bdg, styler_bdg = _build_inv_table(inv_budget)
        st.dataframe(styler_bdg, use_container_width=True,
                     height=min(len(tbl_bdg) * 38 + 60, 450))
    else:
        st.info("Sin datos de Budget 2026. Cargue el archivo de budget para ver esta vista.")

    # ── Export Excel: ambas tablas en hojas separadas ──────────────────────
    _buf = io.BytesIO()
    with pd.ExcelWriter(_buf, engine="openpyxl") as writer:
        tbl_fc.to_excel(writer, sheet_name="Inventario Forecast", index=False)
        if tbl_bdg is not None:
            tbl_bdg.to_excel(writer, sheet_name="Inventario Budget", index=False)
    _buf.seek(0)
    st.download_button(
        "📥 Descargar Excel (Forecast + Budget)",
        data=_buf,
        file_name="proyeccion_inventario_fc_budget.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_inv_fc_bdg",
    )

    # Breakdown por Área/Línea
    st.markdown("**Por Área y Línea**")
    sel_periodo = st.selectbox(
        "Mes a ver",
        options=list(zip(periodos, labels)),
        format_func=lambda x: x[1],
        key="pc_sel_periodo",
    )
    df_al = df_inv[df_inv["PERIODO"] == sel_periodo[0]].copy()
    df_al = df_al.sort_values("TOTAL_INV_CLP", ascending=False)
    df_al["TOTAL_INV_CLP_FMT"] = df_al["TOTAL_INV_CLP"].apply(_fmt_mm)
    df_al["STOCK_CIERRE_FMT"]  = df_al["STOCK_CIERRE_CLP"].apply(_fmt_mm)
    df_al["AGUAS_FMT"]         = df_al["AGUAS_CLP"].apply(_fmt_mm)

    df_top = df_al.head(20).sort_values("TOTAL_INV_CLP", ascending=True)
    fig = go.Figure()
    for area in df_top["AREA"].unique():
        sub = df_top[df_top["AREA"] == area]
        fig.add_trace(go.Bar(
            y=sub["LINEA"], x=sub["TOTAL_INV_CLP"],
            name=area, orientation="h",
            hovertemplate=(
                "Área: " + area +
                "<br>Línea: %{y}" +
                "<br>Stock Cierre: $%{customdata[0]:,.0f}" +
                "<br>En Agua: $%{customdata[1]:,.0f}" +
                "<br>Total CCC: $%{x:,.0f}<extra></extra>"
            ),
            customdata=sub[["STOCK_CIERRE_CLP", "AGUAS_CLP"]].values,
        ))
    fig.update_layout(**dorel_layout(
        barmode="stack",
        xaxis=dict(title="Total Inventario CLP", tickformat="$~s"),
        yaxis=dict(title=""),
        legend=dict(title_text="Área"),
        height=max(len(df_top) * 32, 200),
    ))
    st.plotly_chart(fig, use_container_width=True)

    download_buttons(df_inv, "plan_compras_inventario_proyectado")


def _render_detalle_pos(df_pos):
    """Vista 3: Detalle de POs con semáforo de estado y filtros."""
    st.markdown("#### Detalle de órdenes de compra")

    # Filtros
    f1, f2, f3, f4 = st.columns(4)
    with f1:
        areas = ["Todas"] + sorted(df_pos["AREA"].dropna().unique().tolist())
        sel_area = st.selectbox("Área", areas, key="pc_area")
    with f2:
        base = df_pos if sel_area == "Todas" else df_pos[df_pos["AREA"] == sel_area]
        lineas = ["Todas"] + sorted(base["LINEA"].dropna().unique().tolist())
        sel_linea = st.selectbox("Línea", lineas, key="pc_linea")
    with f3:
        provs = ["Todos"] + sorted(df_pos["NOM_PROVEEDOR"].dropna().unique().tolist())
        sel_prov = st.selectbox("Proveedor", provs, key="pc_prov")
    with f4:
        estados = ["Todos"] + sorted(df_pos["STATUS_PO"].dropna().unique().tolist())
        sel_estado = st.selectbox("Estado PO", estados, key="pc_estado")

    # Filtro mes ETA
    meses_eta = sorted(df_pos["PERIODO_ETA"].dropna().unique().tolist())
    labels_eta = [_periodo_label(m) for m in meses_eta]
    sel_meses = st.multiselect(
        "Mes ETA", options=list(zip(meses_eta, labels_eta)),
        format_func=lambda x: x[1], key="pc_meses_eta",
    )

    # Aplicar filtros
    df_f = df_pos.copy()
    if sel_area != "Todas":
        df_f = df_f[df_f["AREA"] == sel_area]
    if sel_linea != "Todas":
        df_f = df_f[df_f["LINEA"] == sel_linea]
    if sel_prov != "Todos":
        df_f = df_f[df_f["NOM_PROVEEDOR"] == sel_prov]
    if sel_estado != "Todos":
        df_f = df_f[df_f["STATUS_PO"] == sel_estado]
    if sel_meses:
        periodos_sel = [m[0] for m in sel_meses]
        df_f = df_f[df_f["PERIODO_ETA"].isin(periodos_sel)]

    if df_f.empty:
        st.warning("No hay POs con los filtros seleccionados.")
        return

    # KPIs filtrados
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("POs",            f"{df_f['N_PO'].nunique():,}")
    k2.metric("SKUs",           f"{df_f['SKU_PRODUCTO'].nunique():,}")
    k3.metric("Monto Total CLP", _fmt_mm(df_f["MONTO_CLP"].sum()))
    k4.metric("Pendiente CLP",   _fmt_mm(df_f["MONTO_PENDIENTE_CLP"].sum()))

    st.html("<br>")

    # Tabla con semáforo de estado
    cols_show = [
        "N_PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "MARCA",
        "NOM_PROVEEDOR", "STATUS_PO", "TIENE_BL", "STATUS_BOOKING",
        "MONEDA", "QTY_ORDENADA", "QTY_RECEPCIONADA", "QTY_PENDIENTE",
        "MONTO_CLP", "MONTO_PENDIENTE_CLP",
        "ETD_CALC", "ETA_CALC", "PERIODO_ETA", "FECHA_RECEPCION_EN_CD",
    ]
    cols_show = [c for c in cols_show if c in df_f.columns]
    df_show = df_f[cols_show].copy()

    # Semáforo STATUS_PO
    def _color_status(val):
        color = STATUS_COLORS.get(str(val), "#9E9E9E")
        return f"background-color: {color}22; color: {color}; font-weight: bold"

    styled = df_show.style.map(_color_status, subset=["STATUS_PO"])
    for c in ["MONTO_CLP", "MONTO_PENDIENTE_CLP"]:
        if c in df_show.columns:
            styled = styled.format({c: "${:,.0f}"}, na_rep="—")

    st.dataframe(styled, use_container_width=True, height=500)
    download_buttons(df_f[cols_show], "plan_compras_detalle_pos")


# ─── Open to Buy ──────────────────────────────────────────────────────────────

_MOI_OPTIONS = [2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0]


def _compute_otb(df_inv, moi_target=3.0, moi_overrides=None):
    """
    Calcula OTB simplificado por AREA/LINEA/PERIODO.

    OTB = Venta Plan + Stock Target − Pipeline Total
    Pipeline Total = Stock Cierre + En Agua + Pendiente Zarpe
    Stock Target = MOI × Venta Plan promedio mensual (COGS)

    Returns (df_total, df_linea):
      df_total — OTB agregado por PERIODO (total empresa)
      df_linea — OTB por AREA × LINEA × PERIODO
    """
    req = ["AREA", "LINEA", "PERIODO", "VENTA_COSTO_CLP", "STOCK_CIERRE_CLP", "AGUAS_CLP"]
    if not all(c in df_inv.columns for c in req):
        return pd.DataFrame(), pd.DataFrame()

    df = df_inv.copy()
    for c in ["VENTA_COSTO_CLP", "STOCK_CIERRE_CLP", "AGUAS_CLP"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    if "PEND_ZARPE_CLP" in df.columns:
        df["PEND_ZARPE_CLP"] = pd.to_numeric(df["PEND_ZARPE_CLP"], errors="coerce").fillna(0)
    else:
        df["PEND_ZARPE_CLP"] = 0

    # ── Por AREA/LINEA ──────────────────────────────────────────────────
    df_linea = df.groupby(["AREA", "LINEA", "PERIODO"], as_index=False).agg(
        VENTA_PLAN=("VENTA_COSTO_CLP", "sum"),
        STOCK_CIERRE=("STOCK_CIERRE_CLP", "sum"),
        EN_AGUA=("AGUAS_CLP", "sum"),
        PEND_ZARPE=("PEND_ZARPE_CLP", "sum"),
    )
    df_linea["PIPELINE"] = df_linea["STOCK_CIERRE"] + df_linea["EN_AGUA"] + df_linea["PEND_ZARPE"]

    # MOI por línea (default o overrides)
    if moi_overrides is not None and not moi_overrides.empty:
        df_linea = df_linea.merge(
            moi_overrides[["AREA", "LINEA", "MOI_TARGET"]],
            on=["AREA", "LINEA"], how="left",
        )
        df_linea["MOI_TARGET"] = df_linea["MOI_TARGET"].fillna(moi_target)
    else:
        df_linea["MOI_TARGET"] = moi_target

    df_linea["STOCK_TARGET"] = df_linea["MOI_TARGET"] * df_linea["VENTA_PLAN"]
    df_linea["OTB"] = df_linea["VENTA_PLAN"] + df_linea["STOCK_TARGET"] - df_linea["PIPELINE"]
    df_linea["OTB_POSITIVO"] = df_linea["OTB"].clip(lower=0)
    df_linea["LABEL_MES"] = df_linea["PERIODO"].apply(_periodo_label)

    # ── Total empresa ───────────────────────────────────────────────────
    df_total = df_linea.groupby("PERIODO", as_index=False).agg(
        VENTA_PLAN=("VENTA_PLAN", "sum"),
        PIPELINE=("PIPELINE", "sum"),
        STOCK_TARGET=("STOCK_TARGET", "sum"),
        OTB=("OTB", "sum"),
        OTB_POSITIVO=("OTB_POSITIVO", "sum"),
    )
    df_total["LABEL_MES"] = df_total["PERIODO"].apply(_periodo_label)

    return df_total, df_linea


def _render_otb_tab(df_inv):
    """Tab OTB: selector MOI, grilla OTB, gráfico, breakdown por línea."""
    st.markdown("#### Open to Buy")
    st.caption(
        "OTB = Venta Plan + Stock Target − Pipeline Total. "
        "Pipeline = Stock Cierre + En Agua + Pendiente Zarpe. "
        "🟢 OTB > 0 = necesitas comprar más. 🔴 OTB < 0 = sobrestock."
    )

    if df_inv.empty:
        show_empty_state(
            "No hay datos de inventario",
            "Genera una proyección de stock primero.",
        )
        return

    # ── Controles ─────────────────────────────────────────────────────
    c1, c2 = st.columns([1, 2])
    with c1:
        moi_default = st.selectbox(
            "MOI Target (meses)",
            options=_MOI_OPTIONS,
            index=2,  # default 3.0
            key="otb_moi_default",
            help="Meses de inventario objetivo. Pipeline debería cubrir esta cantidad de meses de venta.",
        )
    with c2:
        custom_moi = st.toggle("Personalizar MOI por línea", key="otb_custom_moi")

    # MOI overrides por línea
    moi_overrides = None
    if custom_moi:
        lineas = (
            df_inv[["AREA", "LINEA"]].drop_duplicates()
            .sort_values(["AREA", "LINEA"])
            .reset_index(drop=True)
        )
        lineas["MOI_TARGET"] = float(moi_default)
        edited = st.data_editor(
            lineas,
            column_config={
                "AREA": st.column_config.TextColumn("Área", disabled=True),
                "LINEA": st.column_config.TextColumn("Línea", disabled=True),
                "MOI_TARGET": st.column_config.NumberColumn(
                    "MOI Target", min_value=0.5, max_value=12.0, step=0.5, format="%.1f",
                ),
            },
            use_container_width=True,
            height=min(len(lineas) * 36 + 40, 350),
            key="otb_moi_editor",
        )
        moi_overrides = edited

    # ── Cálculo OTB ───────────────────────────────────────────────────
    df_total, df_linea = _compute_otb(df_inv, moi_target=moi_default, moi_overrides=moi_overrides)

    if df_total.empty:
        st.warning("No se pudo calcular el OTB. Verifica los datos.")
        return

    # ── KPIs ──────────────────────────────────────────────────────────
    otb_sum = df_total["OTB"].sum()
    otb_pos = df_total["OTB_POSITIVO"].sum()
    n_sobre = int((df_linea.groupby(["AREA", "LINEA"])["OTB"].sum() < 0).sum())
    n_sub   = int((df_linea.groupby(["AREA", "LINEA"])["OTB"].sum() > 0).sum())
    n_total = df_linea[["AREA", "LINEA"]].drop_duplicates().shape[0]

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("OTB Neto", _fmt_mm(otb_sum))
    k2.metric("OTB Positivo (a comprar)", _fmt_mm(otb_pos))
    k3.metric("Líneas Sobrestock", f"{n_sobre} / {n_total}", delta=f"-{n_sobre}" if n_sobre > 0 else None, delta_color="inverse")
    k4.metric("Líneas Substock", f"{n_sub} / {n_total}", delta=f"+{n_sub}" if n_sub > 0 else None)

    st.html("<br>")

    # ── Grilla OTB mensual ────────────────────────────────────────────
    periodos = sorted(df_total["PERIODO"].unique())
    labels = [_periodo_label(p) for p in periodos]

    tbl = df_total[["LABEL_MES", "VENTA_PLAN", "STOCK_TARGET", "PIPELINE", "OTB"]].copy()
    tbl = tbl.rename(columns={
        "LABEL_MES": "Mes",
        "VENTA_PLAN": "Venta Plan (COGS)",
        "STOCK_TARGET": "Stock Target",
        "PIPELINE": "Pipeline Total",
        "OTB": "Open to Buy",
    })

    def _color_otb(val):
        if pd.isna(val) or val == 0:
            return ""
        return "color: #1B5E20; font-weight: bold" if val > 0 else "color: #B71C1C; font-weight: bold"

    _fmt = {c: "${:,.0f}" for c in tbl.columns if c != "Mes"}
    styler = (
        tbl.style
        .format(_fmt, na_rep="—")
        .map(_color_otb, subset=["Open to Buy"])
        .set_properties(subset=["Open to Buy"], **{"background-color": "#F5F5F5"})
    )
    st.dataframe(styler, use_container_width=True, height=min(len(tbl) * 38 + 60, 500))

    # ── Gráfico: Pipeline vs Target ──────────────────────────────────
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df_total["LABEL_MES"], y=df_total["PIPELINE"],
        name="Pipeline Total", marker_color="#065E8B",
        hovertemplate="Mes: %{x}<br>Pipeline: $%{y:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=df_total["LABEL_MES"], y=df_total["STOCK_TARGET"],
        name="Stock Target (MOI)", marker_color="#F4A528",
        hovertemplate="Mes: %{x}<br>Target: $%{y:,.0f}<extra></extra>",
    ))

    # Anotaciones OTB
    for _, row in df_total.iterrows():
        otb_val = row["OTB"]
        color = "#1B5E20" if otb_val >= 0 else "#B71C1C"
        sign = "+" if otb_val >= 0 else ""
        fig.add_annotation(
            x=row["LABEL_MES"],
            y=max(row["PIPELINE"], row["STOCK_TARGET"]) * 1.05,
            text=f"<b>{sign}{_fmt_mm(otb_val)}</b>",
            showarrow=False,
            font=dict(color=color, size=11),
        )

    # Budget overlay
    _df_budget = load_budget()
    if not _df_budget.empty and "COGS_BUDGET" in _df_budget.columns:
        _bdg = _df_budget[_df_budget["CANAL"].str.upper() == "TOTAL"].copy()
        _bdg["PERIODO"] = pd.to_datetime(_bdg["PERIODO"], errors="coerce").dt.to_period("M").dt.to_timestamp()
        _bdg = _bdg[_bdg["PERIODO"].isin(periodos)].sort_values("PERIODO")
        if not _bdg.empty:
            fig.add_trace(go.Scatter(
                x=_bdg["PERIODO"].apply(_periodo_label),
                y=_bdg["COGS_BUDGET"],
                name="Budget COGS", mode="lines+markers",
                line=dict(color="#E53935", width=2, dash="dash"),
                marker=dict(size=6),
                hovertemplate="Mes: %{x}<br>Budget: $%{y:,.0f}<extra></extra>",
            ))

            # Holgura
            _merged = df_total.merge(_bdg[["PERIODO", "COGS_BUDGET"]], on="PERIODO", how="left")
            _merged["HOLGURA"] = _merged["COGS_BUDGET"].fillna(0) - _merged["OTB_POSITIVO"]
            holgura_total = _merged["HOLGURA"].sum()
            st.info(f"**Holgura presupuestaria total:** {_fmt_mm(holgura_total)} "
                    f"({'disponible' if holgura_total >= 0 else '⚠️ excede budget'})")

    fig.update_layout(**dorel_layout(
        title="Pipeline Total vs Stock Target (MOI)",
        barmode="group",
        xaxis=dict(title="Mes", categoryorder="array", categoryarray=labels),
        yaxis=dict(title="CLP", tickformat="$~s"),
        legend=dict(title_text=""),
        height=380,
    ))
    st.plotly_chart(fig, use_container_width=True)

    # ── Breakdown por Línea ───────────────────────────────────────────
    with st.expander("📋 Detalle OTB por Línea", expanded=False):
        # Pivot: AREA × LINEA como filas, meses como columnas
        piv = df_linea.pivot_table(
            index=["AREA", "LINEA"],
            columns="LABEL_MES",
            values="OTB",
            aggfunc="sum",
        ).fillna(0)
        piv = piv[labels]  # orden correcto
        piv["TOTAL"] = piv.sum(axis=1)
        piv = piv.sort_values("TOTAL", ascending=False).reset_index()

        # Formatear con colores
        _val_cols = [c for c in piv.columns if c not in ("AREA", "LINEA")]

        def _color_cell(val):
            if pd.isna(val) or val == 0:
                return ""
            return "color: #1B5E20" if val > 0 else "color: #B71C1C"

        _fmt_piv = {c: "${:,.0f}" for c in _val_cols}
        styler_piv = (
            piv.style
            .format(_fmt_piv, na_rep="—")
            .map(_color_cell, subset=_val_cols)
        )
        st.dataframe(styler_piv, use_container_width=True, height=min(len(piv) * 36 + 60, 600))
        download_buttons(piv, "otb_por_linea")


# ─── Exportar Plan de Compra ───────────────────────────────────────────────────

def _generar_plan_compra(df_pos, maestra, factor_file):
    """
    Combina OC pendientes (df_pos ya cargado y enriquecido) con proyección
    (proy_result.parquet). Aplica factor de importación del Excel subido.
    Usa el TC del sidebar vía _get_tc(). No re-consulta Snowflake.
    Retorna DataFrame listo para descarga, o None si hubo error.
    """
    MESES_FULL = {
        1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
        5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
        9: "Setiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
    }
    AREAS_VALIDAS = {"Bebe", "Jugueteria", "Tiempo Libre", "Vestuario"}
    STATUS_CERRADOS = {
        "Cerrada", "PO Recepcionada", "CERRADA", "RECEPCIONADA",
        "Cancelada", "CANCELADA",
        "Cerrado", "Recibido",  # valores reales de Snowflake Peru
    }
    tc = _get_tc()

    hoy = datetime.today()
    anio_actual = hoy.year
    mes_actual = hoy.month
    ultimo_dia_mes = calendar.monthrange(anio_actual, mes_actual)[1]

    # ── Factor de Importación desde Excel subido ────────────────────────────
    try:
        df_factor = pd.read_excel(factor_file)
        # col[0]: clave GRUPO+LINEA+MARCA (sin espacios internos), col[4]: factor
        keys = (
            df_factor.iloc[:, 0]
            .astype(str).str.strip().str.upper()
            .str.replace(r"\s+", "", regex=True)
        )
        factors = pd.to_numeric(df_factor.iloc[:, 4], errors="coerce")
        factor_map = {k: v for k, v in zip(keys, factors) if pd.notna(v)}
    except Exception as e:
        st.error(f"Error leyendo FactordeImportacion.xlsx: {e}")
        return None

    def _lookup_factor(area, linea, marca, procedencia):
        """Devuelve 1.0 para nacionales; busca en factor_map o usa 1.3."""
        if str(procedencia).strip().upper() == "NACIONAL":
            return 1.0
        key = (
            str(area).strip().upper().replace(" ", "")
            + str(linea).strip().upper().replace(" ", "")
            + str(marca).strip().upper().replace(" ", "")
        )
        return factor_map.get(key, 1.3)

    # ── OC pendientes desde df_pos (ya enriquecido, columnas MAYÚSCULAS) ───
    df_ft = df_pos[
        (df_pos["QTY_PENDIENTE"] > 0)
        & (~df_pos["STATUS_PO"].astype(str).isin(STATUS_CERRADOS))
    ].copy()

    proc_ft = df_ft.get("PROCEDENCIA", pd.Series("", index=df_ft.index)).fillna("")
    df_ft["_FACTOR"] = [
        _lookup_factor(r.get("AREA", ""), r.get("LINEA", ""), r.get("MARCA", ""), p)
        for r, p in zip(df_ft.to_dict("records"), proc_ft)
    ]
    mask_imp_ft = proc_ft.str.strip().str.upper() == "IMPORTADO"
    df_ft["CURRENCY"] = np.where(mask_imp_ft, "USD", "PEN")
    df_ft["AMOUNT"] = df_ft["MONTO_MONEDA_ORIG"].fillna(0)
    df_ft["AMOUNT_SOLES"] = np.where(
        mask_imp_ft,
        df_ft["AMOUNT"] * df_ft["_FACTOR"] * tc,
        df_ft["MONTO_CLP"].fillna(0),
    )

    # Fechas — ETD_CALC / ETA_CALC vienen de QUERY_PLAN_COMPRAS
    etd_dt = pd.to_datetime(df_ft["ETD_CALC"], errors="coerce")
    eta_dt = pd.to_datetime(df_ft["ETA_CALC"], errors="coerce")

    # Filtrar solo año actual; filas sin ETA se conservan (se les asignará ETA al cierre del mes)
    keep = (eta_dt.dt.year.fillna(0).astype(int) == anio_actual) | eta_dt.isna()
    df_ft = df_ft[keep].copy()
    etd_dt = etd_dt[keep]
    eta_dt = eta_dt[keep]
    mask_imp_ft = mask_imp_ft[keep]

    # Meses anteriores al actual (dentro del año) → forzar al último día del mes actual
    late = eta_dt.dt.month.fillna(0).astype(int) < mes_actual
    eta_dt = eta_dt.copy()
    eta_dt[late] = pd.Timestamp(datetime(anio_actual, mes_actual, ultimo_dia_mes))
    # Filas sin ETA → último día del mes actual
    no_eta = eta_dt.isna()
    eta_dt[no_eta] = pd.Timestamp(datetime(anio_actual, mes_actual, ultimo_dia_mes))

    df_ft["ETD"] = etd_dt.dt.strftime("%d/%m/%Y").fillna("")
    df_ft["ETA"] = eta_dt.dt.strftime("%d/%m/%Y").fillna("")
    df_ft["MES ETA"] = eta_dt.dt.month.map(MESES_FULL)
    df_ft["Días agua"] = (eta_dt - etd_dt).dt.days
    df_ft["COMPRA_UNDS"] = df_ft["QTY_PENDIENTE"]
    df_ft["FUENTE"] = "ft_compras"
    df_ft["SKU"] = df_ft["SKU_PRODUCTO"]
    nom_col = "NOM_PRODUCTO" if "NOM_PRODUCTO" in df_ft.columns else "SKU_NOM_PRODUCTO"
    df_ft["PRODUCTO"] = df_ft.get(nom_col, pd.Series("", index=df_ft.index)).fillna("")
    mix_map = dict(zip(maestra["SKU_PRODUCTO"], maestra.get("MIX_OFICIAL", pd.Series(dtype=str))))
    df_ft["MIX"] = df_ft["SKU_PRODUCTO"].map(mix_map).fillna("")
    df_ft["ORIGEN"] = proc_ft[keep].values

    # ── Lookup AREA/LINEA/SUBLINEA/MARCA desde vw_producto (maestra) ────────
    # Forzar re-lookup aquí porque _enrich_pos puede traer NULLs si el SKU
    # no estaba en maestra en el momento de la carga inicial.
    _dim_src_cols = [c for c in ["AREA", "LINEA", "SUBLINEA", "MARCA"] if c in maestra.columns]
    if _dim_src_cols and "SKU_PRODUCTO" in maestra.columns:
        _m_dim = (
            maestra[["SKU_PRODUCTO"] + _dim_src_cols]
            .drop_duplicates("SKU_PRODUCTO")
        )
        # Quitar columnas de dims en df_ft que estén todas vacías/NULL para merge limpio
        for _c in _dim_src_cols:
            if _c in df_ft.columns:
                _all_empty = df_ft[_c].replace("", pd.NA).isna().all()
                if _all_empty:
                    df_ft = df_ft.drop(columns=[_c])
        df_ft = df_ft.merge(_m_dim, on="SKU_PRODUCTO", how="left")

    # ── Completar NOM_PROVEEDOR vacío desde vw_producto (maestra) ───────────
    if "PROVEEDOR" in maestra.columns:
        _prov_map = (
            maestra[["SKU_PRODUCTO", "PROVEEDOR"]]
            .drop_duplicates("SKU_PRODUCTO")
            .set_index("SKU_PRODUCTO")["PROVEEDOR"]
        )
        if "NOM_PROVEEDOR" not in df_ft.columns:
            df_ft["NOM_PROVEEDOR"] = pd.NA
        _mask_prov = df_ft["NOM_PROVEEDOR"].replace("", pd.NA).isna()
        if _mask_prov.any():
            df_ft.loc[_mask_prov, "NOM_PROVEEDOR"] = (
                df_ft.loc[_mask_prov, "SKU_PRODUCTO"].map(_prov_map)
            )

    # ── Proyección desde proy_result.parquet (opcional) ────────────────────
    parquet_path = Path(__file__).resolve().parent.parent / "data" / "inputs" / "proy_result.parquet"
    df_pq_rows = []  # lista vacía por defecto si el parquet no existe

    if not parquet_path.exists():
        st.warning(
            "⚠️ No se encontró la proyección guardada (`proy_result.parquet`). "
            "El resumen se genera **sólo con las OCs de FT_COMPRAS**. "
            "Para incluir compras proyectadas, primero corre la **Proyección de Stock** "
            "y luego vuelve aquí."
        )
        df_pq = pd.DataFrame()
    else:
        try:
            df_pq = norm_cols(pd.read_parquet(parquet_path))
        except Exception as e:
            st.warning(f"⚠️ No se pudo leer proy_result.parquet ({e}). Se usarán sólo OCs de FT_COMPRAS.")
            df_pq = pd.DataFrame()

    if not df_pq.empty:
        df_pq["FORECAST_COMPRA"] = pd.to_numeric(
            df_pq["FORECAST_COMPRA"] if "FORECAST_COMPRA" in df_pq.columns else 0,
            errors="coerce",
        ).fillna(0)
        df_pq = df_pq[df_pq["FORECAST_COMPRA"] > 0].copy()
        _periodo_pq = df_pq["PERIODO"] if "PERIODO" in df_pq.columns else pd.Series(dtype="datetime64[ns]")
        eta_pq = pd.to_datetime(_periodo_pq, errors="coerce")
        mask_ano_pq = eta_pq.dt.year == anio_actual
        df_pq = df_pq[mask_ano_pq].copy()
        eta_pq = eta_pq[mask_ano_pq]

        # El parquet ya trae AREA, LINEA, SUBLINEA, MARCA, PROCEDENCIA,
        # MIX_OFICIAL, COSTO_FOB_USD, ULTIMO_COSTO — no se necesita merge con maestra.
        proc_pq = df_pq["PROCEDENCIA"].fillna("") if "PROCEDENCIA" in df_pq.columns else pd.Series("", index=df_pq.index)
        df_pq["_FACTOR"] = [
            _lookup_factor(r.get("AREA", ""), r.get("LINEA", ""), r.get("MARCA", ""), p)
            for r, p in zip(df_pq.to_dict("records"), proc_pq)
        ]
        mask_imp_pq = proc_pq.str.strip().str.upper() == "IMPORTADO"
        costo_fob = pd.to_numeric(
            df_pq["COSTO_FOB_USD"] if "COSTO_FOB_USD" in df_pq.columns else 0,
            errors="coerce",
        ).fillna(0)
        costo_loc = pd.to_numeric(
            df_pq["ULTIMO_COSTO"] if "ULTIMO_COSTO" in df_pq.columns else 0,
            errors="coerce",
        ).fillna(0)
        unds_pq = df_pq["FORECAST_COMPRA"]

        df_pq["CURRENCY"] = np.where(mask_imp_pq, "USD", "PEN")
        df_pq["AMOUNT"] = np.where(mask_imp_pq, costo_fob * unds_pq, costo_loc * unds_pq)
        df_pq["AMOUNT_SOLES"] = np.where(
            mask_imp_pq,
            costo_fob * df_pq["_FACTOR"] * tc * unds_pq,
            costo_loc * unds_pq,
        )
        df_pq["ETD"] = ""
        df_pq["ETA"] = eta_pq.dt.strftime("%d/%m/%Y").fillna("").values
        df_pq["MES ETA"] = eta_pq.dt.month.map(MESES_FULL).values
        df_pq["Días agua"] = ""
        df_pq["COMPRA_UNDS"] = unds_pq
        df_pq["FUENTE"] = "proy_result.parquet"
        df_pq["STATUS_PO"] = "Compra Proy"
        df_pq["N_PO"] = "-"
        df_pq["SKU"] = df_pq["SKU_PRODUCTO"]
        nom_col_pq = "SKU_NOM_PRODUCTO" if "SKU_NOM_PRODUCTO" in df_pq.columns else "SKU_PRODUCTO"
        df_pq["PRODUCTO"] = df_pq[nom_col_pq].fillna("")
        df_pq["MIX"] = df_pq["MIX_OFICIAL"].fillna("") if "MIX_OFICIAL" in df_pq.columns else ""
        # ── Lookup NOM_PROVEEDOR, COD_PROVEEDOR y SUBLINEA desde vw_producto ────
        _pq_sku = df_pq["SKU_PRODUCTO"]
        _maestra_idx = maestra.drop_duplicates("SKU_PRODUCTO").set_index("SKU_PRODUCTO")

        df_pq["NOM_PROVEEDOR"] = (
            _pq_sku.map(_maestra_idx["PROVEEDOR"]).fillna("")
            if "PROVEEDOR" in maestra.columns else ""
        )
        df_pq["COD_PROVEEDOR"] = (
            _pq_sku.map(_maestra_idx["COD_PROVEEDOR"]).fillna("")
            if "COD_PROVEEDOR" in maestra.columns else ""
        )
        if "SUBLINEA" in maestra.columns:
            if "SUBLINEA" not in df_pq.columns:
                df_pq["SUBLINEA"] = pd.NA
            _mask_sl = df_pq["SUBLINEA"].replace("", pd.NA).isna()
            if _mask_sl.any():
                df_pq.loc[_mask_sl, "SUBLINEA"] = _pq_sku[_mask_sl].map(_maestra_idx["SUBLINEA"])

        df_pq["ORIGEN"] = proc_pq.values
        df_pq_rows = [df_pq]

    # ── Concat final ────────────────────────────────────────────────────────
    COLS_SALIDA = [
        "ORIGEN", "STATUS_PO", "N_PO", "NOM_PROVEEDOR", "COD_PROVEEDOR",
        "AMOUNT", "CURRENCY", "_FACTOR", "AMOUNT_SOLES",
        "ETD", "ETA", "MES ETA", "Días agua",
        "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX", "SKU", "PRODUCTO",
        "COMPRA_UNDS", "FUENTE",
    ]
    frames_to_concat = []
    for _frame in [df_ft] + df_pq_rows:
        for col in COLS_SALIDA:
            if col not in _frame.columns:
                _frame[col] = ""
        frames_to_concat.append(_frame[COLS_SALIDA])

    df = pd.concat(frames_to_concat, ignore_index=True)
    df = df[df["AREA"].astype(str).isin(AREAS_VALIDAS)]
    df = df.rename(columns={
        "_FACTOR":      "Factor Importación",
        "AMOUNT":       "Amount (Moneda Orig.)",
        "AMOUNT_SOLES": "Amount Soles c/Factor",
        "COMPRA_UNDS":  "Compra Unds",
        "FUENTE":       "Fuente",
        "ORIGEN":       "Origen",
        "MES ETA":      "Mes ETA",
        "Días agua":    "Días en Agua",
    })
    return df


# ─── Tabla Stock Cierre MES ────────────────────────────────────────────────────

def _render_stock_cierre_tabla(conn):
    """
    Tabla visual 'Stock cierre MES' (año actual, 12 meses).
    Reutiliza la lógica de Flujo de Costos para calcular los valores a nivel CIA.

    Filas:
      VENTA A COSTO DEL MES              → Vta Costo Fcst (real cerrado + fcst futuro)
      TT RECEPCIONES EFECTIVAS DEL MES   → TT S/. + Compras Pry S/.
      TOTAL ON HAND AL CIERRE DE MES     → Stk Final S/.
      TOTAL ON HAND + TT CIERRE DE MES   → Stk[M] + TT_RECEPCIONES[M+1]
      Meses de stock (todos los meses)   → MOI
    """
    from modules.flujo_costos import (
        _fetch_ventas, _fetch_stock, _fetch_pos,
        _compute_transitos, _compute_compras_pry,
        _load_factor_override, _load_budget_df, _load_fcst_override_df,
        _all_periods, _closed_set, _build_matrix, _period_label,
    )

    today    = datetime.today()
    periods  = _all_periods()
    closed   = _closed_set()

    # Solo meses del año actual para esta tabla
    curr_periods = [p for p in periods if p.year == today.year]
    curr_labels  = [_period_label(p) for p in curr_periods]
    cur_lbl      = _period_label(pd.Timestamp(today.year, today.month, 1))

    # ── Datos ─────────────────────────────────────────────────────────────────
    _cid       = id(conn)
    factor_ovr = _load_factor_override()

    try:
        df_ventas = _fetch_ventas(_cid, _conn=conn)
        df_stock  = _fetch_stock(_cid,  _conn=conn)
        # Usar datos raw de POs — formato correcto para _compute_transitos
        df_pos_fc = _fetch_pos(_cid,    _conn=conn)
    except Exception as e:
        st.warning(f"Error cargando datos para tabla Stock cierre: {e}")
        return

    df_tt  = _compute_transitos(df_pos_fc, factor_ovr)
    df_cp  = _compute_compras_pry(factor_ovr)
    df_bud = _load_budget_df()
    df_fov = _load_fcst_override_df()

    try:
        df_matrix = _build_matrix(
            ventas=df_ventas, stock=df_stock,
            transitos=df_tt, compras_pry=df_cp,
            budget=df_bud, fcst_override=df_fov,
            periods=periods, closed=closed, area_filter=None,
        )
    except Exception as e:
        st.warning(f"Error construyendo matriz: {e}")
        return

    # ── Extraer fila CIA ───────────────────────────────────────────────────────
    def _cia_val(metric: str, lbl: str) -> float:
        mask = (df_matrix["__nivel__"] == "CIA") & (df_matrix["Fecha"] == metric)
        row  = df_matrix[mask]
        if row.empty or lbl not in row.columns:
            return 0.0
        try:
            return float(row[lbl].iloc[0] or 0)
        except (TypeError, ValueError):
            return 0.0

    # ── Construir las 5 filas ──────────────────────────────────────────────────
    vta_costo = [_cia_val("Vta Costo Fcst", lbl) for lbl in curr_labels]
    tt_rec    = [_cia_val("TT S/.", lbl) + _cia_val("Compras Pry S/.", lbl)
                 for lbl in curr_labels]
    stk_hand  = [_cia_val("Stk Final S/.", lbl) for lbl in curr_labels]
    moi       = [_cia_val("MOI", lbl) for lbl in curr_labels]

    # TOTAL ON HAND + TT CIERRE = Stk[i] + TT_REC[i+1]
    onhand_tt = []
    for i, lbl in enumerate(curr_labels):
        tt_next = tt_rec[i + 1] if i + 1 < len(curr_labels) else 0.0
        onhand_tt.append(stk_hand[i] + tt_next)

    # ── HTML ──────────────────────────────────────────────────────────────────
    NAVY      = "#1F3563"
    GREEN_HDR = "#00B050"
    YELLOW_BG = "#FFFF00"
    LABEL_W   = "220px"
    CELL_W    = "90px"
    BORDER    = "#C8CBD4"

    def _fmt_int(v: float) -> str:
        return "—" if v == 0 else f"{v:,.0f}"

    def _fmt_moi(v: float) -> str:
        return "—" if v == 0 else f"{v:.2f}"

    rows_data = [
        ("VENTA A COSTO DEL MES",           vta_costo, _fmt_int, "#FFFFFF"),
        ("TT RECEPCIONES EFECTIVAS DEL MES", tt_rec,   _fmt_int, "#FFFFFF"),
        ("TOTAL ON HAND AL CIERRE DE MES",   stk_hand, _fmt_int, "#FFFFFF"),
        ("TOTAL ON HAND + TT CIERRE DE MES", onhand_tt,_fmt_int, "#FFFFFF"),
        ("Meses de stock (todos los meses)", moi,      _fmt_moi, YELLOW_BG),
    ]

    # Header
    th_base = (
        f"padding:6px 10px;border:1px solid {BORDER};"
        f"color:#FFFFFF;font-weight:bold;font-size:11px;text-align:center;"
    )
    header_cells = (
        f"<th style='{th_base}background:{NAVY};width:{LABEL_W};text-align:left;'>Indicador</th>"
    )
    for lbl in curr_labels:
        bg = GREEN_HDR if lbl == cur_lbl else NAVY
        header_cells += f"<th style='{th_base}background:{bg};width:{CELL_W};'>{lbl}</th>"

    # Body
    body_rows = ""
    for label, values, fmt_fn, row_bg in rows_data:
        td_label = (
            f"<td style='padding:5px 8px;border:1px solid {BORDER};"
            f"background:{NAVY};color:#FFFFFF;font-weight:bold;"
            f"font-size:11px;width:{LABEL_W};'>{label}</td>"
        )
        cells = td_label
        for i, v in enumerate(values):
            lbl = curr_labels[i]
            bg  = GREEN_HDR if lbl == cur_lbl else row_bg
            fc  = "#FFFFFF" if bg in (NAVY, GREEN_HDR) else "#000000"
            cells += (
                f"<td style='padding:5px 8px;border:1px solid {BORDER};"
                f"background:{bg};color:{fc};font-size:11px;"
                f"text-align:right;'>{fmt_fn(v)}</td>"
            )
        body_rows += f"<tr>{cells}</tr>"

    html = (
        "<div style='overflow-x:auto;'>"
        "<table style='border-collapse:collapse;font-family:Segoe UI,Arial,sans-serif;"
        "width:100%;margin-bottom:12px;'>"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{body_rows}</tbody>"
        "</table></div>"
    )
    st.markdown("#### Stock Cierre MES")
    st.html(html)


def _render_exportar_plan_compra(conn, df_pos, maestra):
    """Tab: tabla Stock cierre MES + carga Factor de Importación y genera resumen Plan de Compra."""
    # ── Tabla Stock Cierre MES ─────────────────────────────────────────────────
    _render_stock_cierre_tabla(conn)
    st.markdown("---")
    st.markdown("#### Resumen Plan de Compra")
    st.caption(
        "Combina las OC pendientes de **FT_COMPRAS** con las compras proyectadas "
        "de **proy_result.parquet**. Sube el archivo de factores para calcular montos en soles."
    )

    uploaded = st.file_uploader(
        "📂 Cargar FactordeImportacion.xlsx",
        type=["xlsx"],
        key="uploader_factor_importacion",
        help=(
            "Columna 1: clave GRUPO+LINEA+MARCA (mayúsculas, sin espacios internos). "
            "Columna 5 (índice 4): factor numérico. "
            "Si un SKU no tiene match se usa 1.3 por defecto; nacionales usan 1."
        ),
    )

    if uploaded is None:
        st.info("Sube el archivo **FactordeImportacion.xlsx** para habilitar la generación.")
        return

    if st.button("🚀 Generar Resumen", key="btn_gen_plan_compra", type="primary"):
        with st.spinner("Procesando datos..."):
            try:
                df_result = _generar_plan_compra(df_pos, maestra, uploaded)
            except Exception as e:
                st.error(f"Error al generar el resumen: {e}")
                return

        if df_result is None or df_result.empty:
            st.warning("No se generaron datos. Verifica los filtros y el parquet.")
            return

        ft_rows = int((df_result["Fuente"] == "ft_compras").sum())
        pq_rows = int((df_result["Fuente"] == "proy_result.parquet").sum())

        col1, col2, col3 = st.columns(3)
        col1.metric("Total filas", f"{len(df_result):,}")
        col2.metric("OC FT_COMPRAS", f"{ft_rows:,}")
        col3.metric("Compras Proyectadas", f"{pq_rows:,}")

        st.dataframe(df_result, use_container_width=True, height=420)
        download_buttons(df_result, "resumen_plan_compra")


# ─── Entry point ───────────────────────────────────────────────────────────────

def render_plan_compras(conn):
    """Módulo Plan de Compras & OTB."""
    st.html("<h2 class='sub-header'>Plan de Compras & OTB</h2>")
    st.caption(
        "Tránsitos, inventario proyectado y Open to Buy. "
        "Pipeline = Stock On Hand + En Agua + Pendiente Zarpe."
    )

    # ── Botón actualizar ────────────────────────────────────────────────────
    col_ref, col_reset = st.columns([1, 1])
    with col_ref:
        if st.button("🔄 Actualizar Datos", key="btn_refresh_pc"):
            st.cache_data.clear()
            keys_to_del = [k for k in st.session_state if k.startswith("pc_inv_") or k.startswith("_pc_")]
            for k in keys_to_del:
                del st.session_state[k]
            st.rerun()
    with col_reset:
        if st.button("🗑️ Limpiar Caché Completo", key="btn_reset_pc"):
            # Limpia TODO el session_state relacionado con plan_compras
            keys_to_del = [k for k in list(st.session_state.keys())
                           if k.startswith("pc_") or k.startswith("_pc_")]
            for k in keys_to_del:
                del st.session_state[k]
            st.cache_data.clear()
            st.success("Caché limpiado. Recargando...")
            st.rerun()

    # ── Cargar datos ────────────────────────────────────────────────────────
    try:
        df_pos_raw, df_stock, maestra, df_vcosto_hist = _load_data(conn, _query_hash=_PC_QUERY_HASH)
    except Exception as e:
        st.error(f"Error cargando datos: {e}")
        return

    if df_pos_raw.empty:
        st.warning("No se encontraron órdenes de compra.")
        return

    # Enriquecer POs con factor importación y costos CLP
    df_pos = _enrich_pos(df_pos_raw, maestra)
    df_pos = apply_pm_filter(df_pos)

    # ── Obtener df_proy de session_state si está disponible ────────────────
    df_proy = st.session_state.get("df_proy", None)
    if df_proy is not None:
        df_proy = apply_pm_filter(df_proy)

    # Limpieza de claves obsoletas (versiones anteriores del cache_key)
    stale = [k for k in st.session_state if k.startswith("pc_inv_") and not k.startswith("pc_inv_v15_")]
    for k in stale:
        del st.session_state[k]

    if df_proy is not None:
        has_cogs = "COGS_RES_TOTAL" in df_proy.columns
        proy_rows_fc = len(df_proy[df_proy["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])]) if "TIPO_DATO" in df_proy.columns else len(df_proy)
        cogs_sum = df_proy["COGS_RES_TOTAL"].sum() if has_cogs else 0
        st.success(
            f"✅ Forecast cargado: {proy_rows_fc:,} filas de proyección · "
            f"Total Venta Costo FC: ${cogs_sum/1e6:,.1f}M"
        )
    else:
        st.warning(
            "⚠️ Sin forecast de venta costo — los meses futuros muestran el stock sin descuento por ventas. "
            "Para proyectar correctamente, genera una proyección en **📈 Proyección de Stock** y vuelve aquí."
        )

    # ── Filtros globales (Área / Línea / solo activos) ─────────────────────
    st.markdown("### Filtros globales")
    gf1, gf2, gf3 = st.columns(3)
    with gf1:
        areas_g = ["Todas"] + sorted(df_pos["AREA"].dropna().unique().tolist())
        sel_area_g = st.selectbox("Área", areas_g, key="pc_g_area")
    with gf2:
        base_g = df_pos if sel_area_g == "Todas" else df_pos[df_pos["AREA"] == sel_area_g]
        lineas_g = ["Todas"] + sorted(base_g["LINEA"].dropna().unique().tolist())
        sel_linea_g = st.selectbox("Línea", lineas_g, key="pc_g_linea")
    with gf3:
        solo_vigentes = st.toggle(
            "Solo POs vigentes (excluir cerradas)",
            value=True, key="pc_vigentes",
            help="Excluye POs con STATUS = Cerrada. Incluye: Abierta, Confirmada, En proceso."
        )

    # Filtro por año (segmented control, default año actual)
    _anos_eta = sorted(df_pos["PERIODO_ETA"].dropna().dt.year.unique().tolist())
    if not _anos_eta:
        _anos_eta = [datetime.today().year]
    _ano_actual = datetime.today().year
    _opciones_ano = ["Todos"] + [str(a) for a in _anos_eta]
    _default_ano = str(_ano_actual) if str(_ano_actual) in _opciones_ano else "Todos"
    sel_ano = st.segmented_control(
        "Año", _opciones_ano, default=_default_ano, key="pc_g_ano",
    )

    # Status que se consideran "cerrados/recepcionados" — no vigentes
    STATUS_CERRADOS = {"Cerrada", "PO Recepcionada", "CERRADA", "RECEPCIONADA"}

    # Aplicar filtros globales
    df_filt = df_pos.copy()
    if sel_area_g != "Todas":
        df_filt = df_filt[df_filt["AREA"] == sel_area_g]
    if sel_linea_g != "Todas":
        df_filt = df_filt[df_filt["LINEA"] == sel_linea_g]
    if solo_vigentes:
        df_filt = df_filt[~df_filt["STATUS_PO"].astype(str).isin(STATUS_CERRADOS)]

    # Guardar copia sin filtro año para la proyección (necesita todas las POs)
    df_filt_full = df_filt.copy()

    # Aplicar filtro de año para display
    if sel_ano and sel_ano != "Todos":
        _ano_sel = int(sel_ano)
        df_filt = df_filt[df_filt["PERIODO_ETA"].dt.year == _ano_sel]

    # Periodos para ordenar labels
    periodos_eta = sorted(df_filt["PERIODO_ETA"].dropna().unique().tolist())
    periodos_label = [_periodo_label(p) for p in periodos_eta]

    st.markdown("---")

    # ── Proyección de inventario (cacheada en session_state) ───────────────
    # Filtrar venta costo histórica por área/línea seleccionada
    df_vcosto_filt = df_vcosto_hist.copy()
    if sel_area_g != "Todas" and "AREA" in df_vcosto_filt.columns:
        df_vcosto_filt = df_vcosto_filt[df_vcosto_filt["AREA"] == sel_area_g]
    if sel_linea_g != "Todas" and "LINEA" in df_vcosto_filt.columns:
        df_vcosto_filt = df_vcosto_filt[df_vcosto_filt["LINEA"] == sel_linea_g]

    # Incluir en el cache_key si hay forecast (df_proy) o no,
    # Usar el df_proy original de session_state (sin filtrar) para hash estable
    _df_proy_orig = st.session_state.get("df_proy", None)
    _proy_hash = f"proy_{len(_df_proy_orig)}_{_df_proy_orig['PERIODO'].nunique()}" \
        if _df_proy_orig is not None and not _df_proy_orig.empty and "PERIODO" in _df_proy_orig.columns \
        else "noproy"
    cache_key = f"pc_inv_v15_{sel_area_g}_{sel_linea_g}_{solo_vigentes}_{_PC_QUERY_HASH}_{_proy_hash}"
    debug_key = f"{cache_key}_debug"

    # Horizonte automático: alinear con el último mes de la proyección de stock
    _n_meses = 12  # default sin forecast
    if df_proy is not None and not df_proy.empty and "PERIODO" in df_proy.columns:
        _last_p = pd.to_datetime(df_proy["PERIODO"], errors="coerce").max()
        _first_p = pd.Timestamp.today().normalize().to_period("M").to_timestamp()
        if pd.notna(_last_p):
            _diff = ((_last_p.year - _first_p.year) * 12 + _last_p.month - _first_p.month) + 1
            _n_meses = max(_diff, 12)

    if cache_key not in st.session_state:
        with lottie_spinner("stock"):
            df_inv, _debug = _build_inv_projection(
                df_filt_full, df_stock, maestra, df_proy,
                df_vcosto_hist=df_vcosto_filt,
                n_meses=_n_meses,
            )
            st.session_state[cache_key] = df_inv
            st.session_state[debug_key] = _debug
    df_inv = st.session_state[cache_key]
    _inv_debug = st.session_state.get(debug_key, {})

    # Filtrar proyección de inventario por año seleccionado (solo display)
    if sel_ano and sel_ano != "Todos":
        _ano_sel = int(sel_ano)
        df_inv = df_inv[df_inv["PERIODO"].dt.year == _ano_sel]

    # ── Tabs ───────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📦 Inventario & Tránsito",
        "🎯 Open to Buy",
        "📊 Resumen Financiero",
        "🗂️ Detalle POs",
        "📥 Exportar Plan Compra",
    ])

    with tab1:
        _render_proyeccion_inventario(df_inv)
        # Diagnóstico de Venta Costo FC
        with st.expander("🔍 Diagnóstico Venta Costo FC", expanded=False):
            src = _inv_debug.get("vcosto_source", "?")
            st.write(f"**Fuente COGS:** `{src}`")
            if src == "proyeccion":
                st.write(f"- df_proy shape: `{_inv_debug.get('proy_shape')}`")
                st.write(f"- TIPO_DATO: `{_inv_debug.get('proy_tipo_dato')}`")
                st.write(f"- Filas FC (PROYECCION+REAL+FC): `{_inv_debug.get('fc_rows')}`")
                st.write(f"- Filas después dropna AREA/LINEA: `{_inv_debug.get('fc_rows_after_dropna')}`")
                st.write(f"- COGS sum raw: `${_inv_debug.get('cogs_sum_raw', 0):,.0f}`")
                st.write(f"- df_vcosto shape: `{_inv_debug.get('vcosto_shape')}`")
                st.write(f"- df_vcosto sum: `${_inv_debug.get('vcosto_sum', 0):,.0f}`")
                st.write(f"- Areas vcosto: `{_inv_debug.get('vcosto_areas')}`")
                st.write(f"- Areas stock: `{_inv_debug.get('stock_areas')}`")
                st.write(f"- Periodos vcosto: `{_inv_debug.get('vcosto_periodos')}`")
                st.write(f"- Periodos range: `{_inv_debug.get('periodos_range')}`")
            elif src == "historico":
                st.write("Usando promedio histórico (no hay proyección stock disponible)")
            else:
                st.write("⚠️ Sin fuente de venta costo. Genera una proyección en **Proyección de Stock**.")
            # Mostrar totales de la tabla resultado
            if "VENTA_COSTO_CLP" in df_inv.columns:
                total_vc = df_inv["VENTA_COSTO_CLP"].sum()
                st.write(f"- **Total Venta Costo en tabla:** `${total_vc:,.0f}`")

        # ── Breakdown En Agua FC por Área/Línea ──────────────────────────
        _fc_aguas_df = _inv_debug.get("fc_aguas_df")
        if _fc_aguas_df is not None and not _fc_aguas_df.empty:
            with st.expander("🚢 Desglose En Agua FC por Área / Línea / Mes", expanded=False):
                st.caption(
                    "Muestra las compras proyectadas (Forecast Compra × Costo Unitario) "
                    "que estarían **en tránsito** por mes de arribo. Solo incluye meses FC "
                    "posteriores a la última ETA COMEX de cada Área/Línea."
                )
                _fca = _fc_aguas_df.copy()
                _fca["MES_ARRIBO"] = _fca["PERIODO_MES"].apply(_periodo_label)
                _fca["ETD_EST"] = _fca["_ETD_FC"].apply(
                    lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) else ""
                )

                # ── Tabla pivot: Área/Línea × Mes Arribo ─────────────
                _piv = _fca.pivot_table(
                    index=["AREA", "LINEA"],
                    columns="MES_ARRIBO",
                    values="FC_CLP",
                    aggfunc="sum",
                    fill_value=0,
                )
                # Ordenar columnas cronológicamente
                _sorted_months = (
                    _fca.sort_values("PERIODO_MES")["MES_ARRIBO"]
                    .drop_duplicates().tolist()
                )
                _piv = _piv[[c for c in _sorted_months if c in _piv.columns]]
                _piv["TOTAL"] = _piv.sum(axis=1)
                _piv = _piv.sort_values("TOTAL", ascending=False)

                # Formatear como M$
                _piv_fmt = _piv.copy()
                for _c in _piv_fmt.columns:
                    _piv_fmt[_c] = _piv_fmt[_c].apply(_fmt_mm)
                st.dataframe(_piv_fmt, use_container_width=True,
                             height=min(len(_piv_fmt) * 38 + 60, 500))

                # ── Top 10 Área/Línea por monto total FC ─────────────
                _top = _piv.nlargest(10, "TOTAL").drop(columns=["TOTAL"])
                if not _top.empty:
                    st.markdown("**Top 10 Área/Línea — Mayor FC en tránsito**")
                    _top_melt = _top.reset_index().melt(
                        id_vars=["AREA", "LINEA"],
                        var_name="Mes", value_name="FC_CLP",
                    )
                    _top_melt["AREA_LINEA"] = _top_melt["AREA"] + " / " + _top_melt["LINEA"]
                    fig_fc = go.Figure()
                    for al in _top_melt["AREA_LINEA"].unique():
                        sub = _top_melt[_top_melt["AREA_LINEA"] == al]
                        fig_fc.add_trace(go.Bar(
                            x=sub["Mes"], y=sub["FC_CLP"],
                            name=al,
                            hovertemplate="%{x}<br>%{fullData.name}<br>$%{y:,.0f}<extra></extra>",
                        ))
                    fig_fc.update_layout(**dorel_layout(
                        barmode="stack",
                        yaxis=dict(title="FC En Agua (CLP)", tickformat="$~s"),
                        xaxis=dict(title="Mes de Arribo"),
                        legend=dict(title_text="Área / Línea", font_size=9),
                        height=400,
                    ))
                    st.plotly_chart(fig_fc, use_container_width=True)

                # ── Detalle granular ─────────────────────────────────
                st.markdown("**Detalle completo FC en tránsito**")
                _det = _fca[["AREA", "LINEA", "MES_ARRIBO", "FC_UND", "FC_CLP", "ETD_EST"]].copy()
                _det = _det.sort_values(["FC_CLP"], ascending=False)
                _det["FC_CLP_FMT"] = _det["FC_CLP"].apply(_fmt_mm)
                _det["FC_UND_FMT"] = _det["FC_UND"].apply(lambda x: f"{x:,.0f}")
                _det_show = _det.rename(columns={
                    "MES_ARRIBO": "Mes Arribo",
                    "FC_UND_FMT": "Unidades FC",
                    "FC_CLP_FMT": "Monto CLP",
                    "ETD_EST": "ETD Estimado",
                })
                st.dataframe(
                    _det_show[["AREA", "LINEA", "Mes Arribo", "Unidades FC", "Monto CLP", "ETD Estimado"]],
                    use_container_width=True,
                    height=min(len(_det_show) * 38 + 60, 500),
                )

    with tab2:
        _render_otb_tab(df_inv)

    with tab3:
        _render_resumen_financiero(df_filt, periodos_label)

    with tab4:
        _render_detalle_pos(df_filt)

    with tab5:
        _render_exportar_plan_compra(conn, df_pos, maestra)
