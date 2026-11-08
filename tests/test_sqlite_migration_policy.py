"""The additive label must match a small, mechanically decoded operation set."""

from __future__ import annotations

from textwrap import dedent, indent

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa

from storage.migration_policy import classify_migration, classify_migration_source, declared_migration_epoch


def migration(body: str, *, declaration: str = 'MIGRATION_COMPATIBILITY = "additive"') -> str:
    return (
        "from alembic import op\nimport sqlalchemy as sa\n"
        "revision = '20990101_9999'\ndown_revision = '20261108_0076'\n"
        + declaration
        + "\ndef upgrade() -> None:\n"
        + indent(dedent(body), "    ")
        + "\ndef downgrade() -> None:\n    op.drop_table('not_run')\n"
    )


ADDITIVE = [
    'op.create_index("ix_items_value", "items", ["value"])',
    'op.create_index("ix_items_value", "items", ["value"], unique=False, if_not_exists=True)',
    'op.create_table("extra", sa.Column("id", sa.Integer, primary_key=True), sa.Column("value", sa.Text(), unique=True))',
    'op.add_column("items", sa.Column("extra", sa.Text(), nullable=True))',
    'op.add_column("items", sa.Column("extra", sa.String(20)))',
    'op.add_column("items", sa.Column("extra", sa.Integer(), nullable=False, server_default=sa.text("0")))',
    'op.add_column("items", sa.Column("extra", sa.Text(), nullable=False, server_default="NULL"))',
    'op.add_column("items", sa.Column("extra", sa.Text(), nullable=False, server_default=sa.text("\'it\'\'s\'")))',
]


@pytest.mark.parametrize("body", ADDITIVE)
def test_proven_operations_preserve_real_sqlite_named_column_writes(body):
    source = migration(body)
    decision = classify_migration_source(source)
    assert decision.permitted == "additive", decision.reason
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
            conn.exec_driver_sql("INSERT INTO items (id,value) VALUES (1,'before')")
            # Execute only test-authored fixtures, through real Alembic/SQLite.
            namespace = {}
            exec(source, namespace)
            with Operations.context(MigrationContext.configure(conn)):
                namespace["upgrade"]()
            conn.exec_driver_sql("INSERT INTO items (id,value) VALUES (2,'old-writer')")
            conn.exec_driver_sql("UPDATE items SET value='updated' WHERE id=1")
            assert conn.exec_driver_sql("SELECT id,value FROM items ORDER BY id").all() == [
                (1, "updated"),
                (2, "old-writer"),
            ]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "body",
    [
        'op.create_index("uq", "items", ["value"], unique=True)',
        'op.create_index("ix", "items", [sa.text("lower(value)")])',
        'op.create_index("ix", "items", ["value"], sqlite_where=sa.text("value IS NOT NULL"))',
        'op.create_index("ix", "items", ["value"], schema="temp")',
        'op.create_index("ix", "items", ["value"], if_not_exists="yes")',
        'op.create_index(None, "items", ["value"])',
        'op.create_index("ix", "items", "value")',
        'op.create_index("ix", "items", [])',
        'op.create_index("ix", "items", ["value"], unique=False, unique=True)',
        'op.create_index("ix", "items", ["value"], **{})',
        'op.add_column("items", sa.Column("extra", sa.Text(), nullable=False))',
        'op.add_column("items", sa.Column("extra", sa.Text(), nullable=False, default="client-only"))',
        'op.add_column("items", sa.Column("extra", sa.Text(), nullable=False, server_default=sa.text("NULL")))',
        'op.add_column("items", sa.Column("extra", sa.Text(), server_default=sa.text("CURRENT_TIMESTAMP")))',
        'op.add_column("items", sa.Column("extra", sa.Text(), server_default=sa.text("0); DROP TABLE items; --")))',
        'op.add_column("items", sa.Column("extra", sa.Text(), nullable=True, unique=True))',
        'op.add_column("items", sa.Column("extra", sa.Integer(), primary_key=True))',
        'op.add_column("items", sa.Column("extra", sa.Text(), index=True))',
        'op.add_column("items", sa.Column("extra", sa.Integer(), sa.ForeignKey("items.id")))',
        'op.add_column("items", sa.Column("extra", sa.Integer(), sa.CheckConstraint("extra > 0")))',
        'op.add_column("items", sa.Column("extra", sa.Integer(), sa.Computed("id + 1")))',
        'op.add_column("items", sa.Column("extra", sa.Boolean(create_constraint=True)))',
        'op.add_column("items", sa.Column("extra", sa.Text(), nullable="false"))',
        'op.add_column("items", sa.Column("extra"))',
        'op.create_table("extra", sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id")))',
        'op.create_table("extra", sa.Column("id", sa.Integer()), prefixes=["TEMPORARY"])',
        'op.create_table("extra")',
        'op.execute("UPDATE items SET value = NULL")',
        'op.drop_table("items")',
        'op.rename_table("items", "new_items")',
        'op.alter_column("items", "value", nullable=False)',
        "helper()",
        'if True:\n    op.create_index("ix", "items", ["value"])',
        'with op.batch_alter_table("items") as batch:\n    batch.add_column(sa.Column("extra", sa.Text()))',
        "pass",
    ],
)
def test_unknown_or_write_restricting_operations_are_breaking(body):
    decision = classify_migration_source(migration(body))
    assert decision.declared == "additive"
    assert decision.permitted == "breaking"
    assert decision.reason


@pytest.mark.parametrize(
    "declaration",
    [
        "",
        'MIGRATION_SAFETY = "additive"',
        'MIGRATION_COMPATIBILITY = "unknown"',
        'MIGRATION_COMPATIBILITY = "breaking"',
        'MIGRATION_COMPATIBILITY = ["additive"]',
        "MIGRATION_COMPATIBILITY = make_label()",
        'MIGRATION_COMPATIBILITY = "additive"\nMIGRATION_COMPATIBILITY = "additive"',
        'if True:\n    MIGRATION_COMPATIBILITY = "additive"',
    ],
)
def test_missing_unknown_or_breaking_declarations_never_opt_in(declaration):
    assert classify_migration_source(migration(ADDITIVE[0], declaration=declaration)).permitted == "breaking"


@pytest.mark.parametrize(
    "extra",
    [
        "def op():\n    pass\n",
        "def sa():\n    pass\n",
        "sa = None\n",
        "op = None\n",
        "upgrade = None\n",
        "def helper(value=op.execute('DELETE FROM items')):\n    pass\n",
        "def helper() -> op.execute('DELETE FROM items'):\n    pass\n",
        "revision: op.execute('DELETE FROM items') = 'another'\n",
        "COMPATIBILITY_EPOCH = 2\n",
        "import another_module\n",
        "op.execute('DELETE FROM items')\n",
    ],
)
def test_top_level_work_and_rebindings_cannot_hide_behind_a_safe_upgrade(extra):
    decision = classify_migration_source(migration(ADDITIVE[0]) + extra)
    assert decision.permitted == "breaking"


@pytest.mark.parametrize(
    "old,new",
    [
        ("from alembic import op", "from .alembic import op"),
        ("from alembic import op", ""),
        ("import sqlalchemy as sa", ""),
        ("def upgrade() -> None:", "def upgrade() -> op.execute('DELETE FROM items'):"),
        ("def downgrade() -> None:", "def downgrade(value=op.execute('DELETE FROM items')):"),
        ("def upgrade() -> None:", "@decorate\ndef upgrade() -> None:"),
    ],
)
def test_only_known_namespaces_and_inert_function_definitions_are_proven(old, new):
    assert classify_migration_source(migration(ADDITIVE[3]).replace(old, new)).permitted == "breaking"


def test_no_author_code_is_executed(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    source = migration(ADDITIVE[0]) + f"open({str(sentinel)!r}, 'w').write('unsafe')\n"
    path = tmp_path / "migration.py"
    path.write_text(source)
    assert classify_migration(path).permitted == "breaking"
    assert not sentinel.exists()


def test_entire_batch_of_operations_must_be_additive():
    source = migration(ADDITIVE[0] + "\n" + ADDITIVE[3])
    assert classify_migration_source(source).permitted == "additive"
    assert (
        classify_migration_source(migration(ADDITIVE[0] + '\nop.execute("DELETE FROM items")')).permitted == "breaking"
    )


@pytest.mark.parametrize(
    "type_source",
    [
        "sa.String(\"10) CHECK(value != 'blocked'\")",
        "sa.String(length=\"10) CHECK(value != 'blocked'\")",
        "sa.Numeric(\"10) CHECK(value != 'blocked'\")",
        "sa.Numeric(10, \"2) CHECK(value != 'blocked'\")",
    ],
)
def test_type_constructor_injection_is_rejected_before_it_can_restrict_old_writes(type_source):
    source = migration(f'op.add_column("items", sa.Column("extra", {type_source}, nullable=True))')
    assert classify_migration_source(source).permitted == "breaking"
    # Pin the actual SQLAlchemy/SQLite failure mode, not only our parser's vote.
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
            conn.exec_driver_sql("INSERT INTO items (value) VALUES ('before')")
            namespace = {}
            exec(source, namespace)
            with Operations.context(MigrationContext.configure(conn)):
                namespace["upgrade"]()
            with pytest.raises(sa.exc.IntegrityError):
                conn.exec_driver_sql("INSERT INTO items (value) VALUES ('blocked')")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "type_source",
    [
        "sa.String(length=20)",
        "sa.Numeric(10, 2)",
        "sa.Numeric(precision=10, scale=0)",
        "sa.Float(precision=10)",
        "sa.DateTime(timezone=True)",
        "sa.Boolean()",
        "sa.LargeBinary(10)",
    ],
)
def test_type_sizes_are_typed_literal_values(type_source):
    assert (
        classify_migration_source(migration(f'op.add_column("items", sa.Column("extra", {type_source}))')).permitted
        == "additive"
    )


@pytest.mark.parametrize(
    "type_source",
    [
        'sa.String("10")',
        "sa.String(True)",
        "sa.String(-1)",
        "sa.String(20, 'NOCASE')",
        'sa.Numeric(scale="2")',
        "sa.Numeric(scale=True)",
        'sa.Float(precision="1")',
        "sa.Boolean(True)",
        "sa.Boolean(create_constraint=True)",
        'sa.DateTime(timezone="true")',
    ],
)
def test_unknown_type_options_do_not_opt_in(type_source):
    assert (
        classify_migration_source(migration(f'op.add_column("items", sa.Column("extra", {type_source}))')).permitted
        == "breaking"
    )


@pytest.mark.parametrize("alias", ["rowid", "_rowid_", "oid", "RoWiD", "OID"])
def test_new_columns_cannot_shadow_existing_implicit_row_identifiers(alias):
    source = migration(f'op.add_column("items", sa.Column({alias!r}, sa.Integer(), nullable=True))')
    assert classify_migration_source(source).permitted == "breaking"
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE items (value TEXT)")
            conn.exec_driver_sql("INSERT INTO items (value) VALUES ('before')")
            assert conn.exec_driver_sql(f"SELECT {alias} FROM items").scalar() == 1
            namespace = {}
            exec(source, namespace)
            with Operations.context(MigrationContext.configure(conn)):
                namespace["upgrade"]()
            assert conn.exec_driver_sql(f"SELECT {alias} FROM items").scalar() is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "rebinding",
    [
        'MIGRATION_COMPATIBILITY += ""',
        'if True:\n    MIGRATION_COMPATIBILITY = "additive"',
        "del MIGRATION_COMPATIBILITY",
    ],
)
def test_literal_classification_cannot_be_rebound(rebinding):
    assert classify_migration_source(migration(ADDITIVE[0]) + rebinding).permitted == "breaking"


@pytest.mark.parametrize(
    "declaration",
    [
        "COMPATIBILITY_EPOCH = None",
        "COMPATIBILITY_EPOCH = True",
        'COMPATIBILITY_EPOCH = "2"',
        "COMPATIBILITY_EPOCH = 0",
        "COMPATIBILITY_EPOCH = compute()",
        "COMPATIBILITY_EPOCH = 2\nCOMPATIBILITY_EPOCH -= 1",
    ],
)
def test_runtime_epoch_parser_distinguishes_missing_from_malformed(declaration):
    assert declared_migration_epoch("", strict=True) is None
    assert declared_migration_epoch("COMPATIBILITY_EPOCH = 2", strict=True) == 2
    assert declared_migration_epoch(declaration) is None
    with pytest.raises(ValueError):
        declared_migration_epoch(declaration, strict=True)


def test_invalid_python_is_breaking():
    assert classify_migration_source("def upgrade(:").permitted == "breaking"
