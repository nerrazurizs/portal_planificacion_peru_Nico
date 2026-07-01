"""Cumplimiento Comex — dias de demora de embarque (ETD) e ingreso a almacen.

Toma todas las compras de comex en transito (Transito/Recibido) y las que ya
ingresaron (Cerrado) y compara las fechas comprometidas en la PO contra la
fecha real (una vez ocurrida) o la estimacion vigente (mientras sigue en
proceso):

  - Embarque:        PO_FECHA_DELIVERY  vs  ETD vigente
                      (real una vez zarpado, estimado mientras no zarpa)
  - Ingreso Almacen:  PO_FECHA_INGRESO_ALMACEN_ESTIMADO  vs  ingreso a CD vigente
                      (real una vez recepcionado, estimado mientras no llega)

Fuente: db_supply.fct.ft_cubo_comex (fechas comprometidas/estimadas de la PO)
        + db_supply.fct.ft_compras (fechas reales de embarque/recepcion).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, dorel_layout, apply_pm_filter
from db.cache import cached_query as cq
from utils.export import download_buttons
from utils.filters import human_format
from utils.ui_animations import lottie_spinner
from utils.ui_components import page_header

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DATE_COLS = [
    "PO_FECHA_DELIVERY", "PO_FECHA_EMBARQUE", "FECHA_ETD_REAL", "FECHA_ETD_ESTIMADO_VIGENTE",
    "FECHA_ETA_PUERTO", "PO_FECHA_INGRESO_ALMACEN_ESTIMADO",
    "FECHA_INGRESO_ALMACEN_ESTIMADO_VIGENTE",
    "FECHA_REAL_INGRESO_ALMACEN",
]

_NUM_COLS = ["CANTIDAD_PEDIDA", "CANTIDAD_ENTREGADA", "VALOR_TOTAL_PO", "VALORIZADO_MN"]

_COLS_EMBARQUE = [
    "PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "PROVEEDOR", "AREA", "LINEA", "SUBLINEA",
    "ESTADO_IMPORTACION", "PO_FECHA_DELIVERY", "ETD_VIGENTE", "ETD_CONFIRMADO",
    "DIAS_DEMORA_EMBARQUE", "CUMPLIMIENTO_EMBARQUE",
]

_COLS_ALMACEN = [
    "PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "PROVEEDOR", "AREA", "LINEA", "SUBLINEA",
    "ESTADO_IMPORTACION", "PO_FECHA_INGRESO_ALMACEN_ESTIMADO", "ALMACEN_VIGENTE",
    "ALMACEN_CONFIRMADO", "DIAS_DEMORA_ALMACEN", "CUMPLIMIENTO_ALMACEN",
]

_COL_CONFIG = {
    "PO": st.column_config.TextColumn("PO", width="small"),
    "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
    "NOM_PRODUCTO": st.column_config.TextColumn("Producto", width="medium"),
    "PROVEEDOR": st.column_config.TextColumn("Proveedor", width="medium"),
    "AREA": st.column_config.TextColumn("Area", width="small"),
    "LINEA": st.column_config.TextColumn("Linea", width="small"),
    "SUBLINEA": st.column_config.TextColumn("Sublinea", width="small"),
    "ESTADO_IMPORTACION": st.column_config.TextColumn("Estado", width="small"),
    "PO_FECHA_DELIVERY": st.column_config.DateColumn("PO Fecha Delivery", format="DD/MM/YYYY"),
    "ETD_VIGENTE": st.column_config.DateColumn("ETD Vigente", format="DD/MM/YYYY"),
    "ETD_CONFIRMADO": st.column_config.CheckboxColumn("ETD Real"),
    "DIAS_DEMORA_EMBARQUE": st.column_config.NumberColumn("Dias Demora", format="%d"),
    "CUMPLIMIENTO_EMBARQUE": st.column_config.TextColumn("Cumplimiento", width="small"),
    "PO_FECHA_INGRESO_ALMACEN_ESTIMADO": st.column_config.DateColumn("PO Fecha Ing. Almacen Est.", format="DD/MM/YYYY"),
    "ALMACEN_VIGENTE": st.column_config.DateColumn("Ingreso Almacen Vigente", format="DD/MM/YYYY"),
    "ALMACEN_CONFIRMADO": st.column_config.CheckboxColumn("Ingreso Real"),
    "DIAS_DEMORA_ALMACEN": st.column_config.NumberColumn("Dias Demora", format="%d"),
    "CUMPLIMIENTO_ALMACEN": st.column_config.TextColumn("Cumplimiento", width="small"),
}


# ---------------------------------------------------------------------------
# Data loading + enrichment
# ---------------------------------------------------------------------------

def _load(conn) -> pd.DataFrame:
    with lottie_spinner("comex"):
        df = cq.cumplimiento_comex(conn)
    return df


def _cumplimiento_label(dias) -> str:
    if pd.isna(dias):
        return "Sin dato"
    if dias <= 0:
        return "A tiempo"
    if dias <= 7:
        return "Atraso leve"
    return "Atraso critico"


def _cumplimiento_color(label: str) -> str:
    return {
        "A tiempo": COLORS["status_on_track"],
        "Atraso leve": COLORS["status_at_risk"],
        "Atraso critico": COLORS["status_critical"],
        "Sin dato": COLORS["medium_gray"],
    }.get(label, COLORS["medium_gray"])


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Compute ETD/ALMACEN vigente dates, confirmed flags, and delay days."""
    if df.empty:
        return df
    df = df.copy()

    for c in _DATE_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    for c in _NUM_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    df["PROVEEDOR"] = df["NOM_PROVEEDOR_MAESTRA"].fillna(df["NOM_PROVEEDOR_COMEX"])
    df.loc[df["PROVEEDOR"].astype(str).str.strip() == "", "PROVEEDOR"] = df["NOM_PROVEEDOR_COMEX"]

    # --- Embarque: confirmado cuando comex ya registro el ETD real
    # (di_etd / di_fechaembarque). Mientras no se registre, se usa el
    # estimado vigente (revisado por comex), no la fecha real de zarpe.
    df["ETD_CONFIRMADO"] = df["FECHA_ETD_REAL"].notna()
    df["ETD_VIGENTE"] = df["FECHA_ETD_REAL"].where(df["ETD_CONFIRMADO"], df["FECHA_ETD_ESTIMADO_VIGENTE"])
    df["DIAS_DEMORA_EMBARQUE"] = (df["ETD_VIGENTE"] - df["PO_FECHA_DELIVERY"]).dt.days

    # --- Ingreso a almacen: confirmado solo si ya existe la fecha real de
    # recepcion en CD (independiente del estado del expediente comex).
    df["ALMACEN_CONFIRMADO"] = df["FECHA_REAL_INGRESO_ALMACEN"].notna()
    df["ALMACEN_VIGENTE"] = df["FECHA_REAL_INGRESO_ALMACEN"].where(
        df["ALMACEN_CONFIRMADO"], df["FECHA_INGRESO_ALMACEN_ESTIMADO_VIGENTE"]
    )
    df["DIAS_DEMORA_ALMACEN"] = (df["ALMACEN_VIGENTE"] - df["PO_FECHA_INGRESO_ALMACEN_ESTIMADO"]).dt.days

    df["CUMPLIMIENTO_EMBARQUE"] = df["DIAS_DEMORA_EMBARQUE"].apply(_cumplimiento_label)
    df["CUMPLIMIENTO_ALMACEN"] = df["DIAS_DEMORA_ALMACEN"].apply(_cumplimiento_label)

    # Status simplificado para filtros/segmentacion
    df["STATUS_COMPRA"] = np.where(df["ALMACEN_CONFIRMADO"], "Ya Ingreso", "En Transito")

    return df


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _kpi(label: str, value: str, color: str, subtitle: str = "", icon: str = "") -> None:
    sub_html = (
        f'<div style="font-size:0.8rem;color:#64748b;margin-top:2px;'
        f'font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
        f"{subtitle}</div>" if subtitle else ""
    )
    icon_html = f'<span style="font-size:1.1rem;margin-right:4px;">{icon}</span>' if icon else ""
    st.html(
        f"""<div style="background:#fff;padding:0.7rem 0.9rem;border-radius:10px;
        box-shadow:0 1px 4px rgba(0,0,0,0.07);border-left:4px solid {color};
        min-height:80px;display:flex;flex-direction:column;justify-content:center;">
        <div style="font-size:0.7rem;color:#94a3b8;text-transform:uppercase;
        letter-spacing:0.5px;font-weight:600;margin-bottom:4px;">
        {icon_html}{label}</div>
        <div style="font-size:1.15rem;font-weight:700;color:{color};
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{value}</div>
        {sub_html}
    </div>"""
    )


def _select_cols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    present = [c for c in cols if c in df.columns]
    return df[present].copy() if present else df.copy()


def _apply_filters(
    df: pd.DataFrame,
    sel_status: list[str],
    sel_estado: list[str],
    sel_proveedor: list[str],
    sel_area: list[str],
    sel_linea: list[str],
    sel_sublinea: list[str],
    sel_marca: list[str],
) -> pd.DataFrame:
    out = df
    if sel_status:
        out = out[out["STATUS_COMPRA"].isin(sel_status)]
    if sel_estado:
        out = out[out["ESTADO_IMPORTACION"].isin(sel_estado)]
    if sel_proveedor:
        out = out[out["PROVEEDOR"].isin(sel_proveedor)]
    if sel_area:
        out = out[out["AREA"].isin(sel_area)]
    if sel_linea:
        out = out[out["LINEA"].isin(sel_linea)]
    if sel_sublinea:
        out = out[out["SUBLINEA"].isin(sel_sublinea)]
    if sel_marca:
        out = out[out["MARCA"].isin(sel_marca)]
    return out


def _chart_demora_por_proveedor(df: pd.DataFrame, dias_col: str, title: str) -> None:
    # Excluye outliers extremos (> 1 ano) del promedio: son errores de
    # digitacion de fecha en el origen (ej. ano mal tipeado), no demoras
    # reales, y un solo valor asi distorsiona el promedio de un proveedor
    # entero. Las tablas de detalle si los muestran sin filtrar.
    work = df.dropna(subset=[dias_col])
    work = work[work[dias_col].abs() <= 365]
    if work.empty or "PROVEEDOR" not in work.columns:
        st.info("Sin datos suficientes para graficar.")
        return

    grouped = (
        work.groupby("PROVEEDOR", as_index=False)[dias_col]
        .mean()
        .sort_values(dias_col, ascending=True)
        .tail(15)
    )
    colors = [
        COLORS["status_critical"] if v > 7 else COLORS["status_at_risk"] if v > 0 else COLORS["status_on_track"]
        for v in grouped[dias_col]
    ]

    fig = go.Figure(
        go.Bar(
            x=grouped[dias_col],
            y=grouped["PROVEEDOR"],
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.0f}d" for v in grouped[dias_col]],
            textposition="auto",
            hovertemplate="<b>%{y}</b><br>Demora promedio: %{x:.1f} dias<extra></extra>",
        )
    )
    fig.update_layout(
        **dorel_layout(
            title=title,
            height=max(350, len(grouped) * 30 + 100),
            xaxis=dict(title="Dias de demora promedio"),
            yaxis=dict(title="", tickfont=dict(size=10)),
        )
    )
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Tab renderers
# ---------------------------------------------------------------------------

def _render_resumen(df: pd.DataFrame) -> None:
    n_total = len(df)
    n_transito = int((df["STATUS_COMPRA"] == "En Transito").sum())
    n_ingresado = int((df["STATUS_COMPRA"] == "Ya Ingreso").sum())

    emb_confirmado = df[df["ETD_CONFIRMADO"]]
    alm_confirmado = df[df["ALMACEN_CONFIRMADO"]]

    pct_cumple_emb = (
        (emb_confirmado["DIAS_DEMORA_EMBARQUE"] <= 0).mean() * 100 if not emb_confirmado.empty else 0
    )
    pct_cumple_alm = (
        (alm_confirmado["DIAS_DEMORA_ALMACEN"] <= 0).mean() * 100 if not alm_confirmado.empty else 0
    )
    prom_demora_emb = emb_confirmado["DIAS_DEMORA_EMBARQUE"].mean() if not emb_confirmado.empty else np.nan
    prom_demora_alm = alm_confirmado["DIAS_DEMORA_ALMACEN"].mean() if not alm_confirmado.empty else np.nan

    # Alerta: PO con fecha de delivery ya vencida y aun sin ETD confirmado
    hoy = pd.Timestamp.now().normalize()
    en_riesgo = df[
        (~df["ETD_CONFIRMADO"]) & df["PO_FECHA_DELIVERY"].notna() & (df["PO_FECHA_DELIVERY"] < hoy)
    ]

    r1c1, r1c2, r1c3 = st.columns(3)
    with r1c1:
        _kpi("Lineas Totales", f"{n_total:,}", COLORS["primary"], icon="\U0001F4E6")
    with r1c2:
        _kpi("En Transito", f"{n_transito:,}", COLORS["tertiary_blue"], icon="\U0001F6A2")
    with r1c3:
        _kpi("Ya Ingresadas", f"{n_ingresado:,}", COLORS["status_on_track"], icon="✅")

    r2c1, r2c2, r2c3 = st.columns(3)
    with r2c1:
        _kpi(
            "Cumplimiento Embarque", f"{pct_cumple_emb:.0f}%",
            COLORS["status_on_track"] if pct_cumple_emb >= 70 else COLORS["status_at_risk"],
            subtitle=f"Demora prom.: {prom_demora_emb:+.1f}d" if pd.notna(prom_demora_emb) else "Sin ETD confirmado",
            icon="\U0001F4C5",
        )
    with r2c2:
        _kpi(
            "Cumplimiento Almacen", f"{pct_cumple_alm:.0f}%",
            COLORS["status_on_track"] if pct_cumple_alm >= 70 else COLORS["status_at_risk"],
            subtitle=f"Demora prom.: {prom_demora_alm:+.1f}d" if pd.notna(prom_demora_alm) else "Sin ingreso confirmado",
            icon="\U0001F3E2",
        )
    with r2c3:
        _kpi(
            "POs Vencidas sin Embarcar", f"{len(en_riesgo):,}",
            COLORS["status_critical"] if len(en_riesgo) > 0 else COLORS["status_on_track"],
            subtitle="PO Fecha Delivery ya paso, sin ETD confirmado",
            icon="⚠️",
        )

    st.caption(
        "**Confirmado** = ya ocurrio (fecha real de embarque/ingreso a almacen). "
        "Mientras una compra sigue en transito se usa la estimacion vigente mas reciente "
        "(revisada por comex), no la fecha original de la PO."
    )

    st.markdown("---")
    col_left, col_right = st.columns(2)
    with col_left:
        _chart_demora_por_proveedor(df, "DIAS_DEMORA_EMBARQUE", "Demora Promedio de Embarque por Proveedor (Top 15)")
    with col_right:
        _chart_demora_por_proveedor(df, "DIAS_DEMORA_ALMACEN", "Demora Promedio de Ingreso a Almacen por Proveedor (Top 15)")


def _render_tabla_embarque(df: pd.DataFrame) -> None:
    st.info(
        "**Dias Demora** = ETD Vigente − PO Fecha Delivery. Valores positivos indican atraso "
        "respecto al compromiso de embarque acordado en la PO. **ETD Real** marcado = fecha confirmada; "
        "si no, es la estimacion vigente mientras la compra sigue en proceso."
    )
    if df.empty:
        st.success("No hay compras para mostrar con los filtros actuales.")
        return

    display = _select_cols(df, _COLS_EMBARQUE).sort_values("DIAS_DEMORA_EMBARQUE", ascending=False, na_position="last")
    st.dataframe(
        display, column_config=_COL_CONFIG, use_container_width=True, hide_index=True,
        height=min(len(display) * 35 + 60, 600),
    )
    n_atraso = int((display["DIAS_DEMORA_EMBARQUE"] > 0).sum())
    st.caption(f"{len(display)} lineas | {n_atraso} con atraso de embarque")
    download_buttons(display, prefix="cumplimiento_embarque")


def _render_tabla_almacen(df: pd.DataFrame) -> None:
    st.info(
        "**Dias Demora** = Ingreso Almacen Vigente − PO Fecha Ingreso Almacen Estimado. Valores "
        "positivos indican atraso respecto al estimado original de la PO. **Ingreso Real** marcado = "
        "ya recepcionado en CD; si no, es la estimacion vigente mientras sigue en transito."
    )
    if df.empty:
        st.success("No hay compras para mostrar con los filtros actuales.")
        return

    display = _select_cols(df, _COLS_ALMACEN).sort_values("DIAS_DEMORA_ALMACEN", ascending=False, na_position="last")
    st.dataframe(
        display, column_config=_COL_CONFIG, use_container_width=True, hide_index=True,
        height=min(len(display) * 35 + 60, 600),
    )
    n_atraso = int((display["DIAS_DEMORA_ALMACEN"] > 0).sum())
    st.caption(f"{len(display)} lineas | {n_atraso} con atraso de ingreso a almacen")
    download_buttons(display, prefix="cumplimiento_almacen")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def render_cumplimiento_comex(conn) -> None:
    """Entry point — called from app.py."""
    st.html(page_header(
        "Cumplimiento Comex",
        "Dias de demora de embarque (ETD) e ingreso a almacen vs fechas comprometidas en la PO",
    ))

    if conn is None:
        st.warning("No hay conexion a Snowflake.")
        return

    try:
        df_raw = _load(conn)
    except Exception as e:
        st.error(f"Error al cargar datos: {e}")
        import traceback
        st.code(traceback.format_exc())
        return

    df_raw = apply_pm_filter(df_raw)

    if df_raw.empty:
        st.info("No se encontraron compras de comex en transito o ya ingresadas.")
        return

    df = _enrich(df_raw)

    # ── Filtros ──────────────────────────────────────────────────────────
    with st.expander("Filtros", expanded=True):
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            sel_status = st.multiselect(
                "Status", ["En Transito", "Ya Ingreso"], default=[], key="cumplcomex_status",
            )
        with fc2:
            sel_estado = st.multiselect(
                "Estado Importacion",
                sorted(df["ESTADO_IMPORTACION"].dropna().unique().tolist()),
                default=[], key="cumplcomex_estado",
            )
        with fc3:
            sel_proveedor = st.multiselect(
                "Proveedor", sorted(df["PROVEEDOR"].dropna().unique().tolist()),
                default=[], key="cumplcomex_proveedor",
            )

        fc4, fc5, fc6 = st.columns(3)
        with fc4:
            sel_area = st.multiselect(
                "Area", sorted(df["AREA"].dropna().unique().tolist()), default=[], key="cumplcomex_area",
            )
        with fc5:
            sel_linea = st.multiselect(
                "Linea", sorted(df["LINEA"].dropna().unique().tolist()), default=[], key="cumplcomex_linea",
            )
        with fc6:
            sel_marca = st.multiselect(
                "Marca", sorted(df["MARCA"].dropna().unique().tolist()), default=[], key="cumplcomex_marca",
            )
        sel_sublinea = st.multiselect(
            "Sublinea", sorted(df["SUBLINEA"].dropna().unique().tolist()), default=[], key="cumplcomex_sublinea",
        )

    df_f = _apply_filters(df, sel_status, sel_estado, sel_proveedor, sel_area, sel_linea, sel_sublinea, sel_marca)

    # ── Tabs ─────────────────────────────────────────────────────────────
    tab_resumen, tab_embarque, tab_almacen = st.tabs([
        "Resumen",
        f"Cumplimiento Embarque ({len(df_f)})",
        f"Cumplimiento Ingreso Almacen ({len(df_f)})",
    ])

    with tab_resumen:
        _render_resumen(df_f)
    with tab_embarque:
        _render_tabla_embarque(df_f)
    with tab_almacen:
        _render_tabla_almacen(df_f)
