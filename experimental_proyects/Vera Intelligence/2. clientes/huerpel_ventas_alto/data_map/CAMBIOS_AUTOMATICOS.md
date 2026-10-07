# Cambios automáticos del Data Map de huerpel_ventas_alto

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client huerpel_ventas_alto --list` y después
`... --client huerpel_ventas_alto --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-07T14:43:57Z · Promovido: V3 → V4

**Motivo:** candidato del 2026-10-02 rechazado por el gate viejo (ruido)

- Qué cambió en el Data Map:
  - campos agregados en `performance`: `manejoobjeciondisponibilidadentrega`, `manejoobjecionfinanciamientomensualidad`, `manejoobjecionpreciovalor`, `manejoobjecionvehiculoproducto`
  - descripciones modificadas en `performance`: `prompt_id`
  - secciones modificadas: `decision_flow`, `routing`
- Changelog de la regeneración:

  > Promoción manual 2026-10-07 tras re-evaluar con el gate corregido: candidato del 2026-10-02 rechazado por el gate viejo (ruido).

- Verificación: gate v2: pasó.
- Regenerado con `(ver archivo)` en ? intento(s).
- Archivo: `VI Data Map Huerpel Ventas V4.yaml` (anterior: `VI Data Map Huerpel Ventas V3.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client huerpel_ventas_alto --to 3`
