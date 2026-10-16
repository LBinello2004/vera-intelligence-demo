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
