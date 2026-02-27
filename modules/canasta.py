"""
Análisis de Canasta — Co-ocurrencia de productos en transacciones.

Cruza ID_VENTA (boleta) para detectar pares de SKUs que se compran juntos.
Calcula Soporte, Confianza y Lift para cada par.

⚠️ Asociación ≠ causalidad: un lift alto indica co-ocurrencia estadística,
no que un producto cause la compra del otro.
"""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from itertools import combinations
from datetime import date, timedelta

from db.queries import QUERY_CANASTA
from utils.filters import norm_cols, human_format
from utils.export import download_buttons
from config import COLORS, dorel_layout, apply_pm_filter


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_mm(v):
    """Formato millones con 1 decimal."""
    if pd.isna(v) or v == 0:
        return "—"
    if abs(v) >= 1_000_000_000:
        return f"${v / 1_000_000_000:.1f}B"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    return f"${v:,.0f}"


def _fmt_clp(v):
    """Format as Chilean Pesos: $25.450"""
    if pd.isna(v) or v == 0:
        return "$0"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(round(v)):,}".replace(",", ".")


def _fmt_pct_cl(v, decimals=1):
    """Format as Chilean percentage: 67,2%"""
    if pd.isna(v):
        return "—"
    s = f"{v * 100:.{decimals}f}"
    return s.replace(".", ",") + "%"


def _fmt_dec_cl(v, decimals=2):
    """Format decimal with Chilean comma: 2,35"""
    if pd.isna(v):
        return "—"
    return f"{v:.{decimals}f}".replace(".", ",")


def _load_canasta(conn, fecha_ini, fecha_fin):
    """Carga transacciones para análisis de canasta."""
    df = pd.read_sql(QUERY_CANASTA, conn, params=[str(fecha_ini), str(fecha_fin)])
    return norm_cols(df)


def _build_pairs(df, max_skus_basket=30, min_vol_sku=20):
    """
    Construye pares de co-ocurrencia desde transacciones.

    Args:
        df: DataFrame con columnas ID_VENTA, SKU_PRODUCTO (ya filtrado)
        max_skus_basket: Máximo SKUs por canasta (excluir mayorista ruidoso)
        min_vol_sku: Mínimo transacciones totales por SKU para incluirlo

    Returns:
        (df_pairs, n_baskets, sku_freq) — pares con freq, total canastas, freq por SKU
    """
    # Pre-filtro: excluir SKUs con volumen muy bajo
    sku_counts = df.groupby("SKU_PRODUCTO")["ID_VENTA"].nunique()
    valid_skus = set(sku_counts[sku_counts >= min_vol_sku].index)
    df_f = df[df["SKU_PRODUCTO"].isin(valid_skus)]

    # Agrupar por canasta (ID_VENTA) → lista de SKUs únicos
    baskets = df_f.groupby("ID_VENTA")["SKU_PRODUCTO"].apply(
        lambda x: tuple(sorted(x.unique()))
    )

    # Filtrar: solo canastas con ≥2 y ≤max SKUs
    baskets = baskets[baskets.apply(lambda x: 2 <= len(x) <= max_skus_basket)]
    n_baskets = len(baskets)

    if n_baskets == 0:
        return pd.DataFrame(), 0, pd.Series(dtype=float)

    # Frecuencia individual de cada SKU en canastas multi-item
    all_skus_in_baskets = []
    for skus in baskets:
        all_skus_in_baskets.extend(skus)
    sku_freq = pd.Series(all_skus_in_baskets).value_counts()

    # Generar pares
    pairs = []
    for skus in baskets:
        for a, b in combinations(skus, 2):
            pairs.append((a, b))

    if not pairs:
        return pd.DataFrame(), n_baskets, sku_freq

    df_pairs = (
        pd.DataFrame(pairs, columns=["SKU_A", "SKU_B"])
        .groupby(["SKU_A", "SKU_B"])
        .size()
        .reset_index(name="FREQ")
    )

    return df_pairs, n_baskets, sku_freq


def _calc_metrics(df_pairs, n_baskets, sku_freq):
    """
    Calcula Soporte, Confianza y Lift para cada par.

    - Soporte = Freq(A,B) / N_baskets
    - Confianza A→B = Freq(A,B) / Freq(A)
    - Lift = Confianza(A→B) / (Freq(B) / N_baskets)
    """
    if df_pairs.empty or n_baskets == 0:
        return df_pairs

    df = df_pairs.copy()
    df["FREQ_A"] = df["SKU_A"].map(sku_freq).fillna(0)
    df["FREQ_B"] = df["SKU_B"].map(sku_freq).fillna(0)

    df["SOPORTE"] = df["FREQ"] / n_baskets
    df["CONFIANZA_AB"] = np.where(df["FREQ_A"] > 0, df["FREQ"] / df["FREQ_A"], 0)
    df["CONFIANZA_BA"] = np.where(df["FREQ_B"] > 0, df["FREQ"] / df["FREQ_B"], 0)

    soporte_b = np.where(n_baskets > 0, df["FREQ_B"] / n_baskets, 0)
    df["LIFT"] = np.where(soporte_b > 0, df["CONFIANZA_AB"] / soporte_b, 0)

    return df.sort_values("FREQ", ascending=False)


def _enrich_pairs(df_pairs, df_txn):
    """Agrega nombres y dimensiones a los pares."""
    dim_cols = ["SKU_PRODUCTO"]
    for c in ["SKU_NOM_PRODUCTO", "AREA", "LINEA", "MARCA"]:
        if c in df_txn.columns:
            dim_cols.append(c)
    dims = df_txn[dim_cols].drop_duplicates("SKU_PRODUCTO")

    # Merge para SKU_A
    dims_a = dims.rename(columns={c: f"{c}_A" if c != "SKU_PRODUCTO" else "SKU_A" for c in dims.columns})
    df = df_pairs.merge(dims_a, on="SKU_A", how="left")

    # Merge para SKU_B
    dims_b = dims.rename(columns={c: f"{c}_B" if c != "SKU_PRODUCTO" else "SKU_B" for c in dims.columns})
    df = df.merge(dims_b, on="SKU_B", how="left")

    return df


# ─── Pack Pricing & Suggestions ──────────────────────────────────────────────

def _calc_sku_pricing(df_txn):
    """Compute weighted average price and margin per SKU from transactions.

    Returns DataFrame with columns:
        SKU_PRODUCTO, PRECIO_PROM, MARGEN_PCT, APORTE_UNIT
    """
    if df_txn.empty:
        return pd.DataFrame(columns=[
            "SKU_PRODUCTO", "PRECIO_PROM", "MARGEN_PCT", "APORTE_UNIT",
        ])

    t = df_txn.copy()
    for c in ["CANTIDAD", "NETO", "APORTE"]:
        if c in t.columns:
            t[c] = pd.to_numeric(t[c], errors="coerce").fillna(0)

    agg = t.groupby("SKU_PRODUCTO", as_index=False).agg(
        NETO_TOTAL=("NETO", "sum"),
        APORTE_TOTAL=("APORTE", "sum"),
        CANTIDAD_TOTAL=("CANTIDAD", "sum"),
    )

    agg["PRECIO_PROM"] = np.where(
        agg["CANTIDAD_TOTAL"] > 0, agg["NETO_TOTAL"] / agg["CANTIDAD_TOTAL"], 0,
    )
    agg["APORTE_UNIT"] = np.where(
        agg["CANTIDAD_TOTAL"] > 0, agg["APORTE_TOTAL"] / agg["CANTIDAD_TOTAL"], 0,
    )
    agg["MARGEN_PCT"] = np.where(
        agg["NETO_TOTAL"] > 0, agg["APORTE_TOTAL"] / agg["NETO_TOTAL"], 0,
    )

    return agg[["SKU_PRODUCTO", "PRECIO_PROM", "MARGEN_PCT", "APORTE_UNIT"]]


_MARGIN_FLOOR = 0.15  # Never suggest a discount that drops combined margin below 15%


def _calc_pack_pricing(df_pairs, df_txn, n_baskets):
    """Enrich pairs with pack pricing, discount suggestion, and VN incremental.

    For each pair calculates:
    - Individual prices (A, B) and sum
    - Combined margin %
    - Suggested discount tier (Lift x Confidence)
    - Margin-floor-adjusted discount
    - Estimated VN incremental from bundling
    """
    if df_pairs.empty or df_txn.empty:
        return df_pairs

    sku_pricing = _calc_sku_pricing(df_txn)

    # Merge pricing for SKU_A
    pricing_a = sku_pricing.rename(columns={
        "SKU_PRODUCTO": "SKU_A",
        "PRECIO_PROM": "PRECIO_PROM_A",
        "MARGEN_PCT": "MARGEN_PCT_A",
        "APORTE_UNIT": "APORTE_UNIT_A",
    })[["SKU_A", "PRECIO_PROM_A", "MARGEN_PCT_A", "APORTE_UNIT_A"]]

    # Merge pricing for SKU_B
    pricing_b = sku_pricing.rename(columns={
        "SKU_PRODUCTO": "SKU_B",
        "PRECIO_PROM": "PRECIO_PROM_B",
        "MARGEN_PCT": "MARGEN_PCT_B",
        "APORTE_UNIT": "APORTE_UNIT_B",
    })[["SKU_B", "PRECIO_PROM_B", "MARGEN_PCT_B", "APORTE_UNIT_B"]]

    df = df_pairs.merge(pricing_a, on="SKU_A", how="left")
    df = df.merge(pricing_b, on="SKU_B", how="left")

    for c in ["PRECIO_PROM_A", "PRECIO_PROM_B", "MARGEN_PCT_A", "MARGEN_PCT_B",
              "APORTE_UNIT_A", "APORTE_UNIT_B"]:
        df[c] = df[c].fillna(0)

    df["SUMA_PRECIOS"] = df["PRECIO_PROM_A"] + df["PRECIO_PROM_B"]
    df["MARGEN_COMBINADO"] = np.where(
        df["SUMA_PRECIOS"] > 0,
        (df["APORTE_UNIT_A"] + df["APORTE_UNIT_B"]) / df["SUMA_PRECIOS"],
        0,
    )

    # ── Discount suggestion tiers ─────────────────────────────────────────
    conf_max = df[["CONFIANZA_AB", "CONFIANZA_BA"]].max(axis=1)
    conditions = [
        (df["LIFT"] >= 3.0) & (conf_max >= 0.30),
        (df["LIFT"] >= 2.0) & (conf_max >= 0.20),
        (df["LIFT"] >= 1.5) & (conf_max >= 0.15),
        (df["LIFT"] >= 1.2) & (conf_max >= 0.10),
    ]
    df["DCTO_SUGERIDO_RAW"] = np.select(conditions, [0.15, 0.10, 0.07, 0.05], default=0.0)

    # Margin floor: never drop combined margin below _MARGIN_FLOOR
    max_dcto_by_margin = np.where(
        df["MARGEN_COMBINADO"] > _MARGIN_FLOOR,
        df["MARGEN_COMBINADO"] - _MARGIN_FLOOR,
        0,
    )
    df["DCTO_SUGERIDO"] = np.minimum(df["DCTO_SUGERIDO_RAW"], max_dcto_by_margin)

    # ── Pack price and margin ─────────────────────────────────────────────
    df["PRECIO_PACK"] = df["SUMA_PRECIOS"] * (1 - df["DCTO_SUGERIDO"])
    total_aporte = df["APORTE_UNIT_A"] + df["APORTE_UNIT_B"]
    dcto_abs = df["SUMA_PRECIOS"] * df["DCTO_SUGERIDO"]
    df["APORTE_PACK"] = total_aporte - dcto_abs
    df["MARGEN_PACK"] = np.where(
        df["PRECIO_PACK"] > 0, df["APORTE_PACK"] / df["PRECIO_PACK"], 0,
    )

    # ── VN incremental estimate ───────────────────────────────────────────
    uplift_factor = np.minimum((df["LIFT"] - 1) * 0.2, 0.4).clip(lower=0)
    df["VN_INCREMENTAL_EST"] = df["FREQ"] * df["PRECIO_PACK"] * uplift_factor

    # ── Viability flag ────────────────────────────────────────────────────
    df["PACK_VIABLE"] = (
        (df["LIFT"] > 1.2)
        & (df["PRECIO_PROM_A"] > 0)
        & (df["PRECIO_PROM_B"] > 0)
    )

    return df


def _classify_actions(df_packs):
    """Classify each viable pack into an action type based on business rules.

    Types:
    - PACK_PRECIO: Lift >= 2.0, same area or high discount — create price bundles
    - CROSS_MERCH: Lift >= 1.5, different areas — display together in store
    - CROSS_SELL_DIGITAL: Lift >= 1.2, high combined margin — online recommendations
    - EXHIBICION: Moderate affinity — co-display at point of sale
    """
    df = df_packs.copy()
    same_area = (
        df["AREA_A"].astype(str).str.strip().str.upper()
        == df["AREA_B"].astype(str).str.strip().str.upper()
    ) if "AREA_A" in df.columns and "AREA_B" in df.columns else pd.Series(False, index=df.index)

    conditions = [
        (df["LIFT"] >= 2.0) & (same_area | (df["DCTO_SUGERIDO"] >= 0.07)),
        (df["LIFT"] >= 1.5) & (~same_area),
        (df["LIFT"] >= 1.2) & (df["MARGEN_COMBINADO"] >= 0.25),
    ]
    codes = ["PACK_PRECIO", "CROSS_MERCH", "CROSS_SELL_DIGITAL"]
    labels = ["Pack de Precio", "Cross-merchandising", "Cross-sell Digital"]

    df["TIPO_ACCION"] = np.select(conditions, codes, default="EXHIBICION")
    df["TIPO_ACCION_LABEL"] = np.select(conditions, labels, default="Solo Exhibicion")
    return df


def _build_action_text(row):
    """Generate human-readable recommendation text for a pack suggestion."""
    nombre_a = str(row.get("SKU_NOM_PRODUCTO_A", row.get("SKU_A", "")))[:35]
    nombre_b = str(row.get("SKU_NOM_PRODUCTO_B", row.get("SKU_B", "")))[:35]
    dcto = row.get("DCTO_SUGERIDO", 0)
    precio_pack = row.get("PRECIO_PACK", 0)
    lift = row.get("LIFT", 0)
    margen = row.get("MARGEN_PACK", 0)
    conf_ab = row.get("CONFIANZA_AB", 0)
    conf_ba = row.get("CONFIANZA_BA", 0)
    tipo = row.get("TIPO_ACCION", "")

    if tipo == "PACK_PRECIO" and dcto > 0:
        return (
            f"Pack: {nombre_a} + {nombre_b} a {_fmt_clp(precio_pack)} "
            f"(-{_fmt_pct_cl(dcto, 0)}). Lift {_fmt_dec_cl(lift)}, "
            f"Margen {_fmt_pct_cl(margen, 0)}."
        )
    elif tipo == "CROSS_MERCH":
        return (
            f"Exhibir juntos: {nombre_a} + {nombre_b}. "
            f"Lift {_fmt_dec_cl(lift)}. "
            f"Conf {_fmt_pct_cl(max(conf_ab, conf_ba), 0)}."
        )
    elif tipo == "CROSS_SELL_DIGITAL":
        return (
            f"Recomendar online: {nombre_a} + {nombre_b}. "
            f"Margen combinado {_fmt_pct_cl(margen, 0)}, "
            f"Lift {_fmt_dec_cl(lift)}."
        )
    else:
        if dcto > 0:
            return (
                f"{nombre_a} + {nombre_b}: exhibir juntos. "
                f"Dcto hasta {_fmt_pct_cl(dcto, 0)} viable."
            )
        return (
            f"{nombre_a} + {nombre_b}: exhibir juntos. "
            f"Lift {_fmt_dec_cl(lift)}."
        )


def _render_sugerencias_tab(df_packs, df_txn):
    """Render the Sugerencias de Packs & Accionables tab."""
    viable = df_packs[df_packs["PACK_VIABLE"]].copy()

    if viable.empty:
        st.info(
            "No hay packs viables con los filtros actuales. "
            "Intenta bajar el soporte minimo o la frecuencia."
        )
        return

    # ── Section 1: KPIs ──────────────────────────────────────────────────
    st.markdown("#### Resumen de Oportunidades")
    k1, k2, k3, k4 = st.columns(4)

    k1.metric("Packs Viables", f"{len(viable):,}")

    vn_total = viable["VN_INCREMENTAL_EST"].sum()
    k2.metric("VN Incremental Est.", _fmt_mm(vn_total))

    avg_ticket = viable["PRECIO_PACK"].mean()
    k3.metric("Ticket Prom. Pack", _fmt_mm(avg_ticket))

    if "AREA_A" in viable.columns:
        cat_counts = pd.concat([viable["AREA_A"], viable["AREA_B"]]).value_counts()
        top_cat = cat_counts.index[0] if len(cat_counts) > 0 else "—"
        k4.metric("Top Categoria", str(top_cat)[:20])
    else:
        k4.metric("Top Categoria", "—")

    st.html("<br>")

    # ── Section 2: Pack Suggestions Table ─────────────────────────────────
    st.markdown("#### Sugerencias de Packs")

    viable = viable.sort_values("VN_INCREMENTAL_EST", ascending=False)
    viable["ACCION"] = viable.apply(_build_action_text, axis=1)

    display_cols = [
        "SKU_A", "SKU_NOM_PRODUCTO_A", "SKU_B", "SKU_NOM_PRODUCTO_B",
        "FREQ", "LIFT", "CONFIANZA_AB", "CONFIANZA_BA",
        "PRECIO_PROM_A", "PRECIO_PROM_B", "SUMA_PRECIOS",
        "DCTO_SUGERIDO", "PRECIO_PACK", "MARGEN_PACK",
        "VN_INCREMENTAL_EST", "TIPO_ACCION_LABEL", "ACCION",
    ]
    display_cols = [c for c in display_cols if c in viable.columns]
    df_display = viable[display_cols].head(100).copy()

    rename_map = {
        "SKU_NOM_PRODUCTO_A": "Producto A",
        "SKU_NOM_PRODUCTO_B": "Producto B",
        "CONFIANZA_AB": "Conf A→B",
        "CONFIANZA_BA": "Conf B→A",
        "PRECIO_PROM_A": "Precio A",
        "PRECIO_PROM_B": "Precio B",
        "SUMA_PRECIOS": "Suma Ind.",
        "DCTO_SUGERIDO": "Dcto Sug.",
        "PRECIO_PACK": "Precio Pack",
        "MARGEN_PACK": "Margen Pack",
        "VN_INCREMENTAL_EST": "VN Incr. Est.",
        "TIPO_ACCION_LABEL": "Tipo Accion",
        "ACCION": "Recomendacion",
    }
    df_display = df_display.rename(columns=rename_map)

    # ── Pre-format numbers in Chilean locale ──────────────────────
    for col in ["Precio A", "Precio B", "Suma Ind.", "Precio Pack", "VN Incr. Est."]:
        if col in df_display.columns:
            df_display[col] = df_display[col].apply(_fmt_clp)
    for col in ["Conf A→B", "Conf B→A", "Margen Pack"]:
        if col in df_display.columns:
            df_display[col] = df_display[col].apply(lambda x: _fmt_pct_cl(x, 1))
    if "Dcto Sug." in df_display.columns:
        df_display["Dcto Sug."] = df_display["Dcto Sug."].apply(
            lambda x: _fmt_pct_cl(x, 0)
        )
    if "LIFT" in df_display.columns:
        df_display["LIFT"] = df_display["LIFT"].apply(
            lambda x: _fmt_dec_cl(x, 2)
        )
    if "FREQ" in df_display.columns:
        df_display["FREQ"] = df_display["FREQ"].apply(
            lambda x: f"{int(x):,}".replace(",", ".") if not pd.isna(x) else "—"
        )

    col_config = {
        "Recomendacion": st.column_config.TextColumn("Recomendacion", width="large"),
    }

    st.dataframe(
        df_display,
        column_config=col_config,
        use_container_width=True,
        height=500,
        hide_index=True,
    )

    download_buttons(viable[display_cols], "sugerencias_packs")

    st.html("<br>")

    # ── Section 3: Grouped by Action Type ─────────────────────────────────
    st.markdown("#### Accionables por Tipo")

    _action_icons = {
        "PACK_PRECIO": "🎯",
        "CROSS_MERCH": "🏪",
        "CROSS_SELL_DIGITAL": "📧",
        "EXHIBICION": "👀",
    }
    _action_descs = {
        "PACK_PRECIO": "Crear packs con precio bundle. Alta afinidad, mismo rubro o complementario.",
        "CROSS_MERCH": "Exhibir juntos en tienda. Areas distintas con fuerte co-ocurrencia.",
        "CROSS_SELL_DIGITAL": "Recomendar en e-commerce / email. Alto margen combinado.",
        "EXHIBICION": "Co-exhibicion en punto de venta. Afinidad moderada.",
    }

    for action_type in ["PACK_PRECIO", "CROSS_MERCH", "CROSS_SELL_DIGITAL", "EXHIBICION"]:
        subset = viable[viable["TIPO_ACCION"] == action_type]
        if subset.empty:
            continue

        icon = _action_icons.get(action_type, "")
        label = subset["TIPO_ACCION_LABEL"].iloc[0]
        desc = _action_descs.get(action_type, "")
        vn_sub = subset["VN_INCREMENTAL_EST"].sum()

        with st.expander(
            f"{icon} {label} ({len(subset)} pares) — VN Incr. {_fmt_mm(vn_sub)}",
            expanded=(action_type == "PACK_PRECIO"),
        ):
            st.caption(desc)
            top5 = subset.nlargest(5, "VN_INCREMENTAL_EST")
            for _, row in top5.iterrows():
                accion = row.get("ACCION", "")
                if not accion:
                    accion = _build_action_text(row)
                st.markdown(
                    f"- **{row.get('SKU_A', '')}** + **{row.get('SKU_B', '')}**: {accion}"
                )

    st.html("<br>")

    # ── Section 4: Category Opportunity Summary ───────────────────────────
    st.markdown("#### Oportunidades por Categoria")

    if "AREA_A" in viable.columns and "AREA_B" in viable.columns:
        cat_opp = viable.groupby(["AREA_A", "AREA_B"], as_index=False).agg(
            N_PACKS=("FREQ", "count"),
            VN_INCREMENTAL=("VN_INCREMENTAL_EST", "sum"),
            LIFT_PROM=("LIFT", "mean"),
        ).sort_values("VN_INCREMENTAL", ascending=False)

        # Make symmetric: merge (A,B) and (B,A)
        cat_opp["CAT_KEY"] = cat_opp.apply(
            lambda r: tuple(sorted([str(r["AREA_A"]), str(r["AREA_B"])])), axis=1,
        )
        cat_opp = cat_opp.groupby("CAT_KEY", as_index=False).agg(
            N_PACKS=("N_PACKS", "sum"),
            VN_INCREMENTAL=("VN_INCREMENTAL", "sum"),
            LIFT_PROM=("LIFT_PROM", "mean"),
        )
        cat_opp["AREA_1"] = cat_opp["CAT_KEY"].apply(lambda x: x[0])
        cat_opp["AREA_2"] = cat_opp["CAT_KEY"].apply(lambda x: x[1])
        cat_opp = cat_opp.drop(columns=["CAT_KEY"]).sort_values(
            "VN_INCREMENTAL", ascending=False,
        )

        # Top 10 chart
        top10 = cat_opp.head(10).copy()
        if not top10.empty:
            top10["LABEL"] = top10["AREA_1"] + " x " + top10["AREA_2"]
            top10_plot = top10.sort_values("VN_INCREMENTAL", ascending=True)

            fig_cat = go.Figure()
            fig_cat.add_trace(go.Bar(
                y=top10_plot["LABEL"],
                x=top10_plot["VN_INCREMENTAL"],
                orientation="h",
                marker_color=COLORS["secondary"],
                hovertemplate=(
                    "%{y}<br>"
                    "VN Incremental: $%{x:,.0f}<br>"
                    "Packs: %{customdata[0]:,}<br>"
                    "Lift Prom: %{customdata[1]:.2f}"
                    "<extra></extra>"
                ),
                customdata=top10_plot[["N_PACKS", "LIFT_PROM"]].values,
            ))
            fig_cat.update_layout(**dorel_layout(
                title="Top 10 cruces de categoria por VN incremental",
                xaxis=dict(title="VN Incremental Estimado ($)", tickprefix="$", tickformat=","),
                yaxis=dict(title=""),
                height=max(len(top10) * 35, 300),
                margin=dict(l=250),
            ))
            st.plotly_chart(fig_cat, use_container_width=True)

        # Table
        cat_display = cat_opp.rename(columns={
            "AREA_1": "Categoria 1",
            "AREA_2": "Categoria 2",
            "N_PACKS": "N Packs",
            "VN_INCREMENTAL": "VN Incremental",
            "LIFT_PROM": "Lift Prom.",
        })
        cat_display["VN Incremental"] = cat_display["VN Incremental"].apply(_fmt_clp)
        cat_display["Lift Prom."] = cat_display["Lift Prom."].apply(
            lambda x: _fmt_dec_cl(x, 2)
        )
        st.dataframe(
            cat_display,
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Datos de categoria no disponibles para este analisis.")


# ─── Render ───────────────────────────────────────────────────────────────────

def render_canasta(conn):
    """Módulo Análisis de Canasta — co-ocurrencia de productos."""
    st.html(
        "<h2 class='sub-header'>Análisis de Canasta</h2>"
    )
    st.caption(
        "Detecta pares de productos que se compran juntos (misma boleta). "
        "Calcula Soporte, Confianza y Lift para priorizar cross-sell."
    )
    st.info(
        "⚠️ **Asociación ≠ causalidad.** Un lift alto indica co-ocurrencia "
        "estadística, no que un producto cause la compra del otro.",
        icon="📊",
    )

    # ── Filtros ──
    st.markdown("### Parámetros")
    fc1, fc2 = st.columns(2)
    with fc1:
        hoy = date.today()
        fecha_ini = st.date_input(
            "Desde",
            value=hoy - timedelta(days=180),
            max_value=hoy,
            key="can_fecha_ini",
        )
    with fc2:
        fecha_fin = st.date_input(
            "Hasta",
            value=hoy,
            max_value=hoy,
            key="can_fecha_fin",
        )

    st.markdown("**Filtros de ruido**")
    fr1, fr2, fr3, fr4 = st.columns(4)
    with fr1:
        min_soporte = st.slider(
            "Soporte mínimo %",
            min_value=0.01, max_value=1.0, value=0.1, step=0.01,
            key="can_min_sop",
            help="Porcentaje mínimo de canastas en que debe aparecer el par.",
        )
    with fr2:
        min_freq = st.slider(
            "Frecuencia mínima",
            min_value=2, max_value=50, value=5, step=1,
            key="can_min_freq",
            help="Número mínimo de veces que el par debe aparecer.",
        )
    with fr3:
        min_vol = st.slider(
            "Vol. mínimo SKU",
            min_value=5, max_value=100, value=20, step=5,
            key="can_min_vol",
            help="Cada SKU debe tener al menos N transacciones en el período.",
        )
    with fr4:
        max_basket = st.slider(
            "Max SKUs/canasta",
            min_value=5, max_value=100, value=30, step=5,
            key="can_max_basket",
            help="Excluir canastas con más de N SKUs (mayorista ruidoso).",
        )

    # ── Canal ──
    st.markdown("**Canal de análisis**")
    _canal_opts = {
        "Todos los canales": None,
        "Tiendas (Retail)": "MINOR",
        "E-commerce (Etail)": "ETAIL",
        "Mayorista": "MAYOR",
    }
    sel_canal = st.radio(
        "Canal",
        list(_canal_opts.keys()),
        horizontal=True,
        key="can_canal",
        help="Filtra las transacciones por canal antes de generar pares. "
             "Permite ver patrones de co-compra específicos de cada canal.",
    )
    canal_filter = _canal_opts[sel_canal]

    # ── Ejecutar ──
    cache_key = f"can_{fecha_ini}_{fecha_fin}_{min_soporte}_{min_freq}_{min_vol}_{max_basket}_{sel_canal}"

    if st.button("🔍 Analizar Canasta", key="btn_canasta", type="primary"):
        # Limpiar cache anterior
        for k in list(st.session_state.keys()):
            if k.startswith("can_result"):
                del st.session_state[k]

        with st.spinner("Cargando transacciones desde Snowflake..."):
            df_txn = _load_canasta(conn, fecha_ini, fecha_fin)

        if df_txn.empty:
            st.warning("No se encontraron transacciones en el rango seleccionado.")
            return

        # ── Filtrar por canal seleccionado ──
        if canal_filter and "COD_CANAL" in df_txn.columns:
            df_txn = df_txn[
                df_txn["COD_CANAL"].astype(str).str.strip().str.upper() == canal_filter
            ].copy()
            if df_txn.empty:
                st.warning(
                    f"No hay transacciones para el canal **{sel_canal}** "
                    f"en el rango seleccionado."
                )
                return

        st.session_state["can_txn"] = df_txn
        st.session_state["can_canal_used"] = sel_canal

        with st.spinner("Generando pares de co-ocurrencia..."):
            df_pairs_raw, n_baskets, sku_freq = _build_pairs(
                df_txn, max_skus_basket=max_basket, min_vol_sku=min_vol
            )

        if df_pairs_raw.empty:
            st.warning("No hay canastas multi-item suficientes con los filtros seleccionados.")
            return

        with st.spinner("Calculando métricas (Soporte, Confianza, Lift)..."):
            df_pairs = _calc_metrics(df_pairs_raw, n_baskets, sku_freq)

        # Filtrar por soporte y frecuencia mínima
        min_sop_abs = min_soporte / 100
        df_pairs = df_pairs[
            (df_pairs["SOPORTE"] >= min_sop_abs) & (df_pairs["FREQ"] >= min_freq)
        ]

        if df_pairs.empty:
            st.warning(
                "No hay pares que cumplan los umbrales mínimos. "
                "Intenta bajar el soporte o la frecuencia mínima."
            )
            return

        # Enriquecer con nombres y dimensiones
        df_pairs = _enrich_pairs(df_pairs, df_txn)

        # Guardar resultados
        st.session_state["can_result_pairs"] = df_pairs
        st.session_state["can_result_n_baskets"] = n_baskets
        st.session_state["can_result_sku_freq"] = sku_freq

    # ── Mostrar resultados ──
    if "can_result_pairs" not in st.session_state:
        st.markdown("---")
        st.markdown(
            "👆 Configura los parámetros y presiona **Analizar Canasta** para comenzar."
        )
        return

    df_pairs = st.session_state["can_result_pairs"]
    df_pairs = apply_pm_filter(df_pairs)
    n_baskets = st.session_state["can_result_n_baskets"]
    sku_freq = st.session_state["can_result_sku_freq"]
    df_txn = st.session_state.get("can_txn", pd.DataFrame())
    _canal_used = st.session_state.get("can_canal_used", "Todos los canales")

    st.markdown("---")

    # Indicador de canal
    if _canal_used and _canal_used != "Todos los canales":
        st.info(f"Resultados filtrados por canal: **{_canal_used}**", icon="🔍")

    # ── KPIs ──
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Canastas Multi-item", f"{n_baskets:,}")

    # Promedio SKUs por canasta
    if not df_txn.empty:
        _basket_sizes = df_txn.groupby("ID_VENTA")["SKU_PRODUCTO"].nunique()
        _multi = _basket_sizes[_basket_sizes >= 2]
        avg_skus = _multi.mean() if len(_multi) > 0 else 0
        k2.metric("Prom. SKUs/Canasta", f"{avg_skus:.1f}")
    else:
        k2.metric("Prom. SKUs/Canasta", "—")

    n_lift_strong = len(df_pairs[df_pairs["LIFT"] > 1.5])
    k3.metric("Pares Lift > 1.5", f"{n_lift_strong:,}")

    if not df_txn.empty and "NETO" in df_txn.columns:
        _ticket = df_txn.groupby("ID_VENTA")["NETO"].sum()
        _multi_ids = _basket_sizes[_basket_sizes >= 2].index
        _ticket_multi = _ticket[_ticket.index.isin(_multi_ids)]
        ticket_avg = _ticket_multi.mean() if len(_ticket_multi) > 0 else 0
        k4.metric("Ticket Prom. Multi", _fmt_mm(ticket_avg))
    else:
        k4.metric("Ticket Prom. Multi", "—")

    st.html("<br>")

    # ── Tabs ──
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 Pares Frecuentes",
        "🔗 Cross-sell por SKU",
        "🗺️ Afinidad Categorías",
        "📦 Sugerencias de Packs",
    ])

    # ── Tab 1: Pares Frecuentes ──
    with tab1:
        st.markdown("#### Top pares de productos")
        sort_by = st.radio(
            "Ordenar por",
            ["Frecuencia", "Lift"],
            horizontal=True,
            key="can_sort",
        )

        sort_col = "FREQ" if sort_by == "Frecuencia" else "LIFT"
        df_sorted = df_pairs.sort_values(sort_col, ascending=False)

        # Chart: Top 20
        top20 = df_sorted.head(20).copy()
        top20["LABEL"] = top20.apply(
            lambda r: f"{r.get('SKU_NOM_PRODUCTO_A', r['SKU_A'])[:25]} + "
                      f"{r.get('SKU_NOM_PRODUCTO_B', r['SKU_B'])[:25]}",
            axis=1,
        )
        top20_plot = top20.sort_values(sort_col, ascending=True)

        fig = go.Figure()
        fig.add_trace(go.Bar(
            y=top20_plot["LABEL"],
            x=top20_plot[sort_col],
            orientation="h",
            marker_color=COLORS["primary"],
            hovertemplate=(
                "Par: %{y}<br>"
                + (f"Frecuencia: %{{x:,}}<br>" if sort_col == "FREQ" else f"Lift: %{{x:.2f}}<br>")
                + "Soporte: %{customdata[0]:.2%}<br>"
                + "Lift: %{customdata[1]:.2f}"
                + "<extra></extra>"
            ),
            customdata=top20_plot[["SOPORTE", "LIFT"]].values,
        ))
        fig.update_layout(**dorel_layout(
            title=f"Top 20 pares — {sort_by}",
            xaxis=dict(
                title=sort_by,
                tickformat=",.0f" if sort_col == "FREQ" else ".2f",
            ),
            yaxis=dict(title=""),
            height=max(len(top20) * 30, 300),
            margin=dict(l=300),
        ))
        st.plotly_chart(fig, use_container_width=True)

        # Tabla detalle
        st.markdown("**Detalle de pares**")
        tbl_cols = [
            "SKU_A", "SKU_NOM_PRODUCTO_A", "AREA_A", "LINEA_A",
            "SKU_B", "SKU_NOM_PRODUCTO_B", "AREA_B", "LINEA_B",
            "FREQ", "SOPORTE", "CONFIANZA_AB", "CONFIANZA_BA", "LIFT",
        ]
        tbl_cols = [c for c in tbl_cols if c in df_sorted.columns]
        df_tbl = df_sorted[tbl_cols].head(200).copy()

        _rename = {
            "SKU_NOM_PRODUCTO_A": "Producto A",
            "SKU_NOM_PRODUCTO_B": "Producto B",
            "AREA_A": "Área A", "LINEA_A": "Línea A",
            "AREA_B": "Área B", "LINEA_B": "Línea B",
            "CONFIANZA_AB": "Conf A→B",
            "CONFIANZA_BA": "Conf B→A",
        }
        df_tbl = df_tbl.rename(columns=_rename)

        fmt = {
            "SOPORTE": "{:.3%}",
            "Conf A→B": "{:.1%}",
            "Conf B→A": "{:.1%}",
            "LIFT": "{:.2f}",
        }

        def _highlight_lift(row):
            lift = row.get("LIFT", 1)
            if lift > 1.5:
                return ["background-color: #E8F5E9"] * len(row)
            elif lift < 0.8:
                return ["background-color: #FFEBEE"] * len(row)
            return [""] * len(row)

        styler = df_tbl.style.format(fmt, na_rep="—").apply(_highlight_lift, axis=1)
        st.dataframe(styler, use_container_width=True, height=500)
        download_buttons(df_sorted[tbl_cols], "canasta_pares")

    # ── Tab 2: Cross-sell por SKU ──
    with tab2:
        st.markdown("#### Cross-sell: ¿qué se compra junto a un SKU?")

        # Construir lista de SKUs con nombre para el selectbox
        all_skus = set(df_pairs["SKU_A"].unique()) | set(df_pairs["SKU_B"].unique())
        sku_options = sorted(all_skus)

        # Mapeo SKU → nombre
        sku_names = {}
        if not df_txn.empty and "SKU_NOM_PRODUCTO" in df_txn.columns:
            sku_names = (
                df_txn.drop_duplicates("SKU_PRODUCTO")
                .set_index("SKU_PRODUCTO")["SKU_NOM_PRODUCTO"]
                .to_dict()
            )

        sku_labels = {s: f"{s} — {sku_names.get(s, '?')[:40]}" for s in sku_options}
        sel_sku = st.selectbox(
            "Selecciona un SKU",
            options=sku_options,
            format_func=lambda s: sku_labels.get(s, s),
            key="can_sel_sku",
        )

        if sel_sku:
            # Buscar pares donde el SKU seleccionado es A o B
            mask_a = df_pairs["SKU_A"] == sel_sku
            mask_b = df_pairs["SKU_B"] == sel_sku

            rows = []
            # Como A → B
            for _, r in df_pairs[mask_a].iterrows():
                rows.append({
                    "SKU_COMPANION": r["SKU_B"],
                    "NOMBRE": r.get("SKU_NOM_PRODUCTO_B", ""),
                    "AREA": r.get("AREA_B", ""),
                    "LINEA": r.get("LINEA_B", ""),
                    "FREQ": r["FREQ"],
                    "CONFIANZA": r["CONFIANZA_AB"],
                    "LIFT": r["LIFT"],
                })
            # Como B → A (invertir confianza)
            for _, r in df_pairs[mask_b].iterrows():
                rows.append({
                    "SKU_COMPANION": r["SKU_A"],
                    "NOMBRE": r.get("SKU_NOM_PRODUCTO_A", ""),
                    "AREA": r.get("AREA_A", ""),
                    "LINEA": r.get("LINEA_A", ""),
                    "FREQ": r["FREQ"],
                    "CONFIANZA": r["CONFIANZA_BA"],
                    "LIFT": r["LIFT"],
                })

            if rows:
                df_cs = (
                    pd.DataFrame(rows)
                    .sort_values("FREQ", ascending=False)
                    .drop_duplicates("SKU_COMPANION")
                    .head(15)
                )

                st.markdown(f"**Top acompañantes de** `{sel_sku}` — {sku_names.get(sel_sku, '')[:50]}")

                # Mini bar chart de confianza
                df_cs_plot = df_cs.sort_values("CONFIANZA", ascending=True)
                fig_cs = go.Figure()
                fig_cs.add_trace(go.Bar(
                    y=df_cs_plot["SKU_COMPANION"] + " " + df_cs_plot["NOMBRE"].str[:20],
                    x=df_cs_plot["CONFIANZA"],
                    orientation="h",
                    marker_color=COLORS["tertiary_teal"],
                    hovertemplate=(
                        "SKU: %{y}<br>"
                        "Confianza: %{x:.1%}<br>"
                        "Freq: %{customdata[0]:,}<br>"
                        "Lift: %{customdata[1]:.2f}"
                        "<extra></extra>"
                    ),
                    customdata=df_cs_plot[["FREQ", "LIFT"]].values,
                ))
                fig_cs.update_layout(**dorel_layout(
                    title="Confianza (prob. de compra conjunta)",
                    xaxis=dict(title="Confianza", tickformat=".0%"),
                    yaxis=dict(title=""),
                    height=max(len(df_cs) * 32, 200),
                    margin=dict(l=250),
                ))
                st.plotly_chart(fig_cs, use_container_width=True)

                # Tabla
                st.dataframe(
                    df_cs.style.format({
                        "CONFIANZA": "{:.1%}",
                        "LIFT": "{:.2f}",
                    }, na_rep="—"),
                    use_container_width=True,
                )
            else:
                st.info("No hay pares para el SKU seleccionado con los filtros actuales.")

    # ── Tab 3: Afinidad por Categoría ──
    with tab3:
        st.markdown("#### Heatmap de afinidad entre categorías")
        dim_cat = st.radio(
            "Nivel de categoría",
            ["LINEA", "AREA", "SUBLINEA"],
            horizontal=True,
            key="can_dim_heatmap",
        )

        col_a = f"{dim_cat}_A"
        col_b = f"{dim_cat}_B"

        if col_a in df_pairs.columns and col_b in df_pairs.columns:
            # Matriz de co-ocurrencia
            df_cat = df_pairs.groupby([col_a, col_b], as_index=False)["FREQ"].sum()

            # Crear matriz simétrica
            cats = sorted(set(df_cat[col_a].dropna().unique()) | set(df_cat[col_b].dropna().unique()))
            matrix = pd.DataFrame(0, index=cats, columns=cats)

            for _, r in df_cat.iterrows():
                a, b, f = r[col_a], r[col_b], r["FREQ"]
                if pd.notna(a) and pd.notna(b) and a in matrix.index and b in matrix.columns:
                    matrix.loc[a, b] += f
                    matrix.loc[b, a] += f

            if not matrix.empty:
                fig_hm = go.Figure(data=go.Heatmap(
                    z=matrix.values,
                    x=matrix.columns.tolist(),
                    y=matrix.index.tolist(),
                    colorscale="Blues",
                    hovertemplate=(
                        "%{y} × %{x}<br>"
                        "Co-ocurrencias: %{z:,}"
                        "<extra></extra>"
                    ),
                ))
                fig_hm.update_layout(**dorel_layout(
                    title=f"Co-ocurrencia por {dim_cat}",
                    height=max(len(cats) * 35, 400),
                    xaxis=dict(tickangle=-45),
                ))
                st.plotly_chart(fig_hm, use_container_width=True)
            else:
                st.info("No hay datos suficientes para el heatmap.")
        else:
            st.info(f"Columna {dim_cat} no disponible en los datos.")

    # ── Tab 4: Sugerencias de Packs ──
    with tab4:
        if "can_result_packs" not in st.session_state:
            with st.spinner("Calculando sugerencias de packs..."):
                df_packs = _calc_pack_pricing(df_pairs, df_txn, n_baskets)
                df_packs = _classify_actions(df_packs)
                st.session_state["can_result_packs"] = df_packs
        else:
            df_packs = st.session_state["can_result_packs"]

        _render_sugerencias_tab(df_packs, df_txn)
