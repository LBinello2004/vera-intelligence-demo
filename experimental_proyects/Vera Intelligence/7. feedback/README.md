# Feedback de respuestas (👍/👎)

Registro versionado en git de los votos 👍/👎 que cualquiera que use la interfaz web
(`4. scripts/streamlit_app.py`) deja en cada respuesta de Vera Intelligence. Ver
`4. scripts/feedback_tracking.py` para el mecanismo exacto y por qué se decidió versionarlo en git
en vez de dejarlo como el resto de los logs locales del proyecto (`.runtime/`, gitignored).

## Por qué existe

Agregado 2026-09-11, a pedido explícito: hasta ese momento, toda la iteración de prompts del
proyecto se basaba en casos puntuales que alguien notaba a mano (ver `9. HISTORIAL.md`), nunca en
una señal agregada de qué respuestas realmente sirven en uso real. Cada línea de
`feedback_events.jsonl` es un voto real con la pregunta y la respuesta completa que lo motivó -sin
eso el feedback no sirve para saber **qué** estuvo bien o mal, sólo que algo lo estuvo.

## Formato

Un objeto JSON por línea (JSONL), append-only:

```json
{"schema_version":1,"recorded_at":"...","client_id":"...","session_id":"...","message_id":"...","vote":"up"|"down","question":"...|null","answer":"...","tool_names":["..."]}
```

`question` es `null` cuando el voto es sobre el saludo inicial (no responde a una pregunta puntual
del usuario). `tool_names` es la lista deduplicada y ordenada de herramientas técnicas que se
usaron para esa respuesta (ej. `run_readonly_sql`, `get_business_rules`, `search_conversations`).

## Cómo usarlo

Todavía no hay un script de análisis dedicado -son datos crudos pensados para filtrar/leer a mano
(ej. `grep '"vote":"down"'`) hasta que haya volumen suficiente para justificar herramienta propia.
Cargar con `feedback_tracking.load_feedback_events(FEEDBACK_LOG_PATH)` desde Python si hace falta
procesarlo con código.
