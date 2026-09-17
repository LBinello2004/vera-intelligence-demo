# Experimento: coaching playbook (business_rulebook nuevo)

**Estado (2026-09-11): activo en los 19 clientes, con uso PROACTIVO y contenido ampliado (4 partes +
modo equipo + priorización), todavía SIN revisión de negocio.** Contenido actual en
`2. clientes/_shared/business_rulebooks/coaching_playbook.md` -formato de 4 partes (diagnóstico, acción,
encuadre, seguimiento), priorización a máximo 2 focos cuando un vendedor falla varios criterios, un modo
separado para preguntas de equipo/agregado, aviso de volumen bajo, y una lista de qué NO recomendar
(sanciones, guiones inventados, programas formales). Verificado en vivo con un caso real de múltiples
criterios débiles, y también el modo equipo, tras reforzar el trigger genérico de `get_business_rules`
en `vi_agent.py` (agregó señales explícitas de "en qué enfocar/priorizar capacitación" y una regla de
distinción diagnóstico-vs-acción) -la misma pregunta que antes no disparaba el playbook
("¿en qué deberíamos enfocar la próxima capacitación del equipo?") ahora sí lo hace y responde siguiendo
el modo equipo (2 criterios priorizados, una acción grupal concreta, seguimiento). Ver las entradas del
2026-09-11 en `9. HISTORIAL.md` para el detalle completo. Empezó como piloto en 2 clientes (mens_fashion_alto, farma24_alto) y se
generalizó a los 17 restantes el mismo día, a pedido explícito del usuario tras ver un caso real donde
el agente daba recomendaciones de coaching improvisadas en vez de usar este rulebook. También se agregó
uso proactivo: el agente ahora consulta `coaching_playbook` cuando la pregunta lo amerita aunque el
usuario nunca pida "una recomendación" ni nombre la herramienta -antes dependía de que el modelo lo
decidiera por su cuenta de forma pasiva. Ver la entrada del 2026-09-11 en `9. HISTORIAL.md` para el
detalle completo, incluida la verificación en vivo con el caso real que motivó el cambio.

Respecto al plan original de esta carpeta, hubo un cambio de mecanismo: el contenido NO se publicó en
Langfuse (el usuario pidió explícitamente que quedara todo interno en el repo). Se agregó un segundo tipo de
`source` a `BusinessRulebookConfig` (`4. scripts/client_config.py`), además de `langfuse`: `source: local` + `path`, que lee
el contenido directo de un archivo del repo
(`2. clientes/_shared/business_rulebooks/coaching_playbook.md`) en vez de resolverlo contra Langfuse
-`business_rules.py` rama a esa lectura local sin llamar a `requests.get` en absoluto. El mecanismo de
`_build_rulebook_options` (generalizar la guía de "cuándo usar cada rulebook" en el prompt, antes
hardcodeada) SÍ se aplicó tal como estaba diseñado en `parche_vi_agent_referencia.diff` -adaptado a la
estructura actual de `vi_agent.py` (`extra_tools_section`, no `rag_tools_section`, que ya no existe).
**Verificado en vivo** (mens_fashion_alto, pregunta real sobre venta cruzada): el modelo llamó a
`get_business_rules("coaching_playbook")`, recibió el contenido local, y devolvió una recomendación de
coaching en las 3 partes esperadas sin mencionar la fuente. 147/147 tests.

**Actualizado (2026-09-14)**: agregada una sección de "coaching para alto desempeño" (el playbook
antes sólo cubría bajo desempeño -una pregunta como "¿qué le recomendarías a mi mejor vendedor?" no
tenía guía y el modelo hubiera improvisado, exactamente el problema que este playbook existe para
evitar). `business_scope` corregido en los 19 `config.yaml` (decía "vendedores con bajo desempeño",
ahora "según su desempeño (bajo o alto)") para que el trigger proactivo del rulebook no quede sesgado
hacia sólo un caso. Verificado en vivo -ver "Cómo probar el mecanismo REAL hoy" abajo.

**Sigue pendiente igual que antes**: el contenido de `coaching_playbook.md` sigue siendo un borrador
técnico, no revisado por el equipo de negocio -ver "Pendiente real" abajo. Ya generalizado a los 19
clientes (no sólo los 2 del piloto original) -eso ya no está pendiente.

Si estás leyendo esto sin contexto previo del proyecto: primero leé `8. README.md` en la raíz (qué es
`business_rulebooks`, cómo funciona `4. scripts/business_rules.py`) — este documento asume que ya lo
leíste.

## Qué es (y qué es HOY, no el plan original)

Vera Intelligence tiene tres "business rulebooks" por cliente: `sales_evaluation`,
`conversation_insights` (ambos prompts de Langfuse, label `production`) y `coaching_playbook` -el que
nació en esta carpeta-, resueltos en tiempo de ejecución por `4. scripts/business_rules.py` para
ampliar la definición de un criterio cuando el Data Map no alcanza. `coaching_playbook` le da al
agente una guía explícita de cómo formular recomendaciones de coaching (a un vendedor de bajo
desempeño, a uno de alto desempeño, o al equipo en agregado), en vez de tener que improvisarlo sin
ninguna guía de negocio. Ya estaba anotado como idea pendiente desde el onboarding original de Mens
Fashion.

**DEPRECADO -esta carpeta describía originalmente (2026-09-08) un prototipo bloqueado en contenido de
Langfuse. ESO YA NO ES CIERTO.** El mecanismo real que sirve `coaching_playbook` en producción hoy
vive en `2. clientes/_shared/business_rulebooks/coaching_playbook.md` (contenido) +
`source: local` en `BusinessRulebookConfig` (`4. scripts/client_config.py`) -nunca pasa por
Langfuse, a pedido explícito del usuario. Todo lo de abajo (`draft_prompt_coaching_playbook.md`,
`parche_vi_agent_referencia.diff`, `coaching_tester.py`, `_smoke_test.py`) queda como **archivo
histórico** de cómo se probó la idea antes de que existiera el mecanismo real -no son el código que
corre hoy, no se mantienen activamente, y algunos ya no reflejan el `vi_agent.py`/`client_config.py`
actuales. Se conservan por el mismo criterio que el resto del proyecto conserva versiones de Data
Map: historial de auditoría, no descartable.

## Qué hay en esta carpeta (histórico)

- **`draft_prompt_coaching_playbook.md`** — primer borrador (formato de 3 partes), pensado
  originalmente para Langfuse. Superado por el contenido real en
  `2. clientes/_shared/business_rulebooks/coaching_playbook.md` (formato de 4 partes + modo equipo +
  coaching para alto desempeño + priorización + qué NO recomendar, iterado varias veces desde
  entonces, ver `9. HISTORIAL.md`) -no lo uses como referencia de contenido vigente.
- **`parche_vi_agent_referencia.diff`** — diff de referencia que se armó, se probó y se revirtió en su
  momento, para hacer genérica la guía de "cuándo usar cada rulebook". La idea que probaba (no el
  diff en sí, que puede ya no aplicar limpio) se implementó de verdad después como
  `_build_rulebook_options()` en `4. scripts/vi_agent.py`.
- **`coaching_tester.py`** / **`_smoke_test.py`** — testers que interceptaban `get_business_rules` en
  memoria para simular el contenido local ANTES de que `source: local` existiera de verdad en
  `client_config.py`. Ya no hace falta ese truco -`1. vi_agent_tester.py --client <cualquiera>` sirve
  `coaching_playbook` real hoy, para los 19 clientes. Estos scripts no se actualizan más.
- **`preguntas_sugeridas.md`** — 8 preguntas para probar coaching, siguen siendo un buen punto de
  partida para probar el mecanismo real con `1. vi_agent_tester.py` (no hace falta el tester propio de
  esta carpeta para usarlas).

## Cómo probar el mecanismo REAL hoy

Desde la raíz del repositorio, con el entorno Python disponible:

```powershell
.venv\Scripts\python.exe "experimental_proyects/Vera Intelligence/1. vi_agent_tester.py" --client mens_fashion_alto
```

Cualquier pregunta de coaching (ver `preguntas_sugeridas.md` para ideas) dispara `coaching_playbook`
de forma proactiva -no hace falta pedirlo explícitamente ni nombrar la herramienta, ver la guía de
`get_business_rules` en `SYSTEM_INSTRUCTION_TEMPLATE`. Verificado en vivo (2026-09-14): "¿qué le
recomendarías a mi mejor vendedor para seguir desarrollándose?" disparó correctamente el modo de alto
desempeño agregado ese mismo día.

## Riesgo a tener en cuenta (sigue vigente)

Cualquier recomendación de coaching que use un criterio con `reliability_warning` conocido (ver la
tabla de "Estado actual" en `8. README.md` para la lista por cliente) tiene que reflejar esa
incertidumbre, no tratar el número crudo como si fuera exacto -mismo criterio que ya exige el resto
del Data Map. Con 19 clientes activos, vale revisar qué clientes tienen hallazgos abiertos similares
antes de asumir que el playbook produce siempre una recomendación limpia.

## Pendiente real

El contenido de `coaching_playbook.md` sigue siendo un borrador técnico, **sin revisión del equipo de
negocio** -la razón de fondo no cambió desde el diseño original, sólo el mecanismo de entrega
(local, no Langfuse). Revisarlo con negocio queda para cuando se pida explícitamente.

## Potencial de negocio (logrado, no sólo potencial)

Convierte a Vera Intelligence de "qué pasó" a "qué hacer al respecto" -activo hoy en los 19 clientes,
con uso proactivo. Lo único que sigue pendiente es la revisión de negocio del contenido (ver
"Pendiente real" arriba), no trabajo de ingeniería.

Última actualización del README: 2026-09-14
