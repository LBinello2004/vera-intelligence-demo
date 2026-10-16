# Cambios automáticos del Data Map de forever_21_bajo

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client forever_21_bajo --list` y después
`... --client forever_21_bajo --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-16T11:01:20Z · Promovido: V4 → V5

- Prompt `conversation_insights`: versión 7 → 10 (Langfuse, label production)
- Qué cambió en el Data Map:
  - descripciones modificadas en `categorical_insights`: `motivo_de_venta_perdida`
  - descripciones modificadas en `demography`: `tipointeraccion`
  - descripciones modificadas en `descriptive_insights`: `resumen_conversacion`
  - secciones modificadas: `routing`
- Changelog de la regeneración:

  > - Se actualizó el Data Map a V5 (5.0.0, 2026-10-16) por actualización del prompt de insights de v7 a v10.
  > - Se incorporaron las nuevas pautas de tipoInteraccion='compra': derivación a caja, modismos venezolanos ('cancelar'=pagar), medios de pago y datos de facturación sin requerir escuchar el cobro.
  > - Se actualizó la secuencia de resolución de motivo_de_venta_perdida (motivo explícito -> obstáculo comercial directo -> sin_urgencia_o_deja_pensar -> no_determinado como último recurso, sin inferir abandono de audios cortados).
  > - Se verificó en Postgres QA con run_readonly_sql que la base mantiene 1 única fila en las 6 vistas y que motivo_de_venta_perdida permanece en NULL en dicho registro histórico.

- Verificación: gate v2: pasó, 10 preguntas del banco dorado, 4 respondidas por el agente con el Data Map nuevo.
- Regenerado con `gemini-3.7-flash` en 1 intento(s).
- Archivo: `VI Data Map Forever21 V5.yaml` (anterior: `VI Data Map Forever21 V4.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client forever_21_bajo --to 4`
