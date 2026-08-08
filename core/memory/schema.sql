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
            'memory_processing_failed', 'memory_clear_failed'
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
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_capture_queue (
    source_message_digest TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    provider_session_ref TEXT NOT NULL,
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
    occurred_at_ms INTEGER NOT NULL,
    provider_timestamp_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'processing', 'delivered', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_retry_at TEXT,
    lease_owner TEXT,
    lease_at TEXT,
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
    flush_observation TEXT CHECK (
        flush_observation IS NULL OR flush_observation IN (
            'not_attempted', 'in_flight', 'succeeded', 'rejected', 'unknown'
        )
    ),
    flush_status TEXT CHECK (
        flush_status IS NULL OR flush_status IN ('extracted', 'no_extraction')
    ),
    flush_error_code TEXT,
    flush_request_id TEXT,
    flush_observed_at TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (state IN ('pending', 'processing') AND payload_text IS NOT NULL)
        OR (state IN ('delivered', 'dead') AND payload_text IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_memory_capture_due
    ON memory_capture_queue (epoch, state, next_retry_at);

-- Version 2 coordination state for the later session flush coordinator.
CREATE TABLE IF NOT EXISTS memory_session_flush_state (
    provider_session_ref TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL CHECK (
        length(principal_id) = 34
        AND substr(principal_id, 1, 2) = 'u-'
        AND substr(principal_id, 3) NOT GLOB '*[^0-9a-f]*'
    ),
    epoch INTEGER NOT NULL CHECK (epoch >= 0),
    project_ref TEXT NOT NULL CHECK (
        length(project_ref) = 34
        AND substr(project_ref, 1, 2) = 'p-'
        AND substr(project_ref, 3) NOT GLOB '*[^0-9a-f]*'
    ),
    session_id TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    first_unflushed_at TEXT,
    last_add_ack_at TEXT,
    due_at TEXT,
    next_attempt_at TEXT,
    flush_state TEXT NOT NULL DEFAULT 'not_due' CHECK (
        flush_state IN ('not_due', 'due', 'in_flight', 'manual_required')
    ),
    watermark INTEGER NOT NULL DEFAULT 0 CHECK (watermark >= 0),
    fence_epoch INTEGER NOT NULL DEFAULT 0 CHECK (fence_epoch >= 0),
    fence_operation_id TEXT,
    fence_owner TEXT,
    fence_acquired_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (principal_id, epoch, project_ref, session_id)
);

CREATE INDEX IF NOT EXISTS ix_memory_session_flush_due
    ON memory_session_flush_state (flush_state, due_at, next_attempt_at);

-- Settlement history is append-only. A later coordinator can project the
-- latest generation without treating a mutable live row as historical proof.
CREATE TABLE IF NOT EXISTS memory_flush_settlements (
    settlement_id TEXT PRIMARY KEY,
    provider_session_ref TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    fence_epoch INTEGER NOT NULL CHECK (fence_epoch >= 0),
    operation_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('add', 'flush')),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('succeeded', 'rejected', 'unknown', 'manual_required')
    ),
    last_known_state TEXT,
    last_observed_outcome TEXT CHECK (
        last_observed_outcome IS NULL OR last_observed_outcome IN (
            'succeeded', 'rejected', 'unknown', 'manual_required', 'in_flight'
        )
    ),
    request_id TEXT,
    error_code TEXT,
    watermark_before INTEGER CHECK (watermark_before IS NULL OR watermark_before >= 0),
    watermark_after INTEGER CHECK (watermark_after IS NULL OR watermark_after >= 0),
    confirmed_watermark_ms INTEGER CHECK (
        confirmed_watermark_ms IS NULL OR confirmed_watermark_ms >= 0
    ),
    flush_state TEXT CHECK (
        flush_state IS NULL OR flush_state IN ('not_due', 'due', 'in_flight', 'manual_required')
    ),
    source TEXT CHECK (source IS NULL OR source IN ('add', 'flush')),
    observed_at TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    UNIQUE (provider_session_ref, generation, operation_id)
);

CREATE INDEX IF NOT EXISTS ix_memory_flush_settlements_session
    ON memory_flush_settlements (provider_session_ref, generation, observed_at);

PRAGMA user_version = 2;
