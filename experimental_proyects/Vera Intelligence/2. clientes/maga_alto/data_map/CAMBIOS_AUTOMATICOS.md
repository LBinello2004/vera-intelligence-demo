# Cambios automáticos del Data Map de maga_alto

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client maga_alto --list` y después
`... --client maga_alto --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-09T14:26:03Z · Promovido: V2 → V6

- Prompt `sales_evaluation`: versión 15 → 16 (Langfuse, label production)
- Qué cambió en el Data Map:
  - descripciones modificadas en `performance`: `mencionaserviciosfuturos`, `ofreceadicionalnorelacionado`
  - secciones modificadas: `semantic_dependencies`
- Changelog de la regeneración:

  > Actualización de sales_evaluation (v15 -> v16): se incorporó la regla 1.0 de no obligatoriedad de secuencia temporal para los 9 pasos del checklist, clarificando la validez de menciones tempranas en PASO 7 (ofreceAdicionalNoRelacionado) y PASO 8 (mencionaServiciosFuturos) previas al cierre de venta.
  > Se verificaron empíricamente las distribuciones de los pasos 7 y 8 sobre 81.110 filas en vw_maga_rendimiento_vendedor (81,9% Sí en adicional no relacionado; 83,6% Sí en servicios futuros).
  > Se preservaron íntegras todas las fuentes del Data Map vigente, incluyendo dashboard_v2.vw_maga_insights_categoricos_por_producto y sus dependencias analíticas.

- Verificación: gate v2: pasó, 10 preguntas del banco dorado, 4 respondidas por el agente con el Data Map nuevo.
- Regenerado con `gemini-3.7-flash` en 4 intento(s).
- Archivo: `VI Data Map Maga V6.yaml` (anterior: `VI Data Map Maga V2.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client maga_alto --to 2`
