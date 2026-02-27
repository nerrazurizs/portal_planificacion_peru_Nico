"""Elasticidad Precio-Demanda - Analisis de sensibilidad de precios por SKU."""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from scipy import stats

from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format
from utils.ui_animations import lottie_spinner
from utils.export import download_buttons
from config import COLORS, dorel_layout, apply_pm_filter


# ============================================================================
# CONSTANTS
# ============================================================================

# Elasticity classification thresholds
ELAST_SEGMENTS = {
    "MUY ELASTICO": {"min": -999, "max": -1.5, "color": "#C62828", "icon": "🔴"},
    "ELASTICO":     {"min": -1.5, "max": -1.0, "color": "#E53935", "icon": "🟠"},
    "UNITARIO":     {"min": -1.0, "max": -0.7, "color": "#FB8C00", "icon": "🟡"},
    "INELASTICO":   {"min": -0.7, "max": -0.3, "color": "#43A047", "icon": "🟢"},
    "MUY INELASTICO": {"min": -0.3, "max": 0, "color": "#1B5E20", "icon": "🟩"},
    "ANOMALO (+)":  {"min": 0, "max": 999, "color": "#9E9E9E", "icon": "⚪"},
}

SEGMENT_ORDER = ["MUY ELASTICO", "ELASTICO", "UNITARIO", "INELASTICO", "MUY INELASTICO", "ANOMALO (+)"]

CANAL_MAP = {
    "MINOR": "TIENDA",
    "MAYOR": "MAYORISTA",
    "ETAIL": "ETAIL",
    "TIENDA": "TIENDA",
    "MAYORISTA": "MAYORISTA",
}


# ============================================================================
# DATA LOADING
# ============================================================================

def _load_data(conn):
    """Load monthly sales+price data and product master (centralized cache)."""
    with lottie_spinner("snowflake"):
        ventas = cq.ventas_mensual_precio(conn)
        maestra = cq.maestra(conn)
    return ventas, maestra


# ============================================================================
# ELASTICITY CALCULATION
# ============================================================================

def _calculate_elasticity(ventas, maestra):
    """Calculate price elasticity of demand per SKU using log-log regression.

    Method: For each SKU (with MIX_OFICIAL = 'MIX'), we regress:
        ln(cantidad) = alpha + beta * ln(precio)
    where beta is the price elasticity of demand.

    Requirements:
    - At least 6 months of data with positive price and quantity
    - Only SKUs classified as MIX_OFICIAL = 'MIX' (or similar active mix)
    """
    if ventas.empty:
        return pd.DataFrame()

    # Standardize channel
    if "COD_CANAL" in ventas.columns:
        ventas["CANAL_STD"] = ventas["COD_CANAL"].str.strip().str.upper().map(CANAL_MAP)
        ventas = ventas[ventas["CANAL_STD"].notna()]

    # Filter: positive price and quantity
    df = ventas[
        (ventas["CANTIDAD"] > 0) &
        (ventas["PRECIO_PROMEDIO"] > 0) &
        (ventas["PRECIO_PROMEDIO"].notna())
    ].copy()

    if df.empty:
        return pd.DataFrame()

    # Log transforms
    df["LN_PRECIO"] = np.log(df["PRECIO_PROMEDIO"])
    df["LN_CANTIDAD"] = np.log(df["CANTIDAD"])

    # Calculate elasticity per SKU (across all channels pooled)
    results = []
    min_obs = 6  # Minimum number of monthly observations

    for sku, grp in df.groupby("SKU_PRODUCTO"):
        if len(grp) < min_obs:
            continue

        try:
            slope, intercept, r_value, p_value, std_err = stats.linregress(
                grp["LN_PRECIO"].values, grp["LN_CANTIDAD"].values
            )

            results.append({
                "SKU_PRODUCTO": sku,
                "ELASTICIDAD": slope,
                "R_SQUARED": r_value ** 2,
                "P_VALUE": p_value,
                "STD_ERR": std_err,
                "N_OBS": len(grp),
                "PRECIO_PROM": grp["PRECIO_PROMEDIO"].mean(),
                "PRECIO_MIN": grp["PRECIO_PROMEDIO"].min(),
                "PRECIO_MAX": grp["PRECIO_PROMEDIO"].max(),
                "PRECIO_CV": grp["PRECIO_PROMEDIO"].std() / grp["PRECIO_PROMEDIO"].mean()
                    if grp["PRECIO_PROMEDIO"].mean() > 0 else 0,
                "CANTIDAD_PROM_MES": grp["CANTIDAD"].mean(),
                "VN_PROM_MES": grp["NETO"].mean(),
                "APORTE_TOTAL": grp["APORTE"].sum(),
            })
        except Exception:
            continue

    if not results:
        return pd.DataFrame()

    elast_df = pd.DataFrame(results)

    # Classify elasticity
    conditions = [
        elast_df["ELASTICIDAD"] < -1.5,
        elast_df["ELASTICIDAD"] < -1.0,
        elast_df["ELASTICIDAD"] < -0.7,
        elast_df["ELASTICIDAD"] < -0.3,
        elast_df["ELASTICIDAD"] < 0,
    ]
    choices = ["MUY ELASTICO", "ELASTICO", "UNITARIO", "INELASTICO", "MUY INELASTICO"]
    elast_df["SEGMENTO"] = np.select(conditions, choices, default="ANOMALO (+)")

    # Confidence flag: significant regression with enough variability
    elast_df["CONFIABLE"] = (
        (elast_df["P_VALUE"] < 0.1) &
        (elast_df["R_SQUARED"] > 0.1) &
        (elast_df["PRECIO_CV"] > 0.05) &
        (elast_df["N_OBS"] >= min_obs)
    )

    # Merge maestra
    maestra_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                    "SKU_NOM_PRODUCTO", "MIX_OFICIAL", "PROCEDENCIA"]
    available = [c for c in maestra_cols if c in maestra.columns]
    if available:
        maestra_dedup = maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
        elast_df = elast_df.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")

    return elast_df


def _generate_forecast_warnings(elast_df, conn):
    """Generate warnings for elastic SKUs whose forecast prices differ significantly
    from historical prices, suggesting forecast may be inaccurate.

    Logic:
    - If a product is MUY ELASTICO or ELASTICO (|e| > 1.0) and the forecast
      uses a price that deviates >10% from the historical average, the forecast
      volume may be significantly off.
    - Warning severity based on: elasticity * price_change_pct = expected volume impact
    """
    if elast_df.empty:
        return pd.DataFrame()

    # Focus on elastic products with reliable estimates
    elastic = elast_df[
        (elast_df["SEGMENTO"].isin(["MUY ELASTICO", "ELASTICO"])) &
        (elast_df["CONFIABLE"])
    ].copy()

    if elastic.empty:
        return pd.DataFrame()

    # Check if projection data is available in session_state
    proy_df = st.session_state.get("df_proy")
    if proy_df is None or not isinstance(proy_df, pd.DataFrame):
        return pd.DataFrame()

    # Get forecast prices from projection
    precio_cols = [c for c in proy_df.columns if c.startswith("PRECIO_USADO_")]
    if not precio_cols:
        return pd.DataFrame()

    # Get first period forecast prices per SKU
    if "PERIODO" in proy_df.columns:
        first_period = proy_df.groupby("SKU_PRODUCTO").first().reset_index()
    else:
        first_period = proy_df.drop_duplicates(subset=["SKU_PRODUCTO"])

    fc_prices = first_period[["SKU_PRODUCTO"] + precio_cols].copy()

    # Average forecast price across channels (weighted or simple)
    price_vals = fc_prices[precio_cols].replace(0, np.nan)
    fc_prices["PRECIO_FC_PROM"] = price_vals.mean(axis=1)

    # Merge with elasticity data
    warnings = elastic.merge(
        fc_prices[["SKU_PRODUCTO", "PRECIO_FC_PROM"]],
        on="SKU_PRODUCTO",
        how="inner",
    )

    if warnings.empty:
        return pd.DataFrame()

    # Calculate price deviation
    warnings["DESV_PRECIO_PCT"] = (
        (warnings["PRECIO_FC_PROM"] - warnings["PRECIO_PROM"]) / warnings["PRECIO_PROM"] * 100
    )

    # Expected volume impact: elasticity * price_change = demand_change
    # e.g., elasticity=-2.0, price_change=+10% => demand_change=-20%
    warnings["IMPACTO_VOL_PCT"] = warnings["ELASTICIDAD"] * (warnings["DESV_PRECIO_PCT"] / 100) * 100

    # VN mensual en riesgo
    warnings["VN_MES_RIESGO"] = abs(warnings["IMPACTO_VOL_PCT"] / 100) * warnings["VN_PROM_MES"]

    # Flag only significant deviations (>5% price change)
    warnings = warnings[abs(warnings["DESV_PRECIO_PCT"]) > 5].copy()

    # Severity
    warnings["SEVERIDAD"] = np.select(
        [
            abs(warnings["IMPACTO_VOL_PCT"]) > 30,
            abs(warnings["IMPACTO_VOL_PCT"]) > 15,
        ],
        ["ALTO", "MEDIO"],
        default="BAJO",
    )

    warnings = warnings.sort_values("VN_MES_RIESGO", ascending=False)
    return warnings


# ============================================================================
# DASHBOARD CHARTS
# ============================================================================

def _render_kpis(df):
    """KPI summary cards."""
    total = len(df)
    confiable = df["CONFIABLE"].sum()
    elasticos = df[df["SEGMENTO"].isin(["MUY ELASTICO", "ELASTICO"])].shape[0]
    inelasticos = df[df["SEGMENTO"].isin(["INELASTICO", "MUY INELASTICO"])].shape[0]
    elast_median = df.loc[df["CONFIABLE"], "ELASTICIDAD"].median() if confiable > 0 else 0

    cols = st.columns(5)
    data = [
        ("Total SKUs", f"{total:,}", COLORS["primary"]),
        ("Confiables", f"{int(confiable):,}", COLORS["tertiary_blue"]),
        ("Elasticos", f"{elasticos:,}", "#E53935"),
        ("Inelasticos", f"{inelasticos:,}", "#43A047"),
        ("Elasticidad Med.", f"{elast_median:.2f}", COLORS["secondary"]),
    ]

    for col, (label, value, color) in zip(cols, data):
        with col:
            st.html(f"""
            <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                         border-top:4px solid {color};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                <div style='font-size:1.8rem;font-weight:bold;color:{color}'>{value}</div>
                <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{label}</div>
            </div>""")

    st.html("<br>")


def _render_distribution(df):
    """Distribution of elasticity segments."""
    counts = df.groupby("SEGMENTO", as_index=False).agg(
        COUNT=("SKU_PRODUCTO", "count"),
        APORTE=("APORTE_TOTAL", "sum"),
    )

    cat_order = [s for s in SEGMENT_ORDER if s in counts["SEGMENTO"].values]

    # Sort counts by cat_order
    counts["_sort"] = counts["SEGMENTO"].map({s: i for i, s in enumerate(cat_order)})
    counts = counts.sort_values("_sort").drop(columns="_sort")

    fig = go.Figure()
    for _, row in counts.iterrows():
        seg = row["SEGMENTO"]
        fig.add_trace(go.Bar(
            x=[seg], y=[row["COUNT"]],
            marker_color=ELAST_SEGMENTS[seg]["color"],
            name=seg, showlegend=False,
            hovertemplate=(
                f"<b>{seg}</b><br>"
                f"SKUs: {int(row['COUNT']):,}<br>"
                f"Aporte Total: ${row['APORTE']:,.0f}"
                "<extra></extra>"
            ),
        ))
    fig.update_layout(**dorel_layout(
        title="Distribucion de SKUs por Segmento de Elasticidad",
        height=350,
        xaxis=dict(title="Segmento de Elasticidad", categoryorder="array", categoryarray=cat_order),
        yaxis=dict(title="# SKUs"),
    ))
    st.plotly_chart(fig, use_container_width=True)


def _render_histogram(df):
    """Histogram of elasticity values."""
    reliable = df[df["CONFIABLE"]].copy()
    if reliable.empty:
        st.info("No hay SKUs con elasticidad confiable para el histograma.")
        return

    # Clip extreme values for visualization
    reliable["ELAST_CLIP"] = reliable["ELASTICIDAD"].clip(-5, 2)

    fig = go.Figure()
    for seg in SEGMENT_ORDER:
        seg_data = reliable.loc[reliable["SEGMENTO"] == seg, "ELAST_CLIP"]
        if seg_data.empty:
            continue
        fig.add_trace(go.Histogram(
            x=seg_data, nbinsx=40, name=seg,
            marker_color=ELAST_SEGMENTS[seg]["color"],
            opacity=0.8,
            hovertemplate="Segmento: %{fullData.name}<br>Elasticidad: %{x}<br>SKUs: %{y}<extra></extra>",
        ))
    fig.add_vline(x=-1.0, line_dash="dash", line_color="black",
                  annotation_text="Elasticidad Unitaria", annotation_position="top left")
    fig.update_layout(**dorel_layout(
        title="Distribucion de Elasticidad (solo estimaciones confiables)",
        height=350, barmode="stack",
        xaxis=dict(title="Elasticidad"),
        yaxis=dict(title="# SKUs"),
    ))
    st.plotly_chart(fig, use_container_width=True)


def _render_scatter(df):
    """Scatter: Elasticity vs Revenue, colored by segment."""
    reliable = df[df["CONFIABLE"]].copy()
    if reliable.empty:
        return

    x_min = min(-5, reliable["ELASTICIDAD"].quantile(0.01))
    # Clip para evitar valores negativos en size (Plotly requiere >= 0)
    reliable["_APORTE_SIZE"] = reliable["APORTE_TOTAL"].clip(lower=0)
    aporte_max = reliable["_APORTE_SIZE"].max() if reliable["_APORTE_SIZE"].max() > 0 else 1
    sizeref_val = 2.0 * aporte_max / (30 ** 2)

    fig = go.Figure()
    for seg in SEGMENT_ORDER:
        seg_df = reliable[reliable["SEGMENTO"] == seg]
        if seg_df.empty:
            continue
        fig.add_trace(go.Scatter(
            x=seg_df["ELASTICIDAD"], y=seg_df["VN_PROM_MES"],
            mode="markers", name=seg,
            marker=dict(
                color=ELAST_SEGMENTS[seg]["color"],
                size=seg_df["_APORTE_SIZE"],
                sizemode="area", sizeref=sizeref_val, sizemin=4,
                opacity=0.7, line=dict(width=0.5, color="white"),
            ),
            customdata=np.column_stack([
                seg_df["SKU_PRODUCTO"], seg_df["PRECIO_PROM"],
                seg_df["R_SQUARED"], seg_df["SEGMENTO"],
            ]),
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "Elasticidad: %{x:.2f}<br>"
                "VN/Mes: $%{y:,.0f}<br>"
                "Precio Prom: $%{customdata[1]:,.0f}<br>"
                "R2: %{customdata[2]:.2f}<br>"
                "Segmento: %{customdata[3]}"
                "<extra></extra>"
            ),
        ))
    fig.add_vline(x=-1.0, line_dash="dash", line_color="black",
                  annotation_text="Elasticidad Unitaria", annotation_position="top left")
    fig.update_layout(**dorel_layout(
        title="Elasticidad vs VN Mensual (tamano = aporte)",
        height=400,
        xaxis=dict(title="Elasticidad", range=[x_min, 2]),
        yaxis=dict(title="VN Prom Mensual ($)", type="log"),
    ))
    st.plotly_chart(fig, use_container_width=True)


def _render_by_dimension(df):
    """Box/strip plot of elasticity by Area/Linea/Marca."""
    dim = st.radio("Dimension", ["AREA", "LINEA", "MARCA"], horizontal=True, key="elast_dim_radio")
    if dim not in df.columns:
        st.info(f"Columna {dim} no disponible.")
        return

    reliable = df[df["CONFIABLE"]].copy()
    if reliable.empty:
        return

    # Top 15 categories by count
    top_cats = reliable[dim].value_counts().head(15).index.tolist()
    plot_df = reliable[reliable[dim].isin(top_cats)]

    fig = go.Figure()
    for seg in SEGMENT_ORDER:
        seg_df = plot_df[plot_df["SEGMENTO"] == seg]
        if seg_df.empty:
            continue
        fig.add_trace(go.Scatter(
            x=seg_df["ELASTICIDAD"], y=seg_df[dim],
            mode="markers", name=seg,
            marker=dict(color=ELAST_SEGMENTS[seg]["color"], size=7, opacity=0.6),
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "Elasticidad: %{x:.2f}<br>"
                "Segmento: %{customdata[1]}"
                "<extra></extra>"
            ),
            customdata=np.column_stack([seg_df["SKU_PRODUCTO"], seg_df["SEGMENTO"]]),
        ))

    # Median marks per category
    medians = plot_df.groupby(dim)["ELASTICIDAD"].median().reset_index()
    fig.add_trace(go.Scatter(
        x=medians["ELASTICIDAD"], y=medians[dim],
        mode="markers", name="Mediana",
        marker=dict(symbol="line-ns-open", color="black", size=14, line=dict(width=3)),
        hovertemplate="Mediana: %{x:.2f}<extra></extra>",
    ))

    fig.add_vline(x=-1.0, line_dash="dash", line_color="black",
                  annotation_text="Elasticidad Unitaria", annotation_position="top left")
    fig.update_layout(**dorel_layout(
        title=f"Elasticidad por {dim.title()} (Top 15)",
        height=450,
        xaxis=dict(title="Elasticidad", range=[-5, 2]),
        yaxis=dict(title="", categoryorder="array", categoryarray=list(reversed(top_cats))),
    ))
    st.plotly_chart(fig, use_container_width=True)


def _render_warnings(warnings_df):
    """Render forecast warnings for elastic products."""
    if warnings_df.empty:
        st.info(
            "No hay warnings de forecast. Esto puede deberse a: "
            "(1) No se ha generado una proyeccion aun, "
            "(2) Los precios del forecast son consistentes con los historicos, "
            "(3) No hay SKUs elasticos con estimacion confiable."
        )
        return

    st.markdown("### ⚠️ Warnings de Forecast por Elasticidad")
    st.caption(
        "SKUs elasticos cuyo precio de forecast difiere >5% del promedio historico. "
        "El impacto estimado en volumen se calcula como: elasticidad x % cambio precio."
    )

    # KPI row
    c1, c2, c3 = st.columns(3)
    n_warnings = len(warnings_df)
    alto = (warnings_df["SEVERIDAD"] == "ALTO").sum()
    vn_riesgo = warnings_df["VN_MES_RIESGO"].sum()

    with c1:
        st.metric("Warnings", f"{n_warnings}")
    with c2:
        st.metric("Severidad Alta", f"{alto}")
    with c3:
        st.metric("VN Mensual en Riesgo", f"${human_format(vn_riesgo)}")

    # Warning chart
    top_w = warnings_df.head(20).copy()
    nombre_col = "SKU_NOM_PRODUCTO" if "SKU_NOM_PRODUCTO" in top_w.columns else "SKU_PRODUCTO"
    top_w["LABEL"] = top_w["SKU_PRODUCTO"].astype(str) + " - " + top_w[nombre_col].astype(str)
    top_w["LABEL"] = top_w["LABEL"].str[:50]

    sev_colors = {"ALTO": "#C62828", "MEDIO": "#FB8C00", "BAJO": "#FFE082"}

    # Sort by impact for proper bar ordering (largest at top)
    top_w = top_w.sort_values("IMPACTO_VOL_PCT", ascending=True)

    fig = go.Figure()
    for sev in ["ALTO", "MEDIO", "BAJO"]:
        sev_df = top_w[top_w["SEVERIDAD"] == sev]
        if sev_df.empty:
            continue
        fig.add_trace(go.Bar(
            x=sev_df["IMPACTO_VOL_PCT"], y=sev_df["LABEL"],
            orientation="h", name=sev,
            marker_color=sev_colors[sev],
            customdata=np.column_stack([
                sev_df["SKU_PRODUCTO"], sev_df["ELASTICIDAD"],
                sev_df["PRECIO_PROM"], sev_df["PRECIO_FC_PROM"],
                sev_df["DESV_PRECIO_PCT"], sev_df["VN_MES_RIESGO"],
            ]),
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "Elasticidad: %{customdata[1]:.2f}<br>"
                "Precio Hist.: $%{customdata[2]:,.0f}<br>"
                "Precio FC: $%{customdata[3]:,.0f}<br>"
                "Desv. Precio: %{customdata[4]:.1f}%<br>"
                "Impacto Vol: %{x:.1f}%<br>"
                "VN Riesgo/Mes: $%{customdata[5]:,.0f}"
                "<extra></extra>"
            ),
        ))
    fig.update_layout(**dorel_layout(
        title="Top 20 SKUs con Mayor Impacto en Forecast",
        height=500,
        xaxis=dict(title="Impacto Estimado en Volumen (%)"),
        yaxis=dict(title=""),
    ))
    st.plotly_chart(fig, use_container_width=True)

    # Detail table
    warn_cols = ["SKU_PRODUCTO", "SEVERIDAD", "ELASTICIDAD", "SEGMENTO",
                 "PRECIO_PROM", "PRECIO_FC_PROM", "DESV_PRECIO_PCT",
                 "IMPACTO_VOL_PCT", "VN_MES_RIESGO", "VN_PROM_MES"]
    for c in ["AREA", "LINEA", "MARCA", "SKU_NOM_PRODUCTO"]:
        if c in warnings_df.columns:
            warn_cols.insert(1, c)
    warn_cols = [c for c in warn_cols if c in warnings_df.columns]

    st.dataframe(warnings_df[warn_cols].reset_index(drop=True), use_container_width=True, height=400)
    download_buttons(warnings_df[warn_cols], "warnings_elasticidad_forecast")


# ============================================================================
# MAIN RENDER
# ============================================================================

def render_elasticidad(conn):
    """Main entry point for price elasticity analysis module."""
    st.html("<h2 class='sub-header'>Elasticidad Precio-Demanda</h2>")
    st.caption(
        "Estimacion de elasticidad precio-demanda por SKU usando regresion log-log sobre 24 meses de datos. "
        "Solo SKUs MIX_OFICIAL = 'MIX' con al menos 6 observaciones mensuales."
    )

    # Load/cache data
    if st.button("Actualizar Datos", key="btn_refresh_elast"):
        st.session_state.pop("elast_data", None)
        st.session_state.pop("elast_warnings", None)

    if "elast_data" not in st.session_state:
        ventas, maestra = _load_data(conn)

        # Filter MIX_OFICIAL = 'MIX' from maestra
        if "MIX_OFICIAL" in maestra.columns:
            mix_skus = set(
                maestra.loc[
                    maestra["MIX_OFICIAL"].str.strip().str.upper() == "MIX",
                    "SKU_PRODUCTO",
                ]
            )
            if mix_skus:
                ventas = ventas[ventas["SKU_PRODUCTO"].isin(mix_skus)]
                st.info(f"Analizando {len(mix_skus):,} SKUs con MIX_OFICIAL = 'MIX'")
            else:
                st.warning("No se encontraron SKUs con MIX_OFICIAL = 'MIX'. Analizando todos los SKUs.")
        else:
            st.warning("Columna MIX_OFICIAL no encontrada en maestra. Analizando todos los SKUs.")

        elast_df = _calculate_elasticity(ventas, maestra)
        st.session_state["elast_data"] = elast_df

        # Generate forecast warnings
        warnings = _generate_forecast_warnings(elast_df, conn)
        st.session_state["elast_warnings"] = warnings

    df = st.session_state["elast_data"].copy()
    df = apply_pm_filter(df)
    warnings_df = st.session_state.get("elast_warnings", pd.DataFrame()).copy()

    if df.empty:
        st.warning("No se pudo calcular elasticidad. Verifica que hay datos de ventas con variacion de precios.")
        return

    # ── Filters ──────────────────────────────────────────────────────
    st.markdown("### Filtros")
    fc1, fc2, fc3, fc4 = st.columns(4)

    areas = sorted(df["AREA"].dropna().unique()) if "AREA" in df.columns else []
    lineas_all = sorted(df["LINEA"].dropna().unique()) if "LINEA" in df.columns else []
    marcas_all = sorted(df["MARCA"].dropna().unique()) if "MARCA" in df.columns else []

    with fc1:
        sel_area = st.multiselect("Area", areas, key="elast_area")
    with fc2:
        _m = df.copy()
        if sel_area:
            _m = _m[_m["AREA"].isin(sel_area)]
        lineas_filt = sorted(_m["LINEA"].dropna().unique()) if "LINEA" in _m.columns else lineas_all
        sel_linea = st.multiselect("Linea", lineas_filt, key="elast_linea")
    with fc3:
        _m2 = df.copy()
        if sel_area:
            _m2 = _m2[_m2["AREA"].isin(sel_area)]
        if sel_linea:
            _m2 = _m2[_m2["LINEA"].isin(sel_linea)]
        marcas_filt = sorted(_m2["MARCA"].dropna().unique()) if "MARCA" in _m2.columns else marcas_all
        sel_marca = st.multiselect("Marca", marcas_filt, key="elast_marca")
    with fc4:
        solo_confiable = st.checkbox("Solo estimaciones confiables", value=True, key="elast_confiable")

    # Apply filters
    mask = pd.Series(True, index=df.index)
    if sel_area and "AREA" in df.columns:
        mask &= df["AREA"].isin(sel_area)
    if sel_linea and "LINEA" in df.columns:
        mask &= df["LINEA"].isin(sel_linea)
    if sel_marca and "MARCA" in df.columns:
        mask &= df["MARCA"].isin(sel_marca)
    if solo_confiable:
        mask &= df["CONFIABLE"]
    df_filt = df[mask]

    if df_filt.empty:
        st.warning("No hay datos con los filtros seleccionados.")
        return

    st.markdown("---")

    # ── Warnings Section (top priority) ──────────────────────────────
    if not warnings_df.empty:
        _render_warnings(warnings_df)
        st.markdown("---")

    # ── KPIs ─────────────────────────────────────────────────────────
    st.markdown("### Analisis de Elasticidad")
    _render_kpis(df_filt)

    # ── Charts Row 1 ─────────────────────────────────────────────────
    col_left, col_right = st.columns(2)
    with col_left:
        _render_distribution(df_filt)
    with col_right:
        _render_histogram(df_filt)

    st.markdown("---")

    # ── Charts Row 2 ─────────────────────────────────────────────────
    col_scatter, col_dim = st.columns(2)
    with col_scatter:
        _render_scatter(df_filt)
    with col_dim:
        _render_by_dimension(df_filt)

    st.markdown("---")

    # ── Methodology ──────────────────────────────────────────────────
    with st.expander("Metodologia"):
        st.markdown("""
**Calculo de Elasticidad:**
- Metodo: Regresion log-log (ln(cantidad) ~ ln(precio))
- Datos: Ventas mensuales por SKU, ultimos 24 meses
- Minimo: 6 observaciones mensuales por SKU

**Clasificacion:**
| Segmento | Rango Elasticidad | Interpretacion |
|----------|-------------------|----------------|
| Muy Elastico | e < -1.5 | Muy sensible al precio. Un 1% de aumento reduce demanda >1.5% |
| Elastico | -1.5 <= e < -1.0 | Sensible al precio. Cambios de precio impactan proporcionalmente |
| Unitario | -1.0 <= e < -0.7 | Impacto proporcional. Zona de transicion |
| Inelastico | -0.7 <= e < -0.3 | Poco sensible. El precio no afecta mucho la demanda |
| Muy Inelastico | -0.3 <= e < 0 | Casi sin efecto del precio en la demanda |
| Anomalo (+) | e >= 0 | Relacion positiva (Veblen/Giffen o ruido estadistico) |

**Confiabilidad:**
Una estimacion se marca como "confiable" cuando:
- p-value < 0.1 (significancia estadistica)
- R-squared > 0.1 (varianza explicada minima)
- CV del precio > 5% (suficiente variacion de precios para medir)
- Al menos 6 observaciones mensuales

**Warnings de Forecast:**
- Se generan cuando un SKU elastico tiene un precio de forecast que difiere >5% del historico
- Impacto estimado = elasticidad x % cambio precio
- Ejemplo: elasticidad=-2.0, precio sube 10% => demanda baja ~20%
""")

    # ── Detail Table ─────────────────────────────────────────────────
    st.markdown("### Detalle por SKU")

    display_cols = ["SKU_PRODUCTO", "SEGMENTO", "ELASTICIDAD", "R_SQUARED", "P_VALUE",
                    "CONFIABLE", "N_OBS", "PRECIO_PROM", "PRECIO_CV",
                    "CANTIDAD_PROM_MES", "VN_PROM_MES", "APORTE_TOTAL"]
    for c in ["AREA", "LINEA", "SUBLINEA", "MARCA", "SKU_NOM_PRODUCTO"]:
        if c in df_filt.columns:
            display_cols.insert(1, c)
    display_cols = [c for c in display_cols if c in df_filt.columns]

    df_display = df_filt[display_cols].sort_values("ELASTICIDAD").reset_index(drop=True)
    st.dataframe(df_display, use_container_width=True, height=500)

    # ── Export ────────────────────────────────────────────────────────
    download_buttons(df_display, "elasticidad_precio_demanda")
