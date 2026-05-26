import pandas as pd
import numpy as np
import streamlit as st
import io
from datetime import datetime, date
import plotly.graph_objects as go
from db.queries import (
    QUERY_MIRROR_QUALITY,
    QUERY_MIRROR_HIERARCHY,
    QUERY_MIRROR_HIST_SKU,
    QUERY_MIRROR_HIST_MONTHLY,
    QUERY_MIRROR_SUCURSAL,
    _VCM,
    _PROD,
)
from config import COLORS, dorel_layout, apply_pm_filter
from db.cache import cached_query as cq, run_sql
from utils.filters import norm_cols
from utils.ui_animations import lottie_spinner


# ==============================================================================
# 1. MIRROR QUALITY ANALYSIS
# ==============================================================================
def get_mirror_quality(df_input, conn, start_date, end_date):
    """Analyze sales history quality for mirror SKUs."""
    if "ESPEJO" not in df_input.columns:
        return None, None, "Falta columna 'ESPEJO' en el archivo."

    espejos = df_input["ESPEJO"].unique().tolist()
    placeholders = ", ".join(["%s"] * len(espejos))
    query = QUERY_MIRROR_QUALITY.format(placeholders=placeholders)
    params = espejos + [str(start_date), str(end_date)]

    try:
        df_quality = run_sql(conn, query, params)
    except Exception as e:
        return None, None, f"Error consultando Snowflake: {e}"

    if df_quality.empty:
        return pd.DataFrame(), None, "No se encontro historia para estos espejos en el rango seleccionado."

    df_quality["PERIODO"] = (
        df_quality["ANIO"].astype(str) + "-" + df_quality["MES"].astype(str).str.zfill(2)
    )

    df_pivot = df_quality.pivot_table(
        index="ESPEJO",
        columns="PERIODO",
        values="DIAS_CON_VENTA",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()

    anios = df_quality["ANIO"].unique()
    for y in anios:
        cols_anio = [c for c in df_pivot.columns if str(y) in str(c)]
        df_pivot[f"TOTAL_{y}"] = df_pivot[cols_anio].sum(axis=1)
        df_pivot[f"PCT_{y}"] = (df_pivot[f"TOTAL_{y}"] / 365.0).clip(upper=1.0)

    return df_pivot, df_quality, None


def get_period_recommendations(df_raw):
    """Generate period recommendations per mirror SKU based on data density."""
    if df_raw.empty:
        return pd.DataFrame()

    recs = []
    df_raw["FECHA_DATE"] = pd.to_datetime(
        df_raw["ANIO"].astype(str) + "-" + df_raw["MES"].astype(str) + "-01"
    )

    for espejo, group in df_raw.groupby("ESPEJO"):
        validos = group[group["DIAS_CON_VENTA"] >= 3].sort_values("FECHA_DATE")

        if validos.empty:
            recs.append({
                "ESPEJO": espejo,
                "INICIO_SUGERIDO": "-",
                "FIN_SUGERIDO": "-",
                "COMENTARIO": "Sin data suficiente",
            })
            continue

        f_min = validos["FECHA_DATE"].min().date()
        f_max = validos["FECHA_DATE"].max().date()

        rango_meses = (f_max.year - f_min.year) * 12 + (f_max.month - f_min.month) + 1
        meses_activos = len(validos)
        continuidad = (meses_activos / rango_meses) * 100

        comentario = "Historia Continua"
        if continuidad < 80:
            comentario = f"Data intermitente ({int(continuidad)}% coverage)"

        meses_desde_fin = (date.today().year - f_max.year) * 12 + (date.today().month - f_max.month)
        if meses_desde_fin > 6:
            comentario += " (Data Antigua)"

        recs.append({
            "ESPEJO": espejo,
            "INICIO_SUGERIDO": f_min,
            "FIN_SUGERIDO": f_max,
            "COMENTARIO": comentario,
        })

    return pd.DataFrame(recs)


def get_quarterly_quality(df_raw):
    """Aggregate data by quarter and assign traffic light indicators."""
    if df_raw.empty:
        return pd.DataFrame()

    df = df_raw.copy()
    df["Q"] = df["ANIO"].astype(str) + "-Q" + df["MES"].apply(lambda m: (m - 1) // 3 + 1).astype(str)

    df_q = df.pivot_table(index="ESPEJO", columns="Q", values="DIAS_CON_VENTA", aggfunc="sum", fill_value=0)

    def traffic_light(val):
        if val >= 60:
            return "\U0001f7e2"
        if val >= 30:
            return "\U0001f7e1"
        return "\U0001f534"

    df_display = df_q.copy()
    for col in df_display.columns:
        df_display[col] = df_q[col].apply(lambda x: f"{traffic_light(x)} ({int(x)}d)")

    return df_display.reset_index()


# ==============================================================================
# 1b. MIRROR SKU FINDER — Scoring-based candidate search
# ==============================================================================

def _find_mirror_candidates(
    df_maestra: pd.DataFrame,
    df_ventas: pd.DataFrame,
    target_attrs: dict,
    top_n: int = 20,
) -> pd.DataFrame:
    """Find and rank mirror SKU candidates based on product similarity.

    Scoring weights (0-100 scale):
        Name (Jaccard tokens): 30 | Price proximity: 25 | Modelo: 15
        Procedencia: 10 | Proveedor: 10 | Pre-filter bonus: 10

    Parameters
    ----------
    df_maestra : Product master from cq.maestra().
    df_ventas  : Monthly sales+price from cq.ventas_mensual_precio().
    target_attrs : dict with keys SKU_PRODUCTO, SKU_NOM_PRODUCTO, SUBLINEA,
                   MARCA, MODELO, PROCEDENCIA, PROVEEDOR, PRECIO_NETO.
    top_n : Max candidates to return.
    """
    mae = df_maestra.copy()
    if mae.empty:
        return pd.DataFrame()

    # Deduplicate maestra
    mae = mae.drop_duplicates(subset=["SKU_PRODUCTO"])

    # ── 1. Recency filter: only SKUs with sales in last 6 months ──
    cutoff_6m = pd.Timestamp.today() - pd.DateOffset(months=6)
    if "ULTIMA_VENTA" in mae.columns:
        mae["ULTIMA_VENTA"] = pd.to_datetime(mae["ULTIMA_VENTA"], errors="coerce")
        mae = mae[mae["ULTIMA_VENTA"] >= cutoff_6m]

    if mae.empty:
        return pd.DataFrame()

    # ── 2. Exclude self ──
    self_sku = target_attrs.get("SKU_PRODUCTO")
    if self_sku:
        mae = mae[mae["SKU_PRODUCTO"] != self_sku]

    # ── 3. Pre-filter: SUBLINEA + MARCA (hard filter) ──
    target_sub = str(target_attrs.get("SUBLINEA", "") or "").strip().upper()
    target_marca = str(target_attrs.get("MARCA", "") or "").strip().upper()

    if "SUBLINEA" in mae.columns:
        mae["_SUB_UPPER"] = mae["SUBLINEA"].fillna("").astype(str).str.strip().str.upper()
    if "MARCA" in mae.columns:
        mae["_MARCA_UPPER"] = mae["MARCA"].fillna("").astype(str).str.strip().str.upper()

    if target_sub and target_marca:
        mae = mae[
            (mae.get("_SUB_UPPER", pd.Series(dtype=str)) == target_sub)
            & (mae.get("_MARCA_UPPER", pd.Series(dtype=str)) == target_marca)
        ]
    elif target_sub:
        mae = mae[mae.get("_SUB_UPPER", pd.Series(dtype=str)) == target_sub]
    elif target_marca:
        mae = mae[mae.get("_MARCA_UPPER", pd.Series(dtype=str)) == target_marca]

    if mae.empty:
        return pd.DataFrame()

    # ── 4. Price data: avg neto price per SKU (last 6 months) ──
    ven = df_ventas.copy()
    if not ven.empty and "PERIODO" in ven.columns:
        ven["PERIODO"] = pd.to_datetime(ven["PERIODO"], errors="coerce")
        ven = ven[ven["PERIODO"] >= cutoff_6m]

    if not ven.empty:
        for c in ["NETO", "CANTIDAD"]:
            if c in ven.columns:
                ven[c] = pd.to_numeric(ven[c], errors="coerce").fillna(0)

        price_vol = ven.groupby("SKU_PRODUCTO", as_index=False).agg(
            TOTAL_NETO=("NETO", "sum"),
            TOTAL_QTY=("CANTIDAD", "sum"),
        )
        price_vol["PRECIO_PROM_NETO"] = np.where(
            price_vol["TOTAL_QTY"] > 0,
            price_vol["TOTAL_NETO"] / price_vol["TOTAL_QTY"],
            0,
        )
        n_months = max(ven["PERIODO"].nunique(), 1) if "PERIODO" in ven.columns else 6
        price_vol["VOL_MENSUAL_PROM"] = price_vol["TOTAL_QTY"] / n_months

        mae = mae.merge(
            price_vol[["SKU_PRODUCTO", "PRECIO_PROM_NETO", "VOL_MENSUAL_PROM"]],
            on="SKU_PRODUCTO",
            how="left",
        )
    else:
        mae["PRECIO_PROM_NETO"] = 0.0
        mae["VOL_MENSUAL_PROM"] = 0.0

    mae["PRECIO_PROM_NETO"] = pd.to_numeric(mae["PRECIO_PROM_NETO"], errors="coerce").fillna(0)
    mae["VOL_MENSUAL_PROM"] = pd.to_numeric(mae["VOL_MENSUAL_PROM"], errors="coerce").fillna(0)

    # ── 5. Scoring ──
    target_name = str(target_attrs.get("SKU_NOM_PRODUCTO", "") or "").strip().upper()
    target_modelo = str(target_attrs.get("MODELO", "") or "").strip().upper()
    target_proc = str(target_attrs.get("PROCEDENCIA", "") or "").strip().upper()
    target_prov = str(target_attrs.get("PROVEEDOR", "") or "").strip().upper()
    target_price = float(target_attrs.get("PRECIO_NETO", 0) or 0)

    # 5a. Name similarity — Jaccard token overlap (30 pts)
    target_tokens = set(target_name.split()) if target_name else set()

    def _jaccard(name_str):
        if not target_tokens:
            return 0.0
        cand_tokens = set(str(name_str).strip().upper().split()) if name_str else set()
        if not cand_tokens:
            return 0.0
        inter = len(target_tokens & cand_tokens)
        union = len(target_tokens | cand_tokens)
        return inter / union if union > 0 else 0.0

    mae["_SC_NAME"] = mae["SKU_NOM_PRODUCTO"].fillna("").apply(_jaccard) * 30.0

    # 5b. Price proximity (25 pts)
    if target_price > 0:
        price_diff = (mae["PRECIO_PROM_NETO"] - target_price).abs()
        max_price = max(target_price, mae["PRECIO_PROM_NETO"].max(), 1)
        mae["_SC_PRICE"] = (1 - price_diff / max_price).clip(0, 1) * 25.0
    else:
        mae["_SC_PRICE"] = 0.0

    # 5c. Modelo match (15 pts)
    if target_modelo and "MODELO" in mae.columns:
        mae["_SC_MODELO"] = np.where(
            mae["MODELO"].fillna("").astype(str).str.strip().str.upper() == target_modelo,
            15.0, 0.0,
        )
    else:
        mae["_SC_MODELO"] = 0.0

    # 5d. Procedencia match (10 pts)
    if target_proc and "PROCEDENCIA" in mae.columns:
        mae["_SC_PROC"] = np.where(
            mae["PROCEDENCIA"].fillna("").astype(str).str.strip().str.upper() == target_proc,
            10.0, 0.0,
        )
    else:
        mae["_SC_PROC"] = 0.0

    # 5e. Proveedor match (10 pts)
    if target_prov and "PROVEEDOR" in mae.columns:
        mae["_SC_PROV"] = np.where(
            mae["PROVEEDOR"].fillna("").astype(str).str.strip().str.upper() == target_prov,
            10.0, 0.0,
        )
    else:
        mae["_SC_PROV"] = 0.0

    # 5f. Pre-filter bonus (10 pts — survived SUBLINEA+MARCA filter)
    mae["_SC_PREFILTER"] = 10.0

    # ── 6. Total score ──
    mae["SCORE"] = (
        mae["_SC_NAME"] + mae["_SC_PRICE"] + mae["_SC_MODELO"]
        + mae["_SC_PROC"] + mae["_SC_PROV"] + mae["_SC_PREFILTER"]
    )

    mae["DETALLE_SCORE"] = mae.apply(
        lambda r: (
            f"Nombre:{r['_SC_NAME']:.0f} | Precio:{r['_SC_PRICE']:.0f} | "
            f"Modelo:{r['_SC_MODELO']:.0f} | Proc:{r['_SC_PROC']:.0f} | "
            f"Prov:{r['_SC_PROV']:.0f}"
        ),
        axis=1,
    )

    # ── 7. Rank and return top_n ──
    mae = mae.sort_values("SCORE", ascending=False).head(top_n).reset_index(drop=True)
    mae["RANK"] = range(1, len(mae) + 1)

    result_cols = [
        "RANK", "SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "SUBLINEA", "MARCA",
        "MODELO", "PROCEDENCIA", "PROVEEDOR", "PRECIO_PROM_NETO",
        "VOL_MENSUAL_PROM", "ULTIMA_VENTA", "SCORE", "DETALLE_SCORE",
    ]
    available = [c for c in result_cols if c in mae.columns]
    return mae[available]


# ==============================================================================
# 2. CASCADE DISTRIBUTION
# ==============================================================================
def process_forecast_mirror(df_input, conn, start_date, end_date, dates_map=None):
    """Execute forecast disaggregation using cascade logic."""
    try:
        espejos = df_input["ESPEJO"].unique().tolist()
        placeholders = ", ".join(["%s"] * len(espejos))

        # A) Hierarchy
        q_hier = QUERY_MIRROR_HIERARCHY.format(placeholders=placeholders)
        df_hier = run_sql(conn, q_hier, espejos)

        # B) Historical sales
        real_start, real_end = start_date, end_date
        if dates_map:
            all_starts = [d[0] for d in dates_map.values()] + [start_date]
            all_ends = [d[1] for d in dates_map.values()] + [end_date]
            real_start = min(all_starts)
            real_end = max(all_ends)

        q_hist = QUERY_MIRROR_HIST_SKU.format(placeholders=placeholders)
        params_hist = espejos + [str(real_start), str(real_end)]
        df_hist_raw = run_sql(conn, q_hist, params_hist)
        df_hist_raw.columns = [c.upper() for c in df_hist_raw.columns]
        df_hist_raw["FECHA"] = pd.to_datetime(df_hist_raw["FECHA"]).dt.date

        # Sublinea fallback
        sublineas = df_hier["SUBLINEA"].unique().tolist()
        df_hist_sub = pd.DataFrame()
        if sublineas:
            subs_placeholders = ", ".join(["%s"] * len(sublineas))
            q_nivel2 = f"""
            SELECT c.sublinea, b.id_sucursal, SUM(a.cantidad) as venta_qty
            FROM {_VCM} a
            LEFT JOIN db_syncros.public.coo_maestro_sucursal b ON a.cod_ccosto = b.id_sucursal
            LEFT JOIN {_PROD} c ON a.sku_producto = c.sku_producto
            WHERE c.sublinea IN ({subs_placeholders})
              AND a.fecha BETWEEN %s AND %s
              AND b.canal_de_distribucion = 'TIENDA'
            GROUP BY 1, 2
            """
            params_sub = sublineas + [str(start_date), str(end_date)]
            df_hist_sub = run_sql(conn, q_nivel2, params_sub)
            df_hist_sub.columns = [c.upper() for c in df_hist_sub.columns]

        # Global fallback
        q_global = f"""
        SELECT b.id_sucursal, SUM(a.cantidad) as venta_global
        FROM {_VCM} a
        LEFT JOIN db_syncros.public.coo_maestro_sucursal b ON a.cod_ccosto = b.id_sucursal
        WHERE a.fecha BETWEEN %s AND %s
          AND b.canal_de_distribucion = 'TIENDA'
        GROUP BY 1
        """
        df_global = run_sql(conn, q_global, [str(start_date), str(end_date)])
        df_global.columns = [c.upper() for c in df_global.columns]

        # Filter to ACTIVA TIENDA stores only
        # (tiendas operativas segun dt_tienda.activa — false/NULL = cerrada o inactiva)
        try:
            _q_activas = """
                SELECT b.id_sucursal
                FROM db_syncros.public.coo_maestro_sucursal b
                LEFT JOIN db_dimensiones.dim.dv_tienda t ON b.id_sucursal = t."Cod_Bodega"
                WHERE b.canal_de_distribucion = 'TIENDA'
                  AND t."Status" = 'Abierta'
            """
            _df_activas = pd.read_sql(_q_activas, conn)
            _df_activas.columns = [c.upper() for c in _df_activas.columns]
            active_sucursales = set(_df_activas["ID_SUCURSAL"].tolist())
        except Exception:
            active_sucursales = set()  # fallback gracioso: no filtrar

        if active_sucursales:
            df_global = df_global[df_global["ID_SUCURSAL"].isin(active_sucursales)].copy()
            if not df_hist_sub.empty:
                df_hist_sub = df_hist_sub[df_hist_sub["ID_SUCURSAL"].isin(active_sucursales)].copy()

        # C) Calculate weights (safe division — numerador / denominador > 0 → 0 si vacío)
        total_global = df_global["VENTA_GLOBAL"].sum()
        df_global["peso"] = df_global["VENTA_GLOBAL"] / total_global if total_global > 0 else 0.0

        if not df_hist_sub.empty:
            df_hist_sub["total_sub"] = df_hist_sub.groupby("SUBLINEA")["VENTA_QTY"].transform("sum")
            df_hist_sub["peso"] = np.where(
                df_hist_sub["total_sub"] > 0,
                df_hist_sub["VENTA_QTY"] / df_hist_sub["total_sub"],
                0.0,
            )

        # D) Distribution
        results = []
        col_sku_nuevo = next(
            (c for c in df_input.columns if str(c).upper() in ["SKU_NUEVO", "ID_MATERIAL", "SKU_PRODUCTO"]),
            "SKU_NUEVO",
        )
        col_desc = next(
            (c for c in df_input.columns if str(c).upper() in ["DESCRIPCION", "NOM_PRODUCTO"]),
            "DESCRIPCION",
        )

        cols_fijas = [col_sku_nuevo, "ESPEJO", col_desc]
        cols_no_mes = cols_fijas + ["SKU_PRODUCTO", "ID_MATERIAL", "DESCRIPCION", "NOM_PRODUCTO"]
        cols_mes = [
            c
            for c in df_input.columns
            if c not in cols_fijas and str(c).upper() not in cols_no_mes and "UNNAMED" not in str(c).upper()
        ]

        df_long = pd.melt(
            df_input,
            id_vars=[c for c in df_input.columns if c in cols_fijas],
            value_vars=cols_mes,
            var_name="MES",
            value_name="QTY",
        )

        espejos_con_raw = set(df_hist_raw["ESPEJO"].unique())
        sublineas_con_hist = set(df_hist_sub["SUBLINEA"].unique()) if not df_hist_sub.empty else set()
        map_sub = df_hier.set_index("ESPEJO")["SUBLINEA"].to_dict()

        # Sucursal details
        df_suc = pd.read_sql(QUERY_MIRROR_SUCURSAL, conn)
        map_suc_desc = df_suc.set_index("ID_SUCURSAL")["DESCRIPCION_SUCURSAL"].to_dict()
        map_suc_canal = df_suc.set_index("ID_SUCURSAL")["CANAL"].to_dict()

        weights_cache = {}

        for _, row in df_long.iterrows():
            espejo = row.get("ESPEJO")
            qty = pd.to_numeric(row.get("QTY"), errors="coerce")
            sku_nuevo = row.get(col_sku_nuevo)
            descripcion = row.get(col_desc)
            mes = row.get("MES")

            if pd.isna(qty) or qty <= 0:
                continue

            weights = None

            # 1. Mirror SKU level
            if espejo in espejos_con_raw:
                if espejo in weights_cache:
                    weights = weights_cache[espejo]
                else:
                    f_start, f_end = start_date, end_date
                    if dates_map and espejo in dates_map:
                        f_start, f_end = dates_map[espejo]

                    mask = (
                        (df_hist_raw["ESPEJO"] == espejo)
                        & (df_hist_raw["FECHA"] >= f_start)
                        & (df_hist_raw["FECHA"] <= f_end)
                    )
                    df_sku_filtered = df_hist_raw[mask].groupby("ID_SUCURSAL")["VENTA_QTY"].sum().reset_index()

                    # Filtrar a tiendas activas en nivel SKU
                    if active_sucursales:
                        df_sku_filtered = df_sku_filtered[
                            df_sku_filtered["ID_SUCURSAL"].isin(active_sucursales)
                        ].copy()

                    if not df_sku_filtered.empty:
                        total_sku = df_sku_filtered["VENTA_QTY"].sum()
                        df_sku_filtered["peso"] = (
                            df_sku_filtered["VENTA_QTY"] / total_sku if total_sku > 0 else 0.0
                        )
                        weights = df_sku_filtered[["ID_SUCURSAL", "peso"]]
                        weights_cache[espejo] = weights

            # 2. Subline level
            if weights is None:
                sub = map_sub.get(espejo)
                if sub and sub in sublineas_con_hist:
                    weights = df_hist_sub[df_hist_sub["SUBLINEA"] == sub][["ID_SUCURSAL", "peso"]]

            # 3. Global fallback
            if weights is None:
                weights = df_global[["ID_SUCURSAL", "peso"]]

            df_calc = weights.copy()
            df_calc["QTY_ASIGNADA"] = qty * df_calc["peso"]
            df_calc["id_material"] = sku_nuevo
            df_calc["descripcion"] = descripcion
            df_calc["MES"] = mes
            results.append(df_calc)

        if not results:
            return None, "Sin resultados generados."

        df_final_long = pd.concat(results)
        df_final_long["descripcion_sucursal"] = df_final_long["ID_SUCURSAL"].map(map_suc_desc)
        df_final_long["canal"] = df_final_long["ID_SUCURSAL"].map(map_suc_canal)
        df_final_long = df_final_long.rename(columns={"ID_SUCURSAL": "id_sucursal"})
        df_final_long["id_material"] = df_final_long["id_material"].fillna("SIN_SKU")
        df_final_long["descripcion"] = df_final_long["descripcion"].fillna("SIN_DESCRIPCION")

        df_pivot = df_final_long.pivot_table(
            index=["id_material", "descripcion", "id_sucursal", "descripcion_sucursal", "canal"],
            columns="MES",
            values="QTY_ASIGNADA",
            aggfunc="sum",
            fill_value=0,
        ).reset_index()

        return df_pivot, None

    except Exception as e:
        return None, f"Error en Cascade: {e}"


# ==============================================================================
# 2b. GENERATE FORECAST FROM MIRROR (new flow)
# ==============================================================================

_CHANNEL_MAP = {"Tienda": "TIENDA", "E-commerce (ETAIL)": "ETAIL", "Mayorista": "MAYOR"}
_MONTH_NAMES_ES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}


def _detect_granular_columns(df):
    """Auto-detect SKU, sucursal, canal, description and month columns in granular forecast."""
    cols_upper = {c: c.upper().strip() for c in df.columns}

    col_sku = next(
        (c for c, u in cols_upper.items()
         if u in ("SKU_PRODUCTO", "ID_MATERIAL", "SKU", "SKU_NUEVO")), None
    )
    col_suc = next(
        (c for c, u in cols_upper.items()
         if u in ("ID_SUCURSAL", "COD_BODEGA", "SUCURSAL")), None
    )
    col_canal = next(
        (c for c, u in cols_upper.items()
         if u in ("CANAL", "CANAL_DE_DISTRIBUCION", "COD_CANAL")), None
    )
    col_desc = next(
        (c for c, u in cols_upper.items()
         if u in ("DESCRIPCION", "NOM_PRODUCTO", "SKU_NOM_PRODUCTO", "DESCRIPCION_SUCURSAL")), None
    )
    col_desc_suc = next(
        (c for c, u in cols_upper.items()
         if u in ("DESCRIPCION_SUCURSAL", "NOM_SUCURSAL", "NOMBRE_SUCURSAL")), None
    )

    # Month columns: anything not identified as a fixed column
    fixed = {col_sku, col_suc, col_canal, col_desc, col_desc_suc} - {None}
    cols_mes = [c for c in df.columns if c not in fixed and "UNNAMED" not in str(c).upper()]

    return {
        "sku": col_sku, "sucursal": col_suc, "canal": col_canal,
        "desc": col_desc, "desc_suc": col_desc_suc, "meses": cols_mes,
    }


def _load_mirror_history(conn, espejo_sku, start_date, end_date):
    """Load monthly sales by sucursal+canal for a mirror SKU."""
    df = run_sql(conn, QUERY_MIRROR_HIST_MONTHLY,
                 [espejo_sku, str(start_date), str(end_date)])
    df.columns = [c.upper() for c in df.columns]
    if "PERIODO" in df.columns:
        df["PERIODO"] = pd.to_datetime(df["PERIODO"])
    return df


def _load_mirror_history_batch(conn, espejo_skus, start_date, end_date):
    """Load monthly sales by sucursal+canal for multiple mirror SKUs in one query."""
    if not espejo_skus:
        return pd.DataFrame()
    placeholders = ", ".join(["%s"] * len(espejo_skus))
    query = f"""
    SELECT
        a.sku_producto as espejo,
        b.id_sucursal,
        b.descripcion_sucursal,
        b.canal_de_distribucion as canal,
        DATE_TRUNC('month', a.fecha) as periodo,
        SUM(a.cantidad) as venta_qty
    FROM {_VCM} a
    LEFT JOIN db_syncros.public.coo_maestro_sucursal b
        ON a.cod_ccosto = b.id_sucursal
    WHERE a.sku_producto IN ({placeholders})
      AND a.fecha BETWEEN %s AND %s
      AND a.cantidad > 0
      AND b.id_sucursal IS NOT NULL
    GROUP BY 1, 2, 3, 4, 5
    """
    params = list(espejo_skus) + [str(start_date), str(end_date)]
    df = run_sql(conn, query, params)
    df.columns = [c.upper() for c in df.columns]
    if "PERIODO" in df.columns:
        df["PERIODO"] = pd.to_datetime(df["PERIODO"])
    return df


def _build_projection_promedio(df_hist, future_periods):
    """Build flat monthly projection using historical average by sucursal+canal."""
    if df_hist.empty:
        return pd.DataFrame()

    n_months = df_hist["PERIODO"].nunique()
    if n_months == 0:
        return pd.DataFrame()

    # Average monthly qty per sucursal+canal
    avg = (
        df_hist.groupby(["ID_SUCURSAL", "DESCRIPCION_SUCURSAL", "CANAL"], as_index=False)
        ["VENTA_QTY"].sum()
    )
    avg["VENTA_QTY"] = avg["VENTA_QTY"] / n_months

    # Replicate for each future period
    rows = []
    for per in future_periods:
        chunk = avg.copy()
        chunk["PERIODO"] = pd.Timestamp(per)
        rows.append(chunk)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def _build_projection_yoy(df_hist, future_periods):
    """Build projection using same-month-last-year by sucursal+canal."""
    if df_hist.empty:
        return pd.DataFrame()

    df_hist = df_hist.copy()
    df_hist["MES_NUM"] = df_hist["PERIODO"].dt.month

    # Average by sucursal+canal+month (in case multiple years)
    avg_by_month = (
        df_hist.groupby(["ID_SUCURSAL", "DESCRIPCION_SUCURSAL", "CANAL", "MES_NUM"],
                        as_index=False)["VENTA_QTY"].mean()
    )

    # Global average fallback (if month not found)
    n_months = df_hist["PERIODO"].nunique()
    global_avg = (
        df_hist.groupby(["ID_SUCURSAL", "DESCRIPCION_SUCURSAL", "CANAL"], as_index=False)
        ["VENTA_QTY"].sum()
    )
    if n_months > 0:
        global_avg["VENTA_QTY"] = global_avg["VENTA_QTY"] / n_months

    rows = []
    for per in future_periods:
        m = per.month
        month_data = avg_by_month[avg_by_month["MES_NUM"] == m].copy()
        if month_data.empty:
            # Fallback: use global average
            chunk = global_avg.copy()
        else:
            chunk = month_data.drop(columns=["MES_NUM"])
        chunk["PERIODO"] = pd.Timestamp(per)
        rows.append(chunk)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def _build_24m_columns() -> list[str]:
    """Build list of 24 month column names (MM/YYYY) from current month forward."""
    today = date.today()
    cols = []
    for i in range(24):
        m = today.month + i
        y = today.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        cols.append(f"{m:02d}/{y}")
    return cols


def _normalize_month_col(col_name: str) -> str | None:
    """Try to convert any month column format to MM/YYYY.

    Handles: 'Ene-2026', 'Jan-2026', '01/2026', '2026-01', etc.
    Returns None if not a recognizable month column.
    """
    s = str(col_name).strip()

    # Already MM/YYYY
    import re
    m = re.match(r"^(\d{1,2})/(\d{4})$", s)
    if m:
        return f"{int(m.group(1)):02d}/{m.group(2)}"

    # Ene-2026, Feb-2026, etc.
    _es_to_num = {
        "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
        "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
    }
    _en_to_num = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m = re.match(r"^([A-Za-z]{3})[/-](\d{4})$", s)
    if m:
        abbr = m.group(1).lower()
        num = _es_to_num.get(abbr) or _en_to_num.get(abbr)
        if num:
            return f"{num:02d}/{m.group(2)}"

    # YYYY-MM
    m = re.match(r"^(\d{4})-(\d{1,2})$", s)
    if m:
        return f"{int(m.group(2)):02d}/{m.group(1)}"

    return None


def _generate_from_mirror(df_base, new_sku_id, new_sku_desc,
                          multiplier, channels_with_factor, cols_mes):
    """Generate forecast for new SKU from mirror base data.

    Always outputs 24 months (current month forward) in MM/YYYY format.
    Months without forecast data are filled with 0.

    Parameters
    ----------
    df_base : DataFrame in wide format [id_sucursal, descripcion_sucursal, canal, MES1, MES2...]
    new_sku_id : str
    new_sku_desc : str
    multiplier : float
    channels_with_factor : set of channel codes (e.g. {"TIENDA", "ETAIL"})
    cols_mes : list of month column names (any format — will be normalized)

    Returns
    -------
    (df_result, error_msg)
    """
    if df_base is None or df_base.empty:
        return None, "Sin datos base para generar forecast."

    df = df_base.copy()

    # Apply multiplier to selected channels
    for _, row in df.iterrows():
        canal = str(row.get("canal", "")).upper().strip()
        if canal in channels_with_factor:
            factor = multiplier
        else:
            factor = 1.0
        for col in cols_mes:
            if col in df.columns:
                df.at[row.name, col] = pd.to_numeric(row.get(col, 0), errors="coerce") * factor

    # Set SKU info
    df.insert(0, "id_material", new_sku_id)
    df.insert(1, "descripcion", new_sku_desc)

    # ── Normalize existing month columns to MM/YYYY ──────────────────────
    rename_map = {}
    for col in cols_mes:
        if col in df.columns:
            norm = _normalize_month_col(col)
            if norm and norm != col:
                rename_map[col] = norm
    if rename_map:
        df = df.rename(columns=rename_map)

    # ── Ensure full 24 months (current month + 23) ──────────────────────
    full_24m = _build_24m_columns()
    for mc in full_24m:
        if mc not in df.columns:
            df[mc] = 0

    # ── Final column order ───────────────────────────────────────────────
    id_cols = ["id_material", "descripcion", "id_sucursal", "descripcion_sucursal", "canal"]
    existing_id = [c for c in id_cols if c in df.columns]
    final_cols = existing_id + full_24m
    df = df[final_cols].copy()

    # Ensure month columns are numeric with 0 fill
    for mc in full_24m:
        df[mc] = pd.to_numeric(df[mc], errors="coerce").fillna(0)

    return df, None


# ==============================================================================
# 3. UI
# ==============================================================================
def render_forecast_generator(conn):
    st.markdown("## Generador de Forecast (Espejo/Cascada)")
    st.info("Desagrega un forecast mensual (SKU Nuevo) a nivel tienda usando la historia de un SKU Espejo o su jerarquia.")

    with st.expander("Configuracion de Historia Base", expanded=True):
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Fecha Inicio Historia", value=date(2024, 1, 1))
        with col2:
            end_date = st.date_input("Fecha Fin Historia", value=date.today())

    # ==================================================================
    # FORECAST GRANULAR (SKU x Sucursal x Mes)
    # ==================================================================
    with st.expander("Forecast Granular (SKU x Sucursal x Mes)", expanded=False):
        st.caption(
            "Sube el archivo de forecast por sucursal. "
            "Se usara como base para generar forecast de SKUs nuevos desde espejos."
        )
        fc_granular_file = st.file_uploader(
            "Cargar Forecast Granular (Excel/CSV)",
            type=["xlsx", "csv"],
            key="fc_granular_upload",
        )
        if fc_granular_file:
            try:
                if fc_granular_file.name.endswith(".csv"):
                    df_gran = pd.read_csv(fc_granular_file)
                else:
                    df_gran = pd.read_excel(fc_granular_file)

                g_cols = _detect_granular_columns(df_gran)
                st.session_state["fc_granular_df"] = df_gran
                st.session_state["fc_granular_cols"] = g_cols

                _n_skus = df_gran[g_cols["sku"]].nunique() if g_cols["sku"] else "?"
                _n_sucs = df_gran[g_cols["sucursal"]].nunique() if g_cols["sucursal"] else "?"
                _n_pers = len(g_cols["meses"])
                st.success(
                    f"Archivo cargado: **{_n_skus}** SKUs | "
                    f"**{_n_sucs}** Sucursales | **{_n_pers}** Periodos"
                )
                st.dataframe(df_gran.head(10), use_container_width=True, height=200)

                if not g_cols["sku"]:
                    st.warning("No se detecto columna de SKU (SKU_PRODUCTO, ID_MATERIAL, SKU)")
                if not g_cols["sucursal"]:
                    st.warning("No se detecto columna de Sucursal (ID_SUCURSAL, COD_BODEGA)")
            except Exception as e:
                st.error(f"Error leyendo archivo: {e}")

    # ==================================================================
    # BUSCADOR DE ESPEJO — Ranking de candidatos por similitud
    # ==================================================================
    with st.expander("Buscador de Espejo", expanded=False):
        st.caption(
            "Busca SKUs similares para usar como espejo. "
            "Pre-filtra por Sublinea + Marca, luego rankea por "
            "nombre, precio, modelo, procedencia y proveedor."
        )

        # Load cached data (zero cost if already cached)
        df_maestra_mf = cq.maestra(conn)
        df_maestra_mf = norm_cols(df_maestra_mf)
        df_maestra_mf = apply_pm_filter(df_maestra_mf)
        df_ventas_mp = cq.ventas_mensual_precio(conn)
        df_ventas_mp = norm_cols(df_ventas_mp)

        if df_maestra_mf.empty:
            st.warning("No se pudo cargar la maestra de productos.")
        else:
            mode_mf = st.radio(
                "Modo de busqueda",
                ["Seleccionar SKU existente", "Ingresar atributos manualmente"],
                horizontal=True,
                key="mirror_finder_mode",
            )

            target_attrs = {}

            if mode_mf == "Seleccionar SKU existente":
                # Build searchable list: "SKU - NOMBRE"
                sku_opts = (
                    df_maestra_mf["SKU_PRODUCTO"].astype(str)
                    + " - "
                    + df_maestra_mf["SKU_NOM_PRODUCTO"].fillna("").astype(str)
                ).tolist()

                selected_mf = st.selectbox(
                    "Buscar SKU",
                    options=[""] + sorted(sku_opts),
                    key="mirror_sku_select",
                    help="Escribe para filtrar. Formato: SKU - Nombre.",
                )

                if selected_mf and selected_mf != "":
                    sku_id = selected_mf.split(" - ")[0].strip()
                    row = df_maestra_mf[df_maestra_mf["SKU_PRODUCTO"] == sku_id]

                    if not row.empty:
                        r = row.iloc[0]
                        target_attrs = {
                            "SKU_PRODUCTO": r.get("SKU_PRODUCTO", ""),
                            "SKU_NOM_PRODUCTO": r.get("SKU_NOM_PRODUCTO", ""),
                            "AREA": r.get("AREA", ""),
                            "LINEA": r.get("LINEA", ""),
                            "SUBLINEA": r.get("SUBLINEA", ""),
                            "MARCA": r.get("MARCA", ""),
                            "MODELO": r.get("MODELO", ""),
                            "PROCEDENCIA": r.get("PROCEDENCIA", ""),
                            "PROVEEDOR": r.get("PROVEEDOR", ""),
                            "PRECIO_NETO": 0.0,
                        }

                        # Show auto-populated attributes
                        st.markdown(
                            f"**Sublinea:** {target_attrs['SUBLINEA']} | "
                            f"**Marca:** {target_attrs['MARCA']} | "
                            f"**Modelo:** {target_attrs['MODELO']}"
                        )

                        # Price override
                        cp1, cp2 = st.columns([3, 1])
                        with cp1:
                            precio_input = st.number_input(
                                "Precio referencia ($)",
                                value=0.0,
                                min_value=0.0,
                                step=1000.0,
                                key="mirror_precio_exist",
                                help="Dejar en 0 para ignorar precio en el scoring.",
                            )
                        with cp2:
                            con_iva = st.checkbox("Con IVA", value=False, key="mirror_iva_exist")

                        target_attrs["PRECIO_NETO"] = round(
                            precio_input / 1.19 if con_iva else precio_input, 0
                        )

            else:  # Manual mode
                c_l, c_r = st.columns(2)
                with c_l:
                    areas = sorted(df_maestra_mf["AREA"].dropna().unique().tolist()) if "AREA" in df_maestra_mf.columns else []
                    sel_area = st.selectbox("Area", [""] + areas, key="mf_area")

                    lineas_df = df_maestra_mf[df_maestra_mf["AREA"] == sel_area] if sel_area else df_maestra_mf
                    lineas = sorted(lineas_df["LINEA"].dropna().unique().tolist()) if "LINEA" in lineas_df.columns else []
                    sel_linea = st.selectbox("Linea", [""] + lineas, key="mf_linea")

                    sub_df = lineas_df[lineas_df["LINEA"] == sel_linea] if sel_linea else lineas_df
                    subs = sorted(sub_df["SUBLINEA"].dropna().unique().tolist()) if "SUBLINEA" in sub_df.columns else []
                    sel_sublinea = st.selectbox("Sublinea", [""] + subs, key="mf_sublinea")

                    marca_opts = sorted(df_maestra_mf["MARCA"].dropna().unique().tolist()) if "MARCA" in df_maestra_mf.columns else []
                    sel_marca = st.selectbox("Marca", [""] + marca_opts, key="mf_marca")

                with c_r:
                    sel_nombre = st.text_input("Nombre producto (para similitud)", key="mf_nombre")
                    sel_modelo = st.text_input("Modelo (opcional)", key="mf_modelo")
                    proc_opts = sorted(df_maestra_mf["PROCEDENCIA"].dropna().unique().tolist()) if "PROCEDENCIA" in df_maestra_mf.columns else []
                    sel_proc = st.selectbox("Procedencia (opcional)", [""] + proc_opts, key="mf_proc")
                    prov_opts = sorted(df_maestra_mf["PROVEEDOR"].dropna().unique().tolist()) if "PROVEEDOR" in df_maestra_mf.columns else []
                    sel_prov = st.selectbox("Proveedor (opcional)", [""] + prov_opts, key="mf_prov")

                cp1m, cp2m = st.columns([3, 1])
                with cp1m:
                    precio_manual = st.number_input(
                        "Precio referencia ($)", value=0.0, min_value=0.0,
                        step=1000.0, key="mirror_precio_manual",
                        help="Dejar en 0 para ignorar precio en el scoring.",
                    )
                with cp2m:
                    con_iva_m = st.checkbox("Con IVA", value=False, key="mirror_iva_manual")

                target_attrs = {
                    "SKU_PRODUCTO": None,
                    "SKU_NOM_PRODUCTO": sel_nombre,
                    "AREA": sel_area,
                    "LINEA": sel_linea,
                    "SUBLINEA": sel_sublinea,
                    "MARCA": sel_marca,
                    "MODELO": sel_modelo.strip().upper() if sel_modelo else "",
                    "PROCEDENCIA": sel_proc,
                    "PROVEEDOR": sel_prov,
                    "PRECIO_NETO": round(
                        precio_manual / 1.19 if con_iva_m else precio_manual, 0
                    ),
                }

            # ── SEARCH BUTTON ──
            if st.button("Buscar Espejos Similares", key="mirror_search_btn"):
                if not target_attrs.get("SUBLINEA") and not target_attrs.get("MARCA"):
                    st.warning("Selecciona al menos Sublinea o Marca para filtrar candidatos.")
                else:
                    with lottie_spinner("snowflake"):
                        df_candidates = _find_mirror_candidates(
                            df_maestra_mf, df_ventas_mp, target_attrs, top_n=20
                        )

                    if df_candidates.empty:
                        st.warning(
                            "No se encontraron candidatos con esos criterios. "
                            "Intenta relajar Sublinea o Marca."
                        )
                    else:
                        st.success(f"{len(df_candidates)} candidatos encontrados.")
                        st.session_state["mirror_candidates"] = df_candidates

                        # Auto-fill SKU Nuevo with the search SKU
                        _tgt_sku = target_attrs.get("SKU_PRODUCTO", "")
                        _tgt_desc = target_attrs.get("SKU_NOM_PRODUCTO", "")
                        if _tgt_sku:
                            st.session_state["mirror_gen_sku_nuevo"] = _tgt_sku
                        if _tgt_desc:
                            st.session_state["mirror_gen_desc"] = _tgt_desc

                        # Format price as "$25.450" (CLP convention: dot as thousands sep)
                        df_display = df_candidates.copy()
                        if "PRECIO_PROM_NETO" in df_display.columns:
                            df_display["PRECIO_PROM_NETO"] = df_display["PRECIO_PROM_NETO"].apply(
                                lambda v: f"${v:,.0f}".replace(",", ".") if pd.notna(v) and v > 0 else "$0"
                            )

                        st.dataframe(
                            df_display,
                            column_config={
                                "RANK": st.column_config.NumberColumn("#", width="small"),
                                "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="medium"),
                                "SKU_NOM_PRODUCTO": st.column_config.TextColumn("Nombre", width="large"),
                                "SUBLINEA": st.column_config.TextColumn("Sublinea"),
                                "MARCA": st.column_config.TextColumn("Marca"),
                                "MODELO": st.column_config.TextColumn("Modelo"),
                                "PROCEDENCIA": st.column_config.TextColumn("Proc."),
                                "PROVEEDOR": st.column_config.TextColumn("Proveedor"),
                                "PRECIO_PROM_NETO": st.column_config.TextColumn(
                                    "Precio Prom Neto",
                                ),
                                "VOL_MENSUAL_PROM": st.column_config.NumberColumn(
                                    "Vol Mensual Prom", format="%,.0f"
                                ),
                                "ULTIMA_VENTA": st.column_config.DateColumn(
                                    "Ult. Venta", format="DD/MM/YYYY"
                                ),
                                "SCORE": st.column_config.ProgressColumn(
                                    "Score", min_value=0, max_value=100, format="%.0f"
                                ),
                                "DETALLE_SCORE": st.column_config.TextColumn(
                                    "Detalle Score", width="large"
                                ),
                            },
                            use_container_width=True,
                            hide_index=True,
                        )

                        st.caption(
                            "Selecciona un espejo abajo para generar forecast, "
                            "o copia el SKU y usalo en tu archivo."
                        )

    # ==================================================================
    # GENERAR FORECAST DESDE ESPEJO — Seleccion directa + multiplicador
    # ==================================================================
    df_candidates = st.session_state.get("mirror_candidates")
    if df_candidates is not None and not df_candidates.empty:
        st.markdown("---")
        st.markdown("### Generar Forecast desde Espejo")
        st.caption(
            "Selecciona un espejo de los candidatos encontrados. "
            "El sistema generara el forecast por sucursal usando 3 metodos."
        )

        # -- Espejo selection --
        espejo_opts = [
            f"{r.SKU_PRODUCTO} - {r.SKU_NOM_PRODUCTO} (Score: {r.SCORE:.0f})"
            for _, r in df_candidates.iterrows()
        ]
        sel_espejo_str = st.selectbox(
            "Espejo seleccionado", espejo_opts, key="mirror_gen_espejo"
        )
        espejo_sku = sel_espejo_str.split(" - ")[0].strip() if sel_espejo_str else None

        if espejo_sku:
            # -- Detect availability in granular forecast --
            df_granular = st.session_state.get("fc_granular_df")
            g_cols = st.session_state.get("fc_granular_cols", {})
            espejo_in_fc = False
            if df_granular is not None and g_cols.get("sku"):
                col_sku_g = g_cols["sku"]
                espejo_in_fc = espejo_sku in df_granular[col_sku_g].astype(str).str.strip().str.upper().values

            # -- Future periods selection --
            st.markdown("**Periodos a proyectar**")
            today = date.today()
            default_periods = []
            for i in range(18):
                m = today.month + i
                y = today.year + (m - 1) // 12
                m = ((m - 1) % 12) + 1
                default_periods.append(date(y, m, 1))

            period_labels = [
                f"{_MONTH_NAMES_ES[p.month]}-{p.year}" for p in default_periods
            ]
            sel_period_labels = st.multiselect(
                "Meses futuros",
                options=period_labels,
                default=period_labels[:6],
                key="mirror_gen_periods",
            )
            sel_periods = [
                default_periods[i]
                for i, lbl in enumerate(period_labels)
                if lbl in sel_period_labels
            ]

            # -- Load history for preview --
            if st.button("Previsualizar Metodos", key="mirror_gen_preview_btn"):
                with st.spinner("Consultando historia del espejo..."):
                    df_hist = _load_mirror_history(conn, espejo_sku, start_date, end_date)
                    st.session_state["mirror_gen_hist"] = df_hist
                    st.session_state["mirror_gen_periods_sel"] = sel_periods

                    # Build 3 projections (aggregated totals for chart)
                    # Method A: Forecast Granular
                    proj_fc = None
                    if espejo_in_fc:
                        col_sku_g = g_cols["sku"]
                        col_suc_g = g_cols["sucursal"]
                        df_espejo_fc = df_granular[
                            df_granular[col_sku_g].astype(str).str.strip().str.upper() == espejo_sku
                        ].copy()
                        if not df_espejo_fc.empty:
                            mes_cols = g_cols["meses"]
                            # Aggregate to totals per month
                            totals = {}
                            for mc in mes_cols:
                                val = pd.to_numeric(df_espejo_fc[mc], errors="coerce").sum()
                                totals[str(mc)] = val
                            proj_fc = totals

                    # Method B1: Promedio mensual
                    df_prom = _build_projection_promedio(df_hist, sel_periods)
                    prom_totals = {}
                    if not df_prom.empty:
                        for per in sel_periods:
                            val = df_prom[df_prom["PERIODO"] == pd.Timestamp(per)]["VENTA_QTY"].sum()
                            prom_totals[f"{_MONTH_NAMES_ES[per.month]}-{per.year}"] = val

                    # Method B2: Año anterior
                    df_yoy = _build_projection_yoy(df_hist, sel_periods)
                    yoy_totals = {}
                    if not df_yoy.empty:
                        for per in sel_periods:
                            val = df_yoy[df_yoy["PERIODO"] == pd.Timestamp(per)]["VENTA_QTY"].sum()
                            yoy_totals[f"{_MONTH_NAMES_ES[per.month]}-{per.year}"] = val

                    # Store projections
                    st.session_state["mirror_gen_proj_fc"] = proj_fc
                    st.session_state["mirror_gen_proj_prom"] = df_prom
                    st.session_state["mirror_gen_proj_yoy"] = df_yoy

                    # -- Build comparative chart --
                    fig = go.Figure(layout=dorel_layout(
                        title=dict(text="Comparacion de Metodos de Proyeccion", font_size=14, x=0.5),
                        height=400,
                        xaxis=dict(title="Periodo"),
                        yaxis=dict(title="Unidades Totales"),
                        legend=dict(orientation="h", y=-0.2),
                    ))

                    # Historical bars
                    if not df_hist.empty:
                        hist_monthly = (
                            df_hist.groupby("PERIODO")["VENTA_QTY"].sum()
                            .sort_index().reset_index()
                        )
                        hist_labels = [
                            f"{_MONTH_NAMES_ES[p.month]}-{p.year}"
                            for p in hist_monthly["PERIODO"]
                        ]
                        fig.add_trace(go.Bar(
                            x=hist_labels, y=hist_monthly["VENTA_QTY"],
                            name="Venta Real Historica",
                            marker_color="#CCCCCC", opacity=0.6,
                        ))

                    # Future period labels
                    future_labels = [
                        f"{_MONTH_NAMES_ES[p.month]}-{p.year}" for p in sel_periods
                    ]

                    # Forecast Granular line
                    if proj_fc:
                        fc_vals = [proj_fc.get(str(mc), 0) for mc in g_cols.get("meses", [])]
                        fc_labels = [str(mc) for mc in g_cols.get("meses", [])]
                        if fc_vals:
                            fig.add_trace(go.Scatter(
                                x=fc_labels, y=fc_vals,
                                name="Forecast Granular",
                                mode="lines+markers",
                                line=dict(color="#2ECC71", width=3),
                                marker=dict(size=8),
                            ))

                    # Promedio line
                    if prom_totals:
                        fig.add_trace(go.Scatter(
                            x=future_labels,
                            y=[prom_totals.get(lbl, 0) for lbl in future_labels],
                            name="Promedio Mensual",
                            mode="lines+markers",
                            line=dict(color=COLORS.get("primary", "#2196F3"), width=3),
                            marker=dict(size=8),
                        ))

                    # YoY line
                    if yoy_totals:
                        fig.add_trace(go.Scatter(
                            x=future_labels,
                            y=[yoy_totals.get(lbl, 0) for lbl in future_labels],
                            name="Ano Anterior",
                            mode="lines+markers",
                            line=dict(color="#FF9800", width=3),
                            marker=dict(size=8),
                        ))

                    st.plotly_chart(fig, use_container_width=True)

                    if not espejo_in_fc:
                        st.caption("Espejo no encontrado en el forecast granular subido.")
                    if df_hist.empty:
                        st.warning("Sin historia de ventas para este espejo en el rango seleccionado.")

            # -- Method selection + Generation inputs --
            if st.session_state.get("mirror_gen_proj_prom") is not None or st.session_state.get("mirror_gen_proj_fc") is not None:
                st.markdown("---")

                # Method radio
                method_opts = []
                if espejo_in_fc and st.session_state.get("mirror_gen_proj_fc"):
                    method_opts.append("Forecast Granular")
                method_opts.extend(["Promedio Mensual", "Ano Anterior"])

                sel_method = st.radio(
                    "Metodo de proyeccion", method_opts,
                    horizontal=True, key="mirror_gen_method",
                )

                # SKU info — pre-fill with the SKU that was searched for mirrors
                _default_sku = st.session_state.get("mirror_gen_sku_nuevo", "")
                _default_desc = st.session_state.get("mirror_gen_desc", "")
                gc1, gc2 = st.columns(2)
                new_sku_id = gc1.text_input(
                    "SKU Nuevo (ID)", value=_default_sku, key="mirror_gen_sku_input",
                )
                new_sku_desc = gc2.text_input(
                    "Descripcion", value=_default_desc, key="mirror_gen_desc_input",
                )

                # Multiplier
                gc3, gc4 = st.columns([1, 2])
                multiplier = gc3.number_input(
                    "Multiplicador",
                    value=1.0, min_value=0.01, max_value=10.0, step=0.1,
                    key="mirror_gen_mult",
                    help="0.5 = 50% del espejo | 1.0 = igual | 1.2 = +20%",
                )

                # Channel selection
                available_channels = list(_CHANNEL_MAP.keys())
                sel_channels = gc4.multiselect(
                    "Aplicar factor a canales",
                    options=available_channels,
                    default=available_channels,
                    key="mirror_gen_channels",
                )
                channels_with_factor = {_CHANNEL_MAP[c] for c in sel_channels}

                # Generate button
                if st.button("Generar Forecast desde Espejo", type="primary", key="mirror_gen_go"):
                    if not new_sku_id.strip():
                        st.warning("Ingresa el SKU del producto nuevo.")
                    else:
                        sel_periods_stored = st.session_state.get(
                            "mirror_gen_periods_sel", sel_periods
                        )

                        with st.spinner("Generando forecast..."):
                            df_base_wide = None
                            cols_mes_out = []
                            err = None

                            if sel_method == "Forecast Granular":
                                # Build base from granular file
                                col_sku_g = g_cols["sku"]
                                col_suc_g = g_cols["sucursal"]
                                col_canal_g = g_cols.get("canal")
                                col_desc_suc_g = g_cols.get("desc_suc")
                                mes_cols = g_cols["meses"]

                                df_espejo_fc = df_granular[
                                    df_granular[col_sku_g].astype(str).str.strip().str.upper() == espejo_sku
                                ].copy()

                                if df_espejo_fc.empty:
                                    err = "Espejo no encontrado en forecast granular."
                                else:
                                    # Rename to standard columns
                                    rename_map = {}
                                    if col_suc_g:
                                        rename_map[col_suc_g] = "id_sucursal"
                                    if col_canal_g:
                                        rename_map[col_canal_g] = "canal"
                                    if col_desc_suc_g:
                                        rename_map[col_desc_suc_g] = "descripcion_sucursal"

                                    df_base_wide = df_espejo_fc.rename(columns=rename_map)

                                    # Ensure canal column
                                    if "canal" not in df_base_wide.columns:
                                        df_base_wide["canal"] = "TIENDA"
                                    if "descripcion_sucursal" not in df_base_wide.columns:
                                        df_base_wide["descripcion_sucursal"] = ""

                                    # Drop SKU and extra cols, keep only standard + months
                                    keep = ["id_sucursal", "descripcion_sucursal", "canal"] + mes_cols
                                    df_base_wide = df_base_wide[
                                        [c for c in keep if c in df_base_wide.columns]
                                    ].copy()
                                    cols_mes_out = mes_cols

                            else:
                                # Build from historical projection
                                if sel_method == "Promedio Mensual":
                                    df_proj = st.session_state.get("mirror_gen_proj_prom")
                                else:  # Ano Anterior
                                    df_proj = st.session_state.get("mirror_gen_proj_yoy")

                                if df_proj is None or df_proj.empty:
                                    err = "Sin datos de proyeccion disponibles."
                                else:
                                    # Filter to selected periods
                                    df_proj = df_proj.copy()
                                    valid_ts = [pd.Timestamp(p) for p in sel_periods_stored]
                                    df_proj = df_proj[
                                        df_proj["PERIODO"].isin(valid_ts)
                                    ].copy()

                                    if df_proj.empty:
                                        err = "Sin datos para los periodos seleccionados."
                                    else:
                                        # Create period labels for columns
                                        df_proj["MES_LABEL"] = df_proj["PERIODO"].apply(
                                            lambda p: f"{_MONTH_NAMES_ES[p.month]}-{p.year}"
                                        )

                                        # Ensure canal exists
                                        if "CANAL" not in df_proj.columns:
                                            df_proj["CANAL"] = "TIENDA"
                                        if "DESCRIPCION_SUCURSAL" not in df_proj.columns:
                                            df_proj["DESCRIPCION_SUCURSAL"] = ""

                                        # Pivot to wide format
                                        idx_cols = ["ID_SUCURSAL", "DESCRIPCION_SUCURSAL", "CANAL"]
                                        df_base_wide = df_proj.pivot_table(
                                            index=idx_cols,
                                            columns="MES_LABEL",
                                            values="VENTA_QTY",
                                            aggfunc="sum",
                                            fill_value=0,
                                        ).reset_index()

                                        # Rename to lowercase standard
                                        df_base_wide = df_base_wide.rename(columns={
                                            "ID_SUCURSAL": "id_sucursal",
                                            "DESCRIPCION_SUCURSAL": "descripcion_sucursal",
                                            "CANAL": "canal",
                                        })

                                        cols_mes_out = [
                                            f"{_MONTH_NAMES_ES[p.month]}-{p.year}"
                                            for p in sel_periods_stored
                                            if f"{_MONTH_NAMES_ES[p.month]}-{p.year}" in df_base_wide.columns
                                        ]

                            if err:
                                st.error(err)
                            elif df_base_wide is not None and not df_base_wide.empty:
                                df_result, gen_err = _generate_from_mirror(
                                    df_base_wide,
                                    new_sku_id.strip(),
                                    new_sku_desc.strip() or "SIN_DESCRIPCION",
                                    multiplier,
                                    channels_with_factor,
                                    cols_mes_out,
                                )

                                if gen_err:
                                    st.error(gen_err)
                                else:
                                    # Summary — use the 24-month columns from result
                                    _cols_24m = _build_24m_columns()
                                    total_espejo = sum(
                                        pd.to_numeric(df_base_wide[c], errors="coerce").sum()
                                        for c in cols_mes_out if c in df_base_wide.columns
                                    )
                                    total_nuevo = sum(
                                        pd.to_numeric(df_result[c], errors="coerce").sum()
                                        for c in _cols_24m if c in df_result.columns
                                    )
                                    delta_pct = (
                                        (total_nuevo / total_espejo - 1) * 100
                                        if total_espejo > 0 else 0
                                    )

                                    sc1, sc2, sc3 = st.columns(3)
                                    sc1.metric("Total Espejo", f"{total_espejo:,.0f} und")
                                    sc2.metric("Total SKU Nuevo", f"{total_nuevo:,.0f} und")
                                    sc3.metric("Delta", f"{delta_pct:+.1f}%")

                                    st.success(
                                        f"Forecast generado: {len(df_result):,} filas | "
                                        f"Metodo: {sel_method} | Factor: {multiplier}x"
                                    )
                                    st.dataframe(df_result.head(100), use_container_width=True)

                                    buffer = io.BytesIO()
                                    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                                        df_result.to_excel(writer, index=False)

                                    st.download_button(
                                        label="Descargar Excel",
                                        data=buffer.getvalue(),
                                        file_name=(
                                            f"forecast_espejo_{new_sku_id.strip()}_"
                                            f"{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.xlsx"
                                        ),
                                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                    )
                            else:
                                st.error("No se pudo construir la base del forecast.")

    # ==================================================================
    # MODO MASIVO — Buscar espejos y generar forecast para múltiples SKUs
    # ==================================================================
    st.markdown("---")
    st.markdown("### Modo Masivo — Forecast para multiples SKUs")
    st.caption(
        "Pega una lista de SKUs nuevos (uno por linea) para buscar automaticamente "
        "el mejor espejo para cada uno y generar forecast en lote."
    )

    mass_input = st.text_area(
        "SKUs sin forecast (uno por linea)",
        height=150,
        key="mass_sku_input",
        placeholder="013910C11GR\n013920C12BL\n014100A01RS\n...",
    )

    sku_list_mass = []
    if mass_input:
        sku_list_mass = list(dict.fromkeys(
            s.strip().upper() for s in mass_input.strip().split("\n") if s.strip()
        ))

    if sku_list_mass:
        st.info(f"**{len(sku_list_mass)} SKUs** cargados (sin duplicados)")

        # Reuse same cached maestra + ventas
        df_maestra_mass = cq.maestra(conn)
        df_maestra_mass = norm_cols(df_maestra_mass)
        df_ventas_mass = cq.ventas_mensual_precio(conn)
        df_ventas_mass = norm_cols(df_ventas_mass)

        if st.button("🔍 Buscar Espejos Automaticamente", key="mass_search_btn"):
            results_mass = []
            progress = st.progress(0, text="Buscando espejos...")

            for i, sku in enumerate(sku_list_mass):
                row = df_maestra_mass[df_maestra_mass["SKU_PRODUCTO"] == sku]
                if row.empty:
                    results_mass.append({
                        "SKU_NUEVO": sku, "NOMBRE": "No encontrado en maestra",
                        "SUBLINEA": "", "MARCA": "",
                        "ESPEJO": "", "ESPEJO_NOMBRE": "", "SCORE": 0,
                    })
                    continue

                r = row.iloc[0]
                attrs = {
                    "SKU_PRODUCTO": sku,
                    "SKU_NOM_PRODUCTO": r.get("SKU_NOM_PRODUCTO", ""),
                    "SUBLINEA": r.get("SUBLINEA", ""),
                    "MARCA": r.get("MARCA", ""),
                    "MODELO": r.get("MODELO", ""),
                    "PROCEDENCIA": r.get("PROCEDENCIA", ""),
                    "PROVEEDOR": r.get("PROVEEDOR", ""),
                    "PRECIO_NETO": 0.0,
                }
                candidates = _find_mirror_candidates(
                    df_maestra_mass, df_ventas_mass, attrs, top_n=3
                )
                if candidates.empty:
                    results_mass.append({
                        "SKU_NUEVO": sku,
                        "NOMBRE": r.get("SKU_NOM_PRODUCTO", ""),
                        "SUBLINEA": str(r.get("SUBLINEA", "")),
                        "MARCA": str(r.get("MARCA", "")),
                        "ESPEJO": "", "ESPEJO_NOMBRE": "", "SCORE": 0,
                    })
                else:
                    best = candidates.iloc[0]
                    results_mass.append({
                        "SKU_NUEVO": sku,
                        "NOMBRE": r.get("SKU_NOM_PRODUCTO", ""),
                        "SUBLINEA": str(r.get("SUBLINEA", "")),
                        "MARCA": str(r.get("MARCA", "")),
                        "ESPEJO": best.get("SKU_PRODUCTO", ""),
                        "ESPEJO_NOMBRE": best.get("SKU_NOM_PRODUCTO", ""),
                        "SCORE": best.get("SCORE", 0),
                    })
                progress.progress((i + 1) / len(sku_list_mass))

            progress.empty()
            df_mass_res = pd.DataFrame(results_mass)
            st.session_state["mass_mirror_results"] = df_mass_res

    # -- Show results + generation controls --
    df_mass_res = st.session_state.get("mass_mirror_results")
    if df_mass_res is not None and not df_mass_res.empty:
        n_found = (df_mass_res["ESPEJO"] != "").sum()
        n_missing = (df_mass_res["ESPEJO"] == "").sum()

        mc1, mc2, mc3 = st.columns(3)
        mc1.metric("SKUs Procesados", len(df_mass_res))
        mc2.metric("Con Espejo", int(n_found))
        mc3.metric("Sin Espejo", int(n_missing))

        st.dataframe(
            df_mass_res,
            column_config={
                "SKU_NUEVO": st.column_config.TextColumn("SKU Nuevo", width="medium"),
                "NOMBRE": st.column_config.TextColumn("Nombre", width="large"),
                "SUBLINEA": st.column_config.TextColumn("Sublinea"),
                "MARCA": st.column_config.TextColumn("Marca"),
                "ESPEJO": st.column_config.TextColumn("Espejo Sugerido", width="medium"),
                "ESPEJO_NOMBRE": st.column_config.TextColumn("Nombre Espejo", width="large"),
                "SCORE": st.column_config.ProgressColumn(
                    "Score", min_value=0, max_value=100, format="%.0f"
                ),
            },
            use_container_width=True,
            hide_index=True,
        )

        # Download mirror mapping as Excel
        buf_map = io.BytesIO()
        with pd.ExcelWriter(buf_map, engine="openpyxl") as writer:
            df_mass_res.to_excel(writer, index=False, sheet_name="Mapeo Espejos")
        st.download_button(
            label="📋 Descargar Mapeo Espejos (Excel)",
            data=buf_map.getvalue(),
            file_name=f"mapeo_espejos_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="mass_download_mapping",
        )

        # -- Generation controls --
        if n_found > 0:
            st.markdown("---")
            st.markdown("**Configuracion de generacion masiva**")

            mg1, mg2 = st.columns(2)
            with mg1:
                mass_method = st.radio(
                    "Metodo", ["Promedio Mensual", "Ano Anterior"],
                    horizontal=True, key="mass_method",
                )
            with mg2:
                mass_mult = st.number_input(
                    "Multiplicador global", value=1.0, min_value=0.01,
                    max_value=10.0, step=0.1, key="mass_mult",
                    help="0.5 = 50% del espejo | 1.0 = igual | 1.2 = +20%",
                )

            # Period selection
            today_m = date.today()
            default_periods_m = []
            for i in range(12):
                _mm = today_m.month + i
                _yy = today_m.year + (_mm - 1) // 12
                _mm = ((_mm - 1) % 12) + 1
                default_periods_m.append(date(_yy, _mm, 1))

            period_labels_m = [
                f"{_MONTH_NAMES_ES[p.month]}-{p.year}" for p in default_periods_m
            ]
            mass_sel_labels = st.multiselect(
                "Periodos a proyectar",
                options=period_labels_m,
                default=period_labels_m[:6],
                key="mass_periods",
            )
            mass_sel_periods = [
                default_periods_m[i]
                for i, lbl in enumerate(period_labels_m)
                if lbl in mass_sel_labels
            ]

            if st.button("🚀 Generar Forecast Masivo", type="primary", key="mass_generate_btn"):
                df_with_espejo = df_mass_res[df_mass_res["ESPEJO"] != ""].copy()
                unique_espejos = df_with_espejo["ESPEJO"].unique().tolist()

                with st.spinner(f"Cargando historia para {len(unique_espejos)} espejos unicos..."):
                    df_all_hist = _load_mirror_history_batch(
                        conn, unique_espejos, start_date, end_date
                    )

                all_fc_results = []
                progress_gen = st.progress(0, text="Generando forecasts...")

                for i, (_, row_m) in enumerate(df_with_espejo.iterrows()):
                    espejo = row_m["ESPEJO"]
                    sku_nuevo = row_m["SKU_NUEVO"]
                    nombre = row_m.get("NOMBRE", "")

                    # Filter history for this mirror
                    if not df_all_hist.empty:
                        df_hist_e = df_all_hist[df_all_hist["ESPEJO"] == espejo].copy()
                    else:
                        df_hist_e = pd.DataFrame()

                    if df_hist_e.empty:
                        continue

                    # Build projection
                    if mass_method == "Promedio Mensual":
                        df_proj_m = _build_projection_promedio(df_hist_e, mass_sel_periods)
                    else:
                        df_proj_m = _build_projection_yoy(df_hist_e, mass_sel_periods)

                    if df_proj_m is None or df_proj_m.empty:
                        continue

                    # Create period labels
                    df_proj_m["MES_LABEL"] = df_proj_m["PERIODO"].apply(
                        lambda p: f"{_MONTH_NAMES_ES[p.month]}-{p.year}"
                    )

                    if "CANAL" not in df_proj_m.columns:
                        df_proj_m["CANAL"] = "TIENDA"
                    if "DESCRIPCION_SUCURSAL" not in df_proj_m.columns:
                        df_proj_m["DESCRIPCION_SUCURSAL"] = ""

                    # Pivot to wide
                    idx_cols_m = ["ID_SUCURSAL", "DESCRIPCION_SUCURSAL", "CANAL"]
                    df_wide_m = df_proj_m.pivot_table(
                        index=idx_cols_m,
                        columns="MES_LABEL",
                        values="VENTA_QTY",
                        aggfunc="sum",
                        fill_value=0,
                    ).reset_index()

                    df_wide_m = df_wide_m.rename(columns={
                        "ID_SUCURSAL": "id_sucursal",
                        "DESCRIPCION_SUCURSAL": "descripcion_sucursal",
                        "CANAL": "canal",
                    })

                    cols_mes_m = [
                        f"{_MONTH_NAMES_ES[p.month]}-{p.year}"
                        for p in mass_sel_periods
                        if f"{_MONTH_NAMES_ES[p.month]}-{p.year}" in df_wide_m.columns
                    ]

                    # Apply multiplier and set SKU
                    df_fc_out, fc_err = _generate_from_mirror(
                        df_wide_m,
                        sku_nuevo,
                        nombre or "SIN_DESCRIPCION",
                        mass_mult,
                        {"TIENDA", "ETAIL", "MAYOR"},
                        cols_mes_m,
                    )

                    if df_fc_out is not None and not df_fc_out.empty:
                        all_fc_results.append(df_fc_out)

                    progress_gen.progress((i + 1) / len(df_with_espejo))

                progress_gen.empty()

                if all_fc_results:
                    df_final_mass = pd.concat(all_fc_results, ignore_index=True)
                    st.session_state["mass_forecast_result"] = df_final_mass

                    # Summary
                    total_und = sum(
                        pd.to_numeric(df_final_mass[c], errors="coerce").sum()
                        for c in df_final_mass.columns
                        if c not in ("id_material", "descripcion", "id_sucursal",
                                     "descripcion_sucursal", "canal")
                    )

                    st.success(
                        f"Forecast generado para **{len(all_fc_results)}/{len(df_with_espejo)}** SKUs | "
                        f"**{len(df_final_mass):,}** filas | **{total_und:,.0f}** und totales | "
                        f"Metodo: {mass_method} | Factor: {mass_mult}x"
                    )
                else:
                    st.warning("No se pudo generar forecast para ningun SKU. "
                               "Verifica que los espejos tengan historia de ventas.")

    # -- Show final result + download --
    df_final_mass = st.session_state.get("mass_forecast_result")
    if df_final_mass is not None and not df_final_mass.empty:
        st.dataframe(df_final_mass.head(200), use_container_width=True)

        buf_fc = io.BytesIO()
        with pd.ExcelWriter(buf_fc, engine="openpyxl") as writer:
            df_final_mass.to_excel(writer, index=False, sheet_name="Forecast")
            # Also add the mirror mapping as a second sheet
            _map = st.session_state.get("mass_mirror_results")
            if _map is not None:
                _map.to_excel(writer, index=False, sheet_name="Mapeo Espejos")

        st.download_button(
            label="📥 Descargar Forecast Masivo (Excel)",
            data=buf_fc.getvalue(),
            file_name=f"forecast_masivo_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="mass_download_forecast",
        )

    # ==================================================================
    # DISTRIBUCION POR CASCADA (flujo existente con archivo Excel)
    # ==================================================================
    st.markdown("---")
    st.markdown("### Distribucion por Cascada (archivo manual)")

    uploaded_file = st.file_uploader(
        "Cargar Archivo Excel (Columnas: SKU_NUEVO, ESPEJO, [Meses...])", type=["xlsx"]
    )

    if uploaded_file:
        try:
            df_input = pd.read_excel(uploaded_file)
            st.success(f"Archivo cargado: {len(df_input)} registros")
            st.dataframe(df_input.head())

            if st.button("Analizar Calidad Espejos"):
                with lottie_spinner("snowflake"):
                    df_quality, df_raw, err = get_mirror_quality(df_input, conn, start_date, end_date)

                    if err:
                        st.error(err)
                    else:
                        st.markdown("### Calidad de Historia")

                        st.markdown("#### Evolucion Mensual (Dias con Venta)")
                        if not df_raw.empty:
                            # Heatmap: ESPEJO x PERIODO → DIAS_CON_VENTA
                            espejos_list = sorted(df_raw["ESPEJO"].unique())
                            periodos_list = sorted(df_raw["PERIODO"].unique())

                            heatmap_pivot = df_raw.pivot_table(
                                index="ESPEJO", columns="PERIODO",
                                values="DIAS_CON_VENTA", aggfunc="sum", fill_value=0,
                            )
                            heatmap_pivot = heatmap_pivot.reindex(
                                index=espejos_list, columns=periodos_list, fill_value=0
                            )

                            fig_hm = go.Figure(layout=dorel_layout(
                                title=dict(text="Mapa de Calor: Intensidad de Venta", font_size=14, x=0.5),
                                height=max(300, len(espejos_list) * 30 + 100),
                                xaxis=dict(title="Mes", tickangle=-45),
                                yaxis=dict(title="SKU Espejo", autorange="reversed"),
                            ))
                            fig_hm.add_trace(go.Heatmap(
                                z=heatmap_pivot.values,
                                x=periodos_list,
                                y=espejos_list,
                                colorscale="Blues",
                                colorbar=dict(title="Dias"),
                                hovertemplate=(
                                    "Espejo: %{y}<br>Periodo: %{x}<br>"
                                    "Dias con Venta: %{z}<extra></extra>"
                                ),
                            ))
                            st.plotly_chart(fig_hm, use_container_width=True)

                        st.markdown("#### Resumen Trimestral")
                        df_q = get_quarterly_quality(df_raw)
                        st.dataframe(df_q, use_container_width=True)

                        st.markdown("### Sugerencias Ajustadas (Optimizacion)")
                        st.info("El sistema sugiere estos periodos donde la historia es mas continua.")

                        df_recs = get_period_recommendations(df_raw)
                        if not df_recs.empty:
                            st.dataframe(
                                df_recs,
                                column_config={
                                    "ESPEJO": st.column_config.TextColumn("SKU Espejo", width="medium"),
                                    "INICIO_SUGERIDO": st.column_config.DateColumn("Inicio"),
                                    "FIN_SUGERIDO": st.column_config.DateColumn("Fin"),
                                    "COMENTARIO": st.column_config.TextColumn("Analisis", width="large"),
                                },
                                use_container_width=True,
                            )
                            st.session_state["mirror_recommendations"] = df_recs
                        else:
                            st.warning("No se pudo generar sugerencias (sin data suficiente).")
                            st.session_state["mirror_recommendations"] = None

                        st.session_state["mirror_quality_checked"] = True

            if st.session_state.get("mirror_quality_checked"):
                st.markdown("---")

                use_optimization = st.checkbox(
                    "Usar rangos optimizados por SKU (Recomendado)",
                    value=True,
                    help="Si se marca, cada SKU usara su rango sugerido. Si no, todos usaran el rango global.",
                )

                if st.button("Generar Distribucion", type="primary"):
                    dates_map = None
                    if use_optimization and st.session_state.get("mirror_recommendations") is not None:
                        df_recs = st.session_state["mirror_recommendations"]
                        valid_recs = df_recs[df_recs["INICIO_SUGERIDO"] != "-"]
                        dates_map = {}
                        for _, row in valid_recs.iterrows():
                            dates_map[row["ESPEJO"]] = (row["INICIO_SUGERIDO"], row["FIN_SUGERIDO"])
                        st.caption(f"Aplicando optimizacion para {len(dates_map)} SKUs.")

                    with st.spinner("Procesando cascada..."):
                        df_final, err = process_forecast_mirror(
                            df_input, conn, start_date, end_date, dates_map=dates_map
                        )

                        if err:
                            st.error(err)
                        else:
                            st.success(f"Proceso completado. Filas generadas: {len(df_final):,}")
                            st.dataframe(df_final.head(100))

                            buffer = io.BytesIO()
                            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                                df_final.to_excel(writer, index=False)

                            st.download_button(
                                label="Descargar Excel",
                                data=buffer.getvalue(),
                                file_name=f"forecast_distribuido_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            )

        except Exception as e:
            st.error(f"Error leyendo archivo: {e}")
