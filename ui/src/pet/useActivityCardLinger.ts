import { useEffect, useMemo, useRef, useState, type RefObject } from 'react';

import { REPLY_LINGER_MS, REPLY_LINGER_RESUME_MS } from './useReplyLinger';

const STORAGE_KEY = 'avibe.pet.activityExpiry.v1';
const MAX_EXPIRED = 200;
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
type Expiries = Record<string, number>;
type Budget = { scope: unknown; remaining: number; paused: boolean };

function prune(value: unknown, now: number): Expiries {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value)
    .filter((entry): entry is [string, number] => typeof entry[1] === 'number'
      && Number.isFinite(entry[1]) && entry[1] > now - MAX_AGE_MS && entry[1] <= now)
    .reverse().sort((a, b) => b[1] - a[1]).slice(0, MAX_EXPIRED));
}

function readExpired(): { entries: Expiries; persistent: boolean } {
  try { return { entries: prune(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}'), Date.now()), persistent: true }; }
  catch { return { entries: {}, persistent: false }; } // Optional presentation persistence; blocked storage uses memory.
}

/**
 * Only the rendered front card or a >=50% visible list card spends its budget.
 * Reply identities come from Inbox timestamps, not document-local turn counters.
 * Only timer expiry is persisted; running/authorization/manual-close state stays
 * with its existing owner. localStorage naturally isolates Runtime origins.
 */
export function useActivityCardLinger({
  scope, candidateIds, documentVisible, paused, root,
}: {
  scope: unknown;
  candidateIds: readonly string[];
  documentVisible: boolean;
  paused: boolean;
  root: RefObject<HTMLElement | null>;
}): ReadonlySet<string> {
  const [initial] = useState(readExpired);
  const [expired, setExpired] = useState(initial.entries);
  const storageAvailable = useRef(initial.persistent);
  const expiredIds = useMemo(() => new Set(Object.keys(expired)), [expired]);
  const budgets = useRef(new Map<string, Budget>());
  const [exposed, setExposed] = useState<ReadonlySet<string>>(() => new Set());
  const candidateKey = JSON.stringify(candidateIds);

  useEffect(() => {
    if (!storageAvailable.current) return;
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(expired)); }
    catch { storageAvailable.current = false; }
  }, [expired]);

  useEffect(() => {
    const node = root.current;
    if (!node || typeof IntersectionObserver === 'undefined') return;
    let active = true;
    const observed = new Map<Element, string>();
    const visible = new Set<string>();
    const publish = () => setExposed((previous) => (
      previous.size === visible.size && [...previous].every((id) => visible.has(id))
        ? previous : new Set(visible)
    ));
    const observer = new IntersectionObserver((entries) => {
      if (!active) return;
      for (const entry of entries) {
        const id = observed.get(entry.target);
        if (!id) continue;
        if (entry.isIntersecting && entry.intersectionRatio >= 0.5) visible.add(id);
        else visible.delete(id);
      }
      publish();
    }, { threshold: [0, 0.5] });
    const sync = () => {
      for (const [element, id] of observed) {
        if (!node.contains(element) || element.getAttribute('data-pet-reply-id') !== id) {
          observer.unobserve(element);
          observed.delete(element);
          visible.delete(id);
        }
      }
      for (const element of node.querySelectorAll('[data-pet-reply-id]')) {
        if (observed.has(element)) continue;
        observed.set(element, element.getAttribute('data-pet-reply-id')!);
        observer.observe(element);
      }
      publish();
    };
    sync();
    // Expanding/collapsing replaces the cards; a new reply can reuse its DOM node.
    const mutations = new MutationObserver(sync);
    mutations.observe(node, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-pet-reply-id'] });
    return () => { active = false; mutations.disconnect(); observer.disconnect(); };
  }, [root, scope]);

  useEffect(() => {
    const timers: { timer: number; budget: Budget; started: number }[] = [];
    for (const id of JSON.parse(candidateKey) as string[]) {
      if (expiredIds.has(id)) continue;
      const previous = budgets.current.get(id);
      const budget = previous && previous.scope === scope
        ? previous : { scope, remaining: REPLY_LINGER_MS, paused: false };
      budgets.current.set(id, budget);
      if (!documentVisible || paused || !exposed.has(id)) {
        budget.paused = true;
        continue;
      }
      if (budget.paused) budget.remaining = Math.max(budget.remaining, REPLY_LINGER_RESUME_MS);
      budget.paused = false;
      const started = Date.now();
      const timer = window.setTimeout(() => {
        budget.remaining = 0;
        const now = Date.now();
        setExpired((current) => prune({ ...current, [id]: now }, now));
      }, budget.remaining);
      timers.push({ timer, budget, started });
    }
    return () => {
      for (const { timer, budget, started } of timers) {
        window.clearTimeout(timer);
        budget.remaining = Math.max(0, budget.remaining - (Date.now() - started));
      }
    };
  }, [candidateKey, documentVisible, expiredIds, exposed, paused, scope]);

  return expiredIds;
}
