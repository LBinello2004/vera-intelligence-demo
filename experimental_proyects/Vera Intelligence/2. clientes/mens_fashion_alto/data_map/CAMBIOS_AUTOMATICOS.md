# Cambios automáticos del Data Map de mens_fashion_alto

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client mens_fashion_alto --list` y después
`... --client mens_fashion_alto --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-16T11:03:25Z · Promovido: V10 → V11

- Prompt `conversation_insights`: versión 9 → 11 (Langfuse, label production)
- Qué cambió en el Data Map:
  - descripciones modificadas en `categorical_insights`: `cliente_llega_con_cashback_para_usar`, `conducta_vendedor_cashback`, `primer_mencion_cashback`
  - valores de enum modificados en `categorical_product_insights`: `motivo_venta_perdida_producto`
  - secciones modificadas: `decision_flow`, `limitations`, `routing`, `semantic_dependencies`
- Changelog de la regeneración:

  > Actualización a V11 (2026-10-16) motivada por la subida del prompt insights de v9 a v11.
  > Se verificó empíricamente contra Postgres QA que las columnas tipo_mencion_cashback, momento_primera_mencion_cashback y rol_del_cashback_en_la_conversacion no existen físicamente en las vistas dashboard_v2, documentándose como consistencias semánticas del prompt sin agregarlas como campos queryables.
  > Se actualizó la regla de decision_flow para auditar atenciones múltiples en una misma grabación según la directiva de v11 (describir solo la atención principal).
  > Se revalidó la distribución empírica de primer_mencion_cashback, cliente_llega_con_cashback_para_usar y conducta_vendedor_cashback, actualizando el conteo de NULLs y filas totales.

- Verificación: gate v2: pasó, 10 preguntas del banco dorado, 4 respondidas por el agente con el Data Map nuevo.
- Regenerado con `gemini-3.7-flash` en 1 intento(s).
- Archivo: `VI Data Map Mens Fashion V11.yaml` (anterior: `VI Data Map Mens Fashion V10.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client mens_fashion_alto --to 10`
