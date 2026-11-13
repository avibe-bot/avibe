// Protected-tier seed-phrase wallets (proposal, 3.2.7; contract docs/plans/vault-protected-hd-wallets.md
// rev0.6). The parent page draws the complete Add key dialog and launches the secure window; the
// words, passphrase, three-word check and account #0 review all happen in that window, never here.
// Signing stays approval-required in the secure window; Bitcoin is Taproot only on this tier.
import { AlertTriangle, CircleCheck, ExternalLink, KeyRound, Loader2, Lock, ShieldCheck, Signature, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { SegmentedRadio } from '@/components/ui/segmented';
import { TagInput } from '@/components/ui/tag-input';
import { HdAccountAddressList } from '@/components/ui/vault-hd-wallet-steps';
import { HintLine, KeySourceTabs } from '@/components/ui/vault-key-builder-parts';

import { useDraft } from '../draft';
import { CREATED_ADDRESSES, CREATED_WORDS, IMPORTED_ADDRESSES, IMPORTED_WORDS } from './data';
import { AccountIndex, DialogHeading, Notice, TierCards, WordGrid } from './parts';
import { MirrorRow, SandboxCard } from './signing';

const FIELD = 'text-[12.5px] font-semibold text-foreground';
const noop = () => {};

export type LaunchState = 'ready' | 'waiting' | 'blocked' | 'cancelled' | 'closed' | 'timeout';

/** The complete Add key dialog with Protected chosen for a seed-phrase wallet: every field the
 *  dialog has, the secret parts handed to the secure window. */
export function ProtectedHdDialog({ source, state = 'ready' }: { source: 'create' | 'import'; state?: LaunchState }) {
  const { t } = useTranslation();
  const d = useDraft();
  const busy = state === 'waiting';
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <DialogHeading icon={KeyRound} title={t('vaults.dialog.title')} subtitle={t('vaults.dialog.subtitle')} />
      <div className="flex flex-col gap-1.5">
        <span className={FIELD}>{t('vaults.dialog.kindLabel')}</span>
        <SegmentedRadio value="keypair" onChange={noop} ariaLabel={t('vaults.dialog.kindLabel')} options={[
          { id: 'static', label: t('vaults.dialog.kindStatic'), icon: KeyRound },
          { id: 'keypair', label: t('vaults.dialog.kindKeypair'), icon: Signature },
        ]} />
      </div>
      <label className="flex flex-col gap-1.5">
        <span className={FIELD}>{t('vaults.dialog.name')}</span>
        <Input readOnly value="COLD_WALLET" className="font-mono" />
        <span className="text-[11.5px] text-muted">{t('vaults.dialog.nameHint')}</span>
      </label>
      <div className="flex flex-col gap-1.5">
        <span className={FIELD}>{t('vaults.dialog.chainLabel')}</span>
        <SegmentedRadio value="mnemonic" onChange={noop} ariaLabel={t('vaults.dialog.chainLabel')} options={[
          { id: 'evm-bitcoin', label: t('vaults.dialog.chainEvmBitcoin') },
          { id: 'solana', label: t('vaults.dialog.chainSolana') },
          { id: 'mnemonic', label: t('vaults.hd.chainMnemonic') },
        ]} />
        <span className="text-[11.5px] text-muted">{t('vaults.hd.chainMnemonicHint')}</span>
      </div>
      <div className="flex flex-col gap-2.5">
        <KeySourceTabs value={source === 'create' ? 'generate' : 'import'} onChange={noop} />
        <HintLine
          text={source === 'create'
            ? d('在安全窗口里生成 12 个词，抄下来后点 3 个词核对；Passphrase 也在那里设置（可选）。', 'The secure window makes 12 words, you write them down and tap 3 to check; a passphrase is set there too (optional).')
            : d('在安全窗口里粘贴助记词和 Passphrase（如有），核对账户 #0 后保存。', 'Paste the words and passphrase (if any) in the secure window, compare account #0, then save.')}
          info={d('这个页面、Avibe 和这台电脑都看不到助记词和 Passphrase。保存时要用你的通行密钥。', 'This page, Avibe and this computer never see the words or the passphrase. Saving needs your passkey.')}
        />
      </div>
      <label className="flex flex-col gap-1.5">
        <span className={FIELD}>{t('vaults.dialog.description')}</span>
        <Input readOnly value="" placeholder={t('vaults.dialog.descriptionPlaceholder')} />
      </label>
      <div className="flex flex-col gap-1.5">
        <span className={FIELD}>{t('vaults.dialog.tags')}</span>
        <TagInput values={[]} onChange={noop} placeholder={t('vaults.dialog.tagsPlaceholder')} />
        <span className="text-[11px] text-muted">{t('vaults.dialog.tagsHelp')}</span>
      </div>
      <TierCards value="protected" />
      {/* No ask switch on this tier: every signature is approved in the secure window. */}
      <div className="flex items-center gap-2 rounded-[10px] border border-edge bg-surface-2 px-3 py-2.5 text-[12.5px] text-muted shadow-elevation-0">
        <Lock className="size-3.5 shrink-0" />
        {d('保护档：每个账户的每次签名，都要你在安全窗口里用通行密钥批准。', 'Protected: every signature by every account is approved in the secure window with your passkey.')}
      </div>
      <LaunchNotice state={state} />
      <div className="mt-1 flex items-center justify-end gap-2">
        <Button type="button" variant="ghost">{t('vaults.dialog.cancel')}</Button>
        <Button type="button" disabled={busy}>
          {busy ? <Loader2 className="size-4 animate-spin" /> : <ExternalLink className="size-4" />}
          {busy ? d('等安全窗口完成…', 'Waiting for the secure window…') : source === 'create' ? d('在安全窗口中创建', 'Create in secure window') : d('在安全窗口中导入', 'Import in secure window')}
        </Button>
      </div>
    </div>
  );
}

function LaunchNotice({ state }: { state: LaunchState }) {
  const d = useDraft();
  if (state === 'ready') return null;
  if (state === 'waiting') return <Notice tone="neutral" icon={ShieldCheck}>{d('安全窗口已打开。在那里完成后，这里会自动显示结果；什么都不会在这个页面输入。', 'The secure window is open. When you finish there, the result shows here; nothing is typed on this page.')}</Notice>;
  if (state === 'blocked') return <Notice tone="warning" icon={TriangleAlert}>{d('浏览器拦住了安全窗口。允许这个网站弹出窗口后再点一次。', 'The browser blocked the secure window. Allow pop-ups for this site and try again.')}</Notice>;
  if (state === 'cancelled') return <Notice tone="neutral" icon={AlertTriangle}>{d('已取消，什么都没有保存。可以再试一次。', 'Cancelled; nothing was saved. You can try again.')}</Notice>;
  if (state === 'closed') return <Notice tone="neutral" icon={AlertTriangle}>{d('安全窗口关了，什么都没有保存。可以再试一次。', 'The secure window was closed; nothing was saved. You can try again.')}</Notice>;
  return <Notice tone="neutral" icon={AlertTriangle}>{d('超过 5 分钟没有完成，什么都没有保存。再试一次会重新开始。', 'It wasn’t finished within 5 minutes; nothing was saved. Trying again starts over.')}</Notice>;
}

/** The secure window's steps (vault-sandbox draws them; product tokens here for review). */
export function WindowWords() {
  const d = useDraft();
  return (
    <SandboxCard title={d('抄下助记词', 'Write the words down')} subtitle="COLD_WALLET · 1/4" actions={[d('取消', 'Cancel'), d('我已抄好', 'I wrote them down')]}>
      <span className="text-[12.5px] leading-snug text-muted">{d('只在这个安全窗口里出现，保存后不会再显示。这台电脑丢了或通行密钥丢了，只能靠它们（和 Passphrase，如有）找回。', 'They appear only in this secure window and aren’t shown again after saving. If this computer or your passkey is lost, only they (and the passphrase, if any) recover the wallet.')}</span>
      <WordGrid words={CREATED_WORDS} columns={2} />
    </SandboxCard>
  );
}

export function WindowCheck({ wrong = false }: { wrong?: boolean }) {
  const d = useDraft();
  const quiz = [{ position: 3, options: ['thank', 'theme', 'legal'] }, { position: 7, options: ['wave', 'useful', 'worth'] }, { position: 12, options: ['yellow', 'year', 'winner'] }];
  const chosen: Record<number, string> = wrong ? { 3: 'thank', 7: 'useful' } : { 3: 'thank', 7: 'worth', 12: 'yellow' };
  return (
    <SandboxCard title={d('核对', 'Check')} subtitle="COLD_WALLET · 2/4" actions={[d('返回', 'Back'), d('下一步', 'Next')]}>
      <span className="text-[12.5px] text-muted">{d('看着你的纸，选出这 3 个词。', 'Using your paper, pick these 3 words.')}</span>
      {quiz.map((q) => (
        <div key={q.position} className="flex flex-col gap-1.5">
          <span className="text-[12px] text-muted">{d(`第 ${q.position} 个词`, `Word #${q.position}`)}</span>
          <div className="grid grid-cols-3 gap-1.5">
            {q.options.map((o) => {
              const picked = chosen[q.position] === o;
              const bad = picked && wrong && q.position === 7;
              return <span key={o} className={`rounded-md border px-2 py-1.5 text-center font-mono text-[12.5px] ${bad ? 'border-destructive/50 bg-destructive/10 text-destructive-ink' : picked ? 'border-mint bg-mint-soft text-mint-ink' : 'border-edge bg-field text-foreground'}`}>{o}</span>;
            })}
          </div>
        </div>
      ))}
      {wrong ? <span role="alert" className="text-[12px] text-destructive-ink">{d('第 7 个词不对，对照纸再看一遍。', 'Word #7 doesn’t match; check it against your paper.')}</span> : null}
    </SandboxCard>
  );
}

export function WindowPassphrase({ mode, state }: { mode: 'create' | 'import'; state: 'empty' | 'match' | 'mismatch' }) {
  const d = useDraft();
  return (
    <SandboxCard title={d('Passphrase（可选）', 'Passphrase (optional)')} subtitle={`COLD_WALLET · ${mode === 'create' ? '3/4' : '2/3'}`} actions={[d('返回', 'Back'), state === 'empty' ? d('不设置，下一步', 'Skip, next') : d('下一步', 'Next')]}>
      <span className="text-[12.5px] leading-snug text-muted">
        {mode === 'create'
          ? d('Passphrase 和这些词一起决定钱包的地址。恢复时需要词加上一字不差的 Passphrase；忘了它，钱包就找不回来。大多数人不需要。', 'The passphrase and the words together decide the addresses. Recovery needs the words plus this exact passphrase; forget it and the wallet can’t be recovered. Most people don’t need one.')
          : d('只有原钱包用过才填。填错不会报错，只会得到不同的地址，下一步核对。', 'Only if your old wallet used one. A wrong passphrase gives no error, just different addresses; you compare them next.')}
      </span>
      <MirrorRow label="Passphrase" value={state === 'empty' ? '' : '••••••••••••••••••'} />
      <MirrorRow label={d('再输入一次', 'Enter it again')} value={state === 'empty' ? '' : state === 'match' ? '••••••••••••••••••' : '•••••••••••••••••'} />
      {state === 'mismatch' ? <span role="alert" className="text-[12px] text-destructive-ink">{d('两次输入不一样。', 'The two entries don’t match.')}</span> : state === 'match' ? <span className="text-[12px] text-mint-ink">{d('两次输入一致。区分大小写和空格。', 'They match. Case and spaces count.')}</span> : null}
    </SandboxCard>
  );
}

export function WindowReview({ mode }: { mode: 'create' | 'import' }) {
  const d = useDraft();
  const a = mode === 'create' ? CREATED_ADDRESSES[0] : IMPORTED_ADDRESSES[0];
  return (
    <SandboxCard title={mode === 'create' ? d('保存钱包', 'Save the wallet') : d('和原钱包核对，然后保存', 'Compare, then save')} subtitle={`COLD_WALLET · ${mode === 'create' ? '4/4' : '3/3'}`} actions={[d('返回', 'Back'), d('用通行密钥保存', 'Save with passkey')]}>
      <span className="text-[12.5px] text-muted">{mode === 'create' ? d('账户 #0 的地址：', 'Account #0’s addresses:') : d('应该和原钱包的第一个账户一样：', 'These should match your old wallet’s first account:')}</span>
      <MirrorRow label="ETH" value={a.eth} />
      <MirrorRow label="BTC · Taproot" value={a.btc_taproot} />
      <MirrorRow label="Solana" value={a.solana} />
      <span className="flex items-center gap-1.5 text-[12px] text-muted"><Lock className="size-3.5" />{d('保存时会再用一次通行密钥（和解锁是两次不同的确认）。保存后会重新打开核对一遍再交给 Avibe。', 'Saving asks for your passkey once more (separate from unlocking). It reopens and checks the result before handing it to Avibe.')}</span>
    </SandboxCard>
  );
}

export function WindowImport() {
  const d = useDraft();
  return (
    <SandboxCard title={d('导入助记词', 'Import a seed phrase')} subtitle="COLD_WALLET · 1/3" actions={[d('取消', 'Cancel'), d('下一步', 'Next')]}>
      <span className="text-[12.5px] leading-snug text-muted">{d('在这里粘贴，Avibe 和这台电脑都看不到。', 'Paste it here; Avibe and this computer never see it.')}</span>
      <WordGrid words={IMPORTED_WORDS} columns={3} />
      <span className="flex items-center gap-1.5 text-[12px] font-medium text-mint-ink"><CircleCheck className="size-3.5" />{d('12 个词 · 校验通过', '12 words · checksum OK')}</span>
    </SandboxCard>
  );
}

/** Back on the page: the wallet as saved, its #0 public addresses, and honest recovery copy. */
export function ProtectedDone() {
  const d = useDraft();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-col items-center gap-2 pt-2 text-center">
        <span className="flex size-12 items-center justify-center rounded-2xl bg-mint-soft text-mint-ink"><ShieldCheck className="size-6" /></span>
        <span className="text-[16px] font-semibold text-foreground">{d('COLD_WALLET 已创建（保护档）', 'COLD_WALLET is ready (Protected)')}</span>
        <span className="max-w-sm text-[12.5px] leading-snug text-muted">{d('账户 #0 的地址如下。每次签名都要你在安全窗口里批准。', 'Account #0’s addresses are below. Every signature is approved in the secure window.')}</span>
      </div>
      <div className="flex flex-col gap-2 rounded-[10px] border border-edge bg-surface-2 p-3">
        <span className="flex items-center gap-2 text-[12.5px] font-semibold text-foreground"><AccountIndex index={0} />{d('主账户', 'Main')}</span>
        <HdAccountAddressList account={CREATED_ADDRESSES[0]} />
      </div>
      <Notice tone="warning" icon={TriangleAlert}>
        {d('通行密钥丢了，靠这些词和原来的 Passphrase（如有）仍能找回钱包。但有词和 Passphrase 的人，可以在 Avibe 之外直接动用资金。只靠数据库备份无法恢复签名。', 'If the passkey is lost, the words plus the original passphrase (if any) still recover the wallet. But anyone with them can move the funds outside Avibe. A database backup alone cannot restore signing.')}
      </Notice>
    </div>
  );
}

/** A protected wallet's account card: addresses (Taproot only for BTC), no ask switch. */
export function ProtectedAccountCard({ index, name }: { index: number; name: string }) {
  const d = useDraft();
  const a = CREATED_ADDRESSES[index];
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-[10px] border border-edge bg-surface p-3 shadow-elevation-0">
      <div className="flex min-w-0 items-center gap-2">
        <AccountIndex index={index} />
        <span className="truncate text-[13.5px] font-semibold text-foreground">{name}</span>
        <span className="ml-auto flex items-center gap-1 text-[11.5px] text-muted"><Lock className="size-3" />{d('每次签名都批准', 'Approve every signature')}</span>
      </div>
      <HdAccountAddressList account={a} />
    </div>
  );
}

/** Signing a Bitcoin transaction from a protected wallet account: Taproot, account and path. */
export function WindowSignBitcoin() {
  const d = useDraft();
  return (
    <SandboxCard title={d('签名保护档操作', 'Sign protected operation')} subtitle="COLD_WALLET" actions={[d('取消', 'Cancel'), d('确认签名', 'Confirm signature')]}>
      <span className="text-[15px] font-semibold text-foreground">{d('Bitcoin · Taproot 签名', 'Bitcoin · Taproot signature')}</span>
      <MirrorRow label={d('签名地址', 'Signing address')} value={CREATED_ADDRESSES[1].btc_taproot} />
      <MirrorRow label={d('钱包账户', 'Wallet account')} value="#1 · m/86'/0'/0'/0/1 · Taproot" added />
      <MirrorRow label={d('待签摘要', 'Digest to sign')} value="9f2c41d07be3a8156c0e4d92b7a1f3e85d6c20a4b9e7f1c3d8a5062e4b7c19f0" />
      <span className="text-[12px] leading-snug text-muted">{d('这是交易的摘要，这里看不出收款方和金额。只批准你知道来源的请求。', 'This is the transaction’s digest; it doesn’t show the recipient or amount. Approve only requests you know the origin of.')}</span>
    </SandboxCard>
  );
}

export function ProtectedAccountCards() {
  const d = useDraft();
  return (
    <div className="flex min-h-full flex-col gap-2.5 bg-background p-6">
      <ProtectedAccountCard index={0} name={d('主账户', 'Main')} />
      <ProtectedAccountCard index={1} name={d('冷钱包 1', 'Cold 1')} />
    </div>
  );
}
