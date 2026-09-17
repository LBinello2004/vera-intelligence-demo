"""Configuración validada por cliente para Vera Intelligence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CLIENTS_ROOT = PROJECT_ROOT / "2. clientes"
DEFAULT_CLIENT_ID = "mens_fashion_alto"


def available_client_ids() -> tuple[str, ...]:
    """Lista los client_id con config.yaml presente bajo clientes/."""
    if not CLIENTS_ROOT.is_dir():
        return ()
    return tuple(
        sorted(
            entry.name
            for entry in CLIENTS_ROOT.iterdir()
            if entry.is_dir() and (entry / "config.yaml").is_file()
        )
    )


def available_clients_with_display_names() -> dict[str, str]:
    """client_id -> display_name para cada cliente con config.yaml válido bajo clientes/.

    El sufijo de grado (_alto/_medio/_bajo) en el nombre de carpeta es un juicio interno curado
    a mano para el equipo (ver "8. README.md" > "Estado actual") -nunca debería mostrarse en una
    interfaz donde alguien elige cliente; usar `display_name` para eso, `client_id` (nombre de
    carpeta) sólo internamente. Un cliente con config.yaml roto se omite en silencio en vez de
    tirar abajo el listado de todos los demás -usado por menús interactivos, no debe ser frágil-.
    """
    names: dict[str, str] = {}
    for client_id in available_client_ids():
        try:
            names[client_id] = load_client_config(client_id).display_name
        except Exception:  # noqa: BLE001
            continue
    return names


def client_config_path(client_id: str) -> Path:
    """Resuelve clientes/<client_id>/config.yaml, sin permitir salir de clientes/."""
    normalized = client_id.strip().lower()
    if not normalized or "/" in normalized or "\\" in normalized or ".." in normalized:
        raise ValueError(f"client_id inválido: {client_id!r}")
    path = (CLIENTS_ROOT / normalized / "config.yaml").resolve()
    if not path.is_relative_to(CLIENTS_ROOT.resolve()):
        raise ValueError(f"client_id inválido: {client_id!r}")
    if not path.is_file():
        disponibles = ", ".join(available_client_ids()) or "(ninguno)"
        raise ValueError(
            f"No existe configuración para el cliente {client_id!r}. "
            f"Clientes disponibles: {disponibles}."
        )
    return path


@dataclass(frozen=True)
class SourceConfig:
    name: str
    tenant_field: str


@dataclass(frozen=True)
class BusinessRulebookConfig:
    """Un rulebook autorizado para get_business_rules. `source` decide de dónde sale el
    contenido -"langfuse" (default, comportamiento histórico: `name`+`label`, label siempre
    'production') o "local" (2026-09-11, ver "3. experimentos/coaching_playbook/README.md":
    contenido versionado en este mismo repo, sin pasar por Langfuse -pensado para reglas de
    negocio que el equipo de negocio todavía no formalizó como prompt de producción). Exactamente
    uno de (`name`+`label`) o `path` debe estar poblado según `source` -validado en
    load_client_config, no acá (el dataclass no puede expresar esa dependencia condicional)."""

    key: str
    business_scope: str
    source: str = "langfuse"
    name: str | None = None
    label: str | None = None
    path: Path | None = None


@dataclass(frozen=True)
class VectorSearchConfig:
    """Habilita search_conversations (ver 6. busqueda_vectorial/README.md) para un cliente.

    A diferencia de sources/rag_sources, no declara nombres físicos de tabla ni tenant_field: la
    tabla (analytics_v2.conversation_embeddings) y el criterio de aislamiento (seller_id) son
    fijos, compartidos por todos los clientes -viven en vector_search.py, no acá- porque esta
    tool nunca deja que el modelo escriba SQL (a diferencia de run_readonly_sql). top_k y, quien
    lo necesite, store_names varían por cliente.

    ``store_names`` (2026-09-16, habilitación de Huerpel): allowlist OBLIGATORIA de valores
    físicos de `store_name` a la que se restringe la búsqueda, además del filtro de tenant. Sólo
    hace falta cuando un mismo tenant de Postgres (`seller_id`) está compartido por más de un
    cliente lógico de este proyecto -hoy únicamente Huerpel, cuyas tres sub-marcas
    (huerpel_hostess_alto, huerpel_hostess_seminuevos_medio, huerpel_ventas_alto) son un solo
    `seller_id='Huerpel'` en analytics_v2.conversation_embeddings, pero deben verse aisladas entre
    sí igual que si fueran tenants distintos -sin este filtro, search_conversations() de una
    sub-marca devolvería conversaciones de las otras dos-. `None` (default) para el resto de los
    clientes, donde el tenant ya identifica un único cliente lógico y esto no hace falta.
    """

    top_k: int
    store_names: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RagSourceConfig:
    """Fuente de RAG (Gemini File Search) resuelta desde un prompt de Langfuse.

    El nombre físico del store (config.geminiFileSearchStoreName) NO vive acá:
    se resuelve en tiempo de ejecución vía RagSourceRepository, igual que
    business_rules.py resuelve el texto de un rulebook. Así, si el store
    cambia en Langfuse, no hace falta tocar este YAML.
    """

    key: str
    name: str
    label: str
    business_scope: str
    top_k: int


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    display_name: str
    tenant: str
    model: str
    data_map_path: Path
    sources: dict[str, SourceConfig]
    business_rulebooks: dict[str, BusinessRulebookConfig]
    rag_sources: dict[str, RagSourceConfig]
    vector_search: VectorSearchConfig | None


def _required_text(payload: dict[str, Any], key: str, *, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}: falta un valor de texto valido para {key!r}.")
    return value.strip()


def load_client_config(client_id: str = DEFAULT_CLIENT_ID) -> ClientConfig:
    """Carga y valida la configuración de clientes/<client_id>/config.yaml."""
    resolved_path = client_config_path(client_id)
    payload = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Configuración inválida: {resolved_path}")

    data_map_relative = Path(_required_text(payload, "data_map", context="cliente"))
    data_map_path = (PROJECT_ROOT / data_map_relative).resolve()
    if not data_map_path.is_relative_to(PROJECT_ROOT):
        raise ValueError("El Data Map debe vivir dentro del proyecto Vera Intelligence.")
    if not data_map_path.is_file():
        raise ValueError(f"No existe el Data Map configurado: {data_map_path}")

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, dict) or not raw_sources:
        raise ValueError("La configuración debe declarar al menos una fuente autorizada.")
    sources: dict[str, SourceConfig] = {}
    for source_name, source_payload in raw_sources.items():
        if not isinstance(source_name, str) or not isinstance(source_payload, dict):
            raise ValueError("Cada fuente autorizada debe ser un objeto con nombre válido.")
        normalized_name = source_name.strip().lower()
        if not normalized_name.startswith("dashboard_v2."):
            raise ValueError(f"Fuente fuera del esquema autorizado: {source_name}")
        sources[normalized_name] = SourceConfig(
            name=normalized_name,
            tenant_field=_required_text(
                source_payload, "tenant_field", context=f"fuente {source_name}"
            ).lower(),
        )

    raw_rulebooks = payload.get("business_rulebooks")
    if not isinstance(raw_rulebooks, dict) or not raw_rulebooks:
        raise ValueError("La configuración debe declarar reglas de negocio autorizadas.")
    business_rulebooks: dict[str, BusinessRulebookConfig] = {}
    for key, rulebook_payload in raw_rulebooks.items():
        if not isinstance(key, str) or not isinstance(rulebook_payload, dict):
            raise ValueError("Cada conjunto de reglas debe ser un objeto con clave válida.")
        normalized_key = key.strip().lower()
        business_scope = _required_text(
            rulebook_payload, "business_scope", context=f"reglas {key}"
        )
        source = str(rulebook_payload.get("source") or "langfuse").strip().lower()
        if source == "local":
            # Contenido versionado en el repo, nunca en Langfuse -ver docstring de
            # BusinessRulebookConfig. `path` es relativo a PROJECT_ROOT, mismo criterio que
            # `data_map` más arriba: no puede salir del proyecto, y tiene que existir ya.
            path_relative = Path(_required_text(rulebook_payload, "path", context=f"reglas {key}"))
            rulebook_path = (PROJECT_ROOT / path_relative).resolve()
            if not rulebook_path.is_relative_to(PROJECT_ROOT):
                raise ValueError(f"Las reglas {key}: el path debe vivir dentro del proyecto Vera Intelligence.")
            if not rulebook_path.is_file():
                raise ValueError(f"Las reglas {key}: no existe el archivo local declarado: {rulebook_path}")
            business_rulebooks[normalized_key] = BusinessRulebookConfig(
                key=normalized_key,
                business_scope=business_scope,
                source="local",
                path=rulebook_path,
            )
        elif source == "langfuse":
            label = _required_text(rulebook_payload, "label", context=f"reglas {key}")
            if label != "production":
                raise ValueError(f"Las reglas {key} deben usar exclusivamente el label production.")
            business_rulebooks[normalized_key] = BusinessRulebookConfig(
                key=normalized_key,
                business_scope=business_scope,
                source="langfuse",
                name=_required_text(rulebook_payload, "name", context=f"reglas {key}"),
                label=label,
            )
        else:
            raise ValueError(f"Las reglas {key}: source debe ser 'langfuse' o 'local', no {source!r}.")

    raw_rag_sources = payload.get("rag_sources") or {}
    if not isinstance(raw_rag_sources, dict):
        raise ValueError("rag_sources, si se declara, debe ser un objeto.")
    rag_sources: dict[str, RagSourceConfig] = {}
    for key, rag_payload in raw_rag_sources.items():
        if not isinstance(key, str) or not isinstance(rag_payload, dict):
            raise ValueError("Cada fuente de RAG debe ser un objeto con clave válida.")
        normalized_key = key.strip().lower()
        label = _required_text(rag_payload, "label", context=f"rag {key}")
        if label != "production":
            raise ValueError(f"La fuente de RAG {key} debe usar exclusivamente el label production.")
        top_k = rag_payload.get("top_k", 5)
        if not isinstance(top_k, int) or top_k < 1:
            raise ValueError(f"rag {key}: top_k debe ser un entero mayor a 0.")
        rag_sources[normalized_key] = RagSourceConfig(
            key=normalized_key,
            name=_required_text(rag_payload, "name", context=f"rag {key}"),
            label=label,
            business_scope=_required_text(rag_payload, "business_scope", context=f"rag {key}"),
            top_k=top_k,
        )

    raw_vector_search = payload.get("vector_search")
    vector_search: VectorSearchConfig | None = None
    if raw_vector_search is not None:
        if not isinstance(raw_vector_search, dict):
            raise ValueError("vector_search, si se declara, debe ser un objeto.")
        top_k = raw_vector_search.get("top_k", 5)
        if not isinstance(top_k, int) or top_k < 1:
            raise ValueError("vector_search: top_k debe ser un entero mayor a 0.")
        raw_store_names = raw_vector_search.get("store_names")
        store_names: tuple[str, ...] | None = None
        if raw_store_names is not None:
            if not isinstance(raw_store_names, list) or not raw_store_names:
                raise ValueError("vector_search: store_names, si se declara, debe ser una lista no vacía.")
            if not all(isinstance(name, str) and name.strip() for name in raw_store_names):
                raise ValueError("vector_search: store_names debe contener sólo strings no vacíos.")
            store_names = tuple(name.strip() for name in raw_store_names)
        vector_search = VectorSearchConfig(top_k=top_k, store_names=store_names)

    return ClientConfig(
        client_id=_required_text(payload, "client_id", context="cliente"),
        display_name=_required_text(payload, "display_name", context="cliente"),
        tenant=_required_text(payload, "tenant", context="cliente"),
        model=_required_text(payload, "model", context="cliente"),
        data_map_path=data_map_path,
        sources=sources,
        business_rulebooks=business_rulebooks,
        rag_sources=rag_sources,
        vector_search=vector_search,
    )
