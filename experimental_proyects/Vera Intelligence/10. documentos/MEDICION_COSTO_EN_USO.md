# Medición pequeña de costo en uso — 2026-09-14

Ejecución real autorizada por el usuario: Men's Fashion, gemini-3.7-flash, una conversación con 3 análisis encadenados y un agradecimiento. Motor actual con streaming, SQL de solo lectura y optimizaciones activas. Sin simulaciones ni prueba de mil preguntas.

| Caso | Llamadas al modelo | Costo estimado de tokens (USD) | Tiempo (s) |
|---|---:|---:|---:|
| conteo | 4 | 0.01397580 | 16.090 |
| grafico | 2 | 0.00936315 | 4.817 |
| desempeno | 3 | 0.04404023 | 21.304 |
| cortesia | 0 | 0.00000000 | 0.003 |

Total de tokens facturables estimado: US$ 0.06737917, en 9 llamadas. Promedio por análisis: US$ 0.02245972; por llamada al modelo: US$ 0.00748657. El agradecimiento no se incluye en el promedio de análisis.

Las tres ejecuciones finalizaron sin excepción. No se realizó una auditoría independiente de exactitud de sus respuestas. El conteo usó 3 consultas SQL, el gráfico 1 y el desempeño SQL + criterios de negocio. El agradecimiento usó 0 llamadas. Inicialización del chat: 2.281 s, separada de la latencia de cada análisis.

Precios Standard oficiales verificados: US$0,75/M tokens de entrada fresca; US$0,075/M cacheados; US$3,75/M salida + razonamiento, vigentes hasta 2026-12-31. Fuente: https://ai.google.dev/gemini-api/docs/pricing

Fórmula: (prompt - cached + tool_use_prompt) × 0,75/M + cached × 0,075/M + (candidates + thoughts) × 3,75/M. Los contadores provienen de metadata real; el costo es estimación por tarifa, no factura. No se utilizó el estimador del proyecto, cuya fracción de precio cacheado sigue desactualizada (0,25 en lugar de 0,10).

Excluye almacenamiento/creación del cache, embeddings e infraestructura. No hubo tool_use_prompt tokens ni herramientas vectoriales en esta muestra. No se provocaron fallas ni cancelaciones pagas. El límite de herramientas fue 6 y el temporizador de cancelación 180s por análisis; ninguno interrumpió las respuestas.

La muestra es pequeña, de un solo cliente y con contexto acumulado/cache activo. No establece un promedio representativo de todos los clientes ni demuestra ahorro causal frente a una versión anterior: no se ejecutó comparación A/B.

Evidencia local ignorada por Git: .runtime/runtime_smoke/live_cost_20260914/calls.jsonl y summary.json. No se guardaron preguntas/respuestas ni payloads SQL en esos artefactos; solo contadores, estado, tiempos y nombres de herramientas.


## Conteo y período en una consulta — 2026-09-14

Cambio en SYSTEM_INSTRUCTION_TEMPLATE, vi_agent.py: para los últimos N días disponibles, resolver MAX(fecha) y COUNT(DISTINCT conversation_id) en una sola consulta con CTE, devolviendo inicio y fin. Mantiene tenant, segmentos, fuente, grano y semántica temporal. Son N días calendario consecutivos incluyendo la última fecha; timestamps con rango semiabierto. Períodos explícitos/relativos a hoy y seguimientos conservan sus límites. Fechas inexistentes: límites nulos y total 0. Es orientación al modelo, no un optimizador/rewrite automático de SQL: no garantiza un número fijo de llamadas en todas las preguntas.

Una repetición real de la MISMA pregunta de conteo (sesión nueva, sin saludo) terminó con 1 consulta SQL y 2 llamadas al modelo, devolviendo fechas y total juntos. La traza SQL quedó en .runtime/runtime_smoke/period_count_20260914/internal_trace.json (privada, gitignored); resultado de una fila sin truncamiento. La consulta respeta tenant en el ancla y en la agregación, COUNT DISTINCT y el límite temporal semiabierto.

| Métrica | Ejecución previa | Después |
|---|---:|---:|
| Consultas SQL | 3 | 1 |
| Llamadas al modelo | 4 | 2 |
| Tokens de generación estimados (USD) | 0,01397580 | 0,00808035 |
| Latencia de análisis (s) | 16,090 | 9,132 |

En estas dos ejecuciones: aproximadamente 42% menos costo de tokens y 43% menos latencia. No es un benchmark repetido ni un ahorro garantizado: latencia y planificación varían, y la referencia original no guardó SQL para comparar todos sus límites/resultados. No se atribuye ahorro de almacenamiento/creación del cache; la inicialización se mide aparte. El cambio invalida naturalmente el fingerprint del prompt. No se alteraron criterios de negocio ni Data Maps.

Validación: 294 tests aprobados; git diff --check sin errores. Una sola pregunta paga para verificar este cambio. Evidencia de metadata y tiempos en .runtime/runtime_smoke/period_count_20260914/calls.jsonl y summary.json.


## 2026-09-14 — Turnos de desempeño: datos y criterios juntos

Diagnóstico de la ejecución original: 3 llamadas, todas con attempts=1. Secuencia initial → tool_results(run_readonly_sql) → tool_results(get_business_rules). Cero client_safe_rewrite: la hipótesis de corrección de formato no se confirmó. La primera generación consumió 4.183 tokens de salida; el registro original no guardó contenido para atribuirlos con certeza a SQL/texto.

Cambio en vi_agent.py, instrucción compartida: pedir datos y rulebook en el mismo turno cuando ambos son necesarios y el rulebook y la consulta se conocen de antemano. El executor paralelo existente los ejecuta juntos. Respetar dependencias si las reglas son necesarias para construir la consulta o el resultado determina qué rulebook solicitar. No pedir reglas en análisis puramente numéricos. Contenido de criterios completo, sin selección ni recorte; no se modificaron Data Maps.

Verificación real autorizada: una pregunta de desempeño del 7–13 septiembre 2026, en conversación nueva con fechas explícitas. 2 llamadas: initial → tool_results(get_business_rules + run_readonly_sql), ambos intentos=1. Herramientas sin error; SQL devolvió una fila no truncada; rulebook completo de 24.951 caracteres. Costo estimado de tokens US$0,02064285, tiempo 12,338s (inicialización 2,501s aparte). Referencia histórica US$0,04404023 y 21,304s, pero su contexto acumulado/pregunta relativa difieren: no atribuir toda la diferencia al cambio ni anunciar un porcentaje causal. Confirmado que ambas herramientas se pidieron en el mismo turno. No se auditó independientemente la exactitud de la respuesta.

294 tests aprobaron; git diff --check sin errores. Evidencia privada gitignored: .runtime/runtime_smoke/parallel_rules_20260914/{calls.jsonl,summary.json,internal_trace.json}. Precio por metadata y tarifa, no factura; excluye cache storage/creación e infraestructura. Planificación orientada por prompt, no garantía de dos llamadas para todo caso.
