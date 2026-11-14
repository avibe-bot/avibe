import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import '../i18n';
import '../index.css';
import { MobileOnboarding } from './MobileOnboarding';
import { isSheetHash } from './bridge';

// The sheet scrolls its document, so native can hand a pull at its top to the sheet's dismissal.
if (isSheetHash(window.location.hash)) document.documentElement.dataset.documentScroll = '';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MobileOnboarding />
  </StrictMode>,
);
