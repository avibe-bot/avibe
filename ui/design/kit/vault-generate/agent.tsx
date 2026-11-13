// Version 2, the Agent path (owner 2026-11-13 21:08): on the Standard tier an Agent may generate
// and save a new secret itself. It states only the format; the value is made where it is encrypted
// and never printed. What it runs and gets back, and how the result shows in Vaults. Draft copy;
// the list rows mirror the shipped SecretRow (VaultsPage.tsx) with the one addition, the mark.
import type { ReactNode } from 'react';
import { Bot, ChevronRight, KeyRound, MoreHorizontal, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';

import { useDraft } from '../draft';

/** The Agent's command and the CLI's whole answer: the same value-free row `vibe vault list` prints. */
export function AgentRunCard() {
  const d = useDraft();
  const command = 'vibe vault generate SESSION_SECRET --type hex --bytes 32 \\\n  --description "Web session signing key" --tag web';
  const output = `{
  "ok": true,
  "kind": "vault_secret_generated",
  "secret": {
    "name": "SESSION_SECRET",
    "kind": "static",
    "protection": "standard",
    "fields": ["value"],
    "tags": ["web"],
    "description": "Web session signing key",
    "generated": { "by": "agent", "session_id": "ses7k2…", "type": "hex", "bytes": 32 }
  }
}`;
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-edge bg-surface p-4 shadow-elevation-0">
      <span className="flex items-center gap-2 text-[13px] font-semibold text-foreground"><Bot className="size-4 text-violet-ink" />{d('Agent 在会话里运行', 'The Agent runs, in its Session')}</span>
      <pre className="overflow-x-auto rounded-lg bg-field px-3 py-2.5 font-mono text-[12px] leading-relaxed text-foreground">{command}</pre>
      <span className="text-[12px] text-muted">{d('它拿回来的全部内容（没有值）：', 'All it gets back (no value):')}</span>
      <pre className="overflow-x-auto rounded-lg bg-field px-3 py-2.5 font-mono text-[12px] leading-relaxed text-foreground">{output}</pre>
    </div>
  );
}

/** The mark an Agent-generated secret carries in the list: which Agent made it, in which Session. */
export function AgentMark({ session }: { session: string }) {
  const d = useDraft();
  return (
    <Badge variant="outline" className="border-violet/40 bg-violet-soft text-violet-ink">
      <Bot className="size-3" />{d(`Agent 生成 · ${session}`, `Agent-generated · ${session}`)}
    </Badge>
  );
}

type Row = { name: string; desc: string; used: string; tags: string[]; mark?: string; protectedTier?: boolean; host?: string };

function SecretRowMirror({ row }: { row: Row }) {
  const { t } = useTranslation();
  return (
    <li className="flex flex-wrap items-center gap-x-3.5 gap-y-2 rounded-xl border border-edge bg-surface px-4 py-3 shadow-elevation-0 md:flex-nowrap">
      <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-accent/10 text-accent-ink"><KeyRound className="size-4" /></div>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-sm font-semibold">{row.name}</span>
          {row.protectedTier ? <Badge variant="warning">{t('vaults.protected')}</Badge> : <Badge variant="secondary">{t('vaults.standard')}</Badge>}
          {row.mark ? <AgentMark session={row.mark} /> : null}
        </div>
        <span className="truncate text-xs text-muted">{row.desc} · {row.used}</span>
      </div>
      <div className="order-last flex w-full min-w-0 flex-wrap items-center gap-1.5 md:order-none md:w-auto md:max-w-sm md:flex-col md:items-end">
        <div className="flex flex-wrap gap-1">{row.tags.map((tag) => <Badge key={tag} variant="outline">{tag}</Badge>)}</div>
        {row.host ? <Badge variant="info">{t('vaults.proxyHost', { host: row.host })}</Badge> : null}
      </div>
      <Button variant="ghost" size="icon" aria-label={t('vaults.rowActions')}><MoreHorizontal className="size-4" /></Button>
    </li>
  );
}

/** Three rows of the Vaults list, the newest one made by an Agent. */
export function VaultsListWithAgent() {
  const { t } = useTranslation();
  const d = useDraft();
  const rows: Row[] = [
    { name: 'SESSION_SECRET', desc: d('Web 会话签名密钥', 'Web session signing key'), used: t('vaults.neverUsed'), tags: ['web'], mark: d('部署 web 服务', 'Deploy the web service') },
    { name: 'GITHUB_TOKEN', desc: d('CI 用', 'For CI'), used: t('vaults.used', { count: 42 }), tags: ['ci'], host: 'api.github.com' },
    { name: 'ETH_HOT_KEY', desc: d('测试网热钱包', 'Testnet hot wallet'), used: t('vaults.used', { count: 3 }), tags: ['crypto'], protectedTier: true },
  ];
  return <ul className="flex flex-col gap-2">{rows.map((row) => <SecretRowMirror key={row.name} row={row} />)}</ul>;
}

/** In the secret's Edit dialog: where it came from, with the way to the Session and to undo it. */
export function ProvenanceLine(): ReactNode {
  const d = useDraft();
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-[10px] border border-violet/30 bg-violet-soft px-3 py-2.5 text-[12px] text-foreground">
      <Bot className="size-3.5 shrink-0 text-violet-ink" />
      <span className="min-w-0 flex-1">{d('Agent 在会话「部署 web 服务」里生成 · 今天 21:20 · Hex 32 字节。它没见过这个值，和其他标准档密钥一样可以使用它。', 'Generated by an Agent in the Session “Deploy the web service” · today 21:20 · Hex 32 bytes. It never saw the value; like any Standard secret, it can use it.')}</span>
      <Button type="button" variant="ghost" size="xs" className="text-violet-ink">{d('打开会话', 'Open Session')}<ChevronRight className="size-3.5" /></Button>
      <Button type="button" variant="ghost" size="xs" className="text-destructive-ink"><Trash2 className="size-3.5" />{d('删除', 'Delete')}</Button>
    </div>
  );
}
