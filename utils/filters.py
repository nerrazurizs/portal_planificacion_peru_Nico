"""Shared filtering and parsing utilities."""


def limpiar_lista(texto):
    """Parse a text input (comma/space/newline/tab/semicolon separated) into a clean uppercase list."""
    if not texto:
        return []
    texto = texto.replace("\n", ",").replace("\t", ",").replace(";", ",")
    return [x.strip().upper() for x in texto.split(",") if x.strip()]


def clasificar_canal(canal, descripcion):
    """Classify a store/warehouse into a detailed channel category."""
    c = str(canal).upper()
    d = str(descripcion).upper()
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
