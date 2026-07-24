import type { WorkbenchSession } from '../context/ApiContext';

const compareDesc = (left: string | null, right: string | null): number =>
  (right ?? '').localeCompare(left ?? '');

export const compareProjectSessions = (left: WorkbenchSession, right: WorkbenchSession): number => {
  if (left.pinned !== right.pinned) return left.pinned ? -1 : 1;
  return (
    compareDesc(left.last_active_at, right.last_active_at) ||
    compareDesc(left.created_at, right.created_at) ||
    compareDesc(left.id, right.id)
  );
};

export const orderProjectSessions = (sessions: WorkbenchSession[]): WorkbenchSession[] =>
  [...sessions].sort(compareProjectSessions);
