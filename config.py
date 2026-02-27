COLORS = {
    # -- Brand core --
    "primary":           "#065E8B",
    "secondary":         "#632CFF",
    "tertiary_blue":     "#2DAAFF",
    "tertiary_teal":     "#23CED3",
    "tertiary_pink":     "#C94BFF",
    # -- Neutrals --
    "black":             "#2A2927",
    "dark_gray":         "#605D57",
    "medium_gray":       "#B0AEAA",
    "light_gray":        "#D8D7D5",
    "super_light_gray":  "#E5E5E5",
    "white":             "#FFFFFF",
    # -- Dark sidebar --
    "sidebar_bg":        "#1a2332",
    "sidebar_bg_light":  "#1e293b",
    "sidebar_text":      "#94a3b8",
    "sidebar_text_active": "#f1f5f9",
    "sidebar_active_bg": "#2563eb",
    "sidebar_category":  "#64748b",
    "sidebar_border":    "#334155",
    # -- Status / KPI --
    "status_on_track":   "#10b981",
    "status_en_curso":   "#0ea5e9",
    "status_at_risk":    "#f59e0b",
    "status_critical":   "#ef4444",
    # -- Page background --
    "page_bg":           "#f8fafc",
}

# ── Country configuration ──────────────────────────────────────────────────
# CONFIGURACION DOREL PERU
COUNTRY = "PERU"
CURRENCY = "PEN"

# ── Financial defaults ─────────────────────────────────────────────────────
TC_USD_DEFAULT = 3.80      # Tipo de cambio budget USD→PEN (soles)
TC_USD_CLP_DEFAULT = TC_USD_DEFAULT  # Alias para compatibilidad con código existente

DIMENSIONES_VENTA = {
    1:  {"nombre": "sku_producto",          "sql": "a.sku_producto",          "tipo": "agrupacion"},
    2:  {"nombre": "id_sucursal",           "sql": "b.id_sucursal",           "tipo": "agrupacion"},
    3:  {"nombre": "canal_de_distribucion", "sql": "b.canal_de_distribucion", "tipo": "agrupacion"},
    4:  {"nombre": "fecha",                 "sql": "a.fecha",                 "tipo": "agrupacion"},
    5:  {"nombre": "area",                  "sql": "c.area",                  "tipo": "agrupacion"},
    6:  {"nombre": "linea",                 "sql": "c.linea",                 "tipo": "agrupacion"},
    7:  {"nombre": "sublinea",              "sql": "c.sublinea",              "tipo": "agrupacion"},
    8:  {"nombre": "marca",                 "sql": "c.marca",                 "tipo": "agrupacion"},
    9:  {"nombre": "modelo",                "sql": "c.modelo",                "tipo": "agrupacion"},
    10: {"nombre": "nom_producto",          "sql": "c.nom_producto",          "tipo": "descriptivo"},
    11: {"nombre": "procedencia",           "sql": "c.procedencia",           "tipo": "descriptivo"},
    12: {"nombre": "proveedor",             "sql": "c.proveedor",             "tipo": "descriptivo"},
    13: {"nombre": "mix_oficial",           "sql": "c.mix_oficial",           "tipo": "descriptivo"},
    14: {"nombre": "cod_proveedor",         "sql": "c.cod_proveedor",         "tipo": "descriptivo"},
}


# ── Product Manager → AREA / LINEA mapping ────────────────────────────────
# Each PM entry has:
#   areas       – full AREA ownership (ALL lineas in that area)
#   bebe_lineas – specific LINEAs they own **within AREA=BEBE only**
#   jefa        – True if team lead / oversight (can see all)
#
# TODO: Completar con el mapeo de PMs de Dorel Perú.
# Por ahora vacío — el filtro de PM estará deshabilitado en la app.
#
# Usage:  filter_by_pm(df, pm_name) → boolean mask.
PM_MAPPING = {
    # Ejemplo de estructura:
    # "Nombre PM Peru": {
    #     "areas":       ["AREA1", "AREA2"],
    #     "bebe_lineas": ["LINEA1"],
    # },
}

PM_NAMES = list(PM_MAPPING.keys())


def filter_by_pm(df, pm_name):
    """Return boolean mask for rows that belong to *pm_name*.

    Logic:
    - ``areas: ["*"]`` → all rows match (jefa / oversight).
    - A row matches if its AREA is in the PM's *areas* (full ownership)
      **or** if ``AREA == "BEBE"`` and ``LINEA`` is in *bebe_lineas*.

    Requires columns AREA and/or LINEA in *df*.
    """
    import pandas as pd

    cfg = PM_MAPPING.get(pm_name)
    if cfg is None:
        return pd.Series(False, index=df.index)

    if "*" in cfg.get("areas", []):
        return pd.Series(True, index=df.index)

    mask = pd.Series(False, index=df.index)

    areas = cfg.get("areas", [])
    bebe_lineas = cfg.get("bebe_lineas", [])

    # Full area ownership
    if areas and "AREA" in df.columns:
        mask |= df["AREA"].isin(areas)

    # Partial BEBE: only specific lineas within AREA=BEBE
    if bebe_lineas and "AREA" in df.columns and "LINEA" in df.columns:
        mask |= (df["AREA"] == "BEBE") & df["LINEA"].isin(bebe_lineas)

    return mask


def apply_pm_filter(df):
    """Apply the sidebar PM filter to *df*.

    Reads ``st.session_state["sidebar_pm_filter"]``.  When "Todos" is
    selected (or no selection exists), returns *df* unchanged.

    If *df* lacks both AREA and LINEA columns, returns *df* unchanged
    (nothing to filter on — e.g. sucursal/config tables).
    """
    import streamlit as st

    pm_name = st.session_state.get("sidebar_pm_filter", "Todos")
    if pm_name == "Todos":
        return df

    # Guard: nothing to filter on
    if "AREA" not in df.columns and "LINEA" not in df.columns:
        return df

    mask = filter_by_pm(df, pm_name)
    return df[mask].copy()


def get_css():
    c = COLORS
    return f"""
<style>
    /* ================================================================
       GLOBAL: Typography & Page Background
       ================================================================ */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    html, body, [class*="css"] {{
        font-family: 'Inter', sans-serif;
        color: {c['black']};
        -webkit-font-smoothing: antialiased;
    }}

    .stApp {{
        background-color: {c['page_bg']};
    }}

    h1, h2, h3 {{
        color: {c['black']} !important;
        font-weight: 600;
    }}

    /* ================================================================
       SIDEBAR: Dark Navy Theme
       ================================================================ */
    section[data-testid="stSidebar"] {{
        background-color: {c['sidebar_bg']};
        border-right: 1px solid {c['sidebar_border']};
    }}

    /* All sidebar text defaults to muted slate */
    section[data-testid="stSidebar"],
    section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] span,
    section[data-testid="stSidebar"] label,
    section[data-testid="stSidebar"] .stMarkdown {{
        color: {c['sidebar_text']} !important;
    }}

    /* Logo: invert dark logo to white on dark sidebar */
    section[data-testid="stSidebar"] img {{
        filter: brightness(0) invert(1);
        opacity: 0.9;
        padding: 0.5rem 1rem;
    }}

    /* Sidebar dividers */
    section[data-testid="stSidebar"] hr {{
        border-color: {c['sidebar_border']};
        margin: 0.5rem 0;
    }}

    /* ── Sidebar navigation buttons (inactive) ── */
    section[data-testid="stSidebar"] div.stButton > button {{
        background: transparent;
        color: {c['sidebar_text']};
        border: none;
        border-radius: 8px;
        font-weight: 500;
        font-size: 0.85rem;
        text-align: left;
        padding: 0.5rem 0.75rem;
        margin: 1px 0;
        transition: all 0.2s ease;
        display: flex !important;
        justify-content: flex-start !important;
        align-items: center !important;
    }}
    section[data-testid="stSidebar"] div.stButton > button p,
    section[data-testid="stSidebar"] div.stButton > button span,
    section[data-testid="stSidebar"] div.stButton > button div {{
        text-align: left !important;
        width: 100%;
    }}
    section[data-testid="stSidebar"] div.stButton > button:hover {{
        background: {c['sidebar_bg_light']};
        color: {c['sidebar_text_active']};
        transform: none;
        box-shadow: none;
    }}

    /* ── Active module button ── */
    section[data-testid="stSidebar"] div.stButton > button[kind="primary"] {{
        background: {c['sidebar_active_bg']};
        color: #ffffff;
        font-weight: 600;
        box-shadow: 0 2px 8px rgba(37, 99, 235, 0.3);
        border-radius: 8px;
        display: flex !important;
        justify-content: flex-start !important;
    }}
    section[data-testid="stSidebar"] div.stButton > button[kind="primary"]:hover {{
        background: #1d4ed8;
        box-shadow: 0 4px 12px rgba(37, 99, 235, 0.4);
    }}

    /* ── Sidebar expanders (category groups) ── */
    section[data-testid="stSidebar"] details {{
        border: none !important;
        background: transparent !important;
        border-radius: 8px;
        margin-bottom: 0.15rem;
    }}
    section[data-testid="stSidebar"] summary {{
        font-weight: 600;
        font-size: 0.7rem;
        color: {c['sidebar_category']} !important;
        text-transform: uppercase;
        letter-spacing: 1px;
        padding: 0.5rem 0.5rem;
        border-radius: 8px;
    }}
    section[data-testid="stSidebar"] summary:hover {{
        color: {c['sidebar_text']} !important;
        background: {c['sidebar_bg_light']};
    }}
    /* Chevron icon in sidebar expanders */
    section[data-testid="stSidebar"] summary svg {{
        color: {c['sidebar_category']} !important;
    }}

    /* ================================================================
       MAIN CONTENT: Cards, Buttons, DataFrames
       ================================================================ */

    /* DataFrames & Charts container */
    .stDataFrame, .stPlotlyChart {{
        background-color: {c['white']};
        padding: 1rem;
        border-radius: 12px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
    }}

    /* Main content buttons (non-sidebar) */
    div.stButton > button:first-child {{
        background: linear-gradient(90deg, {c['primary']} 0%, {c['tertiary_blue']} 100%);
        color: white;
        border: none;
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.3s ease;
    }}
    div.stButton > button:first-child:hover {{
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(6, 94, 139, 0.3);
    }}

    /* Expander headers in main content */
    .streamlit-expanderHeader {{
        font-weight: 600;
        color: {c['dark_gray']};
        background-color: {c['white']};
        border-radius: 8px;
    }}

    /* Alert messages */
    .stSuccess, .stInfo, .stWarning, .stError {{
        border-radius: 8px;
        border: none;
        box-shadow: 0 2px 5px rgba(0,0,0,0.05);
    }}

    /* Sub-header with left border accent */
    .sub-header {{
        font-size: 1.25rem;
        color: {c['dark_gray']};
        margin-top: 1rem;
        margin-bottom: 0.5rem;
        border-left: 4px solid {c['secondary']};
        padding-left: 10px;
    }}

    /* ── Streamlit native metric cards ── */
    div[data-testid="stMetric"] {{
        background: {c['white']};
        padding: 1rem 1.25rem;
        border-radius: 10px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06);
        border-left: 4px solid {c['primary']};
    }}
    div[data-testid="stMetric"] label {{
        color: {c['sidebar_category']} !important;
        font-size: 0.8rem !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}
    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {{
        font-size: 1.6rem !important;
        font-weight: 700 !important;
        color: {c['black']} !important;
    }}

    /* ================================================================
       KPI CARD SYSTEM (used by utils/ui_components.py)
       ================================================================ */

    /* Professional KPI card */
    .kpi-card-pro {{
        background: {c['white']};
        border-radius: 12px;
        padding: 1.5rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
        border-left: 4px solid var(--card-accent, {c['status_en_curso']});
        position: relative;
        transition: box-shadow 0.2s ease;
        margin-bottom: 1rem;
    }}
    .kpi-card-pro:hover {{
        box-shadow: 0 4px 12px rgba(0,0,0,0.1);
    }}

    .kpi-card-pro .kpi-badge {{
        display: inline-block;
        font-size: 0.65rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        padding: 0.2rem 0.6rem;
        border-radius: 50px;
        margin-bottom: 0.5rem;
    }}
    .kpi-card-pro .kpi-status {{
        font-size: 0.75rem;
        font-weight: 500;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        float: right;
        margin-top: 2px;
    }}
    .kpi-card-pro .kpi-title {{
        font-size: 1.1rem;
        font-weight: 700;
        color: {c['black']};
        margin: 0.3rem 0 0.15rem 0;
        line-height: 1.3;
    }}
    .kpi-card-pro .kpi-description {{
        font-size: 0.8rem;
        color: {c['sidebar_text']};
        margin-bottom: 0.75rem;
    }}
    .kpi-card-pro .kpi-value-row {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin: 0.75rem 0;
    }}
    .kpi-card-pro .kpi-big-value {{
        font-size: 1.4rem;
        font-weight: 700;
        color: {c['black']};
    }}
    .kpi-card-pro .kpi-target {{
        font-size: 0.95rem;
        font-weight: 400;
        color: {c['medium_gray']};
    }}
    .kpi-card-pro .kpi-link {{
        font-size: 0.8rem;
        color: {c['sidebar_active_bg']};
        cursor: pointer;
        font-weight: 500;
    }}

    /* Sub-metric progress bar */
    .sub-metric {{
        margin-bottom: 0.6rem;
    }}
    .sub-metric .sub-metric-header {{
        display: flex;
        justify-content: space-between;
        font-size: 0.75rem;
        color: {c['sidebar_category']};
        margin-bottom: 3px;
    }}
    .sub-metric .sub-metric-bar {{
        height: 6px;
        background: #e2e8f0;
        border-radius: 3px;
        overflow: hidden;
    }}
    .sub-metric .sub-metric-fill {{
        height: 100%;
        border-radius: 3px;
        transition: width 0.5s ease;
    }}

    /* Page header bar */
    .page-header {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        padding: 0.5rem 0 1.5rem 0;
        border-bottom: 1px solid #e2e8f0;
        margin-bottom: 1.5rem;
    }}
    .page-header .header-left h1 {{
        font-size: 1.5rem !important;
        font-weight: 700 !important;
        color: {c['black']} !important;
        margin: 0 !important;
        padding: 0 !important;
    }}
    .page-header .header-left p {{
        font-size: 0.85rem;
        color: {c['sidebar_text']};
        margin: 0.25rem 0 0 0;
    }}
    .page-header .header-right {{
        display: flex;
        align-items: center;
        gap: 12px;
    }}
    .page-header .period-pill {{
        background: {c['white']};
        color: {c['black']};
        font-size: 0.8rem;
        font-weight: 600;
        padding: 0.4rem 1rem;
        border-radius: 50px;
        border: 1px solid #e2e8f0;
    }}
    .page-header .avatar-circle {{
        width: 36px;
        height: 36px;
        border-radius: 50%;
        background: linear-gradient(135deg, {c['primary']}, {c['tertiary_blue']});
        color: white;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 600;
        font-size: 0.85rem;
    }}

    /* ── Legacy KPI cards (backwards compat) ── */
    .kpi-card {{
        background: {c['white']};
        padding: 1.5rem;
        border-radius: 10px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06);
        text-align: center;
        border-top: 4px solid {c['tertiary_teal']};
    }}
    .kpi-value {{
        font-size: 2rem;
        font-weight: bold;
        color: {c['primary']};
    }}
    .kpi-label {{
        font-size: 0.9rem;
        color: {c['medium_gray']};
        text-transform: uppercase;
        letter-spacing: 1px;
    }}

    /* ================================================================
       SNOWFLAKE CONNECTION INDICATOR
       ================================================================ */
    .sf-indicator {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 0.75rem;
        font-weight: 500;
        padding: 0.3rem 0.8rem;
        border-radius: 50px;
        border: 1px solid #e2e8f0;
        white-space: nowrap;
    }}
    .sf-indicator.online {{
        color: {c['status_on_track']};
        background: #f0fdf4;
        border-color: #bbf7d0;
    }}
    .sf-indicator.offline {{
        color: {c['status_critical']};
        background: #fef2f2;
        border-color: #fecaca;
    }}

    /* ── User info in header ── */
    .header-user-info {{
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        gap: 2px;
    }}
    .header-user-name {{
        font-size: 0.8rem;
        font-weight: 600;
        color: {c['black']};
        line-height: 1.2;
    }}
    .header-user-role {{
        font-size: 0.65rem;
        color: {c['sidebar_text']};
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}

    /* ── Sidebar user card (bottom) ── */
    .sidebar-user-card {{
        background: {c['sidebar_bg_light']};
        border-radius: 10px;
        padding: 0.75rem;
        margin-top: 0.5rem;
        display: flex;
        align-items: center;
        gap: 10px;
    }}
    .sidebar-user-card .user-avatar {{
        width: 36px;
        height: 36px;
        border-radius: 50%;
        background: linear-gradient(135deg, {c['primary']}, {c['tertiary_blue']});
        color: white;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 600;
        font-size: 0.8rem;
        flex-shrink: 0;
    }}
    .sidebar-user-card .user-details {{
        overflow: hidden;
    }}
    .sidebar-user-card .user-name {{
        font-size: 0.8rem;
        font-weight: 600;
        color: {c['sidebar_text_active']};
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }}
    .sidebar-user-card .user-email {{
        font-size: 0.65rem;
        color: {c['sidebar_category']};
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }}
</style>
"""


def dorel_layout(**overrides):
    """Plotly layout template with Dorel corporate styling.

    Returns a dict suitable for ``go.Figure(layout=dorel_layout(...))``
    or ``fig.update_layout(**dorel_layout(...))``.  Accepts arbitrary
    keyword overrides that are deep-merged on top of the base template,
    so ``dorel_layout(xaxis=dict(title="Mes"))`` preserves the base
    grid styling while adding the title.
    """
    base = dict(
        font=dict(family="Inter, Arial, sans-serif", size=12, color="#333"),
        plot_bgcolor="#FAFAFA",
        paper_bgcolor="#FFFFFF",
        margin=dict(l=20, r=20, t=60, b=40),
        hoverlabel=dict(
            bgcolor="white",
            font_size=12,
            font_family="Inter, Arial, sans-serif",
            bordercolor="#ddd",
        ),
        xaxis=dict(gridcolor="#ECECEC", gridwidth=1, zeroline=False),
        yaxis=dict(gridcolor="#ECECEC", gridwidth=1, zeroline=False),
        legend=dict(
            bgcolor="rgba(255,255,255,0.9)",
            bordercolor="#eee",
            borderwidth=1,
            font=dict(size=11),
        ),
    )
    # Deep merge: for dict-valued keys, merge sub-dicts instead of replacing
    for key, val in overrides.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            base[key] = {**base[key], **val}
        else:
            base[key] = val
    return base
