# Vera Intelligence (demo) — refresco automático del Data Map en una VM

> **Si sos un asistente (LLM) y una persona te pasó este repo para que la guíes a armar la VM: leé esta página completa y después la guía larga. Tu trabajo es llevar a esa persona paso a paso, de a un paso por vez, verificando cada uno antes de seguir.**

## Qué se quiere lograr

Una VM con un cron que, todos los días hábiles, revisa si cambió algún prompt de negocio de algún cliente; si cambió, regenera el Data Map de ese cliente, lo valida con un gate y,
**solo si pasa**, lo publica con un commit y un push a `main` de este repo. Streamlit Cloud ve el push y actualiza la app solo. Si algo necesita revisión humana, lo avisa por Slack.

## Leé primero (en este orden)

1. **Guía completa, con todos los comandos:** <https://raw.githubusercontent.com/LBinello2004/vera-intelligence-demo/main/experimental_proyects/Vera%20Intelligence/10.%20documentos/REFRESCO_DIARIO_VM.md>
   (la misma en formato web: <https://github.com/LBinello2004/vera-intelligence-demo/blob/main/experimental_proyects/Vera%20Intelligence/10.%20documentos/REFRESCO_DIARIO_VM.md>)
2. Si algo no coincide con tu entorno, el código manda: `experimental_proyects/Vera Intelligence/4. scripts/run_daily_refresh.py` (el docstring del inicio explica el flujo y las opciones).

## Antes de empezar, preguntale a la persona

- ¿Qué sistema operativo y versión tiene la VM? (La guía está **probada en Windows, no en Linux**: si algo falla en Linux, es esperable. Pedí el error exacto y no improvises arreglos de seguridad.)
- ¿Tiene Python 3.11 o superior, git y salida a internet (GitHub, Gemini, Langfuse, Postgres)?
- ¿Quién es el **dueño del repo** en GitHub (`LBinello2004`)? Solo esa persona puede cargar la deploy key.
- ¿Quién tiene las credenciales (Gemini, Postgres, Langfuse) y la URL del webhook de Slack?

## Quién hace qué y cómo se verifica cada paso

| # | Paso | Quién | Cómo saber que salió bien |
|---|---|---|---|
| 1 | Generar la clave SSH en la VM (comandos abajo) y pasar la **clave pública** al dueño del repo | La persona de la VM | Existe `~/.ssh/vera_deploy.pub` |
| 2 | Cargar esa clave pública en GitHub: repo → Settings → Deploy keys → Add deploy key, **tildando "Allow write access"** | **El dueño del repo** (nadie más puede) | `ssh -T git@github.com` responde "successfully authenticated" |
| 3 | Clonar, crear el venv e instalar dependencias (guía, paso 2) | La persona de la VM | `.venv/bin/python -c "import yaml, sqlglot, google.genai"` no da error |
| 4 | Crear el `.env` con las credenciales (guía, paso 3) | La persona de la VM, con lo que le pase quien tenga las credenciales | El archivo existe y **no** está versionado (`git status` no lo muestra) |
| 5 | Prueba sin tocar nada: `--dry-run` (guía, paso 4) | La persona de la VM | Termina con `sin_cambios` |
| 6 | Configurar Slack: el dueño del workspace crea el webhook (guía, sección 4.1), lo pasa **por un medio privado** y se pega en `.env`. Probar con `--test-notify` | Dueño de Slack + persona de la VM | El mensaje `✅ Prueba del refresco diario...` aparece en el canal y el comando termina con código 0 |
| 7 | Prueba completa sin publicar: `--no-push` (guía, paso 5) | La persona de la VM | Se genera `.runtime/daily_refresh/<fecha>.md` sin `ACCIÓN REQUERIDA` |
| 8 | Probar que el push funciona sin ensuciar `main` (comandos abajo) | La persona de la VM | El push a la rama de prueba sale bien y después se borra |
| 9 | Programar el cron (guía, paso 7) | La persona de la VM | `crontab -l` muestra la línea; al día siguiente existe el reporte del día |
| 10 | Desactivar la rutina de Claude `vera-intelligence-data-map-poller` (si no, dos sistemas promoverían lo mismo) | El dueño de esa rutina | Ya no aparece activa |

Comandos de los pasos 1 y 8:

```bash
# Paso 1 — clave solo para este repo
ssh-keygen -t ed25519 -C "vera-vm-refresco" -f ~/.ssh/vera_deploy -N ""
cat ~/.ssh/vera_deploy.pub            # esto es lo que se le pasa al dueño del repo (la privada NUNCA sale de la VM)
printf 'Host github.com\n  IdentityFile ~/.ssh/vera_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
ssh -T git@github.com                 # después de cargar la clave en GitHub (paso 2)

# Paso 8 — prueba de escritura sin tocar main
git push origin HEAD:refs/heads/prueba-vm
git push origin --delete prueba-vm
```

## Reglas para el asistente

- **Un paso por vez.** No des el siguiente hasta que la persona confirme el resultado del anterior.
- **Nunca pidas ni aceptes secretos en el chat** (claves de Gemini, contraseña de Postgres, claves de Langfuse, URL del webhook de Slack, clave privada SSH). Que los peguen directo en el `.env` de la VM.
- **No edites `config.yaml` ni los Data Maps a mano** y no borres `.runtime/` (ahí vive el estado: versiones de prompt ya procesadas, intentos y candados). Si hace falta volver atrás, usá `revert_data_map.py` (guía, sección 5).
- **No uses `git add -A` ni hagas commits a mano en el clon de la VM.** El script solo commitea lo que promovió y el repo tiene que estar limpio para que el refresco corra.
- **Ante cualquier error, pará y pedí el texto exacto** (comando y salida completa). No lo tapes con permisos amplios ni desactivando chequeos.
- No inventes: si algo de la guía no coincide con lo que ves, decíselo a la persona.

## Después de armada: dónde se mira

- **Slack** (si está configurado `VI_NOTIFY_WEBHOOK`): mensajes `⚠️ ACCIÓN REQUERIDA` cuando algo necesita revisión humana, y un "todo bien" diario con `VI_NOTIFY_HEARTBEAT=1`. **Si un día hábil no llega ningún mensaje, hay que mirar la VM** (nadie puede avisar que el cron no corrió).
- **En la VM:** `.runtime/daily_refresh/AAAA-MM-DD.md`. Sin webhook, este archivo es el único aviso.
- Qué significa cada estado y qué hacer: guía, sección 4.

## Cómo tratar cada error

Dónde mirar siempre primero: `.runtime/daily_refresh/AAAA-MM-DD.md` (resumen del día), el `.json` de al lado (todo el detalle) y el log del cron. El detalle de un cliente puntual está en `.runtime/data_map_updates/<cliente>/<fecha>.json`.
Para repetir un solo cliente sin publicar nada: `.venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/run_daily_refresh.py" --clients <cliente> --no-pull --no-push`.

### Si el refresco no pudo ni empezar (código de salida 2)

| Mensaje o síntoma | Causa | Qué hacer |
|---|---|---|
| "Ya hay un refresco en curso" | Un candado de una corrida anterior: o sigue corriendo o murió sin soltarlo (por ejemplo, reinicio de la VM) | Verificar que no esté corriendo (`ps aux \| grep run_daily_refresh`). Si no hay proceso, borrar `.runtime/daily_refresh.lock`. Solo vence a las 6 h. El candado por cliente (`.runtime/store/<cliente>/lock.json`) vence a los 90 min |
| Avisa de "cambios ajenos sin commitear" | Alguien tocó archivos versionados dentro del clon; el script no toca nada para no pisar trabajo | `git status`. Si los cambios no importan: `git restore <archivo>`. **No uses `git add -A` ni commitees a mano en el clon** |
| `git pull --ff-only` falla | El clon tiene un commit local que GitHub no tiene (o la historia divergió) | `git log origin/main..HEAD` para ver qué es. Si es una promoción que no se publicó, la corrida siguiente la publica sola; si es otra cosa, avisar antes de forzar nada |

### Si el script falló por un error inesperado (código 4)

Es un error que el script no contemplaba (un bug, un archivo ilegible). Deja el traceback completo en el log del cron y manda a Slack `⚠️ ACCIÓN REQUERIDA: el refresco del Data Map falló con un error inesperado (<tipo>: <mensaje>)`. Qué hacer: abrir el log, copiar el traceback completo y pasárselo a quien mantiene el código; no hay nada que arreglar a mano en los datos. **Lo que este aviso no cubre:** si Python ni siquiera arranca (falta una dependencia, el venv se rompió), el error ocurre antes de que el script pueda avisar; en ese caso solo se nota porque falta el mensaje diario del latido.

### Si algo se promovió pero no se publicó (código 3)

Falló el push. Causas típicas: la deploy key no tiene **Allow write access**, o no hay red hacia GitHub. Probar `ssh -T git@github.com` y `git push origin HEAD:refs/heads/prueba-vm` (y borrar la rama). Lo promovido queda commiteado en la VM y **la corrida siguiente lo publica antes de hacer nada más**. Mientras tanto la app sigue con el Data Map anterior.

### Si un cliente aparece con un estado de revisión

| Estado | Qué significa | Qué hacer |
|---|---|---|
| `reintento_pendiente` | El gate lo rechazó pero quedan intentos | **Nada.** Se reintenta mañana con el motivo del rechazo como pista. No genera aviso |
| `gate_fallo_no_promovido` / `reintentos_agotados` | Se usaron los 4 intentos y el gate sigue rechazando | Abrir el JSON del cliente y mirar `gate_detail` por pregunta: **`regression: true`** = el candidato dejó de mencionar un número que el Data Map vigente sí daba (mirar `answer` y `missing_numbers`; si el candidato es razonable, promoverlo a mano). **`dropped_fields`** = el candidato perdió un campo que usa una pregunta. **`bank_problem` / `gate_bank_problems`** = el SQL de una pregunta del banco (`preguntas/preguntas_evaluacion.yaml`) está roto (por ejemplo, una columna que cambió de tipo): arreglar la pregunta, no el Data Map. **`sql_ok: false`** = no se pudo verificar (caída de la base): reintentar |
| `deriva_de_columnas_no_promovido` | El candidato declara campos que no existen en la vista real de Postgres | Casi siempre es que el ETL todavía no creó las columnas de un prompt nuevo. Revisar con `data_map_column_drift.py --client <cliente>`. Esperar al ETL; no promover a mano |
| `error_regeneracion` | Gemini no devolvió un YAML válido ni después de 2 correcciones | Reintentar al día siguiente; si se repite, revisar el diff del prompt en el JSON |
| `prompt_no_disponible` | Un prompt de Langfuse del cliente da **404** (renombrado o borrado) o falló 3 corridas seguidas. El agente sigue usando su última copia, así que el cliente no se rompe pero deja de ver los cambios | El aviso trae el nombre del prompt. Si se renombró, apuntar `business_rulebooks.<área>.name` de `config.yaml` al nombre nuevo (como se hizo con GAC); si se borró por error, restaurarlo en Langfuse |
| `error_de_proceso` | El proceso del cliente se cayó o pasó los 60 min, incluso con 2 reintentos | Ver el log. Revisar credenciales (`VERA_AI_API_KEY`, `PGPASSWORD`, `LANGFUSE_*` en el `.env`) y repetir el cliente solo con el comando de arriba |

Si el error dice "No pude iniciar el análisis en este momento", falta `VERA_AI_API_KEY` o `PGPASSWORD` en el `.env`.

**Si el error de un cliente es `ServerError: 504 DEADLINE_EXCEEDED`** (le pasó a Farma 24 el 2026-10-09): Gemini no alcanzó a responder dentro del plazo. Con el código actual la regeneración tiene un plazo de 10 minutos, así que si aparece de nuevo, el Data Map de ese cliente es enorme o Gemini está degradado: repetir ese cliente solo (comando de arriba) y, si persiste, avisar. Cada intento fallido **consume uno de los 4 intentos** del cambio (`.runtime/store/<cliente>/attempts.json`); si se agotaron por un problema ya arreglado, borrar ese `attempts.json` para que reintente con el código nuevo.

### Si no llega el aviso de Slack
- Probar `... run_daily_refresh.py --test-notify`. Si dice "NO se pudo enviar": falta `VI_NOTIFY_WEBHOOK` en el `.env` o la URL está mal copiada o revocada.
- Con `VI_NOTIFY_HEARTBEAT=1` llega un "todo bien" cada día hábil. **Si un día no llega, el problema es la VM o el cron**: `crontab -l`, `date` (la hora de la VM es UTC; las 8:00 de Argentina son las 11:00 UTC, línea `0 11 * * 1-5`), el log del cron y que la VM esté encendida.

### Si una promoción salió mal
Cada promoción deja en `2. clientes/<cliente>/data_map/CAMBIOS_AUTOMATICOS.md` su comando de reversión. Lo habitual: `revert_data_map.py --client <cliente> --reason "..." --publish` (guía, sección 5). No borra versiones y el refresco no vuelve a regenerar ese mismo cambio de prompt.

### Si la app no refleja el cambio después del push
Revisar los logs de la app en Streamlit Community Cloud. No se verificó cómo reacciona esta app al push; las sesiones que ya estaban abiertas siguen con el Data Map anterior hasta reconectarse.

### Si falla algo que parece específico de Linux
Es esperable: la guía se probó en Windows, no en Linux. Pasar el comando exacto, la salida completa, `uname -a`, `cat /etc/os-release` y `python3 --version`.

## Estado conocido (decirlo, no esconderlo)

- Sin probar en Linux y sin VM armada todavía. El primer día hay que mirar el reporte.
- El gate es mecánico: verifica que el candidato no rompa lo que ya se medía; no que un cambio de prompt de negocio haya quedado bien interpretado.
- Lo que promueva la VM queda en este repo; el repo principal (`data-sci-vera`) queda desfasado hasta que se sincronice a mano.

## Qué se hizo (2026-10-07 y 2026-10-08)

**1. El refresco diario.** Reemplaza a una rutina de Claude (pausada). Piezas nuevas en `4. scripts/`: `data_map_store.py` (estado en disco: versión activa, versiones de prompt ya procesadas, cambios pendientes, intentos, candados), `data_map_gate.py` (el gate), `data_map_log.py` y `revert_data_map.py` (registro de cambios y reversión) y `run_daily_refresh.py` (el envoltorio para el cron: un candado global, un cliente por proceso con límite de 60 min y reintentos, reporte diario, **un solo commit y un solo push**, aviso por Slack, `--test-notify`, latido opcional y lectura del `.env`). `data_map_auto_update.py` pasó a reintentar solo (hasta 4 intentos por cambio de prompt), a reparar el YAML que devuelve Gemini y a darle al intento siguiente el motivo del rechazo.

**2. El gate, corregido después de probarlo.** Con el gate original, ni el Data Map vigente de Tigo pasaba su propio gate. Se corrigió: (a) los números chicos (un conteo de 93) no se podían encontrar; (b) los bancos dorados envejecen (las tablas crecen y los denominadores cambian), así que el candidato se compara con lo que responde el Data Map **vigente** y solo se rechaza una regresión; (c) solo se le pregunta al agente lo que el cambio puede afectar (si solo cambió la metadata, nada); (d) lo que responde el vigente se mide una vez y se guarda; (e) antes de llamarlo regresión se dan hasta 3 intentos extra, porque un modelo no responde igual dos veces; (f) un SQL roto del banco se informa como problema del banco y no bloquea. **Validación:** los 19 clientes pasan su propio gate (cada Data Map vigente como candidato de sí mismo). **No** se demostró con un modelo real que rechace un candidato malo; eso solo lo cubren tests.

**3. Costo medido.** Gate de los 19 clientes en el peor caso: US$1,09 en total (≈US$0,06 por cliente). La regeneración no se registra: estimada US$0,10–0,40 por intento. Un día sin cambios cuesta US$0 en Gemini. Estimación mensual con el ritmo histórico: US$3–12. El modelo barato para regenerar (`VI_REGEN_CHEAP_MODEL`) quedó **apagado**: en la prueba pasó el gate pero no hizo ninguna consulta SQL y declaró haberlo verificado.

**4. Data Maps promovidos hoy** (cada uno con su entrada en `CAMBIOS_AUTOMATICOS.md`): Forever 21 V4; Huerpel hostess V4; Huerpel ventas V4; Shoe Box V4 (el contenido que había quedado escrito por error sobre la V3 pasó a V5); Huerpel hostess seminuevos V4, que corrige el tipo de `aplica_registro_completo` y `registro_completo` (texto `'true'`/`'false'`, no booleano). El banco dorado de seminuevos tenía la pregunta `q06` con SQL roto por ese mismo motivo; quedó corregida. Los que el gate viejo había rechazado por ruido se re-evaluaron con el gate corregido.

**5. GAC.** Langfuse dividió el prompt en `clientes/GAC/Hostess/*` y `clientes/GAC/Ventas/*` y el nombre viejo (`clientes/GAC/checklist`) pasó a dar 404; el agente seguía con una copia guardada y el refresco decía `sin_cambios`. La carpeta pasó a `gac_ventas_medio` y el rulebook a `clientes/GAC/Ventas/checklist`: es la misma v4 de producción sobre la que se armó el Data Map, así que no cambió ninguna respuesta. **Pendiente de Pedro:** la v5 de Ventas ("protocolo unificado", 19 campos) no está en producción y sus campos no existen todavía en `vw_gac_rendimiento_vendedor`; Hostess no tiene prompt en producción ni vistas; Ventas no tiene vistas de insights. Cuando la v5 llegue a producción y el ETL cree las columnas, el refresco la detecta y regenera solo. Si la v5 llega antes que las columnas, lo rechaza (`deriva_de_columnas_no_promovido`) y avisa tras agotar los intentos.

**6. Aviso cuando un prompt deja de poder leerse** (lo que pasó con GAC): un 404 pide revisión humana de inmediato (`prompt_no_disponible`); otros errores, tras 3 corridas seguidas.

**7. Esta documentación.** La guía larga es `experimental_proyects/Vera Intelligence/10. documentos/REFRESCO_DIARIO_VM.md`; esta página es el punto de entrada.

**8. Primera corrida real en la VM (2026-10-09) y lo que se corrigió.** Corrió de 8:00 a 9:14 (Argentina): 16 clientes `sin_cambios`, Mens Fashion `cambio_cosmetico_sin_regenerar` (solo cambió la forma del prompt), Maga `reintento_pendiente` y Farma 24 `error_de_proceso`. Causas y arreglos: **(a) Farma 24:** `504 DEADLINE_EXCEEDED`; el SDK de Gemini usa el timeout HTTP como plazo del servidor y 120 s no alcanzan para reescribir un Data Map de 76 KB, así que cada intento fallaba tras ~25 minutos y se reintentaba 3 veces. Ahora la regeneración tiene su propio plazo de 10 minutos, razonamiento bajo y un presupuesto (4 consultas SQL y 6 minutos si solo cambiaron reglas; 12 y 10 minutos si cambiaron campos) que, al agotarse, pide el YAML final en vez de fallar. Medido con el cambio real de Farma: regeneración 155 s + gate 183 s ≈ 6 minutos. **(b) Maga:** el gate lo rechazó por dos defectos del propio gate: el caché de lo que responde el Data Map vigente guardaba números (que cambian todos los días) en vez de posiciones, y no toleraba que faltara un solo denominador nuevo. Corregidos; el candidato de Maga pasa. **(c) Tiempo total:** los clientes corren 4 a la vez, el límite por cliente baja de 60 a 15 minutos y un timeout ya no se reintenta; el gate ejecuta el SQL dorado solo para las preguntas que va a usar y hace las preguntas al agente en paralelo. **(d) Avisos:** el error de un cliente en Slack ahora muestra la causa (la línea de la excepción) y no el inicio del traceback.

**Qué falta, y de quién:** el dueño del repo carga la deploy key y el dueño de Slack crea el webhook hacia `#vera-alertas`; Pedro arma la VM Linux (cron `0 11 * * 1-5` en UTC = 8:00 de Argentina); y cuando el cron haya corrido bien un día hábil, la rutina de Claude queda definitivamente fuera (hoy está pausada, no borrada).
