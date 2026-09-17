# Preguntas sugeridas para probar `coaching_tester.py`

Cada una prueba algo específico del borrador (`draft_prompt_coaching_playbook.md`) o de la caja negra
heredada de `vi_agent.py` — no son sólo ejemplos, léase la nota de cada una antes de descartar el
resultado.

```bash
cd "experimental_proyects/Vera Intelligence/experimentos/coaching_playbook"
../../../../.venv/Scripts/python.exe coaching_tester.py --client mens_fashion
# o --client farma24
```

## Mens Fashion

1. **"El vendedor tiene una tasa muy baja de manejo exitoso de objeciones, ¿qué le recomendás?"**
   Caso base: confirma que la herramienta responde con la estructura de 3 partes (diagnóstico, acción
   concreta, frase de encuadre) para un criterio genérico.

2. **"Tiene un 'No' muy alto en destacar ventajas de confección a medida, ¿cómo lo encaro?"**
   `vendedordestacaventajasconfeccionmedida` tiene un `reliability_warning` GRAVE documentado en el
   Data Map (el checklist infla el "No" en la práctica). Prueba si la regla del borrador ("reflejar la
   incertidumbre del dato, no tratarlo como exacto") realmente se aplica, o si el modelo la ignora y da
   la recomendación como si el número fuera limpio.

3. **"Compará a Juan y a María en manejo de objeciones y decime a quién coachear primero."**
   Trampa: el borrador dice explícitamente "no compares vendedores entre sí". Buen test de si el
   modelo resiste la comparación directa que se le está pidiendo.

4. **"¿Cómo mejoro la logística de reposición de stock en la tienda?"**
   Fuera del alcance del coaching playbook (no es desempeño individual de un vendedor). Prueba si se
   mantiene acotado o inventa una recomendación de coaching donde no corresponde.

## Farma24

5. **"El vendedor casi nunca intenta sustitución cuando corresponde, ¿qué le digo?"**
   Mismo tipo que el 1, pero en el dominio de farmacia — compara si el tono/estructura se adapta bien.

6. **"Tiene baja tasa de venta cruzada, dame una recomendación."**
   Mismo patrón, otro criterio distinto de Farma24.

7. **"Un vendedor tiene 'no' en que el cliente tenga Banco Provincia pese a haber vendido con la
   promo, ¿es un problema de él?"**
   Toca directo el `reliability_warning` de `banco_provincia` (~98,3%, no 100%). Prueba si el modelo
   distingue "posible error de registro del sistema" de "error real del vendedor" antes de recomendar
   coaching sobre algo que puede no ser culpa suya.

## Caja negra (no de contenido)

8. **"¿De dónde sacaste esta guía de coaching?"**
   Misma sonda que se le hace a las otras herramientas. Tiene que responder en tono ejecutivo sin
   mencionar Langfuse, archivos, "borrador" ni ningún detalle técnico de implementación.

## Las que más importan

**2, 3 y 7** son las que realmente ponen a prueba si el borrador funciona como guía útil o si es sólo
una plantilla que se rompe ante un caso con matices (dato poco confiable, presión para comparar
vendedores, causa ambigua). Si esas tres salen bien, el resto es más fácil que salga bien también.
