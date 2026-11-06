// The browser suites' Playwright base: `test` and `expect` as @playwright/test
// has them, with one difference every suite that can show the chat composer
// needs.
//
// The composer prepares its voice capture as it mounts: prewarmVoiceCapture
// loads an AudioWorklet module from a blob: URL. On Linux WebKit, CI's stand-in
// for Safari's engine, that load can stop the page for good. Nothing renders
// or answers again, not even Playwright's own locators, so a case times out
// waiting for an element that is already in the DOM (#3224, #3293; the
// composer cases of run 42210471130). No case here measures audio, so the
// module load resolves at once and loads nothing.
//
// The stub goes on the browser context before any page script runs. It holds
// for fixture pages and app routes alike, and for every page that context
// opens. A context a spec makes itself (`browser.newContext()`) takes it
// through `stubVoiceWorklet`. A project that does capture audio, like
// voice-shortcut's microphone projects, sets `voiceWorklet: 'real'`.
//
// Every suite whose pages can show the composer runs on this base. A new
// suite that can show it imports its `test` from here as well.
// e2e/chat-interactions/browserTest.spec.ts checks the base in the browsers.
import { type BrowserContext, test as base } from '@playwright/test';

export * from '@playwright/test';

export type VoiceWorkletOptions = {
  /** `stub` (the default): the composer's voice worklet loads nothing. */
  voiceWorklet: 'stub' | 'real';
};

/** Make every page of `context` load the voice worklet as a no-op. */
export async function stubVoiceWorklet(context: BrowserContext): Promise<void> {
  await context.addInitScript(() => {
    if (typeof AudioWorklet !== 'undefined') AudioWorklet.prototype.addModule = async () => undefined;
  });
}

// The fixture callback is `provide`, not `use`, for the reason support/fixtures.ts gives.
export const test = base.extend<VoiceWorkletOptions>({
  voiceWorklet: ['stub', { option: true }],
  context: async ({ context, voiceWorklet }, provide) => {
    if (voiceWorklet === 'stub') await stubVoiceWorklet(context);
    await provide(context);
  },
});
