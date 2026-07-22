"""Stage-2 EverOS behavior probes and redacted suite aggregation.

The harness owns every child process and every provider root. Production-shaped
reads always use public ``/search``; the narrow retention inspector is the sole
research-only storage reader and is never used for retrieval scoring.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import socketserver
import threading
import time
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

from .constants import CRITERIA_IDS
from .corpus import Corpus, CorpusMessage, CorpusQuery, evaluate_query, flatten_search_response, load_corpus
from .environment import (
    ProviderSettings,
    assert_clean_harness_source,
    checked_workspace_root,
    discover_provider_settings,
    locked_environment_python,
    verify_locked_environment,
)
from .errors import HarnessError, LaunchError
from .generated_config import write_generated_config
from .identifiers import validate_run_id
from .launcher import EverOSProcess
from .metrics import CallMetrics, read_call_metrics, read_egress_hosts
from .paths import create_owner_directory, ensure_owner_directory, read_private_text, runtime_root, write_private_text
from .provider import EverOSClient, HttpShape
from .reports import (
    build_report,
    load_report,
    set_criterion,
    write_report,
    write_stage2_summary,
)
from .reports import local_timezone_name
from .research_inspection import RetentionInspection, inspect_retention

_OWNER_ID = "00000000-0000-4000-8000-000000000002"
_READINESS_TIMEOUT_SECONDS = 300.0
_SEARCH_POLL_SECONDS = 2.0
_RSS_INTERVAL_SECONDS = 5.0
_IDLE_SAMPLE_SECONDS = 600.0
_MIB = 1024 * 1024
_GIB = 1024 * 1024 * 1024
_QUALITY_CRITERIA = {"temporal_all", "negatives_all", "positive_top8_rate"}
_LOOPBACK_HOSTS = {"localhost"}
_SAFE_NOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,_()/%=-]{0,511}$")


@dataclass(frozen=True)
class ProbeMeasurement:
    label: str
    metrics: CallMetrics
    egress: tuple[str, ...]
    peak_rss_bytes: int
    rss_samples: tuple[int, ...]
    root_growth_bytes: int
    uds_only_verified: bool
    http_shapes: tuple[HttpShape, ...]


class _RssSampler:
    def __init__(self, process: EverOSProcess) -> None:
        self._process = process
        self._samples: list[int] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def samples(self) -> tuple[int, ...]:
        return tuple(self._samples)

    def start(self) -> None:
        self._sample()
        self._thread = threading.Thread(target=self._run, name="memory-poc-rss", daemon=True)
        self._thread.start()

    def stop(self) -> tuple[int, ...]:
        self._sample()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(_RSS_INTERVAL_SECONDS * 2, 1.0))
        return self.samples

    def _run(self) -> None:
        while not self._stop.wait(_RSS_INTERVAL_SECONDS):
            self._sample()

    def _sample(self) -> None:
        child = self._process.process
        if child is None or child.poll() is not None:
            return
        try:
            root = psutil.Process(child.pid)
            processes = [root, *root.children(recursive=True)]
            rss = sum(item.memory_info().rss for item in processes if item.is_running())
        except psutil.Error:
            return
        if rss >= 0:
            self._samples.append(rss)


class _Probe:
    def __init__(self, suite: Stage2Suite, label: str, settings: ProviderSettings) -> None:
        self.suite = suite
        self.label = label
        self.settings = settings
        self.stage_dir = create_owner_directory(suite.run_dir / label, anchor=suite.state)
        self.logs_dir = ensure_owner_directory(self.stage_dir / "logs", anchor=suite.state)
        self.everos_root = ensure_owner_directory(self.stage_dir / "everos-root", anchor=suite.state)
        self.child_home = ensure_owner_directory(self.stage_dir / "child-home", anchor=suite.state)
        write_generated_config(everos_root=self.everos_root, timezone=local_timezone_name(), anchor=suite.state)
        self.metrics_path = self.logs_dir / "request-counts.jsonl"
        self.egress_path = self.logs_dir / "egress.jsonl"
        self._root_initial_bytes = _directory_size(self.everos_root)
        self.process = EverOSProcess(
            python=suite.python,
            everos_root=self.everos_root,
            child_home=self.child_home,
            state_root=suite.state,
            settings=settings,
            metrics_path=self.metrics_path,
            egress_path=self.egress_path,
            owner_id=_OWNER_ID,
        )
        self._sampler: _RssSampler | None = None
        self._rss_samples: list[int] = []
        self._http_shapes: list[HttpShape] = []
        self._closed = False

    def start(self, *, timeout_seconds: float | None = None) -> EverOSClient:
        if self._closed:
            raise HarnessError("probe_closed")
        self.process.start()
        self._sampler = _RssSampler(self.process)
        self._sampler.start()
        return self.process.client(timeout_seconds=timeout_seconds)

    def restart(self, *, timeout_seconds: float | None = None) -> EverOSClient:
        self._stop_sidecar()
        return self.start(timeout_seconds=timeout_seconds)

    def remember_shapes(self, client: EverOSClient) -> None:
        self._http_shapes.extend(client.observed_http_shapes)

    def close(self) -> ProbeMeasurement:
        if self._closed:
            raise HarnessError("probe_closed")
        try:
            self._stop_sidecar()
        finally:
            self._closed = True
        metrics = read_call_metrics(self.metrics_path)
        egress = read_egress_hosts(self.egress_path)
        measurement = ProbeMeasurement(
            label=self.label,
            metrics=metrics,
            egress=egress,
            peak_rss_bytes=max(self._rss_samples, default=0),
            rss_samples=tuple(self._rss_samples),
            root_growth_bytes=max(_directory_size(self.everos_root) - self._root_initial_bytes, 0),
            uds_only_verified=self.process.uds_only_verified,
            http_shapes=tuple(self._http_shapes),
        )
        self.suite.observe(measurement)
        return measurement

    def _stop_sidecar(self) -> None:
        sampler = self._sampler
        if sampler is not None:
            self._rss_samples.extend(sampler.stop())
            self._sampler = None
        if self.process.process is None:
            return
        cleanup_error: LaunchError | None = None
        for _attempt in range(2):
            try:
                self.process.stop()
            except LaunchError as exc:
                cleanup_error = exc
            else:
                cleanup_error = None
            if self.process.child_reaped:
                if cleanup_error is not None:
                    raise cleanup_error
                return
        raise HarnessError("sidecar_child_not_reaped") from cleanup_error


class Stage2Suite:
    """One shared final report with fresh sidecar roots per stage/probe."""

    def __init__(
        self,
        *,
        run_id: str,
        workspace: Path,
        state: Path,
        run_dir: Path,
        settings: ProviderSettings,
        python: Path,
        corpus: Corpus,
        completed_stages: tuple[str, ...],
        evidence_lines: tuple[str, ...],
    ) -> None:
        self.run_id = run_id
        self.workspace = workspace
        self.state = state
        self.run_dir = run_dir
        self.settings = settings
        self.python = python
        self.corpus = corpus
        self.completed_stages = completed_stages
        self.evidence_lines = evidence_lines
        self._report: dict[str, Any] | None = None

    @classmethod
    def open(cls, *, run_id: str, stage: str, workspace: Path | None = None) -> Stage2Suite:
        validate_run_id(run_id)
        root = checked_workspace_root(workspace)
        settings = discover_provider_settings(root)
        assert_clean_harness_source(root)
        python = verify_locked_environment(locked_environment_python(root))
        corpus = load_corpus(root)
        state = ensure_owner_directory(runtime_root(root), anchor=root)
        runs_dir = ensure_owner_directory(state / "runs", anchor=state)
        run_dir = runs_dir / run_id
        if not run_dir.exists():
            create_owner_directory(run_dir, anchor=state)
            metadata = {"stage": "stage2", "corpus_revision": corpus.revision, "completed_stages": []}
            write_private_text(run_dir / "run.json", json.dumps(metadata, sort_keys=True) + "\n", anchor=state)
            write_private_text(run_dir / "evidence.json", json.dumps({"lines": []}, sort_keys=True) + "\n", anchor=state)
            report = build_report(run_id=run_id, settings=settings, corpus_revision=corpus.revision)
            write_report(
                run_dir / "report.json",
                report,
                anchor=state,
                fixture_texts=tuple(item.text for item in corpus.messages),
                secret_values=(settings.llm_api_key, settings.embedding_api_key),
            )
            completed: tuple[str, ...] = ()
            evidence_lines: tuple[str, ...] = ()
        else:
            ensure_owner_directory(run_dir, anchor=state)
            metadata = _read_suite_json(run_dir / "run.json")
            if metadata.get("stage") != "stage2" or metadata.get("corpus_revision") != corpus.revision:
                raise HarnessError("stage2_run_metadata_invalid")
            raw_completed = metadata.get("completed_stages")
            if not isinstance(raw_completed, list) or not all(isinstance(item, str) for item in raw_completed):
                raise HarnessError("stage2_run_metadata_invalid")
            failed_stage = metadata.get("failed_stage")
            if failed_stage is not None:
                if not isinstance(failed_stage, str) or not _safe_label(failed_stage):
                    raise HarnessError("stage2_run_metadata_invalid")
                raise HarnessError("stage2_run_already_failed")
            completed = tuple(raw_completed)
            evidence = _read_suite_json(run_dir / "evidence.json")
            raw_lines = evidence.get("lines")
            if not isinstance(raw_lines, list) or not all(_safe_note(item) for item in raw_lines):
                raise HarnessError("stage2_evidence_invalid")
            evidence_lines = tuple(raw_lines)
        if stage in completed:
            raise HarnessError("stage_already_completed")
        return cls(
            run_id=run_id,
            workspace=root,
            state=state,
            run_dir=run_dir,
            settings=settings,
            python=python,
            corpus=corpus,
            completed_stages=completed,
            evidence_lines=evidence_lines,
        )

    def report(self) -> dict[str, Any]:
        if self._report is None:
            self._report = load_report(
                self.run_dir / "report.json",
                fixture_texts=tuple(item.text for item in self.corpus.messages),
                secret_values=(self.settings.llm_api_key, self.settings.embedding_api_key),
            )
        return self._report

    def probe(self, label: str, *, settings: ProviderSettings | None = None) -> _Probe:
        if not _safe_label(label):
            raise HarnessError("probe_label_invalid")
        return _Probe(self, label, settings or self.settings)

    def observe(self, measurement: ProbeMeasurement) -> None:
        report = self.report()
        resources = report["resources"]
        resources["llm_calls"] += measurement.metrics.llm_calls
        resources["embedding_calls"] += measurement.metrics.embedding_calls
        resources["peak_rss_bytes"] = max(resources["peak_rss_bytes"], measurement.peak_rss_bytes)
        resources["root_growth_bytes"] = max(resources["root_growth_bytes"], measurement.root_growth_bytes)
        report["egress"] = sorted(set(report["egress"]).union(measurement.egress))
        self._write_report(report)

    def complete(self, stage: str, *, report: dict[str, Any], evidence_lines: tuple[str, ...]) -> Path:
        if stage in self.completed_stages or not _safe_label(stage):
            raise HarnessError("stage2_completion_invalid")
        all_lines = self.evidence_lines + evidence_lines
        if not all(_safe_note(line) for line in all_lines):
            raise HarnessError("stage2_evidence_invalid")
        report["recommendation"] = recommendation_for_criteria(report["criteria"])
        self._write_report(report)
        completed = self.completed_stages + (stage,)
        write_private_text(
            self.run_dir / "run.json",
            json.dumps(
                {"stage": "stage2", "corpus_revision": self.corpus.revision, "completed_stages": list(completed)},
                sort_keys=True,
            )
            + "\n",
            anchor=self.state,
        )
        write_private_text(
            self.run_dir / "evidence.json",
            json.dumps({"lines": list(all_lines)}, sort_keys=True) + "\n",
            anchor=self.state,
        )
        write_stage2_summary(
            self.run_dir / "summary.md",
            settings=self.settings,
            report=report,
            completed_stages=completed,
            evidence_lines=all_lines,
            fixture_texts=tuple(item.text for item in self.corpus.messages),
            anchor=self.state,
        )
        self.completed_stages = completed
        self.evidence_lines = all_lines
        return self.run_dir / "report.json"

    def _write_report(self, report: dict[str, Any]) -> None:
        self._report = report
        write_report(
            self.run_dir / "report.json",
            report,
            anchor=self.state,
            fixture_texts=tuple(item.text for item in self.corpus.messages),
            secret_values=(self.settings.llm_api_key, self.settings.embedding_api_key),
        )


def percentile(values: tuple[int, ...] | list[int], fraction: float) -> int:
    if not values or not 0 < fraction <= 1:
        raise HarnessError("percentile_invalid")
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * fraction) - 1]


def recommendation_for_criteria(criteria: list[dict[str, Any]]) -> str:
    states = {item.get("id"): item.get("state") for item in criteria if isinstance(item, dict)}
    if any(states.get(identifier) == "fail" for identifier in _QUALITY_CRITERIA):
        return "stop"
    if all(states.get(identifier) == "pass" for identifier in CRITERIA_IDS):
        return "official"
    return "fork"


def _read_suite_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_private_text(path))
    except (HarnessError, OSError, ValueError) as exc:
        raise HarnessError("stage2_run_metadata_invalid") from exc
    if not isinstance(value, dict):
        raise HarnessError("stage2_run_metadata_invalid")
    return value


def _safe_label(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value))


def _safe_note(value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE_NOTE.fullmatch(value))


def _directory_size(root: Path) -> int:
    total = 0
    for directory, _subdirectories, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            path = Path(directory) / filename
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if info.st_size >= 0 and info.st_mode & 0o170000 == 0o100000:
                total += info.st_size
    return total


def run_stage2(*, stage: str, run_id: str, workspace: Path | None = None) -> Path:
    """Run one Stage-2 probe into the shared final report directory."""
    suite = Stage2Suite.open(run_id=run_id, stage=stage, workspace=workspace)
    runners = {
        "quality": _run_quality,
        "pool": _run_pool,
        "duplicate": _run_duplicate,
        "retention": _run_retention,
        "footprint": _run_footprint,
    }
    runner = runners.get(stage)
    if runner is None:
        raise HarnessError("stage_not_implemented")
    try:
        return runner(suite)
    except HarnessError as exc:
        _write_failed_stage(suite, stage=stage, error=exc)
        raise


def _run_quality(suite: Stage2Suite) -> Path:
    report = suite.report()
    all_outcomes: list[tuple[int, CorpusQuery, int, Any]] = []
    searchable_ms: list[int] = []
    query_ms: list[int] = []
    evidence: list[str] = []
    launch_verified = True

    for trial in range(1, 4):
        probe = suite.probe(f"quality-{trial}")
        client: EverOSClient | None = None
        try:
            client = probe.start()
            started_epoch_ms = time.time_ns() // 1_000_000
            for message in suite.corpus.messages:
                session_id = f"quality-{trial}-{message.session_key}"
                payload = _message_payload(message, session_id=session_id, started_epoch_ms=started_epoch_ms)
                add_ms = _elapsed_ms(lambda: client.add(session_id=session_id, messages=[payload]))
                flush_ms = _elapsed_ms(lambda: client.flush(session_id=session_id))
                hints = suite.corpus.search_hints_for(message)
                if not hints:
                    raise HarnessError("corpus_message_search_hint_missing")
                observed_searchable_ms = _wait_searchable(
                    client,
                    owner_id=_OWNER_ID,
                    query=message.text,
                    hints=hints,
                )
                message_label = f"q{trial}-{message.session_key}-{message.seq}"
                report["latency"]["add_ms"][message_label] = add_ms
                report["latency"]["flush_ms"][message_label] = flush_ms
                report["latency"]["searchable_ms"][message_label] = observed_searchable_ms
                searchable_ms.append(observed_searchable_ms)

            for query in suite.corpus.queries:
                started = time.monotonic()
                result = client.search(owner_id=_OWNER_ID, query=query.query)
                latency_ms = int((time.monotonic() - started) * 1000)
                items = flatten_search_response(result, owner_id=_OWNER_ID)
                outcome = evaluate_query(query, items)
                query_label = f"q{trial}-{query.query_id}"
                report["quality"].append(
                    {
                        "query_id": query_label,
                        "pass": outcome.passed,
                        "rank": outcome.expected_rank,
                        "latency_ms": latency_ms,
                    }
                )
                report["latency"]["query_ms"][query_label] = latency_ms
                all_outcomes.append((trial, query, latency_ms, outcome))
                query_ms.append(latency_ms)
                evidence.extend(_quality_evidence_lines(query_label, query, outcome, items))
        finally:
            if client is not None:
                probe.remember_shapes(client)
            measurement = probe.close()
            launch_verified = launch_verified and measurement.uds_only_verified

    temporals = [outcome for _trial, query, _latency, outcome in all_outcomes if query.type == "temporal"]
    negatives = [outcome for _trial, query, _latency, outcome in all_outcomes if query.type == "negative"]
    per_trial_positive_rates = []
    for trial in range(1, 4):
        trial_outcomes = [outcome for run, query, _latency, outcome in all_outcomes if run == trial and query.type == "positive"]
        rate = sum(item.passed for item in trial_outcomes) / len(trial_outcomes)
        per_trial_positive_rates.append(rate)
        evidence.append(f"quality trial {trial} positive top8 {sum(item.passed for item in trial_outcomes)}/{len(trial_outcomes)}")
    temporal_passed = sum(item.passed for item in temporals)
    negative_passed = sum(item.passed for item in negatives)
    query_p95 = percentile(query_ms, 0.95) / 1000
    searchable_p95 = percentile(searchable_ms, 0.95) / 60000
    _set_measurement(
        report,
        "temporal_all",
        passed=temporal_passed == len(temporals),
        value=temporal_passed,
        threshold=len(temporals),
    )
    _set_measurement(
        report,
        "negatives_all",
        passed=negative_passed == len(negatives),
        value=negative_passed,
        threshold=len(negatives),
    )
    _set_measurement(
        report,
        "positive_top8_rate",
        passed=all(rate >= 0.9 for rate in per_trial_positive_rates),
        value=min(per_trial_positive_rates),
        threshold=0.9,
    )
    _set_measurement(report, "query_p95_s", passed=query_p95 <= 2, value=query_p95, threshold=2)
    _set_measurement(
        report,
        "searchable_p95_min",
        passed=searchable_p95 <= 5,
        value=searchable_p95,
        threshold=5,
    )
    _set_boolean(report, "launcher_uds_only", launch_verified)
    _set_boolean(report, "no_internals_needed", launch_verified)
    evidence.extend(
        (
            f"quality temporal pass {temporal_passed}/{len(temporals)}",
            f"quality negatives pass {negative_passed}/{len(negatives)}",
            f"quality query p95 ms {int(query_p95 * 1000)}",
            f"quality searchable p95 ms {int(searchable_p95 * 60000)}",
            "quality profile search not measured accepted known behavior",
        )
    )
    return suite.complete("quality", report=report, evidence_lines=tuple(evidence))


def _run_pool(suite: Stage2Suite) -> Path:
    report = suite.report()
    probe = suite.probe("pool")
    client: EverOSClient | None = None
    outcomes: list[Any] = []
    evidence: list[str] = []
    try:
        client = probe.start()
        started_epoch_ms = time.time_ns() // 1_000_000
        for session_key, seq in (("s1", 7), ("s2", 5), ("s3", 1)):
            message = suite.corpus.message(session_key, seq)
            session_id = f"pool-{session_key}"
            payload = _message_payload(message, session_id=session_id, started_epoch_ms=started_epoch_ms)
            client.add(session_id=session_id, messages=[payload])
            client.flush(session_id=session_id)
            hints = suite.corpus.search_hints_for(message)
            if not hints:
                raise HarnessError("corpus_message_search_hint_missing")
            _wait_searchable(client, owner_id=_OWNER_ID, query=message.text, hints=hints)
        outcomes.extend(_run_pool_queries(client, suite.corpus))
        client = probe.restart()
        outcomes.extend(_run_pool_queries(client, suite.corpus))
    finally:
        if client is not None:
            probe.remember_shapes(client)
        measurement = probe.close()
    passed = all(outcome.passed for outcome in outcomes)
    _set_boolean(report, "restart_preserves", passed)
    _set_boolean(report, "launcher_uds_only", measurement.uds_only_verified)
    _set_boolean(report, "no_internals_needed", measurement.uds_only_verified)
    evidence.extend(
        (
            f"personal pool cross session assertions pass {sum(item.passed for item in outcomes)}/{len(outcomes)}",
            f"personal pool restart preserves {str(passed).lower()}",
        )
    )
    return suite.complete("pool", report=report, evidence_lines=tuple(evidence))


def _run_pool_queries(client: EverOSClient, corpus: Corpus) -> tuple[Any, ...]:
    outcomes: list[Any] = []
    for query_id in ("q004", "q010", "q040"):
        query = corpus.query(query_id)
        result = client.search(owner_id=_OWNER_ID, query=query.query)
        outcomes.append(evaluate_query(query, flatten_search_response(result, owner_id=_OWNER_ID)))
    return tuple(outcomes)


def _run_retention(suite: Stage2Suite) -> Path:
    report = suite.report()
    probe = suite.probe("retention")
    client: EverOSClient | None = None
    evidence: list[str] = []
    restart_preserves = False
    try:
        client = probe.start()
        message = next(item for item in suite.corpus.messages if "buffered-tail" in item.tags)
        session_id = "retention-buffered"
        payload = _message_payload(message, session_id=session_id, started_epoch_ms=time.time_ns() // 1_000_000)
        client.add(session_id=session_id, messages=[payload])
        buffered_before = client.research_buffer(owner_id=_OWNER_ID, session_id=session_id)
        inspection_before = inspect_retention(probe.everos_root)

        client = probe.restart()
        buffered_after_restart = client.research_buffer(owner_id=_OWNER_ID, session_id=session_id)
        inspection_after_restart = inspect_retention(probe.everos_root)
        client.flush(session_id=session_id)
        hints = suite.corpus.search_hints_for(message)
        if not hints:
            raise HarnessError("corpus_message_search_hint_missing")
        searchable_ms = _wait_searchable(client, owner_id=_OWNER_ID, query=message.text, hints=hints)
        report["latency"]["searchable_ms"]["retention-buffered"] = searchable_ms
        inspection_after_flush = inspect_retention(probe.everos_root)

        client = probe.restart()
        result_after_restart = client.search(owner_id=_OWNER_ID, query=message.text)
        restart_preserves = _search_contains_any_hint(result_after_restart, owner_id=_OWNER_ID, hints=hints)
        inspection_after_extracted_restart = inspect_retention(probe.everos_root)
        buffered_visible = _buffer_contains_session(buffered_before, session_id)
        buffered_after_visible = _buffer_contains_session(buffered_after_restart, session_id)
        evidence.extend(
            (
                f"retention buffered public before flush {str(buffered_visible).lower()}",
                f"retention buffered public after restart {str(buffered_after_visible).lower()}",
                _retention_line("before flush", inspection_before),
                _retention_line("after restart", inspection_after_restart),
                _retention_line("after flush", inspection_after_flush),
                _retention_line("after extracted restart", inspection_after_extracted_restart),
                f"retention extracted search restart {str(restart_preserves).lower()}",
            )
        )
    finally:
        if client is not None:
            probe.remember_shapes(client)
        measurement = probe.close()
    clear_removed = _clear_owned_provider_root(probe.everos_root, stage_dir=probe.stage_dir)
    _set_boolean(report, "restart_preserves", restart_preserves)
    _set_boolean(report, "clear_removes_all", clear_removed)
    _set_boolean(report, "launcher_uds_only", measurement.uds_only_verified)
    _set_boolean(report, "no_internals_needed", measurement.uds_only_verified)
    evidence.append(f"retention full root clear {str(clear_removed).lower()}")
    return suite.complete("retention", report=report, evidence_lines=tuple(evidence))


def _message_payload(message: CorpusMessage, *, session_id: str, started_epoch_ms: int) -> dict[str, Any]:
    return {
        "sender_id": _OWNER_ID,
        "role": "user",
        "timestamp": started_epoch_ms + message.occurred_offset_ms,
        "content": message.text,
    }


def _wait_searchable(client: EverOSClient, *, owner_id: str, query: str, hints: tuple[str, ...]) -> int:
    started = time.monotonic()
    deadline = started + _READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = client.search(owner_id=owner_id, query=query)
        if _search_contains_any_hint(result, owner_id=owner_id, hints=hints):
            return int((time.monotonic() - started) * 1000)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(_SEARCH_POLL_SECONDS, remaining))
    raise HarnessError("searchable_timeout")


def _search_contains_any_hint(value: Any, *, owner_id: str, hints: tuple[str, ...]) -> bool:
    for item in flatten_search_response(value, owner_id=owner_id):
        normalised = _normalise(item.text)
        if any(_normalise(hint) in normalised for hint in hints):
            return True
    return False


def _quality_evidence_lines(
    query_label: str,
    query: CorpusQuery,
    outcome: Any,
    items: tuple[Any, ...],
) -> tuple[str, ...]:
    """Render value-free expected and public result identities for the summary."""
    if query.expect is None:
        expected = "none"
    else:
        seq_refs = ",".join(str(item) for item in query.expect.seq_refs)
        expected = f"{query.expect.kind} {query.expect.session_key} seq {seq_refs}"
    rank = "none" if outcome.expected_rank is None else str(outcome.expected_rank)
    lines = [
        f"quality {query_label} expected {expected} pass {str(outcome.passed).lower()} rank {rank}",
    ]
    for offset in range(0, len(items), 4):
        group = items[offset : offset + 4]
        rendered = ",".join(
            f"{item.kind}_rank{item.rank}_id{item.identity[:48]}" for item in group
        ) or "none"
        lines.append(f"quality {query_label} returned {offset + 1}-{offset + len(group)} {rendered}")
    if not items:
        lines.append(f"quality {query_label} returned none")
    return tuple(lines)


def _buffer_contains_session(value: Any, session_id: str) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("unprocessed_messages"), list)
        and any(isinstance(item, dict) and item.get("session_id") == session_id for item in value["unprocessed_messages"])
    )


def _retention_line(label: str, inspection: RetentionInspection) -> str:
    values = (
        _display_count(inspection.unprocessed_buffer_rows),
        _display_count(inspection.memcell_rows),
        _display_count(inspection.profile_files),
        _display_count(inspection.episode_files),
        _display_count(inspection.atomic_fact_files),
        _display_count(inspection.sqlite_files),
    )
    return "retention {} buffer {} memcell {} profile {} episode {} facts {} sqlite {}".format(label, *values)


def _display_count(value: int | None) -> str:
    return "unavailable" if value is None else str(value)


def _clear_owned_provider_root(root: Path, *, stage_dir: Path) -> bool:
    try:
        root.relative_to(stage_dir)
        root.lstat()
    except (FileNotFoundError, ValueError):
        return False
    if not root.is_dir() or root.is_symlink():
        return False
    shutil.rmtree(root)
    if root.exists():
        return False
    for directory, subdirectories, filenames in os.walk(stage_dir, followlinks=False):
        if Path(directory) == root:
            continue
        if any(name.endswith(".lance") for name in subdirectories):
            return False
        for filename in filenames:
            if filename.endswith((".db", ".lance")) or filename in {"everos.toml", "ome.toml"}:
                return False
    return True


def _elapsed_ms(callback: Any) -> int:
    started = time.monotonic()
    callback()
    return int((time.monotonic() - started) * 1000)


def _set_measurement(report: dict[str, Any], criterion_id: str, *, passed: bool, value: float | int, threshold: float | int) -> None:
    set_criterion(
        report["criteria"],
        criterion_id,
        state="pass" if passed else "fail",
        value=value,
        threshold=threshold,
    )


def _set_boolean(report: dict[str, Any], criterion_id: str, passed: bool) -> None:
    _set_measurement(report, criterion_id, passed=passed, value=1 if passed else 0, threshold=1)


def _normalise(value: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", value).casefold()


def _write_failed_stage(suite: Stage2Suite, *, stage: str, error: HarnessError) -> None:
    """Persist one redacted failure report; callers deliberately do not retry it."""
    try:
        report = suite.report()
        report["recommendation"] = "stop"
        suite._write_report(report)
        line = f"stage {stage} failed {_safe_failure_code(error)}"
        if _safe_note(line):
            write_private_text(
                suite.run_dir / "evidence.json",
                json.dumps({"lines": list(suite.evidence_lines + (line,))}, sort_keys=True) + "\n",
                anchor=suite.state,
            )
            write_private_text(
                suite.run_dir / "run.json",
                json.dumps(
                    {
                        "stage": "stage2",
                        "corpus_revision": suite.corpus.revision,
                        "completed_stages": list(suite.completed_stages),
                        "failed_stage": stage,
                    },
                    sort_keys=True,
                )
                + "\n",
                anchor=suite.state,
            )
            write_stage2_summary(
                suite.run_dir / "summary.md",
                settings=suite.settings,
                report=report,
                completed_stages=("failed",),
                evidence_lines=suite.evidence_lines + (line,),
                fixture_texts=tuple(item.text for item in suite.corpus.messages),
                anchor=suite.state,
            )
    except HarnessError:
        return


def _safe_failure_code(error: HarnessError) -> str:
    value = str(error)
    if 0 < len(value) <= 128 and value.isascii() and re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        return value
    return "harness_failure"


def _run_duplicate(suite: Stage2Suite) -> Path:
    report = suite.report()
    probe = suite.probe("duplicate")
    clients: list[EverOSClient] = []
    evidence: list[str] = []
    restart_preserves = False
    episode_matches = 0
    fact_matches = 0
    same_timestamp_survived = 0
    concurrent_outcome = "not run"
    try:
        client = probe.start()
        clients.append(client)
        started_epoch_ms = time.time_ns() // 1_000_000

        buffered = next(item for item in suite.corpus.messages if "buffered-tail" in item.tags)
        buffered_session = "duplicate-buffer"
        client.add(
            session_id=buffered_session,
            messages=[_message_payload(buffered, session_id=buffered_session, started_epoch_ms=started_epoch_ms)],
        )
        buffer_view = client.research_buffer(owner_id=_OWNER_ID, session_id=buffered_session)
        before_flush_buffered = _buffer_contains_session(buffer_view, buffered_session)
        before_flush_episode = bool(flatten_search_response(buffer_view, owner_id=_OWNER_ID))
        client.flush(session_id=buffered_session)
        buffered_hints = suite.corpus.search_hints_for(buffered)
        if not buffered_hints:
            raise HarnessError("corpus_message_search_hint_missing")
        buffered_searchable_ms = _wait_searchable(
            client,
            owner_id=_OWNER_ID,
            query=buffered.text,
            hints=buffered_hints,
        )
        report["latency"]["searchable_ms"]["duplicate-buffer"] = buffered_searchable_ms

        restart_message = suite.corpus.message("s1", 7)
        restart_session = "duplicate-restart"
        client.add(
            session_id=restart_session,
            messages=[_message_payload(restart_message, session_id=restart_session, started_epoch_ms=started_epoch_ms)],
        )
        client = probe.restart()
        clients.append(client)
        client.flush(session_id=restart_session)
        restart_hints = suite.corpus.search_hints_for(restart_message)
        if not restart_hints:
            raise HarnessError("corpus_message_search_hint_missing")
        restart_result = client.search(owner_id=_OWNER_ID, query=restart_message.text)
        restart_preserves = _search_contains_any_hint(restart_result, owner_id=_OWNER_ID, hints=restart_hints)

        kill_case = next(item for item in suite.corpus.messages if "kill-case" in item.tags)
        retry_session = "duplicate-response-loss"
        retry_payload = _message_payload(kill_case, session_id=retry_session, started_epoch_ms=started_epoch_ms)
        client.add(session_id=retry_session, messages=[retry_payload])
        # Simulate a lost 2xx response by deliberately discarding the returned data,
        # then replay the exact same request once through the public API.
        client.add(session_id=retry_session, messages=[retry_payload])
        client.flush(session_id=retry_session)
        retry_query = suite.corpus.query("q015")
        retry_result = client.search(owner_id=_OWNER_ID, query=retry_query.query)
        retry_items = flatten_search_response(retry_result, owner_id=_OWNER_ID)
        retry_hint = retry_query.expect.text_hint if retry_query.expect is not None else ""
        episode_matches = sum(item.kind == "episode" and _normalise(retry_hint) in _normalise(item.text) for item in retry_items)
        fact_matches = sum(item.kind == "atomic_fact" and _normalise(retry_hint) in _normalise(item.text) for item in retry_items)

        same_timestamp = started_epoch_ms + 1000
        same_session = "duplicate-same-ms"
        same_messages = [
            _synthetic_message("same timestamp alpha marker", same_timestamp),
            _synthetic_message("same timestamp bravo marker", same_timestamp),
        ]
        client.add(session_id=same_session, messages=same_messages)
        client.flush(session_id=same_session)
        alpha = client.search(owner_id=_OWNER_ID, query="same timestamp alpha marker")
        bravo = client.search(owner_id=_OWNER_ID, query="same timestamp bravo marker")
        same_timestamp_survived = int(
            _search_contains_any_hint(alpha, owner_id=_OWNER_ID, hints=("alpha",))
        ) + int(_search_contains_any_hint(bravo, owner_id=_OWNER_ID, hints=("bravo",)))

        concurrent_outcome, concurrent_clients = _concurrent_retry_probe(
            probe,
            started_epoch_ms=started_epoch_ms,
        )
        clients.extend(concurrent_clients)
    finally:
        for client in clients:
            probe.remember_shapes(client)
        measurement = probe.close()

    error_lines = _run_error_matrix(suite)
    duplicate_count = max(episode_matches - 1, 0)
    report["duplicates"] = {
        "observed": (
            f"simulated response loss retry episodes {episode_matches} facts {fact_matches} "
            f"same timestamp survived {same_timestamp_survived} concurrent {concurrent_outcome}"
        ),
        "count": duplicate_count,
    }
    _set_boolean(report, "restart_preserves", restart_preserves)
    _set_boolean(report, "launcher_uds_only", measurement.uds_only_verified)
    _set_boolean(report, "no_internals_needed", measurement.uds_only_verified)
    evidence.extend(
        (
            f"duplicate buffered before flush {str(before_flush_buffered).lower()} completed episode {str(before_flush_episode).lower()}",
            f"duplicate flush searchable ms {buffered_searchable_ms}",
            f"duplicate unflushed restart preserves {str(restart_preserves).lower()}",
            f"duplicate simulated response loss retry episode matches {episode_matches} fact matches {fact_matches}",
            f"duplicate same millisecond distinct survival {same_timestamp_survived}/2",
            f"duplicate concurrent retry outcome {concurrent_outcome}",
            *error_lines,
        )
    )
    return suite.complete("duplicate", report=report, evidence_lines=tuple(evidence))


def _synthetic_message(content: str, timestamp: int) -> dict[str, Any]:
    return {"sender_id": _OWNER_ID, "role": "user", "timestamp": timestamp, "content": content}


def _concurrent_retry_probe(probe: _Probe, *, started_epoch_ms: int) -> tuple[str, tuple[EverOSClient, ...]]:
    first = probe.process.client()
    retry = probe.process.client(timeout_seconds=10)
    payload = [_synthetic_message("concurrent retry marker", started_epoch_ms + 2000)]
    outcomes: list[str] = []

    def invoke_first() -> None:
        try:
            first.add(session_id="duplicate-concurrent", messages=payload)
        except HarnessError as exc:
            outcomes.append(f"first {_safe_failure_code(exc)}")
        else:
            outcomes.append("first completed")

    worker = threading.Thread(target=invoke_first, name="memory-poc-concurrent-add", daemon=True)
    worker.start()
    time.sleep(0.1)
    try:
        retry.add(session_id="duplicate-concurrent", messages=payload)
    except HarnessError as exc:
        retry_outcome = _safe_failure_code(exc)
    else:
        retry_outcome = "completed"
    worker.join(timeout=70)
    if worker.is_alive():
        probe._stop_sidecar()
        worker.join(timeout=5)
        return f"retry {retry_outcome} first force stopped", (first, retry)
    first_outcome = outcomes[0] if outcomes else "first unknown"
    return f"retry {retry_outcome} {first_outcome}", (first, retry)


class _FakeUpstream:
    """Local OpenAI-shaped failure source used only for error classification."""

    def __init__(self, *, status_code: int, marker: bytes | None = None) -> None:
        self.status_code = status_code
        self.marker = marker
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(length)
                status = parent.status_code if parent.marker is None or parent.marker in body else 200
                response = b'{"error":{"code":"poc"}}'
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="memory-poc-fake-upstream", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self._server.server_address[1]}/v1"

    def __enter__(self) -> _FakeUpstream:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def _run_error_matrix(suite: Stage2Suite) -> tuple[str, ...]:
    lines: list[str] = []
    uds_probe = suite.probe("duplicate-error-uds")
    client: EverOSClient | None = None
    socket_path: Path | None = None
    try:
        client = uds_probe.start(timeout_seconds=2)
        socket_path = uds_probe.process.socket_path
    finally:
        if client is not None:
            uds_probe.remember_shapes(client)
        uds_probe.close()
    uds_outcome = "unexpected success"
    if socket_path is not None:
        try:
            EverOSClient(socket_path, timeout_seconds=1).health()
        except HarnessError as exc:
            uds_outcome = _safe_failure_code(exc)
    lines.append(f"error sidecar uds outcome {uds_outcome} status none")

    connection_settings = replace(
        suite.settings,
        llm_base_url="http://localhost:1/v1",
        embedding_base_url="http://localhost:1/v1",
    )
    lines.extend(_error_case_lines(suite, "endpoint", connection_settings, "endpoint error marker"))
    invalid_key_settings = replace(
        suite.settings,
        llm_api_key="poc-invalid-credential",
        embedding_api_key="poc-invalid-credential",
    )
    lines.extend(_error_case_lines(suite, "invalid credential", invalid_key_settings, "credential error marker"))
    with _FakeUpstream(status_code=429) as server:
        rate_limit_settings = replace(suite.settings, llm_base_url=server.base_url, embedding_base_url=server.base_url)
        lines.extend(_error_case_lines(suite, "synthetic rate limit", rate_limit_settings, "rate limit marker"))
    invalid_model_settings = replace(suite.settings, llm_model="poc-invalid-configured-model")
    lines.extend(_error_case_lines(suite, "invalid model", invalid_model_settings, "invalid model marker"))
    with _FakeUpstream(status_code=500, marker=b"poc-content-error-marker") as server:
        content_settings = replace(suite.settings, llm_base_url=server.base_url, embedding_base_url=server.base_url)
        lines.extend(
            _error_case_lines(suite, "synthetic content processing", content_settings, "poc-content-error-marker")
        )
    return tuple(lines)


def _error_case_lines(suite: Stage2Suite, label: str, settings: ProviderSettings, content: str) -> tuple[str, ...]:
    probe = suite.probe(f"duplicate-error-{label.replace(' ', '-')}", settings=settings)
    client: EverOSClient | None = None
    outcome = "unexpected success"
    try:
        client = probe.start(timeout_seconds=45)
        try:
            client.add(
                session_id=f"error-{label.replace(' ', '-')}",
                messages=[_synthetic_message(content, time.time_ns() // 1_000_000)],
            )
        except HarnessError as exc:
            outcome = _safe_failure_code(exc)
    finally:
        if client is not None:
            probe.remember_shapes(client)
        measurement = probe.close()
    status = ",".join(str(item.status_code) for item in measurement.http_shapes if item.route.endswith("/add")) or "none"
    closed = ",".join(
        str(item.closed_code) for item in measurement.http_shapes if item.closed_code is not None
    ) or "absent"
    return (
        f"error {label} outcome {outcome} status {status} closed {closed}",
        *_error_shape_lines(label, measurement.http_shapes),
    )


def _error_shape_lines(label: str, shapes: tuple[HttpShape, ...]) -> tuple[str, ...]:
    """Summarize public error envelopes without retaining response values."""
    unique = tuple(dict.fromkeys(shapes))
    if not unique:
        return (f"error {label} public response shape absent",)
    lines: list[str] = []
    for shape in unique:
        request_keys = ",".join(shape.request_keys) or "none"
        response_keys = ",".join(shape.response_keys) or "none"
        data_keys = ",".join(shape.data_keys) or "none"
        paths = ",".join(_safe_schema_path(path) for path in shape.response_schema_paths) or "none"
        closed = "absent" if shape.closed_code is None else str(shape.closed_code)
        lines.append(
            f"error {label} public shape route {shape.route} status {shape.status_code} closed {closed} "
            f"request keys {request_keys} response keys {response_keys} data keys {data_keys} paths {paths}"
        )
    return tuple(lines)


def _safe_schema_path(value: str) -> str:
    return value.replace("[]", "_array").replace(":", "=")


def _run_footprint(suite: Stage2Suite) -> Path:
    report = suite.report()
    evidence: list[str] = []
    idle_probe = suite.probe("footprint-idle")
    idle_client: EverOSClient | None = None
    try:
        idle_client = idle_probe.start()
        deadline = time.monotonic() + _IDLE_SAMPLE_SECONDS
        while time.monotonic() < deadline:
            time.sleep(min(30.0, deadline - time.monotonic()))
    finally:
        if idle_client is not None:
            idle_probe.remember_shapes(idle_client)
        idle_measurement = idle_probe.close()

    loopback_settings = replace(
        suite.settings,
        llm_base_url="http://localhost:1/v1",
        embedding_base_url="http://localhost:1/v1",
    )
    loopback_probe = suite.probe("footprint-loopback", settings=loopback_settings)
    loopback_client: EverOSClient | None = None
    loopback_outcome = "unexpected success"
    try:
        loopback_client = loopback_probe.start(timeout_seconds=45)
        try:
            loopback_client.add(
                session_id="footprint-loopback",
                messages=[_synthetic_message("loopback egress marker", time.time_ns() // 1_000_000)],
            )
        except HarnessError as exc:
            loopback_outcome = _safe_failure_code(exc)
    finally:
        if loopback_client is not None:
            loopback_probe.remember_shapes(loopback_client)
        loopback_measurement = loopback_probe.close()

    environment_size = _directory_size(suite.state / "env")
    resources = report["resources"]
    resources["env_size_bytes"] = environment_size
    if idle_measurement.rss_samples:
        idle_p95 = percentile(idle_measurement.rss_samples, 0.95)
        resources["idle_rss_p95_bytes"] = idle_p95
        _set_measurement(report, "idle_rss_p95_mib", passed=idle_p95 <= 512 * _MIB, value=idle_p95 / _MIB, threshold=512)
    else:
        set_criterion(report["criteria"], "idle_rss_p95_mib", state="not_measured", value=None, threshold=None)
    peak = resources["peak_rss_bytes"]
    root_growth = resources["root_growth_bytes"]
    _set_measurement(report, "env_size_gib", passed=environment_size <= _GIB, value=environment_size / _GIB, threshold=1)
    _set_measurement(report, "peak_rss_mib", passed=peak <= 1536 * _MIB, value=peak / _MIB, threshold=1536)
    _set_measurement(report, "root_growth_mib", passed=root_growth <= 512 * _MIB, value=root_growth / _MIB, threshold=512)

    all_egress = set(report["egress"])
    configured_hosts = _configured_hosts(suite.settings)
    external_hosts = all_egress - _LOOPBACK_HOSTS
    egress_configured_only = external_hosts.issubset(configured_hosts)
    loopback_only = set(loopback_measurement.egress).issubset(_LOOPBACK_HOSTS)
    _set_boolean(report, "egress_configured_only", egress_configured_only)
    _set_boolean(report, "loopback_no_egress", loopback_only)
    _set_boolean(report, "launcher_uds_only", idle_measurement.uds_only_verified and loopback_measurement.uds_only_verified)
    _set_boolean(report, "no_internals_needed", idle_measurement.uds_only_verified and loopback_measurement.uds_only_verified)

    query_values = tuple(report["latency"]["query_ms"].values())
    if query_values:
        evidence.append(f"footprint query p50 ms {percentile(query_values, 0.5)} p95 ms {percentile(query_values, 0.95)}")
    evidence.extend(
        (
            f"footprint env bytes {environment_size}",
            f"footprint idle rss p95 bytes {resources['idle_rss_p95_bytes']}",
            f"footprint peak rss bytes {peak}",
            f"footprint root growth bytes {root_growth}",
            f"footprint llm calls {resources['llm_calls']} embedding calls {resources['embedding_calls']}",
            f"footprint egress configured only {str(egress_configured_only).lower()}",
            f"footprint loopback outcome {loopback_outcome} external free {str(loopback_only).lower()}",
            "artifact wheels all targets not measured",
            "artifact managed runtime schema fit not measured",
        )
    )
    return suite.complete("footprint", report=report, evidence_lines=tuple(evidence))


def _configured_hosts(settings: ProviderSettings) -> set[str]:
    hosts = set()
    for value in (settings.llm_base_url, settings.embedding_base_url):
        hostname = (urlparse(value).hostname or "").lower().strip(".")
        if hostname and hostname not in _LOOPBACK_HOSTS:
            hosts.add(hostname)
    return hosts
