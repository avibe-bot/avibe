"""Read-only evidence for one allocation introduced into master by a push.

The path and blob come from the actual first-parent change, not an event's file
summary or an association guessed from the latest master. In a queue merge,
several PRs can share the landing commit; identify the PR by its added blob.
"""
from __future__ import annotations

import json
import re

from scripts import ci_release_backstop as ci
from scripts import model_hub_engine_hosted as hosted


class MissingHistoricalEvidence(hosted.Error):
    """A complete, successful lookup returned no historical evidence (not an API error)."""


def _check_review(api, number, head, merged_at):
    found = False
    for comment in ci._paged(api, f"issues/{number}/comments", query={}):
        if (comment.get("user") or {}).get("login") != "chatgpt-codex-connector[bot]":
            continue
        if ci._timestamp(comment.get("updated_at"), context="review evidence") > merged_at:
            continue
        marker = re.search(r"<!-- codex-review-state:v1 (\{[^\n]*\}) -->", comment.get("body", ""))
        if marker:
            verdict = json.loads(marker[1])
            review = verdict.get("code_review") or {}
            if review.get("commit_sha") != head:
                continue
            found = True
            active = [part for part in verdict.values() if isinstance(part, dict) and part.get("status") is not None]
            if (review.get("commit_sha") == head and review.get("status") == "completed"
                    and review.get("has_unresolved_findings") is False and all(
                        part.get("status") == "completed" and part.get("has_any_findings") is False
                        and part.get("has_unresolved_findings") is not True for part in active)):
                return
    if not found:
        raise MissingHistoricalEvidence("missing historical exact-head review")
    raise hosted.Error("no pre-merge clean review of the exact PR head")


def require_preflight(api, head, merged_at):
    # Required aggregate checks can pass on older workflows without this job.
    # A skipped/pending check, a different integration or a post-merge rerun
    # does not validate the committed rehearsal receipt.
    checks = ci._paged(api, f"commits/{head}/check-runs", query={"filter": "all"})
    candidates = [c for c in checks if c.get("name") == "engine-allocation-preflight"
                  and (c.get("app") or {}).get("slug") == "github-actions"
                  and c.get("completed_at") and ci._timestamp(c["completed_at"], context="preflight") <= merged_at]
    if not any(c.get('name') == 'engine-allocation-preflight' for c in checks):
        raise MissingHistoricalEvidence('missing historical allocation preflight')
    if not candidates or max(candidates, key=lambda c: c["id"]).get("conclusion") != "success":
        raise hosted.Error("no successful allocation preflight before merge")


def merged_identity(api, *, commit, path, blob, recovery=False):
    """Bind this added Git blob to one real master merge and its exact PR head."""
    matches = []
    for associated in ci._paged(api, f"commits/{commit}/pulls", query={}):
        number = associated.get("number")
        if type(number) is not int or number <= 0:
            raise hosted.Error("invalid associated PR")
        pull = api.get(f"pulls/{number}")
        base = pull.get("base") or {}
        if not (pull.get("merged") is True and pull.get("state") == "closed" and pull.get("number") == number
                and pull.get("merge_commit_sha") == commit and base.get("ref") == "master"
                and (base.get("repo") or {}).get("full_name") == hosted.producer.REPOSITORY):
            continue
        files = ci._paged(api, f"pulls/{number}/files", query={})
        if len(files) != pull.get("changed_files"):
            raise hosted.Error("incomplete PR file evidence")
        changes = [f for f in files if f.get('filename') == path or f.get('filename', '').startswith(hosted.LEDGER + '/')]
        if not any(f.get("filename") == path for f in changes):
            continue
        if len(changes) != 1 or changes[0].get("status") != "added" or changes[0].get("sha") != blob:
            raise hosted.Error("PR did not add exactly the landed allocation bytes")
        sidecar = path.removeprefix(hosted.LEDGER + '/').replace('.payload.json', '.json')
        allowed = {path, 'patches/model-hub-engine-allocation-evidence/' + sidecar,
                   'patches/model-hub-engine-emergency/' + sidecar}
        if recovery:
            allowed = {path}
        if any(f.get("status") != "added" or f.get("filename") not in allowed for f in files):
            raise hosted.Error("allocation PR must not change its own checks or unrelated files")
        head = ci._sha((pull.get("head") or {}).get("sha"), context="merged PR head")
        contents = api.get(f"contents/{path}", query={"ref": head})
        if contents.get("type") != "file" or contents.get("sha") != blob:
            raise hosted.Error("landed blob differs from the exact reviewed head")
        matches.append((number, head, ci._timestamp(pull.get("merged_at"), context="merge time")))
    if len(matches) != 1:
        raise hosted.Error("no unique merged allocation PR for this landing")
    return matches[0]


def prove_landing(api, *, commit, path, blob, recovery=False):
    number, head, merged_at = merged_identity(api, commit=commit, path=path, blob=blob, recovery=recovery)
    missing = []
    for check in (lambda: _check_review(api, number, head, merged_at), lambda: require_preflight(api, head, merged_at)):
        try:
            check()
        except MissingHistoricalEvidence as exc:
            missing.append(str(exc))
    ci._require_green_checks(api, pull_number=number, head_sha=head, merged_at=merged_at)
    if missing:
        raise MissingHistoricalEvidence('; '.join(missing))
    return number, head


def added_blobs(repo, before, after):
    delta = hosted.git(repo, "diff-tree", "--no-commit-id", "--no-renames", "--no-abbrev", "-r", "--raw", "-z",
                       before, after, "--", hosted.LEDGER).split(b"\0")
    for header, name in zip(delta[:-1:2], delta[1::2], strict=True):
        path = name.decode("ascii")
        record = re.fullmatch(rb":000000 100644 0{40} ([0-9a-f]{40}) A", header)
        if not record or not re.fullmatch(hosted.LEDGER + r"/[0-9]{16}\.payload\.json", path):
            raise hosted.Error("ledger change is not a regular allocation addition")
        yield after, path, record[1].decode()


def iter_push_additions(repo, *, before, after):
    """Walk each actual first-parent delta, including intermediate queue commits."""
    for sha in (before, after):
        if not re.fullmatch("[0-9a-f]{40}", sha) or sha == "0" * 40:
            raise hosted.Error("not an ordinary master update")
    commits = hosted.git(repo, "rev-list", "--first-parent", "--reverse", f"{before}..{after}").decode().splitlines()
    previous = before
    for commit in commits:
        if hosted.git(repo, "rev-parse", f"{commit}^1").decode().strip() != previous:
            raise hosted.Error("not a fast-forward first-parent master update")
        yield from added_blobs(repo, previous, commit)
        previous = commit
    if previous != after:
        raise hosted.Error("master landing range is not contiguous")


def push_additions(repo, *, before, after):
    additions = list(iter_push_additions(repo, before=before, after=after))
    if not additions:
        raise hosted.Error("no new master allocation")
    return additions
