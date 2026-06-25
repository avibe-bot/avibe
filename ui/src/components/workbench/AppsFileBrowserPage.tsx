import { useTranslation } from 'react-i18next';
import { FolderTree } from 'lucide-react';

// Placeholder shell for the File Browser app. The real browser (directory tree +
// FileViewer + CodeMirror editor + favorites) lands once the /api/files backend
// contract is in (Track 4). The route + launcher entry exist now so the Apps
// shell is navigable end to end.
export const AppsFileBrowserPage: React.FC = () => {
  const { t } = useTranslation();
  return (
    <div className="mx-auto flex min-h-[60vh] max-w-3xl flex-col items-center justify-center gap-3 text-center">
      <div className="grid size-14 place-items-center rounded-2xl border border-mint/30 bg-mint/[0.08] text-mint shadow-[0_0_24px_-6px_rgba(91,255,160,0.5)]">
        <FolderTree className="size-7" />
      </div>
      <h1 className="text-[20px] font-semibold text-foreground">{t('apps.fileBrowser.label')}</h1>
      <p className="max-w-md text-[13px] leading-relaxed text-muted">{t('apps.fileBrowser.placeholder')}</p>
    </div>
  );
};
