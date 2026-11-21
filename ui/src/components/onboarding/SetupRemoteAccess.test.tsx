/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '../../i18n';
import { DevicesApiError, devicesApi, type ClaimForm } from '../devices/api';
import { readClaimMarker, writeClaimMarker } from '../devices/claimFlow';
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

  it('cannot be skipped past a request that is on its way, and a request left behind never leaves for avibe.bot', async () => {
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => undefined);
    let answer!: (form: ClaimForm) => void;
    vi.spyOn(devicesApi, 'startClaim').mockReturnValue(new Promise((resolve) => { answer = resolve; }));
    const cancel = vi.spyOn(devicesApi, 'cancelClaim').mockResolvedValue();
    const { unmount } = mount();

    fireEvent.click(screen.getByRole('button', { name: /开启远程访问/ }));
    await waitFor(() => expect((screen.getByRole('button', { name: '以后再说，进入工作台' }) as HTMLButtonElement).disabled).toBe(true));
    // The page goes away some other way (the back button) before the answer arrives.
    unmount();
    answer({ claimId: 'claim-late', action: 'https://avibe.bot/api/v1/claims/redirect-entry', method: 'POST', fields: { user_code: 'CODE' }, referrerPolicy: 'strict-origin' });
    await waitFor(() => expect(cancel).toHaveBeenCalledWith('claim-late'));
    expect(submit).not.toHaveBeenCalled();
    expect(readClaimMarker()).toBeNull();
  });

  it.each(['pending', 'waiting', 'approved'])('hands a claim this tab left open (%s) to the Devices page, and never holds the step', async (state) => {
    // `waiting`: the instance could not reach avibe.bot yet and keeps trying.
    writeClaimMarker({ kind: 'claim', id: 'claim-1', at: Date.now() });
    vi.spyOn(devicesApi, 'claim').mockResolvedValue({ state });
    mount();
    expect(await screen.findByText('devices')).toBeTruthy();
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
