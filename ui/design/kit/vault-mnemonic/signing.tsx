// What a signature request says about a wallet account (ui/design: vault/mnemonic/signing/*).
// Standard tier: the approval card gains an Account row (number and the user's name for it) and the
// derivation path in Details; the signing address is the chosen chain's address of that account.
// Protected tier: the sandbox window adds the account number and path, both from the record's
// authenticated metadata. It never shows the user's account name, which the sandbox cannot vouch for.
import type { ReactNode } from 'react';
import { ChevronRight, CircleCheck, Lock } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { useDraft } from '../draft';
import { CREATED_WORDS, IMPORTED_ADDRESSES, IMPORTED_WORDS, WALLET_ACCOUNTS, path } from './data';
import { WordGrid } from './parts';

const account = WALLET_ACCOUNTS[1];

/** The sandbox window's frame (vault-sandbox src/operationUi.ts), in product tokens. */
function SandboxCard({ title, subtitle, children, actions }: { title: string; subtitle: string; children: ReactNode; actions: [string, string] }) {
  return (
    <div className="flex min-h-full items-center justify-center bg-background p-6">
      <div className="flex w-full max-w-[400px] flex-col overflow-hidden rounded-2xl border border-edge bg-card shadow-elevation-2">
        <div className="flex items-start gap-3 border-b border-edge px-5 py-4">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-xl border border-mint/40 bg-mint/10 text-mint-ink">
            <Lock className="size-5" />
          </span>
          <div className="flex min-w-0 flex-col gap-0.5">
            <span className="text-[16px] font-semibold text-foreground">{title}</span>
            <span className="font-mono text-[12.5px] text-muted">{subtitle}</span>
          </div>
        </div>
        <div className="flex flex-col gap-3 px-5 py-4">{children}</div>
        <div className="grid grid-cols-[1fr_1.6fr] gap-2.5 border-t border-edge px-5 py-4">
          <Button type="button" variant="secondary">{actions[0]}</Button>
          <Button type="button">{actions[1]}</Button>
        </div>
      </div>
    </div>
  );
}

function MirrorRow({ label, value, added = false }: { label: string; value: string; added?: boolean }) {
  const d = useDraft();
  return (
    <div className={cn('flex min-w-0 flex-col gap-1', added && '-mx-2 rounded-lg bg-mint/[0.08] px-2 py-1.5 ring-1 ring-mint/30')}>
      <span className="flex items-center gap-2 text-[12.5px] text-muted">
        {label}
        {added ? <Badge variant="success">{d('新增', 'New')}</Badge> : null}
      </span>
      <span className="min-w-0 break-all font-mono text-[13.5px] text-foreground">{value}</span>
    </div>
  );
}

export function SandboxSignMirror() {
  const d = useDraft();
  return (
    <SandboxCard title={d('签名保护档操作', 'Sign protected operation')} subtitle="AGENT_WALLET" actions={[d('取消', 'Cancel'), d('确认签名', 'Confirm signature')]}>
      <div className="flex flex-col gap-0.5">
        <span className="text-[15px] font-semibold text-foreground">{d('Solana · SOL 转账', 'Solana · SOL transfer')}</span>
        <span className="text-[12.5px] text-muted">{d('会话', 'Session')} <span className="text-foreground">{d('网格交易', 'Grid trading')}</span></span>
      </div>
      <MirrorRow label={d('签名密钥', 'Signing key')} value={account.addresses.solana} />
      <MirrorRow label={d('钱包账户', 'Wallet account')} value={`#${account.index} · ${path('solana', account.index)}`} added />
      <MirrorRow label={d('转入账户', 'To')} value="586Z7H2vpX9qNhN2T4e9Utugie3ogjbxzGaMtM3E6HR5" />
      <MirrorRow label={d('数量', 'Amount')} value="0.01 SOL (10000000 lamports)" />
      <div className="h-px bg-border" />
      <span className="flex items-center gap-1.5 text-[13px] font-semibold text-foreground">
        <ChevronRight className="size-3.5 text-muted" />
        {d('详情', 'Details')}
      </span>
    </SandboxCard>
  );
}

/** The protected tier's words: generated and shown, or typed, only in the sandbox window. */
export function SandboxWordsMirror({ mode }: { mode: 'create' | 'import' }) {
  const d = useDraft();
  if (mode === 'create') {
    return (
      <SandboxCard title={d('抄下助记词', 'Write the words down')} subtitle="AGENT_WALLET" actions={[d('取消', 'Cancel'), d('我已抄好', 'I wrote them down')]}>
        <span className="text-[12.5px] leading-snug text-muted">{d('它们只在这个安全窗口里出现，保存后不会再显示。', 'They appear only in this secure window and are not shown again after saving.')}</span>
        <WordGrid words={CREATED_WORDS} columns={2} />
      </SandboxCard>
    );
  }
  return (
    <SandboxCard title={d('导入助记词', 'Import a seed phrase')} subtitle="AGENT_WALLET" actions={[d('取消', 'Cancel'), d('用通行密钥保存', 'Save with passkey')]}>
      <span className="text-[12.5px] leading-snug text-muted">{d('在这里输入，Avibe 和这台电脑都看不到。', 'Type it here; Avibe and this computer never see it.')}</span>
      <WordGrid words={IMPORTED_WORDS} columns={3} />
      <span className="flex items-center gap-1.5 text-[12px] font-medium text-mint-ink">
        <CircleCheck className="size-3.5" />
        {d('12 个词 · 校验通过', '12 words · checksum OK')}
      </span>
      <MirrorRow label={d('账户 #0 · EVM', 'Account #0 · EVM')} value={IMPORTED_ADDRESSES[0].eth} />
      <MirrorRow label={d('账户 #0 · Solana', 'Account #0 · Solana')} value={IMPORTED_ADDRESSES[0].solana} />
    </SandboxCard>
  );
}
