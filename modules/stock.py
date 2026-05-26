import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from db.queries import QUERY_STOCK_BASE, _PROD
from db.cache import run_sql
from config import apply_pm_filter
from utils.sql_builder import build_in_clause, build_ilike, append_condition
from utils.filters import limpiar_lista, fmt_clp
from utils.export import download_buttons
from utils.ui_animations import lottie_spinner


@st.cache_data(ttl=3600, show_spinner=False)
def _load_distinct(_conn, col: str) -> list[str]:
    """Load distinct non-null values for a dimension column. Cached 1 hour."""
    df = pd.read_sql(
        f"SELECT DISTINCT p.{col} FROM {_PROD} p "
        f"WHERE p.{col} IS NOT NULL AND TRIM(CAST(p.{col} AS VARCHAR)) != '' ORDER BY p.{col}",
        _conn,
    )
    return df.iloc[:, 0].dropna().astype(str).str.strip().tolist()


@st.cache_data(ttl=1800, show_spinner=False)
def _load_canales(_conn) -> list[str]:
    """Load distinct canal values from maestro sucursal + dt_ccosto fallback."""
    df = pd.read_sql(
        "SELECT DISTINCT canal FROM ("
        "  SELECT canal_de_distribucion AS canal "
        "  FROM db_syncros.public.coo_maestro_sucursal "
        "  WHERE canal_de_distribucion IS NOT NULL "
        "  UNION "
        "  SELECT CASE TRIM(cod_canal) "
        "    WHEN '03' THEN 'TIENDA' "
        "    WHEN '02' THEN 'MAYORISTA' "
        "    WHEN '06' THEN 'ETAIL' "
        "  END AS canal "
        "  FROM db_dimensiones.dim.dt_ccosto "
        "  WHERE TRIM(cod_canal) IN ('02','03','06') "
        "  UNION SELECT 'CD' "
        ") sub WHERE canal IS NOT NULL ORDER BY canal",
        _conn,
    )
    return df.iloc[:, 0].dropna().astype(str).str.strip().tolist()


@st.cache_data(ttl=1800, show_spinner=False)
def _max_fecha_stock(_conn):
    """Return the latest available date in ht_in_stock."""
    df = pd.read_sql(
        "SELECT MAX(fecha) as max_fecha FROM db_supply.hst.ht_in_stock WHERE fecha < CURRENT_DATE()",
        _conn,
    )
    val = df.iloc[0, 0]
    if val is None:
        return (datetime.now() - timedelta(days=1)).date()
    return pd.to_datetime(val).date()


def render_stock(conn):
    st.html("<h2 class='sub-header'>Consulta de Stock</h2>")
    st.info("Muestra la foto de stock disponible. Incluye calculo de MOI y Antiguedad.")

    # Load distinct values for dropdown filters (cached 1hr)
    with lottie_spinner("snowflake"):
        opts_area      = _load_distinct(conn, "AREA")
        opts_linea     = _load_distinct(conn, "LINEA")
        opts_sublinea  = _load_distinct(conn, "SUBLINEA")
        opts_marca     = _load_distinct(conn, "MARCA")
        opts_proveedor = _load_distinct(conn, "PROVEEDOR")
        opts_canales   = _load_canales(conn)
        max_fecha      = _max_fecha_stock(conn)

    filtros = {}
    with st.expander("Filtros de Busqueda", expanded=True):
        c1, c2, c3 = st.columns(3)

        # Col 1 — SKU (paste), Sucursal (paste), Nombre (contiene)
        filtros["sku"] = limpiar_lista(c1.text_area("SKUs (separados por coma/espacio)"))
        filtros["sucursal"] = limpiar_lista(c2.text_area("ID Sucursales"))
        filtros["nom_producto"] = c1.text_input("Nombre Producto (contiene)")

        # Col 2 — Canal, Area, Linea, Sublinea (multiselect)
        filtros["canal"] = c2.multiselect(
            "Canal", opts_canales, default=opts_canales
        )
        filtros["area"] = c3.multiselect("Area", opts_area)
        filtros["linea"] = c3.multiselect("Linea", opts_linea)

        c4, c5, c6 = st.columns(3)
        filtros["sublinea"] = c4.multiselect("Sublinea", opts_sublinea)
        filtros["marca"] = c4.multiselect("Marca", opts_marca)
        filtros["proveedor"] = c5.multiselect("Proveedor", opts_proveedor)

        filtros["antiguedad"] = c5.multiselect(
            "Rango Antiguedad",
            ["< 3 meses", ">= 3 meses", ">= 6 meses", ">= 12 meses", ">= 24 meses", "Sin fecha ingreso"],
        )
        filtros["moi"] = c6.multiselect(
            "Rango MOI",
            ["< 3 meses", ">= 3 meses", ">= 6 meses", ">= 12 meses", ">= 24 meses", "Sin MOI"],
        )
        filtros["mix_oficial"] = c6.text_input("Mix Oficial (contiene)")

        # Date inputs — default a la ultima fecha disponible en ht_in_stock
        c10, c11 = st.columns(2)
        filtros["fecha_inicio"] = c10.date_input("Fecha Inicio", value=max_fecha)
        filtros["fecha_fin"]    = c11.date_input("Fecha Fin",    value=max_fecha)

    if st.button("Ejecutar Stock", type="primary"):
        # The base query uses %s for fecha_inicio and fecha_fin
        base_params = [str(filtros["fecha_inicio"]), str(filtros["fecha_fin"])]

        # Build outer WHERE conditions on the subquery result
        conditions = []
        params_list = []

        if filtros["sku"]:
            frag, p = build_in_clause("SKU_PRODUCTO", filtros["sku"])
            append_condition(conditions, params_list, frag, p)

        if filtros["sucursal"]:
            frag, p = build_in_clause("ID_SUCURSAL", filtros["sucursal"])
            append_condition(conditions, params_list, frag, p)

        if filtros["canal"]:
            frag, p = build_in_clause("CANAL_DE_DISTRIBUCION", filtros["canal"])
            append_condition(conditions, params_list, frag, p)

        # Multiselect IN clause filters (from DB)
        for field, col in [
            ("area", "AREA"),
            ("linea", "LINEA"),
            ("sublinea", "SUBLINEA"),
            ("marca", "MARCA"),
            ("proveedor", "PROVEEDOR"),
        ]:
            if filtros[field]:
                frag, p = build_in_clause(col, filtros[field])
                append_condition(conditions, params_list, frag, p)

        # ILIKE filters (contiene)
        for field, col in [
            ("nom_producto", "NOM_PRODUCTO"),
            ("mix_oficial", "MIX_OFICIAL"),
        ]:
            frag, p = build_ilike(col, filtros[field])
            append_condition(conditions, params_list, frag, p)

        if filtros["antiguedad"]:
            frag, p = build_in_clause("RANGO_ANTIGUEDAD", filtros["antiguedad"])
            append_condition(conditions, params_list, frag, p)

        if filtros["moi"]:
            frag, p = build_in_clause("RANGO_MOI", filtros["moi"])
            append_condition(conditions, params_list, frag, p)

        # Wrap base query as subquery, apply outer filters
        where_clause = ""
        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)

        final_query = f"SELECT * FROM ({QUERY_STOCK_BASE}) sub{where_clause}"
        # Flatten params_list (list of lists) into a single list
        flat_params = []
        for p in params_list:
            flat_params.extend(p)
        all_params = base_params + flat_params

        try:
            with lottie_spinner("snowflake"):
                df = run_sql(conn, final_query, all_params)
                df.columns = [c.upper() for c in df.columns]
                df = apply_pm_filter(df)

                st.toast(f"Busqueda completada: {len(df):,} registros")

                for c in ("STOCK_COSTO", "COSTO_PROM_90_CIA"):
                    if c in df.columns:
                        df[c] = df[c].apply(fmt_clp)

                st.dataframe(
                    df.head(500),
                    column_config={
                        "STOCK_UNIDADES": st.column_config.NumberColumn("Unidades", format="%d"),
                        "STOCK_COSTO": st.column_config.TextColumn("Costo Total"),
                        "COSTO_PROM_90_CIA": st.column_config.TextColumn("Costo Prom."),
                        "MOI": st.column_config.NumberColumn("MOI (Meses)", format="%.1f", help="Meses de Inventario"),
                        "ANTIGUEDAD_MESES": st.column_config.ProgressColumn(
                            "Antiguedad", format="%d meses", min_value=0, max_value=24, help="Antiguedad del ultimo ingreso"
                        ),
                        "PERFIL_TIENDAS": st.column_config.NumberColumn("Perfil", format="%d"),
                        "SKU_PRODUCTO": st.column_config.TextColumn("SKU", width="medium"),
                        "NOM_PRODUCTO": st.column_config.TextColumn("Nombre Producto", width="large"),
                    },
                    use_container_width=True,
                    height=500,
                )

                download_buttons(df, "stock_consolidado")
        except Exception as e:
            st.error(f"Error: {e}")
