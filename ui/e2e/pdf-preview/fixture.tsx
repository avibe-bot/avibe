import { createRoot } from 'react-dom/client';

import { FileCard } from '../../src/components/ui/file-card';
import { FileViewerProvider } from '../../src/components/ui/file-viewer';
import { ThemeProvider } from '../../src/context/ThemeProvider';
import '../../src/i18n';
import '../../src/index.css';

// A chat attachment card for the PDF named by ``?file=`` (a media-proxy id), inside the same
// providers ChatPage mounts. Its preview eye opens the real chat file viewer; every
// `/api/media/...` request is answered by the spec, so nothing reaches an Avibe service.

const file = new URLSearchParams(location.search).get('file') ?? 'doc_five';

createRoot(document.getElementById('root')!).render(
  <ThemeProvider>
    <FileViewerProvider>
      <div className="flex h-dvh flex-col bg-background p-4 text-foreground" data-testid="pdf-fixture">
        <FileCard href={`/api/media/${file}`}>{`${file}.pdf`}</FileCard>
      </div>
    </FileViewerProvider>
  </ThemeProvider>,
);
