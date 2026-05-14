import os
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from io import BytesIO
from db.queries import (
    QUERY_STOCK_CRITICO_METRICS,
    QUERY_STOCK_CRITICO_DETAIL,
    QUERY_STOCK_CRITICO_SALES,
    QUERY_COGS_COMPANY_6M,
    QUERY_TRANSIT_STOCK_KPI,
)
from db.cache import cached_query as cq
from utils.filters import clasificar_canal, human_format, fmt_clp
from utils.export import download_buttons
from utils.budget import get_budget_cogs_monthly
from utils.ui_animations import lottie_spinner
from config import COLORS, dorel_layout, apply_pm_filter


AUTO_METRICS_FILE = "querystockcritico_metrics_auto.csv"

# ---------------------------------------------------------------------------
# Helper: condicion de antiguedad critica
# Regla: >= 12 meses  O  sin fecha ingreso (null) con MOI alto (>= 6m o sin ventas).
# Razon: si no tenemos fecha de ingreso pero el MOI es alto, el producto lleva
# tiempo sin rotar — no deberia quedar excluido del analisis critico.
# ---------------------------------------------------------------------------
def _cond_ant_critico(ant_series, moi_series):
    sin_fecha = ant_series.isna()
    alto_moi  = (moi_series >= 6) | moi_series.isna()
    return (ant_series >= 12) | (sin_fecha & alto_moi)
AUTO_DETAIL_FILE = "querystockcritico_detail_auto.csv"
AUTO_SALES_FILE = "ventadiaria_auto.csv"
CHUNK_SIZE = 500_000

# ============================================================================
# 9-BOX HEALTH MATRIX — CONSTANTS
# ============================================================================

_MOI_TIERS = ["0-3m", "3-6m", "6-8m", "8-12m", "12-24m", ">=24m"]
_ANT_TIERS = ["0-3m", "3-6m", "6-8m", "8-12m", "12-24m", ">=24m"]

# Gradient: 11 colors from green (score=0) to dark red (score=10)
_HEALTH_GRADIENT = [
    "#43A047", "#66BB6A", "#A5D6A7", "#C8E6C9",
    "#FFF176", "#F9A825", "#FB8C00", "#F4511E",
    "#E53935", "#C62828", "#B71C1C",
]
# Colors that need white text (dark backgrounds)
_WHITE_TEXT_COLORS = {"#43A047", "#66BB6A", "#FB8C00", "#F4511E", "#E53935", "#C62828", "#B71C1C"}


def _health_color(moi_tier, ant_tier):
    """Color based on combined risk score (sum of tier indices, 0-10)."""
    try:
        idx_m = _MOI_TIERS.index(moi_tier)
        idx_a = _ANT_TIERS.index(ant_tier)
    except ValueError:
        return "#ccc"
    score = min(idx_m + idx_a, 10)
    return _HEALTH_GRADIENT[score]


def _health_description(moi_tier, ant_tier):
    """Action description based on combined risk score."""
    try:
        idx_m = _MOI_TIERS.index(moi_tier)
        idx_a = _ANT_TIERS.index(ant_tier)
    except ValueError:
        return ""
    score = idx_m + idx_a
    if score <= 2:
        return "Stock saludable"
    if score <= 4:
        return "Monitorear"
    if score <= 6:
        return "Activar promociones"
    if score <= 8:
        return "Descuento agresivo"
    return "LIQUIDAR URGENTE"


def _fmt_cl(val):
    """Format number in Chilean style: $24.696.345 (dots as thousand sep)."""
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "$0"
    if pd.isna(val) or val == 0:
        return "$0"
    neg = val < 0
    formatted = f"{abs(val):,.0f}".replace(",", ".")
    return f"-${formatted}" if neg else f"${formatted}"


def _fmt_und(val):
    """Format units in Chilean style: 24.696 (dots as thousand sep)."""
    try:
        val = int(float(val))
    except (TypeError, ValueError):
        return "0"
    if val == 0:
        return "0"
    return f"{val:,}".replace(",", ".")


# ============================================================================
# 9-BOX HEALTH MATRIX — HELPERS
# ============================================================================

def _classify_health_tier(series):
    """Classify numeric months into health tier labels."""
    conditions = [
        series < 3,
        (series >= 3) & (series < 6),
        (series >= 6) & (series < 8),
        (series >= 8) & (series < 12),
        (series >= 12) & (series < 24),
        series >= 24,
    ]
    choices = _MOI_TIERS
    return np.select(conditions, choices, default="Sin Info")


def _prepare_health_data(df):
    """Aggregate to SKU level, classify tiers. Returns (df_valid, df_sin_info)."""
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()

    df = df.copy()
    # Capturar "sin fecha ingreso" ANTES del fillna (null → 0 borra la distincion)
    if "ANTIGUEDAD_MESES" in df.columns:
        df["_SIN_FECHA"] = pd.to_numeric(df["ANTIGUEDAD_MESES"], errors="coerce").isna()
    else:
        df["_SIN_FECHA"] = False
    for c in ["STOCK_COSTO", "STOCK_UNIDADES", "MOI", "ANTIGUEDAD_MESES", "COSTO_PROM_90_CIA"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    dim_cols = ["AREA", "LINEA", "SUBLINEA", "MARCA", "MODELO", "PROVEEDOR", "NOM_PRODUCTO"]
    available_dims = [c for c in dim_cols if c in df.columns]

    agg_dict = {"STOCK_COSTO": "sum"}
    if "STOCK_UNIDADES" in df.columns:
        agg_dict["STOCK_UNIDADES"] = "sum"
    if "COSTO_PROM_90_CIA" in df.columns:
        agg_dict["COSTO_PROM_90_CIA"] = "first"
    if "ANTIGUEDAD_MESES" in df.columns:
        agg_dict["ANTIGUEDAD_MESES"] = "max"
    if "MOI" in df.columns:
        agg_dict["MOI"] = "max"
    for c in available_dims:
        agg_dict[c] = "first"
    agg_dict["_SIN_FECHA"] = "any"

    sku = df.groupby("SKU_PRODUCTO", as_index=False).agg(agg_dict)

    # Recalculate MOI at SKU level
    if "COSTO_PROM_90_CIA" in sku.columns:
        sku["MOI"] = np.where(
            sku["COSTO_PROM_90_CIA"] > 0,
            (sku["STOCK_COSTO"] / sku["COSTO_PROM_90_CIA"]) / 30.44,
            0,
        )

    # Flag SKUs truly without MOI (no sales history) BEFORE overriding to 999
    sku["SIN_MOI"] = (sku["MOI"] <= 0) & (sku["STOCK_COSTO"] > 0)

    # Sin MOI (MOI=0, sin ventas) → treat as worst case (>=24m tier)
    # They have stock but no sales history → critical by definition
    sku.loc[sku["SIN_MOI"], "MOI"] = 999

    sku["TIER_MOI"] = _classify_health_tier(sku["MOI"])
    sku["TIER_ANTIGUEDAD"] = _classify_health_tier(sku["ANTIGUEDAD_MESES"])

    # Sin fecha + MOI alto → reclasificar como >=24m (peor caso: no sabemos cuanto lleva)
    mask_sin_fecha_alto_moi = sku["_SIN_FECHA"] & ((sku["MOI"] >= 6) | sku["SIN_MOI"])
    sku.loc[mask_sin_fecha_alto_moi, "TIER_ANTIGUEDAD"] = ">=24m"

    mask_valid = (sku["ANTIGUEDAD_MESES"] > 0) | mask_sin_fecha_alto_moi
    df_valid = sku[mask_valid].copy()
    df_sin_info = sku[~mask_valid].copy()

    return df_valid, df_sin_info


def _build_health_matrix(df_valid):
    """Build 6x6=36-cell matrix from classified SKU data."""
    all_combos = pd.DataFrame([
        {"TIER_MOI": m, "TIER_ANTIGUEDAD": a}
        for m in _MOI_TIERS for a in _ANT_TIERS
    ])

    if df_valid.empty:
        all_combos["N_SKUS"] = 0
        all_combos["STOCK_COSTO"] = 0.0
        all_combos["STOCK_UNIDADES"] = 0.0
        all_combos["PCT_STOCK"] = 0.0
        all_combos["LABEL"] = "0 SKUs\n$0\n0 und"
        all_combos["COLOR"] = all_combos.apply(
            lambda r: _health_color(r["TIER_MOI"], r["TIER_ANTIGUEDAD"]), axis=1
        )
        return all_combos

    agg_dict = {
        "N_SKUS": ("SKU_PRODUCTO", "nunique"),
        "STOCK_COSTO": ("STOCK_COSTO", "sum"),
    }
    if "STOCK_UNIDADES" in df_valid.columns:
        agg_dict["STOCK_UNIDADES"] = ("STOCK_UNIDADES", "sum")

    matrix = df_valid.groupby(["TIER_MOI", "TIER_ANTIGUEDAD"], as_index=False).agg(**agg_dict)

    if "STOCK_UNIDADES" not in matrix.columns:
        matrix["STOCK_UNIDADES"] = 0

    total_stock = df_valid["STOCK_COSTO"].sum()
    total_skus = df_valid["SKU_PRODUCTO"].nunique()
    matrix["PCT_STOCK"] = np.where(total_stock > 0, matrix["STOCK_COSTO"] / total_stock * 100, 0)

    matrix = all_combos.merge(matrix, on=["TIER_MOI", "TIER_ANTIGUEDAD"], how="left").fillna(0)

    def _make_label(r):
        skus = int(r["N_SKUS"])
        und = int(r["STOCK_UNIDADES"])
        stock_str = _fmt_cl(r["STOCK_COSTO"])
        und_str = _fmt_und(und)
        return f"{skus} SKUs\n{stock_str}\n{und_str} und"

    matrix["LABEL"] = matrix.apply(_make_label, axis=1)
    matrix["COLOR"] = matrix.apply(
        lambda r: _health_color(r["TIER_MOI"], r["TIER_ANTIGUEDAD"]), axis=1
    )
    return matrix


def _download_data(conn):
    """Download fresh data from Snowflake and save to CSV files."""
    progress_bar = st.progress(0, text="Iniciando proceso...")

    # 1. METRICS
    progress_bar.progress(10, text="1/3 Descargando Metricas (MOI/Antiguedad)...")
    df_metrics = pd.read_sql(QUERY_STOCK_CRITICO_METRICS, conn)
    df_metrics.columns = [c.upper() for c in df_metrics.columns]
    df_metrics.to_csv(AUTO_METRICS_FILE, index=False)

    # 2. DETAIL (chunked)
    progress_bar.progress(40, text="2/3 Descargando Detalle (Puede tardar)...")
    first_chunk = True
    for chunk in pd.read_sql(QUERY_STOCK_CRITICO_DETAIL, conn, chunksize=CHUNK_SIZE):
        chunk.columns = [c.upper() for c in chunk.columns]
        mode = "w" if first_chunk else "a"
        chunk.to_csv(AUTO_DETAIL_FILE, mode=mode, header=first_chunk, index=False)
        first_chunk = False
        del chunk

    # 3. SALES
    progress_bar.progress(70, text="3/3 Descargando Ventas...")
    df_sales = pd.read_sql(QUERY_STOCK_CRITICO_SALES, conn)
    df_sales.columns = [c.upper() for c in df_sales.columns]
    df_sales.to_csv(AUTO_SALES_FILE, index=False)

    progress_bar.progress(100, text="Datos Actualizados. Generando Dashboard...")
    return progress_bar


# ── Loaders auxiliares (COGS compañía + Tránsito) ───────────────────────────

@st.cache_data(ttl=1800, show_spinner=False)
def _load_cogs_company_6m(_conn_id, _conn):
    """Load company-level monthly COGS for last 6 complete months."""
    try:
        df = pd.read_sql(QUERY_COGS_COMPANY_6M, _conn)
        df.columns = [c.upper() for c in df.columns]
        if "COGS_MENSUAL" in df.columns:
            df["COGS_MENSUAL"] = pd.to_numeric(df["COGS_MENSUAL"], errors="coerce").fillna(0)
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def _load_transit_kpi(_conn_id, _conn):
    """Load transit stock KPIs (en agua vs pendiente zarpe) with product dims."""
    try:
        df = pd.read_sql(QUERY_TRANSIT_STOCK_KPI, _conn)
        df.columns = [c.upper() for c in df.columns]
        for c in ["QTY_PENDIENTE", "MONTO_PENDIENTE_CLP"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        # Normalise text dimensions for filtering
        for c in ["AREA", "LINEA", "SUBLINEA", "MARCA", "MODELO", "PROVEEDOR", "SKU_PRODUCTO"]:
            if c in df.columns:
                df[c] = df[c].fillna("").astype(str).str.strip()
        return df
    except Exception:
        return pd.DataFrame()


def _find_max_date_in_detail():
    """Find the max date in the detail CSV by scanning chunks."""
    target_date = None
    if os.path.exists(AUTO_DETAIL_FILE):
        for chunk in pd.read_csv(AUTO_DETAIL_FILE, chunksize=CHUNK_SIZE, usecols=["FECHA"]):
            chunk["FECHA"] = pd.to_datetime(chunk["FECHA"])
            chunk_max = chunk["FECHA"].max()
            if target_date is None or chunk_max > target_date:
                target_date = chunk_max
    return target_date


def _process_detail_chunks(crit_skus, target_date):
    """Process the detail CSV in chunks extracting critical stock and store totals."""
    store_totals = {}
    crit_detail_rows = []

    if not os.path.exists(AUTO_DETAIL_FILE):
        return store_totals, crit_detail_rows

    for chunk in pd.read_csv(AUTO_DETAIL_FILE, chunksize=CHUNK_SIZE):
        if "FECHA" not in chunk.columns:
            continue
        chunk["FECHA"] = pd.to_datetime(chunk["FECHA"])
        chunk_snap = chunk[chunk["FECHA"] == target_date]
        if chunk_snap.empty:
            continue

        for r in chunk_snap.itertuples():
            suc = r.DESCRIPCION_SUCURSAL
            costo = r.STOCK_COSTO if not pd.isna(r.STOCK_COSTO) else 0
            store_totals[suc] = store_totals.get(suc, 0) + costo

            if r.SKU_PRODUCTO in crit_skus:
                canal_det = clasificar_canal(r.CANAL_DE_DISTRIBUCION, r.DESCRIPCION_SUCURSAL)
                # Campos dt_tienda — defensivos: pueden no existir en CSV anteriores
                supervisor = getattr(r, "SUPERVISOR", "Sin Supervisor") or "Sin Supervisor"
                cluster    = getattr(r, "CLUSTER",    "Sin Cluster")    or "Sin Cluster"
                id_suc     = getattr(r, "ID_SUCURSAL", "")              or ""
                unidades   = getattr(r, "STOCK_UNIDADES", 0)
                unidades   = unidades if not pd.isna(unidades) else 0
                crit_detail_rows.append({
                    "SKU_PRODUCTO": r.SKU_PRODUCTO,
                    "ID_SUCURSAL": id_suc,
                    "DESCRIPCION_SUCURSAL": suc,
                    "CANAL_DETALLE": canal_det,
                    "SUPERVISOR": supervisor,
                    "CLUSTER": cluster,
                    "STOCK_COSTO": costo,
                    "STOCK_UNIDADES": unidades,
                })

    return store_totals, crit_detail_rows


def render_stock_dashboard(conn):
    st.html("<h2 class='sub-header'>Dashboard Stock Critico</h2>")
    st.info("Descarga los datos actualizados y visualiza el estado del stock critico.")

    with st.form("dashboard_gen_form"):
        st.markdown("### 1. Actualizar Datos desde Snowflake")
        st.markdown(
            "Presiona para descargar las ultimas versiones de:\n"
            "- **Metricas**: MOI, Antiguedad.\n"
            "- **Detalle**: Stock por sucursal.\n"
            "- **Ventas**: Historico diario."
        )
        submitted = st.form_submit_button("Actualizar Datos y Ver Dashboard")

    if submitted:
        st.session_state["stock_dashboard_active"] = True

    if not st.session_state.get("stock_dashboard_active", False):
        return

    try:
        # Download if submitted
        progress_bar = None
        if submitted:
            progress_bar = _download_data(conn)

        # Load metrics
        if not os.path.exists(AUTO_METRICS_FILE):
            st.warning("No se encontraron datos locales. Por favor presione 'Actualizar Datos'.")
            st.stop()

        df_m = pd.read_csv(AUTO_METRICS_FILE)
        df_m.columns = [c.upper() for c in df_m.columns]

        if "SKU_PRODUCTO" not in df_m.columns or df_m.empty:
            st.error("Error critico: Datos vacios o columna SKU_PRODUCTO no encontrada.")
            st.stop()

        df_m["FECHA"] = pd.to_datetime(df_m["FECHA"])
        df_m = apply_pm_filter(df_m)
        latest_date_metrics = df_m["FECHA"].max()

        # Identify critical SKUs
        df_last_m = df_m[df_m["FECHA"] == latest_date_metrics].copy()
        cond_moi_crit = (df_last_m["MOI"] >= 12) | (df_last_m["MOI"].isna())
        cond_ant_crit = _cond_ant_critico(df_last_m["ANTIGUEDAD_MESES"], df_last_m["MOI"])
        cond_crit = cond_moi_crit & cond_ant_crit
        crit_skus = set(df_last_m[cond_crit]["SKU_PRODUCTO"].unique())

        sku_info = (
            df_last_m[["SKU_PRODUCTO", "NOM_PRODUCTO", "LINEA", "MARCA"]]
            .drop_duplicates("SKU_PRODUCTO")
            .set_index("SKU_PRODUCTO")
        )

        # Process detail
        target_date = _find_max_date_in_detail()
        if target_date is None:
            target_date = latest_date_metrics

        if progress_bar:
            progress_bar.progress(50, text="Procesando Detalle para Graficos...")

        store_totals, crit_detail_rows = _process_detail_chunks(crit_skus, target_date)

        if progress_bar:
            progress_bar.progress(90, text="Generando Visualizaciones...")

        # --- DASHBOARD ---
        st.markdown("---")
        st.subheader(f"Analisis de Stock Critico (al {target_date.date()})")

        # ── Filtros Globales ─────────────────────────────────────────────
        with st.expander("Filtros", expanded=False):
            # Row 1: Jerarquía comercial cascading
            _gf1, _gf2, _gf3, _gf4 = st.columns(4)
            _g_areas = sorted(df_last_m["AREA"].dropna().unique().tolist()) if "AREA" in df_last_m.columns else []
            _g_sel_areas = _gf1.multiselect("Area", _g_areas, key="sc_g_area")

            _g_lineas = sorted(df_last_m["LINEA"].dropna().unique().tolist()) if "LINEA" in df_last_m.columns else []
            if _g_sel_areas and "AREA" in df_last_m.columns:
                _g_lineas = sorted(
                    df_last_m[df_last_m["AREA"].isin(_g_sel_areas)]["LINEA"].dropna().unique().tolist()
                )
            _g_sel_lineas = _gf2.multiselect("Linea", _g_lineas, key="sc_g_linea")

            _g_sublineas = sorted(df_last_m["SUBLINEA"].dropna().unique().tolist()) if "SUBLINEA" in df_last_m.columns else []
            if _g_sel_lineas and "LINEA" in df_last_m.columns:
                _g_sublineas = sorted(
                    df_last_m[df_last_m["LINEA"].isin(_g_sel_lineas)]["SUBLINEA"].dropna().unique().tolist()
                )
            elif _g_sel_areas and "AREA" in df_last_m.columns:
                _g_sublineas = sorted(
                    df_last_m[df_last_m["AREA"].isin(_g_sel_areas)]["SUBLINEA"].dropna().unique().tolist()
                )
            _g_sel_sublineas = _gf3.multiselect("Sublinea", _g_sublineas, key="sc_g_sublinea")

            _g_marcas = sorted(df_last_m["MARCA"].dropna().unique().tolist()) if "MARCA" in df_last_m.columns else []
            if _g_sel_sublineas and "SUBLINEA" in df_last_m.columns:
                _g_marcas = sorted(
                    df_last_m[df_last_m["SUBLINEA"].isin(_g_sel_sublineas)]["MARCA"].dropna().unique().tolist()
                )
            elif _g_sel_lineas and "LINEA" in df_last_m.columns:
                _g_marcas = sorted(
                    df_last_m[df_last_m["LINEA"].isin(_g_sel_lineas)]["MARCA"].dropna().unique().tolist()
                )
            _g_sel_marcas = _gf4.multiselect("Marca", _g_marcas, key="sc_g_marca")

            # Row 2: Modelo, Proveedor, SKU, Nombre Producto
            _gf5, _gf6, _gf7, _gf8 = st.columns(4)

            # Modelo — cascaded from Marca > Sublinea > Linea > Area
            _g_modelos = sorted(df_last_m["MODELO"].dropna().unique().tolist()) if "MODELO" in df_last_m.columns else []
            if _g_sel_marcas and "MARCA" in df_last_m.columns:
                _g_modelos = sorted(
                    df_last_m[df_last_m["MARCA"].isin(_g_sel_marcas)]["MODELO"].dropna().unique().tolist()
                )
            elif _g_sel_sublineas and "SUBLINEA" in df_last_m.columns:
                _g_modelos = sorted(
                    df_last_m[df_last_m["SUBLINEA"].isin(_g_sel_sublineas)]["MODELO"].dropna().unique().tolist()
                )
            _g_sel_modelos = _gf5.multiselect("Modelo", _g_modelos, key="sc_g_modelo")

            # Proveedor
            _g_proveedores = sorted(df_last_m["PROVEEDOR"].dropna().unique().tolist()) if "PROVEEDOR" in df_last_m.columns else []
            if _g_sel_areas and "AREA" in df_last_m.columns:
                _tmp = df_last_m[df_last_m["AREA"].isin(_g_sel_areas)]
                _g_proveedores = sorted(_tmp["PROVEEDOR"].dropna().unique().tolist()) if "PROVEEDOR" in _tmp.columns else []
            _g_sel_proveedores = _gf6.multiselect("Proveedor", _g_proveedores, key="sc_g_proveedor")

            # SKU — text input for flexibility (comma/space separated)
            from utils.filters import limpiar_lista
            _g_sku_text = _gf7.text_input("SKU (separados por coma/espacio)", key="sc_g_sku")
            _g_sel_skus = limpiar_lista(_g_sku_text)

            # Nombre producto — contains search
            _g_nom_prod = _gf8.text_input("Nombre Producto (contiene)", key="sc_g_nom_prod")

        # Apply global filters
        if _g_sel_areas and "AREA" in df_last_m.columns:
            df_last_m = df_last_m[df_last_m["AREA"].isin(_g_sel_areas)].copy()
        if _g_sel_lineas and "LINEA" in df_last_m.columns:
            df_last_m = df_last_m[df_last_m["LINEA"].isin(_g_sel_lineas)].copy()
        if _g_sel_sublineas and "SUBLINEA" in df_last_m.columns:
            df_last_m = df_last_m[df_last_m["SUBLINEA"].isin(_g_sel_sublineas)].copy()
        if _g_sel_marcas and "MARCA" in df_last_m.columns:
            df_last_m = df_last_m[df_last_m["MARCA"].isin(_g_sel_marcas)].copy()
        if _g_sel_modelos and "MODELO" in df_last_m.columns:
            df_last_m = df_last_m[df_last_m["MODELO"].isin(_g_sel_modelos)].copy()
        if _g_sel_proveedores and "PROVEEDOR" in df_last_m.columns:
            df_last_m = df_last_m[df_last_m["PROVEEDOR"].isin(_g_sel_proveedores)].copy()
        if _g_sel_skus and "SKU_PRODUCTO" in df_last_m.columns:
            df_last_m = df_last_m[df_last_m["SKU_PRODUCTO"].isin(_g_sel_skus)].copy()
        if _g_nom_prod and "NOM_PRODUCTO" in df_last_m.columns:
            df_last_m = df_last_m[
                df_last_m["NOM_PRODUCTO"].astype(str).str.upper().str.contains(_g_nom_prod.upper(), na=False)
            ].copy()

        # Apply same filters to df_m (historical time series) so all charts respond
        if _g_sel_areas and "AREA" in df_m.columns:
            df_m = df_m[df_m["AREA"].isin(_g_sel_areas)]
        if _g_sel_lineas and "LINEA" in df_m.columns:
            df_m = df_m[df_m["LINEA"].isin(_g_sel_lineas)]
        if _g_sel_sublineas and "SUBLINEA" in df_m.columns:
            df_m = df_m[df_m["SUBLINEA"].isin(_g_sel_sublineas)]
        if _g_sel_marcas and "MARCA" in df_m.columns:
            df_m = df_m[df_m["MARCA"].isin(_g_sel_marcas)]
        if _g_sel_modelos and "MODELO" in df_m.columns:
            df_m = df_m[df_m["MODELO"].isin(_g_sel_modelos)]
        if _g_sel_proveedores and "PROVEEDOR" in df_m.columns:
            df_m = df_m[df_m["PROVEEDOR"].isin(_g_sel_proveedores)]
        if _g_sel_skus and "SKU_PRODUCTO" in df_m.columns:
            df_m = df_m[df_m["SKU_PRODUCTO"].isin(_g_sel_skus)]
        if _g_nom_prod and "NOM_PRODUCTO" in df_m.columns:
            df_m = df_m[
                df_m["NOM_PRODUCTO"].astype(str).str.upper().str.contains(_g_nom_prod.upper(), na=False)
            ]

        # Recompute critical SKUs after filtering
        cond_moi_crit = (df_last_m["MOI"] >= 12) | (df_last_m["MOI"].isna())
        cond_ant_crit = _cond_ant_critico(df_last_m["ANTIGUEDAD_MESES"], df_last_m["MOI"])
        cond_crit = cond_moi_crit & cond_ant_crit
        crit_skus = set(df_last_m[cond_crit]["SKU_PRODUCTO"].unique())

        _g_any_filter = any([
            _g_sel_areas, _g_sel_lineas, _g_sel_sublineas, _g_sel_marcas,
            _g_sel_modelos, _g_sel_proveedores, _g_sel_skus, _g_nom_prod,
        ])
        if _g_any_filter:
            _filter_parts = []
            if _g_sel_areas:
                _filter_parts.append(f"Area: {', '.join(_g_sel_areas)}")
            if _g_sel_lineas:
                _filter_parts.append(f"Linea: {', '.join(_g_sel_lineas)}")
            if _g_sel_sublineas:
                _filter_parts.append(f"Sublinea: {', '.join(_g_sel_sublineas)}")
            if _g_sel_marcas:
                _filter_parts.append(f"Marca: {', '.join(_g_sel_marcas)}")
            if _g_sel_modelos:
                _filter_parts.append(f"Modelo: {', '.join(_g_sel_modelos)}")
            if _g_sel_proveedores:
                _filter_parts.append(f"Proveedor: {', '.join(_g_sel_proveedores)}")
            if _g_sel_skus:
                _filter_parts.append(f"SKU: {', '.join(_g_sel_skus[:5])}{'...' if len(_g_sel_skus) > 5 else ''}")
            if _g_nom_prod:
                _filter_parts.append(f"Nombre: {_g_nom_prod}")
            st.caption(f"Filtro activo: {' | '.join(_filter_parts)}")

        # KPIs
        total_stock_val = df_last_m["STOCK_COSTO"].sum()
        crit_stock_val = df_last_m[cond_crit]["STOCK_COSTO"].sum()
        df_crit_det = pd.DataFrame(crit_detail_rows)
        # Filter detail data by global filtered SKUs
        if _g_any_filter and not df_crit_det.empty and "SKU_PRODUCTO" in df_crit_det.columns:
            _filtered_skus = set(df_last_m["SKU_PRODUCTO"].unique())
            df_crit_det = df_crit_det[df_crit_det["SKU_PRODUCTO"].isin(_filtered_skus)].copy()
        pct_crit = (crit_stock_val / total_stock_val * 100) if total_stock_val > 0 else 0

        # Load sales data for analytics (Charts 13-14)
        df_sales_crit = pd.DataFrame()
        if os.path.exists(AUTO_SALES_FILE):
            try:
                _df_sales_raw = pd.read_csv(AUTO_SALES_FILE)
                _df_sales_raw.columns = [c.upper() for c in _df_sales_raw.columns]
                _df_sales_raw["FECHA"] = pd.to_datetime(_df_sales_raw["FECHA"])
                for _nc in ["UNIDADES_VENDIDAS", "NETO_TOTAL", "APORTE_TOTAL", "MARGEN"]:
                    if _nc in _df_sales_raw.columns:
                        _df_sales_raw[_nc] = pd.to_numeric(_df_sales_raw[_nc], errors="coerce").fillna(0)
                df_sales_crit = _df_sales_raw[
                    _df_sales_raw["SKU_PRODUCTO"].isin(crit_skus)
                ].copy()
                del _df_sales_raw
            except Exception:
                df_sales_crit = pd.DataFrame()

        figures_to_export = {}

        # --- Row 1: KPIs principales (HTML cards con formato chileno) ---
        _total_stock_und = df_last_m["STOCK_UNIDADES"].sum() if "STOCK_UNIDADES" in df_last_m.columns else 0
        _n_crit_skus = df_last_m[cond_crit]["SKU_PRODUCTO"].nunique() if "SKU_PRODUCTO" in df_last_m.columns else 0
        _pct_color = (
            COLORS["status_critical"] if pct_crit > 15
            else COLORS["status_at_risk"] if pct_crit > 5
            else COLORS["status_on_track"]
        )
        _top_kpis = [
            ("Stock Total", _fmt_cl(total_stock_val), COLORS["primary"],
             f"{_fmt_und(_total_stock_und)} unidades"),
            ("Stock Critico", _fmt_cl(crit_stock_val), COLORS["status_critical"],
             f"{_fmt_und(_n_crit_skus)} SKUs criticos"),
            ("% Critico Global", f"{pct_crit:.1f}%", _pct_color,
             f"{_fmt_cl(crit_stock_val)} de {_fmt_cl(total_stock_val)}"),
        ]
        _kc1, _kc2, _kc3 = st.columns(3)
        for _kc_col, (_kc_lab, _kc_val, _kc_clr, _kc_sub) in zip(
            [_kc1, _kc2, _kc3], _top_kpis
        ):
            with _kc_col:
                st.html(f"""
                <div style='background:#fff;padding:1rem;border-radius:10px;text-align:center;
                             border-top:4px solid {_kc_clr};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                    <div style='font-size:1.5rem;font-weight:bold;color:{_kc_clr}'>{_kc_val}</div>
                    <div style='font-size:0.75rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{_kc_lab}</div>
                    <div style='font-size:0.6rem;color:{COLORS["medium_gray"]};margin-top:4px'>{_kc_sub}</div>
                </div>""")

        # ── MOI Compania + Transito KPIs ──────────────────────────────────────
        _conn_id = id(conn)
        df_cogs_6m = _load_cogs_company_6m(_conn_id, conn)
        df_transit = _load_transit_kpi(_conn_id, conn)

        # --- MOI Historico (6m) ---
        avg_monthly_cogs_hist = 0.0
        n_months_hist = 0
        if _g_any_filter:
            # Filtro activo: usar COSTO_PROM_90_CIA de los SKUs filtrados
            # COSTO_PROM_90_CIA = promedio DIARIO de COGS ultimos 90 dias por SKU
            # Se multiplica por 30.44 para convertir a COGS mensual
            if "COSTO_PROM_90_CIA" in df_last_m.columns:
                _daily_cogs_sum = pd.to_numeric(
                    df_last_m["COSTO_PROM_90_CIA"], errors="coerce"
                ).fillna(0).sum()
                avg_monthly_cogs_hist = _daily_cogs_sum * 30.44
                n_months_hist = 1  # ya convertido a mensual
            _moi_hist_source = "COGS SKU filtrado"
        else:
            if not df_cogs_6m.empty and "COGS_MENSUAL" in df_cogs_6m.columns:
                avg_monthly_cogs_hist = df_cogs_6m["COGS_MENSUAL"].mean()
                n_months_hist = len(df_cogs_6m)
            _moi_hist_source = "COGS CIA"
        moi_historico = (
            total_stock_val / avg_monthly_cogs_hist
            if avg_monthly_cogs_hist > 0
            else 0.0
        )

        # --- MOI Forecast (6m) ---
        avg_monthly_cogs_fc = 0.0
        n_months_fc = 0
        df_proy = st.session_state.get("df_proy")
        if df_proy is not None and not df_proy.empty and "COGS_RES_TOTAL" in df_proy.columns:
            _df_fc = df_proy.copy()
            if "TIPO_DATO" in _df_fc.columns:
                _df_fc = _df_fc[_df_fc["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])]
            # Apply global filters to forecast COGS.
            # Use filtered SKU list from df_last_m for dimensions that may have
            # NaN in df_proy (e.g. PROVEEDOR), ensuring consistent filtering.
            if _g_any_filter and "SKU_PRODUCTO" in df_last_m.columns:
                _filtered_skus = set(df_last_m["SKU_PRODUCTO"].unique())
                _df_fc = _df_fc[_df_fc["SKU_PRODUCTO"].isin(_filtered_skus)]
            if "PERIODO" in _df_fc.columns:
                _df_fc["PERIODO"] = pd.to_datetime(_df_fc["PERIODO"], errors="coerce")
                _df_fc["COGS_RES_TOTAL"] = pd.to_numeric(
                    _df_fc["COGS_RES_TOTAL"], errors="coerce"
                ).fillna(0)
                _cogs_monthly = (
                    _df_fc.groupby(_df_fc["PERIODO"].dt.to_period("M"))["COGS_RES_TOTAL"]
                    .sum()
                    .sort_index()
                    .head(6)
                )
                if len(_cogs_monthly) > 0:
                    avg_monthly_cogs_fc = _cogs_monthly.mean()
                    n_months_fc = len(_cogs_monthly)
        moi_forecast = (
            total_stock_val / avg_monthly_cogs_fc
            if avg_monthly_cogs_fc > 0
            else 0.0
        )

        # --- Transit KPIs (apply same dimensional filters) ---
        _df_tr = df_transit.copy() if not df_transit.empty else pd.DataFrame()
        if not _df_tr.empty:
            if _g_sel_areas and "AREA" in _df_tr.columns:
                _df_tr = _df_tr[_df_tr["AREA"].isin(_g_sel_areas)]
            if _g_sel_lineas and "LINEA" in _df_tr.columns:
                _df_tr = _df_tr[_df_tr["LINEA"].isin(_g_sel_lineas)]
            if _g_sel_sublineas and "SUBLINEA" in _df_tr.columns:
                _df_tr = _df_tr[_df_tr["SUBLINEA"].isin(_g_sel_sublineas)]
            if _g_sel_marcas and "MARCA" in _df_tr.columns:
                _df_tr = _df_tr[_df_tr["MARCA"].isin(_g_sel_marcas)]
            if _g_sel_modelos and "MODELO" in _df_tr.columns:
                _df_tr = _df_tr[_df_tr["MODELO"].isin(_g_sel_modelos)]
            if _g_sel_proveedores and "PROVEEDOR" in _df_tr.columns:
                _df_tr = _df_tr[_df_tr["PROVEEDOR"].isin(_g_sel_proveedores)]
            if _g_sel_skus and "SKU_PRODUCTO" in _df_tr.columns:
                _df_tr = _df_tr[_df_tr["SKU_PRODUCTO"].isin(_g_sel_skus)]

        transit_en_agua_und = 0.0
        transit_en_agua_clp = 0.0
        transit_pend_zarpe_und = 0.0
        transit_pend_zarpe_clp = 0.0
        if not _df_tr.empty and "STATUS_TRANSITO" in _df_tr.columns:
            _agua = _df_tr[_df_tr["STATUS_TRANSITO"] == "EN_AGUA"]
            if not _agua.empty:
                transit_en_agua_und = float(_agua["QTY_PENDIENTE"].sum())
                transit_en_agua_clp = float(_agua["MONTO_PENDIENTE_CLP"].sum())
            _zarpe = _df_tr[_df_tr["STATUS_TRANSITO"] == "PENDIENTE_ZARPE"]
            if not _zarpe.empty:
                transit_pend_zarpe_und = float(_zarpe["QTY_PENDIENTE"].sum())
                transit_pend_zarpe_clp = float(_zarpe["MONTO_PENDIENTE_CLP"].sum())
        transit_total_und = transit_en_agua_und + transit_pend_zarpe_und
        transit_total_clp = transit_en_agua_clp + transit_pend_zarpe_clp

        # --- MOI Budget ---
        if _g_any_filter:
            # Budget es solo a nivel canal, no aplica con filtros dimensionales
            avg_monthly_cogs_bdg = 0.0
        else:
            avg_monthly_cogs_bdg = get_budget_cogs_monthly("TOTAL")
        moi_budget = (
            total_stock_val / avg_monthly_cogs_bdg
            if avg_monthly_cogs_bdg > 0
            else 0.0
        )

        # --- Render MOI Compania (3 cards) ---
        st.html("<br>")
        st.markdown("##### MOI Compania")
        _moi_c1, _moi_c2, _moi_c3 = st.columns(3)

        def _moi_color(val):
            if val <= 0:
                return COLORS["medium_gray"]
            return (
                COLORS["status_on_track"] if val < 6
                else COLORS["status_at_risk"] if val < 12
                else COLORS["status_critical"]
            )

        # Card 1: MOI Historico
        _moi_hist_color = _moi_color(moi_historico)
        if _g_any_filter:
            _moi_hist_label = "MOI Historico (filtrado)"
        else:
            _moi_hist_label = f"MOI Historico ({n_months_hist}m)" if n_months_hist > 0 else "MOI Historico"
        with _moi_c1:
            st.html(f"""
            <div style='background:#fff;padding:1rem;border-radius:10px;text-align:center;
                         border-top:4px solid {_moi_hist_color};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                <div style='font-size:1.5rem;font-weight:bold;color:{_moi_hist_color}'>{moi_historico:.1f} meses</div>
                <div style='font-size:0.75rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{_moi_hist_label}</div>
                <div style='font-size:0.6rem;color:{COLORS["medium_gray"]};margin-top:4px'>
                    Stock: {_fmt_cl(total_stock_val)} &divide; COGS prom: {_fmt_cl(avg_monthly_cogs_hist)}/mes
                </div>
            </div>""")

        # Card 2: MOI Forecast
        if n_months_fc == 0:
            _moi_fc_val = "N/D"
            _moi_fc_sub = "Ejecute Proyeccion primero"
            _moi_fc_color = COLORS["medium_gray"]
        else:
            _moi_fc_val = f"{moi_forecast:.1f} meses"
            _moi_fc_sub = f"Stock: {_fmt_cl(total_stock_val)} &divide; FC COGS prom: {_fmt_cl(avg_monthly_cogs_fc)}/mes"
            _moi_fc_color = _moi_color(moi_forecast)
        _moi_fc_label = f"MOI Forecast ({n_months_fc}m)" if n_months_fc > 0 else "MOI Forecast"
        with _moi_c2:
            st.html(f"""
            <div style='background:#fff;padding:1rem;border-radius:10px;text-align:center;
                         border-top:4px solid {_moi_fc_color};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                <div style='font-size:1.5rem;font-weight:bold;color:{_moi_fc_color}'>{_moi_fc_val}</div>
                <div style='font-size:0.75rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{_moi_fc_label}</div>
                <div style='font-size:0.6rem;color:{COLORS["medium_gray"]};margin-top:4px'>
                    {_moi_fc_sub}
                </div>
            </div>""")

        # Card 3: MOI Budget
        if _g_any_filter:
            _moi_bdg_val = "No aplica"
            _moi_bdg_sub = "Budget solo disponible a nivel compania"
            _moi_bdg_color = COLORS["medium_gray"]
        elif avg_monthly_cogs_bdg <= 0:
            _moi_bdg_val = "N/D"
            _moi_bdg_sub = "Sin archivo de budget"
            _moi_bdg_color = COLORS["medium_gray"]
        else:
            _moi_bdg_val = f"{moi_budget:.1f} meses"
            _moi_bdg_sub = f"Stock: {_fmt_cl(total_stock_val)} &divide; Budget COGS: {_fmt_cl(avg_monthly_cogs_bdg)}/mes"
            _moi_bdg_color = _moi_color(moi_budget)
        with _moi_c3:
            st.html(f"""
            <div style='background:#fff;padding:1rem;border-radius:10px;text-align:center;
                         border-top:4px solid {_moi_bdg_color};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                <div style='font-size:1.5rem;font-weight:bold;color:{_moi_bdg_color}'>{_moi_bdg_val}</div>
                <div style='font-size:0.75rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>MOI Budget 2026</div>
                <div style='font-size:0.6rem;color:{COLORS["medium_gray"]};margin-top:4px'>
                    {_moi_bdg_sub}
                </div>
            </div>""")

        # --- Render Transito ---
        st.html("<br>")
        st.markdown("##### Stock en Transito")
        _tr_c1, _tr_c2, _tr_c3 = st.columns(3)

        _transit_data = [
            ("En Agua", transit_en_agua_clp, transit_en_agua_und,
             COLORS["tertiary_blue"], "ETD &le; hoy &lt; ETA"),
            ("Pendiente Zarpe", transit_pend_zarpe_clp, transit_pend_zarpe_und,
             COLORS["secondary"], "ETD &gt; hoy"),
            ("Total Transito", transit_total_clp, transit_total_und,
             COLORS["primary"], "Total pendiente de recepcion"),
        ]
        for _tr_col, (_tr_lab, _tr_clp, _tr_und, _tr_clr, _tr_desc) in zip(
            [_tr_c1, _tr_c2, _tr_c3], _transit_data
        ):
            with _tr_col:
                st.html(f"""
                <div style='background:#fff;padding:1rem;border-radius:10px;text-align:center;
                             border-top:4px solid {_tr_clr};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                    <div style='font-size:1.4rem;font-weight:bold;color:{_tr_clr}'>{_fmt_cl(_tr_clp)}</div>
                    <div style='font-size:0.75rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{_tr_lab}</div>
                    <div style='font-size:0.6rem;color:{COLORS["medium_gray"]};margin-top:4px'>
                        {_fmt_und(_tr_und)} unidades | {_tr_desc}
                    </div>
                </div>""")

        st.html("<br>")

        # ---- CHART 1: Evolution % Critical Stock ----
        st.markdown("### 1. Evolucion % Stock Critico sobre Stock Total (Con Hitos)")
        df_daily_totals = df_m.groupby("FECHA")["STOCK_COSTO"].sum().reset_index()
        df_daily_totals.columns = ["FECHA", "STOCK_TOTAL"]

        cond_moi_all = (df_m["MOI"] >= 12) | (df_m["MOI"].isna())
        cond_ant_all = _cond_ant_critico(df_m["ANTIGUEDAD_MESES"], df_m["MOI"])
        df_m["IS_CRITICO"] = cond_moi_all & cond_ant_all

        evolucion_critico = (
            df_m[df_m["IS_CRITICO"]]
            .groupby("FECHA")
            .agg(STOCK_CRITICO=("STOCK_COSTO", "sum"))
            .reset_index()
        )
        evolucion = pd.merge(evolucion_critico, df_daily_totals, on="FECHA", how="left")
        evolucion["PCT_CRITICO"] = evolucion["STOCK_CRITICO"] / evolucion["STOCK_TOTAL"]

        if not evolucion.empty:
            evolucion = evolucion.sort_values("FECHA")
            idx_start = evolucion.index[0]
            idx_end = evolucion.index[-1]
            idx_max_pct = evolucion["PCT_CRITICO"].idxmax()
            idx_min_pct = evolucion["PCT_CRITICO"].idxmin()
            evolucion["DELTA_PCT"] = evolucion["PCT_CRITICO"].diff()
            idx_max_drop = evolucion["DELTA_PCT"].idxmin()

            key_idxs = list(dict.fromkeys([idx_start, idx_max_pct, idx_max_drop, idx_min_pct, idx_end]))

            fig1 = go.Figure(layout=dorel_layout(
                title=dict(text="% de Stock Critico sobre Stock Total (con Hitos)", font_size=15, x=0.5),
                yaxis=dict(tickformat=".1%", title="% Stock Critico", gridcolor="#ECECEC"),
                xaxis=dict(title="Fecha", gridcolor="#ECECEC"),
                height=480,
            ))
            # Area fill under the curve
            fig1.add_trace(go.Scatter(
                x=evolucion["FECHA"], y=evolucion["PCT_CRITICO"],
                mode="lines",
                line=dict(color=COLORS["primary"], width=3, shape="spline"),
                fill="tozeroy",
                fillcolor="rgba(6, 94, 139, 0.08)",
                hovertemplate="%{x|%Y-%m-%d}<br>% Critico: %{y:.1%}<extra></extra>",
                name="% Critico",
                showlegend=False,
            ))
            # Milestone markers
            for i in key_idxs:
                if i not in evolucion.index:
                    continue
                row = evolucion.loc[i]
                x_val, y_val = row["FECHA"], row["PCT_CRITICO"]
                if pd.isna(x_val) or pd.isna(y_val):
                    continue
                ann_text = (
                    f"{row['FECHA'].date()}<br>"
                    f"{row['PCT_CRITICO']*100:.1f}%<br>"
                    f"Critico: ${human_format(row['STOCK_CRITICO'])}<br>"
                    f"Total: ${human_format(row['STOCK_TOTAL'])}"
                )
                fig1.add_trace(go.Scatter(
                    x=[x_val], y=[y_val],
                    mode="markers",
                    marker=dict(size=10, color=COLORS["status_critical"], line=dict(width=2, color="white")),
                    hovertemplate=ann_text + "<extra></extra>",
                    showlegend=False,
                ))
                fig1.add_annotation(
                    x=x_val, y=y_val,
                    text=f"<b>{row['FECHA'].date()}</b><br>{row['PCT_CRITICO']*100:.1f}%",
                    showarrow=True,
                    arrowhead=2,
                    arrowcolor="#999",
                    ax=0, ay=-40,
                    bgcolor="white",
                    bordercolor="#ccc",
                    borderwidth=1,
                    borderpad=4,
                    font=dict(size=10),
                )

            st.plotly_chart(fig1, use_container_width=True)
            figures_to_export["Evolucion Critico"] = fig1
        else:
            st.info("No hay datos suficientes para graficar evolucion.")

        # ---- CHART 2: Traffic Light (Semaforo) ----
        st.markdown("### 2. Semaforo de Stock con Baja Rotacion (MOI >= 12 o NaN)")
        semaforo_df = df_last_m[(df_last_m["MOI"] >= 12) | (df_last_m["MOI"].isna())].copy()

        def get_granular_bucket(m):
            if pd.isna(m):
                return "Sin Info"
            if m < 3:
                return "< 3m"
            if m < 6:
                return "3-6m"
            if m < 12:
                return "6-12m"
            if m < 24:
                return "12-24m"
            return "> 24m"

        semaforo_df["rango_antiguedad"] = semaforo_df["ANTIGUEDAD_MESES"].apply(get_granular_bucket)
        tabla_plot = semaforo_df.groupby("rango_antiguedad", as_index=False).agg(
            stock_clp=("STOCK_COSTO", "sum"), skus=("SKU_PRODUCTO", "nunique")
        )
        total_stock_sem = semaforo_df["STOCK_COSTO"].sum()
        tabla_plot["pct"] = tabla_plot["stock_clp"] / total_stock_sem

        orden_cat = ["< 3m", "3-6m", "6-12m", "12-24m", "> 24m", "Sin Info"]
        tabla_plot["rango_antiguedad"] = pd.Categorical(tabla_plot["rango_antiguedad"], categories=orden_cat, ordered=True)
        tabla_plot = tabla_plot.sort_values("rango_antiguedad")

        if not tabla_plot.empty:
            _sem_colors = {
                "< 3m": "#43A047", "3-6m": "#A5D6A7", "6-12m": "#F9A825",
                "12-24m": "#FB8C00", "> 24m": "#E53935", "Sin Info": "#9E9E9E",
            }
            _bar_c = [_sem_colors.get(r, "#ccc") for r in tabla_plot["rango_antiguedad"]]
            _text_labels = [
                f"${human_format(r.stock_clp)} ({r.pct:.1%}) | {r.skus} SKUs"
                for r in tabla_plot.itertuples()
            ]
            fig2 = go.Figure(layout=dorel_layout(
                title=dict(
                    text=f"Semaforo de Stock con Baja Rotacion (MOI>=12 o NaN)<br><sub>Total Analizado: ${human_format(total_stock_sem)}</sub>",
                    font_size=15, x=0.5,
                ),
                xaxis=dict(title="Monto en Stock (CLP)", gridcolor="#ECECEC"),
                yaxis=dict(categoryorder="array", categoryarray=list(reversed(orden_cat))),
                height=420,
            ))
            fig2.add_trace(go.Bar(
                y=tabla_plot["rango_antiguedad"].astype(str),
                x=tabla_plot["stock_clp"],
                orientation="h",
                marker=dict(color=_bar_c, line=dict(width=0)),
                text=_text_labels,
                textposition="outside",
                textfont=dict(size=11),
                hovertemplate="%{y}<br>Monto: $%{x:,.0f}<extra></extra>",
                showlegend=False,
            ))
            st.plotly_chart(fig2, use_container_width=True)
            figures_to_export["Semaforo"] = fig2

        # ── Filtro por Supervisor / Zona (aplica a Charts 3-5, 10 y Mapa) ──────
        _sc_supervisors = []
        if not df_crit_det.empty and "SUPERVISOR" in df_crit_det.columns:
            _avail_sups = sorted({
                s for s in df_crit_det["SUPERVISOR"].dropna().unique()
                if str(s) not in ("Sin Supervisor", "nan", "")
            })
            if _avail_sups:
                _sc_supervisors = st.multiselect(
                    "Filtrar por Supervisor / Zona",
                    _avail_sups,
                    key="sc_sup_filter",
                    help="Filtra los charts de tiendas por supervisor. No afecta charts de SKU.",
                )
                if _sc_supervisors:
                    df_crit_det = df_crit_det[df_crit_det["SUPERVISOR"].isin(_sc_supervisors)]

        # ---- CHART 3: Top 15 Immobilized Warehouses ----
        st.markdown("### 3. Top 15 Bodegas Inmovilizadas (Segunda/Merma)")
        if not df_crit_det.empty:
            inmov = (
                df_crit_det[df_crit_det["CANAL_DETALLE"] == "CD INMOVILIZADO"]
                .groupby("DESCRIPCION_SUCURSAL")["STOCK_COSTO"]
                .sum()
                .sort_values(ascending=True)
                .tail(15)
            )
            if not inmov.empty:
                fig3 = go.Figure(layout=dorel_layout(
                    title=dict(text="Top 15 Bodegas Inmovilizadas (Segunda/Merma)", font_size=15, x=0.5),
                    xaxis=dict(title="Monto en Stock (CLP)", gridcolor="#ECECEC"),
                    height=450,
                    margin=dict(l=200, r=30, t=60, b=40),
                ))
                fig3.add_trace(go.Bar(
                    y=inmov.index.tolist(),
                    x=inmov.values.tolist(),
                    orientation="h",
                    marker=dict(color=COLORS["status_critical"], line=dict(width=0)),
                    text=[f"${human_format(v)}" for v in inmov.values],
                    textposition="outside",
                    textfont=dict(size=11),
                    hovertemplate="%{y}<br>Monto: $%{x:,.0f}<extra></extra>",
                    showlegend=False,
                ))
                st.plotly_chart(fig3, use_container_width=True)
                figures_to_export["Top Inmovilizadas"] = fig3

        # ---- CHART 4: Top 20 Critical Stores ----
        st.markdown("### 4. Top 20 Tiendas Criticas: Impacto vs % Inventario Sano")
        if not df_crit_det.empty:
            tiendas = df_crit_det[df_crit_det["CANAL_DETALLE"] == "TIENDA"]
            if not tiendas.empty:
                top_20 = (
                    tiendas.groupby("DESCRIPCION_SUCURSAL")["STOCK_COSTO"]
                    .sum()
                    .sort_values(ascending=False)
                    .head(20)
                    .reset_index()
                )
                top_20.columns = ["DESCRIPCION_SUCURSAL", "CRITICO"]
                top_20["TOTAL"] = top_20["DESCRIPCION_SUCURSAL"].map(store_totals).fillna(0)
                top_20["PCT_CRITICO"] = (top_20["CRITICO"] / top_20["TOTAL"] * 100).fillna(0)

                _t20_sorted = top_20.sort_values("CRITICO", ascending=True)
                fig4 = go.Figure()
                # Primary x-axis: bar (stock $)
                fig4.add_trace(go.Bar(
                    y=_t20_sorted["DESCRIPCION_SUCURSAL"],
                    x=_t20_sorted["CRITICO"],
                    orientation="h",
                    name="Stock Critico ($)",
                    marker=dict(color=COLORS["status_at_risk"], line=dict(width=0)),
                    hovertemplate="%{y}<br>Stock Critico: $%{x:,.0f}<extra></extra>",
                    xaxis="x",
                ))
                # Secondary x-axis (top): line (% critico)
                fig4.add_trace(go.Scatter(
                    y=_t20_sorted["DESCRIPCION_SUCURSAL"],
                    x=_t20_sorted["PCT_CRITICO"],
                    mode="lines+markers",
                    name="% Critico",
                    line=dict(color="#57606f", width=2, dash="dot"),
                    marker=dict(size=7, color="#57606f"),
                    hovertemplate="%{y}<br>% Critico: %{x:.1f}%<extra></extra>",
                    xaxis="x2",
                ))
                fig4.update_layout(**dorel_layout(
                    title=dict(text="Top 20 Tiendas Criticas: Impacto Financiero y % Inventario Sano", font_size=15, x=0.5),
                    height=550,
                    margin=dict(l=220, r=40, t=60, b=40),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
                    xaxis=dict(title="Monto Stock Critico (CLP)", side="bottom", gridcolor="#ECECEC"),
                    xaxis2=dict(title="% Critico", side="top", overlaying="x", showgrid=False),
                ))
                st.plotly_chart(fig4, use_container_width=True)
                figures_to_export["Top Tiendas Criticas"] = fig4

        # ---- CHART 5: Heatmap Top 20 SKUs ----
        st.markdown("### 5. Ubicacion del Top 20 SKUs Criticos (% Distribucion)")
        if not df_crit_det.empty:
            top_skus = (
                df_crit_det.groupby("SKU_PRODUCTO")["STOCK_COSTO"]
                .sum()
                .sort_values(ascending=False)
                .head(20)
                .index
            )
            hm_data = df_crit_det[df_crit_det["SKU_PRODUCTO"].isin(top_skus)].copy()
            hm_data = hm_data.merge(sku_info[["NOM_PRODUCTO"]], on="SKU_PRODUCTO", how="left")
            hm_data["Producto"] = hm_data["NOM_PRODUCTO"].fillna(hm_data["SKU_PRODUCTO"])
            hm_data["Total_SKU"] = hm_data.groupby("SKU_PRODUCTO")["STOCK_COSTO"].transform("sum")
            hm_data["PCT_DIST"] = hm_data["STOCK_COSTO"] / hm_data["Total_SKU"] * 100

            hm_agg = hm_data.groupby(["Producto", "CANAL_DETALLE"])["PCT_DIST"].sum().reset_index()
            hm_pivot = hm_agg.pivot(index="Producto", columns="CANAL_DETALLE", values="PCT_DIST").fillna(0)

            if not hm_pivot.empty:
                _hm_z = hm_pivot.values
                _hm_text = [[f"{v:.0f}%" for v in row] for row in _hm_z]
                fig5 = go.Figure(layout=dorel_layout(
                    title=dict(text="Ubicacion del Top 20 SKUs Criticos (% Distribucion)", font_size=15, x=0.5),
                    xaxis=dict(title="Canal / Tipo Bodega", side="bottom"),
                    yaxis=dict(title="Producto Top 20", autorange="reversed"),
                    height=max(len(hm_pivot) * 35, 400),
                    margin=dict(l=280, r=30, t=60, b=60),
                ))
                fig5.add_trace(go.Heatmap(
                    z=_hm_z,
                    x=hm_pivot.columns.tolist(),
                    y=hm_pivot.index.tolist(),
                    colorscale="YlOrRd",
                    text=_hm_text,
                    texttemplate="%{text}",
                    textfont=dict(size=11),
                    hovertemplate="Producto: %{y}<br>Canal: %{x}<br>Distribucion: %{z:.1f}%<extra></extra>",
                    colorbar=dict(title="% Dist", thickness=15),
                    showscale=True,
                ))
                st.plotly_chart(fig5, use_container_width=True)
                figures_to_export["Heatmap SKUs"] = fig5

        # ---- CHART 6: Top 15 Real Risk ----
        st.markdown("### 6. Top 15 Productos con Riesgo Real (MOI>=12 o Sin Venta | Antiguedad>=12)")
        top_risk = (
            df_last_m[
                ((df_last_m["MOI"] >= 12) | (df_last_m["MOI"].isna())) &
                _cond_ant_critico(df_last_m["ANTIGUEDAD_MESES"], df_last_m["MOI"])
            ]
            .sort_values("STOCK_COSTO", ascending=False)
            .head(15)
            .copy()
        )
        if not top_risk.empty:
            _tr = top_risk.sort_values("STOCK_COSTO", ascending=True)
            _label_col6 = "NOM_PRODUCTO" if "NOM_PRODUCTO" in _tr.columns else "SKU_PRODUCTO"
            _y_labels6 = _tr[_label_col6].fillna(_tr["SKU_PRODUCTO"]).astype(str).str[:45].tolist()
            _bar_c6 = [COLORS["status_critical"] if a >= 24 else COLORS["status_at_risk"]
                       for a in _tr["ANTIGUEDAD_MESES"]]
            _hover6 = []
            for _, r in _tr.iterrows():
                _m = f"{r['MOI']:.1f}" if pd.notna(r["MOI"]) else "NO VENTA"
                _hover6.append(f"${human_format(r['STOCK_COSTO'])} | MOI: {_m} | Ant: {r['ANTIGUEDAD_MESES']:.1f}m")

            fig6 = go.Figure(layout=dorel_layout(
                title=dict(text="TOP 15 Productos con Riesgo Real<br><sub>MOI>=12 o Sin Venta | Antiguedad>=12</sub>", font_size=15, x=0.5),
                xaxis=dict(title="Monto en Stock (CLP)", gridcolor="#ECECEC"),
                height=500,
                margin=dict(l=300, r=30, t=70, b=40),
            ))
            fig6.add_trace(go.Bar(
                y=_y_labels6,
                x=_tr["STOCK_COSTO"].tolist(),
                orientation="h",
                marker=dict(color=_bar_c6, line=dict(width=0)),
                text=_hover6,
                textposition="outside",
                textfont=dict(size=10),
                hovertemplate="%{y}<br>%{text}<extra></extra>",
                showlegend=False,
            ))
            st.plotly_chart(fig6, use_container_width=True)
            figures_to_export["Top Riesgo Real"] = fig6

        # ---- CHART 7: "A Punto" Products ----
        st.markdown("### 7. Productos 'A Punto' (8-12 Meses)")
        future_risk = df_last_m[
            (df_last_m["MOI"] >= 8) & (df_last_m["MOI"] < 12)
            & (df_last_m["ANTIGUEDAD_MESES"] >= 8) & (df_last_m["ANTIGUEDAD_MESES"] < 12)
        ].copy()

        if not future_risk.empty:
            future_risk["dist_moi"] = 12 - future_risk["MOI"]
            future_risk["dist_ant"] = 12 - future_risk["ANTIGUEDAD_MESES"]
            future_risk["score"] = future_risk[["dist_moi", "dist_ant"]].min(axis=1)

            top_future = future_risk.sort_values("STOCK_COSTO", ascending=True).tail(15).copy()

            _label_col7 = "NOM_PRODUCTO" if "NOM_PRODUCTO" in top_future.columns else "SKU_PRODUCTO"
            _y_labels7 = top_future[_label_col7].fillna(top_future["SKU_PRODUCTO"]).astype(str).str[:45].tolist()
            _bar_c7 = [
                "#E53935" if s <= 1.0 else "#FB8C00" if s <= 2.0 else "#F9A825" if s <= 3.0 else "#43A047"
                for s in top_future["score"]
            ]
            _hover7 = [
                f"${human_format(r['STOCK_COSTO'])} | MOI:{r['MOI']:.1f} | Ant:{r['ANTIGUEDAD_MESES']:.1f}m | Dist:{r['score']:.1f}m"
                for _, r in top_future.iterrows()
            ]

            fig7 = go.Figure(layout=dorel_layout(
                title=dict(text="TOP Productos 'A Punto' (8-12 Meses)<br><sub>MOI y Ant: 8-12 meses — color = distancia a critico</sub>", font_size=15, x=0.5),
                xaxis=dict(title="Monto en Stock (CLP)", gridcolor="#ECECEC"),
                height=500,
                margin=dict(l=300, r=30, t=70, b=40),
            ))
            fig7.add_trace(go.Bar(
                y=_y_labels7,
                x=top_future["STOCK_COSTO"].tolist(),
                orientation="h",
                marker=dict(color=_bar_c7, line=dict(width=0)),
                text=_hover7,
                textposition="outside",
                textfont=dict(size=10),
                hovertemplate="%{y}<br>%{text}<extra></extra>",
                showlegend=False,
            ))
            st.plotly_chart(fig7, use_container_width=True)
            figures_to_export["Top A Punto"] = fig7

        # ---- CHART 8: Top 10 Lines by Brand ----
        st.markdown("### 8. Top 10 Lineas Criticas: Composicion por Marca")
        if not df_crit_det.empty and "MARCA" in df_m.columns:
            sku_info_ext = (
                df_m[["SKU_PRODUCTO", "MARCA", "LINEA"]]
                .drop_duplicates("SKU_PRODUCTO")
                .set_index("SKU_PRODUCTO")
            )
            df_crit_viz = df_crit_det.merge(sku_info_ext, on="SKU_PRODUCTO", how="left")
            line_brand = df_crit_viz.groupby(["LINEA", "MARCA"])["STOCK_COSTO"].sum().unstack(fill_value=0)
            top_lines = line_brand.sum(axis=1).sort_values(ascending=False).head(10).index
            line_brand_top = line_brand.loc[top_lines]

            import plotly.express as px
            _palette = px.colors.qualitative.Set2 + px.colors.qualitative.Pastel1
            fig8 = go.Figure(layout=dorel_layout(
                title=dict(text="Top 10 Lineas Criticas: Composicion por Marca", font_size=15, x=0.5),
                barmode="stack",
                yaxis=dict(title="Monto en Stock (CLP)", gridcolor="#ECECEC"),
                xaxis=dict(title="Linea", tickangle=-45),
                height=520,
                margin=dict(l=60, r=30, t=60, b=120),
            ))
            _lines_order = line_brand_top.sum(axis=1).sort_values(ascending=False).index.tolist()
            for i, marca in enumerate(line_brand_top.columns):
                _vals = line_brand_top.loc[_lines_order, marca]
                if _vals.sum() == 0:
                    continue
                fig8.add_trace(go.Bar(
                    x=_lines_order,
                    y=_vals.tolist(),
                    name=str(marca),
                    marker=dict(color=_palette[i % len(_palette)]),
                    hovertemplate="Linea: %{x}<br>Marca: " + str(marca) + "<br>Monto: $%{y:,.0f}<extra></extra>",
                ))
            st.plotly_chart(fig8, use_container_width=True)
            figures_to_export["Top Lineas"] = fig8

        # ---- CHART 9: Waterfall de Recuperación Mensual ----
        st.markdown("---")
        st.markdown("### 9. Waterfall de Recuperacion: Flujo Mensual de Stock Critico")
        st.caption(
            "Compara stock critico entre dos meses. **Nuevos**: SKUs que entraron a critico. "
            "**Recuperados**: SKUs que salieron de critico (vendidos o reclasificados). "
            "**Delta Permanentes**: cambio de valor en SKUs que permanecen criticos."
        )

        # Build monthly snapshots from df_m (which has weekly data)
        if "IS_CRITICO" in df_m.columns and not df_m.empty:
            _wf_df = df_m.copy()
            _wf_df["PERIODO"] = _wf_df["FECHA"].dt.to_period("M")
            # Use last snapshot of each month per SKU
            _wf_last = _wf_df.sort_values("FECHA").groupby(["PERIODO", "SKU_PRODUCTO"]).last().reset_index()
            _periodos = sorted(_wf_last["PERIODO"].unique())

            if len(_periodos) >= 2:
                _wfc1, _wfc2 = st.columns(2)
                _per_labels = [str(p) for p in _periodos]
                _sel_prev = _wfc1.selectbox("Mes Anterior", _per_labels[:-1], index=len(_per_labels) - 2, key="wf_prev")
                _sel_curr = _wfc2.selectbox("Mes Actual", _per_labels[1:], index=len(_per_labels) - 2, key="wf_curr")

                _p_prev = pd.Period(_sel_prev)
                _p_curr = pd.Period(_sel_curr)

                _prev = _wf_last[_wf_last["PERIODO"] == _p_prev][["SKU_PRODUCTO", "IS_CRITICO", "STOCK_COSTO"]].copy()
                _prev.columns = ["SKU_PRODUCTO", "CRIT_PREV", "STOCK_PREV"]
                _curr = _wf_last[_wf_last["PERIODO"] == _p_curr][["SKU_PRODUCTO", "IS_CRITICO", "STOCK_COSTO"]].copy()
                _curr.columns = ["SKU_PRODUCTO", "CRIT_CURR", "STOCK_CURR"]

                _comp = pd.merge(_prev, _curr, on="SKU_PRODUCTO", how="outer")
                _comp["CRIT_PREV"] = _comp["CRIT_PREV"].fillna(False)
                _comp["CRIT_CURR"] = _comp["CRIT_CURR"].fillna(False)
                _comp["STOCK_PREV"] = pd.to_numeric(_comp["STOCK_PREV"], errors="coerce").fillna(0)
                _comp["STOCK_CURR"] = pd.to_numeric(_comp["STOCK_CURR"], errors="coerce").fillna(0)

                _stock_inicio = _comp.loc[_comp["CRIT_PREV"], "STOCK_PREV"].sum()
                _nuevos = _comp.loc[~_comp["CRIT_PREV"] & _comp["CRIT_CURR"], "STOCK_CURR"].sum()
                _recuperados = _comp.loc[_comp["CRIT_PREV"] & ~_comp["CRIT_CURR"], "STOCK_PREV"].sum()
                _permanecen_prev = _comp.loc[_comp["CRIT_PREV"] & _comp["CRIT_CURR"], "STOCK_PREV"].sum()
                _permanecen_curr = _comp.loc[_comp["CRIT_PREV"] & _comp["CRIT_CURR"], "STOCK_CURR"].sum()
                _delta_perm = _permanecen_curr - _permanecen_prev
                _stock_final = _comp.loc[_comp["CRIT_CURR"], "STOCK_CURR"].sum()

                _n_nuevos = int((~_comp["CRIT_PREV"] & _comp["CRIT_CURR"]).sum())
                _n_recup = int((_comp["CRIT_PREV"] & ~_comp["CRIT_CURR"]).sum())
                _n_perm = int((_comp["CRIT_PREV"] & _comp["CRIT_CURR"]).sum())

                fig_wf = go.Figure(go.Waterfall(
                    x=[f"Inicio<br>({_sel_prev})", f"Nuevos Criticos<br>({_n_nuevos} SKUs)",
                       f"Recuperados<br>({_n_recup} SKUs)", f"Delta Permanentes<br>({_n_perm} SKUs)",
                       f"Final<br>({_sel_curr})"],
                    y=[_stock_inicio, _nuevos, -_recuperados, _delta_perm, _stock_final],
                    measure=["absolute", "relative", "relative", "relative", "total"],
                    connector=dict(line=dict(color="#ECECEC", width=1)),
                    increasing=dict(marker=dict(color=COLORS["status_critical"])),
                    decreasing=dict(marker=dict(color=COLORS["status_on_track"])),
                    totals=dict(marker=dict(color=COLORS["primary"])),
                    texttemplate="$%{y:,.0f}",
                    textposition="outside",
                    textfont=dict(size=11),
                    hovertemplate="%{x}<br>Monto: $%{y:,.0f}<extra></extra>",
                ))
                fig_wf.update_layout(**dorel_layout(
                    title=dict(text=f"Flujo de Stock Critico: {_sel_prev} → {_sel_curr}", font_size=15, x=0.5),
                    height=450,
                    margin=dict(l=20, r=20, t=70, b=40),
                    showlegend=False,
                ))
                st.plotly_chart(fig_wf, use_container_width=True)
                figures_to_export["Waterfall Recuperacion"] = fig_wf

                # Summary metrics
                _wm1, _wm2, _wm3, _wm4 = st.columns(4)
                _cambio = _stock_final - _stock_inicio
                _cambio_pct = (_cambio / _stock_inicio * 100) if _stock_inicio > 0 else 0
                _wm1.metric("Stock Critico Inicio", _fmt_cl(_stock_inicio))
                _wm2.metric("Nuevos Criticos", _fmt_cl(_nuevos), delta=f"+{_n_nuevos} SKUs", delta_color="inverse")
                _wm3.metric("Recuperados", _fmt_cl(_recuperados), delta=f"-{_n_recup} SKUs", delta_color="normal")
                _wm4.metric("Stock Critico Final", _fmt_cl(_stock_final), delta=f"{_cambio_pct:+.1f}%",
                            delta_color="inverse")
            else:
                st.info("Se necesitan al menos 2 meses de datos para el Waterfall.")

        # ---- CHART 10: Breakdown por Canal ----
        st.markdown("---")
        st.markdown("### 10. Stock Critico por Canal de Distribucion")

        if not df_crit_det.empty and "CANAL_DETALLE" in df_crit_det.columns:
            _canal_agg = df_crit_det.groupby("CANAL_DETALLE", as_index=False).agg(
                STOCK_COSTO=("STOCK_COSTO", "sum"),
                N_SKUS=("SKU_PRODUCTO", "nunique"),
            )
            _canal_agg = _canal_agg.sort_values("STOCK_COSTO", ascending=False)
            _canal_total = _canal_agg["STOCK_COSTO"].sum()
            _canal_agg["PCT"] = np.where(_canal_total > 0, _canal_agg["STOCK_COSTO"] / _canal_total * 100, 0)

            _canal_colors = {
                "TIENDA": COLORS["status_at_risk"],
                "CD INMOVILIZADO": COLORS["status_critical"],
                "CD": COLORS["primary"],
                "ETAIL": "#8B5CF6",
                "MAYORISTA": "#06B6D4",
            }
            _cc = [_canal_colors.get(c, "#9E9E9E") for c in _canal_agg["CANAL_DETALLE"]]

            _col_donut, _col_tabla = st.columns([3, 2])

            with _col_donut:
                fig_canal = go.Figure(go.Pie(
                    labels=_canal_agg["CANAL_DETALLE"].tolist(),
                    values=_canal_agg["STOCK_COSTO"].tolist(),
                    hole=0.5,
                    marker=dict(colors=_cc, line=dict(color="white", width=2)),
                    texttemplate="%{label}<br>%{percent}",
                    textfont=dict(size=12),
                    hovertemplate="%{label}<br>Monto: $%{value:,.0f}<br>%{percent}<extra></extra>",
                    customdata=_canal_agg["N_SKUS"].tolist(),
                ))
                fig_canal.update_layout(**dorel_layout(
                    title=dict(text="Distribucion del Stock Critico por Canal", font_size=15, x=0.5),
                    height=420,
                    margin=dict(l=20, r=20, t=60, b=20),
                    showlegend=True,
                    legend=dict(orientation="h", yanchor="bottom", y=-0.15, xanchor="center", x=0.5),
                    annotations=[dict(
                        text=f"<b>${human_format(_canal_total)}</b>",
                        x=0.5, y=0.5, font_size=16, showarrow=False,
                    )],
                ))
                st.plotly_chart(fig_canal, use_container_width=True)
                figures_to_export["Breakdown Canal"] = fig_canal

            with _col_tabla:
                st.markdown("**Detalle por Canal**")
                _tbl = _canal_agg.copy()
                _tbl["Monto"] = _tbl["STOCK_COSTO"].apply(_fmt_cl)
                _tbl["% del Total"] = _tbl["PCT"].apply(lambda x: f"{x:.1f}%")
                _tbl["SKUs"] = _tbl["N_SKUS"]
                st.dataframe(
                    _tbl[["CANAL_DETALLE", "Monto", "SKUs", "% del Total"]].rename(columns={"CANAL_DETALLE": "Canal"}),
                    use_container_width=True,
                    hide_index=True,
                )
        else:
            st.info("No hay datos de detalle por canal disponibles.")

        # ---- CHART 11: Impacto Financiero ----
        st.markdown("---")
        st.markdown("### 11. Impacto Financiero del Stock Critico")
        st.caption(
            "Estima el costo de mantener stock critico inmovilizado. "
            "Tasa de carrying cost: **2% mensual** (24% anual) — incluye almacenaje, seguros, "
            "obsolescencia y costo de oportunidad del capital."
        )

        _CARRYING_RATE_MONTHLY = 0.02
        _cash_locked = crit_stock_val
        _carrying_monthly = _cash_locked * _CARRYING_RATE_MONTHLY
        _carrying_annual = _carrying_monthly * 12
        _pct_inventory = (crit_stock_val / total_stock_val * 100) if total_stock_val > 0 else 0

        # KPI cards
        _fk1, _fk2, _fk3, _fk4 = st.columns(4)
        _fin_data = [
            ("Cash Atrapado", _fmt_cl(_cash_locked), COLORS["status_critical"]),
            ("Costo Mantención/Mes", _fmt_cl(_carrying_monthly), COLORS["status_at_risk"]),
            ("Costo Mantención/Año", _fmt_cl(_carrying_annual), "#E53935"),
            ("% del Inventario", f"{_pct_inventory:.1f}%", COLORS["primary"]),
        ]
        for _fk_col, (_fk_lab, _fk_val, _fk_clr) in zip([_fk1, _fk2, _fk3, _fk4], _fin_data):
            with _fk_col:
                st.html(f"""
                <div style='background:#fff;padding:1rem;border-radius:10px;text-align:center;
                             border-top:4px solid {_fk_clr};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                    <div style='font-size:1.4rem;font-weight:bold;color:{_fk_clr}'>{_fk_val}</div>
                    <div style='font-size:0.7rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{_fk_lab}</div>
                </div>""")

        st.html("<br>")

        # Discount simulator
        _desc_pct = st.slider(
            "Simulador: Descuento de Liquidacion sobre Costo (%)",
            min_value=10, max_value=70, value=30, step=5,
            key="fin_discount_slider",
            help="Simula venta del stock critico a un % de descuento sobre el costo. "
                 "Ej: 30% = se vende al 70% del costo unitario.",
        )
        _recuperacion = _cash_locked * (1 - _desc_pct / 100)
        _perdida = _cash_locked * _desc_pct / 100
        _ahorro_carrying = _carrying_annual  # If liquidated, no more carrying cost

        _sm1, _sm2, _sm3 = st.columns(3)
        _sm1.metric("Recuperacion Estimada", _fmt_cl(_recuperacion),
                    help=f"Venta al {100 - _desc_pct}% del costo")
        _sm2.metric("Perdida por Liquidacion", _fmt_cl(_perdida),
                    delta=f"-{_desc_pct}%", delta_color="inverse")
        _sm3.metric("Ahorro Carrying Anual", _fmt_cl(_ahorro_carrying),
                    delta="liberado", delta_color="normal",
                    help="Si se liquida todo, se deja de pagar este costo anual")

        # Top 10 lines by carrying cost
        if "LINEA" in df_last_m.columns:
            _crit_mask_fin = (
                ((df_last_m["MOI"] >= 12) | (df_last_m["MOI"].isna()))
                & _cond_ant_critico(df_last_m["ANTIGUEDAD_MESES"], df_last_m["MOI"])
            )
            _crit_lines = (
                df_last_m[_crit_mask_fin]
                .groupby("LINEA", as_index=False)["STOCK_COSTO"]
                .sum()
                .sort_values("STOCK_COSTO", ascending=True)
                .tail(10)
            )
            _crit_lines["CARRYING_ANUAL"] = _crit_lines["STOCK_COSTO"] * _CARRYING_RATE_MONTHLY * 12

            fig_fin = go.Figure(layout=dorel_layout(
                title=dict(text="Top 10 Lineas: Costo Anual de Mantener Stock Critico", font_size=15, x=0.5),
                xaxis=dict(title="Carrying Cost Anual (CLP)", gridcolor="#ECECEC"),
                height=420,
                margin=dict(l=200, r=30, t=60, b=40),
            ))
            fig_fin.add_trace(go.Bar(
                y=_crit_lines["LINEA"].tolist(),
                x=_crit_lines["CARRYING_ANUAL"].tolist(),
                orientation="h",
                marker=dict(
                    color=_crit_lines["CARRYING_ANUAL"].tolist(),
                    colorscale=[[0, COLORS["status_at_risk"]], [1, COLORS["status_critical"]]],
                    line=dict(width=0),
                ),
                text=[f"${human_format(v)}/año" for v in _crit_lines["CARRYING_ANUAL"]],
                textposition="outside",
                textfont=dict(size=10),
                hovertemplate=(
                    "Linea: %{y}<br>"
                    "Stock Critico: $%{customdata:,.0f}<br>"
                    "Carrying Anual: $%{x:,.0f}<extra></extra>"
                ),
                customdata=_crit_lines["STOCK_COSTO"].tolist(),
                showlegend=False,
            ))
            st.plotly_chart(fig_fin, use_container_width=True)
            figures_to_export["Carrying Cost Lineas"] = fig_fin

        # ---- CHART 12: Matriz Salud de Stock (6x6) ----
        st.markdown("---")
        st.markdown("### 12. Matriz de Salud de Stock (MOI x Antiguedad)")
        st.caption(
            "Cruza **MOI** (meses de inventario segun venta) con **Antiguedad** (meses desde ultimo ingreso). "
            "Rangos: 0-3m | 3-6m | 6-8m | 8-12m | 12-24m | >=24m. "
            "El cuadrante rojo oscuro (>=24m x >=24m) requiere liquidacion a precios agresivos."
        )

        # Filtro adicional: Mix Oficial (solo para esta seccion)
        _mix_opts = sorted(df_last_m["MIX_OFICIAL"].dropna().unique().tolist()) if "MIX_OFICIAL" in df_last_m.columns else []
        _sel_mix = st.multiselect("Mix Oficial", _mix_opts, key="h9_mix")

        _df_9b = df_last_m.copy()
        if _sel_mix and "MIX_OFICIAL" in _df_9b.columns:
            _df_9b = _df_9b[_df_9b["MIX_OFICIAL"].isin(_sel_mix)]

        if _df_9b.empty:
            st.warning("No hay datos con los filtros seleccionados.")
        else:
            _df_valid, _df_sin_info = _prepare_health_data(_df_9b)

            # --- KPI Cards (7 KPIs in 2 rows: 4 + 3) ---
            _total_skus = _df_valid["SKU_PRODUCTO"].nunique() if not _df_valid.empty else 0
            _total_stock = _df_valid["STOCK_COSTO"].sum() if not _df_valid.empty else 0
            _total_und = _df_valid["STOCK_UNIDADES"].sum() if (not _df_valid.empty and "STOCK_UNIDADES" in _df_valid.columns) else 0

            # Saludable: MOI < 6m AND Antiguedad < 6m
            _mask_sano = (
                _df_valid["TIER_MOI"].isin(["0-3m", "3-6m"]) &
                _df_valid["TIER_ANTIGUEDAD"].isin(["0-3m", "3-6m"])
            ) if not _df_valid.empty else pd.Series(dtype=bool)
            _stock_sano = _df_valid.loc[_mask_sano, "STOCK_COSTO"].sum() if not _df_valid.empty else 0
            _pct_sano = (_stock_sano / _total_stock * 100) if _total_stock > 0 else 0

            # A Punto: (MOI 8-12m o Sin MOI [>=24m]) AND Antiguedad 8-12m
            _mask_apunto = (
                _df_valid["TIER_MOI"].isin(["8-12m", ">=24m"]) &
                _df_valid["TIER_ANTIGUEDAD"].isin(["8-12m"])
            ) if not _df_valid.empty else pd.Series(dtype=bool)
            _stock_apunto = _df_valid.loc[_mask_apunto, "STOCK_COSTO"].sum() if not _df_valid.empty else 0

            # Critico: (MOI >= 12 o Sin MOI [>=24m]) AND Antiguedad >= 12
            _mask_crit = (
                _df_valid["TIER_MOI"].isin(["12-24m", ">=24m"]) &
                _df_valid["TIER_ANTIGUEDAD"].isin(["12-24m", ">=24m"])
            ) if not _df_valid.empty else pd.Series(dtype=bool)
            _stock_crit = _df_valid.loc[_mask_crit, "STOCK_COSTO"].sum() if not _df_valid.empty else 0

            # Liquidacion: (MOI >= 24 o Sin MOI [>=24m]) AND Antiguedad >= 24
            _mask_liq = (
                (_df_valid["TIER_MOI"] == ">=24m") &
                (_df_valid["TIER_ANTIGUEDAD"] == ">=24m")
            ) if not _df_valid.empty else pd.Series(dtype=bool)
            _stock_liq = _df_valid.loc[_mask_liq, "STOCK_COSTO"].sum() if not _df_valid.empty else 0

            # Row 1: 4 KPIs generales
            _kpi_row1 = st.columns(4)
            _r1_labels = ["Total SKUs", "Stock Unidades", "Stock Costo", "% Saludable"]
            _r1_vals = [
                f"{_total_skus:,}".replace(",", "."),
                _fmt_und(_total_und),
                _fmt_cl(_total_stock),
                f"{_pct_sano:.1f}%",
            ]
            _r1_colors = [
                COLORS["primary"], COLORS["primary"], COLORS["primary"],
                COLORS["status_on_track"],
            ]
            for _col, _lab, _val, _clr in zip(_kpi_row1, _r1_labels, _r1_vals, _r1_colors):
                with _col:
                    st.html(f"""
                    <div style='background:#fff;padding:1rem;border-radius:10px;text-align:center;
                                 border-top:4px solid {_clr};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                        <div style='font-size:1.5rem;font-weight:bold;color:{_clr}'>{_val}</div>
                        <div style='font-size:0.75rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{_lab}</div>
                    </div>""")

            # Row 2: 3 KPIs de riesgo
            _kpi_row2 = st.columns(3)
            _r2_labels = ["Stock A Punto", "Stock Critico", "Stock Liquidacion"]
            _r2_vals = [_fmt_cl(_stock_apunto), _fmt_cl(_stock_crit), _fmt_cl(_stock_liq)]
            _r2_colors = ["#F9A825", COLORS["status_at_risk"], COLORS["status_critical"]]
            _r2_helps = [
                "MOI 8-12m (o sin MOI) + Antiguedad 8-12m",
                "MOI >=12m (o sin MOI) + Antiguedad >=12m",
                "MOI >=24m (o sin MOI) + Antiguedad >=24m",
            ]
            for _col, _lab, _val, _clr, _hlp in zip(_kpi_row2, _r2_labels, _r2_vals, _r2_colors, _r2_helps):
                with _col:
                    st.html(f"""
                    <div style='background:#fff;padding:1rem;border-radius:10px;text-align:center;
                                 border-top:4px solid {_clr};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                        <div style='font-size:1.5rem;font-weight:bold;color:{_clr}'>{_val}</div>
                        <div style='font-size:0.75rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{_lab}</div>
                        <div style='font-size:0.6rem;color:{COLORS["medium_gray"]};margin-top:4px'>{_hlp}</div>
                    </div>""")

            _sin_info_n = _df_sin_info["SKU_PRODUCTO"].nunique() if not _df_sin_info.empty else 0
            if _sin_info_n > 0:
                st.caption(f"{_sin_info_n} SKUs excluidos por falta de fecha de ingreso (antiguedad desconocida).")
            st.html("<br>")

            # --- Health Matrix Chart + Line Ranking ---
            _matrix = _build_health_matrix(_df_valid)

            _col_9b, _col_rank = st.columns([3, 2])

            with _col_9b:
                # Build risk score for heatmap z-values
                _matrix["_RISK"] = _matrix.apply(
                    lambda r: (_MOI_TIERS.index(r["TIER_MOI"]) + _ANT_TIERS.index(r["TIER_ANTIGUEDAD"]))
                    if r["TIER_MOI"] in _MOI_TIERS and r["TIER_ANTIGUEDAD"] in _ANT_TIERS else 0,
                    axis=1,
                )
                _z_pivot = _matrix.pivot_table(
                    index="TIER_MOI", columns="TIER_ANTIGUEDAD",
                    values="_RISK", fill_value=0,
                ).reindex(index=_MOI_TIERS, columns=_ANT_TIERS, fill_value=0)

                # Build customdata pivots for hover
                _n_pivot = _matrix.pivot_table(
                    index="TIER_MOI", columns="TIER_ANTIGUEDAD", values="N_SKUS", fill_value=0
                ).reindex(index=_MOI_TIERS, columns=_ANT_TIERS, fill_value=0)
                _stk_pivot = _matrix.pivot_table(
                    index="TIER_MOI", columns="TIER_ANTIGUEDAD", values="STOCK_COSTO", fill_value=0
                ).reindex(index=_MOI_TIERS, columns=_ANT_TIERS, fill_value=0)
                _und_pivot = _matrix.pivot_table(
                    index="TIER_MOI", columns="TIER_ANTIGUEDAD", values="STOCK_UNIDADES", fill_value=0
                ).reindex(index=_MOI_TIERS, columns=_ANT_TIERS, fill_value=0)
                _pct_pivot = _matrix.pivot_table(
                    index="TIER_MOI", columns="TIER_ANTIGUEDAD", values="PCT_STOCK", fill_value=0
                ).reindex(index=_MOI_TIERS, columns=_ANT_TIERS, fill_value=0)

                _cdata = np.stack([_n_pivot.values, _stk_pivot.values, _und_pivot.values, _pct_pivot.values], axis=-1)

                # Build discrete colorscale from local _HEALTH_GRADIENT
                _nc = len(_HEALTH_GRADIENT)
                _cscale = [[i / (_nc - 1), c] for i, c in enumerate(_HEALTH_GRADIENT)]

                _fig_health = go.Figure(layout=dorel_layout(
                    title=dict(text="Matriz de Salud de Stock (6x6)", font_size=16),
                    height=500,
                    xaxis=dict(
                        title="Antiguedad", side="top",
                        categoryorder="array", categoryarray=_ANT_TIERS,
                        tickangle=-30, tickfont=dict(size=11),
                    ),
                    yaxis=dict(
                        title="MOI (Meses de Inventario)",
                        categoryorder="array", categoryarray=_MOI_TIERS,
                        tickfont=dict(size=11),
                    ),
                    showlegend=False,
                ))
                _fig_health.add_trace(go.Heatmap(
                    z=_z_pivot.values,
                    x=_ANT_TIERS,
                    y=_MOI_TIERS,
                    colorscale=_cscale,
                    zmin=0, zmax=10,
                    showscale=False,
                    xgap=2, ygap=2,
                    customdata=_cdata,
                    hovertemplate=(
                        "MOI: %{y}<br>Antiguedad: %{x}<br>"
                        "SKUs: %{customdata[0]:,.0f}<br>"
                        "Stock $: $%{customdata[1]:,.0f}<br>"
                        "Unidades: %{customdata[2]:,.0f}<br>"
                        "%% Stock: %{customdata[3]:.1f}%<extra></extra>"
                    ),
                ))
                # Add text annotations
                _label_pivot = _matrix.pivot_table(
                    index="TIER_MOI", columns="TIER_ANTIGUEDAD",
                    values="LABEL", aggfunc="first", fill_value="",
                ).reindex(index=_MOI_TIERS, columns=_ANT_TIERS, fill_value="")
                _color_pivot = _matrix.pivot_table(
                    index="TIER_MOI", columns="TIER_ANTIGUEDAD",
                    values="COLOR", aggfunc="first", fill_value="#ccc",
                ).reindex(index=_MOI_TIERS, columns=_ANT_TIERS, fill_value="#ccc")

                for _mi, _moi_t in enumerate(_MOI_TIERS):
                    for _ai, _ant_t in enumerate(_ANT_TIERS):
                        _lbl = str(_label_pivot.iloc[_mi, _ai]).replace("\n", "<br>")
                        _cell_clr = str(_color_pivot.iloc[_mi, _ai])
                        _txt_clr = "white" if _cell_clr in _WHITE_TEXT_COLORS else "#333333"
                        _fig_health.add_annotation(
                            x=_ant_t, y=_moi_t,
                            text=_lbl, showarrow=False,
                            font=dict(size=9, color=_txt_clr),
                        )

                st.plotly_chart(_fig_health, use_container_width=True)

            with _col_rank:
                # Line health ranking
                if not _df_valid.empty and "LINEA" in _df_valid.columns:
                    _df_w = _df_valid.copy()
                    _df_w["IS_CRITICO"] = (
                        _df_w["TIER_MOI"].isin(["12-24m", ">=24m"]) &
                        _df_w["TIER_ANTIGUEDAD"].isin(["12-24m", ">=24m"])
                    )
                    _df_w["STOCK_CRITICO_SKU"] = np.where(_df_w["IS_CRITICO"], _df_w["STOCK_COSTO"], 0)

                    _by_line = _df_w.groupby("LINEA", as_index=False).agg(
                        TOTAL_SKUS=("SKU_PRODUCTO", "nunique"),
                        STOCK_TOTAL=("STOCK_COSTO", "sum"),
                        SKUS_CRITICOS=("IS_CRITICO", "sum"),
                        STOCK_CRITICO=("STOCK_CRITICO_SKU", "sum"),
                    )
                    _by_line["PCT_STOCK_CRITICO"] = np.where(
                        _by_line["STOCK_TOTAL"] > 0,
                        _by_line["STOCK_CRITICO"] / _by_line["STOCK_TOTAL"] * 100,
                        0,
                    )
                    _by_line = _by_line.sort_values("PCT_STOCK_CRITICO", ascending=False).head(15)

                    if not _by_line.empty:
                        _bl_sorted = _by_line.sort_values("PCT_STOCK_CRITICO", ascending=True)
                        # Color scale: reds proportional to % critico
                        _max_pct = _bl_sorted["PCT_STOCK_CRITICO"].max() if not _bl_sorted.empty else 1
                        _bar_colors_r = [
                            f"rgba(198,40,40,{max(0.3, v / max(_max_pct, 1))})"
                            for v in _bl_sorted["PCT_STOCK_CRITICO"]
                        ]
                        _fig_rank = go.Figure(layout=dorel_layout(
                            title=dict(text="Top 15 Lineas: % Stock Critico", font_size=14),
                            height=max(len(_bl_sorted) * 28, 200),
                            xaxis=dict(title="% Stock Critico"),
                            yaxis=dict(title=""),
                            showlegend=False,
                        ))
                        _fig_rank.add_trace(go.Bar(
                            x=_bl_sorted["PCT_STOCK_CRITICO"],
                            y=_bl_sorted["LINEA"],
                            orientation="h",
                            marker_color=_bar_colors_r,
                            customdata=np.column_stack([
                                _bl_sorted["STOCK_CRITICO"],
                                _bl_sorted["STOCK_TOTAL"],
                            ]),
                            hovertemplate=(
                                "Linea: %{y}<br>% Critico: %{x:.1f}%<br>"
                                "Stock Critico $: $%{customdata[0]:,.0f}<br>"
                                "Stock Total $: $%{customdata[1]:,.0f}<extra></extra>"
                            ),
                        ))
                        st.plotly_chart(_fig_rank, use_container_width=True)
                    else:
                        st.info("Ninguna linea con stock en cuadrantes criticos.")
                else:
                    st.info("Sin datos para ranking de lineas.")

            # --- Cell descriptions (only cells with data) ---
            with st.expander("Descripcion de celdas con stock"):
                for _moi_t in _MOI_TIERS:
                    for _ant_t in _ANT_TIERS:
                        _row = _matrix[(_matrix["TIER_MOI"] == _moi_t) & (_matrix["TIER_ANTIGUEDAD"] == _ant_t)]
                        _n = int(_row["N_SKUS"].values[0]) if not _row.empty else 0
                        if _n == 0:
                            continue
                        _clr = _health_color(_moi_t, _ant_t)
                        _desc = _health_description(_moi_t, _ant_t)
                        _stk = _fmt_cl(_row["STOCK_COSTO"].values[0]) if not _row.empty else "$0"
                        st.html(
                            f'<span style="display:inline-block;width:14px;height:14px;background:{_clr};'
                            f'border-radius:3px;vertical-align:middle;margin-right:6px"></span>'
                            f'<strong>MOI {_moi_t} x Ant {_ant_t}</strong> — {_n} SKUs · {_stk} — {_desc}'
                        )

            # --- Drill-down ---
            st.markdown("#### Detalle por Celda")
            _dc1, _dc2 = st.columns(2)

            # Opciones Tier MOI: 6 originales + SIN MOI + combinada
            _moi_options = _MOI_TIERS + ["SIN MOI", ">=24m y SIN MOI"]
            _sel_mois = _dc1.multiselect("Tier MOI", _moi_options, default=[">=24m"], key="drill_moi_9b")
            _sel_ants = _dc2.multiselect("Tier Antiguedad", _ANT_TIERS, default=[">=24m"], key="drill_ant_9b")

            # Construir máscara MOI (combina todas las selecciones)
            _has_sin_moi = "SIN_MOI" in _df_valid.columns
            if _sel_mois and not _df_valid.empty:
                _moi_mask = pd.Series(False, index=_df_valid.index)
                for _sm in _sel_mois:
                    if _sm == "SIN MOI":
                        _moi_mask |= _df_valid["SIN_MOI"] if _has_sin_moi else False
                    elif _sm == ">=24m y SIN MOI":
                        _moi_mask |= (_df_valid["TIER_MOI"] == ">=24m")
                    else:
                        if _has_sin_moi:
                            _moi_mask |= (_df_valid["TIER_MOI"] == _sm) & (~_df_valid["SIN_MOI"])
                        else:
                            _moi_mask |= (_df_valid["TIER_MOI"] == _sm)
            else:
                _moi_mask = pd.Series(True, index=_df_valid.index) if not _df_valid.empty else pd.Series(dtype=bool)

            # Construir máscara Antiguedad
            if _sel_ants and not _df_valid.empty:
                _ant_mask = _df_valid["TIER_ANTIGUEDAD"].isin(_sel_ants)
            else:
                _ant_mask = pd.Series(True, index=_df_valid.index) if not _df_valid.empty else pd.Series(dtype=bool)

            _filtered = _df_valid[_moi_mask & _ant_mask].copy() if not _df_valid.empty else pd.DataFrame()

            # Resumen visual
            _cell_n = _filtered["SKU_PRODUCTO"].nunique() if not _filtered.empty else 0
            _cell_stock = _filtered["STOCK_COSTO"].sum() if not _filtered.empty else 0
            _cell_und = _filtered["STOCK_UNIDADES"].sum() if (not _filtered.empty and "STOCK_UNIDADES" in _filtered.columns) else 0
            _moi_label = ", ".join(_sel_mois) if _sel_mois else "Todos"
            _ant_label = ", ".join(_sel_ants) if _sel_ants else "Todos"

            # Color: usar el de mayor riesgo seleccionado
            _worst_moi = ">=24m"
            for _t in reversed(_MOI_TIERS):
                if _t in _sel_mois or ">=24m y SIN MOI" in _sel_mois or "SIN MOI" in _sel_mois:
                    _worst_moi = ">=24m"
                    break
                if _t in _sel_mois:
                    _worst_moi = _t
                    break
            _worst_ant = _sel_ants[-1] if _sel_ants else ">=24m"
            _cell_color = _health_color(_worst_moi, _worst_ant)

            st.html(f"""
            <div style='background:{_cell_color};padding:1rem;border-radius:8px;color:white;margin-bottom:1rem'>
                <strong>MOI [{_moi_label}] x Antiguedad [{_ant_label}]</strong> — {_cell_n} SKUs · {_fmt_cl(_cell_stock)} · {_fmt_und(_cell_und)} und
            </div>""")

            if not _filtered.empty:
                _show_cols = ["SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "MARCA",
                              "STOCK_UNIDADES", "STOCK_COSTO", "MOI", "ANTIGUEDAD_MESES"]
                _show_cols = [c for c in _show_cols if c in _filtered.columns]
                _filtered = _filtered.sort_values("STOCK_COSTO", ascending=False)
                _filtered_disp = _filtered[_show_cols].head(200).copy()
                if "STOCK_COSTO" in _filtered_disp.columns:
                    _filtered_disp["STOCK_COSTO"] = _filtered_disp["STOCK_COSTO"].apply(fmt_clp)
                st.dataframe(
                    _filtered_disp,
                    column_config={
                        "STOCK_COSTO": st.column_config.TextColumn("Stock $"),
                        "STOCK_UNIDADES": st.column_config.NumberColumn("Unidades", format="%d"),
                        "MOI": st.column_config.NumberColumn("MOI", format="%.1f"),
                        "ANTIGUEDAD_MESES": st.column_config.NumberColumn("Antiguedad", format="%.1f"),
                    },
                    use_container_width=True, hide_index=True, height=400,
                )
                download_buttons(_filtered[_show_cols], "drilldown_salud")

                # --- Descarga por sucursal (solo sucursales con unidades > 0) ---
                st.markdown("##### Detalle por Sucursal")
                _drill_skus = set(_filtered["SKU_PRODUCTO"].unique())
                if os.path.exists(AUTO_DETAIL_FILE) and _drill_skus:
                    _suc_rows = []
                    for _chunk in pd.read_csv(AUTO_DETAIL_FILE, chunksize=CHUNK_SIZE):
                        _chunk.columns = [c.upper() for c in _chunk.columns]
                        if "FECHA" in _chunk.columns:
                            _chunk["FECHA"] = pd.to_datetime(_chunk["FECHA"], errors="coerce")
                            _chunk = _chunk[_chunk["FECHA"] == _chunk["FECHA"].max()]
                        _chunk = _chunk[_chunk["SKU_PRODUCTO"].isin(_drill_skus)]
                        if not _chunk.empty:
                            _suc_rows.append(_chunk)

                    if _suc_rows:
                        _df_suc = pd.concat(_suc_rows, ignore_index=True)
                        # Coerción numérica defensiva
                        for _nc in ["STOCK_COSTO", "STOCK_UNIDADES"]:
                            if _nc in _df_suc.columns:
                                _df_suc[_nc] = pd.to_numeric(_df_suc[_nc], errors="coerce").fillna(0)
                        # Fallback: usar COD_BODEGA cuando el JOIN a Syncro no matcheó
                        if "COD_BODEGA" in _df_suc.columns:
                            if "ID_SUCURSAL" in _df_suc.columns:
                                _df_suc["ID_SUCURSAL"] = _df_suc["ID_SUCURSAL"].fillna(_df_suc["COD_BODEGA"])
                            if "DESCRIPCION_SUCURSAL" in _df_suc.columns:
                                _df_suc["DESCRIPCION_SUCURSAL"] = _df_suc["DESCRIPCION_SUCURSAL"].fillna(
                                    "Bodega " + _df_suc["COD_BODEGA"].astype(str)
                                )
                        else:
                            if "ID_SUCURSAL" in _df_suc.columns:
                                _df_suc["ID_SUCURSAL"] = _df_suc["ID_SUCURSAL"].fillna("SIN_ID")
                            if "DESCRIPCION_SUCURSAL" in _df_suc.columns:
                                _df_suc["DESCRIPCION_SUCURSAL"] = _df_suc["DESCRIPCION_SUCURSAL"].fillna("Sin Sucursal Identificada")
                        if "CANAL_DE_DISTRIBUCION" in _df_suc.columns:
                            _df_suc["CANAL_DE_DISTRIBUCION"] = _df_suc["CANAL_DE_DISTRIBUCION"].fillna("Sin Canal")
                        # Filtrar solo sucursales con stock > 0
                        if "STOCK_UNIDADES" in _df_suc.columns:
                            _df_suc = _df_suc[_df_suc["STOCK_UNIDADES"] > 0].copy()
                        else:
                            _df_suc = _df_suc[_df_suc["STOCK_COSTO"] > 0].copy()

                        if not _df_suc.empty:
                            _suc_cols = [c for c in [
                                "SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "MARCA",
                                "COD_BODEGA", "ID_SUCURSAL", "DESCRIPCION_SUCURSAL", "CANAL_DE_DISTRIBUCION",
                                "SUPERVISOR", "CLUSTER",
                                "STOCK_UNIDADES", "STOCK_COSTO",
                            ] if c in _df_suc.columns]
                            _df_suc = _df_suc[_suc_cols].sort_values(
                                ["SKU_PRODUCTO", "STOCK_UNIDADES"], ascending=[True, False]
                            )
                            st.caption(
                                f"{len(_df_suc):,} registros SKU×Sucursal con stock > 0 "
                                f"({_df_suc['DESCRIPCION_SUCURSAL'].nunique() if 'DESCRIPCION_SUCURSAL' in _df_suc.columns else '?'} sucursales)"
                            )
                            _df_suc_disp = _df_suc.head(300).copy()
                            if "STOCK_COSTO" in _df_suc_disp.columns:
                                _df_suc_disp["STOCK_COSTO"] = _df_suc_disp["STOCK_COSTO"].apply(fmt_clp)
                            st.dataframe(
                                _df_suc_disp,
                                column_config={
                                    "STOCK_COSTO": st.column_config.TextColumn("Stock $"),
                                    "STOCK_UNIDADES": st.column_config.NumberColumn("Unidades", format="%d"),
                                },
                                use_container_width=True, hide_index=True, height=400,
                            )
                            download_buttons(_df_suc, "drilldown_sucursal")
                        else:
                            st.info("No hay sucursales con stock > 0 para los SKUs seleccionados.")
                    else:
                        st.info("No se encontro detalle por sucursal. Actualiza los datos primero.")
                else:
                    st.caption("Actualiza los datos para ver detalle por sucursal.")
            else:
                st.info("No hay SKUs en esta celda.")

            # --- SKUs sin clasificar ---
            if not _df_sin_info.empty:
                _si_n = _df_sin_info["SKU_PRODUCTO"].nunique()
                _si_stock = _df_sin_info["STOCK_COSTO"].sum()
                with st.expander(f"SKUs sin clasificar ({_si_n} SKUs · {_fmt_cl(_si_stock)})"):
                    st.caption("Excluidos de la matriz por falta de MOI o fecha de ingreso.")
                    _si_cols = ["SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "STOCK_COSTO", "MOI", "ANTIGUEDAD_MESES"]
                    _si_cols = [c for c in _si_cols if c in _df_sin_info.columns]
                    st.dataframe(_df_sin_info[_si_cols].head(200), use_container_width=True, hide_index=True)

            # --- Export with tiers ---
            if not _df_valid.empty:
                download_buttons(
                    _df_valid[["SKU_PRODUCTO"] + [c for c in _df_valid.columns if c != "SKU_PRODUCTO"]],
                    "salud_stock_matriz",
                )

        # ---- CHART 13: Scatter Velocidad de Venta vs Stock Critico ----
        st.markdown("---")
        st.markdown("### 13. Velocidad de Venta vs Stock Critico (Cuadrante Accionable)")
        st.caption(
            "Cada burbuja es un SKU critico. **Eje X**: velocidad de venta (und/dia con venta). "
            "**Eje Y**: stock al costo ($). **Tamano**: MOI. Cuadrantes definen accion recomendada."
        )

        if not df_sales_crit.empty and "UNIDADES_VENDIDAS" in df_sales_crit.columns:
            # Aggregate sales per critical SKU
            _s_pos = df_sales_crit[df_sales_crit["UNIDADES_VENDIDAS"] > 0]
            _sales_agg_13 = _s_pos.groupby("SKU_PRODUCTO").agg(
                TOTAL_UNITS=("UNIDADES_VENDIDAS", "sum"),
                TOTAL_NETO=("NETO_TOTAL", "sum"),
                TOTAL_APORTE=("APORTE_TOTAL", "sum") if "APORTE_TOTAL" in _s_pos.columns else ("NETO_TOTAL", "sum"),
                AVG_MARGIN=("MARGEN", "mean"),
                N_DAYS_SOLD=("FECHA", "nunique"),
            ).reset_index()
            _sales_agg_13 = _sales_agg_13[_sales_agg_13["TOTAL_UNITS"] > 0].copy()
            _sales_agg_13["VELOCITY"] = np.where(
                _sales_agg_13["N_DAYS_SOLD"] > 0,
                _sales_agg_13["TOTAL_UNITS"] / _sales_agg_13["N_DAYS_SOLD"],
                0,
            )

            # Merge with stock data
            _scatter_cols = ["SKU_PRODUCTO", "STOCK_COSTO", "MOI", "ANTIGUEDAD_MESES"]
            if "NOM_PRODUCTO" in df_last_m.columns:
                _scatter_cols.append("NOM_PRODUCTO")
            if "LINEA" in df_last_m.columns:
                _scatter_cols.append("LINEA")
            if "MARCA" in df_last_m.columns:
                _scatter_cols.append("MARCA")
            _scatter_data = _sales_agg_13.merge(
                df_last_m[cond_crit][_scatter_cols].drop_duplicates("SKU_PRODUCTO"),
                on="SKU_PRODUCTO", how="right",
            )
            _scatter_data["VELOCITY"] = _scatter_data["VELOCITY"].fillna(0)
            _scatter_data["TOTAL_NETO"] = _scatter_data["TOTAL_NETO"].fillna(0)
            _scatter_data["TOTAL_UNITS"] = _scatter_data["TOTAL_UNITS"].fillna(0)
            _scatter_data["AVG_MARGIN"] = _scatter_data["AVG_MARGIN"].fillna(0)
            _scatter_data["N_DAYS_SOLD"] = _scatter_data["N_DAYS_SOLD"].fillna(0)
            _scatter_data["STOCK_COSTO"] = pd.to_numeric(_scatter_data["STOCK_COSTO"], errors="coerce").fillna(0)
            _scatter_data["MOI"] = pd.to_numeric(_scatter_data["MOI"], errors="coerce").fillna(0)

            _n_with_sales = int((_scatter_data["VELOCITY"] > 0).sum())
            _n_no_sales = int((_scatter_data["VELOCITY"] == 0).sum())
            _total_crit_skus = len(_scatter_data)
            _val_no_sales = _scatter_data.loc[_scatter_data["VELOCITY"] == 0, "STOCK_COSTO"].sum()
            _pct_no_sales_val = (_val_no_sales / _scatter_data["STOCK_COSTO"].sum() * 100) if _scatter_data["STOCK_COSTO"].sum() > 0 else 0
            _total_revenue_crit = _scatter_data["TOTAL_NETO"].sum()

            # KPI row
            _k13a, _k13b, _k13c, _k13d = st.columns(4)
            _k13a.metric("SKUs con Venta", f"{_n_with_sales}")
            _k13b.metric("SKUs sin Venta (Stock Muerto)", f"{_n_no_sales}")
            _k13c.metric("% Valor sin Venta", f"{_pct_no_sales_val:.1f}%")
            _k13d.metric("Ingresos Acumulados Criticos", _fmt_cl(_total_revenue_crit))

            # Scatter plot
            _scatter_plot = _scatter_data[_scatter_data["STOCK_COSTO"] > 0].copy()
            if not _scatter_plot.empty:
                # Bubble size based on MOI (clipped)
                _moi_clip = _scatter_plot["MOI"].clip(upper=60)
                _bubble_size = np.where(_moi_clip > 0, 8 + (_moi_clip / 60) * 30, 8)

                _nom = _scatter_plot["NOM_PRODUCTO"].astype(str).str[:30] if "NOM_PRODUCTO" in _scatter_plot.columns else _scatter_plot["SKU_PRODUCTO"]
                _labels = _scatter_plot["SKU_PRODUCTO"] + " | " + _nom

                _linea_vals = _scatter_plot["LINEA"].fillna("N/D") if "LINEA" in _scatter_plot.columns else ["N/D"] * len(_scatter_plot)
                _hover_data = np.column_stack([
                    _scatter_plot["MOI"].values,
                    _linea_vals.values if hasattr(_linea_vals, "values") else _linea_vals,
                    _scatter_plot["AVG_MARGIN"].values,
                    _scatter_plot["N_DAYS_SOLD"].values,
                    _scatter_plot["TOTAL_UNITS"].values,
                ])

                # Separate selling vs dead stock for color differentiation
                _mask_selling = _scatter_plot["VELOCITY"] > 0
                fig_scatter = go.Figure(layout=dorel_layout(
                    title=dict(text="Stock Critico: Velocidad de Venta vs Valor", font_size=15, x=0.5),
                    xaxis_title="Velocidad (Und/dia de venta)",
                    yaxis_title="Stock Costo ($)",
                    height=550,
                ))

                # Dead stock (velocity = 0) — grey markers on y-axis
                if (~_mask_selling).any():
                    fig_scatter.add_trace(go.Scatter(
                        x=_scatter_plot.loc[~_mask_selling, "VELOCITY"],
                        y=_scatter_plot.loc[~_mask_selling, "STOCK_COSTO"],
                        mode="markers",
                        marker=dict(
                            size=_bubble_size[~_mask_selling.values],
                            color="#78909C",
                            opacity=0.7,
                            line=dict(width=2, color="#546E7A"),
                        ),
                        text=_labels[~_mask_selling],
                        customdata=_hover_data[~_mask_selling.values],
                        hovertemplate=(
                            "<b>%{text}</b><br>"
                            "Velocity: %{x:.1f} und/dia<br>"
                            "Stock: $%{y:,.0f}<br>"
                            "MOI: %{customdata[0]}m<br>"
                            "Linea: %{customdata[1]}<br>"
                            "Dias con venta: %{customdata[3]}<extra>Stock Muerto</extra>"
                        ),
                        name="Sin Venta",
                        showlegend=True,
                    ))

                # Selling stock — color by MOI
                if _mask_selling.any():
                    fig_scatter.add_trace(go.Scatter(
                        x=_scatter_plot.loc[_mask_selling, "VELOCITY"],
                        y=_scatter_plot.loc[_mask_selling, "STOCK_COSTO"],
                        mode="markers",
                        marker=dict(
                            size=_bubble_size[_mask_selling.values],
                            color=_scatter_plot.loc[_mask_selling, "MOI"],
                            colorscale=[
                                [0, "#065E8B"],
                                [0.3, "#23CED3"],
                                [0.5, "#f59e0b"],
                                [0.8, "#ef4444"],
                                [1, "#7f1d1d"],
                            ],
                            colorbar=dict(title="MOI (meses)", thickness=15, len=0.6),
                            opacity=0.85,
                            line=dict(width=1, color="#fff"),
                        ),
                        text=_labels[_mask_selling],
                        customdata=_hover_data[_mask_selling.values],
                        hovertemplate=(
                            "<b>%{text}</b><br>"
                            "Velocity: %{x:.1f} und/dia<br>"
                            "Stock: $%{y:,.0f}<br>"
                            "MOI: %{customdata[0]}m<br>"
                            "Linea: %{customdata[1]}<br>"
                            "Margen: %{customdata[2]:.0%}<br>"
                            "Dias con venta: %{customdata[3]}<br>"
                            "Unidades vendidas: %{customdata[4]:,.0f}<extra>Con Venta</extra>"
                        ),
                        name="Con Venta",
                        showlegend=True,
                    ))

                # Quadrant reference lines (medians of selling stock only)
                _selling_only = _scatter_plot[_mask_selling]
                if len(_selling_only) > 1:
                    _med_vel = _selling_only["VELOCITY"].median()
                    _med_stock = _selling_only["STOCK_COSTO"].median()
                    fig_scatter.add_hline(y=_med_stock, line_dash="dash", line_color="#666", line_width=1.5)
                    fig_scatter.add_vline(x=_med_vel, line_dash="dash", line_color="#666", line_width=1.5)

                    # Quadrant labels
                    _x_max = _scatter_plot["VELOCITY"].max()
                    _y_max = _scatter_plot["STOCK_COSTO"].max()
                    fig_scatter.add_annotation(
                        x=_med_vel + (_x_max - _med_vel) * 0.5, y=_y_max * 0.95,
                        text="LIQUIDAR<br>(alto stock, se vende)", showarrow=False,
                        font=dict(size=11, color=COLORS["status_at_risk"], family="Arial Black"),
                        bgcolor="rgba(255,255,255,0.95)", borderpad=5,
                    )
                    fig_scatter.add_annotation(
                        x=_med_vel * 0.3, y=_y_max * 0.95,
                        text="WRITE-OFF<br>(alto stock, no se vende)", showarrow=False,
                        font=dict(size=11, color=COLORS["status_critical"], family="Arial Black"),
                        bgcolor="rgba(255,255,255,0.95)", borderpad=5,
                    )
                    fig_scatter.add_annotation(
                        x=_med_vel + (_x_max - _med_vel) * 0.5, y=_med_stock * 0.3,
                        text="SE LIQUIDA SOLO<br>(bajo stock, se vende)", showarrow=False,
                        font=dict(size=11, color=COLORS["status_on_track"], family="Arial Black"),
                        bgcolor="rgba(255,255,255,0.95)", borderpad=5,
                    )
                    fig_scatter.add_annotation(
                        x=_med_vel * 0.3, y=_med_stock * 0.3,
                        text="NO PRIORITARIO<br>(bajo stock, no se vende)", showarrow=False,
                        font=dict(size=11, color=COLORS["medium_gray"], family="Arial Black"),
                        bgcolor="rgba(255,255,255,0.95)", borderpad=5,
                    )

                st.plotly_chart(fig_scatter, use_container_width=True)
                figures_to_export["Scatter Velocidad Venta"] = fig_scatter

                # Info message about dead stock
                if _n_no_sales > 0:
                    st.info(
                        f"{_n_no_sales} SKUs criticos ({_pct_no_sales_val:.1f}% del valor, "
                        f"{_fmt_cl(_val_no_sales)}) no registran venta desde Enero 2025. "
                        f"Estos son candidatos a write-off o donacion."
                    )

                # Expandable table: Top 20 by velocity
                with st.expander("Top 20 SKUs Criticos por Velocidad de Venta"):
                    _top20 = _scatter_plot[_scatter_plot["VELOCITY"] > 0].nlargest(20, "VELOCITY")
                    if not _top20.empty:
                        _top20_display = _top20.copy()
                        _top20_display["ACCION"] = np.where(
                            _top20_display["STOCK_COSTO"] >= _top20_display["STOCK_COSTO"].median(),
                            "Liquidar c/ descuento",
                            "Dejar que se liquide",
                        )
                        _disp_cols = ["SKU_PRODUCTO"]
                        if "NOM_PRODUCTO" in _top20_display.columns:
                            _disp_cols.append("NOM_PRODUCTO")
                        if "LINEA" in _top20_display.columns:
                            _disp_cols.append("LINEA")
                        _top20_display["MARGEN_PCT"] = _top20_display["AVG_MARGIN"] * 100
                        _disp_cols.extend(["STOCK_COSTO", "VELOCITY", "TOTAL_UNITS", "MARGEN_PCT", "N_DAYS_SOLD", "ACCION"])
                        _disp_cols = [c for c in _disp_cols if c in _top20_display.columns]
                        _top20_show = _top20_display[_disp_cols].copy()
                        if "STOCK_COSTO" in _top20_show.columns:
                            _top20_show["STOCK_COSTO"] = _top20_show["STOCK_COSTO"].apply(fmt_clp)
                        st.dataframe(
                            _top20_show,
                            column_config={
                                "STOCK_COSTO": st.column_config.TextColumn("Stock $"),
                                "VELOCITY": st.column_config.NumberColumn("Vel. (und/dia)", format="%.1f"),
                                "TOTAL_UNITS": st.column_config.NumberColumn("Und Vendidas", format="%d"),
                                "MARGEN_PCT": st.column_config.NumberColumn("Margen %", format="%.1f%%"),
                                "N_DAYS_SOLD": st.column_config.NumberColumn("Dias c/Venta", format="%d"),
                            },
                            use_container_width=True, hide_index=True,
                        )
                    else:
                        st.info("No hay SKUs criticos con venta registrada.")
        else:
            st.info("No hay datos de venta disponibles. Presione 'Actualizar Datos' para descargar.")

        # ---- CHART 14: Tendencia de Ingresos desde Stock Critico ----
        st.markdown("---")
        st.markdown("### 14. Tendencia de Ingresos desde Stock Critico")
        st.caption(
            "Ingresos mensuales generados por SKUs actualmente clasificados como criticos. "
            "Barras = venta neta mensual. Linea = cantidad de SKUs que registraron venta ese mes."
        )

        if not df_sales_crit.empty and "NETO_TOTAL" in df_sales_crit.columns:
            _trend = df_sales_crit.copy()
            _trend["PERIODO"] = _trend["FECHA"].dt.to_period("M")

            # Monthly aggregation
            _trend_monthly = _trend.groupby("PERIODO").agg(
                NETO_MENSUAL=("NETO_TOTAL", "sum"),
                UNIDADES_MENSUAL=("UNIDADES_VENDIDAS", "sum"),
            ).reset_index()

            # SKUs with sales per month
            _trend_pos = _trend[_trend["UNIDADES_VENDIDAS"] > 0]
            _skus_month = _trend_pos.groupby("PERIODO")["SKU_PRODUCTO"].nunique().reset_index()
            _skus_month.columns = ["PERIODO", "SKUS_CON_VENTA"]

            _trend_monthly = _trend_monthly.merge(_skus_month, on="PERIODO", how="left")
            _trend_monthly["SKUS_CON_VENTA"] = _trend_monthly["SKUS_CON_VENTA"].fillna(0).astype(int)
            _trend_monthly = _trend_monthly.sort_values("PERIODO")
            _trend_monthly["PERIODO_STR"] = _trend_monthly["PERIODO"].astype(str)

            if "APORTE_TOTAL" in _trend.columns:
                _aporte_month = _trend.groupby("PERIODO")["APORTE_TOTAL"].sum().reset_index()
                _aporte_month.columns = ["PERIODO", "APORTE_MENSUAL"]
                _trend_monthly = _trend_monthly.merge(_aporte_month, on="PERIODO", how="left")
                _trend_monthly["APORTE_MENSUAL"] = _trend_monthly["APORTE_MENSUAL"].fillna(0)

            if len(_trend_monthly) >= 2:
                fig_trend = go.Figure(layout=dorel_layout(
                    title=dict(text="Ingresos Mensuales desde Stock Critico", font_size=15, x=0.5),
                    yaxis=dict(title="Venta Neta ($)", gridcolor="#ECECEC"),
                    yaxis2=dict(
                        title="SKUs con Venta",
                        overlaying="y",
                        side="right",
                        showgrid=False,
                    ),
                    height=450,
                    legend=dict(x=0.01, y=0.99, xanchor="left"),
                ))

                fig_trend.add_trace(go.Bar(
                    x=_trend_monthly["PERIODO_STR"],
                    y=_trend_monthly["NETO_MENSUAL"],
                    marker_color=COLORS["status_at_risk"],
                    marker_opacity=0.8,
                    name="Venta Neta",
                    yaxis="y",
                    text=[_fmt_cl(v) for v in _trend_monthly["NETO_MENSUAL"]],
                    textposition="outside",
                    textfont=dict(size=9),
                    hovertemplate="Periodo: %{x}<br>Venta Neta: $%{y:,.0f}<extra></extra>",
                ))

                fig_trend.add_trace(go.Scatter(
                    x=_trend_monthly["PERIODO_STR"],
                    y=_trend_monthly["SKUS_CON_VENTA"],
                    mode="lines+markers+text",
                    line=dict(color=COLORS["primary"], width=2.5),
                    marker=dict(size=8, color=COLORS["primary"]),
                    name="SKUs con Venta",
                    yaxis="y2",
                    text=_trend_monthly["SKUS_CON_VENTA"].astype(str),
                    textposition="top center",
                    textfont=dict(size=9, color=COLORS["primary"]),
                    hovertemplate="Periodo: %{x}<br>SKUs con venta: %{y}<extra></extra>",
                ))

                st.plotly_chart(fig_trend, use_container_width=True)
                figures_to_export["Tendencia Ingresos Criticos"] = fig_trend

                # Summary metrics below chart
                _avg_neto = _trend_monthly["NETO_MENSUAL"].mean()
                _total_year = _trend_monthly["NETO_MENSUAL"].sum()
                _last_3 = _trend_monthly["NETO_MENSUAL"].tail(3).mean() if len(_trend_monthly) >= 3 else _avg_neto
                _last_val = _trend_monthly["NETO_MENSUAL"].iloc[-1]
                _delta_pct = ((_last_val / _last_3 - 1) * 100) if _last_3 > 0 else 0

                _m14a, _m14b, _m14c = st.columns(3)
                _m14a.metric(
                    "Ultimo Mes vs Promedio 3m",
                    _fmt_cl(_last_val),
                    delta=f"{_delta_pct:+.1f}%",
                    delta_color="normal",
                )
                _m14b.metric("Promedio Mensual", _fmt_cl(_avg_neto))
                _m14c.metric("Total Acumulado", _fmt_cl(_total_year))
            else:
                st.info("Se necesitan al menos 2 meses de datos de venta para la tendencia.")
        else:
            st.info("No hay datos de venta disponibles. Presione 'Actualizar Datos' para descargar.")

        # ---- CHART 15: Mapa Geografico de Stock Critico ----
        st.markdown("---")
        st.markdown("### 15. Mapa Geografico de Stock Critico por Tienda")
        st.caption(
            "Cada circulo representa una tienda. **Color**: % del inventario total clasificado como critico. "
            "**Tamaño**: Stock critico en $. Requiere que los datos hayan sido actualizados "
            "con la nueva version del query (incluye lat/lon)."
        )

        try:
            _td = cq.tienda_dim(conn)
            _td = _td[_td["LATITUD"].notna() & (_td["LATITUD"] != 0) &
                       _td["LONGITUD"].notna() & (_td["LONGITUD"] != 0)].copy() if not _td.empty else _td

            if not _td.empty and not df_crit_det.empty and "DESCRIPCION_SUCURSAL" in df_crit_det.columns:
                # Stock critico por tienda
                _sc_per_store = (
                    df_crit_det[df_crit_det["CANAL_DETALLE"] != "CD INMOVILIZADO"]
                    .groupby("DESCRIPCION_SUCURSAL", as_index=False)
                    .agg(
                        STOCK_CRIT=("STOCK_COSTO", "sum"),
                        N_SKUS_CRIT=("SKU_PRODUCTO", "nunique"),
                    )
                )
                # Total por tienda (desde store_totals dict)
                _sc_per_store["STOCK_TOTAL"] = (
                    _sc_per_store["DESCRIPCION_SUCURSAL"].map(store_totals).fillna(0)
                )
                _sc_per_store["PCT_CRIT"] = np.where(
                    _sc_per_store["STOCK_TOTAL"] > 0,
                    _sc_per_store["STOCK_CRIT"] / _sc_per_store["STOCK_TOTAL"] * 100,
                    0,
                )

                # Merge con tienda_dim por descripcion_sucursal
                _td_geo = _td[["DESCRIPCION_SUCURSAL", "LATITUD", "LONGITUD",
                                "SUPERVISOR", "CLUSTER", "MTS2"]].copy()
                _map_df = _sc_per_store.merge(_td_geo, on="DESCRIPCION_SUCURSAL", how="inner")
                _map_df = _map_df[(_map_df["LATITUD"] != 0) & (_map_df["LONGITUD"] != 0)].copy()

                if not _map_df.empty:
                    # Tamaño de burbuja proporcional al stock critico (escala raíz cuadrada)
                    _max_sc = _map_df["STOCK_CRIT"].max()
                    _map_df["_SIZE"] = (
                        (_map_df["STOCK_CRIT"] / max(_max_sc, 1)) ** 0.5 * 30 + 8
                    ).clip(upper=45)

                    _hover_map = np.column_stack([
                        _map_df["N_SKUS_CRIT"].values,
                        _map_df["STOCK_CRIT"].values,
                        _map_df["STOCK_TOTAL"].values,
                        _map_df["SUPERVISOR"].fillna("N/D").values,
                        _map_df["CLUSTER"].fillna("N/D").values,
                        _map_df["MTS2"].fillna(0).values,
                    ])

                    fig_map = go.Figure()
                    fig_map.add_trace(go.Scattermapbox(
                        lat=_map_df["LATITUD"],
                        lon=_map_df["LONGITUD"],
                        mode="markers",
                        marker=go.scattermapbox.Marker(
                            size=_map_df["_SIZE"],
                            color=_map_df["PCT_CRIT"],
                            colorscale=[
                                [0.0,  "#43A047"],
                                [0.25, "#A5D6A7"],
                                [0.5,  "#F9A825"],
                                [0.75, "#FB8C00"],
                                [1.0,  "#E53935"],
                            ],
                            cmin=0,
                            cmax=100,
                            showscale=True,
                            colorbar=dict(
                                title="% Critico",
                                thickness=14,
                                len=0.6,
                            ),
                            opacity=0.85,
                        ),
                        text=_map_df["DESCRIPCION_SUCURSAL"],
                        customdata=_hover_map,
                        hovertemplate=(
                            "<b>%{text}</b><br>"
                            "SKUs Criticos: %{customdata[0]:,.0f}<br>"
                            "Stock Critico: $%{customdata[1]:,.0f}<br>"
                            "Stock Total: $%{customdata[2]:,.0f}<br>"
                            "% Critico: %{marker.color:.1f}%<br>"
                            "Supervisor: %{customdata[3]}<br>"
                            "Cluster: %{customdata[4]}<br>"
                            "m²: %{customdata[5]:,.0f}<extra></extra>"
                        ),
                    ))
                    fig_map.update_layout(
                        mapbox=dict(
                            style="open-street-map",
                            center=dict(
                                lat=_map_df["LATITUD"].mean(),
                                lon=_map_df["LONGITUD"].mean(),
                            ),
                            zoom=5,
                        ),
                        height=550,
                        margin=dict(l=0, r=0, t=10, b=0),
                        paper_bgcolor="#FFFFFF",
                    )
                    st.plotly_chart(fig_map, use_container_width=True)
                    figures_to_export["Mapa Stock Critico"] = fig_map

                    # Tabla resumen bajo el mapa
                    with st.expander("Detalle por tienda (mapa)"):
                        _map_tbl = _map_df[[
                            "DESCRIPCION_SUCURSAL", "SUPERVISOR", "CLUSTER",
                            "N_SKUS_CRIT", "STOCK_CRIT", "STOCK_TOTAL", "PCT_CRIT", "MTS2",
                        ]].sort_values("PCT_CRIT", ascending=False).reset_index(drop=True).copy()
                        if "STOCK_CRIT" in _map_tbl.columns:
                            _map_tbl["STOCK_CRIT"] = _map_tbl["STOCK_CRIT"].apply(fmt_clp)
                        if "STOCK_TOTAL" in _map_tbl.columns:
                            _map_tbl["STOCK_TOTAL"] = _map_tbl["STOCK_TOTAL"].apply(fmt_clp)
                        st.dataframe(
                            _map_tbl,
                            use_container_width=True,
                            column_config={
                                "DESCRIPCION_SUCURSAL": "Tienda",
                                "SUPERVISOR": "Supervisor",
                                "CLUSTER": "Cluster",
                                "N_SKUS_CRIT": st.column_config.NumberColumn("SKUs Criticos", format="%d"),
                                "STOCK_CRIT": st.column_config.TextColumn("Stock Critico $"),
                                "STOCK_TOTAL": st.column_config.TextColumn("Stock Total $"),
                                "PCT_CRIT": st.column_config.NumberColumn("% Critico", format="%.1f%%"),
                                "MTS2": st.column_config.NumberColumn("m²", format="%d"),
                            },
                        )
                else:
                    st.info("Las tiendas no tienen coordenadas geograficas en dt_tienda.")
            else:
                st.info(
                    "Sin datos de ubicacion o sin stock critico en tiendas. "
                    "Presiona 'Actualizar Datos' para incluir lat/lon en el CSV."
                )
        except Exception as _e_map:
            st.warning(f"Mapa no disponible: {_e_map}")

        # ---- Excel Export ----
        st.markdown("---")
        st.subheader("Exportar Detalle (Excel)")
        if st.button("Generar Informe Excel con Unidades"):
            with lottie_spinner("export"):
                try:
                    cond_moi = (df_last_m["MOI"] >= 12) | (df_last_m["MOI"].isna())
                    cond_ant = _cond_ant_critico(df_last_m["ANTIGUEDAD_MESES"], df_last_m["MOI"])
                    df_crit_650 = df_last_m[cond_moi & cond_ant].copy()
                    df_crit_650["STOCK_UNIDADES"] = 0

                    if os.path.exists(AUTO_SALES_FILE):
                        df_sales_exp = pd.read_csv(
                            AUTO_SALES_FILE,
                            usecols=["SKU_PRODUCTO", "UNIDADES_VENDIDAS", "NETO_TOTAL", "MARGEN"],
                        )
                        sales_agg = df_sales_exp.groupby("SKU_PRODUCTO").agg(
                            TOTAL_UNITS=("UNIDADES_VENDIDAS", "sum"),
                            TOTAL_NETO=("NETO_TOTAL", "sum"),
                            AVG_MARGIN=("MARGEN", "mean"),
                        ).reset_index()
                        sales_agg = sales_agg[sales_agg["TOTAL_UNITS"] > 0].copy()
                        sales_agg["AVG_PRICE"] = sales_agg["TOTAL_NETO"] / sales_agg["TOTAL_UNITS"]
                        sales_agg["EST_UNIT_COST"] = sales_agg["AVG_PRICE"] * (1 - sales_agg["AVG_MARGIN"])
                        sales_agg = sales_agg[sales_agg["EST_UNIT_COST"] > 0]

                        df_crit_650 = df_crit_650.merge(
                            sales_agg[["SKU_PRODUCTO", "EST_UNIT_COST"]], on="SKU_PRODUCTO", how="left"
                        )
                        idx_calc = df_crit_650["EST_UNIT_COST"] > 0
                        df_crit_650.loc[idx_calc, "STOCK_UNIDADES"] = (
                            (df_crit_650.loc[idx_calc, "STOCK_COSTO"] / df_crit_650.loc[idx_calc, "EST_UNIT_COST"])
                            .fillna(0)
                            .round()
                            .astype(int)
                        )

                    cols_export = ["SKU_PRODUCTO", "NOM_PRODUCTO", "STOCK_UNIDADES", "STOCK_COSTO", "MOI", "ANTIGUEDAD_MESES"]
                    if "EST_UNIT_COST" in df_crit_650.columns:
                        cols_export.append("EST_UNIT_COST")

                    output = BytesIO()
                    df_crit_650[cols_export].to_excel(output, index=False, engine="openpyxl")
                    output.seek(0)

                    st.download_button(
                        label="Descargar Excel Stock Critico",
                        data=output,
                        file_name="detalle_stock_critico_unidades.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                    st.success(f"Reporte generado: {len(df_crit_650)} SKUs.")
                except Exception as e:
                    st.error(f"Error generando excel: {e}")


    except Exception as e:
        st.error(f"Error en el proceso: {e}")
