"""Resumen de Compra Proyectada — carga directa del CSV, sin necesidad de correr la proyeccion."""

import io
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from db.queries import QUERY_MAESTRA, QUERY_PERFIL_RESUMEN
from utils.filters import norm_cols, fmt_clp, fmt_usd
from utils.export import download_buttons
from config import COLORS, dorel_layout, apply_pm_filter
from utils.file_persistence import get_saved_file_path, get_saved_file_info, save_input_file
from utils.auth import get_current_user

# ============================================================================
# HELPERS
# ============================================================================

_MES_NAMES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}

_DIM_OPTIONS = {
    "SKU":         "SKU_PRODUCTO",
    "Proveedor":   "PROVEEDOR",
    "Area":        "AREA",
    "Linea":       "LINEA",
    "Sublinea":    "SUBLINEA",
    "Marca":       "MARCA",
    "Procedencia": "PROCEDENCIA",
    "Periodo Entrega": "PERIODO_ENTREGA",
}


def _fmt_mm(val):
    if abs(val) >= 1_000_000_000:
        return f"${val/1e9:,.1f} MM"
    if abs(val) >= 1_000_000:
        return f"${val/1e6:,.1f} M"
    if abs(val) >= 1_000:
        return f"${val/1e3:,.0f} K"
    return f"${val:,.0f}"


def _periodo_label(ts):
    """Convert a Timestamp to 'Mes YY' string."""
    return _MES_NAMES.get(ts.month, "?") + " " + str(ts.year)[-2:]


def _periodo_sort(ts):
    """String for sorting: YYYYMM."""
    return ts.strftime("%Y%m")


# ============================================================================
# CSV PROCESSING
# ============================================================================

def _process_csv(file) -> pd.DataFrame:
    """Parse the purchase CSV."""
    try:
        raw = pd.read_csv(file)
    except Exception as e:
        st.error(f"Error leyendo CSV: {e}")
        return pd.DataFrame()

    raw = norm_cols(raw)

    col_fecha_c = next((c for c in raw.columns if "FECHA" in c), None)
    col_sku     = next((c for c in raw.columns if "MATERIAL" in c or "SKU" in c), None)
    col_ord     = next((c for c in raw.columns if "ORDEN" in c or "UNIDADES" in c or "CANTIDAD" in c), None)
    col_lead    = next((c for c in raw.columns if "LEAD" in c), None)

    missing = []
    if col_fecha_c is None: missing.append("FECHA")
    if col_sku     is None: missing.append("SKU/MATERIAL")
    if col_ord     is None: missing.append("ORDEN/UNIDADES")
    if col_lead    is None: missing.append("LEAD_TIME")
    if missing:
        st.error(f"No se encontraron columnas: {', '.join(missing)}. "
                 f"Columnas del archivo: {raw.columns.tolist()}")
        return pd.DataFrame()

    df = raw.copy()
    df["SKU_PRODUCTO"]   = df[col_sku].astype(str).str.strip().str.upper()
    df["UNIDADES"]       = pd.to_numeric(df[col_ord],  errors="coerce").fillna(0)
    df["LEAD_TIME_DIAS"] = pd.to_numeric(df[col_lead], errors="coerce").fillna(0)

    df["FECHA_PEDIDO"]   = pd.to_datetime(df[col_fecha_c], errors="coerce")
    df["FECHA_ENTREGA"]  = df["FECHA_PEDIDO"] + pd.to_timedelta(df["LEAD_TIME_DIAS"], unit="D")
    df["PERIODO_SORT"]   = df["FECHA_ENTREGA"].apply(
        lambda x: _periodo_sort(x) if pd.notna(x) else "999999"
    )
    df["PERIODO_ENTREGA"] = df["FECHA_ENTREGA"].apply(
        lambda x: _periodo_label(x) if pd.notna(x) else "Sin Fecha"
    )

    rename_map = {
        col_fecha_c: "FECHA_PEDIDO",
        col_sku:     "SKU_PRODUCTO",
        col_ord:     "UNIDADES",
        col_lead:    "LEAD_TIME_DIAS",
    }
    for orig, std in rename_map.items():
        if orig and orig != std and orig in df.columns:
            df = df.drop(columns=[orig], errors="ignore")

    return df


# ============================================================================
# MAESTRA ENRICHMENT
# ============================================================================

_MAESTRA_COLS = [
    "SKU_PRODUCTO", "SKU_NOM_PRODUCTO",
    "PROVEEDOR", "COD_PROVEEDOR",
    "AREA", "LINEA", "SUBLINEA", "MARCA", "MODELO",
    "PROCEDENCIA", "MIX_OFICIAL",
    "ULTIMO_COSTO", "COSTO_FOB_USD", "FACTOR_IMPORTACION",
    "DENSIDAD",
]


@st.cache_data(ttl=600, show_spinner="Cargando maestra de productos...")
def _load_maestra(_conn):
    try:
        m = pd.read_sql(QUERY_MAESTRA, _conn)
        m = norm_cols(m)
        m["SKU_PRODUCTO"] = m["SKU_PRODUCTO"].astype(str).str.strip().str.upper()
        for c in ["ULTIMO_COSTO", "COSTO_FOB_USD", "FACTOR_IMPORTACION"]:
            if c in m.columns:
                m[c] = pd.to_numeric(m[c], errors="coerce").fillna(0)
        available = [c for c in _MAESTRA_COLS if c in m.columns]
        return m[available].drop_duplicates("SKU_PRODUCTO")
    except Exception as e:
        st.warning(f"No se pudo cargar maestra: {e}")
        return pd.DataFrame(columns=_MAESTRA_COLS)


@st.cache_data(ttl=1800, show_spinner=False)
def _load_perfil(_conn_id, _conn):
    """Load perfil summary from coo_config_sku_sucursal: stores with profile > 0 per SKU."""
    try:
        df = pd.read_sql(QUERY_PERFIL_RESUMEN, _conn)
        df.columns = [c.upper() for c in df.columns]
        for c in ["N_SUC_PERFIL", "TOTAL_PERFIL_UND"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
        return df
    except Exception:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "N_SUC_PERFIL", "TOTAL_PERFIL_UND"])


def _enrich(df: pd.DataFrame, maestra: pd.DataFrame) -> pd.DataFrame:
    """Merge maestra and compute COSTO_UNITARIO + COSTO_TOTAL."""
    if maestra.empty:
        for c in _MAESTRA_COLS[1:]:
            df[c] = "Sin Info"
        df["COSTO_UNITARIO"] = 0.0
        df["COSTO_TOTAL"] = 0.0
        return df

    df = df.merge(maestra, on="SKU_PRODUCTO", how="left")

    str_cols = ["SKU_NOM_PRODUCTO","PROVEEDOR","COD_PROVEEDOR","AREA","LINEA",
                "SUBLINEA","MARCA","MODELO","PROCEDENCIA","MIX_OFICIAL"]
    for c in str_cols:
        if c in df.columns:
            df[c] = df[c].fillna("Sin Info").astype(str)
        else:
            df[c] = "Sin Info"

    for c in ["ULTIMO_COSTO","COSTO_FOB_USD","FACTOR_IMPORTACION","DENSIDAD"]:
        if c not in df.columns:
            df[c] = 0.0
        else:
            # Handle comma-decimal text from Snowflake (e.g. "0,001104" → 0.001104)
            df[c] = pd.to_numeric(
                df[c].astype(str).str.replace(",", "."), errors="coerce"
            ).fillna(0)

    df["COSTO_UNITARIO"] = np.where(
        df["ULTIMO_COSTO"] > 0,
        df["ULTIMO_COSTO"],
        df["COSTO_FOB_USD"] * df["FACTOR_IMPORTACION"].replace(0, 1) * 950,
    )
    df["COSTO_TOTAL"] = df["UNIDADES"] * df["COSTO_UNITARIO"]
    df["CBM"] = df["UNIDADES"] * df["DENSIDAD"]
    return df


# ============================================================================
# COLOR PALETTE FOR STACKED BARS
# ============================================================================
_STACK_PALETTE = [
    COLORS["primary"], COLORS["tertiary_blue"], COLORS["secondary"],
    COLORS["tertiary_teal"], COLORS["tertiary_pink"],
    COLORS["status_on_track"], COLORS["status_at_risk"],
    COLORS["status_critical"], "#8B5CF6", "#06B6D4",
    "#F59E0B", "#10B981", "#EF4444", "#6366F1", "#EC4899",
    "#14B8A6", "#F97316", "#8B5CF6", "#06B6D4", "#84CC16",
]


# ============================================================================
# MAIN RENDER
# ============================================================================

def render_resumen_compra(conn):
    """Render standalone purchase summary — upload CSV, enrich with maestra."""

    st.markdown("### Resumen de Compra Proyectada")
    st.caption("Carga el archivo CSV de compra o usa el archivo guardado. "
               "La fecha de pedido sera hoy y la fecha de entrega se calcula con el lead time del archivo.")

    # ── Pre-loaded file detection ──────────────────────────────────────────
    _saved_info = get_saved_file_info("compra")
    _has_saved = _saved_info is not None

    if _has_saved:
        with st.expander("📂 Archivo de compra guardado", expanded=False):
            st.markdown(
                f"**{_saved_info.get('filename', 'compra.csv')}** — "
                f"subido por *{_saved_info.get('uploaded_by_name', '?')}* "
                f"el {_saved_info.get('uploaded_at', '?')[:10]} — "
                f"{_saved_info.get('size_bytes', 0) / 1024:.0f} KB"
            )
        use_saved = st.checkbox(
            "Usar archivo guardado (sin subir nuevo)",
            value=True,
            key="rc_use_saved",
        )
    else:
        use_saved = False

    if not use_saved:
        f_compra = st.file_uploader(
            "Cargar archivo de Compra (CSV)",
            type=["csv"],
            key="rc_file",
        )
    else:
        f_compra = None

    # ── Resolve effective file ─────────────────────────────────────────────
    if use_saved and _has_saved:
        eff_compra = get_saved_file_path("compra")
    elif f_compra is not None:
        eff_compra = f_compra
        # Save uploaded file for future reuse
        try:
            _cu = get_current_user()
            if _cu:
                save_input_file("compra", f_compra, _cu)
        except Exception:
            pass
    else:
        eff_compra = None

    if eff_compra is None:
        st.info("Sube el archivo CSV de compra para comenzar, "
                "o usa el archivo guardado si ya se subió antes.")
        return

    df_raw = _process_csv(eff_compra)
    if df_raw.empty:
        return

    maestra = _load_maestra(conn)
    df = _enrich(df_raw, maestra)
    df = apply_pm_filter(df)

    # Merge perfil (sucursales con perfil y total unidades)
    perfil = _load_perfil(id(conn), conn)
    if not perfil.empty:
        df = df.merge(perfil, on="SKU_PRODUCTO", how="left")
    df["N_SUC_PERFIL"] = df.get("N_SUC_PERFIL", pd.Series(0, index=df.index)).fillna(0).astype(int)
    df["TOTAL_PERFIL_UND"] = df.get("TOTAL_PERFIL_UND", pd.Series(0, index=df.index)).fillna(0).astype(int)

    if df.empty:
        st.warning("No se pudieron procesar los datos.")
        return

    df["UNIDADES"]     = pd.to_numeric(df["UNIDADES"],     errors="coerce").fillna(0)
    df["COSTO_TOTAL"]  = pd.to_numeric(df["COSTO_TOTAL"],  errors="coerce").fillna(0)

    # ── KPI Cards ─────────────────────────────────────────────────────────
    total_ord  = len(df)
    total_und  = df["UNIDADES"].sum()
    total_cost = df["COSTO_TOTAL"].sum()
    n_skus     = df["SKU_PRODUCTO"].nunique()
    n_provs    = df["PROVEEDOR"].nunique() if "PROVEEDOR" in df.columns else 0
    n_periodos = df["PERIODO_SORT"].nunique()

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Ordenes", f"{total_ord:,}")
    k2.metric("Unidades", f"{total_und:,.0f}")
    k3.metric("Costo Total", _fmt_mm(total_cost))
    k4.metric("SKUs", f"{n_skus:,}")
    k5.metric("Proveedores", f"{n_provs:,}")
    k6.metric("Periodos", f"{n_periodos}")

    st.divider()

    # ── Controls ──────────────────────────────────────────────────────────
    ctrl1, ctrl2 = st.columns([2, 1])
    with ctrl1:
        sel_dims = st.multiselect(
            "Agrupar por",
            list(_DIM_OPTIONS.keys()),
            default=["Proveedor"],
            key="rc_dims",
        )
        if not sel_dims:
            sel_dims = ["Proveedor"]
    with ctrl2:
        metric_mode = st.radio("Mostrar en", ["Unidades", "Costo ($)"],
                               horizontal=True, key="rc_metric")

    # ── Cascading filters ─────────────────────────────────────────────────
    _m = df.copy()

    # Row 1: Area → Linea → Sublinea → Marca
    f1, f2, f3, f4 = st.columns(4)

    with f1:
        areas = ["Todos"] + sorted(_m["AREA"].unique())
        sel_area = st.selectbox("Area", areas, key="rc_area")
    if sel_area != "Todos":
        _m = _m[_m["AREA"] == sel_area]

    with f2:
        lineas = ["Todos"] + sorted(_m["LINEA"].unique())
        sel_linea = st.selectbox("Linea", lineas, key="rc_linea")
    if sel_linea != "Todos":
        _m = _m[_m["LINEA"] == sel_linea]

    with f3:
        if "SUBLINEA" in _m.columns:
            sublineas = ["Todos"] + sorted(_m["SUBLINEA"].unique())
        else:
            sublineas = ["Todos"]
        sel_sublinea = st.selectbox("Sublinea", sublineas, key="rc_sublinea")
    if sel_sublinea != "Todos" and "SUBLINEA" in _m.columns:
        _m = _m[_m["SUBLINEA"] == sel_sublinea]

    with f4:
        marcas = ["Todos"] + sorted(_m["MARCA"].unique())
        sel_marca = st.selectbox("Marca", marcas, key="rc_marca")
    if sel_marca != "Todos":
        _m = _m[_m["MARCA"] == sel_marca]

    # Row 2: Modelo, Proveedor, Procedencia, Mix Oficial
    f5, f6, f7, f8 = st.columns(4)

    with f5:
        if "MODELO" in _m.columns:
            modelos = ["Todos"] + sorted(_m["MODELO"].unique())
        else:
            modelos = ["Todos"]
        sel_modelo = st.selectbox("Modelo", modelos, key="rc_modelo")
    if sel_modelo != "Todos" and "MODELO" in _m.columns:
        _m = _m[_m["MODELO"] == sel_modelo]

    with f6:
        provs = ["Todos"] + sorted(_m["PROVEEDOR"].unique())
        sel_prov = st.selectbox("Proveedor", provs, key="rc_prov")
    if sel_prov != "Todos":
        _m = _m[_m["PROVEEDOR"] == sel_prov]

    with f7:
        if "PROCEDENCIA" in _m.columns:
            procs = ["Todos"] + sorted(_m["PROCEDENCIA"].unique())
        else:
            procs = ["Todos"]
        sel_proc = st.selectbox("Procedencia", procs, key="rc_proc")
    if sel_proc != "Todos" and "PROCEDENCIA" in _m.columns:
        _m = _m[_m["PROCEDENCIA"] == sel_proc]

    with f8:
        if "MIX_OFICIAL" in _m.columns:
            mixes = ["Todos"] + sorted(_m["MIX_OFICIAL"].unique())
        else:
            mixes = ["Todos"]
        sel_mix = st.selectbox("Mix Oficial", mixes, key="rc_mix")
    if sel_mix != "Todos" and "MIX_OFICIAL" in _m.columns:
        _m = _m[_m["MIX_OFICIAL"] == sel_mix]

    # Row 3: SKU (contiene), Nombre Producto (contiene)
    f9, f10 = st.columns(2)

    with f9:
        sku_filter = st.text_input("SKU (contiene)", key="rc_sku_filter",
                                   placeholder="Ej: 013910")
    if sku_filter:
        _m = _m[_m["SKU_PRODUCTO"].str.contains(
            sku_filter.strip().upper(), case=False, na=False)]

    with f10:
        nom_filter = st.text_input("Nombre Producto (contiene)", key="rc_nom_filter",
                                   placeholder="Ej: coche")
    if nom_filter and "SKU_NOM_PRODUCTO" in _m.columns:
        _m = _m[_m["SKU_NOM_PRODUCTO"].str.contains(
            nom_filter.strip(), case=False, na=False)]

    if _m.empty:
        st.warning("No hay datos con los filtros seleccionados.")
        return

    # ── Build group columns ───────────────────────────────────────────────
    group_cols = [_DIM_OPTIONS[d] for d in sel_dims if _DIM_OPTIONS[d] in _m.columns]
    if not group_cols:
        group_cols = ["PROVEEDOR"]

    if "SKU_PRODUCTO" in group_cols and "SKU_NOM_PRODUCTO" in _m.columns:
        if "SKU_NOM_PRODUCTO" not in group_cols:
            idx = group_cols.index("SKU_PRODUCTO")
            group_cols.insert(idx + 1, "SKU_NOM_PRODUCTO")

    val_col  = "UNIDADES"  if metric_mode == "Unidades" else "COSTO_TOTAL"
    y_title  = "Unidades"  if metric_mode == "Unidades" else "Costo ($)"

    # ── Chart 1: Compra por Periodo ───────────────────────────────────────
    st.markdown("#### Compra por Periodo de Entrega")

    sorted_periods = sorted(_m["PERIODO_SORT"].unique())
    period_label_map = (
        _m[["PERIODO_SORT", "PERIODO_ENTREGA"]]
        .drop_duplicates()
        .set_index("PERIODO_SORT")["PERIODO_ENTREGA"]
        .to_dict()
    )
    period_order = [period_label_map[p] for p in sorted_periods]

    stk_dim = group_cols[0]

    if stk_dim == "SKU_PRODUCTO" and "SKU_NOM_PRODUCTO" in _m.columns:
        _m["_STK_LABEL"] = _m["SKU_PRODUCTO"].astype(str) + " | " + _m["SKU_NOM_PRODUCTO"].astype(str)
    else:
        _m["_STK_LABEL"] = _m[stk_dim].astype(str)

    if len(sorted_periods) > 1:
        # Stacked bar chart by period
        agg_chart = _m.groupby(["_STK_LABEL", "PERIODO_ENTREGA"], as_index=False)[val_col].sum()
        if not agg_chart.empty:
            labels = agg_chart["_STK_LABEL"].unique().tolist()

            fig1 = go.Figure(layout=dorel_layout(
                height=400,
                xaxis=dict(title="Periodo Entrega", categoryorder="array", categoryarray=period_order),
                yaxis=dict(title=y_title),
                barmode="stack",
                legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="center", x=0.5,
                            font=dict(size=10)),
            ))
            for i, label in enumerate(labels[:20]):  # limit legend items
                sub = agg_chart[agg_chart["_STK_LABEL"] == label]
                fig1.add_trace(go.Bar(
                    x=sub["PERIODO_ENTREGA"], y=sub[val_col],
                    name=str(label)[:40],
                    marker_color=_STACK_PALETTE[i % len(_STACK_PALETTE)],
                    hovertemplate=(
                        f"{stk_dim}: " + str(label)[:40] + "<br>"
                        "Periodo: %{x}<br>"
                        f"{y_title}: " + "%{y:,.0f}<extra></extra>"
                    ),
                ))
            st.plotly_chart(fig1, use_container_width=True)
    else:
        # Single period: horizontal bar
        agg_chart = _m.groupby("_STK_LABEL", as_index=False)[val_col].sum()
        agg_chart = agg_chart.nlargest(30, val_col)
        if not agg_chart.empty:
            periodo_actual = period_order[0] if period_order else ""
            agg_chart = agg_chart.sort_values(val_col, ascending=True)

            fig1 = go.Figure(layout=dorel_layout(
                height=max(300, min(30, len(agg_chart)) * 28),
                xaxis=dict(title=y_title),
                yaxis=dict(title=""),
                margin=dict(l=200, r=20, t=30, b=40),
                showlegend=False,
            ))
            fig1.add_trace(go.Bar(
                x=agg_chart[val_col], y=agg_chart["_STK_LABEL"],
                orientation="h", marker_color=COLORS["primary"],
                hovertemplate=f"{stk_dim}: " + "%{y}<br>" + f"{y_title}: " + "%{x:,.0f}<extra></extra>",
            ))
            st.caption(f"Periodo de entrega: **{periodo_actual}** \u2014 Top {len(agg_chart)} grupos")
            st.plotly_chart(fig1, use_container_width=True)

    st.divider()

    # ── Pivot table ───────────────────────────────────────────────────────
    st.markdown("#### Tabla Pivot por Periodo")

    pivot_agg = _m.groupby(group_cols + ["PERIODO_SORT", "PERIODO_ENTREGA"], as_index=False)[val_col].sum()

    pivot = pivot_agg.pivot_table(
        index=group_cols,
        columns="PERIODO_SORT",
        values=val_col,
        aggfunc="sum",
        fill_value=0,
    )
    pivot = pivot[sorted_periods]
    pivot.columns = [period_label_map.get(c, c) for c in pivot.columns]
    pivot["TOTAL"] = pivot.sum(axis=1)
    pivot = pivot.sort_values("TOTAL", ascending=False).reset_index()

    total_row = {c: "TOTAL" if c in group_cols else pivot[c].sum() for c in pivot.columns}
    pivot = pd.concat([pivot, pd.DataFrame([total_row])], ignore_index=True)

    num_cols_pivot = [c for c in pivot.columns if c not in group_cols]

    if metric_mode == "Unidades":
        col_cfg = {c: st.column_config.NumberColumn(c, format="%,.0f")
                   for c in num_cols_pivot}
        pivot_display = pivot
    else:
        # CLP monetary: format as Chilean text ($25.450)
        pivot_display = pivot.copy()
        for c in num_cols_pivot:
            pivot_display[c] = pivot_display[c].apply(fmt_clp)
        col_cfg = {c: st.column_config.TextColumn(c)
                   for c in num_cols_pivot}

    st.dataframe(
        pivot_display, use_container_width=True, hide_index=True,
        column_config=col_cfg,
        height=min(500, 45 + len(pivot) * 35),
    )
    download_buttons(pivot, prefix="resumen_compra_pivot")

    st.divider()

    # ── Chart 2: Top N by total ───────────────────────────────────────────
    st.markdown("#### Top Grupos por Monto Total")

    pivot_no_total = pivot[pivot[group_cols[0]] != "TOTAL"].copy()

    label_cols = group_cols.copy()
    if len(label_cols) == 1:
        pivot_no_total["_LABEL"] = pivot_no_total[label_cols[0]].astype(str)
    else:
        pivot_no_total["_LABEL"] = pivot_no_total[label_cols].astype(str).agg(" | ".join, axis=1)

    top_n  = min(20, len(pivot_no_total))
    top_df = pivot_no_total.nlargest(top_n, "TOTAL")[["_LABEL", "TOTAL"]]

    if not top_df.empty:
        top_df = top_df.sort_values("TOTAL", ascending=True)

        fig2 = go.Figure(layout=dorel_layout(
            height=max(250, top_n * 28),
            xaxis=dict(title=y_title),
            yaxis=dict(title=""),
            margin=dict(l=200, r=20, t=30, b=40),
            showlegend=False,
        ))
        fig2.add_trace(go.Bar(
            x=top_df["TOTAL"], y=top_df["_LABEL"],
            orientation="h", marker_color=COLORS["primary"],
            hovertemplate="Grupo: %{y}<br>" + f"{y_title}: " + "%{x:,.0f}<extra></extra>",
        ))
        st.plotly_chart(fig2, use_container_width=True)

    st.divider()

    # ── Sabana completa ───────────────────────────────────────────────────
    st.markdown("#### Sabana Completa")

    sabana_col_order = [
        "SKU_PRODUCTO", "SKU_NOM_PRODUCTO",
        "PROVEEDOR", "COD_PROVEEDOR",
        "AREA", "LINEA", "SUBLINEA", "MARCA", "MODELO",
        "PROCEDENCIA", "MIX_OFICIAL",
        "FECHA_PEDIDO", "LEAD_TIME_DIAS", "FECHA_ENTREGA", "PERIODO_ENTREGA",
        "UNIDADES", "DENSIDAD", "CBM", "COSTO_UNITARIO", "COSTO_TOTAL",
        "ULTIMO_COSTO", "COSTO_FOB_USD", "FACTOR_IMPORTACION",
        "N_SUC_PERFIL", "TOTAL_PERFIL_UND",
    ]
    sabana_cols = [c for c in sabana_col_order if c in _m.columns]
    extra_orig = [c for c in _m.columns if c not in sabana_cols
                  and c not in ("PERIODO_SORT", "_STK_LABEL")]
    sabana_cols += extra_orig

    sort_by = [c for c in ["PERIODO_SORT", "PROVEEDOR", "SKU_PRODUCTO"] if c in _m.columns]
    sabana = _m.sort_values(sort_by)[sabana_cols].reset_index(drop=True)

    # Format CLP monetary columns as Chilean format text ($25.450)
    sabana_display = sabana.copy()
    for c in ["COSTO_UNITARIO", "COSTO_TOTAL", "ULTIMO_COSTO"]:
        if c in sabana_display.columns:
            sabana_display[c] = sabana_display[c].apply(fmt_clp)

    st.dataframe(
        sabana_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "UNIDADES":          st.column_config.NumberColumn("Unidades",      format="%,.0f"),
            "DENSIDAD":          st.column_config.NumberColumn("Densidad",      format="%.4f"),
            "CBM":               st.column_config.NumberColumn("CBM",           format="%,.2f"),
            "COSTO_UNITARIO":    st.column_config.TextColumn("Costo Unit."),
            "COSTO_TOTAL":       st.column_config.TextColumn("Costo Total"),
            "ULTIMO_COSTO":      st.column_config.TextColumn("Ult. Costo"),
            "COSTO_FOB_USD":     st.column_config.NumberColumn("FOB (USD)",     format="$%,.2f"),
            "FACTOR_IMPORTACION":st.column_config.NumberColumn("Factor Import.", format="%.2f"),
            "N_SUC_PERFIL":      st.column_config.NumberColumn("Suc. c/Perfil", format="%,.0f"),
            "TOTAL_PERFIL_UND":  st.column_config.NumberColumn("Perfil Total (Und)", format="%,.0f"),
            "LEAD_TIME_DIAS":    st.column_config.NumberColumn("Lead Time (d)", format="%,.0f"),
            "FECHA_PEDIDO":      st.column_config.DateColumn("Fecha Pedido",    format="DD/MM/YYYY"),
            "FECHA_ENTREGA":     st.column_config.DateColumn("Fecha Entrega",   format="DD/MM/YYYY"),
        },
    )

    download_buttons(sabana, prefix="sabana_compra")
