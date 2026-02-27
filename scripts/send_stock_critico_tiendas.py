"""Script autónomo para envío del reporte Stock Crítico Tiendas por email.

Diseñado para correr en GitHub Actions o cron — NO requiere Streamlit.
Replica la misma lógica que _load_stock_critico_tiendas() + _build_stock_critico_tiendas_excel()
del módulo modules/alertas_email.py.

Criterio: SKUs con (MOI >= 12 o sin MOI) AND Antigüedad >= 12 meses, solo canal TIENDA.
Destinatario: samuel.dorival@dorel.cl
Asunto: REPORTE STOCK CRITICO PORTAL DOREL CHILE

Variables de entorno requeridas:
    SNOWFLAKE_USER, SNOWFLAKE_PASSWORD, SNOWFLAKE_ACCOUNT
    SNOWFLAKE_WAREHOUSE, SNOWFLAKE_ROLE, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA
    OUTLOOK_EMAIL, OUTLOOK_PASSWORD, SMTP_SERVER, SMTP_PORT
"""

from __future__ import annotations

import os
import sys
from datetime import date
from io import BytesIO
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

from db.queries import QUERY_STOCK_CRITICO_DETAIL, QUERY_STOCK_CRITICO_METRICS
from utils.email_sender import get_smtp_config, send_alert_email
from utils.filters import norm_cols

# ── Destinatario fijo ─────────────────────────────────────────────────────────
_RECIPIENT = "samuel.dorival@dorel.cl"
_SUBJECT = "REPORTE STOCK CRITICO PORTAL DOREL CHILE"

# ── 13 columnas exactas del reporte de Samuel ────────────────────────────────
_CRITICO_TIENDAS_COLS = [
    "SKU_PRODUCTO", "NOM_PRODUCTO", "AREA", "LINEA", "MARCA",
    "COD_BODEGA", "ID_SUCURSAL", "DESCRIPCION_SUCURSAL",
    "CANAL_DE_DISTRIBUCION", "SUPERVISOR", "CLUSTER",
    "STOCK_UNIDADES", "STOCK_COSTO",
]


# ============================================================================
# SNOWFLAKE
# ============================================================================

def _connect() -> snowflake.connector.SnowflakeConnection:
    return snowflake.connector.connect(
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        role=os.environ.get("SNOWFLAKE_ROLE", "PUBLIC"),
        database=os.environ.get("SNOWFLAKE_DATABASE", ""),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", ""),
    )


def _query(conn, sql: str) -> pd.DataFrame:
    cursor = conn.cursor()
    cursor.execute(sql)
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return norm_cols(pd.DataFrame(rows, columns=cols))


# ============================================================================
# DATA LOADING — réplica de _load_stock_critico_tiendas() sin Streamlit
# ============================================================================

def load_stock_critico_tiendas(conn) -> pd.DataFrame:
    """Load stock critico detail at tienda level.

    Filtro: (MOI >= 12 OR sin MOI) AND Antigüedad >= 12 meses AND TIENDA.

    Steps:
    1. Load QUERY_STOCK_CRITICO_DETAIL (row per bodega/SKU, latest date)
    2. Load QUERY_STOCK_CRITICO_METRICS (MOI/antigüedad per SKU, company level)
    3. Merge to get MOI and ANTIGUEDAD per SKU
    4. Filter: TIENDA only, (MOI >= 12 OR sin MOI) AND antigüedad >= 12
    5. Return the 13-column DataFrame matching Samuel's format
    """
    print("  → Cargando stock critico detalle por tienda...")

    # 1. Detail: stock per tienda/SKU
    detail = _query(conn, QUERY_STOCK_CRITICO_DETAIL)
    if detail.empty:
        print("  ✗ Sin datos de stock critico detalle.")
        return pd.DataFrame(columns=_CRITICO_TIENDAS_COLS)

    for c in ["STOCK_COSTO", "STOCK_UNIDADES"]:
        if c in detail.columns:
            detail[c] = pd.to_numeric(detail[c], errors="coerce").fillna(0)

    print(f"     Detalle: {len(detail):,} filas, {detail['SKU_PRODUCTO'].nunique():,} SKUs.")

    # 2. Metrics: MOI and antigüedad per SKU (company level)
    print("  → Cargando métricas MOI/antigüedad...")
    metrics = _query(conn, QUERY_STOCK_CRITICO_METRICS)

    if not metrics.empty:
        for c in ["STOCK_COSTO", "MOI", "ANTIGUEDAD_MESES", "COSTO_PROM_90_CIA"]:
            if c in metrics.columns:
                metrics[c] = pd.to_numeric(metrics[c], errors="coerce").fillna(0)

        # Keep latest date only
        if "FECHA" in metrics.columns:
            metrics["FECHA"] = pd.to_datetime(metrics["FECHA"], errors="coerce")
            metrics = metrics[metrics["FECHA"] == metrics["FECHA"].max()]

        # Aggregate to SKU level
        sku_metrics = metrics.groupby("SKU_PRODUCTO", as_index=False).agg(
            COSTO_PROM_90_CIA=("COSTO_PROM_90_CIA", "max"),
            STOCK_COSTO_CIA=("STOCK_COSTO", "sum"),
        )

        # Recalculate MOI at company level
        sku_metrics["MOI"] = np.where(
            sku_metrics["COSTO_PROM_90_CIA"] > 0,
            (sku_metrics["STOCK_COSTO_CIA"] / sku_metrics["COSTO_PROM_90_CIA"]) / 30.44,
            np.nan,
        )
        # Items with stock but no sales → keep as NaN = "sin MOI"
        sku_metrics.loc[
            (sku_metrics["MOI"].isna()) & (sku_metrics["STOCK_COSTO_CIA"] > 0), "MOI"
        ] = np.nan

        # Antigüedad from metrics
        if "ANTIGUEDAD_MESES" in metrics.columns:
            ant_sku = metrics.groupby("SKU_PRODUCTO", as_index=False)["ANTIGUEDAD_MESES"].max()
            sku_metrics = sku_metrics.merge(ant_sku, on="SKU_PRODUCTO", how="left")
        else:
            sku_metrics["ANTIGUEDAD_MESES"] = 0

        sku_metrics["ANTIGUEDAD_MESES"] = pd.to_numeric(
            sku_metrics["ANTIGUEDAD_MESES"], errors="coerce"
        ).fillna(0)

        print(f"     Métricas: {len(sku_metrics):,} SKUs con datos MOI/antigüedad.")

        # 3. Merge
        detail = detail.merge(
            sku_metrics[["SKU_PRODUCTO", "MOI", "ANTIGUEDAD_MESES"]],
            on="SKU_PRODUCTO",
            how="left",
        )
    else:
        print("  ⚠ Sin métricas MOI. Se asumirá 'sin MOI' para todos.")
        detail["MOI"] = np.nan
        detail["ANTIGUEDAD_MESES"] = 0

    # 4. Filter: TIENDA only + (MOI >= 12 OR sin MOI) AND antigüedad >= 12
    if "CANAL_DE_DISTRIBUCION" in detail.columns:
        before = len(detail)
        detail = detail[detail["CANAL_DE_DISTRIBUCION"].str.upper() == "TIENDA"]
        print(f"     Filtro TIENDA: {before:,} → {len(detail):,} filas.")

    mask_moi = (detail["MOI"] >= 12) | (detail["MOI"].isna())
    mask_ant = detail["ANTIGUEDAD_MESES"] >= 12
    detail = detail[mask_moi & mask_ant].copy()

    print(f"     Filtro MOI>=12/sinMOI + Ant>=12: {len(detail):,} filas, "
          f"{detail['SKU_PRODUCTO'].nunique():,} SKUs.")

    # 5. Select and order columns
    cols = [c for c in _CRITICO_TIENDAS_COLS if c in detail.columns]
    return detail[cols].reset_index(drop=True)


# ============================================================================
# EXCEL — réplica de _build_stock_critico_tiendas_excel()
# ============================================================================

def build_excel(df_detail: pd.DataFrame) -> bytes:
    """Build Excel with 2 sheets matching Samuel's exact format.

    Sheet 1 'Resumen Tienda': pivot by DESCRIPCION_SUCURSAL with totals
    Sheet 2 'Detalle SKU': full detail, 13 columns
    """
    buf = BytesIO()

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # ── Sheet 1: Resumen Tienda ──
        if not df_detail.empty and "DESCRIPCION_SUCURSAL" in df_detail.columns:
            resumen = df_detail.groupby("DESCRIPCION_SUCURSAL", as_index=False).agg(
                STOCK_UNIDADES=("STOCK_UNIDADES", "sum"),
                STOCK_COSTO=("STOCK_COSTO", "sum"),
            ).sort_values("STOCK_COSTO", ascending=False)

            # Add total row
            total_row = pd.DataFrame([{
                "DESCRIPCION_SUCURSAL": "Total general",
                "STOCK_UNIDADES": resumen["STOCK_UNIDADES"].sum(),
                "STOCK_COSTO": resumen["STOCK_COSTO"].sum(),
            }])
            resumen = pd.concat([resumen, total_row], ignore_index=True)

            resumen.to_excel(writer, sheet_name="Resumen Tienda", index=False)
        else:
            pd.DataFrame({"Info": ["Sin datos"]}).to_excel(
                writer, sheet_name="Resumen Tienda", index=False,
            )

        # ── Sheet 2: Detalle SKU ──
        df_detail.to_excel(writer, sheet_name="Detalle SKU", index=False)

    return buf.getvalue()


# ============================================================================
# HTML EMAIL — réplica de _build_stock_critico_tiendas_email_html()
# ============================================================================

def build_email_html(
    n_skus: int, n_tiendas: int, total_und: float, total_costo: float,
    resumen_top: pd.DataFrame,
) -> str:
    """Build a simple HTML email body with KPIs and top tiendas summary."""
    today_str = date.today().strftime("%d/%m/%Y")

    # Format top tiendas as HTML table rows
    rows_html = ""
    for _, row in resumen_top.iterrows():
        tienda = row.get("DESCRIPCION_SUCURSAL", "—")
        und = int(row.get("STOCK_UNIDADES", 0))
        costo = row.get("STOCK_COSTO", 0)
        rows_html += f"""<tr>
            <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0;">{tienda}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0;text-align:right;">{und:,}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #e2e8f0;text-align:right;">${costo:,.0f}</td>
        </tr>"""

    return f"""
    <html>
    <body style="font-family:Arial,sans-serif;color:#1e293b;margin:0;padding:20px;background:#f8fafc;">
        <div style="max-width:700px;margin:0 auto;background:white;border-radius:12px;
                    box-shadow:0 2px 8px rgba(0,0,0,0.08);overflow:hidden;">

            <!-- Header -->
            <div style="background:#065E8B;padding:24px 32px;color:white;">
                <h1 style="margin:0;font-size:20px;">REPORTE STOCK CRITICO PORTAL DOREL CHILE</h1>
                <p style="margin:8px 0 0;opacity:0.85;font-size:14px;">
                    Fecha: {today_str} | Criterio: MOI &ge; 12 o sin MOI + Antiguedad &ge; 12 meses | Solo tiendas
                </p>
            </div>

            <!-- KPIs -->
            <div style="display:flex;padding:20px 32px;gap:16px;">
                <div style="flex:1;background:#f0f9ff;border-radius:8px;padding:16px;text-align:center;">
                    <div style="font-size:24px;font-weight:700;color:#065E8B;">{n_skus:,}</div>
                    <div style="font-size:12px;color:#64748b;">SKUs Criticos</div>
                </div>
                <div style="flex:1;background:#fef3c7;border-radius:8px;padding:16px;text-align:center;">
                    <div style="font-size:24px;font-weight:700;color:#92400e;">{n_tiendas:,}</div>
                    <div style="font-size:12px;color:#64748b;">Tiendas Afectadas</div>
                </div>
                <div style="flex:1;background:#fce4ec;border-radius:8px;padding:16px;text-align:center;">
                    <div style="font-size:24px;font-weight:700;color:#c62828;">{total_und:,.0f}</div>
                    <div style="font-size:12px;color:#64748b;">Unidades</div>
                </div>
                <div style="flex:1;background:#e8f5e9;border-radius:8px;padding:16px;text-align:center;">
                    <div style="font-size:24px;font-weight:700;color:#2e7d32;">${total_costo:,.0f}</div>
                    <div style="font-size:12px;color:#64748b;">Costo Total</div>
                </div>
            </div>

            <!-- Top Tiendas Table -->
            <div style="padding:0 32px 24px;">
                <h3 style="margin:0 0 12px;font-size:16px;color:#1e293b;">Top 15 Tiendas por Costo Stock Critico</h3>
                <table style="width:100%;border-collapse:collapse;font-size:13px;">
                    <thead>
                        <tr style="background:#f1f5f9;">
                            <th style="padding:8px 12px;text-align:left;border-bottom:2px solid #cbd5e1;">Tienda</th>
                            <th style="padding:8px 12px;text-align:right;border-bottom:2px solid #cbd5e1;">Unidades</th>
                            <th style="padding:8px 12px;text-align:right;border-bottom:2px solid #cbd5e1;">Costo CLP</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>
            </div>

            <!-- Footer -->
            <div style="background:#f1f5f9;padding:16px 32px;text-align:center;font-size:12px;color:#64748b;">
                Generado automaticamente por Portal Dorel Chile &mdash; Ver detalle completo en el Excel adjunto.
            </div>
        </div>
    </body>
    </html>
    """


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 60)
    print(f"Reporte Stock Critico Tiendas — {date.today().strftime('%d/%m/%Y')}")
    print("=" * 60)

    # Validate env vars
    missing = [v for v in [
        "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_ACCOUNT",
        "OUTLOOK_EMAIL", "OUTLOOK_PASSWORD",
    ] if not os.getenv(v)]
    if missing:
        print(f"\n✗ Faltan variables de entorno: {', '.join(missing)}")
        sys.exit(1)

    print(f"\nDestinatario: {_RECIPIENT}")

    # ── Conectar ─────────────────────────────────────────────────────────────
    print("\n[1/4] Conectando a Snowflake...")
    try:
        conn = _connect()
        conn.cursor().execute("SELECT 1")
        print("  ✓ Conexión OK.")
    except Exception as e:
        print(f"  ✗ {e}")
        sys.exit(1)

    # ── Cargar datos ─────────────────────────────────────────────────────────
    print("\n[2/4] Cargando datos...")
    try:
        df = load_stock_critico_tiendas(conn)
    except Exception as e:
        print(f"  ✗ Error cargando datos: {e}")
        import traceback
        traceback.print_exc()
        conn.close()
        sys.exit(1)

    conn.close()

    if df.empty:
        print("\n⚠ No se encontraron SKUs que cumplan el criterio. No se enviará email.")
        sys.exit(0)

    # ── Preparar reporte ─────────────────────────────────────────────────────
    print("\n[3/4] Preparando reporte...")

    n_skus = df["SKU_PRODUCTO"].nunique() if "SKU_PRODUCTO" in df.columns else 0
    n_tiendas = df["DESCRIPCION_SUCURSAL"].nunique() if "DESCRIPCION_SUCURSAL" in df.columns else 0
    total_und = df["STOCK_UNIDADES"].sum() if "STOCK_UNIDADES" in df.columns else 0
    total_costo = df["STOCK_COSTO"].sum() if "STOCK_COSTO" in df.columns else 0

    print(f"  SKUs: {n_skus:,} | Tiendas: {n_tiendas:,} | "
          f"Unidades: {total_und:,.0f} | Costo: ${total_costo:,.0f}")

    # Excel attachment
    excel_bytes = build_excel(df)
    fname = f"Stock Critico tiendas {date.today().strftime('%Y%m%d')}.xlsx"
    print(f"  ✓ Excel: {fname}")

    # Top 15 tiendas for email body
    resumen_top = pd.DataFrame()
    if "DESCRIPCION_SUCURSAL" in df.columns:
        resumen_top = df.groupby("DESCRIPCION_SUCURSAL", as_index=False).agg(
            STOCK_UNIDADES=("STOCK_UNIDADES", "sum"),
            STOCK_COSTO=("STOCK_COSTO", "sum"),
        ).sort_values("STOCK_COSTO", ascending=False).head(15)

    html_body = build_email_html(n_skus, n_tiendas, total_und, total_costo, resumen_top)

    # ── Enviar ───────────────────────────────────────────────────────────────
    print("\n[4/4] Enviando email...")

    port_raw = os.getenv("SMTP_PORT", "587")
    cfg = get_smtp_config({
        "email":    os.getenv("OUTLOOK_EMAIL"),
        "password": os.getenv("OUTLOOK_PASSWORD"),
        "server":   os.getenv("SMTP_SERVER", "smtp.gmail.com"),
        "port":     int(port_raw) if str(port_raw).isdigit() else 587,
    })

    ok, msg = send_alert_email(
        subject=_SUBJECT,
        html_body=html_body,
        to_recipients=[_RECIPIENT],
        cfg=cfg,
        attachments=[(fname, excel_bytes)],
    )

    print(f"\n{'✓' if ok else '✗'} {msg}")
    print("=" * 60)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
