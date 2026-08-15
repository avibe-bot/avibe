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

An unrecorded mock mutation throws `uncontracted_mock_transition`. The error
contains an exact `--record-miss` command. Run that command from the repository
root; it finds the recorded path to the missing pre-state, appends the action to
`model_hub_mock_sequences.json`, and regenerates the corpus. Review both files.

Only the five currently exercised sequences are recorded. There is deliberately
no speculative walkthrough corpus. Fixture-world content grows when a sequence
needs it, while the enforcement boundary is complete: unregistered collaborator,
network, filesystem, clock, or id access fails generation.

The local dynamic event feed and static runtime-status card remain display
fixtures. They do not classify observations, choose targets, validate writes,
compute guards, prune routes, or derive supply. Every such policy-bearing entry
point goes through the transition corpus, including entry points with no record
yet; those fail with the recording command instead of falling back to TypeScript.
