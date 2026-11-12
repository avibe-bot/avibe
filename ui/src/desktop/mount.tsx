import { createRoot } from 'react-dom/client';
import { ThemeProvider } from '../context/ThemeProvider';
import '../i18n';
import '../index.css';
import { AccountApp } from './AccountApp';

createRoot(document.getElementById('root')!).render(<ThemeProvider><AccountApp /></ThemeProvider>);
