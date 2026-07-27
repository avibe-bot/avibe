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
    principal_id TEXT NOT NULL CHECK (
        length(principal_id) = 34
        AND substr(principal_id, 1, 2) = 'u-'
        AND substr(principal_id, 3) NOT GLOB '*[^0-9a-f]*'
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
