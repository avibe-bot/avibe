#!/usr/bin/env python3
"""Validate Model Hub authority tables against live registered consumers."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs/plans/model-hub-contracts"
REGISTRY = CONTRACTS / "mirror-registry.json"


class AuthorityInput:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.files: dict[Path, bytes] = {}
        self._trees: dict[str, ast.Module] = {}
        self._globs: dict[str, tuple[Path, ...]] = {}
        self._checkout_verified = False

    def _path(self, relative: str) -> Path:
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise ValueError(f"authority input escapes checkout: {relative}")
        return path

    def bytes(self, relative: str) -> bytes:
        path = self._path(relative)
        if path not in self.files:
            self.files[path] = path.read_bytes()
        return self.files[path]

    def text(self, relative: str) -> str:
        return self.bytes(relative).decode("utf-8")

    def json(self, relative: str) -> Any:
        return json.loads(self.text(relative))

    def python_tree(self, relative: str, *, retain: bool = True) -> ast.Module:
        if relative not in self._trees:
            tree = ast.parse(self.bytes(relative), filename=relative)
            if not retain:
                return tree
            self._trees[relative] = tree
        return self._trees[relative]

    def _git(self, *args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True, capture_output=True, timeout=30,
        ).stdout

    def glob(self, pattern: str) -> tuple[Path, ...]:
        if not self._checkout_verified:
            checkout = Path(os.fsdecode(self._git("rev-parse", "--show-toplevel")).rstrip("\n"))
            if checkout.resolve() != self.root:
                raise ValueError("authority source root must be the Git checkout root")
            self._checkout_verified = True
        if pattern not in self._globs:
            # Git owns ignore/pathspec semantics; include unstaged new consumers.
            names = self._git(
                "ls-files", "--cached", "--others", "--exclude-standard", "-z",
                "--", f":(glob){pattern}",
            ).split(b"\0")
            paths = (self._path(os.fsdecode(name)) for name in sorted(set(names)) if name)
            self._globs[pattern] = tuple(path for path in paths if path.is_file())
        return self._globs[pattern]

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for path, payload in sorted(self.files.items()):
            digest.update(path.relative_to(self.root).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(payload).digest())
        return digest.hexdigest()


def _json_pointer(document: Any, pointer: str) -> Any:
    value = document
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def _schema_vocabulary(source: AuthorityInput, spec: dict[str, Any]) -> set[str]:
    schema = source.json(f"docs/plans/model-hub-contracts/{spec['schema']}")
    values: set[Any] = set()
    for pointer in spec["paths"]:
        node = _json_pointer(schema, pointer)
        values.update(node["enum"] if "enum" in node else [node["const"]])
    values -= set(spec.get("exclude", ()))
    return {value for value in values if isinstance(value, str)}


def _schema_values(source: AuthorityInput, schema: str, paths: list[str], *, exclude=()) -> set[Any]:
    document = source.json(f"docs/plans/model-hub-contracts/{schema}")
    values: set[Any] = set()
    for pointer in paths:
        node = _json_pointer(document, pointer)
        values.update(node["enum"] if "enum" in node else [node["const"]])
    return values - set(exclude)


def _schema_property_names(source: AuthorityInput, relative: str) -> set[str]:
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                names.update(properties)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(source.json(relative))
    return names


def _schema_decision_tokens(source: AuthorityInput, relative: str) -> set[str]:
    """Collect schema-controlled field names and closed values, never prose."""

    tokens: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                tokens.update(properties)
            required = node.get("required")
            if isinstance(required, list):
                tokens.update(value for value in required if isinstance(value, str))
            enum = node.get("enum")
            if isinstance(enum, list):
                tokens.update(value for value in enum if isinstance(value, str))
            const = node.get("const")
            if isinstance(const, str):
                tokens.add(const)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(source.json(relative))
    return tokens


def _normative_absence_terms(source: AuthorityInput, spec: dict[str, Any]) -> set[str]:
    text = source.text(spec["source_file"])
    start = text.index(spec["start_marker"])
    block = text[start:].split("\n\n", 1)[0]
    terms: set[str] = set()
    for token in re.findall(r"`([^`]+)`", block):
        terms.update(part.strip() for part in token.split("|") if part.strip())
    return terms


def _markdown_row_contract(source: AuthorityInput, spec: dict[str, Any]) -> tuple[set[str], set[str]]:
    row = next(
        line
        for line in source.text(spec["file"]).splitlines()
        if line.startswith(f"| **{spec['row_id']}**")
    )
    tokens = {
        token
        for token in re.findall(r"`([^`]+)`", row)
        if re.fullmatch(r"[a-z][a-z0-9_]+", token)
    }
    negated = set(re.findall(spec["negated_pattern"], row))
    return tokens - negated, negated


def _markdown_table(source: AuthorityInput, spec: dict[str, Any]) -> set[str]:
    lines = source.text(spec["file"]).splitlines()
    try:
        heading_index = next(
            index
            for index, line in enumerate(lines)
            if line.strip().startswith(f"**{spec['heading']}")
        )
    except StopIteration as exc:
        raise ValueError(f"heading not found: {spec['heading']}") from exc

    table_index = next(
        index
        for index in range(heading_index + 1, len(lines))
        if lines[index].lstrip().startswith("|")
    )
    headers = [cell.strip() for cell in lines[table_index].strip().strip("|").split("|")]
    try:
        column = headers.index(spec["column"])
    except ValueError as exc:
        raise ValueError(
            f"column {spec['column']!r} not found under {spec['heading']!r}"
        ) from exc

    values: set[str] = set()
    for line in lines[table_index + 2 :]:
        if not line.lstrip().startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != len(headers):
            raise ValueError(f"malformed authority row under {spec['heading']!r}: {line}")
        tokens = re.findall(r"`([^`]+)`", cells[column])
        if len(tokens) != 1:
            raise ValueError(
                f"authority cell must contain one backticked value: {cells[column]!r}"
            )
        values.add(tokens[0])
    return values


def _markdown_field_names(source: AuthorityInput, spec: dict[str, Any]) -> set[str]:
    lines = source.text(spec["file"]).splitlines()
    heading_index = next(
        index
        for index, line in enumerate(lines)
        if line.strip().startswith(f"**{spec['heading']}")
    )
    table_index = next(
        index
        for index in range(heading_index + 1, len(lines))
        if lines[index].lstrip().startswith("|")
    )
    headers = [cell.strip() for cell in lines[table_index].strip().strip("|").split("|")]
    column = headers.index(spec["column"])
    fields: set[str] = set()
    for line in lines[table_index + 2 :]:
        if not line.lstrip().startswith("|"):
            break
        cell = [part.strip() for part in line.strip().strip("|").split("|")][column]
        fields.update(re.findall(r"\b([a-z][a-z0-9_]*)\s*:", cell))
    return fields


def _marker_values(source: AuthorityInput, spec: dict[str, Any]) -> set[str]:
    text = source.text(spec["file"])
    prefix = re.escape(spec["prefix"])
    return set(re.findall(rf"(?<![a-z0-9_.])({prefix}[a-z0-9_.]+)", text))


def _marker_tail_values(source: AuthorityInput, spec: dict[str, Any]) -> set[str]:
    """Read a live consumer declaration without embedding its members here."""
    values: set[str] = set()
    for line in source.text(spec["file"]).splitlines():
        if spec["marker"] not in line:
            continue
        tail = line.split(spec["marker"], 1)[1].split("-->", 1)[0]
        values.update(token for token in tail.split() if token)
    return values


def _literal_strings(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    values: set[str] = set()
    for child in ast.iter_child_nodes(node):
        values |= _literal_strings(child)
    return values


def _python_test_literals(source: AuthorityInput, relative: str, name: str) -> set[str]:
    tree = source.python_tree(relative)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return _literal_strings(node)
    raise ValueError(f"test function not found: {name}")


def _python_literal_annotation(source: AuthorityInput, spec: dict[str, Any]) -> set[str]:
    tree = source.python_tree(spec["file"])
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != spec["class"]:
            continue
        for item in node.body:
            if (
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and item.target.id == spec["field"]
            ):
                return _literal_strings(item.annotation)
    raise ValueError(f"annotation not found: {spec['class']}.{spec['field']}")


def _python_string_assignment(source: AuthorityInput, spec: dict[str, Any]) -> set[str]:
    tree = source.python_tree(spec["file"])
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == spec["name"] for target in targets):
            continue
        value = node.value
        if value is None:
            break
        return _literal_strings(value)
    raise ValueError(f"assignment not found: {spec['name']}")


def _typescript_string_union(source: AuthorityInput, spec: dict[str, Any]) -> set[str]:
    declaration = re.search(
        rf"^export\s+type\s+{re.escape(spec['name'])}\s*=\s*([^;]+);",
        source.text(spec["file"]),
        flags=re.MULTILINE,
    )
    if declaration is None:
        raise ValueError(f"exported type not found: {spec['name']}")
    body = declaration.group(1).strip()
    literal = re.compile(r'''\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|null)\s*(\||$)''')
    values: set[str] = set()
    offset = 1 if body.startswith("|") else 0
    while offset < len(body):
        token = literal.match(body, offset)
        if token is None:
            raise ValueError(f"not a string/null union: {spec['name']}")
        if token.group(1) != "null":
            values.add(ast.literal_eval(token.group(1)))
        offset = token.end()
        if token.group(2) and offset == len(body):
            raise ValueError(f"incomplete union: {spec['name']}")
    if not values:
        raise ValueError(f"empty string union: {spec['name']}")
    return values


def _versioned_schema_nodes(node: Any):
    """Yield every contract_version subschema from an arbitrary schema tree."""

    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict) and isinstance(
            properties.get("contract_version"),
            dict,
        ):
            yield properties["contract_version"]
        for value in node.values():
            yield from _versioned_schema_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _versioned_schema_nodes(value)


def _literal_contract_versions(text: str) -> list[int]:
    patterns = (
        r"\b[A-Z_]*CONTRACT_VERSION\s*=\s*(\d+)",
        r"[\"']contract_version[\"']\s*:\s*(\d+)",
        r"\bcontract_version\s*:\s*(\d+)",
        r"[\"']contract_version[\"'][^\n\d]{0,40}==\s*(\d+)",
    )
    return [
        int(value)
        for pattern in patterns
        for value in re.findall(pattern, text)
    ]


def _json_object_keys(source: AuthorityInput, spec: dict[str, Any]) -> set[str]:
    value = _json_pointer(source.json(spec["file"]), spec["path"])
    if not isinstance(value, dict):
        raise ValueError(f"JSON object not found: {spec['file']}#{spec['path']}")
    prefix = spec.get("prefix", "")
    return {f"{prefix}{key}" for key in value}


def _extract(source: AuthorityInput, spec: dict[str, Any]) -> set[str]:
    kind = spec["kind"]
    if kind == "schema_vocabulary":
        return _schema_vocabulary(source, spec)
    if kind == "markdown_table":
        return _markdown_table(source, spec)
    if kind == "markdown_field_names":
        return _markdown_field_names(source, spec)
    if kind == "marker":
        return _marker_values(source, spec)
    if kind == "marker_tail":
        return _marker_tail_values(source, spec)
    if kind == "python_literal_annotation":
        return _python_literal_annotation(source, spec)
    if kind == "python_string_assignment":
        return _python_string_assignment(source, spec)
    if kind == "typescript_string_union":
        return _typescript_string_union(source, spec)
    if kind == "json_object_keys":
        return _json_object_keys(source, spec)
    raise ValueError(f"unknown extractor: {kind}")


def _markdown_decision_tables(source: AuthorityInput, relative: str) -> set[str]:
    lines = source.text(relative).splitlines()
    headings: set[str] = set()
    current_heading: str | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("**") and "(authoritative and exhaustive" in stripped:
            current_heading = stripped[2:].split("**", 1)[0].split(" (", 1)[0]
            continue
        if not stripped.startswith("|"):
            continue
        headers = [cell.strip() for cell in stripped.strip("|").split("|")]
        if "Decision" in headers:
            if current_heading is None:
                raise ValueError(f"Decision table has no named heading in {relative}")
            headings.add(current_heading)
    return headings


def _mirror_findings(source: AuthorityInput, entry: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    def vocabulary(item: dict[str, Any], paths_key: str = "paths") -> set[Any]:
        paths = item.get(paths_key)
        if paths is None:
            paths = [item["path"]]
        return _schema_values(
            source,
            item["schema"],
            paths,
            exclude=item.get("exclude", ()),
        )

    def add(kind: str, **detail: Any) -> None:
        findings.append({"kind": kind, "domain": entry["id"], **detail})

    rule = entry["rule"]
    if rule == "equality":
        normalized: list[set[Any]] = []
        for item in entry["sets"]:
            actual = vocabulary(item)
            extras = set(item.get("extras", ()))
            if not extras <= actual:
                add("mirror_missing_extra", schema=item["schema"], values=sorted(extras - actual))
            normalized.append(actual - extras)
        if normalized and any(values != normalized[0] for values in normalized[1:]):
            add(
                "mirror_equality_drift",
                values=[sorted(values, key=repr) for values in normalized],
            )
        return findings

    if rule == "mapping":
        home = vocabulary(entry["home"])
        targets = entry.get("targets")
        if targets is None:
            targets = [{**entry["target"], "mapping": entry["mapping"]}]
        for item in targets:
            target = vocabulary(item)
            mapping = item["mapping"]
            if home != set(mapping):
                add("mirror_mapping_domain_drift", schema=item["schema"])
            if target != set(mapping.values()):
                add("mirror_mapping_range_drift", schema=item["schema"])
        return findings

    if rule == "partition":
        home = vocabulary(entry["home"])
        member = vocabulary(entry["member"])
        exclusions = set(entry["exclusions"])
        for item in entry["exclusion_sets"]:
            exclusions |= vocabulary(item)
        if member & exclusions:
            add("mirror_partition_overlap", values=sorted(member & exclusions))
        if home != member | exclusions:
            add("mirror_partition_drift")
        return findings

    if rule == "bijection":
        home = vocabulary(entry["home"])
        target: set[Any] = set()
        for item in entry["target_sets"]:
            target |= vocabulary(item)
        pairs = entry["pairs"]
        if not set(pairs) <= home:
            add("mirror_bijection_domain_drift")
        if len(set(pairs.values())) != len(pairs):
            add("mirror_bijection_not_injective")
        if target != set(pairs.values()):
            add("mirror_bijection_range_drift")
        return findings

    if rule == "projection":
        home = vocabulary(entry["home"])
        for item in entry["targets"]:
            target = vocabulary(item)
            if target != home - set(item["drop_from_home"]):
                add("mirror_projection_drift", schema=item["schema"])
        return findings

    add("unknown_mirror_rule", rule=rule)
    return findings


def _binding_lane_rows(source: AuthorityInput, check: dict[str, Any]) -> list[str]:
    lines = source.text(check["lane_table_file"]).splitlines()
    heading_index = next(
        index
        for index, line in enumerate(lines)
        if check["lane_table_heading"] in line
    )
    table_index = next(
        index
        for index in range(heading_index + 1, len(lines))
        if lines[index].startswith("| Lane |")
    )
    rows: list[str] = []
    for line in lines[table_index + 2 :]:
        if not line.startswith("|"):
            break
        if re.match(r"\| \*\*I[0-9]+\b", line):
            rows.append(line)
    return rows


def _python_importers(source: AuthorityInput, check: dict[str, Any]) -> set[str]:
    importers: set[str] = set()
    for path in source.glob("**/*.py"):
        relative = path.relative_to(source.root).as_posix()
        if any(fnmatch.fnmatch(relative, pattern) for pattern in check.get("exclude_globs", ())):
            continue
        try:
            tree = source.python_tree(relative, retain=False)
        except (SyntaxError, UnicodeDecodeError):
            continue
        if any(
            isinstance(node, ast.ImportFrom) and node.module == check["module"]
            for node in ast.walk(tree)
        ):
            importers.add(relative)
    return importers


def check(root: Path = ROOT) -> dict[str, Any]:
    source = AuthorityInput(root)
    root = source.root
    registry = source.json("docs/plans/model-hub-contracts/mirror-registry.json")
    findings: list[dict[str, Any]] = []

    version_closure = registry.get("contract_version_closure", {})
    terminal_version = registry.get("contract_version")
    if not isinstance(terminal_version, int):
        findings.append({"kind": "invalid_contract_version", "domain": "V1"})
    else:
        raw_persisted_schema_floors = version_closure.get(
            "persisted_schema_version_floors", {}
        )
        if not isinstance(raw_persisted_schema_floors, dict) or any(
            not isinstance(name, str)
            or not name
            or isinstance(floor, bool)
            or not isinstance(floor, int)
            or floor < 1
            or floor > terminal_version
            for name, floor in (
                raw_persisted_schema_floors.items()
                if isinstance(raw_persisted_schema_floors, dict)
                else ()
            )
        ):
            findings.append(
                {
                    "kind": "invalid_persisted_schema_version_floors",
                    "domain": "V1",
                }
            )
            persisted_schema_floors: dict[str, int] = {}
        else:
            persisted_schema_floors = raw_persisted_schema_floors
        persisted_schemas = set(persisted_schema_floors)
        checked_schemas: set[str] = set()
        contracts_dir = root / "docs/plans/model-hub-contracts"
        for path in sorted(contracts_dir.glob("*.schema.json")):
            relative = path.relative_to(root).as_posix()
            for node in _versioned_schema_nodes(source.json(relative)):
                checked_schemas.add(path.name)
                accepted = [node["const"]] if "const" in node else list(node.get("enum", ()))
                if accepted != sorted(set(accepted)) or not accepted:
                    findings.append(
                        {
                            "kind": "invalid_contract_version_set",
                            "domain": "V1",
                            "file": relative,
                            "values": accepted,
                        }
                    )
                    continue
                expected = (
                    list(
                        range(
                            persisted_schema_floors[path.name],
                            terminal_version + 1,
                        )
                    )
                    if path.name in persisted_schemas
                    else [terminal_version]
                )
                if accepted != expected:
                    findings.append(
                        {
                            "kind": "contract_version_schema_drift",
                            "domain": "V1",
                            "file": relative,
                            "values": accepted,
                            "expected": expected,
                        }
                    )
        for missing in sorted(persisted_schemas - checked_schemas):
            findings.append(
                {
                    "kind": "missing_persisted_version_schema",
                    "domain": "V1",
                    "file": missing,
                }
            )

        for path in sorted(contracts_dir.iterdir()):
            if not path.is_file() or path.name.endswith(".schema.json"):
                continue
            relative = path.relative_to(root).as_posix()
            for value in re.findall(
                r"contract_version[^0-9]{0,12}(\d+)",
                source.text(relative),
            ):
                if int(value) != terminal_version:
                    findings.append(
                        {
                            "kind": "contract_version_text_drift",
                            "domain": "V1",
                            "file": relative,
                            "value": int(value),
                        }
                    )

        required_literal_files = set(
            version_closure.get("required_literal_files", ())
        )
        matched_literal_files: set[str] = set()
        for pattern in version_closure.get("literal_globs", ()):
            for path in source.glob(pattern):
                if not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                values = _literal_contract_versions(source.text(relative))
                if values:
                    matched_literal_files.add(relative)
                for value in values:
                    if value != terminal_version:
                        findings.append(
                            {
                                "kind": "contract_version_literal_drift",
                                "domain": "V1",
                                "file": relative,
                                "value": value,
                            }
                        )
        for missing in sorted(required_literal_files - matched_literal_files):
            findings.append(
                {
                    "kind": "missing_contract_version_literal",
                    "domain": "V1",
                    "file": missing,
                }
            )

        for relative in version_closure.get("contract_headers", ()):
            match = re.search(
                r"FINAL CONTRACT v(\d+)",
                source.text(relative),
            )
            if match is None or int(match.group(1)) != terminal_version:
                findings.append(
                    {
                        "kind": "contract_version_header_drift",
                        "domain": "V1",
                        "file": relative,
                        "value": int(match.group(1)) if match else None,
                    }
                )

    # Absence assertions are discovered from the live normative text. The checker
    # never embeds a retired member; it derives each term from the assertion and
    # checks every contract artifact in the same invocation.
    for absence in registry.get("absence_checks", ()):
        text = source.text(absence["source_file"])
        for term in re.findall(absence["pattern"], text):
            for relative in absence["scope_files"]:
                if term in source.text(relative):
                    findings.append(
                        {"kind": "retired_contract_value", "domain": absence["id"], "file": relative, "value": term}
                    )

    for absence in registry.get("schema_absence_checks", ()):
        terms = _normative_absence_terms(source, absence)
        for pattern in absence["scope_globs"]:
            for path in source.glob(pattern):
                relative = path.relative_to(root).as_posix()
                tokens = _schema_decision_tokens(source, relative)
                for term in sorted(terms & tokens):
                    findings.append(
                        {
                            "kind": "retired_schema_token",
                            "domain": absence["id"],
                            "file": relative,
                            "value": term,
                        }
                    )

    for contract in registry.get("field_contracts", ()):
        properties = _schema_property_names(source, contract["schema_file"])
        claimed, negated = _markdown_row_contract(source, contract)
        for field in sorted(claimed - properties):
            findings.append(
                {"kind": "orphan_prose_field", "domain": contract["id"], "value": field}
            )
        for field in sorted(negated & properties):
            findings.append(
                {"kind": "forbidden_persisted_field", "domain": contract["id"], "value": field}
            )

    for boundary in registry.get("api_boundary_only_errors", ()):
        value = boundary["value"]
        if value not in source.text(boundary["contract_file"]):
            findings.append({"kind": "missing_boundary_error_contract", "value": value})
        try:
            test_literals = _python_test_literals(
                source,
                boundary["negative_test_file"],
                boundary["negative_test"],
            )
        except (OSError, SyntaxError, ValueError) as exc:
            findings.append(
                {"kind": "input_error", "domain": "boundary_error", "detail": str(exc)}
            )
        else:
            if value not in test_literals:
                findings.append(
                    {
                        "kind": "boundary_error_test_does_not_assert_value",
                        "value": value,
                        "test": boundary["negative_test"],
                    }
                )
        for pattern in boundary["forbidden_ui_globs"]:
            for path in source.glob(pattern):
                relative = path.relative_to(root).as_posix()
                if value in source.text(relative):
                    findings.append(
                        {"kind": "boundary_error_has_ui_consumer", "value": value, "file": relative}
                    )

    for absence in registry.get("repo_absence_checks", ()):
        term = "-".join(absence["term_parts"])
        for pattern in absence["scope_globs"]:
            for path in source.glob(pattern):
                if not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                if term in source.text(relative):
                    findings.append(
                        {
                            "kind": "retired_repo_literal",
                            "domain": absence["id"],
                            "file": relative,
                            "value": term,
                        }
                    )

    for route_check in registry.get("route_body_absence_checks", ()):
        matching_rows = [
            line
            for line in source.text(route_check["contract_file"]).splitlines()
            if line.startswith("|") and route_check["path_fragment"] in line
        ]
        if not matching_rows:
            findings.append({"kind": "missing_route_family", "domain": route_check["id"]})
            continue
        for row in matching_rows:
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            request = cells[1].split("→", 1)[0]
            if re.search(rf"\b{re.escape(route_check['forbidden_field'])}\b", request):
                findings.append(
                    {
                        "kind": "route_body_contains_path_identity",
                        "domain": route_check["id"],
                        "value": route_check["forbidden_field"],
                        "row": row,
                    }
                )

    registry_ids = [
        *(entry["id"] for entry in registry["entries"]),
        *(entry["id"] for entry in registry["decision_tables"]),
        *(entry["id"] for entry in registry.get("ownership_checks", ())),
        *(entry["id"] for entry in registry.get("file_mirrors", ())),
    ]
    if len(registry_ids) != len(set(registry_ids)):
        findings.append({"kind": "duplicate_registry_id", "ids": registry_ids})

    registered_tables: dict[str, set[str]] = {}
    for decision in registry["decision_tables"]:
        authority = decision["authority"]
        if authority["kind"] == "markdown_table":
            registered_tables.setdefault(authority["file"], set()).add(authority["heading"])
    for relative in registry["authority_table_discovery"]["files"]:
        try:
            discovered = _markdown_decision_tables(source, relative)
        except (OSError, ValueError) as exc:
            findings.append({"kind": "input_error", "domain": "table_discovery", "detail": str(exc)})
            continue
        registered = registered_tables.get(relative, set())
        for heading in sorted(discovered - registered):
            findings.append({"kind": "unregistered_authority_table", "file": relative, "heading": heading})
        for heading in sorted(registered - discovered):
            findings.append({"kind": "missing_authority_table", "file": relative, "heading": heading})

    for entry in registry["entries"]:
        try:
            findings.extend(_mirror_findings(source, entry))
        except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
            findings.append({"kind": "input_error", "domain": entry["id"], "detail": str(exc)})

    for decision in registry["decision_tables"]:
        try:
            authority = _extract(source, decision["authority"])
            consumer_sets = [_extract(source, item) for item in decision["consumers"]]
        except (KeyError, OSError, ValueError, json.JSONDecodeError, SyntaxError) as exc:
            findings.append(
                {"kind": "input_error", "domain": decision["id"], "detail": str(exc)}
            )
            continue
        consumers = set().union(*consumer_sets)
        for value in sorted(consumers - authority):
            findings.append(
                {"kind": "orphan_branch", "domain": decision["id"], "value": value}
            )
        for value in sorted(authority - consumers):
            findings.append(
                {"kind": "orphan_row", "domain": decision["id"], "value": value}
            )
        if decision.get("authority_fields"):
            fields = _extract(source, decision["authority_fields"])
            contract_text = "\n".join(
                source.text(relative)
                for relative in decision["authority_fields_scope"]
            )
            for field in sorted(fields):
                if field not in contract_text:
                    findings.append(
                        {"kind": "orphan_authority_field", "domain": decision["id"], "value": field}
                    )

    for ownership in registry.get("ownership_checks", ()):
        if ownership["kind"] != "python_importers_have_exactly_one_lane":
            findings.append(
                {"kind": "unknown_ownership_check", "domain": ownership["id"]}
            )
            continue
        rows = _binding_lane_rows(source, ownership)
        for path in sorted(_python_importers(source, ownership)):
            owners = [row for row in rows if f"`{path}`" in row]
            if len(owners) != 1:
                findings.append(
                    {
                        "kind": "ownership_cardinality",
                        "domain": ownership["id"],
                        "path": path,
                        "owners": len(owners),
                    }
                )

    for mirror in registry.get("file_mirrors", ()):
        left = source.bytes(mirror["left"])
        right = source.bytes(mirror["right"])
        if left != right:
            findings.append({"kind": "file_mirror_drift", "domain": mirror["id"]})

    return {
        "ok": not findings,
        "input_mode": registry["input_generation"]["mode"],
        "input_fingerprint": source.fingerprint(),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = check(args.root.resolve())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    elif result["ok"]:
        print(f"Model Hub authority closure OK ({result['input_fingerprint']})")
    else:
        for finding in result["findings"]:
            print(json.dumps(finding, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
