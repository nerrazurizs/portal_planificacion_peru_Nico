"""Carga Perfil — Convierte el archivo Perfil (matriz SKU x Sucursal) al formato DJPE."""

import io
import pandas as pd
import streamlit as st


# ============================================================================
# PARSING
# ============================================================================

def _parse_perfil(uploaded_file) -> tuple[pd.DataFrame, str]:
    """Parse the Perfil Excel file and return (dataframe, error_message).

    Expected layout:
      Row 0 : category headers (ignored)
      Row 1 : centro_costo names  (ignored)
      Row 2 : id_sucursal values  starting at column 9
      Row 3 : column headers  (PROCEDENCIA, GRUPO, ..., id_material, MIX, then 'x' markers)
      Row 4+: data rows
    """
    try:
        raw = pd.read_excel(uploaded_file, sheet_name="Perfil", header=None)
    except Exception as exc:
        return None, f"No se pudo leer la hoja 'Perfil': {exc}"

    if raw.shape[0] < 5 or raw.shape[1] < 10:
        return None, "El archivo no tiene la estructura esperada (pocas filas/columnas)."

    # Row index 2 → sucursal IDs; columns 9 onward
    suc_row = raw.iloc[2]
    suc_cols: dict[int, str] = {}
    for col_idx, val in enumerate(suc_row):
        if col_idx < 9:
            continue
        if pd.notna(val) and str(val).strip() not in ("", "id_sucursal"):
            suc_cols[col_idx] = str(val).strip().zfill(4)

    if not suc_cols:
        return None, "No se encontraron columnas de sucursal (fila 3 del archivo)."

    # Data rows start at index 4
    data = raw.iloc[4:].copy()
    # Column 7 = id_material; filter out rows without it
    data = data[data.iloc[:, 7].notna()].copy()
    data.columns = range(data.shape[1])

    # Build output
    records = []
    for _, row in data.iterrows():
        id_material = str(row[7]).strip()
        if not id_material:
            continue
        for col_idx, suc_id in suc_cols.items():
            if col_idx >= len(row):
                continue
            val = row[col_idx]
            if pd.isna(val) or val == 0:
                continue
            try:
                min_inv = int(val)
            except (ValueError, TypeError):
                continue
            if min_inv <= 0:
                continue
            records.append({
                "id_material": id_material,
                "max_repo": -1,
                "cd_origen": 1190,
                "min_inv_requerido": min_inv,
                "id_sucursal": suc_id,
                "consolida_compra": None,
            })

    if not records:
        return None, "No se encontraron registros con perfil > 0."

    df = pd.DataFrame(records)
    return df, ""


# ============================================================================
# RENDER
# ============================================================================

def render_carga_perfil(_conn=None):
    st.markdown("## Carga Perfil")
    st.caption(
        "Sube el archivo **Perfil** (hoja 'Perfil') para generar la planilla "
        "**DJPE - Planilla carga perfil EMPRESA**."
    )

    uploaded = st.file_uploader(
        "Archivo Perfil (.xlsx)",
        type=["xlsx", "xls"],
        key="cp_upload",
    )

    if uploaded is None:
        st.info("Sube el archivo Perfil para continuar.")
        return

    with st.spinner("Procesando..."):
        df, err = _parse_perfil(uploaded)

    if err:
        st.error(f"Error al procesar el archivo: {err}")
        return

    st.success(f"Archivo procesado: **{len(df):,}** registros generados.")

    # Preview
    st.dataframe(df.head(100), use_container_width=True)

    # Download
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sheet1")
    buf.seek(0)

    from datetime import date
    today = date.today().strftime("%Y%m%d")
    out_name = f"DJPE - Planilla carga perfil EMPRESA {today}.xlsx"

    st.download_button(
        label="Descargar DJPE",
        data=buf,
        file_name=out_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
