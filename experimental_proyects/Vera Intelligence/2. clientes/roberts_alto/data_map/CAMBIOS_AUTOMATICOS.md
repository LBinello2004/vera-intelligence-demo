# Cambios automáticos del Data Map de roberts_alto

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client roberts_alto --list` y después
`... --client roberts_alto --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-16T11:05:33Z · Promovido: V4 → V5

- Prompt `conversation_insights`: versión 7 → 8 (Langfuse, label production)
- Qué cambió en el Data Map:
  - descripciones modificadas en `categorical_insights`: `cliente_llega_con_cashback_para_usar`, `conducta_vendedor_cashback`, `motivador_visita`, `origen_inspiracion_cliente`, `primer_mencion_cashback`, `resultado_general`, `tipo_interaccion`
  - descripciones modificadas en `categorical_product_insights`: `resultado_producto`
  - valores de enum modificados en `categorical_insights`: `motivador_visita`
  - valores de enum modificados en `categorical_product_insights`: `ocasion_de_uso`
  - secciones modificadas: `limitations`, `routing`, `semantic_dependencies`
- Changelog de la regeneración:

  > Actualización a Data Map Roberts V5 tras la subida de conversation_insights a v8 en Langfuse.
  > Se verificó empíricamente contra Postgres QA que las vistas de dashboard_v2 aún no exponen las columnas nuevas del prompt (tipo_mencion_cashback, momento_primera_mencion_cashback, rol_del_cashback_en_la_conversacion), documentándose como limitación crítica.
  > Se actualizaron las tasas y distribuciones de cashback en categorical_insights (44,9% NULL, 7.455 filas evaluadas) y el conteo de filas huérfanas (928 filas).
  > Se incorporaron las reglas de consistencia de cashback y las directrices para grabaciones con múltiples atenciones introducidas por la v8 del prompt.

- Verificación: gate v2: pasó, 10 preguntas del banco dorado, 4 respondidas por el agente con el Data Map nuevo.
- Regenerado con `gemini-3.7-flash` en 1 intento(s).
- Archivo: `VI Data Map Roberts V5.yaml` (anterior: `VI Data Map Roberts V4.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client roberts_alto --to 4`
