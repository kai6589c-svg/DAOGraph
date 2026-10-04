"""Pause before a simulated irreversible effect and resume from JSON."""

from daograph import Checkpoint, Constraints, Context, DAOGraph, Node, Plan


def prepare(context: Context):
    return {"prepared": True, "draft": "Release 0.1.0"}


def publish(context: Context):
    # Simulated only. A real API call belongs here, using a host idempotency key.
    return {"published": True, "receipt": "simulated-release-001"}


def plan(context: Context):
    return Plan.chain("prepare", "publish", reason="Prepare and publish the release")


def build_graph():
    return DAOGraph(
        goal="Prepare and publish a release after approval",
        nodes=(
            Node("prepare", prepare),
            Node("publish", publish, side_effect=True, reversibility=0.1),
        ),
        planner=plan,
        constraints=Constraints(max_steps=4),
    )


if __name__ == "__main__":
    graph = build_graph()
    result = graph.invoke({})
    assert result.status == "paused" and result.checkpoint is not None
    print(f"PAUSED: {result.reason}; completed={result.completed}")
    serialized = result.checkpoint.to_json()
    restored = Checkpoint.from_json(serialized)
    # This line represents an approval supplied by the trusted host/UI.
    result = graph.resume(restored, approval=restored.interrupt.id)
    print(f"RESUMED: {result.status}; completed={result.completed}")
    print(dict(result.state))
