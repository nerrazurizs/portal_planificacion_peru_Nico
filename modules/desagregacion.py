"""Forecast Diario — Historia real + forecast diario con eventos comerciales."""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format
from utils.export import download_buttons
from utils.ui_components import simple_kpi_card
from config import COLORS, dorel_layout, apply_pm_filter
from modules.proyeccion import EVENTOS_COMERCIALES, _classify_event_date


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MESES_ES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}

CHANNEL_MAP = {
    "Total":      {"col": "UND_TOTAL",    "color": COLORS.get("primary", "#065E8B")},
    "Tienda":     {"col": "UND_TIENDA",   "color": COLORS.get("primary", "#065E8B")},
    "Etail":      {"col": "UND_ETAIL",    "color": COLORS.get("tertiary_teal", "#23CED3")},
    "Mayorista":  {"col": "UND_MAYOR",    "color": COLORS.get("secondary", "#632CFF")},
}

CHANNEL_LABELS = {"TIENDA": "Tienda", "ETAIL": "Etail", "MAYORISTA": "Mayorista"}

EVENT_COLORS = {
    "SaleVerano":     "rgba(0, 180, 216, 0.10)",     # celeste — Sale Verano
    "CyberDays":      "rgba(147, 51, 234, 0.10)",    # violeta — CyberDays CCL
    "CyberWow":       "rgba(255, 165, 0, 0.12)",     # naranja — CyberWow IAB
    "DiaMadre":       "rgba(236, 72, 153, 0.10)",    # rosa — Dia de la Madre
    "DiaPadre":       "rgba(30, 144, 255, 0.10)",    # azul — Dia del Padre
    "FiestasPatrias": "rgba(220, 20, 60, 0.10)",     # rojo — Fiestas Patrias
    "DiaNino":        "rgba(34, 197, 94, 0.10)",     # verde — Dia del Nino
    "BlackFriday":    "rgba(80, 80, 80, 0.10)",      # gris — Black Friday
    "Navidad":        "rgba(220, 20, 60, 0.12)",     # rojo — Navidad
}

EVENT_LINE_COLORS = {
    "SaleVerano":     "#00b4d8",
    "CyberDays":      "#9333ea",
    "CyberWow":       "#e68a00",
    "DiaMadre":       "#ec4899",
    "DiaPadre":       "#1e90ff",
    "FiestasPatrias": "#cc1a36",
    "DiaNino":        "#22c55e",
    "BlackFriday":    "#555555",
    "Navidad":        "#cc1a36",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fmt_cl(val):
    """Format number Chilean style: dots as thousand separator."""
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "0"
    if pd.isna(val) or val == 0:
        return "0"
    return f"{abs(val):,.0f}".replace(",", ".")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def _load_combined_data(conn):
    """Combine df_proy_daily (forecast + MTD) with ventas_90d history.

    Returns a unified DataFrame with:
        FECHA, SKU_PRODUCTO, SKU_NOM_PRODUCTO, AREA, LINEA, SUBLINEA, MARCA,
        TIPO_DATO ("HIST_REAL" | "HISTORICO" | "PROYECCION"),
        UND_TIENDA, UND_ETAIL, UND_MAYOR, UND_TOTAL,
        EVENTO (event label or "Normal"),
        + original simulation columns for fulfillment/lost-sales/stock.
    """
    df_proy = st.session_state.get("df_proy_daily")
    has_proy = df_proy is not None and isinstance(df_proy, pd.DataFrame) and not df_proy.empty

    # ── 1. Process df_proy_daily ──────────────────────────────────────────
    parts = []
    proy_min_date = None

    if has_proy:
        dp = df_proy.copy()
        dp["FECHA"] = pd.to_datetime(dp["FECHA"], errors="coerce")
        dp = dp.dropna(subset=["FECHA"])

        # Unified demand/sales columns
        # HISTORICO rows: real sales are in VENTA_FUL_*_UND
        # PROYECCION rows: forecast demand is in DEMANDA_SIM_*
        for ch, dem_col, ful_col in [
            ("UND_TIENDA",  "DEMANDA_SIM_TIENDA",  "VENTA_FUL_TIENDA_UND"),
            ("UND_ETAIL",   "DEMANDA_SIM_ETAIL",   "VENTA_FUL_ETAIL_UND"),
            ("UND_MAYOR",   "DEMANDA_SIM_MAYOR",   "VENTA_FUL_MAYOR_UND"),
        ]:
            dem = pd.to_numeric(dp.get(dem_col, 0), errors="coerce").fillna(0)
            ful = pd.to_numeric(dp.get(ful_col, 0), errors="coerce").fillna(0)
            dp[ch] = np.where(dp["TIPO_DATO"] == "PROYECCION", dem, ful)

        dp["UND_TOTAL"] = dp["UND_TIENDA"] + dp["UND_ETAIL"] + dp["UND_MAYOR"]

        # Track what dates are covered by df_proy_daily
        hist_mask = dp["TIPO_DATO"].isin(["HISTORICO", "REAL+FC"])
        if hist_mask.any():
            proy_min_date = dp.loc[hist_mask, "FECHA"].min()

        parts.append(dp)

    # ── 2. Extended history from ventas_90d ───────────────────────────────
    try:
        df_v90 = cq.ventas_diarias_90d(conn)
    except Exception:
        df_v90 = None

    if df_v90 is not None and not df_v90.empty:
        v90 = df_v90.copy()
        v90.columns = [c.upper().strip() for c in v90.columns]
        v90["FECHA"] = pd.to_datetime(v90["FECHA"], errors="coerce")
        v90 = v90.dropna(subset=["FECHA"])

        # Only take dates BEFORE what df_proy_daily covers (avoid dups)
        if proy_min_date is not None:
            v90 = v90[v90["FECHA"] < proy_min_date]

        if not v90.empty:
            # Standardize channel names
            if "CANAL_DE_DISTRIBUCION" in v90.columns:
                v90["CANAL_STD"] = v90["CANAL_DE_DISTRIBUCION"].astype(str).str.upper().str.strip()
            else:
                v90["CANAL_STD"] = "TIENDA"

            v90["UNIDADES"] = pd.to_numeric(v90.get("UNIDADES", 0), errors="coerce").fillna(0)

            # Pivot channels to columns
            piv = v90.groupby(["SKU_PRODUCTO", "FECHA", "CANAL_STD"], as_index=False)[
                "UNIDADES"
            ].sum()

            piv_wide = piv.pivot_table(
                index=["SKU_PRODUCTO", "FECHA"],
                columns="CANAL_STD",
                values="UNIDADES",
                aggfunc="sum",
                fill_value=0,
            ).reset_index()

            piv_wide.columns.name = None
            for _src, _dst in [("TIENDA", "UND_TIENDA"), ("ETAIL", "UND_ETAIL"), ("MAYORISTA", "UND_MAYOR")]:
                piv_wide[_dst] = pd.to_numeric(piv_wide[_src], errors="coerce").fillna(0) if _src in piv_wide.columns else 0
            piv_wide["UND_TOTAL"] = piv_wide["UND_TIENDA"] + piv_wide["UND_ETAIL"] + piv_wide["UND_MAYOR"]
            piv_wide["TIPO_DATO"] = "HIST_REAL"

            # Enrich with maestra dimensions
            try:
                maestra = cq.maestra(conn)
                if maestra is not None and not maestra.empty:
                    dim_cols = ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA"]
                    dim_cols = [c for c in dim_cols if c in maestra.columns]
                    piv_wide = piv_wide.merge(
                        maestra[dim_cols].drop_duplicates("SKU_PRODUCTO"),
                        on="SKU_PRODUCTO", how="left",
                    )
            except Exception:
                pass

            keep_cols = [c for c in [
                "SKU_PRODUCTO", "FECHA", "TIPO_DATO",
                "UND_TIENDA", "UND_ETAIL", "UND_MAYOR", "UND_TOTAL",
                "SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
            ] if c in piv_wide.columns]
            parts.append(piv_wide[keep_cols])

    if not parts:
        return None

    # ── 3. Combine ────────────────────────────────────────────────────────
    df = pd.concat(parts, ignore_index=True)

    # Ensure dimension columns exist
    for dim in ["AREA", "LINEA", "SUBLINEA", "MARCA", "SKU_NOM_PRODUCTO"]:
        if dim not in df.columns:
            df[dim] = ""
        df[dim] = df[dim].fillna("")

    # Ensure numeric columns
    for c in ["UND_TIENDA", "UND_ETAIL", "UND_MAYOR", "UND_TOTAL"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # ── 4. Classify events ────────────────────────────────────────────────
    df["EVENTO"] = df["FECHA"].apply(
        lambda f: _classify_event_date(f) if pd.notna(f) else "Normal"
    )

    df = df.sort_values(["SKU_PRODUCTO", "FECHA"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def _apply_filters(df):
    """Render cascading filters with name search. Returns (df_filtered, n_skus)."""

    # Row 1: name search + area + linea
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        name_query = st.text_input(
            "Buscar por nombre de producto",
            value="", key="fc_name_search",
            placeholder="Ej: coche, silla, corral...",
        )
    # Apply name filter first (reduces universe for dimension selects)
    df_search = df
    if name_query.strip():
        mask_name = df["SKU_NOM_PRODUCTO"].str.contains(
            name_query.strip(), case=False, na=False
        )
        df_search = df[mask_name]

    with c2:
        areas = sorted(df_search["AREA"].dropna().unique())
        sel_areas = st.multiselect("Area", areas, default=[], key="fc_area")

    df_for_linea = df_search[df_search["AREA"].isin(sel_areas)] if sel_areas else df_search
    with c3:
        lineas = sorted(df_for_linea["LINEA"].dropna().unique())
        sel_lineas = st.multiselect("Linea", lineas, default=[], key="fc_linea")

    # Row 2: sublinea + marca + SKU
    c4, c5, c6 = st.columns([1, 1, 2])
    df_for_sub = df_for_linea[df_for_linea["LINEA"].isin(sel_lineas)] if sel_lineas else df_for_linea
    with c4:
        sublineas = sorted(df_for_sub["SUBLINEA"].dropna().unique())
        sel_sublineas = st.multiselect("Sublinea", sublineas, default=[], key="fc_sublinea")

    df_for_marca = df_for_sub[df_for_sub["SUBLINEA"].isin(sel_sublineas)] if sel_sublineas else df_for_sub
    with c5:
        marcas = sorted(df_for_marca["MARCA"].dropna().unique())
        sel_marcas = st.multiselect("Marca", marcas, default=[], key="fc_marca")

    df_for_sku = df_for_marca[df_for_marca["MARCA"].isin(sel_marcas)] if sel_marcas else df_for_marca
    with c6:
        # Build SKU options with name
        sku_names = (
            df_for_sku[["SKU_PRODUCTO", "SKU_NOM_PRODUCTO"]]
            .drop_duplicates("SKU_PRODUCTO")
            .sort_values("SKU_PRODUCTO")
        )
        sku_options = [
            f"{row.SKU_PRODUCTO} — {row.SKU_NOM_PRODUCTO}"
            for row in sku_names.itertuples()
        ]
        sel_sku_labels = st.multiselect(
            f"SKU ({len(sku_options)} disponibles)", sku_options,
            default=[], key="fc_sku",
            help="Opcional: selecciona SKUs especificos para drill-down",
        )
        sel_skus = [s.split(" — ")[0] for s in sel_sku_labels]

    # Apply all filters progressively
    mask = pd.Series(True, index=df.index)
    if name_query.strip():
        mask &= df["SKU_NOM_PRODUCTO"].str.contains(
            name_query.strip(), case=False, na=False
        )
    if sel_areas:
        mask &= df["AREA"].isin(sel_areas)
    if sel_lineas:
        mask &= df["LINEA"].isin(sel_lineas)
    if sel_sublineas:
        mask &= df["SUBLINEA"].isin(sel_sublineas)
    if sel_marcas:
        mask &= df["MARCA"].isin(sel_marcas)
    if sel_skus:
        mask &= df["SKU_PRODUCTO"].isin(sel_skus)

    df_f = df[mask].copy()
    return df_f, int(df_f["SKU_PRODUCTO"].nunique())


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
def _render_controls():
    """Render canal selector + event toggle. Returns (canal, show_events)."""
    c1, c2 = st.columns([3, 1])
    with c1:
        canal = st.radio(
            "Canal", ["Total", "Tienda", "Etail", "Mayorista"],
            horizontal=True, key="fc_canal",
        )
    with c2:
        show_events = st.checkbox("Mostrar eventos", value=True, key="fc_events")
    return canal, show_events


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------
def _render_kpis(df):
    """Render KPI cards for forecast + history."""
    df_fc = df[df["TIPO_DATO"] == "PROYECCION"]
    df_hist = df[df["TIPO_DATO"].isin(["HIST_REAL", "HISTORICO"])]

    n_skus = df["SKU_PRODUCTO"].nunique()
    n_days_fc = int(df_fc["FECHA"].nunique()) if not df_fc.empty else 0
    n_days_hist = int(df_hist["FECHA"].nunique()) if not df_hist.empty else 0
    dem_total = df_fc["UND_TOTAL"].sum() if not df_fc.empty else 0
    avg_daily = dem_total / max(n_days_fc, 1)

    # Lost sales (only from PROYECCION rows if available)
    lost_total = 0
    for lc in ["LOST_SALES_TIENDA", "LOST_SALES_ETAIL", "LOST_SALES_MAYOR"]:
        if lc in df_fc.columns:
            lost_total += pd.to_numeric(df_fc[lc], errors="coerce").fillna(0).sum()

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        st.html(simple_kpi_card("SKUs", str(n_skus), COLORS.get("primary", "#065E8B")))
    with c2:
        st.html(simple_kpi_card(
            "Demanda FC Total", f"{_fmt_cl(dem_total)} und",
            COLORS.get("tertiary_teal", "#23CED3"),
        ))
    with c3:
        st.html(simple_kpi_card(
            "Prom. Diario FC", f"{_fmt_cl(avg_daily)} und",
            COLORS.get("tertiary_blue", "#3A86FF"),
        ))
    with c4:
        st.html(simple_kpi_card(
            "Dias Forecast", str(n_days_fc),
            COLORS.get("secondary", "#632CFF"),
        ))
    with c5:
        _lc = COLORS.get("status_critical", "#ef4444") if lost_total > 0 else COLORS.get("status_on_track", "#22c55e")
        st.html(simple_kpi_card("Venta Perdida", f"{_fmt_cl(lost_total)} und", _lc))
    with c6:
        st.html(simple_kpi_card(
            "Dias Historia", str(n_days_hist),
            COLORS.get("neutral_500", "#6b7280"),
        ))


# ---------------------------------------------------------------------------
# Main chart: History vs Forecast overlay
# ---------------------------------------------------------------------------
def _add_event_bands(fig, df, year=None):
    """Add shaded event bands to a Plotly figure."""
    fecha_min = df["FECHA"].min()
    fecha_max = df["FECHA"].max()
    if pd.isna(fecha_min) or pd.isna(fecha_max):
        return

    # Build event date ranges that overlap with visible data
    added = set()
    for label, ey, em, ds, de in EVENTOS_COMERCIALES:
        ev_start = pd.Timestamp(year=ey, month=em, day=ds)
        ev_end = pd.Timestamp(year=ey, month=em, day=de)
        if ev_end < fecha_min or ev_start > fecha_max:
            continue
        key = (label, ey, em)
        if key in added:
            continue
        added.add(key)
        fig.add_vrect(
                x0=max(ev_start, fecha_min), x1=min(ev_end, fecha_max),
                fillcolor=EVENT_COLORS.get(label, "rgba(128,128,128,0.08)"),
                line_width=0.5,
                line_color=EVENT_LINE_COLORS.get(label, "#999"),
                layer="below",
                annotation_text=label,
                annotation_position="top left",
                annotation_font_size=9,
                annotation_font_color=EVENT_LINE_COLORS.get(label, "#666"),
            )


def _render_main_chart(df, canal, show_events):
    """Overlay chart: daily history (solid) + forecast (dashed) by selected channel."""
    st.markdown("### Forecast Diario vs Historia Real")

    col_key = CHANNEL_MAP[canal]["col"]
    base_color = CHANNEL_MAP[canal]["color"]

    # Separate history and forecast
    df_hist = df[df["TIPO_DATO"].isin(["HIST_REAL", "HISTORICO"])]
    df_fc = df[df["TIPO_DATO"] == "PROYECCION"]

    # Aggregate by date
    hist_daily = (
        df_hist.groupby("FECHA", as_index=False)[col_key].sum().sort_values("FECHA")
        if not df_hist.empty else pd.DataFrame(columns=["FECHA", col_key])
    )
    fc_daily = (
        df_fc.groupby("FECHA", as_index=False)[col_key].sum().sort_values("FECHA")
        if not df_fc.empty else pd.DataFrame(columns=["FECHA", col_key])
    )

    if hist_daily.empty and fc_daily.empty:
        st.info("Sin datos para graficar.")
        return

    fig = go.Figure(layout=dorel_layout(
        height=450,
        title=dict(text=f"Forecast Diario — {canal}", font_size=14, x=0.5),
        xaxis=dict(title=""),
        yaxis=dict(title="Unidades"),
        legend=dict(orientation="h", y=-0.12, xanchor="center", x=0.5),
        hovermode="x unified",
    ))

    # History line (solid)
    if not hist_daily.empty:
        fig.add_trace(go.Scatter(
            x=hist_daily["FECHA"], y=hist_daily[col_key],
            name="Historia Real",
            mode="lines",
            line=dict(color=base_color, width=2),
            hovertemplate="%{y:,.0f} und<extra>Historia</extra>",
        ))

    # Forecast line (dashed, slightly different shade)
    if not fc_daily.empty:
        fig.add_trace(go.Scatter(
            x=fc_daily["FECHA"], y=fc_daily[col_key],
            name="Forecast",
            mode="lines",
            line=dict(color=base_color, width=2, dash="dash"),
            fill="tozeroy",
            fillcolor=base_color.replace(")", ", 0.08)").replace("rgb", "rgba") if "rgb" in base_color else f"rgba(6,94,139,0.08)",
            hovertemplate="%{y:,.0f} und<extra>Forecast</extra>",
        ))

    # Transition line between history and forecast
    if not hist_daily.empty and not fc_daily.empty:
        transition_date = hist_daily["FECHA"].max()
        # Use add_shape + add_annotation to avoid Plotly Timestamp arithmetic bug
        fig.add_shape(
            type="line", x0=transition_date, x1=transition_date,
            y0=0, y1=1, yref="paper",
            line=dict(dash="dot", color="#aaa", width=1),
        )
        fig.add_annotation(
            x=transition_date, y=1, yref="paper",
            text="Hoy", showarrow=False,
            font=dict(size=9, color="#888"),
            yshift=8,
        )

    # Event bands
    if show_events:
        _add_event_bands(fig, df)

    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Fulfillment chart
# ---------------------------------------------------------------------------
def _render_fulfillment_chart(df, canal):
    """Fulfillment chart (PROYECCION only), respects canal selector."""
    st.markdown("#### Fulfillment Diario")

    df_fc = df[df["TIPO_DATO"] == "PROYECCION"]
    if df_fc.empty:
        st.info("Sin datos de fulfillment.")
        return

    ful_map = {
        "Total":     ["VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND", "VENTA_FUL_MAYOR_UND"],
        "Tienda":    ["VENTA_FUL_TIENDA_UND"],
        "Etail":     ["VENTA_FUL_ETAIL_UND"],
        "Mayorista": ["VENTA_FUL_MAYOR_UND"],
    }
    ful_colors = {
        "VENTA_FUL_TIENDA_UND": COLORS.get("primary", "#065E8B"),
        "VENTA_FUL_ETAIL_UND":  COLORS.get("tertiary_teal", "#23CED3"),
        "VENTA_FUL_MAYOR_UND":  COLORS.get("secondary", "#632CFF"),
    }
    ful_labels = {
        "VENTA_FUL_TIENDA_UND": "Tienda",
        "VENTA_FUL_ETAIL_UND":  "Etail",
        "VENTA_FUL_MAYOR_UND":  "Mayorista",
    }

    cols = [c for c in ful_map.get(canal, []) if c in df_fc.columns]
    if not cols:
        st.info("Sin columnas de fulfillment.")
        return

    daily = df_fc.groupby("FECHA", as_index=False)[cols].sum().sort_values("FECHA")

    fig = go.Figure(layout=dorel_layout(
        height=320,
        title=dict(text=f"Venta Fulfillment — {canal}", font_size=13, x=0.5),
        xaxis=dict(title=""),
        yaxis=dict(title="Und"),
        legend=dict(orientation="h", y=-0.18, xanchor="center", x=0.5),
        hovermode="x unified",
    ))

    if canal == "Total":
        for col in cols:
            fig.add_trace(go.Scatter(
                x=daily["FECHA"], y=daily[col],
                name=ful_labels.get(col, col),
                stackgroup="ful", line=dict(width=0.5),
                fillcolor=ful_colors.get(col, "#999"),
                marker_color=ful_colors.get(col, "#999"),
                hovertemplate="%{y:,.0f}<extra>" + ful_labels.get(col, col) + "</extra>",
            ))
    else:
        col = cols[0]
        fig.add_trace(go.Scatter(
            x=daily["FECHA"], y=daily[col],
            name=ful_labels.get(col, canal),
            mode="lines",
            line=dict(color=ful_colors.get(col, "#065E8B"), width=2),
            fill="tozeroy",
            fillcolor=ful_colors.get(col, "#065E8B").replace(")", ", 0.10)").replace("rgb", "rgba")
                if "rgb" in ful_colors.get(col, "") else "rgba(6,94,139,0.10)",
            hovertemplate="%{y:,.0f}<extra>" + canal + "</extra>",
        ))

    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Lost sales chart
# ---------------------------------------------------------------------------
def _render_lost_sales_chart(df, canal):
    """Lost sales chart (PROYECCION only), respects canal selector."""
    st.markdown("#### Venta Perdida Diaria")

    df_fc = df[df["TIPO_DATO"] == "PROYECCION"]
    if df_fc.empty:
        st.success("Sin datos de venta perdida.")
        return

    lost_map = {
        "Total":     ["LOST_SALES_TIENDA", "LOST_SALES_ETAIL", "LOST_SALES_MAYOR"],
        "Tienda":    ["LOST_SALES_TIENDA"],
        "Etail":     ["LOST_SALES_ETAIL"],
        "Mayorista": ["LOST_SALES_MAYOR"],
    }
    lost_colors = {
        "LOST_SALES_TIENDA": COLORS.get("status_critical", "#ef4444"),
        "LOST_SALES_ETAIL":  COLORS.get("status_at_risk", "#f59e0b"),
        "LOST_SALES_MAYOR":  COLORS.get("secondary", "#632CFF"),
    }
    lost_labels = {
        "LOST_SALES_TIENDA": "Tienda",
        "LOST_SALES_ETAIL":  "Etail",
        "LOST_SALES_MAYOR":  "Mayorista",
    }

    cols = [c for c in lost_map.get(canal, []) if c in df_fc.columns]
    if not cols:
        st.success("Sin venta perdida.")
        return

    for c in cols:
        df_fc[c] = pd.to_numeric(df_fc[c], errors="coerce").fillna(0)

    total_lost = sum(df_fc[c].sum() for c in cols)
    if total_lost <= 0:
        st.success("Sin venta perdida en el periodo.")
        return

    daily = df_fc.groupby("FECHA", as_index=False)[cols].sum().sort_values("FECHA")

    fig = go.Figure(layout=dorel_layout(
        height=320,
        title=dict(text=f"Venta Perdida — {canal}", font_size=13, x=0.5),
        xaxis=dict(title=""),
        yaxis=dict(title="Und Perdidas"),
        legend=dict(orientation="h", y=-0.18, xanchor="center", x=0.5),
    ))

    for col in cols:
        fig.add_trace(go.Bar(
            x=daily["FECHA"], y=daily[col],
            name=lost_labels.get(col, col),
            marker_color=lost_colors.get(col, "#999"),
            hovertemplate="%{y:,.0f}<extra>" + lost_labels.get(col, col) + "</extra>",
        ))

    fig.update_layout(barmode="stack")
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Event table
# ---------------------------------------------------------------------------
def _render_event_table(df):
    """Compact table summarizing commercial events in the visible period."""
    st.markdown("#### Eventos Comerciales")

    fecha_min = df["FECHA"].min()
    fecha_max = df["FECHA"].max()
    if pd.isna(fecha_min) or pd.isna(fecha_max):
        st.info("Sin datos de fechas para evaluar eventos.")
        return

    # Build event rows (deduplicate multi-month events like CyberDay May+Jun)
    event_groups = {}
    for label, ey, em, ds, de in EVENTOS_COMERCIALES:
        ev_start = pd.Timestamp(year=ey, month=em, day=ds)
        ev_end = pd.Timestamp(year=ey, month=em, day=de)
        key = (label, ey)
        if key not in event_groups:
            event_groups[key] = {"start": ev_start, "end": ev_end}
        else:
            event_groups[key]["start"] = min(event_groups[key]["start"], ev_start)
            event_groups[key]["end"] = max(event_groups[key]["end"], ev_end)

    rows = []
    for (label, y), rng in sorted(event_groups.items(), key=lambda x: x[1]["start"]):
        ev_start = rng["start"]
        ev_end = rng["end"]
        n_days = (ev_end - ev_start).days + 1
        in_period = ev_end >= fecha_min and ev_start <= fecha_max

        # Count event days with non-zero demand in data
        if in_period:
            ev_mask = (df["EVENTO"] == label)
            ev_demand = df.loc[ev_mask, "UND_TOTAL"].sum() if ev_mask.any() else 0
        else:
            ev_demand = 0

        rows.append({
            "Evento": label,
            "Inicio": ev_start.strftime("%d %b %Y"),
            "Fin": ev_end.strftime("%d %b %Y"),
            "Dias": n_days,
            "En Periodo": "Si" if in_period else "No",
            "Demanda (und)": _fmt_cl(ev_demand) if in_period else "—",
        })

    if not rows:
        st.info("No hay eventos comerciales definidos.")
        return

    ev_df = pd.DataFrame(rows)
    st.dataframe(ev_df, use_container_width=True, hide_index=True, height=min(200, 35 + 35 * len(ev_df)))


# ---------------------------------------------------------------------------
# Summary table (monthly pivot)
# ---------------------------------------------------------------------------
def _render_summary_table(df):
    """Pivot table: rows = SKU, columns = months, values = demand total."""
    st.markdown("### Resumen Mensual por SKU")

    _df = df.copy()
    _df["MES_NUM"] = _df["FECHA"].dt.to_period("M")
    _df["MES_LABEL"] = _df["FECHA"].dt.month.map(MESES_ES) + " " + _df["FECHA"].dt.year.astype(str).str[-2:]

    month_order = sorted(_df["MES_NUM"].unique())
    month_label_map = _df.drop_duplicates("MES_NUM").set_index("MES_NUM")["MES_LABEL"].to_dict()
    ordered_labels = [month_label_map[m] for m in month_order]

    _df["DEM_TOTAL"] = _df["UND_TOTAL"]

    agg = _df.groupby(
        ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA", "MES_LABEL"],
        as_index=False,
    )["DEM_TOTAL"].sum()

    pivot = agg.pivot_table(
        index=["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA"],
        columns="MES_LABEL",
        values="DEM_TOTAL",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()

    dim_cols = ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA"]
    month_cols = [m for m in ordered_labels if m in pivot.columns]
    pivot = pivot[dim_cols + month_cols]
    pivot["TOTAL"] = pivot[month_cols].sum(axis=1)
    pivot = pivot.sort_values("TOTAL", ascending=False)

    st.caption("Unidades totales (historia + forecast) por SKU y mes.")
    st.dataframe(pivot, use_container_width=True, height=min(400, 35 + 35 * len(pivot)), hide_index=True)

    # Channel split summary
    st.markdown("#### Apertura por Canal")
    canal_summary = []
    for label, col in [("Tienda", "UND_TIENDA"), ("Etail", "UND_ETAIL"), ("Mayorista", "UND_MAYOR")]:
        if col in _df.columns:
            total = _df[col].sum()
            canal_summary.append({"Canal": label, "Demanda (und)": int(total)})
    if canal_summary:
        cs_df = pd.DataFrame(canal_summary)
        cs_df["% Mix"] = np.where(
            cs_df["Demanda (und)"].sum() > 0,
            cs_df["Demanda (und)"] / cs_df["Demanda (und)"].sum() * 100, 0,
        )
        cs_df["% Mix"] = cs_df["% Mix"].map("{:.1f}%".format)
        cs_df["Demanda (und)"] = cs_df["Demanda (und)"].apply(lambda v: _fmt_cl(v))
        st.dataframe(cs_df, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Detail table
# ---------------------------------------------------------------------------
def _render_detail_table(df):
    """Full daily detail table with export.

    Shows a preview of up to 5,000 rows in the browser to avoid
    MessageSizeError.  The full dataset is available via download buttons.
    """
    st.markdown("### Detalle Diario Completo")

    display_cols = [
        "SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "FECHA", "TIPO_DATO", "EVENTO",
        "AREA", "LINEA", "SUBLINEA", "MARCA",
        "UND_TIENDA", "UND_ETAIL", "UND_MAYOR", "UND_TOTAL",
        "VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND", "VENTA_FUL_MAYOR_UND",
        "STOCK_INICIAL_CD", "STOCK_INICIAL_TIENDA",
        "STOCK_FINAL_CD", "STOCK_FINAL_TIENDA",
        "LOST_SALES_TIENDA", "LOST_SALES_ETAIL", "LOST_SALES_MAYOR",
        "FORECAST_COMPRA", "ETA",
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    df_out = df[display_cols].sort_values(["SKU_PRODUCTO", "FECHA"]).reset_index(drop=True)

    n_rows = len(df_out)
    n_skus = df_out["SKU_PRODUCTO"].nunique()
    MAX_PREVIEW = 5_000

    if n_rows > MAX_PREVIEW:
        st.caption(
            f"{n_rows:,} filas — {n_skus} SKUs  "
            f"(mostrando primeras {MAX_PREVIEW:,} filas; descarga el archivo completo abajo)"
        )
        st.dataframe(
            df_out.head(MAX_PREVIEW), use_container_width=True,
            height=500, hide_index=True,
        )
    else:
        st.caption(f"{n_rows:,} filas — {n_skus} SKUs")
        st.dataframe(df_out, use_container_width=True, height=500, hide_index=True)

    download_buttons(df_out, "forecast_diario")


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------
def render_forecast_diario(conn):
    """Render Forecast Diario: daily history overlay + forecast + commercial events."""
    st.html("<h2 class='sub-header'>Forecast Diario</h2>")
    st.caption(
        "Visualizacion del forecast diario generado por la simulacion, "
        "superpuesto con la historia real de ventas (ultimos 90 dias). "
        "Incluye marcadores de eventos comerciales y filtros por canal."
    )

    # ── Check data availability ───────────────────────────────────────────
    has_daily = (
        st.session_state.get("df_proy_daily") is not None
        and isinstance(st.session_state.get("df_proy_daily"), pd.DataFrame)
        and not st.session_state["df_proy_daily"].empty
    )
    has_monthly = st.session_state.get("df_proy") is not None
    sim_mode = st.session_state.get("sim_mode_used", "")

    if not has_daily:
        if has_monthly and sim_mode == "mensual":
            st.warning(
                "La proyeccion fue generada en modo **mensual**. "
                "Para ver el forecast diario, regenere la proyeccion en modo "
                "**Diaria (recomendada)** desde el modulo 'Proyeccion Stock'."
            )
        elif has_monthly:
            st.warning(
                "No hay datos diarios disponibles. "
                "Verifique que la proyeccion cubra periodos futuros."
            )
        else:
            st.warning(
                "No hay datos de simulacion disponibles. "
                "Genere primero una proyeccion en modo **Diaria (recomendada)** "
                "desde el modulo 'Proyeccion Stock'."
            )
        return

    # ── Load combined data ────────────────────────────────────────────────
    with st.spinner("Cargando historia + forecast..."):
        df = _load_combined_data(conn)

    if df is not None and not df.empty:
        df = apply_pm_filter(df)

    if df is None or df.empty:
        st.warning("No se pudieron cargar datos combinados.")
        return

    # Success info
    n_hist = df[df["TIPO_DATO"].isin(["HIST_REAL", "HISTORICO"])]["FECHA"].nunique()
    n_fc = df[df["TIPO_DATO"] == "PROYECCION"]["FECHA"].nunique()
    st.success(
        f"**{df['SKU_PRODUCTO'].nunique()}** SKUs — "
        f"**{n_hist}** dias historia + **{n_fc}** dias forecast "
        f"({df['FECHA'].min().strftime('%d/%m/%Y')} — {df['FECHA'].max().strftime('%d/%m/%Y')})"
    )

    # ── Filters (visible, not collapsed) ──────────────────────────────────
    df_filtered, n_skus = _apply_filters(df)

    if df_filtered.empty:
        st.info("No hay datos para los filtros seleccionados.")
        return

    total_skus = df["SKU_PRODUCTO"].nunique()
    if n_skus < total_skus:
        st.caption(f"Mostrando **{n_skus}** de **{total_skus}** SKUs")

    # ── Controls ──────────────────────────────────────────────────────────
    canal, show_events = _render_controls()
    st.markdown("")

    # ── KPIs ──────────────────────────────────────────────────────────────
    _render_kpis(df_filtered)
    st.markdown("")

    # ── Main chart (history + forecast + events) ──────────────────────────
    _render_main_chart(df_filtered, canal, show_events)

    # ── Fulfillment + Lost Sales (side by side) ───────────────────────────
    col_a, col_b = st.columns(2)
    with col_a:
        _render_fulfillment_chart(df_filtered, canal)
    with col_b:
        _render_lost_sales_chart(df_filtered, canal)

    st.markdown("---")

    # ── Event table ───────────────────────────────────────────────────────
    if show_events:
        _render_event_table(df_filtered)
        st.markdown("---")

    # ── Summary + Detail tables ───────────────────────────────────────────
    _render_summary_table(df_filtered)
    st.markdown("---")
    _render_detail_table(df_filtered)


# Backward compatibility alias
render_desagregacion = render_forecast_diario
