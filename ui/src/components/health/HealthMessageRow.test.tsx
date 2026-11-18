import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import type { WorkbenchMessage, WorkbenchSession } from '@/context/ApiContext';
import { ToastProvider } from '@/context/ToastProvider';
import en from '@/i18n/en.json';
import { MessageRow } from '@/components/workbench/ChatPage';

const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const mobile = { id: 'mreq_0123456789abcdef0123456789abcdef', operation: 'health.connect', state: 'queued', requires_user: true, metrics: ['steps', 'sleep'], created_at: '2026-11-18T05:00:00Z', expires_at: '2026-11-19T05:00:00Z', error: null };
const message = { id: 'msg-health', type: 'notify', author: 'harness', source: 'harness', text: 'Health request created', metadata: { mobile_request: mobile }, content: {}, created_at: mobile.created_at } as unknown as WorkbenchMessage;
const session = { id: 'ses-health', metadata: {}, status: 'active' } as WorkbenchSession;
function draw(row: WorkbenchMessage) {
  return renderToStaticMarkup(<I18nextProvider i18n={i18n}><ToastProvider><MemoryRouter><MessageRow message={row} session={session} agentDisplayName="Agent" messageFontSize={14} /></MemoryRouter></ToastProvider></I18nextProvider>);
}
describe('Health metadata in the real transcript row', () => {
  it('renders the S1 notify/harness envelope as the health card with the requested metric names', () => {
    const html = draw(message);
    expect(html).toContain('Connect health data');
    expect(html).toContain('Steps, Sleep');
    expect(html).toContain('Waiting for iPhone');
    expect(html).not.toContain('Authorize on iPhone');
  });
  it('leaves malformed request metadata in the ordinary notification renderer', () => {
    const html = draw({ ...message, metadata: { mobile_request: { ...mobile, id: 'untrusted-path' } } });
    expect(html).not.toContain('Connect health data');
  });
});
