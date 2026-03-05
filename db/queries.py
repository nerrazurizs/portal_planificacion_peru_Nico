# ============================================================
# PERU COLUMN NORMALIZATION WRAPPERS
# These inline-view wrappers expose the same column names as
# Chile code expects, bridging the Peru table schema differences.
# ft_vcm: ID_PERIODO->fecha, COD_PRODUCTO->sku_producto, UNIDADES->cantidad, aporte computed
# vw_producto: COD_PRODUCTO->sku_producto, DESCRIPCION_PRODUCTO->nom_producto,
#              GRUPO->area, FAMILIA->sublinea, FEC_ULT_ING_CD->ultimo_ingreso_cd
# ft_compras: NUMERO_OC->po, CODIGO_PRODUCTO_OC->sku_producto, CANTIDAD_OC->cantidad_final_corregida
#             CANTIDAD_INGRESADA->cantidad_carpeta_recepcionada, MONTO_TOTAL_OC_MN->montomn
#             FECHA_INGRESO_CD->fecha_recepcion_en_cd, FECHA_EMBARQUE->etd, FECHA_ETA->eta
# ============================================================

_VCM = """(
    SELECT
        TRY_TO_DATE(CAST(id_periodo AS VARCHAR), 'YYYYMMDD') AS fecha,
        cod_producto    AS sku_producto,
        unidades        AS cantidad,
        (neto - costo)  AS aporte,
        cod_canal,
        cod_ccosto,
        cod_agencia,
        id_venta,
        id_venta_detalle,
        cod_cliente,
        cod_clientedireccion,
        cod_moneda,
        neto,
        costo,
        total,
        igv,
        descuento,
        precio_unitario,
        valor_descuento,
        flg_eliminado,
        promocion
    FROM db_finanzas.fct.ft_vcm
)"""

_PROD = """(
    SELECT
        cod_producto            AS sku_producto,
        descripcion_producto    AS nom_producto,
        grupo                   AS area,
        linea,
        familia                 AS sublinea,
        marca,
        modelo,
        mix_oficial,
        mix,
        procedencia,
        cod_proveedor,
        proveedor,
        costo                   AS ultimo_costo,
        pvp,
        precio_fob              AS costo_fob_usd,
        tipo_cambio,
        factor_importacion,
        fec_ult_ing_cd          AS ultimo_ingreso_cd,
        meses_ingreso_cd,
        rango_meses_aging,
        costo_proyectado,
        sku_proveedor,
        moderno,
        outlet,
        tradicional,
        oferta
    FROM db_dimensiones.dim.vw_producto
)"""

_COMPRAS = f"""(
    SELECT
        CAST(f.numero_oc AS VARCHAR)                                 AS po,
        f.codigo_producto_oc                                         AS sku_producto,
        f.cantidad_oc                                                AS cantidad_final_corregida,
        f.cantidad_ingresada                                         AS cantidad_carpeta_recepcionada,
        f.monto_total_oc_mn                                          AS montomn,
        f.precio_unitario_oc                                         AS purchprice,
        f.monto_total_oc                                             AS lineamount,
        TRY_TO_DATE(CAST(f.fecha_emision_oc AS VARCHAR), 'YYYYMMDD') AS fecha_entrega,
        COALESCE(
            TRY_TO_DATE(CAST(f.fecha_embarque AS VARCHAR), 'YYYY-MM-DD'),
            TRY_TO_DATE(CAST(f.fecha_embarque AS VARCHAR), 'YYYYMMDD'),
            TRY_TO_DATE(CAST(f.fecha_emision_oc AS VARCHAR), 'YYYYMMDD')
        )                                                            AS etd,
        COALESCE(
            TRY_TO_DATE(CAST(f.fecha_eta AS VARCHAR), 'YYYY-MM-DD'),
            TRY_TO_DATE(CAST(f.fecha_eta AS VARCHAR), 'YYYYMMDD'),
            TRY_TO_DATE(CAST(f.fecha_eta AS VARCHAR), 'DD/MM/YYYY'),
            DATEADD(day, 47, COALESCE(
                TRY_TO_DATE(CAST(f.fecha_embarque AS VARCHAR), 'YYYY-MM-DD'),
                TRY_TO_DATE(CAST(f.fecha_embarque AS VARCHAR), 'YYYYMMDD'),
                TRY_TO_DATE(CAST(f.fecha_emision_oc AS VARCHAR), 'YYYYMMDD')
            ))
        )                                                            AS eta,
        COALESCE(
            TRY_TO_DATE(CAST(f.fecha_ingreso_cd AS VARCHAR), 'YYYY-MM-DD'),
            TRY_TO_DATE(CAST(f.fecha_ingreso_cd AS VARCHAR), 'YYYYMMDD')
        )                                                            AS fecha_recepcion_en_cd,
        CAST(f.tipocambio AS FLOAT)                                  AS paridad_moneda,
        CAST(f.tipocambio AS FLOAT)                                  AS dolar_sistema,
        CAST(f.moneda AS VARCHAR)                                    AS cod_moneda,
        CAST(f.codigo_proveedor_oc AS VARCHAR)                       AS cod_proveedor,
        f.situacion                                                  AS nom_status,
        f.estatus,
        p.proveedor                                                  AS nom_proveedor,
        p.nom_producto                                               AS nom_producto,
        NULL::VARCHAR                                                AS carpeta_comex,
        NULL::VARCHAR                                                AS factura,
        NULL::VARCHAR                                                AS nom_estadoaprobacion,
        CASE WHEN f.fecha_ingreso_cd IS NULL
             THEN 'Si' ELSE 'No'
        END                                                          AS entransito,
        NULL::BOOLEAN                                                AS tiene_bl,
        NULL::VARCHAR                                                AS tiene_carpeta_comex,
        NULL::VARCHAR                                                AS status_booking,
        NULL::VARCHAR                                                AS forma_pago,
        p.area,
        p.linea,
        p.sublinea,
        p.marca,
        p.modelo,
        p.factor_importacion,
        p.mix_oficial,
        p.procedencia,
        f.codigo_sucursal,
        f.procedencia_oc,
        f.almacen_ingreso_cd
    FROM db_supply.fct.ft_compras f
    LEFT JOIN {_PROD} p ON f.codigo_producto_oc = p.sku_producto
)"""

# ============================================================
# INSTOCK WRAPPERS — ht_in_stock y ht_in_stock_cd de Peru no tienen
# todas las columnas CANTIDAD_PROM_* que Chile. Se exponen como NULL
# para compatibilidad con el codigo Python existente.
#
# Columnas confirmadas como EXISTENTES en Peru:
#   ht_in_stock    : fecha, sku_producto, cod_bodega, stock_unidades,
#                    stock_costo, min_exhibicion, perfil, ultimo_costo,
#                    mix_oficial, cantidad_prom_90, cantidad_prom_180
#   ht_in_stock_cd : fecha, sku_producto, stock_unidades, stock_costo,
#                    cantidad_prom_90_cia, cantidad_prom_180_cia, costo_prom_90_cia
#
# Columnas FALTANTES (NULL en wrapper):
#   ht_in_stock    : cantidad_prom_365, cantidad_prom_*b, in_stock_presentacion
#   ht_in_stock_cd : cantidad_prom_365_cia
# ============================================================

_INSTOCK = """(
    SELECT
        fecha,
        sku_producto,
        cod_bodega,
        stock_unidades,
        stock_costo,
        min_exhibicion,
        perfil,
        ultimo_costo,
        mix_oficial,
        cantidad_prom_90,
        cantidad_prom_180,
        NULL::FLOAT  AS cantidad_prom_365,
        NULL::FLOAT  AS cantidad_prom_90b,
        NULL::FLOAT  AS cantidad_prom_180b,
        NULL::FLOAT  AS cantidad_prom_365b,
        NULL::FLOAT  AS in_stock_presentacion
    FROM db_supply.hst.ht_in_stock
)"""

_INSTOCK_CD = """(
    SELECT
        fecha,
        sku_producto,
        stock_unidades,
        stock_costo,
        cantidad_prom_90_cia,
        cantidad_prom_180_cia,
        NULL::FLOAT  AS cantidad_prom_365_cia,
        costo_prom_90_cia
    FROM db_supply.hst.ht_in_stock_cd
)"""

# Stock Base (parametrized - uses %s placeholders for fecha_inicio and fecha_fin)
QUERY_STOCK_BASE = f"""
select
  a.fecha,
  a.sku_producto,
  a.cod_bodega,
  coalesce(b.id_sucursal, a.cod_bodega) as id_sucursal,
  coalesce(b.descripcion_sucursal, 'Sin descripcion') as descripcion_sucursal,
  COALESCE(b.canal_de_distribucion,
    CASE TRIM(cc.cod_canal)
      WHEN '03' THEN 'TIENDA'
      WHEN '02' THEN 'MAYORISTA'
      WHEN '06' THEN 'ETAIL'
    END,
    'TIENDA'
  ) as canal_de_distribucion,
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
from db_supply.hst.ht_in_stock a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_bodega = b.id_sucursal
left join db_dimensiones.dim.dt_ccosto cc
  on TRIM(a.cod_bodega) = TRIM(cc.cod_ccosto)
left join {_PROD} c
  on a.sku_producto = c.sku_producto
left join db_supply.hst.ht_in_stock_cd d
  on a.fecha = d.fecha
 and a.sku_producto = d.sku_producto
where a.fecha >= %s and a.fecha <= %s
group by 1,2,3,4,5,6,7,8,9,10,11,12,13
"""

QUERY_MAESTRA = f"""
select p.*
from {_PROD} p
"""

# -- Dashboard ejecutivo (queries livianas) --

QUERY_DASHBOARD_VENTAS_MTD = f"""
select
    a.cod_canal,
    sum(a.cantidad) as cantidad_mtd,
    sum(a.neto)     as neto_mtd
from {_VCM} a
where a.cantidad > 0
  and a.fecha >= date_trunc('month', current_date())
  and a.fecha <  current_date()
group by 1
"""

QUERY_DASHBOARD_COMEX = f"""
select
    count(distinct c.po) as n_pos,
    sum(c.cantidad_final_corregida) as und_transito,
    sum(c.montomn) as costo_transito
from {_COMPRAS} c
where c.fecha_recepcion_en_cd is null
  and c.cantidad_final_corregida > 0
"""

QUERY_COMEX_BASE = f"""
SELECT *
FROM {_COMPRAS} c
WHERE 1=1
"""

# POs atrasadas: ETA ya paso y aun no han llegado al CD
QUERY_COMEX_ATRASADAS = f"""
SELECT
    c.po,
    c.sku_producto,
    c.nom_producto,
    c.nom_proveedor,
    c.carpeta_comex,
    c.fecha_entrega,
    c.etd,
    c.eta,
    c.eta AS ETA_CALC,
    GREATEST(0, COALESCE(c.cantidad_final_corregida, 0)
                - COALESCE(c.cantidad_carpeta_recepcionada, 0)) AS QTY_PENDIENTE,
    c.cantidad_final_corregida,
    c.montomn,
    c.area,
    c.linea,
    c.sublinea,
    c.marca,
    DATEDIFF('day',
        DATEADD(day, 10, c.eta),
        CURRENT_DATE()
    ) AS DIAS_ATRASO
FROM {_COMPRAS} c
WHERE c.fecha_recepcion_en_cd IS NULL
  AND c.cantidad_final_corregida > 0
  AND DATEADD(day, 10, c.eta) < CURRENT_DATE()
ORDER BY c.eta ASC
"""

# POs sin carpeta comex creada
QUERY_COMEX_SIN_CARPETA = f"""
SELECT
    c.po,
    c.sku_producto,
    c.nom_producto,
    c.nom_proveedor,
    c.fecha_entrega,
    c.cantidad_final_corregida,
    c.montomn,
    c.area,
    c.linea,
    DATEDIFF('day', c.fecha_entrega, CURRENT_DATE()) AS DIAS_DESDE_ENTREGA
FROM {_COMPRAS} c
WHERE c.fecha_recepcion_en_cd IS NULL
  AND c.cantidad_final_corregida > 0
ORDER BY c.fecha_entrega ASC
"""

# POs en transito sin factura
QUERY_COMEX_SIN_FACTURA = f"""
SELECT
    c.po,
    c.sku_producto,
    c.nom_producto,
    c.nom_proveedor,
    c.carpeta_comex,
    c.etd   AS ETD_CALC,
    c.eta   AS ETA_CALC,
    GREATEST(0, COALESCE(c.cantidad_final_corregida, 0)
                - COALESCE(c.cantidad_carpeta_recepcionada, 0)) AS QTY_PENDIENTE,
    c.cantidad_final_corregida,
    c.montomn,
    c.area,
    c.linea,
    c.sublinea,
    c.marca
FROM {_COMPRAS} c
WHERE c.fecha_recepcion_en_cd IS NULL
  AND c.cantidad_final_corregida > 0
  AND c.etd <= CURRENT_DATE()
  AND c.factura IS NULL
ORDER BY c.eta ASC
"""

# POs proximas a llegar (ETA within next 45 days)
QUERY_COMEX_PROXIMAS = f"""
SELECT
    c.po,
    c.sku_producto,
    c.nom_producto,
    c.nom_proveedor,
    c.carpeta_comex,
    c.etd   AS ETD_CALC,
    c.eta   AS ETA_CALC,
    GREATEST(0, COALESCE(c.cantidad_final_corregida, 0)
                - COALESCE(c.cantidad_carpeta_recepcionada, 0)) AS QTY_PENDIENTE,
    CASE
        WHEN COALESCE(c.cantidad_final_corregida, 0) > 0
        THEN GREATEST(0, COALESCE(c.cantidad_final_corregida, 0)
                         - COALESCE(c.cantidad_carpeta_recepcionada, 0))
             / c.cantidad_final_corregida
             * COALESCE(c.montomn, 0)
        ELSE 0
    END AS MONTO_PENDIENTE_CLP,
    c.cantidad_final_corregida,
    c.montomn,
    c.area,
    c.linea,
    c.sublinea,
    c.marca,
    DATEDIFF('day', CURRENT_DATE(), c.eta) AS DIAS_HASTA_ETA
FROM {_COMPRAS} c
WHERE c.fecha_recepcion_en_cd IS NULL
  AND c.cantidad_final_corregida > 0
  AND c.eta BETWEEN CURRENT_DATE() AND DATEADD(day, 45, CURRENT_DATE())
ORDER BY c.eta ASC
"""

# Proxima ETA pendiente por SKU (para VP insights)
QUERY_ETA_PENDIENTE_POR_SKU = f"""
SELECT
    TRIM(c.sku_producto) AS sku_producto,
    MIN(c.eta)  AS proxima_eta,
    SUM(GREATEST(0, COALESCE(c.cantidad_final_corregida, 0)
        - COALESCE(c.cantidad_carpeta_recepcionada, 0))) AS qty_pendiente,
    COUNT(DISTINCT c.po) AS n_pos,
    MAX(c.nom_proveedor) AS proveedor_eta
FROM {_COMPRAS} c
WHERE c.fecha_recepcion_en_cd IS NULL
  AND c.cantidad_final_corregida > 0
  AND c.eta >= DATEADD('month', -3, CURRENT_DATE())
GROUP BY TRIM(c.sku_producto)
"""

# Precio promedio de venta por SKU (ultimos 90 dias)
QUERY_PRECIO_PROM_SKU = f"""
SELECT
    a.sku_producto,
    SUM(a.neto) / NULLIF(SUM(a.cantidad), 0) AS PRECIO_PROM_90D,
    SUM(a.cantidad) AS UNIDADES_90D,
    SUM(a.neto) AS NETO_90D
FROM {_VCM} a
WHERE a.fecha >= DATEADD('day', -90, CURRENT_DATE())
  AND a.cantidad > 0
GROUP BY 1
"""

# Canasta de Productos
QUERY_CANASTA = f"""
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
FROM {_VCM} a
LEFT JOIN {_PROD} c
    ON a.sku_producto = c.sku_producto
WHERE a.cantidad > 0
  AND a.fecha >= %s
  AND a.fecha <= %s
"""

# Pasillo Infinito (tabla puede no existir en Peru)
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

QUERY_DT_TIENDA = """
SELECT
    b.id_sucursal,
    b.descripcion_sucursal,
    b.canal_de_distribucion,
    0::FLOAT             AS latitud,
    0::FLOAT             AS longitud,
    'Sin Cluster'        AS cluster,
    'Sin Supervisor'     AS supervisor,
    'Sin Grupo'          AS grupo,
    'Sin Tipo'           AS tipo,
    0::FLOAT             AS mts2,
    ''                   AS direccion,
    'Sin Distrito'       AS distrito,
    'Sin Ciudad'         AS ciudad,
    'Sin Comuna'         AS comuna,
    'Sin Zona'           AS zona,
    'Sin Operador'       AS operador,
    TRUE                 AS activa
FROM db_syncros.public.coo_maestro_sucursal b
"""

# Perfil resumen
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
QUERY_SYNCRO_LEADTIMES = f"""
SELECT
    t.*,
    p.nom_producto,
    p.marca,
    p.linea,
    p.sublinea,
    p.area
FROM db_syncros.PUBLIC.coo_rel_proveedor_sku t
LEFT JOIN {_PROD} p
    ON t.ID_MATERIAL = p.sku_producto
"""

# Ventas MTD
QUERY_VENTAS_MTD = f"""
select
    a.cod_canal,
    a.sku_producto,
    sum(a.cantidad) as cantidad_mtd,
    sum(a.neto)     as neto_mtd,
    sum(a.neto) / nullif(sum(a.cantidad), 0) as precio_prom_mtd
from {_VCM} a
where a.cantidad > 0
  and a.fecha >= date_trunc('month', current_date())
  and a.fecha <  current_date()
group by 1,2
"""

# Ventas MTD diarias
QUERY_VENTAS_MTD_DIARIA = f"""
select
    a.fecha,
    a.cod_canal,
    a.sku_producto,
    sum(a.cantidad) as cantidad,
    sum(a.neto)     as neto
from {_VCM} a
where a.cantidad > 0
  and a.fecha >= date_trunc('month', current_date())
  and a.fecha <  current_date()
group by 1, 2, 3
"""

# Ventas Mes Anterior Completo
QUERY_VENTAS_MES_ANTERIOR = f"""
select
    a.cod_canal,
    a.sku_producto,
    sum(a.cantidad) as cantidad_mes,
    sum(a.neto)     as neto_mes,
    sum(case when a.cantidad > 0 then a.neto else 0 end)
      / nullif(sum(case when a.cantidad > 0 then a.cantidad else 0 end), 0) as precio_prom
from {_VCM} a
where a.fecha >= date_trunc('month', dateadd('month', -1, current_date()))
  and a.fecha <  date_trunc('month', current_date())
group by 1,2
"""

# Ventas Ano Anterior completo
QUERY_VENTAS_AA = f"""
select
    a.cod_canal,
    a.sku_producto,
    date_trunc('month', a.fecha) as periodo,
    sum(a.cantidad) as cantidad_aa,
    sum(a.neto)     as neto_aa,
    sum(a.aporte)   as aporte_aa
from {_VCM} a
where a.fecha >= date_trunc('year', dateadd('year', -1, current_date()))
  and a.fecha <  date_trunc('year', current_date())
group by 1, 2, 3
"""

# Ventas Historicas (13 meses)
# NOTE Peru: join on cod_ccosto = id_sucursal (Chile used CUSTOM 1)
QUERY_VENTAS_HISTORICAS = f"""
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
from {_VCM} a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_ccosto = b.id_sucursal
where a.fecha >= date_trunc('year', dateadd('year', -1, current_date()))
  and a.fecha < date_trunc('month', current_date())
group by 1,2,3,4
"""

# Ventas YTD
QUERY_VENTAS_YTD = f"""
select
  b.canal_de_distribucion,
  sum(a.cantidad)                                   as UNIDADES_VENDIDAS,
  sum(a.neto)                                       as NETO_TOTAL,
  sum(a.aporte)                                     as APORTE_TOTAL,
  sum(a.aporte) / nullif(sum(a.neto), 0)            as MARGEN
from {_VCM} a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_ccosto = b.id_sucursal
where a.fecha >= date_trunc('year', current_date())
  and a.fecha <= current_date()
group by 1
"""

# Ventas YTD Ano Anterior
QUERY_VENTAS_YTD_AA = f"""
select
  sum(a.neto)    as NETO_AA,
  sum(a.aporte)  as APORTE_AA
from {_VCM} a
where a.fecha >= dateadd('year', -1, date_trunc('year', current_date()))
  and a.fecha <= dateadd('year', -1, current_date())
"""

# Stock Proyeccion
QUERY_STOCK_PROYECCION = """
select
  a.fecha,
  a.sku_producto,
  case
    when COALESCE(b.canal_de_distribucion,
         CASE TRIM(d.cod_canal)
           WHEN '03' THEN 'TIENDA'
           WHEN '02' THEN 'MAYORISTA'
           WHEN '06' THEN 'ETAIL'
         END
    ) = 'CD' then 'CD'
    else 'TIENDA'
  end as canal_std,
  SUM(a.stock_unidades) as stock_unidades,
  sum(a.min_exhibicion) as perfil_tiendas
from db_supply.hst.ht_in_stock a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_bodega = b.id_sucursal
left join db_dimensiones.dim.dt_ccosto d
  on TRIM(a.cod_bodega) = TRIM(d.cod_ccosto)
where a.fecha = (
    select max(fecha)
    from db_supply.hst.ht_in_stock
    where fecha < current_date()
)
group by 1,2,3
"""

# Stock Critico - Metrics
QUERY_STOCK_CRITICO_METRICS = f"""
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
from db_supply.hst.ht_in_stock a
left join {_PROD} c
  on a.sku_producto = c.sku_producto
left join db_supply.hst.ht_in_stock_cd d
  on a.fecha = d.fecha
 and a.sku_producto = d.sku_producto
where a.fecha >= '2025-01-01'
  and (dayname(a.fecha) = 'Mon' or a.fecha = (select max(fecha) from db_supply.hst.ht_in_stock))
group by 1,2,3,4,5,6,7,8,9,10
"""

# Stock Critico - Detail
# NOTE Peru: dt_almacen replaces dt_bodega
QUERY_STOCK_CRITICO_DETAIL = f"""
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
  coalesce(b.descripcion_sucursal, bo.nom_almacen, 'Bodega ' || a.cod_bodega) as descripcion_sucursal,
  coalesce(b.canal_de_distribucion, 'Sin Canal')                           as canal_de_distribucion,
  coalesce(cast(b.cd as varchar), 'N')                                     as cd,
  'Sin Supervisor' as supervisor,
  'Sin Cluster'    as cluster,
  0                as mts2,
  sum(a.stock_costo) as stock_costo,
  sum(a.stock_unidades) as stock_unidades
from db_supply.hst.ht_in_stock a
left join (
    select id_sucursal,
           max(descripcion_sucursal) as descripcion_sucursal,
           max(canal_de_distribucion) as canal_de_distribucion,
           max(cast(cd as varchar)) as cd
    from db_syncros.public.coo_maestro_sucursal
    group by id_sucursal
  ) b on a.cod_bodega = b.id_sucursal
left join (
    select p.sku_producto,
           max(p.nom_producto) as nom_producto,
           max(p.area) as area,
           max(p.linea) as linea,
           max(p.sublinea) as sublinea,
           max(p.marca) as marca
    from {_PROD} p
    group by p.sku_producto
  ) c on a.sku_producto = c.sku_producto
left join (
    select cod_almacen, max(nom_almacen) as nom_almacen
    from db_dimensiones.dim.dt_almacen
    group by cod_almacen
  ) bo on a.cod_bodega = bo.cod_almacen
where a.fecha = (select max(fecha) from db_supply.hst.ht_in_stock)
group by 1,2,3,4,5,6,7,8,9,10,11,12
"""

# Stock Critico - Sales
QUERY_STOCK_CRITICO_SALES = f"""
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
from {_VCM} a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_ccosto = b.id_sucursal
where a.fecha >= dateadd('month', -12, date_trunc('month', current_date()))
group by 1,2,3,4
"""

# COGS mensual a nivel compania (ultimos 6 meses completos)
QUERY_COGS_COMPANY_6M = f"""
select
    date_trunc('month', a.fecha) as PERIODO,
    sum(a.neto) - sum(a.aporte) as COGS_MENSUAL
from {_VCM} a
where a.cantidad > 0
  and a.fecha >= dateadd('month', -6, date_trunc('month', current_date()))
  and a.fecha <  date_trunc('month', current_date())
group by 1
order by 1
"""

# Stock en transito clasificado para KPIs
QUERY_TRANSIT_STOCK_KPI = f"""
select
    case
        when c.etd <= current_date() and c.eta > current_date() then 'EN_AGUA'
        when c.etd > current_date() then 'PENDIENTE_ZARPE'
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
from {_COMPRAS} c
where c.fecha_recepcion_en_cd is null
  and coalesce(c.cantidad_final_corregida, 0) > 0
group by 1, 2, 3, 4, 5, 6, 7, 8
"""

# Forecast Mirror queries
QUERY_MIRROR_QUALITY = f"""
SELECT
    a.sku_producto as espejo,
    YEAR(a.fecha) as anio,
    MONTH(a.fecha) as mes,
    COUNT(DISTINCT a.fecha) as dias_con_venta
FROM {_VCM} a
WHERE a.sku_producto IN ({{placeholders}})
  AND a.fecha BETWEEN %s AND %s
  AND a.cantidad > 0
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""

QUERY_MIRROR_HIERARCHY = f"""
SELECT p.sku_producto as espejo, p.linea, p.sublinea, p.area
FROM {_PROD} p
WHERE p.sku_producto IN ({{placeholders}})
"""

QUERY_MIRROR_HIST_SKU = f"""
SELECT
    a.sku_producto as espejo,
    b.id_sucursal,
    a.fecha,
    SUM(a.cantidad) as venta_qty
FROM {_VCM} a
LEFT JOIN db_syncros.public.coo_maestro_sucursal b ON a.cod_ccosto = b.id_sucursal
WHERE a.sku_producto IN ({{placeholders}})
  AND a.fecha BETWEEN %s AND %s
  AND b.canal_de_distribucion = 'TIENDA'
  AND a.cantidad > 0
GROUP BY 1, 2, 3
"""

QUERY_MIRROR_SUCURSAL = """
SELECT id_sucursal, descripcion_sucursal, canal_de_distribucion as canal
FROM db_syncros.public.coo_maestro_sucursal
"""

# Mirror monthly sales by sucursal + canal
QUERY_MIRROR_HIST_MONTHLY = f"""
SELECT
    a.sku_producto as espejo,
    b.id_sucursal,
    b.descripcion_sucursal,
    b.canal_de_distribucion as canal,
    DATE_TRUNC('month', a.fecha) as periodo,
    SUM(a.cantidad) as venta_qty
FROM {_VCM} a
LEFT JOIN db_syncros.public.coo_maestro_sucursal b
    ON a.cod_ccosto = b.id_sucursal
WHERE a.sku_producto = %s
  AND a.fecha BETWEEN %s AND %s
  AND a.cantidad > 0
  AND b.id_sucursal IS NOT NULL
GROUP BY 1, 2, 3, 4, 5
ORDER BY 5, 2
"""

# Comex Full (used by proyeccion module)
QUERY_COMEX_FULL = f"""
select *
from {_COMPRAS}
"""

# Plan de Compras
QUERY_PLAN_COMPRAS = f"""
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
    c.etd                                                   as ETD_CALC,
    c.eta                                                   as ETA_CALC,
    c.fecha_recepcion_en_cd,
    c.cantidad_final_corregida                              as QTY_ORDENADA,
    c.cantidad_carpeta_recepcionada                         as QTY_RECEPCIONADA,
    greatest(0, coalesce(c.cantidad_final_corregida, 0)
                - coalesce(c.cantidad_carpeta_recepcionada, 0))
                                                            as QTY_PENDIENTE,
    c.purchprice                                            as PRECIO_UNITARIO,
    c.lineamount                                            as MONTO_MONEDA_ORIG,
    c.montomn                                               as MONTO_CLP,
    c.paridad_moneda                                        as TC_PO,
    c.dolar_sistema                                         as TC_SISTEMA,
    c.factor_importacion,
    case
        when coalesce(c.cantidad_final_corregida, 0) > 0
        then greatest(0, coalesce(c.cantidad_final_corregida, 0)
                         - coalesce(c.cantidad_carpeta_recepcionada, 0))
             / c.cantidad_final_corregida
             * coalesce(c.montomn, 0)
        else 0
    end                                                     as MONTO_PENDIENTE_CLP,
    date_trunc('month', c.eta)                              as PERIODO_ETA,
    date_trunc('month', c.fecha_recepcion_en_cd)            as PERIODO_RECEPCION,
    YEAR(c.eta)                                             as ano_eta,
    MONTH(c.eta)                                            as mes_eta,
    CAST(YEAR(c.eta) AS VARCHAR) || LPAD(CAST(MONTH(c.eta) AS VARCHAR), 2, '0') as anomes_eta
from {_COMPRAS} c
where coalesce(c.cantidad_final_corregida, 0) > 0
"""

# Venta a costo mensual proyectada para plan de compras
QUERY_VENTA_COSTO_PROYECTADA = f"""
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
    from {_VCM} a
    where a.fecha >= dateadd('month', -6, date_trunc('month', current_date()))
      and a.fecha <  date_trunc('month', current_date())
      and a.cantidad > 0
    group by 1, 2
) ventas_sku
left join {_PROD} c
    on ventas_sku.sku_producto = c.sku_producto
where c.area is not null and c.linea is not null
group by c.area, c.linea, 3
"""

# Venta a costo historica
QUERY_VENTA_COSTO_HIST = f"""
select
    c.area,
    c.linea,
    date_trunc('month', a.fecha)        as PERIODO,
    sum(a.aporte)                       as VENTA_COSTO_CLP
from {_VCM} a
left join {_PROD} c
    on a.sku_producto = c.sku_producto
where a.fecha >= dateadd('month', -6, date_trunc('month', current_date()))
  and a.fecha <  date_trunc('month', current_date())
  and a.cantidad > 0
  and c.area is not null
  and c.linea is not null
group by 1, 2, 3
"""

# Stock on hand actual por SKU
QUERY_STOCK_ONHAND = """
select
    sub.sku_producto,
    sum(case when sub.canal_std = 'CD' then sub.stock_unidades else 0 end) as STOCK_CD,
    sum(case when sub.canal_std = 'TIENDA' then sub.stock_unidades else 0 end) as STOCK_TIENDA,
    sum(sub.stock_unidades) as STOCK_TOTAL,
    sum(sub.stock_costo) as STOCK_COSTO_TOTAL
from (
    select
        a.sku_producto,
        a.stock_unidades,
        a.stock_costo,
        case
            when COALESCE(b.canal_de_distribucion,
                 CASE TRIM(d.cod_canal)
                   WHEN '03' THEN 'TIENDA'
                   WHEN '02' THEN 'MAYORISTA'
                   WHEN '06' THEN 'ETAIL'
                 END
            ) = 'CD' then 'CD'
            else 'TIENDA'
        end as canal_std
    from db_supply.hst.ht_in_stock a
    left join db_syncros.public.coo_maestro_sucursal b
        on a.cod_bodega = b.id_sucursal
    left join db_dimensiones.dim.dt_ccosto d
        on TRIM(a.cod_bodega) = TRIM(d.cod_ccosto)
    where a.fecha = (
        select max(fecha)
        from db_supply.hst.ht_in_stock
        where fecha < current_date()
    )
) sub
group by 1
"""

# Ventas diarias ultimos 90 dias
QUERY_VENTAS_DIARIAS_90D = f"""
select
    a.sku_producto,
    a.fecha,
    b.canal_de_distribucion,
    sum(a.cantidad) as unidades,
    sum(a.neto)     as neto
from {_VCM} a
left join db_syncros.public.coo_maestro_sucursal b
  on a.cod_ccosto = b.id_sucursal
where a.fecha >= dateadd('day', -90, current_date())
  and a.cantidad > 0
group by 1, 2, 3
"""

# Ventas mensuales por SKU x Canal (elasticidad)
QUERY_VENTAS_MENSUAL_PRECIO = f"""
select
    a.sku_producto,
    a.cod_canal,
    date_trunc('month', a.fecha) as periodo,
    sum(a.cantidad) as cantidad,
    sum(a.neto)     as neto,
    sum(a.aporte)   as aporte,
    sum(case when a.cantidad > 0 then a.neto else 0 end)
      / nullif(sum(case when a.cantidad > 0 then a.cantidad else 0 end), 0) as precio_promedio
from {_VCM} a
where a.fecha >= dateadd('month', -24, date_trunc('month', current_date()))
  and a.fecha <  date_trunc('month', current_date())
group by 1, 2, 3
having sum(a.cantidad) > 0
"""

# Ventas semanales ultimo ano (abc_xyz)
QUERY_VENTAS_SEMANALES = f"""
select
    a.sku_producto,
    date_trunc('week', a.fecha) as semana,
    sum(a.cantidad) as unidades,
    sum(a.aporte)   as aporte
from {_VCM} a
where a.fecha >= dateadd('year', -1, current_date())
  and a.cantidad > 0
group by 1, 2
"""

# InStock Store Detail snapshot
QUERY_INSTOCK_STORE_DETAIL = """
select
    a.sku_producto,
    b.id_sucursal,
    b.canal_de_distribucion,
    a.perfil,
    a.stock_unidades,
    coalesce(a.cantidad_prom_90, 0) as cantidad_prom_90
from db_supply.hst.ht_in_stock a
join db_syncros.public.coo_maestro_sucursal b
    on a.cod_bodega = b.id_sucursal
where b.canal_de_distribucion = 'TIENDA'
  and a.fecha = (
      select max(fecha)
      from db_supply.hst.ht_in_stock
      where fecha < current_date()
  )
  and a.perfil = 'SI'
"""

# Stock por SKU desglosado CD vs TIENDA
QUERY_STOCK_HIGIENE = """
select
    a.sku_producto,
    b.id_sucursal,
    b.descripcion_sucursal,
    b.canal_de_distribucion,
    sum(a.stock_unidades)  as stock_unidades,
    sum(a.min_exhibicion)  as perfil_tiendas
from db_supply.hst.ht_in_stock a
left join db_syncros.public.coo_maestro_sucursal b
    on a.cod_bodega = b.id_sucursal
where a.fecha = (
    select max(fecha)
    from db_supply.hst.ht_in_stock
    where fecha < current_date()
)
group by 1, 2, 3, 4
having sum(a.stock_unidades) > 0 or sum(a.min_exhibicion) > 0
"""

# Forecast anual por SKU x Canal
QUERY_FORECAST_ANUAL = f"""
select
    a.cod_canal,
    a.sku_producto,
    date_trunc('month', a.fecha) as periodo,
    sum(a.cantidad) as forecast_qty
from {_VCM} a
where a.fecha >= date_trunc('year', current_date())
  and a.fecha <  dateadd('year', 1, date_trunc('year', current_date()))
  and a.cantidad > 0
group by 1, 2, 3
"""

# Ventas semanales por SKU (24 semanas, tendencia)
QUERY_VENTAS_SEMANAL_TENDENCIA = f"""
select
    a.sku_producto,
    date_trunc('week', a.fecha) as semana,
    sum(a.cantidad) as unidades,
    sum(a.neto)     as neto
from {_VCM} a
where a.fecha >= dateadd('week', -24, current_date())
  and a.cantidad > 0
group by 1, 2
"""

# ---------------------------------------------------------------------------
# InStock Historico — tienda level
# ---------------------------------------------------------------------------
QUERY_INSTOCK_HIST_TIENDA = f"""
select
    a.fecha,
    a.sku_producto,
    c.area,
    c.linea,
    c.sublinea,
    c.marca,
    c.mix_oficial,
    count(*)                                                as n_tiendas,
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
    sum(case when a.perfil = 'SI' then 1 else 0 end)        as n_tiendas_is90_perfil,
    sum(case when a.perfil = 'SI' then 1 else 0 end)        as n_tiendas_is180_perfil,
    sum(case when a.perfil = 'SI' then 1 else 0 end)        as n_tiendas_is365_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_90, 0)
             then 1 else 0 end)                             as tiendas_is90_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_180, 0)
             then 1 else 0 end)                             as tiendas_is180_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_365, 0)
             then 1 else 0 end)                             as tiendas_is365_perfil,
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_90_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_90,
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_180_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_180,
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_365_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_365
from {_INSTOCK} a
join db_syncros.public.coo_maestro_sucursal b
    on a.cod_bodega = b.id_sucursal
left join {_PROD} c
    on a.sku_producto = c.sku_producto
left join {_INSTOCK_CD} d
    on a.fecha = d.fecha
   and a.sku_producto = d.sku_producto
where b.canal_de_distribucion = 'TIENDA'
  and a.fecha >= '2023-01-01'
  and (dayname(a.fecha) = 'Mon'
       or a.fecha = (select max(fecha) from db_supply.hst.ht_in_stock))
group by 1, 2, 3, 4, 5, 6, 7
"""

# InStock Historico — CD level
QUERY_INSTOCK_HIST_CD = f"""
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
from {_INSTOCK_CD} a
left join {_PROD} c
    on a.sku_producto = c.sku_producto
where a.fecha >= '2023-01-01'
  and (dayname(a.fecha) = 'Mon'
       or a.fecha = (select max(fecha) from db_supply.hst.ht_in_stock_cd))
"""

# InStock diario (ultimos 30 dias — tienda)
QUERY_INSTOCK_DAILY_TIENDA = f"""
select
    a.fecha,
    a.sku_producto,
    c.area,
    c.linea,
    c.sublinea,
    c.marca,
    c.mix_oficial,
    count(*)                                                as n_tiendas,
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
    sum(case when a.perfil = 'SI' then 1 else 0 end)        as n_tiendas_is90_perfil,
    sum(case when a.perfil = 'SI' then 1 else 0 end)        as n_tiendas_is180_perfil,
    sum(case when a.perfil = 'SI' then 1 else 0 end)        as n_tiendas_is365_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_90, 0)
             then 1 else 0 end)                             as tiendas_is90_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_180, 0)
             then 1 else 0 end)                             as tiendas_is180_perfil,
    sum(case when a.perfil = 'SI' and a.stock_unidades > 0
             and a.stock_unidades >= coalesce(a.cantidad_prom_365, 0)
             then 1 else 0 end)                             as tiendas_is365_perfil,
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_90_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_90,
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_180_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_180,
    coalesce(max(case when d.stock_unidades > 0
                      and d.stock_unidades >= coalesce(d.cantidad_prom_365_cia, 0)
                      then 1 else 0 end), 0)                as instock_cd_365
from {_INSTOCK} a
join db_syncros.public.coo_maestro_sucursal b
    on a.cod_bodega = b.id_sucursal
left join {_PROD} c
    on a.sku_producto = c.sku_producto
left join {_INSTOCK_CD} d
    on a.fecha = d.fecha
   and a.sku_producto = d.sku_producto
where b.canal_de_distribucion = 'TIENDA'
  and a.fecha >= dateadd('day', -30, current_date())
group by 1, 2, 3, 4, 5, 6, 7
"""

QUERY_INSTOCK_DAILY_CD = f"""
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
from {_INSTOCK_CD} a
left join {_PROD} c
    on a.sku_producto = c.sku_producto
where a.fecha >= dateadd('day', -30, current_date())
"""

# Ventas diarias por SKU (ultimo ano, patrones)
QUERY_VENTAS_DIARIAS_PATRON = f"""
select
    a.sku_producto,
    a.fecha,
    dayofweek(a.fecha) as dia_semana,
    day(a.fecha)       as dia_mes,
    sum(a.cantidad) as unidades,
    sum(a.neto)     as neto
from {_VCM} a
where a.fecha >= dateadd('year', -1, current_date())
  and a.cantidad > 0
group by 1, 2, 3, 4
"""

# Pesos DOW + WOM (desagregacion)
QUERY_PESOS_DIARIOS = f"""
with base as (
    select
        a.fecha,
        dayname(a.fecha)                as dow_name,
        ceil(day(a.fecha) / 7.0)::int   as wom,
        sum(a.cantidad)                 as unidades
    from {_VCM} a
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

# Pesos DOW x WOM x CANAL
QUERY_PESOS_DIARIOS_CANAL = f"""
with base as (
    select
        a.fecha,
        dayname(a.fecha)                as dow_name,
        ceil(day(a.fecha) / 7.0)::int   as wom,
        b.canal_de_distribucion         as canal,
        sum(a.cantidad)                 as unidades
    from {_VCM} a
    left join db_syncros.public.coo_maestro_sucursal b
        on a.cod_ccosto = b.id_sucursal
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

# Event Boosts por SUBLINEA x CANAL x EVENTO
QUERY_EVENT_BOOSTS = f"""
with ventas as (
    select
        a.fecha,
        p.sublinea,
        b.canal_de_distribucion  as canal,
        sum(a.cantidad)          as unidades
    from {_VCM} a
    left join db_syncros.public.coo_maestro_sucursal b
        on a.cod_ccosto = b.id_sucursal
    left join {_PROD} p
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

# Event Boosts por SKU x CANAL x EVENTO
QUERY_EVENT_BOOSTS_SKU = f"""
with ventas as (
    select
        a.sku_producto,
        a.fecha,
        b.canal_de_distribucion  as canal,
        sum(a.cantidad)          as unidades
    from {_VCM} a
    left join db_syncros.public.coo_maestro_sucursal b
        on a.cod_ccosto = b.id_sucursal
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

# Redistribucion de Stock — tabla Peru (puede diferir de Chile)
QUERY_TRANSITO_ENTRE_SUCURSALES = """
select *
from db_syncros.public.coo_inventario_transito_entre_sucursales
"""

# Ventas ultimos 90 dias por SKU x Sucursal
QUERY_VENTAS_90D_SUCURSAL = f"""
select
    a.sku_producto,
    b.id_sucursal,
    b.descripcion_sucursal,
    b.canal_de_distribucion,
    sum(a.cantidad)         as unidades_90d,
    sum(a.neto)             as neto_90d,
    count(distinct a.fecha) as dias_con_venta
from {_VCM} a
left join db_syncros.public.coo_maestro_sucursal b
    on a.cod_ccosto = b.id_sucursal
where a.fecha >= dateadd('day', -90, current_date())
  and a.cantidad > 0
group by 1, 2, 3, 4
"""

# ===========================================================================
# SUPPLY OPERATIONS — Pedidos, Picking, Stock Actual, Bultos, Despachos
# NOTE: ft_pedidotransferencia and ft_picking do NOT exist in Peru.
#       The Operaciones Supply module is disabled for Peru in app.py.
# ===========================================================================

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

# ===========================================================================
# VENTA PERDIDA (Lost Sales) — Stock vs Demanda VCM (excl. Diciembre)
# ===========================================================================

# Stock tienda snapshot (por fecha)
QUERY_VP_STOCK_TIENDA = f"""
SELECT
    a.fecha,
    a.sku_producto,
    a.cod_bodega,
    COALESCE(b.id_sucursal, a.cod_bodega) AS id_sucursal,
    COALESCE(b.descripcion_sucursal, 'Sin descripcion') AS descripcion_sucursal,
    SUM(a.stock_unidades) AS stock_unidades
FROM {_INSTOCK} a
LEFT JOIN db_syncros.public.coo_maestro_sucursal b
    ON a.cod_bodega = b.id_sucursal
WHERE a.fecha >= %s AND a.fecha <= %s
  AND COALESCE(b.canal_de_distribucion, 'TIENDA') NOT IN ('CD')
GROUP BY 1,2,3,4,5
"""

# Stock CD snapshot (por fecha)
QUERY_VP_STOCK_CD = f"""
SELECT
    a.fecha,
    a.sku_producto,
    a.stock_unidades AS stock_cd
FROM {_INSTOCK_CD} a
WHERE a.fecha >= %s AND a.fecha <= %s
"""

# Ventas VCM por SKU x Sucursal (tiendas) — ventana de demanda
QUERY_VP_VENTAS_TIENDA = f"""
SELECT
    v.sku_producto,
    COALESCE(b.id_sucursal, v.cod_ccosto) AS id_sucursal,
    SUM(v.cantidad)                    AS total_und,
    SUM(v.neto)                        AS total_neto,
    CASE WHEN SUM(v.cantidad) > 0
         THEN SUM(v.neto) / SUM(v.cantidad)
         ELSE 0 END                    AS avg_price
FROM {_VCM} v
LEFT JOIN db_syncros.public.coo_maestro_sucursal b
    ON v.cod_ccosto = b.id_sucursal
WHERE v.fecha >= %s AND v.fecha <= %s
  AND v.cantidad > 0
  AND COALESCE(b.canal_de_distribucion, 'TIENDA') NOT IN ('CD')
GROUP BY 1, 2
"""

# Ventas VCM por SKU x Canal CD (MAYOR, ETAIL) — ventana de demanda
QUERY_VP_VENTAS_CD = f"""
SELECT
    v.sku_producto,
    b.canal_de_distribucion AS canal,
    SUM(v.cantidad)                    AS total_und,
    SUM(v.neto)                        AS total_neto,
    CASE WHEN SUM(v.cantidad) > 0
         THEN SUM(v.neto) / SUM(v.cantidad)
         ELSE 0 END                    AS avg_price
FROM {_VCM} v
LEFT JOIN db_syncros.public.coo_maestro_sucursal b
    ON v.cod_ccosto = b.id_sucursal
WHERE v.fecha >= %s AND v.fecha <= %s
  AND v.cantidad > 0
  AND b.canal_de_distribucion IN ('MAYOR', 'ETAIL')
GROUP BY 1, 2
"""

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
