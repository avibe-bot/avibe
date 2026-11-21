// Settings · Devices · 连接方式, wired: the connection settings, the domain and its rename, and
// binding remote access again. Every change is sent once and its result is read from the
// instance's own state: a lost answer is unknown, never a failure, and never sent again. Routes:
// `/settings/remote-access/connection` and `…/connection/address` (the rename, reopenable).
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';

import type { RemoteAccessStatus, TunnelConnectivityDiagnostics, TunnelNetworkInterface } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { hasDesktopClient } from '@/desktop/bridge';
import { liveStreamVia, subscribeLiveStreamVia } from '@/lib/liveStreamVia';
import {
  nativeDirectChoiceAvailable, nativeDirectPathReported, onNativeDirectChoiceChanged, readNativeDirectChoice, setNativeDirectChoice,
  type NativeDirectChoice,
} from '@/lib/native';
import { onPageReactivated } from '@/lib/pageActivity';
import { openLinkInNewContext } from '@/lib/pwaNavigation';
import { deviceUsing, directCertificateDate, lastConnectionLegs } from '@/lib/remoteAccessDirect';
import { useNativeDirectPath } from '../remoteAccess/useNativeDirectPath';
import { DevicesApiError, LostAnswer, devicesApi as api, type RenamePreview } from './api';
import { clearClaimMarker } from './claimFlow';
import { ConnectionPageView } from './ConnectionPageView';
import { CONNECTION_ADDRESS_PATH, CONNECTION_PATH, CONNECTION_REBIND_HASH, DEVICES_PATH } from './paths';
import { renameDevices } from './rules';
import type {
  AddressState, ConnectionSetting, ConnectionState, Device, DirectPreference, RebindDialog, RebindRefusal, RebindState,
  RenameRefusal, RenameStep, RetiredName, SwitchRefusal, TunnelChange, TunnelSetting,
} from './types';
import { useClaimFlow, type ClaimEnd } from './useClaimFlow';
import { useOptimizeRoute } from './useOptimizeRoute';
import { rebindRefusal, renameRefusal, tunnelFromStatus } from './wire';

const REFRESH_MS = 30_000;
const FOLLOW_MS = 2_000;
/** The second question's button waits this long: a rename cannot be undone. */
const RENAME_CONFIRM_WAIT = 5;
/** Reads without a trace of a change whose answer was lost, before the page says so. */
const LOST_CHANGE_READS = 3;
const TUNNEL_SETTINGS: readonly ConnectionSetting[] = ['protocol', 'ipFamily', 'outbound'];
const isTunnelSetting = (setting: ConnectionSetting): setting is TunnelSetting => TUNNEL_SETTINGS.includes(setting);
const RUNNING = new Set(['applying', 'verifying', 'restoring', 'unknown']);

const storedValue = (connection: ConnectionState, setting: TunnelSetting): string => (
  setting === 'protocol' ? connection.settings.transport_protocol
    : setting === 'ipFamily' ? connection.settings.edge_ip_version : connection.settings.edge_bind_address
);

const preferenceOf = (choice: NativeDirectChoice): DirectPreference => ({
  mode: choice.mode,
  reverted: choice.revertedAt === null ? null : { at: new Date(choice.revertedAt).toISOString(), reason: 'unreachable' },
});

type Preview = RenamePreview & { name: string };

export function ConnectionPage() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { remote, capabilities } = useInstanceAuthorization();
  // How this Avibe is reached is the owner's: the instance answers nobody else on these routes.
  const owner = capabilities.is_instance_owner;
  const alive = useRef(true);
  const go = useCallback((to: string, replace = false) => navigate(to, { state: location.state, replace }), [location.state, navigate]);

  // ── the connection settings ──
  const [load, setLoad] = useState<'loading' | 'ready' | 'unavailable'>('loading');
  const [connection, setConnection] = useState<ConnectionState | null>(null);
  const [status, setStatus] = useState<RemoteAccessStatus | null>(null);
  const [statusAt, setStatusAt] = useState(() => Date.now());
  const [localChange, setLocalChange] = useState<TunnelChange | undefined>();
  const [refused, setRefused] = useState<SwitchRefusal | undefined>();
  const [sending, setSending] = useState<ConnectionSetting | undefined>();
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [diagnostics, setDiagnostics] = useState<TunnelConnectivityDiagnostics | null>(null);
  const [diagnosing, setDiagnosing] = useState(false);
  const [diagnosticsError, setDiagnosticsError] = useState<string | undefined>();
  const interfaces = useRef<TunnelNetworkInterface[]>([]);
  /** A tunnel change whose answer never arrived: what was asked, and how often the state was read since. */
  const lostSetting = useRef<{ setting: TunnelSetting; to: string; reads: number } | null>(null);

  const readConnection = useCallback(async () => {
    try {
      if (interfaces.current.length === 0) interfaces.current = await api.interfaces().catch(() => []);
      const next = await api.connection(interfaces.current);
      if (!alive.current) return;
      if (next) {
        setConnection(next);
        setLoad('ready');
        const lost = lostSetting.current;
        if (!lost) {
          // The instance's own word replaces what this page assumed while it waited.
          setLocalChange((current) => (current && current.state !== 'failed' ? undefined : current));
        } else if (next.change?.setting === lost.setting && (next.change.to === undefined || next.change.to === lost.to)) {
          // The change whose answer was lost is the one the instance reports: its word, not ours.
          lostSetting.current = null;
          setLocalChange(undefined);
        } else if (storedValue(next, lost.setting) === lost.to) {
          lostSetting.current = null;
          setLocalChange(undefined);
        } else {
          // No trace of it yet. A few reads decide that it never arrived; until then it is unknown.
          lost.reads += 1;
          if (lost.reads >= LOST_CHANGE_READS) {
            lostSetting.current = null;
            setLocalChange({ setting: lost.setting, state: 'failed', from: storedValue(next, lost.setting), reason: 'unknown' });
          }
        }
      } else {
        setLoad('unavailable');
      }
    } catch (error) {
      if (!alive.current) return;
      // A lost answer says nothing: what is on the page stays, and the next read decides.
      if (error instanceof LostAnswer) setLoad((current) => (current === 'loading' ? 'unavailable' : current));
      else setLoad('unavailable');
    }
  }, []);

  const readStatus = useCallback(async () => {
    try {
      const next = await api.status();
      if (alive.current && typeof next.paired === 'boolean') {
        setStatus(next);
        setStatusAt(Date.now());
      }
    } catch {
      // The last status stays; its age is what the page shows it by.
    }
  }, []);

  // ── the domain, and binding again ──
  const [address, setAddress] = useState<AddressState | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [step, setStep] = useState<RenameStep | undefined>();
  const preview = useRef<Preview | null>(null);
  const lostChange = useRef<{ reads: number } | null>(null);
  const renameFrom = useRef<string | null>(null);
  const [rebind, setRebind] = useState<RebindState | undefined>();
  const [rebindRefused, setRebindRefused] = useState<RebindRefusal | undefined>();
  const [rebindDialog, setRebindDialog] = useState<RebindDialog | undefined>();
  const [rebindBusy, setRebindBusy] = useState(false);
  const resetConfirmation = useRef<string | null>(null);

  const readAddress = useCallback(async (): Promise<AddressState | null> => {
    try {
      const next = await api.address();
      if (alive.current) setAddress(next);
      return next;
    } catch {
      return null;
    }
  }, []);

  const readRebind = useCallback(async (): Promise<RebindState> => {
    let next: RebindState;
    if (remote) next = { state: 'remote' };
    else {
      try {
        next = await api.rebind();
      } catch (error) {
        const code = error instanceof DevicesApiError ? error.code : '';
        next = code === 'direct_loopback_required' || code === 'local_operator_required' ? { state: 'remote' } : { state: 'unknown' };
      }
    }
    if (alive.current) setRebind(next);
    return next;
  }, [remote]);

  const onRebindEnd = useCallback((end: ClaimEnd) => {
    void (async () => {
      const [next] = await Promise.all([readRebind(), readAddress(), readStatus()]);
      if (!alive.current) return;
      if (end.state === 'bound') {
        clearClaimMarker();
        setRebindRefused(undefined);
        setRebindDialog({ kind: 'bound', disposition: end.claim.disposition ?? 'unknown', host: end.claim.hostname });
        return;
      }
      // Only its starter may confirm a reset: the marker stays while that is offered.
      if (next.state !== 'asNewOffered') clearClaimMarker();
      // A claim this instance no longer knows says nothing; the overview speaks for itself.
      if (end.code === 'claim_unavailable' || end.code === 'installation_unverified') setRebindRefused(undefined);
      else setRebindRefused(rebindRefusal(end.code === 'no_answer' ? undefined : end.code, end.seconds));
    })();
  }, [readAddress, readRebind, readStatus]);
  const flow = useClaimFlow({ kind: 'rebind', locale: i18n.language, onEnd: onRebindEnd });

  // ── the Avibe App's own path and choice ──
  const pageVia = useSyncExternalStore(subscribeLiveStreamVia, liveStreamVia, () => null);
  const nativePath = useNativeDirectPath(pageVia);
  const [preference, setPreference] = useState<DirectPreference | undefined>();
  useEffect(() => {
    if (!nativeDirectChoiceAvailable()) return undefined;
    let current = true;
    void readNativeDirectChoice().then((choice) => { if (current && choice) setPreference(preferenceOf(choice)); });
    const stop = onNativeDirectChoiceChanged((choice) => setPreference(preferenceOf(choice)));
    return () => {
      current = false;
      stop();
    };
  }, []);

  useEffect(() => {
    if (!owner) navigate(DEVICES_PATH, { replace: true, state: location.state });
  }, [location.state, navigate, owner]);

  useEffect(() => {
    alive.current = true;
    if (!owner) return () => { alive.current = false; };
    queueMicrotask(() => {
      void readConnection();
      void readStatus();
      void readAddress();
      void readRebind();
      void api.devices().then((snapshot) => { if (alive.current) setDevices(snapshot.devices); }).catch(() => undefined);
    });
    const refresh = () => {
      if (document.visibilityState !== 'visible') return;
      void readConnection();
      void readStatus();
      void readRebind();
    };
    const timer = window.setInterval(refresh, REFRESH_MS);
    const stop = onPageReactivated(refresh);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
      stop();
    };
  }, [owner, readAddress, readConnection, readRebind, readStatus]);

  const onStatus = useCallback((next: RemoteAccessStatus) => {
    setStatus(next);
    setStatusAt(Date.now());
  }, []);
  const { optimize, recovering, run: optimizeRoute } = useOptimizeRoute({ status, statusAt, onStatus, refresh: readStatus });

  // While a change is running (or its answer was lost), only reads: the state says how it ended.
  const change = localChange ?? connection?.change;
  const following = Boolean(change && RUNNING.has(change.state)) || recovering;
  useEffect(() => {
    if (!following) return undefined;
    const timer = window.setInterval(() => { void readConnection(); void readStatus(); }, FOLLOW_MS);
    return () => window.clearInterval(timer);
  }, [following, readConnection, readStatus]);

  // A rename that avibe.bot has not answered yet is followed the same way. `renameFrom` is the
  // name it had when the change was first seen, so the list afterwards is the right name's.
  const operation = address?.operation;
  const renameRunning = operation?.state === 'pending' || operation?.state === 'applying' || step?.step === 'pending';
  const currentHost = address?.hostname;
  useEffect(() => {
    if (renameRunning && currentHost && !renameFrom.current) renameFrom.current = currentHost;
  }, [currentHost, renameRunning]);
  useEffect(() => {
    if (!renameRunning) return undefined;
    const timer = window.setInterval(() => {
      void readAddress().then((next) => {
        if (!alive.current || !next) return;
        if (next.operation?.state === 'pending' || next.operation?.state === 'applying') return;
        const lost = lostChange.current;
        const previous = renameFrom.current;
        const changed = Boolean(previous) && next.hostname !== previous;
        if (lost && !changed && !next.operation) {
          // No trace of the change yet: a few reads decide that it never arrived.
          lost.reads += 1;
          if (lost.reads < LOST_CHANGE_READS) return;
        }
        lostChange.current = null;
        renameFrom.current = null;
        const name = preview.current?.name ?? '';
        if (changed && previous) {
          setStep({ step: 'done', hostname: next.hostname, previous, retired: next.retired.find((each) => each.hostname === previous) ?? null });
        } else if (next.operation?.state === 'failed') {
          setStep({ step: 'form', name, refusal: next.operation.error ?? { code: 'unknown', raw: 'unknown' } });
        } else {
          setStep({ step: 'form', name, refusal: { code: 'unknown', raw: lost ? 'no_answer' : 'unknown' } });
        }
      });
    }, FOLLOW_MS);
    return () => window.clearInterval(timer);
  }, [readAddress, renameRunning]);

  // The confirm button's wait.
  const countdown = step?.step === 'confirm' ? step.countdown : 0;
  useEffect(() => {
    if (countdown <= 0) return undefined;
    const timer = window.setTimeout(() => {
      setStep((current) => (current?.step === 'confirm' ? { ...current, countdown: current.countdown - 1 } : current));
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [countdown]);

  // A link to the bind-again row lands on it.
  const rebindShown = Boolean(rebind);
  useEffect(() => {
    if (rebindShown && location.hash === CONNECTION_REBIND_HASH) document.getElementById('rebind')?.scrollIntoView({ block: 'center' });
  }, [location.hash, rebindShown]);

  const changeSetting = (setting: ConnectionSetting, to: string | boolean) => {
    if (!connection) return;
    setRefused(undefined);
    const tunnelSetting = isTunnelSetting(setting) ? setting : null;
    const from = tunnelSetting ? storedValue(connection, tunnelSetting) : '';
    if (tunnelSetting) setLocalChange({ setting: tunnelSetting, state: 'applying' });
    else setSending(setting);
    void (async () => {
      try {
        await api.changeConnection(setting, to);
        if (tunnelSetting && alive.current) setLocalChange(undefined);
      } catch (error) {
        if (!alive.current) return;
        if (error instanceof LostAnswer) {
          // Unknown, not failed: the result is read again, the change is never sent twice.
          if (tunnelSetting) {
            lostSetting.current = { setting: tunnelSetting, to: String(to), reads: 0 };
            setLocalChange({ setting: tunnelSetting, state: 'unknown' });
          }
        } else if (tunnelSetting) {
          const busy = error instanceof DevicesApiError && error.code === 'connection_change_busy';
          setLocalChange({ setting: tunnelSetting, state: 'failed', from, reason: busy ? 'connection_change_busy' : 'unknown' });
        } else if (setting === 'direct' || setting === 'relay') {
          const code = error instanceof DevicesApiError ? error.code : '';
          // Why the relay cannot be turned on is the state's own line after the next read.
          if (code !== 'direct_permission_required' && code !== 'relay_transport_unavailable' && code !== 'relay_not_enrolled') {
            setRefused({ row: setting, reason: code === 'tunnel_unhealthy' || code === 'connection_change_busy' ? code : 'unknown' });
          }
        }
      } finally {
        if (alive.current) setSending(undefined);
        await readConnection();
      }
    })();
  };

  const runDiagnostics = () => {
    setDiagnosing(true);
    setDiagnosticsError(undefined);
    void (async () => {
      try {
        const result = await api.diagnose();
        if (!alive.current) return;
        if (result?.ok === false) setDiagnosticsError(t('errors.remote_access_diagnostics_failed'));
        else setDiagnostics(result);
      } catch {
        if (alive.current) setDiagnosticsError(t('errors.remote_access_diagnostics_failed'));
      } finally {
        if (alive.current) setDiagnosing(false);
      }
    })();
  };

  // ── rename ──
  const addressOpen = location.pathname.replace(/\/+$/, '') === CONNECTION_ADDRESS_PATH;
  const rename = useMemo<RenameStep | undefined>(() => {
    if (step?.step === 'cleanup' || step?.step === 'done') return step;
    if (operation && operation.state !== 'failed') return { step: 'pending', hostname: operation.hostname };
    if (step) return step;
    if (!addressOpen || !address) return undefined;
    // Reopened: a change that was refused meanwhile is said first.
    return operation?.state === 'failed'
      ? { step: 'form', name: '', refusal: operation.error ?? { code: 'unknown', raw: 'unknown' }, earlier: true }
      : { step: 'form', name: '' };
  }, [address, addressOpen, operation, step]);

  const refusalOf = (error: unknown): RenameRefusal => (
    error instanceof DevicesApiError ? renameRefusal({ ...error.body, error: error.code }) : { code: 'unknown', raw: 'no_answer' }
  );

  const renameContinue = () => {
    if (rename?.step !== 'form' || !rename.name) return;
    const { name } = rename;
    setStep({ step: 'form', name, busy: true });
    void (async () => {
      try {
        const answer = await api.previewRename(name, devices.find((each) => each.self)?.id);
        if (!alive.current) return;
        preview.current = { ...answer, name };
        setStep({
          step: 'confirm', name, hostname: answer.hostname, countdown: RENAME_CONFIRM_WAIT, isReturn: answer.isReturn,
          releaseAt: answer.releaseAt, holdDays: answer.holdDays, healthConnected: answer.healthConnected, devices: answer.devices,
        });
      } catch (error) {
        if (alive.current) setStep({ step: 'form', name, refusal: refusalOf(error) });
      }
    })();
  };

  const renameConfirm = () => {
    const pending = preview.current;
    if (step?.step !== 'confirm' || !pending) return;
    const previous = address?.hostname ?? '';
    renameFrom.current = previous || null;
    setStep({ ...step, busy: true });
    void (async () => {
      try {
        const answer = await api.changeAddress(pending.confirmationId);
        const next = await readAddress();
        if (!alive.current) return;
        if (answer.state === 'applied') {
          renameFrom.current = null;
          setStep({ step: 'done', hostname: answer.hostname, previous, retired: next?.retired.find((each) => each.hostname === previous) ?? null });
        } else setStep({ step: 'pending', hostname: pending.hostname });
      } catch (error) {
        if (!alive.current) return;
        if (error instanceof LostAnswer) {
          // Unknown: the address state says whether the change is running, done, or never arrived.
          lostChange.current = { reads: 0 };
          setStep({ step: 'pending', hostname: pending.hostname });
          return;
        }
        renameFrom.current = null;
        await readAddress();
        if (alive.current) setStep({ step: 'form', name: pending.name, refusal: refusalOf(error) });
      }
    })();
  };

  const renameClose = () => {
    setStep(undefined);
    preview.current = null;
    if (addressOpen) go(CONNECTION_PATH, true);
  };

  // ── binding again ──
  const flowBusy = flow.phase !== 'idle';
  const rebindState = useMemo<RebindState | undefined>(() => {
    if (!rebind) return undefined;
    if (rebind.state === 'ready') return { ...rebind, busy: flowBusy, refusal: rebindRefused ?? rebind.refusal };
    if (rebind.state === 'asNewOffered') return { ...rebind, busy: flowBusy || rebindBusy, refusal: rebindRefused };
    return rebind;
  }, [flowBusy, rebind, rebindBusy, rebindRefused]);

  const offerAsNew = () => {
    const id = flow.claimId;
    if (!id) return;
    setRebindBusy(true);
    setRebindRefused(undefined);
    void (async () => {
      try {
        const answer = await api.previewReset(id);
        if (!alive.current) return;
        resetConfirmation.current = answer.confirmationId;
        setRebindDialog({ kind: 'asNew', healthConnected: answer.healthConnected });
      } catch (error) {
        if (alive.current) setRebindRefused(rebindRefusal(error instanceof DevicesApiError ? error.code : undefined));
        await readRebind();
      } finally {
        if (alive.current) setRebindBusy(false);
      }
    })();
  };

  const confirmAsNew = () => {
    const id = flow.claimId;
    const confirmation = resetConfirmation.current;
    if (!id || !confirmation) return;
    const oldHost = address?.hostname;
    setRebindBusy(true);
    void (async () => {
      try {
        const result = await api.reset(id, confirmation);
        resetConfirmation.current = null;
        clearClaimMarker();
        if (alive.current) setRebindDialog(result === 'unpaired' ? { kind: 'resetDone', oldHost } : undefined);
      } catch (error) {
        if (alive.current) {
          setRebindDialog(undefined);
          // A lost answer is unknown: the overview read below says whether the reset began.
          if (!(error instanceof LostAnswer)) setRebindRefused(rebindRefusal(error instanceof DevicesApiError ? error.code : undefined));
        }
      } finally {
        if (alive.current) setRebindBusy(false);
        await Promise.all([readRebind(), readAddress(), readStatus(), readConnection()]);
      }
    })();
  };

  const tunnel = tunnelFromStatus(status, statusAt);
  const shown = connection ? { ...connection, change } : null;
  const host = address?.hostname ?? (() => {
    try {
      return status?.public_url ? new URL(status.public_url).host : undefined;
    } catch {
      return undefined;
    }
  })();

  return (
    <ConnectionPageView
      load={load}
      connection={shown}
      tunnel={tunnel}
      remote={remote}
      via={pageVia === 'direct' ? 'direct' : pageVia === 'other' && remote ? 'tunnel' : null}
      path={preference ? deviceUsing(nativePath, pageVia, nativeDirectPathReported()) : undefined}
      preference={preference}
      refused={refused}
      sending={sending}
      optimize={optimize}
      diagnostics={diagnosticsOpen ? {
        status, diagnostics, running: diagnosing, error: diagnosticsError, now: statusAt,
        // Direct's own details only while direct is on.
        direct: status?.direct?.enabled
          ? { certificate: directCertificateDate(status.direct.certificate_not_after, statusAt), lastConnection: lastConnectionLegs(nativePath) }
          : undefined,
      } : undefined}
      host={owner ? host : undefined}
      address={owner ? address : undefined}
      renameDevices={renameDevices(devices)}
      rename={rename}
      rebind={owner ? rebindState : undefined}
      rebindDialog={rebindDialog}
      canReset={Boolean(flow.claimId)}
      rebindNeedsBrowser={hasDesktopClient()}
      rebindBusy={rebindBusy || flowBusy}
      onBack={() => go(DEVICES_PATH)}
      onRetry={() => { setLoad('loading'); void readConnection(); void readStatus(); }}
      onPreferenceChange={(mode) => { void setNativeDirectChoice(mode).then((choice) => { if (alive.current && choice) setPreference(preferenceOf(choice)); }); }}
      onSettingChange={changeSetting}
      onOptimizeRoute={optimizeRoute}
      onOpenDiagnostics={() => setDiagnosticsOpen(true)}
      onCloseDiagnostics={() => setDiagnosticsOpen(false)}
      onRunDiagnostics={runDiagnostics}
      onChangeDomain={() => go(CONNECTION_ADDRESS_PATH)}
      onShowCleanup={(retired: RetiredName) => setStep({ step: 'cleanup', retired })}
      onRenameNameChange={(name) => setStep({ step: 'form', name: name.trim().toLowerCase() })}
      onRenameContinue={renameContinue}
      onRenameConfirm={renameConfirm}
      onRenameClose={renameClose}
      onRebind={() => { setRebindRefused(undefined); setRebindDialog({ kind: 'confirm' }); }}
      onRebindConfirm={() => { setRebindDialog(undefined); void flow.start(); }}
      onRebindAsNew={offerAsNew}
      onRebindAsNewConfirm={confirmAsNew}
      onRebindClose={() => setRebindDialog(undefined)}
      onRebindTurnOn={() => go(DEVICES_PATH)}
      // The page's own origin, on this computer: the shell hands a new window to the system browser.
      onRebindInBrowser={() => { openLinkInNewContext(`${window.location.origin}${CONNECTION_PATH}${CONNECTION_REBIND_HASH}`, 'noopener,noreferrer'); }}
    />
  );
}

export default ConnectionPage;
