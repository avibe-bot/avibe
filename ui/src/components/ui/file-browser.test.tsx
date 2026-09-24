/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { FileBrowser } from './file-browser';

afterEach(cleanup);

it.each(['en', 'zh'] as const)('localizes known favorites in both shared layouts in %s while retaining path targets', async (language) => {
  const i18n = createInstance();
  await i18n.init({ lng: language, resources: { en: { translation: en }, zh: { translation: zh } } });
  const text = (language === 'en' ? en : zh).directoryBrowser;
  const favorites = [
    { key: 'home', path: '/Users/alice', label: text.favoritesHome },
    { key: 'desktop', path: '/Users/alice/Desktop', label: text.favoritesDesktop },
    { key: 'documents', path: '/Users/alice/Documents', label: text.favoritesDocuments },
    { key: 'downloads', path: '/Users/alice/Downloads', label: text.favoritesDownloads },
    { key: 'applications', path: '/Applications', label: text.favoritesApplications },
    { key: 'drive_z', path: 'Z:\\', label: 'Z:\\' },
    { key: 'custom', path: '/mnt/backup/daily', label: '/mnt/backup/daily' },
  ];
  const navigate = vi.fn();
  render(
    <I18nextProvider i18n={i18n}>
      <FileBrowser
        cwd="/Users/alice"
        crumbs={[]}
        sysFavs={favorites}
        projectFavs={[{ label: 'Project name', path: '/project' }]}
        onNavigate={navigate}
        showHidden={false}
        onShowHiddenChange={() => {}}
        listContent={null}
      />
    </I18nextProvider>,
  );

  // jsdom renders both responsive surfaces: the mobile chips and desktop rail.
  for (const favorite of favorites) {
    const buttons = screen.getAllByRole('button', { name: favorite.label, exact: true });
    expect(buttons).toHaveLength(2);
    for (const button of buttons) {
      expect(button.getAttribute('title')).toBe(favorite.path);
      fireEvent.click(button);
      expect(navigate).toHaveBeenLastCalledWith(favorite.path);
    }
  }
  expect(screen.getAllByRole('button', { name: 'Project name', exact: true })).toHaveLength(2);
});
