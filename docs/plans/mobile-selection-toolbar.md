# Mobile selection toolbar gesture lifetime

> Superseded by [`mobile-selection-toolbar-ios27.md`](./mobile-selection-toolbar-ios27.md).
> The implementation now uses a bounded press snapshot and full-rectangle
> touch placement checks for the iOS 27 selection-handle behavior.

This document records the earlier pointer-capture design from PR #2249 and is
kept only as historical context. Do not use its gesture-lifetime or placement
contract as the current behavior.
