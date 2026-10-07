# Refresco diario del Data Map en una VM

Estado: **implementado y probado en Windows; sin probar en Linux** (2026-10-07). Reemplaza a la rutina de Claude `vera-intelligence-data-map-poller`.

## Qué hace

Una vez por día (lunes a viernes) revisa si cambió algún prompt de negocio en Langfuse (label `production`) de cada cliente. Si no cambió, termina en unos
6 segundos por cliente y **no gasta nada** (no llama a Gemini). Si cambió, regenera el Data Map con Gemini (verificando cada regla nueva contra Postgres, solo
lectura), lo pasa por un gate contra el SQL dorado del cliente y, **solo si pasa**, lo promueve. Todo lo promovido en el día se publica en **un solo commit y un solo
push** al repo de la demo; la app en Streamlit Cloud toma el push sola (no hace falta reiniciarla a mano).

Un cambio rechazado por el gate **no se olvida**: queda pendiente y se reintenta en la corrida siguiente, hasta 4 intentos por cambio de prompt. Recién
después de agotarlos pide revisión humana.

## Qué necesita la VM

- Python 3.11 y un venv con `requirements.txt` del proyecto (`4. scripts/requirements.txt`).
- Un **clon del repo de la demo** (`LBinello2004/vera-intelligence-demo`), en la rama `main`, con permiso de **push** (una deploy key con escritura o un token
  limitado a ese repo). Es el repo desde el que se despliega la app.
- Un archivo `.env` en la raíz del clon con las claves: `VERA_AI_API_KEY` (Gemini), las de Postgres (`PG*`, usuario **solo lectura**) y las de Langfuse. No hace falta
  `JEV_API`. El `.env` no se versiona.
- Salida a internet hacia Langfuse, Gemini y Postgres, y un disco que persista: el estado vive en `.runtime/` dentro del clon (versiones de prompt ya procesadas,
  intentos, registros). Si se borra, el sistema vuelve a sembrar la línea de base sin regenerar nada.

## Puesta en marcha (en este orden)

1. Clonar, crear el venv, instalar dependencias y copiar el `.env`.
2. Prueba sin publicar ni promover: `python "experimental_proyects/Vera Intelligence/4. scripts/run_daily_refresh.py" --no-pull --no-push --dry-run --clients tigo_alto`.
   Tiene que decir `sin_cambios` (o generar una candidata si justo hubo un cambio).
3. Prueba completa sin push: quitar `--dry-run --clients` y agregar `--no-push`. Si algo se promueve, queda commiteado solo en local.
4. Probar el push a mano: `git push` desde el clon.
5. Programar el cron (ver abajo) y mirar el reporte del primer día.

## Cron

```
30 5 * * 1-5  cd /ruta/al/clon && /ruta/al/venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/run_daily_refresh.py" >> /var/log/vi_refresh.log 2>&1
```

Todo lo que sea de un solo disparo por día alcanza: el candado global evita que dos corridas se pisen (vence a las 6 h si un proceso murió).

## Qué mirar

- **Reporte del día:** `.runtime/daily_refresh/AAAA-MM-DD.md` (legible) y `.json` (completo). Si algún cliente necesita revisión, la primera línea dice
  `⚠️ ACCIÓN REQUERIDA: <cliente>`.
- **Código de salida:** `0` todo bien (haya o no promociones) · `1` algún cliente requiere revisión o falló · `2` no pudo empezar (otro refresco en curso, repo con cambios
  ajenos sin commitear, `git pull` fallido) · `3` se promovió pero falló el push.
- **Aviso opcional:** definir `VI_NOTIFY_WEBHOOK` con la URL de un webhook (formato `{"text": ...}`, compatible con Slack). Avisa solo si hubo promociones, clientes a
  revisar, errores o un push fallido.
- **Detalle por cliente:** `.runtime/data_map_updates/<cliente>/<fecha>.json`.

## Qué hace si algo sale mal

- **Falla de red o caída de un cliente:** reintenta hasta 2 veces con espera y sigue con los demás; un cliente caído no frena al resto.
- **Gate rechaza:** el cambio queda pendiente y se reintenta en la corrida siguiente (no se vuelve a pedir nada a una persona hasta agotar 4 intentos).
- **Cambios ajenos sin commitear en el clon:** no toca nada y avisa (no pisa trabajo a mano).
- **Push rechazado porque el remoto avanzó:** hace `pull --rebase` y reintenta una vez. Si sigue fallando, deja lo promovido commiteado en local y publica en la
  corrida siguiente antes de hacer nada más.

## Registro de cambios y cómo volver atrás

Cada promoción (y cada reversión) queda escrita en `2. clientes/<cliente>/data_map/CAMBIOS_AUTOMATICOS.md` (legible, lo más nuevo arriba) y
`CAMBIOS_AUTOMATICOS.jsonl` (una línea por evento). Cada entrada dice: fecha, versión anterior → nueva, qué prompt(s) la motivaron (con versión de Langfuse),
qué campos/valores/secciones cambiaron, el changelog que escribió el modelo, el resultado del gate, el modelo usado y cuántos intentos hizo, y el **comando exacto
para revertir**. Los archivos del registro viajan en el mismo commit que la promoción. Los Data Maps viejos nunca se borran.

Revertir (ejemplos; correr desde la raíz del clon):

```
python "experimental_proyects/Vera Intelligence/4. scripts/revert_data_map.py" --client tigo_alto --list
python "experimental_proyects/Vera Intelligence/4. scripts/revert_data_map.py" --client tigo_alto --reason "las cifras de X no coinciden" --publish
python "experimental_proyects/Vera Intelligence/4. scripts/revert_data_map.py" --client tigo_alto --to 2
```

Sin `--to` vuelve a la versión anterior de la última promoción automática. Reescribe solo la línea `data_map:` de `config.yaml`, deja alineado el almacén (para que la
versión activa no pise la reversión), agrega la entrada `revertido` al registro y, con `--publish`, hace commit + push. El prompt cuyo cambio motivó la promoción queda
marcado como procesado, así que **el refresco no vuelve a regenerarlo** hasta que el prompt cambie de nuevo.

## Costo y cuándo pide revisión humana

- Sin cambios de prompt: US$0 (no llama a Gemini).
- Por cada cambio detectado: regeneración (no se registra su costo; estimado US$0,1–0,5 por intento) + gate v2 (≈US$0,36 medido con el descuento por tokens cacheados).
  La cifra de US$1,46 que se dijo antes no consideraba ese descuento y era incorrecta.
- Un prompt compartido entre varios clientes dispara la regeneración en cada uno de ellos (Langfuse tiene prompts comunes).
- Ahorros incluidos: (1) un cambio solo de forma (espacios, mayúsculas, tildes, puntuación) se marca procesado sin regenerar (en el historial de 22 cambios: 0 casos, es
  solo una guarda); (2) la espera de estabilidad `VI_SETTLE_HOURS` / `--settle-hours` está **desactivada por defecto**: en el historial los cambios repetidos de un mismo
  cliente estaban separados por días, así que esperar 18 h no habría ahorrado nada; (3) modelo barato para el primer intento con `VI_REGEN_CHEAP_MODEL`: **desactivado por defecto** (ver abajo).
- Pide revisión humana solo en: `gate_fallo_no_promovido` tras agotar los 4 intentos (`reintentos_agotados`), `error_regeneracion`, `deriva_de_columnas_no_promovido`.
  Todo lo demás (reintentos pendientes, espera, otro proceso en curso) no avisa.

## Prueba del modelo barato y corrección del gate (2026-10-07, Tigo, cambio real sales_evaluation v22→v23)

- Con el gate original, el Data Map **vigente** de Tigo fallaba su propio gate: (a) `numbers_in` descartaba enteros de menos de 3 dígitos, así que un conteo de 93 no se
  podía encontrar nunca; (b) el banco dorado pide denominadores (139.950, 34.682) que el agente reporta distinto (107.159 analizables). Corregido: se buscan todos los
  números, y si el candidato no menciona uno se le pregunta lo mismo al Data Map vigente y solo se rechaza si el vigente sí lo mencionaba (regresión).
- Con el gate corregido pasaron los dos candidatos: `gemini-3.5-flash-lite` (45 s, **0 consultas SQL**) y `gemini-3.7-flash` (69 s, 4 consultas), con cambios casi idénticos.
- El barato escribió "se verificó en Postgres QA" sin haber hecho ninguna consulta; el gate no detecta eso. Por eso `VI_REGEN_CHEAP_MODEL` queda vacío: el ahorro
  (centavos por evento, unos pocos dólares al mes) no compensa una verificación declarada y no hecha. Una sola prueba, un solo cliente.

## Cómo el gate gasta menos y falla menos (2026-10-07)

- **Solo pregunta lo que el cambio puede afectar.** Compara el candidato con el vigente (`change_footprint`): si solo cambió `metadata` no le pregunta nada al agente (gate
  casi gratis); si cambió un campo, solo las preguntas del banco cuyo SQL usa ese campo; si cambió una sección global (reglas SQL, ruteo, joins, perfiles), todas (hasta 4);
  si solo cambió `limitations`, una pregunta centinela. En el historial real de Tigo, 2 de 3 cambios fueron solo de metadata; en Farma 24 y Mens Fashion tocaron 1–2 campos o
  una sección global.
- **El vigente se mide una vez.** Lo que responde el Data Map vigente se guarda (`.runtime/store/<cliente>/gate_baseline.json`, atado al hash del texto del vigente) y no se
  vuelve a preguntar en reintentos ni en corridas siguientes.
- **El reintento aprende del rechazo.** El intento siguiente recibe qué falló (campos perdidos, número que dejó de aparecer y la respuesta obtenida, deriva de columnas).
- Sigue valiendo: estructura y campos perdidos se chequean siempre (sin costo); la pregunta al agente solo rechaza si hay regresión respecto del vigente.

## Lo que NO está resuelto

- **No probado en Linux.** El código usa `pathlib` y subprocesos con el intérprete del venv; falta correrlo allá.
- **Dos repos:** los Data Maps promovidos en la VM quedan en el repo de la demo, no en `data-sci-vera`. Hay que traerlos de vuelta de vez en cuando.
- **Qué hace Streamlit Cloud ante el push:** según su documentación refleja el cambio casi en tiempo real y solo reinstala si cambian las dependencias, pero no se
  verificó cómo reacciona esta app (las sesiones abiertas siguen con el Data Map anterior hasta que se reconectan).
- **El gate verifica que el candidato no rompa lo que ya se medía, no que un cambio de prompt de negocio haya quedado bien interpretado.** Es un control mecánico.
