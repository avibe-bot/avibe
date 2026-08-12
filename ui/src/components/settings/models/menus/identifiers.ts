// OpenCode identifier scheme (spec §4.4, locked 07-23; opencode-overlay.md).
// Identifiers are `provider/model-id`. The provider segment is the standard
// vendor id; unrecognizable vendors fall back to a single `custom/` provider.
// Identifiers are STABLE across Hub/Direct switches and across source
// add/remove/failover — they never encode a concrete source.

// The standard OpenCode vendor ids come from the backend via the opencode
// agent's `standard_vendors` projection (agent-supply v1.2), server-populated
// from STANDARD_OPENCODE_VENDOR_IDS (core/handlers/model_hub/identifiers.py).
// The UI threads that set through — it no longer hand-mirrors the list, so a
// divergence that would make `set_opencode_menu` reject identifiers can't arise.
export type StandardVendors = ReadonlySet<string>;

/**
 * Provider segment for a source's model, per the FROZEN opencode-overlay.md
 * contract: it is the SOURCE's vendor when that is a standard vendor id, else
 * the single `custom` provider. It is deliberately NOT inferred from the model
 * name — that must byte-match the backend's `opencode_model_id(source.vendor,
 * model.id)`, or `set_opencode_menu` rejects the checked value. So
 * `relay.example` (vendor `custom`) supplying
 * `glm-5.2-air` yields `custom/glm-5.2-air` (not `zhipuai/…`).
 */
export function inferProvider(sourceVendor: string, standardVendors: StandardVendors): string {
  return standardVendors.has(sourceVendor) ? sourceVendor : 'custom';
}

/** Full prefixed identifier for a (source vendor, model id). */
export function buildIdentifier(sourceVendor: string, modelId: string, standardVendors: StandardVendors): string {
  return `${inferProvider(sourceVendor, standardVendors)}/${modelId}`;
}

// `isSourceEligible` lived here and is gone: it mirrored the backend
// `_eligible_for_agent` predicate, whose server-owned inventory inputs the UI
// cannot reconstruct, so it was guaranteed to drift and offer rows the live API
// rejects. Contract v5
// publishes the answer as `AgentSupply.sources.eligibility`; read it through
// `../eligibility`.
