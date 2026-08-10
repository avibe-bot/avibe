"""Durably authorized storage cleanup for Memory clear snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from core.memory.clear_journal import (
    ClearOperation,
    ClearOperationCASMismatch,
    ClearTransitionError,
    MemoryClearJournal,
    _utc_now,
    _validated_digest,
    _validated_operation_id,
    _validated_relative_path,
)
from core.memory.snapshot import MemorySnapshotManager


@dataclass(frozen=True, slots=True)
class _TerminalSnapshot:
    operation_id: str
    relative_path: str
    manifest_sha256: str
    surface_digests: dict[str, str | None]


class MemoryClearSnapshotStorage:
    """Own journal-authorized cleanup of clear snapshots."""

    def __init__(
        self,
        journal: MemoryClearJournal,
        snapshot_manager: MemorySnapshotManager,
    ) -> None:
        self._journal = journal
        self._snapshot_manager = snapshot_manager

    def discard_unrecorded_preparing_snapshot(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
    ) -> ClearOperation:
        """Discard one exact unrecorded preparing snapshot and journal the result."""

        identifier = _validated_operation_id(operation_id)
        connection = self._journal._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._journal._cas_row(
                connection,
                identifier,
                expected_revision,
                execution_token,
                allowed_states=("preparing",),
            )
            nonpending = connection.execute(
                """
                SELECT COUNT(*) FROM clear_surface
                WHERE operation_id = ? AND state != 'pending'
                """,
                (identifier,),
            ).fetchone()[0]
            if (
                nonpending
                or row["snapshot_path"] is not None
                or row["manifest_sha256"] is not None
                or row["destructive_started"]
            ):
                raise ClearTransitionError(
                    "only an unrecorded preparing snapshot may be discarded"
                )

            relative_path = f"state/memory/clear-snapshots/{identifier}"
            self._snapshot_manager._discard_unrecorded_clear_snapshot(
                identifier,
                expected_relative_path=relative_path,
            )

            now = _utc_now()
            revision = row["revision"] + 1
            updated = connection.execute(
                """
                UPDATE clear_operation
                SET updated_at = ?, revision = ?
                WHERE operation_id = ? AND state = 'preparing'
                    AND revision = ? AND execution_token = ?
                """,
                (
                    now,
                    revision,
                    identifier,
                    row["revision"],
                    row["execution_token"],
                ),
            )
            if updated.rowcount != 1:
                raise ClearOperationCASMismatch(
                    "Memory clear discard claim is stale"
                )
            self._journal._append_event(
                connection,
                identifier,
                "snapshot_discarded",
                row["operator_ref"],
                occurred_at=now,
                resulting_revision=revision,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._journal._harden_database_files()
        return self._journal._require_operation(identifier)

    def eligible_terminal_snapshot_ids(self) -> tuple[str, ...]:
        """Return durable terminal snapshot cleanup work in completion order."""

        connection = self._journal._connect()
        try:
            rows = connection.execute(
                """
                SELECT operation_id FROM clear_operation
                WHERE state IN ('completed', 'aborted')
                ORDER BY terminal_at, operation_id
                """
            ).fetchall()
        finally:
            connection.close()
        identifiers = tuple(str(row["operation_id"]) for row in rows)
        for identifier in identifiers:
            self._terminal_snapshot(identifier)
        return identifiers

    def remove_terminal_snapshot(self, operation_id: str) -> None:
        """Remove one snapshot only when its durable clear audit is terminal."""

        snapshot = self._terminal_snapshot(operation_id)
        self._snapshot_manager._remove_clear_snapshot(
            snapshot.operation_id,
            expected_relative_path=snapshot.relative_path,
            expected_manifest_sha256=snapshot.manifest_sha256,
            expected_surface_digests=snapshot.surface_digests,
        )

    def _terminal_snapshot(self, operation_id: str) -> _TerminalSnapshot:
        operation = self._journal._require_operation(
            _validated_operation_id(operation_id)
        )
        required_surface_state = {
            "completed": "deleted",
            "aborted": "restored",
        }.get(operation.state)
        if required_surface_state is None:
            raise ClearTransitionError(
                "only a terminal Memory clear may remove its snapshot"
            )
        if operation.snapshot_path is None or operation.manifest_sha256 is None:
            raise ClearTransitionError(
                "terminal Memory clear snapshot metadata is missing"
            )
        relative_path = _validated_relative_path(operation.snapshot_path)
        manifest_sha256 = _validated_digest(operation.manifest_sha256)
        surfaces = self._journal.get_surfaces(operation.operation_id)
        if len(surfaces) != len(self._journal.surfaces) or any(
            surface.state != required_surface_state or surface.present is None
            for surface in surfaces
        ):
            raise ClearTransitionError(
                "terminal Memory clear surface audit is incomplete"
            )
        surface_digests: dict[str, str | None] = {}
        for surface in surfaces:
            relative_surface = _validated_relative_path(surface.relative_path)
            if relative_surface in surface_digests:
                raise ValueError("terminal Memory snapshot has duplicate surfaces")
            surface_digests[relative_surface] = (
                None
                if surface.snapshot_digest is None
                else _validated_digest(surface.snapshot_digest)
            )
        return _TerminalSnapshot(
            operation_id=operation.operation_id,
            relative_path=relative_path,
            manifest_sha256=manifest_sha256,
            surface_digests=surface_digests,
        )
