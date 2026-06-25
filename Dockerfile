# ============================================================
# Stage 1: Build UI (React + Vite)
# ============================================================
FROM node:20-slim AS ui-builder

WORKDIR /app/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --ignore-scripts
COPY ui/ .
RUN npm run build


# ============================================================
# Stage 2: Python base (API-only tests)
# ============================================================
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    tmux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files
COPY . .

# Copy built UI from stage 1
COPY --from=ui-builder /app/ui/dist /app/ui/dist

# hatch-vcs needs git to determine version; .git is excluded by .dockerignore.
# Use SETUPTOOLS_SCM_PRETEND_VERSION to provide a fake version for the build.
ARG SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0.dev0
RUN SETUPTOOLS_SCM_PRETEND_VERSION=${SETUPTOOLS_SCM_PRETEND_VERSION} \
    pip install --no-cache-dir -e .

# Runtime config
ENV AVIBE_HOME=/data/avibe
ENV PYTHONUNBUFFERED=1

EXPOSE 5123

COPY scripts/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]


# ============================================================
# Stage 3: Integration target (base + agent CLIs)
# For full E2E tests with real agent backends.
# Build with: docker build --target integration .
# ============================================================
FROM base AS integration

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tmux \
    && rm -rf /var/lib/apt/lists/*

# Reuse the Node runtime from the UI builder stage.
# This avoids the NodeSource Debian 13 arm64 binary that currently segfaults
# inside the regression container before Codex can even start.
COPY --from=ui-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=ui-builder /usr/local/bin/npm /usr/local/bin/npm
COPY --from=ui-builder /usr/local/bin/npx /usr/local/bin/npx
COPY --from=ui-builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=ui-builder /usr/local/include/node /usr/local/include/node
COPY --from=ui-builder /usr/local/share/man/man1/node.1 /usr/local/share/man/man1/node.1
COPY --from=ui-builder /usr/local/share/doc/node /usr/local/share/doc/node
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Install agent CLIs (failures are non-fatal: not all may be needed)
RUN npm install -g @anthropic-ai/claude-code 2>/dev/null || echo "WARN: claude-code install failed (optional)"
RUN npm install -g @openai/codex 2>/dev/null || echo "WARN: codex install failed (optional)"
# OpenCode: use the official installer and expose the binary on PATH
RUN bash -lc 'set -euo pipefail; curl -fsSL https://opencode.ai/install | bash && ln -sf /root/.opencode/bin/opencode /usr/local/bin/opencode && opencode --version' \
    || echo "WARN: opencode install failed (optional)"

ENTRYPOINT ["/docker-entrypoint.sh"]
