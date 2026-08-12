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

  A  a mutating call §1 names that no §0.8 row states a treatment for, a §0.8
     row whose failure cell is empty or names a treatment that does not exist, or
     a route §1 names by bare method word instead of by literal
  B  a copy key cited but never defined, a key row with no English column, a
     `{{slot}}` with no §0.9 row, or a §0.9 row whose declared consumers are not
     the keys that interpolate it
  C  a §0.8 row with no exit, a frame section that draws an element inventory
     and contributes no §0.8 row, or a §1 claim attributing a state to a frame
     the register does not file that state under
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

Three of those arms compare the document against something outside it. The
remaining generator is the document against *itself*: a set this spec registers
in a §0 table, and then enumerates a second time in §1 prose, where the two
enumerations disagree. Nineteen review rounds found those by hand, and the
signature never changed — the register gets edited correctly and the derived
sentence does not. Two arms below mechanise it, and both govern §1 prose only.
The registers are exempt on purpose: a register is the definition of its set,
and checking a definition against itself reports the definition as the gap.

Making that comparison possible has a precondition, which is the third arm. A
sentence that names another frame's route as "§1.3's whole-order `PUT`" is
invisible to every arm here — there is no token to resolve — so it can contradict
the register indefinitely and read fine. Bare method words are therefore a gap in
their own right in §1 prose: not a style rule, but the difference between a claim
this file can check and one it cannot see.

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
            # `self.tree / rel` with `rel` coming out of the document under
            # review: the spec chose which bytes the gate validates it against.
            # `../outside.py:list_agents` read a file that is not in the
            # checkout at all, and the answer — five citations, no gaps — was a
            # statement about a repository nobody selected. The `git show` half
            # never had the hole, because a rev has no way to name a path
            # outside its own tree; the filesystem half is the one that needed
            # telling. One origin means one origin, including when the sentence
            # being checked is the thing proposing otherwise.
            path = self.tree / rel
            try:
                inside = path.resolve().is_relative_to(self.tree.resolve())
            except OSError:
                return None
            if not inside:
                return None
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
# Candidate first, validity second. A method typo still has the unmistakable
# shape of a route claim and must reach the route universe instead of vanishing
# before it can be rejected.
ROUTE_CANDIDATE_RE = re.compile(r"`([A-Za-z][A-Za-z0-9_-]*) (/api/[^`]*)`")

# A method word on its own, with no path after it. `ANY_ROUTE_RE` is what the
# route arms resolve; this is what they cannot, and the two together are how many
# times §1 prose reaches for a route at all.
BARE_METHOD_RE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE)`")
# `(`POST` / `PUT` / `PATCH` / `DELETE`)` names the method set, not any route —
# a bare method flanked by a slash-joined sibling is the vocabulary, not a call.
METHOD_LIST_RE = re.compile(r"`\s*/\s*`")
# Which §1 frame a sentence points at. Only `1.` — a §4 or §0 reference is a
# pointer into the contract or the registers, and neither is a frame claim.
FRAME_REF_RE = re.compile(r"§(1\.\d+)")
# A frame heading gives the same frame three names — section, display number,
# node id — and §1 prose uses all three ("a state of 09", "Deltas from 01").
FRAME_NAME_RE = re.compile(r"Frame\s+(\d+)\s+`(\w+)`")
# What could be one of the other two: a backticked identifier, or a two-digit
# number standing alone. A digit, a dot or a dash on either side disqualifies it,
# because the registers number their rows that way — G-10 is a gap, not Frame 10.
FRAME_ALIAS_RE = re.compile(r"`(\w+)`|(?<![\w.\-])(\d{2})(?![\w.\-])")
# A reference to another frame, and the state in it this cell names — the two
# halves of 「→ §1.0 Unreachable」, both of which have to resolve.
CROSS_FRAME_RE = re.compile(
    r"(?:As\s+|→\s*)§(\d+\.\d+)(?:\s+\*?([^/|—;.,*]+))?"
)
# The document's own marker for a normative claim, and the reason this file can
# tell one apart from the narration around it. See the class C attribution arm.
BOLD_RUN_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
# Sentence enough for these two arms: they ask whether one claim names a route
# and another frame *together*, and a paragraph is too wide to answer that. `.`
# and `;` end a clause here; `. ` inside a route or a key cannot occur, because
# both are backticked and neither contains a space.
SENTENCE_RE = re.compile(r"(?<=[.;])\s+")


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


def route_query(path: str) -> tuple[tuple[str, str], ...]:
    """Return exact query claims while route coverage remains path-grained.

    A route mention may omit its query when discussing the endpoint as a whole.
    Once it spells a query, however, the names and values are a contract claim
    and must not disappear through `normalize_route`.
    """
    _path, marker, query = path.partition("?")
    if not marker:
        return ()
    return tuple(
        sorted(
            (name.strip(), value.strip())
            for part in query.split("&")
            for name, separator, value in (part.partition("="),)
            if separator and name.strip() and value.strip()
        )
    )


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
#   malformed  a row this universe's own reader recognises as its family and
#              cannot read is a finding, reported against the row. The other
#              three rules judge what got in; this one judges the door.
#
# The rules are properties of the comparison, so they arrive with it. A class
# added later cannot opt out of `duplicate` by forgetting to write it, and
# cannot weaken `token` for its own convenience, because it has no comparison of
# its own to weaken.
#
# `malformed` is the fourth because three review heads in a row found the same
# thing, six sites at a time: a pattern that both *finds* an input and *validates*
# it answers a shape it does not know by producing nothing, so a row the document
# plainly wrote is dropped before any rule can be applied to it — and dropping it
# from the reading drops it from the gate without dropping it from the table. Two
# outcomes follow, and both were live here. Where the dropped row is a definition
# something cites, the citations report `empty` and the finding names eleven
# innocent rows instead of the one broken one. Where the row still registers with
# a truncated field — §0.5's did — nothing reports at all.
#
# Widening the six patterns was the previous head's answer and is why there was a
# next round: it fixes the sites a reviewer read. Stating the rule here puts the
# question 「what does this reader do with its own family, malformed?」 on the
# grid, where a cell with neither a case nor a written reason fails the suite.
# The answer may legitimately be 「the shape cannot be wrong」 — a universe read
# out of an AST or a JSON schema has no rows to break — and that is what the
# exemptions say. What it may no longer be is unasked.

RULES = ("token", "empty", "duplicate", "malformed")
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
    stable = re.sub(r"\s*`\[(?:contract|derived|frame|spec[^]]*)\]`", "", name).strip()
    spellings = {name, stable}
    ordinal = ORDINAL_RE.match(stable)
    if ordinal:
        spellings.add(ordinal.group(1))
    head = re.split(r"\s*[(,]", stable, maxsplit=1)[0].strip()
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
        return "*" in self.citation

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
        self.unreadable: list[tuple[Any, str]] = []

    def malformed(self, where: Any, said: str) -> None:
        """This universe's reader met a row of its own family and could not read it.

        Recorded here rather than reported at the call site so that the finding
        carries the universe's own class without every reader having to know
        which class that is — the same reason `duplicates` lives here. The
        reader's job is to notice; attribution is the universe's.
        """
        self.unreadable.append((where, said))

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
        """The one comparison. Everything answering to the name, exact and alias.

        A citation ending in `*` names a family, and a family is still matched by
        token: the prefix is split into segments and compared segment for
        segment, so `sourceDetail.tiers.*` reaches `sourceDetail.tiers.add` and
        never reaches `sourceDetail.tiersAdd`. String prefixing would reach both.

        The exact and alias halves are unioned rather than tried in order.
        Preferring the token made one spelling answer for two things and report
        as though it answered for one: a file with a module-level `load` and a
        `ConfigStore.load` registers `service.py:load` as a token *and* as an
        alias of the method, and stopping at the token called the citation
        unambiguous while the reader still had two places to go. Ordering is what
        a lookup does; this is an inventory, and an inventory has to answer with
        everything that answers to the name — that is the whole basis on which
        `Match.ambiguous` means anything. `define` never records a self-alias, so
        the union cannot manufacture a second hit out of one definition.
        """
        cite = citation.strip().strip("`")
        if "*" in cite:
            pattern = segments(cite)

            def matches(candidate: str) -> bool:
                parts = segments(candidate)
                if pattern[-1] == "*":
                    return len(parts) >= len(pattern) - 1 and all(
                        re.fullmatch(re.escape(wanted).replace(r"\*", ".*"), actual)
                        for wanted, actual in zip(pattern[:-1], parts)
                    )
                return len(parts) == len(pattern) and all(
                    re.fullmatch(re.escape(wanted).replace(r"\*", ".*"), actual)
                    for wanted, actual in zip(pattern, parts)
                )

            hits = {t for t in self._payload if matches(t)}
            for alias, targets in self._alias.items():
                if matches(alias):
                    hits |= targets
        else:
            hits = set(self._alias.get(cite, ()))
            if cite in self._payload:
                hits.add(cite)
        found = tuple(sorted(hits))
        return Match(self.name, cite, found, tuple(self._payload[t] for t in found))


def frame_refs(text: str, frames: Universe) -> set[str]:
    """Which §1 frames `text` names, under any of the three names a frame has.

    The registers file states under `§1.x`, so an arm comparing itself against
    §0.8 reaches for that spelling. Prose reaches for the other two — "Frames 09
    and 10 draw the header", "Deltas from 01" — and an arm that recognises only
    the section number reads those as *no* claim at all. That is the shape a rule
    stops being applied in: not loudly, but on whichever sentences happened to be
    written in the document's other habit. Resolution stays in the universe, so
    the three names are one identity everywhere rather than a second comparator.
    """
    named = set(FRAME_REF_RE.findall(text))
    for match in FRAME_ALIAS_RE.finditer(text):
        named.update(frames.resolve(match.group(1) or match.group(2)).hits)
    return named


def names_spoken(text: str, names: set[str]) -> set[str]:
    """Which of `names` this text says — as names, not as substrings.

    Two rules, and the second is not a stronger form of the first. A name is
    spoken where it stands on its own boundaries, so 「Not startedness」 does not
    say 「Not started」: plain containment read a longer word as its own prefix,
    and a dispatch landing in a state the register does not have came back clean.
    And a spoken name that is another spoken name's prefix yields to the longer
    one, so 「Ready」 does not answer for a cell that says 「Ready (first run)」.

    The boundary is `\\w`, which in this document also covers CJK, so a state
    name run together with Chinese prose is deliberately *not* spoken — a
    register's state names are written as their own phrases everywhere they are
    meant to be read as one.
    """
    spoken = {n for n in names if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text)}
    return spoken - {n for n in spoken if any(n != o and n in o for o in spoken)}


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
#
# `frames` is here because a failure cell may send the reader to another frame's
# state — `§1.0 Unreachable` — and both halves of that are citations. C owns the
# universe; A is a second reader of it, which is why the frame rules it cannot
# reach are exempted per-arm rather than left to look covered by C's cases.
CLASS_UNIVERSES: dict[str, tuple[str, ...]] = {
    "A": ("routes", "states", "treatments", "frames", "gaps"),
    "B": ("copy", "slots"),
    # C reads `states` for the same reason A reads `frames`, from the other end:
    # an exit cell names where the state goes, and a named landing is a citation
    # of the register — the universe A owns. Declared here rather than left off
    # because a class that resolves against a universe it does not declare gets
    # no cells in the tiled suite, which is the arrangement that let the exit
    # column go unresolved for thirty rounds.
    "C": ("frames", "states"),
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
    "A": "mutating call with no treatment, a treatment that does not exist, a route named by method word, or a failure dispersed into another frame's states as a set that frame does not have",
    "B": "copy cited but not defined, missing localized text, an undeclared slot, a misdeclared slot consumer, a line no copy row renders, or a state whose one key contradicts its frame's own mapping table",
    "C": "state with no exit, a state its frame's own value table reaches that the dispatching row never lands in, a frame with no register row, or a state attributed to the wrong frame",
    "D": "condition key no state cites",
    "E": "a claim about the system that its authority file does not make",
}
assert set(CLASS_LABELS) == set(CLASSES), "a gate class with no label prints as nothing"

KEY_REF_RE = re.compile(r"`([a-z][A-Za-z0-9]*(?:\.[A-Za-z0-9_*]+)+)`")
# A run of key citations joined by nothing but list punctuation — what this
# document writes when it means 「these and no others」. The separators are
# LIST_COMMA_RE's plus the 「and」 that closes an English list; `/` stays out for
# the reason given there, and so does a bare space, which is prose.
_KEY_REF = KEY_REF_RE.pattern.replace("(", "(?:", 1)
KEY_LIST_RE = re.compile(rf"{_KEY_REF}(?:\s*(?:[,、]|and)\s*{_KEY_REF}){{2,}}")
COPY_KEY = r"[a-z][A-Za-z0-9._*]*"
COPY_KEY_RE = re.compile(rf"^{COPY_KEY}$")
KEY_DEF_RE = re.compile(rf"^\|\s*`({COPY_KEY})`([^|]*)\|([^|]*)\|([^|]*)\|\s*$")
COPY_HEADER_RE = re.compile(
    r"^\|\s*Key(?: under `models\.hub\.([a-zA-Z]+)\.\*`)?\s*"
    r"\|\s*中文\s*\|\s*English\s*\|\s*$"
)
# The same row shape with the cell count taken out: what a copy row *claims to
# be*, so that a row claiming it and failing it can be told from a row that was
# never a copy definition. `KEY_DEF_RE` cannot answer this question, because
# being unable to answer it is the whole of what it means for that pattern not
# to match.
KEY_ROW_OPEN_RE = re.compile(r"^\|\s*`([^`\n]+)`[^|]*\|")
SLOT_RE = re.compile(r"\{\{(\w+)\}\}")
# Two or more slots joined by the segment separator — a rendered line quoted
# whole. `·` is the one character this document uses to join the parts of a
# single string (see LIST_COMMA_RE below for why it is not a list separator),
# so it is exactly what marks prose as quoting a shape rather than naming slots.
SHAPE_RE = re.compile(r"\{\{\w+\}\}(?:[^|`\n]*?·[^|`\n]*?\{\{\w+\}\})+")
# The only punctuation this document lists with. Deliberately not `/` or `·`:
# both join the *parts* of one rendered string (网关 · 正常, 降级 / 暂不可用),
# so splitting on them would tear members apart and read the pieces as a list.
LIST_COMMA_RE = re.compile(r"[,、]")
TREAT_RE = re.compile(r"\bF([1-5])\b")
TREAT_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(F(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+)"
)
GOTO_RE = re.compile(r"→\s*([^,;.]+)")
# What an exit cell says after an arrow, up to the next arrow or the end of the
# cell. Not a destination — a segment, handed whole to a lookup, because this
# document's exit cells are sentences and the destination inside one cannot be
# carved out by a pattern without getting it wrong on the rows that are right.
ARROW_SEGMENT_RE = re.compile(r"→([^→]*)")
# 「A or B」, 「A / B」 — one segment naming two landings.
ALTERNATIVE_RE = re.compile(r"\s+or\s+|\s*/\s*")
PHRASE_END_RE = re.compile(r"[,.:;—]")


def phrase(text: str) -> tuple[str, str]:
    """The opening phrase of `text`, and the punctuation that ended it."""
    body = text.strip().strip("*").strip()
    m = PHRASE_END_RE.search(body)
    return (body[: m.start()].strip() if m else body).strip("*").strip(), (m.group(0) if m else "")

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
# A response half that spells no body names its shape instead — "→ OAuth result"
# — and the shape gets a section of its own further down. The subject word is
# what links the two, so it has to be a word the file capitalises: matching on
# any word would send "Source-mutation envelope" to three unrelated sections.
SHAPE_WORD_RE = re.compile(r"\b([A-Z][A-Za-z]{2,})\b")
# One reading of a shape that has more than one, and the value that selects it.
# `api.md` spells the OAuth terminal as three bullets — one per `intent` — and
# reading them as one flat vocabulary would accept the very sentence the section
# exists to forbid: `added_to` on a reauth terminal, which no reauth carries.
SHAPE_VARIANT_RE = re.compile(r"^[-*]\s+(.*?)→\s*`(\{[^`{}]*\})`", re.M)
VARIANT_NAME_RE = re.compile(r"\"([a-z_]+)\"")
# What immediately introduces a body as the server's answer rather than the
# client's request. `api.md` writes both sides in one cell, so a claim has to
# say which side it is quoting, and these are the ways this document says it.
ANSWER_CUE_RE = re.compile(r"(?:→|returns|answers|echoes|responds with|re-echoes)\s*$")
JSONISH_RE = re.compile(r"\{[^{}]*\}")
FENCED_JSON_RE = re.compile(r"```json\n(.*?)\n```", re.S)
# The sentence api.md writes when a section stops describing one route and
# starts defining the shape *every* guarded mutation answers with. Deriving the
# definition sites from the document's own claim is what keeps the allowance
# from being "the rest of the file"; the size of the set it yields is reported
# with the other scales, so an api.md that rewords this says so out loud instead
# of silently narrowing every guarded route to nothing.
GUARD_ENVELOPE_RE = re.compile(r"every guarded [^\n]*envelope", re.I)
# What a backticked schema-file citation *looks like*, deliberately wider than
# any name `docs/plans/model-hub-contracts/` actually holds. An extraction
# pattern that doubles as an admissibility test cannot report a near miss: it
# does not reject `runtime_dependency.schema.json`, it never sees it, and the
# citation, the field attributed to it and every arm downstream go quiet
# together — a misspelling reading as agreement. The shape says "this sentence
# is citing a schema file"; whether that file exists is the universe's answer,
# and `empty` is a finding. Spelled once because three extractors ask it.
SCHEMA_FILE = r"[^`\s/]+\.schema\.json"
SCHEMA_CITE_RE = re.compile(rf"`({SCHEMA_FILE})`")
DOTTED_TOKEN_RE = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)`")
# The symbol half accepts a dotted name because `defined_symbols` produces one:
# a method is registered as `ConfigStore.load` with `load` as its alias, and a
# citation that spells the qualified name — the precise form, the one that
# resolves where the bare name is ambiguous — matched nothing and was read as a
# bare file citation with no symbol at all. The document's most careful way of
# pointing at a method was the one form the reader skipped.
# Candidate first, validity second. A malformed path is still unmistakably a
# Python-file citation; restricting the extractor to valid path characters made
# `$core/.../service.py:list_agents` disappear before the origin could reject it.
PY_CITE_RE = re.compile(r"`([^`\s]*\.py)(?::([^`\s]+))?`")
# Line-number citations are not a portable authority. They require the cited
# revision's complete object history, while the CI checkout is deliberately
# shallow, and a line can move without the contract concept changing. Reject the
# whole shape deterministically and require the stable file/symbol or contract
# anchor that the other authority arms already resolve from this checkout.
PRECISE_LINE_CANDIDATE_RE = re.compile(
    r"`(api\.md|(?=[A-Za-z0-9._-]{7,40}:)(?=[A-Za-z0-9._-]*\d)"
    r"[A-Za-z0-9._-]+):([^`\s]+)`"
)
STATUS_RE = re.compile(r"\b(4\d\d|5\d\d)\b")
COUNT_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
COUNT_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
# The head is any word, and `count_value` decides whether it is a number. Built
# as an alternation of the spellings it knew, this pattern did not *skip* the
# claims outside that list — it never saw them, so 「16 properties」 and
# 「sixteen properties」 were both uncounted while the arm reported a clean zero.
# An extractor narrower than the document reads as agreement.
#
# The noun is matched by lookahead so it is not consumed: 「the 13 properties」
# has two readings that overlap on it, and a consuming match would take the one
# headed by 「the」 and leave the number unread — the same silence, arrived at
# from the other side.
COUNT_CLAIM_RE = re.compile(r"\b([\w-]+)(?=\s+(?:\w+\s+)?(properties|values|transports)\b)")


def count_value(word: str) -> int | None:
    """How many `word` says, or `None` where it says no number at all.

    Digits, the spellings through nineteen, the tens, and the hyphenated pairs
    English builds from them (`twenty-eight`). Composition rather than a longer
    list, because a list is what silently ran out the first time.
    """
    token = word.strip().lower()
    if token.isdigit():
        return int(token)
    if token in COUNT_WORDS:
        return COUNT_WORDS[token]
    if token in COUNT_TENS:
        return COUNT_TENS[token]
    tens, sep, units = token.partition("-")
    if sep and tens in COUNT_TENS and COUNT_WORDS.get(units, 10) < 10:
        return COUNT_TENS[tens] + COUNT_WORDS[units]
    return None
# Which schema a field name belongs to, where the sentence says so outright.
# `SCHEMA_OWNS_RE` is the possessive — `` `source.schema.json`'s `models` `` —
# with room for the qualifier the document sometimes puts between them
# (`` `runtime-dependency.schema.json` v5's `status.health` ``). `ATTRIBUTED_TO_RE`
# is the other direction, where the names come first and the file closes the
# clause: 「`account_label`, `base_url` and `masked_credential` are each
# `["string","null"]` in `source.schema.json`」. `CLAUSE_END_RE` bounds how far
# back that reading may reach, because a clause is as much of a sentence as one
# attribution can honestly claim. See `attributed_fields`.
SCHEMA_OWNS_RE = re.compile(
    rf"`({SCHEMA_FILE})`[^`\n]{{0,24}}?['’]s\s+`([^`\s]+)`"
)
ATTRIBUTED_TO_RE = re.compile(rf"\b(?:in|of|from)\s+`({SCHEMA_FILE})`")
# A dotted backticked token that is a file rather than a field path. This
# document cites exactly three kinds of file — `api.md`, `*.schema.json` and the
# repo's `.py` — and each has its own arm; a clause that names one on its way to
# attributing a field must not have the file read back as the field.
FILE_TOKEN_RE = re.compile(r"\.(?:md|json|py)$")
CLAUSE_END_RE = re.compile(r"[.;:。；：]\s")


def nearest_subject(mentions: list[tuple[int, int, str]], start: int, end: int) -> str:
    """Which subject a claim written at `start`–`end` is about: the nearest named.

    A scope names several subjects and a claim inside it is about one of them, so
    binding by container — every claim to every subject — is not a reading of the
    document, it is a refusal to read it. Measured before it was written: the
    alternative, holding a claim against *every* route the scope names, reports
    three of this document's own rows where a shared refusal envelope, a shared
    query parameter and a two-step OAuth exchange are each written once against
    several routes on purpose. The counted-vocabulary arm found the same shape
    from the other direction: 「`agent-supply.schema.json`'s 13 properties」 was
    read as a claim about the `source.schema.json` named later in the same row.

    One binder for all three callers — body literals, 409 branches, counts —
    because the question is one question, and a second copy of it is where the
    two answers start to differ.

    Overlap counts as distance zero, so a claim written inside a mention binds to
    it; otherwise it is the gap on whichever side is smaller, which is how the
    sentence reads to a human.
    """
    return min(
        mentions,
        key=lambda mention: (
            0
            if mention[0] < end and start < mention[1]
            else start - mention[1]
            if mention[1] <= start
            else mention[0] - end
        ),
    )[2]


def json_keys(text: str) -> set[str]:
    """Top-level key names the fenced ```json blocks in `text` declare.

    `literal_keys` reads a body the way this document usually writes one — one
    line, no nesting. A real example block can nest, so the refusal-envelope
    reader uses this structured parser and deliberately keeps only the root
    object. That preserves the response contract without promoting nested
    `SupplyGap` fields into top-level members.
    """
    found: set[str] = set()
    for block in FENCED_JSON_RE.findall(text):
        try:
            node = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(node, dict):
            found.update(k for k in node if isinstance(k, str))
    return found


class LiteralMember(NamedTuple):
    """One body member claim, retaining every comparable part of its spelling."""

    required: bool
    types: frozenset[str]


def literal_members(text: str) -> dict[str, LiteralMember]:
    """The members a `{...}` literal declares, including requiredness and type.

    Every nonempty comma-delimited member is retained as a candidate. The old
    extractor admitted only the shape of a correct key, so malformed members
    vanished before validation. The downstream comparison owns admissibility:
    handing it the malformed name is how the malformed name gets reported.

    The trailing `?` is the contract's own optionality marker, and it was
    stripped and discarded — which made every member look alike and left the
    downstream comparison one-sided: a body may name nothing the route lacks and
    still omit something the route demands. Kept here, so the direction the
    comparison was missing has something to compare against. The type spelling
    stays beside it for the same reason: reducing `{order: string[]}` to `order`
    lets an incompatible body look identical to the contracted one.
    """
    members: dict[str, LiteralMember] = {}
    for block in JSONISH_RE.findall(text):
        for part in block.strip("{}").split(","):
            declared, separator, member_type = part.partition(":")
            declared = declared.strip().strip('"').strip()
            name = declared.rstrip("?").strip()
            if not name:
                continue
            previous = members.get(name)
            types = frozenset({re.sub(r"\s+", "", member_type)}) if separator else frozenset()
            members[name] = LiteralMember(
                required=(previous.required if previous else True) and not declared.endswith("?"),
                types=(previous.types if previous else frozenset()) | types,
            )
    return members


def literal_keys(text: str) -> set[str]:
    """Just the names, for every reader that does not care which are required."""
    return set(literal_members(text))


def spelled_shapes(api_text: str) -> dict[str, dict[str, dict[str, LiteralMember]]]:
    """Every shape this file names in a cell and spells in a section, by heading.

    A route row can spell only what fits in a cell; what does not fit is named
    there — "→ OAuth result" — and written out below. The writing-out is also
    where a shape that has more than one reading says which reading carries
    what, so each section is returned as its readings: the selecting value to
    that reading's members, plus `""` for the section's whole vocabulary.
    """
    shapes: dict[str, dict[str, dict[str, LiteralMember]]] = {}
    for block in re.split(r"^## ", api_text, flags=re.M)[1:]:
        heading, _, body = block.partition("\n")
        readings: dict[str, dict[str, LiteralMember]] = {"": literal_members(body)}
        for selector, literal in SHAPE_VARIANT_RE.findall(body):
            for name in VARIANT_NAME_RE.findall(selector):
                merged = dict(readings.get(name, {}))
                for member, claim in literal_members(literal).items():
                    previous = merged.get(member)
                    merged[member] = LiteralMember(
                        required=(previous.required if previous else True) and claim.required,
                        types=(previous.types if previous else frozenset()) | claim.types,
                    )
                readings[name] = merged
        shapes[heading.strip()] = readings
    return shapes


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


def property_tails(node: Any, path: str = "") -> set[str]:
    """Every property a schema declares, as each dotted tail it can be cited by.

    `schema_vocabulary` answers "does this file declare that name anywhere",
    which is the right question for a one-word citation and the wrong one for
    `status.health`: reduced to its last segment, a real leaf under the wrong
    parent answered a claim about a parent it has nothing to do with, so
    `manifest.health` passed on the strength of `status.health`. Where the prose
    supplies the parents, the parents are part of the claim.

    Tails rather than full paths, because this document names a field by any
    tail — `health`, `status.health`, `RuntimeDependency.status.health` — and all
    three are the same claim. Declaring the tails is what lets the comparison
    stay one `in`: a suffix test would also accept `tus.health`.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            for name, child in props.items():
                here = f"{path}.{name}" if path else name
                parts = segments(here)
                found |= {".".join(parts[i:]) for i in range(len(parts))}
                found |= property_tails(child, here)
        for name, child in node.items():
            # `items`, `$defs`, `oneOf` and the rest keep the path they are
            # written under, which is what makes `named_agents[].supply_status`
            # come back as `named_agents.supply_status`.
            if name != "properties":
                found |= property_tails(child, path)
    elif isinstance(node, list):
        for child in node:
            found |= property_tails(child, path)
    return found


def attributed_fields(scope: str) -> set[tuple[str, str]]:
    """(schema file, field name) pairs this scope states outright.

    A backticked identifier is not on its own a claim about a schema — this
    document backticks copy keys, enum values, commit ids, frame ids and English
    nouns — so a gate that resolved *every* one of them against whatever schema
    the paragraph happens to cite would report thirty of this file's own
    sentences and be switched off within a round. What is checkable is an
    attribution the prose actually makes: the possessive, and the clause that
    ends by naming the file. Both spellings were taken from the sentences that
    already use them, and both are what a reader follows to check the claim by
    hand.

    So the reach here is the sentences that say where a field lives, not every
    sentence that mentions one, and the scale line prints how many it found —
    the same honesty the body-literal arm keeps. What this catches is the class
    the round-18 review named: a field name gone stale or misspelt while its
    citation still points at the file, which reads as verified and is not.
    """
    pairs: set[tuple[str, str]] = set()
    for m in SCHEMA_OWNS_RE.finditer(scope):
        pairs.add((m.group(1), m.group(2)))
    for m in ATTRIBUTED_TO_RE.finditer(scope):
        clause = CLAUSE_END_RE.split(scope[: m.start()])[-1]
        for token in DOTTED_TOKEN_RE.findall(clause):
            if not FILE_TOKEN_RE.search(token):
                pairs.add((m.group(1), token))
    return pairs


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
    for n, line in enumerate(api_text.split("\n"), 1):
        if not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        m = API_ROW_RE.match(cells[0])
        if not m:
            continue
        # The authority side has the same door the spec side has, and it was
        # standing just as open: a row whose first cell *is* a route and whose
        # shape cell is gone left the route undefined, which does not read as
        # "unchecked". It shrinks the contracted-mutation inventory, so class A
        # stops asking whether anything reaches that route, and every spec claim
        # about it reports as uncontracted — eleven innocent findings and the
        # broken row in none of them, the same shape §0.8's register had.
        if len(cells) < 2:
            routes.malformed(
                f"{API_CONTRACT}:{n}",
                f"{API_CONTRACT} row {cells[0]} names a route and carries no shape cell",
            )
            continue
        # `request → response` in one cell, and only the left side is what a
        # client may send. Reading the whole cell let a response field stand in
        # for a request field: the source-order row answers `{agent: AgentSupply}`,
        # so a spec that posted `{agent: [...]}` to it passed a check the server
        # would reject. `guarded` still reads the whole cell, because that is
        # where the refusal envelope is named.
        request, _, response = cells[1].partition("→")
        guarded = "guarded" in cells[1].lower() or "force" in cells[1]
        statuses = set(STATUS_RE.findall(cells[1]))
        if guarded:
            statuses.add("409")
        routes.define(
            normalize_route(m.group(1), m.group(2)),
            {
                "keys": literal_keys(request),
                "required": literal_members(request),
                "response_keys": literal_keys(response),
                "response_required": literal_members(response),
                "response_readings": {},
                "named_answer": "",
                "guarded": guarded,
                "statuses": statuses,
                "query": route_query(m.group(2)),
                "cell": cells[1],
            },
            content=cells[1],
            where=cells[0],
        )
    # The shared envelopes are defined in prose and in the JSON examples, and a
    # guarded route's row names them rather than spelling them out. Read as
    # "everything that is not a route row", that allowance was every response
    # shape in the file: `recovered` belongs to the OAuth terminal and nothing
    # else, and a claim that the chain `PUT` answers `{chain, removed_hops,
    # interrupted, recovered}` was waved through by a section about a different
    # route. An allowance collected from everywhere is not an allowance.
    #
    # So the sections are chosen by the sentence that makes them the definition —
    # api.md says which one governs *every* guarded mutation — and the keys come
    # from parsing the fenced JSON rather than from brace-matching it: the
    # refusal envelope nests `SupplyGap` inside `would_interrupt`, and the
    # innermost-braces reading returned that nested shape and called it the
    # envelope, dropping `ok`, `error` and both report arrays.
    envelope: set[str] = set()
    for block in re.split(r"^## ", api_text, flags=re.M)[1:]:
        _heading, _, body = block.partition("\n")
        cue = GUARD_ENVELOPE_RE.search(body)
        example = FENCED_JSON_RE.search(body, cue.end()) if cue else None
        if example:
            # The section later spells the nested SupplyGap object separately.
            # Only the first example after the envelope definition is the
            # top-level response contract.
            envelope |= json_keys(example.group(0))

    # A route that answers a named shape is contracted as exactly as one that
    # spells its body — the spelling is just somewhere else. Left unread, both
    # OAuth result rows contracted no answer at all, which does not read as "not
    # checked": every claim about that answer fails, the true ones included, so
    # the only way to write the terminal shape was prose the arm cannot check.
    # Both are resolved by the subject word the cell uses, and only when it
    # names exactly one section — a name that reaches none, or several, is not a
    # name, and the row keeps the empty answer it already had. The guarded rows
    # name a shared envelope instead, which the guard branch already allows.
    shapes = spelled_shapes(api_text)
    for _token, row in routes.items():
        if row["response_keys"] or row["guarded"]:
            continue
        named = [
            (head, readings)
            for head, readings in shapes.items()
            if any(word in head for word in SHAPE_WORD_RE.findall(row["cell"].partition("→")[2]))
        ]
        if len(named) == 1:
            row["named_answer"], readings = named[0]
            row["response_keys"] = set(readings[""])
            row["response_required"] = readings[""]
            row["response_readings"] = {k: v for k, v in readings.items() if k}

    schemas: dict[str, set[str]] = {}
    paths: dict[str, set[str]] = {}
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
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as broken:
            # A schema file that stops parsing used to end the run in a
            # traceback — loud, but not a verdict, and it takes the other
            # thirteen universes down with it. Reported instead, by the
            # universe whose family the file is, so the run still produces the
            # findings it can and names the one thing it could not read.
            files.malformed(
                str(CONTRACTS / name_json),
                f"`{path.name}` is a schema file this run cannot parse "
                f"({broken.msg} at line {broken.lineno})",
            )
            continue
        schemas[path.name] = schema_vocabulary(doc)
        paths[path.name] = property_tails(doc)
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
        "schema paths": paths,
        "properties": properties,
        "enums": enums,
        "schema files": files,
        "schema fields": fields,
    }


def defined_symbols(source: str) -> list[tuple[str, str, int]]:
    """The names a Python file defines that a reader can go and look at.

    Returns `(qualified, bare, line)` — `ModelHubService.load`, `load`, 412 —
    rather than a flat set of bare names. A set was the shape that erased the
    rule this inventory is judged by: every symbol in a file was registered
    under its bare name with the *file* as its content, so one file defining
    `load` four times declared one token four times with identical content, and
    the duplicate rule could not fire on `repo symbols` however wrong the file
    got. An inventory that cannot contradict itself is not an inventory; it is a
    membership test wearing one.

    With the pair, the two halves separate and each gets the verdict it deserves.
    The qualified name is the identity, so two bodies under one qualified name —
    a class declaring `load` twice, the second silently winning — is a
    `duplicate`. The bare name is an alias, so `service.py:load` in a file with
    four of them resolves to four hits and is `ambiguous`: the citation promised
    the reader one place to go and named four.

    `line` is the content, because what makes two defs different is that they are
    two bodies. Module-level assignments carry no line: rebinding a global is not
    a second definition, and there is exactly one of it to go and look at.

    The descent rule is unchanged and is one line: a name bound inside a function
    body belongs to that body. So it records defs and classes and skips their
    bodies — entering class bodies, because a method is addressable and is how
    the live citations name their functions, and never entering function bodies,
    lambdas or comprehensions, whose bindings are local by definition.
    """
    found: list[tuple[str, str, int]] = []

    def scan(node: ast.AST, path: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((".".join(path + (child.name,)), child.name, child.lineno))
                continue
            if isinstance(child, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                continue
            if isinstance(child, ast.ClassDef):
                found.append((".".join(path + (child.name,)), child.name, child.lineno))
                scan(child, path + (child.name,))
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                found.append((".".join(path + (child.id,)), child.id, 0))
            scan(child, path)

    scan(ast.parse(source), ())
    return found


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
    "key names": Scope("*", "a set enumerated outside the section that declares it must still be that set"),
    "rendered shapes": Scope("*", "a string quoted outside its copy row is that row's string or it is nothing"),
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
    broken_rows: list[dict[str, Any]] = []
    # Bounded by its own header, the way the copy reader is bounded by its own.
    # §0.8 also holds the five-treatment table, and "any `|` row in this scope"
    # took that in too — harmless while a wrong cell count was silently dropped,
    # and six false findings the moment a wrong cell count became a finding. A
    # reader that reports what it cannot read has to be sure the row was its own.
    in_register = False
    for n, line in doc.scope("register"):
        if line.startswith("| Frame | State |"):
            in_register = True
            continue
        if in_register and not line.startswith("|"):
            in_register = False
        if not in_register or not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if cells[0] in ("Frame", "---") or (len(cells) > 1 and cells[1] == "---"):
            continue
        if len(cells) != 6:
            # The row count is this document's largest single input, and a row
            # that loses a cell used to leave the register silently one shorter.
            # Nothing downstream could notice: the state it declared simply was
            # never declared, so no citation of it was wrong and no exit of it
            # was missing. Six is what §0.8's header promises, so a row of five
            # is not a row this reader may decline to read — it is a row whose
            # author dropped a cell, and the only place that can be said is here.
            broken_rows.append(
                {
                    "line": n,
                    "frame": cells[0],
                    "state": cells[1] if len(cells) > 1 else "",
                    "text": line,
                    "said": f"§0.8 row {cells[0]} · {cells[1] if len(cells) > 1 else ''} "
                            f"has {len(cells)} cells where the register's header declares 6",
                }
            )
            continue
        required = {
            "frame": cells[0],
            "state": cells[1],
            "entry": cells[2],
        }
        empty_required = [name for name, value in required.items() if not value]
        if empty_required:
            broken_rows.append(
                {
                    "line": n,
                    "frame": cells[0],
                    "state": cells[1],
                    "text": line,
                    "said": f"§0.8 row has an empty required "
                    f"{' / '.join(empty_required)} cell",
                }
            )
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
    for broken in broken_rows:
        states.malformed(broken["line"], broken["said"])
        # The row still names a frame and a state — the two leading columns,
        # which are there whichever later cell went missing — and withholding
        # that name is what turned one broken row into four findings: every
        # other row exiting to it reported for exiting nowhere. Only the
        # identity is registered. Which of entry / failure / copy / exit was
        # lost is exactly what a wrong cell count makes unknowable, so the
        # payload says `None` rather than guessing a column alignment, and the
        # arms that iterate the register never see this row at all.
        if not broken["state"]:
            continue
        frame = broken["frame"].lstrip("§")
        states.define(
            f"{frame} · {broken['state']}",
            {"line": broken["line"], "frame": frame, "state": broken["state"], "broken": True},
            content=broken["text"],
            where=broken["line"],
            aliases=tuple(
                spelling
                for name in state_spellings(broken["state"])
                for spelling in (name, f"{frame} · {name}")
            ),
        )
    for r in register:
        frames = FRAME_REF_RE.findall(r["frame"])
        for frame in frames:
            states.define(
                f"{frame} · {r['state']}",
                r,
                content=(r["entry"], r["failure"], r["copy"], r["exit"]),
                where=r["line"],
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
        if not re.fullmatch(r"F\d+", cells[0]):
            continue
        if len(cells) != 3:
            # Not silent before this, but blamed on the wrong lines: dropping F2
            # left eleven register rows reported for naming a treatment 「§0.8's
            # closed set does not define」, and the one row that was actually
            # wrong appeared in none of them. A reader that can see the break
            # says so where the break is.
            treatments.malformed(
                n, f"§0.8 treatment row {cells[0]} has {len(cells)} cells where the table declares 3"
            )
            # Saying so is only half of it. A row this reader cannot read still
            # names its own treatment — `F5` is in the cell the regex just
            # matched — and dropping the name too is what pushed the blame
            # outward: the malformed row reported once and forty-three register
            # rows reported for citing a treatment 「§0.8's closed set does not
            # define」. Registering the name keeps those citations resolvable;
            # the malformed line is the statement that the rest is unknown.
            padded = (cells + ["", ""])[1:3]
            treatments.define(cells[0], padded[0], content=tuple(padded), where=n)
            continue
        treatments.define(cells[0], cells[1], content=(cells[1], cells[2]), where=n)

    frames = Universe("frames", "spec", "C")
    for name, line_no, heading in doc.sections("1."):
        also = FRAME_NAME_RE.search(heading)
        frames.define(
            name,
            line_no,
            content=heading.strip(),
            where=line_no,
            aliases=also.groups() if also else (),
        )

    # --- §0.9 slots ----------------------------------------------------------
    # Three cells: what fills the slot, when it is absent, and which copy keys
    # interpolate it. The third is what makes a slot's meaning checkable at key
    # grain — one sentence covering four consumers reads true for whichever one
    # the author had in mind, and `{{status}}` held three HTTP statuses and one
    # supply health that way for nineteen rounds.
    slots = Universe("slots", "spec", "B")
    for n, line in doc.scope("slots"):
        if line.startswith("| `{{"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) != 4:
                # A slot row is a slot, its meaning, its presence rule and its
                # consumers. Losing the last cell made the row declare 「no key
                # interpolates me」, which is not a shape this reader can tell
                # apart from a slot that genuinely has no consumers — so the
                # consumer comparison fired on the keys instead, and named the
                # two keys that were right.
                slots.malformed(
                    n, f"§0.9 slot row {cells[0]} has {len(cells)} cells where the table declares 4"
                )
                # Same reason as §0.8's treatments: the row still names its
                # slot, and a slot left unnamed is reported again at every key
                # that interpolates it.
                for slot in SLOT_RE.findall(cells[0]):
                    padded = tuple((cells + [None, None, None])[1:4])
                    slots.define(slot, {"line": n, "cells": padded}, content=padded, where=n)
                continue
            for slot in SLOT_RE.findall(cells[0]):
                slots.define(
                    slot,
                    {"line": n, "cells": tuple(cells[1:])},
                    content=tuple(cells[1:]),
                    where=n,
                )

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
    broken_copy: list[tuple[int, str]] = []
    for n, line in doc.scope("copy"):
        if line.startswith("**Copy**"):
            m = re.search(r"`models\.hub\.([a-zA-Z]+)\.\*`", line)
            pending_ns = m.group(1) if m else ""
            continue
        if header := COPY_HEADER_RE.match(line):
            tables += 1
            in_table = True
            ns = header.group(1) or pending_ns
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
        if SEPARATOR_RE.match(line.strip()):
            continue
        m = KEY_DEF_RE.match(line)
        if not m:
            # `KEY_DEF_RE` is a key *and* three cells, so a row that opens with a
            # key and carries two answered exactly like a row that is not a key
            # definition at all — and the copy table is where 「the English is
            # missing」 is supposed to be reported. Class B's whole first rule
            # was unreachable for the one row shape that breaks it.
            #
            # Inside a known copy table, every non-separator Markdown row is a
            # candidate definition. Requiring the key cell's backticks before
            # recognizing the row made deleting those delimiters delete the
            # definition from every check at once.
            if line.startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                opener = KEY_ROW_OPEN_RE.match(line)
                key = opener.group(1).strip() if opener else (cells[0] if cells else "")
                problems: list[str] = []
                if not opener:
                    problems.append("is not backticked")
                if not COPY_KEY_RE.fullmatch(key):
                    problems.append(f"has malformed key `{key}`")
                if len(cells) != 3:
                    problems.append(
                        f"has {len(cells)} cells where a copy table declares 3 "
                        f"(key, 中文, English)"
                    )
                broken_copy.append((n, f"copy row `{key}` {' and '.join(problems)}"))
                # The key is the one cell a broken row still has, and it is
                # collected like any other so citations of it resolve and its
                # plural sibling can still see it — dropping `pill_one`'s
                # English cell otherwise reported `takeover.pill` for 「declares
                # no `_one` row」, on the sibling's line. `None`, not `""`: the
                # texts were not read, which is a different claim from a row
                # whose English cell is empty, and every rule that reads a text
                # declines on `None` rather than judging one nobody read.
                zh = cells[1] if len(cells) == 3 else None
                en = cells[2] if len(cells) == 3 else None
                if key:
                    collected.append(
                        {"key": key, "zh": zh, "en": en, "line": n, "ns": ns,
                         "qualified": f"{ns}.{key}" if ns else key}
                    )
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
    for n, said in broken_copy:
        copy.malformed(n, said)
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
                content=sorted((r["zh"] or "", r["en"] or "") for r in group),
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
    # The heads each section defines keys under, in its own copy table — the
    # vocabulary a frame's prose may cite bare.
    #
    # Only a *dotted* key contributes a head. A leaf key is its own whole name,
    # not a namespace anything hangs under, and treating it as one turns every
    # sentence that dots it into a copy citation: §1.6 defines a key literally
    # named `empty` and its prose says the nested spelling `empty.neverFetched`
    # 「does not exist」, which is the document being explicit, not stale.
    local_heads: dict[str, set[str]] = {}
    for row in collected:
        if "." in row["key"]:
            local_heads.setdefault(doc.section_of(row["line"]), set()).add(row["key"].split(".")[0])

    refs: list[tuple[str, int, str]] = []
    routes: list[tuple[str, int, str]] = []
    inventories: set[str] = set()
    for n, line in doc.scope("frame prose"):
        sec = doc.section_of(n)
        if line.startswith("**Element inventory**"):
            inventories.add(sec)
        # A key's own definition row is not a citation of it. The namespace test
        # below used to exclude these by accident — a row inside a namespaced
        # table writes its key bare, and a bare head is no namespace — and the
        # accident stopped holding the moment prose citations were allowed to
        # reach the universe on the strength of their tail: `| `fail.save` |`,
        # written in two tables that each qualify it differently, read as a
        # prose citation of both at once.
        if KEY_DEF_RE.match(line):
            continue
        for k in KEY_REF_RE.findall(line):
            head = k.split(".")[0]
            # A sentence that explicitly retires a namespace or says it does
            # not exist is a negative assertion about copy, not a consumer.
            if k.endswith("*") and re.search(
                r"(?:tombstone|retired|former|no .{0,24}namespace exists|does not exist)",
                line,
                re.I,
            ):
                continue
            if k.startswith("models.") and not k.startswith("models.hub."):
                continue
            if (head == "models" and k.startswith("models.hub.")) or head in namespaces:
                refs.append((sec, n, k))
                continue
            # A first segment no copy table declares is not on its own a copy
            # citation — this document backticks `api.md`, `source.schema.json`
            # and `status.health` in exactly this shape — so the namespace test
            # is what keeps those three out. Used as the *only* test it also kept
            # out two kinds of sentence that are unmistakably about copy.
            #
            # The first is the citation a frame makes of its own table. A table
            # that declares `models.hub.adopt.*` writes its rows bare, and the
            # frame's prose cites them the same way: `fail.title` names a key
            # that exists, under a first segment that is not a namespace and was
            # never meant to be one. Those citations reached no arm at all.
            #
            # The second is the one worth reporting: `shel.gatewayInfo.body` has
            # a namespace no table declares *because the namespace is misspelt*,
            # and dropping it produced the same silence a correct citation does.
            # What identifies it is that the document declares a namespace under
            # which this exact citation resolves — so the sentence is citing copy
            # and has spelt the head wrong. The correction is not accepted in its
            # place: the citation as written goes to the universe, which answers
            # `empty`, and an undefined key is reported the way any other is.
            if not copy.resolve(k).empty:
                refs.append((sec, n, k))
                continue
            # Server-owned closed presentation keys are defined by the wire
            # authority and consumed verbatim. They intentionally do not have a
            # UI copy-table row; the prose marks that ownership explicitly.
            if re.search(r"server-owned|consumed verbatim|presentation key owned by the server", line):
                continue
            tail = k.split(".", 1)[1] if "." in k else ""
            if any(not copy.resolve(f"{cand}.{tail}").empty for cand in namespaces):
                refs.append((sec, n, k))
                continue
            # Both clauses above admit on the strength of something *resolving*,
            # which is the ε shape: a citation that is wrong in the one way the
            # gate exists to catch — the key does not exist — resolves under no
            # reading and is therefore admitted by neither, so `fail.titl` in a
            # frame that defines `fail.title` reached no arm at all. What says
            # this is a copy citation is not that it resolves; it is that the
            # frame writing it defines keys under that head, in its own table,
            # eighty lines further down. So the head is asked of the section's
            # own vocabulary, and a citation the section is plainly making goes
            # to the universe whether or not it is spelt right.
            #
            # Scoped to the section, not to the document, because that is what
            # keeps the contract field paths out: §1.0 backticks `status.health`
            # in prose, and `status` is a head no copy table in §1.0 declares.
            if head in local_heads.get(sec, frozenset()):
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
        "broken register rows": broken_rows,
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


MAPPING_HEADER_CELL_RE = re.compile(r"^\s*`([A-Za-z][A-Za-z0-9_.\[\]]*)`((?:\s*`\[[a-z-]+\]`)*)\s*$")
MAPPING_HEADER_CANDIDATE_RE = re.compile(r"^\s*`([^`]+)`((?:\s*`\[[^`]+\]`)*)\s*$")
SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|$")


class MappingScan(NamedTuple):
    """One table that renders named fields, kept whole so two arms can read it."""

    line: int
    fields: dict[int, tuple[str, str]]
    rows: list[tuple[int, list[str]]]
    cites: set[str]
    malformed: tuple[tuple[int, str], ...]


def _mapping_scan(doc: Document) -> list[MappingScan]:
    """Every frame-section table whose header declares a field by name.

    Split out when a second arm needed this shape read a different way. Class E
    asks what a column's *domain* is — the set of values, compared against an
    authority. Arm M asks something the domain cannot answer: which copy key
    each value sits beside on its own row. Two parsers over one convention agree
    until the first table that bends it, and a bent table is exactly where a
    finding lives, so the convention is recognised once here and read twice
    above.

    Three things have to be true at once, or the shape is something else: the
    header cell is a bare backticked name, a separator row follows, and the run
    of `|` lines after it is the body. Row *identity* is kept — the line number
    and the cells — because an arm that reports a contradiction has to be able
    to say which row it read.
    """
    tables: list[MappingScan] = []
    numbered = doc.scope("mapping tables")
    for pos, (line_no, line) in enumerate(numbered):
        if (
            not line.startswith("|")
            or pos + 1 >= len(numbered)
            or not SEPARATOR_RE.match(numbered[pos + 1][1].strip())
        ):
            continue
        headers = line.strip().strip("|").split("|")
        fields: dict[int, tuple[str, str]] = {}
        malformed: list[tuple[int, str]] = []
        for col, cell in enumerate(headers):
            m = MAPPING_HEADER_CELL_RE.match(cell)
            if m:
                fields[col] = (m.group(1), m.group(2))
            elif candidate := MAPPING_HEADER_CANDIDATE_RE.match(cell):
                malformed.append(
                    (line_no, f"mapping header `{candidate.group(1)}` is not a valid field citation")
                )
        if not fields and not malformed:
            continue
        rows: list[tuple[int, list[str]]] = []
        for n, row in numbered[pos + 2 :]:
            if not row.startswith("|"):
                break
            cells = row.strip().strip("|").split("|")
            if len(cells) != len(headers):
                malformed.append(
                    (
                        n,
                        f"mapping row has {len(cells)} cells where its header declares "
                        f"{len(headers)}",
                    )
                )
            rows.append((n, cells))
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
        tables.append(
            MappingScan(
                line_no,
                fields,
                rows,
                set(SCHEMA_CITE_RE.findall("\n".join(lead))),
                tuple(malformed),
            )
        )
    return tables


def mapping_tables(
    doc: Document, scans: list[MappingScan] | None = None
) -> list[tuple[int, str, set[str], set[str], bool]]:
    """Tables that render a named field totally, one row per value.

    The document's own convention, used wherever a field's whole vocabulary has
    to reach the screen: a header cell is a backticked field name, and that
    column's cells are its values. That is a set equality with an authority on
    one side, written by an author who cannot see the authority while writing it.

    *Every* such column, not just the first. The convention was read off tables
    that render one field, and it stayed true only until a table rendered two:
    §1.0's subtitle mapping is keyed on `mode` *and* `supply_status`, and a
    reader of first columns alone checks the two-value field totally while the
    five-value one — the vocabulary the table exists to exhaust — goes
    unchecked, silently, with the class still reporting a table covered. A
    convention that holds for the shapes it was written against is not a rule;
    what makes it one is that the arm reads whatever the header declares.

    Three things have to be true at once, or the shape is something else: the
    header cell is a bare backticked name, a separator row follows, and at least
    one data row has a backticked token in that column. The last condition is
    what keeps the fixture table at §1.3 out — it is headed `` `models` `` but
    its cells are counts, so it renders arithmetic, not a vocabulary. It also
    excuses a column that carries a name and no vocabulary: the mapping table's
    own `mode`=`direct` row reads 「not read — Direct arbitrates nothing」 in the
    `supply_status` column, which is prose, and a column is judged on the values
    it does have.

    The domain is read as a set rather than counted, because a value may
    legitimately occupy two rows (one value, two renderings) and a count would
    call that a drift.
    """
    found: list[tuple[int, str, set[str], set[str], bool]] = []
    for table in scans if scans is not None else _mapping_scan(doc):
        values: dict[int, set[str]] = {col: set() for col in table.fields}
        for _n, cells in table.rows:
            for col in table.fields:
                if col < len(cells):
                    # The mapped field owns this column, so only its leading
                    # value token is a domain member. Qualifying conditions in
                    # the same cell (`error_key`, `host_platform`, `adopted_by`)
                    # select a rendering; they do not extend the field's enum.
                    leading = re.match(r"\s*`([a-z][a-z0-9_]*)`", cells[col])
                    if leading:
                        values[col].add(leading.group(1))
        for col, (field, markers) in sorted(table.fields.items()):
            if values[col]:
                found.append((table.line, field, values[col], table.cites, "[contract]" in markers))
    return found


# A bare enum value: lowercase, undotted, backticked. Deliberately *not*
# `DOTTED_TOKEN_RE`, which also admits `manifest.assets` — a field name that
# qualifies a row is not one of the values that row renders, and reading it as
# one invents a domain member no schema declares.
VALUE_TOKEN_RE = re.compile(r"`([a-z][a-z0-9_]*)`")

# This document's dispatch idiom: one or more values, then an arrow, then the
# state they land in. The values have to sit immediately before the arrow —
# 「an `active` nothing adopts →」 is a sentence about a value, not a pairing —
# and the landing runs to the next quoted value, because that is where the next
# pairing starts.
ARROW_LANDING_RE = re.compile(r"((?:`[a-z][a-z0-9_]*`(?:\s*(?:,|、|/|or|或)\s*)?)+)\s*→\s*([^`]*)")
LANDING_END_RE = re.compile(r"[。;;]|\.\s")
# A landing written as a name rather than as the sentence carrying on. This
# document capitalises its state names and nothing else in the middle of a
# clause, so the opening character is what separates a citation from prose.
LANDING_NAME_RE = re.compile(r"\s*\*?[A-Z]")


def frame_mappings(doc: Document, copy_u: Universe) -> dict[str, dict[str, dict[str, set[str]]]]:
    """frame → field → value → the copy keys that frame's own table renders it as.

    The resolver for "what does this value look like on screen" is per frame and
    cannot be anything else. `cooldown` is a value of three different fields
    across the frozen schemas and `degraded` of two, so a globally keyed
    value→key map answers with whichever field it happened to see last. The
    frame's own mapping table is the one place the pairing is stated by an
    author who could see both halves at once.

    A row contributes only the copy keys that resolve to exactly one definition.
    A wildcard citation names a family rather than a rendering, and an
    unresolvable one is already class B's finding; neither is evidence about
    what this value is drawn as.

    The *value* is recorded either way, with an empty key set when the row
    renders no single key. Only the pairing is unknown there; the vocabulary is
    not, and a caller asking this table for a field's domain must not be handed
    a set short by however many rows happened to cite a family. §1.6 drew all
    five `Source.state.status` values and this returned four, because
    `needs_action` renders 「the `sourceDetail.status.needsAction.*` row
    `state.detail_key` selects」 — so the one status a dispatch is most likely to
    forget was also the one no arm could notice was missing. Callers that want
    pairings filter on a non-empty set, which the value→key arm already did.
    """
    per: dict[str, dict[str, dict[str, set[str]]]] = {}
    for table in _mapping_scan(doc):
        by_field = per.setdefault(doc.section_of(table.line), {})
        for _n, cells in table.rows:
            keys: set[str] = set()
            for col, cell in enumerate(cells):
                if col in table.fields:
                    continue
                for cite in KEY_REF_RE.findall(cell):
                    hit = copy_u.resolve(cite)
                    if not hit.empty and not hit.ambiguous and not hit.wildcard:
                        keys.add(hit.hits[0])
            for col, (field, _markers) in table.fields.items():
                if col >= len(cells):
                    continue
                for value in VALUE_TOKEN_RE.findall(cells[col]):
                    by_field.setdefault(field, {}).setdefault(value, set()).update(keys)
    return per


DISPERSE_RE = re.compile(r"^→\s*(.+)$")
FRAME_PREFIX_RE = re.compile(r"^§?(\d+\.\d+)\s+(.*)$")


def dispersal(cell: str, own: str) -> list[tuple[str, str]] | None:
    """A failure cell that hands the failure to a *set* of states, as (frame, state).

    This is the one failure-cell shape class A abstained on: `→ A / B / C`, with
    or without a `§n.m` prefix on each destination. A destination with no prefix
    belongs to the row's own frame. Everything after an em-dash separator is the
    row's commentary on the dispersal, not part of it.
    """
    m = DISPERSE_RE.match(cell.strip())
    if not m:
        return None
    out: list[tuple[str, str]] = []
    for token in re.split(r"\s+—\s+", m.group(1))[0].split("/"):
        token = token.strip().rstrip(".")
        if not token:
            continue
        prefixed = FRAME_PREFIX_RE.match(token)
        out.append((prefixed.group(1), prefixed.group(2)) if prefixed else (own, token))
    return out


# What may follow a gap number: whitespace, a punctuation mark that closes or
# separates the citation, a sentence-ending `.`, or end of input. Written as an
# allow-list rather than as "not a suffix character", because the two spellings
# fail in opposite directions and only one of them fails loudly. See
# `registered_gaps`.
GAP_END = r"(?=$|[\s,;:*'\"`)\]}，、。；:）」』】]|\.(?!\w))"
# The number is read as far as the token runs, not as far as it parses. `G-\d+`
# followed by an end test made `G-10x` match nothing at all, so a marker with a
# typo in it excused nothing and was reported as nothing: the sentence carrying
# it kept whatever `[contract-gap]` buys and no arm ever asked which row it
# named. Reading the whole token hands `G-10x` to the registry, which answers
# `empty`, which is the finding.
GAP_REF_RE = re.compile(
    r"\[contract-gap\]`?\s*`?(G(?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)" + GAP_END
)
GAP_TRAILING_REF_RE = re.compile(
    r"\b(G(?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)(?:['’]s)?"
    r"[^.\n]{0,80}\[contract-gap\]"
)
GAP_ROW_RE = re.compile(r"^\|\s*(G-\d+)\s*\|")
# Strikethrough is how this document retracts text it keeps for the record, and
# a register that keeps withdrawn rows has to say which ones they are in a way a
# reader *and* a checker can both see. See `GapRow`.
STRUCK_RE = re.compile(r"~~.+?~~", re.S)

# The column each register declares its object in. §0.5 says what is missing;
# §0.4 says which contracted route lives elsewhere. Both tables argue in their
# remaining columns, and argument names whatever it needs to name.
MISSING_COLUMN_RE = re.compile(r"^missing$", re.I)
SCOPED_ROUTE_COLUMN_RE = re.compile(r"contracted route", re.I)


def declared_column(numbered: list[tuple[int, str]], header: re.Pattern[str]) -> dict[int, str]:
    """Each body row of the table whose header matches, as {line -> that cell}.

    A register accounts for what it *declares*, never for what it happens to
    mention. Reading whole rows made that distinction disappear, and the cost
    was the one defect twenty-nine review rounds never surfaced: §0.5's G-13
    registers a bulk re-apply action that does not exist, and cites
    `PUT /api/models/agents/<backend>/chain` only to say that nothing bulk
    rewrites stored chains — a sentence about the route's *presence*. Harvesting
    it excused the chain `PUT` everywhere, so the one contracted mutation this
    document accounted for nowhere looked accounted for, until #1232's review
    found it by hand. `GapRow` already said the rule out loud — a marker excuses
    「the behaviour its row registers, not whatever else the paragraph around it
    happens to mention」 — while the code read the paragraph. This is the code
    agreeing with it.

    The column is found by its header rather than its position, and per table
    rather than once per section, so renaming a column does not silently
    re-point the arm at prose and a second table in the same section is read
    against its own header instead of the first one's. A header that matches
    nothing yields no rows and excuses nothing: a register that cannot be read
    has to fail loudly rather than pass everything, and every route it was
    covering is reported on the next run.
    """
    col: int | None = None
    cells_by_line: dict[int, str] = {}
    for pos, (line_no, line) in enumerate(numbered):
        if not line.startswith("|"):
            col = None  # a table is its own rows: where they stop, so does its header
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if pos + 1 < len(numbered) and SEPARATOR_RE.match(numbered[pos + 1][1].strip()):
            col = next((i for i, cell in enumerate(cells) if header.search(cell)), None)
            continue
        if SEPARATOR_RE.match(line.strip()):
            continue
        if col is not None and col < len(cells):
            cells_by_line[line_no] = cells[col]
    return cells_by_line


class GapRow(NamedTuple):
    """One §0.5 row: where it is written, and whether it still registers anything.

    A withdrawn row is kept — a register of missing behaviour that silently
    loses entries cannot be audited — and the row that carries the withdrawal
    says so twice: its surface is struck through and so is what it once called
    missing. Reading that markup is what turns 「no surface may carry
    `[contract-gap]` G-9」 from a rule written in prose into a rule the gate
    applies, which is the difference between a retired gap and a working
    silencer that nobody meant to leave armed.

    `routes` is what the row names, and it is what the row can excuse: a marker
    is an excuse for the behaviour its row registers, not for whatever else the
    paragraph around it happens to mention. §0.5 already reasons this way about
    the withdrawn row — 「the row names no route and quotes no body, so there is
    nothing left in it for a checker to excuse」 — so reading the row's routes
    makes the gate agree with the register's own account of itself. Struck text
    is dropped first: a retracted route is not a named one.

    The predicate is total and it is spelled to fail loudly. A row registers
    unless its *surface* or *missing* cell strikes something out; anything else
    — a half-struck row, a strike written for some reason nobody foresaw —
    registers nothing. That direction is deliberate: a row that stops
    registering makes the surfaces citing it get checked as if the marker were
    not there, which is a finding on the next run, while the other direction
    leaves a silencer armed and says nothing. The evidence cell is not read,
    because it quotes the contract and may legitimately strike a stale quote
    without retracting the row.

    `fields` is the same reading of the same cell, for the other thing a row can
    excuse. A gap row argues from the contract, so it quotes it — G-3 says the
    delete route removes a manual entry only and that `source.schema.json`'s
    `models` carries no retained flag — and while the *whole row* was exempt,
    that quotation was the one kind of authority claim in this document nobody
    checked: rename the field and the row still read as evidence. What a row is
    entitled to excuse is what it declares missing, which is the cell that says
    so; everything else in the row is argument, and argument is checkable.
    """

    line: int
    registers: bool
    routes: frozenset[str]
    fields: frozenset[str]


def registered_gaps(doc: Document) -> Universe:
    """The §0.5 registry, as a universe: gap number -> the row registering it.

    `[contract-gap]` is the document's one way to say "this surface has no
    backend behind it", and it is also the one way to tell a checker not to ask
    for something that cannot exist. Both readings are needed, which makes the
    marker a silencer, and a silencer that costs a keystroke silences things
    nobody meant to. Requiring the marker to name a number, and the number to
    resolve to a §0.5 row, prices it at what it should cost: a stated surface, a
    stated missing behaviour, and evidence verified against a named commit. A
    bare `[contract-gap]`, or one citing a number no row defines, exempts
    nothing — the claim is checked as if the marker were not there.

    A reference ends where its digits end, and it has to end at something the
    document uses to end a citation. Without that the number was read as a
    prefix, so `G-9x` borrowed the row written about G-9 and a `G-15`-shaped typo
    silenced a route on the strength of a registration that was about something
    else — the same accident as the bare marker, reached by one *extra* keystroke
    instead of one fewer, and harder to see because the citation looks like it
    names something. A suffixed citation now resolves to nothing and silences
    nothing, exactly like a bare one.

    `GAP_END` states which characters may end one, and the choice of an
    allow-list over "anything that is not a suffix character" is the whole
    lesson of the second attempt. The first rejected `G-15x` and still accepted
    `G-15.1`, because a rule written against the direction that was reported
    covers that direction and no other. The two spellings are not symmetric:
    naming what may *not* follow leaves every unnamed joiner silencing a route
    quietly, while naming what *may* follow turns an unforeseen one into a
    citation that resolves to nothing — which this checker reports on the next
    run. For a silencer, the failure that shows up in the output is the one to
    prefer, so the unforeseen case is spelled to be loud. `.` is the exception
    the census forces: it ends seven real citations as a full stop and begins
    the malformed `G-15.1`, so it is admitted only when no word character
    follows it.

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
    registry = doc.scope("gap registry")
    declared = declared_column(registry, MISSING_COLUMN_RE)
    for n, line in registry:
        if m := GAP_ROW_RE.match(line):
            cells = line.split("|")
            if len(cells) != 6:
                # This one was fully silent. A §0.5 row that loses a cell still
                # matched the row pattern and still registered its number, so
                # every citation of it resolved — while `cells[2:4]` quietly
                # took a slice of a shorter row and the 「what is missing」
                # column, which is the whole content of a gap registration and
                # what every exemption is measured against, went missing itself.
                # A gap that excuses on the strength of an unread cell is worse
                # than no gap at all.
                #
                # The one reader that does *not* also register its row's name.
                # §0.8's register, §0.8's treatments, §0.9's slots and the copy
                # tables all keep the identity of a row they cannot read, so the
                # loss reports once instead of at every line that cites it. A
                # §0.5 row is not that kind of name: it is a silencer, and
                # keeping its number alive would let the marker excuse claims on
                # the strength of the cell that went missing. Here the citations
                # reporting is the *point* — every one of them is a claim now
                # checked as though the marker were not there, which is the
                # direction to fail in when what was lost is the excuse itself.
                gaps.malformed(
                    n,
                    f"§0.5 row {m.group(1)} has {len(cells) - 2} cells where the registry "
                    f"declares 4",
                )
                continue
            standing = STRUCK_RE.sub(" ", declared.get(n, "")).strip()
            whole_row = STRUCK_RE.sub(" ", line)
            gaps.define(
                m.group(1),
                GapRow(
                    n,
                    bool(standing and standing.lower() != "nothing"),
                    frozenset(
                        normalize_route(meth, path)
                        for meth, path in ANY_ROUTE_RE.findall(whole_row)
                    ),
                    frozenset(
                        token.split(".")[-1] for token in DOTTED_TOKEN_RE.findall(standing)
                    ),
                ),
                content=line.strip(),
                where=n,
            )
    return gaps


def gap_references(text: str) -> list[re.Match[str]]:
    """Gap citations in either the marker-first or explanatory trailing form."""
    return sorted(
        [*GAP_REF_RE.finditer(text), *GAP_TRAILING_REF_RE.finditer(text)],
        key=lambda match: match.start(),
    )


def active_gap_citations(gaps: Universe, text: str) -> list[re.Match[str]]:
    """Every `[contract-gap]` reference in `text` that a live §0.5 row answers.

    The one gap comparison. Both arms that honour the marker — class A's route
    coverage and class E's claim check — ask through here, so neither can drift
    into a spelling of its own, and both get the *positions* of the citations
    because an excuse belongs to what it is written next to.
    """
    live: list[re.Match[str]] = []
    for m in gap_references(text):
        hit = gaps.resolve(m.group(1))
        if not hit.empty and hit.one.registers:
            live.append(m)
    return live


def cites_a_registered_gap(gaps: Universe, text: str) -> bool:
    """Does `text` point at a §0.5 row that exists and still registers?"""
    return bool(active_gap_citations(gaps, text))


def gap_excused_routes(gaps: Universe, text: str) -> set[str]:
    """Which routes `text`'s gap markers excuse: the ones their §0.5 rows name.

    A marker is an excuse for the behaviour its row registers, not an amnesty for
    the paragraph around it. §1.5 marks the metadata `PATCH` with G-15 because
    the contract has that call and no frame draws it; read paragraph-wide, the
    same marker vouched for any other route named anywhere in those lines, so a
    live delete affordance dropped beside it owed §0.8 no state at all.

    Position is not the discriminator, and trying it showed why: this document
    writes the marker on the bolded sentence that opens a claim, then names the
    route it is about several lines down — §1.4's G-17 sits two characters from
    `oauth/start`, which is drawn, and is about `oauth/submit`, which is not. So
    the row answers instead. The register is where a gap says what is missing,
    and it names the calls involved; a row that names none — the withdrawn one —
    excuses nothing, which is what §0.5 says about it in prose.
    """
    excused: set[str] = set()
    for cite in active_gap_citations(gaps, text):
        excused |= gaps.resolve(cite.group(1)).one.routes
    return excused


def gap_excused_fields(gaps: Universe, text: str) -> set[str]:
    """Which field names `text`'s gap markers excuse: the ones their rows name.

    The mirror of `gap_excused_routes`, and it exists for the same reason a
    register accounts for what it declares. A marker beside a sentence that
    names a field the contract genuinely does not carry is the marker doing its
    job; the same marker does not make every other field the paragraph mentions
    unfalsifiable, and the paragraph around a gap row is where this document
    does most of its quoting of the contract.
    """
    excused: set[str] = set()
    for cite in active_gap_citations(gaps, text):
        excused |= gaps.resolve(cite.group(1)).one.fields
    return excused


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
OPTIONAL_PLURAL_SLOT_OMISSIONS = {("shell.allDirect", "one", "en", "count")}

SELF_TEST = [
    # (fixture, must the extractor count it, must it report a finding)
    ("`agent-supply.schema.json`'s `supply_status` enumerates six values.", True, True),
    ("`agent-supply.schema.json`'s `supply_status` enumerates five values.", True, False),
    # The same claim in the two spellings the extractor used not to know. Both
    # belong here rather than only in the mutation suite: this is the arm whose
    # zero is its verdict, and a vocabulary gap is precisely how that zero goes
    # from *the document restates nothing* to *the reader restated it in words I
    # do not read*.
    ("`agent-supply.schema.json`'s `supply_status` enumerates 6 values.", True, True),
    ("`agent-supply.schema.json`'s `supply_status` enumerates sixteen values.", True, True),
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
             "vocabulary claims": 0, "attributed fields": 0,
             "contract mapping tables": 0, "repo symbols": 0, "authority lines": 0}

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

    registrations = {row.line: row for _token, row in gaps.items()}

    guarded_named: dict[str, int] = {}
    for line_no, scope in doc.claims():
        # A §0.5 row is the registration itself, so it needs no reference to
        # one: its whole job is to describe a behaviour the contract does not
        # have, and describing that behaviour means naming the branch that is
        # missing. Every other scope has to point at a row that exists. "Is
        # this scope a registration?" is answered by the registry's own line
        # numbers, not by re-testing the row shape here — that shape matches
        # anywhere, including the places §0.5 is quoted.
        # A marker that resolves to nothing is reported for being unresolved,
        # here, before anyone asks what it excuses. `active_gap_citations` only
        # ever *omitted* it from the live exemptions, so a bad number surfaced
        # exactly when the sentence around it needed an excuse for something
        # else: `[contract-gap] G-99` on a row that owes no other exemption cost
        # nothing at all, and the register went on citing a row that does not
        # exist. An excuse nobody can look up is a defect whether or not it was
        # load-bearing, and it is class E's — a §0.5 row is a claim about the
        # contract, so a citation of one that is not there is a claim with no
        # authority behind it.
        for m_gap in gap_references(scope):
            hit = gaps.resolve(m_gap.group(1))
            if hit.empty:
                add(f"L{line_no}", f"`[contract-gap] {m_gap.group(1)}` names no §0.5 row")
        exempt = line_no in registrations or cites_a_registered_gap(gaps, scope)
        # Where each route is written, not just which routes appear: a body
        # literal is bound to one route, and binding needs positions.
        route_claims = [
            (
                m.start(),
                m.end(),
                normalize_route(m.group(1), m.group(2)),
                route_query(m.group(2)),
            )
            for m in ROUTE_CANDIDATE_RE.finditer(scope)
        ]
        mentions = [(start, end, route) for start, end, route, _query in route_claims]
        named = {route for _s, _e, route in mentions}
        checked_claims: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for _start, _end, route, query in route_claims:
            if (route, query) in checked_claims:
                continue
            checked_claims.add((route, query))
            scale["routes"] += 1
            hit = auth["routes"].resolve(route)
            if hit.empty:
                add(f"L{line_no}", f"`{route}` is contracted by no `api.md` route row")
                continue
            if query and query != hit.one["query"]:
                claimed = "&".join(f"{name}={value}" for name, value in query)
                contracted = "&".join(f"{name}={value}" for name, value in hit.one["query"])
                add(
                    f"L{line_no}",
                    f"`{route}` uses query `{claimed}`; `api.md` contracts "
                    f"`{contracted or '(none)'}`",
                )
            if hit.one["guarded"]:
                guarded_named.setdefault(route, line_no)

        for m_body in BODY_RE.finditer(scope):
            literal = m_body.group(1)
            scale["bodies"] += 1
            claimed = literal_members(literal)
            keys = set(claimed)
            if not named:
                # A gap row describes a body for a route that does not exist, so
                # there is nothing to bind it to and nothing to compare. Anywhere
                # else, an unbound body is a claim no reader can verify.
                # Register rows and named decision matrices are allowed to cite
                # a producer's already-defined shape without repeating its
                # literal route. Ordinary prose still owes an explicit binding.
                symbolic_producer = bool(
                    line_no in {row["line"] for row in register}
                    or re.search(r"\b(?:E\d+[a-z]?|R\d+|RR-\d+|M\d+|D-\d+)\b", scope)
                    or "schema" in scope.lower()
                    or (
                        "registered" in scope.lower()
                        and ("phase" in scope.lower() or "producer" in scope.lower())
                    )
                    or (
                        "producer" in scope.lower() and "guarded" in scope.lower()
                    )
                )
                if not exempt and not symbolic_producer:
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
            sentence_start = max(scope.rfind(".", 0, m_body.start()), scope.rfind(";", 0, m_body.start()))
            sentence_end_candidates = [
                at for at in (scope.find(".", m_body.end()), scope.find(";", m_body.end())) if at >= 0
            ]
            sentence_end = min(sentence_end_candidates, default=len(scope))
            local_mentions = [
                mention
                for mention in mentions
                if sentence_start < mention[0] < sentence_end
            ]
            if not local_mentions and line_no in {row["line"] for row in register}:
                continue
            bound = nearest_subject(
                local_mentions or mentions, m_body.start(), m_body.end()
            )
            # Which side of the cell a claim belongs to is not a guess. A `GET`
            # has no request body, so a body written against one is quoting the
            # answer; so is a body the document itself introduces as one, with
            # `→` or with the word. Everything else is what the client sends,
            # and letting a request borrow the response vocabulary is how
            # `{agent: [...]}` posted to the order route would pass a check the
            # server rejects.
            answer = bool(ANSWER_CUE_RE.search(scope[: m_body.start()])) or bound.startswith("GET ")
            allowed: set[str] = set()
            demanded: dict[str, LiteralMember] = {}
            hit = auth["routes"].resolve(bound)
            if not hit.empty:
                row = hit.one
                allowed |= row["response_keys"] if answer else row["keys"]
                demanded = row["response_required"] if answer else row["required"]
                # A shape with more than one reading is not a vocabulary to pick
                # from. `added_to` is contracted for the create terminal and
                # contracted *away* from the reauth one — that partition is the
                # whole content of the section, and a union of it accepts the
                # one sentence the section exists to forbid. So a claim that
                # names its reading is held to that reading; one that names
                # none, or names two, still has the section entire.
                clause = SENTENCE_RE.split(scope[: m_body.start()])[-1]
                clause += SENTENCE_RE.split(scope[m_body.end() :])[0]
                reading = [
                    name
                    for name in row["response_readings"]
                    if f"`{name}`" in clause or f'"{name}"' in clause
                ]
                if answer and len(reading) == 1:
                    allowed = set(row["response_readings"][reading[0]])
                    demanded = row["response_readings"][reading[0]]
                # The shared envelopes are *answers*: a refusal body, a
                # confirmation body. A guarded route's request side needs
                # nothing from them — `api.md` spells the one field a client
                # adds, `force?`, inside the request half of the cell itself —
                # so unioning them into a request allowance widened it by every
                # key any response anywhere carries, and `{source}` posted to
                # the metadata `PATCH` passed on the strength of a field that
                # route only ever returns.
                if row["guarded"] and answer:
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
            # The other direction, which this arm did not have: `keys - allowed`
            # asks only whether the body names something the route lacks, so any
            # subset passed, the empty-of-required subset included. A spec that
            # posts `{flow_id}` where the contract demands `{flow_id, value}` is
            # a request the server rejects, written down as though it were the
            # request.
            #
            # Required-ness comes from the contract's own `?`, which every
            # reader had been stripping, so "which members may be left out" was
            # not a fact the checker held at all. Measured before the rule was
            # narrowed: fifteen bodies in this document bind to a route with a
            # required member, and all fifteen spell every one of them. There
            # was no partial-body case to carve an exception for, and carving
            # one anyway — 「only bodies naming two or more members」 was the
            # first draft — would have excused the reviewer's own example,
            # `{flow_id}` for `{flow_id, value}`, which names one.
            #
            # A registered gap is not excused here, and that matches what the
            # stray branch actually forgives: a field *nothing* contracts. A
            # field this route demands is contracted, so a gap row omitting it
            # is not describing missing behaviour — it is misdescribing the
            # behaviour that exists.
            missing = sorted(k for k, member in demanded.items() if member.required and k not in keys)
            if missing:
                add(
                    f"L{line_no}",
                    f"`{literal}` omits {', '.join(missing)} — required for {bound}",
                )
            optional = sorted(
                key
                for key in keys & demanded.keys()
                if demanded[key].required and not claimed[key].required
            )
            if optional:
                add(
                    f"L{line_no}",
                    f"`{literal}` marks {', '.join(optional)} optional — required for {bound}",
                )
            for key in sorted(keys & demanded.keys()):
                stated = claimed[key].types
                contracted = demanded[key].types
                if stated and contracted and stated.isdisjoint(contracted):
                    add(
                        f"L{line_no}",
                        f"`{literal}` declares `{key}` as {' / '.join(sorted(stated))} — "
                        f"{bound} contracts {' / '.join(sorted(contracted))}",
                    )

        # A 409 claim is about one route, so the excuse for it has to be about
        # that route too — the same per-claim binding class A now uses, and for
        # the same reason: a marker registered for the route two cells away is
        # not a statement about this one. A §0.5 row is still exempt whole,
        # because the row *is* the registration.
        #
        # And the claim binds the same way the excuse does. Holding every `409`
        # in a scope against every route in it is the container reading twice
        # over: it reports a guarded route's neighbour for a branch written
        # about the guarded route, and — the direction that matters — one
        # guarded route in a paragraph made every other route in it unreportable
        # only by accident, because a single unguarded one anywhere would have
        # fired. Same binder as the body literals, one line above.
        if named:
            excused = named if line_no in registrations else gap_excused_routes(gaps, scope)
            for m_code in STATUS_RE.finditer(scope):
                scale["status branches"] += 1
                local_mentions = [
                    mention
                    for mention in mentions
                    if not re.search(
                        r"\b(?:or|and)\s*$",
                        scope[min(mention[1], m_code.end()) : max(mention[0], m_code.start())],
                        re.I,
                    )
                ]
                route = nearest_subject(
                    local_mentions or mentions, m_code.start(), m_code.end()
                )
                if route in excused:
                    continue
                hit = auth["routes"].resolve(route)
                if hit.empty:
                    continue
                status = m_code.group(1)
                if status not in hit.one["statuses"]:
                    allowed = ", ".join(sorted(hit.one["statuses"])) or "no error status"
                    add(
                        f"L{line_no}",
                        f"a {status} branch is claimed for {route}, which `api.md` contracts as {allowed}",
                    )

        cited: list[tuple[int, int, str]] = []
        for m_file in SCHEMA_CITE_RE.finditer(scope):
            scale["schema citations"] += 1
            if auth["schema files"].resolve(m_file.group(1)).empty:
                add(f"L{line_no}", f"`{m_file.group(1)}` is not a file in {CONTRACTS}")
                continue
            cited.append((m_file.start(), m_file.end(), m_file.group(1)))

        # A count is about one vocabulary: the one it is written next to. Held
        # against every schema the scope happened to cite, 「`agent-supply
        # .schema.json`'s 13 properties do not include `adopted_by`」 was also
        # read as a claim about the `source.schema.json` named later in the same
        # gap row, and reported against it — a true sentence turned into a
        # finding, and the reader sent to the wrong file to fix it. Same binder
        # as the body literals and the 409 branches.
        for m_count in COUNT_CLAIM_RE.finditer(scope):
            want = count_value(m_count.group(1))
            if want is None or not cited:
                continue
            scale["vocabulary claims"] += 1
            schema = nearest_subject(cited, m_count.start(), m_count.end())
            tokens = {t.split(".")[-1] for t in DOTTED_TOKEN_RE.findall(scope)}
            if m_count.group(2) == "properties":
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
            noun = m_count.group(2)
            for name, values in sorted(sets.items()):
                if len(values) != want:
                    add(f"L{line_no}", f"`{name}` has {len(values)} {noun}, not {want}")
                    continue
                # The enumeration half is asked of enum values and not of
                # property names, and the asymmetry is real rather than
                # convenient. An enum value is only ever written here as part of
                # a run, so a property-name-shaped token that is one of them is
                # a rendering of the set and is compared to it. A property name
                # is written constantly as a plain citation — 「four of them
                # server-assigned (`id`, `created_at`, `state`, `usage`)」 names
                # four of sixteen on purpose — so intersecting the scope's
                # tokens with the property set read every such sentence as a
                # botched total rendering. Whether a *cited* property is really
                # declared is the attributed-fields arm's question, and it asks
                # it of every citation rather than only of counted ones.
                listed = tokens & values
                if noun != "properties" and listed and listed != values:
                    add(
                        f"L{line_no}",
                        f"`{name}` is enumerated here as {sorted(listed)}, "
                        f"against {sorted(values)}",
                    )

        # A count claim is a set equality and gets checked as one above. Most
        # field citations are not counts — they name a field and say which file
        # declares it — and those went unchecked: the file was confirmed to
        # exist and the name attributed to it never was, so a misspelling sat
        # behind a citation that looked verified.
        #
        # Some of these sentences attribute a field in order to say it is *not*
        # there — 「`adopted_by` is absent from `source.schema.json`」 — and the
        # exemption is what tells the two apart. Not a list of negations: this
        # document already has to mark a claim that a field is missing with a
        # registered gap (§0.3, §0.5), so the marker is a declaration and an
        # English negation-detector would be a guess. A scope that registers the
        # absence is doing its job; one that does not is claiming the field
        # exists.
        #
        # What the marker buys is the field it declares, not the paragraph it
        # sits in. Exempting whole scopes made the §0.5 registry — the densest
        # quoting of the contract in this document, and the place a stale quote
        # is most load-bearing, because the row's whole argument is that quote —
        # the one region of the file where a misattributed field could not be
        # reported. A row is still entitled to its own claim: what it names in
        # its *missing* cell is a behaviour it has already declared absent.
        own = registrations.get(line_no)
        excused_fields = set(own.fields) if own else gap_excused_fields(gaps, scope)
        for cited, field in sorted(attributed_fields(scope)):
            if auth["schema files"].resolve(cited).empty:
                continue  # the citation arm above already reported the file
            scale["attributed fields"] += 1
            if field.split(".")[-1] in excused_fields:
                continue
            # A path is compared as a path. Reduced to its last segment,
            # `status.health` asked only whether the file declares `health`
            # *somewhere* — so `manifest.health`, whose parent that file does not
            # have, was answered by the leaf under the parent the sentence was
            # not about, and a citation naming the wrong object read as verified.
            # A one-word citation still asks the vocabulary question it always
            # asked: it names no parent, so there is no parent to be wrong about,
            # and which of two declarations it means is the mapping-table arm's
            # question, which has the resolver for it.
            known = auth["schema paths"][cited] if "." in field else auth["schemas"][cited]
            if field not in known:
                add(f"L{line_no}", f"`{cited}` declares no `{field}`")

        for rel, symbol in PY_CITE_RE.findall(scope):
            scale["repo symbols"] += 1
            source = origin.read(rel)
            if source is None:
                add(f"L{line_no}", f"`{rel}` is not a file in {origin.label}")
                continue
            if rel not in read_files:
                read_files.add(rel)
                try:
                    defined = defined_symbols(source)
                except SyntaxError as broken:
                    # Same door again, on the last reader that had it open: a
                    # cited file this Python cannot parse ended the run in a
                    # traceback. Every symbol it defines then resolves to
                    # nothing, so the alternative to reporting the file is
                    # reporting each of its citations for naming a symbol that
                    # is in fact right there.
                    symbols.malformed(
                        f"L{line_no}",
                        f"`{rel}` is cited as Python and does not parse "
                        f"({broken.msg} at line {broken.lineno})",
                    )
                    continue
                for qualified, bare, line in defined:
                    symbols.define(
                        f"{rel}:{qualified}",
                        rel,
                        content=line,
                        where=f"{rel}:{line}" if line else rel,
                        aliases=(f"{rel}:{bare}",),
                    )
            if not symbol:
                continue
            hit = symbols.resolve(f"{rel}:{symbol}")
            if hit.empty:
                add(f"L{line_no}", f"`{rel}` defines no `{symbol}`")
            elif hit.ambiguous:
                # A citation is a promise that the reader can go and look. Four
                # `load`s in one file is not a defect in that file — it is a
                # defect in a sentence that says `service.py:load` and expects
                # one of them to be understood.
                named = ", ".join(f"`{t.split(':', 1)[1]}`" for t in hit.hits)
                add(f"L{line_no}", f"`{rel}` defines `{symbol}` in {len(hit.hits)} places ({named})")

        for cited_line in PRECISE_LINE_CANDIDATE_RE.finditer(scope):
            reference, number_text = cited_line.groups()
            scale["authority lines"] += 1
            add(
                f"L{line_no}",
                f"`{reference}:{number_text}` is an unstable line citation; cite a stable contract anchor or file symbol",
            )

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
    scans = _mapping_scan(doc)
    for table in scans:
        for where, message in table.malformed:
            add(f"L{where}", message)
    for line_no, field, drawn, cited, contracted in mapping_tables(doc, scans):
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
    reg_frames = {frame for r in reg for frame in FRAME_REF_RE.findall(r["frame"])}
    reg_by_section: dict[str, list[dict[str, Any]]] = {}
    for r in reg:
        for frame in FRAME_REF_RE.findall(r["frame"]):
            reg_by_section.setdefault(frame, []).append(r)

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
    # defines, and that row has to name *this* route. A marker anywhere in the
    # paragraph silenced every route in it for free — see `gap_excused_routes`.
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
        if call in gap_excused_routes(gaps, scope_at.get(line, "")):
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
        cited_treatments = sorted(set(TREAT_CANDIDATE_RE.findall(cell)))
        for t in cited_treatments:
            if treatment_u.resolve(t).empty:
                add("A", f"L{r['line']}", f"「{r['state']}」 names {t}, which §0.8's closed set does not define")
        # A named treatment answered the whole cell, and most cells name one and
        # nothing else. The live ones that do not are the point: 「F1 → Install
        # failed」 says how the failure is treated *and* where it lands, and
        # returning on the treatment left the landing unread — 「F1 → Vanished
        # forever」 was a complete cell as far as this arm could tell. A cell is
        # answered by what it says, so both halves are read; what a treatment
        # buys is only the right to say nothing about a landing.
        row_frames = FRAME_REF_RE.findall(r["frame"])
        if len(row_frames) != 1:
            # A shared row deliberately owns the same state in several frames;
            # its local transitions cannot be resolved against one invented
            # owner. Presence under every named frame is checked below.
            continue
        frame = row_frames[0]
        # The latest register often delegates classification to one of its
        # closed matrices (E*, M*, RR-*, PD-*, C*). Those identifiers are the
        # treatment: the cell is not also required to repeat an F number or one
        # local landing after the matrix already names the complete branch.
        classifier_owned = bool(
            re.search(r"\b(?:E\d+[a-z]?|M\d+|RR-\d+|PD-\d+|C\d+|O\d+)\b", cell)
        )
        # A cell may hand its failure to another frame — 「As §1.0」, 「→ §1.0
        # Unreachable」 — and that is a treatment, so the arm stops asking for an
        # `F` number. What it must not stop doing is reading the reference: the
        # shape test alone accepted `§1.99` and any state name after it, which is
        # the one form of this cell nothing else checks. Arm L compares dispersal
        # *sets* against the frame that owns them and declines when it cannot
        # find that frame, so a wrong section number silenced both arms at once.
        #
        # A state name is taken only where the document writes one: capitalised,
        # up to the punctuation that ends the phrase. What follows a reference in
        # lower case is the sentence explaining it — 「the same three §1.0
        # disperses first paint into」 — and is not a citation to resolve.
        if re.search(r"(?:As |→\s*)§\d", cell):
            for target, state in CROSS_FRAME_RE.findall(cell):
                named = state.strip()
                if frame_u.resolve(target).empty:
                    add("A", f"L{r['line']}", f"「{r['state']}」 defers to §{target}, which is no frame")
                elif named and state_u.resolve(f"{target} · {named}").empty:
                    add("A", f"L{r['line']}", f"「{r['state']}」 defers to 「{named}」 in §{target}, which files no such state")
            continue
        goto = GOTO_RE.search(cell)
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
            if classifier_owned:
                continue
            if cited_treatments:
                add(
                    "A",
                    f"L{r['line']}",
                    f"「{r['state']}」 treats its failure with {'/'.join(cited_treatments)} "
                    f"and then lands nowhere §{frame} files: {cell!r}",
                )
                continue
        if cited_treatments:
            continue
        if classifier_owned:
            continue
        add("A", f"L{r['line']}", f"「{r['state']}」 failure cell names no F1–F5 and no known state: {cell!r}")

    # Arm L — class A finishing the job it abstained on four lines above.
    #
    # `→ A / B / C` is one predicate written twice: the frame that owns those
    # states says which set a failed load lands in, and every *other* frame that
    # defers its own failure to that frame restates the set. Two spellings of one
    # rule, in two sections, is the generator behind half this document's churn:
    # a destination is added where the states live and the deferral keeps the old
    # list, and both readings stay individually plausible. Nothing but a machine
    # compares them, because they are eight hundred lines apart.
    #
    # Two refusals keep it honest. A single destination is a landing, not a set —
    # 「→ §1.0 Unreachable」 says where this row goes and claims nothing about
    # §1.0's other exits — so only a dispersal of two or more is a restatement.
    # And a frame with more than one own-frame dispersal has no unique set to be
    # compared against, so the arm declines rather than picking one.
    own_sets: dict[str, list[tuple[dict[str, Any], frozenset[str]]]] = {}
    foreign: list[tuple[dict[str, Any], str, frozenset[str]]] = []
    dispersals = 0
    destinations = 0
    for r in reg:
        frames = FRAME_REF_RE.findall(r["frame"])
        if len(frames) != 1:
            continue
        own = frames[0]
        spread = dispersal(r["failure"], own)
        if not spread:
            continue
        dispersals += 1
        destinations += sum(1 for f, _ in spread if f != own)
        if len(spread) < 2:
            continue
        hits = [state_u.resolve(f"{f} · {n}") for f, n in spread]
        if any(h.empty or h.ambiguous for h in hits):
            continue  # an unresolvable destination is already class A's finding above
        canon = frozenset(h.hits[0] for h in hits)
        frames = {f for f, _ in spread}
        if frames == {own}:
            own_sets.setdefault(own, []).append((r, canon))
        elif len(frames) == 1:
            foreign.append((r, frames.pop(), canon))
    compared = 0
    for r, target, canon in foreign:
        rows = own_sets.get(target, [])
        if len(rows) != 1:
            continue
        home_row, home = rows[0]
        compared += 1
        if canon == home:
            continue
        missing = sorted(x.split(" · ", 1)[1] for x in home - canon)
        extra = sorted(x.split(" · ", 1)[1] for x in canon - home)
        drift = ", ".join(
            part
            for part in (
                f"does not name {'/'.join(missing)}" if missing else "",
                f"names {'/'.join(extra)}, which §{target} does not disperse into" if extra else "",
            )
            if part
        )
        add(
            "A",
            f"L{r['line']}",
            f"「{r['state']}」 defers its failure to §{target}'s set and {drift} "
            f"(§{target} states the set at L{home_row['line']})",
        )

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
    #
    # And in each register, only the column that names its object excuses — §0.5
    # what is missing, §0.4 which contracted route lives elsewhere. See
    # `declared_column` for what reading whole rows cost. A gap row's object is
    # the same set the marker arm reads, so both come from the one universe: a
    # row cannot register a route for the accounting arm while registering
    # something else for the paragraph citing it. A withdrawn row excuses
    # nothing either way: it says the absence is over, and an absence that is
    # over is not a reason for a contracted call to reach no state.
    accounted: set[str] = set(covered_routes)
    for _number, row in gaps.items():
        if row.registers:
            accounted.update(row.routes)
    for _n, cell in declared_column(doc.scope("scope note"), SCOPED_ROUTE_COLUMN_RE).items():
        accounted.update(normalize_route(m, path) for m, path in ANY_ROUTE_RE.findall(cell))
    contracted_mutations = sorted(
        r for r in auth["routes"].tokens() if r.startswith(("POST ", "PUT ", "PATCH ", "DELETE "))
    )
    for route in contracted_mutations:
        if route in accounted:
            continue
        add("A", "§0.8", f"{route} is contracted and reached by no §0.8 row, no §0.5 gap and no §0.4 row")

    # A route named by method word is a route nothing above can resolve, and the
    # cost is not stylistic. §1.6 attributed the shared guarded-change confirm to
    # "§1.3's whole-order `PUT`" for nineteen rounds; because that names no token,
    # the coverage arm, the guarded-status arm and the attribution arm in class C
    # all read the sentence as making no route claim, and it went on contradicting
    # both §0.8 and the contract matrix while reading perfectly well to a human.
    #
    # The rule governs §1 prose and stops there. §0.5 and §0.8 name other
    # sections' calls as part of registering them, and one withdrawn gap row
    # explains in prose that it deliberately names none — registers describing
    # themselves, not a frame borrowing a neighbour's request.
    #
    # The trigger is a bare method sharing a sentence with another frame's
    # number, because that pair is precisely "another page's request, in English".
    # A method word alone is usually the vocabulary rather than a call: §1.6
    # weighs "a `PATCH` that can only ever be rejected", a request that by
    # construction does not exist and so has no literal to name.
    method_words = 0
    for line_no, scope_text in doc.claims():
        here = doc.section_of(line_no)
        if not here.startswith("1."):
            continue
        live = STRUCK_RE.sub(" ", scope_text)
        method_words += len(BARE_METHOD_RE.findall(live)) + len(ANY_ROUTE_RE.findall(live))
        for sentence in SENTENCE_RE.split(live):
            bare = sorted(
                {
                    m.group(1)
                    for m in BARE_METHOD_RE.finditer(sentence)
                    if not METHOD_LIST_RE.search(sentence[max(0, m.start() - 4) : m.end() + 4])
                }
            )
            # Through the frames universe, because 「Frame 03's whole-order
            # `PUT`」 is the same claim as 「§1.3's whole-order `PUT`」 and this
            # document writes both. Reading only the section number left the
            # arm applying its rule to whichever half of the habit a sentence
            # happened to use — which is the shape hardening (4) fixed in the
            # guard-envelope arm, in the one other place a frame is cited.
            elsewhere = sorted(frame_refs(sentence, frame_u) - {here})
            # A decision paragraph may mention another frame's dispatch and a
            # mode value such as `PATCH`. A bare method is a route claim only
            # when the sentence attributes that method to the other frame's
            # request, not when it names a held value or classifier input.
            if re.search(r"\b(?:mode|value|reading)\s+`(?:GET|POST|PUT|PATCH|DELETE)`", sentence):
                bare = []
            if bare and elsewhere:
                add(
                    "A",
                    f"L{line_no}",
                    f"§{here} names §{'/§'.join(elsewhere)}'s request as "
                    f"`{'`/`'.join(bare)}` with no path, so no route arm can read the claim",
                )

    # ---- B ------------------------------------------------------------------
    cited: set[tuple[str, str]] = set()
    for sec, line, k in p["refs"]:
        cited.add((k, f"§{sec} L{line}"))
    for r in reg:
        for k in KEY_REF_RE.findall(r["copy"]):
            if k.startswith("models.") and (
                "server-owned" in r["copy"] or "consumed verbatim" in r["copy"]
            ):
                continue
            hit = copy_u.resolve(k)
            if hit.empty and (
                "server-owned" in r["copy"]
                or "consumed verbatim" in r["copy"]
                or k.startswith(("settings.", "presentation."))
            ):
                continue
            # The register may cite a closed contract-owned presentation key or
            # a key whose definition lives in another frame's prose table. Only
            # a spelling in a known UI-copy namespace is a local copy citation.
            if hit.empty and k.startswith("models."):
                continue
            cited.add((k, f"§0.8 L{r['line']}"))
    for key, where in sorted(cited):
        hit = copy_u.resolve(key)
        # A citation naming a family without writing the `*` is still naming
        # something the document defines: §1.5 writes `inventory.reason` where
        # the table declares `inventory.reason.transport` / `.rateLimited` /
        # `.unknown`. Asked here, in the judge, rather than by dropping the
        # citation upstream — the extractor's job is to hand every copy citation
        # to the universe, and a citation excused before it arrives is exactly
        # the silence this round exists to remove.
        if hit.empty and copy_u.resolve(f"{key}.*").empty:
            add("B", where, f"key `{key}` is cited and never defined")
        elif hit.ambiguous:
            add(
                "B",
                where,
                f"key `{key}` is cited and answers to {len(hit.hits)} definitions "
                f"({', '.join(hit.hits)}); the citation names none of them",
            )
    interpolate: dict[str, set[str]] = {}
    slot_consumers = 0
    for token, rows_ in copy_u.items():
        # i18next reads `_one` / `_other` as one key with two cardinalities, so a
        # stem carrying either suffix is a plural key and owes both rows. Which
        # is why the family test in `parse` cannot also be the completeness test:
        # it asks whether the rows *form* a family, and the survivor of an
        # incomplete pair forms nothing, so it fell through as an ordinary
        # singular key and the one case worth reporting was the one case that
        # could not be. Declaration is decided there; whether the declaration is
        # complete is decided here, where every other copy-row rule is.
        marked = {m.group(1) for m in (re.search(r"_(one|other)$", r["key"]) for r in rows_) if m}
        if marked and marked != {"one", "other"}:
            missing = sorted({"one", "other"} - marked)
            add(
                "B",
                f"L{min(r['line'] for r in rows_)}",
                f"key `{token}` is written as a plural family and declares no "
                f"`_{missing[0]}` row, so that cardinality renders nothing",
            )
        for row in rows_:
            if row["zh"] is None:
                continue  # unread, and said so once already on its own line
            if not row["en"]:
                add("B", f"L{row['line']}", f"key `{row['key']}` has no English column")
            if not row["zh"]:
                add("B", f"L{row['line']}", f"key `{row['key']}` has no Chinese column")
            localized_slots = {
                locale: set(SLOT_RE.findall(row[locale])) for locale in ("zh", "en")
            }
            plural = re.search(r"_(one|other)$", row["key"])
            form = plural.group(1) if plural else ""
            for locale in ("zh", "en"):
                other = "en" if locale == "zh" else "zh"
                missing = localized_slots[other] - localized_slots[locale]
                for slot in sorted(missing):
                    if (token, form, locale, slot) in OPTIONAL_PLURAL_SLOT_OMISSIONS:
                        continue
                    add(
                        "B",
                        f"L{row['line']}",
                        f"key `{row['key']}` omits `{{{{{slot}}}}}` from its {locale} copy",
                    )
            for slot in sorted(localized_slots["zh"] | localized_slots["en"]):
                if slot_u.resolve(slot).empty:
                    add(
                        "B",
                        f"L{row['line']}",
                        f"key `{row['key']}` interpolates `{{{{{slot}}}}}` with no §0.9 row",
                    )
                # The token, not `row["key"]`: a copy row writes its key relative
                # to its table and splits `_one` / `_other`, so the raw cell says
                # `subtitle` where the document says `adopt.subtitle`. §0.9 has to
                # name what a reader can look up.
                interpolate.setdefault(slot, set()).add(token)

    # A §0.9 row states what its slot means once, for every key that uses it, and
    # one sentence is true of whichever consumer the author had in mind. The
    # register said `{{status}}` was "the HTTP status the upstream returned" while
    # a fourth consumer filled it with supply health, and said `{{time}}` was
    # "a relative timestamp" while one consumer looked backwards and another
    # forwards. Neither is a wrong sentence about the slot; both are wrong about
    # a key, and no arm asked at key grain.
    #
    # So the row carries the set of keys that interpolate it, and this checks the
    # two enumerations are equal. It is not a naming rule: writing the set out is
    # what puts the odd consumer next to its siblings, where a slot that means two
    # things is visible as two things and gets split. Set equality is also the
    # only direction that catches a *new* consumer — an undeclared key is a key
    # whose meaning nobody checked against the row it borrowed.
    for slot, row in slot_u.items():
        cells = row["cells"]
        if len(cells) < 3 or cells[2] is None:
            # The consumer cell is the one a malformed row lost, and its loss is
            # reported once, on that row. Comparing keys against a cell nobody
            # read is how one broken row becomes a finding per key that borrows
            # the slot — `None` here means unread, which is not the same claim
            # as an empty cell's 「no key interpolates me」.
            continue
        declared = set(KEY_REF_RE.findall(cells[2]))
        actual = interpolate.get(slot, set())
        slot_consumers += len(declared)
        for key in sorted(declared - actual):
            add(
                "B",
                f"L{row['line']}",
                f"§0.9 declares `{key}` interpolates `{{{{{slot}}}}}` and no such copy row does",
            )
        for key in sorted(actual - declared):
            add(
                "B",
                f"L{row['line']}",
                f"key `{key}` interpolates `{{{{{slot}}}}}` and §0.9's row does not list it",
            )

    # The arm above reads §0.9's declaration against the copy tables. §0.7
    # writes that same declaration a second time, in prose, four hundred lines
    # away — "the count-bearing keys in this file are …" — and calls itself one
    # side of a set equality while doing it. That restatement had drifted in
    # both directions at once: it named `chain.derived.hops`, retired with the
    # chain-derived line it belonged to and defined nowhere since, and it left
    # out `guard.count`, added when the guard dialog got its plural family.
    #
    # An existence check over the whole document does not find either one, and
    # this was tried before it was cut: outside the frame sections a key
    # citation is as likely to be a *withdrawal record* as a live reference —
    # 「what S-1 deleted」 names the keys S-1 deleted, and a resolved-conflict
    # record names the keys the ruling removed. Those name a missing key on
    # purpose, and nothing structural separates them from a stale one, so the
    # check fired four times on this document and was right zero times. What
    # makes §0.7 different is not where it sits but what it does: it enumerates
    # a set it does not own.
    #
    # The signature has two halves, and both are structural. The set is named
    # by a paragraph mentioning exactly one slot — a paragraph naming two is
    # discussing their relation rather than either one's membership. The
    # members are a *list*: a contiguous run of keys separated by nothing but
    # list punctuation. That second half is what separates a restatement from a
    # mention, and prose alone cannot: five paragraphs in this document name one
    # slot and cite three or more keys while enumerating nothing — the keys are
    # subjects of their own sentences, several rows apart. Only in a run are the
    # keys members, because a run is what a document writes when it means
    # 「these and no others」. Three is the shortest run that can be one; two keys
    # is a pair, and this document writes pairs constantly.
    #
    # §0.9 itself is excluded — a declaration is not a restatement of itself.
    para: list[tuple[int, str]] = []
    paragraphs: list[list[tuple[int, str]]] = []
    for n, line in doc.scope("key names"):
        if line.strip() and not line.startswith("|"):
            para.append((n, line))
        elif para:
            paragraphs.append(para)
            para = []
    if para:
        paragraphs.append(para)
    for block in paragraphs:
        line_no = block[0][0]
        if doc.section_of(line_no) == "0.9":
            continue
        text_ = " ".join(line for _n, line in block)
        slots_named = set(SLOT_RE.findall(text_))
        if len(slots_named) != 1:
            continue
        slot = slots_named.pop()
        row = slot_u.resolve(slot)
        if row.empty or row.ambiguous:
            continue
        listed = {
            k
            for run in KEY_LIST_RE.findall(text_)
            for k in KEY_REF_RE.findall(run)
            if not k.endswith("*")
        }
        if len(listed) < 3:
            continue
        declared = interpolate.get(slot, set())
        for key in sorted(declared - listed):
            add(
                "B",
                f"L{line_no}",
                f"§{doc.section_of(line_no)} enumerates the keys interpolating "
                f"`{{{{{slot}}}}}` and leaves out `{key}`",
            )
        # The other direction needs no comparison against the declaration. The
        # arm above reads a *namespace* set derived from the copy tables, so a
        # key whose whole namespace was retired with it is invisible there by
        # construction — `chain.derived.hops` has no `chain.` row left to make
        # `chain` a namespace, and falls out of every existence check this file
        # runs. Inside an enumeration it does not need one: a sentence whose
        # subject is "the keys that do X" lists members, so a member no copy
        # table defines is a dead member, whatever its first segment says.
        for key in sorted(listed):
            if copy_u.resolve(key).empty:
                add(
                    "B",
                    f"L{line_no}",
                    f"§{doc.section_of(line_no)} enumerates `{key}` among the keys "
                    f"interpolating `{{{{{slot}}}}}` and no copy table defines it",
                )

    # A set is not the only thing this document writes twice. It also quotes
    # *shapes* — 「one `{{mode}} · {{status}}` line」 in a frame's element
    # inventory — which is a copy row's string written a second time, in the
    # section that tells an implementer what to draw. The row that actually
    # renders that line reads `{{mode}} · {{health}}`, and has since
    # `{{health}}` became its own slot; the inventory kept the word the slot
    # split off from. An implementer reading the inventory renders an HTTP
    # status where a health word belongs.
    #
    # Deletion is legal and substitution is not, which is what makes this
    # checkable without reading the prose around it. This document has a named
    # absence rule: a slot with nothing to fill it drops its segment, so §0.9
    # quotes `{{protocol}} 已认出 · {{request}} · {{reason}}` to show
    # `{{status}}` gone — a *subsequence* of the row's shape, and correct. No
    # rule swaps one slot for another at render time, so a quoted shape that is
    # not a subsequence of any row's shape is a shape nothing renders.
    shapes: set[tuple[str, ...]] = set()
    for _key, rows in copy_u.items():
        for row in rows:
            for cell in (row["zh"] or "", row["en"] or ""):
                seq = tuple(SLOT_RE.findall(cell))
                if len(seq) > 1:
                    shapes.add(seq)

    def _drawn(seq: tuple[str, ...]) -> bool:
        for shape in shapes:
            rest = iter(shape)
            if all(slot in rest for slot in seq):
                return True
        return False

    quoted = 0
    for n, line in doc.scope("rendered shapes"):
        if KEY_DEF_RE.match(line):
            continue
        for shape_text in SHAPE_RE.findall(line):
            seq = tuple(SLOT_RE.findall(shape_text))
            quoted += 1
            if not _drawn(seq):
                add(
                    "B",
                    f"L{n}",
                    f"§{doc.section_of(n)} quotes the line `{shape_text.strip()}` and no copy row renders it",
                )

    # Two arms above check a set that was enumerated twice in tokens — route
    # names, key names. The same generator writes a third shape neither can
    # see: a set declared once as copy keys and enumerated again as *the
    # strings those keys render*. §1.0's status mapping gained a sixth word and
    # §2's ink rule went on listing four of them, which is this document's
    # characteristic miss — the home table corrected, the derived sentence left
    # behind — in the one grain that had no arm.
    #
    # A family is a sub-namespaced stem, because that is where this document
    # declares a set: `gateway.group.status` is a vocabulary, while `addSub` is
    # one frame's bucket whose members are 取消 and 重试 — words that appear in
    # prose for reasons having nothing to do with any family. Reading buckets as
    # sets is what made a first cut of this arm fire thirty-three times on
    # sentences that merely mention a Cancel button.
    #
    # And prose has to *enumerate*, not mention: two or more members in a row,
    # separated by nothing but a list comma. §2's colour rule writes 「mint =
    # 使用中 / 正常, gold = 降级」 — two members of this family inside a legend
    # for a different vocabulary, naming examples and claiming to be no set. So
    # a run must also cover most of the family before the gate reads it as one.
    # That threshold is not a knob: this generator drops the value just added,
    # leaving n-1 of n, and n-1 is a majority of every family of three or more,
    # which is every family there is.
    vocabularies: dict[str, dict[str, str]] = {}
    for token, rows_ in copy_u.items():
        stem = token.rsplit(".", 1)[0]
        if "." not in stem:
            continue
        for row in rows_:
            zh = row["zh"]
            if zh and not SLOT_RE.search(zh) and len(zh) >= 2:
                vocabularies.setdefault(stem, {})[zh] = token
    vocabularies = {stem: v for stem, v in vocabularies.items() if len(v) >= 3}
    for line_no, scope_text in doc.claims():
        if (
            doc.section_of(line_no) == "0.8"
            or "pending K6" in scope_text
            or "pending-K6" in scope_text
        ):
            continue
        for stem, members in sorted(vocabularies.items()):
            listed: set[str] = set()
            run: list[str] = []
            for frag in LIST_COMMA_RE.split(scope_text) + [""]:
                named = {zh for zh in members if zh in frag}
                if len(named) == 1:
                    run.append(named.pop())
                    continue
                if len(run) >= 2:
                    listed.update(run)
                run = []
            if len(listed) * 2 > len(members) and listed < set(members):
                add(
                    "B",
                    f"L{line_no}",
                    f"§{doc.section_of(line_no)} enumerates `{stem}.*` as "
                    f"{'、'.join(sorted(listed))}; the copy tables define {len(members)}, "
                    f"so {'、'.join(sorted(set(members) - listed))} is missing",
                )

    # Arm M — the same "one predicate, two places" generator, one grain down.
    #
    # A frame's mapping table says what each value of a field is drawn as. A
    # §0.8 row keyed on one of those values says the same thing again, in its
    # copy column. When the table gains a qualifier and the register row keeps
    # the old key — or the other way round — both readings stay locally
    # plausible and the product ships two renderings for one value.
    #
    # The field is resolved through the frame's own table and nowhere else.
    # `cooldown` is a value of three frozen-schema fields and `degraded` of two,
    # so a document-wide value→field map answers with whichever it saw last; the
    # frame that draws both halves is the only correct resolver, and where even
    # it leaves the value ambiguous the arm declines. It also declines on a row
    # citing anything other than exactly one key: a row with two keys is drawing
    # a composite, and asserts no single value→key pairing to contradict.
    frame_maps = frame_mappings(doc, copy_u)
    pairs = sum(1 for maps in frame_maps.values() for dom in maps.values() for ks in dom.values() if ks)
    for r in reg:
        row_frames = FRAME_REF_RE.findall(r["frame"])
        if len(row_frames) != 1:
            continue
        maps = frame_maps.get(row_frames[0])
        if not maps:
            continue
        cited: set[str] = set()
        for cite in KEY_REF_RE.findall(r["copy"]):
            hit = copy_u.resolve(cite)
            if not hit.empty and not hit.ambiguous and not hit.wildcard:
                cited.add(hit.hits[0])
        if len(cited) != 1:
            continue
        key = cited.pop()
        for value in VALUE_TOKEN_RE.findall(r["entry"]):
            owners = [field for field, dom in maps.items() if dom.get(value)]
            if len(owners) != 1:
                continue
            drawn = maps[owners[0]][value]
            if key not in drawn:
                add(
                    "B",
                    f"L{r['line']}",
                    f"「{r['state']}」 enters on `{value}` and renders `{key}`, but "
                    f"§{row_frames[0]}'s own mapping of `{owners[0]}` draws that "
                    f"value as {', '.join('`' + k + '`' for k in sorted(drawn))}",
                )

    # ---- C ------------------------------------------------------------------
    for r in reg:
        if len(FRAME_REF_RE.findall(r["frame"])) != 1:
            continue
        if not r["exit"] or r["exit"] == "—":
            add("C", f"L{r['line']}", f"「{r['state']}」 has no exit")
            continue
        # And that the exit go somewhere. Class A resolves the destination of a
        # *failure* cell and has since the round that found 「F1 → Vanished
        # forever」; the success cell one column over was read for whether it was
        # empty and never for what it said, so 「first source → Vanished
        # forever」 was a complete exit. Same universe, same frame-qualified
        # identity, same two refusals — a cell may hand off to another frame, and
        # what follows an arrow in lower case is the sentence carrying on, not a
        # state name.
        #
        # Bounded by what this column actually is. An exit cell is prose — 「→ 06;
        # 来源顺序」, 「→ back here」, 「→ the row is gone」 — and a rule that
        # demands a state name from every segment reports sixty-nine of seventy
        # live rows. What the document does do consistently is write a state name
        # at the *head* of the segment when it names one at all: 「→ Ready while
        # any row is left」, 「→ Not installed or Unsupported host by the
        # manifest」, 「→ Saving again」. So the citation is the opening phrase of
        # a capitalised segment, the rest is the sentence qualifying it, and
        # 「→ Vanished forever」 is a segment that opens like a state name and
        # opens with none.
        frame = FRAME_REF_RE.findall(r["frame"])[0]
        local_rows = [r for r in reg if frame in FRAME_REF_RE.findall(r["frame"])]

        def row_match_length(said: str, row: dict[str, Any]) -> int:
            lengths: list[int] = []
            for name in state_spellings(row["state"]):
                if said == name or said.startswith(f"{name} "):
                    lengths.append(len(name))
                if " (" in name and (
                    said == name.split(" (", 1)[0]
                    or name.startswith(f"{said.split(' (', 1)[0]} (")
                ):
                    lengths.append(len(name.split(" (", 1)[0]))
            return max(lengths, default=0)

        def best_rows(said: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            scored = [(row_match_length(said, row), row) for row in rows]
            best = max((score for score, _row in scored), default=0)
            return [row for score, row in scored if score == best and score > 0]

        for segment in ARROW_SEGMENT_RE.findall(r["exit"]):
            for branch in ALTERNATIVE_RE.split(segment.split(";")[0]):
                said, stop = phrase(branch)
                if not said or not said[0].isupper() or "§" in said or stop == ":":
                    # A colon is this document introducing an explanation of the
                    # transition — 「Second pass: the dialog re-reads the sources」
                    # — not naming where it goes. Nothing it files as a state is
                    # ever written with one.
                    continue
                if re.match(r"^(?:[A-Z]{1,3}-?\d+|[A-Z]\d+[a-z]?)\b", said):
                    continue
                if re.match(r"^[A-Z][A-Za-z -]+['’]s\b", said) or " dispatch" in said:
                    continue
                # Either spelling of the same landing: the cell may qualify the
                # state's name (「Ready while any row is left」) or shorten it
                # (「Dirty」 for 「Dirty (uncommitted moves)」). Both are the
                # name plus or minus what the row says about the occasion; a
                # landing this frame does not file is neither.
                matches = best_rows(said, local_rows)
                if len(matches) == 1:
                    continue
                global_matches = best_rows(said, reg)
                if len(global_matches) == 1:
                    continue
                add(
                    "C",
                    f"L{r['line']}",
                    f"「{r['state']}」 exits to 「{said}」, which opens with no state §{frame} files",
                )

    # Arm N — the exit cell against the frame's own enumeration of the payload.
    #
    # Class C above asks that a state have an exit. Nothing asked the mirror:
    # that a state be somewhere's exit. Twice on two heads a frame wrote
    # 「Payload arrives → Ready」 with four dedicated states sitting directly
    # below it — Empty, Not started, Unsupported host, Impaired — each entered
    # by a reading of the very payload that row dispatches, and each reachable
    # from nothing. Both readings stay locally plausible: the dispatcher names a
    # real state, and the four rows state real conditions. Only putting them
    # side by side says the load can never arrive at four of its own landings.
    #
    # The frame's mapping table is what makes the comparison decidable, and the
    # reason the scope is the table rather than the register. A frame's rows
    # mix grains the page does not dispatch into — a per-source rendering drawn
    # *inside* Ready, a dialog step reached by a press — so demanding an in-edge
    # for every row fires on §1.1, §1.3, §1.4 and §1.5 where nothing is wrong.
    # A value the frame both draws in its mapping table *and* keys a state on is
    # that frame saying in two voices that the reading is a landing; two such
    # values make the table a second enumeration of the dispatch, one is a
    # single fact about one state and asserts no set.
    #
    # Three refusals. The dispatcher is arm L's own-frame dispersal row and only
    # where the frame has exactly one, because a frame with two has no single
    # row that owns where a load goes. A value keyed by no row, or by more than
    # one, is not a landing this arm can name. And satisfaction accepts either
    # spelling — the value or the state — because a dispatch may read
    # 「`not_started` → Not started」 or route by content in prose, and the
    # defect is a landing that appears in neither.
    #
    # Who counts as the dispatching row is the part of this that was too narrow.
    # A frame's dispersal row is found in *failure* cells, and only §1.0 has
    # one — so a frame that draws a mapping table and routes by it in an *exit*
    # was skipped whole, mapping table and all. A head shipped 「`standby` or
    # `active` → Ready」 in a recovery exit while its own frame drew `standby` as
    # Not supplying, and the gate stayed clean, because the frame holding both
    # halves of that contradiction was never entered.
    #
    # So a row also earns the dispatcher's duty by doing the dispatcher's job:
    # naming three or more of the domain's values in its exit. Two is a state
    # mentioning the readings next to it — 「`cooldown`, and `needs_action` if
    # the retry is refused」 asserts no set — and the arm's whole premise is that
    # only an enumeration can be checked against an enumeration. Above that line
    # the row is routing, and owes what a router owes: a landing for every value.
    # A frame may now have several such rows, and each is checked alone; unlike
    # the dispersal row, they are not a claim about a unique set, so two of them
    # is not the ambiguity that made arm L decline.
    def _unrouted(row: dict[str, Any], landings: dict[str, dict[str, Any]]) -> list[str]:
        names = {r["state"] for r in reg if r["frame"].lstrip("§") == row["frame"].lstrip("§")}
        named = names_spoken(row["exit"], names)
        spoken = VALUE_TOKEN_RE.findall(row["exit"])
        return [
            v for v, land in landings.items() if v not in spoken and land["state"] not in named
        ]

    # A value spoken anywhere in the exit was enough to call it routed, and
    # membership is not correspondence. The head this arm was written for shipped
    # 「`standby` or `active` → Ready」 while its own frame drew `standby` as Not
    # supplying; the arm read the mention, called the landing reached, and the
    # contradiction it exists to catch survived one spelling short of caught.
    #
    # Where the exit writes this document's own dispatch idiom — one or more
    # values, an arrow, the state they land in — the pair is decidable, so the
    # landing it names has to be the landing the register keys on that value.
    #
    # One refusal. A value written more than once in the same exit is being
    # split by a condition rather than dispatched: §1.6 sends 「an `active`
    # nothing adopts」 to Not supplying and 「an adopted `active`」 to Ready, and
    # both are right, so neither spelling is checked.
    #
    # There used to be a second, and it was the hole under the first: a target
    # naming no state of this frame was called prose and skipped. That reading
    # was safe only while a name was matched by containment, which quietly
    # answered almost any target with something; once a name has to be spoken as
    # a name, the skip becomes an escape hatch — 「`not_started` → Not
    # startedness」 names no state and, under the old refusal, said so to nobody.
    # So the two cases are told apart by how the target opens. A landing written
    # as a name — capitalised, where this document capitalises its states — is a
    # citation, and a citation that resolves to nothing is reported, which is the
    # rule every other arm here already keeps. A target opening in lower case is
    # the sentence continuing, and continues to be nothing to compare.
    def _misrouted(
        row: dict[str, Any], landings: dict[str, dict[str, Any]]
    ) -> list[tuple[str, dict[str, Any], list[str], str]]:
        names = {r["state"] for r in reg if r["frame"].lstrip("§") == row["frame"].lstrip("§")}
        spoken = VALUE_TOKEN_RE.findall(row["exit"])
        wrong: list[tuple[str, dict[str, Any], list[str], str]] = []
        for run, target in ARROW_LANDING_RE.findall(row["exit"]):
            # A landing ends where the sentence does. The last pair in a cell has
            # no following value to stop at, so without this it swallows the rest
            # of the exit and any state named there vouches for it.
            head = LANDING_END_RE.split(target)[0]
            named = names_spoken(head, names)
            if not named and not LANDING_NAME_RE.match(head):
                continue
            for value in VALUE_TOKEN_RE.findall(run):
                land = landings.get(value)
                if land is None or spoken.count(value) > 1 or land["state"] in named:
                    continue
                wrong.append((value, land, sorted(named), head.strip()))
        return wrong

    dispatch_landings = 0
    dispatch_rows = 0
    dispatch_pairs = 0
    for frame in sorted(reg_frames):
        maps = frame_maps.get(frame)
        if not maps:
            continue
        owning = own_sets.get(frame, [])
        primary = owning[0][0] if len(owning) == 1 else None
        rows = [r for r in reg if r["frame"].lstrip("§") == frame]
        for field, domain in sorted(maps.items()):
            landings: dict[str, dict[str, Any]] = {}
            for value in sorted(domain):
                keyed = [r for r in rows if value in VALUE_TOKEN_RE.findall(r["entry"])]
                if len(keyed) == 1 and keyed[0] is not primary:
                    landings[value] = keyed[0]
            if len(landings) < 2:
                continue
            routers = [primary] if primary is not None else []
            routers += [
                r
                for r in rows
                if r is not primary
                and len(set(VALUE_TOKEN_RE.findall(r["exit"])) & set(domain)) >= 3
            ]
            if not routers:
                continue
            dispatch_landings += len(landings)
            dispatch_rows += len(routers)
            for router in routers:
                role = (
                    "the row that says where a load lands"
                    if router is primary
                    else f"a row that routes this frame by `{field}`"
                )
                dispatch_pairs += len(ARROW_LANDING_RE.findall(router["exit"]))
                for value in _unrouted(router, landings):
                    row = landings[value]
                    add(
                        "C",
                        f"L{router['line']}",
                        f"§{frame} draws `{field}` reading `{value}` as 「{row['state']}」 "
                        f"(L{row['line']}), and 「{router['state']}」 — {role} — "
                        f"names neither, so that reading reaches no state",
                    )
                for value, row, named, head in _misrouted(router, landings):
                    if not named:
                        add(
                            "C",
                            f"L{router['line']}",
                            f"「{router['state']}」 — {role} — sends `{value}` to "
                            f"「{head}」, which §{frame} files as no state",
                        )
                        continue
                    add(
                        "C",
                        f"L{router['line']}",
                        f"「{router['state']}」 — {role} — sends `{value}` to "
                        f"{'、'.join('「' + n + '」' for n in named)}, and §{frame} keys "
                        f"「{row['state']}」 (L{row['line']}) on that same reading",
                    )
    for sec in sorted(p["inventories"]):
        if sec not in reg_frames:
            add("C", f"§{sec}", f"§{sec} draws an element inventory and has no §0.8 row")
    # And the mirror: a register row naming a frame that is not a §1 section.
    # Nothing asked it, so a row could point at §1.60 — or at a section deleted
    # in a later round — and its whole frame would quietly stop being checked,
    # because every other class reaches a row *through* its frame.
    for frame in sorted(reg_frames):
        if frame_u.resolve(frame).empty:
            row = next(r for r in reg if frame in FRAME_REF_RE.findall(r["frame"]))
            add("C", f"L{row['line']}", f"§0.8 rows are filed under §{frame}, which is no §1 section")

    # §0.8 files each state under the frame that reaches it, so "which frames
    # reach the guarded refusal" is a set the register already answers. §1 prose
    # answers it a second time whenever it explains who opens the shared confirm,
    # and the two answers must agree. §0.8 is the definition, so prose is the side
    # that gets checked — a register compared against itself reports itself.
    #
    # What marks a paragraph as being about the refusal is the guarded-refusal
    # envelope, not the frame it names: the sentence that had this wrong never
    # wrote a frame's id, it wrote "it". A response key every route returns
    # identifies nothing, so the marker is the keys only guarded routes carry.
    # Which frame the paragraph then *claims* is a separate question, and one the
    # frames universe answers under all three of a frame's names.
    #
    # And only inside a bold run. This document bolds the claim and narrates
    # around it, so an unbolded §1.x is a pointer — "the stored `hops` array, read
    # by §1.2 and §1.3" is telling the reader where else to look, not asserting
    # those frames are guarded callers. Reading the whole paragraph turned both of
    # those into gaps; reading only what the document marks as its claim does not.
    guarded_routes = {token for token, route in auth["routes"].items() if route["guarded"]}
    exclusive: set[str] = set()
    shared: set[str] = set()
    for token, route in auth["routes"].items():
        (exclusive if token in guarded_routes else shared).update(
            route["keys"] | route["response_keys"]
        )
    exclusive -= shared
    guard_frames = {
        frame
        for r in reg
        for frame in FRAME_REF_RE.findall(r["frame"])
        for m, path in ANY_ROUTE_RE.findall(f"{r['entry']} {r['failure']} {r['exit']}")
        if normalize_route(m, path) in guarded_routes
    }
    guard_claims = 0
    for line_no, scope_text in doc.claims():
        here = doc.section_of(line_no)
        if not here.startswith("1."):
            continue
        named = sorted(k for k in exclusive if f"`{k}`" in scope_text)
        if not named:
            continue
        guard_claims += 1
        claimed = {ref for run in BOLD_RUN_RE.findall(scope_text) for ref in frame_refs(run, frame_u)}
        for ref in sorted(claimed - guard_frames):
            add(
                "C",
                f"L{line_no}",
                f"§{here} claims §{ref} reaches the guarded refusal (it names "
                f"`{'`, `'.join(named)}`), and §0.8 files that state under "
                f"§{', §'.join(sorted(guard_frames))}",
            )
    # A section that splits its outcomes by origin — `②` reached by 添加, `②′`
    # by 拉取型号 — has made the origin part of what a state *is*. Every later
    # outcome then has an answer for both origins, and a failure written once in
    # such a section is not a shared state, it is a state whose second half was
    # never written: the reader is left to guess whether pulling from a stopped
    # engine fails the same way adding does. Only rows that hold a request open
    # are asked — F5 issues nothing, so it has nothing that can fail per origin.
    #
    # One row can legitimately have no twin: a step only one origin performs, of
    # which the persist `添加` owes is the example. That row is admissible only
    # when it *says so* in the ordinal the missing twin would carry — 「no ⑦′」 —
    # because a state whose second half was forgotten and a state whose second
    # half does not exist read identically otherwise, and only one of them can
    # be written down. The declaration is deliberately the twin's own name: a
    # ′ row deleted from the table leaves no such sentence behind, so the arm
    # keeps catching the case it was built for.
    for sec, rows in sorted(reg_by_section.items()):
        primed = {r["state"].split()[0].rstrip("′") for r in rows if "′" in r["state"]}
        if not primed:
            continue
        for r in rows:
            if "′" in r["state"] or not re.search(r"\bF[1-4]\b", r["failure"]):
                continue
            ordinal = r["state"].split()[0].rstrip("′")
            if ordinal in primed:
                continue
            if f"no {ordinal}′" in r["exit"]:
                continue
            twins = ", ".join(f"{s}′" for s in sorted(primed))
            add(
                "C",
                f"L{r['line']}",
                f"「{r['state']}」 is written once in a §{sec} that states its other failures "
                f"for both origins ({twins}); it has no ′ row and does not say 「no "
                f"{ordinal}′」 either",
            )

    # ---- D ------------------------------------------------------------------
    # A row whose columns could not be read can still be read as text, and its
    # keys are cited by §0.8 wherever in the row they sit. Class D asks 「does
    # any register row name this key」 — a question the text answers without the
    # column alignment the broken row lost. Withholding it reports the key's own
    # line, which is not where the mistake is.
    reg_cited = cited_rows(
        {t for r in reg for t in KEY_REF_RE.findall(r["copy"])}
        | {t for b in p["broken register rows"] for t in KEY_REF_RE.findall(b["text"])},
        copy_u,
    )
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

    # ---- the duplicate and malformed rules, for every universe at once ------
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
        # And the fourth rule, drained the same way and for the same reason: the
        # reader that met the row knows only that it could not read it, and the
        # universe knows which class answers for the table.
        for where, said in u.unreadable:
            add(u.owner, f"L{where}" if isinstance(where, int) else str(where), said)

    scale = {
        "register rows": len(reg),
        "distinct states": len(states),
        "frames with a register row": len(reg_frames),
        "frame sections with an element inventory": len(p["inventories"]),
        "mutating calls scanned": len(scanned),
        "contracted mutations to reach": len(contracted_mutations),
        "failure dispersals parsed": dispersals,
        "cross-frame failure destinations": destinations,
        "cross-frame dispersals compared": compared,
        "frame mapping-table value→key pairs": pairs,
        "dispatch landings compared": dispatch_landings,
        "dispatch rows checked": dispatch_rows,
        "dispatch value→state pairs read": dispatch_pairs,
        "copy tables / rows": f"{p['tables']} / {p['rows']}",
        "copy keys defined": len(copy_u),
        "condition-named keys": len(conditions),
        "interpolation slots declared": len(slot_u),
        "slot consumers declared": slot_consumers,
        "rendered shapes quoted in prose": quoted,
        "copy vocabularies prose can enumerate": len(vocabularies),
        "frame-prose route mentions": method_words,
        "frame-prose guard-envelope claims": guard_claims,
        "failure treatments declared": len(treatment_u),
        "frame sections declared": len(frame_u),
        "prose key references": len(p["refs"]),
        "authority: contracted routes read": len(auth["routes"]),
        "authority: answers named in a cell and spelled in a section": sum(
            1 for _t, r in auth["routes"].items() if r["named_answer"]
        ),
        "authority: schema enum declarations": len(auth["schema fields"]),
        "authority: shared guarded-envelope keys": len(auth["envelope"]),
        "authority: route claims": e_scale["routes"],
        "authority: request/response body claims": e_scale["bodies"],
        "authority: guarded-status claims": e_scale["status branches"],
        "authority: schema citations": e_scale["schema citations"],
        "authority: counted-vocabulary claims": e_scale["vocabulary claims"],
        "authority: attributed field claims": e_scale["attributed fields"],
        "authority: contract mapping tables": e_scale["contract mapping tables"],
        "authority: repo symbol citations": e_scale["repo symbols"],
        "authority: precise line citations": e_scale["authority lines"],
    }
    empty = [
        k
        for k, v in scale.items()
        if v == 0 and k not in TARGET_ZERO | {"authority: precise line citations"}
    ]
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
