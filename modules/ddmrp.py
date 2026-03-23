"""DDMRP — Demand Driven MRP for store replenishment (Peru).

Calculates dynamic buffer zones (Red/Yellow/Green) per SKU x Store,
evaluates Net Flow Position, and generates replenishment proposals
when stock falls below the Top of Yellow threshold.

Buffer zones:
    Red   = MIN_INV_REQUERIDO (minimum exhibition / safety stock)
    Yellow = ADU_censurado x Lead Time
    Green  = ADU_censurado x Order Cycle
    TOG   = Red + Yellow + Green  (Top of Green)
    TOY   = Red + Yellow          (Top of Yellow = reorder point)

ADU censurado = UNIDADES_90D / DIAS_CON_STOCK_90D
    Excludes days when the store had no stock, so we measure true
    demand capacity rather than suppressed demand.
"""

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, dorel_layout, apply_pm_filter
from db.cache import cached_query as cq
from modules.redistribucion import _parse_transito
from utils.filters import norm_cols, human_format
from utils.export import download_buttons

# ── Constants ─────────────────────────────────────────────────────────────
TC_USD_PEN = 3.80
_CALENDAR_PATH = Path(__file__).resolve().parent.parent / "data" / "store_calendar.json"

STATUS_COLORS = {
    "CRITICO": "#E53935",
    "REORDER": "#FB8C00",
    "OK": "#43A047",
    "EXCESO": "#2196F3",
}

STATUS_ORDER = ["CRITICO", "REORDER", "OK", "EXCESO"]

# ── Calendar persistence ──────────────────────────────────────────────────

def _load_calendar() -> pd.DataFrame:
    """Load store calendar from JSON. Returns empty DataFrame if missing."""
    if not _CALENDAR_PATH.exists():
        return pd.DataFrame()
    try:
        with open(_CALENDAR_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        stores = data.get("stores", {})
        if not stores:
            return pd.DataFrame()
        rows = []
        for sid, cfg in stores.items():
            rows.append({
                "ID_SUCURSAL": str(sid),
                "LUNES": int(cfg.get("lunes", 0)),
                "MARTES": int(cfg.get("martes", 0)),
                "MIERCOLES": int(cfg.get("miercoles", 0)),
                "JUEVES": int(cfg.get("jueves", 0)),
                "VIERNES": int(cfg.get("viernes", 0)),
                "SABADO": int(cfg.get("sabado", 0)),
                "DOMINGO": int(cfg.get("domingo", 0)),
                "SEM_REVISION": int(cfg.get("sem_revision", 7)),
                "LT_PROM": int(cfg.get("lt_prom", 2)),
            })
        return pd.DataFrame(rows)
    except (json.JSONDecodeError, OSError):
        return pd.DataFrame()


def _save_calendar(df: pd.DataFrame) -> None:
    """Save calendar DataFrame to JSON."""
    _CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    stores = {}
    for _, row in df.iterrows():
        sid = str(row["ID_SUCURSAL"])
        stores[sid] = {
            "lunes": int(row.get("LUNES", 0)),
            "martes": int(row.get("MARTES", 0)),
            "miercoles": int(row.get("MIERCOLES", 0)),
            "jueves": int(row.get("JUEVES", 0)),
            "viernes": int(row.get("VIERNES", 0)),
            "sabado": int(row.get("SABADO", 0)),
            "domingo": int(row.get("DOMINGO", 0)),
            "sem_revision": int(row.get("SEM_REVISION", 7)),
            "lt_prom": int(row.get("LT_PROM", 2)),
        }
    data = {
        "version": "1.0",
        "updated_at": pd.Timestamp.now().isoformat(),
        "stores": stores,
    }
    with open(_CALENDAR_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _init_calendar_from_stores(df_tiendas: pd.DataFrame) -> pd.DataFrame:
    """Generate default calendar from store dimension table."""
    if df_tiendas.empty:
        return pd.DataFrame()
    ids = df_tiendas["ID_SUCURSAL"].dropna().unique()
    rows = []
    for sid in sorted(ids):
        rows.append({
            "ID_SUCURSAL": str(sid),
            "LUNES": 0, "MARTES": 0, "MIERCOLES": 1,
            "JUEVES": 0, "VIERNES": 0, "SABADO": 0, "DOMINGO": 0,
            "SEM_REVISION": 7, "LT_PROM": 2,
        })
    df = pd.DataFrame(rows)
    _save_calendar(df)
    return df


# ── DDMRP Engine ──────────────────────────────────────────────────────────

def _compute_adu_progresivo(df_ventas_90d, df_ventas_sem, df_dias_stock):
    """Compute ADU (Average Daily Usage) with progressive window expansion.

    Strategy:
    1. Start with ventas_90d_sucursal (90 days, reliable join via cod_ccosto)
       ADU = UNIDADES_90D / max(DIAS_CON_STOCK, 90)  [censored if stock data available]
    2. If ADU still 0, try weekly sales data (12m) expanding 12w→24w→36w→48w

    Returns DataFrame with SKU_PRODUCTO, ID_SUCURSAL, ADU, VENTANA_SEMANAS,
                          UNIDADES_VENTANA, DIAS_CON_STOCK
    """
    parts = []

    # ── Primary: ventas_90d_sucursal (most reliable join) ──
    if df_ventas_90d is not None and not df_ventas_90d.empty:
        v90 = df_ventas_90d.copy()
        for c in ["SKU_PRODUCTO", "ID_SUCURSAL"]:
            if c in v90.columns:
                v90[c] = v90[c].astype(str).str.strip()
        v90["UNIDADES_90D"] = pd.to_numeric(v90.get("UNIDADES_90D", 0), errors="coerce").fillna(0)

        # Filter to tienda only
        if "CANAL_DE_DISTRIBUCION" in v90.columns:
            v90 = v90[v90["CANAL_DE_DISTRIBUCION"].astype(str).str.upper() == "TIENDA"]

        v90_agg = v90.groupby(["SKU_PRODUCTO", "ID_SUCURSAL"], as_index=False).agg(
            UNIDADES_VENTANA=("UNIDADES_90D", "sum"),
        )

        # Merge dias_con_stock for censored calculation
        dcs = df_dias_stock.copy() if df_dias_stock is not None else pd.DataFrame()
        if not dcs.empty:
            for c in ["SKU_PRODUCTO", "ID_SUCURSAL"]:
                if c in dcs.columns:
                    dcs[c] = dcs[c].astype(str).str.strip()
            if "DIAS_CON_STOCK" in dcs.columns:
                dcs["DIAS_CON_STOCK"] = pd.to_numeric(dcs["DIAS_CON_STOCK"], errors="coerce").fillna(0)
                v90_agg = v90_agg.merge(
                    dcs[["SKU_PRODUCTO", "ID_SUCURSAL", "DIAS_CON_STOCK"]],
                    on=["SKU_PRODUCTO", "ID_SUCURSAL"], how="left",
                )
        if "DIAS_CON_STOCK" not in v90_agg.columns:
            v90_agg["DIAS_CON_STOCK"] = 0
        v90_agg["DIAS_CON_STOCK"] = v90_agg["DIAS_CON_STOCK"].fillna(0)

        # ADU = unidades / max(dias_con_stock, 90)
        # Use censored if we have stock-days data; otherwise full 90 days
        v90_agg["DIAS_EFECTIVOS"] = np.where(
            v90_agg["DIAS_CON_STOCK"] > 0,
            v90_agg["DIAS_CON_STOCK"],
            90,
        ).clip(min=1)
        v90_agg["ADU"] = (v90_agg["UNIDADES_VENTANA"] / v90_agg["DIAS_EFECTIVOS"]).round(3)
        v90_agg["VENTANA_SEMANAS"] = 12  # ~90 days
        v90_agg = v90_agg[v90_agg["UNIDADES_VENTANA"] > 0]
        parts.append(v90_agg[["SKU_PRODUCTO", "ID_SUCURSAL", "ADU",
                              "VENTANA_SEMANAS", "UNIDADES_VENTANA", "DIAS_CON_STOCK"]])

    # ── Fallback: weekly sales 12m with progressive expansion ──
    found_keys = set()
    if parts:
        _p = parts[0]
        found_keys = set(zip(_p["SKU_PRODUCTO"], _p["ID_SUCURSAL"]))

    if df_ventas_sem is not None and not df_ventas_sem.empty:
        vs = df_ventas_sem.copy()
        for c in ["SKU_PRODUCTO", "ID_SUCURSAL"]:
            if c in vs.columns:
                vs[c] = vs[c].astype(str).str.strip()
        vs["SEMANA"] = pd.to_datetime(vs.get("SEMANA"), errors="coerce")
        vs["UNIDADES"] = pd.to_numeric(vs.get("UNIDADES", 0), errors="coerce").fillna(0)

        hoy = pd.Timestamp.today().normalize()
        all_combos = vs[["SKU_PRODUCTO", "ID_SUCURSAL"]].drop_duplicates()

        # Remove already-found combos
        if found_keys:
            mask = all_combos.apply(
                lambda r: (r["SKU_PRODUCTO"], r["ID_SUCURSAL"]) not in found_keys, axis=1
            )
            remaining = all_combos[mask]
        else:
            remaining = all_combos

        for n_weeks in [12, 24, 36, 48]:
            if remaining.empty:
                break
            cutoff = hoy - pd.Timedelta(weeks=n_weeks)
            vs_w = vs[vs["SEMANA"] >= cutoff]
            agg = vs_w.groupby(["SKU_PRODUCTO", "ID_SUCURSAL"], as_index=False).agg(
                UNIDADES_VENTANA=("UNIDADES", "sum"),
            )
            agg = agg[agg["UNIDADES_VENTANA"] > 0]
            found = remaining.merge(agg, on=["SKU_PRODUCTO", "ID_SUCURSAL"], how="inner")
            if not found.empty:
                found["VENTANA_SEMANAS"] = n_weeks
                found["DIAS_CON_STOCK"] = 0
                found["ADU"] = (found["UNIDADES_VENTANA"] / (n_weeks * 7)).round(3)
                parts.append(found[["SKU_PRODUCTO", "ID_SUCURSAL", "ADU",
                                    "VENTANA_SEMANAS", "UNIDADES_VENTANA", "DIAS_CON_STOCK"]])
                remaining = remaining.merge(
                    found[["SKU_PRODUCTO", "ID_SUCURSAL"]],
                    on=["SKU_PRODUCTO", "ID_SUCURSAL"], how="left", indicator=True,
                )
                remaining = remaining[remaining["_merge"] == "left_only"].drop(columns=["_merge"])

    if not parts:
        return pd.DataFrame(columns=[
            "SKU_PRODUCTO", "ID_SUCURSAL", "ADU", "VENTANA_SEMANAS",
            "UNIDADES_VENTANA", "DIAS_CON_STOCK",
        ])

    return pd.concat(parts, ignore_index=True)


def _compute_ddmrp_buffers(
    df_stock, df_config, df_adu, df_transito, df_calendar,
    df_maestra, df_abc=None,
):
    """Compute DDMRP buffer zones and Net Flow Position for all SKU x Store combos.

    Parameters
    ----------
    df_adu : DataFrame from _compute_adu_progresivo() with ADU, VENTANA_SEMANAS, etc.

    Returns DataFrame with one row per SKU x Store.
    """
    # ── 1. Prepare config (MIN_INV_REQUERIDO, MAX_REPO) ──
    cfg = df_config.copy()
    cfg.columns = [c.upper().strip() for c in cfg.columns]
    sku_col_cfg = "ID_MATERIAL" if "ID_MATERIAL" in cfg.columns else "SKU_PRODUCTO"
    suc_col_cfg = "ID_SUCURSAL" if "ID_SUCURSAL" in cfg.columns else "COD_BODEGA"
    cfg = cfg.rename(columns={sku_col_cfg: "SKU_PRODUCTO", suc_col_cfg: "ID_SUCURSAL"})
    for c in ["SKU_PRODUCTO", "ID_SUCURSAL"]:
        if c in cfg.columns:
            cfg[c] = cfg[c].astype(str).str.strip()
    for c in ["MIN_INV_REQUERIDO", "MAX_REPO"]:
        if c in cfg.columns:
            cfg[c] = pd.to_numeric(cfg[c], errors="coerce").fillna(0)
    if "MIN_INV_REQUERIDO" not in cfg.columns:
        cfg["MIN_INV_REQUERIDO"] = 1
    if "MAX_REPO" not in cfg.columns:
        cfg["MAX_REPO"] = 99

    cfg_dedup = cfg.groupby(["SKU_PRODUCTO", "ID_SUCURSAL"], as_index=False).agg(
        MIN_INV_REQUERIDO=("MIN_INV_REQUERIDO", "max"),
        MAX_REPO=("MAX_REPO", "max"),
    )

    # ── 2. Base = config combos ──
    base = cfg_dedup[["SKU_PRODUCTO", "ID_SUCURSAL", "MIN_INV_REQUERIDO", "MAX_REPO"]].copy()

    # ── 3. Merge stock ──
    stk = df_stock.copy()
    for c in ["SKU_PRODUCTO", "ID_SUCURSAL"]:
        if c in stk.columns:
            stk[c] = stk[c].astype(str).str.strip()
    if "STOCK_UNIDADES" in stk.columns:
        stk["STOCK_UNIDADES"] = pd.to_numeric(stk["STOCK_UNIDADES"], errors="coerce").fillna(0)
    stk_agg = stk.groupby(["SKU_PRODUCTO", "ID_SUCURSAL"], as_index=False).agg(
        ON_HAND=("STOCK_UNIDADES", "sum"),
    ) if "STOCK_UNIDADES" in stk.columns else pd.DataFrame(columns=["SKU_PRODUCTO", "ID_SUCURSAL", "ON_HAND"])

    base = base.merge(stk_agg, on=["SKU_PRODUCTO", "ID_SUCURSAL"], how="left")
    base["ON_HAND"] = base["ON_HAND"].fillna(0)

    # ── 4. Merge ADU (pre-computed with progressive windows) ──
    if df_adu is not None and not df_adu.empty:
        adu_cols = [c for c in ["SKU_PRODUCTO", "ID_SUCURSAL", "ADU", "VENTANA_SEMANAS",
                                "UNIDADES_VENTANA", "DIAS_CON_STOCK"] if c in df_adu.columns]
        base = base.merge(df_adu[adu_cols], on=["SKU_PRODUCTO", "ID_SUCURSAL"], how="left")
    for c in ["ADU", "VENTANA_SEMANAS", "UNIDADES_VENTANA", "DIAS_CON_STOCK"]:
        base[c] = base.get(c, pd.Series(0, index=base.index)).fillna(0)

    # ── 5. Merge transit ──
    tr = df_transito.copy()
    if not tr.empty:
        for c in ["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO"]:
            if c in tr.columns:
                tr[c] = tr[c].astype(str).str.strip()
        tr_agg = tr.groupby(["SKU_PRODUCTO", "ID_SUCURSAL_DESTINO"], as_index=False).agg(
            IN_TRANSIT=("QTY_TRANSITO", "sum"),
        ).rename(columns={"ID_SUCURSAL_DESTINO": "ID_SUCURSAL"})
        base = base.merge(tr_agg, on=["SKU_PRODUCTO", "ID_SUCURSAL"], how="left")
    base["IN_TRANSIT"] = base.get("IN_TRANSIT", pd.Series(0, index=base.index)).fillna(0)

    # ── 6. Merge calendar (LT_PROM, SEM_REVISION) ──
    cal = df_calendar.copy()
    if not cal.empty:
        cal["ID_SUCURSAL"] = cal["ID_SUCURSAL"].astype(str).str.strip()
        cal_cols = ["ID_SUCURSAL", "SEM_REVISION", "LT_PROM"]
        cal_cols = [c for c in cal_cols if c in cal.columns]
        base = base.merge(cal[cal_cols], on="ID_SUCURSAL", how="left")
    base["SEM_REVISION"] = base.get("SEM_REVISION", pd.Series(7, index=base.index)).fillna(7).astype(int)
    base["LT_PROM"] = base.get("LT_PROM", pd.Series(2, index=base.index)).fillna(2).astype(int)

    # ── 7. Merge maestra (dimensions + costo) ──
    mae = df_maestra.copy()
    mae_cols = ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA", "MARCA", "ULTIMO_COSTO"]
    mae_cols = [c for c in mae_cols if c in mae.columns]
    if mae_cols:
        mae_dedup = mae[mae_cols].drop_duplicates(subset=["SKU_PRODUCTO"])
        base = base.merge(mae_dedup, on="SKU_PRODUCTO", how="left")

    # ── 8. Merge ABC-XYZ-FSN class ──
    if df_abc is not None and not df_abc.empty:
        abc_cols = ["SKU_PRODUCTO", "CLASE_ABC", "CLASE_XYZ", "CLASE_FSN", "CLASE_COMBINADA"]
        abc_cols = [c for c in abc_cols if c in df_abc.columns]
        if abc_cols:
            base = base.merge(df_abc[abc_cols].drop_duplicates("SKU_PRODUCTO"),
                              on="SKU_PRODUCTO", how="left")

    # ── 9. Compute DDMRP buffers ──
    # ADU already comes pre-computed from _compute_adu_progresivo()
    # Buffer zones
    base["RED_ZONE"] = base["MIN_INV_REQUERIDO"]
    base["YELLOW_ZONE"] = (base["ADU"] * base["LT_PROM"]).round(1)
    base["GREEN_ZONE"] = np.maximum(base["ADU"] * base["SEM_REVISION"], 1).round(1)
    base["TOG"] = (base["RED_ZONE"] + base["YELLOW_ZONE"] + base["GREEN_ZONE"]).round(1)
    base["TOY"] = (base["RED_ZONE"] + base["YELLOW_ZONE"]).round(1)

    # Net Flow Position
    base["NFP"] = base["ON_HAND"] + base["IN_TRANSIT"]

    # Status
    base["STATUS"] = np.select(
        [
            base["NFP"] <= base["RED_ZONE"],
            base["NFP"] <= base["TOY"],
            base["NFP"] <= base["TOG"],
        ],
        ["CRITICO", "REORDER", "OK"],
        default="EXCESO",
    )

    # Order quantity
    base["ORDER_QTY"] = np.where(
        base["NFP"] < base["TOY"],
        np.ceil(np.maximum(base["TOG"] - base["NFP"], 0)),
        0,
    ).astype(int)

    # Financial
    base["ULTIMO_COSTO"] = pd.to_numeric(base.get("ULTIMO_COSTO", 0), errors="coerce").fillna(0)
    base["COSTO_PEN"] = (base["ORDER_QTY"] * base["ULTIMO_COSTO"]).round(2)
    base["COSTO_USD"] = (base["COSTO_PEN"] / TC_USD_PEN).round(2)

    # Store description
    if "DESCRIPCION_SUCURSAL" not in base.columns:
        stk_names = stk[["ID_SUCURSAL", "DESCRIPCION_SUCURSAL"]].drop_duplicates() \
            if "DESCRIPCION_SUCURSAL" in stk.columns else pd.DataFrame()
        if not stk_names.empty:
            base = base.merge(stk_names, on="ID_SUCURSAL", how="left")

    return base


# ── UI Helpers ────────────────────────────────────────────────────────────

def _render_dashboard(df):
    """Tab 1: Dashboard KPIs and charts."""
    total = len(df)
    n_critico = (df["STATUS"] == "CRITICO").sum()
    n_reorder = (df["STATUS"] == "REORDER").sum()
    pct_critico = n_critico / max(total, 1) * 100
    pct_reorder = n_reorder / max(total, 1) * 100
    total_order_pen = df["COSTO_PEN"].sum()

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Combos SKU×Tienda", f"{total:,}")
    k2.metric("% Critico", f"{pct_critico:.1f}%", delta=f"{n_critico:,} combos")
    k3.metric("% Reorder", f"{pct_reorder:.1f}%", delta=f"{n_reorder:,} combos")
    k4.metric("Valor Orden (PEN)", f"S/{human_format(total_order_pen)}")

    st.html("<br>")

    col1, col2 = st.columns(2)

    # Pie chart
    with col1:
        status_grp = df.groupby("STATUS", as_index=False).size().rename(columns={"size": "COUNT"})
        fig_pie = go.Figure(go.Pie(
            labels=status_grp["STATUS"],
            values=status_grp["COUNT"],
            marker=dict(colors=[STATUS_COLORS.get(s, "#999") for s in status_grp["STATUS"]]),
            textinfo="label+percent+value",
            hole=0.4,
        ))
        fig_pie.update_layout(**dorel_layout(title="Distribucion por Status", height=350))
        st.plotly_chart(fig_pie, use_container_width=True)

    # Stacked bar by AREA
    with col2:
        if "AREA" in df.columns:
            area_grp = df.groupby(["AREA", "STATUS"], as_index=False).size().rename(columns={"size": "COUNT"})
            fig_bar = go.Figure()
            for status in STATUS_ORDER:
                sub = area_grp[area_grp["STATUS"] == status]
                if sub.empty:
                    continue
                fig_bar.add_trace(go.Bar(
                    x=sub["AREA"], y=sub["COUNT"],
                    name=status, marker_color=STATUS_COLORS.get(status, "#999"),
                ))
            fig_bar.update_layout(**dorel_layout(
                title="Status por Area", height=350, barmode="stack",
            ))
            st.plotly_chart(fig_bar, use_container_width=True)


def _render_calendar(conn, df_calendar):
    """Tab 2: Editable store calendar."""
    st.markdown("#### Calendario de Abastecimiento por Tienda")
    st.caption(
        "Define los dias de entrega, order cycle (Sem Revision) y lead time "
        "promedio para cada tienda. Los cambios se guardan localmente."
    )

    if df_calendar.empty:
        st.info("Sin calendario. Haga clic en 'Generar desde Syncro' para crear uno.")
        if st.button("Generar desde Syncro", key="ddmrp_gen_cal"):
            df_tiendas = norm_cols(cq.ddmrp_stock_tienda(conn))
            df_calendar = _init_calendar_from_stores(df_tiendas)
            st.rerun()
        return df_calendar

    # Merge store names for display
    df_edit = df_calendar.copy()

    edited = st.data_editor(
        df_edit,
        column_config={
            "ID_SUCURSAL": st.column_config.TextColumn("Sucursal", disabled=True),
            "LUNES": st.column_config.NumberColumn("Lun", min_value=0, max_value=1, step=1),
            "MARTES": st.column_config.NumberColumn("Mar", min_value=0, max_value=1, step=1),
            "MIERCOLES": st.column_config.NumberColumn("Mie", min_value=0, max_value=1, step=1),
            "JUEVES": st.column_config.NumberColumn("Jue", min_value=0, max_value=1, step=1),
            "VIERNES": st.column_config.NumberColumn("Vie", min_value=0, max_value=1, step=1),
            "SABADO": st.column_config.NumberColumn("Sab", min_value=0, max_value=1, step=1),
            "DOMINGO": st.column_config.NumberColumn("Dom", min_value=0, max_value=1, step=1),
            "SEM_REVISION": st.column_config.NumberColumn("Sem Revision", min_value=1, max_value=30, step=1),
            "LT_PROM": st.column_config.NumberColumn("LT Prom", min_value=1, max_value=30, step=1),
        },
        use_container_width=True,
        height=min(len(df_edit) * 38 + 60, 600),
        num_rows="dynamic",
        key="ddmrp_calendar_editor",
    )

    c1, c2 = st.columns(2)
    with c1:
        if st.button("💾 Guardar Cambios", key="ddmrp_save_cal", type="primary"):
            _save_calendar(edited)
            st.success("Calendario guardado correctamente.")
            st.rerun()
    with c2:
        if st.button("🔄 Restaurar Defaults", key="ddmrp_reset_cal"):
            df_tiendas = norm_cols(cq.ddmrp_stock_tienda(conn))
            _init_calendar_from_stores(df_tiendas)
            st.success("Calendario restaurado con defaults.")
            st.rerun()

    return edited


def _render_buffers(df):
    """Tab 3: Buffer zones table with drill-down visualization."""
    st.markdown("#### Buffers DDMRP por SKU × Tienda")

    display_cols = [
        "SKU_PRODUCTO", "ID_SUCURSAL", "DESCRIPCION_SUCURSAL",
        "AREA", "LINEA", "MARCA",
        "CLASE_ABC", "CLASE_XYZ", "CLASE_COMBINADA",
        "ADU", "VENTANA_SEMANAS", "LT_PROM", "SEM_REVISION",
        "RED_ZONE", "YELLOW_ZONE", "GREEN_ZONE", "TOG", "TOY",
        "ON_HAND", "IN_TRANSIT", "NFP", "STATUS",
        "ORDER_QTY", "COSTO_PEN",
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    st.dataframe(
        df[display_cols].sort_values(
            ["STATUS", "NFP"],
            key=lambda s: s.map({"CRITICO": 0, "REORDER": 1, "OK": 2, "EXCESO": 3}) if s.name == "STATUS" else s,
        ),
        use_container_width=True, height=500,
        column_config={
            "ADU": st.column_config.NumberColumn(format="%.2f"),
            "COSTO_PEN": st.column_config.NumberColumn(format="S/%,.0f"),
            "RED_ZONE": st.column_config.NumberColumn(format="%.0f"),
            "YELLOW_ZONE": st.column_config.NumberColumn(format="%.1f"),
            "GREEN_ZONE": st.column_config.NumberColumn(format="%.1f"),
        },
    )

    # Drill-down
    st.markdown("---")
    st.markdown("**Visualizacion de Buffer Individual**")
    skus_avail = sorted(df["SKU_PRODUCTO"].unique())
    if not skus_avail:
        return
    sel_sku = st.selectbox("SKU", skus_avail, key="ddmrp_drill_sku")
    df_sku = df[df["SKU_PRODUCTO"] == sel_sku].sort_values("STATUS")

    if df_sku.empty:
        return

    # Horizontal stacked bar: Red | Yellow | Green with NFP marker
    fig = go.Figure()
    labels = df_sku["ID_SUCURSAL"].astype(str)
    if "DESCRIPCION_SUCURSAL" in df_sku.columns:
        labels = df_sku["ID_SUCURSAL"].astype(str) + " - " + df_sku["DESCRIPCION_SUCURSAL"].fillna("")

    fig.add_trace(go.Bar(
        y=labels, x=df_sku["RED_ZONE"], name="Red (Min Exhib)",
        orientation="h", marker_color="#E53935",
    ))
    fig.add_trace(go.Bar(
        y=labels, x=df_sku["YELLOW_ZONE"], name="Yellow (LT demand)",
        orientation="h", marker_color="#FFC107",
    ))
    fig.add_trace(go.Bar(
        y=labels, x=df_sku["GREEN_ZONE"], name="Green (OC demand)",
        orientation="h", marker_color="#43A047",
    ))
    # NFP marker
    fig.add_trace(go.Scatter(
        y=labels, x=df_sku["NFP"], mode="markers+text",
        name="NFP (On-Hand + Transit)",
        marker=dict(color="#1A237E", size=12, symbol="diamond"),
        text=df_sku["NFP"].astype(int).astype(str),
        textposition="middle right",
    ))

    fig.update_layout(**dorel_layout(
        title=f"Buffer DDMRP — {sel_sku}",
        barmode="stack", height=max(len(df_sku) * 40, 200),
        xaxis=dict(title="Unidades"),
        yaxis=dict(title=""),
    ))
    st.plotly_chart(fig, use_container_width=True)


def _render_proposals(df):
    """Tab 4: Replenishment proposals (CRITICO + REORDER only)."""
    st.markdown("#### Propuestas de Reposicion")

    df_prop = df[df["STATUS"].isin(["CRITICO", "REORDER"])].copy()
    if df_prop.empty:
        st.success("No hay SKUs que requieran reposicion. Todos los buffers estan OK.")
        return

    # Sort: CRITICO first, then by order value desc
    df_prop["_SORT"] = df_prop["STATUS"].map({"CRITICO": 0, "REORDER": 1}).fillna(2)
    df_prop = df_prop.sort_values(["_SORT", "COSTO_PEN"], ascending=[True, False])

    # Summary
    total_und = df_prop["ORDER_QTY"].sum()
    total_pen = df_prop["COSTO_PEN"].sum()
    total_usd = df_prop["COSTO_USD"].sum()
    n_critico = (df_prop["STATUS"] == "CRITICO").sum()
    n_reorder = (df_prop["STATUS"] == "REORDER").sum()

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Criticos", f"{n_critico:,}")
    k2.metric("Reorder", f"{n_reorder:,}")
    k3.metric("Total Unidades", f"{total_und:,.0f}")
    k4.metric("Valor Total (PEN)", f"S/{human_format(total_pen)}")

    prop_cols = [
        "STATUS", "SKU_PRODUCTO", "ID_SUCURSAL", "DESCRIPCION_SUCURSAL",
        "AREA", "LINEA", "MARCA",
        "CLASE_ABC", "CLASE_XYZ", "CLASE_COMBINADA",
        "ON_HAND", "IN_TRANSIT", "NFP", "TOY", "TOG",
        "ORDER_QTY", "COSTO_PEN", "COSTO_USD",
        "ADU", "VENTANA_SEMANAS", "RED_ZONE", "YELLOW_ZONE", "GREEN_ZONE",
    ]
    prop_cols = [c for c in prop_cols if c in df_prop.columns]

    st.dataframe(
        df_prop[prop_cols],
        use_container_width=True, height=500,
        column_config={
            "COSTO_PEN": st.column_config.NumberColumn(format="S/%,.0f"),
            "COSTO_USD": st.column_config.NumberColumn(format="$%,.0f"),
            "ADU": st.column_config.NumberColumn(format="%.2f"),
        },
    )


def _render_download(df):
    """Tab 5: Full download."""
    st.markdown("#### Descarga Completa DDMRP")
    st.caption(f"{len(df):,} filas — {df['SKU_PRODUCTO'].nunique():,} SKUs — "
               f"{df['ID_SUCURSAL'].nunique():,} tiendas")
    download_buttons(df, "ddmrp_reposicion")


# ── Main Render ───────────────────────────────────────────────────────────

def render_ddmrp(conn):
    """Main entry point for the DDMRP store replenishment module."""
    st.html("<h2 class='sub-header'>DDMRP — Reposicion de Tiendas</h2>")
    st.caption(
        "Demand Driven MRP: calcula buffers Red/Yellow/Green por SKU x Tienda, "
        "evalua Net Flow Position y genera propuestas de reposicion. "
        f"ADU censurado (excluye dias sin stock). TC USD/PEN = {TC_USD_PEN}."
    )

    # ── Load data ──
    with st.spinner("Cargando datos DDMRP..."):
        df_stock = norm_cols(cq.ddmrp_stock_tienda(conn))
        df_config = norm_cols(cq.syncro_config(conn))
        df_ventas_90d = norm_cols(cq.ventas_90d_sucursal(conn))
        try:
            df_ventas_sem = norm_cols(cq.ventas_semanal_sucursal_12m(conn))
        except Exception:
            df_ventas_sem = pd.DataFrame()
        df_dias_stock = norm_cols(cq.ddmrp_dias_con_stock(conn))
        df_transito_raw = cq.transito_sucursales(conn)
        df_transito = _parse_transito(df_transito_raw)
        df_maestra = norm_cols(cq.maestra(conn))
        try:
            df_abc = norm_cols(cq.abc_xyz_fsn(conn))
        except Exception:
            df_abc = pd.DataFrame()

    # Calendar
    df_calendar = _load_calendar()

    # If no calendar exists, init from stores
    if df_calendar.empty and not df_stock.empty:
        df_calendar = _init_calendar_from_stores(df_stock)

    # Apply PM filter to maestra
    df_maestra = apply_pm_filter(df_maestra)

    # ── Compute ADU with progressive windows (12w → 24w → 36w → 48w) ──
    # ── Compute DDMRP ──
    cache_key = "ddmrp_result_v2"
    if cache_key not in st.session_state or st.button("🔄 Recalcular", key="ddmrp_recalc"):
        df_adu = _compute_adu_progresivo(df_ventas_90d, df_ventas_sem, df_dias_stock)
        df_ddmrp = _compute_ddmrp_buffers(
            df_stock, df_config, df_adu,
            df_transito, df_calendar, df_maestra, df_abc,
        )
        st.session_state[cache_key] = df_ddmrp

    df_ddmrp = st.session_state.get(cache_key, pd.DataFrame())

    if df_ddmrp.empty:
        st.warning("No se pudieron calcular buffers DDMRP. Verifique datos de Syncro Config.")
        return

    # ── Filters ──
    with st.expander("Filtros", expanded=True):
        fc1, fc2, fc3, fc4 = st.columns(4)
        with fc1:
            areas = sorted(df_ddmrp["AREA"].dropna().unique()) if "AREA" in df_ddmrp.columns else []
            sel_area = st.multiselect("Area", areas, key="ddmrp_area")
        with fc2:
            _m = df_ddmrp[df_ddmrp["AREA"].isin(sel_area)] if sel_area else df_ddmrp
            lineas = sorted(_m["LINEA"].dropna().unique()) if "LINEA" in _m.columns else []
            sel_linea = st.multiselect("Linea", lineas, key="ddmrp_linea")
        with fc3:
            sel_status = st.multiselect("Status", STATUS_ORDER, key="ddmrp_status")
        with fc4:
            tiendas = sorted(df_ddmrp["ID_SUCURSAL"].dropna().unique())
            sel_tienda = st.multiselect("Tienda", tiendas, key="ddmrp_tienda")

    df_filt = df_ddmrp.copy()
    if sel_area:
        df_filt = df_filt[df_filt["AREA"].isin(sel_area)]
    if sel_linea:
        df_filt = df_filt[df_filt["LINEA"].isin(sel_linea)]
    if sel_status:
        df_filt = df_filt[df_filt["STATUS"].isin(sel_status)]
    if sel_tienda:
        df_filt = df_filt[df_filt["ID_SUCURSAL"].isin(sel_tienda)]

    if df_filt.empty:
        st.info("Sin datos para los filtros seleccionados.")
        return

    # ── Tabs ──
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 Dashboard",
        "📅 Calendario",
        "🔴🟡🟢 Buffers",
        "📋 Propuestas",
        "📥 Descarga",
    ])

    with tab1:
        _render_dashboard(df_filt)
    with tab2:
        df_calendar = _render_calendar(conn, df_calendar)
    with tab3:
        _render_buffers(df_filt)
    with tab4:
        _render_proposals(df_filt)
    with tab5:
        _render_download(df_filt)
