// The page object's attribute selectors, against a value the operator chose.
//
// The sibling invariants guard forbids building one of these by interpolation;
// what it cannot do is notice that the helper it points at is wrong. So this
// drives the real `ModelHubPage` accessors over a document of its own, with an
// id carrying the three things a quoted CSS string cannot hold raw — a `"`, a
// `\`, and a control character. Unescaped, the first one alone makes
// `querySelectorAll` throw `is not a valid selector`.
//
// The id is not invented: `ModelHubMenuConfig.checked` takes arbitrary
// non-credential strings, so a route row's `data-route-model` is whatever a
// backend's menu was ticked with.
//
// No instance is contacted — `setContent` is the whole fixture — so this runs
// wherever a browser does.
import { expect, test } from '@playwright/test';

import { ModelHubPage } from './support/hub';

const OPERATOR_CHOSEN = 'vendor/"quoted"\\back\\slash\ttab:1';

test('an attribute selector is escaped, not interpolated', async ({ page }) => {
  await page.setContent(
    '<div data-source-id="plain">PLAIN</div>'
    + `<div data-source-id="${OPERATOR_CHOSEN.replace(/&/g, '&amp;').replace(/"/g, '&quot;')}">CHOSEN</div>`,
  );
  const hub = new ModelHubPage(page);

  // Exactly the one row, and the RIGHT one: an escape that merely stopped the
  // throw could still match both, or the wrong sibling.
  await expect(hub.sourceRow(OPERATOR_CHOSEN)).toHaveCount(1);
  await expect(hub.sourceRow(OPERATOR_CHOSEN)).toHaveText('CHOSEN');
  await expect(hub.sourceRow('plain')).toHaveText('PLAIN');
  await expect(hub.sourceRow('no-such-source')).toHaveCount(0);
});
