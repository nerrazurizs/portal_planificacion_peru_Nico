"""Correccion Forecast Compra (SKUs Nacionales).

Recalcula FORECAST_COMPRA para SKUs de procedencia Nacional aplicando una
logica bimensual de cobertura (revision cada 2 meses, dimensionamiento a 3
meses, reubicacion de compras ya innecesarias) y mapea el resultado al
archivo compra_proyectada para generar un CSV corregido listo para subir
al sistema de compras.
"""

from __future__ import annotations

import io

import pandas as pd
import streamlit as st


# ============================================================================
# CONSTANTS
# ============================================================================

REQUIRED_PROY_COLS = [
    "TIPO_DATO", "SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "PROCEDENCIA",
    "ID_MES", "STOCK_INICIAL_TOTAL", "FORECAST_COMPRA", "ETA", "DEMANDA_TOTAL",
]

REQUIRED_CP_COLS = [
    "id", "id_material", "costo_promedio", "fecha", "valor_inventario_mmpeso",
    "lead_time", "sigma_lt", "stock_cd", "stock_total", "demanda", "orden",
    "rop", "clase", "reposicion", "valor_compra",
]

URGENTES_NOTA = (
    "Compra urgente: requiere colocacion de orden ANTES del rango de fechas "
    "de este archivo (lead_time ya consumido)"
)


# ============================================================================
# HELPERS
# ============================================================================

def _norm_sku(series: pd.Series) -> pd.Series:
    """Normaliza codigos SKU a string (sin espacios ni '.0' de floats)."""
    s = series.astype(str).str.strip()
    return s.str.replace(r"\.0$", "", regex=True)


def _validar_columnas(df: pd.DataFrame, required: list[str], nombre: str) -> bool:
    faltantes = [c for c in required if c not in df.columns]
    if faltantes:
        st.error(
            f"El archivo **{nombre}** no tiene las columnas requeridas: "
            f"{', '.join(faltantes)}"
        )
        return False
    return True


# ============================================================================
# PASO 2 — Recalculo de FORECAST_COMPRA por SKU
# ============================================================================

def _procesar_sku(grp: pd.DataFrame) -> pd.DataFrame:
    """Recalcula FC_NUEVO mes a mes para un SKU (logica bimensual + cobertura 3m)."""
    grp = grp.sort_values("ID_MES").reset_index(drop=True)
    n = len(grp)
    demanda = grp["DEMANDA_TOTAL"].tolist()
    eta = grp["ETA"].tolist()
    fc_orig = grp["FORECAST_COMPRA"].tolist()
    stock_ini = grp["STOCK_INICIAL_TOTAL"].iloc[0]

    new_fc: list[float] = [0.0] * n
    pending_orig: dict[int, float] = {}

    stock = stock_ini
    for i in range(n):
        # 1. Stock disponible este mes = stock arrastrado + ETA comprometido
        stock_con_eta = stock + eta[i]

        dem_actual = demanda[i]
        dem_sig = demanda[i + 1] if i + 1 < n else dem_actual
        dem_sig2 = demanda[i + 2] if i + 2 < n else dem_sig

        cob2m = dem_actual + dem_sig
        cob3m = dem_actual + dem_sig + dem_sig2

        compra_aqui = 0.0

        # 2. Si stock+ETA no cubre 2 meses -> compra dimensionada a 3 meses
        if stock_con_eta < cob2m:
            compra_aqui = max(0.0, cob3m - stock_con_eta)

        new_fc[i] = round(compra_aqui, 2)
        stock_post_compra = stock_con_eta + compra_aqui

        # 3. Reubicar FC original si ya no es necesario tras la compra de este mes
        if fc_orig[i] > 0:
            if stock_post_compra >= cob2m:
                target = i + 2
                if target < n:
                    pending_orig[target] = pending_orig.get(target, 0.0) + 1

        # 4. Si aterriza una reubicacion pendiente y aun no se genero compra
        if i in pending_orig and pending_orig[i] > 0 and compra_aqui == 0.0:
            if stock_post_compra < cob3m:
                extra = max(0.0, cob3m - stock_post_compra)
                new_fc[i] = round(new_fc[i] + extra, 2)
                stock_post_compra += extra

        # 5. Avanzar stock al siguiente mes
        stock = max(0.0, stock_post_compra - dem_actual)

    grp["FC_NUEVO"] = new_fc
    return grp


# ============================================================================
# PIPELINE COMPLETO
# ============================================================================

def _ejecutar_correccion(df_proy_raw: pd.DataFrame, df_cp_raw: pd.DataFrame):
    """Ejecuta los pasos 1-4 y devuelve (cp_corregido, urgentes, modificadas, summary)."""

    # ---- Paso 1: filtrar y preparar proyeccion ----
    df = df_proy_raw.copy()
    df.columns = df.columns.str.strip()

    tipo_ok = df["TIPO_DATO"].astype(str).str.strip().str.upper() != "HISTORICO"
    proced_ok = df["PROCEDENCIA"].astype(str).str.strip().str.upper() == "NACIONAL"
    nacional = df.loc[tipo_ok & proced_ok, [
        "SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "ID_MES", "STOCK_INICIAL_TOTAL",
        "FORECAST_COMPRA", "ETA", "DEMANDA_TOTAL",
    ]].copy()

    nacional["SKU_PRODUCTO"] = _norm_sku(nacional["SKU_PRODUCTO"])
    for c in ["STOCK_INICIAL_TOTAL", "FORECAST_COMPRA", "ETA", "DEMANDA_TOTAL"]:
        nacional[c] = pd.to_numeric(nacional[c], errors="coerce").fillna(0)
    nacional["ID_MES"] = pd.to_numeric(nacional["ID_MES"], errors="coerce").astype("Int64")
    nacional = nacional.sort_values(["SKU_PRODUCTO", "ID_MES"]).reset_index(drop=True)

    # ---- Paso 2: recalcular FORECAST_COMPRA por SKU ----
    if nacional.empty:
        result = nacional.copy()
        result["FC_NUEVO"] = pd.Series(dtype="float64")
    else:
        result = pd.concat(
            [_procesar_sku(g) for _, g in nacional.groupby("SKU_PRODUCTO")]
        ).reset_index(drop=True)

    nacional_skus = set(result["SKU_PRODUCTO"].unique())
    sku_nom_map = (
        result.drop_duplicates("SKU_PRODUCTO")
        .set_index("SKU_PRODUCTO")["SKU_NOM_PRODUCTO"]
        .to_dict()
    )

    # ---- Paso 3: mapear FC_NUEVO a compra_proyectada ----
    cp = df_cp_raw.copy()
    cp.columns = cp.columns.str.strip()
    cp = cp.reset_index(drop=True)

    cp["_fecha_dt"] = pd.to_datetime(cp["fecha"], errors="coerce")
    cp["_lead_time"] = pd.to_numeric(cp["lead_time"], errors="coerce").fillna(0)
    cp["_fecha_llegada"] = cp["_fecha_dt"] + pd.to_timedelta(cp["_lead_time"], unit="D")
    cp["id_mes_llegada"] = (
        cp["_fecha_llegada"].dt.year * 100 + cp["_fecha_llegada"].dt.month
    ).astype("Int64")

    cp["_id_material_norm"] = _norm_sku(cp["id_material"])
    cp["_orden_original"] = pd.to_numeric(cp["orden"], errors="coerce").fillna(0)
    cp["_costo_promedio"] = pd.to_numeric(cp["costo_promedio"], errors="coerce").fillna(0)

    is_nacional_row = cp["_id_material_norm"].isin(nacional_skus)

    lookup = result[["SKU_PRODUCTO", "ID_MES", "FC_NUEVO"]].rename(
        columns={"SKU_PRODUCTO": "_id_material_norm", "ID_MES": "id_mes_llegada"}
    )
    cp = cp.merge(lookup, on=["_id_material_norm", "id_mes_llegada"], how="left")

    cp["orden_nuevo"] = cp["_orden_original"]
    mask_match = is_nacional_row & cp["FC_NUEVO"].notna()
    cp.loc[mask_match, "orden_nuevo"] = cp.loc[mask_match, "FC_NUEVO"]

    cp_out = df_cp_raw.copy()
    cp_out.columns = cp_out.columns.str.strip()
    cp_out.loc[is_nacional_row.values, "orden"] = cp.loc[is_nacional_row, "orden_nuevo"].values
    recalc_vals = (cp.loc[is_nacional_row, "orden_nuevo"] * cp.loc[is_nacional_row, "_costo_promedio"]).round(2)
    cp_out.loc[is_nacional_row.values, "valor_compra"] = recalc_vals.values
    cp_out = cp_out[REQUIRED_CP_COLS]

    # ---- Filas modificadas (para preview) ----
    delta_mask = is_nacional_row & (cp["orden_nuevo"] != cp["_orden_original"])
    df_modificadas = pd.DataFrame({
        "id_material": cp.loc[delta_mask, "id_material"],
        "sku_nombre": cp.loc[delta_mask, "_id_material_norm"].map(sku_nom_map),
        "fecha": df_cp_raw.reset_index(drop=True).loc[delta_mask, "fecha"],
        "orden_original": cp.loc[delta_mask, "_orden_original"],
        "orden_nuevo": cp.loc[delta_mask, "orden_nuevo"],
    }).reset_index(drop=True)
    if not df_modificadas.empty:
        df_modificadas["delta"] = df_modificadas["orden_nuevo"] - df_modificadas["orden_original"]

    # ---- SKUs nacionales sin filas en compra_proyectada ----
    cp_skus = set(cp["_id_material_norm"].unique())
    skus_omitidos = sorted(nacional_skus - cp_skus)

    # ---- Paso 4: ordenes urgentes fuera de rango ----
    min_mes_llegada = (
        cp.loc[is_nacional_row]
        .groupby("_id_material_norm")["id_mes_llegada"]
        .min()
    )

    urgentes_rows = []
    for sku, grp in result.groupby("SKU_PRODUCTO"):
        if sku not in min_mes_llegada.index:
            continue
        min_mes = min_mes_llegada[sku]
        if pd.isna(min_mes):
            continue
        sub = grp[(grp["FC_NUEVO"] > 0) & (grp["ID_MES"] < min_mes)]
        for _, r in sub.iterrows():
            urgentes_rows.append({
                "id_material": sku,
                "sku_nombre": sku_nom_map.get(sku, ""),
                "id_mes_llegada_requerido": int(r["ID_MES"]),
                "cantidad_orden": r["FC_NUEVO"],
                "nota": URGENTES_NOTA,
            })

    df_urgentes = pd.DataFrame(
        urgentes_rows,
        columns=["id_material", "sku_nombre", "id_mes_llegada_requerido", "cantidad_orden", "nota"],
    )

    # ---- Resumen ----
    valor_antes = (cp.loc[is_nacional_row, "_orden_original"] * cp.loc[is_nacional_row, "_costo_promedio"]).sum()
    valor_despues = cp_out.loc[is_nacional_row.values, "valor_compra"].sum()

    summary = {
        "n_skus_nacional": len(nacional_skus),
        "n_filas_modificadas": len(df_modificadas),
        "valor_total_antes": valor_antes,
        "valor_total_despues": valor_despues,
        "skus_omitidos": skus_omitidos,
    }

    return cp_out, df_urgentes, df_modificadas, summary


# ============================================================================
# RENDER
# ============================================================================

def render_correccion_forecast_compra(conn):  # noqa: ARG001
    from utils.ui_components import page_header

    st.html(
        page_header(
            "Correccion Forecast Compra (Nacional)",
            "Recalcula FORECAST_COMPRA para SKUs nacionales (cobertura bimensual / 3 meses)",
        )
    )

    col1, col2 = st.columns(2)
    with col1:
        proy_file = st.file_uploader(
            "Archivo Proyeccion Stock (.xlsx, hoja 'DATA')",
            type=["xlsx"],
            key="cfc_proy_file",
        )
    with col2:
        cp_file = st.file_uploader(
            "Archivo Compra Proyectada (.csv)",
            type=["csv"],
            key="cfc_cp_file",
        )

    archivos_listos = proy_file is not None and cp_file is not None
    procesar = st.button("Procesar", type="primary", disabled=not archivos_listos)

    if not archivos_listos:
        st.info("Carga ambos archivos para habilitar el procesamiento.")

    if procesar:
        try:
            df_proy_raw = pd.read_excel(proy_file, sheet_name="DATA")
        except Exception as exc:
            st.error(f"Error al leer la hoja 'DATA' de **{proy_file.name}**: {exc}")
            st.stop()

        try:
            df_cp_raw = pd.read_csv(cp_file)
        except Exception as exc:
            st.error(f"Error al leer **{cp_file.name}**: {exc}")
            st.stop()

        df_proy_raw.columns = df_proy_raw.columns.str.strip()
        df_cp_raw.columns = df_cp_raw.columns.str.strip()

        ok_proy = _validar_columnas(df_proy_raw, REQUIRED_PROY_COLS, "proyeccion_stock (hoja DATA)")
        ok_cp = _validar_columnas(df_cp_raw, REQUIRED_CP_COLS, "compra_proyectada")

        if ok_proy and ok_cp:
            with st.spinner("Procesando correccion de forecast..."):
                cp_out, df_urgentes, df_modificadas, summary = _ejecutar_correccion(df_proy_raw, df_cp_raw)

            st.session_state["_cfc_resultado"] = {
                "cp_out": cp_out,
                "df_urgentes": df_urgentes,
                "df_modificadas": df_modificadas,
                "summary": summary,
            }

    # ── Mostrar resultado (persiste entre re-runs vía session_state) ──────────
    resultado = st.session_state.get("_cfc_resultado")
    if not resultado:
        return

    cp_out = resultado["cp_out"]
    df_urgentes = resultado["df_urgentes"]
    df_modificadas = resultado["df_modificadas"]
    summary = resultado["summary"]

    if summary["skus_omitidos"]:
        st.warning(
            f"{len(summary['skus_omitidos'])} SKU(s) nacional(es) de la proyeccion no se "
            f"encontraron en compra_proyectada y fueron omitidos: "
            f"{', '.join(summary['skus_omitidos'][:20])}"
            + (" ..." if len(summary["skus_omitidos"]) > 20 else "")
        )

    st.markdown("#### Resumen")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("SKUs Nacionales Procesados", f"{summary['n_skus_nacional']:,}")
    m2.metric("Filas Modificadas", f"{summary['n_filas_modificadas']:,}")
    m3.metric("Valor Compra Antes", f"{summary['valor_total_antes']:,.2f}")
    m4.metric(
        "Valor Compra Despues",
        f"{summary['valor_total_despues']:,.2f}",
        delta=f"{summary['valor_total_despues'] - summary['valor_total_antes']:,.2f}",
    )

    st.markdown("#### Preview de Filas Modificadas (primeras 50)")
    if df_modificadas.empty:
        st.info("No se modificaron filas de compra_proyectada.")
    else:
        st.dataframe(df_modificadas.head(50), use_container_width=True, hide_index=True)

    if not df_urgentes.empty:
        st.warning(f"{len(df_urgentes):,} orden(es) urgente(s) fuera de rango (ver tabla abajo).")
        st.dataframe(df_urgentes, use_container_width=True, hide_index=True)

    st.markdown("#### Descargas")
    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button(
            "Descargar compra_proyectada_corregida.csv",
            cp_out.to_csv(index=False).encode("utf-8-sig"),
            "compra_proyectada_corregida.csv",
            "text/csv",
            key="dl_cfc_corregida",
        )
    with dl2:
        st.download_button(
            "Descargar ordenes_urgentes_fuera_de_rango.csv",
            df_urgentes.to_csv(index=False).encode("utf-8-sig"),
            "ordenes_urgentes_fuera_de_rango.csv",
            "text/csv",
            key="dl_cfc_urgentes",
        )
