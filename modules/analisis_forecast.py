"""Analisis Forecast — pronostico estadistico de ventas unitarias por SKU/grupo.

Tabs:
  📈 Forecast     — metodos MA/SES/Naive/Ensemble con horizonte configurable.
  📊 Descomposicion — descomposicion multiplicativa Y = T × S × E con selector
                      de dimension (Todo / Area / Linea / Sublinea / Marca / SKU).

Granularidades: Semana / Mes / Año (compartidas e independientes por tab).
Fuente: VCM cantidad (unidades), ultimos 36 meses cerrados.
"""

from __future__ import annotations

import io

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, apply_pm_filter, dorel_layout
from db.cache import cached_query as cq
from utils.export import download_buttons
from utils.filters import norm_cols
from utils.ui_animations import lottie_spinner
from utils.ui_components import page_header

# ---------------------------------------------------------------------------
# Configuracion por granularidad
# ---------------------------------------------------------------------------

_GRAN_CFG: dict[str, dict] = {
    "Semana": {
        "fc_freq":        "W-MON",
        "fc_offset":      pd.DateOffset(weeks=1),
        "decomp_period":  52,
        "decomp_min_obs": 104,
        "hover_fmt":      "Sem %{x|%V %Y}",
        "xlabel":         "Semana",
        "tick_fmt":       "%d %b %y",
    },
    "Mes": {
        "fc_freq":        "MS",
        "fc_offset":      pd.DateOffset(months=1),
        "decomp_period":  12,
        "decomp_min_obs": 24,
        "hover_fmt":      "%{x|%b %Y}",
        "xlabel":         "Mes",
        "tick_fmt":       "%b %Y",
    },
    "Año": {
        "fc_freq":        "YS",
        "fc_offset":      pd.DateOffset(years=1),
        "decomp_period":  None,
        "decomp_min_obs": None,
        "hover_fmt":      "%{x|%Y}",
        "xlabel":         "Año",
        "tick_fmt":       "%Y",
    },
}

_NAIVE_PERIODS = {"Semana": 52, "Mes": 12, "Año": 1}

# ---------------------------------------------------------------------------
# Colores y metodos
# ---------------------------------------------------------------------------

_METHOD_COLORS = {
    "MA3":              "#065E8B",
    "MA6":              "#23CED3",
    "MA12":             "#632CFF",
    "SES":              "#F59E0B",
    "Naive Estacional": "#10B981",
}
_ENSEMBLE_COLOR = "#EF4444"
_ALL_METHODS    = list(_METHOD_COLORS.keys())

# Meses en español (1-based index)
_MESES_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
             "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

# Dimensiones para descomposicion por grupo
_DECOMP_DIMS: dict[str, str] = {
    "Area":     "AREA",
    "Linea":    "LINEA",
    "Sublinea": "SUBLINEA",
    "Marca":    "MARCA",
    "SKU":      "SKU_PRODUCTO",
}
_MAX_SKU_GROUPS = 60

# ---------------------------------------------------------------------------
# Resampleo de serie mensual a la granularidad solicitada
# ---------------------------------------------------------------------------

def _resample_series(monthly: pd.Series, gran: str) -> pd.Series:
    if gran == "Mes":
        return monthly.sort_index()
    if gran == "Año":
        return monthly.resample("YS").sum().sort_index()
    # Semana: distribuir totales mensuales por dia y agregar a semanas
    daily_parts: list[pd.Series] = []
    for ts, val in monthly.items():
        n_days = ts.days_in_month if hasattr(ts, "days_in_month") else pd.Period(ts, "M").days_in_month
        idx = pd.date_range(ts, periods=n_days, freq="D")
        daily_parts.append(pd.Series(val / n_days, index=idx))
    if not daily_parts:
        return pd.Series(dtype=float)
    return pd.concat(daily_parts).sort_index().resample("W-MON").sum().sort_index()


# ---------------------------------------------------------------------------
# Motor de forecast
# ---------------------------------------------------------------------------

def _ma(series: pd.Series, w: int) -> float | None:
    return float(series.iloc[-w:].mean()) if len(series) >= w else None


def _ses(series: pd.Series, alpha: float) -> float:
    s = float(series.iloc[0])
    for v in series.iloc[1:]:
        s = alpha * float(v) + (1.0 - alpha) * s
    return max(0.0, s)


def _naive_seasonal(series: pd.Series, step: int, seasonal_periods: int) -> float:
    lookback = step * seasonal_periods
    if len(series) >= lookback:
        return max(0.0, float(series.iloc[-lookback]))
    return max(0.0, float(series.mean()))


def generate_forecast(
    series: pd.Series,
    horizon: int,
    alpha: float,
    active_methods: list[str],
    gran: str = "Mes",
) -> pd.DataFrame:
    if series.empty or len(series) < 2:
        return pd.DataFrame()

    cfg              = _GRAN_CFG[gran]
    seasonal_periods = _NAIVE_PERIODS[gran]
    series           = series.sort_index()
    last             = series.index[-1]
    future           = pd.date_range(start=last + cfg["fc_offset"], periods=horizon, freq=cfg["fc_freq"])

    rows: list[dict] = []
    for i, p in enumerate(future, start=1):
        row: dict = {"PERIODO": p}
        if "MA3"  in active_methods: row["MA3"]  = _ma(series, 3)
        if "MA6"  in active_methods: row["MA6"]  = _ma(series, 6)
        if "MA12" in active_methods: row["MA12"] = _ma(series, 12)
        if "SES"  in active_methods: row["SES"]  = _ses(series, alpha)
        if "Naive Estacional" in active_methods:
            row["Naive Estacional"] = _naive_seasonal(series, i, seasonal_periods)
        vals        = [row[m] for m in active_methods if m in row and row[m] is not None]
        row["Ensemble"] = float(np.mean(vals)) if vals else None
        rows.append(row)

    df     = pd.DataFrame(rows)
    window = max(horizon, 3)
    sigma  = float(series.iloc[-window:].std(ddof=1)) if len(series) >= 2 else 0.0
    df["CI_LOW"]  = (df["Ensemble"] - sigma).clip(lower=0)
    df["CI_HIGH"] = df["Ensemble"] + sigma
    return df


# ---------------------------------------------------------------------------
# Descomposicion multiplicativa via statsmodels
# ---------------------------------------------------------------------------

def decompose_multiplicative(
    series: pd.Series,
    period: int,
    min_obs: int,
) -> dict[str, pd.Series] | None:
    from statsmodels.tsa.seasonal import seasonal_decompose

    s = series.sort_index().copy().astype(float)
    if len(s) < min_obs:
        return None
    if (s <= 0).any():
        min_pos = float(s[s > 0].min()) if (s > 0).any() else 1.0
        s = s.clip(lower=min_pos * 0.01)

    result = seasonal_decompose(s, model="multiplicative", period=period, extrapolate_trend="freq")
    return {"trend": result.trend, "seasonal": result.seasonal, "residual": result.resid}


# ---------------------------------------------------------------------------
# Descomposicion por grupo
# ---------------------------------------------------------------------------

def _decompose_by_group(
    df: pd.DataFrame,
    group_cols: list,
    gran: str,
    period: int,
    min_obs: int,
) -> dict:
    from statsmodels.tsa.seasonal import seasonal_decompose

    # Build composite group key column
    df = df.copy()
    if len(group_cols) == 1:
        df["__GRP__"] = df[group_cols[0]].astype(str)
    else:
        df["__GRP__"] = df[group_cols].apply(
            lambda r: " | ".join(r.astype(str)), axis=1
        )

    groups         = sorted(df["__GRP__"].dropna().unique().tolist())
    seasonal_rows: list[dict]         = []
    summary_rows:  list[dict]         = []
    trend_dict:    dict[str, pd.Series] = {}
    skipped:       list[str]          = []

    if gran == "Mes":
        period_labels = _MESES_ES
        def _pkey(idx): return idx.month
    else:
        period_labels = [f"W{w:02d}" for w in range(1, 53)]
        def _pkey(idx): return idx.isocalendar().week.astype(int)

    for grp in groups:
        monthly: pd.Series = (
            df[df["__GRP__"] == grp]
            .groupby("PERIODO")["UNIDADES"].sum().sort_index()
        )
        monthly.index = pd.DatetimeIndex(monthly.index).to_period("M").to_timestamp()
        s     = _resample_series(monthly, gran)
        n_obs = len(s)

        if n_obs < min_obs or s.sum() == 0:
            skipped.append(str(grp))
            summary_rows.append({"Grupo": str(grp), "Obs": n_obs,
                                  "Tendencia/periodo": None, "Amplitud estacional": None,
                                  "CV Error (%)": None, "Estado": f"Sin datos (min {min_obs})"})
            continue

        s_fit = s.copy().astype(float)
        if (s_fit <= 0).any():
            s_fit = s_fit.clip(lower=float(s_fit[s_fit > 0].min()) * 0.01 if (s_fit > 0).any() else 0.01)

        try:
            res = seasonal_decompose(s_fit, model="multiplicative", period=period, extrapolate_trend="freq")
        except Exception:
            skipped.append(str(grp))
            summary_rows.append({"Grupo": str(grp), "Obs": n_obs,
                                  "Tendencia/periodo": None, "Amplitud estacional": None,
                                  "CV Error (%)": None, "Estado": "Error en descomposicion"})
            continue

        sf     = pd.Series(res.seasonal.values, index=s.index)
        sf_grp = sf.groupby(_pkey(s.index)).mean()
        row    = {"Grupo": str(grp)}
        for i, lbl in enumerate(period_labels, start=1):
            row[lbl] = float(sf_grp.get(i, np.nan))
        seasonal_rows.append(row)
        trend_dict[str(grp)] = res.trend

        trend_slope = float(res.trend.diff().mean())
        season_amp  = float(sf_grp.max() - sf_grp.min())
        resid       = pd.Series(res.resid.values)
        resid_mean  = float(resid.dropna().mean())
        resid_cv    = float(resid.dropna().std() / resid_mean * 100) if resid_mean != 0 else 0.0
        summary_rows.append({"Grupo": str(grp), "Obs": n_obs,
                              "Tendencia/periodo": round(trend_slope, 2),
                              "Amplitud estacional": round(season_amp, 4),
                              "CV Error (%)": round(resid_cv, 2), "Estado": "OK"})

    seasonal_pivot = pd.DataFrame(seasonal_rows).set_index("Grupo") if seasonal_rows else pd.DataFrame()
    summary_df     = pd.DataFrame(summary_rows)
    trend_pivot    = pd.DataFrame(trend_dict).T if trend_dict else pd.DataFrame()
    if not trend_pivot.empty:
        trend_pivot.index.name = "Grupo"
    return {"seasonal_pivot": seasonal_pivot, "trend_pivot": trend_pivot,
            "summary": summary_df, "skipped": skipped}


# ---------------------------------------------------------------------------
# Helpers de UI — filtros jerarquicos
# ---------------------------------------------------------------------------

def _apply_hierarchy_filters(df: pd.DataFrame) -> pd.DataFrame:
    """Filtros en cascada: Canal → Area → Linea → Sublinea → Marca → Mix Oficial → Nombre Producto → Cod. Producto."""

    if "CANAL" in df.columns:
        sel = st.multiselect("Canal", sorted(df["CANAL"].dropna().unique().tolist()), key="af_canal")
        if sel: df = df[df["CANAL"].isin(sel)]

    sel = st.multiselect("Area", sorted(df["AREA"].dropna().unique().tolist()), key="af_areas")
    if sel: df = df[df["AREA"].isin(sel)]

    sel = st.multiselect("Linea", sorted(df["LINEA"].dropna().unique().tolist()), key="af_lineas")
    if sel: df = df[df["LINEA"].isin(sel)]

    sel = st.multiselect("Sublinea", sorted(df["SUBLINEA"].dropna().unique().tolist()), key="af_sub")
    if sel: df = df[df["SUBLINEA"].isin(sel)]

    if "MARCA" in df.columns:
        sel = st.multiselect("Marca", sorted(df["MARCA"].dropna().unique().tolist()), key="af_marca")
        if sel: df = df[df["MARCA"].isin(sel)]

    if "MIX_OFICIAL" in df.columns:
        sel = st.multiselect("Mix Oficial", sorted(df["MIX_OFICIAL"].dropna().unique().tolist()), key="af_mix")
        if sel: df = df[df["MIX_OFICIAL"].isin(sel)]

    if "NOM_PRODUCTO" in df.columns:
        sel = st.multiselect("Nombre Producto", sorted(df["NOM_PRODUCTO"].dropna().unique().tolist()), key="af_nom")
        if sel: df = df[df["NOM_PRODUCTO"].isin(sel)]

    sku_opts = (
        df[["SKU_PRODUCTO", "NOM_PRODUCTO"]].drop_duplicates()
        .assign(lbl=lambda x: x["SKU_PRODUCTO"].astype(str) + " — " + x["NOM_PRODUCTO"].fillna(""))
        .sort_values("lbl")["lbl"].tolist()
    )
    sel_lbls = st.multiselect("Cod. Producto (vacío = todo el grupo)", sku_opts, key="af_skus")
    if sel_lbls:
        sel_skus = [l.split(" — ")[0].strip() for l in sel_lbls]
        df = df[df["SKU_PRODUCTO"].astype(str).isin(sel_skus)]

    return df


# ---------------------------------------------------------------------------
# Helpers internos de render — Descomposicion
# ---------------------------------------------------------------------------

def _render_decomp_single(series: pd.Series, gran: str, cfg: dict) -> None:
    """Grafico 4-panel + KPIs + tabla de factores para una serie unica."""
    import plotly.subplots as sp

    decomp_period  = cfg["decomp_period"]
    decomp_min_obs = cfg["decomp_min_obs"]
    hover_fmt      = cfg["hover_fmt"]

    if decomp_period is None:
        st.info("La descomposicion estacional no esta disponible para granularidad Año.")
        return

    decomp = decompose_multiplicative(series, period=decomp_period, min_obs=decomp_min_obs)
    if decomp is None:
        st.info(f"Se necesitan al menos **{decomp_min_obs}** periodos. La seleccion tiene **{len(series)}**.")
        return

    trend    = decomp["trend"]
    seasonal = decomp["seasonal"]
    residual = decomp["residual"]

    d1, d2, d3 = st.columns(3)
    trend_slope = float(trend.dropna().diff().mean())
    season_grp  = seasonal.groupby(
        seasonal.index.isocalendar().week.astype(int) if gran == "Semana" else seasonal.index.month
    ).mean()
    season_amp = float(season_grp.max() - season_grp.min())
    resid_mean = float(residual.dropna().mean())
    resid_cv   = float(residual.dropna().std() / resid_mean * 100) if resid_mean != 0 else 0.0

    d1.metric(f"Tendencia / {gran.lower()}", f"{trend_slope:+,.1f} und",
              help="Cambio promedio de la tendencia entre periodos consecutivos.")
    d2.metric("Amplitud estacional", f"{season_amp:.3f}×",
              help="Diferencia entre el periodo de mayor y menor estacionalidad.")
    d3.metric("CV del residuo", f"{resid_cv:.1f}%",
              help="Coeficiente de variacion del error. Menor = mas predecible.")

    fig_d = sp.make_subplots(
        rows=4, cols=1, shared_xaxes=True,
        subplot_titles=("Serie Original (Unidades)", "Tendencia",
                        f"Estacionalidad (factor ×) — periodo={decomp_period}", "Error (residuo)"),
        vertical_spacing=0.07,
    )
    fig_d.update_layout(**dorel_layout(height=720, margin=dict(l=20, r=20, t=60, b=40)))
    fig_d.update_layout(showlegend=False)
    fig_d.update_xaxes(tickformat=cfg["tick_fmt"])

    fig_d.add_trace(go.Bar(x=series.index, y=series.values,
        marker_color=COLORS.get("primary", "#065E8B"), opacity=0.75,
        hovertemplate=f"{hover_fmt}: %{{y:,.0f}} und<extra>Original</extra>"), row=1, col=1)
    fig_d.add_trace(go.Scatter(x=trend.index, y=trend.values, mode="lines",
        line=dict(color="#F59E0B", width=2),
        hovertemplate=f"{hover_fmt}: %{{y:,.1f}}<extra>Tendencia</extra>"), row=2, col=1)
    fig_d.add_trace(go.Scatter(x=seasonal.index, y=seasonal.values, mode="lines+markers",
        line=dict(color="#10B981", width=1.5), marker=dict(size=3),
        hovertemplate=f"{hover_fmt}: %{{y:.3f}}×<extra>Estacionalidad</extra>"), row=3, col=1)
    fig_d.add_hline(y=1.0, line_dash="dash", line_color="#94a3b8", row=3, col=1)
    fig_d.add_trace(go.Scatter(x=residual.index, y=residual.values, mode="markers+lines",
        line=dict(color="#EF4444", width=1), marker=dict(size=3),
        hovertemplate=f"{hover_fmt}: %{{y:.3f}}<extra>Residuo</extra>"), row=4, col=1)
    fig_d.add_hline(y=1.0, line_dash="dash", line_color="#94a3b8", row=4, col=1)
    st.plotly_chart(fig_d, use_container_width=True)

    with st.expander("Factores estacionales por periodo"):
        if gran == "Mes":
            sf    = seasonal.groupby(seasonal.index.month).mean()
            sf_df = pd.DataFrame({"Mes": sf.index.map(lambda m: _MESES_ES[int(m) - 1]),
                                   "Factor": sf.values})
        else:
            sf    = seasonal.groupby(seasonal.index.isocalendar().week.astype(int)).mean()
            sf_df = pd.DataFrame({"Semana ISO": sf.index, "Factor": sf.values})
        sf_df["Factor"] = sf_df["Factor"].apply(lambda x: f"{x:.3f}×")
        st.dataframe(sf_df, use_container_width=True, hide_index=True)


def _render_decomp_grouped(
    df_filtered: pd.DataFrame,
    dim_labels: list,
    gran: str,
    cfg: dict,
    session_key: str,
) -> None:
    """Heatmap + tabla resumen para descomposicion por grupos (una o multiples dimensiones)."""
    decomp_period  = cfg["decomp_period"]
    decomp_min_obs = cfg["decomp_min_obs"]
    dim_cols       = [_DECOMP_DIMS[d] for d in dim_labels]
    dim_display    = " | ".join(dim_labels)

    # Count unique composite groups
    if all(c in df_filtered.columns for c in dim_cols):
        if len(dim_cols) == 1:
            n_groups = int(df_filtered[dim_cols[0]].nunique())
        else:
            n_groups = int(df_filtered[dim_cols].drop_duplicates().shape[0])
    else:
        n_groups = 0

    has_sku = "SKU" in dim_labels
    if has_sku and n_groups > _MAX_SKU_GROUPS:
        st.warning(f"Hay **{n_groups}** combinaciones. Se calcularan las **{_MAX_SKU_GROUPS}** de mayor volumen.")
    else:
        st.caption(f"Grupos a analizar: **{n_groups}** combinacion(es) de **{dim_display}**")

    if st.button("Calcular", type="primary", key=f"{session_key}_btn"):
        df_calc = df_filtered.copy()
        if has_sku and n_groups > _MAX_SKU_GROUPS:
            top = df_calc.groupby("SKU_PRODUCTO")["UNIDADES"].sum().nlargest(_MAX_SKU_GROUPS).index
            df_calc = df_calc[df_calc["SKU_PRODUCTO"].isin(top)]
        with st.spinner(f"Calculando {n_groups} grupos [{dim_display}]..."):
            result = _decompose_by_group(df_calc, dim_cols, gran, decomp_period, decomp_min_obs)
        st.session_state[session_key] = {"result": result, "dim": dim_display, "gran": gran}

    cached = st.session_state.get(session_key)
    if cached is None:
        return

    result    = cached["result"]
    dim_used  = cached["dim"]
    gran_used = cached["gran"]

    seasonal_pivot = result["seasonal_pivot"]
    summary_df     = result["summary"]
    skipped        = result["skipped"]

    if skipped:
        st.caption(f"⚠️ {len(skipped)} omitido(s): " + ", ".join(skipped[:10])
                   + ("…" if len(skipped) > 10 else ""))

    if not seasonal_pivot.empty:
        lbl_tipo = "Mes" if gran_used == "Mes" else "Semana ISO"
        st.markdown(f"**Factores Estacionales — {dim_used} × {lbl_tipo}**")
        st.caption("Factor > 1 = demanda sobre el promedio anual | Factor < 1 = bajo el promedio")

        z_vals    = seasonal_pivot.values.astype(float)
        y_display = [str(y)[:40] + "…" if len(str(y)) > 40 else str(y)
                     for y in seasonal_pivot.index.tolist()]

        fig_hm = go.Figure(layout=dorel_layout(
            height=max(320, len(y_display) * 26 + 120),
            margin=dict(l=20, r=20, t=50, b=50),
        ))
        fig_hm.update_layout(showlegend=False)
        fig_hm.add_trace(go.Heatmap(
            z=z_vals, x=seasonal_pivot.columns.tolist(), y=y_display,
            colorscale="RdYlGn", zmid=1.0,
            colorbar=dict(title="Factor ×", thickness=14, len=0.8),
            hovertemplate="<b>%{y}</b><br>Periodo: %{x}<br>Factor: %{z:.3f}×<extra></extra>",
            text=[[f"{v:.2f}" if not np.isnan(v) else "" for v in row] for row in z_vals],
            texttemplate="%{text}", textfont=dict(size=9),
        ))
        st.plotly_chart(fig_hm, use_container_width=True)

    if not summary_df.empty:
        st.markdown(f"**Resumen estadistico — {dim_used}**")
        df_s = summary_df.copy()
        for col, fmt in [
            ("Tendencia/periodo",   lambda x: f"{x:+.2f}" if pd.notna(x) else "—"),
            ("Amplitud estacional", lambda x: f"{x:.4f}×" if pd.notna(x) else "—"),
            ("CV Error (%)",        lambda x: f"{x:.1f}%"  if pd.notna(x) else "—"),
        ]:
            if col in df_s.columns:
                df_s[col] = df_s[col].apply(fmt)
        st.dataframe(df_s, use_container_width=True, hide_index=True,
                     column_config={
                         "Tendencia/periodo":   st.column_config.TextColumn("Tendencia / periodo", width="medium"),
                         "Amplitud estacional": st.column_config.TextColumn("Amplitud estacional", width="medium"),
                         "CV Error (%)":        st.column_config.TextColumn("CV Error (%)",        width="medium"),
                     })

    buf_grp = io.BytesIO()
    with pd.ExcelWriter(buf_grp, engine="openpyxl") as writer:
        if not seasonal_pivot.empty:
            seasonal_pivot.reset_index().to_excel(writer, index=False, sheet_name="Factores Estacionales")
        if not summary_df.empty:
            summary_df.to_excel(writer, index=False, sheet_name="Resumen")
    buf_grp.seek(0)
    safe_dim = dim_used.lower().replace(" | ", "_")
    st.download_button(
        label=f"📥 Descargar factores por {dim_used} (Excel)",
        data=buf_grp.getvalue(),
        file_name=f"factores_{safe_dim}_{gran_used.lower()}_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{session_key}_dl",
    )


# ---------------------------------------------------------------------------
# Render principal
# ---------------------------------------------------------------------------

def render_analisis_forecast(conn):
    st.html(page_header(
        "Analisis Forecast",
        "Pronostico estadistico de ventas unitarias — VCM 36 meses",
        period_text="Unidades vendidas",
    ))

    if conn is None:
        st.warning("Sin conexion a Snowflake.")
        return

    # ── Carga de datos (compartida entre tabs) ────────────────────────────────
    with lottie_spinner("snowflake"):
        df_raw = cq.vcm_forecast_historico(conn)

    if df_raw is None or df_raw.empty:
        st.warning("Sin datos de ventas en VCM para el periodo solicitado.")
        return

    df_raw = norm_cols(df_raw)
    df_raw = apply_pm_filter(df_raw)
    df_raw["PERIODO"]  = pd.to_datetime(df_raw["PERIODO"])
    df_raw["UNIDADES"] = pd.to_numeric(df_raw["UNIDADES"], errors="coerce").fillna(0)

    # ── Filtros (compartidos entre tabs) ──────────────────────────────────────
    with st.expander("Filtros", expanded=True):
        df_filtered = _apply_hierarchy_filters(df_raw)

    if df_filtered.empty:
        st.info("No hay datos para los filtros seleccionados.")
        return

    n_skus  = int(df_filtered["SKU_PRODUCTO"].nunique())
    n_meses = int(df_filtered["PERIODO"].nunique())
    st.caption(f"Seleccion: **{n_skus}** SKU(s) | **{n_meses}** meses de historia")

    # Serie mensual base compartida
    monthly_series: pd.Series = (
        df_filtered.groupby("PERIODO")["UNIDADES"].sum().sort_index()
    )
    monthly_series.index = pd.DatetimeIndex(monthly_series.index).to_period("M").to_timestamp()

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab_fc, tab_decomp = st.tabs(["📈 Forecast", "📊 Descomposicion"])

    # =========================================================================
    # TAB FORECAST
    # =========================================================================
    with tab_fc:
        with st.expander("Configuracion", expanded=True):
            g_col, m_col, h_col, a_col = st.columns([2, 3, 1, 1])
            with g_col:
                gran = st.radio("Granularidad", ["Semana", "Mes", "Año"],
                                index=1, horizontal=True, key="af_gran")
            with m_col:
                active_methods = st.multiselect("Metodos activos", _ALL_METHODS,
                                                default=["MA3", "MA6", "SES"], key="af_methods")
            with h_col:
                horizon = int(st.number_input("Horizonte (periodos)",
                                              min_value=1, max_value=52, value=6, step=1, key="af_horizon"))
            with a_col:
                alpha = float(st.slider("Alpha SES", 0.05, 0.95, 0.30, 0.05, key="af_alpha",
                                        help="Peso reciente. Alto = mas reactivo al ultimo dato."))

        if not active_methods:
            st.warning("Selecciona al menos un metodo de forecast.")
        else:
            cfg_fc = _GRAN_CFG[gran]
            series = _resample_series(monthly_series, gran)

            if series.empty or len(series) < 2:
                st.warning("Datos insuficientes para la granularidad seleccionada.")
            else:
                if gran == "Semana":
                    st.caption("ℹ️ Datos semanales aproximados: total mensual distribuido equitativamente.")

                df_fc = generate_forecast(series, horizon, alpha, active_methods, gran=gran)
                if df_fc.empty:
                    st.warning("Datos insuficientes para generar forecast.")
                else:
                    total_fc  = float(df_fc["Ensemble"].sum())
                    hist_prev = float(series.iloc[-horizon:].sum()) if len(series) >= horizon else float(series.sum())
                    delta_pct = ((total_fc / hist_prev) - 1.0) * 100.0 if hist_prev > 0 else 0.0

                    k1, k2, k3, k4 = st.columns(4)
                    k1.metric(f"Forecast Ensemble ({horizon} {gran.lower()}s)", f"{total_fc:,.0f} und")
                    k2.metric("Mismo periodo anterior", f"{hist_prev:,.0f} und")
                    k3.metric("Variacion", f"{delta_pct:+.1f}%", delta=f"{delta_pct:+.1f}%")
                    k4.metric(f"Promedio / {gran.lower()}", f"{total_fc / horizon:,.0f} und")

                    hist_show = series.iloc[-min(len(series), 104):].reset_index()
                    hist_show.columns = ["PERIODO", "UNIDADES"]
                    hover_fmt = cfg_fc["hover_fmt"]

                    fig = go.Figure(layout=dorel_layout(
                        title=dict(text=f"Ventas Reales vs Forecast — por {gran}", font_size=15, x=0.5),
                        xaxis=dict(title=cfg_fc["xlabel"], tickformat=cfg_fc["tick_fmt"]),
                        yaxis=dict(title="Unidades"), height=460,
                        legend=dict(orientation="h", y=-0.18), barmode="overlay",
                    ))
                    fig.add_trace(go.Bar(x=hist_show["PERIODO"], y=hist_show["UNIDADES"],
                        name="Ventas Reales", marker_color=COLORS.get("primary", "#065E8B"), opacity=0.75,
                        hovertemplate=f"<b>{hover_fmt}</b><br>Real: %{{y:,.0f}} und<extra></extra>"))
                    x_band = pd.concat([df_fc["PERIODO"], df_fc["PERIODO"].iloc[::-1]])
                    y_band = pd.concat([df_fc["CI_HIGH"],  df_fc["CI_LOW"].iloc[::-1]])
                    fig.add_trace(go.Scatter(x=x_band, y=y_band, fill="toself",
                        fillcolor="rgba(239,68,68,0.10)", line_color="rgba(0,0,0,0)",
                        name="Banda ±1σ", hoverinfo="skip"))
                    for m in active_methods:
                        if m not in df_fc.columns: continue
                        fig.add_trace(go.Scatter(x=df_fc["PERIODO"], y=df_fc[m],
                            mode="lines+markers", name=m,
                            line=dict(color=_METHOD_COLORS[m], dash="dot", width=1.8),
                            marker=dict(size=5),
                            hovertemplate=f"<b>{hover_fmt}</b><br>{m}: %{{y:,.0f}} und<extra></extra>"))
                    fig.add_trace(go.Scatter(x=df_fc["PERIODO"], y=df_fc["Ensemble"],
                        mode="lines+markers", name="Ensemble",
                        line=dict(color=_ENSEMBLE_COLOR, width=2.5),
                        marker=dict(size=8, symbol="diamond"),
                        hovertemplate=f"<b>{hover_fmt}</b><br>Ensemble: %{{y:,.0f}} und<extra></extra>"))
                    st.plotly_chart(fig, use_container_width=True)

                    st.markdown("#### Detalle del forecast")
                    fmt_map   = {"Semana": "%G-W%V", "Mes": "%Y-%m", "Año": "%Y"}
                    disp_cols = (["PERIODO"]
                                 + [m for m in active_methods if m in df_fc.columns]
                                 + ["Ensemble", "CI_LOW", "CI_HIGH"])
                    df_disp   = df_fc[disp_cols].copy()
                    df_disp["PERIODO"] = df_disp["PERIODO"].dt.strftime(fmt_map[gran])
                    df_disp = df_disp.rename(columns={"CI_LOW": "IC Inferior (−1σ)", "CI_HIGH": "IC Superior (+1σ)"})
                    for col in df_disp.columns[1:]:
                        df_disp[col] = df_disp[col].apply(lambda x: f"{x:,.0f}" if pd.notna(x) else "—")
                    st.dataframe(df_disp, use_container_width=True, hide_index=True)

                    st.markdown("---")
                    fmt_str     = fmt_map[gran]
                    df_export   = df_fc.copy()
                    df_export["PERIODO"] = df_export["PERIODO"].dt.strftime(fmt_str)
                    hist_export = hist_show.copy()
                    hist_export["PERIODO"] = hist_export["PERIODO"].dt.strftime(fmt_str)
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                        df_export.to_excel(writer,   index=False, sheet_name="Forecast")
                        hist_export.to_excel(writer, index=False, sheet_name="Historico")
                    buf.seek(0)
                    download_buttons(df_export, prefix=f"forecast_{gran.lower()}", excel_buffer=buf)

    # =========================================================================
    # TAB DESCOMPOSICION
    # =========================================================================
    with tab_decomp:
        st.markdown("##### Descomposicion Multiplicativa  `Y = Tendencia × Estacionalidad × Error`")

        dc1, dc2 = st.columns([2, 3])
        with dc1:
            gran_d = st.radio("Granularidad", ["Semana", "Mes", "Año"],
                              index=1, horizontal=True, key="ad_gran")
        with dc2:
            dim_sel = st.multiselect(
                "Dimensiones de analisis (vacio = serie total agregada)",
                list(_DECOMP_DIMS.keys()),
                default=[],
                key="ad_dim",
                help=(
                    "Selecciona una o mas dimensiones para combinarlas como clave de grupo. "
                    "Ejemplo: Linea + Marca crea grupos 'COCHE | BRAND_A'. "
                    "Sin seleccion descompone la serie total de la seleccion actual."
                ),
            )

        cfg_d    = _GRAN_CFG[gran_d]
        series_d = _resample_series(monthly_series, gran_d)

        if series_d.empty or len(series_d) < 2:
            st.warning("Datos insuficientes para la granularidad seleccionada.")
        elif cfg_d["decomp_period"] is None:
            st.info("La descomposicion estacional no esta disponible para granularidad Año.")
        else:
            if gran_d == "Semana":
                st.caption("ℹ️ Datos semanales aproximados: total mensual distribuido equitativamente.")

            st.markdown("---")

            if not dim_sel:
                _render_decomp_single(series_d, gran_d, cfg_d)
            else:
                _render_decomp_grouped(
                    df_filtered, dim_sel, gran_d, cfg_d,
                    session_key=f"ad_grp_{'_'.join(dim_sel)}_{gran_d}",
                )
