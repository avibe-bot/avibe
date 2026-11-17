import { createRoot } from 'react-dom/client';
import { ThemeProvider } from '../context/ThemeProvider';
import i18n from '../i18n';
import '../index.css';
import { AccountApp } from './AccountApp';

// Native admits only these locale values; apply before the first account render.
const locale = new URLSearchParams(window.location.search).get('locale');
const ready = locale === 'en' || locale === 'zh' ? i18n.changeLanguage(locale) : Promise.resolve();
void ready.then(() => {
  createRoot(document.getElementById('root')!).render(<ThemeProvider><AccountApp /></ThemeProvider>);
});
