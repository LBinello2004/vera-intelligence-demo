# Robustez de respuestas — 2026-09-14

Se implementaron exclusivamente los puntos pedidos: validar cifras y reconocer información insuficiente. No se añadieron otras capacidades.

## Verificación local

answer_verification.py valida valores numéricos de filas SQL, sin tomar números de IDs, nombres o metadata. Los conteos son exactos; decimales y porcentajes toleran solo el redondeo de la precisión escrita. Porcentajes no se respaldan con un conteo del mismo valor: requieren una columna de tasa/porcentaje o cálculo referenciado. Incluye valores de gráficos, fechas reconocidas, separadores de miles/decimales y referencias previas del historial SDK.

Para derivados se admite un bloque interno vera-evidence, retirado antes de mostrar la respuesta. Referencias por id, fila y columna; operaciones identity, sum, mean simple, difference, ratio, percentage y relative_change calculadas con Decimal. Rechaza referencias/operaciones inválidas, cero en denominadores, números no finitos y cálculos incorrectos. Sin eval ni ejecución de código. verification.id se agrega junto a result en la respuesta de la herramienta; el JSON que retorna run_readonly_sql no cambia.

Las cifras de resultados sin evidencia disponible se rechazan. Las definiciones numéricas de reglas se admiten en contexto normativo si fueron consultadas y su cifra aparece en el rulebook. No se usan como resultados observados.

La validación se aplica antes de devolver/publicar cifras. Máximo un turno de reparación de evidencia, registrado como evidence_repair. Si vuelve a fallar, salida local sin cifras inválidas y registro del rechazo en el contexto. La política de información interna conserva su control separado. Una respuesta correcta no necesita una llamada adicional para validarse.

## Suficiencia

- Detalle truncado: advertencia local; algunas afirmaciones de ranking completo/ausencia se bloquean si todos los resultados son parciales. Preferir una agregación completa a extrapolar las filas del detalle.
- Sin registros: no equivale a incumplimiento.
- Métricas nulas/sin observaciones: no presentar como cero ni como desempeño evaluado.
- Bases negativas/fraccionarias: inválidas como cantidad evaluada. Denominadores monetarios no se llaman tamaño de muestra.
- Una observación: advertencia contra generalizar al equipo.
- Tasas/promedios sin base: falta de información para establecer solidez. Bases conocidas: informar las pertinentes y no repetir las ya citadas. No se inventa un mínimo de muestra ni se afirma suficiencia porque n sea mayor que uno.
- Algunas afirmaciones explícitas de certeza, representatividad o significancia se rechazan; negaciones válidas no fuerzan corrección.

La instrucción pide bases efectivamente evaluadas por criterio/grupo, excluyendo N/A y NULL según el mapa. Los criterios permanecen completos; Data Maps intactos.

## Costos y límites

El transporte sigue en streaming y conserva cancelación. Respuestas basadas en datos/cifras se retienen hasta completar la validación; prefijos sin cifras previos a la evidencia pueden publicarse. El bloque interno nunca llega a la interfaz. Puede retrasar el primer texto visible: no se promete mejorar latencia.

Pedir bases y declaraciones de cálculo puede aumentar tokens; una falla puede añadir un turno de reparación acotado a uno. Este cambio busca confiabilidad, no ahorro de tokens.

No prueba que SQL eligió el grano, denominador, etiquetas o filtros de negocio correctos; se mantiene el Data Map y el validador readonly/tenant para esas reglas. Una cifra presente en una celda no demuestra su asociación semántica con cualquier frase ni la exactitud de toda conclusión cualitativa. El parser no cubre toda forma de número en lenguaje natural: formas no soportadas pueden necesitar corrección. No se calculan significancia ni intervalos estadísticos sin diseño de análisis.

## Validación

334 tests: errores de cálculo, cifras pequeñas inventadas, metadata usada como evidencia, conteos cercanos incorrectos, redondeo, fechas/escalas, bases desconocidas/inválidas/de una observación, nulos/ceros, detalle parcial, gráficos, referencias históricas, confianza negada, reparación acotada y ausencia de cifras inválidas en streaming. Mocks anteriores de política/runtime usan evidencia o respuestas sin métricas ficticias; no siguen permitiendo alucinaciones deliberadamente.

Dos preguntas reales de Men's Fashion (7–13 septiembre2026), conteo y desempeño: 2 llamadas al modelo cada una, sin evidence_repair. SQL y criterios se pidieron juntos en desempeño. Costo estimado por metadata: US$0,00477975 y US$0,03345000; tiempos4,832s y17,610s. No es un benchmark de ahorro ni una auditoría independiente del SQL.

Revisión local posterior con guardas finales: ambas respuestas sin errores. Tasas citadas 5,2%/77 observaciones y25,7%/1797, coincidentes con filas respectivas. Se retiró una advertencia que repetía bases de otros indicadores, sin modificar cifras. Últimas guardas verificadas sin llamadas pagas adicionales.

Streamlit AppTest con backend simulado: agradecimiento local sin generación y pregunta normal con una generación, sin excepciones ni servicios externos. Evidencia privada gitignored: .runtime/runtime_smoke/robustness_20260914/{calls.jsonl,summary.json,*_trace.json,*_verified_answer.txt,*_local_final.txt,local_verification.json}.
