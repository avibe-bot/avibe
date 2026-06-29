# Vaults UI — Design Alignment (restore design.pen)

## Why

The shipped Vaults UI drifted from the `design.pen` frames (the visual source of
truth). Realign **every** Vaults surface to its design frame, faithfully. The
frames — and their exported PNGs — are the literal spec. The root cause of the
drift was that nobody compared the implementation against the frames; this task
closes that gap and a gatekeeper will re-verify each surface against the frame
before merge.

## Source of truth

`design.pen` (open via the `pencil` MCP tools, `filePath:
/Users/cyh/vibe-remote-project/design.pen`). Exported reference PNGs are in
`/tmp/vault-design/`. For each surface, BOTH read the PNG and read the frame via
`batch_get` (high `readDepth`, `resolveVariables: true`) to get exact copy,
spacing, colors, radii. Map every value to a UI token/class; add a token if one
is missing — never hardcode a one-off hex.

| Surface | design frame (id) | reference PNG | implementation |
| --- | --- | --- | --- |
| Create / "Add a secret" | `vyed5` (dialog `KsYUN`) | `vyed5.png` | `ui/src/components/ui/vault-secret-form.tsx` + `AddSecretDialog` in `components/workbench/VaultsPage.tsx` |
| Protected **setup** | `kAmWj` (modal `gV1j9`) | `kAmWj.png` | `vault-protected-unlock.tsx` (needs-setup branch) |
| Protected **unlock** | `g5Q7F` (modal `Yv2bL`) | `g5Q7F.png` | `vault-protected-unlock.tsx` (locked branch) |
| Approval — access | `w8u5l` (card `SKBld`) | `w8u5l.png` | `vault-approval-card.tsx` (access) |
| Approval — signing | `eWOjt` (card `pRtHq`) | `eWOjt.png` | `vault-approval-card.tsx` (sign) |
| Provision ($NAME) | `F4N19` (card `NqXrN`) | `F4N19.png` | `vault-secret-form.tsx` (fixedName mode) |
| Hub page | `y4rw5Q` | `y4rw5Q.png` | `components/workbench/VaultsPage.tsx` |
| Group dropdown (open) | `WgGQy` (popover `e3rPI`) | `WgGQy.png` | `Combobox` group picker in `vault-secret-form.tsx` |

## Confirmed divergences (audited — fix these for sure)

### A. Protected setup (`kAmWj`) — `vault-protected-unlock.tsx` needs-setup branch
Current: a passkey card AND a full password `<form>` shown at once as two coequal
sections, embedded in the create `<form>`. Target (match `kAmWj.png`), ONE
recommended path:
- shield-check icon badge; title "Set up protected secrets"; subtitle.
- ONE recommended green card: primary button **"Add a passkey"**, a RECOMMENDED
  badge, caption "Face ID · Touch ID · security key — the most secure option".
- a recovery tip box (add a second passkey / save a recovery code).
- a **"Use a password instead"** text link + a red "less secure" badge; clicking
  it **reveals** the password fields (progressive disclosure) — hidden by default.
- "Maybe later" dismiss.
- **HARD: remove the `<form>` element.** Use buttons + `onClick` reading state.
  Password setup is a button `onClick`, not a form submit. (This is the
  nested-`<form>`-inside-the-create-`<form>` bug that caused a full-page reload.)

### B. Protected unlock (`g5Q7F`) — `vault-protected-unlock.tsx` locked branch
Target (match `g5Q7F.png`): scan-face icon; "Unlock to continue"; subtitle
"<NAME> is protected — unlock it in your browser to sign"; primary **"Unlock with
passkey"** + caption "Face ID · Touch ID · security key"; **"Use password
instead"** reveal link (less secure) → reveals password field + unlock button; a
mint factor-safety note; Cancel. Again **no `<form>`** — onClick buttons.

### C. Create dialog (`vyed5`) — `vault-secret-form.tsx`
- **Kind** selector: design is a **2-segment toggle** (Static value | Signing
  key), not two big cards. Use a segmented control (reuse the existing
  Tabs/segmented primitive, or build one consistent with the design system).
- **Field order**: Kind → Name → Value → Protection → Group → **Advanced**.
- Name hint: "Uppercase A–Z 0–9 _ · globally unique".
- Value: input + eye toggle; placeholder "Paste the secret value".
- Protection: two cards Standard / Protected, exact copy — Standard
  "Machine-encrypted. Headless use OK. For API keys.", Protected "Unlocked in
  your browser. Survives machine theft. No headless."; selected = green border +
  check.
- **Advanced**: a single collapsible box containing (a) "Allowed hosts (for proxy
  fetch)" input, and (b) an **"Always ask before each use"** toggle with help
  "Never granted in advance, even per-key". The impl currently lacks both the
  Advanced grouping and the toggle.
- Primary button label: **"Create secret"** (currently "Save"/"保存").
- Setup/unlock: do NOT embed as a coequal third section under the form. When
  Protected is chosen and the vault is not unlocked, show the setup/unlock step
  (per A/B) as the gating step; keep "Create secret" disabled until unlocked. Must
  not be a nested form and must match the frames.
- **DECISION — do NOT silently drop**: `description` + `tags` exist in the impl
  but are absent from the `vyed5` mock. Do not delete them; fold them under
  "Advanced" (or keep minimal) and **flag this in the PR description** for owner
  review. Also verify whether "Always ask before each use" maps to a real backend
  policy field; if it is not supported, **flag it** rather than fake a dead toggle.

### D. Approval cards (`w8u5l` access, `eWOjt` signing) — `vault-approval-card.tsx`
Compare header / detail rows / scope options / footer to the frames and align
copy + layout. The embedded protected-unlock panel inherits the A/B fix (this
file reuses `VaultProtectedUnlock`).

### E. Hub (`y4rw5Q` → `VaultsPage.tsx`), Provision (`F4N19`), Group dropdown (`WgGQy`)
Compare each to its frame and fix divergences (sections, spacing, control styles,
copy). Provision = the `fixedName` mode of the form → match `SecureInputCard`.
Group dropdown → match `GroupPopover`.

## Constraints
- The `design.pen` frames are the literal spec — match them. Map every
  size/weight/spacing/radius/color/shadow to a UI token/class; add the token if
  missing; never hardcode a one-off hex.
- Reuse `ui/src/components/ui` primitives (Button, Badge, Card, Dialog, Input,
  Switch, Combobox, Tabs/segmented…). Extend a primitive before forking.
- i18n: all copy through `ui/src/i18n/en.json` + `zh.json`, kept 1:1. Reuse
  existing keys; add new ones in both files.
- `cd ui && npm run build` (tsc + vite) MUST pass.
- Do NOT change backend behavior or the crypto. **CARDINAL INVARIANT**: plaintext
  — secret values, released DEKs, private keys — lives ONLY in the browser and in
  avault; the daemon only ever relays opaque blobs. Restyling must not alter any
  crypto / submit path. Keep the keypair sign-only guard.
- Do not touch the parked sandbox (#706) or unrelated code.

## Deliverables
1. Every Vaults surface realigned to its frame.
2. A "Results" section appended here: per surface, what diverged and what changed,
   referencing the frame.
3. `npm run build` green.
4. A NON-draft PR titled `fix(vaults): realign UI to design.pen frames`.
   Description must name the capability, list the surfaces changed, call out the
   description/tags + "always ask" decisions/flags, and state evidence layers
   (build; manual; Codex E2E pending).
5. Own the Codex review loop (background-watch-hook + bundled `wait_pr.py`),
   re-request `@codex review` after each push, fix findings at root. Do NOT
   merge — the gatekeeper merges after Codex passes and after fidelity is
   verified against the frames.

## Verification (by the gatekeeper — not the implementer)
- Render each built surface and compare side-by-side to the frame PNG.
- Codex runs an end-to-end test in the local Incus regression env to confirm the
  design is restored.

## Results

All copy/spacing/colors mapped to existing tokens/classes (`mint`/`mint-soft`,
`gold`, `violet`/`violet-soft`, `accent`/`accent-soft`, `destructive`, `surface`/
`surface-2`, `border`, `muted`/`muted-foreground`, `foreground`). No one-off hex
added. i18n: every string routed through `en.json` + `zh.json`, verified 1:1 (197
keys each, no gaps) and every referenced key resolves. `cd ui && npm run build`
(tsc + vite) passes. Cardinal invariant preserved: the crypto/submit path
(`sealValue` / `sealBlindBox` / `getVaultPubkey` / `createVaultSecret` /
`establishing_vmk` / keypair `signer_kind` + `public_meta`) is byte-for-byte
unchanged; the only payload change is restructuring `policy` and adding the real
`policy.always_ask` field (see Flags). Setup, unlock, create, provision, and the
group dropdown were rendered in a throwaway light-theme harness and visually
confirmed against the frame PNGs (harness removed before commit).

### A. Protected setup (`kAmWj`) — `vault-protected-unlock.tsx` (needs-setup)
- **Diverged:** two coequal sections (passkey card + a full password `<form>`)
  embedded inside the create `<form>` — invalid nested form that reloaded the page.
- **Changed:** full rewrite, form-free (buttons + `onClick`). Centered shield-check
  badge, title "Set up protected secrets" + subtitle. ONE recommended green card
  (1.5px mint border on `mint-soft`): `RECOMMENDED` badge, solid green "Add a
  passkey" (`variant="brand"`), caption. Life-buoy recovery tip. "Use a password
  instead" + `less secure` badge link that **progressively reveals** the password
  fields (hidden by default; auto-revealed only when no passkey is available here).
  "Maybe later" dismiss (via new optional `onDismiss`). Enter submits via keydown,
  not form submit.

### B. Protected unlock (`g5Q7F`) — `vault-protected-unlock.tsx` (locked)
- **Diverged:** password rendered as an inline `<form>`; no progressive disclosure.
- **Changed:** form-free. Gold scan-face badge, "Unlock to continue" + "<NAME> is
  protected — unlock it in your browser to sign" subtitle (new `secretName` prop).
  "Unlock with passkey" (`variant="brand"`) + caption; "Use password instead"
  reveal link → password field + Unlock; mint factor-safety note; Cancel.

### C. Create dialog (`vyed5`) — `vault-secret-form.tsx`
- **Diverged:** Kind as two big cards; field order had Value before Group with
  Description/Tags/Hosts as loose siblings; no Advanced grouping; no always-ask;
  primary button "Save"; protected setup/unlock embedded as a coequal section.
- **Changed:** Kind is now a 2-segment `SegmentedRadio` (reused primitive). Field
  order Kind → Name → Value → Protection → Group → Advanced. Name hint added.
  Value placeholder "Paste the secret value". Protection cards use exact frame copy
  and the selected card shows a mint check. Group uses the extended `Combobox`
  (folder icons + design create row). New **Advanced** collapsible holds
  Description, Tags, Allowed hosts, and the **"Always ask before each use"** toggle.
  Primary button "Create secret". Protected setup/unlock now renders as a gating
  step (not a nested form, not coequal) and "Create secret" stays disabled until
  unlocked.

### D. Approval cards (`w8u5l` access, `eWOjt` sign) — `vault-approval-card.tsx`
- **Diverged:** access header used a cyan key icon; sign used a wallet; detail rows
  were a bordered sub-panel with uppercase labels; scope used a check-circle;
  Approve/Sign used a shield icon.
- **Changed:** access = gold `LockKeyhole`, sign = violet `PenTool`. Details are a
  borderless list with sentence-case fixed-width labels and top/bottom dividers;
  command is a plain mono chip; egress uses `Cpu`. Scope options use a radio
  indicator (mint ring + dot) with the selected label bold. Approve = `Check`,
  Sign = `PenTool`. Protected gating shows the unlock panel while locked, then the
  mint operation note once unlocked (no duplicate note).

### E. Hub (`y4rw5Q`), Provision (`F4N19`), Group dropdown (`WgGQy`)
- **Hub — `VaultsPage.tsx`:** subtitle → "Secrets your agents use — names only,
  values never shown". Active grants are now compact dismissible **chips** (icon ·
  `type · ref` · live countdown · ×) instead of full cards. Group headers gained a
  folder icon and a smarter count ("N secrets" / "N keys"). Secret rows gained an
  `always-ask` badge and a `proxy · <host>` badge. Out of scope (net-new features,
  not restyles): the search field, the "By skill" tab, and per-row ⋯ menus — these
  frames depict capabilities that don't exist yet and were intentionally not built.
- **Provision — `vault-secret-form.tsx` (`fixedName`) + `secret-request-card.tsx`:**
  the `fixedName` mode now matches `SecureInputCard`: a cyan key header ("Agent
  needs a secret"), a cyan name-highlight box (asterisk + "Secret name" + name +
  "not set yet" badge), Value + eye + "Submitted over TLS…" help, a "Store as"
  segmented Standard|Protected, and a "Save & wake agent" button. Same submit path.
- **Group dropdown — extended `Combobox`:** added optional `withFolderIcon` (folder
  icon per option + on the trigger; mint-soft selected row with check) and
  `createButtonLabel` (a bordered "+ <typed>" input with a green Create button).
  Both are opt-in, so every other `Combobox` caller is unchanged.

### Flags for owner review
1. **Description + Tags** exist in the impl but are absent from the `vyed5` mock.
   Per the spec they were **not deleted** — they're folded under the new Advanced
   collapsible and stay fully functional. Confirm this placement is acceptable.
2. **"Always ask before each use"** maps to a **real** backend field:
   `policy.always_ask` (storage/vault_service.py `_secret_access_grantable` returns
   `False` when set → the secret is never granted in advance, even per-key), which
   matches the mock's help text exactly. It is wired live (static secrets only;
   keypairs are already never value-grantable, so the toggle is disabled for them).
   Not a dead control.
