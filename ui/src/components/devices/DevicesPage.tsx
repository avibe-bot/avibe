// Settings · Devices, wired: reads the registry, remote access and activity, and feeds
// `DevicesPageView`. Every change goes to the instance and the page then reads again; nothing
// is shown as done before the instance said so. Routes: `/settings/remote-access` (the page) and
// `/settings/remote-access/activity` (the full list).
import type { TFunction } from 'i18next';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';

import type { RemoteAccessStatus } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { hasDesktopClient } from '@/desktop/bridge';
import { useDesktopClaimRequest } from '@/desktop/useDesktopClaimRequest';
import { useToast } from '@/context/ToastContext';
import { onPageReactivated } from '@/lib/pageActivity';
import { openLinkInNewContext } from '@/lib/pwaNavigation';
import { copyTextToClipboard } from '@/lib/utils';
import { ACCOUNT_SIGN_OUT_OTHERS_URL } from '@/sign-in/links';
import { DevicesApiError, LostAnswer, devicesApi as api, type SignOutAnswer } from './api';
import { awayOnClaim, returnRefusal } from './claimFlow';
import { DevicesPageView } from './DevicesPageView';
import { deviceName } from './format';
import { AVIBE_DOWNLOAD_URL, CONNECTION_PATH, CONNECTION_REBIND_HASH, DEVICES_ACTIVITY_PATH, DEVICES_PATH, instanceUrl } from './paths';
import { functionOffNeedsConfirm, stillSignedIn, signOutNeedsConfirm, turnOffNeedsConfirm } from './rules';
import type {
  AddKind, ClaimProblem, Device, DeviceActivity, DevicesConfirm, FunctionKind, Reach, SignedOutDevice,
} from './types';
import { useClaimFlow, type ClaimEnd } from './useClaimFlow';
import { useNow } from './useNow';
import { useOptimizeRoute } from './useOptimizeRoute';
import { useRebindOffered } from './useRebindOffered';
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

/** avibe.bot's 「退出其他设备」, outside the App. */
const signOutOthersThere = (t: TFunction) => ({
  label: t('devices.signedOut.signOutOtherDevices'),
  onClick: () => { openLinkInNewContext(ACCOUNT_SIGN_OUT_OTHERS_URL, 'noopener,noreferrer'); },
});

/** Whether a sign-out just made leaves a device still signed in to the account, with nothing this
 *  Avibe will do about it: the one case its toast says more than 「已退出」 (product, 2026-11-25). */
function leftSignedIn(answer: SignOutAnswer, owner: SignedOutDevice['owner']): boolean {
  return answer.signedOut.some((each) => stillSignedIn({ ...each, owner: each.owner !== 'unknown' ? each.owner : owner }, Date.now()));
}

export function DevicesPage() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { showToast } = useToast();
  const { remote, capabilities, instanceKind } = useInstanceAuthorization();
  const rebindOffered = useRebindOffered();
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
  /** When this page itself turned remote access on or finished binding: for a minute after, a
   *  connector with no connection yet is still registering, not lost. */
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const justStarted = startedAt !== null && now - startedAt < 60_000;
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
  const [problem, setProblem] = useState<'functionNeedsDevice' | 'failed' | 'gone' | 'lost' | 'offFromAfar' | 'closedFromAfar' | 'restrictedFromAfar' | undefined>();
  // The callback's own refusal arrives as one allowlisted word in the address, read once here.
  const [returned] = useState(() => returnRefusal(location.search));
  // A bind-again started from the connection page comes back here (the claim's return is one
  // fixed address): it is followed there, with the callback's word if there is one. Only that one
  // arrival is handed on: the same tab opening Devices later, while the attempt is still kept for
  // what only its starter may do, is an ordinary visit (the claim flow ends the trip once this
  // page has been shown).
  const [rebindReturn] = useState(() => awayOnClaim('rebind'));
  const [claimIssue, setClaimIssue] = useState<ClaimProblem | undefined>(() => (returned && !rebindReturn ? claimProblem(returned) : undefined));
  const alive = useRef(true);
  const readCount = useRef(0);
  /** A turn-off from this remote device is waiting for its answer: a read that fails meanwhile is
   *  the tunnel going, not a state that cannot be read, and is not said as such. */
  const closingFromAfar = useRef(false);
  /** 「只允许这台电脑」 pressed from another device is waiting for its answer: the same holds. */
  const restrictingFromAfar = useRef(false);

  const go = useCallback((to: string, replace = false) => navigate(to, { state: location.state, replace }), [location.state, navigate]);

  /** Reads the page again. Says whether remote access's state could be read, and what it was. */
  const load = useCallback(async (): Promise<{ readable: boolean; status: RemoteAccessStatus | null }> => {
    const mine = ++readCount.current;
    const current = () => alive.current && mine === readCount.current;
    const [devices, remote] = await Promise.allSettled([api.devices(), reads ? api.status() : Promise.resolve(null)]);
    const read = remote.status === 'fulfilled' && remote.value && typeof remote.value.paired === 'boolean' ? remote.value : null;
    const result = { readable: read !== null, status: read };
    if (!current()) return result;
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
        // Read again through a tunnel that was supposed to be gone: it was not turned off after
        // all. What is known is only that the answer was lost.
        if (remote.value.running) setProblem((current) => (current === 'offFromAfar' || current === 'closedFromAfar' ? 'lost' : current));
      } else if (!closingFromAfar.current && !restrictingFromAfar.current) {
        setStatusRead('unknown');
      }
    }
    if (!owner || devices.status !== 'fulfilled') return result;
    // A device signed out lately is no longer listed: its sign-out receipt still has its name, so the
    // activity says the same name as 「最近退出的设备」 on the same page.
    const byId = new Map<string, string>(devices.value.signedOut.flatMap((each) => (each.deviceId && each.device ? [[each.deviceId, each.device] as const] : [])));
    for (const each of devices.value.devices) byId.set(each.id, deviceName(t, each));
    const nameOf = (id: string | undefined) => (id ? byId.get(id) ?? null : null);
    names.current = nameOf;
    const [events, address] = await Promise.allSettled([api.activity(nameOf), api.address()]);
    if (!current()) return result;
    if (events.status === 'fulfilled') {
      const page = events.value;
      setFeed((current) => joinNewest(current, page));
    }
    // The notice must be seen; a failed read leaves the last answer as it was.
    if (address.status === 'fulfilled') {
      setUnrecognizedChange(address.value?.unrecognizedChange ? { hostname: address.value.unrecognizedChange.hostname } : undefined);
    }
    return result;
  }, [owner, reads, t]);

  const onEnd = useCallback((end: ClaimEnd) => {
    if (end.state === 'bound' || end.code === 'already_bound') {
      if (end.state === 'bound') setStartedAt(Date.now());
      setClaimIssue(undefined);
    } else {
      setClaimIssue(claimProblem(end.code === 'no_answer' ? undefined : end.code, {
        claimId: end.claimId, seconds: end.seconds, confirmingEmail: end.claim?.confirmingEmail,
      }));
    }
    void load();
  }, [load]);
  const flow = useClaimFlow({ kind: 'claim', locale: i18n.language, onEnd, returned });
  const claimRequest = useDesktopClaimRequest();

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

  // The full activity list is the owner's, as 「连接方式」 is: anyone else opening its address is
  // shown the page itself.
  const activityView = location.pathname.replace(/\/+$/, '') === DEVICES_ACTIVITY_PATH;
  useEffect(() => {
    if (activityView && !owner) navigate(DEVICES_PATH, { replace: true, state: location.state });
  }, [activityView, location.state, navigate, owner]);

  const onStatus = useCallback((next: RemoteAccessStatus) => {
    setStatus(next);
  }, []);
  const { optimize, recovering, run: optimizeRoute } = useOptimizeRoute({ status, now, onStatus, refresh: load });
  // An evaluation that is running, and a connector still starting or connecting, are followed until they end.
  const startingStill = justStarted && Boolean(status) && (!status?.paired || !hostOf(status) || !(status.running || status.pid_state === 'unknown'));
  const connecting = startingStill || (Boolean(status?.running) && tunnelFromStatus(status, now, justStarted).state === 'connecting');
  useEffect(() => {
    if (!recovering && !connecting) return undefined;
    const timer = window.setInterval(() => { void load(); }, FOLLOW_MS);
    return () => window.clearInterval(timer);
  }, [connecting, load, recovering]);

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
  const closed = problem === 'offFromAfar' || problem === 'closedFromAfar';
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
  // Just after this page turned it on or bound it, a state read before that (not bound, not
  // running) is not a close: nothing closed it, so it is still starting.
  else if (startingStill) reach = { state: 'starting' };
  // Inside Avibe Desktop this computer is bound by the App's own account sign-in, not by a claim
  // from this page.
  // Binding is the owner's: anyone else is offered nothing on an instance that is not bound.
  else if ((!status.paired || !host) && !owner) reach = { state: 'unknown' };
  // An Avibe this computer's own record calls an organization's is not bound by a person's claim,
  // from here or from the App: the page says so and offers nothing.
  else if ((!status.paired || !host) && instanceKind === 'organization') reach = { state: 'off', surface: 'browser', problem: { code: 'organization' } };
  else if ((!status.paired || !host) && hasDesktopClient()) reach = { state: 'off', surface: 'desktop', busy: claimRequest.requested };
  else if (!status.paired || !host) reach = { state: 'off', surface: 'browser', busy: flow.phase === 'sending', problem: claimIssue };
  else if (status.running || status.pid_state === 'unknown') {
    const tunnel = tunnelFromStatus(status, now, justStarted);
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
    // Turning it off is the one explicit close: whatever this page started is over.
    if (!on) setStartedAt(null);
    if (!on && remote) closingFromAfar.current = true;
    let outcome: 'done' | 'refused' | 'none' = 'none';
    let refusal: unknown;
    try {
      const answer = on ? await api.start() : await api.stop();
      outcome = answer.ok === false ? 'refused' : 'done';
      if (on && outcome === 'done') setStartedAt(Date.now());
    } catch (error) {
      if (!(error instanceof LostAnswer)) {
        outcome = 'refused';
        refusal = error;
      }
    }
    const refused = () => (refusal ? failed(refusal) : setProblem('failed'));
    if (!on && remote) {
      closingFromAfar.current = false;
      // From a remote device, turning it off stops the tunnel this page speaks through, so what the
      // stop request got back (an answer, an error the network made up, nothing) says nothing by
      // itself (Product, 2026-11-23). The state, read once more, decides: not readable is the
      // tunnel gone; read and off is off; read and still on is the only case the answer is taken
      // at its word. Whether the answer said done only picks the sentence.
      const read = await load();
      if (!alive.current) return;
      setSwitching(false);
      setConfirm(undefined);
      if (!read.readable) setProblem(outcome === 'done' ? 'closedFromAfar' : 'offFromAfar');
      else if (!read.status?.running) setProblem('closedFromAfar');
      else if (outcome === 'done') setProblem('closedFromAfar');
      else if (outcome === 'refused') refused();
      else setProblem('lost');
      return;
    }
    if (outcome === 'refused') refused();
    else if (outcome === 'none') failed(new LostAnswer());
    if (alive.current) {
      setSwitching(false);
      setConfirm(undefined);
    }
    await load();
  }, [failed, load, remote]);

  // 「只允许这台电脑」: one request, with no address in it, then the row is read again here. It ends
  // no sign-in and stops no notification; it only closes opening Avibe from the network.
  const restrictToThisComputer = useCallback(async (fromAfar: boolean) => {
    if (!fromAfar) return act(async () => { await api.restrictToThisComputer(); });
    // From another device this is the last thing it can do here: once the change is in force
    // nothing is answered to it any more. The answer says whether it was made; reading again
    // says whether this device was shut out.
    setBusy(true);
    setProblem(undefined);
    restrictingFromAfar.current = true;
    let outcome: 'done' | 'refused' | 'none' = 'none';
    let refusal: unknown;
    try {
      await api.restrictToThisComputer();
      outcome = 'done';
    } catch (error) {
      if (!(error instanceof LostAnswer)) {
        outcome = 'refused';
        refusal = error;
      }
    }
    const read = await load();
    restrictingFromAfar.current = false;
    if (!alive.current) return undefined;
    setBusy(false);
    setConfirm(undefined);
    if (outcome === 'refused') failed(refusal);
    else if (outcome === 'none') failed(new LostAnswer());
    // Answered, and this device can no longer read anything: that is the change in force.
    else if (!read.readable) setProblem('restrictedFromAfar');
    return undefined;
  }, [act, failed, load]);

  const signOut = useCallback((device: Device) => act(async () => {
    const answer = await api.signOut(device.id);
    // Only its own panel is closed: another device opened meanwhile stays open.
    setDetail((current) => (current === device.id ? undefined : current));
    // Out of the list at once; the reload that follows confirms it.
    setSnapshot((current) => ({ ...current, devices: current.devices.filter((each) => each.id !== device.id) }));
    const name = deviceName(t, device);
    // One line, where the App says such things. Only a device left signed in to the account, with
    // nothing here to end it, says more, and that stays until it is closed.
    if (leftSignedIn(answer, device.group === 'mine' ? 'mine' : device.group === 'others' ? 'others' : 'unknown')) {
      // Calm, not a warning; it stays until closed, with the one way to sign it out there.
      showToast(t('devices.signedOut.toastStillSignedIn', { device: name }), 'success', signOutOthersThere(t), { persist: true });
    } else {
      showToast(t('devices.signedOut.toast', { device: name }), 'success');
    }
  }), [act, t, showToast]);

  const confirmed = () => {
    if (confirm === 'turn-off') return void switchRemoteAccess(false);
    if (confirm === 'this-computer-only') return void restrictToThisComputer(true);
    if (confirm === 'remove' && opened) return void signOut(opened);
    if (confirm === 'health-off' && opened) {
      return void act(async () => { await api.revokeHealth(opened.id); });
    }
    if (confirm === 'remove-others' || confirm === 'remove-all-remote') {
      return void act(async () => {
        await api.signOutOthers();
        setSnapshot((current) => ({ ...current, devices: current.devices.filter((each) => each.self || each.kind === 'host') }));
        // One line; a device among them left signed in to the account says so in 「最近退出的设备」.
        showToast(t('devices.signedOut.toastOthers'), 'success');
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
      now={now}
      unrecognizedChange={unrecognizedChange}
      proxyAccess={snapshot.proxyAccess}
      rebindOffered={rebindOffered}
      problem={problem}
      detail={detail}
      confirm={confirm}
      busy={busy || switching}
      add={add}
      view={location.pathname.replace(/\/+$/, '') === DEVICES_ACTIVITY_PATH ? 'activity' : 'page'}
      onTurnOn={() => {
        // The Desktop binds in the shell's own claim dialog; this page reads the result when shown again.
        if (hasDesktopClient()) { claimRequest.request(); return; }
        setClaimIssue(undefined); void flow.start();
      }}
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
      onThisComputerOnly={() => {
        // On the computer itself nothing is lost by it; from another device, that device is shut out.
        if (devices.find((each) => each.kind === 'host')?.self) void restrictToThisComputer(false);
        else setConfirm('this-computer-only');
      }}
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
      onDismissNotice={() => { setProblem(undefined); }}
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
