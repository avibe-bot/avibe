// Two whole pages of the Devices work that the instance itself answers with (plain HTML from the
// Runtime, not this bundle), kept here as their design record: what a browser sees at an address
// this Avibe gave up, and what a signed-out device sees when it comes back too early. Copy as
// decided on 2026-11-21, inline in both languages.
import { ExternalLink, Globe, TriangleAlert } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { KeptTail } from '@/components/ui/kept-tail';
import { useDraft } from './draft';

/** What any browser sees when it opens the old address during the waiting period, after it cleared
 *  what it kept for it: one sentence and a link to avibe.bot. */
export function MovedPage() {
  const d = useDraft();
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-md text-center">
        <div className="mx-auto mb-6 grid size-12 place-items-center rounded-2xl border border-edge bg-field"><Globe className="size-5 text-muted" /></div>
        <p className="text-balance text-[15px] leading-relaxed text-foreground">
          <KeptTail text={d('这个 Avibe 换了新地址。能访问它的人，在 avibe.bot 登录后可以打开。', 'This Avibe has a new address. People who can access it can open it after signing in at avibe.bot.')} />
        </p>
        <a href="https://avibe.bot" className="mt-5 inline-flex items-center gap-1 text-[14px] font-medium text-cyan-ink hover:underline">{d('avibe.bot', 'avibe.bot')}</a>
      </div>
    </main>
  );
}

/** A device that was signed out comes back while avibe.bot is still ending its sign-in. */
export function SignInNotFinishedPage() {
  const d = useDraft();
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-md">
        <div className="mb-6 grid size-12 place-items-center rounded-2xl border border-edge bg-field"><TriangleAlert className="size-5 text-gold-ink" /></div>
        <h1 className="text-xl font-semibold text-foreground">{d('这台设备已被退出', 'This device was signed out')}</h1>
        <p className="mt-3 text-[14px] leading-relaxed text-muted">
          <KeptTail text={d(
            '请过几分钟再登录。一直不行的话，先在 avibe.bot 退出登录，再回来登录。',
            'Sign in again in a few minutes. If it keeps failing, sign out at avibe.bot first, then come back and sign in.',
          )} />
        </p>
        <div className="mt-7 flex flex-wrap gap-3">
          <Button variant="brand">{d('重新登录', 'Sign in again')}</Button>
          <Button variant="ghost"><ExternalLink />{d('打开 avibe.bot', 'Open avibe.bot')}</Button>
        </div>
      </div>
    </main>
  );
}
