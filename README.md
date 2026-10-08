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

## Estado conocido (decirlo, no esconderlo)

- Sin probar en Linux y sin VM armada todavía. El primer día hay que mirar el reporte.
- El gate es mecánico: verifica que el candidato no rompa lo que ya se medía; no que un cambio de prompt de negocio haya quedado bien interpretado.
- Lo que promueva la VM queda en este repo; el repo principal (`data-sci-vera`) queda desfasado hasta que se sincronice a mano.
