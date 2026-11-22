# Microphone input selector sizing

The Desktop/Web microphone input trigger sizes to its contents, aligns left, and stays within its container with long names truncated. Pressing the trigger preserves its size while keeping hover and popover behavior. The test action sits below its status and shares the input selector’s left edge on Desktop/Web.

Scope: the shipped trigger, real-component design fixtures, and browser regression coverage. Device enumeration, permissions, capture testing, and iOS behavior are unchanged.

Validation: compare before/after design frames at the same dimensions in light/dark and English/Chinese, plus selected short/long names and a narrow desktop container. Browser tests measure intrinsic width, containment, truncation, held-pointer geometry and test-action alignment; existing microphone unit and interaction tests retain behavioral coverage. Run the UI build and design typecheck.

Owner review: send screenshots to main and wait for Max's confirmation before opening an English PR. No packaging or installation.

## Evidence

- Microphone Vitest: 6 passed.
- Microphone Playwright: 6 passed (English/Chinese, light/dark, short/long names, narrow containment, held-pointer dimensions and existing capture lifecycle).
- The new regression fails on the original component because the trigger occupies all 834 px of its container.
- UI build and design typecheck passed. Design captures: 24 images, no console errors or unfixtured requests; before/after at 1280 × 800 and a long-device frame at 720 × 800.
- The app-route development tests log a pre-existing React lazyPage use() warning, also seen with the original component; all interactions pass. The design-frame captures are clean.

## Test action alignment follow-up

The owner requested a left-aligned test action on November 22. Desktop/Web now stacks the status and action in their existing order; the action shares the input selector’s left edge. iOS retains its existing layout. Capture, permission and selection behavior are unchanged.

Validation: 6 microphone unit tests and 6 browser tests passed, including the new action-position assertion in both languages and themes. The assertion fails on the preceding layout (action x=1069 versus selector x=347). UI build and design typecheck passed. Another 24 real-component captures cover both themes/languages, short and long names, narrow containment, expanded picker and active testing. Await owner screenshot approval; do not push or open a PR.
