-- ============================================================
-- q01_total_analizables
-- Pregunta: ¿Cuántas conversaciones analizables hay en total?
-- ============================================================
SELECT COUNT(DISTINCT conversation_id) AS total_analizables
FROM dashboard_v2.vw_mens_fashion_demografia
WHERE seller_id = 'Mens Fashion'
  AND usefulforanalysis IS TRUE;

-- ============================================================
-- q02_tasa_se_presenta
-- Pregunta: ¿Qué porcentaje de las veces que era evaluable, el vendedor se presentó al cliente?
-- ============================================================
SELECT 
    COUNT(DISTINCT CASE WHEN vendedorsepresenta = 'Sí' THEN conversation_id END) AS cumplimientos,
    COUNT(DISTINCT CASE WHEN vendedorsepresenta IN ('Sí', 'No') THEN conversation_id END) AS base_evaluable,
    COUNT(DISTINCT CASE WHEN vendedorsepresenta = 'N/A' THEN conversation_id END) AS no_aplica,
    COUNT(DISTINCT conversation_id) AS total_conversaciones,
    ROUND(
        100.0 * COUNT(DISTINCT CASE WHEN vendedorsepresenta = 'Sí' THEN conversation_id END) / 
        NULLIF(COUNT(DISTINCT CASE WHEN vendedorsepresenta IN ('Sí', 'No') THEN conversation_id END), 0), 
        2
    ) AS tasa_cumplimiento_pct
FROM dashboard_v2.vw_mens_fashion_rendimiento_vendedor
WHERE seller_id = 'Mens Fashion';

-- ============================================================
-- q03_distribucion_resultado_general
-- Pregunta: Dame la distribución de resultado_general de las conversaciones (compra total, parcial, no compra, cambio, queja).
-- ============================================================
SELECT 
    resultado_general,
    COUNT(DISTINCT conversation_id) AS total_conversaciones,
    ROUND(100.0 * COUNT(DISTINCT conversation_id) / SUM(COUNT(DISTINCT conversation_id)) OVER (), 2) AS porcentaje
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_generales
WHERE seller_id = 'Mens Fashion'
GROUP BY resultado_general
ORDER BY total_conversaciones DESC;

-- ============================================================
-- q04_top5_motivo_venta_perdida_producto
-- Pregunta: ¿Cuáles son los 5 motivos más frecuentes de venta perdida a nivel producto?
-- ============================================================
SELECT 
    motivo_venta_perdida_producto,
    COUNT(DISTINCT (conversation_id, producto_index)) AS total_productos,
    ROUND(100.0 * COUNT(DISTINCT (conversation_id, producto_index)) / SUM(COUNT(DISTINCT (conversation_id, producto_index))) OVER (), 2) AS porcentaje
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_por_producto
WHERE seller_id = 'Mens Fashion'
  AND resultado_producto = 'no_comprado'
  AND motivo_venta_perdida_producto IS NOT NULL
GROUP BY motivo_venta_perdida_producto
ORDER BY total_productos DESC
LIMIT 10;

-- ============================================================
-- q05_mencion_competencia
-- Pregunta: ¿En cuántas conversaciones se mencionó a la competencia?
-- ============================================================
SELECT 
    COUNT(DISTINCT conversation_id) AS total_conversaciones,
    COUNT(DISTINCT CASE WHEN hubo_mencion_competencia IS TRUE THEN conversation_id END) AS menciones_competencia,
    ROUND(COUNT(DISTINCT CASE WHEN hubo_mencion_competencia IS TRUE THEN conversation_id END)::numeric / NULLIF(COUNT(DISTINCT conversation_id), 0) * 100, 2) AS porcentaje
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_generales
WHERE seller_id = 'Mens Fashion';

-- ============================================================
-- q06_color_mas_solicitado
-- Pregunta: ¿Cuál es el color más solicitado por los clientes?
-- ============================================================
SELECT
    color_solicitado_producto,
    COUNT(DISTINCT (conversation_id, producto_index)) AS total_menciones,
    ROUND(100.0 * COUNT(DISTINCT (conversation_id, producto_index)) / SUM(COUNT(DISTINCT (conversation_id, producto_index))) OVER (), 2) AS porcentaje
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_por_producto
WHERE seller_id = 'Mens Fashion'
  AND color_solicitado_producto IS NOT NULL
GROUP BY color_solicitado_producto
ORDER BY total_menciones DESC;

-- ============================================================
-- q07_tasa_exito_manejo_objecion
-- Pregunta: De las objeciones que el vendedor trabajó, ¿en qué porcentaje tuvo éxito?
-- ============================================================
SELECT
    COUNT(*) AS total_productos_con_objecion,
    SUM(CASE WHEN hubo_manejo_objecion IS TRUE THEN 1 ELSE 0 END) AS objeciones_trabajadas,
    ROUND(100.0 * SUM(CASE WHEN hubo_manejo_objecion IS TRUE THEN 1 ELSE 0 END) / COUNT(*), 2) AS pct_trabajadas
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_por_producto
WHERE seller_id = 'Mens Fashion'
  AND objecion_principal IS NOT NULL;

-- ============================================================
-- q08_tasa_cierre_a_medida
-- Pregunta: Cuando se ofrece confección a medida para un producto, ¿qué porcentaje de las veces ayuda a cerrar la venta?
-- ============================================================
SELECT
    resultado_a_medida,
    se_compro,
    COUNT(DISTINCT (conversation_id, producto_index)) AS productos_count
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_por_producto
WHERE seller_id = 'Mens Fashion'
  AND hubo_ofrecimiento_a_medida IS TRUE
GROUP BY resultado_a_medida, se_compro
ORDER BY resultado_a_medida, se_compro;

-- ============================================================
-- q09_promedio_productos_mencionados
-- Pregunta: ¿Cuál es el promedio de productos mencionados por conversación?
-- ============================================================
SELECT 
    cantidad_productos_mencionados,
    COUNT(DISTINCT conversation_id) AS total_conversaciones,
    ROUND(COUNT(DISTINCT conversation_id) * 100.0 / SUM(COUNT(DISTINCT conversation_id)) OVER(), 2) AS porcentaje
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_generales
WHERE seller_id = 'Mens Fashion'
GROUP BY cantidad_productos_mencionados
ORDER BY cantidad_productos_mencionados;

-- ============================================================
-- q10_ocasion_uso_traje
-- Pregunta: Cuando el producto es un traje, ¿cuál es la ocasión de uso más común?
-- ============================================================
SELECT 
    ocasion_de_uso,
    COUNT(DISTINCT (conversation_id, producto_index)) AS total_menciones,
    ROUND(COUNT(DISTINCT (conversation_id, producto_index)) * 100.0 / SUM(COUNT(DISTINCT (conversation_id, producto_index))) OVER (), 2) AS porcentaje
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_por_producto
WHERE seller_id = 'Mens Fashion'
  AND producto = 'traje'
GROUP BY ocasion_de_uso
ORDER BY total_menciones DESC;

-- ============================================================
-- q11_distribucion_amabilidad
-- Pregunta: ¿En cuántas conversaciones el vendedor fue amable, y en cuántas no?
-- ============================================================
SELECT 
    vendedoramable,
    COUNT(DISTINCT conversation_id) AS total_conversaciones
FROM dashboard_v2.vw_mens_fashion_rendimiento_vendedor
WHERE seller_id = 'Mens Fashion'
GROUP BY vendedoramable
ORDER BY total_conversaciones DESC;

-- ============================================================
-- q12_pct_no_menciona_cashback_evaluado
-- Pregunta: ¿En qué porcentaje de las conversaciones el vendedor no mencionó el cashback?
-- ============================================================
SELECT 
    primer_mencion_cashback,
    COUNT(DISTINCT conversation_id) AS conversaciones,
    ROUND(COUNT(DISTINCT conversation_id) * 100.0 / (SELECT COUNT(DISTINCT conversation_id) FROM dashboard_v2.vw_mens_fashion_insights_categoricos_generales WHERE seller_id = 'Mens Fashion' AND primer_mencion_cashback IS NOT NULL), 2) AS pct_sobre_evaluadas
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_generales
WHERE seller_id = 'Mens Fashion' AND primer_mencion_cashback IS NOT NULL
GROUP BY primer_mencion_cashback
ORDER BY conversaciones DESC;

-- ============================================================
-- q13_legacy_programa_lealtad
-- Pregunta: ¿El vendedor explicó el programa de lealtad en cuántas conversaciones?
-- ============================================================
SELECT 
    vendedorinformocashback20porciento,
    COUNT(DISTINCT conversation_id) AS total_conversaciones
FROM dashboard_v2.vw_mens_fashion_rendimiento_vendedor
WHERE seller_id = 'Mens Fashion'
GROUP BY vendedorinformocashback20porciento;

-- ============================================================
-- q14_ranking_vendedores_compra_total
-- Pregunta: ¿Cuáles son los 5 vendedores con más conversaciones de compra total?
-- ============================================================
SELECT 
    employee_id,
    employee_full_name,
    COUNT(DISTINCT conversation_id) AS total_conversaciones_compra_total
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_generales
WHERE seller_id = 'Mens Fashion'
  AND resultado_general = 'compra_total'
  AND employee_full_name IS NOT NULL
GROUP BY employee_id, employee_full_name
ORDER BY total_conversaciones_compra_total DESC
LIMIT 5;

-- ============================================================
-- q15_fit_mas_buscado_objecion_precio
-- Pregunta: Entre los productos donde hubo objeción de precio, ¿cuál es el fit más buscado?
-- ============================================================
SELECT 
    fit_buscado,
    COUNT(DISTINCT (conversation_id, producto_index)) AS total_productos,
    ROUND(100.0 * COUNT(DISTINCT (conversation_id, producto_index)) / SUM(COUNT(DISTINCT (conversation_id, producto_index))) OVER (), 2) AS porcentaje
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_por_producto
WHERE seller_id = 'Mens Fashion'
  AND objecion_principal = 'precio'
GROUP BY fit_buscado
ORDER BY total_productos DESC;

-- ============================================================
-- q16_tasa_compra_segun_pregunta_ocasion
-- Pregunta: ¿La tasa de compra total es distinta cuando el vendedor pregunta la ocasión de uso versus cuando no lo hace?
-- ============================================================
SELECT 
    rv.vendedorpreguntaocasionuso,
    COUNT(DISTINCT rv.conversation_id) AS total_conversaciones,
    COUNT(DISTINCT CASE WHEN ci.resultado_general = 'compra_total' THEN rv.conversation_id END) AS compras_totales,
    ROUND(100.0 * COUNT(DISTINCT CASE WHEN ci.resultado_general = 'compra_total' THEN rv.conversation_id END) / COUNT(DISTINCT rv.conversation_id), 2) AS pct_compra_total
FROM dashboard_v2.vw_mens_fashion_rendimiento_vendedor rv
JOIN dashboard_v2.vw_mens_fashion_insights_categoricos_generales ci 
  ON rv.conversation_id = ci.conversation_id
WHERE rv.seller_id = 'Mens Fashion'
  AND rv.usefulforanalysis IS TRUE
GROUP BY rv.vendedorpreguntaocasionuso;

-- ============================================================
-- q17_top5_tiendas
-- Pregunta: ¿Cuáles son las 5 tiendas con más conversaciones?
-- ============================================================
SELECT
    store_name,
    COUNT(DISTINCT conversation_id) AS total_conversaciones
FROM dashboard_v2.vw_mens_fashion_demografia
WHERE seller_id = 'Mens Fashion'
GROUP BY store_name
ORDER BY total_conversaciones DESC
LIMIT 5;

-- ============================================================
-- q18_evolucion_mensual
-- Pregunta: Mostrame la cantidad de conversaciones analizables por mes.
-- ============================================================
SELECT 
    TO_CHAR(DATE_TRUNC('month', uploaded_at_local), 'YYYY-MM') AS mes,
    COUNT(DISTINCT conversation_id) AS total_conversaciones_analizables
FROM dashboard_v2.vw_mens_fashion_demografia
WHERE seller_id = 'Mens Fashion'
  AND usefulforanalysis IS TRUE
GROUP BY 1
ORDER BY 1 ASC;

-- ============================================================
-- q19_pregunta_preferencias_sin_complementos
-- Pregunta: De las conversaciones donde el vendedor preguntó las preferencias del cliente, ¿en qué porcentaje NO sugirió complementos?
-- ============================================================
SELECT
    COUNT(DISTINCT conversation_id) AS total_pregunto_preferencias,
    COUNT(DISTINCT CASE WHEN vendedorsugiriocomplementos = 'Sí' THEN conversation_id END) AS sugirio_si,
    COUNT(DISTINCT CASE WHEN vendedorsugiriocomplementos = 'No' THEN conversation_id END) AS sugirio_no,
    COUNT(DISTINCT CASE WHEN vendedorsugiriocomplementos = 'N/A' THEN conversation_id END) AS sugirio_na,
    ROUND(100.0 * COUNT(DISTINCT CASE WHEN vendedorsugiriocomplementos = 'No' THEN conversation_id END) / NULLIF(COUNT(DISTINCT CASE WHEN vendedorsugiriocomplementos IN ('Sí', 'No') THEN conversation_id END), 0), 2) AS tasa_no_base_evaluable,
    ROUND(100.0 * COUNT(DISTINCT CASE WHEN vendedorsugiriocomplementos = 'No' THEN conversation_id END) / COUNT(DISTINCT conversation_id), 2) AS pct_no_total
FROM dashboard_v2.vw_mens_fashion_rendimiento_vendedor
WHERE seller_id = 'Mens Fashion'
  AND vendedorpreguntapreferenciascliente = 'Sí';

-- ============================================================
-- q20_trap_redes_sociales_origen_vs_motivador
-- Pregunta: ¿Cuántas conversaciones el cliente llegó inspirado por redes sociales?
-- ============================================================
SELECT 
    origen_inspiracion_cliente,
    COUNT(DISTINCT conversation_id) AS cantidad_conversaciones
FROM dashboard_v2.vw_mens_fashion_insights_categoricos_generales
WHERE seller_id = 'Mens Fashion'
GROUP BY origen_inspiracion_cliente
ORDER BY cantidad_conversaciones DESC;
