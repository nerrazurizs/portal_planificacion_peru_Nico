"""Operaciones Supply — Pedidos, Picking, Stock Actual, Despachos.

Visualizes the supply chain operations flow: transfer orders (CD→tienda),
picking progress, dispatch tracking, and current inventory by warehouse.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format, fmt_clp
from utils.ui_animations import lottie_spinner
from utils.export import download_buttons
from utils.ui_components import page_header, simple_kpi_card
from config import COLORS, dorel_layout, apply_pm_filter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ESTADO_PEDIDO_COLORS = {
    "Creado": COLORS.get("tertiary_blue", "#2DAAFF"),
    "Enviado": COLORS.get("secondary", "#632CFF"),
    "Recibido": COLORS.get("status_on_track", "#22c55e"),
    "Cancelado": COLORS.get("status_critical", "#ef4444"),
    "Desconocido": COLORS.get("medium_gray", "#94a3b8"),
}

_ESTADO_PICKING_COLORS = {
    "Activado": COLORS.get("tertiary_blue", "#2DAAFF"),
    "Iniciado": COLORS.get("secondary", "#632CFF"),
    "Completado": COLORS.get("status_on_track", "#22c55e"),
    "Cancelado": COLORS.get("status_critical", "#ef4444"),
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_data(conn):
    """Load all supply datasets using cached queries."""
    with lottie_spinner("snowflake"):
        pedidos = cq.supply_pedidos_transfer(conn)
        picking = cq.supply_picking(conn)
        stock_act = cq.supply_stock_actual(conn)
        bultos = cq.supply_bultos(conn)
        fedex = cq.supply_despachos_fedex(conn)
        maestra = cq.maestra(conn)
    return pedidos, picking, stock_act, bultos, fedex, maestra


def _coerce_dates(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Convert date columns to datetime."""
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def _coerce_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Convert numeric columns safely."""
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_operaciones_supply(conn):
    """Main entry point — called from app.py."""
    st.html(page_header(
        "Operaciones Supply",
        "Pedidos de transferencia, picking, despachos y stock actual",
    ))

    if conn is None:
        st.warning("No hay conexion a Snowflake.")
        return

    # Load data
    pedidos, picking, stock_act, bultos, fedex, maestra = _load_data(conn)
    pedidos = apply_pm_filter(pedidos)
    stock_act = apply_pm_filter(stock_act)

    # --- Coerce types ---
    pedidos = _coerce_dates(pedidos, [
        "FECHA_CREACION", "FECHA_MODIFICACION", "FECHA_ENVIO", "FECHA_RECIBO",
    ])
    pedidos = _coerce_numeric(pedidos, [
        "CANTIDAD_TRANSFERIDA", "CANTIDAD_ENVIADA", "CANTIDAD_RECIBIDA",
        "CANTIDAD_BAJA", "CANTIDAD_PENDIENTE",
    ])

    picking = _coerce_dates(picking, [
        "FECHA_ACTIVACION", "FECHA_INICIO", "FECHA_TERMINO",
    ])
    picking = _coerce_numeric(picking, ["CANTIDAD", "RESERVADO", "MINUTOS_PICKING"])

    stock_act = _coerce_numeric(stock_act, [
        "STOCK_ACTUAL", "STOCK_RESERVADO", "CANTIDAD_ORDENADA",
    ])

    bultos = _coerce_dates(bultos, ["FECHA_CREACION"])
    bultos = _coerce_numeric(bultos, ["CANTIDAD"])

    fedex = _coerce_dates(fedex, ["DATE_TIME"])
    fedex = _coerce_numeric(fedex, ["PESO", "LARGO", "ANCHO", "ALTO"])

    # ── Global filters ──
    today = pd.Timestamp.now().normalize()
    default_start = today - timedelta(days=30)

    f1, f2, f3 = st.columns(3)
    with f1:
        date_range = st.date_input(
            "Rango de fechas",
            value=[default_start.date(), today.date()],
            key="supply_date_range",
        )
        if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
            dt_start = pd.Timestamp(date_range[0])
            dt_end = pd.Timestamp(date_range[1]) + timedelta(days=1)  # inclusive
        else:
            dt_start = default_start
            dt_end = today + timedelta(days=1)

    with f2:
        # Bodega origen filter
        _origenes = ["Todos"]
        if "ORIGEN_NOMBRE" in pedidos.columns:
            _origenes += sorted(pedidos["ORIGEN_NOMBRE"].dropna().unique().tolist())
        sel_origen = st.selectbox("Bodega Origen", _origenes, index=0, key="supply_origen")

    with f3:
        sel_canal_dest = st.selectbox(
            "Canal Destino",
            ["Todos", "CD", "TIENDA", "ETAIL", "MAYORISTA"],
            index=0,
            key="supply_canal_dest",
        )

    # --- Apply date filter ---
    def _filter_by_date(df, date_col):
        if date_col in df.columns:
            mask = (df[date_col] >= dt_start) & (df[date_col] < dt_end)
            return df[mask].copy()
        return df.copy()

    ped_f = _filter_by_date(pedidos, "FECHA_CREACION")
    pick_f = _filter_by_date(picking, "FECHA_ACTIVACION")
    bul_f = _filter_by_date(bultos, "FECHA_CREACION")
    fed_f = _filter_by_date(fedex, "DATE_TIME")

    # Apply origin filter
    if sel_origen != "Todos":
        if "ORIGEN_NOMBRE" in ped_f.columns:
            ped_f = ped_f[ped_f["ORIGEN_NOMBRE"] == sel_origen]
        if "ORIGEN_NOMBRE" in pick_f.columns:
            pick_f = pick_f[pick_f["ORIGEN_NOMBRE"] == sel_origen]

    # Apply canal destino filter
    if sel_canal_dest != "Todos":
        if "CANAL_DESTINO" in ped_f.columns:
            ped_f = ped_f[ped_f["CANAL_DESTINO"].str.upper() == sel_canal_dest.upper()]
        if "DESTINO_NOMBRE" in pick_f.columns and "CANAL_DESTINO" not in pick_f.columns:
            pass  # picking may not have canal_destino directly
        if "CANAL" in stock_act.columns:
            stock_act = stock_act[stock_act["CANAL"].str.upper() == sel_canal_dest.upper()]

    # ══════════════════════════════════════════════════════════════════════
    # TABS
    # ══════════════════════════════════════════════════════════════════════
    tab_resumen, tab_pedidos, tab_picking, tab_stock, tab_despachos = st.tabs([
        "📊 Resumen", "📦 Pedidos Transferencia", "🏗️ Picking",
        "🏬 Stock Actual", "🚚 Despachos",
    ])

    # ==================================================================
    # TAB 1: RESUMEN KPIs
    # ==================================================================
    with tab_resumen:
        _render_resumen(ped_f, pick_f, bul_f, fed_f, today)

    # ==================================================================
    # TAB 2: PEDIDOS DE TRANSFERENCIA
    # ==================================================================
    with tab_pedidos:
        _render_pedidos(ped_f, maestra)

    # ==================================================================
    # TAB 3: PICKING
    # ==================================================================
    with tab_picking:
        _render_picking(pick_f, maestra)

    # ==================================================================
    # TAB 4: STOCK ACTUAL
    # ==================================================================
    with tab_stock:
        _render_stock_actual(stock_act, maestra)

    # ==================================================================
    # TAB 5: DESPACHOS
    # ==================================================================
    with tab_despachos:
        _render_despachos(bul_f, fed_f)


# ---------------------------------------------------------------------------
# TAB 1: Resumen
# ---------------------------------------------------------------------------

def _render_resumen(ped_f, pick_f, bul_f, fed_f, today):
    """Dashboard KPIs overview."""

    # ── KPI Row 1 ──
    # Pedidos periodo
    n_pedidos = ped_f["COD_PEDIDOTRANSFERENCIA"].nunique() if "COD_PEDIDOTRANSFERENCIA" in ped_f.columns else 0
    und_transferidas = ped_f["CANTIDAD_TRANSFERIDA"].sum() if "CANTIDAD_TRANSFERIDA" in ped_f.columns else 0

    # Picking activos
    pick_activo_mask = pd.Series(False, index=pick_f.index)
    if "ESTADO_PICKING" in pick_f.columns:
        pick_activo_mask = pick_f["ESTADO_PICKING"].isin(["Activado", "Iniciado"])
    und_en_picking = pick_f.loc[pick_activo_mask, "CANTIDAD"].sum() if "CANTIDAD" in pick_f.columns else 0

    # Picking completado periodo
    pick_comp_mask = pick_f["ESTADO_PICKING"] == "Completado" if "ESTADO_PICKING" in pick_f.columns else pd.Series(False, index=pick_f.index)
    und_pick_comp = pick_f.loc[pick_comp_mask, "CANTIDAD"].sum() if "CANTIDAD" in pick_f.columns else 0

    # Bultos periodo
    n_bultos = len(bul_f)

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.html(simple_kpi_card(
            "Pedidos Transferencia",
            f"{n_pedidos:,.0f}",
            COLORS.get("primary", "#065E8B"),
            subtitle=f"{und_transferidas:,.0f} unidades solicitadas",
        ))
    with k2:
        st.html(simple_kpi_card(
            "Unidades en Picking",
            f"{und_en_picking:,.0f}",
            COLORS.get("secondary", "#632CFF"),
            subtitle="Activado + Iniciado",
        ))
    with k3:
        st.html(simple_kpi_card(
            "Picking Completado",
            f"{und_pick_comp:,.0f}",
            COLORS.get("status_on_track", "#22c55e"),
            subtitle="Unidades pickeadas",
        ))
    with k4:
        st.html(simple_kpi_card(
            "Bultos Despachados",
            f"{n_bultos:,.0f}",
            COLORS.get("tertiary_teal", "#23CED3"),
            subtitle=f"{bul_f['CANTIDAD'].sum():,.0f} unidades" if "CANTIDAD" in bul_f.columns else "",
        ))

    # ── KPI Row 2 ──
    # Completitud picking
    total_pick = len(pick_f)
    n_completados = pick_comp_mask.sum()
    pct_completitud = (n_completados / total_pick * 100) if total_pick > 0 else 0

    # Tiempo promedio picking
    if "MINUTOS_PICKING" in pick_f.columns:
        _comp_times = pick_f.loc[pick_comp_mask, "MINUTOS_PICKING"]
        _valid_times = _comp_times[(_comp_times > 0) & (_comp_times < 10000)]
        avg_tiempo = _valid_times.mean() if not _valid_times.empty else 0
    else:
        avg_tiempo = 0

    # Tasa cancelacion pedidos
    cancel_mask = ped_f["ESTADO_PEDIDO"] == "Cancelado" if "ESTADO_PEDIDO" in ped_f.columns else pd.Series(False, index=ped_f.index)
    n_cancelados = cancel_mask.sum()
    tasa_cancel = (n_cancelados / n_pedidos * 100) if n_pedidos > 0 else 0

    k5, k6, k7 = st.columns(3)
    with k5:
        _pct_color = COLORS.get("status_on_track", "#22c55e") if pct_completitud >= 80 else COLORS.get("status_at_risk", "#f59e0b") if pct_completitud >= 50 else COLORS.get("status_critical", "#ef4444")
        st.html(simple_kpi_card(
            "Completitud Picking",
            f"{pct_completitud:.1f}%",
            _pct_color,
            subtitle=f"{n_completados:,.0f} de {total_pick:,.0f} pickings",
        ))
    with k6:
        st.html(simple_kpi_card(
            "Tiempo Prom. Picking",
            f"{avg_tiempo:,.0f} min",
            COLORS.get("tertiary_blue", "#2DAAFF"),
            subtitle="Activacion → Termino (completados)",
        ))
    with k7:
        _cc = COLORS.get("status_critical", "#ef4444") if tasa_cancel > 10 else COLORS.get("status_at_risk", "#f59e0b") if tasa_cancel > 5 else COLORS.get("status_on_track", "#22c55e")
        st.html(simple_kpi_card(
            "Tasa Cancelacion",
            f"{tasa_cancel:.1f}%",
            _cc,
            subtitle=f"{n_cancelados:,.0f} pedidos cancelados",
        ))

    st.markdown("---")

    # ── Charts ──
    ch1, ch2 = st.columns(2)

    with ch1:
        st.markdown("**Pedidos creados por dia**")
        if not ped_f.empty and "FECHA_CREACION" in ped_f.columns:
            _ped_day = ped_f.copy()
            _ped_day["DIA"] = _ped_day["FECHA_CREACION"].dt.date
            _state_col = "ESTADO_PEDIDO" if "ESTADO_PEDIDO" in _ped_day.columns else None

            if _state_col:
                _pivot = _ped_day.groupby(["DIA", _state_col]).size().reset_index(name="N")
                fig_ped = go.Figure(layout=dorel_layout(
                    height=350, barmode="stack",
                    xaxis=dict(title=""), yaxis=dict(title="Pedidos"),
                    legend=dict(orientation="h", y=-0.15, xanchor="center", x=0.5),
                ))
                for estado in ["Creado", "Enviado", "Recibido", "Cancelado"]:
                    _sub = _pivot[_pivot[_state_col] == estado]
                    if not _sub.empty:
                        fig_ped.add_trace(go.Bar(
                            x=_sub["DIA"], y=_sub["N"],
                            name=estado,
                            marker_color=_ESTADO_PEDIDO_COLORS.get(estado, "#94a3b8"),
                        ))
                st.plotly_chart(fig_ped, use_container_width=True)
            else:
                _daily = _ped_day.groupby("DIA").size().reset_index(name="N")
                fig_ped = go.Figure(layout=dorel_layout(height=350))
                fig_ped.add_trace(go.Bar(x=_daily["DIA"], y=_daily["N"],
                    marker_color=COLORS.get("primary", "#065E8B")))
                st.plotly_chart(fig_ped, use_container_width=True)
        else:
            st.info("Sin datos de pedidos para el periodo seleccionado.")

    with ch2:
        st.markdown("**Unidades picked por dia**")
        if not pick_f.empty and "FECHA_ACTIVACION" in pick_f.columns and "CANTIDAD" in pick_f.columns:
            _pk_day = pick_f.copy()
            _pk_day["DIA"] = _pk_day["FECHA_ACTIVACION"].dt.date
            _state_col_pk = "ESTADO_PICKING" if "ESTADO_PICKING" in _pk_day.columns else None

            if _state_col_pk:
                _piv = _pk_day.groupby(["DIA", _state_col_pk], as_index=False)["CANTIDAD"].sum()
                fig_pk = go.Figure(layout=dorel_layout(
                    height=350, barmode="stack",
                    xaxis=dict(title=""), yaxis=dict(title="Unidades"),
                    legend=dict(orientation="h", y=-0.15, xanchor="center", x=0.5),
                ))
                for estado in ["Activado", "Iniciado", "Completado", "Cancelado"]:
                    _sub = _piv[_piv[_state_col_pk] == estado]
                    if not _sub.empty:
                        fig_pk.add_trace(go.Bar(
                            x=_sub["DIA"], y=_sub["CANTIDAD"],
                            name=estado,
                            marker_color=_ESTADO_PICKING_COLORS.get(estado, "#94a3b8"),
                        ))
                st.plotly_chart(fig_pk, use_container_width=True)
            else:
                _daily_pk = _pk_day.groupby("DIA", as_index=False)["CANTIDAD"].sum()
                fig_pk = go.Figure(layout=dorel_layout(height=350))
                fig_pk.add_trace(go.Bar(x=_daily_pk["DIA"], y=_daily_pk["CANTIDAD"],
                    marker_color=COLORS.get("secondary", "#632CFF")))
                st.plotly_chart(fig_pk, use_container_width=True)
        else:
            st.info("Sin datos de picking para el periodo seleccionado.")


# ---------------------------------------------------------------------------
# TAB 2: Pedidos de Transferencia
# ---------------------------------------------------------------------------

def _render_pedidos(ped_f, maestra):
    """Detailed view of transfer orders."""

    if ped_f.empty:
        st.info("Sin pedidos de transferencia para el periodo seleccionado.")
        return

    # ── Additional filters ──
    pf1, pf2 = st.columns(2)
    with pf1:
        estados_disp = ["Todos"]
        if "ESTADO_PEDIDO" in ped_f.columns:
            estados_disp += sorted(ped_f["ESTADO_PEDIDO"].dropna().unique().tolist())
        sel_estado = st.selectbox("Estado", estados_disp, key="supply_ped_estado")
    with pf2:
        destinos_disp = ["Todos"]
        if "DESTINO_NOMBRE" in ped_f.columns:
            destinos_disp += sorted(ped_f["DESTINO_NOMBRE"].dropna().unique().tolist())
        sel_destino = st.selectbox("Destino", destinos_disp, key="supply_ped_destino")

    df = ped_f.copy()
    if sel_estado != "Todos" and "ESTADO_PEDIDO" in df.columns:
        df = df[df["ESTADO_PEDIDO"] == sel_estado]
    if sel_destino != "Todos" and "DESTINO_NOMBRE" in df.columns:
        df = df[df["DESTINO_NOMBRE"] == sel_destino]

    # ── KPIs ──
    n_ped = df["COD_PEDIDOTRANSFERENCIA"].nunique() if "COD_PEDIDOTRANSFERENCIA" in df.columns else 0
    und_trans = df["CANTIDAD_TRANSFERIDA"].sum() if "CANTIDAD_TRANSFERIDA" in df.columns else 0
    n_pend = df[df["ESTADO_PEDIDO"] == "Creado"]["COD_PEDIDOTRANSFERENCIA"].nunique() if "ESTADO_PEDIDO" in df.columns and "COD_PEDIDOTRANSFERENCIA" in df.columns else 0
    n_recib = df[df["ESTADO_PEDIDO"] == "Recibido"]["COD_PEDIDOTRANSFERENCIA"].nunique() if "ESTADO_PEDIDO" in df.columns and "COD_PEDIDOTRANSFERENCIA" in df.columns else 0

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.html(simple_kpi_card("Total Pedidos", f"{n_ped:,.0f}", COLORS.get("primary", "#065E8B")))
    with k2:
        st.html(simple_kpi_card("Und. Transferidas", f"{und_trans:,.0f}", COLORS.get("tertiary_teal", "#23CED3")))
    with k3:
        st.html(simple_kpi_card("Pendientes (Creado)", f"{n_pend:,.0f}", COLORS.get("status_at_risk", "#f59e0b")))
    with k4:
        st.html(simple_kpi_card("Recibidos", f"{n_recib:,.0f}", COLORS.get("status_on_track", "#22c55e")))

    st.markdown("---")

    # ── Charts ──
    ch1, ch2 = st.columns(2)

    with ch1:
        st.markdown("**Pedidos por dia (por estado)**")
        if "FECHA_CREACION" in df.columns:
            _d = df.copy()
            _d["DIA"] = _d["FECHA_CREACION"].dt.date
            if "ESTADO_PEDIDO" in _d.columns:
                _piv = _d.groupby(["DIA", "ESTADO_PEDIDO"]).size().reset_index(name="N")
                fig = go.Figure(layout=dorel_layout(
                    height=320, barmode="stack",
                    xaxis=dict(title=""), yaxis=dict(title="Pedidos"),
                    legend=dict(orientation="h", y=-0.15, xanchor="center", x=0.5),
                ))
                for est in ["Creado", "Enviado", "Recibido", "Cancelado"]:
                    _sub = _piv[_piv["ESTADO_PEDIDO"] == est]
                    if not _sub.empty:
                        fig.add_trace(go.Bar(
                            x=_sub["DIA"], y=_sub["N"], name=est,
                            marker_color=_ESTADO_PEDIDO_COLORS.get(est, "#94a3b8"),
                        ))
                st.plotly_chart(fig, use_container_width=True)

    with ch2:
        st.markdown("**Top 15 destinos por volumen**")
        if "DESTINO_NOMBRE" in df.columns and "CANTIDAD_TRANSFERIDA" in df.columns:
            _top = df.groupby("DESTINO_NOMBRE", as_index=False)["CANTIDAD_TRANSFERIDA"].sum()
            _top = _top.nlargest(15, "CANTIDAD_TRANSFERIDA")
            _top = _top.sort_values("CANTIDAD_TRANSFERIDA", ascending=True)
            fig2 = go.Figure(layout=dorel_layout(height=320, showlegend=False))
            fig2.add_trace(go.Bar(
                y=_top["DESTINO_NOMBRE"], x=_top["CANTIDAD_TRANSFERIDA"],
                orientation="h",
                marker_color=COLORS.get("primary", "#065E8B"),
                text=[f"{v:,.0f}" for v in _top["CANTIDAD_TRANSFERIDA"]],
                textposition="auto",
            ))
            st.plotly_chart(fig2, use_container_width=True)

    # ── Detail table ──
    st.markdown("**Detalle Pedidos**")
    show_cols = [c for c in [
        "COD_PEDIDOTRANSFERENCIA", "FECHA_CREACION", "ESTADO_PEDIDO",
        "ORIGEN_NOMBRE", "DESTINO_NOMBRE", "SKU_PRODUCTO",
        "CANTIDAD_TRANSFERIDA", "CANTIDAD_ENVIADA", "CANTIDAD_RECIBIDA",
        "CANTIDAD_PENDIENTE",
    ] if c in df.columns]

    if show_cols:
        st.dataframe(
            df[show_cols].sort_values("FECHA_CREACION", ascending=False) if "FECHA_CREACION" in df.columns else df[show_cols],
            use_container_width=True,
            hide_index=True,
            height=min(600, len(df) * 38 + 40),
            column_config={
                "COD_PEDIDOTRANSFERENCIA": st.column_config.TextColumn("Pedido", width="small"),
                "FECHA_CREACION": st.column_config.DateColumn("Fecha Creacion", format="DD/MM/YYYY"),
                "ESTADO_PEDIDO": st.column_config.TextColumn("Estado", width="small"),
                "ORIGEN_NOMBRE": st.column_config.TextColumn("Origen", width="medium"),
                "DESTINO_NOMBRE": st.column_config.TextColumn("Destino", width="medium"),
                "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
                "CANTIDAD_TRANSFERIDA": st.column_config.NumberColumn("Qty Transf.", format="%d"),
                "CANTIDAD_ENVIADA": st.column_config.NumberColumn("Qty Env.", format="%d"),
                "CANTIDAD_RECIBIDA": st.column_config.NumberColumn("Qty Rec.", format="%d"),
                "CANTIDAD_PENDIENTE": st.column_config.NumberColumn("Qty Pend.", format="%d"),
            },
        )
        download_buttons(df[show_cols], prefix="pedidos_transferencia")


# ---------------------------------------------------------------------------
# TAB 3: Picking
# ---------------------------------------------------------------------------

def _render_picking(pick_f, maestra):
    """Detailed picking analysis."""

    if pick_f.empty:
        st.info("Sin datos de picking para el periodo seleccionado.")
        return

    # ── KPIs ──
    n_activo = 0
    n_iniciado = 0
    n_completado = 0
    if "ESTADO_PICKING" in pick_f.columns:
        n_activo = (pick_f["ESTADO_PICKING"] == "Activado").sum()
        n_iniciado = (pick_f["ESTADO_PICKING"] == "Iniciado").sum()
        n_completado = (pick_f["ESTADO_PICKING"] == "Completado").sum()

    # Avg time (only completed, filter outliers)
    if "MINUTOS_PICKING" in pick_f.columns and "ESTADO_PICKING" in pick_f.columns:
        _comp = pick_f[pick_f["ESTADO_PICKING"] == "Completado"]["MINUTOS_PICKING"]
        _valid = _comp[(_comp > 0) & (_comp < 10000)]
        avg_min = _valid.mean() if not _valid.empty else 0
        median_min = _valid.median() if not _valid.empty else 0
    else:
        avg_min = 0
        median_min = 0

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.html(simple_kpi_card("Picking Activos", f"{n_activo:,.0f}", COLORS.get("tertiary_blue", "#2DAAFF")))
    with k2:
        st.html(simple_kpi_card("En Proceso", f"{n_iniciado:,.0f}", COLORS.get("secondary", "#632CFF")))
    with k3:
        st.html(simple_kpi_card("Completados", f"{n_completado:,.0f}", COLORS.get("status_on_track", "#22c55e")))
    with k4:
        st.html(simple_kpi_card(
            "Tiempo Prom. Picking",
            f"{avg_min:,.0f} min",
            COLORS.get("tertiary_teal", "#23CED3"),
            subtitle=f"Mediana: {median_min:,.0f} min",
        ))

    st.markdown("---")

    # ── Charts ──
    ch1, ch2 = st.columns(2)

    with ch1:
        st.markdown("**Volumen picking por dia (unidades)**")
        if "FECHA_ACTIVACION" in pick_f.columns and "CANTIDAD" in pick_f.columns:
            _pk = pick_f.copy()
            _pk["DIA"] = _pk["FECHA_ACTIVACION"].dt.date
            if "ESTADO_PICKING" in _pk.columns:
                _piv = _pk.groupby(["DIA", "ESTADO_PICKING"], as_index=False)["CANTIDAD"].sum()
                fig = go.Figure(layout=dorel_layout(
                    height=320, barmode="stack",
                    xaxis=dict(title=""), yaxis=dict(title="Unidades"),
                    legend=dict(orientation="h", y=-0.15, xanchor="center", x=0.5),
                ))
                for est in ["Activado", "Iniciado", "Completado", "Cancelado"]:
                    _sub = _piv[_piv["ESTADO_PICKING"] == est]
                    if not _sub.empty:
                        fig.add_trace(go.Bar(
                            x=_sub["DIA"], y=_sub["CANTIDAD"], name=est,
                            marker_color=_ESTADO_PICKING_COLORS.get(est, "#94a3b8"),
                        ))
                st.plotly_chart(fig, use_container_width=True)

    with ch2:
        st.markdown("**Distribucion tiempo picking (completados)**")
        if "MINUTOS_PICKING" in pick_f.columns and "ESTADO_PICKING" in pick_f.columns:
            _comp_times = pick_f.loc[
                (pick_f["ESTADO_PICKING"] == "Completado") &
                (pick_f["MINUTOS_PICKING"] > 0) &
                (pick_f["MINUTOS_PICKING"] < 1440),  # < 24h
                "MINUTOS_PICKING"
            ]
            if not _comp_times.empty:
                fig_hist = go.Figure(layout=dorel_layout(
                    height=320,
                    xaxis=dict(title="Minutos"), yaxis=dict(title="Frecuencia"),
                ))
                fig_hist.add_trace(go.Histogram(
                    x=_comp_times,
                    nbinsx=50,
                    marker_color=COLORS.get("secondary", "#632CFF"),
                    opacity=0.8,
                ))
                st.plotly_chart(fig_hist, use_container_width=True)
            else:
                st.info("Sin datos de tiempos de picking completados.")
        else:
            st.info("Sin datos de tiempos de picking.")

    # ── Productividad por operario ──
    if "COD_OPERARIO" in pick_f.columns and "CANTIDAD" in pick_f.columns:
        st.markdown("**Productividad por Operario (Top 15)**")
        _op = pick_f.copy()
        if "ESTADO_PICKING" in _op.columns:
            _op = _op[_op["ESTADO_PICKING"] == "Completado"]

        if not _op.empty:
            _by_op = _op.groupby("COD_OPERARIO", as_index=False).agg(
                PICKINGS=("COD_PICKING", "nunique") if "COD_PICKING" in _op.columns else ("CANTIDAD", "count"),
                UNIDADES=("CANTIDAD", "sum"),
            )
            if "MINUTOS_PICKING" in _op.columns:
                _times = _op.groupby("COD_OPERARIO", as_index=False)["MINUTOS_PICKING"].mean()
                _times = _times.rename(columns={"MINUTOS_PICKING": "TIEMPO_PROM_MIN"})
                _by_op = _by_op.merge(_times, on="COD_OPERARIO", how="left")
                _by_op["TIEMPO_PROM_MIN"] = _by_op["TIEMPO_PROM_MIN"].round(1)

            _by_op = _by_op.sort_values("UNIDADES", ascending=False).head(15)

            st.dataframe(
                _by_op,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "COD_OPERARIO": st.column_config.TextColumn("Operario", width="small"),
                    "PICKINGS": st.column_config.NumberColumn("# Pickings", format="%d"),
                    "UNIDADES": st.column_config.NumberColumn("Unidades", format="%d"),
                    "TIEMPO_PROM_MIN": st.column_config.NumberColumn("Tiempo Prom (min)", format="%.1f"),
                },
            )

    # ── Detail table ──
    st.markdown("---")
    st.markdown("**Detalle Picking**")
    show_cols = [c for c in [
        "COD_PICKING", "COD_PEDIDOTRANSFERENCIA", "ESTADO_PICKING",
        "FECHA_ACTIVACION", "FECHA_INICIO", "FECHA_TERMINO",
        "ORIGEN_NOMBRE", "DESTINO_NOMBRE", "COD_OPERARIO",
        "SKU_PRODUCTO", "CANTIDAD", "MINUTOS_PICKING",
    ] if c in pick_f.columns]

    if show_cols:
        _sorted = pick_f[show_cols].sort_values("FECHA_ACTIVACION", ascending=False) if "FECHA_ACTIVACION" in pick_f.columns else pick_f[show_cols]
        st.dataframe(
            _sorted,
            use_container_width=True,
            hide_index=True,
            height=min(600, len(pick_f) * 38 + 40),
            column_config={
                "COD_PICKING": st.column_config.TextColumn("Picking", width="small"),
                "COD_PEDIDOTRANSFERENCIA": st.column_config.TextColumn("Pedido", width="small"),
                "ESTADO_PICKING": st.column_config.TextColumn("Estado", width="small"),
                "FECHA_ACTIVACION": st.column_config.DatetimeColumn("Activacion", format="DD/MM/YY HH:mm"),
                "FECHA_INICIO": st.column_config.DatetimeColumn("Inicio", format="DD/MM/YY HH:mm"),
                "FECHA_TERMINO": st.column_config.DatetimeColumn("Termino", format="DD/MM/YY HH:mm"),
                "ORIGEN_NOMBRE": st.column_config.TextColumn("Origen", width="medium"),
                "DESTINO_NOMBRE": st.column_config.TextColumn("Destino", width="medium"),
                "COD_OPERARIO": st.column_config.TextColumn("Operario", width="small"),
                "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
                "CANTIDAD": st.column_config.NumberColumn("Cantidad", format="%d"),
                "MINUTOS_PICKING": st.column_config.NumberColumn("Min. Picking", format="%d"),
            },
        )
        download_buttons(pick_f[show_cols], prefix="picking_detail")


# ---------------------------------------------------------------------------
# TAB 4: Stock Actual
# ---------------------------------------------------------------------------

def _render_stock_actual(stock_act, maestra):
    """Current inventory by warehouse."""

    if stock_act.empty:
        st.info("Sin datos de stock actual.")
        return

    # ── Classify CD vs Tienda ──
    _CD_BODEGAS = {"1100001", "1100002", "1060047", "1060068", "1100008", "1010022"}
    if "COD_BODEGA" in stock_act.columns:
        stock_act["TIPO_BODEGA"] = np.where(
            stock_act["COD_BODEGA"].astype(str).isin(_CD_BODEGAS), "CD", "TIENDA"
        )
    elif "CANAL" in stock_act.columns:
        stock_act["TIPO_BODEGA"] = np.where(
            stock_act["CANAL"].str.upper() == "CD", "CD", "TIENDA"
        )
    else:
        stock_act["TIPO_BODEGA"] = "DESCONOCIDO"

    # ── KPIs ──
    stk_total = stock_act["STOCK_ACTUAL"].sum() if "STOCK_ACTUAL" in stock_act.columns else 0
    stk_cd = stock_act.loc[stock_act["TIPO_BODEGA"] == "CD", "STOCK_ACTUAL"].sum() if "STOCK_ACTUAL" in stock_act.columns else 0
    stk_tienda = stock_act.loc[stock_act["TIPO_BODEGA"] == "TIENDA", "STOCK_ACTUAL"].sum() if "STOCK_ACTUAL" in stock_act.columns else 0
    stk_reservado = stock_act["STOCK_RESERVADO"].sum() if "STOCK_RESERVADO" in stock_act.columns else 0

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.html(simple_kpi_card("Stock Total", f"{stk_total:,.0f}", COLORS.get("primary", "#065E8B")))
    with k2:
        st.html(simple_kpi_card("Stock CD", f"{stk_cd:,.0f}", COLORS.get("tertiary_blue", "#2DAAFF")))
    with k3:
        st.html(simple_kpi_card("Stock Tiendas", f"{stk_tienda:,.0f}", COLORS.get("tertiary_teal", "#23CED3")))
    with k4:
        st.html(simple_kpi_card("Stock Reservado", f"{stk_reservado:,.0f}", COLORS.get("secondary", "#632CFF")))

    st.markdown("---")

    # ── Charts ──
    ch1, ch2 = st.columns(2)

    with ch1:
        st.markdown("**Top 20 bodegas por stock**")
        if "BODEGA_NOMBRE" in stock_act.columns and "STOCK_ACTUAL" in stock_act.columns:
            _by_bod = stock_act.groupby("BODEGA_NOMBRE", as_index=False)["STOCK_ACTUAL"].sum()
            _by_bod = _by_bod.nlargest(20, "STOCK_ACTUAL").sort_values("STOCK_ACTUAL", ascending=True)
            fig = go.Figure(layout=dorel_layout(height=400, showlegend=False))
            fig.add_trace(go.Bar(
                y=_by_bod["BODEGA_NOMBRE"], x=_by_bod["STOCK_ACTUAL"],
                orientation="h",
                marker_color=COLORS.get("primary", "#065E8B"),
                text=[f"{v:,.0f}" for v in _by_bod["STOCK_ACTUAL"]],
                textposition="auto",
            ))
            st.plotly_chart(fig, use_container_width=True)

    with ch2:
        st.markdown("**Distribucion CD vs Tienda**")
        fig_pie = go.Figure(layout=dorel_layout(
            height=400, showlegend=True,
            legend=dict(orientation="h", y=-0.1, xanchor="center", x=0.5),
        ))
        fig_pie.add_trace(go.Pie(
            labels=["CD", "Tienda"],
            values=[stk_cd, stk_tienda],
            hole=0.5,
            marker_colors=[COLORS.get("primary", "#065E8B"), COLORS.get("tertiary_teal", "#23CED3")],
            textinfo="label+percent",
            hovertemplate="%{label}<br>%{value:,.0f} und<br>%{percent}<extra></extra>",
        ))
        st.plotly_chart(fig_pie, use_container_width=True)

    # ── Detail table ──
    st.markdown("**Detalle Stock por Bodega**")
    # Aggregate by bodega
    agg_cols = {"STOCK_ACTUAL": "sum", "STOCK_RESERVADO": "sum", "CANTIDAD_ORDENADA": "sum"}
    _grp_cols = [c for c in ["COD_BODEGA", "BODEGA_NOMBRE", "CANAL", "TIPO_BODEGA"] if c in stock_act.columns]
    _num_cols = {k: v for k, v in agg_cols.items() if k in stock_act.columns}

    if _grp_cols and _num_cols:
        _agg = stock_act.groupby(_grp_cols, as_index=False).agg(_num_cols)
        _agg["N_SKUS"] = stock_act.groupby(_grp_cols)["SKU_PRODUCTO"].nunique().values if "SKU_PRODUCTO" in stock_act.columns else 0
        _agg = _agg.sort_values("STOCK_ACTUAL", ascending=False)

        show_cols = _grp_cols + ["N_SKUS"] + list(_num_cols.keys())
        st.dataframe(
            _agg[show_cols],
            use_container_width=True,
            hide_index=True,
            height=min(600, len(_agg) * 38 + 40),
            column_config={
                "COD_BODEGA": st.column_config.TextColumn("Cod Bodega", width="small"),
                "BODEGA_NOMBRE": st.column_config.TextColumn("Bodega", width="medium"),
                "CANAL": st.column_config.TextColumn("Canal", width="small"),
                "TIPO_BODEGA": st.column_config.TextColumn("Tipo", width="small"),
                "N_SKUS": st.column_config.NumberColumn("# SKUs", format="%d"),
                "STOCK_ACTUAL": st.column_config.NumberColumn("Stock Actual", format="%d"),
                "STOCK_RESERVADO": st.column_config.NumberColumn("Reservado", format="%d"),
                "CANTIDAD_ORDENADA": st.column_config.NumberColumn("Ordenado", format="%d"),
            },
        )
        download_buttons(_agg[show_cols], prefix="stock_actual_bodega")


# ---------------------------------------------------------------------------
# TAB 5: Despachos
# ---------------------------------------------------------------------------

def _render_despachos(bul_f, fed_f):
    """Bultos dispatched + FedEx tracking."""

    # ── KPIs ──
    n_bultos = len(bul_f)
    und_bultos = bul_f["CANTIDAD"].sum() if "CANTIDAD" in bul_f.columns else 0
    n_fedex = fed_f["TRACKING_NUMBER"].nunique() if "TRACKING_NUMBER" in fed_f.columns else 0
    peso_total = fed_f["PESO"].sum() if "PESO" in fed_f.columns else 0

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.html(simple_kpi_card("Bultos Despachados", f"{n_bultos:,.0f}", COLORS.get("primary", "#065E8B")))
    with k2:
        st.html(simple_kpi_card("Unidades en Bultos", f"{und_bultos:,.0f}", COLORS.get("tertiary_teal", "#23CED3")))
    with k3:
        st.html(simple_kpi_card("Despachos FedEx", f"{n_fedex:,.0f}", COLORS.get("secondary", "#632CFF")))
    with k4:
        st.html(simple_kpi_card("Peso Total (kg)", f"{peso_total:,.1f}", COLORS.get("tertiary_blue", "#2DAAFF")))

    st.markdown("---")

    # ── Charts ──
    ch1, ch2 = st.columns(2)

    with ch1:
        st.markdown("**Bultos despachados por dia**")
        if not bul_f.empty and "FECHA_CREACION" in bul_f.columns:
            _b = bul_f.copy()
            _b["DIA"] = _b["FECHA_CREACION"].dt.date
            _daily = _b.groupby("DIA").size().reset_index(name="N_BULTOS")
            fig = go.Figure(layout=dorel_layout(
                height=320, xaxis=dict(title=""), yaxis=dict(title="Bultos"),
            ))
            fig.add_trace(go.Bar(
                x=_daily["DIA"], y=_daily["N_BULTOS"],
                marker_color=COLORS.get("primary", "#065E8B"),
                text=_daily["N_BULTOS"],
                textposition="outside",
            ))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Sin datos de bultos para el periodo.")

    with ch2:
        st.markdown("**FedEx — Distribucion por Estado**")
        if not fed_f.empty and "DERIVED_STATUS" in fed_f.columns:
            _status = fed_f.groupby("DERIVED_STATUS").size().reset_index(name="N")
            _status = _status.sort_values("N", ascending=False)
            _palette = [
                COLORS.get("primary", "#065E8B"),
                COLORS.get("status_on_track", "#22c55e"),
                COLORS.get("secondary", "#632CFF"),
                COLORS.get("status_at_risk", "#f59e0b"),
                COLORS.get("status_critical", "#ef4444"),
                COLORS.get("tertiary_teal", "#23CED3"),
            ]
            fig_pie = go.Figure(layout=dorel_layout(
                height=320, showlegend=True,
                legend=dict(orientation="h", y=-0.15, xanchor="center", x=0.5),
            ))
            fig_pie.add_trace(go.Pie(
                labels=_status["DERIVED_STATUS"],
                values=_status["N"],
                marker_colors=_palette[:len(_status)],
                textinfo="label+percent",
                hole=0.4,
            ))
            st.plotly_chart(fig_pie, use_container_width=True)
        else:
            st.info("Sin datos de despachos FedEx para el periodo.")

    # ── Bultos detail ──
    if not bul_f.empty:
        st.markdown("**Detalle Bultos**")
        show_cols_b = [c for c in [
            "COD_CORRELATIVO", "COD_PEDIDOTRANSFERENCIA", "COD_PEDIDOVENTA",
            "COD_PICKING", "TIPO", "SKU_PRODUCTO", "CANTIDAD",
            "FECHA_CREACION", "OPERADOR", "OS", "BLK",
        ] if c in bul_f.columns]
        if show_cols_b:
            st.dataframe(
                bul_f[show_cols_b].sort_values("FECHA_CREACION", ascending=False) if "FECHA_CREACION" in bul_f.columns else bul_f[show_cols_b],
                use_container_width=True,
                hide_index=True,
                height=min(400, len(bul_f) * 38 + 40),
                column_config={
                    "COD_CORRELATIVO": st.column_config.TextColumn("Correlativo", width="small"),
                    "COD_PEDIDOTRANSFERENCIA": st.column_config.TextColumn("Pedido", width="small"),
                    "COD_PICKING": st.column_config.TextColumn("Picking", width="small"),
                    "TIPO": st.column_config.TextColumn("Tipo", width="small"),
                    "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
                    "CANTIDAD": st.column_config.NumberColumn("Cantidad", format="%d"),
                    "FECHA_CREACION": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"),
                },
            )

    # ── FedEx detail ──
    if not fed_f.empty:
        st.markdown("**Detalle FedEx**")
        show_cols_f = [c for c in [
            "TRACKING_NUMBER", "DATE_TIME", "DERIVED_STATUS_CODE",
            "DERIVED_STATUS", "CIUDAD", "PESO", "LARGO", "ANCHO", "ALTO",
        ] if c in fed_f.columns]
        if show_cols_f:
            st.dataframe(
                fed_f[show_cols_f].sort_values("DATE_TIME", ascending=False) if "DATE_TIME" in fed_f.columns else fed_f[show_cols_f],
                use_container_width=True,
                hide_index=True,
                height=min(400, len(fed_f) * 38 + 40),
                column_config={
                    "TRACKING_NUMBER": st.column_config.TextColumn("Tracking", width="medium"),
                    "DATE_TIME": st.column_config.DatetimeColumn("Fecha", format="DD/MM/YY HH:mm"),
                    "DERIVED_STATUS_CODE": st.column_config.TextColumn("Cod Estado", width="small"),
                    "DERIVED_STATUS": st.column_config.TextColumn("Estado", width="medium"),
                    "CIUDAD": st.column_config.TextColumn("Ciudad", width="small"),
                    "PESO": st.column_config.NumberColumn("Peso (kg)", format="%.1f"),
                    "LARGO": st.column_config.NumberColumn("Largo", format="%.1f"),
                    "ANCHO": st.column_config.NumberColumn("Ancho", format="%.1f"),
                    "ALTO": st.column_config.NumberColumn("Alto", format="%.1f"),
                },
            )
        download_buttons(fed_f[show_cols_f], prefix="despachos_fedex")
