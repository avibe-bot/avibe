import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it } from 'vitest';
import { I18nextProvider } from 'react-i18next';

import i18n from '@/i18n';
import { AdoptionNote } from './AdoptionNote';

beforeEach(async () => { await i18n.changeLanguage('en'); });

const render = (addedTo: React.ComponentProps<typeof AdoptionNote>['addedTo']) => renderToStaticMarkup(
  <I18nextProvider i18n={i18n}>
    <AdoptionNote addedTo={addedTo} adoptedBy={addedTo?.map(({ backend, menu_model }) => ({ backend, menu_model })) ?? null} />
  </I18nextProvider>,
);

describe('AdoptionNote', () => {
  it('renders exact add-time Route locations', () => {
    const html = render([{ backend: 'claude', menu_model: 'claude-opus-4-6', source_id: 'src_a', model_id: 'claude-opus-4-6', position: 2 }]);
    expect(html).toContain('Claude Code');
    expect(html).toContain('claude-opus-4-6');
  });

  it('states that an unmatched source remains available for explicit routing', () => {
    expect(render([])).toContain('route');
  });
});
