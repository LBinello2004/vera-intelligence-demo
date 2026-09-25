"""Acceso de solo lectura a criterios de negocio versionados en Langfuse."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from client_config import ClientConfig
from runtime_control import OperationalUnavailable, check_analysis


# Sesión HTTP reusada entre llamadas a Langfuse (2026-09-11) -mismo motivo y mismo patrón que
# _get_reusable_sql_connection()/_get_reusable_embed_client()/_get_reusable_genai_client() en
# vi_agent.py y vector_search.py: `requests.get()` suelto (el comportamiento anterior de _fetch)
# abre una conexión TCP/TLS nueva en CADA llamada, descartando el pool de la anterior. Medido en
# vivo contra Langfuse real (mismo método que los otros tres fixes): ~1.2-1.4s por conexión nueva
# vs. ~0.2s reusando una sesión ya abierta. Impacto mayor en `data_map_auto_update.py` (el poller
# de actualización automática) -`detect_rulebook_changes()` crea un `BusinessRulesRepository`
# nuevo por cliente y llama `get(key, refresh=True)` para cada uno de sus rulebooks en CADA
# corrida, así que cachear a nivel de módulo (no de instancia) es lo que realmente ayuda ahí,
# donde una instancia nueva por cliente igual se beneficia de la sesión ya abierta por el cliente
# anterior. Sin lock: el `PoolManager` de urllib3 detrás de un `requests.Session` está pensado
# para atender conexiones concurrentes -mismo criterio que `httpx.Client` en el fix del cliente de
# embeddings, a diferencia de una `psycopg.Connection` compartida.
_cached_http_session: "requests.Session | None" = None


def _get_reusable_http_session() -> "requests.Session":
    global _cached_http_session
    if _cached_http_session is None:
        _cached_http_session = requests.Session()
    return _cached_http_session


class BusinessRulesRepository:
    """Expone únicamente los conjuntos de reglas autorizados para un cliente."""

    def __init__(self, client: ClientConfig, project_root: Path) -> None:
        self.client = client
        self.cache_dir = project_root / ".runtime" / "business_rules" / client.client_id
        self._memory_cache: dict[str, dict] = {}

    def available_rulebooks(self) -> tuple[str, ...]:
        return tuple(sorted(self.client.business_rulebooks))

    def get(self, rulebook: str, *, refresh: bool = False) -> str:
        """Devuelve criterios vigentes; usa el último snapshot si falla la red.

        Rulebooks con source="local" (2026-09-11, ver client_config.BusinessRulebookConfig) nunca
        tocan Langfuse ni el cache en disco: se leen directo del archivo declarado en `path` en
        cada llamada -son archivos chicos versionados en git, releerlos no tiene costo real, y así
        un cambio al archivo se refleja sin reiniciar el proceso."""
        check_analysis()
        key = rulebook.strip().lower()
        if key not in self.client.business_rulebooks:
            allowed = ", ".join(self.available_rulebooks())
            raise ValueError(f"Área de criterios no autorizada. Valores permitidos: {allowed}.")

        rulebook_config = self.client.business_rulebooks[key]
        if rulebook_config.source == "local":
            try:
                criteria_text = rulebook_config.path.read_text(encoding="utf-8")
            except OSError:
                raise OperationalUnavailable(
                    "No pude consultar el criterio comercial necesario en este momento. Intentá nuevamente más tarde."
                ) from None
            return self._tool_payload(
                {
                    "business_scope": rulebook_config.business_scope,
                    "rules_version": "local",
                    "criteria_text": criteria_text,
                },
                source_status="local_file",
            )

        if not refresh and key in self._memory_cache:
            payload = self._memory_cache[key]
            return self._tool_payload(payload, source_status="memory_cache")

        try:
            payload = self._fetch(key)
            try:
                self._write_cache(key, payload)
            except OSError:
                # La respuesta vigente sigue siendo utilizable aunque el
                # entorno no permita persistir el snapshot local.
                pass
            source_status = "current"
        except (KeyError, RuntimeError, requests.RequestException, ValueError):
            payload = self._read_cache(key)
            if payload is None:
                raise OperationalUnavailable(
                    "No pude consultar el criterio comercial necesario en este momento. Intentá nuevamente más tarde."
                ) from None
            source_status = "last_known_good"

        self._memory_cache[key] = payload
        return self._tool_payload(payload, source_status=source_status)

    def refresh_all(self) -> dict[str, dict]:
        results: dict[str, dict] = {}
        for key in self.available_rulebooks():
            tool_payload = json.loads(self.get(key, refresh=True))
            results[key] = {
                "rules_version": tool_payload["rules_version"],
                "status": tool_payload["source_status"],
                "business_scope": tool_payload["business_scope"],
            }
        return results

    def _fetch(self, key: str) -> dict:
        check_analysis()
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY")
        base_url = os.getenv("LANGFUSE_BASE_URL")
        if not public_key or not secret_key or not base_url:
            raise RuntimeError("Faltan credenciales para actualizar los criterios de negocio.")

        rulebook = self.client.business_rulebooks[key]
        response = _get_reusable_http_session().get(
            f"{base_url.rstrip('/')}/api/public/v2/prompts/{quote(rulebook.name, safe='')}",
            params={"label": rulebook.label},
            auth=(public_key, secret_key),
            timeout=30,
        )
        response.raise_for_status()
        raw = response.json()
        content = raw.get("prompt")
        if isinstance(content, list):
            criteria_text = json.dumps(content, ensure_ascii=False, indent=2)
        elif isinstance(content, str) and content.strip():
            criteria_text = content
        else:
            raise ValueError("La fuente semántica no devolvió contenido válido.")

        return {
            "rulebook": key,
            "business_scope": rulebook.business_scope,
            "rules_version": raw.get("version"),
            "criteria_text": criteria_text,
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
        required = {"rulebook", "business_scope", "rules_version", "criteria_text", "fetched_at"}
        if not isinstance(payload, dict) or not required.issubset(payload):
            return None
        return payload

    @staticmethod
    def _tool_payload(payload: dict, *, source_status: str) -> str:
        # Los nombres físicos y credenciales no se entregan al modelo. Sólo ve el
        # criterio que necesita interpretar y una versión interna auditable.
        return json.dumps(
            {
                "business_scope": payload["business_scope"],
                "rules_version": payload["rules_version"],
                "criteria_text": payload["criteria_text"],
                "source_status": source_status,
            },
            ensure_ascii=False,
        )
