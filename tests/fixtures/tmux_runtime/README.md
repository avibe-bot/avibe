# tmux v3.6b Release Fixtures

`v3.6b-released-manifest.json` is the byte-for-byte packaged manifest from
Avibe commit `0eb15bea2f573b207caaf6c4a3c6e891a3cc0d2d`.
`v3.6b-release-artifacts.json` records the archive and extracted `tmux` binary
digests measured from every archive named by that manifest.

The executable acceptance test verifies the frozen manifest digest, binds all
four platform rows to their released archive name, URL, size, and SHA-256, and
checks that the production tmux released-state reader supplies the measured
binary SHA-256 values to the shared runtime owner.
