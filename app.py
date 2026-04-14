import os
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from config import get_css, COLORS, PM_NAMES, apply_pm_filter
from db.connection import get_snowflake_connection, get_active_connection
from utils.auth import (
    authenticate,
    change_password,
    get_current_user,
    get_allowed_modules,
    get_area_filter,
    get_user_initials,
)
from modules.ventas import render_ventas
from modules.stock import render_stock
from modules.maestra import render_maestra
from modules.comex import render_comex
from modules.syncro import render_syncro
from modules.forecast import render_forecast_generator
from modules.stock_critico import render_stock_dashboard
from modules.proyeccion import render_proyeccion
from modules.alertas_quiebre import render_alertas_quiebre
from modules.abc_xyz import render_abc_xyz
from modules.elasticidad import render_elasticidad
from modules.higiene import render_higiene
from modules.sell_through import render_sell_through
from modules.tendencia import render_tendencia
from modules.forecast_accuracy import render_forecast_accuracy
from modules.simulador import render_simulador
from modules.desagregacion import render_forecast_diario
from modules.resumen_compra import render_resumen_compra
from modules.plan_compras import render_plan_compras
from modules.sop_control_tower import render_sop_control_tower
from modules.instock_historico import render_instock_historico
from modules.alertas_email import render_alertas_email
from modules.pasillo_infinito import render_pasillo_infinito
from modules.canasta import render_canasta
from modules.redistribucion import render_redistribucion
from modules.analisis_transitos import render_analisis_transitos
from modules.capacidad_volumetrica import render_capacidad_volumetrica
from modules.operaciones_supply import render_operaciones_supply
from modules.venta_perdida import render_venta_perdida
from modules.ventas_forecast_210 import render_ventas_forecast_210
from modules.diagnostico import render_diagnostico
from modules.stock_critico_v2 import render_stock_dashboard_v2
from modules.business_case import render_business_case
from modules.listado_oe import render_listado_oe
from modules.ddmrp import render_ddmrp
from modules.fcst_vs_vta_retail import render_fcst_vs_vta_retail
from db.cache import cached_query as cq, clear_all as clear_query_cache, last_refresh_label, auto_refresh_if_new_day
from utils.ui_animations import lottie_spinner, show_lottie, animated_kpi_row

# ============================================================================
# PAGE CONFIG
# ============================================================================
st.set_page_config(
    page_title="Portal de Planificacion Dorel Peru",
    page_icon="🚄",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Logo Corporativo
logo_path = os.path.join(os.path.dirname(__file__), "logo_dorel.png")
if os.path.exists(logo_path):
    st.sidebar.image(logo_path, use_container_width=True)

# CSS (st.html para compatibilidad con Streamlit >=1.42 que deprecó unsafe_allow_html)
st.html(get_css())


# ============================================================================
# SNOWFLAKE CONNECTION CHECK
# ============================================================================
def _check_snowflake_connection() -> bool:
    """Return True if Snowflake connection is alive, False otherwise."""
    try:
        conn = get_snowflake_connection()
        conn.cursor().execute("SELECT 1")
        return True
    except Exception:
        return False


# ============================================================================
# LOGIN
# ============================================================================
def login_screen():
    st.html(
        "<h1 style='text-align: center; margin-top: 3rem;'>"
        "Portal de Planificacion Dorel Peru</h1>"
    )
    st.html(
        "<p style='text-align: center; color: #94a3b8; margin-bottom: 2rem;'>"
        "Ingresa con tu correo corporativo</p>"
    )

    col1, col2, col3 = st.columns([1, 1.5, 1])
    with col2:
        with st.form("login_form"):
            email = st.text_input("Correo electronico", placeholder="nombre@dorel.cl")
            password = st.text_input("Contrasena", type="password")
            submitted = st.form_submit_button("Ingresar", use_container_width=True)

            if submitted:
                if not email or not password:
                    st.error("Ingresa correo y contrasena")
                else:
                    user = authenticate(email, password)
                    if user is not None:
                        st.session_state["authenticated"] = True
                        st.session_state["user"] = user
                        st.rerun()
                    else:
                        st.error("Credenciales incorrectas o usuario inactivo")


# ============================================================================
# FORCED PASSWORD CHANGE (first login)
# ============================================================================
_VALID_PHRASES = {
    "la u es chile",
    "la u es de chile",
    "u es chile",
    "la u es chile!",
    "la u es de chile!",
}


def _phrase_accepted(text: str) -> bool:
    """Check if the user typed an acceptable variant of the magic phrase."""
    normalized = text.strip().lower()
    # Remove trailing punctuation for flexibility
    cleaned = normalized.rstrip("!.,;")
    return cleaned in _VALID_PHRASES or normalized in _VALID_PHRASES


def change_password_screen():
    """Intermediate screen forcing a password change — with a fun twist."""
    user = get_current_user()
    if user is None:
        st.session_state["authenticated"] = False
        st.rerun()
        return

    nombre = user.get("nombre", "Usuario")
    first_name = nombre.split()[0] if nombre else "Usuario"

    st.html(
        "<h1 style='text-align:center; margin-top:2rem;'>"
        "Cambio de Contrasena Obligatorio</h1>"
    )
    st.html(
        "<p style='text-align:center; color:#94a3b8; margin-bottom:1.5rem;'>"
        "Por seguridad, debes cambiar tu contrasena antes de continuar.</p>"
    )

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        # ── Step 1: The prank challenge ──
        st.html(
            f"""
            <div style="background: linear-gradient(135deg, #1e3a5f 0%, #065E8B 100%);
                        padding: 2rem; border-radius: 16px; text-align: center;
                        margin-bottom: 1.5rem; box-shadow: 0 4px 20px rgba(6,94,139,0.3);">
                <div style="font-size: 2.5rem; margin-bottom: 0.5rem;">🏆</div>
                <div style="color: #f1f5f9; font-size: 1.5rem; font-weight: 800;
                            text-transform: uppercase; letter-spacing: 2px;
                            line-height: 1.4;">
                    {first_name.upper()}<br>DI QUE LA U ES CHILE<br>ESCRIBELO
                </div>
                <div style="color: #94a3b8; font-size: 0.8rem; margin-top: 0.75rem;">
                    Protocolo de seguridad obligatorio
                </div>
            </div>
            """
        )

        phrase_input = st.text_input(
            "Escribe la frase para continuar",
            placeholder="Escribe aqui...",
            key="phrase_challenge",
        )

        phrase_ok = _phrase_accepted(phrase_input) if phrase_input else False

        if phrase_input and not phrase_ok:
            st.error("Esa no es la frase correcta. Intentalo de nuevo.")

        if phrase_ok:
            st.balloons()
            st.success("Asi se habla! Ahora puedes cambiar tu contrasena.")

            st.markdown("---")
            with st.form("change_pwd_form"):
                new_pwd = st.text_input("Nueva contrasena", type="password")
                confirm_pwd = st.text_input("Confirmar contrasena", type="password")
                submitted = st.form_submit_button(
                    "Cambiar contrasena", use_container_width=True
                )

                if submitted:
                    if not new_pwd or not confirm_pwd:
                        st.error("Completa ambos campos.")
                    elif len(new_pwd) < 6:
                        st.error("La contrasena debe tener al menos 6 caracteres.")
                    elif new_pwd != confirm_pwd:
                        st.error("Las contrasenas no coinciden.")
                    else:
                        ok = change_password(user["email"], new_pwd)
                        if ok:
                            st.session_state["user"]["must_change_password"] = False
                            st.toast("Contrasena actualizada exitosamente!")
                            st.rerun()
                        else:
                            st.error("Error al cambiar la contrasena.")


# ============================================================================
# MAIN APP
# ============================================================================
def main_app():
    from utils.ui_components import (
        page_header,
        simple_kpi_card,
        sidebar_user_card,
    )

    user = get_current_user()
    if user is None:
        st.session_state["authenticated"] = False
        st.rerun()
        return

    user_rol = user.get("rol", "planner")
    allowed_modules = get_allowed_modules(user_rol)

    # ------------------------------------------------------------------
    # Auto-cargar proyección persistida (si existe y no está en memoria)
    # ------------------------------------------------------------------
    if not st.session_state.get("df_proy_ready"):
        from utils.file_persistence import has_saved_projection, load_projection_results, get_projection_info
        if has_saved_projection():
            _df_proy, _df_proy_daily = load_projection_results()
            if _df_proy is not None and not _df_proy.empty:
                st.session_state["df_proy"] = _df_proy
                if _df_proy_daily is not None and not _df_proy_daily.empty:
                    st.session_state["df_proy_daily"] = _df_proy_daily
                st.session_state["df_proy_ready"] = True
                _info = get_projection_info()
                st.session_state["sim_mode_used"] = _info.get("sim_mode", "diaria") if _info else "diaria"

    # ------------------------------------------------------------------
    # Sidebar: grouped navigation
    # ------------------------------------------------------------------
    if "selected_module" not in st.session_state:
        st.session_state["selected_module"] = "Inicio"

    def _select(module_name: str):
        st.session_state["selected_module"] = module_name

    current = st.session_state["selected_module"]

    # Module definitions grouped by category
    MENU_GROUPS = {
        "CONSULTAS Y DATOS": [
            ("📋", "Maestra Productos"),
            ("📦", "Stock"),
            ("💰", "Ventas"),
            ("🚢", "Comex"),
            ("🔄", "Tablas Syncro"),
            ("🔬", "Diagnostico Tablas"),
        ],
        "PLANIFICACION": [
            ("🎲", "Generador Forecast"),
            ("📈", "Proyeccion Stock"),
            ("📋", "Plan de Compras & OTB"),
            ("💡", "Simulador Rentab."),
            ("📅", "Forecast Diario"),
            ("🔄", "Redistribucion Stock"),
            ("📊", "Ventas Forecast 2+10"),
            ("🚦", "DDMRP Reposicion"),
        ],
        "ANALISIS": [
            ("📊", "Dashboard Stock"),
            ("📊", "Dashboard Stock 2.0"),
            ("📋", "Caso de Negocio"),
            ("📦", "Listado O&E"),
            ("🏬", "InStock Historico"),
            ("🎯", "ABC-XYZ"),
            ("💲", "Elasticidad"),
            ("🔁", "Sell-Through"),
            ("📉", "Tendencia"),
            ("✅", "Forecast Accuracy"),
            ("🛍️", "Pasillo Infinito"),
            ("🛒", "Canasta Productos"),
            ("📐", "Capacidad Volumetrica"),
            ("📉", "Venta Perdida"),
        ],
        "OPERACION Y ALERTAS": [
            ("🗼", "Torre Control S&OP"),
            ("🚨", "Alertas Quiebre"),
            ("🩺", "Higiene Abast."),
            ("🛒", "Resumen Compra"),
            ("🚢", "Analisis Transitos"),
            ("📧", "Alertas Email"),
            ("🏭", "Operaciones Supply"),
            ("📊", "Fcst vs Vta Retail"),
        ],
    }

    # Home button
    st.sidebar.markdown("")
    st.sidebar.button(
        "🏠  Inicio",
        key="nav_Inicio",
        on_click=_select,
        args=("Inicio",),
        use_container_width=True,
        type="primary" if current == "Inicio" else "secondary",
    )

    st.sidebar.markdown("---")

    # Render grouped menu (filtered by role)
    for group_label, modules in MENU_GROUPS.items():
        # Filter modules by user permissions
        visible_modules = [
            (icon, name) for icon, name in modules if name in allowed_modules
        ]
        if not visible_modules:
            continue

        # Determine if this group contains the active module
        group_module_names = [m[1] for m in visible_modules]
        group_is_active = current in group_module_names

        with st.sidebar.expander(group_label, expanded=group_is_active):
            for icon, name in visible_modules:
                is_active = current == name
                st.button(
                    f"{icon}  {name}",
                    key=f"nav_{name}",
                    on_click=_select,
                    args=(name,),
                    use_container_width=True,
                    type="primary" if is_active else "secondary",
                )

    # ── Parámetros globales ──
    st.sidebar.markdown("---")
    st.sidebar.html(
        "<p style='font-size:0.75rem; color:#94a3b8; margin:0 0 4px 0;'>⚙️ PARÁMETROS</p>"
    )

    # TC en vivo como referencia (cacheado 1h)
    @st.cache_data(ttl=3600, show_spinner=False)
    def _fetch_tc_live():
        try:
            import yfinance as yf
            t = yf.Ticker("USDCLP=X")
            h = t.history(period="1d")
            if not h.empty:
                return round(float(h["Close"].iloc[-1]), 1)
        except Exception:
            pass
        return None

    tc_live = _fetch_tc_live()
    _tc_help = "Tipo de cambio USD→CLP para valorización de costos. Budget: 950."
    if tc_live:
        _tc_help += f" TC actual de mercado: ${tc_live:,.0f}"

    st.sidebar.number_input(
        f"TC USD/CLP" + (f"  *(actual: ${tc_live:,.0f})*" if tc_live else ""),
        min_value=500, max_value=1500, value=950, step=10,
        key="tc_usd_clp",
        help=_tc_help,
    )

    # PM filter (global) — opciones cargadas desde cod_pm de vw_producto
    @st.cache_data(ttl=3600, show_spinner=False)
    def _load_pm_options(_conn):
        try:
            import pandas as pd
            df = pd.read_sql(
                "SELECT DISTINCT cod_pm FROM db_dimensiones.dim.vw_producto "
                "WHERE cod_pm IS NOT NULL AND TRIM(CAST(cod_pm AS VARCHAR)) != '' "
                "ORDER BY cod_pm",
                _conn,
            )
            return df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
        except Exception:
            return PM_NAMES  # fallback a lista estatica si falla

    conn_for_pm = get_active_connection()
    _pm_options = _load_pm_options(conn_for_pm) if conn_for_pm else PM_NAMES

    st.sidebar.selectbox(
        "👤 Product Manager",
        ["Todos"] + _pm_options,
        index=0,
        key="sidebar_pm_filter",
        help="Filtra todos los módulos por Product Manager. 'Todos' muestra todo.",
    )

    # ── Sidebar footer: refresh + user card + logout ──
    st.sidebar.markdown("---")

    # Refresh data button
    def _on_refresh():
        clear_query_cache()

    st.sidebar.button(
        "🔄  Refrescar Datos",
        key="btn_refresh_data",
        on_click=_on_refresh,
        use_container_width=True,
        help="Limpia el cache y recarga datos frescos desde Snowflake",
    )
    _refresh_lbl = last_refresh_label()
    if _refresh_lbl:
        st.sidebar.caption(_refresh_lbl)

    st.sidebar.html(
        sidebar_user_card(
            nombre=user.get("nombre", ""),
            email=user.get("email", ""),
        ),
    )

    def _logout():
        st.session_state["authenticated"] = False
        st.session_state["user"] = None
        st.session_state["selected_module"] = "Inicio"

    st.sidebar.button(
        "🚪  Cerrar Sesion",
        key="btn_logout",
        on_click=_logout,
        use_container_width=True,
    )

    # ------------------------------------------------------------------
    # Main content area
    # ------------------------------------------------------------------
    # Check Snowflake connection status (cached per session)
    if "sf_connected" not in st.session_state:
        st.session_state["sf_connected"] = _check_snowflake_connection()
    sf_connected = st.session_state["sf_connected"]

    conn = get_active_connection()

    # ------------------------------------------------------------------
    # Auto-refresh cache al primer inicio del dia (datos frescos de Snowflake)
    # ------------------------------------------------------------------
    auto_refresh_if_new_day()

    # ------------------------------------------------------------------
    # Auto-cargar ABC-XYZ-FSN (dato maestro, cacheado 24 h)
    # ------------------------------------------------------------------
    if conn and not st.session_state.get("abc_xyz_fsn_ready"):
        try:
            _ = cq.abc_xyz_fsn(conn)          # trigger cache population
            st.session_state["abc_xyz_fsn_ready"] = True
        except Exception:
            pass  # silently skip — will retry next rerun

    MODULE_DISPATCH = {
        "Inicio": None,
        "Torre Control S&OP": render_sop_control_tower,
        "Ventas": render_ventas,
        "Proyeccion Stock": render_proyeccion,
        "Dashboard Stock": render_stock_dashboard,
        "Dashboard Stock 2.0": render_stock_dashboard_v2,
        "Caso de Negocio": render_business_case,
        "Listado O&E": render_listado_oe,
        "DDMRP Reposicion": render_ddmrp,
        "Stock": render_stock,
        "Maestra Productos": render_maestra,
        "Comex": render_comex,
        "Tablas Syncro": render_syncro,
        "Generador Forecast": render_forecast_generator,
        "Alertas Quiebre": render_alertas_quiebre,
        "ABC-XYZ": render_abc_xyz,
        "Elasticidad": render_elasticidad,
        "Higiene Abast.": render_higiene,
        "Sell-Through": render_sell_through,
        "Tendencia": render_tendencia,
        "Forecast Accuracy": render_forecast_accuracy,
        "Simulador Rentab.": render_simulador,
        "Forecast Diario": render_forecast_diario,
        "Resumen Compra": render_resumen_compra,
        "Plan de Compras & OTB": render_plan_compras,
        "InStock Historico": render_instock_historico,
        # Pasillo Infinito: pendiente validar tabla db_pos.fct.ft_venta_pasillo_infinito en Peru
        "Pasillo Infinito": None,
        "Canasta Productos": render_canasta,
        "Alertas Email": render_alertas_email,
        "Analisis Transitos": render_analisis_transitos,
        "Redistribucion Stock": render_redistribucion,
        "Capacidad Volumetrica": render_capacidad_volumetrica,
        # Operaciones Supply: tablas ft_pedidotransferencia y ft_picking no existen en Peru
        "Operaciones Supply": None,
        "Venta Perdida": render_venta_perdida,
        "Ventas Forecast 2+10": render_ventas_forecast_210,
        "Fcst vs Vta Retail": render_fcst_vs_vta_retail,
        "Diagnostico Tablas": render_diagnostico,
    }

    if current == "Inicio":
        import plotly.graph_objects as go
        import pandas as pd
        import numpy as np
        from datetime import datetime, timedelta
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from utils.filters import norm_cols, human_format
        from utils.budget import load_budget, get_budget_cogs_monthly
        from config import dorel_layout

        def _hdr(title: str) -> str:
            """Consistent section header with bottom border for Inicio."""
            return (
                f'<div style="margin-top:1.5rem;padding-bottom:0.5rem;'
                f'border-bottom:2px solid #e2e8f0;margin-bottom:0.25rem;">'
                f'<span style="font-size:0.95rem;font-weight:600;'
                f'color:#2A2927;letter-spacing:0.1px;">{title}</span>'
                f'</div>'
            )

        # Hero header con animación Lottie
        _hero_l, _hero_r = st.columns([3, 1])
        with _hero_l:
            st.html(
                page_header(
                    "Portal de Datos Dorel Peru",
                    "Dashboard ejecutivo de supply chain",
                    period_text=datetime.now().strftime("%B %Y").title(),
                    sf_connected=sf_connected,
                    user_name=user.get("nombre", ""),
                    user_cargo=user.get("cargo", ""),
                ),
            )
        with _hero_r:
            show_lottie("analytics", height=100, key="hero_lottie")

        if not sf_connected:
            st.warning("Snowflake no esta conectado. Conecta para ver los KPIs.")
        else:
            # ── Load dashboard data (centralized cache) ──
            try:
                _loaders = [
                    ("stock",      lambda: cq.stock_onhand(conn)),
                    ("ventas",     lambda: cq.dashboard_ventas_mtd(conn)),
                    ("is_cd",      lambda: cq.instock_daily_cd(conn)),
                    ("is_tienda",  lambda: cq.instock_daily_tienda(conn)),
                    ("ytd",        lambda: cq.ventas_ytd(conn)),
                    ("ytd_aa",     lambda: cq.ventas_ytd_aa(conn)),
                    ("v90d",       lambda: cq.ventas_diarias_90d(conn)),
                    ("stock_proy", lambda: cq.stock_proyeccion(conn)),
                    ("comex",      lambda: cq.comex_full(conn)),
                    ("transit",    lambda: cq.transit_stock_kpi(conn)),
                    ("maestra",    lambda: cq.maestra(conn)),
                    ("crit",       lambda: cq.stock_critico_metrics(conn)),
                ]
                _data: dict = {}
                _query_errors: dict = {}
                with st.spinner("Consultando Snowflake..."):
                    for _name, _fn in _loaders:
                        try:
                            _data[_name] = _fn()
                        except Exception as _qe:
                            _query_errors[_name] = str(_qe)
                            _data[_name] = pd.DataFrame()

                # Asignar siempre (queries fallidas entregan DataFrame vacío)
                df_stock      = apply_pm_filter(_data["stock"])
                df_ventas     = apply_pm_filter(_data["ventas"])
                df_is_cd      = apply_pm_filter(_data["is_cd"])
                df_is_tienda  = apply_pm_filter(_data["is_tienda"])
                df_ytd        = apply_pm_filter(_data["ytd"])
                df_ytd_aa     = apply_pm_filter(_data["ytd_aa"])
                df_v90d       = apply_pm_filter(_data["v90d"])
                df_stock_proy = apply_pm_filter(_data["stock_proy"])
                df_comex_full = apply_pm_filter(_data["comex"])
                df_transit    = apply_pm_filter(_data["transit"])
                df_maestra    = _data["maestra"]  # No filtrar maestra (usada para joins)
                df_crit       = apply_pm_filter(_data["crit"])

                # Mostrar errores visibles al usuario
                if _query_errors:
                    with st.expander(f"⚠️ {len(_query_errors)} query(s) fallaron — ver detalle", expanded=True):
                        for qname, qerr in _query_errors.items():
                            st.error(f"**{qname}**: {qerr}")

                # ── Helpers ──
                def _moi_color(val):
                    if val <= 0:
                        return COLORS["medium_gray"]
                    if val < 6:
                        return COLORS["status_on_track"]
                    if val < 12:
                        return COLORS["status_at_risk"]
                    return COLORS["status_critical"]

                def _is_color(pct):
                    if pct >= 85:
                        return COLORS["status_on_track"]
                    if pct >= 70:
                        return COLORS["status_at_risk"]
                    return COLORS["status_critical"]

                # ── Numeric coercion ──
                for c in ["STOCK_CD", "STOCK_TIENDA", "STOCK_TOTAL", "STOCK_COSTO_TOTAL"]:
                    if c in df_stock.columns:
                        df_stock[c] = pd.to_numeric(df_stock[c], errors="coerce").fillna(0)
                for c in ["CANTIDAD_MTD", "NETO_MTD"]:
                    if c in df_ventas.columns:
                        df_ventas[c] = pd.to_numeric(df_ventas[c], errors="coerce").fillna(0)
                for c in ["STOCK_COSTO_CD", "COSTO_PROM_90_CIA", "INSTOCK_CD_90", "STOCK_UND_CD"]:
                    if c in df_is_cd.columns:
                        df_is_cd[c] = pd.to_numeric(df_is_cd[c], errors="coerce").fillna(0)
                if "FECHA" in df_is_cd.columns:
                    df_is_cd["FECHA"] = pd.to_datetime(df_is_cd["FECHA"], errors="coerce")
                for c in ["TIENDAS_IS90", "N_TIENDAS_IS90"]:
                    if c in df_is_tienda.columns:
                        df_is_tienda[c] = pd.to_numeric(df_is_tienda[c], errors="coerce").fillna(0)
                if "FECHA" in df_is_tienda.columns:
                    df_is_tienda["FECHA"] = pd.to_datetime(df_is_tienda["FECHA"], errors="coerce")
                for c in ["STOCK_COSTO", "STOCK_UNIDADES", "MOI", "ANTIGUEDAD_MESES"]:
                    if c in df_crit.columns:
                        df_crit[c] = pd.to_numeric(df_crit[c], errors="coerce")
                if "FECHA" in df_crit.columns:
                    df_crit["FECHA"] = pd.to_datetime(df_crit["FECHA"], errors="coerce")

                # ══════════════════════════════════════════════
                # COMPUTATIONS
                # ══════════════════════════════════════════════

                # --- Stock (Row 1) ---
                stock_cd = df_stock["STOCK_CD"].sum() if "STOCK_CD" in df_stock.columns else 0
                stock_tienda = df_stock["STOCK_TIENDA"].sum() if "STOCK_TIENDA" in df_stock.columns else 0
                stock_total = df_stock["STOCK_TOTAL"].sum() if "STOCK_TOTAL" in df_stock.columns else 0
                stock_costo = df_stock["STOCK_COSTO_TOTAL"].sum() if "STOCK_COSTO_TOTAL" in df_stock.columns else 0

                # Stock Critico: (MOI >= 12 OR sin MOI) AND Antiguedad >= 12 meses
                _crit_last_date = df_crit["FECHA"].max() if "FECHA" in df_crit.columns and not df_crit.empty else None
                _df_crit_last = df_crit[df_crit["FECHA"] == _crit_last_date] if _crit_last_date is not None else pd.DataFrame()
                stock_critico_val = 0
                stock_critico_und = 0
                stock_critico_pct = 0
                if not _df_crit_last.empty and "MOI" in _df_crit_last.columns and "ANTIGUEDAD_MESES" in _df_crit_last.columns:
                    _moi_crit = _df_crit_last["MOI"].isna() | (_df_crit_last["MOI"] >= 12)
                    _age_crit = _df_crit_last["ANTIGUEDAD_MESES"].fillna(0) >= 12
                    _crit_mask = _moi_crit & _age_crit
                    stock_critico_val = _df_crit_last.loc[_crit_mask, "STOCK_COSTO"].fillna(0).sum()
                    stock_critico_und = _df_crit_last.loc[_crit_mask, "STOCK_UNIDADES"].fillna(0).sum()
                    stock_critico_pct = (stock_critico_val / stock_costo * 100) if stock_costo > 0 else 0

                # --- Transito (Row 2) — uses QUERY_TRANSIT_STOCK_KPI (ETD/ETA based) ---
                en_agua_costo = 0
                en_agua_und = 0
                no_zarpado_costo = 0
                no_zarpado_und = 0
                if not df_transit.empty:
                    for _tc in ["QTY_PENDIENTE", "MONTO_PENDIENTE_CLP"]:
                        if _tc in df_transit.columns:
                            df_transit[_tc] = pd.to_numeric(df_transit[_tc], errors="coerce").fillna(0)
                    _st_col = "STATUS_TRANSITO"
                    if _st_col in df_transit.columns:
                        _agua = df_transit[df_transit[_st_col] == "EN_AGUA"]
                        _zarpe = df_transit[df_transit[_st_col] == "PENDIENTE_ZARPE"]
                        en_agua_costo = _agua["MONTO_PENDIENTE_CLP"].sum() if not _agua.empty else 0
                        en_agua_und = _agua["QTY_PENDIENTE"].sum() if not _agua.empty else 0
                        no_zarpado_costo = _zarpe["MONTO_PENDIENTE_CLP"].sum() if not _zarpe.empty else 0
                        no_zarpado_und = _zarpe["QTY_PENDIENTE"].sum() if not _zarpe.empty else 0

                # --- MOI (Row 3) ---
                _cd_last_date = df_is_cd["FECHA"].max() if "FECHA" in df_is_cd.columns and not df_is_cd.empty else None
                df_cd_last = df_is_cd[df_is_cd["FECHA"] == _cd_last_date] if _cd_last_date is not None else pd.DataFrame()

                stock_costo_cd = df_cd_last["STOCK_COSTO_CD"].sum() if "STOCK_COSTO_CD" in df_cd_last.columns else 0
                stock_costo_tienda = max(stock_costo - stock_costo_cd, 0)
                cogs_daily_total = df_cd_last["COSTO_PROM_90_CIA"].sum() if "COSTO_PROM_90_CIA" in df_cd_last.columns else 0
                cogs_monthly_hist = cogs_daily_total * 30.44

                moi_cd_hist = stock_costo_cd / cogs_monthly_hist if cogs_monthly_hist > 0 else 0.0
                moi_ti_hist = stock_costo_tienda / cogs_monthly_hist if cogs_monthly_hist > 0 else 0.0
                moi_total_hist = stock_costo / cogs_monthly_hist if cogs_monthly_hist > 0 else 0.0

                # MOI Forecast
                _moi_fc_available = False
                _avg_monthly_cogs_fc = 0
                moi_cd_fc = 0.0
                moi_ti_fc = 0.0
                moi_total_fc = 0.0
                _df_proy = st.session_state.get("df_proy")
                if _df_proy is not None and not _df_proy.empty and "COGS_RES_TOTAL" in _df_proy.columns:
                    _proy_mask = _df_proy["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"]) if "TIPO_DATO" in _df_proy.columns else pd.Series(True, index=_df_proy.index)
                    _df_fc = _df_proy[_proy_mask]
                    if not _df_fc.empty:
                        _n_per = max(_df_fc["PERIODO"].nunique() if "PERIODO" in _df_fc.columns else 1, 1)
                        _cogs_fc_tot = pd.to_numeric(_df_fc["COGS_RES_TOTAL"], errors="coerce").fillna(0).sum()
                        _avg_monthly_cogs_fc = _cogs_fc_tot / _n_per
                        if _avg_monthly_cogs_fc > 0:
                            moi_cd_fc = stock_costo_cd / _avg_monthly_cogs_fc
                            moi_ti_fc = stock_costo_tienda / _avg_monthly_cogs_fc
                            moi_total_fc = stock_costo / _avg_monthly_cogs_fc
                            _moi_fc_available = True

                # MOI Budget
                _avg_monthly_cogs_bdg = get_budget_cogs_monthly("TOTAL")
                moi_budget = stock_costo / _avg_monthly_cogs_bdg if _avg_monthly_cogs_bdg > 0 else 0.0
                _moi_bdg_available = _avg_monthly_cogs_bdg > 0

                # --- Ventas YTD (Row 4) ---
                for c in ["NETO_TOTAL", "APORTE_TOTAL", "UNIDADES_VENDIDAS"]:
                    if c in df_ytd.columns:
                        df_ytd[c] = pd.to_numeric(df_ytd[c], errors="coerce").fillna(0)
                for c in ["NETO_AA", "APORTE_AA"]:
                    if c in df_ytd_aa.columns:
                        df_ytd_aa[c] = pd.to_numeric(df_ytd_aa[c], errors="coerce").fillna(0)

                ytd_neto = df_ytd["NETO_TOTAL"].sum() if "NETO_TOTAL" in df_ytd.columns else 0
                ytd_aporte = df_ytd["APORTE_TOTAL"].sum() if "APORTE_TOTAL" in df_ytd.columns else 0
                ytd_und = df_ytd["UNIDADES_VENDIDAS"].sum() if "UNIDADES_VENDIDAS" in df_ytd.columns else 0
                ytd_neto_aa = df_ytd_aa["NETO_AA"].sum() if "NETO_AA" in df_ytd_aa.columns else 0
                ytd_delta = ytd_neto - ytd_neto_aa
                ytd_delta_pct = ((ytd_neto / ytd_neto_aa) - 1) * 100 if ytd_neto_aa > 0 else 0

                # Budget YTD
                _df_budget = load_budget()
                ytd_budget_vn = 0
                if not _df_budget.empty and "PERIODO" in _df_budget.columns:
                    _today = datetime.now()
                    _df_budget["PERIODO"] = pd.to_datetime(_df_budget["PERIODO"], errors="coerce")
                    _bdg_mask = (
                        (_df_budget["CANAL"].str.upper() == "TOTAL")
                        & (_df_budget["PERIODO"].dt.year == _today.year)
                        & (_df_budget["PERIODO"].dt.month <= _today.month)
                    )
                    ytd_budget_vn = pd.to_numeric(_df_budget.loc[_bdg_mask, "VN_BUDGET"], errors="coerce").fillna(0).sum()
                _avance_budget_pct = (ytd_neto / ytd_budget_vn * 100) if ytd_budget_vn > 0 else 0

                # VN Forecast YTD (from projection session state)
                ytd_fc_vn = 0
                _ytd_fc_available = False
                if _df_proy is not None and not _df_proy.empty and "VN_RES_TOTAL" in _df_proy.columns:
                    _df_proy_c = _df_proy.copy()
                    if "PERIODO" in _df_proy_c.columns:
                        _df_proy_c["PERIODO"] = pd.to_datetime(_df_proy_c["PERIODO"], errors="coerce")
                        _now = datetime.now()
                        _fc_ytd_mask = (
                            _df_proy_c["TIPO_DATO"].isin(["HISTORICO", "REAL+FC"]) if "TIPO_DATO" in _df_proy_c.columns else pd.Series(True, index=_df_proy_c.index)
                        ) & (_df_proy_c["PERIODO"].dt.year == _now.year) & (_df_proy_c["PERIODO"].dt.month <= _now.month)
                        ytd_fc_vn = pd.to_numeric(_df_proy_c.loc[_fc_ytd_mask, "VN_RES_TOTAL"], errors="coerce").fillna(0).sum()
                        if ytd_fc_vn > 0:
                            _ytd_fc_available = True

                # --- Ventas MTD por canal (Row 5 chart) ---
                venta_total = df_ventas["NETO_MTD"].sum() if "NETO_MTD" in df_ventas.columns else 0
                venta_und = df_ventas["CANTIDAD_MTD"].sum() if "CANTIDAD_MTD" in df_ventas.columns else 0
                _canal_map = {"MINOR": "Minorista", "ETAIL": "Etail", "MAYOR": "Mayorista"}
                vn_por_canal = {}
                if "COD_CANAL" in df_ventas.columns:
                    for _, r in df_ventas.iterrows():
                        canal = _canal_map.get(str(r.get("COD_CANAL", "")).upper(), str(r.get("COD_CANAL", "")))
                        vn_por_canal[canal] = vn_por_canal.get(canal, 0) + r.get("NETO_MTD", 0)

                # --- InStock latest (for Row 7) ---
                _ti_last_date = df_is_tienda["FECHA"].max() if "FECHA" in df_is_tienda.columns and not df_is_tienda.empty else None
                df_ti_last = df_is_tienda[df_is_tienda["FECHA"] == _ti_last_date] if _ti_last_date is not None else pd.DataFrame()

                # --- Sales velocity by SKU (shared for Row 6 & 7) ---
                if not df_v90d.empty and "SKU_PRODUCTO" in df_v90d.columns and "UNIDADES" in df_v90d.columns:
                    df_v90d["UNIDADES"] = pd.to_numeric(df_v90d["UNIDADES"], errors="coerce").fillna(0)
                    if "NETO" in df_v90d.columns:
                        df_v90d["NETO"] = pd.to_numeric(df_v90d["NETO"], errors="coerce").fillna(0)
                    if "FECHA" in df_v90d.columns:
                        df_v90d["FECHA"] = pd.to_datetime(df_v90d["FECHA"], errors="coerce")
                    _n_dias = max(df_v90d["FECHA"].nunique(), 1) if "FECHA" in df_v90d.columns else 90
                    _agg_dict = {"VENTA_TOTAL_90D": ("UNIDADES", "sum")}
                    if "NETO" in df_v90d.columns:
                        _agg_dict["VN_TOTAL_90D"] = ("NETO", "sum")
                    _vta_all = df_v90d.groupby("SKU_PRODUCTO", as_index=False).agg(**_agg_dict)
                    _vta_all["VENTA_DIARIA_PROM"] = _vta_all["VENTA_TOTAL_90D"] / _n_dias
                    _vta_all["VENTA_PROM_MENSUAL"] = _vta_all["VENTA_DIARIA_PROM"] * 30.44
                    if "VN_TOTAL_90D" in _vta_all.columns:
                        _vta_all["VN_PROM_MENSUAL"] = _vta_all["VN_TOTAL_90D"] / _n_dias * 30.44
                    else:
                        _vta_all["VN_TOTAL_90D"] = 0
                        _vta_all["VN_PROM_MENSUAL"] = 0
                else:
                    _vta_all = pd.DataFrame(columns=["SKU_PRODUCTO", "VENTA_TOTAL_90D", "VENTA_DIARIA_PROM", "VENTA_PROM_MENSUAL", "VN_TOTAL_90D", "VN_PROM_MENSUAL"])

                # --- Stock by SKU (shared) ---
                if "STOCK_UNIDADES" in df_stock_proy.columns:
                    df_stock_proy["STOCK_UNIDADES"] = pd.to_numeric(df_stock_proy["STOCK_UNIDADES"], errors="coerce").fillna(0)
                _stk_total = df_stock_proy.groupby("SKU_PRODUCTO", as_index=False).agg(STOCK_TOTAL=("STOCK_UNIDADES", "sum")) if "SKU_PRODUCTO" in df_stock_proy.columns else pd.DataFrame(columns=["SKU_PRODUCTO", "STOCK_TOTAL"])

                # --- Next ETA per SKU (shared for Row 6 & 7) ---
                _cmx_eta = pd.DataFrame(columns=["SKU_PRODUCTO", "PROXIMA_ETA", "QTY_EN_TRANSITO"])
                if not df_comex_full.empty:
                    _cmx2 = df_comex_full.copy()
                    for _ec in ["ETA_CALC", "ETA", "FECHA_ENTREGA"]:
                        if _ec in _cmx2.columns:
                            _cmx2["_ETA"] = pd.to_datetime(_cmx2[_ec], errors="coerce")
                            if _ec == "FECHA_ENTREGA":
                                _cmx2["_ETA"] = _cmx2["_ETA"] + timedelta(days=47)
                            break
                    else:
                        _cmx2["_ETA"] = pd.NaT
                    if "SKU_PRODUCTO" not in _cmx2.columns:
                        for _alt in ["MATERIAL", "SKU", "COD_MATERIAL"]:
                            if _alt in _cmx2.columns:
                                _cmx2 = _cmx2.rename(columns={_alt: "SKU_PRODUCTO"})
                                break
                    _qty_cmx = None
                    for _qc in ["SALDO", "CANTIDAD_FINAL_CORREGIDA", "CANTIDAD"]:
                        if _qc in _cmx2.columns:
                            _qty_cmx = _qc
                            break
                    if "SKU_PRODUCTO" in _cmx2.columns and "_ETA" in _cmx2.columns:
                        _today_dt = pd.Timestamp.now().normalize()
                        _pend = _cmx2[_cmx2["_ETA"] >= _today_dt].copy()
                        if "FECHA_RECEPCION_EN_CD" in _pend.columns:
                            _pend["FECHA_RECEPCION_EN_CD"] = pd.to_datetime(_pend["FECHA_RECEPCION_EN_CD"], errors="coerce")
                            _pend = _pend[_pend["FECHA_RECEPCION_EN_CD"].isna()]
                        if not _pend.empty and _qty_cmx:
                            _pend[_qty_cmx] = pd.to_numeric(_pend[_qty_cmx], errors="coerce").fillna(0)
                            _cmx_eta = _pend.groupby("SKU_PRODUCTO", as_index=False).agg(
                                PROXIMA_ETA=("_ETA", "min"), QTY_EN_TRANSITO=(_qty_cmx, "sum"))
                        elif not _pend.empty:
                            _cmx_eta = _pend.groupby("SKU_PRODUCTO", as_index=False).agg(PROXIMA_ETA=("_ETA", "min"))
                            _cmx_eta["QTY_EN_TRANSITO"] = 0

                # ══════════════════════════════════════════════
                # ROW 1: Ventas YTD (main cards + comparison badges)
                # ══════════════════════════════════════════════
                st.html(_hdr("📈 Ventas YTD (hasta ayer)"))
                _ytd_margen = ytd_aporte / ytd_neto if ytd_neto > 0 else 0
                _v_main1, _v_main2 = st.columns(2)
                with _v_main1:
                    st.html(simple_kpi_card(
                        f"Venta Neta YTD {datetime.now().year}",
                        f"${human_format(ytd_neto)}",
                        COLORS["primary"],
                        subtitle=f"{ytd_und:,.0f} unidades",
                    ))
                with _v_main2:
                    _ap_color = COLORS["status_on_track"] if _ytd_margen >= 0.25 else COLORS["status_at_risk"]
                    st.html(simple_kpi_card(
                        f"Aporte YTD {datetime.now().year}",
                        f"${human_format(ytd_aporte)}",
                        _ap_color,
                        subtitle=f"Margen: {_ytd_margen:.1%}",
                    ))

                # Comparison badges — visually subordinate to the main cards
                def _badge(text: str, bg: str, fg: str) -> str:
                    return (
                        f'<span style="background:{bg};color:{fg};padding:0.35rem 0.75rem;'
                        f'border-radius:8px;font-size:0.78rem;font-weight:500;'
                        f'white-space:nowrap;">{text}</span>'
                    )

                _d_arrow = "▲" if ytd_delta >= 0 else "▼"
                _d_sign = "+" if ytd_delta >= 0 else ""
                _d_color = COLORS["status_on_track"] if ytd_delta >= 0 else COLORS["status_critical"]
                _d_bg = "#f0fdf4" if ytd_delta >= 0 else "#fef2f2"

                _badges = []
                _badges.append(_badge(
                    f"{_d_arrow} {_d_sign}{ytd_delta_pct:.1f}% vs AA · {_d_sign}${human_format(abs(ytd_delta))}",
                    _d_bg, _d_color,
                ))
                _badges.append(_badge(
                    f"AA {datetime.now().year - 1}: ${human_format(ytd_neto_aa)}",
                    "#f8fafc", "#64748b",
                ))
                if ytd_budget_vn > 0:
                    _badges.append(_badge(
                        f"Budget: {_avance_budget_pct:.0f}% avance · ${human_format(ytd_budget_vn)}",
                        "#f5f3ff", COLORS["secondary"],
                    ))
                if _ytd_fc_available:
                    _badges.append(_badge(
                        f"Forecast: ${human_format(ytd_fc_vn)}",
                        "#f0f9ff", COLORS["tertiary_teal"],
                    ))
                st.html(
                    f'<div style="display:flex;gap:0.5rem;flex-wrap:wrap;'
                    f'margin-top:0.25rem;padding:0 0.25rem;">'
                    + "".join(_badges)
                    + "</div>"
                )

                # ══════════════════════════════════════════════
                # ROW 2: Inventario (3 cards)
                # ══════════════════════════════════════════════
                st.html(_hdr("📦 Inventario"))
                r1a, r1b, r1c = st.columns(3)
                with r1a:
                    st.html(simple_kpi_card("Inventario Total", f"${human_format(stock_costo)}", COLORS["primary"],
                        subtitle=f"CD: ${human_format(stock_costo_cd)} · Tienda: ${human_format(stock_costo_tienda)}"))
                with r1b:
                    st.html(simple_kpi_card("Stock Unidades", f"{stock_total:,.0f}", COLORS["tertiary_teal"],
                        subtitle=f"CD: {stock_cd:,.0f} · Tienda: {stock_tienda:,.0f}"))
                with r1c:
                    _sc_color = COLORS["status_critical"] if stock_critico_pct > 15 else COLORS["status_at_risk"] if stock_critico_pct > 5 else COLORS["status_on_track"]
                    st.html(simple_kpi_card("Stock Crítico", f"${human_format(stock_critico_val)}", _sc_color,
                        subtitle=f"{stock_critico_pct:.1f}% del total · {stock_critico_und:,.0f} und"))

                # ══════════════════════════════════════════════
                # ROW 3: Transito (2 cards)
                # ══════════════════════════════════════════════
                st.html(_hdr("🚢 Stock en Tránsito"))
                t1, t2 = st.columns(2)
                with t1:
                    st.html(simple_kpi_card("En Agua", f"${human_format(en_agua_costo)}", COLORS["tertiary_blue"],
                        subtitle=f"{en_agua_und:,.0f} unidades · Mercadería zarpada"))
                with t2:
                    st.html(simple_kpi_card("No Zarpado", f"${human_format(no_zarpado_costo)}", COLORS["secondary"],
                        subtitle=f"{no_zarpado_und:,.0f} unidades · Órdenes pendientes de zarpe"))

                # ══════════════════════════════════════════════
                # ROW 4: MOI Compania (generals + detail)
                # ══════════════════════════════════════════════
                st.html(_hdr("📊 MOI Compañía"))
                # Sub-row 1: Totales generales (hist → fc → budget)
                m1, m2, m3 = st.columns(3)
                with m1:
                    st.html(simple_kpi_card("MOI Total Hist.", f"{moi_total_hist:.1f}m", _moi_color(moi_total_hist),
                        subtitle=f"COGS hist: ${human_format(cogs_monthly_hist)}/mes"))
                with m2:
                    if _moi_fc_available:
                        st.html(simple_kpi_card("MOI Total FC", f"{moi_total_fc:.1f}m", _moi_color(moi_total_fc),
                            subtitle=f"COGS fc: ${human_format(_avg_monthly_cogs_fc)}/mes"))
                    else:
                        st.html(simple_kpi_card("MOI Total FC", "N/D", COLORS["medium_gray"],
                            subtitle="Ejecute Proyeccion primero"))
                with m3:
                    if _moi_bdg_available:
                        st.html(simple_kpi_card("MOI Budget", f"{moi_budget:.1f}m", _moi_color(moi_budget),
                            subtitle=f"COGS bdg: ${human_format(_avg_monthly_cogs_bdg)}/mes"))
                    else:
                        st.html(simple_kpi_card("MOI Budget", "N/D", COLORS["medium_gray"],
                            subtitle="Budget no disponible"))
                # Sub-row 2: Por canal — hist left, fc right
                m4, m5, m6, m7 = st.columns(4)
                with m4:
                    st.html(simple_kpi_card("MOI CD Hist.", f"{moi_cd_hist:.1f}m", _moi_color(moi_cd_hist)))
                with m5:
                    st.html(simple_kpi_card("MOI Tiendas Hist.", f"{moi_ti_hist:.1f}m", _moi_color(moi_ti_hist)))
                with m6:
                    if _moi_fc_available:
                        st.html(simple_kpi_card("MOI CD FC", f"{moi_cd_fc:.1f}m", _moi_color(moi_cd_fc)))
                    else:
                        st.html(simple_kpi_card("MOI CD FC", "N/D", COLORS["medium_gray"]))
                with m7:
                    if _moi_fc_available:
                        st.html(simple_kpi_card("MOI Tiendas FC", f"{moi_ti_fc:.1f}m", _moi_color(moi_ti_fc)))
                    else:
                        st.html(simple_kpi_card("MOI Tiendas FC", "N/D", COLORS["medium_gray"]))

                # ══════════════════════════════════════════════
                # ROW 5: 2 Charts side by side
                # ══════════════════════════════════════════════
                st.html(_hdr("📊 Distribución"))
                ch1, ch2 = st.columns(2)
                with ch1:
                    st.markdown("**Stock por Canal**")
                    fig_stock = go.Figure(layout=dorel_layout(
                        height=300, showlegend=True,
                        legend=dict(orientation="h", y=-0.1, xanchor="center", x=0.5),
                    ))
                    fig_stock.add_trace(go.Pie(
                        labels=["CD", "Tienda"],
                        values=[stock_cd, stock_tienda],
                        hole=0.5,
                        marker_colors=[COLORS["primary"], COLORS["tertiary_teal"]],
                        textinfo="label+percent",
                        textfont=dict(size=12),
                        hovertemplate="%{label}<br>%{value:,.0f} und<br>%{percent}<extra></extra>",
                    ))
                    st.plotly_chart(fig_stock, use_container_width=True)
                with ch2:
                    st.markdown("**Ventas MTD por Canal**")
                    _canales = list(vn_por_canal.keys()) if vn_por_canal else ["Sin datos"]
                    _valores = list(vn_por_canal.values()) if vn_por_canal else [0]
                    _colores = [COLORS["primary"], COLORS["tertiary_blue"], COLORS["secondary"]]
                    fig_ventas = go.Figure(layout=dorel_layout(
                        height=300, showlegend=False,
                        xaxis=dict(title="Venta Neta (CLP)"), yaxis=dict(title=""),
                    ))
                    fig_ventas.add_trace(go.Bar(
                        y=_canales, x=_valores, orientation="h",
                        marker_color=_colores[:len(_canales)],
                        text=[f"${human_format(v)}" for v in _valores],
                        textposition="auto", textfont=dict(size=11),
                        hovertemplate="%{y}<br>$%{x:,.0f}<extra></extra>",
                    ))
                    st.plotly_chart(fig_ventas, use_container_width=True)

                # ══════════════════════════════════════════════
                # ROW 6: Top 20 SKUs en Quiebre
                # ══════════════════════════════════════════════
                st.html(_hdr("⚠️ Top 20 SKUs en Quiebre  —  por Venta Neta Promedio Mensual"))
                _df_q = _stk_total.merge(_vta_all, on="SKU_PRODUCTO", how="inner")
                _df_q = _df_q.merge(_cmx_eta, on="SKU_PRODUCTO", how="left")
                _df_q["DIAS_COBERTURA"] = np.where(
                    _df_q["VENTA_DIARIA_PROM"] > 0, _df_q["STOCK_TOTAL"] / _df_q["VENTA_DIARIA_PROM"], 999)
                _quiebre_mask = (_df_q["STOCK_TOTAL"] <= 0) | (_df_q["DIAS_COBERTURA"] < 14)
                _df_quiebre = _df_q[_quiebre_mask].copy()

                if not _df_quiebre.empty and not df_maestra.empty:
                    _m_cols6 = [c for c in ["SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA", "MIX_OFICIAL"] if c in df_maestra.columns]
                    if "SKU_PRODUCTO" in _m_cols6:
                        _df_quiebre = _df_quiebre.merge(df_maestra[_m_cols6].drop_duplicates("SKU_PRODUCTO"), on="SKU_PRODUCTO", how="left")

                if not _df_quiebre.empty:
                    # Sort by VN (venta neta) promedio mensual
                    _sort_col6 = "VN_PROM_MENSUAL" if "VN_PROM_MENSUAL" in _df_quiebre.columns else "VENTA_PROM_MENSUAL"
                    _df_quiebre = _df_quiebre.sort_values(_sort_col6, ascending=False).head(20)
                    if "PROXIMA_ETA" in _df_quiebre.columns:
                        _df_quiebre["ETA_LLEGADA"] = _df_quiebre["PROXIMA_ETA"].dt.strftime("%d-%b-%Y")
                        _df_quiebre.loc[_df_quiebre["PROXIMA_ETA"].isna(), "ETA_LLEGADA"] = "Sin ETA"
                    else:
                        _df_quiebre["ETA_LLEGADA"] = "Sin ETA"
                    if "VN_PROM_MENSUAL" in _df_quiebre.columns:
                        _df_quiebre["VN_PROM_MENSUAL"] = _df_quiebre["VN_PROM_MENSUAL"].round(0)
                    _df_quiebre["DIAS_COBERTURA"] = _df_quiebre["DIAS_COBERTURA"].round(0)
                    _show6 = [c for c in [
                        "SKU_PRODUCTO", "NOM_PRODUCTO", "MIX_OFICIAL", "AREA", "LINEA", "SUBLINEA", "MARCA",
                        "STOCK_TOTAL", "VN_PROM_MENSUAL", "DIAS_COBERTURA", "ETA_LLEGADA",
                    ] if c in _df_quiebre.columns]
                    st.dataframe(_df_quiebre[_show6], use_container_width=True, hide_index=True,
                        height=min(600, len(_df_quiebre) * 38 + 40),
                        column_config={
                            "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
                            "NOM_PRODUCTO": st.column_config.TextColumn("Producto", width="medium"),
                            "MIX_OFICIAL": st.column_config.TextColumn("Mix", width="small"),
                            "STOCK_TOTAL": st.column_config.NumberColumn("Stock", format="%d"),
                            "VN_PROM_MENSUAL": st.column_config.NumberColumn("VN Prom Mes", format="$%,.0f"),
                            "DIAS_COBERTURA": st.column_config.NumberColumn("Dias Cob.", format="%d"),
                            "ETA_LLEGADA": st.column_config.TextColumn("Prox. ETA", width="small"),
                        })
                else:
                    st.success("No hay SKUs con quiebre critico actualmente.")

                # ══════════════════════════════════════════════
                # ROW 7: Top 20 SKUs Top Venta (con selector)
                # ══════════════════════════════════════════════
                st.html(_hdr("🏆 Top 20 SKUs por Venta Neta"))

                _canal_opts = ["Todos", "TIENDA", "ETAIL", "MAYORISTA"]
                _sel_canal = st.selectbox("Canal", _canal_opts, index=0, key="inicio_canal_top_venta")

                # Map display labels to CANAL_DE_DISTRIBUCION values
                _canal_map_filter = {"TIENDA": "TIENDA", "ETAIL": "ETAIL", "MAYORISTA": "MAYORISTA"}

                # Filter sales by canal
                if _sel_canal != "Todos" and "CANAL_DE_DISTRIBUCION" in df_v90d.columns:
                    _canal_val = _canal_map_filter.get(_sel_canal, _sel_canal)
                    _v90_filt = df_v90d[df_v90d["CANAL_DE_DISTRIBUCION"].str.upper() == _canal_val.upper()]
                else:
                    _v90_filt = df_v90d
                if not _v90_filt.empty and "SKU_PRODUCTO" in _v90_filt.columns:
                    _agg7 = {"VENTA_TOTAL_90D": ("UNIDADES", "sum")}
                    if "NETO" in _v90_filt.columns:
                        _agg7["VN_TOTAL_90D"] = ("NETO", "sum")
                    _vta7 = _v90_filt.groupby("SKU_PRODUCTO", as_index=False).agg(**_agg7)
                    if "VN_TOTAL_90D" not in _vta7.columns:
                        _vta7["VN_TOTAL_90D"] = 0
                    _vta7["VN_PROM_MENSUAL"] = _vta7["VN_TOTAL_90D"] / max(_n_dias, 1) * 30.44
                    _top20 = _vta7.nlargest(20, "VN_TOTAL_90D").copy()
                else:
                    _top20 = pd.DataFrame(columns=["SKU_PRODUCTO", "VENTA_TOTAL_90D", "VN_TOTAL_90D", "VN_PROM_MENSUAL"])

                if not _top20.empty:
                    # Stock per channel
                    if "CANAL_STD" in df_stock_proy.columns:
                        _stk_cd = df_stock_proy[df_stock_proy["CANAL_STD"] == "CD"].groupby("SKU_PRODUCTO", as_index=False).agg(STOCK_CD=("STOCK_UNIDADES", "sum"))
                        _stk_ti = df_stock_proy[df_stock_proy["CANAL_STD"] == "TIENDA"].groupby("SKU_PRODUCTO", as_index=False).agg(STOCK_TIENDA=("STOCK_UNIDADES", "sum"))
                        _top20 = _top20.merge(_stk_cd, on="SKU_PRODUCTO", how="left")
                        _top20 = _top20.merge(_stk_ti, on="SKU_PRODUCTO", how="left")
                    for _fc in ["STOCK_CD", "STOCK_TIENDA"]:
                        if _fc not in _top20.columns:
                            _top20[_fc] = 0
                        _top20[_fc] = _top20[_fc].fillna(0).astype(int)

                    # InStock per SKU (latest date)
                    if not df_ti_last.empty and "SKU_PRODUCTO" in df_ti_last.columns:
                        _is_ti7 = df_ti_last[["SKU_PRODUCTO", "TIENDAS_IS90", "N_TIENDAS_IS90"]].copy()
                        _is_ti7["IS_TIENDA"] = np.where(_is_ti7["N_TIENDAS_IS90"] > 0,
                            _is_ti7["TIENDAS_IS90"] / _is_ti7["N_TIENDAS_IS90"] * 100, 0)
                        _top20 = _top20.merge(_is_ti7[["SKU_PRODUCTO", "IS_TIENDA"]], on="SKU_PRODUCTO", how="left")
                    if "IS_TIENDA" not in _top20.columns:
                        _top20["IS_TIENDA"] = np.nan

                    if not df_cd_last.empty and "SKU_PRODUCTO" in df_cd_last.columns:
                        _is_cd7 = df_cd_last[["SKU_PRODUCTO", "INSTOCK_CD_90"]].copy()
                        _is_cd7 = _is_cd7.rename(columns={"INSTOCK_CD_90": "IS_CD"})
                        _top20 = _top20.merge(_is_cd7, on="SKU_PRODUCTO", how="left")
                    if "IS_CD" not in _top20.columns:
                        _top20["IS_CD"] = np.nan

                    # MOI per SKU (historia from stock_critico_metrics)
                    if not _df_crit_last.empty and "SKU_PRODUCTO" in _df_crit_last.columns and "MOI" in _df_crit_last.columns:
                        _moi7 = _df_crit_last.groupby("SKU_PRODUCTO", as_index=False).agg(MOI_HIST=("MOI", "last"))
                        _top20 = _top20.merge(_moi7, on="SKU_PRODUCTO", how="left")
                    if "MOI_HIST" not in _top20.columns:
                        _top20["MOI_HIST"] = np.nan

                    # MOI Forecast per SKU
                    _top20["MOI_FC"] = np.nan
                    if _moi_fc_available and _df_proy is not None and "COGS_RES_TOTAL" in _df_proy.columns:
                        _proy_fc = _df_proy[_df_proy["TIPO_DATO"].isin(["PROYECCION", "REAL+FC"])] if "TIPO_DATO" in _df_proy.columns else _df_proy
                        _n_p7 = max(_proy_fc["PERIODO"].nunique() if "PERIODO" in _proy_fc.columns else 1, 1)
                        _cogs7 = _proy_fc.groupby("SKU_PRODUCTO", as_index=False).agg(
                            _COGS_TOT=("COGS_RES_TOTAL", lambda x: pd.to_numeric(x, errors="coerce").fillna(0).sum()))
                        _cogs7["_COGS_MES"] = _cogs7["_COGS_TOT"] / _n_p7
                        # Stock per SKU for MOI FC
                        if not _df_crit_last.empty:
                            _sk_costo = _df_crit_last.groupby("SKU_PRODUCTO", as_index=False).agg(_SK_COST=("STOCK_COSTO", "sum"))
                            _cogs7 = _cogs7.merge(_sk_costo, on="SKU_PRODUCTO", how="left")
                            _cogs7["MOI_FC"] = np.where(_cogs7["_COGS_MES"] > 0, _cogs7["_SK_COST"].fillna(0) / _cogs7["_COGS_MES"], np.nan)
                            _top20 = _top20.merge(_cogs7[["SKU_PRODUCTO", "MOI_FC"]], on="SKU_PRODUCTO", how="left", suffixes=("_OLD", ""))
                            if "MOI_FC_OLD" in _top20.columns:
                                _top20["MOI_FC"] = _top20["MOI_FC"].fillna(_top20["MOI_FC_OLD"])
                                _top20 = _top20.drop(columns=["MOI_FC_OLD"])

                    # ETA
                    _top20 = _top20.merge(_cmx_eta[["SKU_PRODUCTO", "PROXIMA_ETA"]], on="SKU_PRODUCTO", how="left")

                    # Maestra enrichment (include MIX_OFICIAL)
                    _m7 = [c for c in ["SKU_PRODUCTO", "NOM_PRODUCTO", "MIX_OFICIAL", "AREA", "LINEA", "SUBLINEA", "MARCA"] if c in df_maestra.columns]
                    if "SKU_PRODUCTO" in _m7:
                        _top20 = _top20.merge(df_maestra[_m7].drop_duplicates("SKU_PRODUCTO"), on="SKU_PRODUCTO", how="left")

                    # Format
                    if "VN_PROM_MENSUAL" in _top20.columns:
                        _top20["VN_PROM_MENSUAL"] = _top20["VN_PROM_MENSUAL"].round(0)
                    if "PROXIMA_ETA" in _top20.columns:
                        _top20["ETA_LLEGADA"] = _top20["PROXIMA_ETA"].dt.strftime("%d-%b-%Y")
                        _top20.loc[_top20["PROXIMA_ETA"].isna(), "ETA_LLEGADA"] = "—"
                    else:
                        _top20["ETA_LLEGADA"] = "—"
                    _top20["IS_TIENDA"] = _top20["IS_TIENDA"].round(1)
                    _top20["MOI_HIST"] = _top20["MOI_HIST"].round(1)
                    _top20["MOI_FC"] = _top20["MOI_FC"].round(1)
                    # IS CD as percentage: 100% or 0%
                    _top20["IS_CD_PCT"] = np.where(_top20["IS_CD"] >= 1, 100.0, np.where(_top20["IS_CD"].isna(), np.nan, 0.0))

                    _show7 = [c for c in [
                        "SKU_PRODUCTO", "NOM_PRODUCTO", "MIX_OFICIAL", "AREA", "LINEA", "SUBLINEA", "MARCA",
                        "VN_PROM_MENSUAL", "STOCK_TIENDA", "STOCK_CD",
                        "IS_TIENDA", "IS_CD_PCT", "MOI_HIST", "MOI_FC", "ETA_LLEGADA",
                    ] if c in _top20.columns]
                    st.dataframe(_top20[_show7], use_container_width=True, hide_index=True,
                        height=min(600, len(_top20) * 38 + 40),
                        column_config={
                            "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="small"),
                            "NOM_PRODUCTO": st.column_config.TextColumn("Producto", width="medium"),
                            "MIX_OFICIAL": st.column_config.TextColumn("Mix", width="small"),
                            "VN_PROM_MENSUAL": st.column_config.NumberColumn("VN Prom Mes", format="$%,.0f"),
                            "STOCK_TIENDA": st.column_config.NumberColumn("Stk Tienda", format="%d"),
                            "STOCK_CD": st.column_config.NumberColumn("Stk CD", format="%d"),
                            "IS_TIENDA": st.column_config.NumberColumn("IS Tienda %", format="%.1f%%"),
                            "IS_CD_PCT": st.column_config.NumberColumn("IS CD %", format="%.0f%%"),
                            "MOI_HIST": st.column_config.NumberColumn("MOI Hist", format="%.1f"),
                            "MOI_FC": st.column_config.NumberColumn("MOI FC", format="%.1f"),
                            "ETA_LLEGADA": st.column_config.TextColumn("Prox. ETA", width="small"),
                        })
                else:
                    st.info("No hay datos de ventas para mostrar.")

            except Exception as e:
                st.error(f"Error cargando dashboard: {e}")
                import traceback
                st.code(traceback.format_exc())

    elif current in MODULE_DISPATCH and MODULE_DISPATCH[current] is None:
        # Modulo no disponible en Dorel Peru (tabla fuente no existe en esta cuenta Snowflake)
        st.warning(
            f"El modulo **{current}** no esta disponible para Dorel Peru. "
            "Las tablas de origen requeridas no existen en esta cuenta Snowflake. "
            "Contacta al equipo de D&A para mas informacion."
        )

    elif current in MODULE_DISPATCH and MODULE_DISPATCH[current] is not None:
        # Verify the user still has access to this module
        if current not in allowed_modules:
            st.warning("No tienes permisos para acceder a este modulo.")
            st.session_state["selected_module"] = "Inicio"
            st.rerun()
        else:
            MODULE_DISPATCH[current](conn)


# ============================================================================
# ENTRY POINT
# ============================================================================
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    login_screen()
elif st.session_state.get("user", {}).get("must_change_password", False):
    change_password_screen()
else:
    main_app()
