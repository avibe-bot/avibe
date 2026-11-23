import { ArrowLeft } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';

/** The bundled Desktop document's way back to the Workbench, for frames that draw that document. */
export function DesktopBackHeader() {
  const { t } = useTranslation();
  return <header className="flex shrink-0 items-center px-8 py-3"><Button variant="ghost" size="sm"><ArrowLeft className="size-4" />{t('desktopAccount.backWorkbench')}</Button></header>;
}
