# Conectar la pantalla de Tigo a la API: todo listo para cuando exista la URL HTTPS (2026-10-16)

Todo lo que hace falta está en este archivo. Se usa cuando Emiliano/Pedro confirman que `https://SUBDOMINIO/health` responde. Tiempo estimado: 10 minutos, de los cuales los 2 clave son los pasos 3 y 4.

Alcance siempre igual: **solo `management@tigo.com`**, nada más del portal cambia, sin historial. Ningún valor secreto se pega en el chat de Lovable: se tipean en su formulario de secretos.

## 0. Antes de empezar (1 minuto)

1. Desde tu PC: `curl.exe -s https://SUBDOMINIO/health` debe devolver `{"status":"ok","client":"tigo_alto",...}`. Si no, no seguir: la URL todavía no está lista.
2. **Rotar la clave de la API** desde una ventana de PowerShell propia (no el panel compartido con Claude): entrar a la VM por SSH y correr
   ```bash
   K=$(~/vi-venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))") && sed -i "s|^VI_API_KEYS=.*|VI_API_KEYS=$K|" ~/.config/vera-intelligence/api.env && echo "$K" && unset K && systemctl --user restart vi-api
   ```
   Guardar la clave que imprime en un gestor de contraseñas. Esa es `VI_API_KEY` para el formulario.
3. Tener a mano: la URL (`https://SUBDOMINIO`, **sin barra final**), la clave del punto 2 y el correo `management@tigo.com`.

## 1. Prompt 0: crear la función `vera-ask` en Lovable

Pegar este texto en el chat de Lovable (el bloque de código ya está incluido; no hay que agregar nada):

````text
Mismo alcance de siempre: solo management@tigo.com; no modifiques nada más. Creá una función de servidor (edge function) NUEVA llamada vera-ask con EXACTAMENTE el código que pego abajo (no lo reescribas, no lo "mejores" ni cambies nada; no modifiques ninguna de las funciones existentes). Necesita tres secretos: VI_API_URL, VI_API_KEY y VI_ALLOWED_EMAILS. NO me pidas los valores en el chat: mostrame el formulario seguro de secretos de Lovable para que los escriba yo ahí. Para esta función dejá la verificación de JWT del gateway como esté por defecto (el código ya valida al usuario con getUser). Después decime si la función quedó desplegada.

```ts
// Supabase Edge Function: puente seguro entre la pantalla de Lovable y la API del agente.
// Archivo: supabase/functions/vera-ask/index.ts   (el nombre de la función es "vera-ask")
//
// Qué hace: (1) exige un usuario logueado en Supabase, (2) opcionalmente lo limita a dominios de correo permitidos,
// (3) reenvía la pregunta a la API del agente con la clave guardada como SECRETO (el navegador nunca la ve),
// (4) devuelve el estado del pedido. Es un EJEMPLO: revisarlo antes de usarlo.
//
// Secretos a cargar en Supabase (Project Settings → Edge Functions → Secrets), nunca en el código ni en Lovable:
//   VI_API_URL                   https://api.tu-dominio.com        (sin barra final)
//   VI_API_KEY                   la clave de VI_API_KEYS de la API
//   VI_ALLOWED_EMAILS            correos exactos permitidos, por ejemplo "management@tigo.com" (recomendado: solo esa cuenta)
//   VI_ALLOWED_EMAIL_DOMAINS     opcional, por ejemplo "tigo.com.co,heyvera.ai"
// SUPABASE_URL y SUPABASE_ANON_KEY ya los inyecta Supabase.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

const API_URL = (Deno.env.get("VI_API_URL") ?? "").replace(/\/$/, "");
const API_KEY = Deno.env.get("VI_API_KEY") ?? "";
const ALLOWED = (Deno.env.get("VI_ALLOWED_EMAIL_DOMAINS") ?? "")
  .split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);

const ALLOWED_EMAILS = (Deno.env.get("VI_ALLOWED_EMAILS") ?? "")
  .split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);

const cors = {
  "Access-Control-Allow-Origin": "*", // endurecer: poner el dominio de la app
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { ...cors, "Content-Type": "application/json" } });

// `userId` = el id del usuario logueado (user.id de Supabase): la API lo usa para el tope por usuario y para que cada
// persona vea solo sus sesiones y pedidos. Es un identificador, no un secreto.
async function callApi(userId: string, path: string, init: RequestInit = {}) {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { ...(init.headers ?? {}), Authorization: `Bearer ${API_KEY}`, "X-VI-User": userId, "Content-Type": "application/json" },
    signal: AbortSignal.timeout(20_000),
  });
  const body = await res.json().catch(() => ({ error: "bad_gateway" }));
  // Un 401 de la API significa que la CLAVE del servidor (VI_API_KEY) es incorrecta o fue rotada: es un problema de configuración, no del usuario.
  // Si se le pasara tal cual al navegador, el usuario vería "tu sesión venció" y volver a ingresar no arreglaría nada. Se registra para quien opera
  // el servicio y al usuario se le devuelve un 502 genérico. Lo mismo si la API dice que falta el identificador del usuario (error nuestro).
  if (res.status === 401 || body?.error === "user_required") {
    console.error(`vera-ask: la API rechazó el pedido por configuración (${res.status} ${body?.error ?? ""}); revisar VI_API_KEY y el encabezado X-VI-User`);
    return { status: 502, body: { error: "bad_gateway", message: "No pude comunicarme con el análisis. Intentá de nuevo en unos minutos." } };
  }
  return { status: res.status, body };
}

// Quién pregunta (tienda, asesor, rol), para que "mi tienda", "yo" o "mi equipo" se resuelvan sin repreguntar. Sale SIEMPRE de `app_metadata`, que solo
// puede escribir el servidor (nunca el usuario), y jamás de lo que llega en el pedido: el navegador no puede fijarlo ni cambiarlo. Si el portal todavía
// no guarda estos datos, no se manda nada y la API repregunta como antes. Nombres de los campos a confirmar con el portal real: store, employee_name, vi_role.
function userContext(user: { app_metadata?: Record<string, unknown> }) {
  const meta = user.app_metadata ?? {};
  const pick = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim().slice(0, 100) : undefined);
  const ctx = { store: pick(meta.store), employee: pick(meta.employee_name), role: pick(meta.vi_role) };
  return Object.values(ctx).some(Boolean) ? ctx : undefined;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (!API_URL || !API_KEY) return json({ error: "not_configured" }, 500);

  // 1) usuario autenticado
  const supabase = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_ANON_KEY")!, {
    global: { headers: { Authorization: req.headers.get("Authorization") ?? "" } },
  });
  const { data: { user }, error } = await supabase.auth.getUser();
  if (error || !user) return json({ error: "unauthorized" }, 401);

  // 2) dominio permitido (opcional pero recomendado)
  // Lista de correos exactos (p. ej. "management@tigo.com"): si está definida, SOLO esas cuentas pasan, aunque el dominio sea válido.
  if (ALLOWED_EMAILS.length && !ALLOWED_EMAILS.includes((user.email ?? "").toLowerCase())) return json({ error: "forbidden" }, 403);
  const domain = (user.email ?? "").split("@")[1]?.toLowerCase() ?? "";
  if (ALLOWED.length && !ALLOWED.includes(domain)) return json({ error: "forbidden" }, 403);

  const body = await req.json().catch(() => null);
  if (!body || typeof body.action !== "string") return json({ error: "bad_request" }, 400);

  // 3) reenvío. El agente conserva el historial por session_id: el front guarda el session_id que devuelve "ask".
  try {
    if (body.action === "ask") {
      if (typeof body.question !== "string" || !body.question.trim()) return json({ error: "bad_request" }, 400);
      const r = await callApi(user.id, "/v1/ask", {
        method: "POST",
        body: JSON.stringify({ question: body.question, session_id: body.session_id ?? undefined, user_context: userContext(user) }),
      });
      return json(r.body, r.status);
    }
    if (body.action === "status" && typeof body.job_id === "string") {
      const r = await callApi(user.id, `/v1/jobs/${encodeURIComponent(body.job_id)}`);
      return json(r.body, r.status);
    }
    if (body.action === "cancel" && typeof body.job_id === "string") {
      const r = await callApi(user.id, `/v1/jobs/${encodeURIComponent(body.job_id)}/cancel`, { method: "POST" });
      return json(r.body, r.status);
    }
    if (body.action === "reset" && typeof body.session_id === "string") {
      const r = await callApi(user.id, `/v1/sessions/${encodeURIComponent(body.session_id)}`, { method: "DELETE" });
      return json(r.body, r.status);
    }
    if (body.action === "feedback" && typeof body.job_id === "string" && (body.rating === "up" || body.rating === "down")) {
      const r = await callApi(user.id, "/v1/feedback", {
        method: "POST",
        body: JSON.stringify({ job_id: body.job_id, rating: body.rating, comment: typeof body.comment === "string" ? body.comment : "" }),
      });
      return json(r.body, r.status);
    }
    return json({ error: "bad_request" }, 400);
  } catch (_e) {
    return json({ error: "bad_gateway", message: "No pude comunicarme con el análisis. Intentá de nuevo." }, 502);
  }
});

// Ya resuelto en la API (la función solo manda X-VI-User): tope de preguntas por usuario, sesiones y pedidos propios de cada usuario, feedback.
// Pendiente aquí: limitar el origen CORS ("*" arriba) al dominio de la app.
```
````

Cuando aparezca el formulario de secretos, tipear a mano:

| Secreto | Valor |
|---|---|
| `VI_API_URL` | `https://SUBDOMINIO` (sin barra final) |
| `VI_API_KEY` | la clave rotada en el paso 0 |
| `VI_ALLOWED_EMAILS` | `management@tigo.com` |

## 2. Prompt B: conectar la pantalla de Tigo a la función

Pegar en el chat de Lovable (una vez que la función quedó desplegada):

````text
Mismo alcance: solo management@tigo.com; no toques nada más. Reemplazá el contenido COMPLETO del archivo src/lib/veraTigoClient.ts por el código que pego abajo, sin cambiar los nombres ni las firmas de ask, status y cancel (la pantalla VeraIntelligenceTigo.tsx ya los usa). Si la ruta del import de supabase es otra en este proyecto, ajustá solo esa línea. No modifiques ninguna otra pantalla, función ni tabla. Si VeraIntelligenceTigo.tsx necesitara un cambio mínimo para seguir funcionando (por ejemplo, para mostrar el mensaje de error que ahora llega en la excepción), hacelo solo en ese archivo y decime qué cambiaste.

```ts
// Cliente de la pantalla "Vera Intelligence" de Tigo. Archivo: src/lib/veraTigoClient.ts
// Llama a la Edge Function "vera-ask" (la clave de la API nunca llega al navegador). Mantiene las firmas que usa VeraIntelligenceTigo.tsx:
//   ask(question, sessionId?) -> { jobId, sessionId }     status(jobId) -> VeraStatus     cancel(jobId)
// La pregunta se consulta cada ~2 s hasta que status() devuelve "done", "error" o "cancelled" (lo hace la pantalla, como con la respuesta simulada).
import { supabase } from "@/integrations/supabase/client"; // ajustar a la ruta real del proyecto

export type VeraChart = { type: string; title: string; [k: string]: unknown };
export type VeraStatus = {
  status: "running" | "done" | "error" | "cancelled";
  progress?: string;
  answer?: string;
  charts?: VeraChart[];
  suggestions?: string[];
  message?: string;
};

// Mensajes para el usuario según el código de error de la función y de la API (nunca detalles técnicos).
const FRIENDLY: Record<string, string> = {
  unauthorized: "Tu sesión venció. Volvé a ingresar al portal.",
  forbidden: "Tu usuario no tiene acceso a Vera Intelligence.",
  daily_limit: "Se alcanzó el tope diario de preguntas. Probá de nuevo mañana.",
  user_daily_limit: "Alcanzaste el tope diario de preguntas. Probá de nuevo mañana.",
  too_many_running: "Ya tenés consultas en curso; esperá a que terminen.",
  busy: "Hay muchas consultas en curso. Reintentá en un momento.",
  restarting: "El servicio se está actualizando. Reintentá en unos segundos.",
  question_too_long: "La pregunta es demasiado larga. Probá con una más corta.",
  job_not_found: "Se perdió esa consulta. Volvé a preguntar.",
  session_not_found: "La conversación venció. Volvé a preguntar.",
  bad_gateway: "No pude comunicarme con el análisis. Intentá de nuevo en unos minutos.",
  not_configured: "El servicio todavía no está configurado.",
};
const explain = (r: { error?: string; message?: string }) =>
  FRIENDLY[r.error ?? ""] ?? r.message ?? "No pude comunicarme con el análisis.";

// Con supabase-js, una respuesta que NO es 2xx llega como `error` con la Response en `error.context` y `data` en null: se lee ese cuerpo para no perder el motivo.
async function call(payload: Record<string, unknown>): Promise<any> {
  const { data, error } = await supabase.functions.invoke("vera-ask", { body: payload });
  if (!error) return data;
  let body: any = null;
  try { body = await (error as any).context?.json(); } catch { /* sin cuerpo legible: red caída u otro fallo */ }
  return { error: body?.error ?? "unreachable", message: body?.message };
}

export async function ask(question: string, sessionId?: string): Promise<{ jobId: string; sessionId: string }> {
  let started = await call({ action: "ask", question, session_id: sessionId });
  if (started.error === "session_not_found" && sessionId) {
    started = await call({ action: "ask", question }); // la sesión del servidor venció (o se reinició): se sigue en una nueva, sin mostrar un error
  }
  if (started.error) throw new Error(explain(started));
  return { jobId: started.job_id, sessionId: started.session_id };
}

export async function status(jobId: string): Promise<VeraStatus> {
  const s = await call({ action: "status", job_id: jobId });
  if (s.error) throw new Error(explain(s)); // pedido perdido, sesión vencida, servicio caído: la pantalla corta con un aviso, no sigue consultando
  if (s.status === "done") return { status: "done", answer: s.answer, charts: s.charts, suggestions: s.suggestions };
  if (s.status === "error") return { status: "error", message: s.message ?? "No pude completar el análisis." };
  if (s.status === "cancelled") return { status: "cancelled", message: s.message };
  return { status: "running", progress: s.progress }; // "queued" y "running"
}

export async function cancel(jobId: string): Promise<void> {
  await call({ action: "cancel", job_id: jobId }).catch(() => undefined);
}
```
````

## 3. Pruebas de aceptación (en la vista previa, antes de publicar)

Con `management@tigo.com`:
1. Tocar la tarjeta "¿Cuál es la tasa de compra por región?": responde en ~25 segundos con BOGOTA 18,3 %, ORIENTE 18,2 %, COSTA 17,6 %, ANDINA 16,3 %, SUR 14,4 % (cifras de todo el histórico; pueden variar levemente).
2. Una pregunta de seguimiento ("¿y cuál de esas es la más baja?"): debe entender el contexto.
3. Una pregunta con gráfico ("mostrame la tasa de compra por región en un gráfico de barras"): se dibuja el gráfico.
4. "Cancelar" a mitad de una respuesta.
5. "+ Nueva" y volver a preguntar.

Con **otra cuenta** (no management): no aparece el menú, `/vera-intelligence-tigo` redirige, y si se invoca la función `vera-ask` devuelve 403.

Si todo pasa, **Publicar** en Lovable.

## 4. Si algo falla

| Síntoma | Qué mirar |
|---|---|
| "No pude comunicarme con el análisis" | `VI_API_URL` o `VI_API_KEY` mal cargados en los secretos (la función registra el motivo en sus logs); probar `curl.exe -s https://SUBDOMINIO/health` |
| "Tu usuario no tiene acceso" | `VI_ALLOWED_EMAILS` no coincide con el correo exacto de la cuenta |
| "Tu sesión venció" | Cerrar sesión y volver a entrar |
| Tarda más de 1 minuto | Es una pregunta cualitativa o pesada; la API corta a los 420 s. Estado en la VM: `systemctl --user status vi-api` y `journalctl --user -u vi-api -n 40 --no-pager` |
| "Hay muchas consultas en curso" | Tope de cola o de concurrencia: normal bajo carga |

Para cortar el acceso rápido sin apagar nada: poner un correo inexistente en `VI_ALLOWED_EMAILS`. Para apagar la API: `systemctl --user stop vi-api` en la VM.
