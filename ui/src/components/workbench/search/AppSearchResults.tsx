import { useTranslation } from 'react-i18next';
import { LayoutGrid } from 'lucide-react';
import clsx from 'clsx';

import { APP_REGISTRY } from '../../../apps/registry';
import { ShowPageAvatarTile } from '../../../apps/showPageAvatarTile';
import { Button } from '../../ui/button';
import type { AppSearchResult } from './appSearch';

type AppSearchResultSectionProps = {
  results: AppSearchResult[];
  selectedKey?: string;
  onSelect: (result: AppSearchResult) => void;
};

export const AppSearchResultSection: React.FC<AppSearchResultSectionProps> = ({
  results,
  selectedKey,
  onSelect,
}) => {
  const { t } = useTranslation();
  if (results.length === 0) return null;

  return (
    <section className="flex flex-col" aria-label={t('workbench.search.appsSection')}>
      <div className="flex items-center gap-1.5 px-2.5 py-1.5">
        <LayoutGrid className="size-[13px] shrink-0 text-muted" />
        <span className="min-w-0 flex-1 font-mono text-[11px] font-bold text-muted">
          {t('workbench.search.appsSection')}
        </span>
        <span className="shrink-0 font-mono text-[10px] text-muted">{results.length}</span>
      </div>
      <div className="flex flex-col">
        {results.map((result) => {
          const selected = selectedKey === result.key;
          const def = APP_REGISTRY[result.appId];
          const Icon = def.icon;
          return (
            <Button
              key={result.key}
              type="button"
              variant="ghost"
              onClick={() => onSelect(result)}
              aria-current={selected ? 'true' : undefined}
              className={clsx(
                'h-auto w-full justify-start gap-2.5 px-2.5 py-2 text-left font-normal',
                selected
                  ? 'bg-mint-soft ring-1 ring-inset ring-mint/40 hover:bg-mint-soft'
                  : 'hover:bg-foreground/[0.04]',
              )}
            >
              {result.kind === 'showpage' && result.sessionId ? (
                <ShowPageAvatarTile
                  sessionId={result.sessionId}
                  title={result.title}
                  iconVersion={result.iconVersion}
                  className="size-7 rounded-md"
                />
              ) : (
                <span
                  className="grid size-7 shrink-0 place-items-center rounded-md border border-border bg-foreground/[0.03]"
                  style={{ color: `var(${def.accent})` }}
                >
                  <Icon className="size-3.5" />
                </span>
              )}
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-foreground">
                {result.title}
              </span>
              <span className="shrink-0 font-mono text-[10px] text-muted">
                {result.kind === 'showpage' ? t('library.kind.showPage') : t('library.kind.builtin')}
              </span>
            </Button>
          );
        })}
      </div>
    </section>
  );
};
