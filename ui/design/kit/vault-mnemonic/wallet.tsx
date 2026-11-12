// The wallet item (ui/design: vault/mnemonic/wallet/*): a compact row in the Vault list, and a
// detail view with every account. An account is one index shared by the three chains, as Phantom
// groups them; the user names it, sees which Session last signed with it, and decides per account
// whether an Agent may sign without asking. Adding an account takes no words: all 20 accounts'
// public keys were computed when the wallet was saved.
import { Bot, ChevronRight, Clock3, Layers, MoreHorizontal, Pencil, Plus, Sparkles, WalletCards } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { CopyButton } from '@/components/ui/copy-button';
import { InlineNameInput } from '@/components/ui/inline-name-input';
import { SegmentedRadio } from '@/components/ui/segmented';
import { SigningAddressList, SigningAddressRow } from '@/components/ui/signing-address-list';
import { Switch } from '@/components/ui/switch';
import { cn } from '@/lib/utils';

import { useDraft } from '../draft';
import { IMPORTED_ADDRESSES, WALLET_ACCOUNTS, addressOf, type MnemonicChain, type WalletAccount } from './data';
import { AccountIndex, CommandLine, useChainLabel } from './parts';

const ago = (minutes: number, d: (zh: string, en: string) => string) =>
  minutes < 60 ? d(`${minutes} 分钟前`, `${minutes} min ago`)
    : minutes < 60 * 24 ? d(`${Math.round(minutes / 60)} 小时前`, `${Math.round(minutes / 60)} h ago`)
      : d(`${Math.round(minutes / 1440)} 天前`, `${Math.round(minutes / 1440)} d ago`);

function WalletBadges({ accounts }: { accounts: number }) {
  const { t } = useTranslation();
  const d = useDraft();
  return (
    <>
      <Badge variant="secondary">{t('vaults.standard')}</Badge>
      <Badge variant="outline" className="border-violet/40 bg-violet-soft text-violet-ink">
        <Layers className="size-3" />
        {d(`助记词 · ${accounts} 个账户`, `Seed phrase · ${accounts} accounts`)}
      </Badge>
    </>
  );
}

/** The wallet's row in the Vault list: accounts at a glance, the detail one click away. */
export function WalletRow({ accounts = WALLET_ACCOUNTS }: { accounts?: WalletAccount[] }) {
  const d = useDraft();
  const chainLabel = useChainLabel();
  return (
    <li className="flex list-none flex-wrap items-center gap-x-3.5 gap-y-2 rounded-xl border border-edge bg-surface px-4 py-3 shadow-elevation-0 md:flex-nowrap">
      <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-violet/10 text-violet-ink">
        <WalletCards className="size-4" />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-sm font-semibold">AGENT_WALLET</span>
          <WalletBadges accounts={accounts.length} />
        </div>
        <span className="truncate text-xs text-muted">
          {d('给交易和测试 Agent 用的热钱包', 'Hot wallet for trading and test Agents')} · {d('最近签名 2 小时前', 'Last signed 2 h ago')}
        </span>
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          {accounts.map((account) => (
            <span key={account.index} className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-field py-0.5 pl-0.5 pr-2 text-[12px] text-foreground">
              <AccountIndex index={account.index} />
              {d(account.name, account.nameEn)}
            </span>
          ))}
          <span className="text-[11px] text-muted">{(['evm', 'bitcoin', 'solana'] as MnemonicChain[]).map(chainLabel).join(' · ')}</span>
        </div>
      </div>
      <div className="order-last flex w-full items-center gap-1.5 md:order-none md:w-auto md:shrink-0">
        <Badge variant="outline" className="mr-auto md:mr-0">crypto</Badge>
        <Button type="button" variant="secondary" size="sm">
          {d('查看账户', 'Accounts')}
          <ChevronRight className="size-3.5" />
        </Button>
        <Button type="button" variant="ghost" size="icon" aria-label={d('更多操作', 'More actions')}>
          <MoreHorizontal className="size-4" />
        </Button>
      </div>
    </li>
  );
}

export type AccountEditing = { index: number; kind: 'rename' | 'new' };

function AccountCard({ account, editing, fresh = false }: { account: WalletAccount; editing?: AccountEditing['kind']; fresh?: boolean }) {
  const d = useDraft();
  const chainLabel = useChainLabel();
  const name = d(account.name, account.nameEn);
  return (
    <div className={cn(
      'flex min-w-0 flex-col gap-2 rounded-[10px] border bg-surface p-3 shadow-elevation-0',
      fresh ? 'border-[1.5px] border-mint' : 'border-edge',
    )}>
      <div className="flex min-w-0 items-center gap-2">
        <AccountIndex index={account.index} />
        {editing ? (
          <span className="flex min-w-0 max-w-60 flex-1">
            <InlineNameInput
              initial={editing === 'rename' ? name : ''}
              placeholder={d('给这个账户起个名字', 'Name this account')}
              onCommit={() => {}}
              onCancel={() => {}}
            />
          </span>
        ) : (
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="truncate text-[13.5px] font-semibold text-foreground">{name}</span>
            <button type="button" aria-label={d('重命名', 'Rename')} className="text-muted hover:text-foreground">
              <Pencil className="size-3" />
            </button>
          </span>
        )}
        {fresh ? <Badge variant="success">{d('新添加', 'New')}</Badge> : null}
        <span className="ml-auto flex shrink-0 items-center gap-2">
          <span className="text-[11.5px] text-muted">{d('签名前问我', 'Ask before signing')}</span>
          <Switch size="sm" checked={!!account.ask} onCheckedChange={() => {}} label={d('签名前问我', 'Ask before signing')} />
          <Button type="button" variant="ghost" size="icon" className="size-7" aria-label={d('更多操作', 'More actions')}>
            <MoreHorizontal className="size-3.5" />
          </Button>
        </span>
      </div>
      <span className="flex min-w-0 items-center gap-1.5 text-[11.5px] text-muted">
        <Clock3 className="size-3 shrink-0" />
        <span className="truncate">
          {account.last
            ? d(`最近签名：${chainLabel(account.last.chain)} · ${ago(account.last.minutes, d)} · 会话「${account.last.session}」`, `Last signed: ${chainLabel(account.last.chain)} · ${ago(account.last.minutes, d)} · “${account.last.sessionEn}”`)
            : d('还没有签过名', 'No signatures yet')}
        </span>
      </span>
      <SigningAddressList addresses={account.addresses} />
    </div>
  );
}

function AgentUsage({ account = 1 }: { account?: number }) {
  const d = useDraft();
  return (
    <div className="flex min-w-0 flex-col gap-1.5 rounded-[10px] border border-edge bg-surface px-3 py-2.5 shadow-elevation-0">
      <span className="flex items-center gap-1.5 text-[12px] font-medium text-foreground">
        <Bot className="size-3.5 text-muted" />
        {d('Agent 用 --chain 选链，--account 选账户', 'Agents pick the chain with --chain and the account with --account')}
      </span>
      <div className="flex min-w-0 items-center gap-2">
        <CommandLine className="flex-1">{`vibe vault sign AGENT_WALLET --chain solana --account ${account} …`}</CommandLine>
        <CopyButton value={`vibe vault sign AGENT_WALLET --chain solana --account ${account}`} label={null} variant="ghost" />
      </div>
    </div>
  );
}

function DetailHeader({ accounts }: { accounts: number }) {
  const d = useDraft();
  return (
    <div className="flex min-w-0 items-start gap-3 pr-6">
      <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-violet/15 text-violet-ink">
        <WalletCards className="size-5" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-[15px] font-semibold text-foreground">AGENT_WALLET</span>
          <WalletBadges accounts={accounts} />
        </span>
        <span className="text-xs text-muted">
          {d(`导入的 12 词助记词 · 已用 ${accounts} / 20 个账户 · 账户编号与 MetaMask、Phantom 的账户顺序一致`, `Imported 12-word phrase · ${accounts} of 20 accounts · numbered as in MetaMask and Phantom`)}
        </span>
      </div>
    </div>
  );
}

function AddAccount({ next }: { next: number }) {
  const d = useDraft();
  return (
    <div className="flex flex-col items-start gap-1">
      <Button type="button" variant="ghost" className="w-full justify-center border border-dashed border-border-strong">
        <Plus className="size-4" />
        {d(`添加账户 #${next}`, `Add account #${next}`)}
      </Button>
      <span className="self-center text-[11px] text-muted">{d('不用再输入助记词；地址已经算好。', 'No words needed; the addresses are already computed.')}</span>
    </div>
  );
}

/** Version A (recommended): one card per account, its three addresses inside. */
export function WalletDetailAccounts({ accounts = WALLET_ACCOUNTS, editing }: { accounts?: WalletAccount[]; editing?: AccountEditing }) {
  const shown = editing?.kind === 'new'
    ? [...accounts, { index: accounts.length, name: '', nameEn: '', addresses: IMPORTED_ADDRESSES[accounts.length] }]
    : accounts;
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <DetailHeader accounts={shown.length} />
      <AgentUsage />
      <div className="flex flex-col gap-2.5">
        {shown.map((account) => (
          <AccountCard
            key={account.index}
            account={account}
            editing={editing?.index === account.index ? editing.kind === 'new' ? 'new' : 'rename' : undefined}
            fresh={editing?.kind === 'new' && editing.index === account.index}
          />
        ))}
      </div>
      <AddAccount next={shown.length} />
    </div>
  );
}

/** Version B: one chain at a time, every account's address for it in a compact list. */
export function WalletDetailChains({ accounts = WALLET_ACCOUNTS, chain = 'solana' }: { accounts?: WalletAccount[]; chain?: MnemonicChain }) {
  const d = useDraft();
  const chainLabel = useChainLabel();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <DetailHeader accounts={accounts.length} />
      <AgentUsage />
      <SegmentedRadio<MnemonicChain>
        value={chain}
        onChange={() => {}}
        ariaLabel={d('链', 'Chain')}
        options={(['evm', 'bitcoin', 'solana'] as MnemonicChain[]).map((id) => ({ id, label: chainLabel(id) }))}
      />
      <ul className="flex flex-col divide-y divide-border rounded-[10px] border border-edge bg-surface shadow-elevation-0">
        {accounts.map((account) => (
          <li key={account.index} className="flex min-w-0 flex-col gap-1 px-3 py-2.5">
            <span className="flex min-w-0 items-center gap-2">
              <AccountIndex index={account.index} />
              <span className="truncate text-[13px] font-semibold text-foreground">{d(account.name, account.nameEn)}</span>
              {account.ask ? <Badge variant="secondary" className="ml-auto">{d('签名前问我', 'Asks first')}</Badge> : null}
            </span>
            <SigningAddressRow label={chainLabel(chain)} value={addressOf(account.addresses, chain)} />
          </li>
        ))}
      </ul>
      <AddAccount next={accounts.length} />
    </div>
  );
}

/** Just created: one account, and why a second one is useful. */
export function WalletDetailFirst() {
  const d = useDraft();
  const first = { ...WALLET_ACCOUNTS[0], ask: false, last: undefined };
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <DetailHeader accounts={1} />
      <AgentUsage account={0} />
      <AccountCard account={first} />
      <div className="flex flex-col items-center gap-2 rounded-[10px] border border-dashed border-border-strong px-4 py-4 text-center">
        <span className="flex size-9 items-center justify-center rounded-xl bg-violet-soft text-violet-ink">
          <Sparkles className="size-4" />
        </span>
        <span className="text-[13px] font-semibold text-foreground">{d('给每个用途一个账户', 'One account per job')}</span>
        <span className="max-w-sm text-[12px] leading-snug text-muted">
          {d('比如交易 Agent 用 #1、空投测试用 #2：资金互不影响，也能单独决定是否每次问你。', 'For example #1 for a trading Agent and #2 for airdrop tests: funds stay apart, and each can ask you or not.')}
        </span>
        <Button type="button" variant="secondary" size="sm">
          <Plus className="size-3.5" />
          {d('添加账户 #1', 'Add account #1')}
        </Button>
      </div>
    </div>
  );
}

