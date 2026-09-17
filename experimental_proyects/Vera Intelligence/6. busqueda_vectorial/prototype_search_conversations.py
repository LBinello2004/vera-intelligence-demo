# -*- coding: utf-8 -*-
"""Prototipo de search_conversations — probar acá antes de tocar 4. scripts/.

SUPERADO (2026-09-10): validado acá y ya conectado en producción como
"4. scripts/vector_search.py" + tool en vi_agent.py, piloto mens_fashion_alto. Este archivo queda
como registro histórico del prototipo -mismo criterio que el proyecto usa para Data Maps, nunca se
borra- pero para tocar el comportamiento real, editar el módulo de 4. scripts/, no este archivo.

Sigue el mismo criterio que 3. experimentos/coaching_playbook/coaching_tester.py: no modifica
ningún código fuente de 4. scripts/, sólo lo importa y prueba una función nueva en memoria.

Ver "6. busqueda_vectorial/README.md" para el diseño completo y las decisiones pendientes -en
particular, la reconstrucción del texto del chunk acá es una aproximación, no el algoritmo real
de chunking (todavía sin confirmar con la persona que vectoriza).

Uso:
    python "prototype_search_conversations.py" "conversaciones donde el cliente se quejo del envio"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
REPO_ROOT = PROJECT_ROOT.parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "4. scripts"
for path in (REPO_ROOT, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from utils.postgres import get_postgres_connection  # noqa: E402

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSION = 768
# Verificado por SQL directo el 2026-09-10 contra analytics_v2.conversation_embeddings -es el
# único valor presente en toda la tabla hoy. Si la otra persona re-vectoriza con otro config,
# esta constante queda desactualizada en silencio (la query no rompe, sólo deja de encontrar
# filas si el filtro es exacto, o compara embeddings de configs distintas si se saca el filtro).
EMBEDDING_CONFIG_ID = (
    "gemini-developer-api:gemini-embedding-001:768:retrieval-document:srt-v1:"
    "short-whole-long-fixed-overlap:tokens-per-word-1.3:max-1500:window-1153:overlap-153:"
    "step-1000:l2-v1"
)

# Aproximación de la ventana de chunking en PALABRAS a partir del ratio tokens-per-word=1.3
# declarado en el config_id. NO es el algoritmo real: se infiere sólo de los nombres de los
# parámetros -el criterio exacto de tokenización (por turno del .srt, por tiempo, por token
# crudo) no está confirmado por la persona que vectoriza. Tratar el texto reconstruido como una
# aproximación razonable para citar, no como el fragmento exacto que generó el vector.
_TOKENS_PER_WORD = 1.3
_WINDOW_WORDS = round(1153 / _TOKENS_PER_WORD)
_STEP_WORDS = round(1000 / _TOKENS_PER_WORD)


def reconstruct_chunk_text(transcript_srt: str, chunk_idx: int) -> str:
    words = transcript_srt.split()
    start = chunk_idx * _STEP_WORDS
    end = start + _WINDOW_WORDS
    return " ".join(words[start:end])


def embed_query(query: str, api_key: str) -> list[float]:
    client = genai.Client(api_key=api_key)
    response = client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=query,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=EMBEDDING_DIMENSION,
        ),
    )
    return list(response.embeddings[0].values)


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(v) for v in vector) + "]"


def search_conversations(query: str, tenant: str, top_k: int = 5) -> list[dict]:
    api_key = os.environ["VERA_AI_API_KEY"]
    query_vector = embed_query(query, api_key)
    literal = vector_literal(query_vector)

    # El tipo `vector` y el operador `<=>` de pgvector viven en el schema analytics_v2 (no en
    # public/search_path default) -confirmado por SQL directo: sin agregar analytics_v2 al
    # search_path de la sesión (ver search_conversations arriba), psycopg tira
    # `UndefinedObject: type "vector" does not exist` y luego `UndefinedFunction` para `<=>`.
    sql = """
        SELECT ce.recording_id, ce.chunk_idx,
               r.store_name, r.employee_full_name, r.started_at,
               cr.data->>'transcribedAudio' AS transcript,
               ce.embedding <=> %s::vector AS distancia
        FROM analytics_v2.conversation_embeddings ce
        JOIN mart_v2.recordings_enriched r ON r.recording_id = ce.recording_id
        JOIN raw_v2.conversations_raw cr ON cr.recording_id = ce.recording_id
        WHERE r.seller_id = %s
          AND ce.embedding_config_id = %s
        ORDER BY ce.embedding <=> %s::vector
        LIMIT %s
    """
    params = (literal, tenant, EMBEDDING_CONFIG_ID, literal, top_k)
    with get_postgres_connection() as connection:
        with connection.cursor() as cursor:
            # analytics_v2 no está en el search_path default -sin esto, ni el tipo `vector` ni
            # el operador `<=>` resuelven (UndefinedObject / UndefinedFunction respectivamente).
            cursor.execute("SET search_path = analytics_v2, public")
            cursor.execute(sql, params)
            rows = cursor.fetchall()

    results = []
    for recording_id, chunk_idx, store_name, employee, started_at, transcript, distancia in rows:
        text = reconstruct_chunk_text(transcript, chunk_idx) if transcript else None
        results.append(
            {
                "recording_id": recording_id,  # sólo para debug del prototipo
                "tienda": store_name,
                "vendedor": employee,
                "fecha": started_at.isoformat() if started_at else None,
                "distancia": float(distancia),
                "fragmento_aproximado": text,
            }
        )
    return results


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    query = sys.argv[1] if len(sys.argv) > 1 else "conversaciones donde el cliente se quejo del envio"
    tenant = sys.argv[2] if len(sys.argv) > 2 else "Mens Fashion"
    results = search_conversations(query, tenant, top_k=5)
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
