import os
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv
import snowflake.connector
from datetime import datetime
import calendar

# ============================
# CONFIG
# ============================
MESES = {
    1:"Enero",2:"Febrero",3:"Marzo",4:"Abril",
    5:"Mayo",6:"Junio",7:"Julio",8:"Agosto",
    9:"Setiembre",10:"Octubre",11:"Noviembre",12:"Diciembre"
}

hoy = datetime.today()
anio_actual = hoy.year
mes_actual = hoy.month
ultimo_dia_mes = calendar.monthrange(anio_actual, mes_actual)[1]

AREAS_VALIDAS = ["Bebe","Jugueteria","Tiempo Libre","Vestuario"]

# ============================
# PATHS
# ============================
def find_project_root(start: Path):
    current = start.resolve()
    for _ in range(6):
        if (current / "data" / "inputs" / "proy_result.parquet").exists():
            return current
        current = current.parent
    return None

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = find_project_root(SCRIPT_DIR)

PARQUET_PATH = PROJECT_ROOT / "data" / "inputs" / "proy_result.parquet"
OUTPUT_PATH = Path.home() / "Desktop" / f"resumen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
FACTOR_PATH = Path.home() / "Desktop" / "FactordeImportacion.xlsx"

load_dotenv()

# ============================
# FUNCION SEGURA
# ============================
def get_col(df, name):
    for c in df.columns:
        if c.lower() == name.lower():
            return df[c]
    return pd.Series([""] * len(df))

# ============================
# PROYECCIÓN
# ============================
df_pq = pd.read_parquet(PARQUET_PATH)
df_pq.columns = df_pq.columns.str.lower()

df_pq["sku_producto"] = df_pq["sku_producto"].astype(str).str.upper().str.strip()
df_pq["forecast_compra"] = pd.to_numeric(df_pq["forecast_compra"], errors="coerce").fillna(0)
df_pq = df_pq[df_pq["forecast_compra"] > 0]

df_pq["Sku_producto"] = df_pq["sku_producto"]

df_pq["Status_PO"] = "Compra Proy"
df_pq["N_PO"] = "-"
df_pq["Origen Data"] = "proy_result.parquet"

df_pq["eta"] = pd.to_datetime(df_pq["periodo"], errors="coerce")
df_pq = df_pq[df_pq["eta"].dt.year == 2026]

# ============================
# SNOWFLAKE
# ============================
conn = snowflake.connector.connect(
    user=os.environ["SNOWFLAKE_USER"],
    password=os.environ["SNOWFLAKE_PASSWORD"],
    account=os.environ["SNOWFLAKE_ACCOUNT"]
)

query_ft = """
SELECT 
    numero_oc,
    codigo_producto_oc,
    codigo_proveedor_oc,
    estatus,
    situacion,
    (cantidad_oc - cantidad_ingresada) AS qty_pendiente,
    monto_total_oc,
    COALESCE(fecha_embarque, fecha_emision_oc) AS etd,
    fecha_eta AS eta
FROM db_supply.fct.ft_compras
WHERE (cantidad_oc - cantidad_ingresada) > 0
"""

query_prod = """
SELECT 
    cod_producto,
    proveedor,
    cod_proveedor,
    descripcion_producto,
    mix_oficial,
    grupo,
    linea,
    familia,
    marca,
    procedencia,
    costo,
    precio_fob
FROM db_dimensiones.dim.vw_producto
"""

df_ft = pd.read_sql(query_ft, conn)
df_prod = pd.read_sql(query_prod, conn)
conn.close()

df_ft.columns = df_ft.columns.str.lower()
df_prod.columns = df_prod.columns.str.lower()

df_ft["codigo_producto_oc"] = df_ft["codigo_producto_oc"].astype(str).str.upper().str.strip()
df_prod["cod_producto"] = df_prod["cod_producto"].astype(str).str.upper().str.strip()

# ============================
# MERGE
# ============================
df_ft = df_ft.merge(df_prod, left_on="codigo_producto_oc", right_on="cod_producto", how="left")
df_pq = df_pq.merge(df_prod, left_on="sku_producto", right_on="cod_producto", how="left")

df_ft["Sku_producto"] = df_ft["codigo_producto_oc"]

# ============================
# LOOKUP PROYECCIÓN
# ============================
df_pq["sku_producto"] = df_pq["sku_producto"].astype(str).str.strip().str.upper()

map_linea = dict(zip(df_prod["cod_producto"], df_prod["linea"]))
map_marca = dict(zip(df_prod["cod_producto"], df_prod["marca"]))
map_mix = dict(zip(df_prod["cod_producto"], df_prod["mix_oficial"]))
map_proveedor = dict(zip(df_prod["cod_producto"], df_prod["proveedor"]))
map_origen = dict(zip(df_prod["cod_producto"], df_prod["procedencia"]))
map_cod_proveedor = dict(zip(df_prod["cod_producto"], df_prod["cod_proveedor"]))

df_pq["LINEA"] = df_pq["sku_producto"].map(map_linea)
df_pq["marca"] = df_pq["sku_producto"].map(map_marca)
df_pq["MIX"] = df_pq["sku_producto"].map(map_mix)
df_pq["COD_PROVEEDOR"] = df_pq["sku_producto"].map(map_cod_proveedor)
df_pq["NOM_PROVEEDOR"] = df_pq["sku_producto"].map(map_proveedor)
df_pq["Origen"] = df_pq["sku_producto"].map(map_origen)

# ============================
# FACTOR
# ============================
df_factor = pd.read_excel(FACTOR_PATH)
df_factor["key"] = df_factor.iloc[:,0].astype(str).str.upper().str.strip()
df_factor["factor"] = df_factor.iloc[:,4]

def aplicar_factor(df):
    grupo = get_col(df, "grupo").astype(str).str.upper()
    linea = get_col(df, "linea").astype(str).str.upper()
    marca = get_col(df, "marca").astype(str).str.upper()

    df["key"] = grupo + linea + marca
    df = df.merge(df_factor[["key","factor"]], on="key", how="left")
    df["FACTOR_IMPORTACION"] = df["factor"].fillna(1.3)
    return df

df_ft = aplicar_factor(df_ft)
df_pq = aplicar_factor(df_pq)

# 🔥 CREAR ORIGEN ANTES DE USARLO
df_ft["Origen"] = get_col(df_ft, "procedencia")
df_pq["Origen"] = get_col(df_pq, "Origen")  # ya lo tienes pero lo aseguramos

# ============================
# AJUSTE FACTOR NACIONAL
# ============================

df_ft.loc[df_ft["Origen"].astype(str).str.upper() == "NACIONAL", "FACTOR_IMPORTACION"] = 1
df_pq.loc[df_pq["Origen"].astype(str).str.upper() == "NACIONAL", "FACTOR_IMPORTACION"] = 1
# ============================
# CAMPOS FT
# ============================
df_ft["COD_PROVEEDOR"] = df_ft["codigo_proveedor_oc"]
df_ft["NOM_PROVEEDOR"] = get_col(df_ft, "proveedor")

df_ft["Producto"] = get_col(df_ft, "descripcion_producto")
df_pq["Producto"] = get_col(df_pq, "descripcion_producto")

df_ft["MIX"] = get_col(df_ft, "mix_oficial")

df_ft["Status_PO"] = df_ft["situacion"]
df_ft["N_PO"] = df_ft["numero_oc"]

# ============================
# 🔥 FIX CLAVE (ANTES DEL CONCAT)
# ============================
df_ft["LINEA"] = get_col(df_ft, "linea")
df_ft["marca"] = get_col(df_ft, "marca")
df_ft["MIX"] = get_col(df_ft, "mix_oficial")
df_ft["Origen"] = get_col(df_ft, "procedencia")

# ============================
# AJUSTE FACTOR NACIONAL
# ============================
df_ft.loc[df_ft["Origen"].astype(str).str.upper() == "NACIONAL", "FACTOR_IMPORTACION"] = 1
df_pq.loc[df_pq["Origen"].astype(str).str.upper() == "NACIONAL", "FACTOR_IMPORTACION"] = 1

# ============================
# FECHAS
# ============================
df_ft["etd"] = pd.to_datetime(df_ft["etd"])
df_ft["eta"] = pd.to_datetime(df_ft["eta"])

# ============================
# NUEVO FILTRO FT_COMPRAS
# ============================

# 1. SOLO pendientes
df_ft = df_ft[
    df_ft["estatus"].astype(str).str.upper().str.contains("PENDIENTE", na=False)
]

# 2. detectar origen
mask_importado_ft = df_ft["Origen"].astype(str).str.upper() == "IMPORTADO"

# 3. aplicar lógica SOLO a importados
df_ft_importado = df_ft[mask_importado_ft].copy()
df_ft_nacional = df_ft[~mask_importado_ft].copy()

# --- IMPORTADOS ---
df_ft_importado["eta"] = pd.to_datetime(df_ft_importado["eta"], errors="coerce")

df_ft_importado = df_ft_importado[df_ft_importado["eta"].dt.year == 2026]

df_ft_importado.loc[
    df_ft_importado["eta"].dt.month < mes_actual,
    "eta"
] = datetime(anio_actual, mes_actual, ultimo_dia_mes)

# --- UNIR DE NUEVO ---
df_ft = pd.concat([df_ft_importado, df_ft_nacional], ignore_index=True)

# ============================
# AJUSTE ETA NACIONAL VACÍO (MEJORADO)
# ============================

# asegurar datetime
df_ft["eta"] = pd.to_datetime(df_ft["eta"], errors="coerce")

# máscara más robusta
mask_nacional_sin_eta = (
    df_ft["Origen"].astype(str).str.upper().str.strip() == "NACIONAL"
) & (
    df_ft["eta"].isna()
)

# asignar último día del mes actual
df_ft.loc[mask_nacional_sin_eta, "eta"] = datetime(
    anio_actual, mes_actual, ultimo_dia_mes
)

df_ft["ETD"] = df_ft["etd"].dt.strftime("%d/%m/%Y")
df_ft["ETA"] = df_ft["eta"].dt.strftime("%d/%m/%Y")

df_pq["ETD"] = ""
df_pq["ETA"] = df_pq["eta"].dt.strftime("%d/%m/%Y")

df_ft["MES ETA"] = df_ft["eta"].dt.month.map(MESES)
df_pq["MES ETA"] = df_pq["eta"].dt.month.map(MESES)

df_ft["Días agua"] = (df_ft["eta"] - df_ft["etd"]).dt.days
df_pq["Días agua"] = ""

# ============================
# CÁLCULOS NUEVOS
# ============================

# ============================
# PROYECCIÓN
# ============================
df_pq["Compra Unds Proy"] = df_pq["forecast_compra"]

mask_importado_pq = df_pq["Origen"].astype(str).str.upper() == "IMPORTADO"

# ============================
# Amount SOLES incluye factor import (PROYECCIÓN)
# ============================

mask_importado_pq = df_pq["Origen"].astype(str).str.upper() == "IMPORTADO"

# base nacional (COSTO * cantidad)
df_pq["Amount SOLES incluye factor import"] = (
    get_col(df_pq, "costo") *
    df_pq["Compra Unds Proy"]
)

# si es importado → FOB * factor * TC * cantidad
df_pq.loc[mask_importado_pq, "Amount SOLES incluye factor import"] = (
    get_col(df_pq, "precio_fob") *
    df_pq["FACTOR_IMPORTACION"] *
    3.6 *
    df_pq["Compra Unds Proy"]
)

# Amount
df_pq["Amount"] = df_pq["Amount SOLES incluye factor import"]

df_pq.loc[mask_importado_pq, "Amount"] = (
    df_pq["Amount SOLES incluye factor import"] /
    (df_pq["FACTOR_IMPORTACION"] * 3.6)
)

# Currency
df_pq["Currency"] = df_pq["Origen"].apply(
    lambda x: "USD" if str(x).upper() == "IMPORTADO" else "PEN"
)

# ============================
# FT_COMPRAS
# ============================
df_ft["Compra Unds Proy"] = df_ft["qty_pendiente"]

mask_importado_ft = df_ft["Origen"].astype(str).str.upper() == "IMPORTADO"

# Amount
df_ft["Amount"] = df_ft["monto_total_oc"]

# Amount SOLES incluye factor import
df_ft["Amount SOLES incluye factor import"] = df_ft["monto_total_oc"]

df_ft.loc[mask_importado_ft, "Amount SOLES incluye factor import"] = (
    df_ft["monto_total_oc"] *
    df_ft["FACTOR_IMPORTACION"] *
    3.6
)

# Currency
df_ft["Currency"] = df_ft["Origen"].apply(
    lambda x: "USD" if str(x).upper() == "IMPORTADO" else "PEN"
)

df_ft["Origen Data"] = "ft_compras"

# ============================
# CONCAT FINAL
# ============================
df = pd.concat([df_ft, df_pq], ignore_index=True).fillna("")

df = df.rename(columns={
    "grupo":"AREA",
    "familia":"SUBLINEA"
})

df = df[df["AREA"].isin(AREAS_VALIDAS)]

columnas = [
"Origen","Status_PO","N_PO","NOM_PROVEEDOR","COD_PROVEEDOR",
"Amount","Currency","FACTOR_IMPORTACION","Amount SOLES incluye factor import",
"ETD","ETA","MES ETA","Días agua","FACTURA","Especificaciones de Pago",
"AREA","LINEA","SUBLINEA","marca","MIX","Sku_producto","Producto","Compra Unds Proy","Origen Data",
"M3 Unit","M3 Total","TAMAÑO","CANT x PALLET","PALLETS"
]

for col in columnas:
    if col not in df.columns:
        df[col] = ""

df = df[columnas]

with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
    df.to_excel(writer, index=False)

print("✅ TODO CORREGIDO - DATA COMPLETA")