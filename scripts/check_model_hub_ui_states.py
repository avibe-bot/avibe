#!/usr/bin/env python3
"""State-completeness gate for docs/plans/model-hub-ui-spec.md.

The spec grew one frame at a time, in prose, and "is this state finished?" was a
question only a reviewer could answer. §0.8 turns the states into a table; this
script turns the table into a set of assertions a machine can check.

It regenerates its input from the live document in the same run it reports —
pass a path, or a git rev that is read through `git show`. It never consumes a
snapshot committed beside it, because a snapshot can agree with a checker while
both disagree with the file everyone else reads.

Four gap classes, each a set computed from the text:

  A  a mutating call §1 names that no §0.8 row states a treatment for, or a
     §0.8 row whose failure cell is empty or names a treatment that does not exist
  B  a copy key cited but never defined, a key row with no English column, or a
     `{{slot}}` with no §0.9 row
  C  a §0.8 row with no exit, or a frame section that draws an element inventory
     and contributes no §0.8 row
  D  a copy key whose name declares a condition that no §0.8 row cites

The input scale is reported before the verdict. What this gate claims is exactly
that those four sets are empty — not that the document is complete, not that the
copy is right. A gate that claims more than its extractors can see reports green
where it should report an error, so the extractors' reach is printed with the
result rather than left in a comment.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SPEC = Path("docs/plans/model-hub-ui-spec.md")

ROUTE_RE = re.compile(r"`(POST|PUT|PATCH|DELETE) (/api/[^`]*)`")
ANY_ROUTE_RE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/api/[^`]*)`")


def normalize_route(method: str, path: str) -> str:
    """`METHOD /path`, query string dropped, for comparison by equality.

    The register, §1 and `api.md` name one endpoint at three grains — the
    contract writes `PUT .../chain?model=<id>` where a state row writes
    `PUT .../chain` — and a query string is an argument to a route, not a
    different route. Everything to the left of `?` then has to match character
    for character. The earlier class A asked whether the route *appeared as a
    substring* of the register's concatenated cells, which answers yes for
    every prefix: `POST /api/models/runtime/start` is a substring of a row
    naming `/api/models/runtime/startup`, so the one mutation that matters —
    a register row drifting off the route it governs — reported green.

    Path parameters collapse to `<>`, because their spelling is not part of a
    route's identity and `api.md` proves it: the same Source id is `<id>` on the
    refresh row and `<source_id>` one row below, on `/sources/<source_id>/models`.
    A comparison that kept the names would split one endpoint into two and then
    report the register as missing a row it does in fact have. Every literal
    segment still has to match character for character, and the segment count
    still has to agree, so the mutation this exists to catch stays caught.
    """
    path = re.sub(r"<[^>/]+>", "<>", path.split("?")[0].rstrip("/"))
    return f"{method} {path}"
KEY_REF_RE = re.compile(r"`([a-z][A-Za-z0-9]*(?:\.[A-Za-z0-9_*]+)+)`")
KEY_DEF_RE = re.compile(r"^\|\s*`([a-z][A-Za-z0-9._*]*)`([^|]*)\|([^|]*)\|([^|]*)\|\s*$")
SLOT_RE = re.compile(r"\{\{(\w+)\}\}")
TREAT_RE = re.compile(r"\bF([1-5])\b")
GOTO_RE = re.compile(r"→\s*([^,;.]+)")

# A key whose name declares a condition. Deliberately narrow: every member is a
# word the document uses to mean "something is wrong, missing, or pending", and
# the size of the matched set is reported so the claim stays bounded.
CONDITION_RE = re.compile(
    r"(?:^|\.)(?:fail\w*|error|empty\w*|unavailable|undetermined|unsupported"
    r"|notInstalled|notStarted|degraded|stopped|starting|adding|progress"
    r"|needsAction|credentialInvalid|cooldown|interrupted|alreadyBound"
    r"|engineDown|oauthFailed|retry|emptyNeverFetched|noEligible|noSource)"
    r"(?:_one|_other)?(?:\.|$)"
)

SECTION_RE = re.compile(r"^### (\d+\.\d+)\b")

# --- class E: what the spec asserts about the system, and who owns each fact ---
#
# The four classes above read the document against itself. They cannot see the
# defect that produced most of one review round: a sentence that restates an
# authority — a route, a request body, a status branch, a schema's vocabulary, a
# backend symbol — and restates it wrongly, or restates it correctly and then
# goes stale when the authority moves. Every one of those is a literal, and a
# literal can be compared. What cannot be compared is a restatement with no
# binding, so an unbound literal is itself the finding: the document has to name
# the route a body belongs to before anyone, machine or reviewer, can say the
# body is right.
CONTRACTS = Path("docs/plans/model-hub-contracts")
API_CONTRACT = CONTRACTS / "api.md"

API_ROW_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+`([^`]+)`$")
BODY_RE = re.compile(r"`(\{[^`{}]*\})`")
JSONISH_RE = re.compile(r"\{[^{}]*\}")
SCHEMA_CITE_RE = re.compile(r"`([a-z][a-z-]*\.schema\.json)`")
DOTTED_TOKEN_RE = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)`")
PY_CITE_RE = re.compile(r"`([A-Za-z0-9_./-]+\.py)(?::([A-Za-z_][A-Za-z0-9_]*))?`")
STATUS_RE = re.compile(r"\b(4\d\d|5\d\d)\b")
COUNT_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15,
}
COUNT_CLAIM_RE = re.compile(
    r"\b(" + "|".join(COUNT_WORDS) + r")\s+(?:\w+\s+)?(properties|values|transports)\b"
)


def literal_keys(text: str) -> set[str]:
    """The key names a `{...}` literal declares, types and values discarded."""
    keys: set[str] = set()
    for block in JSONISH_RE.findall(text):
        for part in block.strip("{}").split(","):
            name = part.split(":")[0].strip().strip('"').rstrip("?").strip()
            if re.fullmatch(r"[a-z_][A-Za-z0-9_]*", name):
                keys.add(name)
    return keys


def schema_vocabulary(node: Any) -> set[str]:
    """Every property name and every enum/const string a schema admits."""
    found: set[str] = set()
    if isinstance(node, dict):
        for name, child in node.items():
            if name == "properties" and isinstance(child, dict):
                found.update(child)
            if name in ("enum", "const"):
                values = child if isinstance(child, list) else [child]
                found.update(v for v in values if isinstance(v, str))
            found |= schema_vocabulary(child)
    elif isinstance(node, list):
        for child in node:
            found |= schema_vocabulary(child)
    return found


def enum_paths(
    node: Any, path: str = "", found: dict[str, dict[str, set[str]]] | None = None
) -> dict[str, dict[str, set[str]]]:
    """Every enum-carrying field, keyed by bare name and then by where it lives.

    Keeping the path is the point. `agent-supply.schema.json` declares
    `supply_status` twice — once at the top level, where it is the backend's
    rollup for the pinned model, and once inside `named_agents[]`, where it is
    one Agent's own rollup — and the two hold independently. A gate that unions
    them by name sees one field with one vocabulary and cannot tell a sentence
    about the first from a sentence about the second, which is exactly the
    substitution one review round found. So the vocabularies stay separated by
    path, and a document that cites the bare name where the authority declares
    two is reported as ambiguous rather than silently resolved.

    `null` is carried as the string `"null"`: it is a value the field admits and
    a value the document renders, and dropping it would make every total
    rendering of a nullable field look like it drew one row too many.
    """
    found = {} if found is None else found
    if isinstance(node, dict):
        values = node.get("enum")
        if isinstance(values, list) and path and "." in path:
            members = {v if isinstance(v, str) else "null" for v in values if isinstance(v, str) or v is None}
            if members:
                found.setdefault(path.split(".")[-1], {}).setdefault(path, set()).update(members)
        for key, child in node.items():
            if key == "properties" and isinstance(child, dict):
                for prop, sub in child.items():
                    enum_paths(sub, f"{path}.{prop}" if path else prop, found)
            elif key == "items":
                enum_paths(child, f"{path}[]" if path else path, found)
            else:
                enum_paths(child, path if key in ("anyOf", "oneOf", "allOf", "then", "else") else "", found)
    elif isinstance(node, list):
        for child in node:
            enum_paths(child, path, found)
    return found


def enum_fields(node: Any) -> dict[str, set[str]]:
    """`enum_paths` flattened by name, for the checks that only count values."""
    return {name: set().union(*paths.values()) for name, paths in enum_paths(node).items()}


def load_authorities(root: Path) -> dict[str, Any]:
    """Read the authority files live, in this run, from `root`.

    The spec may be read from a git revision; the authorities never are. They
    are the files everyone else edits, and the question this class answers is
    whether the spec agrees with *them*, not with a copy of them taken at the
    same moment the spec was written.
    """
    api_text = (root / API_CONTRACT).read_text(encoding="utf-8")
    routes: dict[str, dict[str, Any]] = {}
    table_spans: list[str] = []
    for line in api_text.split("\n"):
        if not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if len(cells) < 2:
            continue
        m = API_ROW_RE.match(cells[0])
        if not m:
            continue
        table_spans.append(line)
        routes[normalize_route(m.group(1), m.group(2))] = {
            "keys": literal_keys(cells[1]),
            "guarded": "guarded" in cells[1].lower() or "force" in cells[1],
            "cell": cells[1],
        }
    # The shared envelopes are defined in prose and in the JSON examples, and a
    # guarded route's row names them rather than spelling them out. Their
    # vocabulary is collected from everything that is not a route row, so a
    # route's own request shape can never leak into another route's allowance.
    outside = "\n".join(l for l in api_text.split("\n") if l not in table_spans)
    envelope = literal_keys(outside)

    schemas: dict[str, set[str]] = {}
    properties: dict[str, set[str]] = {}
    enums: dict[str, dict[str, set[str]]] = {}
    paths: dict[str, dict[str, dict[str, set[str]]]] = {}
    for path in sorted((root / CONTRACTS).glob("*.schema.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        schemas[path.name] = schema_vocabulary(doc)
        properties[path.name] = set(doc.get("properties", {}))
        enums[path.name] = enum_fields(doc)
        paths[path.name] = enum_paths(doc, doc.get("title", path.stem))
    return {
        "routes": routes,
        "envelope": envelope,
        "schemas": schemas,
        "properties": properties,
        "enums": enums,
        "enum_paths": paths,
    }


def defined_symbols(path: Path) -> set[str]:
    """Top-level and class-level names a Python file defines."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


def claim_scopes(lines: list[str]) -> list[tuple[int, str]]:
    """Split the document into the units a claim is read in.

    A table row is its own scope. Everything else is its paragraph. Without the
    first half the §0.8 register — several hundred contiguous rows — reads as
    one scope, and every route in it would vouch for every body literal in it.
    """
    scopes: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("|"):
            if buffer:
                scopes.append((start + 1, "\n".join(buffer)))
                buffer = []
            scopes.append((i + 1, line))
            continue
        if not line.strip():
            if buffer:
                scopes.append((start + 1, "\n".join(buffer)))
                buffer = []
            continue
        if not buffer:
            start = i
        buffer.append(line)
    if buffer:
        scopes.append((start + 1, "\n".join(buffer)))
    return scopes


def read_source(target: str) -> tuple[str, str, str]:
    """Return (text, mode, fingerprint). Always read in this run, never cached.

    `target` is a file on disk or a git revision, and which one it is comes from
    the filesystem rather than from the target's spelling. An earlier version
    decided by spelling — `:` in it, or a bare 7–40 hex run — which meant `HEAD`,
    a branch name and a tag were all treated as paths and died on `[Errno 2]`,
    a message that reads as *the file moved* when what happened is *the revision
    was never resolved*. A gate that cannot be pointed at a named revision is a
    gate nobody runs against the head under review.

    A name that is both a readable file and a valid ref reads as the file: that
    is the copy being edited, and it is the one whose result the author needs.
    A target that is neither fails loudly and says which two things it is not.
    """
    for candidate in (Path(target), ROOT / target):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            return text, "same_run_live_files", hashlib.sha256(text.encode()).hexdigest()[:16]
    rev = target if ":" in target else f"{target}:{SPEC}"
    proc = subprocess.run(["git", "show", rev], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"{target!r} is neither a readable file nor a git revision holding {SPEC}\n"
            f"  git show {rev}: {proc.stderr.strip()}"
        )
    return proc.stdout, "same_run_git_rev", hashlib.sha256(proc.stdout.encode()).hexdigest()[:16]


def parse(text: str) -> dict[str, Any]:
    lines = text.split("\n")

    # --- section spans -------------------------------------------------------
    spans: list[tuple[str, int, int]] = []
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if m:
            if spans:
                spans[-1] = (spans[-1][0], spans[-1][1], i)
            spans.append((m.group(1), i, len(lines)))

    def section_of(idx: int) -> str:
        for name, a, b in spans:
            if a <= idx < b:
                return name
        return "0"

    # --- §0.8 register -------------------------------------------------------
    register: list[dict[str, Any]] = []
    in_reg = False
    for i, line in enumerate(lines):
        if line.startswith("### 0.8"):
            in_reg = True
            continue
        if in_reg and line.startswith("### "):
            in_reg = False
        if not in_reg or not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if len(cells) != 6 or cells[0] in ("Frame", "---") or cells[1] == "---":
            continue
        register.append(
            {
                "line": i + 1,
                "frame": cells[0],
                "state": cells[1],
                "entry": cells[2],
                "failure": cells[3],
                "copy": cells[4],
                "exit": cells[5],
            }
        )

    # --- §0.9 slots ----------------------------------------------------------
    slots: set[str] = set()
    in_slots = False
    for line in lines:
        if line.startswith("### 0.9"):
            in_slots = True
            continue
        if in_slots and line.startswith("### "):
            in_slots = False
        if in_slots and line.startswith("| `{{"):
            slots.update(SLOT_RE.findall(line.split("|")[1]))

    # --- copy tables ---------------------------------------------------------
    # A copy row counts only inside a copy table, which starts at its own
    # `| Key | 中文 | English |` header and ends with the table. Other tables in
    # this document also open with a backticked token — contract enum values,
    # ink names, schema fields — and reading those as key definitions is how a
    # checker invents keys nobody wrote.
    defined: dict[str, dict[str, str]] = {}
    namespaces: set[str] = set()
    tables = 0
    rows = 0
    ns = ""
    pending_ns = ""
    in_table = False
    for i, line in enumerate(lines):
        if line.startswith("**Copy**"):
            m = re.search(r"`models\.hub\.([a-zA-Z]+)\.\*`", line)
            pending_ns = m.group(1) if m else ""
            continue
        if line.startswith("| Key |"):
            tables += 1
            in_table = True
            ns = pending_ns
            pending_ns = ""
            if ns:
                namespaces.add(ns)
            continue
        if in_table and not line.startswith("|"):
            in_table = False
            ns = ""
            continue
        if not in_table:
            continue
        m = KEY_DEF_RE.match(line)
        if not m:
            continue
        key, zh, en = m.group(1), m.group(3).strip(), m.group(4).strip()
        rows += 1
        if not ns:
            namespaces.add(key.split(".")[0])
        for spelling in {key, f"{ns}.{key}" if ns else key}:
            defined[spelling] = {"zh": zh, "en": en, "line": i + 1, "raw": key}

    # A citation is a copy key only when its first segment is a namespace some
    # copy table declares. That set is extracted in the same pass, so `api.md`,
    # `source.schema.json` and `status.health` fall out by construction rather
    # than through a hand-maintained deny-list. The cost is that a key named by
    # a bare suffix in prose is not scanned at all; the document is written to
    # cite keys by their namespace outside their own table, which is also what
    # keeps such a citation from going stale unnoticed.

    # --- prose key references + routes, per section --------------------------
    refs: list[tuple[str, int, str]] = []
    routes: list[tuple[str, int, str]] = []
    inventories: set[str] = set()
    reg_start = next((i for i, l in enumerate(lines) if l.startswith("### 0.8")), 0)
    reg_end = next((i for i, l in enumerate(lines) if l.startswith("### 0.9")), 0)
    for i, line in enumerate(lines):
        if reg_start <= i < reg_end:
            continue
        sec = section_of(i)
        if not sec.startswith("1."):
            continue
        if line.startswith("**Element inventory**"):
            inventories.add(sec)
        for k in KEY_REF_RE.findall(line):
            head = k.split(".")[0]
            if head == "models" and k.startswith("models.hub."):
                refs.append((sec, i + 1, k))
            elif head in namespaces:
                refs.append((sec, i + 1, k))
        for meth, path in ROUTE_RE.findall(line):
            # Normalized on the way in, because class A compares this against a
            # register side that is normalized too, and two sides normalized by
            # different rules is not a comparison — it reports every route as
            # uncovered the moment one side spells a parameter the other way.
            routes.append((sec, i + 1, normalize_route(meth, path)))

    return {
        "register": register,
        "slots": slots,
        "defined": defined,
        "tables": tables,
        "rows": rows,
        "refs": refs,
        "routes": routes,
        "inventories": inventories,
        "namespaces": namespaces,
        "sections": [s for s, _, _ in spans if s.startswith("1.")],
    }


def resolves(key: str, defined: dict[str, Any]) -> bool:
    """True when `key` names a defined copy row.

    This is the gate's one permissive point, and it is permissive in exactly two
    ways, both stated rather than hidden. A key may be written with or without
    its `models.hub.` prefix, and prose may name a key by a unique suffix of it
    (`status.ok` for `gateway.group.status.ok`) because that is how the document
    reads when the namespace is obvious from the paragraph. A suffix that is not
    unique does not resolve.
    """
    if key in defined:
        return True
    bare = key[len("models.hub.") :] if key.startswith("models.hub.") else key
    if bare in defined:
        return True
    for cand in (key, bare):
        if cand.endswith("*") and any(d.startswith(cand.rstrip("*.")) for d in defined):
            return True
        if f"{cand}_one" in defined and f"{cand}_other" in defined:
            return True
        matches = {d for d in defined if d.endswith("." + cand)}
        if len({defined[m]["line"] for m in matches}) == 1:
            return True
        plural = {d for d in defined if d.endswith("." + cand + "_one")}
        if len(plural) == 1:
            return True
    return False


MAPPING_HEADER_RE = re.compile(r"^\|\s*`([A-Za-z][A-Za-z0-9_.\[\]]*)`(?:\s*`\[[a-z-]+\]`)*\s*\|")
SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|$")


def mapping_tables(lines: list[str]) -> list[tuple[int, str, set[str]]]:
    """Tables that render one named field totally, one row per value.

    The document's own convention, used wherever a field's whole vocabulary has
    to reach the screen: the first header cell is the backticked field name, and
    every data row's first cell is one value of it. That is a set equality with
    an authority on one side, written by an author who cannot see the authority
    while writing it.

    Three things have to be true at once, or the shape is something else: the
    header cell is a bare backticked name, a separator row follows, and at least
    one data row leads with a backticked token. The last condition is what keeps
    the fixture table at §1.3 out — it is headed `` `models` `` but its rows lead
    with counts, so it renders arithmetic, not a vocabulary.

    The domain is read as a set rather than counted, because a value may
    legitimately occupy two rows (one value, two renderings) and a count would
    call that a drift.
    """
    found: list[tuple[int, str, set[str], set[str]]] = []
    for i, line in enumerate(lines):
        m = MAPPING_HEADER_RE.match(line)
        if not m or i + 1 >= len(lines) or not SEPARATOR_RE.match(lines[i + 1].strip()):
            continue
        values: set[str] = set()
        for row in lines[i + 2 :]:
            if not row.startswith("|"):
                break
            first = row.strip().strip("|").split("|")[0]
            values.update(DOTTED_TOKEN_RE.findall(first))
        if not values:
            continue
        # The lead-in paragraph is where these tables say whose field this is —
        # "`runtime-dependency.schema.json` enumerates five values", and then the
        # table. Reading it keeps the gate from calling a bound table ambiguous,
        # and leaves the ambiguity finding for the case that earns it: a file
        # that declares the same name twice.
        lead: list[str] = []
        for above in reversed(lines[:i]):
            if not above.strip():
                if lead:
                    break
                continue  # the blank line every table is separated from its lead-in by
            lead.append(above)
        found.append((i + 1, m.group(1), values, set(SCHEMA_CITE_RE.findall("\n".join(lead)))))
    return found


GAP_REF_RE = re.compile(r"\[contract-gap\]`?\s*`?(G-\d+)")
GAP_ROW_RE = re.compile(r"^\|\s*(G-\d+)\s*\|", re.M)


def registered_gaps(text: str) -> set[str]:
    """The gap numbers §0.5 actually carries a row for.

    `[contract-gap]` is the document's one way to say "this surface has no
    backend behind it", and it is also the one way to tell a checker not to ask
    for something that cannot exist. Both readings are needed, which makes the
    marker a silencer, and a silencer that costs a keystroke silences things
    nobody meant to. Requiring the marker to name a number, and the number to
    resolve to a §0.5 row, prices it at what it should cost: a stated surface, a
    stated missing behaviour, and evidence verified against a named commit. A
    bare `[contract-gap]`, or one citing a number no row defines, exempts
    nothing — the claim is checked as if the marker were not there.
    """
    return set(GAP_ROW_RE.findall(text))


# A class whose correct value is zero cannot use "empty means the extractor
# broke" as its guard — for these, empty is the goal. The document's own rule is
# §1.2: state decisions, never restate facts, so every counted restatement of a
# vocabulary is meant to be deleted rather than corrected, and the count is meant
# to reach 0 and stay there. What that costs is the free signal the other
# inventories get: a silently broken extractor reports the same 0 as a clean
# document. So each target-zero class pays for its 0 with a fixture that must
# still be caught, and one that must still pass — the arm proves it can fail and
# proves it can succeed, and only then does 0 on the real document mean anything.
TARGET_ZERO = {"authority: counted-vocabulary claims"}

SELF_TEST = [
    # (fixture, must the extractor count it, must it report a finding)
    ("`agent-supply.schema.json`'s `supply_status` enumerates six values.", True, True),
    ("`agent-supply.schema.json`'s `supply_status` enumerates five values.", True, False),
]


def self_test(auth: dict[str, Any], root: Path) -> list[str]:
    """Prove the target-zero arms still fire before believing their zeros."""
    broken: list[str] = []
    for fixture, want_counted, want_finding in SELF_TEST:
        found, scale = authority_claims(fixture, auth, root, [])
        counted = scale["vocabulary claims"] > 0
        reported = any(f["class"] == "E" and "not" in f["message"] for f in found)
        if counted is not want_counted or reported is not want_finding:
            broken.append(
                f"counted-vocabulary arm: {fixture!r} -> counted={counted} reported={reported}, "
                f"expected counted={want_counted} reported={want_finding}"
            )
    return broken


def authority_claims(text: str, auth: dict[str, Any], root: Path, register: list[dict[str, Any]]) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Class E — every factual claim the spec makes, against the file that owns it.

    Returns the findings and the input scale. The scale is not decoration: this
    class can only check claims that are written as literals and bound to an
    authority, so the honest report of its reach is *how many claims it found*,
    printed next to how many it rejected.
    """
    findings: list[dict[str, str]] = []
    scale = {"routes": 0, "bodies": 0, "status branches": 0, "schema citations": 0,
             "vocabulary claims": 0, "contract mapping tables": 0, "repo symbols": 0}

    def add(where: str, msg: str) -> None:
        findings.append({"class": "E", "where": where, "message": msg})

    # Every key `api.md` contracts anywhere, on any route. A body key drawn from
    # this set is never a hypothesis about missing behaviour — it exists, it is
    # simply written on the wrong row — so a registered gap cannot excuse it.
    every_contracted_key: set[str] = set(auth["envelope"])
    for row in auth["routes"].values():
        every_contracted_key |= row["keys"]

    guarded_named: dict[str, int] = {}
    for line_no, scope in claim_scopes(text.split("\n")):
        # A §0.5 row is the registration itself, so it needs no reference to
        # one: its whole job is to describe a behaviour the contract does not
        # have, and describing that behaviour means naming the branch that is
        # missing. Every other scope has to point at a row that exists.
        exempt = bool(GAP_ROW_RE.match(scope)) or bool(
            registered_gaps(text) & set(GAP_REF_RE.findall(scope))
        )
        named = {normalize_route(m, p) for m, p in ANY_ROUTE_RE.findall(scope)}
        for route in sorted(named):
            scale["routes"] += 1
            if route not in auth["routes"]:
                add(f"L{line_no}", f"`{route}` is contracted by no `api.md` route row")
            elif auth["routes"][route]["guarded"]:
                guarded_named.setdefault(route, line_no)

        for literal in BODY_RE.findall(scope):
            scale["bodies"] += 1
            keys = literal_keys(literal)
            if not keys:
                continue
            if not named:
                # A gap row describes a body for a route that does not exist, so
                # there is nothing to bind it to and nothing to compare. Anywhere
                # else, an unbound body is a claim no reader can verify.
                if not exempt:
                    add(f"L{line_no}", f"`{literal}` names no route — an unbound body claim cannot be checked")
                continue
            allowed: set[str] = set()
            for route in named:
                row = auth["routes"].get(route)
                if row:
                    allowed |= row["keys"] | (auth["envelope"] if row["guarded"] else set())
            # A registered gap forgives a field the contract does not have
            # anywhere — that is what the gap *is*, a behaviour the document
            # states and no route carries yet. It does not forgive a field some
            # other route does carry: `{hops}` on the order save is not a
            # missing behaviour, it is the per-model chain's body written on the
            # wrong route, and the review that found it was reading by hand.
            stray = sorted(k for k in keys - allowed if not (exempt and k not in every_contracted_key))
            if stray:
                add(
                    f"L{line_no}",
                    f"`{literal}` names {', '.join(stray)} — not contracted for "
                    f"{' / '.join(sorted(named))}",
                )

        if named and not exempt:
            for code in set(STATUS_RE.findall(scope)):
                if code != "409":
                    continue
                scale["status branches"] += 1
                unguarded = sorted(r for r in named if r in auth["routes"] and not auth["routes"][r]["guarded"])
                if unguarded:
                    add(f"L{line_no}", f"a 409 branch is claimed for {' / '.join(unguarded)}, which `api.md` does not guard")

        for schema in SCHEMA_CITE_RE.findall(scope):
            scale["schema citations"] += 1
            if schema not in auth["schemas"]:
                add(f"L{line_no}", f"`{schema}` is not a file in {CONTRACTS}")
                continue
            claim = COUNT_CLAIM_RE.search(scope)
            if not claim:
                continue
            scale["vocabulary claims"] += 1
            want = COUNT_WORDS[claim.group(1)]
            tokens = {t.split(".")[-1] for t in DOTTED_TOKEN_RE.findall(scope)}
            if claim.group(2) == "properties":
                sets = {schema: auth["properties"][schema]}
            else:
                fields = auth["enums"][schema]
                sets = {name: values for name, values in fields.items() if name in tokens}
                if not sets:
                    add(
                        f"L{line_no}",
                        f"a claim of {want} values cites `{schema}` and names none of its "
                        f"{len(fields)} enum fields — the set being counted is not stated",
                    )
                    continue
            noun = claim.group(2)
            for name, values in sorted(sets.items()):
                listed = tokens & values
                if len(values) != want:
                    add(f"L{line_no}", f"`{name}` has {len(values)} {noun}, not {want}")
                elif listed and listed != values:
                    add(
                        f"L{line_no}",
                        f"`{name}` is enumerated here as {sorted(listed)}, "
                        f"against {sorted(values)}",
                    )

        for rel, symbol in PY_CITE_RE.findall(scope):
            scale["repo symbols"] += 1
            path = root / rel
            if not path.is_file():
                add(f"L{line_no}", f"`{rel}` is not a file in this repository")
            elif symbol and symbol not in defined_symbols(path):
                add(f"L{line_no}", f"`{rel}` defines no `{symbol}`")

    # A route `api.md` guards can be refused. The register is where a refusal
    # becomes a state with copy and an exit, so the question is not whether the
    # word `force` appears somewhere in 3000 lines — it is whether the rows that
    # govern this route state the branch. Anything looser passes on a mention in
    # a gap table and leaves the state unwritten, which is the shape this found.
    for route in sorted(guarded_named):
        rows = [
            r
            for r in register
            if route
            in {
                normalize_route(m, p)
                for m, p in ANY_ROUTE_RE.findall(f"{r['entry']} {r['exit']} {r['failure']}")
            }
        ]
        if not rows:
            continue
        if not any(re.search(r"force|409|guard|拒绝", f"{r['failure']} {r['entry']} {r['exit']}", re.I) for r in rows):
            add(
                f"L{rows[0]['line']}",
                f"`{route}` is guarded in `api.md`; none of its {len(rows)} §0.8 row(s) states the refusal branch",
            )

    # A table that renders a field totally is a set equality against a schema,
    # and the author writing it cannot see the schema. Three ways it goes wrong,
    # in the order they have to be asked: the field may not be contracted at all
    # (then the table is drawing something the spec itself decided, and saying
    # so is the fix); the bare name may resolve to more than one declaration
    # (then the table has not said which one it renders, and a per-Agent rollup
    # reads as a per-backend one); only after both does the row set mean
    # anything. Values are compared as sets — not counted — because one value
    # may legitimately occupy two rows.
    for line_no, field, drawn, cited in mapping_tables(text.split("\n")):
        scale["contract mapping tables"] += 1
        owners = {
            path: values
            for name, decls in auth["enum_paths"].items()
            if not cited or name in cited
            for path, values in decls.get(field.split(".")[-1], {}).items()
            if path == field or path.endswith(f".{field}")
        }
        if not owners:
            continue
        if len(owners) > 1:
            add(
                f"L{line_no}",
                f"`{field}` is rendered totally here, and its authority declares it at {len(owners)} "
                f"independent places ({', '.join(sorted(owners))}); the table names none of them, "
                f"so a reading of either satisfies the sentence",
            )
            continue
        origin, values = next(iter(owners.items()))
        if drawn != values:
            add(
                f"L{line_no}",
                f"`{field}` renders {sorted(drawn)} against `{origin}`'s {sorted(values)} — "
                f"missing {sorted(values - drawn)}, extra {sorted(drawn - values)}",
            )

    return findings, scale


def check(target: str | Path = SPEC) -> dict[str, Any]:
    """`target` is the spec path, a git rev, or a repo root holding the spec."""
    candidate = Path(target)
    if candidate.is_dir():
        target = candidate / SPEC
    text, mode, fingerprint = read_source(str(target))
    p = parse(text)
    reg = p["register"]
    findings: list[dict[str, str]] = []

    states = {r["state"] for r in reg}
    reg_frames = {r["frame"].lstrip("§") for r in reg}
    reg_by_section: dict[str, list[dict[str, Any]]] = {}
    for r in reg:
        reg_by_section.setdefault(r["frame"].lstrip("§"), []).append(r)

    def add(cls: str, where: str, msg: str) -> None:
        findings.append({"class": cls, "where": where, "message": msg})

    # ---- A ------------------------------------------------------------------
    covered_routes: set[str] = set()
    for r in reg:
        cells = f"{r['entry']} {r['exit']} {r['failure']}"
        covered_routes.update(normalize_route(m, path) for m, path in ROUTE_RE.findall(cells))
    # A route the document names is normally an affordance, and an affordance
    # owes §0.8 a state. The exception is a route named as *evidence of its own
    # absence* — "the contract has this call and no frame draws it" — where
    # demanding a state row would make the document invent a screen for
    # something the product does not do. That exception is exactly a registered
    # gap, so it is spelled as one: the scope has to name a G-number §0.5
    # defines. Anything less specific silences the check for free.
    gaps = registered_gaps(text)
    # Keyed by every line a scope spans, not by the line it starts on: a route
    # sits three lines into a paragraph as often as on its first line, and a map
    # keyed by starts silently misses those.
    scope_at: dict[int, str] = {}
    for ln, s in claim_scopes(text.split("\n")):
        for offset in range(s.count("\n") + 1):
            scope_at[ln + offset] = s
    seen: set[str] = set()
    for sec, line, call in p["routes"]:
        if call in seen:
            continue
        seen.add(call)
        if call in covered_routes:
            continue
        if gaps & set(GAP_REF_RE.findall(scope_at.get(line, ""))):
            continue
        add("A", f"§{sec} L{line}", f"{call} is named by no §0.8 row")
    for r in reg:
        cell = r["failure"]
        if not cell or cell == "—":
            add("A", f"L{r['line']}", f"「{r['state']}」 states no failure treatment")
            continue
        if TREAT_RE.search(cell):
            continue
        if re.search(r"(?:As |→\s*)§\d", cell):
            continue
        goto = GOTO_RE.search(cell)
        if goto:
            targets = [t.strip() for t in re.split(r"[/,]| or ", goto.group(1)) if t.strip()]
            if targets and all(
                any(st == t or st.startswith(t) or t in st for st in states) for t in targets
            ):
                continue
        add("A", f"L{r['line']}", f"「{r['state']}」 failure cell names no F1–F5 and no known state: {cell!r}")

    # ---- B ------------------------------------------------------------------
    cited: set[tuple[str, str]] = set()
    for sec, line, k in p["refs"]:
        cited.add((k, f"§{sec} L{line}"))
    for r in reg:
        for k in KEY_REF_RE.findall(r["copy"]):
            cited.add((k, f"§0.8 L{r['line']}"))
    for key, where in sorted(cited):
        if not resolves(key, p["defined"]):
            add("B", where, f"key `{key}` is cited and never defined")
    for key, d in sorted(p["defined"].items()):
        if key != d["raw"]:
            continue
        if not d["en"]:
            add("B", f"L{d['line']}", f"key `{key}` has no English column")
        for slot in sorted(set(SLOT_RE.findall(d["zh"] + d["en"]))):
            if slot not in p["slots"]:
                add("B", f"L{d['line']}", f"key `{key}` interpolates `{{{{{slot}}}}}` with no §0.9 row")

    # ---- C ------------------------------------------------------------------
    for r in reg:
        if not r["exit"] or r["exit"] == "—":
            add("C", f"L{r['line']}", f"「{r['state']}」 has no exit")
    for sec in sorted(p["inventories"]):
        if sec not in reg_frames:
            add("C", f"§{sec}", f"§{sec} draws an element inventory and has no §0.8 row")
    # A section that splits its outcomes by origin — `②` reached by 添加, `②′`
    # by 拉取型号 — has made the origin part of what a state *is*. Every later
    # outcome then has an answer for both origins, and a failure written once in
    # such a section is not a shared state, it is a state whose second half was
    # never written: the reader is left to guess whether pulling from a stopped
    # engine fails the same way adding does. Only rows that hold a request open
    # are asked — F5 issues nothing, so it has nothing that can fail per origin.
    for sec, rows in sorted(reg_by_section.items()):
        primed = {r["state"].split()[0].rstrip("′") for r in rows if "′" in r["state"]}
        if not primed:
            continue
        for r in rows:
            if "′" in r["state"] or not re.search(r"\bF[1-4]\b", r["failure"]):
                continue
            if r["state"].split()[0].rstrip("′") in primed:
                continue
            twins = ", ".join(f"{s}′" for s in sorted(primed))
            add(
                "C",
                f"L{r['line']}",
                f"「{r['state']}」 is written once in a §{sec} that states its other failures "
                f"for both origins ({twins}); it names neither origin and has no ′ row",
            )

    # ---- D ------------------------------------------------------------------
    reg_copy = " ".join(r["copy"] for r in reg)
    conditions = [k for k, d in p["defined"].items() if k == d["raw"] and CONDITION_RE.search(k)]
    for key in sorted(conditions):
        d = p["defined"][key]
        base = re.sub(r"_(one|other)$", "", key)
        if base in reg_copy or key in reg_copy:
            continue
        add("D", f"L{d['line']}", f"condition key `{key}` is cited by no §0.8 row")

    # ---- E ------------------------------------------------------------------
    auth = load_authorities(ROOT)
    e_findings, e_scale = authority_claims(text, auth, ROOT, reg)
    findings.extend(e_findings)

    scale = {
        "register rows": len(reg),
        "distinct states": len(states),
        "frames with a register row": len(reg_frames),
        "frame sections with an element inventory": len(p["inventories"]),
        "mutating calls scanned": len(seen),
        "copy tables / rows": f"{p['tables']} / {p['rows']}",
        "copy keys defined": len({d["raw"] for d in p["defined"].values()}),
        "condition-named keys": len(conditions),
        "interpolation slots declared": len(p["slots"]),
        "prose key references": len(p["refs"]),
        "authority: contracted routes read": len(auth["routes"]),
        "authority: route claims": e_scale["routes"],
        "authority: request/response body claims": e_scale["bodies"],
        "authority: guarded-status claims": e_scale["status branches"],
        "authority: schema citations": e_scale["schema citations"],
        "authority: counted-vocabulary claims": e_scale["vocabulary claims"],
        "authority: contract mapping tables": e_scale["contract mapping tables"],
        "authority: repo symbol citations": e_scale["repo symbols"],
    }
    empty = [k for k, v in scale.items() if v == 0 and k not in TARGET_ZERO]
    broken = self_test(auth, ROOT)
    return {
        "ok": not findings and not empty and not broken,
        "input_mode": mode,
        "input_fingerprint": fingerprint,
        "input_scale": scale,
        "empty_inventories": empty,
        "broken_arms": broken,
        "findings": findings,
    }


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else str(SPEC)
    r = check(target)
    print(f"input mode        : {r['input_mode']}")
    print(f"input fingerprint : {r['input_fingerprint']}")
    print("input scale (self-generated in this run):")
    for k, v in r["input_scale"].items():
        zero_ok = " (target zero, arms self-tested)" if k in TARGET_ZERO else ""
        print(f"  {k:<42}: {v}{zero_ok}")
    if r["broken_arms"]:
        print()
        print("FAIL: a target-zero arm no longer fires, so its zero proves nothing")
        for k in r["broken_arms"]:
            print(f"  {k}")
        return 2
    if r["empty_inventories"]:
        print()
        print("FAIL: an inventory came back empty — the extractor, not the document, is wrong")
        for k in r["empty_inventories"]:
            print(f"  {k}")
        return 2
    print()
    by: dict[str, list[dict[str, str]]] = {}
    for f in r["findings"]:
        by.setdefault(f["class"], []).append(f)
    labels = {
        "A": "mutating call with no treatment, or a treatment that does not exist",
        "B": "copy cited but not defined, missing English, or an undeclared slot",
        "C": "state with no exit, or a frame with no register row",
        "D": "condition key no state cites",
        "E": "a claim about the system that its authority file does not make",
    }
    for cls in "ABCDE":
        items = by.get(cls, [])
        print(f"[{cls}] {labels[cls]}: {len(items)}")
        for f in items:
            print(f"   {f['where']:<16} {f['message']}")
    print(f"\ntotal gaps: {len(r['findings'])}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
