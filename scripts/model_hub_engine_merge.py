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


def _check_review(api, number, head, merged_at):
    for comment in ci._paged(api, f"issues/{number}/comments", query={}):
        if (comment.get("user") or {}).get("login") != "chatgpt-codex-connector[bot]":
            continue
        if ci._timestamp(comment.get("updated_at"), context="review evidence") > merged_at:
            continue
        marker = re.search(r"<!-- codex-review-state:v1 (\{[^\n]*\}) -->", comment.get("body", ""))
        if marker:
            review = json.loads(marker[1]).get("code_review") or {}
            if (review.get("commit_sha") == head and review.get("status") == "completed"
                    and review.get("has_any_findings") is False and review.get("has_unresolved_findings") is False):
                return
    raise hosted.Error("no pre-merge clean review of the exact PR head")


def prove_landing(api, *, commit, path, blob):
    """Require the actual merged PR's file addition, reviewed head and required CI."""
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
        changes = [f for f in files if f.get("filename", "").startswith(hosted.LEDGER + "/")]
        if not any(f.get("filename") == path for f in changes):
            continue
        if len(changes) != 1 or changes[0].get("status") != "added" or changes[0].get("sha") != blob:
            raise hosted.Error("PR did not add exactly the landed allocation bytes")
        head = ci._sha((pull.get("head") or {}).get("sha"), context="merged PR head")
        contents = api.get(f"contents/{path}", query={"ref": head})
        if contents.get("type") != "file" or contents.get("sha") != blob:
            raise hosted.Error("landed blob differs from the exact reviewed head")
        matches.append((number, head, ci._timestamp(pull.get("merged_at"), context="merge time")))
    if len(matches) != 1:
        raise hosted.Error("no unique merged allocation PR for this landing")
    number, head, merged_at = matches[0]
    _check_review(api, number, head, merged_at)
    ci._require_green_checks(api, pull_number=number, head_sha=head, merged_at=merged_at)
    return number, head


def push_additions(repo, *, before, after):
    """Walk each actual first-parent delta, including intermediate queue commits."""
    for sha in (before, after):
        if not re.fullmatch("[0-9a-f]{40}", sha) or sha == "0" * 40:
            raise hosted.Error("not an ordinary master update")
    commits = hosted.git(repo, "rev-list", "--first-parent", "--reverse", f"{before}..{after}").decode().splitlines()
    previous = before
    additions = []
    for commit in commits:
        if hosted.git(repo, "rev-parse", f"{commit}^1").decode().strip() != previous:
            raise hosted.Error("not a fast-forward first-parent master update")
        delta = hosted.git(repo, "diff-tree", "--no-commit-id", "--no-renames", "--no-abbrev", "-r", "--raw", "-z",
                           previous, commit, "--", hosted.LEDGER).split(b"\0")
        for header, name in zip(delta[:-1:2], delta[1::2], strict=True):
            path = name.decode("ascii")
            record = re.fullmatch(rb":000000 100644 0{40} ([0-9a-f]{40}) A", header)
            if not record or not re.fullmatch(hosted.LEDGER + r"/[0-9]{16}\.payload\.json", path):
                raise hosted.Error("ledger change is not a regular allocation addition")
            additions.append((commit, path, record[1].decode()))
        previous = commit
    if previous != after or not additions:
        raise hosted.Error("no new master allocation")
    return additions
