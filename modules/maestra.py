import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from db.queries import QUERY_MAESTRA, _PROD
from config import apply_pm_filter
from utils.sql_builder import build_ilike, build_in_clause, append_condition
from utils.filters import limpiar_lista, fmt_clp
from utils.export import download_buttons
from utils.ui_animations import lottie_spinner

# ---------------------------------------------------------------------------
# Profiles persistence (JSON on disk, same pattern as file_persistence.py)
# ---------------------------------------------------------------------------

_PROFILES_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "inputs" / "maestra_profiles.json"
)


def _load_profiles() -> dict:
    """Read saved column profiles.  Returns empty dict if missing/corrupt."""
    if not _PROFILES_FILE.exists():
        return {}
    try:
        with open(_PROFILES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_profiles(profiles: dict) -> None:
    """Write column profiles to JSON."""
    _PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Cached helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def _load_distinct(_conn, col: str) -> list[str]:
    """Load distinct non-null values for a maestra column. Cached 1 hour."""
    df = pd.read_sql(
        f"SELECT DISTINCT p.{col} FROM {_PROD} p "
        f"WHERE p.{col} IS NOT NULL AND TRIM(CAST(p.{col} AS VARCHAR)) != '' ORDER BY p.{col}",
        _conn,
    )
    return df.iloc[:, 0].dropna().astype(str).str.strip().tolist()


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render_maestra(conn):
    st.html("<h2 class='sub-header'>Maestra de Productos</h2>")

    # Load distinct values for multiselects (cached, runs once per session)
    with lottie_spinner("snowflake"):
        opts_area        = _load_distinct(conn, "AREA")
        opts_linea       = _load_distinct(conn, "LINEA")
        opts_sublinea    = _load_distinct(conn, "SUBLINEA")
        opts_marca       = _load_distinct(conn, "MARCA")
        opts_modelo      = _load_distinct(conn, "MODELO")
        opts_mix         = _load_distinct(conn, "MIX_OFICIAL")
        opts_proveedor   = _load_distinct(conn, "PROVEEDOR")
        opts_cod_prov    = _load_distinct(conn, "COD_PROVEEDOR")
        opts_procedencia = _load_distinct(conn, "PROCEDENCIA")

    filtros = {}
    with st.expander("Filtros de Busqueda", expanded=True):
        c1, c2, c3 = st.columns(3)

        # Col 1 — SKU + Nombre producto
        filtros["sku"] = limpiar_lista(
            c1.text_area("SKUs (separados por coma/espacio)")
        )
        filtros["nom_producto"] = c1.text_input("Nombre Producto (contiene)")

        # Col 2 — Area, Linea, Sublinea, Marca, Modelo
        filtros["area"]       = c2.multiselect("Area", opts_area)
        filtros["linea"]      = c2.multiselect("Linea", opts_linea)
        filtros["sublinea"]   = c2.multiselect("Sublinea", opts_sublinea)
        filtros["marca"]      = c2.multiselect("Marca", opts_marca)
        filtros["modelo"]     = c2.multiselect("Modelo", opts_modelo)
        filtros["procedencia"] = c2.multiselect("Procedencia", opts_procedencia)

        # Col 3 — Mix Oficial, Proveedor, Cod Proveedor
        filtros["mix_oficial"]   = c3.multiselect("Mix Oficial", opts_mix)
        filtros["proveedor"]     = c3.multiselect("Proveedor", opts_proveedor)
        filtros["cod_proveedor"] = c3.multiselect("Cod Proveedor", opts_cod_prov)

    if st.button("Ejecutar Consulta Maestra", type="primary"):
        conditions = []
        params_list = []

        # SKUs — exact match list
        if filtros["sku"]:
            frag, p = build_in_clause("p.SKU_PRODUCTO", filtros["sku"])
            append_condition(conditions, params_list, frag, p)

        # Nombre producto — ILIKE (contiene)
        if filtros["nom_producto"]:
            frag, p = build_ilike("p.NOM_PRODUCTO", filtros["nom_producto"])
            append_condition(conditions, params_list, frag, p)

        # Multiselect filters — IN clause (exact values from list)
        for field, col in [
            ("area",          "p.AREA"),
            ("linea",         "p.LINEA"),
            ("sublinea",      "p.SUBLINEA"),
            ("marca",         "p.MARCA"),
            ("modelo",        "p.MODELO"),
            ("procedencia",   "p.PROCEDENCIA"),
            ("mix_oficial",   "p.MIX_OFICIAL"),
            ("proveedor",     "p.PROVEEDOR"),
            ("cod_proveedor", "p.COD_PROVEEDOR"),
        ]:
            if filtros[field]:
                frag, p = build_in_clause(col, filtros[field])
                append_condition(conditions, params_list, frag, p)

        # Build final query
        query = QUERY_MAESTRA
        flat_params = []
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
            for p in params_list:
                flat_params.extend(p)

        try:
            with lottie_spinner("snowflake"):
                df = pd.read_sql(query, conn, params=flat_params if flat_params else None)
                df.columns = [c.upper() for c in df.columns]
                df = apply_pm_filter(df)

            st.toast(f"Maestra cargada: {len(df):,} productos, {len(df.columns)} columnas")
            st.session_state["mae_df"] = df

        except Exception as e:
            st.error(f"Error: {e}")
            import traceback
            st.code(traceback.format_exc())
            return

    # ----- Display results (from session_state so profile changes don't re-query) -----
    df = st.session_state.get("mae_df")
    if df is None or df.empty:
        return

    st.divider()
    all_cols = list(df.columns)

    # --- Columnas y Perfiles ---
    profiles = _load_profiles()
    profile_names = ["Todas las columnas"] + sorted(profiles.keys())

    with st.expander("Columnas y Perfiles de Vista", expanded=True):
        pc1, pc2 = st.columns([3, 1])
        sel_profile = pc1.selectbox("Perfil guardado", profile_names, key="mae_profile")

        # Determine columns from profile
        if sel_profile != "Todas las columnas" and sel_profile in profiles:
            profile_cols = [c for c in profiles[sel_profile]["columns"] if c in all_cols]
        else:
            profile_cols = all_cols

        # When profile changes, sync multiselect via session_state and rerun
        _prev_profile = st.session_state.get("_mae_prev_profile")
        if _prev_profile != sel_profile:
            st.session_state["_mae_prev_profile"] = sel_profile
            st.session_state["mae_cols"] = profile_cols
            st.rerun()

        # Initialize mae_cols if not yet set (first render)
        if "mae_cols" not in st.session_state:
            st.session_state["mae_cols"] = profile_cols

        sel_cols = st.multiselect(
            "Columnas a mostrar",
            options=all_cols,
            key="mae_cols",
        )

        st.markdown("---")
        st.markdown("**Gestionar perfiles**")
        sc1, sc2, sc3 = st.columns([2, 1, 1])
        new_name = sc1.text_input("Nombre del nuevo perfil", key="mae_new_profile_name",
                                  placeholder="ej: Campos Elementales")
        if sc2.button("Guardar perfil", type="primary", use_container_width=True):
            if new_name.strip() and sel_cols:
                try:
                    from utils.auth import get_current_user
                    user = get_current_user() or {}
                except Exception:
                    user = {}
                profiles[new_name.strip()] = {
                    "columns": sel_cols,
                    "created_by": user.get("nombre", "Desconocido"),
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
                _save_profiles(profiles)
                st.toast(f"Perfil '{new_name.strip()}' guardado con {len(sel_cols)} columnas")
                st.rerun()
            else:
                st.warning("Ingresa un nombre y selecciona al menos una columna.")

        if sel_profile != "Todas las columnas":
            if sc3.button("Eliminar perfil", use_container_width=True):
                profiles.pop(sel_profile, None)
                _save_profiles(profiles)
                st.toast(f"Perfil '{sel_profile}' eliminado")
                st.session_state["_mae_prev_profile"] = None
                st.rerun()

    # ----- Apply column selection -----
    display_cols = sel_cols if sel_cols else all_cols
    df_display = df[display_cols].copy()

    # Format known columns if present
    if "ULTIMO_COSTO" in df_display.columns:
        df_display["ULTIMO_COSTO"] = df_display["ULTIMO_COSTO"].apply(fmt_clp)

    # Column configs for known columns
    col_config = {}
    if "COSTO_FOB_USD" in display_cols:
        col_config["COSTO_FOB_USD"] = st.column_config.NumberColumn("Costo FOB", format="$%,.2f")
    if "ULTIMO_COSTO" in display_cols:
        col_config["ULTIMO_COSTO"] = st.column_config.TextColumn("Ultimo Costo")
    if "FACTOR_IMPORTACION" in display_cols:
        col_config["FACTOR_IMPORTACION"] = st.column_config.NumberColumn("Factor Imp.", format="%.2f")
    if "SKU_PRODUCTO" in display_cols:
        col_config["SKU_PRODUCTO"] = st.column_config.TextColumn("SKU", width="medium")
    if "SKU_NOM_PRODUCTO" in display_cols:
        col_config["SKU_NOM_PRODUCTO"] = st.column_config.TextColumn("Nombre", width="large")

    st.caption(f"{len(df):,} productos | {len(display_cols)} columnas seleccionadas")
    st.dataframe(
        df_display.head(500),
        column_config=col_config,
        use_container_width=True,
        height=500,
    )
    download_buttons(df_display, "maestra_productos")
