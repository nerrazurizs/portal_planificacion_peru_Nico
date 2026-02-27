"""Analisis ABC-XYZ-FSN — Clasificacion de SKUs por contribucion, variabilidad y volumen.

ABC: Pareto sobre aporte (margen). A=80%, B=80-95%, C=bottom 5%.
XYZ: Coeficiente de variacion semanal. X (CV<0.5), Y (0.5-1.0), Z (>1.0).
FSN: Pareto sobre unidades vendidas. F (Fast)=80%, S (Slow)=80-95%, N (Non-moving)=5%.

La clasificacion se calcula centralizadamente en db/cache.abc_xyz_fsn()
y se actualiza automaticamente 1x/dia (cache 24 h).
"""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format
from utils.export import download_buttons
from config import COLORS, dorel_layout, apply_pm_filter


# ============================================================================
# CONSTANTS
# ============================================================================

ABC_COLORS = {"A": "#065E8B", "B": "#2DAAFF", "C": "#B0AEAA"}
XYZ_COLORS = {"X": "#43A047", "Y": "#FB8C00", "Z": "#E53935"}
FSN_COLORS = {"F": "#1B5E20", "S": "#FB8C00", "N": "#B71C1C"}

# 3x3 matrix colors (darker = more critical from supply perspective)
MATRIX_COLORS = {
    "AX": "#1B5E20", "AY": "#FB8C00", "AZ": "#C62828",
    "BX": "#388E3C", "BY": "#F9A825", "BZ": "#E53935",
    "CX": "#81C784", "CY": "#FFE082", "CZ": "#EF9A9A",
}

MATRIX_DESCRIPTIONS = {
    "AX": "Alto valor, demanda estable — Gestion precisa de inventario",
    "AY": "Alto valor, demanda variable — Requiere stock de seguridad",
    "AZ": "Alto valor, demanda impredecible — Mayor riesgo, planificacion critica",
    "BX": "Valor medio, demanda estable — Gestion estandar",
    "BY": "Valor medio, demanda variable — Monitoreo periodico",
    "BZ": "Valor medio, demanda impredecible — Evaluar si mantener",
    "CX": "Bajo valor, demanda estable — Automatizar reposicion",
    "CY": "Bajo valor, demanda variable — Minimizar inventario",
    "CZ": "Bajo valor, demanda impredecible — Evaluar discontinuar",
}

FSN_DESCRIPTIONS = {
    "F": "Fast — Alta rotacion (top 80% unidades vendidas). Priorizar disponibilidad.",
    "S": "Slow — Rotacion moderada (80-95% acumulado). Monitorear periodicidad.",
    "N": "Non-moving — Baja rotacion (bottom 5%). Evaluar obsolescencia / descontinuacion.",
}


# ============================================================================
# HELPERS
# ============================================================================

def _load_classified_data(conn):
    """Load centralized ABC-XYZ-FSN + enrich with maestra dimensions."""
    df = cq.abc_xyz_fsn(conn)
    if df.empty:
        return df

    # Enrich with product master dimensions
    df_maestra = cq.maestra(conn)
    maestra_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                    "SKU_NOM_PRODUCTO", "PROCEDENCIA", "MIX_OFICIAL", "ULTIMO_INGRESO_CD"]
    available = [c for c in maestra_cols if c in df_maestra.columns]
    if available:
        maestra_dedup = df_maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
        df = df.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")

    return df


def _get_antiguedad_bucket(meses):
    """Clasifica meses de antigüedad en rangos (igual que stock critico)."""
    if pd.isna(meses):
        return "Sin Info"
    if meses < 3:
        return "< 3m"
    if meses < 6:
        return "3-6m"
    if meses < 12:
        return "6-12m"
    if meses < 24:
        return "12-24m"
    return "> 24m"


ORDEN_ANTIGUEDAD = ["< 3m", "3-6m", "6-12m", "12-24m", "> 24m", "Sin Info"]


def _add_antiguedad(df):
    """Agrega ANTIGUEDAD_MESES y RANGO_ANTIGUEDAD al df si ULTIMO_INGRESO_CD existe."""
    if "ULTIMO_INGRESO_CD" not in df.columns:
        return df
    df = df.copy()
    hoy = pd.Timestamp.today().normalize()
    df["ULTIMO_INGRESO_CD"] = pd.to_datetime(df["ULTIMO_INGRESO_CD"], errors="coerce")
    df["ANTIGUEDAD_MESES"] = ((hoy - df["ULTIMO_INGRESO_CD"]) / pd.Timedelta(days=30.44)).round(1)
    df["RANGO_ANTIGUEDAD"] = df["ANTIGUEDAD_MESES"].apply(_get_antiguedad_bucket)
    df["RANGO_ANTIGUEDAD"] = pd.Categorical(df["RANGO_ANTIGUEDAD"], categories=ORDEN_ANTIGUEDAD, ordered=True)
    return df


# ============================================================================
# DASHBOARD CHARTS
# ============================================================================

def _render_kpis(df):
    """KPI cards for ABC-XYZ-FSN summary."""
    total = len(df)
    a_pct = (df["CLASE_ABC"] == "A").sum() / max(total, 1) * 100
    b_pct = (df["CLASE_ABC"] == "B").sum() / max(total, 1) * 100
    c_pct = (df["CLASE_ABC"] == "C").sum() / max(total, 1) * 100
    x_pct = (df["CLASE_XYZ"] == "X").sum() / max(total, 1) * 100
    z_pct = (df["CLASE_XYZ"] == "Z").sum() / max(total, 1) * 100
    f_pct = (df["CLASE_FSN"] == "F").sum() / max(total, 1) * 100 if "CLASE_FSN" in df.columns else 0
    s_pct = (df["CLASE_FSN"] == "S").sum() / max(total, 1) * 100 if "CLASE_FSN" in df.columns else 0
    n_pct = (df["CLASE_FSN"] == "N").sum() / max(total, 1) * 100 if "CLASE_FSN" in df.columns else 0

    # Row 1: ABC + XYZ + Total
    cols = st.columns(6)
    labels = ["Total SKUs", "Clase A", "Clase B", "Clase C", "Clase X", "Clase Z"]
    values = [f"{total:,}", f"{a_pct:.0f}%", f"{b_pct:.0f}%", f"{c_pct:.0f}%", f"{x_pct:.0f}%", f"{z_pct:.0f}%"]
    colors = [COLORS["primary"], ABC_COLORS["A"], ABC_COLORS["B"], ABC_COLORS["C"],
              XYZ_COLORS["X"], XYZ_COLORS["Z"]]

    for col, label, value, color in zip(cols, labels, values, colors):
        with col:
            st.html(f"""
            <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                         border-top:4px solid {color};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                <div style='font-size:2rem;font-weight:bold;color:{color}'>{value}</div>
                <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{label}</div>
            </div>""")

    # Row 2: FSN
    if "CLASE_FSN" in df.columns:
        fsn_cols = st.columns(3)
        fsn_labels = ["Fast (F)", "Slow (S)", "Non-moving (N)"]
        fsn_values = [f"{f_pct:.0f}%", f"{s_pct:.0f}%", f"{n_pct:.0f}%"]
        fsn_colors = [FSN_COLORS["F"], FSN_COLORS["S"], FSN_COLORS["N"]]

        for col, label, value, color in zip(fsn_cols, fsn_labels, fsn_values, fsn_colors):
            with col:
                st.html(f"""
                <div style='background:#fff;padding:0.8rem;border-radius:10px;text-align:center;
                             border-top:4px solid {color};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                    <div style='font-size:1.5rem;font-weight:bold;color:{color}'>{value}</div>
                    <div style='font-size:0.8rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{label}</div>
                </div>""")

    st.html("<br>")


def _render_pareto(df, value_col="APORTE_TOTAL", class_col="CLASE_ABC",
                   color_map=None, title="Curva de Pareto ABC (Top 50 SKUs)",
                   y_label="Aporte ($)"):
    """Pareto chart: bars = value per SKU, line = cumulative %."""
    if color_map is None:
        color_map = ABC_COLORS
    top_n = min(50, len(df))
    pareto = df.nlargest(top_n, value_col).reset_index(drop=True)
    pareto["IDX"] = range(1, len(pareto) + 1)

    total = df[value_col].sum()
    pareto["CUM_PCT_GLOBAL"] = pareto[value_col].cumsum() / max(total, 1) * 100

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    for cls in sorted(pareto[class_col].dropna().unique()):
        mask = pareto[class_col] == cls
        sub = pareto[mask]
        if sub.empty:
            continue
        cum_col = "APORTE_CUM_PCT" if "APORTE_CUM_PCT" in sub.columns else "CUM_PCT_GLOBAL"
        fig.add_trace(
            go.Bar(
                x=sub["IDX"],
                y=sub[value_col],
                name=f"Clase {cls}",
                marker_color=color_map.get(cls, "#999"),
                customdata=np.column_stack([
                    sub["SKU_PRODUCTO"], sub["CUM_PCT_GLOBAL"]
                ]),
                hovertemplate=(
                    "SKU: %{customdata[0]}<br>"
                    f"{y_label}: %{{y:,.0f}}<br>"
                    "Acum: %{customdata[1]:.1f}%<extra></extra>"
                ),
            ),
            secondary_y=False,
        )

    fig.add_trace(
        go.Scatter(
            x=pareto["IDX"],
            y=pareto["CUM_PCT_GLOBAL"],
            mode="lines",
            name="% Acumulado",
            line=dict(color="#E53935", width=2),
            hovertemplate="%{y:.1f}%<extra></extra>",
        ),
        secondary_y=True,
    )

    for pct in [80, 95]:
        fig.add_hline(
            y=pct, secondary_y=True,
            line_dash="dash", line_color="gray", line_width=1,
            annotation_text=f"{pct}%", annotation_position="top left",
        )

    fig.update_layout(
        **dorel_layout(title=title, height=400, barmode="stack")
    )
    fig.update_xaxes(title_text="SKU (Ranking)", showticklabels=False)
    fig.update_yaxes(title_text=y_label, secondary_y=False)
    fig.update_yaxes(title_text="% Acumulado", secondary_y=True, range=[0, 105])

    st.plotly_chart(fig, use_container_width=True)


def _render_matrix_heatmap(df):
    """3x3 ABC-XYZ matrix heatmap."""
    matrix = df.groupby("CLASE_COMBINADA", as_index=False).agg(
        COUNT=("SKU_PRODUCTO", "count"),
        APORTE=("APORTE_TOTAL", "sum"),
    )
    total_aporte = matrix["APORTE"].sum()
    matrix["PCT_APORTE"] = (matrix["APORTE"] / max(total_aporte, 1) * 100).round(1)
    matrix["PCT_SKUS"] = (matrix["COUNT"] / max(len(df), 1) * 100).round(1)

    all_combos = [f"{a}{x}" for a in ["A", "B", "C"] for x in ["X", "Y", "Z"]]
    full = pd.DataFrame({"CLASE_COMBINADA": all_combos})
    matrix = full.merge(matrix, on="CLASE_COMBINADA", how="left").fillna(0)

    matrix["ABC"] = matrix["CLASE_COMBINADA"].str[0]
    matrix["XYZ"] = matrix["CLASE_COMBINADA"].str[1]
    matrix["LABEL"] = matrix.apply(
        lambda r: f"{int(r['COUNT'])} SKUs\n{r['PCT_APORTE']:.0f}% aporte", axis=1
    )

    xyz_order = ["X", "Y", "Z"]
    abc_order = ["A", "B", "C"]
    pivot_count = matrix.pivot(index="ABC", columns="XYZ", values="COUNT").reindex(
        index=abc_order, columns=xyz_order, fill_value=0
    )
    pivot_label = matrix.pivot(index="ABC", columns="XYZ", values="LABEL").reindex(
        index=abc_order, columns=xyz_order, fill_value=""
    )

    count_threshold = matrix["COUNT"].quantile(0.7)

    fig = go.Figure(
        data=go.Heatmap(
            z=pivot_count.values,
            x=xyz_order,
            y=abc_order,
            colorscale="Blues",
            colorbar=dict(title="# SKUs"),
            hovertemplate="Clase: %{y}%{x}<br>SKUs: %{z}<extra></extra>",
        )
    )

    for i, abc_val in enumerate(abc_order):
        for j, xyz_val in enumerate(xyz_order):
            cnt = pivot_count.values[i][j]
            lbl = pivot_label.values[i][j]
            fig.add_annotation(
                x=xyz_val, y=abc_val, text=str(lbl).replace("\n", "<br>"),
                showarrow=False,
                font=dict(
                    size=12,
                    color="white" if cnt > count_threshold else "black",
                ),
            )

    fig.update_layout(
        **dorel_layout(
            title="Matriz ABC-XYZ",
            height=350,
            xaxis=dict(title="XYZ (Variabilidad)", side="top", gridcolor="#ECECEC"),
            yaxis=dict(title="ABC (Contribucion)", autorange="reversed", gridcolor="#ECECEC"),
        )
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("Descripcion de cada celda"):
        for combo, desc in MATRIX_DESCRIPTIONS.items():
            cnt = int(matrix.loc[matrix["CLASE_COMBINADA"] == combo, "COUNT"].values[0])
            st.markdown(f"**{combo}** ({cnt} SKUs): {desc}")


def _render_scatter(df):
    """Scatter: CV vs Aporte, colored by ABC class."""
    plot_df = df[df["CV"] < 10].copy() if "CV" in df.columns else pd.DataFrame()
    if plot_df.empty:
        return

    fig = go.Figure()

    for cls in ["A", "B", "C"]:
        sub = plot_df[plot_df["CLASE_ABC"] == cls]
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub["CV"],
            y=sub["APORTE_TOTAL"],
            mode="markers",
            name=f"Clase {cls}",
            marker=dict(color=ABC_COLORS[cls], size=7, opacity=0.7),
            customdata=np.column_stack([sub["SKU_PRODUCTO"], sub["CLASE_COMBINADA"]]),
            hovertemplate=(
                "SKU: %{customdata[0]}<br>"
                "Aporte: $%{y:,.0f}<br>"
                "CV: %{x:.2f}<br>"
                "Clase: %{customdata[1]}<extra></extra>"
            ),
        ))

    fig.add_vline(x=0.5, line_dash="dash", line_color="gray", line_width=1)
    fig.add_vline(x=1.0, line_dash="dash", line_color="gray", line_width=1)

    cv_max = min(5, plot_df["CV"].quantile(0.99))
    fig.update_layout(
        **dorel_layout(
            title="Scatter: CV vs Aporte (escala log)",
            height=400,
            xaxis=dict(title="Coeficiente de Variacion (CV)", range=[0, cv_max], gridcolor="#ECECEC"),
            yaxis=dict(title="Aporte Anual ($)", type="log", gridcolor="#ECECEC"),
        )
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_distribution_by_dim(df):
    """Stacked bars: ABC distribution by Area or Linea."""
    dim_choice = st.radio(
        "Dimension", ["AREA", "LINEA", "MARCA"], horizontal=True, key="abc_dim_radio"
    )

    if dim_choice not in df.columns:
        st.info(f"Columna {dim_choice} no disponible.")
        return

    grp = df.groupby([dim_choice, "CLASE_ABC"], as_index=False).agg(
        COUNT=("SKU_PRODUCTO", "count"),
        APORTE=("APORTE_TOTAL", "sum"),
    )

    dim_totals = grp.groupby(dim_choice, as_index=False)["COUNT"].sum().sort_values("COUNT", ascending=False)
    dim_order = dim_totals[dim_choice].tolist()

    fig = go.Figure()
    for cls in ["A", "B", "C"]:
        sub = grp[grp["CLASE_ABC"] == cls]
        if sub.empty:
            continue
        sub_indexed = sub.set_index(dim_choice).reindex(dim_order).fillna(0).reset_index()
        fig.add_trace(go.Bar(
            x=sub_indexed[dim_choice],
            y=sub_indexed["COUNT"],
            name=f"Clase {cls}",
            marker_color=ABC_COLORS[cls],
            hovertemplate=f"{dim_choice}: %{{x}}<br>Clase {cls}<br>SKUs: %{{y}}<extra></extra>",
        ))

    fig.update_layout(
        **dorel_layout(
            title=f"Distribucion ABC por {dim_choice.title()}",
            height=400,
            barmode="stack",
            barnorm="percent",
            xaxis=dict(title=dim_choice.title(), gridcolor="#ECECEC"),
            yaxis=dict(title="% SKUs", gridcolor="#ECECEC"),
        )
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_fsn_pareto(df):
    """Pareto chart for FSN classification (by units)."""
    if "CLASE_FSN" not in df.columns or "UNIDADES_TOTAL" not in df.columns:
        return
    _render_pareto(
        df, value_col="UNIDADES_TOTAL", class_col="CLASE_FSN",
        color_map=FSN_COLORS,
        title="Curva de Pareto FSN (Top 50 SKUs por Unidades)",
        y_label="Unidades",
    )


def _render_fsn_summary(df):
    """FSN summary: bar chart by class + description."""
    if "CLASE_FSN" not in df.columns:
        return

    grp = df.groupby("CLASE_FSN", as_index=False).agg(
        SKUs=("SKU_PRODUCTO", "count"),
        Unidades=("UNIDADES_TOTAL", "sum"),
        Aporte=("APORTE_TOTAL", "sum"),
    )
    total_u = grp["Unidades"].sum()
    total_a = grp["Aporte"].sum()
    grp["% Unidades"] = (grp["Unidades"] / max(total_u, 1) * 100).round(1)
    grp["% Aporte"] = (grp["Aporte"] / max(total_a, 1) * 100).round(1)

    # Ensure F, S, N order
    grp["CLASE_FSN"] = pd.Categorical(grp["CLASE_FSN"], categories=["F", "S", "N"], ordered=True)
    grp = grp.sort_values("CLASE_FSN")

    fig = go.Figure()
    for _, row in grp.iterrows():
        cls = row["CLASE_FSN"]
        fig.add_trace(go.Bar(
            x=[cls],
            y=[row["SKUs"]],
            marker_color=FSN_COLORS.get(cls, "#999"),
            text=[f"{row['SKUs']:,} SKUs<br>{row['% Unidades']:.0f}% und"],
            textposition="outside",
            textfont=dict(size=11),
            hovertemplate=(
                f"Clase: {cls}<br>"
                f"SKUs: {int(row['SKUs']):,}<br>"
                f"Unidades: {int(row['Unidades']):,} ({row['% Unidades']:.1f}%)<br>"
                f"Aporte: ${int(row['Aporte']):,} ({row['% Aporte']:.1f}%)<extra></extra>"
            ),
            showlegend=False,
        ))

    fig.update_layout(
        **dorel_layout(
            title="Distribucion FSN (Fast / Slow / Non-moving)",
            height=350,
            xaxis=dict(title="Clase FSN", gridcolor="#ECECEC"),
            yaxis=dict(title="# SKUs", gridcolor="#ECECEC"),
        )
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("Descripcion clases FSN"):
        for cls, desc in FSN_DESCRIPTIONS.items():
            cnt = int(grp.loc[grp["CLASE_FSN"] == cls, "SKUs"].sum()) if cls in grp["CLASE_FSN"].values else 0
            st.markdown(f"**{cls}** ({cnt} SKUs): {desc}")


# ============================================================================
# ANALISIS CZ
# ============================================================================

def _render_cz_analysis(df_filt):
    """Seccion dedicada al analisis de SKUs CZ: breakdown por Area/Linea y antiguedad."""
    df_cz = df_filt[df_filt["CLASE_COMBINADA"] == "CZ"].copy()

    if df_cz.empty:
        st.info("No hay SKUs CZ con los filtros actuales.")
        return

    df_cz = _add_antiguedad(df_cz)

    total_cz = len(df_cz)
    pct_cz = total_cz / max(len(df_filt), 1) * 100
    aporte_cz = df_cz["APORTE_TOTAL"].sum()
    pct_aporte_cz = aporte_cz / max(df_filt["APORTE_TOTAL"].sum(), 1) * 100

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("SKUs CZ", f"{total_cz:,}")
    k2.metric("% del total SKUs", f"{pct_cz:.1f}%")
    k3.metric("Aporte CZ", f"${aporte_cz:,.0f}")
    k4.metric("% del aporte total", f"{pct_aporte_cz:.1f}%")

    st.html("<br>")

    # -- Breakdown por Area y Linea --
    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("**SKUs CZ por Area**")
        if "AREA" in df_cz.columns:
            area_grp = (
                df_cz.groupby("AREA", as_index=False)
                .agg(SKUs=("SKU_PRODUCTO", "count"), Aporte=("APORTE_TOTAL", "sum"))
                .sort_values("SKUs", ascending=False)
            )
            area_grp["% SKUs"] = (area_grp["SKUs"] / total_cz * 100).round(1)
            fig_area = go.Figure(go.Bar(
                x=area_grp["SKUs"],
                y=area_grp["AREA"],
                orientation="h",
                marker_color="#C62828",
                customdata=np.column_stack([area_grp["% SKUs"], area_grp["Aporte"]]),
                hovertemplate=(
                    "Area: %{y}<br># SKUs: %{x}<br>"
                    "%% del CZ: %{customdata[0]:.1f}%<br>"
                    "Aporte: $%{customdata[1]:,.0f}<extra></extra>"
                ),
            ))
            fig_area.update_layout(
                **dorel_layout(
                    height=max(len(area_grp) * 35, 120),
                    xaxis=dict(title="# SKUs CZ"),
                    yaxis=dict(autorange="reversed", title=""),
                ),
            )
            st.plotly_chart(fig_area, use_container_width=True)
        else:
            st.info("Columna AREA no disponible.")

    with col_b:
        st.markdown("**SKUs CZ por Linea (Top 15)**")
        if "LINEA" in df_cz.columns:
            linea_grp = (
                df_cz.groupby("LINEA", as_index=False)
                .agg(SKUs=("SKU_PRODUCTO", "count"), Aporte=("APORTE_TOTAL", "sum"))
                .sort_values("SKUs", ascending=False)
                .head(15)
            )
            linea_grp["% SKUs"] = (linea_grp["SKUs"] / total_cz * 100).round(1)
            fig_linea = go.Figure(go.Bar(
                x=linea_grp["SKUs"],
                y=linea_grp["LINEA"],
                orientation="h",
                marker_color="#E53935",
                customdata=np.column_stack([linea_grp["% SKUs"], linea_grp["Aporte"]]),
                hovertemplate=(
                    "Linea: %{y}<br># SKUs: %{x}<br>"
                    "%% del CZ: %{customdata[0]:.1f}%<br>"
                    "Aporte: $%{customdata[1]:,.0f}<extra></extra>"
                ),
            ))
            fig_linea.update_layout(
                **dorel_layout(
                    height=max(len(linea_grp) * 35, 120),
                    xaxis=dict(title="# SKUs CZ"),
                    yaxis=dict(autorange="reversed", title=""),
                ),
            )
            st.plotly_chart(fig_linea, use_container_width=True)
        else:
            st.info("Columna LINEA no disponible.")

    # -- Segmentacion por Antiguedad --
    st.markdown("**SKUs CZ por Antiguedad (ultimo ingreso a CD)**")

    if "RANGO_ANTIGUEDAD" in df_cz.columns:
        antig_grp = (
            df_cz.groupby("RANGO_ANTIGUEDAD", as_index=False, observed=False)
            .agg(SKUs=("SKU_PRODUCTO", "count"), Aporte=("APORTE_TOTAL", "sum"))
        )
        antig_grp["% SKUs"] = (antig_grp["SKUs"] / total_cz * 100).round(1)

        color_map = {
            "< 3m":    "#43A047",
            "3-6m":    "#8BC34A",
            "6-12m":   "#FB8C00",
            "12-24m":  "#E53935",
            "> 24m":   "#B71C1C",
            "Sin Info": "#9E9E9E",
        }
        antig_grp["color"] = antig_grp["RANGO_ANTIGUEDAD"].astype(str).map(color_map)

        fig_antig = go.Figure()
        for _, row in antig_grp.iterrows():
            rango = str(row["RANGO_ANTIGUEDAD"])
            fig_antig.add_trace(go.Bar(
                x=[rango],
                y=[row["SKUs"]],
                marker_color=color_map.get(rango, "#9E9E9E"),
                text=[f"{row['% SKUs']:.0f}%"],
                textposition="outside",
                textfont=dict(size=12),
                hovertemplate=(
                    f"Rango: {rango}<br>"
                    f"# SKUs: {int(row['SKUs'])}<br>"
                    f"% del CZ: {row['% SKUs']:.1f}%<br>"
                    f"Aporte: ${row['Aporte']:,.0f}<extra></extra>"
                ),
                showlegend=False,
            ))

        fig_antig.update_layout(
            **dorel_layout(
                height=280,
                xaxis=dict(
                    title="Antiguedad (meses desde ultimo ingreso CD)",
                    categoryorder="array",
                    categoryarray=[str(c) for c in ORDEN_ANTIGUEDAD],
                    gridcolor="#ECECEC",
                ),
                yaxis=dict(title="# SKUs CZ", gridcolor="#ECECEC"),
            ),
        )
        st.plotly_chart(fig_antig, use_container_width=True)

        sin_historia = int(antig_grp.loc[antig_grp["RANGO_ANTIGUEDAD"].astype(str) == "< 3m", "SKUs"].sum())
        viejos = int(antig_grp.loc[antig_grp["RANGO_ANTIGUEDAD"].astype(str).isin(["> 24m", "Sin Info"]), "SKUs"].sum())
        st.caption(
            f"📌 {sin_historia} SKUs CZ tienen menos de 3 meses en CD (posible falta de historia). "
            f"{viejos} tienen mas de 24 meses o sin fecha (potenciales candidatos a descontinuar)."
        )

        st.html("<br>")

        with st.expander(f"Ver detalle de los {total_cz} SKUs CZ", expanded=False):
            cz_cols = ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA",
                       "CLASE_FSN", "APORTE_TOTAL", "APORTE_PCT", "CV", "VENTA_SEMANAL_PROM",
                       "SEMANAS_CON_VENTA", "ANTIGUEDAD_MESES", "RANGO_ANTIGUEDAD"]
            cz_cols = [c for c in cz_cols if c in df_cz.columns]
            df_cz_display = (
                df_cz[cz_cols]
                .sort_values("APORTE_TOTAL", ascending=False)
                .reset_index(drop=True)
            )
            st.dataframe(df_cz_display, use_container_width=True, height=450)
            download_buttons(df_cz_display, "abc_xyz_cz_detalle")
    else:
        st.info("ULTIMO_INGRESO_CD no disponible en la maestra.")


# ============================================================================
# MAIN RENDER
# ============================================================================

def render_abc_xyz(conn):
    """Main entry point: renders the ABC-XYZ-FSN analysis dashboard."""
    st.html("<h2 class='sub-header'>Analisis ABC-XYZ-FSN</h2>")
    st.caption(
        "ABC: clasificacion por contribucion (aporte) — A=80%, B=15%, C=5%. "
        "XYZ: clasificacion por variabilidad semanal — X (CV<0.5), Y (0.5-1.0), Z (>1.0). "
        "FSN: clasificacion por volumen (unidades) — F (Fast, top 80%), S (Slow, 80-95%), N (Non-moving, bottom 5%). "
        "⚠️ Aplica solo a productos con MIX_OFICIAL activo. "
        "Se actualiza automaticamente 1 vez al dia (cache 24 h)."
    )

    # Load from centralized cache (auto-computed at app startup)
    df = _load_classified_data(conn)
    df = apply_pm_filter(df)

    if df.empty:
        st.warning("No se encontraron datos suficientes para la clasificacion.")
        return

    # ── Filters ──────────────────────────────────────────────────────
    st.markdown("### Filtros")
    fc1, fc2, fc3, fc4 = st.columns(4)

    areas = sorted(df["AREA"].dropna().unique()) if "AREA" in df.columns else []
    lineas_all = sorted(df["LINEA"].dropna().unique()) if "LINEA" in df.columns else []
    marcas_all = sorted(df["MARCA"].dropna().unique()) if "MARCA" in df.columns else []

    with fc1:
        sel_area = st.multiselect("Area", areas, key="abc_area")
    with fc2:
        lineas_filt = sorted(df[df["AREA"].isin(sel_area)]["LINEA"].dropna().unique()) if sel_area else lineas_all
        sel_linea = st.multiselect("Linea", lineas_filt, key="abc_linea")
    with fc3:
        _m = df.copy()
        if sel_area:
            _m = _m[_m["AREA"].isin(sel_area)]
        if sel_linea:
            _m = _m[_m["LINEA"].isin(sel_linea)]
        marcas_filt = sorted(_m["MARCA"].dropna().unique()) if "MARCA" in _m.columns else marcas_all
        sel_marca = st.multiselect("Marca", marcas_filt, key="abc_marca")
    with fc4:
        sel_fsn = st.multiselect("FSN", ["F", "S", "N"], default=[], key="abc_fsn")

    # Apply filters
    mask = pd.Series(True, index=df.index)
    if sel_area:
        mask &= df["AREA"].isin(sel_area)
    if sel_linea:
        mask &= df["LINEA"].isin(sel_linea)
    if sel_marca:
        mask &= df["MARCA"].isin(sel_marca)
    if sel_fsn and "CLASE_FSN" in df.columns:
        mask &= df["CLASE_FSN"].isin(sel_fsn)
    df_filt = df[mask]

    if df_filt.empty:
        st.warning("No hay datos con los filtros seleccionados.")
        return

    st.markdown("---")

    # ── KPIs ─────────────────────────────────────────────────────────
    _render_kpis(df_filt)

    # ── Charts Row 1: Pareto ABC + Matrix ────────────────────────────
    col_left, col_right = st.columns([3, 2])
    with col_left:
        _render_pareto(df_filt)
    with col_right:
        _render_matrix_heatmap(df_filt)

    st.markdown("---")

    # ── Charts Row 2: Scatter + Distribution ─────────────────────────
    col_scatter, col_dist = st.columns(2)
    with col_scatter:
        _render_scatter(df_filt)
    with col_dist:
        _render_distribution_by_dim(df_filt)

    st.markdown("---")

    # ── FSN Section ──────────────────────────────────────────────────
    if "CLASE_FSN" in df_filt.columns:
        st.markdown("### 📦 Analisis FSN — Rotacion por volumen")
        st.caption(
            "FSN clasifica SKUs por volumen de unidades vendidas (Pareto). "
            "Complementa ABC (que usa aporte/margen) con una perspectiva de rotacion fisica."
        )
        col_fsn1, col_fsn2 = st.columns(2)
        with col_fsn1:
            _render_fsn_summary(df_filt)
        with col_fsn2:
            _render_fsn_pareto(df_filt)

        st.markdown("---")

    # ── Detail Table ─────────────────────────────────────────────────
    st.markdown("### Detalle por SKU")

    display_cols = ["SKU_PRODUCTO", "CLASE_COMBINADA", "CLASE_ABC", "CLASE_XYZ", "CLASE_FSN",
                    "APORTE_TOTAL", "APORTE_PCT", "APORTE_CUM_PCT", "CV",
                    "VENTA_SEMANAL_PROM", "SEMANAS_CON_VENTA",
                    "VN_TOTAL", "UNIDADES_TOTAL", "RANK_ABC"]
    for c in ["AREA", "LINEA", "SUBLINEA", "MARCA", "SKU_NOM_PRODUCTO"]:
        if c in df_filt.columns:
            display_cols.insert(1, c)

    display_cols = [c for c in display_cols if c in df_filt.columns]
    df_display = df_filt[display_cols].sort_values("RANK_ABC").reset_index(drop=True)

    st.dataframe(df_display, use_container_width=True, height=500)

    # ── Export ────────────────────────────────────────────────────────
    download_buttons(df_display, "abc_xyz_fsn_analisis")

    # ── Analisis CZ ──────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 🔴 Analisis SKUs CZ — Bajo aporte, demanda impredecible")
    st.caption("Segmentacion por Area, Linea y antiguedad (ultimo ingreso a CD) para entender la naturaleza de los SKUs CZ.")
    _render_cz_analysis(df_filt)
