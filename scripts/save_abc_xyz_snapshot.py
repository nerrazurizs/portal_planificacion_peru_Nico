"""Guarda un snapshot diario de la clasificación ABC-XYZ-FSN en Parquet.

Diseñado para correr en GitHub Actions (cron diario a las 09:00 Perú / 14:00 UTC).
Reutiliza la lógica de clasificación de db/cache.py sin depender de Streamlit.

Archivo de salida:
    data/abc_xyz_snapshots/abc_xyz_YYYY-MM-DD.parquet

Variables de entorno requeridas (mismas que alertas_diarias):
    SNOWFLAKE_USER, SNOWFLAKE_PASSWORD, SNOWFLAKE_ACCOUNT
    SNOWFLAKE_WAREHOUSE, SNOWFLAKE_ROLE, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

# ── Root del proyecto ────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import numpy as np
import pandas as pd
import snowflake.connector

from db.queries import QUERY_MAESTRA, QUERY_VENTAS_AA, QUERY_VENTAS_SEMANALES
from db.cache import (
    _classify_abc_internal,
    _classify_xyz_internal,
    _classify_fsn,
)
from utils.filters import norm_cols

# ── Directorio de salida ─────────────────────────────────────────────────────
SNAPSHOT_DIR = ROOT / "data" / "abc_xyz_snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


# ── Conexión Snowflake ───────────────────────────────────────────────────────
def _connect() -> snowflake.connector.SnowflakeConnection:
    return snowflake.connector.connect(
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        role=os.environ.get("SNOWFLAKE_ROLE", "PUBLIC"),
        database=os.environ.get("SNOWFLAKE_DATABASE", ""),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", ""),
        login_timeout=60,
        network_timeout=90,
    )


def _query(conn, sql: str) -> pd.DataFrame:
    cursor = conn.cursor()
    cursor.execute(sql)
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return norm_cols(pd.DataFrame(rows, columns=cols))


# ── Clasificación ABC-XYZ-FSN ─────────────────────────────────────────────────
def generate_snapshot(conn) -> pd.DataFrame:
    """Genera el DataFrame de clasificación ABC-XYZ-FSN del día."""
    print("  Cargando ventas anuales...")
    df_aa = _query(conn, QUERY_VENTAS_AA)

    print("  Cargando ventas semanales...")
    df_sem = _query(conn, QUERY_VENTAS_SEMANALES)

    print("  Cargando maestra de productos...")
    df_maestra = _query(conn, QUERY_MAESTRA)

    # Filtrar solo productos MIX_OFICIAL activos (MIX, IN & OUT)
    if "MIX_OFICIAL" in df_maestra.columns:
        mix_skus = set(
            df_maestra.loc[
                df_maestra["MIX_OFICIAL"].astype(str).str.upper().isin(["MIX", "IN & OUT"]),
                "SKU_PRODUCTO",
            ]
        )
        df_aa  = df_aa[df_aa["SKU_PRODUCTO"].isin(mix_skus)]
        df_sem = df_sem[df_sem["SKU_PRODUCTO"].isin(mix_skus)]
        print(f"  Filtrado a {len(mix_skus):,} SKUs activos (MIX + IN & OUT).")

    # Clasificar ABC, XYZ, FSN
    print("  Clasificando ABC...")
    abc_df = _classify_abc_internal(df_aa)

    print("  Clasificando XYZ...")
    xyz_df = _classify_xyz_internal(df_sem)

    print("  Clasificando FSN...")
    fsn_df = _classify_fsn(df_aa)

    # Merge clasificaciones
    result = abc_df.merge(xyz_df, on="SKU_PRODUCTO", how="outer")
    result = result.merge(fsn_df[["SKU_PRODUCTO", "CLASE_FSN"]], on="SKU_PRODUCTO", how="left")
    result["CLASE_ABC"] = result["CLASE_ABC"].fillna("C")
    result["CLASE_XYZ"] = result["CLASE_XYZ"].fillna("Z")
    result["CLASE_FSN"] = result["CLASE_FSN"].fillna("N")
    result["CLASE_COMBINADA"] = result["CLASE_ABC"] + result["CLASE_XYZ"]

    # Enriquecer con dimensiones de maestra
    maestra_cols = ["SKU_PRODUCTO", "SKU_NOM_PRODUCTO", "AREA", "LINEA", "SUBLINEA",
                    "MARCA", "PROCEDENCIA", "MIX_OFICIAL", "ULTIMO_INGRESO_CD"]
    available = [c for c in maestra_cols if c in df_maestra.columns]
    if available:
        maestra_dedup = df_maestra[available].drop_duplicates(subset=["SKU_PRODUCTO"])
        result = result.merge(maestra_dedup, on="SKU_PRODUCTO", how="left")

    # Agregar fecha del snapshot
    result.insert(0, "FECHA_SNAPSHOT", date.today().isoformat())

    print(f"  Snapshot generado: {len(result):,} SKUs.")
    return result


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    hoy = date.today()
    out_path = SNAPSHOT_DIR / f"abc_xyz_{hoy}.parquet"

    if out_path.exists():
        print(f"[OK] Snapshot {hoy} ya existe — omitiendo. ({out_path})")
        return

    print(f"[ABC-XYZ Snapshot] Generando snapshot para {hoy}...")

    conn = None
    try:
        conn = _connect()
        df = generate_snapshot(conn)

        df.to_parquet(out_path, index=False, compression="snappy")
        size_kb = out_path.stat().st_size / 1024
        print(f"[OK] Guardado: {out_path}  ({size_kb:.1f} KB, {len(df):,} filas)")

    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    main()
