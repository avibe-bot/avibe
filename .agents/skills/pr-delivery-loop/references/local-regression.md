# Local regression

For Avibe, bind the action and recheck `MERGED`, then follow this procedure. Other repos offer these choices only when their own documented local workflow applies; never invent a target.

1. Fetch origin and fast-forward the **primary** checkout with `--ff-only`;
   verify `master` equals the fetched `origin/master` SHA. Establish that the
   actual `sync_source()` input, including ignored deployable files, matches that
   Git tree under the runner's sender exclusions, and keep it unchanged through
   the update. Git status or `dirty=false` alone is not proof. A mismatch or an
   unprovable match blocks mutation; never stash, discard, or commit user edits
   to bypass it. Invoke the runner from this verified checkout, not a task tree.
2. Confirm the persistent **local** master environment (`avr-master` /
   `avibe-master` by default), product config, and runtime environment file exist
   and are readable. Missing state is a blocker, not provisioning/re-seeding
   authority. Use the documented local Lima route on macOS; preserve existing
   bind/port settings explicitly when different from defaults.
3. For the existing target, run from the verified checkout:

   ```bash
   python3 scripts/incus_regression.py up --target master --reset-mode none --clean --env-file /dev/null
   ```

   `--env-file /dev/null` suppresses automatic env discovery and leaves
   `/etc/avibe-regression.env` untouched; an empty regular file does not.
   Stop before mutation if the checked-out runner cannot preserve this behavior.
   `--clean` reconciles disposable source/build output, including same-size,
   same-mtime stale bytes. Preserve credentials, runtime environment, product
   state, pairing, agent homes, and sessions; neither choice authorizes a reset.
4. Let the runner rebuild assets. Verify the receipt and served source commit
   match the intended SHA and the runtime environment is unchanged without
   exposing values, before success or E2E checks. Report environment, deployed
   commit, and actual test results; health alone is not an end-to-end pass.

Neither choice authorizes production, a remote tenant, or the running local Avibe
to be updated or restarted.
