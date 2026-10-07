# Cambios automáticos del Data Map de huerpel_hostess_alto

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client huerpel_hostess_alto --list` y después
`... --client huerpel_hostess_alto --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-07T14:43:57Z · Promovido: V3 → V4

**Motivo:** candidato del 2026-09-30 rechazado por el gate viejo (ruido)

- Qué cambió en el Data Map:
  - descripciones modificadas en `conversation_insights`: `tipo_visita`
  - valores de enum modificados en `conversation_insights`: `tipo_visita`
  - secciones modificadas: `routing`
- Changelog de la regeneración:

  > Promoción manual 2026-10-07 tras re-evaluar con el gate corregido: candidato del 2026-09-30 rechazado por el gate viejo (ruido).

- Verificación: gate v2: pasó.
- Regenerado con `(ver archivo)` en ? intento(s).
- Archivo: `VI Data Map Huerpel Hostess V4.yaml` (anterior: `VI Data Map Huerpel Hostess V3.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client huerpel_hostess_alto --to 3`
