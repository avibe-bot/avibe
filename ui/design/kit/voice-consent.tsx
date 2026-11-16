// Asking before voice input sends a recording to third-party AI services (ui/design:
// composer/voice-consent/*). App Store guideline 5.1.2(i): say where the data goes and get an
// explicit yes before it goes. One question per account on this Avibe, asked on the first press
// of any voice control, before the microphone opens; the answer can be changed in
// Settings › General. Product tokens and src/components/ui primitives only; copy is draft copy
// (kit/draft.ts) and moves to src/i18n when this ships.
import { AudioLines, CloudUpload, FileText, MicOff, ShieldCheck } from 'lucide-react';
import type { ComponentType, ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { SettingsPageShell } from '@/components/settings/SettingsPageShell';
import { SettingsPanel, ToggleSwitch } from '@/components/settings/SettingsPrimitives';

import { useDraft } from './draft';

/** Who supplies the speech and language models: avibe.bot's own (a personal Avibe), or the ones an
 *  organization's admins configured (an Avibe paired to an organization). */
export type VoiceSupplier = 'personal' | 'organization';

function Point({ icon: Icon, children }: { icon: ComponentType<{ className?: string }>; children: ReactNode }) {
  return (
    <li className="flex items-start gap-3">
      <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-muted">
        <Icon className="size-4" />
      </span>
      <span className="min-w-0 flex-1 text-[13px] leading-relaxed text-foreground">{children}</span>
    </li>
  );
}

/** The question itself. A dialog on a desktop, a sheet on a phone (the shipped Dialog does both).
 *  Nothing is recorded until "Allow"; "Don't allow", the ×, Esc or a drag down all mean no. */
export function VoiceConsentDialog({ supplier }: { supplier: VoiceSupplier }) {
  const d = useDraft();
  return (
    <Dialog open>
      <DialogContent className="max-w-md" closeLabel={d('关闭', 'Close')}>
        <DialogHeader>
          <DialogTitle>{d('开启语音输入？', 'Turn on voice input?')}</DialogTitle>
          <DialogDescription>
            {d(
              '语音输入要把你说的话交给第三方 AI 服务转成文字。开启前请看一下会发送什么、发给谁。',
              'Voice input uses third-party AI services to turn your speech into text. Here is what is sent and where.',
            )}
          </DialogDescription>
        </DialogHeader>
        <ul className="flex flex-col gap-3">
          <Point icon={AudioLines}>
            {d('你的录音，经 avibe.bot 转发。', 'Your recording, passed on by avibe.bot.')}
          </Point>
          <Point icon={FileText}>
            {d(
              '为了识别更准，还会附上：光标前后的文字、Agent 的上一条回复、你的上一条消息、你的伙伴名字。',
              "To make it more accurate, also: the text around your cursor, the Agent's last reply, your previous message, and your Bots' names.",
            )}
          </Point>
          <Point icon={CloudUpload}>
            {supplier === 'personal'
              ? d(
                '发给新加坡的阿里云百炼：它识别语音，再由同一平台上的 DeepSeek 模型整理文字。',
                'Sent to Alibaba Cloud Model Studio in Singapore, which transcribes it; a DeepSeek model on the same platform tidies the text.',
              )
              : d(
                '发给你所在组织的管理员选定的语音识别和语言模型服务。',
                "Sent to the speech recognition and language model services your organization's admins chose.",
              )}
          </Point>
          <Point icon={ShieldCheck}>
            {d(
              'avibe.bot 不保存录音，也不保存文字。AI 服务按各自的隐私政策处理。',
              'avibe.bot keeps neither the audio nor the text. The AI services handle it under their own privacy policies.',
            )}
          </Point>
        </ul>
        <p className="text-xs leading-relaxed text-muted">
          {d('之后可以在 设置 › 常规 里随时更改。', 'You can change this anytime in Settings › General.')}{' '}
          <a href="https://avibe.bot/privacy#voice" className="text-accent-ink underline-offset-2 hover:underline">
            {d('隐私政策', 'Privacy policy')}
          </a>
        </p>
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
          {d('可以直接打字。要用语音，在 设置 › 常规 里开启。', 'Type instead, or turn it on in Settings › General.')}
        </span>
      </div>
      <Button type="button" variant="outline" size="sm" className="pointer-events-auto shrink-0">
        {d('开启…', 'Turn on…')}
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
        '把录音经 avibe.bot 交给第三方 AI 服务转成文字，avibe.bot 不保存录音和文字。只对你的账号在这个 Avibe 上生效。',
        'Sends your recordings through avibe.bot to third-party AI services that turn them into text. avibe.bot keeps neither the audio nor the text. Applies to your account on this Avibe.',
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
