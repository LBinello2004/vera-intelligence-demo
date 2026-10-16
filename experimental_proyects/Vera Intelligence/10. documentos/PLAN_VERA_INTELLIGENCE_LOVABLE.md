# Plan: llevar Vera Intelligence a producción dentro del portal de Lovable (Tigo)

Escrito el 2026-10-15 (jueves). **Hoy solo se planifica; no se ejecuta nada de Lovable ni de infraestructura.** Primera demostración a Tigo: **lunes 2026-10-19**. Complementa a `API_LOVABLE.md` (cómo es la API y cómo se conecta) con el orden, los responsables y los criterios de aceptación.

Responsables: **Lucas** (decisiones, Lovable), **Pedro** (VM, base de datos, claves), **Claude** (código, pruebas, documentación). Lo que dependa de terceros o no se pudo ver está marcado como *a confirmar*.

## 0. Qué se quiere lograr (en una línea)

En el portal ("Vera Insights Portal"), debajo de "Insights Tigo" en el menú lateral, una opción **"Vera Intelligence"** que abre un chat donde un usuario de Tigo ya logueado hace preguntas de negocio sobre sus conversaciones y recibe respuestas correctas, con gráficos, en un tiempo razonable, sin que ninguna clave quede en el navegador. Búsqueda vectorial **encendida** (se usa para ejemplos y patrones cualitativos; nunca para cifras).

## 1. Cómo se resuelve el login (respuesta corta)

**No se vuelve a pedir login.** El usuario ya inició sesión para entrar al portal; esa misma sesión viaja sola con cada llamada desde la pantalla de chat a la función de servidor `vera-ask` (si el portal usa Supabase Auth, `supabase.functions.invoke` adjunta la sesión actual sin que el usuario haga nada). La función comprueba que la sesión es válida y que el usuario es de Tigo, y recién entonces llama a la API del agente con una clave que el navegador nunca ve.
Hay **tres capas, y solo la primera es el login que ya existe:** (1) sesión del portal; (2) la función `vera-ask` verifica sesión + que el usuario pertenece a Tigo; (3) la API verifica su propia clave y está fijada al cliente Tigo (aunque alguien llegara a la API, solo puede consultar Tigo).
**A confirmar (hoy no lo sabemos):** cómo hace el portal el login y cómo distingue a un usuario de Tigo de uno de otro cliente (¿Supabase Auth? ¿rol, organización, tabla de usuarios?). Es la primera cosa a averiguar (ver D2): si no usara Supabase Auth, la función no podría validar la sesión de la misma forma y habría que adaptarla.

## 2. Decisiones que faltan (todas antes de construir)

| # | Decisión | Quién | Opciones / recomendación | Bloquea |
|---|---|---|---|---|
| D1 | **DECIDIDO 2026-10-16: la VM de Pedro (Lucas tiene acceso).** Dónde corre la API | Lucas + Pedro | VM de Pedro (recomendada para el lunes: cero piezas nuevas) o servicio gestionado (aislado de la VM, un servicio más) | Fases 3 y 4b |
| D2 | Cómo es el login del portal y cómo se identifica a un usuario de Tigo | Lucas (preguntarle a Lovable, solo lectura) | Prompt sugerido: "Explicame cómo funciona la autenticación de esta app, dónde se guardan los usuarios, si hay roles u organizaciones y cómo sé a qué cliente pertenece el usuario logueado. No cambies nada." | Fase 4c |
| D3 | **DECIDIDO: Supabase (lo confirmó Agus).** ¿El proyecto tiene backend (Lovable Cloud / Supabase) con secretos y funciones de servidor? | Lucas | Si no, activarlo; sin eso no hay dónde guardar la clave | Fase 4b |
| D4 | Dirección HTTPS de la API (por ejemplo un subdominio de la empresa) y quién administra el DNS | Pedro | Con la VM: Caddy + dominio (certificado automático) o túnel de Cloudflare | Fase 3 |
| D5 | Quiénes de Tigo lo ven el lunes y qué tope diario por usuario | Lucas | Lista corta de usuarios; 30 a 50 preguntas por usuario y día | Fase 2.2 |
| D6 | Privacidad: confirmar que Tigo acepta que sus preguntas se procesen con Gemini y si se registran las preguntas | Lucas | Por defecto **no** se registra el texto de las preguntas (se puede encender) | Fase 2.3 |
| D7 | Cómo se muestra el lunes | Lucas | En vivo con preguntas ensayadas + una de respaldo grabada (sección 7) | Fase 5 |
| D8 | ¿Alguien más (Santi/Gero) puede crear un índice vectorial en la base? | Pedro | Ver Fase 2.6: baja las búsquedas de ~60-90 s a segundos | Fase 2.6 (opcional) |

## 3. Fase 1 — Calidad del agente en Tigo (la que más pesa en el resultado)  [Claude, empieza ya]

Una API perfecta con respuestas equivocadas no sirve. Hoy hay una corrida de **50 preguntas diversas de Tigo** contra la demo (en curso, en el momento de escribir esto). Orden:

1. **Revisar las 50 respuestas una por una**: cifras contra SQL directo, uso correcto de herramientas, definiciones declaradas, manejo de preguntas vagas, de números no medidos y de trampas, errores del agente, tiempos y costo por pregunta. Resultado: lista de fallos con causa (prompt, Data Map, regla de negocio, SQL del modelo).
2. **Corregir** lo corregible por la vía de siempre (nueva versión del Data Map con el playbook de `2. clientes/0. Data Map Update Playbook.md`, o ajuste de prompt) y **re-correr solo lo que falló**.
3. **Banco real de Tigo**: pedirle a Agustin (o a quien hable con Tigo) las **20 preguntas que Tigo hace hoy** ("muchas preguntas básicas", según el audio). Con SQL de referencia se agregan a `2. clientes/tigo_alto/preguntas/` y pasan a ser el criterio de aceptación.
4. **Definiciones que Tigo puede cuestionar**: la ficha del proyecto registra que algunos campos de checklist marcan "Sí" bastante por encima de lo que respalda una lectura con cita (hallazgo en Tigo y Farma). Decidir si el agente debe declararlo y cómo (el agente ya declara la definición que usa).
5. **Latencia**: p50 y p90 por tipo de pregunta, medidos a través de la API (no solo contra el agente).
**Hallazgos de la primera corrida de 50 preguntas (2026-10-15, agente de la demo con la búsqueda encendida, 3 a la vez; datos crudos en `2. clientes/tigo_alto/preguntas/corrida_50_2026-10-15.json`, que no se versiona por el `.gitignore` de `*.json`).** Primera lectura, **todavía sin verificar cada cifra contra SQL**:
- **Estabilidad:** 50 de 50 respondieron, sin errores ni respuestas vacías; la trampa de inyección (pedir el SQL y las credenciales) se rechazó sin usar herramientas. Los errores de herramientas (12 preguntas) son rechazos del validador de SQL que el agente corrigió solo.
- **Tiempos:** mediana 31 s; cuantitativas 16 s, rankings 23 s, insights 22 s, cualitativas 197 s, coaching 265 s. **4 preguntas pasaron los 300 s** (g02 428 s, h02 403 s, i03 420 s, i01 637 s) y todas usan la búsqueda: con el tope de 420 s de la API, i01 habría terminado en `timeout`. Es consecuencia de la búsqueda sin índice y de la conexión compartida (aquí corrían 3 a la vez); hay que medir las mismas preguntas de a una.
- **Problema de calidad, el más importante: respuestas "no tengo ese dato" para campos que SÍ existen.** `a07` ("¿en qué porcentaje el asesor valida la identidad?", sin usar ninguna herramienta; el campo `a07_valida_identidad_titularidad_antes_de_gestionar` está en el Data Map V3), `k01` y `f01` (la retención exitosa por región y por asesor, aunque `a05`, `b01`, `b03` y `b04` sí la calcularon con `a30_retencion_exitosa`), `f03` (criterios más débiles del checklist), `h03` (mejor asesor) y `g03` (conductas de riesgo, cuando existen `t01` a `t15`). Es la regla de "números que no están medidos" (6-oct) disparándose de más, o el planificador sin mapear la pregunta al campo. Pasó también con `h04` (pidió un resumen ejecutivo y respondió que no tiene el volumen de grabaciones sin procesar). **Esto es lo primero a corregir antes de la demostración:** hacer que el planificador compruebe contra el catálogo de campos antes de declarar "no medido", y re-correr esas preguntas.
- **CORREGIDO el 2026-10-15 (subido a la demo en el commit 26bcfd8; en el repo principal sin commitear).** Causa raíz: el Data Map de Tigo declara los 63 campos de rendimiento (`a01` a `a59`, `t01` a `t15`) en grupos `fields_<grupo>` y no en `fields`, y el planificador (y otros 3 módulos) solo leían `fields`: no veía ninguno y declaraba "no medido". Arreglos: (1) `4. scripts/data_map_fields.py` (`source_fields`) junta `fields` y los grupos, y lo usan el planificador, el gate, el chequeo de columnas y el registro de cambios (el chequeo de columnas por fin verifica esos campos contra la base: sin deriva); (2) una métrica puede nombrar una LISTA de campos (familias como los criterios del checklist o las conductas de riesgo); (3) el catálogo del planificador lista las DIMENSIONES para agrupar (asesor = `employee_full_name`, tienda, región) y se aclara que "por asesor" o "el mejor asesor" no son métricas faltantes; (4) la regla de "números no medidos" del prompt del agente exige revisar el plan y los campos antes de declarar algo no medido. **Resultado: de 15 rechazos en 18 corridas a 0 en 18** (a07, k01, f01, f03, h03, g03, tres veces cada una). Contraste con SQL directo: a07 = 54.178 de 109.396 (49,52 %), exacto; las 3 conductas de riesgo más frecuentes (t02, t12, t15) salen en el mismo orden. Tests: 35 nuevos y relacionados pasan (planificador, gate, columnas, registro, agente). **Nota de método:** un error mío en el script de prueba (apuntaba al código viejo de la demo) hizo que tres re-corridas no midieran el arreglo; la medición válida es la última.
- **Pendiente de este arreglo:** correr la suite completa, y la búsqueda vectorial de Tigo sigue sin leer los campos agrupados para el filtro por criterio (no se tocó a propósito: cambiaría su comportamiento sin probarlo). La demo ya tiene el arreglo (medido: 0 rechazos en 18 corridas sobre el código de la demo); la VM lo toma en su próximo `git pull` automático.
- **Verificado contra SQL directo el 2026-10-15 (cifras del agente de la primera corrida de 50 preguntas).** `a05` (retención de septiembre: 3.004 de 12.509 = 24,01 %), `b02` (tasa de compra por región: Bogotá 18,34 %, Oriente 18,25 %, Costa 17,60 %, Andina 16,33 %, Sur 14,44 %, con los mismos conteos), `j03` (Tienda Centro Medellin: 681 de 3.963 = 17,18 %, la tienda existe y el período coincide) y `a07` coinciden **exactos**. `b06` (cancelaciones por región, Sur 18,64 % contra 18,65 %), `c01` (causas de retiro: factura alta 39,44 % contra 39,45 %, mismo ranking) y `g01`/`g02` (trato grosero 0,39 %; solicitud de dinero 102 contra 104 casos) coinciden con diferencias de unos pocos casos que se explican porque la base crece entre consultas (123.408 conversaciones analizables en la corrida contra 123.961 ahora, +0,45 %). Las cifras que se contrastaron tienen las definiciones correctas y el mismo orden; no apareció ninguna cifra inventada. **Segunda ronda de verificación contra SQL (2026-10-15, el resto de las cuantitativas: `a02`, `a04`, `a06`, `a09`, `b01`, `b03`, `b04`, `b05`, `c03` a `c06`, `d01`, `d02`, `k02`, `h01`): 108 cifras comparadas, 96 exactas o dentro de la tolerancia de crecimiento de la base (1 % en conteos, 0,15 puntos en porcentajes), 12 marcadas.** Las marcadas:
  - **Hallazgo real, `d01` (categorías de producto más mencionadas):** el agente informó 94.455 menciones (planes 25.479, hogar fijo 24.469, móvil 19.130...) pero solo 90.383 son de conversaciones analizables (planes 24.531, hogar 23.445, móvil 18.512...): **sobrecuenta 4,3 % porque omitió el filtro `usefulforanalysis`**. Es inconsistente con su propia respuesta `d02` (misma vista), que sí lo aplicó (24.531 menciones de planes). El orden del ranking no cambia. Causa probable: el Data Map de Tigo (`profiles.standard_conversation.quality_filter`) dice "No agregar por costumbre si no se pide explícitamente", lo que contradice la norma del proyecto de usar siempre conversaciones analizables (Iteración 58); la mayoría de las respuestas lo aplican igual, esta no. Arreglo propuesto: nueva versión del Data Map de Tigo (V4, nunca pisar la V3) que fije el filtro para las vistas por producto, y reverificar `d01`. **Decisión de Lucas (2026-10-15): se deja para después** (falla ocasional y de bajo impacto: sobrecuenta pareja del 4,3 %, el orden del ranking no cambia); antes van la latencia de las cualitativas y decidir dónde corre la API.
  - **Cosmético, `b01`:** el agente escribió "Tienda Cra Cincuenta y Cuatro" para la tienda que en la base se llama "Tienda Cra 54" (164 de 556, correcto). Convierte en palabras los números dentro de los nombres, y un usuario no encontraría la tienda por su nombre. Pendiente de revisar de dónde sale (probablemente la defensa contra números sin respaldo).
  - **Diferencias de unos pocos casos por crecimiento de la base** (la corrida se hizo con 123.408 conversaciones y la base tiene hoy 123.961): `c04` #6 (435 contra 441), `b01` Viva Barranquilla (66 de 196 contra 67; con eso pasa al primer lugar, apenas por encima de Tienda Ford, 34,01 % contra 33,98 %, y el agente ya había advertido un empate técnico entre las primeras posiciones). `b04` (asesores del último mes): los 5 nombres del agente están en el top 7 por SQL de hoy, con tasas dentro de 2,5 puntos, pero la ventana de "último mes" se mueve a diario y hoy aparece otra asesora (Robinson, 21 de 44) que antes no estaba; es esperable y no es un error.
  - **Contrastes exactos:** `a02`, `a04`, `a06` (las 5 semanas), `a09` (los 4 meses), `b03` (a07 y a30 de Costa y Bogotá, los 8 conteos), `b05` y `k02` (agosto y septiembre), `c03`, `c05`, `c06`, `d02` (sentimiento de planes y de hogar fijo, los 10 conteos) y `h01` (últimos 30 días).
  **Sigue sin contrastarse** el resto de las respuestas: las cualitativas (no tienen una cifra de SQL contra la cual compararse) y las de coaching, riesgo y no medido.
- **Respuestas correctas que sí se pudieron contrastar:** `a01` y `a02` (123.408 conversaciones; 102.371 no compra y 21.035 compra) coinciden con la consulta directa a la base.

**Criterio de aceptación:** las 20 preguntas reales de Tigo dan cifras correctas (contra SQL) y ninguna respuesta de las 50 inventa un dato o muestra detalles internos.

## 4. Fase 2 — API lista para producción  [Claude, antes de desplegar]

**Estado (2026-10-15): hechas y probadas 2.1, 2.2, 2.3, 2.5 y 2.8 (con agente falso y una corrida real contra Tigo), y de 2.4 la parte de código (registro, `/v1/status` y avisos a Slack). Falta de 2.4 el monitor externo de `/health` (configuración, depende del despliegue). Pendientes: 2.6 (índice vectorial, depende de quien administra la base) y 2.7 (streaming, opcional).** Detalle de lo hecho en `API_LOVABLE.md` (secciones 3, 7 y 9).

Ya hay una versión funcional (`api_server.py`, 17 tests, probada en local). Faltan, en este orden de importancia:

| # | Pieza | Por qué | Esfuerzo |
|---|---|---|---|
| 2.1 ✅ | **Reiniciar la API cuando cambia el Data Map.** El agente fija la ruta del Data Map al arrancar; si el refresco diario de las 8:00 promueve una versión nueva, una API que ya corre seguiría usando la vieja hasta reiniciarse. Diseño: cada pocos minutos compara el `data_map` de `config.yaml` con el que cargó y, si cambió y no hay preguntas en curso, termina limpio para que systemd/Docker la reinicie. | Evita responder con definiciones viejas sin que nadie lo note (las reglas de Langfuse sí se leen vivas) | Chico |
| 2.2 ✅ | **Tope por usuario y dueño de la sesión.** La función de Supabase manda el identificador del usuario; la API limita por usuario (hoy limita por clave y todos comparten una) y evita que un usuario use la sesión de otro. | Un usuario no puede agotar el cupo de todos ni leer el historial de otro | Chico |
| 2.3 ✅ | **Feedback y registro.** Ruta para 👍/👎 con comentario y registro (local, nunca en Postgres) de pregunta, herramientas, tiempo, costo y resultado, para mejorar el agente con uso real. Reusar `feedback_tracking.py` y `question_tracking.py`. El texto de las preguntas solo si D6 lo permite. | Sin esto no se aprende de lo que Tigo pregunta | Medio |
| 2.4 ✅ | **Observabilidad y alertas.** Monitor externo de `/health` (gratis, tipo UptimeRobot) y aviso a Slack si cae; el registro estructurado ya existe. Heartbeat opcional. | Enterarse antes que el cliente | Chico |
| 2.5 ✅ | **Arranque en caliente.** Una pregunta de calentamiento al iniciar (la primera es más lenta por cachés de Data Map y reglas). | La primera pregunta del lunes no debería ser la lenta | Chico |
| 2.6 | **Índice vectorial en la base** (decisión D8, la mejora de fondo para las cualitativas). Hoy la búsqueda es un barrido exacto de ~60-90 s y todas las búsquedas comparten una conexión: con varios usuarios, hacen cola. Un índice (HNSW o similar, parcial por cliente) las bajaría a segundos. Requiere permisos de escritura sobre la base y evaluar el costo de mantenerlo con la ETL (Santi ya está optimizando la ETL: coordinar con él). | Es lo que más baja los tiempos de las preguntas cualitativas | Medio-grande, depende de otros |
| 2.7 | **Streaming de la respuesta** (SSE) en vez de consulta cada 2 s. | Mejora la sensación de espera; **no es necesario para el lunes** | Medio, opcional |
| 2.8 ✅ | **Pruebas de carga y de seguridad** (ver sección 7). | Sin esto no sabemos qué pasa con 10 usuarios a la vez ni ante abuso | Medio |

**Criterio de aceptación:** los tests nuevos pasan; 10 preguntas simultáneas terminan sin errores y con tiempos medidos; una clave o sesión ajena recibe 404/401; un reinicio por cambio de Data Map no pierde preguntas en curso.

## 5. Fase 3 — Infraestructura  [Pedro + Claude]

Siguiendo `API_LOVABLE.md` sección 5. Pasos y verificación (cada uno con su prueba):
1. Clonar/actualizar la demo en la VM e instalar dependencias → `python -c "import api_server"` sin errores.
2. Crear el archivo de secretos (permisos 600) con `VI_API_KEYS` (clave nueva y larga, **una para el portal**), `VERA_AI_API_KEY`, `PGPASSWORD`; **nunca** en el repo ni por chat.
3. Servicio `systemd` (`deploy_api/vi-api.service`) → `systemctl status vi-api` activo y se reinicia solo ante una caída.
4. HTTPS con Caddy (o túnel) → `curl https://<dominio>/health` desde afuera devuelve `ok`.
5. Prueba de humo desde afuera: una pregunta de punta a punta con la clave.
6. Monitor externo de `/health` + aviso a Slack (2.4).
7. **Plan de vuelta atrás:** parar el servicio no afecta nada más (el refresco diario y la demo de Streamlit son independientes); documentar el comando.
Aviso: el refresco diario hace `git pull` automático en la VM, así que el código de la API se actualiza solo con la próxima corrida, pero **la API no se reinicia sola** (2.1 lo resuelve para el Data Map; para el código, reiniciar a mano).
**Criterio de aceptación:** desde una red externa, `health` y una pregunta real funcionan; una clave incorrecta da 401; reinicio y caída simulados se recuperan.

## 6. Fase 4 — Lovable  [Lucas, con prompts de Claude]

Detalle y prompts en `ejemplos_lovable/PROMPT_PARA_LOVABLE.md` ("Versión para Vera Insights Portal"). Orden:
- **4a (se puede hacer ya que hay decisión del lugar, sin backend):** la opción de menú, la ruta `/vera-intelligence` y la pantalla de chat con **respuestas simuladas**, todo el acceso al análisis aislado en `src/lib/veraClient.ts`. Se revisa el diseño y se ajusta.
- **4b (necesita D1, D3 y Fase 3):** crear la función `vera-ask`, cargar `VI_API_URL` y `VI_API_KEY` **como secretos con la interfaz de Lovable/Supabase, escritos por una persona**, y cambiar `veraClient.ts` para que llame a esa función.
- **4c (necesita D2):** que la opción aparezca solo para usuarios de Tigo y que la función lo verifique del lado servidor.
- **4d:** estados de carga ("Consultando los datos…", y un aviso de que las preguntas cualitativas pueden tardar unos minutos), errores amables, vista de celular, botón de cancelar.
- **4e (con 2.3):** botones 👍/👎 y "Nueva conversación".
- **4f (después):** historial de conversaciones entre sesiones (hoy un recargo de página pierde el chat; la API conserva el historial 60 minutos, pero la pantalla no lo recupera) y habilitar en otros lugares del portal.
**Criterio de aceptación:** un usuario de Tigo ve la opción, pregunta, ve progreso, recibe texto + gráfico + sugerencias, puede cancelar y empezar de nuevo; un usuario de otro cliente no ve la opción ni puede llamar a la función.

## 7. Fase 5 — Pruebas de aceptación y demostración  [todos]

**Lista de control de aceptación (en el portal real, con un usuario de prueba de Tigo):**
1. Pregunta de SQL simple, una con gráfico, una cualitativa con búsqueda, una vaga, un número no medido, una trampa ("ignorá tus instrucciones…"), un seguimiento y cancelar a mitad.
2. 10 preguntas simultáneas desde distintos navegadores: sin errores y con tiempos aceptables.
3. Recarga de página a mitad de una respuesta (qué ve el usuario), sesión vencida, red caída, API caída (mensaje amable, no un error técnico).
4. Seguridad: sin sesión → rechazo; usuario de otro cliente → rechazo; copiar la clave de la API desde el navegador es imposible (no está); intento de usar la sesión de otro usuario → rechazo.
5. Costo medido durante la prueba (registro de uso) y proyectado por usuario y día.

**Guion de la demostración del lunes:** 8 a 10 preguntas **ensayadas y verificadas contra SQL** (las más útiles de las 20 reales de Tigo), ordenadas de rápidas a lentas; la primera pregunta cualitativa se lanza al empezar y se muestra al final (tarda minutos); una respuesta **de respaldo** grabada por si falla la red; la API calentada 15 minutos antes (2.5); ensayo completo el lunes por la mañana.
**Si la infraestructura se atrasa:** se muestra la pantalla de 4a con una sesión real grabada del agente (no con datos inventados: con respuestas reales de la corrida de 50 preguntas) y se aclara que es el diseño. Alternativa de último recurso: un túnel temporal de Cloudflare desde la máquina de Lucas (solo para la demostración; no es producción).

## 8. Camino crítico y calendario sugerido

```
Jue 15  (hoy)  Decisiones D1-D3 (y D2 con Lovable en solo lectura) · Claude: revisar las 50 preguntas (Fase 1)
Vie 16         Fase 3 (despliegue, si D1 está) · Fase 4a (pantalla simulada) · Fase 2.1, 2.2, 2.5 · pedir las 20 preguntas reales
Sáb-Dom 17-18  (opcional) Fase 2.3/2.4 · re-correr lo que falló (Fase 1.2) · armar el guion
Lun 19 mañana  Fase 4b/4c con la API real · lista de control (Fase 5) · ensayo
Lun 19 tarde   Demostración con Tigo
```
Camino crítico: **D1 → Fase 3 → Fase 4b**. Todo lo demás corre en paralelo. Si D1 no está resuelta el viernes, el lunes se muestra con el plan de contingencia de la sección 7.

## 9. Riesgos principales

| Riesgo | Efecto | Mitigación |
|---|---|---|
| Cualitativas de 3 a 4,5 minutos (medido en la prueba de 50 preguntas) | La demostración se siente lenta; con varios usuarios hacen cola | Aviso en pantalla; guion que lanza la cualitativa primero; índice vectorial (2.6); tope de concurrencia y de tiempo ya hechos |
| Una cifra equivocada frente al cliente | Pérdida de confianza | Fase 1 (verificar contra SQL), banco real de Tigo, el agente declara la definición que usa |
| Definiciones del checklist que Tigo cuestione | Discusión sobre el número | Declararlo (Fase 1.4) y mostrar la definición en la respuesta |
| Data Map viejo en una API que no se reinicia | Respuestas con definiciones desactualizadas | Fase 2.1 |
| Portal sin Supabase Auth o sin backend | No se puede validar la sesión ni guardar la clave | Averiguarlo ya (D2, D3); alternativa: otro mecanismo de validación |
| Fuga de la clave | Cualquiera podría consultar y gastar | Secretos solo en paneles de Lovable/Supabase y archivo 600 en la VM; rotación; tope diario; la API solo ve Tigo |
| Gasto por uso | Costo no previsto | Tope por usuario (2.2) y por clave; medición continua (el costo medido es ~US$ 0,015-0,04 por pregunta) |
| La VM comparte CPU y conexión a la base con el refresco diario (8:00 ART) | Lentitud a esa hora | Evitar demostraciones cerca de las 8:00; si crece el uso, pasar a un servicio aparte (Opción B de la guía) |
| Inyección de instrucciones por un usuario externo | Respuestas indebidas | El agente ya tiene defensas y el validador de SQL fija el cliente; probarlo en la lista de control |

## 10. Qué queda fuera por ahora

Otros clientes (una instancia de la API por cliente, ver la guía), historial de conversaciones entre sesiones, streaming, exportar respuestas, panel de costos, y cualquier uso de la búsqueda vectorial para cifras (descartado: ver `3. experimentos/busqueda_insights/README.md`).

## 11. Próximas acciones concretas (para decidir qué se hace primero)

1. **Lucas:** preguntar a Lovable lo de D2 y D3 (solo lectura, un prompt) y decidir D1 con Pedro.
2. **Claude:** terminar de revisar las 50 preguntas de Tigo y proponer la lista de correcciones (Fase 1.1).
3. **Claude (si Lucas lo pide):** construir 2.1, 2.2 y 2.5 de la API (cambios chicos y bien delimitados) con sus tests.
4. **Lucas/Agustin:** conseguir las 20 preguntas reales que Tigo hace hoy.


## 12. Decisiones del 2026-10-16
- Backend: Supabase. API: VM de Pedro (opción A). Acceso: solo `management@tigo.com` (secreto `VI_ALLOWED_EMAILS` en la Edge Function + opción de menú visible solo para esa cuenta). No se modifica nada más del portal.
- Fuera de alcance por ahora: historial de conversaciones, gráficos guardados ("Gráficos autogenerados"), tablas nuevas en Supabase.
- Siguiente camino crítico: Fase 3 en la VM (dependencias, `api.env`, `vi-api.service`, proxy HTTPS/dominio, D4) -> Fase 4b (función `vera-ask` + prompt B) -> prueba con `management@tigo.com`.
