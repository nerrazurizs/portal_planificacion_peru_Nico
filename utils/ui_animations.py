"""Lottie animations and enhanced loading components for the Dorel Portal.

Provides:
  - lottie_spinner()    — replacement for st.spinner() with themed SVG animation
  - show_lottie()       — display a Lottie animation inline
  - show_success()      — animated success checkmark
  - show_empty_state()  — friendly empty-state illustration
  - animated_kpi_card() — KPI card with CSS count-up animation
  - css_loader()        — fallback CSS loader (no dependencies)
"""

import base64
import json
import os
from contextlib import contextmanager
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


# ── SVG supply-chain animations with SMIL (work inside <img> data URIs) ──────
# st.markdown(unsafe_allow_html=True) strips <svg> tags, so we encode each SVG
# as a base64 data URI in an <img> tag. SMIL animations (<animate>,
# <animateTransform>) work inside <img> unlike CSS @keyframes.

def _svg_to_img(svg_str: str, size: int = 120) -> str:
    """Convert SVG string to an <img> tag with base64 data URI."""
    b64 = base64.b64encode(svg_str.encode("utf-8")).decode("ascii")
    return (
        f'<img src="data:image/svg+xml;base64,{b64}" '
        f'width="{size}" height="{size}" alt="" style="display:block;" />'
    )


def _smil_delivery_truck() -> str:
    """Delivery truck with spinning wheels, bouncing body, and wind lines."""
    return (
        '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
        # Wind lines
        '<line x1="100" y1="35" x2="80" y2="35" stroke="#CBD5E1" stroke-width="2"'
        ' stroke-linecap="round" opacity="0">'
        '<animate attributeName="opacity" values="0;1;0" dur="1s" repeatCount="indefinite"/>'
        '<animateTransform attributeName="transform" type="translate"'
        ' values="20,0;-120,0" dur="1s" repeatCount="indefinite"/>'
        '</line>'
        '<line x1="100" y1="55" x2="70" y2="55" stroke="#CBD5E1" stroke-width="2"'
        ' stroke-linecap="round" opacity="0">'
        '<animate attributeName="opacity" values="0;1;0" dur="0.8s" repeatCount="indefinite" begin="0.3s"/>'
        '<animateTransform attributeName="transform" type="translate"'
        ' values="20,0;-120,0" dur="0.8s" repeatCount="indefinite" begin="0.3s"/>'
        '</line>'
        # Truck body (bounces)
        '<g>'
        '<animateTransform attributeName="transform" type="translate"'
        ' values="0,0;0,-3;0,0" dur="0.5s" repeatCount="indefinite"/>'
        '<path d="M10 25 h45 v45 h-45 z" fill="#065E8B"/>'
        '<path d="M15 35 h35 v5 h-35 z" fill="#FFF" opacity="0.3"/>'
        '<path d="M15 45 h35 v5 h-35 z" fill="#FFF" opacity="0.3"/>'
        '<path d="M55 40 h20 c5,0 10,5 10,10 v20 h-30 z" fill="#F59E0B"/>'
        '<path d="M60 45 h10 c2,0 5,2 5,6 v7 h-15 z" fill="#E2E8F0"/>'
        '</g>'
        # Back wheel
        '<circle cx="30" cy="70" r="10" fill="#1E293B"/>'
        '<g>'
        '<animateTransform attributeName="transform" type="rotate"'
        ' from="0 30 70" to="360 30 70" dur="0.8s" repeatCount="indefinite"/>'
        '<line x1="20" y1="70" x2="40" y2="70" stroke="#CBD5E1" stroke-width="1.5"/>'
        '<line x1="30" y1="60" x2="30" y2="80" stroke="#CBD5E1" stroke-width="1.5"/>'
        '</g>'
        '<circle cx="30" cy="70" r="4" fill="#CBD5E1"/>'
        # Front wheel
        '<circle cx="70" cy="70" r="10" fill="#1E293B"/>'
        '<g>'
        '<animateTransform attributeName="transform" type="rotate"'
        ' from="0 70 70" to="360 70 70" dur="0.8s" repeatCount="indefinite"/>'
        '<line x1="60" y1="70" x2="80" y2="70" stroke="#CBD5E1" stroke-width="1.5"/>'
        '<line x1="70" y1="60" x2="70" y2="80" stroke="#CBD5E1" stroke-width="1.5"/>'
        '</g>'
        '<circle cx="70" cy="70" r="4" fill="#CBD5E1"/>'
        '</svg>'
    )


def _smil_factory() -> str:
    """Factory with rising smoke and spinning gear."""
    return (
        '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
        # Smoke puff 1
        '<circle cx="30" cy="40" r="6" fill="#E2E8F0" opacity="0">'
        '<animate attributeName="cy" values="40;5" dur="2.5s" repeatCount="indefinite"/>'
        '<animate attributeName="opacity" values="0;0.8;0" dur="2.5s" repeatCount="indefinite"/>'
        '<animate attributeName="r" values="5;9" dur="2.5s" repeatCount="indefinite"/>'
        '</circle>'
        # Smoke puff 2
        '<circle cx="50" cy="30" r="8" fill="#E2E8F0" opacity="0">'
        '<animate attributeName="cy" values="30;-5" dur="2.5s" repeatCount="indefinite" begin="0.8s"/>'
        '<animate attributeName="opacity" values="0;0.8;0" dur="2.5s" repeatCount="indefinite" begin="0.8s"/>'
        '<animate attributeName="r" values="6;11" dur="2.5s" repeatCount="indefinite" begin="0.8s"/>'
        '</circle>'
        # Smoke puff 3
        '<circle cx="70" cy="40" r="5" fill="#E2E8F0" opacity="0">'
        '<animate attributeName="cy" values="40;5" dur="2.5s" repeatCount="indefinite" begin="1.6s"/>'
        '<animate attributeName="opacity" values="0;0.8;0" dur="2.5s" repeatCount="indefinite" begin="1.6s"/>'
        '<animate attributeName="r" values="4;8" dur="2.5s" repeatCount="indefinite" begin="1.6s"/>'
        '</circle>'
        # Chimneys
        '<path d="M25 40 h10 v20 h-10 z" fill="#64748B"/>'
        '<path d="M45 30 h10 v30 h-10 z" fill="#64748B"/>'
        '<path d="M65 40 h10 v20 h-10 z" fill="#64748B"/>'
        # Main building
        '<path d="M10 60 l20,-15 v15 l20,-15 v15 l20,-15 v15 h20 v30 h-80 z" fill="#065E8B"/>'
        # Spinning gear
        '<circle cx="50" cy="75" r="7" stroke="#FFF" stroke-width="2.5"'
        ' stroke-dasharray="4 2" fill="none">'
        '<animateTransform attributeName="transform" type="rotate"'
        ' from="0 50 75" to="360 50 75" dur="4s" repeatCount="indefinite"/>'
        '</circle>'
        '<circle cx="50" cy="75" r="3" fill="#FFF"/>'
        '</svg>'
    )


def _smil_parcel_tracking() -> str:
    """Parcel tracking with radar pulse and bouncing pin."""
    return (
        '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
        # Device frame
        '<rect x="20" y="5" width="60" height="90" rx="10" stroke="#1E293B"'
        ' stroke-width="3" fill="none"/>'
        '<line x1="40" y1="10" x2="60" y2="10" stroke="#1E293B" stroke-width="3"'
        ' stroke-linecap="round"/>'
        # Radar pulse 1
        '<g transform="translate(50,65)">'
        '<circle cx="0" cy="0" r="5" stroke="#065E8B" stroke-width="1.5" fill="none" opacity="0.8">'
        '<animateTransform attributeName="transform" type="scale" from="1" to="4" dur="2s" repeatCount="indefinite"/>'
        '<animate attributeName="opacity" from="0.8" to="0" dur="2s" repeatCount="indefinite"/>'
        '</circle></g>'
        # Radar pulse 2
        '<g transform="translate(50,65)">'
        '<circle cx="0" cy="0" r="5" stroke="#065E8B" stroke-width="1.5" fill="none" opacity="0.8">'
        '<animateTransform attributeName="transform" type="scale" from="1" to="4" dur="2s" repeatCount="indefinite" begin="0.6s"/>'
        '<animate attributeName="opacity" from="0.8" to="0" dur="2s" repeatCount="indefinite" begin="0.6s"/>'
        '</circle></g>'
        # Radar pulse 3
        '<g transform="translate(50,65)">'
        '<circle cx="0" cy="0" r="5" stroke="#065E8B" stroke-width="1.5" fill="none" opacity="0.8">'
        '<animateTransform attributeName="transform" type="scale" from="1" to="4" dur="2s" repeatCount="indefinite" begin="1.2s"/>'
        '<animate attributeName="opacity" from="0.8" to="0" dur="2s" repeatCount="indefinite" begin="1.2s"/>'
        '</circle></g>'
        # Center dot
        '<ellipse cx="50" cy="65" rx="8" ry="4" fill="#065E8B" opacity="0.3"/>'
        # Bouncing map pin
        '<g>'
        '<animateTransform attributeName="transform" type="translate"'
        ' values="0,0;0,-10;0,0" dur="1.5s" repeatCount="indefinite"/>'
        '<path d="M50 25 c-8,0 -15,7 -15,15 c0,11 15,25 15,25 c0,0 15,-14 15,-25 c0,-8 -7,-15 -15,-15 z" fill="#F59E0B"/>'
        '<circle cx="50" cy="40" r="5" fill="#FFF"/>'
        '</g>'
        '</svg>'
    )


def _smil_cargo_ship() -> str:
    """Cargo ship with bobbing motion and moving waves."""
    return (
        '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
        # Ship (bobs up/down)
        '<g>'
        '<animateTransform attributeName="transform" type="translate"'
        ' values="0,0;0,-4;0,2;0,0" dur="4s" repeatCount="indefinite"/>'
        # Containers row 1
        '<rect x="25" y="35" width="12" height="12" fill="#065E8B"/>'
        '<rect x="39" y="35" width="12" height="12" fill="#F59E0B"/>'
        '<rect x="53" y="35" width="12" height="12" fill="#10B981"/>'
        # Containers row 2
        '<rect x="25" y="49" width="12" height="12" fill="#10B981"/>'
        '<rect x="39" y="49" width="12" height="12" fill="#065E8B"/>'
        '<rect x="53" y="49" width="12" height="12" fill="#F59E0B"/>'
        '<rect x="67" y="49" width="12" height="12" fill="#64748B"/>'
        # Bridge
        '<path d="M75 35 h15 v26 h-15 z" fill="#E2E8F0"/>'
        '<path d="M80 40 h5 v5 h-5 z" fill="#1E293B"/>'
        # Hull
        '<path d="M10 61 l10,15 h65 l5,-15 z" fill="#1E293B"/>'
        '</g>'
        # Wave 1
        '<path d="M-40 85 q10,-5 20,0 q10,5 20,0 q10,-5 20,0 q10,5 20,0'
        ' q10,-5 20,0 q10,5 20,0 q10,-5 20,0 q10,5 20,0"'
        ' stroke="#065E8B" stroke-width="3" stroke-linecap="round" fill="none">'
        '<animateTransform attributeName="transform" type="translate"'
        ' from="0,0" to="-40,0" dur="3s" repeatCount="indefinite"/>'
        '</path>'
        # Wave 2
        '<path d="M-20 95 q10,-5 20,0 q10,5 20,0 q10,-5 20,0 q10,5 20,0'
        ' q10,-5 20,0 q10,5 20,0 q10,-5 20,0 q10,5 20,0"'
        ' stroke="#10B981" stroke-width="3" stroke-linecap="round" fill="none" opacity="0.6">'
        '<animateTransform attributeName="transform" type="translate"'
        ' from="0,0" to="-40,0" dur="2.5s" repeatCount="indefinite"/>'
        '</path>'
        '</svg>'
    )


def _smil_conveyor() -> str:
    """Robotic conveyor with moving belt, sliding box, and stamping arm."""
    return (
        '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
        # Robot base
        '<path d="M35 15 h30 v10 h-30 z" fill="#1E293B"/>'
        # Robot arm (stamps down via scaleY from top)
        '<g transform="translate(50,25)">'
        '<g>'
        '<animateTransform attributeName="transform" type="scale"'
        ' values="1,1;1,1;1,1.4;1,1;1,1" keyTimes="0;0.45;0.5;0.55;1"'
        ' dur="3s" repeatCount="indefinite"/>'
        '<rect x="-5" y="0" width="10" height="25" fill="#64748B"/>'
        '</g></g>'
        # Moving box
        '<g opacity="0">'
        '<animateTransform attributeName="transform" type="translate"'
        ' values="-40,0;100,0" dur="3s" repeatCount="indefinite"/>'
        '<animate attributeName="opacity" values="0;1;1;0"'
        ' keyTimes="0;0.1;0.9;1" dur="3s" repeatCount="indefinite"/>'
        '<rect x="40" y="55" width="20" height="20" fill="#F59E0B" rx="2"/>'
        '<line x1="40" y1="65" x2="60" y2="65" stroke="#FFF" stroke-width="1.5"'
        ' stroke-dasharray="4 2"/>'
        '</g>'
        # Conveyor belt
        '<path d="M10 80 h80" stroke="#CBD5E1" stroke-width="6" stroke-linecap="round"/>'
        '<path d="M15 80 h70" stroke="#1E293B" stroke-width="2" stroke-dasharray="10 10">'
        '<animate attributeName="stroke-dashoffset" from="0" to="-20"'
        ' dur="1s" repeatCount="indefinite"/>'
        '</path>'
        # Rollers
        '<circle cx="20" cy="85" r="4" fill="#065E8B"/>'
        '<circle cx="50" cy="85" r="4" fill="#065E8B"/>'
        '<circle cx="80" cy="85" r="4" fill="#065E8B"/>'
        '</svg>'
    )


def _smil_global_supply() -> str:
    """Globe with orbiting airplane and animated latitude lines."""
    return (
        '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
        # Earth
        '<circle cx="50" cy="50" r="30" fill="#E0F2FE"/>'
        '<circle cx="50" cy="50" r="30" stroke="#065E8B" stroke-width="2" fill="none"/>'
        # Longitude
        '<ellipse cx="50" cy="50" rx="15" ry="30" stroke="#065E8B" stroke-width="1" fill="none"/>'
        # Latitude lines (animated dash)
        '<ellipse cx="50" cy="50" rx="30" ry="10" stroke="#065E8B" stroke-width="1"'
        ' fill="none" stroke-dasharray="5 5">'
        '<animate attributeName="stroke-dashoffset" from="0" to="50"'
        ' dur="5s" repeatCount="indefinite"/>'
        '</ellipse>'
        '<ellipse cx="50" cy="50" rx="30" ry="20" stroke="#065E8B" stroke-width="1"'
        ' fill="none" stroke-dasharray="5 5">'
        '<animate attributeName="stroke-dashoffset" from="0" to="50"'
        ' dur="5s" repeatCount="indefinite"/>'
        '</ellipse>'
        # Flight path ring
        '<circle cx="50" cy="50" r="45" stroke="#CBD5E1" stroke-width="1.5"'
        ' stroke-dasharray="4 4" fill="none"/>'
        # Orbiting airplane (rotates around globe center, counter-rotates to stay upright)
        '<g>'
        '<animateTransform attributeName="transform" type="rotate"'
        ' from="0 50 50" to="360 50 50" dur="4s" repeatCount="indefinite"/>'
        '<g>'
        '<animateTransform attributeName="transform" type="rotate"'
        ' from="0 50 5" to="-360 50 5" dur="4s" repeatCount="indefinite"/>'
        # Airplane at (50, 5)
        '<path d="M50 5 L55 0 L55 10 Z" fill="#F59E0B"/>'
        '<path d="M45 5 L40 0 L40 10 Z" fill="#F59E0B"/>'
        '<ellipse cx="50" cy="5" rx="2" ry="7" fill="#1E293B"/>'
        '<path d="M48 10 L52 10 L50 14 Z" fill="#F59E0B"/>'
        '</g></g>'
        '</svg>'
    )


# Map theme → SMIL SVG function
_THEME_SVGS = {
    "snowflake":  _smil_global_supply,   # Querying global database
    "stock":      _smil_conveyor,         # Inventory / warehouse
    "comex":      _smil_cargo_ship,       # Imports / transit
    "proyeccion": _smil_factory,          # Processing projection
    "export":     _smil_delivery_truck,   # Delivering the file
    "general":    _smil_parcel_tracking,  # Generic tracking
}


@contextmanager
def lottie_spinner(theme: str = "general", height: int = 200):
    """Context manager that shows a fullscreen blur overlay with SVG animation.

    Renders a themed SVG supply-chain animation as an <img> data URI inside
    a fixed overlay div. Uses SMIL animations (which work in <img> tags)
    since st.markdown sanitizes raw <svg> elements.

    Available themes: snowflake, stock, comex, proyeccion, export, general.

    Usage:
        with lottie_spinner("snowflake"):
            data = pd.read_sql(query, conn)

    Falls back to st.spinner if markup injection fails.
    """
    _, message = LOADER_THEMES.get(theme, LOADER_THEMES["general"])
    svg_fn = _THEME_SVGS.get(theme, _THEME_SVGS["general"])
    img_tag = _svg_to_img(svg_fn(), size=120)

    _use_overlay = True
    try:
        overlay_id = f"_dorel_ov_{theme}_{id(message)}"
        placeholder = st.empty()
        with placeholder.container():
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
                    padding: 40px 56px 32px;
                    box-shadow: 0 16px 48px rgba(0,0,0,0.12);
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    gap: 4px;
                }}
                #{overlay_id} .anim-box {{
                    width: 120px; height: 120px;
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
                @keyframes _drl_dot {{
                    0%, 80%, 100% {{ transform: scale(0.4); opacity: 0.3; }}
                    40%           {{ transform: scale(1.0); opacity: 1; }}
                }}
                </style>
                <div id="{overlay_id}">
                    <div class="card">
                        <div class="anim-box">{img_tag}</div>
                        <div class="dots">
                            <span></span><span></span><span></span>
                        </div>
                        <div class="msg">{message}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    except Exception:
        _use_overlay = False

    if _use_overlay:
        try:
            yield
        finally:
            placeholder.empty()
    else:
        # Fallback to standard spinner
        with st.spinner(message):
            yield


class StepProgress:
    """Animated multi-step progress display with SVG animation.

    Shows a themed SVG animation with a step indicator, progress bar,
    and step-by-step text updates. Designed for multi-query download flows.

    Usage:
        sp = StepProgress("stock", steps=["Metricas", "Detalle", "Ventas"])
        sp.advance("Descargando Metricas...")
        data1 = pd.read_sql(query1, conn)
        sp.advance("Descargando Detalle...")
        data2 = pd.read_sql(query2, conn)
        sp.advance("Descargando Ventas...")
        data3 = pd.read_sql(query3, conn)
        sp.finish("Datos Actualizados!")
    """

    def __init__(self, theme: str = "stock", steps: list[str] | None = None,
                 title: str = "Descargando datos..."):
        self._theme = theme
        self._steps = steps or []
        self._title = title
        self._total = len(self._steps) if self._steps else 1
        self._current = 0
        self._placeholder = st.empty()
        svg_fn = _THEME_SVGS.get(theme, _THEME_SVGS["general"])
        self._img_tag = _svg_to_img(svg_fn(), size=110)
        self._uid = f"_drl_sp_{theme}_{id(self)}"
        self._render("Iniciando...")

    def _render(self, message: str):
        pct = min(int((self._current / self._total) * 100), 100)
        # Build step items
        steps_html = ""
        for i, step_name in enumerate(self._steps):
            if i < self._current:
                icon = "✅"
                color = "#10B981"
                weight = "400"
                opacity = "0.7"
            elif i == self._current:
                icon = "⏳"
                color = "#065E8B"
                weight = "600"
                opacity = "1"
            else:
                icon = "⬜"
                color = "#94A3B8"
                weight = "400"
                opacity = "0.5"
            steps_html += (
                f'<div style="display:flex;align-items:center;gap:8px;'
                f'opacity:{opacity};padding:3px 0;">'
                f'<span style="font-size:0.9rem;">{icon}</span>'
                f'<span style="font-size:0.85rem;color:{color};'
                f'font-weight:{weight};">{step_name}</span>'
                f'</div>'
            )
        with self._placeholder.container():
            st.markdown(
                f"""
                <style>
                #{self._uid} {{
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
                #{self._uid} .card {{
                    background: rgba(255, 255, 255, 0.95);
                    border-radius: 24px;
                    padding: 36px 48px 28px;
                    box-shadow: 0 16px 48px rgba(0,0,0,0.12);
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    gap: 8px;
                    min-width: 320px;
                }}
                #{self._uid} .anim-box {{
                    width: 110px; height: 110px;
                }}
                #{self._uid} .progress-track {{
                    width: 100%;
                    height: 8px;
                    background: #E2E8F0;
                    border-radius: 4px;
                    overflow: hidden;
                    margin: 8px 0 4px;
                }}
                #{self._uid} .progress-fill {{
                    height: 100%;
                    border-radius: 4px;
                    background: linear-gradient(90deg, #065E8B, #10B981);
                    transition: width 0.6s ease;
                    width: {pct}%;
                }}
                #{self._uid} .msg {{
                    color: #4a4a4a; font-size: 1rem;
                    font-weight: 600; letter-spacing: 0.3px;
                }}
                #{self._uid} .pct {{
                    color: #94A3B8; font-size: 0.8rem;
                    font-weight: 500;
                }}
                #{self._uid} .steps {{
                    display: flex;
                    flex-direction: column;
                    width: 100%;
                    margin-top: 4px;
                    padding: 8px 12px;
                    background: #F8FAFC;
                    border-radius: 12px;
                }}
                @keyframes _drl_fade {{
                    from {{ opacity: 0; }} to {{ opacity: 1; }}
                }}
                </style>
                <div id="{self._uid}">
                    <div class="card">
                        <div class="anim-box">{self._img_tag}</div>
                        <div class="msg">{message}</div>
                        <div class="progress-track">
                            <div class="progress-fill"></div>
                        </div>
                        <div class="pct">{pct}%</div>
                        <div class="steps">{steps_html}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    def advance(self, message: str = ""):
        """Advance to the next step and update the display."""
        msg = message or (
            f"Paso {self._current + 1}/{self._total}: {self._steps[self._current]}"
            if self._current < len(self._steps) else "Procesando..."
        )
        self._render(msg)
        self._current += 1

    def update(self, message: str):
        """Update the message text without advancing the step counter."""
        self._render(message)

    def finish(self, message: str = "Listo!"):
        """Mark all steps complete and show finish state, then clear."""
        self._current = self._total
        self._render(message)
        import time
        time.sleep(0.6)
        self._placeholder.empty()

    def clear(self):
        """Clear the overlay without finish animation."""
        self._placeholder.empty()


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
