type PartialMutationResult = {
  ok: boolean;
  partial?: boolean;
};

type PartialMutationSettlement = {
  message: string;
  warn: (message: string) => void;
  notify: () => void;
  reload: () => Promise<void>;
  clearExpanded?: () => void;
};

export const reconcileOpencodePartialMutation = async (
  result: PartialMutationResult,
  settlement: PartialMutationSettlement,
): Promise<boolean> => {
  if (result.ok || !result.partial) return false;

  settlement.warn(settlement.message);
  settlement.notify();
  settlement.clearExpanded?.();
  await settlement.reload();
  return true;
};
