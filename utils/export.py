"""Export utilities for CSV, Excel, and PowerPoint."""

import io
from datetime import datetime
import streamlit as st
import pandas as pd
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE


# ── Dorel corporate colors ─────────────────────────────────────────────────
_C_PRIMARY = RGBColor(0x06, 0x5E, 0x8B)
_C_ACCENT = RGBColor(0x23, 0xCE, 0xD3)
_C_WHITE = RGBColor(0xFF, 0xFF, 0xFF)
_C_LIGHT_GRAY = RGBColor(0x94, 0xA3, 0xB8)
_C_DARK_TEXT = RGBColor(0x1E, 0x29, 0x3B)


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


def _fig_to_png_bytes(fig):
    """Convert a matplotlib or Plotly figure to PNG bytes."""
    img_stream = io.BytesIO()
    if hasattr(fig, "savefig"):
        # matplotlib Figure
        fig.savefig(img_stream, format="png", bbox_inches="tight", dpi=150)
    elif hasattr(fig, "to_image"):
        # Plotly Figure — requires kaleido
        img_stream.write(fig.to_image(format="png", width=1200, height=700, scale=2))
    else:
        raise TypeError(f"Unsupported figure type: {type(fig)}")
    img_stream.seek(0)
    return img_stream


def _add_title_bar(slide, prs, text):
    """Add a Dorel-styled title bar at the top of a slide."""
    # Blue background bar
    bar = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(0), Inches(0), prs.slide_width, Inches(0.7),
    )
    bar.fill.solid()
    bar.fill.fore_color.rgb = _C_PRIMARY
    bar.line.fill.background()

    # Accent teal line at bottom of bar
    accent = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(0), Inches(0.7), prs.slide_width, Inches(0.04),
    )
    accent.fill.solid()
    accent.fill.fore_color.rgb = _C_ACCENT
    accent.line.fill.background()

    # Title text
    txBox = slide.shapes.add_textbox(Inches(0.5), Inches(0.12), Inches(12), Inches(0.5))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text
    p.font.size = Pt(18)
    p.font.bold = True
    p.font.color.rgb = _C_WHITE


def generate_ppt(figures_dict, title="Reporte Dorel"):
    """Generate a PowerPoint presentation from a dict of {slide_title: figure}.

    Supports both matplotlib and Plotly figures (auto-detected).
    Returns a BytesIO buffer with the .pptx content.

    Generates widescreen 16:9 slides with Dorel corporate styling.
    """
    prs = Presentation()
    prs.slide_width = Inches(13.33)
    prs.slide_height = Inches(7.5)

    blank_layout = prs.slide_layouts[6]  # Blank

    # ── Slide 1: Title slide ──
    slide = prs.slides.add_slide(blank_layout)

    # Large blue background (60% of slide height)
    bg = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(0), Inches(0), prs.slide_width, Inches(4.5),
    )
    bg.fill.solid()
    bg.fill.fore_color.rgb = _C_PRIMARY
    bg.line.fill.background()

    # Accent teal line
    accent = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(0), Inches(4.5), prs.slide_width, Inches(0.06),
    )
    accent.fill.solid()
    accent.fill.fore_color.rgb = _C_ACCENT
    accent.line.fill.background()

    # Title text (centered, large)
    txBox = slide.shapes.add_textbox(Inches(1), Inches(1.5), Inches(11.33), Inches(1.5))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = title
    p.font.size = Pt(32)
    p.font.bold = True
    p.font.color.rgb = _C_WHITE
    p.alignment = PP_ALIGN.CENTER

    # Subtitle with date
    txSub = slide.shapes.add_textbox(Inches(1), Inches(5.2), Inches(11.33), Inches(0.8))
    tf_sub = txSub.text_frame
    tf_sub.word_wrap = True
    p_sub = tf_sub.paragraphs[0]
    p_sub.text = f"Generado el {datetime.now().strftime('%d de %B %Y')}"
    p_sub.font.size = Pt(14)
    p_sub.font.color.rgb = _C_LIGHT_GRAY
    p_sub.alignment = PP_ALIGN.CENTER

    # Footer
    txFoot = slide.shapes.add_textbox(Inches(1), Inches(6.5), Inches(11.33), Inches(0.5))
    tf_foot = txFoot.text_frame
    p_foot = tf_foot.paragraphs[0]
    p_foot.text = "Portal de Planificacion Dorel Chile"
    p_foot.font.size = Pt(10)
    p_foot.font.color.rgb = _C_LIGHT_GRAY
    p_foot.alignment = PP_ALIGN.CENTER

    # ── Content slides ──
    for name, fig in figures_dict.items():
        slide = prs.slides.add_slide(blank_layout)

        # Title bar
        _add_title_bar(slide, prs, name)

        # Figure as image (full width for 16:9)
        img_stream = _fig_to_png_bytes(fig)
        slide.shapes.add_picture(
            img_stream,
            Inches(0.3), Inches(0.9),
            width=Inches(12.7),
        )

    buffer = io.BytesIO()
    prs.save(buffer)
    buffer.seek(0)
    return buffer
