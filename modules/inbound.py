"""In-Out bound — Flujo de ingresos (Inbound) por mes, categoría y tamaño.

Fuente de datos:
  - POs (ft_compras vía QUERY_PLAN_COMPRAS): unidades, costo, ETA
  - Maestra (vw_producto): AREA, TIPO_ALMACEN, PROCEDENCIA
  - Dimensiones (OT_PRODUCTO_UNIMAR): M3/unidad, Unidades×Pallet

BPA: productos con TIPO_ALMACEN = 'BPA' (reclasificados como AREA = "BPA").
Secciones: NIVEL COMPAÑÍA (4 tablas) + Flujo Almacenamiento (4 tablas).
Descarga: un solo Excel con todas las tablas como hojas.
"""

from __future__ import annotations

import re
from pathlib import Path

import io
from datetime import datetime

import numpy as np
import pandas as pd
import streamlit as st

from config import COLORS
from db.cache import cached_query as cq, TTL_DIARIO
from db.queries import QUERY_PLAN_COMPRAS
from utils.file_persistence import load_projection_results, get_saved_file_path
from utils.filters import norm_cols, human_format
from utils.ui_animations import lottie_spinner
from utils.ui_components import page_header

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

MESES_ES = {
    1: "ENE", 2: "FEB", 3: "MAR", 4: "ABR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AGO", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DIC",
}
MESES_COLS = [MESES_ES[m] for m in range(1, 13)]

# Filas para NIVEL COMPAÑÍA (Und / Pallets / Alm)
NIVEL_ROWS = [
    ("BEBE",         "GRANDE"),
    ("BEBE",         "PEQUEÑO"),
    ("HOGAR",        "PEQUEÑO"),
    ("JUGUETERIA",   "GRANDE"),
    ("JUGUETERIA",   "PEQUEÑO"),
    ("TIEMPO LIBRE", "GRANDE"),
    ("TIEMPO LIBRE", "PEQUEÑO"),
    ("VESTUARIO",    "PEQUEÑO"),
    ("BPA",          "GRANDE"),
    ("BPA",          "PEQUEÑO"),
]

# Filas para contenedores (Philips Avent → BPA, sin fila separada)
CNTR_ROWS = NIVEL_ROWS

_M3_UMBRAL_GRANDE    = 0.3
_PALLETS_POR_CNTR    = 20          # pallets por contenedor 40HC estándar
_TIPO_ALM_PATH   = Path(__file__).resolve().parent.parent / "data" / "tipo_almacen.csv"
_LEYENDAS_PATH   = Path(__file__).resolve().parent.parent / "data" / "inputs" / "leyendas.xlsx"
_PHILIPS_KEYWORDS = {"philips", "avent"}

# Mapeo GRUPO → AREA (para leyendas)
_GRUPO_TO_AREA = {
    "BEBE":                          "BEBE",
    "VALE LISTA DE BEBE":            "BEBE",
    "BEBE - DSCTOS":                 "BEBE",
    "JUGUETERIA":                    "JUGUETERIA",
    "JUGUETERIA - DSCTOS":           "JUGUETERIA",
    "TIEMPO LIBRE":                  "TIEMPO LIBRE",
    "HOGAR":                         "HOGAR",
    "VESTUARIO":                     "VESTUARIO",
}

# ---------------------------------------------------------------------------
# Data loading (cached)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _load_pos(_conn_id, _conn=None) -> pd.DataFrame:
    df = pd.read_sql(QUERY_PLAN_COMPRAS, _conn)
    return norm_cols(df)


def _load_data(conn):
    _cid = id(conn)
    with lottie_spinner("snowflake"):
        df_pos  = _load_pos(_cid, _conn=conn)
        maestra = cq.maestra(conn)
        dims    = cq.contenedor_dims(conn)
    return df_pos, maestra, dims


def _load_proy(dims: pd.DataFrame | None = None) -> pd.DataFrame:
    """Carga compras proyectadas desde la última Proyección de Stock corrida en esta sesión."""
    df_proy_raw, _ = load_projection_results()
    if df_proy_raw is None or df_proy_raw.empty:
        return pd.DataFrame()

    df_pq = norm_cols(df_proy_raw)

    fc_col = "FORECAST_COMPRA" if "FORECAST_COMPRA" in df_pq.columns else None
    if fc_col is None:
        return pd.DataFrame()
    df_pq[fc_col] = pd.to_numeric(df_pq[fc_col], errors="coerce").fillna(0)
    df_pq = df_pq[df_pq[fc_col] > 0].copy()
    if df_pq.empty:
        return pd.DataFrame()

    # PERIODO_ETA desde columna PERIODO
    _per = df_pq["PERIODO"] if "PERIODO" in df_pq.columns else pd.Series(dtype="datetime64[ns]")
    df_pq["PERIODO_ETA"] = pd.to_datetime(_per, errors="coerce").values

    # Columnas categóricas ya vienen en el parquet
    for col in ["AREA", "LINEA", "MARCA", "PROCEDENCIA"]:
        if col in df_pq.columns:
            df_pq[col] = df_pq[col].fillna("").astype(str).str.upper().str.strip()
        else:
            df_pq[col] = ""

    # BPA desde CSV tipo_almacen
    sku_col = next((c for c in df_pq.columns if c.upper().startswith("SKU_PROD")), None)
    if sku_col and _TIPO_ALM_PATH.exists():
        _ta = pd.read_csv(_TIPO_ALM_PATH, dtype=str)
        _ta.columns = [_ta.columns[0], "TIPO_ALMACEN"]
        _ta = _ta.rename(columns={_ta.columns[0]: sku_col})
        _ta[sku_col] = _ta[sku_col].str.strip()
        _ta["TIPO_ALMACEN"] = _ta["TIPO_ALMACEN"].str.strip().str.upper()
        df_pq = df_pq.merge(_ta[[sku_col, "TIPO_ALMACEN"]], on=sku_col, how="left")
    if "TIPO_ALMACEN" not in df_pq.columns:
        df_pq["TIPO_ALMACEN"] = ""
    df_pq["TIPO_ALMACEN"] = df_pq["TIPO_ALMACEN"].fillna("").str.upper()
    df_pq.loc[df_pq["TIPO_ALMACEN"] == "BPA", "AREA"] = "BPA"

    # Join con dims para M3_UNIDAD y UNIDADES_X_PALLET (pallets y tamaño)
    df_pq["M3_UNIDAD"] = 0.0
    df_pq["UNIDADES_X_PALLET"] = 0.0
    if dims is not None and not dims.empty and sku_col:
        dims_norm = norm_cols(dims.copy())
        dim_sku = next((c for c in dims_norm.columns if c.upper().startswith("COD_PROD")), None)
        if dim_sku and "M3_UNIDAD" in dims_norm.columns and "UNIDADES_X_PALLET" in dims_norm.columns:
            dims_sub = dims_norm[[dim_sku, "M3_UNIDAD", "UNIDADES_X_PALLET"]].copy()
            dims_sub = dims_sub.rename(columns={dim_sku: sku_col})
            dims_sub[sku_col] = dims_sub[sku_col].astype(str).str.strip()
            df_pq[sku_col] = df_pq[sku_col].astype(str).str.strip()
            df_pq = df_pq.merge(dims_sub, on=sku_col, how="left", suffixes=("", "_dims"))
            for col in ["M3_UNIDAD", "UNIDADES_X_PALLET"]:
                if col + "_dims" in df_pq.columns:
                    df_pq[col] = df_pq[col + "_dims"].combine_first(df_pq[col])
                    df_pq.drop(columns=[col + "_dims"], inplace=True)
    for col in ["M3_UNIDAD", "UNIDADES_X_PALLET"]:
        df_pq[col] = pd.to_numeric(df_pq[col], errors="coerce").fillna(0.0)

    # Costo desde parquet: COSTO_UNITARIO × FORECAST_COMPRA
    costo_unit = pd.to_numeric(
        df_pq["COSTO_UNITARIO"] if "COSTO_UNITARIO" in df_pq.columns else 0,
        errors="coerce",
    ).fillna(0)

    # TAMAÑO desde leyendas (más preciso que dims)
    df_pq["QTY_PENDIENTE"] = df_pq[fc_col]
    ley = _load_leyendas()
    if not ley.empty and sku_col:
        ley_t = ley[["SKU", "TAMAÑO"]].rename(columns={"SKU": sku_col})
        ley_t[sku_col] = ley_t[sku_col].astype(str).str.strip()
        df_pq = df_pq.merge(ley_t, on=sku_col, how="left")
    if "TAMAÑO" not in df_pq.columns:
        df_pq["TAMAÑO"] = df_pq.apply(
            lambda r: _classify_tamaño(r["M3_UNIDAD"], r["UNIDADES_X_PALLET"]), axis=1
        )
    else:
        df_pq["TAMAÑO"] = df_pq["TAMAÑO"].fillna(
            df_pq.apply(lambda r: _classify_tamaño(r["M3_UNIDAD"], r["UNIDADES_X_PALLET"]), axis=1)
        )
    df_pq["ES_PHILIPS"] = df_pq["MARCA"].apply(_is_philips)
    df_pq.loc[df_pq["ES_PHILIPS"], "AREA"] = "BPA"   # Philips Avent → BPA
    df_pq["PALLETS"] = np.where(
        df_pq["UNIDADES_X_PALLET"] > 0,
        df_pq["QTY_PENDIENTE"] / df_pq["UNIDADES_X_PALLET"],
        0.0,
    )
    df_pq["MT3"]      = df_pq["QTY_PENDIENTE"] * df_pq["M3_UNIDAD"]
    df_pq["N_PO"]     = None
    df_pq["STATUS_PO"]  = "Compra Proy"
    df_pq["COSTO_PEN"]  = df_pq["QTY_PENDIENTE"] * costo_unit
    return df_pq



# ---------------------------------------------------------------------------
# Outbound loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=TTL_DIARIO, show_spinner=False)
def _load_leyendas() -> pd.DataFrame:
    """Carga maestra de productos (leyendas) con AREA, TAMAÑO, TIPO_ALMACEN por SKU.

    Columnas de salida: SKU, AREA, LINEA, MARCA, PROCEDENCIA, TIPO_ALMACEN, TAMAÑO
    """
    if not _LEYENDAS_PATH.exists():
        return pd.DataFrame()

    df = pd.read_excel(_LEYENDAS_PATH, dtype=str)

    # SKU join key
    df["SKU"] = df["COD_PRODUCTO"].fillna("").str.strip()

    # AREA desde GRUPO
    grupo = df["GRUPO"].fillna("").str.strip().str.upper()
    df["AREA"] = grupo.map({k.upper(): v for k, v in _GRUPO_TO_AREA.items()}).fillna("")

    # TIPO_ALMACEN — BPA override en AREA
    ta_col = "TIPO_ALMACEN" if "TIPO_ALMACEN" in df.columns else None
    if ta_col:
        df["TIPO_ALMACEN"] = df[ta_col].fillna("").str.strip().str.upper()
    else:
        df["TIPO_ALMACEN"] = ""
    df.loc[df["TIPO_ALMACEN"] == "BPA", "AREA"] = "BPA"

    # TAMAÑO: col con encoding latino → normalizar a GRANDE / PEQUEÑO
    tamano_col = next((c for c in df.columns if "TAMA" in c.upper()), None)
    if tamano_col:
        t_vals = df[tamano_col].fillna("").astype(str).str.upper()
        df["TAMAÑO"] = t_vals.apply(lambda x: "GRANDE" if "GRAND" in x else "PEQUEÑO")
    else:
        df["TAMAÑO"] = "PEQUEÑO"

    # Otras dimensiones
    for src, dst in [("LINEA", "LINEA"), ("MARCA", "MARCA"), ("PROCEDENCIA", "PROCEDENCIA")]:
        if src in df.columns:
            df[dst] = df[src].fillna("").str.strip().str.upper()
        else:
            df[dst] = ""

    return df[["SKU", "AREA", "LINEA", "MARCA", "PROCEDENCIA", "TIPO_ALMACEN", "TAMAÑO"]].drop_duplicates("SKU")


def _load_outbound(dims: pd.DataFrame | None = None,
                   maestra: pd.DataFrame | None = None) -> pd.DataFrame:
    """Carga demanda proyectada outbound desde el forecast subido en Proyección de Stock
    (forecast_manual_canal_dist, guardado en session_state con la key "forecast")."""
    fcst_file = get_saved_file_path("forecast")
    if fcst_file is None:
        return pd.DataFrame()

    df_raw = pd.read_excel(fcst_file, sheet_name=0, dtype={"id_material": str})
    df_raw["id_material"] = df_raw["id_material"].fillna("").astype(str).str.strip()

    # Columnas de meses: formato "MM/YYYY"
    month_cols = [c for c in df_raw.columns if re.match(r"^\d{2}/\d{4}$", str(c))]
    if not month_cols:
        return pd.DataFrame()

    # Melt → long
    id_vars = [c for c in ["id_material", "canal_distribucion"] if c in df_raw.columns]
    df_long = df_raw.melt(id_vars=id_vars, value_vars=month_cols,
                          var_name="MES_STR", value_name="FORECAST_VENTA")
    df_long["FORECAST_VENTA"] = pd.to_numeric(df_long["FORECAST_VENTA"], errors="coerce").fillna(0)

    # Parsear fecha "MM/YYYY" → primer día del mes
    df_long["PERIODO_ETA"] = pd.to_datetime(
        "01/" + df_long["MES_STR"], format="%d/%m/%Y", errors="coerce"
    )

    # Sumar todos los canales por SKU × mes
    df_agg = (
        df_long.groupby(["id_material", "PERIODO_ETA"], as_index=False)["FORECAST_VENTA"].sum()
    )
    # Eliminar valores residuales de cálculo (< 0.5 unidades)
    df_agg = df_agg[df_agg["FORECAST_VENTA"] >= 0.5].copy()
    if df_agg.empty:
        return pd.DataFrame()

    df_agg = df_agg.rename(columns={"id_material": "SKU_FCST"})
    df_agg["SKU_FCST"] = df_agg["SKU_FCST"].astype(str).str.strip()

    # --- Join con leyendas: AREA, LINEA, MARCA, PROCEDENCIA, TIPO_ALMACEN, TAMAÑO ---
    ley = _load_leyendas()
    if not ley.empty:
        ley_sub = ley.rename(columns={"SKU": "SKU_FCST"})
        df_agg = df_agg.merge(ley_sub, on="SKU_FCST", how="left")

    for col in ["AREA", "LINEA", "MARCA", "PROCEDENCIA", "TIPO_ALMACEN"]:
        if col in df_agg.columns:
            df_agg[col] = df_agg[col].fillna("").astype(str).str.upper().str.strip()
        else:
            df_agg[col] = ""
    # TAMAÑO viene directo de leyendas
    if "TAMAÑO" not in df_agg.columns:
        df_agg["TAMAÑO"] = "PEQUEÑO"
    else:
        df_agg["TAMAÑO"] = df_agg["TAMAÑO"].fillna("PEQUEÑO")

    # --- Join con dims para M3 y pallets ---
    df_agg["M3_UNIDAD"] = 0.0
    df_agg["UNIDADES_X_PALLET"] = 0.0
    if dims is not None and not dims.empty:
        dims_norm = norm_cols(dims.copy())
        dim_sku = next((c for c in dims_norm.columns if c.upper().startswith("COD_PROD")), None)
        if dim_sku and "M3_UNIDAD" in dims_norm.columns and "UNIDADES_X_PALLET" in dims_norm.columns:
            dims_sub = dims_norm[[dim_sku, "M3_UNIDAD", "UNIDADES_X_PALLET"]].copy()
            dims_sub[dim_sku] = dims_sub[dim_sku].astype(str).str.strip()
            dims_sub = dims_sub.rename(columns={dim_sku: "SKU_FCST"})
            df_agg = df_agg.merge(dims_sub, on="SKU_FCST", how="left", suffixes=("", "_d"))
            for col in ["M3_UNIDAD", "UNIDADES_X_PALLET"]:
                if col + "_d" in df_agg.columns:
                    df_agg[col] = df_agg[col + "_d"].combine_first(df_agg[col])
                    df_agg.drop(columns=[col + "_d"], inplace=True)
    for col in ["M3_UNIDAD", "UNIDADES_X_PALLET"]:
        df_agg[col] = pd.to_numeric(df_agg[col], errors="coerce").fillna(0.0)

    # --- Campos finales ---
    df_agg["QTY_PENDIENTE"] = df_agg["FORECAST_VENTA"]
    # TAMAÑO ya viene de leyendas; si falta (SKU sin leyenda), clasificar por dims
    if "TAMAÑO" not in df_agg.columns:
        df_agg["TAMAÑO"] = df_agg.apply(
            lambda r: _classify_tamaño(r["M3_UNIDAD"], r["UNIDADES_X_PALLET"]), axis=1
        )
    df_agg["ES_PHILIPS"] = df_agg["MARCA"].apply(_is_philips)
    df_agg.loc[df_agg["ES_PHILIPS"], "AREA"] = "BPA"   # Philips Avent → BPA
    df_agg["PALLETS"] = np.where(
        df_agg["UNIDADES_X_PALLET"] > 0,
        df_agg["QTY_PENDIENTE"] / df_agg["UNIDADES_X_PALLET"],
        0.0,
    )
    df_agg["MT3"]    = df_agg["QTY_PENDIENTE"] * df_agg["M3_UNIDAD"]
    df_agg["N_PO"]   = None
    df_agg["STATUS"] = "Forecast Venta"
    return df_agg


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def _classify_tamaño(m3: float, und_pallet: float) -> str:
    if m3 > _M3_UMBRAL_GRANDE:
        return "GRANDE"
    if und_pallet > 0 and und_pallet < 15:
        return "GRANDE"
    return "PEQUEÑO"


def _is_philips(marca: str) -> bool:
    if not marca or not isinstance(marca, str):
        return False
    lw = marca.lower()
    return any(kw in lw for kw in _PHILIPS_KEYWORDS)


def _enrich(df_pos: pd.DataFrame, maestra: pd.DataFrame, dims: pd.DataFrame) -> pd.DataFrame:
    # df_pos already has AREA, LINEA, MARCA, PROCEDENCIA from the SQL join.
    # TIPO_ALMACEN comes from data/tipo_almacen.csv (exported from vw_produto).
    df = df_pos.copy()
    if _TIPO_ALM_PATH.exists():
        _ta = pd.read_csv(_TIPO_ALM_PATH, dtype=str)
        # Rename first col to match df_pos key regardless of CSV column name encoding
        _ta = _ta.rename(columns={_ta.columns[0]: "SKU_PRODUCTO"})
        _ta["SKU_PRODUCTO"] = _ta["SKU_PRODUCTO"].str.strip()
        _ta["TIPO_ALMACEN"] = _ta["TIPO_ALMACEN"].str.strip().str.upper()
        df = df.merge(_ta[["SKU_PRODUCTO", "TIPO_ALMACEN"]], on="SKU_PRODUCTO", how="left")

    # Dimensiones de cubicaje
    dims_norm = dims.rename(columns={"COD_PRODUCTO": "SKU_PRODUCTO"})
    for c in ["M3_UNIDAD", "UNIDADES_X_PALLET"]:
        if c not in dims_norm.columns:
            dims_norm[c] = 0.0
    df = df.merge(
        dims_norm[["SKU_PRODUCTO", "M3_UNIDAD", "UNIDADES_X_PALLET"]].drop_duplicates("SKU_PRODUCTO"),
        on="SKU_PRODUCTO", how="left")

    # Coerce numéricos
    for col in ["M3_UNIDAD", "UNIDADES_X_PALLET", "QTY_ORDENADA", "QTY_PENDIENTE",
                "QTY_RECEPCIONADA", "MONTO_CLP", "MONTO_PENDIENTE_CLP"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # TAMAÑO derivado de dimensiones
    df["TAMAÑO"] = df.apply(
        lambda r: _classify_tamaño(r["M3_UNIDAD"], r["UNIDADES_X_PALLET"]), axis=1
    )

    # TIPO_ALMACEN normalizado
    if "TIPO_ALMACEN" not in df.columns:
        df["TIPO_ALMACEN"] = ""
    df["TIPO_ALMACEN"] = df["TIPO_ALMACEN"].fillna("").str.upper().str.strip()

    # AREA: BPA products override their GRUPO with "BPA"
    if "AREA" not in df.columns:
        df["AREA"] = "SIN AREA"
    df["AREA"] = df["AREA"].fillna("SIN AREA").str.upper().str.strip()
    df.loc[df["TIPO_ALMACEN"] == "BPA", "AREA"] = "BPA"

    # MARCA normalizada
    if "MARCA" not in df.columns:
        df["MARCA"] = ""
    df["MARCA"] = df["MARCA"].fillna("").str.upper().str.strip()

    # PROCEDENCIA normalizada
    if "PROCEDENCIA" not in df.columns:
        df["PROCEDENCIA"] = "IMPORTADO"
    df["PROCEDENCIA"] = df["PROCEDENCIA"].fillna("IMPORTADO").str.upper().str.strip()

    # FLAG Philips Avent → forzar AREA = BPA
    df["ES_PHILIPS"] = df["MARCA"].apply(_is_philips)
    df.loc[df["ES_PHILIPS"], "AREA"] = "BPA"

    # PERIODO_ETA como Timestamp
    if "PERIODO_ETA" in df.columns:
        df["PERIODO_ETA"] = pd.to_datetime(df["PERIODO_ETA"], errors="coerce")

    # Pallets, MT3 y Costo basados en QTY_PENDIENTE (mismo criterio que resumen plan de compra)
    df["QTY_PENDIENTE"] = pd.to_numeric(df.get("QTY_PENDIENTE", 0), errors="coerce").fillna(0)
    df["PALLETS"] = np.where(
        df["UNIDADES_X_PALLET"] > 0,
        df["QTY_PENDIENTE"] / df["UNIDADES_X_PALLET"],
        0.0,
    )
    df["MT3"] = df["QTY_PENDIENTE"] * df["M3_UNIDAD"]
    df["COSTO_PEN"] = df["MONTO_PENDIENTE_CLP"].fillna(0) if "MONTO_PENDIENTE_CLP" in df.columns else df["MONTO_CLP"].fillna(0)

    return df


# ---------------------------------------------------------------------------
# Pivot helpers
# ---------------------------------------------------------------------------

def _pivot_monthly(df: pd.DataFrame, value_col: str, year: int,
                   agg: str = "sum") -> pd.DataFrame:
    df_y = df[df["PERIODO_ETA"].dt.year == year].copy()
    df_y["MES"] = df_y["PERIODO_ETA"].dt.month.map(MESES_ES)

    if agg == "count_po":
        grp = df_y.groupby(["AREA", "TAMAÑO", "MES"])["N_PO"].nunique().reset_index()
        grp = grp.rename(columns={"N_PO": value_col})
    else:
        grp = df_y.groupby(["AREA", "TAMAÑO", "MES"])[value_col].sum().reset_index()

    pivot = grp.pivot_table(index=["AREA", "TAMAÑO"], columns="MES",
                            values=value_col, aggfunc="sum", fill_value=0)
    for m in MESES_COLS:
        if m not in pivot.columns:
            pivot[m] = 0.0
    pivot = pivot[MESES_COLS].reset_index()
    pivot["TOTAL"] = pivot[MESES_COLS].sum(axis=1)
    return pivot


def _empty_pivot() -> pd.DataFrame:
    return pd.DataFrame(columns=["AREA", "TAMAÑO"] + MESES_COLS + ["TOTAL"])


def _add_pivots(p1: pd.DataFrame, p2: pd.DataFrame) -> pd.DataFrame:
    """Suma dos pivots con el mismo schema (AREA, TAMAÑO + meses + TOTAL)."""
    if p1.empty and p2.empty:
        return _empty_pivot()
    if p1.empty:
        return p2.reset_index(drop=True)
    if p2.empty:
        return p1.reset_index(drop=True)
    combined = pd.concat([p1, p2], ignore_index=True)
    num_cols = MESES_COLS + ["TOTAL"]
    for c in num_cols:
        combined[c] = pd.to_numeric(combined.get(c, 0), errors="coerce").fillna(0)
    result = combined.groupby(["AREA", "TAMAÑO"], as_index=False)[num_cols].sum()
    return result[["AREA", "TAMAÑO"] + MESES_COLS + ["TOTAL"]]


def _pivot_cntr(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Contenedores: POs reales → N_PO únicos; Proy → pallets / _PALLETS_POR_CNTR."""
    df2 = df.copy()  # Philips Avent ya viene como BPA desde la fuente

    mask_proy = df2["N_PO"].isna()
    df_po = df2[~mask_proy]
    df_pr = df2[mask_proy].copy()

    piv_po = _pivot_monthly(df_po, "N_PO", year, agg="count_po") if not df_po.empty else _empty_pivot()

    if not df_pr.empty:
        df_pr["_CNTR"] = (df_pr["PALLETS"] / _PALLETS_POR_CNTR).round(2)
        piv_pr = _pivot_monthly(df_pr, "_CNTR", year, agg="sum")
    else:
        piv_pr = _empty_pivot()

    return _add_pivots(piv_po, piv_pr)


def _pivot_cntr_out(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Contenedores outbound estimados desde pallets (sin N_PO)."""
    df2 = df.copy()  # Philips Avent ya viene como BPA desde la fuente
    df2["_CNTR"] = (df2["PALLETS"] / _PALLETS_POR_CNTR).round(2)
    return _pivot_monthly(df2, "_CNTR", year, agg="sum")


def _build_table(pivot: pd.DataFrame, rows: list[tuple],
                 cols: list[str]) -> pd.DataFrame:
    display_cols = cols + ["TOTAL"]
    rows_data = []
    for cat, sub in rows:
        mask = (pivot["AREA"] == cat) & (pivot["TAMAÑO"] == sub)
        if mask.any():
            row = pivot[mask][display_cols].iloc[0].to_dict()
        else:
            row = {c: 0.0 for c in display_cols}
        row["CATEGORIA"] = cat
        row["SUBCATEGORIA"] = sub
        rows_data.append(row)

    df_out = pd.DataFrame(rows_data)
    total_row = {c: df_out[c].sum() for c in display_cols}
    total_row["CATEGORIA"] = "TOTAL"
    total_row["SUBCATEGORIA"] = ""
    df_out = pd.concat([pd.DataFrame([total_row]), df_out], ignore_index=True)

    col_order = ["CATEGORIA", "SUBCATEGORIA"] + display_cols
    return df_out[[c for c in col_order if c in df_out.columns]]


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _card_header(titulo: str):
    return (
        f'<div style="background:#065E8B18;border-left:4px solid #065E8B;'
        f'border-radius:0 8px 8px 0;padding:0.6rem 1rem;margin:1rem 0 0.5rem 0;">'
        f'<span style="font-size:0.95rem;font-weight:700;color:#065E8B;">{titulo}</span>'
        f'</div>'
    )


def _section_divider(label: str):
    st.markdown("---")
    st.markdown(
        f'<h4 style="color:#065E8B;margin-bottom:0.25rem;">{label}</h4>',
        unsafe_allow_html=True,
    )


def _render_table(df_table: pd.DataFrame, titulo: str, fmt_decimals: int = 0):
    st.html(_card_header(titulo))
    display = df_table.rename(columns={"CATEGORIA": "Categoría", "SUBCATEGORIA": "Subcategoría"})
    num_cols = [c for c in display.columns if c not in ("Categoría", "Subcategoría")]

    def _fmt(v):
        if pd.isna(v) or v == 0:
            return "-"
        if fmt_decimals == 0:
            return f"{int(round(v)):,}"
        return f"{v:,.{fmt_decimals}f}"

    styled = display.style.apply(
        lambda row: (
            ["font-weight:bold;background-color:#EBF5FB"] * len(row)
            if row["Categoría"] == "TOTAL" else [""] * len(row)
        ),
        axis=1,
    ).format({c: _fmt for c in num_cols}, na_rep="-")

    st.dataframe(styled, use_container_width=True, hide_index=True,
                 height=min(600, (len(df_table) + 1) * 38 + 20))


def _render_kpis(df: pd.DataFrame, year: int):
    df_y = df[df["PERIODO_ETA"].dt.year == year]
    total_und   = df_y["QTY_PENDIENTE"].sum()
    total_pos   = df_y["N_PO"].nunique()
    total_mt3   = df_y["MT3"].sum()
    total_costo = df_y["COSTO_PEN"].sum()

    kc = COLORS.get("primary", "#065E8B")
    kt = COLORS.get("tertiary_teal", "#23CED3")
    kg = COLORS.get("status_on_track", "#22c55e")
    ko = COLORS.get("status_at_risk", "#f59e0b")

    def _kpi(lbl, val, sub, color):
        return (
            f'<div style="background:{color}18;border:1px solid {color}40;'
            f'border-radius:10px;padding:0.8rem 1rem;">'
            f'<div style="font-size:0.75rem;color:#64748b;font-weight:600;">{lbl}</div>'
            f'<div style="font-size:1.7rem;font-weight:800;color:{color};">{val}</div>'
            f'<div style="font-size:0.75rem;color:#64748b;">{sub}</div>'
            f'</div>'
        )

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.html(_kpi("Unidades IN", f"{total_und:,.0f}", f"OCs: {total_pos}", kc))
    with k2:
        st.html(_kpi("M³ Total", f"{total_mt3:,.1f}", "volumen acumulado", kt))
    with k3:
        st.html(_kpi("Costo Total", f"S/ {human_format(total_costo)}", "moneda local", kg))
    with k4:
        st.html(_kpi("Órdenes de Compra", f"{total_pos:,}", f"año {year}", ko))


def _export_all(tables: dict[str, pd.DataFrame], year: int):
    """Genera un Excel con todas las tablas en una sola hoja, separadas por filas de titulo."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = f"Inbound {year}"

    header_fill  = PatternFill("solid", fgColor="065E8B")
    header_font  = Font(bold=True, color="FFFFFF", size=11)
    title_fill   = PatternFill("solid", fgColor="D6E8F4")
    title_font   = Font(bold=True, color="065E8B", size=10)
    total_fill   = PatternFill("solid", fgColor="EBF5FB")
    total_font   = Font(bold=True)
    center_align = Alignment(horizontal="center", vertical="center")
    right_align  = Alignment(horizontal="right",  vertical="center")
    left_align   = Alignment(horizontal="left",   vertical="center")
    thin_side    = Side(style="thin", color="CCCCCC")
    thin_border  = Border(bottom=thin_side)

    current_row = 1

    for title, df_t in tables.items():
        # Title row
        label = title.split("-", 1)[-1].strip().replace("_", " ")
        ws.cell(row=current_row, column=1, value=label).font = title_font
        ws.cell(row=current_row, column=1).fill = title_fill
        ws.merge_cells(
            start_row=current_row, start_column=1,
            end_row=current_row, end_column=len(df_t.columns)
        )
        ws.cell(row=current_row, column=1).alignment = left_align
        current_row += 1

        # Header row
        for col_idx, col_name in enumerate(df_t.columns, start=1):
            cell = ws.cell(row=current_row, column=col_idx, value=col_name)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center_align
        current_row += 1

        # Data rows
        for _, row_data in df_t.iterrows():
            is_total = str(row_data.iloc[0]).upper() == "TOTAL"
            for col_idx, (col_name, val) in enumerate(row_data.items(), start=1):
                cell = ws.cell(row=current_row, column=col_idx)
                # Format numbers
                if isinstance(val, (int, float)) and not (isinstance(val, float) and str(val) == "nan"):
                    if val == 0:
                        cell.value = None
                    else:
                        cell.value = round(val, 2)
                    cell.alignment = right_align
                    cell.number_format = "#,##0.##"
                else:
                    cell.value = val
                    cell.alignment = left_align
                if is_total:
                    cell.font = total_font
                    cell.fill = total_fill
            current_row += 1

        # 2 blank separator rows
        current_row += 2

    # Auto-fit column widths (approximate)
    for col_idx in range(1, ws.max_column + 1):
        max_len = 0
        col_letter = get_column_letter(col_idx)
        for row_idx in range(1, ws.max_row + 1):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[col_letter].width = min(max(max_len + 2, 8), 20)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        "⬇️  Descargar todo (Excel)",
        buf,
        f"inbound_{year}_{ts}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )


# ---------------------------------------------------------------------------
# Render principal
# ---------------------------------------------------------------------------

def render_inbound(conn):
    """Entry point — llamado desde app.py."""
    st.html(page_header(
        "In-Out bound",
        "Flujo de ingresos mensual por categoría · Resumen Plan de Compras",
    ))

    if conn is None:
        st.warning("No hay conexion a Snowflake.")
        return

    df_pos, maestra, dims = _load_data(conn)

    if df_pos.empty:
        st.info("No se encontraron órdenes de compra.")
        return

    df = _enrich(df_pos, maestra, dims)

    # Normalizar PERIODO_ETA a timezone-naive (Snowflake puede retornar tz-aware)
    if "PERIODO_ETA" in df.columns:
        try:
            if df["PERIODO_ETA"].dt.tz is not None:
                df["PERIODO_ETA"] = df["PERIODO_ETA"].dt.tz_convert("UTC").dt.tz_localize(None)
        except Exception:
            df["PERIODO_ETA"] = pd.to_datetime(df["PERIODO_ETA"], errors="coerce")

    # Agregar compras proyectadas (proy_result.parquet) — misma lógica que plan_compras
    _proy_rows = 0
    _proy_err  = ""
    try:
        df_proy = _load_proy(dims)
        _proy_rows = len(df_proy)
        if not df_proy.empty:
            # Asegurar PERIODO_ETA compatible antes del concat
            if "PERIODO_ETA" in df_proy.columns:
                try:
                    if df_proy["PERIODO_ETA"].dt.tz is not None:
                        df_proy["PERIODO_ETA"] = df_proy["PERIODO_ETA"].dt.tz_localize(None)
                except Exception:
                    pass
            df = pd.concat([df, df_proy], ignore_index=True)
    except Exception as e:
        _proy_err = str(e)

    with st.expander("🔍 Debug compras proy (eliminar luego)", expanded=False):
        st.write(f"Proy rows loaded: {_proy_rows}")
        if _proy_err:
            st.error(f"Error: {_proy_err}")
        st.write(f"DF total rows after concat: {len(df)}")
        st.write(f"PERIODO_ETA dtype: {df['PERIODO_ETA'].dtype if 'PERIODO_ETA' in df.columns else 'N/A'}")
        if not df.empty and "PERIODO_ETA" in df.columns:
            yrs = df["PERIODO_ETA"].dropna().dt.year.unique().tolist()
            st.write(f"Years in data: {sorted(yrs)}")

    # Filtro año — defaultear siempre al año actual
    year_default = datetime.now().year
    years_avail = sorted(
        df["PERIODO_ETA"].dropna().dt.year.unique().tolist(), reverse=True
    )
    if not years_avail:
        years_avail = [year_default]
    if year_default not in years_avail:
        years_avail = [year_default] + years_avail
    default_idx = years_avail.index(year_default) if year_default in years_avail else 0

    year = st.sidebar.selectbox("Año Inbound", years_avail, index=default_idx, key="inbound_year")

    st.markdown("")
    _render_kpis(df, year)
    st.markdown("---")

    # ── Filtros: misma lógica que resumen plan de compra ────────────────────
    _STATUS_CERRADOS = {
        "Cerrada", "PO Recepcionada", "CERRADA", "RECEPCIONADA",
        "Cancelada", "CANCELADA", "Cerrado", "Recibido",
    }
    df = df[
        (df["QTY_PENDIENTE"] > 0)
        & (~df["STATUS_PO"].astype(str).isin(_STATUS_CERRADOS))
    ].copy()

    # Meses pasados (dentro del año) → mover al mes actual (igual que plan_compras)
    mes_actual = datetime.now().month
    late = (
        (df["PERIODO_ETA"].dt.year == year)
        & (df["PERIODO_ETA"].dt.month < mes_actual)
    )
    df.loc[late, "PERIODO_ETA"] = pd.Timestamp(datetime(year, mes_actual, 1))

    # ── Pivots NIVEL COMPAÑÍA ────────────────────────────────────────────────
    piv_und = _pivot_monthly(df, "QTY_PENDIENTE", year)
    piv_pal = _pivot_monthly(df, "PALLETS", year)
    piv_cnt = _pivot_cntr(df, year)
    piv_mt3 = _pivot_monthly(df, "MT3", year)
    piv_cos = _pivot_monthly(df, "COSTO_PEN", year)

    with st.expander("🔍 Debug pivot (eliminar luego)", expanded=False):
        proy_mask = df["STATUS_PO"].astype(str) == "Compra Proy"
        df_proy_check = df[proy_mask & (df["PERIODO_ETA"].dt.year == year)]
        st.write(f"Año seleccionado: {year}")
        st.write(f"Rows STATUS='Compra Proy' en año {year}: {len(df_proy_check)}")
        st.write(f"QTY_PENDIENTE total proy {year}: {df_proy_check['QTY_PENDIENTE'].sum():,.0f}")
        if not df_proy_check.empty:
            grp = df_proy_check.groupby(["AREA", "TAMAÑO"])["QTY_PENDIENTE"].sum().reset_index()
            st.dataframe(grp, use_container_width=True, hide_index=True)
        st.write("Pivot und (primeras 10 filas):")
        st.dataframe(piv_und.head(10), use_container_width=True, hide_index=True)

    # ── Construir tablas Inbound ─────────────────────────────────────────────
    t1  = _build_table(piv_und, NIVEL_ROWS, MESES_COLS)
    t2  = _build_table(piv_pal, NIVEL_ROWS, MESES_COLS)
    t3  = _build_table(piv_cnt, CNTR_ROWS,  MESES_COLS)
    t4  = t3.copy()
    num_t4 = [c for c in t4.columns if c not in ("CATEGORIA", "SUBCATEGORIA")]
    t4[num_t4] = (t4[num_t4] / 4.0).round(1)
    t5  = _build_table(piv_und, NIVEL_ROWS, MESES_COLS)
    t6  = _build_table(piv_pal, NIVEL_ROWS, MESES_COLS)
    t7  = _build_table(piv_mt3, NIVEL_ROWS, MESES_COLS)
    t8  = _build_table(piv_cos, NIVEL_ROWS, MESES_COLS)

    # ── Construir tablas Outbound (antes del botón de descarga) ─────────────
    df_out   = pd.DataFrame()
    _out_err = ""
    to1 = to2 = to3 = to4 = to5 = pd.DataFrame()
    try:
        df_out = _load_outbound(dims, maestra)
    except Exception as e:
        _out_err = str(e)

    if not df_out.empty:
        piv_out_und = _pivot_monthly(df_out, "QTY_PENDIENTE", year)
        piv_out_pal = _pivot_monthly(df_out, "PALLETS",       year)
        piv_out_mt3 = _pivot_monthly(df_out, "MT3",           year)
        piv_out_cnt = _pivot_cntr_out(df_out, year)
        to1 = _build_table(piv_out_und, NIVEL_ROWS, MESES_COLS)
        to2 = _build_table(piv_out_pal, NIVEL_ROWS, MESES_COLS)
        to3 = _build_table(piv_out_mt3, NIVEL_ROWS, MESES_COLS)
        to4 = _build_table(piv_out_cnt, CNTR_ROWS,  MESES_COLS)
        to5 = to4.copy()
        num_to5 = [c for c in to5.columns if c not in ("CATEGORIA", "SUBCATEGORIA")]
        to5[num_to5] = (to5[num_to5] / 4.0).round(1)

    # ── Botón de descarga única (Inbound + Outbound) ─────────────────────────
    all_tables: dict[str, pd.DataFrame] = {
        "1-Und_IN_Mes":      t1,
        "2-Pallets_IN_Mes":  t2,
        "3-CNTR_IN_Mes":     t3,
        "4-CNTR_IN_Semana":  t4,
        "5-Alm_Und_Mes":     t5,
        "6-Alm_Pallets_Mes": t6,
        "7-Alm_MT3_Mes":     t7,
        "8-Alm_Costo_Mes":   t8,
    }
    if not df_out.empty:
        all_tables.update({
            "9-Und_OUT_Mes":      to1,
            "10-Pallets_OUT_Mes": to2,
            "11-MT3_OUT_Mes":     to3,
            "12-CNTR_OUT_Mes":    to4,
            "13-CNTR_OUT_Semana": to5,
        })
    _export_all(all_tables, year)

    # ══════════════════════════════════════════════════════════════════════════
    # SECCIÓN 1: NIVEL COMPAÑÍA
    # ══════════════════════════════════════════════════════════════════════════
    _section_divider("📦 NIVEL COMPAÑÍA")

    _render_table(t1, "Unidades IN MES")
    st.markdown("")
    _render_table(t2, "Pallets IN MES", fmt_decimals=1)
    st.markdown("")
    _render_table(t3, "Contenedores IN MES")
    st.markdown("")
    _render_table(t4, "Contenedores IN Semana (÷ 4)", fmt_decimals=1)

    # ══════════════════════════════════════════════════════════════════════════
    # SECCIÓN 2: FLUJO ALMACENAMIENTO
    # ══════════════════════════════════════════════════════════════════════════
    _section_divider("🏢 Flujo Almacenamiento")

    _render_table(t5, "Unidades IN MES — Almacenamiento")
    st.markdown("")
    _render_table(t6, "Pallets IN MES — Almacenamiento", fmt_decimals=1)
    st.markdown("")
    _render_table(t7, "MT3 IN MES — Almacenamiento", fmt_decimals=2)
    st.markdown("")
    _render_table(t8, "Costo $ IN MES — Almacenamiento")

    # ══════════════════════════════════════════════════════════════════════════
    # SECCIÓN 3: OUTBOUND  (forecast_manual_canal_dist)
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown("---")
    _section_divider("🚚 OUTBOUND — Flujo de Salidas")

    if _out_err:
        st.error(f"Error cargando outbound: {_out_err}")
    elif df_out.empty:
        st.info(
            "📋 No se encontró el archivo de forecast (forecast_manual_canal_dist). "
            "Súbelo en Proyección de Stock para activar las tablas Outbound."
        )
    else:
        _render_table(to1, "Unidades OUT MES")
        st.markdown("")
        _render_table(to2, "Pallets OUT MES", fmt_decimals=1)
        st.markdown("")
        _render_table(to3, "MT3 OUT MES", fmt_decimals=2)
        st.markdown("")
        _render_table(to4, "Contenedores OUT MES")
        st.markdown("")
        _render_table(to5, "Contenedores OUT Semana (÷ 4)", fmt_decimals=1)
