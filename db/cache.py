"""Centralized Snowflake query cache.

Provides cached versions of frequently-used queries so that multiple
modules sharing the same underlying data hit Snowflake only once per
TTL window.  Reduces warehouse costs by ~80-90 %.

TTL tiers
---------
DIARIO (24 h) -- everything that loads overnight: stock, ventas, maestra,
                 historical data.  Refreshed once when the user opens the
                 app in the morning; valid all day.
COMEX  ( 1 h) -- import tracking (cv_compracomex) which gets updated
                 during the workday.

Manual refresh via sidebar button "Refrescar Datos" clears everything.

Usage
-----
    from db.cache import cached_query as cq

    maestra = cq.maestra(conn)
    stock   = cq.stock_onhand(conn)
    # ... etc.

    # Force refresh all caches:
    from db.cache import clear_all
    clear_all()
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import streamlit as st

from db.queries import (
    QUERY_COMEX_FULL,
    QUERY_DASHBOARD_COMEX,
    QUERY_DASHBOARD_VENTAS_MTD,
    QUERY_DT_TIENDA,
    QUERY_INSTOCK_DAILY_CD,
    QUERY_INSTOCK_DAILY_TIENDA,
    QUERY_INSTOCK_HIST_CD,
    QUERY_INSTOCK_HIST_TIENDA,
    QUERY_MAESTRA,
    QUERY_STOCK_CRITICO_METRICS,
    QUERY_STOCK_ONHAND,
    QUERY_STOCK_PROYECCION,
    QUERY_TRANSIT_STOCK_KPI,
    QUERY_COMEX_SIN_FACTURA,
    QUERY_COMEX_PROXIMAS,
    QUERY_PRECIO_PROM_SKU,
    QUERY_VENTAS_AA,
    QUERY_VENTAS_DIARIAS_90D,
    QUERY_VENTAS_DIARIAS_PATRON,
    QUERY_PESOS_DIARIOS,
    QUERY_PESOS_DIARIOS_CANAL,
    QUERY_EVENT_BOOSTS,
    QUERY_EVENT_BOOSTS_SKU,
    QUERY_VENTAS_HISTORICAS,
    QUERY_VENTAS_YTD,
    QUERY_VENTAS_YTD_AA,
    QUERY_VENTAS_MENSUAL_PRECIO,
    QUERY_VENTAS_MES_ANTERIOR,
    QUERY_VENTAS_MTD,
    QUERY_VENTAS_MTD_DIARIA,
    QUERY_VENTAS_SEMANALES,
    QUERY_VENTAS_SEMANAL_TENDENCIA,
    QUERY_STOCK_HIGIENE,
    QUERY_INSTOCK_STORE_DETAIL,
    QUERY_SYNCRO_LEADTIMES,
    QUERY_SYNCRO_CONFIG,
    QUERY_TRANSITO_ENTRE_SUCURSALES,
    QUERY_VENTAS_90D_SUCURSAL,
    QUERY_SUPPLY_PEDIDOS_TRANSFER,
    QUERY_SUPPLY_PICKING,
    QUERY_SUPPLY_STOCK_ACTUAL,
    QUERY_SUPPLY_BULTOS,
    QUERY_SUPPLY_DESPACHOS_FEDEX,
)
from utils.filters import norm_cols

# ---------------------------------------------------------------------------
# TTL constants (seconds)
# ---------------------------------------------------------------------------
TTL_DIARIO = 86_400      # 24 hours -- overnight loads: stock, ventas, maestra, historicos
TTL_COMEX = 3_600        # 1 hour   -- comex (updated during the workday)


# ---------------------------------------------------------------------------
# Internal: run query + norm_cols
# ---------------------------------------------------------------------------
def _run(query: str, conn) -> pd.DataFrame:
    """Execute query and normalize column names."""
    df = pd.read_sql(query, conn)
    return norm_cols(df)


# ---------------------------------------------------------------------------
# DIARIO (24 h) — overnight loads: stock, ventas, maestra, historicos
# All these tables load once at night.  Valid for the entire workday.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def maestra(_conn_id, _conn=None) -> pd.DataFrame:
    """Product master (dv_producto).  Shared by 14+ modules."""
    return _run(QUERY_MAESTRA, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_aa(_conn_id, _conn=None) -> pd.DataFrame:
    """Prior-year sales by month/SKU.  Historical — doesn't change."""
    return _run(QUERY_VENTAS_AA, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_mes_anterior(_conn_id, _conn=None) -> pd.DataFrame:
    """Last-month sales (complete month).  Historical once month ends."""
    return _run(QUERY_VENTAS_MES_ANTERIOR, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_semanales(_conn_id, _conn=None) -> pd.DataFrame:
    """Weekly sales last year (for XYZ variability)."""
    return _run(QUERY_VENTAS_SEMANALES, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_historicas(_conn_id, _conn=None) -> pd.DataFrame:
    """Historical monthly sales (for S&OP control tower trend charts)."""
    return _run(QUERY_VENTAS_HISTORICAS, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_ytd(_conn_id, _conn=None) -> pd.DataFrame:
    """Year-to-date sales (Jan 1 current year → today). For S&OP KPIs."""
    return _run(QUERY_VENTAS_YTD, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_ytd_aa(_conn_id, _conn=None) -> pd.DataFrame:
    """YTD sales same period prior year (for YoY delta)."""
    return _run(QUERY_VENTAS_YTD_AA, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_mensual_precio(_conn_id, _conn=None) -> pd.DataFrame:
    """Monthly sales with avg price (for elasticity analysis)."""
    return _run(QUERY_VENTAS_MENSUAL_PRECIO, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_semanal_tendencia(_conn_id, _conn=None) -> pd.DataFrame:
    """Weekly sales last 24 weeks (for trend detection)."""
    return _run(QUERY_VENTAS_SEMANAL_TENDENCIA, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_diarias_patron(_conn_id, _conn=None) -> pd.DataFrame:
    """Daily sales patterns last year (for forecast disaggregation)."""
    return _run(QUERY_VENTAS_DIARIAS_PATRON, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def pesos_diarios(_conn_id, _conn=None) -> pd.DataFrame:
    """Aggregated DOW×WOM weights for daily disaggregation (last 12 months)."""
    return _run(QUERY_PESOS_DIARIOS, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def pesos_diarios_canal(_conn_id, _conn=None) -> pd.DataFrame:
    """DOW×WOM weights per CANAL for daily disaggregation (last 12 months)."""
    return _run(QUERY_PESOS_DIARIOS_CANAL, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def event_boosts(_conn_id, _conn=None) -> pd.DataFrame:
    """Data-driven event boost factors by SUBLINEA × CANAL × EVENTO (last 2 years)."""
    return _run(QUERY_EVENT_BOOSTS, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def event_boosts_sku(_conn_id, _conn=None) -> pd.DataFrame:
    """Data-driven event boost factors by SKU × CANAL × EVENTO (last 2 years)."""
    return _run(QUERY_EVENT_BOOSTS_SKU, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def stock_onhand(_conn_id, _conn=None) -> pd.DataFrame:
    """On-hand inventory summary (CD vs Tienda)."""
    return _run(QUERY_STOCK_ONHAND, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def stock_proyeccion(_conn_id, _conn=None) -> pd.DataFrame:
    """Stock by canal for projection simulation."""
    return _run(QUERY_STOCK_PROYECCION, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def stock_critico_metrics(_conn_id, _conn=None) -> pd.DataFrame:
    """Stock metrics with MOI and aging buckets."""
    return _run(QUERY_STOCK_CRITICO_METRICS, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def stock_higiene(_conn_id, _conn=None) -> pd.DataFrame:
    """Detailed stock by warehouse (for supply hygiene analysis)."""
    return _run(QUERY_STOCK_HIGIENE, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def instock_store_detail(_conn_id, _conn=None) -> pd.DataFrame:
    """Store-level InStock detail from ft_in_stock (perfil='SI', latest date).

    Returns one row per SKU×Store with: sku_producto, id_sucursal,
    canal_de_distribucion, perfil, stock_unidades, cantidad_prom_90.
    Uses the exact same source as the pre-aggregated InStock dashboard.
    """
    return _run(QUERY_INSTOCK_STORE_DETAIL, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_diarias_90d(_conn_id, _conn=None) -> pd.DataFrame:
    """Daily sales last 90 days (for stock-out alerts)."""
    return _run(QUERY_VENTAS_DIARIAS_90D, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def tienda_dim(_conn_id, _conn=None) -> pd.DataFrame:
    """Store dimension table enriched with lat/lon, cluster, supervisor, m2, etc.
    Join: coo_maestro_sucursal.id_sucursal = dt_tienda.cod_bodega.
    """
    return _run(QUERY_DT_TIENDA, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_mtd(_conn_id, _conn=None) -> pd.DataFrame:
    """Month-to-date sales (current month, for avg price calc)."""
    return _run(QUERY_VENTAS_MTD, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_mtd_diaria(_conn_id, _conn=None) -> pd.DataFrame:
    """Daily sales current month (for REAL+FC day-by-day)."""
    return _run(QUERY_VENTAS_MTD_DIARIA, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def dashboard_ventas_mtd(_conn_id, _conn=None) -> pd.DataFrame:
    """Aggregated MTD sales by canal (dashboard KPI)."""
    return _run(QUERY_DASHBOARD_VENTAS_MTD, _conn)


# ---------------------------------------------------------------------------
# InStock Historico (24 h) — weekly-sampled InStock data for historical panel
# ---------------------------------------------------------------------------

@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def instock_hist_tienda(_conn_id, _conn=None) -> pd.DataFrame:
    """InStock tienda aggregated per SKU per date (Mondays + latest)."""
    return _run(QUERY_INSTOCK_HIST_TIENDA, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def instock_hist_cd(_conn_id, _conn=None) -> pd.DataFrame:
    """InStock CD per SKU per date (Mondays + latest)."""
    return _run(QUERY_INSTOCK_HIST_CD, _conn)


# ---------------------------------------------------------------------------
# InStock Diario (24 h) — daily data last 30 days (PowerBI-style view)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def instock_daily_tienda(_conn_id, _conn=None) -> pd.DataFrame:
    """InStock tienda daily (all days, last 30d). PowerBI-style view."""
    return _run(QUERY_INSTOCK_DAILY_TIENDA, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def instock_daily_cd(_conn_id, _conn=None) -> pd.DataFrame:
    """InStock CD daily (all days, last 30d). PowerBI-style view."""
    return _run(QUERY_INSTOCK_DAILY_CD, _conn)


# ---------------------------------------------------------------------------
# ABC-XYZ-FSN (24 h) — centralized SKU classification ("dato maestro")
# ---------------------------------------------------------------------------
# Internal helpers — pure pandas, no Streamlit dependency.

def _classify_abc_internal(ventas_aa: pd.DataFrame) -> pd.DataFrame:
    """ABC: Pareto on APORTE_AA (margin contribution).
    A = top 80%, B = 80-95%, C = bottom 5%."""
    by_sku = ventas_aa.groupby("SKU_PRODUCTO", as_index=False).agg(
        APORTE_TOTAL=("APORTE_AA", "sum"),
        VN_TOTAL=("NETO_AA", "sum"),
        UNIDADES_TOTAL=("CANTIDAD_AA", "sum"),
    )
    by_sku = by_sku[by_sku["APORTE_TOTAL"] > 0].copy()
    if by_sku.empty:
        return pd.DataFrame(columns=[
            "SKU_PRODUCTO", "CLASE_ABC", "APORTE_TOTAL", "APORTE_PCT",
            "APORTE_CUM_PCT", "VN_TOTAL", "UNIDADES_TOTAL", "RANK_ABC",
        ])
    by_sku = by_sku.sort_values("APORTE_TOTAL", ascending=False).reset_index(drop=True)
    total_aporte = by_sku["APORTE_TOTAL"].sum()
    by_sku["APORTE_PCT"] = by_sku["APORTE_TOTAL"] / total_aporte * 100
    by_sku["APORTE_CUM_PCT"] = by_sku["APORTE_PCT"].cumsum()
    by_sku["CLASE_ABC"] = np.select(
        [by_sku["APORTE_CUM_PCT"] <= 80, by_sku["APORTE_CUM_PCT"] <= 95],
        ["A", "B"], default="C",
    )
    by_sku["RANK_ABC"] = range(1, len(by_sku) + 1)
    return by_sku


def _classify_xyz_internal(ventas_sem: pd.DataFrame) -> pd.DataFrame:
    """XYZ: Coefficient of Variation of weekly demand.
    X = CV < 0.5 (stable), Y = 0.5-1.0 (moderate), Z > 1.0 (erratic)."""
    if ventas_sem.empty:
        return pd.DataFrame(columns=[
            "SKU_PRODUCTO", "CLASE_XYZ", "CV",
            "VENTA_SEMANAL_PROM", "VENTA_SEMANAL_STD",
            "SEMANAS_CON_VENTA", "TOTAL_SEMANAS",
        ])
    pivot = ventas_sem.pivot_table(
        index="SKU_PRODUCTO", columns="SEMANA", values="UNIDADES", aggfunc="sum",
    ).fillna(0)
    means = pivot.mean(axis=1)
    stds = pivot.std(axis=1)
    n_weeks = pivot.shape[1]
    cv_df = pd.DataFrame({
        "SKU_PRODUCTO": pivot.index,
        "CV": (stds / means.replace(0, np.nan)).values,
        "VENTA_SEMANAL_PROM": means.values,
        "VENTA_SEMANAL_STD": stds.values,
        "SEMANAS_CON_VENTA": (pivot > 0).sum(axis=1).values,
        "TOTAL_SEMANAS": n_weeks,
    }).reset_index(drop=True)
    cv_df["CV"] = cv_df["CV"].fillna(999)
    cv_df["CLASE_XYZ"] = np.select(
        [cv_df["CV"] < 0.5, cv_df["CV"] < 1.0],
        ["X", "Y"], default="Z",
    )
    return cv_df


def _classify_fsn(ventas_aa: pd.DataFrame) -> pd.DataFrame:
    """FSN: Pareto on CANTIDAD_AA (unit volume).
    F (Fast) = top 80%, S (Slow) = 80-95%, N (Non-moving) = bottom 5%."""
    by_sku = ventas_aa.groupby("SKU_PRODUCTO", as_index=False)["CANTIDAD_AA"].sum()
    by_sku = by_sku[by_sku["CANTIDAD_AA"] > 0].copy()
    if by_sku.empty:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "CLASE_FSN", "CANTIDAD_AA"])
    by_sku = by_sku.sort_values("CANTIDAD_AA", ascending=False).reset_index(drop=True)
    total = by_sku["CANTIDAD_AA"].sum()
    by_sku["CANT_CUM_PCT"] = by_sku["CANTIDAD_AA"].cumsum() / total * 100
    by_sku["CLASE_FSN"] = np.select(
        [by_sku["CANT_CUM_PCT"] <= 80, by_sku["CANT_CUM_PCT"] <= 95],
        ["F", "S"], default="N",
    )
    return by_sku[["SKU_PRODUCTO", "CLASE_FSN", "CANTIDAD_AA"]]


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def abc_xyz_fsn(_conn_id, _conn=None) -> pd.DataFrame:
    """Centralized ABC-XYZ-FSN classification.  Cached 24 h.

    Returns one row per SKU with columns:
        SKU_PRODUCTO, CLASE_ABC, CLASE_XYZ, CLASE_FSN, CLASE_COMBINADA,
        APORTE_TOTAL, APORTE_PCT, APORTE_CUM_PCT, VN_TOTAL, UNIDADES_TOTAL,
        RANK_ABC, CV, VENTA_SEMANAL_PROM, VENTA_SEMANAL_STD,
        SEMANAS_CON_VENTA, TOTAL_SEMANAS, CANTIDAD_AA
    """
    # Leverage existing cached queries (no extra Snowflake cost)
    df_aa = ventas_aa(_conn_id, _conn=_conn)
    df_sem = ventas_semanales(_conn_id, _conn=_conn)
    df_maestra = maestra(_conn_id, _conn=_conn)

    # Filter MIX_OFICIAL active only (MIX, IN & OUT)
    if "MIX_OFICIAL" in df_maestra.columns:
        mix_skus = set(
            df_maestra.loc[
                df_maestra["MIX_OFICIAL"].astype(str).str.upper().isin(["MIX", "IN & OUT"]),
                "SKU_PRODUCTO",
            ]
        )
        df_aa = df_aa[df_aa["SKU_PRODUCTO"].isin(mix_skus)]
        df_sem = df_sem[df_sem["SKU_PRODUCTO"].isin(mix_skus)]

    # Classify
    abc_df = _classify_abc_internal(df_aa)
    xyz_df = _classify_xyz_internal(df_sem)
    fsn_df = _classify_fsn(df_aa)

    # Merge
    result = abc_df.merge(xyz_df, on="SKU_PRODUCTO", how="outer")
    result = result.merge(
        fsn_df[["SKU_PRODUCTO", "CLASE_FSN"]], on="SKU_PRODUCTO", how="left",
    )
    result["CLASE_ABC"] = result["CLASE_ABC"].fillna("C")
    result["CLASE_XYZ"] = result["CLASE_XYZ"].fillna("Z")
    result["CLASE_FSN"] = result["CLASE_FSN"].fillna("N")
    result["CLASE_COMBINADA"] = result["CLASE_ABC"] + result["CLASE_XYZ"]
    return result


# ---------------------------------------------------------------------------
# COMEX (1 h) — import tracking, updated during the workday
# ---------------------------------------------------------------------------

@st.cache_data(ttl=TTL_COMEX, show_spinner=False)
def transit_stock_kpi(_conn_id, _conn=None) -> pd.DataFrame:
    """Transit KPI: EN_AGUA vs PENDIENTE_ZARPE (qty + CLP)."""
    return _run(QUERY_TRANSIT_STOCK_KPI, _conn)


@st.cache_data(ttl=TTL_COMEX, show_spinner=False)
def comex_full(_conn_id, _conn=None) -> pd.DataFrame:
    """Full COMEX purchase orders table."""
    return _run(QUERY_COMEX_FULL, _conn)


@st.cache_data(ttl=TTL_COMEX, show_spinner=False)
def comex_sin_factura(_conn_id, _conn=None) -> pd.DataFrame:
    """POs in transit without invoice journal (diario de factura)."""
    return _run(QUERY_COMEX_SIN_FACTURA, _conn)


@st.cache_data(ttl=TTL_COMEX, show_spinner=False)
def comex_proximas(_conn_id, _conn=None) -> pd.DataFrame:
    """POs arriving within next 45 days."""
    return _run(QUERY_COMEX_PROXIMAS, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def precio_prom_sku(_conn_id, _conn=None) -> pd.DataFrame:
    """Average selling price per SKU (last 90 days)."""
    return _run(QUERY_PRECIO_PROM_SKU, _conn)


@st.cache_data(ttl=TTL_COMEX, show_spinner=False)
def dashboard_comex(_conn_id, _conn=None) -> pd.DataFrame:
    """Aggregated COMEX summary (dashboard KPI)."""
    return _run(QUERY_DASHBOARD_COMEX, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def leadtimes(_conn_id, _conn=None) -> pd.DataFrame:
    """Lead times per SKU from coo_rel_proveedor_sku + maestra dims. Cached 24h."""
    return _run(QUERY_SYNCRO_LEADTIMES, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def syncro_config(_conn_id, _conn=None) -> pd.DataFrame:
    """SKU × Store profile: MIN_INV_REQUERIDO, MAX_REPO, CD_ORIGEN. Cached 24h."""
    return _run(QUERY_SYNCRO_CONFIG, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def transito_sucursales(_conn_id, _conn=None) -> pd.DataFrame:
    """Transit inventory CD→Store (COO_INVENTARIO_TRANSITO_ENTRE_SUCURSALES). Cached 24h."""
    return _run(QUERY_TRANSITO_ENTRE_SUCURSALES, _conn)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def ventas_90d_sucursal(_conn_id, _conn=None) -> pd.DataFrame:
    """Sales velocity last 90 days per SKU × Store. Cached 24h."""
    return _run(QUERY_VENTAS_90D_SUCURSAL, _conn)


# ---------------------------------------------------------------------------
# SUPPLY OPERATIONS (1 h) — pedidos, picking, stock, bultos, despachos
# ---------------------------------------------------------------------------

@st.cache_data(ttl=TTL_COMEX, show_spinner=False)
def supply_pedidos_transfer(_conn_id, _conn=None) -> pd.DataFrame:
    """Transfer orders last 90 days (CD→tienda + inter-bodega)."""
    return _run(QUERY_SUPPLY_PEDIDOS_TRANSFER, _conn)


@st.cache_data(ttl=TTL_COMEX, show_spinner=False)
def supply_picking(_conn_id, _conn=None) -> pd.DataFrame:
    """Picking progress last 90 days with timing and operator."""
    return _run(QUERY_SUPPLY_PICKING, _conn)


@st.cache_data(ttl=TTL_COMEX, show_spinner=False)
def supply_stock_actual(_conn_id, _conn=None) -> pd.DataFrame:
    """Current stock snapshot by bodega/SKU."""
    return _run(QUERY_SUPPLY_STOCK_ACTUAL, _conn)


@st.cache_data(ttl=TTL_COMEX, show_spinner=False)
def supply_bultos(_conn_id, _conn=None) -> pd.DataFrame:
    """Bultos (packages) dispatched last 90 days."""
    return _run(QUERY_SUPPLY_BULTOS, _conn)


@st.cache_data(ttl=TTL_COMEX, show_spinner=False)
def supply_despachos_fedex(_conn_id, _conn=None) -> pd.DataFrame:
    """FedEx dispatch tracking last 90 days."""
    return _run(QUERY_SUPPLY_DESPACHOS_FEDEX, _conn)


# ---------------------------------------------------------------------------
# UNIFIED TRANSIT — combines COO transito (yesterday backward) + today's
# pedidos de transferencia from Syncro (reposicion automatica)
# ---------------------------------------------------------------------------

def _parse_transito_coo(df_raw: pd.DataFrame) -> pd.DataFrame:
    """Parse COO transit table into standard (SKU, DEST, QTY) format.

    Auto-detects columns from coo_inventario_transito_entre_sucursales.
    Mirrors redistribucion._parse_transito() logic for reuse without
    circular dependencies.
    """
    if df_raw is None or df_raw.empty:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO", "QTY_TRANSITO"])

    df = df_raw.copy()
    df.columns = [c.upper().strip() for c in df.columns]
    cols = df.columns.tolist()

    # --- SKU column ---
    sku_col = None
    for candidate in ["ID_MATERIAL", "SKU_PRODUCTO", "SKU", "MATERIAL", "COD_PRODUCTO"]:
        if candidate in cols:
            sku_col = candidate
            break
    if sku_col is None:
        for c in cols:
            if "MATERIAL" in c or "SKU" in c or "PRODUCTO" in c:
                sku_col = c
                break

    # --- Destination column ---
    dest_col = None
    for candidate in ["ID_SUCURSAL_DESTINO", "SUCURSAL_DESTINO", "ID_SUCURSAL",
                       "DESTINO", "COD_DESTINO", "BODEGA_DESTINO"]:
        if candidate in cols:
            dest_col = candidate
            break
    if dest_col is None:
        for c in cols:
            if "DESTINO" in c:
                dest_col = c
                break

    # --- Quantity column ---
    qty_col = None
    for candidate in ["CANTIDAD", "QTY", "UNIDADES", "CANTIDAD_TRANSITO",
                       "STOCK_TRANSITO", "QTY_TRANSITO"]:
        if candidate in cols:
            qty_col = candidate
            break
    if qty_col is None:
        for c in cols:
            if "CANT" in c or "QTY" in c or "UNID" in c:
                qty_col = c
                break

    if sku_col is None or dest_col is None:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO", "QTY_TRANSITO"])

    result = pd.DataFrame({
        "SKU_PRODUCTO": df[sku_col].astype(str).str.strip(),
        "ID_SUCURSAL_DESTINO": df[dest_col].astype(str).str.strip(),
    })
    if qty_col is not None:
        result["QTY_TRANSITO"] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0)
    else:
        result["QTY_TRANSITO"] = 1  # at least flag that transit exists

    result = result.groupby(["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO"], as_index=False).agg(
        QTY_TRANSITO=("QTY_TRANSITO", "sum"),
    )
    return result


def _parse_pedidos_as_transit(df_pedidos: pd.DataFrame) -> pd.DataFrame:
    """Convert today's transfer orders into standard transit format.

    Only takes FECHA_CREACION >= today to avoid overlap with COO table
    (which covers yesterday backwards).  Filters:
    - ESTADO_PEDIDO NOT IN ('Recibido', 'Cancelado')
    - CANAL_DESTINO == 'TIENDA'
    - CANTIDAD_PENDIENTE > 0

    Returns DataFrame with columns: SKU_PRODUCTO, ID_SUCURSAL_DESTINO, QTY_TRANSITO
    """
    empty = pd.DataFrame(columns=["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO", "QTY_TRANSITO"])
    if df_pedidos is None or df_pedidos.empty:
        return empty

    df = df_pedidos.copy()
    df.columns = [c.upper().strip() for c in df.columns]

    # Required columns check
    needed = {"SKU_PRODUCTO", "COD_BODEGA_DESTINO", "CANTIDAD_PENDIENTE"}
    if not needed.issubset(df.columns):
        return empty

    # Filter: only today's orders (no overlap with COO)
    if "FECHA_CREACION" in df.columns:
        df["FECHA_CREACION"] = pd.to_datetime(df["FECHA_CREACION"], errors="coerce")
        today = pd.Timestamp.now().normalize()
        df = df[df["FECHA_CREACION"] >= today]
    else:
        return empty  # Can't dedup without date → skip

    if df.empty:
        return empty

    # Filter: active orders only
    if "ESTADO_PEDIDO" in df.columns:
        df = df[~df["ESTADO_PEDIDO"].astype(str).str.upper().isin(["RECIBIDO", "CANCELADO"])]

    # Filter: only CD→Tienda orders
    if "CANAL_DESTINO" in df.columns:
        df = df[df["CANAL_DESTINO"].astype(str).str.upper() == "TIENDA"]

    # Filter: positive pending qty
    df["CANTIDAD_PENDIENTE"] = pd.to_numeric(df["CANTIDAD_PENDIENTE"], errors="coerce").fillna(0)
    df = df[df["CANTIDAD_PENDIENTE"] > 0]

    if df.empty:
        return empty

    result = pd.DataFrame({
        "SKU_PRODUCTO": df["SKU_PRODUCTO"].astype(str).str.strip(),
        "ID_SUCURSAL_DESTINO": df["COD_BODEGA_DESTINO"].astype(str).str.strip(),
        "QTY_TRANSITO": df["CANTIDAD_PENDIENTE"].values,
    })

    # Aggregate duplicates
    result = result.groupby(["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO"], as_index=False).agg(
        QTY_TRANSITO=("QTY_TRANSITO", "sum"),
    )
    return result


@st.cache_data(ttl=TTL_COMEX, show_spinner=False)
def unified_transit(_conn_id, _conn=None) -> pd.DataFrame:
    """Unified transit: COO (yesterday backward) + today's pedidos.

    Returns DataFrame with columns: SKU_PRODUCTO, ID_SUCURSAL_DESTINO, QTY_TRANSITO.
    Aggregated by (SKU, DEST) with summed quantities.
    Cached 1 h (TTL_COMEX) since pedidos are updated during the workday.
    """
    # Source 1: COO transit (yesterday backward)
    df_coo = transito_sucursales(_conn_id, _conn=_conn)
    parsed_coo = _parse_transito_coo(df_coo)

    # Source 2: Today's transfer orders from Syncro
    df_pedidos = supply_pedidos_transfer(_conn_id, _conn=_conn)
    parsed_pedidos = _parse_pedidos_as_transit(df_pedidos)

    # Combine and aggregate
    combined = pd.concat([parsed_coo, parsed_pedidos], ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO", "QTY_TRANSITO"])

    result = combined.groupby(["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO"], as_index=False).agg(
        QTY_TRANSITO=("QTY_TRANSITO", "sum"),
    )
    return result


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

# Registry of all cached functions for batch clearing
_ALL_CACHED = [
    maestra, ventas_aa, ventas_mes_anterior, ventas_semanales,
    ventas_historicas, ventas_ytd, ventas_ytd_aa,
    ventas_mensual_precio, ventas_semanal_tendencia,
    ventas_diarias_patron, pesos_diarios, pesos_diarios_canal,
    event_boosts, event_boosts_sku,
    stock_onhand, stock_proyeccion, stock_critico_metrics, stock_higiene, instock_store_detail,
    ventas_diarias_90d, tienda_dim,
    ventas_mtd, ventas_mtd_diaria, dashboard_ventas_mtd,
    instock_hist_tienda, instock_hist_cd,
    instock_daily_tienda, instock_daily_cd,
    abc_xyz_fsn,
    comex_full, dashboard_comex,
    leadtimes,
    syncro_config, transito_sucursales, ventas_90d_sucursal,
    supply_pedidos_transfer, supply_picking, supply_stock_actual,
    supply_bultos, supply_despachos_fedex,
    unified_transit,
]


def clear_all():
    """Clear every cached query.  Call from sidebar 'Refrescar datos' button."""
    for fn in _ALL_CACHED:
        fn.clear()
    now = datetime.now()
    st.session_state["_cache_cleared_at"] = now
    st.session_state["_cache_cleared_date"] = now.date()
    # Reset ABC-XYZ-FSN so it re-computes on next page load
    st.session_state.pop("abc_xyz_fsn_ready", None)


def auto_refresh_if_new_day():
    """Auto-clear cache on first access of a new calendar day.

    Called once per session from app.py.  Ensures users always start
    the day with fresh Snowflake data without manual click.
    """
    today = datetime.now().date()
    last_date = st.session_state.get("_cache_cleared_date")
    if last_date is None or last_date < today:
        clear_all()


def last_refresh_label() -> str:
    """Return a human-readable label of when cache was last cleared."""
    ts = st.session_state.get("_cache_cleared_at")
    if ts is None:
        return ""
    return f"Ultima actualizacion: {ts.strftime('%H:%M')}"


# ---------------------------------------------------------------------------
# Convenience namespace
# ---------------------------------------------------------------------------

class cached_query:
    """Namespace so callers can write ``cq.maestra(conn)`` etc."""

    @staticmethod
    def _cid(conn):
        """Hashable connection identifier (memory id)."""
        return id(conn)

    # -- DIARIO (24h) --
    @staticmethod
    def maestra(conn):
        return maestra(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_aa(conn):
        return ventas_aa(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_mes_anterior(conn):
        return ventas_mes_anterior(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_semanales(conn):
        return ventas_semanales(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_historicas(conn):
        return ventas_historicas(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_ytd(conn):
        return ventas_ytd(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_ytd_aa(conn):
        return ventas_ytd_aa(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_mensual_precio(conn):
        return ventas_mensual_precio(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_semanal_tendencia(conn):
        return ventas_semanal_tendencia(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_diarias_patron(conn):
        return ventas_diarias_patron(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def pesos_diarios(conn):
        return pesos_diarios(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def pesos_diarios_canal(conn):
        return pesos_diarios_canal(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def event_boosts(conn):
        return event_boosts(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def event_boosts_sku(conn):
        return event_boosts_sku(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def stock_onhand(conn):
        return stock_onhand(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def stock_proyeccion(conn):
        return stock_proyeccion(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def stock_critico_metrics(conn):
        return stock_critico_metrics(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def stock_higiene(conn):
        return stock_higiene(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def instock_store_detail(conn):
        return instock_store_detail(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_diarias_90d(conn):
        return ventas_diarias_90d(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def tienda_dim(conn):
        return tienda_dim(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_mtd(conn):
        return ventas_mtd(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_mtd_diaria(conn):
        return ventas_mtd_diaria(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def dashboard_ventas_mtd(conn):
        return dashboard_ventas_mtd(cached_query._cid(conn), _conn=conn)

    # -- InStock Historico (24h) --
    @staticmethod
    def instock_hist_tienda(conn):
        return instock_hist_tienda(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def instock_hist_cd(conn):
        return instock_hist_cd(cached_query._cid(conn), _conn=conn)

    # -- InStock Diario (24h) --
    @staticmethod
    def instock_daily_tienda(conn):
        return instock_daily_tienda(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def instock_daily_cd(conn):
        return instock_daily_cd(cached_query._cid(conn), _conn=conn)

    # -- ABC-XYZ-FSN (24h) --
    @staticmethod
    def abc_xyz_fsn(conn):
        return abc_xyz_fsn(cached_query._cid(conn), _conn=conn)

    # -- COMEX (1h) --
    @staticmethod
    def transit_stock_kpi(conn):
        return transit_stock_kpi(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def comex_full(conn):
        return comex_full(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def comex_sin_factura(conn):
        return comex_sin_factura(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def comex_proximas(conn):
        return comex_proximas(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def precio_prom_sku(conn):
        return precio_prom_sku(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def dashboard_comex(conn):
        return dashboard_comex(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def leadtimes(conn):
        return leadtimes(cached_query._cid(conn), _conn=conn)

    # -- Redistribucion (24h) --
    @staticmethod
    def syncro_config(conn):
        return syncro_config(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def transito_sucursales(conn):
        return transito_sucursales(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def ventas_90d_sucursal(conn):
        return ventas_90d_sucursal(cached_query._cid(conn), _conn=conn)

    # -- Unified Transit (1h) --
    @staticmethod
    def unified_transit(conn):
        return unified_transit(cached_query._cid(conn), _conn=conn)

    # -- Supply Operations (1h) --
    @staticmethod
    def supply_pedidos_transfer(conn):
        return supply_pedidos_transfer(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def supply_picking(conn):
        return supply_picking(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def supply_stock_actual(conn):
        return supply_stock_actual(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def supply_bultos(conn):
        return supply_bultos(cached_query._cid(conn), _conn=conn)

    @staticmethod
    def supply_despachos_fedex(conn):
        return supply_despachos_fedex(cached_query._cid(conn), _conn=conn)
