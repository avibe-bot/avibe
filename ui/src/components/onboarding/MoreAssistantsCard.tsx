import { ChevronDown, LayoutGrid } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { getBackendUiMeta } from '@/lib/agentBackends';
import { BackendIcon } from '../visual';
import { Button } from '../ui/button';
import { Card } from '../ui/card';
import type { AssistantId } from './collaborationTimeline';

/**
 * The CLI assistants, folded (ui/design: setup/assistants/default).
 *
 * A new user needs one thing on this screen: an assistant with a model. Vibey is that
 * assistant, so its card stays as it is and the others wait behind this one. Folded is
 * not hidden: each assistant is named here with whether it is switched on, or that its
 * card has an error to read, so an assistant that is already enabled — and can already
 * open the workspace — or one that failed is never a state the person cannot see.
 */
/** What a folded assistant's line says: on, off, or that its card has something to fix. */
export type FoldedAssistantState = 'enabled' | 'notEnabled' | 'attention';

const STATE_LABEL = {
  enabled: 'onboarding.setup.enabled',
  notEnabled: 'onboarding.setup.notEnabled',
  attention: 'onboarding.setup.moreAssistantsAttention',
} as const;

export function MoreAssistantsCard({ backends, state, onShow }: {
  backends: readonly AssistantId[];
  state: (backend: AssistantId) => FoldedAssistantState;
  onShow: () => void;
}) {
  const { t } = useTranslation();
  const title = t('onboarding.setup.moreAssistants');
  return (
    <Card className="onboarding-assistant onboarding-more-assistants" aria-label={title}>
      <div className="onboarding-card-identity">
        <span className="onboarding-card-logo onboarding-more-assistants-logo"><LayoutGrid aria-hidden="true" /></span>
        <h3 className="onboarding-card-name">{title}</h3>
      </div>
      <div className="onboarding-assistant-body">
        <p className="onboarding-assistant-note">{t('onboarding.setup.moreAssistantsNote')}</p>
        <ul className="onboarding-more-assistants-list">
          {backends.map((backend) => (
            <li key={backend} data-backend={backend}>
              <BackendIcon backend={backend} size={18} variant="brand" aria-hidden="true" />
              <span className="onboarding-more-assistants-name">{getBackendUiMeta(backend).label}</span>
              <span className="onboarding-more-assistants-state" data-state={state(backend)}>
                {t(STATE_LABEL[state(backend)])}
              </span>
            </li>
          ))}
        </ul>
        <div className="onboarding-assistant-actions">
          <Button type="button" variant="secondary" className="onboarding-method-row" aria-expanded={false} onClick={onShow}>
            <ChevronDown size={16} />{t('onboarding.setup.moreAssistantsShow')}
          </Button>
        </div>
      </div>
    </Card>
  );
}
