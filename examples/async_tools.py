"""Async tools, fan-in dependencies, and immutable state snapshots."""

import asyncio
import operator

from daograph import Context, DAOGraph, Node, Plan, Step


async def fetch_primary(context: Context):
    await asyncio.sleep(0)  # Replace with an async model, HTTP, or database call.
    return {"evidence": ["Primary source"]}


async def fetch_secondary(context: Context):
    await asyncio.sleep(0)
    return {"evidence": ["Secondary source"]}


def summarize(context: Context):
    return {"answer": "; ".join(context.state["evidence"])}


async def plan(context: Context):
    return Plan(
        (
            Step("primary", "fetch_primary"),
            Step("secondary", "fetch_secondary"),
            Step("summary", "summarize", after=("primary", "secondary")),
        ),
        reason="Gather two sources before summarizing",
    )


async def main():
    graph = DAOGraph(
        goal="Summarize two sources",
        nodes=(
            Node("fetch_primary", fetch_primary),
            Node("fetch_secondary", fetch_secondary),
            Node("summarize", summarize),
        ),
        planner=plan,
        reducers={"evidence": operator.add},
    )
    async for event in graph.astream({"evidence": []}):
        if event.kind == "node_end":
            print(f"EXECUTED: {event.step.id}")
        if event.result:
            print(event.result.state["answer"])


if __name__ == "__main__":
    asyncio.run(main())
