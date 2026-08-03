"""Graph construction.

    START -> prepare -> ┬-> security ─┐
                        ├-> style ────┼-> finalize -> END
                        └-> architecture ┘

The three agent nodes hang off a single conditional edge from `prepare`. Returning
a *list* of node names from the router is what produces the fan-out; LangGraph then
runs those branches concurrently and the `operator.add` reducers on `findings` and
`calls` merge their writes. Because every node is `async def` and the graph is
driven with `ainvoke`, this is real concurrency on one event loop rather than a
thread pool.

Dependencies are bound here with `functools.partial`, so the compiled graph is a
closed-over object with no ambient state. A test builds one around a stub client
and needs no patching.
"""

from __future__ import annotations

from functools import partial

from anthropic import AsyncAnthropic
from langgraph.graph import END, START, StateGraph

from reviewhive.agents.base import AgentSpec
from reviewhive.agents.definitions import AGENTS
from reviewhive.config import Settings
from reviewhive.graph.nodes import Deps, finalize, prepare_diff, run_agent_node
from reviewhive.graph.state import ReviewState
from reviewhive.models import ReviewResult

FINALIZE = "finalize"


def build_review_graph(
    client: AsyncAnthropic,
    settings: Settings,
    agents: tuple[AgentSpec, ...] = AGENTS,
):
    """Compile the review graph. Do this once per process, not once per PR."""
    deps = Deps(client=client, settings=settings)
    agent_names = [spec.name for spec in agents]

    def route(state: ReviewState) -> list[str]:
        # A PR touching only lockfiles should cost zero tokens, not three empty calls.
        if state["budget"].is_empty:
            return [FINALIZE]
        return agent_names

    graph = StateGraph(ReviewState)
    graph.add_node("prepare", partial(prepare_diff, deps=deps))
    for spec in agents:
        graph.add_node(spec.name, partial(run_agent_node, deps=deps, spec=spec))
    graph.add_node(FINALIZE, partial(finalize, deps=deps))

    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", route, [*agent_names, FINALIZE])
    for name in agent_names:
        graph.add_edge(name, FINALIZE)
    graph.add_edge(FINALIZE, END)

    return graph.compile()


async def review_diff(
    diff_text: str,
    client: AsyncAnthropic,
    settings: Settings,
    agents: tuple[AgentSpec, ...] = AGENTS,
    *,
    focus: str | None = None,
) -> ReviewResult:
    """Convenience wrapper: build, run, and unwrap. Callers holding a long-lived
    process should build the graph once and call `ainvoke` themselves."""
    compiled = build_review_graph(client, settings, agents)
    final_state = await compiled.ainvoke({"diff_text": diff_text, "focus": focus})
    return final_state["result"]
