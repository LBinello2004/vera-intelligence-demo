# Optimizaciones durante el uso

La numeración corresponde a la ronda de cinco alternativas posterior a
`OPTIMIZACIONES_COSTO_LATENCIA.md`, no a su lista original de diez.
El usuario pidió implementar las opciones 3, 4 y 5 de esta ronda (2026-09-14).

| Opción | Estado |
|---|---|
| 1. Traducir identificadores conocidos mediante código | Propuesta, no implementada |
| 2. Construir gráficos desde resultados SQL sin copiar valores en la salida del modelo | Propuesta, no implementada |
| 3. Detener análisis abandonados | Implementada |
| 4. Resolver cortesías localmente | Implementada |
| 5. Cortar ante fallas operativas que el agente no puede reparar | Implementada |

## 3. Cancelación cooperativa

La interfaz ofrece **Cancelar análisis** mientras trabaja y **Cancelar presentación** al generar
una bienvenida nueva. Nueva conversación, cambio de cliente, rerun que reemplaza el análisis y
desconexión de la sesión cancelan las etapas posteriores de la ejecución anterior.

`runtime_control.AnalysisControl` usa un evento y el estado real de conexión de Streamlit.
El agente verifica el control antes de cada turno, en los chunks del stream, antes de ejecutar
herramientas y al reintentar chat o embeddings. El control se propaga a los workers de herramientas.
Los backoffs se pueden interrumpir sin esperar el intervalo completo.

El historial curado del SDK se conserva antes de iniciar el análisis. Una cancelación restaura ese
snapshot, conservando las firmas existentes, y registra localmente la cancelación cuando corresponde.
El resultado o falla tardía se descarta y no puede restaurar el historial sobre una conversación nueva.
Cada rerun tiene una identidad; se verifica antes de iniciar trabajo y antes de escribir respuestas
o contexto, bajo el mismo lock del cliente. La cancelación prevalece sobre una falla que llega tarde.
Un lock impide cambiar el cliente global del MVP mientras termina el trabajo ya enviado.

Es cancelación cooperativa: una conexión, consulta o generación que ya empezó puede terminar y
tener costo. No se promete devolución de tokens ni cancelación inmediata de llamadas bloqueantes.
Una conversación nueva puede esperar a que esa operación libere el cliente activo.
Si se corta un stream antes de recibir su metadata final, los logs locales no demuestran su costo final.

## 4. Cortesías sin llamada al modelo

Una lista cerrada reconoce mensajes completos como “gracias”, “muchas gracias”, “gracias Vera”
y “gracias por el análisis”, con variaciones de mayúsculas, espacios y puntuación básica.
Devuelve una respuesta local y registra el intercambio en el historial del SDK mediante
`record_history`, sin enviar una petición. Se mantienen las sugerencias previas en la interfaz.

No intercepta confirmaciones como “sí”, “dale” o “perfecto”, ni mensajes mixtos como
“gracias, ¿cuántas ventas hubo?”. No cambia el análisis de negocio.

## 5. Errores operativos definitivos para la ejecución

`OperationalUnavailable` transporta un mensaje seguro sin credenciales, SQL ni nombres físicos.
Los repositorios de criterios y RAG la usan sólo cuando no pueden obtener el contenido necesario
ni existe un fallback válido; los snapshots y el cache en memoria mantienen su comportamiento.

La clasificación del agente distingue fallas de conexión/autenticación/permisos de errores de
consulta. Una falla definitiva de herramienta termina el análisis sin otro turno de Gemini para
intentar repararla. Un error 401/403 al crear el cache impide iniciar una generación con ese acceso.
Los errores transitorios de chat y embeddings conservan su backoff; al agotarlo no se inicia otra
ronda del agente para repetir la misma operación indisponible.

Columnas o tablas equivocadas, timeout de consulta, deadlock y serialización siguen llegando al
agente como errores corregibles. La transacción se limpia también cuando el error ocurre después
de reconectar, evitando contaminar consultas posteriores.

La interfaz restaura el historial previo y registra la respuesta local de indisponibilidad.
Si tampoco puede reconstruir el chat, conserva el snapshot y evita reutilizar el intercambio
incompleto hasta recuperar acceso. El modo CLI también conserva historial y trata Ctrl+C durante
el análisis como una cancelación.

## Verificación

- Suite completa: **294 tests en verde**; incluye los controles anteriores de SQL, RAG, gráficos,
  evidencia numérica y las optimizaciones previas.
- Nuevos casos: cortesías sin llamadas y con historial, mensajes mixtos, cancelación antes/después
  del modelo y durante herramientas/stream/backoff, workers, desconexión y cambio de cliente.
- Errores: acceso definitivo sin vuelta del modelo, consultas corregibles, retries transitorios,
  autenticación del cache, rollback tras reconexión y recuperación de historial.
- Streamlit AppTest con widgets reales y backend simulado: cortesía 0 generaciones, pregunta mixta
  1 generación, falla operativa sin turno adicional; rerun, Nueva conversación y selector de cliente
  cancelan y restauran el contexto. Fixture local en `.runtime/runtime_smoke/app_fixture.py`.
- Validación AST de la entrada y scripts compartidos. Se usó el Python 3.12.14 del runtime de Codex
  con las dependencias de `.venv`, cuyo ejecutable apunta a una instalación ausente. No se reparó el entorno.

No se hicieron llamadas pagas ni consultas a producción. No se midió ahorro facturado: el beneficio
depende de cuántas cortesías, abandonos y fallas operativas ocurran durante el uso.
