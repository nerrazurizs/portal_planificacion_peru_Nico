"""Shared filtering and parsing utilities."""


def limpiar_lista(texto):
    """Parse a text input (comma/space/newline/tab/semicolon separated) into a clean uppercase list."""
    if not texto:
        return []
    texto = texto.replace("\n", ",").replace("\t", ",").replace(";", ",")
    return [x.strip().upper() for x in texto.split(",") if x.strip()]


def clasificar_canal(canal, descripcion, cod_bodega=None):
    """Classify a store/warehouse into a detailed channel category.

    Bodega 1100008 (B2C Etail) = stock virtual asignado a etail via repo automatica.
    Se clasifica explicitamente para distinguirla del etail transaccional.
    """
    c = str(canal).upper()
    d = str(descripcion).upper()
    # B2C Etail: bodega virtual de asignacion automatica para canal etail
    if cod_bodega == "1100008" or "B2C" in d:
        return "B2C ETAIL"
    if "TIENDA" in c:
        return "TIENDA"
    if "ETAIL" in c:
        return "ETAIL"
    keywords_inmovilizado = ["SEGUNDA", "MERMA", "OBSOLESCENCIA", "LIQUIDACION", "DESTRUCCION"]
    if any(k in d for k in keywords_inmovilizado):
        return "CD INMOVILIZADO"
    if "BLUEXPRESS" in d or "CD" in c:
        return "CD OPERATIVO"
    return "OTROS"


def norm_cols(df):
    """Normalize DataFrame column names to uppercase stripped."""
    df.columns = [str(c).strip().upper() for c in df.columns]
    return df


def human_format(num, pos=None):
    """Format large numbers with K/M/B suffixes. Compatible with matplotlib FuncFormatter."""
    if num is None:
        return "0"
    magnitude = 0
    while abs(num) >= 1000:
        magnitude += 1
        num /= 1000.0
    return "%.1f%s" % (num, ["", "K", "M", "B", "T"][magnitude])


def fmt_clp(val, decimals: int = 0) -> str:
    """Format a value as Chilean Pesos: $25.450 (dot as thousands separator).

    Use on DataFrame columns BEFORE passing to st.dataframe(), then display
    with st.column_config.TextColumn instead of NumberColumn.
    """
    import pandas as pd
    if pd.isna(val) or val == 0:
        return "$0"
    neg = float(val) < 0
    if decimals > 0:
        formatted = f"{abs(float(val)):,.{decimals}f}".replace(",", ".")
    else:
        formatted = f"{abs(float(val)):,.0f}".replace(",", ".")
    return f"-${formatted}" if neg else f"${formatted}"


def enrich_with_abc_xyz_fsn(df, conn):
    """Merge ABC-XYZ-FSN classification onto any DataFrame with SKU_PRODUCTO.

    Uses the centralized cached lookup from db.cache.  Adds columns:
    CLASE_ABC, CLASE_XYZ, CLASE_FSN, CLASE_COMBINADA.
    Safe to call multiple times (drops existing columns before merge).
    """
    if "SKU_PRODUCTO" not in df.columns:
        return df
    from db.cache import cached_query as cq
    lookup = cq.abc_xyz_fsn(conn)
    merge_cols = [c for c in ["SKU_PRODUCTO", "CLASE_ABC", "CLASE_XYZ", "CLASE_FSN", "CLASE_COMBINADA"]
                  if c in lookup.columns]
    # Drop existing classification columns to avoid _x/_y suffixes
    existing = [c for c in merge_cols if c in df.columns and c != "SKU_PRODUCTO"]
    if existing:
        df = df.drop(columns=existing)
    return df.merge(lookup[merge_cols], on="SKU_PRODUCTO", how="left")


def fmt_usd(val, decimals: int = 2) -> str:
    """Format a value as USD: $25,450.00 (comma as thousands separator)."""
    import pandas as pd
    if pd.isna(val) or val == 0:
        return "$0"
    neg = float(val) < 0
    formatted = f"{abs(float(val)):,.{decimals}f}"
    return f"-${formatted}" if neg else f"${formatted}"


def calcular_moi_ajustado(df, ventas_px, stock_cd_diario=None, min_dias=15,
                          instock_pct_min=0.70):
    """Calculate adjusted MOI using censored demand from VCM sales data.

    Two-pass algorithm when stock_cd_diario is provided:
      Pass 1: Calculate clean daily demand using only days with stock_CD > 0
              (binary filter, no circularity).
      Pass 2: Compute InStock CD % per month (demand-based: stock >= demand_diaria).
              Only months with InStock >= instock_pct_min are "good" months.
      Pass 3: MOI Ajustado uses only "good" months' COGS.

    Fallback (no stock_cd_diario): original logic (months with venta > 0).

    Columns added to df:
        MOI_6M           - MOI bruto 6 meses (COGS total / 6)
        ROT_6M           - Rotacion und/mes 6m
        MOI_3M           - MOI bruto 3 meses
        ROT_3M           - Rotacion und/mes 3m
        MOI_AJUST        - MOI ajustado (COGS diario de meses con stock x 30.44)
        ROT_AJUST        - Rotacion ajustada und/mes
        MESES_CON_VENTA  - Meses con venta > 0 en ultimos 6m
        MESES_LOOKBACK   - Meses totales en lookback
        DIAS_CON_STOCK   - Dias calendario de meses con venta
        TASA_DISP        - MESES_CON_VENTA / MESES_LOOKBACK
        INSTOCK_CD_PCT   - % promedio InStock CD (demand-based) en ultimos 6m
        CONFIAB_MOI      - Indicador de confiabilidad
        MOI_CLASIF       - MOI para clasificaciones (ajustado si >=min_dias, sino bruto)

    Parameters
    ----------
    df : DataFrame with SKU_PRODUCTO, STOCK_COSTO (and optionally MOI preexisting).
    ventas_px : DataFrame from QUERY_VENTAS_MENSUAL_PRECIO (SKU x CANAL x MES).
    stock_cd_diario : DataFrame from QUERY_STOCK_CD_DIARIO_6M (SKU x FECHA x STOCK_CD_UND).
        If provided, enables InStock-based demand filtering (two-pass).
    min_dias : int, minimum days with stock to trust adjusted MOI (default 15).
    instock_pct_min : float, minimum InStock CD % to consider a month "good" (default 0.70).

    Returns
    -------
    df with new columns added.
    """
    import numpy as np
    import pandas as pd

    _stk_raw = df["STOCK_COSTO"] if "STOCK_COSTO" in df.columns else pd.Series(0, index=df.index)
    _stk = pd.to_numeric(_stk_raw, errors="coerce").fillna(0)
    _moi_raw = df["MOI"] if "MOI" in df.columns else (df["MOI_HIST"] if "MOI_HIST" in df.columns else pd.Series(0, index=df.index))
    _moi_orig = pd.to_numeric(_moi_raw, errors="coerce").fillna(0)

    # Defaults
    for _c, _v in [("MOI_6M", _moi_orig), ("MOI_3M", _moi_orig),
                    ("MOI_AJUST", _moi_orig), ("ROT_6M", 0), ("ROT_3M", 0),
                    ("ROT_AJUST", 0), ("MESES_CON_VENTA", 0), ("MESES_LOOKBACK", 0),
                    ("DIAS_CON_STOCK", 0), ("TASA_DISP", 1.0), ("INSTOCK_CD_PCT", 0.0),
                    ("CONFIAB_MOI", "Confiable"), ("MOI_CLASIF", _moi_orig)]:
        df[_c] = _v

    if ventas_px.empty or "SKU_PRODUCTO" not in ventas_px.columns:
        return df

    _vpx = ventas_px.copy()
    _vpx["PERIODO"] = pd.to_datetime(_vpx.get("PERIODO"), errors="coerce")
    _vpx["CANTIDAD"] = pd.to_numeric(_vpx.get("CANTIDAD", 0), errors="coerce").fillna(0)
    for _vc in ["NETO", "APORTE"]:
        if _vc in _vpx.columns:
            _vpx[_vc] = pd.to_numeric(_vpx[_vc], errors="coerce").fillna(0)

    # COGS
    if "NETO" in _vpx.columns and "APORTE" in _vpx.columns:
        _vpx["_COGS"] = _vpx["NETO"] - _vpx["APORTE"]
    elif "NETO" in _vpx.columns:
        _vpx["_COGS"] = _vpx["NETO"]
    else:
        return df  # can't compute without NETO

    # 6 meses completos (inicio de mes para no cortar el mes mas antiguo)
    _cut6 = (pd.Timestamp.now().normalize() - pd.DateOffset(months=6)).replace(day=1)
    _vpx6 = _vpx[_vpx["PERIODO"] >= _cut6]
    if _vpx6.empty:
        _vpx6 = _vpx

    # Colapsar canales -> 1 fila por SKU x MES
    _agg_dict = {"CANTIDAD": ("CANTIDAD", "sum"), "_COGS": ("_COGS", "sum")}
    if "DIAS_CON_DATO" in _vpx6.columns:
        _agg_dict["_DIAS_REAL"] = ("DIAS_CON_DATO", "max")
    _mensual = _vpx6.groupby(["SKU_PRODUCTO", "PERIODO"], as_index=False).agg(**_agg_dict)
    # Mes actual (parcial): usar dias reales transcurridos, no calendario
    _mensual["_DIAS_MES"] = _mensual["PERIODO"].dt.days_in_month.fillna(30)
    _mes_actual = pd.Timestamp.now().normalize().replace(day=1)
    if "_DIAS_REAL" in _mensual.columns:
        _is_current = _mensual["PERIODO"] == _mes_actual
        _mensual.loc[_is_current, "_DIAS_MES"] = _mensual.loc[_is_current, "_DIAS_REAL"]
        _mensual.drop(columns=["_DIAS_REAL"], inplace=True)

    # InStock CD-based filtering (two-pass) when stock_cd_diario available
    _has_cd_stock = (stock_cd_diario is not None
                     and not stock_cd_diario.empty
                     and "SKU_PRODUCTO" in stock_cd_diario.columns)

    if _has_cd_stock:
        _scd = stock_cd_diario.copy()
        _scd["FECHA"] = pd.to_datetime(_scd.get("FECHA"), errors="coerce")
        for _sc in ["STOCK_CD_UND", "VTA_UND", "VTA_NETO", "VTA_COGS"]:
            if _sc in _scd.columns:
                _scd[_sc] = pd.to_numeric(_scd[_sc], errors="coerce").fillna(0)
            else:
                _scd[_sc] = 0
        _scd = _scd[_scd["FECHA"] >= _cut6]

        # PASS 1: Demanda diaria desde top 50% meses por volumen
        _scd["_MES"] = _scd["FECHA"].dt.to_period("M")

        _vta_m = _mensual[_mensual["CANTIDAD"] > 0].copy()
        if not _vta_m.empty:
            _mediana = _vta_m.groupby("SKU_PRODUCTO")["CANTIDAD"].transform("median")
            _top = _vta_m[_vta_m["CANTIDAD"] >= _mediana]
            _pass1 = _top.groupby("SKU_PRODUCTO").agg(
                _VTA_TOP=("CANTIDAD", "sum"),
                _DIAS_TOP=("_DIAS_MES", "sum"),
                _MESES_TOP=("PERIODO", "nunique"),
            ).reset_index()
            _pass1["_DEM_DIARIA"] = np.where(
                _pass1["_DIAS_TOP"] >= min_dias,
                _pass1["_VTA_TOP"] / _pass1["_DIAS_TOP"],
                0.0,
            )
            _pass1["_DIAS_CD_STOCK"] = _pass1["_DIAS_TOP"].astype(int)
        else:
            _pass1 = pd.DataFrame({
                "SKU_PRODUCTO": pd.Series(dtype=str),
                "_DIAS_CD_STOCK": pd.Series(dtype=int),
                "_DEM_DIARIA": pd.Series(dtype=float),
            })

        # SKUs sin demanda calculable: fallback a media simple
        _skus_en_pass1 = set(_pass1[_pass1["_DEM_DIARIA"] > 0]["SKU_PRODUCTO"])
        _all_skus_scd = set(_scd["SKU_PRODUCTO"].unique())
        _skus_insuf = _all_skus_scd - _skus_en_pass1

        if _skus_insuf:
            _fb_m = _mensual[
                _mensual["SKU_PRODUCTO"].isin(_skus_insuf) & (_mensual["CANTIDAD"] > 0)
            ].copy()
            if not _fb_m.empty:
                _fb = _fb_m.groupby("SKU_PRODUCTO").agg(
                    _VTA_TOP=("CANTIDAD", "sum"),
                    _DIAS_TOP=("_DIAS_MES", "sum"),
                ).reset_index()
                _fb["_DEM_DIARIA"] = np.where(
                    _fb["_DIAS_TOP"] > 0, _fb["_VTA_TOP"] / _fb["_DIAS_TOP"], 0.0,
                )
                _fb["_DIAS_CD_STOCK"] = 0
                _pass1 = pd.concat([
                    _pass1[~_pass1["SKU_PRODUCTO"].isin(_skus_insuf)],
                    _fb[["SKU_PRODUCTO", "_DIAS_CD_STOCK", "_DEM_DIARIA"]],
                ], ignore_index=True)

        # PASS 2: InStock CD % mensual (demand-based)
        _scd = _scd.merge(
            _pass1[["SKU_PRODUCTO", "_DEM_DIARIA"]],
            on="SKU_PRODUCTO", how="left",
        )
        _scd["_DEM_DIARIA"] = _scd["_DEM_DIARIA"].fillna(0)
        _scd["_IS_DAY"] = (
            (_scd["STOCK_CD_UND"] >= _scd["_DEM_DIARIA"]) & (_scd["_DEM_DIARIA"] > 0)
        ).astype(int)

        _is_mensual = _scd.groupby(["SKU_PRODUCTO", "_MES"]).agg(
            _IS_DAYS=("_IS_DAY", "sum"),
            _TOTAL_DAYS=("FECHA", "count"),
        ).reset_index()
        _is_mensual["_IS_PCT"] = np.where(
            _is_mensual["_TOTAL_DAYS"] > 0,
            _is_mensual["_IS_DAYS"] / _is_mensual["_TOTAL_DAYS"],
            0.0,
        )
        _is_mensual["PERIODO"] = _is_mensual["_MES"].dt.to_timestamp()

        _mensual = _mensual.merge(
            _is_mensual[["SKU_PRODUCTO", "PERIODO", "_IS_PCT"]],
            on=["SKU_PRODUCTO", "PERIODO"], how="left",
        )
        _mensual["_IS_PCT"] = _mensual["_IS_PCT"].fillna(0.0)

        _avg_is = _is_mensual.groupby("SKU_PRODUCTO")["_IS_PCT"].mean().reset_index()
        _avg_is.columns = ["SKU_PRODUCTO", "_AVG_IS_PCT"]

        # PASS 3: Top 50% meses por volumen + cobertura stock CD
        _con_venta = _mensual[_mensual["CANTIDAD"] > 0].copy()
        if not _con_venta.empty:
            _med_sku = _con_venta.groupby("SKU_PRODUCTO")["CANTIDAD"].transform("median")
            _buenos = _con_venta[_con_venta["CANTIDAD"] >= _med_sku].copy()

            _stk_m = _scd.groupby(["SKU_PRODUCTO", "_MES"]).agg(
                _AVG_CD=("STOCK_CD_UND", "mean"),
            ).reset_index()
            _stk_m["PERIODO"] = _stk_m["_MES"].dt.to_timestamp()

            _buenos = _buenos.merge(
                _stk_m[["SKU_PRODUCTO", "PERIODO", "_AVG_CD"]],
                on=["SKU_PRODUCTO", "PERIODO"], how="left",
            )
            _buenos["_AVG_CD"] = _buenos["_AVG_CD"].fillna(0)

            _buenos = _buenos.merge(
                _pass1[["SKU_PRODUCTO", "_DEM_DIARIA"]],
                on="SKU_PRODUCTO", how="left",
            )
            _buenos["_DEM_DIARIA"] = _buenos["_DEM_DIARIA"].fillna(0)

            _cov_min = _buenos["_DEM_DIARIA"] * 15
            _buenos_filt = _buenos[
                (_buenos["_DEM_DIARIA"] <= 0) |
                (_buenos["_AVG_CD"] >= _cov_min)
            ].copy()

            if _buenos_filt.empty:
                _buenos_filt = _buenos.copy()

            _buenos = _buenos_filt.drop(
                columns=["_AVG_CD", "_DEM_DIARIA"], errors="ignore"
            )
        else:
            _buenos = _con_venta.copy()
    else:
        _mensual["_IS_PCT"] = np.where(_mensual["CANTIDAD"] > 0, 1.0, 0.0)
        _buenos = _mensual[_mensual["CANTIDAD"] > 0].copy()
        _avg_is = None

    # Aggregate stats from "good" months
    _all_agg = _mensual.groupby("SKU_PRODUCTO").agg(
        _MESES_LB=("PERIODO", "nunique"),
    ).reset_index()

    if not _buenos.empty:
        _cv_agg = _buenos.groupby("SKU_PRODUCTO").agg(
            _MESES_CV=("PERIODO", "nunique"),
            _VTA_UND=("CANTIDAD", "sum"),
            _COGS_T=("_COGS", "sum"),
            _DIAS=("_DIAS_MES", "sum"),
        ).reset_index()
        _cv_agg = _cv_agg.merge(_all_agg, on="SKU_PRODUCTO", how="left")
    else:
        _cv_agg = _all_agg.copy()
        for _c in ["_MESES_CV", "_VTA_UND", "_COGS_T", "_DIAS"]:
            _cv_agg[_c] = 0

    # MOI Bruto 6m (uses ALL months, not just good ones)
    _bruto6 = _mensual.groupby("SKU_PRODUCTO").agg(
        _COGS_6M=("_COGS", "sum"), _VTA_6M=("CANTIDAD", "sum"),
    ).reset_index()
    _cv_agg = _cv_agg.merge(_bruto6, on="SKU_PRODUCTO", how="left")
    for _c in ["_COGS_6M", "_VTA_6M"]:
        _cv_agg[_c] = _cv_agg[_c].fillna(0)

    # MOI Bruto 3m
    _cut3 = pd.Timestamp.now() - pd.DateOffset(months=3)
    _vpx3 = _vpx6[_vpx6["PERIODO"] >= _cut3]
    if not _vpx3.empty:
        _m3 = _vpx3.groupby(["SKU_PRODUCTO", "PERIODO"], as_index=False).agg(
            _COGS=("_COGS", "sum"), CANTIDAD=("CANTIDAD", "sum"),
        )
        _bruto3 = _m3.groupby("SKU_PRODUCTO").agg(
            _COGS_3M=("_COGS", "sum"), _VTA_3M=("CANTIDAD", "sum"),
            _MESES_3M=("PERIODO", "nunique"),
        ).reset_index()
        _cv_agg = _cv_agg.merge(_bruto3, on="SKU_PRODUCTO", how="left")
    for _c in ["_COGS_3M", "_VTA_3M", "_MESES_3M"]:
        if _c not in _cv_agg.columns:
            _cv_agg[_c] = 0
        _cv_agg[_c] = _cv_agg[_c].fillna(0)

    # Merge InStock CD % average
    if _has_cd_stock and _avg_is is not None:
        _cv_agg = _cv_agg.merge(_avg_is, on="SKU_PRODUCTO", how="left")
        _cv_agg["_AVG_IS_PCT"] = _cv_agg["_AVG_IS_PCT"].fillna(0.0)
    else:
        _cv_agg["_AVG_IS_PCT"] = np.where(_cv_agg["_MESES_LB"] > 0,
                                           _cv_agg["_MESES_CV"] / _cv_agg["_MESES_LB"], 0.0)

    # Merge into df
    df = df.merge(_cv_agg, on="SKU_PRODUCTO", how="left")
    for _c in ["_MESES_CV", "_MESES_LB", "_VTA_UND", "_COGS_T", "_DIAS",
               "_COGS_6M", "_VTA_6M", "_COGS_3M", "_VTA_3M", "_MESES_3M", "_AVG_IS_PCT"]:
        if _c in df.columns:
            df[_c] = df[_c].fillna(0)

    df["MESES_CON_VENTA"] = df["_MESES_CV"].astype(int)
    df["MESES_LOOKBACK"] = df["_MESES_LB"].astype(int)
    df["DIAS_CON_STOCK"] = df["_DIAS"].astype(int)
    df["TASA_DISP"] = np.where(df["_MESES_LB"] > 0, df["_MESES_CV"] / df["_MESES_LB"], 0.0)
    df["INSTOCK_CD_PCT"] = df["_AVG_IS_PCT"]

    # MOI Bruto 6m
    _cogs_m6 = np.where(df["_MESES_LB"] > 0, df["_COGS_6M"] / df["_MESES_LB"], 0.0)
    df["MOI_6M"] = np.where(_cogs_m6 > 0, _stk / _cogs_m6, _moi_orig)
    df["MOI_6M"] = np.clip(df["MOI_6M"], 0, 999)
    df["ROT_6M"] = np.where(df["_MESES_LB"] > 0, df["_VTA_6M"] / df["_MESES_LB"], 0.0)

    # MOI Bruto 3m
    _cogs_m3 = np.where(df["_MESES_3M"] > 0, df["_COGS_3M"] / df["_MESES_3M"], 0.0)
    df["MOI_3M"] = np.where(_cogs_m3 > 0, _stk / _cogs_m3, df["MOI_6M"])
    df["MOI_3M"] = np.clip(df["MOI_3M"], 0, 999)
    df["ROT_3M"] = np.where(df["_MESES_3M"] > 0, df["_VTA_3M"] / df["_MESES_3M"], 0.0)

    # MOI Ajustado (base diaria, solo meses "buenos")
    _cogs_dia = np.where(df["_DIAS"] > 0, df["_COGS_T"] / df["_DIAS"], 0.0)
    _cogs_m_adj = _cogs_dia * 30.44
    _ajust_ok = (_cogs_m_adj > 0) & (df["_DIAS"] >= min_dias)
    df["MOI_AJUST"] = np.where(_ajust_ok, _stk / _cogs_m_adj, df["MOI_6M"])
    df["MOI_AJUST"] = np.clip(df["MOI_AJUST"], 0, 999)
    _rot_dia = np.where(df["_DIAS"] > 0, df["_VTA_UND"] / df["_DIAS"], 0.0)
    df["ROT_AJUST"] = _rot_dia * 30.44

    # Confiabilidad
    df["CONFIAB_MOI"] = np.select(
        [df["_DIAS"] < min_dias,
         df["_DIAS"] >= 60,
         df["_DIAS"] >= 28,
         df["_DIAS"] > 0],
        [f"Insuficiente (<{min_dias}d)", "Confiable", "Parcial", "Bajo"],
        default="Sin Data",
    )
    # MOI para clasificaciones
    df["MOI_CLASIF"] = np.where(_ajust_ok, df["MOI_AJUST"], df["MOI_6M"])

    # Cleanup
    _tmp = [c for c in df.columns if c.startswith("_") and c not in ("_ACCION_ORDER",)]
    df.drop(columns=[c for c in _tmp if c in df.columns], inplace=True, errors="ignore")

    return df
