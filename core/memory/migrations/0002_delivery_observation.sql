ALTER TABLE memory_capture_queue ADD COLUMN add_request_id TEXT;
ALTER TABLE memory_capture_queue ADD COLUMN flush_observation TEXT CHECK (
    flush_observation IS NULL OR flush_observation IN (
        'not_attempted', 'in_flight', 'succeeded', 'rejected', 'unknown'
    )
);
ALTER TABLE memory_capture_queue ADD COLUMN flush_status TEXT CHECK (
    flush_status IS NULL OR flush_status IN ('extracted', 'no_extraction')
);
ALTER TABLE memory_capture_queue ADD COLUMN flush_error_code TEXT;
ALTER TABLE memory_capture_queue ADD COLUMN flush_request_id TEXT;
ALTER TABLE memory_capture_queue ADD COLUMN flush_observed_at TEXT;

ALTER TABLE memory_meta ADD COLUMN processing_fault_kind TEXT CHECK (
    processing_fault_kind IS NULL OR processing_fault_kind IN ('credential', 'engine')
);
ALTER TABLE memory_meta ADD COLUMN processing_fault_since TEXT;
ALTER TABLE memory_meta ADD COLUMN processing_alert_active INTEGER NOT NULL DEFAULT 0 CHECK (
    processing_alert_active IN (0, 1)
);
