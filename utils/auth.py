"""Authentication and role-based access control for the Dorel Portal.

Users are stored in ``users.json`` (gitignored).  Passwords are hashed
with SHA-256 — no external dependency required.

Role hierarchy
--------------
admin  →  full access, can manage users
  └─ jefe  →  full module access (no user admin)
      └─ planner  →  subset of modules, data filtered by area
"""

import hashlib
import json
import os
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_USERS_FILE = Path(__file__).resolve().parent.parent / "users.json"

# ---------------------------------------------------------------------------
# Role hierarchy (higher index = more privileges)
# ---------------------------------------------------------------------------

ROLE_LEVELS = {
    "planner": 1,
    "jefe": 2,
    "admin": 3,
}

# ---------------------------------------------------------------------------
# Module permissions by role
# ---------------------------------------------------------------------------

PLANNER_MODULES = [
    "Maestra Productos",
    "Stock",
    "Comex",
    "Tablas Syncro",
    "Generador Forecast",
    "Proyeccion Stock",
    "Diagnostico Fcst Manual",
    "Correccion Fcst Compra",
    "Frinc Detalle",
    "Plan de Compras & OTB",
    "In-Out bound",
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
    "Optimizador Compras",
]

# Modules that only jefe+ can access (everything else is also available)
JEFE_EXTRA_MODULES = [
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
    "Analisis Forecast",
]

ALL_MODULES = PLANNER_MODULES + JEFE_EXTRA_MODULES


# ---------------------------------------------------------------------------
# User data helpers
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    """Return SHA-256 hex digest of *password*."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


_DEFAULT_HASH = _hash_password("udechile")

_DEFAULT_USERS = {
    "camilo.onate@dorel.cl": {
        "nombre": "Camilo Onate", "cargo": "Administrador", "rol": "admin",
        "jefe": None, "areas": ["*"], "password_hash": _DEFAULT_HASH,
        "must_change_password": False, "activo": True,
    },
    "sebastian.gibaja@comexa.com.pe": {
        "nombre": "Sebastian Gibaja", "cargo": "Planificador", "rol": "jefe",
        "jefe": None, "areas": ["*"], "password_hash": _DEFAULT_HASH,
        "must_change_password": False, "activo": True,
    },
    "kevin.diaz@comexa.com.pe": {
        "nombre": "Kevin Diaz", "cargo": "Planificador", "rol": "jefe",
        "jefe": None, "areas": ["*"], "password_hash": _DEFAULT_HASH,
        "must_change_password": False, "activo": True,
    },
    "angela.berrospi@comexa.com.pe": {
        "nombre": "Angela Berrospi", "cargo": "Planificador", "rol": "jefe",
        "jefe": None, "areas": ["*"], "password_hash": _DEFAULT_HASH,
        "must_change_password": False, "activo": True,
    },
}


def load_users() -> dict:
    """Read ``users.json`` and return the users dict.

    If the file is missing, creates it with default users (password: udechile).
    """
    if not _USERS_FILE.exists():
        save_users(_DEFAULT_USERS)
        return dict(_DEFAULT_USERS)
    try:
        with open(_USERS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("users", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_users(users: dict) -> None:
    """Persist *users* dict back to ``users.json``."""
    with open(_USERS_FILE, "w", encoding="utf-8") as fh:
        json.dump({"users": users}, fh, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate(email: str, password: str) -> dict | None:
    """Validate credentials and return the user record (without hash) or *None*.

    The returned dict has keys:
        email, nombre, cargo, rol, jefe, areas, activo
    """
    email = email.strip().lower()
    users = load_users()

    user = users.get(email)
    if user is None:
        return None

    if not user.get("activo", False):
        return None

    if user.get("password_hash", "") != _hash_password(password):
        return None

    # Return a clean copy without the hash
    return {
        "email": email,
        "nombre": user.get("nombre", ""),
        "cargo": user.get("cargo", ""),
        "rol": user.get("rol", "planner"),
        "jefe": user.get("jefe"),
        "areas": user.get("areas", []),
        "must_change_password": user.get("must_change_password", False),
    }


def change_password(email: str, new_password: str) -> bool:
    """Change password for *email* and clear the must_change_password flag.

    Returns ``True`` on success, ``False`` if user not found.
    """
    users = load_users()
    email = email.strip().lower()
    if email not in users:
        return False

    users[email]["password_hash"] = _hash_password(new_password)
    users[email]["must_change_password"] = False
    save_users(users)
    return True


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_current_user() -> dict | None:
    """Return the currently logged-in user dict from session state, or *None*."""
    return st.session_state.get("user")


def get_user_display_name() -> str:
    """Return 'Nombre (Cargo)' for the current user, or empty string."""
    user = get_current_user()
    if user is None:
        return ""
    return f"{user['nombre']} ({user['cargo']})"


def get_user_initials() -> str:
    """Return up to two uppercase initials of the current user."""
    user = get_current_user()
    if user is None:
        return "U"
    parts = user.get("nombre", "U").split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return parts[0][0].upper() if parts else "U"


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def get_allowed_modules(rol: str) -> list[str]:
    """Return the list of module names the role can access.

    * ``admin`` / ``jefe``: all modules.
    * ``planner``: ``PLANNER_MODULES`` only.
    """
    if rol in ("admin", "jefe"):
        return ALL_MODULES
    return list(PLANNER_MODULES)


def get_area_filter(user: dict | None = None) -> list[str] | None:
    """Return the list of allowed area strings, or *None* for unrestricted.

    * ``["*"]`` means all areas → returns ``None`` (no filter).
    * ``["NURSERY"]`` means only NURSERY → returns ``["NURSERY"]``.
    """
    if user is None:
        user = get_current_user()
    if user is None:
        return None
    areas = user.get("areas", ["*"])
    if "*" in areas:
        return None
    return list(areas)


def require_role(min_role: str) -> bool:
    """Check whether the current user meets or exceeds *min_role*.

    Returns ``True`` if the user's role level >= *min_role* level.
    Returns ``False`` if not logged in or insufficient privileges.
    """
    user = get_current_user()
    if user is None:
        return False
    user_level = ROLE_LEVELS.get(user.get("rol", ""), 0)
    required_level = ROLE_LEVELS.get(min_role, 99)
    return user_level >= required_level
