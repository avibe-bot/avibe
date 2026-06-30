import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { Markdown } from '../ui/markdown';

// Reusable RENDERED (non-editor) file preview for the windowed apps. Pure/presentational: the
// caller supplies the image `src` or the markdown `content`, so the same component serves an
// on-disk image (File Browser overlay, editor image tab — src = contentUrl(path)) and a LIVE buffer
// the editor is editing (SVG preview via a data: URL, Markdown via the buffer text). Code/JSON/CSV
// stay in Monaco / the chat FileViewer; this only covers what the editor can't show as text.
type FilePreviewProps =
  | { kind: 'image'; src: string; name?: string; className?: string }
  | { kind: 'markdown'; content: string; className?: string };

export const FilePreview: React.FC<FilePreviewProps> = (props) => {
  if (props.kind === 'markdown') {
    return (
      <div className={clsx('h-full min-h-0 overflow-auto bg-surface', props.className)}>
        <Markdown content={props.content} interactive={false} className="vr-fileview-md mx-auto max-w-3xl px-6 py-5" />
      </div>
    );
  }
  return <ImagePreview src={props.src} name={props.name} className={props.className} />;
};

// Fit-to-container by default; click toggles 1:1 actual size (zoom-in / zoom-out cursor), with the
// container scrolling when the image overflows. The dark checker-free backdrop keeps transparent
// PNGs/SVGs legible inside the dark-locked windows.
const ImagePreview: React.FC<{ src: string; name?: string; className?: string }> = ({ src, name, className }) => {
  const { t } = useTranslation();
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [actual, setActual] = useState(false);
  // Reset when the source changes — the live SVG preview re-points `src` on every keystroke, so a
  // transient error (a momentarily-invalid buffer) must clear once the buffer becomes valid again.
  useEffect(() => {
    setStatus('loading');
    setActual(false);
  }, [src]);

  if (status === 'error') {
    return (
      <div className={clsx('grid h-full min-h-0 place-items-center bg-[#0c0c0f] p-4 text-[12.5px] text-muted', className)}>
        {t('apps.fileBrowser.previewFailed')}
      </div>
    );
  }
  return (
    <div className={clsx('grid h-full min-h-0 place-items-center overflow-auto bg-[#0c0c0f] p-4', className)}>
      {status === 'loading' && <div className="col-start-1 row-start-1 text-[12px] text-muted">{t('common.loading')}</div>}
      <img
        src={src}
        alt={name || ''}
        onLoad={() => setStatus('ready')}
        onError={() => setStatus('error')}
        onClick={() => setActual((a) => !a)}
        draggable={false}
        className={clsx(
          'col-start-1 row-start-1 select-none',
          actual ? 'max-w-none cursor-zoom-out' : 'max-h-full max-w-full cursor-zoom-in object-contain',
          status !== 'ready' && 'opacity-0',
        )}
      />
    </div>
  );
};
