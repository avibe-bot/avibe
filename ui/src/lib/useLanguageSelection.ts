import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';

import { useApi } from '../context/ApiContext';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { useToast } from '../context/ToastContext';
import { setConfigField } from './configMutations';
import { useLatestRef } from './useLatestRef';

export interface LanguageOption {
  code: string;
  label: string;
}

export interface LanguageSelection {
  languages: LanguageOption[];
  current: LanguageOption;
  /** Apply the pick to this browser immediately, then persist it as the instance
   *  language when the caller may manage the instance. There is no Save button,
   *  so a failed save is reported where the user can act on it instead of being
   *  swallowed under a page that claims changes save themselves. The local
   *  change still stands: the pick they can see is the one they made. */
  select: (code: string) => Promise<void>;
}

/** The interface a language belongs to. Structural on purpose: the key is the
 *  i18n instance itself, the one thing in this stack that lasts as long as the
 *  interface it names. */
type LanguageHost = {
  language: string;
  changeLanguage: (code: string) => unknown;
};

/** What a mounted control lends the operation. Every field is read when a write
 *  actually runs, never captured when it is queued: `ApiContext` memoizes its
 *  value on `t`, so changing the language is itself what hands out a new
 *  `mutateConfig`, and the user's authority can change while a save waits. */
type LanguageConsumer = {
  /** Asked twice on purpose: once as the pick is made, which decides whether it
   *  is an instance decision at all, and again when its write actually runs, so
   *  authority taken away in between is never spent. Neither answer stands in
   *  for the other. */
  canPersist: () => boolean;
  persist: (code: string) => Promise<unknown>;
  reportFailure: (retry: () => void) => void;
};

type LanguageOperation = {
  /** The controls on screen now. Any of them can serve the queue — they read the
   *  same providers — which is the point: the operation belongs to the language,
   *  not to whichever control the user happened to have open. */
  serving: Set<LanguageConsumer>;
  /** The last one to have served, kept after it unmounts so a save already under
   *  way still has an api to finish through. */
  last: LanguageConsumer | null;
  tail: Promise<void>;
  /** Generations, not language codes: A -> B -> A has to stay three decisions,
   *  and a failure has to name the pick that caused it. */
  picks: number;
  latest: number;
  failed: number | null;
};

// MODULE scope on purpose, the way `useCoalescedWrite` owns a chat's writes: the
// language is one instance-wide value and a save outlives the control that
// started it. Settings unmounts the moment the user leaves it, so a queue owned
// by the component let the next control fire a second POST beside the first and
// let the older one commit last. Keyed by the i18n instance rather than by the
// api, because ApiContext's value is memoized on `t` and is therefore replaced by
// the very language change being ordered; a WeakMap rather than a singleton so
// two independent interfaces cannot serialize each other's writes.
const operations = new WeakMap<LanguageHost, LanguageOperation>();

/** The instance behind what a component is holding. `useTranslation` does not
 *  hand out the i18n instance: on every language change it returns a fresh copy
 *  of it — same prototype, same own properties, the instance kept as
 *  `__original` — so the object a control holds is replaced by the very
 *  operation being ordered, exactly like `ApiContext`'s value. Resolved at the
 *  boundary so everything below this line means the instance; a copy is what an
 *  interface looks like from inside a render, not what it is. If a future
 *  react-i18next stops copying, the object already is the instance and this is a
 *  no-op — and the consumer tests fail loudly if neither holds. */
const instanceOf = (held: LanguageHost): LanguageHost =>
  (held as { __original?: LanguageHost }).__original ?? held;

const operationFor = (host: LanguageHost): LanguageOperation => {
  const existing = operations.get(host);
  if (existing) return existing;
  const created: LanguageOperation = {
    serving: new Set<LanguageConsumer>(),
    last: null,
    tail: Promise.resolve(),
    picks: 0,
    latest: 0,
    failed: null,
  };
  operations.set(host, created);
  return created;
};

/** Lends a mounted control to its interface's language operation for as long as
 *  it is on screen. Returns the release, for an effect cleanup. */
export const serveLanguageOperation = (
  held: LanguageHost,
  consumer: LanguageConsumer,
): (() => void) => {
  const operation = operationFor(instanceOf(held));
  operation.serving.add(consumer);
  operation.last = consumer;
  return () => {
    operation.serving.delete(consumer);
  };
};

const activeConsumer = (operation: LanguageOperation): LanguageConsumer | null => {
  let active: LanguageConsumer | null = null;
  for (const consumer of operation.serving) active = consumer;
  return active;
};

const enqueue = (operation: LanguageOperation, code: string, pick: number): Promise<void> => {
  const run = async () => {
    // A pick the user has already moved past is not worth a request.
    if (operation.latest !== pick) return;
    // The control that started this may be gone; the interface still has to
    // finish the save, so fall back to the last one that served. Its authority
    // is read now rather than remembered from when the pick was queued — a
    // write must not outlive the permission behind it. An already-sent request
    // is not recalled; this is about what is still ours to send.
    const consumer = activeConsumer(operation) ?? operation.last;
    if (!consumer || !consumer.canPersist()) return;
    try {
      await consumer.persist(code);
      operation.failed = null;
    } catch {
      if (operation.latest !== pick) return;
      operation.failed = pick;
      consumer.reportFailure(() => retryLanguageSave(operation, code, pick));
    }
  };
  // Both arms run the next save: one refusal must not strand everything queued
  // behind it.
  operation.tail = operation.tail.then(run, run);
  return operation.tail;
};

const retryLanguageSave = (operation: LanguageOperation, code: string, pick: number): void => {
  // Inert unless this is still both the current choice and the current failure:
  // after a newer pick — the same code picked again included — the toast is a
  // record of something that no longer describes the interface.
  if (operation.latest !== pick || operation.failed !== pick) return;
  // Nothing is on screen to own a retry. The pick itself still stands locally;
  // it is the save that is not re-attempted on the authority of a control that
  // is no longer there.
  if (!activeConsumer(operation)) return;
  void enqueue(operation, code, pick);
};

/**
 * Records a user's language pick for one interface: local first, then persisted
 * once, in order, however the controls come and go.
 */
export const selectLanguage = (held: LanguageHost, code: string): Promise<void> => {
  const host = instanceOf(held);
  const operation = operationFor(host);
  operation.picks += 1;
  const pick = operation.picks;
  operation.latest = pick;
  // The pick they can see is the one they made, whatever the save does next —
  // and it retires any older failure, including for a user who persists nothing.
  void host.changeLanguage(code);
  // Whether this pick is an instance decision at all is settled here, by the
  // authority the user held while making it, and never revisited: a member
  // changing their own interface is choosing something local, so authority
  // arriving later — a promotion, a re-read, a second tab — must not turn that
  // into a write they never asked for. Only a control on screen can answer for
  // them; a pick made with none is nobody's authority to spend.
  const chooser = activeConsumer(operation);
  if (!chooser || !chooser.canPersist()) return Promise.resolve();
  return enqueue(operation, code, pick);
};

/**
 * Adopts the persisted instance language on load. A config read can answer late
 * — and the shell starts another one whenever `api` changes identity, which a
 * language change does — so it may only ever speak before the user has: once
 * this interface has a pick of its own, that pick is the answer.
 */
export const adoptPersistedLanguage = (held: LanguageHost, code: unknown): void => {
  if (typeof code !== 'string' || !code) return;
  const host = instanceOf(held);
  const operation = operationFor(host);
  if (operation.picks > 0) return;
  if (code === host.language) return;
  void host.changeLanguage(code);
};

/**
 * One owner for "which language is this, and what happens when you pick another"
 * — shared by the compact header switcher and the General settings selector, so
 * the two cannot drift on persistence or on which codes exist.
 */
export const useLanguageSelection = (): LanguageSelection => {
  const { i18n, t } = useTranslation();
  const { mutateConfig } = useApi();
  const { capabilities } = useInstanceAuthorization();
  const { showToast } = useToast();

  const latest = useLatestRef({
    i18n,
    mutateConfig,
    showToast,
    canManage: capabilities.can_manage_instance,
  });
  useEffect(() => serveLanguageOperation(i18n, {
    canPersist: () => latest.current.canManage,
    persist: (code) => latest.current.mutateConfig([setConfigField(['language'], code)]),
    reportFailure: (retry) => {
      const { showToast: toast, i18n: active } = latest.current;
      // Translated when it is shown, not when the save was started: by now the
      // interface is already in the language that failed to save.
      toast(active.t('settings.general.languageSaveFailed'), 'error', {
        label: active.t('common.retry'),
        onClick: retry,
      });
    },
  }), [i18n, latest]);

  const codes = Object.keys(i18n.options.resources ?? {});
  const languages = (codes.length ? codes : ['en']).map((code) => ({
    code,
    label: t(`language.${code}`, { defaultValue: code }),
  }));
  const current = languages.find((lang) => lang.code === i18n.language) || languages[0];

  const select = (code: string): Promise<void> => (
    code === i18n.language ? Promise.resolve() : selectLanguage(i18n, code)
  );

  return { languages, current, select };
};
