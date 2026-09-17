# Diez alternativas nuevas de costo o latencia

Revisión del 2026-09-14. Propuestas basadas en el código local, contrastadas con `8. README.md`,
`9. HISTORIAL.md` y las iteraciones de búsqueda vectorial. Los puntos 1 y 2 fueron implementados
a pedido del usuario; los puntos 3–10 siguen como propuestas. El ahorro de tokens y latencia
es una hipótesis a medir; el conflicto de gráficos del punto 1 se reprodujo y corrigió localmente.
No se ejecutaron llamadas pagas ni consultas a producción para preparar este documento.

## 1. Evitar reescrituras por la estructura válida de los gráficos

**Implementado y verificado (2026-09-14).** `_answer_policy_text` controla los textos de gráficos
aceptados por el parser, excluyendo su estructura; deja bloques inválidos crudos y decodifica
sugerencias. El mismo camino se aplica al streaming y al diagnóstico de términos para reescribir.
Tests verifican cuatro tipos afectados sin llamada de reescritura, fugas en títulos/etiquetas/series,
Unicode escapado y streaming dividido carácter por carácter. Se corrigió además el borde donde
el cierre del fence llegaba antes de su salto de línea. La descripción siguiente registra el problema original.

`vi_agent.run_tool_loop` pasa la respuesta cruda a `client_answer_violations` antes de extraer los
gráficos. El patrón de identificadores detecta `grouped_bar`, `stacked_bar` y `x_values`, aunque son
parte del contrato de visualización. Eso puede provocar hasta cuatro reescrituras y un fallback.

Propuesta: parsear y validar la estructura; aplicar la política al texto visible, títulos, etiquetas,
nombres de series y sugerencias. Excluir sólo claves y valores estructurales autorizados, nunca
ignorar indiscriminadamente un bloque JSON. Aplicar el mismo criterio al streaming.
Medir llamadas de reescritura y tiempo total con gráficos válidos y con fugas reales en sus etiquetas.
Beneficia costo y latencia en los casos afectados. Prioridad alta.

## 2. Devolver resultados SQL con los nombres de columnas una sola vez

**Implementado y verificado (2026-09-14).** El resultado ahora incluye `columns` y `rows` como listas
de valores; mantiene `row_count`, `truncated`, límite, nulos y conversiones existentes. La descripción
de la herramienta explica cómo interpretar el formato. Los caches se invalidan ante cambios de firma
o descripción para no reusar el contrato viejo. Suite completa: 260 tests en verde.
Fixture sintético de 200 filas y 4 columnas: 23.116 → 6.201 bytes (73,2% menos); ambos formatos
sin espacios: 71,2% menos. Equivalencia exacta de datos comprobada. No se midieron tokens facturados,
latencia real ni ahorro en producción; un resultado de una sola fila puede crecer por `columns`.
La descripción siguiente registra la propuesta original.

`run_readonly_sql` entrega hasta 200 filas como objetos que repiten todas las claves. Propuesta:
usar `columns` más filas de valores, conservando orden, tipos, nulos, `row_count` y `truncated`.
No cambiar ni truncar adicionalmente los datos. Reduce el tamaño del resultado enviado al modelo,
especialmente en desgloses largos. Adaptar también consumidores internos y evidencia numérica.
Medir tokens de entrada y exactitud de las respuestas con los mismos resultados SQL; no confundir
reducción de caracteres con ahorro de tokens demostrado. Prioridad alta.

## 3. Generar una versión operativa del Data Map para el prompt

`build_system_instruction` incorpora el YAML completo, incluidas cronologías de auditoría.
Propuesta: compilar una proyección que conserve fuentes, reglas, definiciones, enums, advertencias,
excepciones y referencias temporales necesarias, pero omita narración histórica redundante.
Mantener íntegro el archivo original; algunas reglas relevantes están dentro de metadata, por lo
que no se puede eliminar ese bloque entero. Medir tokens del contexto y resultados del banco dorado,
incluidas preguntas sobre taxonomías históricas. El cache existente reduce el ahorro marginal;
no asumir que todo token eliminado se facturaba a precio completo. Prioridad alta, riesgo semántico medio.

## 4. Entregar el criterio solicitado sin todo el prompt de extracción

`business_rules.py` devuelve el texto completo del rulebook autorizado. Propuesta: añadir una
selección determinística por criterio que incluya sus dependencias y excepciones, con fallback al
documento completo cuando no haya una delimitación segura. Es especialmente relevante para prompts
de Langfuse con instrucciones de extracción que no necesita la respuesta de negocio.
Medir tamaño del resultado, tokens de la siguiente llamada y fidelidad de las explicaciones.
No reducir a un resumen generado que pueda alterar la regla. Beneficia costo; efecto en latencia
por medir. Riesgo medio.

## 5. Reducir resultados antiguos de herramientas en conversaciones largas

El chat acumula el historial y sus resultados de herramientas. Propuesta: conservar completos los
turnos recientes y sustituir resultados antiguos voluminosos por un estado verificable con cifras,
filtros, períodos y referencias necesarias para recuperar detalle. Mantener correctamente asociados
los intercambios de function calling y sus firmas; no recortar mensajes aislados del SDK.
Medir costo por turno al crecer la conversación y exactitud de seguimientos como “comparalo con
el anterior”. La reducción sólo conviene si supera el costo de compactar y de volver a consultar.
Mayor complejidad y riesgo que los puntos 2–4. Base técnica: [function calling de Gemini](https://ai.google.dev/gemini-api/docs/function-calling).

## 6. Recuperar transcripciones completas después de elegir candidatos vectoriales

`vector_search.py` construye una consulta con transcripciones y enriquecimientos antes del límite
de candidatos; después descarta duplicados en Python. Propuesta: separar selección de IDs y distancia
de la recuperación de texto y resúmenes. Mantener exactamente tenant, elegibilidad por joins,
desempates, deduplicación y cantidad de resultados; que exista el SQL en ese orden no prueba por sí
solo el orden de ejecución del planificador. Medir `EXPLAIN (ANALYZE, BUFFERS)`, bytes transferidos y
latencia completa sobre el mismo conjunto de resultados. No depende de crear el índice pendiente.
Puede no aportar si el plan actual ya evita trabajo equivalente. [Referencia de PostgreSQL sobre CTEs y materialización](https://www.postgresql.org/docs/current/queries-with.html).

## 7. Evitar releer las configuraciones para dibujar el selector

`streamlit_app.main` obtiene nombres de clientes y vuelve a cargar configuraciones para sus marcas
de búsqueda vectorial. Propuesta: una sola lectura por versión de archivos y reutilizar esos datos
de presentación, invalidando al cambiar config o Data Map. No compartir el estado mutable del cliente
activo ni su chat. Medir lecturas YAML y tiempo local de rerun. Es una mejora de interfaz, sin ahorro
directo de tokens; probablemente pequeña con 19 clientes. [Cache de datos de Streamlit](https://docs.streamlit.io/develop/api-reference/caching-and-state/st.cache_data).

## 8. Leer incrementalmente el registro de uso de la barra lateral

La interfaz relee el JSONL completo para mostrar el consumo de una sesión. Propuesta: mantener
posición de lectura y acumulados por sesión, procesando sólo líneas nuevas y detectando reemplazo,
truncamiento y líneas incompletas. Medir tiempo de rerun y bytes leídos con logs crecientes; los
totales deben coincidir exactamente con una lectura completa. Reduce latencia local a medida que
crece el archivo; no reduce la factura de Gemini. Riesgo bajo.

## 9. Terminar el gate cuando el candidato ya está rechazado

El resultado final exige que todas las preguntas pasen, pero el bucle sigue después de un fallo
decisivo. Propuesta: modo de rechazo temprano que guarde el motivo y marque el resto como no
ejecutado; conservar un modo diagnóstico completo. Un candidato aprobado debe seguir pasando todo
el banco requerido. No selecciona preguntas por campos modificados: no es el gate incremental
descartado. Medir llamadas evitadas en candidatos rechazados, sin ahorro esperado en aprobados.
El tradeoff es obtener menos diagnósticos por corrida. Riesgo bajo.

## 10. Ejecutar acciones acotadas de la interfaz sin una llamada de planificación

Hoy un chip envía texto y el modelo debe decidir qué herramienta y consulta usar. Propuesta: para
unas pocas acciones inequívocas, asociar un identificador de acción con SQL validado y parámetros
explícitos de período/cliente; ejecutar y pedir al modelo sólo la explicación. Empezar con una acción
simple, no con coaching o descubrimiento. Las sugerencias libres mantienen el flujo actual.
No guardar respuestas: consultar datos actuales cada vez. Medir llamadas y latencia frente al flujo
normal, con equivalencia de filtros y resultados. Requiere mantener la definición de cada acción
ante cambios del Data Map; dificultad media/alta.

## Cómo decidir

Empezar por 1 y 2; ensayar 3 después de inventariar dónde viven las reglas del Data Map. Comparar
variantes con las mismas preguntas, clientes y resultados de herramientas cuando corresponda;
separar contextos fríos y calientes. Registrar tokens por categoría, llamadas, errores, corrección
y latencia mediana/p95 con suficientes repeticiones. Usar el estimador actual para comparación,
sin presentar sus coeficientes como una tarifa verificada ni el ahorro proyectado como factura real.
