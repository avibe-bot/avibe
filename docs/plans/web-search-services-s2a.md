# Web search services S2a: readers before writers (historical)

This records S2a's reader-only delivery. S2b-2 now activates the producer and service read
together; see `web-search-services.md` §9 for current release ordering.

Source of truth: [web-search-services.md](web-search-services.md), especially §4.3,
§6.5, §8 and §12. S2a is dormant: it accepts and renders the final service shapes
but never searches, meters a live service request, writes service evidence, or
changes `SEARCH_SERVICES_ACTIVE`. S2b owns those producers and activation.

## Change contract

- C-1 accepts optional attempts, estimated costs and a search-API executor origin;
  released native/model records load and serialize unchanged. `HostedSource` and
  the assistant's four-protocol `Origin` are unchanged.
- When attempts are present, they alone own executor identity and the total.
  Skips cost nothing; unknown tried charges make a known sum a lower bound;
  nonzero list/estimated figures are approximate and an exact zero is not.
  Current Source state governs remedies, never historical accounting.
- The Web card and IM consume one projection, with localized switch and repair
  notes; released model-only payloads remain byte-identical.
- Service ledger quantities are independently identified and repaired on both
  doors. Model counts never include service requests. Current catalog prices
  value service quantities only when the existing model price table is present.
- Usage has a separate service table and candidate-gated Source detail; removing
  a Source/catalog row preserves the counts and reported amounts.
- Evidence readers accept `service` without a live producer. Contract v19 is open for S2a; it remains dormant until S2b activates the producer.

## Owning validation boundaries

- C-1 schema/reader table: new service variants plus released load fixtures;
  catches strict readers dropping new fields or narrowing released sources.
- Hosted activity matrix plus shared Web fixtures: last tried role, skip-only,
  zero/unknown/estimated/partial totals, switch, every blocked remedy and
  supersession. Existing shared fixtures protect unchanged model payloads.
- Ledger write/read and summary: repaired relations, released rows, removal,
  catalog repricing, hourly/daily counts and pricing absence. Existing usage tests
  protect retention and model accounting rather than duplicating them.
- UI tests: shared card fixtures in both locales, Usage service rows, active and
  dormant detail. Typecheck, lint and production build cover all consumers.

Downgrade is the documented §12 boundary once S2b writes these shapes. S2a alone
writes none from live paths; no migration, reset, deployment or public-doc change.
