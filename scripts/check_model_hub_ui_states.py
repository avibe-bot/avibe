#!/usr/bin/env python3
"""State-completeness gate for docs/plans/model-hub-ui-spec.md.

The spec grew one frame at a time, in prose, and "is this state finished?" was a
question only a reviewer could answer. §0.8 turns the states into a table; this
script turns the table into a set of assertions a machine can check.

It regenerates its input from the live document in the same run it reports —
pass a checkout, a path, or a git rev that is read through `git show`. It never
consumes a snapshot committed beside it, because a snapshot can agree with a
checker while both disagree with the file everyone else reads.

Every input this run reads — the document, `api.md`, the schemas, the cited
Python — comes from one `Origin` resolved once from that target, and every arm
reads a declared slice of the document rather than all of it. No arm opens a path
or scans the whole text on its own: an arm that chooses where to read is an arm
that can be pointed at the wrong revision, or fooled by a line elsewhere that
looks like the one it wanted, and both have now happened.

Five gap classes, each a set computed from the text:

  A  a mutating call §1 names that no §0.8 row states a treatment for, or a
     §0.8 row whose failure cell is empty or names a treatment that does not exist
  B  a copy key cited but never defined, a key row with no English column, or a
     `{{slot}}` with no §0.9 row
  C  a §0.8 row with no exit, or a frame section that draws an element inventory
     and contributes no §0.8 row
  D  a copy key whose name declares a condition that no §0.8 row cites
  E  a claim the spec makes about the system that the file with authority over
     that claim — `api.md`, a schema, the repo — does not make

Every class asks the same question of a different inventory: does this citation
name exactly one thing that exists? So there is one comparison, `Universe` /
`Match`, and each class names the universes it reads. The three rules that
comparison carries — match by whole token, an empty match is a gap, one name
defined twice is a gap — are the whole of what the classes rely on, which is why
no class is allowed to compare names itself. Nine of these gaps were reported by
reviewers as separate bugs while each class was still inventing its own
comparison; they were one bug in five copies.

The input scale is reported before the verdict. What this gate claims is exactly
that those five sets are empty — not that the document is complete, not that the
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parent.parent
SPEC = Path("docs/plans/model-hub-ui-spec.md")
CONTRACTS = Path("docs/plans/model-hub-contracts")
API_CONTRACT = CONTRACTS / "api.md"

MODE_FILES = "same_run_live_files"
MODE_REV = "same_run_git_rev"


# --- the gate's one input origin --------------------------------------------
#
# Every byte this run reads comes from here, and no arm reads a path of its own.
#
# The two halves of that arrived a review round apart, as one defect found
# twice. Round 9: the gate decided path-or-revision by *spelling*, so `HEAD`, a
# branch and a tag were read as paths and died on a missing file — the gate
# could not be pointed at the head under review. Round 13: the spec could be
# read from a revision but the authorities never were, so a check of a revision
# compared that revision's document against whatever the contracts say right
# now, and an alternate root holding only the spec silently borrowed all of its
# authorities from this checkout and reported green. Neither is an accident of
# one arm. Both are the same generator the shared comparison closed for *names*,
# still running on *reads*: each arm inventing how it gets at a file.
#
# So reading is one object with one resolution, the origin is decided once per
# run from the target, and what it resolved to is printed with the verdict. An
# authority borrowed from somewhere else is precisely the thing a green result
# may not hide.


class Origin:
    """A checkout or a git revision, and the only place this file reads a file."""

    def __init__(self, kind: str, *, tree: Path | None = None, rev: str | None = None) -> None:
        if kind not in ("tree", "rev"):
            raise ValueError(f"{kind!r} is not an origin kind")
        self.kind = kind
        self.tree = tree
        self.rev = rev
        self._cache: dict[str, str | None] = {}

    # --- how a spelling becomes an origin ---------------------------------
    @classmethod
    def tree_at(cls, path: Path) -> "Origin":
        return cls("tree", tree=path.resolve())

    @classmethod
    def revision(cls, rev: str) -> "Origin":
        return cls("rev", rev=rev)

    @classmethod
    def resolve(cls, spelling: str) -> "Origin":
        """A directory is a checkout; anything else has to name a revision.

        Decided by the filesystem, never by spelling — that is what round 9
        cost. A revision has to prove itself by producing the one file every
        authority hangs off, so a typo fails here, saying which two things the
        target is not, instead of surfacing later as an empty inventory.
        """
        for candidate in (Path(spelling), ROOT / spelling):
            if candidate.is_dir():
                return cls.tree_at(candidate)
        origin = cls.revision(spelling.split(":", 1)[0])
        if origin.read(API_CONTRACT) is None:
            raise SystemExit(
                f"{spelling!r} is neither a directory nor a git revision holding {API_CONTRACT}"
            )
        return origin

    @classmethod
    def containing(cls, path: Path) -> "Origin | None":
        """The checkout `path` lives in, found by the file the authorities start at."""
        for parent in path.resolve().parents:
            if (parent / API_CONTRACT).is_file():
                return cls.tree_at(parent)
        return None

    @property
    def mode(self) -> str:
        return MODE_FILES if self.kind == "tree" else MODE_REV

    @property
    def label(self) -> str:
        if self.kind == "rev":
            return f"git rev {self.rev}"
        return "this checkout" if self.tree == ROOT else str(self.tree)

    # --- reading ----------------------------------------------------------
    def read(self, rel: str | Path) -> str | None:
        """Repo-relative path -> text, or None when this origin does not carry it."""
        key = str(rel)
        if key not in self._cache:
            self._cache[key] = self._read(key)
        return self._cache[key]

    def _read(self, rel: str) -> str | None:
        if self.kind == "tree":
            assert self.tree is not None
            path = self.tree / rel
            return path.read_text(encoding="utf-8") if path.is_file() else None
        # `cwd=ROOT` is the object store the revision is named in, not an input:
        # a rev is only meaningful inside a repository, and this is the one whose
        # history the caller spelled a rev of.
        proc = subprocess.run(
            ["git", "show", f"{self.rev}:{rel}"], cwd=ROOT, capture_output=True, text=True
        )
        return proc.stdout if proc.returncode == 0 else None

    def names_in(self, directory: Path, suffix: str) -> list[str]:
        """File names directly under `directory` ending in `suffix`, sorted."""
        if self.kind == "tree":
            assert self.tree is not None
            return sorted(p.name for p in (self.tree / directory).glob(f"*{suffix}"))
        proc = subprocess.run(
            ["git", "ls-tree", "--name-only", f"{self.rev}:{directory}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return []
        return sorted(n for n in proc.stdout.split() if n.endswith(suffix))

    @staticmethod
    def read_detached(path: Path) -> str:
        """A document that is in no checkout — the mutation harness writes one."""
        return path.read_text(encoding="utf-8")


def resolve_inputs(
    target: str | Path, authorities: str | Path | None = None
) -> tuple[str, str, Origin]:
    """The document to check, how it was read, and the origin of every authority.

    `target` is a checkout, a file, or a git revision. The authority origin
    *follows* the target — a revision's spec is checked against that revision's
    contracts — with one case that must be stated out loud rather than assumed:
    a document living outside any checkout has no authorities of its own, so the
    caller names whose it borrows and the borrowing is reported with the verdict.
    Left to a default, that case is a false pass wearing a green badge.
    """
    stated = Origin.resolve(str(authorities)) if authorities is not None else None
    for candidate in (Path(target), ROOT / str(target)):
        if candidate.is_dir():
            here = Origin.tree_at(candidate)
            text = here.read(SPEC)
            if text is None:
                raise SystemExit(f"{here.label} holds no {SPEC}")
            return text, here.mode, stated or here
        if candidate.is_file():
            origin = stated or Origin.containing(candidate)
            if origin is None:
                raise SystemExit(
                    f"{candidate} is in no checkout that holds {API_CONTRACT}, so this run has "
                    f"no authority to check it against. Point the gate at the checkout or the "
                    f"revision, or state whose authorities it borrows: "
                    f"check(<document>, authorities=<checkout or revision>)"
                )
            return Origin.read_detached(candidate), MODE_FILES, origin
    rev, _, rel = str(target).partition(":")
    here = Origin.resolve(rev)
    text = here.read(rel or SPEC)
    if text is None:
        raise SystemExit(f"{here.label} holds no {rel or SPEC}")
    return text, here.mode, stated or here


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


# --- the gate's one comparison ---------------------------------------------
#
# Every class used to grow its own. Class A asked whether a route appeared as a
# *substring* of a register row's concatenated cells, and whether an exit's
# target was `in` some state's name. Class B resolved a copy key by unique
# suffix. Class D resolved the same keys against the same tables by a second,
# different rule. Only the copy tables ever looked for a name defined twice.
# Three review heads in a row each found the same defect in whichever class that
# round's reviewer happened to read — not three bugs, one way of writing, with a
# fresh chance to repeat it every time a class was added.
#
# So the comparison is written once, here, and no class writes another. Three
# rules hold for every universe, enforced by this object rather than promised by
# each caller:
#
#   token      a citation matches a definition as a whole dotted token, through
#              the aliases the universe declares — never as a prefix, suffix or
#              substring. `/models/runtime/start` does not resolve
#              `/models/runtime/startup`; `auth` does not resolve `fail.auth`.
#   empty      a citation that resolves to nothing is a finding, never a silent
#              pass. `if not owners: continue` is how a mistyped field left a
#              table bound to no contracted field with the gate reporting green.
#   duplicate  one canonical token defined twice with different content is a
#              finding: two answers to one question, and no way for a reader to
#              know which one ships.
#
# The rules are properties of the comparison, so they arrive with it. A class
# added later cannot opt out of `duplicate` by forgetting to write it, and
# cannot weaken `token` for its own convenience, because it has no comparison of
# its own to weaken.

RULES = ("token", "empty", "duplicate")
SIDES = ("spec", "authority")


def segments(token: str) -> tuple[str, ...]:
    """A dotted name as its parts. Comparison is over these, never over the string."""
    return tuple(token.split("."))


ORDINAL_RE = re.compile(r"^([①-⑳]′?)")


def state_spellings(name: str) -> tuple[str, ...]:
    """Every spelling a state answers to, written down where the state is defined.

    §0.8 cites its own rows three ways: by the whole name, by the ordinal the
    name leads with (`→ ③ / ④ / ⑤`), and by the name up to its first qualifier
    (`→ Unreachable` for 「Unreachable (engine down)」). All three are real, so
    all three are declared here — once, as that row's names. The alternative is
    to discover them at resolution time with a prefix or substring test, which
    is how 「Ready」 came to vouch for a cell pointing at 「Read」: a test that
    loose cannot tell a legal short spelling from a typo.
    """
    spellings = {name}
    ordinal = ORDINAL_RE.match(name)
    if ordinal:
        spellings.add(ordinal.group(1))
    head = re.split(r"\s*[(,]", name, maxsplit=1)[0].strip()
    if head:
        spellings.add(head)
    return tuple(sorted(spellings))


@dataclass(frozen=True)
class Match:
    """What one citation resolved to. `empty` is a finding; so is `ambiguous`."""

    universe: str
    citation: str
    hits: tuple[str, ...]
    payloads: tuple[Any, ...]

    @property
    def empty(self) -> bool:
        return not self.hits

    @property
    def wildcard(self) -> bool:
        """The citation asked for a family, not for one name."""
        return self.citation.endswith("*")

    @property
    def ambiguous(self) -> bool:
        """More answers than the citation asked for.

        A citation that names one thing and gets several has not identified
        anything, and every class wants to say so. A trailing `*` asks for the
        whole family, so many answers is the correct outcome there — the
        distinction lives here rather than in each caller, because a caller
        that forgets it is exactly how a rule stops being applied.
        """
        return len(self.hits) > 1 and not self.wildcard

    @property
    def one(self) -> Any:
        return self.payloads[0]


class Universe:
    """A named set of exact identities, and the only place names are compared.

    `side` records which file the definitions come from — the spec under review,
    or an authority it must agree with — because that decides how a rule can be
    exercised by a test. `owner` is the class that reports this universe's
    duplicates, so a duplicate is attributed to the class that reads the table
    it lives in rather than to whichever class happened to look first.
    """

    def __init__(self, name: str, side: str, owner: str) -> None:
        if side not in SIDES:
            raise ValueError(f"{side!r} is not one of {SIDES}")
        self.name = name
        self.side = side
        self.owner = owner
        self._payload: dict[str, Any] = {}
        self._content: dict[str, Any] = {}
        self._where: dict[str, Any] = {}
        self._alias: dict[str, set[str]] = {}
        self.duplicates: list[tuple[str, Any, Any]] = []

    def define(
        self,
        token: str,
        payload: Any,
        *,
        content: Any = None,
        where: Any = None,
        aliases: tuple[str, ...] = (),
    ) -> "Universe":
        """Declare one identity. A second declaration with different content is a gap.

        `content` is what "the same definition" means for this universe — the
        rendered text of a copy row, the cells of a contract row. Re-declaring a
        token with the *same* content is a document naming one thing twice,
        which is not a contradiction and is not reported.
        """
        body = payload if content is None else content
        if token in self._payload:
            if self._content[token] != body:
                self.duplicates.append((token, self._where[token], where))
        else:
            self._payload[token] = payload
            self._content[token] = body
            self._where[token] = where
        for alias in aliases:
            if alias != token:
                self._alias.setdefault(alias, set()).add(token)
        return self

    def __len__(self) -> int:
        return len(self._payload)

    def tokens(self) -> set[str]:
        return set(self._payload)

    def items(self) -> list[tuple[str, Any]]:
        return sorted(self._payload.items())

    def resolve(self, citation: str) -> Match:
        """The one comparison. Exact token, then declared aliases, then families.

        A citation ending in `*` names a family, and a family is still matched by
        token: the prefix is split into segments and compared segment for
        segment, so `sourceDetail.tiers.*` reaches `sourceDetail.tiers.add` and
        never reaches `sourceDetail.tiersAdd`. String prefixing would reach both.
        """
        cite = citation.strip().strip("`")
        if cite.endswith("*"):
            prefix = segments(cite.rstrip("*").rstrip("."))
            hits = {t for t in self._payload if segments(t)[: len(prefix)] == prefix}
            for alias, targets in self._alias.items():
                if segments(alias)[: len(prefix)] == prefix:
                    hits |= targets
            found = tuple(sorted(hits))
        elif cite in self._payload:
            found = (cite,)
        else:
            found = tuple(sorted(self._alias.get(cite, ())))
        return Match(self.name, cite, found, tuple(self._payload[t] for t in found))


# Which universes each class consults. A class that reads no universe compares
# nothing, and a class that compares something not listed here has written a
# comparison of its own — which is what the tiled mutation suite exists to
# refuse. Adding a class means adding its row, and adding a universe means
# adding that universe's cases for all three rules.
#
# One thing here is deliberately *not* a universe: the coverage sets class A
# builds (`covered_routes`, `accounted`). Those are set arithmetic over tokens
# `normalize_route` already canonicalised — is this route in that set — not name
# resolution, so they need no resolver and get none. Class A is listed against
# `routes` because it reads that universe as an inventory: it enumerates the
# contracted mutations and asks which of them nothing reaches.
CLASS_UNIVERSES: dict[str, tuple[str, ...]] = {
    "A": ("routes", "states", "treatments", "gaps"),
    "B": ("copy", "slots"),
    "C": ("frames",),
    "D": ("copy",),
    "E": ("routes", "schema files", "schema fields", "repo symbols", "gaps"),
}

# Every universe this module builds, and which side fills it. The tiled mutation
# suite used to union `CLASS_UNIVERSES` with a list of names written out in the
# test, which meant the test's idea of "every universe" was maintained by hand,
# in a second file — so a collection built by hand here was invisible to it
# twice over. The §0.5 gap registry lived exactly there: a dict comprehension,
# outside the comparator, uncovered by the grid that reports itself full.
#
# Declaring it here is what makes the grid self-proving: a universe added
# without its cases fails the suite, and a universe named in `CLASS_UNIVERSES`
# but never built — or built but never declared — fails it too.
UNIVERSE_SIDES: dict[str, str] = {
    "copy": "spec",
    "slots": "spec",
    "states": "spec",
    "treatments": "spec",
    "frames": "spec",
    "gaps": "spec",
    "routes": "authority",
    "schema files": "authority",
    "schema fields": "authority",
    "repo symbols": "authority",
}
UNIVERSES: tuple[str, ...] = tuple(UNIVERSE_SIDES)

# The class list, spelled once. It was spelled `"ABCDE"` in the reporter and as
# the keys of the label table, so a sixth class could be checked and never
# printed — and the mutation suite, which tiles over it, would not have known to
# ask for its cases.
CLASSES: tuple[str, ...] = tuple(CLASS_UNIVERSES)

CLASS_LABELS: dict[str, str] = {
    "A": "mutating call with no treatment, or a treatment that does not exist",
    "B": "copy cited but not defined, missing English, or an undeclared slot",
    "C": "state with no exit, or a frame with no register row",
    "D": "condition key no state cites",
    "E": "a claim about the system that its authority file does not make",
}
assert set(CLASS_LABELS) == set(CLASSES), "a gate class with no label prints as nothing"

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
#
# `CONTRACTS` / `API_CONTRACT` are declared with the other input constants, next
# to the origin that reads them.

API_ROW_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+`([^`]+)`$")
BODY_RE = re.compile(r"`(\{[^`{}]*\})`")
# What immediately introduces a body as the server's answer rather than the
# client's request. `api.md` writes both sides in one cell, so a claim has to
# say which side it is quoting, and these are the ways this document says it.
ANSWER_CUE_RE = re.compile(r"(?:→|returns|answers|echoes|responds with|re-echoes)\s*$")
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


def load_authorities(origin: Origin) -> dict[str, Any]:
    """Read the authority files live, in this run, from `origin`.

    The authorities come from wherever the document came from, and never from
    "wherever this script happens to be installed". A revision's spec is checked
    against that revision's contracts; a spec read out of another checkout is
    checked against that checkout's. Reading them from here while the document
    came from elsewhere answers a question nobody asked — whether a past document
    agrees with today's contracts — and the answer is noise either way.
    """
    api_text = origin.read(API_CONTRACT)
    if api_text is None:
        raise SystemExit(f"{origin.label} holds no {API_CONTRACT}")
    routes = Universe("routes", "authority", "E")
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
        # `request → response` in one cell, and only the left side is what a
        # client may send. Reading the whole cell let a response field stand in
        # for a request field: the source-order row answers `{agent: AgentSupply}`,
        # so a spec that posted `{agent: [...]}` to it passed a check the server
        # would reject. `guarded` still reads the whole cell, because that is
        # where the refusal envelope is named.
        request, _, response = cells[1].partition("→")
        routes.define(
            normalize_route(m.group(1), m.group(2)),
            {
                "keys": literal_keys(request),
                "response_keys": literal_keys(response),
                "guarded": "guarded" in cells[1].lower() or "force" in cells[1],
                "cell": cells[1],
            },
            content=cells[1],
            where=cells[0],
        )
    # The shared envelopes are defined in prose and in the JSON examples, and a
    # guarded route's row names them rather than spelling them out. Their
    # vocabulary is collected from everything that is not a route row, so a
    # route's own request shape can never leak into another route's allowance.
    outside = "\n".join(l for l in api_text.split("\n") if l not in table_spans)
    envelope = literal_keys(outside)

    schemas: dict[str, set[str]] = {}
    properties: dict[str, set[str]] = {}
    enums: dict[str, dict[str, set[str]]] = {}
    files = Universe("schema files", "authority", "E")
    # One canonical token per *declaration site*, with the bare field name as an
    # alias. `agent-supply.schema.json` declares `supply_status` twice — once as
    # the backend's rollup, once inside `named_agents[]` — and a citation of the
    # bare name resolves to both, which is `Match.ambiguous` and a finding. That
    # used to be a bespoke `len(owners) > 1` branch inside one class; it is now
    # what the shared comparison does with any name two things answer to.
    fields = Universe("schema fields", "authority", "E")
    for name_json in origin.names_in(CONTRACTS, ".schema.json"):
        raw = origin.read(CONTRACTS / name_json)
        if raw is None:  # pragma: no cover - the origin just listed it
            continue
        path = Path(name_json)
        doc = json.loads(raw)
        schemas[path.name] = schema_vocabulary(doc)
        properties[path.name] = set(doc.get("properties", {}))
        enums[path.name] = enum_fields(doc)
        files.define(path.name, schema_vocabulary(doc), content=path.name, where=path.name)
        for name, decls in enum_paths(doc, doc.get("title", path.stem)).items():
            for where, values in decls.items():
                # A document names a field by any tail of its path —
                # `detail_key`, `state.detail_key`, `Source.state.detail_key` —
                # so every tail is declared as an alias rather than matched by
                # `endswith`, which would also accept `te.detail_key`. Declaring
                # them is what makes two paths sharing a tail resolve to two
                # hits and report as ambiguous instead of picking one.
                parts = segments(where)
                tails = {".".join(parts[i:]) for i in range(1, len(parts))} | {name}
                fields.define(
                    where,
                    {"schema": path.name, "values": values, "path": where},
                    content=sorted(values),
                    where=path.name,
                    aliases=tuple(sorted(tails | {f"{path.name}::{t}" for t in tails | {where}})),
                )
    return {
        "routes": routes,
        "envelope": envelope,
        "schemas": schemas,
        "properties": properties,
        "enums": enums,
        "schema files": files,
        "schema fields": fields,
    }


def defined_symbols(source: str) -> set[str]:
    """The names a Python file defines that a reader can go and look at.

    A citation like `service.py:list_agents` promises the reader an addressable
    symbol: something a module or a class declares, which a reviewer can reach by
    name. `ast.walk` credited every assignment anywhere, so a local variable
    three frames into a function body vouched for a citation — the identity rule
    inverted, since it made a name the file does *not* export resolve as if it
    did, and it hid the exact drift this class is here to catch.

    The rule is one line: a name bound inside a function body belongs to that
    body. So the descent records defs and classes and skips their bodies —
    entering class bodies, because a method is addressable and is how the two
    live citations name their functions, and never entering function bodies,
    lambdas or comprehensions, whose bindings are local by definition.
    """
    names: set[str] = set()

    def scan(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(child.name)
                continue
            if isinstance(child, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                continue
            if isinstance(child, ast.ClassDef):
                names.add(child.name)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.add(child.id)
            scan(child)

    scan(ast.parse(source))
    return names


def claim_scopes(numbered: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Split a slice of the document into the units a claim is read in.

    A table row is its own scope. Everything else is its paragraph. Without the
    first half the §0.8 register — several hundred contiguous rows — reads as
    one scope, and every route in it would vouch for every body literal in it.

    Takes numbered lines rather than a blob, so a scope carries the line number
    it has in the document even when the slice it came from starts elsewhere. A
    gap in the numbering ends the paragraph: two sections that are not adjacent
    are not one paragraph, however they were concatenated.
    """
    scopes: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 0
    previous = 0

    def flush() -> None:
        nonlocal buffer
        if buffer:
            scopes.append((start, "\n".join(buffer)))
            buffer = []

    for n, line in numbered:
        if n != previous + 1:
            flush()
        previous = n
        if line.startswith("|"):
            flush()
            scopes.append((n, line))
            continue
        if not line.strip():
            flush()
            continue
        if not buffer:
            start = n
        buffer.append(line)
    flush()
    return scopes


# --- what each arm is allowed to read ---------------------------------------
#
# Reading the whole document is a licence to be fooled by a line that looks like
# the one you want. `| G-99 |` written in a §1 example table registered a gap
# and silenced a real claim; a §0.4 route row moved into §0.7 kept accounting
# for a mutation §0.4 no longer excuses. Both are the same shape as the origin
# defect: an arm deciding for itself where its input comes from.
#
# So every arm declares its range and gets exactly that. A scope nobody reads is
# reported (`unread_scopes`), because a declaration that binds nothing is a
# comment pretending to be a constraint. `claims` is whole-document *by
# declaration* — an authority restated wrongly is a defect wherever it is
# written — and that is the point of writing the range down: the one arm that
# needs everything says so, next to the reason, instead of every arm helping
# itself.


class Scope(NamedTuple):
    where: str  # a section number, a `1.`-style prefix, or `*` for the document
    why: str


SCOPES: dict[str, Scope] = {
    "register": Scope("0.8", "the state register is one table, and §0.8 is where it lives"),
    "treatments": Scope("0.8", "the closed failure-treatment set is declared with the register"),
    "gap registry": Scope("0.5", "a `| G-n |` row registers a gap only inside the §0.5 registry"),
    "scope note": Scope("0.4", "§0.4 is the only place a contracted route is declared out of scope"),
    "slots": Scope("0.9", "the interpolation slots are §0.9's table"),
    "copy": Scope("1.", "a copy table belongs to the frame that renders it"),
    "frame prose": Scope("1.", "element inventories, key citations and route mentions are per frame"),
    "mapping tables": Scope("1.", "a total rendering of a field is drawn inside the frame showing it"),
    "claims": Scope("*", "a restated authority is wrong wherever it is written"),
}

# The loader carries two rules, and each one is tiled over the things it governs
# the way `CLASS_UNIVERSES` is tiled over the five classes: one input per origin
# cell, one declared range per scope cell. A rule stated once and tested on one
# arm is a rule that holds on one arm.
LOADER_RULES = ("origin", "scope")
LOADER_ARMS: dict[str, tuple[str, ...]] = {
    # every kind of input, because the defect was one input reading from a
    # different place than the rest
    "origin": ("spec", "api.md", "schema", "python"),
    # every declared range, because the defect was one arm reading past its own
    "scope": tuple(SCOPES),
}


class Document:
    """The document, its section geometry, and the declared slice each arm reads.

    No arm gets the text. Asking for an undeclared scope is a `KeyError` rather
    than a full-document default, so a new arm has to say what it reads before
    it can read anything.
    """

    def __init__(self, text: str) -> None:
        self._lines = text.split("\n")
        self.fingerprint = hashlib.sha256(text.encode()).hexdigest()[:16]
        self.requested: set[str] = set()
        self._spans: list[tuple[str, int, int]] = []
        for i, line in enumerate(self._lines):
            m = SECTION_RE.match(line)
            if m:
                if self._spans:
                    self._spans[-1] = (self._spans[-1][0], self._spans[-1][1], i)
                self._spans.append((m.group(1), i, len(self._lines)))

    def section_of(self, line_no: int) -> str:
        """The §n.m a 1-based line number falls in."""
        for name, a, b in self._spans:
            if a <= line_no - 1 < b:
                return name
        return "0"

    def sections(self, prefix: str) -> list[tuple[str, int, str]]:
        """(name, 1-based heading line, heading text) for sections named `prefix...`.

        Section geometry is a property of the whole document rather than a slice
        of it — a heading is what *creates* the slices — so this needs no
        declaration, and asking for it grants no access to the lines inside.
        """
        return [
            (name, a + 1, self._lines[a]) for name, a, _b in self._spans if name.startswith(prefix)
        ]

    def scope(self, name: str) -> list[tuple[int, str]]:
        """The declared slice for `name`, as 1-based numbered lines."""
        declared = SCOPES[name]
        self.requested.add(name)
        if declared.where == "*":
            return list(enumerate(self._lines, start=1))
        picked: list[tuple[int, str]] = []
        for section, a, b in self._spans:
            if (
                section.startswith(declared.where)
                if declared.where.endswith(".")
                else section == declared.where
            ):
                picked.extend((i + 1, self._lines[i]) for i in range(a, b))
        return picked

    def claims(self) -> list[tuple[int, str]]:
        return claim_scopes(self.scope("claims"))


def parse(doc: Document) -> dict[str, Any]:
    # --- §0.8 register -------------------------------------------------------
    register: list[dict[str, Any]] = []
    for n, line in doc.scope("register"):
        if not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if len(cells) != 6 or cells[0] in ("Frame", "---") or cells[1] == "---":
            continue
        register.append(
            {
                "line": n,
                "frame": cells[0],
                "state": cells[1],
                "entry": cells[2],
                "failure": cells[3],
                "copy": cells[4],
                "exit": cells[5],
            }
        )

    # --- §0.8 states, §0.8 treatments, §1 frames -----------------------------
    # A state's identity is its frame and its name together: 「Ready」 is a
    # different state in every frame that has one, and a universe keyed by the
    # bare name would call fifteen unrelated rows one contradictory definition.
    states = Universe("states", "spec", "A")
    for r in register:
        frame = r["frame"].lstrip("§")
        states.define(
            f"{frame} · {r['state']}",
            r,
            content=(r["entry"], r["failure"], r["copy"], r["exit"]),
            where=r["line"],
            # Both qualified and bare: a cell inside §1.5 writes 「③」, prose
            # elsewhere writes the state's own name. Two rows in one frame that
            # claim the same spelling make that spelling ambiguous, which is a
            # gap — 「Ready」 in two different frames is not.
            aliases=tuple(
                spelling
                for name in state_spellings(r["state"])
                for spelling in (name, f"{frame} · {name}")
            ),
        )

    treatments = Universe("treatments", "spec", "A")
    in_treat = False
    for n, line in doc.scope("treatments"):
        if line.startswith("| # | Treatment |"):
            in_treat = True
            continue
        if in_treat and not line.startswith("|"):
            in_treat = False
        if not in_treat:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and re.fullmatch(r"F\d+", cells[0]):
            treatments.define(cells[0], cells[1], content=(cells[1], cells[2]), where=n)

    frames = Universe("frames", "spec", "C")
    for name, line_no, heading in doc.sections("1."):
        frames.define(name, line_no, content=heading.strip(), where=line_no)

    # --- §0.9 slots ----------------------------------------------------------
    slots = Universe("slots", "spec", "B")
    for n, line in doc.scope("slots"):
        if line.startswith("| `{{"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            for slot in SLOT_RE.findall(cells[0]):
                slots.define(slot, n, content=tuple(cells[1:]), where=n)

    # --- copy tables ---------------------------------------------------------
    # A copy row counts only inside a copy table, which starts at its own
    # `| Key | 中文 | English |` header and ends with the table. Other tables in
    # this document also open with a backticked token — contract enum values,
    # ink names, schema fields — and reading those as key definitions is how a
    # checker invents keys nobody wrote.
    collected: list[dict[str, Any]] = []
    namespaces: set[str] = set()
    tables = 0
    rows = 0
    ns = ""
    pending_ns = ""
    in_table = False
    for n, line in doc.scope("copy"):
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
        collected.append(
            {"key": key, "zh": zh, "en": en, "line": n, "ns": ns,
             "qualified": f"{ns}.{key}" if ns else key}
        )

    # One copy key is one canonical token, and an i18next plural pair is one key
    # written on two rows — `count_one` and `count_other` answer the same
    # question for different cardinalities, so they are assembled into the family
    # before anything is declared. Declaring them separately and then teaching
    # the comparison to forgive the collision is how a rule acquires an
    # exception; assembling first means the duplicate rule needs no exception and
    # still catches the case it exists for, two rows giving one key two texts.
    copy = Universe("copy", "spec", "B")
    families: dict[str, list[dict[str, Any]]] = {}
    for row in collected:
        stem = re.sub(r"_(?:one|other)$", "", row["qualified"])
        families.setdefault(stem, []).append(row)
    for canonical, members in families.items():
        forms = [re.search(r"_(one|other)$", r["key"]) for r in members]
        # A plural family is the only reason two rows may share one token, and
        # it has to look like one: every member carries a form, and no form
        # twice. Anything else sharing a token is two answers to one question,
        # declared separately so the duplicate rule sees them.
        is_family = (
            len(members) > 1
            and all(forms)
            and len({f.group(1) for f in forms if f}) == len(members)
        )
        for group in [members] if is_family else [[r] for r in members]:
            aliases = {canonical, f"models.hub.{canonical}"}
            for row in group:
                aliases |= {
                    row["key"],
                    re.sub(r"_(?:one|other)$", "", row["key"]),
                    row["qualified"],
                    f"models.hub.{row['qualified']}",
                }
            copy.define(
                canonical,
                sorted(group, key=lambda r: r["line"]),
                content=sorted((r["zh"], r["en"]) for r in group),
                where=min(r["line"] for r in group),
                aliases=tuple(sorted(aliases)),
            )

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
    for n, line in doc.scope("frame prose"):
        sec = doc.section_of(n)
        if line.startswith("**Element inventory**"):
            inventories.add(sec)
        for k in KEY_REF_RE.findall(line):
            head = k.split(".")[0]
            if head == "models" and k.startswith("models.hub."):
                refs.append((sec, n, k))
            elif head in namespaces:
                refs.append((sec, n, k))
        for meth, path in ROUTE_RE.findall(line):
            # Normalized on the way in, because class A compares this against a
            # register side that is normalized too, and two sides normalized by
            # different rules is not a comparison — it reports every route as
            # uncovered the moment one side spells a parameter the other way.
            routes.append((sec, n, normalize_route(meth, path)))

    # Built here, once, because a universe built per caller is a universe whose
    # duplicate rule reports per caller: the registry was read twice, by two arms,
    # and neither could see the other's copy.
    return {
        "register": register,
        "universes": {u.name: u for u in (copy, slots, states, treatments, frames, registered_gaps(doc))},
        "tables": tables,
        "rows": rows,
        "refs": refs,
        "routes": routes,
        "inventories": inventories,
        "namespaces": namespaces,
        "sections": [name for name, _line, _heading in doc.sections("1.")],
    }


def cited_rows(tokens: set[str], copy: Universe) -> set[int]:
    """Lines of the copy rows `tokens` name, through the one comparison.

    Two rules used to answer this question — class B's, which resolved a key by
    unique suffix, and class D's, which resolved it by spelling — over the same
    tables. Now there is one, and both classes ask it. Resolution is reported by
    line because a key is written twice, bare inside its own table and qualified
    outside it, and both spellings are the same row.
    """
    return {row["line"] for t in tokens for rows in copy.resolve(t).payloads for row in rows}


MAPPING_HEADER_RE = re.compile(r"^\|\s*`([A-Za-z][A-Za-z0-9_.\[\]]*)`((?:\s*`\[[a-z-]+\]`)*)\s*\|")
SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|$")


def mapping_tables(doc: Document) -> list[tuple[int, str, set[str], set[str], bool]]:
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
    found: list[tuple[int, str, set[str], set[str], bool]] = []
    numbered = doc.scope("mapping tables")
    for pos, (line_no, line) in enumerate(numbered):
        m = MAPPING_HEADER_RE.match(line)
        if (
            not m
            or pos + 1 >= len(numbered)
            or not SEPARATOR_RE.match(numbered[pos + 1][1].strip())
        ):
            continue
        values: set[str] = set()
        for _n, row in numbered[pos + 2 :]:
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
        for _n, above in reversed(numbered[:pos]):
            if not above.strip():
                if lead:
                    break
                continue  # the blank line every table is separated from its lead-in by
            lead.append(above)
        found.append((line_no, m.group(1), values, set(SCHEMA_CITE_RE.findall("\n".join(lead))), "[contract]" in m.group(2)))
    return found


GAP_REF_RE = re.compile(r"\[contract-gap\]`?\s*`?(G-\d+)(?![\w-])")
GAP_ROW_RE = re.compile(r"^\|\s*(G-\d+)\s*\|")


def registered_gaps(doc: Document) -> Universe:
    """The §0.5 registry, as a universe: gap number -> the line registering it.

    `[contract-gap]` is the document's one way to say "this surface has no
    backend behind it", and it is also the one way to tell a checker not to ask
    for something that cannot exist. Both readings are needed, which makes the
    marker a silencer, and a silencer that costs a keystroke silences things
    nobody meant to. Requiring the marker to name a number, and the number to
    resolve to a §0.5 row, prices it at what it should cost: a stated surface, a
    stated missing behaviour, and evidence verified against a named commit. A
    bare `[contract-gap]`, or one citing a number no row defines, exempts
    nothing — the claim is checked as if the marker were not there.

    A reference ends where its digits end. Without that boundary the number was
    read as a prefix, so `G-9x` borrowed the row written about G-9 and a
    `G-15`-shaped typo silenced a route on the strength of a registration that
    was about something else — the same accident as the bare marker, reached by
    one *extra* keystroke instead of one fewer, and harder to see because the
    citation looks like it names something. A suffixed citation now resolves to
    nothing and silences nothing, exactly like a bare one.

    Read from §0.5 alone. Scanning the document for the row *shape* let anything
    shaped like `| G-99 |` — a §1 example, a quoted table, a row moved out of the
    registry in an edit and never removed — mint a silencer, which is the one
    thing this marker must never be cheap enough to do by accident. The row's
    line is kept so the arms that ask "is this scope itself a registration?" can
    ask about *this* row rather than re-testing the shape.

    A universe, not a plain dict, because a silencer is a name and every name in
    this checker is compared in one place. Built by hand, this registry answered
    `G-19` twice by keeping whichever row came last — a second, contradicting row
    silently replacing the first is the one outcome a register of missing
    behaviour may not have — and its references were matched by set intersection,
    which is a second comparison nobody had watched fail. The content is the row
    itself: re-stating a row verbatim is a document repeating itself, while two
    rows under one number saying different things are two answers to "what is
    missing", and `E` reports them, because a gap row is a claim about the
    contract and that is the class that checks those.
    """
    gaps = Universe("gaps", "spec", "E")
    for n, line in doc.scope("gap registry"):
        if m := GAP_ROW_RE.match(line):
            gaps.define(m.group(1), n, content=line.strip(), where=n)
    return gaps


def cites_a_registered_gap(gaps: Universe, text: str) -> bool:
    """Does `text` point at a §0.5 row that exists? The one gap comparison.

    Both arms that honour the marker — class A's route coverage and class E's
    claim check — ask through here, so neither can drift into a spelling of its
    own.
    """
    return any(not gaps.resolve(g).empty for g in GAP_REF_RE.findall(text))


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


def self_test(auth: dict[str, Any], origin: Origin) -> list[str]:
    """Prove the target-zero arms still fire before believing their zeros."""
    broken: list[str] = []
    for fixture, want_counted, want_finding in SELF_TEST:
        fdoc = Document(fixture)
        found, scale = authority_claims(fdoc, auth, origin, [], registered_gaps(fdoc))
        counted = scale["vocabulary claims"] > 0
        reported = any(f["class"] == "E" and "not" in f["message"] for f in found)
        if counted is not want_counted or reported is not want_finding:
            broken.append(
                f"counted-vocabulary arm: {fixture!r} -> counted={counted} reported={reported}, "
                f"expected counted={want_counted} reported={want_finding}"
            )
    return broken


def authority_claims(doc: Document, auth: dict[str, Any], origin: Origin, register: list[dict[str, Any]], gaps: Universe) -> tuple[list[dict[str, str]], dict[str, int]]:
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
    for _token, row in auth["routes"].items():
        every_contracted_key |= row["keys"] | row["response_keys"]

    symbols = Universe("repo symbols", "authority", "E")
    auth["repo symbols"] = symbols
    read_files: set[str] = set()

    registrations = {line_no for _token, line_no in gaps.items()}

    guarded_named: dict[str, int] = {}
    for line_no, scope in doc.claims():
        # A §0.5 row is the registration itself, so it needs no reference to
        # one: its whole job is to describe a behaviour the contract does not
        # have, and describing that behaviour means naming the branch that is
        # missing. Every other scope has to point at a row that exists. "Is
        # this scope a registration?" is answered by the registry's own line
        # numbers, not by re-testing the row shape here — that shape matches
        # anywhere, including the places §0.5 is quoted.
        exempt = line_no in registrations or cites_a_registered_gap(gaps, scope)
        # Where each route is written, not just which routes appear: a body
        # literal is bound to one route, and binding needs positions.
        mentions = [
            (m.start(), m.end(), normalize_route(m.group(1), m.group(2)))
            for m in ANY_ROUTE_RE.finditer(scope)
        ]
        named = {route for _s, _e, route in mentions}
        for route in sorted(named):
            scale["routes"] += 1
            hit = auth["routes"].resolve(route)
            if hit.empty:
                add(f"L{line_no}", f"`{route}` is contracted by no `api.md` route row")
            elif hit.one["guarded"]:
                guarded_named.setdefault(route, line_no)

        for m_body in BODY_RE.finditer(scope):
            literal = m_body.group(1)
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
            # One body belongs to one route, and a scope that names several does
            # not make all of them plausible: the §0.8 row that saves a source
            # order also mentions the chain route two cells away, and the union
            # of their vocabularies accepted `{hops}` posted to the order save —
            # the exact defect this arm was added to catch, hidden by its own
            # allowance. So the body binds to the route mention nearest it,
            # either side, which is how the sentence reads to a human.
            #
            # Measured before it was written, because the alternative — accept a
            # key only if *every* named route contracts it — is simpler and
            # wrong here: it reports three of this document's own rows, where a
            # shared refusal envelope, a shared query parameter and a two-step
            # OAuth exchange are each written once against several routes on
            # purpose. Nearest-mention reports none of them and still catches
            # both the original and a decoy with the routes reordered.
            bound = min(
                mentions,
                key=lambda mention: (
                    0
                    if mention[0] < m_body.end() and m_body.start() < mention[1]
                    else m_body.start() - mention[1]
                    if mention[1] <= m_body.start()
                    else mention[0] - m_body.end()
                ),
            )[2]
            # Which side of the cell a claim belongs to is not a guess. A `GET`
            # has no request body, so a body written against one is quoting the
            # answer; so is a body the document itself introduces as one, with
            # `→` or with the word. Everything else is what the client sends,
            # and letting a request borrow the response vocabulary is how
            # `{agent: [...]}` posted to the order route would pass a check the
            # server rejects.
            answer = bool(ANSWER_CUE_RE.search(scope[: m_body.start()])) or bound.startswith("GET ")
            allowed: set[str] = set()
            hit = auth["routes"].resolve(bound)
            if not hit.empty:
                row = hit.one
                allowed |= row["response_keys"] if answer else row["keys"]
                if row["guarded"]:
                    allowed |= auth["envelope"]
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
                    f"`{literal}` names {', '.join(stray)} — not contracted for {bound}",
                )

        if named and not exempt:
            for code in set(STATUS_RE.findall(scope)):
                if code != "409":
                    continue
                scale["status branches"] += 1
                unguarded = sorted(
                    r
                    for r in named
                    for hit in [auth["routes"].resolve(r)]
                    if not hit.empty and not hit.one["guarded"]
                )
                if unguarded:
                    add(f"L{line_no}", f"a 409 branch is claimed for {' / '.join(unguarded)}, which `api.md` does not guard")

        for schema in SCHEMA_CITE_RE.findall(scope):
            scale["schema citations"] += 1
            if auth["schema files"].resolve(schema).empty:
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
            source = origin.read(rel)
            if source is None:
                add(f"L{line_no}", f"`{rel}` is not a file in {origin.label}")
                continue
            if rel not in read_files:
                read_files.add(rel)
                for name in defined_symbols(source):
                    symbols.define(f"{rel}:{name}", rel, content=rel, where=rel)
            if symbol and symbols.resolve(f"{rel}:{symbol}").empty:
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
    for line_no, field, drawn, cited, contracted in mapping_tables(doc):
        scale["contract mapping tables"] += 1
        hit = auth["schema fields"].resolve(field)
        if cited:
            # The lead-in named which schemas it is quoting, so the citation is
            # `<file>::<field>` and a field of the same name in another file is
            # not an answer to it.
            hit = Match(
                hit.universe,
                field,
                tuple(t for c in sorted(cited) for t in auth["schema fields"].resolve(f"{c}::{field}").hits),
                tuple(p for c in sorted(cited) for p in auth["schema fields"].resolve(f"{c}::{field}").payloads),
            )
        owners = {p["path"]: p["values"] for p in hit.payloads}
        if not owners:
            # A header that carries `[contract]` has asserted that some schema
            # owns this vocabulary. Skipping the lookup when nothing resolves is
            # the silent-green failure this class exists to prevent: a mistyped
            # field or a wrong schema citation would leave the table mapping no
            # contracted field at all, and the gate would call it covered. A
            # header without the marker is drawing something the spec itself
            # decided, and owes no lookup.
            if contracted:
                add(
                    f"L{line_no}",
                    f"`{field}` is marked `[contract]` and rendered totally, but no contract schema "
                    f"declares it{' among ' + ', '.join(sorted(cited)) if cited else ''} — "
                    f"the table maps no contracted field",
                )
            continue
        if len(owners) > 1:
            add(
                f"L{line_no}",
                f"`{field}` is rendered totally here, and its authority declares it at {len(owners)} "
                f"independent places ({', '.join(sorted(owners))}); the table names none of them, "
                f"so a reading of either satisfies the sentence",
            )
            continue
        declared_at, values = next(iter(owners.items()))
        if drawn != values:
            add(
                f"L{line_no}",
                f"`{field}` renders {sorted(drawn)} against `{declared_at}`'s {sorted(values)} — "
                f"missing {sorted(values - drawn)}, extra {sorted(drawn - values)}",
            )

    return findings, scale


def check(target: str | Path = SPEC, *, authorities: str | Path | None = None) -> dict[str, Any]:
    """`target` is the spec path, a git rev, or a checkout holding the spec.

    `authorities` names where `api.md`, the schemas and the cited Python are read
    from. It defaults to the target's own origin and is reported either way; pass
    it only for a document that lives outside any checkout, which is the one case
    that has no authorities to default to.
    """
    text, mode, origin = resolve_inputs(target, authorities)
    doc = Document(text)
    fingerprint = doc.fingerprint
    p = parse(doc)
    reg = p["register"]
    findings: list[dict[str, str]] = []

    copy_u = p["universes"]["copy"]
    slot_u = p["universes"]["slots"]
    state_u = p["universes"]["states"]
    treatment_u = p["universes"]["treatments"]
    frame_u = p["universes"]["frames"]

    states = {r["state"] for r in reg}
    reg_frames = {r["frame"].lstrip("§") for r in reg}
    reg_by_section: dict[str, list[dict[str, Any]]] = {}
    for r in reg:
        reg_by_section.setdefault(r["frame"].lstrip("§"), []).append(r)

    def add(cls: str, where: str, msg: str) -> None:
        findings.append({"class": cls, "where": where, "message": msg})

    # ---- A ------------------------------------------------------------------
    auth = load_authorities(origin)
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
    gaps = p["universes"]["gaps"]
    # Keyed by every line a scope spans, not by the line it starts on: a route
    # sits three lines into a paragraph as often as on its first line, and a map
    # keyed by starts silently misses those.
    scope_at: dict[int, str] = {}
    for ln, s in doc.claims():
        for offset in range(s.count("\n") + 1):
            scope_at[ln + offset] = s
    # One finding per route, decided per occurrence. Deduplicating first read the
    # exemption of whichever mention came earliest in the file and applied it to
    # every other mention: one paragraph carrying a `[contract-gap]` marker
    # silenced the same route everywhere else, including where it was drawn as a
    # live affordance owing §0.8 a state. An excuse belongs to the sentence that
    # writes it, so the verdict is reached first and only then collapsed.
    scanned: set[str] = set()
    reported: set[str] = set()
    for sec, line, call in p["routes"]:
        scanned.add(call)
        if call in covered_routes or call in reported:
            continue
        if cites_a_registered_gap(gaps, scope_at.get(line, "")):
            continue
        reported.add(call)
        add("A", f"§{sec} L{line}", f"{call} is named by no §0.8 row")
    for r in reg:
        cell = r["failure"]
        if not cell or cell == "—":
            add("A", f"L{r['line']}", f"「{r['state']}」 states no failure treatment")
            continue
        # A treatment is resolved, not pattern-matched. `TREAT_RE` accepted any
        # `F1`–`F5` and could not see an `F6`: the regex simply failed to match
        # and the cell fell through to the *next* branch, where a `→ State` or
        # nothing at all decided the verdict. A sixth treatment is the one thing
        # §0.8 says is closed, and it was the one thing the check could not say.
        cited_treatments = sorted(set(re.findall(r"\bF\d+\b", cell)))
        if cited_treatments:
            for t in cited_treatments:
                if treatment_u.resolve(t).empty:
                    add("A", f"L{r['line']}", f"「{r['state']}」 names {t}, which §0.8's closed set does not define")
            continue
        if re.search(r"(?:As |→\s*)§\d", cell):
            continue
        goto = GOTO_RE.search(cell)
        frame = r["frame"].lstrip("§")
        if goto:
            targets = [t.strip() for t in re.split(r"[/,]| or ", goto.group(1)) if t.strip()]
            # `any(st == t or st.startswith(t) or t in st ...)` — a state named
            # 「Ready」 vouched for a cell pointing at 「Read」, and a cell
            # pointing at 「Saving」 was answered by 「Saving order」 in a frame
            # that has both. A target now resolves against this row's own frame
            # (「Ready」 is a different state in every frame) and has to land on
            # exactly one row: none means the exit points nowhere, several means
            # it points at two states at once, and neither is an exit.
            hits = [state_u.resolve(f"{frame} · {t}") for t in targets]
            if targets and all(not h.empty and not h.ambiguous for h in hits):
                continue
        add("A", f"L{r['line']}", f"「{r['state']}」 failure cell names no F1–F5 and no known state: {cell!r}")

    # Class A asks that every route the document names have a state. Nothing
    # asked the mirror, and the mirror is where this document keeps breaking: a
    # capability `api.md` contracts that no surface reaches. Three §0.5 rows and
    # two review findings across two heads were all this one shape, each found by
    # hand. A read may go undrawn — a screen chooses what to show. A mutation is
    # a user action, and an action nobody can take is either a frame that was
    # never written or an absence the document owes the reader out loud.
    # Two registers can account for a route the states do not reach, and both
    # cost a written sentence: §0.5 says the affordance is missing here, §0.4
    # says the affordance belongs to another surface. Silence is the only
    # answer this refuses.
    #
    # Both registers are read as sections, not as shapes found anywhere. A §0.4
    # row moved out of §0.4 in an edit went on excusing its route, and a row
    # merely shaped like a gap registration went on excusing its own: an excuse
    # has to be written where the document says excuses are written, or it is
    # not an excuse.
    accounted: set[str] = set(covered_routes)
    excusing = doc.scope("scope note") + [
        (n, line) for n, line in doc.scope("gap registry") if GAP_ROW_RE.match(line)
    ]
    for _n, line in excusing:
        accounted.update(normalize_route(m, path) for m, path in ANY_ROUTE_RE.findall(line))
    contracted_mutations = sorted(
        r for r in auth["routes"].tokens() if r.startswith(("POST ", "PUT ", "PATCH ", "DELETE "))
    )
    for route in contracted_mutations:
        if route in accounted:
            continue
        add("A", "§0.8", f"{route} is contracted and reached by no §0.8 row, no §0.5 gap and no §0.4 row")

    # ---- B ------------------------------------------------------------------
    cited: set[tuple[str, str]] = set()
    for sec, line, k in p["refs"]:
        cited.add((k, f"§{sec} L{line}"))
    for r in reg:
        for k in KEY_REF_RE.findall(r["copy"]):
            cited.add((k, f"§0.8 L{r['line']}"))
    for key, where in sorted(cited):
        hit = copy_u.resolve(key)
        if hit.empty:
            add("B", where, f"key `{key}` is cited and never defined")
        elif hit.ambiguous:
            add(
                "B",
                where,
                f"key `{key}` is cited and answers to {len(hit.hits)} definitions "
                f"({', '.join(hit.hits)}); the citation names none of them",
            )
    for _token, rows_ in copy_u.items():
        for row in rows_:
            if not row["en"]:
                add("B", f"L{row['line']}", f"key `{row['key']}` has no English column")
            for slot in sorted(set(SLOT_RE.findall(row["zh"] + row["en"]))):
                if slot_u.resolve(slot).empty:
                    add(
                        "B",
                        f"L{row['line']}",
                        f"key `{row['key']}` interpolates `{{{{{slot}}}}}` with no §0.9 row",
                    )

    # ---- C ------------------------------------------------------------------
    for r in reg:
        if not r["exit"] or r["exit"] == "—":
            add("C", f"L{r['line']}", f"「{r['state']}」 has no exit")
    for sec in sorted(p["inventories"]):
        if sec not in reg_frames:
            add("C", f"§{sec}", f"§{sec} draws an element inventory and has no §0.8 row")
    # And the mirror: a register row naming a frame that is not a §1 section.
    # Nothing asked it, so a row could point at §1.60 — or at a section deleted
    # in a later round — and its whole frame would quietly stop being checked,
    # because every other class reaches a row *through* its frame.
    for frame in sorted(reg_frames):
        if frame_u.resolve(frame).empty:
            row = next(r for r in reg if r["frame"].lstrip("§") == frame)
            add("C", f"L{row['line']}", f"§0.8 rows are filed under §{frame}, which is no §1 section")
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
    reg_cited = cited_rows({t for r in reg for t in KEY_REF_RE.findall(r["copy"])}, copy_u)
    conditions = [
        row
        for _token, rows_ in copy_u.items()
        for row in rows_
        if CONDITION_RE.search(row["key"])
    ]
    for row in sorted(conditions, key=lambda r: r["line"]):
        if row["line"] in reg_cited:
            continue
        add("D", f"L{row['line']}", f"condition key `{row['key']}` is cited by no §0.8 row")

    # ---- E ------------------------------------------------------------------
    e_findings, e_scale = authority_claims(doc, auth, origin, reg, gaps)
    findings.extend(e_findings)

    # ---- the duplicate rule, for every universe at once ---------------------
    # Not a class. One canonical token declared twice with different content is
    # the same defect wherever it happens, so it is reported wherever it
    # happens, attributed to the class that reads the table it lives in. Before
    # this, exactly one table was checked for it — the copy tables — because
    # that is the one a reviewer happened to catch; §0.9, §0.8 and `api.md`
    # could each say a thing twice and the later row silently won.
    universes = list(p["universes"].values()) + [
        auth["routes"], auth["schema files"], auth["schema fields"], auth["repo symbols"]
    ]
    for u in universes:
        for token, first, second in u.duplicates:
            where = f"L{second}" if isinstance(second, int) else str(second)
            prior = f"L{first}" if isinstance(first, int) else str(first)
            add(
                u.owner,
                where,
                f"`{token}` is defined twice in {u.name} with different content (also {prior})",
            )

    scale = {
        "register rows": len(reg),
        "distinct states": len(states),
        "frames with a register row": len(reg_frames),
        "frame sections with an element inventory": len(p["inventories"]),
        "mutating calls scanned": len(scanned),
        "contracted mutations to reach": len(contracted_mutations),
        "copy tables / rows": f"{p['tables']} / {p['rows']}",
        "copy keys defined": len(copy_u),
        "condition-named keys": len(conditions),
        "interpolation slots declared": len(slot_u),
        "failure treatments declared": len(treatment_u),
        "frame sections declared": len(frame_u),
        "prose key references": len(p["refs"]),
        "authority: contracted routes read": len(auth["routes"]),
        "authority: schema enum declarations": len(auth["schema fields"]),
        "authority: route claims": e_scale["routes"],
        "authority: request/response body claims": e_scale["bodies"],
        "authority: guarded-status claims": e_scale["status branches"],
        "authority: schema citations": e_scale["schema citations"],
        "authority: counted-vocabulary claims": e_scale["vocabulary claims"],
        "authority: contract mapping tables": e_scale["contract mapping tables"],
        "authority: repo symbol citations": e_scale["repo symbols"],
    }
    empty = [k for k, v in scale.items() if v == 0 and k not in TARGET_ZERO]
    broken = self_test(auth, origin)
    # A declared range nobody asks for is a constraint that binds nothing — the
    # arm was deleted, or renamed, or quietly went back to reading everything.
    # Cheap to check here, and the only place that can see all of it at once.
    unread = sorted(set(SCOPES) - doc.requested)
    return {
        "ok": not findings and not empty and not broken and not unread,
        "input_mode": mode,
        "input_fingerprint": fingerprint,
        "authority_origin": origin.label,
        "input_scale": scale,
        "empty_inventories": empty,
        "broken_arms": broken,
        "unread_scopes": unread,
        "findings": findings,
    }


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else str(SPEC)
    authorities = sys.argv[2] if len(sys.argv) > 2 else None
    r = check(target, authorities=authorities)
    print(f"input mode        : {r['input_mode']}")
    print(f"input fingerprint : {r['input_fingerprint']}")
    print(f"authorities read  : {r['authority_origin']}")
    print("input scale (self-generated in this run):")
    for k, v in r["input_scale"].items():
        zero_ok = " (target zero, arms self-tested)" if k in TARGET_ZERO else ""
        print(f"  {k:<42}: {v}{zero_ok}")
    if r["unread_scopes"]:
        print()
        print("FAIL: a declared parse scope was read by no arm")
        for k in r["unread_scopes"]:
            print(f"  {k} — {SCOPES[k].where} ({SCOPES[k].why})")
        return 2
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
    for cls in CLASSES:
        items = by.get(cls, [])
        print(f"[{cls}] {CLASS_LABELS[cls]}: {len(items)}")
        for f in items:
            print(f"   {f['where']:<16} {f['message']}")
    print(f"\ntotal gaps: {len(r['findings'])}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
