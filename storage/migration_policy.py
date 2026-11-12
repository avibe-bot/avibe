"""Mechanically prove a deliberately small subset of additive migrations.

The author's label is necessary, not sufficient. Only straight-line Alembic
operations with literal arguments and a few SQLAlchemy constructors are
interpreted. SQL, helpers, conditions, data rewrites and unknown constructs are
breaking by default. Historical MIGRATION_SAFETY is not a compatibility proof.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal

from alembic.operations import ops
import sqlalchemy as sa

CompatibilityClass = Literal["additive", "breaking"]
_CLASS_FIELD = "MIGRATION_COMPATIBILITY"
_MISSING = object()
_TYPES = {
    name: getattr(sa, name)
    for name in (
        "String",
        "Text",
        "Unicode",
        "UnicodeText",
        "Integer",
        "BigInteger",
        "SmallInteger",
        "Float",
        "Numeric",
        "Boolean",
        "DateTime",
        "Date",
        "LargeBinary",
    )
}
_TYPE_PARAMETERS = {
    **{name: ("length",) for name in ("String", "Text", "Unicode", "UnicodeText", "LargeBinary")},
    "Float": ("precision",),
    "Numeric": ("precision", "scale"),
    "DateTime": ("timezone",),
}
_CONSTRUCTORS = {**_TYPES, "Column": sa.Column, "text": sa.text}
_METADATA_FIELDS = {
    "revision",
    "down_revision",
    "branch_labels",
    "depends_on",
    "MIGRATION_COMPATIBILITY",
    "MIGRATION_SAFETY",
    "COMPATIBILITY_EPOCH",
}
_OPERATION_KWARGS = {
    "create_index": {"schema", "unique", "if_not_exists"},
    "create_table": {"schema", "if_not_exists"},
    "add_column": {"schema"},
    "execute": set(),
}
_LITERAL_SQL = re.compile(r"(?:[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|'(?:[^']|'')*'|NULL)\Z", re.I | re.S)


@dataclass(frozen=True)
class MigrationCompatibility:
    declared: str | None
    permitted: CompatibilityClass
    reason: str | None = None


class UnprovenMigration(ValueError):
    pass


def _literal_value(tree: ast.Module, field: str) -> object | None:
    values = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == field for target in statement.targets
        ):
            values.append(statement.value)
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == field
        ):
            values.append(statement.value)
    # A literal followed by +=, del, a conditional assignment or a rebinding
    # function/import is not the runtime declaration the guard claims to read.
    bindings = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == field and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == field:
            bindings += 1
        elif isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == field:
            bindings += 1
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name == field:
            bindings += 1
        elif isinstance(node, ast.MatchMapping) and node.rest == field:
            bindings += 1
    if not values and not bindings:
        return _MISSING
    if len(values) == bindings == 1 and isinstance(values[0], ast.Constant):
        return values[0].value
    raise UnprovenMigration(f"{field} requires one unmodified literal declaration")


def declared_migration_epoch(source: str, *, strict: bool = False) -> int | None:
    """Read the source contract, never a mutable imported module attribute.

    Missing means no epoch boundary. CI treats malformed values as unproved;
    runtime callers use strict mode so malformed boundaries cannot be inherited.
    """
    try:
        value = _literal_value(ast.parse(source), "COMPATIBILITY_EPOCH")
        if value is _MISSING:
            return None
        if type(value) is not int or value <= 0:
            raise UnprovenMigration("COMPATIBILITY_EPOCH must be a positive literal integer")
        return value
    except (SyntaxError, UnprovenMigration):
        if strict:
            raise
        return None


def _type_value(name: str, args: list, keywords: dict):
    parameters = _TYPE_PARAMETERS.get(name, ())
    if (
        len(args) > len(parameters)
        or keywords.keys() - set(parameters)
        or set(parameters[: len(args)]) & keywords.keys()
    ):
        raise UnprovenMigration("unclassified type constructor arguments")
    values = dict(zip(parameters, args)) | keywords
    for parameter, value in values.items():
        if parameter == "timezone":
            valid = type(value) is bool
        else:
            valid = value is None or (type(value) is int and value >= (0 if parameter == "scale" else 1))
        if not valid:
            # SQLAlchemy interpolates sizes into DDL; a successful String or
            # Numeric constructor is NOT proof that a string size is safe SQL.
            raise UnprovenMigration("type sizes must be integers; timezone must be boolean")
    return _TYPES[name](*args, **keywords)


def _value(node: ast.AST):
    """Decode literals/constructors, never execute author-provided Python."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sa"
        and node.attr in _TYPES
    ):
        return _TYPES[node.attr]
    if isinstance(node, ast.Call):
        fn = node.func
        if not (
            isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "sa"
            and fn.attr in _CONSTRUCTORS
        ):
            raise UnprovenMigration("opaque SQLAlchemy constructor/helper")
        if any(keyword.arg is None for keyword in node.keywords) or len(
            {keyword.arg for keyword in node.keywords}
        ) != len(node.keywords):
            raise UnprovenMigration("expanded or duplicate keyword arguments")
        args = [_value(arg) for arg in node.args]
        keywords = {keyword.arg: _value(keyword.value) for keyword in node.keywords}
        if fn.attr in _TYPES:
            return _type_value(fn.attr, args, keywords)
        if fn.attr == "text":
            if len(args) != 1 or type(args[0]) is not str or keywords:
                raise UnprovenMigration("text requires one literal string")
            return sa.text(args[0])
        if len(args) != 2 or type(args[0]) is not str:
            raise UnprovenMigration("Column requires a literal name and an allowlisted type")
        if keywords.keys() - {"nullable", "server_default", "primary_key", "unique", "index", "autoincrement"}:
            raise UnprovenMigration("unclassified constructor option")
        if any(
            name in keywords and type(keywords[name]) is not bool
            for name in ("nullable", "primary_key", "unique", "index")
        ):
            raise UnprovenMigration("column flags must be literal booleans")
        if "autoincrement" in keywords and not (
            type(keywords["autoincrement"]) is bool or keywords["autoincrement"] in ("auto", "ignore_fk")
        ):
            raise UnprovenMigration("unclassified autoincrement option")
        return sa.Column(*args, **keywords)
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise UnprovenMigration("non-literal operation argument") from exc


def _emitted_operations(tree: ast.Module) -> list[ops.MigrateOperation]:
    # Reject top-level code that could replace the op/sa bindings or execute
    # hidden work. Helpers in downgrade are irrelevant; calls from upgrade are
    # not on this allowlist even when the helper happens to look safe today.
    imports: set[str] = set()
    bindings: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            if [(alias.name, alias.asname) for alias in statement.names] != [("sqlalchemy", "sa")]:
                raise UnprovenMigration("unclassified module import")
            imports.add("sa")
        elif isinstance(statement, ast.ImportFrom):
            names = [(alias.name, alias.asname) for alias in statement.names]
            if statement.level or not (
                (statement.module == "alembic" and names == [("op", None)])
                or (statement.module == "__future__" and names == [("annotations", None)])
            ):
                raise UnprovenMigration("unclassified module import")
            if statement.module == "alembic":
                imports.add("op")
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            try:
                ast.literal_eval(value)
            except (ValueError, TypeError) as exc:
                raise UnprovenMigration("computed module-level state") from exc
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if (
                len(targets) != 1
                or not isinstance(targets[0], ast.Name)
                or targets[0].id not in _METADATA_FIELDS
                or targets[0].id in bindings
            ):
                raise UnprovenMigration("unclassified/rebound module name")
            bindings.add(targets[0].id)
            if isinstance(statement, ast.AnnAssign) and not (
                isinstance(statement.annotation, ast.Name) and statement.annotation.id in {"str", "int"}
            ):
                raise UnprovenMigration("unclassified metadata annotation")
        elif isinstance(statement, ast.FunctionDef):
            if statement.name not in {"upgrade", "downgrade"} or statement.name in bindings:
                raise UnprovenMigration("unclassified/rebound function name")
            bindings.add(statement.name)
            args = statement.args
            if (
                statement.decorator_list
                or args.args
                or args.posonlyargs
                or args.kwonlyargs
                or args.vararg
                or args.kwarg
            ):
                raise UnprovenMigration("migration functions must be undecorated and take no arguments")
            if statement.returns is not None and not (
                isinstance(statement.returns, ast.Constant) and statement.returns.value in (None, "None")
            ):
                raise UnprovenMigration("unclassified function annotation")
        elif not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            raise UnprovenMigration("unclassified module-level work")
    if "COMPATIBILITY_EPOCH" in bindings:
        raise UnprovenMigration("an epoch boundary must be classified breaking")
    if "op" not in imports or (
        any(isinstance(node, ast.Name) and node.id == "sa" for node in ast.walk(tree)) and "sa" not in imports
    ):
        raise UnprovenMigration("operation namespace was not imported")
    upgrades = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"]
    if len(upgrades) != 1:
        raise UnprovenMigration("expected one upgrade function")
    upgrade = upgrades[0]
    emitted = []
    for statement in upgrade.body:
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            raise UnprovenMigration("upgrade contains SQL, helpers, branching or unclassified work")
        call = statement.value
        fn = call.func
        if not (
            isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "op"
            and fn.attr in _OPERATION_KWARGS
        ):
            raise UnprovenMigration("operation is not on the additive allowlist")
        if any(keyword.arg not in _OPERATION_KWARGS[fn.attr] for keyword in call.keywords) or len(
            {keyword.arg for keyword in call.keywords}
        ) != len(call.keywords):
            raise UnprovenMigration("unclassified operation option/expression")
        args = [_value(arg) for arg in call.args]
        kwargs = {keyword.arg: _value(keyword.value) for keyword in call.keywords}
        if (
            "if_not_exists" in kwargs
            and kwargs["if_not_exists"] is not None
            and type(kwargs["if_not_exists"]) is not bool
        ):
            raise UnprovenMigration("if_not_exists must be a literal boolean")
        if fn.attr == "execute":
            if len(args) != 1 or type(args[0]) is not str:
                raise UnprovenMigration("journal DDL requires one literal SQL string")
            operation = ops.ExecuteSQLOp(args[0])
        elif fn.attr == "create_index":
            operation = ops.CreateIndexOp(*args, **kwargs)
        elif fn.attr == "add_column":
            operation = ops.AddColumnOp(*args, **kwargs)
        else:
            operation = ops.CreateTableOp(args[0], args[1:], **kwargs)
        emitted.append(operation)
    if not emitted:
        raise UnprovenMigration("no proven additive operations")
    _validate_search_journal_operations(emitted)
    return emitted


def _validate_search_journal_operations(operations: list[ops.MigrateOperation]) -> None:
    """Prove the only permitted trigger effects target a new independent journal.

    This is deliberately not a raw-SQL or general trigger exemption. Names,
    source columns, timing, conflict policy and complete bodies are fixed. No
    FTS module or user-defined function participates in an old binary's writes.
    """
    journal_sql = [item for item in operations if isinstance(item, ops.ExecuteSQLOp)]
    if not journal_sql:
        return
    bodies = {
        "insert": ("INSERT", ("new",)),
        "update": ("UPDATE OF id, content_text", ("old", "new")),
        "delete": ("DELETE", ("old",)),
    }
    expected = {
        " ".join((
            f"CREATE TRIGGER IF NOT EXISTS message_search_{name} AFTER {event} ON messages "
            "WHEN EXISTS (SELECT 1 FROM message_search_state WHERE id = 1) BEGIN "
            + " ".join(
                f"INSERT OR IGNORE INTO message_search_dirty (message_id) VALUES ({row}.id);"
                for row in rows
            )
            + " END"
        ).split())
        for name, (event, rows) in bodies.items()
    }
    actual = [" ".join(item.sqltext.split()) for item in journal_sql]
    if len(actual) != len(expected) or set(actual) != expected:
        raise UnprovenMigration("only the complete message-search journal trigger bundle is additive")
    states = [
        item for item in operations
        if isinstance(item, ops.CreateTableOp) and item.table_name == "message_search_state"
    ]
    if len(states) != 1 or not any(
        isinstance(column, sa.Column) and column.name == "id"
        and type(column.type) is sa.Integer and column.primary_key and not column.nullable
        for column in states[0].columns
    ):
        raise UnprovenMigration("journal activation requires its new integer-keyed state table")
    targets = [
        item for item in operations
        if isinstance(item, ops.CreateTableOp) and item.table_name == "message_search_dirty"
    ]
    if len(targets) != 1 or len(targets[0].columns) != 1:
        raise UnprovenMigration("journal requires its new independent single-column target")
    column = targets[0].columns[0]
    if not (
        isinstance(column, sa.Column) and column.name == "message_id"
        and type(column.type) is sa.Text and column.primary_key
        and not column.nullable and column.server_default is None
        and _column_safe(column, independent=True)
    ):
        raise UnprovenMigration("journal target must accept every non-null Message id")
    # A later operation must not constrain the journal or replace its key shape.
    if any(
        getattr(item, "table_name", None) == "message_search_dirty" and item is not targets[0]
        for item in operations
    ):
        raise UnprovenMigration("journal target cannot have additional operations")


def _column_safe(column: object, *, independent: bool) -> bool:
    if (
        not isinstance(column, sa.Column)
        or not isinstance(column.name, str)
        or not column.name
        or type(column.type) not in _TYPES.values()
        or column.foreign_keys
        or column.constraints
        or column.computed is not None
        or column.identity is not None
    ):
        return False
    if column.index or (not independent and (column.primary_key or column.unique)):
        return False
    default = column.server_default
    if default is not None:
        value = default.arg
        if isinstance(value, sa.sql.elements.TextClause):
            if not _LITERAL_SQL.fullmatch(value.text.strip()):
                return False
            non_null = value.text.strip().upper() != "NULL"
        elif isinstance(value, str):
            non_null = True  # SQLAlchemy quotes string server defaults.
        else:
            return False
    else:
        non_null = False
    return independent or bool(column.nullable) or non_null


def _operation_safe(operation: ops.MigrateOperation) -> bool:
    if isinstance(operation, ops.ExecuteSQLOp):
        # Whole-bundle validation (including the target table) ran above.
        return True
    if operation.schema is not None or type(operation.table_name) is not str or not operation.table_name:
        return False
    if isinstance(operation, ops.CreateIndexOp):
        return (
            type(operation.index_name) is str
            and bool(operation.index_name)
            and operation.unique is False
            and isinstance(operation.columns, (list, tuple))
            and bool(operation.columns)
            and all(type(column) is str and column for column in operation.columns)
        )
    if isinstance(operation, ops.AddColumnOp):
        return _column_safe(operation.column, independent=False) and operation.column.name.lower() not in {
            "rowid",
            "_rowid_",
            "oid",
        }
    if isinstance(operation, ops.CreateTableOp):
        return bool(operation.columns) and all(_column_safe(column, independent=True) for column in operation.columns)
    return False


def classify_migration_source(source: str) -> MigrationCompatibility:
    """Return the mechanically permitted class; uncertainty always means breaking."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return MigrationCompatibility(None, "breaking", "invalid Python source")
    try:
        value = _literal_value(tree, _CLASS_FIELD)
    except UnprovenMigration as exc:
        return MigrationCompatibility(None, "breaking", str(exc))
    declaration = value if type(value) is str else None
    if declaration != "additive":
        return MigrationCompatibility(
            declaration, "breaking", None if declaration == "breaking" else "missing/unknown literal classification"
        )
    try:
        operations = _emitted_operations(tree)
        if not all(_operation_safe(operation) for operation in operations):
            raise UnprovenMigration("emitted operation changes accepted writes or is not proven independent")
    except (UnprovenMigration, TypeError, ValueError, IndexError, AttributeError, sa.exc.ArgumentError) as exc:
        return MigrationCompatibility(declaration, "breaking", str(exc))
    return MigrationCompatibility(declaration, "additive")


def classify_migration(path: str | Path) -> MigrationCompatibility:
    return classify_migration_source(Path(path).read_text(encoding="utf-8"))
