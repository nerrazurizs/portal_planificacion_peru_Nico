"""Sell-Through Rate por Canal - Velocidad de rotacion del inventario."""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format, fmt_clp
from utils.export import download_buttons
from config import COLORS, dorel_layout, apply_pm_filter
from utils.ui_animations import lottie_spinner

# ── Paleta cluster (hasta 12 colores distintos) ─────────────────────────────
_CLUSTER_PALETTE = [
    "#065E8B", "#632CFF", "#2DAAFF", "#23CED3", "#C94BFF",
    "#10b981", "#f59e0b", "#ef4444", "#8B5CF6", "#06B6D4",
    "#F97316", "#84CC16",
]

CANAL_MAP = {
    "MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL",
    "TIENDA": "TIENDA", "MAYORISTA": "MAYORISTA",
}

STR_COLORS = {
    "MUY BAJO": "#E53935",
    "BAJO": "#FB8C00",
    "NORMAL": "#43A047",
    "ALTO": "#1B5E20",
    "MUY ALTO": "#0D47A1",
}
STR_ORDER = ["MUY BAJO", "BAJO", "NORMAL", "ALTO", "MUY ALTO"]


def _load_data(conn):
    with lottie_spinner("snowflake"):
        ventas = cq.ventas_aa(conn)
        stock = cq.stock_proyeccion(conn)
        maestra = cq.maestra(conn)
        # Para análisis por tienda
        ventas_hist = cq.ventas_historicas(conn)
        stock_higiene = cq.stock_higiene(conn)
        tienda_dim = cq.tienda_dim(conn)
    return ventas, stock, maestra, ventas_hist, stock_higiene, tienda_dim


def _calculate_str(ventas, stock, maestra):
    """Calculate Sell-Through Rate = Ventas / (Stock Inicial + Ingresos).
    Simplified: STR_mensual = Venta mensual prom / Stock actual.
    """
    if ventas.empty:
        return pd.DataFrame()

    # Map channels
    if "COD_CANAL" in ventas.columns:
        ventas["CANAL_STD"] = ventas["COD_CANAL"].str.strip().str.upper().map(CANAL_MAP)
        ventas = ventas[ventas["CANAL_STD"].notna()]

    # Annual sales by SKU x Channel
    vta_anual = ventas.groupby(["SKU_PRODUCTO", "CANAL_STD"], as_index=False).agg(
        UNIDADES_AA=("CANTIDAD_AA", "sum"),
        VN_AA=("NETO_AA", "sum"),
    )
    vta_anual["VENTA_MENSUAL_PROM"] = vta_anual["UNIDADES_AA"] / 12

    # Also total across channels
    vta_total = ventas.groupby("SKU_PRODUCTO", as_index=False).agg(
        UNIDADES_AA_TOTAL=("CANTIDAD_AA", "sum"),
        VN_AA_TOTAL=("NETO_AA", "sum"),
    )
    vta_total["VENTA_MENSUAL_PROM_TOTAL"] = vta_total["UNIDADES_AA_TOTAL"] / 12

    # Current stock by SKU x Channel
    if "CANAL_STD" in stock.columns:
        stk_by_canal = stock.groupby(["SKU_PRODUCTO", "CANAL_STD"], as_index=False).agg(
            STOCK_ACTUAL=("STOCK_UNIDADES", "sum"),
        )
    else:
        stk_by_canal = pd.DataFrame(columns=["SKU_PRODUCTO", "CANAL_STD", "STOCK_ACTUAL"])

    stk_total = stock.groupby("SKU_PRODUCTO", as_index=False).agg(
        STOCK_TOTAL=("STOCK_UNIDADES", "sum"),
    )

    # Merge: by channel
    df_canal = vta_anual.merge(stk_by_canal, on=["SKU_PRODUCTO", "CANAL_STD"], how="outer")
    df_canal["STOCK_ACTUAL"] = df_canal["STOCK_ACTUAL"].fillna(0)
    df_canal["VENTA_MENSUAL_PROM"] = df_canal["VENTA_MENSUAL_PROM"].fillna(0)

    # STR = monthly avg sales / current stock (as %)
    df_canal["STR_PCT"] = np.where(
        df_canal["STOCK_ACTUAL"] > 0,
        df_canal["VENTA_MENSUAL_PROM"] / df_canal["STOCK_ACTUAL"] * 100,
        np.where(df_canal["VENTA_MENSUAL_PROM"] > 0, 999, 0),
    )

    # Total level
    df_total = vta_total.merge(stk_total, on="SKU_PRODUCTO", how="outer")
    df_total["STOCK_TOTAL"] = df_total["STOCK_TOTAL"].fillna(0)
    df_total["VENTA_MENSUAL_PROM_TOTAL"] = df_total["VENTA_MENSUAL_PROM_TOTAL"].fillna(0)
    df_total["STR_PCT_TOTAL"] = np.where(
        df_total["STOCK_TOTAL"] > 0,
        df_total["VENTA_MENSUAL_PROM_TOTAL"] / df_total["STOCK_TOTAL"] * 100,
        np.where(df_total["VENTA_MENSUAL_PROM_TOTAL"] > 0, 999, 0),
    )

    # Classify STR
    def classify_str(s):
        return np.select(
            [s <= 5, s <= 20, s <= 60, s <= 100],
            ["MUY BAJO", "BAJO", "NORMAL", "ALTO"],
            default="MUY ALTO",
        )

    df_total["STR_SEGMENT"] = classify_str(df_total["STR_PCT_TOTAL"])

    # Months of inventory (inverse of STR)
    df_total["MESES_INVENTARIO"] = np.where(
        df_total["VENTA_MENSUAL_PROM_TOTAL"] > 0,
        df_total["STOCK_TOTAL"] / df_total["VENTA_MENSUAL_PROM_TOTAL"],
        999,
    )

    # Pivot channel STR for wide format
    canal_pivot = df_canal.pivot_table(
        index="SKU_PRODUCTO", columns="CANAL_STD",
        values=["STR_PCT", "STOCK_ACTUAL", "VENTA_MENSUAL_PROM"], aggfunc="sum"
    )
    canal_pivot.columns = [f"{v}_{c}" for v, c in canal_pivot.columns]
    canal_pivot = canal_pivot.reset_index()

    df = df_total.merge(canal_pivot, on="SKU_PRODUCTO", how="left")

    # Maestra
    maestra_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                    "SKU_NOM_PRODUCTO", "MIX_OFICIAL"]
    available = [c for c in maestra_cols if c in maestra.columns]
    maestra_dedup = maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
    df = df.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")

    return df


def _calculate_tienda_metrics(ventas_hist, stock_higiene, tienda_dim):
    """Calcula metricas de productividad por tienda.

    Returns DataFrame con:
      ID_SUCURSAL, DESCRIPCION_SUCURSAL, CLUSTER, SUPERVISOR, TIPO, MTS2,
      VN_TOTAL, UND_TOTAL, N_MESES, VN_MENSUAL_PROM,
      VN_M2 (venta neta anual / m2), VN_MES_M2 (venta mensual prom / m2),
      STOCK_UND, UND_M2_STOCK (unidades stock / m2)
    """
    if ventas_hist.empty or tienda_dim.empty:
        return pd.DataFrame()

    _req = ["ID_SUCURSAL", "NETO_TOTAL", "UNIDADES_VENDIDAS"]
    if not all(c in ventas_hist.columns for c in _req):
        return pd.DataFrame()

    # VN anual por tienda
    _vt = ventas_hist.copy()
    _vt["NETO_TOTAL"] = pd.to_numeric(_vt["NETO_TOTAL"], errors="coerce").fillna(0)
    _vt["UNIDADES_VENDIDAS"] = pd.to_numeric(_vt["UNIDADES_VENDIDAS"], errors="coerce").fillna(0)

    # Solo tiendas (excluir CD) si la columna existe
    if "CANAL_DE_DISTRIBUCION" in _vt.columns:
        _vt = _vt[_vt["CANAL_DE_DISTRIBUCION"] == "TIENDA"]

    if "FECHA" in _vt.columns:
        _vt["FECHA"] = pd.to_datetime(_vt["FECHA"], errors="coerce")
        _n_meses = (
            _vt.groupby("ID_SUCURSAL")["FECHA"]
            .apply(lambda x: x.dt.to_period("M").nunique())
            .reset_index()
        )
        _n_meses.columns = ["ID_SUCURSAL", "N_MESES"]
    else:
        _n_meses = pd.DataFrame(columns=["ID_SUCURSAL", "N_MESES"])

    vn_tienda = _vt.groupby("ID_SUCURSAL", as_index=False).agg(
        VN_TOTAL=("NETO_TOTAL", "sum"),
        UND_TOTAL=("UNIDADES_VENDIDAS", "sum"),
    )
    if not _n_meses.empty:
        vn_tienda = vn_tienda.merge(_n_meses, on="ID_SUCURSAL", how="left")
        vn_tienda["N_MESES"] = vn_tienda["N_MESES"].fillna(1).clip(lower=1)
    else:
        vn_tienda["N_MESES"] = 12

    vn_tienda["VN_MENSUAL_PROM"] = np.where(
        vn_tienda["N_MESES"] > 0,
        vn_tienda["VN_TOTAL"] / vn_tienda["N_MESES"],
        0,
    )

    # Stock actual por tienda (unidades)
    stk_tienda = pd.DataFrame(columns=["ID_SUCURSAL", "STOCK_UND"])
    if not stock_higiene.empty and "ID_SUCURSAL" in stock_higiene.columns:
        _sh = stock_higiene.copy()
        if "CANAL_DE_DISTRIBUCION" in _sh.columns:
            _sh = _sh[_sh["CANAL_DE_DISTRIBUCION"] == "TIENDA"]
        _sh["STOCK_UNIDADES"] = pd.to_numeric(_sh.get("STOCK_UNIDADES", 0), errors="coerce").fillna(0)
        stk_tienda = _sh.groupby("ID_SUCURSAL", as_index=False).agg(
            STOCK_UND=("STOCK_UNIDADES", "sum")
        )

    # Columnas a traer de tienda_dim
    _td_cols = [
        c for c in
        ["ID_SUCURSAL", "DESCRIPCION_SUCURSAL", "MTS2", "CLUSTER", "SUPERVISOR", "TIPO", "GRUPO"]
        if c in tienda_dim.columns
    ]
    df = vn_tienda.merge(tienda_dim[_td_cols], on="ID_SUCURSAL", how="left")
    df = df.merge(stk_tienda, on="ID_SUCURSAL", how="left")
    df["STOCK_UND"] = df["STOCK_UND"].fillna(0)

    # Métricas por m²
    if "MTS2" in df.columns:
        df["MTS2"] = pd.to_numeric(df["MTS2"], errors="coerce").fillna(0)
        df["VN_M2"] = np.where(df["MTS2"] > 0, df["VN_TOTAL"] / df["MTS2"], 0)
        df["VN_MES_M2"] = np.where(df["MTS2"] > 0, df["VN_MENSUAL_PROM"] / df["MTS2"], 0)
        df["UND_M2_STOCK"] = np.where(df["MTS2"] > 0, df["STOCK_UND"] / df["MTS2"], 0)
    else:
        df["VN_M2"] = 0
        df["VN_MES_M2"] = 0
        df["UND_M2_STOCK"] = 0

    return df


def _render_kpis(df):
    total = len(df)
    str_med = df["STR_PCT_TOTAL"].median()
    muy_bajo = (df["STR_SEGMENT"] == "MUY BAJO").sum()
    muy_alto = (df["STR_SEGMENT"] == "MUY ALTO").sum()
    stock_parado = df.loc[df["STR_SEGMENT"] == "MUY BAJO", "STOCK_TOTAL"].sum()

    cols = st.columns(5)
    data = [
        ("Total SKUs", f"{total:,}", COLORS["primary"]),
        ("STR Mediana", f"{str_med:.0f}%", COLORS["tertiary_blue"]),
        ("STR Muy Bajo", f"{muy_bajo:,}", STR_COLORS["MUY BAJO"]),
        ("STR Muy Alto", f"{muy_alto:,}", STR_COLORS["MUY ALTO"]),
        ("Stock Parado", human_format(stock_parado) + " und", STR_COLORS["MUY BAJO"]),
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


def render_sell_through(conn):
    st.html("<h2 class='sub-header'>Sell-Through Rate por Canal</h2>")
    st.caption("STR = Venta mensual promedio / Stock actual. Mide la velocidad de rotacion del inventario.")

    if st.button("Actualizar Datos", key="btn_refresh_str"):
        st.session_state.pop("str_data", None)

    if "str_data" not in st.session_state:
        ventas, stock, maestra, ventas_hist, stock_higiene, tienda_dim = _load_data(conn)
        df = _calculate_str(ventas, stock, maestra)
        st.session_state["str_data"] = df
        # Métricas por tienda (m² + cluster) — guardadas separado
        df_tienda = _calculate_tienda_metrics(ventas_hist, stock_higiene, tienda_dim)
        st.session_state["str_tienda_data"] = df_tienda

    df = st.session_state["str_data"].copy()
    df = apply_pm_filter(df)
    if df.empty:
        st.warning("No se encontraron datos.")
        return

    # Filters
    st.markdown("### Filtros")
    fc1, fc2, fc3 = st.columns(3)
    areas = sorted(df["AREA"].dropna().unique()) if "AREA" in df.columns else []
    with fc1:
        sel_area = st.multiselect("Area", areas, key="str_area")
    with fc2:
        _m = df[df["AREA"].isin(sel_area)] if sel_area else df
        sel_linea = st.multiselect("Linea", sorted(_m["LINEA"].dropna().unique()) if "LINEA" in _m.columns else [], key="str_linea")
    with fc3:
        _m2 = _m[_m["LINEA"].isin(sel_linea)] if sel_linea else _m
        sel_marca = st.multiselect("Marca", sorted(_m2["MARCA"].dropna().unique()) if "MARCA" in _m2.columns else [], key="str_marca")

    mask = pd.Series(True, index=df.index)
    if sel_area:
        mask &= df["AREA"].isin(sel_area)
    if sel_linea:
        mask &= df["LINEA"].isin(sel_linea)
    if sel_marca:
        mask &= df["MARCA"].isin(sel_marca)
    df_filt = df[mask]
    if df_filt.empty:
        st.warning("Sin datos con filtros seleccionados.")
        return

    st.markdown("---")
    _render_kpis(df_filt)

    # ── Chart 1: Distribution bar ──
    col1, col2 = st.columns(2)
    with col1:
        counts = df_filt["STR_SEGMENT"].value_counts().reset_index()
        counts.columns = ["Segmento", "Count"]
        cat = [s for s in STR_ORDER if s in counts["Segmento"].values]
        clr = [STR_COLORS[s] for s in cat]

        fig1 = go.Figure(layout=dorel_layout(
            title=dict(text="Distribucion de STR", font_size=14, x=0.5),
            height=380,
            xaxis=dict(title="", categoryorder="array", categoryarray=cat),
            yaxis=dict(title="# SKUs"),
        ))
        for seg, color in zip(cat, clr):
            row = counts[counts["Segmento"] == seg]
            if not row.empty:
                fig1.add_trace(go.Bar(
                    x=[seg], y=[row["Count"].values[0]],
                    marker_color=color, name=seg, showlegend=False,
                    hovertemplate="Segmento: %{x}<br>SKUs: %{y:,}<extra></extra>",
                ))
        st.plotly_chart(fig1, use_container_width=True)

    # ── Chart 2: STR by dimension ──
    with col2:
        dim = st.radio("Dimension", ["AREA", "LINEA", "MARCA"], horizontal=True, key="str_dim")
        if dim in df_filt.columns:
            by_dim = df_filt.groupby(dim, as_index=False).agg(
                STR_MEDIANA=("STR_PCT_TOTAL", "median"),
                COUNT=("SKU_PRODUCTO", "count"),
            ).nlargest(15, "COUNT")

            fig2 = go.Figure(layout=dorel_layout(
                title=dict(text=f"STR Mediana por {dim.title()}", font_size=14, x=0.5),
                height=380,
                xaxis=dict(title="", categoryorder="total descending"),
                yaxis=dict(title="STR Mediana %"),
            ))
            fig2.add_trace(go.Bar(
                x=by_dim[dim], y=by_dim["STR_MEDIANA"],
                marker_color=COLORS["primary"],
                hovertemplate=(
                    "%{x}<br>STR Mediana: %{y:.0f}%<br>"
                    "SKUs: %{customdata:,}<extra></extra>"
                ),
                customdata=by_dim["COUNT"],
            ))
            st.plotly_chart(fig2, use_container_width=True)

    st.markdown("---")

    # ── Chart 3: Channel comparison ──
    st.markdown("### Comparacion por Canal")
    canal_cols = [c for c in df_filt.columns if c.startswith("STR_PCT_")]
    if canal_cols:
        canal_data = []
        for c in canal_cols:
            canal_name = c.replace("STR_PCT_", "")
            vals = df_filt[c].dropna()
            vals = vals[vals < 999]
            if not vals.empty:
                canal_data.append({"Canal": canal_name, "STR_Mediana": vals.median(), "SKUs": len(vals)})
        if canal_data:
            cdf = pd.DataFrame(canal_data)
            fig3 = go.Figure(layout=dorel_layout(
                title=dict(text="STR Mediana por Canal", font_size=14, x=0.5),
                height=350,
                xaxis=dict(title=""),
                yaxis=dict(title="STR %"),
            ))
            fig3.add_trace(go.Bar(
                x=cdf["Canal"], y=cdf["STR_Mediana"],
                marker_color=COLORS["tertiary_teal"],
                hovertemplate=(
                    "Canal: %{x}<br>STR Mediana: %{y:.0f}%<br>"
                    "SKUs: %{customdata:,}<extra></extra>"
                ),
                customdata=cdf["SKUs"],
            ))
            st.plotly_chart(fig3, use_container_width=True)

    st.markdown("---")
    st.markdown("### Detalle por SKU")
    display_cols = ["SKU_PRODUCTO", "STR_SEGMENT", "STR_PCT_TOTAL", "MESES_INVENTARIO",
                    "STOCK_TOTAL", "VENTA_MENSUAL_PROM_TOTAL", "VN_AA_TOTAL"]
    for c in ["AREA", "LINEA", "MARCA", "SKU_NOM_PRODUCTO"]:
        if c in df_filt.columns:
            display_cols.insert(1, c)
    display_cols = [c for c in display_cols if c in df_filt.columns]
    st.dataframe(df_filt[display_cols].sort_values("STR_PCT_TOTAL").reset_index(drop=True),
                 use_container_width=True, height=500)
    download_buttons(df_filt[display_cols], "sell_through_rate")

    # ══════════════════════════════════════════════════════════════════════════
    # SECCIÓN: Productividad por m² y Benchmarking por Cluster
    # ══════════════════════════════════════════════════════════════════════════
    df_tienda = st.session_state.get("str_tienda_data", pd.DataFrame())
    if df_tienda.empty or "MTS2" not in df_tienda.columns:
        st.markdown("---")
        st.info("Sin datos de m² disponibles. Verifica que la tabla dt_tienda tenga datos.")
        return

    # Filtrar tiendas con m² válido
    df_td = df_tienda[df_tienda["MTS2"] > 0].copy()
    if df_td.empty:
        return

    st.markdown("---")
    st.markdown("## Productividad por m² y Benchmarking por Cluster")
    st.caption(
        "VN/m² = Venta Neta anual acumulada dividida por los metros cuadrados de la tienda. "
        "Permite comparar el rendimiento real de sala entre tiendas de distinto tamaño."
    )

    # ── KPIs m² ──────────────────────────────────────────────────────────────
    _k1, _k2, _k3, _k4 = st.columns(4)
    _vn_m2_med = df_td["VN_M2"].median()
    _vn_m2_top = df_td["VN_M2"].quantile(0.9)
    _n_tiendas = len(df_td)
    _cluster_col = "CLUSTER" if "CLUSTER" in df_td.columns else None
    _n_clusters = df_td[_cluster_col].nunique() if _cluster_col else 0
    _kpi_m2 = [
        ("Tiendas con m²", f"{_n_tiendas}", COLORS["primary"]),
        ("VN/m² Mediana", f"${_vn_m2_med:,.0f}", COLORS["tertiary_blue"]),
        ("VN/m² P90", f"${_vn_m2_top:,.0f}", COLORS["secondary"]),
        ("Clusters", f"{_n_clusters}", COLORS["tertiary_teal"]),
    ]
    for _kc, (_lab, _val, _clr) in zip([_k1, _k2, _k3, _k4], _kpi_m2):
        with _kc:
            st.html(f"""
            <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                         border-top:4px solid {_clr};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                <div style='font-size:1.6rem;font-weight:bold;color:{_clr}'>{_val}</div>
                <div style='font-size:0.75rem;color:#94a3b8;text-transform:uppercase'>{_lab}</div>
            </div>""")
    st.html("<br>")

    # ── Chart: Ranking VN/m² por tienda ──────────────────────────────────────
    st.markdown("### Ranking de Productividad (VN / m²)")
    _tab1, _tab2 = st.tabs(["Top / Bottom Tiendas", "Benchmarking por Cluster"])

    with _tab1:
        _n_show = st.slider("Mostrar Top/Bottom N tiendas", 10, 30, 15, key="str_m2_n")
        _desc_col = "DESCRIPCION_SUCURSAL" if "DESCRIPCION_SUCURSAL" in df_td.columns else None
        _label = df_td[_desc_col].fillna(df_td["ID_SUCURSAL"].astype(str)) if _desc_col else df_td["ID_SUCURSAL"].astype(str)
        df_td["_LABEL"] = _label

        _top_n = df_td.nlargest(_n_show, "VN_M2")
        _bot_n = df_td.nsmallest(_n_show, "VN_M2")

        _col_top, _col_bot = st.columns(2)

        with _col_top:
            _ts = _top_n.sort_values("VN_M2", ascending=True)
            _hover_top = np.column_stack([
                _ts["MTS2"].values,
                _ts.get("CLUSTER", pd.Series(["N/D"] * len(_ts))).fillna("N/D").values,
                _ts.get("SUPERVISOR", pd.Series(["N/D"] * len(_ts))).fillna("N/D").values,
                _ts["VN_TOTAL"].values,
            ])
            fig_top = go.Figure(layout=dorel_layout(
                title=dict(text=f"Top {_n_show} mayor VN/m²", font_size=14, x=0.5),
                xaxis=dict(title="VN/m² ($)"),
                height=max(_n_show * 28, 350),
                margin=dict(l=200, r=20, t=50, b=40),
            ))
            fig_top.add_trace(go.Bar(
                y=_ts["_LABEL"].str[:35],
                x=_ts["VN_M2"],
                orientation="h",
                marker_color=COLORS["status_on_track"],
                customdata=_hover_top,
                hovertemplate=(
                    "<b>%{y}</b><br>"
                    "VN/m²: $%{x:,.0f}<br>"
                    "m²: %{customdata[0]:,.0f}<br>"
                    "Cluster: %{customdata[1]}<br>"
                    "Supervisor: %{customdata[2]}<br>"
                    "VN Total: $%{customdata[3]:,.0f}<extra></extra>"
                ),
            ))
            st.plotly_chart(fig_top, use_container_width=True)

        with _col_bot:
            _bs = _bot_n.sort_values("VN_M2", ascending=False)
            _hover_bot = np.column_stack([
                _bs["MTS2"].values,
                _bs.get("CLUSTER", pd.Series(["N/D"] * len(_bs))).fillna("N/D").values,
                _bs.get("SUPERVISOR", pd.Series(["N/D"] * len(_bs))).fillna("N/D").values,
                _bs["VN_TOTAL"].values,
            ])
            fig_bot = go.Figure(layout=dorel_layout(
                title=dict(text=f"Bottom {_n_show} menor VN/m²", font_size=14, x=0.5),
                xaxis=dict(title="VN/m² ($)"),
                height=max(_n_show * 28, 350),
                margin=dict(l=200, r=20, t=50, b=40),
            ))
            fig_bot.add_trace(go.Bar(
                y=_bs["_LABEL"].str[:35],
                x=_bs["VN_M2"],
                orientation="h",
                marker_color=COLORS["status_critical"],
                customdata=_hover_bot,
                hovertemplate=(
                    "<b>%{y}</b><br>"
                    "VN/m²: $%{x:,.0f}<br>"
                    "m²: %{customdata[0]:,.0f}<br>"
                    "Cluster: %{customdata[1]}<br>"
                    "Supervisor: %{customdata[2]}<br>"
                    "VN Total: $%{customdata[3]:,.0f}<extra></extra>"
                ),
            ))
            st.plotly_chart(fig_bot, use_container_width=True)

        # Scatter: VN total vs m² (tamaño de burbuja = VN/m²)
        st.markdown("#### Relacion VN Total vs m² (burbuja = productividad)")
        if _desc_col:
            _hover_scat = np.column_stack([
                df_td["_LABEL"].values,
                df_td["VN_M2"].values,
                df_td.get("CLUSTER", pd.Series(["N/D"] * len(df_td))).fillna("N/D").values,
                df_td.get("SUPERVISOR", pd.Series(["N/D"] * len(df_td))).fillna("N/D").values,
            ])
            _bubble = (df_td["VN_M2"].clip(lower=0) ** 0.5 * 2).clip(upper=40).fillna(5)
            fig_scat = go.Figure(layout=dorel_layout(
                title=dict(text="VN Total vs m² de Sala", font_size=14, x=0.5),
                xaxis=dict(title="m² de sala"),
                yaxis=dict(title="Venta Neta Acumulada ($)"),
                height=420,
            ))
            fig_scat.add_trace(go.Scatter(
                x=df_td["MTS2"],
                y=df_td["VN_TOTAL"],
                mode="markers",
                marker=dict(
                    size=_bubble,
                    color=df_td["VN_M2"],
                    colorscale="RdYlGn",
                    showscale=True,
                    colorbar=dict(title="VN/m²"),
                    line=dict(width=1, color="white"),
                    opacity=0.85,
                ),
                customdata=_hover_scat,
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>"
                    "m²: %{x:,.0f}<br>"
                    "VN Total: $%{y:,.0f}<br>"
                    "VN/m²: $%{customdata[1]:,.0f}<br>"
                    "Cluster: %{customdata[2]}<br>"
                    "Supervisor: %{customdata[3]}<extra></extra>"
                ),
                showlegend=False,
            ))
            st.plotly_chart(fig_scat, use_container_width=True)

        download_buttons(
            df_td.drop(columns=["_LABEL"], errors="ignore"),
            "productividad_tienda_m2",
        )

    with _tab2:
        # ── Benchmarking por cluster ──────────────────────────────────────────
        if _cluster_col is None or df_td[_cluster_col].nunique() < 2:
            st.info("No hay suficientes clusters para comparar (se necesitan ≥ 2).")
        else:
            st.markdown(
                "Compara el VN/m² entre tiendas del **mismo cluster**. "
                "Las burbujas fuera del rango del cluster son candidatas a investigar."
            )
            _clusters = sorted(df_td[_cluster_col].dropna().unique())
            _palette = {c: _CLUSTER_PALETTE[i % len(_CLUSTER_PALETTE)] for i, c in enumerate(_clusters)}

            # ── Box plot VN/m² por cluster ───────────────────────────────────
            fig_box = go.Figure(layout=dorel_layout(
                title=dict(text="Distribucion VN/m² por Cluster", font_size=14, x=0.5),
                yaxis=dict(title="VN/m² ($)"),
                xaxis=dict(title="Cluster"),
                height=450,
            ))
            for cl in _clusters:
                _sub = df_td[df_td[_cluster_col] == cl]
                fig_box.add_trace(go.Box(
                    y=_sub["VN_M2"],
                    name=str(cl),
                    marker_color=_palette[cl],
                    boxpoints="all",
                    jitter=0.3,
                    pointpos=0,
                    customdata=_sub["_LABEL"].values if "_LABEL" in _sub.columns else _sub["ID_SUCURSAL"].values,
                    hovertemplate=(
                        "<b>%{customdata}</b><br>"
                        f"Cluster: {cl}<br>"
                        "VN/m²: $%{y:,.0f}<extra></extra>"
                    ),
                ))
            st.plotly_chart(fig_box, use_container_width=True)

            # ── Tabla: tiendas con VN/m² fuera del P25-P75 de su cluster ────
            st.markdown("#### Outliers por Cluster (fuera del IQR)")
            _outlier_rows = []
            for cl in _clusters:
                _sub = df_td[df_td[_cluster_col] == cl].copy()
                if len(_sub) < 3:
                    continue
                _p25 = _sub["VN_M2"].quantile(0.25)
                _p75 = _sub["VN_M2"].quantile(0.75)
                _iqr = _p75 - _p25
                _sub["CLUSTER_MED"] = _sub["VN_M2"].median()
                _sub["DESV_VS_MED"] = (_sub["VN_M2"] - _sub["CLUSTER_MED"]) / _sub["CLUSTER_MED"] * 100
                _sub["TIPO_OUTLIER"] = np.where(
                    _sub["VN_M2"] > _p75 + 1.5 * _iqr, "SOBRE-PERFORMER",
                    np.where(_sub["VN_M2"] < _p25 - 1.5 * _iqr, "BAJO-PERFORMER", None)
                )
                _outliers = _sub[_sub["TIPO_OUTLIER"].notna()]
                _outlier_rows.append(_outliers)

            if _outlier_rows:
                _df_out = pd.concat(_outlier_rows, ignore_index=True)
                _show_cols = [
                    c for c in
                    ["_LABEL", _cluster_col, "SUPERVISOR", "MTS2", "VN_TOTAL",
                     "VN_M2", "CLUSTER_MED", "DESV_VS_MED", "TIPO_OUTLIER"]
                    if c in _df_out.columns
                ]
                _df_out_disp = _df_out[_show_cols].rename(columns={
                    "_LABEL": "Tienda", _cluster_col: "Cluster",
                    "SUPERVISOR": "Supervisor", "MTS2": "m²",
                    "VN_TOTAL": "VN Total", "VN_M2": "VN/m²",
                    "CLUSTER_MED": "Mediana Cluster", "DESV_VS_MED": "Desv. % vs Mediana",
                    "TIPO_OUTLIER": "Tipo",
                })
                _df_out_disp = _df_out_disp.sort_values("Desv. % vs Mediana", ascending=False).reset_index(drop=True)
                for _clp_col in ["VN Total", "VN/m²", "Mediana Cluster"]:
                    if _clp_col in _df_out_disp.columns:
                        _df_out_disp[_clp_col] = _df_out_disp[_clp_col].apply(fmt_clp)
                st.dataframe(
                    _df_out_disp,
                    use_container_width=True,
                    column_config={
                        "VN Total": st.column_config.TextColumn(),
                        "VN/m²": st.column_config.TextColumn(),
                        "Mediana Cluster": st.column_config.TextColumn(),
                        "Desv. % vs Mediana": st.column_config.NumberColumn(format="%.1f%%"),
                        "m²": st.column_config.NumberColumn(format="%d"),
                    },
                )
            else:
                st.success("No se detectaron outliers estadísticos en ningún cluster.")
