# Refresco diario del Data Map en una VM — guía completa

Estado (2026-10-07): implementado y probado en Windows con repos git de prueba y con una corrida real de punta a punta (Tigo). **Sin probar en Linux. La VM todavía no está armada.**
Esta guía es la única que hace falta para armar la VM y entender qué pasa después. Reemplaza a la rutina de Claude `vera-intelligence-data-map-poller` (hay que desactivarla cuando la VM ande).

---

## 1. Qué hace y cómo eso actualiza la app solo

```
Langfuse (prompts de negocio de cada cliente, label "production")
        │  ① cron en la VM, todos los días hábiles a las 05:30
        ▼
run_daily_refresh.py  ── por cada uno de los clientes de "2. clientes/", en su propio proceso:
        │   ② ¿cambió algún prompt desde la última versión procesada?
        │        NO → termina (≈6 s por cliente, US$0 de Gemini)
        │        SÍ → ③ Gemini regenera el Data Map (verifica cada regla nueva con SQL de solo lectura contra Postgres)
        │              ④ gate: estructura + campos que no se perdieron + SQL dorado como verdad + el agente responde sin empeorar
        │              ⑤ pasó → se promueve (se edita la línea `data_map:` de config.yaml y se agrega el archivo V<N+1>.yaml)
        │                 no pasó → queda pendiente y se reintenta solo (hasta 4 intentos por cambio de prompt)
        ▼
⑥ UN solo commit + UN solo push al repo de la demo (solo si algo se promovió)
        ▼
⑦ Streamlit Community Cloud ve el push a `main` y vuelve a desplegar la app con el config.yaml nuevo
```

Por eso no hace falta tocar la app ni reiniciarla: el único "canal" entre la VM y la app es `git push` a `main`.
Streamlit Cloud suele reflejar un push casi al instante y solo reinstala si cambian las dependencias (según su documentación; **no se verificó con esta app**). Las sesiones que ya estaban abiertas siguen con el Data Map anterior hasta que se reconectan.

Garantías que importan:
- Los Data Maps viejos **nunca se borran ni se pisan**; cada promoción es una versión nueva (`V4`, `V5`...).
- Cada promoción queda escrita en `2. clientes/<cliente>/data_map/CAMBIOS_AUTOMATICOS.md` (y `.jsonl`) con qué cambió, qué prompt la motivó, el resultado del gate y el comando exacto para revertirla.
- Si algo falla, el sistema **no promueve**: la app sigue con el Data Map que ya tenía.

---

## 2. Qué hay que hacer en la VM (una sola vez)

Requisitos: Linux con Python 3.11+ y git, salida a internet hacia GitHub, Langfuse, Gemini y Postgres, y **disco que persista** (el estado vive en `.runtime/` dentro del clon).

**Paso 1 — Clave de despliegue con permiso de escritura.** En GitHub, repo `LBinello2004/vera-intelligence-demo` → Settings → Deploy keys → Add deploy key → pegar la clave pública de la VM y tildar **Allow write access**. (Alternativa: un token fino limitado a ese repo.)

En la VM, para generar la clave: `ssh-keygen -t ed25519 -C "vera-vm-refresco" -f ~/.ssh/vera_deploy -N ""` y mostrar la pública con `cat ~/.ssh/vera_deploy.pub` (la privada nunca sale de la VM). Después, agregar a `~/.ssh/config` estas tres líneas:
```
Host github.com
  IdentityFile ~/.ssh/vera_deploy
  IdentitiesOnly yes
```
y, con la clave ya cargada en GitHub, `ssh -T git@github.com` tiene que decir que autenticó.

**Paso 2 — Clonar e instalar.**
```bash
git clone git@github.com:LBinello2004/vera-intelligence-demo.git vera-demo
cd vera-demo
git config user.name  "Vera refresco automatico"
git config user.email "refresco-automatico@vera.invalid"
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Paso 3 — Credenciales.** Crear `vera-demo/.env` (en la raíz del clon; no se versiona):
```
VERA_AI_API_KEY=...          # Gemini
PGPASSWORD=...               # Postgres (la conexión es de solo lectura por sesión)
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_BASE_URL=...
# Aviso por Slack (opcional pero muy recomendado; cómo conseguir la URL: sección 4.1):
VI_NOTIFY_WEBHOOK=https://hooks.slack.com/services/...
VI_NOTIFY_HEARTBEAT=1
```
No hace falta `JEV_API` ni nada de Firestore. Con el webhook cargado, probar el aviso: `... run_daily_refresh.py --test-notify` (sección 4.1).

**Paso 4 — Prueba sin tocar nada** (desde `vera-demo/`):
```bash
.venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/run_daily_refresh.py" --no-pull --no-push --dry-run --clients tigo_alto
```
Tiene que terminar con `sin_cambios` (la primera vez solo "siembra" la línea de base: anota qué versión de cada prompt ya está reflejada, sin regenerar nada). Si falla acá, es un problema de credenciales o de red, no del refresco.

**Paso 5 — Prueba completa sin publicar:**
```bash
.venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/run_daily_refresh.py" --no-push
```
Recorre todos los clientes. Si justo hubo un cambio de prompt y se promueve algo, queda commiteado solo en local. Revisar `.runtime/daily_refresh/<fecha>.md`.

**Paso 6 — Probar el push sin ensuciar `main`:** `git push origin HEAD:refs/heads/prueba-vm` y después `git push origin --delete prueba-vm`. Si el primero sale bien, la clave de despliegue escribe.

**Paso 7 — Programar el cron** (`crontab -e`; revisar la zona horaria de la VM):
```
30 5 * * 1-5  cd /ruta/a/vera-demo && .venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/run_daily_refresh.py" >> /ruta/a/vi_refresh.log 2>&1
```
Una sola corrida por día alcanza. Un candado impide que dos corridas se pisen (vence a las 6 h si un proceso murió).

**Paso 8 — Desactivar la rutina de Claude** `vera-intelligence-data-map-poller` (si no, dos sistemas intentarían promover lo mismo) y mirar el reporte del primer día.

---

## 3. Qué pasa cada día, en concreto

1. Toma el candado global. Publica primero lo que haya quedado promovido y sin publicar de una corrida anterior (push que había fallado).
2. Exige repo limpio y hace `git pull --ff-only`. Después hace una **verificación previa** (el `.env`, Postgres, Langfuse y Gemini responden; un reintento tras 20 s); si algo falla, **no arranca** y manda UN aviso claro en vez de fallar cliente por cliente (se omite con `--no-preflight`). Si hay cambios ajenos sin commitear **no toca nada** y avisa.
3. Corre cada cliente en su propio proceso, con límite de 60 minutos y hasta 2 reintentos solo ante fallas de proceso o de red.
4. Escribe el reporte del día.
5. Si algo se promovió: un solo commit que incluye exactamente, por cliente, `config.yaml`, el Data Map nuevo y los archivos `CAMBIOS_AUTOMATICOS`; push (si el remoto avanzó, hace `pull --rebase` y reintenta una vez).
6. Avisa por webhook si está configurado.

Estados posibles por cliente: `sin_cambios`, `promovido`, `cambio_cosmetico_sin_regenerar`, `reintento_pendiente` (**no requiere a nadie**), `en_curso_por_otro_proceso`, y los que **sí requieren revisión humana** (sección 4).

---

## 4. Dónde avisa si hace falta revisión humana

**Importante: sin configurar nada, no avisa a nadie.** Lo único que queda es el reporte en la VM; alguien tiene que mirarlo. Para que avise de verdad hay que crear un canal y poner su URL en `VI_NOTIFY_WEBHOOK` (hoy no existe: decisión pendiente).

| Dónde | Qué ve la persona | Cuándo |
|---|---|---|
| **Webhook** (`VI_NOTIFY_WEBHOOK`, formato `{"text": ...}` de Slack; sirve cualquier canal que lo acepte) | Un mensaje que empieza con `⚠️ ACCIÓN REQUERIDA: <clientes>` y lista cada cliente con su estado y el motivo | Cuando algún cliente requiere revisión, hubo un error de proceso, falló el push, o el refresco no pudo ni empezar (otra corrida en curso, repo con cambios ajenos, `git pull` fallido). **También avisa cuando se promueve algo** (informativo, sin la línea de acción) |
| **Reporte del día** en la VM: `.runtime/daily_refresh/AAAA-MM-DD.md` (y `.json` con todo el detalle) | La primera línea dice `⚠️ ACCIÓN REQUERIDA: ...` si hace falta algo | Siempre se escribe |
| **Log de cron** (`vi_refresh.log` según el cron del paso 7) | Lo mismo que el reporte, impreso | Siempre |
| **Código de salida** del proceso | `0` bien · `1` algún cliente requiere revisión o falló · `2` no pudo empezar · `3` se promovió pero falló el push · `4` error inesperado del propio script (también avisa por webhook con el texto del error) | Útil si algún monitor ya mira cron |
| **Detalle por cliente** `.runtime/data_map_updates/<cliente>/<fecha>.json` | Candidata, changelog, preguntas del gate y por qué falló | Para entender un caso puntual |

**Qué estados piden revisión humana y qué hacer:**

| Estado | Qué significa | Qué hacer |
|---|---|---|
| `gate_fallo_no_promovido` | El gate rechazó el cambio de prompt y ya se usaron los 4 intentos permitidos | Mirar el detalle del cliente: qué pregunta falló y con qué números. Si el Data Map vigente ya no contesta bien esa pregunta, el banco dorado (`preguntas/preguntas_evaluacion.yaml`) puede estar desactualizado: corregirlo. Si el candidato es bueno, promoverlo a mano |
| `reintentos_agotados` | Los intentos ya estaban agotados de corridas anteriores y el cambio sigue sin resolverse | Igual que el anterior (el sistema no vuelve a gastar hasta que alguien lo resuelva o el prompt cambie de nuevo) |
| `reintento_pendiente` | El gate lo rechazó pero quedan intentos | **Nada**: se reintenta en la corrida siguiente, con el motivo del rechazo como pista para Gemini. No genera aviso de acción |
| `prompt_no_disponible` (o un cliente con `rulebook_problems` en el reporte) | Un prompt de Langfuse del cliente no se puede leer: dio **404** (se renombró o se borró; avisa de inmediato) o falló 3 corridas seguidas por otro error. El agente sigue usando su última copia guardada, así que el cliente no se rompe, pero ya no ve los cambios del prompt. Es lo que le pasó a GAC el 2026-10-07 cuando Langfuse lo dividió en Hostess y Ventas | Ver el nombre del prompt en el aviso. Si se renombró, apuntar el rulebook de `config.yaml` (`business_rulebooks.<área>.name`) al nombre nuevo; si se borró por error, restaurarlo en Langfuse |
| `infraestructura_no_disponible` | Gemini (5xx, 429), la base o la red no respondieron. **No es un rechazo del candidato**: el cambio sigue pendiente y no se gasta ningún intento. Pide revisión solo si es de configuración (Gemini 400/401/403/404: clave, permisos o modelo mal) o si se repite 3 días seguidos (`needs_human` en el reporte) | Si es de configuración: revisar `VERA_AI_API_KEY` y el modelo. Si se repite: ver si Gemini o la base tienen un problema sostenido |
| `error_regeneracion` | Gemini no devolvió un YAML válido ni después de 2 correcciones | Reintentar al día siguiente; si se repite, revisar el prompt diff |
| `deriva_de_columnas_no_promovido` | El candidato declara campos que no existen en la vista real de Postgres | Revisar si la vista cambió |
| `error_de_proceso` (errores) | El proceso del cliente se cayó o excedió 60 min, incluso con reintentos | Ver el log |
| Push fallido (exit 3) | Hay algo promovido y commiteado en la VM que no llegó a GitHub | Revisar la clave de despliegue/red; la próxima corrida lo vuelve a intentar antes de hacer nada más |

**Lo que NO cubre:** si la VM está apagada, el cron se cayó o el disco se perdió, **no hay nadie que pueda avisar que no corrió**. Para eso está el latido opcional: con `VI_NOTIFY_HEARTBEAT=1` (y webhook configurado) llega todos los días un mensaje corto `✅ Refresco del <fecha>: N clientes revisados, sin cambios`. **Si un día hábil no llega, hay que mirar la VM.**

### 4.1 Cómo armar el aviso por Slack (una sola vez, ~5 minutos)

1. Entrar a <https://api.slack.com/apps> con una cuenta del workspace → **Create New App** → **From scratch** → nombre (por ejemplo `Vera refresco Data Map`) → elegir el workspace.
2. En el menú de la izquierda, **Incoming Webhooks** → activar **Activate Incoming Webhooks**.
3. Abajo, **Add New Webhook to Workspace** → elegir el canal donde tiene que llegar (uno que mire el equipo, por ejemplo `#vera-alertas`) → **Allow**. Si el workspace exige aprobación de un administrador para apps nuevas, hay que pedirla.
4. Copiar la **Webhook URL** (empieza con `https://hooks.slack.com/services/...`). **Es un secreto**: quien la tenga puede escribir en ese canal. No se pega en el chat, en un commit ni en el repo.
5. En la VM, agregarla al `.env` del clon (el mismo archivo del paso 3):
   ```
   VI_NOTIFY_WEBHOOK=https://hooks.slack.com/services/...
   VI_NOTIFY_HEARTBEAT=1        # recomendado: un "todo bien" corto por día; si un día hábil no llega, mirar la VM
   ```
6. Probar el aviso sin esperar un incidente (desde `vera-demo/`):
   ```bash
   .venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/run_daily_refresh.py" --test-notify
   ```
   Tiene que aparecer en el canal `✅ Prueba del refresco diario del Data Map...` y el comando termina con código 0. Si imprime `NO se pudo enviar`, falta la variable en el `.env` o Slack rechazó el mensaje (URL mal copiada o webhook revocado).
   Prueba alternativa sin el script: `curl -X POST -H 'Content-type: application/json' --data '{"text":"prueba"}' "$VI_NOTIFY_WEBHOOK"`.
7. Si la URL se filtra: en la misma página de la app de Slack, **Revoke** del webhook y crear otro (pasos 3 a 6).

Notas: el aviso usa el formato `{"text": ...}` de Slack. Otros canales (Teams, Discord) usan otros formatos y no funcionarían tal cual. Para que el mensaje notifique a todo el canal, un administrador puede configurar el canal para que avise por cada mensaje; el script no agrega `@channel`.

---

### 4.2 Cuánta carga pone sobre la base (se puede ajustar en el `.env`)

- **Clientes a la vez:** `VI_REFRESH_PARALLEL=2` (por defecto 4; la opción `--parallel N` del comando tiene prioridad). Menos clientes a la vez = menos presión sobre Postgres y Gemini, a costa de tardar más los días con varios cambios.
- **Espera a la ETL:** antes de arrancar, el refresco mira si la ETL está haciendo `REFRESH MATERIALIZED VIEW`. Si es así espera, revisando cada minuto, hasta `VI_REFRESH_WAIT_ETL_MINUTES` (por defecto 10; `0` = no esperar; también `--wait-etl-minutes`). Si pasado ese límite la ETL sigue ocupada, corre **de a un cliente** y lo deja anotado en el reporte. Si no puede consultar la base para saberlo, no frena nada.
- Nuestras consultas son de solo lectura y un `REFRESH ... CONCURRENTLY` está pensado para convivir con lecturas: no hay bloqueos entre ambos. Lo que se evita es sumar presión de disco mientras la ETL trabaja.

---

## 5. Cómo volver atrás

Cada promoción deja en `CAMBIOS_AUTOMATICOS.md` su comando de reversión. Desde la raíz del clon:
```bash
.venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/revert_data_map.py" --client tigo_alto --list
.venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/revert_data_map.py" --client tigo_alto --reason "las cifras de X no coinciden" --publish
.venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/revert_data_map.py" --client tigo_alto --to 2
```
Sin `--to` vuelve a la versión anterior de la última promoción automática. Reescribe solo la línea `data_map:` de `config.yaml`, agrega la entrada `revertido` al registro y, con `--publish`, hace commit y push (la app se actualiza sola). No borra ninguna versión. El prompt que motivó la promoción queda marcado como procesado: **el refresco no vuelve a regenerarlo** hasta que ese prompt cambie otra vez.

---

## 5.1 Promover a mano un candidato que el gate rechazó

Cuando un cliente queda en revisión (`gate_fallo_no_promovido` o `reintentos_agotados`) y una persona revisa el candidato (el diff del Data Map y el detalle del gate) y lo considera bueno, no hace falta editar nada a mano:
```bash
.venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/promote_data_map.py" --client maga_alto --list
.venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/promote_data_map.py" --client maga_alto --reason "revisé el diff: solo cambia la descripción de X" --publish
```
Sin `--version` ni `--file` promueve el candidato más nuevo que la versión vigente. **No corre el gate con Gemini**: verifica solo que el candidato conserve la estructura del vigente y que todos sus campos existan en Postgres (`--skip-drift-check` omite esto último). Apunta `config.yaml` al candidato, marca como procesados los prompts pendientes (el refresco no vuelve a regenerar ese cambio), deja una entrada `promovido_manual` con la razón en `CAMBIOS_AUTOMATICOS` (con el comando para revertir) y, con `--publish`, hace commit y push. La razón es obligatoria. No borra ni pisa ninguna versión.

---

## 6. Costo

Medido el 2026-10-07 sobre el gate de los 19 clientes en su peor caso (todas las preguntas): US$1,09 en total, ≈US$0,06 por cliente (entre US$0,025 y US$0,10; Tigo US$0,20 por reintentos). Supone `gemini-3.7-flash` y tokens cacheados al 10 % del precio.
La regeneración no se registra: estimada en US$0,10–0,40 por intento.

| Situación | Costo |
|---|---|
| Día sin cambios de prompt | US$0 en Gemini (solo lee Langfuse y Postgres) |
| Cambio que solo toca metadata | Solo la regeneración (el gate casi no cuesta) |
| Cambio típico de un cliente | ≈US$0,15–0,60 |
| Mes con el ritmo histórico (~20 eventos en 22 días) | ≈US$3–12 |

Un prompt compartido entre varios clientes regenera una vez por cliente. La VM no suma costo si ya existe.

Cómo se mantiene bajo: el gate solo le pregunta al agente lo que el cambio puede afectar (si solo cambió la metadata, nada); lo que responde el Data Map vigente se mide una vez y se guarda; los cambios de solo forma (espacios, tildes, mayúsculas) no regeneran. El modelo barato para regenerar (`VI_REGEN_CHEAP_MODEL`) está **apagado a propósito**: en la prueba pasó el gate pero no hizo ninguna consulta SQL y declaró haber verificado.

---

**Qué pasa si un cambio grande se pasa de tiempo.** La regeneración tiene su presupuesto (6 minutos si solo cambiaron reglas, 10 si cambiaron campos) y al agotarse pide el YAML final en vez de fallar; el gate suma unos 3 o 4 minutos. Un cambio estructural grande en Farma 24 puede acercarse a 15–17 minutos (estimado, no medido), por eso el tope por cliente es de 90. Si aun así el proceso llega al tope, se mata, el cliente figura como `error_de_proceso` ("timeout de 90 min", avisa a Slack), **no se promueve nada**, el intento **se devuelve** y el cambio se retoma en la corrida siguiente. Para correr un cliente a mano con más margen: `--clients <cliente> --timeout-minutes 120`.

## 6.1 Cuánto tarda (medido el 2026-10-09 con el cambio real de Farma 24, el cliente más pesado)

- **Día sin cambios:** unos 6 segundos por cliente; 19 clientes en uno o dos minutos (corren 4 a la vez).
- **Un cambio de prompt en Farma 24:** regeneración 155 s + gate 183 s ≈ 6 minutos en total. Antes del arreglo la regeneración fallaba tras ~25 minutos por intento (3 intentos = 74 minutos).
- **Por qué fallaba:** el SDK de Gemini manda el timeout HTTP como plazo del servidor; con 120 s, reescribir un Data Map de 76 KB vencía siempre con `504 DEADLINE_EXCEEDED`. La regeneración ahora tiene su propio plazo de 10 minutos y razonamiento bajo.
- **Topes para que un cliente lento no frene al resto:** **90 minutos** por cliente (un timeout **no** se reintenta, pero **devuelve el intento** y suelta el candado del cliente: el límite es nuestro, no un rechazo del candidato), 4 clientes a la vez, y la regeneración tiene presupuesto: 4 consultas SQL y 6 minutos si solo cambiaron reglas, 12 consultas y 10 minutos si cambiaron campos. Al agotarse no falla: se le pide a Gemini el YAML final con lo que alcanzó a verificar y decide el gate.
- Cada resultado trae `timings` (segundos de regeneración y de gate por intento) en `.runtime/data_map_updates/<cliente>/<fecha>.json`.

---

## 7. Límites conocidos (decirlos antes de confiar)

- **No probado en Linux** y sin VM armada. El primer día hay que mirarlo.
- **El gate es mecánico.** Verifica que el candidato no rompa lo que ya se medía (estructura, campos, números del SQL dorado), no que un cambio de prompt de negocio haya quedado bien interpretado. Un cambio que afecte campos que ninguna pregunta del banco usa queda sin verificar.
- **Los bancos dorados envejecen** (las tablas crecen). El gate compara contra el Data Map vigente para no rechazar por eso, pero un banco con SQL roto se informa en el detalle del gate (`gate_bank_problems`) y hay que arreglarlo a mano.
- **Falsos rechazos:** se probó que el gate acepta cada Data Map vigente como candidato de sí mismo (19 de 19), no que rechace correctamente uno malo con un modelo real.
- **Dos repos:** lo que promueva la VM queda en el repo de la demo; el repo principal (`data-sci-vera`) queda desfasado hasta que se sincronice a mano.
- **Streamlit Cloud ante el push:** según su documentación se refleja casi al instante, pero no se verificó con esta app.

---

## 8. Ideas anotadas (decisión del 2026-10-09: no se hacen por ahora)

### Regeneración por parches (la única que se quiso conservar)
- **Qué es.** Hoy, ante un cambio de prompt, Gemini reescribe el Data Map **entero** (76 KB en Farma 24, ≈25 mil tokens de salida). La idea es que devuelva solo las secciones que cambian (por ejemplo, la descripción de un campo, un valor de enum, la línea de versión del prompt y el `changes_from_v*`) y que el código las fusione con el Data Map vigente.
- **Qué mejoraría.** Tiempo (Farma pasaría de ~2,5 min de regeneración a menos de uno), gasto de tokens de salida, y desaparece el riesgo de timeouts en mapas grandes (la causa de la falla de Farma del 2026-10-09). Además se pierde menos contenido "no tocado" por errores de copia del modelo.
- **Qué cuesta.** Es un rediseño de varias horas: formato de parche para Gemini, un aplicador que respete el YAML (hoy se pide "copiar byte a byte lo no tocado"; al fusionar a nivel de estructura el archivo se reescribiría con otro formato), y rehacer las pruebas del gate y de la regeneración.
- **Cuándo retomarla.** Si Farma 24 (o algún cliente que crezca) vuelve a acercarse al plazo de la regeneración, o si el costo medido de las regeneraciones resulta alto.

### Evaluadas y no elegidas (por si se vuelven a plantear)
- Guarda del código que llega por `git pull` (importar módulos y volver al commit anterior si falla).
- Chequeo diario de deriva de tipos y columnas contra `information_schema`, aunque no cambie ningún prompt.
- Registro del costo real de la regeneración en el log de uso.
- Chequeo semanal de los bancos de preguntas (SQL roto o vacío).
- Límite más corto por consulta de verificación de la regeneración.
- Clasificar los cambios triviales de prompt y actualizar solo la versión, sin regenerar (ahorra tiempo y gasto, pero puede dejar desactualizada la descripción de un criterio).
- Guardar las versiones de prompt procesadas dentro del repo (para sobrevivir a la pérdida de `.runtime/` en la VM).

