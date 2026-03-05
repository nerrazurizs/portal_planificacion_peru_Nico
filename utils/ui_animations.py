"""Lottie animations and enhanced loading components for the Dorel Portal.

Provides:
  - lottie_spinner()    — replacement for st.spinner() with themed Lottie animation
  - show_lottie()       — display a Lottie animation inline
  - show_success()      — animated success checkmark
  - show_empty_state()  — friendly empty-state illustration
  - animated_kpi_card() — KPI card with CSS count-up animation
  - css_loader()        — fallback CSS loader (no dependencies)
"""

import json
import os
from pathlib import Path

import streamlit as st

# ── Paths ─────────────────────────────────────────────────────────────────────
_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "lottie"

# ── Animation catalog ─────────────────────────────────────────────────────────
# Keys map to files in assets/lottie/
ANIMATIONS = {
    "loading":        "loading_circle.json",
    "loading_dots":   "loading_dots.json",
    "delivery":       "delivery_truck.json",
    "box":            "box_open.json",
    "success":        "success_check.json",
    "empty":          "empty_box.json",
    "chart":          "chart_analytics.json",
    "analytics":      "data_analytics.json",
}

# Theme descriptions for different loading contexts
LOADER_THEMES = {
    "snowflake":  ("loading_dots", "Consultando Snowflake..."),
    "stock":      ("box",          "Calculando inventario..."),
    "comex":      ("delivery",     "Cargando tránsitos..."),
    "proyeccion": ("chart",        "Procesando proyección..."),
    "general":    ("loading",      "Cargando..."),
    "export":     ("loading_dots", "Generando archivo..."),
}


# ── Lottie loading ────────────────────────────────────────────────────────────

@st.cache_data(ttl=86400)
def _load_lottie_file(name: str):
    """Load a Lottie JSON file from the assets directory (cached 24h)."""
    filename = ANIMATIONS.get(name, name)
    filepath = _ASSETS_DIR / filename
    if filepath.exists():
        with open(filepath, "r") as f:
            return json.load(f)
    return None


def show_lottie(name: str = "loading", height: int = 180, key: str = None,
                loop: bool = True, speed: float = 1.0):
    """Display a Lottie animation by name.

    Args:
        name: Animation name from ANIMATIONS catalog or filename.
        height: Height in pixels.
        key: Streamlit widget key (auto-generated if None).
        loop: Whether to loop the animation.
        speed: Playback speed multiplier.
    """
    try:
        from streamlit_lottie import st_lottie
        anim = _load_lottie_file(name)
        if anim:
            _key = key or f"lottie_{name}_{id(anim)}"
            st_lottie(anim, height=height, key=_key, loop=loop, speed=speed)
            return True
    except ImportError:
        pass
    return False


# ── Theme → CSS animation mapping ────────────────────────────────────────────
_THEME_ICONS = {
    "snowflake":  "\u2744\uFE0F",   # ❄️
    "stock":      "\U0001F4E6",      # 📦
    "comex":      "\U0001F69A",      # 🚚
    "proyeccion": "\U0001F4CA",      # 📊
    "export":     "\U0001F4BE",      # 💾
    "general":    "\u2699\uFE0F",    # ⚙️
}


class lottie_spinner:
    """Context manager that shows a fullscreen blur overlay with animation.

    Implemented as a class (not @contextmanager generator) to avoid the
    'generator didn't stop after throw()' RuntimeError that occurs when
    exceptions propagate through generator-based context managers in
    certain Streamlit execution contexts.

    Usage:
        with lottie_spinner("snowflake"):
            data = pd.read_sql(query, conn)

    Falls back to st.spinner if markup injection fails.
    """

    def __init__(self, theme: str = "general", height: int = 200):
        self._theme = theme
        self._height = height
        self._placeholder = None
        self._use_overlay = False
        self._spinner_cm = None  # fallback st.spinner context manager

    def __enter__(self):
        _, message = LOADER_THEMES.get(self._theme, LOADER_THEMES["general"])
        icon = _THEME_ICONS.get(self._theme, _THEME_ICONS["general"])
        self._message = message

        try:
            overlay_id = f"_dorel_ov_{self._theme}_{id(message)}"
            self._placeholder = st.empty()
            with self._placeholder.container():
                st.markdown(
                    f"""
                    <style>
                    #{overlay_id} {{
                        position: fixed;
                        top: 0; left: 0; right: 0; bottom: 0;
                        z-index: 99999;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        padding-left: 16rem;
                        backdrop-filter: blur(6px);
                        -webkit-backdrop-filter: blur(6px);
                        background: rgba(255, 255, 255, 0.50);
                        animation: _drl_fade 0.3s ease-out;
                    }}
                    #{overlay_id} .card {{
                        background: rgba(255, 255, 255, 0.92);
                        border-radius: 24px;
                        padding: 48px 56px 36px;
                        box-shadow: 0 16px 48px rgba(0,0,0,0.12);
                        display: flex;
                        flex-direction: column;
                        align-items: center;
                        gap: 8px;
                    }}
                    #{overlay_id} .icon {{
                        font-size: 52px;
                        animation: _drl_bounce 1.8s ease-in-out infinite;
                    }}
                    #{overlay_id} .dots {{
                        display: flex; gap: 8px; margin-top: 8px;
                    }}
                    #{overlay_id} .dots span {{
                        width: 10px; height: 10px; border-radius: 50%;
                        background: #065E8B;
                        animation: _drl_dot 1.4s ease-in-out infinite both;
                    }}
                    #{overlay_id} .dots span:nth-child(1) {{ animation-delay: -0.32s; }}
                    #{overlay_id} .dots span:nth-child(2) {{ animation-delay: -0.16s; }}
                    #{overlay_id} .dots span:nth-child(3) {{ animation-delay: 0s; }}
                    #{overlay_id} .msg {{
                        color: #4a4a4a; font-size: 1.05rem;
                        font-weight: 600; letter-spacing: 0.3px;
                        margin-top: 4px;
                    }}
                    @keyframes _drl_fade {{
                        from {{ opacity: 0; }} to {{ opacity: 1; }}
                    }}
                    @keyframes _drl_bounce {{
                        0%, 100% {{ transform: translateY(0); }}
                        50%      {{ transform: translateY(-12px); }}
                    }}
                    @keyframes _drl_dot {{
                        0%, 80%, 100% {{ transform: scale(0.4); opacity: 0.3; }}
                        40%           {{ transform: scale(1.0); opacity: 1; }}
                    }}
                    </style>
                    <div id="{overlay_id}">
                        <div class="card">
                            <div class="icon">{icon}</div>
                            <div class="dots">
                                <span></span><span></span><span></span>
                            </div>
                            <div class="msg">{message}</div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            self._use_overlay = True
        except Exception:
            self._use_overlay = False
            # Fallback: delegate to st.spinner
            self._spinner_cm = st.spinner(message)
            self._spinner_cm.__enter__()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._use_overlay:
            try:
                if self._placeholder is not None:
                    self._placeholder.empty()
            except Exception:
                pass  # Never let cleanup interfere with exception propagation
        elif self._spinner_cm is not None:
            try:
                self._spinner_cm.__exit__(exc_type, exc_val, exc_tb)
            except Exception:
                pass
        # Never suppress exceptions — always return False
        return False


def show_success(message: str = "Listo", height: int = 120):
    """Show an animated success checkmark with a message."""
    ok = show_lottie("success", height=height, loop=False, key=f"success_{hash(message)}")
    if not ok:
        st.success(f"✅ {message}")
    else:
        st.markdown(
            f"<p style='text-align:center; color:#1B5E20; font-weight:600; "
            f"font-size:1rem; margin-top:-8px;'>{message}</p>",
            unsafe_allow_html=True,
        )


def show_empty_state(message: str = "No hay datos disponibles",
                     subtitle: str = "", height: int = 160):
    """Show a friendly empty-state illustration with message."""
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        ok = show_lottie("empty", height=height, loop=True,
                         key=f"empty_{hash(message)}", speed=0.7)
        if not ok:
            # CSS fallback: animated empty box
            st.html(_css_empty_state())

        st.markdown(
            f"<div style='text-align:center; margin-top:-8px;'>"
            f"<p style='color:#605D57; font-size:1rem; font-weight:600;'>{message}</p>"
            f"<p style='color:#B0AEAA; font-size:0.85rem;'>{subtitle}</p>"
            f"</div>",
            unsafe_allow_html=True,
        )


# ── CSS-only loaders (fallback, zero dependencies) ──────────────────────────

def css_loader(style: str = "dots", color: str = "#065E8B",
               message: str = "Cargando...", size: int = 12):
    """Render a CSS-only animated loader.

    Args:
        style: "dots", "pulse", or "boxes"
        color: Primary color (default: Dorel blue)
        message: Text below the loader
        size: Size in pixels
    """
    loaders = {
        "dots":  _css_dots_loader(color, size),
        "pulse": _css_pulse_loader(color, size),
        "boxes": _css_boxes_loader(color, size),
    }
    html = loaders.get(style, loaders["dots"])
    html += (
        f"<p style='text-align:center; color:#605D57; font-size:0.8rem; "
        f"margin-top:12px;'>{message}</p>"
    )
    st.html(f"<div style='display:flex; flex-direction:column; "
            f"align-items:center; padding:24px 0;'>{html}</div>")


def _css_dots_loader(color: str, size: int) -> str:
    return f"""
    <style>
    .dorel-dots {{ display:flex; gap:{size//2}px; }}
    .dorel-dots span {{
        width:{size}px; height:{size}px; border-radius:50%;
        background:{color}; animation: dorel-bounce 1.4s infinite ease-in-out both;
    }}
    .dorel-dots span:nth-child(1) {{ animation-delay: -0.32s; }}
    .dorel-dots span:nth-child(2) {{ animation-delay: -0.16s; }}
    @keyframes dorel-bounce {{
        0%, 80%, 100% {{ transform: scale(0); opacity:0.3; }}
        40% {{ transform: scale(1); opacity:1; }}
    }}
    </style>
    <div class="dorel-dots"><span></span><span></span><span></span></div>
    """


def _css_pulse_loader(color: str, size: int) -> str:
    s = size * 3
    return f"""
    <style>
    .dorel-pulse {{
        width:{s}px; height:{s}px; border-radius:50%;
        background:{color}; animation: dorel-pulse-anim 1.5s ease-in-out infinite;
    }}
    @keyframes dorel-pulse-anim {{
        0% {{ transform: scale(0.8); opacity:0.5; }}
        50% {{ transform: scale(1.2); opacity:1; }}
        100% {{ transform: scale(0.8); opacity:0.5; }}
    }}
    </style>
    <div class="dorel-pulse"></div>
    """


def _css_boxes_loader(color: str, size: int) -> str:
    """Supply chain themed: 3 boxes stacking animation."""
    s = size
    return f"""
    <style>
    .dorel-boxes {{ display:flex; gap:{s//3}px; align-items:flex-end; height:{s*3}px; }}
    .dorel-boxes div {{
        width:{s}px; background:{color}; border-radius:2px;
        animation: dorel-stack 1.2s ease-in-out infinite;
    }}
    .dorel-boxes div:nth-child(1) {{ animation-delay: 0s; height:{s}px; }}
    .dorel-boxes div:nth-child(2) {{ animation-delay: 0.2s; height:{s*2}px; }}
    .dorel-boxes div:nth-child(3) {{ animation-delay: 0.4s; height:{s*3}px; }}
    @keyframes dorel-stack {{
        0%, 100% {{ opacity:0.3; transform: scaleY(0.3); }}
        50% {{ opacity:1; transform: scaleY(1); }}
    }}
    </style>
    <div class="dorel-boxes"><div></div><div></div><div></div></div>
    """


def _css_empty_state() -> str:
    """CSS-only empty state illustration (package icon with question mark)."""
    return """
    <div style="text-align:center; padding:20px;">
      <svg width="80" height="80" viewBox="0 0 80 80" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="15" y="25" width="50" height="40" rx="4" fill="#E5E5E5" stroke="#B0AEAA" stroke-width="2"/>
        <path d="M15 35 L40 22 L65 35" stroke="#B0AEAA" stroke-width="2" fill="#D8D7D5"/>
        <text x="40" y="52" text-anchor="middle" font-size="20" fill="#B0AEAA" font-weight="bold">?</text>
      </svg>
    </div>
    """


# ── Animated KPI Card ─────────────────────────────────────────────────────────

def animated_kpi_card(label: str, value: str, delta: str = "",
                      delta_color: str = "#10b981", icon: str = "",
                      accent_color: str = "#065E8B"):
    """Render a KPI card with entrance animation.

    Args:
        label: KPI title
        value: Main value (formatted string)
        delta: Optional delta text (e.g. "+12%")
        delta_color: Color for the delta text
        icon: Optional emoji icon
        accent_color: Left border color
    """
    delta_html = ""
    if delta:
        delta_html = (
            f"<span style='font-size:0.8rem; color:{delta_color}; "
            f"font-weight:600; margin-left:8px;'>{delta}</span>"
        )

    icon_html = f"<span style='font-size:1.3rem; margin-right:6px;'>{icon}</span>" if icon else ""

    uid = f"kpi_{hash(label + value)}"
    st.html(f"""
    <style>
    @keyframes dorel-slide-up {{
        from {{ opacity:0; transform: translateY(16px); }}
        to {{ opacity:1; transform: translateY(0); }}
    }}
    #{uid} {{
        background: white;
        border-radius: 12px;
        border-left: 4px solid {accent_color};
        padding: 16px 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
        animation: dorel-slide-up 0.5s ease-out;
    }}
    #{uid}:hover {{
        box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        transition: box-shadow 0.3s ease;
    }}
    </style>
    <div id="{uid}">
        <p style="color:#605D57; font-size:0.75rem; font-weight:600;
                  text-transform:uppercase; letter-spacing:0.5px; margin:0 0 4px 0;">
            {icon_html}{label}
        </p>
        <p style="color:#2A2927; font-size:1.6rem; font-weight:700; margin:0;">
            {value}{delta_html}
        </p>
    </div>
    """)


def animated_kpi_row(kpis: list):
    """Render a row of animated KPI cards.

    Args:
        kpis: List of dicts with keys: label, value, delta (optional),
              delta_color (optional), icon (optional), accent_color (optional)
    """
    cols = st.columns(len(kpis))
    for col, kpi in zip(cols, kpis):
        with col:
            animated_kpi_card(
                label=kpi["label"],
                value=kpi["value"],
                delta=kpi.get("delta", ""),
                delta_color=kpi.get("delta_color", "#10b981"),
                icon=kpi.get("icon", ""),
                accent_color=kpi.get("accent_color", "#065E8B"),
            )


# ── Status Timeline (for PO tracking) ────────────────────────────────────────

def po_status_timeline(current_status: str):
    """Render a horizontal timeline for PO status progression.

    Args:
        current_status: One of "Abierta", "Confirmada", "En Tránsito", "PO Recepcionada"
    """
    steps = [
        ("Abierta", "📝", "#9E9E9E"),
        ("Confirmada", "✅", "#1E88E5"),
        ("En Tránsito", "🚢", "#FB8C00"),
        ("PO Recepcionada", "📦", "#43A047"),
    ]

    current_idx = next(
        (i for i, (s, _, _) in enumerate(steps) if s == current_status), 0
    )

    items_html = ""
    for i, (label, icon, color) in enumerate(steps):
        is_active = i <= current_idx
        is_current = i == current_idx
        opacity = "1" if is_active else "0.35"
        border = f"3px solid {color}" if is_current else f"2px solid {'#D8D7D5' if not is_active else color}"
        bg = f"{color}15" if is_active else "white"
        weight = "700" if is_current else "500"

        items_html += f"""
        <div style="display:flex; flex-direction:column; align-items:center;
                    opacity:{opacity}; flex:1;">
            <div style="width:40px; height:40px; border-radius:50%; border:{border};
                        background:{bg}; display:flex; align-items:center;
                        justify-content:center; font-size:1.2rem;
                        {'animation: dorel-pulse-anim 2s ease-in-out infinite;' if is_current else ''}">
                {icon}
            </div>
            <p style="font-size:0.7rem; color:{color if is_active else '#B0AEAA'};
                      font-weight:{weight}; margin-top:6px; text-align:center;">
                {label}
            </p>
        </div>
        """
        # Connector line between steps
        if i < len(steps) - 1:
            line_color = color if is_active and i < current_idx else "#E5E5E5"
            items_html += f"""
            <div style="flex:0.5; height:2px; background:{line_color};
                        align-self:center; margin-top:-20px;"></div>
            """

    st.html(f"""
    <style>
    @keyframes dorel-pulse-anim {{
        0% {{ transform: scale(1); }}
        50% {{ transform: scale(1.1); }}
        100% {{ transform: scale(1); }}
    }}
    </style>
    <div style="display:flex; align-items:flex-start; padding:12px 0; gap:0;">
        {items_html}
    </div>
    """)
