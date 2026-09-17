"""Actualiza de forma determinística los criterios vigentes (snapshots de Langfuse) de un cliente."""

from __future__ import annotations

import argparse
import json
import sys

import vi_agent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresca los snapshots locales de criterios de negocio (Langfuse) de un cliente."
    )
    parser.add_argument(
        "--client",
        default="mens_fashion_alto",
        help="client_id a refrescar (carpeta bajo clientes/, ej. mens_fashion_alto, atlas_medio).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    vi_agent._configure_console_utf8()
    vi_agent.configure_client(args.client)
    vi_agent.load_environment()
    results = vi_agent.refresh_business_rules()
    print(
        json.dumps(
            {
                "client": vi_agent.CLIENT_CONFIG.display_name,
                "updated_rulebooks": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=sys.stdout,
    )


if __name__ == "__main__":
    main()
