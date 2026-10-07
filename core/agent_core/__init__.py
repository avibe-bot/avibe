"""The Avibe Agent engine (``docs/plans/avibe-agent-core.md``).

Layers: ``ai`` (provider adapters), ``agent`` (loop), ``harness`` (transcript and
context management), ``tools`` (coding tools). This package knows nothing about
IM platforms, the controller, Watch, or Model Hub's HTTP surface; the backend
adapter in ``modules/agents/vibey/`` supplies those through the interfaces here.
Shapes follow ``docs/plans/agent-core-contracts/``.
"""
