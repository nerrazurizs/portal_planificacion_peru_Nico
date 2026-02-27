"""Script autónomo para envío de alertas de supply chain por email.

Diseñado para correr en GitHub Actions o cron — NO requiere Streamlit.
Replica los mismos datos y cálculos que los módulos del portal
(stock_critico, ventas, plan_compras, higiene, proyeccion).

Variables de entorno requeridas:
    SNOWFLAKE_USER, SNOWFLAKE_PASSWORD, SNOWFLAKE_ACCOUNT
    SNOWFLAKE_WAREHOUSE, SNOWFLAKE_ROLE, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA
    OUTLOOK_EMAIL, OUTLOOK_PASSWORD, SMTP_SERVER, SMTP_PORT
    ALERT_RECIPIENTS  (emails separados por coma)

Bloques:
    1. Resumen Ejecutivo S&OP  (inventario a COSTO + VN/Aporte YTD vs Budget/AA)
    2. Salud de Stock          (KPIs a costo + matriz 6×6 idéntica a stock_critico.py)
    3. Proyección financiera   (tabla anual + charts área/canal desde proy_result.parquet)
    4. Comex                   (POs atrasadas + sin carpeta + sin diario factura + próximas)
    5. Plan de compras         (POs en tránsito próximas)
    6. Higiene                 (SKUs sin forecast MIX, CD sin perfil tiendas)
    7. InStock                 (Disponibilidad Tiendas & CD, evolución 14d)
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

# ── Root del proyecto ────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import numpy as np
import pandas as pd
import snowflake.connector

from db.queries import (
    QUERY_COMEX_ATRASADAS,
    QUERY_COMEX_PROXIMAS,
    QUERY_COMEX_SIN_CARPETA,
    QUERY_COMEX_SIN_FACTURA,
    QUERY_MAESTRA,
    QUERY_PRECIO_PROM_SKU,
    QUERY_STOCK_CRITICO_METRICS,
    QUERY_STOCK_HIGIENE,
)
from utils.budget import get_budget_cogs_monthly, load_budget
from utils.email_sender import build_alert_email, get_smtp_config, send_alert_email
from utils.filters import norm_cols

# ── Tiers de salud (réplica exacta de modules/stock_critico.py) ──────────────
_MOI_TIERS = ["0-3m", "3-6m", "6-8m", "8-12m", "12-24m", ">=24m"]
_ANT_TIERS = ["0-3m", "3-6m", "6-8m", "8-12m", "12-24m", ">=24m"]

# ── Queries adicionales (no están en queries.py aún) ─────────────────────────
_QUERY_VENTAS_YTD = """
SELECT
    sum(a.neto)   AS NETO_TOTAL,
    sum(a.aporte) AS APORTE_TOTAL,
    sum(a.aporte) / nullif(sum(a.neto), 0) AS MARGEN
FROM db_finanzas.fct.ft_vcm a
WHERE a.fecha >= date_trunc('year', current_date())
  AND a.fecha <= current_date()
"""

_QUERY_VENTAS_YTD_AA = """
SELECT
    sum(a.neto)   AS NETO_AA,
    sum(a.aporte) AS APORTE_AA,
    sum(a.aporte) / nullif(sum(a.neto), 0) AS MARGEN_AA
FROM db_finanzas.fct.ft_vcm a
WHERE a.fecha >= dateadd('year', -1, date_trunc('year', current_date()))
  AND a.fecha <= dateadd('year', -1, current_date())
"""

_QUERY_STOCK_ONHAND_COST = """
SELECT
    sum(case when b.canal_de_distribucion = 'CD'     then a.stock_unidades else 0 end) AS STOCK_CD,
    sum(case when b.canal_de_distribucion = 'TIENDA' then a.stock_unidades else 0 end) AS STOCK_TIENDA,
    sum(a.stock_unidades) AS STOCK_TOTAL,
    sum(a.stock_costo)    AS STOCK_COSTO_TOTAL,
    sum(case when b.canal_de_distribucion = 'CD'     then a.stock_costo else 0 end) AS STOCK_COSTO_CD,
    sum(case when b.canal_de_distribucion = 'TIENDA' then a.stock_costo else 0 end) AS STOCK_COSTO_TIENDA
FROM db_supply.hst.vw_in_stock a
LEFT JOIN db_syncros.public.coo_maestro_sucursal b
    ON a.cod_bodega = b.id_sucursal
WHERE a.fecha = (SELECT max(fecha) FROM db_supply.hst.vw_in_stock WHERE fecha < current_date())
"""

# COGS últimos 6 meses (para MOI Histórico — réplica stock_critico.py)
_QUERY_COGS_6M = """
SELECT
    date_trunc('month', a.fecha) AS PERIODO,
    sum(a.neto) - sum(a.aporte) AS COGS_MENSUAL
FROM db_finanzas.fct.ft_vcm a
WHERE a.cantidad > 0
  AND a.fecha >= dateadd('month', -6, date_trunc('month', current_date()))
  AND a.fecha <  date_trunc('month', current_date())
GROUP BY 1
ORDER BY 1
"""

# Ventas MTD por canal con APORTE (3 canales consolidados)
_QUERY_VENTAS_MTD_CANAL = """
SELECT
    CASE
        WHEN a.cod_canal = 'MINOR' THEN 'MINORISTA'
        WHEN a.cod_canal = 'MAYOR' THEN 'MAYORISTA'
        WHEN a.cod_canal = 'ETAIL' THEN 'ETAIL'
        ELSE 'OTROS'
    END AS CANAL,
    sum(a.cantidad) AS CANTIDAD_MTD,
    sum(a.neto)     AS NETO_MTD,
    sum(a.aporte)   AS APORTE_MTD
FROM db_finanzas.fct.ft_vcm a
WHERE a.cantidad > 0
  AND a.fecha >= date_trunc('month', current_date())
  AND a.fecha <  current_date()
GROUP BY 1
"""

# Ventas MTD año anterior (mismo periodo: 1ro al mismo dia del mes, año pasado)
_QUERY_VENTAS_MTD_AA_CANAL = """
SELECT
    CASE
        WHEN a.cod_canal = 'MINOR' THEN 'MINORISTA'
        WHEN a.cod_canal = 'MAYOR' THEN 'MAYORISTA'
        WHEN a.cod_canal = 'ETAIL' THEN 'ETAIL'
        ELSE 'OTROS'
    END AS CANAL,
    sum(a.neto)   AS NETO_MTD_AA,
    sum(a.aporte) AS APORTE_MTD_AA
FROM db_finanzas.fct.ft_vcm a
WHERE a.cantidad > 0
  AND a.fecha >= dateadd('year', -1, date_trunc('month', current_date()))
  AND a.fecha <  dateadd('year', -1, current_date())
GROUP BY 1
"""

_PLAN_COMPRAS_QUERY = """
SELECT PO, NOM_PRODUCTO, NOM_PROVEEDOR, CARPETA_COMEX,
    CASE WHEN CARPETA_COMEX IS NULL OR TRIM(CARPETA_COMEX) = '' THEN DATEADD(day, 47, FECHA_ENTREGA)
         ELSE ETA END AS ETA_CALC,
    CANTIDAD_FINAL_CORREGIDA, MONTOMN
FROM db_supply.fct.ft_compras
WHERE FECHA_RECEPCION_EN_CD IS NULL AND CANTIDAD_FINAL_CORREGIDA > 0
  AND YEAR(CASE WHEN CARPETA_COMEX IS NULL OR TRIM(CARPETA_COMEX) = '' THEN DATEADD(day, 47, FECHA_ENTREGA)
                ELSE ETA END) >= YEAR(current_date())
ORDER BY ETA_CALC ASC LIMIT 200
"""

# ── InStock queries (pre-agregadas, livianas — para email) ────────────────────
# IS CALCULADO: stock >= vta_prom_90 (no usa flag pre-computado de BI)
# TIENDA: solo SKUs MIX + perfil=SI + CD InStock calculado = 1
_QUERY_INSTOCK_EMAIL_TIENDA = """
SELECT a.fecha,
       c.area,
       sum(CASE WHEN a.perfil = 'SI' THEN 1 ELSE 0 END) AS n_perfil,
       sum(CASE WHEN a.perfil = 'SI' AND a.stock_unidades > 0
                AND a.stock_unidades >= coalesce(a.cantidad_prom_90, 0)
                THEN 1 ELSE 0 END) AS tiendas_is
FROM db_supply.hst.vw_in_stock a
JOIN db_syncros.public.coo_maestro_sucursal b ON a.cod_bodega = b.id_sucursal
INNER JOIN db_dimensiones.dim.vw_producto c ON a.sku_producto = c.sku_producto
INNER JOIN db_supply.hst.vw_in_stock_cd d
    ON a.fecha = d.fecha AND a.sku_producto = d.sku_producto
WHERE b.canal_de_distribucion = 'TIENDA'
  AND a.fecha >= dateadd('day', -14, current_date())
  AND d.stock_unidades > 0
  AND d.stock_unidades >= coalesce(d.cantidad_prom_90_cia, 0)
  AND c.mix_oficial = 'MIX'
GROUP BY 1, 2
"""

_QUERY_INSTOCK_EMAIL_CD = """
SELECT a.fecha,
       c.area,
       count(*) AS n_skus,
       sum(CASE WHEN a.stock_unidades > 0
                AND a.stock_unidades >= coalesce(a.cantidad_prom_90_cia, 0)
                THEN 1 ELSE 0 END) AS skus_is
FROM db_supply.hst.vw_in_stock_cd a
INNER JOIN db_dimensiones.dim.vw_producto c ON a.sku_producto = c.sku_producto
WHERE a.fecha >= dateadd('day', -14, current_date())
  AND c.mix_oficial = 'MIX'
  AND EXISTS (
      SELECT 1 FROM db_supply.hst.vw_in_stock f
      JOIN db_syncros.public.coo_maestro_sucursal s ON f.cod_bodega = s.id_sucursal
      WHERE f.sku_producto = a.sku_producto AND f.fecha = a.fecha
        AND s.canal_de_distribucion = 'TIENDA'
        AND f.perfil = 'SI'
  )
GROUP BY 1, 2
"""

# ── Columnas para tablas ─────────────────────────────────────────────────────
_ATRASADAS_COLS = [
    "PO", "NOM_PRODUCTO", "NOM_PROVEEDOR", "CARPETA_COMEX",
    "ETA_CALC", "DIAS_ATRASO", "CANTIDAD_FINAL_CORREGIDA", "MONTOMN",
]
_SIN_CARPETA_COLS = [
    "PO", "NOM_PRODUCTO", "NOM_PROVEEDOR", "FECHA_ENTREGA",
    "DIAS_DESDE_ENTREGA", "CANTIDAD_FINAL_CORREGIDA", "MONTOMN",
]
_SIN_FACTURA_COLS = [
    "PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "NOM_PROVEEDOR",
    "CARPETA_COMEX", "ETA_CALC", "QTY_PENDIENTE", "MONTOMN",
]
_PROXIMAS_COLS = [
    "PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "NOM_PROVEEDOR",
    "CARPETA_COMEX", "ETA_CALC", "DIAS_HASTA_ETA",
    "QTY_PENDIENTE", "MONTO_PENDIENTE_CLP",
    "VN_POTENCIAL", "MOI_ACTUAL", "PRIORIDAD_SCORE",
]
_PLAN_COLS = [
    "PO", "NOM_PRODUCTO", "NOM_PROVEEDOR", "CARPETA_COMEX",
    "ETA_CALC", "CANTIDAD_FINAL_CORREGIDA", "MONTOMN",
]


# ============================================================================
# SNOWFLAKE
# ============================================================================

def _connect() -> snowflake.connector.SnowflakeConnection:
    return snowflake.connector.connect(
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        role=os.environ.get("SNOWFLAKE_ROLE", "PUBLIC"),
        database=os.environ.get("SNOWFLAKE_DATABASE", ""),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", ""),
    )


def _query(conn, sql: str) -> pd.DataFrame:
    cursor = conn.cursor()
    cursor.execute(sql)
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return norm_cols(pd.DataFrame(rows, columns=cols))


# ============================================================================
# TIER CLASSIFICATION (réplica exacta de stock_critico._classify_health_tier)
# ============================================================================

def _classify_tier(series: pd.Series) -> pd.Series:
    """Classify months into health tiers (same as stock_critico.py)."""
    conditions = [
        series < 3,
        (series >= 3) & (series < 6),
        (series >= 6) & (series < 8),
        (series >= 8) & (series < 12),
        (series >= 12) & (series < 24),
        series >= 24,
    ]
    return pd.Series(np.select(conditions, _MOI_TIERS, default="Sin Info"), index=series.index)


# ============================================================================
# BLOQUE 1 — KPIs Ejecutivos
# ============================================================================

def load_stock_kpis(conn) -> dict:
    """Stock a COSTO por canal (CD vs Tienda)."""
    print("  → Inventario a costo...")
    try:
        df = _query(conn, _QUERY_STOCK_ONHAND_COST)
        for c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return {
            "stock_cd":            float(df["STOCK_CD"].sum()),
            "stock_tienda":        float(df["STOCK_TIENDA"].sum()),
            "stock_total":         float(df["STOCK_TOTAL"].sum()),
            "stock_costo":         float(df["STOCK_COSTO_TOTAL"].sum()),
            "stock_costo_cd":      float(df["STOCK_COSTO_CD"].sum()),
            "stock_costo_tienda":  float(df["STOCK_COSTO_TIENDA"].sum()),
        }
    except Exception as e:
        print(f"  ✗ {e}")
        return {}


def load_ventas_ytd(conn) -> dict:
    """VN y Aporte YTD reales desde Snowflake + Budget + AA + Margen AA."""
    print("  → Ventas YTD real + AA + Budget...")
    kpis: dict = {}
    try:
        # Real YTD
        df = _query(conn, _QUERY_VENTAS_YTD)
        kpis["vn_real"]     = float(df["NETO_TOTAL"].sum())   if "NETO_TOTAL" in df.columns else 0
        kpis["aporte_real"] = float(df["APORTE_TOTAL"].sum()) if "APORTE_TOTAL" in df.columns else 0
        kpis["margen_real"] = float(df["MARGEN"].iloc[0])     if "MARGEN" in df.columns and len(df) > 0 else 0

        # AA YTD (mismo periodo, año anterior)
        df_aa = _query(conn, _QUERY_VENTAS_YTD_AA)
        kpis["vn_aa"]     = float(df_aa["NETO_AA"].sum())   if "NETO_AA" in df_aa.columns else 0
        kpis["aporte_aa"] = float(df_aa["APORTE_AA"].sum()) if "APORTE_AA" in df_aa.columns else 0
        kpis["margen_aa"] = float(df_aa["MARGEN_AA"].iloc[0]) if "MARGEN_AA" in df_aa.columns and len(df_aa) > 0 else 0

        # Budget YTD
        budget_df = load_budget()
        if not budget_df.empty:
            budget_df["PERIODO"] = pd.to_datetime(budget_df["PERIODO"], errors="coerce")
            today = pd.Timestamp(date.today())
            bdgt = budget_df[
                (budget_df["CANAL"].str.upper() == "TOTAL")
                & (budget_df["PERIODO"] <= today)
            ]
            kpis["vn_budget"]     = float(bdgt["VN_BUDGET"].sum())     if "VN_BUDGET" in bdgt.columns else 0
            kpis["aporte_budget"] = float(bdgt["APORTE_BUDGET"].sum()) if "APORTE_BUDGET" in bdgt.columns else 0
            if kpis["vn_budget"] > 0:
                kpis["margen_budget"] = kpis["aporte_budget"] / kpis["vn_budget"]
            else:
                kpis["margen_budget"] = 0

        print(f"     VN YTD: ${kpis['vn_real']/1e6:.1f}M · Budget: ${kpis.get('vn_budget',0)/1e6:.1f}M · AA: ${kpis['vn_aa']/1e6:.1f}M")
    except Exception as e:
        print(f"  ✗ Ventas YTD: {e}")
    return kpis


def load_ventas_mtd(conn) -> dict:
    """Ventas MTD por canal (3 canales consolidados + TOTAL + aporte + margen + vs AA/BU)."""
    print("  → Ventas MTD por canal...")
    try:
        df = _query(conn, _QUERY_VENTAS_MTD_CANAL)
        for c in ["CANTIDAD_MTD", "NETO_MTD", "APORTE_MTD"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

        # Solo 3 canales principales
        canal_col = "CANAL" if "CANAL" in df.columns else df.columns[0]
        main_canals = {"MINORISTA", "MAYORISTA", "ETAIL"}
        df = df[df[canal_col].isin(main_canals)]

        # AA MTD por canal (mismo periodo, año anterior)
        aa_lookup: dict = {}
        try:
            df_aa = _query(conn, _QUERY_VENTAS_MTD_AA_CANAL)
            for c in ["NETO_MTD_AA", "APORTE_MTD_AA"]:
                if c in df_aa.columns:
                    df_aa[c] = pd.to_numeric(df_aa[c], errors="coerce").fillna(0)
            aa_canal_col = "CANAL" if "CANAL" in df_aa.columns else df_aa.columns[0]
            for _, row in df_aa.iterrows():
                canal_aa = str(row.get(aa_canal_col, "")).upper()
                if canal_aa in main_canals:
                    aa_lookup[canal_aa] = float(row.get("NETO_MTD_AA", 0))
        except Exception:
            pass

        # Budget MTD por canal (mes actual, pro-rateado por días transcurridos)
        bu_lookup: dict = {}
        try:
            budget_df = load_budget()
            if not budget_df.empty:
                budget_df["PERIODO"] = pd.to_datetime(budget_df["PERIODO"], errors="coerce")
                current_month = pd.Timestamp(date.today().replace(day=1))
                month_budget = budget_df[budget_df["PERIODO"] == current_month]
                # Pro-rateo: días transcurridos / días del mes
                import calendar
                today = date.today()
                days_in_month = calendar.monthrange(today.year, today.month)[1]
                days_elapsed = today.day - 1  # MTD = 1 al día anterior
                prorrata = days_elapsed / days_in_month if days_in_month > 0 else 0
                canal_budget_map = {"RETAIL": "MINORISTA", "MAYORISTA": "MAYORISTA", "ETAIL": "ETAIL", "TOTAL": "TOTAL"}
                for _, row in month_budget.iterrows():
                    raw_canal = str(row.get("CANAL", "")).upper()
                    mapped = canal_budget_map.get(raw_canal, raw_canal)
                    if mapped in main_canals or mapped == "TOTAL":
                        vn_bu = float(row.get("VN_BUDGET", 0)) * prorrata
                        bu_lookup[mapped] = vn_bu
        except Exception:
            pass

        result = {}
        total_und, total_neto, total_aporte = 0.0, 0.0, 0.0
        total_aa, total_bu = 0.0, 0.0
        for _, row in df.iterrows():
            canal = str(row.get(canal_col, "")).upper()
            und = float(row.get("CANTIDAD_MTD", 0))
            neto = float(row.get("NETO_MTD", 0))
            aporte = float(row.get("APORTE_MTD", 0))
            margen = aporte / neto if neto > 0 else 0
            neto_aa = aa_lookup.get(canal, 0)
            neto_bu = bu_lookup.get(canal, 0)
            result[canal] = {
                "und": und, "neto": neto, "aporte": aporte, "margen": margen,
                "neto_aa": neto_aa, "neto_bu": neto_bu,
            }
            total_und += und
            total_neto += neto
            total_aporte += aporte
            total_aa += neto_aa
            total_bu += neto_bu

        result["TOTAL"] = {
            "und": total_und, "neto": total_neto, "aporte": total_aporte,
            "margen": total_aporte / total_neto if total_neto > 0 else 0,
            "neto_aa": total_aa if total_aa > 0 else aa_lookup.get("TOTAL", 0),
            "neto_bu": total_bu if total_bu > 0 else bu_lookup.get("TOTAL", 0),
        }
        return result
    except Exception as e:
        print(f"  ✗ {e}")
        return {}


def load_moi_kpis(conn, stock_costo_total: float, proy_df: pd.DataFrame) -> dict:
    """MOI Historico/Forecast/Budget — réplica de stock_critico.py."""
    print("  → MOI Compañía (hist/fc/budget)...")
    kpis: dict = {}
    try:
        # MOI Histórico (COGS promedio últimos 6 meses)
        df_cogs = _query(conn, _QUERY_COGS_6M)
        if not df_cogs.empty and "COGS_MENSUAL" in df_cogs.columns:
            df_cogs["COGS_MENSUAL"] = pd.to_numeric(df_cogs["COGS_MENSUAL"], errors="coerce").fillna(0)
            avg_cogs_hist = df_cogs["COGS_MENSUAL"].mean()
            n_months = len(df_cogs)
            kpis["moi_historico"] = stock_costo_total / avg_cogs_hist if avg_cogs_hist > 0 else 0
            kpis["n_months_hist"] = n_months
        else:
            kpis["moi_historico"] = 0
            kpis["n_months_hist"] = 0

        # MOI Forecast (COGS proyectado próximos 6 meses)
        if proy_df is not None and not proy_df.empty and "COGS_RES_TOTAL" in proy_df.columns:
            fc = proy_df[proy_df["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])].copy()
            fc["PERIODO"] = pd.to_datetime(fc["PERIODO"], errors="coerce")
            fc["COGS_RES_TOTAL"] = pd.to_numeric(fc["COGS_RES_TOTAL"], errors="coerce").fillna(0)
            monthly_cogs = fc.groupby(fc["PERIODO"].dt.to_period("M"))["COGS_RES_TOTAL"].sum().sort_index().head(6)
            if len(monthly_cogs) > 0:
                avg_cogs_fc = monthly_cogs.mean()
                kpis["moi_forecast"] = stock_costo_total / avg_cogs_fc if avg_cogs_fc > 0 else 0
                kpis["n_months_fc"] = len(monthly_cogs)
            else:
                kpis["moi_forecast"] = 0
                kpis["n_months_fc"] = 0
        else:
            kpis["moi_forecast"] = 0
            kpis["n_months_fc"] = 0

        # MOI Budget (COGS presupuesto mensual promedio)
        avg_cogs_bdg = get_budget_cogs_monthly("TOTAL")
        kpis["moi_budget"] = stock_costo_total / avg_cogs_bdg if avg_cogs_bdg > 0 else 0

        print(f"     MOI Hist: {kpis['moi_historico']:.1f}m · FC: {kpis['moi_forecast']:.1f}m · Budget: {kpis['moi_budget']:.1f}m")
    except Exception as e:
        print(f"  ✗ MOI: {e}")
    return kpis


# ============================================================================
# BLOQUE 2 — Stock Crítico (réplica exacta de stock_critico.py)
# ============================================================================

def load_critico_data(conn) -> tuple[dict, pd.DataFrame]:
    """Retorna (kpis_dict, matrix_df) con la misma lógica que stock_critico.py."""
    print("  → Salud de stock (matriz 6×6)...")
    kpis: dict = {}
    matrix = pd.DataFrame()
    try:
        df = _query(conn, QUERY_STOCK_CRITICO_METRICS)
        for c in ["STOCK_COSTO", "STOCK_UNIDADES", "MOI", "ANTIGUEDAD_MESES", "COSTO_PROM_90_CIA"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

        # Usar solo la fecha más reciente
        if "FECHA" in df.columns:
            max_fecha = df["FECHA"].max()
            df = df[df["FECHA"] == max_fecha]

        # Agregar a nivel SKU (igual que _prepare_health_data)
        dim_cols = [c for c in ["AREA", "LINEA", "SUBLINEA", "MARCA", "NOM_PRODUCTO"] if c in df.columns]
        agg_dict = {"STOCK_COSTO": "sum"}
        if "STOCK_UNIDADES" in df.columns:
            agg_dict["STOCK_UNIDADES"] = "sum"
        if "COSTO_PROM_90_CIA" in df.columns:
            agg_dict["COSTO_PROM_90_CIA"] = "first"
        if "ANTIGUEDAD_MESES" in df.columns:
            agg_dict["ANTIGUEDAD_MESES"] = "max"
        if "MOI" in df.columns:
            agg_dict["MOI"] = "max"
        for c in dim_cols:
            agg_dict[c] = "first"

        sku = df.groupby("SKU_PRODUCTO", as_index=False).agg(agg_dict)

        # Recalcular MOI a nivel SKU
        if "COSTO_PROM_90_CIA" in sku.columns:
            sku["MOI"] = np.where(
                sku["COSTO_PROM_90_CIA"] > 0,
                (sku["STOCK_COSTO"] / sku["COSTO_PROM_90_CIA"]) / 30.44,
                0,
            )

        # Sin MOI (sin ventas) pero con stock → treat as worst case
        sku.loc[(sku["MOI"] <= 0) & (sku["STOCK_COSTO"] > 0), "MOI"] = 999

        # Clasificar tiers
        sku["TIER_MOI"]        = _classify_tier(sku["MOI"])
        sku["TIER_ANTIGUEDAD"] = _classify_tier(sku["ANTIGUEDAD_MESES"])

        # Incluir TODOS los SKUs con stock — los que no tienen antigüedad
        # se clasifican como peor caso si tienen stock
        sku.loc[
            (sku["ANTIGUEDAD_MESES"] <= 0) & (sku["STOCK_COSTO"] > 0),
            "ANTIGUEDAD_MESES",
        ] = 999  # worst case, igual que MOI
        # Reclasificar después del ajuste
        sku["TIER_ANTIGUEDAD"] = _classify_tier(sku["ANTIGUEDAD_MESES"])

        df_valid = sku[sku["STOCK_COSTO"] > 0].copy()

        if df_valid.empty:
            return kpis, matrix

        total_stock = df_valid["STOCK_COSTO"].sum()
        total_skus  = df_valid["SKU_PRODUCTO"].nunique()

        # ── KPIs (todo en COSTO CLP, igual que el módulo) ────────────────────
        # Saludable: MOI < 6m AND Antigüedad < 6m
        mask_sano = (
            df_valid["TIER_MOI"].isin(["0-3m", "3-6m"])
            & df_valid["TIER_ANTIGUEDAD"].isin(["0-3m", "3-6m"])
        )
        stock_sano = df_valid.loc[mask_sano, "STOCK_COSTO"].sum()

        # A Punto: MOI 8-12m (o >=24m) AND Antigüedad 8-12m
        mask_apunto = (
            df_valid["TIER_MOI"].isin(["8-12m", ">=24m"])
            & df_valid["TIER_ANTIGUEDAD"].isin(["8-12m"])
        )
        stock_apunto = df_valid.loc[mask_apunto, "STOCK_COSTO"].sum()

        # Crítico: MOI >= 12m (o >=24m) AND Antigüedad >= 12m
        mask_crit = (
            df_valid["TIER_MOI"].isin(["12-24m", ">=24m"])
            & df_valid["TIER_ANTIGUEDAD"].isin(["12-24m", ">=24m"])
        )
        stock_crit = df_valid.loc[mask_crit, "STOCK_COSTO"].sum()

        # Liquidación: MOI >= 24m AND Antigüedad >= 24m
        mask_liq = (
            (df_valid["TIER_MOI"] == ">=24m")
            & (df_valid["TIER_ANTIGUEDAD"] == ">=24m")
        )
        stock_liq = df_valid.loc[mask_liq, "STOCK_COSTO"].sum()

        kpis = {
            "total_skus":        int(total_skus),
            "stock_costo":       float(total_stock),
            "pct_saludable":     (stock_sano / total_stock * 100) if total_stock > 0 else 0,
            "stock_apunto":      float(stock_apunto),
            "stock_critico":     float(stock_crit),
            "stock_liquidacion": float(stock_liq),
        }

        # ── Construir matriz 6×6 (igual que _build_health_matrix) ────────────
        all_combos = pd.DataFrame([
            {"TIER_MOI": m, "TIER_ANTIGUEDAD": a}
            for m in _MOI_TIERS for a in _ANT_TIERS
        ])
        grp = df_valid.groupby(["TIER_MOI", "TIER_ANTIGUEDAD"], as_index=False).agg(
            N_SKUS=("SKU_PRODUCTO", "nunique"),
            STOCK_COSTO=("STOCK_COSTO", "sum"),
        )
        matrix = all_combos.merge(grp, on=["TIER_MOI", "TIER_ANTIGUEDAD"], how="left").fillna(0)
        matrix["N_SKUS"]      = matrix["N_SKUS"].astype(int)
        matrix["STOCK_COSTO"] = matrix["STOCK_COSTO"].astype(float)

        sin_stock_n = len(sku) - len(df_valid)
        print(f"     {total_skus:,} SKUs con stock, ${total_stock/1e6:.1f}M, "
              f"saludable {kpis['pct_saludable']:.1f}%, "
              f"crítico ${stock_crit/1e6:.1f}M, liquidación ${stock_liq/1e6:.1f}M "
              f"({sin_stock_n} sin stock excluidos)")

    except Exception as e:
        print(f"  ✗ Stock crítico: {e}")

    return kpis, matrix


# ============================================================================
# BLOQUE 3 — Proyección Financiera (desde parquet)
# ============================================================================

def load_proy_data() -> pd.DataFrame:
    parquet_path = ROOT / "data" / "inputs" / "proy_result.parquet"
    if not parquet_path.exists():
        print("  ✗ proy_result.parquet no encontrado.")
        return pd.DataFrame()
    try:
        df = pd.read_parquet(parquet_path)
        for c in ["VN_RES_TOTAL", "APORTE_RES_TOTAL", "COGS_RES_TOTAL",
                  "AA_VN_TOTAL", "AA_APORTE_TOTAL"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        print(f"  ✓ Proyección: {len(df):,} filas.")
        return df
    except Exception as e:
        print(f"  ✗ Parquet: {e}")
        return pd.DataFrame()


def build_proy_resumen(proy_df: pd.DataFrame, budget_df: pd.DataFrame) -> pd.DataFrame:
    try:
        if proy_df.empty:
            return pd.DataFrame()
        valid = proy_df[proy_df["TIPO_DATO"].isin(["HISTORICO", "REAL+FC", "PROYECCION"])]
        if valid.empty:
            return pd.DataFrame()
        grp = valid.groupby("PERIODO_ANO").agg(
            VN=("VN_RES_TOTAL", "sum"), COGS=("COGS_RES_TOTAL", "sum"),
            APORTE=("APORTE_RES_TOTAL", "sum"),
        ).reset_index()
        grp["MARGEN"] = np.where(grp["VN"] > 0, grp["APORTE"] / grp["VN"], 0)
        grp = grp[grp["PERIODO_ANO"] >= 2026].copy()
        if budget_df is not None and not budget_df.empty:
            budget_df = budget_df.copy()
            budget_df["PERIODO"] = pd.to_datetime(budget_df["PERIODO"], errors="coerce")
            budget_df["ANO"] = budget_df["PERIODO"].dt.year
            bdgt = (budget_df[budget_df["CANAL"].str.upper() == "TOTAL"]
                    .groupby("ANO").agg(VN_B=("VN_BUDGET", "sum"), AP_B=("APORTE_BUDGET", "sum")).reset_index())
            grp = grp.merge(bdgt, left_on="PERIODO_ANO", right_on="ANO", how="left")
        display = pd.DataFrame()
        display["Año"]           = grp["PERIODO_ANO"].astype(int)
        display["VN Proyectada"] = grp["VN"].apply(lambda x: f"${x/1e9:.2f}B" if abs(x) >= 1e9 else f"${x/1e6:.1f}M")
        display["COGS"]          = grp["COGS"].apply(lambda x: f"${x/1e9:.2f}B" if abs(x) >= 1e9 else f"${x/1e6:.1f}M")
        display["Aporte"]        = grp["APORTE"].apply(lambda x: f"${x/1e9:.2f}B" if abs(x) >= 1e9 else f"${x/1e6:.1f}M")
        display["Margen %"]      = grp["MARGEN"].apply(lambda x: f"{x:.1%}")
        if "VN_B" in grp.columns:
            display["Budget VN"] = grp["VN_B"].apply(lambda x: f"${x/1e9:.2f}B" if abs(x) >= 1e9 else f"${x/1e6:.1f}M")
            display["vs Budget"] = np.where(
                grp["VN_B"] > 0,
                ((grp["VN"] - grp["VN_B"]) / grp["VN_B"] * 100).apply(lambda x: f"+{x:.1f}%" if x > 0 else f"{x:.1f}%"),
                "—",
            )
        return display
    except Exception as e:
        print(f"  ✗ Proy resumen: {e}")
        return pd.DataFrame()


def build_proy_por_area(proy_df: pd.DataFrame) -> pd.DataFrame:
    try:
        if proy_df.empty or "AREA" not in proy_df.columns:
            return pd.DataFrame()
        valid = proy_df[
            proy_df["TIPO_DATO"].isin(["HISTORICO", "REAL+FC", "PROYECCION"])
            & (proy_df["PERIODO_ANO"] == 2026)
        ]
        if valid.empty:
            return pd.DataFrame()
        # VN 2026: sumar todos los meses
        vn_area = valid.groupby("AREA")["VN_RES_TOTAL"].sum().reset_index()
        vn_area.columns = ["AREA", "VN_2026"]
        # AA: tomar UNA VEZ por SKU (AA es total anual repetido en cada mes)
        aa_sku = valid.groupby(["SKU_PRODUCTO", "AREA"])["AA_VN_TOTAL"].first().reset_index()
        aa_area = aa_sku.groupby("AREA")["AA_VN_TOTAL"].sum().reset_index()
        aa_area.columns = ["AREA", "VN_AA"]
        grp = vn_area.merge(aa_area, on="AREA", how="left").fillna(0)
        return grp[grp["VN_2026"] > 0].sort_values("VN_2026", ascending=False).head(10).reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def build_proy_por_canal(proy_df: pd.DataFrame) -> pd.DataFrame:
    try:
        if proy_df.empty:
            return pd.DataFrame()
        valid = proy_df[
            proy_df["TIPO_DATO"].isin(["HISTORICO", "REAL+FC", "PROYECCION"])
            & (proy_df["PERIODO_ANO"] == 2026)
        ]
        if valid.empty:
            return pd.DataFrame()
        canal_map = {
            "TIENDA":    ("VN_RES_TIENDA",  "AA_VN_TIENDA"),
            "ETAIL":     ("VN_RES_ETAIL",   "AA_VN_ETAIL"),
            "MAYORISTA": ("VN_RES_MAYOR",   "AA_VN_MAYOR"),
        }
        rows = []
        for canal, (vn_col, aa_col) in canal_map.items():
            vn_val = float(valid[vn_col].sum()) if vn_col in valid.columns else 0
            # AA: tomar UNA VEZ por SKU para evitar multiplicar por 12 meses
            aa_val = 0
            if aa_col in valid.columns:
                aa_sku = valid.groupby("SKU_PRODUCTO")[aa_col].first()
                aa_val = float(aa_sku.sum())
            rows.append({"CANAL": canal, "VN_2026": vn_val, "VN_AA": aa_val})
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


# ============================================================================
# BLOQUE 4 — Comex
# ============================================================================

def _fmt_money_col(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Format a numeric column as Chilean CLP with $ and dots."""
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        df[col] = df[col].apply(lambda x: f"${round(x):,}".replace(",", ".") if x > 0 else "$0")
    return df


def load_comex_atrasadas(conn) -> pd.DataFrame:
    print("  → POs atrasadas...")
    try:
        df = _query(conn, QUERY_COMEX_ATRASADAS)
        # Filtrar solo POs con ETA en el año actual
        if "ETA_CALC" in df.columns:
            df["ETA_CALC"] = pd.to_datetime(df["ETA_CALC"], errors="coerce")
            df = df[df["ETA_CALC"].dt.year >= date.today().year]
        # Ordenar por monto desc
        if "MONTOMN" in df.columns:
            df["MONTOMN"] = pd.to_numeric(df["MONTOMN"], errors="coerce").fillna(0)
            df = df.sort_values("MONTOMN", ascending=False)
        cols = [c for c in _ATRASADAS_COLS if c in df.columns]
        result = df[cols].reset_index(drop=True) if cols else df
        result = _fmt_money_col(result, "MONTOMN")
        print(f"     {len(result):,} POs atrasadas ({date.today().year}+).")
        return result
    except Exception as e:
        print(f"  ✗ {e}")
        return pd.DataFrame()


def load_comex_sin_carpeta(conn) -> pd.DataFrame:
    print("  → POs sin carpeta comex...")
    try:
        df = _query(conn, QUERY_COMEX_SIN_CARPETA)
        # Solo compras internacionales (PO empieza con "PO-"; nacionales NO aplican)
        if "PO" in df.columns:
            before = len(df)
            df = df[df["PO"].astype(str).str.startswith("PO-")]
            print(f"     Filtro internacional: {before} → {len(df)} POs (solo PO-*)")
        # Filtrar solo POs con entrega en el año actual
        if "FECHA_ENTREGA" in df.columns:
            df["FECHA_ENTREGA"] = pd.to_datetime(df["FECHA_ENTREGA"], errors="coerce")
            df = df[df["FECHA_ENTREGA"].dt.year >= date.today().year]
        # Ordenar por monto desc
        if "MONTOMN" in df.columns:
            df["MONTOMN"] = pd.to_numeric(df["MONTOMN"], errors="coerce").fillna(0)
            df = df.sort_values("MONTOMN", ascending=False)
        cols = [c for c in _SIN_CARPETA_COLS if c in df.columns]
        result = df[cols].reset_index(drop=True) if cols else df
        result = _fmt_money_col(result, "MONTOMN")
        print(f"     {len(result):,} POs sin carpeta internacionales ({date.today().year}+).")
        return result
    except Exception as e:
        print(f"  ✗ {e}")
        return pd.DataFrame()


def _filter_year_2026(df: pd.DataFrame, eta_col: str = "ETA_CALC") -> pd.DataFrame:
    """Keep only rows where the ETA/date column is year 2026."""
    if df.empty or eta_col not in df.columns:
        return df
    parsed = pd.to_datetime(df[eta_col], errors="coerce")
    return df[parsed.dt.year == 2026].reset_index(drop=True)


def _load_precios_prom(conn) -> pd.DataFrame:
    """Load average price per SKU (last 90 days) for VN_POTENCIAL calculation."""
    try:
        return _query(conn, QUERY_PRECIO_PROM_SKU)
    except Exception as e:
        print(f"     (precio prom: {e})")
        return pd.DataFrame()


def _enrich_with_vn_potencial(df: pd.DataFrame, precios: pd.DataFrame) -> pd.DataFrame:
    """Enrich a PO DataFrame with VN_POTENCIAL = QTY * avg_price_per_SKU."""
    if df.empty or "SKU_PRODUCTO" not in df.columns:
        df["VN_POTENCIAL"] = 0.0
        return df
    if precios.empty or "PRECIO_PROM_90D" not in precios.columns:
        df["VN_POTENCIAL"] = 0.0
        return df
    precios = precios.copy()
    precios["PRECIO_PROM_90D"] = pd.to_numeric(precios["PRECIO_PROM_90D"], errors="coerce").fillna(0)
    df = df.merge(precios[["SKU_PRODUCTO", "PRECIO_PROM_90D"]], on="SKU_PRODUCTO", how="left")
    df["PRECIO_PROM_90D"] = df["PRECIO_PROM_90D"].fillna(0)
    qty_col = "QTY_PENDIENTE" if "QTY_PENDIENTE" in df.columns else "CANTIDAD_FINAL_CORREGIDA"
    qty = pd.to_numeric(df.get(qty_col, 0), errors="coerce").fillna(0)
    df["VN_POTENCIAL"] = qty * df["PRECIO_PROM_90D"]
    df.drop(columns=["PRECIO_PROM_90D"], inplace=True, errors="ignore")
    return df


def load_comex_sin_factura(conn) -> pd.DataFrame:
    """Load POs in transit without invoice journal (2026 only)."""
    print("  → POs sin diario de factura...")
    try:
        df = _query(conn, QUERY_COMEX_SIN_FACTURA)
        df = _filter_year_2026(df, "ETA_CALC")
        if df.empty:
            print("     0 POs sin diario factura (2026).")
            return df
        # Format money
        if "MONTOMN" in df.columns:
            df["MONTOMN"] = pd.to_numeric(df["MONTOMN"], errors="coerce").fillna(0)
            df = df.sort_values("MONTOMN", ascending=False)
        cols = [c for c in _SIN_FACTURA_COLS if c in df.columns]
        result = df[cols].reset_index(drop=True) if cols else df
        result = _fmt_money_col(result, "MONTOMN")
        print(f"     {len(result):,} POs sin diario factura (2026).")
        return result
    except Exception as e:
        print(f"  ✗ Sin factura: {e}")
        return pd.DataFrame()


def load_comex_proximas(conn, precios: pd.DataFrame) -> pd.DataFrame:
    """Load POs approaching arrival with smart priority score (2026 only)."""
    print("  → POs próximas a llegar...")
    try:
        df = _query(conn, QUERY_COMEX_PROXIMAS)
        df = _filter_year_2026(df, "ETA_CALC")
        if df.empty:
            print("     0 POs próximas (2026).")
            return df

        # Enrich with VN_POTENCIAL
        df = _enrich_with_vn_potencial(df, precios)

        # Get MOI per SKU from stock_critico_metrics
        try:
            moi_df = _query(conn, QUERY_STOCK_CRITICO_METRICS)
            if not moi_df.empty and "MOI" in moi_df.columns:
                if "FECHA" in moi_df.columns:
                    moi_df["FECHA"] = pd.to_datetime(moi_df["FECHA"], errors="coerce")
                    moi_df = moi_df[moi_df["FECHA"] == moi_df["FECHA"].max()]
                moi_sku = moi_df.groupby("SKU_PRODUCTO", as_index=False).agg(MOI_ACTUAL=("MOI", "first"))
                moi_sku["MOI_ACTUAL"] = pd.to_numeric(moi_sku["MOI_ACTUAL"], errors="coerce").fillna(0)
                df = df.merge(moi_sku, on="SKU_PRODUCTO", how="left")
        except Exception as e:
            print(f"     (MOI lookup: {e})")

        if "MOI_ACTUAL" not in df.columns:
            df["MOI_ACTUAL"] = 0.0
        df["MOI_ACTUAL"] = df["MOI_ACTUAL"].fillna(0)

        # Calculate priority score: VN_POTENCIAL / (1 + MOI_ACTUAL)
        df["PRIORIDAD_SCORE"] = np.where(
            df["VN_POTENCIAL"] > 0,
            df["VN_POTENCIAL"] / (1 + df["MOI_ACTUAL"]),
            0.0,
        )
        df = df.sort_values("PRIORIDAD_SCORE", ascending=False).reset_index(drop=True)

        # Format money columns for display
        for mc in ["MONTOMN", "MONTO_PENDIENTE_CLP", "VN_POTENCIAL", "PRIORIDAD_SCORE"]:
            if mc in df.columns:
                df[mc] = pd.to_numeric(df[mc], errors="coerce").fillna(0)
                df[mc] = df[mc].apply(lambda x: f"${round(x):,}".replace(",", ".") if x > 0 else "$0")
        if "MOI_ACTUAL" in df.columns:
            df["MOI_ACTUAL"] = pd.to_numeric(df["MOI_ACTUAL"], errors="coerce").fillna(0).apply(
                lambda x: f"{x:.1f}m" if x > 0 else "0m"
            )

        cols = [c for c in _PROXIMAS_COLS if c in df.columns]
        result = df[cols].reset_index(drop=True) if cols else df
        print(f"     {len(result):,} POs próximas a llegar (2026).")
        return result
    except Exception as e:
        print(f"  ✗ Próximas: {e}")
        return pd.DataFrame()


# ============================================================================
# BLOQUE 5 — Plan de Compras
# ============================================================================

def load_plan_compras(conn) -> tuple[pd.DataFrame, dict]:
    print("  → Plan de compras (en tránsito)...")
    try:
        df = _query(conn, _PLAN_COMPRAS_QUERY)
        for c in ["CANTIDAD_FINAL_CORREGIDA", "MONTOMN"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        kpis = {
            "n_pos":          int(df["PO"].nunique()) if "PO" in df.columns else len(df),
            "und_transito":   float(df["CANTIDAD_FINAL_CORREGIDA"].sum()) if "CANTIDAD_FINAL_CORREGIDA" in df.columns else 0,
            "monto_transito": float(df["MONTOMN"].sum()) if "MONTOMN" in df.columns else 0,
        }
        top = df.sort_values("MONTOMN", ascending=False).head(25)
        cols = [c for c in _PLAN_COLS if c in top.columns]
        result_df = top[cols].reset_index(drop=True)
        result_df = _fmt_money_col(result_df, "MONTOMN")
        print(f"     {kpis['n_pos']:,} POs, ${kpis['monto_transito']/1e6:.1f}M CLP.")
        return result_df, kpis
    except Exception as e:
        print(f"  ✗ {e}")
        return pd.DataFrame(), {}


# ============================================================================
# BLOQUE 6 — Higiene (réplica de higiene.py)
# ============================================================================

def load_higiene(conn, proy_df: pd.DataFrame) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Réplica de _check_stock_sin_perfil y _check_forecast_coverage."""
    print("  → Higiene de abastecimiento...")
    kpis: dict = {}
    sin_fc     = pd.DataFrame()
    sin_perfil = pd.DataFrame()
    cd_stock   = pd.DataFrame()

    try:
        stock = _query(conn, QUERY_STOCK_HIGIENE)
        maestra = _query(conn, QUERY_MAESTRA)

        if stock.empty:
            return kpis, sin_fc, sin_perfil

        # ── CD sin perfil (réplica _check_stock_sin_perfil) ──────────────────
        if "CANAL_DE_DISTRIBUCION" in stock.columns and "DESCRIPCION_SUCURSAL" in stock.columns:
            stock["IS_CD_PRINCIPAL"] = (
                stock["CANAL_DE_DISTRIBUCION"].str.upper().str.contains("CD", na=False)
                & stock["DESCRIPCION_SUCURSAL"].str.upper().str.contains("PRINCIPAL", na=False)
            )
            for c in ["STOCK_UNIDADES", "PERFIL_TIENDAS"]:
                if c in stock.columns:
                    stock[c] = pd.to_numeric(stock[c], errors="coerce").fillna(0)

            cd_stock = (
                stock[stock["IS_CD_PRINCIPAL"] & (stock["STOCK_UNIDADES"] > 0)]
                .groupby("SKU_PRODUCTO", as_index=False)
                .agg(STOCK_CD_PRINCIPAL=("STOCK_UNIDADES", "sum"))
            )

            if not cd_stock.empty:
                if "PERFIL_TIENDAS" in stock.columns:
                    tienda_perfil = (
                        stock[
                            stock["CANAL_DE_DISTRIBUCION"].str.upper().str.contains("TIENDA", na=False)
                            & (stock["PERFIL_TIENDAS"] > 0)
                        ]
                        .groupby("SKU_PRODUCTO", as_index=False)
                        .agg(TIENDAS_CON_PERFIL=("ID_SUCURSAL", "nunique"))
                    )
                    merged = cd_stock.merge(tienda_perfil, on="SKU_PRODUCTO", how="left")
                else:
                    merged = cd_stock.copy()
                    merged["TIENDAS_CON_PERFIL"] = 0

                merged["TIENDAS_CON_PERFIL"] = merged["TIENDAS_CON_PERFIL"].fillna(0).astype(int)
                merged["SIN_PERFIL"] = merged["TIENDAS_CON_PERFIL"] == 0

                # Enriquecer con costo y nombre
                if not maestra.empty:
                    enrich_cols = ["SKU_PRODUCTO"] + [c for c in ["NOM_PRODUCTO", "AREA", "LINEA", "MARCA", "ULTIMO_COSTO"] if c in maestra.columns]
                    slim = maestra[enrich_cols].drop_duplicates("SKU_PRODUCTO")
                    merged = merged.merge(slim, on="SKU_PRODUCTO", how="left")
                    if "ULTIMO_COSTO" in merged.columns:
                        merged["ULTIMO_COSTO"] = pd.to_numeric(merged["ULTIMO_COSTO"], errors="coerce").fillna(0)
                        merged["VALOR_STOCK_CD"] = merged["STOCK_CD_PRINCIPAL"] * merged["ULTIMO_COSTO"]
                    else:
                        merged["VALOR_STOCK_CD"] = 0

                sin_p = merged[merged["SIN_PERFIL"]].copy()
                kpis["sin_perfil"]  = int(len(sin_p))
                kpis["valor_riesgo"] = float(sin_p["VALOR_STOCK_CD"].sum())

                show_cols = [c for c in ["SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "MARCA",
                                         "STOCK_CD_PRINCIPAL", "VALOR_STOCK_CD"] if c in sin_p.columns]
                sin_perfil = sin_p[show_cols].sort_values("VALOR_STOCK_CD", ascending=False).head(30).reset_index(drop=True)
                # Format VALOR_STOCK_CD with $ and dots for display
                sin_perfil = _fmt_money_col(sin_perfil, "VALOR_STOCK_CD")

        # ── SKUs MIX sin forecast (réplica exacta de _check_forecast_coverage) ─
        if proy_df is not None and not proy_df.empty:
            try:
                # 1. ALL MIX SKUs from maestra (NOT filtered by stock)
                mix_skus_df = pd.DataFrame()
                if "MIX_OFICIAL" in maestra.columns:
                    mix_skus_df = maestra[
                        maestra["MIX_OFICIAL"].str.strip().str.upper() == "MIX"
                    ][["SKU_PRODUCTO"]].drop_duplicates()
                print(f"     MIX maestra: {len(mix_skus_df)} SKUs")

                # 2. Aggregate forecast by SKU across ALL periods (same as portal)
                # Priority: DEMANDA_SIM_* first, then FORECAST_VENTA_* as fallback
                fc_cols_map = {
                    "DEMANDA_SIM_TIENDA": "FC_TIENDA",
                    "DEMANDA_SIM_ETAIL": "FC_ETAIL",
                    "DEMANDA_SIM_MAYOR": "FC_MAYOR",
                    "FORECAST_VENTA_MINOR": "FC_TIENDA",
                    "FORECAST_VENTA_ETAIL": "FC_ETAIL",
                    "FORECAST_VENTA_MAYOR": "FC_MAYOR",
                }
                available_fc = {}
                for s, d in fc_cols_map.items():
                    if s in proy_df.columns and d not in available_fc.values():
                        available_fc[s] = d

                if available_fc and not mix_skus_df.empty:
                    # Coerce numeric
                    for col in available_fc.keys():
                        proy_df[col] = pd.to_numeric(proy_df[col], errors="coerce").fillna(0)

                    agg_dict = {src: "sum" for src in available_fc.keys()}
                    fc_by_sku = proy_df.groupby("SKU_PRODUCTO", as_index=False).agg(agg_dict)
                    fc_by_sku = fc_by_sku.rename(columns=available_fc)

                    # Ensure all 3 channels exist
                    for col in ["FC_TIENDA", "FC_ETAIL", "FC_MAYOR"]:
                        if col not in fc_by_sku.columns:
                            fc_by_sku[col] = 0.0

                    fc_by_sku["FC_TOTAL"] = (
                        fc_by_sku["FC_TIENDA"].fillna(0)
                        + fc_by_sku["FC_ETAIL"].fillna(0)
                        + fc_by_sku["FC_MAYOR"].fillna(0)
                    )

                    # 3. LEFT JOIN: MIX SKUs ← forecast (SKUs not in parquet get FC=0)
                    df_fc = mix_skus_df.merge(fc_by_sku, on="SKU_PRODUCTO", how="left")
                    df_fc[["FC_TIENDA", "FC_ETAIL", "FC_MAYOR", "FC_TOTAL"]] = df_fc[
                        ["FC_TIENDA", "FC_ETAIL", "FC_MAYOR", "FC_TOTAL"]
                    ].fillna(0)

                    # 4. Count channels with FC >= 1
                    df_fc["N_CANALES_CON_FC"] = (
                        (df_fc["FC_TIENDA"] >= 1).astype(int)
                        + (df_fc["FC_ETAIL"] >= 1).astype(int)
                        + (df_fc["FC_MAYOR"] >= 1).astype(int)
                    )

                    # 5. Classify (same as portal)
                    df_fc["ISSUE"] = np.select(
                        [df_fc["FC_TOTAL"] < 1, df_fc["N_CANALES_CON_FC"] == 1],
                        ["SIN_FORECAST", "FORECAST_1_CANAL"],
                        default="OK",
                    )

                    sin_fc_count = int((df_fc["ISSUE"] == "SIN_FORECAST").sum())
                    fc_1_count = int((df_fc["ISSUE"] == "FORECAST_1_CANAL").sum())
                    kpis["sin_forecast"] = sin_fc_count
                    kpis["fc_1_canal"] = fc_1_count
                    print(f"     SIN_FORECAST: {sin_fc_count}, FC_1_CANAL: {fc_1_count}, OK: {(df_fc['ISSUE'] == 'OK').sum()}")

                    # 6. Build table of SKUs sin forecast (sin columnas de stock — no aplica)
                    sin_fc_df = df_fc[df_fc["ISSUE"] == "SIN_FORECAST"].copy()
                    if not sin_fc_df.empty and not maestra.empty:
                        enrich_cols2 = ["SKU_PRODUCTO"] + [c for c in ["NOM_PRODUCTO", "AREA", "LINEA", "MARCA"] if c in maestra.columns]
                        slim2 = maestra[enrich_cols2].drop_duplicates("SKU_PRODUCTO")
                        sin_fc_df = sin_fc_df.merge(slim2, on="SKU_PRODUCTO", how="left")
                        # Only show identification columns — stock columns are irrelevant here
                        show_cols2 = [c for c in ["SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "MARCA"]
                                      if c in sin_fc_df.columns]
                        sin_fc = sin_fc_df[show_cols2].reset_index(drop=True)

            except Exception as e:
                print(f"     (forecast check: {e})")
                import traceback
                traceback.print_exc()

        print(f"     sin_perfil={kpis.get('sin_perfil',0)}, sin_forecast={kpis.get('sin_forecast',0)}, "
              f"fc_1_canal={kpis.get('fc_1_canal',0)}, riesgo=${kpis.get('valor_riesgo',0)/1e6:.1f}M")

    except Exception as e:
        print(f"  ✗ Higiene: {e}")

    return kpis, sin_fc, sin_perfil


# ============================================================================
# BLOQUE 7 — InStock Disponibilidad
# ============================================================================

def load_instock_kpis(conn) -> dict:
    """IS% Tienda y CD (90d) últimos 14 días + delta 7d + breakdown por área."""
    print("  → InStock KPIs (14d)...")
    kpis: dict = {}
    try:
        df_t = _query(conn, _QUERY_INSTOCK_EMAIL_TIENDA)
        df_cd = _query(conn, _QUERY_INSTOCK_EMAIL_CD)

        for c in ["N_PERFIL", "TIENDAS_IS", "CD_FILTER"]:
            if c in df_t.columns:
                df_t[c] = pd.to_numeric(df_t[c], errors="coerce").fillna(0)
        for c in ["N_SKUS", "SKUS_IS"]:
            if c in df_cd.columns:
                df_cd[c] = pd.to_numeric(df_cd[c], errors="coerce").fillna(0)

        if "FECHA" in df_t.columns:
            df_t["FECHA"] = pd.to_datetime(df_t["FECHA"], errors="coerce")
        if "FECHA" in df_cd.columns:
            df_cd["FECHA"] = pd.to_datetime(df_cd["FECHA"], errors="coerce")

        # ── IS% Tienda (latest vs 7d ago) ─────────────────────────────────
        if not df_t.empty:
            latest_t = df_t["FECHA"].max()
            ago_7d = latest_t - pd.Timedelta(days=7)
            # Find closest date to 7d ago
            all_dates = sorted(df_t["FECHA"].unique())
            ago_date = min(all_dates, key=lambda d: abs(d - ago_7d))

            def _is_pct_tienda(fecha_filter):
                sub = df_t[df_t["FECHA"] == fecha_filter]
                n = sub["N_PERFIL"].sum()
                s = sub["TIENDAS_IS"].sum()
                return (s / n * 100) if n > 0 else 0

            kpis["is_tienda_pct"] = _is_pct_tienda(latest_t)
            is_t_7d = _is_pct_tienda(ago_date)
            kpis["delta_7d_tienda"] = kpis["is_tienda_pct"] - is_t_7d

            # Per-area breakdown (latest date)
            area_t = df_t[df_t["FECHA"] == latest_t]
            area_breakdown: dict = {}
            if "AREA" in area_t.columns:
                for area, grp in area_t.groupby("AREA"):
                    n = grp["N_PERFIL"].sum()
                    s = grp["TIENDAS_IS"].sum()
                    area_breakdown[str(area)] = {"tienda": (s / n * 100) if n > 0 else 0}
        else:
            kpis["is_tienda_pct"] = 0
            kpis["delta_7d_tienda"] = 0
            area_breakdown = {}

        # ── IS% CD (latest vs 7d ago) ─────────────────────────────────────
        if not df_cd.empty:
            latest_cd = df_cd["FECHA"].max()
            ago_7d_cd = latest_cd - pd.Timedelta(days=7)
            all_dates_cd = sorted(df_cd["FECHA"].unique())
            ago_date_cd = min(all_dates_cd, key=lambda d: abs(d - ago_7d_cd))

            def _is_pct_cd(fecha_filter):
                sub = df_cd[df_cd["FECHA"] == fecha_filter]
                n = sub["N_SKUS"].sum()
                s = sub["SKUS_IS"].sum()
                return (s / n * 100) if n > 0 else 0

            kpis["is_cd_pct"] = _is_pct_cd(latest_cd)
            is_cd_7d = _is_pct_cd(ago_date_cd)
            kpis["delta_7d_cd"] = kpis["is_cd_pct"] - is_cd_7d

            # Merge CD into area breakdown
            area_cd = df_cd[df_cd["FECHA"] == latest_cd]
            if "AREA" in area_cd.columns:
                for area, grp in area_cd.groupby("AREA"):
                    n = grp["N_SKUS"].sum()
                    s = grp["SKUS_IS"].sum()
                    if str(area) not in area_breakdown:
                        area_breakdown[str(area)] = {}
                    area_breakdown[str(area)]["cd"] = (s / n * 100) if n > 0 else 0
        else:
            kpis["is_cd_pct"] = 0
            kpis["delta_7d_cd"] = 0

        # Top 2 areas by tienda IS% (lowest → most critical)
        sorted_areas = sorted(area_breakdown.items(), key=lambda x: x[1].get("tienda", 100))
        kpis["por_area"] = dict(sorted_areas[:4])

        print(f"     IS Tienda: {kpis['is_tienda_pct']:.1f}% (Δ7d: {kpis['delta_7d_tienda']:+.1f}pp) · "
              f"IS CD: {kpis['is_cd_pct']:.1f}% (Δ7d: {kpis['delta_7d_cd']:+.1f}pp)")

    except Exception as e:
        print(f"  ✗ InStock KPIs: {e}")
        import traceback
        traceback.print_exc()
    return kpis


def load_instock_evolution(conn) -> pd.DataFrame:
    """Daily IS% Tienda + CD for the last 14 days (for line chart)."""
    print("  → InStock evolution (14d)...")
    try:
        df_t = _query(conn, _QUERY_INSTOCK_EMAIL_TIENDA)
        df_cd = _query(conn, _QUERY_INSTOCK_EMAIL_CD)

        for c in ["N_PERFIL", "TIENDAS_IS"]:
            if c in df_t.columns:
                df_t[c] = pd.to_numeric(df_t[c], errors="coerce").fillna(0)
        for c in ["N_SKUS", "SKUS_IS"]:
            if c in df_cd.columns:
                df_cd[c] = pd.to_numeric(df_cd[c], errors="coerce").fillna(0)

        # Aggregate tienda by date
        t_by_date = df_t.groupby("FECHA").agg(
            n=("N_PERFIL", "sum"), s=("TIENDAS_IS", "sum"),
        ).reset_index()
        t_by_date["IS_TIENDA_PCT"] = np.where(
            t_by_date["n"] > 0, t_by_date["s"] / t_by_date["n"] * 100, 0,
        )

        # Aggregate CD by date
        cd_by_date = df_cd.groupby("FECHA").agg(
            n=("N_SKUS", "sum"), s=("SKUS_IS", "sum"),
        ).reset_index()
        cd_by_date["IS_CD_PCT"] = np.where(
            cd_by_date["n"] > 0, cd_by_date["s"] / cd_by_date["n"] * 100, 0,
        )

        # Merge
        evo = t_by_date[["FECHA", "IS_TIENDA_PCT"]].merge(
            cd_by_date[["FECHA", "IS_CD_PCT"]], on="FECHA", how="outer",
        ).fillna(0).sort_values("FECHA")

        evo["FECHA"] = pd.to_datetime(evo["FECHA"], errors="coerce")
        print(f"     {len(evo)} días de evolución.")
        return evo

    except Exception as e:
        print(f"  ✗ InStock evolution: {e}")
        return pd.DataFrame()


# ============================================================================
# EXCEL ATTACHMENT
# ============================================================================

def build_excel(
    comex_atrasadas: pd.DataFrame,
    comex_sin_carpeta: pd.DataFrame,
    plan_compras: pd.DataFrame,
    sin_perfil: pd.DataFrame,
    sin_forecast: pd.DataFrame,
    comex_sin_factura: pd.DataFrame | None = None,
    comex_proximas: pd.DataFrame | None = None,
) -> bytes:
    from io import BytesIO
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in [
            ("POs Atrasadas",   comex_atrasadas),
            ("POs sin Carpeta", comex_sin_carpeta),
            ("Sin Diario Fact", comex_sin_factura),
            ("POs Proximas",    comex_proximas),
            ("Plan Compras",    plan_compras),
            ("CD sin Perfil",   sin_perfil),
            ("Sin Forecast",    sin_forecast),
        ]:
            if df is not None and not df.empty:
                df.to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 60)
    print(f"Alertas Supply Chain Dorel — {date.today().strftime('%d/%m/%Y')}")
    print("=" * 60)

    missing = [v for v in [
        "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_ACCOUNT",
        "OUTLOOK_EMAIL", "OUTLOOK_PASSWORD", "ALERT_RECIPIENTS",
    ] if not os.getenv(v)]
    if missing:
        print(f"\n✗ Faltan: {', '.join(missing)}")
        sys.exit(1)

    recipients = [r.strip() for r in os.environ["ALERT_RECIPIENTS"].replace(";", ",").split(",") if r.strip() and "@" in r]
    if not recipients:
        print("\n✗ ALERT_RECIPIENTS vacío.")
        sys.exit(1)
    print(f"\nDestinatarios: {', '.join(recipients)}")

    # ── Conectar ─────────────────────────────────────────────────────────────
    print("\n[1/4] Conectando a Snowflake...")
    try:
        conn = _connect()
        conn.cursor().execute("SELECT 1")
        print("  ✓ Conexión OK.")
    except Exception as e:
        print(f"  ✗ {e}")
        sys.exit(1)

    # ── Cargar datos ─────────────────────────────────────────────────────────
    print("\n[2/4] Cargando datos...")

    stock_kpis    = load_stock_kpis(conn)
    ventas_ytd    = load_ventas_ytd(conn)
    ventas_mtd    = load_ventas_mtd(conn)
    critico_kpis, critico_matrix = load_critico_data(conn)

    print("  → Proyección financiera...")
    proy_df    = load_proy_data()
    budget_df  = load_budget()
    proy_res   = build_proy_resumen(proy_df, budget_df)
    proy_area  = build_proy_por_area(proy_df)
    proy_canal = build_proy_por_canal(proy_df)

    # MOI Compañía (necesita stock_costo_total + proy_df)
    moi_kpis = load_moi_kpis(conn, stock_kpis.get("stock_costo", 0), proy_df)

    comex_atrasadas   = load_comex_atrasadas(conn)
    comex_sin_carpeta = load_comex_sin_carpeta(conn)

    # Load shared price data for VN_POTENCIAL enrichment
    precios_prom = _load_precios_prom(conn)

    comex_sin_factura = load_comex_sin_factura(conn)
    comex_proximas    = load_comex_proximas(conn, precios_prom)

    plan_df, plan_kpis = load_plan_compras(conn)
    higiene_kpis, sin_fc, sin_perf = load_higiene(conn, proy_df)
    instock_kpis = load_instock_kpis(conn)
    instock_evo  = load_instock_evolution(conn)

    conn.close()

    # ── Email ────────────────────────────────────────────────────────────────
    print("\n[3/4] Construyendo email HTML...")
    today_display = date.today().strftime("%d %b %Y")

    html_body = build_alert_email(
        date_str          = today_display,
        stock_kpis        = stock_kpis or None,
        moi_kpis          = moi_kpis or None,
        ventas_ytd        = ventas_ytd or None,
        ventas_mtd        = ventas_mtd or None,
        critico_kpis      = critico_kpis or None,
        critico_matrix    = critico_matrix if not critico_matrix.empty else None,
        proy_resumen      = proy_res if not proy_res.empty else None,
        proy_por_area     = proy_area if not proy_area.empty else None,
        proy_por_canal    = proy_canal if not proy_canal.empty else None,
        comex_atrasadas   = comex_atrasadas if not comex_atrasadas.empty else None,
        comex_sin_carpeta = comex_sin_carpeta if not comex_sin_carpeta.empty else None,
        comex_sin_factura = comex_sin_factura if not comex_sin_factura.empty else None,
        comex_proximas    = comex_proximas if not comex_proximas.empty else None,
        plan_compras      = plan_df if not plan_df.empty else None,
        plan_kpis         = plan_kpis or None,
        higiene_kpis      = higiene_kpis or None,
        higiene_sin_forecast = sin_fc if not sin_fc.empty else None,
        higiene_sin_perfil   = sin_perf if not sin_perf.empty else None,
        instock_kpis      = instock_kpis or None,
        instock_evolution = instock_evo if not instock_evo.empty else None,
    )

    print("[4/4] Enviando...")
    attachments = []
    try:
        excel_bytes = build_excel(
            comex_atrasadas, comex_sin_carpeta, plan_df, sin_perf, sin_fc,
            comex_sin_factura, comex_proximas,
        )
        fname = f"dorel_alertas_{date.today().strftime('%Y%m%d')}.xlsx"
        attachments.append((fname, excel_bytes))
        print(f"  ✓ Excel: {fname}")
    except Exception as e:
        print(f"  ✗ Excel: {e}")

    port_raw = os.getenv("SMTP_PORT", "587")
    cfg = get_smtp_config({
        "email":    os.getenv("OUTLOOK_EMAIL"),
        "password": os.getenv("OUTLOOK_PASSWORD"),
        "server":   os.getenv("SMTP_SERVER", "smtp.gmail.com"),
        "port":     int(port_raw) if str(port_raw).isdigit() else 587,
    })

    subject = f"Alertas Supply Chain Dorel — {today_display}"
    ok, msg = send_alert_email(subject, html_body, recipients, cfg, attachments or None)

    print(f"\n{'✓' if ok else '✗'} {msg}")
    print("=" * 60)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
