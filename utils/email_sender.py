"""Gmail / Outlook SMTP email sender for Dorel Supply Chain Portal.

Uses Python's built-in smtplib — no additional dependencies required.
Charts are rendered with matplotlib (Agg backend) and embedded as base64 PNG.

Credentials are read from .env:
    OUTLOOK_EMAIL      Sender email address
    OUTLOOK_PASSWORD   App password (Gmail or Microsoft)
    SMTP_SERVER        Defaults to smtp.gmail.com
    SMTP_PORT          Defaults to 587
"""

from __future__ import annotations

import base64
import io
import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import numpy as np
import pandas as pd

# ── Defaults ─────────────────────────────────────────────────────────────────
_DEFAULT_SERVER = "smtp.gmail.com"
_DEFAULT_PORT = 587

# ── Dorel brand colors ────────────────────────────────────────────────────────
_C_PRIMARY  = "#065E8B"
_C_CRITICAL = "#ef4444"
_C_WARNING  = "#f59e0b"
_C_OK       = "#10b981"
_C_PURPLE   = "#9C27B0"
_C_BG       = "#f8fafc"
_C_BORDER   = "#e2e8f0"
_C_TEXT     = "#1e293b"
_C_SUBTEXT  = "#64748b"
_C_AA       = "#6366f1"    # indigo — año anterior

# ── Health matrix tiers & gradient (replicados de stock_critico.py) ──────────
_MOI_TIERS = ["0-3m", "3-6m", "6-8m", "8-12m", "12-24m", ">=24m"]
_ANT_TIERS = ["0-3m", "3-6m", "6-8m", "8-12m", "12-24m", ">=24m"]
_HEALTH_GRADIENT = [
    "#43A047", "#66BB6A", "#A5D6A7", "#C8E6C9",
    "#FFF176", "#F9A825", "#FB8C00", "#F4511E",
    "#E53935", "#C62828", "#B71C1C",
]


# ============================================================================
# SMTP CONFIG
# ============================================================================

def get_smtp_config(session_overrides: Optional[dict] = None) -> dict:
    """Return SMTP config from .env, with optional in-session overrides."""
    cfg = {
        "email":    os.getenv("OUTLOOK_EMAIL", ""),
        "password": os.getenv("OUTLOOK_PASSWORD", ""),
        "server":   os.getenv("SMTP_SERVER", _DEFAULT_SERVER),
        "port":     int(os.getenv("SMTP_PORT", _DEFAULT_PORT)),
    }
    if session_overrides:
        for k, v in session_overrides.items():
            if v:
                cfg[k] = v
    return cfg


# ============================================================================
# EMAIL SENDING
# ============================================================================

def send_alert_email(
    subject: str,
    html_body: str,
    to_recipients: list[str],
    cfg: dict,
    attachments: Optional[list[tuple[str, bytes]]] = None,
) -> tuple[bool, str]:
    """Send HTML email via STARTTLS on port 587."""
    if not cfg.get("email") or not cfg.get("password"):
        return False, "Credenciales SMTP no configuradas (email o contraseña vacíos)."
    if not to_recipients:
        return False, "No hay destinatarios configurados."

    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = cfg["email"]
        msg["To"] = ", ".join(to_recipients)

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText("Ver este correo en un cliente compatible con HTML.", "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)

        for filename, content in (attachments or []):
            part = MIMEBase("application", "octet-stream")
            part.set_payload(content)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            msg.attach(part)

        with smtplib.SMTP(cfg["server"], cfg["port"]) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(cfg["email"], cfg["password"])
            smtp.sendmail(cfg["email"], to_recipients, msg.as_string())

        return True, f"Email enviado correctamente a {len(to_recipients)} destinatario(s)."

    except smtplib.SMTPAuthenticationError:
        return False, (
            "Error de autenticación SMTP. Verifica email y contraseña.\n"
            "Si usas Gmail, genera una App Password en myaccount.google.com/apppasswords."
        )
    except smtplib.SMTPConnectError as e:
        return False, f"No se pudo conectar al servidor SMTP ({cfg['server']}:{cfg['port']}): {e}"
    except smtplib.SMTPException as e:
        return False, f"Error SMTP: {e}"
    except Exception as e:
        return False, f"Error inesperado al enviar email: {e}"


# ============================================================================
# CHART HELPERS (matplotlib → base64 PNG)
# ============================================================================

def _fig_to_base64(fig) -> str:
    """Convert a matplotlib Figure to a base64-encoded PNG string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=130, facecolor=fig.get_facecolor())
    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode("utf-8")
    buf.close()
    return encoded


def _embed_img(b64: str, width: str = "100%", alt: str = "") -> str:
    return (
        f'<img src="data:image/png;base64,{b64}" '
        f'alt="{alt}" style="width:{width};max-width:860px;display:block;margin:8px auto 0;" />'
    )


def _make_bar_chart(
    categories: list[str],
    series: dict[str, list[float]],
    title: str,
    ylabel: str = "CLP",
    colors: Optional[list[str]] = None,
    figsize: tuple = (10, 4),
) -> str:
    """Render a grouped bar chart and return base64 PNG."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mticker

        fig, ax = plt.subplots(figsize=figsize, facecolor="#ffffff")
        ax.set_facecolor("#f8fafc")
        ax.grid(axis="y", color="#e2e8f0", linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)

        n_groups = len(categories)
        n_series = len(series)
        bar_w = 0.7 / max(n_series, 1)
        offsets = np.linspace(-(n_series - 1) * bar_w / 2, (n_series - 1) * bar_w / 2, n_series)

        default_colors = [_C_PRIMARY, _C_AA, _C_WARNING, _C_OK, _C_PURPLE]
        clist = colors or default_colors

        for idx, (label, vals) in enumerate(series.items()):
            xs = np.arange(n_groups) + offsets[idx]
            bars = ax.bar(xs, vals, width=bar_w * 0.92, label=label,
                          color=clist[idx % len(clist)], zorder=3, alpha=0.88)
            for bar, v in zip(bars, vals):
                if abs(v) > 0:
                    ax.text(
                        bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + max(abs(x) for x in vals) * 0.015,
                        _fmt_m(v), ha="center", va="bottom",
                        fontsize=7.5, color=_C_TEXT, fontweight="bold",
                    )

        ax.set_xticks(np.arange(n_groups))
        ax.set_xticklabels(categories, fontsize=9.5, color=_C_TEXT)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x/1e6:,.0f}".replace(",", ".")))
        ax.tick_params(axis="y", labelsize=8.5, colors=_C_SUBTEXT)
        ax.set_title(title, fontsize=11, color=_C_PRIMARY, fontweight="bold", pad=10)
        ax.set_ylabel(ylabel, fontsize=8.5, color=_C_SUBTEXT)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color(_C_BORDER)
        if n_series > 1:
            ax.legend(fontsize=8.5, framealpha=0.7, loc="upper right")

        plt.tight_layout()
        b64 = _fig_to_base64(fig)
        plt.close(fig)
        return b64
    except Exception:
        return ""


def _make_line_chart(
    dates: list,
    series: dict[str, list[float]],
    title: str,
    ylabel: str = "%",
    colors: Optional[list[str]] = None,
    figsize: tuple = (10, 3.5),
    y_format: str = "pct",
) -> str:
    """Render a line chart and return base64 PNG.

    y_format: 'pct' (0-100 → 'XX%') or 'raw'.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mticker
        import matplotlib.dates as mdates

        fig, ax = plt.subplots(figsize=figsize, facecolor="#ffffff")
        ax.set_facecolor("#f8fafc")
        ax.grid(axis="y", color="#e2e8f0", linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)

        default_colors = [_C_PRIMARY, _C_WARNING, _C_OK, _C_CRITICAL, _C_PURPLE]
        clist = colors or default_colors

        for idx, (label, vals) in enumerate(series.items()):
            ax.plot(dates, vals, label=label,
                    color=clist[idx % len(clist)], linewidth=2.5,
                    marker="o", markersize=4, zorder=3)
            # Annotate last point
            if vals:
                last_val = vals[-1]
                fmt_val = f"{last_val:.1f}%" if y_format == "pct" else f"{last_val:.1f}"
                ax.annotate(
                    fmt_val, (dates[-1], last_val),
                    textcoords="offset points", xytext=(8, 4),
                    fontsize=8.5, fontweight="bold",
                    color=clist[idx % len(clist)],
                )

        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=12))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right", fontsize=8.5)

        if y_format == "pct":
            ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}%"))
            ax.set_ylim(bottom=max(0, min(min(v) for v in series.values()) - 5),
                        top=min(105, max(max(v) for v in series.values()) + 5))
        ax.tick_params(axis="y", labelsize=8.5, colors=_C_SUBTEXT)

        ax.set_title(title, fontsize=11, color=_C_PRIMARY, fontweight="bold", pad=10)
        ax.set_ylabel(ylabel, fontsize=8.5, color=_C_SUBTEXT)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color(_C_BORDER)
        if len(series) > 1:
            ax.legend(fontsize=8.5, framealpha=0.7, loc="lower left")

        plt.tight_layout()
        b64 = _fig_to_base64(fig)
        plt.close(fig)
        return b64
    except Exception:
        return ""


def _fmt_m(val: float) -> str:
    """Chilean CLP format: millions with dots as thousands separator.

    $9,616,600,000 → $9.617  (millones)
    $437,500,000   → $438
    $2,256,200     → $2.256
    $366,900       → $367
    """
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "$0"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        mm = round(v / 1_000_000)
        return f"${sign}{mm:,}".replace(",", ".")
    if v >= 1_000:
        return f"${sign}{round(v):,}".replace(",", ".")
    if v >= 1:
        return f"${sign}{v:,.0f}"
    return "$0"


def _format_clp(val) -> str:
    return _fmt_m(val)


# ============================================================================
# HTML PRIMITIVES
# ============================================================================

def _header_html(title: str, subtitle: str = "") -> str:
    sub = (
        f'<p style="margin:6px 0 0 0;color:rgba(255,255,255,0.78);font-size:13px;">{subtitle}</p>'
        if subtitle else ""
    )
    return (
        f'<div style="background:{_C_PRIMARY};padding:24px 32px;border-radius:12px 12px 0 0;">'
        f'<h1 style="margin:0;color:#fff;font-size:20px;font-family:Arial,sans-serif;">{title}</h1>'
        f"{sub}</div>"
    )


def _section_title_html(title: str, color: str = _C_PRIMARY) -> str:
    return (
        f'<h2 style="font-family:Arial,sans-serif;font-size:15px;color:{color};'
        f'border-bottom:2px solid {color};padding-bottom:6px;margin:28px 0 14px 0;">'
        f"{title}</h2>"
    )


def _kpi_card_html(label: str, value: str, color: str, sub: str = "") -> str:
    """Single KPI card with optional subtitle."""
    sub_html = (
        f'<div style="font-size:10px;color:{_C_SUBTEXT};margin-top:3px;">{sub}</div>'
        if sub else ""
    )
    return (
        f'<td style="padding:12px 10px;text-align:center;'
        f'border-right:1px solid {_C_BORDER};min-width:110px;vertical-align:top;">'
        f'<div style="font-size:21px;font-weight:bold;color:{color};'
        f'font-family:Arial,sans-serif;">{value}</div>'
        f'<div style="font-size:10px;color:{_C_SUBTEXT};text-transform:uppercase;'
        f'margin-top:3px;">{label}</div>{sub_html}</td>'
    )


def _kpi_row_html(cards: list[str]) -> str:
    """Wrap pre-built kpi card TDs in a table row."""
    return (
        f'<table style="width:100%;border-collapse:collapse;'
        f'background:{_C_BG};border-radius:8px;margin-bottom:16px;">'
        f'<tr>{"".join(cards)}</tr></table>'
    )


def _variance_badge(real: float, ref: float, ref_label: str = "Bdgt") -> str:
    """Return a small HTML badge showing % variance vs reference."""
    if ref == 0:
        return ""
    pct = (real - ref) / abs(ref) * 100
    color = _C_OK if pct >= 0 else _C_CRITICAL
    sign = "+" if pct >= 0 else ""
    return (
        f'<span style="font-size:10px;color:{color};'
        f'background:{color}22;border-radius:4px;padding:1px 4px;margin-left:3px;">'
        f'{sign}{pct:.1f}%</span>'
    )


def _kpi_vs_ref_card(
    label: str, real: float, budget: float, aa: float, icon: str = "",
) -> str:
    """KPI card with Budget and AA variance badges. Value in CLP."""
    vs_b = _variance_badge(real, budget, "Bdgt") if budget else ""
    vs_aa = _variance_badge(real, aa, "AA") if aa else ""
    return (
        f'<td style="padding:14px 12px;text-align:center;'
        f'border-right:1px solid {_C_BORDER};vertical-align:top;">'
        f'<div style="font-size:10px;color:{_C_SUBTEXT};text-transform:uppercase;'
        f'margin-bottom:4px;font-family:Arial,sans-serif;">{icon} {label}</div>'
        f'<div style="font-size:22px;font-weight:bold;color:{_C_PRIMARY};'
        f'font-family:Arial,sans-serif;">{_format_clp(real)}</div>'
        f'<div style="margin-top:5px;">{vs_b}{vs_aa}</div>'
        f'<div style="font-size:9px;color:{_C_SUBTEXT};margin-top:4px;">'
        f'Bdgt: {_format_clp(budget)} &nbsp;|&nbsp; AA: {_format_clp(aa)}</div></td>'
    )


def _df_to_html_table(df: pd.DataFrame, max_rows: int = 25) -> str:
    """Convert DataFrame to a styled HTML table."""
    if df is None or df.empty:
        return f'<p style="color:{_C_SUBTEXT};font-style:italic;font-family:Arial,sans-serif;">Sin datos.</p>'

    display = df.head(max_rows)
    header_cells = "".join(
        f'<th style="padding:7px 10px;background:{_C_PRIMARY};color:#fff;'
        f'font-size:10px;text-align:left;white-space:nowrap;font-family:Arial,sans-serif;">{col}</th>'
        for col in display.columns
    )
    rows_html = ""
    for i, (_, row) in enumerate(display.iterrows()):
        bg = _C_BG if i % 2 == 0 else "#ffffff"
        cells = "".join(
            f'<td style="padding:5px 10px;font-size:10px;color:{_C_TEXT};'
            f'border-bottom:1px solid {_C_BORDER};font-family:Arial,sans-serif;">{val}</td>'
            for val in row.values
        )
        rows_html += f'<tr style="background:{bg};">{cells}</tr>'

    extra = ""
    if len(df) > max_rows:
        extra = (
            f'<p style="font-size:10px;color:{_C_SUBTEXT};margin-top:4px;font-family:Arial,sans-serif;">'
            f"... y {len(df) - max_rows:,} filas más. Ver adjunto Excel para detalle completo.</p>"
        )
    return (
        f'<div style="overflow-x:auto;">'
        f'<table style="width:100%;border-collapse:collapse;">'
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{rows_html}</tbody></table></div>{extra}"
    )


# ============================================================================
# HEALTH MATRIX 6×6 (replica exacta de modules/stock_critico.py)
# ============================================================================

def _health_color(moi_tier: str, ant_tier: str) -> str:
    """Color based on combined risk score (sum of tier indices, 0-10)."""
    try:
        idx_m = _MOI_TIERS.index(moi_tier)
        idx_a = _ANT_TIERS.index(ant_tier)
    except ValueError:
        return "#ccc"
    score = min(idx_m + idx_a, 10)
    return _HEALTH_GRADIENT[score]


def _health_matrix_html(matrix_data: pd.DataFrame) -> str:
    """Build a colored 6×6 HTML matrix from pre-built matrix DataFrame.

    matrix_data must have columns: TIER_MOI, TIER_ANTIGUEDAD, N_SKUS, STOCK_COSTO.
    """
    if matrix_data is None or matrix_data.empty:
        return ""

    lookup: dict = {}
    for _, row in matrix_data.iterrows():
        key = (str(row.get("TIER_MOI", "")), str(row.get("TIER_ANTIGUEDAD", "")))
        lookup[key] = (int(row.get("N_SKUS", 0)), float(row.get("STOCK_COSTO", 0)))

    # White text on dark cells
    _dark = {"#E53935", "#C62828", "#B71C1C", "#F4511E"}

    header = (
        f'<th style="padding:6px;background:{_C_PRIMARY};color:#fff;font-size:9px;'
        f'text-align:center;border:1px solid {_C_BORDER};white-space:nowrap;">MOI \\ Antig.</th>'
        + "".join(
            f'<th style="padding:5px 6px;background:{_C_PRIMARY};color:#fff;font-size:9px;'
            f'white-space:nowrap;border:1px solid {_C_BORDER};text-align:center;">{t}</th>'
            for t in _ANT_TIERS
        )
    )
    body = ""
    for moi_t in _MOI_TIERS:
        body += (
            f'<tr><td style="padding:5px 8px;background:{_C_BG};font-size:9px;'
            f'font-weight:bold;border:1px solid {_C_BORDER};white-space:nowrap;">{moi_t}</td>'
        )
        for ant_t in _ANT_TIERS:
            n, val = lookup.get((moi_t, ant_t), (0, 0.0))
            bg = _health_color(moi_t, ant_t)
            txt_color = "white" if bg in _dark else "#333"
            if n > 0:
                cell = (
                    f'<span style="font-weight:bold;">{n}</span> SKUs<br/>'
                    f'<span style="font-size:8px;">{_format_clp(val)}</span>'
                )
            else:
                cell = '<span style="opacity:0.4;">—</span>'
            body += (
                f'<td style="padding:4px 6px;background:{bg};text-align:center;'
                f'font-size:9px;color:{txt_color};border:1px solid {_C_BORDER};">{cell}</td>'
            )
        body += "</tr>"

    return (
        f'<div style="overflow-x:auto;margin-bottom:10px;">'
        f'<table style="border-collapse:collapse;font-family:Arial,sans-serif;">'
        f"<thead><tr>{header}</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


# ============================================================================
# MAIN EMAIL BUILDER
# ============================================================================

def build_alert_email(
    date_str: str,
    # ── BLOQUE 1: KPIs ejecutivos ───────────────────────────────────────────
    stock_kpis: Optional[dict] = None,
    moi_kpis: Optional[dict] = None,         # {moi_historico, moi_forecast, moi_budget}
    ventas_ytd: Optional[dict] = None,       # {vn_real, vn_budget, vn_aa,
                                             #  aporte_real, aporte_budget, aporte_aa,
                                             #  margen_real, margen_aa}
    ventas_mtd: Optional[dict] = None,       # {CANAL: {und, neto, aporte, margen}}
    # ── BLOQUE 2: Stock crítico ─────────────────────────────────────────────
    critico_kpis: Optional[dict] = None,     # {total_skus, stock_costo, pct_saludable,
                                             #  stock_apunto, stock_critico, stock_liquidacion}
    critico_matrix: Optional[pd.DataFrame] = None,  # pre-built 6×6 matrix
    # ── BLOQUE 3: Proyección financiera ────────────────────────────────────
    proy_resumen: Optional[pd.DataFrame] = None,
    proy_por_area: Optional[pd.DataFrame] = None,
    proy_por_canal: Optional[pd.DataFrame] = None,
    # ── BLOQUE 4: Comex ────────────────────────────────────────────────────
    comex_atrasadas: Optional[pd.DataFrame] = None,
    comex_sin_carpeta: Optional[pd.DataFrame] = None,
    comex_sin_factura: Optional[pd.DataFrame] = None,
    comex_proximas: Optional[pd.DataFrame] = None,
    # ── BLOQUE 5: Plan de compras ──────────────────────────────────────────
    plan_compras: Optional[pd.DataFrame] = None,
    plan_kpis: Optional[dict] = None,
    # ── BLOQUE 6: Higiene ──────────────────────────────────────────────────
    higiene_kpis: Optional[dict] = None,
    higiene_sin_forecast: Optional[pd.DataFrame] = None,
    higiene_sin_perfil: Optional[pd.DataFrame] = None,
    # ── BLOQUE 7: InStock ────────────────────────────────────────────────
    instock_kpis: Optional[dict] = None,
    instock_evolution: Optional[pd.DataFrame] = None,
) -> str:
    """Assemble the complete HTML email body."""

    inner: list[str] = []

    # ══════════════════════════════════════════════════════════════════════════
    # BLOQUE 1 — Resumen Ejecutivo S&OP
    # ══════════════════════════════════════════════════════════════════════════
    inner.append(_section_title_html("📊 Resumen Ejecutivo S&OP", _C_PRIMARY))

    # Stock KPIs — énfasis en COSTO (CLP)
    if stock_kpis:
        cards = [
            _kpi_card_html(
                "Valor Inventario", _format_clp(stock_kpis.get("stock_costo", 0)),
                _C_PRIMARY,
                sub=f"CD: {_format_clp(stock_kpis.get('stock_costo_cd', 0))} · Tienda: {_format_clp(stock_kpis.get('stock_costo_tienda', 0))}",
            ),
        ]
        # MOI Compañía (3 cards)
        if moi_kpis:
            def _moi_color(val):
                if val <= 0: return _C_SUBTEXT
                return _C_OK if val < 6 else _C_WARNING if val < 12 else _C_CRITICAL
            moi_h = moi_kpis.get("moi_historico", 0)
            moi_f = moi_kpis.get("moi_forecast", 0)
            moi_b = moi_kpis.get("moi_budget", 0)
            n_h = moi_kpis.get("n_months_hist", 6)
            n_f = moi_kpis.get("n_months_fc", 6)
            cards.append(_kpi_card_html(
                f"MOI Histórico ({n_h}m)", f"{moi_h:.1f}m", _moi_color(moi_h),
                sub="Stock / COGS prom hist",
            ))
            cards.append(_kpi_card_html(
                f"MOI Forecast ({n_f}m)", f"{moi_f:.1f}m", _moi_color(moi_f),
                sub="Stock / COGS prom FC",
            ))
            cards.append(_kpi_card_html(
                "MOI Budget", f"{moi_b:.1f}m", _moi_color(moi_b),
                sub="Stock / COGS budget",
            ))
        else:
            cards.append(_kpi_card_html(
                "Stock Total", f"{stock_kpis.get('stock_total', 0):,.0f} und",
                _C_PRIMARY,
            ))
        inner.append(_kpi_row_html(cards))

    # VN / Aporte YTD vs Budget vs AA
    if ventas_ytd:
        vn_r   = ventas_ytd.get("vn_real", 0)
        vn_b   = ventas_ytd.get("vn_budget", 0)
        vn_aa  = ventas_ytd.get("vn_aa", 0)
        ap_r   = ventas_ytd.get("aporte_real", 0)
        ap_b   = ventas_ytd.get("aporte_budget", 0)
        ap_aa  = ventas_ytd.get("aporte_aa", 0)
        mg_r   = ventas_ytd.get("margen_real", 0)
        mg_aa  = ventas_ytd.get("margen_aa", 0)
        mg_b   = ventas_ytd.get("margen_budget", 0)

        # Badges para margen
        mg_badges = ""
        if mg_b > 0:
            mg_badges += _variance_badge(mg_r, mg_b, "Bdgt")
        if mg_aa > 0:
            mg_badges += _variance_badge(mg_r, mg_aa, "AA")
        mg_ref_line = ""
        refs = []
        if mg_b > 0:
            refs.append(f"Bdgt: {mg_b:.1%}")
        if mg_aa > 0:
            refs.append(f"AA: {mg_aa:.1%}")
        if refs:
            mg_ref_line = f'<div style="font-size:9px;color:{_C_SUBTEXT};margin-top:4px;">{" &nbsp;|&nbsp; ".join(refs)}</div>'

        ytd_cards = (
            _kpi_vs_ref_card("Ventas Netas YTD", vn_r, vn_b, vn_aa, icon="💰")
            + _kpi_vs_ref_card("Aporte Bruto YTD", ap_r, ap_b, ap_aa, icon="📈")
            + f'<td style="padding:14px 12px;text-align:center;vertical-align:top;">'
            f'<div style="font-size:10px;color:{_C_SUBTEXT};text-transform:uppercase;'
            f'margin-bottom:4px;font-family:Arial,sans-serif;">📉 Margen Bruto YTD</div>'
            f'<div style="font-size:22px;font-weight:bold;color:{_C_PRIMARY};'
            f'font-family:Arial,sans-serif;">{mg_r:.1%}</div>'
            f'<div style="margin-top:5px;">{mg_badges}</div>'
            f'{mg_ref_line}</td>'
        )
        inner.append(
            f'<table style="width:100%;border-collapse:collapse;background:{_C_BG};'
            f'border-radius:8px;margin-bottom:16px;"><tr>{ytd_cards}</tr></table>'
        )

    # Ventas MTD por canal (3 canales + TOTAL + aporte + margen)
    if ventas_mtd:
        inner.append(_section_title_html("💰 Ventas MTD por Canal", _C_OK))
        # Orden fijo: Minorista, Etail, Mayorista, Total
        canal_order = ["MINORISTA", "ETAIL", "MAYORISTA", "TOTAL"]
        _th = (
            lambda h: f'<th style="padding:8px 10px;color:#fff;font-size:10px;text-align:right;'
            f'font-family:Arial,sans-serif;border:1px solid {_C_BORDER};">{h}</th>'
        )
        headers = ["Canal", "Unidades", "Venta Neta", "vs AA", "vs BU", "Aporte", "Margen %"]
        mtd_header = f'<tr style="background:{_C_PRIMARY};">{"".join(_th(h) for h in headers)}</tr>'

        def _pct_badge(real, ref):
            if not ref or ref == 0:
                return "—"
            pct = (real - ref) / abs(ref) * 100
            c = _C_OK if pct >= 0 else _C_CRITICAL
            s = "+" if pct >= 0 else ""
            return (f'<span style="font-size:10px;color:{c};background:{c}22;'
                    f'border-radius:3px;padding:1px 4px;">{s}{pct:.1f}%</span>')

        mtd_rows = ""
        for canal in canal_order:
            v = ventas_mtd.get(canal, {})
            if not v:
                continue
            is_total = canal == "TOTAL"
            fw = "bold" if is_total else "normal"
            bg = "#e8f4fd" if is_total else _C_BG
            _td = (
                lambda val, align="right": f'<td style="padding:6px 10px;font-size:11px;'
                f'font-weight:{fw};text-align:{align};color:{_C_TEXT};'
                f'border:1px solid {_C_BORDER};font-family:Arial,sans-serif;">{val}</td>'
            )
            mtd_rows += (
                f'<tr style="background:{bg};">'
                + _td(canal.title(), "left")
                + _td(f'{v.get("und", 0):,.0f}'.replace(",", "."))
                + _td(_format_clp(v.get("neto", 0)))
                + _td(_pct_badge(v.get("neto", 0), v.get("neto_aa", 0)))
                + _td(_pct_badge(v.get("neto", 0), v.get("neto_bu", 0)))
                + _td(_format_clp(v.get("aporte", 0)))
                + _td(f'{v.get("margen", 0):.1%}')
                + "</tr>"
            )
        inner.append(
            f'<table style="width:100%;border-collapse:collapse;margin-bottom:16px;">'
            f'{mtd_header}{mtd_rows}</table>'
        )

    # ══════════════════════════════════════════════════════════════════════════
    # BLOQUE 2 — Stock Crítico (matriz salud 6×6)
    # ══════════════════════════════════════════════════════════════════════════
    if critico_kpis or critico_matrix is not None:
        inner.append(_section_title_html("🚨 Salud de Stock — Matriz MOI × Antigüedad", _C_CRITICAL))

    if critico_kpis:
        # Row 1: KPIs generales (todo en COSTO CLP)
        r1 = [
            _kpi_card_html("SKUs Analizados", f"{critico_kpis.get('total_skus', 0):,}", _C_PRIMARY),
            _kpi_card_html("Stock Total Costo", _format_clp(critico_kpis.get("stock_costo", 0)), _C_PRIMARY),
            _kpi_card_html("% Saludable", f"{critico_kpis.get('pct_saludable', 0):.1f}%", _C_OK,
                           sub="MOI <6m + Antig <6m"),
        ]
        inner.append(_kpi_row_html(r1))

        # Row 2: KPIs de riesgo (todo en COSTO CLP)
        r2 = [
            _kpi_card_html("Stock A Punto", _format_clp(critico_kpis.get("stock_apunto", 0)),
                           "#F9A825", sub="MOI 8-12m + Antig 8-12m"),
            _kpi_card_html("Stock Crítico", _format_clp(critico_kpis.get("stock_critico", 0)),
                           _C_CRITICAL, sub="MOI ≥12m + Antig ≥12m"),
            _kpi_card_html("Stock Liquidación", _format_clp(critico_kpis.get("stock_liquidacion", 0)),
                           "#B71C1C", sub="MOI ≥24m + Antig ≥24m"),
        ]
        inner.append(_kpi_row_html(r2))

    # Health matrix 6×6
    if critico_matrix is not None and not critico_matrix.empty:
        inner.append(
            f'<p style="font-size:11px;color:{_C_SUBTEXT};font-family:Arial,sans-serif;margin:8px 0 6px 0;">'
            f'Distribución de SKUs por rango MOI (filas) × Antigüedad (columnas). '
            f'Colores: 🟢 Saludable → 🟡 Observar → 🟠 Riesgoso → 🔴 Crítico</p>'
        )
        inner.append(_health_matrix_html(critico_matrix))

    # ══════════════════════════════════════════════════════════════════════════
    # BLOQUE 3 — Proyección Financiera
    # ══════════════════════════════════════════════════════════════════════════
    if proy_resumen is not None and not proy_resumen.empty:
        inner.append(_section_title_html("📈 Proyección Financiera 2026-2027", _C_PRIMARY))
        inner.append(_df_to_html_table(proy_resumen, max_rows=10))

    if proy_por_area is not None and not proy_por_area.empty:
        try:
            categories = proy_por_area["AREA"].tolist()
            series = {}
            if "VN_2026" in proy_por_area.columns:
                series["2026 Real+FC"] = proy_por_area["VN_2026"].tolist()
            if "VN_AA" in proy_por_area.columns:
                series["2025 (AA)"] = proy_por_area["VN_AA"].tolist()
            if series:
                b64 = _make_bar_chart(categories, series,
                                      "Venta Neta por Área — 2026 vs 2025 (CLP)",
                                      colors=[_C_PRIMARY, _C_AA])
                if b64:
                    inner.append(_embed_img(b64, alt="VN por área"))
        except Exception:
            pass

    if proy_por_canal is not None and not proy_por_canal.empty:
        try:
            categories = proy_por_canal["CANAL"].tolist()
            series = {}
            if "VN_2026" in proy_por_canal.columns:
                series["2026 Real+FC"] = proy_por_canal["VN_2026"].tolist()
            if "VN_AA" in proy_por_canal.columns:
                series["2025 (AA)"] = proy_por_canal["VN_AA"].tolist()
            if series:
                b64 = _make_bar_chart(categories, series,
                                      "Venta Neta por Canal — 2026 vs 2025 (CLP)",
                                      colors=[_C_PRIMARY, _C_AA], figsize=(7, 4))
                if b64:
                    inner.append(_embed_img(b64, alt="VN por canal"))
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    # BLOQUE 4 — Comex
    # ══════════════════════════════════════════════════════════════════════════
    if comex_atrasadas is not None and not comex_atrasadas.empty:
        inner.append(_section_title_html(f"⚠️ POs Atrasadas — {len(comex_atrasadas):,} líneas", _C_WARNING))
        inner.append(_df_to_html_table(comex_atrasadas))

    if comex_sin_carpeta is not None and not comex_sin_carpeta.empty:
        inner.append(_section_title_html(f"📁 POs sin Carpeta Comex — {len(comex_sin_carpeta):,} líneas", _C_WARNING))
        inner.append(_df_to_html_table(comex_sin_carpeta))

    # -- Carga en Transito sin Diario de Factura --
    if comex_sin_factura is not None and not comex_sin_factura.empty:
        inner.append(_section_title_html(
            f"📄 Carga en Transito sin Diario de Factura — {len(comex_sin_factura):,} lineas",
            _C_WARNING,
        ))
        _sf_cols = ["PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "NOM_PROVEEDOR",
                    "CARPETA_COMEX", "ETA_CALC", "QTY_PENDIENTE", "MONTOMN"]
        _sf_show = [c for c in _sf_cols if c in comex_sin_factura.columns]
        inner.append(_df_to_html_table(comex_sin_factura[_sf_show].head(30)))

    # -- POs Proximas a Llegar --
    if comex_proximas is not None and not comex_proximas.empty:
        inner.append(_section_title_html(
            f"🚢 POs Proximas a Llegar — {len(comex_proximas):,} lineas (por prioridad)",
            _C_PRIMARY,
        ))
        inner.append(
            f'<p style="font-size:11px;color:{_C_SUBTEXT};font-family:Arial,sans-serif;'
            f'margin-bottom:6px;">Prioridad = VN Potencial ÷ (1 + MOI Actual). '
            f'Mayor prioridad = bajo MOI + alta venta potencial.</p>'
        )
        _px_cols = ["PO", "SKU_PRODUCTO", "NOM_PRODUCTO", "NOM_PROVEEDOR",
                    "CARPETA_COMEX", "ETA_CALC", "DIAS_HASTA_ETA",
                    "QTY_PENDIENTE", "VN_POTENCIAL", "MOI_ACTUAL", "PRIORIDAD_SCORE"]
        _px_show = [c for c in _px_cols if c in comex_proximas.columns]
        inner.append(_df_to_html_table(comex_proximas[_px_show].head(30)))

    # ══════════════════════════════════════════════════════════════════════════
    # BLOQUE 5 — Plan de Compras
    # ══════════════════════════════════════════════════════════════════════════
    if plan_kpis or (plan_compras is not None and not plan_compras.empty):
        inner.append(_section_title_html("🛒 Plan de Compras — En Tránsito", _C_PURPLE))
        if plan_kpis:
            pk = [
                _kpi_card_html("POs en Tránsito", str(plan_kpis.get("n_pos", 0)), _C_PURPLE),
                _kpi_card_html("Monto Tránsito", _format_clp(plan_kpis.get("monto_transito", 0)), _C_PURPLE),
                _kpi_card_html("Unidades Tránsito", f"{plan_kpis.get('und_transito', 0):,.0f}", _C_PURPLE),
            ]
            inner.append(_kpi_row_html(pk))
        if plan_compras is not None and not plan_compras.empty:
            inner.append(
                f'<p style="font-size:11px;color:{_C_SUBTEXT};font-family:Arial,sans-serif;'
                f'margin-bottom:6px;">Próximas llegadas por ETA (top {len(plan_compras)} por monto):</p>'
            )
            inner.append(_df_to_html_table(plan_compras, max_rows=25))

    # ══════════════════════════════════════════════════════════════════════════
    # BLOQUE 6 — Higiene de Abastecimiento
    # ══════════════════════════════════════════════════════════════════════════
    if higiene_kpis or higiene_sin_forecast is not None or higiene_sin_perfil is not None:
        inner.append(_section_title_html("🩺 Higiene de Abastecimiento", _C_PURPLE))

        if higiene_kpis:
            hk = []
            if "sin_forecast" in higiene_kpis:
                hk.append(_kpi_card_html("SKUs sin Forecast", str(higiene_kpis["sin_forecast"]), _C_CRITICAL,
                                         sub="Stock MIX sin demanda asignada"))
            if "fc_1_canal" in higiene_kpis:
                hk.append(_kpi_card_html("FC en 1 Canal", str(higiene_kpis["fc_1_canal"]), _C_WARNING,
                                         sub="Forecast en solo 1 canal"))
            if "sin_perfil" in higiene_kpis:
                hk.append(_kpi_card_html("CD sin Perfil", str(higiene_kpis["sin_perfil"]), _C_WARNING,
                                         sub="SKUs en CD sin perfil de tienda"))
            if "valor_riesgo" in higiene_kpis:
                hk.append(_kpi_card_html("$ en Riesgo", _format_clp(higiene_kpis["valor_riesgo"]), _C_PURPLE,
                                         sub="Valor stock CD sin perfil"))
            if hk:
                inner.append(_kpi_row_html(hk))

        if higiene_sin_forecast is not None and not higiene_sin_forecast.empty:
            inner.append(
                f'<p style="font-size:11px;color:{_C_CRITICAL};font-family:Arial,sans-serif;'
                f'font-weight:bold;margin:10px 0 4px 0;">⛔ SKUs MIX sin Forecast:</p>'
            )
            inner.append(_df_to_html_table(higiene_sin_forecast, max_rows=20))

        if higiene_sin_perfil is not None and not higiene_sin_perfil.empty:
            inner.append(
                f'<p style="font-size:11px;color:{_C_WARNING};font-family:Arial,sans-serif;'
                f'font-weight:bold;margin:10px 0 4px 0;">⚠️ SKUs en CD Principal sin Perfil en Tiendas:</p>'
            )
            inner.append(_df_to_html_table(higiene_sin_perfil, max_rows=20))

    # ══════════════════════════════════════════════════════════════════════════
    # BLOQUE 7 — InStock Disponibilidad (Tiendas + CD)
    # ══════════════════════════════════════════════════════════════════════════
    if instock_kpis or instock_evolution is not None:
        inner.append(_section_title_html("📦 Disponibilidad InStock — Tiendas & CD", _C_PRIMARY))

        if instock_kpis:
            is_t = instock_kpis.get("is_tienda_pct", 0)
            is_cd = instock_kpis.get("is_cd_pct", 0)
            d7_t = instock_kpis.get("delta_7d_tienda", 0)
            d7_cd = instock_kpis.get("delta_7d_cd", 0)

            def _is_color(pct):
                if pct >= 93:
                    return _C_OK
                if pct >= 85:
                    return _C_WARNING
                return _C_CRITICAL

            def _delta_badge(d):
                sign = "+" if d >= 0 else ""
                c = _C_OK if d >= 0 else _C_CRITICAL
                return f'{sign}{d:.1f}pp'

            is_cards = [
                _kpi_card_html(
                    "IS% Tiendas (90d)", f"{is_t:.1f}%", _is_color(is_t),
                    sub=f"vs 7d atrás: {_delta_badge(d7_t)}",
                ),
                _kpi_card_html(
                    "IS% CD (90d)", f"{is_cd:.1f}%", _is_color(is_cd),
                    sub=f"vs 7d atrás: {_delta_badge(d7_cd)}",
                ),
            ]
            # Add per-area breakdown if available
            area_breakdown = instock_kpis.get("por_area", {})
            for area_name, area_data in list(area_breakdown.items())[:2]:
                is_cards.append(_kpi_card_html(
                    f"IS% {area_name}", f"{area_data.get('tienda', 0):.1f}%",
                    _is_color(area_data.get("tienda", 0)),
                    sub=f"CD: {area_data.get('cd', 0):.1f}%",
                ))
            inner.append(_kpi_row_html(is_cards))

        # Line chart: evolution last 14 days
        if instock_evolution is not None and not instock_evolution.empty:
            try:
                evo = instock_evolution.sort_values("FECHA")
                dates = pd.to_datetime(evo["FECHA"]).tolist()
                chart_series = {}
                if "IS_TIENDA_PCT" in evo.columns:
                    chart_series["Tiendas"] = evo["IS_TIENDA_PCT"].tolist()
                if "IS_CD_PCT" in evo.columns:
                    chart_series["CD"] = evo["IS_CD_PCT"].tolist()
                if chart_series:
                    b64 = _make_line_chart(
                        dates, chart_series,
                        "Evolución InStock % — Últimos 14 días",
                        ylabel="InStock %",
                        colors=[_C_PRIMARY, _C_WARNING],
                    )
                    if b64:
                        inner.append(_embed_img(b64, alt="InStock evolution"))
            except Exception:
                pass

    # ── Footer ────────────────────────────────────────────────────────────────
    inner.append(
        f'<div style="margin-top:32px;padding:14px;background:{_C_BG};border-radius:8px;'
        f'border:1px solid {_C_BORDER};text-align:center;">'
        f'<p style="margin:0;font-size:11px;color:{_C_SUBTEXT};font-family:Arial,sans-serif;">'
        f"Portal de Planificación Dorel Chile &nbsp;·&nbsp; Supply Chain Analytics &nbsp;·&nbsp; {date_str}"
        f"</p></div>"
    )

    header_html = _header_html(
        "🚦 Alertas Supply Chain — Dorel Chile",
        f"Reporte generado el {date_str}",
    )
    inner_html = "".join(inner)

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
        f'<body style="margin:0;padding:0;background:{_C_BG};font-family:Arial,sans-serif;">'
        f'<table style="max-width:960px;margin:20px auto;background:#fff;'
        f'border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,0.08);overflow:hidden;">'
        f"<tr><td>{header_html}"
        f'<div style="padding:24px 32px;">{inner_html}</div>'
        f"</td></tr></table></body></html>"
    )
