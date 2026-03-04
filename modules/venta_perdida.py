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

from config import COLORS, apply_pm_filter
from db.queries import QUERY_MAESTRA, _VCM, _INSTOCK, _INSTOCK_CD, _PROD
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
    df = pd.read_sql(sql, _conn, params=[str(lookback_start), str(range_end)])
    df = norm_cols(df)
    if "RECOVERY_DATE" in df.columns:
        df["RECOVERY_DATE"] = pd.to_datetime(df["RECOVERY_DATE"])
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
                       filter_cd_instock="stock_gt_0"):
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

    Params (positional %s, 4 or 8 total):
        demand_start, demand_end        (demand CTE — per store)
      [if filter_cd_instock != "none"]:
        demand_start, demand_end        (demand_all CTE)
        stock_start, stock_end          (stock_daily CTE)
      [if filter_cd_instock != "none"]:
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
    perfil_sql = "AND a.perfil = 'SI'" if perfil_only else ""

    eligible_cte = ""
    if has_mix:
        eligible_cte = f"""eligible_skus AS (
        SELECT DISTINCT p.sku_producto
        FROM {_PROD} p
        WHERE {mix_clause}
    ),
    """

    # Hardcoded closed-store exclusion
    # Double %% to escape Python %-formatting used by Snowflake connector
    closed_stores_sql = (
        "AND UPPER(COALESCE(b.descripcion_sucursal, '')) "
        "NOT LIKE '%%BELLAVISTA%%' "
        "AND UPPER(COALESCE(b.descripcion_sucursal, '')) "
        "NOT LIKE '%%CHICLAYO 2%%' "
        "AND UPPER(COALESCE(b.descripcion_sucursal, '')) "
        "NOT LIKE '%%CHICLAYO2%%' "
        "AND UPPER(COALESCE(b.descripcion_sucursal, '')) "
        "NOT LIKE '%%SAN MIGUEL 2%%' "
        "AND UPPER(COALESCE(b.descripcion_sucursal, '')) "
        "NOT LIKE '%%SANMIGUEL2%%' "
        "AND UPPER(COALESCE(b.descripcion_sucursal, '')) "
        "NOT LIKE '%%OUTLET%%' "
        "AND UPPER(COALESCE(b.descripcion_sucursal, '')) "
        "NOT LIKE '%%CAJAMARCA%%' "
        "AND UPPER(COALESCE(b.descripcion_sucursal, '')) "
        "NOT LIKE '%%TRUJILLO 2%%' "
        "AND UPPER(COALESCE(b.descripcion_sucursal, '')) "
        "NOT LIKE '%%TRUJILLO2%%' "
        "AND CAST(b.id_sucursal AS VARCHAR) "
        "NOT IN ('143', '148')"
    )

    return f"""
    WITH
    {eligible_cte}
    prod_price AS (
        SELECT p.sku_producto, MAX(p.ultimo_costo) AS ultimo_costo
        FROM {_PROD} p
        {mix_join_p}
        GROUP BY 1
    ),
    demand AS (
        SELECT
            v.sku_producto,
            COALESCE(CAST(b.id_sucursal AS VARCHAR),
                     CAST(v.cod_ccosto AS VARCHAR)) AS id_sucursal,
            SUM(v.cantidad) / NULLIF({dias_ventana}::FLOAT, 0)
                AS demand_per_store,
            CASE WHEN SUM(v.cantidad) > 0
                 THEN SUM(v.neto) / SUM(v.cantidad)
                 ELSE 0 END AS avg_price
        FROM {_VCM} v
        LEFT JOIN db_syncros.public.coo_maestro_sucursal b
            ON v.cod_ccosto = b.id_sucursal
        {mix_join_v}
        WHERE v.fecha >= %s AND v.fecha <= %s
          AND v.cantidad > 0
          AND COALESCE(b.canal_de_distribucion, 'TIENDA')
              NOT IN ('CD', 'MAYOR', 'ETAIL', 'MAYORISTA')
          {closed_stores_sql}
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
        GROUP BY 1
    ),""" if filter_cd_instock != "none" else "") + f"""
    stock_daily AS (
        SELECT
            a.fecha,
            a.sku_producto,
            COALESCE(CAST(b.id_sucursal AS VARCHAR),
                     CAST(al.cod_ccosto AS VARCHAR),
                     CAST(a.cod_bodega AS VARCHAR)) AS id_sucursal,
            MAX(COALESCE(b.descripcion_sucursal,
                         al.nom_almacen,
                         CAST(a.cod_bodega AS VARCHAR)))
                AS descripcion_sucursal,
            SUM(a.stock_unidades) AS stock_unidades
        FROM {_INSTOCK} a
        LEFT JOIN (
            SELECT cod_almacen,
                   MAX(cod_ccosto) AS cod_ccosto,
                   MAX(nom_almacen) AS nom_almacen
            FROM db_dimensiones.dim.dt_almacen
            GROUP BY cod_almacen
        ) al ON a.cod_bodega = al.cod_almacen
        LEFT JOIN db_syncros.public.coo_maestro_sucursal b
            ON COALESCE(al.cod_ccosto, a.cod_bodega) = b.id_sucursal
        {mix_join_a}
        WHERE a.fecha >= %s AND a.fecha <= %s
          AND COALESCE(b.canal_de_distribucion, 'TIENDA')
              NOT IN ('CD', 'MAYOR', 'ETAIL', 'MAYORISTA')
          {closed_stores_sql}
          {perfil_sql}
        GROUP BY 1, 2, 3
    ),
    n_perfil AS (
        SELECT fecha, sku_producto, COUNT(DISTINCT id_sucursal) AS n_stores
        FROM stock_daily
        GROUP BY 1, 2
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
    )""" if filter_cd_instock != "none" else "")


def _sql_vp_tienda(dias_ventana, mix_values=None, perfil_only=True):
    """VP per SKU x day (aggregated across stores).

    Always runs WITHOUT cd_instock filter (4 params).
    CD filter is applied post-hoc in Python via detail data.
    """
    cte = _build_tienda_ctes(dias_ventana, mix_values, perfil_only,
                             filter_cd_instock="none")
    return cte + f"""
    SELECT
        s.fecha,
        s.sku_producto,
        SUM(GREATEST(0,
            d.demand_per_store - s.stock_unidades
        )) AS vp_unidades,
        SUM(GREATEST(0,
            d.demand_per_store - s.stock_unidades
        ) * COALESCE(NULLIF(d.avg_price, 0), pp.ultimo_costo, 0))
            AS vp_pesos,
        COUNT(*) AS n_tiendas_total,
        SUM(CASE
            WHEN s.stock_unidades >= d.demand_per_store
            THEN 1 ELSE 0 END) AS n_tiendas_instock,
        SUM(CASE
            WHEN s.stock_unidades < d.demand_per_store
            THEN 1 ELSE 0 END) AS n_tiendas_oos
    FROM stock_daily s
    INNER JOIN demand d
        ON s.sku_producto = d.sku_producto
        AND s.id_sucursal = d.id_sucursal
    LEFT JOIN prod_price pp
        ON s.sku_producto = pp.sku_producto
    GROUP BY 1, 2
    """


def _sql_vp_tienda_by_store(dias_ventana, mix_values=None, perfil_only=True):
    """VP per store x day (aggregated across SKUs).

    Always runs WITHOUT cd_instock filter (4 params).
    """
    cte = _build_tienda_ctes(dias_ventana, mix_values, perfil_only,
                             filter_cd_instock="none")
    return cte + f"""
    SELECT
        s.fecha,
        s.id_sucursal,
        MAX(s.descripcion_sucursal) AS descripcion_sucursal,
        SUM(GREATEST(0,
            d.demand_per_store - s.stock_unidades
        )) AS vp_unidades,
        SUM(GREATEST(0,
            d.demand_per_store - s.stock_unidades
        ) * COALESCE(NULLIF(d.avg_price, 0), pp.ultimo_costo, 0))
            AS vp_pesos,
        COUNT(DISTINCT s.sku_producto) AS n_skus,
        SUM(s.stock_unidades) AS stock_total,
        SUM(d.demand_per_store) AS demanda_tienda,
        SUM(CASE
            WHEN s.stock_unidades >= d.demand_per_store
            THEN 1 ELSE 0 END) AS skus_instock,
        COUNT(*) AS skus_total
    FROM stock_daily s
    INNER JOIN demand d
        ON s.sku_producto = d.sku_producto
        AND s.id_sucursal = d.id_sucursal
    LEFT JOIN prod_price pp
        ON s.sku_producto = pp.sku_producto
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
    ) s
    INNER JOIN demand d ON s.sku_producto = d.sku_producto
    INNER JOIN demand_total dt ON s.sku_producto = dt.sku_producto
    LEFT JOIN prod_price pp ON s.sku_producto = pp.sku_producto
    WHERE s.stock_cd < dt.total_demand
    """


def _sql_vp_tienda_detail(dias_ventana, mix_values=None, perfil_only=True,
                          include_cd_cols=True):
    """VP detail per SKU x Store x Day — full calculation audit.

    Returns every intermediate value: stock, demand, demand_per_store,
    instock flag, VP units, VP $, price used.

    When include_cd_cols=True, LEFT JOINs cd_instock to add:
    stock_cd, daily_demand_all, instock_cd (for post-hoc filtering).
    Uses 8 params; otherwise 4 params.
    """
    _mode = "stock_gt_0" if include_cd_cols else "none"
    cte = _build_tienda_ctes(dias_ventana, mix_values, perfil_only,
                             filter_cd_instock=_mode)
    if include_cd_cols:
        cd_join = """
    LEFT JOIN cd_instock ci
        ON s.fecha = ci.fecha AND s.sku_producto = ci.sku_producto"""
        cd_col = "ci.stock_cd, ci.daily_demand_all, ci.instock_cd"
    else:
        cd_join = ""
        cd_col = ("NULL::FLOAT AS stock_cd, NULL::FLOAT AS daily_demand_all, "
                  "NULL::INT AS instock_cd")
    return cte + f"""
    SELECT
        s.fecha,
        s.sku_producto,
        s.id_sucursal,
        s.descripcion_sucursal,
        s.stock_unidades,
        {cd_col},
        dt.total_daily_demand AS demanda_total_dia,
        np.n_stores AS n_tiendas_perfil,
        d.demand_per_store AS demanda_por_tienda,
        CASE
            WHEN s.stock_unidades >= d.demand_per_store
            THEN 1 ELSE 0 END AS instock,
        GREATEST(0,
            d.demand_per_store - s.stock_unidades) AS vp_unidades,
        GREATEST(0,
            d.demand_per_store - s.stock_unidades)
            * COALESCE(NULLIF(d.avg_price, 0), pp.ultimo_costo, 0)
            AS vp_pesos,
        d.avg_price AS precio_vcm,
        pp.ultimo_costo,
        COALESCE(NULLIF(d.avg_price, 0), pp.ultimo_costo, 0)
            AS precio_usado
    FROM stock_daily s
    INNER JOIN demand d
        ON s.sku_producto = d.sku_producto
        AND s.id_sucursal = d.id_sucursal
    LEFT JOIN demand_total dt
        ON s.sku_producto = dt.sku_producto
    LEFT JOIN n_perfil np
        ON s.fecha = np.fecha AND s.sku_producto = np.sku_producto
    LEFT JOIN prod_price pp
        ON s.sku_producto = pp.sku_producto
    {cd_join}
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

    Returns (df_tienda, df_cd, df_by_store, df_detail).
    """
    month_ranges = _split_into_months(stock_start, stock_end)
    all_tienda = []
    all_cd = []
    all_by_store = []
    all_detail = []

    # Decide whether to include CD columns in detail query
    include_cd = filter_cd_instock != "none"

    for i, (ms, me) in enumerate(month_ranges):
        f_ini, f_fin = _get_demand_window(date(ms.year, ms.month, 15))
        dias_ventana = (f_fin - f_ini).days + 1

        # Base params (aggregate queries): demand + stock_daily = 4
        prm_base = [str(f_ini), str(f_fin), str(ms), str(me)]

        # Detail params: demand, [demand_all], stock_daily, [cd_instock]
        prm_detail = [str(f_ini), str(f_fin)]
        if include_cd:
            prm_detail.extend([str(f_ini), str(f_fin)])  # demand_all
        prm_detail.extend([str(ms), str(me)])  # stock_daily
        if include_cd:
            prm_detail.extend([str(ms), str(me)])  # cd_instock

        # CD query: demand + stock = 4
        prm_cd = [str(f_ini), str(f_fin), str(ms), str(me)]

        # ── Tienda VP (SKU level) — always unfiltered ──
        sql_t = _sql_vp_tienda(dias_ventana, mix_values, perfil_only)
        df_t = norm_cols(pd.read_sql(sql_t, conn, params=prm_base))
        if not df_t.empty:
            all_tienda.append(df_t)

        # ── Diagnostic: check store ID overlap (first month only) ──
        if i == 0:
            try:
                _diag_sql = f"""
                SELECT
                    'demand' AS src,
                    COUNT(*) AS n_rows,
                    COUNT(DISTINCT id_sucursal) AS n_stores,
                    LISTAGG(DISTINCT id_sucursal, ', ')
                        WITHIN GROUP (ORDER BY id_sucursal) AS sample_ids
                FROM (
                    SELECT COALESCE(CAST(b.id_sucursal AS VARCHAR),
                                    CAST(v.cod_ccosto AS VARCHAR)) AS id_sucursal
                    FROM {_VCM} v
                    LEFT JOIN db_syncros.public.coo_maestro_sucursal b
                        ON v.cod_ccosto = b.id_sucursal
                    WHERE v.fecha >= %s AND v.fecha <= %s
                      AND v.cantidad > 0
                      AND COALESCE(b.canal_de_distribucion, 'TIENDA')
                          NOT IN ('CD', 'MAYOR', 'ETAIL', 'MAYORISTA')
                )
                UNION ALL
                SELECT
                    'stock (via dt_almacen)' AS src,
                    COUNT(*) AS n_rows,
                    COUNT(DISTINCT id_sucursal) AS n_stores,
                    LISTAGG(DISTINCT id_sucursal, ', ')
                        WITHIN GROUP (ORDER BY id_sucursal) AS sample_ids
                FROM (
                    SELECT COALESCE(CAST(b.id_sucursal AS VARCHAR),
                                    CAST(al.cod_ccosto AS VARCHAR),
                                    CAST(a.cod_bodega AS VARCHAR)) AS id_sucursal
                    FROM {_INSTOCK} a
                    LEFT JOIN (
                        SELECT cod_almacen, MAX(cod_ccosto) AS cod_ccosto
                        FROM db_dimensiones.dim.dt_almacen
                        GROUP BY cod_almacen
                    ) al ON a.cod_bodega = al.cod_almacen
                    LEFT JOIN db_syncros.public.coo_maestro_sucursal b
                        ON COALESCE(al.cod_ccosto, a.cod_bodega) = b.id_sucursal
                    WHERE a.fecha >= %s AND a.fecha <= %s
                      AND COALESCE(b.canal_de_distribucion, 'TIENDA')
                          NOT IN ('CD', 'MAYOR', 'ETAIL', 'MAYORISTA')
                )
                """
                _diag_prm = [str(f_ini), str(f_fin), str(ms), str(me)]
                _df_diag = pd.read_sql(_diag_sql, conn, params=_diag_prm)
                st.session_state["vp_store_diag"] = _df_diag
            except Exception:
                pass

        # ── Tienda VP (store level) — always unfiltered ──
        sql_ts = _sql_vp_tienda_by_store(
            dias_ventana, mix_values, perfil_only,
        )
        df_ts = norm_cols(pd.read_sql(sql_ts, conn, params=prm_base))
        if not df_ts.empty:
            all_by_store.append(df_ts)

        # ── Tienda VP detail — LEFT JOIN cd_instock for columns ──
        sql_td = _sql_vp_tienda_detail(
            dias_ventana, mix_values, perfil_only,
            include_cd_cols=include_cd,
        )
        df_td = norm_cols(pd.read_sql(sql_td, conn, params=prm_detail))
        if not df_td.empty:
            all_detail.append(df_td)

        # ── CD VP ──
        sql_c = _sql_vp_cd(dias_ventana, mix_values)
        df_c = norm_cols(pd.read_sql(sql_c, conn, params=prm_cd))
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

    return df_tienda, df_cd, df_by_store, df_detail


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

    # 1. Canal values in coo_maestro_sucursal
    try:
        df_canal = pd.read_sql(
            "SELECT canal_de_distribucion, COUNT(*) AS n "
            "FROM db_syncros.public.coo_maestro_sucursal "
            "GROUP BY 1 ORDER BY 2 DESC",
            conn,
        )
        results["canales"] = df_canal
    except Exception:
        results["canales"] = None

    # 2. Stock rows by canal (for the date range)
    try:
        df_stock = pd.read_sql(
            f"SELECT COALESCE(b.canal_de_distribucion, 'NULL/SIN_MATCH') AS canal, "
            f"       COUNT(*) AS filas, "
            f"       COUNT(DISTINCT a.sku_producto) AS skus "
            f"FROM {_INSTOCK} a "
            f"LEFT JOIN db_syncros.public.coo_maestro_sucursal b "
            f"    ON a.cod_bodega = b.id_sucursal "
            f"WHERE a.fecha >= %s AND a.fecha <= %s "
            f"GROUP BY 1 ORDER BY 2 DESC",
            conn, params=[str(stock_start), str(stock_end)],
        )
        results["stock_por_canal"] = df_stock
    except Exception:
        results["stock_por_canal"] = None

    # 3. Perfil distribution
    try:
        df_perfil = pd.read_sql(
            f"SELECT COALESCE(a.perfil, 'NULL') AS perfil, COUNT(*) AS filas "
            f"FROM {_INSTOCK} a "
            f"LEFT JOIN db_syncros.public.coo_maestro_sucursal b "
            f"    ON a.cod_bodega = b.id_sucursal "
            f"WHERE a.fecha >= %s AND a.fecha <= %s "
            f"  AND COALESCE(b.canal_de_distribucion, 'TIENDA') "
            f"      NOT IN ('CD', 'MAYOR', 'ETAIL', 'MAYORISTA') "
            f"GROUP BY 1",
            conn, params=[str(stock_start), str(stock_end)],
        )
        results["perfil"] = df_perfil
    except Exception:
        results["perfil"] = None

    # 4. VCM demand rows for tienda
    try:
        df_demand = pd.read_sql(
            f"SELECT COUNT(*) AS filas, "
            f"       COUNT(DISTINCT v.sku_producto) AS skus, "
            f"       SUM(v.cantidad) AS total_qty "
            f"FROM {_VCM} v "
            f"LEFT JOIN db_syncros.public.coo_maestro_sucursal b "
            f"    ON v.cod_ccosto = b.id_sucursal "
            f"WHERE v.fecha >= %s AND v.fecha <= %s "
            f"  AND v.cantidad > 0 "
            f"  AND COALESCE(b.canal_de_distribucion, 'TIENDA') "
            f"      NOT IN ('CD', 'MAYOR', 'ETAIL', 'MAYORISTA')",
            conn, params=[str(demand_start), str(demand_end)],
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


def _fmt_currency(val: float) -> str:
    """Format as compact currency: $1.2B, $345M, $12K, $1,234."""
    abs_v = abs(val)
    sign = "-" if val < 0 else ""
    if abs_v >= 1_000_000_000:
        return f"${sign}{abs_v / 1_000_000_000:,.1f}B"
    if abs_v >= 1_000_000:
        return f"${sign}{abs_v / 1_000_000:,.1f}M"
    if abs_v >= 10_000:
        return f"${sign}{abs_v / 1_000:,.0f}K"
    if abs_v >= 1_000:
        return f"${sign}{abs_v:,.0f}"
    return f"${sign}{abs_v:,.1f}"


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


def _chart_evolucion(df_tienda, df_cd):
    """Dual-axis chart: VP diaria (bars) + InStock % (line)."""
    parts = []
    if not df_tienda.empty:
        day_t = df_tienda.groupby("FECHA").agg(
            VP_TIENDA=("VP_PESOS", "sum"),
            TIENDAS_IS=("N_TIENDAS_INSTOCK", "sum"),
            TIENDAS_TOTAL=("N_TIENDAS_TOTAL", "sum"),
        ).reset_index()
        parts.append(day_t)
    if not df_cd.empty:
        day_c = df_cd.groupby("FECHA").agg(
            VP_CD=("VP_PESOS", "sum"),
            CD_IS=("INSTOCK_CD", "sum"),
            CD_TOTAL=("INSTOCK_CD", "count"),
        ).reset_index()
        parts.append(day_c)

    if not parts:
        return None

    if len(parts) == 2:
        for p in parts:
            p["FECHA"] = pd.to_datetime(p["FECHA"])
        daily = parts[0].merge(parts[1], on="FECHA", how="outer").fillna(0)
    else:
        daily = parts[0].copy()
        if "VP_TIENDA" not in daily.columns:
            daily["VP_TIENDA"] = 0
        if "VP_CD" not in daily.columns:
            daily["VP_CD"] = 0
        if "TIENDAS_IS" not in daily.columns:
            daily["TIENDAS_IS"] = 0
            daily["TIENDAS_TOTAL"] = 0

    daily["FECHA"] = pd.to_datetime(daily["FECHA"])
    daily = daily.sort_values("FECHA")
    daily["VP_TOTAL"] = daily.get("VP_TIENDA", 0) + daily.get("VP_CD", 0)

    # InStock % (tiendas)
    if "TIENDAS_TOTAL" in daily.columns:
        daily["INSTOCK_PCT"] = np.where(
            daily["TIENDAS_TOTAL"] > 0,
            daily["TIENDAS_IS"] / daily["TIENDAS_TOTAL"] * 100,
            100,
        )
    else:
        daily["INSTOCK_PCT"] = 100

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    if "VP_TIENDA" in daily.columns:
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
    fig.update_yaxes(title_text="Venta Perdida ($)", secondary_y=False)
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
            text=[f"${v:,.0f}" for v in agg[value_col].values[::-1]],
            textposition="outside",
        )
    )
    fig.update_layout(
        title=title,
        xaxis_title="Venta Perdida ($)",
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
            text=[f"${v:,.0f}" for v in agg["VP_PESOS"]],
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
            texttemplate="%{label}<br>%{percent}<br>$%{value:,.0f}",
        )
    )
    fig.update_layout(
        title="VP por Canal CD", height=400,
        margin=dict(l=20, r=20, t=40, b=20),
    )
    return fig


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
        f_status = c3.multiselect(
            "Status Producto",
            ["ESTABLECIDO", "NUEVO", "SIN VENTAS"],
            help="Filtra por antigüedad: ESTABLECIDO=ventas antes del "
                 "periodo, NUEVO=1era venta en el periodo, SIN VENTAS=nunca vendido",
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

            df_tienda, df_cd, df_by_store, df_detail = _compute_vp_range(
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

            st.session_state["vp_tienda"] = df_tienda
            st.session_state["vp_cd"] = df_cd
            st.session_state["vp_by_store"] = df_by_store
            st.session_state["vp_detail"] = df_detail
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

    st.html(_hdr(
        f"📉 Venta Perdida — {vp_desde.strftime('%d/%m/%Y')} "
        f"al {vp_hasta.strftime('%d/%m/%Y')} ({n_dias_rango} dias)"
    ))

    k1, k2, k3, k4 = st.columns(4)
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
    with k4:
        st.html(simple_kpi_card(
            label="SKUS CON VP",
            value=f"{len(skus_all):,}",
            accent_color=COLORS["status_at_risk"],
            subtitle=f"{n_dias_rango} dias analizados",
        ))

    # ── Excluded stores info (hardcoded) ──
    st.info(
        "**Tiendas excluidas** (cerradas): Bellavista, Chiclayo 2, "
        "San Miguel 2, Outlet, Cajamarca, Trujillo 2, 143, 148",
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
        with st.expander("🔧 Diagnostico VP Tiendas = $0", expanded=True):
            # Store ID overlap diagnostic
            if _store_diag is not None and not _store_diag.empty:
                st.write("**Overlap tiendas demand vs stock:**")
                st.dataframe(_store_diag, hide_index=True)
            if diag:
                if diag.get("canales") is not None:
                    st.write("**Canales en coo_maestro_sucursal:**")
                    st.dataframe(diag["canales"], hide_index=True)
                if diag.get("stock_por_canal") is not None:
                    st.write("**Stock (INSTOCK) por canal:**")
                    st.dataframe(diag["stock_por_canal"], hide_index=True)
                if diag.get("perfil") is not None:
                    st.write("**Distribucion PERFIL (tiendas):**")
                    st.dataframe(diag["perfil"], hide_index=True)
                if diag.get("demanda_tienda") is not None:
                    st.write("**Demanda VCM tiendas:**")
                    st.dataframe(diag["demanda_tienda"], hide_index=True)
            st.write(
                "**Tiendas excluidas (cerradas):** Bellavista, Chiclayo 2, "
                "San Miguel 2, Outlet, Cajamarca, Trujillo 2, 143, 148"
            )

    # ── Tabs ──
    tab_evo, tab_tienda, tab_sucursal, tab_cd, tab_detalle, tab_resumen = (
        st.tabs([
            "📈 Evolucion Diaria",
            "🏬 Tiendas (SKU)",
            "🏪 Por Sucursal",
            "📦 CD (Mayor/Etail)",
            "📋 Detalle Calculo",
            "📊 Resumen",
        ])
    )

    # ── Tab Evolucion ──
    with tab_evo:
        fig_evo = _chart_evolucion(df_tienda, df_cd)
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
                        "VP Tiendas ($)", format="$%.0f",
                    ),
                    "VP_CD": st.column_config.NumberColumn(
                        "VP CD ($)", format="$%.0f",
                    ),
                    "VP_TOTAL": st.column_config.NumberColumn(
                        "VP Total ($)", format="$%.0f",
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
                    "VP Acum ($)", format="$%.0f",
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
                    name="VP ($)",
                    marker_color=COLORS.get("primary", "#065E8B"),
                    text=[
                        f"${v:,.0f}"
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
                title_text="Venta Perdida ($)", secondary_y=False,
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
                        "VP Acum ($)", format="$%.0f",
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
                    "VP Acum ($)", format="$%.0f",
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

    # ── Tab Detalle Calculo ──
    with tab_detalle:
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

            # Aggregated view: SKU × Store
            st.html(_hdr("📊 Vista Agregada — SKU × Sucursal"))

            agg_det_cols = {
                "DESCRIPCION_SUCURSAL": "first",
                "STOCK_UNIDADES": "mean",
                "STOCK_CD": "mean",
                "DAILY_DEMAND_ALL": "mean",
                "INSTOCK_CD": "mean",
                "DEMANDA_TOTAL_DIA": "mean",
                "N_TIENDAS_PERFIL": "max",
                "DEMANDA_POR_TIENDA": "mean",
                "INSTOCK": "sum",
                "VP_UNIDADES": "sum",
                "VP_PESOS": "sum",
                "VP_UNIDADES_ORIG": "sum",
                "VP_PESOS_ORIG": "sum",
                "PRECIO_VCM": "last",
                "ULTIMO_COSTO": "last",
                "PRECIO_USADO": "last",
            }
            # Only include columns that exist
            agg_det_cols = {
                k: v for k, v in agg_det_cols.items()
                if k in df_det.columns
            }
            # Add dimension columns
            for c in ["NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA",
                       "MARCA", "MIX_OFICIAL", "PRODUCTO_STATUS",
                       "FIRST_SALE_DATE"]:
                if c in df_det.columns:
                    agg_det_cols[c] = "first"

            group_cols = ["SKU_PRODUCTO", "ID_SUCURSAL"]
            group_cols = [c for c in group_cols if c in df_det.columns]

            if group_cols:
                df_det_agg = (
                    df_det.groupby(group_cols)
                    .agg(**{k: (k, v) for k, v in agg_det_cols.items()})
                    .reset_index()
                )
                # Add days and InStock %
                dias_per_combo = (
                    df_det.groupby(group_cols).size().reset_index(
                        name="DIAS_TOTAL"
                    )
                )
                df_det_agg = df_det_agg.merge(
                    dias_per_combo, on=group_cols, how="left",
                )
                if "INSTOCK" in df_det_agg.columns:
                    df_det_agg.rename(
                        columns={"INSTOCK": "DIAS_INSTOCK"}, inplace=True,
                    )
                    df_det_agg["INSTOCK_PCT"] = np.where(
                        df_det_agg["DIAS_TOTAL"] > 0,
                        df_det_agg["DIAS_INSTOCK"]
                        / df_det_agg["DIAS_TOTAL"] * 100,
                        100,
                    )

                df_det_agg = df_det_agg.sort_values(
                    "VP_PESOS", ascending=False,
                )

                # Display columns
                det_display = [c for c in [
                    "SKU_PRODUCTO", "NOM_PRODUCTO",
                    "ID_SUCURSAL", "DESCRIPCION_SUCURSAL",
                    "AREA", "LINEA", "MARCA", "MIX_OFICIAL",
                    "PRODUCTO_STATUS",
                    "STOCK_UNIDADES", "STOCK_CD",
                    "DAILY_DEMAND_ALL", "INSTOCK_CD",
                    "DEMANDA_POR_TIENDA",
                    "N_TIENDAS_PERFIL",
                    "DIAS_INSTOCK", "DIAS_TOTAL", "INSTOCK_PCT",
                    "VP_UNIDADES", "VP_PESOS",
                    "VP_UNIDADES_ORIG", "VP_PESOS_ORIG",
                    "PRECIO_VCM", "ULTIMO_COSTO", "PRECIO_USADO",
                ] if c in df_det_agg.columns]

                st.dataframe(
                    df_det_agg[det_display].head(2000),
                    column_config={
                        "SKU_PRODUCTO": st.column_config.TextColumn("SKU"),
                        "NOM_PRODUCTO": st.column_config.TextColumn(
                            "Producto", width="medium",
                        ),
                        "ID_SUCURSAL": st.column_config.TextColumn(
                            "Sucursal",
                        ),
                        "DESCRIPCION_SUCURSAL": st.column_config.TextColumn(
                            "Nombre Suc.",
                        ),
                        "PRODUCTO_STATUS": st.column_config.TextColumn(
                            "Status",
                            help="ESTABLECIDO / NUEVO / SIN VENTAS",
                        ),
                        "STOCK_UNIDADES": st.column_config.NumberColumn(
                            "Stock Prom", format="%.1f",
                            help="Stock promedio diario en esa tienda",
                        ),
                        "STOCK_CD": st.column_config.NumberColumn(
                            "Stock CD Prom", format="%.1f",
                            help="Stock promedio diario en CD para este SKU",
                        ),
                        "DAILY_DEMAND_ALL": st.column_config.NumberColumn(
                            "Dda Diaria Cia", format="%.2f",
                            help="Demanda diaria promedio todos los canales "
                                 "(VCM, ventana 2 meses). Umbral InStock CD.",
                        ),
                        "INSTOCK_CD": st.column_config.NumberColumn(
                            "IS CD", format="%d",
                            help="1 = stock CD >= demanda prom 90d cia, 0 = no",
                        ),
                        "DEMANDA_POR_TIENDA": st.column_config.NumberColumn(
                            "Dda/Tienda/Dia", format="%.2f",
                            help="Demanda VCM total / n_tiendas_perfil",
                        ),
                        "N_TIENDAS_PERFIL": st.column_config.NumberColumn(
                            "N Tiendas", format="%d",
                            help="Tiendas con perfil para este SKU",
                        ),
                        "DIAS_INSTOCK": st.column_config.NumberColumn(
                            "Dias IS", format="%d",
                            help="Dias donde stock >= demanda por tienda",
                        ),
                        "DIAS_TOTAL": st.column_config.NumberColumn(
                            "Dias Total", format="%d",
                        ),
                        "INSTOCK_PCT": st.column_config.ProgressColumn(
                            "IS %",
                            format="%.1f%%",
                            min_value=0,
                            max_value=100,
                        ),
                        "VP_UNIDADES": st.column_config.NumberColumn(
                            "VP (Und)", format="%.1f",
                        ),
                        "VP_PESOS": st.column_config.NumberColumn(
                            "VP ($)", format="$%.0f",
                        ),
                        "VP_UNIDADES_ORIG": st.column_config.NumberColumn(
                            "VP Orig (Und)", format="%.1f",
                            help="VP antes de aplicar periodo de gracia",
                        ),
                        "VP_PESOS_ORIG": st.column_config.NumberColumn(
                            "VP Orig ($)", format="$%.0f",
                            help="VP ($) antes de aplicar periodo de gracia",
                        ),
                        "PRECIO_VCM": st.column_config.NumberColumn(
                            "Precio VCM", format="$%.0f",
                            help="Precio promedio VCM (neto/cantidad)",
                        ),
                        "ULTIMO_COSTO": st.column_config.NumberColumn(
                            "Ult. Costo", format="$%.0f",
                            help="Ultimo costo de vw_producto (fallback)",
                        ),
                        "PRECIO_USADO": st.column_config.NumberColumn(
                            "Precio Usado", format="$%.0f",
                            help="Precio usado: VCM si >0, sino ultimo_costo",
                        ),
                    },
                    use_container_width=True, height=600, hide_index=True,
                )

                download_buttons(
                    df_det_agg[det_display],
                    "vp_detalle_sku_sucursal",
                )

            # Raw detail (daily) — download only
            st.html(_hdr("📥 Detalle Diario Completo (descarga)"))
            st.caption(
                f"Detalle a nivel SKU × Sucursal × Dia: "
                f"{len(df_det):,} registros. "
                f"Descargue para analisis en Excel."
            )

            raw_display = [c for c in [
                "FECHA", "SKU_PRODUCTO", "NOM_PRODUCTO",
                "ID_SUCURSAL", "DESCRIPCION_SUCURSAL",
                "AREA", "LINEA", "MARCA", "MIX_OFICIAL",
                "PRODUCTO_STATUS", "FIRST_SALE_DATE",
                "STOCK_UNIDADES", "STOCK_CD",
                "DAILY_DEMAND_ALL", "INSTOCK_CD",
                "DEMANDA_TOTAL_DIA",
                "N_TIENDAS_PERFIL", "DEMANDA_POR_TIENDA",
                "INSTOCK", "VP_UNIDADES", "VP_PESOS",
                "VP_UNIDADES_ORIG", "VP_PESOS_ORIG",
                "RECOVERY_DATE", "DIAS_DESDE_RECOVERY",
                "LEAD_DAYS", "EN_GRACIA",
                "PRECIO_VCM", "ULTIMO_COSTO", "PRECIO_USADO",
            ] if c in df_det.columns]

            download_buttons(df_det[raw_display], "vp_detalle_diario")

    # ── Tab Resumen ──
    with tab_resumen:
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
                                "VP ($)", format="$%.0f",
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
