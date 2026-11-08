"""Connection-local SQL diagnostics for canonical calls and physical legacy traces.

The SQL supplies canonical facts to the same pure projection as Activity. It does
not create persisted objects, register media, or bypass the query authorizer:
reading this view still reads ``agent_events`` and requires its existing access.
"""
from __future__ import annotations

import json

from core.tool_activity import capability, linked_step, object_value, project_response
from storage.agent_transcript import RESPONSE_TYPES


# Both bounds seek the session/context-sequence index. A result belongs to the
# response interval, never merely to a provider id reused by a later response.
_RESPONSE_TYPES_SQL = ", ".join(f"'{kind}'" for kind in RESPONSE_TYPES)
_RESULT_INPUT = f"""
    SELECT json_group_array(json_object(
        'id', r.id, 'context_seq', r.context_seq,
        'content_json', r.content_json, 'metadata_json', r.metadata_json, 'created_at', r.created_at))
    FROM (SELECT e.id, e.context_seq, e.content_json, e.metadata_json, e.created_at
          FROM agent_events e
          WHERE e.session_id = m.session_id AND e.context_seq > m.context_seq
            AND e.context_seq < coalesce((
                SELECT n.context_seq FROM messages n
                WHERE n.session_id = m.session_id AND n.context_seq > m.context_seq
                  AND n.author = 'agent' AND n.type IN ({_RESPONSE_TYPES_SQL})
                ORDER BY n.context_seq LIMIT 1), 9223372036854775807)
            AND e.event_type = 'tool_result' AND e.visibility = 'context'
          ORDER BY e.context_seq) r
"""
TOOL_CALLS_VIEW = f"""
SELECT m.session_id, 'canonical' AS source, m.id AS source_id,
       m.id AS response_id, json_extract(p.value, '$.call_id') AS call_id,
       m.context_seq, json_extract(p.value, '$.id') AS id,
       json_extract(p.value, '$.order_micros') AS order_micros,
       json_extract(p.value, '$.order_index') AS order_index,
       json_extract(p.value, '$.name') AS name,
       json_extract(p.value, '$.arguments') AS arguments_json,
       json_extract(p.value, '$.status') AS status,
       json_extract(p.value, '$.started_at') AS started_at,
       json_extract(p.value, '$.ended_at') AS ended_at,
       json_extract(p.value, '$.duration_ms') AS duration_ms,
       json_extract(p.value, '$.turn_id') AS turn_id,
       json_extract(p.value, '$.text') AS text
FROM messages m
LEFT JOIN session_turns t ON t.id = json_extract(m.metadata_json, '$.turn_id') AND t.session_id = m.session_id,
json_each(tool_response_steps(json_object(
    'id', m.id, 'session_id', m.session_id, 'context_seq', m.context_seq,
    'platform', m.platform, 'created_at', m.created_at,
    'content_json', m.content_json, 'metadata_json', m.metadata_json),
    ({_RESULT_INPUT}), json_object('state', t.state))) p
WHERE m.context_seq IS NOT NULL AND tool_projection_capable(m.metadata_json)
UNION ALL
SELECT e.session_id, 'trace', e.id, NULL, NULL, NULL, e.id, NULL, NULL,
       json_extract(e.content_json, '$.hosted.tool'), NULL,
       coalesce(json_extract(e.content_json, '$.hosted.status'), 'unknown'),
       NULL, NULL, json_extract(e.content_json, '$.hosted.duration_ms'), e.turn_id,
       coalesce(e.content_text, json_extract(e.content_json, '$.text'))
FROM agent_events e
WHERE e.event_type = 'tool_call' AND e.visibility = 'trace'
  AND NOT EXISTS (
      SELECT 1 FROM messages m
      WHERE m.id = json_extract(e.metadata_json, '$.tool_call_source.response_id')
        AND m.session_id = e.session_id
        AND tool_trace_owned(m.id, m.content_json, m.metadata_json, m.context_seq, e.metadata_json))
"""


def sql_response_steps(row, results, turn):
    return json.dumps(project_response(object_value(row), json.loads(results or "[]"), object_value(turn)), ensure_ascii=False)


def sql_trace_owned(response_id, content, metadata, context_seq, trace_metadata):
    linked = linked_step(trace_metadata)
    if linked is None:
        return False
    return any(event["id"] == linked for event in project_response({
        "id": response_id, "content_json": content, "metadata_json": metadata, "context_seq": context_seq,
    }, []))


TOOL_CALL_FUNCTIONS = (
    ("tool_projection_capable", 1, lambda value: capability(value) is not None),
    ("tool_response_steps", 3, sql_response_steps),
    ("tool_trace_owned", 5, sql_trace_owned),
)
