"""Diagnostico Fcst Manual — analisis SKU por SKU de la demanda futura.

Lee df_proy desde session_state (generado por Proyeccion Stock) y calcula:
- Ventas historicas 2025 y reales/proyectadas 2026 mes a mes
- Stock inicial y final 2026 mes a mes
- Metricas resumen (avg mensual, ratio proy/hist, cobertura, etc.)
- Flags diagnosticos y acciones recomendadas por SKU
"""

from __future__ import annotations

import io
import numpy as np
import pandas as pd
import streamlit as st

from config import COLORS
from utils.export import download_buttons


# ============================================================================
# CONSTANTS
# ============================================================================

_MES_LABELS = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

_MESES_2025  = [f"V25_{m}" for m in _MES_LABELS]
_MESES_V26   = [f"V26_{m}" for m in _MES_LABELS]
_MESES_SI26  = [f"SI26_{m}" for m in _MES_LABELS]
_MESES_SF26  = [f"SF26_{m}" for m in _MES_LABELS]
_MESES_ETA26 = [f"ETA26_{m}" for m in _MES_LABELS]
_MESES_FC26  = [f"FC26_{m}"  for m in _MES_LABELS]
_MESES_MOI26 = [f"MOI26_{m}" for m in _MES_LABELS]

# Flag priority (primary flag = first matching in this list)
_FLAG_PRIORITY = [
    "SIN_PLAN",
    "VENTA_SIN_STOCK",
    "ERROR_DECIMAL",
    "SOBREESTIMACION_ALTA",
    "SOBREESTIMACION_MOD",
    "SOBRESTOCK_ACTUAL",
    "SOBRESTOCK_CIERRE",
    "CAIDA_FUERTE",
    "OVERSTOCK",
    "STOCK_AGOTADO",
    "PROY_CERO_CON_STOCK",
    "DISCONTINUADO",
    "ESTACIONALIDAD_IGNORADA",
    "NUEVO",
    "OK",
]

_FLAG_COLORS = {
    "OK":                    "#43A047",
    "NUEVO":                 "#1976D2",
    "DISCONTINUADO":         "#7B1FA2",
    "STOCK_AGOTADO":         "#E53935",
    "OVERSTOCK":             "#FB8C00",
    "CAIDA_FUERTE":          "#B71C1C",
    "SOBREESTIMACION_MOD":   "#F57F17",
    "SOBREESTIMACION_ALTA":  "#BF360C",
    "ERROR_DECIMAL":         "#546E7A",
    "PROY_CERO_CON_STOCK":   "#6A1B9A",
    "ESTACIONALIDAD_IGNORADA": "#00695C",
    "SIN_PLAN":              "#78909C",
    "VENTA_SIN_STOCK":       "#C62828",
    "SOBRESTOCK_ACTUAL":            "#E65100",
    "SOBRESTOCK_CIERRE":     "#F57F17",
}

_FLAG_EMOJI = {
    "OK":                    "✅ OK",
    "NUEVO":                 "🆕 NUEVO",
    "DISCONTINUADO":         "🚫 DISCONTINUADO",
    "STOCK_AGOTADO":         "🔴 STOCK_AGOTADO",
    "OVERSTOCK":             "🟠 OVERSTOCK",
    "CAIDA_FUERTE":          "⬇️ CAIDA_FUERTE",
    "SOBREESTIMACION_MOD":   "📈 SOBREESTIM_MOD",
    "SOBREESTIMACION_ALTA":  "🚀 SOBREESTIM_ALTA",
    "ERROR_DECIMAL":         "⚠️ ERROR_DECIMAL",
    "PROY_CERO_CON_STOCK":   "📦 PROY_CERO_STOCK",
    "ESTACIONALIDAD_IGNORADA": "📅 ESTACIONALIDAD",
    "SIN_PLAN":              "⬜ SIN_PLAN",
    "VENTA_SIN_STOCK":       "🚨 VENTA_SIN_STOCK",
    "SOBRESTOCK_ACTUAL":            "🔶 SOBRESTOCK ACTUAL",
    "SOBRESTOCK_CIERRE":     "🟡 SOBRESTOCK_CIERRE",
}

_FLAG_ACCIONES = {
    "OK":                    "Sin accion requerida. Forecast coherente con historico.",
    "NUEVO":                 "SKU nuevo sin historial 2025. Validar supuestos del forecast manualmente.",
    "DISCONTINUADO":         "Confirmar si fue dado de baja. Si sigue activo, reactivar forecast.",
    "STOCK_AGOTADO":         "Acelerar reposicion. Revisar demanda insatisfecha para ajustar forecast al alza.",
    "OVERSTOCK":             "Revisar plan de compras. Evaluar descuentos o redistribucion.",
    "CAIDA_FUERTE":          "Verificar estatus del SKU. Si activo, investigar causa de caida.",
    "SOBREESTIMACION_MOD":   "Revisar supuestos de crecimiento. Contrastar con tendencia de linea y area.",
    "SOBREESTIMACION_ALTA":  "Reducir forecast. La proyeccion excede ampliamente el historial.",
    "ERROR_DECIMAL":         "Corregir modelo: ventas near-zero. Revisar parametros o excluir del forecast.",
    "PROY_CERO_CON_STOCK":   "Verificar si el SKU debe tener demanda. Stock disponible pero sin forecast.",
    "ESTACIONALIDAD_IGNORADA": "Revisar distribucion mensual. El patron estacional 2025 no se refleja en 2026.",
    "SIN_PLAN":              "Pasar a FM si no tiene plan de comprar.",
    "VENTA_SIN_STOCK":       "Revisar compra o Borrar Fcst.",
    "SOBRESTOCK_ACTUAL":            "Evaluar descuentos, redistribucion o reduccion de forecast.",
    "SOBRESTOCK_CIERRE":     "Revisar plan de compras para no cerrar el ano con exceso de inventario.",
}


# ============================================================================
# EXCEL EXPORT — tabla horizontal multi-fila
# ============================================================================

def _build_excel_tabla_horizontal(df: pd.DataFrame) -> bytes:
    """Genera un Excel con 6 filas por SKU replicando la tabla visual."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import (
        Font, PatternFill, Alignment, Border, Side, numbers as xl_numbers
    )
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Diagnostico Fcst Manual"

    # ── Helpers de estilo ──────────────────────────────────────────
    def _fill(hex_color: str) -> PatternFill:
        return PatternFill("solid", fgColor=hex_color.lstrip("#"))

    def _border_thin():
        s = Side(style="thin", color="D0D7DE")
        return Border(left=s, right=s, top=s, bottom=s)

    def _border_med_bottom():
        thin = Side(style="thin", color="D0D7DE")
        med  = Side(style="medium", color="9BA8B5")
        return Border(left=thin, right=thin, top=thin, bottom=med)

    FILLS = {
        "header_dark": _fill("1E293B"),
        "header_mid":  _fill("334155"),
        "STOCK FIN":   _fill("DBEAFE"),
        "ETA":         _fill("BFDBFE"),
        "FCST COMPRA": _fill("93C5FD"),
        "VTA UNDS":    _fill("D1FAE5"),
        "MOI":         _fill("EDE9FE"),
        "DIAGN.":      _fill("F8FAFC"),
        # cell value colors
        "green_dk":    _fill("D1FAE5"),
        "green_lt":    _fill("ECFDF5"),
        "yellow":      _fill("FEF9C3"),
        "orange":      _fill("FFF7ED"),
        "red":         _fill("FEE2E2"),
        "blue_lt":     _fill("DBEAFE"),
        "gray_lt":     _fill("F8FAFC"),
    }

    WHITE    = Font(name="Arial", size=9, color="FFFFFF", bold=True)
    GRAY     = Font(name="Arial", size=9, color="94A3B8", bold=True)
    BOLD9    = Font(name="Arial", size=9, bold=True)
    REG9     = Font(name="Arial", size=9)
    CENTER   = Alignment(horizontal="center", vertical="center", wrap_text=False)
    LEFT     = Alignment(horizontal="left",   vertical="center", wrap_text=True)
    RIGHT    = Alignment(horizontal="right",  vertical="center")
    VCENTER  = Alignment(horizontal="left",   vertical="center", wrap_text=False)

    # ── Columnas fijas + meses ─────────────────────────────────────
    ID_COLS  = 5          # SKU | Nombre | Area | Linea | Mix
    TIPO_COL = 6          # Tipo (col F = 6)
    # 2025: cols 7–18, 2026: cols 19–30
    MONTH_2025_START = 7
    MONTH_2026_START = 19
    TOTAL_COLS = TIPO_COL + 24   # 30

    # ── Fila 1: Título ─────────────────────────────────────────────
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=TOTAL_COLS)
    c = ws.cell(1, 1, "Diagnostico Forecast Manual — Tabla SKU por SKU")
    c.font = Font(name="Arial", size=12, bold=True, color="1E293B")
    c.alignment = LEFT
    ws.row_dimensions[1].height = 22

    # ── Fila 2: Grupos de columna ──────────────────────────────────
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ID_COLS)
    ws.merge_cells(start_row=2, start_column=TIPO_COL, end_row=2, end_column=TIPO_COL)
    ws.merge_cells(start_row=2, start_column=MONTH_2025_START, end_row=2, end_column=18)
    ws.merge_cells(start_row=2, start_column=MONTH_2026_START, end_row=2, end_column=30)
    for col, text in [
        (1, "IDENTIFICACION"),
        (TIPO_COL, "TIPO"),
        (MONTH_2025_START, "← 2025 →"),
        (MONTH_2026_START, "← 2026 →"),
    ]:
        c = ws.cell(2, col, text)
        c.font = WHITE
        c.fill = FILLS["header_dark"]
        c.alignment = CENTER
    ws.row_dimensions[2].height = 16

    # ── Fila 3: Cabeceras de columna ───────────────────────────────
    headers = ["SKU", "Nombre", "Area", "Linea", "Mix", "Tipo"] + _MES_LABELS + _MES_LABELS
    for col, h in enumerate(headers, 1):
        c = ws.cell(3, col, h)
        c.font = GRAY
        c.fill = FILLS["header_mid"]
        c.alignment = CENTER
    ws.row_dimensions[3].height = 14

    # ── Anchos de columna ──────────────────────────────────────────
    ws.column_dimensions["A"].width = 14   # SKU
    ws.column_dimensions["B"].width = 22   # Nombre
    ws.column_dimensions["C"].width = 9    # Area
    ws.column_dimensions["D"].width = 12   # Linea
    ws.column_dimensions["E"].width = 7    # Mix
    ws.column_dimensions["F"].width = 12   # Tipo
    for col in range(MONTH_2025_START, TOTAL_COLS + 1):
        ws.column_dimensions[get_column_letter(col)].width = 6.5

    # ── Helpers de color de celda ──────────────────────────────────
    def _fill_stock(v, avg_d):
        try:
            f, a = float(v), float(avg_d)
            if f == 0:    return FILLS["red"]
            if a <= 0:    return None
            m = f / a
            if m > 12:    return FILLS["yellow"]
            if m >= 3:    return FILLS["green_lt"]
            return FILLS["orange"]
        except Exception:
            return None

    def _fill_venta(v, avg):
        try:
            f, a = float(v), float(avg)
            if f == 0 or a <= 0: return None
            r = f / a
            if r >= 1.5:  return FILLS["green_dk"]
            if r >= 0.8:  return FILLS["green_lt"]
            if r >= 0.4:  return FILLS["yellow"]
            return FILLS["red"]
        except Exception:
            return None

    def _fill_moi(v):
        try:
            f = float(v)
            if f <= 0 or f != f: return None  # nan/zero
            if f >= 99:   return FILLS["yellow"]
            if f > 7:     return FILLS["red"]
            if f > 4:     return FILLS["yellow"]
            if f >= 2:    return FILLS["green_lt"]
            return FILLS["orange"]
        except Exception:
            return None

    def _num(v):
        """Convierte a float, None si es 0 o nan."""
        try:
            f = float(v)
            return None if (f == 0 or f != f) else round(f, 1)
        except Exception:
            return None

    # ── Filas de datos ─────────────────────────────────────────────
    data_row = 4
    ROWS_PER_SKU = 6

    for _, row in df.iterrows():
        sku   = str(row.get("SKU_PRODUCTO", ""))
        nom   = str(row.get("SKU_NOM_PRODUCTO", ""))
        area  = str(row.get("AREA", ""))
        linea = str(row.get("LINEA", ""))
        mix   = str(row.get("MIX_OFICIAL", ""))
        flag  = str(row.get("FLAG_PRINCIPAL", "OK"))
        diag  = str(row.get("DIAGNOSTICO_DETALLADO", ""))
        accion = str(row.get("ACCION_RECOMENDADA", ""))

        avg25 = float(row.get("AVG_MENS_2025", 0) or 0)
        avg26 = float(row.get("AVG_PROY_JUN_DIC", 0) or 0)
        avg_d = avg26 if avg26 > 0 else avg25

        # Valores de cada fila
        v25  = [row.get(c, 0) for c in _MESES_2025]
        v26  = [row.get(c, 0) for c in _MESES_V26]
        sf26 = [row.get(c, 0) for c in _MESES_SF26]
        eta26= [row.get(c, 0) for c in _MESES_ETA26]
        fc26 = [row.get(c, 0) for c in _MESES_FC26]
        moi26= [row.get(c)    for c in _MESES_MOI26]

        tipo_rows = [
            ("STOCK FIN",   [None]*12 + sf26,   None),
            ("VTA UNDS",    v25 + v26,            None),
            ("ETA",         [None]*12 + eta26,   None),
            ("FCST COMPRA", [None]*12 + fc26,    None),
            ("MOI",         [None]*12 + moi26,   None),
        ]

        r_start = data_row
        r_end   = data_row + ROWS_PER_SKU - 1
        flag_hex = _FLAG_COLORS.get(flag, "#94A3B8").lstrip("#")
        diag_text = f"[{flag}]  {diag}  |  Accion: {accion}"

        id_vals = [sku, nom, area, linea, mix]

        # 5 filas de métricas — sin celdas combinadas, ID se repite en cada fila
        for fi, (tipo, vals, _) in enumerate(tipo_rows):
            r = r_start + fi
            row_fill = FILLS.get(tipo, FILLS["gray_lt"])
            is_last = (fi == len(tipo_rows) - 1)
            border_fn = _border_med_bottom if is_last else _border_thin

            # Columnas de identificacion (repetidas en cada fila)
            for col, val in enumerate(id_vals, 1):
                c = ws.cell(r, col, val)
                c.font = BOLD9 if col == 1 else REG9
                c.alignment = LEFT
                c.border = border_fn()

            # Columna Tipo
            c = ws.cell(r, TIPO_COL, tipo)
            c.font = BOLD9
            c.fill = row_fill
            c.alignment = CENTER
            c.border = border_fn()

            # Meses 2025 (cols 7-18)
            for mi, val in enumerate(vals[:12]):
                col = MONTH_2025_START + mi
                num = _num(val)
                c = ws.cell(r, col, num)
                c.font = REG9
                c.alignment = RIGHT
                c.border = border_fn()
                if num is not None:
                    if tipo == "VTA UNDS":
                        cf = _fill_venta(num, avg25)
                        if cf: c.fill = cf
                    elif tipo in ("ETA", "FCST COMPRA"):
                        c.fill = FILLS["blue_lt"]

            # Meses 2026 (cols 19-30)
            for mi, val in enumerate(vals[12:]):
                col = MONTH_2026_START + mi
                num = _num(val)
                c = ws.cell(r, col, num)
                c.font = REG9
                c.alignment = RIGHT
                c.border = border_fn()
                if num is not None:
                    if tipo == "STOCK FIN":
                        cf = _fill_stock(num, avg_d)
                        if cf: c.fill = cf
                    elif tipo == "VTA UNDS":
                        cf = _fill_venta(num, avg26)
                        if cf: c.fill = cf
                    elif tipo in ("ETA", "FCST COMPRA"):
                        c.fill = FILLS["blue_lt"]
                    elif tipo == "MOI":
                        cf = _fill_moi(num)
                        if cf: c.fill = cf

        # Fila DIAGN. (última del grupo) — sin merge, ID repetido, texto en col TIPO
        r_diag = r_end

        # ID repetido
        for col, val in enumerate(id_vals, 1):
            c = ws.cell(r_diag, col, val)
            c.font = BOLD9 if col == 1 else REG9
            c.alignment = LEFT
            c.border = _border_med_bottom()

        # Tipo
        c = ws.cell(r_diag, TIPO_COL, "DIAGN.")
        c.font = Font(name="Arial", size=9, bold=True, color=flag_hex)
        c.fill = FILLS["DIAGN."]
        c.alignment = CENTER
        c.border = _border_med_bottom()

        # Todo el texto en UNA sola celda (col 7), sin wrap
        full_diag = f"[{flag}]  {diag}  |  {accion}"
        c = ws.cell(r_diag, MONTH_2025_START, full_diag)
        c.font = Font(name="Arial", size=8, color="374151")
        c.fill = FILLS["DIAGN."]
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=False)
        c.border = _border_med_bottom()

        # Celdas restantes vacías con borde (cols 8-30)
        for col in range(MONTH_2025_START + 1, TOTAL_COLS + 1):
            c = ws.cell(r_diag, col, None)
            c.fill = FILLS["DIAGN."]
            c.border = _border_med_bottom()

        ws.row_dimensions[r_diag].height = 15
        data_row += ROWS_PER_SKU

    # Freeze panes
    ws.freeze_panes = "G4"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


# ============================================================================
# COMPUTATION ENGINE
# ============================================================================

def _build_diagnostics(df_proy: pd.DataFrame) -> pd.DataFrame:
    """Transform df_proy (long format) into wide SKU-level diagnostic table."""
    df = df_proy.copy()

    # Normalize PERIODO to datetime
    df["PERIODO"] = pd.to_datetime(df["PERIODO"], errors="coerce")
    df = df.dropna(subset=["PERIODO"])
    df["_YEAR"] = df["PERIODO"].dt.year
    df["_MONTH"] = df["PERIODO"].dt.month

    # Compute total demand per row
    demand_cols = [c for c in ["DEMANDA_TOTAL", "DEMANDA_SIM_TIENDA", "DEMANDA_SIM_ETAIL", "DEMANDA_SIM_MAYOR"] if c in df.columns]
    if "DEMANDA_TOTAL" in df.columns:
        df["_DEMANDA"] = pd.to_numeric(df["DEMANDA_TOTAL"], errors="coerce").fillna(0)
    elif len(demand_cols) > 1:
        df["_DEMANDA"] = sum(pd.to_numeric(df[c], errors="coerce").fillna(0) for c in demand_cols if c != "DEMANDA_TOTAL")
    else:
        df["_DEMANDA"] = 0.0

    # Stock columns
    df["_STK_INI"] = pd.to_numeric(df.get("STOCK_INICIAL_TOTAL", 0), errors="coerce").fillna(0)
    df["_STK_FIN"] = pd.to_numeric(df.get("STOCK_FINAL_TOTAL", 0), errors="coerce").fillna(0)

    # ---------- Pivot monthly demand 2025 (historical rows) ----------
    hist_2025 = df[(df["_YEAR"] == 2025) & (df["TIPO_DATO"].isin(["HISTORICO", "REAL+FC"]))]
    piv_v25 = hist_2025.pivot_table(
        index="SKU_PRODUCTO", columns="_MONTH", values="_DEMANDA", aggfunc="sum", fill_value=0
    )
    for m in range(1, 13):
        if m not in piv_v25.columns:
            piv_v25[m] = 0
    piv_v25 = piv_v25[[m for m in range(1, 13)]]
    piv_v25.columns = _MESES_2025

    # ---------- Pivot monthly demand 2026 (real + projected) ----------
    data_2026 = df[df["_YEAR"] == 2026]
    piv_v26 = data_2026.pivot_table(
        index="SKU_PRODUCTO", columns="_MONTH", values="_DEMANDA", aggfunc="sum", fill_value=0
    )
    for m in range(1, 13):
        if m not in piv_v26.columns:
            piv_v26[m] = 0
    piv_v26 = piv_v26[[m for m in range(1, 13)]]
    piv_v26.columns = _MESES_V26

    # ---------- Pivot stock 2026 ----------
    piv_si26 = data_2026.pivot_table(
        index="SKU_PRODUCTO", columns="_MONTH", values="_STK_INI", aggfunc="sum", fill_value=0
    )
    piv_sf26 = data_2026.pivot_table(
        index="SKU_PRODUCTO", columns="_MONTH", values="_STK_FIN", aggfunc="sum", fill_value=0
    )
    for m in range(1, 13):
        if m not in piv_si26.columns:
            piv_si26[m] = 0
        if m not in piv_sf26.columns:
            piv_sf26[m] = 0
    piv_si26 = piv_si26[[m for m in range(1, 13)]]
    piv_sf26 = piv_sf26[[m for m in range(1, 13)]]
    piv_si26.columns = _MESES_SI26
    piv_sf26.columns = _MESES_SF26

    # ---------- Pivot ETA 2026 (inbound transito) ----------
    if "ETA" in data_2026.columns:
        _data_eta = data_2026.copy()
        _data_eta["_ETA"] = pd.to_numeric(_data_eta["ETA"], errors="coerce").fillna(0)
        piv_eta26 = _data_eta.pivot_table(
            index="SKU_PRODUCTO", columns="_MONTH", values="_ETA", aggfunc="sum", fill_value=0
        )
    else:
        piv_eta26 = pd.DataFrame()
    for m in range(1, 13):
        if m not in piv_eta26.columns:
            piv_eta26[m] = 0
    if not piv_eta26.empty:
        piv_eta26 = piv_eta26[[m for m in range(1, 13)]]
        piv_eta26.columns = _MESES_ETA26

    # ---------- Pivot FORECAST_COMPRA 2026 (ordenes de compra planeadas) ----------
    if "FORECAST_COMPRA" in data_2026.columns:
        _data_fc = data_2026.copy()
        _data_fc["_FC"] = pd.to_numeric(_data_fc["FORECAST_COMPRA"], errors="coerce").fillna(0)
        piv_fc26 = _data_fc.pivot_table(
            index="SKU_PRODUCTO", columns="_MONTH", values="_FC", aggfunc="sum", fill_value=0
        )
    else:
        piv_fc26 = pd.DataFrame()
    for m in range(1, 13):
        if m not in piv_fc26.columns:
            piv_fc26[m] = 0
    if not piv_fc26.empty:
        piv_fc26 = piv_fc26[[m for m in range(1, 13)]]
        piv_fc26.columns = _MESES_FC26

    # ---------- SKU metadata ----------
    id_cols = ["SKU_PRODUCTO"]
    meta_cols = [c for c in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL"] if c in df.columns]
    meta = df[id_cols + meta_cols].drop_duplicates("SKU_PRODUCTO").set_index("SKU_PRODUCTO")

    # ---------- Merge all pivots ----------
    all_skus = meta.index.union(piv_v25.index).union(piv_v26.index)
    result = pd.DataFrame(index=all_skus)
    result = result.join(meta, how="left")
    result = result.join(piv_v25, how="left").fillna({c: 0 for c in _MESES_2025})
    result = result.join(piv_v26, how="left").fillna({c: 0 for c in _MESES_V26})
    result = result.join(piv_si26, how="left").fillna({c: 0 for c in _MESES_SI26})
    result = result.join(piv_sf26, how="left").fillna({c: 0 for c in _MESES_SF26})
    if not piv_eta26.empty:
        result = result.join(piv_eta26, how="left").fillna({c: 0 for c in _MESES_ETA26})
    else:
        for c in _MESES_ETA26:
            result[c] = 0
    if not piv_fc26.empty:
        result = result.join(piv_fc26, how="left").fillna({c: 0 for c in _MESES_FC26})
    else:
        for c in _MESES_FC26:
            result[c] = 0
    result = result.reset_index().rename(columns={"index": "SKU_PRODUCTO"})

    # ---------- MOI 2026: Stock Final / Avg VTA UNDS del año ----------
    # Avg VTA 2025 (12 meses) y Avg VTA 2026 (12 meses)
    _avg_v25 = result[_MESES_2025].sum(axis=1) / 12
    _avg_v26 = result[_MESES_V26].sum(axis=1) / 12
    # Para 2026: MOI_mes = SF26_mes / avg_v26
    for col_moi, col_sf in zip(_MESES_MOI26, _MESES_SF26):
        with np.errstate(divide="ignore", invalid="ignore"):
            result[col_moi] = np.where(
                _avg_v26 > 0,
                result[col_sf] / _avg_v26,
                np.where(result[col_sf] > 0, 99.0, np.nan),
            )

    # ---------- Summary metrics ----------
    # Has historical 2025
    skus_hist = set(hist_2025["SKU_PRODUCTO"].unique())
    result["HIST_2025"] = result["SKU_PRODUCTO"].apply(lambda x: "Si" if x in skus_hist else "No")

    result["VTA_TOTAL_2025"] = result[_MESES_2025].sum(axis=1)

    # Real 2026 = months Jan-May (HISTORICO/REAL+FC in 2026)
    real_2026 = df[(df["_YEAR"] == 2026) & (df["TIPO_DATO"].isin(["HISTORICO", "REAL+FC"]))]
    real_months = sorted(real_2026["_MONTH"].unique()) if not real_2026.empty else []
    # Months 1-5 considered "real" (Jan-May), rest projected
    _real_month_labels = [_MESES_V26[m-1] for m in range(1, 6) if m in real_months or m < 6]
    _proy_month_labels = [_MESES_V26[m-1] for m in range(6, 13)]

    result["VTA_ENE_MAY_2026"] = result[[c for c in _MESES_V26[:5] if c in result.columns]].sum(axis=1)
    result["VTA_JUN_DIC_2026"] = result[[c for c in _MESES_V26[5:] if c in result.columns]].sum(axis=1)

    result["AVG_MENS_2025"] = result["VTA_TOTAL_2025"] / 12
    _n_proy = 7  # Jun-Dic = 7 months
    result["AVG_PROY_JUN_DIC"] = result["VTA_JUN_DIC_2026"] / _n_proy

    result["RATIO_PROY_HIST"] = np.where(
        result["AVG_MENS_2025"] > 0,
        result["AVG_PROY_JUN_DIC"] / result["AVG_MENS_2025"],
        np.nan,
    )

    vta_total_2026 = result[_MESES_V26].sum(axis=1)
    result["CREC_YOY"] = np.where(
        result["VTA_TOTAL_2025"] > 0,
        (vta_total_2026 / result["VTA_TOTAL_2025"] - 1) * 100,
        np.nan,
    )

    # Stock May 2026 (col index 4 = May)
    result["STOCK_MAY_2026"] = result["SF26_May"].fillna(0)
    result["STOCK_DIC_FINAL"] = result["SF26_Dic"].fillna(0)

    result["MESES_COB_DIC"] = np.where(
        result["AVG_PROY_JUN_DIC"] > 0,
        result["STOCK_DIC_FINAL"] / result["AVG_PROY_JUN_DIC"],
        np.where(result["STOCK_DIC_FINAL"] > 0, 99.0, 0.0),
    )

    # Meses hasta stock=0 — using May stock and avg monthly demand Jun-Dec
    result["MESES_STK0_EM"] = np.where(
        result["AVG_PROY_JUN_DIC"] > 0,
        result["STOCK_MAY_2026"] / result["AVG_PROY_JUN_DIC"],
        np.where(result["STOCK_MAY_2026"] > 0, 99.0, 0.0),
    )

    # ---------- Diagnostic flags ----------
    result["_FLAGS"] = [[] for _ in range(len(result))]

    # Pre-calcular totales para reusar en varios flags
    _proy_v26   = result[_MESES_V26[5:]].sum(axis=1)    # demanda Jun-Dic 2026
    _proy_sf26  = result[_MESES_SF26[5:]].sum(axis=1)   # stock fin Jun-Dic 2026
    _proy_eta26 = result[_MESES_ETA26[5:]].sum(axis=1)  # ETA Jun-Dic 2026
    _all_v26    = result[_MESES_V26].sum(axis=1)         # demanda todo 2026
    _all_sf26   = result[_MESES_SF26].sum(axis=1)        # stock fin todo 2026
    _all_eta26  = result[_MESES_ETA26].sum(axis=1)       # ETA todo 2026

    # SIN_PLAN: sin demanda, sin stock y sin ETA en todo 2026
    mask_sin_plan = (_all_v26 < 0.01) & (_all_sf26 < 0.01) & (_all_eta26 < 0.01)
    result.loc[mask_sin_plan, "_FLAGS"] = result.loc[mask_sin_plan, "_FLAGS"].apply(lambda f: f + ["SIN_PLAN"])

    # VENTA_SIN_STOCK: tiene demanda proyectada (Jun-Dic) pero sin stock final ni ETA
    mask_venta_sin_stock = (
        (_proy_v26 >= 0.5)          # hay demanda real proyectada
        & (_proy_sf26 < 0.01)       # sin stock final en proyeccion
        & (_proy_eta26 < 0.01)      # sin ETA en proyeccion
        & ~mask_sin_plan
    )
    result.loc[mask_venta_sin_stock, "_FLAGS"] = result.loc[mask_venta_sin_stock, "_FLAGS"].apply(lambda f: f + ["VENTA_SIN_STOCK"])

    # ERROR_DECIMAL: algún mes proyectado tiene 0 < demanda < 0.01
    # Solo aplica si la demanda total proyectada es tiny (< 0.5 und) — descarta SKUs con ventas reales
    def _has_error_decimal(row):
        vals = [row[c] for c in _MESES_V26[5:]]
        has_near_zero = any(0 < v < 0.01 for v in vals)
        if not has_near_zero:
            return False
        # No marcar ERROR_DECIMAL si hay ventas reales significativas en otros meses
        return sum(vals) < 0.5

    mask_error = result.apply(_has_error_decimal, axis=1) & ~mask_sin_plan & ~mask_venta_sin_stock
    result.loc[mask_error, "_FLAGS"] = result.loc[mask_error, "_FLAGS"].apply(lambda f: f + ["ERROR_DECIMAL"])

    # NUEVO: no historical 2025
    mask_nuevo = result["HIST_2025"] == "No"
    result.loc[mask_nuevo, "_FLAGS"] = result.loc[mask_nuevo, "_FLAGS"].apply(lambda f: f + ["NUEVO"])

    # DISCONTINUADO: had 2025 sales but all 2026 projected = 0
    mask_disc = (result["VTA_TOTAL_2025"] > 0) & (_all_v26 == 0)
    result.loc[mask_disc, "_FLAGS"] = result.loc[mask_disc, "_FLAGS"].apply(lambda f: f + ["DISCONTINUADO"])

    # STOCK_AGOTADO: stock final = 0 in >= 2 real months (Jan-May 2026)
    sf_real_cols = _MESES_SF26[:5]
    n_meses_agotado = (result[sf_real_cols] == 0).sum(axis=1)
    demanda_real = result[_MESES_V26[:5]].sum(axis=1)
    mask_agotado = (n_meses_agotado >= 2) & (demanda_real > 0)
    result.loc[mask_agotado, "_FLAGS"] = result.loc[mask_agotado, "_FLAGS"].apply(lambda f: f + ["STOCK_AGOTADO"])

    # SOBREESTIMACION_ALTA: ratio > 5x
    mask_sobre_alta = result["RATIO_PROY_HIST"] > 5
    result.loc[mask_sobre_alta, "_FLAGS"] = result.loc[mask_sobre_alta, "_FLAGS"].apply(lambda f: f + ["SOBREESTIMACION_ALTA"])

    # SOBREESTIMACION_MOD: ratio 2.5x-5x
    mask_sobre_mod = (result["RATIO_PROY_HIST"] >= 2.5) & (result["RATIO_PROY_HIST"] <= 5)
    result.loc[mask_sobre_mod, "_FLAGS"] = result.loc[mask_sobre_mod, "_FLAGS"].apply(lambda f: f + ["SOBREESTIMACION_MOD"])

    # CAIDA_FUERTE: 2026 total < 25% of 2025 total (not discontinuado)
    mask_caida = (
        (result["VTA_TOTAL_2025"] > 0)
        & (_all_v26 < result["VTA_TOTAL_2025"] * 0.25)
        & ~mask_disc
    )
    result.loc[mask_caida, "_FLAGS"] = result.loc[mask_caida, "_FLAGS"].apply(lambda f: f + ["CAIDA_FUERTE"])

    # OVERSTOCK: coverage > 12 months AND 2026 sales fell > 50%
    mask_overstock = (
        (result["MESES_COB_DIC"] > 12)
        & (result["VTA_TOTAL_2025"] > 0)
        & (_all_v26 < result["VTA_TOTAL_2025"] * 0.5)
    )
    result.loc[mask_overstock, "_FLAGS"] = result.loc[mask_overstock, "_FLAGS"].apply(lambda f: f + ["OVERSTOCK"])

    # PROY_CERO_CON_STOCK: stock > 10 but projected demand = 0 in >= 3 projected months
    proy_zero_months = (result[_MESES_V26[5:]] == 0).sum(axis=1)
    mask_proy_cero = (result["STOCK_MAY_2026"] > 10) & (proy_zero_months >= 3)
    result.loc[mask_proy_cero, "_FLAGS"] = result.loc[mask_proy_cero, "_FLAGS"].apply(lambda f: f + ["PROY_CERO_CON_STOCK"])

    # ESTACIONALIDAD_IGNORADA: 2025 had strong seasonal variation but 2026 projection is flat
    # Detect via coefficient of variation in 2025 vs flatness in 2026 Jun-Dec
    v25_arr = result[_MESES_2025].values
    v25_std = v25_arr.std(axis=1)
    v25_mean = v25_arr.mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        v25_cv = np.where(v25_mean > 0, v25_std / np.where(v25_mean > 0, v25_mean, 1), 0)

    v26_proy_arr = result[_MESES_V26[5:]].values
    v26_std = v26_proy_arr.std(axis=1)
    v26_mean = v26_proy_arr.mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        v26_cv = np.where(v26_mean > 0, v26_std / np.where(v26_mean > 0, v26_mean, 1), 0)

    # Strong seasonality in 2025 (CV > 0.6) but flat projection (CV < 0.15)
    mask_estac = (v25_cv > 0.6) & (v26_cv < 0.15) & (result["VTA_TOTAL_2025"] > 0)
    result.loc[mask_estac, "_FLAGS"] = result.loc[mask_estac, "_FLAGS"].apply(lambda f: f + ["ESTACIONALIDAD_IGNORADA"])

    # SOBRESTOCK_ACTUAL: MOI mes actual > 7 + VTA en 2026 + stock en TODOS los meses de 2026
    from datetime import datetime as _dt
    _cur_month = _dt.now().month  # mes actual (1-12)
    _cur_moi_col = _MESES_MOI26[_cur_month - 1]  # columna MOI del mes actual
    _moi_actual = pd.to_numeric(result[_cur_moi_col], errors="coerce").fillna(0)
    _sf26_min = result[_MESES_SF26].apply(pd.to_numeric, errors="coerce").fillna(0).min(axis=1)
    mask_sobrestock = (
        (_moi_actual > 7)          # MOI actual > 7
        & (_all_v26 >= 0.5)        # tiene demanda proyectada en 2026
        & (_sf26_min > 0)          # stock > 0 en TODOS los 12 meses de 2026
    )
    result.loc[mask_sobrestock, "_FLAGS"] = result.loc[mask_sobrestock, "_FLAGS"].apply(lambda f: f + ["SOBRESTOCK_ACTUAL"])

    # SOBRESTOCK_CIERRE: MOI de Diciembre 2026 > 6
    _moi_dic = pd.to_numeric(result["MOI26_Dic"], errors="coerce").fillna(0)
    mask_sobrestock_cierre = _moi_dic > 6
    result.loc[mask_sobrestock_cierre, "_FLAGS"] = result.loc[mask_sobrestock_cierre, "_FLAGS"].apply(lambda f: f + ["SOBRESTOCK_CIERRE"])

    # OK: no other flags
    mask_ok = result["_FLAGS"].apply(len) == 0
    result.loc[mask_ok, "_FLAGS"] = result.loc[mask_ok, "_FLAGS"].apply(lambda f: f + ["OK"])

    # Derive FLAG_PRINCIPAL (highest priority flag present)
    def _primary_flag(flags):
        for f in _FLAG_PRIORITY:
            if f in flags:
                return f
        return "OK"

    result["FLAG_PRINCIPAL"] = result["_FLAGS"].apply(_primary_flag)
    result["TODOS_LOS_FLAGS"] = result["_FLAGS"].apply(lambda f: " | ".join(f))

    # Diagnostico detallado
    def _build_diagnostico(row):
        flags = row["_FLAGS"]
        parts = []
        if "SIN_PLAN" in flags:
            parts.append("Sin venta, stock ni ETA en todo 2026 — SKU inactivo sin plan de compra")
        if "VENTA_SIN_STOCK" in flags:
            v = _proy_v26[row.name] if hasattr(row, "name") else 0
            parts.append(f"Demanda proyectada Jun-Dic = {v:.1f} uds sin stock ni ETA — brecha de abastecimiento")
        if "ERROR_DECIMAL" in flags:
            parts.append("Ventas proyectadas < 0.01 uds — error numerico del modelo")
        if "NUEVO" in flags:
            parts.append("SKU sin historial 2025")
        if "DISCONTINUADO" in flags:
            parts.append("Tuvo ventas en 2025 pero 2026 = 0 en todos los meses")
        if "STOCK_AGOTADO" in flags:
            n = (pd.Series([row[c] for c in sf_real_cols]) == 0).sum()
            parts.append(f"Stock=0 en {n} meses del periodo real (Ene-May)")
        if "SOBREESTIMACION_ALTA" in flags:
            r = row["RATIO_PROY_HIST"]
            parts.append(f"Proy Jun-Dic = {r:.1f}x el promedio mensual historico 2025")
        if "SOBREESTIMACION_MOD" in flags:
            r = row["RATIO_PROY_HIST"]
            parts.append(f"Proy Jun-Dic = {r:.1f}x el promedio mensual historico 2025")
        if "CAIDA_FUERTE" in flags:
            cr = row["CREC_YOY"]
            parts.append(f"Ventas 2026 caen {abs(cr):.0f}% vs 2025")
        if "OVERSTOCK" in flags:
            cr = row["CREC_YOY"]
            mc = row["MESES_COB_DIC"]
            parts.append(f"Ventas 2026 caen {abs(cr):.0f}% vs 2025 / Stock final Dic = {mc:.0f} meses de cobertura")
        if "PROY_CERO_CON_STOCK" in flags:
            n_zero = proy_zero_months[row.name] if hasattr(row, "name") else 0
            parts.append(f"Stock>{int(row['STOCK_MAY_2026'])} uds pero venta=0 en {n_zero} meses proyectados")
        if "SOBRESTOCK_ACTUAL" in flags:
            moi_v = _moi_actual[row.name] if hasattr(row, "name") else 0
            parts.append(f"MOI mes actual = {moi_v:.1f} meses (umbral: 7)")
        if "SOBRESTOCK_CIERRE" in flags:
            moi_v = _moi_dic[row.name] if hasattr(row, "name") else 0
            parts.append(f"MOI Dic 2026 = {moi_v:.1f} meses (umbral: 6)")
        if "ESTACIONALIDAD_IGNORADA" in flags:
            parts.append("Patron estacional 2025 no se refleja en la proyeccion 2026")
        if "OK" in flags:
            parts.append("Forecast coherente con el historico")
        return " / ".join(parts)

    result["DIAGNOSTICO_DETALLADO"] = result.apply(_build_diagnostico, axis=1)
    result["ACCION_RECOMENDADA"] = result["FLAG_PRINCIPAL"].map(_FLAG_ACCIONES).fillna("")

    # Round numeric columns
    for c in _MESES_2025 + _MESES_V26 + _MESES_SI26 + _MESES_SF26:
        if c in result.columns:
            result[c] = result[c].round(1)

    for c in ["VTA_TOTAL_2025", "VTA_ENE_MAY_2026", "VTA_JUN_DIC_2026",
              "AVG_MENS_2025", "AVG_PROY_JUN_DIC", "STOCK_MAY_2026", "STOCK_DIC_FINAL"]:
        if c in result.columns:
            result[c] = result[c].round(1)

    for c in ["RATIO_PROY_HIST", "CREC_YOY", "MESES_COB_DIC", "MESES_STK0_EM"]:
        if c in result.columns:
            result[c] = result[c].round(2)

    return result


# ============================================================================
# RENDER
# ============================================================================

def render_diagnostico_fcst_manual(conn):  # noqa: ARG001
    from utils.ui_components import page_header

    st.html(
        page_header(
            "Diagnostico Forecast Manual",
            "Analisis SKU por SKU de la demanda futura proyectada",
        )
    )

    # ── Verificar si hay proyeccion en session_state ──
    df_proy_raw = st.session_state.get("df_proy")
    if df_proy_raw is None or (isinstance(df_proy_raw, pd.DataFrame) and df_proy_raw.empty):
        st.warning(
            "No hay datos de Proyeccion Stock disponibles. "
            "Ve a **Proyeccion Stock** y ejecuta la simulacion primero."
        )
        return

    # ── Procesar datos ──
    with st.spinner("Calculando diagnosticos por SKU..."):
        df_diag = _build_diagnostics(df_proy_raw)

    if df_diag.empty:
        st.error("No se pudieron calcular diagnosticos. Verifica los datos de proyeccion.")
        return

    n_total = len(df_diag)
    n_ok = (df_diag["FLAG_PRINCIPAL"] == "OK").sum()
    n_alertas = n_total - n_ok

    # ============================================================
    # KPIs superiores
    # ============================================================
    st.html(
        f"""
        <div style="display:flex; gap:1rem; flex-wrap:wrap; margin-bottom:1rem;">
          <div style="flex:1; min-width:140px; background:#f8fafc; border:1px solid #e2e8f0;
                      border-radius:12px; padding:1rem; text-align:center;">
            <div style="font-size:1.8rem; font-weight:800; color:#1e293b;">{n_total:,}</div>
            <div style="font-size:0.78rem; color:#64748b; margin-top:4px;">SKUs Analizados</div>
          </div>
          <div style="flex:1; min-width:140px; background:#f0fdf4; border:1px solid #bbf7d0;
                      border-radius:12px; padding:1rem; text-align:center;">
            <div style="font-size:1.8rem; font-weight:800; color:#16a34a;">{n_ok:,}</div>
            <div style="font-size:0.78rem; color:#166534; margin-top:4px;">SKUs OK</div>
          </div>
          <div style="flex:1; min-width:140px; background:#fef2f2; border:1px solid #fecaca;
                      border-radius:12px; padding:1rem; text-align:center;">
            <div style="font-size:1.8rem; font-weight:800; color:#dc2626;">{n_alertas:,}</div>
            <div style="font-size:0.78rem; color:#991b1b; margin-top:4px;">SKUs con Alertas</div>
          </div>
        </div>
        """
    )

    # Badges por flag
    flag_counts = df_diag["FLAG_PRINCIPAL"].value_counts()
    badges_html = '<div style="display:flex; gap:0.5rem; flex-wrap:wrap; margin-bottom:1rem;">'
    for flag in _FLAG_PRIORITY:
        count = flag_counts.get(flag, 0)
        if count == 0:
            continue
        color = _FLAG_COLORS.get(flag, "#94a3b8")
        label = _FLAG_EMOJI.get(flag, flag)
        badges_html += (
            f'<span style="background:{color}22; color:{color}; border:1px solid {color}88; '
            f'padding:0.25rem 0.65rem; border-radius:20px; font-size:0.75rem; font-weight:600; '
            f'white-space:nowrap;">{label} ({count})</span>'
        )
    badges_html += "</div>"
    st.html(badges_html)

    # ============================================================
    # FILTROS EN CASCADA
    # Cada filtro muestra solo opciones presentes en el subconjunto
    # que resulta de aplicar los filtros anteriores.
    # ============================================================

    # Leer valores ya seleccionados (para poder calcular opciones disponibles)
    sel_areas  = st.session_state.get("diag_areas",  [])
    sel_mix    = st.session_state.get("diag_mix",    [])
    sel_flags  = st.session_state.get("diag_flags",  [])
    sel_hist   = st.session_state.get("diag_hist",   [])

    # Aplicar filtros en orden para calcular opciones disponibles en cascada
    _df_after_area = df_diag[df_diag["AREA"].isin(sel_areas)] if sel_areas else df_diag
    _df_after_mix  = _df_after_area[_df_after_area["MIX_OFICIAL"].isin(sel_mix)] if sel_mix else _df_after_area
    _df_after_flag = _df_after_mix[_df_after_mix["FLAG_PRINCIPAL"].isin(sel_flags)] if sel_flags else _df_after_mix
    _df_after_hist = _df_after_flag[_df_after_flag["HIST_2025"].isin(sel_hist)] if sel_hist else _df_after_flag

    with st.expander("Filtros", expanded=True):
        fcol1, fcol2, fcol3 = st.columns([2, 2, 2])

        with fcol1:
            area_opts = sorted(df_diag["AREA"].dropna().unique().tolist()) if "AREA" in df_diag.columns else []
            sel_areas = st.multiselect("Area", area_opts, default=sel_areas, key="diag_areas")

        with fcol2:
            # Opciones de Mix = las presentes tras filtrar por Area
            _df_for_mix = df_diag[df_diag["AREA"].isin(sel_areas)] if sel_areas else df_diag
            mix_opts = sorted(_df_for_mix["MIX_OFICIAL"].dropna().unique().tolist()) if "MIX_OFICIAL" in _df_for_mix.columns else []
            # Limpiar selección si ya no está disponible
            sel_mix = [v for v in sel_mix if v in mix_opts]
            sel_mix = st.multiselect("Mix", mix_opts, default=sel_mix, key="diag_mix")

        with fcol3:
            # Opciones de Flag = las presentes tras filtrar por Area + Mix
            _df_for_flag = _df_for_mix[_df_for_mix["MIX_OFICIAL"].isin(sel_mix)] if sel_mix else _df_for_mix
            flag_opts = [f for f in _FLAG_PRIORITY if f in _df_for_flag["FLAG_PRINCIPAL"].values]
            sel_flags = [v for v in sel_flags if v in flag_opts]
            sel_flags = st.multiselect("Flag Principal", flag_opts, default=sel_flags, key="diag_flags")

        fcol4, fcol5 = st.columns([2, 4])
        with fcol4:
            # Opciones de Hist = las presentes tras filtrar por Area + Mix + Flag
            _df_for_hist = _df_for_flag[_df_for_flag["FLAG_PRINCIPAL"].isin(sel_flags)] if sel_flags else _df_for_flag
            hist_opts = sorted(_df_for_hist["HIST_2025"].dropna().unique().tolist()) if "HIST_2025" in _df_for_hist.columns else ["Si", "No"]
            sel_hist = [v for v in sel_hist if v in hist_opts]
            sel_hist = st.multiselect("Tiene Historial 2025", hist_opts, default=sel_hist, key="diag_hist")

        with fcol5:
            search_sku = st.text_input("Buscar SKU / Nombre", key="diag_search_sku", placeholder="codigo o descripcion...")

    # Aplicar todos los filtros al dataset final
    df_show = df_diag.copy()
    if sel_areas:
        df_show = df_show[df_show["AREA"].isin(sel_areas)]
    if sel_mix:
        df_show = df_show[df_show["MIX_OFICIAL"].isin(sel_mix)]
    if sel_flags:
        df_show = df_show[df_show["FLAG_PRINCIPAL"].isin(sel_flags)]
    if sel_hist:
        df_show = df_show[df_show["HIST_2025"].isin(sel_hist)]
    if search_sku:
        q = search_sku.strip().upper()
        mask_sku = df_show["SKU_PRODUCTO"].astype(str).str.upper().str.contains(q, na=False)
        mask_nom = df_show.get("SKU_NOM_PRODUCTO", pd.Series(dtype=str)).astype(str).str.upper().str.contains(q, na=False)
        df_show = df_show[mask_sku | mask_nom]

    st.caption(f"Mostrando {len(df_show):,} de {n_total:,} SKUs")

    # ============================================================
    # TABLA HORIZONTAL — 3 filas por SKU
    # ============================================================
    _PAGE_SIZE = 30
    n_skus = len(df_show)
    n_pages = max(1, (n_skus - 1) // _PAGE_SIZE + 1)

    pcol1, pcol2 = st.columns([1, 5])
    with pcol1:
        page_num = st.number_input("Pag.", 1, n_pages, 1, key="diag_page", label_visibility="collapsed") - 1
    with pcol2:
        st.caption(f"Pagina {page_num + 1} de {n_pages}  ·  {_PAGE_SIZE} SKUs/pag.")

    df_page = df_show.iloc[page_num * _PAGE_SIZE : (page_num + 1) * _PAGE_SIZE]

    # ── Construir HTML ──
    def _fmt(v, decimals=1):
        """Formatea un numero para celda: '-' si es cero, numero si no."""
        try:
            f = float(v)
            if f == 0:
                return "<span style='color:#cbd5e1;'>—</span>"
            return f"{f:.{decimals}f}"
        except (TypeError, ValueError):
            return "—"

    def _cell_color_venta(v, avg):
        """Color de fondo para celdas de venta (relativo al promedio del SKU)."""
        try:
            f, a = float(v), float(avg)
            if a <= 0 or f == 0:
                return ""
            ratio = f / a
            if ratio >= 1.5:
                return "background:#d1fae5;"   # verde fuerte
            if ratio >= 0.8:
                return "background:#ecfdf5;"   # verde suave
            if ratio >= 0.4:
                return "background:#fef9c3;"   # amarillo
            return "background:#fee2e2;"       # rojo
        except (TypeError, ValueError):
            return ""

    def _cell_color_stock(v, avg_demand):
        """Color de fondo para celdas de stock final."""
        try:
            f, a = float(v), float(avg_demand)
            if f == 0:
                return "background:#fee2e2;"   # rojo: agotado
            if a <= 0:
                return ""
            meses = f / a
            if meses > 12:
                return "background:#fef9c3;"   # amarillo: overstock
            if meses > 3:
                return "background:#ecfdf5;"   # verde: OK
            return "background:#fff7ed;"       # naranja: bajo
        except (TypeError, ValueError):
            return ""

    # Encabezados
    th_id = """
        <th style="min-width:95px;padding:6px 8px;text-align:left;font-size:0.72rem;font-weight:700;
                   border-right:1px solid #475569;">SKU</th>
        <th style="min-width:160px;padding:6px 8px;text-align:left;font-size:0.72rem;font-weight:700;">Nombre</th>
        <th style="min-width:60px;padding:6px 8px;text-align:left;font-size:0.72rem;font-weight:700;">Area</th>
        <th style="min-width:70px;padding:6px 8px;text-align:left;font-size:0.72rem;font-weight:700;">Linea</th>
        <th style="min-width:50px;padding:6px 8px;text-align:left;font-size:0.72rem;font-weight:700;
                   border-right:2px solid #475569;">Mix</th>
        <th style="min-width:70px;padding:6px 8px;text-align:left;font-size:0.72rem;font-weight:700;
                   border-right:2px solid #475569;">Tipo</th>
    """
    th_months_2025 = "".join(
        f'<th style="min-width:38px;padding:4px 3px;text-align:center;font-size:0.68rem;">{m}</th>'
        for m in _MES_LABELS
    )
    th_months_2026 = "".join(
        f'<th style="min-width:38px;padding:4px 3px;text-align:center;font-size:0.68rem;">{m}</th>'
        for m in _MES_LABELS
    )

    header_html = f"""
    <tr style="background:#1e293b;color:#f1f5f9;position:sticky;top:0;z-index:2;">
        {th_id}
        <th colspan="12" style="text-align:center;font-size:0.75rem;font-weight:700;padding:6px;
                                border-right:2px solid #475569;border-left:1px solid #334155;">
            ← 2025 →
        </th>
        <th colspan="12" style="text-align:center;font-size:0.75rem;font-weight:700;padding:6px;
                                border-left:1px solid #334155;">
            ← 2026 →
        </th>
    </tr>
    <tr style="background:#334155;color:#94a3b8;">
        <th></th><th></th><th></th><th></th><th style="border-right:2px solid #475569;"></th>
        <th style="border-right:2px solid #475569;"></th>
        {th_months_2025}
        {th_months_2026}
    </tr>
    """

    rows_html = ""
    for idx, (_, row) in enumerate(df_page.iterrows()):
        bg = "#ffffff" if idx % 2 == 0 else "#f8fafc"
        bg_diag = "#f0f4ff" if idx % 2 == 0 else "#eef2ff"

        sku   = str(row["SKU_PRODUCTO"])
        nom   = str(row.get("SKU_NOM_PRODUCTO", ""))[:35]
        area  = str(row.get("AREA", ""))
        linea = str(row.get("LINEA", ""))
        mix   = str(row.get("MIX_OFICIAL", ""))
        flag  = str(row.get("FLAG_PRINCIPAL", "OK"))
        fc    = _FLAG_COLORS.get(flag, "#94a3b8")
        fe    = _FLAG_EMOJI.get(flag, flag)

        avg25 = float(row.get("AVG_MENS_2025", 0) or 0)
        avg26 = float(row.get("AVG_PROY_JUN_DIC", 0) or 0)
        avg_d = avg26 if avg26 > 0 else avg25   # para colorear stock

        # ----- Fila 1: STOCK FIN -----
        sf25_cells = "".join(
            '<td style="text-align:right;padding:3px 4px;font-size:0.71rem;color:#cbd5e1;">—</td>'
            for _ in _MESES_2025
        )
        sf26_cells = "".join(
            f'<td style="text-align:right;padding:3px 4px;font-size:0.71rem;{_cell_color_stock(row.get(c,0), avg_d)}">'
            f'{_fmt(row.get(c, 0))}</td>'
            for c in _MESES_SF26
        )

        # ----- Fila 2: ETA -----
        eta25_cells = "".join(
            '<td style="text-align:right;padding:3px 4px;font-size:0.71rem;color:#cbd5e1;">—</td>'
            for _ in _MESES_2025
        )
        eta26_cells = "".join(
            f'<td style="text-align:right;padding:3px 4px;font-size:0.71rem;'
            f'{"background:#dbeafe;" if float(row.get(c, 0) or 0) > 0 else ""}">'
            f'{_fmt(row.get(c, 0))}</td>'
            for c in _MESES_ETA26
        )

        # ----- Fila 3: VTA UNDS -----
        v25_cells = "".join(
            f'<td style="text-align:right;padding:3px 4px;font-size:0.71rem;{_cell_color_venta(row.get(c,0), avg25)}">'
            f'{_fmt(row.get(c, 0))}</td>'
            for c in _MESES_2025
        )
        v26_cells = "".join(
            f'<td style="text-align:right;padding:3px 4px;font-size:0.71rem;{_cell_color_venta(row.get(c,0), avg26)}">'
            f'{_fmt(row.get(c, 0))}</td>'
            for c in _MESES_V26
        )

        # ----- Fila 4: DIAGNOSTICO -----
        ratio     = row.get("RATIO_PROY_HIST")
        yoy       = row.get("CREC_YOY")
        meses_cob = row.get("MESES_COB_DIC")
        stk_may   = float(row.get("STOCK_MAY_2026", 0) or 0)
        stk_dic   = float(row.get("STOCK_DIC_FINAL", 0) or 0)
        diag_txt  = str(row.get("DIAGNOSTICO_DETALLADO", ""))

        ratio_s   = f"{ratio:.2f}x"       if pd.notna(ratio)    else "N/D"
        yoy_s     = f"{yoy:+.1f}%"        if pd.notna(yoy)      else "N/D"
        cob_s     = f"{meses_cob:.1f} m"  if pd.notna(meses_cob) else "N/D"
        yoy_color = "#16a34a" if (pd.notna(yoy) and yoy >= 0) else "#dc2626"

        ratios_html = (
            f'<span style="margin-right:10px;"><b>Avg&#39;25:</b> {avg25:.1f}</span>'
            f'<span style="margin-right:10px;"><b>Avg&#39;26:</b> {avg26:.1f}</span>'
            f'<span style="margin-right:10px;"><b>Ratio:</b> {ratio_s}</span>'
            f'<span style="margin-right:10px;color:{yoy_color};"><b>YoY:</b> {yoy_s}</span>'
            f'<span style="margin-right:10px;"><b>Stk May:</b> {stk_may:.0f}</span>'
            f'<span style="margin-right:10px;"><b>Stk Dic:</b> {stk_dic:.0f}</span>'
            f'<span style="margin-right:14px;"><b>Cob:</b> {cob_s}</span>'
            f'<span style="background:{fc}22;color:{fc};border:1px solid {fc}88;'
            f'padding:1px 8px;border-radius:10px;font-weight:700;font-size:0.72rem;">{fe}</span>'
            f'&nbsp;&nbsp;<span style="color:#64748b;font-size:0.71rem;">{diag_txt}</span>'
        )

        # ----- Fila 4: MOI -----
        def _fmt_moi(v):
            try:
                f = float(v)
                if pd.isna(f) or f <= 0:
                    return "<span style='color:#cbd5e1;'>—</span>"
                if f >= 99:
                    return "<span style='color:#94a3b8;font-size:0.65rem;'>∞</span>"
                return f"{f:.1f}"
            except (TypeError, ValueError):
                return "—"

        def _cell_color_moi(v):
            try:
                f = float(v)
                if pd.isna(f) or f <= 0:
                    return ""
                if f >= 99:
                    return "background:#fef9c3;"   # infinito = sin demanda, amarillo
                if f > 7:
                    return "background:#fee2e2;"   # rojo: sobrestock
                if f > 4:
                    return "background:#fef9c3;"   # amarillo: alto
                if f >= 2:
                    return "background:#ecfdf5;"   # verde: OK
                return "background:#fff7ed;"       # naranja: bajo
            except (TypeError, ValueError):
                return ""

        moi25_cells = "".join(
            '<td style="text-align:right;padding:3px 4px;font-size:0.71rem;color:#cbd5e1;">—</td>'
            for _ in _MESES_2025
        )
        moi26_cells = "".join(
            f'<td style="text-align:right;padding:3px 4px;font-size:0.71rem;{_cell_color_moi(row.get(c))}">'
            f'{_fmt_moi(row.get(c))}</td>'
            for c in _MESES_MOI26
        )

        # ----- Fila 5: FORECAST_COMPRA -----
        fc25_cells = "".join(
            '<td style="text-align:right;padding:3px 4px;font-size:0.71rem;color:#cbd5e1;">—</td>'
            for _ in _MESES_2025
        )
        fc26_cells = "".join(
            f'<td style="text-align:right;padding:3px 4px;font-size:0.71rem;'
            f'{"background:#dbeafe;" if float(row.get(c, 0) or 0) > 0 else ""}">'
            f'{_fmt(row.get(c, 0))}</td>'
            for c in _MESES_FC26
        )

        # rowspan=6 en columnas de identificacion
        id_cells = f"""
            <td rowspan="6" style="font-size:0.72rem;font-weight:700;vertical-align:middle;
                                   padding:4px 8px;white-space:nowrap;border-right:1px solid #e2e8f0;">{sku}</td>
            <td rowspan="6" style="font-size:0.72rem;vertical-align:middle;padding:4px 8px;
                                   max-width:160px;overflow:hidden;text-overflow:ellipsis;"
                            title="{nom}">{nom}</td>
            <td rowspan="6" style="font-size:0.72rem;vertical-align:middle;padding:4px 8px;">{area}</td>
            <td rowspan="6" style="font-size:0.72rem;vertical-align:middle;padding:4px 8px;">{linea}</td>
            <td rowspan="6" style="font-size:0.72rem;vertical-align:middle;padding:4px 8px;
                                   border-right:2px solid #e2e8f0;">{mix}</td>
        """

        sep_top = "border-top:2px solid #e2e8f0;" if idx > 0 else ""

        rows_html += f"""
        <tr style="background:{bg};{sep_top}">
            {id_cells}
            <td style="font-size:0.68rem;font-weight:800;color:#0891b2;white-space:nowrap;
                       padding:3px 8px;border-right:2px solid #e2e8f0;">STOCK FIN</td>
            {sf25_cells}{sf26_cells}
        </tr>
        <tr style="background:{bg};">
            <td style="font-size:0.68rem;font-weight:800;color:#059669;white-space:nowrap;
                       padding:3px 8px;border-right:2px solid #e2e8f0;">VTA UNDS</td>
            {v25_cells}{v26_cells}
        </tr>
        <tr style="background:{bg};">
            <td style="font-size:0.68rem;font-weight:800;color:#1d4ed8;white-space:nowrap;
                       padding:3px 8px;border-right:2px solid #e2e8f0;">ETA</td>
            {eta25_cells}{eta26_cells}
        </tr>
        <tr style="background:{bg};">
            <td style="font-size:0.68rem;font-weight:800;color:#0369a1;white-space:nowrap;
                       padding:3px 8px;border-right:2px solid #e2e8f0;">FCST COMPRA</td>
            {fc25_cells}{fc26_cells}
        </tr>
        <tr style="background:{bg};">
            <td style="font-size:0.68rem;font-weight:800;color:#7c3aed;white-space:nowrap;
                       padding:3px 8px;border-right:2px solid #e2e8f0;">MOI</td>
            {moi25_cells}{moi26_cells}
        </tr>
        <tr style="background:{bg_diag};">
            <td style="font-size:0.68rem;font-weight:800;color:{fc};white-space:nowrap;
                       padding:4px 8px;border-right:2px solid #e2e8f0;">DIAGN.</td>
            <td colspan="24" style="font-size:0.72rem;padding:4px 10px;">{ratios_html}</td>
        </tr>
        """

    table_html = f"""
    <div style="overflow-x:auto;border:1px solid #e2e8f0;border-radius:10px;margin-top:0.5rem;">
    <table style="width:100%;border-collapse:collapse;font-family:Inter,Arial,sans-serif;">
        <thead>{header_html}</thead>
        <tbody>{rows_html}</tbody>
    </table>
    </div>
    """
    st.html(table_html)

    # ============================================================
    # DETALLE SKU INDIVIDUAL
    # ============================================================
    st.markdown("---")
    st.markdown("#### Detalle por SKU")
    sku_list = df_show["SKU_PRODUCTO"].dropna().unique().tolist()
    if not sku_list:
        st.info("No hay SKUs que mostrar con los filtros actuales.")
        return

    sel_sku = st.selectbox(
        "Selecciona un SKU para ver detalle completo",
        sku_list,
        key="diag_sel_sku",
    )

    if sel_sku:
        row = df_diag[df_diag["SKU_PRODUCTO"] == sel_sku].iloc[0]

        flag = row["FLAG_PRINCIPAL"]
        color = _FLAG_COLORS.get(flag, "#94a3b8")
        nom = row.get("SKU_NOM_PRODUCTO", "")
        area = row.get("AREA", "")
        linea = row.get("LINEA", "")

        st.html(
            f"""
            <div style="background:linear-gradient(135deg,#1e293b,#334155);border-radius:12px;
                        padding:1.25rem 1.5rem; margin-bottom:1rem;">
              <div style="display:flex;justify-content:space-between;align-items:center;">
                <div>
                  <div style="color:#f1f5f9;font-size:1.1rem;font-weight:700;">{sel_sku} — {nom}</div>
                  <div style="color:#94a3b8;font-size:0.82rem;margin-top:4px;">{area} · {linea}</div>
                </div>
                <span style="background:{color}33;color:{color};border:1px solid {color}88;
                             padding:0.3rem 0.8rem;border-radius:20px;font-weight:700;
                             font-size:0.85rem;">{_FLAG_EMOJI.get(flag, flag)}</span>
              </div>
              <div style="margin-top:0.75rem;padding-top:0.75rem;border-top:1px solid #475569;">
                <div style="color:#e2e8f0;font-size:0.82rem;">
                  <strong style="color:#94a3b8;">Todos los flags:</strong> {row['TODOS_LOS_FLAGS']}
                </div>
                <div style="color:#e2e8f0;font-size:0.82rem;margin-top:6px;">
                  <strong style="color:#94a3b8;">Diagnostico:</strong> {row['DIAGNOSTICO_DETALLADO']}
                </div>
                <div style="color:#fbbf24;font-size:0.82rem;margin-top:6px;">
                  <strong>Accion recomendada:</strong> {row['ACCION_RECOMENDADA']}
                </div>
              </div>
            </div>
            """
        )

        # KPIs del SKU
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Vta Total 2025", f"{row['VTA_TOTAL_2025']:,.1f} und")
        k2.metric("Vta Ene-May 2026", f"{row['VTA_ENE_MAY_2026']:,.1f} und")
        k3.metric("Vta Jun-Dic 2026", f"{row['VTA_JUN_DIC_2026']:,.1f} und")
        k4.metric("Stock Dic Final", f"{row['STOCK_DIC_FINAL']:,.1f} und")
        k5.metric(
            "Cobertura Dic",
            f"{row['MESES_COB_DIC']:.1f} m" if pd.notna(row["MESES_COB_DIC"]) else "N/D",
        )

        # Graficos mensuales
        import plotly.graph_objects as go
        from config import dorel_layout

        _meses = _MES_LABELS

        # Ventas chart
        v25_vals = [row.get(c, 0) for c in _MESES_2025]
        v26_vals = [row.get(c, 0) for c in _MESES_V26]

        fig_vta = go.Figure(layout=dorel_layout(height=260, showlegend=True,
            legend=dict(orientation="h", y=-0.2, xanchor="center", x=0.5),
            title=dict(text="Demanda Mensual (und)", font=dict(size=13)),
        ))
        fig_vta.add_trace(go.Bar(
            x=_meses, y=v25_vals, name="Ventas 2025",
            marker_color=COLORS.get("medium_gray", "#94a3b8"),
            opacity=0.7,
        ))
        fig_vta.add_trace(go.Bar(
            x=_meses, y=v26_vals, name="Demanda 2026",
            marker_color=COLORS.get("primary", "#1e3a5f"),
        ))
        fig_vta.update_layout(barmode="group")

        # Stock final chart
        sf_vals = [row.get(c, 0) for c in _MESES_SF26]

        fig_stk = go.Figure(layout=dorel_layout(height=260, showlegend=False,
            title=dict(text="Stock Final Mensual 2026 (und)", font=dict(size=13)),
        ))
        fig_stk.add_trace(go.Scatter(
            x=_meses, y=sf_vals, mode="lines+markers",
            line=dict(color=COLORS.get("tertiary_teal", "#0891b2"), width=2),
            fill="tozeroy", fillcolor="rgba(8,145,178,0.13)",
            name="Stock Final",
        ))

        ch1, ch2 = st.columns(2)
        with ch1:
            st.plotly_chart(fig_vta, use_container_width=True)
        with ch2:
            st.plotly_chart(fig_stk, use_container_width=True)

    # ============================================================
    # DESCARGA
    # ============================================================
    st.markdown("---")
    dl1, dl2 = st.columns([1, 3])

    with dl1:
        with st.spinner("Generando Excel..."):
            excel_bytes = _build_excel_tabla_horizontal(df_show)
        st.download_button(
            label="⬇️ Descargar Tabla (Excel)",
            data=excel_bytes,
            file_name="diagnostico_fcst_manual_tabla.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            type="primary",
        )

    with dl2:
        export_cols = (
            ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "MIX_OFICIAL"]
            + ["HIST_2025", "VTA_TOTAL_2025", "VTA_ENE_MAY_2026", "VTA_JUN_DIC_2026",
               "AVG_MENS_2025", "AVG_PROY_JUN_DIC", "RATIO_PROY_HIST", "CREC_YOY",
               "STOCK_MAY_2026", "STOCK_DIC_FINAL", "MESES_COB_DIC", "MESES_STK0_EM"]
            + _MESES_2025 + _MESES_V26 + _MESES_SF26 + _MESES_ETA26 + _MESES_FC26 + _MESES_MOI26
            + ["FLAG_PRINCIPAL", "TODOS_LOS_FLAGS", "DIAGNOSTICO_DETALLADO", "ACCION_RECOMENDADA"]
        )
        export_cols = [c for c in export_cols if c in df_show.columns]
        download_buttons(df_show[export_cols], "diagnostico_fcst_manual_plano")
