"""Forecast Accuracy - Medicion de precision del forecast vs ventas reales."""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format
from utils.export import download_buttons
from config import COLORS, dorel_layout, apply_pm_filter
from utils.ui_animations import lottie_spinner

CANAL_MAP = {
    "MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL",
    "TIENDA": "TIENDA", "MAYORISTA": "MAYORISTA",
}

ACCURACY_COLORS = {
    "EXCELENTE": "#1B5E20",
    "BUENO": "#43A047",
    "ACEPTABLE": "#FB8C00",
    "MALO": "#E53935",
    "MUY MALO": "#B71C1C",
}
ACCURACY_ORDER = ["EXCELENTE", "BUENO", "ACEPTABLE", "MALO", "MUY MALO"]

BIAS_COLORS = {
    "SOBRE-ESTIMA": "#E53935",
    "NEUTRO": "#43A047",
    "SUB-ESTIMA": "#0D47A1",
}


def _get_forecast_data():
    """Get projection data from session_state."""
    df = st.session_state.get("df_proy")
    if df is not None and isinstance(df, pd.DataFrame) and not df.empty:
        return df.copy()
    return None


def _load_actuals(conn):
    with lottie_spinner("snowflake"):
        actuals = cq.ventas_mes_anterior(conn)
        maestra = cq.maestra(conn)
    return actuals, maestra


def _calculate_accuracy(proy_df, actuals, maestra):
    """Calculate MAE%, Bias, and Forecast Accuracy per SKU."""
    if proy_df is None or proy_df.empty or actuals.empty:
        return pd.DataFrame()

    fc_cols = {}
    for src, dst in [("DEMANDA_SIM_TIENDA", "FC_TIENDA"), ("DEMANDA_SIM_ETAIL", "FC_ETAIL"),
                     ("DEMANDA_SIM_MAYOR", "FC_MAYOR")]:
        if src in proy_df.columns:
            fc_cols[src] = dst

    if not fc_cols:
        return pd.DataFrame()

    if "PERIODO" in proy_df.columns:
        first_period = proy_df.sort_values("PERIODO").groupby("SKU_PRODUCTO").first().reset_index()
    else:
        first_period = proy_df.drop_duplicates(subset=["SKU_PRODUCTO"])

    fc = first_period[["SKU_PRODUCTO"] + list(fc_cols.keys())].rename(columns=fc_cols)
    for c in ["FC_TIENDA", "FC_ETAIL", "FC_MAYOR"]:
        if c not in fc.columns:
            fc[c] = 0.0
    fc["FC_TOTAL"] = fc["FC_TIENDA"].fillna(0) + fc["FC_ETAIL"].fillna(0) + fc["FC_MAYOR"].fillna(0)

    if "COD_CANAL" in actuals.columns:
        actuals["CANAL_STD"] = actuals["COD_CANAL"].str.strip().str.upper().map(CANAL_MAP)

    act_pivot = actuals.groupby("SKU_PRODUCTO", as_index=False).agg(
        ACT_TOTAL=("CANTIDAD_MES", "sum"),
        VN_ACTUAL=("NETO_MES", "sum"),
    )

    if "CANAL_STD" in actuals.columns:
        for canal in ["TIENDA", "ETAIL", "MAYORISTA"]:
            sub = actuals[actuals["CANAL_STD"] == canal].groupby("SKU_PRODUCTO", as_index=False).agg(
                **{f"ACT_{canal}": ("CANTIDAD_MES", "sum")}
            )
            act_pivot = act_pivot.merge(sub, on="SKU_PRODUCTO", how="left")

    for c in ["ACT_TIENDA", "ACT_ETAIL", "ACT_MAYORISTA"]:
        if c not in act_pivot.columns:
            act_pivot[c] = 0.0
        act_pivot[c] = act_pivot[c].fillna(0)

    df = fc.merge(act_pivot, on="SKU_PRODUCTO", how="inner")
    if df.empty:
        return pd.DataFrame()

    df["ERROR_ABS"] = abs(df["FC_TOTAL"] - df["ACT_TOTAL"])
    df["MAE_PCT"] = np.where(df["ACT_TOTAL"] > 0, df["ERROR_ABS"] / df["ACT_TOTAL"] * 100, np.nan)
    df["FA_PCT"] = np.clip(100 - df["MAE_PCT"].fillna(100), 0, 100)
    df["BIAS_PCT"] = np.where(
        df["ACT_TOTAL"] > 0,
        (df["FC_TOTAL"] - df["ACT_TOTAL"]) / df["ACT_TOTAL"] * 100,
        np.nan,
    )

    df["ACCURACY_SEGMENT"] = np.select(
        [df["FA_PCT"] >= 80, df["FA_PCT"] >= 60, df["FA_PCT"] >= 40, df["FA_PCT"] >= 20],
        ["EXCELENTE", "BUENO", "ACEPTABLE", "MALO"],
        default="MUY MALO",
    )

    df["BIAS_SEGMENT"] = np.select(
        [df["BIAS_PCT"] > 15, df["BIAS_PCT"] < -15],
        ["SOBRE-ESTIMA", "SUB-ESTIMA"],
        default="NEUTRO",
    )

    maestra_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                    "SKU_NOM_PRODUCTO", "MIX_OFICIAL"]
    available = [c for c in maestra_cols if c in maestra.columns]
    maestra_dedup = maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
    df = df.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")

    return df


def _render_kpis(df):
    valid = df[df["MAE_PCT"].notna()]
    fa_avg = valid["FA_PCT"].mean() if not valid.empty else 0
    mae_avg = valid["MAE_PCT"].mean() if not valid.empty else 0
    bias_avg = valid["BIAS_PCT"].mean() if not valid.empty else 0
    sobre = (valid["BIAS_SEGMENT"] == "SOBRE-ESTIMA").sum()
    sub = (valid["BIAS_SEGMENT"] == "SUB-ESTIMA").sum()

    cols = st.columns(5)
    data = [
        ("FA% Promedio", f"{fa_avg:.0f}%", COLORS["primary"]),
        ("MAE% Promedio", f"{mae_avg:.0f}%", COLORS["tertiary_blue"]),
        ("Bias Promedio", f"{bias_avg:+.0f}%", "#FB8C00"),
        ("Sobre-Estima", f"{sobre:,}", BIAS_COLORS["SOBRE-ESTIMA"]),
        ("Sub-Estima", f"{sub:,}", BIAS_COLORS["SUB-ESTIMA"]),
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


def _render_warnings(df):
    """Generate warnings for persistent bad accuracy."""
    bad = df[(df["ACCURACY_SEGMENT"].isin(["MALO", "MUY MALO"])) & (df["ACT_TOTAL"] > 0)].copy()
    if bad.empty:
        st.success("No hay SKUs con accuracy critica.")
        return

    st.markdown("### \u26a0\ufe0f Warnings de Forecast")

    sobre = bad[bad["BIAS_SEGMENT"] == "SOBRE-ESTIMA"].nlargest(10, "ERROR_ABS")
    sub = bad[bad["BIAS_SEGMENT"] == "SUB-ESTIMA"].nlargest(10, "ERROR_ABS")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### Sobre-estimados (FC >> Actual)")
        if not sobre.empty:
            st.caption("Estos SKUs tienen forecast mucho mayor que la venta real. "
                       "Considerar reducir el forecast futuro.")
            nombre_col = "SKU_NOM_PRODUCTO" if "SKU_NOM_PRODUCTO" in sobre.columns else "SKU_PRODUCTO"
            sobre = sobre.copy()
            sobre["LABEL"] = sobre["SKU_PRODUCTO"].astype(str) + " - " + sobre[nombre_col].astype(str)
            sobre["LABEL"] = sobre["LABEL"].str[:45]
            sobre = sobre.sort_values("BIAS_PCT", ascending=True)

            fig_s = go.Figure(layout=dorel_layout(
                height=400,
                xaxis=dict(title="Bias %"),
                yaxis=dict(title=""),
                margin=dict(l=250, r=20, t=20, b=40),
                showlegend=False,
            ))
            fig_s.add_trace(go.Bar(
                x=sobre["BIAS_PCT"], y=sobre["LABEL"],
                orientation="h", marker_color=BIAS_COLORS["SOBRE-ESTIMA"],
                hovertemplate=(
                    "SKU: %{customdata[0]}<br>"
                    "FC: %{customdata[1]:,.0f}<br>"
                    "Real: %{customdata[2]:,.0f}<br>"
                    "Bias: %{x:.0f}%<br>"
                    "FA: %{customdata[3]:.0f}%<extra></extra>"
                ),
                customdata=np.column_stack([
                    sobre["SKU_PRODUCTO"], sobre["FC_TOTAL"],
                    sobre["ACT_TOTAL"], sobre["FA_PCT"],
                ]),
            ))
            st.plotly_chart(fig_s, use_container_width=True)
        else:
            st.info("No hay SKUs sobre-estimados con mal accuracy.")

    with col2:
        st.markdown("#### Sub-estimados (FC << Actual)")
        if not sub.empty:
            st.caption("Estos SKUs venden mucho mas que el forecast. "
                       "Riesgo de quiebre si no se ajusta el forecast.")
            nombre_col = "SKU_NOM_PRODUCTO" if "SKU_NOM_PRODUCTO" in sub.columns else "SKU_PRODUCTO"
            sub = sub.copy()
            sub["LABEL"] = sub["SKU_PRODUCTO"].astype(str) + " - " + sub[nombre_col].astype(str)
            sub["LABEL"] = sub["LABEL"].str[:45]
            sub = sub.sort_values("BIAS_PCT", ascending=True)

            fig_sub = go.Figure(layout=dorel_layout(
                height=400,
                xaxis=dict(title="Bias %"),
                yaxis=dict(title=""),
                margin=dict(l=250, r=20, t=20, b=40),
                showlegend=False,
            ))
            fig_sub.add_trace(go.Bar(
                x=sub["BIAS_PCT"], y=sub["LABEL"],
                orientation="h", marker_color=BIAS_COLORS["SUB-ESTIMA"],
                hovertemplate=(
                    "SKU: %{customdata[0]}<br>"
                    "FC: %{customdata[1]:,.0f}<br>"
                    "Real: %{customdata[2]:,.0f}<br>"
                    "Bias: %{x:.0f}%<br>"
                    "FA: %{customdata[3]:.0f}%<extra></extra>"
                ),
                customdata=np.column_stack([
                    sub["SKU_PRODUCTO"], sub["FC_TOTAL"],
                    sub["ACT_TOTAL"], sub["FA_PCT"],
                ]),
            ))
            st.plotly_chart(fig_sub, use_container_width=True)
        else:
            st.info("No hay SKUs sub-estimados con mal accuracy.")


def render_forecast_accuracy(conn):
    st.html("<h2 class='sub-header'>Forecast Accuracy</h2>")
    st.caption("FA% = 1 - MAE%. Compara el forecast de la proyeccion con las ventas reales del mes anterior.")

    proy_df = _get_forecast_data()
    if proy_df is None:
        st.warning("No hay datos de proyeccion. Genera primero una proyeccion en 'Proyeccion Stock'.")
        return

    if st.button("Actualizar", key="btn_refresh_fca"):
        st.session_state.pop("fca_data", None)

    if "fca_data" not in st.session_state:
        actuals, maestra = _load_actuals(conn)
        df = _calculate_accuracy(proy_df, actuals, maestra)
        st.session_state["fca_data"] = df

    df = st.session_state["fca_data"].copy()
    df = apply_pm_filter(df)
    if df.empty:
        st.warning("No se pudo calcular accuracy. Revisa que haya datos de ventas y proyeccion.")
        return

    # Filters
    st.markdown("### Filtros")
    fc1, fc2, fc3 = st.columns(3)
    areas = sorted(df["AREA"].dropna().unique()) if "AREA" in df.columns else []
    with fc1:
        sel_area = st.multiselect("Area", areas, key="fca_area")
    with fc2:
        _m = df[df["AREA"].isin(sel_area)] if sel_area else df
        sel_linea = st.multiselect("Linea", sorted(_m["LINEA"].dropna().unique()) if "LINEA" in _m.columns else [], key="fca_linea")
    with fc3:
        _m2 = _m[_m["LINEA"].isin(sel_linea)] if sel_linea else _m
        sel_marca = st.multiselect("Marca", sorted(_m2["MARCA"].dropna().unique()) if "MARCA" in _m2.columns else [], key="fca_marca")

    mask = pd.Series(True, index=df.index)
    if sel_area:
        mask &= df["AREA"].isin(sel_area)
    if sel_linea:
        mask &= df["LINEA"].isin(sel_linea)
    if sel_marca:
        mask &= df["MARCA"].isin(sel_marca)
    df_filt = df[mask]
    if df_filt.empty:
        st.warning("Sin datos.")
        return

    st.markdown("---")
    _render_kpis(df_filt)

    # Warnings
    _render_warnings(df_filt)
    st.markdown("---")

    # ── Chart 1: Accuracy distribution ──
    col1, col2 = st.columns(2)
    with col1:
        counts = df_filt["ACCURACY_SEGMENT"].value_counts().reset_index()
        counts.columns = ["Segmento", "Count"]
        cat = [a for a in ACCURACY_ORDER if a in counts["Segmento"].values]
        clr = [ACCURACY_COLORS[a] for a in cat]

        fig1 = go.Figure(layout=dorel_layout(
            title=dict(text="Distribucion de Forecast Accuracy", font_size=14, x=0.5),
            height=380,
            xaxis=dict(title="", categoryorder="array", categoryarray=cat),
            yaxis=dict(title="# SKUs"),
            showlegend=False,
        ))
        for seg, color in zip(cat, clr):
            row = counts[counts["Segmento"] == seg]
            if not row.empty:
                fig1.add_trace(go.Bar(
                    x=[seg], y=[row["Count"].values[0]],
                    marker_color=color, showlegend=False,
                    hovertemplate="Segmento: %{x}<br>SKUs: %{y:,}<extra></extra>",
                ))
        st.plotly_chart(fig1, use_container_width=True)

    # ── Chart 2: Scatter FC vs Actual ──
    with col2:
        valid = df_filt[df_filt["ACT_TOTAL"] > 0].copy()
        if not valid.empty:
            max_val = max(valid["FC_TOTAL"].quantile(0.95), valid["ACT_TOTAL"].quantile(0.95))

            fig2 = go.Figure(layout=dorel_layout(
                title=dict(text="Forecast vs Venta Real", font_size=14, x=0.5),
                height=380,
                xaxis=dict(title="Venta Real (Und)", range=[0, max_val * 1.05]),
                yaxis=dict(title="Forecast (Und)", range=[0, max_val * 1.05]),
                showlegend=True,
                legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="center", x=0.5),
            ))

            for seg in ACCURACY_ORDER:
                sub = valid[valid["ACCURACY_SEGMENT"] == seg]
                if sub.empty:
                    continue
                fig2.add_trace(go.Scatter(
                    x=sub["ACT_TOTAL"], y=sub["FC_TOTAL"],
                    mode="markers", name=seg,
                    marker=dict(color=ACCURACY_COLORS[seg], size=6, opacity=0.6),
                    hovertemplate=(
                        "SKU: %{customdata[0]}<br>"
                        "FC: %{y:,.0f}<br>Real: %{x:,.0f}<br>"
                        "FA: %{customdata[1]:.0f}%<br>"
                        "Bias: %{customdata[2]:.0f}%<extra></extra>"
                    ),
                    customdata=np.column_stack([sub["SKU_PRODUCTO"], sub["FA_PCT"], sub["BIAS_PCT"]]),
                ))

            # Diagonal line (perfect accuracy)
            fig2.add_trace(go.Scatter(
                x=[0, max_val], y=[0, max_val],
                mode="lines", name="Perfecto",
                line=dict(color="black", dash="dash", width=1),
                showlegend=False,
            ))
            st.plotly_chart(fig2, use_container_width=True)

    # ── Chart 3: FA% by dimension ──
    st.markdown("---")
    dim = st.radio("Accuracy por", ["AREA", "LINEA", "MARCA"], horizontal=True, key="fca_dim")
    if dim in df_filt.columns:
        valid_dim = df_filt[df_filt["FA_PCT"].notna()]
        by_dim = valid_dim.groupby(dim, as_index=False).agg(
            FA_PROM=("FA_PCT", "mean"),
            BIAS_PROM=("BIAS_PCT", "mean"),
            COUNT=("SKU_PRODUCTO", "count"),
        ).nlargest(15, "COUNT")

        fig3 = go.Figure(layout=dorel_layout(
            title=dict(text=f"FA% Promedio por {dim.title()}", font_size=14, x=0.5),
            height=380,
            xaxis=dict(title="", categoryorder="total descending"),
            yaxis=dict(title="FA% Promedio"),
            showlegend=False,
        ))
        fig3.add_trace(go.Bar(
            x=by_dim[dim], y=by_dim["FA_PROM"],
            marker_color=COLORS["primary"],
            hovertemplate=(
                "%{x}<br>FA: %{y:.0f}%<br>"
                "Bias: %{customdata[0]:+.0f}%<br>"
                "SKUs: %{customdata[1]:,}<extra></extra>"
            ),
            customdata=np.column_stack([by_dim["BIAS_PROM"], by_dim["COUNT"]]),
        ))
        st.plotly_chart(fig3, use_container_width=True)

    st.markdown("---")
    st.markdown("### Detalle")
    display_cols = ["SKU_PRODUCTO", "ACCURACY_SEGMENT", "FA_PCT", "MAE_PCT", "BIAS_PCT",
                    "BIAS_SEGMENT", "FC_TOTAL", "ACT_TOTAL", "ERROR_ABS"]
    for c in ["AREA", "LINEA", "MARCA", "SKU_NOM_PRODUCTO"]:
        if c in df_filt.columns:
            display_cols.insert(1, c)
    display_cols = [c for c in display_cols if c in df_filt.columns]
    st.dataframe(df_filt[display_cols].sort_values("FA_PCT").reset_index(drop=True),
                 use_container_width=True, height=500)
    download_buttons(df_filt[display_cols], "forecast_accuracy")
