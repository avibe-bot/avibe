import { useRef, useState } from 'react';
import { FileArchive, RefreshCw, UploadCloud } from 'lucide-react';
import clsx from 'clsx';

export interface FileDropzoneProps {
  file: File | null;
  onFile: (file: File | null) => void;
  accept?: string;
  hint?: string;
  replaceLabel?: string;
  /** Status line under the file name, e.g. "240 KB · 3 skills found". */
  meta?: string;
}

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** Dashed drop area for a single file (the .zip skill package). */
export function FileDropzone({ file, onFile, accept = '.zip', hint, replaceLabel = 'Replace', meta }: FileDropzoneProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const pick = () => inputRef.current?.click();
  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        const dropped = e.dataTransfer.files?.[0];
        if (dropped) onFile(dropped);
      }}
      className={clsx(
        'flex flex-col gap-2 rounded-[10px] border border-dashed p-3.5 transition',
        file
          ? 'border-mint/40 bg-mint/[0.04]'
          : dragging
            ? 'border-mint/60 bg-mint/[0.06]'
            : 'border-border-strong bg-surface',
      )}
    >
      <input ref={inputRef} type="file" accept={accept} className="hidden" onChange={(e) => onFile(e.target.files?.[0] ?? null)} />
      {file ? (
        <div className="flex items-center gap-2.5 rounded-lg border border-border-strong bg-surface px-3 py-2">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-mint/40 bg-mint-soft text-mint-ink">
            <FileArchive className="size-4" />
          </span>
          <div className="flex min-w-0 flex-1 flex-col">
            <span className="truncate font-mono text-[12px] text-foreground">{file.name}</span>
            <span className="text-[10.5px] text-muted">{meta ?? humanSize(file.size)}</span>
          </div>
          <button
            type="button"
            onClick={pick}
            className="flex shrink-0 items-center gap-1.5 rounded-md border border-border-strong px-2.5 py-1.5 text-[11px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
          >
            <RefreshCw className="size-3 text-muted" />
            {replaceLabel}
          </button>
        </div>
      ) : (
        <button type="button" onClick={pick} className="flex flex-col items-center gap-1.5 py-4 text-center">
          <UploadCloud className="size-6 text-muted" />
          <span className="text-[12px] text-muted">{hint}</span>
        </button>
      )}
    </div>
  );
}
