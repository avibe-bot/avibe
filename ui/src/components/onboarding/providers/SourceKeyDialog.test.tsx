// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { Source } from '@/components/settings/models/types';
import { pressOutside } from '@/lib/testing/pressOutside';
import { SourceKeyDialog } from './SourceKeyDialog';

afterEach(cleanup);

const source: Source = {
  id: 'src_relay', vendor: 'custom', display_name: 'Relay', kind: 'api_key', protocol: 'openai_chat',
  supply_channel: 'hub', billing: 'metered', state: { status: 'active' }, models: [], last_discovered_at: null,
};

describe('SourceKeyDialog', () => {
  it('stays open on a click outside, keeping the key being typed', async () => {
    const onClose = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <SourceKeyDialog source={source} label="Relay" check={undefined} writable
          onReplaced={vi.fn()} onChanged={vi.fn()} onRecheck={vi.fn()} onClose={onClose} />
      </I18nextProvider>,
    );
    await userEvent.type(screen.getByLabelText('New API Key'), 'sk-new-key');
    await pressOutside();
    expect(onClose).not.toHaveBeenCalled();
    expect((screen.getByLabelText('New API Key') as HTMLInputElement).value).toBe('sk-new-key');
  });
});
