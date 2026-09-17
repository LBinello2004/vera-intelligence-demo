"""Conexión segura y de solo lectura al PostgreSQL QA de Vera."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv


PGHOST = "postgres.c38a8ooe0anc.us-east-2.rds.amazonaws.com"
PGPORT = 5432
PGDATABASE = "postgres"
PGUSER = "postgres"


def _load_repository_env() -> None:
    """Carga el .env raíz sin sobrescribir variables ya exportadas."""
    repository_root = Path(__file__).resolve().parents[1]
    load_dotenv(repository_root / ".env", override=False)


def postgres_connection_kwargs() -> dict[str, object]:
    """Devuelve parámetros seguros de conexión sin exponer la contraseña."""
    _load_repository_env()
    password = os.getenv("PGPASSWORD")
    if not password:
        raise RuntimeError(
            "Falta PGPASSWORD. Agregala al .env raíz; consultá .env.example."
        )

    return {
        "host": PGHOST,
        "port": PGPORT,
        "dbname": PGDATABASE,
        "user": PGUSER,
        "password": password,
        "sslmode": "require",
        "connect_timeout": 10,
        # statement_timeout subido de 60000 a 180000ms (2026-09-15, Vera Intelligence): medido en
        # vivo que search_conversations (sin índice ANN sobre analytics_v2.conversation_embeddings,
        # ver "6. busqueda_vectorial/README.md") cortaba con "canceling statement due to statement
        # timeout" el 62,5% de un banco de 16 preguntas reales -pero al repetir esas mismas
        # preguntas con un timeout más alto, TODAS trajeron resultados reales y correctos
        # (confirmado con un juez externo, 14/14), una tardó 148,8s. No eran búsquedas rotas, eran
        # búsquedas lentas cortadas antes de tiempo. 180s cubre el peor caso medido con margen.
        # Compartido por todo el proyecto (run_readonly_sql y search_conversations) -no se aisló
        # sólo para búsqueda vectorial porque ambas rutas usan esta misma función.
        "options": "-c default_transaction_read_only=on -c statement_timeout=180000",
        "application_name": "data-sci-vera-readonly",
    }


@contextmanager
def get_postgres_connection() -> Iterator[object]:
    """Abre una sesión QA de solo lectura y garantiza su cierre."""
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "Falta psycopg. Instalá las dependencias con "
            "python -m pip install -r requirements.txt."
        ) from exc

    connection = psycopg.connect(**postgres_connection_kwargs())
    try:
        yield connection
    finally:
        connection.close()
