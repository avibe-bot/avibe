# Show Runtime v3.0.13 Composite Artifact Fixture

`show-runtime-manifest.json` is the byte-for-byte release asset from Avibe
`v3.0.13`. `composite_artifacts.json` was measured from the six archives named
and digested by that manifest.

Each link row contains, in order:

1. archive member index;
2. `symlink` or `hardlink`;
3. member name;
4. raw `linkname`;
5. immediate target member index, or `null` when another archive symlink is
   required to reach it;
6. fully resolved target member index;
7. fully resolved target name;
8. fully resolved target type.

The executable acceptance test verifies the release-manifest digest and binds
every platform record to its released archive name, size, and SHA-256 before
replaying its link topology through the shared manager.
