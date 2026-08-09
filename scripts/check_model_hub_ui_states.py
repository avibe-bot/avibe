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

import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SPEC = Path("docs/plans/model-hub-ui-spec.md")

ROUTE_RE = re.compile(r"`(POST|PUT|PATCH|DELETE) (/api/[^`]*)`")
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


def read_source(target: str) -> tuple[str, str, str]:
    """Return (text, mode, fingerprint). Always read in this run, never cached."""
    if ":" in target or re.fullmatch(r"[0-9a-f]{7,40}", target):
        rev = target if ":" in target else f"{target}:{SPEC}"
        text = subprocess.run(
            ["git", "show", rev], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        mode = "same_run_git_rev"
    else:
        text = Path(target).read_text(encoding="utf-8")
        mode = "same_run_live_files"
    return text, mode, hashlib.sha256(text.encode()).hexdigest()[:16]


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
            routes.append((sec, i + 1, f"{meth} {path}"))

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


def check(target: str | Path = SPEC) -> dict[str, Any]:
    """`target` is the spec path, a git rev, or a repo root holding the spec."""
    if isinstance(target, Path) or "/" in str(target) or ":" not in str(target):
        candidate = Path(target)
        if candidate.is_dir():
            target = candidate / SPEC
    text, mode, fingerprint = read_source(str(target))
    p = parse(text)
    reg = p["register"]
    findings: list[dict[str, str]] = []

    states = {r["state"] for r in reg}
    reg_frames = {r["frame"].lstrip("§") for r in reg}

    def add(cls: str, where: str, msg: str) -> None:
        findings.append({"class": cls, "where": where, "message": msg})

    # ---- A ------------------------------------------------------------------
    covered = " || ".join(r["entry"] + " " + r["exit"] + " " + r["failure"] for r in reg)
    seen: set[str] = set()
    for sec, line, call in p["routes"]:
        if call in seen:
            continue
        seen.add(call)
        if call not in covered:
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

    # ---- D ------------------------------------------------------------------
    reg_copy = " ".join(r["copy"] for r in reg)
    conditions = [k for k, d in p["defined"].items() if k == d["raw"] and CONDITION_RE.search(k)]
    for key in sorted(conditions):
        d = p["defined"][key]
        base = re.sub(r"_(one|other)$", "", key)
        if base in reg_copy or key in reg_copy:
            continue
        add("D", f"L{d['line']}", f"condition key `{key}` is cited by no §0.8 row")

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
    }
    empty = [k for k, v in scale.items() if v == 0]
    return {
        "ok": not findings and not empty,
        "input_mode": mode,
        "input_fingerprint": fingerprint,
        "input_scale": scale,
        "empty_inventories": empty,
        "findings": findings,
    }


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else str(SPEC)
    r = check(target)
    print(f"input mode        : {r['input_mode']}")
    print(f"input fingerprint : {r['input_fingerprint']}")
    print("input scale (self-generated in this run):")
    for k, v in r["input_scale"].items():
        print(f"  {k:<42}: {v}")
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
    }
    for cls in "ABCD":
        items = by.get(cls, [])
        print(f"[{cls}] {labels[cls]}: {len(items)}")
        for f in items:
            print(f"   {f['where']:<16} {f['message']}")
    print(f"\ntotal gaps: {len(r['findings'])}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
