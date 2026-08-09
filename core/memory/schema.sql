-- Initial Memory schema for new databases.
CREATE TABLE IF NOT EXISTS memory_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    epoch INTEGER NOT NULL,
    clear_in_progress INTEGER NOT NULL DEFAULT 0 CHECK (clear_in_progress IN (0, 1)),
    scope_key BLOB NOT NULL,
    provider_root_id TEXT NOT NULL,
    last_provider_timestamp_ms INTEGER NOT NULL DEFAULT 0,
    missed_count INTEGER NOT NULL DEFAULT 0 CHECK (missed_count >= 0),
    last_success_at TEXT,
    last_error TEXT CHECK (
        last_error IS NULL OR last_error IN (
            'memory_disabled', 'memory_invalid_input', 'memory_input_too_large',
            'memory_queue_full', 'memory_low_disk_space', 'memory_store_unavailable',
            'memory_runtime_missing', 'memory_runtime_unsupported',
            'memory_runtime_install_failed', 'memory_sidecar_unavailable',
            'memory_provider_timeout', 'memory_provider_response_invalid',
            'memory_processing_failed', 'memory_clear_failed',
            'memory_capability_unavailable'
        )
    ),
    last_error_at TEXT,
    processing_fault_kind TEXT CHECK (
        processing_fault_kind IS NULL OR processing_fault_kind IN ('credential', 'engine')
    ),
    processing_fault_since TEXT,
    processing_alert_active INTEGER NOT NULL DEFAULT 0 CHECK (
        processing_alert_active IN (0, 1)
    ),
    processing_recovery_pending_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_attachment_bundle (
    bundle_id TEXT PRIMARY KEY CHECK (
        length(bundle_id) = 32
        AND bundle_id NOT GLOB '*[^0-9a-f]*'
    ),
    relative_path TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('pinned', 'releasing')),
    file_count INTEGER NOT NULL CHECK (file_count BETWEEN 1 AND 8),
    total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_session_flush_state (
    provider_session_ref TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL CHECK (epoch >= 0),
    open_generation INTEGER NOT NULL DEFAULT 1 CHECK (open_generation >= 1),
    target_generation INTEGER CHECK (target_generation IS NULL OR target_generation >= 1),
    state TEXT NOT NULL DEFAULT 'idle' CHECK (
        state IN ('idle', 'due', 'in_flight', 'manual_required')
    ),
    first_unflushed_at TEXT,
    last_add_ack_at TEXT,
    confirmed_add_watermark_ms INTEGER,
    unflushed_count INTEGER NOT NULL DEFAULT 0 CHECK (unflushed_count >= 0),
    due_at TEXT,
    next_attempt_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    operation_epoch INTEGER NOT NULL DEFAULT 0 CHECK (operation_epoch >= 0),
    fence_token TEXT,
    submission_started_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (
        (state = 'idle' AND target_generation IS NULL AND fence_token IS NULL
            AND submission_started_at IS NULL)
        OR
        (state IN ('due', 'in_flight', 'manual_required')
            AND target_generation IS NOT NULL AND fence_token IS NOT NULL)
    ),
    CHECK (state IN ('in_flight', 'manual_required') OR submission_started_at IS NULL)
);

CREATE TABLE IF NOT EXISTS memory_capture_queue (
    source_message_digest TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    provider_session_ref TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    principal_id TEXT NOT NULL CHECK (
        length(principal_id) = 34
        AND substr(principal_id, 1, 2) = 'u-'
        AND substr(principal_id, 3) NOT GLOB '*[^0-9a-f]*'
    ),
    project_ref TEXT NOT NULL CHECK (
        length(project_ref) = 34
        AND substr(project_ref, 1, 2) = 'p-'
        AND substr(project_ref, 3) NOT GLOB '*[^0-9a-f]*'
    ),
    provenance TEXT NOT NULL CHECK (provenance IN ('user_input', 'agent')),
    payload_text TEXT,
    payload_attachments TEXT,
    attachment_bundle_id TEXT REFERENCES memory_attachment_bundle(bundle_id) ON DELETE RESTRICT,
    occurred_at_ms INTEGER NOT NULL,
    provider_timestamp_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'processing', 'delivered', 'dead', 'manual_required')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_retry_at TEXT,
    lease_owner TEXT,
    lease_at TEXT,
    lease_token INTEGER NOT NULL DEFAULT 0 CHECK (lease_token >= 0),
    last_error TEXT CHECK (
        last_error IS NULL OR last_error IN (
            'memory_disabled', 'memory_invalid_input', 'memory_input_too_large',
            'memory_queue_full', 'memory_low_disk_space', 'memory_store_unavailable',
            'memory_runtime_missing', 'memory_runtime_unsupported',
            'memory_runtime_install_failed', 'memory_sidecar_unavailable',
            'memory_provider_timeout', 'memory_provider_response_invalid',
            'memory_processing_failed', 'memory_clear_failed'
        )
    ),
    add_request_id TEXT,
    add_status TEXT CHECK (add_status IS NULL OR add_status IN ('accumulated', 'extracted')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (state IN ('pending', 'processing', 'manual_required') AND payload_text IS NOT NULL)
        OR (state IN ('delivered', 'dead') AND payload_text IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_memory_capture_due
    ON memory_capture_queue (epoch, state, next_retry_at);

CREATE INDEX IF NOT EXISTS ix_memory_capture_session_generation
    ON memory_capture_queue (provider_session_ref, epoch, generation, state);

CREATE INDEX IF NOT EXISTS ix_memory_session_flush_due
    ON memory_session_flush_state (epoch, state, due_at, next_attempt_at);

CREATE TABLE IF NOT EXISTS memory_flush_settlements (
    settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_session_ref TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch >= 0),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('add', 'flush')),
    operation_token TEXT NOT NULL,
    observation TEXT NOT NULL CHECK (
        observation IN ('settled', 'rejected', 'manual_required')
    ),
    request_id TEXT,
    confirmed_watermark_ms INTEGER,
    observed_at TEXT NOT NULL,
    error_code TEXT,
    recovery_origin TEXT CHECK (recovery_origin IS NULL OR recovery_origin = 'boot'),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    CHECK (
        (operation_kind = 'add' AND observation = 'rejected' AND attempts >= 1)
        OR
        (NOT (operation_kind = 'add' AND observation = 'rejected') AND attempts = 0)
    ),
    UNIQUE (
        provider_session_ref, epoch, generation, operation_kind, operation_token
    )
);

CREATE INDEX IF NOT EXISTS ix_memory_flush_settlements_recent
    ON memory_flush_settlements (epoch, observed_at DESC, settlement_id DESC);

CREATE TRIGGER IF NOT EXISTS trg_memory_flush_settlements_immutable
BEFORE UPDATE ON memory_flush_settlements
BEGIN
    SELECT RAISE(ABORT, 'memory flush settlements are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_flush_settlements_no_delete
BEFORE DELETE ON memory_flush_settlements
WHEN COALESCE(
    (SELECT clear_in_progress FROM memory_meta WHERE singleton = 1),
    0
) != 1
BEGIN
    SELECT RAISE(ABORT, 'memory flush settlements are immutable');
END;
