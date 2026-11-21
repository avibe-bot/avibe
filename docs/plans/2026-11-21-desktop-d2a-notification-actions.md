# Desktop D2a: answer an Agent from the macOS notification

Owner decision: Max, 2026-11-20 10:15 (all D2 recommendations). Runtime authority: `docs/plans/desktop-d2-runtime-contract.md` (#4039). Design: `ui/design: desktop/notification-actions/*` (proposal, matter folder `d2-design/`). Scope: the local instance on macOS. Remote instances, Windows actions, time-sensitive notifications, calendar and Vault buttons are out of scope.

## Behavior

1. **One notification center.** Every macOS post goes through `UNUserNotificationCenter`. The deprecated center is no longer used. Authorization is asked once, by the first post. The delegate is set in plugin setup, so a click that launches Avibe still reaches it.
2. **A click opens the notification's subject:**
   - a run opens its Session (`avibe://session/<id>`);
   - a Vault request opens that request (`avibe://vaults/request/<id>`);
   - anything else only brings the main window forward.
3. **Waiting for your answer.** When an Agent message in the local instance carries quick replies and the main window is not in front, Avibe posts a notification.
   - It honors the account switches (Bots, Sessions) and the Desktop switch.
   - The first three quick replies become buttons, and "Reply…" opens a text field.
   - The notification is withdrawn by the existing read reconciliation once the Session is read anywhere.
4. **Answering.** The shell hands the action to the current local Workbench document, which posts it to the Runtime with the user's own authority:
   - a choice: `metadata.quick_reply_for`;
   - text: `in_reply_to`.
   - Both carry `operation_id` (one per click) and `revision` (`content.quick_reply_revision`).
   - A question without a revision (older Runtime) gets no buttons.
5. **Outcomes.**
   - `accepted` / `duplicate` of an accepted answer: nothing more is shown.
   - `already_handled`, `stale`, `forbidden`, `not_found`, or no current document within 30 s (`unavailable`): one follow-up notice states why nothing was sent, and a click opens the Session.
   - Nothing is retried automatically.

## Native ↔ document contract (`__AVIBE_DESKTOP_ANSWER__`, version 1)

The local Workbench defines, at load, `window.__AVIBE_DESKTOP_ANSWER__ = { version: 1, document, begin(request), status(id) }`. `document` is a per-load identity. The shell reaches it only through the fenced eval Ask Vibey uses: top document, exact Runtime origin, unchanged receiver identity.

| Field | Producer | Consumer | Meaning |
| --- | --- | --- | --- |
| `id` | shell | document | the `operation_id`, one per click |
| `sessionId`, `messageId` | shell (from the posted question) | document → Runtime | the question answered |
| `revision` | Runtime (`quick_reply_revision`) via shell | Runtime | the option set the user saw |
| `kind` | shell | document | `choice` or `text` |
| `text` | shell | document → Runtime | the option, or the typed reply |

- `begin` returns `"started"`, or `"invalid"` for a malformed request.
- `status(id)` returns `"pending"` until the POST settles, then a JSON string `{outcome, answerKind, choice}`. `outcome` is one of the Runtime outcomes, or `unavailable` for a transport failure or an uncertain dispatch.
- The document never retries. An HTTP 401/403/404 without an `answer` body becomes `forbidden` / `not_found`.

## Validation

- Unit tests: candidate filtering (author, platform, options, dedup), click targets, outcome mapping, the receiver's request and response mapping.
- Real package: actions, reply, cold launch, and a click after the instance changed.
- Acceptance on a Developer ID TEST RC.
