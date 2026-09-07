/** Apply only order, preserving current row data and appending new projects. */
export function orderProjects<T extends { id: string }>(projects: T[], order: readonly string[]): T[] {
  const positions = new Map(order.map((id, index) => [id, index]));
  return [...projects].sort((a, b) => (positions.get(a.id) ?? order.length) - (positions.get(b.id) ?? order.length));
}
