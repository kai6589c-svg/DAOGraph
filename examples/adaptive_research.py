"""Offline research example: a conflict inserts verification and changes the plan."""

import argparse
import json
from pathlib import Path

from research import Research, Source, Task, plain, write_trace


def build_graph():
    question = "What is the current service release limit?"
    sources = (
        Source("s01", "publisher-one", "fixture://one", question, "2026-10-01"),
        Source("s02", "publisher-two", "fixture://two", question, "2026-10-01"),
        Source("s03", "service-owner", "fixture://official", question, "2026-10-01", True),
    )
    values = {"s01": "12", "s02": "21", "s03": "12"}

    def read(source):
        value = values[source.id]
        return {
            "claims": [
                {
                    "key": "release_limit",
                    "value": value,
                    "excerpt": f"The release limit is {value}.",
                }
            ]
        }

    application = Research(Task(question, ("release_limit",), "2026-01-01"), sources, read)
    return application.graph()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    events = list(build_graph().stream({"evidence": [], "attempts": []}))
    for event in events:
        if event.kind in ("plan", "plan_reused"):
            print(f"{event.kind}: {[step.id for step in event.plan.steps]} — {event.message}")
        if event.kind == "node_end":
            print(f"executed: {event.step.id}")
    result = events[-1].result
    print(
        json.dumps({"status": result.status, "answer": plain(result.state.get("answer"))}, indent=2)
    )
    if args.trace:
        write_trace(events, args.trace)
    if result.status != "completed":
        raise SystemExit("Research example did not complete")


if __name__ == "__main__":
    main()
