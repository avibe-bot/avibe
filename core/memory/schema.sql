-- Identity-only Memory schema for new databases and v4 migration targets.
CREATE TABLE IF NOT EXISTS memory_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    epoch INTEGER NOT NULL,
    clear_in_progress INTEGER NOT NULL DEFAULT 0 CHECK (clear_in_progress IN (0, 1)),
    scope_key BLOB NOT NULL,
    provider_root_id TEXT NOT NULL,
    last_provider_timestamp_ms INTEGER NOT NULL DEFAULT 0,
    missed_count INTEGER NOT NULL DEFAULT 0 CHECK (missed_count >= 0),
    last_success_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    processing_fault_generation INTEGER NOT NULL DEFAULT 0 CHECK (processing_fault_generation >= 0),
    processing_fault_kind TEXT CHECK (
        processing_fault_kind IS NULL OR processing_fault_kind IN ('credential', 'engine')
    ),
    processing_fault_since TEXT,
    processing_alert_active INTEGER NOT NULL DEFAULT 0 CHECK (processing_alert_active IN (0, 1)),
    processing_recovery_pending_at TEXT,
    processing_recovery_generation INTEGER CHECK (
        processing_recovery_generation IS NULL OR processing_recovery_generation >= 0
    ),
    updated_at TEXT NOT NULL,
    CHECK (
        (processing_recovery_pending_at IS NULL AND processing_recovery_generation IS NULL)
        OR
        (processing_recovery_pending_at IS NOT NULL AND processing_recovery_generation IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS memory_projects (
    principal_id TEXT NOT NULL CHECK (
        length(principal_id) = 34
        AND substr(principal_id, 1, 2) = 'u-'
        AND substr(principal_id, 3) NOT GLOB '*[^0-9a-f]*'
    ),
    project_id TEXT NOT NULL CHECK (
        project_id = 'default'
        OR (
            length(project_id) = 34
            AND substr(project_id, 1, 2) = 'p-'
            AND substr(project_id, 3) NOT GLOB '*[^0-9a-f]*'
        )
        OR (
            length(project_id) BETWEEN 1 AND 63
            AND substr(project_id, 1, 1) GLOB '[a-z]'
            AND project_id NOT GLOB '*[^a-z0-9_-]*'
            AND project_id NOT IN ('all', 'personal', 'default')
            AND substr(project_id, 1, 2) NOT IN ('p-', 'u-')
        )
    ),
    created_at TEXT NOT NULL,
    last_written_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, project_id)
);
