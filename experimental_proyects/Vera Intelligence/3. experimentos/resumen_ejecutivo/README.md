# Experimento: resumen ejecutivo periódico

**Estado: prototipo probado una vez contra Farma24, NO incorporado al proyecto principal.** Vive acá a
propósito, separado de `4. scripts/`, `2. clientes/` y del poller real, para decidir si se adopta sin
haber tocado nada de lo que ya funciona. De las tres ideas bajo `3. experimentos/` (`coaching_playbook`
ya se adoptó de verdad, ver su propio README; `conteo_literal_exacto` sigue en evaluación), este es el
de **menor esfuerzo para incorporar de verdad** de las que todavía no se adoptaron — no necesita
contenido nuevo de negocio, sólo trabajo de plomería (ver "Qué falta" abajo).

Si estás leyendo esto sin contexto previo del proyecto: primero leé `8. README.md` en la raíz (qué es
Vera Intelligence, cómo está armada la seguridad/aislamiento por cliente) — este documento asume que ya
lo leíste.

## Qué es y por qué existe

Hoy Vera Intelligence es **100% reactivo**: alguien (gerencia, un analista) tiene que pensar qué
preguntarle cada vez. Este experimento lo hace **proactivo**: corre un set fijo de preguntas de negocio
de alto valor por cliente y arma un resumen en Markdown, pensado para dispararse solo (ej. cada lunes a
la mañana) con el mismo mecanismo de tarea programada que ya usa el poller de actualización del Data Map
(`vera-intelligence-data-map-poller`, ver "Actualización automática del Data Map" en `8. README.md`) —
no un mecanismo nuevo, el mismo cron/scheduled-task que el proyecto ya usa en producción para otra cosa.

Ejemplo de lo que genera (formato real, ver `build_digest()` en `executive_digest.py`):

```markdown
# Resumen ejecutivo — Farma 24
Generado: 2026-09-08

## ¿Cuál es la tasa de compra total?
<respuesta completa de Vera Intelligence, en el mismo tono ejecutivo/caja-negra de siempre>

## ¿Qué objeciones de precio son las más frecuentes?
<respuesta...>
```

## Por qué es de bajo riesgo / fácil de adoptar

No agrega infraestructura nueva: reusa `vi_agent.build_chat()` + `vi_agent.run_tool_loop()` **tal
cual**, así que hereda automáticamente:

- la caja negra de seguridad (`4. scripts/response_policy.py` — nunca expone tablas/columnas/SQL);
- el aislamiento por tenant (`4. scripts/sql_security.py` — las mismas reglas que ya aplican a
  `1. vi_agent_tester.py`);
- el registro de consumo de tokens (`4. scripts/usage_tracking.py`).

No toca Langfuse, no agrega ninguna tool nueva al agente, no cambia el Data Map de ningún cliente. Es
literalmente un script que le hace N preguntas seguidas al agente ya existente y junta las respuestas —
la superficie de riesgo nueva es mínima.

## Qué se probó

Una corrida real completa contra Farma24 (5 preguntas, costo similar a los diagnósticos chicos ya hechos
en el proyecto — unos centavos, ver `usage_report.py` en `8. README.md` > "Consumo de tokens" para cómo
medir el costo real de cualquier corrida). Generó un `.md` legible con las 5 respuestas. No se probó
Mens Fashion en vivo — mismo código, mismo mecanismo ya confirmado, no hacía falta gastar dos veces sólo
para validar que el mecanismo funciona.

## Cómo probarlo

```bash
cd "experimental_proyects/Vera Intelligence/3. experimentos/resumen_ejecutivo"
../../../../.venv/Scripts/python.exe executive_digest.py --client farma24_alto
```

`--client` es opcional: si se omite, pregunta interactivamente cuál de los bancos disponibles bajo
`preguntas/` usar (mismo patrón que `1. vi_agent_tester.py`). Guarda el resultado en
`.runtime/digests/<client_id>/<fecha>.md` (gitignored, dentro de esta misma carpeta de experimento, no
en el `.runtime/` global del proyecto) y lo imprime por stdout.

## Bug de nombres de banco desactualizados — CORREGIDO (2026-09-08)

`preguntas/farma24.yaml` y `preguntas/mens_fashion.yaml` usaban los `client_id` **viejos**, de antes de
que el proyecto agregara el sufijo de grado (`_alto`/`_medio`/`_bajo`) a cada carpeta de cliente bajo
`2. clientes/` (ver "Convención de nombres de carpeta" en `8. README.md`). Como `build_digest()` llama
primero a `vi_agent.configure_client(client_id)` (que resuelve `2. clientes/<client_id>/config.yaml`)
usando el mismo `client_id` que después busca el archivo de preguntas (`preguntas/<client_id>.yaml`),
ningún valor de `--client` corría sin renombrar los archivos primero. **Corregido**: renombrados a
`preguntas/farma24_alto.yaml` y `preguntas/mens_fashion_alto.yaml` (`git mv`, contenido sin cambios).
Verificado que ambos `client_id` cargan su `config.yaml` correctamente y que ambos YAML se leen con sus
5 preguntas cada uno — no se corrió `executive_digest.py` de punta a punta (llamaría a Gemini, no hacía
falta gastar eso sólo para confirmar el rename). Este mismo tipo de desincronización es un riesgo a
futuro: si el proyecto principal vuelve a cambiar la convención de `client_id`, este experimento (al
vivir fuera de `2. clientes/`) no se actualiza solo — vale la pena revisar esto cada vez que cambie esa
convención.

## Qué falta para incorporarlo de verdad, si se decide adoptarlo

1. **Corregir el bug de arriba primero** (renombrar los YAML de `preguntas/`) — sin esto no corre.
2. Revisar y ajustar las preguntas de `preguntas/<client_id>.yaml` con quien vaya a leer el resumen —
   las que están son un punto de partida razonable (reusan preguntas ya validadas del banco dorado de
   cada cliente), no la lista definitiva. Considerar si conviene un número distinto de preguntas por
   cliente según su volumen/madurez (ver tabla de "Estado actual" en `8. README.md` — no tiene sentido
   un resumen ejecutivo tan elaborado para un cliente `_bajo` con volumen mínimo como `forever_21_bajo`).
3. Mover `executive_digest.py` a `4. scripts/` (ya está escrito para que ese movimiento sea trivial: sólo
   hay que simplificar el cálculo de `SCRIPTS_DIR`/`PREGUNTAS_DIR`, que hoy asume que vive dos niveles
   por debajo de la raíz del proyecto).
4. Mover `preguntas/<client_id>.yaml` a `2. clientes/<client_id>/preguntas/digest_preguntas.yaml` (o
   donde se decida) y documentarlo en `8. README.md`, sección "Estructura".
5. Configurar una tarea en el programador disponible en el entorno, si se decide adoptar el prototipo,
   con la cadencia deseada (semanal, ej. lunes a la
   mañana) que corra el script para cada cliente activo — probablemente sólo para los clientes `_alto`
   inicialmente, dado el volumen y la madurez del Data Map.
6. Decidir dónde termina el resumen — hoy sólo lo deja en disco (`.runtime/`) y lo imprime por stdout.
   Opciones no evaluadas: `SendUserFile` al usuario, un mensaje a un canal, guardarlo como documento
   compartido. Ninguna decidida todavía.
7. Estimar el costo real de la cadencia elegida antes de activarlo en producción: N preguntas × M
   clientes × frecuencia — con `usage_report.py` ya se puede medir el costo real de una corrida de
   prueba y proyectar el costo mensual antes de comprometerse a una cadencia.

## Potencial de negocio

Es el cambio de mayor relación valor/esfuerzo del proyecto en este momento: convierte a Vera Intelligence
de "hay que acordarse de preguntarle" a "manda el resumen solo" con **cero infraestructura nueva** — el
"Qué falta" de arriba es casi enteramente trabajo de organización (mover archivos, crear una tarea
programada), no de diseño ni de código nuevo. Para gerencia, un resumen semanal automático es un
argumento de valor mucho más tangible que "podés preguntarle lo que quieras" — no todos los usuarios van
a pensar en preguntas por su cuenta.

Última actualización del README: 2026-09-14
