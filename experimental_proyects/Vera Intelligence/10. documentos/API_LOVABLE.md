# Conectar Vera Intelligence a Lovable (Tigo): API del agente + guía

Estado al 2026-10-15. Pensado para leerlo con un LLM y ejecutarlo paso a paso.

> El orden de trabajo, los responsables, los criterios de aceptación y los riesgos están en `PLAN_VERA_INTELLIGENCE_LOVABLE.md`.

## 1. Qué hay armado y qué falta decidir

**Armado y probado (no depende de ninguna decisión):**
- `4. scripts/api_server.py`: API HTTP del agente (Starlette). Un proceso atiende un cliente (por defecto `tigo_alto`) con la **búsqueda vectorial encendida**, igual que la demo (se apaga con `VI_API_VECTOR_SEARCH=0`). 34 tests (`5. tests/test_api_server.py`, con un agente falso, sin Gemini ni base).
- Probada en vivo en local contra el agente real de Tigo (primero con la búsqueda apagada y luego con el default nuevo, encendida; ver la sección 9): `/health`, rechazo sin clave (401), una pregunta real de punta a punta (26 s, números iguales a la consulta directa a la base, gráfico y sugerencias) y una pregunta de seguimiento en la misma sesión (21 s, entendió "esas regiones").
- `10. documentos/deploy_api/`: `Dockerfile`, servicio `systemd` para la VM Linux y plantilla de variables (`env.api.example`, sin valores reales).
- `10. documentos/ejemplos_lovable/`: función segura para Supabase (`vera-ask.edge.ts`), hook de React (`useVeraChat.ts`) y el prompt para Lovable (`PROMPT_PARA_LOVABLE.md`).
- `4. scripts/requirements-api.txt`: dependencias extra (no se tocó el `requirements.txt` de Streamlit Cloud).

**Falta decidir (hoy no se sabe):**
1. **Qué proyecto de Lovable se usa** (el existente "Vera Insights Portal" u otro nuevo).
2. **Dónde corre la API** (la VM de Pedro u otro servicio). Las dos opciones están descritas en la sección 5; el código es el mismo.

**Falta hacer cuando se decida (no está hecho):** desplegar, cargar los secretos, crear la función en Supabase, pegar el hook y el prompt en Lovable, y probar con un usuario de Tigo. Nada de esto se hizo.

## 2. Arquitectura

```
Navegador (pantalla de Lovable, usuario de Tigo logueado)
   │  supabase.functions.invoke("vera-ask")      ← sin claves en el navegador
   ▼
Supabase Edge Function "vera-ask"    valida el usuario, guarda VI_API_KEY como secreto
   │  HTTPS + Authorization: Bearer <VI_API_KEY>
   ▼
API del agente (api_server.py)       cliente fijo = tigo_alto, SQL + búsqueda vectorial, tope diario, historial por sesión
   │
   ├─► Gemini (VERA_AI_API_KEY)
   └─► Postgres de solo lectura (PGPASSWORD), con el filtro de tenant que ya aplica el agente
```
Una pregunta tarda de ~10 s a varios minutos (las cualitativas con búsqueda), así que la API es de **pedido + consulta de estado**: el front pide, recibe un `job_id` y consulta cada ~2 s hasta que termina (mostrando el progreso: "Consultando los datos", etc.).

## 3. Contrato de la API

Todas las rutas `/v1` piden `Authorization: Bearer <clave>` (o `X-API-Key`) **y `X-VI-User: <id del usuario>`** (el `user.id` de Supabase; letras, números y `. _ @ : + -`, hasta 128; se puede dejar de exigir con `VI_API_REQUIRE_USER=0`). Los cuerpos son JSON en UTF-8, de hasta 16 KB.

| Ruta | Qué hace |
|---|---|
| `GET /health` | Sin clave. `{"status":"ok","client":"tigo_alto","vector_search":true,"warm":true}` (`warm` pasa a `true` cuando terminó la pregunta de calentamiento) |
| `GET /v1/status` | Pedidos en curso y en cola, sesiones, Data Map vigente, si está reiniciándose y el consumo del día de la clave y del usuario |
| `POST /v1/ask` | `{"question": "...", "session_id": "opcional"}` → `202 {"job_id","session_id","status"}`. Sin `session_id` abre una sesión nueva; con él continúa la conversación |
| `GET /v1/jobs/{job_id}` | `status`: `queued`, `running`, `done`, `error` o `cancelled`; `progress`; `seconds`. Si `done`: `answer` (Markdown), `charts`, `suggestions`, `tools`. Si `error`/`cancelled`: `error_code` y `message` |
| `POST /v1/jobs/{job_id}/cancel` | Cancela un pedido en cola o en curso (devuelve el cupo del día) |
| `DELETE /v1/sessions/{session_id}` | Olvida el historial de una sesión |
| `POST /v1/feedback` | `{"job_id","rating":"up"|"down","comment"?}` (hasta 1.000 caracteres). Se registra localmente; solo el dueño del pedido puede dar feedback, hasta 24 h después |

Códigos de error: `401 unauthorized`; `400 bad_request` / `question_too_long` / `user_required`; `413 payload_too_large`; `404 session_not_found` / `job_not_found`; `429 daily_limit` (se agotó el cupo de la clave) / `user_daily_limit` (el del usuario) / `too_many_running` (ya tiene preguntas en curso); `503 busy` (cola global llena, con `Retry-After`) / `restarting` (la API se está actualizando, con `Retry-After`). Errores de estado de un pedido: `internal` (mensaje genérico, el detalle queda solo en el servidor), `unavailable` (sin acceso a datos) y `timeout`.

Ejemplo (consola):
```bash
curl -s -X POST "$URL/v1/ask" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json; charset=utf-8" \
     --data-binary @pregunta.json        # {"question": "¿Cuántas conversaciones hubo por región?"}
curl -s "$URL/v1/jobs/$JOB_ID" -H "Authorization: Bearer $KEY"
```
Nota Windows: mandar el cuerpo desde un archivo UTF-8; una consola con otra codificación genera JSON inválido y la API responde 400 (correcto).

Formatos de `charts` (ya validados del lado servidor): `bar|hbar|line|area|pie|donut` → `{type,title,labels,values}`; `grouped_bar|stacked_bar` → `{type,title,labels,series:[{name,values}]}`; `scatter|bubble` → `{type,title,x_values,values,sizes?}`.

## 4. Correrla en local (prueba)

Desde `experimental_proyects/Vera Intelligence/4. scripts`, con el entorno virtual del repo y el `.env` del agente cargado (`VERA_AI_API_KEY`, `PGPASSWORD`):
```bash
export VI_API_KEYS="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"   # guardar la clave que imprime
python api_server.py          # escucha en 127.0.0.1:8080
```
Sin `VI_API_KEYS` (o con claves de menos de 24 caracteres) la API **no arranca**: nunca se publica sin autenticación. Variables y valores por defecto: encabezado de `api_server.py` y `deploy_api/env.api.example`. Los tests: `python -m pytest "5. tests/test_api_server.py"`.

## 5. Dónde correrla (decisión pendiente)

Requisitos comunes: Python 3.12, `pip install -r requirements.txt -r "experimental_proyects/Vera Intelligence/4. scripts/requirements-api.txt"`, **un solo proceso** (las sesiones y contadores viven en memoria), **HTTPS** hacia afuera, y los secretos fuera del repo.

**Opción A: la VM Linux de Pedro** (ya clona la demo y corre el refresco diario).
1. Con la demo clonada y el `.venv` de la VM: instalar las dependencias de arriba.
2. Copiar `deploy_api/vi-api.service` a `/etc/systemd/system/`, la plantilla de variables a `/etc/vera-intelligence/api.env` (permisos 600) y completar los secretos; ajustar usuario y rutas del servicio. `sudo systemctl enable --now vi-api`.
3. HTTPS: poner delante un proxy. Lo más simple, **Caddy** con un dominio apuntando a la VM (certificado automático):
   ```
   api.tu-dominio.com {
       reverse_proxy 127.0.0.1:8080
   }
   ```
   (alternativa sin abrir puertos: un túnel de Cloudflare). La API escucha solo en `127.0.0.1`.
4. Ventaja: cero piezas nuevas, el agente ya está ahí. Riesgo: la VM también corre el refresco diario del Data Map y una pregunta pesada comparte CPU y la conexión a la base; vigilar. El refresco hace `git pull` automático: el código de la API se actualiza con la próxima corrida, pero **la API no se reinicia sola** (`sudo systemctl restart vi-api` tras un cambio).

**Opción B: servicio gestionado** (Cloud Run, Render, Fly, etc.) con el `Dockerfile` (instrucciones de construcción en su encabezado). Cargar los secretos en el panel del servicio, fijar **una sola instancia** y un **tiempo de espera de solicitud ≥ 60 s** (cada llamada de la API es corta; lo largo ocurre en segundo plano, pero una instancia que se apaga entre consulta y consulta pierde el pedido: configurar "al menos 1 instancia" o mínimo de CPU asignada). Ventaja: aislada de la VM. Costo: un servicio más que mantener.

Recomendación: **A** para el lunes (menos piezas), y pasar a **B** si el uso crece.

## 6. Conectar Lovable (cuando se elija el proyecto)

1. **Secretos en Supabase** (Project Settings → Edge Functions → Secrets): `VI_API_URL` (la URL HTTPS de la API, sin barra final), `VI_API_KEY` (una clave de `VI_API_KEYS`), y opcional `VI_ALLOWED_EMAIL_DOMAINS` (dominios de correo permitidos, por ejemplo los de Tigo y Vera).
2. **Crear la función** `vera-ask` con el código de `ejemplos_lovable/vera-ask.edge.ts` (desde el editor de Lovable o la CLI de Supabase). Mantener activa la verificación de JWT.
3. **Pegar el hook** `useVeraChat.ts` en `src/hooks/` (ajustar la ruta del cliente de Supabase).
4. **Pegar el prompt** de `PROMPT_PARA_LOVABLE.md` en el chat de Lovable para que arme la pantalla.
5. **Probar** con un usuario de Tigo: pregunta simple, pregunta con gráfico, seguimiento ("¿y cuál de esas…?") y cancelar.
6. Las credenciales de Lovable/Supabase **no se comparten por chat ni se guardan en el repo**; se cargan directamente en esos servicios.

## 7. Seguridad: lista de control

- [x] La API no arranca sin clave; compara claves en tiempo constante; no registra la clave (solo un identificador no reversible).
- [x] Las preguntas **no se registran** salvo `VI_API_LOG_QUESTIONS=1`; los errores internos no llegan al front (mensaje genérico).
- [x] Cada clave tiene tope diario (por defecto 200 preguntas); un pedido cancelado o con falla de datos devuelve el cupo; tope de tiempo por pregunta (420 s) y de concurrencia (3).
- [x] Cada sesión y cada pedido pertenecen a **(clave, usuario)**: otra clave u otro usuario de la misma clave recibe 404 (probado en vivo).
- [x] Topes: por clave (200/día), **por usuario (50/día)**, preguntas en curso por usuario (2), cola global (20, con 503), sesiones por usuario (10, se olvida la más vieja), cuerpo (16 KB), largo de pregunta y caracteres de control eliminados.
- [x] **Reinicio ordenado ante un Data Map nuevo:** cada 5 minutos compara el Data Map vigente (config + almacén del refresco diario) con el que cargó; si cambió deja de aceptar preguntas nuevas (503), espera a las en curso (hasta 10 min) y se cierra para que systemd/Docker la levante con el nuevo (por eso `Restart=always`). Verificado que no dispara falsos reinicios con el agente real.
- [x] Registro local de interacciones y feedback (`<proyecto>/.runtime/api/*.jsonl`): usuario solo como hash, sin texto de las preguntas salvo `VI_API_LOG_QUESTIONS=1`.
- [x] Avisos opcionales a Slack (`VI_API_ALERT_WEBHOOK`): arranque, reinicio por Data Map nuevo y 5 fallas en 10 minutos, con enfriamiento de 30 minutos por tipo.
- [x] Pregunta de calentamiento al arrancar (no cuenta contra ningún tope).
- [x] Cliente fijo (`tigo_alto`): el front no puede elegir otro cliente. Con la búsqueda vectorial encendida el agente lee fragmentos de conversaciones pero **nunca muestra citas textuales** (regla de integridad del agente, Iteración 29 de `6. busqueda_vectorial/README.md`), solo patrones y notas; la búsqueda no se usa para números.
- [x] Sin CORS por defecto: el navegador no llama a la API; lo hace la función de Supabase.
- [ ] **Por hacer al desplegar:** HTTPS, secretos fuera del repo, usuario de base de solo lectura (ya lo es en `utils/postgres.py`), revisar `VI_ALLOWED_EMAIL_DOMAINS`.
- [ ] **Pendiente (no incluido):** monitor externo de `/health` (UptimeRobot o similar, es configuración, no código), limitar el origen CORS de la función de Supabase, rotación de la clave, índice vectorial en la base (las cualitativas hacen cola), streaming, y una revisión de la entrada de usuarios externos (el agente ya tiene defensas contra inyección, pero no se probó con usuarios externos reales).

## 8. Costos y tiempos

- Una pregunta solo SQL cuesta ~US$ 0,015 (mediana US$ 0,012) y con búsqueda ~US$ 0,04 (medido sobre 2.155 turnos, ver `3. experimentos/busqueda_insights/COSTOS.md`). En Tigo la búsqueda queda encendida: se espera que las cualitativas cuesten ~US$ 0,04. El tope de 200 preguntas por día y clave acota el gasto a ~US$ 3 por día y clave si todas las preguntas son livianas (~US$ 0,015) y a ~US$ 8 si fueran todas pesadas (~US$ 0,04).
- Tiempo: preguntas de SQL, ~10 a 30 s (pruebas en vivo: 26 s y 21 s). Preguntas cualitativas con búsqueda: **3 a 4,5 minutos** en la prueba de 50 preguntas (3 a la vez); por eso el tope por pregunta de la API es de 420 s y el del front de 8 min. La búsqueda es un barrido exacto de ~60-90 s (no hay índice vectorial en la base) y todas las búsquedas comparten una conexión: **con varios usuarios preguntando a la vez, las cualitativas hacen cola**. La mejora de fondo es un índice vectorial en `analytics_v2.conversation_embeddings` (decisión de quien administra la base).

## 9. Qué se probó y qué no

- **Probado:** los 34 tests de la API (autenticación, dueño por usuario, topes, cola, cancelación, vencimiento, errores seguros, feedback y registro, reinicio por Data Map, calentamiento, alertas y una prueba de carga con 12 pedidos de 6 usuarios sobre 3 trabajadores; repetidos 8 veces sin intermitencias); el arranque real con el agente de Tigo con la búsqueda **encendida**: calentamiento, vigilancia del Data Map sin falsos reinicios, `400` sin usuario, una pregunta de causas de retiro (97 s, SQL + búsqueda, un gráfico y 3 sugerencias), aislamiento entre usuarios (404), feedback y registros, 0 errores en el log; coincidencia de cifras con SQL directo (prueba anterior).
- **No probado:** despliegue en una VM o servicio real; HTTPS; la función de Supabase y la pantalla de Lovable (**el código de `ejemplos_lovable/` no se ejecutó nunca**: son ejemplos a revisar); carga con varios usuarios a la vez (el agente usa una conexión a la base compartida y serializa las consultas SQL); comportamiento ante una caída de Gemini o de la base en medio de una pregunta (el agente tiene reintentos, pero no se probó contra la API); la corrida de 50 preguntas de Tigo contra la demo (en curso al escribir esto, ver el README de la carpeta cuando termine).

## 10. Actualización 2026-10-16: `user_context` y cuatro arreglos de calidad

- **`user_context`** (opcional, en `POST /v1/ask`): objeto con `store`, `employee`, `role`. La API lo sanea y lo antepone a la pregunta como "[Datos de quien pregunta, tomados del sistema…]" para que "mi tienda", "yo" o "mi equipo" se resuelvan sin repreguntar. La Edge Function lo arma SOLO desde `app_metadata` de Supabase (`store`, `employee_name`, `vi_role`), nunca desde lo que manda el navegador. Los nombres de esos campos hay que confirmarlos con el portal real. Sin esos datos la API repregunta como antes.
- **Arreglos de calidad (revisión de 20 preguntas vagas de gerente/vendedor):** (1) conteos de conversaciones se miden con COUNT sobre las vistas, no se declaran "no medidos"; (2) una pregunta general de evolución compara el último mes contra el anterior en vez de repreguntar; (3) si la verificación de cifras falla, se conserva la parte cualitativa sin números (`salvage_qualitative`) en lugar de un mensaje sin salida; (4) rankings de personas por tasa de compra o cierre se basan en conversaciones con oportunidad comercial, con la tasa sobre el total como referencia.
- **Verificación en vivo (12 preguntas, código del demo):** todas responden; sin callejones sin salida ni repreguntas innecesarias; a07 (49,5 %) y b02 siguen correctos. Las cualitativas tardan 2–4 min por la tabla de embeddings sin índice.
- **Sigue abierto:** `d01` (Data Map V4 de Tigo con el filtro de analizables), dónde corre la API (D1) y auth/roles del portal (D2/D3).
