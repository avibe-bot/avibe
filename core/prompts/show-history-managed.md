History is saved automatically around each turn; do not manage versions yourself.
Read freely: `git -C <workspace> status / log / diff / show`.
Restore only via `git restore --source=<ref> -- <path>`; the turn-end checkpoint records it as a forward commit.
Never move HEAD, switch branches, rewrite history, or run gc; if you do, the platform self-heals with the worktree as truth.
Never add remotes, push, or publish the workspace anywhere unless the user explicitly asks.
