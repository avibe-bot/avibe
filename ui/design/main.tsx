// First: frames get their own storage before any product module reads it.
import { designStorage } from './shell/storage';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import i18n from '@/i18n';
import '@/index.css';
// The app build makes every product stylesheet global: one a component imports applies to every
// surface, including ones that use its classes without importing it. A frame document loads only
// its own frame file, so it loads them all here. The pet's sheets ship in its own lazy window.
import.meta.glob(['/src/**/*.css', '!/src/index.css', '!/src/pet/**'], { eager: true });

import { DesignRoot } from './Canvas';
import type { DesignLang, DesignTheme } from './frame';
import { loadAllFrames, loadFrame } from './registry';
import { installFixedClock } from './shell/clock';
import { apiResponses } from './shell/fixtures';
import { designNetwork, installDesignNetwork } from './shell/network';

installFixedClock();
installDesignNetwork();

const params = new URLSearchParams(window.location.search);
const lang = (params.get('lang') === 'en' ? 'en' : 'zh') as DesignLang;
void i18n.changeLanguage(lang);

declare global {
  interface Window {
    __DESIGN__?: {
      frames: { id: string; file: string; title: string; status: string; width: number; height: number; themes: DesignTheme[]; hover?: string; click?: string[]; wait?: number }[];
      unfixtured: () => string[];
      quiet: () => Promise<void>;
      t: (key: string) => string;
    };
  }
}
// A key chosen at run time, so outside the typed key union.
const copy = i18n.t.bind(i18n) as unknown as (key: string) => string;

async function boot() {
  // A frame document loads only its own frame file; the canvas and the frame list load them all.
  const id = params.get('frame');
  const shown = id ? await loadFrame(id) : undefined;
  const frames = id ? [] : await loadAllFrames();
  // One document draws one frame (the canvas puts each in its own iframe), so its API
  // fixtures are in place before any provider can fetch.
  designNetwork.setResponses(apiResponses(shown?.fixtures));
  for (const [key, text] of Object.entries(shown?.fixtures?.copy ?? {})) i18n.addResource(lang, 'translation', key, text[lang]);
  designNetwork.setSockets(shown?.fixtures?.sockets);
  designStorage.seed(shown?.fixtures?.storage);
  window.__DESIGN__ = {
    frames: frames.map((f) => ({ id: f.id, file: f.file, title: f.title, status: f.status, width: f.width, height: f.height, themes: f.themes ?? ['light', 'dark'], hover: f.hover, click: f.click, wait: f.wait })),
    unfixtured: () => designNetwork.unfixtured(),
    quiet: () => designNetwork.quiet(),
    // Click selectors name copy by key (`{t:<key>}`); the page resolves it in the frame's language.
    t: (key: string) => (i18n.exists(key) ? copy(key) : `{missing copy ${key}}`),
  };
  document.body.dataset.designStage = 'render';
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <DesignRoot params={params} frames={frames} frame={shown} />
    </StrictMode>,
  );
}

void boot();
