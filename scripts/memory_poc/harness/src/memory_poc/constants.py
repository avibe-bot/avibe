from __future__ import annotations

EVEROS_VERSION = "1.1.3"
APP_ID = "avibe"
PROJECT_ID = "personal"

STAGES = ("sanity", "quality", "pool", "duplicate", "retention", "footprint")

CRITERIA_IDS = (
    "temporal_all",
    "negatives_all",
    "positive_top8_rate",
    "query_p95_s",
    "searchable_p95_min",
    "env_size_gib",
    "wheels_all_targets",
    "mrm_schema_fit",
    "idle_rss_p95_mib",
    "peak_rss_mib",
    "root_growth_mib",
    "egress_configured_only",
    "loopback_no_egress",
    "launcher_uds_only",
    "restart_preserves",
    "clear_removes_all",
    "no_internals_needed",
)

REQUIRED_PROVIDER_ENV_KEYS = (
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "EMBEDDING_API_KEY",
)

PROXY_AND_TLS_ENV_KEYS = (
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
