"""Read-only qualification fixtures; no cloud credentials, signing or upload."""
import json

import pytest

from scripts import model_hub_engine_merge as gate
from tests.test_model_hub_engine_hosted import commit


PATH = gate.hosted.LEDGER + "/0000000000000001.payload.json"
HEAD, MERGE, BLOB = "a" * 40, "b" * 40, "c" * 40


class Api:
    def __init__(self):
        self.pull = dict(number=1, merged=True, state="closed", merge_commit_sha=MERGE, base={
            "repo": {"full_name": gate.hosted.producer.REPOSITORY}, "ref": "master"}, head={"sha": HEAD},
            changed_files=1, merged_at="2026-01-02T10:00:00Z")
        self.file = dict(filename=PATH, status="added", sha=BLOB)
        self.review = dict(status="completed", commit_sha=HEAD, has_any_findings=False, has_unresolved_findings=False)
        self.runs = [{"id": 3, "app": {"id": 5}, "name": "lint", "status": "completed", "conclusion": "success",
                      "completed_at": "2026-01-02T09:57:00Z"}]
        self.runs.append(dict(self.runs[0], id=4, app={"slug": "github-actions"}, name="engine-allocation-preflight"))
        self.security = {"status": None}
        self.associated = [1]

    def get(self, path, *, query=None):
        if path == f"commits/{MERGE}/pulls":
            return [{"number": n} for n in self.associated]
        if path in ("pulls/1", "pulls/2"):
            return self.pull | {"number": int(path[-1])}
        if path == "pulls/1/files":
            return [self.file]
        if path == "pulls/2/files":
            return [self.file | {"filename": "unrelated"}]
        if path == f"contents/{PATH}" and query == {"ref": HEAD}:
            return {"type": "file", "sha": BLOB}
        if path == "issues/1/comments":
            return [{"user": {"login": "chatgpt-codex-connector[bot]"}, "updated_at": "2026-01-02T09:58:00Z",
                     "body": "<!-- codex-review-state:v1 " + json.dumps({"code_review": self.review, "security_review": self.security}) + " -->"}]
        if path == f"commits/{HEAD}/check-runs":
            return {"check_runs": self.runs}
        if path == "rules/branches/master":
            return [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "lint", "integration_id": 5}]}}]
        raise AssertionError(path)


@pytest.mark.parametrize("associated", [[1], [2, 1]])
def test_unique_allocation_pr_even_with_shared_queue_merge(associated):
    api = Api()
    api.associated = associated
    assert gate.prove_landing(api, commit=MERGE, path=PATH, blob=BLOB) == (1, HEAD)


@pytest.mark.parametrize("failure", ["direct", "closed", "association_only", "wrong_blob", "unchanged", "unreviewed", "old_head", "no_ci"])
def test_refuse_unproven_landing(failure):
    api = Api()
    if failure == "direct":
        api.associated = []
    elif failure == "closed":
        api.pull["merged"] = False
    elif failure == "association_only":
        api.pull["merge_commit_sha"] = HEAD
    elif failure == "wrong_blob":
        api.file["sha"] = HEAD
    elif failure == "unchanged":
        api.file["status"] = "modified"
    elif failure == "unreviewed":
        api.review["status"] = "running"
    elif failure == "old_head":
        api.review["commit_sha"] = MERGE
    else:
        api.runs = []
    with pytest.raises((gate.hosted.Error, gate.ci.ReleaseBackstopError)):
        gate.prove_landing(api, commit=MERGE, path=PATH, blob=BLOB)


@pytest.mark.parametrize("security", [{"status": "running"}, {"status": "failed"},
                                       {"status": "completed", "has_any_findings": True}])
def test_clean_code_review_does_not_override_active_security_review(security):
    api = Api()
    api.security = security
    with pytest.raises(gate.hosted.Error, match="review"):
        gate.prove_landing(api, commit=MERGE, path=PATH, blob=BLOB)


@pytest.mark.parametrize("mutation", [{"conclusion": "skipped"}, {"app": {"slug": "other"}},
                                     {"completed_at": "2026-01-03T10:00:00Z"}])
def test_required_aggregate_cannot_substitute_for_actual_premerge_preflight(mutation):
    api = Api()
    api.runs[-1].update(mutation)
    with pytest.raises(gate.hosted.Error, match="preflight"):
        gate.prove_landing(api, commit=MERGE, path=PATH, blob=BLOB)


def test_first_parent_delta_not_just_latest_master_tree(tmp_path):
    gate.hosted.git(tmp_path, "init", "-q")
    (tmp_path / "note").write_text("one")
    before = commit(tmp_path)
    allocation = tmp_path / PATH
    allocation.parent.mkdir(parents=True)
    allocation.write_bytes(b"test bytes")
    landed = commit(tmp_path)
    blob = gate.hosted.git(tmp_path, "rev-parse", landed + ":" + PATH).decode().strip()
    (tmp_path / "note").write_text("two")
    unrelated = commit(tmp_path)
    assert gate.push_additions(tmp_path, before=before, after=unrelated) == [(landed, PATH, blob)]
    with pytest.raises(gate.hosted.Error):
        gate.push_additions(tmp_path, before=landed, after=unrelated)
    allocation.write_bytes(b"changed")
    rewritten = commit(tmp_path)
    with pytest.raises(gate.hosted.Error):
        gate.push_additions(tmp_path, before=before, after=rewritten)
