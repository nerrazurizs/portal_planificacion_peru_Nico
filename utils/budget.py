"""
Budget 2026 — Parser y loader.

Lee el archivo Excel de Budget Dorel Chile 2026 (Ventas y Aporte por canal/mes)
y lo transforma en un DataFrame limpio:  CANAL × PERIODO × VN/APORTE/COGS/MARGEN.

El resultado se cachea como CSV en data/budget_2026.csv para evitar releer
el Excel en cada sesión.
"""

import os
import numpy as np
import pandas as pd

# Ruta default del Excel fuente y del CSV cacheado
_EXCEL_DEFAULT = os.path.join(
    os.path.expanduser("~"),
    "Downloads", "01_Trabajo_Dorel", "Budget 2026_Ventas y aporte.xlsx",
)
_CSV_CACHE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "budget_2026.csv")

# Canales de interés (filas a extraer de cada sección)
_CANALES = ["ETAIL", "RETAIL", "MAYORISTA"]


def parse_budget_excel(filepath: str | None = None) -> pd.DataFrame:
    """Parse the Budget 2026 Excel file into a clean long-format DataFrame.

    Parameters
    ----------
    filepath : str, optional
        Path to the Excel file. Falls back to _EXCEL_DEFAULT.

    Returns
    -------
    pd.DataFrame
        Columns: CANAL, PERIODO, VN_BUDGET, APORTE_BUDGET, COGS_BUDGET, MARGEN_BUDGET
        48 rows (4 canales × 12 meses) — canales: ETAIL, RETAIL, MAYORISTA, TOTAL.
    """
    filepath = filepath or _EXCEL_DEFAULT
    if not os.path.exists(filepath):
        return pd.DataFrame()

    raw = pd.read_excel(filepath, sheet_name="Hoja1", header=None)

    # --- Detect the 3 sections by header labels in column 1 ---
    sections = {}  # {"VENTA": row_idx, "APORTE": row_idx, "MARGEN": row_idx}
    for i, val in raw.iloc[:, 1].items():
        sval = str(val).strip().upper() if pd.notna(val) else ""
        if sval in ("VENTA", "APORTE", "MARGEN"):
            sections[sval] = i

    if not all(k in sections for k in ("VENTA", "APORTE")):
        return pd.DataFrame()

    # --- Detect month columns (columns 2-13 should be datetime or parseable) ---
    # Row of VENTA header has the month dates in columns 2-13
    header_row = sections["VENTA"]
    month_cols = []
    for col_idx in range(2, min(14, raw.shape[1])):
        val = raw.iloc[header_row, col_idx]
        try:
            dt = pd.to_datetime(val)
            month_cols.append((col_idx, dt))
        except Exception:
            continue

    if len(month_cols) < 12:
        return pd.DataFrame()

    # --- Extract canal rows from each section ---
    def _extract_section(section_start_row: int) -> dict[str, dict[pd.Timestamp, float]]:
        """Return {canal: {periodo: value}} for rows below section_start_row."""
        result = {}
        for i in range(section_start_row + 1, min(section_start_row + 35, raw.shape[0])):
            label = str(raw.iloc[i, 1]).strip().upper() if pd.notna(raw.iloc[i, 1]) else ""
            if not label:
                break  # blank row = end of section
            if label in _CANALES:
                vals = {}
                for col_idx, dt in month_cols:
                    v = raw.iloc[i, col_idx]
                    vals[dt] = float(v) if pd.notna(v) else 0.0
                result[label] = vals
            elif "TOTAL" in label:
                vals = {}
                for col_idx, dt in month_cols:
                    v = raw.iloc[i, col_idx]
                    vals[dt] = float(v) if pd.notna(v) else 0.0
                result["TOTAL"] = vals
        # If no TOTAL row found, compute from canales
        if "TOTAL" not in result and len(result) > 0:
            total = {}
            for dt in [mc[1] for mc in month_cols]:
                total[dt] = sum(result.get(c, {}).get(dt, 0) for c in _CANALES)
            result["TOTAL"] = total
        return result

    venta = _extract_section(sections["VENTA"])
    aporte = _extract_section(sections["APORTE"])
    margen = _extract_section(sections.get("MARGEN", -1)) if "MARGEN" in sections else {}

    # --- Build long-format DataFrame ---
    rows = []
    for canal in _CANALES + ["TOTAL"]:
        for _, dt in month_cols:
            vn = venta.get(canal, {}).get(dt, 0.0)
            ap = aporte.get(canal, {}).get(dt, 0.0)
            mg = margen.get(canal, {}).get(dt, 0.0)
            cogs = vn - ap  # COGS = Venta Neta - Aporte (margen bruto)
            rows.append({
                "CANAL": canal,
                "PERIODO": dt,
                "VN_BUDGET": round(vn, 0),
                "APORTE_BUDGET": round(ap, 0),
                "COGS_BUDGET": round(cogs, 0),
                "MARGEN_BUDGET": round(mg, 4),
            })

    df = pd.DataFrame(rows)
    return df


def load_budget(filepath: str | None = None) -> pd.DataFrame:
    """Load budget data, using cached CSV if available.

    Parameters
    ----------
    filepath : str, optional
        Path to the source Excel. Only needed if CSV cache doesn't exist.

    Returns
    -------
    pd.DataFrame with columns CANAL, PERIODO, VN_BUDGET, APORTE_BUDGET,
    COGS_BUDGET, MARGEN_BUDGET.
    """
    # Try CSV cache first
    if os.path.exists(_CSV_CACHE):
        try:
            df = pd.read_csv(_CSV_CACHE)
            df["PERIODO"] = pd.to_datetime(df["PERIODO"], errors="coerce")
            for c in ["VN_BUDGET", "APORTE_BUDGET", "COGS_BUDGET", "MARGEN_BUDGET"]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
            if not df.empty:
                return df
        except Exception:
            pass

    # Parse from Excel and save cache
    df = parse_budget_excel(filepath)
    if not df.empty:
        os.makedirs(os.path.dirname(_CSV_CACHE), exist_ok=True)
        df.to_csv(_CSV_CACHE, index=False)
    return df


def get_budget_cogs_monthly(canal: str = "TOTAL") -> float:
    """Return the average monthly Budget COGS for the given canal.

    Parameters
    ----------
    canal : str
        One of "ETAIL", "RETAIL", "MAYORISTA", "TOTAL".

    Returns
    -------
    float
        Average monthly COGS from the budget. 0.0 if no data.
    """
    df = load_budget()
    if df.empty or "COGS_BUDGET" not in df.columns:
        return 0.0
    mask = df["CANAL"].str.upper() == canal.upper()
    subset = df.loc[mask, "COGS_BUDGET"]
    if subset.empty:
        return 0.0
    return float(subset.mean())
