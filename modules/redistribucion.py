"""Redistribucion de Stock — Sugiere mover stock inmovilizado entre tiendas.

Identifica SKUs con baja rotacion en una tienda y sugiere enviarlos a otra
tienda que los vende mas rapido, o devolver al CD para ecommerce/mayorista.
Considera transito existente, perfiles, y georreferencia.
"""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from db.cache import cached_query as cq
from utils.filters import norm_cols, human_format, fmt_clp
from utils.export import download_buttons
from config import COLORS, dorel_layout, apply_pm_filter
from data.tarifa_transporte import (
    get_rate_per_kg, get_store_base, route_cost_per_kg, CD_BASE,
)

# ============================================================================
# CONSTANTS
# ============================================================================

_DEFAULT_MOI_THRESHOLD = 6  # meses
_WEIGHT_VELOCITY = 0.50
_WEIGHT_NEED = 0.30
_WEIGHT_COST_EFF = 0.20  # cost efficiency (replaces raw distance)
_MAX_SUGGESTIONS_PER_SKU = 5  # top N receptores por SKU inmovilizado

# Fallback geo for stores missing coordinates in dv_tienda
_GEO_FALLBACK = {
    "1020054": (-42.4700, -73.7700),  # BIS CHILOE
    "1020059": (-23.6500, -70.4000),  # BIS ESPACIO URBANO ANTOFAGASTA
    "1020056": (-32.8800, -71.2500),  # BIS QUILLOTA
    "1020044": (-34.1700, -70.7400),  # BIS RANCAGUA CENTRO
    "1120001": (-33.4000, -70.5700),  # MAXI COSI KENNEDY
    "1040019": (-33.5000, -70.6300),  # OUTLET BIS LA FABRICA
    "1040022": (-33.0100, -71.5500),  # OUTLET PARK VIÑA
}

_MOI_COLORS = {
    "SIN_VENTA": COLORS["status_critical"],
    "12m+": COLORS["status_critical"],
    "6-12m": COLORS["status_at_risk"],
    "3-6m": COLORS["status_en_curso"],
    "0-3m": COLORS["status_on_track"],
}


def _fmt_mm(val):
    """Format large CLP values as $X.XXX M."""
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "$0"
    if pd.isna(val) or val == 0:
        return "$0"
    neg = val < 0
    abs_val = abs(val)
    if abs_val >= 1_000_000_000:
        txt = f"${abs_val / 1_000_000_000:,.1f}B"
    elif abs_val >= 1_000_000:
        txt = f"${abs_val / 1_000_000:,.1f}M"
    else:
        txt = f"${abs_val:,.0f}"
    return f"-{txt}" if neg else txt


def _haversine_km(lat1, lon1, lat2, lon2):
    """Vectorized haversine distance in km. Works with numpy arrays."""
    R = 6371.0
    rlat1, rlat2 = np.radians(lat1), np.radians(lat2)
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(rlat1) * np.cos(rlat2) * np.sin(dlon / 2) ** 2
    return R * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


# ============================================================================
# DATA LOADING
# ============================================================================

def _load_redistribucion_data(conn):
    """Load all data sources for redistribution analysis.

    Returns dict with keys: stock, ventas_90d, config, transito, tiendas,
    maestra, abc, stock_cd.
    """
    with st.spinner("Cargando datos para redistribucion..."):
        stock = cq.stock_higiene(conn)
        ventas_90d = cq.ventas_90d_sucursal(conn)
        config = cq.syncro_config(conn)
        tiendas = cq.tienda_dim(conn)
        maestra = cq.maestra(conn)
        abc = cq.abc_xyz_fsn(conn)
        stock_cd = cq.stock_onhand(conn)

    # Unified transit: COO (yesterday backward) + today's pedidos
    try:
        transito = cq.unified_transit(conn)
    except Exception:
        transito = pd.DataFrame()

    return {
        "stock": stock,
        "ventas_90d": ventas_90d,
        "config": config,
        "transito": transito,
        "tiendas": tiendas,
        "maestra": maestra,
        "abc": abc,
        "stock_cd": stock_cd,
    }


# ============================================================================
# TRANSIT TABLE — defensive column mapping
# ============================================================================

def _parse_transito(df_raw):
    """Auto-detect columns in transit table and return standardized DataFrame.

    Returns DataFrame with columns: SKU_PRODUCTO, ID_SUCURSAL_DESTINO, QTY_TRANSITO
    or empty DataFrame if mapping fails.
    """
    if df_raw is None or df_raw.empty:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO", "QTY_TRANSITO"])

    df = df_raw.copy()
    df.columns = [c.upper().strip() for c in df.columns]
    cols = df.columns.tolist()

    # --- SKU column ---
    sku_col = None
    for candidate in ["ID_MATERIAL", "SKU_PRODUCTO", "SKU", "MATERIAL", "COD_PRODUCTO"]:
        if candidate in cols:
            sku_col = candidate
            break
    if sku_col is None:
        for c in cols:
            if "MATERIAL" in c or "SKU" in c or "PRODUCTO" in c:
                sku_col = c
                break

    # --- Destination column ---
    dest_col = None
    for candidate in ["ID_SUCURSAL_DESTINO", "SUCURSAL_DESTINO", "ID_SUCURSAL",
                       "DESTINO", "COD_DESTINO", "BODEGA_DESTINO"]:
        if candidate in cols:
            dest_col = candidate
            break
    if dest_col is None:
        for c in cols:
            if "DESTINO" in c:
                dest_col = c
                break

    # --- Quantity column ---
    qty_col = None
    for candidate in ["CANTIDAD", "QTY", "UNIDADES", "CANTIDAD_TRANSITO",
                       "STOCK_TRANSITO", "QTY_TRANSITO"]:
        if candidate in cols:
            qty_col = candidate
            break
    if qty_col is None:
        for c in cols:
            if "CANT" in c or "QTY" in c or "UNID" in c:
                qty_col = c
                break

    if sku_col is None or dest_col is None:
        return pd.DataFrame(columns=["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO", "QTY_TRANSITO"])

    result = pd.DataFrame({
        "SKU_PRODUCTO": df[sku_col].astype(str).str.strip(),
        "ID_SUCURSAL_DESTINO": df[dest_col].astype(str).str.strip(),
    })
    if qty_col is not None:
        result["QTY_TRANSITO"] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0)
    else:
        result["QTY_TRANSITO"] = 1  # at least flag that transit exists

    # Aggregate in case of duplicates
    result = result.groupby(["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO"], as_index=False).agg(
        QTY_TRANSITO=("QTY_TRANSITO", "sum"),
    )
    return result


def _get_transito_info(df_raw):
    """Return parsed transit + diagnostic info."""
    parsed = _parse_transito(df_raw)
    info = {}
    if df_raw is not None and not df_raw.empty:
        info["raw_cols"] = list(df_raw.columns)
        info["raw_rows"] = len(df_raw)
    else:
        info["raw_cols"] = []
        info["raw_rows"] = 0
    info["mapped_rows"] = len(parsed)
    info["mapping_ok"] = len(parsed) > 0 or info["raw_rows"] == 0
    return parsed, info


# ============================================================================
# BUILD BASE TABLE — SKU × Tienda enriched
# ============================================================================

def _build_base_table(data):
    """Build the base SKU × Store table with velocity, MOI, profiles."""
    stock = data["stock"].copy() if data["stock"] is not None else pd.DataFrame()
    v90 = data["ventas_90d"].copy() if data["ventas_90d"] is not None else pd.DataFrame()
    cfg = data["config"].copy() if data["config"] is not None else pd.DataFrame()
    tiendas = data["tiendas"].copy() if data["tiendas"] is not None else pd.DataFrame()
    maestra = data["maestra"].copy() if data["maestra"] is not None else pd.DataFrame()
    abc = data["abc"].copy() if data["abc"] is not None else pd.DataFrame()

    if stock.empty:
        return pd.DataFrame()

    # Normalize all column names
    for df in [stock, v90, cfg, tiendas, maestra, abc]:
        df.columns = [c.upper().strip() for c in df.columns]

    # --- Stock base: only TIENDA channel ---
    if "CANAL_DE_DISTRIBUCION" in stock.columns:
        stock = stock[stock["CANAL_DE_DISTRIBUCION"].str.upper() == "TIENDA"].copy()

    for c in ["STOCK_UNIDADES", "PERFIL_TIENDAS"]:
        if c in stock.columns:
            stock[c] = pd.to_numeric(stock[c], errors="coerce").fillna(0)

    stock = stock[stock["STOCK_UNIDADES"] > 0].copy()
    if stock.empty:
        return pd.DataFrame()

    # Ensure ID_SUCURSAL is string for joins
    if "ID_SUCURSAL" in stock.columns:
        stock["ID_SUCURSAL"] = stock["ID_SUCURSAL"].astype(str).str.strip()

    # --- Merge ventas 90d ---
    if not v90.empty:
        if "ID_SUCURSAL" in v90.columns:
            v90["ID_SUCURSAL"] = v90["ID_SUCURSAL"].astype(str).str.strip()
        for c in ["UNIDADES_90D", "NETO_90D", "DIAS_CON_VENTA"]:
            if c in v90.columns:
                v90[c] = pd.to_numeric(v90[c], errors="coerce").fillna(0)
        v90_agg = v90.groupby(["SKU_PRODUCTO", "ID_SUCURSAL"], as_index=False).agg(
            UNIDADES_90D=("UNIDADES_90D", "sum"),
            NETO_90D=("NETO_90D", "sum"),
            DIAS_CON_VENTA=("DIAS_CON_VENTA", "max"),
        )
        stock = stock.merge(v90_agg, on=["SKU_PRODUCTO", "ID_SUCURSAL"], how="left")
    for c in ["UNIDADES_90D", "NETO_90D", "DIAS_CON_VENTA"]:
        if c not in stock.columns:
            stock[c] = 0
        stock[c] = stock[c].fillna(0)

    # Velocity metrics
    stock["VTA_MENSUAL_PROM"] = stock["UNIDADES_90D"] / 3.0
    stock["VTA_DIARIA_PROM"] = stock["UNIDADES_90D"] / 90.0
    stock["MOI_TIENDA"] = np.where(
        stock["VTA_MENSUAL_PROM"] > 0,
        stock["STOCK_UNIDADES"] / stock["VTA_MENSUAL_PROM"],
        np.inf,
    )

    # --- Merge config (MIN_INV_REQUERIDO, MAX_REPO) ---
    if not cfg.empty:
        if "ID_MATERIAL" in cfg.columns and "SKU_PRODUCTO" not in cfg.columns:
            cfg = cfg.rename(columns={"ID_MATERIAL": "SKU_PRODUCTO"})
        if "ID_SUCURSAL" in cfg.columns:
            cfg["ID_SUCURSAL"] = cfg["ID_SUCURSAL"].astype(str).str.strip()
        for c in ["MIN_INV_REQUERIDO", "MAX_REPO"]:
            if c in cfg.columns:
                cfg[c] = pd.to_numeric(cfg[c], errors="coerce").fillna(0)
        cfg_dedup = cfg.drop_duplicates(["SKU_PRODUCTO", "ID_SUCURSAL"], keep="first")
        merge_cols = [c for c in ["SKU_PRODUCTO", "ID_SUCURSAL", "MIN_INV_REQUERIDO", "MAX_REPO"]
                      if c in cfg_dedup.columns]
        if len(merge_cols) >= 3:
            stock = stock.merge(cfg_dedup[merge_cols], on=["SKU_PRODUCTO", "ID_SUCURSAL"], how="left")
    for c in ["MIN_INV_REQUERIDO", "MAX_REPO"]:
        if c not in stock.columns:
            stock[c] = 0
        stock[c] = stock[c].fillna(0)

    # Excess = transferable units
    stock["EXCESO_UND"] = (stock["STOCK_UNIDADES"] - stock["MIN_INV_REQUERIDO"]).clip(lower=0)

    # --- Merge tienda geo ---
    if not tiendas.empty and "ID_SUCURSAL" in tiendas.columns:
        tiendas["ID_SUCURSAL"] = tiendas["ID_SUCURSAL"].astype(str).str.strip()
        geo_cols = [c for c in ["ID_SUCURSAL", "LATITUD", "LONGITUD", "CLUSTER",
                                "CIUDAD", "COMUNA", "ZONA", "MTS2", "ACTIVA",
                                "DESCRIPCION_SUCURSAL"] if c in tiendas.columns]
        # Avoid duplicate DESCRIPCION_SUCURSAL
        if "DESCRIPCION_SUCURSAL" in stock.columns and "DESCRIPCION_SUCURSAL" in geo_cols:
            geo_cols.remove("DESCRIPCION_SUCURSAL")
        tiendas_dedup = tiendas.drop_duplicates("ID_SUCURSAL", keep="first")
        stock = stock.merge(tiendas_dedup[geo_cols], on="ID_SUCURSAL", how="left")
    for c in ["LATITUD", "LONGITUD", "MTS2"]:
        if c in stock.columns:
            stock[c] = pd.to_numeric(stock[c], errors="coerce").fillna(0)

    # Apply hardcoded geo fallback for stores missing coordinates
    if "LATITUD" in stock.columns and "LONGITUD" in stock.columns:
        for sid, (lat, lon) in _GEO_FALLBACK.items():
            mask = (stock["ID_SUCURSAL"] == sid) & (stock["LATITUD"] == 0)
            stock.loc[mask, "LATITUD"] = lat
            stock.loc[mask, "LONGITUD"] = lon

    if "ACTIVA" not in stock.columns:
        stock["ACTIVA"] = True

    # --- Merge maestra dims ---
    if not maestra.empty:
        m_dedup = maestra.drop_duplicates("SKU_PRODUCTO", keep="first")
        for col in ["AREA", "LINEA", "SUBLINEA", "MARCA", "NOM_PRODUCTO",
                     "ULTIMO_COSTO", "PROCEDENCIA", "PESO_BRUTO_UNIDAD"]:
            if col in m_dedup.columns and col not in stock.columns:
                stock = stock.merge(m_dedup[["SKU_PRODUCTO", col]], on="SKU_PRODUCTO", how="left")
    if "ULTIMO_COSTO" in stock.columns:
        stock["ULTIMO_COSTO"] = pd.to_numeric(stock["ULTIMO_COSTO"], errors="coerce").fillna(0)
    else:
        stock["ULTIMO_COSTO"] = 0
    stock["VALOR_INMOVILIZADO"] = stock["EXCESO_UND"] * stock["ULTIMO_COSTO"]

    # --- Parse product weight (VARCHAR in Snowflake) ---
    if "PESO_BRUTO_UNIDAD" in stock.columns:
        stock["PESO_BRUTO_UNIDAD"] = pd.to_numeric(
            stock["PESO_BRUTO_UNIDAD"].astype(str).str.replace(",", "."),
            errors="coerce",
        ).fillna(0)
    else:
        stock["PESO_BRUTO_UNIDAD"] = 0.0

    # --- Resolve tariff base per store ---
    _comuna_col = "COMUNA" if "COMUNA" in stock.columns else None
    _ciudad_col = "CIUDAD" if "CIUDAD" in stock.columns else None

    def _resolve_base(row):
        comuna = str(row[_comuna_col]) if _comuna_col else ""
        ciudad = str(row[_ciudad_col]) if _ciudad_col else ""
        return get_store_base(comuna, ciudad)

    if _comuna_col or _ciudad_col:
        _base_info = stock.apply(_resolve_base, axis=1)
        stock["TARIFA_BASE"] = _base_info.apply(lambda x: x.get("base", ""))
        stock["TARIFA_RADIO"] = _base_info.apply(lambda x: x.get("radio", "BASE"))
    else:
        stock["TARIFA_BASE"] = ""
        stock["TARIFA_RADIO"] = "BASE"

    # --- Merge ABC ---
    if not abc.empty:
        abc_cols = [c for c in ["SKU_PRODUCTO", "CLASE_ABC", "CLASE_XYZ"] if c in abc.columns]
        if len(abc_cols) >= 2:
            abc_dedup = abc[abc_cols].drop_duplicates("SKU_PRODUCTO", keep="first")
            stock = stock.merge(abc_dedup, on="SKU_PRODUCTO", how="left")
    if "CLASE_ABC" not in stock.columns:
        stock["CLASE_ABC"] = "C"
    if "CLASE_XYZ" not in stock.columns:
        stock["CLASE_XYZ"] = "Z"
    stock["CLASE_ABC"] = stock["CLASE_ABC"].fillna("C")
    stock["CLASE_XYZ"] = stock["CLASE_XYZ"].fillna("Z")

    # --- MOI bucket for coloring ---
    conditions = [
        stock["VTA_MENSUAL_PROM"] == 0,
        stock["MOI_TIENDA"] >= 12,
        stock["MOI_TIENDA"] >= 6,
        stock["MOI_TIENDA"] >= 3,
    ]
    choices = ["SIN_VENTA", "12m+", "6-12m", "3-6m"]
    stock["MOI_BUCKET"] = np.select(conditions, choices, default="0-3m")

    return stock


# ============================================================================
# IDENTIFY IMMOBILIZED STOCK
# ============================================================================

def _filter_immobilized(df_base, moi_threshold):
    """Filter to only immobilized stock: MOI > threshold or 0 sales."""
    if df_base.empty:
        return pd.DataFrame()
    mask = (
        (df_base["VTA_MENSUAL_PROM"] == 0) |
        (df_base["MOI_TIENDA"] >= moi_threshold)
    ) & (df_base["EXCESO_UND"] > 0)
    return df_base[mask].copy()


# ============================================================================
# GENERATE REDISTRIBUTION SUGGESTIONS
# ============================================================================

def _generate_suggestions(df_base, df_immob, transito_map, data, moi_threshold):
    """Generate redistribution suggestions.

    Returns DataFrame with one row per suggestion (origin→dest per SKU).
    transito_map: dict of (SKU, DEST) → QTY_TRANSITO for space adjustment.
    """
    if df_immob.empty or df_base.empty:
        return pd.DataFrame()

    # Potential receivers: tiendas that sell, are active, have space
    receivers = df_base[
        (df_base["VTA_MENSUAL_PROM"] > 0) &
        (df_base["ACTIVA"] == True) &
        (df_base["STOCK_UNIDADES"] < df_base["MAX_REPO"].clip(lower=1e9))
    ].copy()

    # Also include receivers where MAX_REPO=0 but they have velocity
    if "MAX_REPO" in receivers.columns:
        extra = df_base[
            (df_base["VTA_MENSUAL_PROM"] > 0) &
            (df_base["ACTIVA"] == True) &
            (df_base["MAX_REPO"] == 0)
        ]
        receivers = pd.concat([receivers, extra]).drop_duplicates(
            ["SKU_PRODUCTO", "ID_SUCURSAL"], keep="first"
        )

    if receivers.empty:
        return pd.DataFrame()

    # Max velocity per SKU (for normalization)
    max_vel = receivers.groupby("SKU_PRODUCTO")["VTA_MENSUAL_PROM"].max().to_dict()

    # CD stock for "devolver a CD" option
    stock_cd = data.get("stock_cd")
    cd_stock_map = {}
    if stock_cd is not None and not stock_cd.empty:
        scd = stock_cd.copy()
        scd.columns = [c.upper().strip() for c in scd.columns]
        if "SKU_PRODUCTO" in scd.columns:
            for c in ["STOCK_CD_UND", "STOCK_UNIDADES"]:
                if c in scd.columns:
                    scd[c] = pd.to_numeric(scd[c], errors="coerce").fillna(0)
                    cd_stock_map = scd.set_index("SKU_PRODUCTO")[c].to_dict()
                    break

    # CD velocity (ETAIL + MAYOR) from ventas_90d
    v90 = data.get("ventas_90d")
    cd_vel_map = {}
    if v90 is not None and not v90.empty:
        v = v90.copy()
        v.columns = [c.upper().strip() for c in v.columns]
        if "CANAL_DE_DISTRIBUCION" in v.columns and "UNIDADES_90D" in v.columns:
            v["UNIDADES_90D"] = pd.to_numeric(v["UNIDADES_90D"], errors="coerce").fillna(0)
            cd_sales = v[v["CANAL_DE_DISTRIBUCION"].isin(["ETAIL", "MAYORISTA"])]
            if not cd_sales.empty:
                cd_agg = cd_sales.groupby("SKU_PRODUCTO")["UNIDADES_90D"].sum()
                cd_vel_map = (cd_agg / 3.0).to_dict()  # monthly avg

    suggestions = []
    immob_skus = df_immob["SKU_PRODUCTO"].unique()

    for sku in immob_skus:
        origins = df_immob[df_immob["SKU_PRODUCTO"] == sku]
        sku_receivers = receivers[receivers["SKU_PRODUCTO"] == sku]
        sku_max_vel = max_vel.get(sku, 1)

        for _, orig in origins.iterrows():
            remaining = orig["EXCESO_UND"]
            if remaining <= 0:
                continue

            orig_lat = orig.get("LATITUD", 0)
            orig_lon = orig.get("LONGITUD", 0)
            orig_id = str(orig["ID_SUCURSAL"])
            orig_name = orig.get("DESCRIPCION_SUCURSAL", orig_id)

            # Score all receivers for this origin
            scored = []
            for _, recv in sku_receivers.iterrows():
                recv_id = str(recv["ID_SUCURSAL"])
                if recv_id == orig_id:
                    continue

                # Adjust space for transit already in route
                transit_incoming = transito_map.get((sku, recv_id), 0)

                vel = recv["VTA_MENSUAL_PROM"]
                vel_score = vel / sku_max_vel if sku_max_vel > 0 else 0

                max_repo = recv.get("MAX_REPO", 0)
                current = recv.get("STOCK_UNIDADES", 0)
                if max_repo > 0:
                    space = max(0, max_repo - current - transit_incoming)
                    need_score = space / max_repo
                else:
                    space = max(0, vel * 3 - current - transit_incoming)  # 3 months of demand as proxy
                    need_score = 0.5 if space > 0 else 0

                if space <= 0:
                    continue  # No net space after transit

                recv_lat = recv.get("LATITUD", 0)
                recv_lon = recv.get("LONGITUD", 0)
                if orig_lat != 0 and orig_lon != 0 and recv_lat != 0 and recv_lon != 0:
                    dist = float(_haversine_km(orig_lat, orig_lon, recv_lat, recv_lon))
                else:
                    dist = 999

                # --- Transport cost for this route ---
                orig_base = orig.get("TARIFA_BASE", "")
                recv_base = recv.get("TARIFA_BASE", "")
                recv_radio = recv.get("TARIFA_RADIO", "BASE")
                peso_unit = orig.get("PESO_BRUTO_UNIDAD", 0)

                cpkg = route_cost_per_kg(orig_base, recv_base, recv_radio)
                cost_unit = cpkg * peso_unit if peso_unit > 0 else cpkg

                scored.append({
                    "recv_id": recv_id,
                    "recv_name": recv.get("DESCRIPCION_SUCURSAL", recv_id),
                    "recv_ciudad": recv.get("CIUDAD", ""),
                    "vel": vel,
                    "vel_score": vel_score,
                    "need_score": need_score,
                    "space": space,
                    "dist": dist,
                    "recv_stock": current,
                    "recv_lat": recv_lat,
                    "recv_lon": recv_lon,
                    "cost_per_kg": cpkg,
                    "cost_per_unit": cost_unit,
                    "recv_base": recv_base,
                    "recv_radio": recv_radio,
                })

            # Helper to build a DEVOLVER_CD suggestion dict
            def _cd_suggestion(qty_cd, orig_row, o_name, o_id, o_lat, o_lon,
                               cd_vel, cd_stock):
                dias_cd = qty_cd / (cd_vel / 30.0) if cd_vel > 0 else 999
                o_base = orig_row.get("TARIFA_BASE", "")
                peso = orig_row.get("PESO_BRUTO_UNIDAD", 0)
                cd_cpkg = route_cost_per_kg(o_base, CD_BASE, "BASE")
                cd_cost_unit = cd_cpkg * peso if peso > 0 else cd_cpkg
                return {
                    "SKU_PRODUCTO": sku,
                    "TIENDA_ORIGEN": o_name,
                    "ID_ORIGEN": o_id,
                    "TIENDA_DESTINO": "CD (Ecommerce/Mayor)",
                    "ID_DESTINO": "CD",
                    "QTY_SUGERIDA": int(qty_cd),
                    "VALOR_CLP": qty_cd * orig_row.get("ULTIMO_COSTO", 0),
                    "DIAS_VENTA_EST": round(dias_cd, 0),
                    "DISTANCIA_KM": 0,
                    "SCORE": 0.3,
                    "TIPO": "DEVOLVER_CD",
                    "VEL_DESTINO": cd_vel,
                    "STOCK_DESTINO": cd_stock,
                    "LAT_ORIGEN": o_lat,
                    "LON_ORIGEN": o_lon,
                    "LAT_DESTINO": 0,
                    "LON_DESTINO": 0,
                    "COSTO_POR_KG": round(cd_cpkg, 0),
                    "COSTO_TRANSPORTE_EST": round(cd_cost_unit * qty_cd, 0),
                    "PESO_COBRABLE_KG": round(peso, 2),
                    "BASE_DESTINO": CD_BASE,
                }

            if not scored:
                # Check CD return option
                cd_stock_val = cd_stock_map.get(sku, 0)
                cd_vel_val = cd_vel_map.get(sku, 0)
                if cd_stock_val == 0 and cd_vel_val > 0:
                    suggestions.append(_cd_suggestion(
                        remaining, orig, orig_name, orig_id, orig_lat, orig_lon,
                        cd_vel_val, cd_stock_val,
                    ))
                continue

            # Normalize cost for scoring (cheaper = higher cost_eff)
            max_cost = max((s["cost_per_kg"] for s in scored), default=1)
            max_cost = max(max_cost, 1)
            max_dist = max((s["dist"] for s in scored), default=1)
            max_dist = max(max_dist, 1)

            for s in scored:
                if s["cost_per_kg"] > 0:
                    cost_eff = 1 - (s["cost_per_kg"] / max_cost)
                else:
                    # Fallback to distance if no tariff data
                    cost_eff = 1 - (s["dist"] / max_dist)
                s["cost_eff"] = cost_eff
                s["total_score"] = (
                    _WEIGHT_VELOCITY * s["vel_score"]
                    + _WEIGHT_NEED * s["need_score"]
                    + _WEIGHT_COST_EFF * cost_eff
                )

            scored.sort(key=lambda x: x["total_score"], reverse=True)

            for s in scored[:_MAX_SUGGESTIONS_PER_SKU]:
                if remaining <= 0:
                    break
                qty = min(remaining, max(1, int(s["space"]))) if s["space"] > 0 else min(remaining, int(s["vel"]))
                qty = max(1, int(qty))
                if qty <= 0:
                    continue

                dias = qty / (s["vel"] / 30.0) if s["vel"] > 0 else 999
                suggestions.append({
                    "SKU_PRODUCTO": sku,
                    "TIENDA_ORIGEN": orig_name,
                    "ID_ORIGEN": orig_id,
                    "TIENDA_DESTINO": s["recv_name"],
                    "ID_DESTINO": s["recv_id"],
                    "QTY_SUGERIDA": int(qty),
                    "VALOR_CLP": qty * orig.get("ULTIMO_COSTO", 0),
                    "DIAS_VENTA_EST": round(dias, 0),
                    "DISTANCIA_KM": round(s["dist"], 1),
                    "SCORE": round(s["total_score"], 3),
                    "TIPO": "TIENDA_A_TIENDA",
                    "VEL_DESTINO": s["vel"],
                    "STOCK_DESTINO": s["recv_stock"],
                    "LAT_ORIGEN": orig_lat,
                    "LON_ORIGEN": orig_lon,
                    "LAT_DESTINO": s["recv_lat"],
                    "LON_DESTINO": s["recv_lon"],
                    "COSTO_POR_KG": round(s["cost_per_kg"], 0),
                    "COSTO_TRANSPORTE_EST": round(s["cost_per_unit"] * qty, 0),
                    "PESO_COBRABLE_KG": round(orig.get("PESO_BRUTO_UNIDAD", 0), 2),
                    "BASE_DESTINO": s.get("recv_base", ""),
                })
                remaining -= qty

            # If still remaining, try CD
            if remaining > 0:
                cd_stock_val = cd_stock_map.get(sku, 0)
                cd_vel_val = cd_vel_map.get(sku, 0)
                if cd_stock_val == 0 and cd_vel_val > 0:
                    suggestions.append(_cd_suggestion(
                        remaining, orig, orig_name, orig_id, orig_lat, orig_lon,
                        cd_vel_val, cd_stock_val,
                    ))

    if not suggestions:
        return pd.DataFrame()

    df_sug = pd.DataFrame(suggestions)

    # Enrich with product dimensions
    if "maestra" in data and data["maestra"] is not None:
        m = data["maestra"].copy()
        m.columns = [c.upper().strip() for c in m.columns]
        m_dedup = m.drop_duplicates("SKU_PRODUCTO", keep="first")
        for col in ["NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA", "CLASE_ABC"]:
            if col in m_dedup.columns and col not in df_sug.columns:
                df_sug = df_sug.merge(m_dedup[["SKU_PRODUCTO", col]], on="SKU_PRODUCTO", how="left")

    # Enrich ABC from abc table
    if "abc" in data and data["abc"] is not None and "CLASE_ABC" not in df_sug.columns:
        a = data["abc"].copy()
        a.columns = [c.upper().strip() for c in a.columns]
        if "CLASE_ABC" in a.columns:
            a_dedup = a[["SKU_PRODUCTO", "CLASE_ABC"]].drop_duplicates("SKU_PRODUCTO", keep="first")
            df_sug = df_sug.merge(a_dedup, on="SKU_PRODUCTO", how="left")

    if "CLASE_ABC" not in df_sug.columns:
        df_sug["CLASE_ABC"] = "C"
    df_sug["CLASE_ABC"] = df_sug["CLASE_ABC"].fillna("C")

    return df_sug


# ============================================================================
# RENDERING
# ============================================================================

def render_redistribucion(conn):
    """Redistribucion de Stock module entry point."""
    st.html("<h2 class='sub-header'>Redistribucion de Stock</h2>")
    st.caption(
        "Identifica stock inmovilizado en tiendas y sugiere redistribuirlo "
        "a tiendas con mejor rotacion, o devolver al CD para ecommerce/mayorista. "
        "Considera transito existente, perfiles minimos y distancia geografica."
    )

    # ── Load data ───────────────────────────────────────────────────────
    try:
        data = _load_redistribucion_data(conn)
    except Exception as e:
        st.error(f"Error cargando datos: {e}")
        import traceback
        st.code(traceback.format_exc())
        return

    if data["stock"] is not None and not data["stock"].empty:
        data["stock"] = apply_pm_filter(data["stock"])

    if data["stock"] is None or data["stock"].empty:
        st.error("No se encontraron datos de stock por tienda.")
        return

    # ── Parse transit ───────────────────────────────────────────────────
    df_transito, transito_info = _get_transito_info(data["transito"])
    transito_map: dict[tuple[str, str], float] = {}
    if not df_transito.empty:
        for _, row in df_transito.iterrows():
            key = (row["SKU_PRODUCTO"], row["ID_SUCURSAL_DESTINO"])
            transito_map[key] = transito_map.get(key, 0) + row.get("QTY_TRANSITO", 0)

    # ── Build base table ────────────────────────────────────────────────
    with st.spinner("Construyendo tabla base SKU x Tienda..."):
        df_base = _build_base_table(data)

    if df_base.empty:
        st.error("No hay stock en tiendas para analizar.")
        return

    # ── Tabs ────────────────────────────────────────────────────────────
    tab_resumen, tab_inmov, tab_sug, tab_mapa = st.tabs([
        "Resumen", "Stock Inmovilizado", "Sugerencias", "Mapa",
    ])

    # ================================================================
    # TAB 1 — RESUMEN
    # ================================================================
    with tab_resumen:
        st.markdown("### Diagnostico de Stock Inmovilizado")

        _c1, _c2 = st.columns([1, 3])
        with _c1:
            moi_threshold = st.slider(
                "Umbral MOI (meses)", min_value=2, max_value=24, value=_DEFAULT_MOI_THRESHOLD,
                step=1, key="redist_moi_threshold",
                help="SKUs con MOI > este valor se consideran inmovilizados",
            )

        # Global area filter
        areas_avail = sorted(df_base["AREA"].dropna().unique().tolist()) if "AREA" in df_base.columns else []
        sel_areas = _c2.multiselect("Filtrar Area", areas_avail, key="redist_area_filter")

        df_filtered = df_base.copy()
        if sel_areas:
            df_filtered = df_filtered[df_filtered["AREA"].isin(sel_areas)]

        df_immob = _filter_immobilized(df_filtered, moi_threshold)

        # KPIs
        n_skus = df_immob["SKU_PRODUCTO"].nunique() if not df_immob.empty else 0
        total_und = int(df_immob["EXCESO_UND"].sum()) if not df_immob.empty else 0
        total_valor = df_immob["VALOR_INMOVILIZADO"].sum() if not df_immob.empty else 0
        n_tiendas = df_immob["ID_SUCURSAL"].nunique() if not df_immob.empty else 0

        _k1, _k2, _k3, _k4 = st.columns(4)
        _k1.metric("SKUs Inmovilizados", f"{n_skus:,}")
        _k2.metric("Unidades Redistribuibles", f"{total_und:,}")
        _k3.metric("Valor Inmovilizado", _fmt_mm(total_valor))
        _k4.metric("Tiendas Afectadas", f"{n_tiendas:,}")

        # Transit info
        if transito_info["raw_rows"] > 0:
            n_transit_combos = len(transito_map)
            total_transit_qty = int(sum(transito_map.values()))
            st.info(
                f"Transito unificado (COO + pedidos hoy): "
                f"{n_transit_combos:,} combinaciones SKU×Tienda, "
                f"{total_transit_qty:,} unidades en transito"
            )
        elif transito_info["raw_rows"] == 0:
            st.warning("Tabla de transito vacia o no disponible. Las sugerencias no descartaran tiendas con transito.")

        if not df_immob.empty:
            st.markdown("---")

            # Chart 1: Top 20 líneas por valor
            st.markdown("##### Top 20 Lineas por Valor Inmovilizado")
            if "LINEA" in df_immob.columns:
                by_linea = (
                    df_immob.groupby(["AREA", "LINEA"], as_index=False)
                    .agg(VALOR=("VALOR_INMOVILIZADO", "sum"), SKUS=("SKU_PRODUCTO", "nunique"))
                    .sort_values("VALOR", ascending=True).tail(20)
                )
                fig_top = go.Figure(layout=dorel_layout(
                    title=dict(text="Valor Inmovilizado por Linea (Top 20)", font_size=14, x=0.5),
                    xaxis=dict(title="Valor ($CLP)", gridcolor="#ECECEC"),
                    height=max(len(by_linea) * 28, 400),
                    margin=dict(l=200, r=30, t=60, b=40),
                ))
                fig_top.add_trace(go.Bar(
                    y=by_linea["LINEA"],
                    x=by_linea["VALOR"],
                    orientation="h",
                    marker=dict(color=COLORS["status_critical"]),
                    text=[_fmt_mm(v) for v in by_linea["VALOR"]],
                    textposition="outside",
                    textfont=dict(size=10),
                    hovertemplate="Linea: %{y}<br>Valor: $%{x:,.0f}<br><extra></extra>",
                    showlegend=False,
                ))
                st.plotly_chart(fig_top, use_container_width=True)

            # Chart 2: Distribución MOI
            st.markdown("##### Distribucion por Rango MOI")
            bucket_order = ["SIN_VENTA", "12m+", "6-12m", "3-6m", "0-3m"]
            bucket_counts = df_immob.groupby("MOI_BUCKET").agg(
                SKUS=("SKU_PRODUCTO", "nunique"),
                VALOR=("VALOR_INMOVILIZADO", "sum"),
            ).reindex([b for b in bucket_order if b in df_immob["MOI_BUCKET"].unique()])

            if not bucket_counts.empty:
                fig_dist = go.Figure(layout=dorel_layout(
                    title=dict(text="SKUs por Rango de MOI", font_size=14, x=0.5),
                    yaxis=dict(title="SKUs", gridcolor="#ECECEC"),
                    height=350,
                ))
                fig_dist.add_trace(go.Bar(
                    x=bucket_counts.index,
                    y=bucket_counts["SKUS"],
                    marker=dict(color=[_MOI_COLORS.get(b, "#ccc") for b in bucket_counts.index]),
                    text=[f"{v:,} SKUs\n{_fmt_mm(val)}" for v, val in
                          zip(bucket_counts["SKUS"], bucket_counts["VALOR"])],
                    textposition="outside",
                    textfont=dict(size=10),
                    showlegend=False,
                ))
                st.plotly_chart(fig_dist, use_container_width=True)

    # ================================================================
    # TAB 2 — STOCK INMOVILIZADO
    # ================================================================
    with tab_inmov:
        st.markdown("### Stock Inmovilizado por Tienda")

        if df_immob.empty:
            st.info("No hay stock inmovilizado con el umbral seleccionado.")
        else:
            # Filters
            _f1, _f2, _f3, _f4 = st.columns(4)

            lineas_avail = sorted(df_immob["LINEA"].dropna().unique().tolist()) if "LINEA" in df_immob.columns else []
            sel_lineas = _f1.multiselect("Linea", lineas_avail, key="redist_inmov_linea")

            marcas_avail = sorted(df_immob["MARCA"].dropna().unique().tolist()) if "MARCA" in df_immob.columns else []
            sel_marcas = _f2.multiselect("Marca", marcas_avail, key="redist_inmov_marca")

            tiendas_avail = sorted(df_immob["DESCRIPCION_SUCURSAL"].dropna().unique().tolist()) if "DESCRIPCION_SUCURSAL" in df_immob.columns else []
            sel_tiendas = _f3.multiselect("Sucursal", tiendas_avail, key="redist_inmov_tienda")

            abc_avail = sorted(df_immob["CLASE_ABC"].dropna().unique().tolist()) if "CLASE_ABC" in df_immob.columns else []
            sel_abc = _f4.multiselect("ABC", abc_avail, key="redist_inmov_abc")

            df_show = df_immob.copy()
            if sel_lineas:
                df_show = df_show[df_show["LINEA"].isin(sel_lineas)]
            if sel_marcas:
                df_show = df_show[df_show["MARCA"].isin(sel_marcas)]
            if sel_tiendas:
                df_show = df_show[df_show["DESCRIPCION_SUCURSAL"].isin(sel_tiendas)]
            if sel_abc:
                df_show = df_show[df_show["CLASE_ABC"].isin(sel_abc)]

            show_cols = [c for c in [
                "SKU_PRODUCTO", "NOM_PRODUCTO", "DESCRIPCION_SUCURSAL",
                "STOCK_UNIDADES", "VTA_MENSUAL_PROM", "MOI_TIENDA",
                "MIN_INV_REQUERIDO", "EXCESO_UND", "VALOR_INMOVILIZADO",
                "CLASE_ABC", "AREA", "LINEA", "MARCA", "MOI_BUCKET",
            ] if c in df_show.columns]

            df_display = df_show[show_cols].sort_values("VALOR_INMOVILIZADO", ascending=False)

            # Replace inf with label
            if "MOI_TIENDA" in df_display.columns:
                df_display["MOI_TIENDA"] = df_display["MOI_TIENDA"].replace([np.inf], 999)

            col_config = {
                "VALOR_INMOVILIZADO": st.column_config.NumberColumn("Valor ($)", format="$%,.0f"),
                "VTA_MENSUAL_PROM": st.column_config.NumberColumn("Vta Mensual", format="%.1f"),
                "MOI_TIENDA": st.column_config.NumberColumn("MOI (meses)", format="%.1f"),
                "STOCK_UNIDADES": st.column_config.NumberColumn("Stock", format="%d"),
                "EXCESO_UND": st.column_config.NumberColumn("Exceso", format="%d"),
                "MIN_INV_REQUERIDO": st.column_config.NumberColumn("Min Perfil", format="%d"),
            }

            st.dataframe(
                df_display,
                use_container_width=True,
                height=min(len(df_display) * 36 + 40, 700),
                hide_index=True,
                column_config=col_config,
            )
            st.caption(f"{len(df_display):,} registros SKU×Tienda")
            download_buttons(df_display, prefix="stock_inmovilizado")

    # ================================================================
    # TAB 3 — SUGERENCIAS
    # ================================================================
    with tab_sug:
        st.markdown("### Sugerencias de Redistribucion")

        if df_immob.empty:
            st.info("No hay stock inmovilizado para redistribuir.")
        else:
            with st.spinner("Generando sugerencias de redistribucion..."):
                df_sug = _generate_suggestions(df_base, df_immob, transito_map, data, moi_threshold)

            if df_sug.empty:
                st.warning("No se encontraron tiendas receptoras para el stock inmovilizado.")
            else:
                # KPIs
                n_sug = len(df_sug)
                total_qty = int(df_sug["QTY_SUGERIDA"].sum())
                total_val = df_sug["VALOR_CLP"].sum()
                avg_dias = df_sug.loc[df_sug["DIAS_VENTA_EST"] < 999, "DIAS_VENTA_EST"].mean()
                avg_dias = avg_dias if pd.notna(avg_dias) else 0

                total_transport = df_sug["COSTO_TRANSPORTE_EST"].sum() if "COSTO_TRANSPORTE_EST" in df_sug.columns else 0

                _k1, _k2, _k3, _k4, _k5 = st.columns(5)
                _k1.metric("Total Sugerencias", f"{n_sug:,}")
                _k2.metric("Unidades a Mover", f"{total_qty:,}")
                _k3.metric("Valor Producto", _fmt_mm(total_val))
                _k4.metric("Costo Transp. Est.", _fmt_mm(total_transport))
                _k5.metric("Dias Prom Venta", f"{avg_dias:.0f}")

                # Sub-filter by type
                tipo_view = st.radio(
                    "Vista", ["Todas", "Tienda → Tienda", "Devolver a CD"],
                    horizontal=True, key="redist_sug_tipo",
                )
                df_sug_view = df_sug.copy()
                if tipo_view == "Tienda → Tienda":
                    df_sug_view = df_sug_view[df_sug_view["TIPO"] == "TIENDA_A_TIENDA"]
                elif tipo_view == "Devolver a CD":
                    df_sug_view = df_sug_view[df_sug_view["TIPO"] == "DEVOLVER_CD"]

                # Table — format monetary columns as CLP
                df_sug_view_fmt = df_sug_view.copy()
                for _mc in ["VALOR_CLP", "COSTO_TRANSPORTE_EST"]:
                    if _mc in df_sug_view_fmt.columns:
                        df_sug_view_fmt[f"{_mc}_FMT"] = df_sug_view_fmt[_mc].apply(fmt_clp)
                if "COSTO_POR_KG" in df_sug_view_fmt.columns:
                    df_sug_view_fmt["COSTO_POR_KG_FMT"] = df_sug_view_fmt["COSTO_POR_KG"].apply(
                        lambda x: fmt_clp(x) if x > 0 else "-"
                    )

                show_cols_sug = [c for c in [
                    "SKU_PRODUCTO", "NOM_PRODUCTO", "TIENDA_ORIGEN", "TIENDA_DESTINO",
                    "QTY_SUGERIDA", "VALOR_CLP_FMT", "COSTO_TRANSPORTE_EST_FMT",
                    "COSTO_POR_KG_FMT", "DIAS_VENTA_EST", "DISTANCIA_KM",
                    "SCORE", "CLASE_ABC", "AREA", "LINEA", "MARCA", "TIPO",
                ] if c in df_sug_view_fmt.columns]

                df_sug_display = df_sug_view_fmt[show_cols_sug].sort_values(
                    "SCORE" if "SCORE" in df_sug_view_fmt.columns else show_cols_sug[0],
                    ascending=False,
                )

                col_config_sug = {
                    "VALOR_CLP_FMT": st.column_config.TextColumn("Valor ($)", width="small"),
                    "COSTO_TRANSPORTE_EST_FMT": st.column_config.TextColumn("Costo Transp.", width="small"),
                    "COSTO_POR_KG_FMT": st.column_config.TextColumn("$/kg Ruta", width="small"),
                    "QTY_SUGERIDA": st.column_config.NumberColumn("Qty", format="%d"),
                    "DIAS_VENTA_EST": st.column_config.NumberColumn("Dias Venta", format="%.0f"),
                    "DISTANCIA_KM": st.column_config.NumberColumn("Dist (km)", format="%.0f"),
                    "SCORE": st.column_config.NumberColumn("Score", format="%.3f"),
                }

                st.dataframe(
                    df_sug_display,
                    use_container_width=True,
                    height=min(len(df_sug_display) * 36 + 40, 700),
                    hide_index=True,
                    column_config=col_config_sug,
                )

                # Chart: Top 15 by value
                st.markdown("---")
                st.markdown("##### Top 15 Movimientos por Valor")
                top_moves = df_sug_view.nlargest(15, "VALOR_CLP")
                if not top_moves.empty:
                    labels = [
                        f"{r.get('SKU_PRODUCTO', '')[:12]} | {r.get('TIENDA_ORIGEN', '')[:15]} → {r.get('TIENDA_DESTINO', '')[:15]}"
                        for _, r in top_moves.iterrows()
                    ]
                    fig_moves = go.Figure(layout=dorel_layout(
                        title=dict(text="Top Movimientos Sugeridos", font_size=14, x=0.5),
                        xaxis=dict(title="Valor ($CLP)", gridcolor="#ECECEC"),
                        height=max(len(top_moves) * 30, 350),
                        margin=dict(l=320, r=30, t=60, b=40),
                    ))
                    colors = [COLORS["primary"] if t == "TIENDA_A_TIENDA"
                              else COLORS["tertiary_teal"]
                              for t in top_moves["TIPO"]]
                    fig_moves.add_trace(go.Bar(
                        y=labels,
                        x=top_moves["VALOR_CLP"],
                        orientation="h",
                        marker=dict(color=colors),
                        text=[_fmt_mm(v) for v in top_moves["VALOR_CLP"]],
                        textposition="outside",
                        textfont=dict(size=9),
                        showlegend=False,
                    ))
                    st.plotly_chart(fig_moves, use_container_width=True)

                download_buttons(df_sug_view[show_cols_sug], prefix="sugerencias_redistribucion")

                # ── Proyeccion mensual de venta post-redistribucion ──
                st.markdown("---")
                st.markdown("##### Proyeccion Mensual de Venta si se Redistribuye")
                st.caption(
                    "Simula mes a mes cuántas unidades vendería cada tienda destino "
                    "con el stock redistribuido, basado en su velocidad real (90 días)."
                )

                _N_MESES_PROY = 6
                from datetime import datetime
                from dateutil.relativedelta import relativedelta

                hoy = datetime.now().replace(day=1)
                meses = [hoy + relativedelta(months=i) for i in range(_N_MESES_PROY)]
                mes_labels = [m.strftime("%b %Y") for m in meses]

                rows_proy = []
                for _, sug in df_sug_view.iterrows():
                    qty_restante = sug.get("QTY_SUGERIDA", 0)
                    vel = sug.get("VEL_DESTINO", 0)
                    costo_unit = sug.get("VALOR_CLP", 0) / max(sug.get("QTY_SUGERIDA", 1), 1)
                    sku = sug.get("SKU_PRODUCTO", "")
                    destino = sug.get("TIENDA_DESTINO", "")
                    area = sug.get("AREA", "")
                    linea = sug.get("LINEA", "")

                    for i, mes in enumerate(meses):
                        if qty_restante <= 0:
                            break
                        venta_mes = min(qty_restante, vel)
                        valor_mes = venta_mes * costo_unit
                        rows_proy.append({
                            "PERIODO": mes,
                            "MES": mes_labels[i],
                            "SKU_PRODUCTO": sku,
                            "TIENDA_DESTINO": destino,
                            "AREA": area,
                            "LINEA": linea,
                            "UND_VENDIDAS": venta_mes,
                            "VALOR_VENTA_CLP": valor_mes,
                            "STOCK_RESTANTE": qty_restante - venta_mes,
                        })
                        qty_restante -= venta_mes

                if rows_proy:
                    df_proy_redist = pd.DataFrame(rows_proy)

                    # Aggregate by month
                    agg_mes = df_proy_redist.groupby("MES", as_index=False).agg(
                        UND_VENDIDAS=("UND_VENDIDAS", "sum"),
                        VALOR_VENTA_CLP=("VALOR_VENTA_CLP", "sum"),
                    )
                    # Keep month order
                    agg_mes["_orden"] = agg_mes["MES"].map({m: i for i, m in enumerate(mes_labels)})
                    agg_mes = agg_mes.sort_values("_orden")

                    # KPIs
                    total_und_proy = agg_mes["UND_VENDIDAS"].sum()
                    total_val_proy = agg_mes["VALOR_VENTA_CLP"].sum()
                    meses_para_vender = (agg_mes["UND_VENDIDAS"] > 0).sum()

                    _pk1, _pk2, _pk3 = st.columns(3)
                    _pk1.metric("Und Vendidas (6m)", f"{total_und_proy:,.0f}")
                    _pk2.metric("Valor Estimado (6m)", _fmt_mm(total_val_proy))
                    _pk3.metric("Meses para Liquidar", f"{meses_para_vender}")

                    # Chart: bar + line
                    fig_proy = go.Figure(layout=dorel_layout(
                        title=dict(text="Venta Mensual Proyectada Post-Redistribucion", font_size=14, x=0.5),
                        yaxis=dict(title="Unidades", gridcolor="#ECECEC"),
                        yaxis2=dict(title="Valor ($CLP)", overlaying="y", side="right",
                                    showgrid=False),
                        height=380,
                        legend=dict(orientation="h", y=-0.15, xanchor="center", x=0.5),
                    ))
                    fig_proy.add_trace(go.Bar(
                        x=agg_mes["MES"], y=agg_mes["UND_VENDIDAS"],
                        name="Unidades",
                        marker=dict(color=COLORS["primary"]),
                        text=[f"{v:,.0f}" for v in agg_mes["UND_VENDIDAS"]],
                        textposition="outside", textfont=dict(size=10),
                    ))
                    fig_proy.add_trace(go.Scatter(
                        x=agg_mes["MES"], y=agg_mes["VALOR_VENTA_CLP"],
                        name="Valor ($)",
                        yaxis="y2",
                        line=dict(color=COLORS["tertiary_teal"], width=2.5),
                        marker=dict(size=7),
                        text=[_fmt_mm(v) for v in agg_mes["VALOR_VENTA_CLP"]],
                        textposition="top center", textfont=dict(size=9),
                        mode="lines+markers+text",
                    ))
                    st.plotly_chart(fig_proy, use_container_width=True)

                    # Detalle por linea × mes
                    with st.expander("Detalle por Linea × Mes"):
                        det_linea = df_proy_redist.groupby(["MES", "LINEA"], as_index=False).agg(
                            UND_VENDIDAS=("UND_VENDIDAS", "sum"),
                            VALOR_VENTA_CLP=("VALOR_VENTA_CLP", "sum"),
                        )
                        det_linea["_orden"] = det_linea["MES"].map({m: i for i, m in enumerate(mes_labels)})
                        det_linea = det_linea.sort_values(["_orden", "LINEA"])

                        # Pivot for readability
                        piv_und = det_linea.pivot_table(
                            index="LINEA", columns="MES", values="UND_VENDIDAS",
                            aggfunc="sum", fill_value=0,
                        )
                        # Reorder columns by month
                        piv_und = piv_und[[m for m in mes_labels if m in piv_und.columns]]
                        piv_und["TOTAL"] = piv_und.sum(axis=1)
                        piv_und = piv_und.sort_values("TOTAL", ascending=False)
                        st.markdown("**Unidades por Linea**")
                        st.dataframe(piv_und, use_container_width=True)

                        piv_val = det_linea.pivot_table(
                            index="LINEA", columns="MES", values="VALOR_VENTA_CLP",
                            aggfunc="sum", fill_value=0,
                        )
                        piv_val = piv_val[[m for m in mes_labels if m in piv_val.columns]]
                        piv_val["TOTAL"] = piv_val.sum(axis=1)
                        piv_val = piv_val.sort_values("TOTAL", ascending=False)
                        st.markdown("**Valor ($CLP) por Linea**")
                        col_config_piv = {c: st.column_config.NumberColumn(format="$%,.0f")
                                          for c in piv_val.columns}
                        st.dataframe(piv_val, use_container_width=True, column_config=col_config_piv)

                    download_buttons(df_proy_redist, prefix="proyeccion_redistribucion")
                else:
                    st.info("No hay datos suficientes para proyectar ventas.")

    # ================================================================
    # TAB 4 — MAPA
    # ================================================================
    with tab_mapa:
        st.markdown("### Mapa de Redistribucion")

        if df_immob.empty:
            st.info("No hay datos para mostrar en el mapa.")
        else:
            # Aggregate immobilized value per store
            geo_data = df_immob.groupby("ID_SUCURSAL", as_index=False).agg(
                VALOR_TOTAL=("VALOR_INMOVILIZADO", "sum"),
                N_SKUS=("SKU_PRODUCTO", "nunique"),
                STOCK_TOTAL=("EXCESO_UND", "sum"),
                DESCRIPCION=("DESCRIPCION_SUCURSAL", "first"),
                LATITUD=("LATITUD", "first"),
                LONGITUD=("LONGITUD", "first"),
                CIUDAD=("CIUDAD", "first") if "CIUDAD" in df_immob.columns else ("ID_SUCURSAL", "first"),
            )
            geo_data = geo_data[(geo_data["LATITUD"] != 0) & (geo_data["LONGITUD"] != 0)]

            if geo_data.empty:
                st.warning("No hay datos de georeferencia para las tiendas.")
            else:
                # Size and color
                max_val = geo_data["VALOR_TOTAL"].max()
                geo_data["SIZE"] = np.clip(geo_data["VALOR_TOTAL"] / max(max_val, 1) * 30 + 5, 5, 40)
                geo_data["COLOR_VAL"] = np.clip(geo_data["VALOR_TOTAL"] / max(max_val, 1), 0, 1)

                fig_map = go.Figure(layout=dorel_layout(height=600))

                # Store markers
                fig_map.add_trace(go.Scattermapbox(
                    lat=geo_data["LATITUD"],
                    lon=geo_data["LONGITUD"],
                    mode="markers",
                    marker=dict(
                        size=geo_data["SIZE"],
                        color=geo_data["VALOR_TOTAL"],
                        colorscale=[[0, COLORS["status_on_track"]], [0.5, COLORS["status_at_risk"]],
                                    [1, COLORS["status_critical"]]],
                        showscale=True,
                        colorbar=dict(title="Valor Inmov."),
                    ),
                    text=[
                        f"{row['DESCRIPCION']}<br>{row.get('CIUDAD', '')}<br>"
                        f"Valor: {_fmt_mm(row['VALOR_TOTAL'])}<br>"
                        f"SKUs: {row['N_SKUS']:,}<br>Und: {int(row['STOCK_TOTAL']):,}"
                        for _, row in geo_data.iterrows()
                    ],
                    hoverinfo="text",
                    name="Tiendas",
                ))

                # Draw flow lines for top suggestions
                df_sug_cached = df_sug if 'df_sug' in dir() and not df_sug.empty else pd.DataFrame()
                if not df_sug_cached.empty:
                    top_flows = df_sug_cached[df_sug_cached["TIPO"] == "TIENDA_A_TIENDA"].nlargest(30, "VALOR_CLP")
                    for _, flow in top_flows.iterrows():
                        lat_o, lon_o = flow.get("LAT_ORIGEN", 0), flow.get("LON_ORIGEN", 0)
                        lat_d, lon_d = flow.get("LAT_DESTINO", 0), flow.get("LON_DESTINO", 0)
                        if lat_o != 0 and lon_o != 0 and lat_d != 0 and lon_d != 0:
                            fig_map.add_trace(go.Scattermapbox(
                                lat=[lat_o, lat_d],
                                lon=[lon_o, lon_d],
                                mode="lines",
                                line=dict(width=1.5, color=COLORS["primary"]),
                                opacity=0.4,
                                showlegend=False,
                                hoverinfo="skip",
                            ))

                fig_map.update_layout(
                    mapbox=dict(
                        style="open-street-map",
                        center=dict(
                            lat=geo_data["LATITUD"].median(),
                            lon=geo_data["LONGITUD"].median(),
                        ),
                        zoom=5,
                    ),
                    margin=dict(l=0, r=0, t=30, b=0),
                )
                st.plotly_chart(fig_map, use_container_width=True)
                st.caption(
                    f"{len(geo_data):,} tiendas en mapa | "
                    f"Tamaño = valor inmovilizado | "
                    f"Lineas = flujos sugeridos (top 30)"
                )
