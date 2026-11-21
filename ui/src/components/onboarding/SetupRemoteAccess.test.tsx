/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '../../i18n';
import { DevicesApiError, devicesApi } from '../devices/api';
import { readClaimMarker } from '../devices/claimFlow';
import { said } from '../devices/testText';
import { SetupRemoteAccess } from './SetupRemoteAccess';
import { offersRemoteAccessStep } from './setupRemoteAccessStep';

// ui/design: setup/remote-access/*
vi.mock('../Wizard', () => ({ SetupHeader: () => null }));

const mount = () => render(
  <MemoryRouter initialEntries={['/setup/remote-access']}>
    <Routes>
      <Route path="/setup/remote-access" element={<SetupRemoteAccess />} />
      <Route path="/" element={<div>workbench</div>} />
      <Route path="/settings/remote-access" element={<div>devices</div>} />
    </Routes>
  </MemoryRouter>,
);

beforeEach(async () => {
  await i18n.changeLanguage('zh');
  window.sessionStorage.clear();
  vi.spyOn(devicesApi, 'status').mockResolvedValue({ ok: true, enabled: false, paired: false, running: false } as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('Setup · remote access', () => {
  it('is offered only where turning it on can work from here', () => {
    const local = { remote: false, desktopShell: false, nativeShell: false, paired: false } as const;
    expect(offersRemoteAccessStep(local)).toBe(true);
    expect(offersRemoteAccessStep({ ...local, paired: true })).toBe(false);
    // A status that could not be read offers nothing.
    expect(offersRemoteAccessStep({ ...local, paired: null })).toBe(false);
    expect(offersRemoteAccessStep({ ...local, remote: true })).toBe(false);
    expect(offersRemoteAccessStep({ ...local, desktopShell: true })).toBe(false);
    expect(offersRemoteAccessStep({ ...local, nativeShell: true })).toBe(false);
  });

  it('turns on with the same claim as the Devices page, and skipping is a link to the workbench', async () => {
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => undefined);
    vi.spyOn(devicesApi, 'startClaim').mockResolvedValue({ claimId: 'claim-1', action: 'https://avibe.bot/api/v1/claims/redirect-entry', method: 'POST', fields: { user_code: 'CODE', redirect_state: 'STATE' }, referrerPolicy: 'strict-origin' });
    vi.spyOn(devicesApi, 'claim').mockResolvedValue({ state: 'pending' });
    mount();

    expect(screen.queryByRole('textbox')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '开启远程访问' }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(readClaimMarker()).toMatchObject({ kind: 'claim', id: 'claim-1' });
  });

  it('skips to the workbench', async () => {
    mount();
    fireEvent.click(screen.getByRole('button', { name: '以后再说，进入工作台' }));
    expect(await screen.findByText('workbench')).toBeTruthy();
  });

  it('says in one sentence when this address cannot turn it on, and then only goes on', async () => {
    vi.spyOn(devicesApi, 'startClaim').mockRejectedValue(new DevicesApiError(409, 'link_required', { state: 'error', code: 'link_required' }));
    mount();
    fireEvent.click(screen.getByRole('button', { name: '开启远程访问' }));
    expect(await screen.findByText(said('从这个地址开不了远程访问。'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: '开启远程访问' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '进入工作台' }));
    expect(await screen.findByText('workbench')).toBeTruthy();
  });

  it('has nothing to offer on an instance that is already bound', async () => {
    vi.spyOn(devicesApi, 'status').mockResolvedValue({ ok: true, enabled: true, paired: true, running: true } as never);
    mount();
    expect(await screen.findByText('devices')).toBeTruthy();
  });
});
