"""Jupyter notebook generator for stock analysis reports."""

import json


class NotebookGenerator:
    def __init__(self):
        self.cells = []

    def add_markdown(self, source):
        self.cells.append({
            "cell_type": "markdown",
            "metadata": {},
            "source": [line + "\n" for line in source.split("\n")],
        })

    def add_code(self, source):
        self.cells.append({
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": [line + "\n" for line in source.split("\n")],
        })

    def get_notebook_json(self):
        return json.dumps(
            {
                "cells": self.cells,
                "metadata": {
                    "kernelspec": {
                        "display_name": "Python 3",
                        "language": "python",
                        "name": "python3",
                    },
                    "language_info": {
                        "codemirror_mode": {"name": "ipython", "version": 3},
                        "file_extension": ".py",
                        "mimetype": "text/x-python",
                        "name": "python",
                        "nbconvert_exporter": "python",
                        "pygments_lexer": "ipython3",
                        "version": "3.8.5",
                    },
                },
                "nbformat": 4,
                "nbformat_minor": 4,
            },
            indent=2,
        )


def generate_stock_analysis_notebook(metrics_file, detail_file, sales_file, stock_file_units=None):
    """Generate a full stock analysis Jupyter notebook with the given data file paths."""
    nb = NotebookGenerator()

    # 1. Header & Setup
    nb.add_markdown(
        "# Analisis Integral de Stock Critico y Reposicion\n"
        "Este notebook consolida TODO el analisis:\n"
        "1. **Macro**: Evolucion historica y semaforos (MOI/Antiguedad).\n"
        "2. **Canales/Bodegas**: Distribucion del stock critico por canal.\n"
        "3. **Micro (Deep Dive)**: Top SKUs, Pareto, Heatmaps.\n\n"
        "**Estrategia de Datos (Two-File Strategy):**\n"
        f"* **Metricas**: `{metrics_file}` (Nivel SKU-Dia).\n"
        f"* **Detalle**: `{detail_file}` (15GB masivo)."
    )

    nb.add_code(
        f"""import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import seaborn as sns
import os
from datetime import datetime
from matplotlib.ticker import FuncFormatter

plt.style.use('seaborn-v0_8-whitegrid')
pd.set_option('display.float_format', lambda x: '%.2f' % x)
pd.set_option('display.max_columns', None)

METRICS_INPUT_FILE = "{metrics_file}"
DETAIL_INPUT_FILE = "{detail_file}"
SALES_INPUT_FILE = "{sales_file}"
STOCK_FILE_WITH_UNITS = "{stock_file_units if stock_file_units else ''}"

SKU_STATUS_FILE = "sku_status_v2.csv"
CRITICAL_STOCK_HISTORY_FILE = "critical_stock_history_v2.csv"
CRITICAL_METRICS_HISTORY_FILE = "critical_metrics_history_v2.csv"
STORE_TOTALS_FILE = "store_totals_v2.csv"

CHUNK_SIZE = 500000
FORCE_REGEN = True"""
    )

    nb.add_code(
        """def human_format(num, pos=None):
    if num is None: return "0"
    magnitude = 0
    while abs(num) >= 1000:
        magnitude += 1
        num /= 1000.0
    return '%.1f%s' % (num, ['', 'K', 'M', 'B', 'T'][magnitude])"""
    )

    # Paso 1
    nb.add_markdown(
        "## Paso 1: Definicion de Status (Fuente: SKU-Dia)\n"
        "Leemos el archivo de metricas para determinar SKUs Criticos."
    )

    nb.add_code(
        """if os.path.exists(SKU_STATUS_FILE) and os.path.exists(CRITICAL_METRICS_HISTORY_FILE) and not FORCE_REGEN:
    print("Carga rapida: Archivos V2 encontrados.")
    sku_status = pd.read_csv(SKU_STATUS_FILE)
    critico_skus = sku_status[sku_status['STATUS'] == 'CRITICO']['SKU_PRODUCTO'].unique()
else:
    print("Procesando metricas desde archivo SKU-Dia...")
    df_metrics = pd.read_csv(METRICS_INPUT_FILE, parse_dates=['FECHA'])
    latest_date = df_metrics['FECHA'].max()
    df_last = df_metrics[df_metrics['FECHA'] == latest_date].copy()

    cond_moi_crit = (df_last['MOI'] >= 12) | (df_last['MOI'].isna())
    cond_ant_crit = (df_last['ANTIGUEDAD_MESES'] >= 12)
    cond_moi_apunto = (df_last['MOI'] >= 8) & (df_last['MOI'] < 12)
    cond_ant_apunto = (df_last['ANTIGUEDAD_MESES'] >= 8) & (df_last['ANTIGUEDAD_MESES'] < 12)

    criticos = df_last[cond_moi_crit & cond_ant_crit][['SKU_PRODUCTO']].copy()
    criticos['STATUS'] = 'CRITICO'
    apunto = df_last[cond_moi_apunto & cond_ant_apunto][['SKU_PRODUCTO']].copy()
    apunto['STATUS'] = 'A_PUNTO'

    sku_status = pd.concat([criticos, apunto]).drop_duplicates(subset='SKU_PRODUCTO')
    sku_status.to_csv(SKU_STATUS_FILE, index=False)

    history_metrics = df_metrics[df_metrics['SKU_PRODUCTO'].isin(sku_status['SKU_PRODUCTO'])].copy()
    history_metrics.to_csv(CRITICAL_METRICS_HISTORY_FILE, index=False)

    critico_skus = criticos['SKU_PRODUCTO'].unique()
    print(f"Status generado: {len(critico_skus)} SKUs Criticos identificados.")"""
    )

    # Paso 2
    nb.add_markdown(
        "## Paso 2: Extraccion de Detalle y Totales (Fuente: 15GB)\n"
        "1. Extraemos detalle SOLO de SKUs criticos.\n"
        "2. Calculamos Stock Total por Tienda."
    )

    nb.add_code(
        """if os.path.exists(CRITICAL_STOCK_HISTORY_FILE) and os.path.exists(STORE_TOTALS_FILE) and os.path.exists('daily_stock_totals.csv') and not FORCE_REGEN:
    print("Historia y Totales V2 encontrados.")
else:
    print("Procesando archivo GIGANTE (15GB)...")
    target_skus = pd.read_csv(SKU_STATUS_FILE)['SKU_PRODUCTO'].unique()
    target_df = pd.DataFrame(target_skus, columns=['SKU_PRODUCTO'])

    extract_chunks, totals_chunks, daily_totals_chunks = [], [], []
    cols = ['FECHA', 'SKU_PRODUCTO', 'NOM_PRODUCTO', 'DESCRIPCION_SUCURSAL', 'CANAL_DE_DISTRIBUCION', 'STOCK_COSTO', 'MARCA', 'LINEA']
    dtypes = {'SKU_PRODUCTO': str, 'CANAL_DE_DISTRIBUCION': str, 'DESCRIPCION_SUCURSAL': str, 'STOCK_COSTO': float}

    print("Buscando ultima fecha en archivo gigante...")
    max_date = None
    for chunk in pd.read_csv(DETAIL_INPUT_FILE, usecols=['FECHA'], chunksize=CHUNK_SIZE, parse_dates=['FECHA']):
        chunk_max = chunk['FECHA'].max()
        if max_date is None or chunk_max > max_date: max_date = chunk_max
    print(f"Fecha corte para totales: {max_date}")

    try:
        reader = pd.read_csv(DETAIL_INPUT_FILE, usecols=lambda c: c in cols, chunksize=CHUNK_SIZE, parse_dates=['FECHA'], dtype=dtypes)
        for i, chunk in enumerate(reader):
            subset_last = chunk[chunk['FECHA'] == max_date]
            if not subset_last.empty:
                totals_agg = subset_last.groupby(['DESCRIPCION_SUCURSAL', 'CANAL_DE_DISTRIBUCION'])['STOCK_COSTO'].sum().reset_index()
                totals_chunks.append(totals_agg)

            merged = chunk.merge(target_df, on='SKU_PRODUCTO', how='inner')
            if not merged.empty:
                extract_chunks.append(merged)

            daily_agg = chunk.groupby('FECHA')['STOCK_COSTO'].sum().reset_index()
            daily_totals_chunks.append(daily_agg)

            if i % 10 == 0: print(f"Procesado chunk {i}...")

        full_detail = pd.concat(extract_chunks, ignore_index=True)
        full_detail['FECHA'] = pd.to_datetime(full_detail['FECHA'])
        full_detail.to_csv(CRITICAL_STOCK_HISTORY_FILE, index=False)

        full_totals = pd.concat(totals_chunks, ignore_index=True)
        final_totals = full_totals.groupby(['DESCRIPCION_SUCURSAL', 'CANAL_DE_DISTRIBUCION'])['STOCK_COSTO'].sum().reset_index()
        final_totals.to_csv(STORE_TOTALS_FILE, index=False)

        full_daily = pd.concat(daily_totals_chunks, ignore_index=True)
        final_daily = full_daily.groupby('FECHA')['STOCK_COSTO'].sum().reset_index()
        final_daily.columns = ['FECHA', 'STOCK_TOTAL']
        final_daily.to_csv('daily_stock_totals.csv', index=False)

        print("Detalle, Totales Tienda y Totales Diarios guardados.")
    except Exception as e:
        print(f"Error procesando detalle: {e}")"""
    )

    # Paso 3: Carga y preparacion
    nb.add_code(
        """print("Cargando datos para visualizacion...")
df_metrics = pd.read_csv(CRITICAL_METRICS_HISTORY_FILE, parse_dates=['FECHA'])
df_detail = pd.read_csv(CRITICAL_STOCK_HISTORY_FILE, parse_dates=['FECHA'])
sku_status = pd.read_csv(SKU_STATUS_FILE)
df_store_totals = pd.read_csv(STORE_TOTALS_FILE)
df_daily_totals = pd.read_csv('daily_stock_totals.csv', parse_dates=['FECHA'])

crit_list = sku_status[sku_status['STATUS'] == 'CRITICO']['SKU_PRODUCTO']
df_metrics_crit = df_metrics[df_metrics['SKU_PRODUCTO'].isin(crit_list)]
df_detail_crit = df_detail[df_detail['SKU_PRODUCTO'].isin(crit_list)]

latest_detail_date = df_detail_crit['FECHA'].max()
df_detail_snap = df_detail_crit[df_detail_crit['FECHA'] == latest_detail_date].copy()

def clasificar_canal(row):
    canal = str(row['CANAL_DE_DISTRIBUCION']).upper()
    desc = str(row['DESCRIPCION_SUCURSAL']).upper()
    if 'TIENDA' in canal: return 'TIENDA'
    if 'ETAIL' in canal: return 'ETAIL'
    keywords_segunda = ["SEGUNDA", "MERMA", "OBSOLESCENCIA", "LIQUIDACION", "DESTRUCCION"]
    if any(k in desc for k in keywords_segunda): return 'CD INMOVILIZADO'
    if 'BLUEXPRESS' in desc or 'CD' in canal: return 'CD OPERATIVO'
    return 'OTROS'

df_detail_snap['CANAL_DETALLE'] = df_detail_snap.apply(clasificar_canal, axis=1)
df_detail_crit = df_detail_crit.copy()
df_detail_crit['CANAL_DETALLE'] = df_detail_crit.apply(clasificar_canal, axis=1)
print("Datos listos.")"""
    )

    # Visualizaciones basicas
    nb.add_markdown("# 1. Visualizaciones Basicas")

    nb.add_code(
        """# 1.1 Evolucion % Stock Critico sobre Total
evolucion_critico = df_metrics_crit.groupby('FECHA').agg(STOCK_CRITICO=('STOCK_COSTO', 'sum')).reset_index()
evolucion = pd.merge(evolucion_critico, df_daily_totals, on='FECHA', how='left')
evolucion['PCT_CRITICO'] = evolucion['STOCK_CRITICO'] / evolucion['STOCK_TOTAL']

if not evolucion.empty:
    evolucion = evolucion.sort_values('FECHA')
    idx_start = evolucion.index[0]
    idx_end = evolucion.index[-1]
    idx_max_pct = evolucion['PCT_CRITICO'].idxmax()
    idx_min_pct = evolucion['PCT_CRITICO'].idxmin()
    evolucion['DELTA_PCT'] = evolucion['PCT_CRITICO'].diff()
    idx_max_drop = evolucion['DELTA_PCT'].idxmin()
    key_idxs = list(dict.fromkeys([idx_start, idx_max_pct, idx_max_drop, idx_min_pct, idx_end]))

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(evolucion['FECHA'], evolucion['PCT_CRITICO'], linewidth=2, color='#1f77b4')
    ax.set_title('% de stock critico sobre stock total (con hitos)', pad=12, fontweight='bold')
    ax.set_xlabel('Fecha')
    ax.set_ylabel('% Stock critico')
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, pos: f"{x*100:.1f}%"))
    ax.grid(True, which='major', linestyle='--', linewidth=0.8, alpha=0.4)
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    plt.setp(ax.get_xticklabels(), rotation=45, ha='right')

    for i in key_idxs:
        if i not in evolucion.index: continue
        row = evolucion.loc[i]
        x, y = row['FECHA'], row['PCT_CRITICO']
        if pd.isna(x) or pd.isna(y): continue
        label = f"{row['FECHA'].date()}\\n{row['PCT_CRITICO']*100:.1f}%\\nCritico: ${human_format(row['STOCK_CRITICO'])}\\nTotal: ${human_format(row['STOCK_TOTAL'])}"
        ax.scatter([x], [y], s=40, color='red', zorder=5)
        ax.annotate(label, xy=(x, y), xytext=(10, 15), textcoords='offset points', fontsize=9,
                    va='bottom', bbox=dict(boxstyle='round,pad=0.3', alpha=0.9, fc='white', ec='gray'),
                    arrowprops=dict(arrowstyle='->', alpha=0.6))
    plt.tight_layout()
    plt.show()
else:
    print("No hay datos suficientes para graficar evolucion.")"""
    )

    nb.add_code(
        """# 1.2 Semaforo de Stock con Baja Rotacion
df_full_metrics = pd.read_csv(METRICS_INPUT_FILE, parse_dates=['FECHA'])
latest_date = df_full_metrics['FECHA'].max()
df_last = df_full_metrics[df_full_metrics['FECHA'] == latest_date].copy()
semaforo_df = df_last[(df_last['MOI'] >= 12) | (df_last['MOI'].isna())].copy()

def get_granular_bucket(m):
    if pd.isna(m): return "Sin Info"
    if m < 3: return "< 3m"
    if m < 6: return "3-6m"
    if m < 12: return "6-12m"
    if m < 24: return "12-24m"
    return "> 24m"

semaforo_df['rango_antiguedad'] = semaforo_df['ANTIGUEDAD_MESES'].apply(get_granular_bucket)
tabla_plot = semaforo_df.groupby("rango_antiguedad", as_index=False).agg(stock_clp=("STOCK_COSTO", "sum"), skus=("SKU_PRODUCTO", "nunique"))
total_stock = semaforo_df['STOCK_COSTO'].sum()
tabla_plot['pct'] = tabla_plot['stock_clp'] / total_stock

orden_cat = ["< 3m", "3-6m", "6-12m", "12-24m", "> 24m", "Sin Info"]
tabla_plot['rango_antiguedad'] = pd.Categorical(tabla_plot['rango_antiguedad'], categories=orden_cat, ordered=True)
tabla_plot = tabla_plot.sort_values('rango_antiguedad')

if not tabla_plot.empty:
    fig, ax = plt.subplots(figsize=(12, 7))
    colors_map = {"< 3m": "#2ca02c", "3-6m": "#bcbd22", "6-12m": "#ff7f0e", "12-24m": "#ffbb78", "> 24m": "#d62728", "Sin Info": "#7f7f7f"}
    bar_colors = [colors_map.get(r, 'gray') for r in tabla_plot['rango_antiguedad']]
    bars = ax.barh(tabla_plot['rango_antiguedad'], tabla_plot['stock_clp'], color=bar_colors)
    ax.set_title(f"Semaforo de Stock con Baja Rotacion (MOI>=12 o NaN)\\n(Total Analizado: {human_format(total_stock)})", fontweight='bold', fontsize=14)
    ax.set_xlabel("Monto en Stock (CLP)")
    ax.xaxis.set_major_formatter(FuncFormatter(human_format))
    for bar, row in zip(bars, tabla_plot.itertuples()):
        width = bar.get_width()
        ax.text(width, bar.get_y() + bar.get_height()/2,
                f"  ${human_format(row.stock_clp)} ({row.pct:.1%}) | {row.skus} SKUs",
                va='center', fontweight='bold', fontsize=10)
    plt.tight_layout()
    plt.show()"""
    )

    nb.add_code(
        """# 1.3 Top 15 Bodegas Inmovilizadas
inmov = df_detail_snap[df_detail_snap['CANAL_DETALLE'] == 'CD INMOVILIZADO'].groupby('DESCRIPCION_SUCURSAL')['STOCK_COSTO'].sum().sort_values(ascending=False).head(15)
if not inmov.empty:
    fig, ax = plt.subplots(figsize=(12, 6))
    bars = ax.barh(inmov.index, inmov.values, color='#d62728')
    ax.set_title("Top 15 Bodegas Inmovilizadas (Segunda/Merma)", fontsize=14)
    ax.xaxis.set_major_formatter(FuncFormatter(human_format))
    ax.bar_label(bars, fmt=lambda x: human_format(x), padding=3)
    ax.invert_yaxis()
    plt.show()"""
    )

    # Visualizaciones Avanzadas
    nb.add_markdown("# 2. Visualizaciones Avanzadas")

    nb.add_code(
        """# 2.1 Top 20 Tiendas Criticas
top_stores = df_detail_snap[df_detail_snap['CANAL_DETALLE'] == 'TIENDA'].groupby('DESCRIPCION_SUCURSAL')['STOCK_COSTO'].sum().sort_values(ascending=False).head(20).reset_index()
top_stores = top_stores.merge(df_store_totals, on='DESCRIPCION_SUCURSAL', how='left')
top_stores['PCT_CRITICO'] = (top_stores['STOCK_COSTO_x'] / top_stores['STOCK_COSTO_y']) * 100

fig, ax1 = plt.subplots(figsize=(14, 8))
bars = ax1.barh(top_stores['DESCRIPCION_SUCURSAL'], top_stores['STOCK_COSTO_x'], color='#ff9f43', label='Stock Critico ($)')
ax1.set_xlabel('Monto Stock Critico (CLP)')
ax1.xaxis.set_major_formatter(FuncFormatter(human_format))
ax1.invert_yaxis()

ax2 = ax1.twiny()
ax2.plot(top_stores['PCT_CRITICO'], top_stores.index, color='#57606f', marker='o', linestyle='--', linewidth=1.5, label='% Critico')
ax2.set_xlabel('% Critico sobre Total Tienda')
ax1.set_title("Top 20 Tiendas Criticas: Impacto Financiero y % Inventario Sano", fontsize=16, fontweight='bold')
plt.grid(True, alpha=0.3)
plt.show()"""
    )

    # Export
    nb.add_code(
        """# 2.6 Exportar Detalle Stock Critico
print("Generando archivo Excel con detalle...")
cond_moi = (df_last['MOI'] >= 12) | (df_last['MOI'].isna())
cond_ant = (df_last['ANTIGUEDAD_MESES'] >= 12)
df_crit_650 = df_last[cond_moi & cond_ant].copy()

try:
    if STOCK_FILE_WITH_UNITS:
        print(f"Buscando unidades reales en {STOCK_FILE_WITH_UNITS}...")
        df_stock_units = pd.read_csv(STOCK_FILE_WITH_UNITS)
        df_crit_650['SKU_PRODUCTO'] = df_crit_650['SKU_PRODUCTO'].astype(str)
        df_stock_units['SKU_PRODUCTO'] = df_stock_units['SKU_PRODUCTO'].astype(str)
        if 'FECHA' in df_stock_units.columns:
            max_d = df_stock_units['FECHA'].max()
            df_stock_units = df_stock_units[df_stock_units['FECHA'] == max_d]
        units_by_sku = df_stock_units.groupby('SKU_PRODUCTO')['STOCK_UNIDADES'].sum().reset_index()
        df_crit_650 = df_crit_650.merge(units_by_sku, on='SKU_PRODUCTO', how='left')
        df_crit_650['STOCK_UNIDADES'] = df_crit_650['STOCK_UNIDADES_y'].fillna(0)
        print("Unidades reales cruzadas desde archivo de stock.")
    else:
        df_crit_650['STOCK_UNIDADES'] = 0
except Exception as e:
    print(f"No se pudo cargar archivo de unidades reales: {e}")
    df_crit_650['STOCK_UNIDADES'] = 0

zero_units = df_crit_650['STOCK_UNIDADES'] == 0
if zero_units.any():
    print(f"Calculando estimacion para {zero_units.sum()} SKUs sin unidades reales...")
    if 'EST_UNIT_COST' not in df_crit_650.columns:
        df_sales_exp = pd.read_csv(SALES_INPUT_FILE, usecols=['SKU_PRODUCTO', 'UNIDADES_VENDIDAS', 'NETO_TOTAL', 'MARGEN'])
        sales_agg = df_sales_exp.groupby('SKU_PRODUCTO').agg(TOTAL_UNITS=('UNIDADES_VENDIDAS', 'sum'), TOTAL_NETO=('NETO_TOTAL', 'sum'), AVG_MARGIN=('MARGEN', 'mean')).reset_index()
        sales_agg = sales_agg[sales_agg['TOTAL_UNITS'] > 0].copy()
        sales_agg['AVG_PRICE'] = sales_agg['TOTAL_NETO'] / sales_agg['TOTAL_UNITS']
        sales_agg['EST_UNIT_COST'] = sales_agg['AVG_PRICE'] * (1 - sales_agg['AVG_MARGIN'])
        sales_agg = sales_agg[sales_agg['EST_UNIT_COST'] > 0]
        df_crit_650 = df_crit_650.merge(sales_agg[['SKU_PRODUCTO', 'EST_UNIT_COST']], on='SKU_PRODUCTO', how='left')
    idx_zeros = df_crit_650[df_crit_650['STOCK_UNIDADES'] == 0].index
    if 'EST_UNIT_COST' in df_crit_650.columns:
        df_crit_650.loc[idx_zeros, 'STOCK_UNIDADES'] = (df_crit_650.loc[idx_zeros, 'STOCK_COSTO'] / df_crit_650.loc[idx_zeros, 'EST_UNIT_COST']).fillna(0).round().astype(int)

print(f"Total Unidades Final: {df_crit_650['STOCK_UNIDADES'].sum():,}")
df_crit_650 = df_crit_650.sort_values('STOCK_COSTO', ascending=False)
output_excel = "detalle_stock_critico_650M.xlsx"
df_crit_650.to_excel(output_excel, index=False)
print(f"Archivo '{output_excel}' generado exitosamente.")"""
    )

    return nb.get_notebook_json()
