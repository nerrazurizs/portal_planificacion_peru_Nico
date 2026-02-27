# Stock Base (parametrized - uses %s placeholders for fecha_inicio and fecha_fin)
QUERY_STOCK_BASE = """
select
  a.fecha,
  a.sku_producto,
  a.cod_bodega,
  coalesce(b.id_sucursal, a.cod_bodega) as id_sucursal,
  coalesce(b.descripcion_sucursal, 'Sin descripción') as descripcion_sucursal,
  case
    when b.canal_de_distribucion is not null then b.canal_de_distribucion
    when a.cod_bodega in ('1100001', '1100002') then 'CD'
    else 'TIENDA'
  end as canal_de_distribucion,
  c.nom_producto,
  c.area,
  c.linea,
  c.sublinea,
  c.marca,
  c.mix_oficial,
  c.ultimo_ingreso_cd,
  SUM(a.stock_unidades) as stock_unidades,
  sum(a.stock_costo) as stock_costo,
  sum(a.min_exhibicion) as perfil_tiendas,
  max(d.costo_prom_90_cia) as costo_prom_90_cia,
  (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 as moi,
  case
    when c.ultimo_ingreso_cd is null then null
    when c.ultimo_ingreso_cd > current_date() then 0
    else datediff('day', c.ultimo_ingreso_cd, current_date()) / 30.44
  end as antiguedad_meses,
  case
    when (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 is null then 'Sin MOI'
    when (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 >= 24 then '>= 24 meses'
    when (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 >= 12 then '>= 12 meses'
    when (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 >= 6  then '>= 6 meses'
    when (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 >= 3  then '>= 3 meses'
    else '< 3 meses'
  end as rango_moi,
  case
    when c.ultimo_ingreso_cd is null then 'Sin fecha ingreso'
    when c.ultimo_ingreso_cd > current_date() then '< 3 meses'
    when datediff('day', c.ultimo_ingreso_cd, current_date()) / 30.44 >= 24 then '>= 24 meses'
    when datediff('day', c.ultimo_ingreso_cd, current_date()) / 30.44 >= 12 then '>= 12 meses'
    when datediff('day', c.ultimo_ingreso_cd, current_date()) / 30.44 >= 6  then '>= 6 meses'
    when datediff('day', c.ultimo_ingreso_cd, current_date()) / 30.44 >= 3  then '>= 3 meses'
    else '< 3 meses'
  end as rango_antiguedad
from db_supply.hst.vw_in_stock a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_bodega = b.id_sucursal
left join db_dimensiones.dim.vw_producto c
  on a.sku_producto = c.sku_producto
left join db_supply.hst.vw_in_stock_cd d
  on a.fecha = d.fecha
 and a.sku_producto = d.sku_producto
where a.fecha >= %s and a.fecha <= %s
group by 1,2,3,4,5,6,7,8,9,10,11,12,13
"""

QUERY_MAESTRA = """
select a.*
from db_dimensiones.dim.vw_producto a
"""

# ── Dashboard ejecutivo (queries livianas) ──

QUERY_DASHBOARD_VENTAS_MTD = """
select
    a.cod_canal,
    sum(a.cantidad) as cantidad_mtd,
    sum(a.neto)     as neto_mtd
from db_finanzas.fct.ft_vcm a
left join db_dimensiones.dim.vw_producto c on a.sku_producto = c.sku_producto
where a.cantidad > 0
  and a.fecha >= date_trunc('month', current_date())
  and a.fecha <  current_date()
group by 1
"""

QUERY_DASHBOARD_COMEX = """
select
    count(distinct po) as n_pos,
    sum(cantidad_final_corregida) as und_transito,
    sum(montomn) as costo_transito
from db_supply.fct.ft_compras
where fecha_recepcion_en_cd is null
  and cantidad_final_corregida > 0
"""

QUERY_COMEX_BASE = """
SELECT *,
    CASE
        WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = '' THEN FECHA_ENTREGA
        ELSE ETD
    END as ETD_CALC,
    CASE
        WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = '' THEN DATEADD(day, 47, FECHA_ENTREGA)
        ELSE ETA
    END as ETA_CALC
FROM db_supply.fct.ft_compras
WHERE 1=1
"""

# POs atrasadas: ETA calculada ya paso y aun no han llegado al CD
# POs atrasadas: ETA calculada + 10 dias de buffer (agenda/programacion CD) ya paso
QUERY_COMEX_ATRASADAS = """
SELECT
    PO,
    SKU_PRODUCTO,
    NOM_PRODUCTO,
    NOM_PROVEEDOR,
    CARPETA_COMEX,
    FECHA_ENTREGA,
    ETD,
    ETA,
    CASE
        WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = '' THEN DATEADD(day, 47, FECHA_ENTREGA)
        ELSE ETA
    END AS ETA_CALC,
    GREATEST(0, COALESCE(CANTIDAD_FINAL_CORREGIDA, 0)
                - COALESCE(CANTIDAD_CARPETA_RECEPCIONADA, 0)) AS QTY_PENDIENTE,
    CANTIDAD_FINAL_CORREGIDA,
    MONTOMN,
    AREA,
    LINEA,
    SUBLINEA,
    MARCA,
    DATEDIFF('day',
        DATEADD(day, 10,
            CASE
                WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = ''
                THEN DATEADD(day, 47, FECHA_ENTREGA)
                ELSE ETA
            END
        ),
        CURRENT_DATE()
    ) AS DIAS_ATRASO
FROM db_supply.fct.ft_compras
WHERE FECHA_RECEPCION_EN_CD IS NULL
  AND CANTIDAD_FINAL_CORREGIDA > 0
  AND PO LIKE 'PO-%%'
  AND DATEADD(day, 10,
        CASE
            WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = ''
            THEN DATEADD(day, 47, FECHA_ENTREGA)
            ELSE ETA
        END
      ) < CURRENT_DATE()
ORDER BY ETA_CALC ASC
"""

# POs sin carpeta comex creada (CARPETA_COMEX es NULL o vacia)
QUERY_COMEX_SIN_CARPETA = """
SELECT
    PO,
    SKU_PRODUCTO,
    NOM_PRODUCTO,
    NOM_PROVEEDOR,
    FECHA_ENTREGA,
    CANTIDAD_FINAL_CORREGIDA,
    MONTOMN,
    AREA,
    LINEA,
    DATEDIFF('day', FECHA_ENTREGA, CURRENT_DATE()) AS DIAS_DESDE_ENTREGA
FROM db_supply.fct.ft_compras
WHERE (CARPETA_COMEX IS NULL OR TRIM(CARPETA_COMEX) = '')
  AND FECHA_RECEPCION_EN_CD IS NULL
  AND CANTIDAD_FINAL_CORREGIDA > 0
  AND PO LIKE 'PO-%%'
ORDER BY FECHA_ENTREGA ASC
"""

# POs en transito sin diario de factura creado
QUERY_COMEX_SIN_FACTURA = """
SELECT
    PO,
    SKU_PRODUCTO,
    NOM_PRODUCTO,
    NOM_PROVEEDOR,
    CARPETA_COMEX,
    CASE
        WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = '' THEN FECHA_ENTREGA
        ELSE ETD
    END AS ETD_CALC,
    CASE
        WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = '' THEN DATEADD(day, 47, FECHA_ENTREGA)
        ELSE ETA
    END AS ETA_CALC,
    GREATEST(0, COALESCE(CANTIDAD_FINAL_CORREGIDA, 0)
                - COALESCE(CANTIDAD_CARPETA_RECEPCIONADA, 0)) AS QTY_PENDIENTE,
    CANTIDAD_FINAL_CORREGIDA,
    MONTOMN,
    AREA,
    LINEA,
    SUBLINEA,
    MARCA
FROM db_supply.fct.ft_compras
WHERE FECHA_RECEPCION_EN_CD IS NULL
  AND CANTIDAD_FINAL_CORREGIDA > 0
  AND PO LIKE 'PO-%%'
  AND CASE
        WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = '' THEN FECHA_ENTREGA
        ELSE ETD
      END <= CURRENT_DATE()
  AND FACTURA IS NULL
ORDER BY ETA_CALC ASC
"""

# POs proximas a llegar (ETA within next 45 days)
QUERY_COMEX_PROXIMAS = """
SELECT
    PO,
    SKU_PRODUCTO,
    NOM_PRODUCTO,
    NOM_PROVEEDOR,
    CARPETA_COMEX,
    CASE
        WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = '' THEN FECHA_ENTREGA
        ELSE ETD
    END AS ETD_CALC,
    CASE
        WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = '' THEN DATEADD(day, 47, FECHA_ENTREGA)
        ELSE ETA
    END AS ETA_CALC,
    GREATEST(0, COALESCE(CANTIDAD_FINAL_CORREGIDA, 0)
                - COALESCE(CANTIDAD_CARPETA_RECEPCIONADA, 0)) AS QTY_PENDIENTE,
    CASE
        WHEN COALESCE(CANTIDAD_FINAL_CORREGIDA, 0) > 0
        THEN GREATEST(0, COALESCE(CANTIDAD_FINAL_CORREGIDA, 0)
                         - COALESCE(CANTIDAD_CARPETA_RECEPCIONADA, 0))
             / CANTIDAD_FINAL_CORREGIDA
             * COALESCE(MONTOMN, 0)
        ELSE 0
    END AS MONTO_PENDIENTE_CLP,
    CANTIDAD_FINAL_CORREGIDA,
    MONTOMN,
    AREA,
    LINEA,
    SUBLINEA,
    MARCA,
    DATEDIFF('day', CURRENT_DATE(),
        CASE
            WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = '' THEN DATEADD(day, 47, FECHA_ENTREGA)
            ELSE ETA
        END
    ) AS DIAS_HASTA_ETA
FROM db_supply.fct.ft_compras
WHERE FECHA_RECEPCION_EN_CD IS NULL
  AND CANTIDAD_FINAL_CORREGIDA > 0
  AND PO LIKE 'PO-%%'
  AND CASE
        WHEN CARPETA_COMEX IS NULL OR CARPETA_COMEX = '' THEN DATEADD(day, 47, FECHA_ENTREGA)
        ELSE ETA
      END BETWEEN CURRENT_DATE() AND DATEADD(day, 45, CURRENT_DATE())
ORDER BY ETA_CALC ASC
"""

# Precio promedio de venta por SKU (ultimos 90 dias, para VN_POTENCIAL)
QUERY_PRECIO_PROM_SKU = """
SELECT
    a.sku_producto,
    SUM(a.neto) / NULLIF(SUM(a.cantidad), 0) AS PRECIO_PROM_90D,
    SUM(a.cantidad) AS UNIDADES_90D,
    SUM(a.neto) AS NETO_90D
FROM db_finanzas.fct.ft_vcm a
WHERE a.fecha >= DATEADD('day', -90, CURRENT_DATE())
  AND a.cantidad > 0
GROUP BY 1
"""

# Canasta de Productos (basket analysis)
QUERY_CANASTA = """
SELECT
    a.fecha,
    a.id_venta,
    a.cod_cliente,
    a.cod_canal,
    a.cod_ccosto,
    a.sku_producto,
    a.cantidad,
    a.neto,
    a.aporte,
    c.nom_producto as sku_nom_producto,
    c.area,
    c.linea,
    c.sublinea,
    c.marca
FROM db_finanzas.fct.ft_vcm a
LEFT JOIN db_dimensiones.dim.vw_producto c
    ON a.sku_producto = c.sku_producto
WHERE a.cantidad > 0
  AND a.fecha >= %s
  AND a.fecha <= %s
"""

# Pasillo Infinito
QUERY_PASILLO_INFINITO = """
SELECT
    fecha,
    sku_producto,
    cod_bodega,
    cantidad,
    monto_neto,
    monto_total,
    num_magento
FROM db_pos.fct.ft_venta_pasillo_infinito
WHERE estado = 'Pagado'
"""

# Syncro tables
QUERY_SYNCRO_CONFIG = "select * from db_syncros.public.coo_config_sku_sucursal"
QUERY_SYNCRO_SUCURSAL = "select * from db_syncros.public.coo_maestro_sucursal"

# Dimension Tiendas — enriquecida con lat/lon, cluster, supervisor, m2, distrito, zona, etc.
# Tabla real: db_dimensiones.dim.dv_tienda
# Join: coo_maestro_sucursal.id_sucursal = dv_tienda."Cod_Bodega"
# Columnas con espacios/mayúsculas deben ir entre comillas dobles en Snowflake.
# Lat/lon y Mt2 Totales están almacenados como VARCHAR con coma decimal → REPLACE+TRY_TO_DOUBLE.
# Activa = Status = 'Abierta'.
QUERY_DT_TIENDA = """
SELECT
    b.id_sucursal,
    b.descripcion_sucursal,
    b.canal_de_distribucion,
    coalesce(TRY_TO_DOUBLE(REPLACE(CAST(t."Latitud"      AS VARCHAR), ',', '.')), 0) AS latitud,
    coalesce(TRY_TO_DOUBLE(REPLACE(CAST(t."Longitud"     AS VARCHAR), ',', '.')), 0) AS longitud,
    coalesce(t."Cluster Final",  'Sin Cluster')      AS cluster,
    coalesce(t."Supervisor",     'Sin Supervisor')   AS supervisor,
    coalesce(t."Grupo",          'Sin Grupo')        AS grupo,
    coalesce(t."Tipo",           'Sin Tipo')         AS tipo,
    coalesce(TRY_TO_DOUBLE(REPLACE(CAST(t."Mt2 Totales" AS VARCHAR), ',', '.')), 0) AS mts2,
    coalesce(t."Direccion",      '')                 AS direccion,
    coalesce(CAST(t."Distrito" AS VARCHAR), 'Sin Distrito') AS distrito,
    coalesce(t."Ciudad",         'Sin Ciudad')       AS ciudad,
    coalesce(t."Comuna",         'Sin Comuna')       AS comuna,
    coalesce(t."Zona",           'Sin Zona')         AS zona,
    coalesce(t."Operador",       'Sin Operador')     AS operador,
    CASE WHEN t."Status" = 'Abierta' THEN TRUE ELSE FALSE END AS activa
FROM db_syncros.public.coo_maestro_sucursal b
LEFT JOIN db_dimensiones.dim.dv_tienda t
    ON b.id_sucursal = t."Cod_Bodega"
"""

# Perfil resumen: sucursales con perfil > 0 y total unidades perfil por SKU
QUERY_PERFIL_RESUMEN = """
select
    c.id_material as sku_producto,
    count(distinct c.id_sucursal)   as N_SUC_PERFIL,
    coalesce(sum(c.min_inv_requerido), 0) as TOTAL_PERFIL_UND
from db_syncros.public.coo_config_sku_sucursal c
where c.min_inv_requerido > 0
group by 1
"""
QUERY_SYNCRO_PRODUCTO = "select * from db_syncros.public.coo_maestro_producto"
QUERY_SYNCRO_LEADTIMES = """
SELECT
    t.*,
    p.NOM_PRODUCTO,
    p.MARCA,
    p.LINEA,
    p.SUBLINEA,
    p.AREA
FROM db_syncros.PUBLIC.coo_rel_proveedor_sku t
LEFT JOIN db_dimensiones.dim.vw_producto p
    ON t.ID_MATERIAL = p.SKU_PRODUCTO
"""

# Ventas MTD
QUERY_VENTAS_MTD = """
select
    a.cod_canal,
    a.sku_producto,
    sum(a.cantidad) as cantidad_mtd,
    sum(a.neto)     as neto_mtd,
    sum(a.neto) / nullif(sum(a.cantidad), 0) as precio_prom_mtd
from db_finanzas.fct.ft_vcm a
where a.cantidad > 0
  and a.fecha >= date_trunc('month', current_date())
  and a.fecha <  current_date()
group by 1,2
"""

# Ventas MTD diarias (simulacion diaria: filas HISTORICO dia a dia)
QUERY_VENTAS_MTD_DIARIA = """
select
    a.fecha,
    a.cod_canal,
    a.sku_producto,
    sum(a.cantidad) as cantidad,
    sum(a.neto)     as neto
from db_finanzas.fct.ft_vcm a
where a.cantidad > 0
  and a.fecha >= date_trunc('month', current_date())
  and a.fecha <  current_date()
group by 1, 2, 3
"""

# Ventas Mes Anterior Completo (para filas HISTORICO en proyeccion)
# Nota: NO filtramos a.cantidad > 0 para incluir devoluciones/notas credito en el neto
# El precio_prom usa solo registros con cantidad > 0 (CASE) para evitar distorsion
QUERY_VENTAS_MES_ANTERIOR = """
select
    a.cod_canal,
    a.sku_producto,
    sum(a.cantidad) as cantidad_mes,
    sum(a.neto)     as neto_mes,
    sum(case when a.cantidad > 0 then a.neto else 0 end)
      / nullif(sum(case when a.cantidad > 0 then a.cantidad else 0 end), 0) as precio_prom
from db_finanzas.fct.ft_vcm a
where a.fecha >= date_trunc('month', dateadd('month', -1, current_date()))
  and a.fecha <  date_trunc('month', current_date())
group by 1,2
"""

# Ventas Ano Anterior completo (para comparacion YoY en proyeccion)
# Agrupa por canal, SKU y mes para total anual + drill-down mensual
QUERY_VENTAS_AA = """
select
    a.cod_canal,
    a.sku_producto,
    date_trunc('month', a.fecha) as periodo,
    sum(a.cantidad) as cantidad_aa,
    sum(a.neto)     as neto_aa,
    sum(a.aporte)   as aporte_aa
from db_finanzas.fct.ft_vcm a
where a.fecha >= date_trunc('year', dateadd('year', -1, current_date()))
  and a.fecha <  date_trunc('year', current_date())
group by 1, 2, 3
"""

# Ventas Historicas (13 meses — usado por charts de tendencia)
QUERY_VENTAS_HISTORICAS = """
select
  a.fecha,
  b.id_sucursal,
  a.sku_producto,
  b.canal_de_distribucion,
  sum(a.cantidad) as UNIDADES_VENDIDAS,
  sum(a.neto) as NETO_TOTAL,
  sum(a.neto) / nullif(sum(a.cantidad), 0) as PRECIO_PROMEDIO,
  sum(a.aporte) as APORTE_TOTAL,
  sum(a.aporte) / nullif(sum(a.neto),0) as MARGEN
from db_finanzas.fct.ft_vcm a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_ccosto = b."CUSTOM 1"
where a.fecha >= date_trunc('year', dateadd('year', -1, current_date()))
  and a.fecha < date_trunc('month', current_date())
group by 1,2,3,4
"""

# Ventas YTD (1 Ene ano actual → hoy) — Torre de Control KPIs
QUERY_VENTAS_YTD = """
select
  b.canal_de_distribucion,
  sum(a.cantidad)                                   as UNIDADES_VENDIDAS,
  sum(a.neto)                                       as NETO_TOTAL,
  sum(a.aporte)                                     as APORTE_TOTAL,
  sum(a.aporte) / nullif(sum(a.neto), 0)            as MARGEN
from db_finanzas.fct.ft_vcm a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_ccosto = b."CUSTOM 1"
where a.fecha >= date_trunc('year', current_date())
  and a.fecha <= current_date()
group by 1
"""

# Ventas YTD Ano Anterior (mismo periodo, ano previo — para delta YoY)
QUERY_VENTAS_YTD_AA = """
select
  sum(a.neto)    as NETO_AA,
  sum(a.aporte)  as APORTE_AA
from db_finanzas.fct.ft_vcm a
where a.fecha >= dateadd('year', -1, date_trunc('year', current_date()))
  and a.fecha <= dateadd('year', -1, current_date())
"""

# Stock Proyeccion
QUERY_STOCK_PROYECCION = """
select
  a.fecha,
  a.sku_producto,
  case
    when b.canal_de_distribucion = 'CD' then 'CD'
    else 'TIENDA'
  end as canal_std,
  SUM(a.stock_unidades) as stock_unidades,
  sum(a.min_exhibicion) as perfil_tiendas
from db_supply.hst.vw_in_stock a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_bodega = b.id_sucursal
where a.fecha = (
    select max(fecha)
    from db_supply.hst.vw_in_stock
    where fecha < current_date()
)
and b.canal_de_distribucion in ('TIENDA', 'CD')
group by 1,2,3
"""

# Stock Critico - Metrics (deduplicated: marca and ultimo_ingreso_cd appear once each)
QUERY_STOCK_CRITICO_METRICS = """
select
  a.fecha,
  a.sku_producto,
  c.nom_producto,
  c.area,
  c.linea,
  c.sublinea,
  c.marca,
  c.modelo,
  c.proveedor,
  c.ultimo_ingreso_cd,
  sum(a.stock_costo) as stock_costo,
  sum(a.stock_unidades) as stock_unidades,
  max(d.costo_prom_90_cia) as costo_prom_90_cia,
  (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 as moi,
  case
    when c.ultimo_ingreso_cd is null then null
    when c.ultimo_ingreso_cd > current_date() then 0
    else datediff('day', c.ultimo_ingreso_cd, current_date()) / 30.44
  end as antiguedad_meses,
  case
    when (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 is null then 'Sin MOI'
    when (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 >= 24 then '>= 24 meses'
    when (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 >= 12 then '>= 12 meses'
    when (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 >= 6  then '>= 6 meses'
    when (sum(a.stock_costo) / nullif(max(d.costo_prom_90_cia), 0)) / 30.44 >= 3  then '>= 3 meses'
    else '< 3 meses'
  end as rango_moi,
  case
    when c.ultimo_ingreso_cd is null then 'Sin fecha ingreso'
    when c.ultimo_ingreso_cd > current_date() then '< 3 meses'
    when datediff('day', c.ultimo_ingreso_cd, current_date()) / 30.44 >= 24 then '>= 24 meses'
    when datediff('day', c.ultimo_ingreso_cd, current_date()) / 30.44 >= 12 then '>= 12 meses'
    when datediff('day', c.ultimo_ingreso_cd, current_date()) / 30.44 >= 6  then '>= 6 meses'
    when datediff('day', c.ultimo_ingreso_cd, current_date()) / 30.44 >= 3  then '>= 3 meses'
    else '< 3 meses'
  end as rango_antiguedad
from db_supply.hst.vw_in_stock a
left join db_dimensiones.dim.vw_producto c
  on a.sku_producto = c.sku_producto
left join db_supply.hst.vw_in_stock_cd d
  on a.fecha = d.fecha
 and a.sku_producto = d.sku_producto
where a.fecha >= '2025-01-01'
  and (dayname(a.fecha) = 'Mon' or a.fecha = (select max(fecha) from db_supply.hst.vw_in_stock))
group by 1,2,3,4,5,6,7,8,9,10
"""

# Stock Critico - Detail
# Enriquecido con id_sucursal, supervisor, cluster y mts2 desde dv_tienda
QUERY_STOCK_CRITICO_DETAIL = """
select
  a.fecha,
  a.sku_producto,
  c.nom_producto,
  c.area,
  c.linea,
  c.sublinea,
  c.marca,
  a.cod_bodega,
  coalesce(b.id_sucursal, a.cod_bodega)                                    as id_sucursal,
  coalesce(b.descripcion_sucursal, bo.nom_bodega, 'Bodega ' || a.cod_bodega) as descripcion_sucursal,
  coalesce(b.canal_de_distribucion, 'Sin Canal')                           as canal_de_distribucion,
  coalesce(cast(b.cd as varchar), 'N')                                     as cd,
  coalesce(max(t."Supervisor"),    'Sin Supervisor') as supervisor,
  coalesce(max(t."Cluster Final"), 'Sin Cluster')    as cluster,
  coalesce(max(TRY_TO_DOUBLE(REPLACE(CAST(t."Mt2 Totales" AS VARCHAR), ',', '.'))), 0) as mts2,
  sum(a.stock_costo) as stock_costo,
  sum(a.stock_unidades) as stock_unidades
from db_supply.hst.vw_in_stock a
left join (
    select id_sucursal,
           max(descripcion_sucursal) as descripcion_sucursal,
           max(canal_de_distribucion) as canal_de_distribucion,
           max(cast(cd as varchar)) as cd
    from db_syncros.public.coo_maestro_sucursal
    group by id_sucursal
  ) b on a.cod_bodega = b.id_sucursal
left join (
    select sku_producto,
           max(nom_producto) as nom_producto,
           max(area) as area,
           max(linea) as linea,
           max(sublinea) as sublinea,
           max(marca) as marca
    from db_dimensiones.dim.vw_producto
    group by sku_producto
  ) c on a.sku_producto = c.sku_producto
left join db_dimensiones.dim.dv_tienda t
  on a.cod_bodega = t."Cod_Bodega"
left join (
    select cod_bodega, max(nom_bodega) as nom_bodega
    from db_dimensiones.dim.dt_bodega
    group by cod_bodega
  ) bo on a.cod_bodega = bo.cod_bodega
where a.fecha = (select max(fecha) from db_supply.hst.vw_in_stock)
group by 1,2,3,4,5,6,7,8,9,10,11,12
"""

# Stock Critico - Sales
QUERY_STOCK_CRITICO_SALES = """
select
  a.fecha,
  b.id_sucursal,
  a.sku_producto,
  b.canal_de_distribucion,
  sum(a.cantidad) as unidades_vendidas,
  sum(a.neto) as neto_total,
  sum(a.neto) / nullif(sum(a.cantidad), 0) as precio_promedio,
  sum(a.aporte) as aporte_total,
  sum(a.aporte) / nullif(sum(a.neto),0) as margen
from db_finanzas.fct.ft_vcm a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_ccosto = b."CUSTOM 1"
where a.fecha >= dateadd('month', -12, date_trunc('month', current_date()))
group by 1,2,3,4
"""

# COGS mensual a nivel compania (ultimos 6 meses completos)
# Usado por stock_critico para MOI Historico a nivel CIA
QUERY_COGS_COMPANY_6M = """
select
    date_trunc('month', a.fecha) as PERIODO,
    sum(a.neto) - sum(a.aporte) as COGS_MENSUAL
from db_finanzas.fct.ft_vcm a
where a.cantidad > 0
  and a.fecha >= dateadd('month', -6, date_trunc('month', current_date()))
  and a.fecha <  date_trunc('month', current_date())
group by 1
order by 1
"""

# Stock en transito clasificado para KPIs (en agua vs pendiente zarpe)
QUERY_TRANSIT_STOCK_KPI = """
select
    case
        when (case when c.carpeta_comex is null or c.carpeta_comex = ''
                   then c.fecha_entrega else c.etd end) <= current_date()
         and (case when c.carpeta_comex is null or c.carpeta_comex = ''
                   then dateadd(day, 47, c.fecha_entrega) else c.eta end) > current_date()
        then 'EN_AGUA'
        when (case when c.carpeta_comex is null or c.carpeta_comex = ''
                   then c.fecha_entrega else c.etd end) > current_date()
        then 'PENDIENTE_ZARPE'
        else 'OTRO'
    end as STATUS_TRANSITO,
    c.area,
    c.linea,
    c.sublinea,
    c.marca,
    c.modelo,
    c.nom_proveedor as PROVEEDOR,
    c.sku_producto,
    sum(greatest(0, coalesce(c.cantidad_final_corregida, 0)
                    - coalesce(c.cantidad_carpeta_recepcionada, 0))) as QTY_PENDIENTE,
    sum(case
        when coalesce(c.cantidad_final_corregida, 0) > 0
        then greatest(0, coalesce(c.cantidad_final_corregida, 0)
                         - coalesce(c.cantidad_carpeta_recepcionada, 0))
             / c.cantidad_final_corregida
             * coalesce(c.montomn, 0)
        else 0
    end) as MONTO_PENDIENTE_CLP
from db_supply.fct.ft_compras c
where c.fecha_recepcion_en_cd is null
  and coalesce(c.cantidad_final_corregida, 0) > 0
  and c.po like 'PO-%%'
group by 1, 2, 3, 4, 5, 6, 7, 8
"""

# Forecast Mirror queries
QUERY_MIRROR_QUALITY = """
SELECT
    a.sku_producto as espejo,
    YEAR(a.fecha) as anio,
    MONTH(a.fecha) as mes,
    COUNT(DISTINCT a.fecha) as dias_con_venta
FROM db_finanzas.fct.ft_vcm a
WHERE a.sku_producto IN ({placeholders})
  AND a.fecha BETWEEN %s AND %s
  AND a.cantidad > 0
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""

QUERY_MIRROR_HIERARCHY = """
SELECT sku_producto as espejo, linea, sublinea, area
FROM db_dimensiones.dim.vw_producto
WHERE sku_producto IN ({placeholders})
"""

QUERY_MIRROR_HIST_SKU = """
SELECT
    a.sku_producto as espejo,
    b.id_sucursal,
    a.fecha,
    SUM(a.cantidad) as venta_qty
FROM db_finanzas.fct.ft_vcm a
LEFT JOIN db_syncros.public.coo_maestro_sucursal b ON a.cod_ccosto = b."CUSTOM 1"
WHERE a.sku_producto IN ({placeholders})
  AND a.fecha BETWEEN %s AND %s
  AND b.canal_de_distribucion = 'TIENDA'
  AND a.cantidad > 0
GROUP BY 1, 2, 3
"""

QUERY_MIRROR_SUCURSAL = """
SELECT id_sucursal, descripcion_sucursal, canal_de_distribucion as canal
FROM db_syncros.public.coo_maestro_sucursal
"""

# Mirror monthly sales by sucursal + canal (for forecast-from-mirror generation)
QUERY_MIRROR_HIST_MONTHLY = """
SELECT
    a.sku_producto as espejo,
    b.id_sucursal,
    b.descripcion_sucursal,
    b.canal_de_distribucion as canal,
    DATE_TRUNC('month', a.fecha) as periodo,
    SUM(a.cantidad) as venta_qty
FROM db_finanzas.fct.ft_vcm a
LEFT JOIN db_syncros.public.coo_maestro_sucursal b
    ON a.cod_ccosto = b."CUSTOM 1"
WHERE a.sku_producto = %s
  AND a.fecha BETWEEN %s AND %s
  AND a.cantidad > 0
  AND b.id_sucursal IS NOT NULL
GROUP BY 1, 2, 3, 4, 5
ORDER BY 5, 2
"""

# Comex Full (used by proyeccion module)
QUERY_COMEX_FULL = """
select *
from db_supply.fct.ft_compras
"""

# Plan de Compras — tránsitos y POs con campos financieros clave
# Nombres de columnas reales en ft_compras:
#   PO, SKU_PRODUCTO, NOM_PRODUCTO, NOM_PROVEEDOR, COD_PROVEEDOR, COD_MONEDA
#   NOM_ESTADOAPROBACION, NOM_STATUS, ENTRANSITO, CARPETA_COMEX, FORMA_PAGO
#   CANTIDAD_FINAL_CORREGIDA (qty ordenada corregida), CANTIDAD_CARPETA_RECEPCIONADA
#   SALDO (qty pendiente ya calculada), PURCHPRICE, LINEAMOUNT, MONTOMN, PARIDAD_MONEDA
#   ETA, ETD, FECHA_ENTREGA, FECHA_RECEPCION_EN_CD, ANO_ETA, MES_ETA
#   AREA, LINEA, SUBLINEA, MARCA, MODELO, FACTOR_IMPORTACION, DOLAR_SISTEMA
QUERY_PLAN_COMPRAS = """
select
    c.po                                                    as N_PO,
    c.sku_producto,
    c.nom_producto,
    c.area,
    c.linea,
    c.sublinea,
    c.marca,
    c.modelo,
    c.nom_proveedor,
    c.cod_proveedor,
    c.cod_moneda                                            as MONEDA,
    c.nom_estadoaprobacion                                  as ESTADO_APROBACION,
    c.nom_status                                            as STATUS_PO,
    c.entransito                                            as ESTA_EN_TRANSITO,
    c.tiene_bl,
    c.tiene_carpeta_comex,
    c.status_booking,
    c.carpeta_comex,
    c.forma_pago,
    -- Fechas clave (ETA_CALC / ETD_CALC ya están calculadas en la vista)
    case
        when c.carpeta_comex is null or c.carpeta_comex = ''
        then c.fecha_entrega
        else c.etd
    end                                                     as ETD_CALC,
    case
        when c.carpeta_comex is null or c.carpeta_comex = ''
        then dateadd(day, 47, c.fecha_entrega)
        else c.eta
    end                                                     as ETA_CALC,
    c.fecha_recepcion_en_cd,
    -- Cantidades
    c.cantidad_final_corregida                              as QTY_ORDENADA,
    c.cantidad_carpeta_recepcionada                         as QTY_RECEPCIONADA,
    -- QTY_PENDIENTE = max(0, ordenada - recepcionada) para evitar negativos
    greatest(0, coalesce(c.cantidad_final_corregida, 0)
                - coalesce(c.cantidad_carpeta_recepcionada, 0))
                                                            as QTY_PENDIENTE,
    -- Montos en moneda original
    c.purchprice                                            as PRECIO_UNITARIO,
    c.lineamount                                            as MONTO_MONEDA_ORIG,
    -- Montos en CLP
    c.montomn                                               as MONTO_CLP,
    c.paridad_moneda                                        as TC_PO,
    c.dolar_sistema                                         as TC_SISTEMA,
    c.factor_importacion,
    -- Monto pendiente CLP (proporcional a qty pendiente / qty ordenada)
    case
        when coalesce(c.cantidad_final_corregida, 0) > 0
        then greatest(0, coalesce(c.cantidad_final_corregida, 0)
                         - coalesce(c.cantidad_carpeta_recepcionada, 0))
             / c.cantidad_final_corregida
             * coalesce(c.montomn, 0)
        else 0
    end                                                     as MONTO_PENDIENTE_CLP,
    -- Periodos para agrupación mensual (ya vienen calculados en la vista)
    date_trunc('month',
        case
            when c.carpeta_comex is null or c.carpeta_comex = ''
            then dateadd(day, 47, c.fecha_entrega)
            else c.eta
        end
    )                                                       as PERIODO_ETA,
    date_trunc('month', c.fecha_recepcion_en_cd)            as PERIODO_RECEPCION,
    c.ano_eta,
    c.mes_eta,
    c.anomes_eta
from db_supply.fct.ft_compras c
where c.po <> 'N/A'
  and coalesce(c.cantidad_final_corregida, 0) > 0
"""

# Venta a costo mensual proyectada para plan de compras
# Lógica: promedio mensual últimos 6 meses de APORTE (= venta a costo) por Área/Línea
# Se usa como estimación de salida de inventario mes a mes
QUERY_VENTA_COSTO_PROYECTADA = """
select
    c.area,
    c.linea,
    date_trunc('month',
        dateadd('month',
            datediff('month',
                date_trunc('month', a.fecha),
                date_trunc('month', current_date())
            ) * -1 + datediff('month',
                date_trunc('month', a.fecha),
                date_trunc('month', current_date())
            ),
            date_trunc('month', current_date())
        )
    )                                   as PERIODO_FUTURO,
    avg(monthly_aporte)                 as VENTA_COSTO_PROM
from (
    select
        a.sku_producto,
        date_trunc('month', a.fecha)    as mes,
        sum(a.aporte)                   as monthly_aporte
    from db_finanzas.fct.ft_vcm a
    where a.fecha >= dateadd('month', -6, date_trunc('month', current_date()))
      and a.fecha <  date_trunc('month', current_date())
      and a.cantidad > 0
    group by 1, 2
) ventas_sku
left join db_dimensiones.dim.vw_producto c
    on ventas_sku.sku_producto = c.sku_producto
where c.area is not null and c.linea is not null
group by c.area, c.linea, 3
"""

# Versión simplificada: promedio mensual de aporte por Área/Línea (últimos 6 meses)
QUERY_VENTA_COSTO_HIST = """
select
    c.area,
    c.linea,
    date_trunc('month', a.fecha)        as PERIODO,
    sum(a.aporte)                       as VENTA_COSTO_CLP
from db_finanzas.fct.ft_vcm a
left join db_dimensiones.dim.vw_producto c
    on a.sku_producto = c.sku_producto
where a.fecha >= dateadd('month', -6, date_trunc('month', current_date()))
  and a.fecha <  date_trunc('month', current_date())
  and a.cantidad > 0
  and c.area is not null
  and c.linea is not null
group by 1, 2, 3
"""

# Stock on hand actual por SKU (CD + Tienda) para plan de compras
# Usa ft_in_stock (fecha más reciente < hoy) + maestro sucursal para canal
QUERY_STOCK_ONHAND = """
select
    a.sku_producto,
    sum(case when b.canal_de_distribucion = 'CD'    then a.stock_unidades else 0 end) as STOCK_CD,
    sum(case when b.canal_de_distribucion = 'TIENDA' then a.stock_unidades else 0 end) as STOCK_TIENDA,
    sum(a.stock_unidades)                                                              as STOCK_TOTAL,
    sum(a.stock_costo)                                                                 as STOCK_COSTO_TOTAL
from db_supply.hst.vw_in_stock a
left join db_syncros.public.coo_maestro_sucursal b
    on a.cod_bodega = b.id_sucursal
where a.fecha = (
    select max(fecha)
    from db_supply.hst.vw_in_stock
    where fecha < current_date()
)
group by 1
"""

# Ventas diarias ultimos 90 dias (usado por alertas_quiebre)
QUERY_VENTAS_DIARIAS_90D = """
select
    a.sku_producto,
    a.fecha,
    b.canal_de_distribucion,
    sum(a.cantidad) as unidades,
    sum(a.neto)     as neto
from db_finanzas.fct.ft_vcm a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_ccosto = b."CUSTOM 1"
where a.fecha >= dateadd('day', -90, current_date())
  and a.cantidad > 0
group by 1, 2, 3
"""

# Ventas mensuales por SKU x Canal (usado por elasticidad - precio vs demanda)
QUERY_VENTAS_MENSUAL_PRECIO = """
select
    a.sku_producto,
    a.cod_canal,
    date_trunc('month', a.fecha) as periodo,
    sum(a.cantidad) as cantidad,
    sum(a.neto)     as neto,
    sum(a.aporte)   as aporte,
    sum(case when a.cantidad > 0 then a.neto else 0 end)
      / nullif(sum(case when a.cantidad > 0 then a.cantidad else 0 end), 0) as precio_promedio
from db_finanzas.fct.ft_vcm a
where a.fecha >= dateadd('month', -24, date_trunc('month', current_date()))
  and a.fecha <  date_trunc('month', current_date())
group by 1, 2, 3
having sum(a.cantidad) > 0
"""

# Ventas semanales ultimo ano (usado por abc_xyz)
QUERY_VENTAS_SEMANALES = """
select
    a.sku_producto,
    date_trunc('week', a.fecha) as semana,
    sum(a.cantidad) as unidades,
    sum(a.aporte)   as aporte
from db_finanzas.fct.ft_vcm a
where a.fecha >= dateadd('year', -1, current_date())
  and a.cantidad > 0
group by 1, 2
"""

# Store-level InStock snapshot (last date) — used by InStock Proyectado.
# Returns one row per SKU×Store with the EXACT same fields that
# QUERY_INSTOCK_DAILY_TIENDA aggregates, so the re-computed IS%
# matches the pre-aggregated dashboard perfectly.
QUERY_INSTOCK_STORE_DETAIL = """
select
    a.sku_producto,
    b.id_sucursal,
    b.canal_de_distribucion,
    a.perfil,
    a.stock_unidades,
    coalesce(a.cantidad_prom_90, 0) as cantidad_prom_90
from db_supply.hst.vw_in_stock a
join db_syncros.public.coo_maestro_sucursal b
    on a.cod_bodega = b.id_sucursal
where b.canal_de_distribucion = 'TIENDA'
  and a.fecha = (
      select max(fecha)
      from db_supply.hst.vw_in_stock
      where fecha < current_date()
  )
  and a.perfil = 'SI'
"""

# Stock por SKU desglosado CD vs TIENDA con perfil (usado por higiene abastecimiento)
QUERY_STOCK_HIGIENE = """
select
    a.sku_producto,
    b.id_sucursal,
    b.descripcion_sucursal,
    b.canal_de_distribucion,
    sum(a.stock_unidades)  as stock_unidades,
    sum(a.min_exhibicion)  as perfil_tiendas
from db_supply.hst.vw_in_stock a
left join db_syncros.public.coo_maestro_sucursal b
    on a.cod_bodega = b.id_sucursal
where a.fecha = (
    select max(fecha)
    from db_supply.hst.vw_in_stock
    where fecha < current_date()
)
group by 1, 2, 3, 4
having sum(a.stock_unidades) > 0 or sum(a.min_exhibicion) > 0
"""

# Forecast anual por SKU x Canal (usado por higiene abastecimiento)
# Trae forecast del periodo actual hasta fin de ano
QUERY_FORECAST_ANUAL = """
select
    a.cod_canal,
    a.sku_producto,
    date_trunc('month', a.fecha) as periodo,
    sum(a.cantidad) as forecast_qty
from db_finanzas.fct.ft_vcm a
where a.fecha >= date_trunc('year', current_date())
  and a.fecha <  dateadd('year', 1, date_trunc('year', current_date()))
  and a.cantidad > 0
group by 1, 2, 3
"""

# Ventas semanales por SKU (ultimas 24 semanas, para deteccion de tendencia)
QUERY_VENTAS_SEMANAL_TENDENCIA = """
select
    a.sku_producto,
    date_trunc('week', a.fecha) as semana,
    sum(a.cantidad) as unidades,
    sum(a.neto)     as neto
from db_finanzas.fct.ft_vcm a
where a.fecha >= dateadd('week', -24, current_date())
  and a.cantidad > 0
group by 1, 2
"""

# ---------------------------------------------------------------------------
# InStock Historico — tienda level (aggregated across stores per SKU per date)
# Sampled on Mondays + latest available date for performance.
# Includes CD InStock join so Python can filter by "solo SKUs con InStock CD=1".
# ---------------------------------------------------------------------------
QUERY_INSTOCK_HIST_TIENDA = """
select
    a.fecha,
    a.sku_producto,
    c.area,
    c.linea,
    c.sublinea,
    c.marca,
    c.mix_oficial,
    -- All tiendas
    count(*)                                                as n_tiendas,
    -- ── IS CALCULADO v.A: stock >= vta_prom (promedio todos los dias) ───
    count(*)                                                as n_tiendas_is90,
    count(*)                                                as n_tiendas_is180,
    count(*)                                                as n_tiendas_is365,
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_90, 0)
             then 1 else 0 end)                             as tiendas_is90,
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_180, 0)
             then 1 else 0 end)                             as tiendas_is180,
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_365, 0)
             then 1 else 0 end)                             as tiendas_is365,
    -- ── IS CALCULADO v.B: stock >= vta_prom (promedio solo dias con stock)
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_90b, 0)
             then 1 else 0 end)                             as tiendas_is90b,
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_180b, 0)
             then 1 else 0 end)                             as tiendas_is180b,
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_365b, 0)
             then 1 else 0 end)                             as tiendas_is365b,
    sum(coalesce(a.in_stock_presentacion, 0))               as tiendas_is_pres,
    sum(coalesce(a.stock_unidades, 0))                      as stock_und_tienda,
    sum(coalesce(a.stock_costo, 0))                         as stock_costo_tienda,
    -- PERFIL=SI: denominadores = total perfil=SI
    sum(case when a.perfil = 'SI' then 1 else 0 end)
                                                            as n_tiendas_is90_perfil,
    sum(case when a.perfil = 'SI' then 1 else 0 end)
                                                            as n_tiendas_is180_perfil,
    sum(case when a.perfil = 'SI' then 1 else 0 end)
                                                            as n_tiendas_is365_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_90, 0)
             then 1 else 0 end)
                                                            as tiendas_is90_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_180, 0)
             then 1 else 0 end)
                                                            as tiendas_is180_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_365, 0)
             then 1 else 0 end)
                                                            as tiendas_is365_perfil,
    -- CD InStock calculado
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_90_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_90,
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_180_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_180,
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_365_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_365
from db_supply.hst.vw_in_stock a
join db_syncros.public.coo_maestro_sucursal b
    on a.cod_bodega = b.id_sucursal
left join db_dimensiones.dim.vw_producto c
    on a.sku_producto = c.sku_producto
left join db_supply.hst.vw_in_stock_cd d
    on a.fecha = d.fecha
   and a.sku_producto = d.sku_producto
where b.canal_de_distribucion = 'TIENDA'
  and a.fecha >= '2023-01-01'
  and (dayname(a.fecha) = 'Mon'
       or a.fecha = (select max(fecha) from db_supply.hst.vw_in_stock))
group by 1, 2, 3, 4, 5, 6, 7
"""

# InStock Historico — CD level (one row per SKU per date)
QUERY_INSTOCK_HIST_CD = """
select
    a.fecha,
    a.sku_producto,
    c.area,
    c.linea,
    c.sublinea,
    c.marca,
    c.mix_oficial,
    a.stock_unidades                   as stock_und_cd,
    a.stock_costo                      as stock_costo_cd,
    -- IS calculado: stock >= vta prom cia
    case when a.stock_unidades > 0
         and a.stock_unidades >= coalesce(a.cantidad_prom_90_cia, 0)
         then 1 else 0 end            as instock_cd_90,
    case when a.stock_unidades > 0
         and a.stock_unidades >= coalesce(a.cantidad_prom_180_cia, 0)
         then 1 else 0 end            as instock_cd_180,
    case when a.stock_unidades > 0
         and a.stock_unidades >= coalesce(a.cantidad_prom_365_cia, 0)
         then 1 else 0 end            as instock_cd_365,
    a.cantidad_prom_90_cia,
    a.costo_prom_90_cia
from db_supply.hst.vw_in_stock_cd a
left join db_dimensiones.dim.vw_producto c
    on a.sku_producto = c.sku_producto
where a.fecha >= '2023-01-01'
  and (dayname(a.fecha) = 'Mon'
       or a.fecha = (select max(fecha) from db_supply.hst.vw_in_stock_cd))
"""

# InStock diario (últimos 30 días, todos los días — para vista PowerBI)
QUERY_INSTOCK_DAILY_TIENDA = """
select
    a.fecha,
    a.sku_producto,
    c.area,
    c.linea,
    c.sublinea,
    c.marca,
    c.mix_oficial,
    count(*)                                                as n_tiendas,
    -- IS CALCULADO v.A: stock >= vta_prom (promedio todos los dias)
    count(*)                                                as n_tiendas_is90,
    count(*)                                                as n_tiendas_is180,
    count(*)                                                as n_tiendas_is365,
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_90, 0)
             then 1 else 0 end)                             as tiendas_is90,
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_180, 0)
             then 1 else 0 end)                             as tiendas_is180,
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_365, 0)
             then 1 else 0 end)                             as tiendas_is365,
    -- IS CALCULADO v.B: stock >= vta_prom (promedio solo dias con stock)
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_90b, 0)
             then 1 else 0 end)                             as tiendas_is90b,
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_180b, 0)
             then 1 else 0 end)                             as tiendas_is180b,
    sum(case when a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_365b, 0)
             then 1 else 0 end)                             as tiendas_is365b,
    sum(coalesce(a.in_stock_presentacion, 0))               as tiendas_is_pres,
    sum(coalesce(a.stock_unidades, 0))                      as stock_und_tienda,
    sum(coalesce(a.stock_costo, 0))                         as stock_costo_tienda,
    -- PERFIL=SI
    sum(case when a.perfil = 'SI' then 1 else 0 end)
                                                            as n_tiendas_is90_perfil,
    sum(case when a.perfil = 'SI' then 1 else 0 end)
                                                            as n_tiendas_is180_perfil,
    sum(case when a.perfil = 'SI' then 1 else 0 end)
                                                            as n_tiendas_is365_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_90, 0)
             then 1 else 0 end)
                                                            as tiendas_is90_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_180, 0)
             then 1 else 0 end)
                                                            as tiendas_is180_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_365, 0)
             then 1 else 0 end)
                                                            as tiendas_is365_perfil,
    -- CD InStock calculado
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_90_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_90,
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_180_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_180,
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_365_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_365
from db_supply.hst.vw_in_stock a
join db_syncros.public.coo_maestro_sucursal b
    on a.cod_bodega = b.id_sucursal
left join db_dimensiones.dim.vw_producto c
    on a.sku_producto = c.sku_producto
left join db_supply.hst.vw_in_stock_cd d
    on a.fecha = d.fecha
   and a.sku_producto = d.sku_producto
where b.canal_de_distribucion = 'TIENDA'
  and a.fecha >= dateadd('day', -30, current_date())
group by 1, 2, 3, 4, 5, 6, 7
"""

QUERY_INSTOCK_DAILY_CD = """
select
    a.fecha,
    a.sku_producto,
    c.area,
    c.linea,
    c.sublinea,
    c.marca,
    c.mix_oficial,
    a.stock_unidades                   as stock_und_cd,
    a.stock_costo                      as stock_costo_cd,
    -- IS calculado: stock >= vta prom cia
    case when a.stock_unidades > 0
         and a.stock_unidades >= coalesce(a.cantidad_prom_90_cia, 0)
         then 1 else 0 end            as instock_cd_90,
    case when a.stock_unidades > 0
         and a.stock_unidades >= coalesce(a.cantidad_prom_180_cia, 0)
         then 1 else 0 end            as instock_cd_180,
    case when a.stock_unidades > 0
         and a.stock_unidades >= coalesce(a.cantidad_prom_365_cia, 0)
         then 1 else 0 end            as instock_cd_365,
    a.cantidad_prom_90_cia,
    a.costo_prom_90_cia
from db_supply.hst.vw_in_stock_cd a
left join db_dimensiones.dim.vw_producto c
    on a.sku_producto = c.sku_producto
where a.fecha >= dateadd('day', -30, current_date())
"""

# Ventas diarias por SKU (ultimo ano, para desagregacion y patrones)
QUERY_VENTAS_DIARIAS_PATRON = """
select
    a.sku_producto,
    a.fecha,
    dayofweek(a.fecha) as dia_semana,
    day(a.fecha)       as dia_mes,
    sum(a.cantidad) as unidades,
    sum(a.neto)     as neto
from db_finanzas.fct.ft_vcm a
where a.fecha >= dateadd('year', -1, current_date())
  and a.cantidad > 0
group by 1, 2, 3, 4
"""

# Pesos agregados DOW + WOM (para desagregacion diaria ponderada)
# dayname() devuelve 'Mon','Tue',... → se mapea a Python weekday en el loader
QUERY_PESOS_DIARIOS = """
with base as (
    select
        a.fecha,
        dayname(a.fecha)                as dow_name,
        ceil(day(a.fecha) / 7.0)::int   as wom,
        sum(a.cantidad)                 as unidades
    from db_finanzas.fct.ft_vcm a
    where a.fecha >= dateadd('month', -12, date_trunc('month', current_date()))
      and a.cantidad > 0
    group by 1, 2, 3
),
total_und as (select sum(unidades) as total from base)
select
    b.dow_name,
    b.wom,
    sum(b.unidades)                                  as unidades,
    sum(b.unidades) / nullif(t.total, 0)             as peso
from base b
cross join total_und t
group by b.dow_name, b.wom, t.total
order by b.dow_name, b.wom
"""

# Pesos DOW × WOM × CANAL (para desagregacion diaria per-canal)
# Cada canal tiene su propio patrón semanal:
#   ETAIL: Lun~20% peak, cae a Sáb~11%
#   TIENDA: Sáb~24% peak, Lun-Jue~11%
#   MAYORISTA: Lun/Mié/Vie~21%, Sáb/Dom~3-5%
QUERY_PESOS_DIARIOS_CANAL = """
with base as (
    select
        a.fecha,
        dayname(a.fecha)                as dow_name,
        ceil(day(a.fecha) / 7.0)::int   as wom,
        b.canal_de_distribucion         as canal,
        sum(a.cantidad)                 as unidades
    from db_finanzas.fct.ft_vcm a
    left join db_syncros.public.coo_maestro_sucursal b
        on a.cod_ccosto = b."CUSTOM 1"
    where a.fecha >= dateadd('month', -12, date_trunc('month', current_date()))
      and a.cantidad > 0
      and b.canal_de_distribucion in ('TIENDA', 'ETAIL', 'MAYORISTA')
    group by 1, 2, 3, 4
),
canal_total as (
    select canal, sum(unidades) as total
    from base
    group by canal
)
select
    b.dow_name,
    b.wom,
    b.canal,
    sum(b.unidades)                                  as unidades,
    sum(b.unidades) / nullif(t.total, 0)             as peso
from base b
inner join canal_total t on b.canal = t.canal
group by b.dow_name, b.wom, b.canal, t.total
order by b.canal, b.dow_name, b.wom
"""

# Boosts por SUBLINEA × CANAL × EVENTO (data-driven desde historial de ventas)
# Calcula ratio = promedio diario en ventana evento / promedio diario fuera de evento (mismo mes)
# Usa últimos 2 años para tener al menos 2 ocurrencias de cada evento
QUERY_EVENT_BOOSTS = """
with ventas as (
    select
        a.fecha,
        p.sublinea,
        b.canal_de_distribucion  as canal,
        sum(a.cantidad)          as unidades
    from db_finanzas.fct.ft_vcm a
    left join db_syncros.public.coo_maestro_sucursal b
        on a.cod_ccosto = b."CUSTOM 1"
    left join db_dimensiones.dim.vw_producto p
        on a.sku_producto = p.sku_producto
    where a.fecha >= dateadd('year', -2, current_date())
      and a.cantidad > 0
      and b.canal_de_distribucion in ('TIENDA', 'ETAIL', 'MAYORISTA')
      and p.sublinea is not null
    group by 1, 2, 3
),
classified as (
    select *,
        date_trunc('month', fecha) as periodo,
        case
            when month(fecha) = 1 and day(fecha) <= 25                         then 'SaleVerano'
            when (month(fecha) = 3 and day(fecha) >= 16)
              or (month(fecha) = 4 and day(fecha) <= 5)                        then 'BlackWeek'
            when month(fecha) = 5 and day(fecha) >= 25                         then 'ExpoBebe'
            when month(fecha) = 6 and day(fecha) <= 14                         then 'CyberDay'
            when month(fecha) = 6 and day(fecha) >= 15 and day(fecha) <= 21    then 'DiaPadre'
            when month(fecha) = 8 and day(fecha) >= 3 and day(fecha) <= 16     then 'DiaNino'
            when month(fecha) = 10 and day(fecha) >= 5 and day(fecha) <= 18    then 'CyberOctubre'
            when month(fecha) = 11 and day(fecha) >= 9 and day(fecha) <= 15    then 'ToysWeek'
            when (month(fecha) = 11 and day(fecha) >= 17)
              or (month(fecha) = 12 and day(fecha) <= 6)                       then 'BlackFriday'
            when month(fecha) = 12 and day(fecha) >= 7 and day(fecha) <= 24    then 'Navidad'
            else 'Normal'
        end as evento
    from ventas
),
daily_avg as (
    select
        sublinea, canal, periodo,
        evento,
        sum(unidades)       as und_total,
        count(distinct fecha) as n_dias,
        sum(unidades) / nullif(count(distinct fecha), 0) as prom_diario
    from classified
    group by 1, 2, 3, 4
),
event_data as (
    select sublinea, canal, periodo, evento, prom_diario
    from daily_avg
    where evento != 'Normal'
),
normal_data as (
    select sublinea, canal, periodo, prom_diario as prom_normal
    from daily_avg
    where evento = 'Normal'
),
ratios as (
    select
        e.sublinea,
        e.canal,
        e.evento,
        e.periodo,
        case when n.prom_normal > 0 then e.prom_diario / n.prom_normal else 0 end as ratio
    from event_data e
    inner join normal_data n
        on e.sublinea = n.sublinea and e.canal = n.canal and e.periodo = n.periodo
)
select
    sublinea,
    canal,
    evento,
    median(ratio)           as boost_mediano,
    avg(ratio)              as boost_promedio,
    count(distinct periodo) as n_periodos
from ratios
group by 1, 2, 3
having n_periodos >= 2
order by sublinea, canal, evento
"""

# Boosts por SKU × CANAL × EVENTO (data-driven, granular)
# Misma lógica que QUERY_EVENT_BOOSTS pero a nivel SKU individual.
# Solo incluye SKUs con al menos 5 und vendidas en ventana evento
# y con dato "Normal" en el mismo periodo para calcular ratio confiable.
QUERY_EVENT_BOOSTS_SKU = """
with ventas as (
    select
        a.sku_producto,
        a.fecha,
        b.canal_de_distribucion  as canal,
        sum(a.cantidad)          as unidades
    from db_finanzas.fct.ft_vcm a
    left join db_syncros.public.coo_maestro_sucursal b
        on a.cod_ccosto = b."CUSTOM 1"
    where a.fecha >= dateadd('year', -2, current_date())
      and a.cantidad > 0
      and b.canal_de_distribucion in ('TIENDA', 'ETAIL', 'MAYORISTA')
    group by 1, 2, 3
),
classified as (
    select *,
        date_trunc('month', fecha) as periodo,
        case
            when month(fecha) = 1 and day(fecha) <= 25                         then 'SaleVerano'
            when (month(fecha) = 3 and day(fecha) >= 16)
              or (month(fecha) = 4 and day(fecha) <= 5)                        then 'BlackWeek'
            when month(fecha) = 5 and day(fecha) >= 25                         then 'ExpoBebe'
            when month(fecha) = 6 and day(fecha) <= 14                         then 'CyberDay'
            when month(fecha) = 6 and day(fecha) >= 15 and day(fecha) <= 21    then 'DiaPadre'
            when month(fecha) = 8 and day(fecha) >= 3 and day(fecha) <= 16     then 'DiaNino'
            when month(fecha) = 10 and day(fecha) >= 5 and day(fecha) <= 18    then 'CyberOctubre'
            when month(fecha) = 11 and day(fecha) >= 9 and day(fecha) <= 15    then 'ToysWeek'
            when (month(fecha) = 11 and day(fecha) >= 17)
              or (month(fecha) = 12 and day(fecha) <= 6)                       then 'BlackFriday'
            when month(fecha) = 12 and day(fecha) >= 7 and day(fecha) <= 24    then 'Navidad'
            else 'Normal'
        end as evento
    from ventas
),
daily_avg as (
    select
        sku_producto, canal, periodo,
        evento,
        sum(unidades)                                       as und_total,
        count(distinct fecha)                               as n_dias,
        sum(unidades) / nullif(count(distinct fecha), 0)    as prom_diario
    from classified
    group by 1, 2, 3, 4
),
event_data as (
    select sku_producto, canal, periodo, evento, prom_diario, und_total
    from daily_avg
    where evento != 'Normal'
      and und_total >= 5
),
normal_data as (
    select sku_producto, canal, periodo, prom_diario as prom_normal
    from daily_avg
    where evento = 'Normal'
      and prom_diario > 0
),
ratios as (
    select
        e.sku_producto, e.canal, e.evento, e.periodo,
        e.prom_diario / n.prom_normal as ratio
    from event_data e
    inner join normal_data n
        on e.sku_producto = n.sku_producto
       and e.canal = n.canal
       and e.periodo = n.periodo
    where n.prom_normal > 0
)
select
    sku_producto,
    canal,
    evento,
    median(ratio)           as boost_mediano,
    avg(ratio)              as boost_promedio,
    count(distinct periodo) as n_periodos
from ratios
group by 1, 2, 3
having n_periodos >= 2
order by sku_producto, canal, evento
"""

# ── Redistribucion de Stock ─────────────────────────────────────────

# Transito CD → Tiendas (columnas desconocidas — SELECT * + norm_cols)
QUERY_TRANSITO_ENTRE_SUCURSALES = """
select *
from DJCHL_SYNCROS.PUBLIC.COO_INVENTARIO_TRANSITO_ENTRE_SUCURSALES
"""

# Ventas ultimos 90 dias por SKU × Sucursal (velocidad por tienda)
QUERY_VENTAS_90D_SUCURSAL = """
select
    a.sku_producto,
    b.id_sucursal,
    b.descripcion_sucursal,
    b.canal_de_distribucion,
    sum(a.cantidad)         as unidades_90d,
    sum(a.neto)             as neto_90d,
    count(distinct a.fecha) as dias_con_venta
from db_finanzas.fct.ft_vcm a
left join db_syncros.public.coo_maestro_sucursal b
    on a.cod_ccosto = b."CUSTOM 1"
where a.fecha >= dateadd('day', -90, current_date())
  and a.cantidad > 0
group by 1, 2, 3, 4
"""


# ===========================================================================
# SUPPLY OPERATIONS — Pedidos, Picking, Stock Actual, Bultos, Despachos
# ===========================================================================

# Pedidos de transferencia (ultimos 90 dias) — CD→tienda + inter-bodega
QUERY_SUPPLY_PEDIDOS_TRANSFER = """
select
    pt.cod_pedidotransferencia,
    pt.fecha_creacion,
    pt.fecha_modificacion,
    pt.fecha_envio,
    pt.fecha_recibo,
    pt.cod_estadopedidotransferencia,
    coalesce(e.nom_estadopedidotransferencia, 'Desconocido') as estado_pedido,
    pt.cod_bodega_origen,
    coalesce(bo.descripcion_sucursal, pt.cod_bodega_origen) as origen_nombre,
    coalesce(bo.canal_de_distribucion, 'DESCONOCIDO')       as canal_origen,
    pt.cod_bodega_destino,
    coalesce(bd.descripcion_sucursal, pt.cod_bodega_destino) as destino_nombre,
    coalesce(bd.canal_de_distribucion, 'DESCONOCIDO')        as canal_destino,
    pt.sku_producto,
    pt.cantidad_transferida,
    pt.cantidad_enviada,
    pt.cantidad_recibida,
    pt.cantidad_baja,
    pt.cantidad_pendiente
from db_supply.fct.ft_pedidotransferencia pt
left join db_dimensiones.dim.dt_estadopedidotransferencia e
    on pt.cod_estadopedidotransferencia = e.cod_estadopedidotransferencia
left join db_syncros.public.coo_maestro_sucursal bo
    on pt.cod_bodega_origen = bo.id_sucursal
left join db_syncros.public.coo_maestro_sucursal bd
    on pt.cod_bodega_destino = bd.id_sucursal
where pt.fecha_creacion >= dateadd('day', -90, current_date())
"""

# Picking con estados, tiempos, operario (ultimos 90 dias)
QUERY_SUPPLY_PICKING = """
select
    pk.cod_picking,
    pk.cod_pedidotransferencia,
    pk.cod_pedidoventa,
    pk.cod_estadopicking,
    coalesce(ep.nom_estadopicking, 'Desconocido') as estado_picking,
    pk.fecha_activacion,
    pk.fecha_inicio,
    pk.fecha_termino,
    pk.cod_bodega_origen,
    coalesce(bo.descripcion_sucursal, pk.cod_bodega_origen) as origen_nombre,
    pk.cod_bodega_destino,
    coalesce(bd.descripcion_sucursal, pk.cod_bodega_destino) as destino_nombre,
    pk.cod_operario,
    pk.sku_producto,
    pk.cantidad,
    pk.reservado,
    case when pk.fecha_termino is not null and pk.fecha_activacion is not null
        then datediff('minute', pk.fecha_activacion, pk.fecha_termino)
        else null end as minutos_picking
from db_supply.fct.ft_picking pk
left join db_dimensiones.dim.dt_estadopicking ep
    on pk.cod_estadopicking = ep.cod_estadopicking
left join db_syncros.public.coo_maestro_sucursal bo
    on pk.cod_bodega_origen = bo.id_sucursal
left join db_syncros.public.coo_maestro_sucursal bd
    on pk.cod_bodega_destino = bd.id_sucursal
where pk.fecha_activacion >= dateadd('day', -90, current_date())
   or pk.fecha_inicio >= dateadd('day', -90, current_date())
"""

# Stock actual por bodega/SKU (snapshot sin filtro fecha)
QUERY_SUPPLY_STOCK_ACTUAL = """
select
    sa.cod_bodega,
    coalesce(b.descripcion_sucursal, sa.cod_bodega) as bodega_nombre,
    coalesce(b.canal_de_distribucion, 'DESCONOCIDO') as canal,
    sa.sku_producto,
    sa.stock_actual,
    sa.stock_reservado,
    sa.cantidad_ordenada
from db_supply.fct.ft_stock_actual sa
left join db_syncros.public.coo_maestro_sucursal b
    on sa.cod_bodega = b.id_sucursal
where sa.stock_actual != 0 or sa.stock_reservado != 0
"""

# Bultos por pedido/picking (ultimos 90 dias)
QUERY_SUPPLY_BULTOS = """
select
    bu.cod_correlativo,
    bu.cod_pedidotransferencia,
    bu.cod_pedidoventa,
    bu.cod_picking,
    bu.tipo,
    bu.sku_producto,
    bu.cantidad,
    bu.fecha_creacion,
    bu.operador,
    bu.os,
    bu.blk
from db_supply.fct.ft_bulto bu
where bu.fecha_creacion >= dateadd('day', -90, current_date())
"""

# Despachos FedEx con tracking (ultimos 90 dias)
QUERY_SUPPLY_DESPACHOS_FEDEX = """
select
    df.tracking_number,
    df.date_time,
    df.derived_status_code,
    df.derived_status,
    df.ciudad,
    df.peso,
    df.peso_unidad,
    df.largo,
    df.ancho,
    df.alto
from db_supply.fct.ft_despacho_fedex df
where df.date_time >= dateadd('day', -90, current_date())
"""
