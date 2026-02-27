"""Simulador de Rentabilidad - Impacto de cambios de precio en margen considerando elasticidad."""

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


def _load_base_data(conn):
    with lottie_spinner("snowflake"):
        ventas = cq.ventas_aa(conn)
        maestra = cq.maestra(conn)
    return ventas, maestra


def _build_base(ventas, maestra):
    """Build base dataset with current price, volume, margin per SKU."""
    if ventas.empty:
        return pd.DataFrame()

    if "COD_CANAL" in ventas.columns:
        ventas["CANAL_STD"] = ventas["COD_CANAL"].str.strip().str.upper().map(CANAL_MAP)

    by_sku = ventas.groupby("SKU_PRODUCTO", as_index=False).agg(
        VN_ANUAL=("NETO_AA", "sum"),
        APORTE_ANUAL=("APORTE_AA", "sum"),
        UNIDADES_ANUAL=("CANTIDAD_AA", "sum"),
    )
    by_sku["PRECIO_PROM"] = np.where(
        by_sku["UNIDADES_ANUAL"] > 0,
        by_sku["VN_ANUAL"] / by_sku["UNIDADES_ANUAL"],
        0,
    )
    by_sku["MARGEN_PCT"] = np.where(
        by_sku["VN_ANUAL"] > 0,
        by_sku["APORTE_ANUAL"] / by_sku["VN_ANUAL"] * 100,
        0,
    )

    maestra_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                    "SKU_NOM_PRODUCTO", "ULTIMO_COSTO", "MIX_OFICIAL"]
    available = [c for c in maestra_cols if c in maestra.columns]
    maestra_dedup = maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
    by_sku = by_sku.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")

    by_sku["COSTO_UNIT"] = by_sku.get("ULTIMO_COSTO", pd.Series(0, index=by_sku.index)).fillna(0)

    elast_df = st.session_state.get("elast_data")
    if elast_df is not None and isinstance(elast_df, pd.DataFrame) and not elast_df.empty:
        elast_sub = elast_df[["SKU_PRODUCTO", "ELASTICIDAD", "CONFIABLE", "SEGMENTO"]].copy()
        by_sku = by_sku.merge(elast_sub, on="SKU_PRODUCTO", how="left")
    else:
        by_sku["ELASTICIDAD"] = -1.0
        by_sku["CONFIABLE"] = False
        by_sku["SEGMENTO"] = "N/A"

    by_sku["ELASTICIDAD"] = by_sku["ELASTICIDAD"].fillna(-1.0)

    return by_sku[by_sku["UNIDADES_ANUAL"] > 0].reset_index(drop=True)


def _simulate(df, price_change_pct):
    """Simulate impact of a price change given elasticity."""
    sim = df.copy()
    pct = price_change_pct / 100.0

    sim["PRECIO_NUEVO"] = sim["PRECIO_PROM"] * (1 + pct)
    sim["DEMAND_CHANGE_PCT"] = sim["ELASTICIDAD"] * pct * 100
    sim["UNIDADES_NUEVO"] = sim["UNIDADES_ANUAL"] * (1 + sim["ELASTICIDAD"] * pct)
    sim["UNIDADES_NUEVO"] = sim["UNIDADES_NUEVO"].clip(lower=0)

    sim["VN_NUEVO"] = sim["UNIDADES_NUEVO"] * sim["PRECIO_NUEVO"]
    sim["COSTO_TOTAL_NUEVO"] = sim["UNIDADES_NUEVO"] * sim["COSTO_UNIT"]
    sim["APORTE_NUEVO"] = sim["VN_NUEVO"] - sim["COSTO_TOTAL_NUEVO"]
    sim["MARGEN_NUEVO_PCT"] = np.where(
        sim["VN_NUEVO"] > 0,
        sim["APORTE_NUEVO"] / sim["VN_NUEVO"] * 100,
        0,
    )

    sim["DELTA_VN"] = sim["VN_NUEVO"] - sim["VN_ANUAL"]
    sim["DELTA_APORTE"] = sim["APORTE_NUEVO"] - sim["APORTE_ANUAL"]
    sim["DELTA_UNIDADES"] = sim["UNIDADES_NUEVO"] - sim["UNIDADES_ANUAL"]

    return sim


def render_simulador(conn):
    st.html("<h2 class='sub-header'>Simulador de Rentabilidad</h2>")
    st.caption(
        "Simula el impacto de cambios de precio en margen y volumen, "
        "considerando la elasticidad precio-demanda de cada SKU."
    )

    if st.button("Actualizar Datos Base", key="btn_refresh_sim"):
        st.session_state.pop("sim_base", None)

    if "sim_base" not in st.session_state:
        ventas, maestra = _load_base_data(conn)
        df = _build_base(ventas, maestra)
        st.session_state["sim_base"] = df

    df_base = st.session_state["sim_base"].copy()
    df_base = apply_pm_filter(df_base)
    if df_base.empty:
        st.warning("No se encontraron datos base.")
        return

    has_elast = st.session_state.get("elast_data") is not None
    if not has_elast:
        st.info(
            "No se detecto data de elasticidad. Se usara elasticidad por defecto (-1.0). "
            "Para mejores resultados, ejecuta primero el modulo 'Elasticidad'."
        )

    # Filters
    st.markdown("### Filtros y Parametros")
    fc1, fc2, fc3, fc4 = st.columns(4)
    areas = sorted(df_base["AREA"].dropna().unique()) if "AREA" in df_base.columns else []
    with fc1:
        sel_area = st.multiselect("Area", areas, key="sim_area")
    with fc2:
        _m = df_base[df_base["AREA"].isin(sel_area)] if sel_area else df_base
        sel_linea = st.multiselect("Linea", sorted(_m["LINEA"].dropna().unique()) if "LINEA" in _m.columns else [], key="sim_linea")
    with fc3:
        _m2 = _m[_m["LINEA"].isin(sel_linea)] if sel_linea else _m
        sel_marca = st.multiselect("Marca", sorted(_m2["MARCA"].dropna().unique()) if "MARCA" in _m2.columns else [], key="sim_marca")

    with fc4:
        price_change = st.slider(
            "Cambio de Precio (%)", min_value=-30, max_value=30, value=5, step=1, key="sim_price_slider"
        )

    mask = pd.Series(True, index=df_base.index)
    if sel_area:
        mask &= df_base["AREA"].isin(sel_area)
    if sel_linea:
        mask &= df_base["LINEA"].isin(sel_linea)
    if sel_marca:
        mask &= df_base["MARCA"].isin(sel_marca)
    df_filt = df_base[mask]
    if df_filt.empty:
        st.warning("Sin datos.")
        return

    sim = _simulate(df_filt, price_change)

    st.markdown("---")

    # KPIs
    st.markdown(f"### Escenario: Precio {'+'  if price_change >= 0 else ''}{price_change}%")

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    vn_actual = sim["VN_ANUAL"].sum()
    vn_nuevo = sim["VN_NUEVO"].sum()
    aporte_actual = sim["APORTE_ANUAL"].sum()
    aporte_nuevo = sim["APORTE_NUEVO"].sum()
    und_actual = sim["UNIDADES_ANUAL"].sum()
    und_nuevo = sim["UNIDADES_NUEVO"].sum()

    with c1:
        st.metric("VN Actual", f"${human_format(vn_actual)}")
    with c2:
        delta_vn = vn_nuevo - vn_actual
        st.metric("VN Simulado", f"${human_format(vn_nuevo)}",
                   delta=f"{delta_vn/max(vn_actual,1)*100:+.1f}%")
    with c3:
        st.metric("Aporte Actual", f"${human_format(aporte_actual)}")
    with c4:
        delta_ap = aporte_nuevo - aporte_actual
        st.metric("Aporte Simulado", f"${human_format(aporte_nuevo)}",
                   delta=f"{delta_ap/max(aporte_actual,1)*100:+.1f}%")
    with c5:
        st.metric("Und Actuales", human_format(und_actual))
    with c6:
        delta_und = und_nuevo - und_actual
        st.metric("Und Simuladas", human_format(und_nuevo),
                   delta=f"{delta_und/max(und_actual,1)*100:+.1f}%",
                   delta_color="inverse" if delta_und < 0 else "normal")

    st.markdown("---")

    # ── Chart 1: Sensitivity curve ──
    col1, col2 = st.columns(2)

    with col1:
        curves = []
        for p in range(-20, 21, 2):
            s = _simulate(df_filt, p)
            curves.append({
                "Precio_Change": p,
                "VN": s["VN_NUEVO"].sum(),
                "Aporte": s["APORTE_NUEVO"].sum(),
                "Unidades": s["UNIDADES_NUEVO"].sum(),
            })
        curve_df = pd.DataFrame(curves)

        fig1 = go.Figure(layout=dorel_layout(
            title=dict(text="Curva de Sensibilidad: Aporte vs Precio", font_size=14, x=0.5),
            height=400,
            xaxis=dict(title="Cambio Precio (%)"),
            yaxis=dict(title="Aporte Anual ($)"),
            showlegend=False,
        ))
        fig1.add_trace(go.Scatter(
            x=curve_df["Precio_Change"], y=curve_df["Aporte"],
            mode="lines+markers",
            line=dict(color=COLORS["primary"], width=2.5),
            marker=dict(size=6),
            hovertemplate="Precio: %{x:+d}%<br>Aporte: $%{y:,.0f}<extra></extra>",
        ))
        # Current scenario marker
        fig1.add_trace(go.Scatter(
            x=[price_change], y=[aporte_nuevo],
            mode="markers", marker=dict(color="red", size=14, symbol="diamond"),
            hovertemplate="Escenario actual<br>Precio: %{x:+d}%<br>Aporte: $%{y:,.0f}<extra></extra>",
        ))
        fig1.add_vline(x=0, line_dash="dash", line_color="gray", line_width=1)
        st.plotly_chart(fig1, use_container_width=True)

    # ── Chart 2: Top winners/losers ──
    with col2:
        sim["DELTA_APORTE_ABS"] = sim["DELTA_APORTE"].abs()
        top = sim.nlargest(15, "DELTA_APORTE_ABS").copy()
        nombre_col = "SKU_NOM_PRODUCTO" if "SKU_NOM_PRODUCTO" in top.columns else "SKU_PRODUCTO"
        top["LABEL"] = top["SKU_PRODUCTO"].astype(str) + " - " + top[nombre_col].astype(str)
        top["LABEL"] = top["LABEL"].str[:40]
        top = top.sort_values("DELTA_APORTE", ascending=True)
        bar_colors = ["#43A047" if v >= 0 else "#E53935" for v in top["DELTA_APORTE"]]

        fig2 = go.Figure(layout=dorel_layout(
            title=dict(text="Top 15 SKUs: Impacto en Aporte", font_size=14, x=0.5),
            height=450,
            xaxis=dict(title="Delta Aporte ($)"),
            yaxis=dict(title=""),
            margin=dict(l=250, r=20, t=50, b=40),
            showlegend=False,
        ))
        fig2.add_trace(go.Bar(
            x=top["DELTA_APORTE"], y=top["LABEL"],
            orientation="h", marker_color=bar_colors,
            hovertemplate=(
                "SKU: %{customdata[0]}<br>"
                "Delta Aporte: $%{x:,.0f}<br>"
                "Elasticidad: %{customdata[1]:.2f}<br>"
                "Cambio Demanda: %{customdata[2]:.0f}%<extra></extra>"
            ),
            customdata=np.column_stack([
                top["SKU_PRODUCTO"], top["ELASTICIDAD"], top["DEMAND_CHANGE_PCT"],
            ]),
        ))
        st.plotly_chart(fig2, use_container_width=True)

    st.markdown("---")

    # ── Chart 3: Grouped bar by dimension ──
    dim = st.radio("Impacto por", ["AREA", "LINEA", "MARCA"], horizontal=True, key="sim_dim")
    if dim in sim.columns:
        by_dim = sim.groupby(dim, as_index=False).agg(
            VN_ACTUAL=("VN_ANUAL", "sum"),
            VN_NUEVO=("VN_NUEVO", "sum"),
            APORTE_ACTUAL=("APORTE_ANUAL", "sum"),
            APORTE_NUEVO=("APORTE_NUEVO", "sum"),
        )
        by_dim = by_dim.sort_values("APORTE_ACTUAL", ascending=False).head(15)

        fig3 = go.Figure(layout=dorel_layout(
            title=dict(text=f"Aporte Actual vs Simulado por {dim.title()}", font_size=14, x=0.5),
            height=400,
            xaxis=dict(title=""),
            yaxis=dict(title="Aporte ($)"),
            barmode="group",
            legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="center", x=0.5),
        ))
        fig3.add_trace(go.Bar(
            x=by_dim[dim], y=by_dim["APORTE_ACTUAL"],
            name="Actual", marker_color=COLORS["primary"],
            hovertemplate="%{x}<br>Aporte Actual: $%{y:,.0f}<extra></extra>",
        ))
        fig3.add_trace(go.Bar(
            x=by_dim[dim], y=by_dim["APORTE_NUEVO"],
            name="Simulado", marker_color=COLORS["tertiary_teal"],
            hovertemplate="%{x}<br>Aporte Simulado: $%{y:,.0f}<extra></extra>",
        ))
        st.plotly_chart(fig3, use_container_width=True)

    st.markdown("---")
    st.markdown("### Detalle de Simulacion")
    display_cols = ["SKU_PRODUCTO", "PRECIO_PROM", "PRECIO_NUEVO", "ELASTICIDAD",
                    "UNIDADES_ANUAL", "UNIDADES_NUEVO", "DEMAND_CHANGE_PCT",
                    "VN_ANUAL", "VN_NUEVO", "DELTA_VN",
                    "APORTE_ANUAL", "APORTE_NUEVO", "DELTA_APORTE",
                    "MARGEN_PCT", "MARGEN_NUEVO_PCT"]
    for c in ["AREA", "LINEA", "MARCA", "SKU_NOM_PRODUCTO", "SEGMENTO"]:
        if c in sim.columns:
            display_cols.insert(1, c)
    display_cols = [c for c in display_cols if c in sim.columns]
    st.dataframe(sim[display_cols].sort_values("DELTA_APORTE").reset_index(drop=True),
                 use_container_width=True, height=500)
    download_buttons(sim[display_cols], "simulacion_rentabilidad")
