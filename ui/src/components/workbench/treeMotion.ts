// The project tree as its motion follows it (docs/plans/web-ui-motion.md M7), for
// the desktop sidebar and the phone's Projects page alike: each project's header,
// then the session rows shown under it, then its Load more. A session a turn moves
// to the top of its project moves and the rows around it glide; a session created
// or archived comes in or leaves. Opening, closing or reordering a project, or its
// first page of sessions arriving, is a different tree (`group`): those cut, and a
// drag animates itself.

type Described = {
  open: boolean;
  /** The session rows drawn under it; null while its first page is loading. */
  rows: readonly { id: string }[] | null;
  /** Whether its Load more button is drawn. */
  more: boolean;
};

export function treeMotion(
  projects: readonly { id: string }[] | null,
  describe: (projectId: string) => Described,
): { ids: string[]; group: string } {
  const ids: string[] = [];
  const group: string[] = [];
  for (const project of projects ?? []) {
    const { open, rows, more } = describe(project.id);
    ids.push(`project:${project.id}`);
    group.push(`${project.id}:${!open ? 'closed' : rows === null ? 'loading' : 'open'}`);
    if (!open || rows === null) continue;
    for (const row of rows) ids.push(row.id);
    if (more) ids.push(`more:${project.id}`);
  }
  return { ids, group: group.join('|') };
}
