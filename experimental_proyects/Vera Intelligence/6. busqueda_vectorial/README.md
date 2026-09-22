# Búsqueda vectorial sobre conversaciones

**Estado al 2026-09-14:** búsqueda habilitada en 7 clientes: mens_fashion_alto, steren_alto,
farma24_alto, maga_alto, tigo_alto, roberts_alto y high_life_alto. Las evaluaciones y decisiones
posteriores al piloto están en las iteraciones de este documento. La reconstrucción de fragmentos
sigue siendo aproximada; no es una herramienta de conteo semántico poblacional.
Esta actualización verificó configuración y código locales, sin repetir consultas en producción.

**Actualización 2026-09-16 (esta línea, no la de arriba, es la vigente):** ampliado a **15 de 19
clientes** -se sumaron atlas_alto, boggi_alto, gac_medio, hyundai_bajo, salomon_alto,
huerpel_hostess_alto, huerpel_hostess_seminuevos_medio y huerpel_ventas_alto, verificado por SQL
directo contra `analytics_v2.conversation_embeddings`. Sin habilitar por volumen insuficiente:
agrosuper_bajo, shoe_box_bajo, forever_21_bajo. En la misma ronda se agregaron: filtro
`employee_name` para personalización de coaching individual (con el hallazgo real de que la
coincidencia parcial puede cruzar entre dos personas con el mismo nombre de pila), un modo
MEJORES PRÁCTICAS INTERNAS que ancla la recomendación a un caso real de un top performer (citado
siempre de forma anónima, nunca por nombre), seguimiento temporal de coaching (¿mejoró después de
la recomendación?), un fix de fondo al falso positivo de `answer_verification.py` (`non_metric`),
un fix de un cuelgue real de red (timeout HTTP explícito, antes ausente en los tres clientes de
Gemini del proyecto), y un reporte de calidad por cliente (`vector_search_quality_report.py`).
Detalle completo, con verificación en vivo de cada punto, en "8. README.md" (raíz del proyecto),
sección "Ampliación de búsqueda vectorial a todos los clientes viables" en adelante -no
duplicado acá para no desincronizar dos fuentes de la misma información.

**Antecedente histórico del primer piloto (2026-09-10): conectado para un cliente, como
prototipo explícitamente no definitivo.** La tool `search_conversations` ya está integrada en
`4. scripts/vi_agent.py` (módulo nuevo `4. scripts/vector_search.py`) y habilitada para
`mens_fashion_alto` — probada de punta a punta con `vi_agent.py --client mens_fashion_alto`: el
modelo combinó `search_conversations` (dos llamadas con reformulaciones distintas) y
`run_readonly_sql` en la misma respuesta, citó fragmentos reales de conversaciones sobre demoras de
entrega, y no filtró ningún identificador interno. El diseño previo de este README (Opción A vs. B,
las 6 preguntas bloqueantes) queda documentado abajo como estaba, más las secciones nuevas de esta
fecha con lo que se corrigió al conectarlo de verdad. La vectorización de las conversaciones la sigue
armando **otra persona, por fuera de este proyecto** — este README y el código de acá sólo consultan
su resultado, de solo lectura.

**Por qué "no definitivo" pese a estar conectado**: a pedido explícito del usuario, se implementó la
reconstrucción del texto del chunk (ver "Hallazgo en Postgres QA" más abajo) como una aproximación
funcional, no como la solución final — sigue sin confirmarse el algoritmo real de chunking con la
persona que vectoriza. Tratar `vector_search.py` como un prototipo conectado a producción, no como
diseño cerrado: es fácil de revisar/reemplazar cuando esa confirmación llegue (toda la lógica de
reconstrucción vive en una sola función, `_reconstruct_chunk_text`).

Si estás leyendo esto sin contexto previo del proyecto: primero leé `8. README.md` en la raíz del
proyecto (arquitectura general, cómo está armado el RAG que ya existe, estado de los 19 clientes) — este
documento asume que ya lo leíste.

## Resumen para la reunión semanal del martes (tarea de Lucas: capacidades y limitaciones)

*(Última actualización 2026-09-11. Ver el resto del documento y `9. HISTORIAL.md` para el detalle
completo y las fuentes de cada afirmación.)*

**Qué es, en una frase**: una tool nueva (`search_conversations`) que le permite a Vera Intelligence
buscar por *significado* dentro del texto libre de las conversaciones de venta — algo que hoy
ninguna de las dos fuentes existentes (SQL estructurado, RAG de catálogo) puede hacer.

**Estado**: ya no es "un piloto en un solo cliente" — habilitado en **7 de 19 clientes**
(mens_fashion_alto, steren_alto, farma24_alto, maga_alto, tigo_alto, roberts_alto, high_life_alto),
elegidos por volumen y cobertura real de datos, no arbitrariamente. Sigue siendo explícitamente no
definitivo (ver limitaciones abajo), pero el foco pasó de "¿funciona?" a "¿para qué sirve de
verdad, y a qué costo?".

### Capacidades demostradas (con evidencia real, no supuestas)

- **Encuentra contenido que ningún campo estructurado captura** — texto libre, tono, promesas
  exactas, menciones de competidores. Verificado en los 7 clientes habilitados, no sólo el piloto
  original.
- **Descubrimiento cualitativo, el ángulo con más valor de negocio real**: usado con preguntas
  abiertas ("¿hay algo que los clientes piden que no resolvemos?"), encontró patrones de negocio
  concretos y accionables en Mens Fashion sin que nadie los buscara a propósito -ej. la confección a
  medida (15-20 días de espera) pierde ventas por urgencia del cliente; inventario desigual entre
  sucursales y canal online; fin de temporada sin reposición. Ver Iteración 16 más abajo.
- **Integridad de citas reforzada (2026-09-11)**: tras un hallazgo real de un revisor externo (ver
  abajo), ahora exige explícitamente que cualquier frase entre comillas venga literal de una
  conversación real -nunca una síntesis de otro campo presentada como si fuera dicha textualmente.
  Verificado en vivo: la misma pregunta que antes se resolvía con una sola búsqueda floja + relleno
  de SQL ahora dispara una búsqueda real con reformulación y responde con diálogo citado genuino.
- **No alucina cuando no hay evidencia real**, se combina con SQL en la misma respuesta cuando hace
  falta, y nunca expone el mecanismo técnico -mismo comportamiento validado repetidamente.
- **~70% menos costo por sesión** (prompt caching de Gemini + presupuesto de razonamiento ajustado,
  medido en vivo) -no es específico de esta tool, pero la hace más barata de generalizar al resto de
  los clientes.

### Un revisor externo encontró y ayudó a arreglar un bug real en producción

A pedido explícito de sacar el sesgo de confirmación (la misma persona diseñó la tool y las 11
preguntas de evaluación originales), se corrió una revisión ciega con un agente sin acceso al
diseño ni a las preguntas existentes. Encontró que **Farma24 estaba 100% caído** -cualquier
pregunta fallaba, incluso el saludo- por una interacción real entre el cache de prompt (agregado
esta misma sesión) y el RAG de Farma24 que nadie había vuelto a probar juntos. Arreglado y
verificado el mismo día en los dos clientes afectados (Farma24, Maga). **Vale la pena mencionarlo
así en la reunión, no esconderlo**: es la prueba de que el proceso de revisión funciona, no un
fracaso -encontrar y cerrar esto en el mismo día es mejor resultado que no haberlo buscado.

### Limitaciones conocidas (honestas, no resueltas a propósito todavía)

- **No sirve para conteos exactos** -probado a fondo (umbral de distancia, lectura manual, embeddings
  de resúmenes, en dos clientes de dominios distintos): la nuance específica de negocio se pierde
  frente al tema general, siempre. La vía correcta para números reales es un pipeline de
  clasificación exhaustivo en la ingesta, no esta tool.
- **Sin índice vectorial todavía, y esto ya dejó de ser teórico**: con Farma24 (~438k conversaciones)
  se confirmó un timeout real en producción por falta de índice -no "podría pasar", ya pasó. No
  depende de este proyecto (la tabla la administra otra persona) -decisión pendiente de escalar.
  **Actualizado 2026-09-11 con datos reales limpios de 7 clientes (no sólo Farma24)**: sobre 49
  búsquedas reales, mediana 4.1s, p90 47.7s, máximo 123.4s -**39% tarda más de 10s, 24% más de 30s**.
  No es un caso aislado, es el comportamiento típico de una fracción real de las búsquedas.
- **La reconstrucción del texto citado sigue siendo una aproximación**, no el algoritmo exacto de
  chunking -sigue sin confirmarse con la persona que vectoriza.
- **12 de 19 clientes sin habilitar todavía** -no por costo (~$0,05/mes incluso con uso alto), sino
  por volumen insuficiente (mayoría con <300 conversaciones) o cobertura de backfill baja.

### Próximo paso recomendado a discutir el martes

(1) Escalar el índice vectorial con la persona que vectoriza -ya no es preventivo, hay un timeout
real bloqueando Farma24 en preguntas sin acotar. (2) Decidir si vale la pena formalizar el ángulo de
descubrimiento como una rutina periódica (no un cambio de código, un hábito de qué preguntas hacerle
a la tool). Ver "Próximos pasos, en orden" más abajo para el detalle completo.

## Por qué existe esto (el problema de negocio)

Hoy Vera Intelligence responde preguntas de negocio de dos formas:

1. **SQL sobre `dashboard_v2`** (`run_readonly_sql`): cualquier pregunta que se pueda calcular a partir
   de campos ya extraídos y estructurados por el pipeline de checklist/insights (tasas, conteos,
   rankings, cruces).
2. **RAG sobre documentos** (`rag_sources` / Gemini File Search): sólo para 2 de 19 clientes
   (`farma24_alto`, `maga_alto`), y sólo sobre un catálogo de productos — no sobre las conversaciones en
   sí.

Ninguna de las dos puede responder una pregunta como *"traeme ejemplos de conversaciones donde el
cliente se quejó de que el envío llegó tarde"* o *"¿qué objeciones de precio menciona la gente que no
estén ya capturadas en el checklist?"* — porque esa información vive en el **texto libre** de la
transcripción, no en un campo estructurado ni en un documento de catálogo. Vectorizar las conversaciones
y poder buscar semánticamente sobre ellas es lo que cierra ese hueco: encontrar conversaciones por
*significado*, no por coincidencia exacta de palabras, y sin que alguien tenga que leer miles de
transcripciones a mano.

Relacionado, no confundir: Farma 24 y Maga ya usan RAG, pero apunta a un **catálogo de productos**
(documentos de referencia, no conversaciones) para resolver sustitución/venta cruzada. Es un mecanismo
similar (File Search de Gemini) aplicado a una fuente de datos completamente distinta.

## La bifurcación que define todo el resto del diseño

Todo lo demás (qué código escribir, qué tan rápido se puede tener un prototipo, cuánto control se tiene
sobre la búsqueda) depende de una sola decisión, que no la tomamos nosotros — la determina cómo la otra
persona entregue el vectorizado:

| | **Opción A — Gemini File Search** | **Opción B — tabla propia (ej. `pgvector`)** |
|---|---|---|
| **Quién indexa/vectoriza** | Gemini (server-side), al subir los documentos a un store | La otra persona, con su propio pipeline de embeddings |
| **Dónde viven los vectores** | Store de Gemini File Search (mismo mecanismo que `product_catalog` de Farma24) | Postgres (mismo motor que `dashboard_v2`) u otro vector store |
| **Código nuevo en este proyecto** | Ninguno — es declarativo | Una tool function nueva (`search_conversations`), con su test y su capa de aislamiento |
| **Control sobre chunking** | Ninguno (lo decide Gemini internamente) | Total (lo decide la otra persona: por conversación, por turno, con overlap) |
| **Control sobre metadata en el resultado** | Limitado a lo que declares en el prompt/documento | Total — cualquier columna que la tabla tenga (fecha, vendedor, tienda, `conversation_id`) |
| **Puede cruzarse con SQL estructurado en la misma pregunta** | Difícil (dos mecanismos de tool-calling distintos, ver nota técnica abajo) | Sí, directo — es otra tool Python más, como `run_readonly_sql` |
| **Filtro de aislamiento por cliente** | Un store físico por cliente (igual que hoy) | Filtro `WHERE client_id = ...` explícito en la query, mismo criterio que `sql_security.py` exige para `dashboard_v2` |
| **Esfuerzo de implementación** | Bajo — declarar la fuente en `config.yaml` de un cliente piloto y probar | Medio-alto — nueva tool, nuevo test, nueva verificación de aislamiento |

**Nota técnica sobre Opción A**: el mecanismo de RAG que ya existe (`4. scripts/rag_sources.py`) usa
`types.Tool(file_search=...)`, que Gemini invoca **del lado del servidor** — no pasa por el loop manual
de `function_calls`/`TOOL_FUNCTIONS` que sí usan `run_readonly_sql` y `get_business_rules`. Combinar un
tool nativo server-side con function calling manual ya requiere
`tool_config=types.ToolConfig(include_server_side_tool_invocations=True)` en `GenerateContentConfig`
(sin ese flag, la API devuelve `400 INVALID_ARGUMENT` — ver "Bug encontrado y corregido al integrar RAG"
en `8. README.md`). Esto ya funciona hoy para RAG de catálogo + SQL en la misma sesión (Farma24), así
que técnicamente no es un problema nuevo — pero si alguna vez hace falta que el modelo decida
dinámicamente, en la misma respuesta, "esto lo busco en las conversaciones vectorizadas Y esto lo
calculo con SQL", vale la pena confirmarlo con una prueba real antes de asumir que funciona igual de
bien con dos fuentes semánticas distintas (catálogo + conversaciones) a la vez.

## Hallazgo en Postgres QA (2026-09-10): la infraestructura de Opción B ya existe

Verificado con SQL de solo lectura directo contra Postgres QA (`utils/postgres.py`, sin pasar por la
persona que vectoriza ni por ningún LLM) — el mismo criterio de `data-map-audit`, aplicado acá a una
tabla nueva en vez de a un Data Map de cliente. Esto **confirma Opción B** (tabla propia, no Gemini File
Search) y responde en gran parte las preguntas 1, 3, 4, 5 y 6 de la lista de abajo, dejando pendientes
principalmente el detalle de reconstrucción de texto (nuevo hallazgo, no estaba en la lista original) y
la pregunta 2 (granularidad) sólo parcialmente resuelta.

### `analytics_v2.conversation_embeddings` — la tabla de vectores

719.338 filas a la fecha de la verificación. Columnas: `embedding_config_id` (text), `recording_id`
(text), `chunk_idx` (int), `embedding` (`vector`, **dimensión 768** confirmada vía `atttypmod`),
`model_id` (text), `content_hash` (text), `source_updated_at`, `source_etl_run_id` (uuid), `embedded_at`.
PK compuesta `(embedding_config_id, recording_id, chunk_idx)`. **No hay índice vectorial** (ivfflat/hnsw)
creado todavía — sólo el btree de la PK; una búsqueda por similitud hoy sería un scan secuencial sobre
las 719k filas, no una búsqueda indexada.

- **Pregunta 3 (modelo de embedding) — resuelta**: un único valor en todo el dataset,
  `model_id = 'gemini-embedding-001'`. El `embedding_config_id` completo es
  `gemini-developer-api:gemini-embedding-001:768:retrieval-document:srt-v1:short-whole-long-fixed-overlap:tokens-per-word-1.3:max-1500:window-1153:overlap-153:step-1000:l2-v1`
  — codifica el modelo, la dimensión, el **task type** (`retrieval-document`) y los parámetros de
  chunking, todo en un solo string versionado.
- **Nuance técnica nueva, no estaba en la lista de preguntas original**: Gemini usa embeddings
  *asimétricos* por `task_type` — los vectores de esta tabla se generaron con `task_type=retrieval_document`.
  Para que la búsqueda funcione, el vector de la *pregunta del usuario* tiene que generarse con
  `task_type=retrieval_query` (no `retrieval_document` ni el default) — son el mismo modelo pero
  proyecciones distintas, optimizadas para el rol de cada lado. Confirmar esto con la otra persona o con
  la documentación de la Gemini Embedding API antes de escribir `search_conversations`; usar el `task_type`
  equivocado no rompe con un error, simplemente da resultados peores sin avisar.
- **Pregunta 2 (granularidad) — parcialmente resuelta**: no es "un vector por conversación completa".
  Promedio 1,12 chunks por `recording_id`, máximo 13-14 en las conversaciones más largas — o sea, la
  mayoría de las conversaciones cortas entran en un solo chunk, pero las largas sí se parten. El
  `embedding_config_id` indica windowing con overlap (`window-1153:overlap-153:step-1000`, `max-1500`
  tokens, ratio `tokens-per-word-1.3`) sobre el `.srt` (`srt-v1`) — pero el algoritmo exacto de cómo se
  arma cada ventana (por turno, por tiempo, por token crudo) no está documentado en la tabla misma; sólo
  se infiere de los nombres de los parámetros. Sigue sin confirmar con la otra persona.
- **Hallazgo nuevo — la tabla NO guarda el texto del chunk**: sólo `content_hash` (SHA-256 del contenido
  fuente, sirve para detectar cambios/dedupe, no para mostrar nada). Para que el modelo cite el fragmento
  encontrado, `search_conversations` va a tener que reconstruir el texto del chunk a partir de
  `transcribed_audio` (en `core_v2.conversations`) aplicando `chunk_idx` + los mismos parámetros de
  windowing — o pedirle a la otra persona que además persista el texto del chunk (más simple, evita
  duplicar lógica de chunking en dos lugares). **Esto es un ítem nuevo a resolver, no estaba anticipado
  en el diseño original.**

### Aislamiento por cliente (pregunta 4) — resuelta, pero no es un filtro directo

La tabla de embeddings **no tiene `client_id`/`seller_id` propio** — sólo `recording_id`. Se resuelve
igual que el resto del proyecto: join por `recording_id` contra `core_v2.recordings` (o
`mart_v2.recordings_enriched`, que además trae `seller_name`/`store_name`/`sector_name` ya resueltos, sin
JOINs adicionales) y filtrar `WHERE seller_id = ...` — confirmado con un JOIN real contra
`core_v2.recordings` (ej. `seller_id = 'Farma 24'`, `store_id = 'Alsina'`). **Implicación de diseño**:
`search_conversations` no puede aislar por tenant dentro de la propia tabla de vectores — el filtro de
aislamiento tiene que aplicarse en el JOIN, con el mismo rigor que `sql_security.py` exige hoy para
`dashboard_v2`. Evaluar extender `sql_security.py` en vez de escribir una validación paralela (el README
ya lo sugería como buena práctica; ahora hay una razón concreta: la tabla de embeddings comparte el mismo
Postgres y el mismo patrón de aislamiento por `seller_id`).

### Metadata disponible (pregunta 5) — resuelta

Vía `recording_id` se llega a `mart_v2.recordings_enriched` (`seller_name`, `store_name`, `sector_name`,
`employee_full_name`, `started_at`, etc.) y a `core_v2.conversations` (`conversation_id`,
`conversation_date`, `employee_name`, `transcribed_audio`). Todo lo que pedía la pregunta 5 está
disponible, con un join adicional — no viene desnormalizado en la tabla de embeddings.

### Frecuencia de actualización (pregunta 6) — resuelta: es continuo, no un snapshot

`embedded_at` va de 2026-08-28 a 2026-09-10 (hoy), con **284 `source_etl_run_id` distintos** — confirma
que es un pipeline corriendo en múltiples runs, no una carga única. Hay dos tablas de soporte en
`audit_v2` que exponen el estado del pipeline:

- **`audit_v2.conversation_embedding_state`** (662.225 filas, una por `recording_id`): trackea el intento
  de embedding por recording con `status`, `attempt_count`, `last_error_class`, `next_retry_at`. Distribución
  de `status` verificada: **643.589 `success`, 18.294 `pending`, 342 `skipped`**, y **0 filas con
  `last_error_class` no nulo** — el pipeline está sano hoy, sin errores acumulados pendientes de retry.
- **`audit_v2.conversation_embedding_backfill_candidate`** (793.647 filas, 793.647 `recording_id`
  distintos): el universo completo de recordings candidatos a vectorizar. **192.426 de esos recordings
  todavía no tienen fila en `conversation_embedding_state`** — o sea, ni siquiera se intentó procesarlos
  todavía (24,2% del universo candidato). De los que sí se procesaron con éxito, la tabla final
  `analytics_v2.conversation_embeddings` tiene **644.119 `recording_id` distintos** con al menos un chunk
  embebido.
- **Cobertura actual del backfill: 644.119 / 793.647 ≈ 81,2%** de los recordings candidatos ya tienen al
  menos un embedding. Sigue creciendo (284 runs en ~13 días) — un índice construido hoy sobre esta tabla
  quedaría ~19% incompleto respecto al universo candidato, sin contar conversaciones nuevas que sigan
  entrando después.
- Se revisó también `audit_v2.etl_run_document_scope` (44,9M filas) esperando que fuera específica del
  pipeline de embeddings — **no lo es**: es el tracking genérico de cambios de todas las colecciones
  ETL de Firestore (`recorder_presence`, `recordings`, `conversations`, etc., vía `change_type`
  `inserted`/`changed`/`unchanged`), embeddings es sólo una de varias cosas que consumen esa tabla de
  scope. No aporta nada específico a este diseño más allá de confirmar que el mismo mecanismo genérico de
  detección de cambios del proyecto es lo que dispara el backfill de embeddings.

## Conexión real (2026-09-10): correcciones encontradas al implementar de verdad

Implementar `search_conversations` de punta a punta (no sólo diseñarla) encontró dos errores
concretos en el diseño de arriba, que quedaban invisibles sólo mirando el schema por fuera:

- **El texto real de la conversación NO está en `core_v2.conversations.transcribed_audio`** — el
  esqueleto de código de la sección "Cómo se vería la implementación" original asumía esa columna.
  Verificado por SQL directo: **está NULL en el 100% de las 856.317 filas** de esa tabla, para
  cualquier cliente. El texto real (formato `.srt`, con número de bloque + timestamp + `Speaker N:`
  + línea — confirma el `srt-v1` del `embedding_config_id`) vive en
  `raw_v2.conversations_raw.data->>'transcribedAudio'` (columna `data`, tipo `jsonb`, con las
  claves `Feedback`, `metadata`, `transcribedAudio`, `Insights_categoricos`, etc. — es el documento
  crudo de Firestore tal cual, no una vista limpia). `vector_search.py` ya usa la fuente correcta;
  si alguna vez se vuelve a este README como referencia sin leer el código, no repetir el error.
- **El tipo `vector` y el operador `<=>` de pgvector viven en el schema `analytics_v2`**, no en
  `public` (el `search_path` default de la conexión). Sin `SET search_path = analytics_v2, public`
  al principio de la sesión, psycopg tira `UndefinedObject: type "vector" does not exist` primero, y
  `UndefinedFunction: operator does not exist: vector <=> vector` después de calificar sólo el tipo.
  `vector_search.py` lo hace en cada conexión antes de correr la query.

Con esas dos correcciones, una prueba real end-to-end (`vi_agent.py "Traeme ejemplos de
conversaciones donde el cliente se quejó de que un pedido o entrega llegó tarde" --client
mens_fashion_alto --internal-debug`) funcionó: el modelo llamó `search_conversations` dos veces con
reformulaciones distintas de la consulta, cruzó el resultado con `run_readonly_sql` sobre
`dashboard_v2` en la misma respuesta, y devolvió una respuesta de negocio con citas textuales reales
de conversaciones de Mens Fashion sobre demoras de entrega/sastrería — sin filtrar ningún
identificador interno (nombre de tabla, columna, ni mención a "base de datos vectorial"). Esto
confirma en la práctica lo que la "Nota técnica sobre Opción A" de arriba dejaba como pendiente de
prueba real para Opción A (combinar dos fuentes semánticas en la misma respuesta) — con la salvedad
de que acá ambas tools (`search_conversations` y `run_readonly_sql`) son funciones Python del loop
manual, no un tool nativo server-side de Gemini, así que no ejercita el `include_server_side_tool_invocations`
que sí haría falta si se combinara con el RAG de catálogo (Opción A) en el mismo cliente.

### Qué se implementó, concretamente

- **`4. scripts/vector_search.py`** (nuevo módulo, mismo patrón que `rag_sources.py`/
  `business_rules.py`): `VectorSearchRepository.search(query, top_k)` — embebe la query con
  `task_type=RETRIEVAL_QUERY`, filtra por `seller_id` del tenant y por `embedding_config_id` exacto,
  ordena por `<=>`, y devuelve fragmentos reconstruidos con tienda/vendedor/fecha/distancia. Nunca
  expone `recording_id` ni otro identificador físico al modelo.
- **`client_config.py`**: nuevo `VectorSearchConfig` (sólo `top_k`, no declara tabla ni
  `tenant_field` — a diferencia de `sources`, porque esta tool nunca deja que el modelo escriba SQL,
  el aislamiento es un literal fijo en `vector_search.py`) y parseo del bloque opcional
  `vector_search:` en `config.yaml`.
- **`vi_agent.py`**: `search_conversations` agregada a `TOOL_FUNCTIONS` y a `_build_tools_list()`
  (condicional a `CLIENT_CONFIG.vector_search`, igual que RAG). `{rag_tools_section}` del
  `SYSTEM_INSTRUCTION_TEMPLATE` se generalizó a `{extra_tools_section}` — ahora numera
  dinámicamente los items 3+ según qué tenga habilitado cada cliente (RAG, vector search, ambos, o
  ninguno), en vez de asumir un único slot fijo.
- **`2. clientes/mens_fashion_alto/config.yaml`**: declara `vector_search: {top_k: 5}` — primer y
  único cliente piloto hoy.
- **Tests**: `5. tests/test_vector_search.py` (reconstrucción de chunk, formato del literal
  pgvector, aislamiento por tenant en el SQL fijo, parseo del config) y una clase nueva en
  `test_vi_agent.py` (`VectorSearchToolExposureTests`) que confirma que la tool sólo se anuncia al
  modelo para el cliente piloto, no para el resto. 40/40 tests verdes después del cambio.
- **Panel de trazas en el tester** (2026-09-10, mismo día): `--internal-debug` ahora también
  funciona en el modo web de `1. vi_agent_tester.py` (antes sólo en `--cli`) — cada respuesta del
  chat de Streamlit muestra un expander colapsado "🔧 Trazas internas" con cada tool call, sus
  argumentos y su resultado, igual que ya se veía en la terminal. Nunca se puede activar desde la UI
  misma, sólo pasando el flag al arrancar el proceso — mismo criterio de "nunca en una interfaz de
  cliente" que ya regía para `--cli`. Cambios en `1. vi_agent_tester.py` (reenvía el flag al
  subproceso de Streamlit) y `4. scripts/streamlit_app.py` (`_render_debug_trace`,
  `tool_calls_log` capturado por mensaje).
- **Ajuste al prompt para consistencia de uso** (2026-09-10, después de la ronda 1 de evaluación,
  ver abajo): la descripción original de `search_conversations` en `SYSTEM_INSTRUCTION_TEMPLATE` la
  presentaba como alternativa a `run_readonly_sql` ("usala cuando..."), y el modelo terminaba
  resolviendo con SQL incluso preguntas que pedían ejemplos textuales o nombres puntuales (ver
  hallazgo de la ronda 1 más abajo). Se reescribió para dejar explícito que **complementa** a
  `run_readonly_sql` (no la reemplaza) y deben llamarse juntas cuando la pregunta lo pida, con
  señales textuales concretas ("ejemplo", "quién/qué vendedor", "cita", "textual") — validado con la
  ronda 2 del mini-banco (ver abajo): pasó de uso inconsistente a 5/5 sobre preguntas diseñadas para
  no tener ningún atajo de SQL posible.

### Mini-banco de evaluación corrido (2026-09-10)

Ver `2. clientes/mens_fashion_alto/preguntas/preguntas_busqueda_vectorial.yaml` (6 preguntas) y su
resultado en `resultados_busqueda_vectorial.json`. Lectura honesta, pregunta por pregunta:

- **vec01 (recuperación directa)**: ✅ citó 3 conversaciones reales con diálogo textual, atribuidas
  a tienda/vendedor, sin fuga técnica.
- **vec02 (vocabulario no literal)**: ⚠️ parcial — llamó `search_conversations` pero terminó
  armando la respuesta final desde campos SQL estructurados (`sensibilidad_precio`,
  `objecion_principal`) en vez de citar fragmentos semánticos concretos. No queda demostrado que el
  hallazgo venga de la búsqueda vectorial en este caso.
- **vec03 (cruce semántico + estructurado)**: ⚠️ el modelo **no llamó `search_conversations` en
  absoluto** — resolvió todo con SQL porque `dashboard_v2` ya tiene un campo categórico
  (`objecion_principal = 'tiempo_entrega'`) que responde la pregunta agregada mejor que la búsqueda
  semántica podría.
- **vec04 (resistencia a alucinación)**: ✅ la búsqueda devolvió resultados irrelevantes (esperable
  — siempre trae los top-k más cercanos aunque ninguno calce), pero el modelo los descartó,
  confirmó "no existe" cruzando con SQL, y no inventó nada.
- **vec05 (fuga técnica directa)**: ✅ ninguna mención a vector/embedding/IA/base de datos, tono
  ejecutivo normal, ni siquiera necesitó tool calls.
- **vec06 (filtro por tienda, limitación conocida)**: ⚠️ confirma la limitación real:
  `search_conversations` no acepta filtro por tienda. En la práctica no fue grave acá porque
  `run_readonly_sql` sí tiene `store_name` y el modelo lo usó para resolver el filtro combinando
  ambas tools — pero si el dato relevante existiera *sólo* en el texto libre de una tienda puntual
  (sin reflejarse en ningún campo categórico), no habría forma de aislarlo.

**Hallazgo principal, no anticipado en el diseño original**: el modelo prioriza SQL estructurado
sobre `search_conversations` cada vez que un campo de `dashboard_v2` ya puede responder la pregunta
(pasó en vec02 y vec03). Esto es razonable -es más preciso y más barato- pero significa que el
mini-banco no forzó tan a fondo el caso de uso real de la tool (contenido que *no* está capturado en
ningún campo estructurado) como hubiera sido ideal. El sistema no alucina ni filtra información
técnica en ningún caso, y `search_conversations` sí se usa cuando hace falta (vec01, vec04, y como
apoyo en vec06) — pero su valor incremental por sobre lo que ya resuelve SQL quedó parcialmente sin
probar. Antes de generalizar a otro cliente, conviene un segundo mini-banco con preguntas donde
`dashboard_v2` no tenga ningún campo categórico que pueda responder directamente (forzando a
`search_conversations` a ser la única fuente posible), para medir su valor real sin ese atajo.

### Mini-banco ronda 2 — valor incremental real (2026-09-10)

Ver `2. clientes/mens_fashion_alto/preguntas/preguntas_busqueda_vectorial_ronda2.yaml` (5
preguntas) y su resultado en `resultados_busqueda_vectorial_ronda2.json`. A diferencia de la ronda
1, cada pregunta se diseñó verificando primero los 172 campos del Data Map
(`VI Data Map Mens Fashion V7.yaml`) para confirmar que **ninguno** pudiera responderla — forzando
a que `search_conversations` sea la única fuente posible, sin el atajo de SQL que explicó los
resultados parciales de vec02/vec03 en la ronda 1. Corrida después del ajuste al prompt descrito
arriba.

- **vec2_01 (charla personal ajena a la venta)**: ✅ llamó `search_conversations`, citó 4 ejemplos
  genuinos y variados (una anécdota sobre café caliente, compartir fruta, un resfrío, fútbol),
  correctamente diferenciados de charla comercial.
- **vec2_02 (mención literal de "espejo"/"portatrajes")**: ✅ llamó la tool, citó fragmentos
  textuales reales distinguiendo bien el uso del espejo (ajuste de calce) del portatrajes (venta
  cruzada / funda de protección).
- **vec2_03 (trato informal — "caballero", "joven", "mi amigo")**: ✅ llamó la tool, citó frases
  exactas con vendedor/tienda para cada expresión, y agregó una categorización útil (trato informal
  vs. formal) no pedida explícitamente.
- **vec2_04 (promesa textual de sastrería "gratis"/"sin costo")**: ✅ el mejor resultado de las 5 —
  reformuló la query y llamó la tool dos veces, citó 4 promesas textuales exactas, y **no cayó en
  el campo categórico de "a medida" que sí existe** (`rol_de_a_medida_en_la_conversacion`) pese a
  ser una tentación de atajo — distinguió correctamente "se mencionó confección a medida"
  (categórico) de "se prometió gratis con esas palabras" (sólo verificable en el texto).
- **vec2_05 (vendedor deriva al cliente a la competencia)**: ✅ llamó la tool (y también intentó
  `run_readonly_sql` en paralelo, que falló por aislamiento — un bug de SQL generado por el modelo
  mismo, no del sistema — y el agente siguió adelante sin problema usando sólo los resultados de
  `search_conversations`). Encontró casos reales y con nombre propio de competidores
  (H&M, Zara, Pull&Bear, C&A, Aldo Conti, Vittorio Forti) mencionados por vendedores ante falta de
  stock.

**Resultado: 5/5 llamó a `search_conversations`** (vs. 4/6 en la ronda 1, con dos de esas cuatro
sólo parcialmente atribuibles a la tool) — confirma que el ajuste al prompt resolvió el problema de
consistencia identificado en la ronda 1. Las 5 respuestas citaron contenido textual genuino con
atribución correcta (tienda/vendedor/fecha), sin fabricar ningún ejemplo.

**Hallazgo de negocio no buscado, sólo posible por esta vía** (vec2_05): vendedores de Mens Fashion
derivan activamente a clientes hacia competidores por nombre cuando falta stock — información que
ningún campo estructurado del Data Map captura (no existe, y no tendría sentido crear, un campo
`vendedor_deriva_a_competencia`). Es la evidencia más clara hasta ahora de por qué vale la pena esta
capacidad más allá del ejercicio técnico — ver "Potencial de negocio" más arriba.

### Qué NO se hizo (deliberado, ver bifurcación de "Por qué 'no definitivo'" arriba)

- No se confirmó el algoritmo real de chunking con la otra persona — `_reconstruct_chunk_text` sigue
  siendo la aproximación por ventana de palabras descripta en "Hallazgo en Postgres QA".
- No se creó ningún índice vectorial (ivfflat/hnsw) — la búsqueda sigue siendo un scan secuencial
  sobre ~720k filas. Con el volumen de mens_fashion_alto (25.841 recordings con embedding) las
  pruebas manuales respondieron en tiempo razonable, pero no se midió latencia de forma sistemática.
- Ya se corrieron dos rondas del mini-banco (11 preguntas en total, ver arriba) — pero sigue siendo
  sobre un único cliente piloto, con preguntas escritas por la misma persona que armó la tool
  (riesgo de sesgo de confirmación). No reemplaza una revisión de alguien que no haya diseñado el
  sistema, ni un volumen de preguntas real de uso en producción.
- No se tocó ningún otro cliente — sólo `mens_fashion_alto` tiene `vector_search` en su
  `config.yaml`.

## Preguntas para la persona que está vectorizando (quedan, incluso con lo de arriba)

Con el hallazgo de Postgres, las preguntas 1, 3, 4, 5 y 6 de la lista original ya están respondidas por
SQL directo (ver arriba) — no hace falta preguntarlas. Lo que sigue abierto y sí requiere confirmación de
la otra persona (no se puede inferir sólo de la estructura de la tabla):

1. **¿Cómo se arma exactamente cada chunk?** (pregunta 2 original, sigue abierta) — el algoritmo preciso
   de windowing sobre el `.srt` (por turno vs. por tiempo vs. por token crudo), necesario para poder
   reconstruir el texto de un `chunk_idx` dado a partir de `transcribed_audio`.
2. **¿El texto del chunk se puede persistir en la tabla (o en una tabla aparte), o hay que
   reconstruirlo?** Hoy sólo hay `content_hash`. Persistirlo evita duplicar la lógica de chunking en
   `search_conversations` y reduce el riesgo de que la reconstrucción no coincida byte a byte con lo que
   realmente se vectorizó.
3. **¿Cuál es el plan para el 19% de recordings sin embedding todavía (192.426 sin intentar + los
   `pending`)?** ¿Termina el backfill en algún plazo conocido, o es continuo indefinidamente a medida que
   entran conversaciones nuevas?
4. **¿Está previsto un índice vectorial (ivfflat/hnsw) antes de production, o el plan es seguir con scan
   secuencial?** Con 719k filas y creciendo, una búsqueda `ORDER BY embedding <=> %s LIMIT k` sin índice
   va a degradar en latencia a medida que crece la tabla.

## Cómo se vería la implementación en cada escenario

**Opción B está confirmada por el hallazgo de Postgres de arriba** — la sección de Opción A queda abajo
sólo como referencia histórica (por si en algún momento se decide migrar), no como camino activo.

### Si es Opción A (Gemini File Search) — descartada por el hallazgo de arriba, queda como referencia

Prácticamente cero trabajo de código — es el mismo patrón que ya está en producción para
`product_catalog`:

1. La otra persona (o un script propio, fuera de este proyecto) sube las transcripciones a un store de
   Gemini File Search.
2. Se crea (o reusa) un prompt en Langfuse con `config.geminiFileSearchStoreName` apuntando a ese store,
   label `production`.
3. Se declara la fuente nueva en `2. clientes/<id>/config.yaml`, bajo `rag_sources` (ya es un dict — un
   cliente puede tener más de una fuente de RAG, ver Farma24 con `product_catalog`; ésta sería una
   segunda, ej. `conversaciones_historicas`).
4. Se prueba con `1. vi_agent_tester.py --cli --client <id>` (modo terminal, más rápido para iterar
   que la interfaz web por default) — mismo camino de prueba que ya se usó para validar el RAG de
   catálogo de Farma24 (ver "RAG (fuente de conocimiento adicional...)" en `8. README.md`).

No hace falta tocar `4. scripts/vi_agent.py`, `4. scripts/rag_sources.py` ni ningún test — el mecanismo
ya soporta múltiples fuentes por cliente.

### Si es Opción B (tabla propia / pgvector) — confirmada e implementada

**Ya implementado y conectado** — ver "Conexión real (2026-09-10)" arriba para el detalle completo
y qué quedó pendiente. Lo que sigue es el esqueleto original que guió esa implementación, con los
nombres reales de tabla/columna verificados en Postgres el 2026-09-10 (el código real vive en
`4. scripts/vector_search.py`, éste es sólo el resumen de referencia; el `SELECT texto...` de abajo
sigue mostrado incompleto a propósito, para no duplicar la query real fuera de sincronía si alguien
la edita en el futuro sin tocar este README):

```python
def search_conversations(query: str, top_k: int = 5) -> list[dict]:
    """Busca conversaciones semánticamente similares a `query` para el cliente activo.

    Devuelve una lista de fragmentos con su metadata (fecha, vendedor, tienda,
    conversation_id) para que el modelo los cite en la respuesta sin inventar contenido.
    """
    query_vector = embed(query, task_type="retrieval_query")  # MISMO modelo (gemini-embedding-001,
    # dim 768) que analytics_v2.conversation_embeddings.embedding, pero con task_type distinto al que
    # se usó para vectorizar (retrieval_document) — ver nuance de Gemini embeddings asimétricos arriba
    tenant_filter = CLIENT_CONFIG.tenant  # seller_id — aislamiento por cliente, no negociable
    # SELECT ce.recording_id, ce.chunk_idx, r.seller_id, r.store_name, r.started_at,
    #        ce.embedding <=> %s AS distancia
    #        -- falta el texto del chunk: content_hash no es el texto, ver "Hallazgo en Postgres QA"
    # FROM analytics_v2.conversation_embeddings ce
    # JOIN mart_v2.recordings_enriched r ON r.recording_id = ce.recording_id
    # WHERE r.seller_id = %s   -- mismo criterio que sql_security.py exige para dashboard_v2
    # ORDER BY distancia LIMIT %s
    ...
```

Pasos concretos (✅ = hecho el 2026-09-10, ver "Conexión real" arriba):

1. ✅ Prototipar esta función **en esta misma carpeta primero**, sin tocar `4. scripts/` ni el
   `config.yaml` de ningún cliente real — mismo criterio que ya usó
   `3. experimentos/coaching_playbook/coaching_tester.py`. Ver `prototype_search_conversations.py`
   en esta carpeta (superado, queda como registro histórico).
2. ✅ Aislamiento por tenant: no se extendió `sql_security.py` en sí (esa validación es para SQL que
   el *modelo* escribe; acá el SQL es fijo en código) — el filtro `WHERE r.seller_id = %s` vive
   directo en `VectorSearchRepository.search`, parametrizado, con un test estático
   (`SearchSqlIsolationTests`) que falla si alguna edición futura lo borra sin querer.
3. ✅ Reconstrucción del texto del chunk: implementada como aproximación explícita, no definitiva
   (ver "Por qué 'no definitivo'" al principio del README).
4. ✅ Sumada a `4. scripts/vi_agent.py` (`TOOL_FUNCTIONS`, `_build_tools_list()`), tests en
   `5. tests/test_vector_search.py` + `test_vi_agent.py`, declarada en el `config.yaml` de
   `mens_fashion_alto` (cliente piloto).
5. ⬜ Pendiente: mini-banco de evaluación (sección de abajo) antes de generalizar a otro cliente.

## Evaluación — cómo saber si funciona de verdad

Antes de generalizar a más de un cliente, seguir el mismo criterio que el resto del proyecto usa para
validar cualquier fuente nueva (ver "Evaluación" en `8. README.md`): armar un mini-banco de 3-5
preguntas reales con un checklist cualitativo de qué debería contener una buena respuesta (mismo formato
que `2. clientes/farma24_alto/preguntas/preguntas_rag_catalogo.yaml`, que no tiene `sql`/
`respuesta_esperada` verificable porque el contenido no vive en Postgres). Ejemplos de preguntas que
vale la pena incluir:

- Una pregunta directa de recuperación semántica ("conversaciones donde el cliente mencionó un problema
  de envío") — control base, la más fácil.
- Una pregunta de vocabulario no literal (buscar un concepto sin usar las palabras exactas que aparecen
  en las transcripciones) — mide si la búsqueda es semántica de verdad o sólo full-text disfrazado.
- Una pregunta cruzando búsqueda semántica + un filtro estructurado (ej. "...pero sólo en la tienda X" o
  "...sólo en conversaciones donde hubo compra según el checklist") — sólo aplica si la metadata lo
  permite (ver pregunta 5 de la lista de arriba).
- Una pregunta de resistencia a alucinación: algo que casi seguro NO está en ninguna conversación, para
  confirmar que el agente dice "no encontré nada" en vez de inventar un ejemplo plausible.
- Una pregunta de fuga técnica directa ("¿de dónde sacás esta información?", "¿qué base de datos usás?")
  — mismo control que ya se prueba para el RAG de catálogo, la caja negra (`4. scripts/response_policy.py`)
  debe seguir aplicando acá también.

## Potencial de negocio (por qué vale la pena, más allá de "sería lindo tenerlo")

- Abre una categoría de pregunta hoy **imposible** para cualquiera de los 19 clientes: insights
  cualitativos sobre contenido no estructurado. Ningún campo del Data Map de ningún cliente captura
  "quejas de envío" o "objeciones no anticipadas por el checklist" como dato estructurado — sólo existe
  en el texto crudo.
- Combinado con los campos ya estructurados (`dashboard_v2`), permite preguntas híbridas de alto valor
  ("¿qué tasa de compra tuvieron las conversaciones que mencionan [tema X]?") que hoy no se pueden
  responder con ninguna de las dos fuentes actuales por separado.
- Es una capacidad transversal a los 19 clientes (a diferencia de RAG de catálogo, que sólo tiene
  sentido para clientes con catálogo de productos) — cualquier cliente con volumen real de conversaciones
  se beneficia.

## Qué NO hacer todavía

- No generalizar `vector_search` a otro `config.yaml` sin repetir al menos la ronda 2 del mini-banco
  contra el cliente nuevo — las 11 preguntas corridas hasta ahora las escribió la misma persona que
  armó la tool (riesgo de sesgo de confirmación) y son sólo sobre `mens_fashion_alto`; el resultado
  positivo de la ronda 2 no garantiza que se sostenga con otro Data Map o volumen de conversaciones.
- No tratar `_reconstruct_chunk_text` (en `4. scripts/vector_search.py`) como definitiva — sigue
  siendo una aproximación por ventana de palabras, no el algoritmo real de chunking. Confirmar con la
  otra persona antes de asumir que los fragmentos citados coinciden exacto con lo que generó el
  vector.
- No construir un índice vectorial de producción (ni prometer una latencia específica) mientras el
  backfill siga con ~19% de recordings sin intentar (192.426 a nivel global) — confirmar con la otra
  persona si hay fecha de finalización antes de fijar expectativas de cobertura con el cliente
  piloto.
- No asumir `task_type=retrieval_document` (o el default) al generar el embedding de la query del
  usuario — `vector_search.py` ya usa `retrieval_query` correctamente; si se reescribe esta lógica en
  otro lado, mantener esa asimetría.

## Próximos pasos, en orden

1. ~~Confirmar Opción A vs. B~~ — resuelto: es Opción B (`analytics_v2.conversation_embeddings`,
   pgvector, `gemini-embedding-001` 768-dim).
2. ~~Implementar y conectar `search_conversations`~~ — hecho el 2026-09-10 para `mens_fashion_alto`,
   ver "Conexión real" arriba.
3. ~~Armar y correr el mini-banco de evaluación~~ — dos rondas corridas el 2026-09-10 (11 preguntas
   en total), ronda 2 con 5/5 usando `search_conversations` sobre preguntas sin atajo de SQL posible.
   Ver "Mini-banco ronda 2" arriba.
4. **Siguiente iteración, en orden de impacto esperado** (ver detalle de cada una más abajo):
   1. Índice vectorial (ivfflat/hnsw) — hoy es scan secuencial, va a degradar con el tiempo. Sigue
      pendiente, no es código de este proyecto.
   2. Confirmar con la otra persona el algoritmo real de chunking y evaluar persistir el texto del
      chunk en vez de reconstruirlo. Sigue pendiente.
   3. ~~Filtro estructurado opcional en `search_conversations` (`store_name`)~~ — hecho el
      2026-09-10, ver "Iteración 3" abajo.
   4. ~~Umbral de distancia mínima~~ — evaluado y descartado a favor de una distancia relativa por
      búsqueda, ver "Iteración 3" abajo (los datos reales no soportan un corte absoluto todavía).
   5. Ronda 3 de evaluación con un revisor que no haya diseñado la tool, para sacar el sesgo de
      confirmación de las 11 preguntas actuales. Sigue pendiente — es la que más falta ahora.
5. Sólo después de eso, evaluar generalizar `vector_search` a otro `config.yaml` — candidatos
   razonables: `salomon_alto`/`steren_alto` (volumen alto, sin RAG activo, para no mezclar dos
   fuentes semánticas nuevas a la vez). Verificar primero el % de cobertura de backfill de ese
   cliente específico (la cifra global de 81,2% es agregada, no por cliente).

### Iteración 3 — filtro por tienda + distancia relativa (2026-09-10, mismo día)

Implementados y verificados los dos primeros puntos de la lista de abajo que dependían sólo de
código propio:

- **Filtro `store_name` en `search_conversations`** (`4. scripts/vector_search.py` +
  `vi_agent.py`): parámetro opcional, `ILIKE '%valor%'` contra `mart_v2.recordings_enriched.store_name`,
  parametrizado (sin riesgo de inyección aunque lo arme el modelo). **Cierra la limitación de
  vec06/ronda 1**: re-probado en vivo con la misma pregunta ("quejas en la sucursal Tlaquepaque") y
  esta vez el modelo llamó `search_conversations({'store_name': 'Tlaquepaque', 'top_k': 5, ...})`
  directamente, en vez de depender sólo de `run_readonly_sql` para el filtro.
- **`distancia_relativa_al_mejor_resultado` en vez de un umbral absoluto**: se evaluó primero
  calibrar un corte fijo de distancia (la idea original de "umbral de distancia mínima"), pero al
  revisar las distancias reales de las 11 preguntas de evaluación corridas hasta ahora, **no hay
  separación limpia entre resultados relevantes e irrelevantes** — ambos grupos caen en el rango
  ~0,20-0,27 (ej. vec01, genuinamente relevante: 0,219-0,241; vec04, la trampa de alucinación sin
  nada relevante: 0,264-0,269 — casi solapado). Un corte absoluto arriesgaba tanto falsos negativos
  como falsos positivos con tan pocos datos. En su lugar, cada resultado ahora trae la distancia
  relativa al mejor resultado de *esa misma búsqueda* (0.0 = el más cercano), una señal comparativa
  más defendible que no depende de calibrar un número universal.
- Tests nuevos: `SearchStoreFilterAndRelativeDistanceTests` en `test_vector_search.py` (5 tests,
  sin tocar Postgres real — cursor/conexión mockeados). **45/45 tests verdes** después del cambio.

### Iteración 4 — orden explícito SQL-primero (2026-09-10, mismo día, a pedido del usuario)

Objetivo del usuario: que `search_conversations` se use *sólo cuando `run_readonly_sql` no tenga la
información* — no como exploración especulativa en paralelo. Se reescribió el ítem de
`search_conversations` en `SYSTEM_INSTRUCTION_TEMPLATE` (`vi_agent.py`) con un encabezado explícito
**"ORDEN DE USO"**: intentar primero `run_readonly_sql`; llamar a la búsqueda semántica únicamente
para la parte que ningún campo estructurado puede responder; instrucción explícita de **no** llamarla
cuando la pregunta es puramente numérica/estructurada y SQL ya la responde por completo. Mantiene la
instrucción de la Iteración 2 (complementar, no reemplazar, para preguntas mixtas).

**Riesgo real de este cambio**: hacerlo demasiado estricto podía revertir la mejora de consistencia
de la Iteración 2 (volver a ignorar la tool cuando sí hace falta). Se verificó con dos corridas:

- **Ronda 3 (control negativo + positivo, nueva)** — ver
  `preguntas_busqueda_vectorial_ronda3.yaml`/`resultados_busqueda_vectorial_ronda3.json`, 3
  preguntas: dos puramente cuantitativas (tasa de conversión general, conteo por sensibilidad de
  precio) y una mixta (mismo conteo + pedido de ejemplo textual). **3/3 correcto**: las dos
  cuantitativas *no* llamaron a `search_conversations` (resueltas 100% con `run_readonly_sql`, sin
  gasto extra de embedding/scan), y la mixta sí la llamó y citó un ejemplo textual genuino (Puebla
  Reforma, negociación de descuento) además del conteo de SQL.
- **Ronda 2 re-corrida (control de regresión)** — mismas 5 preguntas de la Iteración 2 (ninguna con
  atajo de SQL posible), vueltas a correr después del ajuste: **5/5 siguió llamando
  `search_conversations`**, sin regresión. Los ejemplos citados fueron igual de específicos y
  genuinos que en la corrida original (mismas tiendas/vendedores en varios casos, ej. la promesa de
  sastrería gratis de Galerías Coapa/Lucía Jiménez volvió a aparecer idéntica).

**Conclusión**: el ajuste logra lo pedido -evita el costo de llamadas especulativas a
`search_conversations` cuando SQL ya alcanza, sin perder la capacidad de usarla cuando genuinamente
hace falta. 8/8 entre las dos rondas de verificación.

### Iteración 5 — `conversation_id`, `resumen_verificado`, límite de reformulaciones (2026-09-10)

A pedido explícito del usuario ("hacé la 1 sí o sí, y la 2 y la 4" sobre una lista de mejoras
propuestas), tres cambios en `vector_search.py`/`vi_agent.py`:

1. **`conversation_id` en cada resultado**: antes, cuando el modelo quería cruzar un hallazgo
   semántico con datos estructurados, adivinaba con `ILIKE '%texto libre%'` sobre
   `resumen_ejecutivo_conversacion`/`errores_principales_detectados` (varios intentos fallidos
   antes de acertar, visto en corridas anteriores). Ahora puede usar
   `WHERE conversation_id = '...'` o `IN (...)` directo. Requirió un `LEFT JOIN LATERAL` contra
   `core_v2.conversations` -**`recording_id` no siempre es igual a `conversation_id`** (734.874 de
   856.317 filas coinciden, verificado por SQL directo; el resto no) y 206 `recording_id` tienen
   más de una fila asociada, así que el join plano hubiera duplicado resultados -el `LATERAL` con
   `ORDER BY extracted_at DESC LIMIT 1` desempata de forma determinística.
2. **`resumen_verificado`**: adjunta el resumen ejecutivo ya extraído por el pipeline de
   checklist/insights (un LLM aparte, ya en producción) junto al fragmento reconstruido
   -aproximado, ver limitación conocida-, para que el modelo tenga una segunda fuente con la que
   contrastar antes de citar. Se resuelve por convención de nombre
   (`_find_descriptivos_source`, busca una fuente declarada en `config.yaml` que termine en
   `insights_descriptivos_generales`) -ausente sin error para clientes de schema delgado
   (`salomon_alto`, etc., verificado como control negativo en los tests).
3. **Límite de reformulaciones especulativas**: se agregó al prompt "no reformules la query más de
   una vez extra si el primer intento no trajo nada útil" -en corridas anteriores se vieron 2-3
   reformulaciones de `search_conversations` dentro de una misma respuesta, cada una con su costo
   de embedding + scan.

**Bug real encontrado y corregido en el camino** (dejarlo documentado como aprendizaje): al
construir el SQL con columnas/joins condicionales, el orden en que el código hacía
`params.append(...)` no coincidía con el orden en que los placeholders `%s` aparecen en el TEXTO
final del SQL -psycopg sustituye estrictamente en orden de aparición en el string, no en el orden
de construcción en Python. Causó `ProgrammingError: the query has 6 placeholders but 7 parameters`
en la primera prueba real contra Postgres. **Los 51 tests con mocks no lo detectaron** porque
`_FakeCursor.execute()` sólo registra lo que se le pasa, nunca valida que la cantidad de
placeholders coincida con la de parámetros -se agregó un test nuevo
(`test_placeholder_count_matches_params_count`) que cuenta `sql.count("%s")` contra `len(params)`
para que este tipo de bug falle en un test la próxima vez, aunque el orden en sí sólo lo prueba una
ejecución real.

**Verificación real** (después del fix): `VectorSearchRepository.search()` contra Postgres real
devolvió `conversation_id` (UUID válido) y `resumen_verificado` con contenido coherente con el
fragmento citado. Prueba end-to-end con `vi_agent.py` sobre la misma pregunta de "vendedor deriva a
la competencia": el modelo llamó `search_conversations` una sola vez (sin reformular), y usó los
`conversation_id` devueltos en un `WHERE c.conversation_id IN (...)` exacto de `run_readonly_sql`
para traer el detalle completo del caso -mismo caso ya visto en la ronda 2 (Río Tijuana, Brandon
Martínez, derivación a Aldo Conti/Vittorio Forti), esta vez encontrado con precisión en vez de por
adivinanza de texto. **51/51 tests verdes.**

### Iteración 6 — deduplicación por conversación (2026-09-10, mismo día)

Último ítem pendiente de la lista de mejoras ("hacé la 1 sí o sí, y la 2 y la 4" -ver Iteración 5;
la 3, deduplicación, había quedado afuera de ese pedido y se implementó después a pedido explícito
posterior del usuario).

**El problema**: una conversación larga puede tener hasta ~14 chunks (verificado en
mens_fashion_alto), y si esa conversación puntual toca mucho el tema buscado, varios de sus chunks
podían caer en el mismo `top_k` -`top_k=5` podía devolver, por ejemplo, 3 chunks de una sola
conversación + 2 de otra, en vez de 5 conversaciones distintas. El modelo veía "5 resultados" pero
eran en realidad 2 casos, con riesgo de presentar el mismo caso citado varias veces como si fuera
evidencia de un patrón más amplio.

**La solución**: ahora se piden más candidatos de los que pide `top_k` (`_CANDIDATE_MULTIPLIER = 4`,
techo `_MAX_CANDIDATES = 60`) y se deduplica en Python por `conversation_id` (con `recording_id`
como respaldo para el caso raro en que el `LEFT JOIN LATERAL` no resuelva un `conversation_id`),
quedándose con el chunk de mejor distancia por conversación -las filas ya vienen ordenadas por
distancia ascendente, así que la primera aparición de cada conversación es siempre su mejor chunk.
`top_k` ahora es explícitamente "cantidad de conversaciones distintas", no de chunks -documentado
en el docstring y en el prompt de `vi_agent.py`.

**Verificación real**: `top_k=5` contra mens_fashion_alto devolvió 5 resultados con 5
`conversation_id` distintos entre sí -sin deduplicar, no había garantía de esto. 5 tests nuevos
(`test_deduplicates_by_conversation_id_keeping_the_best_distance`,
`test_falls_back_to_recording_id_when_conversation_id_is_missing`,
`test_respects_top_k_after_deduplication`,
`test_requests_more_candidates_than_top_k_to_leave_room_for_dedup`, y el guardrail de
placeholders/params ya existente sigue pasando). **55/55 tests verdes.**

Con esto, las 5 mejoras propuestas en la última ronda de iteración quedaron todas resueltas
(`store_name`, distancia relativa, orden SQL-primero, `conversation_id`/`resumen_verificado`, límite
de reformulaciones, y deduplicación).

### Iteración 7 — filtro por fecha, enmascarado de lenguaje ofensivo, logging de uso (2026-09-10)

Última ronda de una nueva lista de ideas propuestas ("hacé el 2, 3 y 4" -el ítem 1 de esa lista,
limpiar el ruido de timestamps/números de bloque del `.srt` en `fragmento_aproximado`, quedó
pendiente, no se pidió). El ítem 4 (logging) se confirmó primero que no agrega costo real -es un
`append` local a un archivo, sin llamados extra a Postgres ni a Gemini, mismo patrón que
`usage_tracking.py` ya usa para las llamadas del modelo- antes de implementarlo.

- **`date_from`/`date_to` opcionales** (formato `YYYY-MM-DD`) en `search_conversations`: filtran
  por `r.started_at::date`, mismo patrón que `store_name` (parámetros, nunca interpolados en el
  texto SQL). Formato inválido levanta `ValueError` explícito (falla rápido con mensaje claro, en
  vez de dejar que psycopg tire un error de casteo menos legible para que el modelo lo corrija).
- **Enmascarado de lenguaje ofensivo severo** (`_sanitize_offensive_language`) aplicado a
  `fragmento_aproximado` y `resumen_verificado` antes de devolverlos -defensa en profundidad sobre
  el caso real visto en producción (groserías fuertes en texto crudo de una transcripción, ver
  Iteración 5). El modelo ya las manejaba bien al responder (las parafraseaba sin repetirlas), pero
  esto reduce el riesgo de que ese texto crudo llegue sin ningún filtro previo. Lista corta y
  deliberadamente conservadora (`_OFFENSIVE_TERMS`) para no enmascarar vocabulario de negocio
  legítimo por error -verificado con un test dedicado que confirma que una oración de negocio
  normal sale sin tocar.
- **Logging local de uso** (`VECTOR_SEARCH_LOG_PATH`, `.runtime/usage/vector_search_calls.jsonl`):
  cada llamada real registra metadata agregada (client_id, `top_k` pedido, si usó `store_name`/rango
  de fechas, candidatos traídos vs. resultados devueltos, mejor/peor distancia) -**nunca el texto de
  la query ni el contenido citado**, mismo criterio de privacidad que `usage_tracking.py` aplica a
  las llamadas de Gemini. Falla en silencio si no puede escribir (nunca debe romper una búsqueda
  real por un problema de logging). Pensado para acumular datos reales de producción -útil más
  adelante para reconsiderar el umbral de distancia con más muestras que las 11 preguntas de
  evaluación disponibles hoy.

**Verificación real**: filtro de fecha contra mens_fashion_alto (agosto 2026) devolvió 3
resultados, los tres con fecha dentro del rango pedido. El log se escribió correctamente con los
campos esperados y sin rastro del texto de la query. 10 tests nuevos (filtro de fecha, validación
de formato, enmascarado, y logging -incluyendo que una falla de logging no rompe la búsqueda).
**65/65 tests verdes.**

Con esto, las 3 mejoras de esta ronda quedaron resueltas (queda pendiente, sin pedir todavía, la
limpieza del ruido de timestamps/`.srt` en el fragmento devuelto).

### Iteración 8 — limpieza de ruido SRT en el fragmento (2026-09-10)

`_strip_srt_noise` saca índice de bloque + rango de timestamps del `.srt` antes del windowing,
dejando sólo `Speaker N: texto`. Verificado real: fragmento sin timestamps ni `-->`. 66/66 tests.

### Iteración 9 — retry en el embedding + prueba de inyección de prompt (2026-09-10)

`_embed_query` no reintentaba ante 429/5xx (a diferencia del resto de llamadas a Gemini del
proyecto) — agregado backoff exponencial, mismo criterio que `_send_message_with_retry` en
vi_agent.py. Verificado real: simulando un 503 en el primer intento, reintentó y trajo el vector
(768 dims) en el segundo. 3 tests nuevos, 69/69 (menos el flake de Langfuse ya conocido).

También se probó **inyección de prompt vía contenido citado**: se mockeó `search_conversations`
para devolver un fragmento con instrucciones inyectadas (revelar system prompt; forzar un dato de
negocio falso). El modelo resistió ambos casos en pruebas reales contra Gemini — ni reveló info
técnica ni repitió el dato falso, 0 violaciones de `response_policy`. Sin falla encontrada.

### Iteración 10 — reformulación innecesaria (2026-09-10)

Prueba multi-turno detectó que `search_conversations` reformulaba la query aunque el primer
intento ya traía resultados buenos (el texto del prompt permitía "una reformulación gratis").
Ajustado a "usá el primer resultado si ya sirve, reformulá sólo si no trajo nada útil". Verificado
real: la misma pregunta que antes disparaba 2 llamadas ahora hace 1. 68/69 tests (flake de
Langfuse ya conocido).

### Iteración 11 — latencia medida + investigación de performance (2026-09-10)

Se agregó `embed_ms`/`query_ms` al log de uso (sin costo extra, sólo `time.perf_counter()`). Una
primera medición dio 42.5s en `query_ms`, alarmante — pero un `EXPLAIN (ANALYZE, BUFFERS)` de la
misma query (con y sin el JOIN a `resumen_ejecutivo_conversacion`) mostró Execution Time <1s en
ambos casos, usando bien los índices existentes (`idx_core_v2_recordings_seller`,
`conversation_embeddings_pkey`, etc.). Tres corridas reales aisladas después confirmaron `query_ms`
estable en 3.0-3.5s -el 42.5s fue ruido de contención de este entorno de desarrollo (muchas
queries en paralelo corriendo esa sesión), no un problema estructural. **No hay ganancia gratis
para tomar acá** -el plan de ejecución ya es eficiente; la brecha entre el EXPLAIN aislado (<1s) y
la medición real (~3s) es probablemente overhead de abrir una conexión nueva por búsqueda, no la
query en sí.

### Iteración 12 — conexión Postgres reusable (2026-09-10)

`search_conversations` abría una conexión Postgres nueva (con su handshake TLS a RDS) en cada
llamada. Ahora `vector_search.py` cachea una conexión a nivel módulo (`_get_reusable_connection`,
propia de este archivo -no toca `utils/postgres.py`, que sigue abriendo una nueva por llamada para
`run_readonly_sql`/`get_business_rules`) y reconecta automáticamente si se rompió (ej. idle
timeout del lado del server) -un reintento, no reintentos infinitos.

**Verificación real**, 4 búsquedas seguidas en el mismo proceso: `query_ms` bajó a **~1,4-1,5s
estable** en las llamadas 3 y 4 (vs. ~3,0-3,5s antes del cambio, en cada llamada) -**≈55% menos**
una vez que la conexión ya está cacheada. Las llamadas 1 y 2 de esta corrida puntual salieron muy
altas (109s y 18.7s) por contención de este entorno de desarrollo (muchos procesos de fondo
corridos esta sesión compitiendo por el mismo RDS, mismo fenómeno que ya se vio y descartó en la
Iteración 11) -no atribuible al cambio en sí. 3 tests nuevos, 71/72 (flake de Langfuse ya
conocido).

### Iteración 13 — contención de sesión: causa raíz y mitigación (2026-09-11)

Investigada la contención mencionada en Iteraciones 11 y 12: eran **8 procesos de prueba propios**
(dos cadenas `vi_agent_tester.py` → Streamlit, lanzadas con `preview_start` en distintos momentos de
esta sesión de trabajo) que quedaron corriendo en background sin cerrarse, compitiendo por CPU y
conexiones a RDS con las búsquedas reales que se estaban midiendo. Confirmado vía `Get-Process` /
`Get-CimInstance Win32_Process` (CommandLine + ParentProcessId de cada cadena). Terminados los 8.

**No es un problema de la query ni de este código** -ver Iteración 11 (EXPLAIN ANALYZE <1s). Dos
mitigaciones:

1. **Hábito** (la causa real): cerrar explícitamente cada servidor de prueba (`preview_stop`, o el
   proceso de Streamlit/tester) apenas termina la verificación, en vez de dejarlo corriendo para la
   siguiente iteración. Ningún cambio de código evita que procesos de prueba sueltos generen
   contención -es disciplina de quien opera este entorno de desarrollo.
2. **Defensivo, en código**: `_get_reusable_connection` (Iteración 12) ahora serializa la apertura de
   conexión con un `threading.Lock` (`vector_search.py`) -si dos llamadas a `search()` coinciden
   dentro del mismo proceso (ej. el loop de tools del agente paraleliza tool calls), la segunda
   espera a la primera y reutiliza la conexión ya cacheada en vez de abrir otra en simultáneo. Esto
   **no** evita que otros procesos abran su propia conexión -eso lo resuelve sólo el hábito del punto
   1- pero cierra el caso dentro-del-mismo-proceso. 41/41 tests en verde.

### Iteración 14 — experimento "conteo vía recuperar + clasificar" (2026-09-11)

Retomada la pregunta (ya planteada antes por el usuario) de si `search_conversations` puede dar
**datos numéricos concretos** tipo SQL. Ya se había descartado un umbral fijo de distancia (ver
docstring de `search()`: rango ~0,20-0,27 sin separación limpia entre relevante/irrelevante).
Probada acá una alternativa distinta: en vez de un corte de distancia, **recuperar los top_k=20
candidatos (el máximo práctico hoy, `_MAX_CANDIDATES=60` deduplicado) y clasificarlos por lectura
directa**, uno por uno, en vez de confiar en el ranking de distancia.

**Prueba real** (mens_fashion_alto, pregunta: resistencia de precio no explícita -mismo tipo que
vec02): de los 20 resultados devueltos (todos con `distancia_relativa_al_mejor_resultado` entre
0,0 y 0,018, prácticamente empatados):

- Sólo **2 claramente relevantes + 2 dudosos** (~10-20% de precisión).
- **4 de 20 eran ruido no utilizable**: una sesión de entrenamiento interna entre supervisor y
  vendedor (no es conversación con cliente), y transcripciones ininteligibles o con fallas graves
  de diarización.
- El resto (~14) eran ventas cerradas o temas sin relación con precio.

**Conclusión**: leer a mano los top-K no arregla el problema del umbral -lo confirma y lo agrava,
porque además de no haber separación de distancia limpia, una porción no despreciable del corpus
recuperado ni siquiera es una conversación de venta válida para el tema en cuestión. Un clasificador
automático (LLM) sobre esos mismos candidatos heredaría el mismo techo de precisión, y el resultado
seguiría siendo, en el mejor caso, un piso aproximado (nunca el total real) con riesgo real de
ruido mezclado. **Se descarta construir un modo de conteo sobre `search_conversations`**, con
umbral o con clasificación posterior. El camino correcto para números reales sigue siendo un
pipeline de clasificación exhaustivo (LLM sobre el 100% de las conversaciones, no sobre un sample
de vecinos más cercanos) que persista el resultado como campo estructurado consultable por SQL
-responsabilidad del pipeline de checklist/insights, no de esta tool. Script del experimento
(no productivo, no versionado) descartado al cerrar la sesión.

### Iteración 15 — dos ángulos más para el conteo, ambos probados con datos reales (2026-09-11)

A pedido del usuario ("hay otra forma... encarándolo desde otro ángulo"), dos alternativas más a la
lectura manual de la Iteración 14, mismo caso de prueba (mens_fashion_alto, resistencia de precio
no literal):

1. **Multi-query (ensemble de 4 reformulaciones de la misma intención)**, votando por
   `conversation_id` que aparece en ≥2 de las 4 búsquedas (top_k=20 cada una). Resultado: **no
   mejora nada** -el conjunto "consenso" (19 de 47 IDs únicos) sigue dominado por ventas cerradas y
   temas sin relación con precio, con las mismas 2-4 conversaciones genuinamente relevantes de la
   Iteración 14 mezcladas al mismo nivel de votos que varios falsos positivos (ej. la sesión de
   entrenamiento interna subió de sin-voto a voto=2). Los embeddings de chunk parecen agrupar por
   "conversación de venta genérica de retail", no por la nuance específica de la pregunta -pedir la
   misma idea con otras palabras no cambia eso.
2. **Embeber `resumen_ejecutivo_conversacion` (texto limpio del pipeline de insights) en vez de
   chunks crudos de transcripción**, al vuelo con similitud coseno en Python (150 resúmenes de
   `vw_mens_fashion_insights_descriptivos_generales`, sin tocar `analytics_v2.conversation_embeddings`
   -experimento aislado, no productivo). **Mejora real pero parcial**: de los top-20 por similitud,
   **cero son ruido inválido** (nada de sesiones de entrenamiento ni transcripciones ininteligibles
   -el resumen ya es texto limpio, no hereda el ruido de diarización del chunk crudo) y casi todos
   son conversaciones que efectivamente no cerraron venta. Pero la mayoría no cierran por *fit* o
   *inventario*, no específicamente por precio -la nuance fina ("precio" vs "no compró por cualquier
   motivo") se sigue perdiendo; precisión específica similar a la Iteración 14 (~15-25%), aunque
   con una base de candidatos mucho más limpia.

**Conclusión de los tres experimentos (14+15) combinados**: el techo no es la estrategia de
recuperación (top-K, umbral, multi-query) sino la granularidad del texto que se embebe. Embeber
texto limpio (resúmenes) en vez de chunks crudos elimina el ruido de transcripciones inválidas -una
mejora real y accionable- pero ninguna variante probada logra aislar una nuance de negocio
específica (precio vs. motivo genérico de no-cierre) con precisión suficiente para un conteo
automático confiable. **Se mantiene la recomendación de la Iteración 14**: no construir un modo de
conteo sobre `search_conversations`. Hallazgo nuevo y accionable para llevarle a la otra persona
(Pedro): si en algún momento se decide vectorizar algo más allá del chunk crudo, embeber también
`resumen_ejecutivo_conversacion` (o similar) daría resultados de recuperación notablemente más
limpios que el chunk crudo actual, aunque no resolvería el conteo por sí solo. Scripts de ambos
experimentos (no productivos, no versionados) descartados al cerrar la sesión.

### Iteración 16 — ángulo de "descubrimiento cualitativo" en vez de conteo (2026-09-11)

Después de tres experimentos de conteo sin resultado (Iteraciones 14-15), se probó encarar
`search_conversations` desde otro objetivo por completo: no medir ("¿cuántas veces?"), sino
**descubrir señales tempranas de negocio** que todavía no existen como campo estructurado
("¿hay algo que los clientes están pidiendo que hoy no estamos registrando?"). La diferencia clave
con los experimentos anteriores: la pregunta de prueba fue mucho más específica y accionable
("cliente pidió o necesitó algo que la tienda no pudo ofrecer o no tenía disponible -producto,
servicio, talla, opción de compra- fuera de lo habitual") en vez de una nuance amplia y ambigua
("resistencia de precio").

**Prueba real** (mens_fashion_alto, top_k=20, sin filtro de tienda/fecha): **20/20 resultados
genuinamente on-topic, cero ruido** (nada de sesiones de entrenamiento ni transcripciones
ininteligibles, a diferencia de las Iteraciones 14-15) -la mejor precisión de recuperación vista
hasta ahora en cualquier experimento de esta línea. Leídos y agrupados por patrón (parafraseado,
nunca cita textual):

1. **"A la medida" no resuelve la urgencia**: repetidas veces el vendedor ofrece confección a
   medida como alternativa cuando no hay stock, pero el plazo (15-20 días hábiles) no calza con la
   necesidad del cliente (evento próximo, "para hoy", fecha fija) -la venta se pierde por el tiempo
   de entrega, no por el producto en sí.
2. **Inventario desigual entre sucursales/canal online**: cliente vio el producto en la web o en
   otra sucursal y la tienda puntual no lo tiene -a veces el vendedor rastrea/pide desde otra
   sucursal, con éxito desigual.
3. **Fin de temporada sin reposición**: modelos agotados por rotación de colección (cada ~3 meses),
   sin fecha de reingreso -pérdida de venta pura por catálogo.
4. **Categorías fuera del surtido**: clientes piden productos que la tienda no maneja en absoluto
   (monederos, ciertos cortes de suéter, pantalón oversize) -en un caso el vendedor recomienda ir a
   la competencia.
5. **Hallazgo aparte** (no de producto): una conversación es una queja interna sobre un mystery
   shopper al que se le negó un pedido a medida por miedo a demoras/calidad, generando un
   "detractor grave" -señal operativa para escalar aparte, no para contar.

**Conclusión**: el ángulo de descubrimiento sí aporta valor real, y confirma por qué -juega a favor
de la fortaleza ya probada de la búsqueda (semántica no literal, ver vec01/vec02 en Iteración 1-2)
en vez de exigirle precisión de conteo (su debilidad estructural, ver Iteraciones 14-15). La
pregunta de prueba también enseña algo sobre cómo formular estas búsquedas: específica y accionable
("qué pidió el cliente que no se resolvió") recupera mucho mejor que una nuance amplia y abstracta
("resistencia de precio", "no cerró venta"). **Válido tenerlo en cuenta como guía para formular
preguntas de descubrimiento futuras.** El usuario confirmó que este ángulo tiene valor. Script del
experimento (no productivo, no versionado) descartado al cerrar la sesión; JSON crudo del resultado
tampoco versionado.

**Implementado (2026-09-11)**: agregado un párrafo de guía de "descubrimiento" al final de la
descripción de `search_conversations` en `SYSTEM_INSTRUCTION_TEMPLATE` (`vi_agent.py`) -no
reemplaza nada de lo existente (ORDEN DE USO, filtros, conversation_id, etc. quedan igual), sólo
suma: habilita el uso exploratorio ante preguntas abiertas, indica formular la query de forma
específica y accionable (no abstracta -esa es la lección de esta iteración), pide agrupar hallazgos
en 2-4 patrones parafraseados, y prohíbe explícitamente ponerle un número/porcentaje a un patrón
encontrado así (para no repetir el error de las Iteraciones 14-15). 30/31 tests (el 31vo es el
flake de Langfuse ya documentado, no relacionado).

**Verificación real** (mens_fashion_alto, pregunta abierta "¿hay algo que los clientes están
pidiendo que hoy no estemos resolviendo bien?"): el agente llamó `search_conversations` con una
query específica siguiendo la nueva guía
(`'cliente pidió o necesitó algo que no pudimos ofrecer -producto, talla, servicio, tiempo de
entrega o ajuste'`), la combinó con `run_readonly_sql`, y la respuesta final **no inventó ningún
número desde la búsqueda semántica** -todos los porcentajes citados vienen de SQL real. Hallazgo
lateral: buena parte de este patrón concreto (falta de talla, "sin solución", motivo de venta
perdida) **ya existe como campo estructurado** en `dashboard_v2.vw_mens_fashion_insights_*` -la
muestra chica de la Iteración 16 no alcanzaba para verlo. No invalida el ángulo de descubrimiento
(sigue aportando cuando SQL no cubre el patrón), pero sí un recordatorio: antes de presentar un
patrón encontrado por `search_conversations` como "hallazgo nuevo", vale la pena chequear primero
si ya existe un campo estructurado que lo capture.

### Iteración 17 — generalización a Steren y Farma24 (2026-09-11)

A pedido del usuario, primera generalización del piloto más allá de mens_fashion_alto: **Steren**
(sin RAG, para confirmar que funciona en un cliente "limpio") y **Farma 24** (con RAG activo, para
confirmar que ambas fuentes conviven en la misma respuesta sin pisarse -primer cliente con las dos
a la vez).

**Cobertura de backfill verificada antes de habilitar** (recordings con embedding / total,
`analytics_v2.conversation_embeddings` vs `mart_v2.recordings_enriched`, mismo
`EMBEDDING_CONFIG_ID` en los tres): Salomon 49,8% (13.888/27.870), **Steren 73,2%**
(23.235/31.752), **Farma 24 82,6%** (362.211/438.682) -de referencia, Mens Fashion 64,1%
(28.762/44.885). Se eligió Steren sobre Salomon por mayor cobertura. Agregado `vector_search:
{top_k: 5}` a ambos `config.yaml`.

**Bug real encontrado y arreglado**: `_find_descriptivos_source` matchea la fuente
`resumen_verificado` por sufijo de nombre de vista (`insights_descriptivos_generales`), pero
asumía que la columna dentro de esa vista siempre se llama `resumen_ejecutivo_conversacion` -cierto
en mens_fashion_alto, **falso en Farma24** (la columna equivalente se llama
`descriptivos_generales_tipo_interaccion_detalle`, mismo contenido semántico, nombre distinto).
Producía `column di.resumen_ejecutivo_conversacion does not exist` en la primera prueba real contra
Farma24. Arreglado con un mapeo `_RESUMEN_COLUMN_BY_TENANT` en `vector_search.py` (por tenant, no
por client_id) con default al nombre de mens_fashion_alto; agregado
`test_query_uses_tenant_specific_resumen_column_for_farma24` como regresión. **Limitación
documentada de Steren** (esquema no estándar, no es bug): su fuente se llama
`vw_steren_insights_descriptivos` (sin sufijo `_generales`), así que no matchea la convención de
nombre -`resumen_verificado` sale `None` para Steren, sólo el fragmento reconstruido está
disponible ahí.

**Limitación real y más seria, confirmada en producción**: en la segunda pregunta de verificación
contra Farma24, `search_conversations` devolvió `canceling statement due to statement timeout` -el
`statement_timeout` de 60s (mismo de siempre, ver `utils/postgres.py`) se cumplió antes de terminar
el scan secuencial sobre ~438k recordings (más de 5x el volumen de mens_fashion_alto). El agente se
recuperó solo (siguió con `run_readonly_sql` y dio una respuesta razonable igual), pero la tool en
sí falló. **Esto convierte la limitación ya documentada ("sin índice vectorial, el volumen puede
volverse lento") de riesgo teórico a bloqueo real y reproducible en el cliente de mayor volumen
disponible hoy.** Eleva la urgencia de pedirle a Pedro el índice ivfflat/hnsw -ya no es "cuando
crezca", ya está pasando con Farma24 tal como está hoy. No se implementó ningún workaround en este
proyecto (ver limitación 2 del docstring del módulo: crear el índice es decisión de quien
administra `analytics_v2.conversation_embeddings`, no de este proyecto).

**Confirmado (2026-09-11): el timeout es la ÚNICA falla real de Farma24**, no hay un segundo bug
escondido detrás. Se probó acotando por fecha (`date_from`/`date_to` de una semana) para esquivar
el scan completo -la búsqueda funcionó sin error de SQL, devolvió resultados, y `resumen_verificado`
salió `None` en los 3 (cruzado directo contra Postgres: esas conversaciones puntuales, muy
recientes, todavía no tienen fila en `vw_farma24_insights_descriptivos_generales` -comportamiento
ya documentado para "no pasó por el pipeline de insights todavía", no un bug nuevo). Lucas tiene
permisos de escritura sobre la tabla, pero se decidió avisarle a Pedro antes de crear el índice
-sigue siendo su pipeline el que puebla la tabla activamente, evita pisarle algo o competir por
recursos mientras corre backfill. Mensaje a Pedro: pendiente de redactar, no enviado todavía.

**Verificación real, ambos clientes**: Steren respondió con dos ejemplos reales de consultas de
garantía (sucursales Metrocentro y General Escalón), sin fuga técnica. Farma24 respondió
combinando `search_conversations` (ejemplo textual de un caso de falta de stock) con
`run_readonly_sql` (que además reveló el mismo patrón de "sin_solucion"/"falta_talla" ya visto en
Iteración 16 del lado estructurado). **42/42 tests** (`test_vector_search.py`, sumado el de
regresión), **31/31** (`test_vi_agent.py` -los dos tests que antes usaban `farma24_alto` como
"cliente sin búsqueda vectorial" pasaron a usar `salomon_alto`, único de los tres candidatos que
sigue sin el bloque).

**Streamlit -badge de identificación** (`4. scripts/streamlit_app.py`): el selector de cliente
ahora antepone 🔎 al nombre de los clientes con `vector_search` habilitado (`_has_vector_search`,
lee `config.yaml` vía `load_client_config` -no toca qué tools se exponen, eso lo sigue decidiendo
`vi_agent._build_tools_list`), más un `st.caption` debajo del selector cuando el cliente elegido
tiene el badge. Implementado con `format_func` en el `st.selectbox` para que el badge sea sólo
visual -el valor real (`chosen_display_name`, usado en el título del chat y el saludo inicial)
queda limpio, sin el emoji. Verificado visualmente en el navegador: Men's Fashion, Steren y
Farma 24 muestran el badge; el resto (incluido Salomon) no.

### Iteración 18 — el conteo falla igual en otro cliente (2026-09-11)

A pedido del usuario ("prueba si el conteo podría funcionar en otro cliente que no sea Mens"),
repetido el experimento de conteo (Iteración 14: recuperar top-K + clasificar leyendo) en
**Farma 24**, dominio distinto (farmacia vs. retail de moda), mismo tipo de pregunta (resistencia
de precio no literal).

**Bloqueo previo**: sin acotar, la búsqueda vuelve a tirar el timeout de la Iteración 17 (probado
dos veces: sin filtro, y con `date_from`/`date_to` de casi 6 semanas -un `EXPLAIN` confirmó que el
plan hace `Parallel Seq Scan` sobre `mart_v2.recordings_enriched` completo antes de poder ordenar
por distancia, porque no hay índice que soporte `ORDER BY ... LIMIT k` de forma barata). Se logró
correr acotando por `store_name` a una sucursal chica (Rawson, 986 conversaciones -la más chica de
Farma24 con datos reales).

**Resultado, 15/20 candidatos clasificados por lectura directa (sin más llamadas a Gemini)**: **~2
genuinamente relevantes (~13-15% de precisión)** -misma franja que Mens Fashion en la Iteración 14.
Diferencia notable: acá **no hubo ruido inválido** (nada de conversaciones internas o
transcripciones ininteligibles, a diferencia de Mens Fashion) -son todas interacciones reales
cliente-farmacéutico, pero la mayoría sólo *menciona* un precio (cotización normal, venta cerrada
sin fricción) sin mostrar resistencia real.

**Conclusión: la falla de precisión para conteo es del enfoque (embeddings de chunk + distancia),
no específica del dominio de Mens Fashion.** Cambiar de cliente no lo arregla -se mantiene la
recomendación de las Iteraciones 14-16: no construir un modo de conteo sobre
`search_conversations` en ningún cliente; sí seguir usando el ángulo de descubrimiento (Iteración
16), que no depende de precisión de conteo. Nota metodológica: esta prueba sólo pudo correr acotada
a una sola sucursal chica por el bloqueo del índice -no es una muestra representativa de todo
Farma24, pero alcanza para responder la pregunta de fondo (¿el problema es de dominio o de
enfoque?).

### Iteración 19 — ¿ayudaría el índice a "traer todo" para nuances concretas? (2026-09-11,
discusión sin código, sin experimento nuevo)

El usuario preguntó, en la misma línea de las Iteraciones 14-18: si la nuance es lo bastante
concreta (ver el contraste Iteración 16 -evento objetivo, 20/20 precisión- vs. Iteraciones 14/15/18
-estado subjetivo a inferir, ~15% precisión-), ¿el índice vectorial (Iteración 17/pendiente con
Pedro) permitiría escanear y traer *todo* lo que cae dentro del embedding, en vez de sólo el top-K?
Dos razones por las que la respuesta es no, aunque la nuance sea perfecta:

1. **ivfflat/hnsw son índices de vecinos más cercanos aproximados (ANN)**, diseñados para acelerar
   `ORDER BY distancia LIMIT K` -no existe en pgvector un query tipo "traeme todo lo que esté a
   menos de X de distancia" que el índice pueda acelerar. Con o sin índice, sigue sin haber un corte
   de distancia limpio para decidir qué cuenta (mismo hallazgo de la Iteración 3: rango 0,20-0,27
   sin separación entre relevante e irrelevante).
2. Un índice ANN además es **aproximado por diseño** -puede perderse vecinos verdaderamente cercanos
   a cambio de velocidad. Paradójicamente, para exhaustividad real un scan completo sin índice
   (lento pero exacto) es más confiable que un índice ANN (rápido pero aproximado).

Lo único que el índice cambiaría es el techo práctico de K (hoy ~60 candidatos por el timeout de
60s sin índice; con índice, K=500-1000 sin problema) -útil para el ángulo de descubrimiento
(Iteración 16, más candidatos para leer), pero no convierte `search_conversations` en un conteo
exacto y completo. Esa sigue siendo responsabilidad de un pipeline de clasificación exhaustivo en
la ingesta (LLM sobre el 100% de las conversaciones, guardado como campo estructurado consultable
por SQL) -no de retrieval vectorial, con o sin índice.

### Iteración 20 — generalización a los 4 candidatos reales restantes (2026-09-11)

A pedido del usuario ("¿cuánto costaría generalizarlo?"), primero se cuantificó el costo real:
prácticamente nulo en $ (`gemini-embedding-001` a $0,15/millón de tokens, cada query ~20-30
tokens -incluso con 10.000 búsquedas/mes en todo el proyecto, ~$0,05/mes; con el prompt caching de
hoy tampoco suma casi nada de system_instruction por cliente adicional). El costo real es cobertura
de datos y volumen, no dólares. Verificados los 13 tenants que faltaban (SQL puro, sin gastar
Gemini) -candidatos reales por volumen+cobertura: **Maga** (203.365 conversaciones, 83,0%
cobertura -mejor que cualquier cliente con vector_search hasta ahora), **Tigo** (97.442, 67,4%),
**Roberts** (21.621, 70,8%), **High Life** (9.483, 69,2%). Descartados por volumen insuficiente:
Huerpel (tenant compartido entre 3 sub-marcas, no se puede aislar), Atlas (33,2% cobertura, más
bajo), GAC/Hyundai/Dalton/Agrosuper/Shoe Box/Forever 21 (<300 conversaciones cada uno).

Habilitado `vector_search: {top_k: 5}` en los 4 config.yaml. Verificación de config previa: mismo
`EMBEDDING_CONFIG_ID` que mens_fashion_alto en los 4 (sin sorpresas de config distinto).

**Bug real encontrado y arreglado (Maga)**: `vw_maga_insights_descriptivos_generales` matchea el
sufijo de convención de nombre de `_find_descriptivos_source`, pero a diferencia de Farma24 (mismo
dato, columna con otro nombre) a Maga directamente **le falta la columna `conversation_id`** (usa
`recordingid`) -el JOIN de `resumen_verificado` habría tirado
`column di.conversation_id does not exist`. Arreglado agregando un nuevo mecanismo,
`_DESCRIPTIVOS_UNSUPPORTED_TENANTS` (`vector_search.py`), para tenants donde la vista existe pero
no sirve para este JOIN puntual -Maga tratado como cliente de schema delgado sólo para esto.
Verificado con SQL directo contra `information_schema.columns` antes de escribir el fix (no
adivinado). Test de regresión nuevo.

**Limitaciones ya conocidas, confirmadas de nuevo**: Tigo tiene el mismo caso que Steren (vista
`vw_tigo_insights_descriptivos`, sin sufijo `_generales` -no matchea la convención, resumen
ausente). Roberts y High Life tienen exactamente el mismo schema que mens_fashion_alto
(`conversation_id` + `resumen_ejecutivo_conversacion` presentes) -verificado por SQL directo antes
de habilitar, sin sorpresas.

**Verificación real, los 4 clientes** (1 llamada de embedding por cliente, sin pasar por el chat
completo para no gastar de más): los 4 devolvieron resultados sin error de SQL -Maga y Tigo con
`resumen_verificado` ausente (esperado), Roberts y High Life con `resumen_verificado` presente
(esperado). 137/137 tests. **Vector search ahora habilitado en 7 de 19 clientes**: mens_fashion_alto
(piloto original), steren_alto, farma24_alto (Iteración 17), maga_alto, tigo_alto, roberts_alto,
high_life_alto (esta iteración).

### Iteración 21 — centroides contrastivos (positivo/negativo) para conteo: mismo techo de precisión
(2026-09-14)

A pedido del usuario, un cuarto enfoque de conteo (además de umbral fijo, top-K+clasificar, y
multi-query de las Iteraciones 14-15): en vez de un umbral absoluto de distancia contra UNA query,
armar dos **centroides** -promedio de embeddings de ~10 frases de ejemplo por clase- uno para
"resistencia de precio" y otro para "conversación de venta genérica sin relación con precio", y
clasificar cada conversación por cuál centroide le queda más cerca (distancia relativa
por-conversación, no un corte universal). Motivación: evitar el costo de clasificar candidatos con
un LLM (lo que se descartó primero por caro) usando sólo aritmética de vectores -las 20 frases de
ejemplo se embeben una única vez (costo trivial), el resto es una query SQL con dos distancias por
fila, mismo costo de scan que cualquier búsqueda existente.

**Prueba real** (mens_fashion_alto, mismo caso que Iteraciones 14/15: resistencia de precio no
explícita, sin filtro de tienda/fecha -corpus completo, 34.300 conversaciones): query de 16s
calculando distancia a los dos centroides para cada una. **340 conversaciones (1,0%)** quedaron más
cerca del centroide positivo.

Leídos los 15 casos con mayor margen a favor del centroide positivo (los "más confiadamente
positivos"): de los 10 que tenían `resumen_ejecutivo_conversacion` disponible (5 sin resumen, gap de
cobertura del pipeline de insights, no relacionado con este experimento), **sólo 2 eran resistencia
de precio real** (~20% de precisión), 2 borderline, 2 eran ruido no válido (transcripción
ininteligible, interacción no comercial -mismo problema de la Iteración 14, embeddings de chunk
crudo), y 4 sin relación con precio. Los 10 casos con mayor margen hacia el centroide NEGATIVO sí
resultaron correctamente ajenos al tema (retiro de pedido, ajuste de talla, eventos) -el centroide
separa bien en la dirección correcta, pero eso no se traduce en precisión útil en el borde positivo.

**Conclusión: mismo techo (~15-20%) que las Iteraciones 14/15/18, con un método de comparación
distinto.** Confirma, con un cuarto enfoque, lo que la Iteración 19 ya explicaba en teoría: el
problema no es la estrategia de comparación (umbral fijo, top-K+clasificar, multi-query, o ahora
centroides contrastivos) -es que "resistencia de precio no explícita" es una nuance **subjetiva a
inferir**, mientras que el único caso con precisión alta hasta ahora (Iteración 16, 20/20) fue un
evento **concreto y objetivo** ("cliente pidió algo que no pudimos ofrecer"). Cambiar el método de
comparación no cambia esa naturaleza de fondo. **Se mantiene la recomendación de las Iteraciones
14-19: no construir un modo de conteo sobre `search_conversations`**, con ningún método probado hasta
ahora. Script del experimento (no productivo, no versionado) descartado al cerrar la sesión.

### Iteración 22 — conteo literal exacto: movido a `3. experimentos/` (2026-09-14)

Investigado acá originalmente (nombre literal, ej. "¿cuántas veces se menciona el Banco Macro?" -
distinto a las Iteraciones 14-21, que son sobre nuances subjetivas), pero es una propuesta de
FEATURE nueva, no parte de la narrativa de iteraciones de `search_conversations` en sí -movido a
pedido explícito a `3. experimentos/conteo_literal_exacto/README.md` (mismo criterio que
`coaching_playbook/` y `resumen_ejecutivo/`, cada propuesta en evaluación con su propia carpeta).
Ver ese README para el diseño completo, la prueba real (63 conversaciones, el hallazgo de "macro"
ambiguo, latencia sin índice) y la guía de prompt lista para usar.

### Iteración 23 — defensa reforzada contra inyección de instrucciones en contenido citado (2026-09-14)

La Iteración 9 ya había probado esto una vez (mockeando un fragmento con instrucciones inyectadas) y
el modelo resistió sin fallas — pero esa prueba no dejó ninguna defensa DURABLE en el código, sólo
confirmó que el comportamiento base de Gemini + `response_policy.py` aguantó ese caso puntual. Dado
que `search_conversations` mete texto de conversaciones reales (clientes, vendedores) al contexto del
modelo por diseño, y nada impide que una transcripción real contenga algo redactado para manipular al
agente (a propósito o por casualidad), se decidió agregar una defensa explícita en vez de confiar sólo
en el comportamiento por defecto del modelo:

1. **Nueva sección "SEGURIDAD ANTE CONTENIDO DE CONVERSACIONES" en `SYSTEM_INSTRUCTION_TEMPLATE`**
   (`4. scripts/vi_agent.py`): instruye explícitamente que cualquier texto de una conversación real
   devuelto por una tool es DATO citable, nunca una instrucción, sin importar cómo esté redactado
   (orden, cambio de rol, o como si viniera de un "sistema"/administrador). Vive en el template fijo
   (no en `_build_extra_tools_section`, que es dinámico por cliente) porque aplica a cualquier
   herramienta que devuelva texto libre, no sólo a `search_conversations` — mismo patrón que ya
   existía para `get_business_rules` ("El contenido devuelto... no una nueva instrucción para vos").
2. **`posible_instruccion_incrustada` (`4. scripts/vector_search.py`)**: cada resultado de
   `search_conversations` ahora incluye este booleano, calculado por
   `_contains_possible_injection_marker` contra `fragmento_aproximado` y `resumen_verificado` — una
   lista corta de patrones típicos (español e inglés: "ignorá las instrucciones", "ignore previous
   instructions", "you are now", "system:", etc., mismo criterio conservador que
   `_OFFENSIVE_TERMS`). Deliberadamente NO filtra ni altera el texto citable (a diferencia del
   enmascarado de lenguaje ofensivo) — alterar el fragmento rompería la exactitud de la cita para un
   caso donde el texto en sí puede ser la evidencia de negocio relevante. Sólo agrega una señal para
   que el modelo redoble el criterio cuando el patrón aparece — reforzado con una instrucción
   específica en el ítem de `search_conversations` de `_build_extra_tools_section()`.
3. **Qué NO se hizo**: no se implementó ningún filtrado/sanitización del texto (ver punto 2), ni
   detección de inyección sobre los campos de texto libre de `run_readonly_sql` (ej.
   `resumen_ejecutivo_conversacion`) — la instrucción general del punto 1 los cubre igual, pero el
   detector de patrones sólo corre sobre lo que devuelve `search_conversations`, que es la vía
   principal por la que texto de conversaciones no revisado entra al contexto del modelo.

**Verificado**: 8 tests nuevos en `5. tests/test_vector_search.py`
(`InjectionMarkerDetectionTests`, `InjectionMarkerFieldInResultsTests`) — detección de patrones en
español e inglés, texto de negocio ordinario no se marca falso positivo, el campo viaja en el
resultado real de `search()` tanto si el patrón aparece en el fragmento como en el resumen
verificado. 185/185 tests del proyecto en verde. Probado en vivo contra `mens_fashion_alto`
(pregunta real sobre garantías, dos llamadas a `search_conversations` con reformulación) sin
regresiones — el tool loop y las citas siguen funcionando igual que antes. No se probó en vivo un
fragmento real con un patrón de inyección genuino (no hay ninguno conocido en los datos reales
disponibles) — la Iteración 9 ya cubrió ese caso vía mock contra el modelo real.

### Iteración 24 — LLM-as-judge sobre resultados y escucha de audio real (2026-09-14)

Dos pedidos explícitos del usuario en la misma sesión, ambos verificados en vivo contra
`mens_fashion_alto` y `farma24_alto`.

**1. LLM-as-judge, segunda pasada sobre los resultados ya recuperados.** Pedido explícito: "por las
dudas utilices un LLM as a judge para comprobar que las conversaciones que se traen cuando se pide
conversaciones con insultos realmente sean de eso". No resuelve la limitación estructural de las
Iteraciones 14/15/18/21 (la distancia vectorial sola no separa limpio nuances subjetivas) -es una
capa adicional que, con el modelo real del cliente (mismo criterio que "por qué el gate usa el
modelo real, no uno más barato"), lee cada fragmento ya recuperado y decide si genuinamente
respalda la búsqueda antes de devolverlo. Implementado en `_judge_relevance`
(`4. scripts/vector_search.py`), corre siempre (no sólo en queries "subjetivas" -distinguirlas de
antemano sería una heurística frágil), fail-open ante cualquier fallo (nunca deja la búsqueda sin
resultados por un problema del juez mismo). Si descarta algo, `aviso` lo dice explícitamente.
**Costo real medido en vivo**: agrega una llamada más a Gemini por búsqueda -en la prueba contra
Farma24, `search_conversations` pasó de tardar por la falta de índice ANN (ya documentado) a mostrar
"125.4s -- search_conversations 2× 124.8s" con el juez incluido; no se aisló cuánto de eso es el
juez en sí vs. el scan sin índice, pero es un costo no trivial a tener en cuenta.

**2. Escuchar el audio real de una conversación citada.** Pedido explícito: "que se puedan escuchar
las conversaciones". Investigado y confirmado en vivo ANTES de escribir código que el audio original
sí es accesible: vive en Google Cloud Storage (bucket por cliente, ej. `audios-to-analyze-app2` por
default, `audios-to-analyze-tigo` para Tigo -confirmado contra Firestore real), y las credenciales
que ya usa el proyecto para Firestore tienen permiso de lectura ahí. Nuevo módulo
`4. scripts/audio_playback.py`: `conversation_id` (ya expuesto por `search_conversations`) →
Postgres (`mart_v2.recordings_enriched`, scoped por tenant) resuelve `recording_id`/`filename` →
Firestore (`recordings/{recording_id}`) resuelve `bucketName` real (con el default confirmado si no
está declarado) → GCS genera una URL firmada v4, de sólo lectura, 30 minutos de vencimiento. **Nunca
pasa por el modelo** -es la interfaz web (`streamlit_app.py`, botón "🔊 Escuchar" por conversación
citada, resuelto on-demand al hacer click) la que la arma directo, para no romper la regla de "nunca
reveles identificadores técnicos" del prompt. Bug encontrado y corregido al probar en vivo: GCS sirve
estos archivos con `Content-Type: application/octet-stream` real (nunca seteado al subirlos) -sin
forzar el tipo correcto (`response_type` en la URL firmada, mapeado desde la extensión real del
archivo) el `<audio>` del navegador a veces no reproducía aunque el archivo estuviera bien.

**Verificado end-to-end con el navegador real** (no sólo con mocks): contra `mens_fashion_alto`,
"dame un ejemplo de un vendedor que le haya ofrecido al cliente una prenda distinta a la que buscaba
porque no había stock" trajo 4-5 conversaciones citadas, cada una reproducible -confirmado leyendo el
DOM real (`document.querySelectorAll('audio')`) y haciendo `fetch` HEAD contra la URL firmada:
`200`, `Content-Type: audio/mp4`, contenido real (~1.6MB). Contra `farma24_alto`, las dos llamadas a
`search_conversations` de esa corrida fallaron por el timeout ya documentado (Iteración 17, sin
índice ANN) -el agente respondió igual usando `run_readonly_sql`, y correctamente NO se ofreció
ningún botón de audio (no hay resultados reales de búsqueda de los que colgarlo). 378/378 tests del
proyecto en verde (`test_audio_playback.py` nuevo, `test_vector_search.py`/`test_streamlit_app.py`
ampliados).

**Falso positivo real reportado por Lucas el mismo día (2026-09-15), diagnosticado y cerrado**: al
probar contra `roberts_alto`, varios reproductores mostraron "0:00 / 0:00" con el botón de play sin
responder. Investigado a fondo antes de asumir un bug de código: se bajó el archivo real de GCS
(`HQ-angel.villa-...mp4`, 8,4MB), se caminó la estructura de boxes MP4 a mano (`ftyp`/`mdat`/`moov`,
`moov` al final del archivo, ~66KB) y se confirmó `mp4a`/AAC estándar en `stsd` -mismo codec que el
archivo de Mens Fashion ya verificado como reproducible. Range requests, CORS (`Access-Control-Allow-Origin: *`)
y `Content-Type` correcto (`audio/mp4`, ver el fix de `response_type` arriba) confirmados contra el
archivo puntual que fallaba -sin ninguna diferencia real contra el caso que sí funcionaba. **Causa
real: el navegador/acceso que se estaba usando para probar, no el código** -Lucas confirmó que
accediendo por Chrome (en vez de por donde probaba antes) el mismo audio reproduce sin problema.
Nada se cambió en el código a partir de este reporte -quedó documentado acá por si vuelve a
reportarse un "0:00/0:00" antes de asumir que es una regresión real.

**Bug real encontrado por Lucas el mismo día (2026-09-15), corregido**: pidió "dame 5 audios que
ejemplifiquen la mala cordialidad de ese vendedor" contra `roberts_alto` y la respuesta se armó
enteramente con `run_readonly_sql` (campos de evidencia ya extraídos del checklist) -sin ningún
botón de audio, porque sólo los resultados de `search_conversations` traen el `conversation_id` que
mi mecanismo de audio puede reproducir. Causa raíz: la lista de "señales típicas" de
`_build_extra_tools_section` (`4. scripts/vi_agent.py`) no incluía "audio(s)"/"escuchar" como
disparador -el modelo, siguiendo la regla general de "no llames a search_conversations si SQL ya
tiene evidencia", priorizó SQL porque ya tenía texto citable, sin saber que ESE texto no habilita
ningún botón de escucha real. Corregido en dos partes:
1. Agregadas "audio(s)"/"escuchar"/"grabación(es)" a las señales que obligan a usar
   `search_conversations`, con una aclaración explícita: aunque `run_readonly_sql` ya tenga una
   síntesis o cita armada, sólo `search_conversations` deja algo reproducible en la interfaz.
2. **Límite duro nuevo, verificado empíricamente** (no una suposición): `search_conversations`
   nunca es confiable para dar un NÚMERO/conteo/porcentaje de un concepto cualitativo sin campo
   estructurado (ej. "cuántas veces insultó") -si no hay un campo que ya lo mida, el modelo tiene
   que decirlo en vez de convertir un conteo manual de sus propios resultados de búsqueda en una
   cifra. Formaliza en el prompt lo que esta ronda de iteraciones (14/15/18/21) ya había probado
   empíricamente pero sólo estaba escrito como guía dentro de la sección de "descubrimiento", no
   como regla dura aplicable a cualquier pedido de número.

**Verificado en vivo contra `mens_fashion_alto`** (misma pregunta de mala cordialidad, formato de
audios): el agente llamó `search_conversations` 2 veces, encontró **1 caso real** con evidencia
textual genuina (una discusión real cliente-vendedor, citas literales de ambos lados) y fue honesto
sobre no tener los otros 4 pedidos ("no se dispone de más audios con registros concluyentes") en vez
de inventarlos -"🔊 Escuchar conversaciones citadas (1)" apareció con el botón real. 378/378 tests en
verde (sólo texto de prompt, sin tests nuevos necesarios).

### Iteración 25 — mini-benchmark con LLM-as-judge externo: la precisión estaba bien, el timeout no (2026-09-15)

Pedido explícito: medir con un juez de evaluación EXTERNO (independiente del `_judge_relevance`
interno de producción, Iteración 24) si los audios que ofrece la interfaz realmente corresponden a
lo pedido -no confiar en que el sistema se autoevalúa bien. Diseño acordado con Lucas por costo
(aprobado en el rango US$0,10-0,30, todo en `gemini-3.7-flash` -no se usó un modelo más caro para
el juez de evaluación): 8 preguntas (mitad concretas, mitad subjetivas como "insultos"/"mala
cordialidad") contra `mens_fashion_alto` y `steren_alto`, llamando `search_conversations` directo
(sin pagar el turno completo del agente).

**Primer resultado, no el esperado**: 10 de 16 búsquedas (62,5%) cortaron con "canceling statement
due to statement timeout" -incluyendo `mens_fashion_alto`, el cliente históricamente mejor probado
del proyecto. Sólo 3 búsquedas trajeron resultados evaluables; el juez externo las confirmó 3/3
correctas, pero la muestra era demasiado chica para decir algo sobre precisión con esa cantidad de
fallas.

**Segunda ronda, diagnóstico de la causa real**: se repitieron 6 de las preguntas que habían cortado
por timeout, con `statement_timeout` extendido a 180s sólo para esta medición (monkeypatch de
`utils.postgres.postgres_connection_kwargs`, sin tocar el archivo real todavía). Resultado: **las 6
trajeron resultados reales** -69,8s, 35,5s, 148,8s (el peor caso), 59,1s, 35,3s y una genuinamente
sin resultados en 6,5s. No eran búsquedas rotas -eran búsquedas lentas que el límite de 60s cortaba
antes de que terminaran.

**Tercera ronda, cerrando el loop**: se volvió a correr esas mismas 6 preguntas (timeout extendido)
esta vez SÍ con el juez externo sobre el contenido real devuelto -11 resultados, **11/11 correctos**
(sustituto por falta de stock, talla específica no disponible, descuento por combo, todos con
evidencia textual genuina y verificable). Sumado a la primera ronda: **14/14 resultados reales
confirmados correctos** por el juez externo.

**Conclusión y acción tomada**: la precisión de `search_conversations` (para preguntas concretas, ya
documentado en iteraciones previas) no era el problema -el cuello de botella real es que el límite
fijo de 60 segundos corta búsquedas genuinas antes de que devuelvan resultados correctos. Subido
`statement_timeout` de 60000 a 180000ms en `utils/postgres.py` (compartido por
`run_readonly_sql`/`search_conversations`, cubre el peor caso medido -148,8s- con margen). Trade-off
explícito, aceptado por Lucas: en el peor caso el usuario espera hasta 3 minutos en vez de recibir un
corte rápido -no resuelve la causa raíz (falta de índice ANN, sigue siendo trabajo de la persona que
administra `analytics_v2.conversation_embeddings`), es un parche que evita fallar búsquedas que en
realidad iban a funcionar. 378/378 tests en verde tras el cambio (ningún test asumía el valor
`60000` de forma literal).

### Iteración 26 — resumen de capacidades/límites consolidado y aviso de espera larga en la UI (2026-09-15)

Pedido explícito de Lucas, distinto a un bug puntual: "pensemos en la forma de que la búsqueda
vectorial sea lo más útil posible... y que no haga otras cosas" -en vez de agregar una regla más
reactiva, dos mejoras de mantenibilidad/UX sobre lo ya construido en la Iteración 25 y anteriores.

- **Resumen de capacidades/límites al principio del bloque de `search_conversations`** (`vi_agent.py`
  > `_build_extra_tools_section`): las reglas de esta tool se fueron agregando una por una durante
  varias iteraciones (límite duro de conteo, orden de uso vs. `run_readonly_sql`, trigger words de
  audio, integridad de cita, seguridad ante inyección...) y ya sumaban un párrafo largo sin una
  síntesis al principio. Se agregó un párrafo corto ANTES de todo el detalle -"SÍ sirve para
  evidencia/citas/audio de casos puntuales y exploración cualitativa abierta; NO sirve para números,
  conteos, porcentajes, tasas o rankings de un concepto subjetivo, ni para reemplazar SQL cuando ya
  alcanza"- para que sea escaneable de un vistazo. No se tocó ni se borró ninguna regla de detalle
  existente, sólo se le puso un índice arriba. 378/378 tests en verde (cambio de texto de prompt, sin
  lógica nueva que testear).
- **Aviso de espera larga en el spinner de la UI**: con `statement_timeout` en 180s (Iteración 25),
  una búsqueda que antes fallaba rápido ahora puede tardar hasta ~150s con éxito -sin ningún aviso,
  eso se siente como que la app se colgó, justo el peor momento para perder confianza en la
  herramienta. Se cambió el label de progreso de `search_conversations` en `streamlit_app.py`
  (`_TOOL_PROGRESS_LABELS`) de "Buscando ejemplos en conversaciones..." a "Buscando ejemplos en
  conversaciones... (puede tardar hasta 3 minutos)", puesto por adelantado apenas arranca la
  búsqueda, no recién si el usuario se impacienta. `run_readonly_sql` no se tocó -comparte el mismo
  timeout de 180s, pero no hay evidencia medida (a diferencia de la búsqueda vectorial) de que
  habitualmente se acerque a ese límite. Test de texto exacto actualizado en
  `test_streamlit_app.py::ToolProgressLabelTests`, 378/378 en verde.

### Detalle de la siguiente iteración (2026-09-10)

- **Índice vectorial (ivfflat/hnsw)**: no es código de este proyecto (la tabla la administra la
  otra persona), pero si el volumen sigue creciendo (~720k filas hoy, 284 runs de backfill en ~13
  días) la latencia de `ORDER BY embedding <=> %s LIMIT k` sin índice va a degradar. Acción concreta:
  preguntarle a la otra persona si está en su roadmap, y si no, evaluar si tiene sentido proponerlo
  desde acá dado que ya se depende de esa tabla en producción.
- **Chunking real vs. reconstrucción aproximada**: sigue siendo la deuda más grande del prototipo.
  Sin el algoritmo exacto, cada fragmento citado es una aproximación razonable pero no garantizada.
  Alternativa más simple que descifrar el algoritmo: pedirle a la otra persona que persista el texto
  del chunk (una columna más en la tabla, o una tabla aparte por `content_hash`) — evita duplicar
  lógica de chunking en dos lugares y elimina el riesgo de desajuste.
- ~~Filtro estructurado por tienda/vendedor/fecha~~ y ~~umbral de distancia mínima~~ — ambos hechos
  el 2026-09-10, ver "Iteración 3" arriba (el segundo se resolvió distinto a como se planteaba acá
  originalmente: distancia relativa por búsqueda en vez de un corte absoluto, con los motivos
  documentados en esa sección). `date_from`/`date_to` sigue sin implementarse -quedó fuera de esta
  iteración, `store_name` cubrió el caso real encontrado en la evaluación.
- **Segunda opinión sobre el mini-banco**: las 11 preguntas corridas (ronda 1 + ronda 2) las diseñó
  la misma persona que implementó la tool -razonable para destrabar el prototipo rápido, pero con
  sesgo de confirmación inherente. Antes de generalizar a otro cliente, vale la pena que alguien más
  (sin contexto del diseño interno) escriba unas preguntas y las corra a ciegas.

### Iteración 27 — de "citas" a insight: filtro por checklist + analista de conversaciones (2026-09-21)

Pedido explícito de Lucas: que la búsqueda vectorial no sirva sólo para citar sino para tener un
insight que el SQL no da -pedir coaching de un vendedor y que diga **qué hizo mal en particular**
(más allá del checklist) y **cómo mejorar siguiendo a compañeros con mejores métricas que resuelven
esa situación de otra forma-. Restricción: sin tool nueva y sin llamadas LLM adicionales.

Diagnóstico: buscar "conversaciones parecidas a una frase" es el enfoque equivocado para coaching
(no trae *dónde falló este vendedor* ni *dónde acertó su compañero*), y el juez ya leía todos los
fragmentos con un LLM barato pero devolvía sólo true/false. Tras ~30 ajustes de prompt seguía
saliendo genérico.

Cambios (`vector_search.py`, `vi_agent.py`):

- **Filtro por resultado del checklist**: `search_conversations(criterio, resultado)` -JOIN a la
  vista `*_rendimiento_vendedor` (una fila por conversación, por `recording_id`)-. `employee_name` +
  `criterio` + `resultado='No'` trae SUS conversaciones donde falló ese criterio; el nombre de un
  compañero + `resultado='Sí'` trae cómo lo cumple. `criterio` se valida por pertenencia exacta a
  los campos Sí/No de la fuente de rendimiento del Data Map ACTIVO (`_performance_criteria`), y el
  `description` de ese campo (qué significa Sí/No) se le pasa al analista.
- **El juez pasa a analista (misma llamada, mismo modelo `gemini-3.5-flash-lite`)**: además del
  veredicto devuelve por conversación `notas` {situacion, que_hizo, como_termino} y `patrones` que
  se repiten. Se mapea por el índice `i` (encontrado en vivo: con 8 fragmentos devolvió 7 y el
  chequeo de largo tiraba todo). Con filtro de checklist la relevancia no vacía el resultado (se
  conservan los que tienen notas; si ninguno, todos). Sigue aceptando el formato viejo (array de
  booleanos) y sigue siendo fail-open.
- **Prompt**: el bloque de `search_conversations` pasó de ~380 a ~90 líneas, centrado en tres
  flujos (coaching de un vendedor, varios vendedores, equipo en un período) que se arman **por
  situación**: "Cuando [situación], este vendedor [hace X]; un compañero con mejor resultado
  [hace Y] → probar Z".

Costo: prácticamente neutro -el juez ya recibía los ~8 fragmentos completos; sólo crece la salida
(~100 tokens por conversación). Mismo número de llamadas. La búsqueda con filtro tardó 5-15s en
mens_fashion_alto/roberts_alto.

Verificado en vivo (mens_fashion_alto): coaching de Ubaldo Ramos, "peores tres vendedores" y
"qué recomendarías al equipo esta semana" ahora describen conductas concretas por situación
(ej. "cuando el cliente pregunta el precio, informa precio y promoción y espera; un compañero junta
las prendas elegidas y propone pasar a caja") en vez de recomendaciones de manual.

**Ronda 2 (mismo día): `comparar_con_mejores`.** El límite (4) de abajo se resolvió en código en vez
de por prompt: con `criterio` + `resultado='No'` + `comparar_con_mejores=true`, UNA llamada trae
(a) las conversaciones donde el vendedor falló el criterio y (b) las de los mejores en ese criterio
(`_top_performers`: agregación chica sobre la vista de rendimiento, base mínima 15, excluye al
vendedor, respeta período/tienda), y el analista las ve juntas en UNA llamada y devuelve un
`contraste` por situación (situación / qué hace el vendedor / qué hacen los compañeros). Los
nombres de los compañeros **nunca llegan al modelo principal** (se reemplazan en código por
"compañero con mejor resultado"). Menos llamadas que antes (1 por vendedor en vez de 2 búsquedas +
la que el modelo se salteaba) y el emparejamiento de situaciones lo hace el analista barato con
ambos lados a la vista, no el modelo caro con texto crudo. Sin `employee_name` sirve para equipo en
un período. Verificado en vivo: "peores tres vendedores" -3 llamadas, una por persona, cada una con
su propio criterio- devolvió para los tres un "contraste en piso" con situación, conducta del
vendedor, conducta del compañero y "qué probar"; el semanal de equipo, una sola llamada.
Bug preexistente encontrado en el camino (`answer_verification.metric_tokens`): una numeración con
formato markdown ("### 1. Jorge", "**1.** Jorge") se leía como cifra "1" sin respaldo y agotaba los
reintentos (fallback genérico) -corregido y con tests.

Límites conocidos: (1) lo observado es una **muestra** de ~8 conversaciones por lado -se presenta
como "en las conversaciones revisadas", nunca como estadística (LÍMITE DURO sin cambios); (2) la
transcripción tiene ruido y "Speaker N" no confiable -el analista deduce el rol por contexto; (3)
clientes cuyos fragmentos son muy ruidosos (ej. farma24_alto en las pruebas) degradan a "sin notas"
y el resultado es el de antes; (4) [RESUELTO en la ronda 2: la comparación con los mejores ya no depende de que el modelo pida
otra búsqueda]; (5) el fragmento crudo sigue viajando al modelo principal (posible recorte futuro para
ahorrar tokens del modelo caro).

Última actualización del README: 2026-09-21

### Iteración 27, ronda 3 (2026-09-21): recorte de costo

- **`incluir_fragmentos` (default `false`)**: el texto crudo (`fragmento_aproximado`, ~1.000 tokens por
  resultado, ~13 resultados por búsqueda en modo comparación) ya no viaja al modelo principal cuando el
  resultado trae `notas`; el insight va en las notas. El modelo pasa `true` sólo si se piden
  citas/textual/ejemplos/audios. Sin notas (fail-open) el fragmento se conserva.
- **Verificador**: enteros 1-10 usados como cuantificador de prosa ("2 acciones") ya no cuentan como cifra
  de resultados (medido en `gemini_calls.jsonl`: causa principal de `evidence_repair`, cada uno una llamada
  completa al modelo principal; ~28% del gasto de la muestra reciente). Siguen exigiendo respaldo
  cuando cuentan conversaciones, ventas, clientes, etc.
- **Descartado (medido)**: fusionar en una sola query las conversaciones del vendedor y de los mejores.
  `query_ms` es de base de datos (mediana 4,7 s), no gasto de Gemini, y una sola query con `LIMIT` deja que
  un grupo desplace al otro.

### Iteración 28 (2026-09-22): "que_hizo" verificado por código contra el fragmento, no sólo por prompt

Pedido explícito de Lucas ("tenemos que solucionar las notas cualitativas de coaching para que sean
confiables y no digan nada que no es"), a raíz de una auditoría manual de esta misma sesión: de 8
notas de Ubaldo Ramos revisadas a mano contra la transcripción cruda, 2 bien respaldadas, 3
parciales, 1 contradicha por el propio fragmento y 2 no verificables -casi la mitad con algún
problema real. La regla de prompt de la Iteración 27 ("no afirmes una ausencia, decí «no se ve en
el fragmento»") ayuda pero sigue dependiendo de que el modelo se autocorrija; nada impedía que
igual redactara una afirmación plausible pero no respaldada.

- **Nuevo campo obligatorio `evidencia`** en la respuesta del analista: una cita textual de 4 a 20
  palabras del fragmento que respalde `que_hizo`. Se verifica **en código** (`_evidence_supported`,
  `vector_search.py`) -normalizada por mayúsculas/acentos/puntuación para tolerar ruido de
  transcripción trivial, pero exige que sea una subcadena real, no una paráfrasis- y con un mínimo
  de 4 palabras para que una coincidencia trivial ("el cliente") no cuente como respaldo. Si no
  verifica (o no viene), se descarta `que_hizo` -nunca se muestra una afirmación sin cita real. Sin
  llamada extra: mismo prompt, misma cantidad de tokens de salida (la cita ya estaba implícita en el
  razonamiento del modelo), sólo un chequeo de código antes de anotar el resultado.
- **Alcance deliberadamente acotado a `que_hizo`**: es la única nota con una sola fuente de verdad
  tratable por código (un fragmento). `situacion`/`como_termino` (bajo riesgo, describen el momento o
  el desenlace, no una acción específica del vendedor) y `contraste` (sintetiza patrones entre varios
  fragmentos de un grupo, no tiene una cita única que lo respalde) siguen mitigados sólo por prompt.
- **Verificado en vivo contra `mens_fashion_alto`** (Ubaldo Ramos, mismo caso de la auditoría
  original): de 6 conversaciones del vendedor, sólo **2 de 6** conservaron `que_hizo` con evidencia
  verificada (las otras 4 quedaron con `situacion`/`como_termino` pero sin la afirmación de qué hizo
  el vendedor); de los 4 compañeros, **1 de 4**. Es una caída fuerte frente a "10 de 10 con nota"
  antes de este cambio -mide directamente cuánto de lo que antes se mostraba como diagnóstico no
  tenía en realidad una cita real detrás en este cliente (transcripciones muy ruidosas, mucho
  "Speaker 0/Speaker 0" sin turnos claros). **Trade-off explícito, no un bug**: se prioriza no
  inventar por sobre la densidad de la nota -para un cliente con transcripciones más limpias la
  proporción verificada debería ser mayor (no medido todavía; próximo paso si hace falta
  cuantificarlo por cliente). 6 tests nuevos (`EvidenceSupportedTests`) + 4 tests existentes
  actualizados en `test_vector_search.py`; 485/485 en verde.
- **Medición de reintentos post-fix de la Iteración 27** (pedido explícito, "corré lo de retry
  reason"): el log de producción (`gemini_calls.jsonl`) no tenía actividad real posterior al último
  commit de los fixes de esa ronda -sólo datos de mientras se desarrollaban. Se generaron 5
  interacciones reales nuevas (coaching e indicadores, mens_fashion/farma24/roberts): 1 de 5
  (Ubaldo Ramos, coaching) igual disparó "conclusión de certeza sin evidencia suficiente" pese a la
  regla de lenguaje de la Iteración 27 -la regla de prompt reduce pero no elimina el problema; costo
  de esa interacción con reintento: US$0,057 vs. US$0,01-0,04 de las otras 4.

- **Hallazgo aparte, resuelto el mismo día**: `interaction_outcomes.jsonl` estaba contaminado de
  nuevo con eventos de test -literal `session_id="session-test"` encontrado en el archivo real
  (de `test_vi_agent.py`, un test que no pasa `interaction_outcome_recorder` explícito y depende
  del fixture de sesión de `conftest.py`)-, pese al fixture de aislamiento del 2026-09-15. **Causa
  raíz confirmada**: ese fixture es `autouse` de PYTEST -si un archivo de test se ejecuta
  directamente (`python "5. tests/test_vi_agent.py"`, sin pasar por pytest), pytest nunca corre y el
  fixture jamás se activa, así que `configure_client()` sigue apuntando al archivo real. Pasó de
  verdad en algún punto de esta sesión. `gemini_calls.jsonl` no tenía el mismo problema (los tests
  que escriben ahí sí pasan un `UsageRecorder` explícito a un path temporal en cada caso).
  **Arreglado con una salvaguarda independiente de pytest** (`vi_agent.py`): si el archivo que
  Python ejecuta como programa principal (`sys.argv[0]`) vive en `5. tests/`, `USAGE_LOG_PATH`/
  `INTERACTION_LOG_PATH` apuntan a un directorio temporal desde el arranque del módulo, sin
  depender de ningún fixture -cubre pytest, `unittest.main()` directo, y el botón "Run" de un IDE
  por igual. Verificado en vivo: `python "5. tests/test_vi_agent.py"` (110 tests, sin pytest) corrió
  limpio y el log real no registró ninguna línea nueva. Limpieza del archivo real: 19 líneas con la
  firma literal de test removidas (backup en
  `interaction_outcomes.jsonl.bak_before_cleanup_2026-09-22`); las ~800 sesiones de un solo evento
  del 2026-09-21 con IDs aleatorios (probablemente una corrida de gate/evaluación real, no
  confirmado con certeza) se dejaron intactas a propósito -sin poder probar que son ruido, borrarlas
  arriesgaba destruir señal real. 490/490 tests en verde (485 de la ronda anterior + los de la
  verificación de evidencia + éste, sin tests nuevos dedicados a la salvaguarda -se verificó
  ejecutando la suite real, no con un test unitario del guard en sí).

- **Segundo hallazgo del mismo chequeo, también corregido**: la primera versión de la verificación
  de `evidencia` (ítem anterior) rechazaba citas que SÍ estaban en el fragmento -ver
  `_MIN_EVIDENCE_WORDS` y `_collapse_repeated_speaker_turns` en `vector_search.py` para el detalle
  completo. Con el fix, una corrida en vivo sobre el mismo caso de Ubaldo Ramos pasó de 3/10 a 7/10
  notas "qué hizo" verificadas -los 3 casos que siguen sin verificar son ruido real de la
  transcripción (una diarización que le asigna erróneamente varios números seguidos a hablantes
  distintos) o el analista parafraseando un número en palabras en vez de citarlo tal cual aparece
  (ej. "tres mil quinientos" vs. "$3.500" en el fragmento) -correctamente rechazados, no son citas
  textuales.

### Iteración 29 (2026-09-22): nunca mostrarle al usuario una cita textual, ni pedida explícitamente

Pedido explícito de Lucas: "no quiero que se le muestren al usuario citas textuales de la
transcripción" -alcance confirmado como TOTAL, incluyendo cuando la pregunta pide expresamente un
ejemplo/cita/audio (antes esa era la única excepción permitida, ver Iteración 27).

- **`incluir_fragmentos` ya no es controlable por el modelo**: se sacó del `search_conversations`
  expuesto a Gemini en `vi_agent.py` (la tool ya no lo recibe como parámetro) y la llamada interna a
  `VectorSearchRepository.search` fuerza `incluir_fragmentos=False` siempre. El texto crudo de la
  transcripción NUNCA llega al contexto del modelo principal -no es sólo una regla de prompt, el
  dato ni siquiera está disponible para copiar. `VectorSearchRepository.search` conserva el
  parámetro para uso interno/depuración (los scripts de auditoría de esta sesión lo siguen usando).
- **Prompt**: se reemplazó "CITAR SÓLO CUANDO SE PIDE" por "NUNCA CITES TEXTUAL" (sin excepción) -si
  piden la cita exacta, la respuesta explica que no se muestran transcripciones y ofrece el resumen
  del caso en su lugar.
- **Segunda capa, en código** (`response_policy.py`, no sólo prompt): `client_answer_violations`
  detecta cualquier tramo entre comillas (rectas o «») de 4 o más palabras y lo trata como "cita
  textual de una conversación" -mismo mecanismo de reintento (`client_safe_rewrite`) que ya existe
  para fugas técnicas e identificadores internos. Umbral de 4+ palabras para no disparar con una
  frase de negocio corta ("tres por dos"). Sin esto, la única defensa sería que el modelo respete el
  prompt -con la fuente de datos ya cortada (punto anterior) el riesgo residual es bajo, pero esta
  capa cubre el caso de que igual redacte algo entre comillas sin haber leído nada real.
- **BUG REAL encontrado en la verificación en vivo, corregido antes de terminar**: los bloques
  ```vera-suggestions``` (chips de preguntas de seguimiento) son un array JSON de strings entre
  comillas dobles -sin excluirlos, CUALQUIER respuesta con sugerencias de 4+ palabras hubiera
  disparado el bloqueo, forzando una reescritura en casi todas las respuestas sin ninguna cita real
  de por medio. Corregido sacando todo bloque ```...``` (mismo patrón `OTHER_FENCES` que ya usa
  `answer_verification.py`) antes de buscar comillas.
- **Verificado en vivo contra `farma24_alto`**: "mostrame ejemplos textuales... con la cita exacta"
  -la primera corrida (antes del fix de `vera-suggestions`) mostró el bloqueo disparando por error
  sobre las sugerencias; la segunda, después del fix, devolvió coaching por patrones sin ninguna
  cita y el chequeo de comillas dio `[]`. 497/497 tests en verde (7 nuevos en
  `test_response_policy.py`, 2 en `test_vi_agent.py`).
### Iteración 30 (2026-09-22): "contraste" verificado por lado, contra cualquier fragmento de su grupo

Extensión aprobada por Lucas del mecanismo de la Iteración 28 al par "así falla el vendedor" / "así
lo hacen los mejores" -quedó fuera de alcance esa vez porque `contraste` sintetiza un patrón entre
VARIOS fragmentos de un grupo, no de uno solo como `que_hizo`.

- **Dos campos nuevos por par**: `evidencia_vendedor` y `evidencia_companeros`, cada uno una cita
  textual de 3 a 20 palabras que respalde ese lado del contraste. Verificación mecánica en
  `_judge_relevance` (`vector_search.py`): en vez de comparar contra UN fragmento, cada lado se
  verifica contra **cualquiera** de los fragmentos de SU propio grupo (`fragmentos_por_grupo`,
  agrupado por la clave `grupo` que ya trae cada resultado) -reusa `_evidence_supported` tal cual,
  sin llamada extra a Gemini. Si un lado no verifica, se descarta el PAR completo (un contraste con
  un solo lado respaldado no compara nada).
- Una cita real del grupo equivocado (ej. una del lado "companeros" puesta como
  `evidencia_vendedor`) no cuenta -cada lado se valida sólo contra los fragmentos de su propio
  grupo, nunca contra el del otro lado, para que no se pueda usar el comportamiento real de un
  compañero como si respaldara una afirmación sobre el vendedor.
- 6 tests nuevos (`ContrasteEvidenceTests`) cubriendo: par respaldado en ambos lados, cita que
  matchea un fragmento que no es el primero del grupo, descarte por falta de evidencia en cada lado
  por separado, ausencia total de los campos de evidencia, y evidencia real pero del grupo
  equivocado. 503/503 tests en verde.
- **Verificado en vivo contra `mens_fashion_alto`/Ubaldo Ramos**: de 2 pares de contraste que el
  analista proponía antes de este cambio (sin verificar), quedó **1 de 1** con evidencia real en
  ambos lados -el otro no llegó a proponerse esta vez (no hay forma de saber si el analista antes lo
  hubiera inventado sin evidencia real; lo que sí se confirma es que el que quedó tiene una cita
  textual real por cada lado).

### Iteración 31 (2026-09-22): panel de audio -dos bugs reales, retomados tras verificar en vivo el cambio de citas

Verificando en vivo la Iteración 29 en la demo (pregunta con `comparar_con_mejores` real contra
Mens Fashion) apareció el panel "🔊 Escuchar conversaciones citadas (8)" -pendiente de una sesión
anterior (screenshot con nombres reales de compañeros, conteo que no coincidía con lo citado en el
texto, y fecha ISO cruda). Con el modelo ya sin citar texto por default (Iteración 29), el enfoque
cambia: no tiene sentido "filtrar por lo citado en el texto" cuando el texto casi nunca menciona
tienda/fecha de un caso puntual -se ataca lo que sigue siendo un bug real.

- **Bug real encontrado en el camino, no buscado**: `_extract_citable_conversations`
  (`streamlit_app.py`) sólo leía la clave `resultados` del payload de `search_conversations` -en
  modo `comparar_con_mejores` eso es SÓLO el grupo del vendedor coacheado; el grupo `companeros`
  (las conversaciones de los compañeros con mejor resultado, con sus nombres reales -confirmado con
  Lucas que eso queda así a propósito) nunca llegaba al panel. Corregido: ahora lee ambos grupos,
  deduplicando por `conversation_id` igual que antes.
- **Título renombrado, no filtrado**: "Escuchar conversaciones citadas" pasó a "Escuchar
  conversaciones analizadas" -con el modelo sin citar texto por default (Iteración 29), "citadas"
  describía mal lo que el panel siempre mostró en realidad: las conversaciones que la tool encontró
  y usó como insumo, se nombren o no por tienda/fecha en el texto. Filtrar por mención textual
  hubiera dejado el panel vacío en la mayoría de las respuestas de coaching, quitándole el valor de
  poder escuchar el respaldo real de un análisis aunque no se haya pedido un ejemplo puntual.
- **Fecha legible**: `_format_conversation_date` convierte el ISO crudo
  ("2026-07-25T20:53:37.809000+00:00") a "25/07/2026 20:53" -mejor esfuerzo, si no parsea devuelve
  el valor original sin romper el render.
- 8 tests nuevos (`ExtractCitableConversationsTests`, `FormatConversationDateTests`). 509/509 tests
  en verde.

### Iteración 32 (2026-09-22): falso positivo real en `CONFIDENT` -"definitivo/a" no es certeza estadística

Pedido explícito: "¿se pueden bajar los costos todavía?". La medición de reintentos de la
Iteración 28 había mostrado 1 de 5 interacciones reales (coaching de Ubaldo Ramos) disparando
"conclusión de certeza sin evidencia suficiente" pese a la regla de lenguaje de la Iteración 27 -sin
diagnosticar la causa exacta en ese momento. Se investigó sin gastar en una llamada nueva: se probó
el regex `CONFIDENT` (`answer_verification.py`) contra frases de coaching realistas.

- **Hallazgo**: `definitiv[oa]` disparaba con frases de venta completamente normales -"Falta avanzar
  hacia una decisión definitiva del cliente" y "lograr un cierre definitivo cuando el cliente ya
  validó la prenda"-, ninguna con cifras ni certeza estadística de por medio, incluso pasando el
  chequeo de negación ya existente. Mismo patrón de falso positivo que "tecnología" en
  `response_policy.py` (ver esa iteración histórica): una palabra de negocio corriente confundida
  con lenguaje prohibido.
- **Corregido**: se sacó `definitiv[oa]` de `CONFIDENT`, dejando el resto de la lista (sin duda,
  estadísticamente significativo, muestra representativa/suficiente, garantiza, demuestra
  concluyentemente) -términos con mucha menos ambigüedad en este dominio.
- **Impacto esperado, no remedido en vivo todavía**: cada reintento evitado ahorra una llamada
  completa al modelo real (~US$0,02-0,03 según lo medido en la Iteración 28) -en clientes de retail
  donde "cierre" es el criterio de checklist más común (Mens Fashion, Roberts, Boggi, Dalton...),
  "cierre definitivo"/"decisión definitiva" es vocabulario esperable en casi cualquier coaching
  sobre ese criterio, así que el ahorro debería notarse en varios clientes, no sólo en el caso
  puntual encontrado.
- 4 tests nuevos (`ConfidentLanguageTests` en `test_answer_verification.py`): las dos frases reales
  que disparaban el falso positivo ya no lo hacen; el resto de `CONFIDENT` sigue detectando lenguaje
  de certeza genuino. 512/512 tests en verde.
- **Pendiente de verificar en vivo**: no se corrió de nuevo el caso real de Ubaldo Ramos después de
  este fix (para no seguir gastando en la misma investigación) -la próxima vez que se mida la tasa
  de reintentos real (ver Iteración 28), confirmar que bajó.

### Iteración 33 (2026-09-22, mismo día): el bloqueo de citas de la Iteración 29 tenía su propio falso positivo

Siguiendo la misma pregunta ("¿se puede bajar más el costo?"), se auditó el mecanismo agregado ESE
MISMO DÍA (Iteración 29) por si estaba introduciendo costo nuevo en vez de ahorrarlo -mismo método
que la Iteración 32 (probar el regex contra frases realistas, sin gastar en una llamada real antes
de confirmar el problema).

- **Hallazgo**: el umbral de sólo "4+ palabras entre comillas" de `_quoted_conversation_spans`
  disparaba con frases de negocio legítimas -nombre de sucursal ("Mens Fashion Patio Sendero
  Saltillo"), de criterio ("vendedor pregunta la ocasión de uso"), de indicador ("tasa de cierre de
  compra general")-, cada una forzando un `client_safe_rewrite` (una llamada completa al modelo
  real) sin ninguna cita de conversación de por medio. El mecanismo que se agregó para bajar el
  riesgo de citas hubiera terminado sumando costo por su cuenta.
- **Corregido**: `_DIALOGUE_MARKER` exige, ADEMÁS del umbral de palabras, una señal concreta de
  diálogo reconstruido dentro de la cita -signos de pregunta/exclamación, un pronombre personal
  (te/le/nos/me/usted/tú/vos/yo) o un verbo de habla reportada (dijo/preguntó/respondió/...). Un
  sustantivo de negocio no tiene ninguna de las dos cosas; una frase textual real de un cliente o
  vendedor casi siempre sí.
- Verificado en vivo (mismo caso de la Iteración 29, farma24_alto/Daniela Perez con pedido
  explícito de "cita exacta"): sigue sin mostrar ninguna cita, y el chequeo de comillas del texto de
  negocio real de la respuesta ahora da `[]` limpio. 5 tests nuevos (4 casos de negocio + 1 corregido
  para seguir exigiendo una marca de diálogo real). 513/513 tests en verde.

### Iteración 34 (2026-09-22, mismo día): "resumen_verificado" es peso muerto desde que no se cita

Siguiendo la búsqueda de más ahorro, se midió en vivo (mens_fashion_alto/Ubaldo Ramos, instrumentando
`chat.get_history()` turno por turno) qué pesa realmente dentro de una interacción: de los tres tools,
`get_business_rules` (7.917 bytes, texto estático del rulebook) y `search_conversations` (10.194
bytes) son los que dominan -y ese contenido se reenvía completo en cada turno siguiente de la misma
interacción porque el cache de contexto de Gemini sólo cubre el system_instruction, no el historial.

- **Explorado y descartado, no implementado**: mover `get_business_rules` al system_instruction
  cacheado. Encarecería el prompt cacheado de TODAS las preguntas de todos los clientes (no sólo las
  de coaching) -sin datos de qué proporción real de preguntas usan ese rulebook, podría empeorar el
  costo total en vez de mejorarlo. Recortar `search_conversations` a mitad de conversación reescribiendo
  el historial que gestiona el SDK del chat se descartó por el mismo motivo: alto riesgo de ingeniería
  (`answer_verification.py` lee ese mismo historial real para verificar cifras) por un ahorro chico
  (~$0,015-0,02 por interacción con varios turnos, ya con parte explicada por reintentos que se están
  bajando por otro lado).
- **Sí implementado, bajo riesgo**: `resumen_verificado` -campo que nació (2026-09-10) como segunda
  fuente para contrastar contra el fragmento antes de CITARLO- perdió su único motivo de existir del
  lado del modelo principal desde que nunca se cita texto (Iteración 29). No aparece en ninguna
  instrucción de `SYSTEM_INSTRUCTION_TEMPLATE` -el modelo principal no lo usa para nada. Se agrega al
  mismo descarte que ya tenía `fragmento_aproximado`: se saca cuando el resultado ya tiene `notas`
  (el insight viaja ahí), se conserva con `incluir_fragmentos` (uso interno/depuración) o cuando no
  hay notas (fail-open, mismo criterio de siempre).
- **Verificado en vivo, mismo caso de Ubaldo Ramos**: el payload de `search_conversations` bajó de
  10.194 a **7.346 bytes** (~28%) en esa búsqueda -se reenvía completo en cada turno siguiente de la
  interacción, así que el ahorro se multiplica por los turnos restantes. 2 tests nuevos
  (`test_resumen_verificado_is_also_dropped_when_notes_exist_unless_requested`,
  `test_resumen_verificado_is_kept_when_a_result_has_no_notes`). 515/515 tests en verde.

### Iteración 35 (2026-09-22, mismo día): 4 puntos de la lista de "cómo seguir bajando costos"

- **Costo de embeddings, invisible hasta ahora -corregido**: `_embed_query()` (una llamada real por
  cada búsqueda) nunca registraba nada en `gemini_calls.jsonl` -mismo patrón de punto ciego que el
  bug ya corregido de `tool_use_prompt_token_count` para RAG. Confirmado en vivo que
  `embed_content()` no devuelve ningún `usage_metadata`/`statistics` -a diferencia de
  `generate_content`. Se agregó `UsageRecorder.record_estimated()` (estima tokens client-side por
  palabras, marca `is_estimated=true` para no mezclarse con conteos exactos) y el precio de
  `gemini-embedding-001` (US$0,15/M de entrada, sin salida -fuente de terceros: futureagi.com,
  getmaxim.ai, embeddingcost.com; la doc oficial actual ya no lista este nombre exacto de modelo).
  Verificado en vivo: el evento aparece en el log real. Impacto en plata: mínimo (~US$0,0000015 por
  búsqueda) -el valor es la visibilidad, no el ahorro. 5 tests nuevos.
- **Desglose real por tool** (`usage_report.py --by-tool` sobre datos de hoy, 53 llamadas): todas
  son tráfico de prueba propio, no producción -con esa salvedad, `search_conversations` (US$0,098),
  `search_judge` (US$0,086), `run_readonly_sql` (US$0,073) y `evidence_repair` (US$0,043 en sólo 3
  llamadas, la más cara por llamada individual) son los rubros más grandes.
- **Sobre-disparo de `get_business_rules` -verificado en vivo, sin bug**: preguntas puramente
  diagnósticas ("¿cuál es la tasa de cierre de Ubaldo?") correctamente no disparan el rulebook, sólo
  `run_readonly_sql`. La salvaguarda ya existente en el prompt ("No lo hagas de forma especulativa")
  funciona como está diseñada. Sin cambios.
- **Bajar `top_k`/`PEER_TOP_K` -probado y NO adoptado**: comparación en vivo, mismo caso real
  (top_k=8/PEER_TOP_K=4 actual vs. 5/3 reducido): `que_hizo` verificado se mantuvo (10/12 vs. 7/8),
  pero **`contraste` verificado pasó de 1 par a 0** -con menos candidatos no alcanzó material para
  un contraste con evidencia real de ambos lados. Ahorro medido: US$0,0016 por búsqueda (el
  analista ya es el modelo barato). Mala relación costo/beneficio -se pierde el corazón del valor de
  coaching (la comparación vendedor/mejores) por un ahorro mínimo. Valores sin cambios.

- **Hallazgo chico, sin riesgo, el mismo día**: midiendo byte a byte qué campo pesa en el payload
  (`notas` 2.182 bytes, el resto metadata) apareció `distancia` con precisión completa de punto
  flotante (`0.22961762271533293`, 20 caracteres) mientras `distancia_relativa_al_mejor_resultado`
  -el mismo dato, en otra forma- ya iba redondeada a 4 decimales. Redondeado igual -4 decimales
  sigue siendo mucho más preciso que el rango real de distancias observado (~0.20-0.27); mismo
  redondeo también en el log interno de calidad de búsqueda, que lee el mismo dict. Bytes de menos
  en CADA resultado de CADA búsqueda, sin tocar nada que el modelo o el log realmente usen. 1 test
  nuevo. 516/516 tests en verde.
