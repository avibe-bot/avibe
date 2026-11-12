// Creating a seed-phrase wallet in the Add dialog (ui/design: vault/mnemonic/create/*): set up,
// write the words down, check them, done. The words are shown once: on the standard tier avault
// never returns them, so the check is the one chance to catch a copying mistake.
import { ArrowLeft, Bot, Check, Copy, EyeOff, FileDown, Lock, Plus, ShieldCheck, Sparkles, WalletCards, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { SegmentedRadio } from '@/components/ui/segmented';
import { SigningAddressList } from '@/components/ui/signing-address-list';
import { Switch } from '@/components/ui/switch';
import { cn } from '@/lib/utils';

import { useDraft } from '../draft';
import { CREATED_ADDRESSES, CREATED_WORDS } from './data';
import { CommandLine, DialogHeading, FIELD_LABEL, Notice, Stepper, TierCards, WordGrid } from './parts';

export type TypePicker = 'kind' | 'chain';
export type Source = 'create' | 'import';

/** Where the wallet type sits: a third Type (A, recommended) or a third Chain (B). */
export function TypeRow({ picker }: { picker: TypePicker }) {
  const { t } = useTranslation();
  const d = useDraft();
  if (picker === 'kind') {
    return (
      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.kindLabel')}</span>
        <SegmentedRadio
          value="mnemonic"
          onChange={() => {}}
          ariaLabel={t('vaults.dialog.kindLabel')}
          options={[
            { id: 'static', label: t('vaults.dialog.kindStatic') },
            { id: 'keypair', label: t('vaults.dialog.kindKeypair') },
            { id: 'mnemonic', label: d('助记词钱包', 'Seed phrase wallet') },
          ]}
        />
        <span className="text-[11px] text-muted">
          {d('一组助记词覆盖 EVM、Bitcoin、Solana，可在里面添加多个账户。', 'One phrase covers EVM, Bitcoin and Solana, with as many accounts as you add.')}
        </span>
      </div>
    );
  }
  return (
    <>
      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.kindLabel')}</span>
        <SegmentedRadio
          value="keypair"
          onChange={() => {}}
          ariaLabel={t('vaults.dialog.kindLabel')}
          options={[
            { id: 'static', label: t('vaults.dialog.kindStatic') },
            { id: 'keypair', label: t('vaults.dialog.kindKeypair') },
          ]}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{t('vaults.dialog.chainLabel')}</span>
        <SegmentedRadio
          value="mnemonic"
          onChange={() => {}}
          ariaLabel={t('vaults.dialog.chainLabel')}
          options={[
            { id: 'evm-bitcoin', label: t('vaults.dialog.chainEvmBitcoin') },
            { id: 'solana', label: t('vaults.dialog.chainSolana') },
            { id: 'mnemonic', label: d('助记词 · 多链', 'Seed phrase · all') },
          ]}
        />
      </div>
    </>
  );
}

/** New words or words from another wallet: two cards, so the choice reads as two different jobs. */
export function SourceCards({ value }: { value: Source }) {
  const d = useDraft();
  const cards = [
    { key: 'create', icon: Sparkles, title: d('新建助记词', 'New phrase'), desc: d('生成 12 个词。推荐给 Agent 用的新钱包。', 'Generate 12 words. Best for a new wallet your Agents use.') },
    { key: 'import', icon: FileDown, title: d('导入已有助记词', 'Import a phrase'), desc: d('从 MetaMask、Phantom 等钱包搬过来。', 'Bring one over from MetaMask, Phantom and others.') },
  ] as const;
  return (
    <div className="flex flex-col gap-1.5">
      <span className={FIELD_LABEL}>{d('助记词', 'Seed phrase')}</span>
      <div className="grid grid-cols-2 gap-2.5">
        {cards.map(({ key, icon: Icon, title, desc }) => {
          const selected = value === key;
          return (
            <button
              key={key}
              type="button"
              aria-pressed={selected}
              className={cn(
                'flex flex-col gap-1.5 rounded-[10px] border p-3 text-left',
                selected ? 'border-[1.5px] border-mint bg-mint-soft' : 'border-edge bg-field',
              )}
            >
              <span className="flex items-center gap-2">
                <Icon className={cn('size-4', selected ? 'text-mint-ink' : 'text-muted')} />
                <span className="flex-1 text-[13.5px] font-semibold text-foreground">{title}</span>
                {selected && <Check className="size-4 text-mint-ink" />}
              </span>
              <span className="text-[11.5px] leading-snug text-muted">{desc}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function NameField({ value }: { value: string }) {
  const { t } = useTranslation();
  return (
    <label className="flex flex-col gap-1.5">
      <span className={FIELD_LABEL}>{t('vaults.dialog.name')}</span>
      <Input value={value} readOnly className="font-mono" />
    </label>
  );
}

function AskToggle({ checked }: { checked: boolean }) {
  const d = useDraft();
  return (
    <div className="flex items-start gap-3 rounded-[10px] border border-edge bg-surface-2 px-3 py-3 shadow-elevation-0">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-[13px] font-semibold text-foreground">{d('每次签名都要我确认', 'Ask me before every signature')}</span>
        <span className="text-[11.5px] leading-snug text-muted">
          {d('默认 Agent 可以直接签名。之后也能按账户单独设置。', 'By default Agents sign on their own. You can also set this per account later.')}
        </span>
      </div>
      <Switch checked={checked} onCheckedChange={() => {}} label={d('每次签名都要我确认', 'Ask me before every signature')} />
    </div>
  );
}

function Footer({ back, primary, disabled = false, primaryIcon }: { back?: string; primary: string; disabled?: boolean; primaryIcon?: React.ReactNode }) {
  const { t } = useTranslation();
  return (
    <div className="mt-1 flex items-center justify-end gap-2">
      {back ? (
        <Button type="button" variant="ghost" className="mr-auto">
          <ArrowLeft className="size-4" />
          {back}
        </Button>
      ) : (
        <Button type="button" variant="ghost">{t('vaults.dialog.cancel')}</Button>
      )}
      <Button type="button" disabled={disabled}>
        {primaryIcon}
        {primary}
      </Button>
    </div>
  );
}

export function WalletDialogHeading() {
  const d = useDraft();
  return (
    <DialogHeading
      icon={WalletCards}
      title={d('添加助记词钱包', 'Add a seed phrase wallet')}
      subtitle={d('一组助记词，多条链、多个账户。Agent 签名，你掌握备份。', 'One phrase for every chain and account. Agents sign; you keep the backup.')}
    />
  );
}

/** Step 1: the Add dialog with the wallet type chosen. */
export function CreateSetup({ picker = 'kind', tier = 'standard' }: { picker?: TypePicker; tier?: 'standard' | 'protected' }) {
  const d = useDraft();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <WalletDialogHeading />
      <TypeRow picker={picker} />
      <NameField value="AGENT_WALLET" />
      <SourceCards value="create" />
      <TierCards value={tier} />
      {tier === 'standard' ? <AskToggle checked={false} /> : null}
      <Footer primary={d('生成助记词', 'Generate the words')} primaryIcon={<Sparkles className="size-4" />} />
    </div>
  );
}

/** Step 2: the words, hidden until asked for, then shown once. */
export function CreateBackup({ hidden = false, columns = 3, tier = 'standard' }: { hidden?: boolean; columns?: 2 | 3; tier?: 'standard' | 'protected' }) {
  const d = useDraft();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <WalletDialogHeading />
      <Stepper step={2} />
      <div className="flex flex-col gap-1">
        <span className="text-[15px] font-semibold text-foreground">{d('按顺序抄下这 12 个词', 'Write these 12 words down, in order')}</span>
        <span className="text-[12.5px] leading-snug text-muted">
          {tier === 'standard'
            ? d('保存后 Avibe 不会再显示它们。这台电脑坏了或要换到别的钱包时，只能靠这份备份。', 'Avibe won’t show them again after saving. If this computer fails or you move wallets, this backup is the only way back.')
            : d('它们只显示在这个安全窗口里。保存后不会再显示。', 'They appear only in this secure window and are not shown again after saving.')}
        </span>
      </div>
      <WordGrid words={CREATED_WORDS} hidden={hidden} columns={columns} />
      {!hidden ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="secondary" size="sm">
            <Copy className="size-3.5" />
            {d('复制', 'Copy')}
          </Button>
          <Button type="button" variant="ghost" size="sm">
            <EyeOff className="size-3.5" />
            {d('隐藏', 'Hide')}
          </Button>
          <span className="text-[11px] text-muted">{d('复制后其他应用可能读到剪贴板，写在纸上更安全。', 'Other apps can read the clipboard; paper is safer.')}</span>
        </div>
      ) : null}
      <Notice tone="violet" icon={Bot}>
        {d('Agent 签名不需要这些词，它们只用来恢复。不要发给任何 Agent 或聊天。', 'Agents never need these words to sign; they are only for recovery. Never send them to an Agent or a chat.')}
      </Notice>
      <Footer back={d('上一步', 'Back')} primary={d('我已抄好', 'I wrote them down')} disabled={hidden} />
    </div>
  );
}

export type CheckState = 'empty' | 'correct' | 'wrong';

/** One question of the check: which word sits at this position. Tapping, never typing. */
function Question({ position, options, answer, chosen }: { position: number; options: string[]; answer: string; chosen?: string }) {
  const d = useDraft();
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[12.5px] text-muted">{d(`第 ${position} 个词`, `Word #${position}`)}</span>
      <div className="grid grid-cols-3 gap-1.5">
        {options.map((option) => {
          const picked = chosen === option;
          const right = picked && option === answer;
          const wrong = picked && option !== answer;
          return (
            <button
              key={option}
              type="button"
              aria-pressed={picked}
              className={cn(
                'flex items-center justify-center gap-1.5 rounded-lg border px-2 py-2 font-mono text-[13px] font-semibold',
                right ? 'border-[1.5px] border-mint bg-mint-soft text-mint-ink'
                  : wrong ? 'border-[1.5px] border-destructive bg-destructive/10 text-destructive-ink'
                    : 'border-edge bg-field text-foreground',
              )}
            >
              {right ? <Check className="size-3.5" /> : wrong ? <X className="size-3.5" /> : null}
              {option}
            </button>
          );
        })}
      </div>
    </div>
  );
}

const QUESTIONS = [
  { position: 3, options: ['thank', 'theme', 'legal'], answer: 'thank' },
  { position: 7, options: ['wave', 'useful', 'worth'], answer: 'worth' },
  { position: 12, options: ['yellow', 'year', 'winner'], answer: 'yellow' },
];

/** Step 3, version A: three quick taps against the paper copy. */
export function CreateCheckQuiz({ state = 'empty' }: { state?: CheckState }) {
  const d = useDraft();
  const chosen = state === 'empty' ? [] : state === 'correct' ? ['thank', 'worth', 'yellow'] : ['thank', 'useful'];
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <WalletDialogHeading />
      <Stepper step={3} />
      <div className="flex flex-col gap-1">
        <span className="text-[15px] font-semibold text-foreground">{d('看着你的纸，选出这 3 个词', 'Using your paper, pick these 3 words')}</span>
        <span className="text-[12.5px] leading-snug text-muted">{d('只用点 3 下，确认备份能用。', 'Three taps to prove the backup works.')}</span>
      </div>
      <div className="flex flex-col gap-3">
        {QUESTIONS.map((q, index) => <Question key={q.position} {...q} chosen={chosen[index]} />)}
      </div>
      {state === 'wrong' ? (
        <Notice tone="error" icon={X}>
          <span className="font-semibold text-destructive-ink">{d('第 7 个词对不上。', 'Word #7 doesn’t match.')}</span>{' '}
          {d('点「再看一遍」，对照纸上的第 7 个词，可能抄错了。', 'Tap “See the words” and compare word #7 on your paper; it may be miscopied.')}
        </Notice>
      ) : null}
      <Footer
        back={d('再看一遍', 'See the words')}
        primary={d('创建钱包', 'Create wallet')}
        disabled={state !== 'correct'}
        primaryIcon={<WalletCards className="size-4" />}
      />
    </div>
  );
}

/** Step 3, version B: one acknowledgement, as Phantom does. */
export function CreateCheckAcknowledge({ checked = true }: { checked?: boolean }) {
  const d = useDraft();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <WalletDialogHeading />
      <Stepper step={3} />
      <div className="flex flex-col gap-1">
        <span className="text-[15px] font-semibold text-foreground">{d('确认你已保存备份', 'Confirm your backup')}</span>
        <span className="text-[12.5px] leading-snug text-muted">{d('保存后 Avibe 不会再显示这些词。', 'Avibe won’t show these words again after saving.')}</span>
      </div>
      <WordGrid words={CREATED_WORDS} hidden columns={3} />
      <label className="flex items-start gap-2.5 rounded-[10px] border border-edge bg-surface-2 px-3 py-3">
        <Checkbox checked={checked} presentational />
        <span className="text-[13px] leading-snug text-foreground">{d('我已按顺序把 12 个词抄在安全的地方', 'I wrote all 12 words down, in order, somewhere safe')}</span>
      </label>
      <Footer back={d('再看一遍', 'See the words')} primary={d('创建钱包', 'Create wallet')} disabled={!checked} primaryIcon={<WalletCards className="size-4" />} />
    </div>
  );
}

/** Done: account #0 is ready, and the user learns how to point an Agent at it. */
export function CreateDone() {
  const d = useDraft();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-col items-center gap-2 pt-2 text-center">
        <span className="flex size-12 items-center justify-center rounded-2xl bg-mint-soft text-mint-ink">
          <Check className="size-6" strokeWidth={2.5} />
        </span>
        <span className="text-[16px] font-semibold text-foreground">{d('AGENT_WALLET 已创建', 'AGENT_WALLET is ready')}</span>
        <span className="max-w-sm text-[12.5px] leading-snug text-muted">
          {d('账户 #0 在三条链上的地址如下。往这些地址充值后，Agent 就能用它签名。', 'Account #0 has these addresses on the three chains. Fund them and your Agents can sign with it.')}
        </span>
      </div>
      <div className="flex flex-col gap-2 rounded-[10px] border border-edge bg-surface-2 p-3">
        <span className="flex items-center gap-2 text-[12.5px] font-semibold text-foreground">
          <span className="rounded-md bg-violet-soft px-1.5 font-mono text-[11.5px] text-violet-ink">#0</span>
          {d('主账户', 'Main')}
        </span>
        <SigningAddressList addresses={CREATED_ADDRESSES[0]} />
      </div>
      <div className="flex flex-col gap-1.5">
        <span className="flex items-center gap-1.5 text-[12.5px] font-medium text-foreground">
          <Bot className="size-3.5 text-muted" />
          {d('Agent 这样用它', 'How an Agent uses it')}
        </span>
        <CommandLine>vibe vault sign AGENT_WALLET --chain solana --account 0 …</CommandLine>
        <span className="text-[11px] text-muted">{d('--chain 选 evm、bitcoin 或 solana；--account 选账户编号。', '--chain picks evm, bitcoin or solana; --account picks the account number.')}</span>
      </div>
      <div className="mt-1 flex justify-end gap-2">
        <Button type="button" variant="ghost">
          <Plus className="size-4" />
          {d('再添加一个账户', 'Add another account')}
        </Button>
        <Button type="button">{d('完成', 'Done')}</Button>
      </div>
    </div>
  );
}

/** The protected tier: the parent dialog hands the words to the sandbox's own window. */
export function ProtectedHandoff({ source }: { source: Source }) {
  const d = useDraft();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <WalletDialogHeading />
      <SourceCards value={source} />
      <TierCards value="protected" />
      <Notice tone="success" icon={ShieldCheck}>
        <span className="font-semibold">{source === 'create' ? d('助记词会在安全窗口里生成和显示。', 'The words are generated and shown in the secure window.') : d('在安全窗口里输入助记词。', 'You type the words in the secure window.')}</span>{' '}
        {d('这个页面、Avibe 和这台电脑都看不到它们；之后每次签名都要你的通行密钥。', 'This page, Avibe and this computer never see them; every signature then needs your passkey.')}
      </Notice>
      <Footer
        primary={source === 'create' ? d('在安全窗口中生成', 'Generate in secure window') : d('在安全窗口中导入', 'Import in secure window')}
        primaryIcon={<Lock className="size-4" />}
      />
    </div>
  );
}

