import { createContext, useContext } from 'react';

// The route still owns its header and state; the shell supplies only a DOM slot.
export const DesktopWindowChromeContext = createContext<HTMLDivElement | null>(null);
export const useDesktopWindowChrome = () => useContext(DesktopWindowChromeContext);
