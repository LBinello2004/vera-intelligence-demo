# Cambios automáticos del Data Map de shoe_box_bajo

Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el
mismo commit que este archivo. Para volver atrás: `python "4. scripts/revert_data_map.py" --client shoe_box_bajo --list` y después
`... --client shoe_box_bajo --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).

## 2026-10-16T11:03:30Z · Promovido: V4 → V6

- Prompt `conversation_insights`: versión 7 → 10 (Langfuse, label production)
- Qué cambió en el Data Map:
  - descripciones modificadas en `categorical_insights`: `hubo_falta_stock`, `hubo_ofrecimiento_alternativa`, `hubo_ofrecimiento_complementario`, `motivo_de_venta_perdida`
  - descripciones modificadas en `demography`: `tipointeraccion`
  - descripciones modificadas en `descriptive_insights`: `resumen_conversacion`
  - valores de enum modificados en `categorical_insights`: `motivo_de_venta_perdida`
  - secciones modificadas: `limitations`, `routing`
- Changelog de la regeneración:

  > Se actualizó el Data Map a V6 (6.0.0, 2026-10-16) tras el cambio de insights a v10:
  > - Se documentaron las nuevas señales operativas para tipoInteraccion="compra" (basta una señal, avance a caja, "cancelar"=pagar, medios de pago venezolanos).
  > - Se incorporó la secuencia de decisión para motivo_de_venta_perdida (inferencias comerciales directas fundamentadas, sin_urgencia_o_deja_pensar y no_determinado como última opción).
  > - Se verificó empíricamente contra Postgres QA el volumen total (2.821 conversaciones, 2.157 analizables, 1.890 con motivo_de_venta_perdida no nulo y 931 NULL históricos).
  > - Se verificó la ampliación de valores observados en motivo_de_venta_perdida: aparecieron objecion_estilo_o_diseno (6), objecion_calidad_o_autenticidad (1) y condiciones_cambio_o_garantia (1).

- Verificación: gate v2: pasó, 6 preguntas del banco dorado, 4 respondidas por el agente con el Data Map nuevo.
- Regenerado con `gemini-3.7-flash` en 1 intento(s).
- Archivo: `VI Data Map Shoe Box V6.yaml` (anterior: `VI Data Map Shoe Box V4.yaml`)
- Para revertir: `python "4. scripts/revert_data_map.py" --client shoe_box_bajo --to 4`

---

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
