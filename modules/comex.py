import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from db.queries import QUERY_COMEX_BASE, _PROD
from utils.sql_builder import (
    build_in_clause,
    build_ilike,
    build_multi_value_filter,
    build_provider_filter,
    append_condition,
)
from utils.export import download_buttons
from utils.filters import human_format
from utils.ui_animations import lottie_spinner
from config import COLORS, dorel_layout, apply_pm_filter


@st.cache_data(ttl=3600, show_spinner=False)
def _load_distinct(_conn, col):
    """Load distinct non-null values for a vw_producto column. Cached 1 hour."""
    df = pd.read_sql(
        f"SELECT DISTINCT p.{col} FROM {_PROD} p "
        f"WHERE p.{col} IS NOT NULL AND TRIM(CAST(p.{col} AS VARCHAR)) != '' ORDER BY p.{col}",
        _conn,
    )
    return df.iloc[:, 0].dropna().astype(str).str.strip().tolist()


def render_comex(conn):
    st.html("<h2 class='sub-header'>Comex (Importaciones)</h2>")

    # Pre-load dropdown options from DB
    opts_area = _load_distinct(conn, "AREA")
    opts_linea = _load_distinct(conn, "LINEA")
    opts_sublinea = _load_distinct(conn, "SUBLINEA")
    opts_marca = _load_distinct(conn, "MARCA")
    opts_modelo = _load_distinct(conn, "MODELO")

    with st.expander("Filtros Avanzados", expanded=True):
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            f_prov = st.text_input("Proveedor (contiene)").upper()
            f_carpeta = st.text_input("Carpeta Comex").upper()
            f_po = st.text_input("PO / Orden Compra").upper()

        with col2:
            f_sku = st.text_area("SKUs (copie y pegue, sep. coma/espacio)", height=80).upper()
            f_nom = st.text_input("Nombre Producto (contiene)").upper()
            f_mix = st.text_input("Mix Oficial").upper()

        with col3:
            f_area = st.multiselect("Area", opts_area)
            f_linea = st.multiselect("Linea", opts_linea)
            f_sublinea = st.multiselect("Sublinea", opts_sublinea)

        with col4:
            f_marca = st.multiselect("Marca", opts_marca)
            f_modelo = st.multiselect("Modelo", opts_modelo)

        col5, col6, col7, col8 = st.columns(4)
        _anio_opts = [""] + [str(y) for y in range(2024, 2028)]
        _mes_opts = [""] + [str(m) for m in range(1, 13)]
        with col5:
            f_anio_etd = st.selectbox("Anio ETD", _anio_opts, index=0)
        with col6:
            f_mes_etd = st.selectbox("Mes ETD", _mes_opts, index=0)
        with col7:
            f_anio_eta = st.selectbox("Anio ETA", _anio_opts, index=0)
        with col8:
            f_mes_eta = st.selectbox("Mes ETA", _mes_opts, index=0)

    if st.button("Consultar Comex", type="primary"):
        try:
            with lottie_spinner("comex"):
                conditions = []
                params_list = []

                # Provider (search across name OR code)
                frag, p = build_provider_filter("NOM_PROVEEDOR", "COD_PROVEEDOR", f_prov)
                append_condition(conditions, params_list, frag, p)

                # Multi-value text fields (ILIKE/IN auto-detect)
                for field_name, value in [
                    ("CARPETA_COMEX", f_carpeta),
                    ("PO", f_po),
                    ("SKU_PRODUCTO", f_sku),
                    ("MIX_OFICIAL", f_mix),
                ]:
                    frag, p = build_multi_value_filter(field_name, value)
                    append_condition(conditions, params_list, frag, p)

                # Nombre producto: ILIKE directo (búsqueda "contiene")
                frag, p = build_ilike("NOM_PRODUCTO", f_nom)
                append_condition(conditions, params_list, frag, p)

                # Multiselect fields (exact IN clause)
                for field_name, values in [
                    ("AREA", f_area),
                    ("LINEA", f_linea),
                    ("SUBLINEA", f_sublinea),
                    ("MARCA", f_marca),
                    ("MODELO", f_modelo),
                ]:
                    if values:
                        frag, p = build_in_clause(field_name, values)
                        append_condition(conditions, params_list, frag, p)

                # Date filters
                if f_anio_etd:
                    conditions.append("YEAR(ETD) = %s")
                    params_list.append([f_anio_etd])
                if f_mes_etd:
                    conditions.append("MONTH(ETD) = %s")
                    params_list.append([f_mes_etd])
                if f_anio_eta:
                    conditions.append("YEAR(ETA) = %s")
                    params_list.append([f_anio_eta])
                if f_mes_eta:
                    conditions.append("MONTH(ETA) = %s")
                    params_list.append([f_mes_eta])

                # Build final query
                final_query = QUERY_COMEX_BASE
                flat_params = []
                if conditions:
                    final_query += " AND " + " AND ".join(conditions)
                    for p in params_list:
                        flat_params.extend(p)

                df = pd.read_sql(final_query, conn, params=flat_params if flat_params else None)
                df.columns = [c.upper() for c in df.columns]
                # Deduplicate columns (safety: SELECT * may produce overlaps)
                df = df.loc[:, ~df.columns.duplicated()]
                df = apply_pm_filter(df)

                # Timeline Chart — ETA Puerto → Disponibilidad en CD
                if not df.empty:
                    df["CANTIDAD_FINAL_CORREGIDA"] = pd.to_numeric(
                        df.get("CANTIDAD_FINAL_CORREGIDA", 0), errors="coerce"
                    ).fillna(0)

                    mask_pendiente = (df["FECHA_RECEPCION_EN_CD"].isnull()) & (
                        df["CANTIDAD_FINAL_CORREGIDA"] > 0
                    )
                    df_chart = df[mask_pendiente].copy()

                    if not df_chart.empty and "ETA" in df_chart.columns:
                        df_chart["ETA"] = pd.to_datetime(df_chart["ETA"])
                        df_chart["MONTOMN"] = pd.to_numeric(
                            df_chart.get("MONTOMN", 0), errors="coerce"
                        ).fillna(0)

                        # Lag operativo: dias de puerto a CD segun procedencia
                        def _get_lag(proc):
                            p = str(proc).upper() if proc else ""
                            return 7 if "NACIONAL" in p else 14

                        if "PROCEDENCIA_OC" in df_chart.columns:
                            df_chart["_LAG"] = df_chart["PROCEDENCIA_OC"].apply(_get_lag).astype(int)
                        else:
                            df_chart["_LAG"] = 14

                        df_chart["DISP_CD"] = df_chart["ETA"] + df_chart["_LAG"].apply(
                            lambda d: pd.Timedelta(days=int(d))
                        )

                        # Aggregate by PO + Product
                        _agg_dict = {
                            "ETA": ("ETA", "min"),
                            "DISP_CD": ("DISP_CD", "max"),
                            "UNIDADES": ("CANTIDAD_FINAL_CORREGIDA", "sum"),
                            "MONTOMN": ("MONTOMN", "sum"),
                            "_LAG": ("_LAG", "first"),
                        }
                        if "PROCEDENCIA_OC" in df_chart.columns:
                            _agg_dict["PROCEDENCIA_OC"] = ("PROCEDENCIA_OC", "first")

                        _tl = (
                            df_chart.groupby(["PO", "NOM_PRODUCTO"])
                            .agg(**_agg_dict)
                            .reset_index()
                            .sort_values("ETA")
                        )

                        # ── KPI summary ──
                        st.markdown("### Timeline de Transitos Pendientes")
                        _k1, _k2, _k3 = st.columns(3)
                        _k1.metric("Unidades en Transito", f"{_tl['UNIDADES'].sum():,.0f}")
                        _k2.metric("Costo en Transito (CLP)", f"${human_format(_tl['MONTOMN'].sum())}")
                        _prox = _tl["ETA"].min()
                        _k3.metric("Proximo Arribo", _prox.strftime("%d-%b-%Y") if pd.notna(_prox) else "—")

                        st.caption(
                            "Cada barra muestra el periodo desde la **ETA (puerto)** "
                            "hasta la **disponibilidad estimada en CD** "
                            "(ETA + lag operativo: 14 dias importado, 7 dias nacional). "
                            "Para OCs sin carpeta, se estima ETA = Fecha Entrega + 47 dias."
                        )

                        # Limit rows for readability
                        _total_pos = len(_tl)
                        _MAX_ROWS = 40
                        if _total_pos > _MAX_ROWS:
                            _tl = _tl.head(_MAX_ROWS).copy()
                            st.caption(
                                f"Mostrando las {_MAX_ROWS} OCs mas proximas "
                                f"de {_total_pos} totales."
                            )
                        else:
                            _tl = _tl.copy()

                        # Force safe dtypes before building labels/text
                        _tl["PO"] = _tl["PO"].astype(str)
                        _tl["NOM_PRODUCTO"] = _tl["NOM_PRODUCTO"].astype(str)
                        _tl["UNIDADES"] = pd.to_numeric(_tl["UNIDADES"], errors="coerce").fillna(0)
                        _tl["MONTOMN"] = pd.to_numeric(_tl["MONTOMN"], errors="coerce").fillna(0)
                        _tl["_LAG"] = _tl["_LAG"].astype(int)

                        # Row labels
                        _tl["_LABEL"] = _tl["PO"] + " | " + _tl["NOM_PRODUCTO"].str[:35]

                        # Color by procedencia
                        if "PROCEDENCIA_OC" in _tl.columns:
                            _tl["_COLOR"] = _tl["PROCEDENCIA_OC"].apply(
                                lambda p: COLORS["status_on_track"]
                                if "NACIONAL" in str(p).upper()
                                else COLORS["primary"]
                            )
                        else:
                            _tl["_COLOR"] = COLORS["primary"]

                        # Bar text
                        _tl["_TEXT"] = _tl.apply(
                            lambda r: (
                                f"{human_format(r['UNIDADES'])} und  |  "
                                f"${human_format(r['MONTOMN'])}"
                            ),
                            axis=1,
                        )

                        # Hover text
                        _tl["_HOVER"] = _tl.apply(
                            lambda r: (
                                f"<b>{r['PO']}</b><br>"
                                f"Producto: {str(r['NOM_PRODUCTO'])[:45]}<br>"
                                f"ETA Puerto: {r['ETA'].strftime('%d-%b-%Y')}<br>"
                                f"Disp. CD: {r['DISP_CD'].strftime('%d-%b-%Y')}<br>"
                                f"Lag: {r['_LAG']} dias<br>"
                                f"Unidades: {r['UNIDADES']:,.0f}<br>"
                                f"Costo: ${r['MONTOMN']:,.0f}"
                            ),
                            axis=1,
                        )

                        _today_str = pd.Timestamp.now().strftime("%Y-%m-%d")
                        _clr_eta = COLORS["primary"]
                        _clr_cd = COLORS["tertiary_teal"]

                        fig_tl = go.Figure(layout=dorel_layout(
                            title=dict(
                                text="ETA Puerto → Disponibilidad en CD",
                                font_size=15, x=0.5,
                            ),
                            height=max(350, len(_tl) * 50 + 140),
                            xaxis=dict(
                                title="Fecha", type="date",
                                gridcolor="#ECECEC",
                            ),
                            yaxis=dict(
                                title="", type="category",
                                autorange="reversed",
                                tickfont=dict(size=10),
                            ),
                            legend=dict(
                                orientation="h", yanchor="top",
                                y=-0.08, xanchor="center", x=0.5,
                            ),
                        ))

                        # ── Trace 1: connecting lines (NaN-separated) ──
                        _x_lines, _y_lines = [], []
                        for _, row in _tl.iterrows():
                            _x_lines.extend([
                                row["ETA"].strftime("%Y-%m-%d"),
                                row["DISP_CD"].strftime("%Y-%m-%d"),
                                None,
                            ])
                            _y_lines.extend([row["_LABEL"], row["_LABEL"], None])

                        fig_tl.add_trace(go.Scatter(
                            x=_x_lines, y=_y_lines, mode="lines",
                            line=dict(width=12, color=_clr_cd),
                            showlegend=False, hoverinfo="skip",
                        ))

                        # ── Trace 2: ETA markers (diamonds) ──
                        _eta_x = _tl["ETA"].dt.strftime("%Y-%m-%d").tolist()
                        _cd_x = _tl["DISP_CD"].dt.strftime("%Y-%m-%d").tolist()
                        _labels = _tl["_LABEL"].tolist()
                        _hovers = _tl["_HOVER"].tolist()

                        fig_tl.add_trace(go.Scatter(
                            x=_eta_x, y=_labels, mode="markers",
                            marker=dict(
                                size=14, color=_clr_eta, symbol="diamond",
                                line=dict(width=1.5, color="white"),
                            ),
                            name="ETA Puerto",
                            hovertext=_hovers, hoverinfo="text",
                        ))

                        # ── Trace 3: DISP_CD markers (diamonds) ──
                        fig_tl.add_trace(go.Scatter(
                            x=_cd_x, y=_labels, mode="markers",
                            marker=dict(
                                size=14, color=_clr_cd, symbol="diamond",
                                line=dict(width=1.5, color="white"),
                            ),
                            name="Disp. CD",
                            hovertext=_hovers, hoverinfo="text",
                        ))

                        # ── Annotations per PO ──
                        for _, row in _tl.iterrows():
                            _eta_s = row["ETA"].strftime("%Y-%m-%d")
                            _disp_s = row["DISP_CD"].strftime("%Y-%m-%d")
                            _lbl = row["_LABEL"]

                            # Date label at ETA
                            fig_tl.add_annotation(
                                x=_eta_s, y=_lbl,
                                text=f"<b>Puerto</b>: {row['ETA'].strftime('%d-%b')}",
                                showarrow=False, yshift=18,
                                font=dict(size=9, color=_clr_eta),
                                bgcolor="rgba(255,255,255,0.8)",
                                borderpad=2,
                            )
                            # Date label at DISP_CD
                            fig_tl.add_annotation(
                                x=_disp_s, y=_lbl,
                                text=f"<b>CD</b>: {row['DISP_CD'].strftime('%d-%b')}",
                                showarrow=False, yshift=18,
                                font=dict(size=9, color=_clr_cd),
                                bgcolor="rgba(255,255,255,0.8)",
                                borderpad=2,
                            )
                            # Units + cost at midpoint
                            _eta_dt = row["ETA"].to_pydatetime()
                            _disp_dt = row["DISP_CD"].to_pydatetime()
                            _mid = (_eta_dt + (_disp_dt - _eta_dt) / 2).strftime("%Y-%m-%d")
                            fig_tl.add_annotation(
                                x=_mid, y=_lbl,
                                text=f"<b>{row['_TEXT']}</b>",
                                showarrow=False, yshift=-18,
                                font=dict(size=9, color="#555"),
                                bgcolor="rgba(255,255,255,0.8)",
                                borderpad=2,
                            )

                        # ── Vertical line "Hoy" ──
                        fig_tl.add_shape(
                            type="line", x0=_today_str, x1=_today_str,
                            y0=0, y1=1, yref="paper",
                            line=dict(dash="dash", color=COLORS["status_critical"], width=2),
                        )
                        fig_tl.add_annotation(
                            x=_today_str, y=1, yref="paper",
                            text="Hoy", showarrow=False,
                            font=dict(color=COLORS["status_critical"], size=11),
                            yanchor="bottom",
                        )

                        st.plotly_chart(fig_tl, use_container_width=True)
                    elif df_chart.empty:
                        st.info("No hay transitos pendientes en la seleccion actual.")

                st.success(f"Resultados Totales: {len(df):,} filas")
                st.dataframe(df.head(500))
                download_buttons(df, "comex_filtrado")

        except Exception as e:
            st.error(f"Error: {e}")
            import traceback
            st.code(traceback.format_exc())
