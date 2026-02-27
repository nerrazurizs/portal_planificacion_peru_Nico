"""Torre de Control S&OP — Dashboard ejecutivo consolidado."""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from datetime import datetime

from config import COLORS, dorel_layout, apply_pm_filter
from utils.filters import norm_cols, human_format
from utils.ui_components import (
    page_header,
    kpi_card,
    simple_kpi_card,
)
from db.cache import cached_query as cq
from utils.ui_animations import lottie_spinner


# ============================================================================
# DATA LOADERS (delegated to centralized cache)
# ============================================================================

def _load_sales_ytd(conn):
    """YTD sales (Jan 1 current year → today)."""
    return cq.ventas_ytd(conn)


def _load_sales_ytd_aa(conn):
    """Same-period prior year sales (for YoY delta)."""
    return cq.ventas_ytd_aa(conn)


def _load_sales_hist(conn):
    """Historical monthly sales (for trend charts)."""
    return cq.ventas_historicas(conn)


def _load_stock(conn):
    return cq.stock_onhand(conn)


def _load_yoy(conn):
    """Full prior-year monthly sales (for trend chart)."""
    return cq.ventas_aa(conn)


def _load_stock_critico(conn):
    """Stock metrics with MOI and aging (for critical stock card)."""
    return cq.stock_critico_metrics(conn)


# ============================================================================
# HELPERS
# ============================================================================

def _get_status(pct: float) -> str:
    """Map percentage to status key."""
    if pct >= 80:
        return "on_track"
    if pct >= 50:
        return "at_risk"
    return "critical"


def _safe_sum(df: pd.DataFrame, col: str) -> float:
    """Safely sum a column, returning 0 if missing or empty."""
    if df.empty or col not in df.columns:
        return 0.0
    return pd.to_numeric(df[col], errors="coerce").fillna(0).sum()


# ============================================================================
# RENDER
# ============================================================================

def render_sop_control_tower(conn):
    # Get user info for header (if available)
    user = st.session_state.get("user") or {}
    sf_connected = st.session_state.get("sf_connected")

    # Page header
    st.html(
        page_header(
            "Torre de Control S&OP",
            "Dashboard ejecutivo consolidado para monitoreo de Ventas, Salud de Inventario y Rentabilidad",
            period_text=datetime.now().strftime("%B %Y").title(),
            sf_connected=sf_connected,
            user_name=user.get("nombre", ""),
            user_cargo=user.get("cargo", ""),
        ),
    )

    try:
        with lottie_spinner("snowflake"):
            df_ventas_ytd = _load_sales_ytd(conn)
            df_ytd_aa = _load_sales_ytd_aa(conn)
            df_stock = _load_stock(conn)
            df_ventas_hist = _load_sales_hist(conn)
            df_ventas_aa = _load_yoy(conn)
            df_critico = _load_stock_critico(conn)

        df_ventas_ytd = apply_pm_filter(df_ventas_ytd)
        df_ytd_aa = apply_pm_filter(df_ytd_aa)
        df_stock = apply_pm_filter(df_stock)
        df_ventas_hist = apply_pm_filter(df_ventas_hist)
        df_ventas_aa = apply_pm_filter(df_ventas_aa)
        df_critico = apply_pm_filter(df_critico)

        # ── YTD Sales totals ──
        total_ventas = _safe_sum(df_ventas_ytd, "NETO_TOTAL")
        total_aporte = _safe_sum(df_ventas_ytd, "APORTE_TOTAL")
        margen = (total_aporte / total_ventas * 100) if total_ventas > 0 else 0

        # ── YoY comparison (same period prior year) ──
        total_ventas_aa = _safe_sum(df_ytd_aa, "NETO_AA")
        delta_ventas = (
            ((total_ventas - total_ventas_aa) / total_ventas_aa * 100)
            if total_ventas_aa > 0
            else 0
        )

        # ── Stock totals ──
        total_stock_cost = _safe_sum(df_stock, "STOCK_COSTO_TOTAL")
        total_stock_und = _safe_sum(df_stock, "STOCK_TOTAL")
        stock_cd = _safe_sum(df_stock, "STOCK_CD")
        stock_tienda = _safe_sum(df_stock, "STOCK_TIENDA")

        # ── Channel breakdown from YTD data (defensive) ──
        canal_col = "CANAL_DE_DISTRIBUCION"
        retail_vn = etail_vn = mayor_vn = 0.0
        if not df_ventas_ytd.empty and canal_col in df_ventas_ytd.columns:
            canal_vn = (
                df_ventas_ytd.groupby(canal_col)["NETO_TOTAL"]
                .sum()
                .to_dict()
            )
            retail_vn = canal_vn.get("TIENDA", 0)
            etail_vn = canal_vn.get("ETAIL", 0)
            mayor_vn = canal_vn.get("MAYORISTA", 0)

        # ── Venta target: AA + 10% growth ──
        venta_target = total_ventas_aa * 1.1 if total_ventas_aa > 0 else total_ventas
        venta_pct = (total_ventas / venta_target * 100) if venta_target > 0 else 0

        # ── Stock Critico: (MOI >= 12 OR sin MOI) AND antiguedad >= 12 meses ──
        stock_critico_cost = 0.0
        stock_critico_und = 0.0
        n_skus_criticos = 0
        if not df_critico.empty:
            df_cr = df_critico.copy()
            # Use only latest date
            if "FECHA" in df_cr.columns:
                df_cr["FECHA"] = pd.to_datetime(df_cr["FECHA"], errors="coerce")
                fecha_max = df_cr["FECHA"].max()
                df_cr = df_cr[df_cr["FECHA"] == fecha_max]

            for c in ["MOI", "ANTIGUEDAD_MESES", "STOCK_COSTO", "STOCK_UNIDADES"]:
                if c in df_cr.columns:
                    df_cr[c] = pd.to_numeric(df_cr[c], errors="coerce").fillna(0)

            if "MOI" in df_cr.columns and "ANTIGUEDAD_MESES" in df_cr.columns:
                # Critico = antiguedad >= 12 AND (MOI >= 12 OR sin MOI)
                mask_critico = (
                    (df_cr["ANTIGUEDAD_MESES"] >= 12)
                    & ((df_cr["MOI"] >= 12) | (df_cr["MOI"] == 0))
                )
                stock_critico_cost = df_cr.loc[mask_critico, "STOCK_COSTO"].sum() if "STOCK_COSTO" in df_cr.columns else 0
                stock_critico_und = df_cr.loc[mask_critico, "STOCK_UNIDADES"].sum() if "STOCK_UNIDADES" in df_cr.columns else 0
                n_skus_criticos = mask_critico.sum()

        pct_critico = (stock_critico_cost / total_stock_cost * 100) if total_stock_cost > 0 else 0

        # ==================================================================
        # SECTION: METRICAS PRINCIPALES
        # ==================================================================
        _cat_color = COLORS["sidebar_category"]
        st.html(
            f"<h3 style='text-transform:uppercase;font-size:0.85rem;letter-spacing:1px;"
            f"color:{_cat_color};margin:1rem 0 0.75rem 0;'>"
            "\U0001f4ca Metricas Principales</h3>"
        )

        # Row 1: Ventas YTD + Margen
        col_left, col_right = st.columns(2)

        with col_left:
            sub_metrics_venta = []
            if total_ventas > 0:
                sub_metrics_venta = [
                    {"label": "Venta Retail", "value": retail_vn, "max": total_ventas, "color": COLORS["primary"]},
                    {"label": "Venta Etail", "value": etail_vn, "max": total_ventas, "color": COLORS["tertiary_blue"]},
                    {"label": "Venta Mayorista", "value": mayor_vn, "max": total_ventas, "color": COLORS["secondary"]},
                ]

            st.html(
                kpi_card(
                    title="Ventas Netas YTD",
                    value_actual=total_ventas,
                    value_target=venta_target,
                    description=f"{delta_ventas:+.1f}% vs Mismo Periodo AA | Meta: +10% AA",
                    badge_text="VENTA NETA",
                    status=_get_status(venta_pct),
                    sub_metrics=sub_metrics_venta or None,
                ),
            )

        with col_right:
            margen_target = 33.0  # target margin %
            margen_pct_of_target = (margen / margen_target * 100) if margen_target > 0 else 0

            st.html(
                kpi_card(
                    title="Margen Bruto YTD",
                    value_actual=total_aporte,
                    value_target=total_ventas,
                    description=f"Margen: {margen:.1f}% | Objetivo: {margen_target:.0f}%",
                    badge_text="RENTABILIDAD",
                    status=_get_status(margen_pct_of_target),
                    format_fn=lambda v: f"${human_format(v)}",
                ),
            )

        # Row 2: Inventario Total + Stock Critico + Distribucion
        col_left2, col_mid2, col_right2 = st.columns(3)

        with col_left2:
            st.html(
                simple_kpi_card(
                    "Inventario Total (Costo)",
                    f"${human_format(total_stock_cost)}",
                    COLORS["primary"],
                ),
            )
            st.html(
                simple_kpi_card(
                    "Unidades en Stock",
                    f"{total_stock_und:,.0f}",
                    COLORS["tertiary_teal"],
                ),
            )

        with col_mid2:
            st.html(
                simple_kpi_card(
                    "Stock Critico (Costo)",
                    f"${human_format(stock_critico_cost)}",
                    COLORS["status_critical"],
                ),
            )
            st.html(
                simple_kpi_card(
                    f"SKUs Criticos ({n_skus_criticos:,})",
                    f"{pct_critico:.1f}% del inventario",
                    COLORS["status_at_risk"],
                ),
            )

        with col_right2:
            if total_stock_und > 0:
                cd_pct = stock_cd / total_stock_und * 100
                ti_pct = stock_tienda / total_stock_und * 100
                st.html(
                    kpi_card(
                        title="Distribucion de Stock",
                        value_actual=stock_cd,
                        value_target=total_stock_und,
                        description=f"CD: {cd_pct:.0f}% | Tiendas: {ti_pct:.0f}%",
                        badge_text="INVENTARIO",
                        status="on_track",
                        sub_metrics=[
                            {"label": "Stock CD", "value": stock_cd, "max": total_stock_und, "color": COLORS["primary"]},
                            {"label": "Stock Tiendas", "value": stock_tienda, "max": total_stock_und, "color": COLORS["tertiary_teal"]},
                        ],
                        format_fn=lambda v: f"{v:,.0f} und",
                    ),
                )
            else:
                st.info("No hay datos de stock disponibles.")

        st.divider()

        # ==================================================================
        # SECTION: CHARTS
        # ==================================================================
        col1, col2 = st.columns(2)

        # ── Chart 1: Donut — Composicion Ventas por Canal (YTD) ──
        with col1:
            st.html(
                f"<h3 style='font-size:1rem;color:{COLORS['dark_gray']};'>"
                "Composicion de Ventas por Canal (YTD)</h3>"
            )
            if not df_ventas_ytd.empty and canal_col in df_ventas_ytd.columns:
                df_canal = (
                    df_ventas_ytd.groupby(canal_col)["NETO_TOTAL"]
                    .sum()
                    .reset_index()
                    .dropna(subset=[canal_col])
                )

                canal_domain = ["TIENDA", "ETAIL", "MAYORISTA"]
                canal_colors = [COLORS["primary"], COLORS["tertiary_blue"], COLORS["secondary"]]
                # Map each label to its color
                label_color_map = dict(zip(canal_domain, canal_colors))
                colors_ordered = [label_color_map.get(c, COLORS["medium_gray"]) for c in df_canal[canal_col]]

                fig_canal = go.Figure(layout=dorel_layout(
                    height=350,
                    margin=dict(l=20, r=20, t=20, b=20),
                    showlegend=True,
                    legend=dict(orientation="h", yanchor="bottom", y=-0.15, xanchor="center", x=0.5),
                ))
                fig_canal.add_trace(go.Pie(
                    labels=df_canal[canal_col],
                    values=df_canal["NETO_TOTAL"],
                    hole=0.5,
                    marker=dict(colors=colors_ordered),
                    textinfo="percent+label",
                    hovertemplate="Canal: %{label}<br>Venta Neta: $%{value:,.0f}<br>%{percent}<extra></extra>",
                ))
                st.plotly_chart(fig_canal, use_container_width=True)
            else:
                st.info("No hay datos de ventas disponibles.")

        # ── Chart 2: Bar — Distribucion Stock CD vs Tiendas ──
        with col2:
            st.html(
                f"<h3 style='font-size:1rem;color:{COLORS['dark_gray']};'>"
                "Distribucion Stock (Costo) CD vs Tiendas</h3>"
            )
            if not df_stock.empty and total_stock_und > 0:
                avg_cost = total_stock_cost / total_stock_und if total_stock_und > 0 else 0

                ubic_labels = ["Centro de Distribucion", "Tiendas"]
                ubic_values = [stock_cd * avg_cost, stock_tienda * avg_cost]
                ubic_colors = [COLORS["primary"], COLORS["tertiary_teal"]]

                fig_stock = go.Figure(layout=dorel_layout(
                    height=350,
                    xaxis=dict(title=""),
                    yaxis=dict(title="Valor Costo ($)"),
                    showlegend=False,
                ))
                fig_stock.add_trace(go.Bar(
                    x=ubic_labels,
                    y=ubic_values,
                    marker_color=ubic_colors,
                    hovertemplate="Ubicacion: %{x}<br>Valor: $%{y:,.0f}<extra></extra>",
                ))
                st.plotly_chart(fig_stock, use_container_width=True)
            else:
                st.info("No hay datos de stock disponibles.")

        st.divider()

        # ==================================================================
        # SECTION: TREND — Evolucion Ventas vs AA
        # ==================================================================
        st.html(
            f"<h3 style='font-size:1rem;color:{COLORS['dark_gray']};'>"
            "Evolucion de Ventas vs Ano Anterior</h3>"
        )
        if not df_ventas_hist.empty and not df_ventas_aa.empty:
            dv = df_ventas_hist.copy()
            dv["FECHA"] = pd.to_datetime(dv["FECHA"], errors="coerce")
            dv["MES"] = dv["FECHA"].dt.to_period("M").astype(str)
            df_ventas_mes = dv.groupby("MES")["NETO_TOTAL"].sum().reset_index()

            daa = df_ventas_aa.copy()
            if "PERIODO" in daa.columns:
                daa["PERIODO"] = pd.to_datetime(daa["PERIODO"], errors="coerce")
                daa["MES_COMPARE"] = (
                    (daa["PERIODO"] + pd.DateOffset(years=1))
                    .dt.to_period("M")
                    .astype(str)
                )
                df_aa_mes = (
                    daa.groupby("MES_COMPARE")["NETO_AA"]
                    .sum()
                    .reset_index()
                    .rename(columns={"MES_COMPARE": "MES", "NETO_AA": "NETO_TOTAL"})
                )

                # Align months
                all_meses = sorted(set(df_ventas_mes["MES"].tolist() + df_aa_mes["MES"].tolist()))

                fig_trend = go.Figure(layout=dorel_layout(
                    height=400,
                    xaxis=dict(title="Mes", categoryorder="array", categoryarray=all_meses),
                    yaxis=dict(title="Venta Neta ($)"),
                    legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="center", x=0.5),
                    hovermode="x unified",
                ))
                fig_trend.add_trace(go.Scatter(
                    x=df_ventas_mes["MES"], y=df_ventas_mes["NETO_TOTAL"],
                    mode="lines+markers", name="Actual",
                    line=dict(color=COLORS["primary"], width=2.5),
                    marker=dict(size=7),
                    hovertemplate="$%{y:,.0f}<extra>Actual</extra>",
                ))
                fig_trend.add_trace(go.Scatter(
                    x=df_aa_mes["MES"], y=df_aa_mes["NETO_TOTAL"],
                    mode="lines+markers", name="Ano Anterior",
                    line=dict(color=COLORS["medium_gray"], width=2, dash="dot"),
                    marker=dict(size=6),
                    hovertemplate="$%{y:,.0f}<extra>Ano Anterior</extra>",
                ))
                st.plotly_chart(fig_trend, use_container_width=True)
            else:
                st.info("Datos insuficientes para la tendencia mensual.")
        else:
            st.info("Datos insuficientes para la tendencia mensual.")

    except Exception as e:
        st.error(f"Error cargando Torre de Control: {e}")
        import traceback
        st.code(traceback.format_exc())
