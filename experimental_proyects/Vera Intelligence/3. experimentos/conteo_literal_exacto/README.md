# Experimento: conteo literal exacto de nombres/términos concretos

**Estado (2026-09-14): propuesta técnica evaluada con datos reales, NO implementada.** No hay tool,
no hay código productivo -sólo el diseño completo, una prueba real contra Postgres, y una guía de
prompt lista para usar el día que se construya. Movido acá desde
`6. busqueda_vectorial/README.md` (donde se investigó originalmente, Iteración 22) porque es una
propuesta de FEATURE en evaluación -mismo criterio que `coaching_playbook/` y `resumen_ejecutivo/`
en esta misma carpeta-, no parte de la narrativa de iteraciones sobre `search_conversations` en sí.

## Por qué existe

Surgió como pregunta de seguimiento después de varias rondas de intentar sacar conteos de
`search_conversations` (búsqueda semántica) sin éxito -ver Iteraciones 14-21 en
`6. busqueda_vectorial/README.md`, cuatro métodos distintos, mismo techo de ~15-20% de precisión
porque el problema es de fondo (nuances subjetivas no se prestan a conteo, sin importar el método).
El usuario preguntó: ¿y si la pregunta es concisa, tipo "¿cuántas veces se menciona el Banco
Macro?" -un NOMBRE LITERAL, no una nuance subjetiva a inferir? Ahí la respuesta es distinta: la
búsqueda semántica directamente NO es la herramienta correcta (mide significado, no coincidencia de
texto) -lo correcto es **búsqueda de texto literal** (ILIKE/regex), una tecnología completamente
distinta.

## Propuesta

Una tool NUEVA y separada -ni `run_readonly_sql` (restringido a `dashboard_v2`, nunca al texto
crudo) ni `search_conversations` (semántica, no literal)-, con una query fija y parametrizada, mismo
criterio de seguridad que ya usa `vector_search.py` para tocar `raw_v2.conversations_raw` con un
`WHERE` de tenant fijo (nunca SQL libre del modelo). Recibe una LISTA de términos/variaciones (ej.
`["banco macro", "macro"]`) -pensada para que sea el propio modelo el que genere las variaciones
razonables (mayúsculas, con/sin calificador, abreviaturas), igual que ya arma queries específicas
para `search_conversations`- y cuenta conversaciones que contienen CUALQUIERA de esos términos, con
límite de palabra (`\y...\y` en Postgres, no `\b`) para evitar falsos positivos de substring (ej.
que "macro" no matchee "macroeconómico").

## Prueba real (mens_fashion_alto, 2026-09-14)

Términos `["banco macro", "macro"]`, corpus completo sin filtro de tienda/fecha: **63
conversaciones con al menos un match**, límite de palabra funcionando correctamente -ningún falso
positivo de substring en los 5 casos leídos. Pero dos problemas reales, distintos a los de la
búsqueda semántica:

1. **"macro" solo es ambiguo en el mundo real, no por la técnica.** Los 5 primeros resultados leídos
   NO son sobre el banco -"otra sucursal en Macro", "de cachanilla o de macro, aquí en Tijuana", "¿De
   la macro?"- parece un mayorista/proveedor local llamado "Macro", sin relación con "Banco Macro".
   El match es literalmente correcto (la palabra está ahí), pero un término corto y genérico no
   alcanza para identificar sin ambigüedad de qué "macro" se habla -a diferencia de la búsqueda
   semántica (que falla por ser demasiado difusa), acá el texto matchea perfecto pero el TÉRMINO
   elegido es insuficientemente específico.
2. **170,4s sin índice de texto sobre `raw_v2.conversations_raw`** -peor que la latencia típica de la
   búsqueda vectorial sin índice ANN (mediana 4,1s, ver `6. busqueda_vectorial/README.md`).

### Costo real del índice de texto (investigado, nada creado)

`raw_v2.conversations_raw` NO es una tabla física, es una vista sobre `raw_v2.firestore_documents`
-una tabla PARTICIONADA (`relkind='p'`). La partición real detrás de la vista es
`firestore_documents_conversations_prod_7`: **8,3 GB, ~881.000 filas** (todas las conversaciones de
todos los clientes, mismo orden de magnitud que la tabla de embeddings, ~720k). Un índice `GIN` sobre
`to_tsvector('spanish', data->>'transcribedAudio')` en esa partición puntual (no hace falta tocar las
otras 13 particiones de `firestore_documents` -recordings, eventos de conexión, etc.-):

- **Costo en $**: marginal -espacio en disco (una fracción del tamaño del texto indexado, no de la
  tabla entera) y CPU una sola vez al construirlo. Sin infraestructura nueva.
- **Tiempo de build**: para ~881k filas, un `GIN` típico tarda entre 10 y 60 minutos -se puede armar
  con `CREATE INDEX CONCURRENTLY` para no bloquear escrituras mientras tanto.
- **Más simple que el índice vectorial pendiente**: `GIN`/`tsvector` da resultados EXACTOS, no
  aproximados -a diferencia de ivfflat/hnsw (que sí puede perder vecinos reales por diseño), acá no
  hay trade-off de precisión ni parámetros que tunear.
- Sigue siendo la tabla de la otra persona (mismo dueño que la tabla de embeddings) -el pedido en sí
  sigue siendo externo a este proyecto, aunque mucho más chico y simple que el del índice vectorial.

## Conclusión

La mecánica es sólida y SÍ daría un número exacto y confiable -a diferencia de todo lo intentado
para conteo semántico (Iteraciones 14-21), acá no hay ambigüedad de distancia, es coincidencia de
texto determinística. Pero no está lista para construirse tal cual: necesita (a) el índice de texto
de arriba para ser práctica en vivo, y (b) la guía de prompt de abajo, para preferir variaciones
específicas sobre términos cortos/ambiguos y mostrar siempre 1-2 fragmentos de ejemplo junto al
número (mismo criterio que `search_conversations` con `fragmento_aproximado`), para que quien lee el
número pueda detectar a simple vista un caso como el de "macro"/mayorista.

## Guía de prompt (borrador, mejorado 2026-09-14 a pedido explícito)

Texto listo para pegar en `SYSTEM_INSTRUCTION_TEMPLATE` (`4. scripts/vi_agent.py`) el día que la
tool se construya, mismo nivel de detalle que ya tiene `search_conversations`:

```
count_literal_mentions(terms, store_name opcional, date_from/date_to opcionales YYYY-MM-DD):
usala EXCLUSIVAMENTE para preguntas sobre cuántas veces se menciona un NOMBRE PROPIO o TÉRMINO
LITERAL concreto (una marca, un competidor, un banco, un producto por nombre específico) -nunca
para un estado subjetivo o una nuance a inferir (para eso existe search_conversations, que hace lo
opuesto: interpreta significado, no cuenta). Señal para elegir esta tool sobre las otras dos: la
pregunta nombra algo que se podría buscar con Ctrl+F en una transcripción y esperar encontrar la
palabra EXACTA, no un concepto parafraseable de mil formas distintas.

CÓMO ARMAR "terms": generá 2-4 variaciones razonables de cómo aparecería el término dicho en voz
alta y transcripto (mayúsculas/minúsculas no importan, se normaliza solo) -con y sin una palabra
calificadora si el nombre corto es ambiguo. Ejemplo para "Banco Macro": ["banco macro", "sucursal
macro"] está bien; agregar "macro" sola NO está bien, salvo que el nombre completo por sí solo ya
sea inequívoco en el rubro de este cliente. Un término corto y genérico (una palabra, sin
calificador) puede coincidir con otro uso completamente distinto de la misma palabra (ej. un
proveedor, un lugar, un apellido) y arruinar el conteo con ruido que no tiene nada que ver. Ante la
duda entre una variación más específica (menos casos, más confiable) y una más amplia (más casos,
más ruido), preferí SIEMPRE la más específica.

QUÉ HACER CON EL RESULTADO: la tool devuelve un conteo exacto de conversaciones que contienen
alguno de los términos, MÁS 2-3 fragmentos de ejemplo reales. Nunca entregues sólo el número pelado
-mirá los fragmentos de ejemplo antes de responder: si alguno claramente no tiene que ver con lo que
se preguntó (mismo problema que "macro" sin calificador, un homónimo), decilo explícitamente en tu
respuesta ("el número puede incluir menciones de otro uso del mismo término") en vez de presentar
el conteo como si fuera 100% preciso sin esa aclaración. Citá el contenido como evidencia de negocio,
nunca menciones "regex", "coincidencia de texto", "expresión regular" ni ningún término técnico -
mismo criterio de caja negra que toda respuesta final.

LÍMITES: esta tool cuenta CONVERSACIONES con al menos una mención, no menciones individuales -si la
pregunta es "cuántas veces en TOTAL se dijo X" (contando repeticiones dentro de la misma
conversación), aclará que el número que tenés es de conversaciones distintas, no de repeticiones. No
sirve para nombres/términos que se puedan decir de formas radicalmente distintas sin compartir
ninguna palabra en común (para eso no hay atajo -sería una nuance, no un término literal).
```

Puntos de diseño que resuelve, atados a los dos hallazgos reales del experimento: (1) "preferí
SIEMPRE la más específica" ataca directamente el caso "macro"/mayorista -nunca hubiera generado esa
variación sola con esta guía; (2) "nunca entregues sólo el número pelado... mirá los fragmentos"
ataca el mismo caso desde el otro lado, como red de seguridad si igual se cuela un término ambiguo.

## Qué falta para construirlo

1. Pedirle a quien administra `raw_v2.firestore_documents` el índice `GIN` descripto arriba (pedido
   chico y simple, no bloqueado por decisiones de diseño como el índice vectorial).
2. Escribir `count_literal_mentions` en `4. scripts/vi_agent.py` (mismo patrón de tool fija y
   parametrizada que `search_conversations` en `vector_search.py`) + tests.
3. Agregar la guía de arriba a `SYSTEM_INSTRUCTION_TEMPLATE`, condicional a que el cliente la tenga
   habilitada (mismo mecanismo que `vector_search`/`rag_sources` en `config.yaml`).
4. Repetir la prueba real de este experimento CON el índice para confirmar el tiempo de respuesta
   antes de promoverlo a producción.

Script del experimento original (no productivo, no versionado) descartado al cerrar la sesión en la
que se investigó.
