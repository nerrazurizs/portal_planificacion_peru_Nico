"""Persistence layer using Streamlit session_state.

In Streamlit in Snowflake there is no writable filesystem.
Files uploaded by the user and projection results are kept in session_state
for the duration of the browser session.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import BinaryIO

import pandas as pd
import streamlit as st

# Session-state key prefixes
_FILE_KEY_PREFIX = "_fp_file_"
_PROJ_KEY = "_fp_projection"

_FILE_KEYS = {"forecast", "compra", "precios"}


# ---------------------------------------------------------------------------
# Uploaded input files
# ---------------------------------------------------------------------------

def save_input_file(key: str, uploaded_file: BinaryIO, user: dict) -> None:
    """Store an uploaded file in session_state."""
    if key not in _FILE_KEYS:
        raise ValueError(f"Unknown file key: {key}")
    uploaded_file.seek(0)
    raw = uploaded_file.read()
    uploaded_file.seek(0)
    st.session_state[_FILE_KEY_PREFIX + key] = {
        "bytes": raw,
        "filename": getattr(uploaded_file, "name", key),
        "uploaded_by": user.get("email", "unknown"),
        "uploaded_by_name": user.get("nombre", "Desconocido"),
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        "size_bytes": len(raw),
    }


def get_saved_file_path(key: str) -> io.BytesIO | None:
    """Return a fresh BytesIO for a saved file, or None if not saved.

    Returns BytesIO (not Path) — compatible with pd.read_csv / pd.read_excel.
    A new BytesIO is created each call so position is always at the start.
    """
    entry = st.session_state.get(_FILE_KEY_PREFIX + key)
    if entry is None:
        return None
    return io.BytesIO(entry["bytes"])


def has_saved_files() -> bool:
    """Return True if both required files (forecast + compra) are saved."""
    return (
        st.session_state.get(_FILE_KEY_PREFIX + "forecast") is not None
        and st.session_state.get(_FILE_KEY_PREFIX + "compra") is not None
    )


def get_saved_file_info(key: str) -> dict | None:
    """Return metadata dict for a saved file, or None."""
    entry = st.session_state.get(_FILE_KEY_PREFIX + key)
    if entry is None:
        return None
    return {k: v for k, v in entry.items() if k != "bytes"}


# ---------------------------------------------------------------------------
# Projection results
# ---------------------------------------------------------------------------

def save_projection_results(
    df_proy: pd.DataFrame,
    df_proy_daily: pd.DataFrame | None = None,
    sim_mode: str = "diaria",
) -> None:
    """Store projection results in session_state."""
    st.session_state[_PROJ_KEY] = {
        "df_proy": df_proy,
        "df_proy_daily": df_proy_daily,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sim_mode": sim_mode,
        "n_skus": int(df_proy["SKU_PRODUCTO"].nunique()) if "SKU_PRODUCTO" in df_proy.columns else 0,
        "n_periodos": int(df_proy["PERIODO"].nunique()) if "PERIODO" in df_proy.columns else 0,
        "n_rows": len(df_proy),
    }


def load_projection_results() -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Return (df_proy, df_proy_daily) from session_state."""
    entry = st.session_state.get(_PROJ_KEY)
    if entry is None:
        return None, None
    return entry.get("df_proy"), entry.get("df_proy_daily")


def has_saved_projection() -> bool:
    """Return True if projection results exist in session_state."""
    return st.session_state.get(_PROJ_KEY) is not None


def get_projection_info() -> dict | None:
    """Return metadata about the saved projection, or None."""
    entry = st.session_state.get(_PROJ_KEY)
    if entry is None:
        return None
    return {k: v for k, v in entry.items() if k not in ("df_proy", "df_proy_daily")}


def clear_projection_cache() -> None:
    """Remove saved projection results from session_state."""
    st.session_state.pop(_PROJ_KEY, None)


# ---------------------------------------------------------------------------
# Stubs for functionality not available in Streamlit in Snowflake
# ---------------------------------------------------------------------------

def load_metadata() -> dict:
    """Not available in SiS — returns empty dict."""
    return {}


def git_share_inputs() -> tuple[bool, str]:
    """Not available in Streamlit in Snowflake."""
    return False, "Sincronización git no disponible en Streamlit in Snowflake."
