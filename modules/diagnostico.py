"""
Módulo temporal de diagnóstico — muestra columnas reales de las tablas Peru.
Eliminar una vez que las queries estén corregidas.
"""
import streamlit as st
import pandas as pd


TABLAS = {
    "ft_vcm":         "SELECT * FROM db_finanzas.fct.ft_vcm LIMIT 2",
    "vw_producto":    "SELECT * FROM db_dimensiones.dim.vw_producto LIMIT 2",
    "ft_compras":     "SELECT * FROM db_supply.fct.ft_compras LIMIT 2",
    "vw_in_stock":    "SELECT * FROM db_supply.hst.vw_in_stock LIMIT 2",
    "vw_in_stock_cd": "SELECT * FROM db_supply.hst.vw_in_stock_cd LIMIT 2",
}


def render_diagnostico(conn):
    st.title("🔬 Diagnóstico de Tablas — Dorel Perú")
    st.info("Módulo temporal para descubrir columnas reales de las tablas. Eliminar después de corregir queries.")

    for nombre, query in TABLAS.items():
        with st.expander(f"📋 {nombre}", expanded=True):
            try:
                df = pd.read_sql(query, conn)
                st.success(f"✅ OK — {len(df.columns)} columnas")
                st.write("**Columnas:**")
                st.code(", ".join(df.columns.tolist()))
                st.dataframe(df, use_container_width=True)
            except Exception as e:
                st.error(f"❌ Error: {e}")
