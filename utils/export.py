"""Export utilities for CSV and Excel.

PowerPoint export (python-pptx) is not available in Streamlit in Snowflake
and has been removed. CSV and Excel downloads work as before.
"""

import io
from datetime import datetime
import streamlit as st
import pandas as pd


_MAX_CSV_ROWS = 500_000
_MAX_EXCEL_ROWS = 200_000


def _df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Write DataFrame to CSV bytes using BytesIO (avoids giant intermediate string)."""
    buf = io.BytesIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue()


def download_buttons(df, prefix="data", excel_buffer=None):
    """Render CSV and Excel download buttons for a DataFrame.

    Caps large DataFrames to avoid Streamlit message-size failures:
    - CSV:   first 500k rows
    - Excel: first 200k rows
    """
    col1, col2 = st.columns(2)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    n_rows = len(df)

    with col1:
        if n_rows > _MAX_CSV_ROWS:
            df_csv = df.iloc[:_MAX_CSV_ROWS]
            csv_label = f"Descargar CSV ({_MAX_CSV_ROWS // 1_000}k / {n_rows:,} filas)"
        else:
            df_csv = df
            csv_label = "Descargar CSV"

        st.download_button(
            csv_label,
            _df_to_csv_bytes(df_csv),
            f"{prefix}_{timestamp}.csv",
            "text/csv",
            key=f"dl_csv_{prefix}",
        )
        if n_rows > _MAX_CSV_ROWS:
            st.caption(
                f"Dataset tiene {n_rows:,} filas — descarga limitada a "
                f"{_MAX_CSV_ROWS:,}. Aplica filtros para reducir."
            )

    with col2:
        excel_rows = min(n_rows, _MAX_EXCEL_ROWS)
        if n_rows > _MAX_EXCEL_ROWS:
            excel_note = f" ({_MAX_EXCEL_ROWS // 1_000}k / {n_rows:,} filas)"
        else:
            excel_note = ""

        if excel_buffer is None:
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                df.iloc[:_MAX_EXCEL_ROWS].to_excel(writer, index=False)

        # Leer todos los bytes con seek(0) para que st.download_button
        # reciba bytes crudos (más confiable que pasar el objeto BytesIO)
        excel_buffer.seek(0)
        excel_bytes = excel_buffer.read()

        st.download_button(
            f"Descargar Excel{excel_note}",
            excel_bytes,
            f"{prefix}_{timestamp}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_excel_{prefix}",
        )
