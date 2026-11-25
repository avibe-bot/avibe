import { useTranslation } from 'react-i18next';
import { ArrowLeft, ExternalLink, Laptop, RefreshCw, Terminal } from 'lucide-react';
import { Button } from '../components/ui/button';

export interface AddInstanceViewProps { onBack: () => void; onRefresh: () => void; busy: boolean }

/** ui/design: desktop/instances/add-*. Supported setup: README.md and
 * skills/use-avibe/references/remote-access.md. Never issues pairing or deployment actions. */
export function AddInstanceView({ onBack, onRefresh, busy }: AddInstanceViewProps) {
  const { t } = useTranslation();
  return <div data-add-instance className="mx-auto w-full max-w-2xl pt-6">
    <Button variant="ghost" size="sm" className="mb-5 -ml-3" onClick={onBack}><ArrowLeft className="size-4" />{t('desktopAccount.instancesTitle')}</Button>
    <h1 className="text-2xl font-semibold tracking-tight">{t('desktopAccount.addInstance')}</h1>
    <p className="mt-3 text-sm leading-6 text-muted">{t('desktopAccount.addGuide.body')}</p>
    <div className="mt-6 divide-y divide-border overflow-hidden rounded-xl border border-edge bg-surface">
      <section className="p-5"><h2 className="flex items-center gap-3 text-sm font-semibold"><Laptop className="size-5 text-muted" />{t('desktopAccount.addGuide.computerTitle')}</h2><p className="mt-3 text-sm leading-6 text-muted">{t('desktopAccount.addGuide.computerBody')}</p></section>
      <section className="p-5"><h2 className="flex items-center gap-3 text-sm font-semibold"><Terminal className="size-5 text-muted" />{t('desktopAccount.addGuide.serverTitle')}</h2><p className="mt-3 text-sm leading-6 text-muted">{t('desktopAccount.addGuide.serverBody')}</p><code className="mt-3 block rounded-lg bg-field px-3 py-2 font-mono text-sm select-text">{t('desktopAccount.addGuide.command')}</code><p className="mt-3 text-sm leading-6 text-muted">{t('desktopAccount.addGuide.pairingBody')}</p></section>
    </div>
    <a href="https://docs.avibe.bot/get-started/install" target="_blank" rel="noreferrer" className="mt-5 inline-flex items-center gap-2 text-sm underline underline-offset-4">{t('desktopAccount.addGuide.installGuide')}<ExternalLink className="size-4" /></a>
    <div className="mt-6"><Button variant="brand" disabled={busy} onClick={onRefresh}><RefreshCw className="size-4" />{t('desktopAccount.addGuide.done')}</Button></div>
  </div>;
}
