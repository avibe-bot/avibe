import { createContext, type ReactNode, useContext } from 'react';

/** The frame a sign-in step is drawn in, supplied by the shell around the shared flow. */
export type SignInFrameProps = {
  children: ReactNode;
  /** A step that can go back: the shell draws its back control. */
  back?: { disabled: boolean; onBack: () => void };
};

export type SignInChromeValue = {
  Frame: (props: SignInFrameProps) => ReactNode;
  /** Where the steps run: it picks the few lines that name the device's own tools. */
  platform: 'ios' | 'desktop';
};

export const SignInChromeContext = createContext<SignInChromeValue | null>(null);

/** Supplies the shell's frame and platform to the shared steps. */
export const SignInChrome = SignInChromeContext.Provider;

/** The shell the steps are drawn in. */
export function useSignInPlatform(): SignInChromeValue['platform'] {
  return useContext(SignInChromeContext)?.platform ?? 'ios';
}
