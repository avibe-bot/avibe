# Gateway Route Status and Agent Attribution

Scenario: `MH-GATEWAY-STATUS-001` in the Model Hub scenario catalog.

The Model Hub backend header summarizes the routes of the displayed model
catalog, not the configured model of an enabled named Agent. Changing an Agent
selection or enabling/disabling an Agent must not change this route summary.

- Complete per-model supply determines available, partially unavailable,
  unavailable, or unconfigured routes. An empty catalog and an incomplete
  supply read have distinct neutral states.
- Named Agent supply issues retain their own attribution in a collapsible
  footer below the model list. Its collapsed label contains only a count;
  expanded rows display the Agent name, model, and reason separately.
- Long identifiers wrap inside the expanded footer. They never enter the
  compact mode-switch trigger or change the header's dimensions.
- This is a read-only presentation change. It does not select a different
  model, modify routes, or change runtime lifecycle behavior.

Verification covers route-summary invariance, absent/incomplete supply,
localized disclosure behavior, keyboard/touch interaction, and actual
desktop/mobile layout with long identifiers and all localized route statuses
under platform and wider fallback fonts. The existing Model Hub surface
tokens and typography remain unchanged.
