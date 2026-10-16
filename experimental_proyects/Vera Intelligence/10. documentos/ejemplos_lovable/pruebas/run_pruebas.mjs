// Banco de pruebas de los ejemplos de Lovable (vera-ask.edge.ts y useVeraChat.ts). Ejecuta el CÓDIGO REAL de los dos archivos, pero con
// un Deno, un Supabase y un React MÍNIMOS SIMULADOS, contra la API real (api_falsa.py, agente falso). NO es el entorno de Supabase ni de React:
//   - Deno.serve / Deno.env: simulados (el handler queda en memoria).
//   - createClient (Supabase): simulado; una sesión válida es el encabezado "Bearer session:<id>:<email>".
//   - supabase.functions.invoke: emula a supabase-js: un status que no es 2xx devuelve { data: null, error } con error.context = la Response.
//   - React: useState/useRef/useCallback mínimos (sin renderizado). El único cambio al hook es POLL_MS (2000 -> 50) para que las pruebas sean rápidas.
// Uso:  1) python api_falsa.py   2) node --test run_pruebas.mjs
import { readFileSync, writeFileSync, mkdtempSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import { test, before } from "node:test";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const EJ = join(HERE, "..");
const API = process.env.VI_API_URL ?? "http://127.0.0.1:8597";
const KEY = process.env.VI_API_KEY ?? "k".repeat(32);
const DATA = join(tmpdir(), "vi_api_fake_data");
const OUT = mkdtempSync(join(tmpdir(), "vi_ejemplos_"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const userHash = (id) => createHash("sha256").update(id).digest("hex").slice(0, 12);
const jsonl = (name) => (existsSync(join(DATA, name)) ? readFileSync(join(DATA, name), "utf-8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : []);
let n = 0;
const newUser = () => `usuario-${Date.now().toString(36)}-${++n}`;

// ---------------------------------------------------------------- simulaciones de Deno y Supabase
const env = {};
globalThis.Deno = { env: { get: (k) => env[k] }, serve: (h) => { globalThis.__handler = h; } };
globalThis.__createClient = (_url, _key, opts) => ({
  auth: {
    async getUser() {
      const m = /^Bearer session:([^:]+):(.+)$/.exec(opts?.global?.headers?.Authorization ?? "");
      return m ? { data: { user: { id: m[1], email: m[2], app_metadata: globalThis.__meta?.[m[1]] } }, error: null } : { data: { user: null }, error: new Error("invalid JWT") };
    },
  },
});

globalThis.__meta = {};   // app_metadata simulada por id de usuario (solo el servidor puede escribirla en Supabase)
let edgeCount = 0;
async function loadEdge(overrides = {}) {
  Object.assign(env, { VI_API_URL: API, VI_API_KEY: KEY, VI_ALLOWED_EMAIL_DOMAINS: "tigo.com.co,heyvera.ai", SUPABASE_URL: "http://supabase.local", SUPABASE_ANON_KEY: "anon" }, overrides);
  let src = readFileSync(join(EJ, "vera-ask.edge.ts"), "utf-8");
  const before = src;
  src = src.replace(/import \{ createClient \} from "https:\/\/esm\.sh\/[^"]+";/, "const createClient = globalThis.__createClient;");
  assert.notEqual(src, before, "no encontré el import de supabase-js para simular");
  const file = join(OUT, `edge_${++edgeCount}.ts`);
  writeFileSync(file, src);
  await import(pathToFileURL(file).href);
  return globalThis.__handler;
}

const edgeRequest = (handler, body, session, raw) =>
  handler(new Request("http://edge.local/vera-ask", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(session ? { Authorization: `Bearer session:${session.id}:${session.email}` } : {}) },
    body: raw ?? JSON.stringify(body),
  }));

// emula supabase.functions.invoke (supabase-js v2): un non-2xx devuelve data=null y error.context=Response
function makeSupabase(handler, session) {
  return {
    functions: {
      async invoke(_name, { body }) {
        const res = await edgeRequest(handler, body, session);
        if (!res.ok) return { data: null, error: Object.assign(new Error("Edge Function returned a non-2xx status code"), { name: "FunctionsHttpError", context: res }) };
        return { data: await res.json(), error: null };
      },
    },
  };
}

// ---------------------------------------------------------------- React mínimo para correr el hook sin renderizar
let current = null;
globalThis.__react = {
  useState(init) {
    const i = current.si++;
    if (!(i in current.states)) current.states[i] = { v: typeof init === "function" ? init() : init };
    const s = current.states[i], inst = current;
    return [s.v, (nv) => { s.v = typeof nv === "function" ? nv(s.v) : nv; inst.render(); }];
  },
  useRef(init) { const i = current.ri++; if (!(i in current.refs)) current.refs[i] = { current: init }; return current.refs[i]; },
  useCallback(f, deps) {
    const i = current.ci++, prev = current.cbs[i];
    if (prev && deps && prev.deps.length === deps.length && prev.deps.every((d, k) => Object.is(d, deps[k]))) return prev.f;
    current.cbs[i] = { f, deps };
    return f;
  },
};
function renderHook(fn) {
  const inst = { states: [], refs: [], cbs: [], si: 0, ri: 0, ci: 0, result: { current: null } };
  inst.render = () => { current = inst; inst.si = inst.ri = inst.ci = 0; inst.result.current = fn(); };
  inst.render();
  return inst.result;
}

let hookModule = 0;
async function loadHook(supabase) {
  globalThis.__supabase = supabase;
  globalThis.__supabase_proxy = { functions: { invoke: (...a) => globalThis.__supabase.functions.invoke(...a) } };
  let src = readFileSync(join(EJ, "useVeraChat.ts"), "utf-8");
  const orig = src;
  src = src.replace(/import \{ useCallback, useRef, useState \} from "react";/, "const { useCallback, useRef, useState } = globalThis.__react;")
           .replace(/import \{ supabase \} from "@\/integrations\/supabase\/client";[^\n]*/, "const supabase = globalThis.__supabase_proxy;")
           .replace("const POLL_MS = 2000;", "const POLL_MS = 50;");
  assert.notEqual(src, orig);
  assert.ok(src.includes("globalThis.__react") && src.includes("globalThis.__supabase_proxy") && src.includes("POLL_MS = 50"), "no pude simular los imports del hook");
  const file = join(OUT, `hook_${++hookModule}.ts`);
  writeFileSync(file, src);
  const mod = await import(pathToFileURL(file).href);
  return () => renderHook(mod.useVeraChat);
}

const session = (id = newUser(), email = "ana@tigo.com.co") => ({ id, email });

let handler;
before(async () => {
  const r = await fetch(`${API}/health`).catch(() => null);
  assert.ok(r?.ok, `la API de prueba no responde en ${API}: levantala con  python api_falsa.py`);
  handler = await loadEdge();
});

// ================================================================ función de Supabase (vera-ask.edge.ts)
test("edge: sin sesión o con sesión inválida responde 401", async () => {
  for (const s of [null, { id: "x", email: "x@tigo.com.co", invalid: true }]) {
    const res = await handler(new Request("http://edge.local/vera-ask", { method: "POST", headers: { "Content-Type": "application/json", ...(s ? { Authorization: "Bearer cualquiera" } : {}) }, body: JSON.stringify({ action: "ask", question: "hola" }) }));
    assert.equal(res.status, 401);
    assert.equal((await res.json()).error, "unauthorized");
  }
});

test("edge: un correo de otro dominio recibe 403 y uno permitido pasa", async () => {
  const bad = await edgeRequest(handler, { action: "ask", question: "hola" }, session(newUser(), "x@otra-empresa.com"));
  assert.equal(bad.status, 403);
  const ok = await edgeRequest(handler, { action: "ask", question: "hola" }, session());
  assert.ok(ok.status >= 200 && ok.status < 300, `status ${ok.status}`);
});

test("edge: pedidos mal formados reciben 400 y OPTIONS responde para CORS", async () => {
  const s = session();
  assert.equal((await edgeRequest(handler, null, s, "esto no es json")).status, 400);
  assert.equal((await edgeRequest(handler, { action: "inventada" }, s)).status, 400);
  assert.equal((await edgeRequest(handler, { action: "ask", question: "   " }, s)).status, 400);
  const pre = await handler(new Request("http://edge.local/vera-ask", { method: "OPTIONS" }));
  assert.equal(pre.status, 200);
  assert.ok(pre.headers.get("access-control-allow-headers")?.includes("authorization"));
});

test("edge: manda el id del usuario a la API (se registra solo su hash) y pasa pregunta, estado y feedback", async () => {
  const s = session();
  const started = await (await edgeRequest(handler, { action: "ask", question: "¿Cuántas conversaciones hay?" }, s)).json();
  assert.ok(started.job_id && started.session_id);
  let st;
  for (let i = 0; i < 100; i++) { st = await (await edgeRequest(handler, { action: "status", job_id: started.job_id }, s)).json(); if (st.status === "done") break; await sleep(50); }
  assert.equal(st.status, "done");
  assert.match(st.answer, /respuesta 1 a: ¿Cuántas conversaciones hay\?/);
  assert.ok(jsonl("interactions.jsonl").some((r) => r.job_id === started.job_id && r.user === userHash(s.id)), "la API no registró el hash del usuario esperado");
  const fb = await edgeRequest(handler, { action: "feedback", job_id: started.job_id, rating: "up", comment: "ok" }, s);
  assert.equal(fb.status, 200);
  assert.ok(jsonl("feedback.jsonl").some((r) => r.job_id === started.job_id && r.rating === "up"));
  // otro usuario no puede leer ese pedido (aislamiento)
  const other = await edgeRequest(handler, { action: "status", job_id: started.job_id }, session());
  assert.equal(other.status, 404);
});

async function askViaEdge(h, s, body) {
  const started = await (await edgeRequest(h, { action: "ask", ...body }, s)).json();
  assert.ok(started.job_id, JSON.stringify(started));
  for (let i = 0; i < 100; i++) {
    const st = await (await edgeRequest(h, { action: "status", job_id: started.job_id }, s)).json();
    if (st.status === "done") return st;
    await sleep(50);
  }
  assert.fail("el pedido no terminó");
}

test("edge: manda a la API la tienda, el asesor y el rol que salen de app_metadata (datos del servidor)", async () => {
  const s = session();
  globalThis.__meta[s.id] = { store: "Tienda Ibague", employee_name: "ANA GOMEZ", vi_role: "vendedor" };
  const st = await askViaEdge(handler, s, { question: "hola" });
  assert.match(st.answer, /tienda «Tienda Ibague»/);
  assert.match(st.answer, /asesor «ANA GOMEZ»/);
  assert.match(st.answer, /rol «vendedor»/);
});

test("edge: ignora un user_context que mande el navegador (no puede fijar quién es)", async () => {
  const s = session();
  globalThis.__meta[s.id] = { store: "Tienda Real" };
  const st = await askViaEdge(handler, s, { question: "hola", user_context: { store: "Tienda Falsa", employee: "OTRA PERSONA" } });
  assert.match(st.answer, /Tienda Real/);
  assert.ok(!/Falsa|OTRA PERSONA/.test(st.answer), st.answer);
});

test("edge: sin app_metadata no manda contexto y la pregunta llega igual", async () => {
  const s = session();
  const st = await askViaEdge(handler, s, { question: "hola" });
  assert.match(st.answer, /^respuesta 1 a: hola$/);
});

test("edge: si la API no responde devuelve 502 con mensaje amable, sin la URL interna", async () => {
  const h = await loadEdge({ VI_API_URL: "http://127.0.0.1:9" });
  const res = await edgeRequest(h, { action: "ask", question: "hola" }, session());
  assert.equal(res.status, 502);
  const body = await res.json();
  assert.equal(body.error, "bad_gateway");
  assert.ok(!JSON.stringify(body).includes("127.0.0.1"));
  await loadEdge();      // restaura el entorno bueno para el resto
});

test("edge: sin los secretos configurados responde 500 not_configured", async () => {
  const h = await loadEdge({ VI_API_KEY: "" });
  assert.equal((await edgeRequest(h, { action: "ask", question: "hola" }, session())).status, 500);
  await loadEdge();
});

test("edge: si la API rechaza la clave del servidor (VI_API_KEY mal cargada o rotada) devuelve 502 y NO un 401 que parezca una sesión vencida", async () => {
  const h = await loadEdge({ VI_API_KEY: "clave-equivocada-".padEnd(32, "x") });
  const res = await edgeRequest(h, { action: "ask", question: "hola" }, session());
  assert.equal(res.status, 502);
  const body = await res.json();
  assert.equal(body.error, "bad_gateway");
  assert.ok(!JSON.stringify(body).includes("unauthorized"));
  await loadEdge();
});

// ================================================================ hook (useVeraChat.ts) a través de la función y la API
async function setup(s = session()) {
  const h = await loadEdge();
  const make = await loadHook(makeSupabase(h, s));
  return { hook: make(), s };
}

test("hook: una pregunta devuelve respuesta, gráfico, sugerencias y jobId, y deja de estar ocupado", async () => {
  const { hook } = await setup();
  await hook.current.ask("¿Cuántas conversaciones hubo por región?");
  const m = hook.current.messages;
  assert.equal(m.length, 2);
  assert.equal(m[0].role, "user");
  assert.match(m[1].text, /^respuesta 1 a:/);
  assert.equal(m[1].charts?.[0]?.type, "hbar");
  assert.deepEqual(m[1].suggestions, ["¿Y por región?"]);
  assert.ok(m[1].jobId);
  assert.equal(hook.current.busy, false);
  assert.equal(hook.current.progress, "");
});

test("hook: la segunda pregunta sigue en la misma sesión; reset empieza una nueva", async () => {
  const { hook } = await setup();
  await hook.current.ask("uno");
  await hook.current.ask("dos");
  assert.match(hook.current.messages[3].text, /^respuesta 2 a: dos/);
  await hook.current.reset();
  assert.equal(hook.current.messages.length, 0);
  await hook.current.ask("tres");
  assert.match(hook.current.messages[1].text, /^respuesta 1 a: tres/);
});

test("hook: un error interno se muestra como aviso genérico, sin detalles técnicos", async () => {
  const { hook } = await setup();
  await hook.current.ask("boom");
  const last = hook.current.messages.at(-1);
  assert.equal(last.error, true);
  assert.ok(!/password|secreto|Traceback/i.test(last.text), last.text);
  assert.equal(hook.current.busy, false);
});

test("hook: sin acceso a los datos muestra el mensaje seguro de la API", async () => {
  const { hook } = await setup();
  await hook.current.ask("unavail");
  const last = hook.current.messages.at(-1);
  assert.equal(last.error, true);
  assert.equal(last.text, "Sin acceso a los datos.");
});

test("hook: al llegar al tope diario muestra un aviso claro y no se queda ocupado", async () => {
  const { hook } = await setup();
  for (let i = 0; i < 4; i++) await hook.current.ask(`pregunta ${i}`);
  await hook.current.ask("una de más");
  const last = hook.current.messages.at(-1);
  assert.equal(last.error, true);
  assert.match(last.text, /tope diario/i);
  assert.equal(hook.current.busy, false);
});

test("hook: si la sesión del servidor venció, la pregunta sigue en una sesión nueva sin mostrar un error", async () => {
  const s = session();
  const { hook } = await setup(s);
  await hook.current.ask("uno");
  const sid = jsonl("interactions.jsonl").filter((r) => r.user === userHash(s.id)).at(-1);
  assert.ok(sid);
  // borra la sesión del servidor directamente en la API (como si hubiera vencido o se hubiera reiniciado)
  const first = await (await edgeRequest(await loadEdge(), { action: "ask", question: "x" }, s)).json();    // sesión nueva solo para tener un id válido
  assert.ok(first.session_id);
  await fetch(`${API}/v1/sessions/${first.session_id}`, { method: "DELETE", headers: { Authorization: `Bearer ${KEY}`, "X-VI-User": s.id } });
  // fuerza al hook a usar un session_id que ya no existe
  const make = await loadHook({ functions: { async invoke(name, { body }) {
    const sup = makeSupabase(await loadEdge(), s);
    return sup.functions.invoke(name, { body: body.action === "ask" && body.session_id ? { ...body, session_id: first.session_id } : body });
  } } });
  const h2 = make();
  await h2.current.ask("primera");
  await h2.current.ask("segunda con sesión vencida");
  const errors = h2.current.messages.filter((m) => m.error);
  assert.equal(errors.length, 0, `no debería mostrar errores: ${JSON.stringify(errors)}`);
  assert.match(h2.current.messages.at(-1).text, /respuesta \d+ a: segunda con sesión vencida/);
});

test("hook: si el pedido se pierde mientras consulta, corta con un aviso en vez de esperar 8 minutos", async () => {
  const s = session();
  const h = await loadEdge();
  const base = makeSupabase(h, s);
  const make = await loadHook({ functions: { async invoke(name, { body }) {
    if (body.action === "status") return base.functions.invoke(name, { body: { ...body, job_id: "no-existe" } });
    return base.functions.invoke(name, { body });
  } } });
  const hook = make();
  const t0 = Date.now();
  await hook.current.ask("hola");
  assert.ok(Date.now() - t0 < 5000, "tardó demasiado: el hook sigue consultando un pedido que no existe");
  assert.equal(hook.current.messages.at(-1).error, true);
  assert.equal(hook.current.busy, false);
});

test("hook: sin sesión de usuario (401) avisa que hay que volver a ingresar", async () => {
  const { hook } = await setup(null);
  await hook.current.ask("hola");
  const last = hook.current.messages.at(-1);
  assert.equal(last.error, true);
  assert.match(last.text, /sesión|ingres/i);
  assert.equal(hook.current.busy, false);
});

test("hook: cancelar corta el pedido en la API y no deja un error en pantalla", async () => {
  const { hook, s } = await setup();
  const pending = hook.current.ask("hang");
  for (let i = 0; i < 100 && !hook.current.busy; i++) await sleep(20);
  assert.equal(hook.current.busy, true);
  await sleep(300);
  await hook.current.cancel();
  await pending;
  assert.equal(hook.current.busy, false);
  assert.equal(hook.current.messages.filter((m) => m.error).length, 0);
  const rec = await (async () => { for (let i = 0; i < 100; i++) { const r = jsonl("interactions.jsonl").filter((x) => x.user === userHash(s.id)).at(-1); if (r?.status) return r; await sleep(50); } })();
  assert.equal(rec?.status, "cancelled");
});

test("hook: el feedback llega a la API", async () => {
  const { hook, s } = await setup();
  await hook.current.ask("hola");
  const jobId = hook.current.messages.at(-1).jobId;
  await hook.current.sendFeedback(jobId, "down", "faltó el período");
  await sleep(100);
  assert.ok(jsonl("feedback.jsonl").some((r) => r.job_id === jobId && r.rating === "down" && r.comment === "faltó el período" && r.user === userHash(s.id)));
});

test("hook: ignora una segunda pregunta mientras hay una en curso", async () => {
  const { hook } = await setup();
  const first = hook.current.ask("hang");
  for (let i = 0; i < 100 && !hook.current.busy; i++) await sleep(20);
  const before = hook.current.messages.length;
  await hook.current.ask("otra mientras tanto");
  assert.equal(hook.current.messages.length, before);
  await hook.current.cancel();
  await first;
});

// ================================================================ prueba opcional contra el agente REAL (gasta unos centavos de Gemini)
// Uso: levantar la API real (api_server.py con clave) y correr con  VI_REAL=1 VI_API_URL=http://127.0.0.1:8598 VI_API_KEY=<clave>  node --test --test-name-pattern=real run_pruebas.mjs
test("real: el hook recibe una respuesta verdadera del agente de Tigo a través de la función", { skip: !process.env.VI_REAL }, async () => {
  const { hook } = await setup();
  await hook.current.ask("¿Cuántas conversaciones analizables hay en total?");
  const last = hook.current.messages.at(-1);
  assert.equal(last.error, undefined, last.text);
  assert.match(last.text, /\d{2,3}\.\d{3}/, "se esperaba una cifra con separador de miles");
  assert.ok(last.jobId);
  await hook.current.ask("¿Y cuántas terminaron en compra?");       // seguimiento en la misma sesión
  const follow = hook.current.messages.at(-1);
  assert.equal(follow.error, undefined, follow.text);
  assert.match(follow.text, /compra/i);
  assert.equal(hook.current.busy, false);
});

test("hook: con la clave del servidor mal cargada NO dice que la sesión venció (el usuario no puede arreglarlo re-ingresando)", async () => {
  const h = await loadEdge({ VI_API_KEY: "clave-equivocada-".padEnd(32, "x") });
  const make = await loadHook(makeSupabase(h, session()));
  const hook = make();
  await hook.current.ask("hola");
  const last = hook.current.messages.at(-1);
  assert.equal(last.error, true);
  assert.ok(!/sesión|ingres/i.test(last.text), last.text);
  assert.match(last.text, /No pude comunicarme/);
  assert.equal(hook.current.busy, false);
  await loadEdge();
});

// ================================================================ cliente de la pantalla de Tigo (veraTigoClient.ts) y lista de correos de la función
let clientModule = 0;
async function loadClient(supabase) {
  globalThis.__supabase = supabase;
  globalThis.__supabase_proxy = { functions: { invoke: (...a) => globalThis.__supabase.functions.invoke(...a) } };
  let src = readFileSync(join(EJ, "veraTigoClient.ts"), "utf-8");
  const orig = src;
  src = src.replace(/import \{ supabase \} from "@\/integrations\/supabase\/client";[^\n]*/, "const supabase = globalThis.__supabase_proxy;");
  assert.notEqual(src, orig);
  const file = join(OUT, `cliente_${++clientModule}.ts`);
  writeFileSync(file, src);
  return import(pathToFileURL(file).href);
}
async function poll(client, jobId) {
  for (let i = 0; i < 200; i++) {
    const s = await client.status(jobId);
    if (s.status !== "running") return s;
    await sleep(30);
  }
  throw new Error("no terminó");
}

test("cliente de Tigo: ask + status devuelven respuesta, gráfico y sugerencias con la forma que usa la pantalla", async () => {
  const s = session();
  const client = await loadClient(makeSupabase(handler, s));
  const { jobId, sessionId } = await client.ask("¿Cuántas conversaciones hubo por región?");
  assert.ok(jobId && sessionId);
  const done = await poll(client, jobId);
  assert.equal(done.status, "done");
  assert.match(done.answer, /^respuesta 1 a:/);
  assert.equal(done.charts?.[0]?.type, "hbar");
  assert.deepEqual(done.suggestions, ["¿Y por región?"]);
  const next = await client.ask("seguimiento", sessionId);
  assert.equal(next.sessionId, sessionId);
  assert.match((await poll(client, next.jobId)).answer, /^respuesta 2 a: seguimiento/);
});

test("cliente de Tigo: una sesión vencida se reemplaza sin error y un error de la API sale como mensaje claro", async () => {
  const s = session();
  const client = await loadClient(makeSupabase(handler, s));
  const { jobId } = await client.ask("con sesión inexistente", "sesion-que-no-existe-0000");
  assert.match((await poll(client, jobId)).answer, /con sesión inexistente/);
  await assert.rejects(client.status("job-que-no-existe"), /Se perdió esa consulta/);
  for (let i = 0; i < 3; i++) await poll(client, (await client.ask(`p${i}`)).jobId);   // con la primera pregunta, 4 = el tope de la API de prueba
  await assert.rejects(client.ask("una de más"), /tope diario/i);
});

test("cliente de Tigo: cancelar corta el pedido y la API lo registra como cancelado", async () => {
  const s = session();
  const client = await loadClient(makeSupabase(handler, s));
  const { jobId } = await client.ask("hang");
  await sleep(300);
  await client.cancel(jobId);
  const end = await poll(client, jobId);
  assert.equal(end.status, "cancelled");
});

test("edge: con VI_ALLOWED_EMAILS solo pasa el correo exacto, aunque el dominio sea válido", async () => {
  const strict = await loadEdge({ VI_ALLOWED_EMAILS: "management@tigo.com", VI_ALLOWED_EMAIL_DOMAINS: "" });
  try {
    const ok = await edgeRequest(strict, { action: "ask", question: "hola" }, session(newUser(), "Management@Tigo.com"));
    assert.ok(ok.status >= 200 && ok.status < 300, `status ${ok.status}`);
    for (const email of ["ana@tigo.com", "management@tigo.com.co", "management@heyvera.ai"]) {
      const res = await edgeRequest(strict, { action: "ask", question: "hola" }, session(newUser(), email));
      assert.equal(res.status, 403, email);
    }
  } finally {
    env.VI_ALLOWED_EMAILS = "";   // no contamina las demás pruebas
  }
});
