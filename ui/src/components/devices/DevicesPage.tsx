// Settings · Devices, wired: reads the registry, remote access and activity, and feeds
// `DevicesPageView`. Every change goes to the instance and the page then reads again; nothing
// is shown as done before the instance said so. Routes: `/settings/remote-access` (the page) and
// `/settings/remote-access/activity` (the full list).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';

import type { RemoteAccessStatus } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { desktopLocale, hasDesktopClient, presentDesktopClient } from '@/desktop/bridge';
import { useToast } from '@/context/ToastContext';
import { onPageReactivated } from '@/lib/pageActivity';
import { openLinkInNewContext } from '@/lib/pwaNavigation';
import { copyTextToClipboard } from '@/lib/utils';
import { DevicesApiError, LostAnswer, devicesApi as api, type SignOutAnswer } from './api';
import { readClaimMarker, returnRefusal } from './claimFlow';
import { DevicesPageView } from './DevicesPageView';
import { deviceName } from './format';
import { AVIBE_DOWNLOAD_URL, CONNECTION_PATH, CONNECTION_REBIND_HASH, DEVICES_ACTIVITY_PATH, DEVICES_PATH, instanceUrl } from './paths';
import { functionOffNeedsConfirm, signOutNeedsConfirm, turnOffNeedsConfirm } from './rules';
import type {
  AccountResult, AddKind, ClaimProblem, Device, DeviceActivity, DevicesConfirm, FunctionKind, Reach, SignOutNotice,
} from './types';
import { useClaimFlow, type ClaimEnd } from './useClaimFlow';
import { useNow } from './useNow';
import { useOptimizeRoute } from './useOptimizeRoute';
import { claimProblem, tunnelFromStatus, type DevicesSnapshot } from './wire';

const REFRESH_MS = 30_000;
const FOLLOW_MS = 2_000;
const EMPTY: DevicesSnapshot = { devices: [], unlinkedHealth: [], signedOut: [], bulk: null };

const hostOf = (status: RemoteAccessStatus | null): string | null => {
  if (!status?.public_url) return null;
  try {
    return new URL(status.public_url).host;
  } catch {
    return null;
  }
};

type ActivityFeed = { items: DeviceActivity[]; cursor: string | null; extended: boolean };

/**
 * The newest page, read again, joined to the activity already listed. While only that page is
 * listed it simply replaces it. Once older pages were read, the entries the new page no longer
 * carries stay (they were pushed down by newer ones, not removed), and the cursor keeps pointing
 * past the oldest entry listed. If the two do not meet at all, more happened than a page holds:
 * the list starts over from the newest page rather than hide a gap.
 */
function joinNewest(current: ActivityFeed, page: { items: DeviceActivity[]; nextCursor: string | null }): ActivityFeed {
  const fresh = new Set(page.items.map((each) => each.id));
  if (!current.extended || !current.items.some((each) => fresh.has(each.id))) {
    return { items: page.items, cursor: page.nextCursor, extended: false };
  }
  return { items: [...page.items, ...current.items.filter((each) => !fresh.has(each.id))], cursor: current.cursor, extended: true };
}

/** What a sign-out's answer says about whose devices they were: the instance's word when it
 *  gives one, else what the page itself knows without guessing. */
function noticeFrom(answer: SignOutAnswer, known: { device: string | null; owner: SignOutNotice['owner']; hadHealth: boolean }): SignOutNotice {
  const owners = new Set(answer.signedOut.map((each) => each.owner));
  const said = owners.size === 1 ? [...owners][0] : 'unknown';
  const results = [...new Set(answer.signedOut.flatMap((each) => each.results))] as AccountResult[];
  return {
    device: known.device,
    owner: said !== 'unknown' ? said : known.owner,
    results,
    healthCleanupPending: answer.healthCleanupPending,
    hadHealth: known.hadHealth,
  };
}

export function DevicesPage() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { showToast } = useToast();
  const { remote, capabilities } = useInstanceAuthorization();
  // The instance's owner, not everyone who may operate it: only the owner is answered on what
  // this page shows about the instance itself.
  const owner = capabilities.is_instance_owner;
  // A member who may operate the instance keeps the switch and 「优化线路」 (the contract of
  // docs/plans/member-instance-operations.md); binding and everything else stay the owner's.
  const operate = !owner && capabilities.can_manage_instance;
  const reads = owner || operate;

  const [status, setStatus] = useState<RemoteAccessStatus | null>(null);
  const [statusRead, setStatusRead] = useState<'loading' | 'known' | 'unknown'>('loading');
  const [snapshot, setSnapshot] = useState<DevicesSnapshot>(EMPTY);
  const [listRead, setListRead] = useState<'loading' | 'known' | 'unknown'>('loading');
  const now = useNow();
  // Everything read so far, newest first, with the cursor that continues it. A refresh reads the
  // newest page again and joins it to what is already listed, so no entry between them is lost.
  const [feed, setFeed] = useState<{ items: DeviceActivity[]; cursor: string | null; extended: boolean }>({ items: [], cursor: null, extended: false });
  const [loadingOlder, setLoadingOlder] = useState(false);
  const names = useRef<(id: string | undefined) => string | null>(() => null);
  const [unrecognizedChange, setUnrecognizedChange] = useState<{ hostname: string } | undefined>();
  const [detail, setDetail] = useState<string | undefined>();
  const [confirm, setConfirm] = useState<DevicesConfirm | undefined>();
  const [add, setAdd] = useState<AddKind | undefined>();
  const [busy, setBusy] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [problem, setProblem] = useState<'functionNeedsDevice' | 'failed' | 'gone' | 'lost' | 'offFromAfar' | undefined>();
  // The callback's own refusal arrives as one allowlisted word in the address, read once here.
  const [returned] = useState(() => returnRefusal(location.search));
  // A bind-again started from the connection page comes back here (the claim's return is one
  // fixed address): it is followed there, with the callback's word if there is one.
  const [rebindReturn] = useState(() => readClaimMarker()?.kind === 'rebind');
  const [claimIssue, setClaimIssue] = useState<ClaimProblem | undefined>(() => (returned && !rebindReturn ? claimProblem(returned) : undefined));
  const [notice, setNotice] = useState<SignOutNotice | undefined>();
  const alive = useRef(true);
  const readCount = useRef(0);

  const go = useCallback((to: string, replace = false) => navigate(to, { state: location.state, replace }), [location.state, navigate]);

  const load = useCallback(async () => {
    const mine = ++readCount.current;
    const current = () => alive.current && mine === readCount.current;
    const [devices, remote] = await Promise.allSettled([api.devices(), reads ? api.status() : Promise.resolve(null)]);
    if (!current()) return;
    if (devices.status === 'fulfilled') {
      setSnapshot(devices.value);
      setListRead('known');
    } else {
      // A list that could not be read is not an empty list; one read before stays as it was.
      setListRead((current) => (current === 'known' ? current : 'unknown'));
    }
    if (reads) {
      if (remote.status === 'fulfilled' && remote.value && typeof remote.value.paired === 'boolean') {
        setStatus(remote.value);
        setStatusRead('known');
      } else {
        setStatusRead('unknown');
      }
    }
    if (!owner || devices.status !== 'fulfilled') return;
    const byId = new Map(devices.value.devices.map((each) => [each.id, deviceName(t, each)]));
    const nameOf = (id: string | undefined) => (id ? byId.get(id) ?? null : null);
    names.current = nameOf;
    const [events, address] = await Promise.allSettled([api.activity(nameOf), api.address()]);
    if (!current()) return;
    if (events.status === 'fulfilled') {
      const page = events.value;
      setFeed((current) => joinNewest(current, page));
    }
    // The notice must be seen; a failed read leaves the last answer as it was.
    if (address.status === 'fulfilled') {
      setUnrecognizedChange(address.value?.unrecognizedChange ? { hostname: address.value.unrecognizedChange.hostname } : undefined);
    }
  }, [owner, reads, t]);

  const onEnd = useCallback((end: ClaimEnd) => {
    if (end.state === 'bound' || end.code === 'already_bound') {
      setClaimIssue(undefined);
    } else {
      setClaimIssue(claimProblem(end.code === 'no_answer' ? undefined : end.code, { claimId: end.claimId, seconds: end.seconds }));
    }
    void load();
  }, [load]);
  const flow = useClaimFlow({ kind: 'claim', locale: i18n.language, onEnd, returned });

  // One navigation settles the address this page was opened at: on to the connection page for a
  // bind-again, else the callback's word is removed. Never both, or the second undoes the first.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    if (rebindReturn) {
      navigate({ pathname: CONNECTION_PATH, search: returned ? `?claim_error=${returned}` : '' }, { replace: true, state: location.state });
      return;
    }
    if (!params.has('claim_error')) return;
    params.delete('claim_error');
    const search = params.toString();
    navigate({ pathname: location.pathname, search: search ? `?${search}` : '' }, { replace: true, state: location.state });
  }, [location.pathname, location.search, location.state, navigate, rebindReturn, returned]);

  useEffect(() => {
    alive.current = true;
    if (rebindReturn) return () => { alive.current = false; };
    queueMicrotask(() => { void load(); });
    const refresh = () => { if (document.visibilityState === 'visible') void load(); };
    const timer = window.setInterval(refresh, REFRESH_MS);
    const stop = onPageReactivated(refresh);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
      stop();
    };
  }, [load, rebindReturn]);

  const onStatus = useCallback((next: RemoteAccessStatus) => {
    setStatus(next);
  }, []);
  const { optimize, recovering, run: optimizeRoute } = useOptimizeRoute({ status, now, onStatus, refresh: load });
  // An evaluation that is running is followed until it ends.
  useEffect(() => {
    if (!operate || !recovering) return undefined;
    const timer = window.setInterval(() => { void load(); }, FOLLOW_MS);
    return () => window.clearInterval(timer);
  }, [load, operate, recovering]);

  const activity = feed.items;
  const nextCursor = feed.cursor;
  const loadOlder = useCallback(async () => {
    if (!nextCursor || loadingOlder) return;
    setLoadingOlder(true);
    try {
      const page = await api.activity(names.current, nextCursor);
      if (!alive.current) return;
      setFeed((current) => {
        // Only the page that continues what is listed now is appended.
        if (current.cursor !== nextCursor) return current;
        const seen = new Set(current.items.map((each) => each.id));
        return { items: [...current.items, ...page.items.filter((each) => !seen.has(each.id))], cursor: page.nextCursor, extended: true };
      });
    } catch {
      // The list stays as it is; the button is still there to ask again.
    } finally {
      if (alive.current) setLoadingOlder(false);
    }
  }, [loadingOlder, nextCursor]);

  // Once remote access was turned off from afar, the list on the page is the last one read: the
  // connections it names are the ones that were just closed, so no path is said for them.
  const closed = problem === 'offFromAfar';
  const devices = useMemo<Device[]>(() => snapshot.devices.map((each) => ({
    ...each,
    online: closed && each.kind !== 'host' ? undefined : each.online,
    recent: activity.filter((item) => item.deviceId === each.id).slice(0, 3),
  })), [activity, closed, snapshot.devices]);
  const opened = devices.find((each) => each.id === detail);
  const selfAccount = devices.find((each) => each.self)?.account;
  const host = hostOf(status);

  let reach: Reach;
  if (flow.lost) reach = { state: 'moved', host: flow.lost.host };
  else if (flow.phase === 'waiting') reach = { state: 'starting', waiting: true };
  else if (flow.phase === 'retrying') reach = { state: 'starting', retrying: true };
  else if (flow.phase === 'binding') reach = { state: 'starting' };
  else if (statusRead !== 'known' || !status) reach = { state: statusRead === 'loading' ? 'loading' : 'unknown' };
  // Inside Avibe Desktop this computer is bound by the App's own account sign-in, not by a claim
  // from this page.
  // Binding is the owner's: anyone else is offered nothing on an instance that is not bound.
  else if ((!status.paired || !host) && !owner) reach = { state: 'unknown' };
  else if ((!status.paired || !host) && hasDesktopClient()) reach = { state: 'off', surface: 'desktop', account: null, name: '' };
  else if (!status.paired || !host) reach = { state: 'off', surface: 'browser', busy: flow.phase === 'sending', problem: claimIssue };
  else if (status.running || status.pid_state === 'unknown') {
    const tunnel = tunnelFromStatus(status, now);
    reach = {
      state: 'on',
      host,
      // The owner's own account, where this session carries one (at the computer itself it does not).
      account: owner ? selfAccount : undefined,
      tunnel: tunnel.state,
      ms: tunnel.ms,
      direct: status.direct?.enabled === true && status.direct.state === 'running',
      busy: switching,
    };
  } else reach = { state: 'paused', host, account: owner ? selfAccount : undefined, busy: switching };

  // An answer that never arrived says nothing about what happened: the page reads again and says
  // that it does not know, never that it failed.
  const failed = useCallback((error: unknown) => {
    if (error instanceof LostAnswer) setProblem('lost');
    else if (error instanceof DevicesApiError && error.status === 404) setProblem('gone');
    else setProblem('failed');
  }, []);

  const act = useCallback(async (work: () => Promise<void>) => {
    setBusy(true);
    setProblem(undefined);
    try {
      await work();
    } catch (error) {
      failed(error);
    } finally {
      if (alive.current) {
        setBusy(false);
        setConfirm(undefined);
      }
      await load();
    }
  }, [failed, load]);

  const switchRemoteAccess = useCallback(async (on: boolean) => {
    setSwitching(true);
    setProblem(undefined);
    try {
      const answer = on ? await api.start() : await api.stop();
      if (answer.ok === false) setProblem('failed');
    } catch (error) {
      // Turning it off stops the tunnel before the answer leaves: from a remote device, hearing
      // nothing back is what success looks like.
      if (!on && remote && error instanceof LostAnswer) setProblem('offFromAfar');
      else failed(error);
    } finally {
      if (alive.current) {
        setSwitching(false);
        setConfirm(undefined);
      }
      await load();
    }
  }, [failed, load, remote]);

  const signOut = useCallback((device: Device) => act(async () => {
    const answer = await api.signOut(device.id);
    setDetail(undefined);
    setNotice(noticeFrom(answer, {
      device: deviceName(t, device),
      // The row the page just acted on says whose it was: no guess.
      owner: device.group === 'mine' ? 'mine' : device.group === 'others' ? 'others' : 'unknown',
      hadHealth: signOutNeedsConfirm(device),
    }));
  }), [act, t]);

  const confirmed = () => {
    if (confirm === 'turn-off') return void switchRemoteAccess(false);
    if (confirm === 'remove' && opened) return void signOut(opened);
    if (confirm === 'health-off' && opened) {
      return void act(async () => { await api.revokeHealth(opened.id); });
    }
    if (confirm === 'remove-others' || confirm === 'remove-all-remote') {
      const all = confirm === 'remove-all-remote';
      return void act(async () => {
        const answer = await api.signOutOthers();
        setNotice(noticeFrom(answer, {
          device: null,
          // "My other devices" are the viewer's own; every remote device may be anyone's.
          owner: all ? 'unknown' : 'mine',
          hadHealth: devices.some((each) => !each.self && each.kind !== 'host' && signOutNeedsConfirm(each)),
        }));
      });
    }
    if (typeof confirm === 'object') {
      const { unlinkedHealth } = confirm;
      return void act(async () => { await api.revokeUnlinkedHealth(unlinkedHealth); });
    }
    return undefined;
  };

  const changeFunction = (deviceId: string, kind: FunctionKind, on: boolean) => {
    const device = devices.find((each) => each.id === deviceId);
    const fn = device?.functions.find((each) => each.kind === kind);
    if (!device || !fn || kind === 'computerUse') return;
    if (kind === 'health') {
      // Health is only ever turned off here, and that deletes what it synced: it asks first.
      if (!on && functionOffNeedsConfirm(fn)) setConfirm('health-off');
      return;
    }
    void (async () => {
      setProblem(undefined);
      try {
        await api.setFunction(deviceId, kind, on);
      } catch (error) {
        if (error instanceof DevicesApiError && error.status === 409) setProblem('functionNeedsDevice');
        else failed(error);
      }
      await load();
    })();
  };

  const copy = (url: string) => {
    void copyTextToClipboard(url).then((ok) => { if (ok) showToast(t('common.copied'), 'success'); });
  };

  return (
    <DevicesPageView
      reach={reach}
      devices={devices}
      list={listRead === 'known' ? undefined : listRead}
      activity={activity}
      viewer={owner ? 'owner' : 'member'}
      operate={operate}
      optimize={operate ? optimize : undefined}
      onOptimizeRoute={optimizeRoute}
      viewerAccount={selfAccount}
      bulk={snapshot.bulk}
      unlinkedHealth={snapshot.unlinkedHealth}
      signedOut={snapshot.signedOut}
      signOutNotice={notice}
      unrecognizedChange={unrecognizedChange}
      problem={problem}
      detail={detail}
      confirm={confirm}
      busy={busy || switching}
      add={add}
      view={location.pathname.replace(/\/+$/, '') === DEVICES_ACTIVITY_PATH ? 'activity' : 'page'}
      onTurnOn={() => { setClaimIssue(undefined); void flow.start(); }}
      onSignIn={() => { presentDesktopClient('account', undefined, desktopLocale(i18n.resolvedLanguage ?? i18n.language)); }}
      onCancelClaim={(id) => { setClaimIssue(undefined); void flow.cancel(id); }}
      onRemoteAccessChange={(on) => {
        if (!on && turnOffNeedsConfirm(devices)) setConfirm('turn-off');
        else void switchRemoteAccess(on);
      }}
      onRetry={() => {
        if (reads && statusRead !== 'known') setStatusRead('loading');
        if (listRead !== 'known') setListRead('loading');
        void load();
      }}
      onCopyAddress={() => { if (host) copy(instanceUrl(host)); }}
      onOpenAddress={() => { if (host) openLinkInNewContext(instanceUrl(host), 'noopener,noreferrer'); }}
      onOpenConnection={() => go(CONNECTION_PATH)}
      onOpenRebind={() => go(`${CONNECTION_PATH}${CONNECTION_REBIND_HASH}`)}
      onOpenDevice={(id) => setDetail(id)}
      onCloseDevice={() => setDetail(undefined)}
      onFunctionChange={changeFunction}
      onSignOutDevice={(id) => {
        const device = devices.find((each) => each.id === id);
        if (!device) return;
        setDetail(id);
        if (!device.pushOnly && signOutNeedsConfirm(device)) setConfirm('remove');
        else void signOut(device);
      }}
      onSignOutOthers={() => setConfirm(snapshot.bulk === 'all_remote' ? 'remove-all-remote' : 'remove-others')}
      onDisconnectHealth={(id) => setConfirm({ unlinkedHealth: id })}
      onConfirm={confirmed}
      onCancelConfirm={() => setConfirm(undefined)}
      onDismissNotice={() => { setNotice(undefined); setProblem(undefined); }}
      onShowActivity={() => go(DEVICES_ACTIVITY_PATH)}
      moreActivity={nextCursor ? (loadingOlder ? 'loading' : 'available') : undefined}
      onMoreActivity={() => { void loadOlder(); }}
      onAddDevice={() => setAdd('iphone')}
      onAddKindChange={setAdd}
      onCloseAdd={() => setAdd(undefined)}
      onOpenDownloads={() => openLinkInNewContext(AVIBE_DOWNLOAD_URL, 'noopener,noreferrer')}
      onCopyLink={copy}
      onBack={() => go(DEVICES_PATH)}
    />
  );
}

export default DevicesPage;
