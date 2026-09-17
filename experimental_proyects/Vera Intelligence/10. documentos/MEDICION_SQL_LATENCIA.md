# SQL: medición y mejora de conexión — 2026-09-14

Se midieron las dos consultas reales capturadas de Men's Fashion (conteo con período y tasas de desempeño), sin llamadas a Gemini. Cada plan se ejecutó 3 veces con EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON), bajo default_transaction_read_only=on y timeout de 60s. No hubo cambios de esquema ni índices.

## Resultado del perfil

- Conexión nueva: 2.913ms en esta ejecución, separada de la consulta.
- Conteo: ejecución del servidor 9,796 / 4,100 / 4,079ms; ida/vuelta 688,926 / 362,959 / 212,058ms.
- Desempeño: servidor 208,016 / 5,384 / 5,456ms; ida/vuelta 468,997 / 308,816 / 207,934ms.
- Ambas consultas usaron índices existentes sobre la fecha; en caliente no leyeron bloques de disco. No hay evidencia en estas dos consultas que justifique agregar índices o reescribir sus agregaciones.

El tiempo del callback de una herramienta incluye conexión, espera, validación y transporte; no equivale al tiempo de ejecución de PostgreSQL. Estos resultados solo describen este cliente y estas consultas.

## Cambio aplicado

La conexión reusable de vi_agent._get_reusable_sql_connection se abre con autocommit=True. Cada SELECT independiente evita el BEGIN implícito y termina sin dejar una sesión idle in transaction. Se mantienen el validador, tenant, fuentes permitidas, límites, timeout, readonly y recuperación; utils/postgres.py y las conexiones de otros pipelines no se modificaron.

Confirmación del mecanismo en documentación oficial: https://www.psycopg.org/psycopg3/docs/basic/transactions.html#autocommit-transactions

La comparación se hizo en la misma conexión, alternando modos y ejecutando exactamente el mismo SQL, 3 muestras por modo y por consulta. Una lectura previa calentó cada consulta. En el modo transaccional cada muestra empezó una transacción nueva y el rollback posterior se hizo fuera del tiempo medido. Resultados iguales en todas las comparaciones.

| Consulta | Mediana con BEGIN (ms) | Mediana autocommit (ms) |
|---|---:|---:|
| Conteo | 409,975 | 208,104 |
| Desempeño | 411,293 | 207,391 |

El ahorro observado de ~200ms corresponde a iniciar una transacción. No significa 50% menos latencia para cada consulta: la versión anterior dejaba la transacción abierta y sus lecturas posteriores ya tomaban alrededor de 200ms. El beneficio aplica a la primera lectura de una conexión y tras reinicios de transacción; la apertura de conexión de varios segundos sigue existiendo. No modifica los tokens o precio por llamada al modelo ni promete menor factura.

Semántica: cada SELECT conserva su snapshot de sentencia; no se ofrece atomicidad entre distintas consultas del chat. El agente ya usa lecturas analíticas independientes y no hay un contrato de transacción compartida entre preguntas.

## Validación

294 tests aprobados. Verificación real a través de run_readonly_sql confirmó readonly, autocommit, estado IDLE tras éxito y tras UndefinedColumn, recuperación en la misma conexión, resultados idénticos y lectura caliente de 206,451ms. Se provocó solamente una consulta de lectura con columna inexistente, sin DML.

Evidencia privada gitignored en .runtime/runtime_smoke/sql_profile_20260914/: conteo_plans.json, desempeno_plans.json, summary.json, autocommit_comparison.json y verification.json. Scripts locales de medición en .runtime/runtime_smoke/. No guardar planes completos en documentación pública: pueden contener nombres físicos y filtros internos.


## 2026-09-14 — Conexión SQL preparada al entrar al cliente

Implementado prewarm_sql_connection en vi_agent.py: apertura en segundo plano con executor de un trabajador, deduplicación de apertura pendiente y reutilización de conexión ya abierta. El trabajador no ejecuta SQL, no usa Gemini ni consulta datos de un tenant, no accede a Streamlit y no modifica CLIENT_CONFIG. Usa _get_reusable_sql_connection y el mismo lock que run_readonly_sql; si una pregunta llega antes, espera la apertura en curso en lugar de abrir otra. Falla encapsulada como False: la consulta mantiene su conexión/recuperación normal. Autocommit/readonly y validación conservados.

streamlit_app._init_client y vi_agent_tester.run_cli disparan el precalentamiento tras load_environment, antes de build_chat, solapándolo con preparación del chat/saludo. Los reruns que mantienen cliente/chat no vuelven a programar el trabajo; otra sesión/cliente reutiliza la conexión del proceso si está abierta. No se agregaron conexiones por cliente ni pings/consultas para mantenerlas activas. No se precalienta la búsqueda vectorial.

Validación: 298 tests aprobaron. Nuevos tests cubren retorno no bloqueante con conexión retenida, deduplicación de trabajo pendiente, primera consulta reutiliza una única conexión readonly/autocommit, falla no fatal con reintento posterior, conexión ya abierta e integración antes de construir chat sin repetición en rerun. Fixture de AppTest actualizado para no conectar servicios durante pruebas simuladas. git diff --check sin errores.

Una pregunta real de conteo con conexión lista: 1 consulta SQL, 2 llamadas al modelo, análisis 7,249s, herramienta SQL 827,7ms, costo estimado de tokens US$0,0096606. Apertura en segundo plano + construcción del chat 1,665s; build_chat terminó a los0,212s y la conexión aún necesitó1,453s. El script de medición esperó explícitamente la finalización del precalentamiento antes de cronometrar la pregunta; la interfaz productiva NO hace esa espera explícita. Ref previa de conteo: análisis9,132s, SQL2335,8ms, pero se realizaron en momentos distintos y con cambios previos de prompt; no es A/B repetido ni ahorro causal garantizado. La latencia de apertura se desplaza antes de la pregunta; no desaparece y si el usuario pregunta inmediatamente puede seguir esperando. No se promete menor costo de modelo (esta ejecución incluso varió al alza respecto a la referencia por tokens generados).

Evidencia privada gitignored: .runtime/runtime_smoke/prewarmed_count_20260914/{summary.json,calls.jsonl,internal_trace.json}; harness measure_prewarmed_count.py. Sin nuevas escrituras en base ni cambios de índices/esquema. No se modificó utils/postgres.py ni otros pipelines.
