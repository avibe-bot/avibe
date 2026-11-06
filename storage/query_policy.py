"""Schema-only PRAGMA contract shared by guarded queries and their CLI help."""

# These describe the schema and have no setter. Every other PRAGMA stays denied.
SCHEMA_PRAGMAS = frozenset(
    {"table_list", "table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}
)
