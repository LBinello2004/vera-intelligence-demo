# Paso a paso: la API de Vera Intelligence en la VM (2026-10-16)

Para quien la arma (Lucas, con acceso a la VM de Pedro). Sirve también para dárselo a un LLM. **Nada de esto se probó todavía en Linux**: el código de la API sí se probó en Windows (39 tests y corridas reales contra Tigo). Antes de empezar, leer `../API_LOVABLE.md` (secciones 3, 5 y 7).

**Regla:** ningún valor secreto va en el repo ni en el chat. Se escriben a mano, en la VM, en `/etc/vera-intelligence/api.env`, y en el panel de secretos de Supabase.

Qué se obtiene al final: `https://<dominio>/health` responde, la Edge Function `vera-ask` de Supabase llama a la API con una clave, y solo `management@tigo.com` puede usarla.

---

## 0. Antes de tocar nada (5 min)

Anotar estos datos (se usan abajo):
- `RUTA` = carpeta del clon de la demo en la VM. La guía del refresco diario la llama `vera-demo` (`REFRESCO_DIARIO_VM.md`, paso 2). Si ya existe ese clon, **se reutiliza**: no se hace un segundo clon. Averiguarla con `ls ~` y `cd ~/vera-demo && git remote -v` (debe apuntar a `LBinello2004/vera-intelligence-demo`).
- `USUARIO` = usuario Linux que corre el refresco (el servicio de abajo tiene `pedro` de ejemplo).
- Si la VM tiene otra cosa escuchando en el puerto 8080: `ss -ltnp | grep 8080`. Si está ocupado, usar otro puerto (en `VI_API_PORT` y en el proxy).
- Python: `RUTA/.venv/bin/python --version` (3.11 o 3.12 sirven).

## 1. Código al día

```bash
cd RUTA
git pull --ff-only
git log --oneline -1          # debe mostrar el commit "Sincroniza: decisiones Lovable..." o uno posterior
```
Si `git pull` se queja de cambios locales, no forzar: avisar a Pedro (el refresco diario hace commits desde esta VM).

## 2. Dependencias

```bash
cd RUTA
.venv/bin/pip install -r requirements.txt -r "experimental_proyects/Vera Intelligence/4. scripts/requirements-api.txt"
.venv/bin/python -c "import starlette, uvicorn; print('ok')"
```

## 3. Clave de la API y archivo de variables

1. Generar la clave que usará Supabase (se muestra una sola vez; copiarla a un gestor de contraseñas, no al chat):
   ```bash
   RUTA/.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. Crear el archivo de variables fuera del repo:
   ```bash
   sudo mkdir -p /etc/vera-intelligence
   sudo cp "RUTA/experimental_proyects/Vera Intelligence/10. documentos/deploy_api/env.api.example" /etc/vera-intelligence/api.env
   sudo chmod 600 /etc/vera-intelligence/api.env
   sudo nano /etc/vera-intelligence/api.env
   ```
3. En el editor completar **solo estos valores** (el resto de las líneas ya tienen buen valor por defecto):
   - `VI_API_KEYS=` la clave del punto 1.
   - `VERA_AI_API_KEY=` la clave de Gemini (la misma que usa el refresco; está en `RUTA/.env`, copiar el valor de ahí).
   - `PGPASSWORD=` la contraseña de Postgres (idem).
   - Opcional: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` (también en `RUTA/.env`).
   - Opcional pero recomendado: `VI_API_ALERT_WEBHOOK=` el mismo webhook de Slack del refresco (avisa arranques, reinicios y errores repetidos).
   - Dejar `VI_API_CLIENT=tigo_alto`, `VI_API_VECTOR_SEARCH=1`, `VI_API_HOST=127.0.0.1`, `VI_API_REQUIRE_USER=1`.
4. Comprobar que no quedó ningún marcador: `sudo grep -n "REEMPLAZAR" /etc/vera-intelligence/api.env` (no debe devolver nada).

## 4. Servicio systemd

1. Copiar y ajustar:
   ```bash
   sudo cp "RUTA/experimental_proyects/Vera Intelligence/10. documentos/deploy_api/vi-api.service" /etc/systemd/system/vi-api.service
   sudo nano /etc/systemd/system/vi-api.service
   ```
   Cambiar en el servicio: `User=` por `USUARIO`, y las dos rutas (`WorkingDirectory=` y `ExecStart=`) por la real. **Ojo:** el archivo trae `/home/pedro/vera-intelligence-demo/...` de ejemplo; el clon real puede llamarse `vera-demo`. `WorkingDirectory` termina en `experimental_proyects/Vera Intelligence/4. scripts` (con espacios: systemd lo acepta tal cual en `WorkingDirectory`, pero en `ExecStart` solo va la ruta del python y `api_server.py`).
2. Dejar `Restart=always`: sin eso la API **no** se levanta sola después de cerrarse por un Data Map nuevo.
3. Arrancar:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now vi-api
   systemctl status vi-api --no-pager
   journalctl -u vi-api -n 50 --no-pager
   ```
   Esperado en el log: arranque sin traza de error y, si el calentamiento está activo, una pregunta de prueba corta (puede tardar ~1 min).
4. Probar local en la VM:
   ```bash
   curl -s http://127.0.0.1:8080/health
   # -> {"status":"ok","client":"tigo_alto","vector_search":true,"warm":...}
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/v1/status
   # -> 401 (sin clave)
   ```

## 5. Primera pregunta real, todavía sin internet de por medio

El cuerpo se manda desde un archivo UTF-8 (en curl directo los acentos pueden romperse):
```bash
cat > /tmp/q.json <<'EOF'
{"question": "¿Cuál es la tasa de compra por región?"}
EOF
KEY='pegar-la-clave-aqui'          # solo en tu terminal, no en un archivo
curl -s -X POST http://127.0.0.1:8080/v1/ask -H "Authorization: Bearer $KEY" -H "X-VI-User: prueba-vm" -H "Content-Type: application/json" --data-binary @/tmp/q.json
# -> {"job_id":"...","session_id":"..."}
curl -s http://127.0.0.1:8080/v1/jobs/<job_id> -H "Authorization: Bearer $KEY" -H "X-VI-User: prueba-vm"
```
Repetir el segundo comando cada pocos segundos hasta `"status":"done"`. Comparar con lo conocido: regiones BOGOTA 18,34 % y SUR 14,42 % (sobre todo el histórico; los valores cambian un poco con el tiempo). Borrar `/tmp/q.json` y limpiar el historial del shell si la clave quedó ahí (`history -d` o `unset KEY`).

## 6. HTTPS (necesario: Supabase solo llama a URLs públicas con HTTPS)

Elegir una opción. La API escucha solo en `127.0.0.1`: **no abrir el puerto 8080 al exterior**.

**Opción 1: túnel de Cloudflare** (no abre puertos en la VM; hace falta una cuenta de Cloudflare y un dominio o subdominio en ella):
```bash
# instalar cloudflared según la guía oficial de Cloudflare para tu distribución, luego:
cloudflared tunnel login
cloudflared tunnel create vera-api
cloudflared tunnel route dns vera-api api.<tu-dominio>
# archivo ~/.cloudflared/config.yml con: tunnel: <id>, credentials-file: <ruta>, ingress: [{hostname: api.<tu-dominio>, service: http://127.0.0.1:8080}, {service: http_status:404}]
sudo cloudflared service install
```
**Opción 2: Caddy** (la VM debe ser alcanzable por internet en 80/443 y el dominio debe apuntar a su IP pública; certificado automático):
```
api.<tu-dominio> {
    reverse_proxy 127.0.0.1:8080
}
```
(en `/etc/caddy/Caddyfile`, luego `sudo systemctl reload caddy`; abrir solo 80 y 443 en el firewall.)

Si no hay dominio todavía (D4), pedirle a Pedro un subdominio. Comprobar desde tu PC (no desde la VM):
```
curl -s https://api.<tu-dominio>/health
```

## 7. Supabase (lo hace una persona, en el panel)

1. Edge Function `vera-ask`: crearla con el contenido de `ejemplos_lovable/vera-ask.edge.ts` (verificación de JWT activada).
2. Secretos (Project Settings → Edge Functions → Secrets), escritos a mano:
   - `VI_API_URL` = `https://api.<tu-dominio>` (sin barra final).
   - `VI_API_KEY` = la clave del paso 3.
   - `VI_ALLOWED_EMAILS` = `management@tigo.com`.
3. Después seguir con el prompt B de `ejemplos_lovable/PROMPT_PARA_LOVABLE.md`.

## 8. Pruebas de aceptación (cuando todo está conectado)

1. Con `management@tigo.com`: una pregunta simple (segundos), una con gráfico, un seguimiento ("¿y cuál de esas…?") y Cancelar.
2. Con otra cuenta cualquiera: no aparece el menú y, si se llama a la función, responde 403.
3. Reinicio: `sudo systemctl restart vi-api` y volver a preguntar (la sesión anterior puede vencer: el front debe seguir en una nueva sin mostrar error).
4. Una pregunta cualitativa (causas de retiro) tarda 2 a 4 minutos: es lo esperado hoy (la tabla de embeddings no tiene índice).
5. Mirar `journalctl -u vi-api -f` mientras se prueba.

## 9. Operación diaria

- **Actualizar el código:** el refresco hace `git pull` solo; cuando traiga código nuevo de la API hay que reiniciar a mano: `sudo systemctl restart vi-api`. Cuando el cambio es solo de Data Map, la API se cierra sola y systemd la levanta.
- **Ver el estado:** `curl -s https://api.<tu-dominio>/health` y, con clave, `/v1/status`.
- **Rotar la clave:** generar una nueva, editar `api.env`, `sudo systemctl restart vi-api`, actualizar `VI_API_KEY` en Supabase. Hasta actualizarla, la función responderá "No pude comunicarme con el análisis".
- **Apagar de emergencia:** `sudo systemctl stop vi-api` (el portal muestra un aviso amistoso y el resto de la aplicación no se afecta). Para cortar solo el acceso sin apagar la API, poner un correo inexistente en el secreto `VI_ALLOWED_EMAILS`.
- **Carga:** la VM también corre el refresco diario (05:30 días hábiles). Si una pregunta pesada coincide, puede tardar más; ver `uptime` y el log.

## 10. Si algo falla

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| El servicio no arranca, "no hay claves" | `VI_API_KEYS` vacía o de menos de 24 caracteres | Corregir `api.env`; restart |
| `ModuleNotFoundError` | Dependencias en otro entorno virtual | Usar el `.venv` del clon en `ExecStart`; repetir paso 2 |
| Error de Postgres / Gemini al preguntar | Falta `PGPASSWORD` o `VERA_AI_API_KEY` en `api.env` | Completar y reiniciar |
| `/health` local bien, `https` no | Túnel/Caddy mal configurado o DNS sin propagar | Revisar su log; probar de nuevo en unos minutos |
| El portal dice "No pude comunicarme" | `VI_API_URL` o `VI_API_KEY` mal en Supabase (la función registra el motivo en sus logs) | Revisar los secretos y los logs de la función |
| Cada reinicio tarda y la primera pregunta es lenta | Calentamiento al arrancar | Normal; se puede apagar con `VI_API_WARMUP=0` |
| 503 `busy` / 429 | Topes de cola o por usuario | Normal bajo carga; ajustar `VI_API_MAX_*` solo si hace falta |
