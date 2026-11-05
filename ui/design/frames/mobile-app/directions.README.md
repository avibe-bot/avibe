# Mobile onboarding: direction selection

Owner decision, 2026-11-05 20:14; amended at 20:17 and 20:20. Visual expression
may go beyond shipped page compositions while retaining Avibe's brand and tokens.
This is a direction-selection checkpoint, not a merge-ready delivery. The owner
explicitly requested screenshots and design typecheck before selection, with the
complete state/English matrix and full PR gates after a direction is selected.

## Shared contract

All studies preserve email OTP authentication and the existing instance information
model. Welcome → email → code → instance picker. No third-party login. Email is
always fully visible: no masking, ellipsis, truncation or concealed substring.
Long email text wraps (`break-all`); the earlier `welcome/code-small` fixture also
already wraps its full address. Six equal OTP cells form one continuous row with
uniform gaps, without a dash or 3–3 grouping. The reference image itself was not
available; its textual description and the latest owner corrections are the source.

At 390px, six 64–72px squares plus gaps cannot fit. The display therefore uses six
equal responsive squares across the content width (about 50px at this viewport),
with large rounded corners and 24px digits. It keeps the requested one-row shape.

The resend action reads “没收到验证码？重新发送”, with an underlined text action.
After selection, success/failure resend cooldowns show “xx 秒后可重新发送” (60/15s).
The full email remains visible above the code display with a Change email action.

## Three directions

| Direction | Idea | Composition | Components / tokens |
| --- | --- | --- | --- |
| A · Cloud letter / 云间来信 | Make the first connection feel like a friendly invitation from Vibey. | Large mascot, soft mint/cyan/violet atmosphere, rounded floating surfaces, centered verification. | New `DirectionShell`, rounded form/instance compositions; existing mascot assets, `--gradient-wizard-platforms`, `shadow-mint-card`, `rounded-3xl`. |
| B · Pocket workbench / 随身工作台 | Give connection energy through strong type and focused contrast. | Large left-aligned headings, mint statement panel, luminous active code cell, compact strong instance cards. | Shared direction kit with bold hierarchy; existing `mint-soft`, `glow-sm-mint`, brand Button and type scale. |
| C · Quiet entrance / 安静的入口 | Extend the reference's lightness and space through the whole journey. | Small mascot, generous space, pale mint OTP surface, light field shadows, separated list rows. | Shared direction kit with sparse composition; existing `surface`, `mint-soft`, `shadow-sm`, Input/Label/Button/Badge. |

No new tokens, dependencies, product components or native code. All new material
lives in `ui/design/kit/mobile-directions.tsx`. Gradients, shadows, colors, spacing,
type and radii use existing tokens; `src/index.css` is unchanged. Motion is not
implemented or claimed in static frames. Once a direction is selected, any motion
proposal must include reduced-motion behavior and its real-device validation.

## Frames and selection evidence

`mobile-app/directions/{a,b,c}-{welcome,email,code,instances}`: twelve `proposed`
frames, Chinese, 390 × 844, each light/dark (24 captures). Each OTP frame shows
input in progress: two filled cells and a highlighted third cell with a caret.
Show Page compares A/B/C side by side for the same page and theme. The prior
alignment mapping and complete flow frames remain secondary references.

After selection: cover OTP empty, input in progress, full, verifying, invalid,
expired, rate limited, network failure and success; send/resend loading and
60/15-second countdowns; email validation; small/long-email layouts; then extend
no-instance, account, connection-error and new-window screens and English copy.

## Input and accessibility handoff

The six cells are decorative (`aria-hidden`). A single visually hidden but
accessible Input carries an associated label, `type="text"`,
`autocomplete="one-time-code"`, `inputMode="numeric"`, `pattern="[0-9]{6}"`,
`maxLength=6`, and the explanatory text via `aria-describedby`. Never use six
independent focus stops. The fixture drives the visual cells in this proposal;
it is not an interactive OTP implementation.

Implementation must synchronize input/paste/autofill into that single field,
normalize digits, update the displayed characters/caret, make tapping any cell
focus the input, provide a visible focus treatment, expose errors and pending
state accessibly, and retain normal keyboard/screen-reader navigation. iOS OTP
autofill, paste, keyboard, focus, text scaling and actual authentication require
real-device validation. Error cells may become destructive-colored; any shake
must be brief, optional under reduced motion, and never the only error signal.

## Checkpoint validation

Design typecheck, UI production build and lint baseline pass. All 24 direction
captures render with zero console errors or unfixtured reads and have been
visually inspected in both themes. The final A welcome capture uses the existing
high-resolution `assets/mascot/cloud-tuanzi-v2.png` instead of enlarging the small
runtime sprite. No new artwork is generated.

Show Page was checked at 1440px and 390px without horizontal overflow. All four
page selectors show three matching directions; each code frame exposes exactly
one labeled input, six decorative cells and the complete fixture email. The
pre-existing Show Runtime annotation import warning remains separate from the
error-free standalone design capture; the page and frames render correctly.
