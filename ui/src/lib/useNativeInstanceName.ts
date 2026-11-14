import { useEffect, useState } from 'react';

import { hasNativeCapability, nativeAppInfo } from './native';

// The selected instance's name as the App names it, read once per document from `app.info`
// (a rename shows after the next load). Null without `instancePresentation`, or when the shell
// gives no name: the header then keeps "Avibe" (ui/design: mobile-app/switcher/b-home).
let pending: Promise<string | null> | null = null;

const readName = (): Promise<string | null> => {
  pending ??= nativeAppInfo()
    .then((info) => {
      const name = info?.instance?.name;
      return typeof name === 'string' && name.trim() ? name.trim() : null;
    })
    .catch(() => null);
  return pending;
};

export function useNativeInstanceName(): string | null {
  const [name, setName] = useState<string | null>(null);
  useEffect(() => {
    if (!hasNativeCapability('instancePresentation')) return undefined;
    let alive = true;
    void readName().then((value) => {
      if (alive) setName(value);
    });
    return () => {
      alive = false;
    };
  }, []);
  return name;
}
