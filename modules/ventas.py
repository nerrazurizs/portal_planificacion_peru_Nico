import streamlit as st
import pandas as pd
from datetime import datetime
from config import DIMENSIONES_VENTA, apply_pm_filter
from utils.sql_builder import build_in_clause, build_ilike, append_condition
from utils.filters import limpiar_lista, fmt_clp
from utils.export import download_buttons
from utils.ui_animations import lottie_spinner
from db.queries import _VCM, _PROD


@st.cache_data(ttl=3600, show_spinner=False)
def _load_distinct(_conn, col: str) -> list[str]:
    """Load distinct non-null values for a dimension column. Cached 1 hour."""
    df = pd.read_sql(
        f"SELECT DISTINCT p.{col} FROM {_PROD} p "
        f"WHERE p.{col} IS NOT NULL AND TRIM(CAST(p.{col} AS VARCHAR)) != '' ORDER BY p.{col}",
        _conn,
    )
    return df.iloc[:, 0].dropna().astype(str).str.strip().tolist()


def render_ventas(conn):
    st.html("<h2 class='sub-header'>Consulta de Ventas</h2>")

    col_conf, col_filt = st.columns([1, 2])

    with col_conf:
        st.subheader("Configuracion")
        dims_agrup = [
            info["nombre"]
            for info in DIMENSIONES_VENTA.values()
            if info["tipo"] == "agrupacion"
        ]
        dims_desc = [
            info["nombre"]
            for info in DIMENSIONES_VENTA.values()
            if info["tipo"] == "descriptivo"
        ]

        sel_agrup = st.multiselect(
            "Agrupacion", dims_agrup, default=["linea", "canal_de_distribucion"]
        )
        sel_desc = st.multiselect("Descriptivos", dims_desc)

        col_f1, col_f2 = st.columns(2)
        fecha_inicio = col_f1.date_input("Inicio", value=datetime(2026, 1, 1))
        fecha_fin = col_f2.date_input("Fin", value=datetime(2026, 1, 31))

    # Load distinct values for dropdown filters (cached 1hr)
    with lottie_spinner("snowflake"):
        opts_area = _load_distinct(conn, "AREA")
        opts_linea = _load_distinct(conn, "LINEA")
        opts_sublinea = _load_distinct(conn, "SUBLINEA")
        opts_marca = _load_distinct(conn, "MARCA")
        opts_modelo = _load_distinct(conn, "MODELO")
        opts_proveedor = _load_distinct(conn, "PROVEEDOR")

    filtros = {}
    with col_filt:
        st.subheader("Filtros")
        with st.expander("Ver Filtros", expanded=True):
            c1, c2, c3 = st.columns(3)

            # Col 1 — SKU + Sucursal (paste), Nombre (contiene)
            filtros["sku_producto"] = limpiar_lista(c1.text_area("SKUs (separados por coma/espacio)"))
            filtros["id_sucursal"] = limpiar_lista(c1.text_area("Sucursales (ID)"))
            filtros["nom_producto"] = c1.text_input("Nombre Producto (contiene)")

            # Col 2 — Area, Linea, Sublinea, Marca (multiselect from DB)
            filtros["area"] = c2.multiselect("Area", opts_area)
            filtros["linea"] = c2.multiselect("Linea", opts_linea)
            filtros["sublinea"] = c2.multiselect("Sublinea", opts_sublinea)
            filtros["marca"] = c2.multiselect("Marca", opts_marca)

            # Col 3 — Modelo, Proveedor, Canal, Mix
            filtros["modelo"] = c3.multiselect("Modelo", opts_modelo)
            filtros["proveedor"] = c3.multiselect("Proveedor", opts_proveedor)
            filtros["canal_de_distribucion"] = c3.multiselect(
                "Canal", ["TIENDA", "CD", "ETAIL", "MAYOR"]
            )
            filtros["mix_oficial"] = c3.multiselect(
                "Mix Oficial",
                ["MIX", "IN & OUT", "FUERA DE MIX", "DESCONTINUADO"],
            )

    if st.button("Ejecutar Ventas", type="primary"):
        dims_seleccionadas = sel_agrup + sel_desc
        if not dims_seleccionadas:
            st.error("Seleccione agrupacion")
            return

        # Build SELECT fields from dimension map
        select_fields = []
        for nombre in dims_seleccionadas:
            for info in DIMENSIONES_VENTA.values():
                if info["nombre"] == nombre:
                    select_fields.append(info["sql"])
                    break

        metricas = [
            "sum(a.cantidad) as unidades_vendidas",
            "sum(a.neto) as neto_total",
            "sum(a.neto) / nullif(sum(a.cantidad), 0) as precio_promedio",
            "sum(a.aporte) as aporte_total",
            "sum(a.aporte) / nullif(sum(a.neto), 0) as margen",
        ]

        # Base query with parameterized dates
        # Peru: use _VCM and _PROD wrappers for column normalization
        query = (
            f"select {','.join(select_fields + metricas)} "
            f"from {_VCM} a "
            "left join db_syncros.public.coo_maestro_sucursal b on a.cod_ccosto = b.id_sucursal "
            f"left join {_PROD} c on a.sku_producto = c.sku_producto "
            "where a.fecha between %s and %s"
        )
        params = [str(fecha_inicio), str(fecha_fin)]

        # Dynamic filters — field_key → SQL column (all use IN clause)
        field_map_in = {
            "sku_producto": "a.sku_producto",
            "id_sucursal": "b.id_sucursal",
            "area": "c.area",
            "linea": "c.linea",
            "sublinea": "c.sublinea",
            "marca": "c.marca",
            "modelo": "c.modelo",
            "proveedor": "c.proveedor",
            "canal_de_distribucion": "b.canal_de_distribucion",
            "mix_oficial": "c.mix_oficial",
        }

        for campo, valor in filtros.items():
            if not valor:
                continue

            # ILIKE field (nombre producto "contiene")
            if campo == "nom_producto":
                frag, p = build_ilike("c.sku_nom_producto", valor)
                if frag:
                    query += f" and {frag}"
                    params.extend(p)
                continue

            # IN clause fields (multiselect or paste lists)
            sql_field = field_map_in.get(campo)
            if sql_field and isinstance(valor, list) and valor:
                frag, p = build_in_clause(sql_field, valor)
                if frag:
                    query += f" and {frag}"
                    params.extend(p)

        # GROUP BY
        if select_fields:
            query += f" group by {','.join(str(i + 1) for i in range(len(select_fields)))}"

        try:
            with lottie_spinner("snowflake"):
                df = pd.read_sql(query, conn, params=params)
                df.columns = [c.upper() for c in df.columns]
                df = apply_pm_filter(df)

                st.toast(f"Ventas consultadas: {len(df):,} filas")

                for c in ("NETO_TOTAL", "PRECIO_PROMEDIO", "APORTE_TOTAL"):
                    if c in df.columns:
                        df[c] = df[c].apply(fmt_clp)

                cfg = {
                    "UNIDADES_VENDIDAS": st.column_config.NumberColumn("Unds", format="%d"),
                    "NETO_TOTAL": st.column_config.TextColumn("Venta Neta"),
                    "PRECIO_PROMEDIO": st.column_config.TextColumn("Precio Prom."),
                    "APORTE_TOTAL": st.column_config.TextColumn("Aporte"),
                    "MARGEN": st.column_config.NumberColumn("Margen %", format="%.1f %%"),
                }

                st.dataframe(df.head(500), column_config=cfg, use_container_width=True)
                download_buttons(df, "ventas")
        except Exception as e:
            st.error(f"Error: {e}")
