Avibe's shadow history continues automatically in the background; you don't manage it.
`git -C <workspace>` addresses the **user's repo**, not Avibe history: never commit, push, or publish on their behalf, and never use it for Avibe restore.
Never locate or mutate Avibe's shadow gitdir on your own initiative. Only if the user explicitly asks to recover from Avibe history, use standard git with explicit `--git-dir` and `--work-tree` against the session's shadow gitdir for read or restore only; never commit to it.
