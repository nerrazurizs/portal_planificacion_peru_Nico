"""
Safe SQL construction helpers using parameterized queries.
All functions return (query_fragment, params_list) tuples.
"""


def build_in_clause(field_name, values):
    """Build a parameterized IN clause.
    Returns (sql_fragment, params) or (None, []) if values is empty.

    Example: build_in_clause("a.sku_producto", ["ABC", "DEF"])
    Returns: ("a.sku_producto IN (%s, %s)", ["ABC", "DEF"])
    """
    if not values:
        return None, []
    placeholders = ", ".join(["%s"] * len(values))
    return f"{field_name} IN ({placeholders})", list(values)


def build_ilike(field_name, value):
    """Build a parameterized ILIKE clause.
    Returns (sql_fragment, params) or (None, []) if value is empty.

    Example: build_ilike("c.marca", "dorel")
    Returns: ("c.marca ILIKE %s", ["%dorel%"])
    """
    if not value:
        return None, []
    return f"{field_name} ILIKE %s", [f"%{value}%"]


def build_ilike_exact(field_name, value):
    """Build a parameterized exact ILIKE clause (no wildcards).
    Returns (sql_fragment, params) or (None, []) if value is empty.
    """
    if not value:
        return None, []
    return f"{field_name} ILIKE %s", [value]


def build_multi_value_filter(field_name, values_str):
    """Parse a multi-value string (comma/space/newline separated) and build appropriate clause.
    If single value -> ILIKE with wildcards
    If multiple values -> IN clause (exact match)

    Returns (sql_fragment, params) or (None, []) if empty.
    """
    if not values_str:
        return None, []
    vals = [v.strip().upper() for v in values_str.replace(",", " ").replace("\n", " ").replace("\t", " ").replace(";", " ").split() if v.strip()]
    if not vals:
        return None, []
    if len(vals) == 1:
        return build_ilike(field_name, vals[0])
    return build_in_clause(field_name, vals)


def build_where(conditions, params_list):
    """Combine condition fragments into a WHERE clause.

    conditions: list of SQL condition strings (e.g. ["a.sku IN (%s, %s)", "c.marca ILIKE %s"])
    params_list: list of param lists (e.g. [["ABC", "DEF"], ["%dorel%"]])

    Returns (where_clause, flat_params)
    """
    if not conditions:
        return "", []
    flat_params = []
    for p in params_list:
        flat_params.extend(p)
    return " WHERE " + " AND ".join(conditions), flat_params


def append_condition(conditions, params_list, sql_fragment, params):
    """Helper to append a condition only if it's not None."""
    if sql_fragment is not None:
        conditions.append(sql_fragment)
        params_list.append(params)


def build_provider_filter(field_name_1, field_name_2, values_str):
    """Build OR filter across two fields (e.g. search provider by name OR code).
    Returns (sql_fragment, params) or (None, []) if empty.
    """
    if not values_str:
        return None, []
    vals = [v.strip() for v in values_str.replace(",", " ").split() if v.strip()]
    if not vals:
        return None, []
    or_parts = []
    params = []
    for v in vals:
        or_parts.append(f"{field_name_1} ILIKE %s")
        params.append(f"%{v}%")
        or_parts.append(f"{field_name_2} ILIKE %s")
        params.append(f"%{v}%")
    return f"({' OR '.join(or_parts)})", params
