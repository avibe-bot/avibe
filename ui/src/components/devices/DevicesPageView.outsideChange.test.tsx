/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '../../i18n';
import { DevicesPageView } from './DevicesPageView';
import { said } from './testText';
import type { Device, Reach } from './types';

// The notice about an address changed elsewhere, once 「更改域名」 is turned on (features.ts) and the
// instance has binding again: the full sentence and the way to bind again come back.
vi.mock('./features', async (original) => ({ ...await original<typeof import('./features')>(), RENAME_ENTRY_ENABLED: true }));

const host = 'max-app.avibe.bot';
const on: Reach = { state: 'on', host, tunnel: 'up', ms: 120 };
const macbook: Device = { id: 'host', group: 'mine', kind: 'host', name: 'MacBook', client: { kind: 'runtime' }, self: true, functions: [] };

beforeEach(async () => { await i18n.changeLanguage('zh'); });
afterEach(cleanup);

describe('DevicesPageView · an address changed elsewhere, with renaming and binding again in the release', () => {
  it('says the full sentence and offers binding again', () => {
    const onOpenRebind = vi.fn();
    render(<DevicesPageView reach={on} devices={[macbook]} unrecognizedChange={{ hostname: 'sam-app.avibe.bot' }} rebindOffered onOpenRebind={onOpenRebind} />);
    expect(screen.getByText(said('请先重新绑定远程访问，再在「更改域名」里把原来的名字拿回来。'))).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '去重新绑定' }));
    expect(onOpenRebind).toHaveBeenCalled();
  });

  it('keeps the support way out where the instance has no binding again', () => {
    render(<DevicesPageView reach={on} devices={[macbook]} unrecognizedChange={{ hostname: 'sam-app.avibe.bot' }} rebindOffered={false} />);
    expect(screen.getByRole('link', { name: 'support@avibe.bot' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '去重新绑定' })).toBeNull();
  });
});
