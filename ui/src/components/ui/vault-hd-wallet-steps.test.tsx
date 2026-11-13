// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import en from '@/i18n/en.json';
import { HdBackupStep } from './vault-hd-wallet-steps';

const copy = vi.hoisted(() => vi.fn<(text: string) => Promise<boolean>>());
vi.mock('@/lib/utils', async (original) => ({ ...await original<typeof import('@/lib/utils')>(), copyTextToClipboard: copy }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const hd = en.vaults.hd;
const WORDS = 'legal winner thank year wave sausage worth useful legal winner thank yellow'.split(' ');

afterEach(() => { cleanup(); copy.mockReset(); });

const shown = () => {
  render(<I18nextProvider i18n={i18n}><HdBackupStep words={WORDS} /></I18nextProvider>);
  fireEvent.click(screen.getByRole('button', { name: hd.backup.show.replace('{{count}}', '12') }));
};

it('copies the backup through the shared clipboard helper (with its LAN-HTTP fallback)', async () => {
  copy.mockResolvedValue(true);
  shown();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.backup.copy })); });
  expect(copy).toHaveBeenCalledExactlyOnceWith(WORDS.join(' '));
  expect(screen.getByRole('button', { name: hd.backup.copied })).toBeTruthy();
});

it('does not claim the words were copied when copying fails', async () => {
  copy.mockResolvedValue(false);
  shown();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.backup.copy })); });
  expect(screen.queryByRole('button', { name: hd.backup.copied })).toBeNull();
});
