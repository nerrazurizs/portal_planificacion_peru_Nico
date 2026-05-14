"""Persistence layer for projection input files and results.

Saves uploaded files (Forecast, Plan Compra, Precios) to ``data/inputs/``
so they survive app restarts.  Also persists projection results (df_proy,
df_proy_daily) as parquet files to avoid re-running the simulation.
Includes an admin-only git push helper to share files with the team.
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parent.parent
_INPUTS_DIR = _BASE_DIR / "data" / "inputs"
_METADATA_FILE = _INPUTS_DIR / "metadata.json"

# Fixed filenames on disk (original name stored in metadata)
_FILE_KEYS = {
    "forecast": "forecast.xlsx",
    "compra": "compra.csv",
    "precios": "precios.xlsx",
}

# Projection result files (parquet for speed + compression)
_PROY_FILE = _INPUTS_DIR / "proy_result.parquet"
_PROY_DAILY_FILE = _INPUTS_DIR / "proy_daily.parquet"
_PROY_META_KEYS = ["proy_generated_at", "proy_sim_mode", "proy_n_skus", "proy_n_periodos"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    """Create ``data/inputs/`` if it does not exist."""
    _INPUTS_DIR.mkdir(parents=True, exist_ok=True)


def _sanitize_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    """Fix mixed-type object columns that pyarrow cannot serialize.

    Common cases in the projection DataFrame:
    - MES_QUIEBRE: mix of pd.NaT / Timestamps (OK after the fix) or int 0 + Timestamps
    - Boolean columns that ended up as object due to concat with int rows
    - Any object column that should be numeric or datetime

    Returns a copy with problematic columns coerced to safe types.
    """
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object:
            # Try to infer a better dtype
            sample = df[col].dropna()
            if sample.empty:
                df[col] = df[col].astype(str).replace("None", "").replace("nan", "")
                continue
            first = sample.iloc[0]
            # Datetime-like: coerce to datetime64
            if isinstance(first, (pd.Timestamp,)):
                df[col] = pd.to_datetime(df[col], errors="coerce")
            # Boolean-like object: coerce to int
            elif isinstance(first, (bool,)):
                df[col] = df[col].fillna(False).astype(int)
            else:
                # Try numeric; fallback to string
                try:
                    df[col] = pd.to_numeric(df[col], errors="raise").fillna(0)
                except Exception:
                    df[col] = df[col].fillna("").astype(str)
        # Downcast boolean to int8 for parquet compatibility
        elif df[col].dtype == bool:
            df[col] = df[col].astype("int8")
    return df


def _save_metadata(meta: dict) -> None:
    """Write metadata.json."""
    _ensure_dir()
    with open(_METADATA_FILE, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_metadata() -> dict:
    """Read ``metadata.json``.  Returns empty dict if missing or malformed."""
    if not _METADATA_FILE.exists():
        return {}
    try:
        with open(_METADATA_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def save_input_file(
    key: str,
    uploaded_file: BinaryIO,
    user: dict,
) -> None:
    """Save an uploaded file to ``data/inputs/`` and update metadata.

    Parameters
    ----------
    key : str
        One of ``"forecast"``, ``"compra"``, ``"precios"``.
    uploaded_file : BinaryIO
        Streamlit ``UploadedFile`` (BytesIO-like).
    user : dict
        Current user dict from ``get_current_user()``.
    """
    if key not in _FILE_KEYS:
        raise ValueError(f"Unknown file key: {key}")

    _ensure_dir()
    dest = _INPUTS_DIR / _FILE_KEYS[key]

    # Write bytes to disk
    uploaded_file.seek(0)
    with open(dest, "wb") as fh:
        fh.write(uploaded_file.read())
    uploaded_file.seek(0)  # reset so downstream can still read it

    # Update metadata
    meta = load_metadata()
    meta[key] = {
        "filename": getattr(uploaded_file, "name", _FILE_KEYS[key]),
        "saved_as": _FILE_KEYS[key],
        "uploaded_by": user.get("email", "unknown"),
        "uploaded_by_name": user.get("nombre", "Desconocido"),
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        "size_bytes": dest.stat().st_size,
    }
    _save_metadata(meta)


def get_saved_file_path(key: str) -> Path | None:
    """Return the ``Path`` to a saved file, or ``None`` if it does not exist."""
    if key not in _FILE_KEYS:
        return None
    path = _INPUTS_DIR / _FILE_KEYS[key]
    return path if path.exists() else None


def has_saved_files() -> bool:
    """Return ``True`` if both required files (forecast + compra) are saved."""
    return (
        get_saved_file_path("forecast") is not None
        and get_saved_file_path("compra") is not None
    )


def get_saved_file_info(key: str) -> dict | None:
    """Return metadata dict for a saved file, or ``None``."""
    meta = load_metadata()
    info = meta.get(key)
    if info and get_saved_file_path(key) is not None:
        return info
    return None


def git_share_inputs() -> tuple[bool, str]:
    """Git add + commit + push ``data/inputs/``.

    Returns ``(success, message)``.  Only meant to be called by admin users.
    """
    cwd = str(_BASE_DIR)
    try:
        # Stage
        subprocess.run(
            ["git", "add", "data/inputs/"],
            cwd=cwd, check=True, capture_output=True, text=True, timeout=30,
        )

        # Anything to commit?
        status = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=cwd, check=True, capture_output=True, text=True, timeout=10,
        )
        if not status.stdout.strip():
            return True, "No hay cambios nuevos para compartir."

        # Commit
        from utils.auth import get_current_user
        user = get_current_user()
        author = user.get("nombre", "Portal") if user else "Portal"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg = f"datos: inputs proyeccion actualizados por {author} ({ts})"

        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=cwd, check=True, capture_output=True, text=True, timeout=30,
        )

        # Push
        subprocess.run(
            ["git", "push"],
            cwd=cwd, check=True, capture_output=True, text=True, timeout=60,
        )

        # Record share info in metadata
        meta = load_metadata()
        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        meta["last_shared"] = {
            "shared_by": user.get("email", "unknown") if user else "unknown",
            "shared_by_name": user.get("nombre", "Desconocido") if user else "Desconocido",
            "shared_at": datetime.now().isoformat(timespec="seconds"),
            "commit_hash": hash_result.stdout.strip() if hash_result.returncode == 0 else "",
        }
        _save_metadata(meta)

        return True, "Archivos compartidos exitosamente con el equipo."

    except subprocess.TimeoutExpired:
        return False, "Timeout: la operacion git tomo demasiado tiempo."
    except subprocess.CalledProcessError as e:
        return False, f"Error git: {e.stderr or e.stdout or str(e)}"
    except Exception as e:
        return False, f"Error inesperado: {e}"


# ---------------------------------------------------------------------------
# Projection results persistence
# ---------------------------------------------------------------------------

def save_projection_results(
    df_proy: pd.DataFrame,
    df_proy_daily: pd.DataFrame | None = None,
    sim_mode: str = "diaria",
) -> None:
    """Save projection results to parquet files for fast reload.

    Parameters
    ----------
    df_proy : DataFrame
        Monthly aggregated projection (the main result).
    df_proy_daily : DataFrame, optional
        Daily detail (can be large, saved if provided).
    sim_mode : str
        Simulation mode used ("diaria" or "mensual").
    """
    _ensure_dir()
    try:
        df_save = _sanitize_for_parquet(df_proy)
        df_save.to_parquet(_PROY_FILE, index=False, engine="pyarrow")

        if df_proy_daily is not None and not df_proy_daily.empty:
            _sanitize_for_parquet(df_proy_daily).to_parquet(
                _PROY_DAILY_FILE, index=False, engine="pyarrow"
            )

        # Update metadata with projection info
        meta = load_metadata()
        meta["projection"] = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "sim_mode": sim_mode,
            "n_skus": int(df_save["SKU_PRODUCTO"].nunique()) if "SKU_PRODUCTO" in df_save.columns else 0,
            "n_periodos": int(df_save["PERIODO"].nunique()) if "PERIODO" in df_save.columns else 0,
            "n_rows": len(df_save),
            "file_size_mb": round(_PROY_FILE.stat().st_size / 1_048_576, 1),
        }
        _save_metadata(meta)
    except Exception as _e:
        import streamlit as st  # import here to avoid circular deps at module level
        try:
            st.warning(f"⚠️ No se pudo guardar el parquet de proyección: {_e}")
        except Exception:
            pass  # fuera de contexto Streamlit (tests, CLI)


def load_projection_results() -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Load projection results from parquet files.

    Returns ``(df_proy, df_proy_daily)`` or ``(None, None)`` if files
    don't exist or fail to load.
    """
    df_proy = None
    df_proy_daily = None

    try:
        if _PROY_FILE.exists():
            df_proy = pd.read_parquet(_PROY_FILE, engine="pyarrow")
        if _PROY_DAILY_FILE.exists():
            df_proy_daily = pd.read_parquet(_PROY_DAILY_FILE, engine="pyarrow")
    except Exception:
        return None, None

    return df_proy, df_proy_daily


def has_saved_projection() -> bool:
    """Return True if a saved projection result exists on disk."""
    return _PROY_FILE.exists()


def get_projection_info() -> dict | None:
    """Return metadata about the saved projection, or None."""
    meta = load_metadata()
    info = meta.get("projection")
    if info and _PROY_FILE.exists():
        return info
    return None


def clear_projection_cache() -> None:
    """Delete saved projection files (forces re-run next time)."""
    for f in [_PROY_FILE, _PROY_DAILY_FILE]:
        if f.exists():
            f.unlink()
    meta = load_metadata()
    meta.pop("projection", None)
    _save_metadata(meta)
