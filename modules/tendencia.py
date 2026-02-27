"""Deteccion de Tendencia - Identificacion automatica de SKUs con demanda en alza o baja."""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from scipy import stats

from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format
from utils.export import download_buttons
from config import COLORS, dorel_layout, apply_pm_filter
from utils.ui_animations import lottie_spinner

TREND_COLORS = {
    "FUERTE ALZA": "#1B5E20",
    "ALZA": "#43A047",
    "ESTABLE": "#9E9E9E",
    "BAJA": "#FB8C00",
    "FUERTE BAJA": "#E53935",
}
TREND_ORDER = ["FUERTE ALZA", "ALZA", "ESTABLE", "BAJA", "FUERTE BAJA"]


def _load_data(conn):
    with lottie_spinner("snowflake"):
        ventas = cq.ventas_semanal_tendencia(conn)
        maestra = cq.maestra(conn)
    return ventas, maestra


def _detect_trends(ventas, maestra):
    """Detect demand trends using linear regression on weekly sales."""
    if ventas.empty:
        return pd.DataFrame()

    ventas["SEMANA"] = pd.to_datetime(ventas["SEMANA"])
    min_weeks = 8

    results = []
    for sku, grp in ventas.groupby("SKU_PRODUCTO"):
        grp = grp.sort_values("SEMANA")
        if len(grp) < min_weeks:
            continue

        x = np.arange(len(grp))
        y = grp["UNIDADES"].values.astype(float)

        try:
            slope, intercept, r_value, p_value, std_err = stats.linregress(x, y)
        except Exception:
            continue

        mean_y = y.mean()
        if mean_y == 0:
            continue

        # Normalized slope: % change per week
        slope_pct = slope / mean_y * 100

        # Recent vs earlier comparison
        half = len(grp) // 2
        early_avg = y[:half].mean()
        late_avg = y[half:].mean()
        change_pct = (late_avg - early_avg) / max(early_avg, 0.1) * 100

        results.append({
            "SKU_PRODUCTO": sku,
            "SLOPE": slope,
            "SLOPE_PCT_SEMANAL": slope_pct,
            "R_SQUARED": r_value ** 2,
            "P_VALUE": p_value,
            "VENTA_PROM_SEMANAL": mean_y,
            "VN_PROM_SEMANAL": grp["NETO"].mean(),
            "SEMANAS_CON_DATOS": len(grp),
            "CAMBIO_PCT": change_pct,
            "VENTA_RECIENTE": late_avg,
            "VENTA_ANTERIOR": early_avg,
            "ULTIMA_SEMANA": grp["SEMANA"].max(),
        })

    if not results:
        return pd.DataFrame()

    df = pd.DataFrame(results)

    # Classify trend
    df["SIGNIFICATIVA"] = (df["P_VALUE"] < 0.1) & (df["R_SQUARED"] > 0.1)

    conditions = [
        df["SIGNIFICATIVA"] & (df["SLOPE_PCT_SEMANAL"] > 3),
        df["SIGNIFICATIVA"] & (df["SLOPE_PCT_SEMANAL"] > 1),
        df["SIGNIFICATIVA"] & (df["SLOPE_PCT_SEMANAL"] < -3),
        df["SIGNIFICATIVA"] & (df["SLOPE_PCT_SEMANAL"] < -1),
    ]
    choices = ["FUERTE ALZA", "ALZA", "FUERTE BAJA", "BAJA"]
    df["TENDENCIA"] = np.select(conditions, choices, default="ESTABLE")

    # Maestra
    maestra_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                    "SKU_NOM_PRODUCTO", "MIX_OFICIAL"]
    available = [c for c in maestra_cols if c in maestra.columns]
    maestra_dedup = maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
    df = df.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")

    return df


def _render_kpis(df):
    total = len(df)
    f_alza = (df["TENDENCIA"] == "FUERTE ALZA").sum()
    alza = (df["TENDENCIA"] == "ALZA").sum()
    f_baja = (df["TENDENCIA"] == "FUERTE BAJA").sum()
    baja = (df["TENDENCIA"] == "BAJA").sum()

    cols = st.columns(5)
    data = [
        ("Total SKUs", f"{total:,}", COLORS["primary"]),
        ("Fuerte Alza", f"{f_alza:,}", TREND_COLORS["FUERTE ALZA"]),
        ("Alza", f"{alza:,}", TREND_COLORS["ALZA"]),
        ("Baja", f"{baja:,}", TREND_COLORS["BAJA"]),
        ("Fuerte Baja", f"{f_baja:,}", TREND_COLORS["FUERTE BAJA"]),
    ]
    for col, (label, value, color) in zip(cols, data):
        with col:
            st.html(f"""
            <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                         border-top:4px solid {color};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                <div style='font-size:1.8rem;font-weight:bold;color:{color}'>{value}</div>
                <div style='font-size:0.8rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{label}</div>
            </div>""")
    st.html("<br>")


def render_tendencia(conn):
    st.html("<h2 class='sub-header'>Deteccion de Tendencia</h2>")
    st.caption("Identificacion automatica de SKUs con demanda en alza o baja usando regresion lineal sobre 24 semanas.")

    if st.button("Actualizar Datos", key="btn_refresh_tend"):
        st.session_state.pop("tend_data", None)

    if "tend_data" not in st.session_state:
        ventas, maestra = _load_data(conn)
        df = _detect_trends(ventas, maestra)
        st.session_state["tend_data"] = df

    df = st.session_state["tend_data"].copy()
    df = apply_pm_filter(df)
    if df.empty:
        st.warning("No se encontraron datos suficientes.")
        return

    # Filters
    st.markdown("### Filtros")
    fc1, fc2, fc3, fc4 = st.columns(4)
    areas = sorted(df["AREA"].dropna().unique()) if "AREA" in df.columns else []
    with fc1:
        sel_area = st.multiselect("Area", areas, key="tend_area")
    with fc2:
        _m = df[df["AREA"].isin(sel_area)] if sel_area else df
        sel_linea = st.multiselect("Linea", sorted(_m["LINEA"].dropna().unique()) if "LINEA" in _m.columns else [], key="tend_linea")
    with fc3:
        _m2 = _m[_m["LINEA"].isin(sel_linea)] if sel_linea else _m
        sel_marca = st.multiselect("Marca", sorted(_m2["MARCA"].dropna().unique()) if "MARCA" in _m2.columns else [], key="tend_marca")
    with fc4:
        sel_tend = st.multiselect("Tendencia", TREND_ORDER, default=["FUERTE ALZA", "FUERTE BAJA"], key="tend_tipo")

    mask = pd.Series(True, index=df.index)
    if sel_area:
        mask &= df["AREA"].isin(sel_area)
    if sel_linea:
        mask &= df["LINEA"].isin(sel_linea)
    if sel_marca:
        mask &= df["MARCA"].isin(sel_marca)
    if sel_tend:
        mask &= df["TENDENCIA"].isin(sel_tend)
    df_filt = df[mask]
    if df_filt.empty:
        st.warning("Sin datos con filtros seleccionados.")
        return

    st.markdown("---")
    _render_kpis(df_filt)

    # ── Chart 1: Distribution bar ──
    col1, col2 = st.columns(2)

    with col1:
        counts = df_filt["TENDENCIA"].value_counts().reset_index()
        counts.columns = ["Tendencia", "Count"]
        cat = [t for t in TREND_ORDER if t in counts["Tendencia"].values]
        clr = [TREND_COLORS[t] for t in cat]

        fig1 = go.Figure(layout=dorel_layout(
            title=dict(text="Distribucion de Tendencias", font_size=14, x=0.5),
            height=380,
            xaxis=dict(title="", categoryorder="array", categoryarray=cat),
            yaxis=dict(title="# SKUs"),
            showlegend=False,
        ))
        for seg, color in zip(cat, clr):
            row = counts[counts["Tendencia"] == seg]
            if not row.empty:
                fig1.add_trace(go.Bar(
                    x=[seg], y=[row["Count"].values[0]],
                    marker_color=color, name=seg, showlegend=False,
                    hovertemplate="Tendencia: %{x}<br>SKUs: %{y:,}<extra></extra>",
                ))
        st.plotly_chart(fig1, use_container_width=True)

    # ── Chart 2: Scatter — Slope vs VN ──
    with col2:
        plot_df = df_filt[df_filt["TENDENCIA"] != "ESTABLE"].copy()
        if not plot_df.empty:
            # Pre-compute colors per point
            color_map = {t: TREND_COLORS[t] for t in TREND_ORDER}
            point_colors = plot_df["TENDENCIA"].map(color_map).tolist()

            x_lo = plot_df["SLOPE_PCT_SEMANAL"].quantile(0.02)
            x_hi = plot_df["SLOPE_PCT_SEMANAL"].quantile(0.98)

            fig2 = go.Figure(layout=dorel_layout(
                title=dict(text="Pendiente vs VN Semanal", font_size=14, x=0.5),
                height=380,
                xaxis=dict(title="Pendiente Semanal (%)", range=[x_lo, x_hi]),
                yaxis=dict(title="VN Semanal ($)", type="log"),
                showlegend=True,
                legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="center", x=0.5),
            ))

            # Add one trace per trend category for legend
            for trend in TREND_ORDER:
                sub = plot_df[plot_df["TENDENCIA"] == trend]
                if sub.empty:
                    continue
                fig2.add_trace(go.Scatter(
                    x=sub["SLOPE_PCT_SEMANAL"], y=sub["VN_PROM_SEMANAL"],
                    mode="markers", name=trend,
                    marker=dict(color=TREND_COLORS[trend], size=7, opacity=0.7),
                    hovertemplate=(
                        "SKU: %{customdata[0]}<br>"
                        "Pendiente: %{x:.1f}%<br>"
                        "VN Semanal: $%{y:,.0f}<br>"
                        "Cambio: %{customdata[1]:.0f}%<extra></extra>"
                    ),
                    customdata=np.column_stack([sub["SKU_PRODUCTO"], sub["CAMBIO_PCT"]]),
                ))

            # Zero reference line
            fig2.add_vline(x=0, line_dash="dash", line_color="black", line_width=1)
            st.plotly_chart(fig2, use_container_width=True)

    st.markdown("---")

    # ── Charts 3 & 4: Top rising / falling ──
    col_rise, col_fall = st.columns(2)

    with col_rise:
        st.markdown("#### Top SKUs en Alza")
        rising = df_filt[df_filt["TENDENCIA"].isin(["FUERTE ALZA", "ALZA"])].nlargest(15, "SLOPE_PCT_SEMANAL")
        if not rising.empty:
            nombre_col = "SKU_NOM_PRODUCTO" if "SKU_NOM_PRODUCTO" in rising.columns else "SKU_PRODUCTO"
            rising = rising.copy()
            rising["LABEL"] = rising["SKU_PRODUCTO"].astype(str) + " - " + rising[nombre_col].astype(str)
            rising["LABEL"] = rising["LABEL"].str[:45]
            rising = rising.sort_values("SLOPE_PCT_SEMANAL", ascending=True)

            fig3 = go.Figure(layout=dorel_layout(
                height=450,
                xaxis=dict(title="% semanal"),
                yaxis=dict(title=""),
                margin=dict(l=250, r=20, t=30, b=40),
                showlegend=False,
            ))
            fig3.add_trace(go.Bar(
                x=rising["SLOPE_PCT_SEMANAL"], y=rising["LABEL"],
                orientation="h", marker_color=TREND_COLORS["FUERTE ALZA"],
                hovertemplate=(
                    "SKU: %{customdata[0]}<br>"
                    "Pendiente: %{x:.1f}%<br>"
                    "Cambio: %{customdata[1]:.0f}%<br>"
                    "Venta Prom Sem: %{customdata[2]:,.0f}<extra></extra>"
                ),
                customdata=np.column_stack([
                    rising["SKU_PRODUCTO"], rising["CAMBIO_PCT"], rising["VENTA_PROM_SEMANAL"]
                ]),
            ))
            st.plotly_chart(fig3, use_container_width=True)

    with col_fall:
        st.markdown("#### Top SKUs en Baja")
        falling = df_filt[df_filt["TENDENCIA"].isin(["FUERTE BAJA", "BAJA"])].nsmallest(15, "SLOPE_PCT_SEMANAL")
        if not falling.empty:
            nombre_col = "SKU_NOM_PRODUCTO" if "SKU_NOM_PRODUCTO" in falling.columns else "SKU_PRODUCTO"
            falling = falling.copy()
            falling["LABEL"] = falling["SKU_PRODUCTO"].astype(str) + " - " + falling[nombre_col].astype(str)
            falling["LABEL"] = falling["LABEL"].str[:45]
            falling = falling.sort_values("SLOPE_PCT_SEMANAL", ascending=True)

            fig4 = go.Figure(layout=dorel_layout(
                height=450,
                xaxis=dict(title="% semanal"),
                yaxis=dict(title=""),
                margin=dict(l=250, r=20, t=30, b=40),
                showlegend=False,
            ))
            fig4.add_trace(go.Bar(
                x=falling["SLOPE_PCT_SEMANAL"], y=falling["LABEL"],
                orientation="h", marker_color=TREND_COLORS["FUERTE BAJA"],
                hovertemplate=(
                    "SKU: %{customdata[0]}<br>"
                    "Pendiente: %{x:.1f}%<br>"
                    "Cambio: %{customdata[1]:.0f}%<br>"
                    "Venta Prom Sem: %{customdata[2]:,.0f}<extra></extra>"
                ),
                customdata=np.column_stack([
                    falling["SKU_PRODUCTO"], falling["CAMBIO_PCT"], falling["VENTA_PROM_SEMANAL"]
                ]),
            ))
            st.plotly_chart(fig4, use_container_width=True)

    st.markdown("---")
    st.markdown("### Detalle")
    display_cols = ["SKU_PRODUCTO", "TENDENCIA", "SLOPE_PCT_SEMANAL", "CAMBIO_PCT",
                    "VENTA_PROM_SEMANAL", "VN_PROM_SEMANAL", "R_SQUARED", "SEMANAS_CON_DATOS"]
    for c in ["AREA", "LINEA", "MARCA", "SKU_NOM_PRODUCTO"]:
        if c in df_filt.columns:
            display_cols.insert(1, c)
    display_cols = [c for c in display_cols if c in df_filt.columns]
    st.dataframe(df_filt[display_cols].sort_values("SLOPE_PCT_SEMANAL").reset_index(drop=True),
                 use_container_width=True, height=500)
    download_buttons(df_filt[display_cols], "deteccion_tendencia")
