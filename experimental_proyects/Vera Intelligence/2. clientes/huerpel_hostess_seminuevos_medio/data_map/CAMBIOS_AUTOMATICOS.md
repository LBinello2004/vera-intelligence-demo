# Cambios automáticos del Data Map de huerpel_hostess_seminuevos_medio

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client huerpel_hostess_seminuevos_medio --list` y después
`... --client huerpel_hostess_seminuevos_medio --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-07T14:43:58Z · Promovido: V3 → V4

**Motivo:** corrección manual de tipos boolean->text, verificada con SQL

- Qué cambió en el Data Map:
  - descripciones modificadas en `conversation_insights`: `aplica_registro_completo`, `registro_completo`
- Changelog de la regeneración:

  > Promoción manual 2026-10-07 tras re-evaluar con el gate corregido: corrección manual de tipos boolean->text, verificada con SQL.

- Verificación: gate v2: pasó.
- Regenerado con `(ver archivo)` en ? intento(s).
- Archivo: `VI Data Map Huerpel Hostess Seminuevos V4.yaml` (anterior: `VI Data Map Huerpel Hostess Seminuevos V3.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client huerpel_hostess_seminuevos_medio --to 3`
