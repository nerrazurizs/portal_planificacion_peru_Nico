"""Alertas de Quiebre de Stock - Prediccion de desabastecimiento por SKU."""

import io

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from datetime import datetime, timedelta
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format
from utils.export import download_buttons
from utils.ui_animations import lottie_spinner
from config import COLORS, dorel_layout, apply_pm_filter


# ============================================================================
# CONSTANTS
# ============================================================================
_SEMAFORO_COLORS = {
    "CRITICO": "#E53935",
    "ALERTA": "#FB8C00",
    "OK": "#43A047",
    "SIN VENTA": "#9E9E9E",
}

_SEMAFORO_ORDER = ["CRITICO", "ALERTA", "OK", "SIN VENTA"]

# Candidatas de nombre de columna de Leadtime en la tabla Syncro (coo_rel_proveedor_sku)
_LT_CANDIDATES = [
    "LEAD_TIME_OC", "LEAD_TIME", "LEADTIME", "LT",
    "DIAS_LEAD_TIME", "DIAS_ENTREGA", "TIEMPO_ENTREGA", "LT_DIAS",
]

_PARETO_THRESHOLD = 0.80

# SKUs excluidos explicitamente del reporte (a pedido del negocio: codigos de
# variante/color que no deben evaluarse en Alertas Quiebre). Comparacion se
# hace normalizada (upper/strip).
_EXCLUDED_SKUS = {
    "013169104CE", "013169104MO", "013169104RO", "013169104VE",
    "013155160CE", "013155160GR", "013155160GR-WH", "013155160RO", "013155160VE", "013155160VE-WH",
    "01314817CEL", "01314817CEL-WH", "01314817MOR", "01314817MOR-WH", "01314817ROS", "01314817VER", "01314817VER-WH",
    "013148128CE", "013148128MO", "013148128RO", "013148128VE",
    "013148171CE", "013148171MO", "013148171RO", "013148171VE",
    "013148174BL", "013148174EL", "013148174JR", "013148174KA", "013148174OP", "013148174ZO",
    "013148174BA", "013148174EF", "013148174JI", "013148174KO", "013148174OS", "013148174ZR",
    "013148211CE", "013148211MO", "013148211RO", "013148211VE",
    "013148241CE", "013148241MO", "013148241RO", "013148241VE",
    "01314827RM", "01314827VC",
    "01314827CEL", "01314827CEL-WH", "01314827MOR", "01314827MOR-WH",
    "01314827ROS", "01314827ROS-WH", "01314827VER", "01314827VER-WH",
    "013148271CE", "013148271CE-WH", "013148271MO", "013148271MO-WH",
    "013148271RO", "013148271RO-WH", "013148271VE", "013148271VE-WH",
    "013155272GR", "013155272VE",
    "01314844CEL", "01314844CEL-WH", "01314844MOR", "01314844MOR-WH",
    "01314844ROS", "01314844ROS-WH", "01314844VER", "01314844VER-WH",
    "013148142CE", "013148142CE-WH", "013148142MO", "013148142MO-WH",
    "013148142RO", "013148142RO-WH", "013148142VE", "013148142VE-WH",
    "013148166CE", "013148166MO", "013148166RO", "013148166VE",
    "013148441CE", "013148441MO", "013148441RO", "013148441VE",
    "013146725CV", "013146725RM",
    "013148467CE", "013148467MO", "013148467RO", "013148467VE",
}


# ============================================================================
# HELPERS
# ============================================================================

def _load_data(conn):
    """Load all data sources needed for stock-out alert analysis (centralized cache)."""
    with lottie_spinner("snowflake"):
        stock = cq.stock_proyeccion(conn)
        ventas = cq.ventas_diarias_90d(conn)
        maestra = cq.maestra(conn)
        comex = cq.comex_full(conn)
        stock_higiene = cq.stock_higiene(conn)
        tienda_dim = cq.tienda_dim(conn)
        leadtimes = cq.leadtimes(conn)
        stock_cd_hist = cq.stock_cd_diario_6m(conn)
        estado_imp = cq.estado_importacion_sku(conn)
    return (
        stock, ventas, maestra, comex, stock_higiene, tienda_dim, leadtimes,
        stock_cd_hist, estado_imp,
    )


def _process_stock(stock):
    """Aggregate stock by SKU (total across CD + TIENDA)."""
    stk = stock.groupby("SKU_PRODUCTO", as_index=False).agg(
        STOCK_TOTAL=("STOCK_UNIDADES", "sum"),
        PERFIL_TIENDAS=("PERFIL_TIENDAS", "sum"),
    )
    return stk


def _process_ventas(ventas):
    """Calculate daily avg and total sales per SKU over last 90 days."""
    if ventas.empty:
        return pd.DataFrame(columns=[
            "SKU_PRODUCTO", "VENTA_DIARIA_PROM", "VENTA_TOTAL_90D",
            "DIAS_CON_VENTA", "VN_DIARIO_EST", "VN_TOTAL_90D",
        ])

    # Count distinct days in the dataset for proper avg
    n_dias_global = max(ventas["FECHA"].nunique(), 1)

    agg_kwargs = dict(
        VENTA_TOTAL_90D=("UNIDADES", "sum"),
        DIAS_CON_VENTA=("FECHA", "nunique"),
    )
    if "NETO" in ventas.columns:
        agg_kwargs["VN_TOTAL_90D"] = ("NETO", "sum")
    by_sku = ventas.groupby("SKU_PRODUCTO", as_index=False).agg(**agg_kwargs)
    if "VN_TOTAL_90D" not in by_sku.columns:
        by_sku["VN_TOTAL_90D"] = 0.0
    by_sku["VENTA_DIARIA_PROM"] = by_sku["VENTA_TOTAL_90D"] / n_dias_global
    # Venta neta (soles) promedio diaria real, no unidades
    by_sku["VN_DIARIO_EST"] = by_sku["VN_TOTAL_90D"] / n_dias_global
    return by_sku


def _process_comex(comex):
    """Get next inbound ETA per SKU from pending (not yet received) COMEX orders.

    ETA_FINAL siempre toma la columna ETA de ft_compras (via QUERY_COMEX_FULL),
    que ya trae su propio fallback en SQL (fecha_eta -> fecha_embarque+47d).
    FECHA_ENTREGA solo se usa si por alguna razon ETA no viniera en la query.

    "Pendiente" se define por cantidad aun no recepcionada (QTY_PENDIENTE > 0),
    NO por si la fecha ETA ya paso: un pedido puede llegar atrasado (ETA vencida)
    y seguir totalmente vigente -> antes se perdia de PROXIMA_ETA por ese filtro
    de fecha, ocultando pedidos reales en transito (ej. PO con ETA vencida pero
    cantidad_ingresada = 0 y fecha_ingreso_cd = NULL).

    Solo se consideran llegadas con ano ETA = 2026 (a pedido del negocio, para
    no mezclar pedidos con ETA muy antigua/desactualizada en el dato origen).
    """
    if comex.empty:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "PROXIMA_ETA", "QTY_EN_TRANSITO"])

    for col in ["ETA", "FECHA_ENTREGA"]:
        if col in comex.columns:
            comex[col] = pd.to_datetime(comex[col], errors="coerce")

    if "ETA" in comex.columns:
        comex["ETA_FINAL"] = comex["ETA"]
    elif "FECHA_ENTREGA" in comex.columns:
        comex["ETA_FINAL"] = comex["FECHA_ENTREGA"]
    else:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "PROXIMA_ETA", "QTY_EN_TRANSITO"])

    # Cantidad pendiente real = ordenada - recepcionada (nunca negativa).
    # CANTIDAD_FINAL_CORREGIDA / CANTIDAD_CARPETA_RECEPCIONADA vienen de
    # f.cantidad_oc / f.cantidad_ingresada en el wrapper _COMPRAS.
    qty_ordenada = pd.to_numeric(comex.get("CANTIDAD_FINAL_CORREGIDA"), errors="coerce").fillna(0)
    qty_recibida = pd.to_numeric(comex.get("CANTIDAD_CARPETA_RECEPCIONADA"), errors="coerce").fillna(0)
    comex["QTY_PENDIENTE"] = (qty_ordenada - qty_recibida).clip(lower=0)

    pending = comex[
        (comex["QTY_PENDIENTE"] > 0)
        & comex["ETA_FINAL"].notna()
        & (comex["ETA_FINAL"].dt.year == 2026)
    ].copy()
    if pending.empty:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "PROXIMA_ETA", "QTY_EN_TRANSITO"])

    # SKU column might be SKU_PRODUCTO or MATERIAL
    sku_col = "SKU_PRODUCTO"
    if sku_col not in pending.columns:
        for alt_col in ["MATERIAL", "SKU", "COD_MATERIAL"]:
            if alt_col in pending.columns:
                pending = pending.rename(columns={alt_col: "SKU_PRODUCTO"})
                break

    if "SKU_PRODUCTO" not in pending.columns:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "PROXIMA_ETA", "QTY_EN_TRANSITO"])

    # SKU_PRODUCTO en comex viene de f.codigo_producto_oc (ft_compras), que puede
    # traer espacios/casing distinto al resto de las fuentes -> normalizar antes
    # de mergear o se pierden ETAs validas por mismatch silencioso.
    pending["SKU_PRODUCTO"] = pending["SKU_PRODUCTO"].astype(str).str.strip().str.upper()

    agg = pending.groupby("SKU_PRODUCTO", as_index=False).agg(
        PROXIMA_ETA=("ETA_FINAL", "min"),
        QTY_EN_TRANSITO=("QTY_PENDIENTE", "sum"),
    )
    return agg


# Estados terminales de una OC en ft_cubo_comex.estadoimportacion (universo
# verificado: Cerrado, Recibido, Sales Order, Transito, Solicitud PI). Todo lo
# que no sea terminal se considera "pendiente" (en curso).
_ESTADOS_IMPORTACION_TERMINALES = {"CERRADO", "RECIBIDO"}


def _process_estado_importacion(estado_imp):
    """Estado de importacion real por SKU (ft_cubo_comex, SIN el filtro de
    Transito/Recibido/Cerrado que usa Cumplimiento COMEX). Un SKU puede tener
    varios PO: se prioriza el PO pendiente (estado no terminal) con la fecha
    de delivery mas proxima, igual criterio que PROXIMA_ETA en _process_comex.

    Importante:
    - Se usa la fuente SIN filtro de estado (QUERY_ESTADO_IMPORTACION_SKU)
      para no perder OC en estados previos a booking (ej. 'Sales Order'); si
      se filtrara antes en SQL, un PO ya 'Cerrado' del mismo SKU podia tapar
      silenciosamente el estado real de la OC pendiente.
    - "Pendiente" se decide por c.estadoimportacion (no terminal), NO por la
      fecha de ingreso a almacen de ft_compras: esa fecha puede venir vacia
      aun cuando el PO ya esta 'Cerrado' en ft_cubo_comex (cierre
      administrativo vs. recepcion fisica registrada por separado), lo que
      hacia que un PO 'Cerrado' se marcara como pendiente por error y tapara
      la OC realmente en curso (ej. 'Sales Order') del mismo SKU.
    """
    empty = pd.DataFrame(columns=["SKU_PRODUCTO", "ESTADO_IMPORTACION"])
    if estado_imp is None or estado_imp.empty:
        return empty
    if "SKU_PRODUCTO" not in estado_imp.columns or "ESTADO_IMPORTACION" not in estado_imp.columns:
        return empty

    df = estado_imp.copy()
    df["SKU_PRODUCTO"] = df["SKU_PRODUCTO"].astype(str).str.strip().str.upper()
    df["ESTADO_IMPORTACION"] = df["ESTADO_IMPORTACION"].astype(str).str.strip()
    df["_PENDIENTE"] = ~df["ESTADO_IMPORTACION"].str.upper().isin(_ESTADOS_IMPORTACION_TERMINALES)

    sort_cols = ["SKU_PRODUCTO", "_PENDIENTE"]
    ascending = [True, False]
    if "PO_FECHA_DELIVERY" in df.columns:
        sort_cols.append("PO_FECHA_DELIVERY")
        ascending.append(True)

    df = df.sort_values(sort_cols, ascending=ascending)
    agg = df.groupby("SKU_PRODUCTO", as_index=False).first()[["SKU_PRODUCTO", "ESTADO_IMPORTACION"]]
    return agg


def _detect_lt_col(df_lt):
    """Detecta la columna de Lead Time en el DataFrame de Syncro (coo_rel_proveedor_sku)."""
    cols_upper = {c.upper(): c for c in df_lt.columns}
    for cand in _LT_CANDIDATES:
        if cand in cols_upper:
            return cols_upper[cand]
    for col in df_lt.columns:
        if "lead" in col.lower() or col.lower().startswith("lt_"):
            return col
    return None


def _process_leadtimes(df_lt):
    """Leadtime de compra (dias) por SKU desde Tablas Syncro > Leadtimes Proveedor SKU."""
    if df_lt is None or df_lt.empty:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "LT"])

    lt_col = _detect_lt_col(df_lt)
    sku_col = (
        "SKU_PRODUCTO" if "SKU_PRODUCTO" in df_lt.columns
        else "ID_MATERIAL" if "ID_MATERIAL" in df_lt.columns
        else None
    )
    if not lt_col or not sku_col:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "LT"])

    agg = (
        df_lt.rename(columns={sku_col: "SKU_PRODUCTO", lt_col: "LT"})
        .assign(SKU_PRODUCTO=lambda x: x["SKU_PRODUCTO"].astype(str).str.strip())
        .groupby("SKU_PRODUCTO", as_index=False)["LT"].max()
    )
    agg["LT"] = pd.to_numeric(agg["LT"], errors="coerce")
    return agg


def _process_fecha_quiebre_cd(stock_cd_hist, venta_prom):
    """Fecha en la que inicio el quiebre vigente en CD (stock CD < venta diaria prom).

    Recorre el historico diario de stock CD (6m) y ubica, para cada SKU
    actualmente en quiebre (ultimo dato disponible con stock CD < venta
    promedio), el primer dia de esa racha continua.
    """
    empty = pd.DataFrame(columns=["SKU_PRODUCTO", "FECHA_QUIEBRE_CD"])
    if stock_cd_hist is None or stock_cd_hist.empty or venta_prom is None or venta_prom.empty:
        return empty
    if "STOCK_CD_UND" not in stock_cd_hist.columns or "FECHA" not in stock_cd_hist.columns:
        return empty

    df = stock_cd_hist[["SKU_PRODUCTO", "FECHA", "STOCK_CD_UND"]].copy()
    df["FECHA"] = pd.to_datetime(df["FECHA"], errors="coerce")
    df["STOCK_CD_UND"] = pd.to_numeric(df["STOCK_CD_UND"], errors="coerce").fillna(0)
    df = df.merge(venta_prom[["SKU_PRODUCTO", "VENTA_DIARIA_PROM"]], on="SKU_PRODUCTO", how="inner")
    df = df[df["VENTA_DIARIA_PROM"] > 0].dropna(subset=["FECHA"])
    if df.empty:
        return empty

    df = df.sort_values(["SKU_PRODUCTO", "FECHA"])
    df["EN_QUIEBRE"] = df["STOCK_CD_UND"] < df["VENTA_DIARIA_PROM"]

    # Bloques de racha continua del mismo estado (quiebre / no quiebre) por SKU
    changed = df["EN_QUIEBRE"] != df.groupby("SKU_PRODUCTO")["EN_QUIEBRE"].shift()
    df["BLOCK"] = changed.groupby(df["SKU_PRODUCTO"]).cumsum()

    last_state = df.groupby("SKU_PRODUCTO").tail(1)[["SKU_PRODUCTO", "EN_QUIEBRE", "BLOCK"]]
    last_state = last_state[last_state["EN_QUIEBRE"]]  # solo SKUs actualmente quebrados en CD
    if last_state.empty:
        return empty

    breach_start = (
        df.merge(last_state, on=["SKU_PRODUCTO", "BLOCK"])
        .groupby("SKU_PRODUCTO")["FECHA"].min()
        .reset_index()
        .rename(columns={"FECHA": "FECHA_QUIEBRE_CD"})
    )
    return breach_start


def _classify_alerts(df):
    """Assign semaforo category based on coverage days and inbound status."""
    conditions = [
        df["SIN_VENTA"],
        (df["DIAS_COBERTURA"] < 7) & (~df["REPO_CUBRE"]),
        (df["DIAS_COBERTURA"] < 30) | ((df["DIAS_COBERTURA"] < 30) & (~df["REPO_CUBRE"])),
    ]
    choices = ["SIN VENTA", "CRITICO", "ALERTA"]
    df["SEMAFORO"] = np.select(conditions, choices, default="OK")
    return df


def _classify_alerta_oc(df):
    """ALERTA_OC: SKUs que cumplen DIAS_COBERTURA <= LT (regla de "poner orden urgente").

    - Sin PROXIMA_ETA (no hay compra en camino)   -> "PONER ORDEN URGENTE"
    - Con PROXIMA_ETA (ya hay una compra en transito) -> "Compra en Transito"
    - No cumple la regla -> "" (vacio)
    """
    cumple_regla = df["DIAS_COBERTURA"] <= df["LT"]
    conditions = [
        cumple_regla & df["PROXIMA_ETA"].notna(),
        cumple_regla & df["PROXIMA_ETA"].isna(),
    ]
    choices = ["Compra en Transito", "PONER ORDEN URGENTE"]
    df["ALERTA_OC"] = np.select(conditions, choices, default="")
    return df


def _exclude_skus(df):
    """Quita del reporte los SKUs con sufijo '-PV' y los excluidos explicitamente
    en _EXCLUDED_SKUS (codigos de variante/color que el negocio no evalua aqui)."""
    if df.empty or "SKU_PRODUCTO" not in df.columns:
        return df
    sku_norm = df["SKU_PRODUCTO"].astype(str).str.strip().str.upper()
    mask = ~(sku_norm.str.endswith("-PV") | sku_norm.isin(_EXCLUDED_SKUS))
    return df[mask].copy()


def _add_pareto_rank(df, value_col="VN_TOTAL_90D", threshold=_PARETO_THRESHOLD, mix_col="MIX"):
    """Agrega columna PARETO: ranking 1..N (1 = mayor venta) para los SKUs que
    explican el `threshold` (80% default) acumulado de la venta (VN). Los SKUs
    fuera del 80% quedan con PARETO vacio (NaN) -> ordenar/filtrar por esta
    columna reemplaza el checkbox de Pareto directamente en la tabla.

    El calculo solo considera SKUs con MIX en {"MIX", "IN & OUT"} (a pedido
    del negocio); "FUERA MIX" (y cualquier otro valor) queda excluido del
    ranking y su PARETO siempre vacio.
    """
    df = df.copy()
    df["PARETO"] = np.nan
    if df.empty or value_col not in df.columns:
        return df
    if mix_col in df.columns:
        elegibles = df[mix_col].astype(str).str.strip().str.upper().isin(["MIX", "IN & OUT"])
    else:
        elegibles = pd.Series(True, index=df.index)
    vals = pd.to_numeric(df.loc[elegibles, value_col], errors="coerce").fillna(0)
    total = vals.sum()
    if total <= 0:
        return df
    order = vals.sort_values(ascending=False)
    cum_pct = order.cumsum() / total
    keep_idx = list(cum_pct.index[cum_pct <= threshold])
    over = cum_pct[cum_pct > threshold]
    if not over.empty:
        keep_idx.append(over.index[0])  # incluir el SKU que cruza el 80%, no cortar justo antes
    rank_map = {idx: i + 1 for i, idx in enumerate(keep_idx)}
    df["PARETO"] = df.index.map(rank_map)
    return df


# ============================================================================
# MAIN ANALYSIS
# ============================================================================

def _build_alert_table(stock, ventas, maestra, comex, stock_higiene=None, tienda_dim=None,
                        leadtimes=None, stock_cd_hist=None, estado_imp=None):
    """Build the complete alert DataFrame."""
    stk = _process_stock(stock)
    stk = _exclude_skus(stk)  # fuera del reporte: sufijo -PV y lista explicita del negocio
    vta = _process_ventas(ventas)
    cmx = _process_comex(comex)

    # Base: all SKUs with stock (STOCK_TOTAL viene de stk, left-join preserva todos)
    df = stk.merge(vta, on="SKU_PRODUCTO", how="left")

    # cmx (ft_compras via codigo_producto_oc) ya viene normalizado (upper/strip) en
    # _process_comex; se mergea por clave normalizada para no perder ETAs validas
    # por diferencias de espacios/casing con SKU_PRODUCTO del resto de fuentes.
    if not cmx.empty:
        df["_SKU_KEY"] = df["SKU_PRODUCTO"].astype(str).str.strip().str.upper()
        df = df.merge(cmx.rename(columns={"SKU_PRODUCTO": "_SKU_KEY"}), on="_SKU_KEY", how="left")
        df = df.drop(columns="_SKU_KEY")
    else:
        df["PROXIMA_ETA"] = pd.NaT
        df["QTY_EN_TRANSITO"] = 0.0

    # Enrich with maestra
    maestra_cols = ["SKU_PRODUCTO", "AREA", "MIX_OFICIAL", "LINEA", "SUBLINEA", "MARCA",
                    "SKU_NOM_PRODUCTO", "PROCEDENCIA", "ULTIMO_COSTO"]
    available = [c for c in maestra_cols if c in maestra.columns]
    if available:
        maestra_dedup = maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
        df = df.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")
    if "MIX_OFICIAL" in df.columns:
        df = df.rename(columns={"MIX_OFICIAL": "MIX"})

    # Fill NaN
    df["VENTA_DIARIA_PROM"] = df["VENTA_DIARIA_PROM"].fillna(0)
    df["VENTA_TOTAL_90D"] = df["VENTA_TOTAL_90D"].fillna(0)
    df["DIAS_CON_VENTA"] = df["DIAS_CON_VENTA"].fillna(0)
    if "VN_TOTAL_90D" in df.columns:
        df["VN_TOTAL_90D"] = df["VN_TOTAL_90D"].fillna(0)
    if "VN_DIARIO_EST" in df.columns:
        df["VN_DIARIO_EST"] = df["VN_DIARIO_EST"].fillna(0)

    # Flag: sin venta
    df["SIN_VENTA"] = df["VENTA_DIARIA_PROM"] <= 0

    # Coverage days
    df["DIAS_COBERTURA"] = np.where(
        df["VENTA_DIARIA_PROM"] > 0,
        df["STOCK_TOTAL"] / df["VENTA_DIARIA_PROM"],
        999,
    )
    df["DIAS_COBERTURA"] = df["DIAS_COBERTURA"].clip(upper=999)

    # Estimated stockout date
    today = pd.Timestamp.now().normalize()
    df["FECHA_QUIEBRE_EST"] = today + pd.to_timedelta(df["DIAS_COBERTURA"].clip(upper=365), unit="D")
    df.loc[df["DIAS_COBERTURA"] >= 999, "FECHA_QUIEBRE_EST"] = pd.NaT

    # Inbound coverage check
    df["PROXIMA_ETA"] = pd.to_datetime(df["PROXIMA_ETA"], errors="coerce")

    # Pedidos con ETA ya vencida (menor a hoy) pero que siguen pendientes de
    # llegar: no tiene sentido mostrar una fecha pasada como "proxima" llegada,
    # se ajusta a fin del mes actual (a pedido del negocio) hasta tener una
    # fecha real actualizada.
    fin_mes_actual = today + pd.offsets.MonthEnd(0)
    vencida = df["PROXIMA_ETA"].notna() & (df["PROXIMA_ETA"] < today)
    df.loc[vencida, "PROXIMA_ETA"] = fin_mes_actual

    df["QTY_EN_TRANSITO"] = pd.to_numeric(df["QTY_EN_TRANSITO"], errors="coerce").fillna(0)
    df["TIENE_REPO"] = df["PROXIMA_ETA"].notna() & (df["QTY_EN_TRANSITO"] > 0)
    df["REPO_CUBRE"] = df["TIENE_REPO"] & (
        df["PROXIMA_ETA"] <= df["FECHA_QUIEBRE_EST"]
    )

    # Days gap between stockout and next inbound
    df["DIAS_GAP"] = np.where(
        df["TIENE_REPO"] & df["FECHA_QUIEBRE_EST"].notna(),
        (df["PROXIMA_ETA"] - df["FECHA_QUIEBRE_EST"]).dt.days,
        np.nan,
    )

    # Estado de importacion (ft_cubo_comex, sin filtro de estado): solo aplica
    # a SKUs con PROXIMA_ETA vigente (compra en camino); el resto queda vacio.
    est_imp = _process_estado_importacion(estado_imp)
    if not est_imp.empty:
        df["_SKU_KEY"] = df["SKU_PRODUCTO"].astype(str).str.strip().str.upper()
        df = df.merge(est_imp.rename(columns={"SKU_PRODUCTO": "_SKU_KEY"}), on="_SKU_KEY", how="left")
        df = df.drop(columns="_SKU_KEY")
    else:
        df["ESTADO_IMPORTACION"] = np.nan
    df.loc[df["PROXIMA_ETA"].isna(), "ESTADO_IMPORTACION"] = np.nan

    # Leadtime de compra por SKU (Tablas Syncro > Leadtimes Proveedor SKU)
    lt = _process_leadtimes(leadtimes)
    if not lt.empty:
        df["_SKU_KEY"] = df["SKU_PRODUCTO"].astype(str).str.strip()
        df = df.merge(lt.rename(columns={"SKU_PRODUCTO": "_SKU_KEY"}), on="_SKU_KEY", how="left")
        df = df.drop(columns="_SKU_KEY")
    else:
        df["LT"] = np.nan

    # Fecha de quiebre en CD (stock CD < venta diaria prom) + gap vs proxima ETA
    fq_cd = _process_fecha_quiebre_cd(stock_cd_hist, df[["SKU_PRODUCTO", "VENTA_DIARIA_PROM"]])
    if not fq_cd.empty:
        df = df.merge(fq_cd, on="SKU_PRODUCTO", how="left")
    else:
        df["FECHA_QUIEBRE_CD"] = pd.NaT
    df["DIAS_GAP_QUIEBRE_CD"] = np.where(
        df["FECHA_QUIEBRE_CD"].notna() & df["PROXIMA_ETA"].notna(),
        (df["PROXIMA_ETA"] - df["FECHA_QUIEBRE_CD"]).dt.days,
        np.nan,
    )

    # Venta perdida estimada en soles (VN) desde que el SKU quebro en CD hasta
    # hoy: dias en quiebre CD x venta neta diaria promedio. Vacio si no esta
    # quebrado en CD.
    df["VENTA_PERDIDA_CD"] = np.where(
        df["FECHA_QUIEBRE_CD"].notna(),
        (today - df["FECHA_QUIEBRE_CD"]).dt.days * df["VN_DIARIO_EST"],
        np.nan,
    )

    # Classify
    df = _classify_alerts(df)
    df = _classify_alerta_oc(df)

    # VN at risk = daily avg * ULTIMO_COSTO (rough estimate)
    if "ULTIMO_COSTO" in df.columns:
        df["VN_EN_RIESGO"] = df["VENTA_DIARIA_PROM"] * df["ULTIMO_COSTO"].fillna(0) * 30
    else:
        df["VN_EN_RIESGO"] = 0

    # ── Supervisor mapping: SKU → set of supervisors with stock > 0 ──────────
    # Requires stock_higiene (per-store SKU stock) + tienda_dim (store → supervisor)
    df["SUPERVISORES"] = "Sin Supervisor"
    if (
        stock_higiene is not None
        and tienda_dim is not None
        and not stock_higiene.empty
        and not tienda_dim.empty
        and "ID_SUCURSAL" in stock_higiene.columns
        and "ID_SUCURSAL" in tienda_dim.columns
        and "SUPERVISOR" in tienda_dim.columns
    ):
        try:
            # Solo tiendas con stock positivo
            _sth = stock_higiene[
                (stock_higiene.get("CANAL_DE_DISTRIBUCION", pd.Series()) == "TIENDA")
                & (pd.to_numeric(stock_higiene.get("STOCK_UNIDADES", pd.Series()), errors="coerce").fillna(0) > 0)
            ].copy() if "CANAL_DE_DISTRIBUCION" in stock_higiene.columns else stock_higiene.copy()

            _td = tienda_dim[["ID_SUCURSAL", "SUPERVISOR"]].copy()
            _td["SUPERVISOR"] = _td["SUPERVISOR"].fillna("Sin Supervisor")

            _sth_td = _sth[["SKU_PRODUCTO", "ID_SUCURSAL"]].merge(_td, on="ID_SUCURSAL", how="left")
            _sth_td = _sth_td[_sth_td["SKU_PRODUCTO"].notna()]

            # Aggregate: one comma-separated string of unique supervisors per SKU
            _sku_sup = (
                _sth_td.groupby("SKU_PRODUCTO")["SUPERVISOR"]
                .apply(lambda x: ", ".join(sorted({
                    str(s) for s in x
                    if pd.notna(s) and str(s) not in ("Sin Supervisor", "nan", "")
                })))
                .reset_index()
            )
            _sku_sup.columns = ["SKU_PRODUCTO", "SUPERVISORES"]
            _sku_sup["SUPERVISORES"] = _sku_sup["SUPERVISORES"].replace("", "Sin Supervisor")

            df = df.merge(_sku_sup, on="SKU_PRODUCTO", how="left")
            df["SUPERVISORES"] = df["SUPERVISORES"].fillna("Sin Supervisor")
        except Exception:
            pass  # si falla, queda con "Sin Supervisor" por defecto

    # Pareto: ranking por venta (VN) en vez de checkbox de filtro -> columna directa
    df = _add_pareto_rank(df, "VN_TOTAL_90D", _PARETO_THRESHOLD)

    return df


# ============================================================================
# DASHBOARD RENDERING
# ============================================================================

def _render_kpis(df):
    """Display KPI cards."""
    total = len(df)
    criticos = (df["SEMAFORO"] == "CRITICO").sum()
    alerta = (df["SEMAFORO"] == "ALERTA").sum()
    ok = (df["SEMAFORO"] == "OK").sum()
    sin_venta = (df["SEMAFORO"] == "SIN VENTA").sum()

    c1, c2, c3, c4, c5 = st.columns(5)

    with c1:
        st.html(f"""
        <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                     border-top:4px solid {COLORS["primary"]};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
            <div style='font-size:2rem;font-weight:bold;color:{COLORS["primary"]}'>{total:,}</div>
            <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>Total SKUs</div>
        </div>""")

    with c2:
        st.html(f"""
        <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                     border-top:4px solid {_SEMAFORO_COLORS["CRITICO"]};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
            <div style='font-size:2rem;font-weight:bold;color:{_SEMAFORO_COLORS["CRITICO"]}'>{criticos:,}</div>
            <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>Criticos (&lt;7d)</div>
        </div>""")

    with c3:
        st.html(f"""
        <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                     border-top:4px solid {_SEMAFORO_COLORS["ALERTA"]};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
            <div style='font-size:2rem;font-weight:bold;color:{_SEMAFORO_COLORS["ALERTA"]}'>{alerta:,}</div>
            <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>Alerta (&lt;30d)</div>
        </div>""")

    with c4:
        st.html(f"""
        <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                     border-top:4px solid {_SEMAFORO_COLORS["OK"]};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
            <div style='font-size:2rem;font-weight:bold;color:{_SEMAFORO_COLORS["OK"]}'>{ok:,}</div>
            <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>OK (&ge;30d)</div>
        </div>""")

    with c5:
        st.html(f"""
        <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                     border-top:4px solid {_SEMAFORO_COLORS["SIN VENTA"]};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
            <div style='font-size:2rem;font-weight:bold;color:{_SEMAFORO_COLORS["SIN VENTA"]}'>{sin_venta:,}</div>
            <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>Sin Venta</div>
        </div>""")

    st.html("<br>")


def _render_semaforo_chart(df):
    """Donut chart showing alert distribution."""
    counts = df["SEMAFORO"].value_counts().reset_index()
    counts.columns = ["Semaforo", "Cantidad"]

    # Ensure order
    cat_order = [s for s in _SEMAFORO_ORDER if s in counts["Semaforo"].values]
    color_range = [_SEMAFORO_COLORS[s] for s in cat_order]

    fig = go.Figure(layout=dorel_layout(
        height=350, margin=dict(l=20, r=20, t=40, b=20), showlegend=True,
        title_text="Distribucion por Semaforo",
    ))
    fig.add_trace(go.Pie(
        labels=counts["Semaforo"].tolist(),
        values=counts["Cantidad"].tolist(),
        hole=0.5,
        marker=dict(colors=color_range),
        textinfo="percent+label",
        hovertemplate="Semaforo: %{label}<br>Cantidad: %{value:,}<extra></extra>",
    ))
    st.plotly_chart(fig, use_container_width=True)


def _render_top_criticos(df):
    """Horizontal bar chart of top critical SKUs by VN at risk."""
    criticos = df[df["SEMAFORO"].isin(["CRITICO", "ALERTA"])].copy()
    if criticos.empty:
        st.info("No hay SKUs criticos ni en alerta.")
        return

    top = criticos.nlargest(20, "VN_EN_RIESGO")
    nombre_col = "SKU_NOM_PRODUCTO" if "SKU_NOM_PRODUCTO" in top.columns else "SKU_PRODUCTO"
    top["LABEL"] = top["SKU_PRODUCTO"].astype(str) + " - " + top.get(nombre_col, top["SKU_PRODUCTO"]).astype(str)
    top["LABEL"] = top["LABEL"].str[:50]

    color_range = [_SEMAFORO_COLORS.get(s, "#999") for s in ["CRITICO", "ALERTA"]]

    top_sorted = top.sort_values("VN_EN_RIESGO", ascending=True)

    fig = go.Figure(layout=dorel_layout(
        height=500, title_text="Top 20 SKUs en Riesgo (por VN Mensual)",
        xaxis_title="VN Mensual en Riesgo ($)", yaxis_title="",
    ))
    for sem in ["CRITICO", "ALERTA"]:
        sub = top_sorted[top_sorted["SEMAFORO"] == sem]
        if sub.empty:
            continue
        fig.add_trace(go.Bar(
            x=sub["VN_EN_RIESGO"],
            y=sub["LABEL"],
            orientation="h",
            name=sem,
            marker_color=_SEMAFORO_COLORS.get(sem, "#999"),
            customdata=np.column_stack([
                sub["SKU_PRODUCTO"], sub["DIAS_COBERTURA"], sub["SEMAFORO"],
            ]),
            hovertemplate=(
                "SKU: %{customdata[0]}<br>"
                "Dias Cobertura: %{customdata[1]:.0f}<br>"
                "VN en Riesgo: $%{x:,.0f}<br>"
                "Estado: %{customdata[2]}<extra></extra>"
            ),
        ))
    st.plotly_chart(fig, use_container_width=True)


def _render_timeline(df):
    """Scatter chart: expected stockout date vs daily sales rate."""
    active = df[(df["SEMAFORO"] != "SIN VENTA") & (df["FECHA_QUIEBRE_EST"].notna())].copy()
    if active.empty:
        st.info("No hay datos para timeline de quiebres.")
        return

    # Limit to next 180 days
    today = pd.Timestamp.now().normalize()
    cutoff = today + timedelta(days=180)
    active = active[active["FECHA_QUIEBRE_EST"] <= cutoff]
    if active.empty:
        st.info("No hay quiebres esperados en los proximos 180 dias.")
        return

    cat_order = [s for s in _SEMAFORO_ORDER if s in active["SEMAFORO"].unique()]
    color_range = [_SEMAFORO_COLORS[s] for s in cat_order]

    fig = go.Figure(layout=dorel_layout(
        height=400, title_text="Timeline de Quiebres Esperados (prox. 180 dias)",
        xaxis_title="Fecha Estimada de Quiebre",
        yaxis_title="Venta Diaria Prom (Und)",
    ))
    for sem, clr in zip(cat_order, color_range):
        sub = active[active["SEMAFORO"] == sem]
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub["FECHA_QUIEBRE_EST"],
            y=sub["VENTA_DIARIA_PROM"],
            mode="markers",
            name=sem,
            marker=dict(color=clr, size=8),
            customdata=np.column_stack([
                sub["SKU_PRODUCTO"], sub["DIAS_COBERTURA"], sub["STOCK_TOTAL"],
            ]),
            hovertemplate=(
                "SKU: %{customdata[0]}<br>"
                "Quiebre Est.: %{x|%Y-%m-%d}<br>"
                "Dias Cob.: %{customdata[1]:.0f}<br>"
                "Stock Actual: %{customdata[2]:,.0f}<br>"
                "Venta/Dia: %{y:.1f}<extra></extra>"
            ),
        ))
    fig.add_vline(x=today, line_dash="dash", line_color="black")
    st.plotly_chart(fig, use_container_width=True)


def _render_heatmap(df):
    """Heatmap of % critical SKUs by Area x Linea."""
    if "AREA" not in df.columns or "LINEA" not in df.columns:
        return

    active = df[df["SEMAFORO"] != "SIN VENTA"].copy()
    if active.empty:
        return

    pivot = active.groupby(["AREA", "LINEA"]).agg(
        TOTAL=("SKU_PRODUCTO", "count"),
        CRITICOS=("SEMAFORO", lambda x: (x == "CRITICO").sum()),
    ).reset_index()
    pivot["PCT_CRITICO"] = (pivot["CRITICOS"] / pivot["TOTAL"] * 100).round(1)

    # Only show combinations with data
    if pivot.empty:
        return

    lineas = sorted(pivot["LINEA"].unique())
    areas = sorted(pivot["AREA"].unique())

    # Build z-matrix (areas=rows, lineas=cols)
    z_matrix = []
    text_matrix = []
    customdata_matrix = []
    for area in areas:
        row_z = []
        row_t = []
        row_c = []
        for linea in lineas:
            match = pivot[(pivot["AREA"] == area) & (pivot["LINEA"] == linea)]
            if match.empty:
                row_z.append(None)
                row_t.append("")
                row_c.append([0, 0])
            else:
                val = match["PCT_CRITICO"].iloc[0]
                row_z.append(val)
                row_t.append(f"{val:.0f}")
                row_c.append([int(match["TOTAL"].iloc[0]), int(match["CRITICOS"].iloc[0])])
        z_matrix.append(row_z)
        text_matrix.append(row_t)
        customdata_matrix.append(row_c)

    fig = go.Figure(layout=dorel_layout(
        height=400, title_text="% SKUs Criticos por Area x Linea",
        xaxis_title="Linea", yaxis_title="Area",
    ))
    fig.add_trace(go.Heatmap(
        z=z_matrix, x=lineas, y=areas,
        colorscale="Reds", colorbar_title="% Critico",
        customdata=customdata_matrix,
        hovertemplate=(
            "Area: %{y}<br>Linea: %{x}<br>"
            "Total SKUs: %{customdata[0]}<br>"
            "Criticos: %{customdata[1]}<br>"
            "% Critico: %{z:.1f}<extra></extra>"
        ),
    ))
    # Add text annotations
    for i, area in enumerate(areas):
        for j, linea in enumerate(lineas):
            if z_matrix[i][j] is not None:
                fig.add_annotation(
                    x=linea, y=area, text=text_matrix[i][j],
                    showarrow=False, font=dict(
                        size=11,
                        color="white" if z_matrix[i][j] > 50 else "black",
                    ),
                )
    st.plotly_chart(fig, use_container_width=True)


# Descripcion de cada columna del Detalle por SKU, para la hoja "Manual" del
# Excel descargable. Solo se listan las que esten realmente presentes en el
# export (columnas ausentes en df_export se omiten automaticamente).
_COLUMN_DESCRIPTIONS = {
    "SKU_PRODUCTO": "Codigo unico del producto (SKU).",
    "AREA": "Area de negocio a la que pertenece el SKU.",
    "MIX": "Clasificacion oficial del SKU: MIX, IN & OUT o FUERA MIX.",
    "PROCEDENCIA": "Origen del producto (nacional o importado).",
    "LINEA": "Linea de producto.",
    "SUBLINEA": "Sublinea de producto.",
    "MARCA": "Marca del producto.",
    "SKU_NOM_PRODUCTO": "Nombre/descripcion comercial del producto.",
    "SEMAFORO": "Clasificacion de riesgo de quiebre: CRITICO, ALERTA, OK o SIN VENTA, segun dias de cobertura y si hay reposicion en camino.",
    "ALERTA_OC": "'PONER ORDEN URGENTE' si no hay compra en camino y la cobertura es menor o igual al lead time; 'Compra en Transito' si ya hay una compra en camino que cumple esa condicion.",
    "DIAS_COBERTURA": "Dias estimados de stock disponible al ritmo de venta actual (stock total / venta diaria promedio).",
    "LT": "Lead time de compra (dias) del proveedor para el SKU.",
    "STOCK_TOTAL": "Stock total disponible (CD + tiendas), en unidades.",
    "VENTA_DIARIA_PROM": "Venta promedio diaria (unidades) de los ultimos 90 dias.",
    "VENTA_TOTAL_90D": "Venta total (unidades) de los ultimos 90 dias.",
    "VENTA_PERDIDA_CD": "Venta neta estimada (S/.) perdida por el quiebre vigente en el CD, desde que comenzo el quiebre hasta hoy.",
    "FECHA_QUIEBRE_CD": "Fecha en que comenzo el quiebre vigente en el Centro de Distribucion (stock CD por debajo de la venta diaria promedio).",
    "FECHA_QUIEBRE_EST": "Fecha estimada en la que el SKU se quedaria sin stock, segun el ritmo de venta actual.",
    "DIAS_GAP_QUIEBRE_CD": "Diferencia en dias entre la proxima llegada de mercaderia (PROXIMA_ETA) y la fecha de quiebre en CD.",
    "TIENE_REPO": "Indica si el SKU tiene una reposicion (compra) en camino con cantidad pendiente mayor a cero.",
    "PROXIMA_ETA": "Fecha estimada de la proxima llegada de mercaderia en transito (COMEX).",
    "ESTADO_IMPORTACION": "Estado actual de la orden de compra en curso mas proxima a llegar (ej. Sales Order, Solicitud PI, Transito, Recibido, Cerrado). Vacio si el SKU no tiene PROXIMA_ETA.",
    "QTY_EN_TRANSITO": "Cantidad de unidades ya ordenadas y pendientes de llegar (aun no recepcionadas).",
    "DIAS_GAP": "Diferencia en dias entre la fecha estimada de quiebre de stock y la proxima llegada de mercaderia.",
    "PARETO": "Ranking de importancia por venta (1 = mayor venta) entre los SKUs que explican el 80% de la venta acumulada, considerando solo SKUs con MIX = 'MIX' o 'IN & OUT'. Vacio si el SKU esta Fuera de Mix o fuera de ese 80%.",
    "SUPERVISORES": "Supervisores de tienda responsables de las tiendas donde el SKU tiene stock positivo.",
}


def _build_export_excel(df, sheet_name="Detalle SKU"):
    """Excel de Detalle por SKU: hoja "Manual" con la descripcion de cada
    columna (primera hoja), seguida de los datos con fuente tamano 8, columnas
    ajustadas al ancho del texto que contiene cada una, y Venta Perdida CD en
    formato moneda "S/." (a pedido del negocio).
    """
    df_x = df.copy()
    for col in df_x.columns:
        if pd.api.types.is_bool_dtype(df_x[col]):
            df_x[col] = df_x[col].astype(int)
        elif not pd.api.types.is_numeric_dtype(df_x[col]):
            df_x[col] = df_x[col].fillna("").astype(str)

    font_8 = Font(size=8)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # ── Hoja 1: Manual (nombre de columna + descripcion) ────────────────
        manual_df = pd.DataFrame(
            [(col, _COLUMN_DESCRIPTIONS.get(col, "")) for col in df_x.columns],
            columns=["Columna", "Descripcion"],
        )
        manual_df.to_excel(writer, index=False, sheet_name="Manual")
        ws_manual = writer.sheets["Manual"]
        for col_idx, col_name in enumerate(manual_df.columns, start=1):
            letter = get_column_letter(col_idx)
            header_w = len(str(col_name))
            data_w = int(manual_df[col_name].astype(str).str.len().max()) if len(manual_df) > 0 else 0
            ws_manual.column_dimensions[letter].width = max(header_w, data_w) + 2
            for row_idx in range(1, len(manual_df) + 2):
                ws_manual.cell(row=row_idx, column=col_idx).font = font_8

        # ── Hoja 2: datos ────────────────────────────────────────────────
        df_x.to_excel(writer, index=False, sheet_name=sheet_name)
        ws = writer.sheets[sheet_name]
        n_rows = len(df_x)
        money_fmt = '"S/. "#,##0.00'
        money_col = df_x.columns.get_loc("VENTA_PERDIDA_CD") + 1 if "VENTA_PERDIDA_CD" in df_x.columns else None

        for col_idx, col_name in enumerate(df_x.columns, start=1):
            letter = get_column_letter(col_idx)
            header_w = len(str(col_name))
            data_w = int(df_x[col_name].astype(str).str.len().max()) if n_rows > 0 else 0
            ws.column_dimensions[letter].width = max(header_w, data_w) + 2

            for row_idx in range(1, n_rows + 2):  # fila 1 = cabecera
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.font = font_8
                if col_idx == money_col and row_idx > 1:
                    cell.number_format = money_fmt

    buf.seek(0)
    return buf


# ============================================================================
# MAIN RENDER
# ============================================================================

def render_alertas_quiebre(conn):
    """Main entry point: renders the stock-out alerts dashboard."""
    st.html("<h2 class='sub-header'>Alertas de Quiebre de Stock</h2>")
    st.caption("Prediccion de desabastecimiento basada en stock actual, velocidad de venta (90d) y reposicion COMEX.")

    # Load data (cached in session_state to avoid re-querying on every rerun)
    if st.button("Actualizar Datos", key="btn_refresh_alertas"):
        st.session_state.pop("alertas_data", None)

    if "alertas_data" not in st.session_state:
        (
            stock, ventas, maestra, comex, stock_higiene, tienda_dim, leadtimes,
            stock_cd_hist, estado_imp,
        ) = _load_data(conn)
        df = _build_alert_table(
            stock, ventas, maestra, comex, stock_higiene, tienda_dim,
            leadtimes, stock_cd_hist, estado_imp,
        )
        st.session_state["alertas_data"] = df

    df = st.session_state["alertas_data"].copy()
    df = apply_pm_filter(df)

    if df.empty:
        st.warning("No se encontraron datos de stock.")
        return

    # ── Filters ──────────────────────────────────────────────────────
    st.markdown("### Filtros")
    fc1, fc2, fc3, fc4 = st.columns(4)

    areas = sorted(df["AREA"].dropna().unique()) if "AREA" in df.columns else []
    lineas_all = sorted(df["LINEA"].dropna().unique()) if "LINEA" in df.columns else []
    marcas_all = sorted(df["MARCA"].dropna().unique()) if "MARCA" in df.columns else []

    with fc1:
        sel_area = st.multiselect("Area", areas, key="alq_area")
    with fc2:
        lineas_filt = sorted(df[df["AREA"].isin(sel_area)]["LINEA"].dropna().unique()) if sel_area else lineas_all
        sel_linea = st.multiselect("Linea", lineas_filt, key="alq_linea")
    with fc3:
        _m = df.copy()
        if sel_area:
            _m = _m[_m["AREA"].isin(sel_area)]
        if sel_linea:
            _m = _m[_m["LINEA"].isin(sel_linea)]
        marcas_filt = sorted(_m["MARCA"].dropna().unique()) if "MARCA" in _m.columns else marcas_all
        sel_marca = st.multiselect("Marca", marcas_filt, key="alq_marca")
    with fc4:
        sel_semaforo = st.multiselect(
            "Semaforo", _SEMAFORO_ORDER, default=["CRITICO", "ALERTA"], key="alq_semaforo"
        )

    # Filtro por supervisor (solo si hay datos)
    sel_supervisor = []
    if "SUPERVISORES" in df.columns:
        # Descomponer supervisores (pueden ser múltiples por SKU)
        _all_sups = sorted({
            s.strip()
            for val in df["SUPERVISORES"].dropna()
            for s in str(val).split(",")
            if s.strip() and s.strip() != "Sin Supervisor"
        })
        if _all_sups:
            sel_supervisor = st.multiselect(
                "Supervisor / Zona (tiendas con stock)",
                _all_sups,
                key="alq_supervisor",
                help="Filtra SKUs que tienen stock en tiendas bajo el supervisor seleccionado.",
            )

    # Apply filters
    mask = pd.Series(True, index=df.index)
    if sel_area:
        mask &= df["AREA"].isin(sel_area)
    if sel_linea:
        mask &= df["LINEA"].isin(sel_linea)
    if sel_marca:
        mask &= df["MARCA"].isin(sel_marca)
    if sel_semaforo:
        mask &= df["SEMAFORO"].isin(sel_semaforo)
    if sel_supervisor and "SUPERVISORES" in df.columns:
        # SKU visible si cualquiera de sus supervisores está seleccionado
        mask &= df["SUPERVISORES"].apply(
            lambda v: any(s in str(v) for s in sel_supervisor)
        )
    df_filt = df[mask]

    if df_filt.empty:
        st.warning("No hay datos con los filtros seleccionados.")
        return

    st.markdown("---")

    # ── KPIs ─────────────────────────────────────────────────────────
    _render_kpis(df_filt)

    # ── Charts ───────────────────────────────────────────────────────
    col_left, col_right = st.columns(2)
    with col_left:
        _render_semaforo_chart(df_filt)
    with col_right:
        _render_top_criticos(df_filt)

    st.markdown("---")

    col_tl, col_hm = st.columns(2)
    with col_tl:
        _render_timeline(df_filt)
    with col_hm:
        _render_heatmap(df_filt)

    st.markdown("---")

    # ── Detail Table ─────────────────────────────────────────────────
    st.markdown("### Detalle por SKU")

    # Orden explicito: SKU, jerarquia maestra (AREA -> MIX -> PROCEDENCIA -> resto), metricas
    maestra_order = ["AREA", "MIX", "PROCEDENCIA", "LINEA", "SUBLINEA", "MARCA", "SKU_NOM_PRODUCTO"]
    metric_cols = [
        "SEMAFORO", "ALERTA_OC", "DIAS_COBERTURA", "LT", "STOCK_TOTAL",
        "VENTA_DIARIA_PROM", "VENTA_TOTAL_90D", "VENTA_PERDIDA_CD",
        "FECHA_QUIEBRE_CD", "FECHA_QUIEBRE_EST", "DIAS_GAP_QUIEBRE_CD",
        "TIENE_REPO", "PROXIMA_ETA", "ESTADO_IMPORTACION", "QTY_EN_TRANSITO", "DIAS_GAP", "PARETO",
    ]
    display_cols = ["SKU_PRODUCTO"] + maestra_order + metric_cols
    if "SUPERVISORES" in df_filt.columns:
        display_cols.append("SUPERVISORES")

    display_cols = [c for c in display_cols if c in df_filt.columns]
    df_display = df_filt[display_cols].sort_values("DIAS_COBERTURA").reset_index(drop=True)

    # Format: columnas numericas se REDONDEAN pero se mantienen numericas
    # (no texto) para que Excel/CSV no las marque como "numero como texto".
    # El "sin decimales" en pantalla se logra con column_config.NumberColumn.
    fmt_df = df_display.copy()
    if "DIAS_COBERTURA" in fmt_df.columns:
        fmt_df["DIAS_COBERTURA"] = fmt_df["DIAS_COBERTURA"].where(
            fmt_df["DIAS_COBERTURA"] < 999
        ).round(0)
    if "LT" in fmt_df.columns:
        fmt_df["LT"] = fmt_df["LT"].round(0)
    if "VENTA_DIARIA_PROM" in fmt_df.columns:
        fmt_df["VENTA_DIARIA_PROM"] = fmt_df["VENTA_DIARIA_PROM"].round(0)
    if "VENTA_PERDIDA_CD" in fmt_df.columns:
        fmt_df["VENTA_PERDIDA_CD"] = fmt_df["VENTA_PERDIDA_CD"].round(2)  # moneda: 2 decimales
    if "DIAS_GAP_QUIEBRE_CD" in fmt_df.columns:
        fmt_df["DIAS_GAP_QUIEBRE_CD"] = fmt_df["DIAS_GAP_QUIEBRE_CD"].round(0)
    if "PARETO" in fmt_df.columns:
        fmt_df["PARETO"] = fmt_df["PARETO"].round(0)

    # Fechas: quedan como texto dd/mm/yyyy (formato fecha corta, no numero)
    for col in ["FECHA_QUIEBRE_EST", "FECHA_QUIEBRE_CD", "PROXIMA_ETA"]:
        if col in fmt_df.columns:
            fmt_df[col] = fmt_df[col].apply(
                lambda x: x.strftime("%d/%m/%Y") if pd.notna(x) else "N/A"
            )
    if "ESTADO_IMPORTACION" in fmt_df.columns:
        fmt_df["ESTADO_IMPORTACION"] = fmt_df["ESTADO_IMPORTACION"].fillna("N/A")

    num_fmt_cols = [
        c for c in ["DIAS_COBERTURA", "LT", "VENTA_DIARIA_PROM",
                     "DIAS_GAP_QUIEBRE_CD", "PARETO"]
        if c in fmt_df.columns
    ]
    column_config = {c: st.column_config.NumberColumn(format="%.0f") for c in num_fmt_cols}
    if "VENTA_PERDIDA_CD" in fmt_df.columns:
        column_config["VENTA_PERDIDA_CD"] = st.column_config.NumberColumn(format="S/ %.2f")
    if "ESTADO_IMPORTACION" in fmt_df.columns:
        column_config["ESTADO_IMPORTACION"] = st.column_config.TextColumn("Estado Importacion", width="small")

    st.dataframe(fmt_df, use_container_width=True, height=500, column_config=column_config)

    # ── Export ────────────────────────────────────────────────────────
    # Se exporta fmt_df (formateada) para que el CSV/Excel coincida con lo
    # que se ve en pantalla, no los valores crudos sin formato de df_display.
    # A pedido del negocio, la descarga se ordena por PARETO ascendente
    # (1 = mayor venta primero); los SKU sin ranking (fuera de MIX/IN & OUT
    # o fuera del 80%) quedan al final.
    if "PARETO" in fmt_df.columns:
        df_export = fmt_df.sort_values("PARETO", ascending=True, na_position="last").reset_index(drop=True)
    else:
        df_export = fmt_df
    download_buttons(df_export, "alertas_quiebre", excel_buffer=_build_export_excel(df_export))
