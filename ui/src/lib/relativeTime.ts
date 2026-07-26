// Shared relative-time formatter. Three workbench surfaces (Inbox popover,
// full Inbox page, Harness page) had near-identical copies that hardcoded
// English suffixes; centralise here so the i18n strings live in one place.
import type { TFunction } from 'i18next';

export function formatRelativeTime(
  iso: string | null | undefined,
  t: TFunction,
  now: number = Date.now(),
): string {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const diffSec = Math.round((now - then) / 1000);
  if (diffSec < 60) return t('common.relative.justNow');
  const mins = Math.round(diffSec / 60);
  if (mins < 60) return t('common.relative.minutesAgo', { count: mins });
  const hours = Math.round(mins / 60);
  if (hours < 24) return t('common.relative.hoursAgo', { count: hours });
  const days = Math.round(hours / 24);
  if (days < 7) return t('common.relative.daysAgo', { count: days });
  return new Date(iso).toISOString().slice(0, 10);
}

// Absolute timestamp in the viewer's LOCAL timezone, formatted as
// ``YYYY-MM-DD HH:mm:ss`` — a space (not "T") between date and time and no
// trailing "Z". ``new Date(iso)`` parses the UTC instant; the getters below
// return local-time components, so the displayed wall-clock matches the
// reader's machine instead of UTC (workbench chat timestamps).
export function formatLocalDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  );
}
