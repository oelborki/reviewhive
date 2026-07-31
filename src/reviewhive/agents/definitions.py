"""The three agents.

Kept as one registry rather than three near-empty modules: an agent is entirely
described by its name and its prompt file, so a module per agent would be three
files of configuration with no behaviour. Adding a fourth agent means adding a
prompt and one line here — and `AGENTS` is what the graph builder iterates, so a
new entry wires itself into the fan-out automatically.
"""

from __future__ import annotations

from reviewhive.agents.base import AgentSpec

SECURITY = AgentSpec(
    name="security",
    display="Security & Correctness",
    prompt_file="security.md",
)

STYLE = AgentSpec(
    name="style",
    display="Style & Maintainability",
    prompt_file="style.md",
)

ARCHITECTURE = AgentSpec(
    name="architecture",
    display="Architecture",
    prompt_file="architecture.md",
)

AGENTS: tuple[AgentSpec, ...] = (SECURITY, STYLE, ARCHITECTURE)
