// Asking before voice input sends speech to a third-party AI service (ui/design:
// composer/voice-consent/*). App Store guideline 5.1.2(i): "clearly disclose where personal data
// will be shared with third parties, including with third-party AI, and obtain explicit
// permission before doing so." One question per account on this Avibe, asked on the first press
// of any voice control, before the microphone opens; the answer can be changed in
// Settings › General. Owner direction (2026-11-16): a title, one or two ordinary sentences,
// Allow / Don't allow, and a small way to the details; no provider names or risk lists in the
// body. Two variants for the owner to pick: `link` sends "Learn more" to the privacy policy;
// `details` opens small print in the dialog. Product tokens and src/components/ui primitives
// only; copy is draft copy (kit/draft.ts) and moves to src/i18n when this ships.
import { ChevronDown, MicOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { SettingsPageShell } from '@/components/settings/SettingsPageShell';
import { SettingsPanel, ToggleSwitch } from '@/components/settings/SettingsPrimitives';

import { useDraft } from './draft';

const PRIVACY_VOICE = 'https://avibe.bot/privacy#voice';

export type VoiceConsentVariant = 'link' | 'details';

/** The question. A dialog on a desktop, a sheet on a phone (the shipped Dialog does both).
 *  Nothing is recorded until Allow; Don't allow records the no; the × or Esc only closes. */
export function VoiceConsentDialog({ variant, expanded = false }: { variant: VoiceConsentVariant; expanded?: boolean }) {
  const d = useDraft();
  const link = 'text-xs font-medium text-accent-ink underline-offset-2 hover:underline';
  return (
    <Dialog open>
      <DialogContent className="max-w-sm" closeLabel={d('关闭', 'Close')}>
        <DialogHeader>
          <DialogTitle>{d('开启语音输入？', 'Turn on voice input?')}</DialogTitle>
          <DialogDescription className="text-sm leading-relaxed">
            {variant === 'link'
              ? d(
                '你说的话会交给第三方 AI 语音服务转成文字，avibe.bot 不保存录音。',
                "What you say goes to a third-party AI speech service to be turned into text. avibe.bot doesn't keep your recordings.",
              )
              : d(
                '你说的话会交给第三方 AI 服务转成文字，为了更准确，也会带上当前对话里的少量文字。',
                'What you say goes to a third-party AI service to be turned into text, with a little of the current conversation for accuracy.',
              )}
          </DialogDescription>
        </DialogHeader>
        {variant === 'link' ? (
          <a href={PRIVACY_VOICE} target="_blank" rel="noreferrer" className={clsx(link, 'justify-self-start')}>
            {d('了解详情', 'Learn more')}
          </a>
        ) : (
          <div className="flex flex-col gap-2">
            <button type="button" className={clsx(link, 'flex items-center gap-1 self-start')} aria-expanded={expanded}>
              {d('了解详情', 'Learn more')}
              <ChevronDown className={clsx('size-3.5 transition-transform', expanded && 'rotate-180')} />
            </button>
            {expanded && (
              <p className="text-xs leading-relaxed text-muted">
                {d(
                  '录音和这些文字经 avibe.bot 转给阿里云百炼（新加坡）识别，再由同一平台上的模型整理。avibe.bot 不保存录音和文字。随时可以在设置 › 常规里更改。',
                  'They pass through avibe.bot to Alibaba Cloud Model Studio (Singapore), which transcribes the recording, and a model on the same platform tidies the text. avibe.bot keeps neither. You can change this anytime in Settings › General.',
                )}{' '}
                <a href={PRIVACY_VOICE} target="_blank" rel="noreferrer" className="text-accent-ink underline-offset-2 hover:underline">
                  {d('隐私政策', 'Privacy policy')}
                </a>
              </p>
            )}
          </div>
        )}
        <DialogFooter>
          <Button variant="ghost">{d('不允许', "Don't allow")}</Button>
          <Button variant="brand">{d('允许', 'Allow')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Where a press's note goes (as ComposerHold's HoldNote draws one), after "Don't allow": voice
 *  input stays off, typing works as before, and the note says how to turn it on. "Turn on" asks
 *  the same question again; it never turns voice on by itself. */
export function VoiceOffNote() {
  const d = useDraft();
  return (
    <div
      role="status"
      className="flex w-full max-w-sm items-start gap-3 rounded-2xl border border-edge bg-surface-2 px-3.5 py-3 shadow-elevation-2"
    >
      <MicOff className="mt-0.5 size-4 shrink-0 text-muted" />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5 text-xs leading-relaxed">
        <span className="font-semibold text-foreground">{d('语音输入已关闭', 'Voice input is off')}</span>
        <span className="text-muted">
          {d('可以直接打字，或在设置 › 常规里开启。', 'Type instead, or turn it on in Settings › General.')}
        </span>
      </div>
      <Button type="button" variant="outline" size="sm" className="pointer-events-auto shrink-0">
        {d('开启', 'Turn on')}
      </Button>
    </div>
  );
}

/** Settings › General: the account's answer, on this Avibe. Turning it on asks the question again
 *  (the dialog above); turning it off takes effect at once. */
export function VoiceInputSetting({ enabled }: { enabled: boolean }) {
  const d = useDraft();
  return (
    <SettingsPanel
      variant="preference"
      title={d('语音输入', 'Voice input')}
      description={d(
        '用第三方 AI 语音服务把你说的话转成文字，avibe.bot 不保存录音。只对你的账号在这个 Avibe 上生效。',
        "A third-party AI speech service turns what you say into text; avibe.bot doesn't keep your recordings. Applies to your account on this Avibe.",
      )}
      actions={<ToggleSwitch enabled={enabled} label={d('语音输入', 'Voice input')} onClick={() => {}} />}
    />
  );
}

/** Settings › General with the row added below what it shows today (language, appearance, menu
 *  placement, the desktop mini window and home tips are unchanged and left out here). */
export function GeneralWithVoiceInput({ enabled }: { enabled: boolean }) {
  const { t } = useTranslation();
  return (
    <SettingsPageShell title={t('settings.general.title')} subtitle={t('settings.general.subtitle')} activeTab="general" titleScale="landing">
      <div className="flex flex-col gap-6">
        <VoiceInputSetting enabled={enabled} />
      </div>
    </SettingsPageShell>
  );
}
