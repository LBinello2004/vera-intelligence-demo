# Pruebas de los ejemplos de Lovable (`vera-ask.edge.ts` y `useVeraChat.ts`)

Ejecutan el **código real** de los dos archivos, pero con un Deno, un Supabase y un React **mínimos simulados**, contra la API real (con un agente falso, sin Gemini ni base de datos). Detalle de qué se simula, en el encabezado de `run_pruebas.mjs`.

```bash
# desde esta carpeta, con el entorno virtual del repo
python api_falsa.py                 # terminal 1: la API en 127.0.0.1:8597 con un agente falso
node --test run_pruebas.mjs         # terminal 2: 19 pruebas (+1 opcional contra el agente real, que se omite sola) (hace falta Node 22.6 o más; sin instalar nada)
```
Prueba opcional contra el agente REAL de Tigo (gasta unos centavos de Gemini): levantar la API real (`api_server.py` con una clave), y
`VI_REAL=1 VI_API_URL=http://127.0.0.1:8598 VI_API_KEY=<clave> node --test --test-name-pattern=real run_pruebas.mjs`.

**Qué cubren:** autenticación (sesión inválida 401, dominio no permitido 403, pedidos mal formados 400, CORS), que la función mande el usuario a la API (solo se registra su hash) y reenvíe pregunta, estado, cancelación y feedback, la API caída (502 sin la URL interna), sin secretos (500) y con la clave del servidor rechazada (502, no un 401 que parezca una sesión vencida); del hook: respuesta con gráfico y sugerencias, historial entre preguntas, reset, error interno sin detalles técnicos, sin acceso a datos, tope diario, sesión del servidor vencida (se sigue sin error), pedido perdido (corta con aviso), 401, cancelar, feedback y segunda pregunta mientras hay una en curso y la clave del servidor mal cargada (no dice que la sesión venció).

**Qué NO cubren (hay que probarlo en Lovable/Supabase reales):** el runtime de Deno y de Supabase (la verificación real del JWT, los secretos, el despliegue de la función), el renderizado de React (el hook se ejecuta con un React simulado, sin pantallas), los tipos de TypeScript (Node solo borra las anotaciones; no hay `tsc`), y la pantalla que genere Lovable.

**Historial:** la primera corrida (2026-10-16) dio 14 de 17 y encontró tres defectos del hook: con `supabase-js` una respuesta que no es 2xx llega como `error` genérico y el hook perdía el motivo (el tope diario salía como "No pude comunicarme"), una sesión vencida del servidor dejaba al usuario trabado en errores, y un 401 no avisaba que había que volver a ingresar. Se corrigió el hook (lee el cuerpo de `error.context`, reintenta sin sesión si venció, corta ante un error al consultar el estado y traduce los códigos a mensajes claros) y quedó todo en verde, también contra el agente real de Tigo. Después se encontró y corrigió un cuarto defecto, esta vez en la función: una clave del servidor rechazada por la API (401) llegaba al navegador y el hook decía "tu sesión venció"; ahora la función devuelve un 502 genérico y registra el motivo para quien opera el servicio.
