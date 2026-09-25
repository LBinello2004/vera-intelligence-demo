"""Acceso de solo lectura al nombre físico de stores de RAG (Gemini File Search).

Mismo patrón que business_rules.py: el nombre físico del store nunca se declara
en config.yaml, se resuelve a partir de config.geminiFileSearchStoreName del
prompt de Langfuse marcado production y se cachea en disco para poder seguir
funcionando ante una falla de red.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from business_rules import _get_reusable_http_session
from client_config import ClientConfig
from runtime_control import OperationalUnavailable, check_analysis


class RagSourceRepository:
    """Expone únicamente los stores de RAG autorizados para un cliente."""

    def __init__(self, client: ClientConfig, project_root: Path) -> None:
        self.client = client
        self.cache_dir = project_root / ".runtime" / "rag_sources" / client.client_id
        self._memory_cache: dict[str, dict] = {}

    def available_sources(self) -> tuple[str, ...]:
        return tuple(sorted(self.client.rag_sources))

    def get_store_name(self, source: str, *, refresh: bool = False) -> str:
        """Devuelve el nombre físico vigente del store; usa el último snapshot si falla la red."""
        return self._get(source, refresh=refresh)["store_name"]

    def get_top_k(self, source: str) -> int:
        key = source.strip().lower()
        if key not in self.client.rag_sources:
            allowed = ", ".join(self.available_sources())
            raise ValueError(f"Fuente de RAG no autorizada. Valores permitidos: {allowed}.")
        return self.client.rag_sources[key].top_k

    def _get(self, source: str, *, refresh: bool = False) -> dict:
        check_analysis()
        key = source.strip().lower()
        if key not in self.client.rag_sources:
            allowed = ", ".join(self.available_sources())
            raise ValueError(f"Fuente de RAG no autorizada. Valores permitidos: {allowed}.")

        if not refresh and key in self._memory_cache:
            return self._memory_cache[key]

        try:
            payload = self._fetch(key)
            try:
                self._write_cache(key, payload)
            except OSError:
                # El store vigente sigue siendo utilizable aunque el entorno
                # no permita persistir el snapshot local.
                pass
        except (KeyError, RuntimeError, requests.RequestException, ValueError):
            payload = self._read_cache(key)
            if payload is None:
                raise OperationalUnavailable(
                    "No pude acceder a la información de referencia necesaria en este momento. Intentá nuevamente más tarde."
                ) from None

        self._memory_cache[key] = payload
        return payload

    def _fetch(self, key: str) -> dict:
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY")
        base_url = os.getenv("LANGFUSE_BASE_URL")
        if not public_key or not secret_key or not base_url:
            raise RuntimeError("Faltan credenciales para actualizar la fuente de RAG.")

        rag_source = self.client.rag_sources[key]
        # Sesión HTTP reusada, compartida con business_rules.py -mismo host de Langfuse, así que
        # comparten el mismo pool de conexiones en vez de cada módulo abriendo el suyo (ver el
        # comentario junto a _get_reusable_http_session en business_rules.py para el motivo y la
        # medición en vivo).
        response = _get_reusable_http_session().get(
            f"{base_url.rstrip('/')}/api/public/v2/prompts/{quote(rag_source.name, safe='')}",
            params={"label": rag_source.label},
            auth=(public_key, secret_key),
            timeout=30,
        )
        response.raise_for_status()
        raw = response.json()
        config = raw.get("config")
        store_name = None
        if isinstance(config, dict):
            store_name = config.get("geminiFileSearchStoreName")
        if not isinstance(store_name, str) or not store_name.strip():
            raise ValueError(
                "El prompt de Langfuse no declara un geminiFileSearchStoreName válido."
            )

        return {
            "source": key,
            "store_name": store_name.strip(),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _write_cache(self, key: str, payload: dict) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self._cache_path(key)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.chmod(0o600)
        temporary.replace(target)

    def _read_cache(self, key: str) -> dict | None:
        path = self._cache_path(key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        required = {"source", "store_name", "fetched_at"}
        if not isinstance(payload, dict) or not required.issubset(payload):
            return None
        return payload
