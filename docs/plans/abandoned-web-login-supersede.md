# Abandoned Web login recovery

A new explicit sign-in from Web or IM retires a non-terminal native Web flow
for the same backend (and OpenCode provider) when it is unowned or owned by the
same Model Hub Source. Termination must finish and release its credential lease
before a replacement starts. IM flows, another Source's flows, and flows already
being cancelled retain exclusion. Credential custody and the 900-second deadline
remain unchanged. No UI or credential-storage changes are needed.

Evidence: single-flight owner tests cover takeover from both surfaces, same-source
ownership, and protected owners. AUTH-SETUP-934 exercises abandoned Web sign-in,
retry, and successful completion through the Web API with synthetic CLI transport.
Existing AUTH-SETUP-211 continues to cover localized protected-owner rejection.
Manual residual: close the tab mid-login, return, and sign in again.
