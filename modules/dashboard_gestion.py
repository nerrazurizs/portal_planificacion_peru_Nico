"""
Dashboard de Gestión de Planificación — vista mensual por SKU con pivot.
Carga datos desde proy_result.parquet (módulo Proyección Stock).
No requiere conexión a Snowflake.
"""

import calendar
import io
from datetime import date

import numpy as np
import pandas as pd
import streamlit as st
from openpyxl.utils import get_column_letter
try:
    from streamlit_sortables import sort_items as _sort_items
    _HAS_SORTABLES = True
except ImportError:
    _HAS_SORTABLES = False
    def _sort_items(items, **_):  # type: ignore[misc]
        return items

from config import apply_pm_filter
from utils.export import download_buttons
from utils.ui_animations import show_empty_state, lottie_spinner


MESES_ES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}

_DIM_COLS = ["SKU_PRODUCTO", "DESCRIPCION", "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL"]

# Internal name → display label (usado para renombrar antes del pivot)
_METRIC_LABELS = {
    "STOCK_ACTUAL":         "Stock Actual",
    "STOCK_PROYECTADO":     "Stock Proyectado",
    "VTA_UNDS_ACTUAL":      "Vta Unds Actual",
    "VTA_UNDS_PRY_CALC":    "Vta Unds Pry Calculo",
    "VTA_UNDS_PRY_SYNCRO":  "Vta Unds Pry Syncro",
    "VTA_UNDS_AA":          "Vta Unds AA",
    "MOI_ACT":              "MOI Act",
    "MOI_PRY":              "MOI Pry",
    "ETA_PENDIENTE":        "ETA Pendiente Recep",
    "FC_COMPRA":            "Forecast Compra",
    "NETA_ACTUAL":          "Neta Actual",
    "NETA_PRY_CALC":        "Neta Proyectado",
    "NETA_AA":              "Neta AA",
    "NETA_PRY_SYNCRO":      "Neta Pry Syncro",
    "APORTE_ACTUAL":        "Aporte Actual",
    "APORTE_PRY_CALC":      "Aporte Proyectado",
    "APORTE_AA":            "Aporte AA",
    "APORTE_PRY_SYNCRO":    "Aporte Pry Syncro",
    "MRG_ACT":              "Mrg% Actual",
    "MRG_PRY_CALC":         "Mrg% Proyectado",
    "MRG_AA":               "Mrg% AA",
    "MRG_PRY_SYNCRO":       "Mrg% Pry Syncro",
}

_METRIC_COLS = list(_METRIC_LABELS.keys())

# Columnas que se agregan con mean en el pivot (ratios)
_MEAN_METRICS = {"MOI_ACT", "MOI_PRY", "MRG_ACT", "MRG_PRY_CALC", "MRG_PRY_SYNCRO", "MRG_AA"}

_NUMBER_FORMAT = {
    "STOCK_ACTUAL":        "#,##0",
    "STOCK_PROYECTADO":    "#,##0.0",
    "VTA_UNDS_ACTUAL":     "#,##0",
    "VTA_UNDS_PRY_CALC":   "#,##0.0",
    "VTA_UNDS_PRY_SYNCRO": "#,##0",
    "MOI_ACT":             "#,##0.00",
    "MOI_PRY":             "#,##0.00",
    "ETA_PENDIENTE":       "#,##0",
    "FC_COMPRA":           "#,##0",
    "NETA_ACTUAL":         "S/ #,##0.00",
    "NETA_PRY_CALC":       "S/ #,##0.00",
    "NETA_AA":             "S/ #,##0.00",
    "NETA_PRY_SYNCRO":     "S/ #,##0",
    "APORTE_ACTUAL":       "S/ #,##0.00",
    "APORTE_PRY_CALC":     "S/ #,##0.00",
    "APORTE_AA":           "S/ #,##0.00",
    "APORTE_PRY_SYNCRO":   "S/ #,##0",
    "MRG_ACT":             "0.00%",
    "MRG_PRY_CALC":        "0.00%",
    "MRG_AA":              "0.00%",
    "MRG_PRY_SYNCRO":      "0.00%",
}

# Formatos para Excel (col display label → openpyxl number format)
# Mrg% ya viene ×100 en df_display → usamos "0.00\%" (no Excel %)
_EXCEL_NUM_FMTS: dict[str, str] = {
    "Vta Unds Pry Syncro":  "#,##0",
    "MOI Act":              "#,##0.00",
    "MOI Pry":              "#,##0.00",
    "Neta Actual":          r'"S/ "#,##0.00',
    "Neta Proyectado":      r'"S/ "#,##0.00',
    "Neta AA":              r'"S/ "#,##0.00',
    "Neta Pry Syncro":      r'"S/ "#,##0',
    "Aporte Actual":        r'"S/ "#,##0.00',
    "Aporte Proyectado":    r'"S/ "#,##0.00',
    "Aporte AA":            r'"S/ "#,##0.00',
    "Aporte Pry Syncro":    r'"S/ "#,##0',
    "Mrg% Actual":          r'0.00\%',
    "Mrg% Proyectado":      r'0.00\%',
    "Mrg% AA":              r'0.00\%',
    "Mrg% Pry Syncro":      r'0.00\%',
}


# ─── Diagnóstico ───────────────────────────────────────────────────────────────

def _fmt_soles(v: float) -> str:
    """Format a soles value compactly: S/ 1.2M, S/ 45.3K, S/ 123."""
    av = abs(v)
    if av >= 1_000_000:
        return f"S/ {av/1_000_000:.1f}M"
    if av >= 1_000:
        return f"S/ {av/1_000:.1f}K"
    return f"S/ {av:,.0f}"


def _build_diag(
    stock, eta, fc, moi,
    vta_c, vta_s, vta_act, vta_aa,
    neta_act, neta_aa,
    mrg_act, mrg_aa,            # mrg en fracción (0–1)
    neta_pry_calc: float = 0.0,  # neta proyectada al cierre del mes (precio_prom × vta_c)
    vta_aa_full: float = 0.0,    # unidades AA mes completo (sin prorrateo)
    neta_aa_full: float = 0.0,   # neta AA mes completo (sin prorrateo)
    stock_quiebre: bool = True,
) -> str:
    """Lógica compartida de diagnóstico + proyección de impacto para SKU y grupo."""
    diags: list[str] = []

    # ── Quiebre ────────────────────────────────────────────────────────────────
    if stock_quiebre and stock <= 0:
        return "Quiebre c/repos" if (eta > 0 or fc > 0) else "Quiebre"

    # ── Stock / rotación ───────────────────────────────────────────────────────
    if pd.notna(moi):
        if moi > 12:
            diags.append("Baja Rotacion")
        elif moi > 6:
            diags.append("Sobrestock")

    supply = eta + fc
    if supply > 0 and supply / max(vta_c, 0.01) > 3:
        diags.append("Exceso Compra")

    if vta_c > 1 and abs(vta_s - vta_c) / vta_c > 0.30:
        diags.append("Desv Sync/Calc")

    # ── Comparativos AA (alertas) ──────────────────────────────────────────────
    if vta_aa > 0.5 and vta_act < vta_aa * 0.80:
        diags.append("Caida Vta AA")

    if neta_aa > 0 and neta_act < neta_aa * 0.80:
        diags.append("Caida Neta AA")

    if pd.notna(mrg_act) and pd.notna(mrg_aa) and mrg_aa > 0:
        if mrg_act < mrg_aa - 0.05:
            diags.append("Mrg% < AA")

    # ── Proyección al cierre del mes vs mismo mes AA ──────────────────────────
    # "Cierre proy vs Jun-25: -351 unds y -S/ 36.8K"
    # vta_c = unidades proyectadas para el mes completo (al ritmo actual)
    # vta_aa_full = unidades reales del mismo mes año anterior (mes completo)
    _hoy     = date.today()
    _mes_lbl = f"{MESES_ES.get(_hoy.month,'')}-{str(_hoy.year-1)[2:]}"
    impact_parts: list[str] = []

    if vta_aa_full > 0.5:
        delta_unds = vta_c - vta_aa_full
        if abs(delta_unds) >= 1:
            sign = "+" if delta_unds >= 0 else ""
            impact_parts.append(f"{sign}{delta_unds:,.0f} unds")

    if neta_aa_full > 0:
        delta_neta = neta_pry_calc - neta_aa_full
        if abs(delta_neta) >= 1:
            sign = "+" if delta_neta >= 0 else ""
            impact_parts.append(f"{sign}{_fmt_soles(delta_neta)}")

    parts: list[str] = [" · ".join(diags) if diags else "OK"]
    if impact_parts:
        parts.append(
            f"Cierre proy vs {_mes_lbl}: " + " y ".join(impact_parts)
        )
    return " | ".join(parts)


def _build_sugerido(
    stock, eta, fc, moi,
    vta_c, vta_s, vta_act, vta_aa,
    neta_act, neta_aa,
    mrg_act, mrg_aa,            # fracción 0–1
    neta_pry_calc: float = 0.0,
    vta_aa_full: float  = 0.0,
    neta_aa_full: float = 0.0,
) -> str:
    """Genera una recomendacion accionable de gestion."""

    # Banderas base
    quiebre       = stock <= 0
    tiene_repos   = (eta > 0 or fc > 0)
    moi_bajo      = pd.notna(moi) and moi < 2
    moi_ok        = pd.notna(moi) and moi <= 6
    sobrestock    = pd.notna(moi) and 6 < moi <= 12
    baja_rot      = pd.notna(moi) and moi > 12
    exceso_compra = fc > 0 and max(vta_c, 0.01) > 0 and (eta + fc) / max(vta_c, 0.01) > 3
    desv_sync     = vta_c > 1 and abs(vta_s - vta_c) / vta_c > 0.30
    caida_vta_aa  = vta_aa > 0.5 and vta_act < vta_aa * 0.80
    caida_neta_aa = neta_aa > 0 and neta_act < neta_aa * 0.80
    mrg_sobre_aa  = (pd.notna(mrg_act) and pd.notna(mrg_aa)
                     and mrg_aa > 0 and mrg_act > mrg_aa + 0.03)  # >3 pp sobre AA
    mrg_bajo_aa   = (pd.notna(mrg_act) and pd.notna(mrg_aa)
                     and mrg_aa > 0 and mrg_act < mrg_aa - 0.05)  # >5 pp bajo AA
    gana_vs_aa    = (vta_aa_full > 0.5 and vta_c > vta_aa_full * 1.05)

    # ── Prioridades de sugerido (de más a menos urgente) ──────────────────────

    # 1. Quiebre
    if quiebre:
        if tiene_repos:
            return "Acelerar recepcion ETA/FC — producto sin stock con reposicion en camino"
        return "Reponer urgente — sin stock y sin OC en curso, riesgo de venta perdida"

    # 2. Stock crítico bajo
    if moi_bajo:
        if mrg_bajo_aa:
            return "No aplicar descuentos — stock critico y margen ya deteriorado vs AA"
        return "Mantener precio — stock insuficiente; no arriesgar con promocion"

    # 3. Exceso de compra
    if exceso_compra:
        return ("Revisar y pausar proximas OCs — cobertura supera 3 meses de venta proyectada; "
                "evaluar cancelar o posponer")

    # 4. Baja Rotación
    if baja_rot:
        if mrg_sobre_aa and caida_vta_aa:
            return ("Activar promo/descuento — margen sobre AA da espacio para ceder "
                    "rentabilidad y recuperar rotacion perdida vs AA")
        if mrg_sobre_aa:
            return ("Considerar oferta o bundle — alto margen vs AA permite sacrificar "
                    "margen para reactivar rotacion")
        if caida_vta_aa:
            return ("Revisar visibilidad, precio y canal — ventas cayendo vs AA con "
                    "margen ya ajustado; evaluar liquidacion selectiva")
        return ("Evaluar clearance o redistribucion — MOI muy alto sin caida AA marcada; "
                "revisar surtido activo")

    # 5. Sobrestock
    if sobrestock:
        if mrg_sobre_aa and caida_vta_aa:
            return ("Activar descuento focalizados — margen sobre AA da margen de maniobra "
                    "para promover y atacar la caida vs AA")
        if mrg_sobre_aa:
            return ("Considerar promo selectiva — margen sobre AA permite ceder sin comprometer "
                    "rentabilidad general")
        if caida_vta_aa:
            return "Accion comercial para recuperar ritmo vs AA y reducir cobertura de inventario"
        if exceso_compra or (eta + fc) > vta_c * 1.5:
            return ("Revisar plan de compras — nuevo stock en camino agravara el sobrestock; "
                    "evaluar pausar OCs")
        return "Monitorear MOI — evaluar si hay aceleracion estacional esperada"

    # 6. Desviacion Syncro/Calc
    if desv_sync:
        if vta_act > vta_s * 1.30:
            return ("Actualizar forecast syncro — venta supera proyeccion >30%; "
                    "asegurar stock suficiente para el ritmo real")
        return ("Revisar coherencia de forecast syncro — venta por debajo de proyeccion; "
                "ajustar o escalar alerta al equipo de forecast")

    # 7. Caida neta sin caida unidades (precio/mix deteriorado)
    if caida_neta_aa and not caida_vta_aa:
        return ("Revisar estructura de precios y mix de canales — neta cae vs AA con "
                "volumen similar; posible erosion de precio o cambio de mix")

    # 8. Margen deteriorado
    if mrg_bajo_aa:
        if caida_vta_aa:
            return ("Revisar descuentos y costo — margen deteriorado con ventas caidas; "
                    "doble problema de precio y volumen")
        return ("Auditar descuentos y costo unitario — margen por debajo del AA sin "
                "compensacion en volumen")

    # 9. Caida venta AA con margen alto (precio inhibe demanda)
    if caida_vta_aa and mrg_sobre_aa:
        return ("Evaluar ajuste de precio — margen sobre AA puede estar inhibiendo la "
                "demanda; una reduccion controlada puede recuperar volumen")

    # 10. Sin alertas
    if gana_vs_aa:
        return "Mantener estrategia actual — superando performance del AA en volumen y neta"
    return "Sin alertas criticas — mantener seguimiento mensual"


def _calc_diagnostico(row) -> str:
    """Diagnóstico por SKU — usa nombres internos de columna."""
    return _build_diag(
        stock         = row["STOCK_ACTUAL"],
        eta           = row["ETA_PENDIENTE"],
        fc            = row["FC_COMPRA"],
        moi           = row["MOI_ACT"],
        vta_c         = row["VTA_UNDS_PRY_CALC"],
        vta_s         = row["VTA_UNDS_PRY_SYNCRO"],
        vta_act       = row["VTA_UNDS_ACTUAL"],
        vta_aa        = row["VTA_UNDS_AA"],
        neta_act      = row["NETA_ACTUAL"],
        neta_aa       = row["NETA_AA"],
        mrg_act       = row["MRG_ACT"],
        mrg_aa        = row["MRG_AA"],
        neta_pry_calc = row["NETA_PRY_CALC"],
        vta_aa_full   = row.get("_VTA_AA_FULL",  0),
        neta_aa_full  = row.get("_NETA_AA_FULL", 0),
    )


def _calc_diag_group(row) -> str:
    """Diagnóstico para filas agrupadas — usa nombres de display label."""
    mrg_act_raw = row.get("Mrg% Actual", np.nan)
    mrg_aa_raw  = row.get("Mrg% AA",     np.nan)
    mrg_act = mrg_act_raw / 100 if pd.notna(mrg_act_raw) else np.nan
    mrg_aa  = mrg_aa_raw  / 100 if pd.notna(mrg_aa_raw)  else np.nan
    return _build_diag(
        stock=row.get("Stock Actual",0), eta=row.get("ETA Pendiente Recep",0),
        fc=row.get("Forecast Compra",0), moi=row.get("MOI Act",np.nan),
        vta_c=row.get("Vta Unds Pry Calculo",0), vta_s=row.get("Vta Unds Pry Syncro",0),
        vta_act=row.get("Vta Unds Actual",0), vta_aa=row.get("Vta Unds AA",0),
        neta_act=row.get("Neta Actual",0), neta_aa=row.get("Neta AA",0),
        mrg_act=mrg_act, mrg_aa=mrg_aa,
        neta_pry_calc=row.get("Neta Proyectado",0),
        vta_aa_full=row.get("_VTA_AA_FULL",0), neta_aa_full=row.get("_NETA_AA_FULL",0),
    )


def _calc_sugerido(row) -> str:
    """Sugerido de gestión por SKU — usa nombres internos."""
    return _build_sugerido(
        stock=row["STOCK_ACTUAL"], eta=row["ETA_PENDIENTE"], fc=row["FC_COMPRA"],
        moi=row["MOI_ACT"], vta_c=row["VTA_UNDS_PRY_CALC"],
        vta_s=row["VTA_UNDS_PRY_SYNCRO"], vta_act=row["VTA_UNDS_ACTUAL"],
        vta_aa=row["VTA_UNDS_AA"], neta_act=row["NETA_ACTUAL"], neta_aa=row["NETA_AA"],
        mrg_act=row["MRG_ACT"], mrg_aa=row["MRG_AA"],
        neta_pry_calc=row["NETA_PRY_CALC"],
        vta_aa_full=row.get("_VTA_AA_FULL",0), neta_aa_full=row.get("_NETA_AA_FULL",0),
    )


def _calc_sugerido_group(row) -> str:
    """Sugerido de gestión para filas agrupadas — usa display labels."""
    mrg_act_raw = row.get("Mrg% Actual", np.nan)
    mrg_aa_raw  = row.get("Mrg% AA",     np.nan)
    mrg_act = mrg_act_raw / 100 if pd.notna(mrg_act_raw) else np.nan
    mrg_aa  = mrg_aa_raw  / 100 if pd.notna(mrg_aa_raw)  else np.nan
    return _build_sugerido(
        stock=row.get("Stock Actual",0), eta=row.get("ETA Pendiente Recep",0),
        fc=row.get("Forecast Compra",0), moi=row.get("MOI Act",np.nan),
        vta_c=row.get("Vta Unds Pry Calculo",0), vta_s=row.get("Vta Unds Pry Syncro",0),
        vta_act=row.get("Vta Unds Actual",0), vta_aa=row.get("Vta Unds AA",0),
        neta_act=row.get("Neta Actual",0), neta_aa=row.get("Neta AA",0),
        mrg_act=mrg_act, mrg_aa=mrg_aa,
        neta_pry_calc=row.get("Neta Proyectado",0),
        vta_aa_full=row.get("_VTA_AA_FULL",0), neta_aa_full=row.get("_NETA_AA_FULL",0),
    )


# ─── Data helpers ──────────────────────────────────────────────────────────────

def _load_proy() -> pd.DataFrame | None:
    """Load proy_result from session state (Proyeccion Stock) or disk."""
    df = st.session_state.get("df_proy")
    if df is not None and not df.empty:
        return df
    try:
        return pd.read_parquet("data/inputs/proy_result.parquet")
    except Exception:
        return None


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_vta_actuals(_conn) -> pd.DataFrame:
    """Query FT_VCM for MTD actual units, net revenue and margin per SKU."""
    from db.queries import _VCM
    q = f"""
    SELECT
        a.sku_producto                      AS SKU_PRODUCTO,
        SUM(a.cantidad)                     AS VTA_UNDS_ACTUAL_SF,
        SUM(a.neto)                         AS NETA_ACTUAL_SF,
        SUM(a.neto - a.costo)               AS APORTE_ACTUAL_SF
    FROM {_VCM} a
    WHERE a.cantidad > 0
      AND a.fecha >= date_trunc('month', current_date())
      AND a.fecha <= current_date()
    GROUP BY a.sku_producto
    """
    df = pd.read_sql(q, _conn)
    df.columns = [c.upper() for c in df.columns]
    return df


def _build_dashboard(
    df: pd.DataFrame,
    df_sf_vta: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict]:
    """
    Build per-SKU management metrics for the current month.
    Returns (result_df, meta) where meta holds date context strings.
    """
    hoy = date.today()
    año = hoy.year
    mes = hoy.month
    dias_avance = max(hoy.day, 1)
    dias_mes = calendar.monthrange(año, mes)[1]

    # ── Current month (partial real + forecast) ────────────────────────────────
    cur = df[
        (df["PERIODO_ANO"] == año) &
        (df["PERIODO_MES"] == mes) &
        (df["TIPO_DATO"] == "REAL+FC")
    ].copy()

    if cur.empty:
        return pd.DataFrame(), {}

    # ── Closed months average sales (HISTORICO, same year, before current) ─────
    hist = df[
        (df["PERIODO_ANO"] == año) &
        (df["PERIODO_MES"] < mes) &
        (df["TIPO_DATO"] == "HISTORICO")
    ].copy()

    n_meses = max(hist["PERIODO_MES"].nunique(), 1)
    hist["_VTA"] = (
        hist["VENTA_FUL_TIENDA_UND"].fillna(0)
        + hist["VENTA_FUL_ETAIL_UND"].fillna(0)
        + hist["VENTA_FUL_MAYOR_UND"].fillna(0)
    )
    avg_vta = (hist.groupby("SKU_PRODUCTO")["_VTA"].sum() / n_meses).rename("_AVG_VTA")
    cur = cur.merge(avg_vta, on="SKU_PRODUCTO", how="left")
    cur["_AVG_VTA"] = cur["_AVG_VTA"].fillna(0)

    # ── Base actuals: proy_result (fallback) ──────────────────────────────────
    cur["VTA_UNDS_ACTUAL"] = (
        cur["VTA_ACTUAL_TIENDA"].fillna(0)
        + cur["VTA_ACTUAL_ETAIL"].fillna(0)
        + cur["VTA_ACTUAL_MAYOR"].fillna(0)
    )
    cur["NETA_ACTUAL"] = (
        cur["VN_ACTUAL_TIENDA"].fillna(0)
        + cur["VN_ACTUAL_ETAIL"].fillna(0)
        + cur["VN_ACTUAL_MAYOR"].fillna(0)
    )
    cur["APORTE_ACTUAL"] = (
        cur["APORTE_ACTUAL_TIENDA"].fillna(0)
        + cur["APORTE_ACTUAL_ETAIL"].fillna(0)
        + cur["APORTE_ACTUAL_MAYOR"].fillna(0)
    )

    # ── Override actuals with Snowflake FT_VCM when available ─────────────────
    if df_sf_vta is not None and not df_sf_vta.empty:
        cur = cur.merge(df_sf_vta, on="SKU_PRODUCTO", how="left")
        sf_mask = cur["VTA_UNDS_ACTUAL_SF"].notna()
        cur.loc[sf_mask, "VTA_UNDS_ACTUAL"] = cur.loc[sf_mask, "VTA_UNDS_ACTUAL_SF"]
        cur.loc[sf_mask, "NETA_ACTUAL"]     = cur.loc[sf_mask, "NETA_ACTUAL_SF"]
        cur.loc[sf_mask, "APORTE_ACTUAL"]   = cur.loc[sf_mask, "APORTE_ACTUAL_SF"]
        cur = cur.drop(
            columns=["VTA_UNDS_ACTUAL_SF", "NETA_ACTUAL_SF", "APORTE_ACTUAL_SF"],
            errors="ignore",
        )

    # ── Derived ventas metrics (use final actuals) ─────────────────────────────
    cur["VTA_UNDS_PRY_CALC"] = cur["VTA_UNDS_ACTUAL"] * dias_mes / dias_avance
    # Syncro = forecast mensual prorateado al día de hoy
    cur["VTA_UNDS_PRY_SYNCRO"] = (
        cur["VENTA_FUL_TIENDA_UND"].fillna(0)
        + cur["VENTA_FUL_ETAIL_UND"].fillna(0)
        + cur["VENTA_FUL_MAYOR_UND"].fillna(0)
    ) * dias_avance / dias_mes

    # ── Stock ──────────────────────────────────────────────────────────────────
    cur["STOCK_ACTUAL"] = cur["STOCK_INICIAL_TOTAL"].fillna(0)
    # Stock proyectado al cierre del mes:
    # Stock Actual ya tiene descontadas las ventas de los días transcurridos,
    # por eso solo se restan las ventas RESTANTES del mes (Pry Calculo - ya vendido).
    cur["STOCK_PROYECTADO"] = (
        cur["STOCK_ACTUAL"]
        + cur["ETA"].fillna(0)
        + cur["FORECAST_COMPRA"].fillna(0)
        - (cur["VTA_UNDS_PRY_CALC"] - cur["VTA_UNDS_ACTUAL"])
    )

    # ── MOI ────────────────────────────────────────────────────────────────────
    avg = cur["_AVG_VTA"]
    # MOI Act = Stock Actual / promedio mensual (Ene–May)
    cur["MOI_ACT"] = np.where(avg > 0, cur["STOCK_ACTUAL"] / avg, np.nan)

    # MOI Pry = Stock Proyectado / nuevo promedio que incluye el mes actual proyectado
    # nuevo_avg = (sum_Ene-May + Vta_Pry_Calc) / (n_meses_cerrados + 1)
    #           = (avg * n_meses + Vta_Pry_Calc) / (n_meses + 1)
    nuevo_avg = (avg * n_meses + cur["VTA_UNDS_PRY_CALC"]) / (n_meses + 1)
    cur["MOI_PRY"] = np.where(nuevo_avg > 0, cur["STOCK_PROYECTADO"] / nuevo_avg, np.nan)

    # ── ETA y Forecast Compra ──────────────────────────────────────────────────
    cur["ETA_PENDIENTE"] = cur["ETA"].fillna(0)
    cur["FC_COMPRA"] = cur["FORECAST_COMPRA"].fillna(0)

    # ── Neta derivada ──────────────────────────────────────────────────────────
    vta_act = cur["VTA_UNDS_ACTUAL"]
    precio_prom = np.where(vta_act > 0, cur["NETA_ACTUAL"] / vta_act, 0)
    cur["NETA_PRY_CALC"] = precio_prom * cur["VTA_UNDS_PRY_CALC"]
    cur["NETA_PRY_SYNCRO"] = cur["VN_RES_TOTAL"].fillna(0)

    # ── Aporte derivado ────────────────────────────────────────────────────────
    cur["APORTE_PRY_CALC"] = (
        cur["NETA_PRY_CALC"]
        - cur["VTA_UNDS_PRY_CALC"] * cur["COSTO_UNITARIO"].fillna(0)
    )
    cur["APORTE_PRY_SYNCRO"] = cur["APORTE_RES_TOTAL"].fillna(0)

    # ── Márgenes ───────────────────────────────────────────────────────────────
    cur["MRG_ACT"] = np.where(
        cur["NETA_ACTUAL"] > 0, cur["APORTE_ACTUAL"] / cur["NETA_ACTUAL"], np.nan
    )
    cur["MRG_PRY_CALC"] = np.where(
        cur["NETA_PRY_CALC"] > 0, cur["APORTE_PRY_CALC"] / cur["NETA_PRY_CALC"], np.nan
    )
    cur["MRG_PRY_SYNCRO"] = np.where(
        cur["NETA_PRY_SYNCRO"] > 0, cur["APORTE_PRY_SYNCRO"] / cur["NETA_PRY_SYNCRO"], np.nan
    )

    # ── Año Anterior (AA): mismo mes del año previo, proporcional a dias_avance ─
    año_ant = año - 1
    dias_mes_ant = calendar.monthrange(año_ant, mes)[1]
    peso_aa = dias_avance / dias_mes_ant          # escalar mes completo → días avanzados

    hist_aa = df[
        (df["PERIODO_ANO"] == año_ant) &
        (df["PERIODO_MES"] == mes) &
        (df["TIPO_DATO"] == "HISTORICO")
    ].copy()

    if not hist_aa.empty:
        hist_aa = hist_aa.set_index("SKU_PRODUCTO")
        hist_aa["_VTA_AA"] = (
            hist_aa["VENTA_FUL_TIENDA_UND"].fillna(0)
            + hist_aa["VENTA_FUL_ETAIL_UND"].fillna(0)
            + hist_aa["VENTA_FUL_MAYOR_UND"].fillna(0)
        )
        # Valores proporcionales a días avanzados (para comparar MTD vs MTD)
        cur["VTA_UNDS_AA"] = cur["SKU_PRODUCTO"].map(
            hist_aa["_VTA_AA"] * peso_aa
        ).fillna(0).round(0)
        cur["NETA_AA"]     = cur["SKU_PRODUCTO"].map(
            hist_aa["VN_RES_TOTAL"].fillna(0) * peso_aa
        ).fillna(0)
        cur["APORTE_AA"]   = cur["SKU_PRODUCTO"].map(
            hist_aa["APORTE_RES_TOTAL"].fillna(0) * peso_aa
        ).fillna(0)
        # Mes completo AA (sin prorrateo) — para proyectar cierre del mes
        cur["_VTA_AA_FULL"]  = cur["SKU_PRODUCTO"].map(hist_aa["_VTA_AA"]).fillna(0)
        cur["_NETA_AA_FULL"] = cur["SKU_PRODUCTO"].map(
            hist_aa["VN_RES_TOTAL"].fillna(0)
        ).fillna(0)
    else:
        cur["VTA_UNDS_AA"]   = 0.0
        cur["NETA_AA"]       = 0.0
        cur["APORTE_AA"]     = 0.0
        cur["_VTA_AA_FULL"]  = 0.0
        cur["_NETA_AA_FULL"] = 0.0

    cur["MRG_AA"] = np.where(
        cur["NETA_AA"] > 0, cur["APORTE_AA"] / cur["NETA_AA"], np.nan
    )

    # ── Rename SKU_NOM_PRODUCTO → DESCRIPCION for display ─────────────────────
    cur = cur.rename(columns={"SKU_NOM_PRODUCTO": "DESCRIPCION"})
    if "MIX_OFICIAL" not in cur.columns:
        cur["MIX_OFICIAL"] = ""

    cur["DIAGNOSTICO"] = cur.apply(_calc_diagnostico, axis=1)
    cur["SUGERIDO"]    = cur.apply(_calc_sugerido,    axis=1)

    result = cur[
        _DIM_COLS + _METRIC_COLS
        + ["DIAGNOSTICO", "SUGERIDO", "_VTA_AA_FULL", "_NETA_AA_FULL", "_AVG_VTA"]
    ].copy()

    mes_ant_label = MESES_ES.get(mes - 1, "—") if mes > 1 else "—"
    meta = {
        "hoy": hoy,
        "mes_label": f"{MESES_ES[mes]} {año}",
        "dias_avance": dias_avance,
        "dias_mes": dias_mes,
        "n_meses_cerrados": n_meses,
        "meses_cerrados_label": (
            f"Ene – {mes_ant_label} {año}" if mes > 1 else "Sin meses cerrados"
        ),
    }
    return result, meta


def _excel_autofit(df: pd.DataFrame, sheet_name: str = "Datos") -> io.BytesIO:
    """Build Excel BytesIO with auto-sized columns and number formatting."""
    # ── Sanitize types so openpyxl doesn't choke on mixed/exotic dtypes ────────
    df_safe = df.copy()
    for _col in df_safe.columns:
        if pd.api.types.is_datetime64_any_dtype(df_safe[_col]):
            df_safe[_col] = df_safe[_col].dt.strftime("%Y-%m-%d").fillna("")
        elif pd.api.types.is_bool_dtype(df_safe[_col]):
            df_safe[_col] = df_safe[_col].astype(int)
        elif pd.api.types.is_numeric_dtype(df_safe[_col]):
            df_safe[_col] = df_safe[_col].fillna(0)
        else:
            df_safe[_col] = df_safe[_col].fillna("").astype(str)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_safe.to_excel(writer, index=False, sheet_name=sheet_name)
        ws = writer.sheets[sheet_name]
        n_rows = len(df_safe)

        for col_idx, col_name in enumerate(df_safe.columns, start=1):
            letter = get_column_letter(col_idx)

            # ── Auto-width ──────────────────────────────────────────────────
            header_w = len(str(col_name))
            try:
                data_w = (
                    int(df_safe[col_name].astype(str).str.len().max())
                    if n_rows > 0 else 0
                )
            except (ValueError, TypeError):
                data_w = 0
            ws.column_dimensions[letter].width = min(max(header_w, data_w) + 2, 60)

            # ── Number format per column ────────────────────────────────────
            fmt = _EXCEL_NUM_FMTS.get(col_name)
            if fmt:
                for row_idx in range(2, n_rows + 2):   # fila 1 = cabecera
                    ws.cell(row=row_idx, column=col_idx).number_format = fmt

    buf.seek(0)
    return buf


def _fmt_s(v, prefix="S/ ") -> str:
    if pd.isna(v) or v == 0:
        return "—"
    if abs(v) >= 1_000_000:
        return f"{prefix}{v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"{prefix}{v / 1_000:.1f}K"
    return f"{prefix}{v:,.0f}"


def _kpi_card(label: str, value: str, color: str = "#065E8B") -> str:
    return (
        f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;'
        f'padding:1rem 1.25rem;text-align:center;">'
        f'<div style="font-size:0.72rem;color:#64748b;text-transform:uppercase;'
        f'letter-spacing:0.05em;margin-bottom:0.3rem;">{label}</div>'
        f'<div style="font-size:1.4rem;font-weight:700;color:{color};">{value}</div>'
        f"</div>"
    )


def _calc_diag_stock(moi_act: pd.Series, moi_pry: pd.Series) -> pd.Series:
    """Diagnóstico de stock por SKU basado en MOI actual y MOI proyectado fin de año.

    Rangos MOI_PRY_FN_ANO:
      NaN (VTA = 0)  → 📊 Sin Forecast Asignado
      = 0            → 🚨 Sin Stock
      < 2            → 🔴 Riesgo Quiebre
      2 – 3          → 🟠 Stock Bajo
      3 – 5.5        → ✅ Normal
      5.5 – 8        → 🟡 Stock Alto
      > 8            → 🔵 Sobrestock
    """
    _pry = moi_pry.fillna(moi_act)
    conds = [
        moi_act.isna(),        # VTA_PROY = 0 → sin promedio de venta
        _pry == 0,             # stock proyectado = 0
        _pry < 2.0,
        _pry < 3.0,
        _pry <= 5.5,
        _pry <= 8.0,
    ]
    choices = [
        "📊 Sin Forecast Asignado",
        "🚨 Sin Stock",
        "🔴 Riesgo Quiebre",
        "🟠 Stock Bajo",
        "✅ Normal",
        "🟡 Stock Alto",
    ]
    return pd.Series(
        np.select(conds, choices, default="🔵 Sobrestock"),
        index=moi_act.index,
        dtype="object",
    )


# ─── Cierre de Año ─────────────────────────────────────────────────────────────

def _build_cierre_ano(
    df: pd.DataFrame,
    df_sf_ytd: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Build year-end management table per SKU.
    Columns: YTD actual | AA YTD | Proyeccion Año | AA Año | Variaciones.
    """
    hoy = date.today()
    año, mes = hoy.year, hoy.month
    dias_avance = max(hoy.day, 1)
    año_ant = año - 1
    dias_mes_ant = calendar.monthrange(año_ant, mes)[1]
    peso_aa_mes = dias_avance / dias_mes_ant      # escalar mes AA al avance actual

    _DIMS = ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL"]

    # ── Slices del parquet ─────────────────────────────────────────────────────
    hist_ytd   = df[(df["PERIODO_ANO"] == año)     & (df["PERIODO_MES"] < mes)  & (df["TIPO_DATO"] == "HISTORICO")]
    cur_ytd    = df[(df["PERIODO_ANO"] == año)     & (df["PERIODO_MES"] == mes) & (df["TIPO_DATO"] == "REAL+FC")]
    proy_full  = df[(df["PERIODO_ANO"] == año)     & (df["TIPO_DATO"].isin(["HISTORICO", "REAL+FC", "PROYECCION"]))]
    aa_ytd_past= df[(df["PERIODO_ANO"] == año_ant) & (df["PERIODO_MES"] < mes)  & (df["TIPO_DATO"] == "HISTORICO")]
    aa_ytd_mes = df[(df["PERIODO_ANO"] == año_ant) & (df["PERIODO_MES"] == mes) & (df["TIPO_DATO"] == "HISTORICO")]
    aa_full    = df[(df["PERIODO_ANO"] == año_ant) & (df["TIPO_DATO"] == "HISTORICO")]

    # ── Helper: sum SKU-level metrics ─────────────────────────────────────────
    def _sum(frame, vta_col, neta_col, aporte_col, suffix):
        if frame.empty:
            return pd.DataFrame(columns=["SKU_PRODUCTO",
                                         f"VTA_{suffix}", f"NETA_{suffix}", f"APORTE_{suffix}"])
        return (
            frame.groupby("SKU_PRODUCTO", as_index=False)
            .agg(
                **{f"VTA_{suffix}":    (vta_col,    "sum"),
                   f"NETA_{suffix}":   (neta_col,   "sum"),
                   f"APORTE_{suffix}": (aporte_col, "sum")}
            )
        )

    # ── 1. YTD Jan-May (HISTORICO — actual realizado) ─────────────────────────
    hist_ytd2 = hist_ytd.copy()
    hist_ytd2["_VTA_AUX"] = (hist_ytd2["VENTA_FUL_TIENDA_UND"].fillna(0)
                              + hist_ytd2["VENTA_FUL_ETAIL_UND"].fillna(0)
                              + hist_ytd2["VENTA_FUL_MAYOR_UND"].fillna(0))
    s1 = (hist_ytd2.groupby("SKU_PRODUCTO", as_index=False)
          .agg(VTA_H=("_VTA_AUX", "sum"),
               NETA_H=("VN_RES_TOTAL", "sum"),
               APORTE_H=("APORTE_RES_TOTAL", "sum")))

    # ── 2. YTD Jun actual (REAL+FC — días transcurridos) ─────────────────────
    cur_ytd2 = cur_ytd.copy()
    cur_ytd2["_VTA_C"] = (cur_ytd2["VTA_ACTUAL_TIENDA"].fillna(0)
                          + cur_ytd2["VTA_ACTUAL_ETAIL"].fillna(0)
                          + cur_ytd2["VTA_ACTUAL_MAYOR"].fillna(0))
    cur_ytd2["_NETA_C"] = (cur_ytd2["VN_ACTUAL_TIENDA"].fillna(0)
                           + cur_ytd2["VN_ACTUAL_ETAIL"].fillna(0)
                           + cur_ytd2["VN_ACTUAL_MAYOR"].fillna(0))
    cur_ytd2["_APORTE_C"] = (cur_ytd2["APORTE_ACTUAL_TIENDA"].fillna(0)
                             + cur_ytd2["APORTE_ACTUAL_ETAIL"].fillna(0)
                             + cur_ytd2["APORTE_ACTUAL_MAYOR"].fillna(0))
    s2 = (cur_ytd2.groupby("SKU_PRODUCTO", as_index=False)
          .agg(VTA_C=("_VTA_C", "sum"),
               NETA_C=("_NETA_C", "sum"),
               APORTE_C=("_APORTE_C", "sum")))

    # Override con Snowflake FT_VCM si está disponible (más preciso)
    if df_sf_ytd is not None and not df_sf_ytd.empty:
        s2 = s2.merge(df_sf_ytd, on="SKU_PRODUCTO", how="left")
        sf_mask = s2["VTA_UNDS_ACTUAL_SF"].notna()
        s2.loc[sf_mask, "VTA_C"]    = s2.loc[sf_mask, "VTA_UNDS_ACTUAL_SF"]
        s2.loc[sf_mask, "NETA_C"]   = s2.loc[sf_mask, "NETA_ACTUAL_SF"]
        s2.loc[sf_mask, "APORTE_C"] = s2.loc[sf_mask, "APORTE_ACTUAL_SF"]
        s2 = s2.drop(columns=["VTA_UNDS_ACTUAL_SF", "NETA_ACTUAL_SF", "APORTE_ACTUAL_SF"],
                     errors="ignore")

    # ── 3. Proyección cierre año 2026 (todos los meses, mejor tipo por período) ─
    proy_full2 = proy_full.copy()
    proy_full2["_VTA"] = (proy_full2["VENTA_FUL_TIENDA_UND"].fillna(0)
                          + proy_full2["VENTA_FUL_ETAIL_UND"].fillna(0)
                          + proy_full2["VENTA_FUL_MAYOR_UND"].fillna(0))
    s3 = (proy_full2.groupby("SKU_PRODUCTO", as_index=False)
          .agg(VTA_PROY=("_VTA", "sum"),
               NETA_PROY=("VN_RES_TOTAL", "sum"),
               APORTE_PROY=("APORTE_RES_TOTAL", "sum")))

    # ── 4. AA YTD (Jan-May 2025 + Jun 2025 prorateado) ────────────────────────
    aa_past2 = aa_ytd_past.copy()
    aa_past2["_VTA"] = (aa_past2["VENTA_FUL_TIENDA_UND"].fillna(0)
                        + aa_past2["VENTA_FUL_ETAIL_UND"].fillna(0)
                        + aa_past2["VENTA_FUL_MAYOR_UND"].fillna(0))
    s4a = (aa_past2.groupby("SKU_PRODUCTO", as_index=False)
           .agg(VTA_AAYTD_PAST=("_VTA", "sum"),
                NETA_AAYTD_PAST=("VN_RES_TOTAL", "sum"),
                APORTE_AAYTD_PAST=("APORTE_RES_TOTAL", "sum")))

    aa_mes2 = aa_ytd_mes.copy()
    aa_mes2["_VTA"] = ((aa_mes2["VENTA_FUL_TIENDA_UND"].fillna(0)
                        + aa_mes2["VENTA_FUL_ETAIL_UND"].fillna(0)
                        + aa_mes2["VENTA_FUL_MAYOR_UND"].fillna(0)) * peso_aa_mes)
    s4b = (aa_mes2.groupby("SKU_PRODUCTO", as_index=False)
           .agg(VTA_AAYTD_MES=("_VTA", "sum"),
                NETA_AAYTD_MES=("VN_RES_TOTAL", "sum"),
                APORTE_AAYTD_MES=("APORTE_RES_TOTAL", "sum")))
    # Escalar neta/aporte del mes AA por peso
    for col in ["NETA_AAYTD_MES", "APORTE_AAYTD_MES"]:
        s4b[col] = s4b[col] * peso_aa_mes

    # ── 5. AA Cierre Año (2025 completo) ──────────────────────────────────────
    aa_full2 = aa_full.copy()
    aa_full2["_VTA"] = (aa_full2["VENTA_FUL_TIENDA_UND"].fillna(0)
                        + aa_full2["VENTA_FUL_ETAIL_UND"].fillna(0)
                        + aa_full2["VENTA_FUL_MAYOR_UND"].fillna(0))
    s5 = (aa_full2.groupby("SKU_PRODUCTO", as_index=False)
          .agg(VTA_AAANO=("_VTA", "sum"),
               NETA_AAANO=("VN_RES_TOTAL", "sum"),
               APORTE_AAANO=("APORTE_RES_TOTAL", "sum")))

    # ── Base SKU dimension table ───────────────────────────────────────────────
    # Usar REAL+FC de mes actual como base dimensional (tiene todos los SKUs del año)
    base_dims = proy_full.drop_duplicates("SKU_PRODUCTO")[_DIMS].copy()
    base_dims = base_dims.rename(columns={"SKU_NOM_PRODUCTO": "DESCRIPCION"})

    # ── Merge everything ───────────────────────────────────────────────────────
    result = base_dims.copy()
    for s, key in [(s1, "SKU_PRODUCTO"), (s2, "SKU_PRODUCTO"),
                   (s3, "SKU_PRODUCTO"), (s4a, "SKU_PRODUCTO"),
                   (s4b, "SKU_PRODUCTO"), (s5, "SKU_PRODUCTO")]:
        result = result.merge(s, on=key, how="left")

    result = result.fillna(0)

    # ── YTD combinado (Jan-May + Jun parcial) ──────────────────────────────────
    result["VTA_YTD"]    = result["VTA_H"]    + result["VTA_C"]
    result["NETA_YTD"]   = result["NETA_H"]   + result["NETA_C"]
    result["APORTE_YTD"] = result["APORTE_H"] + result["APORTE_C"]
    result["MRG_YTD"]    = np.where(result["NETA_YTD"] > 0,
                                    result["APORTE_YTD"] / result["NETA_YTD"] * 100, 0.0)

    # ── AA YTD combinado ───────────────────────────────────────────────────────
    result["VTA_AA_YTD"]    = result["VTA_AAYTD_PAST"]    + result["VTA_AAYTD_MES"]
    result["NETA_AA_YTD"]   = result["NETA_AAYTD_PAST"]   + result["NETA_AAYTD_MES"]
    result["APORTE_AA_YTD"] = result["APORTE_AAYTD_PAST"] + result["APORTE_AAYTD_MES"]
    result["MRG_AA_YTD"]    = np.where(result["NETA_AA_YTD"] > 0,
                                       result["APORTE_AA_YTD"] / result["NETA_AA_YTD"] * 100, 0.0)

    # ── Proyección Año ─────────────────────────────────────────────────────────
    result["MRG_PROY_ANO"] = np.where(result["NETA_PROY"] > 0,
                                      result["APORTE_PROY"] / result["NETA_PROY"] * 100, 0.0)

    # ── AA Año ─────────────────────────────────────────────────────────────────
    result["MRG_AAANO"]    = np.where(result["NETA_AAANO"] > 0,
                                      result["APORTE_AAANO"] / result["NETA_AAANO"] * 100, 0.0)

    # ── Variaciones YTD vs AA YTD ──────────────────────────────────────────────
    result["VAR_VTA_YTD"]  = np.where(result["VTA_AA_YTD"]  > 0,
                                      (result["VTA_YTD"]  - result["VTA_AA_YTD"])  / result["VTA_AA_YTD"]  * 100, np.nan)
    result["VAR_NETA_YTD"] = np.where(result["NETA_AA_YTD"] > 0,
                                      (result["NETA_YTD"] - result["NETA_AA_YTD"]) / result["NETA_AA_YTD"] * 100, np.nan)

    # ── Variaciones Proy Año vs AA Año ─────────────────────────────────────────
    result["VAR_VTA_ANO"]  = np.where(result["VTA_AAANO"]  > 0,
                                      (result["VTA_PROY"]  - result["VTA_AAANO"])  / result["VTA_AAANO"]  * 100, np.nan)
    result["VAR_NETA_ANO"] = np.where(result["NETA_AAANO"] > 0,
                                      (result["NETA_PROY"] - result["NETA_AAANO"]) / result["NETA_AAANO"] * 100, np.nan)

    keep = (
        ["SKU_PRODUCTO", "DESCRIPCION", "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL"]
        + ["VTA_YTD", "NETA_YTD", "APORTE_YTD", "MRG_YTD"]
        + ["VTA_AA_YTD", "NETA_AA_YTD", "APORTE_AA_YTD", "MRG_AA_YTD"]
        + ["VAR_VTA_YTD", "VAR_NETA_YTD"]
        + ["VTA_PROY", "NETA_PROY", "APORTE_PROY", "MRG_PROY_ANO"]
        + ["VTA_AAANO", "NETA_AAANO", "APORTE_AAANO", "MRG_AAANO"]
        + ["VAR_VTA_ANO", "VAR_NETA_ANO"]
    )
    return result[[c for c in keep if c in result.columns]].copy()


# ─── Main render ───────────────────────────────────────────────────────────────

def render_dashboard_gestion(conn=None):
    st.html("<h2 class='sub-header'>Dashboard de Gestion de Planificacion</h2>")

    # ── Load data ──────────────────────────────────────────────────────────────
    raw = _load_proy()
    if raw is None or raw.empty:
        show_empty_state(
            "Sin datos de proyección",
            "Ejecuta el módulo **Proyección Stock** primero para generar proy_result.",
        )
        return

    raw = apply_pm_filter(raw)

    # ── Fetch actual sales from Snowflake FT_VCM (override proy_result actuals) ─
    df_sf_vta: pd.DataFrame | None = None
    if conn is not None:
        try:
            with lottie_spinner("snowflake"):
                df_sf_vta = _fetch_vta_actuals(conn)
        except Exception as _e:
            st.caption(f"⚠️ No se pudo obtener datos de FT_VCM: {_e}. Usando proy_result.")

    df_full, meta = _build_dashboard(raw, df_sf_vta=df_sf_vta)
    if df_full.empty:
        st.warning(
            "No se encontraron filas REAL+FC para el mes actual. "
            "Verifica que la proyección esté actualizada."
        )
        return

    mes_label = meta["mes_label"]
    dias_avance = meta["dias_avance"]
    dias_mes = meta["dias_mes"]
    n_meses = meta["n_meses_cerrados"]
    meses_label = meta["meses_cerrados_label"]

    st.html(
        f'<p style="color:#64748b;margin-top:-0.5rem;margin-bottom:1rem;font-size:0.85rem;">'
        f"Periodo: <b>{mes_label}</b> &nbsp;|&nbsp; "
        f"Dias avanzados: <b>{dias_avance}/{dias_mes}</b> &nbsp;|&nbsp; "
        f"Promedio MOI base: <b>{meses_label}</b> ({n_meses} meses)</p>"
    )

    # ── Filters ────────────────────────────────────────────────────────────────
    # Líneas excluidas por defecto (sin movimiento relevante en el mercado)
    _LINEAS_EXCLUIR_DEFAULT = ["Motricidad", "Promocionales", "Accesorios Juguetes", "Calzado"]

    with st.expander("Filtros", expanded=False):
        f1, f2, f3, f4, f5 = st.columns(5)
        areas    = sorted(df_full["AREA"].dropna().unique())
        lineas   = sorted(df_full["LINEA"].dropna().unique())
        sublineas= sorted(df_full["SUBLINEA"].dropna().unique())
        marcas   = sorted(df_full["MARCA"].dropna().unique())
        mixes    = sorted(df_full["MIX_OFICIAL"].dropna().unique())

        sel_area  = f1.multiselect("Área",    areas,    key="dg_area")
        sel_linea = f2.multiselect("Línea",   lineas,   key="dg_linea")
        sel_sub   = f3.multiselect("Sublínea",sublineas,key="dg_sub")
        sel_marca = f4.multiselect("Marca",   marcas,   key="dg_marca")
        sel_mix   = f5.multiselect("Mix",     mixes,    key="dg_mix")

        sku_text = st.text_input(
            "SKU(s) (separar por coma o salto de línea)", key="dg_sku"
        )

        # Líneas excluidas — precargadas con las que no aplican al negocio
        lineas_disponibles = sorted(df_full["LINEA"].dropna().unique())
        _excluir_default_validos = [l for l in _LINEAS_EXCLUIR_DEFAULT if l in lineas_disponibles]
        excluir_lineas = st.multiselect(
            "Excluir líneas",
            lineas_disponibles,
            default=st.session_state.get("dg_excluir_lineas", _excluir_default_validos),
            key="dg_excluir_lineas",
            help="Líneas que se ocultan de la vista. Editable.",
        )

    df = df_full.copy()
    # Aplicar exclusiones primero
    if excluir_lineas:
        df = df[~df["LINEA"].isin(excluir_lineas)]
    if sel_area:
        df = df[df["AREA"].isin(sel_area)]
    if sel_linea:
        df = df[df["LINEA"].isin(sel_linea)]
    if sel_sub:
        df = df[df["SUBLINEA"].isin(sel_sub)]
    if sel_marca:
        df = df[df["MARCA"].isin(sel_marca)]
    if sel_mix:
        df = df[df["MIX_OFICIAL"].isin(sel_mix)]
    if sku_text.strip():
        skus = [s.strip() for s in sku_text.replace("\n", ",").split(",") if s.strip()]
        df = df[df["SKU_PRODUCTO"].isin(skus)]

    if df.empty:
        st.warning("No hay datos con los filtros seleccionados.")
        return

    # ── KPI summary ────────────────────────────────────────────────────────────
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    with k1:
        st.html(_kpi_card("Stock Actual", f"{df['STOCK_ACTUAL'].sum():,.0f} und"))
    with k2:
        st.html(_kpi_card("Vta Unds Actual", f"{df['VTA_UNDS_ACTUAL'].sum():,.0f} und"))
    with k3:
        st.html(_kpi_card("Vta Pry Calc", f"{df['VTA_UNDS_PRY_CALC'].sum():,.0f} und"))
    with k4:
        st.html(_kpi_card("Neta Actual", _fmt_s(df["NETA_ACTUAL"].sum())))
    with k5:
        st.html(_kpi_card("Aporte Actual", _fmt_s(df["APORTE_ACTUAL"].sum())))
    with k6:
        mrg_agg = (
            df["APORTE_ACTUAL"].sum() / df["NETA_ACTUAL"].sum()
            if df["NETA_ACTUAL"].sum() > 0
            else 0
        )
        st.html(_kpi_card("Mrg% Actual", f"{mrg_agg:.1%}"))

    st.markdown("---")

    # ── Definicion de todas las columnas disponibles ───────────────────────────
    # Clave interna → (label visible, column_config)
    _ALL_COLS: dict[str, tuple[str, st.column_config._column.Column]] = {
        "SKU_PRODUCTO":         ("SKU",            st.column_config.TextColumn("SKU",            width="small")),
        "DESCRIPCION":          ("Descripcion",    st.column_config.TextColumn("Descripcion",    width="medium")),
        "AREA":                 ("Area",           st.column_config.TextColumn("Area",           width="small")),
        "LINEA":                ("Linea",          st.column_config.TextColumn("Linea",          width="medium")),
        "SUBLINEA":             ("Sublinea",       st.column_config.TextColumn("Sublinea",       width="medium")),
        "MARCA":                ("Marca",          st.column_config.TextColumn("Marca",          width="medium")),
        "MIX_OFICIAL":          ("Mix",            st.column_config.TextColumn("Mix",            width="small")),
        "Stock Actual":         ("Stock Act",      st.column_config.NumberColumn("Stock Act",      format="%,.0f",    width="small")),
        "Stock Proyectado":     ("Stock Pry",      st.column_config.NumberColumn("Stock Pry",      format="%,.0f",    width="small")),
        "Vta Unds Actual":      ("Vta Act",        st.column_config.NumberColumn("Vta Act",        format="%,.0f",    width="small")),
        "Vta Unds Pry Calculo": ("Vta Pry Calc",   st.column_config.NumberColumn("Vta Pry Calc",   format="%,.0f",    width="small")),
        "Vta Unds Pry Syncro":  ("Vta Pry Sync",   st.column_config.NumberColumn("Vta Pry Sync",   format="%,.0f",    width="small")),
        "Vta Unds AA":          ("Vta Unds AA",    st.column_config.NumberColumn("Vta Unds AA",    format="%,.0f",    width="small")),
        "MOI Act":              ("MOI Act",        st.column_config.NumberColumn("MOI Act",        format="%.2f",     width="small")),
        "MOI Pry":              ("MOI Pry",        st.column_config.NumberColumn("MOI Pry",        format="%.2f",     width="small")),
        "ETA Pendiente Recep":  ("ETA Pdte",       st.column_config.NumberColumn("ETA Pdte",       format="%,.0f",    width="small")),
        "Forecast Compra":      ("FC Compra",      st.column_config.NumberColumn("FC Compra",      format="%,.0f",    width="small")),
        "Neta Actual":          ("Neta Act",       st.column_config.NumberColumn("Neta Act",       format="S/ %,.2f", width="small")),
        "Neta Proyectado":      ("Neta Pry",       st.column_config.NumberColumn("Neta Pry",       format="S/ %,.2f", width="small")),
        "Neta AA":              ("Neta AA",        st.column_config.NumberColumn("Neta AA",        format="S/ %,.2f", width="small")),
        "Neta Pry Syncro":      ("Neta Sync",      st.column_config.NumberColumn("Neta Sync",      format="S/ %,.0f", width="small")),
        "Aporte Actual":        ("Aporte Act",     st.column_config.NumberColumn("Aporte Act",     format="S/ %,.2f", width="small")),
        "Aporte Proyectado":    ("Aporte Pry",     st.column_config.NumberColumn("Aporte Pry",     format="S/ %,.2f", width="small")),
        "Aporte AA":            ("Aporte AA",      st.column_config.NumberColumn("Aporte AA",      format="S/ %,.2f", width="small")),
        "Aporte Pry Syncro":    ("Aporte Sync",    st.column_config.NumberColumn("Aporte Sync",    format="S/ %,.0f", width="small")),
        "Mrg% Actual":          ("Mrg% Act",       st.column_config.NumberColumn("Mrg% Act",       format="%.2f %%",  width="small")),
        "Mrg% Proyectado":      ("Mrg% Pry",       st.column_config.NumberColumn("Mrg% Pry",       format="%.2f %%",  width="small")),
        "Mrg% AA":              ("Mrg% AA",        st.column_config.NumberColumn("Mrg% AA",        format="%.2f %%",  width="small")),
        "Mrg% Pry Syncro":      ("Mrg% Sync",      st.column_config.NumberColumn("Mrg% Sync",      format="%.2f %%",  width="small")),
        "DIAGNOSTICO":          ("Diagnostico",    st.column_config.TextColumn("Diagnostico",    width="medium")),
        "SUGERIDO":             ("Sugerido",       st.column_config.TextColumn("Sugerido",       width="large")),
    }
    _ALL_COL_KEYS = list(_ALL_COLS.keys())
    _DEFAULT_COLS = [
        "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL", "SKU_PRODUCTO",
        "Stock Actual", "Stock Proyectado",
        "Vta Unds Actual", "Vta Unds Pry Calculo", "Vta Unds Pry Syncro", "Vta Unds AA",
        "MOI Act", "MOI Pry",
        "ETA Pendiente Recep", "Forecast Compra",
        "Neta Actual", "Neta Proyectado", "Neta AA",
        "Aporte Actual", "Aporte Proyectado", "Aporte AA",
        "Mrg% Actual", "Mrg% Proyectado", "Mrg% AA",
        "DIAGNOSTICO", "SUGERIDO",
    ]

    # ── Panel de configuración de columnas ────────────────────────────────────
    with st.expander("⚙️ Configurar columnas (visibilidad y orden)", expanded=False):
        selected_cols: list[str] = st.multiselect(
            "Columnas visibles (marca para agregar / desmarca para quitar)",
            options=_ALL_COL_KEYS,
            default=st.session_state.get("dg_cols", _DEFAULT_COLS),
            format_func=lambda k: _ALL_COLS[k][0],
            key="dg_cols",
        )
        if selected_cols:
            st.caption("Arrastra las fichas para cambiar el orden de las columnas:")
            _label_to_key = {v[0]: k for k, v in _ALL_COLS.items()}

            # Clave del widget basada en el CONJUNTO activo (no el orden).
            # Al agregar/quitar una columna la clave cambia y el componente
            # se reinicia; al solo reordenar la clave es la misma y el
            # componente conserva el estado de arrastre.
            _order_key = "dg_col_order_" + "_".join(sorted(selected_cols))

            # sort_items almacena su estado como [{"header": None, "items": [...]}].
            # Extraemos la lista de labels del primer contenedor.
            _widget_raw = st.session_state.get(_order_key)
            _prev_labels: list[str] = []
            if isinstance(_widget_raw, list) and _widget_raw:
                first = _widget_raw[0]
                if isinstance(first, dict) and "items" in first:
                    _prev_labels = [l for l in first["items"] if isinstance(l, str)]
                elif isinstance(first, str):
                    _prev_labels = [l for l in _widget_raw if isinstance(l, str)]

            if _prev_labels:
                _prev_keys = [_label_to_key[lbl] for lbl in _prev_labels
                              if lbl in _label_to_key]
            else:
                _prev_keys = selected_cols

            # Merge: mantener orden previo + agregar columnas nuevas al final
            _sel_set  = set(selected_cols)
            _ordered  = [c for c in _prev_keys if c in _sel_set]
            _ordered += [c for c in selected_cols if c not in set(_ordered)]

            _sorted_labels = _sort_items(
                [_ALL_COLS[k][0] for k in _ordered],
                direction="horizontal",
                key=_order_key,
            )
            selected_cols = [
                _label_to_key[lbl] for lbl in _sorted_labels if lbl in _label_to_key
            ]

    if not selected_cols:
        selected_cols = _DEFAULT_COLS

    # ── Preparar DataFrame display ────────────────────────────────────────────
    df_display = (
        df.sort_values(["AREA", "LINEA", "SUBLINEA", "MARCA", "SKU_PRODUCTO"])
        .rename(columns={"SKU_NOM_PRODUCTO": "DESCRIPCION", **_METRIC_LABELS})
        .reset_index(drop=True)
    )

    # Mrg% ya en fracción → escalar a porcentaje para mostrar
    for lbl in ["Mrg% Actual", "Mrg% Proyectado", "Mrg% AA", "Mrg% Pry Syncro"]:
        if lbl in df_display.columns:
            df_display[lbl] = df_display[lbl] * 100

    # Rellenar NaN numéricos con 0
    num_cols = df_display.select_dtypes(include="number").columns
    df_display[num_cols] = df_display[num_cols].fillna(0)

    visible = [c for c in selected_cols if c in df_display.columns]

    # ── Agrupación dinámica ───────────────────────────────────────────────────
    # Dimensiones jerárquicas ordenadas de mayor a menor granularidad.
    # Si el usuario oculta SKU/DESCRIPCION y alguna dim superior, los datos
    # se agregan al nivel de las dimensiones que sí están visibles.
    _HIER_DIMS   = ["AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL"]
    _LEAF_DIMS   = {"SKU_PRODUCTO", "DESCRIPCION"}       # detalle por producto
    _SUM_METRICS = {                                      # columnas que se suman
        "Stock Actual", "Stock Proyectado",
        "Vta Unds Actual", "Vta Unds Pry Calculo", "Vta Unds Pry Syncro", "Vta Unds AA",
        "ETA Pendiente Recep", "Forecast Compra",
        "Neta Actual", "Neta Proyectado", "Neta AA", "Neta Pry Syncro",
        "Aporte Actual", "Aporte Proyectado", "Aporte AA", "Aporte Pry Syncro",
        "_VTA_AA_FULL", "_NETA_AA_FULL",              # para impacto AA en diagnóstico
        "_AVG_VTA",                                   # para recalcular MOI tras agrupar
    }
    _MRG_PAIRS = {                                        # Mrg% se recalcula tras sumar
        "Mrg% Actual":     ("Aporte Actual",    "Neta Actual"),
        "Mrg% Proyectado": ("Aporte Proyectado","Neta Proyectado"),
        "Mrg% AA":         ("Aporte AA",        "Neta AA"),
        "Mrg% Pry Syncro": ("Aporte Pry Syncro","Neta Pry Syncro"),
    }
    _AVG_METRICS: set = set()          # MOI se recalcula desde componentes, no con mean

    vis_set = set(visible)
    groupby_dims = [c for c in _HIER_DIMS if c in vis_set]
    need_agg = bool(groupby_dims) and not (vis_set & _LEAF_DIMS)

    if need_agg:
        # Columnas extra necesarias para recalcular Mrg% aunque no estén visibles
        extra_for_mrg: set[str] = set()
        for mrg_col, (ap, nt) in _MRG_PAIRS.items():
            if mrg_col in vis_set:
                extra_for_mrg.update([ap, nt])

        agg_dict: dict = {}
        for col in df_display.columns:
            if col in groupby_dims:
                continue
            if col in _SUM_METRICS or col in extra_for_mrg:
                agg_dict[col] = "sum"
            elif col in _AVG_METRICS:
                agg_dict[col] = "mean"
            # DIAGNOSTICO se calcula después del groupby con _calc_diag_group

        df_agg = df_display.groupby(groupby_dims, as_index=False).agg(agg_dict)

        # Recalcular Mrg% como Aporte/Neta del grupo (no promedio de fracciones)
        for mrg_col, (ap, nt) in _MRG_PAIRS.items():
            if mrg_col in vis_set and ap in df_agg.columns and nt in df_agg.columns:
                df_agg[mrg_col] = np.where(
                    df_agg[nt] > 0, df_agg[ap] / df_agg[nt] * 100, 0.0
                )

        # Recalcular MOI desde componentes sumados (evita distorsión del mean de ratios)
        # MOI Act = sum(Stock) / sum(Avg mensual Ene-May)
        # MOI Pry = sum(Stock Pry) / nuevo_avg_mensual_incl_junio
        _n = meta.get("n_meses_cerrados", 5)
        if "_AVG_VTA" in df_agg.columns:
            avg_sum = df_agg["_AVG_VTA"]
            if "MOI Act" in vis_set and "Stock Actual" in df_agg.columns:
                df_agg["MOI Act"] = np.where(
                    avg_sum > 0, df_agg["Stock Actual"] / avg_sum, np.nan
                )
            if "MOI Pry" in vis_set and "Stock Proyectado" in df_agg.columns:
                vta_c_sum = df_agg.get("Vta Unds Pry Calculo", pd.Series(0, index=df_agg.index))
                nuevo_avg = (avg_sum * _n + vta_c_sum) / (_n + 1)
                df_agg["MOI Pry"] = np.where(
                    nuevo_avg > 0, df_agg["Stock Proyectado"] / nuevo_avg, np.nan
                )

        # Diagnóstico y Sugerido a nivel de grupo
        if "DIAGNOSTICO" in vis_set:
            df_agg["DIAGNOSTICO"] = df_agg.apply(_calc_diag_group, axis=1)
        if "SUGERIDO" in vis_set:
            df_agg["SUGERIDO"]    = df_agg.apply(_calc_sugerido_group, axis=1)

        df_view = df_agg[[c for c in visible if c in df_agg.columns]]
        row_label = f"**{len(df_view):,} grupos** (agregado)"
    else:
        df_view = df_display[visible]
        row_label = f"**{len(df_view):,} SKUs**"

    col_cfg = {k: v[1] for k, v in _ALL_COLS.items()}
    st.markdown(f"{row_label} · {mes_label} · {len(visible)} columnas")

    st.dataframe(
        df_view,
        use_container_width=True,
        hide_index=True,
        height=600,
        column_config=col_cfg,
    )

    _fname = f"frinc_detalle_{mes_label.replace(' ', '_')}"
    download_buttons(
        df_view,
        _fname,
        excel_buffer=_excel_autofit(df_view, sheet_name="Frinc Detalle"),
    )

    # ══════════════════════════════════════════════════════════════════════════
    # TABLA 2: GESTIÓN DE RESULTADO CIERRE DE AÑO
    # ══════════════════════════════════════════════════════════════════════════
    _año_render = date.today().year
    st.markdown("---")
    st.html("<h3 style='margin-top:0.5rem;'>Gestion de Resultado Cierre de Año</h3>")
    st.html(
        f'<p style="color:#64748b;font-size:0.85rem;margin-top:-0.5rem;">'
        f"YTD real vs AA YTD &nbsp;·&nbsp; Proyeccion cierre {_año_render} "
        f"vs cierre AA {_año_render - 1}</p>"
    )

    with lottie_spinner("analytics"):
        df_ca = _build_cierre_ano(raw, df_sf_ytd=df_sf_vta)

    # Aplicar los mismos filtros de dimensión que la tabla principal
    if excluir_lineas:
        df_ca = df_ca[~df_ca["LINEA"].isin(excluir_lineas)]
    if sel_area:
        df_ca = df_ca[df_ca["AREA"].isin(sel_area)]
    if sel_linea:
        df_ca = df_ca[df_ca["LINEA"].isin(sel_linea)]
    if sel_sub:
        df_ca = df_ca[df_ca["SUBLINEA"].isin(sel_sub)]
    if sel_marca:
        df_ca = df_ca[df_ca["MARCA"].isin(sel_marca)]
    if sel_mix:
        df_ca = df_ca[df_ca["MIX_OFICIAL"].isin(sel_mix)]

    if df_ca.empty:
        st.info("Sin datos para la tabla de cierre de año con los filtros aplicados.")
    else:
        # Misma lógica de agrupación que la tabla principal
        _CA_HIER_DIMS = ["AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL"]
        vis_ca = set(visible)                            # reutiliza dims visibles
        gb_ca  = [c for c in _CA_HIER_DIMS if c in vis_ca]
        need_agg_ca = bool(gb_ca) and not (vis_ca & {"SKU_PRODUCTO", "DESCRIPCION"})

        _CA_SUM = {
            "VTA_YTD","NETA_YTD","APORTE_YTD",
            "VTA_AA_YTD","NETA_AA_YTD","APORTE_AA_YTD",
            "VTA_PROY","NETA_PROY","APORTE_PROY",
            "VTA_AAANO","NETA_AAANO","APORTE_AAANO",
        }
        _CA_MRG_PAIRS = {
            "MRG_YTD":      ("APORTE_YTD",   "NETA_YTD"),
            "MRG_AA_YTD":   ("APORTE_AA_YTD","NETA_AA_YTD"),
            "MRG_PROY_ANO": ("APORTE_PROY",  "NETA_PROY"),
            "MRG_AAANO":    ("APORTE_AAANO", "NETA_AAANO"),
        }

        if need_agg_ca and gb_ca:
            agg_ca = {c: "sum" for c in _CA_SUM if c in df_ca.columns}
            df_ca_agg = df_ca.groupby(gb_ca, as_index=False).agg(agg_ca)
            # Recalcular márgenes y variaciones
            for mrg_col, (ap, nt) in _CA_MRG_PAIRS.items():
                if ap in df_ca_agg.columns and nt in df_ca_agg.columns:
                    df_ca_agg[mrg_col] = np.where(
                        df_ca_agg[nt] > 0, df_ca_agg[ap] / df_ca_agg[nt] * 100, 0.0
                    )
            for var_col, (num, den) in [
                ("VAR_VTA_YTD",  ("VTA_YTD",  "VTA_AA_YTD")),
                ("VAR_NETA_YTD", ("NETA_YTD", "NETA_AA_YTD")),
                ("VAR_VTA_ANO",  ("VTA_PROY", "VTA_AAANO")),
                ("VAR_NETA_ANO", ("NETA_PROY","NETA_AAANO")),
            ]:
                if num in df_ca_agg.columns and den in df_ca_agg.columns:
                    df_ca_agg[var_col] = np.where(
                        df_ca_agg[den] > 0,
                        (df_ca_agg[num] - df_ca_agg[den]) / df_ca_agg[den] * 100, np.nan
                    )
            df_ca_view = df_ca_agg
        else:
            df_ca_view = df_ca

        # ── Definición de todas las columnas disponibles (tabla Cierre Año) ─────
        _ALL_CA_COLS: dict[str, tuple[str, st.column_config._column.Column]] = {
            "AREA":         ("Area",           st.column_config.TextColumn("Area",           width="small")),
            "LINEA":        ("Linea",          st.column_config.TextColumn("Linea",          width="medium")),
            "SUBLINEA":     ("Sublinea",       st.column_config.TextColumn("Sublinea",       width="medium")),
            "MARCA":        ("Marca",          st.column_config.TextColumn("Marca",          width="medium")),
            "MIX_OFICIAL":  ("Mix",            st.column_config.TextColumn("Mix",            width="small")),
            "SKU_PRODUCTO": ("SKU",            st.column_config.TextColumn("SKU",            width="small")),
            "DESCRIPCION":  ("Descripcion",    st.column_config.TextColumn("Descripcion",    width="medium")),
            "VTA_YTD":      ("Vta Unds YTD",   st.column_config.NumberColumn("Vta Unds YTD",   format="%,.0f",    width="small")),
            "NETA_YTD":     ("Neta YTD",       st.column_config.NumberColumn("Neta YTD",       format="S/ %,.0f", width="small")),
            "APORTE_YTD":   ("Aporte YTD",     st.column_config.NumberColumn("Aporte YTD",     format="S/ %,.0f", width="small")),
            "MRG_YTD":      ("Mrg% YTD",       st.column_config.NumberColumn("Mrg% YTD",       format="%.2f %%",  width="small")),
            "VTA_AA_YTD":   ("Vta AA YTD",     st.column_config.NumberColumn("Vta AA YTD",     format="%,.0f",    width="small")),
            "NETA_AA_YTD":  ("Neta AA YTD",    st.column_config.NumberColumn("Neta AA YTD",    format="S/ %,.0f", width="small")),
            "APORTE_AA_YTD":("Aporte AA YTD",  st.column_config.NumberColumn("Aporte AA YTD",  format="S/ %,.0f", width="small")),
            "MRG_AA_YTD":   ("Mrg% AA YTD",    st.column_config.NumberColumn("Mrg% AA YTD",    format="%.2f %%",  width="small")),
            "VAR_VTA_YTD":  ("Var Vta YTD%",   st.column_config.NumberColumn("Var Vta YTD%",   format="%+.1f %%", width="small")),
            "VAR_NETA_YTD": ("Var Neta YTD%",  st.column_config.NumberColumn("Var Neta YTD%",  format="%+.1f %%", width="small")),
            "VTA_PROY":     ("Vta Proy Año",   st.column_config.NumberColumn("Vta Proy Año",   format="%,.0f",    width="small")),
            "NETA_PROY":    ("Neta Proy Año",  st.column_config.NumberColumn("Neta Proy Año",  format="S/ %,.0f", width="small")),
            "APORTE_PROY":  ("Aporte Proy Año",st.column_config.NumberColumn("Aporte Proy Año",format="S/ %,.0f", width="small")),
            "MRG_PROY_ANO": ("Mrg% Proy Año",  st.column_config.NumberColumn("Mrg% Proy Año",  format="%.2f %%",  width="small")),
            "VTA_AAANO":    ("Vta AA Año",      st.column_config.NumberColumn("Vta AA Año",     format="%,.0f",    width="small")),
            "NETA_AAANO":   ("Neta AA Año",     st.column_config.NumberColumn("Neta AA Año",    format="S/ %,.0f", width="small")),
            "APORTE_AAANO": ("Aporte AA Año",   st.column_config.NumberColumn("Aporte AA Año",  format="S/ %,.0f", width="small")),
            "MRG_AAANO":    ("Mrg% AA Año",     st.column_config.NumberColumn("Mrg% AA Año",    format="%.2f %%",  width="small")),
            "VAR_VTA_ANO":  ("Var Vta Año%",    st.column_config.NumberColumn("Var Vta Año%",   format="%+.1f %%", width="small")),
            "VAR_NETA_ANO": ("Var Neta Año%",   st.column_config.NumberColumn("Var Neta Año%",  format="%+.1f %%", width="small")),
        }
        _CA_ALL_KEYS   = list(_ALL_CA_COLS.keys())
        _CA_DEFAULT    = [
            "AREA", "LINEA",
            "VTA_YTD", "NETA_YTD", "MRG_YTD",
            "VTA_AA_YTD", "NETA_AA_YTD", "MRG_AA_YTD",
            "VAR_VTA_YTD", "VAR_NETA_YTD",
            "VTA_PROY", "NETA_PROY", "MRG_PROY_ANO",
            "VTA_AAANO", "NETA_AAANO", "MRG_AAANO",
            "VAR_VTA_ANO", "VAR_NETA_ANO",
        ]

        # ── Panel de configuración (claves ca_ para no mezclar con tabla 1) ───
        with st.expander("⚙️ Configurar columnas — Cierre Año", expanded=False):
            ca_selected: list[str] = st.multiselect(
                "Columnas visibles",
                options=_CA_ALL_KEYS,
                default=st.session_state.get("ca_cols", _CA_DEFAULT),
                format_func=lambda k: _ALL_CA_COLS[k][0],
                key="ca_cols",
            )
            if ca_selected:
                st.caption("Arrastra las fichas para cambiar el orden:")
                _ca_lbl2key = {v[0]: k for k, v in _ALL_CA_COLS.items()}
                _ca_order_key = "ca_col_order_" + "_".join(sorted(ca_selected))
                _ca_widget_raw = st.session_state.get(_ca_order_key)
                _ca_prev_labels: list[str] = []
                if isinstance(_ca_widget_raw, list) and _ca_widget_raw:
                    first = _ca_widget_raw[0]
                    if isinstance(first, dict) and "items" in first:
                        _ca_prev_labels = [l for l in first["items"] if isinstance(l, str)]
                    elif isinstance(first, str):
                        _ca_prev_labels = [l for l in _ca_widget_raw if isinstance(l, str)]
                _ca_prev_keys = ([_ca_lbl2key[l] for l in _ca_prev_labels if l in _ca_lbl2key]
                                 if _ca_prev_labels else ca_selected)
                _ca_sel_set   = set(ca_selected)
                _ca_ordered   = [c for c in _ca_prev_keys if c in _ca_sel_set]
                _ca_ordered  += [c for c in ca_selected if c not in set(_ca_ordered)]
                _ca_sorted = _sort_items(
                    [_ALL_CA_COLS[k][0] for k in _ca_ordered],
                    direction="horizontal",
                    key=_ca_order_key,
                )
                ca_selected = [_ca_lbl2key[l] for l in _ca_sorted if l in _ca_lbl2key]

        if not ca_selected:
            ca_selected = _CA_DEFAULT

        _ca_col_cfg = {k: v[1] for k, v in _ALL_CA_COLS.items()}
        _ca_visible_cols = [c for c in ca_selected if c in df_ca_view.columns]

        st.markdown(
            f"**{len(df_ca_view):,} {'grupos' if need_agg_ca else 'SKUs'}** · "
            f"YTD a {meta['dias_avance']}/{mes_label} | "
            f"Proyeccion {_año_render} | Comparativo {_año_render - 1}"
        )
        st.dataframe(
            df_ca_view[_ca_visible_cols],
            use_container_width=True,
            hide_index=True,
            height=500,
            column_config=_ca_col_cfg,
        )
        download_buttons(
            df_ca_view[_ca_visible_cols],
            f"cierre_ano_{_año_render}",
            excel_buffer=_excel_autofit(df_ca_view[_ca_visible_cols], sheet_name="Cierre Año"),
        )

    # ══════════════════════════════════════════════════════════════════════════
    # TABLA 3: GESTIÓN DE STOCK
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown("---")
    st.html("<h3 style='margin-top:0.5rem;'>Gestion de Stock</h3>")
    st.html(
        '<p style="color:#64748b;font-size:0.85rem;margin-top:-0.5rem;">'
        "Stock actual vs proyectado fin de año &nbsp;·&nbsp; MOI &nbsp;·&nbsp; "
        "Diagnóstico por SKU &nbsp;·&nbsp; Pareto 80% disponible</p>"
    )

    # ── Compute future-months' stock movements from raw ───────────────────────
    _sm_hoy = date.today()
    _sm_año, _sm_mes = _sm_hoy.year, _sm_hoy.month

    _sm_future = raw[
        (raw["PERIODO_ANO"] == _sm_año) &
        (raw["PERIODO_MES"] > _sm_mes) &
        (raw["TIPO_DATO"] == "PROYECCION")
    ].copy()

    if not _sm_future.empty:
        _sm_future["_VTA_F"] = (
            _sm_future["VENTA_FUL_TIENDA_UND"].fillna(0)
            + _sm_future["VENTA_FUL_ETAIL_UND"].fillna(0)
            + _sm_future["VENTA_FUL_MAYOR_UND"].fillna(0)
        )
        _sm_future["_ETA_F"] = _sm_future["ETA"].fillna(0)
        _sm_future["_FC_F"]  = _sm_future["FORECAST_COMPRA"].fillna(0)
        _sm_fut_agg = (
            _sm_future.groupby("SKU_PRODUCTO", as_index=False)
            [["_VTA_F", "_ETA_F", "_FC_F"]].sum()
        )
    else:
        _sm_fut_agg = pd.DataFrame(
            columns=["SKU_PRODUCTO", "_VTA_F", "_ETA_F", "_FC_F"]
        )

    # PROCEDENCIA: first non-null per SKU from raw
    if "PROCEDENCIA" in raw.columns:
        _sm_proc = (
            raw[raw["PROCEDENCIA"].notna()]
            .drop_duplicates("SKU_PRODUCTO")[["SKU_PRODUCTO", "PROCEDENCIA"]]
        )
        _has_proc = True
    else:
        _sm_proc = None
        _has_proc = False

    # ── Base: one row per SKU from current-month df (already filtered) ────────
    _sm_base_cols = [c for c in [
        "SKU_PRODUCTO", "DESCRIPCION", "AREA", "LINEA", "SUBLINEA", "MARCA",
        "MIX_OFICIAL", "STOCK_ACTUAL", "STOCK_PROYECTADO", "ETA_PENDIENTE",
        "FC_COMPRA", "_AVG_VTA",
    ] if c in df.columns]

    df_sm = df[_sm_base_cols].copy()
    df_sm = df_sm.rename(columns={
        "STOCK_ACTUAL":    "STOCK_ACT",
        "ETA_PENDIENTE":   "ETA_PDTE",
    })

    # Join PROCEDENCIA
    if _has_proc:
        df_sm = df_sm.merge(_sm_proc, on="SKU_PRODUCTO", how="left")

    # Join future stock movements
    df_sm = df_sm.merge(_sm_fut_agg, on="SKU_PRODUCTO", how="left")
    for _c in ["_VTA_F", "_ETA_F", "_FC_F"]:
        if _c in df_sm.columns:
            df_sm[_c] = df_sm[_c].fillna(0)
        else:
            df_sm[_c] = 0.0

    # STOCK_PROY_FN_ANO = end-of-current-month projected stock
    #                     + future months' ETA + future FC - future VTA
    _sm_eom_stock = df_sm.get("STOCK_PROYECTADO", df_sm["STOCK_ACT"])
    df_sm["STOCK_PROY_FN_ANO"] = (
        _sm_eom_stock
        + df_sm["_ETA_F"]
        + df_sm["_FC_F"]
        - df_sm["_VTA_F"]
    ).clip(lower=0)

    # ETA_PDTE total = current month remaining ETA + all future months' scheduled ETAs
    # Current-month ETA (from REAL+FC row) may be 0 when nothing arrives in this month.
    # Future months carry the actual incoming stock schedule.
    df_sm["ETA_PDTE"] = df_sm["ETA_PDTE"].fillna(0) + df_sm["_ETA_F"]

    # VTA_PROY y FC_COMPRA = suma de columnas de proyección para meses >= mes actual
    _sm_remain_raw = raw[
        (raw["PERIODO_ANO"] == _sm_año) &
        (raw["PERIODO_MES"] >= _sm_mes) &
        (raw["TIPO_DATO"].isin(["REAL+FC", "PROYECCION"]))
    ].copy()
    _sm_remain_raw["_VTA_R"] = (
        _sm_remain_raw["VENTA_FUL_TIENDA_UND"].fillna(0)
        + _sm_remain_raw["VENTA_FUL_ETAIL_UND"].fillna(0)
        + _sm_remain_raw["VENTA_FUL_MAYOR_UND"].fillna(0)
    )
    _sm_remain_raw["_FC_R"] = _sm_remain_raw["FORECAST_COMPRA"].fillna(0)

    # Meses con venta > 0 por SKU (para promediar solo sobre meses activos)
    _sm_meses_vta = (
        _sm_remain_raw[_sm_remain_raw["_VTA_R"] > 0]
        .groupby("SKU_PRODUCTO")["PERIODO_MES"]
        .nunique()
        .reset_index()
        .rename(columns={"PERIODO_MES": "_MESES_CON_VTA"})
    )
    _sm_remain_agg = (
        _sm_remain_raw.groupby("SKU_PRODUCTO", as_index=False)[["_VTA_R", "_FC_R"]].sum()
        .rename(columns={"_VTA_R": "VTA_PROY", "_FC_R": "FC_COMPRA"})
        .merge(_sm_meses_vta, on="SKU_PRODUCTO", how="left")
    )
    df_sm = df_sm.drop(columns=["FC_COMPRA"], errors="ignore")
    df_sm = df_sm.merge(_sm_remain_agg, on="SKU_PRODUCTO", how="left")

    # Meses restantes del año (inclusive del mes actual: Jun-Dic = 7)
    _sm_meses_rest = 13 - _sm_mes

    # Divisor para VTA_PROM = meses con venta efectiva.
    # Si no hay ninguno (NaN o 0), usar meses restantes como fallback.
    _sm_divisor = df_sm["_MESES_CON_VTA"].fillna(0)
    _sm_divisor = _sm_divisor.where(_sm_divisor > 0, _sm_meses_rest)

    # Promedio mensual = VTA_PROY / meses con venta efectiva
    _sm_nav = df_sm["VTA_PROY"].fillna(0) / _sm_divisor

    df_sm["MOI_ACT"]       = np.where(_sm_nav > 0, df_sm["STOCK_ACT"]         / _sm_nav, np.nan)
    df_sm["MOI_PRY_FN_ANO"]= np.where(_sm_nav > 0, df_sm["STOCK_PROY_FN_ANO"] / _sm_nav, np.nan)
    df_sm["VTA_PROM"]      = np.where(df_sm["VTA_PROY"].fillna(0) > 0, _sm_nav, np.nan)

    # DIAGNOSTICO de stock
    df_sm["DIAGNOSTICO"] = _calc_diag_stock(
        pd.Series(df_sm["MOI_ACT"].values, index=df_sm.index),
        pd.Series(df_sm["MOI_PRY_FN_ANO"].values, index=df_sm.index),
    ).values

    if df_sm.empty:
        st.info("Sin datos de stock con los filtros aplicados.")
    else:
        # ── Pareto filter ─────────────────────────────────────────────────────
        with st.expander("Filtros — Gestión de Stock", expanded=False):
            _sm_pareto = st.checkbox(
                "Mostrar solo SKUs que representan el 80% de la Venta Anual (Pareto)",
                value=st.session_state.get("sm_pareto", False),
                key="sm_pareto",
            )

        if _sm_pareto and "VTA_PROY" in df_sm.columns:
            _sm_vta_total = df_sm["VTA_PROY"].fillna(0).sum()
            if _sm_vta_total > 0:
                _sm_sorted = df_sm.sort_values("VTA_PROY", ascending=False).reset_index(drop=True)
                _sm_cumsum = _sm_sorted["VTA_PROY"].fillna(0).cumsum()
                _sm_cutoff = int((_sm_cumsum < _sm_vta_total * 0.80).sum()) + 1
                df_sm = _sm_sorted.iloc[:_sm_cutoff].copy()

        # ── Column definitions ────────────────────────────────────────────────
        _ALL_SM_COLS: dict[str, tuple[str, st.column_config._column.Column]] = {
            "AREA":              ("Area",              st.column_config.TextColumn("Area",              width="small")),
            "LINEA":             ("Linea",             st.column_config.TextColumn("Linea",             width="medium")),
            "SUBLINEA":          ("Sublinea",          st.column_config.TextColumn("Sublinea",          width="medium")),
            "MARCA":             ("Marca",             st.column_config.TextColumn("Marca",             width="medium")),
            "MIX_OFICIAL":       ("Mix",               st.column_config.TextColumn("Mix",               width="small")),
            "PROCEDENCIA":       ("Procedencia",       st.column_config.TextColumn("Procedencia",       width="small")),
            "SKU_PRODUCTO":      ("SKU",               st.column_config.TextColumn("SKU",               width="small")),
            "DESCRIPCION":       ("Descripcion",       st.column_config.TextColumn("Descripcion",       width="medium")),
            "STOCK_ACT":         ("Stock Act",         st.column_config.NumberColumn("Stock Act",         format="%,.0f",  width="small")),
            "STOCK_PROY_FN_ANO": ("Stock Proy Fn Año", st.column_config.NumberColumn("Stock Proy Fn Año",format="%,.0f",  width="small")),
            "ETA_PDTE":          ("ETA Pdte",          st.column_config.NumberColumn("ETA Pdte",          format="%,.0f",  width="small")),
            "FC_COMPRA":         ("FC Compra",         st.column_config.NumberColumn("FC Compra",         format="%,.0f",  width="small")),
            "VTA_PROY":          ("Vta Proy",          st.column_config.NumberColumn("Vta Proy",          format="%,.0f",  width="small")),
            "VTA_PROM":          ("Vta Prom",          st.column_config.NumberColumn("Vta Prom",          format="%,.0f",  width="small")),
            "MOI_ACT":           ("MOI Act",           st.column_config.NumberColumn("MOI Act",           format="%.2f",   width="small")),
            "MOI_PRY_FN_ANO":    ("MOI Pry Fn Año",    st.column_config.NumberColumn("MOI Pry Fn Año",    format="%.2f",   width="small")),
            "DIAGNOSTICO":       ("Diagnostico",       st.column_config.TextColumn("Diagnostico",        width="medium")),
        }
        _SM_ALL_KEYS = list(_ALL_SM_COLS.keys())
        _SM_DEFAULT  = [
            "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL", "PROCEDENCIA",
            "SKU_PRODUCTO", "DESCRIPCION",
            "STOCK_ACT", "STOCK_PROY_FN_ANO", "ETA_PDTE", "FC_COMPRA",
            "VTA_PROY", "VTA_PROM",
            "MOI_ACT", "MOI_PRY_FN_ANO", "DIAGNOSTICO",
        ]
        _SM_VERSION = "v3"

        # Version-based reset so new defaults take effect automatically
        if st.session_state.get("_sm_version") != _SM_VERSION:
            st.session_state["sm_cols"]    = _SM_DEFAULT
            st.session_state["_sm_version"] = _SM_VERSION

        # ── Column config panel ───────────────────────────────────────────────
        with st.expander("⚙️ Configurar columnas — Gestión de Stock", expanded=False):
            sm_selected: list[str] = st.multiselect(
                "Columnas visibles",
                options=_SM_ALL_KEYS,
                default=st.session_state.get("sm_cols", _SM_DEFAULT),
                format_func=lambda k: _ALL_SM_COLS[k][0],
                key="sm_cols",
            )
            if sm_selected:
                st.caption("Arrastra las fichas para cambiar el orden:")
                _sm_lbl2key   = {v[0]: k for k, v in _ALL_SM_COLS.items()}
                _sm_order_key = "sm_col_order_" + "_".join(sorted(sm_selected))
                _sm_widget_raw = st.session_state.get(_sm_order_key)
                _sm_prev_labels: list[str] = []
                if isinstance(_sm_widget_raw, list) and _sm_widget_raw:
                    _first = _sm_widget_raw[0]
                    if isinstance(_first, dict) and "items" in _first:
                        _sm_prev_labels = [l for l in _first["items"] if isinstance(l, str)]
                    elif isinstance(_first, str):
                        _sm_prev_labels = [l for l in _sm_widget_raw if isinstance(l, str)]
                _sm_prev_keys = (
                    [_sm_lbl2key[l] for l in _sm_prev_labels if l in _sm_lbl2key]
                    if _sm_prev_labels else sm_selected
                )
                _sm_sel_set  = set(sm_selected)
                _sm_ordered  = [c for c in _sm_prev_keys if c in _sm_sel_set]
                _sm_ordered += [c for c in sm_selected if c not in set(_sm_ordered)]
                _sm_sorted   = _sort_items(
                    [_ALL_SM_COLS[k][0] for k in _sm_ordered],
                    direction="horizontal",
                    key=_sm_order_key,
                )
                sm_selected = [_sm_lbl2key[l] for l in _sm_sorted if l in _sm_lbl2key]

        if not sm_selected:
            sm_selected = _SM_DEFAULT

        _sm_col_cfg  = {k: v[1] for k, v in _ALL_SM_COLS.items()}
        _sm_visible  = [c for c in sm_selected if c in df_sm.columns]

        # Fill NaN for display (leave DIAGNOSTICO as-is)
        df_sm_disp = df_sm.copy()
        _sm_num_disp = df_sm_disp[_sm_visible].select_dtypes(include="number").columns
        df_sm_disp[_sm_num_disp] = df_sm_disp[_sm_num_disp].fillna(0)

        # Explicit rounding for correct display precision
        for _rc in ["STOCK_ACT", "STOCK_PROY_FN_ANO", "ETA_PDTE", "FC_COMPRA", "VTA_PROY", "VTA_PROM"]:
            if _rc in df_sm_disp.columns:
                df_sm_disp[_rc] = df_sm_disp[_rc].round(0)
        for _rc in ["MOI_ACT", "MOI_PRY_FN_ANO"]:
            if _rc in df_sm_disp.columns:
                df_sm_disp[_rc] = df_sm_disp[_rc].round(2)

        # Sort for display
        _sm_sort_cols = [c for c in ["AREA", "LINEA", "SKU_PRODUCTO"] if c in df_sm_disp.columns]
        if _sm_sort_cols:
            df_sm_disp = df_sm_disp.sort_values(
                _sm_sort_cols,
                key=lambda col: col.astype(str).str.lower(),
                na_position="last",
            ).reset_index(drop=True)

        _sm_pareto_label = " (Pareto 80%)" if _sm_pareto else ""
        st.markdown(
            f"**{len(df_sm_disp):,} SKUs{_sm_pareto_label}** · {len(_sm_visible)} columnas"
        )
        st.dataframe(
            df_sm_disp[_sm_visible],
            use_container_width=True,
            hide_index=True,
            height=500,
            column_config=_sm_col_cfg,
        )
        download_buttons(
            df_sm_disp[_sm_visible],
            f"gestion_stock_{mes_label.replace(' ', '_')}",
            excel_buffer=_excel_autofit(
                df_sm_disp[_sm_visible], sheet_name="Gestión Stock"
            ),
        )
