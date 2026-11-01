import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import i18n from '@/i18n';
import '@/index.css';

import { DesignRoot } from './Canvas';
import type { DesignLang, DesignTheme } from './frame';
import { ALL_FRAMES } from './registry';
import { installFixedClock } from './shell/clock';
import { designNetwork, installDesignNetwork } from './shell/network';

installFixedClock();
installDesignNetwork();

const params = new URLSearchParams(window.location.search);
const lang = (params.get('lang') === 'en' ? 'en' : 'zh') as DesignLang;
void i18n.changeLanguage(lang);

declare global {
  interface Window {
    __DESIGN__?: {
      frames: { id: string; file: string; title: string; status: string; width: number; height: number; themes: DesignTheme[] }[];
      unfixtured: () => string[];
    };
  }
}
window.__DESIGN__ = {
  frames: ALL_FRAMES.map((f) => ({ id: f.id, file: f.file, title: f.title, status: f.status, width: f.width, height: f.height, themes: f.themes ?? ['light', 'dark'] })),
  unfixtured: () => designNetwork.unfixtured(),
};

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <DesignRoot params={params} />
  </StrictMode>,
);
