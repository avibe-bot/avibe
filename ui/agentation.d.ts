declare module 'agentation' {
  import type { ComponentType } from 'react';

  type AgentationProps = {
    copyToClipboard?: boolean;
    onCopy?: (text: string) => void;
    onSubmit?: (output: string) => void;
  };

  export const Agentation: ComponentType<AgentationProps>;
}
