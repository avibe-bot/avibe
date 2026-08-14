import { useState } from 'react';
import { ChevronDown, SplitSquareVertical } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { CompactField } from '../settings/SettingsPrimitives';

interface ProxyUrlFieldProps {
  value: string;
  onChange: (next: string) => void;
  /**
   * i18n key for the label. Defaults to `common.proxyUrl`. Telegram passes
   * `telegramConfig.proxyUrl` so its label can stay platform-flavored.
   */
  labelKey?: string;
  /**
   * i18n key for the long hint shown below the input when expanded. Defaults
   * to `common.proxyUrlHint`. Telegram passes `telegramConfig.proxyUrlHint`
   * and Lark passes `larkConfig.proxyUrlLarkLimitation` so the SDK warning
   * stays where users see it.
   */
  hintKey?: string;
}

export function ProxyUrlField({
  value,
  onChange,
  labelKey = 'common.proxyUrl',
  hintKey = 'common.proxyUrlHint',
}: ProxyUrlFieldProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(!!value);

  return (
    <div className="space-y-2">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center justify-between gap-2 text-left text-[12px] font-medium text-foreground transition hover:text-cyan-ink"
      >
        <span className="flex items-center gap-2">
          <SplitSquareVertical size={14} className="text-cyan-ink" />
          {t(labelKey)}
          <span className="text-[11px] font-normal text-muted">
            {t('common.proxyUrlCollapsedHint')}
          </span>
        </span>
        <ChevronDown
          size={14}
          className={clsx('text-muted transition-transform', expanded && 'rotate-180')}
        />
      </button>
      {expanded && (
        <div className="space-y-2">
          <CompactField
            type="text"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder="socks5://user:pass@host:port (optional)"
            className="w-full font-mono placeholder:text-muted/55"
          />
          <p className="text-[11px] text-muted">{t(hintKey)}</p>
        </div>
      )}
    </div>
  );
}
