# Cambios automáticos del Data Map de farma24_alto

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client farma24_alto --list` y después
`... --client farma24_alto --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-09T14:23:47Z · Promovido: V10 → V11

- Prompt `sales_evaluation`: versión 18 → 19 (Langfuse, label production)
- Qué cambió en el Data Map:
  - descripciones modificadas en `performance`: `mencionaserviciosfuturos`, `ofreceadicionalnorelacionado`
  - secciones modificadas: `semantic_dependencies`
- Changelog de la regeneración:

  > Actualización a V11 (11.0.0, 2026-10-09) por subida del checklist a v19.
  > Se incorporó la regla 1.0 (el orden de los pasos no invalida su cumplimiento si ocurre antes del cierre) en decision_semantics, sale_closed_cutoff, y en las descripciones de ofreceadicionalnorelacionado (paso 7) y mencionaserviciosfuturos (paso 8).
  > Se verificó empíricamente contra Postgres QA que los enums observados de saludaalinicio, ofreceadicionalnorelacionado y mencionaserviciosfuturos se mantienen estables y sin variantes anómalas.

- Verificación: gate v2: pasó, 10 preguntas del banco dorado, 4 respondidas por el agente con el Data Map nuevo.
- Regenerado con `gemini-3.7-flash` en 1 intento(s).
- Archivo: `VI Data Map Farma24 V11.yaml` (anterior: `VI Data Map Farma24 V10.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client farma24_alto --to 10`
