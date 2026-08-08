// The 「谁会没有来源」 list (api.md → `would_interrupt` / `interrupted_pairs`).
//
// The supply guard refuses a write by NAMING what it would strand, and the spec
// says why that naming matters: 「删除后 pm 将没有可用来源」 is actionable where a
// bare (backend, model) pair is not. So this renders the Agents first and the
// model second, and falls back to the backend only when no named Agent runs the
// pair — which is a real case (a mapping or Agent selection supplies it) and not a reason
// to drop the entry.
//
// One component for all three sites — the forced-delete confirm, the forced
// key-replacement confirm, and the post-repair report — because the list is the
// same list, and the previous per-site prose is exactly where two surfaces would
// start describing the same refusal differently.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import type { SupplyGap } from './types';

export const SupplyGapNote: React.FC<{
  gaps: SupplyGap[];
  /** Rendered above the list. Omitted where the dialog's own description already
   *  states the consequence — a second sentence saying it again is the kind of
   *  line that is better deleted. */
  title?: string;
}> = ({ gaps, title }) => {
  const { t } = useTranslation();
  if (gaps.length === 0) return null;
  const backendName = (backend: string) =>
    t(`settings.models.backends.${backend}`, { defaultValue: backend }) as string;

  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-surface-2/60 px-3 py-2.5">
      {title ? <p className="text-[12px] font-semibold text-foreground">{title}</p> : null}
      <ul className="flex flex-col gap-1">
        {gaps.map((gap) => (
          <li key={`${gap.backend}:${gap.model_id}`} className="text-[12px] leading-relaxed text-muted">
            {/* ' · ' is this page's separator everywhere else (source sub-lines,
                the adoption note, the agent card's amber line) — locale-neutral
                on purpose. Agent names are user-chosen identifiers, so they join
                with a plain comma in both locales rather than through copy. */}
            {`${gap.agents.length > 0 ? gap.agents.join(', ') : backendName(gap.backend)} · ${gap.model_id}`}
          </li>
        ))}
      </ul>
    </div>
  );
};

export default SupplyGapNote;
