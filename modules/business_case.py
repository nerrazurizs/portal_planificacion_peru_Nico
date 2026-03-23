"""
Business Case por SKU — Análisis profundo de un SKU individual.

Módulo independiente que permite:
  - Seleccionar un SKU con filtros (MIX, línea, sublínea, marca, acción)
  - Ver gráfico de precio vs volumen + inventario vs MOI
  - KPIs de runway (MOI hist, MOI FC, meses liquidación, etc.)
  - Diagnóstico completo: MIX, antigüedad, canales, penetración tiendas,
    elasticidad, escenario descuento, riesgo quiebre, sobrestock estructural
  - Generación PPT por línea y descarga masiva por PM
"""

import io
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime
from scipy import stats as _sp_stats

from db.queries import QUERY_PERFIL_RESUMEN
from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format, calcular_moi_ajustado
from utils.export import download_buttons
from config import COLORS, dorel_layout, apply_pm_filter


# ═══════════════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════════════

def _fmt_mm(v):
    """Format millions with 1 decimal."""
    if pd.isna(v) or v == 0:
        return "—"
    if abs(v) >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f}B"
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    return f"${v:,.0f}"


def _classify_age_band(age):
    """Classify antigüedad into age bands for stock health segmentation."""
    if pd.isna(age):
        return "Sin Dato", "#9ca3af"
    age = float(age)
    if age < 3:
        return "Nuevo (<3m)", "#10b981"
    if age < 6:
        return "Reciente (3-6m)", "#0ea5e9"
    if age < 12:
        return "Intermedio (6-12m)", "#f59e0b"
    if age < 24:
        return "Maduro (12-24m)", "#FB8C00"
    return "Longevo (>24m)", "#ef4444"


_AGE_BAND_ORDER = [
    "Nuevo (<3m)", "Reciente (3-6m)", "Intermedio (6-12m)",
    "Maduro (12-24m)", "Longevo (>24m)", "Sin Dato",
]


def _classify_sku_action(moi, age):
    """Classify SKU health and recommended purchase action.

    Age-tiered logic:
    - New (<3m): High MOI is expected for new products — show as "Nuevo" states, not critical.
    - Growing (3-5m): Apply tighter thresholds — MOI ≥ 9 already "Riesgo Temprano".
    - Established (6m+): Standard thresholds. Age > 12m escalates urgency.
    """
    _moi_na = pd.isna(moi)
    _age_na = pd.isna(age)
    moi = float(moi) if not _moi_na else 999.0  # NaN MOI = assume worst case
    age = float(age) if not _age_na else 0.0

    # ── Nuevos (<3m): alta tolerancia al MOI alto, es esperado ──────────────
    if age < 3:
        if _moi_na or moi >= 99 or moi == 0:
            return "Nuevo Sin Rotación", "OBSERVAR", 1
        if moi >= 12:
            return "Nuevo (Alto Stock)", "MONITOREAR", 2
        if moi >= 6:
            return "Nuevo (Normal)", "OK", 3
        if moi > 0:
            return "Nuevo (Activo)", "OK", 4
        return "Nuevo Sin Rotación", "OBSERVAR", 1

    # ── Recientes (3-5m): umbrales más exigentes, flag temprano ─────────────
    if 3 <= age < 6:
        if _moi_na or moi >= 12:
            return "Riesgo Temprano", "PAUSAR", 1
        if moi >= 9:
            return "Riesgo Temprano", "PAUSAR", 2
        if moi >= 6:
            return "Sobreinventariado", "PAUSAR", 3
        if 4 <= moi < 6:
            return "Monitorear", "REDUCIR", 4
        if 2 <= moi < 4:
            return "Saludable", "OK", 5
        return "Bajo Stock", "COMPRAR MÁS", 6

    # ── Establecidos (6m+): lógica estándar con edad como agravante ──────────
    if moi >= 12 and age >= 12:
        return "Acción Urgente", "PAUSAR", 1
    if moi >= 12 or age > 12:
        return "Riesgo Obsolescencia", "PAUSAR", 2
    if _moi_na:
        return "Riesgo Obsolescencia", "PAUSAR", 2
    if 6 < moi <= 12 and age <= 12:
        return "Sobreinventariado", "PAUSAR", 3
    if 4 <= moi <= 6:
        return "Monitorear", "REDUCIR", 4
    if 2 <= moi < 4:
        return "Saludable", "OK", 5
    if 0 < moi < 2:
        return "Bajo Stock", "COMPRAR MÁS", 6
    return "Sin Info", "—", 7


def _classify_demand_action(moi, age):
    """Classify demand-side action: liquidate, markdown, or hold."""
    moi = float(moi) if pd.notna(moi) else 999.0  # NaN MOI = worst case
    age = float(age) if pd.notna(age) else 0.0
    if moi >= 12 and age >= 18:
        return "LIQUIDAR", 50
    if moi >= 12 and age >= 12:
        return "LIQUIDAR", 40
    if age > 18:
        return "LIQUIDAR", 40
    if moi > 12 or age > 12:
        return "LIQUIDAR", 30
    if 6 < moi <= 12 and age > 6:
        return "MARKDOWN", 25
    if 6 < moi <= 12:
        return "MARKDOWN", 20
    if 4 <= moi <= 6 and age > 6:
        return "MARKDOWN", 15
    return "MANTENER", 0


_ACCION_EMOJI = {
    "PAUSAR": "⛔", "REDUCIR": "⚠️", "OK": "✅",
    "COMPRAR MÁS": "🔵", "—": "—",
}
_ACCION_VENTA_EMOJI = {
    "LIQUIDAR": "🔥", "MARKDOWN": "🏷️", "MANTENER": "—",
}
_SALUD_COLOR = {
    "Acción Urgente": "#ef4444", "Riesgo Obsolescencia": "#FB8C00",
    "Riesgo Temprano": "#f97316",
    "Sobreinventariado": "#f59e0b", "Monitorear": "#0ea5e9",
    "Saludable": "#10b981", "Bajo Stock": "#2DAAFF", "Sin Info": "#9ca3af",
    # New product states
    "Nuevo Sin Rotación": "#fbbf24", "Nuevo (Alto Stock)": "#60a5fa",
    "Nuevo (Normal)": "#34d399", "Nuevo (Activo)": "#10b981",
}


# ═══════════════════════════════════════════════════════════════════════════
# Data enrichment — mirrors _enrich_df_for_ppt from plan_compras
# ═══════════════════════════════════════════════════════════════════════════

def _enrich_bc_data(df_base, sku_db, df_perfil, abc_xyz, ventas_px,
                    stock_cd_diario=None, df_tdas_vta=None,
                    filter_year=False, year_end=None):
    """Enrich stock data with all BC metrics: elasticity, MOI FC, classifications, etc."""
    df = df_base.copy()
    if df.empty:
        return df

    # Coerce numerics
    for c in ["MOI", "ANTIGUEDAD_MESES", "STOCK_COSTO", "STOCK_UNIDADES"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    if "MOI" in df.columns:
        df = df.rename(columns={"MOI": "MOI_HIST"})

    # SKU dashboard merge
    if not sku_db.empty and "SKU_PRODUCTO" in sku_db.columns:
        _db_cols = ["SKU_PRODUCTO"]
        for _dc in ["STOCK_CD_UND", "STOCK_TIENDA_UND", "STOCK_CD_CLP", "STOCK_TIENDA_CLP",
                     "TIENDAS_CON_PERFIL", "PERFIL_TOTAL", "UND_6M", "NETO_6M",
                     "APORTE_6M", "PRECIO_PROM_NETO", "ROTACION_UND_MES", "MARGEN_6M"]:
            if _dc in sku_db.columns:
                _db_cols.append(_dc)
        df = df.merge(sku_db[_db_cols].drop_duplicates("SKU_PRODUCTO"),
                       on="SKU_PRODUCTO", how="left")
        for _dc in _db_cols[1:]:
            if _dc in df.columns:
                df[_dc] = df[_dc].fillna(0)

    # Stock concentration
    _stk_cd = df.get("STOCK_CD_CLP", pd.Series(0, index=df.index))
    _stk_ti = df.get("STOCK_TIENDA_CLP", pd.Series(0, index=df.index))
    _stk_tot = _stk_cd + _stk_ti
    df["PCT_STOCK_CD"] = np.where(_stk_tot > 0, _stk_cd / _stk_tot * 100, 0)

    # Perfil override
    if not df_perfil.empty and "SKU_PRODUCTO" in df_perfil.columns:
        df = df.drop(columns=["TIENDAS_CON_PERFIL", "PERFIL_TOTAL"], errors="ignore")
        _perfil_dedup = df_perfil.drop_duplicates("SKU_PRODUCTO")
        df = df.merge(
            _perfil_dedup[["SKU_PRODUCTO", "N_SUC_PERFIL", "TOTAL_PERFIL_UND"]],
            on="SKU_PRODUCTO", how="left",
        )
        df = df.rename(columns={
            "N_SUC_PERFIL": "TIENDAS_CON_PERFIL",
            "TOTAL_PERFIL_UND": "PERFIL_TOTAL",
        })
        df["TIENDAS_CON_PERFIL"] = df["TIENDAS_CON_PERFIL"].fillna(0)
        df["PERFIL_TOTAL"] = df["PERFIL_TOTAL"].fillna(0)

    # Store penetration
    if df_tdas_vta is not None and not df_tdas_vta.empty and "SKU_PRODUCTO" in df_tdas_vta.columns:
        df = df.drop(columns=["N_TIENDAS_VENTA", "N_TIENDAS_VENTA_3M", "N_MESES_CON_VENTA"], errors="ignore")
        df = df.merge(
            df_tdas_vta[["SKU_PRODUCTO", "N_TIENDAS_VENTA", "N_TIENDAS_VENTA_3M", "N_MESES_CON_VENTA"]].drop_duplicates("SKU_PRODUCTO"),
            on="SKU_PRODUCTO", how="left",
        )
        for _tc in ["N_TIENDAS_VENTA", "N_TIENDAS_VENTA_3M", "N_MESES_CON_VENTA"]:
            df[_tc] = df[_tc].fillna(0)

    # ABC-XYZ
    if not abc_xyz.empty and "SKU_PRODUCTO" in abc_xyz.columns:
        _abc_cols = ["SKU_PRODUCTO"]
        for _ac in ["CLASE_ABC", "CLASE_XYZ", "CLASE_COMBINADA"]:
            if _ac in abc_xyz.columns:
                _abc_cols.append(_ac)
        df = df.merge(
            abc_xyz[_abc_cols].drop_duplicates("SKU_PRODUCTO"),
            on="SKU_PRODUCTO", how="left",
        )
    for _ac in ["CLASE_ABC", "CLASE_XYZ", "CLASE_COMBINADA"]:
        if _ac not in df.columns:
            df[_ac] = "—"
        df[_ac] = df[_ac].fillna("—")

    # Elasticity
    df["ELASTICIDAD"] = np.nan
    df["ELAST_SEGMENTO"] = "Sin Dato"
    if not ventas_px.empty and "SKU_PRODUCTO" in ventas_px.columns:
        _vp = ventas_px.copy()
        for _vc in ["CANTIDAD", "PRECIO_PROMEDIO"]:
            if _vc in _vp.columns:
                _vp[_vc] = pd.to_numeric(_vp[_vc], errors="coerce")
        _vp = _vp[
            (_vp.get("CANTIDAD", pd.Series(dtype=float)) > 0)
            & (_vp.get("PRECIO_PROMEDIO", pd.Series(dtype=float)) > 0)
        ].copy()
        if not _vp.empty:
            _vp["_LN_PX"] = np.log(_vp["PRECIO_PROMEDIO"])
            _vp["_LN_QTY"] = np.log(_vp["CANTIDAD"])
            _elast_rows = []
            for _esku, _egrp in _vp.groupby("SKU_PRODUCTO"):
                if len(_egrp) < 6:
                    continue
                try:
                    _sl, _, _rv, _pv, _ = _sp_stats.linregress(
                        _egrp["_LN_PX"].values, _egrp["_LN_QTY"].values
                    )
                    _conf = (_pv < 0.1) and (_rv ** 2 > 0.1)
                    _elast_rows.append({
                        "SKU_PRODUCTO": _esku,
                        "ELASTICIDAD": round(_sl, 2),
                        "ELAST_CONFIABLE": _conf,
                    })
                except Exception:
                    continue
            if _elast_rows:
                _df_elast = pd.DataFrame(_elast_rows)
                _e_cond = [
                    _df_elast["ELASTICIDAD"] < -1.5,
                    _df_elast["ELASTICIDAD"] < -1.0,
                    _df_elast["ELASTICIDAD"] < -0.7,
                    _df_elast["ELASTICIDAD"] < -0.3,
                    _df_elast["ELASTICIDAD"] < 0,
                ]
                _e_cho = ["Muy Elástico", "Elástico", "Unitario", "Inelástico", "Muy Inelástico"]
                _df_elast["ELAST_SEGMENTO"] = np.select(_e_cond, _e_cho, default="Anómalo")
                _df_elast.loc[~_df_elast["ELAST_CONFIABLE"], "ELAST_SEGMENTO"] = "No Confiable"
                df = df.drop(columns=["ELASTICIDAD", "ELAST_SEGMENTO"], errors="ignore")
                df = df.merge(
                    _df_elast[["SKU_PRODUCTO", "ELASTICIDAD", "ELAST_SEGMENTO"]],
                    on="SKU_PRODUCTO", how="left",
                )
                df["ELASTICIDAD"] = df["ELASTICIDAD"].fillna(np.nan)
                df["ELAST_SEGMENTO"] = df["ELAST_SEGMENTO"].fillna("Sin Dato")

    # MOI FC from projection
    _df_proy = st.session_state.get("df_proy", pd.DataFrame())
    if not _df_proy.empty and "SKU_PRODUCTO" in _df_proy.columns:
        _proy_filt = _df_proy.copy()
        if "TIPO_DATO" in _proy_filt.columns:
            _proy_filt = _proy_filt[
                _proy_filt["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])
            ]
        if filter_year and year_end and "PERIODO" in _proy_filt.columns:
            _proy_filt["PERIODO"] = pd.to_datetime(_proy_filt["PERIODO"], errors="coerce")
            _proy_filt = _proy_filt[_proy_filt["PERIODO"] <= year_end]
        _cogs_col = "COGS_RES_TOTAL" if "COGS_RES_TOTAL" in _proy_filt.columns else None
        if _cogs_col:
            _proy_filt[_cogs_col] = pd.to_numeric(_proy_filt[_cogs_col], errors="coerce").fillna(0)
            _n_months = _proy_filt["PERIODO"].nunique() if "PERIODO" in _proy_filt.columns else 1
            _n_months = max(_n_months, 1)
            _cogs_sku = (
                _proy_filt.groupby("SKU_PRODUCTO", as_index=False)[_cogs_col]
                .sum()
                .rename(columns={_cogs_col: "_COGS_FC_TOTAL"})
            )
            _cogs_sku["_COGS_FC_MENSUAL"] = _cogs_sku["_COGS_FC_TOTAL"] / _n_months
            df = df.merge(_cogs_sku[["SKU_PRODUCTO", "_COGS_FC_MENSUAL"]],
                           on="SKU_PRODUCTO", how="left")
            df["_COGS_FC_MENSUAL"] = pd.to_numeric(
                df["_COGS_FC_MENSUAL"], errors="coerce"
            ).fillna(0)
            df["MOI_FC"] = np.where(
                df["_COGS_FC_MENSUAL"] > 0,
                df["STOCK_COSTO"] / df["_COGS_FC_MENSUAL"],
                0.0,
            )
            df.drop(columns=["_COGS_FC_MENSUAL"], inplace=True)
        else:
            df["MOI_FC"] = 0.0
    else:
        df["MOI_FC"] = 0.0

    # ── MOI Ajustado por demanda censurada (función compartida) ──
    df = calcular_moi_ajustado(df, ventas_px, stock_cd_diario=stock_cd_diario, min_dias=15)
    # Map shared column names to business_case names for backward compat
    df["MOI_HIST"] = df["MOI_6M"]
    df["ROTACION_UND_MES"] = df["ROT_6M"]
    df["MOI_HIST_AJUST"] = df["MOI_AJUST"]
    df["ROT_AJUST_UND_MES"] = df["ROT_AJUST"]
    df["MOI_HIST_3M"] = df["MOI_3M"]
    df["TASA_DISPONIBILIDAD"] = df["TASA_DISP"]
    df["CONFIABILIDAD_MOI"] = df["CONFIAB_MOI"]
    df["MOI_PARA_CLASIF"] = df["MOI_CLASIF"]

    # Classifications — usar MOI ajustado cuando es confiable (≥15 días con stock)
    _classifications = df.apply(
        lambda r: _classify_sku_action(
            r.get("MOI_CLASIF", r.get("MOI_HIST", 0)),
            r.get("ANTIGUEDAD_MESES", 0),
        ),
        axis=1,
    )
    df["SALUD"] = [c[0] for c in _classifications]
    df["ACCION_RAW"] = [c[1] for c in _classifications]
    df["_ACCION_ORDER"] = [c[2] for c in _classifications]

    _demand_class = df.apply(
        lambda r: _classify_demand_action(
            r.get("MOI_CLASIF", r.get("MOI_HIST", 0)),
            r.get("ANTIGUEDAD_MESES", 0),
        ),
        axis=1,
    )
    df["ACCION_VENTA_RAW"] = [d[0] for d in _demand_class]
    df["DCTO_SUGERIDO"] = [d[1] for d in _demand_class]

    # Age band segmentation
    if "ANTIGUEDAD_MESES" in df.columns:
        _age_bands = df["ANTIGUEDAD_MESES"].apply(_classify_age_band)
        df["TRAMO_EDAD"] = [b[0] for b in _age_bands]
    else:
        df["TRAMO_EDAD"] = "Sin Dato"

    # Rotation simulation with elasticity
    _rot = pd.to_numeric(df.get("ROTACION_UND_MES", 0), errors="coerce").fillna(0)
    _elast = pd.to_numeric(df.get("ELASTICIDAD", np.nan), errors="coerce")
    _dcto = pd.to_numeric(df.get("DCTO_SUGERIDO", 0), errors="coerce").fillna(0)
    _elast_abs = _elast.abs().fillna(1.5)
    df["ROT_SIM"] = _rot * (1 + _elast_abs * _dcto / 100)
    _st_u = pd.to_numeric(df.get("STOCK_UNIDADES", 0), errors="coerce").fillna(0)
    df["MESES_LIQ"] = np.where(
        df["ROT_SIM"] > 0, _st_u / df["ROT_SIM"], 999,
    )
    df["MESES_LIQ"] = np.clip(df["MESES_LIQ"], 0, 36)
    _mg = pd.to_numeric(df.get("MARGEN_6M", 0), errors="coerce").fillna(0)
    _px = pd.to_numeric(df.get("PRECIO_PROM_NETO", 0), errors="coerce").fillna(0)
    df["MG_SIM"] = np.where(
        _px > 0, _mg - _dcto / 100 * (1 - _mg), 0,
    )

    # MIX label
    if "MIX_OFICIAL" in df.columns:
        df["TIPO"] = df["MIX_OFICIAL"].fillna("").astype(str).str.strip()
        df["TIPO"] = df["TIPO"].replace({"": "Sin Definir", "nan": "Sin Definir",
                                          "None": "Sin Definir"})
    else:
        df["TIPO"] = "Sin Definir"

    df = df.sort_values(
        ["_ACCION_ORDER", "STOCK_COSTO"],
        ascending=[True, False],
    ).reset_index(drop=True)

    return df


# ═══════════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════════

def _load_bc_data(conn):
    """Load all data sources needed for the Business Case module.

    Returns dict with all dataframes, loading with spinners.
    """
    data = {}

    with st.spinner("Cargando datos de stock..."):
        try:
            _raw = norm_cols(cq.stock_critico_metrics(conn))
            # Keep only latest snapshot per SKU
            if not _raw.empty and "FECHA" in _raw.columns:
                _raw["FECHA"] = pd.to_datetime(_raw["FECHA"], errors="coerce")
                _latest = _raw.groupby("SKU_PRODUCTO", as_index=False)["FECHA"].max()
                _raw = _raw.merge(_latest, on=["SKU_PRODUCTO", "FECHA"], how="inner")
            data["stock_detail"] = _raw
        except Exception as e:
            data["stock_detail"] = pd.DataFrame()
            st.toast(f"⚠️ Error cargando stock: {e}", icon="⚠️")

    with st.spinner("Cargando métricas SKU..."):
        try:
            data["sku_db"] = norm_cols(cq.sku_dashboard(conn))
        except Exception:
            data["sku_db"] = pd.DataFrame()

    with st.spinner("Cargando perfil tiendas..."):
        try:
            data["df_perfil"] = norm_cols(pd.read_sql(QUERY_PERFIL_RESUMEN, conn))
        except Exception:
            data["df_perfil"] = pd.DataFrame()

    with st.spinner("Cargando primera venta..."):
        try:
            data["primera_venta"] = norm_cols(cq.primera_venta_sku(conn))
        except Exception:
            data["primera_venta"] = pd.DataFrame()

    with st.spinner("Calculando antigüedad FIFO del stock..."):
        try:
            # Siempre query directa para evitar caché stale
            from db.queries import QUERY_STOCK_AGE_FIFO
            data["stock_age_fifo"] = norm_cols(pd.read_sql(QUERY_STOCK_AGE_FIFO, conn))
        except Exception as _e_fifo:
            import traceback as _tb
            st.warning(f"⚠️ FIFO no cargó: {_e_fifo}")
            st.code(_tb.format_exc())
            data["stock_age_fifo"] = pd.DataFrame()

    with st.spinner("Cargando ABC-XYZ..."):
        try:
            data["abc_xyz"] = norm_cols(cq.abc_xyz_fsn(conn))
        except Exception:
            data["abc_xyz"] = pd.DataFrame()

    with st.spinner("Cargando ventas 24m..."):
        try:
            data["ventas_px"] = norm_cols(cq.ventas_mensual_precio(conn))
        except Exception:
            data["ventas_px"] = pd.DataFrame()

    with st.spinner("Cargando stock CD diario 6m..."):
        try:
            data["stock_cd_diario"] = norm_cols(cq.stock_cd_diario_6m(conn))
        except Exception:
            data["stock_cd_diario"] = pd.DataFrame()

    with st.spinner("Cargando penetración tiendas..."):
        try:
            data["tdas_vta"] = norm_cols(cq.tiendas_venta_sku(conn))
        except Exception:
            data["tdas_vta"] = pd.DataFrame()

    with st.spinner("Cargando detalle stock por bodega..."):
        try:
            data["stock_bodega"] = norm_cols(cq.stock_detalle_bodega(conn))
        except Exception:
            data["stock_bodega"] = pd.DataFrame()

    return data


# ═══════════════════════════════════════════════════════════════════════════
# Chart builder
# ═══════════════════════════════════════════════════════════════════════════

def _build_bc_chart(bc_sku, ventas_px, conn, show_cost=False, canal_filter=None):
    """Build the 2-row business case chart for a single SKU.

    Returns a Plotly figure or None if no data is available.
    """
    _today = pd.Timestamp.now().normalize()
    _today_m = _today.to_period("M").to_timestamp()

    # ── Historical price + volume ──
    _bc_hist = pd.DataFrame()
    if not ventas_px.empty and "SKU_PRODUCTO" in ventas_px.columns:
        _bc_v = ventas_px[ventas_px["SKU_PRODUCTO"] == bc_sku].copy()
        if canal_filter and "COD_CANAL" in _bc_v.columns:
            _bc_v = _bc_v[_bc_v["COD_CANAL"].isin(canal_filter)]
        if not _bc_v.empty:
            _bc_v["PERIODO"] = pd.to_datetime(_bc_v["PERIODO"], errors="coerce")
            for _vc in ["CANTIDAD", "NETO", "APORTE"]:
                if _vc in _bc_v.columns:
                    _bc_v[_vc] = pd.to_numeric(_bc_v[_vc], errors="coerce").fillna(0)
            _bc_hist = _bc_v.groupby("PERIODO", as_index=False).agg(
                CANTIDAD=("CANTIDAD", "sum"),
                NETO=("NETO", "sum"),
                APORTE=("APORTE", "sum"),
            ).sort_values("PERIODO")
            _bc_hist["PRECIO_PROM"] = np.where(
                _bc_hist["CANTIDAD"] > 0,
                _bc_hist["NETO"] / _bc_hist["CANTIDAD"],
                0,
            )

    # ── Projection ──
    _bc_proy = pd.DataFrame()
    _df_proy_bc = st.session_state.get("df_proy", pd.DataFrame())
    if not _df_proy_bc.empty and "SKU_PRODUCTO" in _df_proy_bc.columns:
        _bp = _df_proy_bc[_df_proy_bc["SKU_PRODUCTO"] == bc_sku].copy()
        if "TIPO_DATO" in _bp.columns:
            _bp = _bp[_bp["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])]
        if not _bp.empty:
            _bp["PERIODO"] = pd.to_datetime(_bp["PERIODO"], errors="coerce")
            for _nc in ["VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND",
                        "VENTA_FUL_MAYOR_UND", "VN_RES_TOTAL",
                        "STOCK_FINAL_TOTAL", "COSTO_UNITARIO",
                        "COGS_RES_TOTAL", "FORECAST_COMPRA", "ETA"]:
                if _nc in _bp.columns:
                    _bp[_nc] = pd.to_numeric(_bp[_nc], errors="coerce").fillna(0)
            _bp["CANTIDAD_PROY"] = (
                _bp.get("VENTA_FUL_TIENDA_UND", 0)
                + _bp.get("VENTA_FUL_ETAIL_UND", 0)
                + _bp.get("VENTA_FUL_MAYOR_UND", 0)
            )
            _bp["PRECIO_PROY"] = np.where(
                _bp["CANTIDAD_PROY"] > 0,
                _bp.get("VN_RES_TOTAL", 0) / _bp["CANTIDAD_PROY"],
                0,
            )
            _bp["STOCK_FINAL"] = _bp.get("STOCK_FINAL_TOTAL", 0)
            _cu_bp = _bp.get("COSTO_UNITARIO", 0)
            _bp["STOCK_FINAL_CLP"] = _bp["STOCK_FINAL"] * _cu_bp
            _bp = _bp.sort_values("PERIODO").reset_index(drop=True)
            _cogs_vals = _bp["COGS_RES_TOTAL"].values
            _cogs_fwd6 = np.zeros(len(_bp))
            for _i in range(len(_bp)):
                _window = _cogs_vals[_i:_i + 6]
                _window_pos = _window[_window > 0]
                _cogs_fwd6[_i] = _window_pos.mean() if len(_window_pos) > 0 else 0
            _bp["_COGS_FWD6"] = _cogs_fwd6
            _bp["MOI_PROY"] = np.where(
                _bp["_COGS_FWD6"] > 0,
                _bp["STOCK_FINAL_CLP"] / _bp["_COGS_FWD6"],
                0,
            )
            _bp.drop(columns=["_COGS_FWD6"], inplace=True)
            _bc_proy = _bp.sort_values("PERIODO")

    # ── Fallback: project with historical sales if no forecast ──
    _is_fallback = False
    if _bc_proy.empty and not _bc_hist.empty:
        _is_fallback = True
        _hist_avg_qty = _bc_hist["CANTIDAD"].mean()
        _hist_avg_px = float(np.where(
            _bc_hist["CANTIDAD"].sum() > 0,
            _bc_hist["NETO"].sum() / _bc_hist["CANTIDAD"].sum(),
            0,
        ))
        _fb_periods = pd.date_range(start=_today_m, periods=12, freq="MS")
        _fb_rows = [{"PERIODO": fp, "CANTIDAD_PROY": _hist_avg_qty,
                     "PRECIO_PROY": _hist_avg_px, "STOCK_FINAL": 0,
                     "STOCK_FINAL_CLP": 0, "MOI_PROY": 0,
                     "FORECAST_COMPRA": 0, "ETA": 0} for fp in _fb_periods]
        _bc_proy = pd.DataFrame(_fb_rows)

    # ── Weekly stock+MOI ──
    _bc_stock_hist = pd.DataFrame()
    try:
        _bc_all_m = norm_cols(cq.stock_critico_metrics(conn))
        if not _bc_all_m.empty and "SKU_PRODUCTO" in _bc_all_m.columns:
            _bc_stock_hist = _bc_all_m[_bc_all_m["SKU_PRODUCTO"] == bc_sku].copy()
            _bc_stock_hist["FECHA"] = pd.to_datetime(
                _bc_stock_hist["FECHA"], errors="coerce",
            )
            for _sc in ["STOCK_COSTO", "STOCK_UNIDADES", "MOI"]:
                if _sc in _bc_stock_hist.columns:
                    _bc_stock_hist[_sc] = pd.to_numeric(
                        _bc_stock_hist[_sc], errors="coerce",
                    ).fillna(0)
            _bc_stock_hist = _bc_stock_hist.sort_values("FECHA")
    except Exception:
        pass

    _has_hist = not _bc_hist.empty
    _has_proy = not _bc_proy.empty
    _has_stock = not _bc_stock_hist.empty

    if not _has_hist and not _has_proy and not _has_stock:
        return None

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        vertical_spacing=0.16, row_heights=[0.5, 0.5],
        specs=[[{"secondary_y": True}], [{"secondary_y": True}]],
        subplot_titles=("Precio Promedio vs Volumen Vendido",
                        "Nivel de Inventario vs MOI"),
    )

    # ── Get MTD real sales for current month ──
    _mtd_und = 0
    _mtd_vn = 0
    try:
        _df_mtd = norm_cols(cq.ventas_mtd(conn))
        if not _df_mtd.empty and "SKU_PRODUCTO" in _df_mtd.columns:
            _mtd_sku = _df_mtd[_df_mtd["SKU_PRODUCTO"] == bc_sku]
            if canal_filter and "COD_CANAL" in _mtd_sku.columns:
                _mtd_sku = _mtd_sku[_mtd_sku["COD_CANAL"].isin(canal_filter)]
            _mtd_und = pd.to_numeric(
                _mtd_sku.get("CANTIDAD_MTD", 0), errors="coerce"
            ).fillna(0).sum()
            _mtd_vn = pd.to_numeric(
                _mtd_sku.get("NETO_MTD", 0), errors="coerce"
            ).fillna(0).sum()
    except Exception:
        pass

    # ROW 1: Price vs Volume
    if _has_hist:
        fig.add_trace(
            go.Bar(
                x=_bc_hist["PERIODO"], y=_bc_hist["CANTIDAD"],
                name="Venta Real (und)", marker_color=COLORS["primary"],
                opacity=0.7,
            ),
            row=1, col=1, secondary_y=False,
        )
        _px_hist = _bc_hist[_bc_hist["PRECIO_PROM"] > 0]
        if not _px_hist.empty:
            fig.add_trace(
                go.Scatter(
                    x=_px_hist["PERIODO"], y=_px_hist["PRECIO_PROM"],
                    name="Precio Promedio ($)", mode="lines+markers",
                    line=dict(color=COLORS["status_at_risk"], width=2.5),
                    marker=dict(size=5),
                ),
                row=1, col=1, secondary_y=True,
            )

    # Current month: split bar (green=real MTD, hatched=FC remainder)
    _bc_proy_chart = _bc_proy if _has_proy else pd.DataFrame()
    if _has_proy and _mtd_und > 0:
        _cur_month = _today_m
        _proy_cur = _bc_proy[_bc_proy["PERIODO"] == _cur_month]
        _proy_rest = _bc_proy[_bc_proy["PERIODO"] != _cur_month]
        if not _proy_cur.empty:
            _fc_total = float(_proy_cur["CANTIDAD_PROY"].iloc[0])
            _fc_remainder = max(_fc_total - _mtd_und, 0)
            _dia_mes = _today.day
            _px_mtd = _mtd_vn / _mtd_und if _mtd_und > 0 else 0
            fig.add_trace(
                go.Bar(
                    x=[_cur_month], y=[_mtd_und],
                    name="Venta Real MTD",
                    marker_color=COLORS.get("status_on_track", "#22c55e"),
                    opacity=0.85,
                    customdata=[[_fc_total, _dia_mes, _px_mtd]],
                    hovertemplate=(
                        "Real MTD: %{y:,.0f} und (al día %{customdata[1]:.0f})<br>"
                        "FC mes: %{customdata[0]:,.0f} und<br>"
                        "Precio MTD: $%{customdata[2]:,.0f}<extra></extra>"
                    ),
                ),
                row=1, col=1, secondary_y=False,
            )
            if _fc_remainder > 0:
                fig.add_trace(
                    go.Bar(
                        x=[_cur_month], y=[_fc_remainder],
                        base=[_mtd_und],
                        name="FC Restante Mes",
                        marker_color=COLORS["tertiary_blue"],
                        opacity=0.4, marker_pattern_shape="/",
                        hovertemplate="FC restante: %{y:,.0f} und<extra></extra>",
                    ),
                    row=1, col=1, secondary_y=False,
                )
            _bc_proy_chart = _proy_rest

    if not _bc_proy_chart.empty:
        _proy_label = "Proy. Hist. (und)" if _is_fallback else "Venta Proyectada (und)"
        fig.add_trace(
            go.Bar(
                x=_bc_proy_chart["PERIODO"], y=_bc_proy_chart["CANTIDAD_PROY"],
                name=_proy_label,
                marker_color=COLORS["tertiary_blue"],
                opacity=0.5, marker_pattern_shape="/",
            ),
            row=1, col=1, secondary_y=False,
        )
    if _has_proy and not _is_fallback:
        _px_proy = _bc_proy[_bc_proy["PRECIO_PROY"] > 0]
        if not _px_proy.empty:
            fig.add_trace(
                go.Scatter(
                    x=_px_proy["PERIODO"], y=_px_proy["PRECIO_PROY"],
                    name="Precio Proyectado ($)", mode="lines+markers",
                    line=dict(color=COLORS["status_at_risk"], width=2, dash="dash"),
                    marker=dict(size=4),
                ),
                row=1, col=1, secondary_y=True,
            )

    # ROW 2: Inventory vs MOI
    _inv_hist_col = "STOCK_COSTO" if show_cost else "STOCK_UNIDADES"
    _inv_label = "Stock ($)" if show_cost else "Stock (und)"
    _proy_inv_col = "STOCK_FINAL_CLP" if show_cost else "STOCK_FINAL"

    if _has_stock and _inv_hist_col in _bc_stock_hist.columns:
        fig.add_trace(
            go.Bar(
                x=_bc_stock_hist["FECHA"], y=_bc_stock_hist[_inv_hist_col],
                name=f"Stock Hist ({_inv_label})",
                marker_color=COLORS["primary"], opacity=0.6,
            ),
            row=2, col=1, secondary_y=False,
        )
    if _has_proy and not _is_fallback and _proy_inv_col in _bc_proy.columns:
        fig.add_trace(
            go.Bar(
                x=_bc_proy["PERIODO"], y=_bc_proy[_proy_inv_col],
                name=f"Stock Proy ({_inv_label})",
                marker_color=COLORS["tertiary_blue"],
                opacity=0.75, marker_pattern_shape="/",
            ),
            row=2, col=1, secondary_y=False,
        )

    if _has_stock and "MOI" in _bc_stock_hist.columns:
        _moi_valid = _bc_stock_hist[_bc_stock_hist["MOI"] > 0]
        if not _moi_valid.empty:
            fig.add_trace(
                go.Scatter(
                    x=_moi_valid["FECHA"], y=_moi_valid["MOI"],
                    name="MOI Histórico", mode="lines+markers",
                    line=dict(color=COLORS["status_at_risk"], width=2.5),
                    marker=dict(size=4),
                ),
                row=2, col=1, secondary_y=True,
            )
    if _has_proy and not _is_fallback and "MOI_PROY" in _bc_proy.columns:
        _moi_proy_v = _bc_proy[_bc_proy["MOI_PROY"] > 0]
        if not _moi_proy_v.empty:
            fig.add_trace(
                go.Scatter(
                    x=_moi_proy_v["PERIODO"], y=_moi_proy_v["MOI_PROY"],
                    name="MOI Proyectado", mode="lines+markers",
                    line=dict(color=COLORS["status_at_risk"], width=2, dash="dash"),
                    marker=dict(size=4),
                ),
                row=2, col=1, secondary_y=True,
            )

    # Reference lines
    fig.add_hline(y=4, line_dash="dash", line_color=COLORS["status_on_track"],
                  annotation_text="MOI Target (4m)", annotation_position="top right",
                  row=2, col=1, secondary_y=True)
    fig.add_hline(y=12, line_dash="dash", line_color=COLORS["status_critical"],
                  annotation_text="MOI Crítico (12m)", annotation_position="top right",
                  row=2, col=1, secondary_y=True)

    # "Today" line
    for _vl_yref in ["y domain", "y3 domain"]:
        fig.add_shape(
            type="line", x0=_today_m, x1=_today_m, y0=0, y1=1,
            yref=_vl_yref,
            line=dict(dash="dot", color="gray", width=1.5),
        )
    fig.add_annotation(
        x=_today_m, y=1, yref="y domain",
        text="Hoy", showarrow=False,
        font=dict(size=10, color="gray"), yshift=10,
    )

    # Supply inflow bars
    if _has_proy and not _is_fallback:
        for _fc_col, _fc_name, _fc_color in [
            ("ETA", "Recepción ETA", COLORS.get("status_en_curso", "#0ea5e9")),
            ("FORECAST_COMPRA", "FC Compra", COLORS.get("tertiary_teal", "#14b8a6")),
        ]:
            if _fc_col in _bc_proy.columns:
                _fc_data = _bc_proy[_bc_proy[_fc_col] > 0]
                if not _fc_data.empty:
                    fig.add_trace(
                        go.Bar(
                            x=_fc_data["PERIODO"], y=_fc_data[_fc_col],
                            name=_fc_name, marker_color=_fc_color,
                            opacity=0.65, width=15 * 86400000,
                            text=[f"{v:,.0f}" for v in _fc_data[_fc_col]],
                            textposition="outside",
                            textfont=dict(size=8, color=_fc_color),
                        ),
                        row=2, col=1, secondary_y=False,
                    )

    # Layout
    _moi_max = 15
    if _has_stock and "MOI" in _bc_stock_hist.columns:
        _moi_max = max(_moi_max, _bc_stock_hist["MOI"].max() * 1.3)
    if _has_proy and "MOI_PROY" in _bc_proy.columns:
        _moi_max = max(_moi_max, _bc_proy["MOI_PROY"].max() * 1.3)

    fig.update_yaxes(title_text="Unidades Vendidas", row=1, col=1,
                     secondary_y=False, showgrid=False)
    fig.update_yaxes(title_text="Precio Neto ($)", row=1, col=1,
                     secondary_y=True, showgrid=False)
    fig.update_yaxes(title_text=_inv_label, row=2, col=1,
                     secondary_y=False, showgrid=False)
    fig.update_yaxes(title_text="MOI (meses)", row=2, col=1,
                     range=[0, _moi_max], secondary_y=True, showgrid=False)
    fig.update_xaxes(title_text="Periodo", row=2, col=1, showgrid=False)
    fig.update_xaxes(showgrid=False, row=1, col=1)

    _lo = dorel_layout(
        height=800,
        margin=dict(t=100),
        legend=dict(orientation="h", yanchor="bottom",
                    y=1.06, x=0.5, xanchor="center",
                    font=dict(size=10)),
    )
    fig.update_layout(**_lo, hovermode="x unified")
    for _ann in fig.layout.annotations:
        if hasattr(_ann, "y") and _ann.y is not None:
            if _ann.y > 0.7:
                _ann.update(y=_ann.y - 0.04, font=dict(size=12))
            else:
                _ann.update(font=dict(size=12))

    if _is_fallback:
        fig.add_annotation(
            text="Sin forecast — proyección basada en promedio histórico de venta",
            xref="paper", yref="paper", x=0.5, y=1.12,
            showarrow=False,
            font=dict(size=11, color=COLORS.get("status_at_risk", "#f59e0b")),
        )

    return fig, _bc_hist, _bc_proy, _bc_stock_hist


# ═══════════════════════════════════════════════════════════════════════════
# Diagnostics builder
# ═══════════════════════════════════════════════════════════════════════════

def _build_bc_diagnostics(bc_row, ventas_px, df_pool, bc_hist=None, stock_bodega=None):
    """Build diagnostic markdown bullets for a single SKU.

    Returns list of markdown strings.
    """
    _insights = []
    _bc_sku = bc_row.get("SKU_PRODUCTO", "")
    _bc_moi_h = float(bc_row.get("MOI_HIST", 0) or 0)
    _bc_moi_f = float(bc_row.get("MOI_FC", 0) or 0)
    _bc_moi_adj = float(bc_row.get("MOI_HIST_AJUST", _bc_moi_h) or _bc_moi_h)
    _bc_rot = float(bc_row.get("ROTACION_UND_MES", 0) or 0)
    _bc_rot_adj = float(bc_row.get("ROT_AJUST_UND_MES", _bc_rot) or _bc_rot)
    _bc_dcto = float(bc_row.get("DCTO_SUGERIDO", 0) or 0)
    _bc_st_u = float(bc_row.get("STOCK_UNIDADES", 0) or 0)
    _bc_tasa_disp = float(bc_row.get("TASA_DISPONIBILIDAD", 1.0) or 1.0)
    _bc_meses_cv = int(bc_row.get("MESES_CON_VENTA", 0) or 0)
    _bc_meses_lb = int(bc_row.get("MESES_LOOKBACK", 0) or 0)
    _bc_conf_moi = str(bc_row.get("CONFIABILIDAD_MOI", "") or "")
    _bc_dias_stock = int(bc_row.get("DIAS_CON_STOCK", 0) or 0)

    # ── 1. Header: MIX type + hierarchy + age ──
    _mix_tipo = str(bc_row.get("MIX_OFICIAL", "")).strip()
    _meses_cia = float(bc_row.get("MESES_EN_CIA", 0) or 0)
    _primera_vta = bc_row.get("PRIMERA_VENTA", pd.NaT)
    _area = str(bc_row.get("AREA", "")).strip()
    _linea = str(bc_row.get("LINEA", "")).strip()
    _sublinea = str(bc_row.get("SUBLINEA", "")).strip()
    _marca = str(bc_row.get("MARCA", "")).strip()

    _hdr_parts = []
    if _mix_tipo and _mix_tipo.upper() not in ("NAN", "NONE", ""):
        _hdr_parts.append(f"**{_mix_tipo}**")
    else:
        _hdr_parts.append("**Sin clasificar MIX**")
    _hier = " > ".join(p for p in [_area, _linea, _sublinea, _marca]
                       if p and p.upper() not in ("NAN", "NONE", ""))
    if _hier:
        _hdr_parts.append(_hier)
    if _meses_cia > 0:
        _y = int(_meses_cia // 12)
        _m = int(_meses_cia % 12)
        _age_str = f"{_y}a {_m}m" if _y > 0 else f"{_m}m"
        _pv_str = ""
        try:
            if pd.notna(_primera_vta):
                _pv_str = f" (1ª venta: {pd.Timestamp(_primera_vta).strftime('%b-%Y')})"
        except Exception:
            pass
        _hdr_parts.append(f"antigüedad **{_age_str}**{_pv_str}")
    else:
        _hdr_parts.append("sin historial de ventas")
    _insights.append("🏷️ " + " · ".join(_hdr_parts))

    # ── 2. MOI severity + censored demand insight ──
    _moi_display = _bc_moi_adj  # use adjusted as primary reference
    if _bc_tasa_disp < 0.7 and _bc_moi_h > _bc_moi_adj * 1.5 and _bc_meses_lb > 0:
        # Significant divergence due to stockouts — explain
        # Nota de confiabilidad según días de data buena
        _conf_nota = ""
        if _bc_dias_stock >= 60:
            _conf_nota = f" Confiabilidad alta ({_bc_dias_stock} días de data buena)."
        elif _bc_dias_stock >= 56:
            _conf_nota = (
                f" Confiabilidad cercana a alta ({_bc_dias_stock} días, "
                f"prácticamente 2 meses completos de data buena)."
            )
        elif _bc_dias_stock >= 28:
            _conf_nota = (
                f" Confiabilidad parcial ({_bc_dias_stock} días de data buena, "
                f"~{_bc_dias_stock / 30.44:.1f} meses). "
                f"Se consolidará con más meses de stock adecuado."
            )
        else:
            _conf_nota = (
                f" Dato insuficiente (solo {_bc_dias_stock} días con stock adecuado)."
            )
        _insights.append(
            f"📊 **Demanda censurada**: MOI bruto = {_bc_moi_h:.0f}m pero el "
            f"producto solo tuvo stock {_bc_meses_cv} de {_bc_meses_lb} meses "
            f"({_bc_tasa_disp:.0%}). Ajustando por disponibilidad real, "
            f"**MOI Ajustado = {_bc_moi_adj:.0f}m** "
            f"(rot ajust. {_bc_rot_adj:.1f} und/mes vs bruta {_bc_rot:.1f})."
            f"{_conf_nota}"
        )
    if _moi_display >= 12:
        _insights.append(
            f"⚠️ Este SKU tiene **{_moi_display:.0f} meses de inventario** "
            f"al ritmo {'ajustado' if _bc_tasa_disp < 0.7 else 'actual'} "
            f"de venta ({_bc_rot_adj:.1f} und/mes)."
        )
    elif _moi_display >= 6:
        _insights.append(
            f"Este SKU tiene **{_moi_display:.0f} meses de inventario**, por encima "
            f"del target de 4 meses."
        )

    # ── 2b. Cruce MOI Ajustado vs MOI FC (validación cruzada) ──
    if _bc_moi_adj > 0 and _bc_moi_f > 0 and _bc_tasa_disp < 0.7:
        _adj_fc_ratio = _bc_moi_adj / _bc_moi_f
        if 0.5 <= _adj_fc_ratio <= 2.0:
            _insights.append(
                f"✅ **Validación cruzada**: MOI Ajustado ({_bc_moi_adj:.0f}m) y "
                f"MOI FC ({_bc_moi_f:.0f}m) son consistentes (ratio {_adj_fc_ratio:.1f}×). "
                f"El forecast refleja bien la demanda real cuando hay stock."
            )
        elif _adj_fc_ratio > 3.0:
            _insights.append(
                f"⚠️ **MOI Ajustado ({_bc_moi_adj:.0f}m) sigue alto** aún corrigiendo "
                f"por quiebres. El forecast ({_bc_moi_f:.0f}m) asume {_adj_fc_ratio:.1f}× más "
                f"venta — verificar si el FC es realista."
            )

    # ── 3. MOI divergence + channel breakdown ──
    if _bc_moi_f > 0 and _bc_moi_h > 0:
        _moi_ratio = _bc_moi_h / _bc_moi_f
        _ch_detail = ""
        _df_proy_ch = st.session_state.get("df_proy", pd.DataFrame())
        if not _df_proy_ch.empty and "SKU_PRODUCTO" in _df_proy_ch.columns:
            _ch_sku = _df_proy_ch[_df_proy_ch["SKU_PRODUCTO"] == _bc_sku].copy()
            if "TIPO_DATO" in _ch_sku.columns and not _ch_sku.empty:
                _ch_hist = _ch_sku[_ch_sku["TIPO_DATO"] == "HISTORICO"]
                _ch_fc = _ch_sku[_ch_sku["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])]
                _n_hist_m = max(_ch_hist["PERIODO"].nunique(), 1) if "PERIODO" in _ch_hist.columns and not _ch_hist.empty else 1
                _n_fc_m = max(_ch_fc["PERIODO"].nunique(), 1) if "PERIODO" in _ch_fc.columns and not _ch_fc.empty else 1
                _h_range = ""
                _f_range = ""
                if "PERIODO" in _ch_hist.columns and not _ch_hist.empty:
                    _hp = pd.to_datetime(_ch_hist["PERIODO"], errors="coerce").dropna()
                    if not _hp.empty:
                        _h_range = f"{_hp.min().strftime('%b-%y')}→{_hp.max().strftime('%b-%y')}"
                if "PERIODO" in _ch_fc.columns and not _ch_fc.empty:
                    _fp = pd.to_datetime(_ch_fc["PERIODO"], errors="coerce").dropna()
                    if not _fp.empty:
                        _f_range = f"{_fp.min().strftime('%b-%y')}→{_fp.max().strftime('%b-%y')}"
                _ch_lines = []
                for _ch_name, _dem_col, _ful_col in [
                    ("Retail", "DEMANDA_SIM_TIENDA", "VENTA_FUL_TIENDA_UND"),
                    ("Etail", "DEMANDA_SIM_ETAIL", "VENTA_FUL_ETAIL_UND"),
                    ("Mayor", "DEMANDA_SIM_MAYOR", "VENTA_FUL_MAYOR_UND"),
                ]:
                    _h_avg = 0.0
                    if _ful_col in _ch_hist.columns and not _ch_hist.empty:
                        _h_avg = pd.to_numeric(_ch_hist[_ful_col], errors="coerce").fillna(0).sum() / _n_hist_m
                    _f_avg = 0.0
                    if _dem_col in _ch_fc.columns and not _ch_fc.empty:
                        _f_avg = pd.to_numeric(_ch_fc[_dem_col], errors="coerce").fillna(0).sum() / _n_fc_m
                    if _h_avg > 0 or _f_avg > 0:
                        _ch_ratio = _f_avg / _h_avg if _h_avg > 0 else float("inf")
                        _arrow = "🔺" if _ch_ratio > 1.3 else ("🔻" if _ch_ratio < 0.7 else "➡️")
                        if _h_avg > 0:
                            _ch_lines.append(
                                f"  - **{_ch_name}**: hist {_h_avg:,.0f} → FC {_f_avg:,.0f} und/mes "
                                f"({_arrow} {_ch_ratio:.1f}×)")
                        else:
                            _ch_lines.append(
                                f"  - **{_ch_name}**: sin venta hist → FC {_f_avg:,.0f} und/mes")
                if _ch_lines:
                    _range_label = ""
                    if _h_range or _f_range:
                        _range_label = f"\n  Hist ({_h_range}, {_n_hist_m}m) vs FC ({_f_range}, {_n_fc_m}m):"
                    _ch_detail = _range_label + "\n" + "\n".join(_ch_lines)

        if _moi_ratio > 3:
            _insights.append(
                f"🔴 **Gran divergencia**: MOI histórico "
                f"(**{_bc_moi_h:.0f}m**) vs forecast "
                f"(**{_bc_moi_f:.0f}m**). El forecast asume "
                f"**{_moi_ratio:.1f}× más venta** que la historia reciente. "
                f"Si la venta real sigue por debajo del forecast, "
                f"el stock tardará mucho más en rotar."
                + _ch_detail
            )
        elif _moi_ratio > 1.5:
            _insights.append(
                f"⚠️ **Divergencia moderada**: MOI histórico "
                f"(**{_bc_moi_h:.0f}m**) vs forecast "
                f"(**{_bc_moi_f:.0f}m**). El forecast asume "
                f"**{_moi_ratio:.1f}× más venta** — monitorear "
                f"cumplimiento del FC."
                + _ch_detail
            )
        elif _moi_ratio < 0.7 and _bc_moi_h >= 4:
            _insights.append(
                f"📉 El forecast proyecta **menos venta** que la "
                f"historia reciente (MOI FC {_bc_moi_f:.0f}m > "
                f"MOI Hist {_bc_moi_h:.0f}m). "
                f"El stock podría crecer si no se ajusta."
                + _ch_detail
            )

    # ── 4. Forecast gap detection ──
    _df_proy_diag = st.session_state.get("df_proy", pd.DataFrame())
    _has_fc_diag = False
    if not _df_proy_diag.empty and "SKU_PRODUCTO" in _df_proy_diag.columns:
        _fc_check = _df_proy_diag[_df_proy_diag["SKU_PRODUCTO"] == _bc_sku].copy()
        if "TIPO_DATO" in _fc_check.columns:
            _fc_check = _fc_check[_fc_check["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])]
        _has_fc_diag = not _fc_check.empty
        if _has_fc_diag and "PERIODO" in _fc_check.columns:
            _fc_check["PERIODO"] = pd.to_datetime(_fc_check["PERIODO"], errors="coerce")
            _fc_sorted = _fc_check.sort_values("PERIODO")
            _fc_dem_cols = [c for c in [
                "DEMANDA_SIM_TIENDA", "DEMANDA_SIM_ETAIL", "DEMANDA_SIM_MAYOR",
                "DEMANDA_TOTAL",
            ] if c in _fc_sorted.columns]
            if not _fc_dem_cols:
                _fc_dem_cols = [c for c in [
                    "VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND",
                    "VENTA_FUL_MAYOR_UND",
                ] if c in _fc_sorted.columns]
            if _fc_dem_cols:
                for _qc in _fc_dem_cols:
                    _fc_sorted[_qc] = pd.to_numeric(_fc_sorted[_qc], errors="coerce").fillna(0)
                _fc_sorted["_DEM_TOTAL"] = _fc_sorted[_fc_dem_cols].sum(axis=1)
                if "DEMANDA_TOTAL" in _fc_dem_cols and len(_fc_dem_cols) > 1:
                    _fc_sorted["_DEM_TOTAL"] = pd.to_numeric(
                        _fc_sorted["DEMANDA_TOTAL"], errors="coerce"
                    ).fillna(0)
                _fc_with_dem = _fc_sorted[_fc_sorted["_DEM_TOTAL"] >= 1]
                _fc_zero_dem = _fc_sorted[_fc_sorted["_DEM_TOTAL"] < 1]
                _n_fc_total = len(_fc_sorted)
                _n_fc_zero = len(_fc_zero_dem)
                if _n_fc_zero > 0 and _n_fc_total > 0:
                    _zero_periods = _fc_zero_dem["PERIODO"].dt.strftime("%b %Y").tolist()
                    if not _fc_with_dem.empty:
                        _last_fc_period = _fc_with_dem["PERIODO"].max()
                        _fc_after_last = _fc_sorted[_fc_sorted["PERIODO"] > _last_fc_period]
                        _n_missing_tail = len(_fc_after_last)
                        if _n_missing_tail >= 2:
                            _insights.append(
                                f"🔴 **FORECAST INCOMPLETO**: el forecast desaparece después de "
                                f"**{_last_fc_period.strftime('%b %Y')}** — "
                                f"**{_n_missing_tail} meses sin demanda proyectada** "
                                f"({', '.join(_zero_periods[-5:])}). "
                                f"Esto eleva artificialmente el MOI proyectado. "
                                f"Revisar si el forecast está cargado correctamente."
                            )
                        elif _n_fc_zero >= 2:
                            _insights.append(
                                f"⚠️ **Gaps en forecast**: {_n_fc_zero} de {_n_fc_total} "
                                f"meses tienen **demanda 0** "
                                f"({', '.join(_zero_periods[:5])}). "
                                f"Verificar si el forecast cubre todos los periodos."
                            )
                    elif _n_fc_zero == _n_fc_total:
                        _insights.append(
                            f"🔴 **FORECAST EN CERO**: la proyección existe pero con "
                            f"**0 unidades de demanda** en los {_n_fc_total} meses. "
                            f"Revisar si el forecast está correctamente asignado a este SKU."
                        )
    if not _has_fc_diag:
        _insights.append(
            "⚠️ **SIN FORECAST**: Este SKU no tiene proyección cargada. "
            "La proyección del gráfico se basa en el promedio de venta histórica."
        )

    # ── 5. Price trend ──
    if bc_hist is not None and not bc_hist.empty and len(bc_hist) >= 6:
        _px_6m = bc_hist.tail(6)["PRECIO_PROM"]
        _px_first = _px_6m.iloc[0] if len(_px_6m) > 0 else 0
        _px_last = _px_6m.iloc[-1] if len(_px_6m) > 0 else 0
        if _px_first > 0:
            _px_var = (_px_last - _px_first) / _px_first * 100
            if abs(_px_var) < 5:
                _insights.append(
                    f"El precio se ha mantenido **estable** los últimos 6 meses "
                    f"(variación {_px_var:+.1f}%). La demanda no ha respondido."
                )
            elif _px_var < -5:
                _insights.append(
                    f"El precio bajó **{_px_var:.1f}%** en 6 meses, pero "
                    f"el inventario sigue alto."
                )

    # ── 6. Transit ──
    _bc_transit_eta = pd.to_datetime(bc_row.get("TRANSITO_ETA", pd.NaT), errors="coerce")
    _bc_transit_und = bc_row.get("TRANSITO_UND", 0) or 0
    if pd.notna(_bc_transit_eta) and _bc_transit_und > 0:
        _moi_ll = float(bc_row.get("MOI_FC_LLEGADA", 0) or 0)
        _insights.append(
            f"🚢 Llegan **{_bc_transit_und:,.0f} unidades** el "
            f"**{_bc_transit_eta.strftime('%d/%m/%Y')}**"
            + (f", lo que elevará el MOI a ~**{_moi_ll:.1f}m**."
               if _moi_ll > 0 else ".")
        )

    # ── 7. Channel distribution ──
    _ui_dead_channels = []
    _ui_active_channels = []
    if not ventas_px.empty and "COD_CANAL" in ventas_px.columns:
        _bc_vp_sku = ventas_px[ventas_px["SKU_PRODUCTO"] == _bc_sku].copy()
        if not _bc_vp_sku.empty:
            _bc_vp_sku["CANTIDAD"] = pd.to_numeric(
                _bc_vp_sku.get("CANTIDAD", 0), errors="coerce"
            ).fillna(0)
            _bc_vp_sku["NETO"] = pd.to_numeric(
                _bc_vp_sku.get("NETO", 0), errors="coerce"
            ).fillna(0)
            _bc_vp_sku["PERIODO"] = pd.to_datetime(_bc_vp_sku["PERIODO"], errors="coerce")
            _bc_vp_6m = _bc_vp_sku[
                _bc_vp_sku["PERIODO"]
                >= _bc_vp_sku["PERIODO"].max() - pd.DateOffset(months=5)
            ]
            _ch_agg = _bc_vp_6m.groupby("COD_CANAL").agg(
                UND=("CANTIDAD", "sum"),
                VN=("NETO", "sum"),
                MESES=("PERIODO", "nunique"),
            )
            _ch_names = {"MINOR": "Retail", "ETAIL": "Etail", "MAYOR": "Mayorista"}
            _ch_total_und = _ch_agg["UND"].sum()
            _bc_vp_pos = _bc_vp_sku[_bc_vp_sku["CANTIDAD"] > 0]
            _last_sale_ch = (
                _bc_vp_pos.groupby("COD_CANAL")["PERIODO"].max()
                if not _bc_vp_pos.empty
                else pd.Series(dtype="datetime64[ns]")
            )
            _ch_lines = []
            for _cod, _label in _ch_names.items():
                if _cod in _ch_agg.index and _ch_agg.loc[_cod, "UND"] > 0:
                    _cu = _ch_agg.loc[_cod, "UND"]
                    _cm = int(_ch_agg.loc[_cod, "MESES"])
                    _rot_ch = _cu / max(_cm, 1)
                    _pct = _cu / _ch_total_und * 100 if _ch_total_und > 0 else 0
                    _reg = f"vende {_cm} de 6 meses" if _cm < 6 else "regular"
                    _ch_lines.append(
                        f"  - **{_label}**: {_cu:,.0f} und "
                        f"({_rot_ch:,.1f} und/mes) — "
                        f"{_pct:.0f}% del mix [{_reg}]"
                    )
                    _ui_active_channels.append(_cod)
                else:
                    if _cod in _last_sale_ch.index:
                        _meses_sin = max(1, (pd.Timestamp.now() - _last_sale_ch[_cod]).days // 30)
                        _ch_lines.append(f"  - **{_label}**: sin ventas hace **{_meses_sin} meses**")
                    else:
                        _ch_lines.append(f"  - **{_label}**: sin ventas registradas")
                    _ui_dead_channels.append(_cod)

            _ch_header = "📦 **Venta por canal** (últimos 6 meses):"
            if _ui_dead_channels:
                _dead_names = [_ch_names.get(c, c) for c in _ui_dead_channels]
                _ch_header += f" ⚠️ **sin ventas en {', '.join(_dead_names)}**"
            _insights.append("\n".join([_ch_header] + _ch_lines))

            if len(_ui_dead_channels) >= 2:
                _dead_names = [_ch_names.get(c, c) for c in _ui_dead_channels]
                _insights.append(
                    f"🚫 **Sin salidas de venta** en "
                    f"**{' ni '.join(_dead_names)}**. "
                    f"El producto depende de un solo canal, "
                    f"lo que limita severamente la rotación."
                )
        elif _bc_vp_sku.empty:
            _insights.append("📦 **Sin ventas en ningún canal** los últimos 24 meses.")

    # ── 8. Store coverage ──
    _ui_has_minor_sales = "MINOR" in _ui_active_channels
    _bc_n_perfil = float(bc_row.get("TIENDAS_CON_PERFIL", 0) or 0)
    _bc_n_venta = float(bc_row.get("N_TIENDAS_VENTA", 0) or 0)
    _bc_n_venta_3m = float(bc_row.get("N_TIENDAS_VENTA_3M", 0) or 0)
    _bc_n_meses_vta = float(bc_row.get("N_MESES_CON_VENTA", 0) or 0)
    _max_tdas = df_pool["TIENDAS_CON_PERFIL"].max() if "TIENDAS_CON_PERFIL" in df_pool.columns else 0
    if _bc_n_perfil == 0:
        _insights.append(
            "🏬 **Sin perfil en tiendas**. Este SKU no está asignado "
            "a ninguna sucursal retail."
        )
    else:
        _pct_tdas = _bc_n_perfil / _max_tdas * 100 if _max_tdas > 0 else 0
        _store_line = f"🏬 Perfil en **{_bc_n_perfil:.0f} tiendas**"
        if _max_tdas > 0:
            _store_line += f" ({_pct_tdas:.0f}% de la red)"
        if _bc_n_venta > 0:
            _penetracion = _bc_n_venta / _bc_n_perfil * 100
            _store_line += (
                f". Venden **{_bc_n_venta:.0f}** "
                f"(**{_penetracion:.0f}%** penetración 6m)"
            )
            if _bc_n_venta_3m < _bc_n_venta:
                _penetracion_3m = _bc_n_venta_3m / _bc_n_perfil * 100
                _store_line += (
                    f", solo **{_bc_n_venta_3m:.0f}** en últimos 3m ({_penetracion_3m:.0f}%)"
                )
        elif _ui_has_minor_sales:
            _store_line += ". Dato de penetración por tienda no disponible"
        else:
            _store_line += ". **Sin ventas retail** en 6m"
        if _bc_n_meses_vta > 0 and _bc_n_meses_vta < 6:
            _store_line += f". Actividad solo en {_bc_n_meses_vta:.0f} de 6 meses"
        _store_line += "."
        _insights.append(_store_line)
        if _bc_n_venta > 0:
            _penetracion = _bc_n_venta / _bc_n_perfil * 100
            if _penetracion < 30:
                _insights.append(
                    f"⚠️ **Baja penetración**: solo {_penetracion:.0f}% de las "
                    f"tiendas con perfil venden este SKU. "
                    f"Revisar si el perfil es adecuado o si hay problemas de exhibición."
                )

    # ── 9. Elasticity ──
    _ui_n_active = len(_ui_active_channels)
    _bc_elast = bc_row.get("ELASTICIDAD", np.nan)
    _bc_eseg = bc_row.get("ELAST_SEGMENTO", "Sin Dato")
    _ui_elast_unreliable = (
        _ui_n_active <= 1 or _bc_rot < 1
        or _bc_eseg in ("Sin Dato", "No Confiable")
    )
    if _ui_elast_unreliable:
        _reason_parts = []
        if _ui_n_active <= 1:
            _reason_parts.append("solo vende en 1 canal")
        if _bc_rot < 1:
            _reason_parts.append(f"rotación muy baja ({_bc_rot:.1f} und/mes)")
        if _bc_eseg in ("Sin Dato", "No Confiable"):
            _reason_parts.append("dato no confiable")
        _insights.append(
            f"💲 **Elasticidad no confiable** "
            f"({' + '.join(_reason_parts)}). "
            f"Sin historia suficiente para estimar respuesta a precio."
        )
    elif pd.notna(_bc_elast):
        if _bc_elast < -1.5 and _bc_moi_h >= 6:
            _insights.append(
                f"💲 Elasticidad **{_bc_elast:.2f}** ({_bc_eseg}): "
                f"la demanda responde fuerte a precio. "
                f"Una rebaja podría reducir el stock significativamente."
            )
        elif _bc_elast < -1.0 and _bc_moi_h >= 6:
            _insights.append(
                f"💲 Elasticidad **{_bc_elast:.2f}** ({_bc_eseg}): "
                f"hay espacio para acelerar venta con acción de precio."
            )
        elif abs(_bc_elast) < 0.3 and _bc_moi_h >= 6:
            _insights.append(
                f"💲 Elasticidad **{_bc_elast:.2f}** (Inelástico): "
                f"bajar precio no genera más volumen. "
                f"Considerar redistribuir o liquidar por otro canal."
            )

    # ── 10. Discount scenario ──
    if _bc_dcto > 0:
        _bc_px_actual = float(bc_row.get("PRECIO_PROM_NETO", 0) or 0)
        _bc_rot_act = float(bc_row.get("ROTACION_UND_MES", 0) or 0)
        _bc_rot_sim = float(bc_row.get("ROT_SIM", 0) or 0)
        _bc_mg_actual = float(bc_row.get("MARGEN_6M", 0) or 0)
        _bc_mg_sim = float(bc_row.get("MG_SIM", 0) or 0)
        _bc_px_nuevo = _bc_px_actual * (1 - _bc_dcto / 100) if _bc_px_actual > 0 else 0
        _bc_elast_v = bc_row.get("ELASTICIDAD", np.nan)
        _bc_elast_abs = abs(float(_bc_elast_v)) if pd.notna(_bc_elast_v) else 1.5
        _bc_meses = float(bc_row.get("MESES_LIQ", 999) or 999)

        # FC perspective
        _bc_meses_fc = 999
        if _bc_moi_f > 0 and _bc_rot_act > 0:
            _rot_fc_impl = _bc_st_u / _bc_moi_f if _bc_moi_f > 0 else 0
            _rot_fc_sim = _rot_fc_impl * (1 + _bc_elast_abs * _bc_dcto / 100)
            _bc_meses_fc = _bc_st_u / _rot_fc_sim if _rot_fc_sim > 0 else 999

        _rot_mult = _bc_rot_sim / _bc_rot_act if _bc_rot_act > 0 else 1
        _margen_drop = (_bc_mg_actual - _bc_mg_sim) * 100 if _bc_mg_actual > 0 else 0

        _dcto_lines = [
            f"💡 **Escenario descuento -{_bc_dcto:.0f}%** "
            f"(sobre precio promedio neto vendido 6m):"
        ]
        if _bc_px_actual > 0:
            _dcto_lines.append(f"  - Precio: **${_bc_px_actual:,.0f}** → **${_bc_px_nuevo:,.0f}**")
        if _bc_rot_act > 0:
            _dcto_lines.append(
                f"  - Rotación: **{_bc_rot_act:,.1f}** → "
                f"**{_bc_rot_sim:,.1f} und/mes** "
                f"(×{_rot_mult:.1f}, elast. {_bc_elast_abs:.1f})"
            )
        _dcto_lines.append(
            f"  - Margen: **{_bc_mg_actual*100:.0f}%** → **{_bc_mg_sim*100:.0f}%**"
        )
        _liq_hist = f"**{_bc_meses:.0f}m** (hist)" if _bc_meses < 999 else ">36m (hist)"
        _liq_fc = f"**{_bc_meses_fc:.0f}m** (FC)" if _bc_meses_fc < 999 else "—"
        _dcto_lines.append(f"  - Liquidación estimada: {_liq_hist} / {_liq_fc}")
        _insights.append("\n".join(_dcto_lines))

        # ── 11. MIX + stockout risk ──
        _bc_mix = str(bc_row.get("MIX_OFICIAL", bc_row.get("TIPO", ""))).strip().upper()
        _is_mix_activo = _bc_mix in ("MIX", "IN & OUT", "IN &AMP; OUT", "I&O", "IN&OUT")
        _is_descontinuar = _bc_mix in ("DESCONTINUAR", "FUERA DE MIX", "FUERA MIX")
        _liq_meses_ref = min(_bc_meses, _bc_meses_fc) if _bc_meses_fc < 999 else _bc_meses

        if _is_mix_activo and _liq_meses_ref < 999:
            if _liq_meses_ref <= 6:
                _insights.append(
                    f"🚨 **Riesgo de quiebre post-descuento**: este SKU es "
                    f"**{_bc_mix}** (activo) y con -{_bc_dcto:.0f}% el stock "
                    f"se agota en ~**{_liq_meses_ref:.0f} meses**. "
                    f"Si se aplica descuento, **adelantar reposición** para "
                    f"evitar quiebre. Evaluar lead time de importación."
                )
            elif _liq_meses_ref <= 12:
                _insights.append(
                    f"📋 SKU **{_bc_mix}** (activo): con descuento el stock dura "
                    f"~**{_liq_meses_ref:.0f} meses**. "
                    f"Monitorear y programar reposición a tiempo."
                )
        elif _is_descontinuar:
            _insights.append(
                f"✅ SKU **{_bc_mix}**: agotar stock es el objetivo. "
                f"No requiere reposición."
            )

        # ── 11b. Stock location for discontinued/out-of-mix with low stock ──
        if (_is_descontinuar or not _is_mix_activo) and stock_bodega is not None and not stock_bodega.empty:
            _bc_sku = str(bc_row.get("SKU_PRODUCTO", "")).strip()
            _sku_bodegas = stock_bodega[stock_bodega["SKU_PRODUCTO"] == _bc_sku].copy()
            if not _sku_bodegas.empty:
                _total_und = pd.to_numeric(_sku_bodegas.get("STOCK_UNIDADES", 0), errors="coerce").fillna(0).sum()
                _loc_lines = [f"📍 **Ubicación del stock** ({_total_und:,.0f} und en {len(_sku_bodegas)} bodegas):"]
                for _, _brow in _sku_bodegas.iterrows():
                    _bname = str(_brow.get("BODEGA_NOMBRE", _brow.get("COD_BODEGA", "?"))).strip()
                    _bcanal = str(_brow.get("CANAL", "")).strip().upper()
                    _bund = pd.to_numeric(_brow.get("STOCK_UNIDADES", 0), errors="coerce")
                    if _bund <= 0:
                        continue
                    # Recommend action based on location
                    _action = ""
                    _bname_up = _bname.upper()
                    if _bcanal == "CD":
                        if any(kw in _bname_up for kw in ["SEGUNDA", "2DA", "OUTLET", "DEVOL", "RECHAZO"]):
                            _action = " → gestionar venta manual o liquidación directa"
                        else:
                            _action = " → vender por ecommerce, buscar tienda que lo venda, o enviar a outlet"
                    elif _bcanal == "TIENDA":
                        if _bund <= 2:
                            _action = " → agotar en tienda (stock mínimo)"
                        else:
                            _action = " → descuento en tienda o transferir a outlet"
                    else:
                        _action = " → evaluar canal de salida"
                    _loc_lines.append(
                        f"  - **{_bname}** ({_bcanal}): {_bund:,.0f} und{_action}"
                    )
                if len(_loc_lines) > 1:
                    _insights.append("\n".join(_loc_lines))

        # ── 12. Structural overstock ──
        if _bc_rot_act > 0 and _bc_rot_act < 30 and _bc_moi_h >= 8:
            _order_est = _bc_rot_act * 6
            _moi_post_order = (_bc_st_u + _order_est) / _bc_rot_act if _bc_rot_act > 0 else 999
            if _moi_post_order > 12 and _order_est > 0:
                _insights.append(
                    f"⚠️ **Producto estructuralmente sobreinventariado**: "
                    f"con solo {_bc_rot_act:.1f} und/mes de rotación, "
                    f"cualquier reposición (ej: {_order_est:.0f} und ≈ 6m de demanda) "
                    f"genera ~**{_moi_post_order:.0f} meses de inventario**. "
                    f"El MOI alto es inherente a la baja rotación, no solo a exceso de compra. "
                    f"Evaluar si el perfil de tiendas y la amplitud de distribución justifican mantenerlo."
                )

        # ── 13. Discount ineffectiveness ──
        if _rot_mult < 1.3 and _margen_drop > 10 and _bc_moi_h >= 6:
            _warn_lines = [
                f"⚠️ **Descuento poco efectivo**: el precio baja "
                f"**-{_bc_dcto:.0f}%** pero la rotación sube solo "
                f"**×{_rot_mult:.1f}** (de {_bc_rot_act:.1f} a "
                f"{_bc_rot_sim:.1f} und/mes), "
                f"perdiendo **{_margen_drop:.0f} pp de margen**. "
                f"Alternativas a considerar:"
            ]
            if "MINOR" in _ui_dead_channels or _bc_n_perfil <= 5:
                _warn_lines.append("  - ampliar cobertura en tiendas")
            if "MAYOR" in _ui_dead_channels:
                _warn_lines.append('  - venta especial mayorista (ofrecer lote a "riflero")')
            if "ETAIL" in _ui_dead_channels:
                _warn_lines.append("  - activar/impulsar publicidad en RRSS y Etail")
            _warn_lines.append("  - incluir en pack o canasta promocional")
            _insights.append("\n".join(_warn_lines))

    return _insights


# ═══════════════════════════════════════════════════════════════════════════
# Scenario Simulator — aggregate impact by segment
# ═══════════════════════════════════════════════════════════════════════════


def _build_segments(df_enriched):
    """Build segment groups from enriched BC data.

    Returns DataFrame with one row per SEGMENTO with aggregate metrics.
    """
    df = df_enriched.copy()

    # Build segment label
    _mix = df.get("MIX_OFICIAL", df.get("TIPO", pd.Series("Sin Definir", index=df.index)))
    _mix = _mix.fillna("Sin Definir").astype(str).str.strip()
    _mix = _mix.replace({"": "Sin Definir", "nan": "Sin Definir", "None": "Sin Definir"})
    _act = df.get("ACCION_VENTA_RAW", pd.Series("MANTENER", index=df.index))
    _act = _act.fillna("MANTENER").astype(str).str.strip()
    df["SEGMENTO"] = _mix + " — " + _act

    _stk_c = pd.to_numeric(df.get("STOCK_COSTO", 0), errors="coerce").fillna(0)
    _stk_u = pd.to_numeric(df.get("STOCK_UNIDADES", 0), errors="coerce").fillna(0)
    _moi = pd.to_numeric(df.get("MOI_HIST", 0), errors="coerce").fillna(0)
    _dcto = pd.to_numeric(df.get("DCTO_SUGERIDO", 0), errors="coerce").fillna(0)
    _elast = pd.to_numeric(df.get("ELASTICIDAD", np.nan), errors="coerce").abs().fillna(1.5)

    df["_STK_C"] = _stk_c
    df["_STK_U"] = _stk_u
    df["_MOI"] = _moi
    df["_DCTO"] = _dcto
    df["_ELAST"] = _elast

    seg = df.groupby("SEGMENTO", as_index=False).agg(
        N_SKUS=("SKU_PRODUCTO", "nunique"),
        STOCK_COSTO=("_STK_C", "sum"),
        STOCK_UNIDADES=("_STK_U", "sum"),
        MOI_PROM=("_MOI", "mean"),
        DCTO_SUGERIDO=("_DCTO", "mean"),
        ELAST_PROM=("_ELAST", "mean"),
    )
    seg = seg.sort_values("STOCK_COSTO", ascending=False).reset_index(drop=True)

    # Also keep SKU-level mapping
    sku_seg = df[["SKU_PRODUCTO", "SEGMENTO", "_ELAST", "_DCTO"]].drop_duplicates("SKU_PRODUCTO")

    return seg, sku_seg


def _apply_scenario_to_proy(df_proy, sku_seg, discount_map, active_segments):
    """Apply discount scenarios to df_proy at SKU×Period level.

    Returns modified copy with scenario and baseline columns.
    """
    df = df_proy.copy()

    # Save baseline
    for c in ["COGS_RES_TOTAL", "VN_RES_TOTAL", "APORTE_RES_TOTAL"]:
        if c in df.columns:
            df[f"{c}_BASE"] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        else:
            df[c] = 0.0
            df[f"{c}_BASE"] = 0.0

    if not active_segments or not discount_map:
        return df

    # Merge segment info
    sku_info = sku_seg[sku_seg["SEGMENTO"].isin(active_segments)].copy()
    if sku_info.empty:
        return df

    sku_info = sku_info.rename(columns={"_ELAST": "_SCN_ELAST", "_DCTO": "_SCN_DCTO_ORIG"})
    # Map segment discount
    sku_info["_SCN_DCTO"] = sku_info["SEGMENTO"].map(discount_map).fillna(0)

    df = df.merge(
        sku_info[["SKU_PRODUCTO", "SEGMENTO", "_SCN_ELAST", "_SCN_DCTO"]],
        on="SKU_PRODUCTO", how="left",
    )
    df["_SCN_ELAST"] = df["_SCN_ELAST"].fillna(0)
    df["_SCN_DCTO"] = df["_SCN_DCTO"].fillna(0)

    # Only modify PROYECCION and REAL+FC rows
    _mask = (
        (df["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"]))
        & (df["_SCN_DCTO"] > 0)
    )

    # Rotation multiplier = 1 + |elasticity| * discount/100
    _rot_mult = 1 + df["_SCN_ELAST"] * df["_SCN_DCTO"] / 100
    _price_mult = 1 - df["_SCN_DCTO"] / 100

    df.loc[_mask, "COGS_RES_TOTAL"] = df.loc[_mask, "COGS_RES_TOTAL_BASE"] * _rot_mult[_mask]
    df.loc[_mask, "VN_RES_TOTAL"] = df.loc[_mask, "VN_RES_TOTAL_BASE"] * _rot_mult[_mask] * _price_mult[_mask]
    df.loc[_mask, "APORTE_RES_TOTAL"] = df.loc[_mask, "VN_RES_TOTAL"] - df.loc[_mask, "COGS_RES_TOTAL"]

    # ── Pause future purchases for liquidation segments ──
    # If a segment has a discount applied, zero out FORECAST_COMPRA for those SKUs
    # (no sense buying more of what you're trying to liquidate)
    _pause_mask = (
        (df["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"]))
        & (df["_SCN_DCTO"] > 0)
    )
    if "FORECAST_COMPRA" in df.columns:
        df.loc[_pause_mask, "FORECAST_COMPRA"] = 0

    df.drop(columns=["SEGMENTO", "_SCN_ELAST", "_SCN_DCTO", "_SCN_DCTO_ORIG"], errors="ignore", inplace=True)

    return df


def _aggregate_scenario_monthly(df_scenario, stock_inicial_clp):
    """Aggregate scenario df to monthly level with iterative stock cascade.

    Returns DataFrame with one row per month, including ETA COMEX/FC split,
    En Agua COMEX/FC, and DOI.
    """
    TRANSIT_DAYS = 60  # default transit time for En Agua estimation

    df = df_scenario.copy()
    df["PERIODO"] = pd.to_datetime(df.get("PERIODO", ""), errors="coerce")
    df = df.dropna(subset=["PERIODO"])

    # Only future periods
    _fc_mask = df["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])
    df_fc = df[_fc_mask].copy()

    if df_fc.empty:
        return pd.DataFrame()

    # Ensure numeric
    for c in ["COGS_RES_TOTAL", "VN_RES_TOTAL", "APORTE_RES_TOTAL",
              "COGS_RES_TOTAL_BASE", "VN_RES_TOTAL_BASE", "APORTE_RES_TOTAL_BASE",
              "FORECAST_COMPRA", "ETA", "COSTO_UNITARIO"]:
        if c in df_fc.columns:
            df_fc[c] = pd.to_numeric(df_fc[c], errors="coerce").fillna(0)
        else:
            df_fc[c] = 0.0

    # Split: ETA COMEX (confirmed POs) vs FC (forecast purchases)
    df_fc["_REC_COMEX_CLP"] = df_fc["ETA"] * df_fc["COSTO_UNITARIO"]
    df_fc["_REC_FC_CLP"] = df_fc["FORECAST_COMPRA"] * df_fc["COSTO_UNITARIO"]
    df_fc["_REC_CLP"] = df_fc["_REC_COMEX_CLP"] + df_fc["_REC_FC_CLP"]

    monthly = df_fc.groupby("PERIODO", as_index=False).agg(
        VENTA_COSTO=("COGS_RES_TOTAL", "sum"),
        VENTA_COSTO_BASE=("COGS_RES_TOTAL_BASE", "sum"),
        VN_TOTAL=("VN_RES_TOTAL", "sum"),
        VN_TOTAL_BASE=("VN_RES_TOTAL_BASE", "sum"),
        APORTE_TOTAL=("APORTE_RES_TOTAL", "sum"),
        APORTE_TOTAL_BASE=("APORTE_RES_TOTAL_BASE", "sum"),
        REC_COMEX=("_REC_COMEX_CLP", "sum"),
        REC_FC=("_REC_FC_CLP", "sum"),
        RECEPCIONES=("_REC_CLP", "sum"),
    ).sort_values("PERIODO").reset_index(drop=True)

    # ── En Agua estimation ──
    # POs arriving in month M sailed ~TRANSIT_DAYS before.
    # "En Agua" for month M = sum of all future arrivals whose ETD ≤ end(M)
    # ETD estimated = arrival month - TRANSIT_DAYS
    _periodos = monthly["PERIODO"].tolist()
    _aguas_comex = []
    _aguas_fc = []
    _aguas_total = []
    for i, p in enumerate(_periodos):
        _fin_p = p + pd.offsets.MonthEnd(0)
        _agua_c = 0.0
        _agua_f = 0.0
        for j in range(i + 1, len(_periodos)):
            _arr = _periodos[j]
            _etd_est = _arr - pd.Timedelta(days=TRANSIT_DAYS)
            if _etd_est <= _fin_p:
                _agua_c += monthly.iloc[j]["REC_COMEX"]
                _agua_f += monthly.iloc[j]["REC_FC"]
        _aguas_comex.append(_agua_c)
        _aguas_fc.append(_agua_f)
        _aguas_total.append(_agua_c + _agua_f)

    monthly["AGUAS_COMEX"] = _aguas_comex
    monthly["AGUAS_FC"] = _aguas_fc
    monthly["AGUAS"] = _aguas_total

    # Iterative stock cascade — baseline
    _stk_base = stock_inicial_clp
    _stk_list_base = []
    for _, r in monthly.iterrows():
        _cierre = max(_stk_base + r["RECEPCIONES"] - r["VENTA_COSTO_BASE"], 0)
        _stk_list_base.append({"STOCK_INI_BASE": _stk_base, "STOCK_CIERRE_BASE": _cierre})
        _stk_base = _cierre
    _stk_base_df = pd.DataFrame(_stk_list_base)

    # Iterative stock cascade — scenario
    _stk_scn = stock_inicial_clp
    _stk_list_scn = []
    for _, r in monthly.iterrows():
        _cierre = max(_stk_scn + r["RECEPCIONES"] - r["VENTA_COSTO"], 0)
        _stk_list_scn.append({"STOCK_INI_SCN": _stk_scn, "STOCK_CIERRE_SCN": _cierre})
        _stk_scn = _cierre
    _stk_scn_df = pd.DataFrame(_stk_list_scn)

    monthly = pd.concat([monthly, _stk_base_df, _stk_scn_df], axis=1)

    # Total Inv (CCC) = Stock Cierre + En Agua
    monthly["TOTAL_INV_BASE"] = monthly["STOCK_CIERRE_BASE"] + monthly["AGUAS"]
    monthly["TOTAL_INV_SCN"] = monthly["STOCK_CIERRE_SCN"] + monthly["AGUAS"]

    # ── DOI (Days of Inventory) = Total Inv / (avg daily COGS rolling 3m) ──
    def _calc_doi_col(total_inv_col, cogs_col):
        doi_vals = []
        for i in range(len(monthly)):
            _lookback = [monthly.iloc[k][cogs_col]
                         for k in range(max(0, i - 2), i + 1)
                         if monthly.iloc[k][cogs_col] > 0]
            _avg_cogs = sum(_lookback) / len(_lookback) if _lookback else 0
            _daily = _avg_cogs / 30 if _avg_cogs > 0 else 0
            _total = monthly.iloc[i][total_inv_col]
            doi_vals.append(round(_total / _daily) if _daily > 0 else 0)
        return doi_vals

    monthly["DOI_BASE"] = _calc_doi_col("TOTAL_INV_BASE", "VENTA_COSTO_BASE")
    monthly["DOI_SCN"] = _calc_doi_col("TOTAL_INV_SCN", "VENTA_COSTO")

    # Label
    monthly["LABEL_MES"] = monthly["PERIODO"].dt.strftime("%b %y")

    return monthly


def _build_scenario_comparison(monthly):
    """Build comparison table: Baseline | Escenario | Delta.

    Returns (DataFrame for display, Styler).
    """
    if monthly.empty:
        return pd.DataFrame(), None

    rows = []
    for _, r in monthly.iterrows():
        _d_vc = r["VENTA_COSTO"] - r["VENTA_COSTO_BASE"]
        _d_stk = r["STOCK_CIERRE_SCN"] - r["STOCK_CIERRE_BASE"]
        _mg_base = r["APORTE_TOTAL_BASE"] / r["VN_TOTAL_BASE"] if r["VN_TOTAL_BASE"] > 0 else 0
        _mg_scn = r["APORTE_TOTAL"] / r["VN_TOTAL"] if r["VN_TOTAL"] > 0 else 0
        rows.append({
            "Mes": r["LABEL_MES"],
            "Stock Ini Base": r["STOCK_INI_BASE"],
            "Stock Ini Escen.": r["STOCK_INI_SCN"],
            "+ ETA COMEX": r.get("REC_COMEX", 0),
            "+ ETA Proy.": r.get("REC_FC", 0),
            "= Recepciones": r["RECEPCIONES"],
            "- Vta Costo Base": r["VENTA_COSTO_BASE"],
            "- Vta Costo Escen.": r["VENTA_COSTO"],
            "Δ Vta Costo": _d_vc,
            "Stock Cierre Base": r["STOCK_CIERRE_BASE"],
            "Stock Cierre Escen.": r["STOCK_CIERRE_SCN"],
            "Δ Stock": _d_stk,
            "+ Agua COMEX": r.get("AGUAS_COMEX", 0),
            "+ Agua FC": r.get("AGUAS_FC", 0),
            "= En Agua": r.get("AGUAS", 0),
            "Total Inv Base": r.get("TOTAL_INV_BASE", 0),
            "Total Inv Escen.": r.get("TOTAL_INV_SCN", 0),
            "DOI Base": r.get("DOI_BASE", 0),
            "DOI Escen.": r.get("DOI_SCN", 0),
            "VN Base": r["VN_TOTAL_BASE"],
            "VN Escen.": r["VN_TOTAL"],
            "Aporte Base": r["APORTE_TOTAL_BASE"],
            "Aporte Escen.": r["APORTE_TOTAL"],
            "Margen Base": _mg_base,
            "Margen Escen.": _mg_scn,
        })

    tbl = pd.DataFrame(rows)

    # Format
    _doi_cols = {"DOI Base", "DOI Escen."}
    _pct_cols = {"Margen Base", "Margen Escen."}
    _skip_cols = {"Mes"} | _doi_cols | _pct_cols
    _clp_cols = [c for c in tbl.columns if c not in _skip_cols]
    _fmt = {}
    for c in _clp_cols:
        _fmt[c] = "${:,.0f}"
    for c in _doi_cols:
        if c in tbl.columns:
            _fmt[c] = "{:.0f}d"
    _fmt["Margen Base"] = "{:.1%}"
    _fmt["Margen Escen."] = "{:.1%}"

    def _highlight_delta(val):
        if "Δ Stock" in str(val):
            return ""
        return ""

    styler = tbl.style.format(_fmt, na_rep="—")

    # Color deltas
    def _color_delta_stock(v):
        if pd.isna(v) or v == 0:
            return ""
        return "color: green" if v < 0 else "color: red"

    def _color_delta_vta(v):
        if pd.isna(v) or v == 0:
            return ""
        return "color: green" if v > 0 else "color: red"

    if "Δ Stock" in tbl.columns:
        styler = styler.map(_color_delta_stock, subset=["Δ Stock"])
    if "Δ Vta Costo" in tbl.columns:
        styler = styler.map(_color_delta_vta, subset=["Δ Vta Costo"])

    return tbl, styler


def _render_scenario_simulator(df_enriched, data):
    """Render the aggregate scenario simulator section."""

    st.markdown("---")
    st.markdown("## 🧪 Simulador de Escenarios — Impacto Agregado")

    # Show active filters indicator
    _active_filters = []
    for _fk, _flab in [
        ("bc_filt_mix", "MIX"), ("bc_filt_linea", "Línea"),
        ("bc_filt_sub", "Sublínea"), ("bc_filt_marca", "Marca"),
        ("bc_filt_accion", "Acción"),
    ]:
        _fv = st.session_state.get(_fk, [])
        if _fv:
            _active_filters.append(f"**{_flab}**: {', '.join(_fv)}")
    if _active_filters:
        st.caption("🔍 Filtros activos: " + " | ".join(_active_filters))
    else:
        st.caption("🔍 Sin filtros — simulando sobre **todos los SKUs** de la línea seleccionada.")

    st.info(
        "Simulación aproximada (lineal). Escala la rotación por elasticidad y descuento "
        "sin re-ejecutar el motor de inventario. Efectos de segundo orden "
        "(e.g., liberar stock CD para otros productos) no se capturan.",
        icon="ℹ️",
    )

    # Check df_proy exists
    df_proy_full = st.session_state.get("df_proy", pd.DataFrame())
    if df_proy_full.empty:
        st.warning(
            "Genera una **Proyección de Stock** primero para poder simular escenarios. "
            "Ve al módulo 'Proyección Stock' y ejecuta la simulación."
        )
        return

    # Filter df_proy to only SKUs in the filtered pool (df_enriched)
    _pool_skus = set(df_enriched["SKU_PRODUCTO"].unique()) if "SKU_PRODUCTO" in df_enriched.columns else set()
    if _pool_skus and "SKU_PRODUCTO" in df_proy_full.columns:
        df_proy = df_proy_full[df_proy_full["SKU_PRODUCTO"].isin(_pool_skus)].copy()
        st.caption(f"📊 Simulando sobre **{len(_pool_skus):,}** SKUs filtrados.")
    else:
        df_proy = df_proy_full.copy()

    # Build segments
    seg_summary, sku_seg = _build_segments(df_enriched)
    if seg_summary.empty:
        st.warning("No hay segmentos para simular.")
        return

    # ── Zone A: Segment cards ──
    st.markdown("### Segmentos")
    _n_seg = len(seg_summary)
    _cols_per_row = min(_n_seg, 4)

    discount_map = {}
    active_segments = set()

    for i in range(0, _n_seg, _cols_per_row):
        _chunk = seg_summary.iloc[i:i + _cols_per_row]
        _cols = st.columns(len(_chunk))
        for j, (_, _sr) in enumerate(_chunk.iterrows()):
            _seg_name = _sr["SEGMENTO"]
            _n_skus = int(_sr["N_SKUS"])
            _stk_mm = _fmt_mm(_sr["STOCK_COSTO"])
            _moi_p = _sr["MOI_PROM"]
            _dcto_def = int(round(_sr["DCTO_SUGERIDO"]))

            with _cols[j]:
                _active = st.checkbox(
                    f"**{_seg_name}**",
                    value=(_dcto_def > 0),
                    key=f"bc_scn_act_{i}_{j}",
                )
                st.caption(f"{_n_skus} SKUs | {_stk_mm} | MOI {_moi_p:.1f}m")
                _dcto = st.slider(
                    "Descuento %",
                    min_value=0, max_value=60,
                    value=_dcto_def if _active else 0,
                    step=5,
                    key=f"bc_scn_dcto_{i}_{j}",
                    disabled=not _active,
                )
                if _active and _dcto > 0:
                    _rot_m = 1 + _sr["ELAST_PROM"] * _dcto / 100
                    st.caption(f"Mult. rotación: **{_rot_m:.2f}×**")
                    active_segments.add(_seg_name)
                    discount_map[_seg_name] = _dcto

    if not active_segments:
        st.info("Activa al menos un segmento para ver el impacto.")
        return

    # ── Apply scenario ──
    with st.spinner("Calculando escenario..."):
        df_scn = _apply_scenario_to_proy(df_proy, sku_seg, discount_map, active_segments)

        # Stock inicial = sum STOCK_COSTO from enriched
        _stock_ini = pd.to_numeric(
            df_enriched.get("STOCK_COSTO", 0), errors="coerce"
        ).fillna(0).sum()

        monthly = _aggregate_scenario_monthly(df_scn, _stock_ini)

    if monthly.empty:
        st.warning("No se pudo generar la tabla mensual. Verifica la proyección.")
        return

    # ── Zone C: Results ──
    st.markdown("### Resultados")

    # Summary KPIs
    _total_vc_base = monthly["VENTA_COSTO_BASE"].sum()
    _total_vc_scn = monthly["VENTA_COSTO"].sum()
    _total_vn_base = monthly["VN_TOTAL_BASE"].sum()
    _total_vn_scn = monthly["VN_TOTAL"].sum()
    _total_ap_base = monthly["APORTE_TOTAL_BASE"].sum()
    _total_ap_scn = monthly["APORTE_TOTAL"].sum()
    _stk_final_base = monthly.iloc[-1]["STOCK_CIERRE_BASE"] if len(monthly) > 0 else 0
    _stk_final_scn = monthly.iloc[-1]["STOCK_CIERRE_SCN"] if len(monthly) > 0 else 0

    _doi_final_base = monthly.iloc[-1].get("DOI_BASE", 0) if len(monthly) > 0 else 0
    _doi_final_scn = monthly.iloc[-1].get("DOI_SCN", 0) if len(monthly) > 0 else 0
    _total_inv_base = monthly.iloc[-1].get("TOTAL_INV_BASE", 0) if len(monthly) > 0 else 0
    _total_inv_scn = monthly.iloc[-1].get("TOTAL_INV_SCN", 0) if len(monthly) > 0 else 0

    _k1, _k2, _k3, _k4, _k5 = st.columns(5)
    _k1.metric(
        "Vta Costo (acum)",
        _fmt_mm(_total_vc_scn),
        delta=f"+{_fmt_mm(_total_vc_scn - _total_vc_base)}" if _total_vc_scn > _total_vc_base else _fmt_mm(_total_vc_scn - _total_vc_base),
        delta_color="normal",
    )
    _k2.metric(
        "Stock Cierre Final",
        _fmt_mm(_stk_final_scn),
        delta=_fmt_mm(_stk_final_scn - _stk_final_base),
        delta_color="inverse",
    )
    _mg_base = _total_ap_base / _total_vn_base if _total_vn_base > 0 else 0
    _mg_scn = _total_ap_scn / _total_vn_scn if _total_vn_scn > 0 else 0
    _k3.metric(
        "Margen Promedio",
        f"{_mg_scn:.1%}",
        delta=f"{(_mg_scn - _mg_base) * 100:+.1f} pp",
        delta_color="normal" if _mg_scn >= _mg_base else "inverse",
    )
    _k4.metric(
        "Total Inv (CCC)",
        _fmt_mm(_total_inv_scn),
        delta=_fmt_mm(_total_inv_scn - _total_inv_base),
        delta_color="inverse",
    )
    _k5.metric(
        "DOI Final",
        f"{_doi_final_scn:.0f} días",
        delta=f"{_doi_final_scn - _doi_final_base:+.0f}d",
        delta_color="inverse",
    )

    # Tabs
    _tab_table, _tab_chart, _tab_seg = st.tabs([
        "📋 Tabla Mensual", "📊 Gráfico Comparativo", "📦 Detalle por Segmento",
    ])

    with _tab_table:
        tbl, styler = _build_scenario_comparison(monthly)
        if styler is not None:
            st.dataframe(styler, use_container_width=True, height=min(len(tbl) * 38 + 60, 500))
        else:
            st.info("Sin datos para mostrar.")

    with _tab_chart:
        _fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            vertical_spacing=0.12, row_heights=[0.5, 0.5],
            subplot_titles=("Venta Costo Mensual: Base vs Escenario",
                            "Evolución Stock: Base vs Escenario"),
        )
        _meses = monthly["LABEL_MES"].tolist()

        # Row 1: Venta Costo
        _fig.add_trace(go.Bar(
            x=_meses, y=monthly["VENTA_COSTO_BASE"],
            name="Vta Costo Base", marker_color=COLORS.get("primary_blue", "#1f4e79"),
            opacity=0.6,
        ), row=1, col=1)
        _fig.add_trace(go.Bar(
            x=_meses, y=monthly["VENTA_COSTO"],
            name="Vta Costo Escenario", marker_color=COLORS.get("status_at_risk", "#FF8C00"),
        ), row=1, col=1)

        # Row 2: Stock evolution
        _fig.add_trace(go.Scatter(
            x=_meses, y=monthly["STOCK_CIERRE_BASE"],
            name="Stock Base", mode="lines+markers",
            line=dict(color=COLORS.get("primary_blue", "#1f4e79"), width=2),
        ), row=2, col=1)
        _fig.add_trace(go.Scatter(
            x=_meses, y=monthly["STOCK_CIERRE_SCN"],
            name="Stock Escenario", mode="lines+markers",
            line=dict(color=COLORS.get("status_at_risk", "#FF8C00"), width=2, dash="dash"),
        ), row=2, col=1)

        # Shade the gap
        _fig.add_trace(go.Scatter(
            x=_meses + _meses[::-1],
            y=monthly["STOCK_CIERRE_BASE"].tolist() + monthly["STOCK_CIERRE_SCN"].tolist()[::-1],
            fill="toself", fillcolor="rgba(0,128,0,0.1)",
            line=dict(width=0), showlegend=False,
            name="Reducción stock",
        ), row=2, col=1)

        _lo = dorel_layout(height=700, margin=dict(t=80))
        _lo["legend"] = dict(orientation="h", yanchor="bottom", y=1.06, x=0.5, xanchor="center")
        _fig.update_layout(**_lo, barmode="group", hovermode="x unified")
        st.plotly_chart(_fig, use_container_width=True)

    with _tab_seg:
        st.markdown("**Impacto acumulado por segmento activado:**")
        for _seg_name in sorted(active_segments):
            _d = discount_map.get(_seg_name, 0)
            _seg_skus = sku_seg[sku_seg["SEGMENTO"] == _seg_name]["SKU_PRODUCTO"].tolist()
            _seg_proy = df_proy[
                (df_proy["SKU_PRODUCTO"].isin(_seg_skus))
                & (df_proy["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"]))
            ]
            _cogs_base = pd.to_numeric(_seg_proy.get("COGS_RES_TOTAL", 0), errors="coerce").fillna(0).sum()
            _vn_base = pd.to_numeric(_seg_proy.get("VN_RES_TOTAL", 0), errors="coerce").fillna(0).sum()

            _seg_info = seg_summary[seg_summary["SEGMENTO"] == _seg_name]
            _elast_p = _seg_info["ELAST_PROM"].iloc[0] if not _seg_info.empty else 1.5
            _rot_m = 1 + _elast_p * _d / 100
            _cogs_new = _cogs_base * _rot_m
            _vn_new = _vn_base * _rot_m * (1 - _d / 100)
            _delta_cogs = _cogs_new - _cogs_base

            st.markdown(
                f"- **{_seg_name}** (dcto {_d}%): "
                f"{len(_seg_skus)} SKUs, "
                f"rotación ×{_rot_m:.2f}, "
                f"Vta Costo {_fmt_mm(_cogs_base)} → {_fmt_mm(_cogs_new)} "
                f"(**+{_fmt_mm(_delta_cogs)}** salida de stock), "
                f"VN {_fmt_mm(_vn_base)} → {_fmt_mm(_vn_new)}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Main render function
# ═══════════════════════════════════════════════════════════════════════════

def render_business_case(conn):
    """Render the Business Case module."""
    st.markdown("## 📊 Caso de Negocio")
    st.caption(
        "Panorama general de inventario, segmentación por acción, simulación de escenarios "
        "y deep dive por SKU. De lo macro a lo micro."
    )

    # ── Load data ──
    # Reload button
    if st.button("🔄 Recargar datos", key="bc_reload"):
        st.cache_data.clear()
        st.session_state.pop("bc_data_loaded", None)
        st.session_state.pop("bc_data", None)

    if "bc_data_loaded" not in st.session_state:
        data = _load_bc_data(conn)
        st.session_state["bc_data"] = data
        st.session_state["bc_data_loaded"] = True
    else:
        data = st.session_state.get("bc_data", {})

    _stock_detail = data.get("stock_detail", pd.DataFrame())
    _sku_db = data.get("sku_db", pd.DataFrame())
    _df_perfil = data.get("df_perfil", pd.DataFrame())
    _abc_xyz = data.get("abc_xyz", pd.DataFrame())
    _ventas_px = data.get("ventas_px", pd.DataFrame())
    _df_tdas_vta = data.get("tdas_vta", pd.DataFrame())
    _primera_venta  = data.get("primera_venta", pd.DataFrame())
    _stock_age_fifo = data.get("stock_age_fifo", pd.DataFrame())

    if _stock_detail.empty:
        st.warning("Sin datos de stock disponibles. Verifica la conexión a Snowflake.")
        return

    # Enrich stock_detail with maestra (AREA, LINEA, SUBLINEA, MARCA, MIX_OFICIAL, etc.)
    try:
        _maestra = norm_cols(cq.maestra(conn))
    except Exception:
        _maestra = st.session_state.get("_pm_maestra_cache", pd.DataFrame())
        if not _maestra.empty:
            _maestra = norm_cols(_maestra)

    if not _maestra.empty and "SKU_PRODUCTO" in _maestra.columns:
        _mae_cols = ["SKU_PRODUCTO"]
        for c in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL",
                   "TAMANO_ALMACENAJE"]:
            if c in _maestra.columns:
                _mae_cols.append(c)
        _mae_dedup = _maestra[_mae_cols].drop_duplicates("SKU_PRODUCTO")

        if "AREA" not in _stock_detail.columns or "LINEA" not in _stock_detail.columns:
            _stock_detail = _stock_detail.merge(_mae_dedup, on="SKU_PRODUCTO", how="left", suffixes=("", "_mae"))
            for _c in ["AREA", "LINEA", "SUBLINEA", "MARCA"]:
                _cM = f"{_c}_mae"
                if _cM in _stock_detail.columns:
                    if _c not in _stock_detail.columns:
                        _stock_detail[_c] = _stock_detail[_cM]
                    else:
                        _stock_detail[_c] = _stock_detail[_c].fillna(_stock_detail[_cM])
                    _stock_detail.drop(columns=[_cM], inplace=True)
        else:
            _extra = [c for c in _mae_cols if c not in _stock_detail.columns]
            if _extra:
                _stock_detail = _stock_detail.merge(
                    _mae_dedup[["SKU_PRODUCTO"] + _extra],
                    on="SKU_PRODUCTO", how="left",
                )

    # Enrich with BC metrics
    with st.spinner("Enriqueciendo datos..."):
        df_enriched = _enrich_bc_data(
            _stock_detail, _sku_db, _df_perfil, _abc_xyz, _ventas_px,
            stock_cd_diario=data.get("stock_cd_diario"),
            df_tdas_vta=_df_tdas_vta,
        )

    # Merge primera venta
    if not _primera_venta.empty and "SKU_PRODUCTO" in _primera_venta.columns:
        _pv = _primera_venta.copy()
        for _pc in ["MESES_EN_CIA"]:
            if _pc in _pv.columns:
                _pv[_pc] = pd.to_numeric(_pv[_pc], errors="coerce").fillna(0)
        if "PRIMERA_VENTA" in _pv.columns:
            _pv["PRIMERA_VENTA"] = pd.to_datetime(_pv["PRIMERA_VENTA"], errors="coerce")
        df_enriched = df_enriched.drop(columns=["PRIMERA_VENTA", "ULTIMA_VENTA", "MESES_EN_CIA"], errors="ignore")
        df_enriched = df_enriched.merge(
            _pv[["SKU_PRODUCTO", "PRIMERA_VENTA", "ULTIMA_VENTA", "MESES_EN_CIA"]].drop_duplicates("SKU_PRODUCTO"),
            on="SKU_PRODUCTO", how="left",
        )
        df_enriched["MESES_EN_CIA"] = df_enriched["MESES_EN_CIA"].fillna(0)
    else:
        if "MESES_EN_CIA" not in df_enriched.columns:
            df_enriched["MESES_EN_CIA"] = 0

    # Merge FIFO stock age — antigüedad del stock físico actual según FIFO comex
    # ANTIGUEDAD_STOCK_MESES: cuándo llegó la unidad más antigua que todavía está en bodega
    # MESES_EN_CIA: cuándo se vendió por primera vez = edad del producto en la compañía
    # Son dos métricas distintas: un SKU puede ser "maduro" (MESES_EN_CIA=18m) pero
    # tener "stock nuevo" (ANTIGUEDAD_STOCK_MESES=2m) si se agotó y se volvió a traer.
    if not _stock_age_fifo.empty and "SKU_PRODUCTO" in _stock_age_fifo.columns:
        _sf = _stock_age_fifo.copy()
        _sf["ANTIGUEDAD_STOCK_MESES"] = pd.to_numeric(
            _sf.get("ANTIGUEDAD_STOCK_MESES", 0), errors="coerce"
        ).fillna(0)
        if "FECHA_STOCK_ANTIGUO" in _sf.columns:
            _sf["FECHA_STOCK_ANTIGUO"] = pd.to_datetime(_sf["FECHA_STOCK_ANTIGUO"], errors="coerce")
        df_enriched = df_enriched.drop(
            columns=["ANTIGUEDAD_STOCK_MESES", "FECHA_STOCK_ANTIGUO", "FECHA_ULTIMA_RECEPCION"],
            errors="ignore"
        )
        df_enriched = df_enriched.merge(
            _sf[["SKU_PRODUCTO", "ANTIGUEDAD_STOCK_MESES", "FECHA_STOCK_ANTIGUO", "FECHA_ULTIMA_RECEPCION"]
                ].drop_duplicates("SKU_PRODUCTO"),
            on="SKU_PRODUCTO", how="left",
        )
        df_enriched["ANTIGUEDAD_STOCK_MESES"] = df_enriched["ANTIGUEDAD_STOCK_MESES"].fillna(0)
    else:
        if "ANTIGUEDAD_STOCK_MESES" not in df_enriched.columns:
            df_enriched["ANTIGUEDAD_STOCK_MESES"] = 0

    # TRAMO_EDAD usa antigüedad del producto (MESES_EN_CIA = primera venta VCM)
    # para determinar si el MOI alto es "justificado" (producto nuevo sin rodaje)
    # o es una señal real de sobrestock (producto maduro que no rota).
    # ANTIGUEDAD_STOCK_MESES queda disponible como columna separada para diagnóstico.
    _mec = pd.to_numeric(df_enriched.get("MESES_EN_CIA", 0), errors="coerce").fillna(0)
    _mask_pv = _mec > 0
    if _mask_pv.any():
        df_enriched.loc[_mask_pv, "ANTIGUEDAD_MESES"] = _mec[_mask_pv]
        _age_bands_fixed = df_enriched["ANTIGUEDAD_MESES"].apply(_classify_age_band)
        df_enriched["TRAMO_EDAD"] = [b[0] for b in _age_bands_fixed]
        # Recalculate SALUD with the corrected age (MESES_EN_CIA).
        # Without this, a Longevo product recently restocked would still show
        # "Nuevo (Alto Stock)" because _enrich_bc_data used the old ANTIGUEDAD_MESES.
        _cls_fixed = df_enriched.apply(
            lambda r: _classify_sku_action(r.get("MOI_HIST", 0), r.get("ANTIGUEDAD_MESES", 0)),
            axis=1,
        )
        df_enriched["SALUD"] = [c[0] for c in _cls_fixed]
        df_enriched["ACCION_RAW"] = [c[1] for c in _cls_fixed]
        df_enriched["_ACCION_ORDER"] = [c[2] for c in _cls_fixed]

    # TRAMO_EDAD_STOCK: tramo según antigüedad física del stock (FIFO)
    # útil para identificar stock que "volvió a llegar" recientemente vs stock varado desde hace tiempo
    _asm = pd.to_numeric(df_enriched.get("ANTIGUEDAD_STOCK_MESES", 0), errors="coerce").fillna(0)
    df_enriched["TRAMO_EDAD_STOCK"] = [_classify_age_band(v)[0] for v in _asm]

    # Store penetration merge
    if not _df_tdas_vta.empty and "SKU_PRODUCTO" in _df_tdas_vta.columns:
        _df_tdas_vta["SKU_PRODUCTO"] = _df_tdas_vta["SKU_PRODUCTO"].astype(str).str.strip()
        for _tc in ["N_TIENDAS_VENTA", "N_TIENDAS_VENTA_3M", "N_MESES_CON_VENTA"]:
            if _tc in _df_tdas_vta.columns:
                _df_tdas_vta[_tc] = pd.to_numeric(_df_tdas_vta[_tc], errors="coerce").fillna(0)
        df_enriched = df_enriched.drop(columns=["N_TIENDAS_VENTA", "N_TIENDAS_VENTA_3M", "N_MESES_CON_VENTA"], errors="ignore")
        df_enriched = df_enriched.merge(
            _df_tdas_vta[["SKU_PRODUCTO", "N_TIENDAS_VENTA", "N_TIENDAS_VENTA_3M", "N_MESES_CON_VENTA"]].drop_duplicates("SKU_PRODUCTO"),
            on="SKU_PRODUCTO", how="left",
        )
        for _tc in ["N_TIENDAS_VENTA", "N_TIENDAS_VENTA_3M", "N_MESES_CON_VENTA"]:
            df_enriched[_tc] = df_enriched[_tc].fillna(0)
    else:
        for _tc in ["N_TIENDAS_VENTA", "N_TIENDAS_VENTA_3M", "N_MESES_CON_VENTA"]:
            if _tc not in df_enriched.columns:
                df_enriched[_tc] = 0

    if df_enriched.empty:
        st.warning("Sin SKUs para analizar.")
        return

    # ── Filters (global, applies to all tabs) ──
    with st.expander("🔎 Filtros", expanded=False):
        _f1, _f2, _f3, _f4, _f5 = st.columns(5)

    with _f1:
        _mix_vals = sorted(
            df_enriched["MIX_OFICIAL"].dropna().astype(str).str.strip().unique()
        ) if "MIX_OFICIAL" in df_enriched.columns else []
        _mix_sel = st.multiselect(
            "Tipo MIX", _mix_vals, default=[], key="bc_filt_mix",
            placeholder="Todos",
        )
    with _f2:
        _linea_vals = sorted(
            df_enriched["LINEA"].dropna().astype(str).str.strip().unique()
        ) if "LINEA" in df_enriched.columns else []
        _linea_sel = st.multiselect(
            "Línea", _linea_vals, default=[], key="bc_filt_linea",
            placeholder="Todas",
        )
    with _f3:
        _sub_vals = sorted(
            df_enriched["SUBLINEA"].dropna().astype(str).str.strip().unique()
        ) if "SUBLINEA" in df_enriched.columns else []
        _sub_sel = st.multiselect(
            "Sublínea", _sub_vals, default=[], key="bc_filt_sub",
            placeholder="Todas",
        )
    with _f4:
        _marca_vals = sorted(
            df_enriched["MARCA"].dropna().astype(str).str.strip().unique()
        ) if "MARCA" in df_enriched.columns else []
        _marca_sel = st.multiselect(
            "Marca", _marca_vals, default=[], key="bc_filt_marca",
            placeholder="Todas",
        )
    with _f5:
        _accion_vals = sorted(
            df_enriched["ACCION_VENTA_RAW"].dropna().astype(str).str.strip().unique()
        ) if "ACCION_VENTA_RAW" in df_enriched.columns else []
        _accion_sel = st.multiselect(
            "Acción", _accion_vals, default=[], key="bc_filt_accion",
            placeholder="Todas",
        )

    # Apply filters
    _pool = df_enriched.copy()
    if _mix_sel and "MIX_OFICIAL" in _pool.columns:
        _pool = _pool[_pool["MIX_OFICIAL"].astype(str).str.strip().isin(_mix_sel)]
    if _linea_sel and "LINEA" in _pool.columns:
        _pool = _pool[_pool["LINEA"].astype(str).str.strip().isin(_linea_sel)]
    if _sub_sel and "SUBLINEA" in _pool.columns:
        _pool = _pool[_pool["SUBLINEA"].astype(str).str.strip().isin(_sub_sel)]
    if _marca_sel and "MARCA" in _pool.columns:
        _pool = _pool[_pool["MARCA"].astype(str).str.strip().isin(_marca_sel)]
    if _accion_sel and "ACCION_VENTA_RAW" in _pool.columns:
        _pool = _pool[_pool["ACCION_VENTA_RAW"].astype(str).str.strip().isin(_accion_sel)]

    _show_sin_stock = st.checkbox(
        "Incluir SKUs sin stock", value=False, key="bc_show_sin_stock",
    )
    if not _show_sin_stock and "STOCK_UNIDADES" in _pool.columns:
        _pool = _pool[pd.to_numeric(_pool["STOCK_UNIDADES"], errors="coerce").fillna(0) > 0]

    if _pool.empty:
        st.info("Sin SKUs para analizar con los filtros seleccionados.")
        return

    # ══════════════════════════════════════════════════════════════════════
    # TABS: Panorama → Segmentación → Simulador → Deep Dive SKU
    # ══════════════════════════════════════════════════════════════════════
    _tab_panorama, _tab_segments, _tab_simulator, _tab_sku, _tab_activacion = st.tabs([
        "📊 Panorama General",
        "🎯 Segmentación por Acción",
        "🧪 Simulador de Escenarios",
        "🔍 Deep Dive SKU",
        "📋 Lista de Activación",
    ])

    # ══════════════════════════════════════════════════════════════════════
    # TAB 1: PANORAMA GENERAL
    # ══════════════════════════════════════════════════════════════════════
    with _tab_panorama:
        # ── Shared computation ──
        for _nc in ["STOCK_UNIDADES", "STOCK_COSTO", "ROTACION_UND_MES", "MOI_HIST", "MOI_FC",
                     "DCTO_SUGERIDO", "MESES_LIQ"]:
            if _nc in _pool.columns:
                _pool[_nc] = pd.to_numeric(_pool[_nc], errors="coerce").fillna(0)

        _total_stock_clp = _pool["STOCK_COSTO"].sum() if "STOCK_COSTO" in _pool.columns else 0
        _total_stock_und = _pool["STOCK_UNIDADES"].sum() if "STOCK_UNIDADES" in _pool.columns else 0
        _total_skus = _pool["SKU_PRODUCTO"].nunique()
        _total_rot = _pool["ROTACION_UND_MES"].sum() if "ROTACION_UND_MES" in _pool.columns else 0
        _moi_hist_global = _total_stock_und / _total_rot if _total_rot > 0 else 0
        _df_proy_bc = st.session_state.get("df_proy", pd.DataFrame())

        # Pre-compute VCM últimos 6 meses completos para MOI Hist por grupo
        # Formula: MOI Hist = stock_CLP / (APORTE_VCM_6m / n_meses)
        _vcm_6m_g = pd.DataFrame()
        _n_vcm_m = 1
        if not _ventas_px.empty and "APORTE" in _ventas_px.columns and "PERIODO" in _ventas_px.columns:
            _vx = _ventas_px.copy()
            _vx["PERIODO"] = pd.to_datetime(_vx["PERIODO"], errors="coerce")
            _vx["APORTE"]  = pd.to_numeric(_vx["APORTE"], errors="coerce").fillna(0)
            _today_m = pd.Timestamp.now().to_period("M").to_timestamp()
            _vx = _vx[_vx["PERIODO"] < _today_m]           # solo meses completos
            _last6 = sorted(_vx["PERIODO"].dropna().unique())[-6:]
            _vx = _vx[_vx["PERIODO"].isin(_last6)]
            _n_vcm_m = max(len(_last6), 1)
            # Adjuntar dimensiones del pool para poder agrupar por PROVEEDOR/MARCA/SUBLINEA etc.
            _dim_vcm = ["SKU_PRODUCTO"] + [
                c for c in ["PROVEEDOR", "MARCA", "SUBLINEA", "LINEA", "TRAMO_EDAD"]
                if c in _pool.columns
            ]
            _vx = _vx.merge(
                _pool[_dim_vcm].drop_duplicates("SKU_PRODUCTO"),
                on="SKU_PRODUCTO", how="inner"   # INNER: solo SKUs del pool actual
            )
            _vcm_6m_g = _vx
            # Recalcular MOI Hist global en base CLP (consistente con la misma fórmula por grupo)
            _vcm_global_aporte_m = _vx["APORTE"].sum() / _n_vcm_m
            if _vcm_global_aporte_m > 0:
                _moi_hist_global = _total_stock_clp / _vcm_global_aporte_m

        _moi_fc_global = 0.0
        if not _df_proy_bc.empty and "SKU_PRODUCTO" in _df_proy_bc.columns:
            _proy_skus_set = set(_pool["SKU_PRODUCTO"])
            _pf = _df_proy_bc[
                _df_proy_bc["SKU_PRODUCTO"].isin(_proy_skus_set) &
                _df_proy_bc["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])
            ].copy() if "TIPO_DATO" in _df_proy_bc.columns else _df_proy_bc[
                _df_proy_bc["SKU_PRODUCTO"].isin(_proy_skus_set)
            ].copy()
            if not _pf.empty and "COGS_RES_TOTAL" in _pf.columns:
                _pf["COGS_RES_TOTAL"] = pd.to_numeric(_pf["COGS_RES_TOTAL"], errors="coerce").fillna(0)
                _n_fc_months = max(_pf["PERIODO"].nunique() if "PERIODO" in _pf.columns else 1, 1)
                _fc_cogs_month = _pf["COGS_RES_TOTAL"].sum() / _n_fc_months
                _moi_fc_global = _total_stock_clp / _fc_cogs_month if _fc_cogs_month > 0 else 0

        # Pre-compute group-level COGS_FC for MOI FC by dimension
        # Formula: MOI_FC_grupo = stock_clp_grupo / (sum(COGS_RES_TOTAL_grupo) / n_meses)
        _n_fc_months_g = 1
        _pf_g = pd.DataFrame()
        if not _df_proy_bc.empty and "COGS_RES_TOTAL" in _df_proy_bc.columns:
            _pf_g = _df_proy_bc.copy()
            if "TIPO_DATO" in _pf_g.columns:
                _pf_g = _pf_g[_pf_g["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])]
            if "PERIODO" in _pf_g.columns:
                _pf_g["PERIODO"] = pd.to_datetime(_pf_g["PERIODO"], errors="coerce")
                _fc_start = pd.Timestamp.now().to_period("M").to_timestamp()
                _fc_end   = _fc_start + pd.DateOffset(months=6)
                _pf_g = _pf_g[(_pf_g["PERIODO"] >= _fc_start) & (_pf_g["PERIODO"] < _fc_end)]
            _pf_g["COGS_RES_TOTAL"] = pd.to_numeric(_pf_g["COGS_RES_TOTAL"], errors="coerce").fillna(0)
            _n_fc_months_g = max(_pf_g["PERIODO"].nunique() if "PERIODO" in _pf_g.columns else 1, 1)
            # Attach group dimensions from _pool — solo las que no vienen ya en df_proy
            _want_dims = ["LINEA", "PROVEEDOR", "TRAMO_EDAD", "MARCA", "SUBLINEA"]
            _missing_dims = [d for d in _want_dims if d in _pool.columns and d not in _pf_g.columns]
            if _missing_dims:
                _pf_g = _pf_g.merge(
                    _pool[["SKU_PRODUCTO"] + _missing_dims].drop_duplicates("SKU_PRODUCTO"),
                    on="SKU_PRODUCTO", how="left"
                )

        def _moi_fc_por_grupo(by_df, group_col, stock_col="Stock_CLP"):
            """MOI FC = stock_grupo / (cogs_fc_grupo / n_meses)
            Usa COGS_RES_TOTAL de df_proy (PROYECCION + REAL+FC).
            """
            if _pf_g.empty or group_col not in _pf_g.columns:
                return np.zeros(len(by_df))
            _cogs_g = (
                _pf_g.groupby(group_col)["COGS_RES_TOTAL"].sum() / _n_fc_months_g
            ).rename("_COGS_FC_M").reset_index()
            _merged = by_df[[group_col, stock_col]].merge(_cogs_g, on=group_col, how="left")
            _merged["_COGS_FC_M"] = _merged["_COGS_FC_M"].fillna(0)
            return np.where(
                _merged["_COGS_FC_M"] > 0,
                _merged[stock_col] / _merged["_COGS_FC_M"],
                0.0,
            )

        def _moi_hist_por_grupo(by_df, group_col, stock_col="Stock_CLP"):
            """MOI Hist grupo = stock_CLP_grupo / (APORTE_VCM_6m_grupo / n_meses).
            'Cuánto stock tengo / cuánto vendo a costo en promedio últimos 6 meses.'
            Fallback: COSTO_PROM_90_CIA si no hay VCM disponible.
            """
            if not _vcm_6m_g.empty and group_col in _vcm_6m_g.columns:
                _cogs_g = (
                    _vcm_6m_g.groupby(group_col)["APORTE"].sum() / _n_vcm_m
                ).rename("_COGS_M").reset_index()
                _merged = by_df[[group_col, stock_col]].merge(_cogs_g, on=group_col, how="left")
                _merged["_COGS_M"] = _merged["_COGS_M"].fillna(0)
                return np.where(_merged["_COGS_M"] > 0, _merged[stock_col] / _merged["_COGS_M"], 0.0)
            # Fallback: COSTO_PROM_90_CIA (90-day rolling daily COGS)
            if "COSTO_PROM_90_CIA" not in _pool.columns or group_col not in _pool.columns:
                return np.zeros(len(by_df))
            _p = _pool[[group_col, "STOCK_COSTO", "COSTO_PROM_90_CIA"]].copy()
            _p["COSTO_PROM_90_CIA"] = pd.to_numeric(_p["COSTO_PROM_90_CIA"], errors="coerce").fillna(0)
            _agg = _p.groupby(group_col).agg(
                _stk=("STOCK_COSTO", "sum"),
                _cogs_dia=("COSTO_PROM_90_CIA", "sum"),
            ).reset_index()
            _agg["_MOI"] = np.where(
                _agg["_cogs_dia"] > 0,
                _agg["_stk"] / (_agg["_cogs_dia"] * 30.44),
                0.0,
            )
            _merged = by_df[[group_col]].merge(_agg[[group_col, "_MOI"]], on=group_col, how="left")
            return _merged["_MOI"].fillna(0).values

        def _pct(v, total):
            return f"{v / total * 100:.0f}%" if total > 0 else "—"

        # ── Criterio de stock crítico: MOI >= 12 AND antigüedad >= 12m ──
        # MOI_CLASIF (ajustado si hay data buena, sino bruto 6m)
        # MESES_EN_CIA (desde primera venta = edad del producto)
        _moi_col = "MOI_CLASIF" if "MOI_CLASIF" in _pool.columns else "MOI_HIST"
        _pool["_MOI_CRIT"] = pd.to_numeric(_pool[_moi_col], errors="coerce").fillna(0)
        _pool["_EDAD_CIA"] = pd.to_numeric(
            _pool["MESES_EN_CIA"] if "MESES_EN_CIA" in _pool.columns else 0,
            errors="coerce",
        ).fillna(0)
        _pool["_ES_CRITICO"] = (_pool["_MOI_CRIT"] >= 12) & (_pool["_EDAD_CIA"] >= 12)

        # ══════════════════════════════════════════════════════════════════════
        # 1. SITUACIÓN ACTUAL — ¿Cómo estamos hoy?
        # ══════════════════════════════════════════════════════════════════════
        st.markdown("### Situación Actual")

        _avg_age = 0.0
        if "MESES_EN_CIA" in _pool.columns:
            _age_vals = pd.to_numeric(_pool["MESES_EN_CIA"], errors="coerce").dropna()
            _age_vals = _age_vals[_age_vals > 0]
            if not _age_vals.empty:
                _age_weights = _pool.loc[_age_vals.index, "STOCK_COSTO"]
                _avg_age = (_age_vals * _age_weights).sum() / _age_weights.sum() if _age_weights.sum() > 0 else _age_vals.mean()

        # Antigüedad física del stock (FIFO) — para contextualizar MOI
        _asm_col = pd.to_numeric(_pool.get("ANTIGUEDAD_STOCK_MESES", 0), errors="coerce").fillna(0)
        _avg_stock_age = 0.0
        if _total_stock_clp > 0 and _asm_col.sum() > 0:
            _asm_weights = _pool["STOCK_COSTO"]
            _avg_stock_age = (_asm_col * _asm_weights).sum() / _asm_weights.sum()
        # Clasificar stock crítico (MOI>=12) por antigüedad FIFO
        _crit_pool = _pool[_pool["_ES_CRITICO"]].copy() if "_ES_CRITICO" in _pool.columns else pd.DataFrame()
        _crit_asm = pd.to_numeric(_crit_pool.get("ANTIGUEDAD_STOCK_MESES", 0), errors="coerce").fillna(0) if not _crit_pool.empty else pd.Series(dtype=float)
        _crit_stock_nuevo = _crit_pool.loc[_crit_asm < 3, "STOCK_COSTO"].sum() if not _crit_pool.empty else 0   # <3m → dar tiempo
        _crit_stock_viejo = _crit_pool.loc[_crit_asm >= 3, "STOCK_COSTO"].sum() if not _crit_pool.empty else 0  # >=3m → problema real

        _kc = st.columns(4)
        _kc[0].metric("Stock Total", _fmt_mm(_total_stock_clp), f"{_total_stock_und:,.0f} und · {_total_skus:,} SKUs")
        _kc[1].metric(
            "MOI Histórico",
            f"{_moi_hist_global:.1f}m",
            f"venta/costo 6m · stock físico {_avg_stock_age:.0f}m promedio",
            delta_color="off",
        )
        _kc[2].metric(
            "MOI Forecast",
            f"{_moi_fc_global:.1f}m" if _moi_fc_global > 0 else "—",
            (f"{'↓' if _moi_fc_global < _moi_hist_global else '↑'} vs hist" if _moi_fc_global > 0 else "sin proyección"),
            delta_color=("normal" if _moi_fc_global < _moi_hist_global else "inverse") if _moi_fc_global > 0 else "off",
        )
        _kc[3].metric("Antigüedad Prom.", f"{_avg_age:.0f} meses" if _avg_age > 0 else "—", "producto · desde 1ª venta", delta_color="off")

        _mask_h_sal = _pool["MOI_HIST"].between(0.1, 6)
        _mask_h_sob = _pool["MOI_HIST"].between(6, 12)
        _mask_h_cri = _pool["_ES_CRITICO"]
        _stk_h_sal = _pool.loc[_mask_h_sal, "STOCK_COSTO"].sum()
        _stk_h_sob = _pool.loc[_mask_h_sob, "STOCK_COSTO"].sum()
        _stk_h_cri = _pool.loc[_mask_h_cri, "STOCK_COSTO"].sum()
        _mask_f_sal = _pool["MOI_FC"].between(0.1, 6)
        _mask_f_sob = _pool["MOI_FC"].between(6, 12)
        _mask_f_cri = _pool["MOI_FC"] >= 12
        _stk_f_sal = _pool.loc[_mask_f_sal, "STOCK_COSTO"].sum()
        _stk_f_sob = _pool.loc[_mask_f_sob, "STOCK_COSTO"].sum()
        _stk_f_cri = _pool.loc[_mask_f_cri, "STOCK_COSTO"].sum()

        _h_col, _f_col = st.columns(2)
        with _h_col:
            st.markdown("**Según Historia (6m)**")
            _kh = st.columns(3)
            _kh[0].metric("✅ Saludable", _pct(_stk_h_sal, _total_stock_clp), _fmt_mm(_stk_h_sal))
            _kh[1].metric("⚠️ Sobrestock", _pct(_stk_h_sob, _total_stock_clp), _fmt_mm(_stk_h_sob))
            _kh[2].metric("🔴 Crítico", _pct(_stk_h_cri, _total_stock_clp), _fmt_mm(_stk_h_cri))
        with _f_col:
            st.markdown("**Según Forecast**")
            _kf = st.columns(3)
            _kf[0].metric("✅ Saludable", _pct(_stk_f_sal, _total_stock_clp), _fmt_mm(_stk_f_sal))
            _kf[1].metric("⚠️ Sobrestock", _pct(_stk_f_sob, _total_stock_clp), _fmt_mm(_stk_f_sob))
            _kf[2].metric("🔴 Crítico", _pct(_stk_f_cri, _total_stock_clp), _fmt_mm(_stk_f_cri))

        # Contexto antigüedad: desglosar crítico por si el stock es realmente viejo o recién llegó
        if _stk_h_cri > 0 and _crit_stock_viejo + _crit_stock_nuevo > 0:
            _total_crit_fifo = _crit_stock_viejo + _crit_stock_nuevo
            st.caption(
                f"🔴 Del stock crítico: "
                f"**{_crit_stock_viejo / _total_crit_fifo * 100:.0f}% llegó hace >3 meses** ({_fmt_mm(_crit_stock_viejo)}) — problema real · "
                f"{_crit_stock_nuevo / _total_crit_fifo * 100:.0f}% llegó hace <3 meses ({_fmt_mm(_crit_stock_nuevo)}) — dar rodaje"
            )

        # ── Comparación MOI Bruto vs Ajustado ──
        if "MOI_PARA_CLASIF" in _pool.columns:
            _moi_clasif_col = pd.to_numeric(_pool["MOI_PARA_CLASIF"], errors="coerce").fillna(0)
            _mask_adj_cri = (_moi_clasif_col >= 12) & (_pool["_EDAD_CIA"] >= 12)
            _mask_adj_sal = _moi_clasif_col.between(0.1, 6)
            _mask_adj_sob = _moi_clasif_col.between(6, 12)
            _stk_adj_cri = _pool.loc[_mask_adj_cri, "STOCK_COSTO"].sum()
            _stk_adj_sal = _pool.loc[_mask_adj_sal, "STOCK_COSTO"].sum()
            _stk_adj_sob = _pool.loc[_mask_adj_sob, "STOCK_COSTO"].sum()

            # SKUs que cambian clasificación
            _falso_crit = _mask_h_cri & ~_mask_adj_cri  # bruto dice crítico, ajustado no
            _nuevo_crit = ~_mask_h_cri & _mask_adj_cri  # bruto no crítico, ajustado sí
            _n_falso = _falso_crit.sum()
            _n_nuevo = _nuevo_crit.sum()
            _clp_falso = _pool.loc[_falso_crit, "STOCK_COSTO"].sum()
            _clp_nuevo = _pool.loc[_nuevo_crit, "STOCK_COSTO"].sum()

            with st.expander(
                f"🔍 Impacto MOI Ajustado: crítico {_fmt_mm(_stk_h_cri)} (bruto) → {_fmt_mm(_stk_adj_cri)} (ajustado) · "
                f"{_n_falso} salen, {_n_nuevo} entran",
                expanded=False,
            ):
                    st.markdown(
                        f"**Al usar MOI Ajustado** (corrige demanda censurada por falta de stock CD), "
                        f"el stock crítico cambia de **{_fmt_mm(_stk_h_cri)}** a **{_fmt_mm(_stk_adj_cri)}** "
                        f"(delta {_fmt_mm(_stk_adj_cri - _stk_h_cri)})."
                    )
                    _ca, _cb = st.columns(2)
                    if _n_falso > 0:
                        _ca.markdown(
                            f"**🟢 Salen de crítico** ({_n_falso} SKUs, {_fmt_mm(_clp_falso)})\n\n"
                            f"MOI bruto ≥12m pero ajustado <12m. La venta estaba "
                            f"censurada por bajo stock CD → MOI inflado."
                        )
                        _df_falso = _pool.loc[_falso_crit, [
                            "SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "STOCK_COSTO",
                            "MOI_HIST", "MOI_PARA_CLASIF", "CONFIABILIDAD_MOI",
                            "MESES_CON_VENTA", "MESES_LOOKBACK",
                        ]].copy()
                        _df_falso.columns = [
                            "SKU", "Producto", "Stock $", "MOI Bruto",
                            "MOI Ajust", "Confiab", "Meses c/Stock", "Meses LB",
                        ]
                        _df_falso = _df_falso.sort_values("Stock $", ascending=False)
                        _ca.dataframe(_df_falso, use_container_width=True, hide_index=True)
                    else:
                        _ca.info("Ningún SKU sale de crítico con el ajuste.")
                    if _n_nuevo > 0:
                        _cb.markdown(
                            f"**🔴 Entran a crítico** ({_n_nuevo} SKUs, {_fmt_mm(_clp_nuevo)})\n\n"
                            f"MOI bruto <12m pero ajustado ≥12m. Meses con buen stock "
                            f"muestran menor rotación real."
                        )
                        _df_nuevo = _pool.loc[_nuevo_crit, [
                            "SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "STOCK_COSTO",
                            "MOI_HIST", "MOI_PARA_CLASIF", "CONFIABILIDAD_MOI",
                            "MESES_CON_VENTA", "MESES_LOOKBACK",
                        ]].copy()
                        _df_nuevo.columns = [
                            "SKU", "Producto", "Stock $", "MOI Bruto",
                            "MOI Ajust", "Confiab", "Meses c/Stock", "Meses LB",
                        ]
                        _df_nuevo = _df_nuevo.sort_values("Stock $", ascending=False)
                        _cb.dataframe(_df_nuevo, use_container_width=True, hide_index=True)
                    else:
                        _cb.info("Ningún SKU nuevo entra a crítico con el ajuste.")

        if "MESES_EN_CIA" in _pool.columns and _total_stock_clp > 0:
            _age_col_sia = pd.to_numeric(_pool["MESES_EN_CIA"], errors="coerce").fillna(0)
            _stk_new = _pool.loc[_age_col_sia <= 6, "STOCK_COSTO"].sum()
            _stk_mid = _pool.loc[_age_col_sia.between(6, 18), "STOCK_COSTO"].sum()
            _stk_old = _pool.loc[_age_col_sia > 18, "STOCK_COSTO"].sum()
            st.caption(
                f"📅 Producto en compañía (desde 1ª venta): "
                f"**{_stk_new / _total_stock_clp * 100:.0f}%** nuevos (<6m) | "
                f"**{_stk_mid / _total_stock_clp * 100:.0f}%** intermedios (6-18m) | "
                f"**{_stk_old / _total_stock_clp * 100:.0f}%** maduros (>18m)"
            )

        st.divider()

        # ══════════════════════════════════════════════════════════════════════
        # 2. ANTIGÜEDAD — ¿Qué tan antiguo es y cómo rota?
        # ══════════════════════════════════════════════════════════════════════
        st.markdown("### 2. ¿Qué tan antiguo es y cómo rota?")
        st.caption(
            "La **antigüedad del producto** (desde 1ª venta) dice si tiene rodaje para juzgar su MOI. "
            "La **antigüedad del stock físico** (FIFO comex) dice si las unidades actuales son recientes o llevan tiempo varadas. "
            "MOI alto + producto maduro + stock viejo = problema real sin excusas."
        )

        with st.expander("📖 Diccionario — Estados de Salud"):
            st.markdown("""
| Estado | Color | MOI referencial | Interpretación | Acción sugerida |
|---|---|---|---|---|
| 🟢 **Saludable** | Verde | 2 – 4m | Rotación ideal. Stock en línea con la demanda. | Mantener |
| 🔵 **Bajo Stock** | Azul | < 2m | Riesgo de quiebre inminente. Poco margen de maniobra. | Comprar más |
| 🔵 **Monitorear** | Azul cielo | 4 – 6m | Stock levemente alto pero dentro del rango aceptable. | Reducir próximas compras |
| 🟡 **Sobreinventariado** | Amarillo | 6 – 12m | Stock excesivo vs demanda histórica. Capital inmovilizado. | Pausar compras |
| 🟠 **Riesgo Obsolescencia** | Naranja | > 12m (producto maduro) | MOI alto en SKU que ya tiene rodaje. Sin excusa de "falta de tiempo". | Pausar + evaluar liquidar |
| 🔴 **Acción Urgente** | Rojo | > 12m + producto > 12m en cía. | MOI crítico y el producto ya es maduro. Riesgo real de obsolescencia. | Acción inmediata: markdown o liquidación |
| 🟠 **Riesgo Temprano** | Naranja | ≥ 9m (producto 3-6m) | Baja rotación en etapa de lanzamiento. Señal temprana de problema. | Pausar + revisar forecast |
| 🟡 **Nuevo Sin Rotación** | Amarillo | sin ventas (< 3m) | SKU nuevo sin ninguna venta aún. Puede ser normal si acaba de llegar. | Observar, dar tiempo |
| 🔵 **Nuevo (Alto Stock)** | Azul | ≥ 12m (< 3m en cía.) | MOI alto pero es un producto nuevo — puede ser normal. No penalizar. | Monitorear |
| 🟢 **Nuevo (Normal)** | Verde claro | 6 – 12m (< 3m en cía.) | Stock razonable para un lanzamiento. | OK |
| 🟢 **Nuevo (Activo)** | Verde | < 6m (< 3m en cía.) | Buen arranque. Rotando bien desde el inicio. | OK |

> **Nota MOI:** Los estados de productos **nuevos (<3m desde 1ª venta)** usan umbrales más permisivos porque la demanda aún está estableciéndose.
> Un MOI de 15m en un producto nuevo no es lo mismo que en uno con 2 años en la compañía.
            """)


        _age_tab_prod, _age_tab_stock = st.tabs([
            "📅 Por antigüedad del producto", "📦 Por antigüedad del stock físico (FIFO)"
        ])

        _age_order_map = {v: i for i, v in enumerate(_AGE_BAND_ORDER)}

        with _age_tab_prod:
            if "TRAMO_EDAD" in _pool.columns and "ANTIGUEDAD_MESES" in _pool.columns:
                _age_col = pd.to_numeric(_pool["ANTIGUEDAD_MESES"], errors="coerce").fillna(0)

                # Alerts
                _nuevos_sin_rot = _pool[
                    (_age_col < 3) &
                    (pd.to_numeric(_pool.get("ROTACION_UND_MES", 0), errors="coerce").fillna(0) == 0)
                ]
                if not _nuevos_sin_rot.empty:
                    st.warning(
                        f"⚠️ **{len(_nuevos_sin_rot)} SKUs nuevos (<3m) sin rotación** — "
                        f"{_fmt_mm(_nuevos_sin_rot['STOCK_COSTO'].sum())} inmovilizado. Dar rodaje."
                    )
                _recientes_riesgo = _pool[
                    (_age_col >= 3) & (_age_col < 6) &
                    (_pool.get("SALUD", pd.Series("", index=_pool.index)) == "Riesgo Temprano")
                ]
                if not _recientes_riesgo.empty:
                    st.error(
                        f"🚨 **{len(_recientes_riesgo)} SKUs con 3-6m y Riesgo Temprano** — "
                        f"{_fmt_mm(_recientes_riesgo['STOCK_COSTO'].sum())}. Baja rotación en etapa de lanzamiento."
                    )

                # Aggregate
                _by_age = _pool.groupby("TRAMO_EDAD").agg(
                    SKUs=("SKU_PRODUCTO", "nunique"),
                    Stock_CLP=("STOCK_COSTO", "sum"),
                ).reset_index()
                _by_age["MOI_HIST"] = _moi_hist_por_grupo(_by_age, "TRAMO_EDAD")
                _by_age["MOI_FC"]   = _moi_fc_por_grupo(_by_age, "TRAMO_EDAD")
                _crit_by_age = _pool[_pool["_ES_CRITICO"]].groupby("TRAMO_EDAD")["STOCK_COSTO"].sum().rename("Critico_CLP")
                _by_age = _by_age.merge(_crit_by_age, on="TRAMO_EDAD", how="left").fillna({"Critico_CLP": 0})
                _by_age["Pct_Critico"] = np.where(_by_age["Stock_CLP"] > 0, _by_age["Critico_CLP"] / _by_age["Stock_CLP"] * 100, 0)
                # % stock físico viejo (FIFO >= 3m) por tramo de producto
                if "ANTIGUEDAD_STOCK_MESES" in _pool.columns:
                    _fifo_viejo = _pool[
                        pd.to_numeric(_pool["ANTIGUEDAD_STOCK_MESES"], errors="coerce").fillna(0) >= 3
                    ].groupby("TRAMO_EDAD")["STOCK_COSTO"].sum().rename("Stock_Fisico_Viejo")
                    _by_age = _by_age.merge(_fifo_viejo, on="TRAMO_EDAD", how="left").fillna({"Stock_Fisico_Viejo": 0})
                    _by_age["Pct_Fisico_Viejo"] = np.where(_by_age["Stock_CLP"] > 0, _by_age["Stock_Fisico_Viejo"] / _by_age["Stock_CLP"] * 100, 0)
                else:
                    _by_age["Pct_Fisico_Viejo"] = 0

                _by_age["_ord"] = _by_age["TRAMO_EDAD"].map(_age_order_map).fillna(99)
                _by_age = _by_age.sort_values("_ord").drop(columns=["_ord"])

                # Chart
                _age_salud_agg = _pool.groupby(["TRAMO_EDAD", "SALUD"])["STOCK_COSTO"].sum().reset_index()
                _age_salud_agg["_ord"] = _age_salud_agg["TRAMO_EDAD"].map(_age_order_map).fillna(99)
                _age_salud_agg = _age_salud_agg.sort_values("_ord")
                _fig_age = go.Figure()
                for _salud in _age_salud_agg["SALUD"].unique():
                    _sd = _age_salud_agg[_age_salud_agg["SALUD"] == _salud]
                    _fig_age.add_trace(go.Bar(
                        name=_salud, x=_sd["TRAMO_EDAD"], y=_sd["STOCK_COSTO"] / 1_000_000,
                        marker_color=_SALUD_COLOR.get(_salud, "#9ca3af"),
                        hovertemplate="%{x}<br>" + _salud + ": $%{y:.1f}M<extra></extra>",
                    ))
                _fig_age.update_layout(
                    barmode="stack",
                    title=dict(text="Stock por Antigüedad del Producto × Estado de Salud", x=0.5, font_size=14),
                    xaxis_title="Antigüedad del producto (desde 1ª venta)",
                    yaxis_title="Stock (M$)", height=340,
                    legend=dict(orientation="h", yanchor="bottom", y=1.02),
                    plot_bgcolor="#FAFAFA", paper_bgcolor="#FFFFFF",
                )
                st.plotly_chart(_fig_age, use_container_width=True)

                # Table
                _age_disp = _by_age.copy()
                _age_disp["Stock ($)"]      = _age_disp["Stock_CLP"].apply(_fmt_mm)
                _age_disp["% Total"]        = (_age_disp["Stock_CLP"] / _total_stock_clp * 100).apply(lambda x: f"{x:.0f}%")
                _age_disp["MOI Hist"]       = _age_disp["MOI_HIST"].apply(lambda x: f"{x:.1f}m" if x > 0 else "—")
                _age_disp["MOI FC"]         = _age_disp["MOI_FC"].apply(lambda x: f"{x:.1f}m" if x > 0 else "—")
                _age_disp["% Crítico"]      = _age_disp["Pct_Critico"].apply(lambda x: f"{x:.0f}%")
                _age_disp["% Stock Viejo"]  = _age_disp["Pct_Fisico_Viejo"].apply(lambda x: f"{x:.0f}%" if x > 0 else "—")
                st.caption("**% Stock Viejo**: del stock en ese tramo, qué porcentaje llegó hace >3m (FIFO). MOI alto + stock viejo = problema real, no falta de rodaje.")
                st.dataframe(
                    _age_disp[["TRAMO_EDAD", "SKUs", "Stock ($)", "% Total", "MOI Hist", "MOI FC", "% Crítico", "% Stock Viejo"]].rename(
                        columns={"TRAMO_EDAD": "Antigüedad Producto"}
                    ),
                    use_container_width=True, hide_index=True,
                )

                # Drill-down SKU
                with st.expander("🔍 Ver SKUs por tramo de antigüedad"):
                    _dc1, _dc2 = st.columns(2)
                    with _dc1:
                        _tramos_disp = ["Todos"] + [t for t in _AGE_BAND_ORDER if t in _pool["TRAMO_EDAD"].unique()]
                        _tramo_sel = st.selectbox("Tramo antigüedad producto", _tramos_disp, key="bc_drill_tramo_prod")
                    with _dc2:
                        _salud_opts = ["Todos"] + sorted(_pool["SALUD"].dropna().unique().tolist()) if "SALUD" in _pool.columns else ["Todos"]
                        _salud_sel = st.selectbox("Estado de salud", _salud_opts, key="bc_drill_salud_prod")
                    _drill = _pool.copy()
                    if _tramo_sel != "Todos":
                        _drill = _drill[_drill["TRAMO_EDAD"] == _tramo_sel]
                    if _salud_sel != "Todos" and "SALUD" in _drill.columns:
                        _drill = _drill[_drill["SALUD"] == _salud_sel]
                    for _nc in ["STOCK_COSTO", "STOCK_UNIDADES", "MOI_HIST", "MOI_FC", "ROTACION_UND_MES", "ANTIGUEDAD_MESES", "ANTIGUEDAD_STOCK_MESES"]:
                        if _nc in _drill.columns:
                            _drill[_nc] = pd.to_numeric(_drill[_nc], errors="coerce").fillna(0)
                    _drill["STOCK_MM"] = (_drill["STOCK_COSTO"] / 1_000_000).round(1)
                    _drill_cols = [c for c in ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "LINEA", "MARCA",
                                               "ANTIGUEDAD_MESES", "TRAMO_EDAD", "ANTIGUEDAD_STOCK_MESES",
                                               "SALUD", "STOCK_UNIDADES", "STOCK_MM",
                                               "MOI_HIST", "MOI_FC", "ROTACION_UND_MES", "ACCION_VENTA_RAW"] if c in _drill.columns]
                    st.caption(f"{len(_drill)} SKUs")
                    st.dataframe(
                        _drill[_drill_cols].sort_values("STOCK_MM", ascending=False),
                        use_container_width=True, hide_index=True,
                        column_config={
                            "STOCK_MM": st.column_config.NumberColumn("Stock (M$)", format="%.1f"),
                            "STOCK_UNIDADES": st.column_config.NumberColumn("Stock (und)", format="%,.0f"),
                            "MOI_HIST": st.column_config.NumberColumn("MOI Hist", format="%.1f"),
                            "MOI_FC": st.column_config.NumberColumn("MOI FC", format="%.1f"),
                            "ROTACION_UND_MES": st.column_config.NumberColumn("Rot/mes", format="%.1f"),
                            "ANTIGUEDAD_MESES": st.column_config.NumberColumn("Edad Prod (m)", format="%.0f"),
                            "ANTIGUEDAD_STOCK_MESES": st.column_config.NumberColumn("Edad Stock FIFO (m)", format="%.0f"),
                            "TRAMO_EDAD": st.column_config.TextColumn("Tramo"),
                            "SALUD": st.column_config.TextColumn("Salud"),
                            "ACCION_VENTA_RAW": st.column_config.TextColumn("Acción"),
                        },
                    )
            else:
                st.info("Sin datos de antigüedad disponibles.")

        with _age_tab_stock:
            if "TRAMO_EDAD_STOCK" in _pool.columns and not _stock_age_fifo.empty:
                st.caption(
                    "¿Cuándo llegó la unidad más antigua que está hoy en bodega? (FIFO sobre comex) "
                    "Stock **Nuevo (<3m)** → MOI alto puede ser prematuro, dar tiempo. "
                    "Stock **Maduro (>12m)** con MOI alto → acumulación real, sin excusas."
                )
                if "ANTIGUEDAD_STOCK_MESES" in _pool.columns and "ANTIGUEDAD_MESES" in _pool.columns:
                    _varados = _pool[
                        (pd.to_numeric(_pool["ANTIGUEDAD_STOCK_MESES"], errors="coerce").fillna(0) > 6) &
                        (pd.to_numeric(_pool["ANTIGUEDAD_MESES"], errors="coerce").fillna(0) > 12)
                    ]
                    if not _varados.empty:
                        st.warning(
                            f"📦 **{len(_varados)} SKUs** con stock físico >6m y producto maduro (>12m en compañía) — "
                            f"{_fmt_mm(_varados['STOCK_COSTO'].sum())} posiblemente varado desde recepciones anteriores."
                        )
                _by_stk_age = _pool.groupby("TRAMO_EDAD_STOCK").agg(
                    SKUs=("SKU_PRODUCTO", "nunique"),
                    Stock_CLP=("STOCK_COSTO", "sum"),
                ).reset_index()
                _by_stk_age["MOI_HIST"] = _moi_hist_por_grupo(_by_stk_age, "TRAMO_EDAD_STOCK")
                _by_stk_age["MOI_FC"]   = _moi_fc_por_grupo(_by_stk_age, "TRAMO_EDAD_STOCK")
                _crit_stk = _pool[_pool["_ES_CRITICO"]].groupby("TRAMO_EDAD_STOCK")["STOCK_COSTO"].sum().rename("Critico_CLP")
                _by_stk_age = _by_stk_age.merge(_crit_stk, on="TRAMO_EDAD_STOCK", how="left").fillna({"Critico_CLP": 0})
                _by_stk_age["Pct_Critico"] = np.where(_by_stk_age["Stock_CLP"] > 0, _by_stk_age["Critico_CLP"] / _by_stk_age["Stock_CLP"] * 100, 0)
                _by_stk_age["_ord"] = _by_stk_age["TRAMO_EDAD_STOCK"].map(_age_order_map).fillna(99)
                _by_stk_age = _by_stk_age.sort_values("_ord").drop(columns=["_ord"])
                _bsa_disp = _by_stk_age.copy()
                _bsa_disp["Stock ($)"]   = _bsa_disp["Stock_CLP"].apply(_fmt_mm)
                _bsa_disp["% Total"]     = (_bsa_disp["Stock_CLP"] / _total_stock_clp * 100).apply(lambda x: f"{x:.0f}%")
                _bsa_disp["MOI Hist"]    = _bsa_disp["MOI_HIST"].apply(lambda x: f"{x:.1f}m" if x > 0 else "—")
                _bsa_disp["MOI FC"]      = _bsa_disp["MOI_FC"].apply(lambda x: f"{x:.1f}m" if x > 0 else "—")
                _bsa_disp["% Crítico"]   = _bsa_disp["Pct_Critico"].apply(lambda x: f"{x:.0f}%")
                st.dataframe(
                    _bsa_disp[["TRAMO_EDAD_STOCK", "SKUs", "Stock ($)", "% Total", "MOI Hist", "MOI FC", "% Crítico"]].rename(
                        columns={"TRAMO_EDAD_STOCK": "Antigüedad Stock Físico (FIFO)"}
                    ),
                    use_container_width=True, hide_index=True,
                )

                # Drill-down SKU por stock físico
                with st.expander("🔍 Ver SKUs por antigüedad del stock físico (FIFO)"):
                    _fc1, _fc2 = st.columns(2)
                    with _fc1:
                        _tramos_fifo = ["Todos"] + [t for t in _AGE_BAND_ORDER if t in _pool["TRAMO_EDAD_STOCK"].unique()]
                        _tramo_fifo_sel = st.selectbox("Tramo stock físico (FIFO)", _tramos_fifo, key="bc_drill_tramo_fifo")
                    with _fc2:
                        _salud_fifo_opts = ["Todos"] + sorted(_pool["SALUD"].dropna().unique().tolist()) if "SALUD" in _pool.columns else ["Todos"]
                        _salud_fifo_sel = st.selectbox("Estado de salud", _salud_fifo_opts, key="bc_drill_salud_fifo")
                    _drillf = _pool.copy()
                    if _tramo_fifo_sel != "Todos":
                        _drillf = _drillf[_drillf["TRAMO_EDAD_STOCK"] == _tramo_fifo_sel]
                    if _salud_fifo_sel != "Todos" and "SALUD" in _drillf.columns:
                        _drillf = _drillf[_drillf["SALUD"] == _salud_fifo_sel]
                    for _nc in ["STOCK_COSTO", "STOCK_UNIDADES", "MOI_HIST", "MOI_FC", "ANTIGUEDAD_MESES", "ANTIGUEDAD_STOCK_MESES"]:
                        if _nc in _drillf.columns:
                            _drillf[_nc] = pd.to_numeric(_drillf[_nc], errors="coerce").fillna(0)
                    _drillf["STOCK_MM"] = (_drillf["STOCK_COSTO"] / 1_000_000).round(1)
                    _drillf_cols = [c for c in ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "LINEA", "MARCA",
                                                "ANTIGUEDAD_STOCK_MESES", "TRAMO_EDAD_STOCK",
                                                "FECHA_STOCK_ANTIGUO", "FECHA_ULTIMA_RECEPCION",
                                                "ANTIGUEDAD_MESES", "TRAMO_EDAD",
                                                "SALUD", "STOCK_UNIDADES", "STOCK_MM",
                                                "MOI_HIST", "MOI_FC", "ACCION_VENTA_RAW"] if c in _drillf.columns]
                    st.caption(f"{len(_drillf)} SKUs")
                    st.dataframe(
                        _drillf[_drillf_cols].sort_values("STOCK_MM", ascending=False),
                        use_container_width=True, hide_index=True,
                        column_config={
                            "STOCK_MM": st.column_config.NumberColumn("Stock (M$)", format="%.1f"),
                            "STOCK_UNIDADES": st.column_config.NumberColumn("Stock (und)", format="%,.0f"),
                            "MOI_HIST": st.column_config.NumberColumn("MOI Hist", format="%.1f"),
                            "MOI_FC": st.column_config.NumberColumn("MOI FC", format="%.1f"),
                            "ANTIGUEDAD_MESES": st.column_config.NumberColumn("Edad Prod (m)", format="%.0f"),
                            "ANTIGUEDAD_STOCK_MESES": st.column_config.NumberColumn("Edad Stock FIFO (m)", format="%.0f"),
                            "TRAMO_EDAD": st.column_config.TextColumn("Tramo Prod"),
                            "TRAMO_EDAD_STOCK": st.column_config.TextColumn("Tramo Stock"),
                            "SALUD": st.column_config.TextColumn("Salud"),
                            "ACCION_VENTA_RAW": st.column_config.TextColumn("Acción"),
                        },
                    )
            elif _stock_age_fifo.empty:
                st.info("Sin datos FIFO disponibles. Verifica la conexión a Snowflake o recarga el módulo.")

        st.divider()

        # ══════════════════════════════════════════════════════════════════════
        # 3. MIX — ¿Qué calidad comercial tiene el stock?
        # ══════════════════════════════════════════════════════════════════════
        st.markdown("### 3. ¿Qué calidad comercial tiene?")
        st.caption(
            "El tipo de MIX define el destino comercial del producto. "
            "**MIX / IN & OUT** con alto MOI → problema de gestión (demanda o compras). "
            "**FUERA DE MIX / DESCONTINUAR** con alto MOI → capital sin salida comercial, requiere liquidación."
        )
        if "MIX_OFICIAL" in _pool.columns:
            _by_mix = _pool.groupby("MIX_OFICIAL").agg(
                SKUs=("SKU_PRODUCTO", "nunique"),
                Stock_CLP=("STOCK_COSTO", "sum"),
            ).reset_index()
            _by_mix["MOI_HIST"] = _moi_hist_por_grupo(_by_mix, "MIX_OFICIAL")
            _by_mix["MOI_FC"]   = _moi_fc_por_grupo(_by_mix, "MIX_OFICIAL")
            _crit_mix = _pool[_pool["_ES_CRITICO"]].groupby("MIX_OFICIAL")["STOCK_COSTO"].sum().rename("Critico_CLP")
            _by_mix = _by_mix.merge(_crit_mix, on="MIX_OFICIAL", how="left").fillna({"Critico_CLP": 0})
            _by_mix["Pct_Critico"] = np.where(_by_mix["Stock_CLP"] > 0, _by_mix["Critico_CLP"] / _by_mix["Stock_CLP"] * 100, 0)
            _by_mix["Pct_Total"]   = np.where(_total_stock_clp > 0, _by_mix["Stock_CLP"] / _total_stock_clp * 100, 0)
            _by_mix = _by_mix.sort_values("Stock_CLP", ascending=False)

            _sin_salida = _by_mix[_by_mix["MIX_OFICIAL"].str.upper().str.strip().isin(["FUERA DE MIX", "DESCONTINUAR"])]
            if not _sin_salida.empty:
                _ss_stk  = _sin_salida["Stock_CLP"].sum()
                _ss_crit = _sin_salida["Critico_CLP"].sum()
                if _ss_stk > 0:
                    st.error(
                        f"🚨 **Capital sin salida comercial**: {_fmt_mm(_ss_stk)} en FUERA DE MIX / DESCONTINUAR "
                        f"({_ss_stk / _total_stock_clp * 100:.0f}% del total) — "
                        f"{_fmt_mm(_ss_crit)} con MOI >12m. Sin acción de precio o liquidación, este stock no saldrá."
                    )

            _bm_disp = _by_mix.copy()
            _bm_disp["Stock ($)"]   = _bm_disp["Stock_CLP"].apply(_fmt_mm)
            _bm_disp["% Total"]     = _bm_disp["Pct_Total"].apply(lambda x: f"{x:.0f}%")
            _bm_disp["MOI Hist"]    = _bm_disp["MOI_HIST"].apply(lambda x: f"{x:.1f}m" if x > 0 else "—")
            _bm_disp["MOI FC"]      = _bm_disp["MOI_FC"].apply(lambda x: f"{x:.1f}m" if x > 0 else "—")
            _bm_disp["Crítico ($)"] = _bm_disp["Critico_CLP"].apply(_fmt_mm)
            _bm_disp["% Crítico"]   = _bm_disp["Pct_Critico"].apply(lambda x: f"{x:.0f}%")
            st.dataframe(
                _bm_disp[["MIX_OFICIAL", "SKUs", "Stock ($)", "% Total", "MOI Hist", "MOI FC", "Crítico ($)", "% Crítico"]].rename(
                    columns={"MIX_OFICIAL": "Tipo MIX"}
                ),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("Columna MIX_OFICIAL no disponible.")

        st.divider()

        # ══════════════════════════════════════════════════════════════════════
        # PROYECCIÓN MENSUAL (expander — detalle para quien quiera profundizar)
        # ══════════════════════════════════════════════════════════════════════
        _proy_exp_label = "📈 Evolución mensual proyectada — ¿hacia dónde va el stock?"
        if not _df_proy_bc.empty and "SKU_PRODUCTO" in _df_proy_bc.columns:
            with st.expander(_proy_exp_label, expanded=False):
                _proy_skus = set(_pool["SKU_PRODUCTO"])
                _proy_filtered = _df_proy_bc[_df_proy_bc["SKU_PRODUCTO"].isin(_proy_skus)].copy()
                if "TIPO_DATO" in _proy_filtered.columns:
                    _proy_fc = _proy_filtered[_proy_filtered["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])]
                    _proy_hist_m = _proy_filtered[_proy_filtered["TIPO_DATO"] == "HISTORICO"].copy()
                else:
                    _proy_fc = _proy_filtered
                    _proy_hist_m = pd.DataFrame()

                if not _proy_fc.empty and "PERIODO" in _proy_fc.columns:
                    _proy_fc = _proy_fc.copy()
                    _proy_fc["PERIODO"] = pd.to_datetime(_proy_fc["PERIODO"], errors="coerce")
                    for _nc in ["STOCK_FINAL_TOTAL", "COGS_RES_TOTAL", "VN_RES_TOTAL",
                                "APORTE_RES_TOTAL", "FORECAST_COMPRA", "ETA",
                                "LOST_SALES_TOTAL", "COSTO_UNITARIO"]:
                        if _nc in _proy_fc.columns:
                            _proy_fc[_nc] = pd.to_numeric(_proy_fc[_nc], errors="coerce").fillna(0)
                    _cu = _proy_fc["COSTO_UNITARIO"] if "COSTO_UNITARIO" in _proy_fc.columns else 0
                    _proy_fc["_STOCK_CLP"]   = _proy_fc["STOCK_FINAL_TOTAL"] * _cu if "STOCK_FINAL_TOTAL" in _proy_fc.columns else 0
                    _proy_fc["_COMPRAS_CLP"] = _proy_fc["FORECAST_COMPRA"] * _cu if "FORECAST_COMPRA" in _proy_fc.columns else 0
                    _proy_fc["_ETA_CLP"]     = _proy_fc["ETA"] * _cu if "ETA" in _proy_fc.columns else 0
                    _proy_fc["_LOST_CLP"]    = _proy_fc["LOST_SALES_TOTAL"] * _cu if "LOST_SALES_TOTAL" in _proy_fc.columns else 0
                    _agg_map = {
                        "_STOCK_CLP": "STOCK_CIERRE", "COGS_RES_TOTAL": "VTA_COSTO",
                        "VN_RES_TOTAL": "VN", "APORTE_RES_TOTAL": "APORTE",
                        "_COMPRAS_CLP": "COMPRAS", "_ETA_CLP": "ETA_COMEX", "_LOST_CLP": "LOST_SALES",
                    }
                    _agg_dict = {_tgt: (_src, "sum") for _src, _tgt in _agg_map.items() if _src in _proy_fc.columns}
                    _month_agg = _proy_fc.groupby("PERIODO").agg(**_agg_dict).reset_index().sort_values("PERIODO")
                    if "VTA_COSTO" in _month_agg.columns:
                        _month_agg["MOI"] = np.where(_month_agg["VTA_COSTO"] > 0, _month_agg["STOCK_CIERRE"] / _month_agg["VTA_COSTO"], 0)
                    if "VN" in _month_agg.columns and "APORTE" in _month_agg.columns:
                        _month_agg["MARGEN"] = np.where(_month_agg["VN"] > 0, _month_agg["APORTE"] / _month_agg["VN"] * 100, 0)
                    _disp_month = _month_agg.copy()
                    _disp_month["Mes"] = _disp_month["PERIODO"].dt.strftime("%b %y")
                    _show_cols = ["Mes"]
                    _col_config = {}
                    for _c, _label in [
                        ("STOCK_CIERRE", "Stock Cierre ($)"), ("COMPRAS", "+ Compras Proy."),
                        ("ETA_COMEX", "+ ETA Comex"), ("VTA_COSTO", "- Vta Costo"),
                        ("VN", "Venta Neta"), ("APORTE", "Aporte"),
                        ("MARGEN", "Margen %"), ("MOI", "MOI"), ("LOST_SALES", "Vta Perdida"),
                    ]:
                        if _c in _disp_month.columns:
                            _show_cols.append(_c)
                            if _c == "MARGEN":
                                _col_config[_c] = st.column_config.NumberColumn(_label, format="%.1f%%")
                            elif _c == "MOI":
                                _col_config[_c] = st.column_config.NumberColumn(_label, format="%.1f")
                            else:
                                _disp_month[_c] = _disp_month[_c].apply(_fmt_mm)
                                _col_config[_c] = st.column_config.TextColumn(_label)
                    _col_config["Mes"] = st.column_config.TextColumn("Mes", width="small")
                    st.dataframe(_disp_month[_show_cols], column_config=_col_config, use_container_width=True, hide_index=True)
                else:
                    st.info("Ejecuta la proyección de stock para ver la evolución mensual.")
        else:
            with st.expander(_proy_exp_label, expanded=False):
                st.info("Ejecuta la proyección de stock para ver la evolución mensual.")

        st.divider()

        # ══════════════════════════════════════════════════════════════════════
        # 4. CONCENTRACIÓN — ¿Dónde se concentra el problema?
        # ══════════════════════════════════════════════════════════════════════
        st.markdown("### ¿Dónde se concentra el problema?")
        if "LINEA" in _pool.columns:
            _by_linea = _pool.groupby("LINEA").agg(
                SKUs=("SKU_PRODUCTO", "nunique"),
                Stock_CLP=("STOCK_COSTO", "sum"),
                Stock_Und=("STOCK_UNIDADES", "sum"),
                Rotacion=("ROTACION_UND_MES", "sum"),
            ).reset_index()
            _by_linea["MOI_HIST"] = _moi_hist_por_grupo(_by_linea, "LINEA")
            _by_linea["MOI_FC"]   = _moi_fc_por_grupo(_by_linea, "LINEA")
            _by_linea = _by_linea.sort_values("Stock_CLP", ascending=False)
            _crit_by_linea = _pool[_pool["_ES_CRITICO"]].groupby("LINEA")["STOCK_COSTO"].sum().rename("Critico_CLP")
            _by_linea = _by_linea.merge(_crit_by_linea, on="LINEA", how="left")
            _by_linea["Critico_CLP"] = _by_linea["Critico_CLP"].fillna(0)
            _by_linea["% Crítico"] = np.where(
                _by_linea["Stock_CLP"] > 0, _by_linea["Critico_CLP"] / _by_linea["Stock_CLP"] * 100, 0
            )

            # Pareto chart
            _pareto_df = _by_linea[_by_linea["Critico_CLP"] > 0].sort_values("Critico_CLP", ascending=False).copy()
            if not _pareto_df.empty:
                _total_crit = _pareto_df["Critico_CLP"].sum()
                _pareto_df["% Acum"] = _pareto_df["Critico_CLP"].cumsum() / _total_crit * 100
                _fig_pareto = go.Figure()
                _fig_pareto.add_trace(go.Bar(
                    name="Stock Crítico ($)", x=_pareto_df["LINEA"],
                    y=_pareto_df["Critico_CLP"] / 1_000_000,
                    marker_color="#ef4444", yaxis="y",
                    hovertemplate="%{x}: $%{y:.1f}M<extra></extra>",
                ))
                _fig_pareto.add_trace(go.Scatter(
                    name="% Acumulado", x=_pareto_df["LINEA"], y=_pareto_df["% Acum"],
                    mode="lines+markers", line=dict(color="#065E8B", width=2),
                    marker=dict(size=7), yaxis="y2",
                    hovertemplate="%{y:.0f}% acum<extra></extra>",
                ))
                _fig_pareto.add_hline(y=80, line_dash="dot", line_color="#f59e0b",
                                      annotation_text="80%", annotation_position="right", yref="y2")
                _fig_pareto.update_layout(
                    title=dict(text="Pareto — Concentración de Stock Crítico por Línea", x=0.5, font_size=14),
                    yaxis=dict(title="Stock Crítico (M$)"),
                    yaxis2=dict(title="% Acumulado", overlaying="y", side="right", range=[0, 105]),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02),
                    height=370, plot_bgcolor="#FAFAFA", paper_bgcolor="#FFFFFF",
                )
                st.plotly_chart(_fig_pareto, use_container_width=True)

            _dl = _by_linea.copy()
            _dl["_pct_crit_num"] = np.where(
                _dl["Stock_CLP"] > 0, _dl["Critico_CLP"] / _dl["Stock_CLP"] * 100, 0
            )
            _dl["Stock ($)"]   = _dl["Stock_CLP"].apply(_fmt_mm)
            _dl["Crítico ($)"] = _dl["Critico_CLP"].apply(_fmt_mm)
            _dl["% Crítico"]   = _dl["_pct_crit_num"].apply(lambda x: f"{x:.0f}%")
            _dl["MOI Hist"]    = _dl["MOI_HIST"].apply(lambda x: f"{x:.1f}m" if x > 0 else "—")
            _dl["MOI FC"]      = _dl["MOI_FC"].apply(lambda x: f"{x:.1f}m" if x > 0 else "—")
            _dl["⚠️"]           = _dl["_pct_crit_num"].apply(lambda x: "⚠️" if x >= 20 else "")
            st.dataframe(
                _dl[["LINEA", "SKUs", "Stock ($)", "MOI Hist", "MOI FC", "Crítico ($)", "% Crítico", "⚠️"]],
                use_container_width=True, hide_index=True,
            )

        # ── Sub-tabs: Proveedor / Marca / Sublínea ──────────────────────────
        def _render_group_table(dim_col, label, top_n=15):
            if dim_col not in _pool.columns:
                st.caption(f"Columna {dim_col} no disponible.")
                return
            _bg = _pool.groupby(dim_col).agg(
                SKUs=("SKU_PRODUCTO", "nunique"),
                Stock_CLP=("STOCK_COSTO", "sum"),
                Stock_Und=("STOCK_UNIDADES", "sum"),
                Rotacion=("ROTACION_UND_MES", "sum"),
            ).reset_index()
            _bg["MOI_HIST"] = _moi_hist_por_grupo(_bg, dim_col)
            _bg["MOI_FC"]   = _moi_fc_por_grupo(_bg, dim_col)
            _crit_g = (
                _pool[_pool["_ES_CRITICO"]]
                .groupby(dim_col)["STOCK_COSTO"].sum().rename("Critico_CLP")
            )
            _bg = _bg.merge(_crit_g, on=dim_col, how="left").fillna({"Critico_CLP": 0})
            _bg["Pct_Critico"] = np.where(
                _bg["Stock_CLP"] > 0, _bg["Critico_CLP"] / _bg["Stock_CLP"] * 100, 0
            )
            # % del stock total de la compañía — da contexto al impacto real de cada grupo
            _bg["Pct_Total"] = np.where(
                _total_stock_clp > 0, _bg["Stock_CLP"] / _total_stock_clp * 100, 0
            )
            # Flag portafolio polarizado: MOI agr OK (<12m) pero capital concentrado en crítico (>60%)
            _bg["_polarizado"] = (
                (_bg["MOI_HIST"] > 0) & (_bg["MOI_HIST"] < 12) &
                (_bg["Pct_Critico"] > 60) &
                (_bg["Stock_CLP"] > 0)
            )
            _bg = _bg.sort_values("Critico_CLP", ascending=False).head(top_n)

            # Alertas portafolio polarizado — solo si el grupo tiene peso relevante (>1% del total)
            _pol_rows = _bg[_bg["_polarizado"] & (_bg["Pct_Total"] >= 1)]
            for _, _pr in _pol_rows.iterrows():
                _grp_name = _pr[dim_col]
                st.warning(
                    f"⚡ **Portafolio Polarizado — {_grp_name}**: "
                    f"MOI agregado {_pr['MOI_HIST']:.1f}m (parece aceptable) "
                    f"pero el **{_pr['Pct_Critico']:.0f}%** del capital "
                    f"({_fmt_mm(_pr['Critico_CLP'])} de {_fmt_mm(_pr['Stock_CLP'])}) "
                    f"está en SKUs con cobertura >12m. "
                    f"SKUs de alta rotación están subsidiando la métrica agregada."
                )

            _dg = _bg.copy()
            _dg["Stock ($)"]   = _dg["Stock_CLP"].apply(_fmt_mm)
            _dg["% Total"]     = _dg["Pct_Total"].apply(lambda x: f"{x:.1f}%")
            _dg["Crítico ($)"] = _dg["Critico_CLP"].apply(_fmt_mm)
            _dg["% Crítico"]   = _dg["Pct_Critico"].apply(lambda x: f"{x:.0f}%")
            _dg["MOI Hist"]    = _dg["MOI_HIST"].apply(lambda x: f"{x:.1f}m" if x > 0 else "—")
            _dg["MOI FC"]      = _dg["MOI_FC"].apply(lambda x: f"{x:.1f}m" if x > 0 else "—")
            _dg["⚡"]          = _dg["_polarizado"].apply(lambda x: "⚡" if x else "")
            st.dataframe(
                _dg[[dim_col, "SKUs", "Stock ($)", "% Total", "MOI Hist", "MOI FC", "Crítico ($)", "% Crítico", "⚡"]],
                use_container_width=True, hide_index=True,
            )

        _sub_tabs_dims = []
        for _dim_lbl in [("PROVEEDOR", "Proveedor"), ("MARCA", "Marca"), ("SUBLINEA", "Sublínea")]:
            if _dim_lbl[0] in _pool.columns:
                _sub_tabs_dims.append(_dim_lbl)

        if _sub_tabs_dims:
            _sub_tabs = st.tabs([f"Por {lbl}" for _, lbl in _sub_tabs_dims])
            for (_dim_col, _dim_lbl), _stab in zip(_sub_tabs_dims, _sub_tabs):
                with _stab:
                    _render_group_table(_dim_col, _dim_lbl)

        # ── DEBUG MOI ────────────────────────────────────────────────────────
        with st.expander("🔍 Debug MOI por marca (selecciona una marca)", expanded=False):
            _debug_marcas = sorted(_pool["MARCA"].dropna().unique()) if "MARCA" in _pool.columns else []
            if _debug_marcas:
                _dbg_marca = st.selectbox("Marca", _debug_marcas, key="dbg_marca_sel")
                _dbg_pool = _pool[_pool["MARCA"] == _dbg_marca][
                    ["SKU_PRODUCTO", "STOCK_COSTO", "STOCK_UNIDADES", "ROTACION_UND_MES", "MOI_HIST"]
                ].copy() if "MARCA" in _pool.columns else pd.DataFrame()

                if not _dbg_pool.empty:
                    _has_c90 = "COSTO_PROM_90_CIA" in _pool.columns
                    _dbg_pool = _pool[_pool["MARCA"] == _dbg_marca][
                        ["SKU_PRODUCTO", "STOCK_COSTO", "STOCK_UNIDADES", "ROTACION_UND_MES", "MOI_HIST"]
                        + (["COSTO_PROM_90_CIA"] if _has_c90 else [])
                    ].copy()
                    _stk_total = _dbg_pool["STOCK_COSTO"].sum()
                    _c90_total  = _dbg_pool["COSTO_PROM_90_CIA"].sum() if _has_c90 else 0
                    _moi_grupo  = _stk_total / (_c90_total * 30.44) if _c90_total > 0 else 0
                    st.markdown(f"**{len(_dbg_pool)} SKUs en pool**")
                    st.caption(
                        f"Stock: {_fmt_mm(_stk_total)} | "
                        f"COGS diario grupo: {_fmt_mm(_c90_total)}/día | "
                        f"→ MOI grupo = {_moi_grupo:.1f}m"
                    )
                    _dbg_pool_disp = _dbg_pool.copy()
                    _dbg_pool_disp["STOCK_COSTO"] = _dbg_pool_disp["STOCK_COSTO"].apply(_fmt_mm)
                    _dbg_pool_disp["MOI_HIST"] = _dbg_pool_disp["MOI_HIST"].apply(lambda x: f"{x:.1f}m")
                    if _has_c90:
                        _dbg_pool_disp["COSTO_PROM_90_CIA"] = _dbg_pool_disp["COSTO_PROM_90_CIA"].apply(_fmt_mm)
                    st.dataframe(_dbg_pool_disp, use_container_width=True, hide_index=True)

                _dbg_pool_skus = set(_dbg_pool["SKU_PRODUCTO"])
                if not _ventas_px.empty and "MARCA" in _pool.columns:
                    _dbg_vcm_all = _ventas_px.merge(
                        _pool[_pool["MARCA"] == _dbg_marca][["SKU_PRODUCTO"]].drop_duplicates(),
                        on="SKU_PRODUCTO", how="inner"
                    )
                    _dbg_vcm_pool = _ventas_px[_ventas_px["SKU_PRODUCTO"].isin(_dbg_pool_skus)].copy()

                    if "APORTE" in _ventas_px.columns and "PERIODO" in _ventas_px.columns:
                        _dbg_vcm_all["PERIODO"] = pd.to_datetime(_dbg_vcm_all["PERIODO"], errors="coerce")
                        _dbg_vcm_pool["PERIODO"] = pd.to_datetime(_dbg_vcm_pool["PERIODO"], errors="coerce")
                        _today_m = pd.Timestamp.now().to_period("M").to_timestamp()
                        _n_m = max(_dbg_vcm_all[_dbg_vcm_all["PERIODO"] < _today_m]["PERIODO"].nunique(), 1)

                        _cogs_all  = _dbg_vcm_all[_dbg_vcm_all["PERIODO"] < _today_m]["APORTE"].sum() / _n_m
                        _cogs_pool = _dbg_vcm_pool[_dbg_vcm_pool["PERIODO"] < _today_m]["APORTE"].sum() / _n_m
                        _skus_all  = _dbg_vcm_all["SKU_PRODUCTO"].nunique()
                        _skus_pool = _dbg_vcm_pool["SKU_PRODUCTO"].nunique()
                        _stock = _dbg_pool["STOCK_COSTO"].sum()

                        st.markdown("**Comparación de denominadores (APORTE VCM mensual promedio)**")
                        _dc1, _dc2 = st.columns(2)
                        _dc1.metric(
                            f"COGS con TODOS los SKUs Britax ({_skus_all} SKUs en VCM)",
                            _fmt_mm(_cogs_all) + "/mes",
                            f"→ MOI = {_stock/max(_cogs_all,1):.1f}m"
                        )
                        _dc2.metric(
                            f"COGS solo SKUs en pool ({_skus_pool} SKUs)",
                            _fmt_mm(_cogs_pool) + "/mes",
                            f"→ MOI = {_stock/max(_cogs_pool,1):.1f}m"
                        )
                        st.caption(f"Meses de VCM usados: {_n_m} (últimos 6 meses completos)")

        st.divider()

        # ══════════════════════════════════════════════════════════════════════
        # 5. DIAGNÓSTICO DE CAUSAS — ¿Por qué?
        # ══════════════════════════════════════════════════════════════════════
        st.markdown("### ¿Por qué? — Diagnóstico de Causas")
        st.caption("Clasificación del stock con MOI ≥ 6m según la causa más probable. Las causas no son excluyentes.")

        # Causa 1: Alza de precio → SKUs elásticos con MOI alto
        _elast_seg = _pool.get("ELAST_SEGMENTO", pd.Series("", index=_pool.index))
        _c1 = _pool[_elast_seg.isin(["Muy Elástico", "Elástico"]) & (_pool["MOI_HIST"] >= 6)]
        _stk_c1 = _c1["STOCK_COSTO"].sum() if not _c1.empty else 0

        # Causa 2: Stock atrapado en CD sin llegar a tiendas
        _pct_cd = pd.to_numeric(_pool.get("PCT_STOCK_CD", pd.Series(0, index=_pool.index)), errors="coerce").fillna(0)
        _n_tdas = pd.to_numeric(_pool.get("N_TIENDAS_VENTA_3M", pd.Series(1, index=_pool.index)), errors="coerce").fillna(1)
        _c2 = _pool[(_pct_cd >= 80) & (_n_tdas == 0) & (_pool["MOI_HIST"] >= 6)]
        _stk_c2 = _c2["STOCK_COSTO"].sum() if not _c2.empty else 0

        # Causa 3: Demanda caída — sin rotación, antigüedad ≥6m, no es problema de precio
        _ant = pd.to_numeric(_pool.get("ANTIGUEDAD_MESES", pd.Series(0, index=_pool.index)), errors="coerce").fillna(0)
        _c3 = _pool[
            (_pool["MOI_HIST"] >= 6) & (_pool["ROTACION_UND_MES"] == 0) &
            (_ant >= 6) & (~_elast_seg.isin(["Muy Elástico", "Elástico"]))
        ]
        _stk_c3 = _c3["STOCK_COSTO"].sum() if not _c3.empty else 0

        # Causa 4: Caída de canal — detectar desde df_proy HISTORICO
        _stk_c4, _canal_caido, _canal_pct_caida = 0.0, "—", 0.0
        if not _df_proy_bc.empty and "TIPO_DATO" in _df_proy_bc.columns:
            _hist_proy = _df_proy_bc[_df_proy_bc["TIPO_DATO"] == "HISTORICO"].copy()
            if not _hist_proy.empty and "PERIODO" in _hist_proy.columns:
                _hist_proy["PERIODO"] = pd.to_datetime(_hist_proy["PERIODO"], errors="coerce")
                _periodos_h = sorted(_hist_proy["PERIODO"].dropna().unique())
                if len(_periodos_h) >= 6:
                    _last3p, _prev3p = _periodos_h[-3:], _periodos_h[-6:-3]
                    _canal_drops = {}
                    for _cname, _ccol in [("Tienda", "VN_RES_TIENDA"), ("Etail", "VN_RES_ETAIL"), ("Mayorista", "VN_RES_MAYOR")]:
                        if _ccol in _hist_proy.columns:
                            _hist_proy[_ccol] = pd.to_numeric(_hist_proy[_ccol], errors="coerce").fillna(0)
                            _v_last = _hist_proy[_hist_proy["PERIODO"].isin(_last3p)][_ccol].sum()
                            _v_prev = _hist_proy[_hist_proy["PERIODO"].isin(_prev3p)][_ccol].sum()
                            if _v_prev > 0:
                                _drop = (_v_prev - _v_last) / _v_prev * 100
                                if _drop > 15:
                                    _canal_drops[_cname] = _drop
                    if _canal_drops:
                        _canal_caido = max(_canal_drops, key=_canal_drops.get)
                        _canal_pct_caida = _canal_drops[_canal_caido]
                        _stk_c4 = _pool.loc[_pool["MOI_HIST"] >= 6, "STOCK_COSTO"].sum() * (_canal_pct_caida / 100) * 0.4

        # Causa 5: Sobre-compra estructural — MOI muy alto pero sí rota
        _c5 = _pool[
            _pool["_ES_CRITICO"] & (_pool["ROTACION_UND_MES"] > 0) &
            (~_elast_seg.isin(["Muy Elástico", "Elástico"])) & (_pct_cd < 80)
        ]
        _stk_c5 = _c5["STOCK_COSTO"].sum() if not _c5.empty else 0

        def _causa_card(col, emoji, titulo, monto, detalle, color="#065E8B"):
            col.markdown(
                f"""<div style="border-left:4px solid {color};padding:8px 12px;border-radius:4px;background:#f8fafc;margin-bottom:4px">
                <div style="font-size:1.3em">{emoji}</div>
                <div style="font-size:0.72em;color:#64748b;font-weight:600;text-transform:uppercase;margin-bottom:2px">{titulo}</div>
                <div style="font-size:1.25em;font-weight:700;color:{color}">{monto}</div>
                <div style="font-size:0.68em;color:#94a3b8">{detalle}</div>
                </div>""",
                unsafe_allow_html=True,
            )

        _cc = st.columns(5)
        _causa_card(_cc[0], "💸", "Alza de Precio", _fmt_mm(_stk_c1),
                    f"{len(_c1)} SKUs elásticos con MOI≥6m", color="#f97316")
        _causa_card(_cc[1], "🏭", "Atrapado en CD", _fmt_mm(_stk_c2),
                    f"{len(_c2)} SKUs ≥80% en CD sin vender en tiendas", color="#0ea5e9")
        _causa_card(_cc[2], "📉", "Demanda Caída", _fmt_mm(_stk_c3),
                    f"{len(_c3)} SKUs ≥6m sin rotación", color="#8b5cf6")
        if _canal_caido != "—":
            _causa_card(_cc[3], "📊", f"Caída Canal {_canal_caido}", _fmt_mm(_stk_c4),
                        f"{_canal_pct_caida:.0f}% menos vs 3m previos", color="#ef4444")
        else:
            _causa_card(_cc[3], "📊", "Caída de Canal", "—",
                        "Sin caída >15% detectada en últimos 3m", color="#9ca3af")
        _causa_card(_cc[4], "📦", "Sobre-compra", _fmt_mm(_stk_c5),
                    f"{len(_c5)} SKUs MOI≥12m que sí rotan", color="#FB8C00")

    # ══════════════════════════════════════════════════════════════════════
    # TAB 2: SEGMENTACIÓN POR ACCIÓN
    # ══════════════════════════════════════════════════════════════════════
    with _tab_segments:
        st.markdown("### Distribución por Segmento de Acción")

        # Build segments from MIX_OFICIAL + MOI
        if "MIX_OFICIAL" in _pool.columns and "ACCION_VENTA_RAW" in _pool.columns:
            _seg_cols = ["MIX_OFICIAL", "ACCION_VENTA_RAW"]
            _pool["_SEGMENTO"] = _pool["MIX_OFICIAL"].astype(str).str.strip() + " — " + _pool["ACCION_VENTA_RAW"].astype(str).str.strip()
        elif "ACCION_VENTA_RAW" in _pool.columns:
            _pool["_SEGMENTO"] = _pool["ACCION_VENTA_RAW"].astype(str).str.strip()
        else:
            _pool["_SEGMENTO"] = "SIN CLASIFICAR"

        _seg_agg = _pool.groupby("_SEGMENTO").agg(
            SKUs=("SKU_PRODUCTO", "nunique"),
            Stock_CLP=("STOCK_COSTO", "sum"),
            Stock_Und=("STOCK_UNIDADES", "sum"),
            Rotacion=("ROTACION_UND_MES", "sum"),
        ).reset_index()
        _seg_agg["MOI"] = np.where(
            _seg_agg["Rotacion"] > 0,
            _seg_agg["Stock_Und"] / _seg_agg["Rotacion"],
            0
        )
        _seg_agg = _seg_agg.sort_values("Stock_CLP", ascending=False)

        # Display as cards
        _n_seg = len(_seg_agg)
        _cols_per_row = 4
        for _i in range(0, _n_seg, _cols_per_row):
            _row_segs = _seg_agg.iloc[_i:_i + _cols_per_row]
            _seg_cols_ui = st.columns(min(_cols_per_row, len(_row_segs)))
            for _j, (_, _seg_row) in enumerate(_row_segs.iterrows()):
                with _seg_cols_ui[_j]:
                    _seg_name = _seg_row["_SEGMENTO"]
                    _seg_stk = _seg_row["Stock_CLP"]
                    _seg_skus = _seg_row["SKUs"]
                    _seg_moi = _seg_row["MOI"]
                    st.markdown(
                        f"**{_seg_name}**\n\n"
                        f"{_seg_skus} SKUs | {_fmt_mm(_seg_stk)} | MOI {_seg_moi:.1f}m"
                    )

        # Detail table
        st.markdown("### Detalle por Segmento")
        _seg_filter_c1, _seg_filter_c2 = st.columns(2)
        with _seg_filter_c1:
            _seg_detail_sel = st.selectbox(
                "Segmento de acción",
                ["Todos"] + _seg_agg["_SEGMENTO"].tolist(),
                key="bc_seg_detail_sel",
            )
        with _seg_filter_c2:
            _edad_opts = ["Todos"] + [b for b in _AGE_BAND_ORDER if b in _pool.get("TRAMO_EDAD", pd.Series(dtype=str)).unique()]
            _edad_sel = st.selectbox(
                "Tramo de antigüedad",
                _edad_opts,
                key="bc_edad_detail_sel",
            )
        _seg_pool = _pool.copy()
        if _seg_detail_sel != "Todos":
            _seg_pool = _seg_pool[_seg_pool["_SEGMENTO"] == _seg_detail_sel]
        if _edad_sel != "Todos" and "TRAMO_EDAD" in _seg_pool.columns:
            _seg_pool = _seg_pool[_seg_pool["TRAMO_EDAD"] == _edad_sel]
        # Coerción numérica para garantizar sort y formato correcto
        for _nc in ["STOCK_COSTO", "STOCK_UNIDADES", "MOI_HIST", "MOI_FC",
                    "ROTACION_UND_MES", "ANTIGUEDAD_MESES", "ANTIGUEDAD_STOCK_MESES", "DCTO_SUGERIDO"]:
            if _nc in _seg_pool.columns:
                _seg_pool[_nc] = pd.to_numeric(_seg_pool[_nc], errors="coerce").fillna(0)
        _seg_show_cols = ["SKU_PRODUCTO"]
        for _sc in ["SKU_NOM_PRODUCTO", "LINEA", "MARCA", "MIX_OFICIAL",
                     "ANTIGUEDAD_MESES", "TRAMO_EDAD",
                     "ANTIGUEDAD_STOCK_MESES", "TRAMO_EDAD_STOCK",
                     "SALUD", "STOCK_UNIDADES", "STOCK_COSTO",
                     "MOI_HIST", "MOI_FC", "ROTACION_UND_MES",
                     "ACCION_VENTA_RAW", "DCTO_SUGERIDO"]:
            if _sc in _seg_pool.columns:
                _seg_show_cols.append(_sc)
        st.dataframe(
            _seg_pool[_seg_show_cols].sort_values("STOCK_COSTO", ascending=False),
            use_container_width=True, hide_index=True,
            column_config={
                "STOCK_COSTO": st.column_config.NumberColumn("Stock ($)", format="$%,.0f"),
                "STOCK_UNIDADES": st.column_config.NumberColumn("Stock (und)", format="%,.0f"),
                "MOI_HIST": st.column_config.NumberColumn("MOI Hist", format="%.1f"),
                "MOI_FC": st.column_config.NumberColumn("MOI FC", format="%.1f"),
                "ROTACION_UND_MES": st.column_config.NumberColumn("Rot/mes", format="%.1f"),
                "ANTIGUEDAD_MESES": st.column_config.NumberColumn("Edad Producto (m)", format="%.0f"),
                "ANTIGUEDAD_STOCK_MESES": st.column_config.NumberColumn("Edad Stock FIFO (m)", format="%.0f"),
                "TRAMO_EDAD": st.column_config.TextColumn("Tramo Producto"),
                "TRAMO_EDAD_STOCK": st.column_config.TextColumn("Tramo Stock"),
                "SALUD": st.column_config.TextColumn("Salud"),
                "DCTO_SUGERIDO": st.column_config.NumberColumn("Dcto %", format="%.0f%%"),
            },
        )

    # ══════════════════════════════════════════════════════════════════════
    # TAB 3: SIMULADOR DE ESCENARIOS
    # ══════════════════════════════════════════════════════════════════════
    with _tab_simulator:
        _render_scenario_simulator(df_enriched=_pool, data=data)

    # ══════════════════════════════════════════════════════════════════════
    # TAB 4: DEEP DIVE SKU
    # ══════════════════════════════════════════════════════════════════════
    with _tab_sku:
        st.markdown("### 🔍 Análisis Detallado por SKU")

        # ── SKU Selector ──
        _bc_opts = []
        for _, _r in _pool.sort_values("STOCK_COSTO", ascending=False).iterrows():
            _sku_v = _r["SKU_PRODUCTO"]
            _nom_raw = str(_r.get("SKU_NOM_PRODUCTO", "")).strip()
            # SKU_NOM_PRODUCTO often starts with the SKU code itself, avoid duplicating
            if _nom_raw.upper().startswith(_sku_v.upper()):
                _nom_v = _nom_raw[len(_sku_v):].lstrip(" -–—")
            else:
                _nom_v = _nom_raw
            _nom_v = _nom_v or _sku_v
            _moi_v = _r.get("MOI_HIST", 0) or 0
            _stk_v = pd.to_numeric(_r.get("STOCK_UNIDADES", 0), errors="coerce") or 0
            _mix_v = str(_r.get("MIX_OFICIAL", "")).strip()
            _bc_opts.append(
                f"{_sku_v} — {_nom_v} [{_mix_v}] "
                f"(Stock: {_stk_v:,.0f} | MOI: {_moi_v:.1f}m)"
            )

        if not _bc_opts:
            st.info("Sin SKUs para analizar con los filtros seleccionados.")
            return

        st.markdown("---")
        # Force selectbox to show full text without truncation
        st.markdown(
            """<style>
            div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
                white-space: normal !important;
                overflow: visible !important;
                text-overflow: unset !important;
                min-height: 2.5rem;
                height: auto !important;
            }
            div[data-testid="stSelectbox"] li {
                white-space: normal !important;
            }
            </style>""",
            unsafe_allow_html=True,
        )
        _bc_sel = st.selectbox(
            "Seleccionar SKU", _bc_opts, index=0, key="bc_sku_select",
        )
        _bc_sku = _bc_sel.split(" — ")[0].strip() if _bc_sel else None
        if _bc_sku is None:
            st.warning("Selecciona un SKU.")
            return

        _bc_row = _pool[_pool["SKU_PRODUCTO"] == _bc_sku]
        _bc_row = _bc_row.iloc[0] if not _bc_row.empty else None
        if _bc_row is None:
            st.warning("SKU no encontrado.")
            return

        # ── Toggle units/cost + channel filter ──
        _c1, _c2 = st.columns([1, 2])
        with _c1:
            _bc_mode = st.radio(
                "Mostrar inventario en",
                ["Unidades", "Costo ($)"],
                index=0, horizontal=True, key="bc_unit_mode",
            )
        _show_cost = _bc_mode == "Costo ($)"

        _canal_map = {"MINOR": "Retail", "ETAIL": "Etail", "MAYOR": "Mayorista"}
        _canal_opts = list(_canal_map.values())
        _canal_inv = {v: k for k, v in _canal_map.items()}
        with _c2:
            _bc_canales = st.multiselect(
                "Canales de venta", options=_canal_opts,
                default=["Retail", "Etail"],
                key="bc_canal_filter",
                help="Filtra los canales para el gráfico de precio y volumen.",
            )
        _canal_raw = [_canal_inv[c] for c in _bc_canales if c in _canal_inv]

        # ── Build chart ──
        result = _build_bc_chart(
            _bc_sku, _ventas_px, conn,
            show_cost=_show_cost, canal_filter=_canal_raw,
        )
        if result is None:
            st.info("Sin datos históricos ni proyección para este SKU.")
            return

        fig, _bc_hist, _bc_proy, _bc_stock_hist = result

        # MOI FC line
        _bc_moi_f_val = float(_bc_row.get("MOI_FC", 0) or 0)
        if _bc_moi_f_val > 0:
            fig.add_hline(
                y=_bc_moi_f_val, line_dash="dot",
                line_color=COLORS.get("primary", "#1B3A5C"),
                annotation_text=f"MOI FC ({_bc_moi_f_val:.1f}m)",
                annotation_position="top left",
                row=2, col=1, secondary_y=True,
            )

        # MOI Ajustado line (when significantly different from MOI Histórico)
        _bc_moi_adj_val = float(_bc_row.get("MOI_HIST_AJUST", 0) or 0)
        _bc_moi_h_val = float(_bc_row.get("MOI_HIST", 0) or 0)
        if (_bc_moi_adj_val > 0
                and _bc_moi_h_val > _bc_moi_adj_val * 1.5
                and _bc_moi_adj_val < 100):
            fig.add_hline(
                y=_bc_moi_adj_val, line_dash="dashdot",
                line_color="#10b981",
                annotation_text=f"MOI Ajust. ({_bc_moi_adj_val:.0f}m)",
                annotation_position="bottom left",
                row=2, col=1, secondary_y=True,
            )

        st.plotly_chart(fig, use_container_width=True)

        # ── KPI Runway Cards ──
        _bc_moi_h = float(_bc_row.get("MOI_HIST", 0) or 0)
        _bc_moi_f = float(_bc_row.get("MOI_FC", 0) or 0)
        _bc_meses = float(_bc_row.get("MESES_LIQ", 999) or 999)
        _bc_dcto = float(_bc_row.get("DCTO_SUGERIDO", 0) or 0)
        _bc_fc = float(_bc_row.get("FC_COMPRA_CLP", 0) or 0)
        _bc_rot = float(_bc_row.get("ROTACION_UND_MES", 0) or 0)
        _bc_st_u = float(_bc_row.get("STOCK_UNIDADES", 0) or 0)
        _bc_moi_ll = float(_bc_row.get("MOI_FC_LLEGADA", 0) or 0)

        # ── MOI Ajustado data ──
        _bc_moi_adj = float(_bc_row.get("MOI_HIST_AJUST", _bc_moi_h) or _bc_moi_h)
        _bc_conf_moi = str(_bc_row.get("CONFIABILIDAD_MOI", "🟢 Confiable") or "🟢 Confiable")
        _bc_tasa_disp = float(_bc_row.get("TASA_DISPONIBILIDAD", 1.0) or 1.0)
        _bc_meses_cv = int(_bc_row.get("MESES_CON_VENTA", 0) or 0)
        _bc_meses_lb = int(_bc_row.get("MESES_LOOKBACK", 0) or 0)
        _bc_rot_adj = float(_bc_row.get("ROT_AJUST_UND_MES", _bc_rot) or _bc_rot)

        _bc_moi_3m = float(_bc_row.get("MOI_HIST_3M", 0) or 0)
        _bc_rot_3m = float(_bc_row.get("ROT_3M", 0) or 0)

        st.markdown("##### Indicadores de Runway")
        _k1, _k2, _k3, _k4, _k5, _k6, _k7 = st.columns(7)

        # MOI Ajustado (demanda censurada) — el indicador principal
        _adj_label = f"rot {_bc_rot_adj:.1f} und/mes" if _bc_rot_adj > 0 else "sin venta"
        _k1.metric(
            f"MOI Ajustado {_bc_conf_moi.split(' ')[0]}",
            f"{_bc_moi_adj:.0f}m" if _bc_moi_adj < 999 else ">999m",
            delta=_adj_label,
            delta_color="off",
        )
        if _bc_tasa_disp < 1.0 and _bc_meses_lb > 0:
            _k1.caption(
                f"Con stock {_bc_meses_cv}/{_bc_meses_lb} meses "
                f"({_bc_tasa_disp:.0%})"
            )

        # MOI Bruto 6m
        _k2.metric(
            "MOI Bruto (6m)",
            f"{_bc_moi_h:.0f}m" if _bc_moi_h < 999 else ">999m",
            delta=f"rot {_bc_rot:.1f} und/mes" if _bc_rot > 0 else "sin venta",
            delta_color="off",
        )
        # MOI Bruto 3m
        _k3.metric(
            "MOI Bruto (3m)",
            f"{_bc_moi_3m:.0f}m" if _bc_moi_3m > 0 and _bc_moi_3m < 999 else ("—" if _bc_moi_3m == 0 else ">999m"),
            delta=f"rot {_bc_rot_3m:.1f} und/mes" if _bc_rot_3m > 0 else "sin venta",
            delta_color="off",
        )
        _k4.metric(
            "MOI Forecast",
            f"{_bc_moi_f:.0f}m" if _bc_moi_f > 0 else "—",
            delta=(
                f"{'↓' if _bc_moi_f < _bc_moi_h else '↑'} vs bruto {_bc_moi_h:.0f}m"
            ) if _bc_moi_f > 0 and _bc_moi_h > 0 else "sin FC",
            delta_color=("normal" if _bc_moi_f < _bc_moi_h else "inverse") if _bc_moi_f > 0 else "off",
        )
        _k5.metric(
            "Meses p/ Liquidar",
            f"{_bc_meses:.0f}m" if _bc_meses < 999 else ">36m",
            delta=f"con dcto -{_bc_dcto:.0f}%" if _bc_dcto > 0 else "sin dcto",
            delta_color="off",
        )
        _k6.metric(
            "Ahorro si Pausa FC",
            _fmt_mm(_bc_fc),
            delta="detenible", delta_color="off",
        )
        _k7.metric(
            "MOI c/ Tránsito",
            f"{_bc_moi_ll:.1f}m" if _bc_moi_ll > 0 else "—",
            delta=(
                f"+{_bc_moi_ll - _bc_moi_h:.1f}m vs actual"
                if _bc_moi_ll > 0 and _bc_moi_h > 0 else "sin tránsito"
            ),
            delta_color="inverse" if _bc_moi_ll > _bc_moi_h else "off",
        )

        # ── Diagnostics ──
        _stock_bodega = data.get("stock_bodega", pd.DataFrame())
        _insights = _build_bc_diagnostics(
            _bc_row, _ventas_px, _pool, bc_hist=_bc_hist,
            stock_bodega=_stock_bodega,
        )
        if _insights:
            st.markdown("---")
            st.markdown("**Diagnóstico:**")
            for _ins in _insights:
                st.markdown(f"- {_ins}")

        # ── PPT Generation ──
        st.markdown("---")
        st.markdown("#### Generar Presentación Caso de Negocio")

        # Determine current line for PPT
        _sel_area = str(_bc_row.get("AREA", "")).strip()
        _sel_linea = str(_bc_row.get("LINEA", "")).strip()
        _line_label = f"{_sel_area} > {_sel_linea}" if _sel_area and _sel_linea else "SKU seleccionado"

        st.caption(
            f"Genera un PowerPoint con gráficas, KPIs y diagnóstico por cada SKU "
            f"de **{_line_label}**, organizado por criticidad y tipo de mix."
        )

        # Filter pool to same line for PPT
        _ppt_pool = _pool.copy()
        if _sel_area and "AREA" in _ppt_pool.columns:
            _ppt_pool = _ppt_pool[_ppt_pool["AREA"].astype(str).str.strip().str.upper() == _sel_area.upper()]
        if _sel_linea and "LINEA" in _ppt_pool.columns:
            _ppt_pool = _ppt_pool[_ppt_pool["LINEA"].astype(str).str.strip().str.upper() == _sel_linea.upper()]

        if st.button(f"📊 Generar PPT {_line_label}", key="bc_ppt_generate"):
            from utils.export import generate_otb_ppt
            from modules.plan_compras import _build_sku_bc_chart, _build_sku_bc_diagnostics

            with st.spinner(f"Generando PPT para {_line_label}..."):
                _ppt_entries = []
                _n_skus = len(_ppt_pool)
                _progress = st.progress(0, text="Preparando datos...")

                for _idx, (_, _row) in enumerate(
                    _ppt_pool.sort_values(
                        ["_ACCION_ORDER", "STOCK_COSTO"],
                        ascending=[True, False],
                    ).iterrows()
                ):
                    _ppt_sku = _row["SKU_PRODUCTO"]
                    _ppt_nom = str(_row.get("SKU_NOM_PRODUCTO", ""))[:60]
                    _progress.progress(
                        (_idx + 1) / _n_skus,
                        text=f"SKU {_idx+1}/{_n_skus}: {_ppt_sku}",
                    )

                    _ppt_fig = _build_sku_bc_chart(
                        _ppt_sku, _ventas_px, conn, show_cost=False,
                    )
                    _ppt_diag = _build_sku_bc_diagnostics(
                        _row, _ventas_px, _ppt_pool,
                    )

                    _mix_raw = str(_row.get("TIPO", _row.get("MIX_OFICIAL", ""))).strip().upper()
                    if _mix_raw in ("DESCONTINUAR", "FUERA DE MIX", "FUERA MIX"):
                        _mix_group = "Fuera de Mix"
                    elif _mix_raw in ("IN & OUT", "IN &AMP; OUT", "I&O", "IN&OUT"):
                        _mix_group = "IN & OUT"
                    elif _mix_raw == "MIX":
                        _mix_group = "MIX"
                    else:
                        _mix_group = "Sin Definir"

                    _ppt_entries.append({
                        "line": _line_label,
                        "accion": _row.get("ACCION_RAW", "OK"),
                        "mix_type": _mix_group,
                        "sku": _ppt_sku,
                        "nombre": _ppt_nom,
                        "fig": _ppt_fig,
                        "kpis": {
                            "moi_hist": float(_row.get("MOI_HIST", 0) or 0),
                            "rot": float(_row.get("ROTACION_UND_MES", 0) or 0),
                            "moi_fc": float(_row.get("MOI_FC", 0) or 0),
                            "meses_liq": float(_row.get("MESES_LIQ", 999) or 999),
                            "dcto": float(_row.get("DCTO_SUGERIDO", 0) or 0),
                            "ahorro_fc": float(_row.get("FC_COMPRA_CLP", 0) or 0),
                            "moi_transito": float(_row.get("MOI_FC_LLEGADA", 0) or 0),
                            "stock_costo": float(_row.get("STOCK_COSTO", 0) or 0),
                        },
                        "diagnostics": _ppt_diag,
                    })

                _progress.empty()

                _line_stock = _ppt_pool["STOCK_COSTO"].sum()
                _line_st_mm = f"${_line_stock/1_000_000:.1f}M" if _line_stock >= 1_000_000 else f"${_line_stock:,.0f}"
                _line_stats = {
                    _line_label: {
                        "n_skus": len(_ppt_pool),
                        "stock_mm": _line_st_mm,
                        "moi": 0,
                    },
                }

                _pm_name = st.session_state.get("sidebar_pm_filter", "Dorel Chile")
                _ppt_buf = generate_otb_ppt(_pm_name, _ppt_entries, _line_stats)

                st.session_state["_bc_ppt_buffer"] = _ppt_buf
                st.session_state["_bc_ppt_filename"] = (
                    f"BC_{_sel_area}_{_sel_linea}_{datetime.now().strftime('%Y%m%d')}.pptx"
                )
                st.success(f"PPT generado: {len(_ppt_entries)} SKUs.")

        if st.session_state.get("_bc_ppt_buffer") is not None:
            st.download_button(
                "⬇️ Descargar PPT",
                st.session_state["_bc_ppt_buffer"],
                st.session_state.get("_bc_ppt_filename", "BC_reporte.pptx"),
                "application/vnd.openxmlformats-officedocument"
                ".presentationml.presentation",
                key="bc_ppt_download",
            )

        # Scenario simulator moved to tab 3 (Simulador de Escenarios)

    # ══════════════════════════════════════════════════════════════════════
    # TAB 5: LISTA DE ACTIVACIÓN
    # ══════════════════════════════════════════════════════════════════════
    with _tab_activacion:
        st.markdown("### 📋 Lista de Activación de Precios — Stock Crítico")
        st.caption(
            "SKUs con MOI alto y antigüedad ≥12m que necesitan acción de precio. "
            "Descuento calculado contra el precio promedio neto de venta real (VCM). "
            "Segmentado por MIX: FUERA DE MIX/DESCONTINUAR → agresivo · MIX → moderado."
        )

        # ── Build activation list from enriched pool ──
        _act = _pool.copy()
        _act_cols_needed = ["SKU_PRODUCTO", "STOCK_COSTO", "STOCK_UNIDADES", "MOI_HIST",
                            "ANTIGUEDAD_MESES", "MIX_OFICIAL", "ACCION_VENTA_RAW",
                            "DCTO_SUGERIDO", "PRECIO_PROM_NETO"]
        for _c in _act_cols_needed:
            if _c not in _act.columns:
                _act[_c] = 0

        for _c in ["STOCK_COSTO", "STOCK_UNIDADES", "MOI_HIST", "ANTIGUEDAD_MESES",
                    "DCTO_SUGERIDO", "PRECIO_PROM_NETO"]:
            _act[_c] = pd.to_numeric(_act[_c], errors="coerce").fillna(0)

        # Add dimension columns
        for _dc in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                     "STOCK_CD_UND", "TIENDAS_CON_PERFIL", "ROTACION_UND_MES",
                     "TRAMO_EDAD", "MOI_FC", "MESES_LIQ", "MARGEN_6M"]:
            if _dc not in _act.columns:
                _act[_dc] = 0

        for _nc in ["STOCK_CD_UND", "TIENDAS_CON_PERFIL", "ROTACION_UND_MES",
                     "MOI_FC", "MESES_LIQ", "MARGEN_6M"]:
            _act[_nc] = pd.to_numeric(_act[_nc], errors="coerce").fillna(0)

        # Filter: critical stock = MOI_CLASIF >= 12 AND edad producto >= 12m
        _moi_act_col = "MOI_CLASIF" if "MOI_CLASIF" in _act.columns else "MOI_HIST"
        _moi_act = pd.to_numeric(_act[_moi_act_col], errors="coerce").fillna(0)
        _age_act = pd.to_numeric(_act.get("MESES_EN_CIA", _act.get("ANTIGUEDAD_MESES", 0)), errors="coerce").fillna(0)
        _mask_crit = (
            (_moi_act >= 12) | (_moi_act == 0)
        ) & (
            _age_act >= 12
        ) & (
            _act["STOCK_UNIDADES"] > 0
        )
        _act_crit = _act[_mask_crit].copy()

        if _act_crit.empty:
            st.info("No hay SKUs críticos (MOI ≥12 y edad ≥12m) en el pool seleccionado.")
        else:
            # Classify aggressiveness by MIX
            _mix_str = _act_crit["MIX_OFICIAL"].fillna("").astype(str).str.upper().str.strip()
            _act_crit["SEGMENTO"] = np.where(
                _mix_str.isin(["FUERA DE MIX", "DESCONTINUAR", "FUERA_DE_MIX"]),
                "🔴 FUERA MIX / DESCONTINUAR",
                np.where(
                    _mix_str.isin(["IN & OUT", "IN&OUT", "IN AND OUT"]),
                    "🟡 IN & OUT",
                    "🟢 MIX ACTIVO"
                )
            )

            # ── Canal filter for price/margin ──
            _canal_map = {"MINOR": "Retail (Tiendas)", "ETAIL": "E-commerce", "MAYOR": "Mayorista"}
            _canal_opts = list(_canal_map.values())
            if not _ventas_px.empty and "COD_CANAL" in _ventas_px.columns:
                _canales_con_data = sorted(
                    _ventas_px["COD_CANAL"].dropna().unique().tolist()
                )
                _canal_opts = [_canal_map.get(c, c) for c in _canales_con_data if c in _canal_map]
            _canal_inv = {v: k for k, v in _canal_map.items()}

            # Compute price/margin from ventas_px (canal-specific, últimos 3m)
            # Default: sin filtro canal = todos los canales
            _vp_3m = pd.DataFrame()
            if not _ventas_px.empty and "SKU_PRODUCTO" in _ventas_px.columns:
                _vp = _ventas_px.copy()
                _vp["PERIODO"] = pd.to_datetime(_vp.get("PERIODO"), errors="coerce")
                _today_m = pd.Timestamp.now().to_period("M").to_timestamp()
                _last_3m = sorted(_vp[_vp["PERIODO"] < _today_m]["PERIODO"].dropna().unique())[-3:]
                _vp_3m = _vp[_vp["PERIODO"].isin(_last_3m)].copy()
                for _vc in ["NETO", "APORTE", "CANTIDAD"]:
                    if _vc in _vp_3m.columns:
                        _vp_3m[_vc] = pd.to_numeric(_vp_3m[_vc], errors="coerce").fillna(0)

            # Compute price with discount (uses pool PRECIO_PROM_NETO as base)
            _act_crit["PRECIO_ACTUAL"] = _act_crit["PRECIO_PROM_NETO"]
            _act_crit["PRECIO_CON_DCTO"] = np.where(
                (_act_crit["PRECIO_ACTUAL"] > 0) & (_act_crit["DCTO_SUGERIDO"] > 0),
                _act_crit["PRECIO_ACTUAL"] * (1 - _act_crit["DCTO_SUGERIDO"] / 100),
                _act_crit["PRECIO_ACTUAL"]
            )

            # Margen: from ventas_px últimos 3m, filtrable por canal
            _act_crit["MARGEN_ACTUAL"] = 0.0
            _act_crit["MARGEN_CON_DCTO"] = 0.0
            if not _vp_3m.empty:
                _vp_margin = _vp_3m.groupby("SKU_PRODUCTO").agg(
                    _NETO=("NETO", "sum"), _APORTE=("APORTE", "sum"),
                    _PX=("PRECIO_PROMEDIO", "mean") if "PRECIO_PROMEDIO" in _vp_3m.columns else ("NETO", "count"),
                ).reset_index()
                _vp_margin["_MG"] = np.where(
                    _vp_margin["_NETO"] > 0, _vp_margin["_APORTE"] / _vp_margin["_NETO"], 0.0
                )
                _mg_map = _vp_margin.set_index("SKU_PRODUCTO")["_MG"]
                _act_crit["MARGEN_ACTUAL"] = _act_crit["SKU_PRODUCTO"].map(_mg_map).fillna(0)

                # Also update PRECIO_ACTUAL from canal-specific VCM if available
                if "_PX" in _vp_margin.columns and "PRECIO_PROMEDIO" in _vp_3m.columns:
                    _px_map = _vp_margin.set_index("SKU_PRODUCTO")["_PX"]
                    _px_canal = _act_crit["SKU_PRODUCTO"].map(_px_map).fillna(0)
                    _act_crit["PRECIO_ACTUAL"] = np.where(
                        _px_canal > 0, _px_canal, _act_crit["PRECIO_ACTUAL"]
                    )
                    _act_crit["PRECIO_CON_DCTO"] = np.where(
                        (_act_crit["PRECIO_ACTUAL"] > 0) & (_act_crit["DCTO_SUGERIDO"] > 0),
                        _act_crit["PRECIO_ACTUAL"] * (1 - _act_crit["DCTO_SUGERIDO"] / 100),
                        _act_crit["PRECIO_ACTUAL"]
                    )

            # Margen con descuento: M_nuevo = M - Dcto × (1 - M)
            _dcto_frac = _act_crit["DCTO_SUGERIDO"] / 100
            _mg_con_dcto = np.where(
                _act_crit["MARGEN_ACTUAL"] > 0,
                _act_crit["MARGEN_ACTUAL"] - _dcto_frac * (1 - _act_crit["MARGEN_ACTUAL"]),
                0.0
            )
            # Convertir a % para display (0.40 → 40.0)
            _act_crit["MARGEN_ACTUAL"] = _act_crit["MARGEN_ACTUAL"] * 100
            _act_crit["MARGEN_CON_DCTO"] = _mg_con_dcto * 100

            # Sort: FUERA MIX first, then by stock value desc
            _seg_order = {"🔴 FUERA MIX / DESCONTINUAR": 0, "🟡 IN & OUT": 1, "🟢 MIX ACTIVO": 2}
            _act_crit["_seg_sort"] = _act_crit["SEGMENTO"].map(_seg_order).fillna(9)
            _act_crit = _act_crit.sort_values(["_seg_sort", "STOCK_COSTO"], ascending=[True, False])

            # ── KPIs ──
            _k1, _k2, _k3, _k4 = st.columns(4)
            _n_total = len(_act_crit)
            _stk_total = _act_crit["STOCK_COSTO"].sum()
            _n_fuera = (_act_crit["_seg_sort"] == 0).sum()
            _stk_fuera = _act_crit.loc[_act_crit["_seg_sort"] == 0, "STOCK_COSTO"].sum()
            _k1.metric("SKUs a Activar", f"{_n_total:,}")
            _k2.metric("Capital Crítico", f"${_stk_total / 1e6:,.0f}M")
            _k3.metric("FUERA MIX / DESCONT.", f"{_n_fuera:,} SKUs")
            _k4.metric("Capital FUERA MIX", f"${_stk_fuera / 1e6:,.0f}M")

            # ── Filters ──
            _fc1, _fc2, _fc3 = st.columns(3)
            with _fc1:
                _seg_opts = sorted(_act_crit["SEGMENTO"].unique().tolist())
                _sel_seg = st.multiselect("Filtrar por segmento", _seg_opts, default=_seg_opts,
                                           key="act_seg_filter")
            with _fc3:
                _sel_canal_act = st.multiselect(
                    "Canal (para precio y margen)",
                    _canal_opts, default=[], key="act_canal_filter",
                    help="Filtra las ventas VCM por canal para calcular precio y margen sin distorsión de mayorista. Vacío = todos.",
                    placeholder="Todos",
                )
                # Re-compute margin if canal selected
                if _sel_canal_act and not _vp_3m.empty and "COD_CANAL" in _vp_3m.columns:
                    _cod_canales_sel = [_canal_inv.get(c, c) for c in _sel_canal_act]
                    _vp_canal = _vp_3m[_vp_3m["COD_CANAL"].isin(_cod_canales_sel)]
                    if not _vp_canal.empty:
                        _vp_mg_c = _vp_canal.groupby("SKU_PRODUCTO").agg(
                            _NETO=("NETO", "sum"), _APORTE=("APORTE", "sum"),
                        ).reset_index()
                        _vp_mg_c["_MG"] = np.where(
                            _vp_mg_c["_NETO"] > 0, _vp_mg_c["_APORTE"] / _vp_mg_c["_NETO"], 0.0
                        )
                        _mg_c_map = _vp_mg_c.set_index("SKU_PRODUCTO")["_MG"]
                        _act_crit["MARGEN_ACTUAL"] = _act_crit["SKU_PRODUCTO"].map(_mg_c_map).fillna(0) * 100
                        # Update precio from canal
                        if "PRECIO_PROMEDIO" in _vp_canal.columns:
                            _px_c = _vp_canal.groupby("SKU_PRODUCTO")["PRECIO_PROMEDIO"].mean()
                            _px_c_vals = _act_crit["SKU_PRODUCTO"].map(_px_c).fillna(0)
                            _act_crit["PRECIO_ACTUAL"] = np.where(
                                _px_c_vals > 0, _px_c_vals, _act_crit["PRECIO_ACTUAL"]
                            )
                            _act_crit["PRECIO_CON_DCTO"] = np.where(
                                (_act_crit["PRECIO_ACTUAL"] > 0) & (_act_crit["DCTO_SUGERIDO"] > 0),
                                _act_crit["PRECIO_ACTUAL"] * (1 - _act_crit["DCTO_SUGERIDO"] / 100),
                                _act_crit["PRECIO_ACTUAL"]
                            )
                        # Recalc margen con dcto
                        _mg_raw = _act_crit["MARGEN_ACTUAL"] / 100
                        _dcto_f2 = _act_crit["DCTO_SUGERIDO"] / 100
                        _act_crit["MARGEN_CON_DCTO"] = np.where(
                            _mg_raw > 0, (_mg_raw - _dcto_f2 * (1 - _mg_raw)) * 100, 0.0
                        )

            with _fc2:
                if "TAMANO_ALMACENAJE" in _act_crit.columns:
                    _tam_vals = sorted(
                        _act_crit["TAMANO_ALMACENAJE"].fillna("Sin Info")
                        .astype(str).str.strip().unique().tolist()
                    )
                    _sel_tam = st.multiselect("Tamaño almacenaje (Hard/Soft)",
                                               _tam_vals, default=[],
                                               key="act_tam_filter",
                                               placeholder="Todos")
                else:
                    _sel_tam = []

            _act_view = _act_crit.copy()
            if _sel_seg:
                _act_view = _act_view[_act_view["SEGMENTO"].isin(_sel_seg)]
            if _sel_tam:
                _act_view = _act_view[
                    _act_view["TAMANO_ALMACENAJE"].fillna("Sin Info").astype(str).str.strip().isin(_sel_tam)
                ]

            # ── Display columns ──
            _disp_cols = [c for c in [
                "SEGMENTO", "SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "LINEA", "MARCA",
                "STOCK_UNIDADES", "STOCK_COSTO", "STOCK_CD_UND", "TIENDAS_CON_PERFIL",
                "MOI_HIST", "MOI_FC", "ANTIGUEDAD_MESES",
                "PRECIO_ACTUAL", "MARGEN_ACTUAL", "DCTO_SUGERIDO",
                "PRECIO_CON_DCTO", "MARGEN_CON_DCTO",
                "ROTACION_UND_MES", "MESES_LIQ", "ACCION_VENTA_RAW",
            ] if c in _act_view.columns]

            st.dataframe(
                _act_view[_disp_cols].rename(columns={
                    "SEGMENTO": "Segmento",
                    "SKU_PRODUCTO": "SKU",
                    "SKU_NOM_PRODUCTO": "Producto",
                    "LINEA": "Línea",
                    "MARCA": "Marca",
                    "STOCK_UNIDADES": "Stock (und)",
                    "STOCK_CD_UND": "Stock CD (und)",
                    "TIENDAS_CON_PERFIL": "Tiendas c/Perfil",
                    "MOI_HIST": "MOI Hist",
                    "MOI_FC": "MOI FC",
                    "ANTIGUEDAD_MESES": "Edad (m)",
                    "PRECIO_ACTUAL": "Precio Actual",
                    "MARGEN_ACTUAL": "Margen Actual",
                    "DCTO_SUGERIDO": "Dcto %",
                    "PRECIO_CON_DCTO": "Precio c/Dcto",
                    "MARGEN_CON_DCTO": "Margen c/Dcto",
                    "ROTACION_UND_MES": "Rot/mes",
                    "MESES_LIQ": "Meses Liq.",
                    "ACCION_VENTA_RAW": "Acción",
                }),
                column_config={
                    "Stock (und)": st.column_config.NumberColumn(format="%,.0f"),
                    "STOCK_COSTO": st.column_config.NumberColumn("Stock ($)", format="$%,.0f"),
                    "Stock CD (und)": st.column_config.NumberColumn(format="%,.0f"),
                    "Tiendas c/Perfil": st.column_config.NumberColumn(format="%.0f"),
                    "MOI Hist": st.column_config.NumberColumn(format="%.1f"),
                    "MOI FC": st.column_config.NumberColumn(format="%.1f"),
                    "Edad (m)": st.column_config.NumberColumn(format="%.0f"),
                    "Precio Actual": st.column_config.NumberColumn(format="$%,.0f"),
                    "Margen Actual": st.column_config.NumberColumn(format="%.1f%%",
                        help="Aporte / Venta Neta últimos 6 meses"),
                    "Dcto %": st.column_config.NumberColumn(format="%.0f%%"),
                    "Precio c/Dcto": st.column_config.NumberColumn(format="$%,.0f"),
                    "Margen c/Dcto": st.column_config.NumberColumn(format="%.1f%%",
                        help="Margen estimado post-descuento"),
                    "Rot/mes": st.column_config.NumberColumn(format="%.1f"),
                    "Meses Liq.": st.column_config.NumberColumn(format="%.1f"),
                },
                use_container_width=True,
                height=min(700, 40 + len(_act_view) * 35),
                hide_index=True,
            )

            # ── Download ──
            _act_export = _act_view[_disp_cols].copy()
            download_buttons(_act_export, prefix="lista_activacion")
