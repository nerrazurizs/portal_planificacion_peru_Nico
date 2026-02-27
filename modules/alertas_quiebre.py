"""Alertas de Quiebre de Stock - Prediccion de desabastecimiento por SKU."""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from datetime import datetime, timedelta

from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format
from utils.export import download_buttons
from utils.ui_animations import lottie_spinner
from config import COLORS, dorel_layout, apply_pm_filter


# ============================================================================
# CONSTANTS
# ============================================================================
_SEMAFORO_COLORS = {
    "CRITICO": "#E53935",
    "ALERTA": "#FB8C00",
    "OK": "#43A047",
    "SIN VENTA": "#9E9E9E",
}

_SEMAFORO_ORDER = ["CRITICO", "ALERTA", "OK", "SIN VENTA"]


# ============================================================================
# HELPERS
# ============================================================================

def _load_data(conn):
    """Load all data sources needed for stock-out alert analysis (centralized cache)."""
    with lottie_spinner("snowflake"):
        stock = cq.stock_proyeccion(conn)
        ventas = cq.ventas_diarias_90d(conn)
        maestra = cq.maestra(conn)
        comex = cq.comex_full(conn)
        stock_higiene = cq.stock_higiene(conn)
        tienda_dim = cq.tienda_dim(conn)
    return stock, ventas, maestra, comex, stock_higiene, tienda_dim


def _process_stock(stock):
    """Aggregate stock by SKU (total across CD + TIENDA)."""
    stk = stock.groupby("SKU_PRODUCTO", as_index=False).agg(
        STOCK_TOTAL=("STOCK_UNIDADES", "sum"),
        PERFIL_TIENDAS=("PERFIL_TIENDAS", "sum"),
    )
    return stk


def _process_ventas(ventas):
    """Calculate daily avg and total sales per SKU over last 90 days."""
    if ventas.empty:
        return pd.DataFrame(columns=[
            "SKU_PRODUCTO", "VENTA_DIARIA_PROM", "VENTA_TOTAL_90D",
            "DIAS_CON_VENTA", "VN_DIARIO_EST",
        ])

    # Count distinct days in the dataset for proper avg
    n_dias_global = max(ventas["FECHA"].nunique(), 1)

    by_sku = ventas.groupby("SKU_PRODUCTO", as_index=False).agg(
        VENTA_TOTAL_90D=("UNIDADES", "sum"),
        DIAS_CON_VENTA=("FECHA", "nunique"),
    )
    by_sku["VENTA_DIARIA_PROM"] = by_sku["VENTA_TOTAL_90D"] / n_dias_global
    # Estimate daily net value (rough: use units * avg)
    by_sku["VN_DIARIO_EST"] = by_sku["VENTA_DIARIA_PROM"]  # placeholder, enrich later
    return by_sku


def _process_comex(comex):
    """Get next inbound ETA per SKU from pending COMEX orders."""
    if comex.empty:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "PROXIMA_ETA", "QTY_EN_TRANSITO"])

    # Normalize ETA columns
    for col in ["ETA", "ETA_CALC", "FECHA_ENTREGA"]:
        if col in comex.columns:
            comex[col] = pd.to_datetime(comex[col], errors="coerce")

    # Use ETA_CALC if available, else ETA, else FECHA_ENTREGA
    if "ETA_CALC" in comex.columns:
        comex["ETA_FINAL"] = comex["ETA_CALC"]
    elif "ETA" in comex.columns:
        comex["ETA_FINAL"] = comex["ETA"]
    elif "FECHA_ENTREGA" in comex.columns:
        comex["ETA_FINAL"] = comex["FECHA_ENTREGA"]
    else:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "PROXIMA_ETA", "QTY_EN_TRANSITO"])

    today = pd.Timestamp.now().normalize()
    # Only future ETAs
    pending = comex[comex["ETA_FINAL"] >= today].copy()
    if pending.empty:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "PROXIMA_ETA", "QTY_EN_TRANSITO"])

    # SKU column might be SKU_PRODUCTO or MATERIAL
    sku_col = "SKU_PRODUCTO"
    if sku_col not in pending.columns:
        for alt_col in ["MATERIAL", "SKU", "COD_MATERIAL"]:
            if alt_col in pending.columns:
                pending = pending.rename(columns={alt_col: "SKU_PRODUCTO"})
                break

    if "SKU_PRODUCTO" not in pending.columns:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "PROXIMA_ETA", "QTY_EN_TRANSITO"])

    # Qty column
    qty_col = None
    for c in ["FORECAST_COMPRA", "CANTIDAD", "QTY", "UNIDADES"]:
        if c in pending.columns:
            qty_col = c
            break

    agg = pending.groupby("SKU_PRODUCTO", as_index=False).agg(
        PROXIMA_ETA=("ETA_FINAL", "min"),
        QTY_EN_TRANSITO=(qty_col if qty_col else "ETA_FINAL", "count" if not qty_col else "sum"),
    )
    return agg


def _classify_alerts(df):
    """Assign semaforo category based on coverage days and inbound status."""
    conditions = [
        df["SIN_VENTA"],
        (df["DIAS_COBERTURA"] < 7) & (~df["REPO_CUBRE"]),
        (df["DIAS_COBERTURA"] < 30) | ((df["DIAS_COBERTURA"] < 30) & (~df["REPO_CUBRE"])),
    ]
    choices = ["SIN VENTA", "CRITICO", "ALERTA"]
    df["SEMAFORO"] = np.select(conditions, choices, default="OK")
    return df


# ============================================================================
# MAIN ANALYSIS
# ============================================================================

def _build_alert_table(stock, ventas, maestra, comex, stock_higiene=None, tienda_dim=None):
    """Build the complete alert DataFrame."""
    stk = _process_stock(stock)
    vta = _process_ventas(ventas)
    cmx = _process_comex(comex)

    # Base: all SKUs with stock
    df = stk.merge(vta, on="SKU_PRODUCTO", how="left")
    df = df.merge(cmx, on="SKU_PRODUCTO", how="left")

    # Enrich with maestra
    maestra_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                    "SKU_NOM_PRODUCTO", "PROCEDENCIA", "ULTIMO_COSTO"]
    available = [c for c in maestra_cols if c in maestra.columns]
    if available:
        maestra_dedup = maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
        df = df.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")

    # Fill NaN
    df["VENTA_DIARIA_PROM"] = df["VENTA_DIARIA_PROM"].fillna(0)
    df["VENTA_TOTAL_90D"] = df["VENTA_TOTAL_90D"].fillna(0)
    df["DIAS_CON_VENTA"] = df["DIAS_CON_VENTA"].fillna(0)

    # Flag: sin venta
    df["SIN_VENTA"] = df["VENTA_DIARIA_PROM"] <= 0

    # Coverage days
    df["DIAS_COBERTURA"] = np.where(
        df["VENTA_DIARIA_PROM"] > 0,
        df["STOCK_TOTAL"] / df["VENTA_DIARIA_PROM"],
        999,
    )
    df["DIAS_COBERTURA"] = df["DIAS_COBERTURA"].clip(upper=999)

    # Estimated stockout date
    today = pd.Timestamp.now().normalize()
    df["FECHA_QUIEBRE_EST"] = today + pd.to_timedelta(df["DIAS_COBERTURA"].clip(upper=365), unit="D")
    df.loc[df["DIAS_COBERTURA"] >= 999, "FECHA_QUIEBRE_EST"] = pd.NaT

    # Inbound coverage check
    df["PROXIMA_ETA"] = pd.to_datetime(df["PROXIMA_ETA"], errors="coerce")
    df["QTY_EN_TRANSITO"] = pd.to_numeric(df["QTY_EN_TRANSITO"], errors="coerce").fillna(0)
    df["TIENE_REPO"] = df["PROXIMA_ETA"].notna() & (df["QTY_EN_TRANSITO"] > 0)
    df["REPO_CUBRE"] = df["TIENE_REPO"] & (
        df["PROXIMA_ETA"] <= df["FECHA_QUIEBRE_EST"]
    )

    # Days gap between stockout and next inbound
    df["DIAS_GAP"] = np.where(
        df["TIENE_REPO"] & df["FECHA_QUIEBRE_EST"].notna(),
        (df["PROXIMA_ETA"] - df["FECHA_QUIEBRE_EST"]).dt.days,
        np.nan,
    )

    # Classify
    df = _classify_alerts(df)

    # VN at risk = daily avg * ULTIMO_COSTO (rough estimate)
    if "ULTIMO_COSTO" in df.columns:
        df["VN_EN_RIESGO"] = df["VENTA_DIARIA_PROM"] * df["ULTIMO_COSTO"].fillna(0) * 30
    else:
        df["VN_EN_RIESGO"] = 0

    # ── Supervisor mapping: SKU → set of supervisors with stock > 0 ──────────
    # Requires stock_higiene (per-store SKU stock) + tienda_dim (store → supervisor)
    df["SUPERVISORES"] = "Sin Supervisor"
    if (
        stock_higiene is not None
        and tienda_dim is not None
        and not stock_higiene.empty
        and not tienda_dim.empty
        and "ID_SUCURSAL" in stock_higiene.columns
        and "ID_SUCURSAL" in tienda_dim.columns
        and "SUPERVISOR" in tienda_dim.columns
    ):
        try:
            # Solo tiendas con stock positivo
            _sth = stock_higiene[
                (stock_higiene.get("CANAL_DE_DISTRIBUCION", pd.Series()) == "TIENDA")
                & (pd.to_numeric(stock_higiene.get("STOCK_UNIDADES", pd.Series()), errors="coerce").fillna(0) > 0)
            ].copy() if "CANAL_DE_DISTRIBUCION" in stock_higiene.columns else stock_higiene.copy()

            _td = tienda_dim[["ID_SUCURSAL", "SUPERVISOR"]].copy()
            _td["SUPERVISOR"] = _td["SUPERVISOR"].fillna("Sin Supervisor")

            _sth_td = _sth[["SKU_PRODUCTO", "ID_SUCURSAL"]].merge(_td, on="ID_SUCURSAL", how="left")
            _sth_td = _sth_td[_sth_td["SKU_PRODUCTO"].notna()]

            # Aggregate: one comma-separated string of unique supervisors per SKU
            _sku_sup = (
                _sth_td.groupby("SKU_PRODUCTO")["SUPERVISOR"]
                .apply(lambda x: ", ".join(sorted({
                    str(s) for s in x
                    if pd.notna(s) and str(s) not in ("Sin Supervisor", "nan", "")
                })))
                .reset_index()
            )
            _sku_sup.columns = ["SKU_PRODUCTO", "SUPERVISORES"]
            _sku_sup["SUPERVISORES"] = _sku_sup["SUPERVISORES"].replace("", "Sin Supervisor")

            df = df.merge(_sku_sup, on="SKU_PRODUCTO", how="left")
            df["SUPERVISORES"] = df["SUPERVISORES"].fillna("Sin Supervisor")
        except Exception:
            pass  # si falla, queda con "Sin Supervisor" por defecto

    return df


# ============================================================================
# DASHBOARD RENDERING
# ============================================================================

def _render_kpis(df):
    """Display KPI cards."""
    total = len(df)
    criticos = (df["SEMAFORO"] == "CRITICO").sum()
    alerta = (df["SEMAFORO"] == "ALERTA").sum()
    ok = (df["SEMAFORO"] == "OK").sum()
    sin_venta = (df["SEMAFORO"] == "SIN VENTA").sum()

    c1, c2, c3, c4, c5 = st.columns(5)

    with c1:
        st.html(f"""
        <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                     border-top:4px solid {COLORS["primary"]};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
            <div style='font-size:2rem;font-weight:bold;color:{COLORS["primary"]}'>{total:,}</div>
            <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>Total SKUs</div>
        </div>""")

    with c2:
        st.html(f"""
        <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                     border-top:4px solid {_SEMAFORO_COLORS["CRITICO"]};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
            <div style='font-size:2rem;font-weight:bold;color:{_SEMAFORO_COLORS["CRITICO"]}'>{criticos:,}</div>
            <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>Criticos (&lt;7d)</div>
        </div>""")

    with c3:
        st.html(f"""
        <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                     border-top:4px solid {_SEMAFORO_COLORS["ALERTA"]};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
            <div style='font-size:2rem;font-weight:bold;color:{_SEMAFORO_COLORS["ALERTA"]}'>{alerta:,}</div>
            <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>Alerta (&lt;30d)</div>
        </div>""")

    with c4:
        st.html(f"""
        <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                     border-top:4px solid {_SEMAFORO_COLORS["OK"]};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
            <div style='font-size:2rem;font-weight:bold;color:{_SEMAFORO_COLORS["OK"]}'>{ok:,}</div>
            <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>OK (&ge;30d)</div>
        </div>""")

    with c5:
        st.html(f"""
        <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                     border-top:4px solid {_SEMAFORO_COLORS["SIN VENTA"]};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
            <div style='font-size:2rem;font-weight:bold;color:{_SEMAFORO_COLORS["SIN VENTA"]}'>{sin_venta:,}</div>
            <div style='font-size:0.85rem;color:{COLORS["medium_gray"]};text-transform:uppercase'>Sin Venta</div>
        </div>""")

    st.html("<br>")


def _render_semaforo_chart(df):
    """Donut chart showing alert distribution."""
    counts = df["SEMAFORO"].value_counts().reset_index()
    counts.columns = ["Semaforo", "Cantidad"]

    # Ensure order
    cat_order = [s for s in _SEMAFORO_ORDER if s in counts["Semaforo"].values]
    color_range = [_SEMAFORO_COLORS[s] for s in cat_order]

    fig = go.Figure(layout=dorel_layout(
        height=350, margin=dict(l=20, r=20, t=40, b=20), showlegend=True,
        title_text="Distribucion por Semaforo",
    ))
    fig.add_trace(go.Pie(
        labels=counts["Semaforo"].tolist(),
        values=counts["Cantidad"].tolist(),
        hole=0.5,
        marker=dict(colors=color_range),
        textinfo="percent+label",
        hovertemplate="Semaforo: %{label}<br>Cantidad: %{value:,}<extra></extra>",
    ))
    st.plotly_chart(fig, use_container_width=True)


def _render_top_criticos(df):
    """Horizontal bar chart of top critical SKUs by VN at risk."""
    criticos = df[df["SEMAFORO"].isin(["CRITICO", "ALERTA"])].copy()
    if criticos.empty:
        st.info("No hay SKUs criticos ni en alerta.")
        return

    top = criticos.nlargest(20, "VN_EN_RIESGO")
    nombre_col = "SKU_NOM_PRODUCTO" if "SKU_NOM_PRODUCTO" in top.columns else "SKU_PRODUCTO"
    top["LABEL"] = top["SKU_PRODUCTO"].astype(str) + " - " + top.get(nombre_col, top["SKU_PRODUCTO"]).astype(str)
    top["LABEL"] = top["LABEL"].str[:50]

    color_range = [_SEMAFORO_COLORS.get(s, "#999") for s in ["CRITICO", "ALERTA"]]

    top_sorted = top.sort_values("VN_EN_RIESGO", ascending=True)

    fig = go.Figure(layout=dorel_layout(
        height=500, title_text="Top 20 SKUs en Riesgo (por VN Mensual)",
        xaxis_title="VN Mensual en Riesgo ($)", yaxis_title="",
    ))
    for sem in ["CRITICO", "ALERTA"]:
        sub = top_sorted[top_sorted["SEMAFORO"] == sem]
        if sub.empty:
            continue
        fig.add_trace(go.Bar(
            x=sub["VN_EN_RIESGO"],
            y=sub["LABEL"],
            orientation="h",
            name=sem,
            marker_color=_SEMAFORO_COLORS.get(sem, "#999"),
            customdata=np.column_stack([
                sub["SKU_PRODUCTO"], sub["DIAS_COBERTURA"], sub["SEMAFORO"],
            ]),
            hovertemplate=(
                "SKU: %{customdata[0]}<br>"
                "Dias Cobertura: %{customdata[1]:.0f}<br>"
                "VN en Riesgo: $%{x:,.0f}<br>"
                "Estado: %{customdata[2]}<extra></extra>"
            ),
        ))
    st.plotly_chart(fig, use_container_width=True)


def _render_timeline(df):
    """Scatter chart: expected stockout date vs daily sales rate."""
    active = df[(df["SEMAFORO"] != "SIN VENTA") & (df["FECHA_QUIEBRE_EST"].notna())].copy()
    if active.empty:
        st.info("No hay datos para timeline de quiebres.")
        return

    # Limit to next 180 days
    today = pd.Timestamp.now().normalize()
    cutoff = today + timedelta(days=180)
    active = active[active["FECHA_QUIEBRE_EST"] <= cutoff]
    if active.empty:
        st.info("No hay quiebres esperados en los proximos 180 dias.")
        return

    cat_order = [s for s in _SEMAFORO_ORDER if s in active["SEMAFORO"].unique()]
    color_range = [_SEMAFORO_COLORS[s] for s in cat_order]

    fig = go.Figure(layout=dorel_layout(
        height=400, title_text="Timeline de Quiebres Esperados (prox. 180 dias)",
        xaxis_title="Fecha Estimada de Quiebre",
        yaxis_title="Venta Diaria Prom (Und)",
    ))
    for sem, clr in zip(cat_order, color_range):
        sub = active[active["SEMAFORO"] == sem]
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub["FECHA_QUIEBRE_EST"],
            y=sub["VENTA_DIARIA_PROM"],
            mode="markers",
            name=sem,
            marker=dict(color=clr, size=8),
            customdata=np.column_stack([
                sub["SKU_PRODUCTO"], sub["DIAS_COBERTURA"], sub["STOCK_TOTAL"],
            ]),
            hovertemplate=(
                "SKU: %{customdata[0]}<br>"
                "Quiebre Est.: %{x|%Y-%m-%d}<br>"
                "Dias Cob.: %{customdata[1]:.0f}<br>"
                "Stock Actual: %{customdata[2]:,.0f}<br>"
                "Venta/Dia: %{y:.1f}<extra></extra>"
            ),
        ))
    fig.add_vline(x=today, line_dash="dash", line_color="black")
    st.plotly_chart(fig, use_container_width=True)


def _render_heatmap(df):
    """Heatmap of % critical SKUs by Area x Linea."""
    if "AREA" not in df.columns or "LINEA" not in df.columns:
        return

    active = df[df["SEMAFORO"] != "SIN VENTA"].copy()
    if active.empty:
        return

    pivot = active.groupby(["AREA", "LINEA"]).agg(
        TOTAL=("SKU_PRODUCTO", "count"),
        CRITICOS=("SEMAFORO", lambda x: (x == "CRITICO").sum()),
    ).reset_index()
    pivot["PCT_CRITICO"] = (pivot["CRITICOS"] / pivot["TOTAL"] * 100).round(1)

    # Only show combinations with data
    if pivot.empty:
        return

    lineas = sorted(pivot["LINEA"].unique())
    areas = sorted(pivot["AREA"].unique())

    # Build z-matrix (areas=rows, lineas=cols)
    z_matrix = []
    text_matrix = []
    customdata_matrix = []
    for area in areas:
        row_z = []
        row_t = []
        row_c = []
        for linea in lineas:
            match = pivot[(pivot["AREA"] == area) & (pivot["LINEA"] == linea)]
            if match.empty:
                row_z.append(None)
                row_t.append("")
                row_c.append([0, 0])
            else:
                val = match["PCT_CRITICO"].iloc[0]
                row_z.append(val)
                row_t.append(f"{val:.0f}")
                row_c.append([int(match["TOTAL"].iloc[0]), int(match["CRITICOS"].iloc[0])])
        z_matrix.append(row_z)
        text_matrix.append(row_t)
        customdata_matrix.append(row_c)

    fig = go.Figure(layout=dorel_layout(
        height=400, title_text="% SKUs Criticos por Area x Linea",
        xaxis_title="Linea", yaxis_title="Area",
    ))
    fig.add_trace(go.Heatmap(
        z=z_matrix, x=lineas, y=areas,
        colorscale="Reds", colorbar_title="% Critico",
        customdata=customdata_matrix,
        hovertemplate=(
            "Area: %{y}<br>Linea: %{x}<br>"
            "Total SKUs: %{customdata[0]}<br>"
            "Criticos: %{customdata[1]}<br>"
            "% Critico: %{z:.1f}<extra></extra>"
        ),
    ))
    # Add text annotations
    for i, area in enumerate(areas):
        for j, linea in enumerate(lineas):
            if z_matrix[i][j] is not None:
                fig.add_annotation(
                    x=linea, y=area, text=text_matrix[i][j],
                    showarrow=False, font=dict(
                        size=11,
                        color="white" if z_matrix[i][j] > 50 else "black",
                    ),
                )
    st.plotly_chart(fig, use_container_width=True)


# ============================================================================
# MAIN RENDER
# ============================================================================

def render_alertas_quiebre(conn):
    """Main entry point: renders the stock-out alerts dashboard."""
    st.html("<h2 class='sub-header'>Alertas de Quiebre de Stock</h2>")
    st.caption("Prediccion de desabastecimiento basada en stock actual, velocidad de venta (90d) y reposicion COMEX.")

    # Load data (cached in session_state to avoid re-querying on every rerun)
    if st.button("Actualizar Datos", key="btn_refresh_alertas"):
        st.session_state.pop("alertas_data", None)

    if "alertas_data" not in st.session_state:
        stock, ventas, maestra, comex, stock_higiene, tienda_dim = _load_data(conn)
        df = _build_alert_table(stock, ventas, maestra, comex, stock_higiene, tienda_dim)
        st.session_state["alertas_data"] = df

    df = st.session_state["alertas_data"].copy()
    df = apply_pm_filter(df)

    if df.empty:
        st.warning("No se encontraron datos de stock.")
        return

    # ── Filters ──────────────────────────────────────────────────────
    st.markdown("### Filtros")
    fc1, fc2, fc3, fc4 = st.columns(4)

    areas = sorted(df["AREA"].dropna().unique()) if "AREA" in df.columns else []
    lineas_all = sorted(df["LINEA"].dropna().unique()) if "LINEA" in df.columns else []
    marcas_all = sorted(df["MARCA"].dropna().unique()) if "MARCA" in df.columns else []

    with fc1:
        sel_area = st.multiselect("Area", areas, key="alq_area")
    with fc2:
        lineas_filt = sorted(df[df["AREA"].isin(sel_area)]["LINEA"].dropna().unique()) if sel_area else lineas_all
        sel_linea = st.multiselect("Linea", lineas_filt, key="alq_linea")
    with fc3:
        _m = df.copy()
        if sel_area:
            _m = _m[_m["AREA"].isin(sel_area)]
        if sel_linea:
            _m = _m[_m["LINEA"].isin(sel_linea)]
        marcas_filt = sorted(_m["MARCA"].dropna().unique()) if "MARCA" in _m.columns else marcas_all
        sel_marca = st.multiselect("Marca", marcas_filt, key="alq_marca")
    with fc4:
        sel_semaforo = st.multiselect(
            "Semaforo", _SEMAFORO_ORDER, default=["CRITICO", "ALERTA"], key="alq_semaforo"
        )

    # Filtro por supervisor (solo si hay datos)
    sel_supervisor = []
    if "SUPERVISORES" in df.columns:
        # Descomponer supervisores (pueden ser múltiples por SKU)
        _all_sups = sorted({
            s.strip()
            for val in df["SUPERVISORES"].dropna()
            for s in str(val).split(",")
            if s.strip() and s.strip() != "Sin Supervisor"
        })
        if _all_sups:
            sel_supervisor = st.multiselect(
                "Supervisor / Zona (tiendas con stock)",
                _all_sups,
                key="alq_supervisor",
                help="Filtra SKUs que tienen stock en tiendas bajo el supervisor seleccionado.",
            )

    # Apply filters
    mask = pd.Series(True, index=df.index)
    if sel_area:
        mask &= df["AREA"].isin(sel_area)
    if sel_linea:
        mask &= df["LINEA"].isin(sel_linea)
    if sel_marca:
        mask &= df["MARCA"].isin(sel_marca)
    if sel_semaforo:
        mask &= df["SEMAFORO"].isin(sel_semaforo)
    if sel_supervisor and "SUPERVISORES" in df.columns:
        # SKU visible si cualquiera de sus supervisores está seleccionado
        mask &= df["SUPERVISORES"].apply(
            lambda v: any(s in str(v) for s in sel_supervisor)
        )
    df_filt = df[mask]

    if df_filt.empty:
        st.warning("No hay datos con los filtros seleccionados.")
        return

    st.markdown("---")

    # ── KPIs ─────────────────────────────────────────────────────────
    _render_kpis(df_filt)

    # ── Charts ───────────────────────────────────────────────────────
    col_left, col_right = st.columns(2)
    with col_left:
        _render_semaforo_chart(df_filt)
    with col_right:
        _render_top_criticos(df_filt)

    st.markdown("---")

    col_tl, col_hm = st.columns(2)
    with col_tl:
        _render_timeline(df_filt)
    with col_hm:
        _render_heatmap(df_filt)

    st.markdown("---")

    # ── Detail Table ─────────────────────────────────────────────────
    st.markdown("### Detalle por SKU")

    display_cols = [
        "SKU_PRODUCTO", "SEMAFORO", "DIAS_COBERTURA", "STOCK_TOTAL",
        "VENTA_DIARIA_PROM", "VENTA_TOTAL_90D", "FECHA_QUIEBRE_EST",
        "TIENE_REPO", "PROXIMA_ETA", "QTY_EN_TRANSITO", "DIAS_GAP",
    ]
    # Add maestra cols + supervisor if available
    for c in ["AREA", "LINEA", "SUBLINEA", "MARCA", "SKU_NOM_PRODUCTO"]:
        if c in df_filt.columns:
            display_cols.insert(1, c)
    if "SUPERVISORES" in df_filt.columns:
        display_cols.append("SUPERVISORES")

    display_cols = [c for c in display_cols if c in df_filt.columns]
    df_display = df_filt[display_cols].sort_values("DIAS_COBERTURA").reset_index(drop=True)

    # Format
    fmt_df = df_display.copy()
    if "DIAS_COBERTURA" in fmt_df.columns:
        fmt_df["DIAS_COBERTURA"] = fmt_df["DIAS_COBERTURA"].apply(
            lambda x: f"{x:.0f}" if x < 999 else "N/A"
        )
    if "VENTA_DIARIA_PROM" in fmt_df.columns:
        fmt_df["VENTA_DIARIA_PROM"] = fmt_df["VENTA_DIARIA_PROM"].apply(lambda x: f"{x:.1f}")
    if "FECHA_QUIEBRE_EST" in fmt_df.columns:
        fmt_df["FECHA_QUIEBRE_EST"] = fmt_df["FECHA_QUIEBRE_EST"].apply(
            lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) else "N/A"
        )

    st.dataframe(fmt_df, use_container_width=True, height=500)

    # ── Export ────────────────────────────────────────────────────────
    download_buttons(df_display, "alertas_quiebre")
