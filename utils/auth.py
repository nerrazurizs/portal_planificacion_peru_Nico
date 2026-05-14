"""Authentication for Streamlit in Snowflake.

In SiS, login is handled by Snowflake SSO — no password management needed.
All authenticated Snowflake users have access to all modules.
"""

import streamlit as st

# ---------------------------------------------------------------------------
# Module list (drives the sidebar menu)
# ---------------------------------------------------------------------------

ALL_MODULES = [
    # CONSULTAS Y DATOS
    "Maestra Productos",
    "Stock",
    "Comex",
    "Tablas Syncro",
    "Generador Forecast",
    "Proyeccion Stock",
    "Plan de Compras & OTB",
    "Forecast Diario",
    "Ventas",
    "Dashboard Stock",
    "InStock Historico",
    "Alertas Quiebre",
    "Resumen Compra",
    "Redistribucion Stock",
    "Analisis Transitos",
    "Operaciones Supply",
    "Venta Perdida",
    "Ventas Forecast 2+10",
    "ABC-XYZ",
    "Dashboard Stock 2.0",
    "Listado O&E",
    "DDMRP Reposicion",
    "Alerta Forecast",
    "Fcst vs Vta Retail",
    "Analisis Venta",
    "Agotamiento",
    # ANALISIS / JEFE+
    "Caso de Negocio",
    "Elasticidad",
    "Sell-Through",
    "Tendencia",
    "Forecast Accuracy",
    "Simulador Rentab.",
    "Higiene Abast.",
    "Torre Control S&OP",
    "Pasillo Infinito",
    "Canasta Productos",
    "Alertas Email",
    "Capacidad Volumetrica",
    "Flujo de Costos",
    "Analisis Contenedor",
]


# ---------------------------------------------------------------------------
# User identity — resolved from Snowflake SSO session
# ---------------------------------------------------------------------------

def get_current_user() -> dict:
    """Return a minimal user dict from the active Snowflake SSO session."""
    try:
        email = st.experimental_user.email or ""
    except Exception:
        email = ""
    nombre = email.split("@")[0].replace(".", " ").title() if email else "Usuario"
    return {
        "email": email.lower(),
        "nombre": nombre,
        "cargo": "",
        "rol": "admin",
        "areas": ["*"],
    }


def get_user_display_name() -> str:
    return get_current_user()["nombre"]


def get_user_initials() -> str:
    parts = get_current_user()["nombre"].split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return parts[0][0].upper() if parts else "U"


# ---------------------------------------------------------------------------
# Permissions — all Snowflake-authenticated users have full access
# ---------------------------------------------------------------------------

def get_allowed_modules(rol: str = "admin") -> list[str]:
    """All modules visible to all authenticated users."""
    return list(ALL_MODULES)


def get_area_filter(user: dict | None = None) -> list[str] | None:
    """No area restriction."""
    return None


def require_role(min_role: str) -> bool:
    """Always True — access is controlled at the Snowflake account level."""
    return True


# ---------------------------------------------------------------------------
# Stubs kept for backward compatibility while app.py is updated
# ---------------------------------------------------------------------------

def authenticate(email: str, password: str) -> dict | None:
    """Unused in SiS — login handled by Snowflake SSO."""
    return None


def change_password(email: str, new_password: str) -> bool:
    """Unused in SiS — passwords managed by Snowflake."""
    return False
