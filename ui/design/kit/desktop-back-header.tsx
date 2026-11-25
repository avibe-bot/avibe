import { ArrowLeft } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { DesktopSignIn } from '@/desktop/DesktopSignIn';
import type { DesktopClientState } from '@/desktop/bridge';
import { SIGN_IN_PROVIDERS } from '@/sign-in/types';

/** The bundled Desktop document's way back to the Workbench, for frames that draw that document. */
export function DesktopBackHeader() {
  const { t } = useTranslation();
  return <header className="flex shrink-0 items-center px-8 py-3"><Button variant="ghost" size="sm"><ArrowLeft className="size-4" />{t('desktopAccount.backWorkbench')}</Button></header>;
}

/** The claim dialog's sign-in face: the App's own sign-in under the dialog's heading, with Cancel. */
export function ClaimDialogSignIn({ state }: { state: DesktopClientState }) {
  const { t } = useTranslation();
  return <DesktopSignIn state={state} send={async () => null} methods={{ providers: SIGN_IN_PROVIDERS, passkey: true }}
    onLocal={() => undefined} exitLabel={t('common.cancel')}
    heading={<h2 className="mb-2 text-xl font-semibold tracking-tight">{t('desktopAccount.bind.signInTitle')}</h2>}
    onDifferentAccount={() => undefined} onOpenSecurity={() => undefined} renderEnroll={() => null} />;
}
