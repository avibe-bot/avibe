# Avibe

NPM entrypoint for [Avibe](https://github.com/avibe-bot/avibe), the local-first Agent OS.

On a Mac or Windows PC, the [Desktop App](https://avibe.bot) is the recommended way to run Avibe; this package is for people who want the command line through npm.

Avibe is installed and upgraded as the Python package `avibe-os`.
This package is a thin bootstrapper for developers who expect npm-native entry
points.

## Quick Start

```bash
npx @avibe/cli
```

Or install the npm entrypoint globally:

```bash
npm install -g @avibe/cli
vibe
```

The first run installs the underlying `vibe` command if needed, then starts the
local Avibe setup wizard. The global npm package exposes both `vibe` and
`avibe`; `vibe` matches the rest of the Avibe docs, while `avibe` remains
available when you want to call the npm bootstrapper explicitly.

## Commands

```bash
vibe            # start Avibe after global npm install
avibe install   # install or refresh the underlying avibe-os Python CLI
avibe init      # start the setup wizard
avibe start     # start Avibe
avibe status    # show runtime status
avibe doctor    # diagnose local setup issues
avibe remote    # configure remote Web UI access
avibe upgrade   # upgrade the underlying avibe-os Python package
```

After bootstrap, the npm entrypoint delegates to the real `vibe` command.

## Requirements

- Node.js 16+
- macOS / Linux: `curl` or `wget`
- Windows: PowerShell

The installer downloads `uv` automatically when it is missing. `uv` manages the
Python runtime for `avibe-os`.

## Docs

- <https://docs.avibe.bot>
- <https://github.com/avibe-bot/avibe>
