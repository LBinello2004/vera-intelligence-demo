# Cambios automáticos del Data Map de shoe_box_bajo

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client shoe_box_bajo --list` y después
`... --client shoe_box_bajo --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-07T14:43:58Z · Promovido: V5 → V4

**Motivo:** candidato del 2026-10-05 rechazado por el gate viejo (ruido)

- Qué cambió en el Data Map:
  - descripciones modificadas en `categorical_insights`: `motivo_de_venta_perdida`
  - descripciones modificadas en `descriptive_insights`: `resumen_conversacion`
  - secciones modificadas: `routing`
- Changelog de la regeneración:

  > Promoción manual 2026-10-07 tras re-evaluar con el gate corregido: candidato del 2026-10-05 rechazado por el gate viejo (ruido).

- Verificación: gate v2: pasó.
- Regenerado con `(ver archivo)` en ? intento(s).
- Archivo: `VI Data Map Shoe Box V4.yaml` (anterior: `VI Data Map Shoe Box V5.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client shoe_box_bajo --to 5`
