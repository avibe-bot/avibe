# Desktop Welcome height adaptation

Approved option C (Max, 2026-11-27). Initial base: `21e33fd03585fe03797db04e5d758cb541a874e8`.
Integrated master: `e0d3854921a467321717f47fc4b2153c284f95d5`.
Reference: local design head `105a857251`, `desktop/welcome-spacing/c-*`.

Only the compact Welcome and its outer Desktop column change. Text, elements,
order, colors, non-compact mobile Welcome, and subsequent sign-in steps remain
unchanged. Keep the action column 384px wide and the subtitle on one line.

- Below 780px: 128px hero, 112px mascot, 24px title; 20px hero/title,
  8px title/subtitle, 24px subtitle/actions, 12px action gaps, 16px local-exit gap;
  no column vertical padding. Content must fit the native 880×600 window's
  486px available height after 94px chrome and 20px bottom padding.
- 780–899px: 192px hero, 160px mascot, 30px title; 32px hero/title,
  12px title/subtitle, 40px subtitle/actions, 16px action gaps, 32px local-exit gap.
- From 900px: 256px hero, 208px mascot, 36px title; 40px hero/title,
  16px title/subtitle, 48px subtitle/actions, 20px action gaps, 40px local-exit gap.

Two height variants in index.css are only consumed by compact Welcome classes.
Shipped desktop/sign-in/start frames include the native header and render the
real component. The unmerged proposal kit is not copied into production.

Validation: related sign-in tests, types, canonical lint, UI/Desktop builds,
production Chromium/WebKit captures at four required windows and 779/780/899/900
boundaries, English/Chinese and light/dark. Compare iOS and other-step pixels
against the same base build. No installed App acceptance, package or installation.
PR authorization ends at Merge-ready; main owns merge and review requests.

## Local results

255 related tests, test/design/source typechecks, lint and both frontend builds
passed. Chromium and WebKit each verified all eight required sizes/boundaries in
both languages and themes, using production AccountApp assets with a fake bridge
and separately production design assets (64 cases per surface). Content measured
484/655/778px across the three tiers; no scroll overflow or clipping. English
subtitle measured about 430px at the 880px minimum window. Sixteen iOS and twelve
later-step before/after pixel comparisons matched exactly with settled 2× captures.
Evidence and executable probes: the sign-in matter's `welcome-spacing/impl/`.
Installed App acceptance remains outstanding.
