import type { ReactNode } from 'react';
import { Check, ChevronRight, Download, KeyRound, RefreshCw, SlidersHorizontal } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { BackendIcon } from '../visual';
import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import { Card } from '../ui/card';
import { getBackendUiMeta } from '@/lib/agentBackends';
import type { AssistantId } from './collaborationTimeline';

/** One card's reading of its current Model Hub route. */
export type AssistantRouteView =
  | { kind: 'pending' | 'failed' | 'unavailable' | 'no-agent-model' }
  | { kind: 'route'; model: string | null; backups: number };

export interface AssistantRowProps {
  backend: AssistantId;
  status: 'unknown' | 'ok' | 'missing';
  installing: boolean;
  detecting: boolean;
  error?: { message: string; output?: string | null };
  lifecycle: ReactNode;
  /** The action the lifecycle pill offers — update or upgrade-in-flight — drawn
      opposite the pill on the state row, where the design puts it. */
  upgrade?: ReactNode;
  enabledControl: ReactNode;
  onInstall: () => void;
  onDetect: () => void;
  onConfigure: () => void;
  onAddKey?: () => void;
  onRefreshConnection?: () => void;
  onRetryRoute?: () => void;
  connectionPending?: boolean;
  connectionError?: string;
  routeError?: string;
  configuringDisabled?: boolean;
  /** Presentation only. The connection owner must supply confirmed state. */
  connection?: 'subscription' | 'api_key' | 'hub';
  /** Model Hub is where this setup's models come from, whatever this card's own
      state is — including one whose CLI is not on disk yet. It decides what the
      card offers: an install that also enables, a switch to flip, or its own
      route to review. */
  hubManaged?: boolean;
  /** Whether the assistant is switched on. The pill and the note read from it, so
      the card says the same thing its own switch does. */
  enabled?: boolean;
  /** The route read's evidence; pending and failed reads cannot assert absence. */
  route?: AssistantRouteView;
}

/**
 * One assistant, drawn as the card the welcome already showed it in.
 *
 * The frame, the column it sits in and the identity header at its top are the story
 * card's, from the same classes; only the body under them changes. The reference says
 * it directly — "the assistant logo and name stay at the top in both states; connection
 * state replaces roles with enable switches" — so this card reuses that header and puts
 * its switch in the slot the story fills with a role. Moving from the intro to the
 * connection therefore reads as the cards taking on work rather than as one screen
 * being swapped for another. `geometry.spec.ts` holds both steps to the same boxes,
 * which is what keeps that true as either step changes.
 */
export function AssistantRow({ backend, status, installing, detecting, error, lifecycle, upgrade, enabledControl,
  onInstall, onDetect, onConfigure, configuringDisabled = false, connection, onAddKey, onRefreshConnection, onRetryRoute, connectionPending, connectionError, routeError,
  hubManaged = false, enabled = false, route }: AssistantRowProps) {
  const { t } = useTranslation();
  const label = getBackendUiMeta(backend).label;
  // With Model Hub holding the credentials, a card has three things it can be, and
  // each one has a single next step: install it, switch it on, or look at the route
  // it owns. Anything else the card could say about connections belongs to
  // the other supply modes below.
  const hub = hubManaged || connection === 'hub';
  const native = connection === 'hub' ? undefined : connection;
  const hubState = !hub ? null
    : status === 'ok' ? (enabled ? 'enabled' : 'idle')
      : status === 'missing' ? 'missing'
        : 'checking';
  const routeModel = route?.kind === 'route' ? route.model : null;
  const routeUnknown = route?.kind === 'pending' || route?.kind === 'failed';
  const routeLead = () => (route?.kind === 'route' && route.backups ? t('onboarding.setup.defaultModelWithBackups', { count: route.backups })
      : t('onboarding.setup.defaultModel'));
  // Off, the card still shows the model the assistant would call, so switching it on
  // has no surprise in it. It is a statement then, not a control: no chevron, nothing
  // to press, and the switch above is the only thing that changes it.
  const routeChoice = (live: boolean) => (
    <button type="button" className="onboarding-model-choice" onClick={live ? onConfigure : undefined}
      aria-label={t('onboarding.setup.defaultModelNamed', { name: label })}
      aria-disabled={live ? undefined : true}
      disabled={!live || configuringDisabled || installing || detecting || !!connectionPending}>
      <span>
        <small>{routeUnknown ? t('onboarding.setup.defaultModel') : routeLead()}</small>
        {routeUnknown
          ? <strong className="onboarding-model-choice-pending" aria-hidden="true" />
          : <strong>{routeModel}</strong>}
      </span>
      {live && !routeUnknown && (connectionPending
        ? <RefreshCw size={17} className="motion-safe:animate-spin" />
        : <ChevronRight size={17} />)}
    </button>
  );
  return (
    <Card className="onboarding-assistant" aria-label={label}>
      <div className="onboarding-card-identity">
        <span className="onboarding-card-logo"><BackendIcon backend={backend} size={28} variant="brand" aria-hidden="true" /></span>
        {/* A heading here and a `strong` on the welcome: the setup's three cards are
            a real document structure a person navigates, the welcome's are an
            illustration the card already labels. Same class, so same geometry. */}
        <h3 className="onboarding-card-name">{label}</h3>
        <div className="onboarding-assistant-enable">{enabledControl}</div>
      </div>
      {/* The state row is where the story puts its caption: the lifecycle pill on the
          left and, opposite it, the single action that pill offers — update or
          install. Both stay on this row so the card body below is the note and the
          connection methods, exactly as the design stacks them. */}
      <div className="onboarding-assistant-state">
        {installing || detecting || status === 'unknown' ? (
          <Badge variant="secondary"><RefreshCw size={12} className={installing || detecting ? 'motion-safe:animate-spin' : ''} />
            {t(installing ? 'agentDetection.installing' : detecting ? 'onboarding.welcome.detecting' : 'common.notChecked')}
          </Badge>
        ) : status === 'ok' ? lifecycle : (
          <Badge variant={error ? 'destructive' : 'secondary'}><span className="size-2 rounded-full bg-current" />
            {t(error ? 'onboarding.setup.installFailed' : 'onboarding.setup.notInstalled')}
          </Badge>
        )}
        {upgrade}
        {status === 'missing' && !hub && (
          <Button type="button" variant="secondary" className="onboarding-life-action" onClick={onInstall} disabled={installing || detecting}>
            {installing ? <RefreshCw size={14} className="motion-safe:animate-spin" /> : <Download size={14} />}
            {t(installing ? 'agentDetection.installing' : error ? 'common.retry' : 'onboarding.setup.install')}
          </Button>
        )}
        {status === 'unknown' && !detecting && (
          <Button type="button" variant="secondary" className="onboarding-life-action" onClick={onDetect}>
            <RefreshCw size={14} />{t('common.retry')}
          </Button>
        )}
      </div>
      <div className="onboarding-assistant-body">
        {/* The note is the guidance for the state the card is in, the way the frames
            draw it — what to do next when nothing is installed, how to connect when
            the binary is ready, what a settled connection means, and where a
            hub-owned credential is managed — rather than a static description of the
            assistant. The frames keep it to one line at every tier, which a
            per-state sentence is short enough to honour in both languages. */}
        <p className="onboarding-assistant-note">
          {hubState
            ? t(hubState === 'missing' ? 'onboarding.setup.noteNotInstalled'
              : hubState === 'enabled' || hubState === 'idle'
                ? (route?.kind === 'no-agent-model' ? 'onboarding.setup.noteModelUnset'
                  : route?.kind === 'route' && !routeModel ? 'onboarding.setup.noteNoModels'
                    : hubState === 'enabled' ? 'onboarding.setup.noteEnabled' : 'onboarding.setup.noteNotEnabled')
                : 'onboarding.setup.noteNotEnabled', { name: label })
            : status === 'missing'
              ? t('onboarding.setup.installFirstNamed', { name: label })
              : native
                ? t(`onboarding.setup.${native}ConnectedHint`)
                : t(`onboarding.setup.${backend}Guide`)}
        </p>
        {/* One full-width row per connection method, subscription above API Key, and
            the connected state wearing the same row with a mint check. */}
        <div className="onboarding-assistant-actions">
          {hubState === 'checking' ? null : hubState === 'missing' ? (
            <Button type="button" variant="secondary" className="onboarding-method-row" onClick={onInstall} disabled={installing || detecting}>
              {installing ? <RefreshCw size={16} className="motion-safe:animate-spin" /> : <Download size={16} />}
              {t(installing ? 'agentDetection.installing' : error ? 'common.retry' : 'onboarding.setup.installAndEnable')}
            </Button>
          ) : route?.kind === 'no-agent-model' && (hubState === 'idle' || hubState === 'enabled') ? null : hubState === 'idle' ? (
            /* The model it would call, stated but not offered — or, with no route to
               state, the receipt that the binary is already there. */
            routeModel || routeUnknown ? routeChoice(false) : (
              <span className="onboarding-method-receipt"><Check size={16} />{t('onboarding.setup.installedNotEnabled')}</span>
            )
          ) : hubState === 'enabled' ? (
            /* The card shows the model this assistant will call and opens its route.
               With nothing to show yet the button keeps its box, so the card does not
               grow under the pointer when the read lands. */
            routeModel || routeUnknown ? routeChoice(!routeUnknown) : (
              <button type="button" className="onboarding-method-connected" onClick={onConfigure}
                disabled={configuringDisabled || installing || detecting || !!connectionPending}>
                {connectionPending ? <RefreshCw size={16} className="motion-safe:animate-spin" /> : <SlidersHorizontal size={16} />}
                {t('onboarding.setup.configureRoute')}
              </button>
            )
          ) : native ? (
            /* A button wearing the connected row: it reads as the static receipt the
               design draws, and clicking it still reopens the connection dialog,
               which is the only way back into a settled connection from this card. */
            <button type="button" className="onboarding-method-connected" onClick={onConfigure}
              disabled={configuringDisabled || installing || detecting || !!connectionPending}>
              {connectionPending ? <RefreshCw size={16} className="motion-safe:animate-spin" /> : <Check size={16} />}
              {t(`onboarding.setup.${native}Connected`)}
            </button>
          ) : (
            <Button type="button" variant="secondary" className="onboarding-method-row" onClick={onConfigure} disabled={configuringDisabled || installing || detecting || !!connectionPending}>
              {connectionPending ? <RefreshCw size={16} className="motion-safe:animate-spin" /> : <SlidersHorizontal size={16} />}
              {t('onboarding.connection.addSubscription')}
            </Button>
          )}
          {!hub && !native && onAddKey && (
            <Button type="button" variant="secondary" className="onboarding-method-row" onClick={onAddKey} disabled={configuringDisabled || installing || detecting || !!connectionPending}>
              {connectionPending ? <RefreshCw size={16} className="motion-safe:animate-spin" /> : <KeyRound size={16} />}
              {t('onboarding.connection.addKey')}
            </Button>
          )}
        </div>
        {/* A pending read must not add a row: the cards hold a fixed height, and a
            line that appears while a connection settles and vanishes when it lands
            is a jump under the person's pointer. Pending reads as the action rows
            spinning and disabling instead; a failure is the one thing worth an
            extra line, because it says what to do next. */}
        {connectionError && <div className="onboarding-assistant-error" role="alert">
          {connectionError}
          {onRefreshConnection && <Button variant="link" size="xs" onClick={onRefreshConnection}>{t('common.retry')}</Button>}
        </div>}
        {routeError && <div className="onboarding-assistant-error" role="alert">
          {routeError}
          {onRetryRoute && <Button variant="link" size="xs" onClick={onRetryRoute}>{t('common.retry')}</Button>}
        </div>}
        {error && <div className="onboarding-assistant-error" role="alert">
          <p>{error.message}</p>
          {error.output && <details><summary>{t('onboarding.details')}</summary><pre>{error.output}</pre></details>}
        </div>}
      </div>
    </Card>
  );
}
