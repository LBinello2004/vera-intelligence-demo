# Cambios automáticos del Data Map de boggi_alto

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client boggi_alto --list` y después
`... --client boggi_alto --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-16T11:02:07Z · Promovido: V2 → V3

- Prompt `conversation_insights`: versión 4 → 5 (Langfuse, label production)
- Qué cambió en el Data Map:
  - valores de enum modificados en `categorical_insights`: `motivador_visita`
- Changelog de la regeneración:

  > - Actualización de metadata a V3 (3.0.0, 2026-10-16) por subida de versión de insights_prompt de v4 a v5.
  > - Se documentó la regla de atención principal para audios con múltiples atenciones en metadata.changes_from_v2.
  > - Se verificó en Postgres QA el estado actual de vw_boggi_insights_categoricos_generales (878 filas, 848 conversaciones distintas, 30 huérfanas) actualizando data_quality_note.
  > - Se verificaron valores observados de motivador_visita (aparece gala_cena_formal) y resultado_producto (comprado, no_comprado, pendiente, reemplazado).

- Verificación: gate v2: pasó, 10 preguntas del banco dorado, 2 respondidas por el agente con el Data Map nuevo.
- Regenerado con `gemini-3.7-flash` en 1 intento(s).
- Archivo: `VI Data Map Boggi V3.yaml` (anterior: `VI Data Map Boggi V2.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client boggi_alto --to 2`
