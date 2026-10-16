// Hook de React para el chat con el agente (EJEMPLO para pegar en Lovable o dárselo como referencia).
// Usa la Edge Function "vera-ask" de este mismo directorio; la clave de la API nunca llega al navegador.
import { useCallback, useRef, useState } from "react";
import { supabase } from "@/integrations/supabase/client"; // ajustar a la ruta real del proyecto

// Formatos de gráfico que devuelve el agente (ya validados del lado servidor):
//  bar | hbar | line | area | pie | donut        -> { type, title, labels: string[], values: number[] }
//  grouped_bar | stacked_bar                       -> { type, title, labels: string[], series: {name, values: number[]}[] }
//  scatter | bubble                                -> { type, title, x_values: number[], values: number[], sizes?: number[] }
export type VeraChart = { type: string; title: string; [k: string]: unknown };
export type VeraMessage = {
  role: "user" | "assistant";
  text: string;
  charts?: VeraChart[];
  suggestions?: string[];
  error?: boolean;
  jobId?: string; // para enviar feedback sobre esta respuesta
};

const POLL_MS = 2000;
const MAX_WAIT_MS = 8 * 60 * 1000; // la API corta a los 420 s; este es el tope del lado del navegador

export function useVeraChat() {
  const [messages, setMessages] = useState<VeraMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState("");
  const sessionId = useRef<string | undefined>(undefined);
  const jobId = useRef<string | undefined>(undefined);
  const cancelled = useRef(false);

  const call = async (payload: Record<string, unknown>) => {
    const { data, error } = await supabase.functions.invoke("vera-ask", { body: payload });
    if (error) throw new Error("No pude comunicarme con el análisis.");
    return data as any;
  };

  const ask = useCallback(async (question: string) => {
    if (busy || !question.trim()) return;
    cancelled.current = false;
    setBusy(true);
    setProgress("Enviando la pregunta");
    setMessages((m) => [...m, { role: "user", text: question }]);
    try {
      const started = await call({ action: "ask", question, session_id: sessionId.current });
      if (started.error === "session_not_found") sessionId.current = undefined; // venció: la próxima va en sesión nueva
      if (started.error) throw new Error(started.message ?? "No se pudo iniciar el análisis.");
      sessionId.current = started.session_id;
      jobId.current = started.job_id;

      const t0 = Date.now();
      while (!cancelled.current && Date.now() - t0 < MAX_WAIT_MS) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        const s = await call({ action: "status", job_id: started.job_id });
        if (s.progress) setProgress(s.progress); // "Consultando los datos", "Calculando estadísticas", ...
        if (s.status === "done") {
          setMessages((m) => [...m, { role: "assistant", text: s.answer, charts: s.charts, suggestions: s.suggestions, jobId: s.job_id }]);
          return;
        }
        if (s.status === "error" || s.status === "cancelled") {
          setMessages((m) => [...m, { role: "assistant", text: s.message ?? "No pude completar el análisis.", error: s.status === "error" }]);
          return;
        }
      }
      if (!cancelled.current) throw new Error("La consulta tardó demasiado. Probá con una pregunta más acotada.");
    } catch (e: any) {
      setMessages((m) => [...m, { role: "assistant", text: e.message ?? "Ocurrió un error.", error: true }]);
    } finally {
      setBusy(false);
      setProgress("");
    }
  }, [busy]);

  const cancel = useCallback(async () => {
    cancelled.current = true;
    if (jobId.current) await call({ action: "cancel", job_id: jobId.current }).catch(() => undefined);
    setBusy(false);
    setProgress("");
  }, []);

  // 👍/👎 sobre una respuesta (rating: "up" | "down"); el job_id de cada respuesta viene en `messages[i].jobId`.
  const sendFeedback = useCallback(async (jobIdToRate: string, rating: "up" | "down", comment = "") => {
    await call({ action: "feedback", job_id: jobIdToRate, rating, comment }).catch(() => undefined);
  }, []);

  const reset = useCallback(async () => {
    if (sessionId.current) await call({ action: "reset", session_id: sessionId.current }).catch(() => undefined);
    sessionId.current = undefined;
    setMessages([]);
  }, []);

  return { messages, busy, progress, ask, cancel, reset, sendFeedback };
}
