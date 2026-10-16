# Prompt para pegar en Lovable (pantalla de chat de Vera Intelligence para Tigo)

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
> Por ahora la respuesta es SIMULADA: encapsulá todo el acceso al análisis en un único archivo `src/lib/veraClient.ts` con las funciones `ask(question, sessionId?)` (devuelve `{jobId, sessionId}`), `status(jobId)` (devuelve `{status: "running"|"done"|"error"|"cancelled", progress, answer, charts, suggestions, message}`) y `cancel(jobId)`, y hacé que hoy devuelvan datos de ejemplo (esperar ~3 segundos mostrando el progreso "Consultando los datos", y devolver una respuesta de ejemplo en Markdown con una lista, un gráfico de barras `{type:"bar", title, labels, values}` y 3 sugerencias). Más adelante cambiaremos solo ese archivo por llamadas reales; el resto de la pantalla no debe depender de datos simulados.
>
> Mostrá cada respuesta como Markdown; los `charts` debajo del texto con Recharts (`bar`/`hbar` = barras con `labels` y `values`; `line`/`area`; `pie`/`donut`; `grouped_bar`/`stacked_bar` con `series` [{name, values}]; `scatter`/`bubble` con `x_values`, `values`, `sizes`); las `suggestions` como chips clicables debajo de la respuesta. Mientras responde, indicador con el texto de `progress` y un botón "Cancelar" (llama a `cancel`). Botón "Nueva conversación" arriba. Los errores se muestran con estilo de aviso y sin detalles técnicos. Solo de consulta: ninguna función de edición. Español, estilo del resto de la app, y que se vea bien en celular.

## Paso 2 (cuando la API esté desplegada): conectar de verdad

1. Confirmar en el proyecto de Lovable que tiene backend (Lovable Cloud / Supabase conectado, para poder crear funciones de servidor y guardar secretos). Si no lo tiene, activarlo desde la configuración del proyecto.
2. Pedirle a Lovable crear la función de servidor `vera-ask` con el contenido de `vera-ask.edge.ts` (o crearla a mano) y **cargar los secretos con la interfaz de secretos de Lovable/Supabase**: `VI_API_URL`, `VI_API_KEY`, y opcional `VI_ALLOWED_EMAIL_DOMAINS`. Los valores los pega una persona directamente ahí; no van por chat ni en el código.
3. Pedirle a Lovable: "Reemplazá el contenido de `src/lib/veraClient.ts` para que `ask`, `status` y `cancel` llamen a la función `vera-ask` con `supabase.functions.invoke` (acciones `ask`, `status`, `cancel`), sin tocar el resto de la pantalla". La lógica de consulta cada 2 s está en `useVeraChat.ts` como referencia.
4. Para limitar el acceso: que "Vera Intelligence" aparezca solo para los usuarios de Tigo (por el rol o la organización que ya use el portal) y que la función `vera-ask` también lo verifique del lado servidor.

## Otros clientes más adelante

Cada despliegue de la API atiende **un solo cliente** (el agente fija el cliente por proceso). Para habilitar otro cliente: otra instancia de la API con `VI_API_CLIENT=<cliente>`, otra clave, y que la función `vera-ask` elija la URL y la clave según el cliente del usuario (secretos por cliente). Conviene no hacerlo hasta que Tigo esté estable.
