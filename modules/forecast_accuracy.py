"""Forecast Accuracy — Historical snapshot persistence + accuracy measurement.

Allows uploading Syncro forecast exports (SKU × canal × 24 months),
persisting them as monthly snapshots, and comparing against actual
VCM sales to measure FA%, MAE%, Bias% over time.

Tabs
----
1. Cargar Forecast   — upload & persist Syncro export
2. Historial         — browse / delete saved snapshots
3. Accuracy Snapshot — select snapshot, compare vs actuals
4. Evolucion         — how each snapshot forecasted a given month
"""

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

# ── Constants ────────────────────────────────────────────────────────────────

MESES_ES = {
    1: "ENE", 2: "FEB", 3: "MAR", 4: "ABR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AGO", 9: "SET", 10: "OCT", 11: "NOV", 12: "DIC",
}

CANAL_MAP = {
    "MINOR": "TIENDA", "MAYOR": "MAYORISTA", "ETAIL": "ETAIL",
    "TIENDA": "TIENDA", "MAYORISTA": "MAYORISTA",
    "03": "TIENDA", "02": "MAYORISTA", "06": "ETAIL",
}

ACCURACY_COLORS = {
    "EXCELENTE": "#1B5E20",
    "BUENO": "#43A047",
    "ACEPTABLE": "#FB8C00",
    "MALO": "#E53935",
    "MUY MALO": "#B71C1C",
}
ACCURACY_ORDER = ["EXCELENTE", "BUENO", "ACEPTABLE", "MALO", "MUY MALO"]

BIAS_COLORS = {
    "SOBRE-ESTIMA": "#E53935",
    "NEUTRO": "#43A047",
    "SUB-ESTIMA": "#0D47A1",
}

_SNAPSHOTS_DIR = Path(__file__).resolve().parent.parent / "data" / "forecast_snapshots"
_META_FILE = _SNAPSHOTS_DIR / "metadata.json"

_SPANISH_MONTHS = {
    "ENE": "Jan", "FEB": "Feb", "MAR": "Mar", "ABR": "Apr",
    "MAY": "May", "JUN": "Jun", "JUL": "Jul", "AGO": "Aug",
    "SEP": "Sep", "SET": "Sep", "OCT": "Oct", "NOV": "Nov", "DIC": "Dec",
    "ENERO": "January", "FEBRERO": "February", "MARZO": "March",
    "ABRIL": "April", "MAYO": "May", "JUNIO": "June",
    "JULIO": "July", "AGOSTO": "August", "SEPTIEMBRE": "September",
    "OCTUBRE": "October", "NOVIEMBRE": "November", "DICIEMBRE": "December",
}


# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE LAYER
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_dir():
    _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def _load_meta() -> dict:
    _ensure_dir()
    if _META_FILE.exists():
        try:
            with open(_META_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save_meta(meta: dict):
    _ensure_dir()
    with open(_META_FILE, "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False, default=str)


def _snapshot_path(key: str) -> Path:
    return _SNAPSHOTS_DIR / f"fcst_{key}.parquet"


def _save_snapshot(key: str, df: pd.DataFrame, original_filename: str):
    """Save a forecast snapshot (parquet) + update metadata.json."""
    _ensure_dir()
    path = _snapshot_path(key)
    df.to_parquet(path, index=False)

    meta = _load_meta()
    meta[key] = {
        "original_filename": original_filename,
        "uploaded_at": datetime.now().isoformat(),
        "uploaded_by": st.session_state.get("user_email", "unknown"),
        "uploaded_by_name": st.session_state.get("user_name", "Desconocido"),
        "n_skus": int(df["SKU_PRODUCTO"].nunique()),
        "n_months": int(df["PERIODO"].nunique()),
        "month_range": [
            df["PERIODO"].min().strftime("%Y-%m"),
            df["PERIODO"].max().strftime("%Y-%m"),
        ],
        "file_size_kb": round(path.stat().st_size / 1024, 1),
    }
    _save_meta(meta)


def _delete_snapshot(key: str):
    """Remove snapshot parquet + metadata entry."""
    path = _snapshot_path(key)
    if path.exists():
        path.unlink()
    meta = _load_meta()
    meta.pop(key, None)
    _save_meta(meta)


def _load_snapshot(key: str):
    """Load a snapshot DataFrame from parquet.  Returns None if missing."""
    path = _snapshot_path(key)
    if path.exists():
        try:
            return pd.read_parquet(path)
        except Exception:
            return None
    return None


def _list_snapshots() -> list[dict]:
    """Return all saved snapshots (newest first) with metadata."""
    meta = _load_meta()
    result = []
    for key in sorted(meta.keys(), reverse=True):
        if _snapshot_path(key).exists():
            result.append({"key": key, **meta[key]})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# PARSER — Syncro Forecast Export
# ══════════════════════════════════════════════════════════════════════════════

def _spanish_to_english(text: str) -> str:
    """Replace Spanish month names/abbreviations with English equivalents."""
    t = text.strip()
    for es, en in sorted(_SPANISH_MONTHS.items(), key=lambda x: -len(x[0])):
        t = re.sub(rf'\b{es}\b', en, t, flags=re.IGNORECASE)
    return t


def _detect_month_col(col_name) -> "pd.Timestamp | None":
    """Try to parse a column header as a month.  Returns Timestamp(day=1) or None."""
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

    # Flexible pandas parser (last resort)
    try:
        dt = pd.to_datetime(s_en, dayfirst=True)
        if 2020 <= dt.year <= 2030:
            return pd.Timestamp(dt.replace(day=1))
    except Exception:
        pass

    return None


def _parse_syncro_forecast(file) -> "tuple[pd.DataFrame, str | None]":
    """Parse a Syncro forecast export into long format.

    Expected format: Excel with rows per SKU (x canal/sucursal),
    columns = fixed cols + 24 month columns.

    Returns
    -------
    (DataFrame[SKU_PRODUCTO, CANAL, PERIODO, FC_UND], error_message)
    """
    try:
        df = pd.read_excel(file)
    except Exception as e:
        return pd.DataFrame(), f"Error leyendo Excel: {e}"

    if df.empty:
        return pd.DataFrame(), "Archivo vacio."

    # ── Detect fixed columns ──
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
            "No se encontro columna de SKU. "
            "Se busco: SKU_PRODUCTO, ID_MATERIAL, SKU, MATERIAL, COD_PRODUCTO."
        )

    # ── Detect month columns ──
    fixed_cols = {col_sku, col_canal, col_suc} - {None}
    desc_patterns = [
        "DESC", "NOMBRE", "NOM_", "PRODUCTO", "DESCRIPCION", "MARCA",
        "LINEA", "AREA", "MODELO", "CLASE", "GRUPO", "FAMILIA", "TIPO",
        "PROVEEDOR", "PROCEDENCIA", "MIX", "UNIDAD",
    ]

    month_cols: dict = {}  # original_col_name → parsed Timestamp
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
            "No se detectaron columnas de meses en los headers. "
            "Formatos aceptados: MM/YYYY, MMM-YYYY, YYYY-MM, ENE-2026, etc."
        )

    # ── Melt to long format ──
    id_vars = [col_sku]
    if col_canal:
        id_vars.append(col_canal)
    if col_suc and col_suc not in id_vars:
        id_vars.append(col_suc)

    df_melt = df[id_vars + list(month_cols.keys())].copy()
    df_melt = df_melt.melt(id_vars=id_vars, var_name="_MES_COL", value_name="FC_UND")
    df_melt["PERIODO"] = df_melt["_MES_COL"].map(month_cols)
    df_melt["FC_UND"] = pd.to_numeric(df_melt["FC_UND"], errors="coerce").fillna(0)

    # Standardize SKU
    df_melt.rename(columns={col_sku: "SKU_PRODUCTO"}, inplace=True)
    df_melt["SKU_PRODUCTO"] = df_melt["SKU_PRODUCTO"].astype(str).str.strip()

    # Standardize CANAL
    if col_canal:
        df_melt["CANAL"] = (
            df_melt[col_canal].astype(str).str.strip().str.upper()
            .map(CANAL_MAP).fillna("OTRO")
        )
    else:
        df_melt["CANAL"] = "TOTAL"

    # Aggregate duplicates
    result = df_melt.groupby(
        ["SKU_PRODUCTO", "CANAL", "PERIODO"], as_index=False,
    ).agg(FC_UND=("FC_UND", "sum"))

    # Drop zero rows
    result = result[result["FC_UND"] != 0].reset_index(drop=True)
    return result, None


# ══════════════════════════════════════════════════════════════════════════════
# ACTUALS LOADER
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=86_400, show_spinner=False)
def _load_actuals_24m(_conn_id, _conn=None):
    """24 months of monthly VCM actuals aggregated by SKU x canal."""
    from db.queries import QUERY_VCM_MONTHLY_24M

    df = pd.read_sql(QUERY_VCM_MONTHLY_24M, _conn)
    df = norm_cols(df)
    df["PERIODO"] = pd.to_datetime(df["PERIODO"])
    df["CANAL"] = df["COD_CANAL"].map(CANAL_MAP).fillna("OTRO")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# ACCURACY ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _compute_snapshot_accuracy(
    snap_df: pd.DataFrame,
    actuals_df: pd.DataFrame,
    maestra: pd.DataFrame,
) -> pd.DataFrame:
    """Compare a forecast snapshot against actuals.

    Returns one row per SKU x PERIODO with FC, Actual, FA%, MAE%, Bias%.
    Only includes months with complete actual data.
    """
    if snap_df.empty or actuals_df.empty:
        return pd.DataFrame()

    actual_months = set(actuals_df["PERIODO"].unique())
    snap_past = snap_df[snap_df["PERIODO"].isin(actual_months)].copy()
    if snap_past.empty:
        return pd.DataFrame()

    # Total across channels
    fc_total = snap_past.groupby(
        ["SKU_PRODUCTO", "PERIODO"], as_index=False,
    ).agg(FC_UND=("FC_UND", "sum"))

    act_total = actuals_df.groupby(
        ["SKU_PRODUCTO", "PERIODO"], as_index=False,
    ).agg(
        ACT_UND=("UNIDADES", "sum"),
        ACT_NETO=("NETO", "sum"),
        ACT_APORTE=("APORTE", "sum"),
    )

    df = fc_total.merge(act_total, on=["SKU_PRODUCTO", "PERIODO"], how="inner")
    if df.empty:
        return pd.DataFrame()

    # ── Metrics ──
    df["ERROR_ABS"] = abs(df["FC_UND"] - df["ACT_UND"])
    df["MAE_PCT"] = np.where(
        df["ACT_UND"] > 0, df["ERROR_ABS"] / df["ACT_UND"] * 100, np.nan,
    )
    df["FA_PCT"] = np.clip(100 - df["MAE_PCT"].fillna(100), 0, 100)
    df["BIAS_PCT"] = np.where(
        df["ACT_UND"] > 0,
        (df["FC_UND"] - df["ACT_UND"]) / df["ACT_UND"] * 100,
        np.nan,
    )
    df["ACCURACY_SEGMENT"] = np.select(
        [df["FA_PCT"] >= 80, df["FA_PCT"] >= 60,
         df["FA_PCT"] >= 40, df["FA_PCT"] >= 20],
        ["EXCELENTE", "BUENO", "ACEPTABLE", "MALO"],
        default="MUY MALO",
    )
    df["BIAS_SEGMENT"] = np.select(
        [df["BIAS_PCT"] > 15, df["BIAS_PCT"] < -15],
        ["SOBRE-ESTIMA", "SUB-ESTIMA"],
        default="NEUTRO",
    )

    # ── Merge maestra ──
    maestra_cols = [
        "SKU_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA",
        "SKU_NOM_PRODUCTO", "MIX_OFICIAL", "COD_PM",
    ]
    available = [c for c in maestra_cols if c in maestra.columns]
    if available:
        maestra_dedup = maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
        df = df.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")

    df["MES_LABEL"] = (
        df["PERIODO"].dt.month.map(MESES_ES) + " " + df["PERIODO"].dt.year.astype(str)
    )
    return df.sort_values(["PERIODO", "SKU_PRODUCTO"]).reset_index(drop=True)


def _compute_evolution(
    target_month: "pd.Timestamp",
    actuals_df: pd.DataFrame,
) -> pd.DataFrame:
    """For a given target month show how each snapshot forecasted it.

    Returns one row per snapshot: FC_total, Actual_total, FA%, months_ahead.
    """
    snapshots = _list_snapshots()
    if not snapshots:
        return pd.DataFrame()

    act_month = actuals_df[actuals_df["PERIODO"] == target_month]
    act_total = float(act_month["UNIDADES"].sum()) if not act_month.empty else 0.0

    rows = []
    for snap in snapshots:
        snap_df = _load_snapshot(snap["key"])
        if snap_df is None:
            continue
        snap_df["PERIODO"] = pd.to_datetime(snap_df["PERIODO"])
        fc_month = snap_df[snap_df["PERIODO"] == target_month]
        if fc_month.empty:
            continue

        fc_total = float(fc_month["FC_UND"].sum())
        snap_date = pd.Timestamp(f"{snap['key']}-01")
        months_ahead = (
            (target_month.year - snap_date.year) * 12
            + (target_month.month - snap_date.month)
        )

        error_abs = abs(fc_total - act_total)
        mae_pct = (error_abs / act_total * 100) if act_total > 0 else None
        fa_pct = max(0.0, 100.0 - mae_pct) if mae_pct is not None else None
        bias_pct = ((fc_total - act_total) / act_total * 100) if act_total > 0 else None

        rows.append({
            "SNAPSHOT": snap["key"],
            "SNAPSHOT_LABEL": (
                MESES_ES.get(snap_date.month, "?") + " " + str(snap_date.year)
            ),
            "MESES_ANTICIPACION": months_ahead,
            "FC_TOTAL": fc_total,
            "ACT_TOTAL": act_total,
            "ERROR_ABS": error_abs,
            "FA_PCT": fa_pct,
            "MAE_PCT": mae_pct,
            "BIAS_PCT": bias_pct,
        })

    if not rows:
        return pd.DataFrame()

    return (
        pd.DataFrame(rows)
        .sort_values("MESES_ANTICIPACION", ascending=False)
        .reset_index(drop=True)
    )


# ══════════════════════════════════════════════════════════════════════════════
# UI COMPONENTS
# ══════════════════════════════════════════════════════════════════════════════

def _render_kpis(df):
    """Render 5 KPI cards."""
    valid = df[df["MAE_PCT"].notna()]
    if valid.empty:
        return

    fa_avg = valid["FA_PCT"].mean()
    mae_avg = valid["MAE_PCT"].mean()
    bias_avg = valid["BIAS_PCT"].mean()
    sobre = (valid["BIAS_SEGMENT"] == "SOBRE-ESTIMA").sum()
    sub = (valid["BIAS_SEGMENT"] == "SUB-ESTIMA").sum()

    cols = st.columns(5)
    data = [
        ("FA% Promedio", f"{fa_avg:.0f}%", COLORS["primary"]),
        ("MAE% Promedio", f"{mae_avg:.0f}%", COLORS["tertiary_blue"]),
        ("Bias Promedio", f"{bias_avg:+.0f}%", "#FB8C00"),
        ("Sobre-Estima", f"{sobre:,}", BIAS_COLORS["SOBRE-ESTIMA"]),
        ("Sub-Estima", f"{sub:,}", BIAS_COLORS["SUB-ESTIMA"]),
    ]
    for col, (label, value, color) in zip(cols, data):
        with col:
            st.html(f"""
            <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                         border-top:4px solid {color};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
                <div style='font-size:1.8rem;font-weight:bold;color:{color}'>{value}</div>
                <div style='font-size:0.8rem;color:{COLORS["medium_gray"]};
                     text-transform:uppercase'>{label}</div>
            </div>""")
    st.html("<br>")


def _render_monthly_accuracy(df):
    """Bar chart — FA% by month."""
    by_month = (
        df.groupby("PERIODO", as_index=False)
        .agg(
            FA_PROM=("FA_PCT", "mean"),
            MAE_PROM=("MAE_PCT", "mean"),
            BIAS_PROM=("BIAS_PCT", "mean"),
            N_SKUS=("SKU_PRODUCTO", "nunique"),
        )
        .sort_values("PERIODO")
    )
    by_month["MES_LABEL"] = (
        by_month["PERIODO"].dt.month.map(MESES_ES)
        + " " + by_month["PERIODO"].dt.year.astype(str)
    )

    bar_colors = [
        ACCURACY_COLORS["EXCELENTE"] if v >= 80 else
        ACCURACY_COLORS["BUENO"] if v >= 60 else
        ACCURACY_COLORS["ACEPTABLE"] if v >= 40 else
        ACCURACY_COLORS["MALO"] if v >= 20 else
        ACCURACY_COLORS["MUY MALO"]
        for v in by_month["FA_PROM"]
    ]

    fig = go.Figure(layout=dorel_layout(
        title=dict(text="FA% Promedio por Mes", font_size=14, x=0.5),
        height=380,
        xaxis=dict(title=""),
        yaxis=dict(title="FA%", range=[0, 105]),
        showlegend=False,
    ))
    fig.add_trace(go.Bar(
        x=by_month["MES_LABEL"],
        y=by_month["FA_PROM"],
        marker_color=bar_colors,
        hovertemplate=(
            "%{x}<br>FA: %{y:.0f}%<br>"
            "MAE: %{customdata[0]:.0f}%<br>"
            "Bias: %{customdata[1]:+.0f}%<br>"
            "SKUs: %{customdata[2]:,}<extra></extra>"
        ),
        customdata=np.column_stack([
            by_month["MAE_PROM"], by_month["BIAS_PROM"], by_month["N_SKUS"],
        ]),
    ))
    st.plotly_chart(fig, use_container_width=True)


def _render_accuracy_charts(df):
    """Distribution bar + scatter FC vs Actual."""
    col1, col2 = st.columns(2)

    with col1:
        counts = df["ACCURACY_SEGMENT"].value_counts().reset_index()
        counts.columns = ["Segmento", "Count"]
        cat = [a for a in ACCURACY_ORDER if a in counts["Segmento"].values]
        clr = [ACCURACY_COLORS[a] for a in cat]

        fig1 = go.Figure(layout=dorel_layout(
            title=dict(text="Distribucion de Forecast Accuracy", font_size=14, x=0.5),
            height=380,
            xaxis=dict(title="", categoryorder="array", categoryarray=cat),
            yaxis=dict(title="# SKU-Meses"),
            showlegend=False,
        ))
        for seg, color in zip(cat, clr):
            row = counts[counts["Segmento"] == seg]
            if not row.empty:
                fig1.add_trace(go.Bar(
                    x=[seg], y=[row["Count"].values[0]],
                    marker_color=color,
                    hovertemplate="Segmento: %{x}<br>SKU-Meses: %{y:,}<extra></extra>",
                ))
        st.plotly_chart(fig1, use_container_width=True)

    with col2:
        valid = df[df["ACT_UND"] > 0].copy()
        if not valid.empty:
            max_val = max(
                valid["FC_UND"].quantile(0.95),
                valid["ACT_UND"].quantile(0.95),
            )

            fig2 = go.Figure(layout=dorel_layout(
                title=dict(text="Forecast vs Venta Real", font_size=14, x=0.5),
                height=380,
                xaxis=dict(title="Venta Real (Und)", range=[0, max_val * 1.05]),
                yaxis=dict(title="Forecast (Und)", range=[0, max_val * 1.05]),
                showlegend=True,
                legend=dict(
                    orientation="h", yanchor="top", y=-0.15,
                    xanchor="center", x=0.5,
                ),
            ))
            for seg in ACCURACY_ORDER:
                sub = valid[valid["ACCURACY_SEGMENT"] == seg]
                if sub.empty:
                    continue
                fig2.add_trace(go.Scatter(
                    x=sub["ACT_UND"], y=sub["FC_UND"],
                    mode="markers", name=seg,
                    marker=dict(color=ACCURACY_COLORS[seg], size=6, opacity=0.6),
                    hovertemplate=(
                        "SKU: %{customdata[0]}<br>"
                        "FC: %{y:,.0f}<br>Real: %{x:,.0f}<br>"
                        "FA: %{customdata[1]:.0f}%<extra></extra>"
                    ),
                    customdata=np.column_stack([
                        sub["SKU_PRODUCTO"], sub["FA_PCT"],
                    ]),
                ))
            # Diagonal (perfect accuracy)
            fig2.add_trace(go.Scatter(
                x=[0, max_val], y=[0, max_val],
                mode="lines", name="Perfecto",
                line=dict(color="black", dash="dash", width=1),
                showlegend=False,
            ))
            st.plotly_chart(fig2, use_container_width=True)


def _render_dim_accuracy(df):
    """Bar chart — FA% by Area / Linea / Marca."""
    dim = st.radio(
        "Accuracy por", ["AREA", "LINEA", "MARCA"],
        horizontal=True, key="fca_dim2",
    )
    if dim not in df.columns:
        return

    valid_dim = df[df["FA_PCT"].notna()]
    by_dim = (
        valid_dim.groupby(dim, as_index=False)
        .agg(
            FA_PROM=("FA_PCT", "mean"),
            BIAS_PROM=("BIAS_PCT", "mean"),
            COUNT=("SKU_PRODUCTO", "count"),
        )
        .nlargest(15, "COUNT")
    )

    fig = go.Figure(layout=dorel_layout(
        title=dict(text=f"FA% Promedio por {dim.title()}", font_size=14, x=0.5),
        height=380,
        xaxis=dict(title="", categoryorder="total descending"),
        yaxis=dict(title="FA%"),
        showlegend=False,
    ))
    fig.add_trace(go.Bar(
        x=by_dim[dim], y=by_dim["FA_PROM"],
        marker_color=COLORS["primary"],
        hovertemplate=(
            "%{x}<br>FA: %{y:.0f}%<br>"
            "Bias: %{customdata[0]:+.0f}%<br>"
            "SKU-Meses: %{customdata[1]:,}<extra></extra>"
        ),
        customdata=np.column_stack([by_dim["BIAS_PROM"], by_dim["COUNT"]]),
    ))
    st.plotly_chart(fig, use_container_width=True)


def _kpi_card(label, value, color):
    """Single HTML KPI card."""
    st.html(f"""
    <div style='background:#fff;padding:1.2rem;border-radius:10px;text-align:center;
                 border-top:4px solid {color};box-shadow:0 2px 5px rgba(0,0,0,0.05)'>
        <div style='font-size:1.8rem;font-weight:bold;color:{color}'>{value}</div>
        <div style='font-size:0.8rem;color:{COLORS["medium_gray"]};
             text-transform:uppercase'>{label}</div>
    </div>""")


# ══════════════════════════════════════════════════════════════════════════════
# TAB RENDERERS
# ══════════════════════════════════════════════════════════════════════════════

def _render_upload_tab(conn):
    """Tab 1 — Upload & persist a Syncro forecast snapshot."""
    st.markdown("### Cargar Forecast de Syncro")
    st.info(
        "Sube el archivo de forecast descargado de Supply Syncro. "
        "Formato esperado: columnas de SKU + canal + 24 meses "
        "(ENE-2026, FEB-2026, ...). Se guardara como snapshot del mes seleccionado."
    )

    col1, col2 = st.columns([2, 1])
    with col1:
        file = st.file_uploader(
            "Archivo Forecast Syncro (Excel)",
            type=["xlsx", "xls"],
            key="fca_syncro_upload",
        )

    with col2:
        now = datetime.now()
        years = list(range(now.year - 1, now.year + 1))
        months_list = list(range(1, 13))
        sel_year = st.selectbox(
            "Año del snapshot", years,
            index=years.index(now.year), key="fca_snap_year",
        )
        sel_month = st.selectbox(
            "Mes del snapshot", months_list,
            index=now.month - 1,
            format_func=lambda m: MESES_ES[m],
            key="fca_snap_month",
        )

    if file is None:
        # Show existing snapshots summary
        snaps = _list_snapshots()
        if snaps:
            st.markdown("---")
            st.caption(f"ℹ️ Actualmente hay **{len(snaps)}** snapshot(s) guardado(s).")
        return

    with st.spinner("Parseando archivo..."):
        df_parsed, error = _parse_syncro_forecast(file)

    if error:
        st.error(error)
        return
    if df_parsed.empty:
        st.warning("El archivo no contiene datos de forecast validos.")
        return

    # ── Preview ──
    n_skus = df_parsed["SKU_PRODUCTO"].nunique()
    n_months = df_parsed["PERIODO"].nunique()
    min_m = df_parsed["PERIODO"].min().strftime("%b %Y")
    max_m = df_parsed["PERIODO"].max().strftime("%b %Y")
    canales = sorted(df_parsed["CANAL"].unique().tolist())

    st.success(
        f"**{n_skus:,}** SKUs | **{n_months}** meses ({min_m} → {max_m}) | "
        f"Canales: {', '.join(canales)}"
    )

    # Pivot preview
    preview = df_parsed.pivot_table(
        index="SKU_PRODUCTO", columns="PERIODO",
        values="FC_UND", aggfunc="sum", fill_value=0,
    )
    preview.columns = [c.strftime("%b-%Y") for c in preview.columns]
    st.dataframe(preview.head(20), use_container_width=True, height=300)

    # ── Save ──
    snapshot_key = f"{sel_year}-{sel_month:02d}"
    existing = _load_meta()
    if snapshot_key in existing:
        st.warning(
            f"⚠️ Ya existe un snapshot para **{MESES_ES[sel_month]} {sel_year}**. "
            "Si guardas, se reemplazara."
        )

    btn_label = f"💾 Guardar Snapshot ({MESES_ES[sel_month]} {sel_year})"
    if st.button(btn_label, type="primary", key="btn_save_snap"):
        _save_snapshot(snapshot_key, df_parsed, file.name)
        st.success(
            f"Snapshot **{MESES_ES[sel_month]} {sel_year}** guardado exitosamente."
        )
        st.balloons()


def _render_historial_tab():
    """Tab 2 — Browse and manage saved snapshots."""
    st.markdown("### Historial de Snapshots Guardados")

    snapshots = _list_snapshots()
    if not snapshots:
        st.info(
            "No hay snapshots guardados. "
            "Ve a la pestaña **📤 Cargar Forecast** para subir uno."
        )
        return

    rows = []
    for s in snapshots:
        snap_date = pd.Timestamp(f"{s['key']}-01")
        rows.append({
            "Mes": MESES_ES.get(snap_date.month, "?") + " " + str(snap_date.year),
            "Key": s["key"],
            "Archivo Original": s.get("original_filename", "?"),
            "SKUs": s.get("n_skus", "?"),
            "Meses FC": s.get("n_months", "?"),
            "Rango": " → ".join(s.get("month_range", ["?", "?"])),
            "Subido por": s.get("uploaded_by_name", "?"),
            "Fecha Carga": s.get("uploaded_at", "?")[:16],
            "Tamaño KB": s.get("file_size_kb", "?"),
        })

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ── Delete ──
    with st.expander("🗑️ Eliminar Snapshot"):
        snap_keys = [s["key"] for s in snapshots]
        snap_labels = []
        for k in snap_keys:
            sd = pd.Timestamp(f"{k}-01")
            snap_labels.append(
                f"{k} — {MESES_ES.get(sd.month, '?')} {sd.year}"
            )
        sel_del = st.selectbox(
            "Seleccionar snapshot a eliminar",
            snap_labels, key="fca_del_snap",
        )
        if sel_del and st.button("Eliminar", type="secondary", key="btn_del_snap"):
            key_to_del = sel_del.split(" — ")[0]
            _delete_snapshot(key_to_del)
            st.success(f"Snapshot **{key_to_del}** eliminado.")
            st.rerun()


def _render_accuracy_tab(conn):
    """Tab 3 — Select a snapshot, compute accuracy vs actuals."""
    st.markdown("### Accuracy: Snapshot vs Ventas Reales")

    snapshots = _list_snapshots()
    if not snapshots:
        st.info("No hay snapshots guardados. Carga uno primero.")
        return

    # ── Select snapshot ──
    snap_keys = [s["key"] for s in snapshots]
    snap_labels_map = {}
    for k in snap_keys:
        sd = pd.Timestamp(f"{k}-01")
        snap_labels_map[
            f"{MESES_ES.get(sd.month, '?')} {sd.year}"
        ] = k

    sel_label = st.selectbox(
        "Seleccionar Snapshot",
        list(snap_labels_map.keys()),
        key="fca_sel_snap",
    )
    sel_key = snap_labels_map[sel_label]

    # ── Load data ──
    with lottie_spinner("snowflake"):
        snap_df = _load_snapshot(sel_key)
        actuals = _load_actuals_24m(id(conn), _conn=conn)
        maestra = cq.maestra(conn)

    if snap_df is None:
        st.error("No se pudo cargar el snapshot.")
        return

    snap_df["PERIODO"] = pd.to_datetime(snap_df["PERIODO"])
    df_acc = _compute_snapshot_accuracy(snap_df, actuals, maestra)

    if df_acc.empty:
        st.warning(
            "No hay meses pasados para comparar. "
            "Los meses del forecast aun no han transcurrido."
        )
        return

    df_acc = apply_pm_filter(df_acc)
    if df_acc.empty:
        st.warning("Sin datos para el PM seleccionado.")
        return

    # ── Snapshot info ──
    snap_meta = next((s for s in snapshots if s["key"] == sel_key), {})
    n_past = df_acc["PERIODO"].nunique()
    total_months = snap_meta.get("n_months", "?")
    st.caption(
        f"Snapshot **{sel_label}** — {n_past} de {total_months} meses con datos reales. "
        f"Archivo: {snap_meta.get('original_filename', '?')}"
    )

    # ── Filters ──
    st.markdown("### Filtros")
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        areas = sorted(df_acc["AREA"].dropna().unique()) if "AREA" in df_acc.columns else []
        sel_area = st.multiselect("Area", areas, key="fca_area2")
    with fc2:
        _m = df_acc[df_acc["AREA"].isin(sel_area)] if sel_area else df_acc
        lineas = sorted(_m["LINEA"].dropna().unique()) if "LINEA" in _m.columns else []
        sel_linea = st.multiselect("Linea", lineas, key="fca_linea2")
    with fc3:
        _m2 = _m[_m["LINEA"].isin(sel_linea)] if sel_linea else _m
        marcas = sorted(_m2["MARCA"].dropna().unique()) if "MARCA" in _m2.columns else []
        sel_marca = st.multiselect("Marca", marcas, key="fca_marca2")

    mask = pd.Series(True, index=df_acc.index)
    if sel_area:
        mask &= df_acc["AREA"].isin(sel_area)
    if sel_linea:
        mask &= df_acc["LINEA"].isin(sel_linea)
    if sel_marca:
        mask &= df_acc["MARCA"].isin(sel_marca)
    df_filt = df_acc[mask]

    if df_filt.empty:
        st.warning("Sin datos con los filtros seleccionados.")
        return

    # ── KPIs ──
    st.markdown("---")
    _render_kpis(df_filt)

    # ── Monthly accuracy bars ──
    _render_monthly_accuracy(df_filt)

    # ── Distribution + Scatter ──
    st.markdown("---")
    _render_accuracy_charts(df_filt)

    # ── Accuracy by dimension ──
    st.markdown("---")
    _render_dim_accuracy(df_filt)

    # ── Detail table ──
    st.markdown("---")
    st.markdown("### Detalle por SKU × Mes")
    display_cols = [
        "SKU_PRODUCTO", "MES_LABEL", "ACCURACY_SEGMENT", "FA_PCT", "MAE_PCT",
        "BIAS_PCT", "BIAS_SEGMENT", "FC_UND", "ACT_UND", "ERROR_ABS",
    ]
    for c in ["AREA", "LINEA", "MARCA", "SKU_NOM_PRODUCTO"]:
        if c in df_filt.columns:
            display_cols.insert(1, c)
    display_cols = [c for c in display_cols if c in df_filt.columns]

    st.dataframe(
        df_filt[display_cols].sort_values("FA_PCT").reset_index(drop=True),
        use_container_width=True, height=500,
    )
    download_buttons(df_filt[display_cols], "forecast_accuracy_snapshot")


def _render_evolution_tab(conn):
    """Tab 4 — How each snapshot forecasted a given month."""
    st.markdown("### Evolucion: ¿Como se fue ajustando el forecast?")
    st.caption(
        "Selecciona un mes objetivo y observa como cada snapshot lo proyectaba. "
        "Muestra la convergencia del forecast hacia la venta real a medida que "
        "se acerca la fecha."
    )

    snapshots = _list_snapshots()
    if len(snapshots) < 2:
        st.info(
            "Necesitas al menos **2 snapshots** para ver la evolucion temporal. "
            "Carga mas snapshots historicos en la pestaña **📤 Cargar Forecast**."
        )
        return

    # ── Load actuals ──
    with lottie_spinner("snowflake"):
        actuals = _load_actuals_24m(id(conn), _conn=conn)

    if actuals.empty:
        st.warning("No hay ventas reales disponibles.")
        return

    # ── Select target month ──
    actual_months = sorted(actuals["PERIODO"].unique())
    month_labels = {
        m: f"{MESES_ES.get(m.month, '?')} {m.year}" for m in actual_months
    }

    sel_target = st.selectbox(
        "Mes objetivo (con venta real)",
        actual_months,
        index=len(actual_months) - 1,
        format_func=lambda m: month_labels[m],
        key="fca_evo_target",
    )

    # ── Compute ──
    df_evo = _compute_evolution(sel_target, actuals)
    if df_evo.empty:
        st.warning(
            "Ningun snapshot guardado tiene forecast para este mes. "
            "Verifica que los snapshots incluyan el mes seleccionado."
        )
        return

    act_total = df_evo["ACT_TOTAL"].iloc[0]
    target_label = month_labels[sel_target]

    # ── KPIs ──
    c1, c2, c3 = st.columns(3)
    with c1:
        _kpi_card(
            f"Venta Real {target_label} (Und)",
            f"{act_total:,.0f}",
            COLORS["primary"],
        )
    with c2:
        _kpi_card(
            "Snapshots con Forecast",
            f"{len(df_evo)}",
            COLORS["tertiary_blue"],
        )
    with c3:
        best_fa = df_evo["FA_PCT"].max() if df_evo["FA_PCT"].notna().any() else 0
        _kpi_card("Mejor FA%", f"{best_fa:.0f}%", "#43A047")

    st.markdown("---")

    # ── Evolution chart ──
    fig = go.Figure(layout=dorel_layout(
        title=dict(
            text=f"Evolucion del Forecast para {target_label}",
            font_size=14, x=0.5,
        ),
        height=420,
        xaxis=dict(title="Snapshot (mes de carga del forecast)"),
        yaxis=dict(title="Forecast (Und)"),
        showlegend=True,
    ))

    fig.add_trace(go.Bar(
        x=df_evo["SNAPSHOT_LABEL"],
        y=df_evo["FC_TOTAL"],
        name="Forecast",
        marker_color=COLORS["tertiary_blue"],
        hovertemplate=(
            "Snapshot: %{x}<br>"
            "FC: %{y:,.0f} und<br>"
            "Anticipacion: %{customdata[0]} meses<br>"
            "FA: %{customdata[1]:.0f}%<extra></extra>"
        ),
        customdata=np.column_stack([
            df_evo["MESES_ANTICIPACION"],
            df_evo["FA_PCT"].fillna(0),
        ]),
    ))

    fig.add_hline(
        y=act_total,
        line=dict(color=COLORS["primary"], width=2, dash="dash"),
        annotation_text=f"Venta Real: {act_total:,.0f}",
        annotation_position="top left",
    )

    st.plotly_chart(fig, use_container_width=True)

    # ── Convergence line chart ──
    if len(df_evo) >= 3:
        fig_line = go.Figure(layout=dorel_layout(
            title=dict(
                text="Convergencia: Error Absoluto vs Anticipacion",
                font_size=14, x=0.5,
            ),
            height=350,
            xaxis=dict(title="Meses de anticipacion", autorange="reversed"),
            yaxis=dict(title="Error Abs (Und)"),
            showlegend=False,
        ))
        df_sorted = df_evo.sort_values("MESES_ANTICIPACION")
        fig_line.add_trace(go.Scatter(
            x=df_sorted["MESES_ANTICIPACION"],
            y=df_sorted["ERROR_ABS"],
            mode="lines+markers",
            marker=dict(color=COLORS["primary"], size=8),
            line=dict(color=COLORS["primary"], width=2),
            hovertemplate=(
                "Anticipacion: %{x} meses<br>"
                "Error: %{y:,.0f} und<br>"
                "Snapshot: %{customdata[0]}<extra></extra>"
            ),
            customdata=np.column_stack([df_sorted["SNAPSHOT_LABEL"]]),
        ))
        st.plotly_chart(fig_line, use_container_width=True)

    # ── Detail table ──
    st.markdown("### Detalle por Snapshot")
    df_display = df_evo[[
        "SNAPSHOT_LABEL", "MESES_ANTICIPACION", "FC_TOTAL",
        "ACT_TOTAL", "ERROR_ABS", "FA_PCT", "MAE_PCT", "BIAS_PCT",
    ]].rename(columns={
        "SNAPSHOT_LABEL": "Snapshot",
        "MESES_ANTICIPACION": "Meses Antic.",
        "FC_TOTAL": "Forecast (Und)",
        "ACT_TOTAL": "Venta Real (Und)",
        "ERROR_ABS": "Error Abs",
        "FA_PCT": "FA%",
        "MAE_PCT": "MAE%",
        "BIAS_PCT": "Bias%",
    })
    st.dataframe(df_display, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def render_forecast_accuracy(conn):
    st.html("<h2 class='sub-header'>Forecast Accuracy</h2>")
    st.caption(
        "Carga snapshots de forecast Syncro, guarda el historico, "
        "y mide la precision vs ventas reales (FA%, MAE%, Bias%)."
    )

    tab_upload, tab_hist, tab_acc, tab_evo = st.tabs([
        "📤 Cargar Forecast",
        "📋 Historial",
        "📊 Accuracy Snapshot",
        "📈 Evolucion",
    ])

    with tab_upload:
        _render_upload_tab(conn)
    with tab_hist:
        _render_historial_tab()
    with tab_acc:
        _render_accuracy_tab(conn)
    with tab_evo:
        _render_evolution_tab(conn)
