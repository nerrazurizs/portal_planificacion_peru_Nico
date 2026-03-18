"""Historical InStock Dashboard — Tiendas & CD.

Replaces PowerBI InStock panels with a faster, Python-powered alternative.
Queries pre-aggregated weekly data (Mondays + latest) from ft_in_stock and
ft_in_stock_cd, then renders time series, dimension breakdowns, and detail
tables with full Plotly interactivity.

Key feature: toggle "Solo SKUs con InStock CD = 1" to measure tienda
availability only for products the CD actually had in stock (true fill rate).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, dorel_layout, apply_pm_filter
from db.cache import cached_query as cq
from utils.export import download_buttons
from utils.filters import human_format, norm_cols
from utils.ui_animations import lottie_spinner


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_WINDOW_OPTIONS = {"90 Dias": "90", "180 Dias": "180", "365 Dias": "365"}
_WINDOW_DEFAULT = "90 Dias"

# Column name mapping per window — regular and perfil (solo tiendas con reposicion)
_COL_MAP_TIENDA = {
    "90":  {"is": "TIENDAS_IS90",  "n": "N_TIENDAS_IS90",
            "isb": "TIENDAS_IS90B",  "cd": "INSTOCK_CD_90",
            "is_perfil": "TIENDAS_IS90_PERFIL",  "n_perfil": "N_TIENDAS_IS90_PERFIL"},
    "180": {"is": "TIENDAS_IS180", "n": "N_TIENDAS_IS180",
            "isb": "TIENDAS_IS180B", "cd": "INSTOCK_CD_180",
            "is_perfil": "TIENDAS_IS180_PERFIL", "n_perfil": "N_TIENDAS_IS180_PERFIL"},
    "365": {"is": "TIENDAS_IS365", "n": "N_TIENDAS_IS365",
            "isb": "TIENDAS_IS365B", "cd": "INSTOCK_CD_365",
            "is_perfil": "TIENDAS_IS365_PERFIL", "n_perfil": "N_TIENDAS_IS365_PERFIL"},
}
_COL_MAP_CD = {
    "90":  "INSTOCK_CD_90",
    "180": "INSTOCK_CD_180",
    "365": "INSTOCK_CD_365",
}

# CD filter always uses 90-day window (matches PowerBI standard: "In Stock CD 90: 1")
_CD_FILTER_COL = "INSTOCK_CD_90"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_pct(num: float, den: float) -> float:
    """Return num/den as percentage (0-1), or 0.0 if den <= 0."""
    return num / den if den > 0 else 0.0


def _aggregate_instock_tienda(
    df: pd.DataFrame,
    window: str,
    group_cols: list[str],
    filter_cd: bool = False,
    solo_perfil: bool = False,
) -> pd.DataFrame:
    """Aggregate tienda InStock % by group_cols.

    InStock % = sum(tiendas_with_stock) / sum(n_tiendas) per group.
    If *filter_cd*, only include rows where instock_cd for this window == 1.
    If *solo_perfil*, use only tiendas with PERFIL configured (reposicion).
    """
    cols = _COL_MAP_TIENDA[window]

    # Choose columns: perfil-filtered or all tiendas
    # Fallback chain: perfil → window-specific denom → legacy N_TIENDAS
    if solo_perfil and cols["is_perfil"] in df.columns and cols["n_perfil"] in df.columns:
        is_col = cols["is_perfil"]
        n_col = cols["n_perfil"]
    elif cols["n"] in df.columns:
        is_col = cols["is"]
        n_col = cols["n"]
    else:
        is_col = cols["is"]
        n_col = "N_TIENDAS"  # Legacy fallback (stale cache)

    work = df.copy()
    if filter_cd:
        work = work[work[_CD_FILTER_COL] >= 1]

    if work.empty:
        return pd.DataFrame(columns=group_cols + ["INSTOCK_PCT", "N_COMBINACIONES"])

    agg = work.groupby(group_cols, dropna=False).agg(
        _sum_is=(is_col, "sum"),
        _sum_n=(n_col, "sum"),
    ).reset_index()

    agg["INSTOCK_PCT"] = np.where(agg["_sum_n"] > 0, agg["_sum_is"] / agg["_sum_n"], 0.0)
    agg["N_COMBINACIONES"] = agg["_sum_n"]
    return agg.drop(columns=["_sum_is", "_sum_n"])


def _aggregate_instock_cd(
    df: pd.DataFrame,
    window: str,
    group_cols: list[str],
) -> pd.DataFrame:
    """Aggregate CD InStock % by group_cols.

    InStock % = count(instock_cd==1) / count(*) per group.
    """
    cd_col = _COL_MAP_CD[window]
    work = df.copy()

    agg = work.groupby(group_cols, dropna=False).agg(
        _sum_is=(cd_col, "sum"),
        _count=(cd_col, "count"),
    ).reset_index()

    agg["INSTOCK_PCT"] = np.where(agg["_count"] > 0, agg["_sum_is"] / agg["_count"], 0.0)
    agg["N_SKUS"] = agg["_count"]
    return agg.drop(columns=["_sum_is", "_count"])


def _reaggregate_by_period(
    df: pd.DataFrame,
    period: str,
    is_tienda: bool,
    window: str,
    filter_cd: bool = False,
    solo_perfil: bool = False,
    group_cols: list[str] | None = None,
) -> pd.DataFrame:
    """Re-aggregate SKU×date data into period buckets with correct IS%.

    Parameters
    ----------
    period : str
        ``"D"`` daily (delegates to existing funcs), ``"W"`` weekly,
        ``"M"`` monthly, ``"Q"`` quarterly.
    is_tienda : bool
        True → tienda logic, False → CD logic.
    group_cols : list[str] | None
        Extra dimension columns (e.g. ``["AREA"]``).  ``FECHA`` is always
        included and should **not** appear here.
    """
    extra = group_cols or []

    # Daily → delegate to existing functions (no re-aggregation)
    if period == "D":
        if is_tienda:
            return _aggregate_instock_tienda(
                df, window, ["FECHA"] + extra,
                filter_cd=filter_cd, solo_perfil=solo_perfil,
            )
        return _aggregate_instock_cd(df, window, ["FECHA"] + extra)

    if df.empty:
        cols = ["FECHA"] + extra + ["INSTOCK_PCT"]
        cols.append("N_COMBINACIONES" if is_tienda else "N_SKUS")
        return pd.DataFrame(columns=cols)

    work = df.copy()
    # Bucket dates
    work["_PERIODO"] = work["FECHA"].dt.to_period(period).dt.start_time

    if is_tienda:
        cols_map = _COL_MAP_TIENDA[window]
        if solo_perfil and cols_map["is_perfil"] in work.columns and cols_map["n_perfil"] in work.columns:
            is_col, n_col = cols_map["is_perfil"], cols_map["n_perfil"]
        elif cols_map["n"] in work.columns:
            is_col, n_col = cols_map["is"], cols_map["n"]
        else:
            is_col, n_col = cols_map["is"], "N_TIENDAS"

        if filter_cd:
            work = work[work[_CD_FILTER_COL] >= 1]
        if work.empty:
            return pd.DataFrame(columns=["FECHA"] + extra + ["INSTOCK_PCT", "N_COMBINACIONES"])

        agg_dict: dict = {
            "_sum_is": (is_col, "sum"),
            "_sum_n": (n_col, "sum"),
        }
        # Stock positions → mean (not sum, it's a snapshot)
        for sc in ["STOCK_UND_TIENDA", "STOCK_COSTO_TIENDA"]:
            if sc in work.columns:
                agg_dict[sc] = (sc, "mean")

        agg = work.groupby(["_PERIODO"] + extra, dropna=False).agg(**agg_dict).reset_index()
        agg["INSTOCK_PCT"] = np.where(agg["_sum_n"] > 0, agg["_sum_is"] / agg["_sum_n"], 0.0)
        agg["N_COMBINACIONES"] = agg["_sum_n"]
        agg = agg.drop(columns=["_sum_is", "_sum_n"])
    else:
        cd_col = _COL_MAP_CD[window]
        agg_dict = {
            "_sum_is": (cd_col, "sum"),
            "_count": (cd_col, "count"),
        }
        for sc in ["STOCK_UND_CD", "STOCK_COSTO_CD"]:
            if sc in work.columns:
                agg_dict[sc] = (sc, "mean")

        agg = work.groupby(["_PERIODO"] + extra, dropna=False).agg(**agg_dict).reset_index()
        agg["INSTOCK_PCT"] = np.where(agg["_count"] > 0, agg["_sum_is"] / agg["_count"], 0.0)
        agg["N_SKUS"] = agg["_count"]
        agg = agg.drop(columns=["_sum_is", "_count"])

    agg = agg.rename(columns={"_PERIODO": "FECHA"})
    return agg


def _format_pct(v: float) -> str:
    """Format 0-1 float as percentage string."""
    return f"{v * 100:.1f}%"


def _kpi_card_html(label: str, value: str, delta: str = "", color: str = "") -> str:
    """Return HTML for a mini KPI card."""
    delta_html = ""
    if delta:
        d_color = color or "#333"
        delta_html = f'<span style="font-size:13px;color:{d_color};margin-left:6px;">{delta}</span>'
    return f"""
    <div style="background:#fff;border-radius:10px;padding:16px 20px;
                box-shadow:0 1px 4px rgba(0,0,0,0.06);text-align:center;">
        <div style="font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;">
            {label}
        </div>
        <div style="font-size:28px;font-weight:700;color:#1a1a1a;margin-top:4px;">
            {value}{delta_html}
        </div>
    </div>"""


# ─────────────────────────────────────────────────────────────────────────────
# Dimension pivot table (like PowerBI daily tables)
# ─────────────────────────────────────────────────────────────────────────────

def _build_pivot_table(
    agg_by_date_dim: pd.DataFrame,
    dim_col: str,
    top_n: int = 15,
    agg_period: str = "D",
) -> pd.DataFrame | None:
    """Build a pivot table: rows = dimension, columns = dates, values = InStock %.

    Returns a styled DataFrame ready for st.dataframe().
    """
    if agg_by_date_dim.empty:
        return None

    # Keep only top_n dimensions by latest InStock %
    latest_date = agg_by_date_dim["FECHA"].max()
    latest = agg_by_date_dim[agg_by_date_dim["FECHA"] == latest_date]
    top_dims = (
        latest.nlargest(top_n, "N_COMBINACIONES")[dim_col].tolist()
        if "N_COMBINACIONES" in latest.columns
        else latest.nlargest(top_n, "N_SKUS")[dim_col].tolist()
    )
    filtered = agg_by_date_dim[agg_by_date_dim[dim_col].isin(top_dims)]

    pvt = filtered.pivot_table(
        index=dim_col,
        columns="FECHA",
        values="INSTOCK_PCT",
        aggfunc="mean",
    )

    # Sort dates ascending and format adaptively
    pvt = pvt.reindex(columns=sorted(pvt.columns))

    def _fmt_col(c):
        if not hasattr(c, "strftime"):
            return str(c)
        if agg_period == "M":
            return c.strftime("%m/%Y")
        if agg_period == "Q":
            return f"Q{(c.month - 1) // 3 + 1}/{c.year}"
        return c.strftime("%d/%m")

    pvt.columns = [_fmt_col(c) for c in pvt.columns]

    # Add Total row (weighted by N_COMBINACIONES / N_SKUS if available)
    if not filtered.empty:
        weight_col = ("N_COMBINACIONES" if "N_COMBINACIONES" in filtered.columns
                      else "N_SKUS" if "N_SKUS" in filtered.columns
                      else None)
        if weight_col:
            _tmp = filtered[["FECHA", "INSTOCK_PCT", weight_col]].copy()
            _tmp["_w_is"] = _tmp["INSTOCK_PCT"] * _tmp[weight_col]
            _g = _tmp.groupby("FECHA").agg(
                _wsum=("_w_is", "sum"),
                _wden=(weight_col, "sum"),
            ).reset_index()
            _g["_val"] = np.where(_g["_wden"] > 0, _g["_wsum"] / _g["_wden"], 0.0)
        else:
            _g = (
                filtered.groupby("FECHA")["INSTOCK_PCT"]
                .mean()
                .reset_index(name="_val")
            )
        total_row = {}
        for _, r in _g.iterrows():
            col_name = r["FECHA"].strftime("%d/%m") if hasattr(r["FECHA"], "strftime") else str(r["FECHA"])
            total_row[col_name] = r["_val"]
        pvt.loc["Total"] = pd.Series(total_row)

    return pvt


def _render_pivot_styled(pvt: pd.DataFrame, title: str):
    """Render a pivot table with color-coded percentages."""
    if pvt is None or pvt.empty:
        st.info("Sin datos para mostrar.")
        return

    bg = COLORS["primary"]
    st.html(
        f"<div style='background:{bg};color:white;padding:8px 16px;"
        f"border-radius:6px 6px 0 0;font-weight:600;font-size:14px;'>"
        f"{title}</div>"
    )

    # Format as percentages for display
    styled = pvt.map(lambda v: f"{v * 100:.1f} %" if pd.notna(v) else "")

    def _color_cell(v):
        try:
            num = float(str(v).replace(" %", "").replace(",", ".")) / 100
        except (ValueError, TypeError):
            return ""
        if num >= 0.97:
            return "background-color: #c8e6c9; color: #1b5e20;"
        if num >= 0.93:
            return "background-color: #dcedc8; color: #33691e;"
        if num >= 0.88:
            return "background-color: #fff9c4; color: #f57f17;"
        if num >= 0.80:
            return "background-color: #ffe0b2; color: #e65100;"
        return "background-color: #ffcdd2; color: #b71c1c;"

    st.dataframe(
        styled.style.map(_color_cell),
        use_container_width=True,
        height=min(450, 40 + len(pvt) * 35),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main render function
# ─────────────────────────────────────────────────────────────────────────────

def _build_combined_summary_table(
    df_tienda: pd.DataFrame,
    df_cd: pd.DataFrame,
    window: str,
    filter_cd: bool,
    solo_perfil: bool,
    group_col: str = "AREA",
) -> pd.DataFrame | None:
    """Build a PowerBI-style summary: Area/Linea | IS% Tienda | IS% CD | N SKUs.

    Color-coded with green ≥93%, yellow ≥85%, red <85%.
    """
    t_latest = df_tienda[df_tienda["FECHA"] == df_tienda["FECHA"].max()] if not df_tienda.empty else df_tienda
    cd_latest = df_cd[df_cd["FECHA"] == df_cd["FECHA"].max()] if not df_cd.empty else df_cd

    agg_t = _aggregate_instock_tienda(
        t_latest, window, [group_col],
        filter_cd=filter_cd, solo_perfil=solo_perfil,
    )
    agg_cd = _aggregate_instock_cd(cd_latest, window, [group_col])

    if agg_t.empty and agg_cd.empty:
        return None

    # Merge tienda + CD
    merged = agg_t[[group_col, "INSTOCK_PCT", "N_COMBINACIONES"]].rename(
        columns={"INSTOCK_PCT": "IS_TIENDA", "N_COMBINACIONES": "N_COMBOS_TIENDA"}
    ) if not agg_t.empty else pd.DataFrame(columns=[group_col, "IS_TIENDA", "N_COMBOS_TIENDA"])

    cd_slim = agg_cd[[group_col, "INSTOCK_PCT", "N_SKUS"]].rename(
        columns={"INSTOCK_PCT": "IS_CD", "N_SKUS": "N_SKUS_CD"}
    ) if not agg_cd.empty else pd.DataFrame(columns=[group_col, "IS_CD", "N_SKUS_CD"])

    if merged.empty:
        combined = cd_slim
    elif cd_slim.empty:
        combined = merged
    else:
        combined = merged.merge(cd_slim, on=group_col, how="outer")

    combined = combined.fillna(0)

    # Total row
    total = {group_col: "TOTAL"}
    if not agg_t.empty:
        t_sum_is = agg_t["N_COMBINACIONES"].dot(agg_t["INSTOCK_PCT"])
        t_sum_n = agg_t["N_COMBINACIONES"].sum()
        total["IS_TIENDA"] = t_sum_is / t_sum_n if t_sum_n > 0 else 0
        total["N_COMBOS_TIENDA"] = t_sum_n
    if not agg_cd.empty:
        cd_sum_is = agg_cd["N_SKUS"].dot(agg_cd["INSTOCK_PCT"])
        cd_sum_n = agg_cd["N_SKUS"].sum()
        total["IS_CD"] = cd_sum_is / cd_sum_n if cd_sum_n > 0 else 0
        total["N_SKUS_CD"] = cd_sum_n

    combined = pd.concat([combined, pd.DataFrame([total])], ignore_index=True)

    # Sort by IS_TIENDA desc (Total at bottom) — fallback to IS_CD if no store data
    mask_total = combined[group_col] == "TOTAL"
    _sort_col = "IS_TIENDA" if "IS_TIENDA" in combined.columns else (
        "IS_CD" if "IS_CD" in combined.columns else None
    )
    top = combined[~mask_total].sort_values(_sort_col, ascending=False) if _sort_col else combined[~mask_total]
    combined = pd.concat([top, combined[mask_total]], ignore_index=True)

    return combined


def _render_combined_summary(combined: pd.DataFrame, title: str, group_col: str = "AREA"):
    """Render the PowerBI-style combined summary table with color coding."""
    if combined is None or combined.empty:
        st.info("Sin datos para resumen.")
        return

    bg = COLORS["primary"]
    st.html(
        f"<div style='background:{bg};color:white;padding:8px 16px;"
        f"border-radius:6px 6px 0 0;font-weight:600;font-size:14px;'>"
        f"{title}</div>"
    )

    def _color_pct(val):
        try:
            num = float(val)
        except (ValueError, TypeError):
            return ""
        if num >= 0.93:
            return "background-color: #c8e6c9; color: #1b5e20;"
        if num >= 0.85:
            return "background-color: #fff9c4; color: #f57f17;"
        return "background-color: #ffcdd2; color: #b71c1c;"

    display = combined.copy()
    rename_map = {
        group_col: group_col.title(),
        "IS_TIENDA": "IS% Tienda",
        "IS_CD": "IS% CD",
        "N_COMBOS_TIENDA": "SKU×Tienda",
        "N_SKUS_CD": "SKUs CD",
    }
    display = display.rename(columns={k: v for k, v in rename_map.items() if k in display.columns})

    # Format
    for pct_col in ["IS% Tienda", "IS% CD"]:
        if pct_col in display.columns:
            display[pct_col] = display[pct_col].apply(lambda v: f"{v*100:.1f}%" if pd.notna(v) else "")
    for int_col in ["SKU×Tienda", "SKUs CD"]:
        if int_col in display.columns:
            display[int_col] = display[int_col].apply(lambda v: f"{int(v):,}" if pd.notna(v) and v > 0 else "")

    def _style_row(row):
        styles = [""] * len(row)
        is_total = str(row.iloc[0]).upper() == "TOTAL"
        for i, col in enumerate(row.index):
            if is_total:
                styles[i] = "font-weight: bold; background-color: #e3f2fd;"
            if col in ["IS% Tienda", "IS% CD"]:
                try:
                    num = float(str(row[col]).replace("%", "")) / 100
                    styles[i] += _color_pct(num)
                except (ValueError, TypeError):
                    pass
        return styles

    st.dataframe(
        display.style.apply(_style_row, axis=1),
        use_container_width=True,
        height=min(450, 40 + len(display) * 35),
        hide_index=True,
    )


def _compute_projected_instock(conn, apply_filters_fn=None, filter_cd=False) -> pd.DataFrame | None:
    """Compute projected InStock by adding unified transit to current stock.

    Uses **ft_in_stock** store-level data (instock_store_detail) as the base,
    which is the same source the pre-aggregated InStock dashboard reads from.
    This guarantees IS% Actual matches the dashboard exactly.

    Pipeline:
    1. instock_store_detail → store×SKU rows where perfil='SI' (latest date)
       Each row has stock_unidades and cantidad_prom_90 from ft_in_stock.
    1b. (optional) filter by CD InStock ≥ 1 for alignment with main dashboard
    2. unified_transit → transit per dest×SKU
    3. LEFT JOIN transit onto base
    4. IS flags (same formula as Snowflake):
       IS_ACTUAL = stock > 0 AND stock >= demand
       IS_PROY   = (stock + transit) > 0 AND (stock + transit) >= demand
    5. Enrich with maestra + ABC-XYZ-FSN
    6. Aggregate to SKU level

    Parameters
    ----------
    filter_cd : bool
        If True, restrict to SKUs where the CD had InStock_CD_90 ≥ 1
        (aligns with the main dashboard's "Solo SKUs con IS CD = 1" toggle).

    Returns DataFrame at SKU level with columns:
        SKU_PRODUCTO, AREA, LINEA, SUBLINEA, MARCA, MIX_OFICIAL,
        CLASE_ABC, CLASE_XYZ, CLASE_FSN,
        N_TIENDAS, N_IS_ACTUAL, N_IS_PROY, IS_PCT_ACTUAL, IS_PCT_PROY,
        DELTA_PP, TRANSIT_QTY, TRANSIT_COMBOS
    Returns None if insufficient data.
    """
    try:
        df_store = cq.instock_store_detail(conn)
        df_transit = cq.unified_transit(conn)
    except Exception:
        return None

    if df_store is None or df_store.empty:
        return None
    if df_transit is None or df_transit.empty:
        return None

    # --- 1. Base from ft_in_stock (perfil='SI', latest date) ---
    base = df_store.copy()
    base.columns = [c.upper().strip() for c in base.columns]

    if "SKU_PRODUCTO" not in base.columns or "ID_SUCURSAL" not in base.columns:
        return None

    base["ID_SUCURSAL"] = base["ID_SUCURSAL"].astype(str).str.strip()
    base["STOCK_UND"] = pd.to_numeric(
        base.get("STOCK_UNIDADES", 0), errors="coerce"
    ).fillna(0)
    base["DEMAND_DAILY"] = pd.to_numeric(
        base.get("CANTIDAD_PROM_90", 0), errors="coerce"
    ).fillna(0)

    # --- 1b. Filter by CD InStock (align with main dashboard) ---
    if filter_cd:
        try:
            df_cd_data = cq.instock_daily_cd(conn)
            if df_cd_data is not None and not df_cd_data.empty:
                df_cd_data.columns = [c.upper().strip() for c in df_cd_data.columns]
                if "FECHA" in df_cd_data.columns:
                    df_cd_data["FECHA"] = pd.to_datetime(df_cd_data["FECHA"], errors="coerce")
                    cd_latest = df_cd_data[df_cd_data["FECHA"] == df_cd_data["FECHA"].max()]
                else:
                    cd_latest = df_cd_data
                if "INSTOCK_CD_90" in cd_latest.columns:
                    cd_latest["INSTOCK_CD_90"] = pd.to_numeric(
                        cd_latest["INSTOCK_CD_90"], errors="coerce"
                    ).fillna(0)
                    skus_cd_ok = set(
                        cd_latest[cd_latest["INSTOCK_CD_90"] >= 1]["SKU_PRODUCTO"]
                    )
                    base = base[base["SKU_PRODUCTO"].isin(skus_cd_ok)]
        except Exception:
            pass  # gracefully skip if CD data unavailable

    if base.empty:
        return None

    # --- 2. Transit per dest×SKU ---
    tr = df_transit.copy()
    tr.columns = [c.upper().strip() for c in tr.columns]
    if "ID_SUCURSAL_DESTINO" in tr.columns:
        tr = tr.rename(columns={"ID_SUCURSAL_DESTINO": "ID_SUCURSAL"})
    tr["ID_SUCURSAL"] = tr["ID_SUCURSAL"].astype(str).str.strip()
    tr["QTY_TRANSITO"] = pd.to_numeric(tr.get("QTY_TRANSITO", 0), errors="coerce").fillna(0)
    transit = tr.groupby(["SKU_PRODUCTO", "ID_SUCURSAL"], as_index=False)["QTY_TRANSITO"].sum()

    # --- 3. Join transit onto base ---
    merged = base.merge(transit, on=["SKU_PRODUCTO", "ID_SUCURSAL"], how="left")
    merged["QTY_TRANSITO"] = merged["QTY_TRANSITO"].fillna(0)

    # --- 4. InStock flags (identical to Snowflake ft_in_stock definition) ---
    # IS = perfil='SI' AND stock_unidades > 0 AND stock_unidades >= cantidad_prom_90
    # The base already only contains perfil='SI' rows (filtered in SQL).
    merged["PROJECTED_STOCK"] = merged["STOCK_UND"] + merged["QTY_TRANSITO"]
    merged["IS_ACTUAL"] = (
        (merged["STOCK_UND"] > 0)
        & (merged["STOCK_UND"] >= merged["DEMAND_DAILY"])
    ).astype(int)
    merged["IS_PROY"] = (
        (merged["PROJECTED_STOCK"] > 0)
        & (merged["PROJECTED_STOCK"] >= merged["DEMAND_DAILY"])
    ).astype(int)

    # --- 5. Enrich with maestra + ABC ---
    try:
        df_maestra = cq.maestra(conn)
        if not df_maestra.empty and "SKU_PRODUCTO" in df_maestra.columns:
            dim_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL"]
            dim_cols = [c for c in dim_cols if c in df_maestra.columns]
            merged = merged.merge(
                df_maestra[dim_cols].drop_duplicates("SKU_PRODUCTO"),
                on="SKU_PRODUCTO", how="left",
            )
    except Exception:
        pass

    try:
        df_abc = cq.abc_xyz_fsn(conn)
        if not df_abc.empty and "SKU_PRODUCTO" in df_abc.columns:
            abc_cols = ["SKU_PRODUCTO", "CLASE_ABC", "CLASE_XYZ", "CLASE_FSN"]
            abc_cols = [c for c in abc_cols if c in df_abc.columns]
            merged = merged.merge(
                df_abc[abc_cols].drop_duplicates("SKU_PRODUCTO"),
                on="SKU_PRODUCTO", how="left",
            )
    except Exception:
        pass

    # --- 6. Apply user filters if provided ---
    if apply_filters_fn is not None:
        merged = apply_filters_fn(merged)

    if merged.empty:
        return None

    # --- 7. Aggregate to SKU level ---
    agg = merged.groupby("SKU_PRODUCTO", as_index=False).agg(
        N_TIENDAS=("IS_ACTUAL", "count"),
        N_IS_ACTUAL=("IS_ACTUAL", "sum"),
        N_IS_PROY=("IS_PROY", "sum"),
        TRANSIT_QTY=("QTY_TRANSITO", "sum"),
        TRANSIT_COMBOS=("QTY_TRANSITO", lambda x: (x > 0).sum()),
    )
    agg["IS_PCT_ACTUAL"] = np.where(agg["N_TIENDAS"] > 0, agg["N_IS_ACTUAL"] / agg["N_TIENDAS"], 0.0)
    agg["IS_PCT_PROY"] = np.where(agg["N_TIENDAS"] > 0, agg["N_IS_PROY"] / agg["N_TIENDAS"], 0.0)
    agg["DELTA_PP"] = (agg["IS_PCT_PROY"] - agg["IS_PCT_ACTUAL"]) * 100  # percentage points

    # Enrich with dims (from merged, first occurrence)
    dim_agg_cols = ["AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL",
                    "CLASE_ABC", "CLASE_XYZ", "CLASE_FSN"]
    dim_agg_cols = [c for c in dim_agg_cols if c in merged.columns]
    if dim_agg_cols:
        dims = merged.groupby("SKU_PRODUCTO", as_index=False)[dim_agg_cols].first()
        agg = agg.merge(dims, on="SKU_PRODUCTO", how="left")

    # Attach store-level detail for downstream tabs (brechas, etc.)
    agg.attrs["_store_detail"] = merged

    return agg


def _render_projected_instock_tab(conn, apply_filters_fn, figures_export: dict,
                                  filter_cd: bool = False):
    """Render the InStock Proyectado section inside a tab.

    Shows projected InStock = current stock + unified transit (COO + pedidos hoy).
    """
    st.markdown("### InStock Proyectado — Stock + Transito en Ruta")
    st.caption(
        "Proyeccion que suma al stock actual las unidades en transito "
        "(COO + pedidos de reposicion de hoy). Muestra cuantas combinaciones "
        "SKU×Tienda pasarian a InStock=1 si llega el transito pendiente."
    )

    with lottie_spinner("snowflake"):
        df_proy = _compute_projected_instock(
            conn, apply_filters_fn=apply_filters_fn, filter_cd=filter_cd,
        )

    if df_proy is not None and not df_proy.empty:
        # --- KPIs ---
        total_combos = int(df_proy["N_TIENDAS"].sum())
        total_is_actual = int(df_proy["N_IS_ACTUAL"].sum())
        total_is_proy = int(df_proy["N_IS_PROY"].sum())
        is_pct_actual = total_is_actual / total_combos if total_combos > 0 else 0
        is_pct_proy = total_is_proy / total_combos if total_combos > 0 else 0
        delta_pp = (is_pct_proy - is_pct_actual) * 100
        transit_combos = int(df_proy["TRANSIT_COMBOS"].sum())
        transit_qty = int(df_proy["TRANSIT_QTY"].sum())

        pk1, pk2, pk3, pk4 = st.columns(4)
        with pk1:
            delta_str = f"(+{delta_pp:.1f}pp)" if delta_pp >= 0 else f"({delta_pp:.1f}pp)"
            delta_color = COLORS["status_on_track"] if delta_pp >= 0 else COLORS["status_critical"]
            st.html(_kpi_card_html(
                "IS% Actual → Proyectado",
                f"{is_pct_actual*100:.1f}% → {is_pct_proy*100:.1f}%",
                delta_str, delta_color,
            ))
        with pk2:
            gained = total_is_proy - total_is_actual
            st.html(_kpi_card_html("Combinaciones que ganan IS", f"+{gained:,}"))
        with pk3:
            st.html(_kpi_card_html("SKU×Tienda con Transito", f"{transit_combos:,}"))
        with pk4:
            st.html(_kpi_card_html("Unidades en Transito", f"{transit_qty:,}"))

        st.html("<br>")

        # --- Chart: Actual vs Proyectado by Area ---
        if "AREA" in df_proy.columns:
            area_agg = df_proy.groupby("AREA", as_index=False).agg(
                N_TIENDAS=("N_TIENDAS", "sum"),
                N_IS_ACTUAL=("N_IS_ACTUAL", "sum"),
                N_IS_PROY=("N_IS_PROY", "sum"),
            )
            area_agg["IS_ACTUAL"] = np.where(
                area_agg["N_TIENDAS"] > 0,
                area_agg["N_IS_ACTUAL"] / area_agg["N_TIENDAS"], 0.0,
            )
            area_agg["IS_PROY"] = np.where(
                area_agg["N_TIENDAS"] > 0,
                area_agg["N_IS_PROY"] / area_agg["N_TIENDAS"], 0.0,
            )
            area_agg = area_agg.sort_values("IS_ACTUAL", ascending=True)

            fig_proy = go.Figure(layout=dorel_layout(
                title=dict(text="InStock Actual vs Proyectado por Area", font_size=14, x=0.5),
                xaxis=dict(tickformat=".0%", title="InStock %", range=[0, 1.05]),
                yaxis=dict(title=""),
                height=max(300, len(area_agg) * 55 + 100),
                barmode="group",
                legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="center", x=0.5),
            ))
            fig_proy.add_trace(go.Bar(
                x=area_agg["IS_ACTUAL"], y=area_agg["AREA"],
                orientation="h", name="Actual",
                marker_color=COLORS["status_at_risk"],
                text=area_agg["IS_ACTUAL"].apply(lambda v: f"{v*100:.1f}%"),
                textposition="outside",
                hovertemplate="%{y}: <b>%{x:.1%}</b><extra>Actual</extra>",
            ))
            fig_proy.add_trace(go.Bar(
                x=area_agg["IS_PROY"], y=area_agg["AREA"],
                orientation="h", name="Proyectado",
                marker_color=COLORS["status_on_track"],
                text=area_agg["IS_PROY"].apply(lambda v: f"{v*100:.1f}%"),
                textposition="outside",
                hovertemplate="%{y}: <b>%{x:.1%}</b><extra>Proyectado</extra>",
            ))
            fig_proy.add_vline(
                x=0.93, line_dash="dash", line_color="#888", line_width=1,
                annotation_text="93%", annotation_position="top",
                annotation_font_size=10, annotation_font_color="#888",
            )
            st.plotly_chart(fig_proy, use_container_width=True)
            figures_export["InStock Proyectado por Area"] = fig_proy

        # --- Detail table (expander) ---
        with st.expander("Ver detalle SKU — Mayor impacto de transito", expanded=False):
            detail_proy = df_proy.copy()
            detail_proy = detail_proy[detail_proy["TRANSIT_QTY"] > 0]

            if not detail_proy.empty:
                detail_proy = detail_proy.nlargest(100, "DELTA_PP")

                display_cols = ["SKU_PRODUCTO"]
                for dc in ["AREA", "LINEA", "MARCA", "CLASE_ABC"]:
                    if dc in detail_proy.columns:
                        display_cols.append(dc)
                display_cols += [
                    "N_TIENDAS", "N_IS_ACTUAL", "N_IS_PROY",
                    "IS_PCT_ACTUAL", "IS_PCT_PROY", "DELTA_PP",
                    "TRANSIT_QTY", "TRANSIT_COMBOS",
                ]
                display_cols = [c for c in display_cols if c in detail_proy.columns]
                detail_display = detail_proy[display_cols].copy()

                for pct_c in ["IS_PCT_ACTUAL", "IS_PCT_PROY"]:
                    if pct_c in detail_display.columns:
                        detail_display[pct_c] = detail_display[pct_c].apply(
                            lambda v: f"{v*100:.1f}%"
                        )
                if "DELTA_PP" in detail_display.columns:
                    detail_display["DELTA_PP"] = detail_display["DELTA_PP"].apply(
                        lambda v: f"+{v:.1f}pp" if v >= 0 else f"{v:.1f}pp"
                    )

                rename = {
                    "N_TIENDAS": "Tiendas",
                    "N_IS_ACTUAL": "IS Actual",
                    "N_IS_PROY": "IS Proy.",
                    "IS_PCT_ACTUAL": "IS% Actual",
                    "IS_PCT_PROY": "IS% Proy.",
                    "DELTA_PP": "Delta (pp)",
                    "TRANSIT_QTY": "Und. Transito",
                    "TRANSIT_COMBOS": "Tiendas c/Trans.",
                }
                detail_display = detail_display.rename(
                    columns={k: v for k, v in rename.items() if k in detail_display.columns}
                )

                st.dataframe(
                    detail_display,
                    use_container_width=True,
                    height=min(500, 40 + len(detail_display) * 35),
                    hide_index=True,
                )
                download_buttons(detail_display, prefix="instock_proyectado_detalle")
            else:
                st.info("No hay SKUs con transito activo para los filtros seleccionados.")
    else:
        st.info(
            "Sin datos suficientes para calcular InStock Proyectado. "
            "Se requieren datos de stock tienda, ventas 90d y transito unificado."
        )



def _render_brechas_instock_tab(
    conn, apply_filters_fn, figures_export: dict, filter_cd: bool = False,
):
    """Tab: Brechas InStock — SKU×Tienda combinations that are NOT InStock.

    Shows which combinations don't reach InStock=1 even after projected transit,
    with detail of current stock, transit qty, demand, and deficit.
    """
    st.markdown("### Brechas InStock — Combinaciones sin cobertura")
    st.caption(
        "Detalle de combinaciones SKU×Tienda (perfil=SI) que **no alcanzan InStock** "
        "ni siquiera con el transito proyectado (COO + pedidos de hoy). "
        "Deficit = demanda diaria - (stock + transito)."
    )

    with lottie_spinner("stock"):
        df_proy = _compute_projected_instock(
            conn, apply_filters_fn=apply_filters_fn, filter_cd=filter_cd,
        )

    if df_proy is None or df_proy.empty:
        st.info("Sin datos suficientes para calcular brechas de InStock.")
        return

    # Recover the store-level detail attached by _compute_projected_instock
    merged = df_proy.attrs.get("_store_detail")
    if merged is None or merged.empty:
        st.info("No se pudo recuperar el detalle a nivel tienda.")
        return

    # Only rows where IS_PROY == 0 (NOT InStock even with transit)
    brechas = merged[merged["IS_PROY"] == 0].copy()

    if brechas.empty:
        st.success(
            "Todas las combinaciones SKU×Tienda alcanzan InStock con el transito "
            "proyectado. No hay brechas."
        )
        return

    # Compute deficit
    brechas["STOCK_PROYECTADO"] = brechas["STOCK_UND"] + brechas["QTY_TRANSITO"]
    brechas["DEFICIT"] = (brechas["DEMAND_DAILY"] - brechas["STOCK_PROYECTADO"]).clip(lower=0)
    brechas["TIENE_TRANSITO"] = (brechas["QTY_TRANSITO"] > 0).astype(int)

    # ── KPIs ──────────────────────────────────────────────────────────────
    total_brechas = len(brechas)
    total_combos = len(merged)
    skus_con_brecha = brechas["SKU_PRODUCTO"].nunique()
    tiendas_con_brecha = brechas["ID_SUCURSAL"].nunique()
    deficit_total = brechas["DEFICIT"].sum()
    sin_stock = (brechas["STOCK_UND"] == 0).sum()
    con_transito = brechas["TIENE_TRANSITO"].sum()

    pk1, pk2, pk3, pk4 = st.columns(4)
    with pk1:
        pct_brecha = total_brechas / total_combos * 100 if total_combos > 0 else 0
        st.html(_kpi_card_html(
            "Combinaciones sin IS",
            f"{total_brechas:,}",
            f"({pct_brecha:.1f}% del total)",
            COLORS["status_critical"],
        ))
    with pk2:
        st.html(_kpi_card_html(
            "SKUs afectados",
            f"{skus_con_brecha:,}",
            f"en {tiendas_con_brecha:,} tiendas",
        ))
    with pk3:
        st.html(_kpi_card_html(
            "Deficit Total (und)",
            f"{deficit_total:,.0f}",
            f"{sin_stock:,} sin stock",
            COLORS["status_at_risk"],
        ))
    with pk4:
        st.html(_kpi_card_html(
            "Con transito pero insuficiente",
            f"{con_transito:,}",
            f"de {total_brechas:,} brechas",
        ))

    st.html("<br>")

    # ── Chart: Brechas por Area ───────────────────────────────────────────
    if "AREA" in brechas.columns:
        area_b = brechas.groupby("AREA", as_index=False).agg(
            N_BRECHAS=("IS_PROY", "count"),
            DEFICIT_UND=("DEFICIT", "sum"),
            SIN_STOCK=("STOCK_UND", lambda x: (x == 0).sum()),
            CON_TRANSITO=("TIENE_TRANSITO", "sum"),
        ).sort_values("N_BRECHAS", ascending=True)

        fig_b = go.Figure(layout=dorel_layout(
            title=dict(text="Brechas InStock por Area", font_size=14, x=0.5),
            xaxis=dict(title="Combinaciones sin InStock"),
            yaxis=dict(title=""),
            height=max(280, len(area_b) * 55 + 100),
            barmode="stack",
            legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="center", x=0.5),
        ))
        fig_b.add_trace(go.Bar(
            x=area_b["SIN_STOCK"], y=area_b["AREA"],
            orientation="h", name="Sin Stock (0 und)",
            marker_color=COLORS["status_critical"],
            hovertemplate="%{y}: <b>%{x:,}</b> sin stock<extra></extra>",
        ))
        fig_b.add_trace(go.Bar(
            x=area_b["N_BRECHAS"] - area_b["SIN_STOCK"], y=area_b["AREA"],
            orientation="h", name="Stock insuficiente",
            marker_color=COLORS["status_at_risk"],
            hovertemplate="%{y}: <b>%{x:,}</b> stock insuficiente<extra></extra>",
        ))
        # Annotation with total per area
        for _, row in area_b.iterrows():
            fig_b.add_annotation(
                x=row["N_BRECHAS"], y=row["AREA"],
                text=f" {int(row['N_BRECHAS']):,}",
                showarrow=False, xanchor="left",
                font=dict(size=11, color="#333"),
            )
        st.plotly_chart(fig_b, use_container_width=True)
        figures_export["Brechas InStock por Area"] = fig_b

    # ── Detail table ──────────────────────────────────────────────────────
    st.markdown("#### Detalle SKU×Tienda")

    # Enrich with sucursal description
    try:
        df_tienda_dim = cq.tienda_dim(conn)
        if df_tienda_dim is not None and not df_tienda_dim.empty:
            df_tienda_dim.columns = [c.upper().strip() for c in df_tienda_dim.columns]
            if "COD_BODEGA" in df_tienda_dim.columns:
                td = df_tienda_dim.rename(columns={"COD_BODEGA": "ID_SUCURSAL"})
            elif "ID_SUCURSAL" in df_tienda_dim.columns:
                td = df_tienda_dim
            else:
                td = None
            if td is not None:
                td["ID_SUCURSAL"] = td["ID_SUCURSAL"].astype(str).str.strip()
                desc_col = next(
                    (c for c in ["DESCRIPCION_SUCURSAL", "NOM_TIENDA", "TIENDA"] if c in td.columns),
                    None,
                )
                if desc_col:
                    td_lookup = td[["ID_SUCURSAL", desc_col]].drop_duplicates("ID_SUCURSAL")
                    td_lookup = td_lookup.rename(columns={desc_col: "TIENDA"})
                    brechas = brechas.merge(td_lookup, on="ID_SUCURSAL", how="left")
    except Exception:
        pass

    # Build display table
    display_cols = ["SKU_PRODUCTO"]
    if "TIENDA" in brechas.columns:
        display_cols.append("TIENDA")
    else:
        display_cols.append("ID_SUCURSAL")
    for dc in ["AREA", "LINEA", "MARCA", "CLASE_ABC"]:
        if dc in brechas.columns:
            display_cols.append(dc)
    display_cols += ["STOCK_UND", "QTY_TRANSITO", "STOCK_PROYECTADO", "DEMAND_DAILY", "DEFICIT"]
    display_cols = [c for c in display_cols if c in brechas.columns]

    detail = brechas[display_cols].copy()
    detail = detail.sort_values("DEFICIT", ascending=False)

    # Limit for performance
    _MAX_ROWS = 2000
    total_rows = len(detail)
    if total_rows > _MAX_ROWS:
        detail = detail.head(_MAX_ROWS)
        st.caption(f"Mostrando las {_MAX_ROWS:,} brechas mas criticas de {total_rows:,} totales.")

    rename_map = {
        "SKU_PRODUCTO": "SKU",
        "TIENDA": "Tienda",
        "ID_SUCURSAL": "Cod Tienda",
        "STOCK_UND": "Stock Actual",
        "QTY_TRANSITO": "En Transito",
        "STOCK_PROYECTADO": "Stock Proyectado",
        "DEMAND_DAILY": "Demanda Diaria",
        "DEFICIT": "Deficit (und)",
    }
    detail = detail.rename(columns={k: v for k, v in rename_map.items() if k in detail.columns})

    col_config = {}
    for c in ["Stock Actual", "En Transito", "Stock Proyectado", "Demanda Diaria", "Deficit (und)"]:
        if c in detail.columns:
            col_config[c] = st.column_config.NumberColumn(c, format="%,.0f")

    st.dataframe(
        detail,
        use_container_width=True,
        height=min(600, 40 + len(detail) * 35),
        hide_index=True,
        column_config=col_config,
    )
    download_buttons(detail, prefix="brechas_instock")


def _render_venta_perdida_tab(
    conn,
    sel_window: str,
    sel_window_label: str,
    filter_cd_toggle: bool,
    solo_perfil_toggle: bool,
    dim_filter_fn,
    figures_export: dict,
):
    """Render Venta Perdida (Lost Sales) analysis tab.

    VP per SKU × date = OOS_stores × avg_daily_velocity_per_store × price.
    Cause split: Reposición (CD had stock) vs Abastecimiento (CD also OOS).
    Loads own InStock weekly history (since 2023) for long-range analysis.
    Features: period selector, monthly aggregation, YTD, YoY comparison.
    """
    st.markdown("### 💰 Venta Perdida — Estimación Diaria")
    st.caption(
        "Venta perdida estimada por falta de stock en tiendas. "
        "Se calcula como: tiendas OOS × velocidad promedio diaria "
        "por tienda × precio promedio 90d. Causa: **Reposición** si el CD tenía stock, "
        "**Abastecimiento** si el CD también estaba sin stock."
    )

    # ── VP-specific controls ──────────────────────────────────────────
    vp_c1, vp_c2 = st.columns(2)
    with vp_c1:
        vp_period_opts = {
            "Último Mes": 30,
            "Últimos 3 Meses": 90,
            "Últimos 6 Meses": 180,
            "YTD": -1,
            "Último Año": 365,
            "Todo (desde 2023)": 9999,
        }
        vp_period_label = st.selectbox(
            "Período VP",
            list(vp_period_opts.keys()),
            index=2,
            key="vp_period_sel",
        )
        vp_period_days = vp_period_opts[vp_period_label]
    with vp_c2:
        vp_agg_mode = st.radio(
            "Agregación",
            ["Semanal", "Mensual"],
            index=1,
            horizontal=True,
            key="vp_agg_mode",
        )

    # ── Load data (always weekly InStock for full history) ────────────
    with lottie_spinner("snowflake"):
        try:
            df_is = cq.instock_hist_tienda(conn).copy()
            df_vel_raw = cq.ventas_90d_sucursal(conn)
            df_precio_raw = cq.precio_prom_sku(conn)
        except Exception:
            st.error("Error al cargar datos para Venta Perdida.")
            return

    if df_is is None or df_is.empty:
        st.warning("Sin datos de InStock para calcular venta perdida.")
        return
    if df_vel_raw is None or df_vel_raw.empty:
        st.warning("Sin datos de ventas 90d para calcular velocidad.")
        return

    # ── Coerce numerics ───────────────────────────────────────────────
    _num_cols = [
        "N_TIENDAS",
        "N_TIENDAS_IS90", "N_TIENDAS_IS180", "N_TIENDAS_IS365",
        "TIENDAS_IS90", "TIENDAS_IS180", "TIENDAS_IS365",
        "N_TIENDAS_IS90_PERFIL", "N_TIENDAS_IS180_PERFIL", "N_TIENDAS_IS365_PERFIL",
        "TIENDAS_IS90_PERFIL", "TIENDAS_IS180_PERFIL", "TIENDAS_IS365_PERFIL",
        "INSTOCK_CD_90", "INSTOCK_CD_180", "INSTOCK_CD_365",
    ]
    for c in _num_cols:
        if c in df_is.columns:
            df_is[c] = pd.to_numeric(df_is[c], errors="coerce").fillna(0)
    if "FECHA" in df_is.columns:
        df_is["FECHA"] = pd.to_datetime(df_is["FECHA"], errors="coerce")

    # ── Enrich with ABC ───────────────────────────────────────────────
    try:
        _abc = cq.abc_xyz_fsn(conn)
        if _abc is not None and not _abc.empty and "SKU_PRODUCTO" in _abc.columns:
            _abc_cols = [c for c in ["SKU_PRODUCTO", "CLASE_ABC", "CLASE_XYZ", "CLASE_FSN"]
                         if c in _abc.columns]
            _abc_dd = _abc[_abc_cols].drop_duplicates("SKU_PRODUCTO")
            for _c in _abc_cols[1:]:
                if _c in df_is.columns:
                    df_is = df_is.drop(columns=[_c])
            df_is = df_is.merge(_abc_dd, on="SKU_PRODUCTO", how="left")
    except Exception:
        pass

    # ── Apply dimension filters ───────────────────────────────────────
    df_is = dim_filter_fn(df_is)

    # ── Apply VP period filter ────────────────────────────────────────
    if "FECHA" in df_is.columns:
        if vp_period_days == -1:  # YTD
            ytd_start = pd.Timestamp(pd.Timestamp.now().year, 1, 1)
            df_is = df_is[df_is["FECHA"] >= ytd_start]
        elif vp_period_days < 9999:
            cutoff = pd.Timestamp.now() - pd.Timedelta(days=vp_period_days)
            df_is = df_is[df_is["FECHA"] >= cutoff]

    if df_is.empty:
        st.warning("Sin datos para el período seleccionado.")
        return

    # ── Apply CD filter ───────────────────────────────────────────────
    cd_col = _CD_FILTER_COL
    if filter_cd_toggle and cd_col in df_is.columns:
        df_is = df_is[df_is[cd_col] >= 1]

    if df_is.empty:
        st.warning("Sin datos después de aplicar filtro CD.")
        return

    # ── Prepare velocity (TIENDA only) ────────────────────────────────
    vel = df_vel_raw.copy()
    vel.columns = [c.upper().strip() for c in vel.columns]
    if "CANAL_DE_DISTRIBUCION" in vel.columns:
        vel = vel[vel["CANAL_DE_DISTRIBUCION"].astype(str).str.upper() == "TIENDA"]
    if vel.empty:
        st.warning("Sin datos de velocidad para tiendas.")
        return

    vel["UNIDADES_90D"] = pd.to_numeric(vel.get("UNIDADES_90D", 0), errors="coerce").fillna(0)
    vel_sku = vel.groupby("SKU_PRODUCTO", as_index=False).agg(
        VEL_TOTAL_90D=("UNIDADES_90D", "sum"),
    )
    vel_sku["VEL_DIARIA_TOTAL"] = vel_sku["VEL_TOTAL_90D"] / 90

    # ── Prepare pricing ───────────────────────────────────────────────
    precio = df_precio_raw.copy() if df_precio_raw is not None else pd.DataFrame()
    if not precio.empty:
        precio.columns = [c.upper().strip() for c in precio.columns]
        precio["PRECIO_PROM_90D"] = pd.to_numeric(
            precio.get("PRECIO_PROM_90D", 0), errors="coerce"
        ).fillna(0)
        precio_map = precio.set_index("SKU_PRODUCTO")["PRECIO_PROM_90D"].to_dict()
    else:
        precio_map = {}

    # ── Compute VP per SKU × date ─────────────────────────────────────
    cols = _COL_MAP_TIENDA[sel_window]
    if (solo_perfil_toggle
            and cols["is_perfil"] in df_is.columns
            and cols["n_perfil"] in df_is.columns):
        is_col = cols["is_perfil"]
        n_col = cols["n_perfil"]
    elif cols["n"] in df_is.columns:
        is_col = cols["is"]
        n_col = cols["n"]
    else:
        is_col = cols["is"]
        n_col = "N_TIENDAS"

    work = df_is.copy()
    for c in [is_col, n_col, cd_col]:
        if c in work.columns:
            work[c] = pd.to_numeric(work[c], errors="coerce").fillna(0)

    work["OOS_TIENDAS"] = (work[n_col] - work[is_col]).clip(lower=0)

    work = work.merge(
        vel_sku[["SKU_PRODUCTO", "VEL_DIARIA_TOTAL"]],
        on="SKU_PRODUCTO", how="left",
    )
    work["VEL_DIARIA_TOTAL"] = work["VEL_DIARIA_TOTAL"].fillna(0)

    work["VEL_PER_STORE"] = np.where(
        work[n_col] > 0, work["VEL_DIARIA_TOTAL"] / work[n_col], 0.0,
    )
    work["VP_UND"] = work["OOS_TIENDAS"] * work["VEL_PER_STORE"]

    work["PRECIO"] = work["SKU_PRODUCTO"].map(precio_map).fillna(0)
    work["VP_CLP"] = work["VP_UND"] * work["PRECIO"]

    # Cause split
    if cd_col in work.columns:
        work["CAUSA"] = np.where(work[cd_col] >= 1, "Reposición", "Abastecimiento")
    else:
        work["CAUSA"] = "Sin Info CD"

    work["VP_REPO_CLP"] = np.where(work["CAUSA"] == "Reposición", work["VP_CLP"], 0)
    work["VP_ABAST_CLP"] = np.where(work["CAUSA"] == "Abastecimiento", work["VP_CLP"], 0)
    work["VP_REPO_UND"] = np.where(work["CAUSA"] == "Reposición", work["VP_UND"], 0)
    work["VP_ABAST_UND"] = np.where(work["CAUSA"] == "Abastecimiento", work["VP_UND"], 0)

    # ── Time dimensions ───────────────────────────────────────────────
    if "FECHA" in work.columns:
        work["YEAR"] = work["FECHA"].dt.year
        work["MONTH"] = work["FECHA"].dt.month
        work["YEAR_MONTH"] = work["FECHA"].dt.to_period("M")

    # ── Helper ────────────────────────────────────────────────────────
    def _fmt_clp(v):
        return f"${v:,.0f}".replace(",", ".")

    # ── Monthly aggregation ───────────────────────────────────────────
    is_monthly = vp_agg_mode == "Mensual" and "FECHA" in work.columns

    if is_monthly:
        ts_monthly = work.groupby("YEAR_MONTH", as_index=False).agg(
            VP_CLP_MEAN=("VP_CLP", "mean"),
            VP_UND_MEAN=("VP_UND", "mean"),
            VP_REPO_MEAN=("VP_REPO_CLP", "mean"),
            VP_ABAST_MEAN=("VP_ABAST_CLP", "mean"),
            N_SNAPSHOTS=("FECHA", "nunique"),
        )
        ts_monthly["VP_CLP"] = ts_monthly["VP_CLP_MEAN"] * 30
        ts_monthly["VP_UND"] = ts_monthly["VP_UND_MEAN"] * 30
        ts_monthly["VP_REPO"] = ts_monthly["VP_REPO_MEAN"] * 30
        ts_monthly["VP_ABAST"] = ts_monthly["VP_ABAST_MEAN"] * 30
        ts_monthly["LABEL"] = ts_monthly["YEAR_MONTH"].astype(str)
        ts_monthly["YEAR"] = ts_monthly["YEAR_MONTH"].apply(lambda p: p.year)
        ts_monthly["MONTH"] = ts_monthly["YEAR_MONTH"].apply(lambda p: p.month)
        ts_display = ts_monthly.sort_values("YEAR_MONTH")
    else:
        ts_weekly = work.groupby("FECHA", as_index=False).agg(
            VP_CLP=("VP_CLP", "sum"),
            VP_UND=("VP_UND", "sum"),
            VP_REPO=("VP_REPO_CLP", "sum"),
            VP_ABAST=("VP_ABAST_CLP", "sum"),
            OOS_TOTAL=("OOS_TIENDAS", "sum"),
        ).sort_values("FECHA")
        ts_display = ts_weekly

    # ── KPIs (latest snapshot) ────────────────────────────────────────
    if "FECHA" in work.columns:
        latest_date = work["FECHA"].max()
        latest = work[work["FECHA"] == latest_date]
        latest_str = latest_date.strftime("%d/%m/%Y") if hasattr(latest_date, "strftime") else ""
    else:
        latest = work
        latest_str = ""

    vp_diaria_clp = latest["VP_CLP"].sum()
    vp_mensual_est = vp_diaria_clp * 30
    vp_repo_clp = latest["VP_REPO_CLP"].sum()
    vp_abast_clp = latest["VP_ABAST_CLP"].sum()
    vp_diaria_und = latest["VP_UND"].sum()
    n_skus_vp = int((latest["VP_UND"] > 0).sum())
    n_combos_oos = int((latest["OOS_TIENDAS"] > 0).sum())
    pct_repo = vp_repo_clp / vp_diaria_clp * 100 if vp_diaria_clp > 0 else 0

    # Period total (for context)
    if is_monthly and not ts_display.empty:
        vp_period_total = ts_display["VP_CLP"].sum()
    elif not is_monthly and "VP_CLP" in ts_display.columns:
        vp_period_total = ts_display["VP_CLP"].sum() * 7  # weekly snapshot × 7 days
    else:
        vp_period_total = 0

    # Row 1
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.html(_kpi_card_html("VP Diaria ($)", _fmt_clp(vp_diaria_clp)))
    with k2:
        st.html(_kpi_card_html("VP Mensual Est. ($)", _fmt_clp(vp_mensual_est)))
    with k3:
        st.html(_kpi_card_html(
            "VP Reposición", _fmt_clp(vp_repo_clp),
            f"({pct_repo:.0f}%)", COLORS["status_at_risk"],
        ))
    with k4:
        pct_abast = 100 - pct_repo if vp_diaria_clp > 0 else 0
        st.html(_kpi_card_html(
            "VP Abastecimiento", _fmt_clp(vp_abast_clp),
            f"({pct_abast:.0f}%)" if vp_diaria_clp > 0 else "",
            COLORS["status_critical"],
        ))

    # Row 2
    k5, k6, k7, k8 = st.columns(4)
    with k5:
        st.html(_kpi_card_html("SKUs con VP", f"{n_skus_vp:,}"))
    with k6:
        st.html(_kpi_card_html("VP Diaria (und)", f"{vp_diaria_und:,.0f}".replace(",", ".")))
    with k7:
        st.html(_kpi_card_html("Combinaciones OOS", f"{n_combos_oos:,}"))
    with k8:
        total_rev_daily = (
            latest["PRECIO"].clip(lower=0)
            .mul(latest["VEL_PER_STORE"])
            .mul(latest[n_col])
            .sum()
        )
        vp_pct_rev = vp_diaria_clp / total_rev_daily * 100 if total_rev_daily > 0 else 0
        st.html(_kpi_card_html(
            "VP % s/Venta", f"{vp_pct_rev:.1f}%",
            f"(fecha: {latest_str})" if latest_str else "",
        ))

    st.html("<br>")

    # ── Chart 1: Time series ──────────────────────────────────────────
    if is_monthly and not ts_display.empty:
        fig_ts = go.Figure(layout=dorel_layout(
            title=dict(text=f"Venta Perdida Mensual — {vp_period_label}", font_size=14, x=0.5),
            yaxis=dict(title="VP Mensual ($)", gridcolor="#ECECEC"),
            xaxis=dict(title="", gridcolor="#ECECEC"),
            height=420, barmode="stack",
            legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="center", x=0.5),
        ))
        fig_ts.add_trace(go.Bar(
            x=ts_display["LABEL"], y=ts_display["VP_REPO"],
            name="Reposición", marker_color=COLORS["status_at_risk"],
            hovertemplate="%{x}: <b>$%{y:,.0f}</b><extra>Reposición</extra>",
        ))
        fig_ts.add_trace(go.Bar(
            x=ts_display["LABEL"], y=ts_display["VP_ABAST"],
            name="Abastecimiento", marker_color=COLORS["status_critical"],
            hovertemplate="%{x}: <b>$%{y:,.0f}</b><extra>Abastecimiento</extra>",
        ))
        fig_ts.add_trace(go.Scatter(
            x=ts_display["LABEL"], y=ts_display["VP_CLP"],
            mode="lines+markers", name="VP Total",
            line=dict(color=COLORS["primary"], width=2, dash="dot"),
            marker=dict(size=5),
            hovertemplate="%{x}: <b>$%{y:,.0f}</b><extra>Total</extra>",
        ))
        st.plotly_chart(fig_ts, use_container_width=True)
        figures_export["Venta Perdida Mensual"] = fig_ts

    elif not is_monthly and "FECHA" in ts_display.columns and len(ts_display) > 1:
        fig_ts = go.Figure(layout=dorel_layout(
            title=dict(text=f"Venta Perdida Semanal — {vp_period_label}", font_size=14, x=0.5),
            yaxis=dict(title="VP Diaria ($)", gridcolor="#ECECEC"),
            xaxis=dict(title="", gridcolor="#ECECEC"),
            height=420, barmode="stack",
            legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="center", x=0.5),
        ))
        fig_ts.add_trace(go.Bar(
            x=ts_display["FECHA"], y=ts_display["VP_REPO"],
            name="Reposición", marker_color=COLORS["status_at_risk"],
            hovertemplate="%{x|%d/%m/%Y}: <b>$%{y:,.0f}</b><extra>Reposición</extra>",
        ))
        fig_ts.add_trace(go.Bar(
            x=ts_display["FECHA"], y=ts_display["VP_ABAST"],
            name="Abastecimiento", marker_color=COLORS["status_critical"],
            hovertemplate="%{x|%d/%m/%Y}: <b>$%{y:,.0f}</b><extra>Abastecimiento</extra>",
        ))
        fig_ts.add_trace(go.Scatter(
            x=ts_display["FECHA"], y=ts_display["VP_CLP"],
            mode="lines+markers", name="VP Total",
            line=dict(color=COLORS["primary"], width=2, dash="dot"),
            marker=dict(size=4),
            hovertemplate="%{x|%d/%m/%Y}: <b>$%{y:,.0f}</b><extra>Total</extra>",
        ))
        st.plotly_chart(fig_ts, use_container_width=True)
        figures_export["Venta Perdida Semanal"] = fig_ts

    # ── Chart 2: VP by Area ───────────────────────────────────────────
    vp_col1, vp_col2 = st.columns(2)

    with vp_col1:
        if "AREA" in latest.columns:
            area_vp = latest.groupby("AREA", as_index=False).agg(
                VP_REPO=("VP_REPO_CLP", "sum"),
                VP_ABAST=("VP_ABAST_CLP", "sum"),
                VP_TOTAL=("VP_CLP", "sum"),
            )
            area_vp = area_vp.sort_values("VP_TOTAL", ascending=True)
            fig_area = go.Figure(layout=dorel_layout(
                title=dict(text="VP por Area ($)", font_size=14, x=0.5),
                xaxis=dict(title="VP ($)"), yaxis=dict(title=""),
                height=max(300, len(area_vp) * 50 + 100),
                barmode="stack",
                legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="center", x=0.5),
            ))
            fig_area.add_trace(go.Bar(
                x=area_vp["VP_REPO"], y=area_vp["AREA"],
                orientation="h", name="Reposición",
                marker_color=COLORS["status_at_risk"],
                hovertemplate="%{y}: <b>$%{x:,.0f}</b><extra>Repo</extra>",
            ))
            fig_area.add_trace(go.Bar(
                x=area_vp["VP_ABAST"], y=area_vp["AREA"],
                orientation="h", name="Abastecimiento",
                marker_color=COLORS["status_critical"],
                hovertemplate="%{y}: <b>$%{x:,.0f}</b><extra>Abast</extra>",
            ))
            st.plotly_chart(fig_area, use_container_width=True)
            figures_export["VP por Area"] = fig_area

    # ── Chart 3: VP by Linea (top 15) ─────────────────────────────────
    with vp_col2:
        if "LINEA" in latest.columns:
            linea_vp = latest.groupby("LINEA", as_index=False)["VP_CLP"].sum()
            linea_vp = linea_vp.nlargest(15, "VP_CLP").sort_values("VP_CLP", ascending=True)
            fig_linea = go.Figure(layout=dorel_layout(
                title=dict(text="Top 15 Líneas por VP ($)", font_size=14, x=0.5),
                xaxis=dict(title="VP ($)"), yaxis=dict(title=""),
                height=max(350, len(linea_vp) * 40 + 100),
            ))
            fig_linea.add_trace(go.Bar(
                x=linea_vp["VP_CLP"], y=linea_vp["LINEA"],
                orientation="h", marker_color=COLORS["primary"],
                text=linea_vp["VP_CLP"].apply(lambda v: _fmt_clp(v)),
                textposition="outside",
                hovertemplate="%{y}: <b>$%{x:,.0f}</b><extra></extra>",
            ))
            st.plotly_chart(fig_linea, use_container_width=True)
            figures_export["VP por Linea"] = fig_linea

    # ── Donut + explanation ───────────────────────────────────────────
    donut_c1, donut_c2 = st.columns([1, 1])
    with donut_c1:
        cause_data = latest.groupby("CAUSA", as_index=False)["VP_CLP"].sum()
        if not cause_data.empty and cause_data["VP_CLP"].sum() > 0:
            color_map = {
                "Reposición": COLORS["status_at_risk"],
                "Abastecimiento": COLORS["status_critical"],
                "Sin Info CD": "#888",
            }
            fig_donut = go.Figure(layout=dorel_layout(
                title=dict(text="Composición VP por Causa", font_size=14, x=0.5),
                height=350,
            ))
            fig_donut.add_trace(go.Pie(
                labels=cause_data["CAUSA"], values=cause_data["VP_CLP"],
                hole=0.5,
                marker=dict(colors=[color_map.get(c, "#888") for c in cause_data["CAUSA"]]),
                textinfo="label+percent",
                hovertemplate="%{label}: <b>$%{value:,.0f}</b> (%{percent})<extra></extra>",
            ))
            st.plotly_chart(fig_donut, use_container_width=True)
            figures_export["VP Composicion Causa"] = fig_donut

    with donut_c2:
        st.markdown("""
        **Interpretación de causas:**

        - 🟠 **Reposición**: El CD tenía stock pero la tienda no.
          Problema de *logistics/transferencia*. Acción: mejorar
          reglas de reposición automática, perfiles de tienda, o
          frecuencia de envío.

        - 🔴 **Abastecimiento**: Ni el CD ni la tienda tenían stock.
          Problema de *purchasing/supply chain*. Acción: revisar
          forecast, lead times, safety stock del CD, o plan de compras.

        **Metodología VP:**
        - `VP (und) = Tiendas OOS × Velocidad Prom. Diaria por Tienda`
        - `VP ($) = VP (und) × Precio Prom. Neto 90d`
        - VP Mensual = Promedio VP diaria × 30 días
        """)

    # ── YoY comparison ────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### VP Año Actual vs Año Anterior")

    if "YEAR" in work.columns and "MONTH" in work.columns:
        current_year = pd.Timestamp.now().year
        prior_year = current_year - 1

        monthly_yoy = work.groupby(["YEAR", "MONTH"], as_index=False).agg(
            VP_CLP_MEAN=("VP_CLP", "mean"),
            VP_UND_MEAN=("VP_UND", "mean"),
            N_SNAPSHOTS=("FECHA", "nunique"),
        )
        monthly_yoy["VP_MENSUAL"] = monthly_yoy["VP_CLP_MEAN"] * 30
        monthly_yoy["VP_UND_MENSUAL"] = monthly_yoy["VP_UND_MEAN"] * 30

        cy = monthly_yoy[monthly_yoy["YEAR"] == current_year].copy()
        py = monthly_yoy[monthly_yoy["YEAR"] == prior_year].copy()

        if not cy.empty or not py.empty:
            comparison = cy[["MONTH", "VP_MENSUAL"]].rename(
                columns={"VP_MENSUAL": "VP_CY"},
            ).merge(
                py[["MONTH", "VP_MENSUAL"]].rename(
                    columns={"VP_MENSUAL": "VP_PY"},
                ),
                on="MONTH", how="outer",
            ).fillna(0).sort_values("MONTH")

            comparison["DELTA"] = comparison["VP_CY"] - comparison["VP_PY"]
            comparison["PCT_CHG"] = np.where(
                comparison["VP_PY"] > 0,
                comparison["DELTA"] / comparison["VP_PY"] * 100, 0,
            )

            month_names = {
                1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
                7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
            }
            comparison["MES"] = comparison["MONTH"].map(month_names)

            # Chart
            fig_yoy = go.Figure(layout=dorel_layout(
                title=dict(
                    text=f"VP Mensual: {current_year} vs {prior_year}",
                    font_size=14, x=0.5,
                ),
                yaxis=dict(title="VP Mensual ($)", gridcolor="#ECECEC"),
                xaxis=dict(title=""),
                height=400, barmode="group",
                legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="center", x=0.5),
            ))
            fig_yoy.add_trace(go.Bar(
                x=comparison["MES"], y=comparison["VP_CY"],
                name=str(current_year), marker_color=COLORS["primary"],
                hovertemplate="%{x}: <b>$%{y:,.0f}</b><extra>" + str(current_year) + "</extra>",
            ))
            fig_yoy.add_trace(go.Bar(
                x=comparison["MES"], y=comparison["VP_PY"],
                name=str(prior_year), marker_color=COLORS["secondary"],
                hovertemplate="%{x}: <b>$%{y:,.0f}</b><extra>" + str(prior_year) + "</extra>",
            ))
            st.plotly_chart(fig_yoy, use_container_width=True)
            figures_export["VP YoY"] = fig_yoy

            # Table
            with st.expander("📊 Tabla YoY Mensual", expanded=False):
                tbl = comparison[["MES", "VP_CY", "VP_PY", "DELTA", "PCT_CHG"]].copy()
                totals = {
                    "MES": "TOTAL",
                    "VP_CY": tbl["VP_CY"].sum(),
                    "VP_PY": tbl["VP_PY"].sum(),
                    "DELTA": tbl["DELTA"].sum(),
                    "PCT_CHG": 0,
                }
                totals["PCT_CHG"] = (
                    totals["DELTA"] / totals["VP_PY"] * 100 if totals["VP_PY"] > 0 else 0
                )
                tbl = pd.concat([tbl, pd.DataFrame([totals])], ignore_index=True)

                for mc in ["VP_CY", "VP_PY", "DELTA"]:
                    tbl[mc] = tbl[mc].apply(lambda v: _fmt_clp(v))
                tbl["PCT_CHG"] = tbl["PCT_CHG"].apply(
                    lambda v: f"+{v:.1f}%" if v >= 0 else f"{v:.1f}%"
                )
                tbl = tbl.rename(columns={
                    "MES": "Mes",
                    "VP_CY": f"VP {current_year}",
                    "VP_PY": f"VP {prior_year}",
                    "DELTA": "Delta ($)",
                    "PCT_CHG": "% Cambio",
                })
                st.dataframe(tbl, use_container_width=True, hide_index=True)
        else:
            st.info("Sin datos suficientes para comparación interanual.")
    else:
        st.info("Sin datos temporales para comparación interanual.")

    # ── VP by Marca (expander) ────────────────────────────────────────
    if "MARCA" in latest.columns:
        with st.expander("📊 VP por Marca — Top 15", expanded=False):
            marca_vp = latest.groupby("MARCA", as_index=False).agg(
                VP_TOTAL=("VP_CLP", "sum"),
                VP_REPO=("VP_REPO_CLP", "sum"),
                VP_UND=("VP_UND", "sum"),
                OOS=("OOS_TIENDAS", "sum"),
                N_SKUS=("SKU_PRODUCTO", "nunique"),
            )
            marca_vp = marca_vp.nlargest(15, "VP_TOTAL")
            marca_vp["PCT_REPO"] = np.where(
                marca_vp["VP_TOTAL"] > 0,
                marca_vp["VP_REPO"] / marca_vp["VP_TOTAL"] * 100, 0,
            )
            display_m = marca_vp.copy()
            for mc in ["VP_TOTAL", "VP_REPO"]:
                display_m[mc] = display_m[mc].apply(_fmt_clp)
            display_m["VP_UND"] = display_m["VP_UND"].apply(
                lambda v: f"{v:,.0f}".replace(",", ".")
            )
            display_m["PCT_REPO"] = display_m["PCT_REPO"].apply(lambda v: f"{v:.0f}%")
            display_m = display_m.rename(columns={
                "VP_TOTAL": "VP Diaria ($)", "VP_REPO": "VP Repo ($)",
                "VP_UND": "VP (und)", "PCT_REPO": "% Reposición",
                "OOS": "Combos OOS", "N_SKUS": "SKUs",
            })
            st.dataframe(
                display_m, use_container_width=True, hide_index=True,
                height=min(450, 40 + len(display_m) * 35),
            )

    # ── Detail table ──────────────────────────────────────────────────
    st.markdown("---")
    with st.expander("📋 Detalle VP por SKU — Última Fecha (Top 100)", expanded=False):
        if not latest.empty:
            sku_vp = latest.groupby("SKU_PRODUCTO", as_index=False).agg(
                VP_CLP=("VP_CLP", "sum"),
                VP_UND=("VP_UND", "sum"),
                VP_REPO_CLP=("VP_REPO_CLP", "sum"),
                OOS_TIENDAS=("OOS_TIENDAS", "sum"),
                N_TIENDAS_VAL=(n_col, "first"),
            )
            for dc in ["AREA", "LINEA", "MARCA", "CLASE_ABC"]:
                if dc in latest.columns:
                    sku_vp[dc] = sku_vp["SKU_PRODUCTO"].map(
                        latest.groupby("SKU_PRODUCTO")[dc].first()
                    )
            sku_vp = sku_vp[sku_vp["VP_CLP"] > 0].nlargest(100, "VP_CLP")

            sku_vp["VP_MENSUAL"] = sku_vp["VP_CLP"] * 30
            sku_vp["PCT_REPO"] = np.where(
                sku_vp["VP_CLP"] > 0,
                sku_vp["VP_REPO_CLP"] / sku_vp["VP_CLP"] * 100, 0,
            )
            sku_vp["PCT_OOS"] = np.where(
                sku_vp["N_TIENDAS_VAL"] > 0,
                sku_vp["OOS_TIENDAS"] / sku_vp["N_TIENDAS_VAL"] * 100, 0,
            )

            display_cols = ["SKU_PRODUCTO"]
            for dc in ["AREA", "LINEA", "MARCA", "CLASE_ABC"]:
                if dc in sku_vp.columns:
                    display_cols.append(dc)
            display_cols += [
                "VP_CLP", "VP_MENSUAL", "VP_UND",
                "PCT_REPO", "OOS_TIENDAS", "N_TIENDAS_VAL", "PCT_OOS",
            ]
            display_cols = [c for c in display_cols if c in sku_vp.columns]
            sku_d = sku_vp[display_cols].copy()

            for mc in ["VP_CLP", "VP_MENSUAL"]:
                if mc in sku_d.columns:
                    sku_d[mc] = sku_d[mc].apply(_fmt_clp)
            for pc in ["PCT_REPO", "PCT_OOS"]:
                if pc in sku_d.columns:
                    sku_d[pc] = sku_d[pc].apply(lambda v: f"{v:.0f}%")
            if "VP_UND" in sku_d.columns:
                sku_d["VP_UND"] = sku_d["VP_UND"].apply(
                    lambda v: f"{v:,.1f}".replace(",", ".")
                )
            for ic in ["OOS_TIENDAS", "N_TIENDAS_VAL"]:
                if ic in sku_d.columns:
                    sku_d[ic] = sku_d[ic].apply(
                        lambda v: f"{int(v):,}".replace(",", ".")
                    )
            sku_d = sku_d.rename(columns={
                "VP_CLP": "VP Diaria ($)", "VP_MENSUAL": "VP Mensual Est.",
                "VP_UND": "VP Diaria (und)", "PCT_REPO": "% Reposición",
                "OOS_TIENDAS": "Tiendas OOS", "N_TIENDAS_VAL": "Tiendas Total",
                "PCT_OOS": "% Tiendas OOS",
            })
            st.dataframe(
                sku_d, use_container_width=True, hide_index=True,
                height=min(500, 40 + len(sku_d) * 35),
            )
            download_buttons(sku_d, prefix="venta_perdida_detalle")
        else:
            st.info("Sin datos de venta perdida para los filtros seleccionados.")


def render_instock_historico(conn):
    """Render the Historical InStock dashboard."""

    st.html(
        "<h2 class='sub-header'>InStock Historico — Tiendas & CD</h2>"
    )

    st.caption(
        "Evolucion del InStock **calculado**: stock >= venta promedio diaria "
        "(IS=1 si la tienda/CD puede cubrir al menos 1 dia de demanda). "
        "Datos diarios (ultimos 30 dias) + semanales (desde 2023, muestreado cada lunes)."
    )

    # ── Load data — merge daily (30d) + weekly (2023+) ─────────────────
    with lottie_spinner("snowflake"):
        _df_daily_t = cq.instock_daily_tienda(conn).copy()
        _df_daily_cd = cq.instock_daily_cd(conn).copy()
        _df_hist_t = cq.instock_hist_tienda(conn).copy()
        _df_hist_cd = cq.instock_hist_cd(conn).copy()

    # Combine: weekly history + daily recent (daily takes precedence for overlapping dates)
    def _merge_daily_hist(df_daily, df_hist):
        if df_daily.empty:
            return df_hist
        if df_hist.empty:
            return df_daily
        daily_dates = set(df_daily["FECHA"].dropna().unique()) if "FECHA" in df_daily.columns else set()
        hist_no_overlap = df_hist[~df_hist["FECHA"].isin(daily_dates)] if "FECHA" in df_hist.columns else df_hist
        return pd.concat([hist_no_overlap, df_daily], ignore_index=True)

    df_tienda_raw = _merge_daily_hist(_df_daily_t, _df_hist_t)
    df_cd_raw = _merge_daily_hist(_df_daily_cd, _df_hist_cd)

    if df_tienda_raw.empty and df_cd_raw.empty:
        st.warning("No se encontraron datos de InStock. Verifique la conexion a Snowflake.")
        return

    # Hint if new columns are missing (stale cache)
    if not df_tienda_raw.empty and "N_TIENDAS_IS90" not in df_tienda_raw.columns:
        st.info(
            "⚠️ Cache desactualizado — Haz clic en **Refrescar Datos** en el sidebar "
            "para cargar las columnas actualizadas (denominadores por ventana)."
        )

    # ── Coerce numerics ───────────────────────────────────────────────────
    _num_cols_tienda = [
        "N_TIENDAS",
        "N_TIENDAS_IS90", "N_TIENDAS_IS180", "N_TIENDAS_IS365",
        "TIENDAS_IS90", "TIENDAS_IS180", "TIENDAS_IS365",
        "TIENDAS_IS90B", "TIENDAS_IS180B", "TIENDAS_IS365B", "TIENDAS_IS_PRES",
        "STOCK_UND_TIENDA", "STOCK_COSTO_TIENDA",
        "N_TIENDAS_IS90_PERFIL", "N_TIENDAS_IS180_PERFIL", "N_TIENDAS_IS365_PERFIL",
        "TIENDAS_IS90_PERFIL", "TIENDAS_IS180_PERFIL", "TIENDAS_IS365_PERFIL",
        "INSTOCK_CD_90", "INSTOCK_CD_180", "INSTOCK_CD_365",
    ]
    for c in _num_cols_tienda:
        if c in df_tienda_raw.columns:
            df_tienda_raw[c] = pd.to_numeric(df_tienda_raw[c], errors="coerce").fillna(0)

    _num_cols_cd = [
        "STOCK_UND_CD", "STOCK_COSTO_CD",
        "INSTOCK_CD_90", "INSTOCK_CD_180", "INSTOCK_CD_365",
        "CANTIDAD_PROM_90_CIA", "COSTO_PROM_90_CIA",
    ]
    for c in _num_cols_cd:
        if c in df_cd_raw.columns:
            df_cd_raw[c] = pd.to_numeric(df_cd_raw[c], errors="coerce").fillna(0)

    # Parse dates
    for df in [df_tienda_raw, df_cd_raw]:
        if "FECHA" in df.columns:
            df["FECHA"] = pd.to_datetime(df["FECHA"], errors="coerce")

    # ── Enrich with ABC-XYZ-FSN classification ────────────────────────────
    try:
        _abc_lookup = cq.abc_xyz_fsn(conn)
        if not _abc_lookup.empty:
            _abc_cols = ["SKU_PRODUCTO", "CLASE_ABC", "CLASE_XYZ", "CLASE_FSN"]
            _abc_cols = [c for c in _abc_cols if c in _abc_lookup.columns]
            _abc_dedup = _abc_lookup[_abc_cols].drop_duplicates("SKU_PRODUCTO")
            if "SKU_PRODUCTO" in df_tienda_raw.columns:
                for _c in _abc_cols[1:]:
                    if _c in df_tienda_raw.columns:
                        df_tienda_raw = df_tienda_raw.drop(columns=[_c])
                df_tienda_raw = df_tienda_raw.merge(_abc_dedup, on="SKU_PRODUCTO", how="left")
            if "SKU_PRODUCTO" in df_cd_raw.columns:
                for _c in _abc_cols[1:]:
                    if _c in df_cd_raw.columns:
                        df_cd_raw = df_cd_raw.drop(columns=[_c])
                df_cd_raw = df_cd_raw.merge(_abc_dedup, on="SKU_PRODUCTO", how="left")
    except Exception:
        pass  # gracefully skip if ABC-XYZ-FSN not available

    df_tienda_raw = apply_pm_filter(df_tienda_raw)
    df_cd_raw = apply_pm_filter(df_cd_raw)

    # ── Dimension options for filters ─────────────────────────────────────
    combined_areas = sorted(
        set(df_tienda_raw["AREA"].dropna().unique())
        | set(df_cd_raw["AREA"].dropna().unique())
    )
    combined_lineas = sorted(
        set(df_tienda_raw["LINEA"].dropna().unique())
        | set(df_cd_raw["LINEA"].dropna().unique())
    )
    combined_marcas = sorted(
        set(df_tienda_raw["MARCA"].dropna().unique())
        | set(df_cd_raw["MARCA"].dropna().unique())
    )

    # ── Filters ───────────────────────────────────────────────────────────
    with st.expander("Filtros", expanded=True):
        fc1, fc2, fc3 = st.columns(3)

        with fc1:
            sel_window_label = st.radio(
                "Ventana de Venta Promedio",
                list(_WINDOW_OPTIONS.keys()),
                index=0,
                horizontal=True,
            )
            sel_window = _WINDOW_OPTIONS[sel_window_label]

        with fc2:
            # Determine available date range from loaded data
            _all_fechas = pd.concat([
                df_tienda_raw["FECHA"].dropna() if not df_tienda_raw.empty else pd.Series(dtype="datetime64[ns]"),
                df_cd_raw["FECHA"].dropna() if not df_cd_raw.empty else pd.Series(dtype="datetime64[ns]"),
            ])
            if _all_fechas.empty:
                _min_date = date.today() - timedelta(days=30)
                _max_date = date.today()
            else:
                _min_date = _all_fechas.min().date()
                _max_date = _all_fechas.max().date()

            _default_start = _max_date - timedelta(days=90)
            if _default_start < _min_date:
                _default_start = _min_date

            st.markdown("**Rango de Fechas**")
            _dc1, _dc2 = st.columns(2)
            with _dc1:
                fecha_inicio = st.date_input(
                    "Desde", value=_default_start,
                    min_value=_min_date, max_value=_max_date,
                    key="is_fecha_ini",
                )
            with _dc2:
                fecha_fin = st.date_input(
                    "Hasta", value=_max_date,
                    min_value=_min_date, max_value=_max_date,
                    key="is_fecha_fin",
                )

            _AGG_OPTIONS = {
                "Diario": "D", "Semanal": "W",
                "Mensual": "M", "Trimestral": "Q",
            }
            sel_agg_label = st.selectbox(
                "Nivel de Agregacion",
                list(_AGG_OPTIONS.keys()),
                index=0,
                key="is_agg_level",
            )
            sel_agg_period = _AGG_OPTIONS[sel_agg_label]

        with fc3:
            filter_cd_toggle = st.toggle(
                "Solo SKUs con InStock CD 90 = 1",
                value=True,
                help="Filtra solo SKUs donde el CD tenia stock suficiente "
                     "para cubrir 1 dia de venta promedio (ventana 90 dias). "
                     "Siempre usa ventana 90d independiente del display.",
            )
            solo_perfil_toggle = st.toggle(
                "Solo con Perfil de Reposicion",
                value=True,
                help="Solo cuenta combinaciones SKU-tienda que tienen "
                     "PERFIL configurado (reposicion activa). Excluye "
                     "tiendas donde el SKU no estaba asignado.",
            )

        fc4, fc5, fc6, fc7 = st.columns(4)
        with fc4:
            sel_areas = st.multiselect("Area", combined_areas, default=[])
        with fc5:
            sel_lineas = st.multiselect("Linea", combined_lineas, default=[])
        with fc6:
            sel_marcas = st.multiselect("Marca", combined_marcas, default=[])
        with fc7:
            mix_options = ["MIX", "IN & OUT", "FUERA MIX"]
            sel_mix = st.multiselect("Mix Oficial", mix_options, default=["MIX"], key="is_mix_filt")

        # ABC-XYZ-FSN classification filters
        _has_abc = "CLASE_ABC" in df_tienda_raw.columns or "CLASE_ABC" in df_cd_raw.columns
        if _has_abc:
            fc8, fc9, fc10 = st.columns(3)
            with fc8:
                sel_abc = st.multiselect("ABC", ["A", "B", "C"], default=[], key="is_abc_filt")
            with fc9:
                sel_xyz = st.multiselect("XYZ", ["X", "Y", "Z"], default=[], key="is_xyz_filt")
            with fc10:
                sel_fsn = st.multiselect("FSN", ["F", "S", "N"], default=[], key="is_fsn_filt")
        else:
            sel_abc, sel_xyz, sel_fsn = [], [], []

    # ── Apply filters ─────────────────────────────────────────────────────
    _ts_inicio = pd.Timestamp(fecha_inicio)
    _ts_fin = pd.Timestamp(fecha_fin)

    # Warn if daily aggregation requested but range exceeds daily data coverage
    if sel_agg_period == "D":
        _daily_min = None
        if not _df_daily_t.empty and "FECHA" in _df_daily_t.columns:
            _daily_min = _df_daily_t["FECHA"].min()
        elif not _df_daily_cd.empty and "FECHA" in _df_daily_cd.columns:
            _daily_min = _df_daily_cd["FECHA"].min()
        if _daily_min is not None and _ts_inicio < pd.Timestamp(_daily_min):
            st.warning(
                f"⚠️ Los datos diarios cubren desde **{_daily_min.strftime('%d/%m/%Y')}**. "
                f"Para fechas anteriores, los datos son semanales (solo lunes). "
                f"Considera usar agregacion **Semanal** o **Mensual** para rangos mas largos."
            )

    def _apply_filters(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        if "FECHA" in out.columns:
            out = out[(out["FECHA"] >= _ts_inicio) & (out["FECHA"] <= _ts_fin)]
        if sel_areas:
            out = out[out["AREA"].isin(sel_areas)]
        if sel_lineas:
            out = out[out["LINEA"].isin(sel_lineas)]
        if sel_marcas:
            out = out[out["MARCA"].isin(sel_marcas)]
        if sel_mix and "MIX_OFICIAL" in out.columns:
            out = out[out["MIX_OFICIAL"].isin(sel_mix)]
        # ABC-XYZ-FSN filters
        if sel_abc and "CLASE_ABC" in out.columns:
            out = out[out["CLASE_ABC"].isin(sel_abc)]
        if sel_xyz and "CLASE_XYZ" in out.columns:
            out = out[out["CLASE_XYZ"].isin(sel_xyz)]
        if sel_fsn and "CLASE_FSN" in out.columns:
            out = out[out["CLASE_FSN"].isin(sel_fsn)]
        return out

    df_tienda = _apply_filters(df_tienda_raw)
    df_cd = _apply_filters(df_cd_raw)

    if df_tienda.empty and df_cd.empty:
        st.warning("Sin datos para los filtros seleccionados.")
        return

    # ── Compute overall InStock series ────────────────────────────────────
    ts_tienda = _reaggregate_by_period(
        df_tienda, sel_agg_period, is_tienda=True, window=sel_window,
        filter_cd=filter_cd_toggle, solo_perfil=solo_perfil_toggle,
    )
    ts_cd = _reaggregate_by_period(
        df_cd, sel_agg_period, is_tienda=False, window=sel_window,
    )

    # ── KPI cards ─────────────────────────────────────────────────────────
    def _latest_and_delta(ts: pd.DataFrame) -> tuple[float, float]:
        """Return (latest_pct, delta_vs_previous)."""
        if ts.empty or "FECHA" not in ts.columns:
            return 0.0, 0.0
        ts_sorted = ts.sort_values("FECHA")
        latest = ts_sorted["INSTOCK_PCT"].iloc[-1] if len(ts_sorted) > 0 else 0.0
        prev = ts_sorted["INSTOCK_PCT"].iloc[-2] if len(ts_sorted) > 1 else latest
        return latest, latest - prev

    is_tienda_pct, is_tienda_delta = _latest_and_delta(ts_tienda)
    is_cd_pct, is_cd_delta = _latest_and_delta(ts_cd)

    n_skus_tienda = df_tienda["SKU_PRODUCTO"].nunique() if not df_tienda.empty else 0
    n_skus_cd = df_cd["SKU_PRODUCTO"].nunique() if not df_cd.empty else 0

    latest_date_str = ""
    for _df in [df_tienda, df_cd]:
        if not _df.empty and "FECHA" in _df.columns:
            ld = _df["FECHA"].max()
            if hasattr(ld, "strftime"):
                latest_date_str = ld.strftime("%d/%m/%Y")
            break

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        delta_str = f"({'+'if is_tienda_delta>=0 else ''}{is_tienda_delta*100:.1f}pp)"
        delta_color = COLORS["status_on_track"] if is_tienda_delta >= 0 else COLORS["status_critical"]
        st.html(
            _kpi_card_html("InStock Tiendas", _format_pct(is_tienda_pct), delta_str, delta_color)
        )
    with k2:
        delta_str = f"({'+'if is_cd_delta>=0 else ''}{is_cd_delta*100:.1f}pp)"
        delta_color = COLORS["status_on_track"] if is_cd_delta >= 0 else COLORS["status_critical"]
        st.html(
            _kpi_card_html("InStock CD", _format_pct(is_cd_pct), delta_str, delta_color)
        )
    with k3:
        filter_label = "con IS CD=1" if filter_cd_toggle else "todos"
        st.html(
            _kpi_card_html(f"SKUs Tienda ({filter_label})", f"{n_skus_tienda:,}")
        )
    with k4:
        st.html(
            _kpi_card_html("Ultima Fecha", latest_date_str or "N/A")
        )

    st.html("<br>")

    # ── Diagnostics (collapsed) ───────────────────────────────────────
    with st.expander("🔍 Diagnostico de datos", expanded=False):
        if not df_tienda.empty:
            cols_w = _COL_MAP_TIENDA[sel_window]
            cd_col_diag = _CD_FILTER_COL  # Always 90-day CD
            is_col_diag = cols_w["is"]
            # Determine denominator column (window-specific or legacy)
            n_col_diag = cols_w["n"] if cols_w["n"] in df_tienda.columns else "N_TIENDAS"

            total_rows = len(df_tienda)
            rows_cd1 = int((df_tienda[cd_col_diag] >= 1).sum()) if cd_col_diag in df_tienda.columns else 0

            dc1, dc2, dc3 = st.columns(3)
            with dc1:
                st.metric("Filas SKU×fecha", f"{total_rows:,}")
                st.metric("Filas con InStock CD ≥ 1", f"{rows_cd1:,}")
                pct_cd = f"{rows_cd1 / total_rows * 100:.1f}%" if total_rows > 0 else "0%"
                st.metric("% filas que pasan filtro CD", pct_cd)

            with dc2:
                if n_col_diag in df_tienda.columns:
                    total_combos = int(df_tienda[n_col_diag].sum())
                    total_is = int(df_tienda[is_col_diag].sum())
                    is_pct_raw = _format_pct(total_is / total_combos) if total_combos > 0 else "0.0%"
                    st.metric("Combinaciones SKU-Tienda", f"{total_combos:,}")
                    st.metric("IS% sin filtro CD", is_pct_raw)
                    st.metric(
                        "N_TIENDAS total vs ventana",
                        f"{int(df_tienda['N_TIENDAS'].sum()):,} vs {total_combos:,}"
                        if "N_TIENDAS" in df_tienda.columns else "N/A",
                    )

            with dc3:
                if cd_col_diag in df_tienda.columns and n_col_diag in df_tienda.columns:
                    filt_diag = df_tienda[df_tienda[cd_col_diag] >= 1]
                    if not filt_diag.empty:
                        combos_f = int(filt_diag[n_col_diag].sum())
                        is_f = int(filt_diag[is_col_diag].sum())
                        is_pct_f = _format_pct(is_f / combos_f) if combos_f > 0 else "0.0%"
                        st.metric("Combinaciones (filtro CD)", f"{combos_f:,}")
                        st.metric("IS% con filtro CD", is_pct_f)
                    else:
                        st.metric("Combinaciones (filtro CD)", "0")
                        st.metric("IS% con filtro CD", "N/A")

            # Show CD InStock value distribution
            if cd_col_diag in df_tienda.columns:
                dist = df_tienda[cd_col_diag].value_counts().sort_index()
                dist_df = pd.DataFrame({
                    cd_col_diag: dist.index.tolist(),
                    "Filas": dist.values.tolist(),
                })
                st.caption(f"Distribucion de {cd_col_diag}:")
                st.dataframe(dist_df, use_container_width=True, height=120)
        else:
            st.info("Sin datos de tiendas para diagnostico.")

    # Collect figures for PPT export
    figures_export: dict[str, go.Figure] = {}

    # ── Dynamic Y-axis range based on data ─────────────────────────────
    _all_pcts = []
    if not ts_tienda.empty:
        _all_pcts.extend(ts_tienda["INSTOCK_PCT"].dropna().tolist())
    if not ts_cd.empty:
        _all_pcts.extend(ts_cd["INSTOCK_PCT"].dropna().tolist())
    if _all_pcts:
        _y_min = max(0, min(_all_pcts) - 0.05)
        _y_max = min(1.02, max(_all_pcts) + 0.03)
        # Ensure at least 15pp range for readability
        if _y_max - _y_min < 0.15:
            _y_mid = (_y_min + _y_max) / 2
            _y_min = max(0, _y_mid - 0.075)
            _y_max = min(1.02, _y_mid + 0.075)
    else:
        _y_min, _y_max = 0, 1.02

    # ── Chart 1: Time series InStock Tiendas vs CD ────────────────────────
    st.markdown(
        f"### Evolucion InStock % — Ventana {sel_window_label}",
    )

    fig_ts = go.Figure(layout=dorel_layout(
        title=dict(
            text=f"InStock Tiendas vs CD — {sel_window_label}",
            font_size=15, x=0.5,
        ),
        yaxis=dict(
            tickformat=".0%", title="InStock %",
            gridcolor="#ECECEC", range=[_y_min, _y_max],
        ),
        xaxis=dict(title="", gridcolor="#ECECEC"),
        height=420,
        legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="center", x=0.5),
    ))

    # Smaller markers for long history (many data points)
    _mkr_sz = 3 if (not ts_tienda.empty and len(ts_tienda) > 30) else 5

    if not ts_tienda.empty:
        ts_t = ts_tienda.sort_values("FECHA")
        fig_ts.add_trace(go.Scatter(
            x=ts_t["FECHA"], y=ts_t["INSTOCK_PCT"],
            mode="lines+markers",
            name="Tiendas",
            line=dict(color=COLORS["primary"], width=2.5, shape="spline"),
            marker=dict(size=_mkr_sz),
            hovertemplate="%{x|%d/%m/%Y}: <b>%{y:.1%}</b><extra>Tiendas</extra>",
        ))

    if not ts_cd.empty:
        ts_c = ts_cd.sort_values("FECHA")
        fig_ts.add_trace(go.Scatter(
            x=ts_c["FECHA"], y=ts_c["INSTOCK_PCT"],
            mode="lines+markers",
            name="CD",
            line=dict(color=COLORS["status_at_risk"], width=2.5, shape="spline", dash="dot"),
            marker=dict(size=_mkr_sz),
            hovertemplate="%{x|%d/%m/%Y}: <b>%{y:.1%}</b><extra>CD</extra>",
        ))

    # Reference line at 93% (target)
    fig_ts.add_hline(y=0.93, line_dash="dash", line_color="#888", line_width=1,
                     annotation_text="Meta 93%", annotation_position="bottom right",
                     annotation_font_size=10, annotation_font_color="#888")

    st.plotly_chart(fig_ts, use_container_width=True)
    figures_export["Evolucion InStock"] = fig_ts

    # ── Chart 2: InStock by Area (bar chart latest date) ──────────────────
    st.markdown("### InStock por Area")

    area_t = _aggregate_instock_tienda(
        df_tienda, sel_window, ["AREA"],
        filter_cd=filter_cd_toggle, solo_perfil=solo_perfil_toggle,
    )
    area_cd = _aggregate_instock_cd(df_cd, sel_window, ["AREA"])

    # Filter to latest date only for the bar chart
    latest_t = df_tienda[df_tienda["FECHA"] == df_tienda["FECHA"].max()] if not df_tienda.empty else df_tienda
    latest_cd = df_cd[df_cd["FECHA"] == df_cd["FECHA"].max()] if not df_cd.empty else df_cd

    area_t_latest = _aggregate_instock_tienda(
        latest_t, sel_window, ["AREA"],
        filter_cd=filter_cd_toggle, solo_perfil=solo_perfil_toggle,
    )
    area_cd_latest = _aggregate_instock_cd(latest_cd, sel_window, ["AREA"])

    bar_col1, bar_col2 = st.columns(2)

    with bar_col1:
        if not area_t_latest.empty:
            area_t_sorted = area_t_latest.sort_values("INSTOCK_PCT", ascending=True)
            fig_bar_t = go.Figure(layout=dorel_layout(
                title=dict(text="InStock Tiendas por Area", font_size=14, x=0.5),
                xaxis=dict(tickformat=".0%", title="InStock %", range=[0, 1.05]),
                yaxis=dict(title=""),
                height=max(300, len(area_t_sorted) * 50 + 100),
            ))
            bar_colors = [
                COLORS["status_on_track"] if v >= 0.93
                else COLORS["status_at_risk"] if v >= 0.85
                else COLORS["status_critical"]
                for v in area_t_sorted["INSTOCK_PCT"]
            ]
            fig_bar_t.add_trace(go.Bar(
                x=area_t_sorted["INSTOCK_PCT"],
                y=area_t_sorted["AREA"],
                orientation="h",
                marker_color=bar_colors,
                text=area_t_sorted["INSTOCK_PCT"].apply(lambda v: f"{v*100:.1f}%"),
                textposition="outside",
                hovertemplate="%{y}: <b>%{x:.1%}</b><extra></extra>",
            ))
            st.plotly_chart(fig_bar_t, use_container_width=True)
            figures_export["InStock Tiendas por Area"] = fig_bar_t
        else:
            st.info("Sin datos de tiendas.")

    with bar_col2:
        if not area_cd_latest.empty:
            area_cd_sorted = area_cd_latest.sort_values("INSTOCK_PCT", ascending=True)
            fig_bar_cd = go.Figure(layout=dorel_layout(
                title=dict(text="InStock CD por Area", font_size=14, x=0.5),
                xaxis=dict(tickformat=".0%", title="InStock %", range=[0, 1.05]),
                yaxis=dict(title=""),
                height=max(300, len(area_cd_sorted) * 50 + 100),
            ))
            bar_colors_cd = [
                COLORS["status_on_track"] if v >= 0.93
                else COLORS["status_at_risk"] if v >= 0.85
                else COLORS["status_critical"]
                for v in area_cd_sorted["INSTOCK_PCT"]
            ]
            fig_bar_cd.add_trace(go.Bar(
                x=area_cd_sorted["INSTOCK_PCT"],
                y=area_cd_sorted["AREA"],
                orientation="h",
                marker_color=bar_colors_cd,
                text=area_cd_sorted["INSTOCK_PCT"].apply(lambda v: f"{v*100:.1f}%"),
                textposition="outside",
                hovertemplate="%{y}: <b>%{x:.1%}</b><extra></extra>",
            ))
            st.plotly_chart(fig_bar_cd, use_container_width=True)
            figures_export["InStock CD por Area"] = fig_bar_cd
        else:
            st.info("Sin datos de CD.")

    # ── Combined summary table (PowerBI style) ────────────────────────────
    st.markdown("---")
    st.markdown("### Resumen InStock — Tiendas vs CD")

    sum_col1, sum_col2 = st.columns(2)
    with sum_col1:
        summary_area = _build_combined_summary_table(
            df_tienda, df_cd, sel_window,
            filter_cd=filter_cd_toggle, solo_perfil=solo_perfil_toggle,
            group_col="AREA",
        )
        _render_combined_summary(summary_area, f"Por Area — {sel_window_label}", "AREA")

    with sum_col2:
        summary_linea = _build_combined_summary_table(
            df_tienda, df_cd, sel_window,
            filter_cd=filter_cd_toggle, solo_perfil=solo_perfil_toggle,
            group_col="LINEA",
        )
        _render_combined_summary(summary_linea, f"Por Linea — {sel_window_label}", "LINEA")

    # ── Pivot tables: Disponibilidad por Area (like PowerBI) ──────────────
    st.markdown("---")
    st.markdown(f"### Disponibilidad por Area — Evolucion {sel_agg_label}")

    pvt_col1, pvt_col2 = st.columns(2)

    # Tienda pivot by Area × Date
    area_date_t = _reaggregate_by_period(
        df_tienda, sel_agg_period, is_tienda=True, window=sel_window,
        filter_cd=filter_cd_toggle, solo_perfil=solo_perfil_toggle,
        group_cols=["AREA"],
    )
    pvt_tienda = _build_pivot_table(area_date_t, "AREA", top_n=20, agg_period=sel_agg_period)
    with pvt_col1:
        _render_pivot_styled(pvt_tienda, f"InStock Tiendas — {sel_window_label}")

    # CD pivot by Area × Date
    area_date_cd = _reaggregate_by_period(
        df_cd, sel_agg_period, is_tienda=False, window=sel_window,
        group_cols=["AREA"],
    )
    pvt_cd = _build_pivot_table(area_date_cd, "AREA", top_n=20, agg_period=sel_agg_period)
    with pvt_col2:
        _render_pivot_styled(pvt_cd, f"InStock CD — {sel_window_label}")

    # ── Pivot tables: by Linea ────────────────────────────────────────────
    st.markdown("---")
    st.markdown(f"### Disponibilidad por Linea — Evolucion {sel_agg_label}")

    pvt_col3, pvt_col4 = st.columns(2)

    linea_date_t = _reaggregate_by_period(
        df_tienda, sel_agg_period, is_tienda=True, window=sel_window,
        filter_cd=filter_cd_toggle, solo_perfil=solo_perfil_toggle,
        group_cols=["LINEA"],
    )
    pvt_linea_t = _build_pivot_table(linea_date_t, "LINEA", top_n=20, agg_period=sel_agg_period)
    with pvt_col3:
        _render_pivot_styled(pvt_linea_t, f"InStock Tiendas por Linea — {sel_window_label}")

    linea_date_cd = _reaggregate_by_period(
        df_cd, sel_agg_period, is_tienda=False, window=sel_window,
        group_cols=["LINEA"],
    )
    pvt_linea_cd = _build_pivot_table(linea_date_cd, "LINEA", top_n=20, agg_period=sel_agg_period)
    with pvt_col4:
        _render_pivot_styled(pvt_linea_cd, f"InStock CD por Linea — {sel_window_label}")

    # ── Chart 3: InStock evolution by Area (multi-line) ───────────────────
    st.markdown("---")
    st.markdown("### Evolucion InStock Tiendas por Area")

    if not area_date_t.empty:
        # Dynamic Y-axis for area chart
        _area_pcts = area_date_t["INSTOCK_PCT"].dropna().tolist()
        if _area_pcts:
            _ya_min = max(0, min(_area_pcts) - 0.05)
            _ya_max = min(1.02, max(_area_pcts) + 0.03)
            if _ya_max - _ya_min < 0.15:
                _ya_mid = (_ya_min + _ya_max) / 2
                _ya_min = max(0, _ya_mid - 0.075)
                _ya_max = min(1.02, _ya_mid + 0.075)
        else:
            _ya_min, _ya_max = 0, 1.02

        fig_area_ts = go.Figure(layout=dorel_layout(
            title=dict(text=f"InStock Tiendas por Area — {sel_window_label}", font_size=14, x=0.5),
            yaxis=dict(tickformat=".0%", title="InStock %", gridcolor="#ECECEC",
                       range=[_ya_min, _ya_max]),
            xaxis=dict(title="", gridcolor="#ECECEC"),
            height=460,
            legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="center", x=0.5),
        ))

        _palette = [
            COLORS["primary"], COLORS["status_at_risk"], COLORS["status_on_track"],
            COLORS["tertiary_teal"], COLORS["tertiary_pink"], COLORS["secondary"],
            COLORS["status_critical"], COLORS["status_en_curso"],
            "#FF6F00", "#6D4C41", "#546E7A", "#7B1FA2",
        ]
        areas_sorted = (
            area_date_t.groupby("AREA")["INSTOCK_PCT"].mean()
            .sort_values(ascending=False)
            .index.tolist()
        )
        _amkr_sz = 2 if len(area_date_t["FECHA"].unique()) > 30 else 4

        for idx, area in enumerate(areas_sorted[:10]):
            sub = area_date_t[area_date_t["AREA"] == area].sort_values("FECHA")
            fig_area_ts.add_trace(go.Scatter(
                x=sub["FECHA"], y=sub["INSTOCK_PCT"],
                mode="lines+markers",
                name=area,
                line=dict(color=_palette[idx % len(_palette)], width=2),
                marker=dict(size=_amkr_sz),
                hovertemplate=f"{area}<br>%{{x|%d/%m}}: <b>%{{y:.1%}}</b><extra></extra>",
            ))

        # Reference line at 93%
        fig_area_ts.add_hline(y=0.93, line_dash="dash", line_color="#888", line_width=1,
                              annotation_text="93%", annotation_position="bottom right",
                              annotation_font_size=10, annotation_font_color="#888")

        st.plotly_chart(fig_area_ts, use_container_width=True)
        figures_export["InStock Tiendas por Area (evolucion)"] = fig_area_ts
    else:
        st.info("Sin datos de tiendas para grafico de evolucion.")

    # ── Detail table: SKU × Periodo (aggregated, with download) ─────────
    st.markdown("---")
    st.markdown(f"### Detalle por SKU — Agregacion {sel_agg_label}")

    with st.expander("Ver tabla detalle SKU (con descarga)", expanded=False):
        # Context about data coverage
        _daily_dates = set()
        if not _df_daily_t.empty and "FECHA" in _df_daily_t.columns:
            _daily_dates = set(_df_daily_t["FECHA"].dropna().dt.date.unique())
        _n_dates_in_range = 0
        if not df_tienda.empty and "FECHA" in df_tienda.columns:
            _n_dates_in_range = df_tienda["FECHA"].nunique()
        if sel_agg_period == "D" and _n_dates_in_range > 0:
            _pct_daily = sum(1 for d in df_tienda["FECHA"].dropna().dt.date.unique() if d in _daily_dates) / max(_n_dates_in_range, 1) * 100
            if _pct_daily < 100:
                st.caption(
                    f"📌 **{_pct_daily:.0f}%** de las fechas en el rango tienen dato diario. "
                    f"El resto son muestreos semanales (lunes). "
                    f"Para IS% proporcional, usa agregacion **Semanal** o **Mensual**."
                )

        _cols_map = _COL_MAP_TIENDA[sel_window]

        # Determine IS cols
        if (solo_perfil_toggle
                and _cols_map["is_perfil"] in df_tienda.columns
                and _cols_map["n_perfil"] in df_tienda.columns):
            _is_col, _n_col = _cols_map["is_perfil"], _cols_map["n_perfil"]
        elif _cols_map["n"] in df_tienda.columns:
            _is_col, _n_col = _cols_map["is"], _cols_map["n"]
        else:
            _is_col, _n_col = _cols_map["is"], "N_TIENDAS"

        # ── Build detail per SKU × Periodo (tienda) ───────────────────
        if not df_tienda.empty:
            _det_t = df_tienda.copy()
            if filter_cd_toggle:
                _det_t = _det_t[_det_t[_CD_FILTER_COL] >= 1]

            # Bucket dates
            if sel_agg_period == "D":
                _det_t["PERIODO"] = _det_t["FECHA"]
            else:
                _det_t["PERIODO"] = _det_t["FECHA"].dt.to_period(sel_agg_period).dt.start_time

            _dim_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL"]
            _dim_cols = [c for c in _dim_cols if c in _det_t.columns]
            _grp_cols = ["PERIODO"] + _dim_cols

            _agg_dict = {
                "IS_NUMERADOR": (_is_col, "sum"),
                "IS_DENOMINADOR": (_n_col, "sum"),
            }
            for _sc in ["STOCK_UND_TIENDA", "STOCK_COSTO_TIENDA"]:
                if _sc in _det_t.columns:
                    _agg_dict[_sc] = (_sc, "mean")
            for _pc in [_cols_map.get("n_perfil"), _cols_map.get("is_perfil")]:
                if _pc and _pc in _det_t.columns:
                    _agg_dict[f"{_pc}_SUM"] = (_pc, "sum")
            if _cols_map["cd"] in _det_t.columns:
                _agg_dict["INSTOCK_CD"] = (_cols_map["cd"], "mean")
            # ABC-XYZ (take first, they're static per SKU)
            for _ac in ["CLASE_ABC", "CLASE_XYZ", "CLASE_FSN"]:
                if _ac in _det_t.columns:
                    _agg_dict[_ac] = (_ac, "first")

            _det_agg = _det_t.groupby(_grp_cols, dropna=False).agg(**_agg_dict).reset_index()
            _det_agg["IS_PCT_TIENDA"] = np.where(
                _det_agg["IS_DENOMINADOR"] > 0,
                _det_agg["IS_NUMERADOR"] / _det_agg["IS_DENOMINADOR"],
                0.0,
            )
            _det_agg = _det_agg.rename(columns={"PERIODO": "FECHA"})
        else:
            _det_agg = pd.DataFrame()

        # ── CD detail per SKU × Periodo ───────────────────────────────
        if not df_cd.empty:
            _det_cd = df_cd.copy()
            if sel_agg_period == "D":
                _det_cd["PERIODO"] = _det_cd["FECHA"]
            else:
                _det_cd["PERIODO"] = _det_cd["FECHA"].dt.to_period(sel_agg_period).dt.start_time

            _cd_agg_dict = {}
            for _cc in ["STOCK_UND_CD", "STOCK_COSTO_CD"]:
                if _cc in _det_cd.columns:
                    _cd_agg_dict[_cc] = (_cc, "mean")
            if "INSTOCK_CD_90" in _det_cd.columns:
                _cd_agg_dict["INSTOCK_CD_90"] = ("INSTOCK_CD_90", "mean")
            if "CANTIDAD_PROM_90_CIA" in _det_cd.columns:
                _cd_agg_dict["CANTIDAD_PROM_90_CIA"] = ("CANTIDAD_PROM_90_CIA", "mean")

            if _cd_agg_dict:
                _det_cd_agg = _det_cd.groupby(["PERIODO", "SKU_PRODUCTO"], dropna=False).agg(
                    **_cd_agg_dict
                ).reset_index().rename(columns={"PERIODO": "FECHA"})
            else:
                _det_cd_agg = pd.DataFrame()
        else:
            _det_cd_agg = pd.DataFrame()

        # ── Merge tienda + CD ─────────────────────────────────────────
        if not _det_agg.empty:
            if not _det_cd_agg.empty:
                _cd_new = [c for c in _det_cd_agg.columns
                           if c not in _det_agg.columns or c in ("SKU_PRODUCTO", "FECHA")]
                _det_agg = _det_agg.merge(
                    _det_cd_agg[_cd_new], on=["FECHA", "SKU_PRODUCTO"], how="left",
                )
            _det_agg = _det_agg.sort_values(["FECHA", "IS_PCT_TIENDA"], ascending=[True, True])

            st.caption(
                f"{len(_det_agg):,} filas — {_det_agg['SKU_PRODUCTO'].nunique():,} SKUs "
                f"x {_det_agg['FECHA'].nunique()} periodos"
            )
            st.dataframe(_det_agg, use_container_width=True, height=500)
            download_buttons(_det_agg, prefix="instock_detalle_sku")

        elif not _det_cd_agg.empty:
            st.dataframe(_det_cd_agg, use_container_width=True, height=500)
            download_buttons(_det_cd_agg, prefix="instock_detalle_sku")
        else:
            st.info("Sin datos para el rango seleccionado.")

    # ── InStock Proyectado + Venta Perdida (tabs) ─────────────────────────
    st.markdown("---")

    # Build a filter function that mirrors the current dashboard filters
    def _proy_filter(df):
        out = df.copy()
        if sel_areas and "AREA" in out.columns:
            out = out[out["AREA"].isin(sel_areas)]
        if sel_lineas and "LINEA" in out.columns:
            out = out[out["LINEA"].isin(sel_lineas)]
        if sel_marcas and "MARCA" in out.columns:
            out = out[out["MARCA"].isin(sel_marcas)]
        if sel_mix and "MIX_OFICIAL" in out.columns:
            out = out[out["MIX_OFICIAL"].isin(sel_mix)]
        if sel_abc and "CLASE_ABC" in out.columns:
            out = out[out["CLASE_ABC"].isin(sel_abc)]
        if sel_xyz and "CLASE_XYZ" in out.columns:
            out = out[out["CLASE_XYZ"].isin(sel_xyz)]
        if sel_fsn and "CLASE_FSN" in out.columns:
            out = out[out["CLASE_FSN"].isin(sel_fsn)]
        return out

    _tab_proy, _tab_brechas, _tab_vp = st.tabs([
        "\U0001F4E6 InStock Proyectado",
        "\U0001F534 Brechas InStock",
        "\U0001F4B0 Venta Perdida",
    ])

    with _tab_proy:
        _render_projected_instock_tab(
            conn, _proy_filter, figures_export, filter_cd=filter_cd_toggle,
        )

    with _tab_brechas:
        _render_brechas_instock_tab(
            conn, _proy_filter, figures_export, filter_cd=filter_cd_toggle,
        )

    with _tab_vp:
        _render_venta_perdida_tab(
            conn, sel_window, sel_window_label,
            filter_cd_toggle, solo_perfil_toggle,
            _proy_filter, figures_export,
        )

    # ── Export PPT ────────────────────────────────────────────────────────
    if figures_export:
        from utils.export import generate_ppt

        ppt_buffer = generate_ppt(figures_export, title="InStock Historico — Dorel")
        st.download_button(
            "Descargar Reporte PPT",
            ppt_buffer,
            file_name="instock_historico_dorel.pptx",
            mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
