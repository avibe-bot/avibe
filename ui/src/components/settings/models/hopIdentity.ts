export type HopIdentity = Readonly<{
  source_id: string;
  model_id: string;
}>;

export const hopIdentity = (hop: HopIdentity): HopIdentity => ({
  source_id: hop.source_id,
  model_id: hop.model_id,
});

export const equalHopIdentity = (
  left: HopIdentity | null | undefined,
  right: HopIdentity | null | undefined,
): boolean => Boolean(
  left
  && right
  && left.source_id === right.source_id
  && left.model_id === right.model_id,
);

export const hopBelongsToSource = (hop: HopIdentity, sourceId: string): boolean =>
  hop.source_id === sourceId;
