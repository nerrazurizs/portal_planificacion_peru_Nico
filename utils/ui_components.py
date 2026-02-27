"""Reusable HTML UI components for the Dorel Supply Chain Portal.

All functions return raw HTML strings. The caller wraps them with:
    st.html(html)

This keeps functions pure, testable, and free of Streamlit side-effects.
"""

import math
import os

from config import COLORS

# ---------------------------------------------------------------------------
# Status configuration
# ---------------------------------------------------------------------------

STATUS_CONFIG = {
    "on_track": {
        "label": "En curso",
        "color": COLORS["status_en_curso"],
        "bg": "#f0f9ff",
    },
    "at_risk": {
        "label": "En riesgo",
        "color": COLORS["status_at_risk"],
        "bg": "#fffbeb",
    },
    "critical": {
        "label": "Critico",
        "color": COLORS["status_critical"],
        "bg": "#fef2f2",
    },
    "completed": {
        "label": "Completado",
        "color": COLORS["status_on_track"],
        "bg": "#f0fdf4",
    },
}


def _human_fmt(value: float) -> str:
    """Compact number formatting: 1.2B, 345M, 12K, 1,234."""
    abs_v = abs(value)
    sign = "-" if value < 0 else ""
    if abs_v >= 1_000_000_000:
        return f"{sign}{abs_v / 1_000_000_000:,.1f}B"
    if abs_v >= 1_000_000:
        return f"{sign}{abs_v / 1_000_000:,.1f}M"
    if abs_v >= 10_000:
        return f"{sign}{abs_v / 1_000:,.0f}K"
    if abs_v >= 1_000:
        return f"{sign}{abs_v:,.0f}"
    return f"{sign}{abs_v:,.1f}"


# ---------------------------------------------------------------------------
# Primitive components
# ---------------------------------------------------------------------------


def progress_ring_svg(
    percentage: float,
    color: str = "#0ea5e9",
    size: int = 64,
    stroke: int = 6,
) -> str:
    """Return inline SVG for a circular progress ring.

    Args:
        percentage: 0-100 float.
        color: Hex color for the progress arc.
        size: Width/height in pixels.
        stroke: Stroke width.
    """
    pct = max(0.0, min(100.0, float(percentage)))
    radius = (size - stroke) / 2
    center = size / 2
    circumference = 2 * math.pi * radius
    offset = circumference * (1 - pct / 100)

    return f"""
    <div style="position:relative;width:{size}px;height:{size}px;flex-shrink:0;">
        <svg width="{size}" height="{size}" viewBox="0 0 {size} {size}"
             style="transform:rotate(-90deg);">
            <circle cx="{center}" cy="{center}" r="{radius}"
                    fill="none" stroke="#e2e8f0" stroke-width="{stroke}"/>
            <circle cx="{center}" cy="{center}" r="{radius}"
                    fill="none" stroke="{color}" stroke-width="{stroke}"
                    stroke-dasharray="{circumference:.2f}"
                    stroke-dashoffset="{offset:.2f}"
                    stroke-linecap="round"
                    style="transition:stroke-dashoffset 0.5s ease;"/>
        </svg>
        <div style="position:absolute;top:50%;left:50%;
                    transform:translate(-50%,-50%);
                    font-size:{max(size // 5, 10)}px;font-weight:700;
                    color:{COLORS['black']};">
            {pct:.0f}%
        </div>
    </div>
    """


def status_badge(status: str) -> str:
    """Return HTML for a status badge with colored dot.

    Args:
        status: One of 'on_track', 'at_risk', 'critical', 'completed'.
    """
    cfg = STATUS_CONFIG.get(status, STATUS_CONFIG["on_track"])
    return f"""
    <span class="kpi-status" style="color:{cfg['color']};">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;
                     background:{cfg['color']};"></span>
        {cfg['label']}
    </span>
    """


def metric_bar(
    label: str,
    value: float,
    max_value: float,
    color: str = "#0ea5e9",
    suffix: str = "%",
) -> str:
    """Return HTML for a labeled horizontal progress bar.

    Args:
        label: Text label above bar.
        value: Current value.
        max_value: Maximum value for percentage.
        color: Bar fill color.
        suffix: Display suffix.
    """
    pct = min(100.0, (value / max_value * 100) if max_value > 0 else 0)
    if suffix == "%":
        display_val = f"{pct:.0f}%"
    else:
        display_val = f"{_human_fmt(value)}{suffix}"

    return f"""
    <div class="sub-metric">
        <div class="sub-metric-header">
            <span>{label}</span>
            <span style="font-weight:600;color:{COLORS['black']};">{display_val}</span>
        </div>
        <div class="sub-metric-bar">
            <div class="sub-metric-fill"
                 style="width:{pct:.1f}%;background:{color};"></div>
        </div>
    </div>
    """


# ---------------------------------------------------------------------------
# Composite components
# ---------------------------------------------------------------------------


def kpi_card(
    title: str,
    value_actual: float,
    value_target: float,
    description: str = "",
    badge_text: str = "KPI PRINCIPAL",
    status: str = "on_track",
    sub_metrics: list | None = None,
    format_fn=None,
    link_text: str = "",
    ring_size: int = 72,
) -> str:
    """Return full HTML for a professional KPI card with progress ring.

    Args:
        title: KPI title (e.g., "Venta 2026").
        value_actual: Current value.
        value_target: Target value.
        description: Gray subtitle.
        badge_text: Pill badge text.
        status: 'on_track', 'at_risk', 'critical', 'completed'.
        sub_metrics: List of dicts with keys: label, value, max, color (opt).
        format_fn: Callable(float) -> str. Defaults to "$" + human_fmt.
        link_text: Optional link text at bottom.
        ring_size: SVG ring diameter in px.
    """
    if format_fn is None:
        format_fn = lambda v: f"${_human_fmt(v)}"

    cfg = STATUS_CONFIG.get(status, STATUS_CONFIG["on_track"])
    pct = min(100.0, (value_actual / value_target * 100) if value_target > 0 else 0)

    badge_html = f"""
    <span class="kpi-badge" style="background:{cfg['bg']};color:{cfg['color']};">
        {badge_text}
    </span>
    """

    ring_html = progress_ring_svg(pct, cfg["color"], ring_size)

    # Sub-metrics
    sub_html = ""
    if sub_metrics:
        for sm in sub_metrics:
            sm_color = sm.get("color", cfg["color"])
            sub_html += metric_bar(
                sm.get("label", ""),
                sm.get("value", 0),
                sm.get("max", 100),
                sm_color,
                sm.get("suffix", "%"),
            )

    link_html = f'<div class="kpi-link">{link_text}</div>' if link_text else ""

    return f"""
    <div class="kpi-card-pro" style="--card-accent:{cfg['color']};">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
            <div style="flex:1;">
                {badge_html}
                {status_badge(status)}
                <div class="kpi-title">{title}</div>
                <div class="kpi-description">{description}</div>
            </div>
            {ring_html}
        </div>
        <div class="kpi-value-row">
            <div>
                <span class="kpi-big-value">{format_fn(value_actual)}</span>
                <span class="kpi-target"> / {format_fn(value_target)}</span>
            </div>
            {link_html}
        </div>
        {sub_html}
    </div>
    """


def snowflake_indicator(is_connected: bool) -> str:
    """Return HTML for a Snowflake connection status pill.

    Args:
        is_connected: True for green 'Online', False for red 'Offline'.
    """
    if is_connected:
        label = "Snowflake Online"
        dot_color = COLORS["status_on_track"]
        text_color = COLORS["status_on_track"]
        bg_color = "#f0fdf4"
        border_color = "#bbf7d0"
    else:
        label = "Snowflake Offline"
        dot_color = COLORS["status_critical"]
        text_color = COLORS["status_critical"]
        bg_color = "#fef2f2"
        border_color = "#fecaca"

    return (
        f'<span style="display:inline-flex;align-items:center;gap:6px;'
        f"font-size:0.75rem;font-weight:500;padding:0.3rem 0.8rem;"
        f"border-radius:50px;border:1px solid {border_color};"
        f"background:{bg_color};color:{text_color};white-space:nowrap;"
        f'">'
        f'<span style="display:inline-block;width:8px;height:8px;'
        f"border-radius:50%;background:{dot_color};"
        f'"></span>'
        f"{label}</span>"
    )


def page_header(
    title: str,
    subtitle: str = "",
    period_text: str = "",
    show_avatar: bool = True,
    sf_connected: bool | None = None,
    user_name: str = "",
    user_cargo: str = "",
) -> str:
    """Return HTML for a page header bar with title, subtitle, period pill, avatar.

    Args:
        title: Main page title.
        subtitle: Gray subtitle below title.
        period_text: Text for period pill (e.g., "Febrero 2026").
        show_avatar: Show user avatar circle.
        sf_connected: Show Snowflake indicator (None = hide).
        user_name: Full name for avatar tooltip and user info.
        user_cargo: Job title displayed below user name.
    """
    # Determine initials for avatar
    if user_name:
        parts = user_name.split()
        if len(parts) >= 2:
            initials = (parts[0][0] + parts[1][0]).upper()
        else:
            initials = parts[0][0].upper() if parts else "U"
    else:
        initials = os.environ.get("APP_USER", "U")[:1].upper()

    period_html = ""
    if period_text:
        period_html = (
            f'<span style="background:{COLORS["white"]};color:{COLORS["black"]};'
            f"font-size:0.8rem;font-weight:600;padding:0.4rem 1rem;"
            f'border-radius:50px;border:1px solid #e2e8f0;">'
            f"{period_text}</span>"
        )

    sf_html = ""
    if sf_connected is not None:
        sf_html = snowflake_indicator(sf_connected)

    user_info_html = ""
    if user_name:
        user_info_html = (
            f'<div style="display:flex;flex-direction:column;'
            f'align-items:flex-end;gap:2px;">'
            f'<span style="font-size:0.8rem;font-weight:600;'
            f'color:{COLORS["black"]};line-height:1.2;">{user_name}</span>'
            f'<span style="font-size:0.65rem;color:{COLORS["sidebar_text"]};'
            f'text-transform:uppercase;letter-spacing:0.5px;">'
            f"{user_cargo}</span></div>"
        )

    avatar_html = ""
    if show_avatar:
        avatar_html = (
            f'<div style="width:36px;height:36px;border-radius:50%;'
            f"background:linear-gradient(135deg,{COLORS['primary']},"
            f"{COLORS['tertiary_blue']});color:white;display:flex;"
            f"align-items:center;justify-content:center;font-weight:600;"
            f'font-size:0.85rem;flex-shrink:0;" title="{user_name}">'
            f"{initials}</div>"
        )

    return (
        f'<div style="display:flex;justify-content:space-between;'
        f"align-items:flex-start;padding:0.5rem 0 1.5rem 0;"
        f'border-bottom:1px solid #e2e8f0;margin-bottom:1.5rem;">'
        f"<div>"
        f'<h1 style="font-size:1.5rem !important;font-weight:700 !important;'
        f"color:{COLORS['black']} !important;margin:0 !important;"
        f'padding:0 !important;">{title}</h1>'
        f'<p style="font-size:0.85rem;color:{COLORS["sidebar_text"]};'
        f'margin:0.25rem 0 0 0;">{subtitle}</p>'
        f"</div>"
        f'<div style="display:flex;align-items:center;gap:12px;">'
        f"{sf_html}{period_html}{user_info_html}{avatar_html}"
        f"</div></div>"
    )


def simple_kpi_card(
    label: str,
    value: str,
    accent_color: str = "#065E8B",
    value_color: str | None = None,
    subtitle: str | None = None,
) -> str:
    """Return HTML for a simple KPI card with left border accent.

    Drop-in replacement for the inline HTML pattern used across modules.

    Args:
        label: Uppercase label text.
        value: Pre-formatted value string.
        accent_color: Left border color.
        value_color: Value text color (defaults to accent_color).
        subtitle: Optional small secondary line below the label.
    """
    vc = value_color or accent_color
    sub_html = (
        f'<div style="font-size:0.72rem;color:#94a3b8;margin-top:0.25rem;">{subtitle}</div>'
        if subtitle else ""
    )
    return f"""
    <div style="background:#ffffff;padding:1.25rem;border-radius:12px;
                box-shadow:0 1px 3px rgba(0,0,0,0.06),0 1px 2px rgba(0,0,0,0.04);
                border-left:4px solid {accent_color};">
        <div style="font-size:1.8rem;font-weight:700;color:{vc};line-height:1.2;">
            {value}
        </div>
        <div style="font-size:0.75rem;color:#94a3b8;text-transform:uppercase;
                    letter-spacing:0.5px;margin-top:0.3rem;">
            {label}
        </div>
        {sub_html}
    </div>
    """


def sidebar_user_card(
    nombre: str,
    email: str,
    initials: str = "",
) -> str:
    """Return HTML for a user identity card shown at the bottom of the sidebar.

    Args:
        nombre: Full display name.
        email: Email address.
        initials: 1-2 letter initials for the avatar circle.
    """
    if not initials:
        parts = nombre.split()
        if len(parts) >= 2:
            initials = (parts[0][0] + parts[1][0]).upper()
        else:
            initials = parts[0][0].upper() if parts else "U"

    return f"""
    <div class="sidebar-user-card">
        <div class="user-avatar">{initials}</div>
        <div class="user-details">
            <div class="user-name">{nombre}</div>
            <div class="user-email">{email}</div>
        </div>
    </div>
    """
