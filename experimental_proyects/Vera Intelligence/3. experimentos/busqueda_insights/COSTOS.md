# Desglose de costos por pregunta (medido, 2026-10-13)

Fuente: `.runtime/usage/gemini_calls.jsonl`, turnos del agente entre 2026-09-15 y 2026-10-12 (2.155 turnos), precios de `usage_tracking.estimate_cost_usd`.
Responde a la duda de la reunión semanal del 13-oct: ¿el costo está en el orquestador o en el reprocesamiento?

## Una pregunta típica (US$ por turno)

| Tipo de turno | n | Promedio | Mediana | p90 |
|---|---|---|---|---|
| Solo SQL (sin búsqueda) | 1.777 | 0,0154 | 0,0119 | 0,0300 |
| Con búsqueda (sin contar juez ni embedding) | 378 | 0,0394 | 0,0330 | 0,0755 |

Reparto del costo del turno:

| Componente | Solo SQL | Con búsqueda |
|---|---|---|
| Modelo principal leyendo resultados de herramientas (`tool_results`) | 56 % (0,0086) | 61 % (0,0241) |
| Primera llamada del modelo (`initial`) | 23 % (0,0036) | 16 % (0,0065) |
| Reparación de evidencia (`evidence_repair`, segunda pasada) | 19 % (0,0029) | 21 % (0,0083) |
| **Orquestador / planificador** | **2 % (0,0002)** | **1 % (0,0002)** |
| Reescritura segura para el cliente | 1 % | 0 % |

Costos de la búsqueda aparte (no están en el turno):
- Embedding de la consulta: ~US$ 0,000003 por consulta (663 consultas = US$ 0,0017). Despreciable.
- Juez de relevancia (flash-lite): ~US$ 0,0015 por llamada y ~US$ 0,003 por búsqueda (1.729 llamadas = US$ 2,63; ~2 llamadas por búsqueda porque la caché por conversación evita repetir). El 841 de búsquedas es el número de líneas del log de búsquedas, así que el promedio por búsqueda es aproximado.

Una pregunta con búsqueda cuesta entonces ~US$ 0,043 (0,039 + 0,003 + ~0), contra ~US$ 0,015 una solo SQL: ~3 veces más.

## Respuesta a la duda de la reunión

- El **orquestador** (planificador) cuesta ~1-2 % del turno. No es el problema.
- El **juez** (la única "relectura" que queda) es ~7 % de una pregunta con búsqueda.
- Lo caro es el **modelo principal leyendo resultados** (56-61 %) y la **reparación de evidencia** (~20 %).

## Si se reprocesara para contar (lo que se descartó)

- Leer N conversaciones con el juez actual cuesta ~US$ 0,0015 por lectura: **1.000 lecturas ≈ US$ 1,5 por pregunta** (~35 veces una pregunta normal).
- La referencia anterior con JEV: US$ 0,31 por 1.000 lecturas (factura de TypeSafe), US$ 0,3 a 1,2 por pregunta.
- Costo del experimento de hoy (mezcla distancia + palabras clave): unas 1.400 lecturas de referencia del juez + 13 llamadas de variantes: ~US$ 2.

Límites: el costo del juez por búsqueda es un promedio de todo el período (incluye caché y pruebas); los precios son los de `usage_tracking`.
