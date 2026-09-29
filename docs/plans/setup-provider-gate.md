# Require a provider before assistant setup

## Problem and contract

An empty provider inventory could advance to assistant setup through the footer
hint or a declined migration. Assistant entry then required a model connection
that none of its controls could establish.

- The provider screen can continue only after its source read confirms an
  `active` or `standby` Hub source. Native CLI subscriptions cannot supply
  setup's Hub route and do not satisfy this prerequisite.
- Without a source, the primary action opens the subscription/API-key dialog.
  Declining migration preserves the dismissal and keeps the Add action.
- A detected key is an import offer, not a connected source. Reading failures
  retain Retry; pending writes and readbacks cannot advance.
- If a current source read on the assistant screen finds no usable source,
  entry stays disabled even when a direct backend reports itself eligible;
  the hint offers Add model source and returns to providers.
- Each return to providers requires a fresh source read. An inventory from
  the previous visit or a write that settled while hidden cannot enable Continue.

## Scope and validation

Reuse the existing provider action state machine and setup navigation. Remove
the bypass rather than adding another navigation or persistence mechanism.
Backend entry readiness, migration consent, source validation and completion
remain owned by their existing flows.

AUTH-SETUP-126 covers the provider component, registered Wizard, assistant
recovery and a browser journey in English and Chinese with controlled network
responses. AUTH-SETUP-111 also checks the real source API's empty/created
inventory readback. These layers do not establish packaged desktop acceptance
against a live instance.

Required checks: affected component files, source API scenario harness, browser
provider/assistant journeys, UI lint, TypeScript checks and production build.

## Review diagnosis and scope decision

The full PR #2265 review inventory contains two findings-bearing heads:
`c48088e37b` and `e67a2aafd1`. Both exposed the same entry-admission class:
backend eligibility was treated as sufficient when provider evidence was empty
or unknown. The second head also exposed a retry handler that bypassed the
button's guard and a native-CLI source incorrectly admitted as Hub supply.
This triggered the review-loop circuit breaker before another implementation edit.

Inspection of both screen actions, their handlers, the retained-screen lifecycle,
the source projection and `entryGate.routeRunnable` confirms a local ownership
gap, not a missing persistence model. Completion already requires Hub mode.
The smallest complete fix is to require a successful current inventory with a
usable Hub source for all setup entry actions, including retry; display an
inventory-read retry for failed reads; and apply the same Hub-source predicate
to the provider diagram and its Continue action. Pending/failed reads must never
be mistaken for a confirmed empty list or successful provider evidence.

The existing route-read epoch/token, provider read authority and completion gate
remain the owners. No parallel state store or new backend policy is needed.
Consumer validation covers pending/failing reads and retry recovery, native-only
inventory, stale completion retry, current-visit readback and ordinary Hub entry.

Review of `8dd1e03926` found the remaining producer side of the same custody
boundary: setup still opened the general subscription chooser, whose Anthropic
default creates native CLI custody. The full inventory now has three
findings-bearing heads; the custody class repeats on the latter two. Before
editing again, the producer-to-consumer audit confirmed that the existing OAuth
API already supports Hub custody for both setup subscription vendors. Constrain
only setup's create flow to that supported custody in the shared OAuth dialog,
retaining its explicit sign-in and disclosure step. Settings and reauthentication
keep their existing channel choice. Validate real dialog composition through
the browser, including the outgoing OAuth channel and post-login inventory gate.

The fourth findings-bearing head, `d71d1e68fe`, repeated the evidence-lifecycle
class at a different boundary: completion performs fresh server reads, but a
refusal did not invalidate the assistant screen's earlier inventory. The full
inventory still has one unresolved finding. Inspection of `Wizard.complete` and
all entry-handler call sites confirms that its thrown refusal is the existing
handoff boundary. Refresh the existing route/source reader after that refusal,
without retrying completion or any write automatically. A registered-Wizard case
removes supply while the assistant screen stays mounted, holds the recovery read,
and then verifies the provider recovery action with no completion writes.
