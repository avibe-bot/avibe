import { useEffect, useId, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog';
import { SettingsPanel, ToggleSwitch } from '../settings/SettingsPrimitives';
import { useToast } from '../../context/ToastContext';
import { apiFetch } from '../../lib/apiFetch';
import {
  answerVoiceConsent,
  askVoiceConsent,
  closeVoiceConsent,
  knownVoiceConsent,
  noteVoiceStatus,
  refreshVoiceConsent,
  useVoiceConsent,
  useVoiceConsentAsking,
} from '../../lib/voiceConsent';

// Asking before voice goes to a third-party AI service (App Store guideline 5.1.2(i);
// ui/design: composer/voice-consent/*): one question per account on this Avibe, before the
// microphone first opens. The body says where speech goes; "Learn more" opens the small print
// (where it goes by name, retention, where to change it). Only Allow records a yes.

const VOICE_PRIVACY_URL = 'https://avibe.bot/privacy#voice';

/** "Learn more" and the small print it opens: where speech goes by name, retention, where to
 *  change it, and the privacy policy. */
function VoiceConsentDetails({ defaultExpanded }: { defaultExpanded: boolean }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(defaultExpanded);
  const detailsId = useId();
  const link = 'text-xs font-medium text-accent-ink underline-offset-2 hover:underline';
  return (
    <div className="flex flex-col gap-2">
      <button
        type="button"
        className={clsx(link, 'flex items-center gap-1 self-start')}
        aria-expanded={expanded}
        aria-controls={detailsId}
        onClick={() => setExpanded((value) => !value)}
      >
        {t('voice.consent.learnMore')}
        <ChevronDown className={clsx('size-3.5 transition-transform', expanded && 'rotate-180')} />
      </button>
      {expanded && (
        <p id={detailsId} className="text-xs leading-relaxed text-muted">
          {t('voice.consent.details')}
          <a href={VOICE_PRIVACY_URL} target="_blank" rel="noopener noreferrer" className="ml-1 text-accent-ink underline-offset-2 hover:underline">
            {t('voice.consent.privacyPolicy')}
          </a>
        </p>
      )}
    </div>
  );
}

/** The question, drawn as given. A dialog on a desktop, a sheet on a phone. */
export function VoiceConsentDialog({
  open,
  busy = false,
  defaultExpanded = false,
  onAllow,
  onDeny,
  onClose,
}: {
  open: boolean;
  busy?: boolean;
  /** Show the small print from the start (a design frame of it opened). */
  defaultExpanded?: boolean;
  onAllow: () => void;
  onDeny: () => void;
  /** The × or Esc: closes without an answer. */
  onClose: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !busy) onClose(); }}>
      <DialogContent className="max-w-sm" closeLabel={t('voice.dismiss')}>
        <DialogHeader>
          <DialogTitle>{t('voice.consent.title')}</DialogTitle>
          <DialogDescription className="text-sm leading-relaxed">{t('voice.consent.body')}</DialogDescription>
        </DialogHeader>
        {/* Mounted with the dialog, so each opening starts with the small print closed. */}
        <VoiceConsentDetails defaultExpanded={defaultExpanded} />
        <DialogFooter>
          <Button variant="ghost" disabled={busy} onClick={onDeny}>{t('voice.consent.deny')}</Button>
          <Button variant="brand" disabled={busy} onClick={onAllow}>{t('voice.consent.allow')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The one place the question is drawn, mounted once by the shell; any voice control puts it up
 *  (`gateVoicePress`, `askVoiceConsent`). */
export function VoiceConsentHost() {
  const asking = useVoiceConsentAsking();
  const { showToast } = useToast();
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  // Put up before the answer was read: read it, and say nothing more to a reader who said yes.
  useEffect(() => {
    if (!asking || knownVoiceConsent() !== undefined) return;
    let alive = true;
    void refreshVoiceConsent().then((consent) => {
      if (alive && consent === 'granted') closeVoiceConsent();
    });
    return () => {
      alive = false;
    };
  }, [asking]);
  const answer = (choice: 'granted' | 'declined') => {
    setBusy(true);
    void answerVoiceConsent(choice).then((saved) => {
      setBusy(false);
      if (saved) closeVoiceConsent();
      else showToast(t('voice.consent.saveFailed'), 'error');
    });
  };
  return (
    <VoiceConsentDialog
      open={asking}
      busy={busy}
      onAllow={() => answer('granted')}
      onDeny={() => answer('declined')}
      onClose={closeVoiceConsent}
    />
  );
}

/** Settings › General: the account's answer on this Avibe. On asks the question; off is at once. */
export function VoiceInputSetting() {
  const { t } = useTranslation();
  const consent = useVoiceConsent();
  const { showToast } = useToast();
  const [available, setAvailable] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    apiFetch('/api/asr/status')
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (!alive || !data) return;
        noteVoiceStatus(data);
        // Where voice works here, or an answer was given that can still be taken back.
        setAvailable(data.available === true || data.consent === 'granted' || data.consent === 'declined');
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  // Shown where the reader can use voice here (the status read answers them).
  if (!available) return null;
  const enabled = consent === 'granted';
  return (
    <SettingsPanel
      variant="preference"
      title={t('voice.consent.settingTitle')}
      description={t('voice.consent.settingDescription')}
      actions={(
        <ToggleSwitch
          enabled={enabled}
          disabled={busy || consent === undefined}
          label={t('voice.consent.settingTitle')}
          onClick={() => {
            if (!enabled) {
              askVoiceConsent();
              return;
            }
            setBusy(true);
            void answerVoiceConsent('declined').then((saved) => {
              setBusy(false);
              if (!saved) showToast(t('voice.consent.saveFailed'), 'error');
            });
          }}
        />
      )}
    />
  );
}
