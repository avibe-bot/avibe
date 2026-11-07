export interface VaultFetchOptions {
  /** Static secret name or NAME.field. Never the credential value. */
  auth: string;
  url: string;
  method?: string;
  headers?: Record<string, string> | string[];
  /** Inline UTF-8 body. File paths and output files belong to the caller. */
  body?: string;
  approvalWaitSeconds?: number;
  noApprovalWait?: boolean;
}

export interface VaultFetchContext {
  /** Same raw invocation context as the replaced CLI; never browser headers. */
  callerEnv?: Record<string, string | undefined>;
  cwd?: string;
  /** Abort must close the actual IPC request, not only abandon its Promise. */
  signal?: AbortSignal;
}

export interface VaultFetchResult {
  version: 1;
  execution: 'completed' | 'not_executed' | 'execution_unknown';
  exit_code: 0 | 1;
  payload: {
    ok: boolean;
    kind: 'vault_fetch' | 'error';
    status?: number;
    headers?: Record<string, string>;
    body?: string;
    code?: string;
    error?: string;
    hint?: string;
    details?: Record<string, unknown>;
  };
}

/** Node-only server helper. One submission; uncertain outcomes are never replayed. */
export declare function vaultFetch(options: VaultFetchOptions, context?: VaultFetchContext): Promise<VaultFetchResult>;
