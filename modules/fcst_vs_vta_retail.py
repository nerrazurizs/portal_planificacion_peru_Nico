"""
Fcst Syncro vs Venta Actual — Canal Retail / Minorista

Compara el forecast del mes actual contra la venta real MTD por SKU × Sucursal
filtrando solo los canales RETAIL y MINORISTA.

Fuentes de Forecast Syncro (elegibles por el usuario):
  1. proy_result.parquet guardado en data/inputs/ (columna DEMANDA_SIM_TIENDA)
  2. Archivo cargado directamente: .parquet, .xlsx o .csv

Columnas del reporte:
  SKU, Descripcion, AREA, LINEA, SUBLINEA, MARCA, MIX, PROCEDENCIA,
  id_sucursal, descripcion_sucursal, CANAL,
  Fcst Syncro, Vta Actual, Desv%
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from db.cache import cached_query as cq
from db.queries import (
    QUERY_DIAG_JOIN_VCM_SUCURSAL,
    QUERY_DIAG_MAESTRO_SUCURSAL,
    QUERY_DIAG_VCM_CANALES_MTD,
    QUERY_DIAG_VCM_CCOSTO_RETAIL,
)
from modules.ddmrp import VCM_CCOSTO_TO_SYNCRO
from utils.auth import require_role
from utils.export import download_buttons
from utils.file_persistence import (
    has_saved_projection,
    get_projection_info,
    load_projection_results,
)
from utils.filters import norm_cols

# Dict normalizado: cod_almacen (4 dígitos SAP) → id_sucursal Syncro (sin ceros a la izquierda)
_CCOSTO_TO_SYNCRO = {
    k: str(int(v)) if v.lstrip("0").isdigit() or v == "0" else v
    for k, v in VCM_CCOSTO_TO_SYNCRO.items()
}

# ── FCST column candidates in proy_result.parquet ──────────────────────────────
_FCST_PROY_CANDIDATES = [
    "DEMANDA_SIM_TIENDA",
    "VENTA_FUL_TIENDA_UND",
    "DEMANDA_TIENDA",
    "FCST_TIENDA",
]

# ── Column name patterns for flexible file upload detection ────────────────────
_SKU_PATTERNS   = ["SKU_PRODUCTO", "SKU", "COD_PRODUCTO", "PRODUCTO", "ID_MATERIAL", "MATERIAL"]
_FCST_PATTERNS  = ["FCST", "FORECAST", "DEMANDA", "PROYECCION", "VENTA", "UND"]
_SUC_PATTERNS   = ["ID_SUCURSAL", "COD_SUCURSAL", "SUCURSAL", "TIENDA", "COD_TIENDA"]


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _first_match(cols: list[str], patterns: list[str]) -> str | None:
    upper = [c.upper().strip() for c in cols]
    for pat in patterns:
        if pat in upper:
            return cols[upper.index(pat)]
    return None


def _first_contains(cols: list[str], patterns: list[str]) -> str | None:
    for col in cols:
        col_up = col.upper().strip()
        for pat in patterns:
            if pat in col_up:
                return col
    return None


def _detect_sku_col(cols: list[str]) -> str | None:
    return _first_match(cols, _SKU_PATTERNS)


def _detect_fcst_col_proy(cols: list[str]) -> str | None:
    upper = [c.upper().strip() for c in cols]
    for cand in _FCST_PROY_CANDIDATES:
        if cand in upper:
            return cols[upper.index(cand)]
    return None


def _detect_fcst_col_generic(cols: list[str]) -> str | None:
    result = _first_match(cols, _FCST_PATTERNS)
    if result:
        return result
    return _first_contains(cols, _FCST_PATTERNS)


def _detect_suc_col(cols: list[str]) -> str | None:
    return _first_match(cols, _SUC_PATTERNS)


def _norm_id_sucursal(s: "pd.Series") -> "pd.Series":
    """Normaliza código de almacén a string entero sin ceros a la izquierda.

    Convierte "0218" → "218", "218" → "218", "218.0" → "218".
    Usa .apply() para evitar bugs de Copy-on-Write en pandas 2.x.
    """
    def _one(v):
        try:
            return str(int(float(str(v).strip())))
        except (ValueError, TypeError):
            return str(v).strip()
    return s.apply(_one)


# ── FCST loaders ────────────────────────────────────────────────────────────────

def _load_fcst_from_proy(periodo: pd.Period) -> pd.DataFrame | None:
    """Extract FCST from proy_result.parquet for the given monthly period."""
    df_proy, _ = load_projection_results()
    if df_proy is None or df_proy.empty:
        st.warning("proy_result.parquet está vacío o no contiene datos.")
        return None

    df = norm_cols(df_proy.copy())

    fcst_col = _detect_fcst_col_proy(list(df.columns))
    if fcst_col is None:
        st.error(
            "No se encontró columna de demanda en proy_result.parquet. "
            f"Se buscó: {_FCST_PROY_CANDIDATES}"
        )
        return None

    sku_col = _detect_sku_col(list(df.columns))
    if sku_col is None:
        st.error("No se encontró columna SKU_PRODUCTO en proy_result.parquet.")
        return None

    # Filter to current period
    if "PERIODO" in df.columns:
        df["_p"] = pd.to_datetime(df["PERIODO"], errors="coerce")
        df_mes = df[df["_p"].dt.to_period("M") == periodo].copy()
        if df_mes.empty:
            st.warning(
                f"No hay filas para el periodo {periodo} en proy_result.parquet. "
                "Verifique que la proyección haya sido generada para el mes actual."
            )
            return None
        df = df_mes

    result = (
        df[[sku_col, fcst_col]]
        .rename(columns={sku_col: "SKU_PRODUCTO", fcst_col: "FCST_SYNCRO"})
        .copy()
    )
    result["SKU_PRODUCTO"] = result["SKU_PRODUCTO"].astype(str).str.strip()
    result["FCST_SYNCRO"]  = pd.to_numeric(result["FCST_SYNCRO"], errors="coerce").fillna(0)
    result = result.groupby("SKU_PRODUCTO", as_index=False)["FCST_SYNCRO"].sum()
    return result


def _detect_pivot_period_col(cols: list[str], periodo: pd.Period) -> str | None:
    """Detect a pivoted period column matching the current month.

    Accepts formats: 'MM/YYYY', 'MM/YY', 'YYYY-MM', 'YYYYMM', 'MMM YYYY', etc.
    Returns the exact column name found, or None.
    """
    import re
    # Build candidate strings for current period
    now = pd.Timestamp.now()
    candidates = {
        f"{now.month:02d}/{now.year}",           # 04/2026
        f"{now.month:02d}/{str(now.year)[2:]}",  # 04/26
        f"{now.year}-{now.month:02d}",           # 2026-04
        f"{now.year}{now.month:02d}",            # 202604
        now.strftime("%b %Y").upper(),           # APR 2026
        now.strftime("%b. %Y").upper(),          # APR. 2026
        now.strftime("%B %Y").upper(),           # APRIL 2026
    }
    for col in cols:
        if col.upper().strip() in candidates:
            return col
    return None


def _load_fcst_from_file(uploaded_file) -> pd.DataFrame | None:
    """Load FCST from user-uploaded file (.parquet, .xlsx, .xls, .csv).

    Soporta dos formatos:
      1. Columna explícita de forecast: FCST, DEMANDA, DEMANDA_SIM_TIENDA, etc.
      2. Formato pivotado: columnas con períodos MM/YYYY — se usa la del mes actual.
    """
    name = uploaded_file.name.lower()
    try:
        if name.endswith(".parquet"):
            df = pd.read_parquet(uploaded_file, engine="pyarrow")
        elif name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(uploaded_file)
        elif name.endswith(".csv"):
            df = pd.read_csv(uploaded_file)
        else:
            st.error("Formato no soportado. Use .parquet, .xlsx, .xls o .csv")
            return None
    except Exception as exc:
        st.error(f"Error al leer el archivo: {exc}")
        return None

    df   = norm_cols(df)
    cols = list(df.columns)

    sku_col = _detect_sku_col(cols)
    if sku_col is None:
        st.error(
            f"No se encontró columna SKU. "
            f"Se esperaba alguna de: {_SKU_PATTERNS}. "
            f"Columnas disponibles: {cols}"
        )
        return None

    periodo = pd.Timestamp.now().to_period("M")

    # ── Intento 1: columna explícita de forecast ──────────────────────────────
    fcst_col = _detect_fcst_col_proy(cols) or _detect_fcst_col_generic(cols)

    # ── Intento 2: formato pivotado — columna = período MM/YYYY ───────────────
    if fcst_col is None:
        fcst_col = _detect_pivot_period_col(cols, periodo)
        if fcst_col is not None:
            st.info(
                f"Formato pivotado detectado. Usando columna **{fcst_col}** "
                f"como Fcst Syncro del mes actual."
            )

    if fcst_col is None:
        st.error(
            f"No se encontró columna de forecast. "
            f"Se esperaba alguna de: {_FCST_PROY_CANDIDATES + _FCST_PATTERNS} "
            f"o una columna de período (ej. {pd.Timestamp.now().strftime('%m/%Y')}). "
            f"Columnas disponibles: {cols}"
        )
        return None

    # ── Clave de join: ID_SUCURSAL (código de almacén) ───────────────────────────
    id_suc_col = _first_match(cols, ["ID_SUCURSAL", "COD_SUCURSAL", "COD_TIENDA"])

    # Nombre del almacén — para mostrar en la columna descripcion_sucursal
    desc_suc_col = _first_match(
        cols, ["DESCRIPCION_SUCURSAL", "DESC_SUCURSAL", "NOM_SUCURSAL", "NOMBRE_SUCURSAL"]
    )
    if desc_suc_col is None:
        desc_suc_col = _first_contains(cols, ["DESCRIPCION_SUC", "DESC_SUC", "ALMACEN"])

    # Filter by current period if PERIODO column present (non-pivot format)
    if "PERIODO" in cols and fcst_col != _detect_pivot_period_col(cols, periodo):
        df["_p"] = pd.to_datetime(df["PERIODO"], errors="coerce")
        df_mes = df[df["_p"].dt.to_period("M") == periodo]
        if not df_mes.empty:
            df = df_mes

    result = pd.DataFrame()
    result["SKU_PRODUCTO"] = df[sku_col].astype(str).str.strip().str.upper()
    result["FCST_SYNCRO"]  = pd.to_numeric(df[fcst_col], errors="coerce").fillna(0)

    if id_suc_col is not None:
        # Normalizar a entero-string para evitar mismatch "001" vs "1"
        result["ID_SUCURSAL"] = _norm_id_sucursal(df[id_suc_col])
        group_keys = ["SKU_PRODUCTO", "ID_SUCURSAL"]

        if desc_suc_col is not None:
            # El archivo es la fuente de verdad del nombre de almacén
            result["DESCRIPCION_SUCURSAL"] = df[desc_suc_col].astype(str).str.strip().str.upper()
            group_keys = ["SKU_PRODUCTO", "ID_SUCURSAL", "DESCRIPCION_SUCURSAL"]

        result = result.groupby(group_keys, as_index=False).agg(
            FCST_SYNCRO=("FCST_SYNCRO", "sum")
        )
    else:
        # Sin columna de sucursal: FCST a nivel SKU
        result = result.groupby("SKU_PRODUCTO", as_index=False)["FCST_SYNCRO"].sum()

    return result


# ── Styling ─────────────────────────────────────────────────────────────────────

def _color_desv(val):
    if pd.isna(val):
        return ""
    if val >= 0:
        return "color:#1b5e20;font-weight:600"
    if val >= -0.20:
        return "color:#e65100;font-weight:600"
    return "color:#b71c1c;font-weight:600"


# ── Main render ─────────────────────────────────────────────────────────────────

def render_fcst_vs_vta_retail(conn):
    st.html("<h2 class='sub-header'>Fcst Syncro vs Venta Actual — Retail / Minorista</h2>")

    periodo = pd.Timestamp.now().to_period("M")
    mes_label = pd.Timestamp.now().strftime("%B %Y").title()
    st.caption(f"Periodo: **{mes_label}** · Canales: RETAIL / MINORISTA")

    # ── 1. FCST source selection ──────────────────────────────────────────────
    st.markdown("#### Fuente del Forecast Syncro")

    has_proy   = has_saved_projection()
    proy_info  = get_projection_info() if has_proy else None

    col_radio, col_info = st.columns([1, 2])
    with col_radio:
        fuente = st.radio(
            "Seleccione fuente:",
            ["proy_result.parquet (guardado)", "Subir archivo"],
            key="fcsvta_fuente",
        )

    with col_info:
        if "proy_result" in fuente:
            if has_proy and proy_info:
                st.info(
                    f"**proy_result.parquet disponible**  \n"
                    f"Generado: {proy_info.get('generated_at', '?')}  \n"
                    f"SKUs: {proy_info.get('n_skus', '?')}  ·  "
                    f"Periodos: {proy_info.get('n_periodos', '?')}"
                )
            else:
                st.warning(
                    "No hay proy_result.parquet guardado.  \n"
                    "Genere la proyección desde **Proyeccion Stock** "
                    "o use la opción **Subir archivo**."
                )

    uploaded_file = None
    if "Subir" in fuente:
        uploaded_file = st.file_uploader(
            "Archivo Fcst Syncro (.parquet, .xlsx, .xls, .csv)",
            type=["parquet", "xlsx", "xls", "csv"],
            key="fcsvta_upload",
            help=(
                "Debe tener al menos las columnas SKU y FCST/DEMANDA. "
                "Si incluye ID_SUCURSAL, el forecast se aplica por tienda; "
                "si no, se aplica al total del SKU."
            ),
        )

    # Guard: stop if no source available
    if "proy_result" in fuente and not has_proy:
        st.stop()
    if "Subir" in fuente and uploaded_file is None:
        st.info("Sube el archivo de Fcst Syncro para continuar.")
        st.stop()

    # ── 2. Load data ──────────────────────────────────────────────────────────
    with st.spinner("Cargando datos de Snowflake y archivo de forecast..."):
        df_vta      = cq.vta_mtd_retail(conn)
        maestra_raw = cq.maestra(conn)
        df_perfil   = cq.perfil_sku(conn)
        df_tiendas  = cq.tienda_dim(conn)      # maestro Syncro: id_sucursal → descripcion

        if "proy_result" in fuente:
            df_fcst = _load_fcst_from_proy(periodo)
        else:
            df_fcst = _load_fcst_from_file(uploaded_file)

    # ── DIAGNÓSTICO — solo visible para admin ────────────────────────────────
    if require_role("admin"):
        has_problem = df_vta is None or df_vta.empty
        with st.expander("🔍 Diagnóstico de carga (admin)", expanded=has_problem):

            def _diag_query(label, query, conn, *, success_msg=None, empty_msg="❌ Sin filas."):
                st.markdown(f"##### {label}")
                try:
                    df = pd.read_sql(query, conn)
                    if df.empty:
                        st.error(empty_msg)
                    else:
                        st.success(success_msg or f"✅ {len(df):,} filas")
                        st.dataframe(df, use_container_width=True, hide_index=True)
                    return df
                except Exception as exc:
                    st.error(f"Error: {exc}")
                    return pd.DataFrame()

            _diag_query(
                "Paso 1 — Canales cod_canal en VCM (mes actual)",
                QUERY_DIAG_VCM_CANALES_MTD, conn,
                success_msg="✅ VCM tiene ventas este mes (cod_canal '03' = RETAIL):",
                empty_msg="❌ VCM sin ventas en el mes actual.",
            )
            _diag_query(
                "Paso 2 — cod_ccosto canal '03' (RETAIL) este mes",
                QUERY_DIAG_VCM_CCOSTO_RETAIL, conn,
                empty_msg="❌ No hay ventas con cod_canal='03' este mes.",
            )
            _diag_query(
                "Paso 3 — Maestro sucursal",
                QUERY_DIAG_MAESTRO_SUCURSAL, conn,
            )
            df_join = _diag_query(
                "Paso 4 — Join VCM cod_ccosto ↔ maestro id_sucursal",
                QUERY_DIAG_JOIN_VCM_SUCURSAL, conn,
                empty_msg="❌ JOIN sin filas — cod_ccosto no coincide con id_sucursal.",
            )
            if not df_join.empty and "id_sucursal_maestro" in df_join.columns:
                nulls = df_join["id_sucursal_maestro"].isna().sum()
                if nulls:
                    st.warning(f"⚠️ {nulls} registros sin match en maestro sucursal.")

            st.markdown("##### Paso 5 — Resultado QUERY_VTA_MTD_RETAIL")
            if df_vta is None:
                st.error("❌ df_vta es None.")
            elif df_vta.empty:
                st.error("❌ Query devolvió 0 filas.")
            else:
                canales = df_vta["canal"].unique().tolist() if "canal" in df_vta.columns else "?"
                st.success(f"✅ {len(df_vta):,} filas · canales: {canales}")
                st.dataframe(df_vta.head(10), use_container_width=True, hide_index=True)

            st.markdown("##### Paso 6 — Archivo Forecast")
            if df_fcst is None or df_fcst.empty:
                st.error("❌ Forecast vacío o no cargado.")
            else:
                st.success(f"✅ {len(df_fcst):,} filas · columnas: {list(df_fcst.columns)}")
                st.dataframe(df_fcst.head(5), use_container_width=True, hide_index=True)

    if df_fcst is None or df_fcst.empty:
        st.error("No se pudo cargar el forecast. Revisa la fuente seleccionada.")
        st.stop()

    if df_vta is None or df_vta.empty:
        st.stop()

    # ── 3. Normalise columns ──────────────────────────────────────────────────
    df_vta      = norm_cols(df_vta)
    maestra_raw = norm_cols(maestra_raw)

    # Product master: keep only needed columns
    master_cols = [
        "SKU_PRODUCTO", "NOM_PRODUCTO",
        "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL", "PROCEDENCIA",
    ]
    maestra = (
        maestra_raw[[c for c in master_cols if c in maestra_raw.columns]]
        .drop_duplicates("SKU_PRODUCTO")
    )

    # ── 4. Build report ───────────────────────────────────────────────────────
    # Traducir cod_almacen (SAP, e.g. "0248") → id_sucursal Syncro (e.g. "1480")
    df_vta["SKU_PRODUCTO"] = df_vta["SKU_PRODUCTO"].astype(str).str.strip().str.upper()
    df_vta["ID_SUCURSAL"]  = df_vta["COD_ALMACEN"].map(_CCOSTO_TO_SYNCRO)

    # Nombre del almacén desde el maestro Syncro (tienda_dim) por ID_SUCURSAL
    store_desc = (
        norm_cols(df_tiendas)[["ID_SUCURSAL", "DESCRIPCION_SUCURSAL"]]
        .drop_duplicates("ID_SUCURSAL")
    )
    df_vta = df_vta.merge(store_desc, on="ID_SUCURSAL", how="left")

    # Merge actual sales with product master
    df = df_vta.merge(maestra, on="SKU_PRODUCTO", how="left")

    # Merge perfil (SI/NO) por SKU desde db_supply
    df_perfil["SKU_PRODUCTO"] = df_perfil["SKU_PRODUCTO"].astype(str).str.strip().str.upper()
    df = df.merge(df_perfil[["SKU_PRODUCTO", "PERFIL"]], on="SKU_PRODUCTO", how="left")

    # Canal: todas las ventas de cod_canal='03' son TIENDA
    df["CANAL"] = "TIENDA"

    # Determinar estrategia de join con FCST
    fcst_by_id_suc   = "ID_SUCURSAL" in df_fcst.columns
    fcst_by_desc_suc = "DESCRIPCION_SUCURSAL" in df_fcst.columns and not fcst_by_id_suc

    if fcst_by_id_suc:
        # Join por ID_SUCURSAL Syncro: archivo usa 1480, BD tiene "1480" vía _CCOSTO_TO_SYNCRO
        df_fcst["SKU_PRODUCTO"] = df_fcst["SKU_PRODUCTO"].astype(str).str.strip().str.upper()
        df_fcst["ID_SUCURSAL"]  = _norm_id_sucursal(df_fcst["ID_SUCURSAL"])

        # Columnas del archivo que vienen al join
        fcst_cols = ["SKU_PRODUCTO", "ID_SUCURSAL", "FCST_SYNCRO"]
        if "DESCRIPCION_SUCURSAL" in df_fcst.columns:
            fcst_cols.append("DESCRIPCION_SUCURSAL")

        df = df.merge(
            df_fcst[fcst_cols],
            on=["SKU_PRODUCTO", "ID_SUCURSAL"],
            how="left",
            suffixes=("_MAESTRO", "_ARCH"),   # MAESTRO = tienda_dim, ARCH = archivo
        )

        # Preferir nombre del archivo; fallback al maestro Syncro
        if "DESCRIPCION_SUCURSAL_ARCH" in df.columns:
            df["DESCRIPCION_SUCURSAL"] = (
                df["DESCRIPCION_SUCURSAL_ARCH"]
                .fillna(df.get("DESCRIPCION_SUCURSAL_MAESTRO", ""))
            )
            df.drop(
                columns=["DESCRIPCION_SUCURSAL_ARCH", "DESCRIPCION_SUCURSAL_MAESTRO"],
                errors="ignore", inplace=True,
            )

    elif fcst_by_desc_suc:
        # Fallback: join por nombre de sucursal (solo si no hay columna de código)
        df_fcst["SKU_PRODUCTO"]         = df_fcst["SKU_PRODUCTO"].astype(str).str.strip().str.upper()
        df_fcst["DESCRIPCION_SUCURSAL"] = df_fcst["DESCRIPCION_SUCURSAL"].astype(str).str.strip().str.upper()
        df["DESCRIPCION_SUCURSAL"]      = df["DESCRIPCION_SUCURSAL"].astype(str).str.strip().str.upper()
        df = df.merge(
            df_fcst[["SKU_PRODUCTO", "DESCRIPCION_SUCURSAL", "FCST_SYNCRO"]],
            on=["SKU_PRODUCTO", "DESCRIPCION_SUCURSAL"],
            how="left",
        )
    else:
        # Sin sucursal en FCST: join solo por SKU
        df = df.merge(df_fcst[["SKU_PRODUCTO", "FCST_SYNCRO"]], on="SKU_PRODUCTO", how="left")

    # ── Diagnóstico de match (visible siempre) ───────────────────────────────
    if fcst_by_id_suc and "FCST_SYNCRO" in df.columns:
        matched = df["FCST_SYNCRO"].notna().sum()
        total   = len(df)
        if matched == 0:
            ids_vta  = sorted(df["ID_SUCURSAL"].unique().tolist())[:10]
            ids_fcst = sorted(df_fcst["ID_SUCURSAL"].unique().tolist())[:10]
            skus_vta  = sorted(df["SKU_PRODUCTO"].unique().tolist())[:5]
            skus_fcst = sorted(df_fcst["SKU_PRODUCTO"].unique().tolist())[:5]
            st.error(
                "⚠️ **El archivo no generó ningún match con las ventas Snowflake.**  \n"
                f"**ID_SUCURSAL en ventas (BD):** `{ids_vta}`  \n"
                f"**ID_SUCURSAL en archivo:** `{ids_fcst}`  \n"
                f"**SKU muestra BD:** `{skus_vta}`  \n"
                f"**SKU muestra archivo:** `{skus_fcst}`"
            )
        elif matched < total * 0.5:
            st.warning(f"⚠️ Solo {matched}/{total} filas con FCST matched. Verifica los códigos de almacén.")

    # Fcst Syncro vacíos → 0
    df["FCST_SYNCRO"] = pd.to_numeric(df.get("FCST_SYNCRO", np.nan), errors="coerce").fillna(0)
    df["VTA_ACTUAL"]  = pd.to_numeric(
        df.get("VTA_ACTUAL_UND", df.get("CANTIDAD", 0)), errors="coerce"
    ).fillna(0)

    # Desv% = Vta / Fcst - 1   (NaN cuando Fcst = 0)
    df["DESV_PCT"] = np.where(
        df["FCST_SYNCRO"] > 0,
        df["VTA_ACTUAL"] / df["FCST_SYNCRO"] - 1,
        np.nan,
    )

    # Comentario basado en Desv%
    def _comentario(desv):
        if pd.isna(desv) or desv >= 1.0:   # NaN o >= 100 %
            return "Sin Fcst cargado"
        if desv < 0:                         # < 0 %
            return "Fcst Sobrestimado"
        return "Fcst Subestimado"            # >= 0 % y < 100 %

    df["COMENTARIO"] = df["DESV_PCT"].apply(_comentario)

    # ── 5. Filters ────────────────────────────────────────────────────────────
    st.markdown("#### Filtros")
    fc1, fc2, fc3, fc4 = st.columns(4)

    with fc1:
        opts_area = sorted(df["AREA"].dropna().unique().tolist())
        sel_area = st.multiselect("Área", opts_area, key="fcsvta_area")
    with fc2:
        opts_linea = sorted(df["LINEA"].dropna().unique().tolist())
        sel_linea = st.multiselect("Línea", opts_linea, key="fcsvta_linea")
    with fc3:
        opts_marca = sorted(df["MARCA"].dropna().unique().tolist())
        sel_marca = st.multiselect("Marca", opts_marca, key="fcsvta_marca")
    with fc4:
        opts_canal = sorted(df["CANAL"].dropna().unique().tolist())
        sel_canal = st.multiselect("Canal", opts_canal, default=opts_canal, key="fcsvta_canal")

    mask = pd.Series(True, index=df.index)
    if sel_area:
        mask &= df["AREA"].isin(sel_area)
    if sel_linea:
        mask &= df["LINEA"].isin(sel_linea)
    if sel_marca:
        mask &= df["MARCA"].isin(sel_marca)
    if sel_canal:
        mask &= df["CANAL"].isin(sel_canal)

    df_f = df[mask].copy()

    if df_f.empty:
        st.info("Sin resultados para los filtros seleccionados.")
        st.stop()

    # ── 6. KPI summary ────────────────────────────────────────────────────────
    if fcst_by_id_suc or fcst_by_desc_suc:
        total_fcst = df_f["FCST_SYNCRO"].sum()
    else:
        total_fcst = df_f.drop_duplicates("SKU_PRODUCTO")["FCST_SYNCRO"].sum()

    total_vta   = df_f["VTA_ACTUAL"].sum()
    desv_global = (total_vta / total_fcst - 1) if total_fcst > 0 else 0.0
    n_skus      = df_f["SKU_PRODUCTO"].nunique()
    n_tiendas   = df_f["ID_SUCURSAL"].nunique() if "ID_SUCURSAL" in df_f.columns else 0

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("SKUs", f"{n_skus:,}")
    m2.metric("Tiendas", f"{n_tiendas:,}")
    m3.metric("Fcst Syncro", f"{total_fcst:,.2f}")
    m4.metric("Vta Actual (Und)", f"{total_vta:,.0f}")
    desv_sign = "+" if desv_global >= 0 else ""
    m5.metric(
        "Desv% Total",
        f"{desv_sign}{desv_global:.0%}",
        delta=f"{desv_sign}{desv_global:.0%}",
        delta_color="normal" if desv_global >= 0 else "inverse",
    )

    if fcst_by_id_suc:
        st.caption("✅ Match por **SKU + ID Sucursal** — código de almacén (más confiable).")
    elif fcst_by_desc_suc:
        st.caption("⚠️ Match por **SKU + Descripción Sucursal** — nombre de almacén (fallback).")
    else:
        st.caption("ℹ️ Fcst Syncro a nivel SKU total. Desv% compara cada tienda vs el forecast total del SKU.")

    # ── 7. Report table ───────────────────────────────────────────────────────
    st.markdown("#### Reporte Detallado")

    # Orden de columnas exacto (igual al screenshot + Comentario al final)
    COL_ORDER = [
        "SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA",
        "MARCA", "MIX_OFICIAL", "PROCEDENCIA", "PERFIL",
        "ID_SUCURSAL", "COD_ALMACEN", "DESCRIPCION_SUCURSAL", "CANAL",
        "FCST_SYNCRO", "VTA_ACTUAL", "DESV_PCT", "COMENTARIO",
    ]
    COL_RENAME = {
        "SKU_PRODUCTO":         "SKU",
        "NOM_PRODUCTO":         "descripcion",
        "AREA":                 "AREA",
        "LINEA":                "LINEA",
        "SUBLINEA":             "SUBLINEA",
        "MARCA":                "MARCA",
        "MIX_OFICIAL":          "MIX",
        "PROCEDENCIA":          "PROCEDENCIA",
        "PERFIL":               "Perfil",
        "ID_SUCURSAL":          "id_sucursal",
        "COD_ALMACEN":          "cod_almacen",
        "DESCRIPCION_SUCURSAL": "descripcion_sucursal",
        "CANAL":                "canal",
        "FCST_SYNCRO":          "Fcst Syncro",
        "VTA_ACTUAL":           "Vta Actual",
        "DESV_PCT":             "Desv%",
        "COMENTARIO":           "Comentario",
    }

    avail_cols = [c for c in COL_ORDER if c in df_f.columns]
    df_display = (
        df_f[avail_cols]
        .rename(columns=COL_RENAME)
        .sort_values(["SKU", "descripcion_sucursal"])
        .reset_index(drop=True)
    )

    # ── Column config para st.dataframe (filtros + formato) ───────────────────
    col_cfg = {
        "SKU":                  st.column_config.TextColumn("SKU", width="small"),
        "descripcion":          st.column_config.TextColumn("descripcion", width="large"),
        "AREA":                 st.column_config.TextColumn("AREA", width="small"),
        "LINEA":                st.column_config.TextColumn("LINEA", width="medium"),
        "SUBLINEA":             st.column_config.TextColumn("SUBLINEA", width="medium"),
        "MARCA":                st.column_config.TextColumn("MARCA", width="medium"),
        "MIX":                  st.column_config.TextColumn("MIX", width="small"),
        "PROCEDENCIA":          st.column_config.TextColumn("PROCEDENCIA", width="small"),
        "Perfil":               st.column_config.TextColumn("Perfil", width="small"),
        "id_sucursal":          st.column_config.TextColumn("id_sucursal", width="small"),
        "cod_almacen":          st.column_config.TextColumn("cod_almacen", width="small"),
        "descripcion_sucursal": st.column_config.TextColumn("descripcion_sucursal", width="large"),
        "canal":                st.column_config.TextColumn("canal", width="small"),
        "Fcst Syncro":          st.column_config.NumberColumn("Fcst Syncro", format="%.2f", width="small"),
        "Vta Actual":           st.column_config.NumberColumn("Vta Actual", format="%d", width="small"),
        "Desv%":                st.column_config.NumberColumn("Desv%", format="%.0f%%", width="small"),
        "Comentario":           st.column_config.TextColumn("Comentario", width="medium"),
    }

    # Convertir Desv% a porcentaje entero para column_config (multiplica x100)
    if "Desv%" in df_display.columns:
        df_display["Desv%"] = df_display["Desv%"] * 100

    st.dataframe(
        df_display,
        column_config=col_cfg,
        use_container_width=True,
        height=600,
        hide_index=True,
    )

    # ── 8. Export ─────────────────────────────────────────────────────────────
    st.markdown("#### Exportar")

    df_export = df_display.copy()
    if "Desv%" in df_export.columns:
        df_export["Desv%"] = df_export["Desv%"].apply(
            lambda v: f"{v:.0f}%" if pd.notna(v) else ""
        )
    download_buttons(df_export, prefix="fcst_vs_vta_retail")
