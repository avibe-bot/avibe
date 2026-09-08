import { describe, expect, it } from 'vitest';

import type { WorkbenchProject } from '../context/ApiContext';
import { orderProjects, sortProjectsByRecent } from './projectOrder';

const project = (id: string, lastActiveAt: string | null): WorkbenchProject => ({
  id,
  scope_id: `avibe::project::${id}`,
  display_name: id,
  folder_path: `/tmp/${id}`,
  created_at: '2026-01-01T00:00:00Z',
  last_active_at: lastActiveAt,
  archived: false,
  capabilities: { can_chat: true, has_folder: true },
});

describe('project ordering policies', () => {
  it('selects the same recent defaults regardless of navigation positions, including ties', () => {
    const rows = [project('older', null), project('recent-b', '2026-02-01T00:00:00Z'), project('recent-a', '2026-02-01T00:00:00Z')];
    const expected = [rows[2], rows[1], rows[0]];
    for (const ids of [['older', 'recent-b', 'recent-a'], ['recent-a', 'older', 'recent-b'], ['recent-b', 'recent-a', 'older']]) {
      const navigation = orderProjects(rows, ids);
      expect(sortProjectsByRecent(navigation)).toEqual(expected);
      expect(navigation.map((row) => row.id)).toEqual(ids);
    }
    expect(sortProjectsByRecent([])).toEqual([]);
  });
});
