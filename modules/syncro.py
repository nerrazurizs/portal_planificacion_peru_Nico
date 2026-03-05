import streamlit as st
import pandas as pd
from db.queries import (
    QUERY_SYNCRO_CONFIG,
    QUERY_SYNCRO_SUCURSAL,
    QUERY_SYNCRO_PRODUCTO,
    QUERY_SYNCRO_LEADTIMES,
)
from utils.sql_builder import build_multi_value_filter, build_in_clause, build_ilike, append_condition
from utils.filters import limpiar_lista
from utils.export import download_buttons

from utils.ui_animations import lottie_spinner
from config import apply_pm_filter


def _safe_read_sql(conn, query, params):
    """Execute a Snowflake query, handling hex-float errors ('0x1.0p0').

    Strategy:
    1. Try normal pd.read_sql first.
    2. On hex-float error (Snowflake 100038), get column metadata via LIMIT 0,
       then re-issue the query wrapping every FLOAT/NUMBER column with
       TRY_TO_DOUBLE() which gracefully parses hex-float representations.
    """
    _params = params if params else None
    try:
        return pd.read_sql(query, conn, params=_params)
    except Exception as e:
        if "0x" not in str(e) and "100038" not in str(e):
            raise

    # ── Fallback: discover column types via LIMIT 0 (reads 0 rows → no
    # hex-float error), then re-select with TRY_TO_DOUBLE wrapping. ──
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM ({query}) _meta LIMIT 0", _params)
    col_descs = cur.description  # (name, type_code, display_size, ...)

    safe_cols = []
    for desc in col_descs:
        name = desc[0]
        type_code = desc[1]
        # Snowflake type codes: 0=FIXED(NUMBER/INT), 1=REAL(FLOAT/DOUBLE),
        # 6=FIXED, etc.  Wrap any numeric type to be safe.
        if type_code in (0, 1, 4, 5, 6):
            safe_cols.append(f'TRY_TO_DOUBLE("{name}") AS "{name}"')
        else:
            safe_cols.append(f'"{name}"')

    safe_query = f"SELECT {', '.join(safe_cols)} FROM ({query}) _sq"
    cur.execute(safe_query, _params)
    return cur.fetch_pandas_all()


def render_syncro(conn):
    st.html("<h2 class='sub-header'>Tablas Syncro</h2>")

    opcion = st.selectbox(
        "Seleccione Tabla a Consultar",
        [
            "COO_CONFIG_SKU (Configuracion SKU/Sucursal)",
            "MAESTRO SUCURSAL",
            "MAESTRO PRODUCTO (Syncro)",
            "LEADTIMES PROVEEDOR SKU",
        ],
    )

    query = None
    params = []
    archivo = "syncro"

    if "COO_CONFIG_SKU" in opcion:
        st.info("Filtros opcionales para Config SKU:")
        c1, c2 = st.columns(2)
        f_sku = limpiar_lista(c1.text_area("SKU (Sep. por coma/espacio)", key="cfg_sku"))
        f_suc = limpiar_lista(c2.text_area("ID Sucursal (Sep. por coma/espacio)", key="cfg_suc"))

        conditions = []
        params_list = []

        if f_sku:
            frag, p = build_in_clause("ID_MATERIAL", f_sku)
            append_condition(conditions, params_list, frag, p)

        if f_suc:
            frag, p = build_in_clause("ID_SUCURSAL", f_suc)
            append_condition(conditions, params_list, frag, p)

        query = QUERY_SYNCRO_CONFIG
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
            for p in params_list:
                params.extend(p)

        archivo = "syncro_config_sku"

    elif "MAESTRO SUCURSAL" in opcion:
        st.info("Filtros opcionales para Maestro Sucursal:")
        c1, c2 = st.columns(2)
        f_suc = limpiar_lista(c1.text_area("ID Sucursal (Sep. por coma/espacio)", key="suc_id"))
        f_canal = c2.multiselect("Canal de Distribucion", ["TIENDA", "CD", "ETAIL", "MAYOR"], key="suc_canal")

        conditions = []
        params_list = []

        if f_suc:
            frag, p = build_in_clause("ID_SUCURSAL", f_suc)
            append_condition(conditions, params_list, frag, p)

        if f_canal:
            frag, p = build_in_clause("CANAL_DE_DISTRIBUCION", f_canal)
            append_condition(conditions, params_list, frag, p)

        query = QUERY_SYNCRO_SUCURSAL
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
            for p in params_list:
                params.extend(p)

        archivo = "syncro_sucursal"

    elif "PRODUCTO" in opcion:
        st.info("Filtros opcionales para Maestro Producto:")
        c1, c2, c3 = st.columns(3)
        f_sku = limpiar_lista(c1.text_area("SKU (Sep. por coma/espacio)", key="prod_sku"))
        f_nom = c2.text_input("Nombre Producto (contiene)", key="prod_nom")
        f_marca = c3.text_input("Marca (contiene)", key="prod_marca")
        f_linea = c3.text_input("Linea (contiene)", key="prod_linea")

        conditions = []
        params_list = []

        if f_sku:
            frag, p = build_in_clause("ID_MATERIAL", f_sku)
            append_condition(conditions, params_list, frag, p)

        for field_val, col in [
            (f_nom, "NOM_PRODUCTO"),
            (f_marca, "MARCA"),
            (f_linea, "LINEA"),
        ]:
            frag, p = build_ilike(col, field_val)
            append_condition(conditions, params_list, frag, p)

        query = QUERY_SYNCRO_PRODUCTO
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
            for p in params_list:
                params.extend(p)

        archivo = "syncro_producto"

    elif "LEADTIMES" in opcion:
        st.info("Filtros opcionales para Leadtimes:")
        c1, c2, c3 = st.columns(3)
        f_sku = c1.text_area("SKU (Sep. por coma/espacio)")
        f_nom = c2.text_input("Nombre Producto")
        f_prov = c3.text_input("ID Proveedor")

        c4, c5 = st.columns(2)
        f_marca = c4.text_input("Marca")
        f_linea = c5.text_input("Linea")

        conditions = []
        params_list = []

        frag, p = build_multi_value_filter("ID_MATERIAL", f_sku)
        append_condition(conditions, params_list, frag, p)

        for field_val, col in [
            (f_nom, "NOM_PRODUCTO"),
            (f_prov, "ID_PROVEEDOR"),
            (f_marca, "MARCA"),
            (f_linea, "LINEA"),
        ]:
            frag, p = build_ilike(col, field_val)
            append_condition(conditions, params_list, frag, p)

        query = QUERY_SYNCRO_LEADTIMES
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
            for p in params_list:
                params.extend(p)

        archivo = "syncro_leadtimes"

    if st.button("Consultar Tabla", type="primary"):
        if query:
            try:
                with lottie_spinner("snowflake"):
                    df = _safe_read_sql(conn, query, params)
                    df.columns = [c.upper() for c in df.columns]
                df = apply_pm_filter(df)
                st.success(f"{len(df):,} filas")
                st.dataframe(df.head(500), use_container_width=True, height=500)
                download_buttons(df, archivo)
            except Exception as e:
                st.error(f"Error: {e}")
