import { describe, expect, it } from 'vitest';

import type { WorkbenchSession } from '../context/ApiContext';
import { orderProjectSessions } from './sessionPinning';

const session = (id: string, pinned: boolean, lastActive: string): WorkbenchSession => ({
  id,
  scope_id: 'avibe::project::proj_test',
  project_id: 'proj_test',
  title: id,
  agent_id: null,
  agent_name: null,
  agent_backend: null,
  agent_variant: null,
  model: null,
  reasoning_effort: null,
  status: 'active',
  agent_status: 'idle',
  workdir: '/tmp/project',
  native_session_id: null,
  pinned,
  created_at: lastActive,
  updated_at: lastActive,
  last_active_at: lastActive,
  metadata: {},
});

describe('orderProjectSessions', () => {
  it('keeps every pinned session above newer unpinned sessions', () => {
    const rows = orderProjectSessions([
      session('new-unpinned', false, '2026-07-24T03:00:00Z'),
      session('old-pinned', true, '2026-07-24T01:00:00Z'),
      session('new-pinned', true, '2026-07-24T02:00:00Z'),
    ]);

    expect(rows.map((row) => row.id)).toEqual(['new-pinned', 'old-pinned', 'new-unpinned']);
  });
});
