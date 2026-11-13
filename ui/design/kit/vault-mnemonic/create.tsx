// The protected-tier seed-phrase wallet hand-off (ui/design: vault/mnemonic/create/protected-*), still a
// proposal: the words are generated or typed in the sandbox's own window (vault-sandbox, after 3.2.6).
// The standard-tier steps have shipped (src/components/ui/vault-hd-wallet-steps.tsx).
import { ArrowLeft, KeyRound, Lock, ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { KeySourceTabs } from '@/components/ui/vault-key-builder-parts';

import { useDraft } from '../draft';
import { DialogHeading, HintLine, Notice, TierCards } from './parts';

export type Source = 'create' | 'import';

export function SourceCards({ value }: { value: Source }) {
  const d = useDraft();
  return (
    <div className="flex flex-col gap-2.5">
      <KeySourceTabs value={value === 'create' ? 'generate' : 'import'} onChange={() => {}} />
      {value === 'create' ? (
        <HintLine
          text={d('下一步生成 12 个词，抄下来后点 3 个词核对。', 'Next you get 12 words; write them down, then tap 3 of them to check.')}
          info={d('推荐给 Agent 用的新钱包。标准档的种子由这台电脑上的 avault 加密保存；保存后 Avibe 不会再显示这些词。', 'Best for a new wallet your Agents use. On Standard, avault on this computer keeps the seed encrypted; Avibe won’t show the words again after saving.')}
        />
      ) : (
        <HintLine
          text={d('粘贴后逐词检查，并显示账户 #0 的地址，可以先核对再创建。', 'Each word is checked as you paste, and account #0’s addresses show so you can compare before creating.')}
          info={d('从 MetaMask、Phantom 等钱包搬过来：12 或 24 个英文单词，可选 BIP39 密码。助记词只在这台电脑上处理，不会进入聊天或模型。', 'Bring one over from MetaMask, Phantom and others: 12 or 24 English words, with an optional BIP39 passphrase. The phrase stays on this computer and never reaches a chat or a model.')}
        />
      )}
    </div>
  );
}

export function Footer({ back, primary, disabled = false, primaryIcon }: { back?: string; primary: string; disabled?: boolean; primaryIcon?: React.ReactNode }) {
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

/** The Add dialog's shipped header: the wallet is one more kind of signing key, not a dialog of its own. */
export function WalletDialogHeading() {
  const { t } = useTranslation();
  return <DialogHeading icon={KeyRound} title={t('vaults.dialog.title')} subtitle={t('vaults.dialog.subtitle')} />;
}

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

