"""Proyecto A — Comparativo Abastecimiento Real vs DDMRP (Ene–Mar 2026)

Canal: RETAIL (TIENDA) únicamente — excluye ETAIL y MAYORISTA.

Tabla de resultados:
  • Filas  : (SKU, Métrica)  → Ventas | Stock | Abastecimiento | CD Válida | Quiebre
  • Columnas: días (01/01 … 31/03)

Metodología DDMRP:
  • CV calculado sobre Oct–Dic 2025:
      - CV ≤ 0.5  → ventana 14 días (baja variabilidad)
      - CV > 0.5  → ventana  8 días (alta variabilidad)
  • Red = ADU×LT×0.5 | Yellow = ADU×LT | Green = ADU×Ciclo
  • Reorden cuando NFP < TOY; reposición efectiva solo si CD tenía stock
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, dorel_layout
from db.queries import _PROD
from utils.export import download_buttons
from utils.filters import limpiar_lista

# ─── Mapping VCM cod_ccosto (RETAIL) → Syncro id_sucursal ────────────────
_VCM_TO_SYNCRO: dict[str, str] = {
    "0206": "0005", "0207": "0002", "0209": "0009",
    "0212": "1090", "0213": "1110", "0216": "1710",
    "0217": "1170", "0218": "1180", "0219": "1690",
    "0224": "1240", "0226": "1260", "0227": "1270",
    "0229": "1290", "0230": "1300", "0231": "1310",
    "0232": "1320", "0233": "1330", "0234": "1340",
    "0238": "1380", "0240": "1400", "0241": "1410",
    "0242": "1420", "0248": "1480", "0250": "1500",
    "0252": "1520", "0255": "1550", "0259": "1590",
    "0260": "1600", "0261": "1610", "0265": "1650",
    "0266": "1680", "0267": "1670", "0268": "1700",
    "0269": "1720",
}
# ccosto válidos (solo RETAIL, excluye 0201=Web Bis/Etail)
_CCOSTO_RETAIL = tuple(_VCM_TO_SYNCRO.keys())
_CCOSTO_IN     = ", ".join(f"'{c}'" for c in _CCOSTO_RETAIL)
# Syncro IDs equivalentes — para filtrar tablas de stock por sucursal RETAIL
_SYNCRO_RETAIL = tuple(_VCM_TO_SYNCRO.values())
_SYNCRO_IN     = ", ".join(f"'{s}'" for s in _SYNCRO_RETAIL)

# ─── Parámetros ───────────────────────────────────────────────────────────
_FECHA_INICIO_STR = "2026-01-01"
_FECHA_FIN_STR    = "2026-03-31"
_CV_HIST_START    = "2025-10-03"
_CV_HIST_END      = "2025-12-31"
CV_THRESHOLD      = 0.5
ADU_DIAS_LOW      = 14

# ─── Calendarios de reabastecimiento por tienda ───────────────────────────
# review_days: {weekday → lt_dias}  (0=Lun, 1=Mar, 2=Mie, 3=Jue, 4=Vie)
# Coincidencia por substring en el nombre de la tienda (case-insensitive).
# Trujillo   : Mar/Jue/Vie,  LT=4
# Jockey     : Lun/Mie/Jue,  LT=2
# Atocongo   : Mie(LT=2) / Vie(LT=4)   [Mie→llega Vie, Vie→llega Mar]
# Salaverry  : igual que Atocongo
_STORE_CALENDARS: list[tuple[str, dict[int, int]]] = [
    ("TRUJILLO",   {1: 3, 3: 4, 4: 4}),   # Mar→Vie(LT=3), Jue→Lun(LT=4), Vie→Mar(LT=4)
    ("JOCKEY",     {0: 2, 2: 2, 3: 2}),
    ("SAN MIGUEL", {1: 2, 4: 4}),   # Mar→llega Jue (LT=2), Vie→llega Mar (LT=4)
    ("ATOCONGO",   {2: 2, 4: 4}),
    ("SALAVERRY",  {2: 2, 4: 4}),
]


_DIAS_LABEL = {0: "Lun", 1: "Mar", 2: "Mié", 3: "Jue", 4: "Vie", 5: "Sáb", 6: "Dom"}


def _get_store_calendar(nombre: str) -> dict[int, int] | None:
    """Retorna {weekday: lt} si el nombre coincide con un calendario conocido."""
    nombre_up = nombre.upper()
    for pattern, cal in _STORE_CALENDARS:
        if pattern in nombre_up:
            return cal
    return None


def _avg_ciclo(review_days: dict[int, int] | None) -> float:
    """Ciclo promedio = media de los gaps entre días de revisión (circular 7 días)."""
    if not review_days:
        return float(CICLO_DEFAULT)
    days = sorted(review_days.keys())
    gaps = [((days[(i + 1) % len(days)] - days[i]) % 7) or 7 for i in range(len(days))]
    return round(sum(gaps) / len(gaps), 1)


def _avg_lt(review_days: dict[int, int] | None) -> float:
    """LT promedio de todos los días de revisión."""
    if not review_days:
        return float(LT_DEFAULT)
    return round(sum(review_days.values()) / len(review_days), 1)
ADU_DIAS_HIGH     = 8
LT_DEFAULT        = 2
CICLO_DEFAULT     = 7
N_TOP_TIENDAS     = 3
N_TOP_SKUS        = 5

# ─── SQL ─────────────────────────────────────────────────────────────────

_SQL_VENTAS_2026 = f"""
SELECT
    TRY_TO_DATE(CAST(v.id_periodo AS VARCHAR), 'YYYYMMDD')  AS fecha,
    LPAD(CAST(v.cod_ccosto AS VARCHAR), 4, '0')              AS cod_ccosto,
    v.cod_producto                                            AS sku_producto,
    SUM(v.unidades)                                           AS unidades,
    SUM(v.neto)                                               AS neto
FROM db_finanzas.fct.ft_vcm v
WHERE TRY_TO_DATE(CAST(v.id_periodo AS VARCHAR), 'YYYYMMDD')
      BETWEEN '2026-01-01' AND '2026-03-31'
  AND v.unidades > 0
  AND LPAD(CAST(v.cod_ccosto AS VARCHAR), 4, '0') IN ({_CCOSTO_IN})
GROUP BY 1, 2, 3
"""

_SQL_VENTAS_PRE2026 = f"""
SELECT
    TRY_TO_DATE(CAST(v.id_periodo AS VARCHAR), 'YYYYMMDD')  AS fecha,
    LPAD(CAST(v.cod_ccosto AS VARCHAR), 4, '0')              AS cod_ccosto,
    v.cod_producto                                            AS sku_producto,
    SUM(v.unidades)                                           AS unidades
FROM db_finanzas.fct.ft_vcm v
WHERE TRY_TO_DATE(CAST(v.id_periodo AS VARCHAR), 'YYYYMMDD')
      BETWEEN '{_CV_HIST_START}' AND '{_CV_HIST_END}'
  AND v.unidades > 0
  AND LPAD(CAST(v.cod_ccosto AS VARCHAR), 4, '0') IN ({_CCOSTO_IN})
GROUP BY 1, 2, 3
"""

_SQL_STOCK_TIENDA_2026 = f"""
SELECT
    a.fecha,
    a.cod_bodega    AS id_sucursal,
    a.sku_producto,
    SUM(a.stock_unidades) AS stock_unidades
FROM db_supply.hst.ht_in_stock a
JOIN db_syncros.public.coo_maestro_sucursal b ON a.cod_bodega = b.id_sucursal
WHERE a.fecha BETWEEN '2026-01-01' AND '2026-03-31'
  AND a.cod_bodega IN ({_SYNCRO_IN})
  AND CASE WHEN b.canal_de_distribucion = 'RETAIL'
           THEN 'TIENDA' ELSE b.canal_de_distribucion END = 'TIENDA'
GROUP BY 1, 2, 3
"""

_SQL_STOCK_CD_2026 = """
SELECT
    fecha,
    sku_producto,
    SUM(stock_unidades) AS stock_cd_unidades
FROM db_supply.hst.ht_in_stock_cd
WHERE fecha BETWEEN '2026-01-01' AND '2026-03-31'
GROUP BY 1, 2
"""

_SQL_CATALOGOS = """
SELECT id_sucursal,
       descripcion_sucursal,
       CASE WHEN canal_de_distribucion = 'RETAIL' THEN 'TIENDA'
            ELSE canal_de_distribucion END AS canal
FROM db_syncros.public.coo_maestro_sucursal
"""

_SQL_NOMBRES_SKUS = """
SELECT cod_producto AS sku_producto, descripcion_producto AS nom_producto
FROM db_dimensiones.dim.vw_producto
"""

# ─── Loaders ─────────────────────────────────────────────────────────────

def _run_query(conn, sql: str) -> pd.DataFrame:
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    cols = [d[0].lower() for d in cur.description]
    cur.close()
    return pd.DataFrame(rows, columns=cols)


@st.cache_data(ttl=3600, show_spinner=False)
def _load_ventas_2026(_conn):
    df = _run_query(_conn, _SQL_VENTAS_2026)
    df["fecha"]    = pd.to_datetime(df["fecha"])
    df["unidades"] = pd.to_numeric(df["unidades"], errors="coerce").fillna(0)
    df["neto"]     = pd.to_numeric(df["neto"],     errors="coerce").fillna(0)
    df["id_sucursal"] = df["cod_ccosto"].map(_VCM_TO_SYNCRO)
    return df.dropna(subset=["id_sucursal"])


@st.cache_data(ttl=3600, show_spinner=False)
def _load_ventas_pre2026(_conn):
    df = _run_query(_conn, _SQL_VENTAS_PRE2026)
    df["fecha"]    = pd.to_datetime(df["fecha"])
    df["unidades"] = pd.to_numeric(df["unidades"], errors="coerce").fillna(0)
    df["id_sucursal"] = df["cod_ccosto"].map(_VCM_TO_SYNCRO)
    return df.dropna(subset=["id_sucursal"])


@st.cache_data(ttl=3600, show_spinner=False)
def _load_stock_tienda_2026(_conn):
    df = _run_query(_conn, _SQL_STOCK_TIENDA_2026)
    df["fecha"]          = pd.to_datetime(df["fecha"])
    df["stock_unidades"] = pd.to_numeric(df["stock_unidades"], errors="coerce").fillna(0)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_stock_cd_2026(_conn):
    df = _run_query(_conn, _SQL_STOCK_CD_2026)
    df["fecha"]             = pd.to_datetime(df["fecha"])
    df["stock_cd_unidades"] = pd.to_numeric(df["stock_cd_unidades"], errors="coerce").fillna(0)
    return df


@st.cache_data(ttl=86400, show_spinner=False)
def _load_catalogos(_conn):
    df_t = _run_query(_conn, _SQL_CATALOGOS)
    df_s = _run_query(_conn, _SQL_NOMBRES_SKUS)
    return df_t, df_s


@st.cache_data(ttl=3600, show_spinner=False)
def _load_dim_producto(_conn) -> pd.DataFrame:
    """Dimensión de producto: área, línea, sublínea, marca, proveedor, mix, procedencia."""
    sql = f"""
    SELECT DISTINCT
        p.sku_producto,
        p.nom_producto,
        UPPER(TRIM(CAST(p.area        AS VARCHAR))) AS area,
        UPPER(TRIM(CAST(p.linea       AS VARCHAR))) AS linea,
        UPPER(TRIM(CAST(p.sublinea    AS VARCHAR))) AS sublinea,
        UPPER(TRIM(CAST(p.marca       AS VARCHAR))) AS marca,
        UPPER(TRIM(CAST(p.proveedor   AS VARCHAR))) AS proveedor,
        UPPER(TRIM(CAST(p.mix_oficial AS VARCHAR))) AS mix_oficial,
        UPPER(TRIM(CAST(p.mix         AS VARCHAR))) AS mix,
        UPPER(TRIM(CAST(p.procedencia AS VARCHAR))) AS procedencia
    FROM {_PROD} p
    WHERE p.sku_producto IS NOT NULL
    """
    df = _run_query(_conn, sql)
    # Limpiar valores vacíos
    for col in ("area", "linea", "sublinea", "marca", "proveedor", "mix_oficial", "mix", "procedencia"):
        df[col] = df[col].replace({"": None, "NONE": None, "NULL": None})
    return df


# ─── CV y ADU ────────────────────────────────────────────────────────────

def _compute_cv_adu(df_pre: pd.DataFrame) -> pd.DataFrame:
    """CV y ADU por SKU × Tienda usando histórico Oct–Dic 2025."""
    if df_pre.empty:
        return pd.DataFrame(columns=["sku_producto", "id_sucursal", "cv", "adu", "variabilidad"])

    all_dates = pd.date_range(_CV_HIST_START, _CV_HIST_END, freq="D")
    combos    = df_pre[["sku_producto", "id_sucursal"]].drop_duplicates()
    records   = []

    for _, row in combos.iterrows():
        sku = row["sku_producto"]
        suc = row["id_sucursal"]
        sub = _safe_series(
            df_pre[(df_pre["sku_producto"] == sku) & (df_pre["id_sucursal"] == suc)].copy(),
            "unidades", all_dates,
        )
        mean_ = sub.mean()
        std_  = sub.std(ddof=0)
        cv    = float(std_ / mean_) if mean_ > 0 else 999.0
        var   = "Baja" if cv <= CV_THRESHOLD else "Alta"
        win   = ADU_DIAS_LOW if var == "Baja" else ADU_DIAS_HIGH
        adu   = float(sub.iloc[-win:].mean()) if len(sub) >= win else float(sub.mean())

        records.append({
            "sku_producto": sku,
            "id_sucursal":  suc,
            "cv":           round(cv, 3),
            "adu":          round(adu, 4),
            "variabilidad": var,
        })
    return pd.DataFrame(records)


# ─── Simulación DDMRP ────────────────────────────────────────────────────

def _simulate_ddmrp(
    ventas_ser:       pd.Series,
    stock_inicio:     float,
    stock_cd_ser:     pd.Series,
    adu:              float,
    review_days:      dict[int, int] | None = None,
    lt:               int = LT_DEFAULT,
    ciclo:            int = CICLO_DEFAULT,
    quiebre_real_ser: "pd.Series | None" = None,
    mode_by_weekday:  "dict[int, float] | None" = None,
) -> pd.DataFrame:
    """Simulación diaria DDMRP para un SKU × Tienda.

    review_days      : {weekday: lt_dias} — si se provee, solo genera pedidos en
                       esos días de la semana y usa el LT específico por día.
    quiebre_real_ser : Serie (fecha→0/1). Si el día tenía quiebre real (venta=0
                       por falta de stock en tienda), y el stock DDMRP es > 0,
                       se sustituye la demanda por mode_by_weekday[weekday].
    mode_by_weekday  : {weekday: moda_unidades} — demanda estimada por día de semana.
    """
    adu    = max(adu, 0.001)
    # Buffers calculados con promedios efectivos de ciclo/LT del calendario
    eff_lt    = _avg_lt(review_days)    if review_days else float(lt)
    eff_ciclo = _avg_ciclo(review_days) if review_days else float(ciclo)
    red    = adu * eff_lt * 0.5
    yellow = adu * eff_lt
    green  = adu * eff_ciclo
    toy    = red + yellow
    tog    = toy + green

    dates   = ventas_ser.index.sort_values()
    stock   = float(stock_inicio)
    pending: dict = {}

    rows = []
    for dt in dates:
        # 1. Llegan pedidos programados para hoy
        cd_avail  = float(stock_cd_ser.get(dt, 0))
        arriving  = pending.pop(dt, 0.0)
        fulfilled = min(arriving, cd_avail)
        stock    += fulfilled
        stock_inicio_dia = stock

        # 2. Ventas del día
        demand = float(ventas_ser.get(dt, 0))
        real_qb = bool(
            quiebre_real_ser is not None and quiebre_real_ser.get(dt, 0) == 1
        )
        # Si la venta es 0 y el stock DDMRP supera la moda del día de semana,
        # simular la venta esperada: DDMRP tenía suficiente stock para vender.
        if demand == 0 and mode_by_weekday is not None:
            wd   = dt.weekday()
            moda = mode_by_weekday.get(wd, 0.0)
            if moda > 0 and stock > moda:
                demand = moda
        venta_posible = min(demand, stock)
        # Quiebre: stock insuficiente frente a demanda real/estimada, o día con
        # quiebre real donde DDMRP también se quedó sin stock.
        quiebre = 1 if (stock <= 0 and (demand > 0 or real_qb)) else 0
        stock   = max(0.0, stock - demand)

        # 3. Revisión: solo en días de reabastecimiento (o diario si no hay calendario)
        es_dia_revision = (
            review_days is None or dt.weekday() in review_days
        )
        pedido_hoy = 0.0
        if es_dia_revision:
            nfp = stock + sum(pending.values())
            if nfp < toy:
                order_qty = max(0.0, tog - nfp)
                if order_qty >= 0.5:
                    # LT específico del día de la semana, o default
                    lt_hoy = review_days[dt.weekday()] if review_days else lt
                    arr_dt = dt + timedelta(days=lt_hoy)
                    pending[arr_dt] = pending.get(arr_dt, 0.0) + order_qty
                    pedido_hoy = order_qty  # unidades cargadas hoy al CD

        rows.append({
            "fecha":                 dt,
            "stock_inicio_dia":      round(stock_inicio_dia, 1),
            "ventas":                round(demand, 1),
            "ventas_posibles_ddmrp": round(venta_posible, 1),
            "pedido_ddmrp":          round(pedido_hoy, 1),     # cargado hoy
            "abastecimiento_ddmrp":  round(fulfilled, 1),      # llegó hoy
            "stock_fin_ddmrp":       round(stock, 1),
            "quiebre_ddmrp":         quiebre,
            "puede_abastecer_cd":    1 if cd_avail > 0 else 0,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df["fecha"] = pd.to_datetime(df["fecha"])
    return df


# ─── Construcción de tablas (filas=métricas, columnas=días) ──────────────

def _build_wide_table(
    records: dict[str, pd.Series],
    sku_label: str,
    all_dates,
) -> pd.DataFrame:
    """
    records: {metrica_label: pd.Series indexed by date}
    Devuelve DataFrame con columnas = fechas (dd/mm), index = métrica.
    Primera columna extra: SKU y Métrica para identificación.
    """
    # Columna = "Lun 01/01" — día de semana en 3 letras + fecha
    date_cols = [
        f"{_DIAS_LABEL[d.weekday()]} {d.strftime('%d/%m')}"
        for d in all_dates
    ]
    rows = []
    for metrica, ser in records.items():
        ser = ser.reindex(all_dates, fill_value=0)
        row = {"SKU": sku_label, "Métrica": metrica}
        for d, col in zip(all_dates, date_cols):
            row[col] = ser.get(d, 0)
        rows.append(row)
    return pd.DataFrame(rows)


def _safe_series(df: pd.DataFrame, col: str, all_dates) -> pd.Series:
    """Agrupa por fecha (suma), normaliza índice a Timestamp y hace reindex."""
    ser = df.groupby("fecha")[col].sum()
    ser.index = pd.to_datetime(ser.index)
    return ser.groupby(level=0).sum().reindex(all_dates, fill_value=0)


def _norm_reindex(ser: pd.Series, all_dates) -> pd.Series:
    """Normaliza índice a Timestamp, dedup y reindex — para series ya agrupadas."""
    ser = ser.copy()
    ser.index = pd.to_datetime(ser.index)
    return ser.groupby(level=0).sum().reindex(all_dates, fill_value=0)


def _compute_venta_moda(ventas_ser: pd.Series, quiebre_ser: pd.Series) -> dict[int, float]:
    """Moda de venta real por día de semana, usando solo días sin quiebre de stock.

    Returns {weekday(0=Lun..6=Dom): moda_unidades}.
    """
    df = pd.DataFrame({"ventas": ventas_ser, "quiebre": quiebre_ser})
    df.index = pd.to_datetime(df.index)
    df["weekday"] = df.index.weekday
    result: dict[int, float] = {}
    for wd in range(7):
        # Solo días sin quiebre y con venta positiva
        sub = df[(df["weekday"] == wd) & (df["quiebre"] == 0) & (df["ventas"] > 0)]["ventas"]
        if len(sub) > 0:
            result[wd] = float(sub.mode().iloc[0])
        else:
            # Fallback: promedio de días con venta (aunque haya quiebre)
            fallback = df[(df["weekday"] == wd) & (df["ventas"] > 0)]["ventas"]
            result[wd] = float(fallback.mean()) if len(fallback) > 0 else 0.0
    return result


def _compute_ventas_ajustadas(
    ventas_ser:  pd.Series,
    quiebre_ser: pd.Series,
    cd_ser:      pd.Series,
    moda_wd:     dict[int, float],
) -> pd.Series:
    """Reemplaza venta=0 por quiebre con moda del día de semana,
    solo si el CD tenía stock ≥ moda ese día.
    """
    adj = ventas_ser.copy().astype(float)
    for dt in adj.index:
        if quiebre_ser.get(dt, 0) == 1:
            wd   = pd.Timestamp(dt).weekday()
            moda = moda_wd.get(wd, 0.0)
            if moda > 0 and float(cd_ser.get(dt, 0)) >= moda:
                adj[dt] = moda
    return adj


def _make_sc1_table(df_sc1, df_st_tienda, sku, sku_label, all_dates, cd_pivot) -> pd.DataFrame:
    """Tabla Escenario Real para un SKU.

    df_st_tienda debe estar pre-filtrado por id_sucursal para que el stock
    corresponda solo a esa tienda y no sume otras sucursales.
    """
    # Stock de tienda — solo esa sucursal × sku
    st_ser = _safe_series(
        df_st_tienda[df_st_tienda["sku_producto"] == sku].copy(),
        "stock_unidades", all_dates,
    )
    v_ser = _safe_series(
        df_sc1[df_sc1["sku_producto"] == sku].copy(),
        "ventas_real", all_dates,
    )
    q_ser = _safe_series(
        df_sc1[df_sc1["sku_producto"] == sku].copy(),
        "quiebre_real", all_dates,
    )
    cd_ser = (
        _norm_reindex(cd_pivot[sku], all_dates)
        if sku in cd_pivot.columns
        else pd.Series(0, index=all_dates)
    )

    # Abastecimiento: abast[t] = stock[t] - stock[t-1] + ventas[t]
    # Si la diferencia es negativa (consumo puro), abastecimiento = 0
    abast_ser = (st_ser.diff().fillna(0) + v_ser).clip(lower=0)

    records = {
        "Ventas (und)":         v_ser,
        "Stock Tienda (und)":   st_ser,
        "Abastecimiento (und)": abast_ser,
        "Stock CD (und)":       cd_ser,
        "Quiebre CD (0/1)":     (cd_ser > 0).astype(int),
        "Quiebre (0/1)":        q_ser,
    }
    return _build_wide_table(records, sku_label, all_dates)


def _make_sc2_table(
    df_sim, sku_label, all_dates,
    red_val:  float = 0.0,
    toy_val:  float = 0.0,
    moda_wd:  "dict[int, float] | None" = None,
) -> pd.DataFrame:
    """Tabla Escenario DDMRP para un SKU.
    Pedido DDMRP  = unidades cargadas al CD ese día (día de revisión).
    Llegada DDMRP = unidades que ingresan a tienda ese día (LT después del pedido).
    Buffer TOY    = Red+Yellow (nivel de reorden) — valor constante.
    Ventas (und)  = ventas reales del día; en días de quiebre DDMRP (stock=0)
                    se sustituye el 0 por la moda del día de semana para mostrar
                    la venta potencial que se habría generado con stock disponible.
    """
    base = df_sim.groupby("fecha").sum(numeric_only=True)
    base.index = pd.to_datetime(base.index)
    base = base.groupby(level=0).sum()

    ventas_disp  = base["ventas"].reindex(all_dates, fill_value=0)
    quiebre_disp = base["quiebre_ddmrp"].reindex(all_dates, fill_value=0)

    # Para días de quiebre DDMRP (stock=0) la simulación no pudo vender,
    # pero mostramos la moda como venta potencial no realizada.
    if moda_wd:
        moda_ser = pd.Series(
            [moda_wd.get(d.weekday(), 0.0) for d in all_dates],
            index=all_dates,
        )
        mask = (quiebre_disp == 1) & (ventas_disp == 0)
        ventas_disp = ventas_disp.where(~mask, moda_ser)

    records = {
        "Ventas (und)":          ventas_disp,
        "Stock DDMRP (und)":     base["stock_fin_ddmrp"].reindex(all_dates, fill_value=0),
        "Buffer TOY (und)":      pd.Series(round(toy_val, 1), index=all_dates),
        "Pedido DDMRP (und)":    base["pedido_ddmrp"].reindex(all_dates, fill_value=0),
        "Llegada DDMRP (und)":   base["abastecimiento_ddmrp"].reindex(all_dates, fill_value=0),
        "CD Válida (0/1)":       base["puede_abastecer_cd"].reindex(all_dates, fill_value=0),
        "Quiebre DDMRP (0/1)":   base["quiebre_ddmrp"].reindex(all_dates, fill_value=0),
    }
    return _build_wide_table(records, sku_label, all_dates)


def _make_sc3_table(df_sim, sku_label, all_dates) -> pd.DataFrame:
    """Tabla Escenario 3 — DDMRP con demanda estimada (rellena quiebres con moda)."""
    base = df_sim.groupby("fecha").sum(numeric_only=True)
    base.index = pd.to_datetime(base.index)
    base = base.groupby(level=0).sum()
    records = {
        "Venta estimada (und)":  base["ventas"].reindex(all_dates, fill_value=0),
        "Stock SC3 (und)":       base["stock_fin_ddmrp"].reindex(all_dates, fill_value=0),
        "Pedido SC3 (und)":      base["pedido_ddmrp"].reindex(all_dates, fill_value=0),
        "Llegada SC3 (und)":     base["abastecimiento_ddmrp"].reindex(all_dates, fill_value=0),
        "CD Válida (0/1)":       base["puede_abastecer_cd"].reindex(all_dates, fill_value=0),
        "Quiebre SC3 (0/1)":     base["quiebre_ddmrp"].reindex(all_dates, fill_value=0),
    }
    return _build_wide_table(records, sku_label, all_dates)


# ─── Estilo de tablas ─────────────────────────────────────────────────────

def _style_table(df: pd.DataFrame) -> "pd.io.formats.style.Styler":
    """Colorea filas de Quiebre en rojo, Abastecimiento en azul claro."""
    date_cols = [c for c in df.columns if c not in ("SKU", "Métrica")]

    def row_style(row):
        m = str(row.get("Métrica", ""))
        if "Quiebre" in m:
            return [
                "background-color:#FFE0E0;color:#C62828;font-weight:600"
                if c in date_cols else ""
                for c in df.columns
            ]
        if "Pedido" in m:
            return [
                "background-color:#FFF3E0;color:#E65100;font-weight:600"
                if c in date_cols else ""
                for c in df.columns
            ]
        if "Llegada" in m or "Abastecimiento" in m:
            return [
                "background-color:#E3F2FD;color:#0D47A1"
                if c in date_cols else ""
                for c in df.columns
            ]
        if "CD Válida" in m or "Stock CD" in m or "Quiebre CD" in m:
            return [
                "background-color:#E8F5E9;color:#1B5E20"
                if c in date_cols else ""
                for c in df.columns
            ]
        if "Buffer" in m:
            return [
                "background-color:#F3E5F5;color:#6A1B9A;font-weight:600"
                if c in date_cols else ""
                for c in df.columns
            ]
        return [""] * len(df.columns)

    date_cols_set = set(date_cols)

    def cell_zero_red(val, col):
        """Celda roja cuando el valor numérico es 0 (identifica ausencia rápidamente)."""
        if col not in date_cols_set:
            return ""
        try:
            return "color:#CC0000;font-weight:700" if float(val) == 0 else ""
        except (TypeError, ValueError):
            return ""

    num_cols = df.select_dtypes(include="number").columns.tolist()
    styler = df.style.apply(row_style, axis=1)
    for col in date_cols:
        styler = styler.applymap(lambda v, c=col: cell_zero_red(v, c), subset=[col])
    return styler.format({c: "{:.0f}" for c in num_cols}, na_rep="-")


def _style_ddmrp_table(
    df: pd.DataFrame,
    red_val: float = 0.0,
    toy_val: float = 0.0,
) -> "pd.io.formats.style.Styler":
    """Igual que _style_table pero colorea celdas de Stock DDMRP por zona de buffer.

    Zonas:
      · Rojo   : stock < Red  (= ADU × LT × 0.5)
      · Amarillo: Red ≤ stock < TOY (= ADU × LT × 1.5)
      · Verde  : stock ≥ TOY
    """
    date_cols = [c for c in df.columns if c not in ("SKU", "Métrica")]

    def row_style(row):
        m = str(row.get("Métrica", ""))
        if "Quiebre" in m:
            return [
                "background-color:#FFE0E0;color:#C62828;font-weight:600"
                if c in date_cols else "" for c in df.columns
            ]
        if "Pedido" in m:
            return [
                "background-color:#FFF3E0;color:#E65100;font-weight:600"
                if c in date_cols else "" for c in df.columns
            ]
        if "Llegada" in m or "Abastecimiento" in m:
            return [
                "background-color:#E3F2FD;color:#0D47A1"
                if c in date_cols else "" for c in df.columns
            ]
        if "CD Válida" in m or "Stock CD" in m or "Quiebre CD" in m:
            return [
                "background-color:#E8F5E9;color:#1B5E20"
                if c in date_cols else "" for c in df.columns
            ]
        if "Buffer" in m:
            return [
                "background-color:#F3E5F5;color:#6A1B9A;font-weight:600"
                if c in date_cols else "" for c in df.columns
            ]
        if "Stock DDMRP" in m or "Stock SC3" in m:
            # Coloreo por zona de buffer (celda a celda)
            styles = []
            for c in df.columns:
                if c not in date_cols:
                    styles.append("")
                    continue
                val = row[c]
                if val < red_val:
                    styles.append(
                        "background-color:#FFCDD2;color:#B71C1C;font-weight:700"
                    )
                elif val < toy_val:
                    styles.append(
                        "background-color:#FFF9C4;color:#F57F17;font-weight:700"
                    )
                else:
                    styles.append(
                        "background-color:#C8E6C9;color:#1B5E20;font-weight:700"
                    )
            return styles
        return [""] * len(df.columns)

    date_cols_set = set(date_cols)

    def cell_zero_red(val, col):
        if col not in date_cols_set:
            return ""
        try:
            return "color:#CC0000;font-weight:700" if float(val) == 0 else ""
        except (TypeError, ValueError):
            return ""

    num_cols = df.select_dtypes(include="number").columns.tolist()
    styler = df.style.apply(row_style, axis=1)
    for col in date_cols:
        styler = styler.applymap(lambda v, c=col: cell_zero_red(v, c), subset=[col])
    return styler.format({c: "{:.0f}" for c in num_cols}, na_rep="-")


# ─── Render principal ─────────────────────────────────────────────────────

def render_proyecto_a(conn):
    st.markdown(
        '<h2 style="color:#065E8B;margin-bottom:0.1rem">Proyecto A — Real vs DDMRP</h2>'
        '<p style="color:#64748b;font-size:0.875rem;margin-top:0">'
        "Comparativo abastecimiento real vs metodología DDMRP · Canal RETAIL · Ene–Mar 2026"
        "</p>",
        unsafe_allow_html=True,
    )

    _CACHE_KEY    = "pa_cache"
    _EXCLUDED_IDS = {"1610"}   # San Miguel 2 — tienda cerrada

    # ── Datos base (cacheados, rápidos) ──────────────────────────────────────
    with st.spinner("Cargando catálogos…"):
        df_v26             = _load_ventas_2026(conn)
        df_t_cat, df_s_cat = _load_catalogos(conn)
        df_dim_prod        = _load_dim_producto(conn)

    if df_v26.empty:
        st.warning("Sin ventas RETAIL para Ene–Mar 2026.")
        return

    nombre_tienda: dict[str, str] = dict(zip(df_t_cat["id_sucursal"], df_t_cat["descripcion_sucursal"]))
    nombre_sku:    dict[str, str] = dict(zip(df_s_cat["sku_producto"], df_s_cat["nom_producto"]))

    # ── Top 3 tiendas RETAIL por Neta 2026 (excluye tiendas cerradas) ────
    neta_por_tienda = df_v26.groupby("id_sucursal")["neto"].sum()
    top_tiendas     = (
        neta_por_tienda[~neta_por_tienda.index.isin(_EXCLUDED_IDS)]
        .nlargest(N_TOP_TIENDAS)
        .index.tolist()
    )

    # ── PASO 1: Tabla de ciclos de reabastecimiento ───────────────────────
    st.markdown("#### Paso 1 — Ciclos de reabastecimiento por tienda")
    st.caption(
        "Los valores se pre-cargan desde el calendario configurado. "
        "**Días revisión**: días de la semana en que se revisa y genera pedido al CD. "
        "**LT prom**: lead time promedio CD→Tienda. Puedes editarlos si necesitas ajustar."
    )

    def _dias_str(cal: dict[int, int] | None) -> str:
        if not cal:
            return "Todos"
        return " / ".join(
            f"{_DIAS_LABEL[d]}(LT={lt})" for d, lt in sorted(cal.items())
        )

    ciclos_init_rows = []
    review_days_map: dict[str, dict[int, int] | None] = {}
    for t in top_tiendas:
        nom   = nombre_tienda.get(t, t)
        cal   = _get_store_calendar(nom)
        review_days_map[t] = cal
        ciclos_init_rows.append({
            "id_sucursal":      t,
            "Tienda":           nom,
            "Neta 2026 (S/)":   int(neta_por_tienda.get(t, 0)),
            "Días revisión":    _dias_str(cal),
            "LT prom (días)":   round(_avg_lt(cal), 1),
            "Ciclo prom (días)": round(_avg_ciclo(cal), 1),
        })

    df_ciclos = st.data_editor(
        pd.DataFrame(ciclos_init_rows),
        column_config={
            "id_sucursal":       st.column_config.TextColumn("Código", disabled=True, width="small"),
            "Tienda":            st.column_config.TextColumn("Tienda", disabled=True),
            "Neta 2026 (S/)":    st.column_config.NumberColumn("Neta 2026 (S/)", disabled=True,
                                     format="S/ %d"),
            "Días revisión":     st.column_config.TextColumn("Días revisión", disabled=True),
            "LT prom (días)":    st.column_config.NumberColumn("LT prom (días)",
                                     min_value=1, max_value=21, step=0.5),
            "Ciclo prom (días)": st.column_config.NumberColumn("Ciclo prom (días)",
                                     min_value=1, max_value=60, step=0.5),
        },
        hide_index=True,
        use_container_width=True,
        key="pa_ciclos_editor",
        num_rows="fixed",
    )
    # Permite override manual de LT/Ciclo desde la tabla
    lt_override    = dict(zip(df_ciclos["id_sucursal"], df_ciclos["LT prom (días)"].astype(float)))
    ciclo_override = dict(zip(df_ciclos["id_sucursal"], df_ciclos["Ciclo prom (días)"].astype(float)))

    st.markdown("---")

    # ── Filtros de Producto ───────────────────────────────────────────────
    st.markdown("#### Filtros de Búsqueda")
    st.caption("Aplican sobre los SKUs y tiendas antes de calcular el Top N. Deja vacío para incluir todo.")

    def _opts(col: str) -> list[str]:
        return sorted(df_dim_prod[col].dropna().unique().tolist())

    # Opciones de tienda (solo las top tiendas activas)
    tienda_opts_map = {t: f"{nombre_tienda.get(t, t)} — {t}" for t in top_tiendas}

    with st.expander("Filtros de Búsqueda de Producto", expanded=False):
        # Fila 0: Tienda y Centro de Costo
        ft1, ft2 = st.columns(2)
        f_tiendas = ft1.multiselect(
            "Tienda",
            options=list(tienda_opts_map.keys()),
            format_func=lambda x: tienda_opts_map[x],
            key="pa_f_tiendas",
        )
        f_cc = limpiar_lista(ft2.text_area(
            "Centro de Costo (código, separados por coma)",
            key="pa_f_cc", height=68,
        ))

        st.markdown("---")

        # Fila 1: SKU / Nombre / Área / Línea / Sublínea
        fc1, fc2, fc3 = st.columns(3)
        f_sku       = limpiar_lista(fc1.text_area(
            "SKUs (separados por coma/espacio)", key="pa_f_sku", height=80))
        f_nombre    = fc1.text_input("Nombre producto (contiene)", key="pa_f_nombre")
        f_area      = fc2.multiselect("Área",      _opts("area"),      key="pa_f_area")
        f_linea     = fc2.multiselect("Línea",     _opts("linea"),     key="pa_f_linea")
        f_sublinea  = fc3.multiselect("Sublínea",  _opts("sublinea"),  key="pa_f_sublinea")

        # Fila 2: Marca / Proveedor / Mix / Procedencia
        fm1, fm2, fm3, fm4 = st.columns(4)
        f_marca       = fm1.multiselect("Marca",        _opts("marca"),       key="pa_f_marca")
        f_proveedor   = fm2.multiselect("Proveedor",    _opts("proveedor"),   key="pa_f_proveedor")
        f_mix         = fm3.multiselect("Mix",          _opts("mix"),         key="pa_f_mix")
        f_procedencia = fm4.multiselect("Procedencia",  _opts("procedencia"), key="pa_f_procedencia")

    # ── Tiendas activas según filtro ──────────────────────────────────────
    # Si el usuario filtra por tienda o por código CC, restringimos top_tiendas
    tiendas_activas = top_tiendas  # default: todas las top
    if f_tiendas:
        tiendas_activas = [t for t in top_tiendas if t in f_tiendas]
    if f_cc:
        # Los códigos CC se mapean a id_sucursal via _VCM_TO_SYNCRO
        syncro_from_cc = {_VCM_TO_SYNCRO[c] for c in f_cc if c in _VCM_TO_SYNCRO}
        tiendas_activas = [t for t in tiendas_activas if t in syncro_from_cc] or tiendas_activas

    # ── SKUs elegibles según filtros de producto ──────────────────────────
    mask_prod = pd.Series(True, index=df_dim_prod.index)
    if f_sku:
        mask_prod &= df_dim_prod["sku_producto"].str.upper().isin(
            [s.upper() for s in f_sku])
    if f_nombre:
        mask_prod &= df_dim_prod["nom_producto"].str.upper().str.contains(
            f_nombre.upper(), na=False)
    if f_area:
        mask_prod &= df_dim_prod["area"].isin(f_area)
    if f_linea:
        mask_prod &= df_dim_prod["linea"].isin(f_linea)
    if f_sublinea:
        mask_prod &= df_dim_prod["sublinea"].isin(f_sublinea)
    if f_marca:
        mask_prod &= df_dim_prod["marca"].isin(f_marca)
    if f_proveedor:
        mask_prod &= df_dim_prod["proveedor"].isin(f_proveedor)
    if f_mix:
        mask_prod &= df_dim_prod["mix"].isin(f_mix)
    if f_procedencia:
        mask_prod &= df_dim_prod["procedencia"].isin(f_procedencia)

    _filtros_activos = any([f_sku, f_nombre, f_area, f_linea, f_sublinea,
                            f_marca, f_proveedor, f_mix, f_procedencia])
    sku_elegibles: set[str] | None = (
        set(df_dim_prod.loc[mask_prod, "sku_producto"])
        if mask_prod.any() and _filtros_activos
        else None   # None = sin filtro, tomar todos
    )

    st.markdown("---")

    # ── Selectores (siempre visibles en sidebar) ──────────────────────────
    with st.sidebar:
        st.markdown("---")
        st.markdown("**⚙️ Proyecto A**")
        selected_tienda = st.selectbox(
            "Centro de Costo",
            options=tiendas_activas,
            format_func=lambda x: f"{nombre_tienda.get(x, 'Tienda')} — {x}",
            key="pa_tienda",
        )
        df_store_all = df_v26[df_v26["id_sucursal"] == selected_tienda]
        df_store_fil = (
            df_store_all[df_store_all["sku_producto"].isin(sku_elegibles)]
            if sku_elegibles is not None else df_store_all
        )
        top_skus_sel = (
            df_store_fil.groupby("sku_producto")["neto"]
            .sum().nlargest(N_TOP_SKUS).index.tolist()
        )
        sku_opts = {s: f"{nombre_sku.get(s, s)[:35]} ({s})" for s in top_skus_sel}
        selected_skus = st.multiselect(
            f"SKUs (top {N_TOP_SKUS} por Neta)",
            options=list(sku_opts.keys()),
            default=list(sku_opts.keys()),
            format_func=lambda x: sku_opts[x],
            key="pa_skus",
        )

    # ── BOTÓN CALCULAR — siempre visible ─────────────────────────────────
    _FLAG_KEY = "pa_calcular_flag"   # intención persistida en session_state

    col_btn, col_aviso = st.columns([2, 6])
    with col_btn:
        calcular_clicked = st.button(
            "🔄 Calcular reporte",
            type="primary",
            key="pa_calcular",
            help="Ejecuta la carga de stock y la simulación DDMRP",
        )

    # Persistir intención INMEDIATAMENTE (antes de cualquier return)
    # para que no se pierda si un return temprano interrumpe este rerun.
    if calcular_clicked:
        st.session_state[_FLAG_KEY] = True
        st.session_state.pop(_CACHE_KEY, None)   # invalidar cache previo

    # Validación post-botón
    if not selected_skus:
        with col_aviso:
            st.info("Selecciona al menos un SKU en el panel lateral.")
        return

    # Consumir el flag (True solo el rerun en que se debe calcular)
    should_compute = st.session_state.pop(_FLAG_KEY, False)

    if _CACHE_KEY not in st.session_state:
        if not should_compute:
            with col_aviso:
                st.info(
                    "Configura los parámetros arriba y haz clic en **🔄 Calcular reporte** "
                    "para generar el análisis. El resultado queda guardado hasta que "
                    "vuelvas a calcular."
                )
            return

        # ── Carga y simulación (solo al hacer clic) ───────────────────────
        with st.spinner("Cargando stock y simulando escenarios…"):
            df_vpre = _load_ventas_pre2026(conn)
            df_st   = _load_stock_tienda_2026(conn)
            df_cd   = _load_stock_cd_2026(conn)
            df_cv_all = _compute_cv_adu(df_vpre)

            precio_global: dict[str, float] = {
                sku: round(float(sub["neto"].sum() / sub["unidades"].sum()), 2)
                if sub["unidades"].sum() > 0 else 0.0
                for sku, sub in df_v26.groupby("sku_producto")
            }

            all_dates = pd.date_range(_FECHA_INICIO_STR, _FECHA_FIN_STR, freq="D")
            cd_pivot  = df_cd.pivot_table(
                index="fecha", columns="sku_producto",
                values="stock_cd_unidades", aggfunc="sum", fill_value=0,
            )
            cd_pivot.index = pd.to_datetime(cd_pivot.index)

            all_results: dict[str, dict[str, dict]] = {}
            for tienda in tiendas_activas:
                all_results[tienda] = {}
                cal    = review_days_map.get(tienda)
                lt_t   = lt_override.get(tienda, _avg_lt(cal))
                ciclo_t = ciclo_override.get(tienda, _avg_ciclo(cal))
                df_cv_t = df_cv_all[df_cv_all["id_sucursal"] == tienda]
                adu_t   = dict(zip(df_cv_t["sku_producto"], df_cv_t["adu"]))
                df_t    = df_v26[df_v26["id_sucursal"] == tienda]
                df_st_t = df_st[df_st["id_sucursal"] == tienda]
                df_t_fil = (
                    df_t[df_t["sku_producto"].isin(sku_elegibles)]
                    if sku_elegibles is not None else df_t
                )
                top_skus_t = (
                    df_t_fil.groupby("sku_producto")["neto"]
                    .sum().nlargest(N_TOP_SKUS).index.tolist()
                )

                for sku in top_skus_t:
                    ventas_sku = _safe_series(
                        df_t[df_t["sku_producto"] == sku].copy(),
                        "unidades", all_dates,
                    )
                    st_sku = _safe_series(
                        df_st_t[df_st_t["sku_producto"] == sku].copy(),
                        "stock_unidades", all_dates,
                    )
                    cd_sku = (
                        _norm_reindex(cd_pivot[sku], all_dates)
                        if sku in cd_pivot.columns
                        else pd.Series(0.0, index=all_dates)
                    )
                    adu_sku = adu_t.get(sku) or float(ventas_sku.mean()) or 0.01

                    sc1 = pd.DataFrame([{
                        "sku_producto": sku,
                        "id_sucursal":  tienda,
                        "fecha":        dt,
                        "ventas_real":  float(ventas_sku.get(dt, 0)),
                        "stock_tienda": float(st_sku.get(dt, 0)),
                        "quiebre_real": 1 if float(st_sku.get(dt, 0)) <= 0
                                          and float(ventas_sku.get(dt, 0)) > 0 else 0,
                    } for dt in all_dates])

                    stock_jan1 = float(st_sku.iloc[0]) if len(st_sku) > 0 else 0.0

                    # Moda por día de semana + serie de quiebres reales
                    # (se calculan antes de SC2 para poder usarlos en la simulación)
                    quiebre_real_ser = pd.Series(
                        {dt: (1 if float(st_sku.get(dt, 0)) <= 0
                                   and float(ventas_sku.get(dt, 0)) > 0 else 0)
                         for dt in all_dates},
                        dtype=float,
                    )
                    moda_wd = _compute_venta_moda(ventas_sku, quiebre_real_ser)

                    # Buffers DDMRP (para almacenar y colorear tablas)
                    eff_lt_sku    = _avg_lt(cal)    if cal else float(lt_t)
                    eff_ciclo_sku = _avg_ciclo(cal) if cal else float(ciclo_t)
                    red_b  = adu_sku * eff_lt_sku * 0.5
                    toy_b  = adu_sku * eff_lt_sku * 1.5   # Red + Yellow
                    tog_b  = toy_b + adu_sku * eff_ciclo_sku

                    # SC2: DDMRP con ventas reales pero demanda estimada en días de quiebre real
                    sc2 = _simulate_ddmrp(
                        ventas_ser       = ventas_sku,
                        stock_inicio     = stock_jan1,
                        stock_cd_ser     = cd_sku,
                        adu              = adu_sku,
                        review_days      = cal,
                        lt               = int(round(lt_t)),
                        ciclo            = int(round(ciclo_t)),
                        quiebre_real_ser = quiebre_real_ser,
                        mode_by_weekday  = moda_wd,
                    )
                    sc2["sku_producto"] = sku
                    sc2["id_sucursal"]  = tienda

                    # SC3: DDMRP con demanda pre-ajustada (quiebres → moda si CD tenía stock)
                    ventas_adj = _compute_ventas_ajustadas(ventas_sku, quiebre_real_ser, cd_sku, moda_wd)
                    sc3 = _simulate_ddmrp(
                        ventas_ser   = ventas_adj,
                        stock_inicio = stock_jan1,
                        stock_cd_ser = cd_sku,
                        adu          = adu_sku,
                        review_days  = cal,
                        lt           = int(round(lt_t)),
                        ciclo        = int(round(ciclo_t)),
                    )
                    sc3["sku_producto"] = sku
                    sc3["id_sucursal"]  = tienda
                    all_results[tienda][sku] = {
                        "sc1": sc1, "sc2": sc2, "sc3": sc3,
                        "moda_wd": moda_wd,
                        "red": red_b, "toy": toy_b, "tog": tog_b,
                    }

        st.session_state[_CACHE_KEY] = {
            "all_results":    all_results,
            "df_st":          df_st,
            "df_cd":          df_cd,
            "df_cv_all":      df_cv_all,
            "all_dates":      all_dates,
            "cd_pivot":       cd_pivot,
            "precio_global":  precio_global,
            "review_days_map":  review_days_map,
            "lt_override":      lt_override,
            "ciclo_override":   ciclo_override,
            "tiendas_activas":  tiendas_activas,
        }

    # ── Leer siempre desde el cache ───────────────────────────────────────
    _c               = st.session_state[_CACHE_KEY]
    all_results      = _c["all_results"]
    df_st            = _c["df_st"]
    df_cd            = _c["df_cd"]
    df_cv_all        = _c["df_cv_all"]
    all_dates        = _c["all_dates"]
    cd_pivot         = _c["cd_pivot"]
    precio_global    = _c["precio_global"]
    review_days_map  = _c["review_days_map"]
    lt_override      = _c["lt_override"]
    ciclo_override   = _c["ciclo_override"]
    tiendas_activas  = _c["tiendas_activas"]

    # Helpers para acceder a datos de la tienda seleccionada
    def _get_sc1(tienda, skus):
        parts = [all_results[tienda][s]["sc1"] for s in skus if s in all_results.get(tienda, {})]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    def _get_sc2(tienda, skus):
        parts = [all_results[tienda][s]["sc2"] for s in skus if s in all_results.get(tienda, {})]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    def _get_sc3(tienda, skus):
        parts = [all_results[tienda][s]["sc3"] for s in skus if s in all_results.get(tienda, {})]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    df_sc1 = _get_sc1(selected_tienda, selected_skus)
    df_sc2 = _get_sc2(selected_tienda, selected_skus)
    df_sc3 = _get_sc3(selected_tienda, selected_skus)

    # ADU/CV/VAR/Precio para tienda seleccionada
    df_cv_sel = df_cv_all[df_cv_all["id_sucursal"] == selected_tienda]
    adu_map   = dict(zip(df_cv_sel["sku_producto"], df_cv_sel["adu"]))
    var_map   = dict(zip(df_cv_sel["sku_producto"], df_cv_sel["variabilidad"]))
    cv_map    = dict(zip(df_cv_sel["sku_producto"], df_cv_sel["cv"]))
    for sku in selected_skus:
        if not adu_map.get(sku):
            avg = df_store_all[df_store_all["sku_producto"] == sku]["unidades"].mean()
            adu_map[sku] = round(float(avg) if pd.notna(avg) else 0.01, 4)
            var_map[sku] = "Alta"; cv_map[sku] = 999.0

    lt_sel    = lt_override.get(selected_tienda, LT_DEFAULT)
    ciclo_sel = ciclo_override.get(selected_tienda, CICLO_DEFAULT)
    cal_sel   = review_days_map.get(selected_tienda)
    tienda_label = nombre_tienda.get(selected_tienda, selected_tienda)

    st.info(
        f"**Tienda:** {tienda_label} ({selected_tienda})  ·  "
        f"**{len(selected_skus)} SKU(s)**  ·  "
        f"LT prom = {lt_sel} días  ·  Ciclo prom = {ciclo_sel} días  ·  "
        f"Días revisión: {_dias_str(cal_sel)}"
    )

    # ─── TABS ────────────────────────────────────────────────────────────
    tab_res, tab1, tab2, tab3 = st.tabs([
        "📋 Resumen",
        "📊 Escenario Real",
        "🚦 Escenario DDMRP",
        "📈 Comparativo",
    ])

    # ── TAB RESUMEN ───────────────────────────────────────────────────────
    with tab_res:
        st.markdown("### Resumen Comparativo — Real vs DDMRP")

        # Filtro dinámico de SKUs (afecta solo este tab)
        _all_skus_res = sorted({
            sku
            for t in tiendas_activas
            for sku in all_results.get(t, {})
        })
        _sku_disp_res = {s: f"{nombre_sku.get(s, s)[:45]} ({s})" for s in _all_skus_res}
        _sel_skus_res = st.multiselect(
            "Filtrar SKUs para el resumen",
            options=_all_skus_res,
            default=_all_skus_res,
            format_func=lambda x: _sku_disp_res[x],
            key="pa_res_skus",
        )
        _skus_res = _sel_skus_res if _sel_skus_res else _all_skus_res

        # ── Calcular métricas ────────────────────────────────────────────
        _tot_vr = 0.0;  _tot_vd = 0.0
        _tot_qr = 0;    _tot_qd = 0
        _daily_vr: dict = {};  _daily_vd: dict = {}
        _rows_det = []

        for _t in tiendas_activas:
            for _s in _skus_res:
                if _s not in all_results.get(_t, {}):
                    continue
                _d   = all_results[_t][_s]
                _sc1 = _d["sc1"];  _sc2 = _d["sc2"]
                _pr  = precio_global.get(_s, 0.0)

                _vr  = float(_sc1["ventas_real"].sum())
                _vd  = float(_sc2["ventas_posibles_ddmrp"].sum())
                _qr  = int(_sc1["quiebre_real"].sum())
                _qd  = int(_sc2["quiebre_ddmrp"].sum())

                _tot_vr += _vr;  _tot_vd += _vd
                _tot_qr += _qr;  _tot_qd += _qd

                # series diarias para gráficos
                for _, _row in _sc1.iterrows():
                    _dt = _row["fecha"]
                    _daily_vr[_dt] = _daily_vr.get(_dt, 0.0) + _row["ventas_real"]
                for _, _row in _sc2.iterrows():
                    _dt = _row["fecha"]
                    _daily_vd[_dt] = _daily_vd.get(_dt, 0.0) + _row["ventas_posibles_ddmrp"]

                _rows_det.append({
                    "Tienda":             nombre_tienda.get(_t, _t)[:25],
                    "SKU":                _s,
                    "Descripción":        nombre_sku.get(_s, _s)[:35],
                    "Venta Real (und)":   round(_vr, 0),
                    "Venta DDMRP (und)":  round(_vd, 0),
                    "Δ Venta (und)":      round(_vd - _vr, 0),
                    "Δ Venta (S/)":       round((_vd - _vr) * _pr, 0),
                    "Días quiebre Real":  _qr,
                    "Días quiebre DDMRP": _qd,
                    "Δ Quiebres":         _qr - _qd,
                    "% Reducción Q":      round((_qr - _qd) / _qr * 100, 1) if _qr else 0.0,
                })

        # ── KPIs ─────────────────────────────────────────────────────────
        _delta_v   = _tot_vd - _tot_vr
        _delta_q   = _tot_qr - _tot_qd
        _pct_v     = round(_delta_v / _tot_vr * 100, 1) if _tot_vr else 0.0
        _pct_q     = round(_delta_q / _tot_qr * 100, 1) if _tot_qr else 0.0
        _tot_s_vr  = sum(
            _sc1["ventas_real"].sum() * precio_global.get(_s, 0)
            for _t in tiendas_activas
            for _s, _d in all_results.get(_t, {}).items()
            if _s in _skus_res
            for _sc1 in [_d["sc1"]]
        )
        _tot_s_vd  = sum(
            _sc2["ventas_posibles_ddmrp"].sum() * precio_global.get(_s, 0)
            for _t in tiendas_activas
            for _s, _d in all_results.get(_t, {}).items()
            if _s in _skus_res
            for _sc2 in [_d["sc2"]]
        )
        _delta_s = _tot_s_vd - _tot_s_vr

        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("Venta Real (und)",    f"{_tot_vr:,.0f}")
        k2.metric("Venta DDMRP (und)",   f"{_tot_vd:,.0f}",
                  delta=f"{_delta_v:+,.0f} ({_pct_v:+.1f}%)",
                  delta_color="normal")
        k3.metric("Incremento (S/)",     f"S/ {_delta_s:,.0f}",
                  delta=f"{_pct_v:+.1f}%", delta_color="normal")
        k4.metric("Días quiebre Real",   f"{_tot_qr:,}")
        k5.metric("Días quiebre DDMRP",  f"{_tot_qd:,}",
                  delta=f"{_tot_qd - _tot_qr:+,}", delta_color="inverse")
        k6.metric("Reducción quiebres",  f"{_pct_q:.1f}%",
                  delta=f"{_delta_q:+,} días", delta_color="normal")

        st.markdown("---")

        # ── Gráfico 1: Ventas diarias acumuladas Real vs DDMRP ────────────
        if _daily_vr or _daily_vd:
            _df_daily = pd.DataFrame({
                "Real":  pd.Series(_daily_vr),
                "DDMRP": pd.Series(_daily_vd),
            }).sort_index().fillna(0)
            _df_cumul = _df_daily.cumsum()

            fig_cumul = go.Figure()
            fig_cumul.add_scatter(
                x=_df_cumul.index, y=_df_cumul["Real"],
                name="Venta acumulada Real",
                line=dict(color="#E53935", dash="dash", width=2),
                fill=None,
            )
            fig_cumul.add_scatter(
                x=_df_cumul.index, y=_df_cumul["DDMRP"],
                name="Venta acumulada DDMRP",
                line=dict(color="#43A047", width=2.5),
                fill="tonexty", fillcolor="rgba(67,160,71,0.08)",
            )
            fig_cumul.update_layout(
                dorel_layout(title="Ventas acumuladas Ene–Mar 2026: Real vs DDMRP"),
                yaxis_title="Unidades acumuladas", height=320,
                legend=dict(orientation="h", yanchor="bottom", y=1.02, x=1, xanchor="right"),
            )
            st.plotly_chart(fig_cumul, use_container_width=True)

        # ── Gráfico 2: Días quiebre por SKU ───────────────────────────────
        if _rows_det:
            _df_det = pd.DataFrame(_rows_det)
            _df_q   = (
                _df_det.groupby("Descripción")[["Días quiebre Real", "Días quiebre DDMRP"]]
                .sum()
                .sort_values("Días quiebre Real", ascending=True)
            )
            fig_q = go.Figure()
            fig_q.add_bar(
                y=_df_q.index, x=_df_q["Días quiebre Real"],
                name="Real", orientation="h",
                marker_color="#E53935",
                text=_df_q["Días quiebre Real"].astype(int),
                textposition="outside",
            )
            fig_q.add_bar(
                y=_df_q.index, x=_df_q["Días quiebre DDMRP"],
                name="DDMRP", orientation="h",
                marker_color="#43A047",
                text=_df_q["Días quiebre DDMRP"].astype(int),
                textposition="outside",
            )
            fig_q.update_layout(
                dorel_layout(title="Días de quiebre por SKU: Real vs DDMRP"),
                barmode="group", xaxis_title="Días", height=max(280, len(_df_q) * 52),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                margin=dict(l=10, r=60),
            )
            st.plotly_chart(fig_q, use_container_width=True)

            # ── Tabla detalle ─────────────────────────────────────────────
            st.markdown("##### Detalle por tienda × SKU")

            def _hl_resumen(row):
                s = [""] * len(row)
                cols = _df_det.columns.tolist()
                for _col, _good in [("Δ Venta (und)", True), ("Δ Quiebres", True),
                                     ("% Reducción Q", True)]:
                    if _col in cols:
                        i = cols.index(_col)
                        v = row.iloc[i]
                        if _good:
                            s[i] = ("background-color:#C8E6C9;color:#1B5E20;font-weight:700"
                                    if v > 0 else
                                    "background-color:#FFCDD2;color:#B71C1C;font-weight:700"
                                    if v < 0 else "")
                return s

            st.dataframe(
                _df_det.style.apply(_hl_resumen, axis=1)
                .format({
                    "Venta Real (und)":   "{:.0f}",
                    "Venta DDMRP (und)":  "{:.0f}",
                    "Δ Venta (und)":      "{:+.0f}",
                    "Δ Venta (S/)":       "S/ {:+,.0f}",
                    "Días quiebre Real":  "{:.0f}",
                    "Días quiebre DDMRP": "{:.0f}",
                    "Δ Quiebres":         "{:+.0f}",
                    "% Reducción Q":      "{:.1f}%",
                }),
                use_container_width=True, hide_index=True,
            )
            download_buttons(_df_det, prefix="resumen_ddmrp")

    # ── TAB 1: Escenario Real ─────────────────────────────────────────────
    with tab1:
        st.markdown(f"### Escenario Real — {tienda_label}")
        st.caption(
            "**Stock Tienda**: cierre de día (solo esa sucursal).  "
            "**Abastecimiento**: `stock[hoy] − stock[ayer] + ventas[hoy]`.  "
            "**Stock CD**: unidades en CD ese día.  "
            "**Quiebre CD = 1**: CD tenía stock disponible.  "
            "**Quiebre = 1**: stock tienda = 0 con demanda."
        )
        if not df_sc1.empty:
            total_q_real = int(df_sc1["quiebre_real"].sum())
            # Quiebres donde CD tenía stock disponible
            quiebres_df = df_sc1[df_sc1["quiebre_real"] == 1]
            dias_cd_ok  = int((quiebres_df.merge(
                df_cd[["fecha","sku_producto","stock_cd_unidades"]],
                on=["fecha","sku_producto"], how="left"
            )["stock_cd_unidades"].fillna(0) > 0).sum())
            c1, c2, c3 = st.columns(3)
            c1.metric("Días-quiebre (∑ SKUs)", f"{total_q_real:,}")
            c2.metric("Quiebres con CD disponible", f"{dias_cd_ok:,}")
            c3.metric("% evitables", f"{dias_cd_ok/total_q_real*100:.1f}%" if total_q_real else "0%")

        for sku in selected_skus:
            if sku not in all_results.get(selected_tienda, {}):
                continue
            label      = f"{nombre_sku.get(sku, sku)[:40]} ({sku})"
            sc1_s      = all_results[selected_tienda][sku]["sc1"]
            df_st_sel  = df_st[df_st["id_sucursal"] == selected_tienda]
            df_tbl     = _make_sc1_table(sc1_s, df_st_sel, sku, label, all_dates, cd_pivot)
            st.markdown(f"#### {label}")
            with st.expander("Ver tabla diaria", expanded=True):
                st.dataframe(_style_table(df_tbl), use_container_width=True, height=250,
                             column_config={"SKU": st.column_config.TextColumn(width="medium")})
            download_buttons(df_tbl.drop(columns=["SKU"]), prefix=f"real_{sku}")

    # ── TAB 2: Escenario DDMRP ────────────────────────────────────────────
    with tab2:
        st.markdown(f"### Escenario DDMRP — {tienda_label}")
        st.caption(
            "Stock inicial = real al 01-ene-2026.  "
            f"Días de revisión: **{_dias_str(cal_sel)}**.  "
            "Reposición efectiva solo si CD tenía stock.  "
            "**Ventas**: días con quiebre DDMRP muestran la moda del día de semana "
            "(venta potencial si hubiéramos tenido stock)."
        )
        if not df_sc2.empty:
            total_q_real  = int(df_sc1["quiebre_real"].sum()) if not df_sc1.empty else 0
            total_q_ddmrp = int(df_sc2["quiebre_ddmrp"].sum())
            total_abast   = df_sc2["abastecimiento_ddmrp"].sum()
            dif           = total_q_real - total_q_ddmrp
            c1, c2, c3 = st.columns(3)
            c1.metric("Días-quiebre DDMRP",       f"{total_q_ddmrp:,}")
            c2.metric("Evitados vs Real", f"{dif:,}", delta=f"{dif:+,}",
                      delta_color="normal" if dif > 0 else "inverse")
            c3.metric("Unidades repuestas",        f"{total_abast:,.0f}")

        for sku in selected_skus:
            if sku not in all_results.get(selected_tienda, {}):
                continue
            label   = f"{nombre_sku.get(sku, sku)[:40]} ({sku})"
            res_sku = all_results[selected_tienda][sku]
            sc2_s   = res_sku["sc2"]
            sc1_s   = res_sku["sc1"]
            red_b   = res_sku.get("red", 0.0)
            toy_b   = res_sku.get("toy", 0.0)
            moda_b  = res_sku.get("moda_wd")
            df_tbl  = _make_sc2_table(sc2_s, label, all_dates, red_val=red_b, toy_val=toy_b, moda_wd=moda_b)
            st.markdown(f"#### {label}")
            with st.expander("Ver tabla diaria", expanded=True):
                st.dataframe(
                    _style_ddmrp_table(df_tbl, red_val=red_b, toy_val=toy_b),
                    use_container_width=True, height=300,
                    column_config={"SKU": st.column_config.TextColumn(width="medium")},
                )
            download_buttons(df_tbl.drop(columns=["SKU"]), prefix=f"ddmrp_{sku}")

            sc1_plot = sc1_s.sort_values("fecha")
            fig = go.Figure()
            fig.add_scatter(x=sc1_plot["fecha"], y=sc1_plot["stock_tienda"],
                            name="Stock Real", line=dict(color="#E53935", dash="dash", width=1.5))
            fig.add_scatter(x=sc2_s["fecha"], y=sc2_s["stock_fin_ddmrp"],
                            name="Stock DDMRP", line=dict(color="#43A047", width=2))
            fig.add_bar(x=sc2_s["fecha"], y=sc2_s["abastecimiento_ddmrp"],
                        name="Reposición DDMRP", marker_color="#2196F3", opacity=0.45)
            fig.update_layout(dorel_layout(title=f"Stock — {nombre_sku.get(sku, sku)[:30]}"),
                              yaxis_title="Unidades", height=280, barmode="overlay",
                              legend=dict(orientation="h", yanchor="bottom", y=1.02, x=1, xanchor="right"))
            st.plotly_chart(fig, use_container_width=True)

    # ── TAB 3: Comparativo ────────────────────────────────────────────────
    with tab3:
        st.markdown("### Comparativo: Real vs DDMRP")

        # ── Nivel general: todas las tiendas × sus top SKUs ──────────────
        st.markdown("#### Nivel General — todas las tiendas")
        gen_rows = []
        for tienda in tiendas_activas:
            t_nom = nombre_tienda.get(tienda, tienda)
            df_cv_t = df_cv_all[df_cv_all["id_sucursal"] == tienda]
            adu_t   = dict(zip(df_cv_t["sku_producto"], df_cv_t["adu"]))
            for sku, data in all_results.get(tienda, {}).items():
                adu   = adu_t.get(sku) or float(df_v26[(df_v26["id_sucursal"] == tienda) & (df_v26["sku_producto"] == sku)]["unidades"].mean() or 0.01)
                prec  = precio_global.get(sku, 0)
                dq_r  = int(data["sc1"]["quiebre_real"].sum())
                dq_d  = int(data["sc2"]["quiebre_ddmrp"].sum())
                red_  = round((dq_r - dq_d) / dq_r * 100, 1) if dq_r > 0 else 0.0
                vp_r  = int(dq_r * adu * prec)
                vp_d  = int(dq_d * adu * prec)
                gen_rows.append({
                    "Tienda":                  t_nom[:30],
                    "SKU":                     sku,
                    "Descripción":             nombre_sku.get(sku, sku)[:30],
                    "ADU":                     round(adu, 3),
                    "Días quiebre Real":       dq_r,
                    "Días quiebre DDMRP":      dq_d,
                    "Reducción (%)":           red_,
                    "Venta perdida Real (S/)": vp_r,
                    "Venta perd. DDMRP (S/)":  vp_d,
                    "Ahorro (S/)":             vp_r - vp_d,
                })

        df_gen = pd.DataFrame(gen_rows)
        if not df_gen.empty:
            tot_r = int(df_gen["Días quiebre Real"].sum())
            tot_d = int(df_gen["Días quiebre DDMRP"].sum())
            tot_a = int(df_gen["Ahorro (S/)"].sum())
            pct   = round((tot_r - tot_d) / tot_r * 100, 1) if tot_r > 0 else 0
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Días quiebre Real (total)",  f"{tot_r:,}")
            c2.metric("Días quiebre DDMRP (total)", f"{tot_d:,}",
                      delta=f"{tot_d - tot_r:+,}", delta_color="inverse")
            c3.metric("Mejora global DDMRP",        f"{pct}%",
                      delta=f"{pct:+.1f}%", delta_color="normal")
            c4.metric("Ahorro total venta perdida", f"S/ {tot_a:,.0f}")

            def _hl_gen(row):
                n = len(row)
                s = [""] * n
                ir = df_gen.columns.get_loc("Días quiebre Real")
                id_ = df_gen.columns.get_loc("Días quiebre DDMRP")
                if row.iloc[id_] < row.iloc[ir]:
                    s[id_] = "background-color:#E8F5E9;color:#2E7D32;font-weight:700"
                elif row.iloc[id_] > row.iloc[ir]:
                    s[id_] = "background-color:#FFEBEE;color:#C62828;font-weight:700"
                return s

            st.dataframe(df_gen.style.apply(_hl_gen, axis=1),
                         use_container_width=True, hide_index=True)
            download_buttons(df_gen, prefix="comparativo_general")

            # Gráfico por tienda (agrupado)
            fig_g = go.Figure()
            t_labels = df_gen.groupby("Tienda")[["Días quiebre Real", "Días quiebre DDMRP"]].sum().reset_index()
            fig_g.add_bar(name="Real",  x=t_labels["Tienda"], y=t_labels["Días quiebre Real"],
                          marker_color="#E53935", text=t_labels["Días quiebre Real"], textposition="outside")
            fig_g.add_bar(name="DDMRP", x=t_labels["Tienda"], y=t_labels["Días quiebre DDMRP"],
                          marker_color="#43A047", text=t_labels["Días quiebre DDMRP"], textposition="outside")
            fig_g.update_layout(dorel_layout(title="Días quiebre por tienda: Real vs DDMRP"),
                                barmode="group", yaxis_title="Días", height=340,
                                legend=dict(orientation="h", yanchor="bottom", y=1.02))
            st.plotly_chart(fig_g, use_container_width=True)

        st.markdown("---")

        # ── Detalle filtrado por tienda + SKU ──────────────────────────
        st.markdown("#### Detalle por Tienda y SKU")
        dc1, dc2 = st.columns(2)
        det_tienda = dc1.selectbox(
            "Tienda",
            options=tiendas_activas,
            format_func=lambda x: f"{nombre_tienda.get(x, x)} ({x})",
            key="pa_comp_tienda",
        )
        skus_det_opts = list(all_results.get(det_tienda, {}).keys())
        det_sku = dc2.selectbox(
            "SKU",
            options=skus_det_opts,
            format_func=lambda x: f"{nombre_sku.get(x, x)[:35]} ({x})",
            key="pa_comp_sku",
        ) if skus_det_opts else None

        if det_sku and det_sku in all_results.get(det_tienda, {}):
            data    = all_results[det_tienda][det_sku]
            sc1_det = data["sc1"]
            sc2_det = data["sc2"]
            cal_det = review_days_map.get(det_tienda)
            df_cv_d = df_cv_all[df_cv_all["id_sucursal"] == det_tienda]
            adu_det = float(df_cv_d[df_cv_d["sku_producto"] == det_sku]["adu"].iloc[0]) if not df_cv_d[df_cv_d["sku_producto"] == det_sku].empty else float(df_v26[(df_v26["id_sucursal"] == det_tienda) & (df_v26["sku_producto"] == det_sku)]["unidades"].mean() or 0.01)
            var_det = df_cv_d[df_cv_d["sku_producto"] == det_sku]["variabilidad"].iloc[0] if not df_cv_d[df_cv_d["sku_producto"] == det_sku].empty else "Alta"
            lt_det  = lt_override.get(det_tienda, LT_DEFAULT)
            cyc_det = ciclo_override.get(det_tienda, CICLO_DEFAULT)

            dq_r  = int(sc1_det["quiebre_real"].sum())
            dq_d  = int(sc2_det["quiebre_ddmrp"].sum())
            prec  = precio_global.get(det_sku, 0)
            vp_r  = int(dq_r * adu_det * prec)
            vp_d  = int(dq_d * adu_det * prec)

            st.markdown(
                f"**{nombre_tienda.get(det_tienda, det_tienda)}** — "
                f"**{nombre_sku.get(det_sku, det_sku)[:40]}** ({det_sku})  ·  "
                f"Variabilidad: **{var_det}**  ·  ADU: **{adu_det:.3f}**  ·  "
                f"Días revisión: **{_dias_str(cal_det)}**  ·  "
                f"LT={lt_det} d  Ciclo={cyc_det} d"
            )
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Días quiebre Real",  dq_r)
            m2.metric("Días quiebre DDMRP", dq_d,
                      delta=f"{dq_d - dq_r:+}", delta_color="inverse")
            m3.metric("Venta perdida Real",  f"S/ {vp_r:,.0f}")
            m4.metric("Ahorro DDMRP",        f"S/ {vp_r - vp_d:,.0f}",
                      delta=f"S/ {vp_r - vp_d:+,.0f}", delta_color="normal")

            # Tablas diarias del SKU seleccionado
            col_t1, col_t2 = st.columns(2)
            with col_t1:
                st.markdown("**Escenario Real**")
                df_tbl1 = _make_sc1_table(
                    sc1_det,
                    df_st[df_st["id_sucursal"] == det_tienda],
                    det_sku, nombre_sku.get(det_sku, det_sku)[:30], all_dates, cd_pivot,
                )
                with st.expander("Tabla diaria Real", expanded=True):
                    st.dataframe(_style_table(df_tbl1), use_container_width=True, height=240)
            with col_t2:
                st.markdown("**Escenario DDMRP**")
                df_tbl2 = _make_sc2_table(sc2_det, nombre_sku.get(det_sku, det_sku)[:30], all_dates)
                with st.expander("Tabla diaria DDMRP", expanded=True):
                    st.dataframe(_style_table(df_tbl2), use_container_width=True, height=240)

            # Gráfico comparativo del SKU
            fig_d = go.Figure()
            fig_d.add_scatter(x=sc1_det["fecha"], y=sc1_det["stock_tienda"],
                              name="Stock Real", line=dict(color="#E53935", dash="dash", width=1.5))
            fig_d.add_scatter(x=sc2_det["fecha"], y=sc2_det["stock_fin_ddmrp"],
                              name="Stock DDMRP", line=dict(color="#43A047", width=2))
            fig_d.add_bar(x=sc2_det["fecha"], y=sc2_det["abastecimiento_ddmrp"],
                          name="Reposición DDMRP", marker_color="#2196F3", opacity=0.4)
            fig_d.update_layout(
                dorel_layout(title=f"{nombre_sku.get(det_sku, det_sku)[:30]} — Stock Real vs DDMRP"),
                yaxis_title="Unidades", height=320, barmode="overlay",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, x=1, xanchor="right"),
            )
            st.plotly_chart(fig_d, use_container_width=True)

            # Parámetros DDMRP del SKU
            with st.expander("Parámetros buffer DDMRP"):
                red_v = round(adu_det * lt_det * 0.5, 2)
                yel_v = round(adu_det * lt_det, 2)
                grn_v = round(adu_det * cyc_det, 2)
                st.dataframe(pd.DataFrame([{
                    "CV":          round(df_cv_d[df_cv_d["sku_producto"] == det_sku]["cv"].iloc[0] if not df_cv_d[df_cv_d["sku_producto"] == det_sku].empty else 999, 3),
                    "Variabilidad": var_det,
                    "ADU":         round(adu_det, 4),
                    "LT prom":     lt_det,
                    "Ciclo prom":  cyc_det,
                    "Red":         red_v,
                    "Yellow":      yel_v,
                    "Green":       grn_v,
                    "TOY":         round(red_v + yel_v, 2),
                    "TOG":         round(red_v + yel_v + grn_v, 2),
                }]), use_container_width=True, hide_index=True)
        else:
            st.info("Selecciona una tienda y SKU para ver el detalle.")
