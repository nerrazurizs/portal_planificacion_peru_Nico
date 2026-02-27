"""Higiene de Abastecimiento - Deteccion de inconsistencias en el proceso de supply."""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format
from utils.export import download_buttons
from config import COLORS, dorel_layout, apply_pm_filter
from utils.ui_animations import lottie_spinner


# ============================================================================
# CONSTANTS
# ============================================================================

CANAL_MAP = {
    "MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL",
    "TIENDA": "TIENDA", "MAYORISTA": "MAYORISTA",
}

ISSUE_COLORS = {
    "SIN_FORECAST": "#E53935",
    "FORECAST_1_CANAL": "#FB8C00",
    "STOCK_CD_SIN_PERFIL": "#9C27B0",
    "OK": "#43A047",
}

ISSUE_LABELS = {
    "SIN_FORECAST": "Sin Forecast (FC=0 en todos los canales)",
    "FORECAST_1_CANAL": "Forecast en solo 1 canal",
    "STOCK_CD_SIN_PERFIL": "Stock en CD Principal sin perfil en tiendas",
}


# ============================================================================
# DATA LOADING
# ============================================================================

def _load_data(conn):
    """Load stock, maestra, and forecast data (centralized cache)."""
    with lottie_spinner("snowflake"):
        stock = cq.stock_higiene(conn)
        maestra = cq.maestra(conn)
    return stock, maestra


def _get_forecast_data():
    """Try to get forecast data from projection session_state or return None."""
    df_proy = st.session_state.get("df_proy")
    if df_proy is not None and isinstance(df_proy, pd.DataFrame) and not df_proy.empty:
        return df_proy.copy()
    return None


# ============================================================================
# CHECK 1: SKUs MIX sin forecast
# ============================================================================

def _check_forecast_coverage(maestra, proy_df):
    """Check that all MIX SKUs have forecast >= 1 in the year.

    Returns DataFrame with:
    - SKU_PRODUCTO, MIX_OFICIAL, AREA, LINEA, MARCA
    - FC_TIENDA, FC_ETAIL, FC_MAYOR, FC_TOTAL (annual totals)
    - N_CANALES_CON_FC (number of channels with FC >= 1)
    - ISSUE: SIN_FORECAST | FORECAST_1_CANAL | OK
    """
    if "MIX_OFICIAL" not in maestra.columns:
        return pd.DataFrame()

    # Get MIX SKUs
    mix_skus = maestra[
        maestra["MIX_OFICIAL"].str.strip().str.upper() == "MIX"
    ][["SKU_PRODUCTO"]].drop_duplicates()

    if mix_skus.empty:
        return pd.DataFrame()

    # Extract forecast from projection data
    if proy_df is not None and not proy_df.empty:
        # Use projection data — aggregate forecast by SKU across all periods
        fc_cols_map = {
            "DEMANDA_SIM_TIENDA": "FC_TIENDA",
            "DEMANDA_SIM_ETAIL": "FC_ETAIL",
            "DEMANDA_SIM_MAYOR": "FC_MAYOR",
            "FORECAST_VENTA_MINOR": "FC_TIENDA",
            "FORECAST_VENTA_ETAIL": "FC_ETAIL",
            "FORECAST_VENTA_MAYOR": "FC_MAYOR",
        }

        # Find available forecast columns
        available_fc = {}
        for src, dst in fc_cols_map.items():
            if src in proy_df.columns and dst not in available_fc.values():
                available_fc[src] = dst

        if not available_fc:
            return pd.DataFrame()

        # Sum forecast across all periods per SKU
        agg_dict = {src: "sum" for src in available_fc.keys()}
        fc_by_sku = proy_df.groupby("SKU_PRODUCTO", as_index=False).agg(agg_dict)
        fc_by_sku = fc_by_sku.rename(columns=available_fc)

    else:
        return pd.DataFrame()

    # Ensure all 3 channels exist
    for col in ["FC_TIENDA", "FC_ETAIL", "FC_MAYOR"]:
        if col not in fc_by_sku.columns:
            fc_by_sku[col] = 0.0

    fc_by_sku["FC_TOTAL"] = (
        fc_by_sku["FC_TIENDA"].fillna(0)
        + fc_by_sku["FC_ETAIL"].fillna(0)
        + fc_by_sku["FC_MAYOR"].fillna(0)
    )

    # Merge with MIX SKUs
    df = mix_skus.merge(fc_by_sku, on="SKU_PRODUCTO", how="left")
    df[["FC_TIENDA", "FC_ETAIL", "FC_MAYOR", "FC_TOTAL"]] = df[
        ["FC_TIENDA", "FC_ETAIL", "FC_MAYOR", "FC_TOTAL"]
    ].fillna(0)

    # Count channels with forecast >= 1
    df["N_CANALES_CON_FC"] = (
        (df["FC_TIENDA"] >= 1).astype(int)
        + (df["FC_ETAIL"] >= 1).astype(int)
        + (df["FC_MAYOR"] >= 1).astype(int)
    )

    # Classify issue
    df["ISSUE"] = np.select(
        [df["FC_TOTAL"] < 1, df["N_CANALES_CON_FC"] == 1],
        ["SIN_FORECAST", "FORECAST_1_CANAL"],
        default="OK",
    )

    # Enrich with maestra
    maestra_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                    "SKU_NOM_PRODUCTO", "MIX_OFICIAL", "PROCEDENCIA"]
    available = [c for c in maestra_cols if c in maestra.columns]
    maestra_dedup = maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
    df = df.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")

    return df


# ============================================================================
# CHECK 2: Stock en CD Principal sin perfil en tiendas
# ============================================================================

def _check_stock_sin_perfil(stock, maestra):
    """Detect SKUs with stock in principal CD warehouses but no store profile.

    'Principal' = descripcion_sucursal contains 'PRINCIPAL'
    (e.g., BLUEXPRESS PRINCIPAL, LOGISTICS PRINCIPAL)
    """
    if stock.empty:
        return pd.DataFrame()

    # Identify principal CDs
    stock["IS_CD_PRINCIPAL"] = (
        stock["CANAL_DE_DISTRIBUCION"].str.upper().str.contains("CD", na=False)
        & stock["DESCRIPCION_SUCURSAL"].str.upper().str.contains("PRINCIPAL", na=False)
    )

    # SKUs with stock in principal CD
    cd_stock = (
        stock[stock["IS_CD_PRINCIPAL"] & (stock["STOCK_UNIDADES"] > 0)]
        .groupby("SKU_PRODUCTO", as_index=False)
        .agg(
            STOCK_CD_PRINCIPAL=("STOCK_UNIDADES", "sum"),
            BODEGAS_CD=("ID_SUCURSAL", "nunique"),
            BODEGAS_CD_LISTA=("DESCRIPCION_SUCURSAL", lambda x: ", ".join(x.unique())),
        )
    )

    if cd_stock.empty:
        return pd.DataFrame()

    # SKUs with perfil in any TIENDA
    tienda_perfil = (
        stock[
            stock["CANAL_DE_DISTRIBUCION"].str.upper().str.contains("TIENDA", na=False)
            & (stock["PERFIL_TIENDAS"] > 0)
        ]
        .groupby("SKU_PRODUCTO", as_index=False)
        .agg(
            TIENDAS_CON_PERFIL=("ID_SUCURSAL", "nunique"),
            PERFIL_TOTAL=("PERFIL_TIENDAS", "sum"),
        )
    )

    # Merge: CD stock LEFT JOIN tienda perfil
    df = cd_stock.merge(tienda_perfil, on="SKU_PRODUCTO", how="left")
    df["TIENDAS_CON_PERFIL"] = df["TIENDAS_CON_PERFIL"].fillna(0).astype(int)
    df["PERFIL_TOTAL"] = df["PERFIL_TOTAL"].fillna(0)

    # Flag: stock in CD but NO tienda profile
    df["SIN_PERFIL"] = df["TIENDAS_CON_PERFIL"] == 0

    # Enrich with maestra
    maestra_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                    "SKU_NOM_PRODUCTO", "MIX_OFICIAL", "PROCEDENCIA", "ULTIMO_COSTO"]
    available = [c for c in maestra_cols if c in maestra.columns]
    maestra_dedup = maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
    df = df.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")

    # Estimate value at risk
    if "ULTIMO_COSTO" in df.columns:
        df["VALOR_STOCK_CD"] = df["STOCK_CD_PRINCIPAL"] * df["ULTIMO_COSTO"].fillna(0)
    else:
        df["VALOR_STOCK_CD"] = 0

    return df


# ============================================================================
# DASHBOARD
# ============================================================================

def _render_summary_kpis(fc_df, perfil_df):
    """Top-level KPI cards summarizing all hygiene issues."""
    # Forecast issues
    sin_fc = (fc_df["ISSUE"] == "SIN_FORECAST").sum() if not fc_df.empty else 0
    fc_1_canal = (fc_df["ISSUE"] == "FORECAST_1_CANAL").sum() if not fc_df.empty else 0
    total_mix = len(fc_df) if not fc_df.empty else 0

    # Stock-perfil issues
    sin_perfil = perfil_df["SIN_PERFIL"].sum() if not perfil_df.empty else 0
    valor_riesgo = perfil_df.loc[perfil_df["SIN_PERFIL"], "VALOR_STOCK_CD"].sum() if not perfil_df.empty else 0

    c1, c2, c3, c4, c5 = st.columns(5)

    kpis = [
        (c1, "SKUs MIX", f"{total_mix:,}", COLORS["primary"]),
        (c2, "Sin Forecast", f"{sin_fc:,}", ISSUE_COLORS["SIN_FORECAST"]),
        (c3, "FC en 1 Canal", f"{fc_1_canal:,}", ISSUE_COLORS["FORECAST_1_CANAL"]),
        (c4, "CD sin Perfil", f"{int(sin_perfil):,}", ISSUE_COLORS["STOCK_CD_SIN_PERFIL"]),
        (c5, "$ en Riesgo", f"${human_format(valor_riesgo)}", ISSUE_COLORS["STOCK_CD_SIN_PERFIL"]),
    ]

    for col, label, value, color in kpis:
        with col:
            st.html(f"""
            <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                         border-top:4px solid {color};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                <div style='font-size:1.8rem;font-weight:bold;color:{color}'>{value}</div>
                <div style='font-size:0.8rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>{label}</div>
            </div>""")

    st.html("<br>")


def _render_forecast_section(fc_df):
    """Render forecast coverage analysis."""
    if fc_df.empty:
        st.warning(
            "No hay datos de forecast disponibles. "
            "Genera primero una proyeccion en el modulo 'Proyeccion Stock' y luego vuelve aqui."
        )
        return

    st.markdown("### 1. Cobertura de Forecast (SKUs MIX)")

    issues_only = fc_df[fc_df["ISSUE"] != "OK"]

    # Issue distribution chart
    issue_counts = fc_df["ISSUE"].value_counts().reset_index()
    issue_counts.columns = ["Issue", "Count"]

    all_issues = ["SIN_FORECAST", "FORECAST_1_CANAL", "OK"]
    colors_list = [ISSUE_COLORS.get(i, "#999") for i in all_issues if i in issue_counts["Issue"].values]
    domain_list = [i for i in all_issues if i in issue_counts["Issue"].values]

    col_chart, col_by_dim = st.columns(2)

    with col_chart:
        fig = go.Figure(
            data=[go.Pie(
                labels=issue_counts["Issue"],
                values=issue_counts["Count"],
                hole=0.5,
                marker=dict(colors=colors_list),
                textinfo="label+percent",
                hovertemplate="%{label}: %{value}<extra></extra>",
            )],
            layout=dorel_layout(title="Distribucion de Issues de Forecast", height=350),
        )
        st.plotly_chart(fig, use_container_width=True)

    with col_by_dim:
        # Issues by Area
        if "AREA" in fc_df.columns and not issues_only.empty:
            by_area = issues_only.groupby(["AREA", "ISSUE"], as_index=False).agg(
                COUNT=("SKU_PRODUCTO", "count")
            )
            fig2 = go.Figure(layout=dorel_layout(
                title="Issues de Forecast por Area",
                height=350,
                barmode="stack",
                xaxis_title="Area",
                yaxis_title="# SKUs con Issue",
            ))
            for issue in ["SIN_FORECAST", "FORECAST_1_CANAL"]:
                subset = by_area[by_area["ISSUE"] == issue]
                if not subset.empty:
                    fig2.add_trace(go.Bar(
                        x=subset["AREA"],
                        y=subset["COUNT"],
                        name=issue,
                        marker_color=ISSUE_COLORS[issue],
                        hovertemplate="Area: %{x}<br>Count: %{y}<extra>" + issue + "</extra>",
                    ))
            st.plotly_chart(fig2, use_container_width=True)

    # Detail: SKUs sin forecast
    sin_fc = fc_df[fc_df["ISSUE"] == "SIN_FORECAST"]
    if not sin_fc.empty:
        with st.expander(f"SKUs MIX sin Forecast ({len(sin_fc):,} SKUs)", expanded=False):
            detail_cols = ["SKU_PRODUCTO"]
            for c in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA"]:
                if c in sin_fc.columns:
                    detail_cols.append(c)
            detail_cols += ["FC_TIENDA", "FC_ETAIL", "FC_MAYOR", "FC_TOTAL"]
            detail_cols = [c for c in detail_cols if c in sin_fc.columns]
            st.dataframe(sin_fc[detail_cols].reset_index(drop=True), use_container_width=True, height=300)
            download_buttons(sin_fc[detail_cols], "skus_mix_sin_forecast")

    # Detail: SKUs con FC en solo 1 canal
    fc_1c = fc_df[fc_df["ISSUE"] == "FORECAST_1_CANAL"]
    if not fc_1c.empty:
        with st.expander(f"SKUs MIX con Forecast en solo 1 canal ({len(fc_1c):,} SKUs)", expanded=False):
            detail_cols = ["SKU_PRODUCTO"]
            for c in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA"]:
                if c in fc_1c.columns:
                    detail_cols.append(c)
            detail_cols += ["FC_TIENDA", "FC_ETAIL", "FC_MAYOR", "FC_TOTAL", "N_CANALES_CON_FC"]
            detail_cols = [c for c in detail_cols if c in fc_1c.columns]
            st.dataframe(fc_1c[detail_cols].reset_index(drop=True), use_container_width=True, height=300)
            download_buttons(fc_1c[detail_cols], "skus_mix_fc_1_canal")

    # Summary table: all issues
    with st.expander("Tabla completa de cobertura de forecast"):
        all_cols = ["SKU_PRODUCTO", "ISSUE"]
        for c in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA", "MIX_OFICIAL"]:
            if c in fc_df.columns:
                all_cols.append(c)
        all_cols += ["FC_TIENDA", "FC_ETAIL", "FC_MAYOR", "FC_TOTAL", "N_CANALES_CON_FC"]
        all_cols = [c for c in all_cols if c in fc_df.columns]
        st.dataframe(fc_df[all_cols].sort_values("ISSUE").reset_index(drop=True),
                      use_container_width=True, height=400)
        download_buttons(fc_df[all_cols], "forecast_coverage_completa")


def _render_perfil_section(perfil_df):
    """Render stock in CD without store profile analysis."""
    if perfil_df.empty:
        st.info("No se encontraron SKUs con stock en bodegas CD principales.")
        return

    st.markdown("### 2. Stock en CD Principal sin Perfil en Tiendas")
    st.caption(
        "SKUs que tienen stock en bodegas principales (Bluexpress Principal, Logistics Principal, etc.) "
        "pero no tienen ninguna tienda configurada con perfil de exhibicion (min_exhibicion > 0). "
        "Esto significa que hay inventario almacenado que no se va a distribuir automaticamente."
    )

    sin_perfil = perfil_df[perfil_df["SIN_PERFIL"]].copy()
    con_perfil = perfil_df[~perfil_df["SIN_PERFIL"]].copy()

    # KPIs for this section
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Total SKUs en CD Ppal", f"{len(perfil_df):,}")
    with c2:
        st.metric("Sin Perfil Tienda", f"{len(sin_perfil):,}",
                   delta=f"-{len(sin_perfil)/max(len(perfil_df),1)*100:.0f}%",
                   delta_color="inverse")
    with c3:
        valor = sin_perfil["VALOR_STOCK_CD"].sum() if "VALOR_STOCK_CD" in sin_perfil.columns else 0
        st.metric("Valor Stock Atrapado", f"${human_format(valor)}")
    with c4:
        und = sin_perfil["STOCK_CD_PRINCIPAL"].sum() if not sin_perfil.empty else 0
        st.metric("Unidades Atrapadas", f"{human_format(und)}")

    if sin_perfil.empty:
        st.success("Todos los SKUs con stock en CD principal tienen perfil en al menos 1 tienda.")
        return

    col_chart, col_chart2 = st.columns(2)

    with col_chart:
        # By Area
        if "AREA" in sin_perfil.columns:
            by_area = sin_perfil.groupby("AREA", as_index=False).agg(
                COUNT=("SKU_PRODUCTO", "count"),
                VALOR=("VALOR_STOCK_CD", "sum"),
            )
            fig3 = go.Figure(
                data=[go.Bar(
                    x=by_area["AREA"],
                    y=by_area["COUNT"],
                    marker_color=ISSUE_COLORS["STOCK_CD_SIN_PERFIL"],
                    hovertemplate=(
                        "Area: %{x}<br>SKUs: %{y}<br>"
                        "Valor Stock: $%{customdata:,.0f}<extra></extra>"
                    ),
                    customdata=by_area["VALOR"],
                )],
                layout=dorel_layout(
                    title="SKUs sin Perfil por Area",
                    height=350,
                    xaxis_title="Area",
                    yaxis_title="# SKUs sin Perfil",
                ),
            )
            st.plotly_chart(fig3, use_container_width=True)

    with col_chart2:
        # Top 20 by value
        top = sin_perfil.nlargest(20, "VALOR_STOCK_CD")
        nombre_col = "SKU_NOM_PRODUCTO" if "SKU_NOM_PRODUCTO" in top.columns else "SKU_PRODUCTO"
        top["LABEL"] = top["SKU_PRODUCTO"].astype(str) + " - " + top[nombre_col].astype(str)
        top["LABEL"] = top["LABEL"].str[:45]

        fig4 = go.Figure(
            data=[go.Bar(
                x=top["VALOR_STOCK_CD"],
                y=top["LABEL"],
                orientation="h",
                marker_color=ISSUE_COLORS["STOCK_CD_SIN_PERFIL"],
                customdata=np.stack([
                    top["SKU_PRODUCTO"].values,
                    top["STOCK_CD_PRINCIPAL"].values,
                    top["VALOR_STOCK_CD"].values,
                    top["BODEGAS_CD_LISTA"].values if "BODEGAS_CD_LISTA" in top.columns else [""] * len(top),
                ], axis=-1),
                hovertemplate=(
                    "SKU: %{customdata[0]}<br>"
                    "Unidades: %{customdata[1]:,.0f}<br>"
                    "Valor: $%{customdata[2]:,.0f}<br>"
                    "Bodegas: %{customdata[3]}<extra></extra>"
                ),
            )],
            layout=dorel_layout(
                title="Top 20 SKUs por Valor de Stock Atrapado",
                height=450,
                xaxis_title="Valor Stock en CD ($)",
                yaxis=dict(autorange="reversed", gridcolor="#ECECEC", gridwidth=1, zeroline=False),
            ),
        )
        st.plotly_chart(fig4, use_container_width=True)

    # Detail table
    with st.expander(f"Detalle: {len(sin_perfil):,} SKUs con Stock en CD sin Perfil", expanded=True):
        detail_cols = ["SKU_PRODUCTO"]
        for c in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA", "MIX_OFICIAL"]:
            if c in sin_perfil.columns:
                detail_cols.append(c)
        detail_cols += ["STOCK_CD_PRINCIPAL", "VALOR_STOCK_CD", "BODEGAS_CD_LISTA",
                        "TIENDAS_CON_PERFIL", "PERFIL_TOTAL"]
        detail_cols = [c for c in detail_cols if c in sin_perfil.columns]

        st.dataframe(
            sin_perfil[detail_cols].sort_values("VALOR_STOCK_CD", ascending=False).reset_index(drop=True),
            use_container_width=True, height=400,
        )
        download_buttons(sin_perfil[detail_cols], "stock_cd_sin_perfil_tienda")


def _render_cross_check(fc_df, perfil_df):
    """Cross-check: SKUs with both forecast issues AND perfil issues."""
    if fc_df.empty or perfil_df.empty:
        return

    # SKUs with forecast issues
    fc_issues = set(fc_df.loc[fc_df["ISSUE"] != "OK", "SKU_PRODUCTO"])

    # SKUs with perfil issues
    perfil_issues = set(perfil_df.loc[perfil_df["SIN_PERFIL"], "SKU_PRODUCTO"])

    overlap = fc_issues & perfil_issues

    if not overlap:
        return

    st.markdown("### 3. SKUs con Multiples Problemas")
    st.warning(
        f"**{len(overlap):,} SKUs** tienen problemas tanto de forecast como de perfil. "
        "Estos son los casos mas criticos que requieren atencion inmediata."
    )

    # Get details
    overlap_df = fc_df[fc_df["SKU_PRODUCTO"].isin(overlap)].merge(
        perfil_df[perfil_df["SKU_PRODUCTO"].isin(overlap)][
            ["SKU_PRODUCTO", "STOCK_CD_PRINCIPAL", "VALOR_STOCK_CD", "TIENDAS_CON_PERFIL"]
        ],
        on="SKU_PRODUCTO",
        how="inner",
    )

    detail_cols = ["SKU_PRODUCTO", "ISSUE"]
    for c in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA"]:
        if c in overlap_df.columns:
            detail_cols.append(c)
    detail_cols += ["FC_TOTAL", "STOCK_CD_PRINCIPAL", "VALOR_STOCK_CD", "TIENDAS_CON_PERFIL"]
    detail_cols = [c for c in detail_cols if c in overlap_df.columns]

    st.dataframe(
        overlap_df[detail_cols].sort_values("VALOR_STOCK_CD", ascending=False).reset_index(drop=True),
        use_container_width=True, height=300,
    )
    download_buttons(overlap_df[detail_cols], "skus_multiples_problemas")


# ============================================================================
# MAIN RENDER
# ============================================================================

def render_higiene(conn):
    """Main entry point for supply hygiene dashboard."""
    st.html("<h2 class='sub-header'>Higiene de Abastecimiento</h2>")
    st.caption(
        "Diagnostico de inconsistencias en el proceso de abastecimiento: "
        "cobertura de forecast, configuracion de perfiles, y stock atrapado."
    )

    # Load data (cached)
    if st.button("Actualizar Datos", key="btn_refresh_higiene"):
        st.session_state.pop("higiene_stock", None)
        st.session_state.pop("higiene_fc", None)
        st.session_state.pop("higiene_perfil", None)

    if "higiene_stock" not in st.session_state:
        stock, maestra = _load_data(conn)
        st.session_state["higiene_stock"] = stock
        st.session_state["higiene_maestra"] = maestra

    stock = st.session_state["higiene_stock"]
    maestra = st.session_state["higiene_maestra"]

    # Get forecast data from projection (if available)
    proy_df = _get_forecast_data()
    if proy_df is not None:
        proy_df = apply_pm_filter(proy_df)

    # Run checks
    if "higiene_fc" not in st.session_state or proy_df is not None:
        fc_df = _check_forecast_coverage(maestra, proy_df)
        st.session_state["higiene_fc"] = fc_df

    if "higiene_perfil" not in st.session_state:
        perfil_df = _check_stock_sin_perfil(stock, maestra)
        st.session_state["higiene_perfil"] = perfil_df

    fc_df = st.session_state["higiene_fc"]
    perfil_df = st.session_state["higiene_perfil"]

    # ── Filters ──────────────────────────────────────────────────────
    st.markdown("### Filtros")
    fc1, fc2, fc3, fc4 = st.columns(4)

    # Combine MIX_OFICIAL options from both DataFrames
    all_mix = set()
    for df_check in [fc_df, perfil_df]:
        if not df_check.empty and "MIX_OFICIAL" in df_check.columns:
            all_mix.update(df_check["MIX_OFICIAL"].dropna().unique())
    mix_opts = sorted(all_mix)

    # Combine areas from both DataFrames
    all_areas = set()
    for df_check in [fc_df, perfil_df]:
        if not df_check.empty and "AREA" in df_check.columns:
            all_areas.update(df_check["AREA"].dropna().unique())
    areas = sorted(all_areas)

    all_lineas = set()
    for df_check in [fc_df, perfil_df]:
        if not df_check.empty and "LINEA" in df_check.columns:
            all_lineas.update(df_check["LINEA"].dropna().unique())

    all_marcas = set()
    for df_check in [fc_df, perfil_df]:
        if not df_check.empty and "MARCA" in df_check.columns:
            all_marcas.update(df_check["MARCA"].dropna().unique())

    with fc1:
        sel_mix = st.multiselect("Mix Oficial", mix_opts, key="hig_mix")
    with fc2:
        sel_area = st.multiselect("Area", areas, key="hig_area")
    with fc3:
        if sel_area:
            _lineas = set()
            for df_check in [fc_df, perfil_df]:
                if not df_check.empty and "LINEA" in df_check.columns:
                    _lineas.update(df_check[df_check["AREA"].isin(sel_area)]["LINEA"].dropna().unique())
            lineas_filt = sorted(_lineas)
        else:
            lineas_filt = sorted(all_lineas)
        sel_linea = st.multiselect("Linea", lineas_filt, key="hig_linea")
    with fc4:
        if sel_area or sel_linea:
            _marcas = set()
            for df_check in [fc_df, perfil_df]:
                if not df_check.empty and "MARCA" in df_check.columns:
                    tmp = df_check.copy()
                    if sel_area:
                        tmp = tmp[tmp["AREA"].isin(sel_area)]
                    if sel_linea:
                        tmp = tmp[tmp["LINEA"].isin(sel_linea)]
                    _marcas.update(tmp["MARCA"].dropna().unique())
            marcas_filt = sorted(_marcas)
        else:
            marcas_filt = sorted(all_marcas)
        sel_marca = st.multiselect("Marca", marcas_filt, key="hig_marca")

    # Apply filters to both DataFrames
    def apply_filters(df):
        if df.empty:
            return df
        mask = pd.Series(True, index=df.index)
        if sel_mix and "MIX_OFICIAL" in df.columns:
            mask &= df["MIX_OFICIAL"].isin(sel_mix)
        if sel_area and "AREA" in df.columns:
            mask &= df["AREA"].isin(sel_area)
        if sel_linea and "LINEA" in df.columns:
            mask &= df["LINEA"].isin(sel_linea)
        if sel_marca and "MARCA" in df.columns:
            mask &= df["MARCA"].isin(sel_marca)
        return df[mask]

    fc_filt = apply_filters(fc_df)
    perfil_filt = apply_filters(perfil_df)

    st.markdown("---")

    # ── Summary KPIs ─────────────────────────────────────────────────
    _render_summary_kpis(fc_filt, perfil_filt)

    st.markdown("---")

    # ── Section 1: Forecast Coverage ─────────────────────────────────
    _render_forecast_section(fc_filt)

    st.markdown("---")

    # ── Section 2: Stock sin Perfil ──────────────────────────────────
    _render_perfil_section(perfil_filt)

    st.markdown("---")

    # ── Section 3: Cross-check ───────────────────────────────────────
    _render_cross_check(fc_filt, perfil_filt)
