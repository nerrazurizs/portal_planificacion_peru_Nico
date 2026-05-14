"""Export utilities for CSV and Excel.

PowerPoint export (python-pptx) is not available in Streamlit in Snowflake
and has been removed. CSV and Excel downloads work as before.
"""

import io
from datetime import datetime
import streamlit as st
import pandas as pd


def download_buttons(df, prefix="data", excel_buffer=None):
    """Render CSV and Excel download buttons for a DataFrame."""
    col1, col2 = st.columns(2)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    with col1:
        st.download_button(
            "Descargar CSV",
            df.to_csv(index=False).encode("utf-8-sig"),
            f"{prefix}_{timestamp}.csv",
            "text/csv",
        )

    with col2:
        if len(df) < 1_000_000:
            if excel_buffer is None:
                excel_buffer = io.BytesIO()
                with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                    df.to_excel(writer, index=False)

            st.download_button(
                "Descargar Excel",
                excel_buffer,
                f"{prefix}_{timestamp}.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        else:
            st.warning("Excel no disponible (>1M filas). Use CSV.")
