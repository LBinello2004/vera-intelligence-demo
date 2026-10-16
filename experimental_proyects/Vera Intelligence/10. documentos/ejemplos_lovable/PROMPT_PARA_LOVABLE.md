# Prompt para Lovable: VERSIÓN VIGENTE (2026-10-16). Las versiones de abajo son anteriores y quedan solo como historial.

Decisiones tomadas: el backend es el Supabase de Lovable Cloud (confirmado por Agus). La API corre en la VM de Pedro. **Solo la cuenta `management@tigo.com` ve y usa esta función; ninguna otra cuenta ni pantalla cambia.** No hay historial de conversaciones por ahora.

Orden: (1) pegar el prompt A para armar la pantalla con respuestas simuladas (no necesita la API), (2) cuando haya URL HTTPS, pegar el prompt 0 (crea la función `vera-ask` y pide los secretos), (3) pegar el prompt B (conecta la pantalla a la función).

## Aclaración (2026-10-16, Agus): no hay un Supabase aparte
El Supabase del portal es el que viene dentro de Lovable (Lovable Cloud). No hay un panel externo al que dar acceso: la función y los secretos se crean desde Lovable.

## Prompt 0 (crear la función `vera-ask` y pedir los secretos; se hace cuando ya hay URL HTTPS de la API)

> Mismo alcance de siempre: solo `management@tigo.com`; no modifiques nada más. Creá una función de servidor (edge function) NUEVA llamada `vera-ask` (no modifiques ninguna de las existentes) con exactamente el código que pego abajo (no lo reescribas ni "mejores"). Necesita tres secretos: `VI_API_URL`, `VI_API_KEY` y `VI_ALLOWED_EMAILS`. NO me pidas los valores en el chat: mostrame el formulario seguro de secretos de Lovable para que los escriba yo ahí. `VI_ALLOWED_EMAILS` va a ser `management@tigo.com`. Después decime cómo la invoco desde el frontend.
>
> (pegar acá el contenido completo de `vera-ask.edge.ts`)

Los valores los tipea una persona en el formulario de secretos de Lovable (`VI_API_URL` = `https://SUBDOMINIO` sin barra final; `VI_API_KEY` = la clave de la API; `VI_ALLOWED_EMAILS` = `management@tigo.com`). Si Lovable no ofrece ese formulario, no pegar valores en el chat: pedir ayuda a Agus.

## Prompt A (pantalla nueva e independiente, respuestas simuladas) [rehecho el 2026-10-16 tras revisar el código del portal]

Hallazgos de Lovable: existen `VeraIntelligenceSteren.tsx`, `VeraIntelligenceMulticlient.tsx` y `VeraIntelligenceV2Multicliente.tsx`; ninguna sirve a Tigo (solo Haber/Steren), el sidebar filtra por `emails`/`brands` en `AppSidebar.tsx`, y esas pantallas usan `:::CHART(...):::` con `columns`/`rows` e historial en `chat_conversations`. Por eso se crea una página NUEVA que no reutiliza sus funciones ni tablas.

> REGLA PRINCIPAL: este cambio es SOLO para el usuario con el correo `management@tigo.com`. No modifiques nada más: no toques `VeraIntelligenceSteren.tsx`, `VeraIntelligenceMulticlient.tsx`, `VeraIntelligenceV2Multicliente.tsx`, sus rutas, ninguna de las edge functions existentes (en especial `vera-intelligence-multiclient`, `vera-orchestrator` y las de Tigo), ninguna tabla, ni la lógica del menú para otros usuarios. Para cualquier otra cuenta (incluidos los usuarios SSO de Tigo y el admin) la aplicación debe quedar exactamente igual que hoy. Si algo de lo siguiente exigiera tocar otra cosa, no lo hagas y avisame.
>
> 1. Creá una página NUEVA `src/pages/VeraIntelligenceTigo.tsx`, ruta `/vera-intelligence-tigo`, con el mismo layout y estilo visual que `VeraIntelligenceSteren.tsx` (encabezado con ícono de chispa, título "Vera Intelligence", subtítulo "Consultas en tiempo real de tu negocio", botón "+ Nueva", pantalla de bienvenida con tarjetas de preguntas y campo de entrada abajo con botón de enviar). Copiá solo la apariencia; no importes su lógica de datos.
> 2. NO incluyas botón "Historial", ni "Gráficos autogenerados", ni guardado de gráficos, ni lectura o escritura de ninguna tabla (no uses `chat_conversations`, `chat_messages` ni `auto_generated_charts`). La conversación vive solo en el estado de la página.
> 3. En `src/components/layout/AppSidebar.tsx` agregá UN ítem nuevo "Vera Intelligence" (ícono de chispa, como el de Steren), debajo de "Insights Tigo", que lleve a `/vera-intelligence-tigo` y se muestre ÚNICAMENTE si el correo del usuario es `management@tigo.com` (usá la misma convención `emails: [...]`). No debe mostrarse a usuarios SSO de Tigo, ni al admin, ni a nadie más, y no cambies el filtrado de los ítems existentes. Protegé también la ruta: si otra cuenta entra a mano, redirigila a su pantalla de inicio sin mostrar nada.
> 4. Bienvenida: título "Bienvenido a Vera Intelligence", texto "Consultas en tiempo real de tu negocio. Hacé cualquier pregunta sobre tus datos, pedí gráficos, tablas y análisis." y 4 tarjetas clicables: "¿Cuál es la tasa de compra por región?", "¿Cuáles son las principales causas de retiro de los clientes?", "¿Qué tiendas tienen mejor cumplimiento del protocolo?", "¿Qué trámites son los más frecuentes?". Campo "Preguntale a Vera sobre tu negocio...".
> 5. Respuestas: mensajes del usuario a la derecha y del asistente a la izquierda; Markdown con `react-markdown` (negritas, listas, tablas). Los gráficos llegan como un campo `charts` aparte (NO con `:::CHART:::`) con estos formatos y se dibujan con Recharts: `bar`/`hbar` = `{type, title, labels: string[], values: number[]}`; `line`/`area` igual; `pie`/`donut` igual; `grouped_bar`/`stacked_bar` = `{type, title, labels, series: [{name, values}]}`; `scatter`/`bubble` = `{type, title, x_values, values, sizes?}`. Dibujá solo lo que llega, sin inventar datos. Las `suggestions` aparecen como chips clicables debajo de la respuesta.
> 6. Mientras responde: indicador con el texto de `progress` y botón "Cancelar" (una respuesta puede tardar hasta 4 minutos; no bloquees la pantalla). Errores con estilo de aviso, sin detalles técnicos. Solo consulta, sin edición ni borrado. Español, bien en celular.
> 7. Por ahora la respuesta es SIMULADA: todo el acceso al análisis va en un único archivo `src/lib/veraTigoClient.ts` con `ask(question, sessionId?)` (devuelve `{jobId, sessionId}`), `status(jobId)` (devuelve `{status: "running"|"done"|"error"|"cancelled", progress, answer, charts, suggestions, message}`) y `cancel(jobId)`; que hoy devuelvan datos de ejemplo (3 segundos con el progreso "Consultando los datos", una respuesta en Markdown con una lista, un gráfico de barras y 3 sugerencias).

## Prompt B (conectar de verdad, cuando la API ya corre en la VM)

> Mismo alcance: solo `management@tigo.com`; no toques nada más. Reemplazá el contenido de `src/lib/veraTigoClient.ts` para que `ask`, `status` y `cancel` llamen a la función `vera-ask` con `supabase.functions.invoke` (acciones `ask`, `status`, `cancel`; también existen `reset` y `feedback`), sin tocar el resto de la pantalla. Una respuesta que no es 2xx llega como `error` con la Response en `error.context`: leé ese cuerpo. Usá como referencia `useVeraChat.ts`.

Los secretos (`VI_API_URL`, `VI_API_KEY`, `VI_ALLOWED_EMAILS`) los carga una persona en el panel de secretos de Supabase; no van por chat ni en el código. El control real de acceso es el de la Edge Function (403 para cualquier otra cuenta); ocultar el menú es solo comodidad.

---

# (Anterior) Prompt para pegar en Lovable (pantalla de chat de Vera Intelligence para Tigo)

Antes de pegarlo: crear en Supabase la función `vera-ask` con el código de `vera-ask.edge.ts` y cargar sus secretos (ver ese archivo).
El hook `useVeraChat.ts` de este directorio puede pegarse tal cual en `src/hooks/`.

---

Agregá una pantalla "Preguntale a Vera" (ruta `/preguntar`, solo para usuarios logueados) que sea un chat para hacer preguntas de negocio sobre las conversaciones de venta de Tigo.

Requisitos de la pantalla:
1. Usá el hook `useVeraChat` de `src/hooks/useVeraChat.ts` (ya existe; no lo reescribas). Toda la comunicación pasa por la Edge Function `vera-ask` de Supabase. No llames a ninguna otra URL ni guardes ninguna clave en el código.
2. Interfaz tipo chat: mensajes del usuario a la derecha y respuestas a la izquierda, campo de texto abajo con botón "Preguntar" (Enter envía). Mientras `busy` es verdadero mostrá un indicador con el texto de `progress` (por ejemplo "Consultando los datos") y un botón "Cancelar" que llama a `cancel()`. Una respuesta puede tardar hasta 2 minutos: no bloquees la pantalla.
3. Mostrá cada respuesta como Markdown (negritas, listas y tablas). Si `charts` trae gráficos, mostralos debajo del texto con una librería de gráficos (Recharts): `bar` y `hbar` = barras verticales/horizontales con `labels` y `values`; `line` y `area` = línea/área; `pie` y `donut` = torta/dona; `grouped_bar` y `stacked_bar` = barras agrupadas/apiladas con `series` (cada una con `name` y `values`); `scatter` y `bubble` = dispersión con `x_values` y `values` (en `bubble`, `sizes`). Usá el `title` como título y no inventes datos: dibujá solo lo que llega.
4. Debajo de cada respuesta, mostrá las `suggestions` como botones/chips; al hacer clic se envía esa pregunta.
5. Arriba, un botón "Nueva conversación" que llama a `reset()`. Cuando el chat está vacío, mostrá 4 preguntas de ejemplo como chips: "¿Cuántas conversaciones hubo por región?", "¿Cuáles son las principales causas de retiro de los clientes?", "¿Cuál es la tasa de retención exitosa por región?", "¿Qué trámites son los más frecuentes?".
6. Los mensajes con `error` van con estilo de aviso (no rojo agresivo) y sin detalles técnicos.
7. Diseño sobrio y legible, en español, con el estilo del resto de la aplicación. Debe verse bien en celular.
8. No agregues ninguna función de edición ni de borrado de datos: es solo de consulta.

---

# Versión para el proyecto "Vera Insights Portal" (decidido el 2026-10-15)

Lugar: en el menú lateral de la app (grupo "Principal": "Dashboard", "Insights Tigo"), **debajo de "Insights Tigo"**, una opción nueva **"Vera Intelligence"** que abre un chat. Más adelante puede habilitarse en otros lugares y para otros clientes.

## Paso 1 (hoy, sin backend): la pantalla con respuestas simuladas

Pegar esto en Lovable. Sirve para tener la interfaz lista aunque la API todavía no esté desplegada:

> En el menú lateral, debajo de "Insights Tigo", agregá una opción "Vera Intelligence" (con un ícono de chispa o burbuja de chat, igual estilo que las otras) que lleve a una nueva ruta `/vera-intelligence`, dentro del mismo layout y con la misma protección de login que "Dashboard" e "Insights Tigo".
>
> La pantalla es un chat para hacer preguntas de negocio sobre las conversaciones de Tigo: mensajes del usuario a la derecha y respuestas a la izquierda, campo de texto abajo con botón "Preguntar" (Enter envía). Cuando el chat está vacío mostrá un título corto, una frase ("Preguntale a Vera sobre las conversaciones de Tigo") y 4 preguntas de ejemplo como chips clicables: "¿Cuántas conversaciones hubo por región?", "¿Cuáles son las principales causas de retiro de los clientes?", "¿Cuál es la tasa de retención exitosa por región?", "¿Qué trámites son los más frecuentes?".
>
> Por ahora la respuesta es SIMULADA: encapsulá todo el acceso al análisis en un único archivo `src/lib/veraTigoClient.ts` con las funciones `ask(question, sessionId?)` (devuelve `{jobId, sessionId}`), `status(jobId)` (devuelve `{status: "running"|"done"|"error"|"cancelled", progress, answer, charts, suggestions, message}`) y `cancel(jobId)`, y hacé que hoy devuelvan datos de ejemplo (esperar ~3 segundos mostrando el progreso "Consultando los datos", y devolver una respuesta de ejemplo en Markdown con una lista, un gráfico de barras `{type:"bar", title, labels, values}` y 3 sugerencias). Más adelante cambiaremos solo ese archivo por llamadas reales; el resto de la pantalla no debe depender de datos simulados.
>
> Mostrá cada respuesta como Markdown; los `charts` debajo del texto con Recharts (`bar`/`hbar` = barras con `labels` y `values`; `line`/`area`; `pie`/`donut`; `grouped_bar`/`stacked_bar` con `series` [{name, values}]; `scatter`/`bubble` con `x_values`, `values`, `sizes`); las `suggestions` como chips clicables debajo de la respuesta. Mientras responde, indicador con el texto de `progress` y un botón "Cancelar" (llama a `cancel`). Botón "Nueva conversación" arriba. Los errores se muestran con estilo de aviso y sin detalles técnicos. Solo de consulta: ninguna función de edición. Español, estilo del resto de la app, y que se vea bien en celular.

## Paso 2 (cuando la API esté desplegada): conectar de verdad

1. Confirmar en el proyecto de Lovable que tiene backend (Lovable Cloud / Supabase conectado, para poder crear funciones de servidor y guardar secretos). Si no lo tiene, activarlo desde la configuración del proyecto.
2. Pedirle a Lovable crear la función de servidor `vera-ask` con el contenido de `vera-ask.edge.ts` (o crearla a mano) y **cargar los secretos con la interfaz de secretos de Lovable/Supabase**: `VI_API_URL`, `VI_API_KEY`, y opcional `VI_ALLOWED_EMAIL_DOMAINS`. Los valores los pega una persona directamente ahí; no van por chat ni en el código.
3. Pedirle a Lovable: "Reemplazá el contenido de `src/lib/veraTigoClient.ts` para que `ask`, `status` y `cancel` llamen a la función `vera-ask` con `supabase.functions.invoke` (acciones `ask`, `status`, `cancel`), sin tocar el resto de la pantalla". La lógica de consulta cada 2 s está en `useVeraChat.ts` como referencia.
4. Para limitar el acceso: que "Vera Intelligence" aparezca solo para los usuarios de Tigo (por el rol o la organización que ya use el portal) y que la función `vera-ask` también lo verifique del lado servidor.

## Otros clientes más adelante

Cada despliegue de la API atiende **un solo cliente** (el agente fija el cliente por proceso). Para habilitar otro cliente: otra instancia de la API con `VI_API_CLIENT=<cliente>`, otra clave, y que la función `vera-ask` elija la URL y la clave según el cliente del usuario (secretos por cliente). Conviene no hacerlo hasta que Tigo esté estable.
