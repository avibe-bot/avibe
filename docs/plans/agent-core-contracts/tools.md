# C-7 Tools

The coding tools in `core/agent_core/tools/`. Names, parameters, logic, and model-facing text follow Pi
(`packages/coding-agent/src/core/tools/` at `7fbbd5f`, MIT, Copyright (c) 2025 Mario Zechner); strings quoted below
are Pi's unless marked as an Avibe addition. tau 0.4.7 (MIT, Copyright (c) 2026 Alejandro AO) is the Python
reference. One tool set serves every model.

On Windows the command tool is `powershell` instead of `bash` (§5); what this contract and the others (C-3, C-9, C-10)
say of `bash` holds for it.

`Tool.concurrent` (Avibe) says whether a tool's calls may run at the same time as the neighbouring concurrent calls of
the same response (C-3 [`loop-control.md`](loop-control.md) §2). When it is absent or `False`, every call of the tool
is exclusive. `read` and `bash` are concurrent; `write` and `edit` are exclusive. Pi instead runs a response's calls at
the same time unless a tool opts out, and serializes `write` and `edit` only per file path, which `bash` bypasses.

## 1. Constants

| Name | Value |
| --- | --- |
| `MAX_LINES` | 2,000 |
| `MAX_BYTES` | 51,200 (50 KB) |
| Foreground window for `bash` before handover to Watch | 120 s, configurable (Avibe) |

## 2. `read`

Description: `Read the contents of a file. Supports text files and images (jpg, png, gif, webp). Images are sent
as attachments. For text files, output is truncated to 2000 lines or 50KB (whichever is hit first). Use offset/limit
for large files. When you need the full file, continue with offset until complete.`

```json
{
  "type": "object",
  "required": ["path"],
  "properties": {
    "path": { "type": "string", "description": "Path to the file to read (relative or absolute)" },
    "offset": { "type": "integer", "minimum": 1, "description": "Line number to start reading from (1-indexed)" },
    "limit": { "type": "integer", "minimum": 1, "description": "Maximum number of lines to read" }
  }
}
```

Raw text, no line numbers, head kept. Model-facing suffixes:

- `[Showing lines {start}-{end} of {total}. Use offset={next} to continue.]`
- `[Showing lines {start}-{end} of {total} (50KB limit). Use offset={next} to continue.]`
- `[{remaining} more lines in file. Use offset={next} to continue.]` (an explicit `limit` stopped before the end)
- `[Line {n} is {size}, exceeds 50KB limit. Use bash: sed -n '{n}p' {quoted_path} | head -c 51200]`, where
  `{quoted_path}` is the path shell-quoted (Avibe change to Pi's text, so the suggested command is always safe to run).
  On Windows: `[Line {n} is {size}, exceeds 50KB limit. Use powershell: {command}]`, where `{command}` prints what
  `sed` and `head -c` would: line `{n}` read as UTF-8, at most its first 51200 bytes, cut at a character boundary,
  with no newline added, streaming the lines before it. The path is in single quotes with each `'` doubled:
  `Get-Content -LiteralPath {quoted_path} -TotalCount {n} -Encoding UTF8 | Select-Object -Last 1 | ForEach-Object {
  $b = [Text.Encoding]::UTF8.GetBytes($_); $n = [Math]::Min($b.Length, 51200); while ($n -lt $b.Length -and ($b[$n]
  -band 0xC0) -eq 0x80) { $n-- }; [Console]::Out.Write([Text.Encoding]::UTF8.GetString($b, 0, $n)) }`.
  Known limit: `Get-Content` ends a line at a bare `\r` as well, where `read` ends one only at `\n`, so in a file
  with bare carriage returns the command can count lines differently and show part of the line. PowerShell 7
  ignores `-Delimiter "`n"` for that, and a byte-scanning reader would not be a one-line command.
- Error: `Offset {offset} is beyond end of file ({total} lines total)`

Images are attached as `ImageBlock`s (C-1); for a model without image input the result says so and omits the image.
Avibe changes to Pi's text: `bmp` is not listed because v1 does not convert it, and there is no resizing (no image
dependency); oversized images are refused with a message.

## 3. `write`

Description: `Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically
creates parent directories.`

```json
{
  "type": "object",
  "required": ["path", "content"],
  "properties": {
    "path": { "type": "string", "description": "Path to the file to write (relative or absolute)" },
    "content": { "type": "string", "description": "Content to write to the file" }
  }
}
```

Result: `Successfully wrote to {path}`. Writes are serialized per canonical path with `edit`.

How a write lands (Avibe); `edit` publishes the same way. The new content is always written to a temp file beside the
file first, the owner's alone while it is there (0600 on POSIX, an owner-only DACL on Windows).

- A new file is that temp file, renamed into place, so a failure leaves its absence as it was.
- On POSIX an existing file is replaced by the temp file, which takes its mode, so a failure (a full disk, a quota)
  leaves it as it was.
- On Windows an existing file is rewritten in place, so it stays the same file object: its owner, DACL, audit
  entries, integrity label, encryption, compression, named streams (the Mark of the Web among them), hard links, and
  creation time stay as they were. The temp file only shows the volume has room, and is removed. The file is opened
  without following a link, and no other program can write, rename, or delete it while it is open; one that already
  has it open that way makes the write fail before a byte changes. With the file held, the checks made before
  publication (a writable regular file, which the path still names; for `edit`, the file it read) are made again
  against it, so the write lands only in the file that passed them.
- A file whose ACL denies writing it is refused, as a read-only one is.
- `edit` writes only over the file it read: the same file id, size, mtime, and change time (Windows' NTFS change
  time, which `stat` does not report there on every Python).

Known limits, kept as they are:

- A POSIX replacement is a new file of this user's, as with an editor that saves by renaming. Replacing a
  group-writable file another account owns makes this user its owner. The mode is carried over, but not ACLs,
  extended attributes (file capabilities among them), or hard links. POSIX has no compare-and-rename, so a change
  between the last check and the rename is not seen; a rename replaces a name and never writes through a link.
- A Windows write to an existing file is an ordinary in-place write, as an editor's save is. It is not all or nothing:
  a crash, a quota, or a failure of the disk itself in the middle of the rewrite can leave the file partly written.
  Its growth is charged to the file owner's quota, and the temp file, which shows the volume has room, reserves
  nothing.
- A new file is published by renaming its temp file, on both platforms, as before. A directory that lets this user
  create files but neither rename nor delete them (a Windows drop folder) refuses the rename and the removal alike,
  so the temp file stays there, with the ACL the new file would have had.

## 4. `edit`

Description: `Edit a single file using exact text replacement. Every edits[].oldText must match a unique,
non-overlapping region of the original file. If two changes affect the same block or nearby lines, merge them into
one edit instead of emitting overlapping edits. Do not include large unchanged regions just to connect distant
changes.`

```json
{
  "type": "object",
  "required": ["path", "edits"],
  "properties": {
    "path": { "type": "string", "description": "Path to the file to edit (relative or absolute)" },
    "edits": {
      "type": "array",
      "minItems": 1,
      "description": "One or more targeted replacements. Each edit is matched against the original file, not incrementally. Do not include overlapping or nested edits. If two changes touch the same block or nearby lines, merge them into one edit instead.",
      "items": {
        "type": "object",
        "required": ["oldText", "newText"],
        "properties": {
          "oldText": { "type": "string", "description": "Exact text for one targeted replacement. It must be unique in the original file unless replaceAll is true, and must not overlap with any other edits[].oldText in the same call." },
          "newText": { "type": "string", "description": "Replacement text for this targeted edit." },
          "replaceAll": { "type": "boolean", "description": "Replace every occurrence of oldText instead of requiring exactly one. Defaults to false." }
        }
      }
    }
  }
}
```

`replaceAll` is an Avibe addition (from Claude Code and OpenCode); the `oldText` description gains "unless replaceAll
is true". Logic, from Pi's `edit-diff.ts`:

1. Read the file inside the per-path mutation lock; strip the BOM; remember the line-ending style; normalize to LF.
2. For each edit, find `oldText` exactly; if absent, find it after Pi's deterministic normalization (NFKC, trailing
   whitespace per line, smart quotes to ASCII, Unicode dashes to `-`, special spaces to a space). No similarity
   threshold. Avibe bound: NFKC can make one character eighteen, so normalization stops once it has grown the file by
   1 Mi characters, or at a run of over 128 Ki characters with no ASCII character that is not already in NFKC (it is
   normalized in pieces split before ASCII characters); the not-found error then says that loose matching was not
   possible (below).
3. Require exactly one occurrence unless `replaceAll`; reject empty `oldText`; reject overlapping spans, including
   any occurrence matched by a `replaceAll` item.
4. Apply all edits against the original, restore line endings and BOM, write once. Nothing is written if any edit
   fails.

Result: Pi's `Successfully replaced {n} block(s) in {path}.`; the diff goes to `details` for display only. Avibe
improvement: before the period, the result names the new file's rewritten lines, ` ({lines})`, for example
`Successfully replaced 2 block(s) in {path} (lines 40-52, 88-90).` (`line 7` for a single line). Each replacement's
rewritten lines run from its first to its last character that differs from the text it replaced, so unchanged context
at either end of an edit, and a replacement that changed nothing, are left out. The range says where to look, not a
line diff: an unchanged line between two changes of one edit is inside its range. Lines are numbered as `read`
numbers them (only `\n` ends a line), touching ranges merge, and after five ranges the rest are counted
(`..., ...and 3 more`). No diff is returned to the model. Avibe: when an item uses `replaceAll`, the result counts
occurrences instead, `Successfully replaced {total} occurrences in {path} ({lines}).` (`1 occurrence` when singular),
with `; edits[0]: {count}, edits[1]: {count}` after the lines for several items, so the model need not read the file
again to count or inspect them. Errors:

- `Could not find edits[{i}] in {path}. The oldText must match exactly including all whitespace and newlines.`; when
  the normalization budget was reached, Avibe ends it with `: the file is too large to match it loosely after Unicode
  normalization.` instead of the period
- `Found {count} occurrences of edits[{i}] in {path}. Each oldText must be unique. Please provide more context to make it unique.`
  Avibe improvement: it continues ` Occurrences:` and one `line {n}: {excerpt}` line per occurrence, for the first five
  in file order, then `...and {k} more`. The excerpt is the text of the line it starts on, without indentation; a line over
  100 characters is shown only around the occurrence, with `...` where it is cut.
- `edits[{a}] and edits[{b}] overlap in {path}. Merge them into one edit or target disjoint regions.`
- `edits[{i}].oldText must not be empty in {path}.`

## 5. `bash`

Description: `Execute a bash command in the current working directory. Returns stdout and stderr. Output is truncated
to last 2000 lines or 50KB (whichever is hit first). If truncated, the output log is saved to a file; very large logs
keep their beginning and end. Optionally provide a timeout in seconds.` (Avibe change to Pi's last-but-one sentence,
because the on-disk log is bounded.) Avibe appends: `Commands still running after 120 seconds, or started with watch=true,
continue in the background as an Avibe Watch; you get a follow-up message when they finish.`

```json
{
  "type": "object",
  "required": ["command"],
  "properties": {
    "command": { "type": "string", "description": "Shell command to execute" },
    "timeout": { "type": "number", "description": "Timeout in seconds (optional, no default timeout)" },
    "watch": { "type": "boolean", "description": "Start the command as a background Watch and return immediately. Defaults to false." }
  }
}
```

`watch` is an Avibe addition. Behavior:

- stdin is closed; stdout and stderr are merged into the job's `output.log`; the tool follows the file for progress.
- The tail is kept: `[Showing lines {start}-{end} of {total}. {label}: {path}]`, or with ` (50KB limit)` when the
  byte cap applied, or `[Showing last {size} of line {n} (line is {size}). {label}: {path}]`, where `{label}` is
  `Output log (first {head}, then the last {tail} in tail.log beside it)` and `{head}`/`{tail}` are the on-disk bounds
  (J4: 1.0MB and 2.0MB). `tail.log` starts with the line `[output from byte {offset}]`. When the middle was dropped,
  `[{size} between output.log and tail.log were omitted.]` follows. Avibe change to Pi's `Full output:`: one label in
  every state, because it states the log's policy rather than its current size, and a job that is handed over keeps
  appending after its result was written, so no result promises a complete file that may not exist. The line size
  counts the whole line, including bytes the normalizer dropped from its start.
- Exit 0 is a normal result; otherwise an error result ending in `Command exited with code {code}`. `(no output)` when
  empty.
- `timeout` kills the process tree: `Command timed out after {n} seconds`. Abort kills it: `Stopped by the user; the
  command was terminated.` (Avibe change to Pi's `Command aborted`: an abort is the user's Stop, and the model should
  know who ended the command). The adapter commits that result when the run ends (recovery's renderer, T2).
- Every stop reason a job records ends its result the same way, as an error result: the output so far under the tail
  rules above, then one line naming the reason. In the foreground and in recovery (T2) alike:
  - `timeout`: `Command timed out after {n} seconds`;
  - `aborted`: `Stopped by the user; the command was terminated.`;
  - `killed` (anyone else's stop, a run ending with its command still running, or a signal that reached the
    wrapper): `Command was killed before it finished`;
  - `wrapper_error`: `Command ended because the job wrapper running it failed`;
  - a reason another owner recorded through the job host, such as `vibe_stop`:
    `Command was stopped before it finished ({reason})`, naming the reason only when it reads as a name.

  A job that never ran, or ended with neither an exit nor a recorded stop, settles as interrupted (T2).
- Handover (Avibe), when `watch: true` or the foreground window passes: the result is not an error and reads
  `Command is still running and is now Watch {watch_id}. You will get a follow-up message when it finishes.` followed
  by the output so far (tail rules above), `{label}: {path}`, and `Check: vibe watch show {watch_id}`,
  `Stop: vibe watch remove {watch_id}`.
- The Watch follow-up states the command, exit code, elapsed time, the tail under the same rules, and the output log
  under the same label.

### On Windows: `powershell` (Avibe)

The tool is named `powershell`, so the model writes PowerShell, with the same parameters, behavior, and results. Its
description is the one above with `Execute a PowerShell command in the current working directory. Returns its output
and errors.` as the first sentence; under Windows PowerShell 5.1 it adds `This is Windows PowerShell 5.1: it has no &&
or ||, so separate commands with ; and test $? or $LASTEXITCODE.` The advice in other tools' results that names `bash`
names `powershell` there, and "for example sed or a short script" becomes "for example a short script".

- Shell: PowerShell 7 (`pwsh`) when it is on the `PATH` the commands run with, else Windows PowerShell 5.1, which
  every Windows 10 and 11 has.
- The command is written to `command.ps1` in the job directory, so no length limit applies (a command line holds
  32,767 characters), and a fixed bootstrap runs it through `-NoProfile -NonInteractive -Command`
  (`-EncodedCommand` makes Windows PowerShell write errors as CLIXML). It turns off progress records and color, and
  makes the output UTF-8: PowerShell's own, and the console code page that native programs inherit;
  `PYTHONIOENCODING=utf-8` is set unless the environment sets it, so Python's standard streams are UTF-8 while
  the files it opens keep the system's encoding.
- A command that does not parse fails with exit code 1 and the parser's messages before anything runs. Otherwise
  it runs whole, as one script block, so PowerShell's own semantics hold (`return`, classes, named blocks, traps,
  `using`). Only the statement that runs last is wrapped, to record how it ended; a `return`, `break`, or
  `continue` at the top level ends the command.
- The exit code follows PowerShell's `$?` for the last statement of the last block that runs (`clean`, which
  PowerShell 7.3 and later run after the others, and not at all in a script with no other block; else `end`, else
  `process`, else `begin`, else `dynamicparam`): the command's own `exit N`; else 0 when that statement succeeded, or the command ended
  before it; else the `$LASTEXITCODE` that statement set, when it is not 0, so a failed native program reports its
  code; else 1. A code an earlier statement left in `$LASTEXITCODE` is not the last statement's, and a function
  that writes an error and returns has succeeded, as `$?` says.
- The command sees `$LASTEXITCODE` and `$?` exactly as PowerShell sets them, so a last statement may test the one
  before it (`cmd /c exit 7; if (-not $?) { exit $LASTEXITCODE }`), and its named blocks may be written in any order.
  A compound last statement (an `if`, a loop, a `try`) is one statement: when a native program in it fails and a
  cmdlet after it fails too, the code is the native program's, which that statement set.
- Errors are reported at the command's own lines: the wrapping adds no line before the last statement, and one after
  it only when it uses `--%`, whose arguments run to the end of its line. Only an error in a last statement that
  shares its line with the statement before it or with its block's opening brace, or that follows a `--%` statement,
  is reported at a later column. Parameter binding, a `param` default included, runs before the last statement. The wrapping reads nothing that
  `Set-StrictMode` forbids, so a command may turn strict mode on.
- Known limit: which statement set `$LASTEXITCODE` is tracked for ordinary scripts. In unusual code it can credit
  the last statement with an earlier native code, for example after a named block's keyword followed by a comment
  that holds a brace (`end <# { #> { … }`).
- Known limit: PowerShell in ConstrainedLanguage mode (an AppLocker or WDAC policy) is not supported. The bootstrap
  needs .NET calls that mode blocks, so the tool says so and runs nothing.
- Known limit, as with `powershell -Command`: an `exit` ends the process before PowerShell formats objects that are
  still waiting to be laid out as a table, so `Get-ChildItem | Select-Object Name; exit 2` can print nothing.
  Strings and the output of native programs are written as they come.

## 6. Output normalizer

Every model-facing command output (`bash` results, job logs, Watch follow-ups) passes through one normalizer: decode
as UTF-8 with replacement, strip ANSI escape sequences, collapse carriage-return redraws to the final line state, then
apply the caps above. `read` returns file contents unchanged apart from Pi's caps: the model copies that text into
`edit`, which must match the real file. For `pipe` jobs the first steps are usually no-ops; for the future `pty`
backend they are required.

## 7. Job handle

`JobHost` is the interface `tools` uses; the adapter implements it, with Watch for handover. Persisted metadata:
[`job.schema.json`](job.schema.json).

| Operation | v1 | Meaning |
| --- | --- | --- |
| `start(command, cwd, env, timeout, call)` → `job_id` | yes | create the job directory and metadata for the call instance `call`, then spawn the wrapper in its own session (on Windows, its own job object) |
| `status(job_id)` | yes | `running`, `exited{code}`, or `gone` (ended without an exit code) |
| `wait(job_id, deadline_s)` | yes | until exit, or at most `deadline_s` seconds from now (`None`: until exit) |
| `output_path(job_id)` | yes | absolute path of `output.log`, named in truncated and handover results |
| `output(job_id, since)` | yes | output after a logical offset into everything the command has produced (not a file position), plus the new offset; bytes dropped by the on-disk bound are reported as omitted, never silently skipped |
| `kill(job_id)` | yes | terminate the process tree, verified by process identity |
| `hand_over(job_id)` → `watch_id` | yes | register a once Watch with target kind `job` |
| `send(job_id, keys)`, `screen(job_id)`, `resize(job_id, cols, rows)`, `attach_info(job_id)` | reserved | `pty` backend (plan §5.4) |

A job belongs to its call instance (`CallInstance`, the loop's `ToolContext.call`): the committed response that made
the call, as its row id and `context_seq`, plus the call id. Providers reuse call ids, so every lookup from a call to
its job matches the whole instance, and none compares a job's time with a row's: the wall clock can step back. A
checkpoint turn's call has no committed response and starts no job.

Job directory: `<state>/agent_core/jobs/<job_id>/` with `meta.json`, `output.log`, and the files the `tools` lane's
launch mechanism needs. The job host meets recovery invariants J1–J6 ([`recovery.md`](recovery.md)): a command starts
at most once and recovery can always decide whether it did; its identity is recorded before it runs; `timeout` is an
absolute deadline that survives handover and restarts; output on disk is bounded; files are kept until the call is
durably settled; handover creates at most one Watch per job.

On Windows the wrapper's tree is a job object it creates and joins before it can run the command, named after the
job and ended when the wrapper's handle to it closes. Every process the command starts is in it and cannot leave it.
`kill` terminates the job object; a kill of the wrapper alone (`TerminateProcess`, which nothing can catch) also ends
the tree but records no reason, so T2 settles that call as interrupted rather than `killed`. A process the command
leaves running keeps the wrapper, and so the job, alive until it ends or is killed; on POSIX it runs on in the process
group. On both, the deadline is the command's (J3): once the shell has exited, it reaches no such process. Job files are replaced with POSIX rename semantics and Avibe reads them with delete sharing, so no reader of
Avibe's makes the wrapper's replacement of `tail.log` fail; while another program holds it open without that sharing,
the wrapper keeps the tail and writes it at a later snapshot. The last snapshot, taken when the command exits, cannot
wait: it is written into `tail.log` itself, which such a program shares for writing (Python's `open`,
`Get-Content -Wait`); one that shares neither leaves the previous tail there while it holds the file. The command's
exit stands either way.
A decision or a stop reason is created once by a rename, which never replaces on Windows, rather than by a hard link,
so job files work on a volume without hard links (FAT, exFAT).

## 8. Environment section

Changing facts do not go into the system prompt, which stays stable and cacheable. When the loop consumes an input
(C-5 `ModelInput`), it renders an environment block into that message with the fields that changed since the previous
input (all fields on the first input), so the stored transcript still equals what the model saw. The first input after a checkpoint carries every field
the projected context no longer shows, because the inputs that carried the earlier values may be summarized away:

```text
<environment>
cwd: /absolute/path
os: macOS 26.0 (arm64)
shell: /bin/bash
date: 2026-10-02
timezone: Asia/Shanghai
watches: wch_8f2k "nightly sync" command running; wch_3c1d job running
</environment>
```

`shell` is the shell the commands run in: the job host's on POSIX (`/bin/bash`, else `bash` on `PATH`, else `sh`), and
on Windows `PowerShell 7 ({path})` or `Windows PowerShell 5.1 ({path})`.
`watches` names the Session's enabled Watches by id, name, and kind, never a command: at most 20, then `and N more`.
A field's identity is its raw value; only the block displays it, through the one display helper (`display`, C-9
`context.md` §7): one line of plain text, escaped injectively (a backslash doubled, control characters and `<`, `>`
as `\uXXXX`), then cut in the middle to 160 UTF-8 bytes, each Watch's line on its own. The cwd is escaped and never
cut: the model builds absolute paths from it, so a cut one would be false, and the OS bounds its length. So the block
stays bounded on every input whatever the script, and no value can add a line, forge a field, or close the block.
The rows hold only what the model read, so a field counts as unchanged only when its display equals what the model
last read and is not cut: escaping is injective, but a cut display can look the same after its value changed, so a
cut field is sent on every input rather than taken for unchanged.

## 9. Project instructions (Avibe)

The Agent reads the user's instruction files into its system prompt, as Pi does (`core/resource-loader.ts`
`loadProjectContextFiles` and `core/system-prompt.ts` `renderProjectContext`). The native CLIs load their own, and
Avibe's shared prompt tells every agent that `AGENTS.md` is what later Agents load first.

**Which files, in order.**
1. The global file: `AGENTS.md` in the Avibe home, `~/.avibe/AGENTS.md` by default or under `AVIBE_HOME`
   (`config.paths.get_global_instructions_path`). It is one known path, so a user who keeps global principles
   elsewhere can sync them to it.
2. Each directory from the filesystem root down to the Session's cwd. Each contributes the first of these that is a
   readable file: `AGENTS.override.md`, `AGENTS.md`, `AGENTS.MD`, `CLAUDE.md`, `CLAUDE.MD`. A candidate that is not a
   file, or cannot be read, is skipped silently, and the next one is tried.

How the files are read:
- Symlinks are followed. A file reached twice (the same real path) is loaded once: as the global file if it is that
  one, else at the directory nearest the cwd. The first readable candidate is its directory's file even when blank.
- A linked git worktree nested inside its main repository has two copies of the same file: the main repository's is an
  ancestor of the cwd, and the worktree root holds its own. Loading both would apply that repository's instructions
  twice, so the main repository's file with the same name as the worktree root's is left out.
  - This is Pi's rule. Finding the main repository reads the worktree's `.git` file and the `commondir` it points to.
  - Instruction files themselves are read only from the ancestor chain and the global path.
- A blank file is left out.
- Each file is read as UTF-8, with a leading byte-order mark dropped and undecodable bytes replaced.
- Each file is read only as far as the limit below, plus one character's bytes. Every Turn reads the files, so a
  mistaken huge one costs no more than a full section.
- A file's size:
  - read whole, it is the size of its text as read, each undecodable byte counting as its three-byte replacement
    character;
  - otherwise, it is the size the file system reports.

  Either way, the limit holds for what the section carries.
- Of `.git` and `commondir`, only the one path line Git writes is read (8 KiB at most).

**Where they enter.** A `<project_context>` section of the system prompt. It comes after the Agent's head (preamble,
tools, guidelines) and before Avibe's injected sections:

```text
<project_context>
Project-specific instructions and guidelines:
More general files come first; where two conflict, the later, more specific one wins.

<project_instructions path="/Users/me/.avibe/AGENTS.md">
...
</project_instructions>

<project_instructions path="/Users/me/src/app/AGENTS.md">
...
</project_instructions>
</project_context>
```

- The first line is Pi's. The second is Avibe's.
- Each path is escaped as the environment block's cwd is (§8): never cut.
- Three reasons for the place:
  - It is Pi's order: rules, then project context, then skills.
  - The head and these files are the same for every Session in that cwd, while Avibe's sections carry the Session's
    own facts, so the provider's cache shares the longer prefix across Sessions.
  - The Agent's own definition, in Avibe's sections, is more specific than a repository's guidance, and comes later.
- With no file to load, the section is absent.

**Read every Turn, cache-stable.** The files are read again at the start of every Turn, from that Turn's cwd, so an
edit takes effect on the next Turn. The section depends only on the files' paths and contents, never on times or
modification stamps, so the system prompt changes only when a file does. What uses the Turn's system prompt gets the
same section: the checkpoint turn and the request after a checkpoint (C-9), and a side turn (C-10). A fork Session reads
its own at its first Turn.

**Bound.** The files share 65,536 bytes (64 KiB) of content, about 16,000 tokens. The owner's own set is about 31 KB
(a global file, a workspace file, and a repository file), so Codex's 32 KiB default would already cut it.
- **Who gets the budget.** First the global file, then the files from the cwd upward. The most specific project file is
  therefore the last to be cut. The section keeps the order above either way.
- **A file the budget cuts** keeps its first bytes, on a character boundary, and ends with:
  `[Truncated: the first {shown} of {size} bytes. The instruction files share a 65,536-byte limit; read the file for the rest.]`
- **Files the budget leaves out entirely** are named after the last block:
  `[Not loaded, over the 65,536-byte limit for instruction files: {path} ({size} bytes); ... Read them if the task needs them.]`

**Deviations from Pi.**
- The global file is one path, `AGENTS.md` in the Avibe home, not the candidate list in the agent's own directory.
- The total byte limit and its notices; Pi has no limit.
- A file is loaded once per real path, not once per path string.
- A blank file is left out.
- An unreadable file is skipped silently. Pi warns on stderr, which the Agent has no place to show.
- The precedence line.
