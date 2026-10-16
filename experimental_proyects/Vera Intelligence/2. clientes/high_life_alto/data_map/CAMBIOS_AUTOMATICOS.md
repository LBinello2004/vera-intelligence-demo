# Cambios automáticos del Data Map de high_life_alto

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client high_life_alto --list` y después
`... --client high_life_alto --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-16T11:01:52Z · Promovido: V5 → V6

- Prompt `conversation_insights`: versión 7 → 8 (Langfuse, label production)
- Qué cambió en el Data Map:
  - valores de enum modificados en `categorical_insights`: `motivador_visita`
  - valores de enum modificados en `categorical_product_insights`: `ocasion_de_uso`
- Changelog de la regeneración:

  > - Actualización de conversation_insights a versión v8 (metadata V6, 6.0.0, fecha 2026-10-16).
  > - motivador_visita: actualizados configured_values a la taxonomía v8 (boda, graduacion, cumpleanos_aniversario, trabajo, evento_formal_social, regalo, reposicion_renovacion, uso_cotidiano, viaje, otra_necesidad_especifica, no_claro); verificado en Postgres QA que conviven valores legados (necesidad_especifica, promocion, aparador, bautismo_comunion).
  > - ocasion_de_uso: actualizados configured_values a la taxonomía simplificada v8 (boda, trabajo, casual, evento, regalo, no_claro); verificado empíricamente que evento, casual, boda, trabajo, regalo y bautismo_comunion (remanente v7) están observados en base.
  > - Incorporado bloque metadata.hallazgos_criticos_verificados_2026-10-16 documentando los conteos y la distribución observada.

- Verificación: gate v2: pasó, 10 preguntas del banco dorado, 0 respondidas por el agente con el Data Map nuevo.
- Regenerado con `gemini-3.7-flash` en 1 intento(s).
- Archivo: `VI Data Map High Life V6.yaml` (anterior: `VI Data Map High Life V5.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client high_life_alto --to 5`
