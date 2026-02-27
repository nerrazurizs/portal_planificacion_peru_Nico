"""Analisis de Transitos — modulo Streamlit para monitoreo de POs en transito.

Tabs:
  1. Overview          KPIs consolidados + chart proveedor + llegadas por semana
  2. POs Atrasadas     Detalle de POs vencidas (ETA + 10 dias buffer)
  3. POs Proximas      POs con ETA dentro de 45 dias, con score de prioridad
  4. Sin Diario Factura POs embarcadas sin factura registrada
  5. Sin Carpeta Comex  POs sin carpeta comex creada
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, dorel_layout, apply_pm_filter
from db.cache import cached_query as cq
from db.queries import QUERY_COMEX_ATRASADAS, QUERY_COMEX_SIN_CARPETA
from utils.export import download_buttons
from utils.filters import human_format, norm_cols
from utils.ui_animations import lottie_spinner

# ============================================================================
# CONSTANTS
# ============================================================================

_NUM_COLS_TRANSIT = [
    "QTY_PENDIENTE", "MONTO_PENDIENTE_CLP", "MONTOMN",
    "CANTIDAD_FINAL_CORREGIDA", "DIAS_ATRASO", "DIAS_HASTA_ETA",
    "DIAS_DESDE_ENTREGA",
]

_NUM_COLS_PRECIOS = ["PRECIO_PROM_90D", "UNIDADES_90D", "NETO_90D"]

_NUM_COLS_MOI = [
    "STOCK_COSTO", "STOCK_UNIDADES", "MOI", "ANTIGUEDAD_MESES",
    "COSTO_PROM_90_CIA",
]

# Columns for each display table
_COLS_ATRASADAS = [
    "PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "NOM_PROVEEDOR",
    "CARPETA_COMEX", "ETA_CALC", "DIAS_ATRASO", "QTY_PENDIENTE",
    "MONTOMN", "VN_POTENCIAL", "AREA", "LINEA",
]

_COLS_PROXIMAS = [
    "PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "NOM_PROVEEDOR",
    "CARPETA_COMEX", "ETA_CALC", "DIAS_HASTA_ETA", "QTY_PENDIENTE",
    "MONTO_PENDIENTE_CLP", "VN_POTENCIAL", "MOI_ACTUAL",
    "PRIORIDAD_SCORE",
]

_COLS_SIN_FACTURA = [
    "PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "NOM_PROVEEDOR",
    "CARPETA_COMEX", "ETA_CALC", "QTY_PENDIENTE",
    "MONTOMN", "AREA", "LINEA",
]

_COLS_SIN_CARPETA = [
    "PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "NOM_PROVEEDOR",
    "FECHA_ENTREGA", "DIAS_DESDE_ENTREGA", "CANTIDAD_FINAL_CORREGIDA",
    "MONTOMN", "AREA", "LINEA",
]


# ============================================================================
# HELPERS
# ============================================================================

def _safe_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Coerce columns to numeric safely."""
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def _kpi(
    label: str,
    value: str,
    color: str,
    subtitle: str = "",
    icon: str = "",
) -> None:
    """Render a compact KPI card with optional subtitle line."""
    _sub_html = (
        f'<div style="font-size:0.8rem;color:#64748b;margin-top:2px;'
        f'font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
        f"{subtitle}</div>"
        if subtitle
        else ""
    )
    _icon_html = (
        f'<span style="font-size:1.1rem;margin-right:4px;">{icon}</span>'
        if icon
        else ""
    )
    st.html(
        f"""<div style="background:#fff;padding:0.7rem 0.9rem;border-radius:10px;
        box-shadow:0 1px 4px rgba(0,0,0,0.07);border-left:4px solid {color};
        min-height:80px;display:flex;flex-direction:column;justify-content:center;">
        <div style="font-size:0.7rem;color:#94a3b8;text-transform:uppercase;
        letter-spacing:0.5px;font-weight:600;margin-bottom:4px;">
        {_icon_html}{label}</div>
        <div style="font-size:1.15rem;font-weight:700;color:{color};
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{value}</div>
        {_sub_html}
    </div>"""
    )


def _fmt_clp(val: float) -> str:
    """Format Chilean Pesos with dot thousand separator."""
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "$0"
    if pd.isna(val) or val == 0:
        return "$0"
    sign = "-" if val < 0 else ""
    formatted = f"{abs(val):,.0f}".replace(",", ".")
    return f"{sign}${formatted}"


def _select_cols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Return only existing columns from the requested list, in order."""
    present = [c for c in cols if c in df.columns]
    return df[present].copy() if present else df.copy()


def _filter_eta_year(
    df: pd.DataFrame,
    year: int | None,
    eta_col: str = "ETA_CALC",
) -> pd.DataFrame:
    """Keep only rows where the ETA column year == year. None = no filter."""
    if df.empty or year is None:
        return df
    col = eta_col if eta_col in df.columns else None
    if col is None:
        return df
    parsed = pd.to_datetime(df[col], errors="coerce")
    return df[parsed.dt.year == year].reset_index(drop=True)


def _apply_global_filters(
    df: pd.DataFrame,
    proveedores: list[str],
    areas: list[str],
    lineas: list[str],
) -> pd.DataFrame:
    """Apply cascading multiselect filters to a DataFrame."""
    if df.empty:
        return df
    out = df.copy()
    if proveedores and "NOM_PROVEEDOR" in out.columns:
        out = out[out["NOM_PROVEEDOR"].isin(proveedores)]
    if areas and "AREA" in out.columns:
        out = out[out["AREA"].isin(areas)]
    if lineas and "LINEA" in out.columns:
        out = out[out["LINEA"].isin(lineas)]
    return out


# ============================================================================
# ENRICHMENT FUNCTIONS
# ============================================================================

def _enrich_vn_potencial(df: pd.DataFrame, df_precios: pd.DataFrame) -> pd.DataFrame:
    """Add VN_POTENCIAL = QTY_PENDIENTE * PRECIO_PROM_90D.

    Merges average 90-day price per SKU and computes the potential net sales
    value for each pending PO line.
    """
    if df.empty:
        return df
    out = df.copy()

    if not df_precios.empty and "SKU_PRODUCTO" in df_precios.columns:
        price_map = df_precios.set_index("SKU_PRODUCTO")["PRECIO_PROM_90D"]
        if "SKU_PRODUCTO" in out.columns:
            out["PRECIO_PROM_90D"] = out["SKU_PRODUCTO"].map(price_map).fillna(0)
        else:
            out["PRECIO_PROM_90D"] = 0
    else:
        out["PRECIO_PROM_90D"] = 0

    qty_col = "QTY_PENDIENTE" if "QTY_PENDIENTE" in out.columns else "CANTIDAD_FINAL_CORREGIDA"
    if qty_col in out.columns:
        out["VN_POTENCIAL"] = (
            pd.to_numeric(out[qty_col], errors="coerce").fillna(0)
            * pd.to_numeric(out["PRECIO_PROM_90D"], errors="coerce").fillna(0)
        )
    else:
        out["VN_POTENCIAL"] = 0

    return out


def _enrich_priority(df: pd.DataFrame, df_moi: pd.DataFrame) -> pd.DataFrame:
    """Add MOI_ACTUAL and PRIORIDAD_SCORE = VN_POTENCIAL / (1 + MOI_ACTUAL).

    Lower MOI means the SKU needs stock more urgently, so dividing by MOI
    gives higher priority to items close to stock-out.
    """
    if df.empty:
        return df
    out = df.copy()

    if not df_moi.empty and "SKU_PRODUCTO" in df_moi.columns and "MOI" in df_moi.columns:
        # Aggregate MOI to SKU level (take max across locations)
        moi_sku = df_moi.groupby("SKU_PRODUCTO", as_index=False)["MOI"].max()
        moi_map = moi_sku.set_index("SKU_PRODUCTO")["MOI"]
        if "SKU_PRODUCTO" in out.columns:
            out["MOI_ACTUAL"] = out["SKU_PRODUCTO"].map(moi_map).fillna(0)
        else:
            out["MOI_ACTUAL"] = 0
    else:
        out["MOI_ACTUAL"] = 0

    out["MOI_ACTUAL"] = pd.to_numeric(out["MOI_ACTUAL"], errors="coerce").fillna(0)

    if "VN_POTENCIAL" in out.columns:
        vn = pd.to_numeric(out["VN_POTENCIAL"], errors="coerce").fillna(0)
        out["PRIORIDAD_SCORE"] = np.where(
            (1 + out["MOI_ACTUAL"]) > 0,
            vn / (1 + out["MOI_ACTUAL"]),
            0,
        )
    else:
        out["PRIORIDAD_SCORE"] = 0

    return out


# ============================================================================
# DATA LOADING (parallel)
# ============================================================================

def _load_all_data(conn) -> dict:
    """Load all datasets in parallel using ThreadPoolExecutor.

    Returns a dict with keys:
        transit_kpi, sin_factura, proximas, precios, moi, atrasadas, sin_carpeta
    """
    results: dict = {}

    def _load_transit_kpi():
        return cq.transit_stock_kpi(conn)

    def _load_sin_factura():
        return cq.comex_sin_factura(conn)

    def _load_proximas():
        return cq.comex_proximas(conn)

    def _load_precios():
        return cq.precio_prom_sku(conn)

    def _load_moi():
        return cq.stock_critico_metrics(conn)

    def _load_atrasadas():
        return norm_cols(pd.read_sql(QUERY_COMEX_ATRASADAS, conn))

    def _load_sin_carpeta():
        return norm_cols(pd.read_sql(QUERY_COMEX_SIN_CARPETA, conn))

    tasks = {
        "transit_kpi": _load_transit_kpi,
        "sin_factura": _load_sin_factura,
        "proximas": _load_proximas,
        "precios": _load_precios,
        "moi": _load_moi,
        "atrasadas": _load_atrasadas,
        "sin_carpeta": _load_sin_carpeta,
    }

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                st.warning(f"Error cargando {key}: {exc}")
                results[key] = pd.DataFrame()

    return results


# ============================================================================
# CHART BUILDERS
# ============================================================================

def _chart_monto_por_proveedor(df: pd.DataFrame) -> None:
    """Horizontal bar chart: monto pendiente CLP by supplier (top 15)."""
    if df.empty or "NOM_PROVEEDOR" not in df.columns:
        st.info("Sin datos de proveedor para graficar.")
        return

    monto_col = "MONTO_PENDIENTE_CLP" if "MONTO_PENDIENTE_CLP" in df.columns else "MONTOMN"
    if monto_col not in df.columns:
        st.info("Sin datos de monto para graficar.")
        return

    grouped = (
        df.groupby("NOM_PROVEEDOR", as_index=False)[monto_col]
        .sum()
        .sort_values(monto_col, ascending=True)
        .tail(15)
    )

    fig = go.Figure(
        go.Bar(
            x=grouped[monto_col],
            y=grouped["NOM_PROVEEDOR"],
            orientation="h",
            marker_color=COLORS["secondary"],
            text=[_fmt_clp(v) for v in grouped[monto_col]],
            textposition="auto",
            hovertemplate="<b>%{y}</b><br>Monto: %{text}<extra></extra>",
        )
    )
    fig.update_layout(
        **dorel_layout(
            title="Monto Pendiente CLP por Proveedor (Top 15)",
            height=max(350, len(grouped) * 30 + 100),
            xaxis=dict(title="Monto CLP"),
            yaxis=dict(title="", tickfont=dict(size=10)),
        )
    )
    st.plotly_chart(fig, use_container_width=True)


def _chart_llegadas_por_semana(df: pd.DataFrame) -> None:
    """Bar chart: arriving POs grouped by week of ETA_CALC (next 6 weeks)."""
    if df.empty or "ETA_CALC" not in df.columns:
        st.info("Sin datos de ETA para graficar llegadas.")
        return

    work = df.copy()
    work["ETA_CALC"] = pd.to_datetime(work["ETA_CALC"], errors="coerce")
    work = work.dropna(subset=["ETA_CALC"])

    today = pd.Timestamp(date.today())
    cutoff = today + timedelta(weeks=6)
    work = work[(work["ETA_CALC"] >= today) & (work["ETA_CALC"] <= cutoff)]

    if work.empty:
        st.info("Sin llegadas programadas en las proximas 6 semanas.")
        return

    # Group by ISO week
    work["SEMANA"] = work["ETA_CALC"].dt.isocalendar().week.astype(int)
    work["ANO_SEMANA"] = (
        work["ETA_CALC"].dt.isocalendar().year.astype(str)
        + "-W"
        + work["SEMANA"].astype(str).str.zfill(2)
    )

    qty_col = "QTY_PENDIENTE" if "QTY_PENDIENTE" in work.columns else "CANTIDAD_FINAL_CORREGIDA"
    monto_col = "MONTO_PENDIENTE_CLP" if "MONTO_PENDIENTE_CLP" in work.columns else "MONTOMN"

    agg_dict: dict = {"PO": "nunique"}
    if qty_col in work.columns:
        agg_dict[qty_col] = "sum"
    if monto_col in work.columns:
        agg_dict[monto_col] = "sum"

    weekly = (
        work.groupby("ANO_SEMANA", as_index=False)
        .agg(agg_dict)
        .rename(columns={"PO": "N_POS"})
        .sort_values("ANO_SEMANA")
    )

    fig = go.Figure()
    if monto_col in weekly.columns:
        fig.add_trace(
            go.Bar(
                x=weekly["ANO_SEMANA"],
                y=weekly[monto_col],
                name="Monto CLP",
                marker_color=COLORS["primary"],
                text=[_fmt_clp(v) for v in weekly[monto_col]],
                textposition="outside",
                hovertemplate="Semana %{x}<br>Monto: %{text}<extra></extra>",
            )
        )
    if qty_col in weekly.columns:
        fig.add_trace(
            go.Bar(
                x=weekly["ANO_SEMANA"],
                y=weekly[qty_col],
                name="Unidades",
                marker_color=COLORS["tertiary_teal"],
                visible="legendonly",
                hovertemplate="Semana %{x}<br>Und: %{y:,.0f}<extra></extra>",
            )
        )

    fig.update_layout(
        **dorel_layout(
            title="Llegadas Programadas por Semana (Proximas 6 semanas)",
            height=400,
            xaxis=dict(title="Semana ISO"),
            yaxis=dict(title="Monto CLP"),
            barmode="group",
        )
    )
    st.plotly_chart(fig, use_container_width=True)


# ============================================================================
# TAB RENDERERS
# ============================================================================

def _render_overview(
    df_transit: pd.DataFrame,
    df_atrasadas: pd.DataFrame,
    df_proximas: pd.DataFrame,
    df_sin_factura: pd.DataFrame,
    df_sin_carpeta: pd.DataFrame,
) -> None:
    """Tab 1: Overview KPIs + charts."""

    # ── Extract KPI values from transit summary ──────────────────────────
    en_agua_clp = en_agua_und = 0.0
    no_zarpado_clp = no_zarpado_und = 0.0

    if not df_transit.empty and "STATUS_TRANSITO" in df_transit.columns:
        for _, row in df_transit.iterrows():
            status = str(row.get("STATUS_TRANSITO", "")).upper()
            qty = float(row.get("QTY_PENDIENTE", 0))
            monto = float(row.get("MONTO_PENDIENTE_CLP", 0))
            if status == "EN_AGUA":
                en_agua_clp += monto
                en_agua_und += qty
            elif status == "PENDIENTE_ZARPE":
                no_zarpado_clp += monto
                no_zarpado_und += qty

    # Atrasadas KPIs
    atrasadas_count = len(df_atrasadas) if not df_atrasadas.empty else 0
    atrasadas_clp = (
        float(df_atrasadas["MONTOMN"].sum())
        if not df_atrasadas.empty and "MONTOMN" in df_atrasadas.columns
        else 0
    )

    # Sin Carpeta / Sin Factura counts
    sin_carpeta_count = len(df_sin_carpeta) if not df_sin_carpeta.empty else 0
    sin_factura_count = len(df_sin_factura) if not df_sin_factura.empty else 0

    # Proximas KPIs
    proximas_count = len(df_proximas) if not df_proximas.empty else 0
    proximas_clp = 0.0
    if not df_proximas.empty:
        monto_col = (
            "MONTO_PENDIENTE_CLP"
            if "MONTO_PENDIENTE_CLP" in df_proximas.columns
            else "MONTOMN"
        )
        if monto_col in df_proximas.columns:
            proximas_clp = float(
                pd.to_numeric(df_proximas[monto_col], errors="coerce").fillna(0).sum()
            )

    # ── KPI cards (2 rows × 3 cols) ──────────────────────────────────────
    r1c1, r1c2, r1c3 = st.columns(3)
    with r1c1:
        _kpi(
            "En Agua",
            _fmt_clp(en_agua_clp),
            COLORS["status_en_curso"],
            subtitle=f"{human_format(en_agua_und)} unidades",
            icon="\U0001F6A2",
        )
    with r1c2:
        _kpi(
            "No Zarpado",
            _fmt_clp(no_zarpado_clp),
            COLORS["primary"],
            subtitle=f"{human_format(no_zarpado_und)} unidades",
            icon="\U0001F4E6",
        )
    with r1c3:
        _kpi(
            "Proximas 45 dias",
            f"{proximas_count} POs",
            COLORS["status_on_track"],
            subtitle=_fmt_clp(proximas_clp),
            icon="\U0001F4C5",
        )

    r2c1, r2c2, r2c3 = st.columns(3)
    with r2c1:
        _kpi(
            "POs Atrasadas",
            f"{atrasadas_count} POs",
            COLORS["status_critical"],
            subtitle=_fmt_clp(atrasadas_clp),
            icon="\u26A0\uFE0F",
        )
    with r2c2:
        _kpi(
            "Sin Carpeta Comex",
            f"{sin_carpeta_count} lineas",
            COLORS["status_at_risk"],
            icon="\U0001F4C1",
        )
    with r2c3:
        _kpi(
            "Sin Diario Factura",
            f"{sin_factura_count} lineas",
            COLORS["status_at_risk"],
            icon="\U0001F4C4",
        )

    st.markdown("---")

    # ── Charts ───────────────────────────────────────────────────────────
    col_left, col_right = st.columns(2)
    with col_left:
        _chart_monto_por_proveedor(df_proximas)
    with col_right:
        _chart_llegadas_por_semana(df_proximas)


def _render_atrasadas(df: pd.DataFrame) -> None:
    """Tab 2: POs Atrasadas detail table."""
    st.info(
        "ETA calculada + 10 dias de buffer (programacion CD). "
        "Ordenado por Venta Neta Potencial descendente."
    )

    if df.empty:
        st.success("No hay POs atrasadas actualmente.")
        return

    display = _select_cols(df, _COLS_ATRASADAS)
    display = display.sort_values("VN_POTENCIAL", ascending=False) if "VN_POTENCIAL" in display.columns else display

    col_config = {
        "PO": st.column_config.TextColumn("PO", width="small"),
        "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
        "NOM_PRODUCTO": st.column_config.TextColumn("Producto", width="medium"),
        "NOM_PROVEEDOR": st.column_config.TextColumn("Proveedor", width="medium"),
        "CARPETA_COMEX": st.column_config.TextColumn("Carpeta", width="small"),
        "ETA_CALC": st.column_config.DateColumn("ETA Calc", format="DD/MM/YYYY"),
        "DIAS_ATRASO": st.column_config.NumberColumn("Dias Atraso", format="%d"),
        "QTY_PENDIENTE": st.column_config.NumberColumn("Und Pend.", format="%,.0f"),
        "MONTOMN": st.column_config.NumberColumn("Monto CLP", format="$%,.0f"),
        "VN_POTENCIAL": st.column_config.NumberColumn("VN Potencial", format="$%,.0f"),
        "AREA": st.column_config.TextColumn("Area", width="small"),
        "LINEA": st.column_config.TextColumn("Linea", width="small"),
    }

    st.dataframe(
        display,
        column_config=col_config,
        use_container_width=True,
        hide_index=True,
        height=min(len(display) * 35 + 60, 600),
    )

    st.caption(f"{len(display)} lineas | Monto total: {_fmt_clp(display['MONTOMN'].sum() if 'MONTOMN' in display.columns else 0)}")
    download_buttons(display, prefix="pos_atrasadas")


def _render_proximas(df: pd.DataFrame) -> None:
    """Tab 3: POs Proximas a Llegar with priority score."""
    st.info(
        "**Score de Prioridad** = VN Potencial / (1 + MOI Actual). "
        "SKUs con bajo MOI (cercanos a quiebre) y alto valor de venta "
        "obtienen mayor prioridad. Ordenado por Score descendente."
    )

    if df.empty:
        st.success("No hay POs proximas en los siguientes 45 dias.")
        return

    display = _select_cols(df, _COLS_PROXIMAS)
    display = display.sort_values("PRIORIDAD_SCORE", ascending=False) if "PRIORIDAD_SCORE" in display.columns else display

    col_config = {
        "PO": st.column_config.TextColumn("PO", width="small"),
        "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
        "NOM_PRODUCTO": st.column_config.TextColumn("Producto", width="medium"),
        "NOM_PROVEEDOR": st.column_config.TextColumn("Proveedor", width="medium"),
        "CARPETA_COMEX": st.column_config.TextColumn("Carpeta", width="small"),
        "ETA_CALC": st.column_config.DateColumn("ETA Calc", format="DD/MM/YYYY"),
        "DIAS_HASTA_ETA": st.column_config.NumberColumn("Dias p/ ETA", format="%d"),
        "QTY_PENDIENTE": st.column_config.NumberColumn("Und Pend.", format="%,.0f"),
        "MONTO_PENDIENTE_CLP": st.column_config.NumberColumn("Monto Pend. CLP", format="$%,.0f"),
        "VN_POTENCIAL": st.column_config.NumberColumn("VN Potencial", format="$%,.0f"),
        "MOI_ACTUAL": st.column_config.NumberColumn("MOI Actual", format="%.1f"),
        "PRIORIDAD_SCORE": st.column_config.NumberColumn("Prioridad", format="%,.0f"),
    }

    st.dataframe(
        display,
        column_config=col_config,
        use_container_width=True,
        hide_index=True,
        height=min(len(display) * 35 + 60, 600),
    )

    monto_col = "MONTO_PENDIENTE_CLP" if "MONTO_PENDIENTE_CLP" in display.columns else "MONTOMN"
    total_monto = float(display[monto_col].sum()) if monto_col in display.columns else 0
    st.caption(f"{len(display)} lineas | Monto pendiente total: {_fmt_clp(total_monto)}")
    download_buttons(display, prefix="pos_proximas")


def _render_sin_factura(df: pd.DataFrame) -> None:
    """Tab 4: POs sin diario de factura."""
    if df.empty:
        st.success("Todas las POs en transito tienen diario de factura.")
        return

    # Summary KPIs
    total_lines = len(df)
    total_monto = float(df["MONTOMN"].sum()) if "MONTOMN" in df.columns else 0

    k1, k2, k3 = st.columns([1, 1, 2])
    with k1:
        _kpi("Lineas sin Factura", f"{total_lines:,}", COLORS["status_at_risk"])
    with k2:
        _kpi("Monto Total", _fmt_clp(total_monto), COLORS["status_at_risk"])

    st.markdown("")  # spacer

    display = _select_cols(df, _COLS_SIN_FACTURA)

    col_config = {
        "PO": st.column_config.TextColumn("PO", width="small"),
        "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
        "NOM_PRODUCTO": st.column_config.TextColumn("Producto", width="medium"),
        "NOM_PROVEEDOR": st.column_config.TextColumn("Proveedor", width="medium"),
        "CARPETA_COMEX": st.column_config.TextColumn("Carpeta", width="small"),
        "ETA_CALC": st.column_config.DateColumn("ETA Calc", format="DD/MM/YYYY"),
        "QTY_PENDIENTE": st.column_config.NumberColumn("Und Pend.", format="%,.0f"),
        "MONTOMN": st.column_config.NumberColumn("Monto CLP", format="$%,.0f"),
        "AREA": st.column_config.TextColumn("Area", width="small"),
        "LINEA": st.column_config.TextColumn("Linea", width="small"),
    }

    st.dataframe(
        display,
        column_config=col_config,
        use_container_width=True,
        hide_index=True,
        height=min(len(display) * 35 + 60, 600),
    )

    download_buttons(display, prefix="sin_factura")


def _render_sin_carpeta(df: pd.DataFrame) -> None:
    """Tab 5: POs sin carpeta comex."""
    if df.empty:
        st.success("Todas las POs tienen carpeta comex asignada.")
        return

    total_lines = len(df)
    total_monto = float(df["MONTOMN"].sum()) if "MONTOMN" in df.columns else 0

    k1, k2, k3 = st.columns([1, 1, 2])
    with k1:
        _kpi("Lineas sin Carpeta", f"{total_lines:,}", COLORS["status_critical"])
    with k2:
        _kpi("Monto Total", _fmt_clp(total_monto), COLORS["status_critical"])

    st.markdown("")  # spacer

    display = _select_cols(df, _COLS_SIN_CARPETA)

    col_config = {
        "PO": st.column_config.TextColumn("PO", width="small"),
        "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
        "NOM_PRODUCTO": st.column_config.TextColumn("Producto", width="medium"),
        "NOM_PROVEEDOR": st.column_config.TextColumn("Proveedor", width="medium"),
        "FECHA_ENTREGA": st.column_config.DateColumn("F. Entrega", format="DD/MM/YYYY"),
        "DIAS_DESDE_ENTREGA": st.column_config.NumberColumn("Dias desde Entrega", format="%d"),
        "CANTIDAD_FINAL_CORREGIDA": st.column_config.NumberColumn("Und Total", format="%,.0f"),
        "MONTOMN": st.column_config.NumberColumn("Monto CLP", format="$%,.0f"),
        "AREA": st.column_config.TextColumn("Area", width="small"),
        "LINEA": st.column_config.TextColumn("Linea", width="small"),
    }

    st.dataframe(
        display,
        column_config=col_config,
        use_container_width=True,
        hide_index=True,
        height=min(len(display) * 35 + 60, 600),
    )

    download_buttons(display, prefix="sin_carpeta")


# ============================================================================
# FILTER OPTIONS EXTRACTION
# ============================================================================

def _collect_filter_options(*dataframes: pd.DataFrame) -> dict:
    """Collect unique filter values from multiple DataFrames.

    Returns dict with keys: proveedores, areas, lineas.
    """
    all_prov: set = set()
    all_area: set = set()
    all_linea: set = set()

    for df in dataframes:
        if df is None or df.empty:
            continue
        if "NOM_PROVEEDOR" in df.columns:
            all_prov.update(
                df["NOM_PROVEEDOR"].dropna().astype(str).str.strip().unique()
            )
        if "AREA" in df.columns:
            all_area.update(
                df["AREA"].dropna().astype(str).str.strip().unique()
            )
        if "LINEA" in df.columns:
            all_linea.update(
                df["LINEA"].dropna().astype(str).str.strip().unique()
            )

    return {
        "proveedores": sorted(all_prov - {"", "NAN", "NONE"}),
        "areas": sorted(all_area - {"", "NAN", "NONE"}),
        "lineas": sorted(all_linea - {"", "NAN", "NONE"}),
    }


def _get_cascading_lineas(
    areas_selected: list[str],
    all_lineas: list[str],
    *dataframes: pd.DataFrame,
) -> list[str]:
    """Return lineas filtered by selected areas (cascading filter)."""
    if not areas_selected:
        return all_lineas

    lineas_subset: set = set()
    for df in dataframes:
        if df is None or df.empty:
            continue
        if "AREA" in df.columns and "LINEA" in df.columns:
            mask = df["AREA"].isin(areas_selected)
            lineas_subset.update(
                df.loc[mask, "LINEA"].dropna().astype(str).str.strip().unique()
            )

    return sorted(lineas_subset - {"", "NAN", "NONE"})


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def render_analisis_transitos(conn):
    """Main entry point for transit analysis module."""

    st.html("<h2 class='sub-header'>Analisis de Transitos</h2>")

    # ── Load all data ────────────────────────────────────────────────────
    try:
        with lottie_spinner("snowflake"):
            data = _load_all_data(conn)
    except Exception as e:
        st.error(f"Error al cargar datos: {e}")
        import traceback
        st.code(traceback.format_exc())
        return

    df_transit    = data.get("transit_kpi", pd.DataFrame())
    df_sin_factura = data.get("sin_factura", pd.DataFrame())
    df_proximas   = data.get("proximas", pd.DataFrame())
    df_precios    = data.get("precios", pd.DataFrame())
    df_moi        = data.get("moi", pd.DataFrame())
    df_atrasadas  = data.get("atrasadas", pd.DataFrame())
    df_sin_carpeta = data.get("sin_carpeta", pd.DataFrame())

    df_transit     = apply_pm_filter(df_transit)
    df_sin_factura = apply_pm_filter(df_sin_factura)
    df_proximas    = apply_pm_filter(df_proximas)
    df_atrasadas   = apply_pm_filter(df_atrasadas)
    df_sin_carpeta = apply_pm_filter(df_sin_carpeta)

    # ── Coerce numeric columns ───────────────────────────────────────────
    df_transit     = _safe_numeric(df_transit, ["QTY_PENDIENTE", "MONTO_PENDIENTE_CLP"])
    df_atrasadas   = _safe_numeric(df_atrasadas, _NUM_COLS_TRANSIT)
    df_proximas    = _safe_numeric(df_proximas, _NUM_COLS_TRANSIT)
    df_sin_factura = _safe_numeric(df_sin_factura, _NUM_COLS_TRANSIT)
    df_sin_carpeta = _safe_numeric(df_sin_carpeta, _NUM_COLS_TRANSIT)
    df_precios     = _safe_numeric(df_precios, _NUM_COLS_PRECIOS)
    df_moi         = _safe_numeric(df_moi, _NUM_COLS_MOI)

    # ── Enrich with VN_POTENCIAL and Priority ────────────────────────────
    df_atrasadas = _enrich_vn_potencial(df_atrasadas, df_precios)
    df_proximas  = _enrich_vn_potencial(df_proximas, df_precios)
    df_proximas  = _enrich_priority(df_proximas, df_moi)

    # ── Global Filters ───────────────────────────────────────────────────
    filter_opts = _collect_filter_options(
        df_atrasadas, df_proximas, df_sin_factura, df_sin_carpeta,
    )

    with st.expander("Filtros", expanded=True):
        fy_col, fc1, fc2, fc3 = st.columns([1, 2, 2, 2])
        with fy_col:
            sel_year = st.selectbox(
                "Año ETA",
                options=[2025, 2026, 2027],
                index=1,   # default 2026
                key="transito_year_eta",
            )
        with fc1:
            sel_prov = st.multiselect(
                "Proveedor",
                options=filter_opts["proveedores"],
                default=[],
                key="transito_prov",
            )
        with fc2:
            sel_area = st.multiselect(
                "Area",
                options=filter_opts["areas"],
                default=[],
                key="transito_area",
            )
        with fc3:
            cascading_lineas = _get_cascading_lineas(
                sel_area,
                filter_opts["lineas"],
                df_atrasadas, df_proximas, df_sin_factura, df_sin_carpeta,
            )
            sel_linea = st.multiselect(
                "Linea",
                options=cascading_lineas,
                default=[],
                key="transito_linea",
            )

    # ── Apply ETA year filter ─────────────────────────────────────────────
    df_atrasadas   = _filter_eta_year(df_atrasadas, sel_year, "ETA_CALC")
    df_proximas    = _filter_eta_year(df_proximas, sel_year, "ETA_CALC")
    df_sin_factura = _filter_eta_year(df_sin_factura, sel_year, "ETA_CALC")
    df_sin_carpeta = _filter_eta_year(df_sin_carpeta, sel_year, "FECHA_ENTREGA")

    # ── Apply global (proveedor / area / linea) filters ───────────────────
    df_atrasadas_f   = _apply_global_filters(df_atrasadas, sel_prov, sel_area, sel_linea)
    df_proximas_f    = _apply_global_filters(df_proximas, sel_prov, sel_area, sel_linea)
    df_sin_factura_f = _apply_global_filters(df_sin_factura, sel_prov, sel_area, sel_linea)
    df_sin_carpeta_f = _apply_global_filters(df_sin_carpeta, sel_prov, sel_area, sel_linea)

    # ── Tabs ─────────────────────────────────────────────────────────────
    tab_overview, tab_atrasadas, tab_proximas, tab_sin_fac, tab_sin_carp = st.tabs([
        "Overview",
        f"POs Atrasadas ({len(df_atrasadas_f)})",
        f"Proximas a Llegar ({len(df_proximas_f)})",
        f"Sin Diario Factura ({len(df_sin_factura_f)})",
        f"Sin Carpeta Comex ({len(df_sin_carpeta_f)})",
    ])

    with tab_overview:
        _render_overview(
            df_transit,
            df_atrasadas_f,
            df_proximas_f,
            df_sin_factura_f,
            df_sin_carpeta_f,
        )

    with tab_atrasadas:
        _render_atrasadas(df_atrasadas_f)

    with tab_proximas:
        _render_proximas(df_proximas_f)

    with tab_sin_fac:
        _render_sin_factura(df_sin_factura_f)

    with tab_sin_carp:
        _render_sin_carpeta(df_sin_carpeta_f)
