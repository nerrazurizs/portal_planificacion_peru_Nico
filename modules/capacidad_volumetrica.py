"""Capacidad Volumetrica de Tiendas — analisis de ocupacion de espacio fisico.

Cruza el espacio fisico de cada tienda (m2) con el volumen ocupado por
inventario (stock_unidades × densidad CBM/unidad) para detectar:
  - Tiendas en riesgo de colapso (sobreocupadas)
  - SKUs que ocupan mucho espacio pero no se venden ("space hogs")
  - Perfiles de exhibicion excesivos respecto a ventas reales
  - Segmentacion por Supervisor, Tipo, Cluster, Zona

Tabs:
  1. Resumen         KPIs + histograma ocupacion + top 10 + donut riesgo
  2. Por Tienda      Ranking de tiendas por % ocupacion con drill-down
  3. Eficiencia      Scatter CBM vs VN/CBM + tabla de "space hogs"
  4. Perfil          Cuestionamiento de perfiles excesivos
  5. Mapa            Geolocalizacion con color = ocupacion
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, dorel_layout, apply_pm_filter
from db.cache import cached_query as cq
from utils.export import download_buttons
from utils.filters import human_format, norm_cols, fmt_clp
from utils.ui_animations import lottie_spinner

# ============================================================================
# CONSTANTS
# ============================================================================

_DEFAULT_FACTOR_CBM_M2 = 2.0   # m3 capacity per m2 (avg shelf height)
_DEFAULT_UMBRAL_RIESGO = 85    # % occupancy threshold for risk flag

_RISK_LABELS = {
    "CRITICO": "Critico (>100%)",
    "ALTO": "Alto (85-100%)",
    "MEDIO": "Medio (60-85%)",
    "BAJO": "Bajo (<60%)",
}

_RISK_COLORS = {
    "CRITICO": COLORS["status_critical"],
    "ALTO": COLORS["status_at_risk"],
    "MEDIO": COLORS["status_en_curso"],
    "BAJO": COLORS["status_on_track"],
}

_RISK_ORDER = ["CRITICO", "ALTO", "MEDIO", "BAJO"]

_MAESTRA_COLS = [
    "SKU_PRODUCTO", "DENSIDAD", "ALTO", "ANCHO", "PROFUNDIDAD",
    "PESO_BRUTO_UNIDAD",
    "AREA", "LINEA", "SUBLINEA",
    "MARCA", "SKU_NOM_PRODUCTO", "MIX_OFICIAL",
]

# Columns that might contain density (try each in order)
_DENSITY_CANDIDATES = ["DENSIDAD", "CBM", "VOLUMEN", "M3", "VOLUMEN_UNITARIO"]


# ============================================================================
# HELPERS
# ============================================================================

def _to_numeric_robust(series: pd.Series) -> pd.Series:
    """Convert a series to numeric, handling text with comma decimals.

    Handles: "0,001104" → 0.001104, "1.234,56" → 1234.56, None → 0.
    """
    s = series.copy()
    # If already numeric, just fillna
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce").fillna(0)

    # Convert to string, replace comma decimal
    s = s.astype(str).str.strip()
    # If values have format "1.234,56" (thousand dot, comma decimal)
    # detect by checking if comma appears after a dot
    sample = s[s.str.contains(r"\d", na=False)].head(100)
    if not sample.empty:
        has_dot_then_comma = sample.str.contains(r"\d\.\d{3},\d", na=False).any()
        if has_dot_then_comma:
            # Format: 1.234,56 → remove dots, replace comma with dot
            s = s.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
        else:
            # Format: 0,001104 → just replace comma with dot
            s = s.str.replace(",", ".", regex=False)

    return pd.to_numeric(s, errors="coerce").fillna(0)


def _safe_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Coerce columns to numeric safely, handling text with comma decimals."""
    for c in cols:
        if c in df.columns:
            df[c] = _to_numeric_robust(df[c])
    return df


def _fmt_cbm(val: float) -> str:
    """Format CBM value with 2 decimals."""
    if pd.isna(val) or val == 0:
        return "0.00"
    return f"{val:,.2f}"


def _fmt_pct(val: float) -> str:
    """Format percentage with 1 decimal."""
    if pd.isna(val):
        return "0.0%"
    return f"{val:,.1f}%"


def _fmt_mm(val: float) -> str:
    """Format large CLP values as $X.XXX M."""
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "$0"
    if pd.isna(val) or val == 0:
        return "$0"
    neg = val < 0
    abs_val = abs(val)
    if abs_val >= 1_000_000_000:
        txt = f"${abs_val / 1_000_000_000:,.1f}B"
    elif abs_val >= 1_000_000:
        txt = f"${abs_val / 1_000_000:,.1f}M"
    else:
        txt = f"${abs_val:,.0f}"
    return f"-{txt}" if neg else txt


def _classify_risk(pct: float, umbral: float) -> str:
    """Classify occupancy % into risk level."""
    if pct > 100:
        return "CRITICO"
    if pct > umbral:
        return "ALTO"
    if pct > 60:
        return "MEDIO"
    return "BAJO"


def _kpi_card(label: str, value: str, color: str = COLORS["primary"]) -> None:
    """Render a single KPI card (inline HTML)."""
    st.html(
        f'<div style="background:#fff;padding:1.2rem;border-radius:10px;'
        f"text-align:center;border-left:4px solid {color};"
        f'box-shadow:0 1px 3px rgba(0,0,0,0.06);">'
        f'<div style="font-size:1.6rem;font-weight:700;color:{color};">'
        f"{value}</div>"
        f'<div style="font-size:0.8rem;color:#666;margin-top:0.2rem;">'
        f"{label}</div></div>"
    )


# ============================================================================
# DATA LOADING & PREPROCESSING
# ============================================================================

def _load_data(conn):
    """Load all data sources for volumetric analysis."""
    with lottie_spinner("snowflake"):
        stock_hig = cq.stock_higiene(conn)
        tienda_dim = cq.tienda_dim(conn)
        maestra = cq.maestra(conn)
        ventas_90d = cq.ventas_90d_sucursal(conn)

    # Transit may fail if table doesn't exist
    try:
        transito = cq.transito_sucursales(conn)
    except Exception:
        transito = pd.DataFrame()

    return stock_hig, tienda_dim, maestra, ventas_90d, transito


def _parse_transito(df_raw: pd.DataFrame) -> pd.DataFrame:
    """Standardize transit table columns (defensive, copied pattern from redistribucion)."""
    if df_raw is None or df_raw.empty:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO", "QTY_TRANSITO"])

    df = df_raw.copy()
    df.columns = [c.upper().strip() for c in df.columns]

    # Detect SKU column
    sku_col = None
    for candidate in ["SKU_PRODUCTO", "ID_MATERIAL", "SKU", "COD_MATERIAL",
                       "MATERIAL", "PRODUCTO"]:
        if candidate in df.columns:
            sku_col = candidate
            break
    if sku_col is None:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO", "QTY_TRANSITO"])

    # Detect destination store column
    dest_col = None
    for candidate in ["ID_SUCURSAL_DESTINO", "SUCURSAL_DESTINO", "BOD_DESTINO",
                       "COD_BODEGA_DESTINO", "DESTINO", "ID_SUCURSAL"]:
        if candidate in df.columns:
            dest_col = candidate
            break
    if dest_col is None:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO", "QTY_TRANSITO"])

    # Detect quantity column
    qty_col = None
    for candidate in ["CANTIDAD", "QTY", "UNIDADES", "CANTIDAD_TRANSITO",
                       "STOCK_TRANSITO", "QTY_TRANSITO"]:
        if candidate in df.columns:
            qty_col = candidate
            break

    result = pd.DataFrame({
        "SKU_PRODUCTO": df[sku_col].astype(str).str.strip(),
        "ID_SUCURSAL_DESTINO": df[dest_col].astype(str).str.strip(),
    })
    if qty_col:
        result["QTY_TRANSITO"] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0)
    else:
        result["QTY_TRANSITO"] = 1

    result = result.groupby(
        ["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO"], as_index=False,
    ).agg(QTY_TRANSITO=("QTY_TRANSITO", "sum"))
    return result


def _apply_density_fallback(df: pd.DataFrame) -> pd.DataFrame:
    """Impute missing DENSIDAD using hierarchical averages.

    Fallback hierarchy (same pattern as factor_importacion):
        1. SKU-specific DENSIDAD (if > 0)
        2. Average by SUBLINEA + MARCA
        3. Average by LINEA + MARCA
        4. Average by AREA + MARCA
        5. Average by SUBLINEA
        6. Average by LINEA
        7. Average by AREA
        8. Global average of non-zero densities

    Adds column ORIGEN_DENSIDAD: "MAESTRA", "SUBLINEA_MARCA", "LINEA_MARCA",
    "AREA_MARCA", "SUBLINEA", "LINEA", "AREA", "GLOBAL", "SIN_DENSIDAD".
    """
    if df.empty or "DENSIDAD" not in df.columns:
        return df

    df = df.copy()
    df["DENSIDAD"] = pd.to_numeric(df["DENSIDAD"], errors="coerce").fillna(0)
    df["ORIGEN_DENSIDAD"] = np.where(df["DENSIDAD"] > 0, "MAESTRA", "")

    missing = df["DENSIDAD"] <= 0
    if not missing.any():
        return df

    has_density = df[df["DENSIDAD"] > 0]
    if has_density.empty:
        # No density data at all — leave as 0
        df.loc[missing, "ORIGEN_DENSIDAD"] = "SIN_DENSIDAD"
        return df

    # Pre-compute averages at each level
    _avgs = {}
    for level_name, group_cols in [
        ("SUBLINEA_MARCA", ["SUBLINEA", "MARCA"]),
        ("LINEA_MARCA", ["LINEA", "MARCA"]),
        ("AREA_MARCA", ["AREA", "MARCA"]),
        ("SUBLINEA", ["SUBLINEA"]),
        ("LINEA", ["LINEA"]),
        ("AREA", ["AREA"]),
    ]:
        available = [c for c in group_cols if c in has_density.columns]
        if len(available) == len(group_cols):
            avg = (
                has_density.groupby(available, as_index=False)["DENSIDAD"]
                .mean()
                .rename(columns={"DENSIDAD": f"_DENS_{level_name}"})
            )
            _avgs[level_name] = (available, avg)

    global_avg = has_density["DENSIDAD"].mean()

    # Apply fallback in priority order
    still_missing = df["DENSIDAD"] <= 0
    for level_name in ["SUBLINEA_MARCA", "LINEA_MARCA", "AREA_MARCA",
                        "SUBLINEA", "LINEA", "AREA"]:
        if level_name not in _avgs or not still_missing.any():
            continue
        group_cols, avg_df = _avgs[level_name]
        col_name = f"_DENS_{level_name}"
        df = df.merge(avg_df, on=group_cols, how="left")
        fill_mask = still_missing & (df[col_name] > 0)
        df.loc[fill_mask, "DENSIDAD"] = df.loc[fill_mask, col_name]
        df.loc[fill_mask, "ORIGEN_DENSIDAD"] = level_name
        still_missing = df["DENSIDAD"] <= 0
        df.drop(columns=[col_name], inplace=True)

    # Global fallback
    if still_missing.any() and global_avg > 0:
        df.loc[still_missing, "DENSIDAD"] = global_avg
        df.loc[still_missing, "ORIGEN_DENSIDAD"] = "GLOBAL"
        still_missing = df["DENSIDAD"] <= 0

    if still_missing.any():
        df.loc[still_missing, "ORIGEN_DENSIDAD"] = "SIN_DENSIDAD"

    return df


def _find_density_column(maestra: pd.DataFrame) -> str | None:
    """Find the actual density column name in the maestra DataFrame.

    Uses robust text-to-numeric conversion (handles comma decimals).
    """
    for candidate in _DENSITY_CANDIDATES:
        if candidate in maestra.columns:
            vals = _to_numeric_robust(maestra[candidate])
            if (vals > 0).any():
                return candidate
    return None


def _build_detail(
    stock_hig: pd.DataFrame,
    tienda_dim: pd.DataFrame,
    maestra: pd.DataFrame,
    ventas_90d: pd.DataFrame,
    transito: pd.DataFrame,
) -> pd.DataFrame:
    """Build SKU × Tienda detail with all volumetric metrics.

    Returns DataFrame with one row per SKU × Tienda (only active TIENDA stores
    with MTS2 > 0 and SKUs with DENSIDAD > 0).
    """
    if stock_hig.empty or tienda_dim.empty or maestra.empty:
        return pd.DataFrame()

    # ── 1. Stock: only stores (not CD) ──────────────────────────────────
    sh = stock_hig.copy()
    _safe_numeric(sh, ["STOCK_UNIDADES", "PERFIL_TIENDAS"])
    if "CANAL_DE_DISTRIBUCION" in sh.columns:
        sh = sh[sh["CANAL_DE_DISTRIBUCION"] == "TIENDA"]
    if sh.empty:
        return pd.DataFrame()

    # Keep only needed columns
    sh_cols = ["SKU_PRODUCTO", "ID_SUCURSAL", "STOCK_UNIDADES", "PERFIL_TIENDAS"]
    sh_cols = [c for c in sh_cols if c in sh.columns]
    sh = sh[sh_cols].copy()

    # ── 2. Active stores with M2 > 0 ───────────────────────────────────
    td = tienda_dim.copy()
    _safe_numeric(td, ["MTS2"])
    if "ACTIVA" in td.columns:
        td = td[td["ACTIVA"] == True]  # noqa: E712
    td = td[td["MTS2"] > 0]
    if td.empty:
        return pd.DataFrame()

    active_stores = set(td["ID_SUCURSAL"].astype(str).str.strip())
    sh["ID_SUCURSAL"] = sh["ID_SUCURSAL"].astype(str).str.strip()
    sh = sh[sh["ID_SUCURSAL"].isin(active_stores)]
    if sh.empty:
        return pd.DataFrame()

    # ── 3. Merge maestra (DENSIDAD + dimensions) ───────────────────────
    available_m_cols = [c for c in _MAESTRA_COLS if c in maestra.columns]
    m = maestra[available_m_cols].drop_duplicates(subset=["SKU_PRODUCTO"]).copy()

    # Robust text-to-numeric for density and dimension columns
    for col in ["DENSIDAD", "ALTO", "ANCHO", "PROFUNDIDAD", "PESO_BRUTO_UNIDAD"]:
        if col in m.columns:
            m[col] = _to_numeric_robust(m[col])

    # Auto-detect density column (could be DENSIDAD, CBM, VOLUMEN, etc.)
    dens_col = _find_density_column(m)
    if dens_col and dens_col != "DENSIDAD":
        m["DENSIDAD"] = _to_numeric_robust(m[dens_col])
    elif "DENSIDAD" not in m.columns:
        m["DENSIDAD"] = 0.0

    # Fallback: calculate CBM from ALTO × ANCHO × PROFUNDIDAD (cm → m³)
    # If dimensions are in cm: CBM = (A × B × C) / 1_000_000
    # If dimensions are in m:  CBM = A × B × C
    dims_available = all(c in m.columns for c in ["ALTO", "ANCHO", "PROFUNDIDAD"])
    if dims_available:
        _a = m["ALTO"]
        _b = m["ANCHO"]
        _c = m["PROFUNDIDAD"]
        has_dims = (_a > 0) & (_b > 0) & (_c > 0)

        if has_dims.any():
            raw_vol = _a * _b * _c
            # Heuristic: if median volume > 1, dimensions are likely in cm
            median_vol = raw_vol[has_dims].median()
            if median_vol > 1:
                # cm → m³ (divide by 1,000,000)
                cbm_calc = raw_vol / 1_000_000
                dim_unit = "cm"
            else:
                # Already in meters
                cbm_calc = raw_vol
                dim_unit = "m"

            # Fill where DENSIDAD is 0 but dimensions exist
            fill_mask = (m["DENSIDAD"] <= 0) & has_dims
            m.loc[fill_mask, "DENSIDAD"] = cbm_calc[fill_mask]

    df = sh.merge(m, on="SKU_PRODUCTO", how="left")
    df["DENSIDAD"] = df["DENSIDAD"].fillna(0)

    # ── 3b. Apply density fallback (impute from category averages) ────
    df = _apply_density_fallback(df)

    # ── 4. Calculate CBM ────────────────────────────────────────────────
    df["CBM_STOCK"] = df["STOCK_UNIDADES"] * df["DENSIDAD"]
    df["CBM_PERFIL"] = df.get("PERFIL_TIENDAS", 0) * df["DENSIDAD"]

    # ── 5. Merge transit data ───────────────────────────────────────────
    tr = _parse_transito(transito)
    if not tr.empty:
        tr = tr.rename(columns={"ID_SUCURSAL_DESTINO": "ID_SUCURSAL"})
        tr["ID_SUCURSAL"] = tr["ID_SUCURSAL"].astype(str).str.strip()
        df = df.merge(tr, on=["SKU_PRODUCTO", "ID_SUCURSAL"], how="left")
        df["QTY_TRANSITO"] = df["QTY_TRANSITO"].fillna(0)
    else:
        df["QTY_TRANSITO"] = 0

    df["CBM_TRANSITO"] = df["QTY_TRANSITO"] * df["DENSIDAD"]

    # ── 6. Merge ventas 90d ─────────────────────────────────────────────
    if not ventas_90d.empty:
        v90 = ventas_90d.copy()
        _safe_numeric(v90, ["UNIDADES_90D", "NETO_90D", "DIAS_CON_VENTA"])
        if "ID_SUCURSAL" in v90.columns:
            v90["ID_SUCURSAL"] = v90["ID_SUCURSAL"].astype(str).str.strip()
            v90_agg = v90.groupby(
                ["SKU_PRODUCTO", "ID_SUCURSAL"], as_index=False,
            ).agg(
                UNIDADES_90D=("UNIDADES_90D", "sum"),
                NETO_90D=("NETO_90D", "sum"),
                DIAS_CON_VENTA=("DIAS_CON_VENTA", "max"),
            )
            df = df.merge(v90_agg, on=["SKU_PRODUCTO", "ID_SUCURSAL"], how="left")
    for c in ["UNIDADES_90D", "NETO_90D", "DIAS_CON_VENTA"]:
        if c not in df.columns:
            df[c] = 0
        df[c] = df[c].fillna(0)

    # ── 7. Derived metrics ──────────────────────────────────────────────
    df["VENTA_DIA"] = df["UNIDADES_90D"] / 90
    df["MOI_TIENDA"] = np.where(
        df["VENTA_DIA"] > 0,
        df["STOCK_UNIDADES"] / (df["VENTA_DIA"] * 30),
        np.where(df["STOCK_UNIDADES"] > 0, 999, 0),
    )

    # VN per CBM (how productive is the space)
    df["VN_90D_POR_CBM"] = np.where(
        df["CBM_STOCK"] > 0, df["NETO_90D"] / df["CBM_STOCK"], 0,
    )

    # Profile days coverage
    df["DIAS_VENTA_PERFIL"] = np.where(
        df["VENTA_DIA"] > 0,
        df.get("PERFIL_TIENDAS", 0) / df["VENTA_DIA"],
        np.where(df.get("PERFIL_TIENDAS", 0) > 0, 999, 0),
    )

    # Profile flags
    df["PERFIL_EXCESIVO"] = df["DIAS_VENTA_PERFIL"] > 90
    df["SIN_VENTA_CON_PERFIL"] = (
        (df.get("PERFIL_TIENDAS", 0) > 0) & (df["UNIDADES_90D"] == 0)
    )

    return df


def _aggregate_by_tienda(
    df_detail: pd.DataFrame,
    tienda_dim: pd.DataFrame,
    factor: float,
    umbral: float,
) -> pd.DataFrame:
    """Aggregate detail data by store.

    Returns one row per store with volumetric KPIs.
    """
    if df_detail.empty:
        return pd.DataFrame()

    agg = df_detail.groupby("ID_SUCURSAL", as_index=False).agg(
        CBM_STOCK=("CBM_STOCK", "sum"),
        CBM_PERFIL=("CBM_PERFIL", "sum"),
        CBM_TRANSITO=("CBM_TRANSITO", "sum"),
        NETO_90D=("NETO_90D", "sum"),
        UNIDADES_90D=("UNIDADES_90D", "sum"),
        STOCK_UNIDADES=("STOCK_UNIDADES", "sum"),
        N_SKUS=("SKU_PRODUCTO", "nunique"),
        N_SKUS_CON_DENSIDAD=("DENSIDAD", lambda x: (x > 0).sum()),
    )

    # Merge tienda dimensions
    td_cols = [
        "ID_SUCURSAL", "DESCRIPCION_SUCURSAL", "MTS2", "CLUSTER",
        "SUPERVISOR", "TIPO", "GRUPO", "ZONA", "CIUDAD",
        "LATITUD", "LONGITUD",
    ]
    td_cols = [c for c in td_cols if c in tienda_dim.columns]
    td = tienda_dim[td_cols].copy()
    td["ID_SUCURSAL"] = td["ID_SUCURSAL"].astype(str).str.strip()
    _safe_numeric(td, ["MTS2", "LATITUD", "LONGITUD"])
    td = td.drop_duplicates(subset=["ID_SUCURSAL"])

    df = agg.merge(td, on="ID_SUCURSAL", how="left")
    df["MTS2"] = df["MTS2"].fillna(0)

    # Capacity and occupancy
    df["CAPACIDAD_CBM"] = df["MTS2"] * factor
    df["OCUPACION_PCT"] = np.where(
        df["CAPACIDAD_CBM"] > 0,
        df["CBM_STOCK"] / df["CAPACIDAD_CBM"] * 100,
        0,
    )
    df["OCUPACION_CON_TRANSITO"] = np.where(
        df["CAPACIDAD_CBM"] > 0,
        (df["CBM_STOCK"] + df["CBM_TRANSITO"]) / df["CAPACIDAD_CBM"] * 100,
        0,
    )

    # Risk classification
    df["RIESGO"] = df["OCUPACION_PCT"].apply(lambda x: _classify_risk(x, umbral))

    # VN per m2
    df["VN_M2_90D"] = np.where(df["MTS2"] > 0, df["NETO_90D"] / df["MTS2"], 0)

    return df.sort_values("OCUPACION_PCT", ascending=False).reset_index(drop=True)


def _aggregate_by_sku(df_detail: pd.DataFrame) -> pd.DataFrame:
    """Aggregate detail data by SKU across all stores."""
    if df_detail.empty:
        return pd.DataFrame()

    agg = df_detail.groupby("SKU_PRODUCTO", as_index=False).agg(
        CBM_STOCK_TOTAL=("CBM_STOCK", "sum"),
        CBM_PERFIL_TOTAL=("CBM_PERFIL", "sum"),
        CBM_TRANSITO_TOTAL=("CBM_TRANSITO", "sum"),
        STOCK_TOTAL_UND=("STOCK_UNIDADES", "sum"),
        PERFIL_TOTAL_UND=("PERFIL_TIENDAS", "sum"),
        NETO_90D_TOTAL=("NETO_90D", "sum"),
        UNIDADES_90D_TOTAL=("UNIDADES_90D", "sum"),
        N_TIENDAS=("ID_SUCURSAL", "nunique"),
        DENSIDAD=("DENSIDAD", "first"),
    )

    # Add product dimensions (first occurrence)
    dim_cols = ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA",
                "SUBLINEA", "MARCA", "MIX_OFICIAL"]
    available = [c for c in dim_cols if c in df_detail.columns]
    if len(available) > 1:
        dims = df_detail[available].drop_duplicates(subset=["SKU_PRODUCTO"])
        agg = agg.merge(dims, on="SKU_PRODUCTO", how="left")

    # Efficiency: VN per CBM
    agg["VN_POR_CBM"] = np.where(
        agg["CBM_STOCK_TOTAL"] > 0,
        agg["NETO_90D_TOTAL"] / agg["CBM_STOCK_TOTAL"],
        0,
    )

    return agg.sort_values("CBM_STOCK_TOTAL", ascending=False).reset_index(drop=True)


# ============================================================================
# TAB 1: RESUMEN
# ============================================================================

def _render_tab_resumen(df_tienda: pd.DataFrame, df_detail: pd.DataFrame) -> None:
    """Render overview KPIs and summary charts."""
    if df_tienda.empty:
        st.warning("Sin datos de tiendas para mostrar.")
        return

    # ── KPIs row ────────────────────────────────────────────────────────
    total_tiendas = len(df_tienda)
    ocup_prom = df_tienda["OCUPACION_PCT"].mean()
    tiendas_alto = (df_tienda["RIESGO"].isin(["CRITICO", "ALTO"])).sum()
    tiendas_critico = (df_tienda["RIESGO"] == "CRITICO").sum()
    cbm_stock_total = df_tienda["CBM_STOCK"].sum()
    cbm_perfil_total = df_tienda["CBM_PERFIL"].sum()

    # Count SKUs by density origin
    skus_sin_densidad = 0
    skus_con_densidad_maestra = 0
    skus_con_densidad_imputada = 0
    if "ORIGEN_DENSIDAD" in df_detail.columns:
        origins = df_detail.drop_duplicates("SKU_PRODUCTO")["ORIGEN_DENSIDAD"].value_counts()
        skus_sin_densidad = origins.get("SIN_DENSIDAD", 0)
        skus_con_densidad_maestra = origins.get("MAESTRA", 0)
        skus_con_densidad_imputada = (
            df_detail.drop_duplicates("SKU_PRODUCTO")["ORIGEN_DENSIDAD"]
            .isin(["SUBLINEA_MARCA", "LINEA_MARCA", "AREA_MARCA",
                    "SUBLINEA", "LINEA", "AREA", "GLOBAL"])
            .sum()
        )
    elif "DENSIDAD" in df_detail.columns:
        skus_sin_densidad = df_detail.loc[
            df_detail["DENSIDAD"] == 0, "SKU_PRODUCTO"
        ].nunique()

    cbm_transito_total = df_tienda["CBM_TRANSITO"].sum()

    cols = st.columns(6)
    kpi_data = [
        ("Total Tiendas", f"{total_tiendas:,}", COLORS["primary"]),
        ("Ocupacion Prom", _fmt_pct(ocup_prom), COLORS["tertiary_blue"]),
        ("Riesgo Alto+Critico", f"{tiendas_alto:,}", COLORS["status_at_risk"]),
        ("CBM Stock Total", _fmt_cbm(cbm_stock_total), COLORS["secondary"]),
        ("CBM Transito", _fmt_cbm(cbm_transito_total), COLORS["tertiary_teal"]),
        ("Densidad Maestra", f"{skus_con_densidad_maestra:,}", COLORS["status_on_track"]),
    ]
    for col, (label, value, color) in zip(cols, kpi_data):
        with col:
            _kpi_card(label, value, color)

    # ── Density coverage info ───────────────────────────────────────────
    if "ORIGEN_DENSIDAD" in df_detail.columns:
        origins = (
            df_detail.drop_duplicates("SKU_PRODUCTO")["ORIGEN_DENSIDAD"]
            .value_counts()
            .to_dict()
        )
        parts = []
        if origins.get("MAESTRA", 0) > 0:
            parts.append(f"**{origins['MAESTRA']:,}** con densidad en maestra")
        imputados = sum(
            origins.get(k, 0) for k in
            ["SUBLINEA_MARCA", "LINEA_MARCA", "AREA_MARCA",
             "SUBLINEA", "LINEA", "AREA", "GLOBAL"]
        )
        if imputados > 0:
            parts.append(f"**{imputados:,}** con densidad imputada (promedio categoria)")
        if origins.get("SIN_DENSIDAD", 0) > 0:
            parts.append(f"**{origins['SIN_DENSIDAD']:,}** sin densidad (excluidos del calculo)")
        if parts:
            st.info("📏 **Cobertura de Densidad:** " + " · ".join(parts))

    st.markdown("")

    # ── Charts row ──────────────────────────────────────────────────────
    col1, col2 = st.columns(2)

    with col1:
        # Histogram of occupancy
        fig = go.Figure()
        fig.add_trace(go.Histogram(
            x=df_tienda["OCUPACION_PCT"],
            nbinsx=20,
            marker_color=COLORS["primary"],
            opacity=0.85,
            hovertemplate="Rango: %{x:.0f}%<br>Tiendas: %{y}<extra></extra>",
        ))
        fig.add_vline(
            x=85, line_dash="dash", line_color=COLORS["status_at_risk"],
            annotation_text="Umbral riesgo",
        )
        fig.update_layout(
            **dorel_layout(
                title=dict(text="Distribucion de Ocupacion (%)", font=dict(size=14)),
                xaxis=dict(title="Ocupacion %"),
                yaxis=dict(title="N° Tiendas"),
                height=380,
            )
        )
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        # Donut by risk
        risk_counts = (
            df_tienda.groupby("RIESGO", as_index=False)
            .size()
            .rename(columns={"size": "N"})
        )
        # Ensure order
        risk_counts["RIESGO"] = pd.Categorical(
            risk_counts["RIESGO"], categories=_RISK_ORDER, ordered=True,
        )
        risk_counts = risk_counts.sort_values("RIESGO").dropna(subset=["RIESGO"])
        colors = [_RISK_COLORS.get(r, "#ccc") for r in risk_counts["RIESGO"]]
        labels = [_RISK_LABELS.get(r, r) for r in risk_counts["RIESGO"]]

        fig = go.Figure(go.Pie(
            labels=labels,
            values=risk_counts["N"],
            hole=0.55,
            marker=dict(colors=colors),
            textinfo="label+value",
            hovertemplate="%{label}: %{value} tiendas<extra></extra>",
        ))
        fig.update_layout(
            **dorel_layout(
                title=dict(text="Clasificacion de Riesgo", font=dict(size=14)),
                height=380,
                showlegend=False,
            )
        )
        st.plotly_chart(fig, use_container_width=True)

    # ── Top 10 most occupied ────────────────────────────────────────────
    top10 = df_tienda.head(10).copy()
    if not top10.empty:
        nombre_col = "DESCRIPCION_SUCURSAL" if "DESCRIPCION_SUCURSAL" in top10.columns else "ID_SUCURSAL"
        fig = go.Figure()
        fig.add_trace(go.Bar(
            y=top10[nombre_col],
            x=top10["OCUPACION_PCT"],
            orientation="h",
            marker_color=[
                _RISK_COLORS.get(r, COLORS["primary"]) for r in top10["RIESGO"]
            ],
            text=[_fmt_pct(v) for v in top10["OCUPACION_PCT"]],
            textposition="auto",
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Ocupacion: %{x:.1f}%<br>"
                "<extra></extra>"
            ),
        ))
        fig.update_layout(
            **dorel_layout(
                title=dict(text="Top 10 Tiendas Mas Ocupadas", font=dict(size=14)),
                xaxis=dict(title="Ocupacion %"),
                yaxis=dict(autorange="reversed"),
                height=420,
            )
        )
        st.plotly_chart(fig, use_container_width=True)


# ============================================================================
# TAB 2: POR TIENDA
# ============================================================================

def _render_tab_tiendas(
    df_tienda: pd.DataFrame,
    df_detail: pd.DataFrame,
) -> None:
    """Render per-store ranking table with drill-down."""
    if df_tienda.empty:
        st.warning("Sin datos de tiendas.")
        return

    # Display columns
    display_cols = [
        "DESCRIPCION_SUCURSAL", "CLUSTER", "SUPERVISOR", "TIPO",
        "MTS2", "CBM_STOCK", "CBM_TRANSITO", "CAPACIDAD_CBM",
        "OCUPACION_PCT", "OCUPACION_CON_TRANSITO", "RIESGO",
        "N_SKUS", "VN_M2_90D_FMT",
    ]
    df_show = df_tienda.copy()
    # Format VN/m2 as Chilean pesos with dots
    if "VN_M2_90D" in df_show.columns:
        df_show["VN_M2_90D_FMT"] = df_show["VN_M2_90D"].apply(fmt_clp)
    display_cols = [c for c in display_cols if c in df_show.columns]
    df_show = df_show[display_cols]

    column_config = {
        "DESCRIPCION_SUCURSAL": st.column_config.TextColumn("Tienda", width="medium"),
        "CLUSTER": st.column_config.TextColumn("Cluster", width="small"),
        "SUPERVISOR": st.column_config.TextColumn("Supervisor", width="small"),
        "TIPO": st.column_config.TextColumn("Tipo", width="small"),
        "MTS2": st.column_config.NumberColumn("M2", format="%.0f"),
        "CBM_STOCK": st.column_config.NumberColumn("CBM Stock", format="%.1f"),
        "CBM_TRANSITO": st.column_config.NumberColumn("CBM Transito", format="%.1f"),
        "CAPACIDAD_CBM": st.column_config.NumberColumn("Capacidad CBM", format="%.1f"),
        "OCUPACION_PCT": st.column_config.ProgressColumn(
            "Ocupacion %", min_value=0, max_value=150, format="%.1f%%",
        ),
        "OCUPACION_CON_TRANSITO": st.column_config.NumberColumn(
            "Ocup. c/Transito %", format="%.1f%%",
        ),
        "RIESGO": st.column_config.TextColumn("Riesgo", width="small"),
        "N_SKUS": st.column_config.NumberColumn("SKUs", format="%d"),
        "VN_M2_90D_FMT": st.column_config.TextColumn("VN/m2 (90d)", width="small"),
    }

    st.dataframe(
        df_show,
        column_config=column_config,
        use_container_width=True,
        hide_index=True,
        height=500,
    )

    download_buttons(df_tienda, prefix="cap_vol_tiendas")

    # ── Drill-down per store ────────────────────────────────────────────
    st.markdown("---")
    st.markdown("##### Detalle por Tienda")

    if "DESCRIPCION_SUCURSAL" in df_tienda.columns:
        store_options = df_tienda.set_index("ID_SUCURSAL")["DESCRIPCION_SUCURSAL"].to_dict()
    else:
        store_options = {sid: sid for sid in df_tienda["ID_SUCURSAL"]}

    selected_store = st.selectbox(
        "Seleccionar tienda para drill-down",
        options=list(store_options.keys()),
        format_func=lambda x: f"{store_options.get(x, x)} ({x})",
        key="cv_store_drilldown",
    )

    if selected_store and not df_detail.empty:
        store_detail = df_detail[
            df_detail["ID_SUCURSAL"] == selected_store
        ].sort_values("CBM_STOCK", ascending=False)

        if not store_detail.empty:
            # Format monetary columns as CLP
            sd = store_detail.copy()
            if "NETO_90D" in sd.columns:
                sd["NETO_90D_FMT"] = sd["NETO_90D"].apply(fmt_clp)
            if "VN_90D_POR_CBM" in sd.columns:
                sd["VN_90D_POR_CBM_FMT"] = sd["VN_90D_POR_CBM"].apply(fmt_clp)

            drill_cols = [
                "SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA",
                "STOCK_UNIDADES", "DENSIDAD", "CBM_STOCK", "CBM_PERFIL",
                "UNIDADES_90D", "NETO_90D_FMT", "VN_90D_POR_CBM_FMT", "MOI_TIENDA",
            ]
            drill_cols = [c for c in drill_cols if c in sd.columns]

            drill_config = {
                "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
                "SKU_NOM_PRODUCTO": st.column_config.TextColumn("Producto", width="medium"),
                "AREA": st.column_config.TextColumn("Area", width="small"),
                "LINEA": st.column_config.TextColumn("Linea", width="small"),
                "STOCK_UNIDADES": st.column_config.NumberColumn("Stock Und", format="%d"),
                "DENSIDAD": st.column_config.NumberColumn("Densidad (CBM)", format="%.4f"),
                "CBM_STOCK": st.column_config.NumberColumn("CBM Stock", format="%.2f"),
                "CBM_PERFIL": st.column_config.NumberColumn("CBM Perfil", format="%.2f"),
                "UNIDADES_90D": st.column_config.NumberColumn("Venta 90d", format="%d"),
                "NETO_90D_FMT": st.column_config.TextColumn("VN 90d", width="small"),
                "VN_90D_POR_CBM_FMT": st.column_config.TextColumn("VN/CBM", width="small"),
                "MOI_TIENDA": st.column_config.NumberColumn("MOI", format="%.1f"),
            }

            st.dataframe(
                sd[drill_cols],
                column_config=drill_config,
                use_container_width=True,
                hide_index=True,
                height=400,
            )
            st.caption(f"{len(store_detail):,} SKUs en esta tienda")
        else:
            st.info("Sin detalle SKU para esta tienda.")


# ============================================================================
# TAB 3: EFICIENCIA ESPACIO (POR SKU)
# ============================================================================

def _render_tab_eficiencia(df_sku: pd.DataFrame) -> None:
    """Render SKU-level space efficiency: scatter + table of 'space hogs'."""
    if df_sku.empty:
        st.warning("Sin datos de SKU.")
        return

    # Filter to SKUs with actual CBM
    df_plot = df_sku[df_sku["CBM_STOCK_TOTAL"] > 0].copy()

    if df_plot.empty:
        st.info("No hay SKUs con volumen CBM > 0 (verificar datos de densidad).")
        return

    # ── Scatter: CBM total vs VN/CBM ────────────────────────────────────
    # Identify quadrants
    cbm_median = df_plot["CBM_STOCK_TOTAL"].median()
    vn_cbm_median = df_plot.loc[df_plot["VN_POR_CBM"] > 0, "VN_POR_CBM"].median()
    if pd.isna(vn_cbm_median) or vn_cbm_median == 0:
        vn_cbm_median = 1

    # Color by quadrant
    df_plot["CUADRANTE"] = np.select(
        [
            (df_plot["CBM_STOCK_TOTAL"] >= cbm_median) & (df_plot["VN_POR_CBM"] < vn_cbm_median),
            (df_plot["CBM_STOCK_TOTAL"] >= cbm_median) & (df_plot["VN_POR_CBM"] >= vn_cbm_median),
            (df_plot["CBM_STOCK_TOTAL"] < cbm_median) & (df_plot["VN_POR_CBM"] < vn_cbm_median),
        ],
        ["Alto Vol / Bajo Giro", "Alto Vol / Alto Giro", "Bajo Vol / Bajo Giro"],
        default="Bajo Vol / Alto Giro",
    )

    cuadrante_colors = {
        "Alto Vol / Bajo Giro": COLORS["status_critical"],
        "Alto Vol / Alto Giro": COLORS["status_on_track"],
        "Bajo Vol / Bajo Giro": COLORS["status_at_risk"],
        "Bajo Vol / Alto Giro": COLORS["tertiary_blue"],
    }

    fig = go.Figure()
    for cuad, color in cuadrante_colors.items():
        mask = df_plot["CUADRANTE"] == cuad
        if mask.any():
            sub = df_plot[mask]
            fig.add_trace(go.Scatter(
                x=sub["CBM_STOCK_TOTAL"],
                y=sub["VN_POR_CBM"],
                mode="markers",
                name=cuad,
                marker=dict(size=7, color=color, opacity=0.7),
                text=sub.get("SKU_NOM_PRODUCTO", sub["SKU_PRODUCTO"]),
                hovertemplate=(
                    "<b>%{text}</b><br>"
                    "CBM Total: %{x:.2f}<br>"
                    "VN/CBM: $%{y:,.0f}<br>"
                    "<extra></extra>"
                ),
            ))

    fig.add_hline(y=vn_cbm_median, line_dash="dash", line_color="#999", opacity=0.5)
    fig.add_vline(x=cbm_median, line_dash="dash", line_color="#999", opacity=0.5)

    fig.update_layout(
        **dorel_layout(
            title=dict(text="Eficiencia de Espacio: CBM vs VN/CBM", font=dict(size=14)),
            xaxis=dict(title="CBM Stock Total (m3)"),
            yaxis=dict(title="VN 90d por CBM ($)"),
            height=500,
        )
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── Table: Space Hogs ───────────────────────────────────────────────
    st.markdown("##### SKUs con Alto Volumen y Bajo Giro")
    st.caption(
        "SKUs que ocupan mas espacio de lo que generan en ventas. "
        "Candidatos a reducir perfil o retirar de tiendas."
    )

    space_hogs = df_plot[
        df_plot["CUADRANTE"] == "Alto Vol / Bajo Giro"
    ].sort_values("CBM_STOCK_TOTAL", ascending=False).head(50)

    if space_hogs.empty:
        st.success("No hay SKUs en el cuadrante de alto volumen / bajo giro.")
        return

    # Format monetary columns as CLP
    sh = space_hogs.copy()
    if "NETO_90D_TOTAL" in sh.columns:
        sh["NETO_90D_TOTAL_FMT"] = sh["NETO_90D_TOTAL"].apply(fmt_clp)
    if "VN_POR_CBM" in sh.columns:
        sh["VN_POR_CBM_FMT"] = sh["VN_POR_CBM"].apply(fmt_clp)

    hog_cols = [
        "SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA",
        "CBM_STOCK_TOTAL", "NETO_90D_TOTAL_FMT", "VN_POR_CBM_FMT",
        "N_TIENDAS", "STOCK_TOTAL_UND", "DENSIDAD",
    ]
    hog_cols = [c for c in hog_cols if c in sh.columns]

    hog_config = {
        "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
        "SKU_NOM_PRODUCTO": st.column_config.TextColumn("Producto", width="medium"),
        "AREA": st.column_config.TextColumn("Area", width="small"),
        "LINEA": st.column_config.TextColumn("Linea", width="small"),
        "MARCA": st.column_config.TextColumn("Marca", width="small"),
        "CBM_STOCK_TOTAL": st.column_config.NumberColumn("CBM Total", format="%.2f"),
        "NETO_90D_TOTAL_FMT": st.column_config.TextColumn("VN 90d", width="small"),
        "VN_POR_CBM_FMT": st.column_config.TextColumn("VN/CBM", width="small"),
        "N_TIENDAS": st.column_config.NumberColumn("Tiendas", format="%d"),
        "STOCK_TOTAL_UND": st.column_config.NumberColumn("Stock Und", format="%,d"),
        "DENSIDAD": st.column_config.NumberColumn("Densidad", format="%.4f"),
    }

    st.dataframe(
        sh[hog_cols],
        column_config=hog_config,
        use_container_width=True,
        hide_index=True,
        height=400,
    )

    download_buttons(df_sku, prefix="cap_vol_eficiencia_sku")


# ============================================================================
# TAB 4: CUESTIONAMIENTO DE PERFIL
# ============================================================================

def _render_tab_perfil(df_detail: pd.DataFrame) -> None:
    """Render profile questioning: excessive min exhibition vs actual sales."""
    if df_detail.empty:
        st.warning("Sin datos de detalle.")
        return

    # Filter rows with profile issues
    mask_excesivo = df_detail["PERFIL_EXCESIVO"] == True  # noqa: E712
    mask_sin_venta = df_detail["SIN_VENTA_CON_PERFIL"] == True  # noqa: E712
    mask_any = mask_excesivo | mask_sin_venta

    df_issues = df_detail[mask_any].copy()

    # ── KPIs ────────────────────────────────────────────────────────────
    n_issues = len(df_issues)
    n_skus_afectados = df_issues["SKU_PRODUCTO"].nunique() if not df_issues.empty else 0
    n_tiendas_afectadas = df_issues["ID_SUCURSAL"].nunique() if not df_issues.empty else 0
    cbm_perfil_excesivo = df_issues["CBM_PERFIL"].sum() if not df_issues.empty else 0
    n_sin_venta = mask_sin_venta.sum()
    n_excesivo = mask_excesivo.sum()

    cols = st.columns(6)
    kpis = [
        ("Alertas Totales", f"{n_issues:,}", COLORS["status_critical"]),
        ("SKUs Afectados", f"{n_skus_afectados:,}", COLORS["status_at_risk"]),
        ("Tiendas Afectadas", f"{n_tiendas_afectadas:,}", COLORS["status_at_risk"]),
        ("CBM Perfil en Riesgo", _fmt_cbm(cbm_perfil_excesivo), COLORS["secondary"]),
        ("Perfil Excesivo (>90d)", f"{n_excesivo:,}", COLORS["status_at_risk"]),
        ("Sin Venta con Perfil", f"{n_sin_venta:,}", COLORS["status_critical"]),
    ]
    for col, (label, value, color) in zip(cols, kpis):
        with col:
            _kpi_card(label, value, color)

    if df_issues.empty:
        st.success("No se detectaron perfiles excesivos. Todos los perfiles estan alineados con las ventas.")
        return

    st.markdown("")

    # ── Treemap: CBM Perfil by Area > Linea ─────────────────────────────
    if all(c in df_issues.columns for c in ["AREA", "LINEA", "CBM_PERFIL"]):
        treemap_data = df_issues.groupby(
            ["AREA", "LINEA"], as_index=False,
        ).agg(CBM_PERFIL=("CBM_PERFIL", "sum"))

        if not treemap_data.empty:
            fig = go.Figure(go.Treemap(
                labels=treemap_data["LINEA"],
                parents=treemap_data["AREA"],
                values=treemap_data["CBM_PERFIL"],
                textinfo="label+value",
                hovertemplate=(
                    "<b>%{label}</b><br>"
                    "Area: %{parent}<br>"
                    "CBM Perfil: %{value:.1f}<br>"
                    "<extra></extra>"
                ),
                marker=dict(
                    colorscale="RdYlGn_r",
                    line=dict(width=1, color="#fff"),
                ),
            ))
            fig.update_layout(
                **dorel_layout(
                    title=dict(
                        text="CBM Perfil en Alertas por Area / Linea",
                        font=dict(size=14),
                    ),
                    height=400,
                )
            )
            st.plotly_chart(fig, use_container_width=True)

    # ── Detail table ────────────────────────────────────────────────────
    st.markdown("##### Detalle de Alertas de Perfil")

    # Add a readable flag
    df_issues["TIPO_ALERTA"] = np.where(
        df_issues["SIN_VENTA_CON_PERFIL"],
        "Sin Venta con Perfil",
        "Perfil Excesivo (>90d venta)",
    )

    profile_cols = [
        "SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "ID_SUCURSAL",
        "TIPO_ALERTA", "PERFIL_TIENDAS", "STOCK_UNIDADES",
        "UNIDADES_90D", "DIAS_VENTA_PERFIL", "CBM_PERFIL",
        "CBM_STOCK", "AREA", "LINEA",
    ]
    profile_cols = [c for c in profile_cols if c in df_issues.columns]

    profile_config = {
        "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
        "SKU_NOM_PRODUCTO": st.column_config.TextColumn("Producto", width="medium"),
        "ID_SUCURSAL": st.column_config.TextColumn("Sucursal", width="small"),
        "TIPO_ALERTA": st.column_config.TextColumn("Tipo Alerta", width="medium"),
        "PERFIL_TIENDAS": st.column_config.NumberColumn("Perfil Und", format="%d"),
        "STOCK_UNIDADES": st.column_config.NumberColumn("Stock Und", format="%d"),
        "UNIDADES_90D": st.column_config.NumberColumn("Venta 90d", format="%d"),
        "DIAS_VENTA_PERFIL": st.column_config.NumberColumn("Dias Venta Perfil", format="%.0f"),
        "CBM_PERFIL": st.column_config.NumberColumn("CBM Perfil", format="%.3f"),
        "CBM_STOCK": st.column_config.NumberColumn("CBM Stock", format="%.3f"),
        "AREA": st.column_config.TextColumn("Area", width="small"),
        "LINEA": st.column_config.TextColumn("Linea", width="small"),
    }

    st.dataframe(
        df_issues[profile_cols].sort_values("CBM_PERFIL", ascending=False),
        column_config=profile_config,
        use_container_width=True,
        hide_index=True,
        height=450,
    )

    download_buttons(df_issues, prefix="cap_vol_perfil_alertas")


# ============================================================================
# TAB 5: MAPA
# ============================================================================

def _render_tab_mapa(df_tienda: pd.DataFrame) -> None:
    """Render geographic map of stores colored by occupancy."""
    if df_tienda.empty:
        st.warning("Sin datos de tiendas.")
        return

    if "LATITUD" not in df_tienda.columns or "LONGITUD" not in df_tienda.columns:
        st.info("Sin coordenadas geograficas disponibles para el mapa.")
        return

    df_map = df_tienda[
        (df_tienda["LATITUD"] != 0) & (df_tienda["LONGITUD"] != 0)
    ].copy()

    if df_map.empty:
        st.info("Sin coordenadas validas para el mapa.")
        return

    nombre_col = "DESCRIPCION_SUCURSAL" if "DESCRIPCION_SUCURSAL" in df_map.columns else "ID_SUCURSAL"

    # Size: CBM Stock (normalized for visualization)
    max_cbm = df_map["CBM_STOCK"].max()
    df_map["_size"] = np.where(
        max_cbm > 0,
        df_map["CBM_STOCK"] / max_cbm * 30 + 5,
        10,
    )

    fig = go.Figure()
    for riesgo in _RISK_ORDER:
        mask = df_map["RIESGO"] == riesgo
        if not mask.any():
            continue
        sub = df_map[mask]
        fig.add_trace(go.Scattermapbox(
            lat=sub["LATITUD"],
            lon=sub["LONGITUD"],
            mode="markers",
            name=_RISK_LABELS.get(riesgo, riesgo),
            marker=dict(
                size=sub["_size"],
                color=_RISK_COLORS.get(riesgo, COLORS["primary"]),
                opacity=0.8,
            ),
            text=sub[nombre_col],
            customdata=np.stack([
                sub["OCUPACION_PCT"],
                sub["CBM_STOCK"],
                sub["MTS2"],
                sub.get("SUPERVISOR", ""),
                sub.get("TIPO", ""),
            ], axis=-1),
            hovertemplate=(
                "<b>%{text}</b><br>"
                "Ocupacion: %{customdata[0]:.1f}%<br>"
                "CBM Stock: %{customdata[1]:.1f}<br>"
                "M2: %{customdata[2]:.0f}<br>"
                "Supervisor: %{customdata[3]}<br>"
                "Tipo: %{customdata[4]}<br>"
                "<extra></extra>"
            ),
        ))

    fig.update_layout(
        **dorel_layout(
            title=dict(text="Mapa de Ocupacion Volumetrica", font=dict(size=14)),
            mapbox=dict(
                style="carto-positron",
                center=dict(lat=-33.45, lon=-70.65),
                zoom=5,
            ),
            height=600,
            margin=dict(l=0, r=0, t=50, b=0),
        )
    )
    st.plotly_chart(fig, use_container_width=True)


# ============================================================================
# MAIN RENDER FUNCTION
# ============================================================================

def render_capacidad_volumetrica(conn) -> None:
    """Main entry point for the Capacidad Volumetrica module."""
    from utils.ui_components import page_header

    st.html(
        page_header(
            "Capacidad Volumetrica Tiendas",
            "Analisis de ocupacion de espacio fisico en tiendas retail",
        )
    )

    # ── Load data ───────────────────────────────────────────────────────
    stock_hig, tienda_dim, maestra, ventas_90d, transito = _load_data(conn)
    stock_hig = apply_pm_filter(stock_hig)

    if stock_hig.empty:
        st.error("No se pudieron cargar datos de stock por tienda.")
        return
    if tienda_dim.empty:
        st.error("No se pudieron cargar datos de tiendas.")
        return

    # ── Sidebar filters ─────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("---")
        st.markdown("**Parametros Volumetricos**")

        factor_cbm = st.slider(
            "Factor CBM/m2 (altura estanterias)",
            min_value=1.0, max_value=4.0,
            value=_DEFAULT_FACTOR_CBM_M2, step=0.1,
            help="Metros cubicos de capacidad por metro cuadrado de tienda. "
                 "Default 2.0 = estanterias de ~2m altura promedio.",
            key="cv_factor_cbm",
        )

        umbral_riesgo = st.slider(
            "Umbral riesgo (%)",
            min_value=50, max_value=100,
            value=_DEFAULT_UMBRAL_RIESGO, step=5,
            help="Porcentaje de ocupacion sobre el cual se clasifica como riesgo ALTO.",
            key="cv_umbral_riesgo",
        )

    # ── Diagnostic: show density column status ─────────────────────────
    with st.expander("🔍 Diagnostico de datos de densidad", expanded=False):
        # Check what density-related columns exist in maestra
        dens_col = _find_density_column(maestra)
        all_cols = sorted(maestra.columns.tolist())
        vol_related = [c for c in all_cols if any(
            kw in c.upper() for kw in
            ["DENS", "CBM", "VOL", "M3", "PESO", "WEIGHT", "ALTO", "ANCHO", "PROF"]
        )]

        if dens_col:
            vals = _to_numeric_robust(maestra[dens_col])
            n_con = (vals > 0).sum()
            n_total = len(vals)
            st.success(
                f"Columna densidad: **{dens_col}** — "
                f"**{n_con:,}**/{n_total:,} SKUs con valor > 0 "
                f"(rango: {vals[vals>0].min():.6f} – {vals[vals>0].max():.6f})"
            )
        else:
            st.warning(
                "No se encontro columna de densidad con datos > 0. "
                "Buscamos: DENSIDAD, CBM, VOLUMEN, M3, VOLUMEN_UNITARIO."
            )
            # Show raw values for debugging
            if "DENSIDAD" in maestra.columns:
                raw = maestra["DENSIDAD"].dropna()
                if not raw.empty:
                    sample = raw[raw.astype(str).str.strip() != ""].head(10)
                    st.write("Muestra de valores crudos DENSIDAD:", sample.tolist())
                    st.write(f"Dtype: {maestra['DENSIDAD'].dtype}")

        # Check dimensions
        for dim_col in ["ALTO", "ANCHO", "PROFUNDIDAD"]:
            if dim_col in maestra.columns:
                dv = _to_numeric_robust(maestra[dim_col])
                n_pos = (dv > 0).sum()
                if n_pos > 0:
                    st.info(
                        f"**{dim_col}**: {n_pos:,} SKUs con valor > 0 "
                        f"(mediana: {dv[dv>0].median():.2f}, "
                        f"max: {dv[dv>0].max():.2f})"
                    )

        if vol_related:
            st.write("Columnas relacionadas:", vol_related)
        st.caption(f"Total columnas en maestra: {len(all_cols)}")

    # ── Build data ──────────────────────────────────────────────────────
    df_detail = _build_detail(stock_hig, tienda_dim, maestra, ventas_90d, transito)

    if df_detail.empty:
        st.warning(
            "No se pudo construir el analisis volumetrico. "
            "Verificar que existan tiendas activas con M2 > 0 y SKUs con DENSIDAD > 0."
        )
        return

    df_tienda = _aggregate_by_tienda(df_detail, tienda_dim, factor_cbm, umbral_riesgo)
    df_sku = _aggregate_by_sku(df_detail)

    # ── Global filters (cascading) ──────────────────────────────────────
    with st.sidebar:
        st.markdown("**Filtros**")

        # Supervisor
        if "SUPERVISOR" in df_tienda.columns:
            supervisors = sorted(df_tienda["SUPERVISOR"].dropna().unique())
            sel_supervisor = st.multiselect(
                "Supervisor", supervisors, key="cv_fil_supervisor",
            )
            if sel_supervisor:
                tienda_ids = set(
                    df_tienda.loc[
                        df_tienda["SUPERVISOR"].isin(sel_supervisor), "ID_SUCURSAL"
                    ]
                )
                df_tienda = df_tienda[df_tienda["ID_SUCURSAL"].isin(tienda_ids)]
                df_detail = df_detail[df_detail["ID_SUCURSAL"].isin(tienda_ids)]
                df_sku = _aggregate_by_sku(df_detail)

        # Tipo
        if "TIPO" in df_tienda.columns:
            tipos = sorted(df_tienda["TIPO"].dropna().unique())
            sel_tipo = st.multiselect("Tipo Tienda", tipos, key="cv_fil_tipo")
            if sel_tipo:
                tienda_ids = set(
                    df_tienda.loc[
                        df_tienda["TIPO"].isin(sel_tipo), "ID_SUCURSAL"
                    ]
                )
                df_tienda = df_tienda[df_tienda["ID_SUCURSAL"].isin(tienda_ids)]
                df_detail = df_detail[df_detail["ID_SUCURSAL"].isin(tienda_ids)]
                df_sku = _aggregate_by_sku(df_detail)

        # Cluster
        if "CLUSTER" in df_tienda.columns:
            clusters = sorted(df_tienda["CLUSTER"].dropna().unique())
            sel_cluster = st.multiselect("Cluster", clusters, key="cv_fil_cluster")
            if sel_cluster:
                tienda_ids = set(
                    df_tienda.loc[
                        df_tienda["CLUSTER"].isin(sel_cluster), "ID_SUCURSAL"
                    ]
                )
                df_tienda = df_tienda[df_tienda["ID_SUCURSAL"].isin(tienda_ids)]
                df_detail = df_detail[df_detail["ID_SUCURSAL"].isin(tienda_ids)]
                df_sku = _aggregate_by_sku(df_detail)

        # Zona
        if "ZONA" in df_tienda.columns:
            zonas = sorted(df_tienda["ZONA"].dropna().unique())
            sel_zona = st.multiselect("Zona", zonas, key="cv_fil_zona")
            if sel_zona:
                tienda_ids = set(
                    df_tienda.loc[
                        df_tienda["ZONA"].isin(sel_zona), "ID_SUCURSAL"
                    ]
                )
                df_tienda = df_tienda[df_tienda["ID_SUCURSAL"].isin(tienda_ids)]
                df_detail = df_detail[df_detail["ID_SUCURSAL"].isin(tienda_ids)]
                df_sku = _aggregate_by_sku(df_detail)

    # ── Tabs ────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 Resumen",
        "🏬 Por Tienda",
        "📦 Eficiencia Espacio",
        "⚠️ Cuestionamiento Perfil",
        "🗺️ Mapa",
    ])

    with tab1:
        _render_tab_resumen(df_tienda, df_detail)

    with tab2:
        _render_tab_tiendas(df_tienda, df_detail)

    with tab3:
        _render_tab_eficiencia(df_sku)

    with tab4:
        _render_tab_perfil(df_detail)

    with tab5:
        _render_tab_mapa(df_tienda)
