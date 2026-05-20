"""Optimizador de Compras y Contenedores.

Lee un Excel de proyección de compras (IMPORTADO/NACIONAL), consolida OCs de
importación en contenedores FCL/LCL mediante un algoritmo greedy que minimiza
el número de contenedores y maximiza el fill, respetando restricciones de MOI
y ROP. Los costos de flete no participan en las decisiones de optimización.

Tabs:
  1. Resumen    KPIs comparativos BASE vs OPTIMIZADO
  2. Planes     Tablas Plan_Base y Plan_Optimizado con notas de movimiento
  3. Detalle    OCs individuales con contenedor asignado
  4. Movimientos Log de OCs movidas con impacto en fill y MOI
"""

from __future__ import annotations

import io
import math
from datetime import date

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dateutil.relativedelta import relativedelta
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from config import COLORS, dorel_layout

# ============================================================================
# CONSTANTES Y DEFAULTS
# ============================================================================

_PROCEDENCIA_IMPORT = "IMPORTADO"
_MIX_OFICIAL_FILTRO = "MIX"

_ORIGEN_NORMALIZAR = {
    "TAWIAN": "TAIWAN",
    "UNITED STATES": "USA",
    "BRAZIL": "BRASIL",
    "NETHERLANDS": "PAISES BAJOS",
}

# Estilos Excel
_HDR_FILL = PatternFill("solid", fgColor="1F3864")
_SEC_FILL = PatternFill("solid", fgColor="2E75B6")
_ALT_FILL = PatternFill("solid", fgColor="D6E4F0")
_WHT_FILL = PatternFill("solid", fgColor="FFFFFF")
_HDR_FONT = Font(bold=True, color="FFFFFF", size=10)
_NRM_FONT = Font(size=10)
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)

# ============================================================================
# UTILIDADES INTERNAS
# ============================================================================


def _first_of_month(d) -> pd.Timestamp:
    if pd.isna(d):
        return pd.NaT
    ts = pd.Timestamp(d)
    return pd.Timestamp(ts.year, ts.month, 1)


def _add_months(ts: pd.Timestamp, n: int) -> pd.Timestamp:
    r = ts + relativedelta(months=n)
    return pd.Timestamp(r.year, r.month, 1)


class _IDGen:
    def __init__(self):
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"A{self._n:09d}"


def _container_combo(cbm_total: float, cap20: float, cap40: float):
    """Mínimo número de contenedores con máximo fill."""
    if cbm_total <= 0:
        return 0, 0, 0.0, 0.0
    best = None
    for n40 in range(0, math.ceil(cbm_total / cap40) + 2):
        rem = cbm_total - n40 * cap40
        n20 = 0 if rem <= 0 else math.ceil(rem / cap20)
        cap = n40 * cap40 + n20 * cap20
        if cap < cbm_total:
            continue
        n = n40 + n20
        fill = cbm_total / cap if cap > 0 else 0
        if best is None or n < best[4] or (n == best[4] and fill > best[3]):
            best = (n40, n20, cap, fill, n)
    if best is None:
        n40 = math.ceil(cbm_total / cap40)
        cap = n40 * cap40
        return n40, 0, cap, cbm_total / cap if cap > 0 else 0
    return best[0], best[1], best[2], best[3]


def _classify(cbm_total: float, fill: float, cap20: float,
              permitir_lcl: bool, lcl_umbral: float, min_fill: float):
    lcl_min = lcl_umbral * cap20
    if not permitir_lcl or cbm_total >= lcl_min:
        modo = "FCL"
        if fill >= min_fill:
            estado = "OK"
        elif fill >= 0.70:
            estado = "BAJO"
        else:
            estado = "CRITICO"
    else:
        modo, estado = "LCL", "LCL"
    return modo, estado


# ============================================================================
# CARGA Y LIMPIEZA
# ============================================================================


def _load(uploaded_file, hoy_override=None):
    """Lee el CSV de base de compras. Solo normaliza tipos; NO aplica filtros todavía."""
    df = pd.read_csv(uploaded_file, sep=None, engine="python", encoding_errors="replace")
    df.columns = df.columns.str.strip()

    # Capturar columnas originales ANTES de cualquier enriquecimiento
    csv_cols = list(df.columns)

    # Normalizar columna clave a string
    if "id_material" in df.columns:
        df["id_material"] = df["id_material"].astype(str).str.strip()

    # Numéricos presentes en el CSV
    for col in ["lead_time", "orden", "stock_cd", "stock_total",
                "demanda", "rop", "costo_promedio"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    if "lead_time" in df.columns:
        df["lead_time"] = df["lead_time"].astype(int)

    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")

    if hoy_override:
        hoy = pd.Timestamp(hoy_override)
        hoy = pd.Timestamp(hoy.year, hoy.month, 1)
    else:
        t = date.today()
        hoy = pd.Timestamp(t.year, t.month, 1)

    return df, hoy, csv_cols


def _enrich(df: pd.DataFrame, conn) -> tuple[pd.DataFrame, list[str]]:
    """Une el CSV con la maestra de Snowflake para obtener las columnas de producto.

    Columnas que aporta Snowflake: MIX_OFICIAL, PROCEDENCIA, COD_PROVEEDOR,
    PROVEEDOR, FAMILIA, MARCA, LINEA.
    Columnas que deben venir del CSV o fallarán con advertencia: VOLUMEN, ORIGEN.
    """
    warns = []

    # ── Traer maestra ────────────────────────────────────────────────────
    if conn is not None:
        try:
            from db.cache import cached_query as cq
            maestra = cq.maestra(conn)
            maestra.columns = maestra.columns.str.upper().str.strip()

            # Mapeo de nombres Snowflake → módulo
            rename_map = {
                "SKU_PRODUCTO":  "id_material",
                "MIX_OFICIAL":   "MIX_OFICIAL",
                "PROCEDENCIA":   "PROCEDENCIA",
                "COD_PROVEEDOR": "COD_PROVEEDOR",
                "PROVEEDOR":     "PROVEEDOR",
                "SUBLINEA":      "FAMILIA",   # en Perú familia == sublinea
                "MARCA":         "MARCA",
                "LINEA":         "LINEA",
            }
            maestra = maestra.rename(columns=rename_map)
            cols_to_keep = ["id_material"] + [v for v in rename_map.values()
                                               if v != "id_material"]
            cols_to_keep = [c for c in cols_to_keep if c in maestra.columns]
            maestra = maestra[cols_to_keep].drop_duplicates(subset=["id_material"])
            maestra["id_material"] = maestra["id_material"].astype(str).str.strip()

            # Solo traer columnas que NO están ya en el CSV
            cols_new = [c for c in cols_to_keep if c != "id_material" and c not in df.columns]
            if cols_new:
                df = df.merge(
                    maestra[["id_material"] + cols_new],
                    on="id_material", how="left",
                )
        except Exception as e:
            warns.append(f"No se pudo cargar maestra desde Snowflake: {e}")
    else:
        warns.append("Sin conexión a Snowflake — columnas de producto no disponibles")

    # ── Normalizar columnas de producto ──────────────────────────────────
    for col in ["PROCEDENCIA", "MIX_OFICIAL", "ORIGEN", "COD_PROVEEDOR",
                "PROVEEDOR", "FAMILIA", "MARCA", "LINEA"]:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str).str.strip()
        else:
            df[col] = ""

    # ORIGEN no está en _PROD → advertir si está vacío
    if (df["ORIGEN"] == "").all():
        warns.append(
            "ORIGEN (país de origen) no disponible — se agrupará solo por COD_PROVEEDOR. "
            "Agrégalo al CSV si necesitas separar por país."
        )
        # Fallback: usar COD_PROVEEDOR como ORIGEN para que el algoritmo funcione
        df["ORIGEN"] = df["COD_PROVEEDOR"]

    df["ORIGEN"] = df["ORIGEN"].replace(_ORIGEN_NORMALIZAR)

    # COD_PROVEEDOR vacío → proxy
    mask_sin_prov = df["COD_PROVEEDOR"] == ""
    if mask_sin_prov.any():
        df.loc[mask_sin_prov, "COD_PROVEEDOR"] = "PROV_" + df.loc[mask_sin_prov, "ORIGEN"]
        warns.append("COD_PROVEEDOR vacío en algunos SKUs → generado como PROV_<ORIGEN>")

    # ── VOLUMEN + PROCEDENCIA desde dt_producto (fuente autoritativa) ───
    _need_vol = (
        "VOLUMEN" not in df.columns
        or (pd.to_numeric(df.get("VOLUMEN", 0), errors="coerce").fillna(0) == 0).all()
    )
    if conn is not None:
        try:
            dims = cq.dt_producto(conn)
            dims.columns = dims.columns.str.upper().str.strip()
            dims = dims.rename(columns={"COD_PRODUCTO": "id_material"})
            dims["id_material"] = dims["id_material"].astype(str).str.strip()

            # PROCEDENCIA: normalizar códigos cortos → texto completo
            _PROC_MAP = {"I": "IMPORTADO", "N": "NACIONAL", "E": "IMPORTADO",
                         "i": "IMPORTADO", "n": "NACIONAL", "e": "IMPORTADO"}
            dims["PROCEDENCIA"] = (
                dims["PROCEDENCIA"].fillna("").astype(str).str.strip().replace(_PROC_MAP)
            )

            # VOLUMEN: usar directo; DENSIDAD como fallback si VOLUMEN = 0
            for c in ["VOLUMEN", "DENSIDAD"]:
                dims[c] = pd.to_numeric(dims[c], errors="coerce").fillna(0)
            dims["VOLUMEN"] = dims["VOLUMEN"].where(dims["VOLUMEN"] > 0, dims["DENSIDAD"])

            dims = dims.drop_duplicates(subset=["id_material"])

            # Siempre sobreescribir PROCEDENCIA con el valor de dt_producto
            df = df.drop(columns=["PROCEDENCIA"], errors="ignore")
            merge_cols = ["id_material", "PROCEDENCIA"]
            if _need_vol:
                merge_cols.append("VOLUMEN")

            df = df.merge(dims[merge_cols], on="id_material", how="left")
            df["PROCEDENCIA"] = df["PROCEDENCIA"].fillna("").astype(str).str.strip()

            if _need_vol:
                df["VOLUMEN"] = pd.to_numeric(df["VOLUMEN"], errors="coerce").fillna(0)
                n_con = (df["VOLUMEN"] > 0).sum()
                n_sin = (df["VOLUMEN"] == 0).sum()
                warns.append(
                    f"VOLUMEN obtenido desde dt_producto: "
                    f"{n_con:,} OCs con CBM, {n_sin:,} sin datos."
                )

            procs = df["PROCEDENCIA"].value_counts().to_dict()
            warns.append(f"PROCEDENCIA desde dt_producto: {procs}")

        except Exception as e:
            warns.append(f"⛔ No se pudo obtener datos desde dt_producto: {e}")
            if _need_vol:
                df["VOLUMEN"] = 0.0
    else:
        warns.append(
            "⛔ Sin conexión a Snowflake — VOLUMEN y PROCEDENCIA no disponibles."
        )
        if _need_vol:
            df["VOLUMEN"] = 0.0

    if "VOLUMEN" in df.columns:
        df["VOLUMEN"] = pd.to_numeric(df["VOLUMEN"], errors="coerce").fillna(0)
    else:
        df["VOLUMEN"] = 0.0

    return df, warns


def _apply_filters(df: pd.DataFrame, hoy: pd.Timestamp) -> pd.DataFrame:
    """Aplica filtros MIX_OFICIAL, orden>0 y fecha>=hoy."""
    df = df[df["MIX_OFICIAL"] == _MIX_OFICIAL_FILTRO].copy()
    df = df[df["orden"] > 0].copy()
    df = df[df["fecha"] >= hoy].copy()
    return df


# ============================================================================
# MOI
# ============================================================================


def _build_moi(df: pd.DataFrame) -> dict:
    demand_idx, stock_idx = {}, {}
    for _, r in df.iterrows():
        mes = _first_of_month(r["fecha"])
        k = (r["id_material"], mes)
        demand_idx[k] = r["demanda"]
        stock_idx[k] = r["stock_total"]

    moi = {}
    for (sku, mes), stock in stock_idx.items():
        ds = [demand_idx.get((sku, _add_months(mes, k))) for k in range(3)]
        ds = [d for d in ds if d is not None]
        if not ds:
            moi[(sku, mes)] = None
        else:
            avg = np.mean(ds)
            moi[(sku, mes)] = float("inf") if avg == 0 else stock / avg
    return moi


def _get_moi(moi_idx: dict, sku: str, mes: pd.Timestamp):
    return moi_idx.get((sku, mes), None)


# ============================================================================
# PLAN BASE
# ============================================================================


def _plan_base(df: pd.DataFrame, moi_idx: dict):
    nac = df[df["PROCEDENCIA"] != _PROCEDENCIA_IMPORT].copy()
    imp = df[df["PROCEDENCIA"] == _PROCEDENCIA_IMPORT].copy()

    imp["fecha_oc"] = (
        imp["fecha"].dt.to_period("M").dt.to_timestamp()
    )
    imp["fecha_llegada"] = (
        (imp["fecha"] + pd.to_timedelta(imp["lead_time"], unit="D"))
        .dt.to_period("M").dt.to_timestamp()
    )
    imp["mes_llegada"] = imp["fecha_llegada"]
    imp["mes_llegada_orig"] = imp["mes_llegada"]
    imp["cbm_orden"] = imp["orden"] * imp["VOLUMEN"]
    imp["valor_orden"] = imp["orden"] * imp["costo_promedio"]
    # moi_en_oc: lookup vectorizado sobre índice (sku, mes)
    imp["moi_en_oc"] = [
        _get_moi(moi_idx, sku, mes)
        for sku, mes in zip(imp["id_material"], imp["fecha_oc"])
    ]
    imp["delta_meses"] = 0
    imp["accion"] = ""

    return imp.reset_index(drop=True), nac.reset_index(drop=True)


# ============================================================================
# ASIGNACIÓN DE CONTENEDORES (Best Fit Decreasing)
# ============================================================================


def _assign_group(grp: pd.DataFrame, id_gen: _IDGen, cap20: float, cap40: float):
    df_g = grp.copy().sort_values("cbm_orden", ascending=False).reset_index(drop=True)
    containers: list[dict] = []

    def _best_fit(cbm):
        best_i, best_rem = None, float("inf")
        for i, c in enumerate(containers):
            rem = c["cap"] - c["used"]
            if rem >= cbm and rem < best_rem:
                best_rem, best_i = rem, i
        return best_i

    cont_idx = []
    for _, row in df_g.iterrows():
        cbm = row["cbm_orden"]
        i = _best_fit(cbm)
        if i is not None:
            containers[i]["used"] += cbm
            cont_idx.append(i)
        else:
            cap = cap40 if cbm > cap20 else cap20
            containers.append({"id": id_gen.next(), "used": cbm, "cap": cap})
            cont_idx.append(len(containers) - 1)

    # ── Downgrade: 40ft con carga ≤ cap20 → reclasificar como 20ft ──────
    # Evita pagar un 40ft al 20% cuando un 20ft al 60% es suficiente.
    for c in containers:
        if c["cap"] == cap40 and c["used"] <= cap20:
            c["cap"] = cap20

    df_g["contenedor_id"]   = [containers[i]["id"]   for i in cont_idx]
    df_g["cbm_contenedor"]  = [containers[i]["used"]  for i in cont_idx]
    df_g["cap_contenedor"]  = [containers[i]["cap"]   for i in cont_idx]
    df_g["fill_pct_cont"]   = df_g["cbm_contenedor"] / df_g["cap_contenedor"]

    # ── Tipo de contenedor por fila ───────────────────────────────────────
    df_g["tipo_cont"] = df_g["cap_contenedor"].apply(
        lambda c: "20ft" if c == cap20 else "40ft"
    )

    # ── Resumen del grupo: "1×40ft + 2×20ft" ──────────────────────────────
    n40_g = sum(1 for c in containers if c["cap"] == cap40)
    n20_g = sum(1 for c in containers if c["cap"] == cap20)
    partes = []
    if n40_g: partes.append(f"{n40_g}×40ft")
    if n20_g: partes.append(f"{n20_g}×20ft")
    df_g["contenedores_grupo"] = " + ".join(partes)

    return df_g


def _assign_all(df_imp: pd.DataFrame, id_gen: _IDGen, cap20: float, cap40: float):
    if df_imp.empty:
        # Devolver DataFrame vacío con las columnas esperadas
        return df_imp.assign(
            contenedor_id="", cbm_contenedor=0.0,
            cap_contenedor=0.0, fill_pct_cont=0.0,
            tipo_cont="", contenedores_grupo="",
        )
    parts = []
    for _, grp in df_imp.groupby(["COD_PROVEEDOR", "ORIGEN", "mes_llegada"], sort=False):
        parts.append(_assign_group(grp, id_gen, cap20, cap40))
    return pd.concat(parts, ignore_index=True)


# ============================================================================
# CONSOLIDACIÓN GREEDY
# ============================================================================


def _group_fill(df: pd.DataFrame, prov: str, orig: str, mes: pd.Timestamp,
                cap20: float, cap40: float):
    mask = (
        (df["COD_PROVEEDOR"] == prov) &
        (df["ORIGEN"] == orig) &
        (df["mes_llegada"] == mes)
    )
    cbm = df.loc[mask, "cbm_orden"].sum()
    if cbm <= 0:
        return 0.0, cbm
    _, _, _, fill = _container_combo(cbm, cap20, cap40)
    return fill, cbm


def _validate(df: pd.DataFrame, moi_idx: dict, sku_list: list,
              prov: str, orig: str, mes_orig: pd.Timestamp,
              mes_dest: pd.Timestamp, delta: int, acum: int,
              hoy: pd.Timestamp, fill_dest_antes: float,
              cfg: dict) -> tuple[bool, str]:
    # Val 1 — fecha mínima
    mask = (df["COD_PROVEEDOR"] == prov) & (df["ORIGEN"] == orig) & (df["mes_llegada"] == mes_orig)
    max_lt = df.loc[mask, "lead_time"].max()
    fecha_min = _add_months(hoy, math.ceil(max_lt / 30))
    if mes_dest < fecha_min:
        return False, "FECHA_MIN"

    # Val 4 — delta acumulado
    if abs(acum + delta) > cfg["max_meses"]:
        return False, "DELTA_MAX"

    # Simular fill mejora
    cbm_mov = df.loc[mask, "cbm_orden"].sum()
    mask_d = (df["COD_PROVEEDOR"] == prov) & (df["ORIGEN"] == orig) & (df["mes_llegada"] == mes_dest)
    cbm_d = df.loc[mask_d, "cbm_orden"].sum()
    _, _, _, fill_new = _container_combo(cbm_d + cbm_mov, cfg["cap20"], cfg["cap40"])
    fill_mejora = fill_new - fill_dest_antes

    # Val 2 — MOI
    for sku in sku_list:
        moi = _get_moi(moi_idx, sku, mes_dest)
        if moi is None:
            continue
        if moi < cfg["moi_min"]:
            return False, f"MOI_MIN_{sku}"
        if moi > cfg["moi_max"] and fill_mejora < cfg["moi_fill_override"]:
            return False, f"MOI_MAX_{sku}"

    # Val 3 — ROP (solo atrasos)
    if delta > 0:
        for sku in sku_list:
            for step in range(1, delta + 1):
                mes_paso = _add_months(mes_orig, step)
                rows = df[(df["id_material"] == sku) & (df["mes_llegada"] == mes_paso)]
                if rows.empty:
                    continue
                if rows.iloc[0]["stock_cd"] < rows.iloc[0]["rop"]:
                    return False, f"ROP_{sku}"

    return True, "OK"


def _consolidate(df_in: pd.DataFrame, moi_idx: dict, hoy: pd.Timestamp, cfg: dict):
    df = df_in.copy()
    log = []
    delta_acum = {i: 0 for i in df.index}

    for (prov, orig), _ in df.groupby(["COD_PROVEEDOR", "ORIGEN"], sort=False):
        iters, mejoro = 0, True
        while mejoro and iters < 50:
            mejoro = False
            iters += 1

            mask_g = (df["COD_PROVEEDOR"] == prov) & (df["ORIGEN"] == orig)
            meses = sorted(df.loc[mask_g, "mes_llegada"].unique())
            fills = []
            for mes in meses:
                fp, cbm_t = _group_fill(df, prov, orig, mes, cfg["cap20"], cfg["cap40"])
                fills.append((cbm_t, fp, mes))
            fills.sort(key=lambda x: x[0])

            candidatos = [(c, f, m) for c, f, m in fills if f < cfg["min_fill"]]
            if not candidatos:
                break

            for cbm_o, fill_o, mes_orig in candidatos:
                mask_o = mask_g & (df["mes_llegada"] == mes_orig)
                sku_list = df.loc[mask_o, "id_material"].unique().tolist()
                idx_list = df.loc[mask_o].index.tolist()
                acum_max = max((delta_acum.get(i, 0) for i in idx_list), default=0)

                mejor_delta, mejor_fill, mejor_dest = None, -1, None

                for delta in range(-cfg["max_meses"], cfg["max_meses"] + 1):
                    if delta == 0:
                        continue
                    mes_dest = _add_months(mes_orig, delta)
                    if mes_dest < hoy:
                        continue

                    fill_d_antes, _ = _group_fill(df, prov, orig, mes_dest, cfg["cap20"], cfg["cap40"])
                    ok, _ = _validate(
                        df, moi_idx, sku_list, prov, orig,
                        mes_orig, mes_dest, delta, acum_max, hoy,
                        fill_d_antes, cfg,
                    )
                    if not ok:
                        continue

                    cbm_mov = df.loc[mask_o, "cbm_orden"].sum()
                    mask_d = mask_g & (df["mes_llegada"] == mes_dest)
                    cbm_d = df.loc[mask_d, "cbm_orden"].sum()
                    _, _, _, fill_sim = _container_combo(cbm_d + cbm_mov, cfg["cap20"], cfg["cap40"])

                    if fill_sim > mejor_fill:
                        mejor_fill, mejor_delta, mejor_dest = fill_sim, delta, mes_dest

                if mejor_delta is None:
                    continue

                fill_d_antes, _ = _group_fill(df, prov, orig, mejor_dest, cfg["cap20"], cfg["cap40"])
                # Solo mover si el fill resultante mejora sobre ambos meses individualmente.
                # La comparación anterior era (mejora_pp < umbral_absoluto) lo que
                # bloqueaba casi todas las consolidaciones útiles.
                if mejor_fill <= max(fill_o, fill_d_antes):
                    continue

                cbm_mov = df.loc[mask_o, "cbm_orden"].sum()
                n_ocs = len(idx_list)

                moi_o = [_get_moi(moi_idx, s, mes_orig) for s in sku_list]
                moi_o = [m for m in moi_o if m is not None and not math.isinf(m)]
                moi_d = [_get_moi(moi_idx, s, mejor_dest) for s in sku_list]
                moi_d = [m for m in moi_d if m is not None and not math.isinf(m)]

                max_lt = df.loc[mask_o, "lead_time"].max()
                fecha_min = _add_months(hoy, math.ceil(max_lt / 30))

                df.loc[mask_o, "mes_llegada"] = mejor_dest
                df.loc[mask_o, "fecha_llegada"] = mejor_dest
                for i in idx_list:
                    delta_acum[i] = delta_acum.get(i, 0) + mejor_delta
                    df.loc[i, "delta_meses"] = delta_acum[i]

                dir_t = "ADELANTADO" if mejor_delta < 0 else "ATRASADO"
                df.loc[mask_o, "accion"] = f"{dir_t} {abs(mejor_delta)} meses"

                fill_d_desp, _ = _group_fill(df, prov, orig, mejor_dest, cfg["cap20"], cfg["cap40"])

                log.append({
                    "ORIGEN":               orig,
                    "COD_PROVEEDOR":        prov,
                    "mes_origen":           mes_orig,
                    "mes_destino":          mejor_dest,
                    "delta_meses":          mejor_delta,
                    "accion":               f"{dir_t} {abs(mejor_delta)} meses",
                    "n_ocs_movidas":        n_ocs,
                    "cbm_movido":           round(cbm_mov, 3),
                    "fill_origen_antes":    round(fill_o, 4),
                    "fill_destino_antes":   round(fill_d_antes, 4),
                    "fill_destino_despues": round(fill_d_desp, 4),
                    "moi_origen_prom":      round(np.mean(moi_o), 2) if moi_o else None,
                    "moi_destino_min":      round(min(moi_d), 2) if moi_d else None,
                    "fecha_min_llegada":    fecha_min,
                })
                mejoro = True
                break

    return df, pd.DataFrame(log)


# ============================================================================
# RESÚMENES
# ============================================================================


_PLAN_COLS = [
    "COD_PROVEEDOR", "PROVEEDOR", "ORIGEN", "mes_llegada", "modo_envio",
    "contenedor_ids", "n_skus", "n_lineas_oc", "cbm_total", "contenedores",
    "n40", "n20", "cap_cbm", "fill_pct", "cbm_sobrante", "costo_lcl_ref",
    "estado", "movimiento_notas",
]


def _build_plan(df_imp: pd.DataFrame, log_df: pd.DataFrame | None, cfg: dict) -> pd.DataFrame:
    if df_imp.empty:
        return pd.DataFrame(columns=_PLAN_COLS)
    rows = []
    for (prov, orig, mes), grp in df_imp.groupby(
        ["COD_PROVEEDOR", "ORIGEN", "mes_llegada"], sort=False
    ):
        cbm_total = grp["cbm_orden"].sum()
        n40, n20, cap_cbm, fill = _container_combo(cbm_total, cfg["cap20"], cfg["cap40"])
        modo, estado = _classify(cbm_total, fill, cfg["cap20"], cfg["permitir_lcl"],
                                 cfg["lcl_umbral"], cfg["min_fill"])
        cont_ids = (
            ",".join(sorted(grp["contenedor_id"].unique()))
            if "contenedor_id" in grp.columns else ""
        )
        costo_lcl = round(cbm_total * cfg["costo_lcl_cbm"], 2) if modo == "LCL" else 0.0

        notas = ""
        if log_df is not None and not log_df.empty:
            movs = log_df[log_df["mes_destino"] == mes]
            partes = []
            for _, mv in movs.iterrows():
                dir_t = "ADELANTADO" if mv["delta_meses"] < 0 else "ATRASADO"
                partes.append(
                    f"{dir_t} {abs(mv['delta_meses'])} meses desde "
                    f"{mv['mes_origen'].strftime('%Y-%m-%d')} "
                    f"({int(mv['n_ocs_movidas'])} OCs / {mv['cbm_movido']:.1f} CBM | "
                    f"fill origen {mv['fill_origen_antes']:.0%} → "
                    f"destino {mv['fill_destino_antes']:.0%} → "
                    f"{mv['fill_destino_despues']:.0%})"
                )
            notas = "; ".join(partes)

        rows.append({
            "COD_PROVEEDOR":    prov,
            "PROVEEDOR":        grp["PROVEEDOR"].iloc[0] if "PROVEEDOR" in grp.columns else "",
            "ORIGEN":           orig,
            "mes_llegada":      mes,
            "modo_envio":       modo,
            "contenedor_ids":   cont_ids,
            "n_skus":           grp["id_material"].nunique(),
            "n_lineas_oc":      len(grp),
            "cbm_total":        round(cbm_total, 3),
            "contenedores":     n40 + n20,
            "n40":              n40,
            "n20":              n20,
            "cap_cbm":          cap_cbm,
            "fill_pct":         round(fill, 4),
            "cbm_sobrante":     round(cap_cbm - cbm_total, 3),
            "costo_lcl_ref":    costo_lcl,
            "estado":           estado,
            "movimiento_notas": notas,
        })
    return pd.DataFrame(rows)


def _resumen_origen(plan: pd.DataFrame) -> pd.DataFrame:
    if plan.empty or "COD_PROVEEDOR" not in plan.columns:
        return pd.DataFrame(columns=[
            "COD_PROVEEDOR", "ORIGEN", "embarques_fcl", "embarques_lcl",
            "n40", "n20", "contenedores", "cbm_total", "cbm_fcl", "cbm_lcl",
            "fill_promedio_fcl", "costo_lcl_ref",
        ])
    rows = []
    for (prov, orig), grp in plan.groupby(["COD_PROVEEDOR", "ORIGEN"], sort=False):
        fcl = grp[grp["modo_envio"] == "FCL"]
        lcl = grp[grp["modo_envio"] == "LCL"]
        rows.append({
            "COD_PROVEEDOR":      prov,
            "ORIGEN":             orig,
            "embarques_fcl":      len(fcl),
            "embarques_lcl":      len(lcl),
            "n40":                int(fcl["n40"].sum()),
            "n20":                int(fcl["n20"].sum()),
            "contenedores":       int(fcl["contenedores"].sum()),
            "cbm_total":          round(grp["cbm_total"].sum(), 3),
            "cbm_fcl":            round(fcl["cbm_total"].sum(), 3),
            "cbm_lcl":            round(lcl["cbm_total"].sum(), 3),
            "fill_promedio_fcl":  round(fcl["fill_pct"].mean(), 4) if len(fcl) > 0 else 0,
            "costo_lcl_ref":      round(lcl["costo_lcl_ref"].sum(), 2),
        })
    return pd.DataFrame(rows)


# ============================================================================
# RECONSTRUCCIÓN CSV EN FORMATO DE ENTRADA
# ============================================================================


def _build_compra_proyectada(
    df_raw: pd.DataFrame,
    df_imp_opt: pd.DataFrame,
    csv_cols: list,
) -> pd.DataFrame:
    """Genera un DataFrame con el mismo esquema que el CSV de entrada pero con
    las fechas de OC ajustadas según la optimización (delta_meses).

    Reglas:
    - Filas IMPORTADO movidas (delta_meses ≠ 0): ``fecha`` desplazada N meses.
    - Resto de filas (NACIONAL, orden=0, fuera de ventana, históricas): sin cambios.
    - ``valor_compra`` recalculada en todas las filas como ``orden × costo_promedio``.
    - La salida contiene exactamente las columnas originales del CSV (``csv_cols``),
      sin columnas Snowflake añadidas por ``_enrich``.
    """
    # Trabajar sobre una copia completa de df_raw (todas las filas: imports,
    # nacionales, orden=0, fechas pasadas).
    out = df_raw.copy()

    # ── Construir mapa id → delta_meses ─────────────────────────────────
    # Solo filas que realmente se movieron (delta != 0).
    if "delta_meses" in df_imp_opt.columns:
        moved = df_imp_opt[df_imp_opt["delta_meses"] != 0]
    else:
        moved = pd.DataFrame()

    if not moved.empty and "id" in moved.columns and "id" in out.columns:
        # Ruta rápida: join por id único de fila (columna 'id' del CSV original)
        delta_map = moved.set_index("id")["delta_meses"].to_dict()
        out["_delta"] = out["id"].map(delta_map).fillna(0).astype(int)

    elif not moved.empty and "fecha_oc" in moved.columns and "id_material" in out.columns:
        # Fallback: join por (id_material, año-mes).
        # Se usa fecha_oc (primer día del mes de la OC original) para el join.
        tmp = moved[["id_material", "fecha_oc", "delta_meses"]].copy()
        tmp["_ym"] = pd.to_datetime(tmp["fecha_oc"]).dt.to_period("M").astype(str)
        out["_ym"] = pd.to_datetime(out["fecha"]).dt.to_period("M").astype(str)
        out = out.merge(
            tmp[["id_material", "_ym", "delta_meses"]]
            .drop_duplicates(subset=["id_material", "_ym"]),
            on=["id_material", "_ym"],
            how="left",
        )
        out["_delta"] = out["delta_meses"].fillna(0).astype(int)
        out = out.drop(columns=["delta_meses", "_ym"], errors="ignore")

    else:
        # Sin movimientos (optimización no desplazó nada, o primera ejecución)
        out["_delta"] = 0

    # ── Actualizar fecha para filas movidas ──────────────────────────────
    mask_moved = out["_delta"] != 0
    if mask_moved.any():
        out.loc[mask_moved, "fecha"] = out.loc[mask_moved].apply(
            lambda r: _add_months(pd.Timestamp(r["fecha"]), int(r["_delta"])),
            axis=1,
        )

    # ── Recalcular valor_compra ──────────────────────────────────────────
    if {"orden", "costo_promedio", "valor_compra"}.issubset(out.columns):
        out["valor_compra"] = out["orden"] * out["costo_promedio"]

    # ── Formatear fecha como YYYY-MM-DD ──────────────────────────────────
    out["fecha"] = pd.to_datetime(out["fecha"]).dt.strftime("%Y-%m-%d")

    # ── Devolver solo las columnas originales del CSV, en su orden original ─
    # Esto excluye columnas añadidas por _enrich (PROCEDENCIA, VOLUMEN, etc.)
    final_cols = [c for c in csv_cols if c in out.columns]
    return out[final_cols].reset_index(drop=True)


# ============================================================================
# EXPORTACIÓN EXCEL (multi-hoja, en memoria)
# ============================================================================

_README = [
    ("SECCIÓN", "VARIABLE / PARÁMETRO", "DESCRIPCIÓN"),
    ("CONFIG", "CAP_20", "Capacidad útil contenedor 20ft en CBM"),
    ("CONFIG", "CAP_40", "Capacidad útil contenedor 40ft en CBM"),
    ("CONFIG", "MIN_FILL_OBJETIVO", "Fill mínimo objetivo. OK ≥ 90%, BAJO 70-90%, CRITICO < 70%"),
    ("CONFIG", "MAX_MESES_MOVIMIENTO", "Ventana máxima de adelanto/atraso en meses (±)"),
    ("CONFIG", "MOI_MIN", "MOI mínimo aceptable. Si MOI < MOI_MIN → bloqueo (riesgo quiebre)"),
    ("CONFIG", "MOI_MAX", "MOI máximo aceptable. Si MOI > MOI_MAX → sobrestock"),
    ("CONFIG", "MOI_FILL_OVERRIDE", "Mejora mínima fill (pp) para tolerar sobrestock"),
    ("CONFIG", "LCL_UMBRAL", "Fracción de CAP_20 para umbral FCL/LCL (0.75 = 18.75 CBM)"),
    ("CONFIG", "COSTO_LCL_CBM", "Costo referencial LCL en USD/CBM (solo informativo)"),
    ("CONFIG", "PERMITIR_LCL", "True = acepta LCL; False = todo fuerza FCL"),
    ("ENTRADA", "id_material", "Código del SKU"),
    ("ENTRADA", "fecha", "Mes de proyección (primera del mes)"),
    ("ENTRADA", "lead_time", "Días desde emisión OC hasta llegada al CD"),
    ("ENTRADA", "orden", "Unidades a comprar para no quebrar a la fecha"),
    ("ENTRADA", "VOLUMEN", "CBM por unidad del SKU"),
    ("ENTRADA", "stock_cd", "Stock proyectado en el CD"),
    ("ENTRADA", "stock_total", "Stock proyectado incluyendo entradas y salidas"),
    ("ENTRADA", "demanda", "Demanda mensual proyectada"),
    ("ENTRADA", "rop", "Punto de reorden mínimo aceptable"),
    ("ENTRADA", "PROCEDENCIA", "IMPORTADO o NACIONAL"),
    ("ENTRADA", "ORIGEN", "País de origen del SKU"),
    ("ENTRADA", "COD_PROVEEDOR", "Código del proveedor (agrupación primaria)"),
    ("ENTRADA", "MIX_OFICIAL", "Segmento SKU — solo procesa valor MIX"),
    ("ENTRADA", "costo_promedio", "Costo unitario en USD"),
    ("CALC", "fecha_oc", "Primer día del mes de la OC"),
    ("CALC", "fecha_llegada", "Primer día del mes de llegada (fecha_oc + lead_time días)"),
    ("CALC", "mes_llegada", "Mes de llegada normalizado YYYY-MM-01"),
    ("CALC", "mes_llegada_orig", "Mes de llegada original antes de optimización"),
    ("CALC", "cbm_orden", "Volumen total de la OC (orden × VOLUMEN)"),
    ("CALC", "valor_orden", "Valor total en USD (orden × costo_promedio)"),
    ("CALC", "moi_en_oc", "MOI del SKU en el mes de la OC"),
    ("CALC", "delta_meses", "Meses de adelanto (−) o atraso (+) aplicados"),
    ("CALC", "contenedor_id", "ID correlativo del contenedor (A000000001...)"),
    ("CALC", "fill_pct_cont", "Fill del contenedor = cbm_contenedor / cap_contenedor"),
    ("MOI", "Fórmula", "stock_total(mes) / avg(demanda_mes, mes+1, mes+2)"),
    ("MOI", "Especial", "Demanda = 0 → MOI = inf (no bloquea). Sin datos → MOI = None"),
    ("ALGORITMO", "Consolidación", "Greedy: mueve mes con menor CBM hacia el que maximice fill destino"),
    ("ALGORITMO", "Asignación", "Best Fit Decreasing: OCs mayor → menor CBM"),
    ("ALGORITMO", "Contenedores", "Minimiza número de contenedores; igual cantidad → maximiza fill"),
    ("ESTADOS", "OK", "Fill ≥ 90%"),
    ("ESTADOS", "BAJO", "Fill 70–90%"),
    ("ESTADOS", "CRITICO", "Fill < 70%"),
    ("ESTADOS", "LCL", "Carga suelta (CBM < LCL_UMBRAL × CAP_20)"),
]


def _df_to_ws(ws, df: pd.DataFrame):
    DATE_COLS = {
        "mes_llegada", "mes_llegada_orig", "mes_origen", "mes_destino",
        "fecha_oc", "fecha_llegada", "fecha_min_llegada",
    }
    ws.append(list(df.columns))
    for _, row in df.iterrows():
        out = []
        for col, val in zip(df.columns, row):
            if col in DATE_COLS and pd.notna(val) and not isinstance(val, str):
                try:
                    ts = pd.Timestamp(val)
                    val = pd.Timestamp(ts.year, ts.month, 1)
                except Exception:
                    pass
            out.append(val)
        ws.append(out)


def _style_ws(ws, alt=True):
    for cell in ws[1]:
        cell.fill = _HDR_FILL
        cell.font = _HDR_FONT
        cell.alignment = _CENTER
    for row_idx in range(2, ws.max_row + 1):
        fill = _ALT_FILL if (alt and row_idx % 2 == 0) else _WHT_FILL
        for cell in ws[row_idx]:
            cell.fill = fill
            cell.font = _NRM_FONT
            cell.alignment = _LEFT
    for col in ws.columns:
        max_len = max((len(str(c.value)) if c.value else 0) for c in col)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 4, 40)


def _export_excel(
    df_imp_base: pd.DataFrame,
    df_imp_opt: pd.DataFrame,
    df_nac: pd.DataFrame,
    plan_base: pd.DataFrame,
    plan_opt: pd.DataFrame,
    log_df: pd.DataFrame,
    resumen: pd.DataFrame,
) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)

    OC_COLS = [
        "contenedor_id", "tipo_cont", "contenedores_grupo",
        "COD_PROVEEDOR", "PROVEEDOR", "ORIGEN", "id_material",
        "FAMILIA", "MARCA", "mes_llegada_orig", "mes_llegada", "delta_meses",
        "fecha_oc", "lead_time", "fecha_llegada", "orden", "VOLUMEN", "cbm_orden",
        "valor_orden", "costo_promedio", "stock_cd", "demanda", "rop", "moi_en_oc",
        "accion", "cbm_contenedor", "cap_contenedor", "fill_pct_cont",
    ]

    # OCs_Optimizadas
    ws = wb.create_sheet("OCs_Optimizadas")
    _df_to_ws(ws, df_imp_opt[[c for c in OC_COLS if c in df_imp_opt.columns]])
    _style_ws(ws)

    # Plan_Comparativo
    ws = wb.create_sheet("Plan_Comparativo")
    b = plan_base.copy(); b.insert(0, "escenario", "BASE")
    o = plan_opt.copy();  o.insert(0, "escenario", "OPTIMIZADO")
    comp = pd.concat([b, o]).sort_values(["COD_PROVEEDOR", "ORIGEN", "mes_llegada", "escenario"])
    _df_to_ws(ws, comp)
    _style_ws(ws)

    # Plan_Optimizado / Plan_Base
    for name, df in [("Plan_Optimizado", plan_opt), ("Plan_Base", plan_base)]:
        ws = wb.create_sheet(name)
        _df_to_ws(ws, df)
        _style_ws(ws)

    # Contenedor_SKU
    ws = wb.create_sheet("Contenedor_SKU")
    sku_cols = [
        "contenedor_id", "COD_PROVEEDOR", "ORIGEN", "mes_llegada",
        "id_material", "FAMILIA", "orden", "cbm_orden", "valor_orden",
        "cbm_contenedor", "cap_contenedor", "fill_pct_cont",
    ]
    _df_to_ws(ws, df_imp_opt[[c for c in sku_cols if c in df_imp_opt.columns]])
    _style_ws(ws)

    # Movimientos_OC
    ws = wb.create_sheet("Movimientos_OC")
    if not log_df.empty:
        _df_to_ws(ws, log_df)
        _style_ws(ws)

    # Resumen_ORIGEN
    ws = wb.create_sheet("Resumen_ORIGEN")
    _df_to_ws(ws, resumen)
    _style_ws(ws)

    # OCs_Nacional
    ws = wb.create_sheet("OCs_Nacional")
    _df_to_ws(ws, df_nac)
    _style_ws(ws)

    # README
    ws = wb.create_sheet("README")
    ws.append(["SECCIÓN", "VARIABLE / PARÁMETRO", "DESCRIPCIÓN"])
    for cell in ws[1]:
        cell.fill = _HDR_FILL
        cell.font = _HDR_FONT
        cell.alignment = _CENTER

    prev_sec = None
    for i, (sec, var, desc) in enumerate(_README[1:], start=2):
        ws.append([sec, var, desc])
        row_cells = ws[i]
        if sec != prev_sec:
            for cell in row_cells:
                cell.fill = _SEC_FILL
                cell.font = Font(bold=True, color="FFFFFF", size=10)
        else:
            fill = _ALT_FILL if i % 2 == 0 else _WHT_FILL
            for cell in row_cells:
                cell.fill = fill
                cell.font = _NRM_FONT
        for cell in row_cells:
            cell.alignment = _LEFT
        prev_sec = sec
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 24
    ws.column_dimensions["C"].width = 70

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ============================================================================
# HELPERS DE RENDER
# ============================================================================


def _kpi(label: str, value: str, color: str = COLORS["primary"]) -> None:
    st.html(
        f'<div style="background:#fff;padding:1.1rem;border-radius:10px;'
        f"text-align:center;border-left:4px solid {color};"
        f'box-shadow:0 1px 3px rgba(0,0,0,0.06);">'
        f'<div style="font-size:1.5rem;font-weight:700;color:{color};">{value}</div>'
        f'<div style="font-size:0.78rem;color:#666;margin-top:0.15rem;">{label}</div>'
        f"</div>"
    )


def _plan_stats(plan: pd.DataFrame):
    fcl = plan[plan["modo_envio"] == "FCL"]
    lcl = plan[plan["modo_envio"] == "LCL"]
    return {
        "cbm":       round(plan["cbm_total"].sum(), 1),
        "n40":       int(fcl["n40"].sum()),
        "n20":       int(fcl["n20"].sum()),
        "cont_fcl":  int(fcl["contenedores"].sum()),
        "lcl":       len(lcl),
        "fill_avg":  round(fcl["fill_pct"].mean() * 100, 1) if len(fcl) > 0 else 0.0,
        "embarques": len(plan),
        "lcl_cost":  round(lcl["costo_lcl_ref"].sum(), 2),
    }


# ============================================================================
# ============================================================================
# HELPERS DE DESCARGA
# ============================================================================


def _dl_buttons(df: pd.DataFrame, stem: str, label: str = "") -> None:
    """Renderiza botones de descarga Excel y CSV uno al lado del otro."""
    if df.empty:
        return
    lbl = f" {label}" if label else ""
    c1, c2, *_ = st.columns([1, 1, 4])
    # Excel
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as wr:
        df.to_excel(wr, index=False, sheet_name="Datos")
    with c1:
        st.download_button(
            f"⬇ Excel{lbl}",
            data=buf.getvalue(),
            file_name=f"{stem}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_xlsx_{stem}",
        )
    # CSV
    with c2:
        st.download_button(
            f"⬇ CSV{lbl}",
            data=df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{stem}.csv",
            mime="text/csv",
            key=f"dl_csv_{stem}",
        )


# ============================================================================
# TABS DE RENDER
# ============================================================================


def _tab_resumen(plan_base: pd.DataFrame, plan_opt: pd.DataFrame) -> None:
    b = _plan_stats(plan_base)
    o = _plan_stats(plan_opt)

    # KPIs comparativos
    cols = st.columns(4)
    delta_cont = o["cont_fcl"] - b["cont_fcl"]
    delta_fill = o["fill_avg"] - b["fill_avg"]
    delta_lcl  = o["lcl"] - b["lcl"]

    with cols[0]:
        _kpi("CBM Total", f"{o['cbm']:,.1f}", COLORS["primary"])
    with cols[1]:
        sign = "+" if delta_cont >= 0 else ""
        color = COLORS["status_on_track"] if delta_cont <= 0 else COLORS["status_at_risk"]
        _kpi("Contenedores FCL (Opt)", f"{o['cont_fcl']} ({sign}{delta_cont})", color)
    with cols[2]:
        sign = "+" if delta_fill >= 0 else ""
        color = COLORS["status_on_track"] if delta_fill >= 0 else COLORS["status_at_risk"]
        _kpi("Fill Prom FCL (Opt)", f"{o['fill_avg']:.1f}% ({sign}{delta_fill:.1f}pp)", color)
    with cols[3]:
        sign = "+" if delta_lcl >= 0 else ""
        color = COLORS["status_at_risk"] if delta_lcl > 0 else COLORS["status_on_track"]
        _kpi("Embarques LCL (Opt)", f"{o['lcl']} ({sign}{delta_lcl})", color)

    st.markdown("")

    # Gráfico fill BASE vs OPT por ORIGEN
    col1, col2 = st.columns(2)

    with col1:
        origs = sorted(plan_opt["ORIGEN"].unique())
        fills_b, fills_o = [], []
        for orig in origs:
            fcl_b = plan_base[(plan_base["ORIGEN"] == orig) & (plan_base["modo_envio"] == "FCL")]
            fcl_o = plan_opt[(plan_opt["ORIGEN"] == orig) & (plan_opt["modo_envio"] == "FCL")]
            fills_b.append(round(fcl_b["fill_pct"].mean() * 100, 1) if len(fcl_b) > 0 else 0)
            fills_o.append(round(fcl_o["fill_pct"].mean() * 100, 1) if len(fcl_o) > 0 else 0)

        fig = go.Figure()
        fig.add_trace(go.Bar(
            name="Base", x=origs, y=fills_b,
            marker_color=COLORS["medium_gray"],
            text=[f"{v:.0f}%" for v in fills_b], textposition="auto",
        ))
        fig.add_trace(go.Bar(
            name="Optimizado", x=origs, y=fills_o,
            marker_color=COLORS["primary"],
            text=[f"{v:.0f}%" for v in fills_o], textposition="auto",
        ))
        fig.add_hline(y=90, line_dash="dash", line_color=COLORS["status_on_track"],
                      annotation_text="Objetivo 90%")
        fig.update_layout(**dorel_layout(
            title=dict(text="Fill FCL por Origen (%)", font=dict(size=13)),
            barmode="group", height=380,
            yaxis=dict(title="Fill %", range=[0, 105]),
            legend=dict(orientation="h", y=-0.15),
        ))
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        # Distribución de estados OPTIMIZADO
        estado_counts = plan_opt["estado"].value_counts().reset_index()
        estado_counts.columns = ["estado", "n"]
        estado_colors = {
            "OK":     COLORS["status_on_track"],
            "BAJO":   COLORS["status_at_risk"],
            "CRITICO": COLORS["status_critical"],
            "LCL":    COLORS["tertiary_blue"],
        }
        fig = go.Figure(go.Pie(
            labels=estado_counts["estado"],
            values=estado_counts["n"],
            hole=0.52,
            marker=dict(colors=[estado_colors.get(e, COLORS["medium_gray"])
                                 for e in estado_counts["estado"]]),
            textinfo="label+value",
            hovertemplate="%{label}: %{value} embarques<extra></extra>",
        ))
        fig.update_layout(**dorel_layout(
            title=dict(text="Estado Embarques — Optimizado", font=dict(size=13)),
            height=380, showlegend=False,
        ))
        st.plotly_chart(fig, use_container_width=True)

    # Tabla resumen comparativa por ORIGEN
    st.markdown("##### Comparativa por COD_PROVEEDOR / ORIGEN")
    all_keys = set(
        zip(plan_base["COD_PROVEEDOR"], plan_base["ORIGEN"])
    ) | set(zip(plan_opt["COD_PROVEEDOR"], plan_opt["ORIGEN"]))

    comp_rows = []
    for prov, orig in sorted(all_keys):
        bg = plan_base[(plan_base["COD_PROVEEDOR"] == prov) & (plan_base["ORIGEN"] == orig)]
        og = plan_opt[(plan_opt["COD_PROVEEDOR"] == prov) & (plan_opt["ORIGEN"] == orig)]

        def _gs(g):
            fcl = g[g["modo_envio"] == "FCL"]
            lcl = g[g["modo_envio"] == "LCL"]
            return (
                int(fcl["contenedores"].sum()),
                len(lcl),
                round(fcl["fill_pct"].mean() * 100, 1) if len(fcl) > 0 else 0.0,
                round(g["cbm_total"].sum(), 1),
            )

        bc, bl, bf, bcbm = _gs(bg) if len(bg) > 0 else (0, 0, 0.0, 0.0)
        oc, ol, of_, ocbm = _gs(og) if len(og) > 0 else (0, 0, 0.0, 0.0)
        comp_rows.append({
            "COD_PROVEEDOR": prov,
            "ORIGEN": orig,
            "Cont Base": bc, "Cont Opt": oc,
            "LCL Base": bl, "LCL Opt": ol,
            "Fill Base%": bf, "Fill Opt%": of_,
            "CBM": ocbm,
        })

    df_comp = pd.DataFrame(comp_rows)
    st.dataframe(
        df_comp,
        column_config={
            "Fill Base%": st.column_config.NumberColumn("Fill Base%", format="%.1f%%"),
            "Fill Opt%":  st.column_config.NumberColumn("Fill Opt%",  format="%.1f%%"),
            "CBM":        st.column_config.NumberColumn("CBM", format="%.1f"),
        },
        use_container_width=True, hide_index=True,
    )
    _dl_buttons(df_comp, "resumen_comparativo", "Resumen")


def _tab_planes(plan_base: pd.DataFrame, plan_opt: pd.DataFrame) -> None:
    sub_b, sub_o = st.tabs(["Plan Base", "Plan Optimizado"])

    fill_cfg = {
        "fill_pct": st.column_config.ProgressColumn("Fill%", min_value=0, max_value=1, format="%.0%%"),
        "cbm_total": st.column_config.NumberColumn("CBM Total", format="%.1f"),
        "cbm_sobrante": st.column_config.NumberColumn("CBM Sobrante", format="%.1f"),
        "costo_lcl_ref": st.column_config.NumberColumn("Costo LCL Ref (USD)", format="$%.2f"),
        "mes_llegada": st.column_config.DateColumn("Mes Llegada", format="YYYY-MM-DD"),
    }

    with sub_b:
        st.dataframe(plan_base, column_config=fill_cfg,
                     use_container_width=True, hide_index=True, height=420)
        _dl_buttons(plan_base, "plan_base", "Plan Base")
    with sub_o:
        st.dataframe(plan_opt, column_config=fill_cfg,
                     use_container_width=True, hide_index=True, height=420)
        _dl_buttons(plan_opt, "plan_optimizado", "Plan Optimizado")


def _tab_detalle(df_imp_opt: pd.DataFrame) -> None:
    OC_COLS = [
        "contenedor_id", "tipo_cont", "contenedores_grupo",
        "COD_PROVEEDOR", "PROVEEDOR", "ORIGEN", "id_material",
        "FAMILIA", "MARCA", "mes_llegada_orig", "mes_llegada", "delta_meses",
        "fecha_oc", "lead_time", "fecha_llegada", "orden", "VOLUMEN", "cbm_orden",
        "valor_orden", "costo_promedio", "stock_cd", "demanda", "rop", "moi_en_oc",
        "accion", "cbm_contenedor", "cap_contenedor", "fill_pct_cont",
    ]
    df_show = df_imp_opt[[c for c in OC_COLS if c in df_imp_opt.columns]].copy()

    # Filtros rápidos
    col1, col2 = st.columns(2)
    with col1:
        origenes = sorted(df_show["ORIGEN"].unique())
        sel_orig = st.multiselect("Filtrar ORIGEN", origenes, key="opt_fil_orig")
        if sel_orig:
            df_show = df_show[df_show["ORIGEN"].isin(sel_orig)]
    with col2:
        provs = sorted(df_show["COD_PROVEEDOR"].unique())
        sel_prov = st.multiselect("Filtrar COD_PROVEEDOR", provs, key="opt_fil_prov")
        if sel_prov:
            df_show = df_show[df_show["COD_PROVEEDOR"].isin(sel_prov)]

    st.caption(f"{len(df_show):,} OCs mostradas")
    st.dataframe(
        df_show,
        column_config={
            "contenedor_id":    st.column_config.TextColumn("ID Contenedor"),
            "tipo_cont":        st.column_config.TextColumn("Tipo"),
            "contenedores_grupo": st.column_config.TextColumn("Embarque"),
            "fill_pct_cont":    st.column_config.ProgressColumn("Fill Cont%", min_value=0, max_value=1, format="%.0%%"),
            "cbm_orden":        st.column_config.NumberColumn("CBM Orden", format="%.3f"),
            "cbm_contenedor":   st.column_config.NumberColumn("CBM Cont.", format="%.1f"),
            "cap_contenedor":   st.column_config.NumberColumn("Cap. Cont.", format="%.0f"),
            "moi_en_oc":        st.column_config.NumberColumn("MOI OC", format="%.1f"),
            "valor_orden":      st.column_config.NumberColumn("Valor (USD)", format="$%.2f"),
            "mes_llegada":      st.column_config.DateColumn("Mes Llegada", format="YYYY-MM-DD"),
            "mes_llegada_orig":  st.column_config.DateColumn("Mes Orig.", format="YYYY-MM-DD"),
        },
        use_container_width=True, hide_index=True, height=500,
    )

    _dl_buttons(df_show, "detalle_ocs_optimizadas", "Detalle OCs")


def _tab_movimientos(log_df: pd.DataFrame) -> None:
    if log_df.empty:
        st.info("No se realizaron movimientos durante la optimización.")
        return

    st.caption(f"{len(log_df)} movimientos aplicados")
    st.dataframe(
        log_df,
        column_config={
            "fill_origen_antes":    st.column_config.NumberColumn("Fill Orig. Antes",  format="%.0%%"),
            "fill_destino_antes":   st.column_config.NumberColumn("Fill Dest. Antes",  format="%.0%%"),
            "fill_destino_despues": st.column_config.NumberColumn("Fill Dest. Después", format="%.0%%"),
            "cbm_movido":           st.column_config.NumberColumn("CBM Movido", format="%.1f"),
            "moi_origen_prom":      st.column_config.NumberColumn("MOI Orig. Prom.", format="%.2f"),
            "moi_destino_min":      st.column_config.NumberColumn("MOI Dest. Min.", format="%.2f"),
            "mes_origen":           st.column_config.DateColumn("Mes Origen",  format="YYYY-MM-DD"),
            "mes_destino":          st.column_config.DateColumn("Mes Destino", format="YYYY-MM-DD"),
            "fecha_min_llegada":    st.column_config.DateColumn("Fecha Min", format="YYYY-MM-DD"),
        },
        use_container_width=True, hide_index=True, height=420,
    )
    _dl_buttons(log_df, "movimientos_consolidacion", "Movimientos")


# ============================================================================
# PUNTO DE ENTRADA
# ============================================================================


def render_optimizador_compras(conn) -> None:  # noqa: ARG001 (conn no usado, convención del portal)
    from utils.ui_components import page_header

    st.html(
        page_header(
            "Optimizador de Compras y Contenedores",
            "Consolida OCs de importación en FCL/LCL maximizando el fill y respetando MOI y ROP",
        )
    )

    # ── Parámetros en sidebar ────────────────────────────────────────────
    with st.sidebar:
        st.markdown("---")
        st.markdown("**Parámetros del Optimizador**")

        cap20 = st.number_input("Cap. 20ft (CBM)", 10.0, 50.0, 25.0, 0.5,
                                key="opt_cap20", help="CBM útiles de un contenedor 20'")
        cap40 = st.number_input("Cap. 40ft (CBM)", 30.0, 80.0, 55.0, 0.5,
                                key="opt_cap40", help="CBM útiles de un contenedor 40'")
        min_fill = st.slider("Fill objetivo (%)", 60, 100, 90, 1,
                             key="opt_min_fill") / 100
        max_meses = st.number_input("Ventana máx. movimiento (meses)", 1, 12, 5, 1,
                                    key="opt_max_meses")
        moi_min = st.number_input("MOI mínimo", 0.5, 5.0, 2.5, 0.1,
                                  key="opt_moi_min")
        moi_max = st.number_input("MOI máximo", 2.0, 24.0, 6.0, 0.5,
                                  key="opt_moi_max")
        moi_fill_override = st.slider("Fill override sobrestock (pp)", 5, 40, 15, 1,
                                      key="opt_moi_fill_ov") / 100
        _sb_lcl = st.session_state.get("opt_lcl", True)
        if _sb_lcl:
            lcl_umbral = st.slider("Umbral LCL (fracción 20ft)", 0.40, 1.00, 0.75, 0.05,
                                   key="opt_lcl_umbral")
            costo_lcl_cbm = st.number_input("Costo LCL ref (USD/CBM)", 0.0, 200.0, 45.0, 1.0,
                                            key="opt_costo_lcl",
                                            help="Solo referencial, no afecta la optimización")
        else:
            lcl_umbral = 0.75
            costo_lcl_cbm = 45.0

    # ── Modo de envío ────────────────────────────────────────────────────
    st.markdown("#### Modo de envío")
    col_lcl, col_info = st.columns([2, 3])
    with col_lcl:
        permitir_lcl = st.toggle(
            "Permitir embarques LCL",
            value=st.session_state.get("opt_lcl", True),
            key="opt_lcl",
            help=(
                "LCL (Less than Container Load): habilita embarques parciales "
                "cuando el volumen no alcanza para llenar un contenedor completo. "
                "Si está desactivado, sólo se generan FCL y las OCs con poco "
                "volumen se consolidan o posponen."
            ),
        )
    with col_info:
        if permitir_lcl:
            st.info(
                "✅ **LCL habilitado** — embarques con volumen insuficiente "
                "se despacharán en LCL (costo por CBM configurable en el panel lateral)."
            )
        else:
            st.warning(
                "⛔ **Solo FCL** — OCs con volumen insuficiente se consolidarán "
                "o diferirán hasta completar un contenedor. No se generarán "
                "embarques LCL."
            )
    st.markdown("---")

    cfg = dict(
        cap20=cap20, cap40=cap40, min_fill=min_fill,
        max_meses=int(max_meses), moi_min=moi_min, moi_max=moi_max,
        moi_fill_override=moi_fill_override, lcl_umbral=lcl_umbral,
        permitir_lcl=permitir_lcl, costo_lcl_cbm=costo_lcl_cbm,
    )

    # ── Carga de archivo ─────────────────────────────────────────────────
    uploaded = st.file_uploader(
        "Cargar CSV de base de compras",
        type=["csv"],
        help="Archivo con columnas id_material, orden, fecha, lead_time, VOLUMEN, etc.",
        key="opt_uploader",
    )

    if uploaded is None:
        st.info("Sube el CSV de base de compras para iniciar el análisis.")
        return

    # Invalidar resultados si cambió el archivo o los parámetros
    cache_key = f"{uploaded.name}_{uploaded.size}_{hash(str(cfg))}"
    if st.session_state.get("opt_cache_key") != cache_key:
        for k in ["opt_results", "opt_cache_key"]:
            st.session_state.pop(k, None)

    if "opt_results" not in st.session_state:
        with st.spinner("Leyendo CSV..."):
            try:
                df_raw, hoy, csv_cols = _load(uploaded)
            except Exception as e:
                st.error(f"Error al leer el archivo: {e}")
                return

        with st.spinner("Enriqueciendo con maestra Snowflake..."):
            df_raw, warns = _enrich(df_raw, conn)

        # Mostrar diagnóstico antes de continuar
        with st.expander("📋 Diagnóstico de columnas", expanded=bool(warns)):
            cols_ok  = [c for c in ["id_material", "fecha", "orden", "lead_time",
                                     "stock_cd", "stock_total", "demanda", "rop",
                                     "costo_promedio", "MIX_OFICIAL", "PROCEDENCIA",
                                     "COD_PROVEEDOR", "ORIGEN", "VOLUMEN"]
                        if c in df_raw.columns and not (df_raw[c] == "" ).all()
                        and not (pd.to_numeric(df_raw[c], errors="coerce").fillna(0) == 0).all()]
            cols_warn = [c for c in ["VOLUMEN", "ORIGEN", "COD_PROVEEDOR"]
                         if c not in df_raw.columns
                         or (df_raw[c].astype(str).str.strip() == "").all()
                         or (pd.to_numeric(df_raw[c], errors="coerce").fillna(0) == 0).all()]
            st.markdown(f"**Filas en CSV:** {len(df_raw):,} · **SKUs únicos:** {df_raw['id_material'].nunique():,}")
            for w in warns:
                st.warning(w)
            if cols_warn:
                st.error(f"Columnas con problema: {', '.join(cols_warn)}")

        # Bloquear si VOLUMEN es todo cero
        if "VOLUMEN" not in df_raw.columns or (df_raw["VOLUMEN"] == 0).all():
            st.error("No se puede optimizar sin VOLUMEN (CBM/unidad). Agrega la columna al CSV y vuelve a subir.")
            return

        with st.spinner("Optimizando..."):
            df = _apply_filters(df_raw, hoy)

            if df.empty:
                st.warning("No hay filas con MIX='MIX', orden>0 y fecha>=hoy. Revisa el archivo.")
                return

            moi_idx = _build_moi(df)
            df_imp, df_nac = _plan_base(df, moi_idx)

            if df_imp.empty:
                st.warning(
                    f"No se encontraron OCs de tipo IMPORTADO después de aplicar filtros. "
                    f"Valores únicos de PROCEDENCIA en el archivo: "
                    f"{df['PROCEDENCIA'].unique().tolist()}"
                )
                return

            id_gen = _IDGen()
            df_imp_base = _assign_all(df_imp.copy(), id_gen, cap20, cap40)
            plan_base = _build_plan(df_imp_base, None, cfg)

            df_imp_opt, log_df = _consolidate(df_imp.copy(), moi_idx, hoy, cfg)
            df_imp_opt = _assign_all(df_imp_opt, id_gen, cap20, cap40)
            plan_opt = _build_plan(df_imp_opt, log_df, cfg)
            resumen = _resumen_origen(plan_opt)

            # CSV en formato compra_proyectada con fechas optimizadas.
            # csv_cols captura las columnas ANTES de _enrich, garantizando que
            # el output tenga exactamente las mismas cabeceras que el archivo
            # de entrada (incluyendo nacionales y filas excluidas de la optimizacion).
            cp_opt_df  = _build_compra_proyectada(df_raw, df_imp_opt, csv_cols)
            cp_opt_csv = cp_opt_df.to_csv(index=False).encode("utf-8-sig")

            st.session_state["opt_results"] = dict(
                df_imp_base=df_imp_base, df_imp_opt=df_imp_opt,
                df_nac=df_nac, plan_base=plan_base, plan_opt=plan_opt,
                log_df=log_df, resumen=resumen,
                cp_opt_csv=cp_opt_csv,
                n_imp=len(df_imp), n_nac=len(df_nac), hoy=hoy,
            )
            st.session_state["opt_cache_key"] = cache_key

    res = st.session_state["opt_results"]

    # ── Botones de descarga ──────────────────────────────────────────────
    st.markdown("")
    excel_bytes = _export_excel(
        res["df_imp_base"], res["df_imp_opt"], res["df_nac"],
        res["plan_base"], res["plan_opt"], res["log_df"], res["resumen"],
    )
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    dl_c1, dl_c2 = st.columns(2)
    with dl_c1:
        st.download_button(
            "📊 Descargar resultado completo (.xlsx)",
            data=excel_bytes,
            file_name=f"optimizador_compras_{ts}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            help="Excel multi-hoja: OCs optimizadas, planes, movimientos, resumen por proveedor",
        )
    with dl_c2:
        st.download_button(
            "📥 Descargar compra_proyectada optimizada (.csv)",
            data=res["cp_opt_csv"],
            file_name=f"compra_proyectada_opt_{ts}.csv",
            mime="text/csv",
            help=(
                "CSV con el mismo formato que el archivo de entrada. "
                "Las filas IMPORTADO consolidadas tienen la fecha de OC "
                "desplazada según la optimización (delta_meses). "
                "El resto de filas (NACIONAL, orden=0) permanece sin cambios."
            ),
        )

    st.caption(
        f"Archivo: **{uploaded.name}** · "
        f"Fecha ref: **{res['hoy'].strftime('%Y-%m-%d')}** · "
        f"IMPORTADO: {res['n_imp']:,} OCs · NACIONAL: {res['n_nac']:,} OCs"
    )

    # ── Tabs ─────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 Resumen",
        "📋 Planes Base / Optimizado",
        "📦 Detalle OCs",
        "🔀 Movimientos",
    ])

    with tab1:
        _tab_resumen(res["plan_base"], res["plan_opt"])
    with tab2:
        _tab_planes(res["plan_base"], res["plan_opt"])
    with tab3:
        _tab_detalle(res["df_imp_opt"])
    with tab4:
        _tab_movimientos(res["log_df"])
