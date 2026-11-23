"""Run-history read budget: real assembly, SQLite plans, ordered rc15 goldens.

This is a correctness fixture, not a latency or physical-byte benchmark.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event

from core.services import agent_graph
from storage import migrations
from storage.db import create_sqlite_engine
from tests.test_agent_graph_service import (
    NOW, _edge, _insert_definition, _insert_run, _insert_session, _nodes_by_id,
    isolated_state,  # noqa: F401 -- pytest fixture
)


def seed(c, tail):
    for sid in ('hot', 'parent', 'ended', 'empty'):
        _insert_session(c, sid, scope_id=None)
    _insert_definition(c, 'task', cron='0 * * * *')
    _insert_run(c, 'different-mode', session_id='ended')
    for i in range(tail):
        _insert_run(c, f'old-{i}', session_id='hot', created=NOW-timedelta(days=7, minutes=i))
    for i in range(6):
        _insert_run(c, f'old-edge-{i}', session_id='hot', created=NOW-timedelta(days=3, minutes=i),
                    source_kind='agent', source_actor='parent', callback_session_id='parent', callback_status='sent')
    for i in range(16):
        _insert_run(c, f'recent-{i:02}', session_id='hot', created=NOW-timedelta(seconds=i))
    # Neither real offset trigger is in the latest 10. One sorts before the
    # literal window floor, one after it; both count. Older task does not.
    for rid, raw in [('trig-in','2026-07-22T01:01:00-02:00'), ('trig-2','2026-07-22T04:00:00Z'),
                     ('trig-out','2026-07-22T03:59:00+02:00')]:
        _insert_run(c, rid, session_id='hot', run_type='watch', definition_id='task')
        c.exec_driver_sql('UPDATE agent_runs SET created_at=? WHERE id=?', (raw, rid))
    # Raw Z maximum is NOT the newest instant; offset crosses the timeline
    # boundary, and parsed ties must use descending ID (not insertion order).
    for rid, raw in [('raw-max','2026-07-24T00:00:00+23:00'), ('precise','2026-07-23T02:00:00.9+00:00'),
                     ('offset','2026-07-22T22:59:59-03:00'), ('tie','2026-07-23T01:59:59Z')]:
        _insert_run(c, rid, session_id='hot')
        c.exec_driver_sql('UPDATE agent_runs SET created_at=? WHERE id=?', (raw, rid))
    for rid, status in [('z-sent','sent'), ('a-unsent','skipped')]:
        _insert_run(c, rid, session_id='hot', created=NOW-timedelta(days=2), callback_session_id='parent',
                    callback_status=status, callback_run_id='winning-delivery')
    for i in range(4):
        _insert_run(c, f'no-notice-{i}', session_id='hot', created=NOW-timedelta(days=1, minutes=i),
                    callback_session_id='parent', callback_status='skipped', metadata={'callback_mode':'failure'})
    # A trailing field can cross overflow pages even when the projection does
    # not name stdout. Do not open tied but older malformed callback metadata.
    c.exec_driver_sql("UPDATE agent_runs SET stdout=zeroblob(24000) WHERE session_id='hot'")
    c.exec_driver_sql("UPDATE agent_runs SET metadata_json='invalid' WHERE id='a-unsent'")


LIVE = [{'session_id': s, 'state': 'active'} for s in ('hot', 'parent', 'empty')]


@pytest.mark.parametrize('history', [False, True])
@pytest.mark.parametrize('tail', [8, 40])
def test_exact_order_and_long_tail_read_budget(isolated_state, history, tail):  # noqa: F811
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            seed(conn, tail)
        spans = agent_graph.HistoryStatementSpans()
        graph = agent_graph.build_graph(now=NOW, live_agents=LIVE, engine=engine, include_ended=history, history_spans=spans)
        hot = _nodes_by_id(graph)['hot']
        assert hot['run_counts']['total'] == tail + 35
        assert [r['id'] for r in hot['runs']][:5] == ['precise', 'recent-00', 'tie', 'recent-01', 'offset']
        assert _edge(graph, 'spawn', 'parent', 'hot')['run_count'] == 6
        assert _edge(graph, 'callback', 'hot', 'parent')['last_run_id'] == 'winning-delivery'
        assert _edge(graph, 'trigger', 'def:task', 'hot')['run_count'] == 2
        # Golden from immutable rc15 graph assembly, no sorting of keys/arrays.
        digest = hashlib.sha256(json.dumps(graph, separators=(',', ':')).encode()).hexdigest()
        assert digest == GOLDENS[tail, history]
        fields = spans.log_fields()
        rows = {name: f['rows'] for name, f in fields.items()}
        assert rows['run_headers'] == 14 + history  # latest 10 + raw max + three edge winners
        assert rows['time_candidates'] < 35 + history  # independent of unrelated tail length
        assert rows['trigger_runs'] == 2 and rows['trigger_exceptions'] == 1
        assert rows['callback_tie_ids'] == 2 and rows['callback_modes'] == 4 and rows['callback_delivery_ids'] == 1
        assert rows['timeline_timestamps_covered'] == 10 + history
        assert rows['timeline_bounds'] == 1
    finally:
        engine.dispose()


def test_boundary_ties_and_invalid_order_witness(isolated_state):  # noqa: F811
    engine = create_sqlite_engine()
    try:
        with engine.begin() as c:
            _insert_session(c, 's', scope_id=None)
            for i in range(15):
                _insert_run(c, f't{i:02}', session_id='s')
                raw = ['2026-07-23T01:00:00Z', '2026-07-22T23:00:00-02:00',
                       '2026-07-23T01:00:00.000000+00:00'][i % 3]
                c.exec_driver_sql('UPDATE agent_runs SET created_at=? WHERE id=?', (raw, f't{i:02}'))
            _insert_run(c, 'raw-maximum', session_id='s')
            c.exec_driver_sql("UPDATE agent_runs SET created_at='not-a-date' WHERE id='raw-maximum'")
        spans = agent_graph.HistoryStatementSpans()
        graph = agent_graph.build_graph(now=NOW, engine=engine, history_spans=spans)
        assert [r['id'] for r in graph['nodes'][0]['runs']] == [f't{i:02}' for i in range(14, 4, -1)]
        assert spans.log_fields()['time_candidates']['rows'] == 16  # No unsafe lexical LIMIT.
    finally:
        engine.dispose()


GOLDENS = {
    (8, False): '9c16cc6c9fc021069bc5cd3291e2b042fe3a81be074a627e0752497ceaee02f1',
    (8, True): '491834cb6e6c1514c6d1e6ad94d33623f75a7e4031ac974c170d61774fb1b7d2',
    (40, False): '1eb19deaaa88019734163408ac8d65d4550457412873f1eac94f51e32713645d',
    (40, True): '32424edfd8aa90d06c898c5567733d650573c18a2de5ac0507b743e11d9de1bc',
}


@pytest.mark.parametrize('history', [False, True])
@pytest.mark.parametrize(('index', 'keys', 'normal', 'fallback'), [
    ('ix_agent_runs_session_created', 'session_id,created_at', 'trigger_runs', 'trigger_runs_fallback'),
    ('ix_agent_runs_timeline_times', 'id,started_at,completed_at', 'timeline_timestamps_covered', 'timeline_timestamps_fallback'),
])
@pytest.mark.parametrize('layout', ['absent', 'partial', 'wrong_order', 'extra', 'collation', 'descending', 'expression'])
def test_compatible_optional_index_layout(isolated_state, history, index, keys, normal, fallback, layout):  # noqa: F811
    # A compatible future snapshot can omit or replace either performance
    # index. Test the actual admission gate and both complete ordered graphs.
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            seed(conn, 8)
            conn.exec_driver_sql(f'DROP INDEX {index}')
            replacements = {
                'partial': f"({keys}) WHERE status='running'",
                'wrong_order': '(' + ','.join(reversed(keys.split(','))) + ')',
                'extra': f'({keys},status)', 'collation': f'({keys} COLLATE NOCASE)',
                'descending': f'({keys} DESC)', 'expression': f'({keys},length(status))',
            }
            if layout != 'absent':
                conn.exec_driver_sql(f'CREATE INDEX {index} ON agent_runs {replacements[layout]}')
            conn.exec_driver_sql("UPDATE alembic_version SET version_num='20990101_9999'")
        assert migrations.preflight_database(Path(engine.url.database)).compatible_open
        spans = agent_graph.HistoryStatementSpans()
        graph = agent_graph.build_graph(now=NOW, live_agents=LIVE, engine=engine, include_ended=history, history_spans=spans)
        fields = spans.log_fields()
        assert fields['history_index_probe']['statements'] == 1
        other_columns = 3 if normal == 'trigger_runs' else 2
        declared = len(keys.split(','))
        observed = 0 if layout in {'absent', 'partial'} else declared + (layout in {'extra', 'expression'})
        assert fields['history_index_probe']['rows'] == other_columns + observed
        assert normal not in fields and fields[fallback]['rows'] == (2 if normal == 'trigger_runs' else 10 + history)
        assert hashlib.sha256(json.dumps(graph, separators=(',', ':')).encode()).hexdigest() == GOLDENS[8, history]
    finally:
        engine.dispose()


@pytest.mark.parametrize('hit', [False, True])
def test_read_budget_uses_actual_index_ranges(isolated_state, hit):  # noqa: F811
    engine = create_sqlite_engine()
    sessions = {'hot', 'parent', *(f'busy-{i}' for i in range(6))}
    try:
        with engine.begin() as conn:
            seed(conn, 8)
            # SQLite can prefer the global run-type index with a multi-ID IN
            # even though the single-ID plan uses the desired time range.
            for sid in sorted(sessions - {'hot', 'parent'}):
                for i in range(11):
                    _insert_run(conn, f'{sid}-{i}', session_id=sid)
            if not hit:
                conn.exec_driver_sql("UPDATE agent_runs SET source_actor='invisible', callback_session_id='invisible', "
                                     "run_type='agent', definition_id=NULL WHERE session_id='hot'")
        with engine.connect() as conn:
            plans = {}
            current = None

            @event.listens_for(conn, 'before_cursor_execute')
            def observe(_conn, cursor, statement, params, *_):
                cursor.execute('EXPLAIN QUERY PLAN ' + statement, params)
                plans.setdefault(current, []).extend(r[-1] for r in cursor.fetchall())

            class Reader(agent_graph._GraphReader):
                def rows(self, stmt, *, mappings=False, name=None):
                    nonlocal current
                    current = name
                    yield from super().rows(stmt, mappings=mappings, name=name)

            reader = Reader(conn, {'execute': 0., 'fetch': 0.})
            totals = agent_graph._run_totals(reader, sessions)
            agent_graph._load_runs(reader, sessions, sessions, NOW-timedelta(days=1), totals)
            expected = {
                'run_totals': 'COVERING INDEX ix_agent_runs_session_status (session_id=?)',
                'timeline_bounds': 'COVERING INDEX ix_agent_runs_session_created (session_id=?)',
                'time_candidates': 'COVERING INDEX ix_agent_runs_session_created (session_id=?)',
                'trigger_runs': 'INDEX ix_agent_runs_session_created (session_id=? AND created_at>?)',
                'trigger_exceptions': 'INTEGER PRIMARY KEY (rowid=?)',
                'spawn_endpoints': 'COVERING INDEX ix_agent_runs_source_session (source_kind=? AND session_id=? AND source_actor=?)',
                'callback_endpoints': 'COVERING INDEX ix_agent_runs_pair_callback (session_id=? AND callback_session_id=?',
                'run_headers': 'INTEGER PRIMARY KEY (rowid=?)',
                'timeline_timestamps_covered': 'COVERING INDEX ix_agent_runs_timeline_times (id=?)',
            }
            if hit:
                expected.update(dict.fromkeys(('callback_tie_ids', 'callback_modes', 'callback_delivery_ids'),
                                              'INTEGER PRIMARY KEY (rowid=?)'))
            assert all('agent_runs USING' not in p for p in plans['history_index_probe'])
            assert ('callback_modes' in plans) == hit
            # The all-short wave uses a different timestamp shape.
            agent_graph._load_runs(reader, {'parent'}, sessions, NOW-timedelta(days=1), totals)
            assert any('COVERING INDEX ix_agent_runs_session_created (session_id=?)' in p
                       for p in plans['all_timeline_times'])
            for name, shape in expected.items():
                assert any(shape in p for p in plans[name]), (name, plans[name])
                assert not any('SCAN agent_runs' in p or 'TEMP B-TREE FOR ORDER BY' in p for p in plans[name])
    finally:
        engine.dispose()
