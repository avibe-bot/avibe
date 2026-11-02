// First: frames get their own storage before any product module reads it.
import { designStorage } from './shell/storage';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import i18n from '@/i18n';
import '@/index.css';

import { DesignRoot } from './Canvas';
import type { DesignLang, DesignTheme } from './frame';
import { ALL_FRAMES } from './registry';
import { installFixedClock } from './shell/clock';
import { apiResponses } from './shell/fixtures';
import { designNetwork, installDesignNetwork } from './shell/network';

installFixedClock();
installDesignNetwork();

const params = new URLSearchParams(window.location.search);
const lang = (params.get('lang') === 'en' ? 'en' : 'zh') as DesignLang;
void i18n.changeLanguage(lang);
// One document draws one frame (the canvas puts each in its own iframe), so its API
// fixtures are in place before any provider can fetch.
const shown = ALL_FRAMES.find((f) => f.id === params.get('frame'));
designNetwork.setResponses(apiResponses(shown?.fixtures));
designStorage.seed(shown?.fixtures?.storage);

declare global {
  interface Window {
    __DESIGN__?: {
      frames: { id: string; file: string; title: string; status: string; width: number; height: number; themes: DesignTheme[]; hover?: string }[];
      unfixtured: () => string[];
      quiet: () => Promise<void>;
    };
  }
}
window.__DESIGN__ = {
  frames: ALL_FRAMES.map((f) => ({ id: f.id, file: f.file, title: f.title, status: f.status, width: f.width, height: f.height, themes: f.themes ?? ['light', 'dark'], hover: f.hover })),
  unfixtured: () => designNetwork.unfixtured(),
  quiet: () => designNetwork.quiet(),
};

document.body.dataset.designStage = 'render';
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <DesignRoot params={params} />
  </StrictMode>,
);
