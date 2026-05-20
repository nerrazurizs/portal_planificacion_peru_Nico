"""Analisis de Agotamiento — modulo Streamlit.

Evalua la velocidad de agotamiento (sell-through) de cada SKU desde su
ultimo ingreso al CD, comparando las ventas contra la cantidad recibida
y el Lead Time del proveedor.

Logica de Evaluacion (referencia: 75% del Lead Time):
    >= 60% vendido  → Excelente
    40% – 60%       → Bueno
    30% – 40%       → Regular
    < 30%           → Malo
    dias < 75% LT   → En Proceso
    sin ingreso     → Sin Ingreso
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import streamlit as st

from config import COLORS, apply_pm_filter
from db.cache import cached_query as cq
from utils.export import download_buttons
from utils.filters import human_format
from utils.ui_animations import lottie_spinner


# ============================================================================
# CONSTANTS
# ============================================================================

_DEFAULT_AREAS = {"BEBE", "JUGUETERIA", "VESTUARIO", "TIEMPO LIBRE"}
_DEFAULT_MIX   = {"MIX"}

_EVAL_ORDER = ["Excelente", "Bueno", "Regular", "Malo", "En Proceso", "Sin Ingreso", "Sin datos"]

_EVAL_COLORS = {
    "Excelente":   COLORS.get("status_on_track",  "#43A047"),
    "Bueno":       COLORS.get("status_en_curso",   "#1976D2"),
    "Regular":     COLORS.get("status_at_risk",    "#FB8C00"),
    "Malo":        COLORS.get("status_critical",   "#E53935"),
    "En Proceso":  COLORS.get("secondary",         "#78909C"),
    "Sin Ingreso": "#90A4AE",
    "Sin datos":   "#B0BEC5",
}

# Emojis semaforo para la columna Evaluacion en tabla
_EVAL_EMOJI = {
    "Excelente":   "🟢 Excelente",
    "Bueno":       "🔵 Bueno",
    "Regular":     "🟠 Regular",
    "Malo":        "🔴 Malo",
    "En Proceso":  "⏳ En Proceso",
    "Sin Ingreso": "⬜ Sin Ingreso",
    "Sin datos":   "⬜ Sin datos",
}

# Columnas de visualizacion (tabla compacta — Costo va solo en descarga)
_DISPLAY_COLS = [
    "SKU_PRODUCTO", "DESCRIPCION", "AREA", "LINEA", "MARCA",
    "FECHA_ULT_INGRESO_CD", "DIAS",
    "CANT_RECIBIDA_ULT_ING", "STOCK_ACTUAL_TOTAL",
    "VENTA_UNIDADES", "PCT_AVANCE", "TASA_DIARIA",
    "LEAD_TIME", "OBJ_HOY", "OBJ_HOY_UNDS",
    "PVP", "MARGEN_MAESTRA",
    "PV_PROM_MTD", "MRG_MTD", "DESC_PV",
    "INDICE_VEL",
    "EVALUACION",          # siempre al final
]

# Columnas completas para el Excel de descarga (incluye todo)
_DOWNLOAD_COLS = [
    "SKU_PRODUCTO", "DESCRIPCION", "AREA", "LINEA", "FAMILIA", "MARCA",
    "PROCEDENCIA", "MIX_OFICIAL",
    "FECHA_ULT_INGRESO_CD", "DIAS",
    "CANT_RECIBIDA_ULT_ING", "STOCK_ACTUAL_TOTAL",
    "VENTA_UNIDADES", "PCT_AVANCE", "TASA_DIARIA",
    "LEAD_TIME", "OBJ_HOY", "OBJ_HOY_UNDS",
    "PVP", "ULTIMO_COSTO", "MARGEN_MAESTRA",
    "PV_PROM_MTD", "MRG_MTD", "DESC_PV",
    "INDICE_VEL",
    "EVALUACION",
]


# ============================================================================
# HELPERS
# ============================================================================

def _detect_lt_col(df_lt: pd.DataFrame) -> str | None:
    """Detecta la columna de Lead Time en el DataFrame de Syncro."""
    candidates = [
        "LEAD_TIME_OC", "LEAD_TIME", "LEADTIME", "LT",
        "DIAS_LEAD_TIME", "DIAS_ENTREGA", "TIEMPO_ENTREGA", "LT_DIAS",
    ]
    cols_upper = {c.upper(): c for c in df_lt.columns}
    for cand in candidates:
        if cand in cols_upper:
            return cols_upper[cand]
    for col in df_lt.columns:
        if "lead" in col.lower() or col.lower().startswith("lt_"):
            return col
    return None


def _evaluar_agotamiento(
    dias: float | None,
    lt: float | None,
    vta_unds: float | None,
    recib_unds: float | None,
) -> str:
    """Evaluacion por indice de velocidad de venta.

    Compara la tasa diaria real vs la tasa diaria planificada al hacer el pedido:
      tasa_real  = Vta / DIAS          (unidades/dia desde el ingreso)
      tasa_obj   = Recib / LT          (unidades/dia que justificaron la compra)
      indice     = tasa_real / tasa_obj = (Vta * LT) / (DIAS * Recib)

      indice >= 1.2 → Excelente  (vendiendo 20%+ mas rapido que lo planeado)
      indice >= 0.8 → Bueno
      indice >= 0.6 → Regular
      indice <  0.6 → Malo
    """
    if pd.isna(dias):
        return "Sin datos"
    min_dias = max(7, round(lt * 0.05)) if (pd.notna(lt) and lt > 0) else 7
    if dias < min_dias:
        return "En Proceso"
    if pd.isna(lt) or lt <= 0 or pd.isna(recib_unds) or recib_unds <= 0:
        return "Sin datos"
    vta_unds = 0.0 if pd.isna(vta_unds) else vta_unds
    # tasa_obj = (Recib * 0.7) / LT  — se espera vender el 70% del lote en el LT
    # indice = tasa_real / tasa_obj — independiente del stock previo
    indice = (vta_unds * lt) / (dias * recib_unds * 0.7)
    if indice >= 1.2: return "Excelente"
    if indice >= 0.8: return "Bueno"
    if indice >= 0.6: return "Regular"
    return "Malo"


def _kpi_card(label: str, value: str, color: str, subtitle: str = "") -> None:
    sub_html = (
        f'<div style="font-size:0.72rem;color:#64748b;margin-top:2px;font-weight:500;">'
        f"{subtitle}</div>"
        if subtitle else ""
    )
    st.html(
        f"""<div style="background:#fff;padding:0.55rem 0.75rem;border-radius:10px;
        box-shadow:0 1px 4px rgba(0,0,0,0.07);border-left:4px solid {color};
        min-height:68px;display:flex;flex-direction:column;justify-content:center;">
        <div style="font-size:0.62rem;color:#94a3b8;text-transform:uppercase;
        letter-spacing:0.5px;font-weight:600;margin-bottom:3px;">{label}</div>
        <div style="font-size:1.0rem;font-weight:700;color:{color};">{value}</div>
        {sub_html}</div>"""
    )


# ============================================================================
# DATA LOADING
# ============================================================================

def _load_all_data(conn) -> dict:
    results: dict = {}
    tasks = {
        "agot": lambda: cq.agotamiento(conn),
        "lt":   lambda: cq.leadtimes(conn),
        "mtd":  lambda: cq.ventas_mtd(conn),
    }
    with ThreadPoolExecutor(max_workers=3) as exc:
        futures = {exc.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as err:
                st.warning(f"Error cargando {key}: {err}")
                results[key] = pd.DataFrame()
    return results


# ============================================================================
# ENRICHMENT
# ============================================================================

def _build_pv_mtd(df_mtd: pd.DataFrame) -> pd.DataFrame:
    """Precio promedio del mes actual por SKU (agrega todos los canales)."""
    if df_mtd.empty or "SKU_PRODUCTO" not in df_mtd.columns:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "PV_PROM_MTD"])
    neto_col = next((c for c in df_mtd.columns if "NETO" in c.upper()), None)
    qty_col  = next((c for c in df_mtd.columns if "CANTIDAD" in c.upper()), None)
    if not neto_col or not qty_col:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "PV_PROM_MTD"])
    agg = (
        df_mtd.groupby("SKU_PRODUCTO", as_index=False)
        .agg(_neto=(neto_col, "sum"), _qty=(qty_col, "sum"))
    )
    agg["PV_PROM_MTD"] = (
        pd.to_numeric(agg["_neto"], errors="coerce")
        / pd.to_numeric(agg["_qty"], errors="coerce").replace(0, np.nan)
    )
    return agg[["SKU_PRODUCTO", "PV_PROM_MTD"]]


def _enrich(df: pd.DataFrame, df_lt: pd.DataFrame, df_mtd: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()

    # ── Asegurar SKU como string ─────────────────────────────────────────
    out["SKU_PRODUCTO"] = out["SKU_PRODUCTO"].astype(str).str.strip()

    # ── Coerce numerics ──────────────────────────────────────────────────
    for col in ["DIAS", "CANT_RECIBIDA_ULT_ING", "STOCK_ACTUAL_TOTAL",
                "VENTA_UNIDADES", "PCT_AVANCE", "PVP", "ULTIMO_COSTO"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    # ── % Avance = Vta / Recib, capeado a 100% ──────────────────────────
    # Recalculado en Python para garantizar consistencia con las columnas mostradas
    if "VENTA_UNIDADES" in out.columns and "CANT_RECIBIDA_ULT_ING" in out.columns:
        out["PCT_AVANCE"] = (
            out["VENTA_UNIDADES"] / out["CANT_RECIBIDA_ULT_ING"].replace(0, np.nan)
        ).clip(lower=0.0, upper=1.0)
    elif "PCT_AVANCE" in out.columns:
        out["PCT_AVANCE"] = pd.to_numeric(out["PCT_AVANCE"], errors="coerce").clip(lower=0.0, upper=1.0)

    # ── Lead Time desde Syncro ───────────────────────────────────────────
    lt_col = _detect_lt_col(df_lt) if not df_lt.empty else None
    sku_col_lt = (
        "SKU_PRODUCTO" if lt_col and "SKU_PRODUCTO" in df_lt.columns
        else "ID_MATERIAL" if lt_col and "ID_MATERIAL" in df_lt.columns
        else None
    )
    if lt_col and sku_col_lt:
        lt_agg = (
            df_lt.rename(columns={sku_col_lt: "SKU_PRODUCTO", lt_col: "_LT_RAW"})
            .assign(SKU_PRODUCTO=lambda x: x["SKU_PRODUCTO"].astype(str).str.strip())
            .groupby("SKU_PRODUCTO", as_index=False)["_LT_RAW"].max()
            .rename(columns={"_LT_RAW": "LEAD_TIME"})
        )
        out = out.merge(lt_agg, on="SKU_PRODUCTO", how="left")
        out["LEAD_TIME"] = pd.to_numeric(out["LEAD_TIME"], errors="coerce")
    else:
        out["LEAD_TIME"] = np.nan

    # ── Margen maestra: (PVP - Costo) / PVP ─────────────────────────────
    if "PVP" in out.columns and "ULTIMO_COSTO" in out.columns:
        out["MARGEN_MAESTRA"] = np.where(
            out["PVP"] > 0,
            (out["PVP"] - out["ULTIMO_COSTO"]) / out["PVP"],
            np.nan,
        )
    else:
        out["MARGEN_MAESTRA"] = np.nan

    # ── Precio promedio y margen del mes actual ──────────────────────────
    pv_mtd = _build_pv_mtd(df_mtd)
    if not pv_mtd.empty:
        pv_mtd["SKU_PRODUCTO"] = pv_mtd["SKU_PRODUCTO"].astype(str).str.strip()
        out = out.merge(pv_mtd, on="SKU_PRODUCTO", how="left")
    else:
        out["PV_PROM_MTD"] = np.nan

    if "PV_PROM_MTD" in out.columns and "ULTIMO_COSTO" in out.columns:
        out["MRG_MTD"] = np.where(
            out["PV_PROM_MTD"] > 0,
            (out["PV_PROM_MTD"] - out["ULTIMO_COSTO"]) / out["PV_PROM_MTD"],
            np.nan,
        )
    else:
        out["MRG_MTD"] = np.nan

    # ── Descuento PV vs PV MTD: (PVP - PV_MTD×1.18) / PVP ──────────────
    if "PVP" in out.columns and "PV_PROM_MTD" in out.columns:
        out["DESC_PV"] = np.where(
            out["PVP"] > 0,
            (out["PVP"] - out["PV_PROM_MTD"] * 1.18) / out["PVP"],
            np.nan,
        )
    else:
        out["DESC_PV"] = np.nan

    # ── Objetivo diario acumulado (curva tramo-lineal) ───────────────────
    # Tramo 1 (DIAS ≤ LT/2): tasa 1.2/LT → alcanza 60% al 50% del LT
    # Tramo 2 (DIAS >  LT/2): tasa 0.8/LT → alcanza 100% al final del LT
    # Ejemplo: LT=120 → OBJ_HOY=60% en dia 60; LT=180 → 60% en dia 90
    if "DIAS" in out.columns and "LEAD_TIME" in out.columns:
        lt   = out["LEAD_TIME"]
        dias = out["DIAS"]
        obj_tramo1 = (1.2 * dias / lt).clip(upper=1.0)
        obj_tramo2 = (0.2 + 0.8 * dias / lt).clip(upper=1.0)
        out["OBJ_HOY"] = np.where(
            lt > 0,
            np.where(dias <= lt / 2, obj_tramo1, obj_tramo2),
            np.nan,
        )
    else:
        out["OBJ_HOY"] = np.nan

    # ── Objetivo en unidades: Recib × OBJ_HOY (referencia informativa) ──
    if "CANT_RECIBIDA_ULT_ING" in out.columns:
        out["OBJ_HOY_UNDS"] = out["CANT_RECIBIDA_ULT_ING"] * out["OBJ_HOY"]
    else:
        out["OBJ_HOY_UNDS"] = np.nan

    # ── Tasa de venta diaria real: Vta / DIAS ────────────────────────────
    if "VENTA_UNIDADES" in out.columns and "DIAS" in out.columns:
        out["TASA_DIARIA"] = np.where(
            out["DIAS"] > 0,
            out["VENTA_UNIDADES"] / out["DIAS"],
            np.nan,
        )
    else:
        out["TASA_DIARIA"] = np.nan

    # ── Indice de velocidad: (Vta*LT) / (DIAS*Recib*0.7) ────────────────
    if all(c in out.columns for c in ["VENTA_UNIDADES", "LEAD_TIME", "DIAS", "CANT_RECIBIDA_ULT_ING"]):
        denom = out["DIAS"] * out["CANT_RECIBIDA_ULT_ING"] * 0.7
        out["INDICE_VEL"] = np.where(
            denom > 0,
            (out["VENTA_UNIDADES"] * out["LEAD_TIME"]) / denom,
            np.nan,
        )
    else:
        out["INDICE_VEL"] = np.nan

    # ── Evaluacion por indice de velocidad ──────────────────────────────
    if "FECHA_ULT_INGRESO_CD" not in out.columns:
        out["EVALUACION"] = "Sin Ingreso"
    else:
        out["EVALUACION"] = out.apply(
            lambda r: "Sin Ingreso" if pd.isna(r.get("FECHA_ULT_INGRESO_CD"))
            else _evaluar_agotamiento(
                r.get("DIAS"),
                r.get("LEAD_TIME"),
                r.get("VENTA_UNIDADES"),
                r.get("CANT_RECIBIDA_ULT_ING"),
            ),
            axis=1,
        )

    return out


# ============================================================================
# KPI SUMMARY
# ============================================================================

def _render_kpis(df: pd.DataFrame) -> None:
    total        = len(df)
    con_ingreso  = int(df["FECHA_ULT_INGRESO_CD"].notna().sum()) if "FECHA_ULT_INGRESO_CD" in df.columns else 0
    eval_counts  = df["EVALUACION"].value_counts() if "EVALUACION" in df.columns else pd.Series(dtype=int)
    pct_mean     = df.loc[df["PCT_AVANCE"].notna(), "PCT_AVANCE"].mean() if "PCT_AVANCE" in df.columns else None
    pct_str      = f"{min(pct_mean, 1.0):.0%}" if pct_mean is not None else "—"

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    with c1:
        _kpi_card("Total SKUs", f"{total:,}", COLORS.get("primary", "#1976D2"),
                  subtitle=f"{con_ingreso:,} con ingreso")
    with c2:
        _kpi_card("% Avance Prom.", pct_str, COLORS.get("secondary", "#78909C"))
    with c3:
        _kpi_card("🟢 Excelente", f"{int(eval_counts.get('Excelente', 0)):,}", _EVAL_COLORS["Excelente"])
    with c4:
        _kpi_card("🔵 Bueno",     f"{int(eval_counts.get('Bueno', 0)):,}",     _EVAL_COLORS["Bueno"])
    with c5:
        _kpi_card("🟠 Regular",   f"{int(eval_counts.get('Regular', 0)):,}",   _EVAL_COLORS["Regular"])
    with c6:
        _kpi_card("🔴 Malo",      f"{int(eval_counts.get('Malo', 0)):,}",      _EVAL_COLORS["Malo"])
    with c7:
        _kpi_card("⏳ En Proceso", f"{int(eval_counts.get('En Proceso', 0)):,}", _EVAL_COLORS["En Proceso"])


# ============================================================================
# TABLE
# ============================================================================

def _render_tabla(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("Sin datos para mostrar con los filtros aplicados.")
        return

    # ── Columnas de visualizacion ────────────────────────────────────────
    vis_cols  = [c for c in _DISPLAY_COLS  if c in df.columns]
    down_cols = [c for c in _DOWNLOAD_COLS if c in df.columns]

    display  = df[vis_cols].copy()
    download = df[down_cols].copy()

    # ── Descripcion completa ─────────────────────────────────────────────
    if "DESCRIPCION" in display.columns:
        display["DESCRIPCION"] = display["DESCRIPCION"].astype(str)

    # ── Porcentajes → escala 0-100 entero para mostrar con "%" ───────────
    for _pcol in ["PCT_AVANCE", "MARGEN_MAESTRA", "MRG_MTD", "OBJ_HOY", "DESC_PV", "INDICE_VEL"]:
        if _pcol in display.columns:
            display[_pcol] = (pd.to_numeric(display[_pcol], errors="coerce") * 100).round(0)

    # ── Emoji semaforo en EVALUACION (solo en display, no en download) ───
    if "EVALUACION" in display.columns:
        display["EVALUACION"] = display["EVALUACION"].map(
            lambda x: _EVAL_EMOJI.get(x, x) if pd.notna(x) else x
        )

    col_config = {
        "SKU_PRODUCTO":          st.column_config.TextColumn("SKU",         width="small"),
        "DESCRIPCION":           st.column_config.TextColumn("Descripcion", width="medium"),
        "AREA":                  st.column_config.TextColumn("Area",        width="small"),
        "LINEA":                 st.column_config.TextColumn("Linea",       width="small"),
        "MARCA":                 st.column_config.TextColumn("Marca",       width="small"),
        "FECHA_ULT_INGRESO_CD":  st.column_config.DateColumn("Ult.Ing.",    format="DD/MM/YY", width="small"),
        "DIAS":                  st.column_config.NumberColumn("Dias",      format="%d",       width="small"),
        "CANT_RECIBIDA_ULT_ING": st.column_config.NumberColumn("Recib.",    format="%,.0f",    width="small"),
        "STOCK_ACTUAL_TOTAL":    st.column_config.NumberColumn("Stock",     format="%,.0f",    width="small"),
        "VENTA_UNIDADES":        st.column_config.NumberColumn("Vta.",       format="%,.0f",    width="small"),
        "PCT_AVANCE":            st.column_config.NumberColumn("% Av.",      format="%d%%",     width="small"),
        "TASA_DIARIA":           st.column_config.NumberColumn("u/día",      format="%.2f",     width="small"),
        "LEAD_TIME":             st.column_config.NumberColumn("LT(d)",      format="%d",       width="small"),
        "OBJ_HOY":               st.column_config.NumberColumn("Obj.Hoy%",    format="%d%%",  width="small"),
        "OBJ_HOY_UNDS":          st.column_config.NumberColumn("Obj.Hoy Und", format="%,.0f", width="small"),
        "PVP":                   st.column_config.NumberColumn("PV",          format="%.2f",  width="small"),
        "MARGEN_MAESTRA":        st.column_config.NumberColumn("Mrg%",      format="%d%%",     width="small"),
        "PV_PROM_MTD":           st.column_config.NumberColumn("PV MTD",    format="%.2f",     width="small"),
        "MRG_MTD":               st.column_config.NumberColumn("Mrg MTD",   format="%d%%",     width="small"),
        "DESC_PV":               st.column_config.NumberColumn("Desc.PV",   format="%d%%",     width="small"),
        "INDICE_VEL":            st.column_config.NumberColumn("Índice",    format="%d%%",     width="small"),
        "EVALUACION":            st.column_config.TextColumn("Evaluacion",  width="medium"),
    }

    st.dataframe(
        display,
        column_config=col_config,
        use_container_width=True,
        hide_index=True,
        height=min(len(display) * 28 + 50, 680),
    )

    total_rec = df["CANT_RECIBIDA_ULT_ING"].sum() if "CANT_RECIBIDA_ULT_ING" in df.columns else 0
    total_vta = df["VENTA_UNIDADES"].sum()         if "VENTA_UNIDADES"         in df.columns else 0
    st.caption(
        f"{len(df):,} SKUs | Recibido: {total_rec:,.0f} und | Vendido: {total_vta:,.0f} und"
    )
    download_buttons(download, prefix="agotamiento")


# ============================================================================
# FILTERS
# ============================================================================

def _render_filters(df: pd.DataFrame) -> dict:
    def _opts(col):
        if col not in df.columns:
            return []
        return sorted(df[col].dropna().astype(str).str.strip().unique().tolist())

    # Años disponibles desde FECHA_ULT_INGRESO_CD
    if "FECHA_ULT_INGRESO_CD" in df.columns:
        anio_series = pd.to_datetime(df["FECHA_ULT_INGRESO_CD"], errors="coerce").dt.year
        anio_opts   = sorted(anio_series.dropna().astype(int).unique().tolist(), reverse=True)
        anio_opts   = [str(a) for a in anio_opts]
    else:
        anio_opts = []

    # Preseleccion por defecto: solo primera carga
    area_opts = _opts("AREA")
    mix_opts  = _opts("MIX_OFICIAL")
    if "agot_area" not in st.session_state:
        st.session_state["agot_area"] = [a for a in area_opts if a.upper() in _DEFAULT_AREAS]
    if "agot_mix" not in st.session_state:
        st.session_state["agot_mix"]  = [m for m in mix_opts  if m.upper() in _DEFAULT_MIX]

    with st.expander("Filtros", expanded=True):
        c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
        with c1:
            sel_area = st.multiselect("Area",       options=area_opts, key="agot_area")
        with c2:
            linea_pool = (
                sorted(df.loc[df["AREA"].isin(sel_area), "LINEA"].dropna().unique().tolist())
                if sel_area and "LINEA" in df.columns else _opts("LINEA")
            )
            sel_linea = st.multiselect("Linea",       options=linea_pool,           default=[], key="agot_linea")
        with c3:
            sel_marca = st.multiselect("Marca",       options=_opts("MARCA"),       default=[], key="agot_marca")
        with c4:
            sel_mix   = st.multiselect("Mix Oficial", options=mix_opts,                         key="agot_mix")
        with c5:
            sel_proc  = st.multiselect("Procedencia", options=_opts("PROCEDENCIA"), default=[], key="agot_proc")
        with c6:
            eval_opts = [e for e in _EVAL_ORDER if e in df["EVALUACION"].unique()] if "EVALUACION" in df.columns else []
            sel_eval  = st.multiselect("Evaluacion",  options=eval_opts,            default=[], key="agot_eval")
        with c7:
            sel_anio  = st.multiselect("Año Ingreso", options=anio_opts,            default=[], key="agot_anio")

    return {
        "area":        sel_area,
        "linea":       sel_linea,
        "marca":       sel_marca,
        "mix_oficial": sel_mix,
        "procedencia": sel_proc,
        "evaluacion":  sel_eval,
        "anio":        sel_anio,
    }


def _apply_filters(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    out = df.copy()
    mapping = {
        "area":        "AREA",
        "linea":       "LINEA",
        "marca":       "MARCA",
        "mix_oficial": "MIX_OFICIAL",
        "procedencia": "PROCEDENCIA",
        "evaluacion":  "EVALUACION",
    }
    for key, col in mapping.items():
        vals = filters.get(key, [])
        if vals and col in out.columns:
            out = out[out[col].isin(vals)]
    # Filtro de año sobre FECHA_ULT_INGRESO_CD
    anios = filters.get("anio", [])
    if anios and "FECHA_ULT_INGRESO_CD" in out.columns:
        anio_series = pd.to_datetime(out["FECHA_ULT_INGRESO_CD"], errors="coerce").dt.year.astype("Int64").astype(str)
        out = out[anio_series.isin(anios)]
    return out


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def render_analisis_agotamiento(conn) -> None:
    st.html("<h2 class='sub-header'>Agotamiento de Stock</h2>")

    try:
        with lottie_spinner("snowflake"):
            data = _load_all_data(conn)
    except Exception as exc:
        st.error(f"Error al cargar datos: {exc}")
        import traceback
        st.code(traceback.format_exc())
        return

    df_raw = apply_pm_filter(data.get("agot", pd.DataFrame()))
    df_lt  = data.get("lt",  pd.DataFrame())
    df_mtd = data.get("mtd", pd.DataFrame())

    if df_raw.empty:
        st.warning("Sin datos de agotamiento disponibles.")
        return

    # ── Enriquecimiento ──────────────────────────────────────────────────
    df = _enrich(df_raw, df_lt, df_mtd)

    # ── Filtro base: solo SKUs con stock > 0 ────────────────────────────
    if "STOCK_ACTUAL_TOTAL" in df.columns:
        df = df[df["STOCK_ACTUAL_TOTAL"] > 0].reset_index(drop=True)

    if df.empty:
        st.warning("Sin SKUs con stock disponible.")
        return

    if df["LEAD_TIME"].isna().all():
        st.warning(
            "No se encontro columna de Lead Time en Syncro. "
            "La evaluacion aplica los umbrales de % avance sin considerar el tiempo vs LT.",
            icon="⚠️",
        )

    # ── Filtros ──────────────────────────────────────────────────────────
    filters = _render_filters(df)
    df_filt = _apply_filters(df, filters)

    st.markdown("---")

    # ── KPIs ────────────────────────────────────────────────────────────
    _render_kpis(df_filt)

    st.markdown("---")

    # ── Tabla ────────────────────────────────────────────────────────────
    _render_tabla(df_filt)
