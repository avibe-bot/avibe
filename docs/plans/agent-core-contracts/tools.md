# C-7 Tools

The coding tools in `core/agent_core/tools/`. Names, parameters, logic, and model-facing text follow Pi
(`packages/coding-agent/src/core/tools/` at `7fbbd5f`, MIT, Copyright (c) 2025 Mario Zechner); strings quoted below
are Pi's unless marked as an Avibe addition. tau 0.4.7 (MIT, Copyright (c) 2026 Alejandro AO) is the Python
reference. One tool set serves every model.

## 1. Constants

| Name | Value |
| --- | --- |
| `MAX_LINES` | 2,000 |
| `MAX_BYTES` | 51,200 (50 KB) |
| Foreground window for `bash` before handover to Watch | 120 s, configurable (Avibe) |

## 2. `read`

Description: `Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp). Images are sent
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
  `{quoted_path}` is the path shell-quoted (Avibe change to Pi's text, so the suggested command is always safe to run)
- Error: `Offset {offset} is beyond end of file ({total} lines total)`

Images are attached as `ImageBlock`s (C-1) after resizing; for a model without image input the result says so and
omits the image.

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
   threshold.
3. Require exactly one occurrence unless `replaceAll`; reject empty `oldText`; reject overlapping spans, including
   any occurrence matched by a `replaceAll` item.
4. Apply all edits against the original, restore line endings and BOM, write once. Nothing is written if any edit
   fails.

Result: `Successfully replaced {n} block(s) in {path}.`; the diff goes to `details` for display only. Errors:

- `Could not find edits[{i}] in {path}. The oldText must match exactly including all whitespace and newlines.`
- `Found {count} occurrences of edits[{i}] in {path}. Each oldText must be unique. Please provide more context to make it unique.`
- `edits[{a}] and edits[{b}] overlap in {path}. Merge them into one edit or target disjoint regions.`
- `edits[{i}].oldText must not be empty in {path}.`

## 5. `bash`

Description: `Execute a bash command in the current working directory. Returns stdout and stderr. Output is truncated
to last 2000 lines or 50KB (whichever is hit first). If truncated, full output is saved to a temp file. Optionally
provide a timeout in seconds.` Avibe appends: `Commands still running after 120 seconds, or started with watch=true,
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
- The tail is kept: `[Showing lines {start}-{end} of {total}. Full output: {path}]`, or with ` (50KB limit)` when the
  byte cap applied, or `[Showing last {size} of line {n} (line is {size}). Full output: {path}]`. When the job's log
  itself was bounded on disk (J4), `Full output:` becomes `Output log (middle omitted beyond {cap}):`, so no result
  promises a complete file that does not exist.
- Exit 0 is a normal result; otherwise an error result ending in `Command exited with code {code}`. `(no output)` when
  empty.
- `timeout` kills the process tree: `Command timed out after {n} seconds`. Abort kills it: `Command aborted`.
- Handover (Avibe), when `watch: true` or the foreground window passes: the result is not an error and reads
  `Command is still running and is now Watch {watch_id}. You will get a follow-up message when it finishes.` followed
  by the output so far (tail rules above), `Full output: {path}`, and `Check: vibe watch show {watch_id}`,
  `Stop: vibe watch remove {watch_id}`.
- The Watch follow-up states the command, exit code, elapsed time, the tail under the same rules, and the full-output
  path.

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
| `start(command, cwd, env, timeout)` → `job_id` | yes | create the job directory and metadata, then spawn the wrapper in its own session |
| `status(job_id)` | yes | `running`, `exited{code}`, or `gone` (ended without an exit code) |
| `wait(job_id, deadline_s)` | yes | until exit, or at most `deadline_s` seconds from now (`None`: until exit) |
| `output_path(job_id)` | yes | absolute path of `output.log`, named in truncated and handover results |
| `output(job_id, since)` | yes | output after a logical offset into everything the command has produced (not a file position), plus the new offset; bytes dropped by the on-disk bound are reported as omitted, never silently skipped |
| `kill(job_id)` | yes | terminate the process tree, verified by process identity |
| `hand_over(job_id)` → `watch_id` | yes | register a once Watch with target kind `job` |
| `send(job_id, keys)`, `screen(job_id)`, `resize(job_id, cols, rows)`, `attach_info(job_id)` | reserved | `pty` backend (plan §5.4) |

Job directory: `<state>/agent_core/jobs/<job_id>/` with `meta.json`, `output.log`, and the files the `tools` lane's
launch mechanism needs. The job host meets recovery invariants J1–J6 ([`recovery.md`](recovery.md)): a command starts
at most once and recovery can always decide whether it did; its identity is recorded before it runs; `timeout` is an
absolute deadline that survives handover and restarts; output on disk is bounded; files are kept until the call is
durably settled; handover creates at most one Watch per job.

## 8. Environment section

Changing facts do not go into the system prompt, which stays stable and cacheable. When the loop consumes an input
(C-5 `ModelInput`), it renders an environment block into that message with the fields that changed since the previous
input (all fields on the first input), so the stored transcript still equals what the model saw. The first input after a checkpoint carries all fields
again, because the inputs that carried the earlier values may be summarized away:

```text
<environment>
cwd: /absolute/path
os: macOS 26.0 (arm64)
shell: /bin/zsh
date: 2026-10-02
timezone: Asia/Shanghai
watches: wch_8f2k "pytest -q" running 6m
</environment>
```
