import { useCallback, useRef, useState } from 'react';

export type OperationToken = number;

/**
 * One operation at a time on a page: a token marks who owns it, so a result that arrives after
 * its operation was retired changes nothing.
 */
export function useOperationOwner<Action extends string>() {
  const nextToken = useRef(0);
  const currentToken = useRef<OperationToken | null>(null);
  const [operation, setOperation] = useState<{
    token: OperationToken;
    action: Action;
  } | null>(null);

  const beginOperation = useCallback((action: Action): OperationToken | null => {
    if (currentToken.current !== null) return null;
    const token = nextToken.current + 1;
    nextToken.current = token;
    currentToken.current = token;
    setOperation({ token, action });
    return token;
  }, []);

  const ownsOperation = useCallback(
    (token: OperationToken): boolean => currentToken.current === token,
    [],
  );

  const finishOperation = useCallback((token: OperationToken): boolean => {
    if (currentToken.current !== token) return false;
    currentToken.current = null;
    setOperation((active) => active?.token === token ? null : active);
    return true;
  }, []);

  const isOperationLocked = useCallback(
    (): boolean => currentToken.current !== null,
    [],
  );

  /** Ends whatever operation is running without its result: an account change retired it. */
  const retireOperation = useCallback(() => {
    currentToken.current = null;
    setOperation(null);
  }, []);

  return {
    beginOperation,
    busy: operation?.action ?? null,
    finishOperation,
    isOperationLocked,
    locked: operation !== null,
    ownsOperation,
    retireOperation,
  };
}

export type OperationOwner<Action extends string> = ReturnType<typeof useOperationOwner<Action>>;
