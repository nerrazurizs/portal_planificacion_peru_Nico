"""Venta Perdida (Lost Sales) — acumulada por rango de fechas.

Calcula VP diaria para cada dia del rango seleccionado.
Demanda promedio se obtiene de VCM (2 meses completos, sin diciembre).
La VP se computa en Snowflake via CTEs para eficiencia:
  - Tiendas: demanda a nivel SKU (total tienda) distribuida entre tiendas
    con perfil definido. Evita mismatch de join cod_ccosto vs cod_bodega.
  - CD: nivel SKU x canal (MAYOR/ETAIL) x dia

Resultados: VP acumulada, evolucion diaria, instock %, detalle por dimension.
"""

import calendar
from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from config import COLORS, apply_pm_filter, TC_USD_DEFAULT
from db.queries import (
    QUERY_MAESTRA, QUERY_ETA_PENDIENTE_POR_SKU,
    _VCM, _INSTOCK, _INSTOCK_CD, _PROD,
)
from db.cache import run_sql as _run_sql
from utils.export import download_buttons
from utils.filters import limpiar_lista, norm_cols
from utils.ui_animations import lottie_spinner
from utils.ui_components import simple_kpi_card


# ── Cached helpers ────────────────────────────────────────────────────────────


@st.cache_data(ttl=1800, show_spinner=False)
def _max_fecha_instock(_conn):
    """Latest available date in ht_in_stock (before today)."""
    df = pd.read_sql(
        "SELECT MAX(fecha) AS max_fecha "
        "FROM db_supply.hst.ht_in_stock "
        "WHERE fecha < CURRENT_DATE()",
        _conn,
    )
    val = df.iloc[0, 0]
    if val is None:
        from datetime import datetime
        return (datetime.now() - timedelta(days=1)).date()
    return pd.to_datetime(val).date()


@st.cache_data(ttl=1800, show_spinner=False)
def _min_fecha_instock(_conn):
    """Earliest available date in ht_in_stock."""
    df = pd.read_sql(
        "SELECT MIN(fecha) AS min_fecha FROM db_supply.hst.ht_in_stock",
        _conn,
    )
    val = df.iloc[0, 0]
    if val is None:
        return date(2025, 1, 1)
    return pd.to_datetime(val).date()


@st.cache_data(ttl=3600, show_spinner=False)
def _load_distinct(_conn, col: str) -> list[str]:
    """Load distinct non-null values for a product dimension column."""
    df = pd.read_sql(
        f"SELECT DISTINCT p.{col} FROM {_PROD} p "
        f"WHERE p.{col} IS NOT NULL AND TRIM(CAST(p.{col} AS VARCHAR)) != '' "
        f"ORDER BY p.{col}",
        _conn,
    )
    return df.iloc[:, 0].dropna().astype(str).str.strip().tolist()


@st.cache_data(ttl=3600, show_spinner=False)
def _load_first_sale_dates(_conn):
    """Load first sale date per SKU from all VCM history."""
    sql = f"""
    SELECT sku_producto, MIN(fecha) AS first_sale_date
    FROM {_VCM}
    WHERE cantidad > 0
    GROUP BY 1
    """
    df = pd.read_sql(sql, _conn)
    df = norm_cols(df)
    if "FIRST_SALE_DATE" in df.columns:
        df["FIRST_SALE_DATE"] = pd.to_datetime(df["FIRST_SALE_DATE"])
    return df


@st.cache_data(ttl=1800, show_spinner=False)
def _load_cd_recovery_events(_conn, lookback_start, range_end):
    """Detect CD stock recovery events (0 → >0 transitions).

    Looks back before the analysis range to catch recoveries whose
    grace period extends into it.

    Returns DataFrame with columns: SKU_PRODUCTO, RECOVERY_DATE
    """
    sql = f"""
    SELECT fecha AS recovery_date, sku_producto
    FROM (
        SELECT
            fecha,
            sku_producto,
            stock_unidades AS stock_cd,
            LAG(stock_unidades, 1, 0)
                OVER (PARTITION BY sku_producto ORDER BY fecha)
                AS prev_stock
        FROM {_INSTOCK_CD}
        WHERE fecha >= %s AND fecha <= %s
    ) sub
    WHERE stock_cd > 0 AND prev_stock = 0
    """
    df = _run_sql(_conn, sql, [str(lookback_start), str(range_end)])
    df = norm_cols(df)
    if "RECOVERY_DATE" in df.columns:
        df["RECOVERY_DATE"] = pd.to_datetime(df["RECOVERY_DATE"])
    return df


@st.cache_data(ttl=1800, show_spinner=False)
def _load_eta_pendiente(_conn):
    """Load next pending ETA per SKU from ft_compras (in-transit POs)."""
    df = pd.read_sql(QUERY_ETA_PENDIENTE_POR_SKU, _conn)
    df = norm_cols(df)
    # Normalize SKU values — ft_compras.codigo_producto_oc may have
    # trailing spaces or different formatting vs ft_vcm/vw_in_stock.
    if "SKU_PRODUCTO" in df.columns:
        df["SKU_PRODUCTO"] = df["SKU_PRODUCTO"].astype(str).str.strip()
    for c in ["QTY_PENDIENTE", "N_POS"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    if "PROXIMA_ETA" in df.columns:
        df["PROXIMA_ETA"] = pd.to_datetime(df["PROXIMA_ETA"])
    return df


# ── Demand window logic ──────────────────────────────────────────────────────


def _get_demand_window(stock_date):
    """Return (fecha_inicio, fecha_fin) for the 2-month VCM demand window.

    Always excludes December. Picks the 2 most recent complete months
    before the stock month, skipping December.
    """
    m, y = stock_date.month, stock_date.year
    months = []
    cur_m, cur_y = m - 1, y
    if cur_m == 0:
        cur_m, cur_y = 12, y - 1
    while len(months) < 2:
        if cur_m == 12:
            cur_m = 11
            continue
        months.append((cur_y, cur_m))
        cur_m -= 1
        if cur_m == 0:
            cur_m, cur_y = 12, cur_y - 1
    oldest, newest = months[1], months[0]
    f_ini = date(oldest[0], oldest[1], 1)
    f_fin = date(newest[0], newest[1], calendar.monthrange(newest[0], newest[1])[1])
    return f_ini, f_fin


def _month_label(y, m):
    MES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
           "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    return f"{MES[m]} {y}"


def _split_into_months(start, end):
    """Split a date range into list of (month_start, month_end) tuples."""
    result = []
    d = start.replace(day=1)
    while d <= end:
        y, m = d.year, d.month
        ms = max(start, date(y, m, 1))
        me = min(end, date(y, m, calendar.monthrange(y, m)[1]))
        result.append((ms, me))
        if m == 12:
            d = date(y + 1, 1, 1)
        else:
            d = date(y, m + 1, 1)
    return result


# ── SQL builders (CTE-based VP computation in Snowflake) ─────────────────────


def _build_mix_in(mix_values):
    """Build SQL IN expression for mix_oficial. Returns '' if no filter."""
    if not mix_values:
        return ""
    escaped = ", ".join(
        "'{}'".format(v.replace("'", "''")) for v in mix_values
    )
    return f"p.mix_oficial IN ({escaped})"


def _build_tienda_ctes(dias_ventana, mix_values=None, perfil_only=True,
                       filter_cd_instock="stock_gt_0",
                       always_include_cd_ctes=False):
    """Build the shared WITH ... CTE block for tienda VP queries.

    Returns the SQL string starting with ``WITH ... n_perfil AS (...)``
    ready to be followed by a final SELECT.

    Includes:
    - Hardcoded exclusion of closed stores (Bellavista, Chiclayo 2).
    - ``cd_instock`` CTE (optional): CD daily stock for filtering.

    Args:
        filter_cd_instock: CD filter mode:
            "none"        – no CD filter
            "stock_gt_0"  – only VP where CD stock > 0 (default)
            "instock_cd"  – only VP where InStock CD = 1
                            (stock >= cantidad_prom_90_cia)
        always_include_cd_ctes: When True, demand_all and cd_instock CTEs
            are always generated regardless of filter_cd_instock. Used by
            the detail query to support TIPO_VP classification.

    Params (positional %s, 6 or 10 total):
        demand_start, demand_end        (demand CTE — per store)
      [if filter_cd_instock != "none" OR always_include_cd_ctes]:
        demand_start, demand_end        (demand_all CTE)
        stock_start, stock_end          (stock_daily CTE)
        stock_start, stock_end          (dates CTE — independent)
      [if filter_cd_instock != "none" OR always_include_cd_ctes]:
        stock_start, stock_end          (cd_instock CTE)
    """
    mix_clause = _build_mix_in(mix_values)
    has_mix = bool(mix_clause)

    mix_join_v = (
        f"INNER JOIN eligible_skus e ON v.sku_producto = e.sku_producto"
        if has_mix else ""
    )
    mix_join_a = (
        f"INNER JOIN eligible_skus e ON a.sku_producto = e.sku_producto"
        if has_mix else ""
    )
    mix_join_p = (
        f"INNER JOIN eligible_skus e ON p.sku_producto = e.sku_producto"
        if has_mix else ""
    )
    # Perfil filter: only for n_perfil (store count for demand allocation),
    # NOT for stock_daily.  Stock must reflect REAL inventory regardless
    # of whether the store currently has perfil for the SKU.
    n_perfil_where = "WHERE perfil = 'SI'" if perfil_only else ""

    eligible_cte = ""
    if has_mix:
        eligible_cte = f"""eligible_skus AS (
        SELECT DISTINCT p.sku_producto
        FROM {_PROD} p
        WHERE {mix_clause}
    ),
    """

    # ── valid_stores CTE: tiendas fisicas (tipoalmacen=9) minus cerradas ──
    # Double %% to escape %-formatting used by Snowflake connector
    valid_stores_cte = f"""valid_stores AS (
        SELECT cod_almacen,
               LPAD(TRIM(CAST(cod_ccosto AS VARCHAR)), 4, '0') AS cod_ccosto,
               nom_almacen
        FROM db_dimensiones.dim.dt_almacen
        WHERE cod_tipoalmacen = '9'
          AND cod_ccosto IS NOT NULL
          AND cod_ccosto NOT IN ('0229', '1290')  -- Benavides (ambas tiendas cerradas)
          AND UPPER(COALESCE(nom_almacen, ''))
              NOT LIKE '%%BELLAVISTA%%'
          AND UPPER(COALESCE(nom_almacen, ''))
              NOT LIKE '%%CHICLAYO 2%%'
          AND UPPER(COALESCE(nom_almacen, ''))
              NOT LIKE '%%CHICLAYO2%%'
          AND UPPER(COALESCE(nom_almacen, ''))
              NOT LIKE '%%SAN MIGUEL 2%%'
          AND UPPER(COALESCE(nom_almacen, ''))
              NOT LIKE '%%SANMIGUEL2%%'
          AND UPPER(COALESCE(nom_almacen, ''))
              NOT LIKE '%%OUTLET%%'
          AND UPPER(COALESCE(nom_almacen, ''))
              NOT LIKE '%%CAJAMARCA%%'
          AND UPPER(COALESCE(nom_almacen, ''))
              NOT LIKE '%%TRUJILLO 2%%'
          AND UPPER(COALESCE(nom_almacen, ''))
              NOT LIKE '%%TRUJILLO2%%'
    ),
    """

    return f"""
    WITH
    {eligible_cte}
    {valid_stores_cte}
    prod_price AS (
        SELECT p.sku_producto, MAX(p.ultimo_costo) AS ultimo_costo
        FROM {_PROD} p
        {mix_join_p}
        WHERE p.sku_producto NOT LIKE '%%-PV'
        GROUP BY 1
    ),
    demand AS (
        SELECT
            v.sku_producto,
            CAST(vs.cod_ccosto AS VARCHAR) AS id_sucursal,
            MAX(vs.nom_almacen) AS descripcion_sucursal,
            SUM(v.cantidad) / NULLIF({dias_ventana}::FLOAT, 0)
                AS demand_per_store,
            CASE WHEN SUM(v.cantidad) > 0
                 THEN SUM(v.neto) / SUM(v.cantidad)
                 ELSE 0 END AS avg_price
        FROM {_VCM} v
        INNER JOIN valid_stores vs
            ON LPAD(TRIM(CAST(v.cod_ccosto AS VARCHAR)), 4, '0') = vs.cod_ccosto
        {mix_join_v}
        WHERE v.fecha >= %s AND v.fecha <= %s
          AND v.cantidad > 0
          AND v.sku_producto NOT LIKE '%%-PV'
        GROUP BY 1, 2
    ),
    demand_total AS (
        SELECT sku_producto,
               SUM(demand_per_store) AS total_daily_demand
        FROM demand
        GROUP BY 1
    ),""" + (f"""
    demand_all AS (
        SELECT
            v.sku_producto,
            SUM(v.cantidad) / NULLIF({dias_ventana}::FLOAT, 0)
                AS daily_demand_all
        FROM {_VCM} v
        {mix_join_v}
        WHERE v.fecha >= %s AND v.fecha <= %s
          AND v.cantidad > 0
          AND v.sku_producto NOT LIKE '%%-PV'
        GROUP BY 1
    ),""" if filter_cd_instock != "none" or always_include_cd_ctes else "") + f"""
    stock_daily AS (
        SELECT
            a.fecha,
            a.sku_producto,
            CAST(vs.cod_ccosto AS VARCHAR) AS id_sucursal,
            MAX(vs.nom_almacen) AS descripcion_sucursal,
            SUM(a.stock_unidades) AS stock_unidades,
            MAX(a.perfil) AS perfil
        FROM {_INSTOCK} a
        INNER JOIN valid_stores vs
            ON a.cod_bodega = vs.cod_almacen
        {mix_join_a}
        WHERE a.fecha >= %s AND a.fecha <= %s
          AND a.sku_producto NOT LIKE '%%-PV'
        GROUP BY 1, 2, 3
    ),
    n_perfil AS (
        SELECT fecha, sku_producto, COUNT(DISTINCT id_sucursal) AS n_stores
        FROM stock_daily
        {n_perfil_where}
        GROUP BY 1, 2
    ),
    dates AS (
        SELECT DISTINCT fecha
        FROM {_INSTOCK}
        WHERE fecha >= %s AND fecha <= %s
    )""" + (f""",
    cd_instock AS (
        SELECT cd.fecha, cd.sku_producto, cd.stock_unidades AS stock_cd,
               COALESCE(da.daily_demand_all, 0) AS daily_demand_all,
               CASE WHEN cd.stock_unidades > 0
                    AND cd.stock_unidades >= COALESCE(da.daily_demand_all, 0)
                    THEN 1 ELSE 0 END AS instock_cd
        FROM {_INSTOCK_CD} cd
        LEFT JOIN demand_all da ON cd.sku_producto = da.sku_producto
        WHERE cd.fecha >= %s AND cd.fecha <= %s
    )""" if filter_cd_instock != "none" or always_include_cd_ctes else "")


def _sql_vp_tienda(dias_ventana, mix_values=None, perfil_only=True):
    """VP per SKU x day (aggregated across stores).

    Always runs WITHOUT cd_instock filter (4 params).
    CD filter is applied post-hoc in Python via detail data.
    """
    cte = _build_tienda_ctes(dias_ventana, mix_values, perfil_only,
                             filter_cd_instock="none")
    # Exclude store×day combos without active perfil to avoid phantom VP
    pf_where = ("\n    WHERE COALESCE(s.perfil, 'NO') = 'SI'"
                if perfil_only else "")
    return cte + f"""
    SELECT
        dt.fecha,
        d.sku_producto,
        SUM(GREATEST(0,
            d.demand_per_store - COALESCE(s.stock_unidades, 0)
        )) AS vp_unidades,
        SUM(GREATEST(0,
            d.demand_per_store - COALESCE(s.stock_unidades, 0)
        ) * COALESCE(NULLIF(d.avg_price, 0), pp.ultimo_costo, 0))
            AS vp_pesos,
        COUNT(*) AS n_tiendas_total,
        SUM(CASE
            WHEN COALESCE(s.stock_unidades, 0) >= d.demand_per_store
            THEN 1 ELSE 0 END) AS n_tiendas_instock,
        SUM(CASE
            WHEN COALESCE(s.stock_unidades, 0) < d.demand_per_store
            THEN 1 ELSE 0 END) AS n_tiendas_oos
    FROM dates dt
    CROSS JOIN demand d
    LEFT JOIN stock_daily s
        ON dt.fecha = s.fecha
        AND d.sku_producto = s.sku_producto
        AND d.id_sucursal = s.id_sucursal
    LEFT JOIN prod_price pp
        ON d.sku_producto = pp.sku_producto
    {pf_where}
    GROUP BY 1, 2
    """


def _sql_vp_tienda_by_store(dias_ventana, mix_values=None, perfil_only=True):
    """VP per store x day (aggregated across SKUs).

    Always runs WITHOUT cd_instock filter (4 params).
    """
    cte = _build_tienda_ctes(dias_ventana, mix_values, perfil_only,
                             filter_cd_instock="none")
    # Exclude store×day combos without active perfil to avoid phantom VP
    pf_where = ("\n    WHERE COALESCE(s.perfil, 'NO') = 'SI'"
                if perfil_only else "")
    return cte + f"""
    SELECT
        dt.fecha,
        d.id_sucursal,
        MAX(COALESCE(s.descripcion_sucursal,
                     d.descripcion_sucursal)) AS descripcion_sucursal,
        SUM(GREATEST(0,
            d.demand_per_store - COALESCE(s.stock_unidades, 0)
        )) AS vp_unidades,
        SUM(GREATEST(0,
            d.demand_per_store - COALESCE(s.stock_unidades, 0)
        ) * COALESCE(NULLIF(d.avg_price, 0), pp.ultimo_costo, 0))
            AS vp_pesos,
        COUNT(DISTINCT d.sku_producto) AS n_skus,
        SUM(COALESCE(s.stock_unidades, 0)) AS stock_total,
        SUM(d.demand_per_store) AS demanda_tienda,
        SUM(CASE
            WHEN COALESCE(s.stock_unidades, 0) >= d.demand_per_store
            THEN 1 ELSE 0 END) AS skus_instock,
        COUNT(*) AS skus_total
    FROM dates dt
    CROSS JOIN demand d
    LEFT JOIN stock_daily s
        ON dt.fecha = s.fecha
        AND d.sku_producto = s.sku_producto
        AND d.id_sucursal = s.id_sucursal
    LEFT JOIN prod_price pp
        ON d.sku_producto = pp.sku_producto
    {pf_where}
    GROUP BY 1, 2
    """


def _sql_vp_cd(dias_ventana, mix_values=None):
    """Build SQL that computes CD VP per SKU x canal x day.

    VP is pro-rated between MAYOR and ETAIL by demand share.
    Params (positional %s): demand_start, demand_end, stock_start, stock_end
    """
    mix_clause = _build_mix_in(mix_values)
    has_mix = bool(mix_clause)

    mix_join_v = (
        f"INNER JOIN eligible_skus e ON v.sku_producto = e.sku_producto"
        if has_mix else ""
    )
    mix_join_a = (
        f"INNER JOIN eligible_skus e ON a.sku_producto = e.sku_producto"
        if has_mix else ""
    )
    mix_join_p = (
        f"INNER JOIN eligible_skus e ON p.sku_producto = e.sku_producto"
        if has_mix else ""
    )

    eligible_cte = ""
    if has_mix:
        eligible_cte = f"""eligible_skus AS (
        SELECT DISTINCT p.sku_producto
        FROM {_PROD} p
        WHERE {mix_clause}
    ),
    """

    return f"""
    WITH
    {eligible_cte}
    prod_price AS (
        SELECT p.sku_producto, MAX(p.ultimo_costo) AS ultimo_costo
        FROM {_PROD} p
        {mix_join_p}
        WHERE p.sku_producto NOT LIKE '%%-PV'
        GROUP BY 1
    ),
    demand AS (
        SELECT
            v.sku_producto,
            b.canal_de_distribucion AS canal,
            SUM(v.cantidad) / NULLIF({dias_ventana}::FLOAT, 0)
                AS avg_daily_demand,
            CASE WHEN SUM(v.cantidad) > 0
                 THEN SUM(v.neto) / SUM(v.cantidad)
                 ELSE 0 END AS avg_price
        FROM {_VCM} v
        LEFT JOIN db_syncros.public.coo_maestro_sucursal b
            ON v.cod_ccosto = b.id_sucursal
        {mix_join_v}
        WHERE v.fecha >= %s AND v.fecha <= %s
          AND v.cantidad > 0
          AND v.sku_producto NOT LIKE '%%-PV'
          AND b.canal_de_distribucion IN ('MAYOR', 'MAYORISTA', 'ETAIL')
        GROUP BY 1, 2
    ),
    demand_total AS (
        SELECT sku_producto, SUM(avg_daily_demand) AS total_demand
        FROM demand
        GROUP BY 1
    )
    SELECT
        s.fecha,
        d.sku_producto,
        d.canal,
        GREATEST(0, dt.total_demand - s.stock_cd)
            * (d.avg_daily_demand / NULLIF(dt.total_demand, 0))
            AS vp_unidades,
        GREATEST(0, dt.total_demand - s.stock_cd)
            * (d.avg_daily_demand / NULLIF(dt.total_demand, 0))
            * COALESCE(NULLIF(d.avg_price, 0), pp.ultimo_costo, 0)
            AS vp_pesos,
        s.stock_cd,
        dt.total_demand AS demand_total_cd,
        d.avg_daily_demand AS demand_canal,
        CASE WHEN s.stock_cd >= dt.total_demand THEN 1 ELSE 0 END
            AS instock_cd
    FROM (
        SELECT a.fecha, a.sku_producto, a.stock_unidades AS stock_cd
        FROM {_INSTOCK_CD} a
        {mix_join_a}
        WHERE a.fecha >= %s AND a.fecha <= %s
          AND a.sku_producto NOT LIKE '%%-PV'
    ) s
    INNER JOIN demand d ON s.sku_producto = d.sku_producto
    INNER JOIN demand_total dt ON s.sku_producto = dt.sku_producto
    LEFT JOIN prod_price pp ON s.sku_producto = pp.sku_producto
    WHERE s.stock_cd < dt.total_demand
    """


def _sql_vp_tienda_detail(dias_ventana, mix_values=None, perfil_only=True,
                          filter_cd_instock="none"):
    """VP detail per SKU x Store x Day — full calculation audit.

    Always includes CD columns (stock_cd, daily_demand_all, instock_cd)
    via LEFT JOIN to cd_instock CTE for TIPO_VP classification.

    Params: always 10 (demand, demand_all, stock_daily, dates, cd_instock).
    """
    cte = _build_tienda_ctes(dias_ventana, mix_values, perfil_only,
                             filter_cd_instock=filter_cd_instock,
                             always_include_cd_ctes=True)
    cd_join = """
    LEFT JOIN cd_instock ci
        ON dts.fecha = ci.fecha AND d.sku_producto = ci.sku_producto"""
    cd_col = "ci.stock_cd, ci.daily_demand_all, ci.instock_cd"
    # Exclude store×day combos without active perfil to avoid phantom VP
    pf_where = ("\n    WHERE COALESCE(s.perfil, 'NO') = 'SI'"
                if perfil_only else "")
    return cte + f"""
    SELECT
        dts.fecha,
        d.sku_producto,
        d.id_sucursal,
        COALESCE(s.descripcion_sucursal,
                 d.descripcion_sucursal) AS descripcion_sucursal,
        COALESCE(s.stock_unidades, 0) AS stock_unidades,
        {cd_col},
        dtot.total_daily_demand AS demanda_total_dia,
        np.n_stores AS n_tiendas_perfil,
        d.demand_per_store AS demanda_por_tienda,
        CASE
            WHEN COALESCE(s.stock_unidades, 0) >= d.demand_per_store
            THEN 1 ELSE 0 END AS instock,
        GREATEST(0,
            d.demand_per_store - COALESCE(s.stock_unidades, 0))
            AS vp_unidades,
        GREATEST(0,
            d.demand_per_store - COALESCE(s.stock_unidades, 0))
            * COALESCE(NULLIF(d.avg_price, 0), pp.ultimo_costo, 0)
            AS vp_pesos,
        d.avg_price AS precio_vcm,
        pp.ultimo_costo,
        COALESCE(NULLIF(d.avg_price, 0), pp.ultimo_costo, 0)
            AS precio_usado
    FROM dates dts
    CROSS JOIN demand d
    LEFT JOIN stock_daily s
        ON dts.fecha = s.fecha
        AND d.sku_producto = s.sku_producto
        AND d.id_sucursal = s.id_sucursal
    LEFT JOIN demand_total dtot
        ON d.sku_producto = dtot.sku_producto
    LEFT JOIN n_perfil np
        ON dts.fecha = np.fecha AND d.sku_producto = np.sku_producto
    LEFT JOIN prod_price pp
        ON d.sku_producto = pp.sku_producto
    {cd_join}
    {pf_where}
    """


# ── VP computation (per-month, handles rolling demand windows) ────────────────


def _compute_vp_range(conn, stock_start, stock_end,
                      mix_values=None, perfil_only=True,
                      filter_cd_instock="stock_gt_0",
                      apply_grace=False, grace_lead_days=3,
                      progress_bar=None):
    """Compute VP for a date range, running queries per month.

    Each month uses its own demand window (rolling, excl December).

    Args:
        filter_cd_instock: CD filter mode:
            "none"        – no CD filter
            "stock_gt_0"  – VP where CD stock > 0 (default)
            "instock_cd"  – VP where InStock CD = 1 (stock >= 1 day demand VCM all channels)
        apply_grace: Apply lead-time grace period after CD recovery.
        grace_lead_days: Days to wait after CD recovery before measuring VP.

    Returns (df_tienda, df_cd, df_by_store, df_detail, df_detail_full).
    df_detail_full is the UNFILTERED detail with TIPO_VP classification.
    """
    month_ranges = _split_into_months(stock_start, stock_end)
    all_tienda = []
    all_cd = []
    all_by_store = []
    all_detail = []

    for i, (ms, me) in enumerate(month_ranges):
        f_ini, f_fin = _get_demand_window(date(ms.year, ms.month, 15))
        dias_ventana = (f_fin - f_ini).days + 1

        # Base params (aggregate queries): demand + stock_daily + dates = 6
        prm_base = [str(f_ini), str(f_fin), str(ms), str(me),
                    str(ms), str(me)]  # dates CTE (independent)

        # Detail params: ALWAYS 10 (demand, demand_all, stock_daily,
        #                           dates, cd_instock)
        prm_detail = [
            str(f_ini), str(f_fin),   # demand CTE
            str(f_ini), str(f_fin),   # demand_all CTE (always present)
            str(ms), str(me),         # stock_daily CTE
            str(ms), str(me),         # dates CTE (independent)
            str(ms), str(me),         # cd_instock CTE (always present)
        ]

        # CD query: demand + stock = 4
        prm_cd = [str(f_ini), str(f_fin), str(ms), str(me)]

        # ── Tienda VP (SKU level) — always unfiltered ──
        sql_t = _sql_vp_tienda(dias_ventana, mix_values, perfil_only)
        df_t = norm_cols(_run_sql(conn, sql_t, prm_base))
        if not df_t.empty:
            all_tienda.append(df_t)

        # ── Diagnostic: check valid_stores + demand/stock overlap ──
        if i == 0:
            try:
                _diag_sql = f"""
                WITH vs AS (
                    SELECT cod_almacen,
                           LPAD(TRIM(CAST(cod_ccosto AS VARCHAR)), 4, '0') AS cod_ccosto,
                           nom_almacen
                    FROM db_dimensiones.dim.dt_almacen
                    WHERE cod_tipoalmacen = '9'
                      AND cod_ccosto IS NOT NULL
                )
                SELECT
                    'demand (tipo=9)' AS src,
                    COUNT(*) AS n_rows,
                    COUNT(DISTINCT vs.cod_ccosto) AS n_stores,
                    LISTAGG(DISTINCT vs.cod_ccosto, ', ')
                        WITHIN GROUP (ORDER BY vs.cod_ccosto)
                        AS sample_ids
                FROM {_VCM} v
                INNER JOIN vs
                    ON LPAD(TRIM(CAST(v.cod_ccosto AS VARCHAR)), 4, '0') = vs.cod_ccosto
                WHERE v.fecha >= %s AND v.fecha <= %s
                  AND v.cantidad > 0
                UNION ALL
                SELECT
                    'stock (tipo=9)' AS src,
                    COUNT(*) AS n_rows,
                    COUNT(DISTINCT vs.cod_ccosto) AS n_stores,
                    LISTAGG(DISTINCT vs.cod_ccosto, ', ')
                        WITHIN GROUP (ORDER BY vs.cod_ccosto)
                        AS sample_ids
                FROM {_INSTOCK} a
                INNER JOIN vs ON a.cod_bodega = vs.cod_almacen
                WHERE a.fecha >= %s AND a.fecha <= %s
                """
                _diag_prm = [str(f_ini), str(f_fin), str(ms), str(me)]
                _df_diag = _run_sql(conn, _diag_sql, _diag_prm)
                st.session_state["vp_store_diag"] = _df_diag
            except Exception:
                pass

            # ── CROSS JOIN diagnostic: how many rows, how many with VP ──
            try:
                cte_diag = _build_tienda_ctes(
                    dias_ventana, mix_values, perfil_only,
                    filter_cd_instock="none",
                )
                _xj_sql = cte_diag + f"""
                SELECT
                    COUNT(*) AS total_rows,
                    COUNT(s.stock_unidades) AS rows_with_stock,
                    COUNT(*) - COUNT(s.stock_unidades) AS rows_no_stock,
                    SUM(CASE WHEN GREATEST(0,
                        d.demand_per_store - COALESCE(s.stock_unidades, 0)
                    ) > 0 THEN 1 ELSE 0 END) AS rows_with_vp,
                    SUM(d.demand_per_store) AS total_demand,
                    SUM(COALESCE(s.stock_unidades, 0)) AS total_stock,
                    SUM(GREATEST(0,
                        d.demand_per_store - COALESCE(s.stock_unidades, 0)
                    )) AS total_vp_units,
                    (SELECT COUNT(*) FROM dates) AS n_dates,
                    (SELECT COUNT(*) FROM demand) AS n_demand,
                    (SELECT COUNT(*) FROM stock_daily) AS n_stock_daily
                FROM dates dt
                CROSS JOIN demand d
                LEFT JOIN stock_daily s
                    ON dt.fecha = s.fecha
                    AND d.sku_producto = s.sku_producto
                    AND d.id_sucursal = s.id_sucursal
                """
                _xj_df = _run_sql(conn, _xj_sql, prm_base)
                st.session_state["vp_crossjoin_diag"] = _xj_df
            except Exception as _xe:
                st.session_state["vp_crossjoin_diag"] = f"ERROR: {_xe}"

            # ── Debug: aggregate result stats ──
            st.session_state["_debug_df_t"] = {
                "rows": len(df_t),
                "vp_pesos": float(df_t["VP_PESOS"].sum())
                if not df_t.empty and "VP_PESOS" in df_t.columns
                else 0,
                "vp_unidades": float(df_t["VP_UNIDADES"].sum())
                if not df_t.empty and "VP_UNIDADES" in df_t.columns
                else 0,
                "columns": list(df_t.columns) if not df_t.empty else [],
            }

        # ── Tienda VP (store level) — always unfiltered ──
        sql_ts = _sql_vp_tienda_by_store(
            dias_ventana, mix_values, perfil_only,
        )
        df_ts = norm_cols(_run_sql(conn, sql_ts, prm_base))
        if not df_ts.empty:
            all_by_store.append(df_ts)

        # ── Tienda VP detail — always includes CD columns for TIPO_VP ──
        sql_td = _sql_vp_tienda_detail(
            dias_ventana, mix_values, perfil_only,
            filter_cd_instock=filter_cd_instock,
        )
        df_td = norm_cols(_run_sql(conn, sql_td, prm_detail))
        if not df_td.empty:
            all_detail.append(df_td)

        # ── CD VP ──
        sql_c = _sql_vp_cd(dias_ventana, mix_values)
        df_c = norm_cols(_run_sql(conn, sql_c, prm_cd))
        if not df_c.empty:
            all_cd.append(df_c)

        if progress_bar:
            progress_bar.progress((i + 1) / len(month_ranges))

    df_tienda = (
        pd.concat(all_tienda, ignore_index=True) if all_tienda
        else pd.DataFrame()
    )
    df_cd = (
        pd.concat(all_cd, ignore_index=True) if all_cd
        else pd.DataFrame()
    )
    df_by_store = (
        pd.concat(all_by_store, ignore_index=True) if all_by_store
        else pd.DataFrame()
    )
    df_detail = (
        pd.concat(all_detail, ignore_index=True) if all_detail
        else pd.DataFrame()
    )

    # ── TIPO_VP classification (before any filtering) ──
    if not df_detail.empty:
        if "VP_UNIDADES" in df_detail.columns:
            df_detail["VP_UNIDADES"] = pd.to_numeric(
                df_detail["VP_UNIDADES"], errors="coerce"
            ).fillna(0)
        if "STOCK_CD" in df_detail.columns:
            df_detail["STOCK_CD"] = pd.to_numeric(
                df_detail["STOCK_CD"], errors="coerce"
            ).fillna(0)
            df_detail["TIPO_VP"] = np.where(
                df_detail["VP_UNIDADES"] <= 0,
                "SIN_VP",
                np.where(
                    df_detail["STOCK_CD"] <= 0,
                    "QUIEBRE_PRODUCTO",
                    "REPOSICION",
                ),
            )
        else:
            # Fallback: no CD data → all VP is "unknown"
            df_detail["TIPO_VP"] = np.where(
                df_detail["VP_UNIDADES"] > 0,
                "QUIEBRE_PRODUCTO",
                "SIN_VP",
            )

    # Save unfiltered detail for Insights tab
    df_detail_full = df_detail.copy() if not df_detail.empty else pd.DataFrame()

    # ── CD InStock filter (post-hoc on detail, then re-aggregate) ──
    if filter_cd_instock != "none" and not df_detail.empty:
        _pre_filter_n = len(df_detail)
        if "STOCK_CD" in df_detail.columns:
            if filter_cd_instock == "instock_cd":
                # Keep rows where InStock CD = 1 OR where we have no CD data (NULL)
                mask = df_detail["INSTOCK_CD"].isna() | (df_detail["INSTOCK_CD"] == 1)
            else:  # stock_gt_0
                # Keep rows where CD stock > 0 OR where we have no CD data (NULL)
                mask = df_detail["STOCK_CD"].isna() | (df_detail["STOCK_CD"] > 0)
            df_detail = df_detail[mask].copy()
        # Only re-aggregate if filter kept some rows
        if not df_detail.empty:
            df_tienda, df_by_store = _reaggregate_from_detail(df_detail)

    # ── Grace period: zero VP during lead-time after CD recovery ──
    if apply_grace and not df_detail.empty:
        lookback = stock_start - timedelta(days=max(grace_lead_days, 30))
        df_recovery = _load_cd_recovery_events(conn, lookback, stock_end)
        df_detail = _apply_grace_period(
            df_detail, df_recovery, grace_lead_days,
        )
        # Re-aggregate tienda and by_store from grace-adjusted detail
        if not df_detail.empty:
            df_tienda, df_by_store = _reaggregate_from_detail(df_detail)

    return df_tienda, df_cd, df_by_store, df_detail, df_detail_full


def _enrich_with_maestra(df, conn):
    """Merge product dimension data (AREA, LINEA, MARCA, etc.)."""
    if df.empty:
        return df
    df_maestra = pd.read_sql(QUERY_MAESTRA, conn)
    df_maestra = norm_cols(df_maestra)
    cols_dim = [
        "SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA",
        "SUBLINEA", "MARCA", "MODELO", "PROVEEDOR",
        "ULTIMO_COSTO", "MIX_OFICIAL",
    ]
    cols_dim = [c for c in cols_dim if c in df_maestra.columns]
    df_maestra = df_maestra[cols_dim].drop_duplicates(subset=["SKU_PRODUCTO"])
    df = df.merge(df_maestra, on="SKU_PRODUCTO", how="left")
    return df


def _enrich_with_new_flag(df, df_first_sale, demand_start):
    """Add PRODUCTO_STATUS column: SIN VENTAS / NUEVO / ESTABLECIDO.

    - SIN VENTAS: SKU never sold in all VCM history
    - NUEVO: first sale within or after the demand window start
    - ESTABLECIDO: has sales before the demand window
    """
    if df.empty:
        return df
    if "FIRST_SALE_DATE" in df.columns:
        df = df.drop(columns=["FIRST_SALE_DATE"], errors="ignore")
    df = df.merge(df_first_sale, on="SKU_PRODUCTO", how="left")
    ds = pd.Timestamp(demand_start)
    df["PRODUCTO_STATUS"] = np.where(
        df["FIRST_SALE_DATE"].isna(),
        "SIN VENTAS",
        np.where(df["FIRST_SALE_DATE"] >= ds, "NUEVO", "ESTABLECIDO"),
    )
    return df


def _apply_grace_period(df_detail, df_recovery, default_lead_days=3):
    """Zero out VP for rows within the lead-time grace period.

    After CD recovers stock (0 → >0), stores need time to receive
    the replenishment. During this grace period, VP is "expected"
    and not actionable.
    """
    if df_detail.empty:
        for c in ("RECOVERY_DATE", "DIAS_DESDE_RECOVERY", "LEAD_DAYS",
                   "EN_GRACIA", "VP_UNIDADES_ORIG", "VP_PESOS_ORIG"):
            df_detail[c] = np.nan if "FLOAT" not in c else 0
        df_detail["EN_GRACIA"] = False
        return df_detail

    df = df_detail.copy()
    df["FECHA"] = pd.to_datetime(df["FECHA"])

    if df_recovery.empty:
        df["RECOVERY_DATE"] = pd.NaT
        df["DIAS_DESDE_RECOVERY"] = np.nan
        df["LEAD_DAYS"] = default_lead_days
        df["EN_GRACIA"] = False
        df["VP_UNIDADES_ORIG"] = df["VP_UNIDADES"]
        df["VP_PESOS_ORIG"] = df["VP_PESOS"]
        return df

    # Ensure datetime + sort for merge_asof (requires sorted keys)
    df["FECHA"] = pd.to_datetime(df["FECHA"])
    df = df.sort_values("FECHA").reset_index(drop=True)

    rec = df_recovery[["SKU_PRODUCTO", "RECOVERY_DATE"]].drop_duplicates()
    rec["RECOVERY_DATE"] = pd.to_datetime(rec["RECOVERY_DATE"])
    rec = rec.sort_values("RECOVERY_DATE").reset_index(drop=True)

    merged = pd.merge_asof(
        df, rec,
        by="SKU_PRODUCTO",
        left_on="FECHA",
        right_on="RECOVERY_DATE",
        direction="backward",
    )

    merged["DIAS_DESDE_RECOVERY"] = (
        (merged["FECHA"] - merged["RECOVERY_DATE"]).dt.days
    )
    merged["LEAD_DAYS"] = default_lead_days
    merged["EN_GRACIA"] = (
        merged["DIAS_DESDE_RECOVERY"].notna()
        & (merged["DIAS_DESDE_RECOVERY"] >= 0)
        & (merged["DIAS_DESDE_RECOVERY"] < default_lead_days)
    )

    # Preserve original VP, then zero out grace period rows
    merged["VP_UNIDADES_ORIG"] = merged["VP_UNIDADES"]
    merged["VP_PESOS_ORIG"] = merged["VP_PESOS"]
    merged.loc[merged["EN_GRACIA"], "VP_UNIDADES"] = 0
    merged.loc[merged["EN_GRACIA"], "VP_PESOS"] = 0

    return merged


def _reaggregate_from_detail(df_detail):
    """Rebuild df_tienda and df_by_store from grace-adjusted detail."""
    if df_detail.empty:
        return pd.DataFrame(), pd.DataFrame()

    # df_tienda: SKU x day
    df_tienda = (
        df_detail.groupby(["FECHA", "SKU_PRODUCTO"])
        .agg(
            VP_UNIDADES=("VP_UNIDADES", "sum"),
            VP_PESOS=("VP_PESOS", "sum"),
            N_TIENDAS_TOTAL=("ID_SUCURSAL", "nunique"),
            N_TIENDAS_INSTOCK=("INSTOCK", "sum"),
            N_TIENDAS_OOS=("INSTOCK", lambda x: (x == 0).sum()),
        )
        .reset_index()
    )

    # ── TIPO_VP breakdown columns ──
    if "TIPO_VP" in df_detail.columns:
        vp_rows = df_detail[df_detail["VP_UNIDADES"] > 0]
        if not vp_rows.empty:
            tipo_agg = (
                vp_rows.groupby(["FECHA", "SKU_PRODUCTO", "TIPO_VP"])
                .agg(
                    _VP_UND=("VP_UNIDADES", "sum"),
                    _VP_PES=("VP_PESOS", "sum"),
                )
                .reset_index()
            )
            for tipo, sfx in [("QUIEBRE_PRODUCTO", "_QP"),
                               ("REPOSICION", "_REP")]:
                sub = (
                    tipo_agg[tipo_agg["TIPO_VP"] == tipo]
                    .rename(columns={
                        "_VP_UND": f"VP_UNIDADES{sfx}",
                        "_VP_PES": f"VP_PESOS{sfx}",
                    })
                    .drop(columns=["TIPO_VP"])
                )
                df_tienda = df_tienda.merge(
                    sub, on=["FECHA", "SKU_PRODUCTO"], how="left",
                )
        for c in ["VP_UNIDADES_QP", "VP_PESOS_QP",
                   "VP_UNIDADES_REP", "VP_PESOS_REP"]:
            if c in df_tienda.columns:
                df_tienda[c] = df_tienda[c].fillna(0)
            else:
                df_tienda[c] = 0.0

    # df_by_store: store x day
    agg_dict = {
        "DESCRIPCION_SUCURSAL": ("DESCRIPCION_SUCURSAL", "first"),
        "VP_UNIDADES": ("VP_UNIDADES", "sum"),
        "VP_PESOS": ("VP_PESOS", "sum"),
        "N_SKUS": ("SKU_PRODUCTO", "nunique"),
        "STOCK_TOTAL": ("STOCK_UNIDADES", "sum"),
        "SKUS_INSTOCK": ("INSTOCK", "sum"),
        "SKUS_TOTAL": ("INSTOCK", "count"),
    }
    if "DEMANDA_POR_TIENDA" in df_detail.columns:
        agg_dict["DEMANDA_TIENDA"] = ("DEMANDA_POR_TIENDA", "sum")

    df_by_store = (
        df_detail.groupby(["FECHA", "ID_SUCURSAL"])
        .agg(**agg_dict)
        .reset_index()
    )

    return df_tienda, df_by_store


def _run_diagnostics(conn, stock_start, stock_end, demand_start, demand_end):
    """Run quick diagnostic queries to identify VP data issues."""
    results = {}

    # 1. valid_stores: tiendas fisicas (tipoalmacen=9)
    try:
        df_stores = pd.read_sql(
            "SELECT cod_tipoalmacen AS tipo, COUNT(*) AS n, "
            "       COUNT(DISTINCT cod_ccosto) AS n_ccosto "
            "FROM db_dimensiones.dim.dt_almacen "
            "GROUP BY 1 ORDER BY 2 DESC",
            conn,
        )
        results["tipos_almacen"] = df_stores
    except Exception:
        results["tipos_almacen"] = None

    # 2. Stock rows by tipo almacen (for the date range)
    try:
        df_stock = _run_sql(
            conn,
            f"SELECT COALESCE(CAST(al.cod_tipoalmacen AS VARCHAR), "
            f"       'SIN_MATCH') AS tipo, "
            f"       COUNT(*) AS filas, "
            f"       COUNT(DISTINCT a.sku_producto) AS skus "
            f"FROM {_INSTOCK} a "
            f"LEFT JOIN db_dimensiones.dim.dt_almacen al "
            f"    ON a.cod_bodega = al.cod_almacen "
            f"WHERE a.fecha >= %s AND a.fecha <= %s "
            f"GROUP BY 1 ORDER BY 2 DESC",
            [str(stock_start), str(stock_end)],
        )
        results["stock_por_tipo"] = df_stock
    except Exception:
        results["stock_por_tipo"] = None

    # 3. Perfil distribution (tiendas tipo=9)
    try:
        df_perfil = _run_sql(
            conn,
            f"SELECT COALESCE(a.perfil, 'NULL') AS perfil, COUNT(*) AS filas "
            f"FROM {_INSTOCK} a "
            f"INNER JOIN db_dimensiones.dim.dt_almacen al "
            f"    ON a.cod_bodega = al.cod_almacen "
            f"WHERE a.fecha >= %s AND a.fecha <= %s "
            f"  AND al.cod_tipoalmacen = '9' "
            f"GROUP BY 1",
            [str(stock_start), str(stock_end)],
        )
        results["perfil"] = df_perfil
    except Exception:
        results["perfil"] = None

    # 4. VCM demand rows for tiendas tipo=9
    try:
        df_demand = _run_sql(
            conn,
            f"SELECT COUNT(*) AS filas, "
            f"       COUNT(DISTINCT v.sku_producto) AS skus, "
            f"       SUM(v.cantidad) AS total_qty "
            f"FROM {_VCM} v "
            f"INNER JOIN db_dimensiones.dim.dt_almacen al "
            f"    ON LPAD(TRIM(CAST(v.cod_ccosto AS VARCHAR)), 4, '0') "
            f"     = LPAD(TRIM(CAST(al.cod_ccosto AS VARCHAR)), 4, '0') "
            f"WHERE v.fecha >= %s AND v.fecha <= %s "
            f"  AND v.cantidad > 0 "
            f"  AND al.cod_tipoalmacen = '9'",
            [str(demand_start), str(demand_end)],
        )
        results["demanda_tienda"] = df_demand
    except Exception:
        results["demanda_tienda"] = None

    return results


def _apply_filters(df, f_sku, f_area, f_linea, f_sublinea,
                   f_marca, f_proveedor, f_status=None):
    """Apply user-selected dimension filters."""
    if df.empty:
        return df
    if f_sku:
        df = df[df["SKU_PRODUCTO"].isin(f_sku)]
    if f_area and "AREA" in df.columns:
        df = df[df["AREA"].isin(f_area)]
    if f_linea and "LINEA" in df.columns:
        df = df[df["LINEA"].isin(f_linea)]
    if f_sublinea and "SUBLINEA" in df.columns:
        df = df[df["SUBLINEA"].isin(f_sublinea)]
    if f_marca and "MARCA" in df.columns:
        df = df[df["MARCA"].isin(f_marca)]
    if f_proveedor and "PROVEEDOR" in df.columns:
        df = df[df["PROVEEDOR"].isin(f_proveedor)]
    if f_status and "PRODUCTO_STATUS" in df.columns:
        df = df[df["PRODUCTO_STATUS"].isin(f_status)]
    return df.copy()


# ── Visual helpers (Chile-style) ──────────────────────────────────────────────


def _hdr(title: str) -> str:
    """Section header with bottom border (Chile dashboard style)."""
    return (
        f'<div style="margin-top:1.5rem;padding-bottom:0.5rem;'
        f'border-bottom:2px solid #e2e8f0;margin-bottom:0.25rem;">'
        f'<span style="font-size:0.95rem;font-weight:600;'
        f'color:#2A2927;letter-spacing:0.1px;">{title}</span>'
        f'</div>'
    )


def _vp_sym() -> str:
    """Símbolo de la moneda de despliegue seleccionada en el toggle."""
    return "$" if st.session_state.get("vp_moneda") == "USD ($)" else "S/"


def _vp_div() -> float:
    """Divisor para convertir montos en soles a la moneda de despliegue."""
    if st.session_state.get("vp_moneda") == "USD ($)":
        tc = st.session_state.get("tc_usd_pen", TC_USD_DEFAULT) or TC_USD_DEFAULT
        try:
            tc = float(tc)
        except (TypeError, ValueError):
            tc = TC_USD_DEFAULT
        return tc if tc > 0 else TC_USD_DEFAULT
    return 1.0


# Columnas monetarias (en soles) que deben convertirse cuando se elige USD.
_VP_MONEY_COLS = ("VP_PESOS", "PRECIO_VCM", "ULTIMO_COSTO", "PRECIO_USADO")


def _vp_convert(df):
    """Devuelve una copia con las columnas monetarias en la moneda elegida.

    La sesión siempre guarda los datos en soles; aquí se convierten a USD
    (dividiendo por el TC USD/PEN) solo para el despliegue. Si la moneda es
    soles, retorna el df sin tocar.
    """
    div = _vp_div()
    if df is None or getattr(df, "empty", True) or div == 1.0:
        return df
    out = df.copy()
    for c in _VP_MONEY_COLS:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0) / div
    return out


def _fmt_currency(val: float) -> str:
    """Format as compact currency: 1.2B, 345M, 12K, 1,234 con el símbolo activo.

    El valor ya viene en la moneda de despliegue (convertido en _vp_convert),
    aquí solo se le antepone el símbolo (S/ o $).
    """
    sym = _vp_sym()
    abs_v = abs(val)
    sign = "-" if val < 0 else ""
    if abs_v >= 1_000_000_000:
        return f"{sign}{sym} {abs_v / 1_000_000_000:,.1f}B"
    if abs_v >= 1_000_000:
        return f"{sign}{sym} {abs_v / 1_000_000:,.1f}M"
    if abs_v >= 10_000:
        return f"{sign}{sym} {abs_v / 1_000:,.0f}K"
    if abs_v >= 1_000:
        return f"{sign}{sym} {abs_v:,.0f}"
    return f"{sign}{sym} {abs_v:,.1f}"


def _instock_dot(pct: float) -> str:
    """Return colored dot HTML for InStock percentage."""
    if pct >= 93:
        color = COLORS["status_on_track"]  # green
    elif pct >= 85:
        color = COLORS["status_at_risk"]  # orange
    else:
        color = COLORS["status_critical"]  # red
    return (
        f'<span style="display:inline-block;width:10px;height:10px;'
        f'border-radius:50%;background:{color};"></span>'
    )


# ── Charts ────────────────────────────────────────────────────────────────────


def _chart_evolucion(df_tienda, df_cd, df_detail_full=None):
    """Dual-axis chart: VP diaria (stacked bars) + InStock % (line).

    When df_detail_full has TIPO_VP, VP Tiendas is split into
    Quiebre Producto (dark red) and Reposicion (orange).
    """
    # ── Try to build Quiebre / Repo split from detail_full ──
    has_tipo = (
        df_detail_full is not None
        and not df_detail_full.empty
        and "TIPO_VP" in df_detail_full.columns
    )
    day_tipo = None
    if has_tipo:
        vp_rows = df_detail_full[df_detail_full["VP_UNIDADES"] > 0]
        if not vp_rows.empty:
            day_tipo = (
                vp_rows.groupby(["FECHA", "TIPO_VP"])["VP_PESOS"]
                .sum()
                .unstack(fill_value=0)
                .reset_index()
            )
            day_tipo["FECHA"] = pd.to_datetime(day_tipo["FECHA"])

    # ── InStock % from df_tienda ──
    is_data = None
    if not df_tienda.empty:
        is_data = df_tienda.groupby("FECHA").agg(
            TIENDAS_IS=("N_TIENDAS_INSTOCK", "sum"),
            TIENDAS_TOTAL=("N_TIENDAS_TOTAL", "sum"),
        ).reset_index()
        is_data["FECHA"] = pd.to_datetime(is_data["FECHA"])

    # ── CD VP ──
    day_cd = None
    if not df_cd.empty:
        day_cd = df_cd.groupby("FECHA").agg(
            VP_CD=("VP_PESOS", "sum"),
        ).reset_index()
        day_cd["FECHA"] = pd.to_datetime(day_cd["FECHA"])

    # Merge all into one daily DF
    if day_tipo is not None:
        daily = day_tipo.copy()
    elif not df_tienda.empty:
        dt = df_tienda.groupby("FECHA").agg(
            VP_TIENDA=("VP_PESOS", "sum"),
        ).reset_index()
        dt["FECHA"] = pd.to_datetime(dt["FECHA"])
        daily = dt
    else:
        daily = pd.DataFrame(columns=["FECHA"])

    if daily.empty and day_cd is not None:
        daily = day_cd.copy()
    elif day_cd is not None and not daily.empty:
        daily = daily.merge(day_cd, on="FECHA", how="outer").fillna(0)

    if is_data is not None and not daily.empty:
        daily = daily.merge(is_data, on="FECHA", how="left").fillna(0)

    if daily.empty:
        return None

    daily = daily.sort_values("FECHA")

    # InStock %
    if "TIENDAS_TOTAL" in daily.columns:
        daily["INSTOCK_PCT"] = np.where(
            daily["TIENDAS_TOTAL"] > 0,
            daily["TIENDAS_IS"] / daily["TIENDAS_TOTAL"] * 100,
            100,
        )
    else:
        daily["INSTOCK_PCT"] = 100

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    # ── Bars: Quiebre + Repo (or single VP Tiendas) ──
    if day_tipo is not None:
        if "QUIEBRE_PRODUCTO" in daily.columns:
            fig.add_trace(
                go.Bar(
                    x=daily["FECHA"],
                    y=daily["QUIEBRE_PRODUCTO"],
                    name="Quiebre Producto",
                    marker_color="#8B0000",
                ),
                secondary_y=False,
            )
        if "REPOSICION" in daily.columns:
            fig.add_trace(
                go.Bar(
                    x=daily["FECHA"],
                    y=daily["REPOSICION"],
                    name="Reposicion",
                    marker_color=COLORS.get("status_at_risk", "#F5A623"),
                ),
                secondary_y=False,
            )
    elif "VP_TIENDA" in daily.columns:
        fig.add_trace(
            go.Bar(
                x=daily["FECHA"], y=daily["VP_TIENDA"],
                name="VP Tiendas",
                marker_color=COLORS.get("primary", "#065E8B"),
            ),
            secondary_y=False,
        )

    if "VP_CD" in daily.columns:
        fig.add_trace(
            go.Bar(
                x=daily["FECHA"], y=daily["VP_CD"],
                name="VP CD",
                marker_color=COLORS.get("accent", "#23CED3"),
            ),
            secondary_y=False,
        )

    fig.add_trace(
        go.Scatter(
            x=daily["FECHA"], y=daily["INSTOCK_PCT"],
            name="InStock % Tiendas", mode="lines+markers",
            line=dict(color="#E74C3C", width=2),
            marker=dict(size=4),
        ),
        secondary_y=True,
    )

    fig.update_layout(
        title="Evolucion Diaria — Venta Perdida vs InStock %",
        barmode="stack",
        height=450,
        margin=dict(l=20, r=20, t=40, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    fig.update_yaxes(title_text=f"Venta Perdida ({_vp_sym()})", secondary_y=False)
    fig.update_yaxes(title_text="InStock %", secondary_y=True, range=[0, 105])

    return fig


def _chart_top_skus(df, value_col="VP_PESOS", n=20, title="Top 20 SKUs"):
    """Horizontal bar chart of top N SKUs by VP."""
    if df.empty:
        return None

    agg_dict = {value_col: "sum"}
    if "NOM_PRODUCTO" in df.columns:
        agg_dict["NOM_PRODUCTO"] = "first"

    agg = (
        df.groupby("SKU_PRODUCTO")
        .agg(**{k: (k, v) for k, v in agg_dict.items()})
        .reset_index()
        .sort_values(value_col, ascending=False)
        .head(n)
    )

    label_col = (
        "NOM_PRODUCTO" if "NOM_PRODUCTO" in agg.columns
        else "SKU_PRODUCTO"
    )
    agg["LABEL"] = (
        agg["SKU_PRODUCTO"].astype(str) + " - "
        + agg[label_col].astype(str).str[:35]
    )

    fig = go.Figure(
        go.Bar(
            x=agg[value_col].values[::-1],
            y=agg["LABEL"].values[::-1],
            orientation="h",
            marker_color=COLORS.get("primary", "#065E8B"),
            text=[f"{_vp_sym()} {v:,.0f}" for v in agg[value_col].values[::-1]],
            textposition="outside",
        )
    )
    fig.update_layout(
        title=title,
        xaxis_title=f"Venta Perdida ({_vp_sym()})",
        height=max(400, n * 28),
        margin=dict(l=20, r=20, t=40, b=20),
    )
    return fig


def _chart_vp_por_dimension(df, dim_col, title="VP por Dimension"):
    """Bar chart of VP aggregated by a dimension."""
    if df.empty or dim_col not in df.columns:
        return None
    agg = (
        df.groupby(dim_col)["VP_PESOS"].sum()
        .reset_index()
        .sort_values("VP_PESOS", ascending=False)
    )
    fig = go.Figure(
        go.Bar(
            x=agg[dim_col], y=agg["VP_PESOS"],
            marker_color=COLORS.get("accent", "#23CED3"),
            text=[f"{_vp_sym()} {v:,.0f}" for v in agg["VP_PESOS"]],
            textposition="outside",
        )
    )
    fig.update_layout(
        title=title, height=400,
        margin=dict(l=20, r=20, t=40, b=20),
    )
    return fig


def _chart_canal_donut(df):
    """Donut chart showing VP split by channel."""
    if df.empty or "CANAL" not in df.columns:
        return None
    agg = df.groupby("CANAL")["VP_PESOS"].sum().reset_index()
    fig = go.Figure(
        go.Pie(
            labels=agg["CANAL"], values=agg["VP_PESOS"], hole=0.45,
            marker_colors=[
                COLORS.get("primary", "#065E8B"),
                COLORS.get("accent", "#23CED3"),
            ],
            textinfo="label+percent+value",
            texttemplate="%{label}<br>%{percent}<br>" + _vp_sym() + " %{value:,.0f}",
        )
    )
    fig.update_layout(
        title="VP por Canal CD", height=400,
        margin=dict(l=20, r=20, t=40, b=20),
    )
    return fig


# ── Summary tables (channel × month) ─────────────────────────────────────────

_MES_CORTO = ["", "Ene", "Feb", "Mar", "Abr", "May",
              "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

# Channel colours for the summary tables
_CH_COLORS = {
    "Etail":       ("#0D9488", "#F0FDFA"),   # teal accent + light bg
    "Mayoristas":  ("#065E8B", "#EFF6FF"),   # blue accent + light bg
    "Tienda":      ("#7C3AED", "#F5F3FF"),   # purple accent + light bg
}
_CH_TIPO = {
    "Quiebre Producto": ("#8B0000", "#FEF2F2"),
    "Reposicion":       ("#D97706", "#FFFBEB"),
}


def _fmt_m(val: float) -> str:
    """Format large currency values: 1.2M o 345K con el símbolo activo (ya convertido)."""
    sym = _vp_sym()
    if abs(val) >= 1_000_000:
        return f"{sym} {val / 1_000_000:,.1f}M"
    if abs(val) >= 1_000:
        return f"{sym} {val / 1_000:,.0f}K"
    return f"{sym} {val:,.0f}"


def _html_summary_table(title, subtitle, col_headers, rows, accent_color,
                        row_meta=None):
    """Build a styled HTML summary table.

    Parameters
    ----------
    title : str          Section title
    subtitle : str       Small description
    col_headers : list   Column header labels (first = row label, rest = months + total)
    rows : list[list]    Each row: [label, val1, val2, ..., total]
    accent_color : str   CSS colour for the title bar
    row_meta : list      Optional list of (accent, bg) per data row (excl totals)
    """
    n_cols = len(col_headers)

    # ── CSS ──
    html = f"""
    <div style="margin-bottom:1.5rem;border-radius:12px;overflow:hidden;
                border:1px solid #e2e8f0;box-shadow:0 1px 3px rgba(0,0,0,0.06);">
      <!-- Title bar -->
      <div style="background:{accent_color};padding:0.7rem 1.2rem;
                  display:flex;align-items:center;justify-content:space-between;">
        <span style="font-weight:700;font-size:0.95rem;color:#fff;">
          {title}
        </span>
        <span style="font-size:0.72rem;color:rgba(255,255,255,0.75);">
          {subtitle}
        </span>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:0.82rem;
                     font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
                     Roboto,sans-serif;">
        <thead>
          <tr style="background:#f8fafc;">
    """
    # Column headers
    for i, h in enumerate(col_headers):
        align = "left" if i == 0 else "right"
        fw = "700" if h == "TOTAL" else "600"
        bg = "#f1f5f9" if h == "TOTAL" else "#f8fafc"
        html += (
            f'<th style="padding:0.55rem 0.9rem;text-align:{align};'
            f'font-weight:{fw};color:#64748b;font-size:0.72rem;'
            f'text-transform:uppercase;letter-spacing:0.5px;'
            f'border-bottom:2px solid #e2e8f0;background:{bg};">'
            f'{h}</th>'
        )
    html += "</tr></thead><tbody>"

    # Data rows
    for r_idx, row in enumerate(rows):
        is_total = (r_idx == len(rows) - 1)
        bg = "#f8fafc" if is_total else ("#fff" if r_idx % 2 == 0 else "#fafbfc")
        border_top = "border-top:2px solid #cbd5e1;" if is_total else ""

        # Determine row accent colour
        row_accent = "#334155"
        row_bg = bg
        if row_meta and r_idx < len(row_meta):
            row_accent, row_bg = row_meta[r_idx]
            if not is_total:
                bg = row_bg

        html += f'<tr style="background:{bg};{border_top}">'
        for i, val in enumerate(row):
            if i == 0:
                # Label cell with coloured dot
                dot = ""
                if not is_total and row_meta and r_idx < len(row_meta):
                    dot = (
                        f'<span style="display:inline-block;width:8px;height:8px;'
                        f'border-radius:50%;background:{row_accent};'
                        f'margin-right:6px;vertical-align:middle;"></span>'
                    )
                fw = "700" if is_total else "600"
                html += (
                    f'<td style="padding:0.6rem 0.9rem;font-weight:{fw};'
                    f'color:{row_accent};white-space:nowrap;">'
                    f'{dot}{val}</td>'
                )
            else:
                is_total_col = (i == n_cols - 1)
                fw = "700" if is_total or is_total_col else "400"
                color = "#1e293b" if is_total or is_total_col else "#475569"
                bg_cell = "#f1f5f9" if is_total_col and not is_total else ""
                bg_style = f"background:{bg_cell};" if bg_cell else ""
                formatted = _fmt_m(val) if isinstance(val, (int, float)) else str(val)
                html += (
                    f'<td style="padding:0.6rem 0.9rem;text-align:right;'
                    f'font-weight:{fw};color:{color};{bg_style}'
                    f'font-variant-numeric:tabular-nums;">'
                    f'{formatted}</td>'
                )
        html += "</tr>"

    html += "</tbody></table></div>"
    return html


def _render_channel_month_summary(df_tienda, df_cd, df_detail):
    """Render two styled summary tables: Channel×Month and Tienda breakdown."""

    # ── Collect all dates ──
    all_dates = pd.Series(dtype="datetime64[ns]")
    if not df_tienda.empty and "FECHA" in df_tienda.columns:
        all_dates = pd.concat([
            all_dates, pd.to_datetime(df_tienda["FECHA"])
        ])
    if not df_cd.empty and "FECHA" in df_cd.columns:
        all_dates = pd.concat([
            all_dates, pd.to_datetime(df_cd["FECHA"])
        ])
    if all_dates.empty:
        st.info("No hay datos para generar el resumen mensual.")
        return

    # Sorted unique year-month periods
    ym_periods = sorted(all_dates.dt.to_period("M").unique())
    month_labels = [
        f"{_MES_CORTO[p.month]} {p.year}" for p in ym_periods
    ]

    # ── Table 1: VP by Channel × Month ──
    # Tienda
    if not df_tienda.empty:
        df_t = df_tienda.copy()
        df_t["_YM"] = pd.to_datetime(df_t["FECHA"]).dt.to_period("M")
        tienda_agg = df_t.groupby("_YM")["VP_PESOS"].sum()
    else:
        tienda_agg = pd.Series(dtype=float)

    # CD split by canal
    if not df_cd.empty:
        df_c = df_cd.copy()
        df_c["_YM"] = pd.to_datetime(df_c["FECHA"]).dt.to_period("M")
        etail_agg = (
            df_c[df_c["CANAL"] == "ETAIL"]
            .groupby("_YM")["VP_PESOS"].sum()
        )
        mayor_agg = (
            df_c[df_c["CANAL"].isin(["MAYOR", "MAYORISTA"])]
            .groupby("_YM")["VP_PESOS"].sum()
        )
    else:
        etail_agg = pd.Series(dtype=float)
        mayor_agg = pd.Series(dtype=float)

    channels_data = [
        ("Etail", etail_agg),
        ("Mayoristas", mayor_agg),
        ("Tienda", tienda_agg),
    ]

    table1_rows = []
    table1_meta = []
    for ch_name, series in channels_data:
        row = [ch_name]
        total = 0.0
        for p in ym_periods:
            val = float(series.get(p, 0))
            row.append(val)
            total += val
        row.append(total)
        table1_rows.append(row)
        table1_meta.append(_CH_COLORS.get(ch_name, ("#334155", "#fff")))

    # Total row
    total_row = ["TOTAL"]
    for i in range(len(ym_periods)):
        total_row.append(sum(r[i + 1] for r in table1_rows))
    total_row.append(sum(r[-1] for r in table1_rows))
    table1_rows.append(total_row)

    col_headers_1 = ["Canal"] + month_labels + ["TOTAL"]
    html1 = _html_summary_table(
        title="Venta Perdida por Canal",
        subtitle=f"{len(ym_periods)} meses",
        col_headers=col_headers_1,
        rows=table1_rows,
        accent_color="#1e293b",
        row_meta=table1_meta,
    )
    st.html(html1)

    # ── Table 2: Tienda breakdown — Quiebre vs Reposicion ──
    qp_agg = pd.Series(dtype=float)
    rep_agg = pd.Series(dtype=float)

    if not df_tienda.empty:
        df_t2 = df_tienda.copy()
        df_t2["_YM"] = pd.to_datetime(df_t2["FECHA"]).dt.to_period("M")
        if "VP_PESOS_QP" in df_t2.columns:
            qp_agg = df_t2.groupby("_YM")["VP_PESOS_QP"].sum()
        if "VP_PESOS_REP" in df_t2.columns:
            rep_agg = df_t2.groupby("_YM")["VP_PESOS_REP"].sum()

    # If columns not in df_tienda, try df_detail
    if qp_agg.sum() == 0 and not df_detail.empty and "TIPO_VP" in df_detail.columns:
        df_d = df_detail.copy()
        df_d["_YM"] = pd.to_datetime(df_d["FECHA"]).dt.to_period("M")
        qp_agg = (
            df_d[df_d["TIPO_VP"] == "QUIEBRE_PRODUCTO"]
            .groupby("_YM")["VP_PESOS"].sum()
        )
        rep_agg = (
            df_d[df_d["TIPO_VP"] == "REPOSICION"]
            .groupby("_YM")["VP_PESOS"].sum()
        )

    tipos_data = [
        ("Quiebre Producto", qp_agg),
        ("Reposicion", rep_agg),
    ]

    table2_rows = []
    table2_meta = []
    for tipo_name, series in tipos_data:
        row = [tipo_name]
        total = 0.0
        for p in ym_periods:
            val = float(series.get(p, 0))
            row.append(val)
            total += val
        row.append(total)
        table2_rows.append(row)
        table2_meta.append(_CH_TIPO.get(tipo_name, ("#334155", "#fff")))

    # Total row
    total_row2 = ["TOTAL"]
    for i in range(len(ym_periods)):
        total_row2.append(sum(r[i + 1] for r in table2_rows))
    total_row2.append(sum(r[-1] for r in table2_rows))
    table2_rows.append(total_row2)

    col_headers_2 = ["Tipo VP"] + month_labels + ["TOTAL"]
    html2 = _html_summary_table(
        title="VP Tiendas — Quiebre Producto vs Reposicion",
        subtitle="Apertura por tipo de venta perdida",
        col_headers=col_headers_2,
        rows=table2_rows,
        accent_color="#7C3AED",
        row_meta=table2_meta,
    )
    st.html(html2)


# ── Main render ───────────────────────────────────────────────────────────────


def render_venta_perdida(conn):
    st.html("<h2 class='sub-header'>📉 Venta Perdida</h2>")

    # ── Load filter options and dates ──
    with lottie_spinner("snowflake"):
        max_fecha = _max_fecha_instock(conn)
        min_fecha = _min_fecha_instock(conn)
        opts_area = _load_distinct(conn, "AREA")
        opts_linea = _load_distinct(conn, "LINEA")
        opts_sublinea = _load_distinct(conn, "SUBLINEA")
        opts_marca = _load_distinct(conn, "MARCA")
        opts_proveedor = _load_distinct(conn, "PROVEEDOR")
        opts_mix = _load_distinct(conn, "MIX_OFICIAL")

    # ── Filters (Chile-style expander) ──
    with st.expander("🔎 Filtros", expanded=True):
        # Row 1: Date range + Mix
        c_d1, c_d2, c_mix = st.columns([1, 1, 1])
        default_desde = max(min_fecha, max_fecha - timedelta(days=59))
        fecha_desde = c_d1.date_input(
            "Desde", value=default_desde,
            min_value=min_fecha, max_value=max_fecha,
        )
        fecha_hasta = c_d2.date_input(
            "Hasta", value=max_fecha,
            min_value=min_fecha, max_value=max_fecha,
        )
        f_mix = c_mix.multiselect(
            "Mix Oficial", opts_mix,
            help="Dejar vacio = todos los mix",
        )

        # Row 1b: Checkboxes + Grace Period
        c_chk1, c_chk2, c_chk3, c_grace = st.columns([1, 1, 1, 1])
        f_perfil = c_chk1.checkbox(
            "Solo perfil definido",
            value=True,
            help="Medir VP solo en SKU x Tienda con perfil = SI",
        )
        _cd_options = {
            "Sin filtro CD": "none",
            "Stock CD > 0": "stock_gt_0",
            "InStock CD (≥1 día)": "instock_cd",
        }
        _cd_sel = c_chk2.selectbox(
            "Filtro CD",
            options=list(_cd_options.keys()),
            index=0,
            help=(
                "InStock CD: VP donde stock CD ≥ demanda diaria "
                "promedio todos los canales (VCM ventana 2 meses). "
                "Stock CD > 0: VP donde CD tenía algo de stock. "
                "Sin filtro: todas las combinaciones SKU×día."
            ),
        )
        f_cd_filter = _cd_options[_cd_sel]
        f_grace = c_chk3.checkbox(
            "Gracia lead time",
            value=False,
            help="Tras recuperar stock en CD, esperar N dias antes de "
                 "medir VP (tiempo de transito CD → tiendas).",
        )
        grace_days = 3
        if f_grace:
            grace_days = c_grace.slider(
                "Dias gracia", 1, 30, 3,
                help="Lead time CD → tienda en dias.",
            )

        # Row 2: SKU + dimensions
        c1, c2, c3 = st.columns(3)
        f_sku = limpiar_lista(
            c1.text_area("SKUs (separados por coma/espacio)", height=68)
        )
        f_area = c1.multiselect("Area", opts_area)
        f_linea = c2.multiselect("Linea", opts_linea)
        f_sublinea = c2.multiselect("Sublinea", opts_sublinea)
        f_marca = c3.multiselect("Marca", opts_marca)
        f_proveedor = c3.multiselect("Proveedor", opts_proveedor)

        # Row 3: Status filter — dedicated full-width row
        cs1, cs2 = st.columns([1, 2])
        f_status = cs1.multiselect(
            "Status del Producto",
            ["NUEVO", "ESTABLECIDO", "SIN VENTAS"],
            help="NUEVO = primera venta dentro del periodo  |  "
                 "ESTABLECIDO = ya tenia ventas antes del periodo  |  "
                 "SIN VENTAS = SKU sin historial de ventas",
        )

        # Show demand window info per month
        month_ranges = _split_into_months(fecha_desde, fecha_hasta)
        demand_info = []
        for ms, _ in month_ranges:
            fi, ff = _get_demand_window(date(ms.year, ms.month, 15))
            demand_info.append(
                f"**{_month_label(ms.year, ms.month)}**: "
                f"{_month_label(fi.year, fi.month)} + "
                f"{_month_label(ff.year, ff.month)}"
            )
        st.caption(
            "📅 Ventanas de demanda VCM: " + " | ".join(demand_info)
        )

    n_dias = (fecha_hasta - fecha_desde).days + 1
    if n_dias > 90:
        st.warning("El rango es mayor a 90 dias. La consulta puede tardar.")

    # ── Execute ──
    if st.button(
        "🚀 Calcular Venta Perdida", type="primary",
        use_container_width=True,
    ):
        try:
            progress = st.progress(0, text="Consultando Snowflake...")

            df_tienda, df_cd, df_by_store, df_detail, df_detail_full = _compute_vp_range(
                conn, fecha_desde, fecha_hasta,
                mix_values=f_mix or None,
                perfil_only=f_perfil,
                filter_cd_instock=f_cd_filter,
                apply_grace=f_grace,
                grace_lead_days=grace_days,
                progress_bar=progress,
            )

            progress.progress(70, text="Enriqueciendo con maestra...")

            df_tienda = _enrich_with_maestra(df_tienda, conn)
            df_cd = _enrich_with_maestra(df_cd, conn)

            # ── Enhancement 3: New product flag ──
            progress.progress(80, text="Clasificando productos nuevos...")
            df_first_sale = _load_first_sale_dates(conn)
            first_ms = month_ranges[0][0]
            demand_start_flag, _ = _get_demand_window(
                date(first_ms.year, first_ms.month, 15)
            )
            df_tienda = _enrich_with_new_flag(
                df_tienda, df_first_sale, demand_start_flag,
            )
            df_cd = _enrich_with_new_flag(
                df_cd, df_first_sale, demand_start_flag,
            )

            # Apply PM filter
            if not df_tienda.empty:
                df_tienda = apply_pm_filter(df_tienda)
            if not df_cd.empty:
                df_cd = apply_pm_filter(df_cd)

            # Apply user dimension filters (post-SQL)
            df_tienda = _apply_filters(
                df_tienda, f_sku, f_area, f_linea,
                f_sublinea, f_marca, f_proveedor, f_status,
            )
            df_cd = _apply_filters(
                df_cd, f_sku, f_area, f_linea,
                f_sublinea, f_marca, f_proveedor, f_status,
            )

            progress.progress(100, text="Listo!")

            # Enrich detail with maestra + PM + new flag
            df_detail = _enrich_with_maestra(df_detail, conn)
            df_detail = _enrich_with_new_flag(
                df_detail, df_first_sale, demand_start_flag,
            )
            if not df_detail.empty:
                df_detail = apply_pm_filter(df_detail)

            # Enrich detail_full (unfiltered) for Insights tab
            df_detail_full = _enrich_with_maestra(df_detail_full, conn)
            df_detail_full = _enrich_with_new_flag(
                df_detail_full, df_first_sale, demand_start_flag,
            )
            if not df_detail_full.empty:
                df_detail_full = apply_pm_filter(df_detail_full)

            st.session_state["vp_tienda"] = df_tienda
            st.session_state["vp_cd"] = df_cd
            st.session_state["vp_by_store"] = df_by_store
            st.session_state["vp_detail"] = df_detail
            st.session_state["vp_detail_full"] = df_detail_full
            st.session_state["vp_ready"] = True
            st.session_state["vp_desde"] = fecha_desde
            st.session_state["vp_hasta"] = fecha_hasta
            st.session_state["vp_cd_filter"] = f_cd_filter
            st.session_state["vp_grace"] = f_grace
            st.session_state["vp_grace_days"] = grace_days

            # Run diagnostics (always — to show excluded stores info)
            try:
                first_ms = month_ranges[0][0]
                fi, ff = _get_demand_window(
                    date(first_ms.year, first_ms.month, 15)
                )
                diag = _run_diagnostics(
                    conn, fecha_desde, fecha_hasta, fi, ff,
                )
                st.session_state["vp_diagnostics"] = diag
            except Exception:
                pass

        except Exception as e:
            st.error(f"Error calculando venta perdida: {e}")
            import traceback
            st.code(traceback.format_exc())
            return

    # ── Show results (from session state) ──
    if not st.session_state.get("vp_ready"):
        return

    df_tienda = st.session_state["vp_tienda"]
    df_cd = st.session_state["vp_cd"]
    df_by_store = st.session_state.get("vp_by_store", pd.DataFrame())
    df_detail = st.session_state.get("vp_detail", pd.DataFrame())
    vp_desde = st.session_state["vp_desde"]
    vp_hasta = st.session_state["vp_hasta"]

    # ── Toggle de moneda (S/ ↔ USD) ──
    _mc_l, _mc_r = st.columns([3, 1])
    with _mc_r:
        st.radio(
            "💱 Moneda",
            ["S/", "USD ($)"],
            horizontal=True,
            key="vp_moneda",
            help=(
                "Convierte todos los montos del módulo (KPIs, tablas y "
                "gráficos). USD usa el TC USD/PEN del sidebar."
            ),
        )
    if st.session_state.get("vp_moneda") == "USD ($)":
        _mc_l.caption(f"Montos en USD · TC USD/PEN = {_vp_div():.2f}")

    # Conversión a la moneda elegida (copias; la sesión queda en soles)
    df_tienda = _vp_convert(df_tienda)
    df_cd = _vp_convert(df_cd)
    df_by_store = _vp_convert(df_by_store)
    df_detail = _vp_convert(df_detail)

    # ── KPIs — Chile-style cards ──
    vp_t = df_tienda["VP_PESOS"].sum() if not df_tienda.empty else 0
    vp_c = df_cd["VP_PESOS"].sum() if not df_cd.empty else 0
    vp_total = vp_t + vp_c
    n_dias_rango = (vp_hasta - vp_desde).days + 1
    vp_diario_prom = vp_total / n_dias_rango if n_dias_rango > 0 else 0

    skus_t = (
        set(df_tienda["SKU_PRODUCTO"].tolist())
        if not df_tienda.empty else set()
    )
    skus_c = (
        set(df_cd["SKU_PRODUCTO"].tolist())
        if not df_cd.empty else set()
    )
    skus_all = skus_t | skus_c

    # Compute TIPO_VP split from FILTERED detail (must match VP Tiendas)
    df_detail_full = _vp_convert(st.session_state.get(
        "vp_detail_full", pd.DataFrame()
    ))
    vp_quiebre = 0.0
    vp_reposicion = 0.0
    if (not df_detail.empty
            and "TIPO_VP" in df_detail.columns
            and "VP_PESOS" in df_detail.columns):
        # Apply same dimension filters as df_tienda
        _df_det_filt = _apply_filters(
            df_detail, f_sku, f_area, f_linea,
            f_sublinea, f_marca, f_proveedor, f_status,
        )
        vp_quiebre = float(
            _df_det_filt.loc[
                _df_det_filt["TIPO_VP"] == "QUIEBRE_PRODUCTO",
                "VP_PESOS",
            ].sum()
        )
        vp_reposicion = float(
            _df_det_filt.loc[
                _df_det_filt["TIPO_VP"] == "REPOSICION",
                "VP_PESOS",
            ].sum()
        )

    st.html(_hdr(
        f"📉 Venta Perdida — {vp_desde.strftime('%d/%m/%Y')} "
        f"al {vp_hasta.strftime('%d/%m/%Y')} ({n_dias_rango} dias)"
    ))

    # ── Row 1: Main KPIs (Total, Tiendas, CD) ──
    k1, k2, k3 = st.columns(3)
    with k1:
        st.html(simple_kpi_card(
            label="VP TOTAL ACUMULADA",
            value=_fmt_currency(vp_total),
            accent_color=COLORS["status_critical"],
            subtitle=f"Prom diario: {_fmt_currency(vp_diario_prom)}",
        ))
    with k2:
        st.html(simple_kpi_card(
            label="VP TIENDAS",
            value=_fmt_currency(vp_t),
            accent_color=COLORS["primary"],
            subtitle=f"{len(skus_t):,} SKUs afectados",
        ))
    with k3:
        st.html(simple_kpi_card(
            label="VP CD (MAYOR + ETAIL)",
            value=_fmt_currency(vp_c),
            accent_color=COLORS["tertiary_teal"],
            subtitle=f"{len(skus_c):,} SKUs afectados",
        ))

    # ── Row 2: Breakdown VP Tiendas → Quiebre + Reposición ──
    pct_qp = vp_quiebre / vp_t * 100 if vp_t > 0 else 0
    pct_rp = vp_reposicion / vp_t * 100 if vp_t > 0 else 0
    bar_qp = max(pct_qp, 2)  # min width for visibility
    bar_rp = max(pct_rp, 2)
    st.html(f"""
    <div style="margin:-0.4rem 0 0.8rem 0; padding:0.9rem 1.2rem;
                background:#f8fafc; border-radius:10px;
                border-left:4px solid {COLORS["primary"]};">
        <div style="font-size:0.7rem; color:#94a3b8; text-transform:uppercase;
                    letter-spacing:0.5px; margin-bottom:0.5rem;">
            Descomposicion VP Tiendas
        </div>
        <div style="display:flex; gap:1.5rem; align-items:center;
                    flex-wrap:wrap;">
            <div style="display:flex; align-items:center; gap:0.5rem;">
                <div style="width:12px; height:12px; border-radius:3px;
                            background:#8B0000;"></div>
                <span style="font-size:0.82rem; color:#475569; font-weight:600;">
                    Quiebre Producto
                </span>
                <span style="font-size:1.1rem; font-weight:700; color:#8B0000;">
                    {_fmt_currency(vp_quiebre)}
                </span>
                <span style="font-size:0.75rem; color:#94a3b8;">
                    ({pct_qp:.0f}%) — CD sin stock
                </span>
            </div>
            <div style="display:flex; align-items:center; gap:0.5rem;">
                <div style="width:12px; height:12px; border-radius:3px;
                            background:{COLORS["status_at_risk"]};"></div>
                <span style="font-size:0.82rem; color:#475569; font-weight:600;">
                    Reposicion
                </span>
                <span style="font-size:1.1rem; font-weight:700;
                            color:{COLORS["status_at_risk"]};">
                    {_fmt_currency(vp_reposicion)}
                </span>
                <span style="font-size:0.75rem; color:#94a3b8;">
                    ({pct_rp:.0f}%) — CD tenia stock, tienda no
                </span>
            </div>
        </div>
        <div style="display:flex; height:8px; border-radius:4px;
                    overflow:hidden; margin-top:0.6rem;
                    background:#e2e8f0;">
            <div style="width:{bar_qp:.1f}%; background:#8B0000;
                        border-radius:4px 0 0 4px;"></div>
            <div style="width:{bar_rp:.1f}%;
                        background:{COLORS["status_at_risk"]};
                        border-radius:0 4px 4px 0;"></div>
        </div>
    </div>
    """)

    # ── Excluded stores info (hardcoded) ──
    st.info(
        "🏬 Solo **tiendas fisicas** (cod_tipoalmacen=9). "
        "**Excluidas** (cerradas): Bellavista, Chiclayo 2, "
        "San Miguel 2, Outlet, Cajamarca, Trujillo 2",
        icon="🏪",
    )

    # ── Filter info banners ──
    _cd_mode = st.session_state.get("vp_cd_filter", "none")
    if _cd_mode == "instock_cd":
        st.info(
            "**Filtro CD activo**: Solo VP tiendas para SKUs con "
            "InStock CD = 1 (stock CD ≥ demanda diaria promedio "
            "todos los canales, calculada desde VCM)",
            icon="📦",
        )
    elif _cd_mode == "stock_gt_0":
        st.info(
            "**Filtro CD activo**: Solo VP tiendas para SKUs donde CD "
            "tenía stock > 0 ese día",
            icon="📦",
        )

    # ── CD InStock diagnostic ──
    if (not df_detail.empty and "INSTOCK_CD" in df_detail.columns
            and "STOCK_CD" in df_detail.columns):
        _n_total = len(df_detail)
        _n_cd_gt0 = int((df_detail["STOCK_CD"].fillna(0) > 0).sum())
        _n_is_cd = int((df_detail["INSTOCK_CD"].fillna(0) == 1).sum())
        _pct_gt0 = _n_cd_gt0 / _n_total * 100 if _n_total else 0
        _pct_is = _n_is_cd / _n_total * 100 if _n_total else 0
        st.caption(
            f"📊 **Cobertura CD**: de {_n_total:,} registros detail → "
            f"Stock CD > 0: {_n_cd_gt0:,} ({_pct_gt0:.1f}%) · "
            f"InStock CD = 1: {_n_is_cd:,} ({_pct_is:.1f}%)"
        )
    if st.session_state.get("vp_grace"):
        gd = st.session_state.get("vp_grace_days", 3)
        # Count grace-adjusted rows
        n_gracia = 0
        if not df_detail.empty and "EN_GRACIA" in df_detail.columns:
            n_gracia = int(df_detail["EN_GRACIA"].sum())
        st.info(
            f"**Gracia lead time**: {gd} dias tras recovery CD. "
            f"{n_gracia:,} registros en periodo de gracia (VP anulada)",
            icon="⏳",
        )

    # ── Diagnostics (if tiendas empty) ──
    diag = st.session_state.get("vp_diagnostics")
    _store_diag = st.session_state.get("vp_store_diag")
    if df_tienda.empty:
        with st.expander("🔧 Diagnostico VP Tiendas = S/ 0", expanded=True):
            # Store ID overlap diagnostic
            if _store_diag is not None and not _store_diag.empty:
                st.write("**Overlap tiendas demand vs stock:**")
                st.dataframe(_store_diag, hide_index=True)
            if diag:
                if diag.get("tipos_almacen") is not None:
                    st.write("**Tipos almacen en dt_almacen:**")
                    st.dataframe(diag["tipos_almacen"], hide_index=True)
                if diag.get("stock_por_tipo") is not None:
                    st.write("**Stock (INSTOCK) por tipo almacen:**")
                    st.dataframe(diag["stock_por_tipo"], hide_index=True)
                if diag.get("perfil") is not None:
                    st.write("**Distribucion PERFIL (tiendas tipo=9):**")
                    st.dataframe(diag["perfil"], hide_index=True)
                if diag.get("demanda_tienda") is not None:
                    st.write("**Demanda VCM tiendas (tipo=9):**")
                    st.dataframe(diag["demanda_tienda"], hide_index=True)
            st.write(
                "**Filtro:** Solo tiendas fisicas (cod_tipoalmacen=9). "
                "**Excluidas:** Bellavista, Chiclayo 2, "
                "San Miguel 2, Outlet, Cajamarca, Trujillo 2"
            )

            # CROSS JOIN diagnostic
            _xj_diag = st.session_state.get("vp_crossjoin_diag")
            if _xj_diag is not None:
                st.write("---")
                st.write("**CROSS JOIN diagnostic:**")
                if isinstance(_xj_diag, str):
                    st.error(_xj_diag)
                else:
                    st.dataframe(_xj_diag, hide_index=True)

            # Aggregate result debug
            _dbg = st.session_state.get("_debug_df_t")
            if _dbg:
                st.write("---")
                st.write(
                    f"**df_tienda resultado:** {_dbg['rows']} filas, "
                    f"VP_UNIDADES={_dbg['vp_unidades']:,.2f}, "
                    f"VP_PESOS=S/ {_dbg['vp_pesos']:,.0f}"
                )
                if _dbg.get("columns"):
                    st.caption(f"Columnas: {_dbg['columns']}")

    # ── Tabs ──
    (tab_evo, tab_tienda, tab_sucursal, tab_cd,
     tab_insights, tab_detalle, tab_resumen) = (
        st.tabs([
            "📈 Evolucion Diaria",
            "🏬 Tiendas (SKU)",
            "🏪 Por Sucursal",
            "📦 CD (Mayor/Etail)",
            "💡 Insights",
            "📋 Detalle Calculo",
            "📊 Resumen",
        ])
    )

    # ── Tab Evolucion ──
    with tab_evo:
        # Use filtered detail for stacked bars (matches KPIs)
        _df_det_evo = _apply_filters(
            df_detail, f_sku, f_area, f_linea,
            f_sublinea, f_marca, f_proveedor, f_status,
        ) if not df_detail.empty else pd.DataFrame()
        fig_evo = _chart_evolucion(df_tienda, df_cd, _df_det_evo)
        if fig_evo:
            st.plotly_chart(fig_evo, use_container_width=True)
        else:
            st.warning("No hay datos de evolucion para mostrar.")

        # Daily table
        parts = []
        if not df_tienda.empty:
            dt = df_tienda.groupby("FECHA").agg(
                VP_TIENDA=("VP_PESOS", "sum"),
                VP_UND_TIENDA=("VP_UNIDADES", "sum"),
            ).reset_index()
            parts.append(dt)
        if not df_cd.empty:
            dc = df_cd.groupby("FECHA").agg(
                VP_CD=("VP_PESOS", "sum"),
                VP_UND_CD=("VP_UNIDADES", "sum"),
            ).reset_index()
            parts.append(dc)

        if parts:
            for p in parts:
                p["FECHA"] = pd.to_datetime(p["FECHA"])
            if len(parts) == 2:
                daily_tbl = parts[0].merge(
                    parts[1], on="FECHA", how="outer"
                ).fillna(0)
            else:
                daily_tbl = parts[0].copy()
            daily_tbl = daily_tbl.sort_values("FECHA")
            daily_tbl["VP_TOTAL"] = (
                daily_tbl.get("VP_TIENDA", 0)
                + daily_tbl.get("VP_CD", 0)
            )
            st.dataframe(
                daily_tbl,
                column_config={
                    "FECHA": st.column_config.DateColumn("Fecha"),
                    "VP_TIENDA": st.column_config.NumberColumn(
                        f"VP Tiendas ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                    ),
                    "VP_CD": st.column_config.NumberColumn(
                        f"VP CD ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                    ),
                    "VP_TOTAL": st.column_config.NumberColumn(
                        f"VP Total ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                    ),
                },
                use_container_width=True, hide_index=True,
            )
            download_buttons(daily_tbl, "venta_perdida_diaria")

    # ── Tab Tiendas ──
    with tab_tienda:
        if df_tienda.empty:
            st.warning(
                "No se encontro venta perdida en tiendas para este rango."
            )
        else:
            # Aggregate by SKU
            agg_cols = {
                "VP_PESOS": "sum",
                "VP_UNIDADES": "sum",
                "N_TIENDAS_TOTAL": "max",
                "N_TIENDAS_INSTOCK": "sum",
                "N_TIENDAS_OOS": "sum",
            }
            for c in [
                "NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA",
                "MARCA", "MODELO", "PROVEEDOR", "MIX_OFICIAL",
                "PRODUCTO_STATUS", "FIRST_SALE_DATE",
            ]:
                if c in df_tienda.columns:
                    agg_cols[c] = "first"

            df_sku_t = (
                df_tienda.groupby("SKU_PRODUCTO")
                .agg(**{k: (k, v) for k, v in agg_cols.items()})
                .reset_index()
                .sort_values("VP_PESOS", ascending=False)
            )

            # Calculate InStock % per SKU
            if "N_TIENDAS_TOTAL" in df_sku_t.columns:
                total_checks = (
                    df_sku_t["N_TIENDAS_INSTOCK"]
                    + df_sku_t["N_TIENDAS_OOS"]
                )
                df_sku_t["IS_TIENDA_PCT"] = np.where(
                    total_checks > 0,
                    df_sku_t["N_TIENDAS_INSTOCK"] / total_checks * 100,
                    100,
                )

            # Section KPIs
            st.html(_hdr("🏬 VP Tiendas — Top SKUs"))
            kt1, kt2, kt3 = st.columns(3)
            with kt1:
                st.html(simple_kpi_card(
                    label="VP TIENDAS TOTAL",
                    value=_fmt_currency(vp_t),
                    accent_color=COLORS["primary"],
                ))
            with kt2:
                st.html(simple_kpi_card(
                    label="SKUS CON VP",
                    value=f"{len(df_sku_t):,}",
                    accent_color=COLORS["tertiary_teal"],
                ))
            with kt3:
                avg_is = (
                    df_sku_t["IS_TIENDA_PCT"].mean()
                    if "IS_TIENDA_PCT" in df_sku_t.columns else 0
                )
                is_color = (
                    COLORS["status_on_track"] if avg_is >= 93
                    else COLORS["status_at_risk"] if avg_is >= 85
                    else COLORS["status_critical"]
                )
                st.html(simple_kpi_card(
                    label="INSTOCK % PROMEDIO",
                    value=f"{avg_is:.1f}%",
                    accent_color=is_color,
                ))

            # Charts
            ct1, ct2 = st.columns(2)
            with ct1:
                fig = _chart_top_skus(
                    df_sku_t, title="Top 20 SKUs — VP Tiendas",
                )
                if fig:
                    st.plotly_chart(fig, use_container_width=True)
            with ct2:
                fig = _chart_vp_por_dimension(
                    df_sku_t, "AREA", "VP por Area — Tiendas",
                )
                if fig:
                    st.plotly_chart(fig, use_container_width=True)

            # Table with InStock indicators
            display_cols = [c for c in [
                "SKU_PRODUCTO", "NOM_PRODUCTO",
                "AREA", "LINEA", "SUBLINEA", "MARCA",
                "MIX_OFICIAL", "PRODUCTO_STATUS",
                "VP_PESOS", "VP_UNIDADES",
                "IS_TIENDA_PCT",
            ] if c in df_sku_t.columns]

            col_config = {
                "SKU_PRODUCTO": st.column_config.TextColumn("SKU"),
                "NOM_PRODUCTO": st.column_config.TextColumn(
                    "Producto", width="large",
                ),
                "PRODUCTO_STATUS": st.column_config.TextColumn(
                    "Status",
                    help="ESTABLECIDO / NUEVO / SIN VENTAS",
                ),
                "VP_PESOS": st.column_config.NumberColumn(
                    f"VP Acum ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                ),
                "VP_UNIDADES": st.column_config.NumberColumn(
                    "VP Acum (Und)", format="%.0f",
                ),
                "IS_TIENDA_PCT": st.column_config.ProgressColumn(
                    "IS Tienda %",
                    format="%.1f%%",
                    min_value=0,
                    max_value=100,
                ),
            }

            st.dataframe(
                df_sku_t[display_cols].head(500),
                column_config=col_config,
                use_container_width=True, height=500, hide_index=True,
            )
            download_buttons(df_sku_t[display_cols], "venta_perdida_tiendas")

    # ── Tab Por Sucursal ──
    with tab_sucursal:
        if df_by_store.empty:
            st.warning(
                "No se encontro venta perdida por sucursal para este rango."
            )
        else:
            # Aggregate across days → store totals
            agg_store = (
                df_by_store.groupby(["ID_SUCURSAL"])
                .agg(
                    DESCRIPCION_SUCURSAL=("DESCRIPCION_SUCURSAL", "first"),
                    VP_PESOS=("VP_PESOS", "sum"),
                    VP_UNIDADES=("VP_UNIDADES", "sum"),
                    N_SKUS=("N_SKUS", "max"),
                    STOCK_TOTAL=("STOCK_TOTAL", "sum"),
                    DEMANDA_TIENDA=("DEMANDA_TIENDA", "sum"),
                    SKUS_INSTOCK=("SKUS_INSTOCK", "sum"),
                    SKUS_TOTAL=("SKUS_TOTAL", "sum"),
                )
                .reset_index()
                .sort_values("VP_PESOS", ascending=False)
            )
            # InStock %
            agg_store["INSTOCK_PCT"] = np.where(
                agg_store["SKUS_TOTAL"] > 0,
                agg_store["SKUS_INSTOCK"]
                / agg_store["SKUS_TOTAL"] * 100,
                100,
            )

            # Section KPIs (Chile-style cards)
            st.html(_hdr("🏪 VP por Sucursal"))
            ks1, ks2, ks3 = st.columns(3)
            with ks1:
                st.html(simple_kpi_card(
                    label="SUCURSALES CON VP",
                    value=f"{len(agg_store):,}",
                    accent_color=COLORS["primary"],
                ))
            with ks2:
                avg_is = agg_store["INSTOCK_PCT"].mean()
                is_color = (
                    COLORS["status_on_track"] if avg_is >= 93
                    else COLORS["status_at_risk"] if avg_is >= 85
                    else COLORS["status_critical"]
                )
                st.html(simple_kpi_card(
                    label="INSTOCK % PROMEDIO",
                    value=f"{avg_is:.1f}%",
                    accent_color=is_color,
                ))
            with ks3:
                st.html(simple_kpi_card(
                    label="VP PROM POR SUCURSAL",
                    value=_fmt_currency(agg_store["VP_PESOS"].mean()),
                    accent_color=COLORS["tertiary_teal"],
                ))

            # Top stores chart
            top_n = min(30, len(agg_store))
            top_stores = agg_store.head(top_n).copy()
            top_stores["LABEL"] = (
                top_stores["ID_SUCURSAL"].astype(str) + " - "
                + top_stores["DESCRIPCION_SUCURSAL"].astype(str).str[:30]
            )

            fig_stores = make_subplots(specs=[[{"secondary_y": True}]])
            fig_stores.add_trace(
                go.Bar(
                    x=top_stores["LABEL"].values[::-1],
                    y=top_stores["VP_PESOS"].values[::-1],
                    name=f"VP ({_vp_sym()})",
                    marker_color=COLORS.get("primary", "#065E8B"),
                    text=[
                        f"{_vp_sym()} {v:,.0f}"
                        for v in top_stores["VP_PESOS"].values[::-1]
                    ],
                    textposition="outside",
                ),
                secondary_y=False,
            )
            fig_stores.add_trace(
                go.Scatter(
                    x=top_stores["LABEL"].values[::-1],
                    y=top_stores["INSTOCK_PCT"].values[::-1],
                    name="InStock %",
                    mode="lines+markers",
                    line=dict(color=COLORS["status_critical"], width=2),
                    marker=dict(size=6),
                ),
                secondary_y=True,
            )
            fig_stores.update_layout(
                title=f"Top {top_n} Sucursales — VP vs InStock %",
                height=max(450, top_n * 22),
                margin=dict(l=20, r=20, t=40, b=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                xaxis_tickangle=-45,
            )
            fig_stores.update_yaxes(
                title_text=f"Venta Perdida ({_vp_sym()})", secondary_y=False,
            )
            fig_stores.update_yaxes(
                title_text="InStock %", secondary_y=True,
                range=[0, 105],
            )
            st.plotly_chart(fig_stores, use_container_width=True)

            # Store table with InStock progress
            display_cols_s = [c for c in [
                "ID_SUCURSAL", "DESCRIPCION_SUCURSAL",
                "VP_PESOS", "VP_UNIDADES", "N_SKUS",
                "INSTOCK_PCT", "STOCK_TOTAL", "DEMANDA_TIENDA",
            ] if c in agg_store.columns]

            st.dataframe(
                agg_store[display_cols_s],
                column_config={
                    "ID_SUCURSAL": st.column_config.TextColumn(
                        "Sucursal",
                    ),
                    "DESCRIPCION_SUCURSAL": st.column_config.TextColumn(
                        "Nombre", width="medium",
                    ),
                    "VP_PESOS": st.column_config.NumberColumn(
                        f"VP Acum ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                    ),
                    "VP_UNIDADES": st.column_config.NumberColumn(
                        "VP Acum (Und)", format="%.0f",
                    ),
                    "N_SKUS": st.column_config.NumberColumn(
                        "SKUs", format="%d",
                    ),
                    "INSTOCK_PCT": st.column_config.ProgressColumn(
                        "InStock %",
                        format="%.1f%%",
                        min_value=0,
                        max_value=100,
                    ),
                    "STOCK_TOTAL": st.column_config.NumberColumn(
                        "Stock Total", format="%.0f",
                    ),
                    "DEMANDA_TIENDA": st.column_config.NumberColumn(
                        "Demanda Total", format="%.0f",
                    ),
                },
                use_container_width=True, height=500, hide_index=True,
            )
            download_buttons(
                agg_store[display_cols_s], "venta_perdida_por_sucursal",
            )

    # ── Tab CD ──
    with tab_cd:
        if df_cd.empty:
            st.warning(
                "No se encontro venta perdida en CD para este rango."
            )
        else:
            # Aggregate by SKU × Canal
            agg_cols_cd = {"VP_PESOS": "sum", "VP_UNIDADES": "sum"}
            for c in [
                "NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA",
                "MARCA", "MIX_OFICIAL", "PRODUCTO_STATUS",
            ]:
                if c in df_cd.columns:
                    agg_cols_cd[c] = "first"
            if "INSTOCK_CD" in df_cd.columns:
                agg_cols_cd["INSTOCK_CD"] = "mean"

            df_sku_c = (
                df_cd.groupby(["SKU_PRODUCTO", "CANAL"])
                .agg(**{k: (k, v) for k, v in agg_cols_cd.items()})
                .reset_index()
                .sort_values("VP_PESOS", ascending=False)
            )

            # Convert instock to pct
            if "INSTOCK_CD" in df_sku_c.columns:
                df_sku_c["IS_CD_PCT"] = df_sku_c["INSTOCK_CD"] * 100

            # Section KPIs
            st.html(_hdr("📦 VP Centro de Distribucion"))
            vp_mayor = (
                df_sku_c.loc[
                    df_sku_c["CANAL"].isin(["MAYOR", "MAYORISTA"]),
                    "VP_PESOS",
                ].sum() if "CANAL" in df_sku_c.columns else 0
            )
            vp_etail = (
                df_sku_c.loc[
                    df_sku_c["CANAL"] == "ETAIL", "VP_PESOS"
                ].sum() if "CANAL" in df_sku_c.columns else 0
            )
            kc1, kc2, kc3 = st.columns(3)
            with kc1:
                st.html(simple_kpi_card(
                    label="VP CD TOTAL",
                    value=_fmt_currency(vp_c),
                    accent_color=COLORS["tertiary_teal"],
                ))
            with kc2:
                st.html(simple_kpi_card(
                    label="VP MAYOR",
                    value=_fmt_currency(vp_mayor),
                    accent_color=COLORS["primary"],
                ))
            with kc3:
                st.html(simple_kpi_card(
                    label="VP ETAIL",
                    value=_fmt_currency(vp_etail),
                    accent_color=COLORS["secondary"],
                ))

            # Charts
            cc1, cc2 = st.columns(2)
            with cc1:
                fig = _chart_canal_donut(df_sku_c)
                if fig:
                    st.plotly_chart(fig, use_container_width=True)
            with cc2:
                fig = _chart_top_skus(
                    df_sku_c, title="Top 20 SKUs — VP CD",
                )
                if fig:
                    st.plotly_chart(fig, use_container_width=True)

            # Table
            display_cols = [c for c in [
                "SKU_PRODUCTO", "NOM_PRODUCTO", "CANAL",
                "AREA", "LINEA", "SUBLINEA", "MARCA",
                "MIX_OFICIAL", "PRODUCTO_STATUS",
                "VP_PESOS", "VP_UNIDADES",
                "IS_CD_PCT",
            ] if c in df_sku_c.columns]

            col_cfg_cd = {
                "SKU_PRODUCTO": st.column_config.TextColumn("SKU"),
                "NOM_PRODUCTO": st.column_config.TextColumn(
                    "Producto", width="large",
                ),
                "PRODUCTO_STATUS": st.column_config.TextColumn(
                    "Status",
                    help="ESTABLECIDO / NUEVO / SIN VENTAS",
                ),
                "VP_PESOS": st.column_config.NumberColumn(
                    f"VP Acum ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                ),
                "VP_UNIDADES": st.column_config.NumberColumn(
                    "VP Acum (Und)", format="%.0f",
                ),
            }
            if "IS_CD_PCT" in display_cols:
                col_cfg_cd["IS_CD_PCT"] = st.column_config.ProgressColumn(
                    "IS CD %",
                    format="%.1f%%",
                    min_value=0,
                    max_value=100,
                )

            st.dataframe(
                df_sku_c[display_cols].head(500),
                column_config=col_cfg_cd,
                use_container_width=True, height=500, hide_index=True,
            )
            download_buttons(
                df_sku_c[display_cols], "venta_perdida_cd",
            )

    # ── Tab Insights ──
    with tab_insights:
        st.html(_hdr("💡 Insights — Venta Perdida por Canal y Tipo"))

        # Use FILTERED detail (same as KPIs: grace + CD filter applied)
        # so Quiebre/Repo numbers match the top summary exactly.
        df_det_ins = _apply_filters(
            df_detail, f_sku, f_area, f_linea,
            f_sublinea, f_marca, f_proveedor, f_status,
        ) if not df_detail.empty else pd.DataFrame()
        df_cd_data = df_cd  # already dimension-filtered

        if df_det_ins.empty and df_cd_data.empty:
            st.warning("No hay datos para generar insights.")
        else:
            # ── Section A: Composicion VP por Canal y Tipo ──
            st.html(_hdr("Composicion VP por Canal y Tipo"))

            # VP Tiendas split
            _vp_qp = 0.0
            _vp_rep = 0.0
            if (not df_det_ins.empty
                    and "TIPO_VP" in df_det_ins.columns):
                _vp_qp = float(df_det_ins.loc[
                    df_det_ins["TIPO_VP"] == "QUIEBRE_PRODUCTO",
                    "VP_PESOS",
                ].sum())
                _vp_rep = float(df_det_ins.loc[
                    df_det_ins["TIPO_VP"] == "REPOSICION",
                    "VP_PESOS",
                ].sum())

            # VP CD split by channel
            _vp_mayor = 0.0
            _vp_etail = 0.0
            if (not df_cd_data.empty
                    and "CANAL" in df_cd_data.columns):
                _vp_mayor = float(df_cd_data.loc[
                    df_cd_data["CANAL"].isin(["MAYOR", "MAYORISTA"]),
                    "VP_PESOS",
                ].sum())
                _vp_etail = float(df_cd_data.loc[
                    df_cd_data["CANAL"] == "ETAIL",
                    "VP_PESOS",
                ].sum())

            categories = [
                "Quiebre Producto", "Reposicion",
                "CD Mayor", "CD Etail",
            ]
            values = [_vp_qp, _vp_rep, _vp_mayor, _vp_etail]
            bar_colors = [
                "#8B0000",
                COLORS.get("status_at_risk", "#F5A623"),
                COLORS.get("primary", "#065E8B"),
                COLORS.get("accent", "#23CED3"),
            ]

            ci1, ci2 = st.columns(2)
            with ci1:
                fig_comp = go.Figure(go.Bar(
                    x=values, y=categories,
                    orientation="h",
                    marker_color=bar_colors,
                    text=[_fmt_currency(v) for v in values],
                    textposition="outside",
                ))
                fig_comp.update_layout(
                    title=f"VP por Canal y Tipo ({_vp_sym()})",
                    height=300,
                    margin=dict(l=20, r=100, t=40, b=20),
                )
                st.plotly_chart(fig_comp, use_container_width=True)
            with ci2:
                non_zero = [
                    (c, v) for c, v in zip(categories, values)
                    if v > 0
                ]
                if non_zero:
                    fig_pie = go.Figure(go.Pie(
                        labels=[c for c, _ in non_zero],
                        values=[v for _, v in non_zero],
                        hole=0.45,
                        marker_colors=[
                            bar_colors[categories.index(c)]
                            for c, _ in non_zero
                        ],
                        textinfo="label+percent",
                    ))
                    fig_pie.update_layout(
                        title="Distribucion VP",
                        height=350,
                        margin=dict(l=10, r=10, t=40, b=10),
                    )
                    st.plotly_chart(fig_pie, use_container_width=True)

            # ── Section B: Top SKUs por Canal ──
            st.html(_hdr("Top SKUs por Canal"))

            sub_tabs = st.tabs([
                "🔴 Quiebre Producto",
                "🟠 Reposicion",
                "📦 CD Mayor",
                "🌐 CD Etail",
            ])

            with sub_tabs[0]:
                if (not df_det_ins.empty
                        and "TIPO_VP" in df_det_ins.columns):
                    _df_qp = df_det_ins[
                        df_det_ins["TIPO_VP"] == "QUIEBRE_PRODUCTO"
                    ]
                    if not _df_qp.empty:
                        _agg = {"VP_PESOS": "sum", "VP_UNIDADES": "sum"}
                        for _c in ["NOM_PRODUCTO", "AREA", "LINEA",
                                   "MARCA"]:
                            if _c in _df_qp.columns:
                                _agg[_c] = "first"
                        _top_qp = (
                            _df_qp.groupby("SKU_PRODUCTO")
                            .agg(**{k: (k, v) for k, v in _agg.items()})
                            .reset_index()
                            .sort_values("VP_PESOS", ascending=False)
                        )
                        fig = _chart_top_skus(
                            _top_qp, n=15,
                            title="Top 15 SKUs — Quiebre de Producto",
                        )
                        if fig:
                            st.plotly_chart(fig, use_container_width=True)
                        st.dataframe(
                            _top_qp.head(100),
                            column_config={
                                "SKU_PRODUCTO": st.column_config.TextColumn(
                                    "SKU",
                                ),
                                "NOM_PRODUCTO": st.column_config.TextColumn(
                                    "Producto", width="large",
                                ),
                                "VP_PESOS": st.column_config.NumberColumn(
                                    f"VP ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                                ),
                                "VP_UNIDADES": st.column_config.NumberColumn(
                                    "VP (Und)", format="%.0f",
                                ),
                            },
                            use_container_width=True, hide_index=True,
                        )
                    else:
                        st.info("No hay VP por quiebre de producto.")
                else:
                    st.info("No hay datos de detalle disponibles.")

            with sub_tabs[1]:
                if (not df_det_ins.empty
                        and "TIPO_VP" in df_det_ins.columns):
                    _df_rep = df_det_ins[
                        df_det_ins["TIPO_VP"] == "REPOSICION"
                    ]
                    if not _df_rep.empty:
                        _agg = {"VP_PESOS": "sum", "VP_UNIDADES": "sum"}
                        for _c in ["NOM_PRODUCTO", "AREA", "LINEA",
                                   "MARCA"]:
                            if _c in _df_rep.columns:
                                _agg[_c] = "first"
                        _top_rep = (
                            _df_rep.groupby("SKU_PRODUCTO")
                            .agg(**{k: (k, v) for k, v in _agg.items()})
                            .reset_index()
                            .sort_values("VP_PESOS", ascending=False)
                        )
                        fig = _chart_top_skus(
                            _top_rep, n=15,
                            title="Top 15 SKUs — Reposicion",
                        )
                        if fig:
                            st.plotly_chart(fig, use_container_width=True)
                        st.dataframe(
                            _top_rep.head(100),
                            column_config={
                                "SKU_PRODUCTO": st.column_config.TextColumn(
                                    "SKU",
                                ),
                                "NOM_PRODUCTO": st.column_config.TextColumn(
                                    "Producto", width="large",
                                ),
                                "VP_PESOS": st.column_config.NumberColumn(
                                    f"VP ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                                ),
                                "VP_UNIDADES": st.column_config.NumberColumn(
                                    "VP (Und)", format="%.0f",
                                ),
                            },
                            use_container_width=True, hide_index=True,
                        )
                    else:
                        st.info("No hay VP por reposicion.")
                else:
                    st.info("No hay datos de detalle disponibles.")

            with sub_tabs[2]:
                if (not df_cd_data.empty
                        and "CANAL" in df_cd_data.columns):
                    _df_may = df_cd_data[
                        df_cd_data["CANAL"].isin(["MAYOR", "MAYORISTA"])
                    ]
                    if not _df_may.empty:
                        _agg = {"VP_PESOS": "sum", "VP_UNIDADES": "sum"}
                        for _c in ["NOM_PRODUCTO", "AREA", "LINEA",
                                   "MARCA"]:
                            if _c in _df_may.columns:
                                _agg[_c] = "first"
                        _top_may = (
                            _df_may.groupby("SKU_PRODUCTO")
                            .agg(**{k: (k, v) for k, v in _agg.items()})
                            .reset_index()
                            .sort_values("VP_PESOS", ascending=False)
                        )
                        fig = _chart_top_skus(
                            _top_may, n=15,
                            title="Top 15 SKUs — CD Mayor",
                        )
                        if fig:
                            st.plotly_chart(fig, use_container_width=True)
                        st.dataframe(
                            _top_may.head(100),
                            column_config={
                                "SKU_PRODUCTO": st.column_config.TextColumn(
                                    "SKU",
                                ),
                                "NOM_PRODUCTO": st.column_config.TextColumn(
                                    "Producto", width="large",
                                ),
                                "VP_PESOS": st.column_config.NumberColumn(
                                    f"VP ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                                ),
                                "VP_UNIDADES": st.column_config.NumberColumn(
                                    "VP (Und)", format="%.0f",
                                ),
                            },
                            use_container_width=True, hide_index=True,
                        )
                    else:
                        st.info("No hay VP CD Mayor.")
                else:
                    st.info("No hay datos de CD disponibles.")

            with sub_tabs[3]:
                if (not df_cd_data.empty
                        and "CANAL" in df_cd_data.columns):
                    _df_eta = df_cd_data[
                        df_cd_data["CANAL"] == "ETAIL"
                    ]
                    if not _df_eta.empty:
                        _agg = {"VP_PESOS": "sum", "VP_UNIDADES": "sum"}
                        for _c in ["NOM_PRODUCTO", "AREA", "LINEA",
                                   "MARCA"]:
                            if _c in _df_eta.columns:
                                _agg[_c] = "first"
                        _top_et = (
                            _df_eta.groupby("SKU_PRODUCTO")
                            .agg(**{k: (k, v) for k, v in _agg.items()})
                            .reset_index()
                            .sort_values("VP_PESOS", ascending=False)
                        )
                        fig = _chart_top_skus(
                            _top_et, n=15,
                            title="Top 15 SKUs — CD Etail",
                        )
                        if fig:
                            st.plotly_chart(fig, use_container_width=True)
                        st.dataframe(
                            _top_et.head(100),
                            column_config={
                                "SKU_PRODUCTO": st.column_config.TextColumn(
                                    "SKU",
                                ),
                                "NOM_PRODUCTO": st.column_config.TextColumn(
                                    "Producto", width="large",
                                ),
                                "VP_PESOS": st.column_config.NumberColumn(
                                    f"VP ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                                ),
                                "VP_UNIDADES": st.column_config.NumberColumn(
                                    "VP (Und)", format="%.0f",
                                ),
                            },
                            use_container_width=True, hide_index=True,
                        )
                    else:
                        st.info("No hay VP CD Etail.")
                else:
                    st.info("No hay datos de CD disponibles.")

            # ── Section C: SKUs Quebrados en CD + Proxima ETA ──
            st.html(_hdr(
                "SKUs con Quiebre CD — Proxima ETA"
            ))
            st.caption(
                "SKUs que tienen venta perdida por quiebre de producto "
                "(CD sin stock) y sus proximas ETAs de reposicion "
                "programadas."
            )

            if (not df_det_ins.empty
                    and "TIPO_VP" in df_det_ins.columns):
                skus_quiebre = (
                    df_det_ins[
                        df_det_ins["TIPO_VP"] == "QUIEBRE_PRODUCTO"
                    ]
                    .groupby("SKU_PRODUCTO")
                    .agg(
                        VP_PESOS=("VP_PESOS", "sum"),
                        VP_UNIDADES=("VP_UNIDADES", "sum"),
                        DIAS_QUIEBRE=("FECHA", "nunique"),
                    )
                    .reset_index()
                    .sort_values("VP_PESOS", ascending=False)
                )

                if not skus_quiebre.empty:
                    # Normalize SKU values before merge
                    skus_quiebre["SKU_PRODUCTO"] = (
                        skus_quiebre["SKU_PRODUCTO"].astype(str).str.strip()
                    )

                    # Load ETA data
                    try:
                        df_eta_data = _load_eta_pendiente(conn)
                    except Exception:
                        df_eta_data = pd.DataFrame()

                    # Merge
                    skus_eta = skus_quiebre.merge(
                        df_eta_data, on="SKU_PRODUCTO", how="left",
                    )

                    # Enrich with maestra
                    skus_eta = _enrich_with_maestra(skus_eta, conn)

                    # Days until ETA
                    if "PROXIMA_ETA" in skus_eta.columns:
                        skus_eta["DIAS_HASTA_ETA"] = (
                            skus_eta["PROXIMA_ETA"]
                            - pd.Timestamp.now()
                        ).dt.days
                        skus_eta["TIENE_ETA"] = (
                            skus_eta["PROXIMA_ETA"].notna()
                        )
                    else:
                        skus_eta["DIAS_HASTA_ETA"] = np.nan
                        skus_eta["TIENE_ETA"] = False

                    # KPIs
                    n_con_eta = int(skus_eta["TIENE_ETA"].sum())
                    n_sin_eta = int((~skus_eta["TIENE_ETA"]).sum())
                    vp_sin_eta = float(
                        skus_eta.loc[
                            ~skus_eta["TIENE_ETA"], "VP_PESOS"
                        ].sum()
                    )

                    ke1, ke2, ke3 = st.columns(3)
                    with ke1:
                        st.html(simple_kpi_card(
                            label="SKUS QUIEBRE CON ETA",
                            value=f"{n_con_eta}",
                            accent_color=COLORS["status_on_track"],
                            subtitle="Reposicion en camino",
                        ))
                    with ke2:
                        st.html(simple_kpi_card(
                            label="SKUS QUIEBRE SIN ETA",
                            value=f"{n_sin_eta}",
                            accent_color=COLORS["status_critical"],
                            subtitle=f"VP: {_fmt_currency(vp_sin_eta)}",
                        ))
                    with ke3:
                        _dias_eta_vals = skus_eta.loc[
                            skus_eta["TIENE_ETA"], "DIAS_HASTA_ETA"
                        ]
                        avg_dias = (
                            _dias_eta_vals.mean()
                            if len(_dias_eta_vals) > 0
                            else float("nan")
                        )
                        st.html(simple_kpi_card(
                            label="DIAS PROM HASTA ETA",
                            value=(
                                f"{avg_dias:.0f}"
                                if not np.isnan(avg_dias) else "N/A"
                            ),
                            accent_color=COLORS["primary"],
                        ))

                    # Table
                    _eta_cols = [c for c in [
                        "SKU_PRODUCTO", "NOM_PRODUCTO",
                        "AREA", "LINEA", "MARCA",
                        "VP_PESOS", "VP_UNIDADES", "DIAS_QUIEBRE",
                        "TIENE_ETA", "PROXIMA_ETA", "QTY_PENDIENTE",
                        "N_POS", "PROVEEDOR_ETA", "DIAS_HASTA_ETA",
                    ] if c in skus_eta.columns]

                    st.dataframe(
                        skus_eta[_eta_cols].head(500),
                        column_config={
                            "SKU_PRODUCTO": st.column_config.TextColumn(
                                "SKU",
                            ),
                            "NOM_PRODUCTO": st.column_config.TextColumn(
                                "Producto", width="large",
                            ),
                            "VP_PESOS": st.column_config.NumberColumn(
                                f"VP Quiebre ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                            ),
                            "VP_UNIDADES": st.column_config.NumberColumn(
                                "VP Quiebre (Und)", format="%.0f",
                            ),
                            "DIAS_QUIEBRE": st.column_config.NumberColumn(
                                "Dias Quiebre", format="%d",
                            ),
                            "TIENE_ETA": st.column_config.CheckboxColumn(
                                "Tiene ETA",
                            ),
                            "PROXIMA_ETA": st.column_config.DateColumn(
                                "Proxima ETA",
                            ),
                            "QTY_PENDIENTE": st.column_config.NumberColumn(
                                "Qty Pendiente", format="%.0f",
                            ),
                            "N_POS": st.column_config.NumberColumn(
                                "N POs", format="%d",
                            ),
                            "PROVEEDOR_ETA": st.column_config.TextColumn(
                                "Proveedor",
                            ),
                            "DIAS_HASTA_ETA": st.column_config.NumberColumn(
                                "Dias Hasta ETA", format="%d",
                            ),
                        },
                        use_container_width=True,
                        height=600,
                        hide_index=True,
                    )
                    download_buttons(
                        skus_eta[_eta_cols], "vp_quiebre_eta",
                    )
                else:
                    st.info(
                        "No hay SKUs con quiebre de producto en el "
                        "rango seleccionado."
                    )
            else:
                st.warning(
                    "No hay datos de detalle para generar insights "
                    "de quiebre."
                )

    # ── Tab Detalle Calculo ──
    with tab_detalle:
        # Status filter — always visible at top of tab
        _det_status = st.multiselect(
            "Filtrar Status del Producto",
            ["NUEVO", "ESTABLECIDO", "SIN VENTAS"],
            default=[],
            key="vp_det_status_inline",
            help="NUEVO = primera venta dentro del periodo  |  "
                 "ESTABLECIDO = ya tenia ventas antes del periodo  |  "
                 "SIN VENTAS = SKU sin historial de ventas",
        )

        if df_detail.empty:
            st.warning(
                "No hay datos de detalle para mostrar. "
                "Ejecute el calculo primero."
            )
        else:
            st.html(_hdr("📋 Sabana de Detalle — SKU × Sucursal × Dia"))
            st.caption(
                "Muestra cada combinacion SKU × Sucursal × Dia con los "
                "valores intermedios del calculo: stock, demanda por tienda, "
                "flag instock (1=abastecido, 0=quiebre), VP, precio usado."
            )

            # Apply dimension filters to detail too
            df_det = _apply_filters(
                df_detail, f_sku, f_area, f_linea,
                f_sublinea, f_marca, f_proveedor, f_status,
            )
            # Apply inline status filter
            if _det_status and "PRODUCTO_STATUS" in df_det.columns:
                df_det = df_det[df_det["PRODUCTO_STATUS"].isin(_det_status)]

            # Summary KPIs for the detail
            n_rows = len(df_det)
            n_instock = (
                df_det["INSTOCK"].sum()
                if "INSTOCK" in df_det.columns else 0
            )
            n_total = len(df_det)
            is_pct_detail = (
                n_instock / n_total * 100 if n_total > 0 else 0
            )

            kd1, kd2, kd3, kd4 = st.columns(4)
            with kd1:
                st.html(simple_kpi_card(
                    label="REGISTROS DETALLE",
                    value=f"{n_rows:,}",
                    accent_color=COLORS["primary"],
                ))
            with kd2:
                is_color = (
                    COLORS["status_on_track"] if is_pct_detail >= 93
                    else COLORS["status_at_risk"] if is_pct_detail >= 85
                    else COLORS["status_critical"]
                )
                st.html(simple_kpi_card(
                    label="INSTOCK % GLOBAL",
                    value=f"{is_pct_detail:.1f}%",
                    accent_color=is_color,
                    subtitle=(
                        f"{int(n_instock):,} de {n_total:,} "
                        f"combinaciones SKU×Tienda×Dia"
                    ),
                ))
            with kd3:
                avg_demand = (
                    df_det["DEMANDA_POR_TIENDA"].mean()
                    if "DEMANDA_POR_TIENDA" in df_det.columns else 0
                )
                st.html(simple_kpi_card(
                    label="DEMANDA PROM/TIENDA/DIA",
                    value=f"{avg_demand:.2f} und",
                    accent_color=COLORS["tertiary_teal"],
                ))
            with kd4:
                avg_stock = (
                    df_det["STOCK_UNIDADES"].mean()
                    if "STOCK_UNIDADES" in df_det.columns else 0
                )
                st.html(simple_kpi_card(
                    label="STOCK PROM/TIENDA/DIA",
                    value=f"{avg_stock:.1f} und",
                    accent_color=COLORS["status_at_risk"],
                ))

            # ── Daily detail table — SKU × Sucursal × Dia ──
            st.html(_hdr("📋 Detalle Diario — SKU × Sucursal × Dia"))

            st.caption(
                f"Cada fila es un dia especifico × SKU × tienda. "
                f"Total: **{len(df_det):,}** registros. "
                f"Mostrando hasta 5,000 filas ordenadas por VP ({_vp_sym()}) desc."
            )

            raw_display = [c for c in [
                "FECHA", "SKU_PRODUCTO", "NOM_PRODUCTO",
                "ID_SUCURSAL", "DESCRIPCION_SUCURSAL",
                "AREA", "LINEA", "MARCA", "MIX_OFICIAL",
                "PRODUCTO_STATUS", "TIPO_VP",
                "STOCK_UNIDADES", "STOCK_CD",
                "DAILY_DEMAND_ALL", "INSTOCK_CD",
                "DEMANDA_TOTAL_DIA",
                "N_TIENDAS_PERFIL", "DEMANDA_POR_TIENDA",
                "INSTOCK", "VP_UNIDADES", "VP_PESOS",
                "PRECIO_VCM", "ULTIMO_COSTO", "PRECIO_USADO",
            ] if c in df_det.columns]

            df_det_sorted = df_det.sort_values(
                "VP_PESOS", ascending=False,
            )

            st.dataframe(
                df_det_sorted[raw_display].head(5000),
                column_config={
                    "FECHA": st.column_config.DateColumn(
                        "Fecha", format="YYYY-MM-DD",
                    ),
                    "SKU_PRODUCTO": st.column_config.TextColumn("SKU"),
                    "NOM_PRODUCTO": st.column_config.TextColumn(
                        "Producto", width="medium",
                    ),
                    "ID_SUCURSAL": st.column_config.TextColumn("Sucursal"),
                    "DESCRIPCION_SUCURSAL": st.column_config.TextColumn(
                        "Nombre Suc.",
                    ),
                    "PRODUCTO_STATUS": st.column_config.TextColumn(
                        "Status",
                        help="ESTABLECIDO / NUEVO / SIN VENTAS",
                    ),
                    "TIPO_VP": st.column_config.TextColumn(
                        "Tipo VP",
                        help="QUIEBRE_PRODUCTO = CD sin stock; "
                             "REPOSICION = CD tenia stock; "
                             "SIN_VP = sin venta perdida",
                    ),
                    "STOCK_UNIDADES": st.column_config.NumberColumn(
                        "Stock Tienda", format="%.0f",
                        help="Stock en esa tienda ese dia",
                    ),
                    "STOCK_CD": st.column_config.NumberColumn(
                        "Stock CD", format="%.0f",
                        help="Stock en CD ese dia para este SKU",
                    ),
                    "DAILY_DEMAND_ALL": st.column_config.NumberColumn(
                        "Dda Diaria Cia", format="%.2f",
                        help="Demanda diaria promedio todos canales "
                             "(VCM, ventana 2 meses)",
                    ),
                    "INSTOCK_CD": st.column_config.NumberColumn(
                        "IS CD", format="%d",
                        help="1 = stock CD >= demanda diaria cia, 0 = no",
                    ),
                    "DEMANDA_TOTAL_DIA": st.column_config.NumberColumn(
                        "Dda Total Dia", format="%.2f",
                        help="Demanda total dia (VCM canal tienda)",
                    ),
                    "N_TIENDAS_PERFIL": st.column_config.NumberColumn(
                        "N Tiendas", format="%d",
                        help="Tiendas con perfil para este SKU",
                    ),
                    "DEMANDA_POR_TIENDA": st.column_config.NumberColumn(
                        "Dda/Tienda/Dia", format="%.4f",
                        help="Demanda VCM total / n_tiendas_perfil",
                    ),
                    "INSTOCK": st.column_config.NumberColumn(
                        "InStock", format="%d",
                        help="1 = stock tienda >= demanda por tienda, "
                             "0 = quiebre",
                    ),
                    "VP_UNIDADES": st.column_config.NumberColumn(
                        "VP Und", format="%.4f",
                    ),
                    "VP_PESOS": st.column_config.NumberColumn(
                        f"VP ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                    ),
                    "PRECIO_VCM": st.column_config.NumberColumn(
                        "Precio VCM", format=f"{_vp_sym()} %.0f",
                        help="Precio promedio VCM (neto/cantidad)",
                    ),
                    "ULTIMO_COSTO": st.column_config.NumberColumn(
                        "Ult. Costo", format=f"{_vp_sym()} %.0f",
                        help="Ultimo costo de vw_producto (fallback)",
                    ),
                    "PRECIO_USADO": st.column_config.NumberColumn(
                        "Precio Usado", format=f"{_vp_sym()} %.0f",
                        help="Precio usado: VCM si >0, sino ultimo_costo",
                    ),
                },
                use_container_width=True, height=700, hide_index=True,
            )

            download_buttons(
                df_det_sorted[raw_display], "vp_detalle_diario",
            )

    # ── Tab Resumen ──
    with tab_resumen:
        # ── Cuadro Resumen: Canal × Mes + Tienda breakdown ──
        _render_channel_month_summary(df_tienda, df_cd, df_detail)

        df_all = pd.concat(
            [
                df_tienda.assign(ORIGEN="TIENDA")
                if not df_tienda.empty else pd.DataFrame(),
                df_cd.assign(ORIGEN="CD")
                if not df_cd.empty else pd.DataFrame(),
            ],
            ignore_index=True,
        )
        if df_all.empty:
            st.warning("No hay datos para el resumen.")
        else:
            st.html(_hdr("📊 Resumen por Dimension"))

            for dim_name, dim_cols in [
                ("Area", ["AREA"]),
                ("Linea", ["AREA", "LINEA"]),
                ("Marca", ["MARCA"]),
            ]:
                if all(c in df_all.columns for c in dim_cols):
                    st.markdown(f"**VP por {dim_name}**")
                    agg = (
                        df_all.groupby(dim_cols)
                        .agg(
                            VP_PESOS=("VP_PESOS", "sum"),
                            VP_UNIDADES=("VP_UNIDADES", "sum"),
                            SKUS=("SKU_PRODUCTO", "nunique"),
                        )
                        .reset_index()
                        .sort_values("VP_PESOS", ascending=False)
                    )
                    st.dataframe(
                        agg,
                        column_config={
                            "VP_PESOS": st.column_config.NumberColumn(
                                f"VP ({_vp_sym()})", format=f"{_vp_sym()} %.0f",
                            ),
                            "VP_UNIDADES": st.column_config.NumberColumn(
                                "VP (Und)", format="%.0f",
                            ),
                            "SKUS": st.column_config.NumberColumn(
                                "SKUs", format="%d",
                            ),
                        },
                        use_container_width=True, hide_index=True,
                    )
            download_buttons(df_all, "venta_perdida_resumen")
