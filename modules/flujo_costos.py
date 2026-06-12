"""
Flujo de Costos — Matriz financiera mensual por CIA / Área / Línea.

Filtros internos de área: BEBE, JUGUETERIA, VESTUARIO, TIEMPO LIBRE
Filtros internos de línea (excluidos):
  - Globales : Combos, Material POP, Muestras, Promocionales, Repuestos
  - BEBE     : Motricidad
  - JUGUETERIA: Accesorios Juguetes
Filtros internos de canal: MINORISTA ('03'), MAYORISTA ('02'), ETAIL ('06')

Stock proyectado meses futuros:
  Stk[M] = Stk[M-1] − VtaCostoFcst[M] + TT[M] + ComprasPry[M]

MOI = Stk Final / promedio anual Vta Costo Fcst  (NO móvil)
      Dic-25 y Dic-26 resaltados en verde.

Archivos opcionales en data/inputs/:
  budget_default.xlsx   — budget base (CIA | Fecha | meses)
  budget_override.xlsx  — reemplaza el default (mismo formato)
  factor_override.xlsx  — AREA | LINEA | FACTOR_IMPORTACION
  fcst_override.xlsx    — override Neta/Aporte Fcst futuros (mismo formato budget)
"""

import os
import re
import numpy as np
import pandas as pd
import streamlit as st
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from db.queries import _VCM, _PROD, QUERY_PLAN_COMPRAS, _CD_EXCLUIDOS_LIST
from db.cache import cached_query as cq, TTL_DIARIO
from utils.filters import norm_cols
from utils.export import download_buttons
from utils.ui_animations import lottie_spinner, show_empty_state
from config import TC_USD_DEFAULT

# ─── Constantes ────────────────────────────────────────────────────────────────

MESES_ES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}

_AREAS_INTERNAS       = {"BEBE", "JUGUETERIA", "VESTUARIO", "TIEMPO LIBRE"}
_AREAS_SQL            = ",".join(f"'{a}'" for a in sorted(_AREAS_INTERNAS))
_CANALES_SQL          = "('03','02','06')"

_LINEAS_EXCL_GLOBAL    = {"COMBOS", "MATERIAL POP", "MUESTRAS", "PROMOCIONALES", "REPUESTOS"}
_LINEAS_EXCL_BEBE      = {"MOTRICIDAD"}
_LINEAS_EXCL_JUGUETERIA= {"ACCESORIOS JUGUETES"}

# SQL para excluir líneas en Snowflake
_LINEAS_SQL_EXCL = """
  AND NOT (
    UPPER(TRIM(COALESCE(p.linea,''))) IN ('COMBOS','MATERIAL POP','MUESTRAS','PROMOCIONALES','REPUESTOS')
    OR (UPPER(TRIM(COALESCE(p.area,'')))='BEBE'       AND UPPER(TRIM(COALESCE(p.linea,'')))='MOTRICIDAD')
    OR (UPPER(TRIM(COALESCE(p.area,'')))='JUGUETERIA' AND UPPER(TRIM(COALESCE(p.linea,'')))='ACCESORIOS JUGUETES')
  )"""

METRICAS = [
    "Stk Final S/.",
    "Neta Budget",
    "Aporte Budget",
    "Mrg% Budget",
    "Vta Costo Budget",
    "Neta Fcst/Real",
    "Aporte Fcst/Real",
    "Mrg% Fcst/Real",
    "Vta Costo Fcst",
    "TT S/.",
    "Compras Pry S/.",
    "MOI",
]

_METRICAS_PCT = {"Mrg% Budget", "Mrg% Fcst/Real"}
_METRICAS_MOI = {"MOI"}

_STATUS_CERRADOS = {
    "CERRADA", "PO RECEPCIONADA", "CANCELADA", "CERRADO",
    "RECIBIDO", "RECEPCIONADA",
}
_CD_EXCL = (
    "AND snap.cod_bodega NOT IN ("
    + ",".join(f"'{x}'" for x in _CD_EXCLUIDOS_LIST) + ")"
)

# ─── Rutas ──────────────────────────────────────────────────────────────────────

_DATA_DIR              = Path(__file__).resolve().parent.parent / "data" / "inputs"
_BUDGET_DEFAULT_PATH   = _DATA_DIR / "budget_default.xlsx"
_BUDGET_OVERRIDE_PATH  = _DATA_DIR / "budget_override.xlsx"
_FACTOR_OVERRIDE_PATH  = _DATA_DIR / "factor_override.xlsx"
_FCST_OVERRIDE_PATH    = _DATA_DIR / "fcst_override.xlsx"

# ─── SQL ────────────────────────────────────────────────────────────────────────

_Q_VENTAS = f"""
SELECT
    DATE_TRUNC('month', a.fecha)                                    AS PERIODO,
    UPPER(TRIM(COALESCE(p.area,  'SIN AREA')))                      AS AREA,
    UPPER(TRIM(COALESCE(p.linea, 'SIN LINEA')))                     AS LINEA,
    SUM(CASE WHEN a.cantidad > 0 AND a.neto > 0 THEN a.neto   ELSE 0 END) AS NETA_REAL,
    SUM(CASE WHEN a.cantidad > 0 AND a.neto > 0 THEN a.aporte ELSE 0 END) AS APORTE_REAL
FROM {_VCM} a
LEFT JOIN {_PROD} p ON a.sku_producto = p.sku_producto
WHERE a.fecha >= DATE_TRUNC('year', DATEADD('year', -1, CURRENT_DATE()))
  AND a.fecha <  DATE_TRUNC('month', CURRENT_DATE())
  AND a.cantidad > 0
  AND TRIM(a.cod_canal) IN {_CANALES_SQL}
  AND UPPER(TRIM(COALESCE(p.area, ''))) IN ({_AREAS_SQL})
{_LINEAS_SQL_EXCL}
GROUP BY 1, 2, 3
"""

_Q_STOCK = f"""
SELECT
    DATE_TRUNC('month', snap.fecha)                                 AS PERIODO,
    UPPER(TRIM(COALESCE(p.area,  'SIN AREA')))                      AS AREA,
    UPPER(TRIM(COALESCE(p.linea, 'SIN LINEA')))                     AS LINEA,
    SUM(snap.stock_costo)                                           AS STOCK_COSTO
FROM db_supply.hst.ht_in_stock snap
LEFT JOIN {_PROD} p ON snap.sku_producto = p.sku_producto
WHERE snap.fecha IN (
    SELECT MAX(t.fecha)
    FROM db_supply.hst.ht_in_stock t
    WHERE t.fecha >= DATE_TRUNC('year', DATEADD('year', -1, CURRENT_DATE()))
      AND t.fecha <  CURRENT_DATE()
    GROUP BY DATE_TRUNC('month', t.fecha)
)
{_CD_EXCL}
  AND UPPER(TRIM(COALESCE(p.area, ''))) IN ({_AREAS_SQL})
{_LINEAS_SQL_EXCL}
GROUP BY 1, 2, 3
"""

# ─── Helper: excluir línea en Python ───────────────────────────────────────────

def _is_linea_excluded(area: str, linea: str) -> bool:
    a = str(area).strip().upper()
    l = str(linea).strip().upper()
    if not l:
        return False  # línea vacía se filtra en otro lado
    if l in _LINEAS_EXCL_GLOBAL:
        return True
    if a == "BEBE" and l in _LINEAS_EXCL_BEBE:
        return True
    if a == "JUGUETERIA" and l in _LINEAS_EXCL_JUGUETERIA:
        return True
    return False

# ─── Helpers de período ─────────────────────────────────────────────────────────

def _period_label(ts: pd.Timestamp) -> str:
    return f"{MESES_ES[ts.month]}-{str(ts.year)[2:]}"

def _all_periods() -> list:
    today = datetime.today()
    return pd.date_range(
        pd.Timestamp(today.year - 1, 1, 1),
        pd.Timestamp(today.year, 12, 1),
        freq="MS",
    ).tolist()

def _closed_set() -> set:
    today = datetime.today()
    cur   = pd.Timestamp(today.year, today.month, 1)
    return {p for p in _all_periods() if p < cur}

# ─── Carga Snowflake (cacheada 24 h) ───────────────────────────────────────────

@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _fetch_ventas(_cid, _conn=None) -> pd.DataFrame:
    df = norm_cols(pd.read_sql(_Q_VENTAS, _conn))
    df["PERIODO"]     = pd.to_datetime(df["PERIODO"])
    df["NETA_REAL"]   = pd.to_numeric(df["NETA_REAL"],   errors="coerce").fillna(0)
    df["APORTE_REAL"] = pd.to_numeric(df["APORTE_REAL"], errors="coerce").fillna(0)
    return df

@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _fetch_stock(_cid, _conn=None) -> pd.DataFrame:
    df = norm_cols(pd.read_sql(_Q_STOCK, _conn))
    df["PERIODO"]     = pd.to_datetime(df["PERIODO"])
    df["STOCK_COSTO"] = pd.to_numeric(df["STOCK_COSTO"], errors="coerce").fillna(0)
    return df

@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _fetch_pos(_cid, _conn=None) -> pd.DataFrame:
    return norm_cols(pd.read_sql(QUERY_PLAN_COMPRAS, _conn))

@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _fetch_pos_enriched(_cid, _conn=None) -> pd.DataFrame:
    """
    POs enriquecidos con AREA/LINEA/MARCA/PROCEDENCIA/FACTOR_IMPORTACION/MONTO_CLP
    desde el maestro de productos — misma lógica que _enrich_pos en plan_compras.
    Esto garantiza que _compute_transitos use el mismo conjunto de filas y
    valores que _generar_plan_compra (Resumen Plan de Compra).
    """
    from db.cache import cached_query as cq

    df = norm_cols(pd.read_sql(QUERY_PLAN_COMPRAS, _conn))
    try:
        maestra = cq.maestra(_conn)
    except Exception:
        return df  # si falla, devolver raw

    # Columnas que el wrapper Peru puede traer como NULL (se rellenan desde maestra)
    _null_cols = ["AREA", "LINEA", "SUBLINEA", "MARCA", "MODELO",
                  "FACTOR_IMPORTACION", "NOM_PRODUCTO"]
    for col in _null_cols:
        if col in df.columns and df[col].isna().all():
            df = df.drop(columns=[col])

    # Detectar la clave SKU automáticamente — compatible con ortografía español/portugués
    # (maestra puede tener SKU_PRODUTO ó SKU_PRODUTO según el portal)
    _maestra_key = next((c for c in maestra.columns if c.startswith("SKU_PROD")), None)
    _df_key      = next((c for c in df.columns      if c.startswith("SKU_PROD")), None)
    if _maestra_key is None or _df_key is None:
        return df  # no se puede hacer el merge → devolver raw

    _mcols = [_maestra_key]
    for c in ["SKU_NOM_PRODUTO", "SKU_NOM_PRODUTO", "NOM_PRODUTO", "NOM_PRODUTO",
              "AREA", "LINEA", "SUBLINEA", "MARCA", "MODELO",
              "FACTOR_IMPORTACION", "PROCEDENCIA", "ULTIMO_COSTO", "COSTO_FOB_USD"]:
        if c in maestra.columns and c not in _mcols:
            _mcols.append(c)

    _m = maestra[_mcols].drop_duplicates(_maestra_key)
    if _maestra_key != _df_key:
        _m = _m.rename(columns={_maestra_key: _df_key})

    # Eliminar de df las columnas que vendrán de maestra para evitar sufijos _x/_y
    _overwrite = [c for c in _m.columns if c != _df_key and c in df.columns]
    if _overwrite:
        df = df.drop(columns=_overwrite)

    df = df.merge(_m, on=_df_key, how="left")

    # Recalcular MONTO_CLP para filas donde es nulo/0
    if "MONTO_CLP" in df.columns:
        _mask = df["MONTO_CLP"].isna() | (df["MONTO_CLP"] <= 0)
        if _mask.any():
            _idx = df.index
            _fi  = pd.to_numeric(df.get("FACTOR_IMPORTACION",  pd.Series(1.0, index=_idx)), errors="coerce").fillna(1.0)
            _tpo = pd.to_numeric(df.get("TC_PO",               pd.Series(3.60, index=_idx)), errors="coerce").fillna(3.60)
            _mo  = pd.to_numeric(df.get("MONTO_MONEDA_ORIG",   pd.Series(0.0, index=_idx)), errors="coerce").fillna(0.0)
            df.loc[_mask, "MONTO_CLP"] = _mo[_mask] * _fi[_mask] * _tpo[_mask]

    return df

# ─── Factor override loader ─────────────────────────────────────────────────────

def _load_factor_override() -> dict:
    """
    Lee factor_override.xlsx con el mismo formato que usa Plan de Compras:
      col[0] = clave concatenada GRUPO+LINEA+MARCA sin espacios (ej. 'BebeCochesInfanti')
      col[4] = factor de importación

    Devuelve {KEY_UPPER_NOSPACE: factor}.
    Match exacto: si no existe clave → 1.3 (sin fallback).
    """
    if not _FACTOR_OVERRIDE_PATH.exists():
        return {}
    try:
        df = pd.read_excel(_FACTOR_OVERRIDE_PATH)
        if df.shape[1] < 5:
            # Archivo con menos de 5 columnas — no es el formato esperado
            return {}
        keys    = (df.iloc[:, 0]
                   .astype(str).str.strip().str.upper()
                   .str.replace(r"\s+", "", regex=True))
        factors = pd.to_numeric(df.iloc[:, 4], errors="coerce")
        return {k: v for k, v in zip(keys, factors) if pd.notna(v) and v > 0}
    except Exception:
        return {}


def _get_factor_from_ovr(ovr: dict, area: str, linea: str,
                         marca: str = "", default: float = 1.3) -> float:
    """
    Busca factor por clave exacta AREA+LINEA+MARCA (sin espacios, mayúsculas).
    Sin fallback: si no hay match devuelve default (1.3).
    Misma lógica que _lookup_factor en plan_compras.py.
    """
    key = (
        str(area).strip().upper().replace(" ", "")
        + str(linea).strip().upper().replace(" ", "")
        + str(marca).strip().upper().replace(" ", "")
    )
    return ovr.get(key, default)

# ─── Tránsito (TT S/.) ─────────────────────────────────────────────────────────

def _compute_transitos(df_pos: pd.DataFrame, factor_ovr: dict) -> pd.DataFrame:
    EMPTY = pd.DataFrame(columns=["PERIODO", "AREA", "LINEA", "TT_SOLES"])

    # ── Atajo: si existe el Resumen Plan Compra generado, usarlo directamente ──
    _resumen = st.session_state.get("_fc_resumen_generado")
    if _resumen is not None and not _resumen.empty and "Fuente" in _resumen.columns:
        try:
            df_ft = _resumen[_resumen["Fuente"] == "ft_compras"].copy()
            if not df_ft.empty and "ETA" in df_ft.columns and "Amount Soles c/Factor" in df_ft.columns:
                # Convertir ETA string ("01/06/2026") a período
                eta_s = pd.to_datetime(df_ft["ETA"], dayfirst=True, errors="coerce")
                df_ft["PERIODO"] = eta_s.dt.to_period("M").dt.to_timestamp()
                df_ft["AREA"]    = df_ft["AREA"].astype(str).str.upper().str.strip()
                df_ft["LINEA"]   = df_ft["LINEA"].astype(str).str.upper().str.strip()
                df_ft["AMOUNT_SOLES"] = pd.to_numeric(df_ft["Amount Soles c/Factor"], errors="coerce").fillna(0)
                result = (df_ft.groupby(["PERIODO", "AREA", "LINEA"], as_index=False)["AMOUNT_SOLES"]
                          .sum().rename(columns={"AMOUNT_SOLES": "TT_SOLES"}))
                if not result.empty:
                    return result
        except Exception:
            pass  # Fallback al cálculo normal

    # TC: misma clave que usa Plan de Compras (_get_tc → tc_usd_pen) para que
    # TT S/. en Flujo de Costos coincida con Amount Soles c/Factor (ft_compras)
    tc = float(st.session_state.get("tc_usd_pen", TC_USD_DEFAULT))
    if df_pos is None or df_pos.empty:
        return EMPTY

    df = df_pos.copy()
    df["QTY_PENDIENTE"] = pd.to_numeric(
        df.get("QTY_PENDIENTE", pd.Series(0, index=df.index)), errors="coerce"
    ).fillna(0)
    df = df[df["QTY_PENDIENTE"] > 0].copy()
    if "STATUS_PO" in df.columns:
        df = df[~df["STATUS_PO"].astype(str).str.upper().str.strip().isin(_STATUS_CERRADOS)]
    if df.empty:
        return EMPTY

    for c in ("AREA", "LINEA"):
        if c not in df.columns:
            df[c] = f"SIN {c}"
    df["AREA"]  = df["AREA"].astype(str).str.upper().str.strip()
    df["LINEA"] = df["LINEA"].astype(str).str.upper().str.strip()
    df["MARCA"] = df["MARCA"].astype(str).str.upper().str.strip() if "MARCA" in df.columns else ""

    proc     = df.get("PROCEDENCIA", pd.Series("", index=df.index)).fillna("").astype(str).str.upper().str.strip()
    mask_imp = proc == "IMPORTADO"

    # Factor base del campo FACTOR_IMPORTACION del raw PO (0 → 1.3)
    fi_base = pd.to_numeric(
        df.get("FACTOR_IMPORTACION", pd.Series(1.3, index=df.index)), errors="coerce"
    ).fillna(1.3)
    fi_base = fi_base.where(fi_base > 0, 1.3)

    if factor_ovr:
        factor = df.apply(
            lambda r: _get_factor_from_ovr(
                factor_ovr, r["AREA"], r["LINEA"],
                r.get("MARCA", ""), float(fi_base.at[r.name])
            ), axis=1
        ).astype(float)
    else:
        factor = fi_base

    # Misma fórmula que _generar_plan_compra en Plan de Compras:
    #   Importado → MONTO_MONEDA_ORIG (lineamount USD) × FI × TC
    #   Nacional  → MONTO_CLP (montomn PEN, ya en soles)
    monto_orig = pd.to_numeric(
        df.get("MONTO_MONEDA_ORIG", pd.Series(0, index=df.index)), errors="coerce"
    ).fillna(0)
    monto_clp = pd.to_numeric(
        df.get("MONTO_CLP", pd.Series(0, index=df.index)), errors="coerce"
    ).fillna(0)
    df["AMOUNT_SOLES"] = np.where(
        mask_imp,
        monto_orig * factor * tc,   # Importado: lineamount × FI × TC
        monto_clp,                  # Nacional:  monto en PEN (ya convertido)
    )

    eta   = pd.to_datetime(df.get("ETA_CALC", pd.Series(dtype="datetime64[ns]")), errors="coerce")
    # Remover timezone si existe (Snowflake puede devolver tz-aware timestamps)
    if hasattr(eta, "dt") and getattr(eta.dt, "tz", None) is not None:
        eta = eta.dt.tz_localize(None)
    today = datetime.today()
    cur_month = pd.Timestamp(today.year, today.month, 1)
    # Solo ETA año en curso; NaT se mantiene para reasignar luego
    keep  = (eta.dt.year.fillna(0).astype(int) == today.year) | eta.isna()
    df, eta = df[keep].copy(), eta[keep].copy()
    # Meses pasados del año actual → mes en curso
    eta = eta.apply(lambda ts: cur_month if pd.notna(ts) and ts < cur_month else ts)
    eta = eta.fillna(cur_month)
    # Calcular PERIODO como primer día del mes
    df = df.copy()  # garantizar copia independiente antes de asignar
    df["PERIODO"] = eta.dt.to_period("M").dt.to_timestamp()
    if "PERIODO" not in df.columns:  # fallback defensivo
        df["PERIODO"] = cur_month

    # Filtros internos
    df = df[df["AREA"].isin(_AREAS_INTERNAS)].copy()
    df = df[~df.apply(lambda r: _is_linea_excluded(r["AREA"], r["LINEA"]), axis=1)].copy()

    if df.empty or "PERIODO" not in df.columns:
        return pd.DataFrame(columns=["PERIODO", "AREA", "LINEA", "TT_SOLES"])

    return (df.groupby(["PERIODO", "AREA", "LINEA"], as_index=False)["AMOUNT_SOLES"]
              .sum().rename(columns={"AMOUNT_SOLES": "TT_SOLES"}))

# ─── Compras Proyectadas ────────────────────────────────────────────────────────

def _compute_compras_pry(factor_ovr: dict) -> pd.DataFrame:
    EMPTY = pd.DataFrame(columns=["PERIODO", "AREA", "LINEA", "COMPRA_PRY_SOLES"])

    # ── Atajo: si existe el Resumen Plan Compra generado, usarlo directamente ──
    _resumen = st.session_state.get("_fc_resumen_generado")
    if _resumen is not None and not _resumen.empty and "Fuente" in _resumen.columns:
        try:
            df_pq = _resumen[_resumen["Fuente"] == "proy_result.parquet"].copy()
            if not df_pq.empty and "ETA" in df_pq.columns and "Amount Soles c/Factor" in df_pq.columns:
                eta_s = pd.to_datetime(df_pq["ETA"], dayfirst=True, errors="coerce")
                df_pq["PERIODO"] = eta_s.dt.to_period("M").dt.to_timestamp()
                df_pq["AREA"]    = df_pq["AREA"].astype(str).str.upper().str.strip()
                df_pq["LINEA"]   = df_pq["LINEA"].astype(str).str.upper().str.strip()
                df_pq["AMOUNT_SOLES"] = pd.to_numeric(df_pq["Amount Soles c/Factor"], errors="coerce").fillna(0)
                result = (df_pq.groupby(["PERIODO", "AREA", "LINEA"], as_index=False)["AMOUNT_SOLES"]
                          .sum().rename(columns={"AMOUNT_SOLES": "COMPRA_PRY_SOLES"}))
                if not result.empty:
                    return result
        except Exception:
            pass

    tc      = st.session_state.get("fc_tc_pen", _TC_PEN_DEFAULT)
    parquet = _DATA_DIR / "proy_result.parquet"

    # 1️⃣ Intentar parquet guardado en disco
    if parquet.exists():
        try:
            df = norm_cols(pd.read_parquet(parquet))
        except Exception as e:
            st.warning(f"Error leyendo proy_result.parquet: {e}")
            df = pd.DataFrame()
    else:
        df = pd.DataFrame()

    # 2️⃣ Fallback: resultado de Proyección en sesión actual
    if df.empty and "df_proy" in st.session_state:
        df_ss = st.session_state["df_proy"]
        if isinstance(df_ss, pd.DataFrame) and not df_ss.empty:
            df = norm_cols(df_ss.copy())

    # 3️⃣ Sin datos → aviso accionable
    if df.empty:
        return EMPTY

    if "FORECAST_COMPRA" not in df.columns or "PERIODO" not in df.columns:
        return EMPTY

    df["FORECAST_COMPRA"] = pd.to_numeric(df["FORECAST_COMPRA"], errors="coerce").fillna(0)
    df = df[df["FORECAST_COMPRA"] > 0].copy()
    df["_PER"] = pd.to_datetime(df["PERIODO"], errors="coerce")
    df = df[df["_PER"].dt.year == datetime.today().year].copy()
    if df.empty:
        return EMPTY

    for c in ("AREA", "LINEA"):
        if c not in df.columns:
            df[c] = f"SIN {c}"
    df["AREA"]  = df["AREA"].astype(str).str.upper().str.strip()
    df["LINEA"] = df["LINEA"].astype(str).str.upper().str.strip()
    df["MARCA"] = df["MARCA"].astype(str).str.upper().str.strip() if "MARCA" in df.columns else ""

    proc     = df.get("PROCEDENCIA", pd.Series("", index=df.index)).fillna("").str.upper().str.strip()
    mask_imp = proc == "IMPORTADO"
    fob      = pd.to_numeric(df.get("COSTO_FOB_USD",  pd.Series(0, index=df.index)), errors="coerce").fillna(0)
    loc      = pd.to_numeric(df.get("ULTIMO_COSTO",   pd.Series(0, index=df.index)), errors="coerce").fillna(0)

    # Factor base del parquet (0 → 1.3 como default seguro)
    fi_base = (pd.to_numeric(df.get("FACTOR_IMPORTACION", pd.Series(1.3, index=df.index)),
                             errors="coerce").fillna(1.3))
    fi_base = fi_base.where(fi_base > 0, 1.3)

    if factor_ovr:
        factor = df.apply(
            lambda r: _get_factor_from_ovr(
                factor_ovr, r["AREA"], r["LINEA"],
                r.get("MARCA", ""), float(fi_base.at[r.name])
            ), axis=1
        ).astype(float)
    else:
        factor = fi_base

    df["COMPRA_PRY_SOLES"] = np.where(
        mask_imp,
        fob * factor * tc * df["FORECAST_COMPRA"],
        loc * df["FORECAST_COMPRA"],
    )

    df["PERIODO"] = df["_PER"].dt.to_period("M").dt.to_timestamp()
    df = df[df["AREA"].isin(_AREAS_INTERNAS)]
    df = df[~df.apply(lambda r: _is_linea_excluded(r["AREA"], r["LINEA"]), axis=1)]

    return df.groupby(["PERIODO", "AREA", "LINEA"], as_index=False)["COMPRA_PRY_SOLES"].sum()


# ─── Diagnóstico Compras Pry S/. + TT ──────────────────────────────────────────

def _render_diagnostico_compras(factor_ovr: dict, df_pos_raw: pd.DataFrame):
    """Expander con desglose de Compras Pry S/. por línea + tabla de tránsitos."""
    with st.expander("🔍 Diagnóstico Compras Pry S/. y TT (detalle por línea)", expanded=False):
        tc = _get_tc_pen()
        hoy = datetime.today()
        st.caption(f"TC USD/PEN: **{tc:.2f}**  ·  Áreas: BEBE · JUGUETERIA · VESTUARIO · TIEMPO LIBRE")

        # ── Estado del factor override ───────────────────────────────────────
        if factor_ovr:
            ovr_preview = "  |  ".join(
                f"{k}: {v:.4f}" for k, v in list(factor_ovr.items())[:8]
            )
            st.success(f"✅ Factor override — {len(factor_ovr)} regla(s):  {ovr_preview}")
        else:
            st.warning(
                "⚠️ Sin factor override activo. "
                "Sube un archivo con columnas **Area_FI | Linea_FI | Marca_FI | FI** "
                "en *Archivos de configuración*. "
                "Se usa 1.3 para factores = 0 en el parquet."
            )

        # ═══════════════════════════════════════════════════════════════════════
        # SECCIÓN 1 — COMPRAS PRY S/.
        # ═══════════════════════════════════════════════════════════════════════
        st.markdown("### 📦 Compras Proyectadas (proy_result.parquet)")

        parquet = _DATA_DIR / "proy_result.parquet"
        raw = pd.DataFrame()
        if parquet.exists():
            try:
                raw = norm_cols(pd.read_parquet(parquet))
            except Exception as e:
                st.error(f"Error leyendo parquet: {e}"); return
        elif "df_proy" in st.session_state:
            df_ss = st.session_state["df_proy"]
            if isinstance(df_ss, pd.DataFrame) and not df_ss.empty:
                raw = norm_cols(df_ss.copy())
                st.info("ℹ️ Usando proyección de la sesión actual (parquet no encontrado en disco).")
        if raw.empty:
            st.warning(
                "Sin datos de Compras Proyectadas. "
                "Ve a **Proyección de Stock**, carga los archivos y presiona **Procesar**."
            )
            return

        for c in ("AREA", "LINEA"):
            if c not in raw.columns: raw[c] = f"SIN {c}"
        raw["AREA"]  = raw["AREA"].astype(str).str.upper().str.strip()
        raw["LINEA"] = raw["LINEA"].astype(str).str.upper().str.strip()
        raw["MARCA"] = raw["MARCA"].astype(str).str.upper().str.strip() if "MARCA" in raw.columns else ""

        if "FORECAST_COMPRA" not in raw.columns or "PERIODO" not in raw.columns:
            st.error("El parquet no tiene columnas FORECAST_COMPRA o PERIODO."); return

        raw["FORECAST_COMPRA"] = pd.to_numeric(raw["FORECAST_COMPRA"], errors="coerce").fillna(0)
        raw["_PER"] = pd.to_datetime(raw["PERIODO"], errors="coerce")

        # ── Solo áreas internas y año actual ────────────────────────────────
        df_yr = raw[
            (raw["_PER"].dt.year == hoy.year) &
            (raw["AREA"].isin(_AREAS_INTERNAS))
        ].copy()

        if df_yr.empty:
            st.warning(f"Sin datos en el parquet para {hoy.year} en las áreas habilitadas."); return

        # ── Selectores área / línea ──────────────────────────────────────────
        _TODAS = "— Todas —"

        # Si el botón Limpiar fue pulsado en la vuelta anterior, borramos las
        # claves ANTES de crear los widgets (única forma válida en Streamlit)
        if st.session_state.pop("_diag_reset_pending", False):
            st.session_state.pop("diag_area",  None)
            st.session_state.pop("diag_linea", None)

        areas_disp = [_TODAS] + sorted(df_yr["AREA"].unique())

        # Si el valor guardado ya no existe en las opciones, borrarlo antes de crear el widget
        if st.session_state.get("diag_area", _TODAS) not in areas_disp:
            st.session_state.pop("diag_area", None)

        col_a, col_l, col_reset = st.columns([2, 2, 1])
        sel_area = col_a.selectbox("Área", options=areas_disp, key="diag_area")

        # Líneas dependen del área seleccionada
        lineas_src = df_yr if sel_area == _TODAS else df_yr[df_yr["AREA"] == sel_area]
        lineas_disp = [_TODAS] + sorted(lineas_src["LINEA"].unique())

        # Si el valor guardado de línea ya no aplica al área actual, borrarlo
        if st.session_state.get("diag_linea", _TODAS) not in lineas_disp:
            st.session_state.pop("diag_linea", None)

        sel_linea = col_l.selectbox("Línea", options=lineas_disp, key="diag_linea")

        with col_reset:
            st.markdown("<div style='margin-top:28px'>", unsafe_allow_html=True)
            if st.button("🔄 Limpiar", key="diag_reset_filtros"):
                # Marcar para limpiar en la próxima vuelta (no podemos tocar
                # claves de widgets ya creados en esta misma vuelta)
                st.session_state["_diag_reset_pending"] = True
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

        # Aplicar filtros
        df_sel = df_yr.copy()
        if sel_area  != _TODAS: df_sel = df_sel[df_sel["AREA"]  == sel_area]
        if sel_linea != _TODAS: df_sel = df_sel[df_sel["LINEA"] == sel_linea]
        df_sel = df_sel[df_sel["FORECAST_COMPRA"] > 0].copy()

        if df_sel.empty:
            st.info("Sin unidades proyectadas de compra para esta área/línea.")
        else:
            proc     = df_sel.get("PROCEDENCIA", pd.Series("", index=df_sel.index)).fillna("").str.upper().str.strip()
            mask_imp = proc == "IMPORTADO"
            fob      = pd.to_numeric(df_sel.get("COSTO_FOB_USD", pd.Series(0, index=df_sel.index)), errors="coerce").fillna(0)
            loc      = pd.to_numeric(df_sel.get("ULTIMO_COSTO",  pd.Series(0, index=df_sel.index)), errors="coerce").fillna(0)
            fi_par   = pd.to_numeric(df_sel.get("FACTOR_IMPORTACION", pd.Series(1.3, index=df_sel.index)), errors="coerce").fillna(1.3)
            fi_par   = fi_par.where(fi_par > 0, 1.3)
            marca_s  = df_sel.get("MARCA", pd.Series("", index=df_sel.index)).fillna("").astype(str).str.upper().str.strip()

            if factor_ovr:
                factor_used = df_sel.apply(
                    lambda r: _get_factor_from_ovr(
                        factor_ovr, r["AREA"], r["LINEA"],
                        r.get("MARCA", ""), float(fi_par.at[r.name])
                    ), axis=1
                ).astype(float)
                def _has_ovr(r):
                    a = str(r.get("AREA","")).strip().upper().replace(" ","")
                    l = str(r.get("LINEA","")).strip().upper().replace(" ","")
                    m = str(r.get("MARCA","")).strip().upper().replace(" ","")
                    return (a+l+m) in factor_ovr or (a+l) in factor_ovr or a in factor_ovr
                factor_src = df_sel.apply(
                    lambda r: "override" if _has_ovr(r) else "parquet→1.3", axis=1
                )
            else:
                factor_used = fi_par.copy()
                factor_src  = pd.Series("parquet→1.3", index=df_sel.index)

            df_sel["_PROC"]          = proc
            df_sel["_COSTO_BASE"]    = np.where(mask_imp, fob, loc)
            df_sel["_TIPO_COSTO"]    = np.where(mask_imp, "FOB_USD", "ULTIMO_COSTO")
            df_sel["_FACTOR_USADO"]  = factor_used
            df_sel["_FACTOR_FUENTE"] = factor_src
            df_sel["_COMPRA_SOLES"]  = np.where(
                mask_imp,
                fob * factor_used * tc * df_sel["FORECAST_COMPRA"],
                loc * df_sel["FORECAST_COMPRA"],
            )
            df_sel["_PERIODO_LBL"] = df_sel["_PER"].dt.to_period("M").dt.to_timestamp().apply(_period_label)

            # Fórmula
            st.markdown(
                "**Fórmula:** "
                "Importado → `COSTO_FOB_USD × FACTOR × TC × Fcst_Compra`  |  "
                "Local → `ULTIMO_COSTO × Fcst_Compra`"
            )

            # Tabla por SKU
            sku_col  = next((c for c in df_sel.columns if c in ("SKU_PRODUCTO","SKU","COD_PRODUCTO")), None)
            nom_col  = next((c for c in df_sel.columns if "NOM" in c and "PROD" in c), None)
            show_c   = ([sku_col] if sku_col else []) + ([nom_col] if nom_col else []) + \
                       (["MARCA"] if "MARCA" in df_sel.columns else []) + \
                       ["_PERIODO_LBL","_PROC","FORECAST_COMPRA","_TIPO_COSTO",
                        "_COSTO_BASE","_FACTOR_USADO","_FACTOR_FUENTE","_COMPRA_SOLES"]
            show_c   = [c for c in show_c if c in df_sel.columns]
            df_show  = df_sel[show_c].rename(columns={
                sku_col:          "SKU",          nom_col:         "Descripción",
                "MARCA":          "Marca",
                "_PERIODO_LBL":   "Periodo",      "_PROC":         "Procedencia",
                "FORECAST_COMPRA":"Fcst Compra",  "_TIPO_COSTO":   "Tipo Costo",
                "_COSTO_BASE":    "Costo Base",   "_FACTOR_USADO": "Factor",
                "_FACTOR_FUENTE": "Fuente Factor","_COMPRA_SOLES": "Compra Pry S/.",
            })
            for col in ["Costo Base","Compra Pry S/."]:
                if col in df_show: df_show[col] = df_show[col].apply(lambda v: f"{v:,.2f}" if pd.notna(v) else "—")
            if "Factor"      in df_show: df_show["Factor"]      = df_show["Factor"].apply(lambda v: f"{v:.4f}" if pd.notna(v) else "—")
            if "Fcst Compra" in df_show: df_show["Fcst Compra"] = df_show["Fcst Compra"].apply(lambda v: f"{v:,.0f}" if pd.notna(v) else "—")
            st.dataframe(df_show, use_container_width=True, height=280)

            # Resumen mensual
            st.markdown("**📅 Resumen mensual Compra Pry S/.**")
            order = {_period_label(p): i for i, p in enumerate(_all_periods())}
            monthly = (df_sel.groupby("_PERIODO_LBL")["_COMPRA_SOLES"].sum().reset_index()
                       .rename(columns={"_PERIODO_LBL":"Periodo","_COMPRA_SOLES":"Total S/."}))
            monthly["_ord"] = monthly["Periodo"].map(order).fillna(99)
            monthly = monthly.sort_values("_ord").drop(columns="_ord")
            monthly["Total S/."] = monthly["Total S/."].apply(lambda v: f"{v:,.0f}")
            if not monthly.empty:
                st.dataframe(monthly.set_index("Periodo").T, use_container_width=True)
            total_cp = df_sel["_COMPRA_SOLES"].sum()
            _lbl_area  = sel_area  if sel_area  != _TODAS else "Todas las áreas"
            _lbl_linea = sel_linea if sel_linea != _TODAS else "todas las líneas"
            st.metric(f"Total año {hoy.year} — {_lbl_area} / {_lbl_linea}", f"S/ {total_cp:,.0f}")

            n_cero = int(((mask_imp & (fob == 0)) | (~mask_imp & (loc == 0))).sum())
            if n_cero:
                st.warning(f"⚠️ {n_cero} SKU(s) con costo base = 0 — no aportan a Compra Pry S/.")

        # ═══════════════════════════════════════════════════════════════════════
        # SECCIÓN 2 — TRÁNSITOS (TT S/.)
        # ═══════════════════════════════════════════════════════════════════════
        st.markdown("---")
        st.markdown("### 🚢 Tránsitos activos (TT S/.)")

        if df_pos_raw is None or df_pos_raw.empty:
            st.info("Sin datos de tránsitos disponibles.")
            return

        df_tt = df_pos_raw.copy()
        for c in ("AREA","LINEA"):
            if c not in df_tt.columns: df_tt[c] = ""
        df_tt["AREA"]  = df_tt["AREA"].astype(str).str.upper().str.strip()
        df_tt["LINEA"] = df_tt["LINEA"].astype(str).str.upper().str.strip()

        # Solo áreas internas y filas con qty pendiente
        df_tt = df_tt[df_tt["AREA"].isin(_AREAS_INTERNAS)].copy()
        if "QTY_PENDIENTE" in df_tt.columns:
            df_tt["QTY_PENDIENTE"] = pd.to_numeric(df_tt["QTY_PENDIENTE"], errors="coerce").fillna(0)
            df_tt = df_tt[df_tt["QTY_PENDIENTE"] > 0]
        if "STATUS_PO" in df_tt.columns:
            df_tt = df_tt[~df_tt["STATUS_PO"].astype(str).str.upper().str.strip().isin(_STATUS_CERRADOS)]

        # Filtrar al área/línea seleccionados
        df_tt_sel = df_tt.copy()
        if sel_area  != _TODAS: df_tt_sel = df_tt_sel[df_tt_sel["AREA"]  == sel_area]
        if sel_linea != _TODAS: df_tt_sel = df_tt_sel[df_tt_sel["LINEA"] == sel_linea]

        if df_tt_sel.empty:
            _lbl_tt = f"{sel_area} / {sel_linea}" if sel_area != _TODAS else "las áreas seleccionadas"
            st.info(f"Sin tránsitos activos para {_lbl_tt}.")
            return

        # Calcular TT_SOLES para este filtro — misma lógica que _compute_transitos
        precio_unit_tt = pd.to_numeric(df_tt_sel.get("PRECIO_UNITARIO", pd.Series(0, index=df_tt_sel.index)), errors="coerce").fillna(0)
        qty_pend_tt    = pd.to_numeric(df_tt_sel.get("QTY_PENDIENTE",   pd.Series(0, index=df_tt_sel.index)), errors="coerce").fillna(0)
        proc_tt        = df_tt_sel.get("PROCEDENCIA", pd.Series("", index=df_tt_sel.index)).fillna("").astype(str).str.upper().str.strip()
        mask_imp_tt    = proc_tt == "IMPORTADO"
        fi_tt          = pd.to_numeric(df_tt_sel.get("FACTOR_IMPORTACION", pd.Series(1.3, index=df_tt_sel.index)), errors="coerce").fillna(1.3)
        fi_tt          = fi_tt.where(fi_tt > 0, 1.3)

        if factor_ovr:
            factor_tt = df_tt_sel.apply(
                lambda r: _get_factor_from_ovr(
                    factor_ovr, r["AREA"], r["LINEA"],
                    r.get("MARCA",""), float(fi_tt.at[r.name])
                ), axis=1
            ).astype(float)
        else:
            factor_tt = fi_tt

        # Importado: Precio Unit × Qty Pendiente × FI × TC
        # Nacional:  Precio Unit × Qty Pendiente
        base_tt = precio_unit_tt * qty_pend_tt
        df_tt_sel["_TT_SOLES"] = np.where(mask_imp_tt, base_tt * factor_tt * tc, base_tt)

        # ETA label — misma lógica que _compute_transitos:
        # meses pasados del año actual → mes en curso; NaT → mes en curso
        eta_raw      = pd.to_datetime(
            df_tt_sel.get("ETA_CALC", pd.Series(dtype="datetime64[ns]")),
            errors="coerce"
        )
        cur_month_ts = pd.Timestamp(hoy.year, hoy.month, 1)
        eta_filled   = eta_raw.apply(
            lambda ts: cur_month_ts if pd.isna(ts) or ts < cur_month_ts else ts
        )
        df_tt_sel["_ETA_LBL"] = eta_filled.apply(
            lambda ts: _period_label(ts) if pd.notna(ts) else "—"
        )

        # Columnas a mostrar (PROCEDENCIA + PRECIO_UNITARIO para validar la fórmula)
        tt_show_cols = []
        for cname in ["N_PO","SKU_PRODUCTO","NOM_PRODUCTO","AREA","LINEA","SUBLINEA","MARCA",
                      "PROCEDENCIA","STATUS_PO","_ETA_LBL","QTY_PENDIENTE",
                      "PRECIO_UNITARIO","MONTO_MONEDA_ORIG","MONEDA","_TT_SOLES"]:
            if cname in df_tt_sel.columns: tt_show_cols.append(cname)

        df_tt_show = df_tt_sel[tt_show_cols].rename(columns={
            "N_PO":             "N° PO",
            "SKU_PRODUCTO":     "SKU",
            "NOM_PRODUCTO":     "Descripción",
            "SUBLINEA":         "Sublínea",
            "PROCEDENCIA":      "Procedencia",
            "STATUS_PO":        "Estado",
            "_ETA_LBL":         "ETA (mes)",
            "QTY_PENDIENTE":    "Qty Pendiente",
            "PRECIO_UNITARIO":  "Precio Unit.",
            "MONTO_MONEDA_ORIG":"Monto Línea Orig.",
            "MONEDA":           "Moneda",
            "_TT_SOLES":        "TT S/.",
        })
        for num_col in ["Precio Unit.","Monto Línea Orig.","TT S/."]:
            if num_col in df_tt_show:
                df_tt_show[num_col] = df_tt_show[num_col].apply(lambda v: f"{v:,.2f}" if pd.notna(v) else "—")
        if "Qty Pendiente" in df_tt_show:
            df_tt_show["Qty Pendiente"] = df_tt_show["Qty Pendiente"].apply(lambda v: f"{v:,.0f}" if pd.notna(v) else "—")

        st.dataframe(df_tt_show, use_container_width=True, height=280)
        total_tt   = df_tt_sel["_TT_SOLES"].sum()
        _lbl_area  = sel_area  if sel_area  != _TODAS else "Todas las áreas"
        _lbl_linea = sel_linea if sel_linea != _TODAS else "todas las líneas"
        st.metric(f"Total TT S/. — {_lbl_area} / {_lbl_linea}", f"S/ {total_tt:,.0f}")


# ─── Parsers Budget / Forecast ──────────────────────────────────────────────────

def _parse_budget_carga(path_or_file) -> pd.DataFrame:
    """Formato: CIA | Fecha (Neta Fcst/Real | Aporte Fcst/Real) | meses..."""
    try:
        raw = pd.read_excel(path_or_file, sheet_name=0)
    except Exception:
        return pd.DataFrame()

    cia_col = fecha_col = None
    month_cols: dict = {}
    for col in raw.columns:
        cu = str(col).strip().upper()
        if cu == "CIA":
            cia_col = col
        elif cu == "FECHA":
            fecha_col = col
        else:
            try:
                ts = pd.to_datetime(col)
                month_cols[col] = pd.Timestamp(ts.year, ts.month, 1)
            except Exception:
                pass

    if cia_col is None or fecha_col is None or not month_cols:
        return pd.DataFrame()

    current_area = ""
    rows_out: list = []
    for _, row in raw.iterrows():
        cia       = str(row[cia_col]).strip().upper()
        fecha_val = str(row[fecha_col]).strip().upper()
        if cia in ("", "NAN", "NONE"):
            continue
        if cia == "DJPERU":
            area, linea = "DJPERU", ""
        elif cia in _AREAS_INTERNAS:
            current_area = cia
            area, linea  = cia, ""
        else:
            area, linea = current_area, cia

        if "NETA" in fecha_val or fecha_val.startswith("VN"):
            metric = "VN"
        elif "APORTE" in fecha_val:
            metric = "AP"
        else:
            continue

        for col, periodo in month_cols.items():
            try:
                val = float(row[col]) if pd.notna(row[col]) else 0.0
            except Exception:
                val = 0.0
            rows_out.append({"AREA": area, "LINEA": linea, "PERIODO": periodo,
                             "METRICA": metric, "VALOR": val})

    if not rows_out:
        return pd.DataFrame()
    df    = pd.DataFrame(rows_out)
    df_vn = df[df["METRICA"] == "VN"].rename(columns={"VALOR": "VN_BUDGET"})[["AREA","LINEA","PERIODO","VN_BUDGET"]]
    df_ap = df[df["METRICA"] == "AP"].rename(columns={"VALOR": "APORTE_BUDGET"})[["AREA","LINEA","PERIODO","APORTE_BUDGET"]]
    out   = df_vn.merge(df_ap, on=["AREA","LINEA","PERIODO"], how="outer").fillna(0)
    out["AREA"]  = out["AREA"].astype(str).str.upper()
    out["LINEA"] = out["LINEA"].astype(str).str.upper()
    return out

_MONTH_MAP = {
    "ene":1,"jan":1,"feb":2,"mar":3,"abr":4,"apr":4,"may":5,"jun":6,
    "jul":7,"ago":8,"aug":8,"set":9,"sep":9,"oct":10,"nov":11,"dic":12,"dec":12,
}
_RE_MES = re.compile(
    r"(ene|jan|feb|mar|abr|apr|may|jun|jul|ago|aug|set|sep|oct|nov|dic|dec)[_\-\s]*(\d{2,4})",
    re.IGNORECASE,
)

def _detect_month_cols(columns: list) -> dict:
    result = {}
    for col in columns:
        m = _RE_MES.search(str(col))
        if m:
            mon  = _MONTH_MAP.get(m.group(1).lower()[:3])
            yr   = m.group(2)
            year = int("20" + yr) if len(yr) == 2 else int(yr)
            if mon:
                result[col] = pd.Timestamp(year, mon, 1)
    return result

def _parse_budget_generic(path_or_file) -> pd.DataFrame:
    try:
        raw = pd.read_excel(path_or_file, sheet_name=0, dtype=str)
        raw.columns = [str(c).strip().upper() for c in raw.columns]
        raw = raw.dropna(how="all")
    except Exception:
        return pd.DataFrame()

    if all(c in raw.columns for c in ["AREA","LINEA","PERIODO","VN_BUDGET","APORTE_BUDGET"]):
        df = raw[["AREA","LINEA","PERIODO","VN_BUDGET","APORTE_BUDGET"]].copy()
        df["PERIODO"]       = pd.to_datetime(df["PERIODO"], errors="coerce")
        df["VN_BUDGET"]     = pd.to_numeric(df["VN_BUDGET"],     errors="coerce").fillna(0)
        df["APORTE_BUDGET"] = pd.to_numeric(df["APORTE_BUDGET"], errors="coerce").fillna(0)
        df["AREA"]  = df["AREA"].fillna("").str.upper().str.strip()
        df["LINEA"] = df["LINEA"].fillna("").str.upper().str.strip()
        return df.dropna(subset=["PERIODO"])

    month_cols  = _detect_month_cols(raw.columns.tolist())
    area_col    = next((c for c in raw.columns if c == "AREA"),  None)
    linea_col   = next((c for c in raw.columns if c == "LINEA"), None)
    metrica_col = next((c for c in raw.columns if c in ("METRICA","TIPO","MEASURE")), None)
    if not month_cols or not area_col:
        return pd.DataFrame()

    rows = []
    for _, row in raw.iterrows():
        area    = str(row[area_col]).strip().upper()
        linea   = str(row.get(linea_col,"")).strip().upper() if linea_col else ""
        metrica = str(row.get(metrica_col,"VN")).strip().upper() if metrica_col else "VN"
        if area in ("","NAN","NONE"):
            continue
        if linea in ("NAN","NONE"):
            linea = ""
        for col, periodo in month_cols.items():
            raw_val = str(row.get(col,"")).replace(",",".").strip()
            try:
                val = float(raw_val) if raw_val not in ("","nan","NAN") else 0.0
            except ValueError:
                val = 0.0
            rows.append({"AREA":area,"LINEA":linea,"PERIODO":periodo,"METRICA":metrica,"VALOR":val})

    if not rows:
        return pd.DataFrame()
    df    = pd.DataFrame(rows)
    is_vn = df["METRICA"].str.contains(r"\bVN\b|NETA|VENTA",   regex=True, na=False)
    is_ap = df["METRICA"].str.contains(r"\bAP\b|APORTE|MARGIN", regex=True, na=False)
    df_vn = df[is_vn].rename(columns={"VALOR":"VN_BUDGET"})[["AREA","LINEA","PERIODO","VN_BUDGET"]]
    df_ap = df[is_ap].rename(columns={"VALOR":"APORTE_BUDGET"})[["AREA","LINEA","PERIODO","APORTE_BUDGET"]]
    out   = df_vn.merge(df_ap, on=["AREA","LINEA","PERIODO"], how="outer").fillna(0)
    out["AREA"]  = out["AREA"].astype(str).str.upper()
    out["LINEA"] = out["LINEA"].astype(str).str.upper()
    return out

def _load_df_from_path(path: Path) -> pd.DataFrame:
    df = _parse_budget_carga(path)
    return df if not df.empty else _parse_budget_generic(path)

def _load_budget_df() -> pd.DataFrame:
    for p in [_BUDGET_OVERRIDE_PATH, _BUDGET_DEFAULT_PATH]:
        if p.exists():
            df = _load_df_from_path(p)
            if not df.empty:
                return df
    return pd.DataFrame()

def _load_fcst_override_df() -> pd.DataFrame:
    if _FCST_OVERRIDE_PATH.exists():
        return _load_df_from_path(_FCST_OVERRIDE_PATH)
    return pd.DataFrame()

# ─── Índices auxiliares ─────────────────────────────────────────────────────────

def _make_index(df: pd.DataFrame, value_col: str, periods: list) -> dict:
    if df.empty or value_col not in df.columns:
        return {}
    p_set = set(periods)
    out   = {}
    df    = df.copy()
    df["PERIODO"] = pd.to_datetime(df["PERIODO"])
    df["AREA"]    = df["AREA"].astype(str).str.upper().str.strip()
    df["LINEA"]   = df["LINEA"].astype(str).str.upper().str.strip()
    for (a, l), g in df.groupby(["AREA", "LINEA"]):
        g2 = g[g["PERIODO"].isin(p_set)]
        out[(a, l)] = dict(zip(g2["PERIODO"],
                               pd.to_numeric(g2[value_col], errors="coerce").fillna(0)))
    return out

def _make_bud_index(df_bud: pd.DataFrame, periods: list) -> dict:
    if df_bud.empty:
        return {}
    p_set = set(periods)
    out   = {}
    df    = df_bud.copy()
    df["PERIODO"] = pd.to_datetime(df["PERIODO"])
    df["AREA"]    = df["AREA"].astype(str).str.upper().str.strip()
    df["LINEA"]   = df["LINEA"].astype(str).str.upper().str.strip()
    for (a, l), g in df.groupby(["AREA", "LINEA"]):
        g2 = g[g["PERIODO"].isin(p_set)]
        out[(a, l)] = {
            "VN": dict(zip(g2["PERIODO"], pd.to_numeric(g2.get("VN_BUDGET",    0), errors="coerce").fillna(0))),
            "AP": dict(zip(g2["PERIODO"], pd.to_numeric(g2.get("APORTE_BUDGET", 0), errors="coerce").fillna(0))),
        }
    return out

def _sum_idx(idx: dict, pairs: list, p: pd.Timestamp) -> float:
    return sum(idx.get(pair, {}).get(p, 0.0) for pair in pairs)

def _bud_vals(bud_idx, pairs, p, area_f=None, linea_f=None):
    if area_f is None:
        for k in [("TOTAL",""),("CIA",""),("DJPERU","")]:
            if k in bud_idx:
                return bud_idx[k]["VN"].get(p, 0.0), bud_idx[k]["AP"].get(p, 0.0)
    elif linea_f is None:
        k = (area_f, "")
        if k in bud_idx:
            return bud_idx[k]["VN"].get(p, 0.0), bud_idx[k]["AP"].get(p, 0.0)
    else:
        k = (area_f, linea_f)
        if k in bud_idx:
            return bud_idx[k]["VN"].get(p, 0.0), bud_idx[k]["AP"].get(p, 0.0)
    vn = sum(bud_idx.get(pair, {}).get("VN", {}).get(p, 0.0) for pair in pairs)
    ap = sum(bud_idx.get(pair, {}).get("AP", {}).get(p, 0.0) for pair in pairs)
    return vn, ap

# ─── Construcción de la matriz ──────────────────────────────────────────────────

def _build_matrix(ventas, stock, transitos, compras_pry, budget, fcst_override,
                  periods, closed, area_filter=None):
    today     = datetime.today()
    cur_month = pd.Timestamp(today.year, today.month, 1)
    labels    = [_period_label(p) for p in periods]

    v_idx  = _make_index(ventas,      "NETA_REAL",        periods)
    a_idx  = _make_index(ventas,      "APORTE_REAL",      periods)
    s_idx  = _make_index(stock,       "STOCK_COSTO",      periods)
    tt_idx = _make_index(transitos,   "TT_SOLES",         periods)
    cp_idx = _make_index(compras_pry, "COMPRA_PRY_SOLES", periods)
    bud    = _make_bud_index(budget,        periods)
    fovr   = _make_bud_index(fcst_override, periods)   # índice forecast override

    all_pairs: set = set()
    for d in [v_idx, a_idx, s_idx, tt_idx, cp_idx, bud, fovr]:
        all_pairs.update(d.keys())

    # Solo áreas habilitadas; excluir líneas filtradas
    area_pairs = {
        (a, l) for (a, l) in all_pairs
        if a in _AREAS_INTERNAS
        and (not l or not _is_linea_excluded(a, l))
    }

    if area_filter:
        af_upper  = {a.upper() for a in area_filter}
        area_pairs = {(a, l) for (a, l) in area_pairs if a in af_upper}

    by_area: dict = defaultdict(list)
    for (a, l) in sorted(area_pairs):
        by_area[a].append(l)

    sorted_periods = sorted(periods)
    closed_sorted  = [p for p in sorted_periods if p < cur_month]
    future_sorted  = [p for p in sorted_periods if p >= cur_month]

    rows = []

    def _add_block(label, nivel, pairs, area_f=None, linea_f=None):
        # ── 1. Valores base ──────────────────────────────────────────────────
        period_vals: dict = {}
        for p in periods:
            stk       = _sum_idx(s_idx,  pairs, p)
            neta      = _sum_idx(v_idx,  pairs, p)
            ap        = _sum_idx(a_idx,  pairs, p)
            tt        = _sum_idx(tt_idx, pairs, p)
            cp        = _sum_idx(cp_idx, pairs, p)
            vn_bud, ap_bud = _bud_vals(bud, pairs, p, area_f, linea_f)
            mrg_bud   = (ap_bud / vn_bud * 100) if vn_bud else 0.0

            if p in closed:
                neta_fr   = neta
                aporte_fr = ap
            else:
                # Forecast override tiene prioridad sobre budget para meses futuros
                ovr_vn, ovr_ap = _bud_vals(fovr, pairs, p, area_f, linea_f)
                neta_fr   = ovr_vn   if ovr_vn   != 0 else vn_bud
                aporte_fr = ovr_ap   if ovr_ap   != 0 else ap_bud

            mrg_fr = (aporte_fr / neta_fr * 100) if neta_fr else 0.0
            vcf    = neta_fr - aporte_fr

            period_vals[p] = {
                "Stk Final S/.":   stk,
                "Neta Budget":      vn_bud,
                "Aporte Budget":    ap_bud,
                "Mrg% Budget":      mrg_bud,
                "Vta Costo Budget": vn_bud - ap_bud,
                "Neta Fcst/Real":   neta_fr,
                "Aporte Fcst/Real": aporte_fr,
                "Mrg% Fcst/Real":   mrg_fr,
                "Vta Costo Fcst":   vcf,
                "TT S/.":           tt,
                "Compras Pry S/.":  cp,
            }

        # ── 2. Proyección de stock (meses futuros) ───────────────────────────
        if future_sorted:
            prev_stk = (period_vals[closed_sorted[-1]]["Stk Final S/."]
                        if closed_sorted else 0.0)
            for p in future_sorted:
                vcf_p    = period_vals[p]["Vta Costo Fcst"]
                tt_p     = period_vals[p]["TT S/."]
                cp_p     = period_vals[p]["Compras Pry S/."]
                proj_stk = prev_stk - vcf_p + tt_p + cp_p
                period_vals[p]["Stk Final S/."] = proj_stk
                prev_stk = proj_stk

        # ── 3. MOI (después de proyectar stock) ──────────────────────────────
        vcf_by_year: dict = {}
        for p in periods:
            vcf_by_year.setdefault(p.year, []).append(period_vals[p]["Vta Costo Fcst"])
        avg_vcf = {yr: (sum(v)/len(v)) if v else 0.0
                   for yr, v in vcf_by_year.items()}
        for p in periods:
            av  = avg_vcf.get(p.year, 0.0)
            stk = period_vals[p]["Stk Final S/."]
            period_vals[p]["MOI"] = (stk / av) if av else 0.0

        # ── 4. Ensamblar filas ───────────────────────────────────────────────
        rows.append({
            "CIA": label, "Fecha": "Fecha",
            **{_period_label(p): "" for p in periods},
            "__nivel__": nivel, "__header__": True,
        })
        for met in METRICAS:
            row = {"CIA": label, "Fecha": met, "__nivel__": nivel, "__header__": False}
            for p in periods:
                row[_period_label(p)] = period_vals[p].get(met, 0.0)
            rows.append(row)
        rows.append({
            "CIA": "", "Fecha": "", "__nivel__": "sep", "__header__": False,
            **{_period_label(p): "" for p in periods},
        })

    # DJPERU = suma de todas las áreas habilitadas
    _add_block("DJPERU", "CIA", sorted(area_pairs))

    for area in sorted(by_area.keys()):
        area_p = [(area, l) for l in by_area[area]]
        _add_block(area, "AREA", area_p, area_f=area)
        for linea in sorted(by_area[area]):
            if not linea or not linea.strip():
                continue
            _add_block(linea, "LINEA", [(area, linea)], area_f=area, linea_f=linea)

    return pd.DataFrame(rows)

# ─── Formato de número ──────────────────────────────────────────────────────────

def _fmt(v, metrica: str) -> str:
    if v == "" or v is None:
        return ""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if np.isnan(v):
        return ""
    if metrica in _METRICAS_PCT:
        return f"{v:.2f}%"
    if metrica in _METRICAS_MOI:
        return f"{v:.2f}" if v != 0.0 else "—"
    if v == 0:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}{abs(v):,.0f}"

# ─── HTML table ─────────────────────────────────────────────────────────────────

def _build_html_table(df_matrix: pd.DataFrame, periods: list, closed: set) -> str:
    today       = datetime.today()
    prev_labels = [_period_label(p) for p in periods if p.year < today.year]
    curr_labels = [_period_label(p) for p in periods if p.year == today.year]
    future_set  = {_period_label(p) for p in periods
                   if p >= pd.Timestamp(today.year, today.month, 1)}

    dic_prev    = f"Dic-{str(today.year - 1)[2:]}"
    dic_curr    = f"Dic-{str(today.year)[2:]}"
    moi_dic_set = {dic_prev, dic_curr}

    # Paleta
    CIA_BG   = "#FFFF00";  AREA_BG  = "#FFF59D";  LINEA_BG = "#FFFDE7"
    HDR_BG   = "#4472C4";  HDR_TXT  = "#FFFFFF"
    PREV_HDR = "#5B7FBE";  CURR_HDR = "#2F5496";  FUT_HDR  = "#1F3563"
    FUT_BG   = "#BDD7EE";  PCT_BG   = "#F2F2F2";  PCT_FUT  = "#D6EAF8"
    MOI_BG   = "#E8F8F5";  MOI_FUT  = "#D1F2EB";  MOI_DIC  = "#1E8449"
    NEG_TXT  = "#C00000";  SEP_BG   = "#E8ECF5";  MET_BG   = "#FAFAFA"
    BORDER   = "#C8CBD4"
    cia_bg_map = {"CIA": CIA_BG, "AREA": AREA_BG, "LINEA": LINEA_BG}

    # Ancho de celda ajustado para ver ~12 meses sin scroll
    CELL_W = "75px"

    css = f"""
<style>
.fc-wrap {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  font-size: 10.5px;
  overflow: auto;
  max-height: 840px;
  border: 1px solid {BORDER};
  border-radius: 6px;
  box-shadow: 0 2px 10px rgba(0,0,0,.14);
}}
.fc-tbl {{
  border-collapse: collapse;
  table-layout: fixed;
  width: max-content;
}}
.fc-tbl th, .fc-tbl td {{
  border: 1px solid {BORDER};
  padding: 2px 5px;
  white-space: nowrap;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.fc-tbl thead tr th {{ position: sticky; top: 0; z-index: 10; }}
.fc-tbl thead tr:first-child th {{ top: 0; }}
.fc-tbl thead tr:last-child  th {{ top: 24px; }}
.fc-tbl td.c-cia, .fc-tbl th.c-cia {{
  position: sticky; left: 0; z-index: 8;
  width: 88px; min-width: 88px; text-align: left;
}}
.fc-tbl td.c-met, .fc-tbl th.c-met {{
  position: sticky; left: 89px; z-index: 8;
  width: 148px; min-width: 148px; text-align: left;
}}
.fc-tbl thead th.c-cia, .fc-tbl thead th.c-met {{ z-index: 15; }}
.fc-tbl td.v, .fc-tbl td.v-fut,
.fc-tbl td.v-pct, .fc-tbl td.v-pct-fut,
.fc-tbl td.v-moi, .fc-tbl td.v-moi-fut {{
  width: {CELL_W}; min-width: {CELL_W};
}}
.fc-tbl td.v-fut     {{ background: {FUT_BG}; }}
.fc-tbl td.v-pct     {{ background: {PCT_BG}; font-style: italic; }}
.fc-tbl td.v-pct-fut {{ background: {PCT_FUT}; font-style: italic; }}
.fc-tbl td.v-moi     {{ background: {MOI_BG}; font-style: italic; }}
.fc-tbl td.v-moi-fut {{ background: {MOI_FUT}; font-style: italic; }}
.neg {{ color: {NEG_TXT} !important; font-weight: 600; }}
.fc-tbl th.yr-sep, .fc-tbl td.yr-sep {{
  background: #8899BB; width: 3px; min-width: 3px; padding: 0; border: none;
}}
.fc-tbl tr.tr-sep td {{
  background: {SEP_BG}; height: 4px; padding: 0;
  border-left: none; border-right: none;
}}
.fc-tbl tr.tr-hdr td {{ background: {HDR_BG}; color: {HDR_TXT}; font-weight: 700; }}
</style>"""

    h = [css, '<div class="fc-wrap"><table class="fc-tbl"><thead>']

    # Fila 1 — años
    h.append("<tr>")
    h.append(f'<th class="c-cia" style="background:{CURR_HDR};color:#9DB8E8;font-size:9px;vertical-align:middle;" rowspan="2">CIA</th>')
    h.append(f'<th class="c-met" style="background:{CURR_HDR};color:#9DB8E8;font-size:9px;vertical-align:middle;" rowspan="2">Fecha</th>')
    h.append(f'<th colspan="{len(prev_labels)}" style="background:{PREV_HDR};color:#D6E4F5;font-size:9px;letter-spacing:1px;text-align:center;">{today.year-1}</th>')
    h.append('<th class="yr-sep"></th>')
    h.append(f'<th colspan="{len(curr_labels)}" style="background:{FUT_HDR};color:#9DB8E8;font-size:9px;letter-spacing:1px;text-align:center;">{today.year}</th>')
    h.append("</tr><tr>")
    for lbl in prev_labels:
        h.append(f'<th style="background:{PREV_HDR};color:#E8F0FA;width:{CELL_W};">{lbl}</th>')
    h.append('<th class="yr-sep"></th>')
    for lbl in curr_labels:
        bg = FUT_HDR if lbl in future_set else CURR_HDR
        fc = "#BDD7EE"  if lbl in future_set else "#E8F0FA"
        h.append(f'<th style="background:{bg};color:{fc};width:{CELL_W};">{lbl}</th>')
    h.append("</tr></thead><tbody>")

    for idx in df_matrix.index:
        nivel   = str(df_matrix.at[idx, "__nivel__"])
        is_hdr  = bool(df_matrix.at[idx, "__header__"])
        metrica = str(df_matrix.at[idx, "Fecha"])
        cia_val = str(df_matrix.at[idx, "CIA"])
        is_pct  = metrica in _METRICAS_PCT
        is_moi  = metrica in _METRICAS_MOI

        if nivel == "sep":
            ncols = 2 + len(prev_labels) + 1 + len(curr_labels)
            h.append(f'<tr class="tr-sep"><td colspan="{ncols}"></td></tr>')
            continue

        cia_bg = cia_bg_map.get(nivel, CIA_BG)

        if is_hdr:
            h.append('<tr class="tr-hdr">')
            h.append(f'<td class="c-cia" style="background:{cia_bg};color:#000;font-weight:700;">{cia_val}</td>')
            h.append(f'<td class="c-met" style="background:{HDR_BG};">Fecha</td>')
            for _ in prev_labels:
                h.append(f'<td style="background:{HDR_BG};width:{CELL_W};"></td>')
            h.append('<td class="yr-sep" style="background:#3A5A9A;"></td>')
            for _ in curr_labels:
                h.append(f'<td style="background:{HDR_BG};width:{CELL_W};"></td>')
            h.append("</tr>")
            continue

        h.append("<tr>")
        h.append(f'<td class="c-cia" style="background:{cia_bg};"></td>')
        if is_pct:
            met_style = f"background:{PCT_BG};font-style:italic;"
        elif is_moi:
            met_style = f"background:{MOI_BG};font-style:italic;"
        else:
            met_style = f"background:{MET_BG};"
        h.append(f'<td class="c-met" style="{met_style}">{metrica}</td>')

        def _cell(lbl: str, is_future: bool) -> str:
            raw = df_matrix.at[idx, lbl] if lbl in df_matrix.columns else 0
            txt = _fmt(raw, metrica)
            neg = ""
            try:
                if not is_moi and float(raw) < 0:
                    neg = " neg"
            except (TypeError, ValueError):
                pass
            # MOI Dic resaltado
            if is_moi and lbl in moi_dic_set:
                return (f'<td style="background:{MOI_DIC};color:#FFF;'
                        f'font-weight:bold;width:{CELL_W};text-align:right;">{txt}</td>')
            if is_pct:
                cls = "v-pct-fut" if is_future else "v-pct"
            elif is_moi:
                cls = "v-moi-fut" if is_future else "v-moi"
            else:
                cls = "v-fut" if is_future else "v"
            return f'<td class="{cls}{neg}">{txt}</td>'

        for lbl in prev_labels:
            h.append(_cell(lbl, False))
        h.append('<td class="yr-sep"></td>')
        for lbl in curr_labels:
            h.append(_cell(lbl, lbl in future_set))
        h.append("</tr>")

    h.append("</tbody></table></div>")
    return "".join(h)

# ─── KPIs YTD ───────────────────────────────────────────────────────────────────

def _render_kpis(df_matrix: pd.DataFrame, closed: set):
    today       = datetime.today()
    closed_lbls = [_period_label(p) for p in sorted(closed)]
    curr_lbls   = [_period_label(p) for p in _all_periods() if p.year == today.year]

    mask = (df_matrix["__nivel__"] == "CIA") & (~df_matrix["__header__"].astype(bool))
    df_k = df_matrix[mask]

    def _sum(met, lbls):
        row = df_k[df_k["Fecha"] == met]
        if row.empty:
            return 0.0
        r = row.iloc[0]
        return sum(float(r.get(lbl) or 0) for lbl in lbls if lbl in r.index)

    neta   = _sum("Neta Fcst/Real",   closed_lbls)
    aporte = _sum("Aporte Fcst/Real", closed_lbls)
    vn_b   = _sum("Neta Budget",      closed_lbls)
    ap_b   = _sum("Aporte Budget",    closed_lbls)
    tt     = _sum("TT S/.",           curr_lbls)
    cp     = _sum("Compras Pry S/.",  curr_lbls)
    cogs   = neta - aporte
    mrg    = (aporte / neta * 100) if neta else 0.0
    mrg_b  = (ap_b   / vn_b * 100) if vn_b  else 0.0

    def _m(v):
        av = abs(v)
        if av >= 1e6: return f"S/ {v/1e6:.2f}M"
        if av >= 1e3: return f"S/ {v/1e3:.1f}K"
        return f"S/ {v:,.0f}"

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Venta Neta Real YTD",   _m(neta),
              delta=f"{_m(neta-vn_b)} vs Bud" if vn_b else None)
    c2.metric("Aporte Real YTD",       _m(aporte),
              delta=f"{_m(aporte-ap_b)} vs Bud" if ap_b else None)
    c3.metric("Mrg% Real YTD",         f"{mrg:.2f}%",
              delta=f"{mrg-mrg_b:+.2f}pp vs Bud" if mrg_b else None)
    c4.metric("Vta Costo Real YTD",    _m(cogs))
    c5.metric("TT S/. (año completo)", _m(tt))
    c6.metric("Compras Proyectadas",   _m(cp))

# ─── Panel de archivos (main page) ──────────────────────────────────────────────

_TC_PEN_DEFAULT = TC_USD_DEFAULT   # Tipo de cambio USD→PEN por defecto (config, 3.80)

def _get_tc_pen() -> float:
    """TC USD→PEN. Usa session_state['fc_tc_pen'] (ingresado en el panel) o TC_USD_DEFAULT."""
    return float(st.session_state.get("fc_tc_pen", _TC_PEN_DEFAULT))


def _render_file_panel():
    """Renderiza el panel de carga de archivos en la página principal. Devuelve factor_ovr dict."""
    with st.expander("📁 Archivos de configuración (Budget · Factor Importación · Forecast Override)", expanded=False):
        # ── Tipo de cambio PEN ───────────────────────────────────────────────
        tc_col, _ = st.columns([1, 3])
        tc_col.number_input(
            "💱 TC USD → PEN (soles)",
            min_value=1.0, max_value=20.0,
            value=TC_USD_DEFAULT,
            step=0.05, format="%.2f",
            key="fc_tc_pen",
            help="Tipo de cambio USD a Soles peruanos. Default 3.80. "
                 "Se usa para valorizar importados en TT y Compras Pry S/.",
        )
        st.divider()
        col1, col2, col3 = st.columns(3)

        # ── Budget ───────────────────────────────────────────────────────────
        with col1:
            st.markdown("**📋 Budget**")
            if _BUDGET_OVERRIDE_PATH.exists():
                st.success("✅ Override activo")
                if st.button("↩️ Restaurar default", key="fc_restore_bud"):
                    _BUDGET_OVERRIDE_PATH.unlink(missing_ok=True)
                    st.rerun()
            else:
                st.info("ℹ️ Usando default 2026")
            st.caption("Formato: **CIA | Fecha | Ene-26…Dic-26**")
            up = st.file_uploader(
                "Subir Budget (.xlsx)", type=["xlsx", "xls"],
                key="fc_up_budget",
                help="Mismo formato que Carga Fcst-Budget2026.xlsx",
            )
            if up:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                _BUDGET_OVERRIDE_PATH.write_bytes(up.read())
                st.success("Budget guardado ✓")
                st.rerun()

        # ── Factor importación ────────────────────────────────────────────────
        with col2:
            st.markdown("**🔢 Factor Importación**")
            if _FACTOR_OVERRIDE_PATH.exists():
                st.success("✅ Factor override activo")
                if st.button("↩️ Quitar factor override", key="fc_restore_fac"):
                    _FACTOR_OVERRIDE_PATH.unlink(missing_ok=True)
                    st.rerun()
            else:
                st.info("ℹ️ Usando factor del parquet (default 1.3)")
            st.caption(
                "Formato (mismo que Plan de Compras):  \n"
                "**Col 0:** ID concatenado → `BebeCochesInfanti`  \n"
                "**Col 4:** Factor de importación → `1.27`  \n"
                "Columnas: `ID | Area_FI | Linea_FI | Marca_FI | FI`"
            )
            up = st.file_uploader(
                "Subir Factor (.xlsx)", type=["xlsx", "xls"],
                key="fc_up_factor",
                help="LINEA es opcional — si se omite aplica a toda el área",
            )
            if up:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                _FACTOR_OVERRIDE_PATH.write_bytes(up.read())
                st.success("Factor guardado ✓")
                st.rerun()

        # ── Forecast Override ─────────────────────────────────────────────────
        with col3:
            st.markdown("**📝 Override Neta/Aporte Fcst futuro**")
            if _FCST_OVERRIDE_PATH.exists():
                st.success("✅ Forecast override activo")
                if st.button("↩️ Quitar forecast override", key="fc_restore_fcst"):
                    _FCST_OVERRIDE_PATH.unlink(missing_ok=True)
                    st.rerun()
            else:
                st.info("ℹ️ Sin override — usando Budget para meses futuros")
            st.caption("Formato: **CIA | Fecha | Ene-26…Dic-26** (mismo que Budget)")
            up = st.file_uploader(
                "Subir Forecast Override (.xlsx)", type=["xlsx", "xls"],
                key="fc_up_fcst",
                help="Solo aplica a meses futuros (cierra con real automáticamente)",
            )
            if up:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                _FCST_OVERRIDE_PATH.write_bytes(up.read())
                st.success("Forecast override guardado ✓")
                st.rerun()

    return _load_factor_override()

# ─── Helper: consolidar df_pos compartido por Plan de Compras ─────────────────

def _consolidate_pos(df: pd.DataFrame) -> pd.DataFrame:
    """
    El df_pos enriquecido de plan_compras puede tener columnas con sufijos _x/_y
    (de merges internos en _enrich_pos/_apply_factor_fallback).
    Esta función consolida AREA/LINEA/etc: si la columna existe pero está vacía,
    la rellena desde la versión _x (que trae el valor del raw query).
    """
    df = df.copy()
    for col in ("AREA", "LINEA", "SUBLINEA", "MARCA",
                "PROCEDENCIA", "FACTOR_IMPORTACION",
                "MONTO_MONEDA_ORIG", "MONTO_CLP", "ETA_CALC"):
        # Si la columna no existe o tiene todos vacíos, intentar desde _x o _y
        col_x, col_y = f"{col}_x", f"{col}_y"
        if col not in df.columns:
            if col_x in df.columns:
                df[col] = df[col_x]
            elif col_y in df.columns:
                df[col] = df[col_y]
        else:
            # Columna existe pero puede tener "" vacíos (puesto por _apply_factor_fallback)
            empty = df[col].isna() | (df[col].astype(str).str.strip() == "")
            if empty.any():
                if col_x in df.columns:
                    df.loc[empty, col] = df.loc[empty, col_x]
                elif col_y in df.columns:
                    df.loc[empty, col] = df.loc[empty, col_y]
    return df


def _resolve_df_pos_shared() -> pd.DataFrame | None:
    """Lee df_pos compartido de session_state y lo consolida."""
    _shared = st.session_state.get("_pc_df_pos_shared")
    if _shared is None or (isinstance(_shared, pd.DataFrame) and _shared.empty):
        return None
    return _consolidate_pos(_shared)


# ─── Render principal ────────────────────────────────────────────────────────────

def render_flujo_costos(conn):
    st.html("<h2 class='sub-header'>💸 Flujo de Costos</h2>")
    st.caption(
        "Áreas: **BEBE · JUGUETERIA · VESTUARIO · TIEMPO LIBRE** · "
        "Canales: Minorista · Mayorista · Etail · "
        "24 meses (año anterior + año actual)"
    )

    c1, c2, _ = st.columns([1, 1, 5])
    with c1:
        if st.button("🔄 Actualizar", key="btn_fc_ref"):
            _fetch_ventas.clear(); _fetch_stock.clear(); _fetch_pos.clear()
            st.rerun()
    with c2:
        if st.button("🗑️ Limpiar caché", key="btn_fc_clr"):
            st.cache_data.clear(); st.rerun()

    # Panel de archivos (en página principal)
    factor_ovr = _render_file_panel()

    # ── Datos Snowflake ───────────────────────────────────────────────────────
    _cid = id(conn)
    with lottie_spinner("snowflake"):
        try:
            df_ventas = _fetch_ventas(_cid, _conn=conn)
            df_stock  = _fetch_stock(_cid,  _conn=conn)
            # Usar datos enriquecidos de Plan de Compras si están disponibles
            # (garantiza números idénticos a Resumen Plan de Compra)
            _shared = _resolve_df_pos_shared()
            df_pos  = _shared if (_shared is not None and not _shared.empty) \
                      else _fetch_pos_enriched(_cid, _conn=conn)
        except Exception as e:
            st.error(f"Error Snowflake: {e}"); return

    df_tt = _compute_transitos(df_pos, factor_ovr)
    df_cp = _compute_compras_pry(factor_ovr)

    if not (_DATA_DIR / "proy_result.parquet").exists():
        if "df_proy" in st.session_state:
            st.info("ℹ️ Usando proyección de la sesión actual para *Compras Pry S/.* (parquet no encontrado en disco).")
        else:
            st.warning(
                "⚠️ Sin datos de *Compras Pry S/.* — Ve a **Proyección de Stock**, "
                "carga los archivos y presiona **Procesar** para generarlos."
            )

    # ── Budget + Forecast override ────────────────────────────────────────────
    df_budget = _load_budget_df()
    df_fcst_ovr = _load_fcst_override_df()

    if df_budget.empty:
        st.warning("📋 Sin budget — meses futuros y filas Budget mostrarán cero.")
    else:
        src = "override" if _BUDGET_OVERRIDE_PATH.exists() else "default 2026"
        msgs = [f"✅ Budget ({src}) · {df_budget['AREA'].nunique()} áreas"]
        if not df_fcst_ovr.empty:
            msgs.append("· Forecast override activo")
        if factor_ovr:
            msgs.append(f"· Factor override: {len(factor_ovr)} reglas")
        st.success("  ".join(msgs))

    # ── Filtro de áreas ───────────────────────────────────────────────────────
    areas_snap = (
        {str(a).upper() for a in df_ventas["AREA"].unique()}
        | {str(a).upper() for a in df_tt["AREA"].unique()}
        | {str(a).upper() for a in df_cp["AREA"].unique()}
    )
    if not df_budget.empty:
        areas_snap |= {str(a).upper() for a in df_budget["AREA"].unique()}
    areas_disp = sorted(areas_snap & _AREAS_INTERNAS)

    sel_areas = st.multiselect(
        "Filtrar Áreas",
        options=areas_disp or list(_AREAS_INTERNAS),
        default=[],
        placeholder="Todas (BEBE · JUGUETERIA · VESTUARIO · TIEMPO LIBRE)",
        key="fc_sel_areas",
    )
    area_filter = sel_areas if sel_areas else None

    # ── Matriz ────────────────────────────────────────────────────────────────
    periods = _all_periods()
    closed  = _closed_set()

    with st.spinner("Construyendo matriz…"):
        df_matrix = _build_matrix(
            ventas=df_ventas, stock=df_stock,
            transitos=df_tt, compras_pry=df_cp,
            budget=df_budget, fcst_override=df_fcst_ovr,
            periods=periods, closed=closed, area_filter=area_filter,
        )

    if df_matrix.empty:
        show_empty_state("Sin datos para la matriz actual."); return

    # ── KPIs ──────────────────────────────────────────────────────────────────
    st.markdown("---")
    closed_max = max(closed) if closed else None
    st.markdown(
        "##### 📊 Resumen YTD — CIA"
        + (f"  ·  Último mes cerrado: **{_period_label(closed_max)}**" if closed_max else "")
    )
    _render_kpis(df_matrix, closed)

    # ── Diagnóstico Compras Pry + TT ─────────────────────────────────────────
    _render_diagnostico_compras(factor_ovr, df_pos)

    # ── Leyenda ───────────────────────────────────────────────────────────────
    st.markdown("---")
    leg = st.columns(6)
    leg[0].markdown('<span style="background:#FFFF00;padding:2px 8px;border-radius:3px;font-size:11px;border:1px solid #ccc;">■ CIA</span>', unsafe_allow_html=True)
    leg[1].markdown('<span style="background:#FFF59D;padding:2px 8px;border-radius:3px;font-size:11px;border:1px solid #ccc;">■ Área</span>', unsafe_allow_html=True)
    leg[2].markdown('<span style="background:#FFFDE7;padding:2px 8px;border-radius:3px;font-size:11px;border:1px solid #ccc;">■ Línea</span>', unsafe_allow_html=True)
    leg[3].markdown('<span style="background:#BDD7EE;padding:2px 8px;border-radius:3px;font-size:11px;border:1px solid #ccc;">■ Mes futuro</span>', unsafe_allow_html=True)
    leg[4].markdown('<span style="background:#E8F8F5;padding:2px 8px;border-radius:3px;font-size:11px;border:1px solid #ccc;">■ MOI</span>', unsafe_allow_html=True)
    leg[5].markdown('<span style="background:#1E8449;color:#fff;padding:2px 8px;border-radius:3px;font-size:11px;">■ MOI Dic</span>', unsafe_allow_html=True)

    # ── Tabla ─────────────────────────────────────────────────────────────────
    st.html(_build_html_table(df_matrix, periods, closed))

    # ── Descarga ──────────────────────────────────────────────────────────────
    with st.expander("⬇️ Descargar datos", expanded=False):
        dl = df_matrix.drop(columns=["__nivel__","__header__"], errors="ignore")
        download_buttons(dl, "flujo_costos")
