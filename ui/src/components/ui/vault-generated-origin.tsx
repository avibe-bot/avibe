// Where a saved value came from when an Agent or the CLI generated it inside avault
// (ui/design: vault/generate-secret/agent-edit). Read from the server's per-field provenance only:
// a value generated in a browser dialog is the user's own, and the server drops an entry once the
// user replaces or removes that value.
import * as React from 'react';
import { Bot, ChevronRight, Terminal } from 'lucide-react';
import { Trans, useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import { useApi, type VaultGeneratedField, type VaultSecret } from '@/context/ApiContext';
import { formatRelativeTime } from '@/lib/relativeTime';
import { vaultFieldNames } from '@/lib/vaultFields';
import { cn } from '@/lib/utils';
import { Badge } from './badge';

function useOriginText() {
  const { t } = useTranslation();
  return {
    recipe: (entry: VaultGeneratedField) => (entry.type === 'password'
      ? t('vaults.generatedBy.password', { count: entry.length ?? 0 })
      : t(entry.type === 'hex' ? 'vaults.generatedBy.hex' : 'vaults.generatedBy.base64', { count: entry.bytes ?? 0 })),
  };
}

/** The Vaults list's markers: one per origin (an Agent, the CLI), each with the fields it generated
 *  when the secret has several, or the Session it was made in (its title then) when it has one. */
export function VaultGeneratedOriginBadge({ secret, className }: { secret: VaultSecret; className?: string }) {
  const { t } = useTranslation();
  const entries = secret.generated ?? [];
  if (entries.length === 0) return null;
  const single = vaultFieldNames(secret).length === 1;
  return (
    <>
      {(['agent', 'cli'] as const).map((by) => {
        const group = entries.filter((entry) => entry.by === by);
        if (group.length === 0) return null;
        const detail = single ? group[0].session_title?.trim() || null : group.map((entry) => entry.field).join(', ');
        const label = by === 'agent' ? t('vaults.generatedBy.agent') : t('vaults.generatedBy.cli');
        return (
          <Badge key={by} variant="outline" className={cn('max-w-full border-violet/40 bg-violet-soft text-violet-ink', className)}>
            {by === 'agent' ? <Bot className="size-3 shrink-0" /> : <Terminal className="size-3 shrink-0" />}
            <span className="truncate">{detail ? t('vaults.generatedBy.badge', { who: label, detail }) : label}</span>
          </Badge>
        );
      })}
    </>
  );
}

type SessionLookup = { status: 'loading' | 'missing' } | { status: 'found'; title: string | null };

/** Whether the Session still exists, so a link is offered only to one that opens. */
function useGeneratedSession(sessionId: string | null): SessionLookup {
  const api = useApi();
  // The answer for one Session id; a different id reads as loading until its own answer arrives.
  const [answer, setAnswer] = React.useState<{ id: string; lookup: SessionLookup } | null>(null);
  React.useEffect(() => {
    if (!sessionId) return;
    let alive = true;
    api.getSession(sessionId, { handleError: false }).then(
      (row) => {
        if (!alive) return;
        setAnswer({
          id: sessionId,
          lookup: row && row.id === sessionId ? { status: 'found', title: row.title?.trim() || null } : { status: 'missing' },
        });
      },
      () => {
        if (alive) setAnswer({ id: sessionId, lookup: { status: 'missing' } });
      },
    );
    return () => {
      alive = false;
    };
  }, [api, sessionId]);
  if (!sessionId) return { status: 'missing' };
  return answer?.id === sessionId ? answer.lookup : { status: 'loading' };
}

function OriginLine({ entry, showField, now }: { entry: VaultGeneratedField; showField: boolean; now: number }) {
  const { t } = useTranslation();
  const { recipe } = useOriginText();
  const session = useGeneratedSession(entry.by === 'agent' ? entry.session_id : null);
  // The title it has now when the Session still exists, else the title it had then; never an id.
  const title = (session.status === 'found' ? session.title : null) ?? entry.session_title?.trim() ?? null;
  const who = entry.by === 'cli'
    ? t('vaults.generatedBy.cliLong')
    : title ? t('vaults.generatedBy.agentInSession', { session: title }) : t('vaults.generatedBy.agent');
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <span className="min-w-0 flex-1 text-[12.5px] leading-relaxed text-foreground">
        {showField ? <span className="mr-1.5 font-mono text-violet-ink">{entry.field}</span> : null}
        <Trans
          i18nKey="vaults.generatedBy.line"
          values={{ who, when: formatRelativeTime(entry.at, t, now, { alwaysRelative: true }), recipe: recipe(entry) }}
          // The format ("Hex 32 字节") reads as one token: it never breaks inside.
          components={{ recipe: <span className="whitespace-nowrap" /> }}
        />
        {entry.by === 'agent' ? <span className="block text-[12px] text-muted">{t('vaults.generatedBy.agentNote')}</span> : null}
      </span>
      {session.status === 'found' && entry.session_id ? (
        <Link
          to={`/chat/${encodeURIComponent(entry.session_id)}`}
          className="inline-flex shrink-0 items-center gap-0.5 text-[12.5px] font-medium text-violet-ink hover:underline"
        >
          {t('vaults.generatedBy.openSession')}
          <ChevronRight className="size-3.5" />
        </Link>
      ) : null}
    </li>
  );
}

/** The edit dialog's account of each generated field, from the latest saved state. */
export function VaultGeneratedOrigins({ secret, hidden = [] }: { secret: VaultSecret; hidden?: readonly string[] }) {
  const entries = (secret.generated ?? []).filter((entry) => !hidden.includes(entry.field));
  const [now] = React.useState(() => Date.now());
  if (entries.length === 0) return null;
  const byAgent = entries.some((entry) => entry.by === 'agent');
  const showField = vaultFieldNames(secret).length > 1;
  return (
    <div className="flex items-start gap-2.5 rounded-[10px] border border-violet/40 bg-violet-soft px-3 py-2.5">
      {byAgent ? <Bot className="mt-0.5 size-4 shrink-0 text-violet-ink" /> : <Terminal className="mt-0.5 size-4 shrink-0 text-violet-ink" />}
      <ul className="flex min-w-0 flex-1 flex-col gap-1.5">
        {entries.map((entry) => <OriginLine key={entry.field} entry={entry} showField={showField} now={now} />)}
      </ul>
    </div>
  );
}
