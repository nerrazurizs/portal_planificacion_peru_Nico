"""Alertas Email — módulo Streamlit para enviar reportes de supply chain por correo.

Secciones incluidas en el email:
  1. Resumen Ejecutivo S&OP  (inventario KPIs + VN/Aporte YTD vs Budget/AA)
  2. Stock crítico           (matriz 6×6 + waterfall + tabla top SKUs)
  3. Proyección financiera   (tabla anual + gráficos área/canal vs AA, desde parquet)
  4. POs atrasadas / POs sin carpeta comex
  5. Plan de compras         (próximas llegadas en tránsito)
  6. Higiene de abastecimiento (SKUs sin forecast, sin perfil tiendas)

Credenciales SMTP: leer de .env (OUTLOOK_EMAIL, OUTLOOK_PASSWORD, SMTP_SERVER, SMTP_PORT).
Si no están en .env, el usuario puede ingresarlas directamente en la UI.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import streamlit as st

from db.cache import cached_query as cq
from db.queries import (
    QUERY_COMEX_ATRASADAS, QUERY_COMEX_SIN_CARPETA,
    QUERY_COMEX_SIN_FACTURA, QUERY_COMEX_PROXIMAS,
    QUERY_STOCK_CRITICO_DETAIL,
    _COMPRAS,
)
from utils.budget import load_budget
from utils.email_sender import build_alert_email, get_smtp_config, send_alert_email
from config import apply_pm_filter
from utils.filters import human_format, norm_cols

ROOT = Path(__file__).resolve().parent.parent

# ── Columns shown in the email tables ────────────────────────────────────────
_CRITICO_COLS = [
    "SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "MARCA",
    "STOCK_UNIDADES", "STOCK_COSTO", "MOI", "RANGO_MOI",
    "ANTIGUEDAD_MESES", "RANGO_ANTIGUEDAD",
]
_ATRASADAS_COLS = [
    "PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "NOM_PROVEEDOR", "CARPETA_COMEX",
    "ETA_CALC", "DIAS_ATRASO", "QTY_PENDIENTE", "MONTOMN", "VN_POTENCIAL",
]
_SIN_CARPETA_COLS = [
    "PO", "NOM_PRODUCTO", "NOM_PROVEEDOR", "FECHA_ENTREGA",
    "DIAS_DESDE_ENTREGA", "CANTIDAD_FINAL_CORREGIDA", "MONTOMN",
]
_PLAN_COLS = [
    "PO", "NOM_PRODUCTO", "NOM_PROVEEDOR", "CARPETA_COMEX",
    "ETA_CALC", "CANTIDAD_FINAL_CORREGIDA", "MONTOMN",
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

MOI_THRESHOLD        = 8
ANTIGUEDAD_THRESHOLD = 8

# ── Plan compras query (standalone, no Streamlit cache) ───────────────────────
# Peru: usa _COMPRAS wrapper que normaliza columnas de ft_compras
_PLAN_COMPRAS_QUERY = f"""
SELECT
    po, nom_producto, nom_proveedor, carpeta_comex,
    eta AS ETA_CALC,
    cantidad_final_corregida, montomn
FROM {_COMPRAS}
WHERE fecha_recepcion_en_cd IS NULL AND cantidad_final_corregida > 0
ORDER BY eta ASC LIMIT 200
"""


# ============================================================================
# DATA LOADING
# ============================================================================

def _load_stock_kpis(conn) -> dict:
    df = cq.stock_onhand(conn)
    if df.empty:
        return {}
    for c in ["STOCK_CD", "STOCK_TIENDA", "STOCK_TOTAL", "STOCK_COSTO_TOTAL"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return {
        "stock_cd":     df["STOCK_CD"].sum()           if "STOCK_CD" in df.columns else 0,
        "stock_tienda": df["STOCK_TIENDA"].sum()       if "STOCK_TIENDA" in df.columns else 0,
        "stock_total":  df["STOCK_TOTAL"].sum()        if "STOCK_TOTAL" in df.columns else 0,
        "stock_costo":  df["STOCK_COSTO_TOTAL"].sum()  if "STOCK_COSTO_TOTAL" in df.columns else 0,
    }


def _load_ventas_kpis(conn) -> dict:
    df = cq.dashboard_ventas_mtd(conn)
    if df.empty:
        return {}
    for c in ["CANTIDAD_MTD", "NETO_MTD"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    result = {}
    canal_col = "COD_CANAL" if "COD_CANAL" in df.columns else df.columns[0]
    for _, row in df.iterrows():
        canal = str(row.get(canal_col, "TOTAL")).upper()
        result[canal] = {
            "und":  float(row.get("CANTIDAD_MTD", 0)),
            "neto": float(row.get("NETO_MTD", 0)),
        }
    return result


def _build_kpis_ytd(proy_df: Optional[pd.DataFrame]) -> dict:
    """YTD actuals vs Budget vs AA from session_state parquet."""
    kpis: dict = {}
    try:
        if proy_df is None or proy_df.empty:
            return kpis
        today = pd.Timestamp(date.today())
        ytd = proy_df[
            proy_df["TIPO_DATO"].isin(["HISTORICO", "REAL+FC"])
            & (proy_df["PERIODO"] <= today)
        ].copy()
        for col in ["VN_RES_TOTAL", "APORTE_RES_TOTAL", "AA_VN_TOTAL", "AA_APORTE_TOTAL"]:
            if col in ytd.columns:
                ytd[col] = pd.to_numeric(ytd[col], errors="coerce").fillna(0)
        vn_r    = float(ytd["VN_RES_TOTAL"].sum())    if "VN_RES_TOTAL" in ytd.columns else 0
        ap_r    = float(ytd["APORTE_RES_TOTAL"].sum()) if "APORTE_RES_TOTAL" in ytd.columns else 0
        vn_aa   = float(ytd["AA_VN_TOTAL"].sum())     if "AA_VN_TOTAL" in ytd.columns else 0
        ap_aa   = float(ytd["AA_APORTE_TOTAL"].sum()) if "AA_APORTE_TOTAL" in ytd.columns else 0
        kpis.update({"vn_real": vn_r, "aporte_real": ap_r, "vn_aa": vn_aa,
                     "aporte_aa": ap_aa, "margen_real": (ap_r / vn_r) if vn_r > 0 else 0})
        # Budget
        try:
            budget_df = load_budget()
            if not budget_df.empty:
                budget_df["PERIODO"] = pd.to_datetime(budget_df["PERIODO"], errors="coerce")
                bdgt = budget_df[(budget_df["CANAL"].str.upper() == "TOTAL") & (budget_df["PERIODO"] <= today)]
                kpis["vn_budget"]     = float(bdgt["VN_BUDGET"].sum())
                kpis["aporte_budget"] = float(bdgt["APORTE_BUDGET"].sum())
        except Exception:
            pass
    except Exception:
        pass
    return kpis


def _load_stock_critico(conn, moi_threshold: float, ant_threshold: float) -> tuple[dict, pd.DataFrame]:
    """Returns (critico_kpis, matrix_df) with same logic as stock_critico.py."""
    _TIERS = ["0-3m", "3-6m", "6-8m", "8-12m", "12-24m", ">=24m"]

    df = cq.stock_critico_metrics(conn)
    if df.empty:
        return {}, pd.DataFrame()
    for c in ["STOCK_COSTO", "STOCK_UNIDADES", "MOI", "ANTIGUEDAD_MESES", "COSTO_PROM_90_CIA"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Latest date only
    if "FECHA" in df.columns:
        df = df[df["FECHA"] == df["FECHA"].max()]

    # Aggregate to SKU level
    dim_cols = [c for c in ["AREA", "LINEA", "SUBLINEA", "MARCA", "NOM_PRODUCTO"] if c in df.columns]
    agg = {"STOCK_COSTO": "sum"}
    if "STOCK_UNIDADES" in df.columns: agg["STOCK_UNIDADES"] = "sum"
    if "COSTO_PROM_90_CIA" in df.columns: agg["COSTO_PROM_90_CIA"] = "first"
    if "ANTIGUEDAD_MESES" in df.columns: agg["ANTIGUEDAD_MESES"] = "max"
    if "MOI" in df.columns: agg["MOI"] = "max"
    for c in dim_cols: agg[c] = "first"

    sku = df.groupby("SKU_PRODUCTO", as_index=False).agg(agg)
    if "COSTO_PROM_90_CIA" in sku.columns:
        sku["MOI"] = np.where(sku["COSTO_PROM_90_CIA"] > 0,
                              (sku["STOCK_COSTO"] / sku["COSTO_PROM_90_CIA"]) / 30.44, 0)
    sku.loc[(sku["MOI"] <= 0) & (sku["STOCK_COSTO"] > 0), "MOI"] = 999

    # Classify tiers
    def _classify(s):
        conds = [s < 3, (s >= 3) & (s < 6), (s >= 6) & (s < 8), (s >= 8) & (s < 12), (s >= 12) & (s < 24), s >= 24]
        return pd.Series(np.select(conds, _TIERS, default="Sin Info"), index=s.index)

    sku["TIER_MOI"] = _classify(sku["MOI"])
    sku["TIER_ANTIGUEDAD"] = _classify(sku["ANTIGUEDAD_MESES"])

    valid = sku[(sku["TIER_ANTIGUEDAD"] != "Sin Info") & (sku["ANTIGUEDAD_MESES"] > 0)].copy()
    if valid.empty:
        return {}, pd.DataFrame()

    total_stock = valid["STOCK_COSTO"].sum()
    mask_sano = valid["TIER_MOI"].isin(["0-3m", "3-6m"]) & valid["TIER_ANTIGUEDAD"].isin(["0-3m", "3-6m"])
    mask_apunto = valid["TIER_MOI"].isin(["8-12m", ">=24m"]) & valid["TIER_ANTIGUEDAD"].isin(["8-12m"])
    mask_crit = valid["TIER_MOI"].isin(["12-24m", ">=24m"]) & valid["TIER_ANTIGUEDAD"].isin(["12-24m", ">=24m"])
    mask_liq = (valid["TIER_MOI"] == ">=24m") & (valid["TIER_ANTIGUEDAD"] == ">=24m")

    kpis = {
        "total_skus": int(valid["SKU_PRODUCTO"].nunique()),
        "stock_costo": float(total_stock),
        "pct_saludable": (valid.loc[mask_sano, "STOCK_COSTO"].sum() / total_stock * 100) if total_stock > 0 else 0,
        "stock_apunto": float(valid.loc[mask_apunto, "STOCK_COSTO"].sum()),
        "stock_critico": float(valid.loc[mask_crit, "STOCK_COSTO"].sum()),
        "stock_liquidacion": float(valid.loc[mask_liq, "STOCK_COSTO"].sum()),
    }

    # Build 6×6 matrix
    all_combos = pd.DataFrame([{"TIER_MOI": m, "TIER_ANTIGUEDAD": a} for m in _TIERS for a in _TIERS])
    grp = valid.groupby(["TIER_MOI", "TIER_ANTIGUEDAD"], as_index=False).agg(
        N_SKUS=("SKU_PRODUCTO", "nunique"), STOCK_COSTO=("STOCK_COSTO", "sum"))
    matrix = all_combos.merge(grp, on=["TIER_MOI", "TIER_ANTIGUEDAD"], how="left").fillna(0)
    matrix["N_SKUS"] = matrix["N_SKUS"].astype(int)

    return kpis, matrix


def _build_proy_data(proy_df: Optional[pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns (resumen_anual, por_area, por_canal) from parquet data."""
    empty = (pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    try:
        if proy_df is None or proy_df.empty:
            return empty
        for c in ["VN_RES_TOTAL", "APORTE_RES_TOTAL", "COGS_RES_TOTAL",
                  "AA_VN_TOTAL", "AA_APORTE_TOTAL"]:
            if c in proy_df.columns:
                proy_df[c] = pd.to_numeric(proy_df[c], errors="coerce").fillna(0)

        valid = proy_df[proy_df["TIPO_DATO"].isin(["HISTORICO", "REAL+FC", "PROYECCION"])]

        # ── Resumen anual ─────────────────────────────────────────────────────
        grp = valid.groupby("PERIODO_ANO").agg(
            VN=("VN_RES_TOTAL", "sum"), COGS=("COGS_RES_TOTAL", "sum"),
            APORTE=("APORTE_RES_TOTAL", "sum"),
        ).reset_index()
        grp["MARGEN"] = np.where(grp["VN"] > 0, grp["APORTE"] / grp["VN"], 0)
        grp = grp[grp["PERIODO_ANO"] >= 2026].copy()
        try:
            budget_df = load_budget()
            if not budget_df.empty:
                budget_df["PERIODO"] = pd.to_datetime(budget_df["PERIODO"], errors="coerce")
                budget_df["ANO"] = budget_df["PERIODO"].dt.year
                bdgt = (budget_df[budget_df["CANAL"].str.upper() == "TOTAL"]
                        .groupby("ANO").agg(VN_BUDGET=("VN_BUDGET", "sum"),
                                            APORTE_BUDGET=("APORTE_BUDGET", "sum")).reset_index())
                grp = grp.merge(bdgt, left_on="PERIODO_ANO", right_on="ANO", how="left")
        except Exception:
            pass
        display = pd.DataFrame()
        display["Año"] = grp["PERIODO_ANO"].astype(int)
        display["VN Proyectada"] = grp["VN"].apply(lambda x: f"${x/1e9:.2f}B" if abs(x) >= 1e9 else f"${x/1e6:.1f}M")
        display["COGS"]          = grp["COGS"].apply(lambda x: f"${x/1e9:.2f}B" if abs(x) >= 1e9 else f"${x/1e6:.1f}M")
        display["Aporte"]        = grp["APORTE"].apply(lambda x: f"${x/1e9:.2f}B" if abs(x) >= 1e9 else f"${x/1e6:.1f}M")
        display["Margen %"]      = grp["MARGEN"].apply(lambda x: f"{x:.1%}")
        if "VN_BUDGET" in grp.columns:
            display["Budget VN"] = grp["VN_BUDGET"].apply(lambda x: f"${x/1e9:.2f}B" if abs(x) >= 1e9 else f"${x/1e6:.1f}M")

        # ── Por área ───────────────────────────────────────────────────────────
        valid2026 = valid[valid["PERIODO_ANO"] == 2026]
        por_area = pd.DataFrame()
        if "AREA" in valid2026.columns:
            por_area = valid2026.groupby("AREA").agg(
                VN_2026=("VN_RES_TOTAL", "sum"), VN_AA=("AA_VN_TOTAL", "sum"),
            ).reset_index()
            por_area = por_area[por_area["VN_2026"] > 0].sort_values("VN_2026", ascending=False).head(10)

        # ── Por canal ──────────────────────────────────────────────────────────
        canal_map = {
            "TIENDA":    ("VN_RES_TIENDA",  "AA_VN_TIENDA"),
            "ETAIL":     ("VN_RES_ETAIL",   "AA_VN_ETAIL"),
            "MAYORISTA": ("VN_RES_MAYOR",   "AA_VN_MAYOR"),
        }
        rows = []
        for canal, (vn_col, aa_col) in canal_map.items():
            rows.append({
                "CANAL":   canal,
                "VN_2026": float(valid2026[vn_col].sum()) if vn_col in valid2026.columns else 0,
                "VN_AA":   float(valid2026[aa_col].sum()) if aa_col in valid2026.columns else 0,
            })
        por_canal = pd.DataFrame(rows)

        return display, por_area, por_canal
    except Exception:
        return empty


def _enrich_with_vn_potencial(df: pd.DataFrame, conn) -> pd.DataFrame:
    """Enrich a PO DataFrame with VN_POTENCIAL = QTY * avg_price_per_SKU."""
    if df.empty or "SKU_PRODUCTO" not in df.columns:
        df["VN_POTENCIAL"] = 0.0
        return df
    precios = cq.precio_prom_sku(conn)
    if precios.empty or "PRECIO_PROM_90D" not in precios.columns:
        df["VN_POTENCIAL"] = 0.0
        return df
    precios["PRECIO_PROM_90D"] = pd.to_numeric(precios["PRECIO_PROM_90D"], errors="coerce").fillna(0)
    df = df.merge(precios[["SKU_PRODUCTO", "PRECIO_PROM_90D"]], on="SKU_PRODUCTO", how="left")
    df["PRECIO_PROM_90D"] = df["PRECIO_PROM_90D"].fillna(0)
    qty_col = "QTY_PENDIENTE" if "QTY_PENDIENTE" in df.columns else "CANTIDAD_FINAL_CORREGIDA"
    qty = pd.to_numeric(df.get(qty_col, 0), errors="coerce").fillna(0)
    df["VN_POTENCIAL"] = qty * df["PRECIO_PROM_90D"]
    df.drop(columns=["PRECIO_PROM_90D"], inplace=True, errors="ignore")
    return df


def _filter_year_2026(df: pd.DataFrame, eta_col: str = "ETA_CALC") -> pd.DataFrame:
    """Keep only rows where the ETA/fecha column is year 2026."""
    if df.empty or eta_col not in df.columns:
        return df
    parsed = pd.to_datetime(df[eta_col], errors="coerce")
    return df[parsed.dt.year == 2026].reset_index(drop=True)


def _load_comex_atrasadas(conn) -> pd.DataFrame:
    """Load late POs enriched with VN_POTENCIAL, sorted by VN_POTENCIAL desc."""
    try:
        df = pd.read_sql(QUERY_COMEX_ATRASADAS, conn)
        df = norm_cols(df)
        if df.empty:
            return df
        df = _filter_year_2026(df, "ETA_CALC")
        if df.empty:
            return df
        df = _enrich_with_vn_potencial(df, conn)
        df = df.sort_values("VN_POTENCIAL", ascending=False).reset_index(drop=True)
        cols = [c for c in _ATRASADAS_COLS if c in df.columns]
        return df[cols].reset_index(drop=True) if cols else df
    except Exception:
        return pd.DataFrame()


def _load_comex_sin_carpeta(conn) -> pd.DataFrame:
    try:
        df = pd.read_sql(QUERY_COMEX_SIN_CARPETA, conn)
        df = norm_cols(df)
        cols = [c for c in _SIN_CARPETA_COLS if c in df.columns]
        return df[cols].reset_index(drop=True) if cols else df
    except Exception:
        return pd.DataFrame()


def _load_comex_sin_factura(conn) -> pd.DataFrame:
    """Load POs in transit without invoice journal (2026 only)."""
    try:
        df = pd.read_sql(QUERY_COMEX_SIN_FACTURA, conn)
        df = norm_cols(df)
        df = _filter_year_2026(df, "ETA_CALC")
        cols = [c for c in _SIN_FACTURA_COLS if c in df.columns]
        return df[cols].reset_index(drop=True) if cols else df
    except Exception:
        return pd.DataFrame()


def _load_comex_proximas(conn) -> pd.DataFrame:
    """Load POs approaching arrival with smart priority score (2026 only)."""
    try:
        df = pd.read_sql(QUERY_COMEX_PROXIMAS, conn)
        df = norm_cols(df)
        df = _filter_year_2026(df, "ETA_CALC")
        if df.empty:
            return df
        df = _enrich_with_vn_potencial(df, conn)
        # Get MOI per SKU
        moi_df = cq.stock_critico_metrics(conn)
        if not moi_df.empty and "MOI" in moi_df.columns:
            if "FECHA" in moi_df.columns:
                moi_df["FECHA"] = pd.to_datetime(moi_df["FECHA"], errors="coerce")
                moi_df = moi_df[moi_df["FECHA"] == moi_df["FECHA"].max()]
            moi_sku = moi_df.groupby("SKU_PRODUCTO", as_index=False).agg(MOI_ACTUAL=("MOI", "first"))
            moi_sku["MOI_ACTUAL"] = pd.to_numeric(moi_sku["MOI_ACTUAL"], errors="coerce").fillna(0)
            df = df.merge(moi_sku, on="SKU_PRODUCTO", how="left")
        if "MOI_ACTUAL" not in df.columns:
            df["MOI_ACTUAL"] = 0.0
        df["MOI_ACTUAL"] = df["MOI_ACTUAL"].fillna(0)
        df["PRIORIDAD_SCORE"] = np.where(
            df["VN_POTENCIAL"] > 0,
            df["VN_POTENCIAL"] / (1 + df["MOI_ACTUAL"]),
            0.0,
        )
        df = df.sort_values("PRIORIDAD_SCORE", ascending=False).reset_index(drop=True)
        cols = [c for c in _PROXIMAS_COLS if c in df.columns]
        return df[cols].reset_index(drop=True) if cols else df
    except Exception:
        return pd.DataFrame()


def _load_plan_compras(conn) -> tuple[pd.DataFrame, dict]:
    try:
        df = pd.read_sql(_PLAN_COMPRAS_QUERY, conn)
        df = norm_cols(df)
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
        return top[cols].reset_index(drop=True), kpis
    except Exception:
        return pd.DataFrame(), {}


def _build_higiene_resumen(conn, proy_df: Optional[pd.DataFrame]) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Returns (resumen, sin_forecast_df, sin_perfil_df)."""
    result: dict = {}
    sin_fc   = pd.DataFrame()
    sin_perf = pd.DataFrame()
    try:
        from modules.higiene import _check_stock_sin_perfil
        stock   = cq.stock_higiene(conn)
        maestra = cq.maestra(conn)
        perfil_df = _check_stock_sin_perfil(stock, maestra)
        if not perfil_df.empty and "SIN_PERFIL" in perfil_df.columns:
            sin_p = perfil_df[perfil_df["SIN_PERFIL"]]
            result["sin_perfil"]  = int(len(sin_p))
            result["valor_riesgo"] = float(sin_p["VALOR_STOCK_CD"].sum() if "VALOR_STOCK_CD" in sin_p.columns else 0)
            sin_perf = sin_p.head(30)
    except Exception:
        pass
    try:
        if proy_df is not None and not proy_df.empty:
            from modules.higiene import _check_forecast_coverage
            maestra = cq.maestra(conn)
            fc_df = _check_forecast_coverage(maestra, proy_df)
            if not fc_df.empty and "ISSUE" in fc_df.columns:
                result["sin_forecast"] = int((fc_df["ISSUE"] == "SIN_FORECAST").sum())
                result["fc_1_canal"]   = int((fc_df["ISSUE"] == "FORECAST_1_CANAL").sum())
                sin_fc = fc_df[fc_df["ISSUE"] == "SIN_FORECAST"].head(30)
    except Exception:
        pass
    return result, sin_fc, sin_perf


# ============================================================================
# EXCEL ATTACHMENT
# ============================================================================

def _build_excel_attachment(
    stock_critico:   Optional[pd.DataFrame],
    comex_atrasadas: Optional[pd.DataFrame],
    comex_sin_carpeta: Optional[pd.DataFrame],
    plan_compras:    Optional[pd.DataFrame] = None,
    sin_perfil:      Optional[pd.DataFrame] = None,
    sin_forecast:    Optional[pd.DataFrame] = None,
    comex_sin_factura: Optional[pd.DataFrame] = None,
    comex_proximas:    Optional[pd.DataFrame] = None,
) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet_name, df in [
            ("Stock Critico",   stock_critico),
            ("POs Atrasadas",   comex_atrasadas),
            ("POs sin Carpeta", comex_sin_carpeta),
            ("Sin Diario Fact", comex_sin_factura),
            ("POs Proximas",    comex_proximas),
            ("Plan Compras",    plan_compras),
            ("CD sin Perfil",   sin_perfil),
            ("Sin Forecast",    sin_forecast),
        ]:
            if df is not None and not df.empty:
                df.to_excel(writer, sheet_name=sheet_name, index=False)
    return buf.getvalue()


# ============================================================================
# MAIN RENDER
# ============================================================================

def render_alertas_email(conn):
    """Main entry point for the email alerts module."""
    st.html("<h2 class='sub-header'>Alertas por Email</h2>")
    st.caption(
        "Genera y envía reportes enriquecidos de supply chain por correo. "
        "Incluye KPIs ejecutivos, proyección financiera, stock crítico, comex y higiene."
    )

    # ── SMTP Configuration ────────────────────────────────────────────────────
    with st.expander("⚙️ Configuración SMTP (Gmail / Outlook)", expanded=False):
        st.info(
            "Define credenciales en `.env` (OUTLOOK_EMAIL, OUTLOOK_PASSWORD, "
            "SMTP_SERVER=smtp.gmail.com, SMTP_PORT=587) o ingrésalas aquí."
        )
        c1, c2 = st.columns(2)
        with c1:
            override_email  = st.text_input("Email remitente",  placeholder="alertas@gmail.com", key="smtp_email_override")
            override_server = st.text_input("Servidor SMTP",    placeholder="smtp.gmail.com",    key="smtp_server_override")
        with c2:
            override_password = st.text_input("Contraseña / App Password", type="password",      key="smtp_password_override")
            override_port     = st.text_input("Puerto", value="587",                              key="smtp_port_override")

        if st.button("Probar conexión SMTP", key="btn_test_smtp"):
            cfg_test = get_smtp_config({
                "email":    override_email,
                "password": override_password,
                "server":   override_server,
                "port":     int(override_port) if override_port.isdigit() else 587,
            })
            with st.spinner("Probando..."):
                import smtplib
                try:
                    with smtplib.SMTP(cfg_test["server"], cfg_test["port"]) as s:
                        s.ehlo(); s.starttls(); s.login(cfg_test["email"], cfg_test["password"])
                    st.success("✅ Conexión SMTP exitosa.")
                except smtplib.SMTPAuthenticationError:
                    st.error("❌ Error de autenticación.")
                except Exception as e:
                    st.error(f"❌ Error: {e}")

    st.markdown("---")

    # ── Recipients ────────────────────────────────────────────────────────────
    st.markdown("### 📧 Destinatarios")
    recipients_raw = st.text_area(
        "Emails (uno por línea o separados por coma)",
        placeholder="camilo@dorel.cl\njuan@dorel.cl",
        height=80,
        key="email_recipients",
    )
    recipients = [r.strip() for r in recipients_raw.replace(",", "\n").split("\n") if r.strip() and "@" in r]
    if recipients:
        st.caption(f"Destinatarios: {', '.join(recipients)}")
    else:
        st.warning("Ingresa al menos un email destinatario.")

    st.markdown("---")

    # ── Secciones ─────────────────────────────────────────────────────────────
    st.markdown("### 📋 Secciones a incluir")
    c1, c2, c3 = st.columns(3)
    with c1:
        inc_stock_kpis = st.checkbox("📦 Inventario KPIs",  value=True, key="inc_stock_kpis")
        inc_ventas     = st.checkbox("💰 Ventas MTD",        value=True, key="inc_ventas")
        inc_ytd        = st.checkbox("📊 KPIs YTD vs Budget", value=True, key="inc_ytd")
    with c2:
        inc_critico    = st.checkbox("🚨 Stock crítico",     value=True, key="inc_critico")
        inc_proy       = st.checkbox("📈 Proyección financiera", value=True, key="inc_proy")
        inc_atrasadas  = st.checkbox("⚠️ POs atrasadas",     value=True, key="inc_atrasadas")
    with c3:
        inc_sin_carpeta = st.checkbox("📁 POs sin carpeta",      value=True, key="inc_sin_carpeta")
        inc_sin_factura = st.checkbox("📄 Sin diario factura",    value=True, key="inc_sin_factura")
        inc_proximas    = st.checkbox("🚢 Proximas a llegar",     value=True, key="inc_proximas")
        inc_plan        = st.checkbox("🛒 Plan de compras",       value=True, key="inc_plan")
        inc_higiene     = st.checkbox("🩺 Higiene abast.",        value=True, key="inc_higiene")

    if inc_critico:
        colt1, colt2 = st.columns(2)
        with colt1:
            moi_thresh = st.slider("Umbral MOI (meses)", 3, 24, MOI_THRESHOLD, key="critico_moi_thresh")
        with colt2:
            ant_thresh = st.slider("Umbral Antigüedad (meses)", 3, 24, ANTIGUEDAD_THRESHOLD, key="critico_ant_thresh")
    else:
        moi_thresh, ant_thresh = MOI_THRESHOLD, ANTIGUEDAD_THRESHOLD

    inc_excel = st.checkbox("📎 Adjuntar Excel con detalle completo", value=True, key="inc_excel_attachment")

    st.markdown("---")

    # ── Asunto ────────────────────────────────────────────────────────────────
    today_str = date.today().strftime("%d/%m/%Y")
    subject = st.text_input("Asunto", value=f"Alertas Supply Chain Dorel — {today_str}", key="email_subject")

    st.markdown("---")

    # ── Preview ───────────────────────────────────────────────────────────────
    st.markdown("### 🔍 Vista Previa")
    if st.button("Cargar datos y previsualizar", type="secondary", key="btn_preview_email"):
        with st.spinner("Consultando Snowflake..."):
            _load_all_and_store(conn, inc_critico, moi_thresh, ant_thresh)
        st.toast("Datos cargados.")

    if st.session_state.get("_email_data_loaded"):
        _render_data_preview()

    st.markdown("---")

    # ── Send ──────────────────────────────────────────────────────────────────
    st.markdown("### 📤 Enviar")
    if st.button("📨 Enviar Email Ahora", type="primary", key="btn_send_email", use_container_width=True):
        if not recipients:
            st.error("Ingresa al menos un destinatario.")
            st.stop()

        if not st.session_state.get("_email_data_loaded"):
            with st.spinner("Cargando datos..."):
                _load_all_and_store(
                    conn, inc_critico,
                    st.session_state.get("critico_moi_thresh", MOI_THRESHOLD),
                    st.session_state.get("critico_ant_thresh", ANTIGUEDAD_THRESHOLD),
                )

        with st.spinner("Construyendo email..."):
            today_display = date.today().strftime("%d %b %Y")

            # Retrieve session_state data
            proy_df = st.session_state.get("df_proy")
            if proy_df is not None:
                proy_df = apply_pm_filter(proy_df)
            proy_res, proy_area, proy_canal = _build_proy_data(proy_df) if inc_proy else (None, None, None)

            html_body = build_alert_email(
                date_str          = today_display,
                stock_kpis        = st.session_state.get("_email_stock_kpis")    if inc_stock_kpis else None,
                ventas_ytd        = _build_kpis_ytd(proy_df)                     if inc_ytd else None,
                ventas_mtd        = st.session_state.get("_email_ventas_kpis")   if inc_ventas else None,
                critico_kpis      = st.session_state.get("_email_critico_kpis")  if inc_critico else None,
                critico_matrix    = st.session_state.get("_email_critico_matrix") if inc_critico else None,
                proy_resumen      = proy_res,
                proy_por_area     = proy_area,
                proy_por_canal    = proy_canal,
                comex_atrasadas   = st.session_state.get("_email_comex_atrasadas")   if inc_atrasadas else None,
                comex_sin_carpeta = st.session_state.get("_email_comex_sin_carpeta") if inc_sin_carpeta else None,
                comex_sin_factura = st.session_state.get("_email_comex_sin_factura") if inc_sin_factura else None,
                comex_proximas    = st.session_state.get("_email_comex_proximas")    if inc_proximas else None,
                plan_compras      = st.session_state.get("_email_plan_compras")  if inc_plan else None,
                plan_kpis         = st.session_state.get("_email_plan_kpis")     if inc_plan else None,
                higiene_kpis      = st.session_state.get("_email_higiene_resumen") if inc_higiene else None,
                higiene_sin_forecast = st.session_state.get("_email_sin_forecast") if inc_higiene else None,
                higiene_sin_perfil   = st.session_state.get("_email_sin_perfil")   if inc_higiene else None,
            )

            attachments = []
            if inc_excel:
                try:
                    excel_bytes = _build_excel_attachment(
                        st.session_state.get("_email_stock_critico"),
                        st.session_state.get("_email_comex_atrasadas"),
                        st.session_state.get("_email_comex_sin_carpeta"),
                        st.session_state.get("_email_plan_compras"),
                        st.session_state.get("_email_sin_perfil"),
                        st.session_state.get("_email_sin_forecast"),
                        st.session_state.get("_email_comex_sin_factura"),
                        st.session_state.get("_email_comex_proximas"),
                    )
                    fname = f"dorel_alertas_{date.today().strftime('%Y%m%d')}.xlsx"
                    attachments.append((fname, excel_bytes))
                except Exception as exc:
                    st.warning(f"No se pudo generar el Excel adjunto: {exc}")

            port_val = st.session_state.get("smtp_port_override", "587")
            cfg = get_smtp_config({
                "email":    st.session_state.get("smtp_email_override", ""),
                "password": st.session_state.get("smtp_password_override", ""),
                "server":   st.session_state.get("smtp_server_override", ""),
                "port":     int(port_val) if str(port_val).isdigit() else 587,
            })

        with st.spinner(f"Enviando a {len(recipients)} destinatario(s)..."):
            ok, msg = send_alert_email(subject, html_body, recipients, cfg, attachments or None)

        if ok:
            st.success(f"✅ {msg}")
            st.balloons()
        else:
            st.error(f"❌ Error al enviar: {msg}")
            with st.expander("Ayuda"):
                st.markdown(
                    "1. Verifica email y contraseña\n"
                    "2. Gmail: genera App Password en `myaccount.google.com/apppasswords`\n"
                    "3. Servidor: `smtp.gmail.com`, puerto `587`"
                )

    # ══════════════════════════════════════════════════════════════════════
    # REPORTE STOCK CRITICO TIENDAS (standalone, one-click)
    # ══════════════════════════════════════════════════════════════════════
    st.markdown("---")
    st.markdown("### 📦 Reporte Stock Critico Tiendas")
    st.caption(
        "Genera y envia reporte automatico de stock critico en tiendas. "
        "Criterio: MOI ≥ 12 meses o sin MOI + Antiguedad ≥ 12 meses. "
        "Destinatario: samuel.dorival@dorel.cl"
    )

    # Preview button
    if st.button("🔍 Cargar y previsualizar datos", key="btn_preview_critico_tiendas"):
        try:
            with st.spinner("Consultando stock critico por tienda..."):
                df_ct = _load_stock_critico_tiendas(conn)
                st.session_state["_critico_tiendas_df"] = df_ct
            if df_ct.empty:
                st.warning("No se encontraron SKUs que cumplan el criterio.")
            else:
                st.toast(f"Cargados {len(df_ct):,} registros de {df_ct['SKU_PRODUCTO'].nunique():,} SKUs criticos.")
        except Exception as e:
            st.error(f"Error al cargar datos: {e}")
            import traceback
            st.code(traceback.format_exc())

    # Show preview if data is loaded
    df_ct_preview = st.session_state.get("_critico_tiendas_df")
    if df_ct_preview is not None and not df_ct_preview.empty:
        _n_skus = df_ct_preview["SKU_PRODUCTO"].nunique() if "SKU_PRODUCTO" in df_ct_preview.columns else 0
        _n_tiendas = df_ct_preview["DESCRIPCION_SUCURSAL"].nunique() if "DESCRIPCION_SUCURSAL" in df_ct_preview.columns else 0
        _total_und = df_ct_preview["STOCK_UNIDADES"].sum() if "STOCK_UNIDADES" in df_ct_preview.columns else 0
        _total_costo = df_ct_preview["STOCK_COSTO"].sum() if "STOCK_COSTO" in df_ct_preview.columns else 0

        # KPI row
        ck1, ck2, ck3, ck4 = st.columns(4)
        with ck1:
            st.metric("SKUs Criticos", f"{_n_skus:,}")
        with ck2:
            st.metric("Tiendas", f"{_n_tiendas:,}")
        with ck3:
            st.metric("Unidades", f"{_total_und:,.0f}")
        with ck4:
            st.metric("Costo Total", f"${_total_costo:,.0f}")

        # Resumen por tienda
        if "DESCRIPCION_SUCURSAL" in df_ct_preview.columns:
            _resumen = df_ct_preview.groupby("DESCRIPCION_SUCURSAL", as_index=False).agg(
                STOCK_UNIDADES=("STOCK_UNIDADES", "sum"),
                STOCK_COSTO=("STOCK_COSTO", "sum"),
            ).sort_values("STOCK_COSTO", ascending=False)
            st.dataframe(
                _resumen,
                use_container_width=True,
                hide_index=True,
                height=min(400, len(_resumen) * 38 + 40),
                column_config={
                    "DESCRIPCION_SUCURSAL": st.column_config.TextColumn("Tienda", width="medium"),
                    "STOCK_UNIDADES": st.column_config.NumberColumn("Unidades", format="%d"),
                    "STOCK_COSTO": st.column_config.NumberColumn("Costo CLP", format="$%,.0f"),
                },
            )
            st.caption(f"Total: {len(_resumen)} tiendas | {_total_und:,.0f} unidades | ${_total_costo:,.0f} CLP")

    # Send button
    st.markdown("")
    if st.button(
        "📨 Enviar Reporte Stock Critico Tiendas",
        type="primary",
        key="btn_send_critico_tiendas",
        use_container_width=True,
    ):
        # Load data if not already loaded
        df_ct = st.session_state.get("_critico_tiendas_df")
        if df_ct is None or df_ct.empty:
            with st.spinner("Cargando datos..."):
                try:
                    df_ct = _load_stock_critico_tiendas(conn)
                    st.session_state["_critico_tiendas_df"] = df_ct
                except Exception as e:
                    st.error(f"Error al cargar: {e}")
                    st.stop()

        if df_ct.empty:
            st.warning("No hay datos que enviar.")
            st.stop()

        with st.spinner("Generando Excel y enviando email..."):
            # Build Excel
            excel_bytes = _build_stock_critico_tiendas_excel(df_ct)
            fname = f"Stock Critico tiendas {date.today().strftime('%Y%m%d')}.xlsx"

            # Build HTML body
            _n_skus = df_ct["SKU_PRODUCTO"].nunique() if "SKU_PRODUCTO" in df_ct.columns else 0
            _n_tiendas = df_ct["DESCRIPCION_SUCURSAL"].nunique() if "DESCRIPCION_SUCURSAL" in df_ct.columns else 0
            _total_und = df_ct["STOCK_UNIDADES"].sum() if "STOCK_UNIDADES" in df_ct.columns else 0
            _total_costo = df_ct["STOCK_COSTO"].sum() if "STOCK_COSTO" in df_ct.columns else 0

            # Top 15 tiendas for email body
            resumen_top = pd.DataFrame()
            if "DESCRIPCION_SUCURSAL" in df_ct.columns:
                resumen_top = df_ct.groupby("DESCRIPCION_SUCURSAL", as_index=False).agg(
                    STOCK_UNIDADES=("STOCK_UNIDADES", "sum"),
                    STOCK_COSTO=("STOCK_COSTO", "sum"),
                ).sort_values("STOCK_COSTO", ascending=False).head(15)

            html_body = _build_stock_critico_tiendas_email_html(
                _n_skus, _n_tiendas, _total_und, _total_costo, resumen_top,
            )

            # SMTP config (reuse from main section)
            port_val = st.session_state.get("smtp_port_override", "587")
            cfg = get_smtp_config({
                "email":    st.session_state.get("smtp_email_override", ""),
                "password": st.session_state.get("smtp_password_override", ""),
                "server":   st.session_state.get("smtp_server_override", ""),
                "port":     int(port_val) if str(port_val).isdigit() else 587,
            })

            ok, msg = send_alert_email(
                subject="REPORTE STOCK CRITICO PORTAL DOREL CHILE",
                html_body=html_body,
                to_recipients=["samuel.dorival@dorel.cl"],
                cfg=cfg,
                attachments=[(fname, excel_bytes)],
            )

        if ok:
            st.success(f"✅ {msg}")
            st.balloons()
        else:
            st.error(f"❌ {msg}")


# ============================================================================
# STOCK CRITICO TIENDAS — Automated Report
# ============================================================================

_CRITICO_TIENDAS_COLS = [
    "SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "MARCA",
    "COD_BODEGA", "ID_SUCURSAL", "DESCRIPCION_SUCURSAL",
    "CANAL_DE_DISTRIBUCION", "SUPERVISOR", "CLUSTER",
    "STOCK_UNIDADES", "STOCK_COSTO",
]


def _load_stock_critico_tiendas(conn) -> pd.DataFrame:
    """Load stock critico detail at tienda level filtered by MOI>=12 or sin MOI AND antiguedad>=12.

    Steps:
    1. Load QUERY_STOCK_CRITICO_DETAIL (row per bodega/SKU, latest date)
    2. Load stock_critico_metrics (MOI/antiguedad per SKU, company level)
    3. Merge to get MOI and ANTIGUEDAD per SKU
    4. Filter: TIENDA only, (MOI >= 12 OR sin MOI) AND antiguedad >= 12
    5. Return the 13-column DataFrame matching Samuel's format
    """
    # 1. Detail: stock per tienda/SKU
    detail = pd.read_sql(QUERY_STOCK_CRITICO_DETAIL, conn)
    detail = norm_cols(detail)
    if detail.empty:
        return pd.DataFrame(columns=_CRITICO_TIENDAS_COLS)

    for c in ["STOCK_COSTO", "STOCK_UNIDADES"]:
        if c in detail.columns:
            detail[c] = pd.to_numeric(detail[c], errors="coerce").fillna(0)

    # 2. Metrics: MOI and antiguedad per SKU (company level)
    metrics = cq.stock_critico_metrics(conn)
    if not metrics.empty:
        for c in ["STOCK_COSTO", "MOI", "ANTIGUEDAD_MESES", "COSTO_PROM_90_CIA"]:
            if c in metrics.columns:
                metrics[c] = pd.to_numeric(metrics[c], errors="coerce").fillna(0)

        # Keep latest date only
        if "FECHA" in metrics.columns:
            metrics["FECHA"] = pd.to_datetime(metrics["FECHA"], errors="coerce")
            metrics = metrics[metrics["FECHA"] == metrics["FECHA"].max()]

        # Aggregate to SKU level (max MOI, max antiguedad)
        sku_metrics = metrics.groupby("SKU_PRODUCTO", as_index=False).agg(
            COSTO_PROM_90_CIA=("COSTO_PROM_90_CIA", "max"),
            STOCK_COSTO_CIA=("STOCK_COSTO", "sum"),
        )
        # Recalculate MOI at company level
        sku_metrics["MOI"] = np.where(
            sku_metrics["COSTO_PROM_90_CIA"] > 0,
            (sku_metrics["STOCK_COSTO_CIA"] / sku_metrics["COSTO_PROM_90_CIA"]) / 30.44,
            np.nan,
        )
        # Mark items with stock but no sales (infinite MOI)
        sku_metrics.loc[
            (sku_metrics["MOI"].isna()) & (sku_metrics["STOCK_COSTO_CIA"] > 0), "MOI"
        ] = np.nan  # keep as NaN = "sin MOI"

        # Antiguedad from metrics (already calculated in query)
        if "ANTIGUEDAD_MESES" in metrics.columns:
            ant_sku = metrics.groupby("SKU_PRODUCTO", as_index=False)["ANTIGUEDAD_MESES"].max()
            sku_metrics = sku_metrics.merge(ant_sku, on="SKU_PRODUCTO", how="left")
        else:
            sku_metrics["ANTIGUEDAD_MESES"] = 0

        sku_metrics["ANTIGUEDAD_MESES"] = pd.to_numeric(
            sku_metrics["ANTIGUEDAD_MESES"], errors="coerce"
        ).fillna(0)

        # 3. Merge
        detail = detail.merge(
            sku_metrics[["SKU_PRODUCTO", "MOI", "ANTIGUEDAD_MESES"]],
            on="SKU_PRODUCTO",
            how="left",
        )
    else:
        detail["MOI"] = np.nan
        detail["ANTIGUEDAD_MESES"] = 0

    # 4. Filter: TIENDA only + (MOI >= 12 OR sin MOI) AND antiguedad >= 12
    if "CANAL_DE_DISTRIBUCION" in detail.columns:
        detail = detail[detail["CANAL_DE_DISTRIBUCION"].str.upper() == "TIENDA"]

    mask_moi = (detail["MOI"] >= 12) | (detail["MOI"].isna())
    mask_ant = detail["ANTIGUEDAD_MESES"] >= 12
    detail = detail[mask_moi & mask_ant].copy()

    # 5. Select and order columns
    cols = [c for c in _CRITICO_TIENDAS_COLS if c in detail.columns]
    return detail[cols].reset_index(drop=True)


def _build_stock_critico_tiendas_excel(df_detail: pd.DataFrame) -> bytes:
    """Build Excel with 2 sheets matching Samuel's exact format.

    Sheet 1 'Resumen Tienda': pivot by DESCRIPCION_SUCURSAL with totals
    Sheet 2 'Detalle SKU': full detail, 13 columns
    """
    buf = BytesIO()

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # ── Sheet 1: Resumen Tienda ──
        if not df_detail.empty and "DESCRIPCION_SUCURSAL" in df_detail.columns:
            resumen = df_detail.groupby("DESCRIPCION_SUCURSAL", as_index=False).agg(
                STOCK_UNIDADES=("STOCK_UNIDADES", "sum"),
                STOCK_COSTO=("STOCK_COSTO", "sum"),
            ).sort_values("STOCK_COSTO", ascending=False)

            # Add total row
            total_row = pd.DataFrame([{
                "DESCRIPCION_SUCURSAL": "Total general",
                "STOCK_UNIDADES": resumen["STOCK_UNIDADES"].sum(),
                "STOCK_COSTO": resumen["STOCK_COSTO"].sum(),
            }])
            resumen = pd.concat([resumen, total_row], ignore_index=True)

            resumen.to_excel(writer, sheet_name="Resumen Tienda", index=False)
        else:
            pd.DataFrame({"Info": ["Sin datos"]}).to_excel(
                writer, sheet_name="Resumen Tienda", index=False,
            )

        # ── Sheet 2: Detalle SKU ──
        df_detail.to_excel(writer, sheet_name="Detalle SKU", index=False)

    return buf.getvalue()


def _build_stock_critico_tiendas_email_html(
    n_skus: int, n_tiendas: int, total_und: float, total_costo: float,
    resumen_top: pd.DataFrame,
) -> str:
    """Build a simple HTML email body with KPIs and top tiendas summary."""
    today_str = date.today().strftime("%d/%m/%Y")

    # Format top tiendas as HTML table rows
    rows_html = ""
    for _, row in resumen_top.iterrows():
        tienda = row.get("DESCRIPCION_SUCURSAL", "—")
        und = int(row.get("STOCK_UNIDADES", 0))
        costo = row.get("STOCK_COSTO", 0)
        rows_html += f"""<tr>
            <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0;">{tienda}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0;text-align:right;">{und:,}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0;text-align:right;">${costo:,.0f}</td>
        </tr>"""

    return f"""
    <html>
    <body style="font-family:Arial,sans-serif;color:#1e293b;margin:0;padding:20px;background:#f8fafc;">
        <div style="max-width:700px;margin:0 auto;background:white;border-radius:12px;
                    box-shadow:0 2px 8px rgba(0,0,0,0.08);overflow:hidden;">

            <!-- Header -->
            <div style="background:#065E8B;padding:24px 32px;color:white;">
                <h1 style="margin:0;font-size:20px;">REPORTE STOCK CRITICO PORTAL DOREL CHILE</h1>
                <p style="margin:8px 0 0;opacity:0.85;font-size:14px;">
                    Fecha: {today_str} | Criterio: MOI &ge; 12 o sin MOI + Antiguedad &ge; 12 meses | Solo tiendas
                </p>
            </div>

            <!-- KPIs -->
            <div style="display:flex;padding:20px 32px;gap:16px;">
                <div style="flex:1;background:#f0f9ff;border-radius:8px;padding:16px;text-align:center;">
                    <div style="font-size:24px;font-weight:700;color:#065E8B;">{n_skus:,}</div>
                    <div style="font-size:12px;color:#64748b;">SKUs Criticos</div>
                </div>
                <div style="flex:1;background:#fef3c7;border-radius:8px;padding:16px;text-align:center;">
                    <div style="font-size:24px;font-weight:700;color:#92400e;">{n_tiendas:,}</div>
                    <div style="font-size:12px;color:#64748b;">Tiendas Afectadas</div>
                </div>
                <div style="flex:1;background:#fce4ec;border-radius:8px;padding:16px;text-align:center;">
                    <div style="font-size:24px;font-weight:700;color:#c62828;">{total_und:,.0f}</div>
                    <div style="font-size:12px;color:#64748b;">Unidades</div>
                </div>
                <div style="flex:1;background:#e8f5e9;border-radius:8px;padding:16px;text-align:center;">
                    <div style="font-size:24px;font-weight:700;color:#2e7d32;">${total_costo:,.0f}</div>
                    <div style="font-size:12px;color:#64748b;">Costo Total</div>
                </div>
            </div>

            <!-- Top Tiendas Table -->
            <div style="padding:0 32px 24px;">
                <h3 style="margin:0 0 12px;font-size:16px;color:#1e293b;">Top 15 Tiendas por Costo Stock Critico</h3>
                <table style="width:100%;border-collapse:collapse;font-size:13px;">
                    <thead>
                        <tr style="background:#f1f5f9;">
                            <th style="padding:8px 12px;text-align:left;border-bottom:2px solid #cbd5e1;">Tienda</th>
                            <th style="padding:8px 12px;text-align:right;border-bottom:2px solid #cbd5e1;">Unidades</th>
                            <th style="padding:8px 12px;text-align:right;border-bottom:2px solid #cbd5e1;">Costo CLP</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>
            </div>

            <!-- Footer -->
            <div style="background:#f1f5f9;padding:16px 32px;text-align:center;font-size:12px;color:#64748b;">
                Generado automaticamente por Portal Dorel Chile &mdash; Ver detalle completo en el Excel adjunto.
            </div>
        </div>
    </body>
    </html>
    """


# ============================================================================
# HELPERS
# ============================================================================

def _load_all_and_store(conn, inc_critico: bool, moi_thresh: float, ant_thresh: float):
    """Load all alert data and store in session_state."""
    st.session_state["_email_stock_kpis"]  = _load_stock_kpis(conn)
    st.session_state["_email_ventas_kpis"] = _load_ventas_kpis(conn)

    if inc_critico:
        critico_kpis, critico_matrix = _load_stock_critico(conn, moi_thresh, ant_thresh)
        st.session_state["_email_critico_kpis"]   = critico_kpis
        st.session_state["_email_critico_matrix"]  = critico_matrix
    else:
        st.session_state["_email_critico_kpis"]   = {}
        st.session_state["_email_critico_matrix"]  = pd.DataFrame()

    st.session_state["_email_comex_atrasadas"]   = _load_comex_atrasadas(conn)
    st.session_state["_email_comex_sin_carpeta"] = _load_comex_sin_carpeta(conn)
    st.session_state["_email_comex_sin_factura"] = _load_comex_sin_factura(conn)
    st.session_state["_email_comex_proximas"]    = _load_comex_proximas(conn)

    plan_df, plan_kpis = _load_plan_compras(conn)
    st.session_state["_email_plan_compras"] = plan_df
    st.session_state["_email_plan_kpis"]    = plan_kpis

    proy_df = st.session_state.get("df_proy")
    if proy_df is not None:
        proy_df = apply_pm_filter(proy_df)
    higiene, sin_fc, sin_perf = _build_higiene_resumen(conn, proy_df)
    st.session_state["_email_higiene_resumen"] = higiene
    st.session_state["_email_sin_forecast"]    = sin_fc
    st.session_state["_email_sin_perfil"]      = sin_perf

    st.session_state["_email_data_loaded"] = True


def _render_data_preview():
    """Show a summary of loaded data so user can verify before sending."""
    st.markdown("#### Resumen de datos cargados")

    stock_kpis   = st.session_state.get("_email_stock_kpis", {})
    critico_kpis = st.session_state.get("_email_critico_kpis", {})
    atrasadas    = st.session_state.get("_email_comex_atrasadas", pd.DataFrame())
    sin_carpeta  = st.session_state.get("_email_comex_sin_carpeta", pd.DataFrame())
    plan_kpis    = st.session_state.get("_email_plan_kpis", {})
    higiene      = st.session_state.get("_email_higiene_resumen", {})

    sin_factura = st.session_state.get("_email_comex_sin_factura", pd.DataFrame())
    proximas   = st.session_state.get("_email_comex_proximas", pd.DataFrame())

    c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)
    for col, label, val in [
        (c1, "💰 Stock Costo",     human_format(stock_kpis.get("stock_costo", 0))),
        (c2, "🚨 SKUs Analiz.",    f"{critico_kpis.get('total_skus', 0):,}"),
        (c3, "⚠️ POs Atrasadas",   f"{len(atrasadas):,}"),
        (c4, "📁 Sin Carpeta",     f"{len(sin_carpeta):,}"),
        (c5, "📄 Sin Factura",     f"{len(sin_factura):,}"),
        (c6, "🚢 Proximas",        f"{len(proximas):,}"),
        (c7, "🛒 POs Transito",    f"{plan_kpis.get('n_pos', 0):,}"),
        (c8, "🩺 Sin Forecast",    str(higiene.get("sin_forecast", "—"))),
    ]:
        with col:
            st.metric(label, val)

    tabs = st.tabs(["POs Atrasadas", "Sin Carpeta", "Sin Factura", "Proximas", "Plan Compras", "Higiene"])
    for tab, key, empty_msg in zip(tabs, [
        "_email_comex_atrasadas", "_email_comex_sin_carpeta",
        "_email_comex_sin_factura", "_email_comex_proximas",
        "_email_plan_compras", "_email_sin_perfil",
    ], [
        "No hay POs atrasadas.",
        "Todas las POs tienen carpeta comex.",
        "Todas las POs tienen diario de factura.",
        "No hay POs proximas a llegar.",
        "No hay POs en transito.",
        "No hay SKUs sin perfil de tienda.",
    ]):
        with tab:
            df = st.session_state.get(key, pd.DataFrame())
            if isinstance(df, pd.DataFrame) and df.empty:
                st.success(empty_msg)
            elif isinstance(df, pd.DataFrame):
                st.dataframe(df.head(50), use_container_width=True, height=280)
                st.caption(f"Total: {len(df):,} filas. Email muestra los primeros 20-25.")
