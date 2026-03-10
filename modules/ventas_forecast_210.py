"""
Ventas Forecast 2+10 — Automated monthly forecast report builder.

Combines three data sources into a single annual forecast report:
  1. VCM real (Snowflake) — completed months (Neto, Aporte)
  2. Channel FCST Excels — current month forecast by cost center
  3. Budget Excel — remaining months forecast by cost center

Output: 4-sheet Excel matching the "DJPeru - Ventas Forecast" template,
plus a Streamlit visual preview with tabs.
"""

import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from db.queries import QUERY_VCM_CCOSTO_MONTHLY
from utils.filters import norm_cols

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MESES_ES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN",
            "JUL", "AGO", "SET", "OCT", "NOV", "DIC"]

CANALES = ["Retail", "Mayorista", "Etail"]

# Channel code mapping (VCM cod_canal → readable name)
_CANAL_MAP = {"03": "Retail", "02": "Mayorista", "06": "Etail"}

# openpyxl styles
_HDR_FONT = Font(bold=True, size=10, color="FFFFFF")
_HDR_FILL = PatternFill("solid", fgColor="2A2927")
_SECTION_FONT = Font(bold=True, size=10, color="2A2927")
_SECTION_FILL = PatternFill("solid", fgColor="D9E2F3")
_TOTAL_FONT = Font(bold=True, size=10)
_TOTAL_FILL = PatternFill("solid", fgColor="E2EFDA")
_CANAL_FONT = Font(size=10)
_PCT_FMT = "0.0%"
_NUM_FMT = "#,##0"
_NUM_FMT_DEC = "#,##0.00"
_THIN_BORDER = Border(
    left=Side(style="thin", color="D0D0D0"),
    right=Side(style="thin", color="D0D0D0"),
    top=Side(style="thin", color="D0D0D0"),
    bottom=Side(style="thin", color="D0D0D0"),
)

# Source type labels for colour coding
SRC_REAL = "REAL"
SRC_FCST = "FCST"
SRC_BUDGET = "BUDGET"

# Colours for source type in Streamlit display
_SRC_COLORS = {
    SRC_REAL: "#2A2927",    # dark (actual)
    SRC_FCST: "#1565C0",    # blue (forecast)
    SRC_BUDGET: "#757575",  # grey (budget)
}


# =========================================================================
# 1. PARSERS — Excel input files
# =========================================================================

def _find_col(columns, patterns):
    """Find column name matching any of the patterns (case-insensitive)."""
    for col in columns:
        col_upper = str(col).upper().strip()
        for pat in patterns:
            if pat.upper() in col_upper:
                return col
    return None


def _parse_fcst_retail(file) -> pd.DataFrame:
    """Parse retail FCST Excel → DataFrame[COD_CCOSTO, NETO, APORTE, CANAL].

    Reads the 'FCST X TDA' sheet with columns: CENTRO_COSTO, Fcst, Mg, Aporte.
    """
    try:
        df = pd.read_excel(file, sheet_name="FCST X TDA")
    except Exception:
        # Try first sheet as fallback
        df = pd.read_excel(file, sheet_name=0)

    df = norm_cols(df)

    # Find columns flexibly
    col_cc = _find_col(df.columns, ["CENTRO_COSTO", "CENTRO DE COSTO", "COD_CCOSTO", "CCOSTO", "TIENDA"])
    col_neto = _find_col(df.columns, ["FCST", "NETO", "VENTA"])
    col_aporte = _find_col(df.columns, ["APORTE"])
    col_mg = _find_col(df.columns, ["MG", "MARGEN", "% MARGEN"])

    if col_cc is None or col_neto is None:
        st.error("No se encontraron las columnas esperadas en el archivo FCST Retail.")
        return pd.DataFrame()

    result = pd.DataFrame()
    result["COD_CCOSTO"] = df[col_cc].astype(str).str.strip()
    result["NETO"] = pd.to_numeric(df[col_neto], errors="coerce").fillna(0)

    if col_aporte is not None:
        result["APORTE"] = pd.to_numeric(df[col_aporte], errors="coerce").fillna(0)
    elif col_mg is not None:
        mg = pd.to_numeric(df[col_mg], errors="coerce").fillna(0)
        # If margin > 1, assume it's percentage (e.g. 55 means 55%)
        mg = np.where(mg > 1, mg / 100, mg)
        result["APORTE"] = result["NETO"] * mg
    else:
        result["APORTE"] = 0.0

    result["CANAL"] = "Retail"

    # Remove empty / NaN rows and "Total" rows
    result = result[result["COD_CCOSTO"].notna()
                    & (result["COD_CCOSTO"] != "")
                    & (result["COD_CCOSTO"] != "NAN")]
    result = result[~result["COD_CCOSTO"].str.upper().str.contains("TOTAL", na=False)]

    return result.reset_index(drop=True)


def _parse_fcst_whs(file) -> pd.DataFrame:
    """Parse wholesale FCST Excel → DataFrame[COD_CCOSTO, NETO, APORTE, CANAL].

    Reads the 'FCST X CC' sheet with columns: CENTRO_COSTO, Neto M$ ACT, % Margen ACT, Aporte M$ ACT.
    """
    try:
        df = pd.read_excel(file, sheet_name="FCST X CC")
    except Exception:
        df = pd.read_excel(file, sheet_name=0)

    df = norm_cols(df)

    col_cc = _find_col(df.columns, ["CENTRO_COSTO", "CENTRO DE COSTO", "COD_CCOSTO", "CCOSTO"])
    col_neto = _find_col(df.columns, ["NETO", "VENTA"])
    col_aporte = _find_col(df.columns, ["APORTE"])
    col_mg = _find_col(df.columns, ["MARGEN", "MG"])

    if col_cc is None or col_neto is None:
        st.error("No se encontraron las columnas esperadas en el archivo FCST Wholesale.")
        return pd.DataFrame()

    result = pd.DataFrame()
    result["COD_CCOSTO"] = df[col_cc].astype(str).str.strip()
    result["NETO"] = pd.to_numeric(df[col_neto], errors="coerce").fillna(0)

    if col_aporte is not None:
        result["APORTE"] = pd.to_numeric(df[col_aporte], errors="coerce").fillna(0)
    elif col_mg is not None:
        mg = pd.to_numeric(df[col_mg], errors="coerce").fillna(0)
        mg = np.where(mg > 1, mg / 100, mg)
        result["APORTE"] = result["NETO"] * mg
    else:
        result["APORTE"] = 0.0

    result["CANAL"] = "Mayorista"

    result = result[result["COD_CCOSTO"].notna()
                    & (result["COD_CCOSTO"] != "")
                    & (result["COD_CCOSTO"] != "NAN")]
    result = result[~result["COD_CCOSTO"].str.upper().str.contains("TOTAL", na=False)]

    return result.reset_index(drop=True)


def _parse_fcst_etail(file) -> pd.DataFrame:
    """Parse etail FCST Excel → DataFrame[COD_CCOSTO, NETO, APORTE, CANAL].

    Reads the 'Etail 2026' sheet (or 'Resumen') and extracts the monthly total.
    Etail has no store-level breakdown so returns a single consolidated row.
    """
    neto = 0.0
    aporte = 0.0

    # Try to read from 'Etail 2026' sheet first (row with ETAIL PERU objective)
    try:
        df = pd.read_excel(file, sheet_name=0, header=None)
        # Scan for a row containing the forecast objective
        for i in range(min(20, df.shape[0])):
            for j in range(min(10, df.shape[1])):
                val = str(df.iloc[i, j]).strip().upper() if pd.notna(df.iloc[i, j]) else ""
                if "ETAIL" in val and "PERU" in val:
                    # Objective is usually in the next columns
                    for k in range(j + 1, min(j + 5, df.shape[1])):
                        v = df.iloc[i, k]
                        if pd.notna(v):
                            try:
                                candidate = float(v)
                                if candidate > 1000:  # likely the neto amount
                                    neto = candidate
                                elif 0 < candidate < 1:  # likely margin
                                    pass
                                break
                            except (ValueError, TypeError):
                                continue
    except Exception:
        pass

    # Try Resumen sheet for structured data
    if neto == 0:
        try:
            df_res = pd.read_excel(file, sheet_name="Resumen", header=None)
            for i in range(df_res.shape[0]):
                label = str(df_res.iloc[i, 0]).strip().upper() if pd.notna(df_res.iloc[i, 0]) else ""
                label1 = str(df_res.iloc[i, 1]).strip().upper() if df_res.shape[1] > 1 and pd.notna(df_res.iloc[i, 1]) else ""
                if "FORECAST" in label or "FCST" in label or "FORECAST" in label1 or "FCST" in label1:
                    # Look for neto value in this or next rows
                    for row_offset in range(0, 3):
                        ri = i + row_offset
                        if ri >= df_res.shape[0]:
                            break
                        for j in range(2, min(15, df_res.shape[1])):
                            v = df_res.iloc[ri, j]
                            if pd.notna(v):
                                try:
                                    candidate = float(v)
                                    if candidate > 1000:
                                        neto = candidate
                                        break
                                except (ValueError, TypeError):
                                    continue
                        if neto > 0:
                            break
        except Exception:
            pass

    # If we got neto but no aporte, use a default margin
    if neto > 0 and aporte == 0:
        aporte = neto * 0.49  # ~49% margin typical for etail

    result = pd.DataFrame([{
        "COD_CCOSTO": "ETAIL",
        "NETO": neto,
        "APORTE": aporte,
        "CANAL": "Etail",
    }])
    return result


def _parse_budget(file) -> pd.DataFrame:
    """Parse Budget Excel → DataFrame[COD_CCOSTO, CANAL, MES, NETO, APORTE].

    Expected structure: rows per cost center, columns per month (12 months).
    Tries multiple formats:
      A) Wide format with sections VENTA/APORTE (similar to Chile Budget)
      B) Long format with explicit columns
      C) Multi-sheet with one sheet per channel
    """
    xls = pd.ExcelFile(file)
    all_rows = []

    # Strategy: try to find sheets that match channels or a consolidated sheet
    for sheet_name in xls.sheet_names:
        df = pd.read_excel(file, sheet_name=sheet_name, header=None)
        if df.empty or df.shape[0] < 3:
            continue

        # Detect if this sheet has month headers (ENE-DIC pattern)
        month_row = _detect_month_header_row(df)
        if month_row is None:
            continue

        # Detect channel from sheet name or content
        canal = _detect_canal(sheet_name, df)

        # Detect month columns
        month_cols = _detect_month_cols(df, month_row)
        if len(month_cols) < 6:
            continue

        # Detect metric sections (VENTA, APORTE, etc.)
        sections = _detect_sections(df, month_row)

        if "VENTA" in sections and "APORTE" in sections:
            # Structured with sections — extract cost center rows from each
            ccosto_col = _detect_ccosto_col(df, month_row)
            venta_rows = _extract_section_rows(df, sections["VENTA"], sections, ccosto_col)
            aporte_rows = _extract_section_rows(df, sections["APORTE"], sections, ccosto_col)

            for cc in venta_rows:
                for mes, val in venta_rows[cc].items():
                    ap = aporte_rows.get(cc, {}).get(mes, 0)
                    all_rows.append({
                        "COD_CCOSTO": cc,
                        "CANAL": canal,
                        "MES": mes,
                        "NETO": val,
                        "APORTE": ap,
                    })
        else:
            # Simple table: rows are cost centers, columns are months
            ccosto_col = _detect_ccosto_col(df, month_row)
            if ccosto_col is None:
                continue

            for i in range(month_row + 1, df.shape[0]):
                cc = df.iloc[i, ccosto_col]
                if pd.isna(cc) or str(cc).strip() == "":
                    continue
                cc = str(cc).strip()
                if "TOTAL" in cc.upper():
                    continue
                for mes_num, col_idx in month_cols.items():
                    val = df.iloc[i, col_idx]
                    neto = float(val) if pd.notna(val) else 0.0
                    all_rows.append({
                        "COD_CCOSTO": cc,
                        "CANAL": canal,
                        "MES": mes_num,
                        "NETO": neto,
                        "APORTE": 0.0,  # will need to be filled from margin
                    })

    if not all_rows:
        return pd.DataFrame()

    return pd.DataFrame(all_rows)


def _detect_month_header_row(df, max_scan=10):
    """Find the row containing month headers (ENE, FEB, etc.)."""
    month_patterns = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN",
                      "JUL", "AGO", "SET", "SEP", "OCT", "NOV", "DIC"]
    for i in range(min(max_scan, df.shape[0])):
        row_vals = [str(v).strip().upper() for v in df.iloc[i] if pd.notna(v)]
        matches = sum(1 for v in row_vals if v in month_patterns)
        if matches >= 6:
            return i
    return None


def _detect_month_cols(df, header_row):
    """Map month number (1-12) → column index from the header row."""
    month_map = {
        "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AGO": 8, "SET": 9, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
    }
    result = {}
    for j in range(df.shape[1]):
        val = str(df.iloc[header_row, j]).strip().upper() if pd.notna(df.iloc[header_row, j]) else ""
        if val in month_map:
            result[month_map[val]] = j
    return result


def _detect_canal(sheet_name, df):
    """Detect channel from sheet name or first few rows."""
    sn = sheet_name.upper()
    if "RETAIL" in sn or "TIENDA" in sn or "MINOR" in sn:
        return "Retail"
    if "MAYOR" in sn or "WHOLESALE" in sn or "WHS" in sn:
        return "Mayorista"
    if "ETAIL" in sn or "ECOM" in sn:
        return "Etail"
    # Scan first rows
    for i in range(min(5, df.shape[0])):
        for j in range(min(5, df.shape[1])):
            val = str(df.iloc[i, j]).strip().upper() if pd.notna(df.iloc[i, j]) else ""
            if "RETAIL" in val:
                return "Retail"
            if "MAYOR" in val or "WHOLESALE" in val:
                return "Mayorista"
            if "ETAIL" in val:
                return "Etail"
    return "Retail"  # default


def _detect_sections(df, month_row):
    """Find section header rows (VENTA, APORTE, MARGEN, COSTO) above month_row or in col 0-2."""
    sections = {}
    for i in range(df.shape[0]):
        for j in range(min(5, df.shape[1])):
            val = str(df.iloc[i, j]).strip().upper() if pd.notna(df.iloc[i, j]) else ""
            if val in ("VENTA", "NETA", "NETO"):
                sections["VENTA"] = i
            elif val == "APORTE":
                sections["APORTE"] = i
            elif val in ("COSTO", "VTA COSTO"):
                sections["COSTO"] = i
            elif "MARGEN" in val or "MG%" in val:
                sections["MARGEN"] = i
    return sections


def _detect_ccosto_col(df, month_row):
    """Find the column containing cost center names."""
    for j in range(min(5, df.shape[1])):
        val = str(df.iloc[month_row, j]).strip().upper() if pd.notna(df.iloc[month_row, j]) else ""
        if any(k in val for k in ["CENTRO", "CCOSTO", "COSTO", "TIENDA", "CLIENTE"]):
            return j
    return 0  # default to first column


def _extract_section_rows(df, section_start, all_sections, ccosto_col):
    """Extract cost center → {month: value} from a section."""
    # Find section end (next section start or end of data)
    sorted_starts = sorted(v for v in all_sections.values() if v > section_start)
    section_end = sorted_starts[0] if sorted_starts else df.shape[0]

    result = {}
    for i in range(section_start + 1, section_end):
        cc = df.iloc[i, ccosto_col] if ccosto_col is not None else df.iloc[i, 0]
        if pd.isna(cc) or str(cc).strip() == "" or "TOTAL" in str(cc).upper():
            continue
        cc = str(cc).strip()
        vals = {}
        for j in range(df.shape[1]):
            v = df.iloc[i, j]
            if pd.notna(v) and j != ccosto_col:
                try:
                    vals[j] = float(v)
                except (ValueError, TypeError):
                    pass
        result[cc] = vals
    return result


# =========================================================================
# 2. VCM data loader
# =========================================================================

@st.cache_data(ttl=1800, show_spinner=False)
def _load_vcm_real(_conn):
    """Load VCM actual data grouped by cost center, month, and channel."""
    df = pd.read_sql(QUERY_VCM_CCOSTO_MONTHLY, _conn)
    df = norm_cols(df)
    if df.empty:
        return df

    # Parse period to month number
    df["PERIODO"] = pd.to_datetime(df["PERIODO"], errors="coerce")
    df["MES"] = df["PERIODO"].dt.month

    # Map channel code → readable name
    df["CANAL"] = df["COD_CANAL"].map(_CANAL_MAP).fillna("Otro")

    # Ensure numeric
    for c in ["NETO", "APORTE", "COSTO", "UNIDADES"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    return df


# =========================================================================
# 3. Report builder — combine VCM + FCST + Budget
# =========================================================================

def _build_combined_data(df_vcm, df_fcst_retail, df_fcst_whs, df_fcst_etail,
                         df_budget, current_month):
    """Build the combined annual data with source tracking.

    Returns:
        df_detail: DataFrame[COD_CCOSTO, CANAL, MES, NETO, APORTE, COSTO, SOURCE]
        df_consolidated: DataFrame[CANAL, MES, NETO, APORTE, COSTO, UNIDADES, SOURCE]
    """
    rows = []

    # ── Past months: VCM real (month 1 to current_month - 1) ──
    if not df_vcm.empty:
        past = df_vcm[df_vcm["MES"] < current_month].copy()
        for _, r in past.iterrows():
            rows.append({
                "COD_CCOSTO": str(r.get("COD_CCOSTO", "")).strip(),
                "CANAL": r["CANAL"],
                "MES": int(r["MES"]),
                "NETO": r["NETO"],
                "APORTE": r["APORTE"],
                "UNIDADES": r.get("UNIDADES", 0),
                "SOURCE": SRC_REAL,
            })

    # ── Current month: FCST files ──
    for df_fcst in [df_fcst_retail, df_fcst_whs, df_fcst_etail]:
        if df_fcst is not None and not df_fcst.empty:
            for _, r in df_fcst.iterrows():
                rows.append({
                    "COD_CCOSTO": str(r["COD_CCOSTO"]).strip(),
                    "CANAL": r["CANAL"],
                    "MES": current_month,
                    "NETO": r["NETO"],
                    "APORTE": r["APORTE"],
                    "UNIDADES": 0,
                    "SOURCE": SRC_FCST,
                })

    # ── Future months: Budget (current_month + 1 to 12) ──
    if df_budget is not None and not df_budget.empty:
        future = df_budget[df_budget["MES"] > current_month].copy()
        for _, r in future.iterrows():
            rows.append({
                "COD_CCOSTO": str(r["COD_CCOSTO"]).strip(),
                "CANAL": r["CANAL"],
                "MES": int(r["MES"]),
                "NETO": r["NETO"],
                "APORTE": r["APORTE"],
                "UNIDADES": 0,
                "SOURCE": SRC_BUDGET,
            })

    if not rows:
        return pd.DataFrame(), pd.DataFrame()

    df_detail = pd.DataFrame(rows)
    df_detail["COSTO"] = df_detail["NETO"] - df_detail["APORTE"]
    df_detail["MARGEN"] = np.where(
        df_detail["NETO"] > 0,
        df_detail["APORTE"] / df_detail["NETO"],
        0.0,
    )

    # Build consolidated (channel × month)
    df_consolidated = (
        df_detail
        .groupby(["CANAL", "MES", "SOURCE"], as_index=False)
        .agg(NETO=("NETO", "sum"),
             APORTE=("APORTE", "sum"),
             COSTO=("COSTO", "sum"),
             UNIDADES=("UNIDADES", "sum"))
    )
    df_consolidated["MARGEN"] = np.where(
        df_consolidated["NETO"] > 0,
        df_consolidated["APORTE"] / df_consolidated["NETO"],
        0.0,
    )

    return df_detail, df_consolidated


def _pivot_consolidated(df_cons):
    """Pivot consolidated data into the template layout.

    Returns dict: {metric: {canal: {mes: value}}}
    """
    metrics = {}
    for metric_name, col in [("Neta", "NETO"), ("Aporte", "APORTE"),
                              ("Margen%", "MARGEN"), ("Costo", "COSTO"),
                              ("Unidades", "UNIDADES")]:
        canal_data = {}
        for canal in CANALES + ["Totales"]:
            mes_vals = {}
            for mes in range(1, 13):
                if canal == "Totales":
                    mask = df_cons["MES"] == mes
                else:
                    mask = (df_cons["CANAL"] == canal) & (df_cons["MES"] == mes)
                subset = df_cons[mask]
                if col == "MARGEN":
                    neto_sum = subset["NETO"].sum()
                    aporte_sum = subset["APORTE"].sum()
                    mes_vals[mes] = aporte_sum / neto_sum if neto_sum > 0 else 0.0
                else:
                    mes_vals[mes] = subset[col].sum()
            canal_data[canal] = mes_vals
        metrics[metric_name] = canal_data
    return metrics


def _get_source_for_month(current_month, mes):
    """Return the source type for a given month."""
    if mes < current_month:
        return SRC_REAL
    elif mes == current_month:
        return SRC_FCST
    else:
        return SRC_BUDGET


# =========================================================================
# 4. Streamlit UI
# =========================================================================

def _display_consolidated_table(metrics, current_month):
    """Display the PERU consolidated table in Streamlit."""
    # Build a display DataFrame
    rows = []
    for metric_name in ["Neta", "Aporte", "Margen%", "Costo", "Unidades"]:
        for canal in CANALES + ["Totales"]:
            row = {"Seccion": metric_name, "Canal": canal}
            total = 0.0
            for mes in range(1, 13):
                val = metrics[metric_name][canal][mes]
                col_name = MESES_ES[mes - 1]
                if metric_name == "Margen%":
                    row[col_name] = f"{val:.1%}"
                else:
                    row[col_name] = val
                    total += val
            if metric_name == "Margen%":
                # Total margin = weighted average
                neto_total = sum(metrics["Neta"][canal][m] for m in range(1, 13))
                aporte_total = sum(metrics["Aporte"][canal][m] for m in range(1, 13))
                row["TOTAL"] = f"{aporte_total / neto_total:.1%}" if neto_total > 0 else "0.0%"
            else:
                row["TOTAL"] = total
            rows.append(row)

    df_display = pd.DataFrame(rows)

    # Format numeric columns
    num_cols = MESES_ES + ["TOTAL"]
    col_config = {}
    for col in num_cols:
        col_config[col] = st.column_config.NumberColumn(
            col,
            format="%.0f",
        )

    st.dataframe(
        df_display,
        use_container_width=True,
        height=min(len(rows) * 38 + 50, 800),
        hide_index=True,
    )


def _display_detail_table(df_detail, canal, current_month):
    """Display a Retail/Wholesale detail table in Streamlit."""
    mask = df_detail["CANAL"] == canal
    df_ch = df_detail[mask].copy()

    if df_ch.empty:
        st.info(f"No hay datos para {canal}.")
        return

    # Pivot: rows = COD_CCOSTO, columns = MES, values = NETO
    metrics_to_show = ["NETO", "COSTO", "APORTE", "MARGEN"]
    labels = {"NETO": "VENTA", "COSTO": "COSTO", "APORTE": "APORTE", "MARGEN": "MARGEN %"}

    for metric in metrics_to_show:
        if metric == "MARGEN":
            # Pivot neto and aporte separately, then compute margin
            pv_neto = df_ch.pivot_table(
                index="COD_CCOSTO", columns="MES", values="NETO", aggfunc="sum", fill_value=0
            )
            pv_aporte = df_ch.pivot_table(
                index="COD_CCOSTO", columns="MES", values="APORTE", aggfunc="sum", fill_value=0
            )
            pv = pv_aporte / pv_neto.replace(0, np.nan)
            pv = pv.fillna(0)
        else:
            pv = df_ch.pivot_table(
                index="COD_CCOSTO", columns="MES", values=metric, aggfunc="sum", fill_value=0
            )

        # Rename columns to month names
        pv.columns = [MESES_ES[m - 1] if m in range(1, 13) else m for m in pv.columns]

        # Add total column
        if metric == "MARGEN":
            neto_totals = pv_neto.sum(axis=1)
            aporte_totals = pv_aporte.sum(axis=1)
            pv["TOTAL"] = np.where(neto_totals > 0, aporte_totals / neto_totals, 0.0)
        else:
            pv["TOTAL"] = pv.sum(axis=1)

        # Add totals row
        if metric == "MARGEN":
            totals_row = {}
            for col in pv.columns:
                if col == "TOTAL":
                    total_neto = pv_neto.sum().sum()
                    total_aporte = pv_aporte.sum().sum()
                    totals_row["TOTAL"] = total_aporte / total_neto if total_neto > 0 else 0
                else:
                    m_idx = MESES_ES.index(col) + 1 if col in MESES_ES else None
                    if m_idx:
                        n = pv_neto[m_idx].sum() if m_idx in pv_neto.columns else 0
                        a = pv_aporte[m_idx].sum() if m_idx in pv_aporte.columns else 0
                        totals_row[col] = a / n if n > 0 else 0
                    else:
                        totals_row[col] = 0
            pv.loc["TOTALES"] = totals_row
        else:
            pv.loc["TOTALES"] = pv.sum()

        pv = pv.reset_index()
        pv = pv.rename(columns={"COD_CCOSTO": "Centro de Costo"})

        st.markdown(f"**{labels[metric]}**")
        st.dataframe(pv, use_container_width=True, hide_index=True,
                     height=min(len(pv) * 36 + 50, 600))


def _display_etail_table(df_detail, df_budget_etail, current_month):
    """Display the Etail Peru summary table."""
    mask = df_detail["CANAL"] == "Etail"
    df_et = df_detail[mask].copy()

    if df_et.empty:
        st.info("No hay datos para Etail.")
        return

    # Build rows: BU, Fcst, Vta Real, Cumpl%
    bu_row = {"Concepto": "Budget"}
    fcst_row = {"Concepto": "Forecast"}
    real_row = {"Concepto": "Vta Real"}
    cumpl_row = {"Concepto": "Cumpl % vs Budget"}

    for mes in range(1, 13):
        col_name = MESES_ES[mes - 1]
        # Budget value
        bu_val = 0.0
        if df_budget_etail is not None and not df_budget_etail.empty:
            bu_mask = df_budget_etail["MES"] == mes
            bu_val = df_budget_etail.loc[bu_mask, "NETO"].sum()
        bu_row[col_name] = bu_val

        # Forecast / Real
        et_mask = df_et["MES"] == mes
        fcst_val = df_et.loc[et_mask, "NETO"].sum()

        source = _get_source_for_month(current_month, mes)
        if source == SRC_REAL:
            real_row[col_name] = fcst_val
            fcst_row[col_name] = bu_val  # use budget as forecast for past months
        else:
            fcst_row[col_name] = fcst_val
            real_row[col_name] = fcst_val  # future months: forecast = expected real

        # Compliance
        if bu_val > 0:
            cumpl_row[col_name] = fcst_val / bu_val
        else:
            cumpl_row[col_name] = 0.0

    # Totals
    for row in [bu_row, fcst_row, real_row]:
        row["TOTAL"] = sum(v for k, v in row.items() if k not in ("Concepto", "TOTAL"))
    cumpl_row["TOTAL"] = (
        real_row["TOTAL"] / bu_row["TOTAL"] if bu_row["TOTAL"] > 0 else 0
    )

    df_display = pd.DataFrame([bu_row, fcst_row, real_row, cumpl_row])
    st.dataframe(df_display, use_container_width=True, hide_index=True, height=220)


# =========================================================================
# 5. Excel export — openpyxl formatted output
# =========================================================================

def _write_peru_sheet(wb, metrics, current_month, df_budget_cons=None):
    """Write the consolidated 'PERU' sheet."""
    ws = wb.active
    ws.title = "PERU"

    # ── Header row 1-2 ──
    n_real = current_month - 1
    n_fcst = 12 - n_real
    ws.merge_cells("B1:N1")
    ws["B1"] = f"Fcst {n_real} + {n_fcst}"
    ws["B1"].font = Font(bold=True, size=14, color="2A2927")

    # ── Section: Avance Neta Real {year} ──
    row = 3
    sections = [
        ("Avance Neta Real 2025", "Neta"),
        ("Avance Aporte Real 2025", "Aporte"),
        ("Avance Mgn% Real 2025", "Margen%"),
        ("Avance Vta Costo", "Costo"),
        ("Avance Vta Unds", "Unidades"),
    ]

    for section_label, metric_key in sections:
        # Section title
        ws.cell(row=row, column=2, value=section_label).font = _SECTION_FONT

        # Year total
        if metric_key != "Margen%":
            total_all = sum(metrics[metric_key]["Totales"][m] for m in range(1, 13))
            ws.cell(row=row, column=4, value=round(total_all, 2)).font = _SECTION_FONT
            ws.cell(row=row, column=4).number_format = _NUM_FMT_DEC

        row += 1

        # Sub-header: Fcst | ENE | FEB | ... | DIC | Total
        ws.cell(row=row, column=2, value="Fcst").font = _HDR_FONT
        ws.cell(row=row, column=2).fill = _HDR_FILL
        for m in range(1, 13):
            c = ws.cell(row=row, column=m + 2, value=MESES_ES[m - 1])
            c.font = _HDR_FONT
            c.fill = _HDR_FILL
            c.alignment = Alignment(horizontal="center")
        c = ws.cell(row=row, column=15, value="Total")
        c.font = _HDR_FONT
        c.fill = _HDR_FILL
        c.alignment = Alignment(horizontal="center")
        row += 1

        # Data rows: Retail, Mayorista, Etail, Totales
        for canal in CANALES + ["Totales"]:
            ws.cell(row=row, column=2, value=canal)
            if canal == "Totales":
                ws.cell(row=row, column=2).font = _TOTAL_FONT
            else:
                ws.cell(row=row, column=2).font = _CANAL_FONT

            year_total = 0.0
            for m in range(1, 13):
                val = metrics[metric_key][canal][m]
                c = ws.cell(row=row, column=m + 2, value=val)
                c.border = _THIN_BORDER

                if metric_key == "Margen%":
                    c.number_format = _PCT_FMT
                else:
                    c.number_format = _NUM_FMT_DEC

                # Color by source
                src = _get_source_for_month(current_month, m)
                if src == SRC_FCST:
                    c.font = Font(size=10, color="1565C0")
                elif src == SRC_BUDGET:
                    c.font = Font(size=10, color="757575", italic=True)

                if metric_key != "Margen%":
                    year_total += val

                if canal == "Totales":
                    c.font = Font(bold=True, size=10,
                                  color=c.font.color if c.font.color else "000000")

            # Total column
            if metric_key == "Margen%":
                neto_total = sum(metrics["Neta"][canal][m] for m in range(1, 13))
                aporte_total = sum(metrics["Aporte"][canal][m] for m in range(1, 13))
                total_val = aporte_total / neto_total if neto_total > 0 else 0
                c = ws.cell(row=row, column=15, value=total_val)
                c.number_format = _PCT_FMT
            else:
                c = ws.cell(row=row, column=15, value=year_total)
                c.number_format = _NUM_FMT_DEC

            if canal == "Totales":
                c.font = _TOTAL_FONT
            c.border = _THIN_BORDER
            row += 1

        row += 1  # blank row between sections

    # Set column widths
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 25
    for col_idx in range(3, 16):
        ws.column_dimensions[get_column_letter(col_idx)].width = 16


def _write_detail_sheet(wb, sheet_name, canal_label, df_detail, current_month):
    """Write a Retail/Wholesale detail sheet with 4 metric blocks side-by-side."""
    ws = wb.create_sheet(title=sheet_name)

    # Row 0: Channel label
    ws.cell(row=1, column=1, value=f"CANAL {canal_label.upper()}")
    ws.cell(row=1, column=1).font = Font(bold=True, size=12, color="2A2927")

    # Get unique cost centers
    ccostos = sorted(df_detail["COD_CCOSTO"].unique())

    # Define the 4 metric blocks
    blocks = [
        ("VENTA", "NETO"),
        ("COSTO", "COSTO"),
        ("APORTE", "APORTE"),
        ("MARGEN %", "MARGEN"),
    ]

    # Each block: 1 label col + 12 months + 1 total = 14 cols, + 1 separator
    block_width = 14  # ENE-DIC + TOTAL = 13 cols (no separate label col per block)
    start_cols = []
    for b_idx in range(len(blocks)):
        start_col = 4 + b_idx * (block_width + 1)  # col D, then S, then AH, etc.
        start_cols.append(start_col)

    # Row 3: Metric block headers
    for b_idx, (block_label, _) in enumerate(blocks):
        sc = start_cols[b_idx]
        ws.cell(row=3, column=sc, value=block_label)
        ws.cell(row=3, column=sc).font = _SECTION_FONT
        ws.cell(row=3, column=sc).fill = _SECTION_FILL

    # Row 4: Column headers
    ws.cell(row=4, column=1, value="CENTRO DE COSTO")
    ws.cell(row=4, column=1).font = _HDR_FONT
    ws.cell(row=4, column=1).fill = _HDR_FILL

    for b_idx in range(len(blocks)):
        sc = start_cols[b_idx]
        for m in range(12):
            c = ws.cell(row=4, column=sc + m, value=MESES_ES[m])
            c.font = _HDR_FONT
            c.fill = _HDR_FILL
            c.alignment = Alignment(horizontal="center")
        c = ws.cell(row=4, column=sc + 12, value="TOTAL")
        c.font = _HDR_FONT
        c.fill = _HDR_FILL
        c.alignment = Alignment(horizontal="center")

    # Data rows
    data_row = 5
    for cc in ccostos:
        cc_data = df_detail[df_detail["COD_CCOSTO"] == cc]

        ws.cell(row=data_row, column=1, value=cc)
        ws.cell(row=data_row, column=1).font = _CANAL_FONT

        for b_idx, (_, metric_col) in enumerate(blocks):
            sc = start_cols[b_idx]
            year_total = 0.0

            for m in range(1, 13):
                m_data = cc_data[cc_data["MES"] == m]
                if metric_col == "MARGEN":
                    neto_sum = m_data["NETO"].sum()
                    aporte_sum = m_data["APORTE"].sum()
                    val = aporte_sum / neto_sum if neto_sum > 0 else 0
                else:
                    val = m_data[metric_col].sum() if not m_data.empty else 0

                c = ws.cell(row=data_row, column=sc + m - 1, value=val)
                c.border = _THIN_BORDER

                if metric_col == "MARGEN":
                    c.number_format = _PCT_FMT
                else:
                    c.number_format = _NUM_FMT_DEC
                    year_total += val

                # Color by source
                src = _get_source_for_month(current_month, m)
                if src == SRC_FCST:
                    c.font = Font(size=9, color="1565C0")
                elif src == SRC_BUDGET:
                    c.font = Font(size=9, color="757575", italic=True)

            # Total
            if metric_col == "MARGEN":
                neto_all = cc_data["NETO"].sum()
                aporte_all = cc_data["APORTE"].sum()
                total_val = aporte_all / neto_all if neto_all > 0 else 0
                c = ws.cell(row=data_row, column=sc + 12, value=total_val)
                c.number_format = _PCT_FMT
            else:
                c = ws.cell(row=data_row, column=sc + 12, value=year_total)
                c.number_format = _NUM_FMT_DEC
            c.border = _THIN_BORDER

        data_row += 1

    # Totals row
    ws.cell(row=data_row + 1, column=1, value="TOTALES")
    ws.cell(row=data_row + 1, column=1).font = _TOTAL_FONT

    for b_idx, (_, metric_col) in enumerate(blocks):
        sc = start_cols[b_idx]
        year_grand = 0.0
        for m in range(1, 13):
            m_data = df_detail[df_detail["MES"] == m]
            if metric_col == "MARGEN":
                n = m_data["NETO"].sum()
                a = m_data["APORTE"].sum()
                val = a / n if n > 0 else 0
            else:
                val = m_data[metric_col].sum()
                year_grand += val

            c = ws.cell(row=data_row + 1, column=sc + m - 1, value=val)
            c.font = _TOTAL_FONT
            c.border = _THIN_BORDER
            c.number_format = _PCT_FMT if metric_col == "MARGEN" else _NUM_FMT_DEC

        # Grand total
        if metric_col == "MARGEN":
            n = df_detail["NETO"].sum()
            a = df_detail["APORTE"].sum()
            total_val = a / n if n > 0 else 0
            c = ws.cell(row=data_row + 1, column=sc + 12, value=total_val)
            c.number_format = _PCT_FMT
        else:
            c = ws.cell(row=data_row + 1, column=sc + 12, value=year_grand)
            c.number_format = _NUM_FMT_DEC
        c.font = _TOTAL_FONT
        c.border = _THIN_BORDER

    # Column widths
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 3
    ws.column_dimensions["C"].width = 3
    for b_idx in range(len(blocks)):
        sc = start_cols[b_idx]
        for offset in range(13):
            ws.column_dimensions[get_column_letter(sc + offset)].width = 14


def _write_etail_sheet(wb, df_detail, df_budget_etail, current_month):
    """Write the Etail Peru sheet."""
    ws = wb.create_sheet(title="Etail Peru")

    mask = df_detail["CANAL"] == "Etail"
    df_et = df_detail[mask].copy()

    # Row headers
    ws.cell(row=2, column=2, value="BU 2025").font = Font(bold=True, size=11)

    # Month headers
    month_labels_lower = ["ene.", "feb.", "mar.", "abr.", "may.", "jun.",
                          "jul.", "ago.", "sep.", "oct.", "nov.", "dic."]
    for m in range(12):
        c = ws.cell(row=2, column=m + 3, value=month_labels_lower[m])
        c.font = _HDR_FONT
        c.fill = _HDR_FILL
        c.alignment = Alignment(horizontal="center")
    c = ws.cell(row=2, column=15, value="Totales")
    c.font = _HDR_FONT
    c.fill = _HDR_FILL

    # Section: BU (Budget)
    row_labels = [
        ("Venta", "NETO"),
        ("Aporte", "APORTE"),
        ("Mg%", "MARGEN"),
    ]

    row_idx = 3
    for label, metric in row_labels:
        ws.cell(row=row_idx, column=2, value=label).font = _CANAL_FONT
        total = 0.0
        for m in range(1, 13):
            if metric == "MARGEN":
                bu_n = 0
                bu_a = 0
                if df_budget_etail is not None and not df_budget_etail.empty:
                    bu_mask = df_budget_etail["MES"] == m
                    bu_n = df_budget_etail.loc[bu_mask, "NETO"].sum()
                    bu_a = df_budget_etail.loc[bu_mask, "APORTE"].sum()
                val = bu_a / bu_n if bu_n > 0 else 0
            else:
                val = 0
                if df_budget_etail is not None and not df_budget_etail.empty:
                    bu_mask = df_budget_etail["MES"] == m
                    val = df_budget_etail.loc[bu_mask, metric].sum()
                total += val

            c = ws.cell(row=row_idx, column=m + 2, value=val)
            c.border = _THIN_BORDER
            c.number_format = _PCT_FMT if metric == "MARGEN" else _NUM_FMT_DEC

        if metric == "MARGEN":
            all_n = df_budget_etail["NETO"].sum() if df_budget_etail is not None and not df_budget_etail.empty else 0
            all_a = df_budget_etail["APORTE"].sum() if df_budget_etail is not None and not df_budget_etail.empty else 0
            ws.cell(row=row_idx, column=15, value=all_a / all_n if all_n > 0 else 0).number_format = _PCT_FMT
        else:
            ws.cell(row=row_idx, column=15, value=total).number_format = _NUM_FMT_DEC
        row_idx += 1

    row_idx += 1  # blank

    # Section: Fcst (combined = real + fcst + budget)
    ws.cell(row=row_idx, column=2, value="Fcst").font = Font(bold=True, size=11)
    for m in range(12):
        c = ws.cell(row=row_idx, column=m + 3, value=month_labels_lower[m])
        c.font = _HDR_FONT
        c.fill = _HDR_FILL
        c.alignment = Alignment(horizontal="center")
    row_idx += 1

    for label, metric in row_labels:
        ws.cell(row=row_idx, column=2, value=label).font = _CANAL_FONT
        total = 0.0
        for m in range(1, 13):
            m_data = df_et[df_et["MES"] == m]
            if metric == "MARGEN":
                n = m_data["NETO"].sum()
                a = m_data["APORTE"].sum()
                val = a / n if n > 0 else 0
            else:
                val = m_data[metric].sum()
                total += val

            c = ws.cell(row=row_idx, column=m + 2, value=val)
            c.border = _THIN_BORDER
            c.number_format = _PCT_FMT if metric == "MARGEN" else _NUM_FMT_DEC

        if metric == "MARGEN":
            n = df_et["NETO"].sum()
            a = df_et["APORTE"].sum()
            ws.cell(row=row_idx, column=15, value=a / n if n > 0 else 0).number_format = _PCT_FMT
        else:
            ws.cell(row=row_idx, column=15, value=total).number_format = _NUM_FMT_DEC
        row_idx += 1

    row_idx += 1

    # Section: Vta Real
    ws.cell(row=row_idx, column=2, value="Vta Real").font = Font(bold=True, size=11)
    for m in range(12):
        c = ws.cell(row=row_idx, column=m + 3, value=month_labels_lower[m])
        c.font = _HDR_FONT
        c.fill = _HDR_FILL
        c.alignment = Alignment(horizontal="center")
    row_idx += 1

    for label, metric in row_labels:
        ws.cell(row=row_idx, column=2, value=label).font = _CANAL_FONT
        total = 0.0
        for m in range(1, 13):
            m_data = df_et[(df_et["MES"] == m) & (df_et["SOURCE"] == SRC_REAL)]
            if m_data.empty:
                m_data = df_et[df_et["MES"] == m]  # fallback for non-real months
            if metric == "MARGEN":
                n = m_data["NETO"].sum()
                a = m_data["APORTE"].sum()
                val = a / n if n > 0 else 0
            else:
                val = m_data[metric].sum()
                total += val

            c = ws.cell(row=row_idx, column=m + 2, value=val)
            c.border = _THIN_BORDER
            c.number_format = _PCT_FMT if metric == "MARGEN" else _NUM_FMT_DEC

        if metric == "MARGEN":
            n = df_et["NETO"].sum()
            a = df_et["APORTE"].sum()
            ws.cell(row=row_idx, column=15, value=a / n if n > 0 else 0).number_format = _PCT_FMT
        else:
            ws.cell(row=row_idx, column=15, value=total).number_format = _NUM_FMT_DEC
        row_idx += 1

    row_idx += 1

    # Section: Cumpl % vs Budget
    ws.cell(row=row_idx, column=2, value="Cumpl % vs Budget").font = Font(bold=True, size=11)
    for m in range(12):
        c = ws.cell(row=row_idx, column=m + 3, value=month_labels_lower[m])
        c.font = _HDR_FONT
        c.fill = _HDR_FILL
        c.alignment = Alignment(horizontal="center")
    row_idx += 1

    for label, metric in [("Venta", "NETO"), ("Aporte", "APORTE")]:
        ws.cell(row=row_idx, column=2, value=label).font = _CANAL_FONT
        total_real = 0.0
        total_bu = 0.0
        for m in range(1, 13):
            real_val = df_et[df_et["MES"] == m][metric].sum()
            bu_val = 0
            if df_budget_etail is not None and not df_budget_etail.empty:
                bu_val = df_budget_etail.loc[df_budget_etail["MES"] == m, metric].sum()
            total_real += real_val
            total_bu += bu_val
            cumpl = real_val / bu_val if bu_val > 0 else 0

            c = ws.cell(row=row_idx, column=m + 2, value=cumpl)
            c.border = _THIN_BORDER
            c.number_format = _PCT_FMT

        ws.cell(row=row_idx, column=15,
                value=total_real / total_bu if total_bu > 0 else 0).number_format = _PCT_FMT
        row_idx += 1

    # Column widths
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 22
    for col_idx in range(3, 16):
        ws.column_dimensions[get_column_letter(col_idx)].width = 16


def _generate_excel(metrics, df_detail, df_budget_etail, current_month):
    """Generate the complete 4-sheet Excel workbook.

    Returns BytesIO buffer ready for download.
    """
    wb = Workbook()

    # Sheet 1: PERU (consolidated)
    _write_peru_sheet(wb, metrics, current_month)

    # Sheet 2: Retail Peru
    df_retail = df_detail[df_detail["CANAL"] == "Retail"]
    if not df_retail.empty:
        _write_detail_sheet(wb, "Retail Peru", "Retail", df_retail, current_month)

    # Sheet 3: Wholesale Peru
    df_whs = df_detail[df_detail["CANAL"] == "Mayorista"]
    if not df_whs.empty:
        _write_detail_sheet(wb, "Wholesale Peru", "Mayorista", df_whs, current_month)

    # Sheet 4: Etail Peru
    _write_etail_sheet(wb, df_detail, df_budget_etail, current_month)

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


# =========================================================================
# 6. Main render function
# =========================================================================

def render_ventas_forecast_210(conn):
    """Main entry point for the Ventas Forecast 2+10 module."""
    st.html("<h2 class='sub-header'>Ventas Forecast 2+10</h2>")

    current_month = datetime.now().month
    current_year = datetime.now().year
    n_real = current_month - 1
    n_fcst = 12 - n_real

    st.info(
        f"**Reporte {n_real}+{n_fcst}** — "
        f"{'ENE' if n_real >= 1 else ''}"
        f"{'-' + MESES_ES[n_real - 1] if n_real >= 1 else ''} Real (VCM) | "
        f"{MESES_ES[current_month - 1]} Forecast | "
        f"{MESES_ES[current_month] if current_month < 12 else ''}"
        f"{'-DIC' if current_month < 12 else ''} Budget"
    )

    # ── File uploaders ──
    st.subheader("Archivos de Input")
    c1, c2 = st.columns(2)
    with c1:
        f_retail = st.file_uploader(
            f"FCST Retail ({MESES_ES[current_month - 1]} {current_year})",
            type=["xlsx"], key="fcst210_retail",
        )
        f_whs = st.file_uploader(
            f"FCST Wholesale ({MESES_ES[current_month - 1]} {current_year})",
            type=["xlsx"], key="fcst210_whs",
        )
    with c2:
        f_etail = st.file_uploader(
            f"FCST Etail ({MESES_ES[current_month - 1]} {current_year})",
            type=["xlsx"], key="fcst210_etail",
        )
        f_budget = st.file_uploader(
            "Budget Anual",
            type=["xlsx"], key="fcst210_budget",
        )

    # ── Generate button ──
    if st.button("Generar Reporte", type="primary", use_container_width=True):
        # Validate at least 1 file
        if not any([f_retail, f_whs, f_etail]):
            st.warning("Sube al menos un archivo FCST para el mes actual.")
            return

        try:
            with st.spinner("Procesando..."):
                # 1. Load VCM real
                df_vcm = _load_vcm_real(conn)
                if df_vcm.empty:
                    st.warning("No se encontraron ventas reales (VCM) para el año actual.")

                # 2. Parse FCST files
                df_fcst_retail = _parse_fcst_retail(f_retail) if f_retail else pd.DataFrame()
                df_fcst_whs = _parse_fcst_whs(f_whs) if f_whs else pd.DataFrame()
                df_fcst_etail = _parse_fcst_etail(f_etail) if f_etail else pd.DataFrame()

                # 3. Parse Budget
                df_budget = _parse_budget(f_budget) if f_budget else pd.DataFrame()

                # 4. Build combined data
                df_detail, df_consolidated = _build_combined_data(
                    df_vcm, df_fcst_retail, df_fcst_whs, df_fcst_etail,
                    df_budget, current_month,
                )

                if df_detail.empty:
                    st.error("No se generaron datos. Verifica los archivos de input.")
                    return

                # 5. Build pivot for consolidated view
                metrics = _pivot_consolidated(df_consolidated)

                # Budget etail for Etail sheet
                df_budget_etail = None
                if df_budget is not None and not df_budget.empty:
                    df_budget_etail = df_budget[df_budget["CANAL"] == "Etail"]

                # Store in session state
                st.session_state["f210_detail"] = df_detail
                st.session_state["f210_consolidated"] = df_consolidated
                st.session_state["f210_metrics"] = metrics
                st.session_state["f210_budget_etail"] = df_budget_etail
                st.session_state["f210_current_month"] = current_month
                st.session_state["f210_ready"] = True

        except Exception as e:
            st.error(f"Error procesando: {e}")
            import traceback
            st.code(traceback.format_exc())
            return

    # ── Display results if ready ──
    if not st.session_state.get("f210_ready"):
        return

    df_detail = st.session_state["f210_detail"]
    df_consolidated = st.session_state["f210_consolidated"]
    metrics = st.session_state["f210_metrics"]
    df_budget_etail = st.session_state.get("f210_budget_etail")
    cm = st.session_state["f210_current_month"]

    # ── KPIs ──
    st.markdown("---")
    k1, k2, k3, k4 = st.columns(4)
    neta_ytd = sum(metrics["Neta"]["Totales"][m] for m in range(1, cm + 1))
    aporte_ytd = sum(metrics["Aporte"]["Totales"][m] for m in range(1, cm + 1))
    neta_anual = sum(metrics["Neta"]["Totales"][m] for m in range(1, 13))
    aporte_anual = sum(metrics["Aporte"]["Totales"][m] for m in range(1, 13))

    k1.metric("Neta YTD", f"S/ {neta_ytd:,.0f}")
    k2.metric("Aporte YTD", f"S/ {aporte_ytd:,.0f}")
    k3.metric("Neta Anual Proy.", f"S/ {neta_anual:,.0f}")
    k4.metric("Margen Anual", f"{aporte_anual / neta_anual:.1%}" if neta_anual > 0 else "0%")

    # ── Tabs ──
    tab_peru, tab_retail, tab_whs, tab_etail = st.tabs([
        "PERU (Consolidado)",
        "Retail Peru",
        "Wholesale Peru",
        "Etail Peru",
    ])

    with tab_peru:
        _display_consolidated_table(metrics, cm)

    with tab_retail:
        _display_detail_table(df_detail, "Retail", cm)

    with tab_whs:
        _display_detail_table(df_detail, "Mayorista", cm)

    with tab_etail:
        _display_etail_table(df_detail, df_budget_etail, cm)

    # ── Excel download ──
    st.markdown("---")
    excel_buffer = _generate_excel(metrics, df_detail, df_budget_etail, cm)
    n_real_dl = cm - 1
    n_fcst_dl = 12 - n_real_dl
    st.download_button(
        label="Descargar Excel",
        data=excel_buffer.getvalue(),
        file_name=f"DJPeru - Ventas Forecast {n_real_dl} + {n_fcst_dl}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        type="primary",
    )
