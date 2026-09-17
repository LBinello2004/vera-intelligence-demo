"""Validación estructural de consultas de solo lectura y un único cliente."""

from __future__ import annotations

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from client_config import ClientConfig


ALLOWED_FUNCTIONS = {
    "ABS",
    "AVG",
    "CAST",
    "CEIL",
    "CEILING",
    "COALESCE",
    "CONCAT",
    "COUNT",
    "CURRENT_DATE",
    "DATE_TRUNC",
    "DENSE_RANK",
    "EXTRACT",
    "FLOOR",
    "GREATEST",
    "LAG",
    "LEAD",
    "LEAST",
    "LENGTH",
    "LOWER",
    "MAX",
    "MIN",
    "NULLIF",
    "PERCENT_RANK",
    "PERCENTILE_CONT",
    "PERCENTILE_DISC",
    "RANK",
    "REPLACE",
    "ROUND",
    "ROW_NUMBER",
    "STDDEV",
    "STDDEV_POP",
    "STDDEV_SAMP",
    "SUBSTRING",
    "SUM",
    "TO_CHAR",
    "TRIM",
    "UPPER",
    "VARIANCE",
}

FORBIDDEN_EXPRESSIONS = (
    exp.Alter,
    exp.Command,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Insert,
    exp.Merge,
    exp.Transaction,
    exp.Update,
)


def _table_name(table: exp.Table) -> str:
    parts = [part for part in (table.catalog, table.db, table.name) if part]
    return ".".join(parts).lower()


def _has_exact_tenant_filter(
    scope: exp.Expression,
    field: str,
    tenant: str,
    *,
    qualifier: str | None = None,
    require_qualifier: bool = False,
) -> bool:
    """Busca `qualifier.field = 'tenant'` (o `field = 'tenant'` sin calificar,
    cuando `require_qualifier` es False) dentro de `scope` únicamente.

    `scope` es el nodo Select/subconsulta que efectivamente encierra a la
    tabla que se está validando -no todo el statement-: un filtro escrito
    dentro de una CTE cuenta para las tablas de esa misma CTE, no para las de
    otra. Cuando la tabla es la única fuente física de su scope, un filtro
    SIN calificar (columna sin alias, ej. `seller_id = 'X'` dentro de una CTE
    de una sola tabla) también es válido -no hay ambigüedad posible ahí-.
    Cuando el scope tiene más de una fuente física (un JOIN dentro de la
    misma CTE o del mismo SELECT), se exige el calificador exacto por alias
    para que un predicado de una tabla no habilite accidentalmente a la otra.
    """
    for equality in scope.find_all(exp.EQ):
        pairs = ((equality.left, equality.right), (equality.right, equality.left))
        for possible_column, possible_literal in pairs:
            if not isinstance(possible_column, exp.Column):
                continue
            if possible_column.name.lower() != field.lower():
                continue
            column_qualifier = possible_column.table
            if column_qualifier:
                if not qualifier or column_qualifier.lower() != qualifier.lower():
                    continue
            elif require_qualifier:
                continue
            if isinstance(possible_literal, exp.Literal) and possible_literal.is_string:
                if possible_literal.this == tenant:
                    return True
    return False


def validate_readonly_sql(sql: str, client: ClientConfig) -> None:
    """Rechaza consultas fuera de las vistas y el tenant configurados."""
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("La consulta está vacía.")
    try:
        statements = parse(sql, read="postgres")
    except ParseError as exc:
        raise ValueError(f"La consulta no tiene una estructura SQL válida: {exc}") from exc
    if len(statements) != 1:
        raise ValueError("Sólo se permite una consulta por ejecución.")

    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise ValueError("Sólo se permiten consultas de lectura SELECT o WITH...SELECT.")
    for forbidden_type in FORBIDDEN_EXPRESSIONS:
        if statement.find(forbidden_type) is not None:
            raise ValueError("La consulta contiene una operación no permitida.")

    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    referenced_sources: set[str] = set()
    # Cada tabla física se valida contra el scope (Select/subconsulta) que
    # efectivamente la contiene, no contra el statement completo -así una
    # tabla dentro de una CTE se valida con los predicados de esa CTE, no
    # con los de otra CTE ni con el WHERE del SELECT final que la combina.
    physical_tables: list[tuple[str, str, exp.Expression]] = []
    for table in statement.find_all(exp.Table):
        name = _table_name(table)
        if not name:
            raise ValueError("La consulta contiene una fuente que no se puede validar.")
        if "." not in name and name in cte_names:
            continue
        if name not in client.sources:
            raise ValueError(f"La consulta referencia una fuente no autorizada: {name}")
        referenced_sources.add(name)
        scope = table.find_ancestor(exp.Select) or statement
        physical_tables.append((name, table.alias_or_name.lower(), scope))

    if not referenced_sources:
        raise ValueError("La consulta no referencia ninguna fuente autorizada.")

    # Las funciones nativas que sqlglot reconoce tienen nodos tipados. Una
    # función arbitraria queda como Anonymous: sólo permitimos un conjunto
    # explícito para impedir llamadas laterales o funciones administrativas.
    for function in statement.find_all(exp.Anonymous):
        name = function.name.upper()
        if name not in ALLOWED_FUNCTIONS:
            raise ValueError(f"La consulta usa una función no autorizada: {name}")

    for source, qualifier, scope in physical_tables:
        tenant_field = client.sources[source].tenant_field
        # Con más de una fuente física EN EL MISMO SCOPE (ej. un JOIN dentro
        # del mismo SELECT/CTE) exigimos un filtro calificado por alias para
        # cada una, buscado sólo dentro de ese scope. Así un predicado de una
        # tabla no puede habilitar accidentalmente a otra, ni un filtro de
        # una CTE distinta puede "prestarle" aislamiento a esta. Cuando la
        # tabla es la única fuente física de su scope (ej. una CTE de una
        # sola tabla), un filtro sin calificar dentro de ese mismo scope es
        # igual de válido -no hay otra tabla ahí con la que confundirse-.
        siblings_in_scope = sum(1 for _, _, s in physical_tables if s is scope)
        if not _has_exact_tenant_filter(
            scope,
            tenant_field,
            client.tenant,
            qualifier=qualifier,
            require_qualifier=siblings_in_scope > 1,
        ):
            raise ValueError(
                "La consulta no aplica de forma explícita el aislamiento del cliente autorizado."
            )
