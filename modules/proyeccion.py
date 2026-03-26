"""Stock projection module - iterative monthly simulation."""

import io
import re
from datetime import datetime
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from config import dorel_layout, apply_pm_filter
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers
from openpyxl.utils import get_column_letter

from db.cache import cached_query as cq
from utils.filters import norm_cols, fmt_clp
from utils.export import download_buttons
from utils.auth import get_current_user, require_role
from utils.ui_animations import lottie_spinner
from utils.file_persistence import (
    save_input_file, get_saved_file_path, get_saved_file_info,
    has_saved_files, load_metadata, git_share_inputs,
    save_projection_results, load_projection_results,
    has_saved_projection, get_projection_info,
)


# ============================================================================
# SIMULATION ENGINE
# ============================================================================

def run_stock_simulation(df):
    """Execute iterative stock simulation (SKU-by-SKU monthly loop)."""
    frames = []
    for sku, g in df.groupby("SKU_PRODUCTO", sort=False):
        g = g.sort_values("PERIODO").reset_index(drop=True)
        n = len(g)

        # Numpy arrays for speed
        fc = g["FORECAST_COMPRA"].to_numpy(float)
        eta = g["ETA"].to_numpy(float)
        ve = g["DEMANDA_SIM_ETAIL"].to_numpy(float)
        vm = g["DEMANDA_SIM_MAYOR"].to_numpy(float)
        vmin = g["DEMANDA_SIM_TIENDA"].to_numpy(float)
        perfil_min = g["PERFIL_TIENDAS"].to_numpy(float)

        # Result vectors
        stock_ini_cd = np.zeros(n)
        stock_ini_ti = np.zeros(n)
        stock_ini_total = np.zeros(n)
        necesidad = np.zeros(n)
        carga = np.zeros(n)
        fin_cd = np.zeros(n)
        fin_ti = np.zeros(n)
        fin_total = np.zeros(n)
        stock_disp = np.zeros(n)
        lost_tienda = np.zeros(n)
        lost_cd_total = np.zeros(n)
        lost_etail = np.zeros(n)
        lost_mayor = np.zeros(n)
        ful_tienda = np.zeros(n)
        ful_etail = np.zeros(n)
        ful_mayor = np.zeros(n)
        disp_cd_post = np.zeros(n)
        demanda_cd_solo = np.zeros(n)
        target_cia = np.zeros(n)
        instock_cd_arr = np.zeros(n)
        instock_ti_arr = np.zeros(n)
        instock_cia_arr = np.zeros(n)

        # Initial stock from snapshot (first period only)
        cd = float(g.loc[0, "STOCK_INICIAL_CD"])
        ti = float(g.loc[0, "STOCK_INICIAL_TIENDA"])

        for i in range(n):
            stock_ini_cd[i] = cd
            stock_ini_ti[i] = ti
            stock_ini_total[i] = cd + ti

            inbound_total = fc[i] + eta[i]
            cd_avail = max(0.0, cd + inbound_total)

            # Store replenishment logic
            target_tienda = perfil_min[i] + vmin[i]
            necesidad[i] = max(0.0, target_tienda - ti)
            carga[i] = min(necesidad[i], cd_avail)

            # Store sales (fulfilled)
            tienda_avail = ti + carga[i]
            venta_minor_fulfilled = min(vmin[i], tienda_avail)
            ful_tienda[i] = venta_minor_fulfilled
            lost_tienda[i] = max(0.0, vmin[i] - venta_minor_fulfilled)
            ti_f = max(0.0, tienda_avail - venta_minor_fulfilled)

            # CD sales (Etail + Wholesale)
            cd_post_transfer = cd_avail - carga[i]
            dem_cd_only = ve[i] + vm[i]
            demanda_cd_solo[i] = dem_cd_only
            disp_cd_post[i] = cd_post_transfer

            venta_cd_total_ful = min(dem_cd_only, cd_post_transfer)
            lost_cd_total[i] = max(0.0, dem_cd_only - venta_cd_total_ful)

            # Pro-rate CD sales across channels
            if dem_cd_only > 0:
                ratio = venta_cd_total_ful / dem_cd_only
                ful_etail[i] = ve[i] * ratio
                ful_mayor[i] = vm[i] * ratio
                # Pro-rate lost sales by channel demand proportion
                lost_etail[i] = lost_cd_total[i] * (ve[i] / dem_cd_only)
                lost_mayor[i] = lost_cd_total[i] * (vm[i] / dem_cd_only)
            else:
                ful_etail[i] = 0.0
                ful_mayor[i] = 0.0
                lost_etail[i] = 0.0
                lost_mayor[i] = 0.0

            cd_f = max(0.0, cd_post_transfer - venta_cd_total_ful)

            fin_cd[i] = cd_f
            fin_ti[i] = ti_f
            fin_total[i] = cd_f + ti_f
            stock_disp[i] = (stock_ini_total[i]) + inbound_total
            target_cia[i] = (dem_cd_only + vmin[i]) + perfil_min[i]

            # InStock: 1 if closing stock covers today's consumption (proxy for tomorrow)
            consumo_cd_dia = carga[i] + ve[i] + vm[i]
            instock_cd_arr[i] = 1 if cd_f >= consumo_cd_dia else 0
            instock_ti_arr[i] = 1 if ti_f >= vmin[i] else 0
            instock_cia_arr[i] = 1 if (cd_f + ti_f) >= (vmin[i] + ve[i] + vm[i]) else 0

            # Carry forward
            cd, ti = cd_f, ti_f

        res_sku = pd.DataFrame({
            "SKU_PRODUCTO": sku,
            "PERIODO": g["PERIODO"],
            "FORECAST_COMPRA": fc,
            "ETA": eta,
            "STOCK_INICIAL_CD": stock_ini_cd,
            "STOCK_INICIAL_TIENDA": stock_ini_ti,
            "STOCK_INICIAL_TOTAL": stock_ini_total,
            "PERFIL_TIENDAS": perfil_min,
            "NECESIDAD_TIENDA": necesidad,
            "CARGA_REAL": carga,
            "STOCK_FINAL_CD": fin_cd,
            "STOCK_FINAL_TIENDA": fin_ti,
            "STOCK_FINAL_TOTAL": fin_total,
            "STOCK_DISPONIBLE": stock_disp,
            "DISP_CD_POST_TRANSFER": disp_cd_post,
            "VENTA_FUL_TIENDA_UND": ful_tienda,
            "VENTA_FUL_ETAIL_UND": ful_etail,
            "VENTA_FUL_MAYOR_UND": ful_mayor,
            "LOST_SALES_TIENDA": lost_tienda,
            "LOST_SALES_CD": lost_cd_total,
            "LOST_SALES_ETAIL": lost_etail,
            "LOST_SALES_MAYOR": lost_mayor,
            "DEMANDA_SIM_TIENDA": vmin,
            "DEMANDA_SIM_ETAIL": ve,
            "DEMANDA_SIM_MAYOR": vm,
            "INSTOCK_CD": instock_cd_arr,
            "INSTOCK_TIENDA": instock_ti_arr,
            "INSTOCK_CIA": instock_cia_arr,
            "DEFICIT_PERFIL_UND": necesidad - carga,
            "SKU_NOM_PRODUCTO": g["SKU_NOM_PRODUCTO"].iloc[0] if "SKU_NOM_PRODUCTO" in g.columns else "",
            "AREA": g["AREA"].iloc[0] if "AREA" in g.columns else "",
            "LINEA": g["LINEA"].iloc[0] if "LINEA" in g.columns else "",
            "SUBLINEA": g["SUBLINEA"].iloc[0] if "SUBLINEA" in g.columns else "",
            "MARCA": g["MARCA"].iloc[0] if "MARCA" in g.columns else "",
            "MODELO": g["MODELO"].iloc[0] if "MODELO" in g.columns else "",
            "PROCEDENCIA": g["PROCEDENCIA"].iloc[0] if "PROCEDENCIA" in g.columns else "",
            "MIX_OFICIAL": g["MIX_OFICIAL"].iloc[0] if "MIX_OFICIAL" in g.columns else "",
        })
        frames.append(res_sku)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ============================================================================
# DAILY SIMULATION ENGINE
# ============================================================================

_DOW_NAME_MAP = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def _build_daily_weights(conn) -> dict:
    """Build DOW×WOM weight lookups: global + per-canal (hybrid).

    Returns dict with:
      - "global":    DataFrame [DOW, WOM, PESO] (normalised, sum=1)
      - "per_canal": DataFrame [DOW, WOM, CANAL, PESO] (normalised per canal)

    The simulation uses global weights for ETAIL/TIENDA and per-canal
    weights for MAYORISTA only (backtest showed per-canal adds noise for
    ETAIL/TIENDA but captures MAYORISTA's weekday-only pattern).

    Falls back to empty DataFrames if queries fail.
    """
    result = {"global": pd.DataFrame(), "per_canal": pd.DataFrame()}

    def _parse_weights(df_w):
        """Parse raw Snowflake weight data into DOW/WOM int columns."""
        dow_col = "DOW_NAME" if "DOW_NAME" in df_w.columns else "DOW"
        df_w["DOW"] = df_w[dow_col].map(_DOW_NAME_MAP)
        df_w["DOW"] = pd.to_numeric(df_w["DOW"], errors="coerce")
        df_w = df_w.dropna(subset=["DOW"])
        df_w["DOW"] = df_w["DOW"].astype(int)
        df_w["WOM"] = pd.to_numeric(df_w["WOM"], errors="coerce").fillna(1).astype(int)
        peso_col = "PESO" if "PESO" in df_w.columns else "UNIDADES"
        df_w["PESO"] = pd.to_numeric(df_w[peso_col], errors="coerce").fillna(0)
        return df_w

    # ── Global weights ──
    try:
        df_w = cq.pesos_diarios(conn)
        df_w = norm_cols(df_w)
        if not df_w.empty:
            df_w = _parse_weights(df_w)
            wt = df_w.groupby(["DOW", "WOM"], as_index=False)["PESO"].sum()
            total = wt["PESO"].sum()
            if total > 0:
                wt["PESO"] = wt["PESO"] / total
            result["global"] = wt[["DOW", "WOM", "PESO"]]
    except Exception:
        pass

    # ── Per-canal weights ──
    try:
        df_w = cq.pesos_diarios_canal(conn)
        df_w = norm_cols(df_w)
        if not df_w.empty and "CANAL" in df_w.columns:
            df_w = _parse_weights(df_w)
            wt = df_w.groupby(["DOW", "WOM", "CANAL"], as_index=False)["PESO"].sum()
            for canal in wt["CANAL"].unique():
                mask = wt["CANAL"] == canal
                total_c = wt.loc[mask, "PESO"].sum()
                if total_c > 0:
                    wt.loc[mask, "PESO"] = wt.loc[mask, "PESO"] / total_c
            result["per_canal"] = wt[["DOW", "WOM", "CANAL", "PESO"]]
    except Exception:
        pass

    return result


# ── Event / promotional calendar ──────────────────────────────────
# Event windows: (label, month, start_day, end_day)
# The actual boost multiplier per SUBLINEA×CANAL comes from historical data
# (QUERY_EVENT_BOOSTS), not from hardcoded values.

EVENTOS_COMERCIALES = [
    # Calendario comercial Peru — year-specific (label, year, month, day_start, day_end)
    # Fuentes: cyberwow.pe, cyberdays.pe, CCL, IAB Peru

    # ── 2024 ──────────────────────────────────────────────────────
    ("SaleVerano",     2024,  1,  1, 25),
    ("CyberDays",      2024,  3, 18, 21),   # CyberDays marzo 2024
    ("CyberWow",       2024,  4, 15, 19),   # CyberWow abril 2024
    ("DiaMadre",       2024,  5,  6, 12),   # 2do dom mayo = 12 may 2024
    ("DiaPadre",       2024,  6, 10, 16),   # 3er dom junio = 16 jun 2024
    ("CyberDays",      2024,  7, 22, 25),   # CyberDays julio 2024
    ("CyberWow",       2024,  7, 15, 18),   # CyberWow julio 2024
    ("FiestasPatrias",  2024,  7, 21, 31),
    ("DiaNino",        2024,  8, 12, 18),   # 3er dom agosto = 18 ago 2024
    ("CyberWow",       2024, 11,  4,  7),   # CyberWow noviembre 2024
    ("BlackFriday",    2024, 11, 25, 30),   # Black Friday 29 nov + Black Week
    ("Navidad",        2024, 12,  1, 24),

    # ── 2025 ──────────────────────────────────────────────────────
    ("SaleVerano",     2025,  1,  1, 25),
    ("CyberDays",      2025,  3, 24, 27),   # CyberDays marzo 2025
    ("CyberWow",       2025,  4, 14, 17),   # CyberWow abril 2025 (est.)
    ("DiaMadre",       2025,  5,  5, 11),   # 2do dom mayo = 11 may 2025
    ("DiaPadre",       2025,  6,  9, 15),   # 3er dom junio = 15 jun 2025
    ("CyberDays",      2025,  7,  7, 10),   # CyberDays julio 2025
    ("CyberWow",       2025,  7, 14, 17),   # CyberWow julio 2025
    ("FiestasPatrias",  2025,  7, 21, 31),
    ("DiaNino",        2025,  8, 11, 17),   # 3er dom agosto = 17 ago 2025
    ("CyberDays",      2025, 10, 27, 30),   # CyberDays octubre 2025
    ("CyberWow",       2025, 11,  3,  6),   # CyberWow noviembre 2025
    ("BlackFriday",    2025, 11, 24, 30),   # Black Friday 28 nov + Black Week
    ("Navidad",        2025, 12,  1, 24),

    # ── 2026 ──────────────────────────────────────────────────────
    ("SaleVerano",     2026,  1,  1, 25),
    ("CyberDays",      2026,  3, 23, 26),   # CyberDays marzo 2026 (est.)
    ("CyberWow",       2026,  4, 13, 16),   # CyberWow abril 2026 (est.)
    ("DiaMadre",       2026,  5,  4, 10),   # 2do dom mayo = 10 may 2026
    ("DiaPadre",       2026,  6, 15, 21),   # 3er dom junio = 21 jun 2026
    ("CyberDays",      2026,  7,  6,  9),   # CyberDays julio 2026 (est.)
    ("CyberWow",       2026,  7, 13, 16),   # CyberWow julio 2026 (est.)
    ("FiestasPatrias",  2026,  7, 21, 31),
    ("DiaNino",        2026,  8, 10, 16),   # 3er dom agosto = 16 ago 2026
    ("CyberDays",      2026, 10, 26, 29),   # CyberDays octubre 2026 (est.)
    ("CyberWow",       2026, 11,  2,  5),   # CyberWow noviembre 2026 (est.)
    ("BlackFriday",    2026, 11, 23, 29),   # Black Friday 27 nov + Black Week
    ("Navidad",        2026, 12,  1, 24),

    # ── 2027 (forecast) ──────────────────────────────────────────
    ("SaleVerano",     2027,  1,  1, 25),
    ("CyberDays",      2027,  3, 22, 25),
    ("CyberWow",       2027,  4, 12, 15),
    ("DiaMadre",       2027,  5,  3,  9),   # 2do dom mayo = 9 may 2027
    ("DiaPadre",       2027,  6, 14, 20),   # 3er dom junio = 20 jun 2027
    ("CyberDays",      2027,  7,  5,  8),
    ("CyberWow",       2027,  7, 12, 15),
    ("FiestasPatrias",  2027,  7, 21, 31),
    ("DiaNino",        2027,  8,  9, 15),   # 3er dom agosto = 15 ago 2027
    ("CyberDays",      2027, 10, 25, 28),
    ("CyberWow",       2027, 11,  1,  4),
    ("BlackFriday",    2027, 11, 22, 28),   # Black Friday 26 nov
    ("Navidad",        2027, 12,  1, 24),
]


def _classify_event_date(fecha):
    """Return event label for a date, or 'Normal'."""
    y, m, d = fecha.year, fecha.month, fecha.day
    for label, ey, em, ds, de in EVENTOS_COMERCIALES:
        if y == ey and m == em and ds <= d <= de:
            return label
    return "Normal"


def _build_event_boosts_from_data(conn) -> dict:
    """Load data-driven event boost factors from Snowflake.

    Returns a dict with two DataFrames:
      - "sku":      [SKU_PRODUCTO, CANAL, EVENTO, BOOST]  (granular, per-SKU)
      - "sublinea": [SUBLINEA, CANAL, EVENTO, BOOST]      (fallback, per-SUBLINEA)

    Hierarchy at apply time: SKU-level → SUBLINEA-level → 1.0 (no boost).

    Falls back to empty dict if data unavailable.
    """
    result = {"sku": pd.DataFrame(), "sublinea": pd.DataFrame()}
    try:
        # ── SKU-level boosts (granular) ──
        df_sku = cq.event_boosts_sku(conn)
        df_sku = norm_cols(df_sku)
        if not df_sku.empty:
            col = "BOOST_MEDIANO" if "BOOST_MEDIANO" in df_sku.columns else "BOOST_PROMEDIO"
            df_sku["BOOST"] = pd.to_numeric(df_sku[col], errors="coerce").fillna(1.0)
            df_sku["BOOST"] = df_sku["BOOST"].clip(0.1, 30.0)
            result["sku"] = df_sku[["SKU_PRODUCTO", "CANAL", "EVENTO", "BOOST"]].copy()

        # ── SUBLINEA-level boosts (fallback) ──
        df_sub = cq.event_boosts(conn)
        df_sub = norm_cols(df_sub)
        if not df_sub.empty:
            col = "BOOST_MEDIANO" if "BOOST_MEDIANO" in df_sub.columns else "BOOST_PROMEDIO"
            df_sub["BOOST"] = pd.to_numeric(df_sub[col], errors="coerce").fillna(1.0)
            df_sub["BOOST"] = df_sub["BOOST"].clip(0.1, 30.0)
            result["sublinea"] = df_sub[["SUBLINEA", "CANAL", "EVENTO", "BOOST"]].copy()

        return result
    except Exception:
        return result


def _expand_forecast_to_daily(f_piv, weights_dict=None, event_boosts_dict=None):
    """Expand monthly forecast rows into daily rows with weighted distribution.

    Hybrid approach: ETAIL/TIENDA use global DOW×WOM weights,
    MAYORISTA uses per-canal weights (captures weekday-only pattern).

    weights_dict: dict with "global" (DataFrame [DOW,WOM,PESO]) and
                  "per_canal" (DataFrame [DOW,WOM,CANAL,PESO]).
                  Also accepts a single DataFrame for backward compat.

    If event_boosts_dict is provided (dict with "sku" and "sublinea" DataFrames
    from _build_event_boosts_from_data), applies multiplicative boosts on event
    days with hierarchical fallback: SKU-level → SUBLINEA-level → 1.0.
    MAYORISTA boosts are disabled (always 1.0) since that channel is too erratic.
    Re-normalises within each SKU×month so monthly totals are preserved.

    Input:  f_piv with [SKU_PRODUCTO, PERIODO, FORECAST_VENTA_ETAIL/MAYOR/MINOR/TOTAL,
                        optionally SUBLINEA]
    Output: same columns + FECHA (date), with forecast weighted per day.
    """
    import calendar

    f = f_piv.copy()
    f["PERIODO"] = pd.to_datetime(f["PERIODO"])
    f["_YEAR"] = f["PERIODO"].dt.year
    f["_MONTH"] = f["PERIODO"].dt.month
    f["_N_DAYS"] = f.apply(lambda r: calendar.monthrange(int(r["_YEAR"]), int(r["_MONTH"]))[1], axis=1)
    f["_DATES"] = f.apply(
        lambda r: pd.date_range(r["PERIODO"], periods=int(r["_N_DAYS"]), freq="D"), axis=1
    )

    f_daily = f.explode("_DATES").rename(columns={"_DATES": "FECHA"})
    fc_cols = [c for c in f_daily.columns if c.startswith("FORECAST_VENTA")]

    # ── Parse weights_dict (supports dict or legacy single DataFrame) ──
    if isinstance(weights_dict, dict) and ("global" in weights_dict or "per_canal" in weights_dict):
        wt_global = weights_dict.get("global", pd.DataFrame())
        wt_per_canal = weights_dict.get("per_canal", pd.DataFrame())
    elif isinstance(weights_dict, pd.DataFrame) and not weights_dict.empty:
        # Backward compat: single DataFrame passed directly
        if "CANAL" in weights_dict.columns:
            wt_global = pd.DataFrame()
            wt_per_canal = weights_dict
        else:
            wt_global = weights_dict
            wt_per_canal = pd.DataFrame()
    else:
        wt_global = pd.DataFrame()
        wt_per_canal = pd.DataFrame()

    use_weights = not wt_global.empty or not wt_per_canal.empty
    _has_per_canal = not wt_per_canal.empty

    # Check if we have any event boost data
    _has_sku_boosts = (event_boosts_dict is not None
                       and isinstance(event_boosts_dict, dict)
                       and not event_boosts_dict.get("sku", pd.DataFrame()).empty)
    _has_sub_boosts = (event_boosts_dict is not None
                       and isinstance(event_boosts_dict, dict)
                       and not event_boosts_dict.get("sublinea", pd.DataFrame()).empty
                       and "SUBLINEA" in f_daily.columns)
    use_events = _has_sku_boosts or _has_sub_boosts

    # Map forecast columns → canal name (for weight & boost assignment)
    _col_canal_map = {
        "FORECAST_VENTA_ETAIL": "ETAIL",
        "FORECAST_VENTA_MINOR": "TIENDA",
        "FORECAST_VENTA_MAYOR": "MAYORISTA",
    }
    # Which canals use per-canal weights (hybrid: only MAYORISTA)
    _CANALS_PER_CANAL_WEIGHTS = {"MAYORISTA"}

    if use_weights:
        # Compute DOW (Python: Monday=0) and WOM (ceil(day/7))
        f_daily["_DOW"] = f_daily["FECHA"].dt.weekday            # 0=Mon…6=Sun
        f_daily["_WOM"] = np.ceil(f_daily["FECHA"].dt.day / 7.0).astype(int)

        # ── Merge global weights once (for ETAIL/TIENDA) ──
        if not wt_global.empty:
            f_daily = f_daily.merge(
                wt_global.rename(columns={"DOW": "_DOW", "WOM": "_WOM", "PESO": "_PESO_GLOBAL"}),
                on=["_DOW", "_WOM"], how="left"
            )
            f_daily["_PESO_GLOBAL"] = f_daily["_PESO_GLOBAL"].fillna(0)
        else:
            f_daily["_PESO_GLOBAL"] = 0

        # ── Assign per-column peso: global for ETAIL/TIENDA, per-canal for MAYORISTA ──
        for fc_col in fc_cols:
            canal_name = _col_canal_map.get(fc_col, None)
            if canal_name is None:
                continue  # TOTAL handled later
            peso_col = f"_PESO_{fc_col}"

            if canal_name in _CANALS_PER_CANAL_WEIGHTS and _has_per_canal:
                # Per-canal weight for this canal
                w_canal = wt_per_canal[wt_per_canal["CANAL"] == canal_name][["DOW", "WOM", "PESO"]].copy()
                w_canal = w_canal.rename(columns={"DOW": "_DOW", "WOM": "_WOM", "PESO": peso_col})
                f_daily = f_daily.merge(w_canal, on=["_DOW", "_WOM"], how="left")
                f_daily[peso_col] = f_daily[peso_col].fillna(0)
            else:
                # Global weight
                f_daily[peso_col] = f_daily["_PESO_GLOBAL"]

        f_daily = f_daily.drop(columns=["_PESO_GLOBAL"], errors="ignore")

        # ── Apply data-driven event boosts: SKU → SUBLINEA → 1.0 ──
        if use_events:
            # Classify each date as an event or 'Normal'
            f_daily["_EVENTO"] = f_daily["FECHA"].apply(_classify_event_date)

            df_sku_b = event_boosts_dict.get("sku", pd.DataFrame()) if _has_sku_boosts else pd.DataFrame()
            df_sub_b = event_boosts_dict.get("sublinea", pd.DataFrame()) if _has_sub_boosts else pd.DataFrame()

            # For each forecast column, compute boost with SKU→SUBLINEA→1.0 fallback
            for fc_col in fc_cols:
                canal_name = _col_canal_map.get(fc_col, None)
                if canal_name is None:
                    # TOTAL: will be handled after canal-specific columns
                    continue

                boost_col = f"_B_{fc_col}"
                f_daily[boost_col] = 1.0  # default: no boost

                # ── MAYORISTA: skip event boosts (too erratic) ──
                if canal_name == "MAYORISTA":
                    # Keep boost = 1.0 for MAYORISTA
                    pass
                else:
                    # Only apply on event days
                    evt_mask = f_daily["_EVENTO"] != "Normal"

                    if not df_sku_b.empty:
                        # Step 1: SKU-level boost
                        sku_sub = df_sku_b[df_sku_b["CANAL"] == canal_name][
                            ["SKU_PRODUCTO", "EVENTO", "BOOST"]
                        ].rename(columns={"BOOST": "_B_SKU"})
                        f_daily = f_daily.merge(
                            sku_sub,
                            left_on=["SKU_PRODUCTO", "_EVENTO"],
                            right_on=["SKU_PRODUCTO", "EVENTO"],
                            how="left",
                            suffixes=("", f"_skuevt_{canal_name}"),
                        )
                        f_daily = f_daily.drop(
                            columns=[c for c in f_daily.columns
                                     if c.startswith("EVENTO") and c != "_EVENTO"],
                            errors="ignore",
                        )
                        has_sku = evt_mask & f_daily["_B_SKU"].notna()
                        f_daily.loc[has_sku, boost_col] = f_daily.loc[has_sku, "_B_SKU"]
                        f_daily = f_daily.drop(columns=["_B_SKU"], errors="ignore")

                    if not df_sub_b.empty and "SUBLINEA" in f_daily.columns:
                        # Step 2: SUBLINEA-level fallback
                        sub_sub = df_sub_b[df_sub_b["CANAL"] == canal_name][
                            ["SUBLINEA", "EVENTO", "BOOST"]
                        ].rename(columns={"BOOST": "_B_SUB"})
                        f_daily = f_daily.merge(
                            sub_sub,
                            left_on=["SUBLINEA", "_EVENTO"],
                            right_on=["SUBLINEA", "EVENTO"],
                            how="left",
                            suffixes=("", f"_subevt_{canal_name}"),
                        )
                        f_daily = f_daily.drop(
                            columns=[c for c in f_daily.columns
                                     if c.startswith("EVENTO") and c != "_EVENTO"],
                            errors="ignore",
                        )
                        needs_sub = evt_mask & (f_daily[boost_col] == 1.0) & f_daily["_B_SUB"].notna()
                        f_daily.loc[needs_sub, boost_col] = f_daily.loc[needs_sub, "_B_SUB"]
                        f_daily = f_daily.drop(columns=["_B_SUB"], errors="ignore")

                # Boosted weight = base_peso × boost
                peso_col = f"_PESO_{fc_col}"
                f_daily[f"_WB_{fc_col}"] = f_daily[peso_col] * f_daily[boost_col]
                # Normalise within SKU × month (preserves monthly total)
                g_sum = f_daily.groupby(["SKU_PRODUCTO", "PERIODO"])[f"_WB_{fc_col}"].transform("sum")
                f_daily[f"_WN_{fc_col}"] = np.where(
                    g_sum > 0,
                    f_daily[f"_WB_{fc_col}"] / g_sum,
                    1.0 / f_daily["_N_DAYS"],
                )
                f_daily[fc_col] = f_daily[fc_col] * f_daily[f"_WN_{fc_col}"]

            # Handle TOTAL: weighted average of canal weights+boosts proportional to forecast
            if "FORECAST_VENTA_TOTAL" in fc_cols:
                fc_e = pd.to_numeric(f_daily.get("FORECAST_VENTA_ETAIL", 0), errors="coerce").fillna(0)
                fc_m = pd.to_numeric(f_daily.get("FORECAST_VENTA_MINOR", 0), errors="coerce").fillna(0)
                fc_y = pd.to_numeric(f_daily.get("FORECAST_VENTA_MAYOR", 0), errors="coerce").fillna(0)
                b_e = f_daily.get("_B_FORECAST_VENTA_ETAIL", pd.Series(1.0, index=f_daily.index))
                b_m = f_daily.get("_B_FORECAST_VENTA_MINOR", pd.Series(1.0, index=f_daily.index))
                b_y = f_daily.get("_B_FORECAST_VENTA_MAYOR", pd.Series(1.0, index=f_daily.index))
                p_e = f_daily.get("_PESO_FORECAST_VENTA_ETAIL", pd.Series(0, index=f_daily.index))
                p_m = f_daily.get("_PESO_FORECAST_VENTA_MINOR", pd.Series(0, index=f_daily.index))
                p_y = f_daily.get("_PESO_FORECAST_VENTA_MAYOR", pd.Series(0, index=f_daily.index))
                fc_sum_raw = fc_e + fc_m + fc_y
                wb_total = np.where(
                    fc_sum_raw > 0,
                    (fc_e * p_e * b_e + fc_m * p_m * b_m + fc_y * p_y * b_y) / fc_sum_raw,
                    (p_e + p_m + p_y) / 3.0,
                )
                f_daily["_WB_TOTAL"] = wb_total
                g_sum_t = f_daily.groupby(["SKU_PRODUCTO", "PERIODO"])["_WB_TOTAL"].transform("sum")
                wn_total = np.where(g_sum_t > 0, wb_total / g_sum_t, 1.0 / f_daily["_N_DAYS"])
                f_daily["FORECAST_VENTA_TOTAL"] = f_daily["FORECAST_VENTA_TOTAL"] * wn_total

            # Clean up temporary columns
            drop_cols = [c for c in f_daily.columns
                         if c.startswith(("_B_", "_WB_", "_WN_", "_EVENTO", "_PESO_"))]
            f_daily = f_daily.drop(columns=drop_cols, errors="ignore")
        else:
            # No events — standard normalisation per canal column
            for fc_col in fc_cols:
                peso_col = f"_PESO_{fc_col}"
                if fc_col == "FORECAST_VENTA_TOTAL":
                    # TOTAL: weighted average of canal pesos
                    fc_e = pd.to_numeric(f_daily.get("FORECAST_VENTA_ETAIL", 0), errors="coerce").fillna(0)
                    fc_m = pd.to_numeric(f_daily.get("FORECAST_VENTA_MINOR", 0), errors="coerce").fillna(0)
                    fc_y = pd.to_numeric(f_daily.get("FORECAST_VENTA_MAYOR", 0), errors="coerce").fillna(0)
                    p_e = f_daily.get("_PESO_FORECAST_VENTA_ETAIL", pd.Series(0, index=f_daily.index))
                    p_m = f_daily.get("_PESO_FORECAST_VENTA_MINOR", pd.Series(0, index=f_daily.index))
                    p_y = f_daily.get("_PESO_FORECAST_VENTA_MAYOR", pd.Series(0, index=f_daily.index))
                    fc_sum_raw = fc_e + fc_m + fc_y
                    peso_total = np.where(
                        fc_sum_raw > 0,
                        (fc_e * p_e + fc_m * p_m + fc_y * p_y) / fc_sum_raw,
                        (p_e + p_m + p_y) / 3.0,
                    )
                    f_daily["_PESO_TOTAL_TMP"] = peso_total
                    g_sum = f_daily.groupby(["SKU_PRODUCTO", "PERIODO"])["_PESO_TOTAL_TMP"].transform("sum")
                    f_daily["_W"] = np.where(g_sum > 0, peso_total / g_sum, 1.0 / f_daily["_N_DAYS"])
                    f_daily[fc_col] = f_daily[fc_col] * f_daily["_W"]
                    f_daily = f_daily.drop(columns=["_PESO_TOTAL_TMP", "_W"], errors="ignore")
                elif peso_col in f_daily.columns:
                    group_peso = f_daily.groupby(["SKU_PRODUCTO", "PERIODO"])[peso_col].transform("sum")
                    f_daily["_W"] = np.where(
                        group_peso > 0,
                        f_daily[peso_col] / group_peso,
                        1.0 / f_daily["_N_DAYS"]
                    )
                    f_daily[fc_col] = f_daily[fc_col] * f_daily["_W"]
                    f_daily = f_daily.drop(columns=["_W"], errors="ignore")

            # Clean up peso columns
            drop_peso = [c for c in f_daily.columns if c.startswith("_PESO_")]
            f_daily = f_daily.drop(columns=drop_peso, errors="ignore")

        f_daily = f_daily.drop(columns=["_DOW", "_WOM"], errors="ignore")
    else:
        # Uniform fallback
        for col in fc_cols:
            f_daily[col] = f_daily[col] / f_daily["_N_DAYS"]

    f_daily = f_daily.drop(columns=["_YEAR", "_MONTH", "_N_DAYS"], errors="ignore")
    return f_daily.reset_index(drop=True)


def _find_compra_col(cols, keywords, label="columna"):
    """Find the first column matching any keyword. Returns None if not found."""
    for kw in keywords:
        match = next((c for c in cols if kw in c), None)
        if match:
            return match
    return None


def _expand_compra_to_daily(compra, col_fecha_c, col_sku_c, col_orden, col_lead):
    """Process purchase file keeping exact arrival dates (FECHA_ORDEN + LEAD_TIME).

    Returns DataFrame with [SKU_PRODUCTO, FECHA, FORECAST_COMPRA].
    """
    compra = compra.copy()
    compra[col_fecha_c] = pd.to_datetime(compra[col_fecha_c], errors="coerce")
    lead_val = pd.to_numeric(compra[col_lead], errors="coerce").fillna(0)
    compra["FECHA_RECEPCION"] = compra[col_fecha_c] + pd.to_timedelta(lead_val, unit="D")
    compra["FECHA"] = compra["FECHA_RECEPCION"].dt.normalize()
    compra["SKU_PRODUCTO"] = compra[col_sku_c].astype(str).str.strip().str.upper()
    compra["FORECAST_COMPRA"] = pd.to_numeric(compra[col_orden], errors="coerce").fillna(0)

    c_agg = compra.groupby(["SKU_PRODUCTO", "FECHA"], as_index=False)["FORECAST_COMPRA"].sum()
    return c_agg


def _expand_comex_to_daily(comex, maestra):
    """Process COMEX arrivals with flat 10-day port-to-CD delay, keeping exact arrival date.

    Returns DataFrame with [SKU_PRODUCTO, FECHA, ETA].
    """
    comex = comex.copy()

    # SKU standardization
    col_sku = next(
        (c for c in comex.columns if c.upper() in ("SKU_PRODUCTO", "SKU", "MATERIAL")), None
    )
    if col_sku:
        comex["SKU_PRODUCTO"] = comex[col_sku].astype(str).str.strip().str.upper()
    elif "SKU_PRODUCTO" not in comex.columns:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "FECHA", "ETA"])

    # ETA original
    col_eta = next((c for c in comex.columns if c == "ETA"), None)
    if not col_eta:
        col_eta = next(
            (c for c in comex.columns if "ETA" in c and "RECEP" not in c and "DISP" not in c),
            None,
        )
    if col_eta:
        comex["ETA_ORIGINAL"] = pd.to_datetime(comex[col_eta], errors="coerce")
    else:
        comex["ETA_ORIGINAL"] = pd.NaT

    # Flat 10-day port-to-CD delay
    comex["ETA_DISP"] = comex["ETA_ORIGINAL"] + pd.Timedelta(days=10)
    comex["FECHA"] = comex["ETA_DISP"].dt.normalize()

    # Pending quantity
    if "CANTIDAD_FINAL_CORREGIDA" in comex.columns:
        col_cant_final = "CANTIDAD_FINAL_CORREGIDA"
    elif "CANTIDAD_FINAL" in comex.columns:
        col_cant_final = "CANTIDAD_FINAL"
    else:
        col_cant_final = None

    col_cant_rec = next((c for c in comex.columns if "RECEPCIONADA" in c), None)

    c_final = pd.to_numeric(comex[col_cant_final], errors="coerce").fillna(0) if col_cant_final else 0
    c_rec = pd.to_numeric(comex[col_cant_rec], errors="coerce").fillna(0) if col_cant_rec else 0
    comex["PENDIENTE"] = (c_final - c_rec).clip(lower=0)

    # Filter valid rows
    comex = comex[comex["ETA_ORIGINAL"].notna() & (comex["PENDIENTE"] > 0)].copy()

    if comex.empty:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "FECHA", "ETA"])

    eta_agg = (
        comex.groupby(["SKU_PRODUCTO", "FECHA"], as_index=False)["PENDIENTE"]
        .sum()
        .rename(columns={"PENDIENTE": "ETA"})
    )
    return eta_agg


def run_stock_simulation_daily(df):
    """Execute iterative stock simulation at daily granularity (SKU-by-SKU daily loop).

    Same logic as run_stock_simulation but iterates per day instead of per month.
    Store replenishment target = perfil + next-day demand (so store stock stays ≈ perfil).
    """
    frames = []
    for sku, g in df.groupby("SKU_PRODUCTO", sort=False):
        g = g.sort_values("FECHA").reset_index(drop=True)
        n = len(g)

        # Numpy arrays for speed
        fc = g["FORECAST_COMPRA"].to_numpy(float)
        eta = g["ETA"].to_numpy(float)
        ve = g["DEMANDA_SIM_ETAIL"].to_numpy(float)
        vm = g["DEMANDA_SIM_MAYOR"].to_numpy(float)
        vmin = g["DEMANDA_SIM_TIENDA"].to_numpy(float)
        perfil_min = g["PERFIL_TIENDAS"].to_numpy(float)

        # Result vectors
        stock_ini_cd = np.zeros(n)
        stock_ini_ti = np.zeros(n)
        stock_ini_total = np.zeros(n)
        necesidad = np.zeros(n)
        carga = np.zeros(n)
        fin_cd = np.zeros(n)
        fin_ti = np.zeros(n)
        fin_total = np.zeros(n)
        stock_disp = np.zeros(n)
        lost_tienda = np.zeros(n)
        lost_cd_total = np.zeros(n)
        lost_etail = np.zeros(n)
        lost_mayor = np.zeros(n)
        ful_tienda = np.zeros(n)
        ful_etail = np.zeros(n)
        ful_mayor = np.zeros(n)
        disp_cd_post = np.zeros(n)
        demanda_cd_solo = np.zeros(n)
        target_cia = np.zeros(n)
        instock_cd_arr = np.zeros(n)
        instock_ti_arr = np.zeros(n)
        instock_cia_arr = np.zeros(n)

        # Initial stock from snapshot (first day only)
        cd = float(g.loc[0, "STOCK_INICIAL_CD"])
        ti = float(g.loc[0, "STOCK_INICIAL_TIENDA"])

        for i in range(n):
            stock_ini_cd[i] = cd
            stock_ini_ti[i] = ti
            stock_ini_total[i] = cd + ti

            inbound_total = fc[i] + eta[i]
            cd_avail = max(0.0, cd + inbound_total)

            # Store replenishment: cover perfil + NEXT day's demand
            demanda_next = vmin[i + 1] if (i + 1) < n else vmin[i]
            target_tienda = perfil_min[i] + demanda_next
            necesidad[i] = max(0.0, target_tienda - ti)
            carga[i] = min(necesidad[i], cd_avail)

            # Store sales (fulfilled) — today's demand
            tienda_avail = ti + carga[i]
            venta_minor_fulfilled = min(vmin[i], tienda_avail)
            ful_tienda[i] = venta_minor_fulfilled
            lost_tienda[i] = max(0.0, vmin[i] - venta_minor_fulfilled)
            ti_f = max(0.0, tienda_avail - venta_minor_fulfilled)

            # CD sales (Etail + Wholesale)
            cd_post_transfer = cd_avail - carga[i]
            dem_cd_only = ve[i] + vm[i]
            demanda_cd_solo[i] = dem_cd_only
            disp_cd_post[i] = cd_post_transfer

            venta_cd_total_ful = min(dem_cd_only, cd_post_transfer)
            lost_cd_total[i] = max(0.0, dem_cd_only - venta_cd_total_ful)

            # Pro-rate CD sales across channels
            if dem_cd_only > 0:
                ratio = venta_cd_total_ful / dem_cd_only
                ful_etail[i] = ve[i] * ratio
                ful_mayor[i] = vm[i] * ratio
                # Pro-rate lost sales by channel demand proportion
                lost_etail[i] = lost_cd_total[i] * (ve[i] / dem_cd_only)
                lost_mayor[i] = lost_cd_total[i] * (vm[i] / dem_cd_only)
            else:
                ful_etail[i] = 0.0
                ful_mayor[i] = 0.0
                lost_etail[i] = 0.0
                lost_mayor[i] = 0.0

            cd_f = max(0.0, cd_post_transfer - venta_cd_total_ful)

            fin_cd[i] = cd_f
            fin_ti[i] = ti_f
            fin_total[i] = cd_f + ti_f
            stock_disp[i] = (stock_ini_total[i]) + inbound_total
            target_cia[i] = (dem_cd_only + vmin[i]) + perfil_min[i]

            # InStock: 1 if closing stock covers today's consumption (proxy for tomorrow)
            consumo_cd_dia = carga[i] + ve[i] + vm[i]
            instock_cd_arr[i] = 1 if cd_f >= consumo_cd_dia else 0
            instock_ti_arr[i] = 1 if ti_f >= vmin[i] else 0
            instock_cia_arr[i] = 1 if (cd_f + ti_f) >= (vmin[i] + ve[i] + vm[i]) else 0

            # Carry forward to next DAY
            cd, ti = cd_f, ti_f

        res_sku = pd.DataFrame({
            "SKU_PRODUCTO": sku,
            "FECHA": g["FECHA"],
            "PERIODO": g["PERIODO"],
            "FORECAST_COMPRA": fc,
            "ETA": eta,
            "STOCK_INICIAL_CD": stock_ini_cd,
            "STOCK_INICIAL_TIENDA": stock_ini_ti,
            "STOCK_INICIAL_TOTAL": stock_ini_total,
            "PERFIL_TIENDAS": perfil_min,
            "NECESIDAD_TIENDA": necesidad,
            "CARGA_REAL": carga,
            "STOCK_FINAL_CD": fin_cd,
            "STOCK_FINAL_TIENDA": fin_ti,
            "STOCK_FINAL_TOTAL": fin_total,
            "STOCK_DISPONIBLE": stock_disp,
            "DISP_CD_POST_TRANSFER": disp_cd_post,
            "VENTA_FUL_TIENDA_UND": ful_tienda,
            "VENTA_FUL_ETAIL_UND": ful_etail,
            "VENTA_FUL_MAYOR_UND": ful_mayor,
            "LOST_SALES_TIENDA": lost_tienda,
            "LOST_SALES_CD": lost_cd_total,
            "LOST_SALES_ETAIL": lost_etail,
            "LOST_SALES_MAYOR": lost_mayor,
            "DEMANDA_SIM_TIENDA": vmin,
            "DEMANDA_SIM_ETAIL": ve,
            "DEMANDA_SIM_MAYOR": vm,
            "INSTOCK_CD": instock_cd_arr,
            "INSTOCK_TIENDA": instock_ti_arr,
            "INSTOCK_CIA": instock_cia_arr,
            "DEFICIT_PERFIL_UND": necesidad - carga,
            "SKU_NOM_PRODUCTO": g["SKU_NOM_PRODUCTO"].iloc[0] if "SKU_NOM_PRODUCTO" in g.columns else "",
            "AREA": g["AREA"].iloc[0] if "AREA" in g.columns else "",
            "LINEA": g["LINEA"].iloc[0] if "LINEA" in g.columns else "",
            "SUBLINEA": g["SUBLINEA"].iloc[0] if "SUBLINEA" in g.columns else "",
            "MARCA": g["MARCA"].iloc[0] if "MARCA" in g.columns else "",
            "MODELO": g["MODELO"].iloc[0] if "MODELO" in g.columns else "",
            "PROCEDENCIA": g["PROCEDENCIA"].iloc[0] if "PROCEDENCIA" in g.columns else "",
            "MIX_OFICIAL": g["MIX_OFICIAL"].iloc[0] if "MIX_OFICIAL" in g.columns else "",
        })
        frames.append(res_sku)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _aggregate_daily_to_monthly(df_daily):
    """Roll up daily simulation results to monthly for downstream compatibility.

    Flows (demand, sales, arrivals) → sum.
    Stock positions → first (opening) / last (closing).
    In-stock flags → min (conservative: 0 if any day had stockout).
    """
    df = df_daily.copy()
    df = df.sort_values(["SKU_PRODUCTO", "FECHA"])

    agg_dict = {
        # Flows: sum
        "FORECAST_COMPRA": "sum",
        "ETA": "sum",
        "DEMANDA_SIM_TIENDA": "sum",
        "DEMANDA_SIM_ETAIL": "sum",
        "DEMANDA_SIM_MAYOR": "sum",
        "VENTA_FUL_TIENDA_UND": "sum",
        "VENTA_FUL_ETAIL_UND": "sum",
        "VENTA_FUL_MAYOR_UND": "sum",
        "LOST_SALES_TIENDA": "sum",
        "LOST_SALES_CD": "sum",
        "LOST_SALES_ETAIL": "sum",
        "LOST_SALES_MAYOR": "sum",
        "NECESIDAD_TIENDA": "sum",
        "CARGA_REAL": "sum",
        "DEFICIT_PERFIL_UND": "sum",
        # Stock positions: first/last
        "STOCK_INICIAL_CD": "first",
        "STOCK_INICIAL_TIENDA": "first",
        "STOCK_INICIAL_TOTAL": "first",
        "STOCK_FINAL_CD": "last",
        "STOCK_FINAL_TIENDA": "last",
        "STOCK_FINAL_TOTAL": "last",
        "STOCK_DISPONIBLE": "first",
        # In-stock: mean of 0/1 flags = % of days with coverage
        "INSTOCK_CD": "mean",
        "INSTOCK_TIENDA": "mean",
        "INSTOCK_CIA": "mean",
        # In-stock days: sum of 0/1 flags = count of days with coverage
        "INSTOCK_DIAS_CD": "sum",
        "INSTOCK_DIAS_TIENDA": "sum",
        "INSTOCK_DIAS_CIA": "sum",
    }

    # Text columns: first (constant per SKU)
    for tc in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
               "MODELO", "PROCEDENCIA", "MIX_OFICIAL"]:
        if tc in df.columns:
            agg_dict[tc] = "first"

    # Create INSTOCK_DIAS_* columns (copy of 0/1 flags, will be sum'd)
    for _is in ["CD", "TIENDA", "CIA"]:
        src = f"INSTOCK_{_is}"
        dst = f"INSTOCK_DIAS_{_is}"
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src].copy()

    # Only aggregate columns that exist
    agg_dict = {k: v for k, v in agg_dict.items() if k in df.columns}

    df_monthly = df.groupby(["SKU_PRODUCTO", "PERIODO"], as_index=False).agg(agg_dict)

    # Calculate DIAS_MES (total calendar days per period)
    if "PERIODO" in df_monthly.columns:
        df_monthly["DIAS_MES"] = pd.to_datetime(df_monthly["PERIODO"]).dt.days_in_month

    # Perfil: take first (constant per SKU)
    if "PERFIL_TIENDAS" in df.columns:
        perfil = df.groupby(["SKU_PRODUCTO", "PERIODO"], as_index=False)["PERFIL_TIENDAS"].first()
        df_monthly = df_monthly.merge(perfil, on=["SKU_PRODUCTO", "PERIODO"], how="left")

    return df_monthly


# ============================================================================
# DATA PROCESSING
# ============================================================================

def process_projection(file_forecast, file_compra, file_precios, conn,
                        excluir_plan_compra=False):
    """Full projection pipeline: load inputs, query Snowflake, simulate, and return results.

    If *excluir_plan_compra* is True, FORECAST_COMPRA is zeroed out so the
    simulation only considers current stock + confirmed COMEX (ETA).
    """

    # 1. Load local files
    forecast = pd.read_excel(file_forecast, sheet_name=0)
    compra = pd.read_csv(file_compra)

    precios = None
    precios_mensuales = None

    if file_precios:
        _raw_precios = pd.read_excel(file_precios, sheet_name=0)
        _raw_precios = norm_cols(_raw_precios)
        col_sku_p = next((c for c in _raw_precios.columns if "SKU" in c or "MATERIAL" in c), None)
        if col_sku_p:
            _raw_precios["SKU_PRODUCTO"] = _raw_precios[col_sku_p].astype(str).str.strip().str.upper()

            # Detect format: monthly (notebook style) vs flat
            mes_cols = [c for c in _raw_precios.columns if re.fullmatch(r"\d{1,2}", str(c)) and 1 <= int(c) <= 12]
            canal_col = next((c for c in _raw_precios.columns if c in ("CANAL", "CANAL_STD")), None)

            if len(mes_cols) >= 6 and canal_col:
                # === MONTHLY FORMAT (SKU | CANAL | 1 | 2 | ... | 12) ===
                _raw_precios[canal_col] = _raw_precios[canal_col].astype(str).str.strip().str.upper()
                p_long = _raw_precios.melt(
                    id_vars=["SKU_PRODUCTO", canal_col],
                    value_vars=mes_cols,
                    var_name="PERIODO_MES",
                    value_name="PRECIO_NETO",
                )
                p_long["PERIODO_MES"] = pd.to_numeric(p_long["PERIODO_MES"], errors="coerce").astype("Int64")
                p_long["PRECIO_NETO"] = pd.to_numeric(p_long["PRECIO_NETO"], errors="coerce")

                # Expand "TIENDA Y ETAIL" into both channels
                rows_p = []
                for _, r in p_long.iterrows():
                    if pd.isna(r["PERIODO_MES"]):
                        continue
                    canal_src = r[canal_col]
                    if "TIENDA" in canal_src and "ETAIL" in canal_src:
                        rows_p.append({"SKU_PRODUCTO": r["SKU_PRODUCTO"], "CANAL_STD": "TIENDA",
                                       "PERIODO_MES": int(r["PERIODO_MES"]), "PRECIO_NETO": r["PRECIO_NETO"]})
                        rows_p.append({"SKU_PRODUCTO": r["SKU_PRODUCTO"], "CANAL_STD": "ETAIL",
                                       "PERIODO_MES": int(r["PERIODO_MES"]), "PRECIO_NETO": r["PRECIO_NETO"]})
                    elif "MAYOR" in canal_src:
                        rows_p.append({"SKU_PRODUCTO": r["SKU_PRODUCTO"], "CANAL_STD": "MAYORISTA",
                                       "PERIODO_MES": int(r["PERIODO_MES"]), "PRECIO_NETO": r["PRECIO_NETO"]})
                    else:
                        rows_p.append({"SKU_PRODUCTO": r["SKU_PRODUCTO"], "CANAL_STD": canal_src,
                                       "PERIODO_MES": int(r["PERIODO_MES"]), "PRECIO_NETO": r["PRECIO_NETO"]})

                p_std = pd.DataFrame(rows_p)
                p_wide = (
                    p_std.pivot_table(index=["SKU_PRODUCTO", "PERIODO_MES"],
                                      columns="CANAL_STD", values="PRECIO_NETO", aggfunc="max")
                    .reset_index()
                )
                rename_map = {}
                if "TIENDA" in p_wide.columns:
                    rename_map["TIENDA"] = "PRECIO_NETO_TIENDA"
                if "ETAIL" in p_wide.columns:
                    rename_map["ETAIL"] = "PRECIO_NETO_ETAIL"
                if "MAYORISTA" in p_wide.columns:
                    rename_map["MAYORISTA"] = "PRECIO_NETO_MAYOR"
                p_wide = p_wide.rename(columns=rename_map)
                precios_mensuales = p_wide  # Will merge by SKU + PERIODO_MES later
            else:
                # === FLAT FORMAT (SKU | PRECIO_TIENDA | PRECIO_ETAIL | ...) ===
                precios = _raw_precios.drop_duplicates("SKU_PRODUCTO")
                cols_to_merge = [c for c in precios.columns if c != "SKU_PRODUCTO" and c != col_sku_p]
                precios = precios[["SKU_PRODUCTO"] + cols_to_merge]

    # 2. Query Snowflake (centralized cache)
    with lottie_spinner("snowflake"):
        stock = cq.stock_proyeccion(conn)
        comex = cq.comex_full(conn)
        maestra = cq.maestra(conn)
        ventas_mtd = cq.ventas_mtd(conn)
        ventas_hist = cq.ventas_mes_anterior(conn)
        ventas_aa = cq.ventas_aa(conn)

    # =========================
    # 3. Process Forecast
    # =========================
    try:
        month_cols = [c for c in forecast.columns if re.fullmatch(r"\d{2}/\d{4}", str(c))]
        if not month_cols:
            st.error("No se encontraron columnas de fecha MM/YYYY en Forecast")
            return None

        f_long = forecast.melt(
            id_vars=[c for c in forecast.columns if c not in month_cols],
            value_vars=month_cols,
            var_name="mes",
            value_name="forecast",
        )
        f_long["PERIODO"] = pd.to_datetime("01/" + f_long["mes"].astype(str), format="%d/%m/%Y", errors="coerce")

        chan_map = {
            "ETAIL": "FORECAST_VENTA_ETAIL",
            "MAYORISTA": "FORECAST_VENTA_MAYOR",
            "MAYOR": "FORECAST_VENTA_MAYOR",
            "TIENDA": "FORECAST_VENTA_MINOR",
            "RETAIL": "FORECAST_VENTA_MINOR",
            "MINOR": "FORECAST_VENTA_MINOR",
        }
        col_canal = next((c for c in f_long.columns if "canal" in c.lower()), None)
        col_sku_fc = next((c for c in f_long.columns if "material" in c.lower() or "sku" in c.lower()), None)

        if not col_canal or not col_sku_fc:
            st.error("Faltan columnas 'canal' o 'sku/material' en Forecast")
            return None

        # Normalize channel values to uppercase to handle mixed casing
        f_long[col_canal] = f_long[col_canal].astype(str).str.strip().str.upper()

        f_sales = f_long[f_long[col_canal].isin(chan_map)].copy()
        f_sales["col"] = f_sales[col_canal].map(chan_map)
        f_sales["SKU_PRODUCTO"] = f_sales[col_sku_fc].astype(str).str.strip().str.upper()

        f_piv = (
            f_sales.pivot_table(index=["SKU_PRODUCTO", "PERIODO"], columns="col", values="forecast", aggfunc="sum")
            .reset_index()
            .fillna(0)
        )

        for c in chan_map.values():
            if c not in f_piv.columns:
                f_piv[c] = 0.0

        f_piv["FORECAST_VENTA_TOTAL"] = (
            f_piv["FORECAST_VENTA_ETAIL"] + f_piv["FORECAST_VENTA_MAYOR"] + f_piv["FORECAST_VENTA_MINOR"]
        )
    except Exception as e:
        st.error(f"Error procesando Forecast: {e}")
        return None

    # =========================
    # 4. Process Purchases
    # =========================
    try:
        compra = norm_cols(compra)
        _cc = compra.columns.tolist()
        col_fecha_c = _find_compra_col(_cc, ["FECHA", "DATE", "PERIODO"])
        col_sku_c = _find_compra_col(_cc, ["MATERIAL", "SKU", "PRODUCTO", "CODIGO"])
        col_orden = _find_compra_col(_cc, ["ORDEN", "UNIDADES", "CANTIDAD", "QTY"])
        col_lead = _find_compra_col(_cc, ["LEAD"])

        if not col_fecha_c or not col_sku_c or not col_orden:
            _miss = [n for n, v in [("Fecha", col_fecha_c), ("SKU", col_sku_c), ("Cantidad", col_orden)] if not v]
            st.error(f"Columnas requeridas no encontradas en Compras: {_miss}. Columnas disponibles: {_cc}")
            return None

        compra[col_fecha_c] = pd.to_datetime(compra[col_fecha_c], errors="coerce")

        if col_lead and col_lead in compra.columns:
            if isinstance(compra[col_lead], pd.DataFrame):
                st.warning(f"Columna Lead Time '{col_lead}' es duplicada. Usando la primera.")
                compra = compra.loc[:, ~compra.columns.duplicated()]
            lead_val = pd.to_numeric(compra[col_lead], errors="coerce").fillna(0)
        else:
            lead_val = 0  # No lead time column → arrival = order date
        compra["FECHA_RECEPCION"] = compra[col_fecha_c] + pd.to_timedelta(lead_val, unit="D")
        compra["PERIODO"] = compra["FECHA_RECEPCION"].dt.to_period("M").dt.to_timestamp()
        compra["SKU_PRODUCTO"] = compra[col_sku_c].astype(str).str.strip().str.upper()
        compra["FORECAST_COMPRA"] = pd.to_numeric(compra[col_orden], errors="coerce").fillna(0)

        c_agg = compra.groupby(["SKU_PRODUCTO", "PERIODO"], as_index=False)["FORECAST_COMPRA"].sum()
    except Exception as e:
        st.error(f"Error procesando Compras: {e}. Cols: {compra.columns.tolist()}")
        return None

    # =========================
    # 5. Process Stock (Snapshot)
    # =========================
    try:
        stock["SKU_PRODUCTO"] = stock["SKU_PRODUCTO"].astype(str).str.strip().str.upper()

        s_piv = (
            stock.pivot_table(index="SKU_PRODUCTO", columns="CANAL_STD", values="STOCK_UNIDADES", aggfunc="sum")
            .fillna(0)
            .reset_index()
        )
        if "CD" not in s_piv.columns:
            s_piv["CD"] = 0
        if "TIENDA" not in s_piv.columns:
            s_piv["TIENDA"] = 0
        s_piv = s_piv.rename(columns={"CD": "STOCK_INICIAL_CD", "TIENDA": "STOCK_INICIAL_TIENDA"})

        if "PERFIL_TIENDAS" in stock.columns:
            perfil = stock.groupby("SKU_PRODUCTO", as_index=False)["PERFIL_TIENDAS"].sum()
        else:
            perfil = pd.DataFrame({"SKU_PRODUCTO": stock["SKU_PRODUCTO"].unique(), "PERFIL_TIENDAS": 0.0})

        s_piv = s_piv.merge(perfil, on="SKU_PRODUCTO", how="left").fillna(0)
    except Exception as e:
        st.error(f"Error procesando Stock: {e}")
        return None

    # =========================
    # 6. Process MTD Sales
    # =========================
    mtd_map = {"MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL"}
    ventas_mtd["CANAL_STD"] = ventas_mtd["COD_CANAL"].map(mtd_map).fillna("TIENDA")

    v_mtd_piv = (
        ventas_mtd.pivot_table(index="SKU_PRODUCTO", columns="CANAL_STD", values="CANTIDAD_MTD", aggfunc="sum")
        .reset_index()
        .fillna(0)
        .rename(columns={"TIENDA": "VENTA_MTD_TIENDA", "ETAIL": "VENTA_MTD_ETAIL", "MAYORISTA": "VENTA_MTD_MAYOR"})
    )

    # Pivot NETO_MTD by channel (for adding real MTD revenue to current month VN_RES)
    neto_mtd_col = "NETO_MTD" if "NETO_MTD" in ventas_mtd.columns else "NETO_TOTAL"
    if neto_mtd_col in ventas_mtd.columns:
        v_mtd_neto_piv = (
            ventas_mtd.pivot_table(index="SKU_PRODUCTO", columns="CANAL_STD", values=neto_mtd_col, aggfunc="sum")
            .reset_index()
            .fillna(0)
            .rename(columns={"TIENDA": "NETO_MTD_TIENDA", "ETAIL": "NETO_MTD_ETAIL", "MAYORISTA": "NETO_MTD_MAYOR"})
        )
    else:
        v_mtd_neto_piv = None

    # =========================
    # 7. Process Comex (ETA)
    # =========================
    try:
        maestra["SKU_PRODUCTO"] = maestra["SKU_PRODUCTO"].astype(str).str.strip().str.upper()
        col_proc = next((c for c in maestra.columns if "PROCEDENCIA" in c), None)

        if "SKU_PRODUCTO" not in comex.columns:
            st.warning("Comex: no SKU_PRODUCTO column found. Trying to find equivalent...")
            sku_col_comex = next((c for c in comex.columns if "SKU" in c or "MATERIAL" in c), None)
            if sku_col_comex:
                comex["SKU_PRODUCTO"] = comex[sku_col_comex].astype(str).str.strip().str.upper()
                pass  # Found equivalent SKU column
            else:
                st.error("Comex: no se encontro columna SKU.")
                eta_agg = pd.DataFrame(columns=["SKU_PRODUCTO", "PERIODO", "ETA"])
                raise ValueError("No SKU column in Comex")

        comex["SKU_PRODUCTO"] = comex["SKU_PRODUCTO"].astype(str).str.strip().str.upper()

        if col_proc:
            comex = comex.merge(
                maestra[["SKU_PRODUCTO", col_proc]].drop_duplicates("SKU_PRODUCTO"), on="SKU_PRODUCTO", how="left"
            )

        def get_lag(proc):
            p = str(proc).upper()
            if "NACIONAL" in p:
                return 7
            if "IMPORT" in p:
                return 14
            return 14

        col_eta = next((c for c in comex.columns if c == "ETA"), None)
        if not col_eta:
            col_eta = next((c for c in comex.columns if "ETA" in c and "RECEP" not in c and "DISP" not in c), None)
        if not col_eta:
            st.warning("No se encontro columna ETA en Comex. Asumiendo ETA = Hoy + 14 dias.")
            comex["ETA_ORIGINAL"] = pd.Timestamp.now() + pd.Timedelta(days=14)
        else:
            comex["ETA_ORIGINAL"] = pd.to_datetime(comex[col_eta], errors="coerce")

        comex["LAG"] = comex[col_proc].apply(get_lag) if col_proc else 14
        lag_series = pd.to_numeric(comex["LAG"], errors="coerce").fillna(14)
        comex["ETA_DISP"] = comex["ETA_ORIGINAL"] + pd.to_timedelta(lag_series, unit="D")
        comex["PERIODO"] = comex["ETA_DISP"].dt.to_period("M").dt.to_timestamp()

        # Prefer CANTIDAD_FINAL_CORREGIDA over CANTIDAD_FINAL
        if "CANTIDAD_FINAL_CORREGIDA" in comex.columns:
            col_cant_final = "CANTIDAD_FINAL_CORREGIDA"
        elif "CANTIDAD_FINAL" in comex.columns:
            col_cant_final = "CANTIDAD_FINAL"
        else:
            col_cant_final = None

        col_cant_rec = next((c for c in comex.columns if "RECEPCIONADA" in c), None)

        c_final = pd.to_numeric(comex[col_cant_final], errors="coerce").fillna(0) if col_cant_final else 0
        c_rec = pd.to_numeric(comex[col_cant_rec], errors="coerce").fillna(0) if col_cant_rec else 0

        comex["PENDIENTE"] = (c_final - c_rec).clip(lower=0)

        # Filter: only rows with valid ETA and positive pending quantity
        comex = comex[comex["ETA_ORIGINAL"].notna() & (comex["PENDIENTE"] > 0)].copy()

        eta_agg = (
            comex.groupby(["SKU_PRODUCTO", "PERIODO"], as_index=False)["PENDIENTE"]
            .sum()
            .rename(columns={"PENDIENTE": "ETA"})
        )

    except Exception as e:
        st.error(f"Error procesando Comex: {e}")
        return None

    # =========================
    # 8. Merge Everything
    # =========================
    df_main = (
        f_piv.merge(c_agg, on=["SKU_PRODUCTO", "PERIODO"], how="outer")
        .merge(eta_agg, on=["SKU_PRODUCTO", "PERIODO"], how="outer")
        .fillna(0)
    )

    # Zero out planned purchases if flag is set (keep ETA from COMEX)
    if excluir_plan_compra and "FORECAST_COMPRA" in df_main.columns:
        df_main["FORECAST_COMPRA"] = 0
        st.info("Plan de compras excluido: simulación solo con stock actual + COMEX confirmado.")

    cols_maestra = ["SKU_PRODUCTO"]
    for c in ["SKU_NOM_PRODUCTO", "LINEA", "SUBLINEA", "AREA", "MARCA",
              "PROCEDENCIA", "COSTO_FOB_USD", "ULTIMO_COSTO", "FACTOR_IMPORTACION",
              "PROVEEDOR", "COD_PROVEEDOR", "MIX_OFICIAL", "MODELO"]:
        if c in maestra.columns:
            cols_maestra.append(c)
    df_main = df_main.merge(maestra[cols_maestra].drop_duplicates("SKU_PRODUCTO"), on="SKU_PRODUCTO", how="left")

    # fillna(0) only on numeric columns to avoid overwriting text dimension columns (AREA, LINEA, etc.)
    def _fillna_numeric(df):
        num_cols = df.select_dtypes(include="number").columns
        df[num_cols] = df[num_cols].fillna(0)
        return df

    df_main = _fillna_numeric(df_main.merge(s_piv, on="SKU_PRODUCTO", how="left"))
    df_main = _fillna_numeric(df_main.merge(v_mtd_piv, on="SKU_PRODUCTO", how="left"))
    if v_mtd_neto_piv is not None:
        df_main = _fillna_numeric(df_main.merge(v_mtd_neto_piv, on="SKU_PRODUCTO", how="left"))

    # Compute simulation demand
    stock_date = pd.to_datetime(stock["FECHA"]).max().normalize()
    PERIODO_ACTUAL = stock_date.to_period("M").to_timestamp()

    df_main["PERIODO"] = pd.to_datetime(df_main["PERIODO"])
    df_main["ES_MES_ACTUAL"] = (df_main["PERIODO"] == PERIODO_ACTUAL).astype(int)

    df_main["DEMANDA_SIM_TIENDA"] = np.where(
        df_main["ES_MES_ACTUAL"] == 1,
        np.maximum(0, df_main["FORECAST_VENTA_MINOR"] - df_main.get("VENTA_MTD_TIENDA", 0)),
        df_main["FORECAST_VENTA_MINOR"],
    )
    df_main["DEMANDA_SIM_ETAIL"] = np.where(
        df_main["ES_MES_ACTUAL"] == 1,
        np.maximum(0, df_main["FORECAST_VENTA_ETAIL"] - df_main.get("VENTA_MTD_ETAIL", 0)),
        df_main["FORECAST_VENTA_ETAIL"],
    )
    df_main["DEMANDA_SIM_MAYOR"] = np.where(
        df_main["ES_MES_ACTUAL"] == 1,
        np.maximum(0, df_main["FORECAST_VENTA_MAYOR"] - df_main.get("VENTA_MTD_MAYOR", 0)),
        df_main["FORECAST_VENTA_MAYOR"],
    )

    # =========================
    # 9. Run Simulation
    # =========================
    df_sim = run_stock_simulation(df_main)

    if df_sim.empty:
        st.warning("No se generaron datos de simulacion. Revisa inputs.")
        return None

    # Merge MTD neto and ES_MES_ACTUAL from df_main to df_sim
    neto_mtd_cols = [c for c in df_main.columns if c.startswith("NETO_MTD_")]
    if neto_mtd_cols:
        mtd_neto_merge = df_main[["SKU_PRODUCTO", "PERIODO"] + neto_mtd_cols].drop_duplicates(
            ["SKU_PRODUCTO", "PERIODO"]
        )
        df_sim = df_sim.merge(mtd_neto_merge, on=["SKU_PRODUCTO", "PERIODO"], how="left")
        for c in neto_mtd_cols:
            df_sim[c] = df_sim[c].fillna(0)

    # =========================
    # 10. Pricing & Valuation
    # =========================
    # 10a. Merge flat prices (if uploaded in flat format)
    if precios is not None:
        df_sim = df_sim.merge(precios, on="SKU_PRODUCTO", how="left")

    # 10b. Period columns (needed for monthly price merge)
    df_sim["PERIODO"] = pd.to_datetime(df_sim["PERIODO"])
    df_sim["PERIODO_ANO"] = df_sim["PERIODO"].dt.year
    df_sim["PERIODO_MES"] = df_sim["PERIODO"].dt.month
    df_sim["ID_MES"] = df_sim["PERIODO"].dt.strftime("%Y%m")
    df_sim["TIPO_DATO"] = "PROYECCION"

    # 10c. Merge monthly prices (if uploaded in monthly format: SKU|CANAL|1|2|...|12)
    if precios_mensuales is not None:
        df_sim = df_sim.merge(precios_mensuales, on=["SKU_PRODUCTO", "PERIODO_MES"], how="left")

    # 10d. Cost — merge from maestra (simulation doesn't carry cost columns)
    # Fallback chain: ULTIMO_COSTO → COSTO_FOB_USD × FACTOR_IMPORTACION × 950 (USD→CLP)
    # FACTOR_IMPORTACION fallback: SKU → SUBLINEA-MARCA → LINEA-MARCA → AREA-MARCA
    #                                   → SUBLINEA → LINEA → AREA
    cost_cols_needed = [c for c in ["ULTIMO_COSTO", "COSTO_FOB_USD", "FACTOR_IMPORTACION"]
                        if c in maestra.columns]
    if cost_cols_needed:
        cost_merge = maestra[["SKU_PRODUCTO"] + cost_cols_needed].drop_duplicates("SKU_PRODUCTO")
        cost_cols_new = [c for c in cost_cols_needed if c not in df_sim.columns]
        if cost_cols_new:
            df_sim = df_sim.merge(cost_merge[["SKU_PRODUCTO"] + cost_cols_new], on="SKU_PRODUCTO", how="left")

    # Parse cost & structure columns
    for cc in ["ULTIMO_COSTO", "COSTO_FOB_USD", "FACTOR_IMPORTACION"]:
        if cc in df_sim.columns:
            df_sim[cc] = pd.to_numeric(df_sim[cc], errors="coerce").fillna(0)

    # --- FACTOR_IMPORTACION fallback by commercial hierarchy ---
    # Build average factor lookup from maestra (only rows with valid factor > 0)
    _has_struct = all(c in maestra.columns for c in ["FACTOR_IMPORTACION", "SUBLINEA", "LINEA", "AREA", "MARCA"])
    if _has_struct:
        _m = maestra.copy()
        _m["FACTOR_IMPORTACION"] = pd.to_numeric(_m["FACTOR_IMPORTACION"], errors="coerce")
        _m_valid = _m[_m["FACTOR_IMPORTACION"] > 0]

        # Pre-compute average factors per hierarchy level
        factor_avg = {}
        hierarchy_levels = [
            ("SUBLINEA_MARCA", ["SUBLINEA", "MARCA"]),
            ("LINEA_MARCA",    ["LINEA", "MARCA"]),
            ("AREA_MARCA",     ["AREA", "MARCA"]),
            ("SUBLINEA",       ["SUBLINEA"]),
            ("LINEA",          ["LINEA"]),
            ("AREA",           ["AREA"]),
        ]
        for level_name, group_cols in hierarchy_levels:
            agg = _m_valid.groupby(group_cols, as_index=False)["FACTOR_IMPORTACION"].mean()
            agg = agg.rename(columns={"FACTOR_IMPORTACION": f"FACTOR_AVG_{level_name}"})
            factor_avg[level_name] = (group_cols, agg)

        # Apply fallback: fill missing FACTOR_IMPORTACION in df_sim
        mask_no_factor = (df_sim["FACTOR_IMPORTACION"].isna()) | (df_sim["FACTOR_IMPORTACION"] <= 0)

        if mask_no_factor.any():
            # Ensure structure columns exist in df_sim
            for sc in ["SUBLINEA", "LINEA", "AREA", "MARCA"]:
                if sc not in df_sim.columns:
                    df_sim[sc] = ""

            df_sim["FACTOR_IMPORTACION_ORIG"] = df_sim["FACTOR_IMPORTACION"].copy()

            for level_name, (group_cols, agg_df) in factor_avg.items():
                still_missing = (df_sim["FACTOR_IMPORTACION"].isna()) | (df_sim["FACTOR_IMPORTACION"] <= 0)
                if not still_missing.any():
                    break
                col_avg = f"FACTOR_AVG_{level_name}"
                df_sim = df_sim.merge(agg_df, on=group_cols, how="left")
                fill_mask = still_missing & df_sim[col_avg].notna() & (df_sim[col_avg] > 0)
                df_sim.loc[fill_mask, "FACTOR_IMPORTACION"] = df_sim.loc[fill_mask, col_avg]
                df_sim.drop(columns=[col_avg], inplace=True)

    # --- Primary cost: ULTIMO_COSTO ---
    df_sim["COSTO_UNITARIO"] = df_sim.get("ULTIMO_COSTO", pd.Series(0, index=df_sim.index))

    # Fallback: where ULTIMO_COSTO is 0 or NaN, use COSTO_FOB_USD × FACTOR_IMPORTACION × TC
    TC_USD_CLP = st.session_state.get("tc_usd_clp", 950)
    mask_sin_costo = (df_sim["COSTO_UNITARIO"].isna()) | (df_sim["COSTO_UNITARIO"] <= 0)
    if "COSTO_FOB_USD" in df_sim.columns and "FACTOR_IMPORTACION" in df_sim.columns:
        costo_landed = df_sim["COSTO_FOB_USD"] * df_sim["FACTOR_IMPORTACION"] * TC_USD_CLP
        df_sim.loc[mask_sin_costo, "COSTO_UNITARIO"] = costo_landed[mask_sin_costo]
    elif "COSTO_FOB_USD" in df_sim.columns:
        costo_fob_clp = df_sim["COSTO_FOB_USD"] * TC_USD_CLP
        df_sim.loc[mask_sin_costo, "COSTO_UNITARIO"] = costo_fob_clp[mask_sin_costo]

    df_sim["COSTO_UNITARIO"] = df_sim["COSTO_UNITARIO"].fillna(0)

    # Track cost origin
    factor_was_imputed = (
        df_sim.get("FACTOR_IMPORTACION_ORIG", df_sim.get("FACTOR_IMPORTACION", 0)) <= 0
    ) & (df_sim.get("FACTOR_IMPORTACION", 0) > 0)

    df_sim["ORIGEN_COSTO"] = np.where(
        df_sim.get("ULTIMO_COSTO", 0) > 0, "ULTIMO_COSTO",
        np.where(
            df_sim["COSTO_UNITARIO"] > 0,
            np.where(factor_was_imputed, "LANDED_FACTOR_IMPUTADO", "LANDED_CALC"),
            "SIN_COSTO"
        )
    )
    # Flag SKUs with zero cost (contaminates margin calculations)
    df_sim["FLAG_SIN_COSTO"] = (
        (df_sim["ORIGEN_COSTO"] == "SIN_COSTO") | (df_sim["COSTO_UNITARIO"] <= 0)
    )

    # Clean up temp column
    if "FACTOR_IMPORTACION_ORIG" in df_sim.columns:
        df_sim.drop(columns=["FACTOR_IMPORTACION_ORIG"], inplace=True)

    # 10e. MTD prices (current month partial sales average)
    neto_col = "NETO_MTD" if "NETO_MTD" in ventas_mtd.columns else "NETO_TOTAL"
    if (
        not ventas_mtd.empty
        and neto_col in ventas_mtd.columns
        and "CANTIDAD_MTD" in ventas_mtd.columns
    ):
        ventas_mtd["PRECIO_PROMEDIO"] = np.where(
            ventas_mtd["CANTIDAD_MTD"] > 0,
            ventas_mtd[neto_col] / ventas_mtd["CANTIDAD_MTD"],
            0,
        )
        mtd_prices = (
            ventas_mtd.pivot_table(index="SKU_PRODUCTO", columns="CANAL_STD", values="PRECIO_PROMEDIO", aggfunc="mean")
            .reset_index()
        )
        mtd_prices.columns = ["SKU_PRODUCTO"] + [f"PRECIO_MTD_{c}" for c in mtd_prices.columns[1:]]
        df_sim = df_sim.merge(mtd_prices, on="SKU_PRODUCTO", how="left")

    # 10f. Previous month prices (additional fallback from ventas_hist)
    if not ventas_hist.empty:
        try:
            hist_price_map = {"MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL"}
            vh_price = ventas_hist.copy()
            vh_price["CANAL_STD"] = vh_price["COD_CANAL"].map(hist_price_map).fillna("TIENDA")
            vh_price["PRECIO_PROMEDIO"] = np.where(
                vh_price["CANTIDAD_MES"] > 0,
                vh_price["NETO_MES"] / vh_price["CANTIDAD_MES"],
                0,
            )
            hist_prices = (
                vh_price.pivot_table(
                    index="SKU_PRODUCTO", columns="CANAL_STD",
                    values="PRECIO_PROMEDIO", aggfunc="mean"
                ).reset_index()
            )
            hist_prices.columns = ["SKU_PRODUCTO"] + [f"PRECIO_MES_ANT_{c}" for c in hist_prices.columns[1:]]
            df_sim = df_sim.merge(hist_prices, on="SKU_PRODUCTO", how="left")
        except Exception:
            pass

    # 10g. Price fallback logic: PLANILLA -> MTD -> MES_ANTERIOR -> CROSS_CHANNEL -> 0
    p_tienda = next(
        (c for c in df_sim.columns if "PRECIO" in c and ("TIENDA" in c or "MINOR" in c)
         and "MTD" not in c and "MES_ANT" not in c and "USADO" not in c), None
    )
    p_etail = next(
        (c for c in df_sim.columns if "PRECIO" in c and "ETAIL" in c
         and "MTD" not in c and "MES_ANT" not in c and "USADO" not in c), None
    )
    p_mayor = next(
        (c for c in df_sim.columns if "PRECIO" in c and "MAYOR" in c
         and "MTD" not in c and "MES_ANT" not in c and "USADO" not in c), None
    )

    for canal, col_planilla, col_mtd, col_hist in [
        ("TIENDA", p_tienda, "PRECIO_MTD_TIENDA", "PRECIO_MES_ANT_TIENDA"),
        ("ETAIL", p_etail, "PRECIO_MTD_ETAIL", "PRECIO_MES_ANT_ETAIL"),
        ("MAYOR", p_mayor, "PRECIO_MTD_MAYORISTA", "PRECIO_MES_ANT_MAYORISTA"),
    ]:
        precio_plan = df_sim[col_planilla].fillna(0) if col_planilla and col_planilla in df_sim.columns else 0.0
        precio_mtd = df_sim[col_mtd].fillna(0) if col_mtd in df_sim.columns else 0.0
        precio_hist = df_sim[col_hist].fillna(0) if col_hist in df_sim.columns else 0.0

        df_sim[f"PRECIO_USADO_{canal}"] = np.where(
            precio_plan > 0, precio_plan,
            np.where(precio_mtd > 0, precio_mtd,
                np.where(precio_hist > 0, precio_hist, 0.0)))

        df_sim[f"ORIGEN_PRECIO_{canal}"] = np.where(
            precio_plan > 0, "PLANILLA",
            np.where(precio_mtd > 0, "PROMEDIO_MTD",
                np.where(precio_hist > 0, "PROMEDIO_MES_ANT", "SIN_PRECIO")))

    # 10h. Cross-channel fallback: if a channel has no price, use another channel's price
    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
        otros = [c for c in ["TIENDA", "ETAIL", "MAYOR"] if c != canal]
        mask_sin = df_sim[f"PRECIO_USADO_{canal}"] == 0
        for otro in otros:
            precio_otro = df_sim.loc[mask_sin, f"PRECIO_USADO_{otro}"]
            updated = mask_sin & (precio_otro > 0)
            df_sim.loc[updated, f"PRECIO_USADO_{canal}"] = df_sim.loc[updated, f"PRECIO_USADO_{otro}"]
            df_sim.loc[updated, f"ORIGEN_PRECIO_{canal}"] = f"CROSS_{otro}"
            mask_sin = df_sim[f"PRECIO_USADO_{canal}"] == 0

    # Unrestricted vs restricted units
    df_sim["UND_IRR_TIENDA"] = df_sim["DEMANDA_SIM_TIENDA"]
    df_sim["UND_IRR_ETAIL"] = df_sim["DEMANDA_SIM_ETAIL"]
    df_sim["UND_IRR_MAYOR"] = df_sim["DEMANDA_SIM_MAYOR"]

    # Valued sales (unrestricted)
    df_sim["VN_IRR_TIENDA"] = df_sim["UND_IRR_TIENDA"] * df_sim["PRECIO_USADO_TIENDA"]
    df_sim["VN_IRR_ETAIL"] = df_sim["UND_IRR_ETAIL"] * df_sim["PRECIO_USADO_ETAIL"]
    df_sim["VN_IRR_MAYOR"] = df_sim["UND_IRR_MAYOR"] * df_sim["PRECIO_USADO_MAYOR"]
    df_sim["VN_IRR_TOTAL"] = df_sim["VN_IRR_TIENDA"] + df_sim["VN_IRR_ETAIL"] + df_sim["VN_IRR_MAYOR"]

    # Valued sales (restricted)
    # For the current month: VN_RES = real MTD neto (already sold) + simulated remaining * price
    # For future months: VN_RES = fulfilled units * price
    es_actual = (df_sim["PERIODO"] == PERIODO_ACTUAL).astype(int)

    for canal, neto_col_name in [
        ("TIENDA", "NETO_MTD_TIENDA"),
        ("ETAIL", "NETO_MTD_ETAIL"),
        ("MAYOR", "NETO_MTD_MAYOR"),
    ]:
        vn_sim = df_sim[f"VENTA_FUL_{canal}_UND"] * df_sim[f"PRECIO_USADO_{canal}"]
        vn_mtd_real = df_sim[neto_col_name] if neto_col_name in df_sim.columns else 0.0
        df_sim[f"VN_RES_{canal}"] = np.where(
            es_actual == 1,
            vn_mtd_real + vn_sim,  # Current month: real MTD revenue + simulated remaining
            vn_sim,                 # Future months: only simulated
        )

    df_sim["VN_RES_TOTAL"] = df_sim["VN_RES_TIENDA"] + df_sim["VN_RES_ETAIL"] + df_sim["VN_RES_MAYOR"]

    # ── COGS, APORTE, MARGEN por canal (restricto) ──
    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
        df_sim[f"COGS_RES_{canal}"] = df_sim[f"VENTA_FUL_{canal}_UND"] * df_sim["COSTO_UNITARIO"]
        df_sim[f"APORTE_RES_{canal}"] = df_sim[f"VN_RES_{canal}"] - df_sim[f"COGS_RES_{canal}"]
        df_sim[f"MARGEN_RES_{canal}"] = np.where(
            df_sim[f"VN_RES_{canal}"] > 0,
            df_sim[f"APORTE_RES_{canal}"] / df_sim[f"VN_RES_{canal}"],
            0.0,
        )

    # Totales restrictos (suma de canales)
    df_sim["COGS_RES_TOTAL"] = df_sim["COGS_RES_TIENDA"] + df_sim["COGS_RES_ETAIL"] + df_sim["COGS_RES_MAYOR"]
    df_sim["APORTE_RES_TOTAL"] = df_sim["VN_RES_TOTAL"] - df_sim["COGS_RES_TOTAL"]
    df_sim["MARGEN_RES_TOTAL"] = np.where(
        df_sim["VN_RES_TOTAL"] > 0, df_sim["APORTE_RES_TOTAL"] / df_sim["VN_RES_TOTAL"], 0.0
    )

    # ── COGS, APORTE, MARGEN irrestrictos por canal ──
    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
        df_sim[f"COGS_IRR_{canal}"] = df_sim[f"UND_IRR_{canal}"] * df_sim["COSTO_UNITARIO"]
        df_sim[f"APORTE_IRR_{canal}"] = df_sim[f"VN_IRR_{canal}"] - df_sim[f"COGS_IRR_{canal}"]
        df_sim[f"MARGEN_IRR_{canal}"] = np.where(
            df_sim[f"VN_IRR_{canal}"] > 0,
            df_sim[f"APORTE_IRR_{canal}"] / df_sim[f"VN_IRR_{canal}"],
            0.0,
        )

    # Totales irrestrictos
    df_sim["COGS_IRR_TOTAL"] = df_sim["COGS_IRR_TIENDA"] + df_sim["COGS_IRR_ETAIL"] + df_sim["COGS_IRR_MAYOR"]
    df_sim["APORTE_IRR_TOTAL"] = df_sim["VN_IRR_TOTAL"] - df_sim["COGS_IRR_TOTAL"]
    df_sim["MARGEN_IRR_TOTAL"] = np.where(
        df_sim["VN_IRR_TOTAL"] > 0, df_sim["APORTE_IRR_TOTAL"] / df_sim["VN_IRR_TOTAL"], 0.0
    )

    # KPIs & adjustments
    df_sim["LOST_SALES_TOTAL"] = df_sim.get("LOST_SALES_TIENDA", 0) + df_sim.get("LOST_SALES_CD", 0)

    # Ensure per-channel lost sales exist (fallback pro-rata if missing)
    if "LOST_SALES_ETAIL" not in df_sim.columns:
        _dem_cd = pd.to_numeric(df_sim.get("DEMANDA_SIM_ETAIL", 0), errors="coerce").fillna(0) + \
                  pd.to_numeric(df_sim.get("DEMANDA_SIM_MAYOR", 0), errors="coerce").fillna(0)
        _ls_cd = pd.to_numeric(df_sim.get("LOST_SALES_CD", 0), errors="coerce").fillna(0)
        df_sim["LOST_SALES_ETAIL"] = np.where(_dem_cd > 0, _ls_cd * (df_sim.get("DEMANDA_SIM_ETAIL", 0) / _dem_cd), 0.0)
        df_sim["LOST_SALES_MAYOR"] = np.where(_dem_cd > 0, _ls_cd * (df_sim.get("DEMANDA_SIM_MAYOR", 0) / _dem_cd), 0.0)

    # Financial Lost Sales per channel
    for _lc in ["TIENDA", "ETAIL", "MAYOR"]:
        _ls_col = f"LOST_SALES_{_lc}"
        _pr_col = f"PRECIO_USADO_{_lc}"
        if _ls_col not in df_sim.columns:
            df_sim[_ls_col] = 0.0
        _ls_und = pd.to_numeric(df_sim[_ls_col], errors="coerce").fillna(0)
        _pr = pd.to_numeric(df_sim.get(_pr_col, 0), errors="coerce").fillna(0)
        _cu = pd.to_numeric(df_sim.get("COSTO_UNITARIO", 0), errors="coerce").fillna(0)
        df_sim[f"VN_LOST_{_lc}"] = _ls_und * _pr
        df_sim[f"COGS_LOST_{_lc}"] = _ls_und * _cu
        df_sim[f"APORTE_LOST_{_lc}"] = df_sim[f"VN_LOST_{_lc}"] - df_sim[f"COGS_LOST_{_lc}"]
        df_sim[f"MARGEN_LOST_{_lc}"] = np.where(
            df_sim[f"VN_LOST_{_lc}"] > 0,
            df_sim[f"APORTE_LOST_{_lc}"] / df_sim[f"VN_LOST_{_lc}"],
            0.0,
        )
    df_sim["VN_LOST_TOTAL"] = df_sim["VN_LOST_TIENDA"] + df_sim["VN_LOST_ETAIL"] + df_sim["VN_LOST_MAYOR"]
    df_sim["COGS_LOST_TOTAL"] = df_sim["COGS_LOST_TIENDA"] + df_sim["COGS_LOST_ETAIL"] + df_sim["COGS_LOST_MAYOR"]
    df_sim["APORTE_LOST_TOTAL"] = df_sim["VN_LOST_TOTAL"] - df_sim["COGS_LOST_TOTAL"]
    df_sim["MARGEN_LOST_TOTAL"] = np.where(
        df_sim["VN_LOST_TOTAL"] > 0, df_sim["APORTE_LOST_TOTAL"] / df_sim["VN_LOST_TOTAL"], 0.0
    )

    # INSTOCK_CIA ya viene calculado desde la simulación (mean de 0/1 diarios)
    df_sim["QUIEBRE_POR_PERFIL"] = np.where(df_sim["DEFICIT_PERFIL_UND"] > 0, 1, 0)

    # Suggested purchase adjustment
    deficit_sku = df_sim.groupby("SKU_PRODUCTO")["DEFICIT_PERFIL_UND"].transform("sum")
    df_sim["ES_PRIMER_MES"] = (
        df_sim["PERIODO"] == df_sim.groupby("SKU_PRODUCTO")["PERIODO"].transform("min")
    ).astype(int)
    df_sim["AJUSTE_COMPRA_SUGERIDO_UND"] = np.where(df_sim["ES_PRIMER_MES"] == 1, deficit_sku, 0.0)
    df_sim["VN_INCREMENTAL_PERFIL"] = df_sim["AJUSTE_COMPRA_SUGERIDO_UND"] * df_sim["PRECIO_USADO_TIENDA"]

    # MOI coverage
    df_sim = df_sim.sort_values(["SKU_PRODUCTO", "PERIODO"])
    df_sim["DEMANDA_TOTAL"] = df_sim["DEMANDA_SIM_TIENDA"] + df_sim["DEMANDA_SIM_ETAIL"] + df_sim["DEMANDA_SIM_MAYOR"]
    df_sim["DEMANDA_AVG_3M"] = df_sim.groupby("SKU_PRODUCTO")["DEMANDA_TOTAL"].transform(
        lambda x: x.rolling(3, min_periods=1).mean()
    )
    df_sim["MOI_COBERTURA"] = np.where(
        df_sim["DEMANDA_AVG_3M"] > 0, df_sim["STOCK_FINAL_TOTAL"] / df_sim["DEMANDA_AVG_3M"], 999
    )

    # Break detection
    df_sim["TIENE_QUIEBRE"] = (df_sim["STOCK_FINAL_TOTAL"] < 0.1).astype(int)
    quiebre_mes = df_sim[df_sim["TIENE_QUIEBRE"] == 1].groupby("SKU_PRODUCTO")["PERIODO"].min()
    df_sim = df_sim.merge(quiebre_mes.rename("MES_QUIEBRE"), on="SKU_PRODUCTO", how="left")
    df_sim["MESES_HASTA_QUIEBRE"] = np.where(
        df_sim["MES_QUIEBRE"].notna(),
        ((df_sim["MES_QUIEBRE"] - PERIODO_ACTUAL).dt.days / 30).round(1),
        999,
    )

    # Forecast vs real warning
    if not ventas_mtd.empty and "CANTIDAD_MTD" in ventas_mtd.columns and "FORECAST_VENTA_TOTAL" in df_sim.columns:
        jan_sales = ventas_mtd.groupby("SKU_PRODUCTO")["CANTIDAD_MTD"].sum().reset_index()
        jan_sales.columns = ["SKU_PRODUCTO", "JAN_SALES"]

        feb_mar_fc = (
            df_sim[
                (df_sim["PERIODO"].dt.month.isin([2, 3])) & (df_sim["TIPO_DATO"] == "PROYECCION")
            ]
            .groupby("SKU_PRODUCTO")["FORECAST_VENTA_TOTAL"]
            .mean()
            .reset_index()
        )
        feb_mar_fc.columns = ["SKU_PRODUCTO", "FEB_MAR_FC_AVG"]

        df_sim = df_sim.merge(jan_sales, on="SKU_PRODUCTO", how="left")
        df_sim = df_sim.merge(feb_mar_fc, on="SKU_PRODUCTO", how="left")

        df_sim["WARNING_FC_REAL"] = np.where(
            (df_sim["FEB_MAR_FC_AVG"] < df_sim["JAN_SALES"] * 0.5) & (df_sim["JAN_SALES"] > 10),
            "LOW_FORECAST",
            np.where(
                (df_sim["FEB_MAR_FC_AVG"] > df_sim["JAN_SALES"] * 2) & (df_sim["JAN_SALES"] > 10),
                "HIGH_FORECAST",
                "",
            ),
        )
    else:
        df_sim["WARNING_FC_REAL"] = ""

    # Column ordering
    cols_order = [
        "SKU_PRODUCTO", "PERIODO", "PERIODO_ANO", "PERIODO_MES", "ID_MES", "TIPO_DATO",
        "FORECAST_COMPRA", "ETA",
        "STOCK_INICIAL_CD", "STOCK_INICIAL_TIENDA", "STOCK_INICIAL_TOTAL", "STOCK_DISPONIBLE",
        "STOCK_FINAL_CD", "STOCK_FINAL_TIENDA", "STOCK_FINAL_TOTAL",
        "MOI_COBERTURA", "MES_QUIEBRE", "MESES_HASTA_QUIEBRE",
        "PERFIL_TIENDAS", "NECESIDAD_TIENDA", "CARGA_REAL",
        "DEFICIT_PERFIL_UND", "QUIEBRE_POR_PERFIL", "AJUSTE_COMPRA_SUGERIDO_UND", "VN_INCREMENTAL_PERFIL",
        "DEMANDA_SIM_TIENDA", "DEMANDA_SIM_ETAIL", "DEMANDA_SIM_MAYOR", "DEMANDA_TOTAL",
        "VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND", "VENTA_FUL_MAYOR_UND",
        "LOST_SALES_TIENDA", "LOST_SALES_ETAIL", "LOST_SALES_MAYOR", "LOST_SALES_CD", "LOST_SALES_TOTAL",
        "INSTOCK_CD", "INSTOCK_TIENDA", "INSTOCK_CIA",
        "INSTOCK_DIAS_CD", "INSTOCK_DIAS_TIENDA", "INSTOCK_DIAS_CIA", "DIAS_MES",
        "COSTO_UNITARIO",
        "PRECIO_NETO_TIENDA", "PRECIO_NETO_ETAIL", "PRECIO_NETO_MAYOR",
        "PRECIO_MTD_TIENDA", "PRECIO_MTD_ETAIL", "PRECIO_MTD_MAYORISTA",
        "PRECIO_USADO_TIENDA", "PRECIO_USADO_ETAIL", "PRECIO_USADO_MAYOR",
        "ORIGEN_PRECIO_TIENDA", "ORIGEN_PRECIO_ETAIL", "ORIGEN_PRECIO_MAYOR",
        # Financiero restricto por canal
        "VN_RES_TIENDA", "VN_RES_ETAIL", "VN_RES_MAYOR", "VN_RES_TOTAL",
        "COGS_RES_TIENDA", "COGS_RES_ETAIL", "COGS_RES_MAYOR", "COGS_RES_TOTAL",
        "APORTE_RES_TIENDA", "APORTE_RES_ETAIL", "APORTE_RES_MAYOR", "APORTE_RES_TOTAL",
        "MARGEN_RES_TIENDA", "MARGEN_RES_ETAIL", "MARGEN_RES_MAYOR", "MARGEN_RES_TOTAL",
        # Financiero irrestricto por canal (potencial)
        "VN_IRR_TIENDA", "VN_IRR_ETAIL", "VN_IRR_MAYOR", "VN_IRR_TOTAL",
        "COGS_IRR_TIENDA", "COGS_IRR_ETAIL", "COGS_IRR_MAYOR", "COGS_IRR_TOTAL",
        "APORTE_IRR_TIENDA", "APORTE_IRR_ETAIL", "APORTE_IRR_MAYOR", "APORTE_IRR_TOTAL",
        "MARGEN_IRR_TIENDA", "MARGEN_IRR_ETAIL", "MARGEN_IRR_MAYOR", "MARGEN_IRR_TOTAL",
        # Financiero venta perdida (lost sales valorizado)
        "VN_LOST_TIENDA", "VN_LOST_ETAIL", "VN_LOST_MAYOR", "VN_LOST_TOTAL",
        "COGS_LOST_TIENDA", "COGS_LOST_ETAIL", "COGS_LOST_MAYOR", "COGS_LOST_TOTAL",
        "APORTE_LOST_TIENDA", "APORTE_LOST_ETAIL", "APORTE_LOST_MAYOR", "APORTE_LOST_TOTAL",
        "MARGEN_LOST_TIENDA", "MARGEN_LOST_ETAIL", "MARGEN_LOST_MAYOR", "MARGEN_LOST_TOTAL",
        "WARNING_FC_REAL",
        "SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA", "PROCEDENCIA",
        "PROVEEDOR", "COD_PROVEEDOR", "MIX_OFICIAL", "MODELO",
        "ORIGEN_COSTO", "FACTOR_IMPORTACION", "COSTO_FOB_USD", "ULTIMO_COSTO", "FLAG_SIN_COSTO",
    ]

    # ============================================================
    # 11. PRIOR YEAR (Ano Anterior) DATA — YoY comparison
    # ============================================================
    if not ventas_aa.empty and "SKU_PRODUCTO" in ventas_aa.columns:
        try:
            # Map channels
            aa_chan_map = {"MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL"}
            ventas_aa["CANAL_STD"] = ventas_aa["COD_CANAL"].map(aa_chan_map).fillna("TIENDA")
            ventas_aa["PERIODO"] = pd.to_datetime(ventas_aa["PERIODO"])
            ventas_aa["MES"] = ventas_aa["PERIODO"].dt.month

            for c_num in ["CANTIDAD_AA", "NETO_AA", "APORTE_AA"]:
                if c_num in ventas_aa.columns:
                    ventas_aa[c_num] = pd.to_numeric(ventas_aa[c_num], errors="coerce").fillna(0)

            # Annual aggregation by SKU x Channel
            aa_annual = ventas_aa.groupby(["SKU_PRODUCTO", "CANAL_STD"]).agg(
                CANTIDAD_AA=("CANTIDAD_AA", "sum"),
                NETO_AA=("NETO_AA", "sum"),
                APORTE_AA=("APORTE_AA", "sum"),
            ).reset_index()

            # Pivot by channel and merge to df_sim
            for canal_std, suffix in [("TIENDA", "TIENDA"), ("ETAIL", "ETAIL"), ("MAYORISTA", "MAYOR")]:
                canal_data = aa_annual[aa_annual["CANAL_STD"] == canal_std].copy()
                if canal_data.empty:
                    continue
                canal_data = canal_data.rename(columns={
                    "CANTIDAD_AA": f"AA_UND_{suffix}",
                    "NETO_AA": f"AA_VN_{suffix}",
                    "APORTE_AA": f"AA_APORTE_{suffix}",
                })[["SKU_PRODUCTO", f"AA_UND_{suffix}", f"AA_VN_{suffix}", f"AA_APORTE_{suffix}"]]
                df_sim = df_sim.merge(canal_data, on="SKU_PRODUCTO", how="left")

            # Fill NaN and compute totals
            aa_cols = [c for c in df_sim.columns if c.startswith("AA_")]
            df_sim[aa_cols] = df_sim[aa_cols].fillna(0)

            df_sim["AA_VN_TOTAL"] = (
                df_sim.get("AA_VN_TIENDA", 0) + df_sim.get("AA_VN_ETAIL", 0) + df_sim.get("AA_VN_MAYOR", 0)
            )
            df_sim["AA_UND_TOTAL"] = (
                df_sim.get("AA_UND_TIENDA", 0) + df_sim.get("AA_UND_ETAIL", 0) + df_sim.get("AA_UND_MAYOR", 0)
            )
            df_sim["AA_APORTE_TOTAL"] = (
                df_sim.get("AA_APORTE_TIENDA", 0) + df_sim.get("AA_APORTE_ETAIL", 0) + df_sim.get("AA_APORTE_MAYOR", 0)
            )

            # Monthly breakdown for dashboard (save to session_state)
            aa_monthly = ventas_aa.groupby(["SKU_PRODUCTO", "CANAL_STD", "MES"]).agg(
                CANTIDAD_AA=("CANTIDAD_AA", "sum"),
                NETO_AA=("NETO_AA", "sum"),
                APORTE_AA=("APORTE_AA", "sum"),
            ).reset_index()
            st.session_state["aa_monthly"] = aa_monthly

            # --- Add SKUs from prior year NOT in the forecast ---
            forecast_skus = set(df_sim["SKU_PRODUCTO"].unique())
            aa_all_skus = set(ventas_aa["SKU_PRODUCTO"].unique())
            missing_skus = aa_all_skus - forecast_skus

            if missing_skus:
                # Build one row per missing SKU with AA data
                missing_aa = aa_annual[aa_annual["SKU_PRODUCTO"].isin(missing_skus)].copy()

                missing_rows_list = []
                for sku in missing_skus:
                    sku_data = missing_aa[missing_aa["SKU_PRODUCTO"] == sku]
                    row = {
                        "SKU_PRODUCTO": sku,
                        "TIPO_DATO": "SOLO_AA",
                        "PERIODO": PERIODO_ACTUAL,
                    }
                    for _, r in sku_data.iterrows():
                        canal = r["CANAL_STD"]
                        suffix = {"TIENDA": "TIENDA", "ETAIL": "ETAIL", "MAYORISTA": "MAYOR"}.get(canal, canal)
                        row[f"AA_VN_{suffix}"] = r["NETO_AA"]
                        row[f"AA_UND_{suffix}"] = r["CANTIDAD_AA"]
                        row[f"AA_APORTE_{suffix}"] = r["APORTE_AA"]

                    row["AA_VN_TOTAL"] = sum(row.get(f"AA_VN_{s}", 0) for s in ["TIENDA", "ETAIL", "MAYOR"])
                    row["AA_UND_TOTAL"] = sum(row.get(f"AA_UND_{s}", 0) for s in ["TIENDA", "ETAIL", "MAYOR"])
                    row["AA_APORTE_TOTAL"] = sum(row.get(f"AA_APORTE_{s}", 0) for s in ["TIENDA", "ETAIL", "MAYOR"])
                    missing_rows_list.append(row)

                if missing_rows_list:
                    df_missing = pd.DataFrame(missing_rows_list)
                    # Merge maestra for structure columns
                    df_missing = df_missing.merge(
                        maestra[cols_maestra].drop_duplicates("SKU_PRODUCTO"),
                        on="SKU_PRODUCTO", how="left"
                    )
                    # Align schema
                    for c in df_sim.columns:
                        if c not in df_missing.columns:
                            if pd.api.types.is_datetime64_any_dtype(df_sim[c]):
                                df_missing[c] = pd.NaT
                            elif pd.api.types.is_numeric_dtype(df_sim[c]):
                                df_missing[c] = 0.0
                            else:
                                df_missing[c] = ""
                    df_missing = df_missing[[c for c in df_sim.columns if c in df_missing.columns]].copy()
                    df_sim = pd.concat([df_sim, df_missing], ignore_index=True)

        except Exception as e:
            st.warning(f"No se pudo integrar datos ano anterior: {e}")

    # Update cols_order with AA columns
    cols_order.extend([
        "AA_VN_TIENDA", "AA_VN_ETAIL", "AA_VN_MAYOR", "AA_VN_TOTAL",
        "AA_UND_TIENDA", "AA_UND_ETAIL", "AA_UND_MAYOR", "AA_UND_TOTAL",
        "AA_APORTE_TIENDA", "AA_APORTE_ETAIL", "AA_APORTE_MAYOR", "AA_APORTE_TOTAL",
    ])

    final_cols = [c for c in cols_order if c in df_sim.columns]
    extra_cols = [c for c in df_sim.columns if c not in final_cols]

    # --- Integrate historical sales as rows (mes anterior completo) ---
    # ventas_hist already loaded at the top with the other Snowflake queries
    if not ventas_hist.empty and "SKU_PRODUCTO" in ventas_hist.columns:
        try:
            # Periodo historico = mes anterior (enero si hoy es febrero)
            PERIODO_HISTORICO = PERIODO_ACTUAL - pd.DateOffset(months=1)

            # Mapear canales
            hist_mtd_map = {"MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL"}
            ventas_hist["CANAL_STD"] = ventas_hist["COD_CANAL"].map(hist_mtd_map).fillna("TIENDA")

            hist_agg = ventas_hist.groupby("SKU_PRODUCTO").agg(
                {"CANTIDAD_MES": "sum", "NETO_MES": "sum"}
            ).reset_index()

            hist_agg = hist_agg.merge(
                maestra[cols_maestra].drop_duplicates("SKU_PRODUCTO"), on="SKU_PRODUCTO", how="left"
            )

            h_costo_col = "ULTIMO_COSTO" if "ULTIMO_COSTO" in hist_agg.columns else "COSTO_FOB_USD"
            if h_costo_col in hist_agg.columns:
                hist_agg["COSTO_UNITARIO"] = pd.to_numeric(hist_agg[h_costo_col], errors="coerce").fillna(0)
            else:
                hist_agg["COSTO_UNITARIO"] = 0.0

            hist_rows = pd.DataFrame({
                "SKU_PRODUCTO": hist_agg["SKU_PRODUCTO"],
                "PERIODO": PERIODO_HISTORICO,
                "TIPO_DATO": "HISTORICO",
                "VENTA_FUL_TIENDA_UND": 0.0,
                "VENTA_FUL_ETAIL_UND": 0.0,
                "VENTA_FUL_MAYOR_UND": 0.0,
                "VN_RES_TIENDA": 0.0,
                "VN_RES_ETAIL": 0.0,
                "VN_RES_MAYOR": 0.0,
                "VN_RES_TOTAL": hist_agg["NETO_MES"],
                "COSTO_UNITARIO": hist_agg["COSTO_UNITARIO"],
            })

            for col in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                        "PROCEDENCIA", "MIX_OFICIAL", "MODELO", "PROVEEDOR", "COD_PROVEEDOR"]:
                if col in hist_agg.columns:
                    hist_rows[col] = hist_agg[col].values

            # Desglosar por canal
            for canal_std, col_suffix in [("TIENDA", "TIENDA"), ("ETAIL", "ETAIL"), ("MAYORISTA", "MAYOR")]:
                canal_data = ventas_hist[ventas_hist["CANAL_STD"] == canal_std]
                if not canal_data.empty:
                    canal_agg = canal_data.groupby("SKU_PRODUCTO").agg(
                        {"CANTIDAD_MES": "sum", "NETO_MES": "sum"}
                    ).reset_index()
                    hist_rows = hist_rows.merge(
                        canal_agg.rename(columns={
                            "CANTIDAD_MES": f"VENTA_FUL_{col_suffix}_UND_TEMP",
                            "NETO_MES": f"VN_RES_{col_suffix}_TEMP",
                        }),
                        on="SKU_PRODUCTO",
                        how="left",
                    )
                    hist_rows[f"VENTA_FUL_{col_suffix}_UND"] = hist_rows.get(
                        f"VENTA_FUL_{col_suffix}_UND_TEMP", 0
                    ).fillna(0)
                    hist_rows[f"VN_RES_{col_suffix}"] = hist_rows.get(
                        f"VN_RES_{col_suffix}_TEMP", 0
                    ).fillna(0)
                    hist_rows = hist_rows.drop(
                        columns=[c for c in hist_rows.columns if "_TEMP" in c], errors="ignore"
                    )

            hist_rows["COGS_RES_TOTAL"] = hist_agg["CANTIDAD_MES"] * hist_rows["COSTO_UNITARIO"]
            hist_rows["APORTE_RES_TOTAL"] = hist_rows["VN_RES_TOTAL"] - hist_rows["COGS_RES_TOTAL"]
            hist_rows["MARGEN_RES_TOTAL"] = np.where(
                hist_rows["VN_RES_TOTAL"] > 0, hist_rows["APORTE_RES_TOTAL"] / hist_rows["VN_RES_TOTAL"], 0.0
            )

            # Align schema
            for c in df_sim.columns:
                if c not in hist_rows.columns:
                    if pd.api.types.is_datetime64_any_dtype(df_sim[c]):
                        hist_rows[c] = pd.NaT
                    elif pd.api.types.is_numeric_dtype(df_sim[c]):
                        hist_rows[c] = 0.0
                    else:
                        hist_rows[c] = np.nan

            hist_rows = hist_rows[df_sim.columns].copy()
            for c in df_sim.columns:
                if pd.api.types.is_datetime64_any_dtype(df_sim[c]):
                    hist_rows[c] = pd.to_datetime(hist_rows[c], errors="coerce")

            df_sim = pd.concat([hist_rows, df_sim], ignore_index=True)
        except Exception as e:
            st.warning(f"No se pudo integrar ventas historicas: {e}")

    # Recalcular campos de periodo para TODAS las filas (incluyendo HISTORICO)
    df_sim["PERIODO"] = pd.to_datetime(df_sim["PERIODO"])
    df_sim["PERIODO_ANO"] = df_sim["PERIODO"].dt.year
    df_sim["PERIODO_MES"] = df_sim["PERIODO"].dt.month
    df_sim["ID_MES"] = df_sim["PERIODO"].dt.strftime("%Y%m")

    # Recalcular final_cols y extra_cols despues del concat
    final_cols = [c for c in cols_order if c in df_sim.columns]
    extra_cols = [c for c in df_sim.columns if c not in final_cols]

    return df_sim[final_cols + extra_cols]


# ============================================================================
# DAILY PROJECTION PIPELINE
# ============================================================================

def process_projection_daily(file_forecast, file_compra, file_precios, conn,
                              excluir_plan_compra=False):
    """Full daily projection pipeline: same inputs as process_projection, but simulates day-by-day.

    Returns (df_sim_monthly, df_sim_daily):
      - df_sim_monthly: monthly-aggregated results compatible with downstream (plan_compras, dashboards)
      - df_sim_daily: detailed daily simulation results

    If *excluir_plan_compra* is True, FORECAST_COMPRA is zeroed out so the
    simulation only considers current stock + confirmed COMEX (ETA).
    """
    # ── 1. Load local files (idéntico a process_projection) ──────────────
    forecast = pd.read_excel(file_forecast, sheet_name=0)
    compra_raw = pd.read_csv(file_compra)

    precios = None
    precios_mensuales = None

    if file_precios:
        _raw_precios = pd.read_excel(file_precios, sheet_name=0)
        _raw_precios = norm_cols(_raw_precios)
        col_sku_p = next((c for c in _raw_precios.columns if "SKU" in c or "MATERIAL" in c), None)
        if col_sku_p:
            _raw_precios["SKU_PRODUCTO"] = _raw_precios[col_sku_p].astype(str).str.strip().str.upper()

            mes_cols = [c for c in _raw_precios.columns if re.fullmatch(r"\d{1,2}", str(c)) and 1 <= int(c) <= 12]
            canal_col = next((c for c in _raw_precios.columns if c in ("CANAL", "CANAL_STD")), None)

            if len(mes_cols) >= 6 and canal_col:
                _raw_precios[canal_col] = _raw_precios[canal_col].astype(str).str.strip().str.upper()
                p_long = _raw_precios.melt(
                    id_vars=["SKU_PRODUCTO", canal_col],
                    value_vars=mes_cols,
                    var_name="PERIODO_MES",
                    value_name="PRECIO_NETO",
                )
                p_long["PERIODO_MES"] = pd.to_numeric(p_long["PERIODO_MES"], errors="coerce").astype("Int64")
                p_long["PRECIO_NETO"] = pd.to_numeric(p_long["PRECIO_NETO"], errors="coerce")

                rows_p = []
                for _, r in p_long.iterrows():
                    if pd.isna(r["PERIODO_MES"]):
                        continue
                    canal_src = r[canal_col]
                    if "TIENDA" in canal_src and "ETAIL" in canal_src:
                        rows_p.append({"SKU_PRODUCTO": r["SKU_PRODUCTO"], "CANAL_STD": "TIENDA",
                                       "PERIODO_MES": int(r["PERIODO_MES"]), "PRECIO_NETO": r["PRECIO_NETO"]})
                        rows_p.append({"SKU_PRODUCTO": r["SKU_PRODUCTO"], "CANAL_STD": "ETAIL",
                                       "PERIODO_MES": int(r["PERIODO_MES"]), "PRECIO_NETO": r["PRECIO_NETO"]})
                    elif "MAYOR" in canal_src:
                        rows_p.append({"SKU_PRODUCTO": r["SKU_PRODUCTO"], "CANAL_STD": "MAYORISTA",
                                       "PERIODO_MES": int(r["PERIODO_MES"]), "PRECIO_NETO": r["PRECIO_NETO"]})
                    else:
                        rows_p.append({"SKU_PRODUCTO": r["SKU_PRODUCTO"], "CANAL_STD": canal_src,
                                       "PERIODO_MES": int(r["PERIODO_MES"]), "PRECIO_NETO": r["PRECIO_NETO"]})

                p_std = pd.DataFrame(rows_p)
                p_wide = (
                    p_std.pivot_table(index=["SKU_PRODUCTO", "PERIODO_MES"],
                                      columns="CANAL_STD", values="PRECIO_NETO", aggfunc="max")
                    .reset_index()
                )
                rename_map = {}
                if "TIENDA" in p_wide.columns:
                    rename_map["TIENDA"] = "PRECIO_NETO_TIENDA"
                if "ETAIL" in p_wide.columns:
                    rename_map["ETAIL"] = "PRECIO_NETO_ETAIL"
                if "MAYORISTA" in p_wide.columns:
                    rename_map["MAYORISTA"] = "PRECIO_NETO_MAYOR"
                p_wide = p_wide.rename(columns=rename_map)
                precios_mensuales = p_wide
            else:
                precios = _raw_precios.drop_duplicates("SKU_PRODUCTO")
                cols_to_merge = [c for c in precios.columns if c != "SKU_PRODUCTO" and c != col_sku_p]
                precios = precios[["SKU_PRODUCTO"] + cols_to_merge]

    # ── 2. Query Snowflake (centralized cache) ───────────────────────────
    with lottie_spinner("snowflake"):
        stock = cq.stock_proyeccion(conn)
        comex = cq.comex_full(conn)
        maestra = cq.maestra(conn)
        ventas_mtd = cq.ventas_mtd(conn)
        ventas_mtd_diaria = cq.ventas_mtd_diaria(conn)
        ventas_hist = cq.ventas_mes_anterior(conn)
        ventas_aa = cq.ventas_aa(conn)

    # ── 3. Process Forecast → monthly f_piv ─────────────────────────────
    try:
        month_cols = [c for c in forecast.columns if re.fullmatch(r"\d{2}/\d{4}", str(c))]
        if not month_cols:
            st.error("No se encontraron columnas de fecha MM/YYYY en Forecast")
            return None

        f_long = forecast.melt(
            id_vars=[c for c in forecast.columns if c not in month_cols],
            value_vars=month_cols, var_name="mes", value_name="forecast",
        )
        f_long["PERIODO"] = pd.to_datetime("01/" + f_long["mes"].astype(str), format="%d/%m/%Y", errors="coerce")

        chan_map = {
            "ETAIL": "FORECAST_VENTA_ETAIL",
            "MAYORISTA": "FORECAST_VENTA_MAYOR",
            "MAYOR": "FORECAST_VENTA_MAYOR",
            "TIENDA": "FORECAST_VENTA_MINOR",
            "RETAIL": "FORECAST_VENTA_MINOR",
            "MINOR": "FORECAST_VENTA_MINOR",
        }
        col_canal = next((c for c in f_long.columns if "canal" in c.lower()), None)
        col_sku_fc = next((c for c in f_long.columns if "material" in c.lower() or "sku" in c.lower()), None)
        if not col_canal or not col_sku_fc:
            st.error("Faltan columnas 'canal' o 'sku/material' en Forecast")
            return None

        # Normalize channel values to uppercase to handle mixed casing
        f_long[col_canal] = f_long[col_canal].astype(str).str.strip().str.upper()

        f_sales = f_long[f_long[col_canal].isin(chan_map)].copy()
        f_sales["col"] = f_sales[col_canal].map(chan_map)
        f_sales["SKU_PRODUCTO"] = f_sales[col_sku_fc].astype(str).str.strip().str.upper()

        f_piv = (
            f_sales.pivot_table(index=["SKU_PRODUCTO", "PERIODO"], columns="col", values="forecast", aggfunc="sum")
            .reset_index().fillna(0)
        )
        for c in chan_map.values():
            if c not in f_piv.columns:
                f_piv[c] = 0.0
        f_piv["FORECAST_VENTA_TOTAL"] = f_piv["FORECAST_VENTA_ETAIL"] + f_piv["FORECAST_VENTA_MAYOR"] + f_piv["FORECAST_VENTA_MINOR"]
    except Exception as e:
        st.error(f"Error procesando Forecast: {e}")
        return None

    # ── 3b. Load daily weights (DOW×WOM) + data-driven event boosts, expand to daily ──
    with st.spinner("Cargando patrones históricos y expandiendo forecast diario..."):
        _daily_weights = _build_daily_weights(conn)
        _event_boosts_dict = _build_event_boosts_from_data(conn)
        # Enrich f_piv with SUBLINEA from maestra (needed for sublinea-level fallback)
        _has_any_boosts = (
            not _event_boosts_dict.get("sku", pd.DataFrame()).empty
            or not _event_boosts_dict.get("sublinea", pd.DataFrame()).empty
        )
        if _has_any_boosts and "SUBLINEA" not in f_piv.columns:
            _mae = cq.maestra(conn)
            _mae = norm_cols(_mae)
            if "SUBLINEA" in _mae.columns:
                _sub_map = _mae[["SKU_PRODUCTO", "SUBLINEA"]].drop_duplicates("SKU_PRODUCTO")
                f_piv = f_piv.merge(_sub_map, on="SKU_PRODUCTO", how="left")
        _has_wt = (not _daily_weights.get("global", pd.DataFrame()).empty
                   or not _daily_weights.get("per_canal", pd.DataFrame()).empty)
        _wt_mode = "ponderada (DOW×WOM híbrido)" if _has_wt else "uniforme"
        _n_sku_b = len(_event_boosts_dict.get("sku", pd.DataFrame()))
        _n_sub_b = len(_event_boosts_dict.get("sublinea", pd.DataFrame()))
        if _n_sku_b > 0:
            _wt_mode += f" + eventos por SKU ({_n_sku_b:,} combos)"
        if _n_sub_b > 0:
            _wt_mode += f" + fallback sublinea ({_n_sub_b:,})"
        f_daily = _expand_forecast_to_daily(
            f_piv, weights_dict=_daily_weights, event_boosts_dict=_event_boosts_dict
        )
        st.info(f"📊 Distribución diaria: **{_wt_mode}**")

    # ── 4. Process Purchases → daily arrivals ───────────────────────────
    try:
        compra_raw = norm_cols(compra_raw)
        _cc_d = compra_raw.columns.tolist()
        col_fecha_c = _find_compra_col(_cc_d, ["FECHA", "DATE", "PERIODO"])
        col_sku_c = _find_compra_col(_cc_d, ["MATERIAL", "SKU", "PRODUCTO", "CODIGO"])
        col_orden = _find_compra_col(_cc_d, ["ORDEN", "UNIDADES", "CANTIDAD", "QTY"])
        col_lead = _find_compra_col(_cc_d, ["LEAD"])

        if not col_fecha_c or not col_sku_c or not col_orden:
            _miss = [n for n, v in [("Fecha", col_fecha_c), ("SKU", col_sku_c), ("Cantidad", col_orden)] if not v]
            st.error(f"Columnas requeridas no encontradas en Compras: {_miss}. Columnas disponibles: {_cc_d}")
            return None

        if col_lead and col_lead in compra_raw.columns:
            if isinstance(compra_raw[col_lead], pd.DataFrame):
                compra_raw = compra_raw.loc[:, ~compra_raw.columns.duplicated()]
        else:
            compra_raw["_LEAD_TIME_0"] = 0
            col_lead = "_LEAD_TIME_0"

        c_daily = _expand_compra_to_daily(compra_raw, col_fecha_c, col_sku_c, col_orden, col_lead)
    except Exception as e:
        st.error(f"Error procesando Compras (diario): {e}. Cols: {compra_raw.columns.tolist()}")
        return None

    # ── 5. Process Stock (Snapshot) ─────────────────────────────────────
    try:
        stock["SKU_PRODUCTO"] = stock["SKU_PRODUCTO"].astype(str).str.strip().str.upper()
        s_piv = (
            stock.pivot_table(index="SKU_PRODUCTO", columns="CANAL_STD", values="STOCK_UNIDADES", aggfunc="sum")
            .fillna(0).reset_index()
        )
        if "CD" not in s_piv.columns:
            s_piv["CD"] = 0
        if "TIENDA" not in s_piv.columns:
            s_piv["TIENDA"] = 0
        s_piv = s_piv.rename(columns={"CD": "STOCK_INICIAL_CD", "TIENDA": "STOCK_INICIAL_TIENDA"})

        if "PERFIL_TIENDAS" in stock.columns:
            perfil = stock.groupby("SKU_PRODUCTO", as_index=False)["PERFIL_TIENDAS"].sum()
        else:
            perfil = pd.DataFrame({"SKU_PRODUCTO": stock["SKU_PRODUCTO"].unique(), "PERFIL_TIENDAS": 0.0})
        s_piv = s_piv.merge(perfil, on="SKU_PRODUCTO", how="left").fillna(0)
    except Exception as e:
        st.error(f"Error procesando Stock: {e}")
        return None

    # ── 6. Process MTD Sales ────────────────────────────────────────────
    mtd_map = {"MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL"}
    ventas_mtd["CANAL_STD"] = ventas_mtd["COD_CANAL"].map(mtd_map).fillna("TIENDA") if "COD_CANAL" in ventas_mtd.columns else "TIENDA"
    ventas_mtd["SKU_PRODUCTO"] = ventas_mtd["SKU_PRODUCTO"].astype(str).str.strip().str.upper() if "SKU_PRODUCTO" in ventas_mtd.columns else ""

    # ── 7. Process COMEX → daily arrivals (ETA + 10 days) ───────────────
    try:
        eta_daily = _expand_comex_to_daily(comex, maestra)
    except Exception as e:
        st.error(f"Error procesando Comex (diario): {e}")
        eta_daily = pd.DataFrame(columns=["SKU_PRODUCTO", "FECHA", "ETA"])

    # ── 8. Merge maestra dimensions into f_daily ────────────────────────
    cols_maestra = ["SKU_PRODUCTO"]
    for c in ["SKU_NOM_PRODUCTO", "LINEA", "SUBLINEA", "AREA", "MARCA",
              "PROCEDENCIA", "COSTO_FOB_USD", "ULTIMO_COSTO", "FACTOR_IMPORTACION",
              "PROVEEDOR", "COD_PROVEEDOR", "MIX_OFICIAL", "MODELO"]:
        if c in maestra.columns:
            cols_maestra.append(c)

    def _fillna_numeric(df):
        num_cols = df.select_dtypes(include="number").columns
        df[num_cols] = df[num_cols].fillna(0)
        return df

    # ── 9. Build daily df_main ──────────────────────────────────────────
    with lottie_spinner("stock"):
        # Merge forecast daily + compra daily + comex daily
        df_main = f_daily.merge(c_daily, on=["SKU_PRODUCTO", "FECHA"], how="outer")
        df_main = df_main.merge(eta_daily, on=["SKU_PRODUCTO", "FECHA"], how="outer")

        # Fill numeric NaN
        for col in ["FORECAST_COMPRA", "ETA", "FORECAST_VENTA_ETAIL", "FORECAST_VENTA_MAYOR",
                     "FORECAST_VENTA_MINOR", "FORECAST_VENTA_TOTAL"]:
            if col in df_main.columns:
                df_main[col] = df_main[col].fillna(0)

        # Zero out planned purchases if flag is set (keep ETA from COMEX)
        if excluir_plan_compra and "FORECAST_COMPRA" in df_main.columns:
            df_main["FORECAST_COMPRA"] = 0
            st.info("Plan de compras excluido: simulación solo con stock actual + COMEX confirmado.")

        # Fill PERIODO for rows that came from compra/comex (don't have PERIODO)
        if "PERIODO" not in df_main.columns or df_main["PERIODO"].isna().any():
            df_main["PERIODO"] = df_main["FECHA"].dt.to_period("M").dt.to_timestamp()

        # Merge maestra (by SKU only)
        df_main = df_main.merge(
            maestra[cols_maestra].drop_duplicates("SKU_PRODUCTO"), on="SKU_PRODUCTO", how="left"
        )
        df_main = _fillna_numeric(df_main)

        # Merge stock snapshot (by SKU only — same for every day)
        df_main = _fillna_numeric(df_main.merge(s_piv, on="SKU_PRODUCTO", how="left"))

        # ── MTD: drop past days → simulation starts from stock_date + 1 ──
        stock_date = pd.to_datetime(stock["FECHA"]).max().normalize()
        df_main = df_main[df_main["FECHA"] > stock_date].copy()

        # Stock inicial: only applies to the FIRST day per SKU; rest = 0
        # (the simulation will carry forward from day to day)
        # NOTE: must run AFTER filtering past days so stock goes to the first future day
        df_main = df_main.sort_values(["SKU_PRODUCTO", "FECHA"])
        first_day_mask = ~df_main.duplicated(subset=["SKU_PRODUCTO"], keep="first")
        for col_stock in ["STOCK_INICIAL_CD", "STOCK_INICIAL_TIENDA"]:
            if col_stock in df_main.columns:
                df_main.loc[~first_day_mask, col_stock] = 0

        if df_main.empty:
            st.warning("No hay días futuros para simular. Verifica el forecast.")
            return None

        # Demand for future days = full daily forecast
        df_main["DEMANDA_SIM_TIENDA"] = df_main["FORECAST_VENTA_MINOR"]
        df_main["DEMANDA_SIM_ETAIL"] = df_main["FORECAST_VENTA_ETAIL"]
        df_main["DEMANDA_SIM_MAYOR"] = df_main["FORECAST_VENTA_MAYOR"]

        st.info(f"📸 Foto de stock: **{stock_date.strftime('%d/%m/%Y')}** — "
                f"simulación inicia el **{(stock_date + pd.Timedelta(days=1)).strftime('%d/%m/%Y')}**")

    # ── 10. Run daily simulation ────────────────────────────────────────
    with lottie_spinner("proyeccion"):
        df_sim_daily = run_stock_simulation_daily(df_main)

    if df_sim_daily.empty:
        st.warning("No se generaron datos de simulación diaria.")
        return None

    df_sim_daily["TIPO_DATO"] = "PROYECCION"
    df_sim_daily["DEMANDA_TOTAL"] = (
        df_sim_daily["DEMANDA_SIM_TIENDA"]
        + df_sim_daily["DEMANDA_SIM_ETAIL"]
        + df_sim_daily["DEMANDA_SIM_MAYOR"]
    )

    # ── 10b. Build HISTORICO rows from daily MTD sales (1..stock_date) ──
    df_hist_daily = pd.DataFrame()
    if not ventas_mtd_diaria.empty and "FECHA" in ventas_mtd_diaria.columns:
        try:
            vd = ventas_mtd_diaria.copy()
            vd["FECHA"] = pd.to_datetime(vd["FECHA"]).dt.normalize()
            vd["SKU_PRODUCTO"] = vd["SKU_PRODUCTO"].astype(str).str.strip().str.upper()

            # Map channels
            _ch_map = {"MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL"}
            if "COD_CANAL" in vd.columns:
                vd["CANAL_STD"] = vd["COD_CANAL"].map(_ch_map).fillna("TIENDA")
            else:
                vd["CANAL_STD"] = "TIENDA"

            # Pivot: one row per SKU × FECHA with columns per channel
            for _col in ["CANTIDAD", "NETO"]:
                vd[_col] = pd.to_numeric(vd[_col], errors="coerce").fillna(0)

            qty_piv = (
                vd.pivot_table(index=["SKU_PRODUCTO", "FECHA"], columns="CANAL_STD",
                               values="CANTIDAD", aggfunc="sum").fillna(0).reset_index()
            )
            neto_piv = (
                vd.pivot_table(index=["SKU_PRODUCTO", "FECHA"], columns="CANAL_STD",
                               values="NETO", aggfunc="sum").fillna(0).reset_index()
            )

            df_h = qty_piv.copy()
            df_h = df_h.rename(columns={
                "TIENDA": "VENTA_FUL_TIENDA_UND", "ETAIL": "VENTA_FUL_ETAIL_UND",
                "MAYORISTA": "VENTA_FUL_MAYOR_UND",
            })
            for _c in ["VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND", "VENTA_FUL_MAYOR_UND"]:
                if _c not in df_h.columns:
                    df_h[_c] = 0.0

            # Add neto by channel
            neto_piv = neto_piv.rename(columns={
                "TIENDA": "VN_RES_TIENDA", "ETAIL": "VN_RES_ETAIL", "MAYORISTA": "VN_RES_MAYOR",
            })
            for _c in ["VN_RES_TIENDA", "VN_RES_ETAIL", "VN_RES_MAYOR"]:
                if _c not in neto_piv.columns:
                    neto_piv[_c] = 0.0
            df_h = df_h.merge(neto_piv[["SKU_PRODUCTO", "FECHA", "VN_RES_TIENDA", "VN_RES_ETAIL", "VN_RES_MAYOR"]],
                              on=["SKU_PRODUCTO", "FECHA"], how="left")

            df_h["VN_RES_TOTAL"] = df_h["VN_RES_TIENDA"].fillna(0) + df_h["VN_RES_ETAIL"].fillna(0) + df_h["VN_RES_MAYOR"].fillna(0)
            df_h["PERIODO"] = df_h["FECHA"].dt.to_period("M").dt.to_timestamp()
            df_h["TIPO_DATO"] = "HISTORICO"

            # Merge maestra dimensions
            df_h = df_h.merge(
                maestra[cols_maestra].drop_duplicates("SKU_PRODUCTO"), on="SKU_PRODUCTO", how="left"
            )

            df_hist_daily = df_h
            st.info(f"📊 Ventas históricas: **{df_h['FECHA'].nunique()}** días "
                    f"({df_h['FECHA'].min().strftime('%d/%m')} – {df_h['FECHA'].max().strftime('%d/%m')}), "
                    f"**{df_h['SKU_PRODUCTO'].nunique()}** SKUs")
        except Exception as e:
            st.warning(f"No se pudo construir filas HISTORICO diarias: {e}")

    # ── 11. Aggregate to monthly ────────────────────────────────────────
    with st.spinner("Agregando resultados a nivel mensual..."):
        df_sim = _aggregate_daily_to_monthly(df_sim_daily)

    # ── 12. Pricing & Valuation (applied to monthly aggregate) ──────────
    # Same logic as process_projection steps 10a-10h
    if precios is not None:
        df_sim = df_sim.merge(precios, on="SKU_PRODUCTO", how="left")

    df_sim["PERIODO"] = pd.to_datetime(df_sim["PERIODO"])
    df_sim["PERIODO_ANO"] = df_sim["PERIODO"].dt.year
    df_sim["PERIODO_MES"] = df_sim["PERIODO"].dt.month
    df_sim["ID_MES"] = df_sim["PERIODO"].dt.strftime("%Y%m")
    df_sim["TIPO_DATO"] = "PROYECCION"

    if precios_mensuales is not None:
        df_sim = df_sim.merge(precios_mensuales, on=["SKU_PRODUCTO", "PERIODO_MES"], how="left")

    # Cost merge from maestra
    cost_cols_needed = [c for c in ["ULTIMO_COSTO", "COSTO_FOB_USD", "FACTOR_IMPORTACION"] if c in maestra.columns]
    if cost_cols_needed:
        cost_merge = maestra[["SKU_PRODUCTO"] + cost_cols_needed].drop_duplicates("SKU_PRODUCTO")
        cost_cols_new = [c for c in cost_cols_needed if c not in df_sim.columns]
        if cost_cols_new:
            df_sim = df_sim.merge(cost_merge[["SKU_PRODUCTO"] + cost_cols_new], on="SKU_PRODUCTO", how="left")

    for cc in ["ULTIMO_COSTO", "COSTO_FOB_USD", "FACTOR_IMPORTACION"]:
        if cc in df_sim.columns:
            df_sim[cc] = pd.to_numeric(df_sim[cc], errors="coerce").fillna(0)

    # Factor importacion fallback by hierarchy
    _has_struct = all(c in maestra.columns for c in ["FACTOR_IMPORTACION", "SUBLINEA", "LINEA", "AREA", "MARCA"])
    if _has_struct:
        _m = maestra.copy()
        _m["FACTOR_IMPORTACION"] = pd.to_numeric(_m["FACTOR_IMPORTACION"], errors="coerce")
        _m_valid = _m[_m["FACTOR_IMPORTACION"] > 0]

        factor_avg = {}
        for level_name, group_cols in [
            ("SUBLINEA_MARCA", ["SUBLINEA", "MARCA"]), ("LINEA_MARCA", ["LINEA", "MARCA"]),
            ("AREA_MARCA", ["AREA", "MARCA"]), ("SUBLINEA", ["SUBLINEA"]),
            ("LINEA", ["LINEA"]), ("AREA", ["AREA"]),
        ]:
            agg = _m_valid.groupby(group_cols, as_index=False)["FACTOR_IMPORTACION"].mean()
            agg = agg.rename(columns={"FACTOR_IMPORTACION": f"FACTOR_AVG_{level_name}"})
            factor_avg[level_name] = (group_cols, agg)

        mask_no_factor = (df_sim["FACTOR_IMPORTACION"].isna()) | (df_sim["FACTOR_IMPORTACION"] <= 0)
        if mask_no_factor.any():
            for sc in ["SUBLINEA", "LINEA", "AREA", "MARCA"]:
                if sc not in df_sim.columns:
                    df_sim[sc] = ""
            for level_name, (group_cols, agg_df) in factor_avg.items():
                still_missing = (df_sim["FACTOR_IMPORTACION"].isna()) | (df_sim["FACTOR_IMPORTACION"] <= 0)
                if not still_missing.any():
                    break
                col_avg = f"FACTOR_AVG_{level_name}"
                df_sim = df_sim.merge(agg_df, on=group_cols, how="left")
                fill_mask = still_missing & df_sim[col_avg].notna() & (df_sim[col_avg] > 0)
                df_sim.loc[fill_mask, "FACTOR_IMPORTACION"] = df_sim.loc[fill_mask, col_avg]
                df_sim.drop(columns=[col_avg], inplace=True)

    # Primary cost: ULTIMO_COSTO
    df_sim["COSTO_UNITARIO"] = df_sim.get("ULTIMO_COSTO", pd.Series(0, index=df_sim.index))
    TC_USD_CLP = st.session_state.get("tc_usd_clp", 950)
    mask_sin_costo = (df_sim["COSTO_UNITARIO"].isna()) | (df_sim["COSTO_UNITARIO"] <= 0)
    if "COSTO_FOB_USD" in df_sim.columns and "FACTOR_IMPORTACION" in df_sim.columns:
        costo_landed = df_sim["COSTO_FOB_USD"] * df_sim["FACTOR_IMPORTACION"] * TC_USD_CLP
        df_sim.loc[mask_sin_costo, "COSTO_UNITARIO"] = costo_landed[mask_sin_costo]
    elif "COSTO_FOB_USD" in df_sim.columns:
        df_sim.loc[mask_sin_costo, "COSTO_UNITARIO"] = df_sim.loc[mask_sin_costo, "COSTO_FOB_USD"] * TC_USD_CLP
    df_sim["COSTO_UNITARIO"] = df_sim["COSTO_UNITARIO"].fillna(0)

    df_sim["ORIGEN_COSTO"] = np.where(
        df_sim.get("ULTIMO_COSTO", 0) > 0, "ULTIMO_COSTO",
        np.where(df_sim["COSTO_UNITARIO"] > 0, "LANDED_CALC", "SIN_COSTO")
    )

    # Flag SKUs with zero cost (contaminates margin calculations)
    df_sim["FLAG_SIN_COSTO"] = (
        (df_sim["ORIGEN_COSTO"] == "SIN_COSTO") | (df_sim["COSTO_UNITARIO"] <= 0)
    )

    # MTD & historical prices (same logic as monthly)
    PERIODO_ACTUAL = stock_date.to_period("M").to_timestamp()
    neto_col = "NETO_MTD" if "NETO_MTD" in ventas_mtd.columns else "NETO_TOTAL"
    if not ventas_mtd.empty and neto_col in ventas_mtd.columns and "CANTIDAD_MTD" in ventas_mtd.columns:
        ventas_mtd["PRECIO_PROMEDIO"] = np.where(
            ventas_mtd["CANTIDAD_MTD"] > 0, ventas_mtd[neto_col] / ventas_mtd["CANTIDAD_MTD"], 0)
        mtd_prices = (
            ventas_mtd.pivot_table(index="SKU_PRODUCTO", columns="CANAL_STD", values="PRECIO_PROMEDIO", aggfunc="mean")
            .reset_index()
        )
        mtd_prices.columns = ["SKU_PRODUCTO"] + [f"PRECIO_MTD_{c}" for c in mtd_prices.columns[1:]]
        df_sim = df_sim.merge(mtd_prices, on="SKU_PRODUCTO", how="left")

    if not ventas_hist.empty:
        try:
            hist_price_map = {"MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL"}
            vh_price = ventas_hist.copy()
            vh_price["CANAL_STD"] = vh_price["COD_CANAL"].map(hist_price_map).fillna("TIENDA")
            vh_price["PRECIO_PROMEDIO"] = np.where(
                vh_price["CANTIDAD_MES"] > 0, vh_price["NETO_MES"] / vh_price["CANTIDAD_MES"], 0)
            hist_prices = (
                vh_price.pivot_table(index="SKU_PRODUCTO", columns="CANAL_STD", values="PRECIO_PROMEDIO", aggfunc="mean")
                .reset_index()
            )
            hist_prices.columns = ["SKU_PRODUCTO"] + [f"PRECIO_MES_ANT_{c}" for c in hist_prices.columns[1:]]
            df_sim = df_sim.merge(hist_prices, on="SKU_PRODUCTO", how="left")
        except Exception:
            pass

    # Price fallback: PLANILLA → MTD → MES_ANTERIOR → CROSS_CHANNEL
    p_tienda = next((c for c in df_sim.columns if "PRECIO" in c and ("TIENDA" in c or "MINOR" in c)
                     and "MTD" not in c and "MES_ANT" not in c and "USADO" not in c), None)
    p_etail = next((c for c in df_sim.columns if "PRECIO" in c and "ETAIL" in c
                    and "MTD" not in c and "MES_ANT" not in c and "USADO" not in c), None)
    p_mayor = next((c for c in df_sim.columns if "PRECIO" in c and "MAYOR" in c
                    and "MTD" not in c and "MES_ANT" not in c and "USADO" not in c), None)

    for canal, col_planilla, col_mtd, col_hist in [
        ("TIENDA", p_tienda, "PRECIO_MTD_TIENDA", "PRECIO_MES_ANT_TIENDA"),
        ("ETAIL", p_etail, "PRECIO_MTD_ETAIL", "PRECIO_MES_ANT_ETAIL"),
        ("MAYOR", p_mayor, "PRECIO_MTD_MAYORISTA", "PRECIO_MES_ANT_MAYORISTA"),
    ]:
        precio_plan = df_sim[col_planilla].fillna(0) if col_planilla and col_planilla in df_sim.columns else 0.0
        precio_mtd = df_sim[col_mtd].fillna(0) if col_mtd in df_sim.columns else 0.0
        precio_hist = df_sim[col_hist].fillna(0) if col_hist in df_sim.columns else 0.0

        df_sim[f"PRECIO_USADO_{canal}"] = np.where(
            precio_plan > 0, precio_plan,
            np.where(precio_mtd > 0, precio_mtd,
                np.where(precio_hist > 0, precio_hist, 0.0)))
        df_sim[f"ORIGEN_PRECIO_{canal}"] = np.where(
            precio_plan > 0, "PLANILLA",
            np.where(precio_mtd > 0, "PROMEDIO_MTD",
                np.where(precio_hist > 0, "PROMEDIO_MES_ANT", "SIN_PRECIO")))

    # Cross-channel fallback
    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
        otros = [c for c in ["TIENDA", "ETAIL", "MAYOR"] if c != canal]
        mask_sin = df_sim[f"PRECIO_USADO_{canal}"] == 0
        for otro in otros:
            precio_otro = df_sim.loc[mask_sin, f"PRECIO_USADO_{otro}"]
            updated = mask_sin & (precio_otro > 0)
            df_sim.loc[updated, f"PRECIO_USADO_{canal}"] = df_sim.loc[updated, f"PRECIO_USADO_{otro}"]
            df_sim.loc[updated, f"ORIGEN_PRECIO_{canal}"] = f"CROSS_{otro}"
            mask_sin = df_sim[f"PRECIO_USADO_{canal}"] == 0

    # ── Revenue por canal (restricto) ──
    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
        df_sim[f"VN_RES_{canal}"] = df_sim[f"VENTA_FUL_{canal}_UND"] * df_sim[f"PRECIO_USADO_{canal}"]
    df_sim["VN_RES_TOTAL"] = df_sim["VN_RES_TIENDA"] + df_sim["VN_RES_ETAIL"] + df_sim["VN_RES_MAYOR"]

    # ── COGS, APORTE, MARGEN por canal (restricto) ──
    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
        df_sim[f"COGS_RES_{canal}"] = df_sim[f"VENTA_FUL_{canal}_UND"] * df_sim["COSTO_UNITARIO"]
        df_sim[f"APORTE_RES_{canal}"] = df_sim[f"VN_RES_{canal}"] - df_sim[f"COGS_RES_{canal}"]
        df_sim[f"MARGEN_RES_{canal}"] = np.where(
            df_sim[f"VN_RES_{canal}"] > 0,
            df_sim[f"APORTE_RES_{canal}"] / df_sim[f"VN_RES_{canal}"],
            0.0,
        )

    # Totales restrictos (suma de canales)
    df_sim["COGS_RES_TOTAL"] = df_sim["COGS_RES_TIENDA"] + df_sim["COGS_RES_ETAIL"] + df_sim["COGS_RES_MAYOR"]
    df_sim["APORTE_RES_TOTAL"] = df_sim["VN_RES_TOTAL"] - df_sim["COGS_RES_TOTAL"]
    df_sim["MARGEN_RES_TOTAL"] = np.where(
        df_sim["VN_RES_TOTAL"] > 0, df_sim["APORTE_RES_TOTAL"] / df_sim["VN_RES_TOTAL"], 0.0)

    # ── Revenue irrestricto por canal (demanda × precio) ──
    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
        df_sim[f"VN_IRR_{canal}"] = df_sim[f"DEMANDA_SIM_{canal}"] * df_sim[f"PRECIO_USADO_{canal}"]
        df_sim[f"COGS_IRR_{canal}"] = df_sim[f"DEMANDA_SIM_{canal}"] * df_sim["COSTO_UNITARIO"]
        df_sim[f"APORTE_IRR_{canal}"] = df_sim[f"VN_IRR_{canal}"] - df_sim[f"COGS_IRR_{canal}"]
        df_sim[f"MARGEN_IRR_{canal}"] = np.where(
            df_sim[f"VN_IRR_{canal}"] > 0,
            df_sim[f"APORTE_IRR_{canal}"] / df_sim[f"VN_IRR_{canal}"],
            0.0,
        )

    # Totales irrestrictos
    df_sim["VN_IRR_TOTAL"] = df_sim["VN_IRR_TIENDA"] + df_sim["VN_IRR_ETAIL"] + df_sim["VN_IRR_MAYOR"]
    df_sim["COGS_IRR_TOTAL"] = df_sim["COGS_IRR_TIENDA"] + df_sim["COGS_IRR_ETAIL"] + df_sim["COGS_IRR_MAYOR"]
    df_sim["APORTE_IRR_TOTAL"] = df_sim["VN_IRR_TOTAL"] - df_sim["COGS_IRR_TOTAL"]
    df_sim["MARGEN_IRR_TOTAL"] = np.where(
        df_sim["VN_IRR_TOTAL"] > 0, df_sim["APORTE_IRR_TOTAL"] / df_sim["VN_IRR_TOTAL"], 0.0)

    # KPIs
    df_sim["LOST_SALES_TOTAL"] = df_sim.get("LOST_SALES_TIENDA", 0) + df_sim.get("LOST_SALES_CD", 0)

    # Ensure per-channel lost sales exist (fallback pro-rata if missing)
    if "LOST_SALES_ETAIL" not in df_sim.columns:
        _dem_cd = pd.to_numeric(df_sim.get("DEMANDA_SIM_ETAIL", 0), errors="coerce").fillna(0) + \
                  pd.to_numeric(df_sim.get("DEMANDA_SIM_MAYOR", 0), errors="coerce").fillna(0)
        _ls_cd = pd.to_numeric(df_sim.get("LOST_SALES_CD", 0), errors="coerce").fillna(0)
        df_sim["LOST_SALES_ETAIL"] = np.where(_dem_cd > 0, _ls_cd * (df_sim.get("DEMANDA_SIM_ETAIL", 0) / _dem_cd), 0.0)
        df_sim["LOST_SALES_MAYOR"] = np.where(_dem_cd > 0, _ls_cd * (df_sim.get("DEMANDA_SIM_MAYOR", 0) / _dem_cd), 0.0)

    # Financial Lost Sales per channel
    for _lc in ["TIENDA", "ETAIL", "MAYOR"]:
        _ls_col = f"LOST_SALES_{_lc}"
        _pr_col = f"PRECIO_USADO_{_lc}"
        if _ls_col not in df_sim.columns:
            df_sim[_ls_col] = 0.0
        _ls_und = pd.to_numeric(df_sim[_ls_col], errors="coerce").fillna(0)
        _pr = pd.to_numeric(df_sim.get(_pr_col, 0), errors="coerce").fillna(0)
        _cu = pd.to_numeric(df_sim.get("COSTO_UNITARIO", 0), errors="coerce").fillna(0)
        df_sim[f"VN_LOST_{_lc}"] = _ls_und * _pr
        df_sim[f"COGS_LOST_{_lc}"] = _ls_und * _cu
        df_sim[f"APORTE_LOST_{_lc}"] = df_sim[f"VN_LOST_{_lc}"] - df_sim[f"COGS_LOST_{_lc}"]
        df_sim[f"MARGEN_LOST_{_lc}"] = np.where(
            df_sim[f"VN_LOST_{_lc}"] > 0,
            df_sim[f"APORTE_LOST_{_lc}"] / df_sim[f"VN_LOST_{_lc}"],
            0.0,
        )
    df_sim["VN_LOST_TOTAL"] = df_sim["VN_LOST_TIENDA"] + df_sim["VN_LOST_ETAIL"] + df_sim["VN_LOST_MAYOR"]
    df_sim["COGS_LOST_TOTAL"] = df_sim["COGS_LOST_TIENDA"] + df_sim["COGS_LOST_ETAIL"] + df_sim["COGS_LOST_MAYOR"]
    df_sim["APORTE_LOST_TOTAL"] = df_sim["VN_LOST_TOTAL"] - df_sim["COGS_LOST_TOTAL"]
    df_sim["MARGEN_LOST_TOTAL"] = np.where(
        df_sim["VN_LOST_TOTAL"] > 0, df_sim["APORTE_LOST_TOTAL"] / df_sim["VN_LOST_TOTAL"], 0.0
    )

    # INSTOCK_CIA ya viene calculado desde la simulación (mean de 0/1 diarios)
    df_sim["QUIEBRE_POR_PERFIL"] = np.where(df_sim.get("DEFICIT_PERFIL_UND", 0) > 0, 1, 0)

    df_sim["DEMANDA_TOTAL"] = df_sim["DEMANDA_SIM_TIENDA"] + df_sim["DEMANDA_SIM_ETAIL"] + df_sim["DEMANDA_SIM_MAYOR"]
    df_sim = df_sim.sort_values(["SKU_PRODUCTO", "PERIODO"])
    df_sim["DEMANDA_AVG_3M"] = df_sim.groupby("SKU_PRODUCTO")["DEMANDA_TOTAL"].transform(
        lambda x: x.rolling(3, min_periods=1).mean()
    )
    df_sim["MOI_COBERTURA"] = np.where(
        df_sim["DEMANDA_AVG_3M"] > 0, df_sim["STOCK_FINAL_TOTAL"] / df_sim["DEMANDA_AVG_3M"], 999)

    # Break detection
    df_sim["TIENE_QUIEBRE"] = (df_sim["STOCK_FINAL_TOTAL"] < 0.1).astype(int)
    quiebre_mes = df_sim[df_sim["TIENE_QUIEBRE"] == 1].groupby("SKU_PRODUCTO")["PERIODO"].min()
    df_sim = df_sim.merge(quiebre_mes.rename("MES_QUIEBRE"), on="SKU_PRODUCTO", how="left")
    df_sim["MESES_HASTA_QUIEBRE"] = np.where(
        df_sim["MES_QUIEBRE"].notna(),
        ((df_sim["MES_QUIEBRE"] - PERIODO_ACTUAL).dt.days / 30).round(1), 999)

    df_sim["WARNING_FC_REAL"] = ""

    # ── Integrate HISTORICO rows: combine current month into REAL+FC ────
    if not df_hist_daily.empty:
        try:
            # Aggregate historical daily sales to monthly
            h_agg = df_hist_daily.groupby(["SKU_PRODUCTO", "PERIODO"]).agg({
                "VENTA_FUL_TIENDA_UND": "sum",
                "VENTA_FUL_ETAIL_UND": "sum",
                "VENTA_FUL_MAYOR_UND": "sum",
                "VN_RES_TIENDA": "sum",
                "VN_RES_ETAIL": "sum",
                "VN_RES_MAYOR": "sum",
                "VN_RES_TOTAL": "sum",
            }).reset_index()
            h_agg["PERIODO"] = pd.to_datetime(h_agg["PERIODO"])

            # ── Combine current month: HISTORICO + PROYECCION → REAL+FC ──
            current_month = PERIODO_ACTUAL
            h_current = h_agg[h_agg["PERIODO"] == current_month].copy()

            if not h_current.empty:
                # Columns to sum (real + projected)
                _sum_cols = ["VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND",
                             "VENTA_FUL_MAYOR_UND", "VN_RES_TIENDA",
                             "VN_RES_ETAIL", "VN_RES_MAYOR", "VN_RES_TOTAL"]

                # Rename hist columns to avoid clash
                h_rename = {c: f"{c}_HIST" for c in _sum_cols}
                h_for_merge = h_current[["SKU_PRODUCTO"] + _sum_cols].rename(columns=h_rename)

                # Get PROYECCION rows for current month
                proy_mask = df_sim["PERIODO"] == current_month
                proy_current = df_sim[proy_mask].copy()

                if not proy_current.empty:
                    # Merge real sales onto PROYECCION rows
                    combined = proy_current.merge(h_for_merge, on="SKU_PRODUCTO", how="left")

                    # Sum: venta fulfillment = real + projected
                    for c in _sum_cols:
                        combined[c] = combined[c].fillna(0) + combined[f"{c}_HIST"].fillna(0)

                    # Drop _HIST temp columns
                    combined = combined.drop(columns=[f"{c}_HIST" for c in _sum_cols], errors="ignore")

                    # Recalculate COGS, APORTE, MARGEN por canal y total
                    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
                        combined[f"COGS_RES_{canal}"] = (
                            combined[f"VENTA_FUL_{canal}_UND"] * combined["COSTO_UNITARIO"]
                        )
                        combined[f"APORTE_RES_{canal}"] = (
                            combined[f"VN_RES_{canal}"] - combined[f"COGS_RES_{canal}"]
                        )
                        combined[f"MARGEN_RES_{canal}"] = np.where(
                            combined[f"VN_RES_{canal}"] > 0,
                            combined[f"APORTE_RES_{canal}"] / combined[f"VN_RES_{canal}"],
                            0.0,
                        )

                    combined["COGS_RES_TOTAL"] = (
                        combined["COGS_RES_TIENDA"] + combined["COGS_RES_ETAIL"] + combined["COGS_RES_MAYOR"]
                    )
                    combined["APORTE_RES_TOTAL"] = combined["VN_RES_TOTAL"] - combined["COGS_RES_TOTAL"]
                    combined["MARGEN_RES_TOTAL"] = np.where(
                        combined["VN_RES_TOTAL"] > 0,
                        combined["APORTE_RES_TOTAL"] / combined["VN_RES_TOTAL"], 0.0)

                    combined["TIPO_DATO"] = "REAL+FC"

                    # Replace PROYECCION rows of current month with combined REAL+FC
                    df_sim = pd.concat(
                        [df_sim[~proy_mask], combined], ignore_index=True
                    )

            # Also add HISTORICO to daily detail
            df_hist_daily["TIPO_DATO"] = "HISTORICO"
            df_hist_daily["DEMANDA_TOTAL"] = 0.0
            for c in df_sim_daily.columns:
                if c not in df_hist_daily.columns:
                    if pd.api.types.is_datetime64_any_dtype(df_sim_daily[c]):
                        df_hist_daily[c] = pd.NaT
                    elif pd.api.types.is_numeric_dtype(df_sim_daily[c]):
                        df_hist_daily[c] = 0.0
                    else:
                        df_hist_daily[c] = np.nan
            df_hist_daily = df_hist_daily[[c for c in df_sim_daily.columns if c in df_hist_daily.columns]].copy()
            df_sim_daily = pd.concat([df_hist_daily, df_sim_daily], ignore_index=True)

        except Exception as e:
            st.warning(f"No se pudo integrar filas HISTORICO al resultado mensual: {e}")

    # ── Integrate ventas mes anterior as HISTORICO (same as monthly) ───
    if not ventas_hist.empty and "SKU_PRODUCTO" in ventas_hist.columns:
        try:
            PERIODO_HISTORICO_ANT = PERIODO_ACTUAL - pd.DateOffset(months=1)
            hist_mtd_map = {"MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL"}
            vh = ventas_hist.copy()
            vh["CANAL_STD"] = vh["COD_CANAL"].map(hist_mtd_map).fillna("TIENDA") if "COD_CANAL" in vh.columns else "TIENDA"

            h2_agg = vh.groupby("SKU_PRODUCTO").agg({"CANTIDAD_MES": "sum", "NETO_MES": "sum"}).reset_index()
            h2_agg = h2_agg.merge(maestra[cols_maestra].drop_duplicates("SKU_PRODUCTO"), on="SKU_PRODUCTO", how="left")

            h2_costo_col = "ULTIMO_COSTO" if "ULTIMO_COSTO" in h2_agg.columns else "COSTO_FOB_USD"
            h2_agg["COSTO_UNITARIO"] = pd.to_numeric(h2_agg.get(h2_costo_col, 0), errors="coerce").fillna(0)

            h2_rows = pd.DataFrame({
                "SKU_PRODUCTO": h2_agg["SKU_PRODUCTO"],
                "PERIODO": PERIODO_HISTORICO_ANT,
                "TIPO_DATO": "HISTORICO",
                "VENTA_FUL_TIENDA_UND": 0.0, "VENTA_FUL_ETAIL_UND": 0.0, "VENTA_FUL_MAYOR_UND": 0.0,
                "VN_RES_TIENDA": 0.0, "VN_RES_ETAIL": 0.0, "VN_RES_MAYOR": 0.0,
                "VN_RES_TOTAL": h2_agg["NETO_MES"],
                "COSTO_UNITARIO": h2_agg["COSTO_UNITARIO"],
            })
            for _dim in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                         "PROCEDENCIA", "MIX_OFICIAL", "MODELO", "PROVEEDOR", "COD_PROVEEDOR"]:
                if _dim in h2_agg.columns:
                    h2_rows[_dim] = h2_agg[_dim].values

            # Break out by channel
            for canal_std, col_suffix in [("TIENDA", "TIENDA"), ("ETAIL", "ETAIL"), ("MAYORISTA", "MAYOR")]:
                canal_data = vh[vh["CANAL_STD"] == canal_std]
                if not canal_data.empty:
                    canal_agg = canal_data.groupby("SKU_PRODUCTO").agg(
                        {"CANTIDAD_MES": "sum", "NETO_MES": "sum"}).reset_index()
                    h2_rows = h2_rows.merge(
                        canal_agg.rename(columns={
                            "CANTIDAD_MES": f"VENTA_FUL_{col_suffix}_UND_TEMP",
                            "NETO_MES": f"VN_RES_{col_suffix}_TEMP"}),
                        on="SKU_PRODUCTO", how="left")
                    h2_rows[f"VENTA_FUL_{col_suffix}_UND"] = h2_rows.get(
                        f"VENTA_FUL_{col_suffix}_UND_TEMP", 0).fillna(0)
                    h2_rows[f"VN_RES_{col_suffix}"] = h2_rows.get(
                        f"VN_RES_{col_suffix}_TEMP", 0).fillna(0)
                    h2_rows = h2_rows.drop(columns=[c for c in h2_rows.columns if "_TEMP" in c], errors="ignore")

            # ── COGS, APORTE, MARGEN por canal (restricto) ──
            for canal in ["TIENDA", "ETAIL", "MAYOR"]:
                h2_rows[f"COGS_RES_{canal}"] = (
                    h2_rows[f"VENTA_FUL_{canal}_UND"] * h2_rows["COSTO_UNITARIO"]
                )
                h2_rows[f"APORTE_RES_{canal}"] = (
                    h2_rows[f"VN_RES_{canal}"] - h2_rows[f"COGS_RES_{canal}"]
                )
                h2_rows[f"MARGEN_RES_{canal}"] = np.where(
                    h2_rows[f"VN_RES_{canal}"] > 0,
                    h2_rows[f"APORTE_RES_{canal}"] / h2_rows[f"VN_RES_{canal}"], 0.0)

            h2_rows["COGS_RES_TOTAL"] = (
                h2_rows["COGS_RES_TIENDA"] + h2_rows["COGS_RES_ETAIL"] + h2_rows["COGS_RES_MAYOR"]
            )
            h2_rows["APORTE_RES_TOTAL"] = h2_rows["VN_RES_TOTAL"] - h2_rows["COGS_RES_TOTAL"]
            h2_rows["MARGEN_RES_TOTAL"] = np.where(
                h2_rows["VN_RES_TOTAL"] > 0, h2_rows["APORTE_RES_TOTAL"] / h2_rows["VN_RES_TOTAL"], 0.0)

            # ── Precios historicos por canal (VN / Unidades) ──
            for canal in ["TIENDA", "ETAIL", "MAYOR"]:
                h2_rows[f"PRECIO_USADO_{canal}"] = np.where(
                    h2_rows[f"VENTA_FUL_{canal}_UND"] > 0,
                    h2_rows[f"VN_RES_{canal}"] / h2_rows[f"VENTA_FUL_{canal}_UND"], 0.0)
                h2_rows[f"ORIGEN_PRECIO_{canal}"] = np.where(
                    h2_rows[f"VENTA_FUL_{canal}_UND"] > 0, "HISTORICO", "SIN_PRECIO")

            # ── Irrestricto = restricto para HISTORICO (demanda = venta real) ──
            for canal in ["TIENDA", "ETAIL", "MAYOR"]:
                h2_rows[f"VN_IRR_{canal}"] = h2_rows[f"VN_RES_{canal}"]
                h2_rows[f"COGS_IRR_{canal}"] = h2_rows[f"COGS_RES_{canal}"]
                h2_rows[f"APORTE_IRR_{canal}"] = h2_rows[f"APORTE_RES_{canal}"]
                h2_rows[f"MARGEN_IRR_{canal}"] = h2_rows[f"MARGEN_RES_{canal}"]
            h2_rows["VN_IRR_TOTAL"] = h2_rows["VN_RES_TOTAL"]
            h2_rows["COGS_IRR_TOTAL"] = h2_rows["COGS_RES_TOTAL"]
            h2_rows["APORTE_IRR_TOTAL"] = h2_rows["APORTE_RES_TOTAL"]
            h2_rows["MARGEN_IRR_TOTAL"] = h2_rows["MARGEN_RES_TOTAL"]

            # ── DIAS_MES para HISTORICO ──
            h2_rows["DIAS_MES"] = pd.to_datetime(PERIODO_HISTORICO_ANT).days_in_month

            h2_rows["PERIODO"] = pd.to_datetime(h2_rows["PERIODO"])
            h2_rows["PERIODO_ANO"] = h2_rows["PERIODO"].dt.year
            h2_rows["PERIODO_MES"] = h2_rows["PERIODO"].dt.month
            h2_rows["ID_MES"] = h2_rows["PERIODO"].dt.strftime("%Y%m")

            for c in df_sim.columns:
                if c not in h2_rows.columns:
                    if pd.api.types.is_datetime64_any_dtype(df_sim[c]):
                        h2_rows[c] = pd.NaT
                    elif pd.api.types.is_numeric_dtype(df_sim[c]):
                        h2_rows[c] = 0.0
                    else:
                        h2_rows[c] = np.nan
            h2_rows = h2_rows[[c for c in df_sim.columns if c in h2_rows.columns]].copy()
            df_sim = pd.concat([h2_rows, df_sim], ignore_index=True)
        except Exception as e:
            st.warning(f"No se pudo integrar ventas mes anterior como HISTORICO: {e}")

    # ============================================================
    # PRIOR YEAR (Ano Anterior) DATA — YoY comparison
    # (Ported from process_projection; essential for dashboards)
    # ============================================================
    if not ventas_aa.empty and "SKU_PRODUCTO" in ventas_aa.columns:
        try:
            # Map channels
            aa_chan_map = {"MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL"}
            ventas_aa["CANAL_STD"] = ventas_aa["COD_CANAL"].map(aa_chan_map).fillna("TIENDA")
            ventas_aa["PERIODO"] = pd.to_datetime(ventas_aa["PERIODO"])
            ventas_aa["MES"] = ventas_aa["PERIODO"].dt.month

            for c_num in ["CANTIDAD_AA", "NETO_AA", "APORTE_AA"]:
                if c_num in ventas_aa.columns:
                    ventas_aa[c_num] = pd.to_numeric(ventas_aa[c_num], errors="coerce").fillna(0)

            # Annual aggregation by SKU x Channel
            aa_annual = ventas_aa.groupby(["SKU_PRODUCTO", "CANAL_STD"]).agg(
                CANTIDAD_AA=("CANTIDAD_AA", "sum"),
                NETO_AA=("NETO_AA", "sum"),
                APORTE_AA=("APORTE_AA", "sum"),
            ).reset_index()

            # Pivot by channel and merge to df_sim
            for canal_std, suffix in [("TIENDA", "TIENDA"), ("ETAIL", "ETAIL"), ("MAYORISTA", "MAYOR")]:
                canal_data = aa_annual[aa_annual["CANAL_STD"] == canal_std].copy()
                if canal_data.empty:
                    continue
                canal_data = canal_data.rename(columns={
                    "CANTIDAD_AA": f"AA_UND_{suffix}",
                    "NETO_AA": f"AA_VN_{suffix}",
                    "APORTE_AA": f"AA_APORTE_{suffix}",
                })[["SKU_PRODUCTO", f"AA_UND_{suffix}", f"AA_VN_{suffix}", f"AA_APORTE_{suffix}"]]
                df_sim = df_sim.merge(canal_data, on="SKU_PRODUCTO", how="left")

            # Fill NaN and compute totals
            aa_cols = [c for c in df_sim.columns if c.startswith("AA_")]
            df_sim[aa_cols] = df_sim[aa_cols].fillna(0)

            df_sim["AA_VN_TOTAL"] = (
                df_sim.get("AA_VN_TIENDA", 0) + df_sim.get("AA_VN_ETAIL", 0) + df_sim.get("AA_VN_MAYOR", 0)
            )
            df_sim["AA_UND_TOTAL"] = (
                df_sim.get("AA_UND_TIENDA", 0) + df_sim.get("AA_UND_ETAIL", 0) + df_sim.get("AA_UND_MAYOR", 0)
            )
            df_sim["AA_APORTE_TOTAL"] = (
                df_sim.get("AA_APORTE_TIENDA", 0) + df_sim.get("AA_APORTE_ETAIL", 0) + df_sim.get("AA_APORTE_MAYOR", 0)
            )

            # Monthly breakdown for dashboard (save to session_state)
            aa_monthly = ventas_aa.groupby(["SKU_PRODUCTO", "CANAL_STD", "MES"]).agg(
                CANTIDAD_AA=("CANTIDAD_AA", "sum"),
                NETO_AA=("NETO_AA", "sum"),
                APORTE_AA=("APORTE_AA", "sum"),
            ).reset_index()
            st.session_state["aa_monthly"] = aa_monthly

            # --- Add SKUs from prior year NOT in the forecast ---
            forecast_skus = set(df_sim["SKU_PRODUCTO"].unique())
            aa_all_skus = set(ventas_aa["SKU_PRODUCTO"].unique())
            missing_skus = aa_all_skus - forecast_skus

            if missing_skus:
                missing_aa = aa_annual[aa_annual["SKU_PRODUCTO"].isin(missing_skus)].copy()
                missing_rows_list = []
                for sku in missing_skus:
                    sku_data = missing_aa[missing_aa["SKU_PRODUCTO"] == sku]
                    row = {
                        "SKU_PRODUCTO": sku,
                        "TIPO_DATO": "SOLO_AA",
                        "PERIODO": PERIODO_ACTUAL,
                    }
                    for _, r in sku_data.iterrows():
                        canal = r["CANAL_STD"]
                        suffix = {"TIENDA": "TIENDA", "ETAIL": "ETAIL", "MAYORISTA": "MAYOR"}.get(canal, canal)
                        row[f"AA_VN_{suffix}"] = r["NETO_AA"]
                        row[f"AA_UND_{suffix}"] = r["CANTIDAD_AA"]
                        row[f"AA_APORTE_{suffix}"] = r["APORTE_AA"]

                    row["AA_VN_TOTAL"] = sum(row.get(f"AA_VN_{s}", 0) for s in ["TIENDA", "ETAIL", "MAYOR"])
                    row["AA_UND_TOTAL"] = sum(row.get(f"AA_UND_{s}", 0) for s in ["TIENDA", "ETAIL", "MAYOR"])
                    row["AA_APORTE_TOTAL"] = sum(row.get(f"AA_APORTE_{s}", 0) for s in ["TIENDA", "ETAIL", "MAYOR"])
                    missing_rows_list.append(row)

                if missing_rows_list:
                    df_missing = pd.DataFrame(missing_rows_list)
                    # Merge maestra for structure columns
                    df_missing = df_missing.merge(
                        maestra[cols_maestra].drop_duplicates("SKU_PRODUCTO"),
                        on="SKU_PRODUCTO", how="left"
                    )
                    # Align schema
                    for c in df_sim.columns:
                        if c not in df_missing.columns:
                            if pd.api.types.is_datetime64_any_dtype(df_sim[c]):
                                df_missing[c] = pd.NaT
                            elif pd.api.types.is_numeric_dtype(df_sim[c]):
                                df_missing[c] = 0.0
                            else:
                                df_missing[c] = ""
                    df_missing = df_missing[[c for c in df_sim.columns if c in df_missing.columns]].copy()
                    df_sim = pd.concat([df_sim, df_missing], ignore_index=True)

        except Exception as e:
            st.warning(f"No se pudo integrar datos ano anterior: {e}")

    # Re-sort after adding HISTORICO + AA
    df_sim = df_sim.sort_values(["SKU_PRODUCTO", "PERIODO"]).reset_index(drop=True)
    df_sim_daily = df_sim_daily.sort_values(["SKU_PRODUCTO", "FECHA"]).reset_index(drop=True)

    return df_sim, df_sim_daily


# ============================================================================
# README SHEET DATA
# ============================================================================

README_DATA = [
    # ── HEADER ──
    ["Seccion", "Columna", "Descripcion"],
    # ── DESCRIPCION GENERAL ──
    ["ACERCA DE", "", "Este archivo contiene la proyeccion de stock simulada dia a dia para cada SKU."],
    ["ACERCA DE", "", "La simulacion parte del inventario real (snapshot Snowflake) y proyecta hacia adelante"],
    ["ACERCA DE", "", "consumiendo forecast de demanda, recibiendo compras planificadas y ETAs de importacion."],
    ["ACERCA DE", "", "El objetivo es estimar venta restricta (limitada por stock), venta perdida (lost sales),"],
    ["ACERCA DE", "", "posiciones de inventario futuras, margenes financieros y KPIs de InStock por canal."],
    ["ACERCA DE", "", ""],
    ["ACERCA DE", "", "HOJAS DEL ARCHIVO:"],
    ["ACERCA DE", "", "  README          - Esta hoja. Diccionario de datos y logica de proyeccion."],
    ["ACERCA DE", "", "  DATA            - Detalle completo SKU x Periodo (todas las columnas)."],
    ["ACERCA DE", "", "  Resumen SKU     - Totales anuales agrupados por SKU (VN, Aporte, Margen, InStock)."],
    ["ACERCA DE", "", "  Resumen Area-Linea - Totales anuales agrupados por Area y Linea (nivel gerencial)."],
    ["ACERCA DE", "", "  Pivot VN Mensual - Tabla cruzada de Venta Neta por Area-Linea x Mes."],
    ["ACERCA DE", "", ""],
    # ── IDENTIFICADORES ──
    ["IDs", "SKU_PRODUCTO", "Codigo unico del producto (clave primaria junto con PERIODO)"],
    ["IDs", "PERIODO", "Primer dia del mes proyectado en formato YYYY-MM-01"],
    ["IDs", "PERIODO_ANO", "Ano del periodo (ej: 2026)"],
    ["IDs", "PERIODO_MES", "Numero de mes del periodo (1-12)"],
    ["IDs", "ID_MES", "Identificador ordinal del mes dentro de la simulacion (1 = primer mes, 2 = segundo, etc.)"],
    ["IDs", "TIPO_DATO", "Clasificacion temporal de la fila (ver detalle abajo)"],
    ["IDs", "", "  HISTORICO   = Meses completos pasados con ventas 100% reales"],
    ["IDs", "", "  REAL+FC     = Mes actual: venta real acumulada (MTD) + simulacion dias restantes"],
    ["IDs", "", "  PROYECCION  = Meses futuros puros basados en forecast y simulacion de stock"],
    ["IDs", "", "  SOLO_AA     = SKUs que vendieron el ano anterior pero no tienen forecast este ano"],
    ["IDs", "", ""],
    # ── ABASTECIMIENTO / SUPPLY ──
    ["Abastecimiento", "FORECAST_COMPRA", "Unidades que se planea recibir en el mes segun plan de compras (ordenes de compra confirmadas o planificadas)"],
    ["Abastecimiento", "ETA", "Unidades en transito via COMEX (importacion) con fecha estimada de arribo al CD, ajustada con lag operativo segun procedencia: productos de Asia +15 dias, Europa +10 dias, Latam +5 dias"],
    ["Abastecimiento", "", ""],
    # ── INVENTARIO ──
    ["Inventario", "STOCK_INICIAL_CD", "Stock fisico en Centro de Distribucion al inicio del dia 1 del mes. Para el primer mes = snapshot real de Snowflake. Para meses siguientes = STOCK_FINAL_CD del mes anterior"],
    ["Inventario", "STOCK_INICIAL_TIENDA", "Stock fisico en tiendas al inicio del dia 1 del mes. Para el primer mes = snapshot real. Para meses siguientes = STOCK_FINAL_TIENDA del mes anterior"],
    ["Inventario", "STOCK_INICIAL_TOTAL", "STOCK_INICIAL_CD + STOCK_INICIAL_TIENDA"],
    ["Inventario", "STOCK_DISPONIBLE", "Stock total disponible para la venta durante el mes: STOCK_INICIAL_CD + FORECAST_COMPRA + ETA (antes de transferencias y ventas)"],
    ["Inventario", "DISP_CD_POST_TRANSFER", "Stock CD disponible despues de transferir a tiendas, antes de vender por canales directos (Etail, Mayorista)"],
    ["Inventario", "STOCK_FINAL_CD", "Stock en CD al cierre del ultimo dia del mes, tras todas las operaciones"],
    ["Inventario", "STOCK_FINAL_TIENDA", "Stock en tiendas al cierre del ultimo dia del mes"],
    ["Inventario", "STOCK_FINAL_TOTAL", "STOCK_FINAL_CD + STOCK_FINAL_TIENDA"],
    ["Inventario", "", ""],
    # ── COBERTURA Y RIESGO ──
    ["Cobertura", "MOI_COBERTURA", "Months of Inventory: STOCK_FINAL_TOTAL / Promedio demanda 3 meses siguientes. Indica cuantos meses de venta cubre el inventario actual"],
    ["Cobertura", "MES_QUIEBRE", "Primer periodo (YYYY-MM) donde el stock total llega a cero. Si no quiebra = vacio"],
    ["Cobertura", "MESES_HASTA_QUIEBRE", "Cantidad de meses desde hoy hasta el quiebre. 999 = sin quiebre en el horizonte simulado"],
    ["Cobertura", "", ""],
    # ── PERFIL TIENDAS Y TRANSFERENCIAS ──
    ["Perfil Tiendas", "PERFIL_TIENDAS", "Stock minimo target que debe haber en tiendas (exhibicion + buffer). Calculado como promedio historico de stock tienda por SKU"],
    ["Perfil Tiendas", "NECESIDAD_TIENDA", "Unidades necesarias para abastecer tiendas = max(0, PERFIL_TIENDAS + demanda_dia_siguiente - STOCK_TIENDA_ACTUAL). Incluye el perfil + cobertura de demanda"],
    ["Perfil Tiendas", "CARGA_REAL", "Unidades efectivamente transferidas CD -> Tienda = min(NECESIDAD, Stock CD disponible). Si el CD no tiene suficiente, se transfiere lo que haya"],
    ["Perfil Tiendas", "DEFICIT_PERFIL_UND", "Unidades faltantes para cubrir el perfil: max(0, NECESIDAD - CARGA_REAL). Indica cuanto perfil no se pudo cubrir por falta de stock CD"],
    ["Perfil Tiendas", "QUIEBRE_POR_PERFIL", "Flag binario: 1 si DEFICIT_PERFIL_UND > 0 en algun dia del mes (no se cumple el perfil de exhibicion)"],
    ["Perfil Tiendas", "AJUSTE_COMPRA_SUGERIDO_UND", "Unidades adicionales sugeridas de compra para cubrir el deficit de perfil en el horizonte proyectado"],
    ["Perfil Tiendas", "VN_INCREMENTAL_PERFIL", "Venta neta adicional estimada ($) si se cubriera el deficit de perfil (lost sales recuperables)"],
    ["Perfil Tiendas", "", ""],
    # ── DEMANDA SIMULADA ──
    ["Demanda", "DEMANDA_SIM_TIENDA", "Demanda simulada canal Tienda (Retail/Minor). En mes actual = Forecast - venta real MTD. En meses futuros = Forecast completo. Distribuida diariamente de forma uniforme"],
    ["Demanda", "DEMANDA_SIM_ETAIL", "Demanda simulada canal E-commerce. Misma logica de ajuste MTD para mes actual"],
    ["Demanda", "DEMANDA_SIM_MAYOR", "Demanda simulada canal Mayorista. Misma logica de ajuste MTD para mes actual"],
    ["Demanda", "DEMANDA_TOTAL", "DEMANDA_SIM_TIENDA + DEMANDA_SIM_ETAIL + DEMANDA_SIM_MAYOR"],
    ["Demanda", "", ""],
    # ── VENTA FULFILLMENT (RESTRICTA POR STOCK) ──
    ["Venta Restricta", "VENTA_FUL_TIENDA_UND", "Unidades vendidas en Tienda = min(demanda_dia, stock_tienda_apertura + carga_dia). La venta se limita al stock fisicamente disponible en tienda cada dia"],
    ["Venta Restricta", "VENTA_FUL_ETAIL_UND", "Unidades vendidas E-commerce = min(demanda_dia, proporcion de stock CD post-transfer). El stock CD se pro-ratea entre Etail y Mayorista segun proporcion de demanda"],
    ["Venta Restricta", "VENTA_FUL_MAYOR_UND", "Unidades vendidas Mayorista = min(demanda_dia, proporcion de stock CD post-transfer). Pro-rateo con Etail"],
    ["Venta Restricta", "", "NOTA: En meses HISTORICO y REAL+FC (parte real), la venta restricta = venta real (no hay restriccion de stock porque ya ocurrio)"],
    ["Venta Restricta", "", ""],
    # ── VENTA PERDIDA (LOST SALES) ──
    ["Venta Perdida", "LOST_SALES_TIENDA", "Unidades de demanda no satisfecha en Tienda = max(0, DEMANDA_SIM_TIENDA - VENTA_FUL_TIENDA_UND). Venta que se pierde por falta de stock"],
    ["Venta Perdida", "LOST_SALES_ETAIL", "Unidades de demanda no satisfecha en Etail = LOST_SALES_CD x (DEMANDA_SIM_ETAIL / (DEMANDA_SIM_ETAIL + DEMANDA_SIM_MAYOR))"],
    ["Venta Perdida", "LOST_SALES_MAYOR", "Unidades de demanda no satisfecha en Mayorista = LOST_SALES_CD x (DEMANDA_SIM_MAYOR / (DEMANDA_SIM_ETAIL + DEMANDA_SIM_MAYOR))"],
    ["Venta Perdida", "LOST_SALES_CD", "Unidades de demanda no satisfecha desde CD (Etail + Mayorista combinados)"],
    ["Venta Perdida", "LOST_SALES_TOTAL", "LOST_SALES_TIENDA + LOST_SALES_CD"],
    ["Venta Perdida", "", ""],
    # ── INSTOCK KPIs ──
    ["InStock", "INSTOCK_CD", "Proporcion de dias del mes donde el stock CD cubrio la demanda diaria completa de los 3 canales (transferencia a tienda + Etail + Mayor). Valor 0 a 1 (ej: 0.87 = 87% de los dias)"],
    ["InStock", "INSTOCK_TIENDA", "Proporcion de dias del mes donde el stock en tienda cubrio la demanda diaria de venta retail. Valor 0 a 1"],
    ["InStock", "INSTOCK_CIA", "Proporcion de dias del mes donde el stock total (CD+Tienda) cubrio la demanda total de los 3 canales. Valor 0 a 1"],
    ["InStock", "INSTOCK_DIAS_CD", "Cantidad de dias con InStock CD = 1 (entero). Para calculo agregado en Excel: InStock Agregado = SUM(INSTOCK_DIAS_CD) / SUM(DIAS_MES) sobre multiples SKUs"],
    ["InStock", "INSTOCK_DIAS_TIENDA", "Cantidad de dias con InStock Tienda = 1 (entero). Mismo uso para calculo agregado"],
    ["InStock", "INSTOCK_DIAS_CIA", "Cantidad de dias con InStock Compania = 1 (entero). Mismo uso para calculo agregado"],
    ["InStock", "DIAS_MES", "Total dias calendario del mes (28/29/30/31). Denominador para calcular InStock agregado ponderado por dias"],
    ["InStock", "", "EJEMPLO CALCULO AGREGADO: Si 3 SKUs tienen INSTOCK_DIAS_CIA = 15, 20, 31 y DIAS_MES = 31, 31, 31:"],
    ["InStock", "", "  InStock CIA Agregado = (15 + 20 + 31) / (31 + 31 + 31) = 66/93 = 71.0%"],
    ["InStock", "", ""],
    # ── COSTOS ──
    ["Costos", "COSTO_UNITARIO", "Costo unitario usado para calcular COGS. Se determina segun jerarquia de fallback (ver detalle abajo)"],
    ["Costos", "ORIGEN_COSTO", "Fuente del costo: ULTIMO_COSTO (del sistema), LANDED_CALC (FOB x Factor x TC), LANDED_FACTOR_IMPUTADO (con factor estimado), SIN_COSTO"],
    ["Costos", "FACTOR_IMPORTACION", "Multiplicador que convierte FOB USD a costo CLP puesto en bodega. Incluye flete, seguro, arancel, gastos portuarios. Rango tipico: 1.3 a 2.5"],
    ["Costos", "COSTO_FOB_USD", "Costo FOB (Free on Board) en dolares segun maestra de productos"],
    ["Costos", "ULTIMO_COSTO", "Ultimo costo registrado en el sistema ERP (en CLP). Primera prioridad en el fallback"],
    ["Costos", "FLAG_SIN_COSTO", "Flag booleano: True si el SKU no tiene costo valido (ORIGEN_COSTO=SIN_COSTO o COSTO_UNITARIO<=0). Util para filtrar y limpiar calculos de margen"],
    ["Costos", "", "JERARQUIA DE FALLBACK para COSTO_UNITARIO:"],
    ["Costos", "", "  1) ULTIMO_COSTO (del sistema ERP, si existe y > 0)"],
    ["Costos", "", "  2) COSTO_FOB_USD x FACTOR_IMPORTACION x 950 (TC USD/CLP referencia)"],
    ["Costos", "", "  3) Si falta FACTOR_IMPORTACION: se imputa promedio por Sublinea+Marca, luego Linea+Marca, luego Area+Marca, luego Sublinea, Linea, Area"],
    ["Costos", "", "  4) 0 (SIN_COSTO) como ultimo recurso"],
    ["Costos", "", ""],
    # ── PRECIOS POR CANAL ──
    ["Precios", "PRECIO_NETO_TIENDA", "Precio neto teorico Tienda (de planilla cargada por el usuario, si disponible)"],
    ["Precios", "PRECIO_NETO_ETAIL", "Precio neto teorico E-commerce (de planilla)"],
    ["Precios", "PRECIO_NETO_MAYOR", "Precio neto teorico Mayorista (de planilla)"],
    ["Precios", "PRECIO_MTD_TIENDA", "Precio promedio real month-to-date Tienda (de transacciones reales del mes en curso)"],
    ["Precios", "PRECIO_MTD_ETAIL", "Precio promedio real month-to-date E-commerce"],
    ["Precios", "PRECIO_MTD_MAYORISTA", "Precio promedio real month-to-date Mayorista"],
    ["Precios", "PRECIO_USADO_TIENDA", "Precio efectivamente usado para calcular VN Tienda. Resultado de la jerarquia de fallback"],
    ["Precios", "PRECIO_USADO_ETAIL", "Precio efectivamente usado para calcular VN E-commerce"],
    ["Precios", "PRECIO_USADO_MAYOR", "Precio efectivamente usado para calcular VN Mayorista"],
    ["Precios", "ORIGEN_PRECIO_TIENDA", "Fuente del precio Tienda: PLANILLA, PROMEDIO_MTD, PROMEDIO_MES_ANT, CROSS_ETAIL/MAYOR, HISTORICO, SIN_PRECIO"],
    ["Precios", "ORIGEN_PRECIO_ETAIL", "Fuente del precio E-commerce (mismas opciones)"],
    ["Precios", "ORIGEN_PRECIO_MAYOR", "Fuente del precio Mayorista (mismas opciones)"],
    ["Precios", "", "JERARQUIA DE FALLBACK para precios por canal:"],
    ["Precios", "", "  1) PRECIO_NETO (planilla del usuario, si se cargo)"],
    ["Precios", "", "  2) PRECIO_MTD (promedio real del mes actual, de transacciones reales)"],
    ["Precios", "", "  3) Precio promedio del mes anterior (si no hay ventas este mes)"],
    ["Precios", "", "  4) Cross-channel: precio de otro canal (ej: si Etail no tiene, usa Tienda)"],
    ["Precios", "", "  5) 0 (SIN_PRECIO) — el SKU queda con VN = 0 en ese canal"],
    ["Precios", "", ""],
    # ── FINANCIERO RESTRICTO (limitado por stock) ──
    ["Fin. Restricto", "VN_RES_TIENDA", "Venta Neta Restricta Tienda = VENTA_FUL_TIENDA_UND x PRECIO_USADO_TIENDA. Es la venta real que se puede concretar dado el stock disponible"],
    ["Fin. Restricto", "VN_RES_ETAIL", "Venta Neta Restricta E-commerce = VENTA_FUL_ETAIL_UND x PRECIO_USADO_ETAIL"],
    ["Fin. Restricto", "VN_RES_MAYOR", "Venta Neta Restricta Mayorista = VENTA_FUL_MAYOR_UND x PRECIO_USADO_MAYOR"],
    ["Fin. Restricto", "VN_RES_TOTAL", "VN_RES_TIENDA + VN_RES_ETAIL + VN_RES_MAYOR"],
    ["Fin. Restricto", "COGS_RES_TIENDA", "Costo de Venta Restricto Tienda = VENTA_FUL_TIENDA_UND x COSTO_UNITARIO"],
    ["Fin. Restricto", "COGS_RES_ETAIL", "Costo de Venta Restricto E-commerce"],
    ["Fin. Restricto", "COGS_RES_MAYOR", "Costo de Venta Restricto Mayorista"],
    ["Fin. Restricto", "COGS_RES_TOTAL", "COGS_RES_TIENDA + COGS_RES_ETAIL + COGS_RES_MAYOR"],
    ["Fin. Restricto", "APORTE_RES_TIENDA", "Margen Bruto Restricto Tienda = VN_RES_TIENDA - COGS_RES_TIENDA"],
    ["Fin. Restricto", "APORTE_RES_ETAIL", "Margen Bruto Restricto E-commerce"],
    ["Fin. Restricto", "APORTE_RES_MAYOR", "Margen Bruto Restricto Mayorista"],
    ["Fin. Restricto", "APORTE_RES_TOTAL", "APORTE_RES_TIENDA + APORTE_RES_ETAIL + APORTE_RES_MAYOR"],
    ["Fin. Restricto", "MARGEN_RES_TIENDA", "Margen % Tienda = APORTE_RES_TIENDA / VN_RES_TIENDA (0 si VN=0)"],
    ["Fin. Restricto", "MARGEN_RES_ETAIL", "Margen % E-commerce"],
    ["Fin. Restricto", "MARGEN_RES_MAYOR", "Margen % Mayorista"],
    ["Fin. Restricto", "MARGEN_RES_TOTAL", "Margen % Total = APORTE_RES_TOTAL / VN_RES_TOTAL"],
    ["Fin. Restricto", "", ""],
    # ── FINANCIERO IRRESTRICTO (potencial sin restriccion de stock) ──
    ["Fin. Irrestricto", "VN_IRR_TIENDA", "Venta Neta Irrestricta Tienda = DEMANDA_SIM_TIENDA x PRECIO_USADO_TIENDA. Representa la venta potencial si hubiera stock infinito"],
    ["Fin. Irrestricto", "VN_IRR_ETAIL", "Venta Neta Irrestricta E-commerce"],
    ["Fin. Irrestricto", "VN_IRR_MAYOR", "Venta Neta Irrestricta Mayorista"],
    ["Fin. Irrestricto", "VN_IRR_TOTAL", "VN_IRR_TIENDA + VN_IRR_ETAIL + VN_IRR_MAYOR"],
    ["Fin. Irrestricto", "COGS_IRR_TIENDA", "COGS Irrestricto Tienda = DEMANDA_SIM_TIENDA x COSTO_UNITARIO"],
    ["Fin. Irrestricto", "COGS_IRR_ETAIL", "COGS Irrestricto E-commerce"],
    ["Fin. Irrestricto", "COGS_IRR_MAYOR", "COGS Irrestricto Mayorista"],
    ["Fin. Irrestricto", "COGS_IRR_TOTAL", "COGS_IRR_TIENDA + COGS_IRR_ETAIL + COGS_IRR_MAYOR"],
    ["Fin. Irrestricto", "APORTE_IRR_TIENDA", "Margen Bruto Irrestricto Tienda"],
    ["Fin. Irrestricto", "APORTE_IRR_ETAIL", "Margen Bruto Irrestricto E-commerce"],
    ["Fin. Irrestricto", "APORTE_IRR_MAYOR", "Margen Bruto Irrestricto Mayorista"],
    ["Fin. Irrestricto", "APORTE_IRR_TOTAL", "APORTE_IRR_TIENDA + APORTE_IRR_ETAIL + APORTE_IRR_MAYOR"],
    ["Fin. Irrestricto", "MARGEN_IRR_TIENDA", "Margen % Irrestricto Tienda"],
    ["Fin. Irrestricto", "MARGEN_IRR_ETAIL", "Margen % Irrestricto E-commerce"],
    ["Fin. Irrestricto", "MARGEN_IRR_MAYOR", "Margen % Irrestricto Mayorista"],
    ["Fin. Irrestricto", "MARGEN_IRR_TOTAL", "Margen % Irrestricto Total"],
    ["Fin. Irrestricto", "", "NOTA: Comparar VN_RES vs VN_IRR permite cuantificar el impacto financiero de las roturas de stock (cuanto dejamos de vender por falta de inventario)"],
    ["Fin. Irrestricto", "", ""],
    # ── FINANCIERO VENTA PERDIDA (Lost Sales valorizado) ──
    ["Fin. Lost Sales", "VN_LOST_TIENDA", "Venta Neta Perdida Tienda = LOST_SALES_TIENDA x PRECIO_USADO_TIENDA. Cuantifica en $ la venta que se pierde por falta de stock"],
    ["Fin. Lost Sales", "VN_LOST_ETAIL", "Venta Neta Perdida E-commerce = LOST_SALES_ETAIL x PRECIO_USADO_ETAIL"],
    ["Fin. Lost Sales", "VN_LOST_MAYOR", "Venta Neta Perdida Mayorista = LOST_SALES_MAYOR x PRECIO_USADO_MAYOR"],
    ["Fin. Lost Sales", "VN_LOST_TOTAL", "VN_LOST_TIENDA + VN_LOST_ETAIL + VN_LOST_MAYOR"],
    ["Fin. Lost Sales", "COGS_LOST_TIENDA", "Costo de Venta Perdida Tienda = LOST_SALES_TIENDA x COSTO_UNITARIO"],
    ["Fin. Lost Sales", "COGS_LOST_ETAIL", "Costo de Venta Perdida E-commerce"],
    ["Fin. Lost Sales", "COGS_LOST_MAYOR", "Costo de Venta Perdida Mayorista"],
    ["Fin. Lost Sales", "COGS_LOST_TOTAL", "COGS_LOST_TIENDA + COGS_LOST_ETAIL + COGS_LOST_MAYOR"],
    ["Fin. Lost Sales", "APORTE_LOST_TIENDA", "Margen Bruto Perdido Tienda = VN_LOST - COGS_LOST"],
    ["Fin. Lost Sales", "APORTE_LOST_ETAIL", "Margen Bruto Perdido E-commerce"],
    ["Fin. Lost Sales", "APORTE_LOST_MAYOR", "Margen Bruto Perdido Mayorista"],
    ["Fin. Lost Sales", "APORTE_LOST_TOTAL", "APORTE_LOST_TIENDA + APORTE_LOST_ETAIL + APORTE_LOST_MAYOR"],
    ["Fin. Lost Sales", "MARGEN_LOST_TIENDA", "Margen % Perdido Tienda = APORTE_LOST / VN_LOST (0 si VN=0)"],
    ["Fin. Lost Sales", "MARGEN_LOST_ETAIL", "Margen % Perdido E-commerce"],
    ["Fin. Lost Sales", "MARGEN_LOST_MAYOR", "Margen % Perdido Mayorista"],
    ["Fin. Lost Sales", "MARGEN_LOST_TOTAL", "Margen % Perdido Total"],
    ["Fin. Lost Sales", "", "NOTA: VN_LOST cuantifica el impacto financiero directo de las roturas de stock por canal. Diferencia con IRR: VN_LOST = solo la venta perdida, VN_IRR = venta potencial total (restricta + perdida)."],
    ["Fin. Lost Sales", "", ""],
    # ── ALERTAS ──
    ["Alertas", "WARNING_FC_REAL", "LOW_FORECAST si el forecast es < 50% de la venta real de enero, HIGH_FORECAST si > 200%. Alerta de calidad del forecast"],
    ["Alertas", "", ""],
    # ── DIMENSIONES DEL PRODUCTO ──
    ["Dimensiones", "SKU_NOM_PRODUCTO", "Nombre comercial del producto"],
    ["Dimensiones", "AREA", "Area de negocio: NURSERY, GEAR, etc."],
    ["Dimensiones", "LINEA", "Linea de producto dentro del area (ej: COCHES, SILLAS AUTO, CUNAS)"],
    ["Dimensiones", "SUBLINEA", "Subdivision de la linea (ej: COCHES TRAVEL SYSTEM, COCHES LIVIANOS)"],
    ["Dimensiones", "MARCA", "Marca comercial del producto (ej: INFANTI, SAFETY 1ST, QUINNY)"],
    ["Dimensiones", "MODELO", "Modelo especifico dentro de la marca"],
    ["Dimensiones", "PROCEDENCIA", "Origen: NACIONAL o IMPORTADO"],
    ["Dimensiones", "PROVEEDOR", "Nombre del proveedor"],
    ["Dimensiones", "COD_PROVEEDOR", "Codigo del proveedor en el sistema"],
    ["Dimensiones", "MIX_OFICIAL", "Clasificacion de portafolio: MIX (activo), IN & OUT (temporal), FUERA DE MIX (inactivo), DESCONTINUADO"],
    ["Dimensiones", "", ""],
    # ── ANO ANTERIOR (YoY) ──
    ["Ano Anterior", "AA_VN_TIENDA", "Venta Neta Tienda del ano anterior completo (12 meses)"],
    ["Ano Anterior", "AA_VN_ETAIL", "Venta Neta E-commerce del ano anterior"],
    ["Ano Anterior", "AA_VN_MAYOR", "Venta Neta Mayorista del ano anterior"],
    ["Ano Anterior", "AA_VN_TOTAL", "VN Total del ano anterior (suma de los 3 canales)"],
    ["Ano Anterior", "AA_UND_TIENDA", "Unidades vendidas Tienda - ano anterior"],
    ["Ano Anterior", "AA_UND_ETAIL", "Unidades vendidas E-commerce - ano anterior"],
    ["Ano Anterior", "AA_UND_MAYOR", "Unidades vendidas Mayorista - ano anterior"],
    ["Ano Anterior", "AA_UND_TOTAL", "Unidades vendidas Total - ano anterior"],
    ["Ano Anterior", "AA_APORTE_TIENDA", "Aporte (margen bruto) Tienda - ano anterior"],
    ["Ano Anterior", "AA_APORTE_ETAIL", "Aporte E-commerce - ano anterior"],
    ["Ano Anterior", "AA_APORTE_MAYOR", "Aporte Mayorista - ano anterior"],
    ["Ano Anterior", "AA_APORTE_TOTAL", "Aporte Total - ano anterior"],
    ["Ano Anterior", "", "NOTA: TIPO_DATO = SOLO_AA identifica SKUs que vendieron el ano anterior pero NO tienen forecast este ano. Permite detectar productos discontinuados con venta historica."],
    ["Ano Anterior", "", ""],
    # ── LOGICA DETALLADA DE LA SIMULACION ──
    ["", "", ""],
    ["LOGICA SIMULACION", "", "===================================================================="],
    ["LOGICA SIMULACION", "", "MOTOR DE SIMULACION DE STOCK — PASO A PASO"],
    ["LOGICA SIMULACION", "", "===================================================================="],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "La simulacion opera dia a dia (daily) para cada SKU, iterando desde el dia"],
    ["LOGICA SIMULACION", "", "siguiente al ultimo snapshot de inventario real en Snowflake."],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "FUENTES DE DATOS (INPUTS):"],
    ["LOGICA SIMULACION", "", "  - Stock Real: Snapshot diario del inventario por SKU y bodega (CD vs Tienda) desde Snowflake"],
    ["LOGICA SIMULACION", "", "  - Forecast de Demanda: Archivo Excel cargado por el usuario con proyeccion mensual por SKU y canal"],
    ["LOGICA SIMULACION", "", "  - Plan de Compras: Archivo Excel con unidades a recibir por mes (ordenes de compra planificadas)"],
    ["LOGICA SIMULACION", "", "  - COMEX (Importaciones): POs en transito con fecha ETA estimada, extraidas de Snowflake"],
    ["LOGICA SIMULACION", "", "  - Maestra de Productos: Atributos del SKU (costo, factor importacion, clasificacion comercial)"],
    ["LOGICA SIMULACION", "", "  - Precios Netos: Opcional. Planilla de precios teoricos por canal. Si no se carga, se usan promedios reales"],
    ["LOGICA SIMULACION", "", "  - Ventas Reales: MTD del mes actual + mes anterior (para precios fallback) + ano anterior (para YoY)"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "PASO 1: INICIALIZACION"],
    ["LOGICA SIMULACION", "", "  - Se toma la fecha mas reciente del snapshot de stock en Snowflake (ej: 2026-02-18)"],
    ["LOGICA SIMULACION", "", "  - La simulacion arranca el DIA SIGUIENTE a esa fecha (ej: 2026-02-19)"],
    ["LOGICA SIMULACION", "", "  - Stock inicial primer dia = stock real del snapshot (separado CD y Tienda)"],
    ["LOGICA SIMULACION", "", "  - El forecast mensual se distribuye uniformemente entre los dias del mes"],
    ["LOGICA SIMULACION", "", "  - Para el mes actual, la demanda simulada = Forecast - Venta Real MTD (lo que falta por vender)"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "PASO 2: CADA DIA SE EJECUTA (por cada SKU):"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "  2a. RECEPCION DE MERCADERIA"],
    ["LOGICA SIMULACION", "", "      Stock_CD = Stock_CD_apertura + Forecast_Compra_dia + ETA_dia"],
    ["LOGICA SIMULACION", "", "      (Las compras y ETAs se distribuyen uniformemente en los dias del mes de arribo)"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "  2b. TRANSFERENCIA CD -> TIENDA (Abastecimiento de Perfil)"],
    ["LOGICA SIMULACION", "", "      Necesidad = max(0, PERFIL_TIENDAS + demanda_tienda_dia_siguiente - Stock_Tienda_apertura)"],
    ["LOGICA SIMULACION", "", "      Carga = min(Necesidad, Stock_CD_disponible)"],
    ["LOGICA SIMULACION", "", "      Stock_CD -= Carga"],
    ["LOGICA SIMULACION", "", "      Stock_Tienda += Carga"],
    ["LOGICA SIMULACION", "", "      SUPUESTO: El perfil de tiendas representa el stock minimo de exhibicion que siempre debe"],
    ["LOGICA SIMULACION", "", "      estar disponible. La necesidad incluye tanto el perfil como la demanda esperada del dia siguiente."],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "  2c. VENTA TIENDA (Fulfillment)"],
    ["LOGICA SIMULACION", "", "      Venta_Tienda = min(Demanda_Tienda_dia, Stock_Tienda_actual)"],
    ["LOGICA SIMULACION", "", "      Stock_Tienda -= Venta_Tienda"],
    ["LOGICA SIMULACION", "", "      Lost_Sales_Tienda = max(0, Demanda - Venta_Tienda)"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "  2d. VENTA CD (Fulfillment Etail + Mayorista)"],
    ["LOGICA SIMULACION", "", "      Demanda_CD_total = Demanda_Etail_dia + Demanda_Mayor_dia"],
    ["LOGICA SIMULACION", "", "      Si Demanda_CD_total <= Stock_CD_post_transfer:"],
    ["LOGICA SIMULACION", "", "          Venta_Etail = Demanda_Etail_dia (satisfaccion completa)"],
    ["LOGICA SIMULACION", "", "          Venta_Mayor = Demanda_Mayor_dia"],
    ["LOGICA SIMULACION", "", "      Si no (stock insuficiente):"],
    ["LOGICA SIMULACION", "", "          Se pro-ratea proporcionalmente entre canales segun su demanda"],
    ["LOGICA SIMULACION", "", "          Venta_Etail = Stock_CD x (Demanda_Etail / Demanda_CD_total)"],
    ["LOGICA SIMULACION", "", "          Venta_Mayor = Stock_CD x (Demanda_Mayor / Demanda_CD_total)"],
    ["LOGICA SIMULACION", "", "      Stock_CD -= (Venta_Etail + Venta_Mayor)"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "  2e. CIERRE DEL DIA"],
    ["LOGICA SIMULACION", "", "      Stock_Final_CD = Stock_CD_post_ventas"],
    ["LOGICA SIMULACION", "", "      Stock_Final_Tienda = Stock_Tienda_post_ventas"],
    ["LOGICA SIMULACION", "", "      InStock_CD = 1 si Stock_CD pudo cubrir toda la demanda (transferencia + venta directa), 0 si no"],
    ["LOGICA SIMULACION", "", "      InStock_Tienda = 1 si Stock_Tienda pudo cubrir toda la demanda retail, 0 si no"],
    ["LOGICA SIMULACION", "", "      InStock_CIA = 1 si Stock_Total pudo cubrir demanda total de 3 canales, 0 si no"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "  2f. CARRY FORWARD"],
    ["LOGICA SIMULACION", "", "      Stock apertura dia siguiente = Stock cierre dia anterior"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "PASO 3: AGREGACION DIARIA -> MENSUAL"],
    ["LOGICA SIMULACION", "", "  - Flujos (ventas, compras, demanda, lost sales) = SUMA del mes"],
    ["LOGICA SIMULACION", "", "  - Stock apertura = valor del PRIMER dia del mes"],
    ["LOGICA SIMULACION", "", "  - Stock cierre = valor del ULTIMO dia del mes"],
    ["LOGICA SIMULACION", "", "  - InStock % = PROMEDIO de flags diarios (0/1) del mes"],
    ["LOGICA SIMULACION", "", "  - InStock Dias = SUMA de flags diarios (cantidad de dias con stock)"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "PASO 4: COMBINACION REAL+FC (MES ACTUAL)"],
    ["LOGICA SIMULACION", "", "  - Para el mes en curso, se combinan datos reales (MTD) con la simulacion (dias restantes):"],
    ["LOGICA SIMULACION", "", "    * Ventas (unidades y $): SUMA de real MTD + proyectado restante"],
    ["LOGICA SIMULACION", "", "    * Stock y demanda: Solo de la simulacion (posicion futura)"],
    ["LOGICA SIMULACION", "", "    * COGS y margenes: Recalculados sobre ventas combinadas"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "PASO 5: CALCULO FINANCIERO"],
    ["LOGICA SIMULACION", "", "  - VN = Unidades vendidas x Precio (con fallback jerarquico)"],
    ["LOGICA SIMULACION", "", "  - COGS = Unidades vendidas x Costo Unitario (con fallback jerarquico)"],
    ["LOGICA SIMULACION", "", "  - Aporte = VN - COGS"],
    ["LOGICA SIMULACION", "", "  - Margen = Aporte / VN (0 si VN = 0)"],
    ["LOGICA SIMULACION", "", "  - Se calcula tanto restricto (con stock real) como irrestricto (sin restriccion)"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "SUPUESTOS IMPORTANTES:"],
    ["LOGICA SIMULACION", "", "  1) El forecast se distribuye uniformemente entre los dias del mes (no hay estacionalidad intra-mes)"],
    ["LOGICA SIMULACION", "", "  2) Las compras se reciben el dia 1 del mes planificado (distribuidas uniformemente)"],
    ["LOGICA SIMULACION", "", "  3) ETAs de importacion tienen lag operativo segun procedencia (Asia +15d, Europa +10d, Latam +5d)"],
    ["LOGICA SIMULACION", "", "  4) El perfil de tiendas es estatico durante la simulacion (promedio historico)"],
    ["LOGICA SIMULACION", "", "  5) No hay reposicion automatica durante el mes (solo compras planificadas en el plan de compras)"],
    ["LOGICA SIMULACION", "", "  6) Lost sales son definitivas: demanda no satisfecha no se recupera en dias posteriores"],
    ["LOGICA SIMULACION", "", "  7) Pro-rateo CD: si no alcanza stock para Etail + Mayor, se reparte proporcionalmente"],
    ["LOGICA SIMULACION", "", "  8) Tipo de cambio referencia: USD/CLP = 950 para calculo de landed cost (cuando falta costo sistema)"],
    ["LOGICA SIMULACION", "", "  9) Factor de importacion: si no existe para un SKU, se imputa promedio por Sublinea, Linea o Area"],
    ["LOGICA SIMULACION", "", " 10) Para HISTORICO, la venta restricta = venta real (no hay simulacion, es dato observado)"],
    ["LOGICA SIMULACION", "", ""],
    ["LOGICA SIMULACION", "", "MODELO DE 3 CANALES:"],
    ["LOGICA SIMULACION", "", "  - TIENDA (Retail/Minor): Venta en tiendas fisicas. Se abastece desde CD via transferencias."],
    ["LOGICA SIMULACION", "", "  - ETAIL (E-commerce): Venta online. Se despacha directamente desde CD."],
    ["LOGICA SIMULACION", "", "  - MAYOR (Mayorista): Venta a distribuidores. Se despacha directamente desde CD."],
    ["LOGICA SIMULACION", "", "  Los canales ETAIL y MAYOR compiten por el mismo stock CD (pro-rateo si falta)."],
    ["LOGICA SIMULACION", "", "  TIENDA tiene su propio stock, abastecido por transferencias diarias desde CD."],
]


# ============================================================================
# EXCEL SUMMARY SHEETS (Resumen por SKU / Area-Linea / Pivot Mensual)
# ============================================================================

# Styling constants for summary sheets
_HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
_HDR_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
_HDR_FILL_ALT = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
_TOTAL_FONT = Font(bold=True, size=10)
_TOTAL_FILL = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")
_THIN_BORDER = Border(
    bottom=Side(style="thin", color="B4C6E7"),
)
_FMT_NUMBER = '#,##0'
_FMT_MONEY = '$#,##0'
_FMT_PCT = '0.0%'


def _style_summary_ws(ws, df, money_cols=None, pct_cols=None, number_cols=None):
    """Apply professional formatting to a summary worksheet.

    Args:
        ws: openpyxl Worksheet (data already written by pandas).
        df: The DataFrame that was written (to know dtypes/columns).
        money_cols: Column names that should use money format.
        pct_cols: Column names that should use percentage format.
        number_cols: Column names that should use integer number format.
    """
    money_cols = set(money_cols or [])
    pct_cols = set(pct_cols or [])
    number_cols = set(number_cols or [])

    col_names = list(df.columns)

    # Style header row
    for col_idx in range(1, len(col_names) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = _HDR_FONT
        cell.fill = _HDR_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Auto-width and number formatting
    for col_idx, col_name in enumerate(col_names, start=1):
        col_letter = get_column_letter(col_idx)

        # Determine format
        fmt = None
        if col_name in money_cols:
            fmt = _FMT_MONEY
        elif col_name in pct_cols:
            fmt = _FMT_PCT
        elif col_name in number_cols:
            fmt = _FMT_NUMBER

        max_len = len(str(col_name)) + 2
        for row_idx in range(2, ws.max_row + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            if fmt:
                cell.number_format = fmt
            cell_len = len(str(cell.value or ""))
            if cell_len > max_len:
                max_len = cell_len

        ws.column_dimensions[col_letter].width = min(max_len + 2, 25)

    # Freeze first row
    ws.sheet_view.showGridLines = True
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def _build_resumen_sku(df: pd.DataFrame) -> pd.DataFrame:
    """Build a yearly summary by SKU: totals across all periods.

    Aggregates:
        - Units sold per channel (sum)
        - VN, COGS, Aporte per channel and total (sum)
        - Margen (recalculated)
        - InStock weighted average (sum dias / sum dias_mes)
        - Dimensions (first non-null)
    """
    # Exclude SOLO_AA rows from the main aggregation
    df_work = df[df["TIPO_DATO"] != "SOLO_AA"].copy()
    if df_work.empty:
        return pd.DataFrame()

    # Columns to aggregate
    sum_cols = []
    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
        sum_cols.extend([
            f"VENTA_FUL_{canal}_UND",
            f"VN_RES_{canal}", f"COGS_RES_{canal}", f"APORTE_RES_{canal}",
            f"VN_IRR_{canal}", f"COGS_IRR_{canal}", f"APORTE_IRR_{canal}",
        ])
    sum_cols.extend([
        "VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND", "VENTA_FUL_MAYOR_UND",
        "VN_RES_TOTAL", "COGS_RES_TOTAL", "APORTE_RES_TOTAL",
        "VN_IRR_TOTAL", "COGS_IRR_TOTAL", "APORTE_IRR_TOTAL",
        "DEMANDA_TOTAL", "LOST_SALES_TOTAL",
        "LOST_SALES_TIENDA", "LOST_SALES_ETAIL", "LOST_SALES_MAYOR",
        "VN_LOST_TIENDA", "VN_LOST_ETAIL", "VN_LOST_MAYOR", "VN_LOST_TOTAL",
        "COGS_LOST_TOTAL", "APORTE_LOST_TOTAL",
        "INSTOCK_DIAS_CD", "INSTOCK_DIAS_TIENDA", "INSTOCK_DIAS_CIA", "DIAS_MES",
    ])
    # De-duplicate
    sum_cols = list(dict.fromkeys(sum_cols))
    # Filter to existing columns
    sum_cols = [c for c in sum_cols if c in df_work.columns]

    dim_cols = ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                "PROCEDENCIA", "PROVEEDOR", "MIX_OFICIAL"]
    dim_cols = [c for c in dim_cols if c in df_work.columns]

    agg_dict = {c: "sum" for c in sum_cols}
    for c in dim_cols:
        agg_dict[c] = "first"

    resumen = df_work.groupby("SKU_PRODUCTO", as_index=False).agg(agg_dict)

    # Recalculate margins
    for canal in ["TIENDA", "ETAIL", "MAYOR", "TOTAL"]:
        vn_col = f"VN_RES_{canal}"
        ap_col = f"APORTE_RES_{canal}"
        mg_col = f"MARGEN_RES_{canal}"
        if vn_col in resumen.columns and ap_col in resumen.columns:
            resumen[mg_col] = np.where(
                resumen[vn_col] > 0,
                resumen[ap_col] / resumen[vn_col],
                0.0,
            )

    # InStock ponderado
    for tag in ["CD", "TIENDA", "CIA"]:
        dias_col = f"INSTOCK_DIAS_{tag}"
        if dias_col in resumen.columns and "DIAS_MES" in resumen.columns:
            resumen[f"INSTOCK_POND_{tag}"] = np.where(
                resumen["DIAS_MES"] > 0,
                resumen[dias_col] / resumen["DIAS_MES"],
                0.0,
            )

    # Select and order output columns
    out_cols = ["SKU_PRODUCTO"] + dim_cols
    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
        ful_col = f"VENTA_FUL_{canal}_UND"
        if ful_col in resumen.columns:
            out_cols.append(ful_col)
    out_cols.append("DEMANDA_TOTAL") if "DEMANDA_TOTAL" in resumen.columns else None
    out_cols.append("LOST_SALES_TOTAL") if "LOST_SALES_TOTAL" in resumen.columns else None
    for canal in ["TIENDA", "ETAIL", "MAYOR", "TOTAL"]:
        for metric in ["VN_RES", "COGS_RES", "APORTE_RES", "MARGEN_RES"]:
            col = f"{metric}_{canal}"
            if col in resumen.columns:
                out_cols.append(col)
    for tag in ["CD", "TIENDA", "CIA"]:
        ist_col = f"INSTOCK_POND_{tag}"
        if ist_col in resumen.columns:
            out_cols.append(ist_col)

    out_cols = [c for c in out_cols if c is not None and c in resumen.columns]
    return resumen[out_cols]


def _build_resumen_area_linea(df: pd.DataFrame) -> pd.DataFrame:
    """Build a yearly summary grouped by AREA × LINEA.

    Same financial aggregation as SKU summary but at category level.
    """
    df_work = df[df["TIPO_DATO"] != "SOLO_AA"].copy()
    if df_work.empty:
        return pd.DataFrame()

    # Need both dimensions
    for c in ["AREA", "LINEA"]:
        if c not in df_work.columns:
            return pd.DataFrame()

    sum_cols = []
    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
        sum_cols.extend([
            f"VENTA_FUL_{canal}_UND",
            f"VN_RES_{canal}", f"COGS_RES_{canal}", f"APORTE_RES_{canal}",
        ])
    sum_cols.extend([
        "VN_RES_TOTAL", "COGS_RES_TOTAL", "APORTE_RES_TOTAL",
        "DEMANDA_TOTAL", "LOST_SALES_TOTAL",
        "VN_LOST_TOTAL", "COGS_LOST_TOTAL", "APORTE_LOST_TOTAL",
        "INSTOCK_DIAS_CD", "INSTOCK_DIAS_TIENDA", "INSTOCK_DIAS_CIA", "DIAS_MES",
    ])
    sum_cols = list(dict.fromkeys(sum_cols))
    sum_cols = [c for c in sum_cols if c in df_work.columns]

    agg_dict = {c: "sum" for c in sum_cols}
    resumen = df_work.groupby(["AREA", "LINEA"], as_index=False).agg(agg_dict)

    # Recalculate margins
    for canal in ["TIENDA", "ETAIL", "MAYOR", "TOTAL"]:
        vn_col = f"VN_RES_{canal}"
        ap_col = f"APORTE_RES_{canal}"
        mg_col = f"MARGEN_RES_{canal}"
        if vn_col in resumen.columns and ap_col in resumen.columns:
            resumen[mg_col] = np.where(
                resumen[vn_col] > 0,
                resumen[ap_col] / resumen[vn_col],
                0.0,
            )

    # InStock ponderado
    for tag in ["CD", "TIENDA", "CIA"]:
        dias_col = f"INSTOCK_DIAS_{tag}"
        if dias_col in resumen.columns and "DIAS_MES" in resumen.columns:
            resumen[f"INSTOCK_POND_{tag}"] = np.where(
                resumen["DIAS_MES"] > 0,
                resumen[dias_col] / resumen["DIAS_MES"],
                0.0,
            )

    # Count unique SKUs per group
    sku_count = df_work.groupby(["AREA", "LINEA"])["SKU_PRODUCTO"].nunique().reset_index()
    sku_count.columns = ["AREA", "LINEA", "CANT_SKUS"]
    resumen = resumen.merge(sku_count, on=["AREA", "LINEA"], how="left")

    # Select and order output columns
    out_cols = ["AREA", "LINEA", "CANT_SKUS"]
    for canal in ["TIENDA", "ETAIL", "MAYOR"]:
        ful_col = f"VENTA_FUL_{canal}_UND"
        if ful_col in resumen.columns:
            out_cols.append(ful_col)
    if "DEMANDA_TOTAL" in resumen.columns:
        out_cols.append("DEMANDA_TOTAL")
    if "LOST_SALES_TOTAL" in resumen.columns:
        out_cols.append("LOST_SALES_TOTAL")
    for canal in ["TIENDA", "ETAIL", "MAYOR", "TOTAL"]:
        for metric in ["VN_RES", "COGS_RES", "APORTE_RES", "MARGEN_RES"]:
            col = f"{metric}_{canal}"
            if col in resumen.columns:
                out_cols.append(col)
    for tag in ["CD", "TIENDA", "CIA"]:
        ist_col = f"INSTOCK_POND_{tag}"
        if ist_col in resumen.columns:
            out_cols.append(ist_col)

    out_cols = [c for c in out_cols if c in resumen.columns]

    # Sort by VN_RES_TOTAL descending
    if "VN_RES_TOTAL" in resumen.columns:
        resumen = resumen.sort_values("VN_RES_TOTAL", ascending=False)

    return resumen[out_cols]


def _build_pivot_mensual(df: pd.DataFrame) -> pd.DataFrame:
    """Build a monthly pivot: rows = AREA × LINEA, columns = PERIODO.

    Values: VN_RES_TOTAL per month. Creates a cross-tab that the user
    can use directly as a monthly breakdown by category.
    """
    df_work = df[df["TIPO_DATO"] != "SOLO_AA"].copy()
    if df_work.empty:
        return pd.DataFrame()

    for c in ["AREA", "LINEA", "PERIODO", "VN_RES_TOTAL"]:
        if c not in df_work.columns:
            return pd.DataFrame()

    df_work["PERIODO"] = pd.to_datetime(df_work["PERIODO"])
    df_work["MES_LABEL"] = df_work["PERIODO"].dt.strftime("%Y-%m")

    # Pivot table: rows = AREA+LINEA, columns = month labels, values = VN total
    pivot = pd.pivot_table(
        df_work,
        values="VN_RES_TOTAL",
        index=["AREA", "LINEA"],
        columns="MES_LABEL",
        aggfunc="sum",
        fill_value=0,
    )

    # Sort columns chronologically
    pivot = pivot[sorted(pivot.columns)]

    # Add TOTAL row and column
    pivot["TOTAL_ANO"] = pivot.sum(axis=1)
    pivot = pivot.sort_values("TOTAL_ANO", ascending=False)

    # Reset index for clean export
    pivot = pivot.reset_index()

    return pivot


def _write_summary_sheets(writer, df: pd.DataFrame):
    """Write all summary sheets to the Excel writer.

    Adds three sheets:
        - 'Resumen SKU': yearly aggregation per SKU
        - 'Resumen Area-Linea': yearly aggregation per Area × Linea
        - 'Pivot VN Mensual': cross-tab VN by month
    """
    wb = writer.book

    # --- Sheet 1: Resumen por SKU ---
    try:
        df_sku = _build_resumen_sku(df)
        if not df_sku.empty:
            df_sku.to_excel(writer, sheet_name="Resumen SKU", index=False)
            ws = wb["Resumen SKU"]

            money_cols = {c for c in df_sku.columns
                          if any(c.startswith(p) for p in ["VN_", "COGS_", "APORTE_"])}
            pct_cols = {c for c in df_sku.columns
                        if c.startswith("MARGEN_") or c.startswith("INSTOCK_POND_")}
            number_cols = {c for c in df_sku.columns
                           if any(c.startswith(p) for p in
                                  ["VENTA_FUL_", "DEMANDA_", "LOST_SALES_"])}

            _style_summary_ws(ws, df_sku, money_cols, pct_cols, number_cols)
    except Exception:
        pass  # Defensive: never fail the entire export for a summary sheet

    # --- Sheet 2: Resumen por Area-Linea ---
    try:
        df_al = _build_resumen_area_linea(df)
        if not df_al.empty:
            df_al.to_excel(writer, sheet_name="Resumen Area-Linea", index=False)
            ws = wb["Resumen Area-Linea"]

            money_cols = {c for c in df_al.columns
                          if any(c.startswith(p) for p in ["VN_", "COGS_", "APORTE_"])}
            pct_cols = {c for c in df_al.columns
                        if c.startswith("MARGEN_") or c.startswith("INSTOCK_POND_")}
            number_cols = {c for c in df_al.columns
                           if c.startswith("VENTA_FUL_") or c.startswith("DEMANDA_")
                           or c.startswith("LOST_SALES_") or c == "CANT_SKUS"}

            _style_summary_ws(ws, df_al, money_cols, pct_cols, number_cols)

            # Add a grand total row
            last_row = ws.max_row + 1
            ws.cell(row=last_row, column=1, value="TOTAL").font = _TOTAL_FONT
            ws.cell(row=last_row, column=1).fill = _TOTAL_FILL
            for col_idx in range(2, len(df_al.columns) + 1):
                col_name = df_al.columns[col_idx - 1]
                cell = ws.cell(row=last_row, column=col_idx)
                cell.fill = _TOTAL_FILL
                cell.font = _TOTAL_FONT

                if col_name.startswith("MARGEN_"):
                    # Recalculate margin from VN and APORTE columns
                    vn_col_name = col_name.replace("MARGEN_RES_", "VN_RES_")
                    if vn_col_name in df_al.columns:
                        total_vn = df_al[vn_col_name].sum()
                        ap_col_name = col_name.replace("MARGEN_RES_", "APORTE_RES_")
                        total_ap = df_al[ap_col_name].sum() if ap_col_name in df_al.columns else 0
                        cell.value = total_ap / total_vn if total_vn > 0 else 0
                        cell.number_format = _FMT_PCT
                elif col_name.startswith("INSTOCK_POND_"):
                    # Weighted avg: total dias / total dias_mes
                    tag = col_name.replace("INSTOCK_POND_", "")
                    dias_col = f"INSTOCK_DIAS_{tag}"
                    if dias_col in df_al.columns and "DIAS_MES" in df_al.columns:
                        total_dias = df_al[dias_col].sum()
                        total_dm = df_al["DIAS_MES"].sum()
                        cell.value = total_dias / total_dm if total_dm > 0 else 0
                    cell.number_format = _FMT_PCT
                elif col_name in money_cols:
                    cell.value = df_al[col_name].sum()
                    cell.number_format = _FMT_MONEY
                elif col_name in number_cols:
                    cell.value = df_al[col_name].sum()
                    cell.number_format = _FMT_NUMBER
    except Exception:
        pass

    # --- Sheet 3: Pivot VN Mensual ---
    try:
        df_piv = _build_pivot_mensual(df)
        if not df_piv.empty:
            df_piv.to_excel(writer, sheet_name="Pivot VN Mensual", index=False)
            ws = wb["Pivot VN Mensual"]

            # Style header
            for col_idx in range(1, len(df_piv.columns) + 1):
                cell = ws.cell(row=1, column=col_idx)
                col_name = df_piv.columns[col_idx - 1]
                if col_name in ("AREA", "LINEA"):
                    cell.font = _HDR_FONT
                    cell.fill = _HDR_FILL
                elif col_name == "TOTAL_ANO":
                    cell.font = _HDR_FONT
                    cell.fill = PatternFill(start_color="C65911", end_color="C65911", fill_type="solid")
                else:
                    cell.font = _HDR_FONT
                    cell.fill = _HDR_FILL_ALT
                cell.alignment = Alignment(horizontal="center", vertical="center")

            # Format data cells
            for col_idx in range(3, len(df_piv.columns) + 1):
                col_letter = get_column_letter(col_idx)
                for row_idx in range(2, ws.max_row + 1):
                    ws.cell(row=row_idx, column=col_idx).number_format = _FMT_MONEY

            # Auto-width
            for col_idx in range(1, len(df_piv.columns) + 1):
                col_letter = get_column_letter(col_idx)
                max_len = len(str(df_piv.columns[col_idx - 1])) + 2
                ws.column_dimensions[col_letter].width = max(max_len, 12)

            ws.freeze_panes = "C2"
            ws.auto_filter.ref = ws.dimensions

            # Add grand total row
            last_row = ws.max_row + 1
            ws.cell(row=last_row, column=1, value="TOTAL").font = _TOTAL_FONT
            ws.cell(row=last_row, column=1).fill = _TOTAL_FILL
            ws.cell(row=last_row, column=2).fill = _TOTAL_FILL
            for col_idx in range(3, len(df_piv.columns) + 1):
                cell = ws.cell(row=last_row, column=col_idx)
                cell.value = df_piv.iloc[:, col_idx - 1].sum()
                cell.number_format = _FMT_MONEY
                cell.font = _TOTAL_FONT
                cell.fill = _TOTAL_FILL
    except Exception:
        pass


# ============================================================================
# YoY DASHBOARD
# ============================================================================

_MES_NAMES = {1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
              7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic"}


def _fmt_mm(val):
    """Format large numbers as millions with M suffix."""
    if abs(val) >= 1_000_000_000:
        return f"${val / 1_000_000_000:,.1f} MM"
    elif abs(val) >= 1_000_000:
        return f"${val / 1_000_000:,.0f} M"
    else:
        return f"${val:,.0f}"


def _fmt_cl(val):
    """Format number in Chilean style: $1.234.567 (dots as thousand separators)."""
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "$0"
    if pd.isna(val) or val == 0:
        return "$0"
    negative = val < 0
    formatted = f"{abs(val):,.0f}".replace(",", ".")
    return f"-${formatted}" if negative else f"${formatted}"


def _render_yoy_dashboard(df_proy):
    """Render interactive YoY comparison dashboard."""

    if "AA_VN_TOTAL" not in df_proy.columns:
        st.info("No hay datos de ano anterior disponibles para comparar.")
        return

    current_year = pd.Timestamp.now().year
    aa_year = current_year - 1

    st.markdown(f"### Dashboard Comparativo: Forecast vs Real {aa_year}")

    # --- Year + Month range + Filters ---
    frow1, frow2, frow3, frow4, frow5, frow6 = st.columns([1, 1, 1, 1, 1, 1])
    with frow1:
        fc_years = sorted([
            y for y in df_proy[df_proy["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])]["PERIODO_ANO"].unique()
            if y >= current_year
        ])
        sel_fc_year = st.selectbox("Ano Forecast", fc_years, index=0, key="yoy_fc_year") if fc_years else current_year
    with frow2:
        mes_hasta = st.selectbox(
            "Comparar hasta mes",
            options=list(range(1, 13)),
            index=11,  # default = Diciembre (todo el ano)
            format_func=lambda m: _MES_NAMES.get(m, str(m)),
            key="yoy_mes_hasta",
        )
    with frow3:
        areas = sorted([str(x) for x in df_proy["AREA"].dropna().unique() if str(x).strip()])
        sel_area = st.selectbox("Area", ["Todos"] + areas, key="yoy_area")
    with frow4:
        base = df_proy if sel_area == "Todos" else df_proy[df_proy["AREA"].astype(str) == sel_area]
        lineas_opt = sorted([str(x) for x in base["LINEA"].dropna().unique() if str(x).strip()])
        sel_linea = st.selectbox("Linea", ["Todos"] + lineas_opt, key="yoy_linea")
    with frow5:
        base2 = base if sel_linea == "Todos" else base[base["LINEA"].astype(str) == sel_linea]
        sub_opt = sorted([str(x) for x in base2["SUBLINEA"].dropna().unique() if str(x).strip()])
        sel_sublinea = st.selectbox("Sublinea", ["Todos"] + sub_opt, key="yoy_sublinea")
    with frow6:
        marcas_opt = sorted([str(x) for x in df_proy["MARCA"].dropna().unique() if str(x).strip()])
        sel_marca = st.selectbox("Marca", ["Todos"] + marcas_opt, key="yoy_marca")

    # Apply structure filters
    df_f = df_proy.copy()
    if sel_area != "Todos":
        df_f = df_f[df_f["AREA"].astype(str) == sel_area]
    if sel_linea != "Todos":
        df_f = df_f[df_f["LINEA"].astype(str) == sel_linea]
    if sel_sublinea != "Todos":
        df_f = df_f[df_f["SUBLINEA"].astype(str) == sel_sublinea]
    if sel_marca != "Todos":
        df_f = df_f[df_f["MARCA"].astype(str) == sel_marca]

    if df_f.empty:
        st.warning("No hay datos con los filtros seleccionados.")
        return

    # === Filter forecast + historico to SELECTED YEAR + MONTH RANGE ===
    # Include HISTORICO rows (real sales for completed months) so past months
    # show actual data instead of 0 when there is no forecast for them.
    df_fc = df_f[
        (df_f["TIPO_DATO"].isin(["PROYECCION", "HISTORICO", "REAL+FC"]))
        & (df_f["PERIODO_ANO"] == sel_fc_year)
        & (df_f["PERIODO_MES"] <= mes_hasta)
    ]

    # === FIX: Get AA per SKU using MAX (not drop_duplicates which picks HISTORICO rows with AA=0) ===
    aa_sku_cols = [c for c in df_f.columns if c.startswith("AA_")]
    if aa_sku_cols:
        df_aa_dedup = df_f.groupby("SKU_PRODUCTO", as_index=False)[aa_sku_cols].max()
        # Merge back structure columns from first non-empty row
        struct_cols = ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA", "TIPO_DATO"]
        struct_cols = [c for c in struct_cols if c in df_f.columns]
        struct_df = df_f[struct_cols].drop_duplicates("SKU_PRODUCTO")
        df_aa_dedup = df_aa_dedup.merge(struct_df, on="SKU_PRODUCTO", how="left")
    else:
        df_aa_dedup = df_f.drop_duplicates("SKU_PRODUCTO")

    # === Filter AA monthly by month range too ===
    aa_monthly_full = st.session_state.get("aa_monthly")
    if aa_monthly_full is not None and not aa_monthly_full.empty:
        aa_monthly_filtered = aa_monthly_full[aa_monthly_full["MES"] <= mes_hasta].copy()
    else:
        aa_monthly_filtered = None

    # Recompute AA totals for selected month range (if filtering months < 12)
    if mes_hasta < 12 and aa_monthly_filtered is not None:
        # Recalculate AA per SKU for only the selected months
        valid_skus = set(df_f["SKU_PRODUCTO"].unique())
        aa_m_filt = aa_monthly_filtered[aa_monthly_filtered["SKU_PRODUCTO"].isin(valid_skus)]
        aa_recalc = aa_m_filt.groupby(["SKU_PRODUCTO", "CANAL_STD"], as_index=False).agg(
            NETO_AA=("NETO_AA", "sum"), CANTIDAD_AA=("CANTIDAD_AA", "sum"), APORTE_AA=("APORTE_AA", "sum")
        )
        # Rebuild per-channel AA
        for canal_std, suffix in [("TIENDA", "TIENDA"), ("ETAIL", "ETAIL"), ("MAYORISTA", "MAYOR")]:
            cd = aa_recalc[aa_recalc["CANAL_STD"] == canal_std][["SKU_PRODUCTO", "NETO_AA", "CANTIDAD_AA", "APORTE_AA"]]
            cd = cd.rename(columns={"NETO_AA": f"AA_VN_{suffix}", "CANTIDAD_AA": f"AA_UND_{suffix}", "APORTE_AA": f"AA_APORTE_{suffix}"})
            df_aa_dedup = df_aa_dedup.drop(columns=[f"AA_VN_{suffix}", f"AA_UND_{suffix}", f"AA_APORTE_{suffix}"], errors="ignore")
            df_aa_dedup = df_aa_dedup.merge(cd, on="SKU_PRODUCTO", how="left")
        for c in [c for c in df_aa_dedup.columns if c.startswith("AA_")]:
            df_aa_dedup[c] = df_aa_dedup[c].fillna(0)
        df_aa_dedup["AA_VN_TOTAL"] = df_aa_dedup.get("AA_VN_TIENDA", 0) + df_aa_dedup.get("AA_VN_ETAIL", 0) + df_aa_dedup.get("AA_VN_MAYOR", 0)
        df_aa_dedup["AA_UND_TOTAL"] = df_aa_dedup.get("AA_UND_TIENDA", 0) + df_aa_dedup.get("AA_UND_ETAIL", 0) + df_aa_dedup.get("AA_UND_MAYOR", 0)
        df_aa_dedup["AA_APORTE_TOTAL"] = df_aa_dedup.get("AA_APORTE_TIENDA", 0) + df_aa_dedup.get("AA_APORTE_ETAIL", 0) + df_aa_dedup.get("AA_APORTE_MAYOR", 0)

    # --- Compute KPIs ---
    # Determine label: if HISTORICO rows are included, show "Real+FC" label
    has_hist = df_fc["TIPO_DATO"].isin(["HISTORICO", "REAL+FC"]).any() if "TIPO_DATO" in df_fc.columns else False
    fc_label = f"Real+FC {sel_fc_year}" if has_hist else f"FC {sel_fc_year}"

    fc_vn = df_fc["VN_RES_TOTAL"].sum()
    fc_aporte = df_fc["APORTE_RES_TOTAL"].sum() if "APORTE_RES_TOTAL" in df_fc.columns else 0
    fc_und = sum(df_fc.get(c, pd.Series(0, index=df_fc.index)).sum()
                 for c in ["VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND", "VENTA_FUL_MAYOR_UND"])
    fc_skus = df_fc["SKU_PRODUCTO"].nunique()

    aa_vn = df_aa_dedup["AA_VN_TOTAL"].sum()
    aa_aporte = df_aa_dedup["AA_APORTE_TOTAL"].sum()
    aa_und = df_aa_dedup["AA_UND_TOTAL"].sum()
    aa_skus = df_aa_dedup[df_aa_dedup["AA_VN_TOTAL"] != 0]["SKU_PRODUCTO"].nunique()

    vn_var = ((fc_vn / aa_vn) - 1) * 100 if aa_vn > 0 else 0
    aporte_var = ((fc_aporte / aa_aporte) - 1) * 100 if aa_aporte > 0 else 0
    und_var = ((fc_und / aa_und) - 1) * 100 if aa_und > 0 else 0
    skus_sin_fc = len(df_f[df_f["TIPO_DATO"] == "SOLO_AA"]["SKU_PRODUCTO"].unique())

    # --- Period label ---
    mes_label = f"Ene-{_MES_NAMES[mes_hasta]}" if mes_hasta < 12 else "Anual"

    if has_hist:
        hist_meses = sorted(df_fc[df_fc["TIPO_DATO"].isin(["HISTORICO", "REAL+FC"])]["PERIODO_MES"].unique())
        meses_reales = ", ".join([_MES_NAMES.get(m, str(m)) for m in hist_meses])
        st.caption(f"Meses con venta real (total o parcial): {meses_reales}. Resto es forecast.")

    # --- KPI Cards: 3 rows ---
    st.markdown(f"#### Venta Neta ({mes_label})")
    k1, k2, k3 = st.columns(3)
    k1.metric(fc_label, _fmt_mm(fc_vn))
    k2.metric(f"Real {aa_year}", _fmt_mm(aa_vn))
    k3.metric("Var %", f"{vn_var:+.1f}%", delta=f"{vn_var:+.1f}%", delta_color="normal")

    st.markdown(f"#### Aporte ({mes_label})")
    a1, a2, a3 = st.columns(3)
    a1.metric(fc_label, _fmt_mm(fc_aporte))
    a2.metric(f"Real {aa_year}", _fmt_mm(aa_aporte))
    a3.metric("Var %", f"{aporte_var:+.1f}%", delta=f"{aporte_var:+.1f}%", delta_color="normal")

    st.markdown(f"#### Unidades y Cobertura SKU ({mes_label})")
    u1, u2, u3, u4 = st.columns(4)
    u1.metric(f"Und {fc_label}", f"{fc_und:,.0f}")
    u2.metric(f"Und Real {aa_year}", f"{aa_und:,.0f}")
    u3.metric("Var Und %", f"{und_var:+.1f}%", delta=f"{und_var:+.1f}%", delta_color="normal")
    u4.metric("SKUs Sin FC", f"{skus_sin_fc:,}", delta=f"-{skus_sin_fc}" if skus_sin_fc > 0 else "0",
              delta_color="inverse")

    st.divider()

    # --- Metric selector for charts ---
    metric_opts = {
        "Venta Neta": ("VN_RES_TOTAL", "AA_VN_TOTAL", "VN_RES_TIENDA", "VN_RES_ETAIL", "VN_RES_MAYOR",
                        "AA_VN_TIENDA", "AA_VN_ETAIL", "AA_VN_MAYOR", "NETO_AA", "$"),
        "Aporte": ("APORTE_RES_TOTAL", "AA_APORTE_TOTAL", "VN_RES_TIENDA", "VN_RES_ETAIL", "VN_RES_MAYOR",
                    "AA_APORTE_TIENDA", "AA_APORTE_ETAIL", "AA_APORTE_MAYOR", "APORTE_AA", "$"),
        "Unidades": ("_UND_FC_TOTAL", "AA_UND_TOTAL", "VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND", "VENTA_FUL_MAYOR_UND",
                      "AA_UND_TIENDA", "AA_UND_ETAIL", "AA_UND_MAYOR", "CANTIDAD_AA", ""),
    }
    sel_metric = st.radio("Metrica:", list(metric_opts.keys()), horizontal=True, key="yoy_metric")
    m_fc_col, m_aa_col, m_fc_t, m_fc_e, m_fc_m, m_aa_t, m_aa_e, m_aa_m, m_aa_monthly_col, m_prefix = metric_opts[sel_metric]

    # --- Chart 1: Grouped bars by hierarchy ---
    st.markdown(f"#### {sel_metric} por Estructura Comercial")
    dim_options = {"Area": "AREA", "Linea": "LINEA", "Sublinea": "SUBLINEA", "Marca": "MARCA"}
    sel_dim_label = st.radio("Agrupar por:", list(dim_options.keys()), horizontal=True, key="yoy_dim")
    dim_col = dim_options[sel_dim_label]

    # FC aggregation
    if sel_metric == "Unidades":
        # Sum all channel units
        df_fc_agg = df_fc.copy()
        df_fc_agg["_UND_FC_TOTAL"] = (
            df_fc_agg.get("VENTA_FUL_TIENDA_UND", 0).fillna(0)
            + df_fc_agg.get("VENTA_FUL_ETAIL_UND", 0).fillna(0)
            + df_fc_agg.get("VENTA_FUL_MAYOR_UND", 0).fillna(0)
        )
        fc_by_dim = df_fc_agg.groupby(dim_col, as_index=False)["_UND_FC_TOTAL"].sum()
        fc_by_dim = fc_by_dim.rename(columns={"_UND_FC_TOTAL": "Monto"})
    else:
        fc_by_dim = df_fc.groupby(dim_col, as_index=False)[m_fc_col].sum()
        fc_by_dim = fc_by_dim.rename(columns={m_fc_col: "Monto"})
    fc_by_dim["Tipo"] = fc_label

    aa_by_dim = df_aa_dedup.groupby(dim_col, as_index=False)[m_aa_col].sum()
    aa_by_dim = aa_by_dim.rename(columns={m_aa_col: "Monto"})
    aa_by_dim["Tipo"] = f"Real {aa_year}"

    chart_data = pd.concat([fc_by_dim, aa_by_dim], ignore_index=True)
    chart_data = chart_data[chart_data["Monto"] != 0]
    chart_data[dim_col] = chart_data[dim_col].astype(str)

    fmt_str = f"{m_prefix},.0f" if m_prefix else ",.0f"

    if not chart_data.empty:
        # Sort dimension values by total descending
        dim_order = (
            chart_data.groupby(dim_col)["Monto"].sum()
            .sort_values(ascending=False).index.tolist()
        )
        fc_data = chart_data[chart_data["Tipo"] == fc_label].set_index(dim_col).reindex(dim_order).reset_index()
        aa_data = chart_data[chart_data["Tipo"] == f"Real {aa_year}"].set_index(dim_col).reindex(dim_order).reset_index()

        fig_bars = go.Figure(layout=dorel_layout(
            height=400,
            xaxis=dict(title=sel_dim_label),
            yaxis=dict(title=sel_metric),
            barmode="group",
        ))
        fig_bars.add_trace(go.Bar(
            x=fc_data[dim_col], y=fc_data["Monto"],
            name=fc_label, marker_color="#065E8B",
            hovertemplate=f"{sel_dim_label}: %{{x}}<br>{sel_metric}: %{{y:,.0f}}<extra>{fc_label}</extra>",
        ))
        fig_bars.add_trace(go.Bar(
            x=aa_data[dim_col], y=aa_data["Monto"],
            name=f"Real {aa_year}", marker_color="#23CED3",
            hovertemplate=f"{sel_dim_label}: %{{x}}<br>{sel_metric}: %{{y:,.0f}}<extra>Real {aa_year}</extra>",
        ))
        st.plotly_chart(fig_bars, use_container_width=True)

    st.divider()

    # --- Row 2: Channel + Monthly trend side by side ---
    col_ch1, col_ch2 = st.columns(2)

    with col_ch1:
        st.markdown(f"#### {sel_metric} por Canal")
        if sel_metric == "Unidades":
            fc_vals = [df_fc[c].sum() if c in df_fc.columns else 0 for c in [m_fc_t, m_fc_e, m_fc_m]]
        elif sel_metric == "Aporte":
            # Aporte by channel not directly available — use VN proportion
            fc_vals = [df_fc[c].sum() if c in df_fc.columns else 0 for c in ["VN_RES_TIENDA", "VN_RES_ETAIL", "VN_RES_MAYOR"]]
            fc_total_vn = sum(fc_vals)
            if fc_total_vn > 0 and fc_aporte > 0:
                fc_vals = [v / fc_total_vn * fc_aporte for v in fc_vals]
        else:
            fc_vals = [df_fc[c].sum() if c in df_fc.columns else 0 for c in [m_fc_t, m_fc_e, m_fc_m]]
        aa_vals = [df_aa_dedup[c].sum() if c in df_aa_dedup.columns else 0 for c in [m_aa_t, m_aa_e, m_aa_m]]

        channels_fc = pd.DataFrame({"Canal": ["Tienda", "Etail", "Mayorista"],
                                     "Monto": fc_vals, "Tipo": [fc_label] * 3})
        channels_aa = pd.DataFrame({"Canal": ["Tienda", "Etail", "Mayorista"],
                                     "Monto": aa_vals, "Tipo": [f"Real {aa_year}"] * 3})
        chan_data = pd.concat([channels_fc, channels_aa], ignore_index=True)

        fig_chan = go.Figure(layout=dorel_layout(
            height=350,
            xaxis=dict(title="Canal"),
            yaxis=dict(title=sel_metric),
            barmode="group",
        ))
        fig_chan.add_trace(go.Bar(
            x=channels_fc["Canal"], y=channels_fc["Monto"],
            name=fc_label, marker_color="#065E8B",
            hovertemplate="Canal: %{x}<br>Monto: %{y:,.0f}<extra>" + fc_label + "</extra>",
        ))
        fig_chan.add_trace(go.Bar(
            x=channels_aa["Canal"], y=channels_aa["Monto"],
            name=f"Real {aa_year}", marker_color="#23CED3",
            hovertemplate="Canal: %{x}<br>Monto: %{y:,.0f}<extra>Real " + str(aa_year) + "</extra>",
        ))
        st.plotly_chart(fig_chan, use_container_width=True)

    with col_ch2:
        st.markdown(f"#### Tendencia Mensual {sel_metric}")

        # FC + HISTORICO monthly — selected year (all 12 months for trend, not cut by mes_hasta)
        # Include HISTORICO rows so completed months show real data instead of 0
        if sel_metric == "Unidades":
            df_fc_full_year = df_f[
                (df_f["TIPO_DATO"].isin(["PROYECCION", "HISTORICO", "REAL+FC"])) & (df_f["PERIODO_ANO"] == sel_fc_year)
            ].copy()
            df_fc_full_year["_M_VAL"] = (
                df_fc_full_year.get("VENTA_FUL_TIENDA_UND", 0).fillna(0)
                + df_fc_full_year.get("VENTA_FUL_ETAIL_UND", 0).fillna(0)
                + df_fc_full_year.get("VENTA_FUL_MAYOR_UND", 0).fillna(0)
            )
            fc_monthly = df_fc_full_year.groupby("PERIODO_MES", as_index=False)["_M_VAL"].sum()
        else:
            df_fc_full_year = df_f[
                (df_f["TIPO_DATO"].isin(["PROYECCION", "HISTORICO", "REAL+FC"])) & (df_f["PERIODO_ANO"] == sel_fc_year)
            ]
            fc_monthly = df_fc_full_year.groupby("PERIODO_MES", as_index=False)[m_fc_col].sum()
            fc_monthly = fc_monthly.rename(columns={m_fc_col: "_M_VAL"})
        fc_monthly = fc_monthly.rename(columns={"PERIODO_MES": "MES", "_M_VAL": "Monto"})
        fc_monthly["Tipo"] = f"Real+FC {sel_fc_year}"

        # AA monthly (full year, filtered by structure)
        aa_monthly_all = st.session_state.get("aa_monthly")
        if aa_monthly_all is not None and not aa_monthly_all.empty:
            aa_m = aa_monthly_all.copy()
            valid_skus = set(df_f["SKU_PRODUCTO"].unique())
            aa_m = aa_m[aa_m["SKU_PRODUCTO"].isin(valid_skus)]

            aa_m_agg = aa_m.groupby("MES", as_index=False)[m_aa_monthly_col].sum()
            aa_m_agg = aa_m_agg.rename(columns={m_aa_monthly_col: "Monto"})
            aa_m_agg["Tipo"] = f"Real {aa_year}"
            trend_data = pd.concat([fc_monthly, aa_m_agg], ignore_index=True)
        else:
            trend_data = fc_monthly

        trend_data["MES_NOM"] = trend_data["MES"].map(_MES_NAMES)
        trend_data = trend_data.sort_values("MES")

        if not trend_data.empty:
            trend_data["En Rango"] = trend_data["MES"] <= mes_hasta
            _mes_tick_labels = {1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
                                7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic"}

            fig_trend = go.Figure(layout=dorel_layout(
                height=350,
                xaxis=dict(
                    title="Mes",
                    tickmode="array",
                    tickvals=list(range(1, 13)),
                    ticktext=[_mes_tick_labels[m] for m in range(1, 13)],
                ),
                yaxis=dict(title=sel_metric),
            ))

            color_map = {f"Real+FC {sel_fc_year}": "#065E8B", f"Real {aa_year}": "#23CED3"}
            for tipo, color in color_map.items():
                td = trend_data[trend_data["Tipo"] == tipo].sort_values("MES")
                if td.empty:
                    continue
                # Split into in-range (solid) and out-of-range (dimmed) segments
                td_in = td[td["En Rango"]]
                td_out = td[~td["En Rango"]]
                # Main solid trace
                fig_trend.add_trace(go.Scatter(
                    x=td_in["MES"], y=td_in["Monto"],
                    mode="lines+markers", name=tipo,
                    line=dict(color=color, width=2),
                    marker=dict(color=color, size=7),
                    opacity=1.0,
                    hovertemplate="Mes: %{text}<br>Monto: %{y:,.0f}<extra>" + tipo + "</extra>",
                    text=td_in["MES_NOM"],
                ))
                # Dimmed trace for months beyond cutoff
                if not td_out.empty:
                    # Add connecting point from last in-range to first out-of-range
                    bridge = pd.concat([td_in.tail(1), td_out]).sort_values("MES")
                    fig_trend.add_trace(go.Scatter(
                        x=bridge["MES"], y=bridge["Monto"],
                        mode="lines+markers", name=tipo,
                        line=dict(color=color, width=2),
                        marker=dict(color=color, size=7),
                        opacity=0.25,
                        showlegend=False,
                        hovertemplate="Mes: %{text}<br>Monto: %{y:,.0f}<extra>" + tipo + "</extra>",
                        text=bridge["MES_NOM"],
                    ))

            # Cutoff rule
            if mes_hasta < 12:
                fig_trend.add_vline(
                    x=mes_hasta + 0.5, line_dash="dash",
                    line_color="#C94BFF", opacity=0.6,
                )
            st.plotly_chart(fig_trend, use_container_width=True)

    st.divider()

    # --- Growth ratio table by dimension ---
    st.markdown(f"#### Ratio Crecimiento {sel_fc_year} vs {aa_year}")

    # Build a summary table with FC, AA, Var% for selected metric by selected dimension
    fc_dim = fc_by_dim.rename(columns={"Monto": "FC"}).drop(columns=["Tipo"])
    aa_dim = aa_by_dim.rename(columns={"Monto": "AA"}).drop(columns=["Tipo"])
    ratio_df = fc_dim.merge(aa_dim, on=dim_col, how="outer").fillna(0)
    ratio_df["Var %"] = np.where(ratio_df["AA"] > 0, ((ratio_df["FC"] / ratio_df["AA"]) - 1) * 100, 0)
    ratio_df["Diferencia"] = ratio_df["FC"] - ratio_df["AA"]

    # Add SKU counts
    fc_sku_cnt = df_fc.groupby(dim_col)["SKU_PRODUCTO"].nunique().reset_index().rename(columns={"SKU_PRODUCTO": "SKUs FC"})
    aa_sku_cnt = df_aa_dedup[df_aa_dedup["AA_VN_TOTAL"] != 0].groupby(dim_col)["SKU_PRODUCTO"].nunique().reset_index().rename(columns={"SKU_PRODUCTO": "SKUs AA"})
    ratio_df = ratio_df.merge(fc_sku_cnt, on=dim_col, how="left").merge(aa_sku_cnt, on=dim_col, how="left").fillna(0)
    ratio_df["SKUs FC"] = ratio_df["SKUs FC"].astype(int)
    ratio_df["SKUs AA"] = ratio_df["SKUs AA"].astype(int)
    ratio_df = ratio_df.sort_values("AA", ascending=False)

    if m_prefix == "$":
        ratio_df["FC"] = ratio_df["FC"].apply(fmt_clp)
        ratio_df["AA"] = ratio_df["AA"].apply(fmt_clp)
        ratio_df["Diferencia"] = ratio_df["Diferencia"].apply(fmt_clp)
        col_cfg = {
            "FC": st.column_config.TextColumn(fc_label),
            "AA": st.column_config.TextColumn(f"Real {aa_year}"),
            "Diferencia": st.column_config.TextColumn("Diferencia"),
            "Var %": st.column_config.NumberColumn("Var %", format="%.1f%%"),
        }
    else:
        col_cfg = {
            "FC": st.column_config.NumberColumn(fc_label, format="%,.0f"),
            "AA": st.column_config.NumberColumn(f"Real {aa_year}", format="%,.0f"),
            "Diferencia": st.column_config.NumberColumn("Diferencia", format="%,.0f"),
            "Var %": st.column_config.NumberColumn("Var %", format="%.1f%%"),
        }
    st.dataframe(ratio_df, use_container_width=True, column_config=col_cfg, hide_index=True)

    st.divider()

    # --- SKU Scatter ---
    st.markdown(f"#### Scatter SKU: {fc_label} vs Real {aa_year}")

    sku_compare = df_aa_dedup[["SKU_PRODUCTO", "SKU_NOM_PRODUCTO",
                                "AREA", "LINEA", "SUBLINEA", "MARCA",
                                "AA_VN_TOTAL", "AA_UND_TOTAL", "AA_APORTE_TOTAL", "TIPO_DATO"]].copy()

    # FC totals per SKU for selected year
    fc_sku = df_fc.groupby("SKU_PRODUCTO", as_index=False).agg(
        FC_VN=("VN_RES_TOTAL", "sum"),
        FC_APORTE=("APORTE_RES_TOTAL", "sum") if "APORTE_RES_TOTAL" in df_fc.columns else ("VN_RES_TOTAL", "sum"),
    )
    und_cols = [c for c in ["VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND", "VENTA_FUL_MAYOR_UND"] if c in df_fc.columns]
    if und_cols:
        fc_und_sku = df_fc.groupby("SKU_PRODUCTO", as_index=False)[und_cols].sum()
        fc_und_sku["FC_UND"] = fc_und_sku[und_cols].sum(axis=1)
        fc_sku = fc_sku.merge(fc_und_sku[["SKU_PRODUCTO", "FC_UND"]], on="SKU_PRODUCTO", how="left")
    else:
        fc_sku["FC_UND"] = 0

    sku_compare = sku_compare.merge(fc_sku, on="SKU_PRODUCTO", how="left")
    for c in ["FC_VN", "FC_APORTE", "FC_UND"]:
        sku_compare[c] = sku_compare[c].fillna(0)
    sku_compare["Estado"] = np.where(
        sku_compare["TIPO_DATO"] == "SOLO_AA", "Sin Forecast",
        np.where(sku_compare["AA_VN_TOTAL"] == 0, "Solo Forecast", "Ambos")
    )

    # Select scatter metric based on radio
    sc_x = {"Venta Neta": "AA_VN_TOTAL", "Aporte": "AA_APORTE_TOTAL", "Unidades": "AA_UND_TOTAL"}[sel_metric]
    sc_y = {"Venta Neta": "FC_VN", "Aporte": "FC_APORTE", "Unidades": "FC_UND"}[sel_metric]

    top_n = st.slider("Top N SKUs (por valor Ano Anterior)", 20, 200, 50, key="yoy_topn")
    sku_top = sku_compare.nlargest(top_n, sc_x)

    if not sku_top.empty:
        sku_top["SKU_NOM_PRODUCTO"] = sku_top["SKU_NOM_PRODUCTO"].astype(str).str[:50]

        fig_scatter = go.Figure(layout=dorel_layout(
            height=450,
            xaxis=dict(title=f"Real {aa_year}"),
            yaxis=dict(title=fc_label),
        ))
        estado_colors = {"Ambos": "#065E8B", "Sin Forecast": "#C94BFF", "Solo Forecast": "#23CED3"}
        for estado, color in estado_colors.items():
            sub = sku_top[sku_top["Estado"] == estado]
            if sub.empty:
                continue
            fig_scatter.add_trace(go.Scatter(
                x=sub[sc_x], y=sub[sc_y],
                mode="markers", name=estado,
                marker=dict(color=color, size=8, opacity=0.7),
                hovertemplate=(
                    "SKU: %{customdata[0]}<br>Producto: %{customdata[1]}<br>"
                    "Linea: %{customdata[2]}<br>Marca: %{customdata[3]}<br>"
                    f"Real {aa_year}: %{{x:,.0f}}<br>{fc_label}: %{{y:,.0f}}"
                    f"<extra>{estado}</extra>"
                ),
                customdata=sub[["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "LINEA", "MARCA"]].values,
            ))

        max_val = max(sku_top[sc_x].max(), sku_top[sc_y].max(), 1)
        fig_scatter.add_trace(go.Scatter(
            x=[0, max_val], y=[0, max_val],
            mode="lines", line=dict(dash="dash", color="gray", width=1),
            showlegend=False, hoverinfo="skip",
        ))
        st.plotly_chart(fig_scatter, use_container_width=True)
        st.caption("Sobre la diagonal = crecimiento. Bajo la diagonal = decrecimiento. Morado = sin forecast.")

    st.divider()

    # --- Table: SKUs sin forecast ---
    df_solo_aa = df_f[df_f["TIPO_DATO"] == "SOLO_AA"].drop_duplicates("SKU_PRODUCTO")
    if not df_solo_aa.empty:
        st.markdown(f"#### SKUs Sin Forecast ({len(df_solo_aa)} SKUs vendieron en {aa_year} sin FC {sel_fc_year})")
        display_cols = ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                        "AA_VN_TOTAL", "AA_UND_TOTAL", "AA_APORTE_TOTAL"]
        display_cols = [c for c in display_cols if c in df_solo_aa.columns]
        df_solo_display = df_solo_aa[display_cols].sort_values("AA_VN_TOTAL", ascending=False).reset_index(drop=True)
        if "AA_VN_TOTAL" in df_solo_display.columns:
            df_solo_display["AA_VN_TOTAL"] = df_solo_display["AA_VN_TOTAL"].apply(fmt_clp)
        if "AA_APORTE_TOTAL" in df_solo_display.columns:
            df_solo_display["AA_APORTE_TOTAL"] = df_solo_display["AA_APORTE_TOTAL"].apply(fmt_clp)
        st.dataframe(
            df_solo_display,
            use_container_width=True,
            column_config={
                "AA_VN_TOTAL": st.column_config.TextColumn(f"VN {aa_year} ($)"),
                "AA_UND_TOTAL": st.column_config.NumberColumn(f"Und {aa_year}", format="%,.0f"),
                "AA_APORTE_TOTAL": st.column_config.TextColumn(f"Aporte {aa_year} ($)"),
            },
            hide_index=True,
        )
    else:
        st.success(f"Todos los SKUs de {aa_year} tienen forecast en {sel_fc_year}.")


# ============================================================================
# PPT TABLES
# ============================================================================

def _build_ppt_table_data(fc_por_mes, aa_por_mes, meses_target, mes_labels, metric_fc, metric_aa, fmt_fn):
    """Build a 3-row table (FC, AA, Var%) for a given metric. Returns list of row dicts."""
    current_year = pd.Timestamp.now().year
    aa_year = current_year - 1

    row_fc = {"Métrica": f"Forecast {current_year}"}
    row_aa = {"Métrica": f"Real {aa_year}"}
    row_var = {"Métrica": "Var %"}

    total_fc = 0.0
    total_aa = 0.0

    for m in meses_target:
        lbl = mes_labels[m]
        fc_val = float(fc_por_mes.loc[fc_por_mes["MES"] == m, metric_fc].sum()) if not fc_por_mes.empty else 0.0
        aa_val = float(aa_por_mes.loc[aa_por_mes.index == m, metric_aa].sum()) if aa_por_mes is not None and m in aa_por_mes.index else 0.0

        total_fc += fc_val
        total_aa += aa_val

        row_fc[lbl] = fmt_fn(fc_val)
        row_aa[lbl] = fmt_fn(aa_val)
        if aa_val != 0:
            var = (fc_val - aa_val) / abs(aa_val) * 100
            row_var[lbl] = f"{'+' if var >= 0 else ''}{var:.1f}%"
        else:
            row_var[lbl] = "N/D"

    row_fc["Total"] = fmt_fn(total_fc)
    row_aa["Total"] = fmt_fn(total_aa)
    if total_aa != 0:
        var_total = (total_fc - total_aa) / abs(total_aa) * 100
        row_var["Total"] = f"{'+' if var_total >= 0 else ''}{var_total:.1f}%"
    else:
        row_var["Total"] = "N/D"

    return [row_fc, row_aa, row_var]


def _style_ppt_table(df_display):
    """Apply green/red coloring to Var % row."""
    def color_row(row):
        styles = [""] * len(row)
        if row["Métrica"] == "Var %":
            for i, val in enumerate(row):
                if isinstance(val, str) and val not in ("N/D", "Var %"):
                    try:
                        num = float(val.replace("+", "").replace("%", ""))
                        if num > 0:
                            styles[i] = "color: #1a7f37; font-weight: bold"
                        elif num < 0:
                            styles[i] = "color: #cf222e; font-weight: bold"
                    except ValueError:
                        pass
        return styles

    return df_display.style.apply(color_row, axis=1)


def _generate_ppt_file(tables_data, meses_target, mes_labels, sel_linea, current_year, extra_data=None):
    """Generate a python-pptx BytesIO buffer.

    Slide 1: Main slide — KPI chips + 3 tables (VN, Aporte, Margen) side by side.
    Slide 2: SKUs sin forecast (top AA sellers not in current forecast).
    Slide 3: SKUs que bajaron (significant YoY decline in projection).
    """
    from pptx import Presentation as PptxPresentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    import io as _io

    extra_data = extra_data or {}

    prs = PptxPresentation()
    prs.slide_width = Inches(13.33)
    prs.slide_height = Inches(7.5)
    blank_layout = prs.slide_layouts[6]

    # ── Colors ──────────────────────────────────────────────────────────────
    C_DARK   = RGBColor(0x06, 0x5E, 0x8B)   # header blue
    C_ACCENT = RGBColor(0x23, 0xCE, 0xD3)   # cyan accent
    C_ALT    = RGBColor(0xF0, 0xF4, 0xF8)   # alt row
    C_WHITE  = RGBColor(0xFF, 0xFF, 0xFF)
    C_GREEN  = RGBColor(0x1A, 0x7F, 0x37)
    C_RED    = RGBColor(0xCF, 0x22, 0x2E)
    C_ORANGE = RGBColor(0xF4, 0xA5, 0x28)
    C_GRAY   = RGBColor(0x88, 0x88, 0x88)
    C_BGKPI  = RGBColor(0xE8, 0xF4, 0xFB)   # light blue KPI bg

    mes_range_str = f"{mes_labels[meses_target[0]]}-{mes_labels[meses_target[-1]]} {current_year}"
    linea_str = sel_linea if sel_linea != "Todas" else "Todas las Líneas"
    aa_year = current_year - 1

    col_names_list = [""] + [mes_labels[m] for m in meses_target] + ["Total"]

    # ── Helper: add text box ─────────────────────────────────────────────────
    def _txb(slide, left, top, w, h, text, size=11, bold=False, color=None, align=PP_ALIGN.LEFT, wrap=True):
        tb = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = wrap
        p = tf.paragraphs[0]
        p.alignment = align
        r = p.add_run()
        r.text = text
        r.font.size = Pt(size)
        r.font.bold = bold
        if color:
            r.font.color.rgb = color
        return tb

    # ── Helper: filled rectangle (chip) ─────────────────────────────────────
    def _chip(slide, left, top, w, h, fill_color):
        from pptx.util import Inches as _I
        shape = slide.shapes.add_shape(
            1,  # MSO_SHAPE_TYPE.RECTANGLE
            _I(left), _I(top), _I(w), _I(h)
        )
        shape.fill.solid()
        shape.fill.fore_color.rgb = fill_color
        shape.line.color.rgb = fill_color
        return shape

    # ── Helper: mini metric chip ─────────────────────────────────────────────
    def _kpi_chip(slide, left, top, label, value, value_color=None):
        """Draw a small KPI card: label on top, value big below."""
        _chip(slide, left, top, 1.9, 0.75, C_BGKPI)
        _txb(slide, left + 0.08, top + 0.03, 1.74, 0.28,
             label, size=7, bold=False, color=C_GRAY)
        _txb(slide, left + 0.08, top + 0.3, 1.74, 0.38,
             value, size=13, bold=True, color=value_color or C_DARK)

    # ── Helper: compact metric table (no title row — label is in first col) ─
    def _mini_table(slide, left, top, w, h, rows_data, col_names, title):
        """Draw title + compact 3-row table."""
        # Section label
        _txb(slide, left, top, w, 0.28, title, size=9, bold=True, color=C_DARK)
        top += 0.28

        n_rows = len(rows_data) + 1
        n_cols = len(col_names)
        tbl = slide.shapes.add_table(
            n_rows, n_cols,
            Inches(left), Inches(top), Inches(w), Inches(h)
        ).table

        # Column widths
        label_w = Inches(1.5)
        tbl.columns[0].width = label_w
        data_w = Inches((w - 1.5) / max(n_cols - 1, 1))
        for ci in range(1, n_cols):
            tbl.columns[ci].width = data_w

        # Header row
        for ci, cname in enumerate(col_names):
            cell = tbl.cell(0, ci)
            cell.text = str(cname)
            cell.fill.solid()
            cell.fill.fore_color.rgb = C_DARK
            p_c = cell.text_frame.paragraphs[0]
            p_c.alignment = PP_ALIGN.CENTER
            r_h = p_c.runs[0] if p_c.runs else p_c.add_run()
            r_h.font.bold = True
            r_h.font.color.rgb = C_WHITE
            r_h.font.size = Pt(8)

        # Data rows
        for ri, row_dict in enumerate(rows_data):
            is_var = row_dict.get("", "") == "Var %"
            bg = C_WHITE if ri % 2 == 0 else C_ALT
            for ci, cname in enumerate(col_names):
                cell = tbl.cell(ri + 1, ci)
                val = row_dict.get(cname, "")
                cell.text = str(val)
                cell.fill.solid()
                cell.fill.fore_color.rgb = bg
                p_c = cell.text_frame.paragraphs[0]
                p_c.alignment = PP_ALIGN.LEFT if ci == 0 else PP_ALIGN.CENTER
                r_d = p_c.runs[0] if p_c.runs else p_c.add_run()
                r_d.font.size = Pt(8)
                r_d.font.bold = ci == 0
                if is_var and ci > 0:
                    try:
                        num = float(str(val).replace("+", "").replace("%", ""))
                        r_d.font.color.rgb = C_GREEN if num >= 0 else C_RED
                        r_d.font.bold = True
                    except ValueError:
                        pass

    # ════════════════════════════════════════════════════════════════════════
    # SLIDE 1: Main — KPIs + 3 tables
    # ════════════════════════════════════════════════════════════════════════
    slide1 = prs.slides.add_slide(blank_layout)

    # Header bar
    _chip(slide1, 0, 0, 13.33, 0.65, C_DARK)
    _txb(slide1, 0.3, 0.08, 9, 0.5, f"Proyección {linea_str}",
         size=20, bold=True, color=C_WHITE)
    _txb(slide1, 9.3, 0.12, 3.7, 0.4,
         f"FC {current_year} vs Real {aa_year}  ·  {mes_range_str}",
         size=10, color=C_ACCENT, align=PP_ALIGN.RIGHT)

    # ── KPI chips row ────────────────────────────────────────────────────────
    kpi_y = 0.78
    kpis = [
        ("SKUs FC " + str(current_year), str(extra_data.get("skus_fc", "—"))),
        ("SKUs vendidos " + str(aa_year), str(extra_data.get("skus_aa", "—"))),
        ("SKUs solo AA (sin FC)", str(extra_data.get("skus_solo_aa", "—"))),
        ("Instock CD (toda línea)", extra_data.get("instock_cd_str", "—")),
        ("Stock final proy. (und)", extra_data.get("stock_fc_str", "—")),
        ("Stock AA (und aprox.)", extra_data.get("stock_aa_str", "—")),
    ]
    chip_w = 1.95
    chip_gap = 0.22
    for ki, (lbl, val) in enumerate(kpis):
        kx = 0.3 + ki * (chip_w + chip_gap)
        v_color = C_RED if "solo AA" in lbl and val not in ("0", "—") else C_DARK
        _kpi_chip(slide1, kx, kpi_y, lbl, val, v_color)

    # ── 3 tables (VN / Aporte / Margen) stacked in 3 columns ───────────────
    tbl_top = 1.75
    tbl_h = 0.95    # height of each table body (3 data rows + header)
    tbl_w = 4.1
    tbl_gap = 0.22

    table_configs = [
        ("Venta Neta (CLP)", tables_data[0][1]),   # rows_vn
        ("Aporte (CLP)",     tables_data[1][1]),   # rows_aporte
        ("Margen (%)",       tables_data[2][1]),   # rows_margen
    ]

    for ti, (tbl_title, tbl_rows) in enumerate(table_configs):
        tx = 0.3 + ti * (tbl_w + tbl_gap)
        # relabel first col key from "Métrica" to ""
        remapped = [{("" if k == "Métrica" else k): v for k, v in row.items()} for row in tbl_rows]
        _mini_table(slide1, tx, tbl_top, tbl_w, tbl_h, remapped, col_names_list, tbl_title)

    # ── Divider ─────────────────────────────────────────────────────────────
    _chip(slide1, 0.3, tbl_top + tbl_h + 0.55, 12.73, 0.03, C_ACCENT)

    # ── SKUs sin forecast highlight (bottom strip) ───────────────────────────
    solo_aa_skus = extra_data.get("solo_aa_top", [])  # list of dicts: {sku, nombre, vn_aa}
    bot_y = tbl_top + tbl_h + 0.72
    _txb(slide1, 0.3, bot_y, 12.5, 0.28,
         f"⚠️  Top SKUs vendidos en {aa_year} SIN forecast en {current_year}",
         size=9, bold=True, color=C_ORANGE)
    bot_y += 0.3

    if solo_aa_skus:
        chip_item_w = 12.5 / min(len(solo_aa_skus), 5)
        for si, item in enumerate(solo_aa_skus[:5]):
            ix = 0.3 + si * chip_item_w
            _chip(slide1, ix + 0.05, bot_y, chip_item_w - 0.1, 0.9, C_ALT)
            sku_lbl = item.get("nombre", "") or item.get("sku", "")
            if len(sku_lbl) > 32:
                sku_lbl = sku_lbl[:29] + "…"
            _txb(slide1, ix + 0.12, bot_y + 0.05, chip_item_w - 0.22, 0.35,
                 sku_lbl, size=7, bold=True, color=C_DARK)
            _txb(slide1, ix + 0.12, bot_y + 0.40, chip_item_w - 0.22, 0.22,
                 f"VN AA: {item.get('vn_aa_str', '—')}",
                 size=7, bold=False, color=C_RED)
            _razon_color = C_GRAY if item.get("razon") == "Sin Mix" else C_ORANGE
            _txb(slide1, ix + 0.12, bot_y + 0.60, chip_item_w - 0.22, 0.22,
                 item.get("razon", ""),
                 size=6, bold=False, color=_razon_color)
    else:
        _txb(slide1, 0.3, bot_y, 12.5, 0.4,
             "No hay SKUs vendidos el año anterior sin forecast actual.",
             size=9, color=C_GRAY)

    # Footnote
    _txb(slide1, 0.3, 7.18, 12.73, 0.25,
         f"Línea: {linea_str}  ·  Período: {mes_range_str}  ·  Valores en CLP",
         size=7, color=C_GRAY)

    # ════════════════════════════════════════════════════════════════════════
    # SLIDE 2: SKUs sin forecast — detalle completo
    # ════════════════════════════════════════════════════════════════════════
    slide2 = prs.slides.add_slide(blank_layout)
    _chip(slide2, 0, 0, 13.33, 0.65, C_DARK)
    _txb(slide2, 0.3, 0.08, 10, 0.5,
         f"SKUs vendidos en {aa_year} SIN forecast en {current_year}  —  {linea_str}",
         size=18, bold=True, color=C_WHITE)

    all_solo = extra_data.get("solo_aa_all", [])
    if all_solo:
        # Table: SKU | Nombre | VN AA | Aporte AA | Unidades AA | Razón
        hdr2 = ["SKU", "Nombre producto", f"VN {aa_year}", f"Aporte {aa_year}", f"Und {aa_year}", "Razón"]
        n_rows2 = min(len(all_solo), 20) + 1
        tbl2 = slide2.shapes.add_table(
            n_rows2, 6,
            Inches(0.3), Inches(0.85), Inches(12.73), Inches(min(n_rows2 * 0.32, 5.8))
        ).table
        col_ws2 = [Inches(x) for x in [1.2, 4.0, 1.8, 1.8, 1.3, 1.5]]
        for ci, cw in enumerate(col_ws2):
            tbl2.columns[ci].width = cw
        for ci, hname in enumerate(hdr2):
            cell = tbl2.cell(0, ci)
            cell.text = hname
            cell.fill.solid()
            cell.fill.fore_color.rgb = C_ORANGE
            p_c = cell.text_frame.paragraphs[0]
            p_c.alignment = PP_ALIGN.CENTER
            r_h = p_c.runs[0] if p_c.runs else p_c.add_run()
            r_h.font.bold = True
            r_h.font.color.rgb = C_WHITE
            r_h.font.size = Pt(9)
        for ri, item in enumerate(all_solo[:20]):
            bg = C_WHITE if ri % 2 == 0 else C_ALT
            razon_val = item.get("razon", "")
            vals2 = [
                item.get("sku", ""),
                item.get("nombre", ""),
                item.get("vn_aa_str", "—"),
                item.get("aporte_aa_str", "—"),
                item.get("und_aa_str", "—"),
                razon_val,
            ]
            for ci, v in enumerate(vals2):
                cell = tbl2.cell(ri + 1, ci)
                cell.text = str(v)
                cell.fill.solid()
                cell.fill.fore_color.rgb = bg
                p_c = cell.text_frame.paragraphs[0]
                p_c.alignment = PP_ALIGN.LEFT if ci <= 1 else PP_ALIGN.CENTER
                r_d = p_c.runs[0] if p_c.runs else p_c.add_run()
                r_d.font.size = Pt(9)
                if ci == 5:
                    # Colorear la columna Razón: gris=Sin Mix, naranja=Sin Forecast
                    r_d.font.color.rgb = C_GRAY if razon_val == "Sin Mix" else C_ORANGE
                    r_d.font.bold = True
    else:
        _txb(slide2, 0.3, 1.5, 12.5, 0.5,
             "No hay SKUs vendidos el año anterior sin forecast actual.",
             size=12, color=C_GRAY, align=PP_ALIGN.CENTER)

    _txb(slide2, 0.3, 7.18, 12.73, 0.25,
         f"Línea: {linea_str}  ·  Valores en CLP", size=7, color=C_GRAY)

    # ════════════════════════════════════════════════════════════════════════
    # SLIDE 3: SKUs que bajaron (VN FC < VN AA con caída significativa)
    # ════════════════════════════════════════════════════════════════════════
    slide3 = prs.slides.add_slide(blank_layout)
    _chip(slide3, 0, 0, 13.33, 0.65, C_DARK)
    _txb(slide3, 0.3, 0.08, 10, 0.5,
         f"SKUs con mayor caída: FC {current_year} vs Real {aa_year}  —  {linea_str}",
         size=18, bold=True, color=C_WHITE)

    declining = extra_data.get("declining_skus", [])
    if declining:
        hdr3 = ["SKU", "Nombre producto", f"VN FC {current_year}", f"VN Real {aa_year}", "Var %"]
        n_rows3 = min(len(declining), 20) + 1
        tbl3 = slide3.shapes.add_table(
            n_rows3, 5,
            Inches(0.3), Inches(0.85), Inches(12.73), Inches(min(n_rows3 * 0.32, 5.8))
        ).table
        col_ws3 = [Inches(x) for x in [1.4, 4.5, 2.0, 2.0, 1.5]]
        for ci, cw in enumerate(col_ws3):
            tbl3.columns[ci].width = cw
        for ci, hname in enumerate(hdr3):
            cell = tbl3.cell(0, ci)
            cell.text = hname
            cell.fill.solid()
            cell.fill.fore_color.rgb = C_DARK
            p_c = cell.text_frame.paragraphs[0]
            p_c.alignment = PP_ALIGN.CENTER
            r_h = p_c.runs[0] if p_c.runs else p_c.add_run()
            r_h.font.bold = True
            r_h.font.color.rgb = C_WHITE
            r_h.font.size = Pt(9)
        for ri, item in enumerate(declining[:20]):
            bg = C_WHITE if ri % 2 == 0 else C_ALT
            vals3 = [
                item.get("sku", ""),
                item.get("nombre", ""),
                item.get("vn_fc_str", "—"),
                item.get("vn_aa_str", "—"),
                item.get("var_str", "—"),
            ]
            for ci, v in enumerate(vals3):
                cell = tbl3.cell(ri + 1, ci)
                cell.text = str(v)
                cell.fill.solid()
                cell.fill.fore_color.rgb = bg
                p_c = cell.text_frame.paragraphs[0]
                p_c.alignment = PP_ALIGN.LEFT if ci <= 1 else PP_ALIGN.CENTER
                r_d = p_c.runs[0] if p_c.runs else p_c.add_run()
                r_d.font.size = Pt(9)
                if ci == 4:
                    r_d.font.color.rgb = C_RED
                    r_d.font.bold = True
    else:
        _txb(slide3, 0.3, 1.5, 12.5, 0.5,
             "No se encontraron SKUs con caída significativa.",
             size=12, color=C_GRAY, align=PP_ALIGN.CENTER)

    _txb(slide3, 0.3, 7.18, 12.73, 0.25,
         f"Línea: {linea_str}  ·  Valores en CLP  ·  Solo SKUs con AA > 0 y FC < AA",
         size=7, color=C_GRAY)

    buf = _io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


# ============================================================================
# ABC-XYZ ON-DEMAND + INSTOCK DASHBOARD
# ============================================================================

def _calc_abc_xyz_ondemand(_conn):
    """ABC-XYZ-FSN classification (delegates to centralized cache).
    Returns df with SKU_PRODUCTO, CLASE_ABC, CLASE_XYZ, CLASE_FSN, CLASE_COMBINADA."""
    return cq.abc_xyz_fsn(_conn)


@st.cache_data(ttl=3600)
def _load_mix_oficial(_conn):
    """Load MIX_OFICIAL (and MODELO) from maestra to enrich instock dashboard."""
    df_m = cq.maestra(_conn)
    keep = ["SKU_PRODUCTO"] + [c for c in ["MIX_OFICIAL", "MODELO"] if c in df_m.columns]
    return df_m[keep].drop_duplicates("SKU_PRODUCTO")


def _render_instock_dashboard(df_proy, conn):
    """Render Instock Rate dashboard: monthly instock % by SKU/line, with optional ABC-XYZ overlay."""

    st.markdown("---")
    st.markdown("### 📡 Dashboard Instock")
    st.caption(
        "Instock Rate = SUM(días con stock) / SUM(días del mes) por grupo. "
        "CD = sólo centro de distribución · CIA = sin lost sales totales"
    )

    instock_cols_avail = [c for c in ["INSTOCK_CD", "INSTOCK_TIENDA", "INSTOCK_CIA"] if c in df_proy.columns]
    if not instock_cols_avail:
        st.info("No hay columnas INSTOCK en la proyección. Genera la proyección primero.")
        return

    tab_gen, tab_abc = st.tabs(["📊 Vista General", "🔬 Por Clase ABC-XYZ"])

    # ── Shared: build per-SKU×period instock table ──────────────────────────
    df_is = df_proy[
        df_proy["TIPO_DATO"].isin(["PROYECCION", "HISTORICO", "REAL+FC"])
    ].copy()

    # Normalize string dimension columns: fillna(0) in earlier merges can set them to 0
    # Replace "0", "nan", "None" → actual NaN so filters work correctly
    for _dim_col in ["AREA", "LINEA", "SUBLINEA", "MARCA", "PROCEDENCIA", "MIX_OFICIAL", "MODELO"]:
        if _dim_col in df_is.columns:
            df_is[_dim_col] = df_is[_dim_col].astype(str).replace(
                {"0": pd.NA, "nan": pd.NA, "None": pd.NA, "": pd.NA}
            )

    # Enrich MIX_OFICIAL directly from maestra (independent of projection generation date)
    _needs_mix = (
        "MIX_OFICIAL" not in df_is.columns
        or df_is["MIX_OFICIAL"].isna().all()
    )
    if _needs_mix:
        try:
            _mix_df = _load_mix_oficial(conn)
            # drop existing MIX_OFICIAL/MODELO cols before merge to avoid _x/_y suffixes
            for _c in ["MIX_OFICIAL", "MODELO"]:
                if _c in df_is.columns:
                    df_is = df_is.drop(columns=[_c])
            df_is = df_is.merge(_mix_df, on="SKU_PRODUCTO", how="left")
        except Exception:
            pass  # silently skip if Snowflake unavailable

    # Add period label (YYYYMM → "Feb 25")
    if "PERIODO_MES" in df_is.columns and "PERIODO_ANO" in df_is.columns:
        _ano_int = pd.to_numeric(df_is["PERIODO_ANO"], errors="coerce").fillna(0).astype(int)
        _mes_int = pd.to_numeric(df_is["PERIODO_MES"], errors="coerce").fillna(0).astype(int)
        df_is["_MES_LBL"] = (
            _mes_int.map(_MES_NAMES).fillna("?")
            + " " + _ano_int.astype(str).str[-2:]
        )
        df_is["_PERIODO_SORT"] = _ano_int * 100 + _mes_int
    else:
        df_is["_MES_LBL"] = df_is["PERIODO"].astype(str)
        df_is["_PERIODO_SORT"] = df_is["PERIODO"].astype(str)

    # ── Tab 1: Vista General ─────────────────────────────────────────────────
    with tab_gen:

        def _clean_opts(series):
            """Return sorted unique non-null, non-'nan' string values."""
            return sorted([
                str(x) for x in series.dropna().unique()
                if str(x).strip() and str(x).lower() != "nan"
            ])

        # ── Row 1: dimension filters (cascading) ──
        f1, f2, f3, f4, f5 = st.columns(5)
        with f1:
            areas_opt = _clean_opts(df_is["AREA"])
            sel_area_is = st.selectbox("Área", ["Todos"] + areas_opt, key="is_area")
        with f2:
            base_is = df_is if sel_area_is == "Todos" else df_is[df_is["AREA"].astype(str) == sel_area_is]
            lineas_opt = _clean_opts(base_is["LINEA"])
            sel_linea_is = st.selectbox("Línea", ["Todos"] + lineas_opt, key="is_linea")
        with f3:
            base_is2 = base_is if sel_linea_is == "Todos" else base_is[base_is["LINEA"].astype(str) == sel_linea_is]
            sublineas_opt = _clean_opts(base_is2["SUBLINEA"]) if "SUBLINEA" in base_is2.columns else []
            sel_sublinea_is = st.selectbox("Sublínea", ["Todos"] + sublineas_opt, key="is_sublinea")
        with f4:
            base_is3 = base_is2 if sel_sublinea_is == "Todos" else base_is2[base_is2["SUBLINEA"].astype(str) == sel_sublinea_is]
            marcas_opt = _clean_opts(base_is3["MARCA"])
            sel_marca_is = st.selectbox("Marca", ["Todos"] + marcas_opt, key="is_marca")
        with f5:
            # Mix Oficial filter
            if "MIX_OFICIAL" in df_is.columns:
                mix_vals = _clean_opts(df_is["MIX_OFICIAL"])
                if mix_vals:
                    sel_mix = st.selectbox("Mix Oficial", ["Todos"] + mix_vals, key="is_mix")
                else:
                    sel_mix = "Todos"
                    st.selectbox("Mix Oficial", ["Todos"], key="is_mix", disabled=True,
                                 help="Sin valores en MIX_OFICIAL")
            else:
                sel_mix = "Todos"
                st.selectbox("Mix Oficial", ["Todos"], key="is_mix", disabled=True,
                             help="Re-genera la proyección para incluir MIX_OFICIAL")

        # ── Row 2: SKU, Nombre, ABC-XYZ, Tipo Instock ──
        g1, g2, g3, g4 = st.columns([2, 2, 2, 1])
        with g1:
            # SKU multiselect — options cascade from dimension filters above
            _base_sku = base_is3 if sel_marca_is == "Todos" else base_is3[base_is3["MARCA"].astype(str) == sel_marca_is]
            _sku_opts = sorted(_base_sku["SKU_PRODUCTO"].dropna().unique().astype(str).tolist())
            sel_skus_is = st.multiselect("SKU (multi)", _sku_opts, key="is_skus",
                                         help="Deja vacío para todos los SKUs")
        with g2:
            sel_nombre_is = st.text_input("Nombre Producto contiene", "", key="is_nombre",
                                          help="Búsqueda parcial en SKU_NOM_PRODUCTO (case insensitive)")
        with g3:
            # ABC-XYZ class filter — on-demand from session_state
            _abc_classes_avail = []
            if "_instock_abc" in st.session_state:
                _abc_df_tmp = st.session_state["_instock_abc"]
                if "CLASE_COMBINADA" in _abc_df_tmp.columns:
                    _abc_classes_avail = sorted(_abc_df_tmp["CLASE_COMBINADA"].dropna().unique().tolist())
            if _abc_classes_avail:
                sel_abc_class_is = st.multiselect("Clase ABC-XYZ", _abc_classes_avail, key="is_abc_class",
                                                  help="Calcula ABC-XYZ en pestaña 'Por Clase ABC-XYZ' primero")
            else:
                sel_abc_class_is = []
                st.multiselect("Clase ABC-XYZ", [], key="is_abc_class_disabled", disabled=True,
                               help="Calcula ABC-XYZ en la pestaña 'Por Clase ABC-XYZ' primero")
        with g4:
            instock_label_map = {"CD": "INSTOCK_CD", "Tienda": "INSTOCK_TIENDA", "Compañía": "INSTOCK_CIA"}
            instock_opts = [k for k, v in instock_label_map.items() if v in instock_cols_avail]
            sel_instock_type = st.selectbox("Tipo Instock", instock_opts, key="is_tipo")
        instock_col = instock_label_map[sel_instock_type]

        # ── Apply all filters ──
        _m = df_is.copy()
        if sel_area_is != "Todos":
            _m = _m[_m["AREA"].astype(str) == sel_area_is]
        if sel_linea_is != "Todos":
            _m = _m[_m["LINEA"].astype(str) == sel_linea_is]
        if sel_sublinea_is != "Todos" and "SUBLINEA" in _m.columns:
            _m = _m[_m["SUBLINEA"].astype(str) == sel_sublinea_is]
        if sel_marca_is != "Todos":
            _m = _m[_m["MARCA"].astype(str) == sel_marca_is]
        if sel_mix != "Todos" and "MIX_OFICIAL" in _m.columns:
            _m = _m[_m["MIX_OFICIAL"].astype(str) == sel_mix]
        if sel_skus_is:
            _m = _m[_m["SKU_PRODUCTO"].astype(str).isin(sel_skus_is)]
        if sel_nombre_is.strip() and "SKU_NOM_PRODUCTO" in _m.columns:
            _m = _m[_m["SKU_NOM_PRODUCTO"].fillna("").astype(str).str.contains(sel_nombre_is.strip(), case=False, na=False)]
        if sel_abc_class_is and "_instock_abc" in st.session_state:
            _abc_skus = st.session_state["_instock_abc"]
            _abc_skus_filt = _abc_skus[_abc_skus["CLASE_COMBINADA"].isin(sel_abc_class_is)]["SKU_PRODUCTO"]
            _m = _m[_m["SKU_PRODUCTO"].isin(_abc_skus_filt)]

        if _m.empty:
            st.warning("No hay datos con los filtros seleccionados.")
        else:
            # ── Mapping instock flag → INSTOCK_DIAS_* column ──
            _dias_map = {
                "INSTOCK_CD": "INSTOCK_DIAS_CD",
                "INSTOCK_TIENDA": "INSTOCK_DIAS_TIENDA",
                "INSTOCK_CIA": "INSTOCK_DIAS_CIA",
            }
            _dias_col = _dias_map.get(instock_col, None)
            _use_dias = (
                _dias_col is not None
                and _dias_col in _m.columns
                and "DIAS_MES" in _m.columns
            )

            # ── KPIs ──
            if _use_dias:
                _sum_dias = _m[_dias_col].sum()
                _sum_dm = _m["DIAS_MES"].sum()
                instock_rate_avg = (_sum_dias / _sum_dm * 100) if _sum_dm > 0 else 0.0
            else:
                instock_rate_avg = _m[instock_col].mean() * 100
            periodos_uniq = _m["_PERIODO_SORT"].nunique()

            # SKUs con instock < 80% en ALGÚN período (using DIAS/DIAS_MES per SKU×period)
            if _use_dias:
                _sp = _m.groupby(["SKU_PRODUCTO", "_PERIODO_SORT"]).agg(
                    _sd=(_dias_col, "sum"), _dm=("DIAS_MES", "sum")
                ).reset_index()
                _sp["_rate"] = np.where(_sp["_dm"] > 0, _sp["_sd"] / _sp["_dm"], 0.0)
            else:
                _sp = _m.groupby(["SKU_PRODUCTO", "_PERIODO_SORT"])[instock_col].mean().reset_index()
                _sp.columns = ["SKU_PRODUCTO", "_PERIODO_SORT", "_rate"]
            skus_con_problema = int((_sp["_rate"] < 0.8).groupby(_sp["SKU_PRODUCTO"]).any().sum())
            total_skus = _m["SKU_PRODUCTO"].nunique()

            k1, k2, k3, k4 = st.columns(4)
            k1.metric(f"Instock Rate ({sel_instock_type})", f"{instock_rate_avg:.1f}%")
            k2.metric("SKUs con Instock < 80%", f"{skus_con_problema} / {total_skus}")
            k3.metric("Períodos Analizados", periodos_uniq)
            k4.metric("SKUs Totales", total_skus)

            st.divider()

            # ── Resumen mensual ──
            sorted_periods = sorted(_m["_PERIODO_SORT"].unique())
            period_lbl = (
                _m[["_PERIODO_SORT", "_MES_LBL"]].drop_duplicates()
                .set_index("_PERIODO_SORT")["_MES_LBL"].to_dict()
            )

            if _use_dias:
                monthly_sum = _m.groupby("_PERIODO_SORT").agg(
                    _SUM_DIAS=(_dias_col, "sum"),
                    _SUM_DM=("DIAS_MES", "sum"),
                    TOTAL_SKU=(_dias_col, "count"),
                ).reset_index()
                monthly_sum["INSTOCK_RATE_PCT"] = np.where(
                    monthly_sum["_SUM_DM"] > 0,
                    monthly_sum["_SUM_DIAS"] / monthly_sum["_SUM_DM"] * 100,
                    0.0,
                )
            else:
                monthly_sum = _m.groupby("_PERIODO_SORT").agg(
                    INSTOCK_AVG=(instock_col, "mean"),
                    TOTAL_SKU=(instock_col, "count"),
                ).reset_index()
                monthly_sum["INSTOCK_RATE_PCT"] = monthly_sum["INSTOCK_AVG"] * 100
            # SKUs con instock < 80%: count SKUs whose DIAS/DIAS_MES is below threshold
            if _use_dias:
                sku_period = _m.groupby(["SKU_PRODUCTO", "_PERIODO_SORT"]).agg(
                    _sd=(_dias_col, "sum"), _dm=("DIAS_MES", "sum")
                ).reset_index()
                sku_period["_rate"] = np.where(sku_period["_dm"] > 0, sku_period["_sd"] / sku_period["_dm"], 0.0)
            else:
                sku_period = _m.groupby(["SKU_PRODUCTO", "_PERIODO_SORT"])[instock_col].mean().reset_index()
                sku_period.rename(columns={instock_col: "_rate"}, inplace=True)
            quiebre_by_period = sku_period[sku_period["_rate"] < 0.8].groupby("_PERIODO_SORT").size().reset_index(name="QUIEBRE_SKUS")
            monthly_sum = monthly_sum.merge(quiebre_by_period, on="_PERIODO_SORT", how="left")
            monthly_sum["QUIEBRE_SKUS"] = monthly_sum["QUIEBRE_SKUS"].fillna(0).astype(int)
            monthly_sum["MES"] = monthly_sum["_PERIODO_SORT"].map(period_lbl)
            monthly_sum = monthly_sum.sort_values("_PERIODO_SORT")

            col_left, col_right = st.columns([2, 3])

            with col_left:
                st.markdown("**Resumen por Período**")
                tbl_disp = monthly_sum[["MES", "INSTOCK_RATE_PCT", "TOTAL_SKU", "QUIEBRE_SKUS"]].copy()
                tbl_disp.columns = ["Período", "Instock %", "SKUs Totales", "InStock < 80%"]
                tbl_disp["Instock %"] = tbl_disp["Instock %"].round(1)
                tbl_disp["SKUs Totales"] = tbl_disp["SKUs Totales"].astype(int)

                def color_instock_pct(val):
                    try:
                        v = float(val)
                        if v >= 90:
                            return "color: #1a7f37; font-weight: bold"
                        elif v >= 75:
                            return "color: #bf8700"
                        else:
                            return "color: #cf222e; font-weight: bold"
                    except Exception:
                        return ""

                styled_tbl = tbl_disp.style.map(color_instock_pct, subset=["Instock %"])
                st.dataframe(styled_tbl, use_container_width=True, hide_index=True)

            with col_right:
                st.markdown("**Instock Rate % por Mes**")
                _chart_meses = [period_lbl[p] for p in sorted_periods]
                _chart_vals = monthly_sum.sort_values("_PERIODO_SORT")["INSTOCK_RATE_PCT"].tolist()

                fig_is = go.Figure()
                # Area fill (semi-transparent)
                fig_is.add_trace(go.Scatter(
                    x=_chart_meses, y=_chart_vals,
                    fill="tozeroy",
                    fillcolor="rgba(6, 94, 139, 0.10)",
                    line=dict(width=0),
                    showlegend=False, hoverinfo="skip",
                ))
                # Main line with markers and data labels
                fig_is.add_trace(go.Scatter(
                    x=_chart_meses, y=_chart_vals,
                    mode="lines+markers+text",
                    line=dict(color="#065E8B", width=3, shape="spline"),
                    marker=dict(size=10, color="#065E8B", line=dict(width=2, color="white")),
                    text=[f"{v:.1f}%" for v in _chart_vals],
                    textposition="top center",
                    textfont=dict(size=11, color="#065E8B", family="Arial Black"),
                    hovertemplate="<b>%{x}</b><br>Instock: %{y:.1f}%<extra></extra>",
                    showlegend=False,
                ))
                # Reference line at 80%
                fig_is.add_hline(
                    y=80, line_dash="dash", line_color="#C94BFF", line_width=1.5,
                    annotation_text="Meta 80%", annotation_position="top left",
                    annotation_font=dict(size=10, color="#C94BFF"),
                )
                fig_is.update_layout(
                    height=310,
                    margin=dict(l=40, r=20, t=20, b=40),
                    plot_bgcolor="white",
                    yaxis=dict(
                        range=[0, 105], ticksuffix="%", tickformat=".0f",
                        gridcolor="rgba(0,0,0,0.06)", zeroline=False,
                        title="",
                    ),
                    xaxis=dict(title="", showgrid=False),
                    hovermode="x unified",
                )
                st.plotly_chart(fig_is, use_container_width=True)

            st.divider()

            # ── Heatmap: filas = agrupación, cols = período ──
            st.markdown("**Heatmap Instock por Período**")

            # Decide grouping: SKU si < 60 SKUs, else LINEA
            n_skus = _m["SKU_PRODUCTO"].nunique()
            heat_group_opts = ["LINEA", "SUBLINEA", "MARCA", "SKU"]
            hg_default = 0 if n_skus > 60 else 3
            heat_group_sel = st.radio(
                "Agrupar heatmap por:", heat_group_opts, index=hg_default, horizontal=True, key="is_heat_group"
            )

            if heat_group_sel == "SKU":
                heat_col = "SKU_PRODUCTO"
                _m["_HEAT_LBL"] = _m["SKU_PRODUCTO"].astype(str)
                if "SKU_NOM_PRODUCTO" in _m.columns:
                    _m["_HEAT_LBL"] = _m["SKU_PRODUCTO"].astype(str) + " | " + _m["SKU_NOM_PRODUCTO"].fillna("").astype(str)
            else:
                heat_col = heat_group_sel
                _m["_HEAT_LBL"] = _m[heat_col].astype(str)

            if _use_dias:
                heat_agg = _m.groupby(["_HEAT_LBL", "_PERIODO_SORT", "_MES_LBL"]).agg(
                    _SD=(_dias_col, "sum"), _DM=("DIAS_MES", "sum")
                ).reset_index()
                heat_agg["_RATE"] = np.where(heat_agg["_DM"] > 0, heat_agg["_SD"] / heat_agg["_DM"], 0.0)
            else:
                heat_agg = _m.groupby(["_HEAT_LBL", "_PERIODO_SORT", "_MES_LBL"])[instock_col].mean().reset_index()
                heat_agg.rename(columns={instock_col: "_RATE"}, inplace=True)
            heat_pivot = heat_agg.pivot_table(
                index="_HEAT_LBL", columns="_PERIODO_SORT",
                values="_RATE", aggfunc="mean",
            )
            # Rename columns to period labels (deduplicate if needed)
            new_cols = [period_lbl.get(c, str(c)) for c in heat_pivot.columns]
            seen: dict[str, int] = {}
            for i, col in enumerate(new_cols):
                if col in seen:
                    seen[col] += 1
                    new_cols[i] = f"{col} ({seen[col]})"
                else:
                    seen[col] = 0
            heat_pivot.columns = new_cols
            heat_pivot.columns.name = None
            heat_pivot.index.name = None
            heat_pivot = heat_pivot * 100  # convert to percentage

            def color_heat(val):
                try:
                    v = float(val)
                    if v >= 90:
                        return "background-color: #2dc26e; color: #fff"
                    elif v >= 75:
                        return "background-color: #f0ad4e; color: #000"
                    elif v >= 50:
                        return "background-color: #e87722; color: #fff"
                    else:
                        return "background-color: #d62728; color: #fff"
                except Exception:
                    return ""

            heat_styled = heat_pivot.style.map(color_heat).format("{:.0f}%")
            st.dataframe(heat_styled, use_container_width=True)

    # ── Tab 2: Por Clase ABC-XYZ ─────────────────────────────────────────────
    with tab_abc:
        # Instock type selector (shared control)
        sel_instock_abc = st.selectbox(
            "Tipo Instock", instock_opts, key="is_tipo_abc"
        )
        instock_col_abc = instock_label_map[sel_instock_abc]

        # Load ABC-XYZ-FSN from centralized cache (auto-computed at app startup)
        try:
            df_abc_data = cq.abc_xyz_fsn(conn)
        except Exception as e:
            st.error(f"Error cargando ABC-XYZ-FSN: {e}")
            df_abc_data = pd.DataFrame()

        if df_abc_data.empty:
            st.warning("Clasificacion ABC-XYZ no disponible. Verifica la conexion a Snowflake.")
        else:
            # Merge df_proy PROYECCION rows with ABC-XYZ-FSN
            df_is_abc = df_proy[df_proy["TIPO_DATO"].isin(["PROYECCION", "HISTORICO", "REAL+FC"])].copy()
            _merge_cols = [c for c in ["SKU_PRODUCTO", "CLASE_ABC", "CLASE_XYZ", "CLASE_FSN", "CLASE_COMBINADA"]
                          if c in df_abc_data.columns]
            df_is_abc = df_is_abc.merge(df_abc_data[_merge_cols], on="SKU_PRODUCTO", how="left")
            df_is_abc["CLASE_ABC"] = df_is_abc["CLASE_ABC"].fillna("C")
            df_is_abc["CLASE_XYZ"] = df_is_abc["CLASE_XYZ"].fillna("Z")
            df_is_abc["CLASE_COMBINADA"] = df_is_abc["CLASE_ABC"] + df_is_abc["CLASE_XYZ"]

            # Multiselect filters
            fa1, fa2, fa3 = st.columns(3)
            with fa1:
                _areas_abc = sorted(df_is_abc["AREA"].dropna().astype(str).unique()) if "AREA" in df_is_abc.columns else []
                _areas_abc = [x for x in _areas_abc if x not in ("0", "nan", "None", "")]
                sel_area_abc = st.multiselect("Área", _areas_abc, key="is_abc_area")
            with fa2:
                sel_abc_cls = st.multiselect("Clase ABC", ["A", "B", "C"], default=["A", "B", "C"], key="is_abc_cls")
            with fa3:
                sel_xyz_cls = st.multiselect("Clase XYZ", ["X", "Y", "Z"], default=["X", "Y", "Z"], key="is_xyz_cls")

            if sel_area_abc:
                df_is_abc = df_is_abc[df_is_abc["AREA"].astype(str).isin(sel_area_abc)]
            if sel_abc_cls:
                df_is_abc = df_is_abc[df_is_abc["CLASE_ABC"].isin(sel_abc_cls)]
            if sel_xyz_cls:
                df_is_abc = df_is_abc[df_is_abc["CLASE_XYZ"].isin(sel_xyz_cls)]

            if df_is_abc.empty:
                st.warning("No hay datos con los filtros seleccionados.")
            else:
                st.divider()

                # ── Heatmap 3x3 ABC × XYZ ──
                st.markdown("**Heatmap ABC-XYZ — Instock Rate (%)**")
                _dias_map_abc = {
                    "INSTOCK_CD": "INSTOCK_DIAS_CD",
                    "INSTOCK_TIENDA": "INSTOCK_DIAS_TIENDA",
                    "INSTOCK_CIA": "INSTOCK_DIAS_CIA",
                }
                _dias_col_abc = _dias_map_abc.get(instock_col_abc, None)
                _use_dias_abc = (
                    _dias_col_abc is not None
                    and _dias_col_abc in df_is_abc.columns
                    and "DIAS_MES" in df_is_abc.columns
                )
                if _use_dias_abc:
                    pivot_abc = df_is_abc.groupby(["CLASE_ABC", "CLASE_XYZ"]).agg(
                        _SD=(_dias_col_abc, "sum"), _DM=("DIAS_MES", "sum")
                    ).reset_index()
                    pivot_abc["_RATE"] = np.where(pivot_abc["_DM"] > 0, pivot_abc["_SD"] / pivot_abc["_DM"], 0.0)
                else:
                    pivot_abc = (
                        df_is_abc.groupby(["CLASE_ABC", "CLASE_XYZ"])[instock_col_abc]
                        .mean()
                        .reset_index()
                    )
                    pivot_abc["_RATE"] = pivot_abc[instock_col_abc]
                pivot_abc_wide = pivot_abc.pivot(
                    index="CLASE_ABC", columns="CLASE_XYZ", values="_RATE"
                ) * 100

                # Ensure all 3x3 cells exist
                for cls in ["A", "B", "C"]:
                    if cls not in pivot_abc_wide.index:
                        pivot_abc_wide.loc[cls] = np.nan
                for cls in ["X", "Y", "Z"]:
                    if cls not in pivot_abc_wide.columns:
                        pivot_abc_wide[cls] = np.nan
                pivot_abc_wide = pivot_abc_wide.loc[["A", "B", "C"], ["X", "Y", "Z"]]

                styled_abc = (
                    pivot_abc_wide.style
                    .background_gradient(cmap="RdYlGn", vmin=0, vmax=100, axis=None)
                    .format("{:.1f}%", na_rep="—")
                )
                st.dataframe(styled_abc, use_container_width=True)
                st.caption("Verde = alto instock · Rojo = quiebre frecuente")

                st.divider()

                # ── Bar chart por clase combinada ──
                st.markdown("**Instock Rate por Clase Combinada**")
                if _use_dias_abc:
                    bar_abc = df_is_abc.groupby("CLASE_COMBINADA").agg(
                        _SD=(_dias_col_abc, "sum"), _DM=("DIAS_MES", "sum")
                    ).reset_index()
                    bar_abc["Instock %"] = np.where(bar_abc["_DM"] > 0, bar_abc["_SD"] / bar_abc["_DM"] * 100, 0.0)
                else:
                    bar_abc = (
                        df_is_abc.groupby("CLASE_COMBINADA")[instock_col_abc]
                        .mean()
                        .reset_index()
                    )
                    bar_abc["Instock %"] = bar_abc[instock_col_abc] * 100
                bar_abc = bar_abc.sort_values("CLASE_COMBINADA")

                color_map = {"A": "#065E8B", "B": "#23CED3", "C": "#F4A528"}
                bar_abc["_COLOR"] = bar_abc["CLASE_COMBINADA"].str[0].map(color_map).fillna("#888888")

                fig_abc = go.Figure(layout=dorel_layout(
                    height=280,
                    xaxis=dict(title="Clase Combinada"),
                    yaxis=dict(title="Instock Rate %", range=[0, 100]),
                ))
                for _, row in bar_abc.iterrows():
                    fig_abc.add_trace(go.Bar(
                        x=[row["CLASE_COMBINADA"]],
                        y=[row["Instock %"]],
                        marker_color=row["_COLOR"],
                        showlegend=False,
                        hovertemplate="Clase: %{x}<br>Instock Rate: %{y:.1f}%<extra></extra>",
                    ))
                fig_abc.add_hline(
                    y=80, line_dash="dash", line_color="#C94BFF", opacity=0.7,
                )
                st.plotly_chart(fig_abc, use_container_width=True)

                st.divider()

                # ── Detalle por SKU ──
                st.markdown("**Detalle por SKU — Instock Rate**")
                sku_agg_cols = ["SKU_PRODUCTO", "CLASE_COMBINADA", "CLASE_ABC", "CLASE_XYZ"]
                extra_cols = [c for c in ["SKU_NOM_PRODUCTO", "LINEA", "MARCA", "MIX_OFICIAL"] if c in df_is_abc.columns]
                sku_agg_cols += extra_cols

                if _use_dias_abc:
                    sku_detail = df_is_abc.groupby(sku_agg_cols, as_index=False).agg(
                        _SD=(_dias_col_abc, "sum"), _DM=("DIAS_MES", "sum")
                    )
                    sku_detail["INSTOCK_RATE"] = np.where(
                        sku_detail["_DM"] > 0, sku_detail["_SD"] / sku_detail["_DM"], 0.0
                    )
                    sku_detail = sku_detail.drop(columns=["_SD", "_DM"])
                else:
                    sku_detail = (
                        df_is_abc.groupby(sku_agg_cols, as_index=False)[instock_col_abc]
                        .mean()
                        .rename(columns={instock_col_abc: "INSTOCK_RATE"})
                    )
                sku_detail["INSTOCK_RATE_PCT"] = (sku_detail["INSTOCK_RATE"] * 100).round(1)
                sku_detail = sku_detail.drop(columns=["INSTOCK_RATE"]).sort_values(
                    ["CLASE_COMBINADA", "INSTOCK_RATE_PCT"]
                )

                col_cfg = {
                    "INSTOCK_RATE_PCT": st.column_config.ProgressColumn(
                        "Instock Rate %", format="%.1f%%", min_value=0, max_value=100
                    )
                }
                st.dataframe(sku_detail, use_container_width=True, hide_index=True, column_config=col_cfg)


def _render_ppt_tables(df_proy):
    """Render PPT-ready comparison tables: next 3 months FC vs AA for VN, Aporte, Margen."""

    st.markdown("---")
    st.markdown("### 📊 Tablas Comparativas — Próximos Meses vs Año Anterior")
    st.caption("Venta Neta, Aporte y Margen proyectados vs Año Anterior · Listos para PPT")

    if "VN_RES_TOTAL" not in df_proy.columns:
        st.info("Genera la proyección para ver las tablas comparativas.")
        return

    current_year = pd.Timestamp.now().year
    aa_year = current_year - 1
    mes_actual = pd.Timestamp.now().month

    # Próximos 3 meses
    meses_target = [(mes_actual + i - 1) % 12 + 1 for i in range(1, 4)]
    # Adjust year if wrapping (e.g., Nov→Dec→Jan)
    periodos_target = []
    for i, m in enumerate(meses_target):
        yr = current_year if (mes_actual + i) <= 12 else current_year + 1
        periodos_target.append(yr * 100 + m)
    mes_labels = {m: _MES_NAMES[m] for m in meses_target}

    # --- Controls ---
    ctrl1, ctrl2, ctrl3 = st.columns([2, 2, 2])
    with ctrl1:
        lineas_disp = sorted([str(x) for x in df_proy["LINEA"].dropna().unique() if str(x).strip()])
        sel_linea = st.selectbox("Línea de producto", ["Todas"] + lineas_disp, key="ppt_linea")
    with ctrl2:
        _canal_opts_ppt = ["Todos", "Tienda", "Etail", "Mayor"]
        sel_canal_ppt = st.selectbox("Canal", _canal_opts_ppt, key="ppt_canal")
    with ctrl3:
        excluir_sin_costo_ppt = st.checkbox("Excluir SKUs sin costo", key="ppt_excluir_sc")

    # --- Filter ---
    df_f = df_proy.copy()
    if sel_linea != "Todas":
        df_f = df_f[df_f["LINEA"].astype(str) == sel_linea]
    if excluir_sin_costo_ppt and "FLAG_SIN_COSTO" in df_f.columns:
        df_f = df_f[~df_f["FLAG_SIN_COSTO"].astype(bool)]

    # Map channel selection to FC column names
    _canal_col_map = {
        "Todos": ("VN_RES_TOTAL", "APORTE_RES_TOTAL", "COGS_RES_TOTAL"),
        "Tienda": ("VN_RES_TIENDA", "APORTE_RES_TIENDA", "COGS_RES_TIENDA"),
        "Etail": ("VN_RES_ETAIL", "APORTE_RES_ETAIL", "COGS_RES_ETAIL"),
        "Mayor": ("VN_RES_MAYOR", "APORTE_RES_MAYOR", "COGS_RES_MAYOR"),
    }
    _vn_col_ppt, _ap_col_ppt, _cogs_col_ppt = _canal_col_map.get(sel_canal_ppt, _canal_col_map["Todos"])

    # Map channel selection to AA CANAL_STD filter
    _canal_aa_map = {"Todos": None, "Tienda": "TIENDA", "Etail": "ETAIL", "Mayor": "MAYORISTA"}

    # FC rows for next 3 months
    _period_int = df_f["PERIODO_ANO"] * 100 + df_f["PERIODO_MES"]
    df_fc = df_f[
        df_f["TIPO_DATO"].isin(["PROYECCION", "HISTORICO", "REAL+FC"])
        & _period_int.isin(periodos_target)
    ].copy()

    if df_fc.empty:
        st.warning("No hay proyección para los próximos 3 meses con los filtros seleccionados.")
        return

    # Aggregate FC by month (using selected channel columns)
    _agg_dict_ppt = {"VN_FC": (_vn_col_ppt, "sum"), "APORTE_FC": (_ap_col_ppt, "sum")}
    fc_por_mes = df_fc.groupby("PERIODO_MES", as_index=False).agg(**_agg_dict_ppt).rename(columns={"PERIODO_MES": "MES"})
    fc_por_mes["MARGEN_FC"] = (
        fc_por_mes["APORTE_FC"] / fc_por_mes["VN_FC"].replace(0, np.nan) * 100
    )

    # --- AA data ---
    aa_monthly_full = st.session_state.get("aa_monthly")
    aa_por_mes = None
    has_aa = False

    if aa_monthly_full is not None and not aa_monthly_full.empty:
        sku_linea = df_f[["SKU_PRODUCTO", "LINEA"]].drop_duplicates()
        aa_enriched = aa_monthly_full.merge(sku_linea, on="SKU_PRODUCTO", how="left")
        if sel_linea != "Todas":
            aa_enriched = aa_enriched[aa_enriched["LINEA"].astype(str) == sel_linea]
        # Channel filter for AA
        _aa_canal_filt = _canal_aa_map.get(sel_canal_ppt)
        if _aa_canal_filt is not None and "CANAL_STD" in aa_enriched.columns:
            aa_enriched = aa_enriched[aa_enriched["CANAL_STD"] == _aa_canal_filt]
        # Exclude SKUs without cost if toggle is on
        if excluir_sin_costo_ppt and "FLAG_SIN_COSTO" in df_proy.columns:
            _valid_skus_sc = set(df_f["SKU_PRODUCTO"].unique())
            aa_enriched = aa_enriched[aa_enriched["SKU_PRODUCTO"].isin(_valid_skus_sc)]
        aa_filt = aa_enriched[aa_enriched["MES"].isin(meses_target)]
        if not aa_filt.empty:
            aa_por_mes = aa_filt.groupby("MES").agg(
                VN_AA=("NETO_AA", "sum"),
                APORTE_AA=("APORTE_AA", "sum"),
            )
            aa_por_mes["MARGEN_AA"] = (
                aa_por_mes["APORTE_AA"] / aa_por_mes["VN_AA"].replace(0, np.nan) * 100
            )
            has_aa = True

    if not has_aa:
        st.info(f"No hay datos de Año Anterior ({aa_year}) disponibles. Se muestra solo Forecast.")

    # --- Build tables ---
    def fmt_pct(v):
        if pd.isna(v):
            return "N/D"
        return f"{v:.1f}%"

    _canal_label_ppt = f" ({sel_canal_ppt})" if sel_canal_ppt != "Todos" else ""
    rows_vn = _build_ppt_table_data(fc_por_mes, aa_por_mes, meses_target, mes_labels, "VN_FC", "VN_AA", _fmt_cl)
    rows_aporte = _build_ppt_table_data(fc_por_mes, aa_por_mes, meses_target, mes_labels, "APORTE_FC", "APORTE_AA", _fmt_cl)
    rows_margen = _build_ppt_table_data(fc_por_mes, aa_por_mes, meses_target, mes_labels, "MARGEN_FC", "MARGEN_AA", fmt_pct)

    col_names = ["Métrica"] + [mes_labels[m] for m in meses_target] + ["Total"]

    def show_table(title, rows):
        st.markdown(f"**{title}**")
        df_disp = pd.DataFrame(rows, columns=col_names)
        styled = _style_ppt_table(df_disp)
        st.dataframe(styled, use_container_width=True, hide_index=True)

    t1, t2, t3 = st.tabs([
        f"💰 Venta Neta",
        f"📈 Aporte",
        f"📉 Margen",
    ])
    with t1:
        show_table(f"Venta Neta Proyectada vs Real {aa_year}{_canal_label_ppt}", rows_vn)
    with t2:
        show_table(f"Aporte Proyectado vs Real {aa_year}{_canal_label_ppt}", rows_aporte)
    with t3:
        show_table(f"Margen Proyectado vs Real {aa_year}{_canal_label_ppt}", rows_margen)

    # --- PPT download ---
    st.markdown("")
    tables_data = [
        (f"Venta Neta — FC {current_year} vs Real {aa_year}", rows_vn),
        (f"Aporte — FC {current_year} vs Real {aa_year}", rows_aporte),
        (f"Margen — FC {current_year} vs Real {aa_year}", rows_margen),
    ]

    # ── Build extra_data for PPT KPIs + SKU slides ──────────────────────────
    try:
        # SKU counts
        skus_fc = int(df_f[df_f["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])]["SKU_PRODUCTO"].nunique())
        skus_aa = int(df_f[df_f["AA_VN_TOTAL"] > 0]["SKU_PRODUCTO"].nunique()) if "AA_VN_TOTAL" in df_f.columns else 0
        skus_solo_aa = int(df_f[df_f["TIPO_DATO"] == "SOLO_AA"]["SKU_PRODUCTO"].nunique())

        # Instock CD — average over projected periods
        if "INSTOCK_CD" in df_fc.columns:
            instock_cd_val = df_fc["INSTOCK_CD"].mean() * 100
            instock_cd_str = f"{instock_cd_val:.1f}%"
        else:
            instock_cd_str = "—"

        # Stock final projected (units, sum of next 3 months last period per SKU)
        if "STOCK_FINAL_TOTAL" in df_fc.columns:
            # last projected period per SKU
            last_period = df_fc.groupby("SKU_PRODUCTO")["PERIODO_MES"].max()
            df_fc_last = df_fc.merge(last_period.rename("_LAST"), on="SKU_PRODUCTO")
            df_fc_last = df_fc_last[df_fc_last["PERIODO_MES"] == df_fc_last["_LAST"]]
            stock_fc = df_fc_last["STOCK_FINAL_TOTAL"].sum()
            stock_fc_str = f"{stock_fc:,.0f}"
        else:
            stock_fc_str = "—"

        # AA units total (all months, used as proxy for "AA stock baseline")
        if "AA_UND_TOTAL" in df_f.columns:
            stock_aa = df_f[df_f["TIPO_DATO"].isin(["PROYECCION", "HISTORICO", "SOLO_AA"])]["AA_UND_TOTAL"].sum()
            stock_aa_str = f"{stock_aa:,.0f}"
        else:
            stock_aa_str = "—"

        # Solo AA top SKUs (sold last year, not forecasted this year)
        df_solo = df_f[df_f["TIPO_DATO"] == "SOLO_AA"].copy()
        solo_aa_all = []
        solo_aa_top = []
        if not df_solo.empty and "AA_VN_TOTAL" in df_solo.columns:
            df_solo_agg = (
                df_solo.groupby("SKU_PRODUCTO", as_index=False)
                .agg(
                    VN_AA=("AA_VN_TOTAL", "sum"),
                    APORTE_AA=("AA_APORTE_TOTAL", "sum") if "AA_APORTE_TOTAL" in df_solo.columns else ("AA_VN_TOTAL", "sum"),
                    UND_AA=("AA_UND_TOTAL", "sum") if "AA_UND_TOTAL" in df_solo.columns else ("AA_VN_TOTAL", "count"),
                )
                .sort_values("VN_AA", ascending=False)
            )
            # Attach nombre + mix_oficial para calcular razón sin forecast
            nom_map = df_f[["SKU_PRODUCTO", "SKU_NOM_PRODUCTO"]].drop_duplicates("SKU_PRODUCTO").set_index("SKU_PRODUCTO")["SKU_NOM_PRODUCTO"].to_dict() if "SKU_NOM_PRODUCTO" in df_f.columns else {}
            mix_map = df_solo[["SKU_PRODUCTO", "MIX_OFICIAL"]].drop_duplicates("SKU_PRODUCTO").set_index("SKU_PRODUCTO")["MIX_OFICIAL"].to_dict() if "MIX_OFICIAL" in df_solo.columns else {}
            for _, row_s in df_solo_agg.iterrows():
                sku_id = row_s["SKU_PRODUCTO"]
                mix_val = str(mix_map.get(sku_id, "")).strip().upper()
                # "Sin Mix" si MIX_OFICIAL no es "MIX" ni "IN & OUT" → fuera del portafolio activo
                # "Sin Forecast" si es MIX o IN & OUT pero no tiene forecast en los períodos target
                _es_mix_activo = mix_val in ("MIX", "IN & OUT")
                razon = "Sin Forecast" if _es_mix_activo else "Sin Mix"
                item = {
                    "sku": str(sku_id),
                    "nombre": str(nom_map.get(sku_id, "")),
                    "vn_aa_str": _fmt_mm(row_s["VN_AA"]),
                    "aporte_aa_str": _fmt_mm(row_s.get("APORTE_AA", 0)),
                    "und_aa_str": f"{row_s.get('UND_AA', 0):,.0f}",
                    "vn_aa": float(row_s["VN_AA"]),
                    "razon": razon,
                }
                solo_aa_all.append(item)
            solo_aa_top = solo_aa_all[:5]

        # Declining SKUs — forecasted this year but VN_FC < VN_AA (significant drop)
        declining_skus = []
        if "AA_VN_TOTAL" in df_f.columns and "VN_RES_TOTAL" in df_f.columns:
            df_proj = df_f[df_f["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])].copy()
            fc_by_sku = df_proj.groupby("SKU_PRODUCTO", as_index=False)["VN_RES_TOTAL"].sum().rename(columns={"VN_RES_TOTAL": "VN_FC"})
            aa_by_sku = (
                df_f[df_f["TIPO_DATO"].isin(["PROYECCION", "HISTORICO", "REAL+FC"])]
                .groupby("SKU_PRODUCTO", as_index=False)["AA_VN_TOTAL"].max()
                .rename(columns={"AA_VN_TOTAL": "VN_AA"})
            )
            comp = fc_by_sku.merge(aa_by_sku, on="SKU_PRODUCTO", how="inner")
            comp = comp[(comp["VN_AA"] > 0) & (comp["VN_FC"] < comp["VN_AA"])]
            comp["VAR_PCT"] = (comp["VN_FC"] - comp["VN_AA"]) / comp["VN_AA"] * 100
            comp = comp.sort_values("VAR_PCT").head(20)
            nom_map2 = df_f[["SKU_PRODUCTO", "SKU_NOM_PRODUCTO"]].drop_duplicates("SKU_PRODUCTO").set_index("SKU_PRODUCTO")["SKU_NOM_PRODUCTO"].to_dict() if "SKU_NOM_PRODUCTO" in df_f.columns else {}
            for _, row_d in comp.iterrows():
                declining_skus.append({
                    "sku": str(row_d["SKU_PRODUCTO"]),
                    "nombre": str(nom_map2.get(row_d["SKU_PRODUCTO"], "")),
                    "vn_fc_str": _fmt_mm(row_d["VN_FC"]),
                    "vn_aa_str": _fmt_mm(row_d["VN_AA"]),
                    "var_str": f"{row_d['VAR_PCT']:+.1f}%",
                })

        extra_data = {
            "skus_fc": skus_fc,
            "skus_aa": skus_aa,
            "skus_solo_aa": skus_solo_aa,
            "instock_cd_str": instock_cd_str,
            "stock_fc_str": stock_fc_str,
            "stock_aa_str": stock_aa_str,
            "solo_aa_top": solo_aa_top,
            "solo_aa_all": solo_aa_all,
            "declining_skus": declining_skus,
        }
    except Exception as _ex:
        extra_data = {}

    try:
        ppt_buf = _generate_ppt_file(tables_data, meses_target, mes_labels, sel_linea, current_year, extra_data)
        linea_slug = sel_linea.replace(" ", "_") if sel_linea != "Todas" else "Todas"
        mes_slug = f"{mes_labels[meses_target[0]]}-{mes_labels[meses_target[-1]]}"
        st.download_button(
            label="⬇️ Descargar PPT",
            data=ppt_buf,
            file_name=f"tablas_comparativas_{linea_slug}_{mes_slug}_{current_year}.pptx",
            mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
    except Exception as e:
        st.warning(f"No se pudo generar el PPT: {e}")


# ============================================================================
# SCENARIO COMPARISON
# ============================================================================

MESES_CORTO = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}


def _render_comparison_results(df1, df2, label1, label2):
    """Render side-by-side comparison charts and tables for two scenario DataFrames."""

    # ── Metric selector + filters ────────────────────────────────────────
    _m1, _m2, _m3 = st.columns([2, 2, 2])
    with _m1:
        metric_options = {
            "Venta Neta (S/)": "VN_RES_TOTAL",
            "COGS (S/)": "COGS_RES_TOTAL",
            "Aporte (S/)": "APORTE_RES_TOTAL",
            "Unidades Vendidas": "VENTA_FUL_TOTAL_UND",
            "Venta Neta Tienda": "VN_RES_TIENDA",
            "Venta Neta Etail": "VN_RES_ETAIL",
            "Venta Neta Mayor": "VN_RES_MAYOR",
            "Lost Sales (und)": "LOST_SALES_TOTAL",
            "Stock Final (und)": "STOCK_FINAL_TOTAL",
        }
        sel_metrics = st.multiselect(
            "Métricas a comparar",
            list(metric_options.keys()),
            default=["Venta Neta (S/)", "Aporte (S/)", "Unidades Vendidas"],
            key="esc_metrics",
        )
    with _m2:
        _areas_esc = sorted(set(
            list(df1["AREA"].dropna().unique()) + list(df2["AREA"].dropna().unique())
        )) if "AREA" in df1.columns and "AREA" in df2.columns else []
        sel_area_esc = st.multiselect("Filtro Área", _areas_esc, key="esc_area")
    with _m3:
        _src_l = df1 if not sel_area_esc else df1[df1["AREA"].isin(sel_area_esc)]
        _lineas_esc = sorted(set(
            list(_src_l["LINEA"].dropna().unique()) +
            list(df2["LINEA"].dropna().unique())
        )) if "LINEA" in df1.columns and "LINEA" in df2.columns else []
        sel_linea_esc = st.multiselect("Filtro Línea", _lineas_esc, key="esc_linea")

    if not sel_metrics:
        st.warning("Selecciona al menos una métrica.")
        return

    def _filter_esc(df):
        _d = df.copy()
        if sel_area_esc and "AREA" in _d.columns:
            _d = _d[_d["AREA"].isin(sel_area_esc)]
        if sel_linea_esc and "LINEA" in _d.columns:
            _d = _d[_d["LINEA"].isin(sel_linea_esc)]
        if "TIPO_DATO" in _d.columns:
            _d = _d[_d["TIPO_DATO"].isin(["PROYECCION", "REAL+FC", "HISTORICO"])]
        return _d

    df1_f = _filter_esc(df1)
    df2_f = _filter_esc(df2)

    def _agg_monthly(df, metric_col):
        if "PERIODO" not in df.columns:
            return pd.DataFrame(columns=["PERIODO", "VALOR"])
        if metric_col not in df.columns:
            if metric_col == "VENTA_FUL_TOTAL_UND":
                _parts = ["VENTA_FUL_TIENDA_UND", "VENTA_FUL_ETAIL_UND", "VENTA_FUL_MAYOR_UND"]
                _existing = [c for c in _parts if c in df.columns]
                if _existing:
                    df = df.copy()
                    df[metric_col] = sum(df[c].fillna(0) for c in _existing)
                else:
                    return pd.DataFrame(columns=["PERIODO", "VALOR"])
            else:
                return pd.DataFrame(columns=["PERIODO", "VALOR"])
        agg = df.groupby("PERIODO", as_index=False)[metric_col].sum()
        agg.rename(columns={metric_col: "VALOR"}, inplace=True)
        return agg.sort_values("PERIODO")

    # ── Per-metric comparison ────────────────────────────────────────────
    st.markdown("---")

    for metric_label in sel_metrics:
        metric_col = metric_options[metric_label]
        agg1 = _agg_monthly(df1_f, metric_col)
        agg2 = _agg_monthly(df2_f, metric_col)

        if agg1.empty and agg2.empty:
            st.caption(f"Sin datos para **{metric_label}**")
            continue

        merged = pd.merge(
            agg1.rename(columns={"VALOR": "ESC_1"}),
            agg2.rename(columns={"VALOR": "ESC_2"}),
            on="PERIODO", how="outer",
        ).fillna(0).sort_values("PERIODO")

        merged["DELTA"] = merged["ESC_2"] - merged["ESC_1"]
        merged["DELTA_%"] = np.where(
            merged["ESC_1"] > 0,
            merged["DELTA"] / merged["ESC_1"] * 100,
            0.0,
        )
        def _periodo_label(p):
            if pd.isna(p):
                return str(p)
            if isinstance(p, (pd.Timestamp, datetime)):
                return f"{MESES_CORTO.get(p.month, '?')} {p.year}"
            s = str(int(p)) if not isinstance(p, str) else str(p)
            if len(s) >= 6:
                return f"{MESES_CORTO.get(int(s[4:6]), '?')} {s[:4]}"
            return s

        merged["MES_LABEL"] = merged["PERIODO"].apply(_periodo_label)

        st.markdown(f"#### {metric_label}")

        total1 = merged["ESC_1"].sum()
        total2 = merged["ESC_2"].sum()
        delta_total = total2 - total1
        delta_pct = (delta_total / total1 * 100) if total1 > 0 else 0
        is_monetary = "S/" in metric_label or metric_label in ("COGS (S/)", "Aporte (S/)")

        _k1, _k2, _k3 = st.columns(3)
        if is_monetary:
            _k1.metric(f"📌 {label1}", _fmt_cl(total1))
            _k2.metric(f"📌 {label2}", _fmt_cl(total2))
            _k3.metric("Δ Diferencia", _fmt_cl(delta_total), f"{delta_pct:+.1f}%")
        else:
            _k1.metric(f"📌 {label1}", f"{total1:,.0f}")
            _k2.metric(f"📌 {label2}", f"{total2:,.0f}")
            _k3.metric("Δ Diferencia", f"{delta_total:+,.0f}", f"{delta_pct:+.1f}%")

        # Chart
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=merged["MES_LABEL"], y=merged["ESC_1"], name=label1,
            marker_color="#065E8B",
            text=[_fmt_cl(v) if is_monetary else f"{v:,.0f}" for v in merged["ESC_1"]],
            textposition="outside", textfont=dict(size=10),
        ))
        fig.add_trace(go.Bar(
            x=merged["MES_LABEL"], y=merged["ESC_2"], name=label2,
            marker_color="#23CED3",
            text=[_fmt_cl(v) if is_monetary else f"{v:,.0f}" for v in merged["ESC_2"]],
            textposition="outside", textfont=dict(size=10),
        ))
        fig.update_layout(**dorel_layout(
            barmode="group",
            xaxis=dict(title=""),
            yaxis=dict(title=metric_label),
            legend=dict(orientation="h", y=1.12, x=0.5, xanchor="center"),
            height=380, margin=dict(t=50, b=40),
        ))
        st.plotly_chart(fig, use_container_width=True)

        # Detail table
        with st.expander(f"📋 Tabla detallada — {metric_label}", expanded=False):
            tbl = merged[["MES_LABEL", "ESC_1", "ESC_2", "DELTA", "DELTA_%"]].copy()
            tbl.columns = ["Mes", label1, label2, "Diferencia", "Δ %"]
            totals_row = pd.DataFrame([{
                "Mes": "TOTAL", label1: total1, label2: total2,
                "Diferencia": delta_total, "Δ %": delta_pct,
            }])
            tbl = pd.concat([tbl, totals_row], ignore_index=True)
            col_cfg = {}
            fmt_money = "$%,.0f" if is_monetary else "%,.0f"
            for c in [label1, label2, "Diferencia"]:
                col_cfg[c] = st.column_config.NumberColumn(format=fmt_money)
            col_cfg["Δ %"] = st.column_config.NumberColumn(format="%.1f%%")
            st.dataframe(tbl, use_container_width=True, hide_index=True, column_config=col_cfg)

        st.markdown("")


def _render_scenario_mode(conn):
    """Render the full scenario comparison mode with dual file uploaders."""

    st.html("<h2 class='sub-header'>Comparador de Escenarios</h2>")
    st.caption(
        "Carga dos versiones de Forecast y/o Plan de Compras para simular "
        "ambos escenarios y comparar métricas lado a lado."
    )

    # ── Escenario names ──────────────────────────────────────────────────
    _n1, _n2 = st.columns(2)
    with _n1:
        name_a = st.text_input("Nombre Escenario A", value="Escenario A", key="esc_name_a")
    with _n2:
        name_b = st.text_input("Nombre Escenario B", value="Escenario B", key="esc_name_b")

    # ── File uploaders side by side ──────────────────────────────────────
    st.markdown("---")
    col_a, col_sep, col_b = st.columns([5, 0.3, 5])

    with col_a:
        st.markdown(f"##### 📁 {name_a}")
        fa_fc = st.file_uploader("Forecast (Excel)", type=["xlsx"], key="esc_a_fc")
        fa_co = st.file_uploader("Plan Compra (CSV)", type=["csv"], key="esc_a_co")
        fa_pr = st.file_uploader("Precios (Excel, opcional)", type=["xlsx"], key="esc_a_pr")

    with col_sep:
        st.markdown("")  # visual separator

    with col_b:
        st.markdown(f"##### 📁 {name_b}")
        fb_fc = st.file_uploader("Forecast (Excel)", type=["xlsx"], key="esc_b_fc")
        fb_co = st.file_uploader("Plan Compra (CSV)", type=["csv"], key="esc_b_co")
        fb_pr = st.file_uploader("Precios (Excel, opcional)", type=["xlsx"], key="esc_b_pr")

    # ── Shared options ───────────────────────────────────────────────────
    st.markdown("")
    _opt1, _opt2 = st.columns(2)
    with _opt1:
        _shared_compra = st.checkbox(
            "Usar mismo Plan de Compra para ambos escenarios",
            value=False, key="esc_shared_compra",
            help="Si el cambio es solo en el Forecast, activa esto y solo carga 1 CSV.",
        )
    with _opt2:
        _shared_fc = st.checkbox(
            "Usar mismo Forecast para ambos escenarios",
            value=False, key="esc_shared_fc",
            help="Si el cambio es solo en el Plan de Compras, activa esto y solo carga 1 Forecast.",
        )

    # Resolve effective files
    eff_a_fc = fa_fc
    eff_a_co = fa_co
    eff_a_pr = fa_pr
    eff_b_fc = fb_fc if not _shared_fc else fa_fc
    eff_b_co = fb_co if not _shared_compra else fa_co
    eff_b_pr = fb_pr if fb_pr else fa_pr  # precios fallback

    ready_a = eff_a_fc is not None and eff_a_co is not None
    ready_b = eff_b_fc is not None and eff_b_co is not None

    if not ready_a or not ready_b:
        missing = []
        if not ready_a:
            missing.append(f"**{name_a}**: falta Forecast y/o Plan Compra")
        if not ready_b:
            missing.append(f"**{name_b}**: falta Forecast y/o Plan Compra")
        st.warning("Archivos pendientes:\n\n" + "\n\n".join(missing))
        return

    # ── Run both simulations ─────────────────────────────────────────────
    if st.button("🚀 Comparar Escenarios", type="primary", key="btn_run_comparison"):
        try:
            with st.spinner(f"Simulando {name_a}..."):
                result_a = process_projection_daily(
                    eff_a_fc, eff_a_co, eff_a_pr, conn, excluir_plan_compra=False,
                )
            if result_a is None:
                st.error(f"Error al procesar {name_a}.")
                return
            df_a, _ = result_a

            # Reset file positions for scenario B (in case shared files)
            for _f in [eff_b_fc, eff_b_co, eff_b_pr]:
                if _f is not None and hasattr(_f, "seek"):
                    _f.seek(0)

            with st.spinner(f"Simulando {name_b}..."):
                result_b = process_projection_daily(
                    eff_b_fc, eff_b_co, eff_b_pr, conn, excluir_plan_compra=False,
                )
            if result_b is None:
                st.error(f"Error al procesar {name_b}.")
                return
            df_b, _ = result_b

            st.session_state["esc_df_a"] = df_a
            st.session_state["esc_df_b"] = df_b
            st.session_state["esc_label_a"] = name_a
            st.session_state["esc_label_b"] = name_b
            st.session_state["esc_ready"] = True
            st.toast("Ambos escenarios simulados exitosamente")
            st.rerun()

        except Exception as e:
            st.error(f"Error: {e}")
            import traceback
            st.code(traceback.format_exc())

    # ── Show comparison results ──────────────────────────────────────────
    if st.session_state.get("esc_ready"):
        df_a = st.session_state["esc_df_a"]
        df_b = st.session_state["esc_df_b"]
        lbl_a = st.session_state.get("esc_label_a", "Escenario A")
        lbl_b = st.session_state.get("esc_label_b", "Escenario B")

        st.markdown("---")
        st.markdown(f"### Resultados: {lbl_a} vs {lbl_b}")

        # Quick summary KPIs
        _sa, _sb = st.columns(2)
        with _sa:
            _skus_a = df_a["SKU_PRODUCTO"].nunique() if "SKU_PRODUCTO" in df_a.columns else 0
            _periods_a = df_a["PERIODO"].nunique() if "PERIODO" in df_a.columns else 0
            st.info(f"**{lbl_a}**: {_skus_a:,} SKUs · {_periods_a} periodos · {len(df_a):,} filas")
        with _sb:
            _skus_b = df_b["SKU_PRODUCTO"].nunique() if "SKU_PRODUCTO" in df_b.columns else 0
            _periods_b = df_b["PERIODO"].nunique() if "PERIODO" in df_b.columns else 0
            st.info(f"**{lbl_b}**: {_skus_b:,} SKUs · {_periods_b} periodos · {len(df_b):,} filas")

        _render_comparison_results(df_a, df_b, lbl_a, lbl_b)

        # ── Download both scenarios ──────────────────────────────────
        st.markdown("---")
        st.markdown("### 📥 Descargar Escenarios")
        _d1, _d2 = st.columns(2)
        with _d1:
            buf_a = io.BytesIO()
            df_a.to_excel(buf_a, index=False, engine="openpyxl")
            st.download_button(
                f"📥 Descargar {lbl_a} (Excel)",
                data=buf_a.getvalue(),
                file_name=f"{lbl_a.replace(' ', '_')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.spreadsheet",
            )
        with _d2:
            buf_b = io.BytesIO()
            df_b.to_excel(buf_b, index=False, engine="openpyxl")
            st.download_button(
                f"📥 Descargar {lbl_b} (Excel)",
                data=buf_b.getvalue(),
                file_name=f"{lbl_b.replace(' ', '_')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.spreadsheet",
            )


# ============================================================================
# UI
# ============================================================================

def render_proyeccion(conn):
    st.html("<h2 class='sub-header'>Proyeccion de Stock</h2>")

    # ── Mode toggle: Proyección vs Comparador ─────────────────────────
    _mode = st.radio(
        "Modo",
        ["📊 Proyección", "🔄 Comparar Escenarios"],
        horizontal=True,
        key="proy_mode_toggle",
        label_visibility="collapsed",
    )

    if _mode == "🔄 Comparar Escenarios":
        _render_scenario_mode(conn)
        return

    # Tutorial download button
    import pathlib as _pathlib
    _tutorial_path = _pathlib.Path(__file__).resolve().parent.parent / "tutorial_simulacion_stock.pptx"
    if _tutorial_path.exists():
        with open(_tutorial_path, "rb") as _f:
            st.download_button(
                label="📖 Descargar Tutorial: Logica de Simulacion (PPT)",
                data=_f.read(),
                file_name="Simulacion_de_Stock_Logica_Dorel.pptx",
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                help="Presentacion con la logica completa del motor de simulacion de stock",
            )

    # ── Archivos guardados (persistencia local) ────────────────────────────
    _saved_fc_info = get_saved_file_info("forecast")
    _saved_co_info = get_saved_file_info("compra")
    _saved_pr_info = get_saved_file_info("precios")
    _any_saved = _saved_fc_info or _saved_co_info

    if _any_saved:
        with st.expander("📂 Archivos guardados disponibles", expanded=False):
            sc1, sc2, sc3 = st.columns(3)
            for _col_ref, _key, _label, _info in [
                (sc1, "forecast", "Forecast", _saved_fc_info),
                (sc2, "compra", "Plan Compra", _saved_co_info),
                (sc3, "precios", "Precios", _saved_pr_info),
            ]:
                with _col_ref:
                    if _info:
                        _sz = _info.get("size_bytes", 0)
                        _sz_str = f"{_sz / 1024:.0f} KB" if _sz > 0 else ""
                        st.markdown(
                            f"**{_label}**: `{_info['filename']}`\n\n"
                            f"Subido por: {_info.get('uploaded_by_name', '?')}\n\n"
                            f"Fecha: {_info.get('uploaded_at', '?')[:16].replace('T', ' ')}  "
                            f"({_sz_str})"
                        )
                    else:
                        st.markdown(f"**{_label}**: _No guardado_")

            _meta = load_metadata()
            if "last_shared" in _meta:
                _ls = _meta["last_shared"]
                st.caption(
                    f"Ultimo push al equipo: {_ls.get('shared_at', '?')[:16].replace('T', ' ')} "
                    f"por {_ls.get('shared_by_name', _ls.get('shared_by', '?'))} "
                    f"(commit {_ls.get('commit_hash', '?')})"
                )

        use_saved = st.checkbox(
            "Usar archivos guardados (sin subir nuevos)",
            value=True if has_saved_files() else False,
            key="use_saved_inputs",
        )
    else:
        use_saved = False

    # ── File uploaders (solo si no usa guardados) ─────────────────────────
    if not use_saved:
        col1, col2, col3 = st.columns(3)
        with col1:
            f_forecast = st.file_uploader("1. Cargar Forecast (Excel)", type=["xlsx"])
        with col2:
            f_compra = st.file_uploader("2. Cargar Proyeccion Compra (CSV)", type=["csv"])
        with col3:
            f_precios = st.file_uploader("3. Cargar Precios (Excel, Opcional)", type=["xlsx"])
    else:
        f_forecast = None
        f_compra = None
        f_precios = None

    # ── Resolver archivos efectivos ───────────────────────────────────────
    if use_saved and has_saved_files():
        eff_forecast = get_saved_file_path("forecast")
        eff_compra = get_saved_file_path("compra")
        eff_precios = get_saved_file_path("precios")  # puede ser None
        files_ready = True
    elif f_forecast and f_compra:
        eff_forecast = f_forecast
        eff_compra = f_compra
        eff_precios = f_precios
        files_ready = True
    else:
        eff_forecast = None
        eff_compra = None
        eff_precios = None
        files_ready = False

    _sim_col1, _sim_col2 = st.columns([2, 2])
    with _sim_col1:
        sim_mode = st.radio(
            "Modo de Simulacion",
            ["Diaria (recomendada)", "Mensual (legacy)"],
            horizontal=True,
            key="sim_mode",
            help="Diaria: simula día a día con llegadas COMEX/compras en fecha exacta y perfil diario. "
                 "Mensual: simulación mensual original.",
        )
    with _sim_col2:
        excluir_plan = st.checkbox(
            "Excluir Plan de Compras",
            value=False,
            key="excluir_plan_compra",
            help="Si se activa, la simulación NO considera las compras proyectadas del archivo CSV. "
                 "Solo usa el stock actual + COMEX confirmado (ETA). "
                 "Útil para simular escenario pesimista: ¿qué pasa si no se concretan las compras?",
        )

    if files_ready:
        if st.button("Generar Proyeccion", type="primary"):
            try:
                # Guardar archivos nuevos a disco para reutilización futura
                _current_user = get_current_user()
                if _current_user and not use_saved:
                    if f_forecast:
                        save_input_file("forecast", f_forecast, _current_user)
                    if f_compra:
                        save_input_file("compra", f_compra, _current_user)
                    if f_precios:
                        save_input_file("precios", f_precios, _current_user)

                if sim_mode == "Diaria (recomendada)":
                    result = process_projection_daily(eff_forecast, eff_compra, eff_precios, conn,
                                                      excluir_plan_compra=excluir_plan)
                    if result is not None:
                        df_proy_monthly, df_proy_daily = result
                        st.session_state["df_proy"] = df_proy_monthly
                        st.session_state["df_proy_daily"] = df_proy_daily
                        st.session_state["df_proy_ready"] = True
                        st.session_state["sim_mode_used"] = "diaria"
                        st.session_state.pop("proy_excel_buffer", None)
                        # Persistir resultado en disco (parquet)
                        save_projection_results(df_proy_monthly, df_proy_daily, sim_mode="diaria")
                        st.toast("Proyeccion Diaria Generada Exitosamente")
                        st.rerun()
                else:
                    df_proy = process_projection(eff_forecast, eff_compra, eff_precios, conn,
                                                    excluir_plan_compra=excluir_plan)
                    if df_proy is not None:
                        st.session_state["df_proy"] = df_proy
                        st.session_state.pop("df_proy_daily", None)
                        st.session_state["df_proy_ready"] = True
                        st.session_state["sim_mode_used"] = "mensual"
                        st.session_state.pop("proy_excel_buffer", None)
                        # Persistir resultado en disco (parquet)
                        save_projection_results(df_proy, sim_mode="mensual")
                        st.toast("Proyeccion Mensual Generada Exitosamente")
                        st.rerun()
            except Exception as e:
                st.error(f"Error en procesar: {e}")
                import traceback
                st.code(traceback.format_exc())
    else:
        st.warning("Debes cargar al menos Forecast y Proyeccion de Compra.")

    # ── Sincronizar datos para alertas email automáticas ────────────────
    if has_saved_files():
        st.divider()
        _acol1, _acol2 = st.columns([3, 1])
        with _acol1:
            st.markdown(
                "📧 **Sincronizar datos para alertas email** — "
                "Actualiza la proyección en el repositorio para que el email "
                "automático (Lun-Vie 8:30 AM) use los datos más recientes."
            )
        with _acol2:
            if st.button("🔄 Sincronizar", type="primary", key="git_push_inputs"):
                with st.spinner("Ejecutando git add/commit/push..."):
                    _ok, _msg = git_share_inputs()
                if _ok:
                    st.success(f"✅ {_msg} El próximo email usará estos datos.")
                else:
                    st.error(_msg)

    # --- Display results from session_state (persists across widget interactions) ---
    if st.session_state.get("df_proy_ready") and "df_proy" in st.session_state:
        df_proy = st.session_state["df_proy"]
        df_proy = apply_pm_filter(df_proy)

        # Executive summary — all forecast years
        st.markdown("### Resumen de Resultados")
        if "VN_RES_TOTAL" in df_proy.columns and "PERIODO_ANO" in df_proy.columns:

            # Controls: canal + excluir sin costo + product filters
            _rc1, _rc2, _rc3 = st.columns([1.5, 1.5, 3])
            with _rc1:
                _canal_res_opts = ["Todos", "Tienda", "Etail", "Mayor"]
                sel_canal_res = st.selectbox("Canal", _canal_res_opts, key="resumen_canal")
            with _rc2:
                excluir_sin_costo_res = st.checkbox("Excluir SKUs sin costo", value=False, key="resumen_excluir_sc")

            # ── Product dimension filters ──
            with st.expander("Filtros de Producto", expanded=False):
                _pf1, _pf2, _pf3, _pf4 = st.columns(4)
                _areas_r = sorted(df_proy["AREA"].dropna().unique().tolist()) if "AREA" in df_proy.columns else []
                sel_area_r = _pf1.multiselect("Area", _areas_r, key="resumen_area")

                # Cascade: filter línea options by selected area
                _df_linea_src = df_proy if not sel_area_r else df_proy[df_proy["AREA"].isin(sel_area_r)]
                _lineas_r = sorted(_df_linea_src["LINEA"].dropna().unique().tolist()) if "LINEA" in _df_linea_src.columns else []
                sel_linea_r = _pf2.multiselect("Linea", _lineas_r, key="resumen_linea")

                # Cascade: filter sublínea by area+línea
                _df_sub_src = _df_linea_src if not sel_linea_r else _df_linea_src[_df_linea_src["LINEA"].isin(sel_linea_r)]
                _sublins_r = sorted(_df_sub_src["SUBLINEA"].dropna().unique().tolist()) if "SUBLINEA" in _df_sub_src.columns else []
                sel_sublinea_r = _pf3.multiselect("Sublinea", _sublins_r, key="resumen_sublinea")

                _marcas_r = sorted(df_proy["MARCA"].dropna().unique().tolist()) if "MARCA" in df_proy.columns else []
                sel_marca_r = _pf4.multiselect("Marca", _marcas_r, key="resumen_marca")

                _pf5, _pf6, _pf7, _pf8 = st.columns(4)
                _modelos_r = sorted(df_proy["MODELO"].dropna().unique().tolist()) if "MODELO" in df_proy.columns else []
                sel_modelo_r = _pf5.multiselect("Modelo", _modelos_r, key="resumen_modelo")

                _provs_r = sorted(df_proy["PROVEEDOR"].dropna().unique().tolist()) if "PROVEEDOR" in df_proy.columns else []
                sel_prov_r = _pf6.multiselect("Proveedor", _provs_r, key="resumen_proveedor")

                sel_sku_r = _pf7.text_input("SKU (contiene)", key="resumen_sku", placeholder="ej: 013910")
                sel_nombre_r = _pf8.text_input("Producto (contiene)", key="resumen_nombre", placeholder="ej: Coche")

            # Map channel → columns
            _canal_res_map = {
                "Todos": {"vn": "VN_RES_TOTAL", "cogs": "COGS_RES_TOTAL", "aporte": "APORTE_RES_TOTAL"},
                "Tienda": {"vn": "VN_RES_TIENDA", "cogs": "COGS_RES_TIENDA", "aporte": "APORTE_RES_TIENDA"},
                "Etail": {"vn": "VN_RES_ETAIL", "cogs": "COGS_RES_ETAIL", "aporte": "APORTE_RES_ETAIL"},
                "Mayor": {"vn": "VN_RES_MAYOR", "cogs": "COGS_RES_MAYOR", "aporte": "APORTE_RES_MAYOR"},
            }
            _res_cols = _canal_res_map.get(sel_canal_res, _canal_res_map["Todos"])
            _canal_lbl_res = f" — {sel_canal_res}" if sel_canal_res != "Todos" else ""

            # Apply filters
            df_kpi = df_proy.copy()
            if excluir_sin_costo_res and "FLAG_SIN_COSTO" in df_kpi.columns:
                df_kpi = df_kpi[~df_kpi["FLAG_SIN_COSTO"].astype(bool)]
            if sel_area_r and "AREA" in df_kpi.columns:
                df_kpi = df_kpi[df_kpi["AREA"].isin(sel_area_r)]
            if sel_linea_r and "LINEA" in df_kpi.columns:
                df_kpi = df_kpi[df_kpi["LINEA"].isin(sel_linea_r)]
            if sel_sublinea_r and "SUBLINEA" in df_kpi.columns:
                df_kpi = df_kpi[df_kpi["SUBLINEA"].isin(sel_sublinea_r)]
            if sel_marca_r and "MARCA" in df_kpi.columns:
                df_kpi = df_kpi[df_kpi["MARCA"].isin(sel_marca_r)]
            if sel_modelo_r and "MODELO" in df_kpi.columns:
                df_kpi = df_kpi[df_kpi["MODELO"].isin(sel_modelo_r)]
            if sel_prov_r and "PROVEEDOR" in df_kpi.columns:
                df_kpi = df_kpi[df_kpi["PROVEEDOR"].isin(sel_prov_r)]
            if sel_sku_r and "SKU_PRODUCTO" in df_kpi.columns:
                df_kpi = df_kpi[df_kpi["SKU_PRODUCTO"].str.contains(sel_sku_r.strip().upper(), case=False, na=False)]
            if sel_nombre_r and "SKU_NOM_PRODUCTO" in df_kpi.columns:
                df_kpi = df_kpi[df_kpi["SKU_NOM_PRODUCTO"].str.contains(sel_nombre_r.strip(), case=False, na=False)]

            current_year = pd.Timestamp.now().year
            valid_years = sorted(
                y for y in df_kpi["PERIODO_ANO"].unique()
                if y >= current_year
            )

            for year in valid_years:
                df_year = df_kpi[df_kpi["PERIODO_ANO"] == year]
                if df_year.empty:
                    continue
                _vn_c = _res_cols["vn"]
                _cogs_c = _res_cols["cogs"]
                _ap_c = _res_cols["aporte"]
                vn = df_year[_vn_c].sum() if _vn_c in df_year.columns else 0
                cogs = df_year[_cogs_c].sum() if _cogs_c in df_year.columns else 0
                aporte = df_year[_ap_c].sum() if _ap_c in df_year.columns else vn - cogs
                margen = (aporte / vn * 100) if vn > 0 else 0

                c1, c2, c3, c4 = st.columns(4)
                c1.metric(f"Venta Neta {year}{_canal_lbl_res}", _fmt_cl(vn))
                c2.metric("Costo Venta", _fmt_cl(cogs))
                c3.metric("Aporte $", _fmt_cl(aporte))
                c4.metric("Margen %", f"{margen:.1f}%")

            # Show zero-cost SKU expander
            if "FLAG_SIN_COSTO" in df_proy.columns:
                df_sin_costo = df_proy[df_proy["FLAG_SIN_COSTO"].astype(bool)].copy()
                n_skus_sc = df_sin_costo["SKU_PRODUCTO"].nunique()
                if n_skus_sc > 0:
                    vn_contaminado = df_sin_costo["VN_RES_TOTAL"].sum()
                    with st.expander(f"⚠️ SKUs sin costo ({n_skus_sc} SKUs · VN = {_fmt_cl(vn_contaminado)})"):
                        st.caption(
                            "Estos SKUs tienen COSTO_UNITARIO = 0 y generan margen artificialmente alto (100%). "
                            "Usa el toggle 'Excluir SKUs sin costo' para ver métricas limpias."
                        )
                        sc_summary = (
                            df_sin_costo.groupby("SKU_PRODUCTO", as_index=False)
                            .agg(
                                VN_TOTAL=("VN_RES_TOTAL", "sum"),
                                ORIGEN_COSTO=("ORIGEN_COSTO", "first"),
                            )
                            .sort_values("VN_TOTAL", ascending=False)
                        )
                        if "SKU_NOM_PRODUCTO" in df_proy.columns:
                            nom_map = df_proy[["SKU_PRODUCTO", "SKU_NOM_PRODUCTO"]].drop_duplicates("SKU_PRODUCTO")
                            sc_summary = sc_summary.merge(nom_map, on="SKU_PRODUCTO", how="left")
                        sc_summary["VN_TOTAL"] = sc_summary["VN_TOTAL"].apply(_fmt_cl)
                        st.dataframe(sc_summary, use_container_width=True, hide_index=True)

        # YoY Dashboard
        _render_yoy_dashboard(df_proy)

        # PPT Tables — next 3 months vs AA
        _render_ppt_tables(df_proy)

        # Instock Dashboard — with ABC-XYZ on-demand
        _render_instock_dashboard(df_proy, conn)

        st.markdown("### Detalle (Primeras 50 filas)")
        st.dataframe(df_proy.head(50), use_container_width=True)

        # Download with README + Summary sheets (cached to avoid rebuild on rerun)
        if "proy_excel_buffer" not in st.session_state:
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df_proy.to_excel(writer, sheet_name="DATA", index=False)

                # Summary sheets (Resumen SKU, Resumen Area-Linea, Pivot VN)
                _write_summary_sheets(writer, df_proy)

                wb = writer.book

                # README sheet — move to first position
                ws = wb.create_sheet("README", 0)
                for row_idx, row_data in enumerate(README_DATA, start=1):
                    for col_idx, value in enumerate(row_data, start=1):
                        cell = ws.cell(row=row_idx, column=col_idx, value=value)
                        if row_idx == 1:
                            cell.font = Font(color="FFFFFF", bold=True)
                            cell.fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")

            st.session_state["proy_excel_buffer"] = buf.getvalue()

        buffer = io.BytesIO(st.session_state["proy_excel_buffer"])
        download_buttons(df_proy, "proyeccion_stock_simulada", excel_buffer=buffer)
