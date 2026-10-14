# Terminal-Bench reliability and failure diagnosis

## Scope and evidence

Continue the full-runtime benchmark branch; preserve the original smoke and
paired-pilot artifacts. The installed-package pilot ended with a real failed
Run and verifier reward 0. Its summary says `empty_response`, but the retained
artifacts do not contain the final provider stop reason or response shape.
The reference lost its Harbor owner without a terminal trial receipt and left
an orphan task container. Neither observation proves model capability or the
historical root cause. No additional paid trial is authorized by this repair.

The prior owner has explicitly stopped writing. This session is the sole writer;
independent audits are read-only. All execution tests run in the user-authorized,
dedicated remote benchmark environment, never the local VM or another tenant.
Do not modify shared host services, resource caps, provider budgets, official
tasks/verifiers, or old results.

## Repair contract

1. Reproduce final empty, reasoning-only, and output-truncated provider responses
   with scripted, zero-paid input. Preserve the distinction between a provider
   stop with no answer and an explicit output-limit stop. Do not silently increase
   output limits, retry a whole task, replay successful tools, or mark incomplete
   work successful. Any core change needs a failing before/fixed after test and
   must agree with the shared loop contract, not special-case a benchmark/model.
2. Add a bounded diagnostic projection to the wrapper's existing `execution.json`.
   Read through normal product CLI/API, not raw SQLite/transcript files. Export
   only allowlisted error/stop categories, response block counts/lengths, reported
   token usage and completeness status. No raw prompts, reasoning text, provider
   keys, source configs, raw logs or internal database copies. Missing diagnostics
   must be explicit, must not change the real Run outcome, and must not block
   cleanup. Capture before the owned service is stopped. Keep old receipts intact.
3. Add a source-only Linux evaluation-host supervisor around Harbor. A systemd
   task-owned unit owns its process tree independently of SSH; Avibe Harness
   observes the durable receipt rather than re-executing the paid command. Claim
   a fresh run directory and a single-host benchmark lock before launch; validate
   config paths, wheel hash and limits without a provider call; preserve the input
   config and an explicit local-path relocation manifest. No mutation of product
   dependencies or user/live Avibe state.
4. Every supervisor exit writes a separate orchestration receipt. A dead Harbor
   parent is failure even if a stale result says running; success requires a
   complete trial receipt. Preserve Harbor's originals and never invent reward,
   usage, or verifier completion. A remote hard wall cap is independent of SSH
   and bounds setup/execution/verification; the configured Harbor agent budget is
   unchanged and recorded separately. Stop/delete only containers/networks with
   positively established trial labels, after preserving task artifacts/logs.
   Never broad-prune a Docker daemon or pattern-kill processes.
5. Secrets travel via protected files/stdin, never command arguments or logs.
   Reject missing/empty/wrong-mode credential files before Harbor starts. Consume
   transport files promptly; diagnostics and receipts must pass sentinel scans.

## Validation gates

- Table-driven provider/loop regressions: empty vs reasoning-only vs length,
  valid tools/text, refusal/interruption and retry budget preservation.
- Diagnostic projection: synthetic secret/text sentinels never exported,
  correct final stop/error shape, bounded query, explicit unavailable state.
- Supervisor: normal terminal result, preflight rejection, nonzero/zero exit with
  missing result, hard timeout, exact-owned cleanup with an untouched bystander,
  and two-submit/single-owner refusal.
- Remote no-paid acceptance: drop SSH after submission; work continues, then
  produces a terminal receipt. Kill the child or hit a short deadline; receipt
  and cleanup still happen. Verify unrelated services were not targeted.
- Focused tests and lint remotely, independent review, then normal PR/CI gates.
  No merge, deployment to the live user runtime, official-task rerun, or expanded
  benchmark batch is included.

## Historical limitations to retain in reporting

- The candidate's final raw response was not retained. A reproduction of one
  failure class does not retroactively prove that class caused this pilot.
- The reference has partial trajectory usage, not a completed score or bill.
- The initial synthetic reward 1 is only integration evidence.
- A successful repair simulation is not a claim of improved benchmark scores.
