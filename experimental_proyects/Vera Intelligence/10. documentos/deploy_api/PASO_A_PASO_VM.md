# Paso a paso: la API de Vera Intelligence en la VM (armado real, 2026-10-16)

Esto es lo que se hizo y funcionó en la VM de AWS (Ubuntu 24.04, Python 3.12, 2 CPU, 7,6 GB). Reemplaza a la versión anterior, que suponía un servicio de sistema con `sudo`. El repo es **público**: acá no van hostnames, usuarios ni claves.

Estado: API corriendo como **servicio de usuario** (`systemctl --user`) con la búsqueda vectorial **apagada**, probada con una pregunta real (tasa de compra por región, ~25 s). Pendiente: HTTPS con dominio (Caddy) y conectar Supabase/Lovable.

**Regla:** ningún secreto en el repo ni en el chat. Se escriben a mano en la VM (`nano`) y en el panel de secretos de Supabase.

## Contexto de la VM
- Corre el ETL (usuario `ubuntu`, `~/etl-v2-mvp`, su propio `.venv`, refresca vistas materializadas cada ~15 min) y el refresco diario del Data Map. La API **no comparte** el clon ni el entorno de Python con ninguno de los dos.
- El usuario que corre la API **no tiene sudo ni Docker**. Todo lo que sigue va en su carpeta personal.
- Las conexiones del agente son de solo lectura (`default_transaction_read_only=on`), con tope de 180 s por consulta y sin transacciones abiertas (`autocommit`).

## 1. Una sola vez, con sudo (lo corre un administrador)
```bash
sudo loginctl enable-linger USUARIO && sudo apt-get install -y python3-venv git && loginctl show-user USUARIO -p Linger
```
`enable-linger` hace que el servicio de usuario siga vivo sin sesión de SSH y arranque con la VM. Tiene que decir `Linger=yes`.

## 2. Clon y entorno propios (sin sudo)
```bash
cd ~ && git clone https://github.com/LBinello2004/vera-intelligence-demo.git vera-api && python3 -m venv ~/vi-venv
~/vi-venv/bin/pip install -q -r ~/vera-api/requirements.txt -r "$HOME/vera-api/experimental_proyects/Vera Intelligence/4. scripts/requirements-api.txt"
ln -sfn "$HOME/vera-api/experimental_proyects/Vera Intelligence/4. scripts" ~/vi-scripts
~/vi-venv/bin/python -c "import starlette, uvicorn, psycopg, yaml; from google import genai; print('OK dependencias completas')"
```
La instalación tarda unos minutos: no interrumpirla (un `Ctrl+C` la deja a medias). El enlace `~/vi-scripts` evita los espacios de la ruta en el servicio.

## 3. Variables (fuera del repo)
```bash
mkdir -p ~/.config/vera-intelligence && umask 077 && cp ~/vera-api/experimental_proyects/"Vera Intelligence"/"10. documentos"/deploy_api/env.api.example ~/.config/vera-intelligence/api.env
K=$(~/vi-venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))") && sed -i "s|^VI_API_KEYS=.*|VI_API_KEYS=$K|" ~/.config/vera-intelligence/api.env && echo "$K"; unset K
chmod 600 ~/.config/vera-intelligence/api.env && nano ~/.config/vera-intelligence/api.env
```
Completar a mano `VERA_AI_API_KEY` (clave de **Gemini**, no la de la API) y `PGPASSWORD` (Postgres de solo lectura). `VI_API_KEYS` es la clave de la API: la que va a Supabase como `VI_API_KEY`. Mostrar la clave una sola vez y guardarla en un gestor de contraseñas; si se vio en una pantalla compartida, rotarla antes de usarla (mismo comando `sed` desde una ventana privada y `systemctl --user restart vi-api`). `VI_API_VECTOR_SEARCH=0` (solo SQL) es el valor por defecto.

## 4. Servicio de usuario
```bash
mkdir -p ~/.config/systemd/user && cat > ~/.config/systemd/user/vi-api.service <<'EOF'
[Unit]
Description=Vera Intelligence API (Tigo)
After=network-online.target

[Service]
WorkingDirectory=%h/vi-scripts
EnvironmentFile=%h/.config/vera-intelligence/api.env
ExecStart=%h/vi-venv/bin/python api_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now vi-api && sleep 8 && systemctl --user status vi-api --no-pager | head -12 && curl -s http://127.0.0.1:8080/health
```
Esperado: `active (running)` y `{"status":"ok","client":"tigo_alto","vector_search":false,...}`. `Restart=always` es necesario: la API se cierra sola al detectar un Data Map nuevo. Logs: `journalctl --user -u vi-api -n 40 --no-pager`.

## 5. Prueba real (sin imprimir la clave)
```bash
cd ~ && K=$(grep '^VI_API_KEYS=' ~/.config/vera-intelligence/api.env | cut -d= -f2 | cut -d, -f1) && cat > /tmp/q.json <<'EOF'
{"question": "¿Cuál es la tasa de compra por región?"}
EOF
J=$(curl -s -X POST http://127.0.0.1:8080/v1/ask -H "Authorization: Bearer $K" -H "X-VI-User: prueba-vm" -H "Content-Type: application/json" --data-binary @/tmp/q.json | ~/vi-venv/bin/python -c "import sys,json; print(json.load(sys.stdin)['job_id'])") && for i in $(seq 1 60); do R=$(curl -s http://127.0.0.1:8080/v1/jobs/$J -H "Authorization: Bearer $K" -H "X-VI-User: prueba-vm"); S=$(echo "$R" | ~/vi-venv/bin/python -c "import sys,json; print(json.load(sys.stdin)['status'])"); echo "$i: $S"; [ "$S" = done ] || [ "$S" = error ] && break; sleep 5; done; echo "$R" | ~/vi-venv/bin/python -c "import sys,json; d=json.load(sys.stdin); print((d.get('answer') or d.get('message') or '')[:900])"; unset K; rm -f /tmp/q.json
```
Referencia (todo el histórico): BOGOTA 18,34 %, ORIENTE 18,23 %, COSTA 17,59 %, ANDINA 16,33 %, SUR 14,42 %.

## 6. Mantener el Data Map al día (cron, 4 veces al día)
```bash
( crontab -l 2>/dev/null | grep -v "vera-api.*git pull" ; echo '30 0,6,12,18 * * * cd $HOME/vera-api && flock -n /tmp/vi-pull.lock git pull --ff-only -q >> $HOME/.config/vera-intelligence/pull.log 2>&1' ) | crontab - && crontab -l
```
Corre a las 00:30, 06:30, 12:30 y 18:30 (UTC), una hora después del refresco de las 05:30. Cuando entra un Data Map nuevo, la API espera a que terminen las preguntas en curso, se cierra y systemd la levanta con el nuevo. Un cambio de **código** no reinicia la API: `systemctl --user restart vi-api` a mano.

## 7. HTTPS (pendiente; lo hacen administradores)
Supabase solo llama a URLs públicas con HTTPS. Opción elegida: **Caddy** con un subdominio. Requiere (a) abrir TCP 80 y 443 en el security group de AWS, (b) un registro DNS `A` hacia la IP pública (mejor una Elastic IP), (c) con sudo: instalar Caddy y un `Caddyfile` con `SUBDOMINIO { reverse_proxy 127.0.0.1:8080 }`. La API sigue escuchando solo en `127.0.0.1`. Alternativas descartadas por ahora: túnel rápido de Cloudflare (la dirección cambia en cada reinicio) y servicio gestionado con el `Dockerfile` de esta carpeta.

## 8. Supabase / Lovable
Ver `../API_LOVABLE.md` (sección 6) y `../ejemplos_lovable/PROMPT_PARA_LOVABLE.md`. Secretos de la Edge Function: `VI_API_URL` (`https://SUBDOMINIO`, sin barra final), `VI_API_KEY` (la clave del paso 3) y `VI_ALLOWED_EMAILS=management@tigo.com`.

## 9. Operación
- **Estado:** `systemctl --user status vi-api`; `curl -s http://127.0.0.1:8080/health`.
- **Reiniciar:** `systemctl --user restart vi-api`. **Apagar:** `systemctl --user stop vi-api`.
- **Rotar la clave:** nueva `VI_API_KEYS` en `api.env`, `restart`, y actualizar `VI_API_KEY` en Supabase (hasta entonces el portal dice "No pude comunicarme con el análisis").
- **Corte de acceso sin apagar la API:** poner un correo inexistente en `VI_ALLOWED_EMAILS`.
- **Encender la búsqueda vectorial:** `VI_API_VECTOR_SEARCH=1` en `api.env` y reiniciar (las cualitativas tardan 2 a 4 min).

## 10. Si algo falla
| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `python3 -m venv` falla (ensurepip) | Falta `python3-venv` | Paso 1 (administrador) |
| `ModuleNotFoundError` | Instalación cortada con Ctrl+C | Repetir el `pip install` del paso 2 completo |
| La API se cae al cerrar SSH | Sin linger | Paso 1 |
| Error al preguntar | `VERA_AI_API_KEY` o `PGPASSWORD` mal copiadas | Corregir `api.env`, `restart` |
| `/health` local bien y `https` no | DNS sin propagar o 80/443 cerrados | Revisar el security group y `sudo journalctl -u caddy -n 30` |
| El portal dice "No pude comunicarme" | `VI_API_URL` o `VI_API_KEY` mal en Supabase | Revisar secretos y los logs de la función |
