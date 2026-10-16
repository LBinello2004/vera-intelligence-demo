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
//   VI_ALLOWED_EMAIL_DOMAINS     opcional, por ejemplo "tigo.com.co,heyvera.ai"
// SUPABASE_URL y SUPABASE_ANON_KEY ya los inyecta Supabase.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

const API_URL = (Deno.env.get("VI_API_URL") ?? "").replace(/\/$/, "");
const API_KEY = Deno.env.get("VI_API_KEY") ?? "";
const ALLOWED = (Deno.env.get("VI_ALLOWED_EMAIL_DOMAINS") ?? "")
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
