"""Alerta Forecast — Detecta SKUs cuyo forecast supera umbrales estadisticos
sobre la venta real de los ultimos 4 meses cerrados, por SKU x Centro de Costo.

Logica de alertas
-----------------
Para cada (SKU, Centro de Costo) se calcula:
    media_4m = promedio mensual de los ultimos 4 meses
    std_4m   = desviacion standard (ddof=1) de los 4 meses

    ADVERTENCIA : forecast > media + 2*std  (forecast sobreestimado, revisar)
    ALERTA      : forecast > media + 5*std  (forecast muy sobreestimado, corregir)

Fuente del forecast
-------------------
1. Si existe un snapshot guardado en Forecast Accuracy, se usa automaticamente
   (muestra nombre del archivo).  El usuario puede elegir otro snapshot o subir uno nuevo.
2. Si no hay snapshots, se muestra el uploader de archivo Excel.
"""

import io
import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, dorel_layout, apply_pm_filter
from db.cache import cached_query as cq
from utils.export import download_buttons
from utils.filters import norm_cols
from utils.ui_animations import lottie_spinner


# ============================================================================
# CONSTANTS
# ============================================================================

_CANAL_MAP = {
    "MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL",
    "TIENDA": "TIENDA", "MAYORISTA": "MAYORISTA",
    "03": "TIENDA", "02": "MAYORISTA", "06": "ETAIL",
    "RETAIL": "TIENDA",
}

_ESTADO_COLORS = {
    "ALERTA":      "#E53935",
    "ADVERTENCIA": "#FB8C00",
    "OK":          "#43A047",
    "SIN DATOS":   "#9E9E9E",
}

_ESTADO_ICON = {
    "ALERTA":      "🔴",
    "ADVERTENCIA": "🟡",
    "OK":          "🟢",
    "SIN DATOS":   "⚪",
}

_ESTADO_ORDER = ["ALERTA", "ADVERTENCIA", "OK", "SIN DATOS"]

# Umbrales basados en ratio FC / Media historica (mas robusto que sigma para
# skus con baja variabilidad o bajo volumen).
# ADVERTENCIA : forecast > 50 % sobre el promedio historico  (ratio > 1.5)
# ALERTA      : forecast > 100 % sobre el promedio historico (ratio > 2.0, doble)
_RATIO_ADVERTENCIA = 1.5
_RATIO_ALERTA      = 2.0

_SPANISH_MONTHS = {
    "ENE": "Jan", "FEB": "Feb", "MAR": "Mar", "ABR": "Apr",
    "MAY": "May", "JUN": "Jun", "JUL": "Jul", "AGO": "Aug",
    "SEP": "Sep", "SET": "Sep", "OCT": "Oct", "NOV": "Nov", "DIC": "Dec",
    "ENERO": "January", "FEBRERO": "February", "MARZO": "March",
    "ABRIL": "April", "MAYO": "May", "JUNIO": "June",
    "JULIO": "July", "AGOSTO": "August", "SEPTIEMBRE": "September",
    "OCTUBRE": "October", "NOVIEMBRE": "November", "DICIEMBRE": "December",
}

# Directorio compartido con Forecast Accuracy
_SNAPSHOTS_DIR = Path(__file__).resolve().parent.parent / "data" / "forecast_snapshots"
_META_FILE = _SNAPSHOTS_DIR / "metadata.json"

# ============================================================================
# SNAPSHOT READER (comparte parquets con modules/forecast_accuracy.py)
# ============================================================================

def _list_snapshots() -> list[dict]:
    """Devuelve lista de snapshots guardados (mas nuevo primero) con metadata."""
    if not _META_FILE.exists():
        return []
    try:
        with open(_META_FILE, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return []
    result = []
    for key in sorted(meta.keys(), reverse=True):
        path = _SNAPSHOTS_DIR / f"fcst_{key}.parquet"
        if path.exists():
            result.append({"key": key, **meta[key]})
    return result


def _load_snapshot(key: str) -> "pd.DataFrame | None":
    """Carga un snapshot de forecast desde parquet."""
    path = _SNAPSHOTS_DIR / f"fcst_{key}.parquet"
    if path.exists():
        try:
            return pd.read_parquet(path)
        except Exception:
            return None
    return None


# ============================================================================
# FORECAST PARSER (formato Syncro: SKU x CANAL x meses — Excel)
# ============================================================================

def _spanish_to_english(text: str) -> str:
    t = text.strip()
    for es, en in sorted(_SPANISH_MONTHS.items(), key=lambda x: -len(x[0])):
        t = re.sub(rf'\b{es}\b', en, t, flags=re.IGNORECASE)
    return t


def _detect_month_col(col_name):
    s = str(col_name).strip()
    if not s or "UNNAMED" in s.upper():
        return None
    s_en = _spanish_to_english(s)
    for fmt in [
        "%m/%Y", "%m-%Y", "%Y-%m", "%Y/%m",
        "%b-%Y", "%b/%Y", "%b %Y", "%b-%y", "%b/%y", "%b %y",
        "%B-%Y", "%B/%Y", "%B %Y",
        "%Y-%m-%d", "%d/%m/%Y",
    ]:
        try:
            dt = datetime.strptime(s_en, fmt)
            return pd.Timestamp(dt.replace(day=1))
        except (ValueError, TypeError):
            continue
    try:
        dt = pd.to_datetime(s_en, dayfirst=True)
        if 2020 <= dt.year <= 2030:
            return pd.Timestamp(dt.replace(day=1))
    except Exception:
        pass
    return None


def _parse_forecast_excel(file) -> "tuple[pd.DataFrame, str | None]":
    """Parsea Excel Syncro a formato largo (SKU_PRODUCTO, CANAL, PERIODO, FC_UND)."""
    try:
        df = pd.read_excel(file)
    except Exception as e:
        return pd.DataFrame(), f"Error leyendo Excel: {e}"

    if df.empty:
        return pd.DataFrame(), "Archivo vacio."

    cols_upper = {c: str(c).upper().strip() for c in df.columns}

    col_sku = next(
        (c for c, u in cols_upper.items()
         if u in ("SKU_PRODUCTO", "ID_MATERIAL", "SKU", "SKU_NUEVO",
                   "MATERIAL", "COD_PRODUCTO")),
        None,
    )
    col_canal = next(
        (c for c, u in cols_upper.items()
         if u in ("CANAL", "CANAL_DE_DISTRIBUCION", "COD_CANAL", "CANAL_DIST")),
        None,
    )
    col_suc = next(
        (c for c, u in cols_upper.items()
         if u in ("ID_SUCURSAL", "COD_BODEGA", "SUCURSAL")),
        None,
    )

    if col_sku is None:
        return pd.DataFrame(), (
            "No se encontro columna SKU. "
            "Formatos aceptados: SKU_PRODUCTO, ID_MATERIAL, SKU, COD_PRODUCTO."
        )

    desc_patterns = [
        "DESC", "NOMBRE", "NOM_", "PRODUCTO", "DESCRIPCION", "MARCA",
        "LINEA", "AREA", "MODELO", "CLASE", "GRUPO", "FAMILIA", "TIPO",
        "PROVEEDOR", "PROCEDENCIA", "MIX", "UNIDAD",
    ]
    fixed_cols = {col_sku, col_canal, col_suc} - {None}

    month_cols: dict = {}
    for c in df.columns:
        if c in fixed_cols:
            continue
        u = str(c).upper().strip()
        if any(p in u for p in desc_patterns):
            continue
        parsed = _detect_month_col(c)
        if parsed is not None:
            month_cols[c] = parsed

    if not month_cols:
        return pd.DataFrame(), (
            "No se detectaron columnas de meses. "
            "Formatos validos: MM/YYYY, MMM-YYYY, YYYY-MM, ENE-2026, etc."
        )

    id_vars = [col_sku]
    if col_canal:
        id_vars.append(col_canal)
    if col_suc and col_suc not in id_vars:
        id_vars.append(col_suc)

    df_melt = df[id_vars + list(month_cols.keys())].copy()
    df_melt = df_melt.melt(id_vars=id_vars, var_name="_MES_COL", value_name="FC_UND")
    df_melt["PERIODO"] = df_melt["_MES_COL"].map(month_cols)
    df_melt["FC_UND"] = pd.to_numeric(df_melt["FC_UND"], errors="coerce").fillna(0)

    df_melt.rename(columns={col_sku: "SKU_PRODUCTO"}, inplace=True)
    df_melt["SKU_PRODUCTO"] = df_melt["SKU_PRODUCTO"].astype(str).str.strip()

    if col_canal:
        df_melt["CANAL"] = (
            df_melt[col_canal].astype(str).str.strip().str.upper()
            .map(_CANAL_MAP).fillna("OTRO")
        )
    else:
        df_melt["CANAL"] = "TOTAL"

    # Si el archivo tiene columna de tienda/sucursal, preservar granularidad
    # por tienda (SKU x COD_CCOSTO) en lugar de agregar a nivel canal.
    # Esto evita que el total de canal se asigne a cada tienda por separado.
    if col_suc:
        df_melt["COD_CCOSTO"] = (
            df_melt[col_suc].astype(str).str.strip()
            .apply(lambda x: x.zfill(4) if x.isdigit() else x)
        )
        group_keys = ["SKU_PRODUCTO", "COD_CCOSTO", "CANAL", "PERIODO"]
    else:
        group_keys = ["SKU_PRODUCTO", "CANAL", "PERIODO"]

    result = df_melt.groupby(group_keys, as_index=False).agg(FC_UND=("FC_UND", "sum"))
    result = result[result["FC_UND"] > 0].reset_index(drop=True)
    return result, None


# ============================================================================
# ALERT ENGINE — analisis a nivel SKU x CENTRO DE COSTO
# ============================================================================

def _build_stats(
    ventas_df: pd.DataFrame,
    semanas_df: "pd.DataFrame | None" = None,
    stock_df: "pd.DataFrame | None" = None,
) -> pd.DataFrame:
    """Estadisticas por SKU x COD_CCOSTO.

    ventas_df : meses historicos cerrados + mes actual MTD (GROUP BY mes).
    semanas_df: ventas semanales ultimas 8 semanas (GROUP BY semana).
    stock_df  : stock por SKU x COD_CCOSTO x mes (ultimo dia del mes).

    Columnas resultado:
        SKU_PRODUCTO, COD_CCOSTO, CENTRO_COSTO, CANAL,
        MEDIA_4M, MEDIA_4M_AJUSTADO, STD_4M, N_PERIODOS, UMBRAL_ADVERTENCIA, UMBRAL_ALERTA,
        VENTA_MES_ACTUAL, SEMANA_1..SEMANA_5, VENTA_SEMANAL_PROMEDIO,
        FORECAST_SUGERIDO, FORECAST_SUGERIDO_CRONOLOGICO
    """
    if ventas_df.empty:
        return pd.DataFrame(columns=[
            "SKU_PRODUCTO", "COD_CCOSTO", "CENTRO_COSTO", "CANAL",
            "MEDIA_4M", "MEDIA_4M_AJUSTADO", "STD_4M", "N_PERIODOS",
            "UMBRAL_ADVERTENCIA", "UMBRAL_ALERTA",
            "VENTA_MES_ACTUAL",
            "SEMANA_1", "SEMANA_2", "SEMANA_3", "SEMANA_4", "SEMANA_5",
            "VENTA_SEMANAL_PROMEDIO", "FORECAST_SUGERIDO", "FORECAST_SUGERIDO_CRONOLOGICO",
        ])

    vdf = ventas_df.copy()
    vdf["PERIODO"] = pd.to_datetime(vdf["PERIODO"]).dt.normalize()

    # Mes actual (primer dia del mes en curso)
    mes_actual = pd.Timestamp.now().to_period("M").to_timestamp()

    # Separar historico (meses cerrados) vs MTD (mes en curso)
    vdf_hist = vdf[vdf["PERIODO"] < mes_actual].copy()
    vdf_mtd  = vdf[vdf["PERIODO"] >= mes_actual].copy()

    # Nombre de tienda
    nombre_map = (
        vdf.drop_duplicates(subset=["COD_CCOSTO"])
        .set_index("COD_CCOSTO")["CENTRO_COSTO"]
        .to_dict()
        if "CENTRO_COSTO" in vdf.columns else {}
    )

    # Ventas mensuales historicas por SKU x CCOSTO
    by_cc_mes = vdf_hist.groupby(
        ["SKU_PRODUCTO", "COD_CCOSTO", "PERIODO"], as_index=False,
    ).agg(UNIDADES=("UNIDADES", "sum"))

    # Estadisticas (solo meses cerrados)
    stats = by_cc_mes.groupby(["SKU_PRODUCTO", "COD_CCOSTO"]).agg(
        MEDIA_4M=("UNIDADES", "mean"),
        STD_4M=("UNIDADES", lambda x: x.std(ddof=1) if len(x) > 1 else 0.0),
        N_PERIODOS=("UNIDADES", "count"),
    ).reset_index()

    stats["STD_4M"]             = stats["STD_4M"].fillna(0.0)
    stats["UMBRAL_ADVERTENCIA"] = stats["MEDIA_4M"] * _RATIO_ADVERTENCIA
    stats["UMBRAL_ALERTA"]      = stats["MEDIA_4M"] * _RATIO_ALERTA

    # ── MEDIA_4M_AJUSTADO: promedio solo en meses donde stock > 0 en esa tienda ─
    if stock_df is not None and not stock_df.empty:
        st_df = stock_df.copy()
        st_df.columns = st_df.columns.str.upper()
        st_df["PERIODO"]      = pd.to_datetime(st_df["PERIODO"]).dt.normalize()
        st_df["COD_CCOSTO"]   = st_df["COD_CCOSTO"].astype(str).str.strip().str.zfill(4)
        st_df["SKU_PRODUCTO"] = st_df["SKU_PRODUCTO"].astype(str).str.strip()
        st_df["STOCK_UNIDADES"] = pd.to_numeric(st_df["STOCK_UNIDADES"], errors="coerce").fillna(0)
        meses_con_stock = st_df[st_df["STOCK_UNIDADES"] > 0][["SKU_PRODUCTO", "COD_CCOSTO", "PERIODO"]]
        ventas_con_stock = by_cc_mes.merge(meses_con_stock, on=["SKU_PRODUCTO", "COD_CCOSTO", "PERIODO"], how="inner")
        if not ventas_con_stock.empty:
            media_ajustada = (
                ventas_con_stock.groupby(["SKU_PRODUCTO", "COD_CCOSTO"], as_index=False)
                .agg(MEDIA_4M_AJUSTADO=("UNIDADES", "mean"))
            )
            media_ajustada["MEDIA_4M_AJUSTADO"] = media_ajustada["MEDIA_4M_AJUSTADO"].round(1)
            stats = stats.merge(media_ajustada, on=["SKU_PRODUCTO", "COD_CCOSTO"], how="left")
        else:
            stats["MEDIA_4M_AJUSTADO"] = float("nan")

        # ── TIPO: NUEVO si sin stock en los ultimos 3 meses cerrados, ACTUAL si tuvo stock ──
        # Usar los 3 meses mas recientes del historial de stock
        meses_recientes = (
            st_df.groupby(["SKU_PRODUCTO", "COD_CCOSTO"])["PERIODO"]
            .nlargest(3)
            .reset_index(level=2)
            .reset_index()
            [["SKU_PRODUCTO", "COD_CCOSTO", "PERIODO"]]
        )
        stock_reciente = st_df.merge(meses_recientes, on=["SKU_PRODUCTO", "COD_CCOSTO", "PERIODO"], how="inner")
        tuvo_stock = (
            stock_reciente.groupby(["SKU_PRODUCTO", "COD_CCOSTO"], as_index=False)
            .agg(_max_stock=("STOCK_UNIDADES", "max"))
        )
        tuvo_stock["TIPO"] = tuvo_stock["_max_stock"].apply(
            lambda x: "ACTUAL" if x > 0 else "NUEVO"
        )
        stats = stats.merge(
            tuvo_stock[["SKU_PRODUCTO", "COD_CCOSTO", "TIPO"]],
            on=["SKU_PRODUCTO", "COD_CCOSTO"], how="left",
        )
        stats["TIPO"] = stats["TIPO"].fillna("NUEVO")
    else:
        stats["MEDIA_4M_AJUSTADO"] = float("nan")
        stats["TIPO"] = "NUEVO"

    # ── Semanales: FORECAST_SUGERIDO (5 semanas con venta) y CRONOLOGICO (4 semanas) ─
    if semanas_df is not None and not semanas_df.empty:
        sw = semanas_df.copy()
        sw.columns = sw.columns.str.upper()
        sw["UNIDADES"] = pd.to_numeric(sw["UNIDADES"], errors="coerce").fillna(0)

        # FORECAST_SUGERIDO_CRONOLOGICO: ultimas 4 semanas cronologicas (incluye 0s)
        sw_crono  = sw.sort_values("SEMANA", ascending=False)
        top4_crono = sw_crono.groupby(["SKU_PRODUCTO", "COD_CCOSTO"]).head(4)
        vsp_crono  = top4_crono.groupby(["SKU_PRODUCTO", "COD_CCOSTO"], as_index=False).agg(_avg=("UNIDADES", "mean"))
        vsp_crono["FORECAST_SUGERIDO_CRONOLOGICO"] = (vsp_crono["_avg"] * 4).round(1)
        vsp_crono.drop(columns=["_avg"], inplace=True)
        stats = stats.merge(vsp_crono, on=["SKU_PRODUCTO", "COD_CCOSTO"], how="left")

        # FORECAST_SUGERIDO: ultimas 5 semanas con venta > 0
        sw_pos    = sw[sw["UNIDADES"] > 0].copy()
        sw_sorted = sw_pos.sort_values("SEMANA", ascending=False)
        top5 = sw_sorted.groupby(["SKU_PRODUCTO", "COD_CCOSTO"]).head(5)

        vsp = top5.groupby(["SKU_PRODUCTO", "COD_CCOSTO"], as_index=False).agg(VENTA_SEMANAL_PROMEDIO=("UNIDADES", "mean"))
        vsp["VENTA_SEMANAL_PROMEDIO"] = vsp["VENTA_SEMANAL_PROMEDIO"].round(1)
        vsp["FORECAST_SUGERIDO"]      = (vsp["VENTA_SEMANAL_PROMEDIO"] * 4).round(1)
        stats = stats.merge(vsp, on=["SKU_PRODUCTO", "COD_CCOSTO"], how="left")

        # SEMANA_1..5: rank por recencia (solo semanas con venta)
        top5_ranked = top5.copy()
        top5_ranked["RANK"] = (
            top5_ranked.groupby(["SKU_PRODUCTO", "COD_CCOSTO"])["SEMANA"]
            .rank(method="first", ascending=False).astype(int)
        )
        sw_pivot = top5_ranked.pivot_table(
            index=["SKU_PRODUCTO", "COD_CCOSTO"], columns="RANK",
            values="UNIDADES", aggfunc="sum",
        ).reset_index()
        sw_pivot.columns.name = None
        sw_pivot.columns = (
            ["SKU_PRODUCTO", "COD_CCOSTO"]
            + [f"SEMANA_{int(c)}" for c in sw_pivot.columns[2:]]
        )
        for i in range(1, 6):
            if f"SEMANA_{i}" not in sw_pivot.columns:
                sw_pivot[f"SEMANA_{i}"] = float("nan")
        sw_pivot = sw_pivot[["SKU_PRODUCTO", "COD_CCOSTO"] + [f"SEMANA_{i}" for i in range(1, 6)]]
        stats = stats.merge(sw_pivot, on=["SKU_PRODUCTO", "COD_CCOSTO"], how="left")
    else:
        stats["FORECAST_SUGERIDO_CRONOLOGICO"] = float("nan")
        stats["VENTA_SEMANAL_PROMEDIO"]        = float("nan")
        stats["FORECAST_SUGERIDO"]             = float("nan")
        for i in range(1, 6):
            stats[f"SEMANA_{i}"] = float("nan")

    # VENTA_MES_ACTUAL = suma MTD del mes en curso
    if not vdf_mtd.empty:
        venta_mes_actual = vdf_mtd.groupby(
            ["SKU_PRODUCTO", "COD_CCOSTO"], as_index=False,
        ).agg(VENTA_MES_ACTUAL=("UNIDADES", "sum"))
        stats = stats.merge(venta_mes_actual, on=["SKU_PRODUCTO", "COD_CCOSTO"], how="left")
    else:
        stats["VENTA_MES_ACTUAL"] = 0

    stats["VENTA_MES_ACTUAL"]              = stats["VENTA_MES_ACTUAL"].fillna(0).astype(int)
    stats["VENTA_SEMANAL_PROMEDIO"]        = stats.get("VENTA_SEMANAL_PROMEDIO",        pd.Series(dtype=float)).fillna(float("nan"))
    stats["FORECAST_SUGERIDO"]             = stats.get("FORECAST_SUGERIDO",             pd.Series(dtype=float)).fillna(float("nan"))
    stats["FORECAST_SUGERIDO_CRONOLOGICO"] = stats.get("FORECAST_SUGERIDO_CRONOLOGICO", pd.Series(dtype=float)).fillna(float("nan"))
    stats["MEDIA_4M_AJUSTADO"]             = stats.get("MEDIA_4M_AJUSTADO",             pd.Series(dtype=float)).fillna(float("nan"))
    if "TIPO" not in stats.columns:
        stats["TIPO"] = "NUEVO"

    # Nombre de tienda
    stats["CENTRO_COSTO"] = stats["COD_CCOSTO"].map(nombre_map).fillna("Sin nombre")

    # Canal dominante por (SKU, CCOSTO)
    canal_dom = (
        vdf.groupby(["SKU_PRODUCTO", "COD_CCOSTO", "COD_CANAL"])
        .agg(TOTAL_UND=("UNIDADES", "sum"))
        .reset_index()
        .sort_values("TOTAL_UND", ascending=False)
        .drop_duplicates(subset=["SKU_PRODUCTO", "COD_CCOSTO"])
    )
    canal_dom["CANAL"] = canal_dom["COD_CANAL"].map(_CANAL_MAP).fillna("OTRO")
    stats = stats.merge(
        canal_dom[["SKU_PRODUCTO", "COD_CCOSTO", "CANAL"]],
        on=["SKU_PRODUCTO", "COD_CCOSTO"], how="left",
    )

    return stats


def _classify_alert(
    fc: float,
    media: float,
    umbral_alerta: float,
    umbral_advertencia: float,
    venta_mes_actual: float = 0,
) -> str:
    """Clasifica basado en ratio FC / Media historica y venta real del mes.

    SIN DATOS    : forecast nulo/cero o sin historial de ventas
    ALERTA       : FC > Media * RATIO_ALERTA  (sobreestimacion extrema)
                   o FC < VENTA_MES_ACTUAL    (subestimacion — ya se esta vendiendo mas)
    ADVERTENCIA  : Media * RATIO_ADVERTENCIA < FC <= Media * RATIO_ALERTA
    OK           : FC <= Media * RATIO_ADVERTENCIA  y  FC >= VENTA_MES_ACTUAL
    """
    if pd.isna(fc) or fc <= 0 or pd.isna(media) or media <= 0:
        return "SIN DATOS"
    # Subestimacion: el forecast ya es menor a lo vendido en el mes actual
    vma = venta_mes_actual if not pd.isna(venta_mes_actual) else 0
    if vma > 0 and fc < vma:
        return "ALERTA"
    if fc > umbral_alerta:
        return "ALERTA"
    if fc > umbral_advertencia:
        return "ADVERTENCIA"
    return "OK"


def _build_alert_table(
    forecast_df: pd.DataFrame,
    stats_df: pd.DataFrame,
    maestra_df: pd.DataFrame,
    periodos_sel: list,
) -> pd.DataFrame:
    fc = forecast_df[forecast_df["PERIODO"].isin(periodos_sel)].copy() if periodos_sel else forecast_df.copy()

    if fc.empty or stats_df.empty:
        return pd.DataFrame()

    # ── Join forecast → stats ─────────────────────────────────────────────────
    # Prioridad 1: si el archivo tiene COD_CCOSTO (tienda-nivel), join exacto
    # Prioridad 2: si es por CANAL (total canal), join por canal dominante
    fc_has_ccosto = "COD_CCOSTO" in fc.columns

    if fc_has_ccosto:
        fc_cols = ["SKU_PRODUCTO", "COD_CCOSTO", "PERIODO", "FC_UND"]
        merged = stats_df.merge(
            fc[fc_cols],
            on=["SKU_PRODUCTO", "COD_CCOSTO"], how="left",
        )
    else:
        fc_cols = ["SKU_PRODUCTO", "CANAL", "PERIODO", "FC_UND"]
        merged = stats_df.merge(
            fc[fc_cols],
            on=["SKU_PRODUCTO", "CANAL"], how="left",
        )

    # Fallback: si no hay match, usar total SKU (suma de todos los canales/tiendas)
    fc_sku = fc.groupby(["SKU_PRODUCTO", "PERIODO"], as_index=False).agg(
        FC_UND_TOTAL=("FC_UND", "sum")
    )
    mask_sin = merged["FC_UND"].isna()
    if mask_sin.any():
        fb = merged[mask_sin].drop(columns=["FC_UND"]).merge(
            fc_sku.rename(columns={"FC_UND_TOTAL": "FC_UND"}),
            on=["SKU_PRODUCTO", "PERIODO"], how="left",
        )
        merged = pd.concat([merged[~mask_sin], fb], ignore_index=True)

    for col in ["MEDIA_4M", "STD_4M", "UMBRAL_ADVERTENCIA", "UMBRAL_ALERTA"]:
        merged[col] = merged[col].fillna(0)

    merged["ESTADO"] = merged.apply(
        lambda r: _classify_alert(
            r["FC_UND"], r["MEDIA_4M"], r["UMBRAL_ALERTA"], r["UMBRAL_ADVERTENCIA"],
            r.get("VENTA_MES_ACTUAL", 0),
        ),
        axis=1,
    )
    merged["SEMAFORO"] = merged["ESTADO"].map(_ESTADO_ICON)
    merged["DIFF_VS_ADVERTENCIA"] = merged["FC_UND"] - merged["UMBRAL_ADVERTENCIA"]
    merged["DIFF_VS_ALERTA"]      = merged["FC_UND"] - merged["UMBRAL_ALERTA"]
    # Ratio FC / Media (el indicador principal del metodo)
    merged["RATIO_FC_MEDIA"] = np.where(
        merged["MEDIA_4M"] > 0,
        (merged["FC_UND"] / merged["MEDIA_4M"]).round(2),
        np.nan,
    )
    merged["DESV_VS_MEDIA_PCT"] = np.where(
        merged["MEDIA_4M"] > 0,
        ((merged["FC_UND"] / merged["MEDIA_4M"] - 1) * 100).round(1),
        np.nan,
    )

    # Enriquecer con maestra
    maestra_cols = ["SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
                    "SKU_NOM_PRODUCTO", "MIX_OFICIAL", "PROCEDENCIA"]
    available = [c for c in maestra_cols if c in maestra_df.columns]
    if available:
        m_dedup = maestra_df[available].drop_duplicates(subset=["SKU_PRODUCTO"])
        merged = merged.merge(m_dedup, on="SKU_PRODUCTO", how="left")

    return merged


# ============================================================================
# README & EXCEL EXPORT
# ============================================================================

_README_ROWS = [
    # (COLUMNA, DESCRIPCION, CALCULO / FUENTE)
    ("SEMAFORO",              "Icono visual del estado de alerta",
     "🔴 ALERTA  |  🟡 ADVERTENCIA  |  🟢 OK  |  ⚪ SIN DATOS"),
    ("ESTADO",                "Clasificacion textual de la alerta",
     "Derivado del ratio FC_UND / MEDIA_4M (ver umbrales abajo)"),
    ("SKU_PRODUCTO",          "Codigo del producto",
     "Identificador unico del SKU en el sistema"),
    ("COD_CCOSTO",            "Codigo del Centro de Costo / Sucursal",
     "4 digitos. Para MAYORISTA/ETAIL = cod_ccosto; para TIENDA = cod_agencia"),
    ("CENTRO_COSTO",          "Nombre del Centro de Costo",
     "Descripcion de la sucursal segun maestro"),
    ("CANAL",                 "Canal de distribucion dominante del SKU en ese CC",
     "TIENDA (03) | MAYORISTA (02) | ETAIL (06)"),
    ("PERIODO",               "Mes del forecast evaluado",
     "Primer dia del mes, formato YYYY-MM-DD"),
    ("FC_UND",                "Forecast en unidades para el periodo seleccionado",
     "Extraido del archivo Excel de forecast cargado (Syncro)"),
    ("VENTA_MES_ACTUAL",      "Venta acumulada del mes en curso (MTD)",
     "Suma de unidades vendidas desde el 1er dia del mes actual hasta hoy"),
    ("SEMANA_1",              "Venta de la semana mas reciente con ventas > 0",
     "Semana mas reciente (rank 1) de las ultimas 8 semanas con unidades > 0"),
    ("SEMANA_2",              "Venta de la 2da semana mas reciente con ventas > 0",
     "Semana rank 2 de las ultimas 8 semanas con unidades > 0"),
    ("SEMANA_3",              "Venta de la 3ra semana mas reciente con ventas > 0",
     "Semana rank 3 de las ultimas 8 semanas con unidades > 0"),
    ("SEMANA_4",              "Venta de la 4ta semana mas reciente con ventas > 0",
     "Semana rank 4 de las ultimas 8 semanas con unidades > 0"),
    ("SEMANA_5",              "Venta de la 5ta semana mas reciente con ventas > 0",
     "Semana rank 5 de las ultimas 8 semanas con unidades > 0"),
    ("VENTA_SEMANAL_PROMEDIO","Promedio de las 5 ultimas semanas con venta > 0",
     "Media de SEMANA_1 a SEMANA_5 (semanas sin ventas no se cuentan)"),
    ("FORECAST_SUGERIDO",     "Proyeccion mensual sugerida basada en tendencia semanal",
     "VENTA_SEMANAL_PROMEDIO × 4"),
    ("MEDIA_4M",              "Promedio mensual de ventas reales de los ultimos 4 meses cerrados",
     "Media de unidades vendidas en los 4 meses previos al mes actual"),
    ("RATIO_FC_MEDIA",        "Ratio entre el forecast y el promedio historico",
     "FC_UND / MEDIA_4M  (1.0x = igual al promedio; 2.0x = doble)"),
    ("DESV_VS_MEDIA_PCT",     "Desviacion porcentual del forecast respecto a la media historica",
     "(FC_UND / MEDIA_4M − 1) × 100%"),
    ("UMBRAL_ADVERTENCIA",    f"Umbral de advertencia: forecast supera +50 % el promedio",
     f"MEDIA_4M × {_RATIO_ADVERTENCIA}"),
    ("UMBRAL_ALERTA",         f"Umbral de alerta: forecast supera +100 % el promedio (doble)",
     f"MEDIA_4M × {_RATIO_ALERTA}"),
    ("DIFF_VS_ADVERTENCIA",   "Exceso del forecast sobre el umbral de advertencia",
     "FC_UND − UMBRAL_ADVERTENCIA  (negativo = no supera el umbral)"),
    ("DIFF_VS_ALERTA",        "Exceso del forecast sobre el umbral de alerta",
     "FC_UND − UMBRAL_ALERTA  (negativo = no supera el umbral)"),
]

_README_ESTADOS = [
    ("🟢 OK",          f"FC / Media ≤ {_RATIO_ADVERTENCIA}x  y  FC ≥ Venta MTD",
     f"El forecast no supera el {int((_RATIO_ADVERTENCIA-1)*100)} % sobre el promedio y cubre la venta del mes. Sin accion requerida."),
    ("🟡 ADVERTENCIA", f"{_RATIO_ADVERTENCIA}x < FC / Media ≤ {_RATIO_ALERTA}x",
     f"El forecast supera el {int((_RATIO_ADVERTENCIA-1)*100)} % sobre el promedio. Revisar si hay justificacion."),
    ("🔴 ALERTA",      f"FC / Media > {_RATIO_ALERTA}x  o  FC < Venta MTD",
     f"Sobreestimacion: forecast mas del doble del promedio. "
     f"Subestimacion: forecast ya por debajo de lo vendido en el mes. Corregir antes de aprobar."),
    ("⚪ SIN DATOS",   "FC nulo/cero o sin historial de ventas",
     "No se puede calcular el ratio. Verificar si el SKU tiene historial de ventas en ese CC."),
]


def _build_readme_df() -> "tuple[pd.DataFrame, pd.DataFrame]":
    """Devuelve (df_columnas, df_estados) para la hoja README del Excel."""
    df_col = pd.DataFrame(
        _README_ROWS,
        columns=["COLUMNA", "DESCRIPCION", "CALCULO / FUENTE"],
    )
    df_est = pd.DataFrame(
        _README_ESTADOS,
        columns=["ESTADO", "CONDICION", "INTERPRETACION"],
    )
    return df_col, df_est


def _build_excel_alerta(df_export: pd.DataFrame) -> io.BytesIO:
    """Genera Excel con dos hojas: 'Alerta Forecast' y 'README'."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # ── Hoja principal ────────────────────────────────────────────────────
        df_export.to_excel(writer, sheet_name="Alerta Forecast", index=False)
        ws_data = writer.sheets["Alerta Forecast"]
        # Ajustar ancho de columnas automaticamente
        for col_cells in ws_data.columns:
            max_len = max(
                len(str(cell.value)) if cell.value is not None else 0
                for cell in col_cells
            )
            ws_data.column_dimensions[col_cells[0].column_letter].width = min(max_len + 3, 40)

        # ── Hoja README ───────────────────────────────────────────────────────
        df_col, df_est = _build_readme_df()

        # Titulo general
        readme_rows: list[list] = []
        readme_rows.append(["ALERTA FORECAST — Glosario de columnas y estados"])
        readme_rows.append([f"Generado el {datetime.now().strftime('%Y-%m-%d %H:%M')}"])
        readme_rows.append([])
        readme_rows.append(["DEFINICION DE COLUMNAS"])
        readme_rows.append(list(df_col.columns))
        for r in df_col.itertuples(index=False):
            readme_rows.append(list(r))
        readme_rows.append([])
        readme_rows.append(["ESTADOS Y CRITERIOS DE ALERTA"])
        readme_rows.append(list(df_est.columns))
        for r in df_est.itertuples(index=False):
            readme_rows.append(list(r))

        ws_readme = writer.book.create_sheet("README")
        for row in readme_rows:
            ws_readme.append(row)

        # Estilo basico: negrita en encabezados, ancho de columnas
        from openpyxl.styles import Font, PatternFill, Alignment
        _AZUL  = "FF065E8B"  # azul Dorel
        _VERDE = "FF43A047"
        header_rows_idx = {1, 2, 4, 5, len(readme_rows) - len(df_est) - 2,
                           len(readme_rows) - len(df_est) - 1}
        for i, row in enumerate(ws_readme.iter_rows(), start=1):
            for cell in row:
                if cell.value:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                if i in {1}:
                    cell.font = Font(bold=True, size=13, color="FFFFFFFF")
                    cell.fill = PatternFill("solid", fgColor=_AZUL)
                elif i in {4, len(readme_rows) - len(df_est) - 2}:
                    cell.font = Font(bold=True, size=11, color="FFFFFFFF")
                    cell.fill = PatternFill("solid", fgColor=_AZUL)
                elif i in {5, len(readme_rows) - len(df_est) - 1}:
                    cell.font = Font(bold=True)
                    cell.fill = PatternFill("solid", fgColor="FFE3F0FB")

        ws_readme.column_dimensions["A"].width = 28
        ws_readme.column_dimensions["B"].width = 52
        ws_readme.column_dimensions["C"].width = 60

    buf.seek(0)
    return buf


# ============================================================================
# KPI CARDS
# ============================================================================

def _render_kpis(df: pd.DataFrame):
    total       = df[["SKU_PRODUCTO", "COD_CCOSTO"]].drop_duplicates().shape[0]
    en_alerta   = df[df["ESTADO"] == "ALERTA"][["SKU_PRODUCTO", "COD_CCOSTO"]].drop_duplicates().shape[0]
    advertencia = df[df["ESTADO"] == "ADVERTENCIA"][["SKU_PRODUCTO", "COD_CCOSTO"]].drop_duplicates().shape[0]
    ok          = df[df["ESTADO"] == "OK"][["SKU_PRODUCTO", "COD_CCOSTO"]].drop_duplicates().shape[0]
    sin_datos   = df[df["ESTADO"] == "SIN DATOS"][["SKU_PRODUCTO", "COD_CCOSTO"]].drop_duplicates().shape[0]

    c1, c2, c3, c4, c5 = st.columns(5)

    def _kpi(col, val, label, color):
        col.html(f"""
        <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                     border-top:4px solid {color};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
            <div style='font-size:2rem;font-weight:bold;color:{color}'>{val:,}</div>
            <div style='font-size:0.8rem;color:#B0AEAA;text-transform:uppercase'>{label}</div>
        </div>""")

    _kpi(c1, total,       "SKU x C. Costo",       COLORS["primary"])
    _kpi(c2, en_alerta,   "ALERTA (>5\u03c3)",     _ESTADO_COLORS["ALERTA"])
    _kpi(c3, advertencia, "ADVERTENCIA (>2\u03c3)", _ESTADO_COLORS["ADVERTENCIA"])
    _kpi(c4, ok,          "OK",                    _ESTADO_COLORS["OK"])
    _kpi(c5, sin_datos,   "Sin Forecast",          _ESTADO_COLORS["SIN DATOS"])
    st.html("<br>")


# ============================================================================
# CHARTS
# ============================================================================

def _render_distribucion(df: pd.DataFrame):
    counts = (
        df.drop_duplicates(["SKU_PRODUCTO", "COD_CCOSTO"])["ESTADO"]
        .value_counts().reset_index()
    )
    counts.columns = ["Estado", "Cantidad"]

    fig = go.Figure(layout=dorel_layout(
        height=320, margin=dict(l=20, r=20, t=40, b=20), showlegend=True,
        title_text="Distribucion por Estado (SKU x C.Costo)",
    ))
    fig.add_trace(go.Pie(
        labels=counts["Estado"].tolist(),
        values=counts["Cantidad"].tolist(),
        hole=0.5,
        marker=dict(colors=[_ESTADO_COLORS.get(s, "#999") for s in counts["Estado"]]),
        textinfo="percent+label",
        hovertemplate="Estado: %{label}<br>SKU x C.Costo: %{value:,}<extra></extra>",
    ))
    st.plotly_chart(fig, use_container_width=True)


def _render_top_alertas(df: pd.DataFrame):
    alertas = df[df["ESTADO"] == "ALERTA"].copy()
    if alertas.empty:
        st.info("No hay combinaciones SKU x C.Costo en ALERTA.")
        return

    cc_col     = "CENTRO_COSTO"      if "CENTRO_COSTO"      in alertas.columns else "COD_CCOSTO"
    nombre_col = "SKU_NOM_PRODUCTO"  if "SKU_NOM_PRODUCTO"  in alertas.columns else "SKU_PRODUCTO"

    top = alertas.nlargest(20, "DIFF_VS_ALERTA")
    top["LABEL"] = (
        top["SKU_PRODUCTO"].astype(str)
        + " | " + top.get(nombre_col, top["SKU_PRODUCTO"]).astype(str).str[:25]
        + " | " + top[cc_col].astype(str).str[:20]
    )
    top["LABEL"] = top["LABEL"].str[:65]
    top_sorted = top.sort_values("DIFF_VS_ALERTA", ascending=True)

    fig = go.Figure(layout=dorel_layout(
        height=520,
        title_text="Top 20 SKU x C.Costo en ALERTA (exceso sobre umbral 5\u03c3)",
        xaxis_title="Exceso sobre Umbral ALERTA (unidades)",
        yaxis_title="",
    ))
    fig.add_trace(go.Bar(
        x=top_sorted["DIFF_VS_ALERTA"],
        y=top_sorted["LABEL"],
        orientation="h",
        marker_color=_ESTADO_COLORS["ALERTA"],
        customdata=np.column_stack([
            top_sorted["FC_UND"],
            top_sorted["UMBRAL_ALERTA"],
            top_sorted["UMBRAL_ADVERTENCIA"],
            top_sorted["MEDIA_4M"],
            top_sorted[cc_col],
            top_sorted["CANAL"],
        ]),
        hovertemplate=(
            "%{y}<br>"
            "Forecast: %{customdata[0]:,.0f} und<br>"
            "Umbral ALERTA (5\u03c3): %{customdata[1]:,.0f}<br>"
            "Umbral ADVERTENCIA (2\u03c3): %{customdata[2]:,.0f}<br>"
            "Media 4m: %{customdata[3]:,.0f}<br>"
            "Centro Costo: %{customdata[4]}<br>"
            "Canal: %{customdata[5]}<br>"
            "Exceso: %{x:,.0f}<extra></extra>"
        ),
    ))
    st.plotly_chart(fig, use_container_width=True)


def _render_scatter(df: pd.DataFrame):
    sub = df[df["ESTADO"].isin(["ALERTA", "ADVERTENCIA", "OK"])].copy()
    if sub.empty:
        return

    cc_col     = "CENTRO_COSTO"     if "CENTRO_COSTO"     in sub.columns else "COD_CCOSTO"
    nombre_col = "SKU_NOM_PRODUCTO" if "SKU_NOM_PRODUCTO" in sub.columns else "SKU_PRODUCTO"

    fig = go.Figure(layout=dorel_layout(
        height=420,
        title_text="Forecast vs Umbral ADVERTENCIA (media + 2\u03c3) por SKU x C.Costo",
        xaxis_title="Umbral ADVERTENCIA [unidades]",
        yaxis_title="Forecast [unidades]",
    ))
    for estado in ["ALERTA", "ADVERTENCIA", "OK"]:
        s = sub[sub["ESTADO"] == estado]
        if s.empty:
            continue
        fig.add_trace(go.Scatter(
            x=s["UMBRAL_ADVERTENCIA"],
            y=s["FC_UND"],
            mode="markers",
            name=estado,
            marker=dict(color=_ESTADO_COLORS[estado], size=7, opacity=0.75),
            customdata=np.column_stack([
                s["SKU_PRODUCTO"],
                s.get(nombre_col, s["SKU_PRODUCTO"]),
                s[cc_col],
                s["CANAL"],
                s["MEDIA_4M"],
                s["UMBRAL_ALERTA"],
            ]),
            hovertemplate=(
                "SKU: %{customdata[0]}<br>"
                "Nombre: %{customdata[1]}<br>"
                "Centro Costo: %{customdata[2]}<br>"
                "Canal: %{customdata[3]}<br>"
                "Forecast: %{y:,.0f}<br>"
                "Umbral Adv (2\u03c3): %{x:,.0f}<br>"
                "Umbral Alerta (5\u03c3): %{customdata[5]:,.0f}<br>"
                "Media 4m: %{customdata[4]:,.0f}<extra></extra>"
            ),
        ))

    _max = max(sub["UMBRAL_ADVERTENCIA"].max(), sub["FC_UND"].max(), 1)
    fig.add_trace(go.Scatter(
        x=[0, _max], y=[0, _max], mode="lines",
        name="FC = Umbral", line=dict(color="#aaa", dash="dash"), hoverinfo="skip",
    ))
    st.plotly_chart(fig, use_container_width=True)


# ============================================================================
# PANEL DE DEFINICIONES
# ============================================================================

def _render_definiciones():
    with st.expander("Definicion de estados — como interpretar las alertas", expanded=False):
        adv_pct = int((_RATIO_ADVERTENCIA - 1) * 100)
        alt_pct = int((_RATIO_ALERTA      - 1) * 100)
        st.html(f"""
        <div style='font-family:sans-serif;font-size:0.9rem;line-height:1.8'>
          <p style='margin:0 0 0.8rem'>
            El sistema compara el <strong>forecast cargado</strong> contra el
            <strong>promedio de venta real de los ultimos 4 meses cerrados</strong>
            y contra la <strong>venta acumulada del mes actual (MTD)</strong>
            por cada combinacion <em>SKU x Centro de Costo</em>.
            Ejemplo sobreestimacion: media = 10 und → forecast = 25 → ratio = 2.5x → 🔴 ALERTA.<br>
            Ejemplo subestimacion: venta MTD = 80 und → forecast = 50 → 🔴 ALERTA (ya se vendio mas de lo forecasted).
          </p>
          <div style='display:flex;gap:1rem;flex-wrap:wrap'>
            <div style='flex:1;min-width:200px;background:#E8F5E9;border-left:5px solid #43A047;
                        padding:0.8rem;border-radius:6px'>
              <div style='font-weight:700;color:#43A047'>🟢 OK</div>
              <div>FC / Media &le; {_RATIO_ADVERTENCIA}x &nbsp;y&nbsp; FC &ge; Venta MTD</div>
              <div style='color:#555;font-size:0.82rem;margin-top:0.3rem'>
                El forecast no supera el {adv_pct}% sobre el promedio y cubre la venta actual. Sin accion.
              </div>
            </div>
            <div style='flex:1;min-width:200px;background:#FFF8E1;border-left:5px solid #FB8C00;
                        padding:0.8rem;border-radius:6px'>
              <div style='font-weight:700;color:#FB8C00'>🟡 ADVERTENCIA</div>
              <div>{_RATIO_ADVERTENCIA}x &lt; FC / Media &le; {_RATIO_ALERTA}x</div>
              <div style='color:#555;font-size:0.82rem;margin-top:0.3rem'>
                El forecast supera el {adv_pct}% sobre el promedio.
                <strong>Revisar</strong> si hay un evento o justificacion.
              </div>
            </div>
            <div style='flex:1;min-width:200px;background:#FFEBEE;border-left:5px solid #E53935;
                        padding:0.8rem;border-radius:6px'>
              <div style='font-weight:700;color:#E53935'>🔴 ALERTA</div>
              <div>FC / Media &gt; {_RATIO_ALERTA}x &nbsp;<em>o</em>&nbsp; FC &lt; Venta MTD</div>
              <div style='color:#555;font-size:0.82rem;margin-top:0.3rem'>
                <strong>Sobreestimacion:</strong> forecast mas del doble del promedio historico.<br>
                <strong>Subestimacion:</strong> forecast ya por debajo de lo vendido en el mes.
                Corregir antes de aprobar.
              </div>
            </div>
          </div>
          <p style='margin:0.8rem 0 0;color:#888;font-size:0.8rem'>
            Metodo: ratio FC / Media 4m. &nbsp;|&nbsp;
            Umbral ADVERTENCIA = Media &times; {_RATIO_ADVERTENCIA} &nbsp;|&nbsp;
            Umbral ALERTA = Media &times; {_RATIO_ALERTA} &nbsp;|&nbsp;
            Columna <em>DESV_VS_MEDIA_PCT</em> = (FC/Media - 1) &times; 100%
          </p>
        </div>
        """)


# ============================================================================
# RESUMEN POR TIENDA
# ============================================================================

def _render_resumen_tienda(df: pd.DataFrame):
    """Tabla resumen de alertas agrupada por Centro de Costo."""
    st.markdown("### Resumen por Centro de Costo")

    group_cols = ["COD_CCOSTO"]
    if "CENTRO_COSTO" in df.columns:
        group_cols.append("CENTRO_COSTO")

    # Contar combinaciones unicas SKU x CCOSTO por estado
    base = df.drop_duplicates(["SKU_PRODUCTO", "COD_CCOSTO", "PERIODO"]) if "PERIODO" in df.columns \
           else df.drop_duplicates(["SKU_PRODUCTO", "COD_CCOSTO"])

    counts = (
        base.groupby(group_cols + ["ESTADO"])
        .size()
        .reset_index(name="N")
    )
    pivot = counts.pivot_table(
        index=group_cols, columns="ESTADO", values="N", fill_value=0
    ).reset_index()
    pivot.columns.name = None

    for estado in _ESTADO_ORDER:
        if estado not in pivot.columns:
            pivot[estado] = 0

    pivot["TOTAL_SKU"] = pivot[_ESTADO_ORDER].sum(axis=1)
    pivot["% ALERTA+ADV"] = (
        (pivot.get("ALERTA", 0) + pivot.get("ADVERTENCIA", 0))
        / pivot["TOTAL_SKU"].replace(0, pd.NA) * 100
    ).round(1)

    pivot = pivot.sort_values("ALERTA", ascending=False).reset_index(drop=True)

    rename_map = {
        "COD_CCOSTO":   "Cod. C.Costo",
        "CENTRO_COSTO": "Centro de Costo",
        "ALERTA":       "🔴 ALERTA",
        "ADVERTENCIA":  "🟡 ADVERTENCIA",
        "OK":           "🟢 OK",
        "SIN DATOS":    "⚪ SIN DATOS",
        "TOTAL_SKU":    "Total SKU-CC",
        "% ALERTA+ADV": "% Alerta+Adv",
    }
    pivot.rename(columns=rename_map, inplace=True)

    col_order = ["Cod. C.Costo", "Centro de Costo",
                 "🔴 ALERTA", "🟡 ADVERTENCIA", "🟢 OK", "⚪ SIN DATOS",
                 "Total SKU-CC", "% Alerta+Adv"]
    col_order = [c for c in col_order if c in pivot.columns]

    st.dataframe(pivot[col_order], use_container_width=True, hide_index=True)
    download_buttons(pivot[col_order], "resumen_ccosto_alerta_forecast")


# ============================================================================
# FORECAST SOURCE SELECTOR
# ============================================================================

def _render_forecast_source() -> "tuple[pd.DataFrame | None, str]":
    """Muestra selector de fuente de forecast.

    Prioridad:
    1. Si hay snapshots guardados (Forecast Accuracy), ofrece usarlos directamente.
    2. Si no, o si el usuario lo prefiere, pide subir un Excel.

    Devuelve (forecast_df, nombre_archivo) o (None, "").
    """
    snapshots = _list_snapshots()

    st.markdown("### 1. Fuente del Forecast")

    if snapshots:
        latest = snapshots[0]
        uploaded_at = latest.get("uploaded_at", "?")[:10]
        n_skus      = latest.get("n_skus", "?")
        mr          = latest.get("month_range", ["?", "?"])
        filename    = latest.get("original_filename", latest["key"])

        st.info(
            f"**Forecast disponible:** `{filename}`  \n"
            f"Subido el {uploaded_at} · {n_skus:,} SKUs · "
            f"Rango: {mr[0]} – {mr[1]}"
        )

        opciones = [f"Usar: {s['original_filename']} ({s.get('uploaded_at','?')[:10]})"
                    for s in snapshots]
        opciones.append("Subir nuevo archivo Excel")

        sel = st.radio("Seleccionar fuente:", opciones, key="af_fuente_radio")

        if sel == "Subir nuevo archivo Excel":
            uploaded = st.file_uploader(
                "Archivo de forecast (Excel Syncro: SKU x CANAL x meses)",
                type=["xlsx", "xls"],
                key="af_upload_new",
            )
            if uploaded is None:
                st.info("Sube el archivo para continuar.")
                return None, ""
            df, err = _parse_forecast_excel(uploaded)
            if err:
                st.error(f"Error al parsear el forecast: {err}")
                return None, ""
            return df, uploaded.name

        else:
            # Determinar cuál snapshot fue seleccionado
            idx = opciones.index(sel)
            snap = snapshots[idx]
            df = _load_snapshot(snap["key"])
            if df is None:
                st.error("No se pudo cargar el snapshot seleccionado.")
                return None, ""
            # El parquet ya tiene SKU_PRODUCTO, CANAL, PERIODO, FC_UND
            df = norm_cols(df)
            if "PERIODO" in df.columns:
                df["PERIODO"] = pd.to_datetime(df["PERIODO"])
            if "FC_UND" not in df.columns:
                # buscar columna de forecast
                for candidate in ["FC_UND", "FORECAST", "FCST", "CANTIDAD", "UNIDADES"]:
                    if candidate in df.columns:
                        df = df.rename(columns={candidate: "FC_UND"})
                        break
            if "CANAL" not in df.columns:
                df["CANAL"] = "TOTAL"
            return df, snap.get("original_filename", snap["key"])

    else:
        # Sin snapshots guardados → solo uploader
        st.caption("No hay snapshots de Forecast Accuracy guardados. Sube un archivo Excel.")
        uploaded = st.file_uploader(
            "Archivo de forecast (Excel Syncro: SKU x CANAL x meses)",
            type=["xlsx", "xls"],
            key="af_upload_only",
        )
        if uploaded is None:
            st.info("Sube un archivo de forecast para continuar.")
            return None, ""
        df, err = _parse_forecast_excel(uploaded)
        if err:
            st.error(f"Error al parsear el forecast: {err}")
            return None, ""
        return df, uploaded.name


# ============================================================================
# MAIN RENDER
# ============================================================================

def render_alerta_forecast(conn):
    """Entry point: dashboard de Alerta Forecast."""
    st.html("<h2 class='sub-header'>Alerta Forecast</h2>")
    adv_pct = int((_RATIO_ADVERTENCIA - 1) * 100)
    alt_pct = int((_RATIO_ALERTA      - 1) * 100)
    st.caption(
        "Compara el forecast cargado contra el promedio de venta real de los ultimos 4 meses por SKU x Centro de Costo. "
        f"Metodo: ratio FC / Media.  "
        f"ADVERTENCIA = forecast > {_RATIO_ADVERTENCIA}x el promedio (+{adv_pct}%)  |  "
        f"ALERTA = forecast > {_RATIO_ALERTA}x el promedio (+{alt_pct}%)."
    )

    _render_definiciones()

    # ── 1. Fuente del forecast (siempre visible) ──────────────────────────────
    forecast_df, fc_filename = _render_forecast_source()

    if forecast_df is None or forecast_df.empty:
        return

    # ── 2. Cargar datos base de ventas ────────────────────────────────────────
    with lottie_spinner("snowflake"):
        ventas_raw   = cq.alerta_forecast_ventas(conn)
        semanas_raw  = cq.alerta_vta_semanal(conn)
        maestra_df   = cq.maestra(conn)
        stock_men_df = cq.alerta_stock_tienda_mensual(conn)
        perfil_raw   = cq.perfil_sku_ccosto(conn)

    maestra_df = apply_pm_filter(maestra_df)

    if ventas_raw.empty:
        st.warning(
            "No se encontraron ventas de tiendas en los ultimos 4 meses cerrados. "
            "Verificar que el VCM tenga registros con cod_canal '03' (TIENDA) en ese periodo."
        )
        return

    ventas_raw["PERIODO"] = pd.to_datetime(ventas_raw["PERIODO"])

    # ── 3. Estadisticas por SKU x CCOSTO ─────────────────────────────────────
    stats_df = _build_stats(
        ventas_raw,
        semanas_df=semanas_raw  if not semanas_raw.empty  else None,
        stock_df  =stock_men_df if not stock_men_df.empty else None,
    )

    periodos_fc = sorted(forecast_df["PERIODO"].unique())
    st.success(
        f"Forecast cargado: **{fc_filename}** · "
        f"{forecast_df['SKU_PRODUCTO'].nunique():,} SKUs · "
        f"{len(periodos_fc)} mes(es) "
        f"({periodos_fc[0].strftime('%b %Y')} – {periodos_fc[-1].strftime('%b %Y')})"
    )

    # ── Seleccion de periodo(s) ──────────────────────────────────────────────
    st.markdown("### 3. Seleccionar Periodo(s) a Comparar")
    periodos_str = [p.strftime("%b %Y") for p in periodos_fc]
    sel_periodos_str = st.multiselect(
        "Periodos del forecast a evaluar",
        periodos_str,
        default=periodos_str[:1] if periodos_str else [],
        key="af_periodos",
        help="Selecciona uno o mas meses del forecast para comparar contra el umbral.",
    )
    sel_periodos = [pd.Timestamp(p) for p in periodos_fc if p.strftime("%b %Y") in sel_periodos_str]

    if not sel_periodos:
        st.warning("Selecciona al menos un periodo del forecast.")
        return

    # ── Construir tabla de alertas ───────────────────────────────────────────
    alert_df = _build_alert_table(forecast_df, stats_df, maestra_df, sel_periodos)

    if alert_df.empty:
        st.warning("No se encontraron coincidencias entre el forecast y las ventas reales.")
        return

    alert_df = apply_pm_filter(alert_df)

    # ── Columna PERFIL: 1 si SKU x COD_CCOSTO tiene perfil en Syncro, 0 si no ─
    if not perfil_raw.empty:
        pf = perfil_raw.copy()
        pf.columns = pf.columns.str.upper()
        pf["SKU_PRODUTO"] = pf["SKU_PRODUTO"].astype(str).str.strip() if "SKU_PRODUTO" in pf.columns else pf.get("SKU_PRODUCTO", pd.Series()).astype(str).str.strip()
        pf["COD_CCOSTO"]  = pf["COD_CCOSTO"].astype(str).str.strip().str.zfill(4)
        pf = pf.rename(columns={"SKU_PRODUTO": "SKU_PRODUCTO"})
        pf["PERFIL"] = 1
        alert_df = alert_df.merge(
            pf[["SKU_PRODUCTO", "COD_CCOSTO", "PERFIL"]].drop_duplicates(),
            on=["SKU_PRODUCTO", "COD_CCOSTO"], how="left",
        )
        alert_df["PERFIL"] = alert_df["PERFIL"].fillna(0).astype(int)
    else:
        alert_df["PERFIL"] = 0

    # ── Filtros ───────────────────────────────────────────────────────────────
    st.markdown("### 4. Filtros")

    f1, f2, f3, f4 = st.columns(4)
    areas = sorted(alert_df["AREA"].dropna().unique()) if "AREA" in alert_df.columns else []
    with f1:
        sel_area = st.multiselect("Area", areas, key="af_area")

    lineas_all = sorted(alert_df["LINEA"].dropna().unique()) if "LINEA" in alert_df.columns else []
    with f2:
        lineas_filt = (
            sorted(alert_df[alert_df["AREA"].isin(sel_area)]["LINEA"].dropna().unique())
            if sel_area and "LINEA" in alert_df.columns else lineas_all
        )
        sel_linea = st.multiselect("Linea", lineas_filt, key="af_linea")

    marcas_all = sorted(alert_df["MARCA"].dropna().unique()) if "MARCA" in alert_df.columns else []
    with f3:
        _m_tmp = alert_df.copy()
        if sel_area:
            _m_tmp = _m_tmp[_m_tmp["AREA"].isin(sel_area)]
        if sel_linea:
            _m_tmp = _m_tmp[_m_tmp["LINEA"].isin(sel_linea)]
        marcas_filt = sorted(_m_tmp["MARCA"].dropna().unique()) if "MARCA" in _m_tmp.columns else marcas_all
        sel_marca = st.multiselect("Marca", marcas_filt, key="af_marca")

    canales_all = sorted(alert_df["CANAL"].dropna().unique()) if "CANAL" in alert_df.columns else []
    with f4:
        sel_canal = st.multiselect("Canal", canales_all, key="af_canal")

    f5, f6, f7, f8 = st.columns(4)
    with f5:
        sel_sku = st.text_input("SKU (busqueda)", key="af_sku", placeholder="Ej: 12345")

    mixes_all = sorted(alert_df["MIX_OFICIAL"].dropna().unique()) if "MIX_OFICIAL" in alert_df.columns else []
    with f6:
        sel_mix = st.multiselect("MIX", mixes_all, key="af_mix")

    cod_ccosto_all = sorted(alert_df["COD_CCOSTO"].dropna().unique()) if "COD_CCOSTO" in alert_df.columns else []
    with f7:
        sel_cod_ccosto = st.multiselect("Cod. C.Costo", cod_ccosto_all, key="af_ccosto")

    centro_costo_all = sorted(alert_df["CENTRO_COSTO"].dropna().unique()) if "CENTRO_COSTO" in alert_df.columns else []
    with f8:
        sel_centro_costo = st.multiselect("Centro de Costo", centro_costo_all, key="af_centro_costo")

    f9, f10 = st.columns([1, 3])
    with f9:
        sel_estado = st.multiselect(
            "Estado",
            _ESTADO_ORDER,
            default=[],
            key="af_estado",
            help="Vacio = mostrar todos los estados",
        )

    # ── Aplicar filtros ──────────────────────────────────────────────────────
    mask = pd.Series(True, index=alert_df.index)
    if sel_area         and "AREA"         in alert_df.columns: mask &= alert_df["AREA"].isin(sel_area)
    if sel_linea        and "LINEA"        in alert_df.columns: mask &= alert_df["LINEA"].isin(sel_linea)
    if sel_marca        and "MARCA"        in alert_df.columns: mask &= alert_df["MARCA"].isin(sel_marca)
    if sel_canal:                                                mask &= alert_df["CANAL"].isin(sel_canal)
    if sel_sku:         mask &= alert_df["SKU_PRODUCTO"].astype(str).str.contains(sel_sku.strip(), case=False)
    if sel_mix          and "MIX_OFICIAL"  in alert_df.columns: mask &= alert_df["MIX_OFICIAL"].isin(sel_mix)
    if sel_cod_ccosto   and "COD_CCOSTO"   in alert_df.columns: mask &= alert_df["COD_CCOSTO"].isin(sel_cod_ccosto)
    if sel_centro_costo and "CENTRO_COSTO" in alert_df.columns: mask &= alert_df["CENTRO_COSTO"].isin(sel_centro_costo)
    if sel_estado:                                               mask &= alert_df["ESTADO"].isin(sel_estado)

    df_filt = alert_df[mask].copy()

    if df_filt.empty:
        # Mostrar distribucion de estados para ayudar al usuario a ajustar filtros
        if not alert_df.empty:
            dist = alert_df["ESTADO"].value_counts().to_dict()
            dist_str = "  |  ".join(
                f"{_ESTADO_ICON.get(k,'?')} {k}: {v:,}" for k, v in dist.items()
            )
            st.warning(
                f"No hay datos con los filtros seleccionados.  \n"
                f"Distribucion total (sin filtros): {dist_str}"
            )
        else:
            st.warning("No hay datos con los filtros seleccionados.")
        return

    st.markdown("---")

    # ── KPIs ─────────────────────────────────────────────────────────────────
    _render_kpis(df_filt)

    # ── Graficos ─────────────────────────────────────────────────────────────
    col_l, col_r = st.columns(2)
    with col_l:
        _render_distribucion(df_filt)
    with col_r:
        _render_top_alertas(df_filt)

    st.markdown("---")
    _render_scatter(df_filt)

    st.markdown("---")

    # ── Resumen por tienda ────────────────────────────────────────────────────
    _render_resumen_tienda(df_filt)

    st.markdown("---")

    # ── Tabla de detalle ─────────────────────────────────────────────────────
    st.markdown("### Detalle por SKU x Centro de Costo")

    display_cols = [
        "SEMAFORO", "ESTADO", "PERFIL", "TIPO", "SKU_PRODUCTO",
        "COD_CCOSTO", "CENTRO_COSTO",
        "CANAL", "PERIODO",
        "FC_UND", "VENTA_MES_ACTUAL",
        "SEMANA_1", "SEMANA_2", "SEMANA_3", "SEMANA_4", "SEMANA_5",
        "VENTA_SEMANAL_PROMEDIO", "FORECAST_SUGERIDO", "FORECAST_SUGERIDO_CRONOLOGICO",
        "MEDIA_4M", "MEDIA_4M_AJUSTADO", "RATIO_FC_MEDIA", "DESV_VS_MEDIA_PCT",
        "UMBRAL_ADVERTENCIA", "UMBRAL_ALERTA",
        "DIFF_VS_ADVERTENCIA", "DIFF_VS_ALERTA",
    ]
    for c in ["AREA", "LINEA", "SUBLINEA", "MARCA", "SKU_NOM_PRODUCTO", "MIX_OFICIAL", "PROCEDENCIA"]:
        if c in df_filt.columns:
            display_cols.insert(3, c)

    display_cols = [c for c in dict.fromkeys(display_cols) if c in df_filt.columns]
    df_display = df_filt[display_cols].sort_values(
        ["ESTADO", "DIFF_VS_ALERTA"], ascending=[True, False],
    ).reset_index(drop=True)

    # Formateo numerico — sin Styler para evitar limite de celdas
    fmt = df_display.copy()
    for col in ["FC_UND", "VENTA_MES_ACTUAL", "MEDIA_4M", "MEDIA_4M_AJUSTADO", "STD_4M",
                "UMBRAL_ADVERTENCIA", "UMBRAL_ALERTA",
                "DIFF_VS_ADVERTENCIA", "DIFF_VS_ALERTA"]:
        if col in fmt.columns:
            fmt[col] = fmt[col].apply(lambda x: f"{x:,.0f}" if pd.notna(x) else "-")
    for col in ["SEMANA_1", "SEMANA_2", "SEMANA_3", "SEMANA_4", "SEMANA_5",
                "VENTA_SEMANAL_PROMEDIO", "FORECAST_SUGERIDO", "FORECAST_SUGERIDO_CRONOLOGICO"]:
        if col in fmt.columns:
            fmt[col] = fmt[col].apply(lambda x: f"{x:,.1f}" if pd.notna(x) else "-")
    if "RATIO_FC_MEDIA" in fmt.columns:
        fmt["RATIO_FC_MEDIA"] = fmt["RATIO_FC_MEDIA"].apply(
            lambda x: f"{x:.2f}x" if pd.notna(x) else "-"
        )
    if "DESV_VS_MEDIA_PCT" in fmt.columns:
        fmt["DESV_VS_MEDIA_PCT"] = fmt["DESV_VS_MEDIA_PCT"].apply(
            lambda x: f"{x:+.1f}%" if pd.notna(x) else "-"
        )
    if "PERIODO" in fmt.columns:
        fmt["PERIODO"] = pd.to_datetime(fmt["PERIODO"]).dt.strftime("%Y-%m-%d")

    # Usar st.dataframe plano (sin Styler) — el icono SEMAFORO da el color visual
    st.dataframe(fmt, use_container_width=True, height=520)

    # ── Descarga Excel multi-hoja (Alerta Forecast + README) ─────────────────
    col_csv, col_xlsx = st.columns(2)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with col_csv:
        st.download_button(
            "Descargar CSV",
            df_display.to_csv(index=False).encode("utf-8-sig"),
            f"alerta_forecast_{timestamp}.csv",
            "text/csv",
        )
    with col_xlsx:
        excel_buf = _build_excel_alerta(df_display)
        st.download_button(
            "Descargar Excel (+ README)",
            excel_buf,
            f"alerta_forecast_{timestamp}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ============================================================================
# CONVERTIR ALERTA FORECAST → FORMATO SYNCRO
# ============================================================================

def render_convertir_forecast():
    """Convierte el Excel de Alerta Forecast al formato Syncro listo para cargar.

    Input : Excel generado por Alerta Forecast (columnas SKU_PRODUCTO,
            COD_CCOSTO, CANAL, PERIODO, FORECAST_SUGERIDO, ...).
    Output: Excel ancho con cabeceras Syncro —
            id_material | descripcion | id_sucursal | descripcion_sucursal
            | canal | MM/YY | MM/YY | ... (24 meses desde el mes de inicio)
    """
    # Mapeo de canal interno → valor Syncro
    _CANAL_SYNCRO = {
        "TIENDA":    "RETAIL",
        "MINORISTA": "RETAIL",
        "RETAIL":    "RETAIL",
        "MAYORISTA": "MAYORISTA",
        "MAYOR":     "MAYORISTA",
        "ETAIL":     "ETAIL",
        "MINOR":     "RETAIL",
        "03":        "RETAIL",
        "02":        "MAYORISTA",
        "06":        "ETAIL",
    }

    st.html("<h2 class='sub-header'>Convertir Alerta → Formato Forecast</h2>")
    st.caption(
        "Sube el Excel generado por la pestaña **Alerta Forecast**. "
        "La columna **FORECAST_SUGERIDO** se convierte al formato Syncro "
        "(id_material × id_sucursal × meses MM/AA) listo para cargar."
    )

    uploaded = st.file_uploader(
        "Excel de Alerta Forecast (.xlsx / .xls)",
        type=["xlsx", "xls"],
        key="conv_upload",
        help="Usa el archivo descargado con 'Descargar Excel (+ README)' de Alerta Forecast.",
    )
    if uploaded is None:
        st.info("Sube el archivo Excel generado por Alerta Forecast para continuar.")
        return

    # ── Leer y normalizar ─────────────────────────────────────────────────────
    try:
        raw = pd.read_excel(uploaded)
    except Exception as e:
        st.error(f"Error leyendo el archivo: {e}")
        return

    raw = norm_cols(raw)

    required = {"SKU_PRODUCTO", "COD_CCOSTO", "FORECAST_SUGERIDO"}
    missing = required - set(raw.columns)
    if missing:
        st.error(
            f"Columnas faltantes: **{', '.join(sorted(missing))}**. "
            "Asegurate de subir el Excel exportado por Alerta Forecast."
        )
        return

    # PERIODO: derivar del archivo o usar el mes actual
    if "PERIODO" not in raw.columns:
        st.warning("Columna PERIODO no encontrada — se asigna el mes actual.")
        raw["PERIODO"] = pd.Timestamp.now().to_period("M").to_timestamp()
    else:
        raw["PERIODO"] = pd.to_datetime(raw["PERIODO"], errors="coerce")

    # FORECAST_SUGERIDO: tolerar tanto numeros como strings formateados ("45.0", "-")
    fc_raw = raw["FORECAST_SUGERIDO"].astype(str).str.strip().str.replace(",", "", regex=False)
    raw["FORECAST_SUGERIDO"] = pd.to_numeric(fc_raw, errors="coerce")

    raw = raw[raw["FORECAST_SUGERIDO"].notna() & (raw["FORECAST_SUGERIDO"] > 0)].copy()
    raw = raw[raw["PERIODO"].notna()].copy()

    if raw.empty:
        st.warning("No hay filas con FORECAST_SUGERIDO > 0 y PERIODO valido.")
        return

    raw["PERIODO"] = raw["PERIODO"].dt.to_period("M").dt.to_timestamp()

    # ── Selector de mes de inicio + rango de 24 meses ────────────────────────
    primer_periodo = raw["PERIODO"].min()

    st.markdown("### Rango de meses en el archivo de salida")
    c_ini, c_info = st.columns([1, 2])
    with c_ini:
        mes_inicio_input = st.date_input(
            "Mes de inicio",
            value=primer_periodo.date(),
            key="conv_mes_inicio",
            help="Primer mes del archivo Syncro. Se generan 24 meses consecutivos a partir de aqui.",
        )
    mes_inicio = pd.Timestamp(mes_inicio_input).to_period("M").to_timestamp()
    meses_salida = pd.date_range(mes_inicio, periods=24, freq="MS")
    with c_info:
        st.caption(
            f"El archivo tendra **24 columnas de meses**: "
            f"{meses_salida[0].strftime('%b %y')} → {meses_salida[-1].strftime('%b %y')}.  \n"
            "Los meses sin datos en el archivo fuente quedan **vacios**."
        )

    # ── Columnas descriptoras a conservar (primera aparicion por SKU x CC) ───
    # COD_CCOSTO → id_sucursal  |  CENTRO_COSTO → descripcion_sucursal
    desc_candidates = ["SKU_NOM_PRODUCTO", "CENTRO_COSTO"]
    desc_cols       = [c for c in desc_candidates if c in raw.columns]
    canal_cols      = ["CANAL"] if "CANAL" in raw.columns else []

    # Mapear valores de canal al estandar Syncro
    if "CANAL" in raw.columns:
        raw["CANAL"] = (
            raw["CANAL"].astype(str).str.strip().str.upper()
            .map(_CANAL_SYNCRO)
            .fillna(raw["CANAL"].astype(str).str.strip().str.upper())
        )

    # ── Agrupar: suma FORECAST_SUGERIDO por SKU x CC x CANAL x PERIODO ───────
    grp_keys = ["SKU_PRODUCTO", "COD_CCOSTO"] + canal_cols + ["PERIODO"]
    agg_df = raw.groupby(grp_keys, as_index=False).agg(
        FORECAST_SUGERIDO=("FORECAST_SUGERIDO", "sum")
    )

    # Unir descriptoras (first por SKU x CC)
    if desc_cols:
        desc_df = (
            raw.groupby(["SKU_PRODUCTO", "COD_CCOSTO"])[desc_cols]
            .first()
            .reset_index()
        )
        agg_df = agg_df.merge(desc_df, on=["SKU_PRODUCTO", "COD_CCOSTO"], how="left")

    # ── Pivot: meses de la fuente como columnas ───────────────────────────────
    idx_cols = ["SKU_PRODUCTO"] + desc_cols + ["COD_CCOSTO"] + canal_cols
    pivot = agg_df.pivot_table(
        index=idx_cols,
        columns="PERIODO",
        values="FORECAST_SUGERIDO",
        aggfunc="sum",
    ).reset_index()
    pivot.columns.name = None

    # ── Expandir / rellenar los 24 meses de salida ────────────────────────────
    existing_month_cols = {
        c: c for c in pivot.columns if isinstance(c, pd.Timestamp)
    }
    for mes in meses_salida:
        if mes not in existing_month_cols:
            pivot[mes] = None   # mes sin datos → celda vacia en Excel

    # Reordenar: primero idx_cols, luego los 24 meses en orden cronologico
    pivot = pivot[idx_cols + list(meses_salida)]

    # ── Renombrar columnas al estandar Syncro ─────────────────────────────────
    rename_map: dict = {
        "SKU_PRODUCTO":    "id_material",
        "SKU_NOM_PRODUCTO": "descripcion",
        "CENTRO_COSTO":    "descripcion_sucursal",
        "COD_CCOSTO":      "id_sucursal",
        "CANAL":           "canal",
    }
    # Meses: Timestamp → MM/YY  (ej. 04/26)
    for mes in meses_salida:
        rename_map[mes] = mes.strftime("%m/%y")

    pivot.rename(columns=rename_map, inplace=True)

    # ── Resumen y preview ─────────────────────────────────────────────────────
    month_cols_out = [c for c in pivot.columns if re.match(r"^\d{2}/\d{2}$", str(c))]
    n_skus = pivot["id_material"].nunique()
    n_suc  = pivot["id_sucursal"].nunique() if "id_sucursal" in pivot.columns else "?"

    st.success(
        f"Conversion exitosa: **{len(pivot):,} filas** · "
        f"**{n_skus:,} SKUs** · **{n_suc} Centros de Costo** · "
        f"**{len(month_cols_out)} meses**: {month_cols_out[0]} → {month_cols_out[-1]}"
    )
    st.dataframe(pivot.head(200), use_container_width=True, height=420)

    # ── Generar Excel ─────────────────────────────────────────────────────────
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pivot.to_excel(writer, sheet_name="Forecast", index=False)
        ws = writer.sheets["Forecast"]
        for col_cells in ws.columns:
            max_len = max(
                len(str(cell.value)) if cell.value is not None else 0
                for cell in col_cells
            )
            ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 3, 35)
    buf.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        "Descargar Forecast Syncro (.xlsx)",
        buf,
        f"forecast_syncro_{timestamp}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
