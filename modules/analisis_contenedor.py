"""Analisis Contenedor — alerta de sobrestock con cubicaje y recomendacion de contenedor.

Dos tablas:
  Tabla 1 — Stock en bodegas Primera/Segunda Calidad, Devoluciones, Outlet
             (excluye "Cdu. Almacen Contenedor").
             Columnas: SKU, Descripcion, Area, Linea, Sublinea, Marca,
                       M3, Und x Pallet, M3 Total, Cant Pallets,
                       Stock, Stock Costo, MOI, Sugerido Contenedor.
             Resumen inferior: total M3 sugerido y numero de contenedores.

  Tabla 2 — Stock exclusivo de "Cdu. Almacen Contenedor".
             Mismas columnas base + MOI (sin columna Sugerido).

  Botones MOI: 3 / 6 / 12 meses (promedio movil de venta).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import streamlit as st

from config import COLORS, apply_pm_filter
from db.cache import cached_query as cq
from utils.export import download_buttons
from utils.filters import human_format, norm_cols
from utils.ui_animations import lottie_spinner
from utils.ui_components import page_header

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Usable cargo volume (m³) por tipo de contenedor
_M3_20FT = 25.0
_M3_40HQ = 67.0

# Umbral de MOI para alerta de sobrestock (meses)
_MOI_UMBRAL = 7.0

# Patron de nombre de bodega del contenedor (coincidencia parcial, case-insensitive)
_CONTENEDOR_PATTERN = "contenedor"

# Nombres de bodegas que SI deben aparecer en Tabla 1 (patrones case-insensitive).
# Son las bodegas visibles en la imagen: Primera/Segunda Calidad, Devoluciones, Outlet.
_BODEGAS_INCLUIDAS_PATTERNS = [
    "cdu.",          # todos los "Cdu. Simple...", "Cdu. Bpa...", "Cdu. Devoluciones..."
    "transito mercaderia",
]

# Candidate column names for volume and pallet data in dt_producto
_VOL_CANDIDATES    = ["VOLUMENM3", "VOLUMEN_M3", "VOLUMEN", "CBM", "M3", "VOLUMEN_UNITARIO", "DENSIDAD"]
_PALLET_CANDIDATES = ["CANT_POR_PALLET", "UNIDADESPALETA", "UNIDADES_X_PALLET", "UNIDADES_PALLET", "PALLET", "UND_PALLET"]

# MOI range filter options (label → (min_inclusive, max_exclusive or None))
_MOI_RANGES = [
    ("6 - 12 meses",  6,  12),
    ("12 - 18 meses", 12, 18),
    ("18 - 24 meses", 18, 24),
    ("> 24 meses",    24, None),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_contenedor_bodega(nom: str) -> bool:
    return _CONTENEDOR_PATTERN in nom.lower()


def _is_bodega_incluida(nom: str) -> bool:
    """True si la bodega pertenece al set de bodegas de interes (sin contenedor)."""
    nom_l = nom.lower()
    return any(p in nom_l for p in _BODEGAS_INCLUIDAS_PATTERNS)



def _load_dims(conn) -> pd.DataFrame:
    """Load M3 and Und x Pallet from OT_PRODUCTO_UNIMAR (cached 24 h via cq)."""
    try:
        return cq.contenedor_dims(conn)
    except Exception:
        return pd.DataFrame(columns=["COD_PRODUCTO", "M3_UNIDAD", "UNIDADES_X_PALLET"])



def _load_data(conn):
    with lottie_spinner("snowflake"):
        df_stock = cq.contenedor_stock(conn)
        df_ventas = cq.contenedor_ventas_12m(conn)
        df_dims = _load_dims(conn)

    # Merge volume dimensions into stock
    if not df_dims.empty and "COD_PRODUCTO" in df_dims.columns:
        df_stock = df_stock.merge(
            df_dims.rename(columns={"COD_PRODUCTO": "SKU_PRODUCTO"}),
            on="SKU_PRODUCTO",
            how="left",
        )

    # Ensure M3_UNIDAD and UNIDADES_X_PALLET always exist (default 0)
    for col in ("M3_UNIDAD", "UNIDADES_X_PALLET"):
        if col not in df_stock.columns:
            df_stock[col] = 0.0
        else:
            df_stock[col] = pd.to_numeric(df_stock[col], errors="coerce").fillna(0.0)

    return df_stock, df_ventas


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    for c in ["M3_UNIDAD", "UNIDADES_X_PALLET", "STOCK_UNIDADES", "STOCK_COSTO"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def _calc_moi(df: pd.DataFrame, df_ventas: pd.DataFrame, n_meses: int) -> pd.Series:
    """Retorna Serie MOI indexada por SKU_PRODUCTO."""
    cutoff = pd.Timestamp.now().normalize().to_period("M").to_timestamp()
    start = cutoff - pd.DateOffset(months=n_meses)

    if "PERIODO" in df_ventas.columns:
        df_ventas["PERIODO"] = pd.to_datetime(df_ventas["PERIODO"], errors="coerce")
    df_v = df_ventas[df_ventas["PERIODO"] >= start].copy()

    vta = (
        df_v.groupby("SKU_PRODUCTO", as_index=False)["UNIDADES_VENDIDAS"]
        .sum()
        .rename(columns={"UNIDADES_VENDIDAS": "VTA_TOTAL"})
    )
    vta["VENTA_PROM_MENSUAL"] = vta["VTA_TOTAL"] / n_meses

    merged = df[["SKU_PRODUCTO", "STOCK_UNIDADES"]].merge(
        vta[["SKU_PRODUCTO", "VENTA_PROM_MENSUAL"]], on="SKU_PRODUCTO", how="left"
    )
    vpm = merged["VENTA_PROM_MENSUAL"].fillna(0)
    moi = np.where(vpm > 0, merged["STOCK_UNIDADES"] / vpm, np.nan)
    return pd.DataFrame({"MOI": moi, "VENTA_PROM_MENSUAL": vpm.values})


def _enrich(df: pd.DataFrame, df_ventas: pd.DataFrame, n_meses: int) -> pd.DataFrame:
    """Agrega M3_TOTAL, CANT_PALLETS, MOI y columnas de cantidad sugerida."""
    df = df.copy()
    df["M3_TOTAL"] = (df["M3_UNIDAD"] * df["STOCK_UNIDADES"]).round(4)
    df["CANT_PALLETS"] = np.where(
        df["UNIDADES_X_PALLET"] > 0,
        (df["STOCK_UNIDADES"] / df["UNIDADES_X_PALLET"]).round(2),
        np.nan,
    )
    calc = _calc_moi(df, df_ventas, n_meses)
    df["MOI"] = calc["MOI"].values
    df["VENTA_PROM_MENSUAL"] = calc["VENTA_PROM_MENSUAL"].values

    # Cant. Sugerida a separar al contenedor: dejar 6 meses en destino actual
    vpm = df["VENTA_PROM_MENSUAL"].fillna(0)
    stock = df["STOCK_UNIDADES"]
    df["CANT_SUGERIDA"] = np.where(
        vpm > 0,
        np.maximum(0, stock - vpm * 6).round(0),
        stock,  # sin ventas → todo es sugerido
    ).astype(float)
    df["M3_SUGERIDA"] = (df["CANT_SUGERIDA"] * df["M3_UNIDAD"]).round(4)
    df["PALLETS_SUGERIDOS"] = np.where(
        df["UNIDADES_X_PALLET"] > 0,
        (df["CANT_SUGERIDA"] / df["UNIDADES_X_PALLET"]).round(2),
        np.nan,
    )
    return df


def _sugerido_col(df: pd.DataFrame, m3_min: float) -> pd.Series:
    """Columna Sugerido Contenedor: alerta si MOI > umbral Y M3_Total > m3_min."""
    cond = (df["MOI"].fillna(0) > _MOI_UMBRAL) & (df["M3_TOTAL"] >= m3_min)
    return np.where(cond, "Se Sugiere separar a un contenedor", "")


def _container_recommendation(total_m3: float) -> str:
    if total_m3 <= 0:
        return ""
    if total_m3 <= _M3_20FT:
        n = math.ceil(total_m3 / _M3_20FT)
        tipo = "20'"
    else:
        n = math.ceil(total_m3 / _M3_40HQ)
        tipo = "40' HQ"
    return (
        f"La cantidad a separar al contenedor equivale a **{total_m3:,.2f} m³** "
        f"(dejando 6 meses de cobertura en destino), "
        f"los cuales cubican a **{n} contenedor(es) de {tipo}**"
    )


def _moi_color(val):
    if pd.isna(val) or val <= 0:
        return COLORS.get("medium_gray", "#94a3b8")
    if val < 6:
        return COLORS.get("status_on_track", "#22c55e")
    if val < 12:
        return COLORS.get("status_at_risk", "#f59e0b")
    return COLORS.get("status_critical", "#ef4444")


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

_DISPLAY_COLS_T1 = [
    "SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
    "PRODUCTO_STATUS",
    "M3_UNIDAD", "UNIDADES_X_PALLET", "M3_TOTAL", "CANT_PALLETS",
    "STOCK_UNIDADES", "STOCK_COSTO", "MOI", "SUGERIDO_CONTENEDOR",
    "CANT_SUGERIDA", "M3_SUGERIDA", "PALLETS_SUGERIDOS",
]

_DISPLAY_COLS_T2 = [
    "SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
    "ALERTA_RETIRAR",
    "M3_UNIDAD", "UNIDADES_X_PALLET", "M3_TOTAL", "CANT_PALLETS",
    "STOCK_UNIDADES", "STOCK_COSTO", "MOI",
]

_COL_CONFIG_BASE = {
    "SKU_PRODUCTO":       st.column_config.TextColumn("SKU", width="small"),
    "NOM_PRODUCTO":       st.column_config.TextColumn("Descripcion", width="large"),
    "AREA":               st.column_config.TextColumn("Area", width="small"),
    "LINEA":              st.column_config.TextColumn("Linea", width="small"),
    "SUBLINEA":           st.column_config.TextColumn("Sublinea", width="small"),
    "MARCA":              st.column_config.TextColumn("Marca", width="small"),
    "M3_UNIDAD":          st.column_config.NumberColumn("M3", format="%.4f"),
    "UNIDADES_X_PALLET":  st.column_config.NumberColumn("Und x Pallet", format="%.0f"),
    "M3_TOTAL":           st.column_config.NumberColumn("M3 Total", format="%.3f"),
    "CANT_PALLETS":       st.column_config.NumberColumn("Cant Pallets", format="%.2f"),
    "STOCK_UNIDADES":     st.column_config.NumberColumn("Stock", format="%d"),
    "STOCK_COSTO":        st.column_config.NumberColumn("Stock Costo", format="$%,.0f"),
    "MOI":                st.column_config.NumberColumn("MOI (meses)", format="%.1f"),
    "PRODUCTO_STATUS":     st.column_config.TextColumn("Status", width="small"),
    "ALERTA_RETIRAR":      st.column_config.TextColumn("Alerta", width="large"),
    "SUGERIDO_CONTENEDOR": st.column_config.TextColumn("Sugerido Contenedor", width="large"),
    "CANT_SUGERIDA":      st.column_config.NumberColumn("Cant. a Separar", format="%.0f",
                              help="Unidades a mover al contenedor dejando 6 meses en destino actual"),
    "M3_SUGERIDA":        st.column_config.NumberColumn("M3 a Separar", format="%.3f"),
    "PALLETS_SUGERIDOS":  st.column_config.NumberColumn("Pallets a Separar", format="%.2f"),
}


def _add_status(df: pd.DataFrame, df_primera_venta: pd.DataFrame, meses_nuevo: int = 12) -> pd.DataFrame:
    """Agrega PRODUCTO_STATUS (NUEVO / ESTABLECIDO / SIN VENTAS) basado en primera venta."""
    if df_primera_venta.empty:
        df["PRODUCTO_STATUS"] = "SIN VENTAS"
        return df
    pv = norm_cols(df_primera_venta.copy())[["SKU_PRODUCTO", "MESES_EN_CIA"]]
    merged = df.merge(pv, on="SKU_PRODUCTO", how="left")
    merged["PRODUCTO_STATUS"] = np.where(
        merged["MESES_EN_CIA"].isna(), "SIN VENTAS",
        np.where(merged["MESES_EN_CIA"] <= meses_nuevo, "NUEVO", "ESTABLECIDO"),
    )
    return merged.drop(columns=["MESES_EN_CIA"], errors="ignore")


def _apply_filters(
    df: pd.DataFrame,
    sel_areas: list,
    sel_lineas: list,
    sel_marcas: list,
    sel_moi_ranges: list,
    sel_status: list | None = None,
) -> pd.DataFrame:
    if sel_areas:
        df = df[df["AREA"].isin(sel_areas)]
    if sel_lineas:
        df = df[df["LINEA"].isin(sel_lineas)]
    if sel_marcas:
        df = df[df["MARCA"].isin(sel_marcas)]
    if sel_status and "PRODUCTO_STATUS" in df.columns:
        df = df[df["PRODUCTO_STATUS"].isin(sel_status)]
    if sel_moi_ranges and "MOI" in df.columns:
        mask = pd.Series(False, index=df.index)
        for label in sel_moi_ranges:
            for lbl, lo, hi in _MOI_RANGES:
                if lbl == label:
                    row_mask = df["MOI"].fillna(-1) >= lo
                    if hi is not None:
                        row_mask &= df["MOI"].fillna(-1) < hi
                    mask |= row_mask
        df = df[mask]
    return df


def _render_moi_buttons(prefix: str) -> int:
    """Renders 3/6/12 month buttons; returns the selected number of months."""
    key = f"moi_n_meses_{prefix}"
    if key not in st.session_state:
        st.session_state[key] = 6

    c1, c2, c3, _ = st.columns([1, 1, 1, 6])
    with c1:
        if st.button("3 meses", key=f"btn_3m_{prefix}",
                     type="primary" if st.session_state[key] == 3 else "secondary"):
            st.session_state[key] = 3
            st.rerun()
    with c2:
        if st.button("6 meses", key=f"btn_6m_{prefix}",
                     type="primary" if st.session_state[key] == 6 else "secondary"):
            st.session_state[key] = 6
            st.rerun()
    with c3:
        if st.button("12 meses", key=f"btn_12m_{prefix}",
                     type="primary" if st.session_state[key] == 12 else "secondary"):
            st.session_state[key] = 12
            st.rerun()

    return st.session_state[key]


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render_analisis_contenedor(conn):
    """Entry point — called from app.py."""
    st.html(page_header(
        "Analisis Contenedor",
        "Alerta de sobrestock por cubicaje · Primera/Segunda Calidad · Devoluciones · Outlet",
    ))

    if conn is None:
        st.warning("No hay conexion a Snowflake.")
        return

    df_raw, df_ventas = _load_data(conn)
    df_raw = apply_pm_filter(df_raw)

    try:
        df_primera_venta = cq.primera_venta_sku(conn)
    except Exception:
        df_primera_venta = pd.DataFrame()

    if df_raw.empty:
        st.info("No se encontraron datos de stock por bodega.")
        return

    df_raw = _coerce(df_raw)

    if "PERIODO" in df_ventas.columns:
        df_ventas["PERIODO"] = pd.to_datetime(df_ventas["PERIODO"], errors="coerce")
    if "UNIDADES_VENDIDAS" in df_ventas.columns:
        df_ventas["UNIDADES_VENDIDAS"] = pd.to_numeric(df_ventas["UNIDADES_VENDIDAS"], errors="coerce").fillna(0)

    # Split by bodega type
    mask_cont = df_raw["NOM_ALMACEN"].apply(_is_contenedor_bodega)
    mask_incl = df_raw["NOM_ALMACEN"].apply(_is_bodega_incluida)

    df_cont_raw   = df_raw[mask_cont].copy()
    df_otras_raw  = df_raw[mask_incl & ~mask_cont].copy()

    # SKUs con MOI <= 1 en Tabla 1 (calculado antes de filtros de usuario)
    skus_bajo_moi: set = set()

    # Aggregate per SKU (sum across bodegas within each group)
    def _agg_sku(df):
        grp_cols = ["SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                    "M3_UNIDAD", "UNIDADES_X_PALLET"]
        grp_cols = [c for c in grp_cols if c in df.columns]
        num_cols = {c: "sum" for c in ["STOCK_UNIDADES", "STOCK_COSTO"] if c in df.columns}
        # M3_UNIDAD and UNIDADES_X_PALLET: take max (should be same per SKU across bodegas)
        dim_cols = {c: "max" for c in ["M3_UNIDAD", "UNIDADES_X_PALLET"] if c in df.columns}
        agg = {**dim_cols, **num_cols}
        base_cols = ["SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA"]
        base_cols = [c for c in base_cols if c in df.columns]
        return df.groupby(base_cols, as_index=False).agg(agg) if base_cols else df

    df_otras = _agg_sku(df_otras_raw)
    df_cont  = _agg_sku(df_cont_raw)

    # =========================================================================
    # Stock en Bodegas CD (excluye Almacen Contenedor)
    # =========================================================================
    st.markdown("---")
    st.markdown("### 📦 Stock en Bodegas CD")

    n_meses_t1 = _render_moi_buttons("t1")
    st.caption(f"MOI calculado con promedio movil de **{n_meses_t1} meses**")

    if df_otras.empty:
        st.info(
            "No hay stock en bodegas de Primera/Segunda Calidad, Devoluciones u Outlet. "
            "Verifique que los nombres de bodega en Snowflake contengan 'Cdu.' o 'Transito Mercaderia'."
        )
    else:
        # Enrich first so MOI is available for the filter options
        df_t1 = _enrich(df_otras, df_ventas, n_meses_t1)
        df_t1 = _add_status(df_t1, df_primera_venta)

        # Capturar SKUs con stock critico (MOI <= 1) antes de aplicar filtros de usuario
        skus_bajo_moi = set(
            df_t1.loc[df_t1["MOI"].fillna(float("inf")) <= 1, "SKU_PRODUCTO"]
        )

        # ── Filters ──────────────────────────────────────────────────────────
        with st.expander("🔍 Filtros", expanded=False):
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                opt_areas = sorted(df_t1["AREA"].dropna().unique().tolist()) if "AREA" in df_t1.columns else []
                sel_areas = st.multiselect("Área", opt_areas, key="cont_t1_area")
            with fc2:
                opt_lineas = sorted(df_t1["LINEA"].dropna().unique().tolist()) if "LINEA" in df_t1.columns else []
                sel_lineas = st.multiselect("Línea", opt_lineas, key="cont_t1_linea")
            with fc3:
                opt_marcas = sorted(df_t1["MARCA"].dropna().unique().tolist()) if "MARCA" in df_t1.columns else []
                sel_marcas = st.multiselect("Marca", opt_marcas, key="cont_t1_marca")

            fc4, fc5, fc6 = st.columns([2, 2, 1])
            with fc4:
                moi_opts = [lbl for lbl, *_ in _MOI_RANGES]
                sel_moi_ranges = st.multiselect("Rango MOI", moi_opts, key="cont_t1_moi_range")
            with fc5:
                _SUGERIDO_OPTS = ["Todos", "Solo Sugeridos", "Sin Sugerencia"]
                sel_sugerido = st.selectbox("Sugerido Contenedor", _SUGERIDO_OPTS, key="cont_t1_sugerido")
            with fc6:
                m3_min_t1 = st.number_input(
                    "M3 min alerta",
                    min_value=0.0, max_value=200.0, value=5.0, step=0.5,
                    key="cont_t1_m3_umbral",
                    help=f"M3 Total mínimo para alerta 'Sugerido Contenedor' (MOI > {_MOI_UMBRAL:.0f} m.)",
                )

            fs1, fs2 = st.columns([1, 2])
            with fs1:
                sel_status_t1 = st.multiselect(
                    "Status del Producto",
                    ["NUEVO", "ESTABLECIDO", "SIN VENTAS"],
                    key="cont_t1_status",
                    help="NUEVO = primera venta en los últimos 12 meses  |  "
                         "ESTABLECIDO = más de 12 meses de historial  |  "
                         "SIN VENTAS = sin ventas registradas",
                )

        df_t1 = _apply_filters(df_t1, sel_areas, sel_lineas, sel_marcas, sel_moi_ranges, sel_status_t1)
        df_t1["SUGERIDO_CONTENEDOR"] = _sugerido_col(df_t1, m3_min_t1)

        # Sugerido filter
        if sel_sugerido == "Solo Sugeridos":
            df_t1 = df_t1[df_t1["SUGERIDO_CONTENEDOR"] != ""]
        elif sel_sugerido == "Sin Sugerencia":
            df_t1 = df_t1[df_t1["SUGERIDO_CONTENEDOR"] == ""]

        # Sort: sugeridos primero, luego por M3_TOTAL desc
        df_t1["_TIENE_ALERTA"] = (df_t1["SUGERIDO_CONTENEDOR"] != "").astype(int)
        df_t1 = df_t1.sort_values(["_TIENE_ALERTA", "M3_TOTAL"], ascending=[False, False])
        df_t1 = df_t1.drop(columns=["_TIENE_ALERTA"])

        show_cols = [c for c in _DISPLAY_COLS_T1 if c in df_t1.columns]
        st.dataframe(
            df_t1[show_cols],
            use_container_width=True,
            hide_index=True,
            height=min(700, len(df_t1) * 38 + 42),
            column_config=_COL_CONFIG_BASE,
        )
        download_buttons(df_t1[show_cols], prefix="analisis_contenedor_bodegas")

        # ── Resumen sugerido contenedor ──
        df_alerta = df_t1[df_t1["SUGERIDO_CONTENEDOR"] != ""]
        if not df_alerta.empty:
            total_m3      = df_alerta["M3_SUGERIDA"].fillna(0).sum()
            total_pallets = df_alerta["PALLETS_SUGERIDOS"].fillna(0).sum()
            n_skus_alerta = len(df_alerta)
            rec = _container_recommendation(total_m3)

            st.markdown("---")
            st.markdown("#### 🚨 Resumen Sobrestock Sugerido para Contenedor")

            ka, kb, kc = st.columns(3)
            _kpi = lambda lbl, val, sub, color: (
                f'<div style="background:{color}18;border:1px solid {color}40;'
                f'border-radius:10px;padding:1rem 1.2rem;">'
                f'<div style="font-size:0.8rem;color:#64748b;font-weight:600;">{lbl}</div>'
                f'<div style="font-size:2rem;font-weight:800;color:{color};">{val}</div>'
                f'<div style="font-size:0.8rem;color:#64748b;">{sub}</div>'
                f'</div>'
            )
            with ka:
                st.html(_kpi(
                    "SKUs con alerta", f"{n_skus_alerta:,}",
                    f"MOI > {_MOI_UMBRAL:.0f} meses y M3 ≥ {m3_min_t1:.1f}",
                    COLORS.get("status_critical", "#ef4444"),
                ))
            with kb:
                st.html(_kpi(
                    "M3 a separar", f"{total_m3:,.2f} m³",
                    "M3 Sugerida (excluye 6 meses de cobertura)",
                    COLORS.get("status_at_risk", "#f59e0b"),
                ))
            with kc:
                st.html(_kpi(
                    "Pallets a separar", f"{total_pallets:,.1f}",
                    "Pallets Sugeridos a mover al contenedor",
                    COLORS.get("tertiary_teal", "#23CED3"),
                ))

            if rec:
                st.html(
                    f'<div style="background:{COLORS.get("primary","#065E8B")}15;'
                    f'border-left:4px solid {COLORS.get("primary","#065E8B")};'
                    f'border-radius:0 8px 8px 0;padding:0.9rem 1.2rem;margin-top:0.5rem;">'
                    f'<span style="font-size:1rem;font-weight:600;color:{COLORS.get("primary","#065E8B")};">'
                    f'📦 {rec}</span>'
                    f'</div>'
                )
        else:
            st.success(
                f"No hay SKUs con MOI > {_MOI_UMBRAL:.0f} meses y M3 Total ≥ {m3_min_t1:.1f} m³. "
                "Sin sugerencias de contenedor por el momento."
            )

    # =========================================================================
    # Stock en Almacen Contenedor
    # =========================================================================
    st.markdown("---")
    st.markdown("### 🏭 Stock en Almacen Contenedor")

    n_meses_t2 = _render_moi_buttons("t2")
    st.caption(f"MOI calculado con promedio movil de **{n_meses_t2} meses**")

    if df_cont.empty:
        st.info(
            "No hay stock en el Almacen Contenedor. "
            "Verifique que el nombre de bodega en Snowflake contenga 'Contenedor'."
        )
    else:
        df_t2 = _enrich(df_cont, df_ventas, n_meses_t2)

        # ── Filtros independientes para Almacen Contenedor ───────────────────
        with st.expander("🔍 Filtros", expanded=False):
            g1, g2, g3 = st.columns(3)
            with g1:
                opt_a2 = sorted(df_t2["AREA"].dropna().unique().tolist()) if "AREA" in df_t2.columns else []
                sel_areas_t2 = st.multiselect("Área", opt_a2, key="cont_t2_area")
            with g2:
                opt_l2 = sorted(df_t2["LINEA"].dropna().unique().tolist()) if "LINEA" in df_t2.columns else []
                sel_lineas_t2 = st.multiselect("Línea", opt_l2, key="cont_t2_linea")
            with g3:
                opt_m2 = sorted(df_t2["MARCA"].dropna().unique().tolist()) if "MARCA" in df_t2.columns else []
                sel_marcas_t2 = st.multiselect("Marca", opt_m2, key="cont_t2_marca")

            g4, g5 = st.columns([2, 2])
            with g4:
                moi_opts2 = [lbl for lbl, *_ in _MOI_RANGES]
                sel_moi_t2 = st.multiselect("Rango MOI", moi_opts2, key="cont_t2_moi_range")
            with g5:
                sel_moi_sin_datos = st.checkbox(
                    "Incluir SKUs sin venta (MOI indefinido)", value=True, key="cont_t2_sin_moi"
                )

        df_t2 = _apply_filters(df_t2, sel_areas_t2, sel_lineas_t2, sel_marcas_t2, sel_moi_t2)
        if not sel_moi_sin_datos:
            df_t2 = df_t2[df_t2["MOI"].notna()]

        # Alerta: SKUs cuyo MOI en Tabla 1 es <= 1 deben salir del contenedor
        df_t2["ALERTA_RETIRAR"] = df_t2["SKU_PRODUCTO"].apply(
            lambda sku: "⚠️ Retirar de Contenedor y Poner Disponible"
            if sku in skus_bajo_moi else ""
        )

        # Ordenar: alertas primero, luego por M3 total descendente
        df_t2["_tiene_alerta"] = (df_t2["ALERTA_RETIRAR"] != "").astype(int)
        df_t2 = df_t2.sort_values(
            ["_tiene_alerta", "M3_TOTAL"], ascending=[False, False]
        ).drop(columns=["_tiene_alerta"])

        show_cols2 = [c for c in _DISPLAY_COLS_T2 if c in df_t2.columns]
        st.dataframe(
            df_t2[show_cols2],
            use_container_width=True,
            hide_index=True,
            height=min(600, len(df_t2) * 38 + 42),
            column_config=_COL_CONFIG_BASE,
        )
        download_buttons(df_t2[show_cols2], prefix="analisis_contenedor_almacen")

        # KPIs resumen contenedor
        total_stock_cont = df_t2["STOCK_UNIDADES"].sum()
        total_costo_cont = df_t2["STOCK_COSTO"].sum()
        total_m3_cont    = df_t2["M3_TOTAL"].sum()
        n_skus_cont      = df_t2["SKU_PRODUCTO"].nunique()

        kc1, kc2, kc3, kc4 = st.columns(4)
        _card = lambda lbl, val, color: (
            f'<div style="background:{color}18;border:1px solid {color}40;'
            f'border-radius:10px;padding:0.75rem 1rem;">'
            f'<div style="font-size:0.75rem;color:#64748b;font-weight:600;">{lbl}</div>'
            f'<div style="font-size:1.5rem;font-weight:800;color:{color};">{val}</div>'
            f'</div>'
        )
        with kc1:
            st.html(_card("SKUs en Contenedor", f"{n_skus_cont:,}", COLORS.get("primary", "#065E8B")))
        with kc2:
            st.html(_card("Stock Total (und)", f"{total_stock_cont:,.0f}", COLORS.get("tertiary_teal", "#23CED3")))
        with kc3:
            st.html(_card("Stock Costo", f"${human_format(total_costo_cont)}", COLORS.get("secondary", "#632CFF")))
        with kc4:
            st.html(_card("M3 Total", f"{total_m3_cont:,.2f} m³", COLORS.get("tertiary_blue", "#2DAAFF")))
