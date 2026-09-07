import type { WorkbenchProject } from '../context/ApiContext';

/** Operation defaults follow recency, independently of the navigation order. */
export function sortProjectsByRecent(projects: readonly WorkbenchProject[]): WorkbenchProject[] {
  return [...projects].sort((a, b) =>
    (b.last_active_at || b.created_at).localeCompare(a.last_active_at || a.created_at) || a.id.localeCompare(b.id),
  );
}

/** Apply only order, preserving current row data and appending new projects. */
export function orderProjects<T extends { id: string }>(projects: T[], order: readonly string[]): T[] {
  const positions = new Map(order.map((id, index) => [id, index]));
  return [...projects].sort((a, b) => (positions.get(a.id) ?? order.length) - (positions.get(b.id) ?? order.length));
}
