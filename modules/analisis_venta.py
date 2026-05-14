import streamlit as st
import pandas as pd
from datetime import datetime, date

from config import apply_pm_filter
from utils.sql_builder import build_in_clause, build_ilike
from utils.filters import limpiar_lista
from utils.export import download_buttons
from utils.ui_animations import lottie_spinner
from db.queries import _VCM, _PROD, _SUCURSAL


@st.cache_data(ttl=3600, show_spinner=False)
def _load_distinct_av(_conn, col: str) -> list[str]:
    df = pd.read_sql(
        f"SELECT DISTINCT p.{col} FROM {_PROD} p "
        f"WHERE p.{col} IS NOT NULL AND TRIM(CAST(p.{col} AS VARCHAR)) != '' ORDER BY p.{col}",
        _conn,
    )
    return df.iloc[:, 0].dropna().astype(str).str.strip().tolist()


@st.cache_data(ttl=3600, show_spinner=False)
def _load_canales_av(_conn) -> list[str]:
    # Usa _SUCURSAL para que RETAIL ya llegue normalizado como TIENDA,
    # igual que los valores que produce el WHERE en _build_query.
    df = pd.read_sql(
        f"SELECT DISTINCT canal FROM ("
        f"  SELECT canal_de_distribucion AS canal FROM {_SUCURSAL} "
        f"  WHERE canal_de_distribucion IS NOT NULL "
        f"  UNION "
        f"  SELECT CASE TRIM(cod_canal) "
        f"    WHEN '03' THEN 'TIENDA' "
        f"    WHEN '02' THEN 'MAYORISTA' "
        f"    WHEN '06' THEN 'ETAIL' "
        f"  END AS canal "
        f"  FROM db_dimensiones.dim.dt_ccosto "
        f"  WHERE TRIM(cod_canal) IN ('02','03','06') "
        f") sub WHERE canal IS NOT NULL ORDER BY canal",
        _conn,
    )
    return df.iloc[:, 0].dropna().astype(str).str.strip().tolist()


def _build_query(fecha_inicio, fecha_fin, filtros: dict) -> tuple[str, list]:
    """Build the ventas query for pivot consumption."""
    select_dims = (
        # Jerarquía temporal — todas las columnas disponibles para el pivot
        "a.fecha AS fecha, "
        "YEAR(a.fecha) AS anio, "
        "'Q' || CAST(QUARTER(a.fecha) AS VARCHAR) AS trimestre, "
        "TO_CHAR(DATE_TRUNC('month', a.fecha), 'YYYY-MM') AS mes, "
        "CAST(YEAR(a.fecha) AS VARCHAR) || '-W' "
        "  || LPAD(CAST(WEEKOFYEAR(a.fecha) AS VARCHAR), 2, '0') AS semana, "
        "COALESCE(b.canal_de_distribucion, "
        "  CASE TRIM(d.cod_canal) "
        "    WHEN '03' THEN 'TIENDA' "
        "    WHEN '02' THEN 'MAYORISTA' "
        "    WHEN '06' THEN 'ETAIL' "
        "    ELSE TRIM(d.nom_ccosto) "
        "  END) AS canal_de_distribucion, "
        "COALESCE(b.id_sucursal, TRIM(d.cod_ccosto)) AS id_sucursal, "
        "COALESCE(b.descripcion_sucursal, d.nom_ccosto, alm.nom_almacen) AS nom_ccosto, "
        "COALESCE(alm.nom_almacen, d.nom_ccosto, b.descripcion_sucursal) AS nom_almacen, "
        "a.sku_producto, "
        "COALESCE(c.nom_producto, a.sku_producto) AS nom_producto, "
        "COALESCE(c.area, 'SIN ASIGNAR') AS area, "
        "COALESCE(c.linea, 'SIN ASIGNAR') AS linea, "
        "COALESCE(c.sublinea, 'SIN ASIGNAR') AS sublinea, "
        "COALESCE(c.marca, 'SIN ASIGNAR') AS marca, "
        "COALESCE(c.mix_oficial, 'SIN ASIGNAR') AS mix_oficial"
    )

    select_metrics = (
        "SUM(a.cantidad) AS unidades_vendidas, "
        "SUM(a.neto) AS neto_total, "
        "SUM(a.aporte) AS aporte_total"
    )

    query = (
        f"SELECT {select_dims}, {select_metrics} "
        f"FROM {_VCM} a "
        # _SUCURSAL normaliza RETAIL → TIENDA, garantizando que el valor del
        # campo canal_de_distribucion coincida con lo que el usuario selecciona.
        f"LEFT JOIN {_SUCURSAL} b ON a.cod_ccosto = b.id_sucursal "
        "LEFT JOIN db_dimensiones.dim.dt_ccosto d ON TRIM(a.cod_ccosto) = TRIM(d.cod_ccosto) "
        "LEFT JOIN db_dimensiones.dim.dt_almacen alm ON TRIM(a.cod_ccosto) = TRIM(alm.cod_almacen) "
        f"LEFT JOIN {_PROD} c ON a.sku_producto = c.sku_producto "
        "WHERE a.fecha BETWEEN %s AND %s"
    )
    params: list = [str(fecha_inicio), str(fecha_fin)]

    field_map_in = {
        # CAST a VARCHAR por si cod_producto es numérico en ft_vcm
        "sku_producto": "CAST(a.sku_producto AS VARCHAR)",
        "area": "c.area",
        "linea": "c.linea",
        "sublinea": "c.sublinea",
        "marca": "c.marca",
        # b.canal_de_distribucion ya viene normalizado (RETAIL→TIENDA) via _SUCURSAL
        "canal_de_distribucion": (
            "COALESCE(b.canal_de_distribucion, "
            "CASE TRIM(d.cod_canal) "
            "  WHEN '03' THEN 'TIENDA' "
            "  WHEN '02' THEN 'MAYORISTA' "
            "  WHEN '06' THEN 'ETAIL' "
            "  ELSE TRIM(d.nom_ccosto) "
            "END)"
        ),
        "mix_oficial": "c.mix_oficial",
    }

    for campo, valor in filtros.items():
        if not valor:
            continue
        if campo == "nom_producto":
            frag, p = build_ilike("c.nom_producto", valor)
            if frag:
                query += f" AND {frag}"
                params.extend(p)
            continue
        sql_field = field_map_in.get(campo)
        if sql_field and isinstance(valor, list) and valor:
            frag, p = build_in_clause(sql_field, valor)
            if frag:
                query += f" AND {frag}"
                params.extend(p)

    # 16 dims: fecha, anio, trimestre, mes, semana + canal + id_sucursal +
    #          nom_ccosto + nom_almacen + sku + nom_producto + area +
    #          linea + sublinea + marca + mix_oficial
    query += (
        " GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16"
        " ORDER BY 1"
    )
    return query, params


def render_analisis_venta(conn):
    st.html("<h2 class='sub-header'>Analisis de Venta</h2>")

    # ── Dropdown options: carga una sola vez por sesión ───────────────────────
    if "av_opts" not in st.session_state:
        with lottie_spinner("snowflake"):
            st.session_state["av_opts"] = {
                "area":     _load_distinct_av(conn, "AREA"),
                "linea":    _load_distinct_av(conn, "LINEA"),
                "sublinea": _load_distinct_av(conn, "SUBLINEA"),
                "marca":    _load_distinct_av(conn, "MARCA"),
                "canales":  _load_canales_av(conn),
            }
    opts = st.session_state["av_opts"]

    with st.expander("Filtros y configuracion", expanded=True):
        c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.2, 0.8])

        with c1:
            fecha_inicio = st.date_input("Fecha inicio", value=date(datetime.now().year, 1, 1), key="av_fi")
            fecha_fin = st.date_input("Fecha fin", value=date.today(), key="av_ff")
            st.caption("La query incluye Fecha, Semana, Mes, Trimestre y Año — arrastralos en el pivot.")

        with c2:
            filtros: dict = {}
            filtros["area"] = st.multiselect("Area", opts["area"], key="av_area")
            filtros["linea"] = st.multiselect("Linea", opts["linea"], key="av_linea")
            filtros["sublinea"] = st.multiselect("Sublinea", opts["sublinea"], key="av_sublinea")

        with c3:
            filtros["marca"] = st.multiselect("Marca", opts["marca"], key="av_marca")
            filtros["canal_de_distribucion"] = st.multiselect("Canal", opts["canales"], key="av_canal")
            filtros["mix_oficial"] = st.multiselect(
                "Mix Oficial",
                ["MIX", "IN & OUT", "FUERA DE MIX", "DESCONTINUADO"],
                key="av_mix",
            )

        with c4:
            filtros["sku_producto"] = limpiar_lista(st.text_area("SKUs", key="av_sku", height=80))
            filtros["nom_producto"] = st.text_input("Nombre producto (contiene)", key="av_nom")

        ejecutar = st.button("Consultar", type="primary", key="av_run")

    # ── Query execution ───────────────────────────────────────────────────────
    if ejecutar:
        query, params = _build_query(fecha_inicio, fecha_fin, filtros)
        try:
            with lottie_spinner("snowflake"):
                df = pd.read_sql(query, conn, params=params)

            df.columns = [c.upper() for c in df.columns]
            df = apply_pm_filter(df)

            if df.empty:
                st.warning("La consulta no devolvio resultados con los filtros seleccionados.")
                return

            df["FECHA"] = pd.to_datetime(df["FECHA"], errors="coerce")
            for col in ("UNIDADES_VENDIDAS", "NETO_TOTAL", "APORTE_TOTAL"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

            st.session_state["av_df"] = df
            st.toast(f"{len(df):,} filas cargadas")

        except Exception as e:
            st.error(f"Error al consultar: {e}")
            return

    # ── Pivot table ───────────────────────────────────────────────────────────
    df: pd.DataFrame | None = st.session_state.get("av_df")
    if df is None or df.empty:
        st.info("Configura los filtros y presiona **Consultar** para cargar datos.")
        return

    st.markdown(f"**{len(df):,} filas**")

    metric_cols = [c for c in ("NETO_TOTAL", "UNIDADES_VENDIDAS", "APORTE_TOTAL") if c in df.columns]
    dim_cols = [c for c in df.columns if c not in metric_cols and c != "FECHA"]

    pc1, pc2, pc3 = st.columns(3)
    with pc1:
        sel_rows = st.multiselect("Filas", dim_cols, default=["MES", "AREA", "LINEA"] if all(c in dim_cols for c in ["MES", "AREA", "LINEA"]) else dim_cols[:2], key="av_pivot_rows")
    with pc2:
        col_opts = [c for c in dim_cols if c not in sel_rows]
        sel_cols = st.multiselect("Columnas", col_opts, default=["CANAL_DE_DISTRIBUCION"] if "CANAL_DE_DISTRIBUCION" in col_opts else [], key="av_pivot_cols")
    with pc3:
        sel_metric = st.selectbox("Métrica", metric_cols, key="av_pivot_metric")

    if sel_rows and sel_metric:
        try:
            pivot = df.pivot_table(
                index=sel_rows,
                columns=sel_cols if sel_cols else None,
                values=sel_metric,
                aggfunc="sum",
                margins=True,
                margins_name="Total",
            )
            st.dataframe(pivot.style.format("{:,.0f}"), use_container_width=True)
        except Exception as e:
            st.warning(f"No se pudo generar el pivot: {e}")
            st.dataframe(df[sel_rows + (sel_cols or []) + [sel_metric]], use_container_width=True)
    else:
        st.info("Selecciona al menos una fila y una métrica.")

    st.markdown("---")
    download_buttons(df, "analisis_venta")
