"""
Pasillo Infinito — Análisis de venta por pasillo infinito y oportunidades de perfil.

Cruza ventas de pasillo infinito (ft_venta_pasillo_infinito) con el perfil de
tiendas (coo_config_sku_sucursal) para detectar pares SKU×Tienda que venden
recurrentemente por PI pero no están configurados en perfil.
"""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from db.queries import QUERY_PASILLO_INFINITO, QUERY_SYNCRO_CONFIG, QUERY_SYNCRO_SUCURSAL, QUERY_DT_TIENDA
from db.cache import cached_query as cq, TTL_DIARIO
from utils.filters import norm_cols, human_format
from utils.ui_animations import lottie_spinner
from utils.export import download_buttons
from config import COLORS, dorel_layout, apply_pm_filter


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_mm(v):
    """Formato millones con 1 decimal."""
    if pd.isna(v) or v == 0:
        return "—"
    if abs(v) >= 1_000_000_000:
        return f"${v / 1_000_000_000:.1f}B"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    return f"${v:,.0f}"


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _load_pi(_conn_id, _conn=None):
    """Carga ventas de pasillo infinito — cached 24h."""
    df = pd.read_sql(QUERY_PASILLO_INFINITO, _conn)
    return norm_cols(df)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _load_config_sku(_conn_id, _conn=None):
    """Carga config SKU×Sucursal — cached 24h."""
    df = pd.read_sql(QUERY_SYNCRO_CONFIG, _conn)
    return norm_cols(df)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _load_sucursales(_conn_id, _conn=None):
    """Carga maestro de sucursales — cached 24h."""
    df = pd.read_sql(QUERY_SYNCRO_SUCURSAL, _conn)
    return norm_cols(df)


@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _load_tiendas_dim(_conn_id, _conn=None):
    """Carga dimension tiendas con flag activa — cached 24h."""
    df = pd.read_sql(QUERY_DT_TIENDA, _conn)
    return norm_cols(df)


def _analyze_pi(df_pi, df_config, maestra, sucursales, df_tiendas_dim=None):
    """
    Enriquece y agrega ventas PI por SKU×Tienda.

    Filters:
      - Solo SKUs con MIX_OFICIAL = 'MIX' (portafolio activo)
      - Solo tiendas activas (Status = 'Abierta' en dv_tienda)

    Returns DataFrame con métricas por par y flag EN_PERFIL.
    """
    # ── Enriquecer con maestra ──
    m_cols = ["SKU_PRODUCTO"]
    for c in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA"]:
        if c in maestra.columns:
            m_cols.append(c)
    m_dedup = maestra[m_cols].drop_duplicates("SKU_PRODUCTO")
    df = df_pi.merge(m_dedup, on="SKU_PRODUCTO", how="left")

    # ── Filtrar solo SKUs MIX (portafolio activo) ──
    if "MIX_OFICIAL" in maestra.columns:
        mix_skus = set(maestra.loc[
            maestra["MIX_OFICIAL"].astype(str).str.strip().str.upper() == "MIX",
            "SKU_PRODUCTO",
        ])
        if mix_skus:
            df = df[df["SKU_PRODUCTO"].isin(mix_skus)].copy()

    # ── Filtrar solo tiendas activas ──
    if df_tiendas_dim is not None and not df_tiendas_dim.empty:
        if "ID_SUCURSAL" in df_tiendas_dim.columns and "ACTIVA" in df_tiendas_dim.columns:
            activas = set(df_tiendas_dim.loc[
                df_tiendas_dim["ACTIVA"] == True, "ID_SUCURSAL"
            ])
            if activas:
                df = df[df["COD_BODEGA"].isin(activas)].copy()

    # ── Enriquecer con sucursales ──
    if "ID_SUCURSAL" in sucursales.columns and "DESCRIPCION_SUCURSAL" in sucursales.columns:
        suc_map = sucursales.drop_duplicates("ID_SUCURSAL")[
            ["ID_SUCURSAL", "DESCRIPCION_SUCURSAL"]
        ].copy()
        suc_map = suc_map.rename(columns={"ID_SUCURSAL": "COD_BODEGA"})
        df = df.merge(suc_map, on="COD_BODEGA", how="left")
    else:
        df["DESCRIPCION_SUCURSAL"] = df["COD_BODEGA"]

    # ── Coerción numérica ──
    for c in ["CANTIDAD", "MONTO_NETO", "MONTO_TOTAL"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    df["FECHA"] = pd.to_datetime(df["FECHA"], errors="coerce")
    df["MES"] = df["FECHA"].dt.to_period("M")

    # ── Agregar por SKU × Tienda ──
    agg = df.groupby(["SKU_PRODUCTO", "COD_BODEGA"], as_index=False).agg(
        N_TRANSACCIONES=("FECHA", "count"),
        CANTIDAD_TOTAL=("CANTIDAD", "sum"),
        VENTA_NETA_TOTAL=("MONTO_NETO", "sum"),
        VENTA_BRUTA_TOTAL=("MONTO_TOTAL", "sum"),
        PRIMERA_VENTA=("FECHA", "min"),
        ULTIMA_VENTA=("FECHA", "max"),
        MESES_ACTIVOS=("MES", "nunique"),
    )

    # ── Merge dimensiones (first row per SKU) ──
    dim_cols = ["SKU_PRODUCTO", "COD_BODEGA"]
    for c in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA", "DESCRIPCION_SUCURSAL"]:
        if c in df.columns:
            dim_cols.append(c)
    dims = df[dim_cols].drop_duplicates(["SKU_PRODUCTO", "COD_BODEGA"])
    agg = agg.merge(dims, on=["SKU_PRODUCTO", "COD_BODEGA"], how="left")

    # ── Cruce con config (¿está en perfil?) ──
    if not df_config.empty:
        # Normalizar nombres para el join
        cfg = df_config.copy()
        cfg_join_cols = {}
        if "ID_MATERIAL" in cfg.columns:
            cfg_join_cols["ID_MATERIAL"] = "SKU_PRODUCTO"
        if "ID_SUCURSAL" in cfg.columns:
            cfg_join_cols["ID_SUCURSAL"] = "COD_BODEGA"

        if cfg_join_cols:
            cfg = cfg.rename(columns=cfg_join_cols)
            cfg_cols = ["SKU_PRODUCTO", "COD_BODEGA"]
            if "MIN_INV_REQUERIDO" in cfg.columns:
                cfg_cols.append("MIN_INV_REQUERIDO")
                cfg["MIN_INV_REQUERIDO"] = pd.to_numeric(
                    cfg["MIN_INV_REQUERIDO"], errors="coerce"
                ).fillna(0)
            if "MAX_REPO" in cfg.columns:
                cfg_cols.append("MAX_REPO")
                cfg["MAX_REPO"] = pd.to_numeric(cfg["MAX_REPO"], errors="coerce").fillna(0)

            cfg = cfg[cfg_cols].drop_duplicates(["SKU_PRODUCTO", "COD_BODEGA"])
            cfg["_EN_PERFIL"] = True

            agg = agg.merge(cfg, on=["SKU_PRODUCTO", "COD_BODEGA"], how="left")
            agg["EN_PERFIL"] = agg["_EN_PERFIL"].fillna(False)
            agg.drop(columns=["_EN_PERFIL"], inplace=True)
        else:
            agg["EN_PERFIL"] = False
    else:
        agg["EN_PERFIL"] = False

    if "MIN_INV_REQUERIDO" not in agg.columns:
        agg["MIN_INV_REQUERIDO"] = 0
    if "MAX_REPO" not in agg.columns:
        agg["MAX_REPO"] = 0

    # ── Score de oportunidad (para priorizar) ──
    agg["SCORE"] = agg["N_TRANSACCIONES"] * agg["VENTA_NETA_TOTAL"]

    return agg.sort_values("VENTA_NETA_TOTAL", ascending=False)


# ─── Render ───────────────────────────────────────────────────────────────────

def render_pasillo_infinito(conn):
    """Módulo Pasillo Infinito — análisis de oportunidades de perfil."""
    st.html(
        "<h2 class='sub-header'>Pasillo Infinito</h2>"
    )
    st.caption(
        "Análisis de ventas por pasillo infinito. Detecta pares SKU×Tienda con venta "
        "recurrente que **no están en perfil** — candidatos para agregar stock físico."
    )

    # ── Refresh ──
    if st.button("🔄 Actualizar Datos", key="btn_refresh_pi"):
        st.session_state.pop("pi_data", None)
        st.cache_data.clear()
        st.rerun()

    # ── Load ──
    if "pi_data" not in st.session_state:
        _cid = id(conn)
        with lottie_spinner("snowflake"):
            df_pi = _load_pi(_cid, _conn=conn)
            df_config = _load_config_sku(_cid, _conn=conn)
            sucursales = _load_sucursales(_cid, _conn=conn)
            maestra = cq.maestra(conn)
            df_tiendas_dim = _load_tiendas_dim(_cid, _conn=conn)

        if df_pi.empty:
            st.warning("No se encontraron datos de pasillo infinito.")
            return

        df_agg = _analyze_pi(df_pi, df_config, maestra, sucursales, df_tiendas_dim)
        st.session_state["pi_data"] = df_agg
        st.session_state["pi_raw"] = df_pi
        st.session_state["pi_tiendas_dim"] = df_tiendas_dim

    df = st.session_state["pi_data"].copy()
    df = apply_pm_filter(df)
    df_raw = st.session_state.get("pi_raw", pd.DataFrame())

    if df.empty:
        st.warning("No hay datos para mostrar.")
        return

    # ── Info de scope ──
    _n_skus_scope = df["SKU_PRODUCTO"].nunique()
    _n_tiendas_scope = df["COD_BODEGA"].nunique()
    st.caption(
        f"Analizando **{_n_skus_scope:,}** SKUs MIX en **{_n_tiendas_scope:,}** tiendas activas"
    )

    # ── Filtros ──
    st.markdown("### Filtros")
    fc1, fc2, fc3, fc4 = st.columns(4)

    with fc1:
        areas = sorted(df["AREA"].dropna().unique().tolist()) if "AREA" in df.columns else []
        sel_area = st.multiselect("Área", areas, key="pi_area")
    with fc2:
        _base = df[df["AREA"].isin(sel_area)] if sel_area else df
        lineas = sorted(_base["LINEA"].dropna().unique().tolist()) if "LINEA" in _base.columns else []
        sel_linea = st.multiselect("Línea", lineas, key="pi_linea")
    with fc3:
        _base2 = _base[_base["LINEA"].isin(sel_linea)] if sel_linea else _base
        marcas = sorted(_base2["MARCA"].dropna().unique().tolist()) if "MARCA" in _base2.columns else []
        sel_marca = st.multiselect("Marca", marcas, key="pi_marca")
    with fc4:
        solo_sin_perfil = st.toggle(
            "Solo sin perfil",
            value=True,
            key="pi_solo_sin_perfil",
            help="Mostrar solo pares SKU×Tienda que NO están configurados en perfil.",
        )

    # Rango de fechas (basado en datos raw)
    if not df_raw.empty and "FECHA" in df_raw.columns:
        _fechas = pd.to_datetime(df_raw["FECHA"], errors="coerce").dropna()
        if not _fechas.empty:
            fc5, fc6 = st.columns(2)
            with fc5:
                fecha_desde = st.date_input(
                    "Desde",
                    value=_fechas.min().date(),
                    min_value=_fechas.min().date(),
                    max_value=_fechas.max().date(),
                    key="pi_fecha_desde",
                )
            with fc6:
                fecha_hasta = st.date_input(
                    "Hasta",
                    value=_fechas.max().date(),
                    min_value=_fechas.min().date(),
                    max_value=_fechas.max().date(),
                    key="pi_fecha_hasta",
                )

            # Refiltrar raw por fechas y recalcular
            mask_fechas = (_fechas >= pd.Timestamp(fecha_desde)) & (
                _fechas <= pd.Timestamp(fecha_hasta)
            )
            if not mask_fechas.all():
                df_raw_filt = df_raw.loc[mask_fechas]
                if not df_raw_filt.empty:
                    df_config = _load_config_sku(id(conn), _conn=conn)
                    sucursales = _load_sucursales(id(conn), _conn=conn)
                    maestra = cq.maestra(conn)
                    _td = st.session_state.get("pi_tiendas_dim", _load_tiendas_dim(id(conn), _conn=conn))
                    df = _analyze_pi(df_raw_filt, df_config, maestra, sucursales, _td)

    # Aplicar filtros dimensionales
    if sel_area:
        df = df[df["AREA"].isin(sel_area)]
    if sel_linea:
        df = df[df["LINEA"].isin(sel_linea)]
    if sel_marca:
        df = df[df["MARCA"].isin(sel_marca)]
    if solo_sin_perfil:
        df = df[~df["EN_PERFIL"]]

    if df.empty:
        st.info("No hay datos con los filtros seleccionados.")
        return

    st.markdown("---")

    # ── KPIs ──
    k1, k2, k3, k4 = st.columns(4)
    total_vn = df["VENTA_NETA_TOTAL"].sum()
    n_skus = df["SKU_PRODUCTO"].nunique()
    n_pares_sin = len(df[~df["EN_PERFIL"]])
    vn_sin_perfil = df.loc[~df["EN_PERFIL"], "VENTA_NETA_TOTAL"].sum()

    k1.metric("Venta Neta PI", _fmt_mm(total_vn))
    k2.metric("SKUs Únicos", f"{n_skus:,}")
    k3.metric("Pares Sin Perfil", f"{n_pares_sin:,}")
    k4.metric("Oportunidad $", _fmt_mm(vn_sin_perfil))

    st.html("<br>")

    # ── Charts ──
    col1, col2 = st.columns(2)

    # Chart 1: Top 20 SKUs sin perfil por venta neta
    df_sin = df[~df["EN_PERFIL"]]
    with col1:
        st.markdown("**Top 20 SKUs sin perfil — Venta Neta**")
        top_skus = (
            df_sin.groupby("SKU_PRODUCTO", as_index=False)["VENTA_NETA_TOTAL"]
            .sum()
            .nlargest(20, "VENTA_NETA_TOTAL")
            .sort_values("VENTA_NETA_TOTAL", ascending=True)
        )
        if not top_skus.empty:
            fig1 = go.Figure()
            fig1.add_trace(go.Bar(
                y=top_skus["SKU_PRODUCTO"],
                x=top_skus["VENTA_NETA_TOTAL"],
                orientation="h",
                marker_color=COLORS["primary"],
                hovertemplate="SKU: %{y}<br>Venta Neta: $%{x:,.0f}<extra></extra>",
            ))
            fig1.update_layout(**dorel_layout(
                xaxis=dict(title="Venta Neta CLP", tickformat="$~s"),
                yaxis=dict(title=""),
                height=max(len(top_skus) * 28, 200),
                margin=dict(l=120),
            ))
            st.plotly_chart(fig1, use_container_width=True)
        else:
            st.info("No hay SKUs sin perfil.")

    # Chart 2: Top 20 Tiendas con más venta PI sin perfil
    with col2:
        st.markdown("**Top 20 Tiendas sin perfil — Venta Neta**")
        lbl_col = "DESCRIPCION_SUCURSAL" if "DESCRIPCION_SUCURSAL" in df_sin.columns else "COD_BODEGA"
        top_tiendas = (
            df_sin.groupby(lbl_col, as_index=False)["VENTA_NETA_TOTAL"]
            .sum()
            .nlargest(20, "VENTA_NETA_TOTAL")
            .sort_values("VENTA_NETA_TOTAL", ascending=True)
        )
        if not top_tiendas.empty:
            fig2 = go.Figure()
            fig2.add_trace(go.Bar(
                y=top_tiendas[lbl_col],
                x=top_tiendas["VENTA_NETA_TOTAL"],
                orientation="h",
                marker_color=COLORS["tertiary_teal"],
                hovertemplate="Tienda: %{y}<br>Venta Neta: $%{x:,.0f}<extra></extra>",
            ))
            fig2.update_layout(**dorel_layout(
                xaxis=dict(title="Venta Neta CLP", tickformat="$~s"),
                yaxis=dict(title=""),
                height=max(len(top_tiendas) * 28, 200),
                margin=dict(l=180),
            ))
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("No hay tiendas sin perfil.")

    # ── Tabla detalle ──
    st.markdown("### Detalle por SKU × Tienda")
    display_cols = [
        "SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA",
        "COD_BODEGA", "DESCRIPCION_SUCURSAL",
        "N_TRANSACCIONES", "CANTIDAD_TOTAL", "VENTA_NETA_TOTAL",
        "MESES_ACTIVOS", "EN_PERFIL", "MIN_INV_REQUERIDO",
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    df_show = df[display_cols].copy()
    _rename = {
        "SKU_NOM_PRODUCTO": "Producto",
        "DESCRIPCION_SUCURSAL": "Tienda",
        "N_TRANSACCIONES": "Txns",
        "CANTIDAD_TOTAL": "Unidades",
        "VENTA_NETA_TOTAL": "Venta Neta",
        "MESES_ACTIVOS": "Meses",
        "EN_PERFIL": "En Perfil",
        "MIN_INV_REQUERIDO": "Min Inv Config",
    }
    df_show = df_show.rename(columns=_rename)

    # Formato
    fmt = {}
    if "Venta Neta" in df_show.columns:
        fmt["Venta Neta"] = "${:,.0f}"
    if "Min Inv Config" in df_show.columns:
        fmt["Min Inv Config"] = "{:.0f}"

    # Highlight sin perfil
    def _highlight_no_perfil(row):
        if "En Perfil" in row.index and not row["En Perfil"]:
            return ["background-color: #FFF3E0"] * len(row)
        return [""] * len(row)

    styler = df_show.style.format(fmt, na_rep="—").apply(_highlight_no_perfil, axis=1)

    st.dataframe(
        styler,
        use_container_width=True,
        height=min(len(df_show) * 38 + 60, 600),
    )

    download_buttons(df[display_cols], "pasillo_infinito")

    # ── Resumen por Área/Línea ──
    with st.expander("📊 Resumen por Área y Línea", expanded=False):
        dim = st.radio(
            "Agrupar por",
            ["AREA", "LINEA", "MARCA"],
            horizontal=True,
            key="pi_dim",
        )
        if dim in df.columns:
            resumen = df.groupby(dim, as_index=False).agg(
                PARES=("SKU_PRODUCTO", "count"),
                SIN_PERFIL=("EN_PERFIL", lambda x: (~x).sum()),
                VENTA_NETA=("VENTA_NETA_TOTAL", "sum"),
                SKUS=("SKU_PRODUCTO", "nunique"),
                TIENDAS=("COD_BODEGA", "nunique"),
            ).sort_values("VENTA_NETA", ascending=False)

            resumen["% SIN PERFIL"] = np.where(
                resumen["PARES"] > 0,
                (resumen["SIN_PERFIL"] / resumen["PARES"] * 100).round(1),
                0,
            )

            fmt_r = {"VENTA_NETA": "${:,.0f}", "% SIN PERFIL": "{:.1f}%"}
            st.dataframe(
                resumen.style.format(fmt_r, na_rep="—")
                .background_gradient(subset=["VENTA_NETA"], cmap="Blues"),
                use_container_width=True,
            )
