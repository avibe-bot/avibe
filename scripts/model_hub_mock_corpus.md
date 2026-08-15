# Model Hub mock corpus

`generate_model_hub_mock_corpus.py` executes the recorded action sequences
against the real Python `ModelHubService`. It records the full canonical config,
the complete registered fixture world, each server result or error, and the
server-generated read projections after every step.

Generate the corpus:

```shell
python3 scripts/generate_model_hub_mock_corpus.py
```

Verify that the checked-in corpus matches the current server:

```shell
python3 scripts/generate_model_hub_mock_corpus.py --check
```

An unrecorded mock mutation throws `uncontracted_mock_transition`. When the
operation has an authoritative service dispatch, the error contains an exact
`--record-miss` command. Run that command from the repository root; it finds the
recorded path to the missing pre-state, appends the action to
`model_hub_mock_sequences.json`, and regenerates the corpus. Review both files.
The transition id is a SHA-256 digest. A separate request token carries only the
canonical request: sensitive fields are replaced by `<sensitive>` and volatile
per-call fields receive stable first-appearance aliases such as `<volatile:1>`
before either value is built. The error prints
that redacted canonical request for inspection. If a concrete request needs
fixture content that is not registered, or the server has no dispatch for the
operation, the error names the reason instead of advertising a command that
cannot produce server evidence.
The generator's operation registry owns the authoritative service dispatch,
recording command and probe, and explicit reachability (`seed` or a declared
prerequisite sequence). Its fifth facet, request identity, declares the
identity-bearing remainder plus sensitive and volatile field paths. Generation
executes every declared path in the sealed fixture world and records the exact
transition ids that succeeded. Only those execution-proven ids can advertise a
recovery command. `--check` runs every advertised command against isolated files
and verifies that it records the promised transition. The mock's advertised set
and request canonicalization are generated from those same entries.

The corpus keeps the raw service result. The TypeScript operation registry owns
one response transform and call-settlement contract per `ModelsApi` operation,
and both `LiveApi` and `MockStore` use them. The shared call contract owns abort
handling and commits replay state only after a non-aborted call settles. The
contract test starts from each
registered reachability path, runs the exact command copied from the thrown
error, then verifies the transition replays through the shared transform. A new
operation cannot silently omit dispatch, recovery, reachability, response
normalization, cancellation, or request identity.

Production imports only the live client. Hermetic callers opt into
`mock-only/modelsApi.mockEntry.ts`, which lazily loads the replay engine and
corpus. The UI build resolves the live client's module graph and fails if it can
reach any file under `mock-only/`; a post-build marker scan remains as a second
check on emitted production assets.

Only the five currently exercised sequences are recorded. There is deliberately
no speculative walkthrough corpus. Fixture-world content grows when a sequence
needs it, while the enforcement boundary is complete: unregistered collaborator,
network, filesystem, clock, or id access fails generation.

The local dynamic event feed and static runtime-status card remain display
fixtures. Runtime installation now replays the authoritative route added by
#1462. The display fixtures do not classify observations, choose targets,
validate writes, compute guards, prune routes, or derive supply. Every such
policy-bearing entry point goes through the transition corpus, including entry
points with no record yet; those fail with an execution-proven recording command
or an explicit unproven-request reason instead of falling back to TypeScript.
