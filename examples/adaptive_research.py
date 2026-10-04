"""A new observation inserts verification into the unfinished execution graph."""

from daograph import Context, DAOGraph, Node, Plan, Situation, SituationEngine


def assess(state, previous):
    uncertain = bool(state.get("conflicting_sources")) and not state.get("verified")
    return Situation(
        risk=0.1,
        uncertainty=0.85 if uncertain else 0.15,
        trust=0.4 if uncertain else 0.9,
        evidence=("Sources disagree" if uncertain else "Evidence is consistent",),
    )


def research(context: Context):
    # Replace these deterministic observations with your search tool.
    return {"sources": ["Source A: 12", "Source B: 21"], "conflicting_sources": True}


def verify(context: Context):
    return {"verified": True, "resolved_value": 12}


def answer(context: Context):
    return {"answer": f"Verified value: {context.state.get('resolved_value', 'unknown')}"}


def plan(context: Context):
    if context.state.get("answer"):
        return Plan(reason="Answer delivered")
    if context.situation.uncertainty >= 0.6:
        return Plan.chain("research", "verify", "answer", reason="Insert verification")
    if context.state.get("verified"):
        # Preserve the completed answer's future dependency when rebuilding.
        return Plan.chain("research", "verify", "answer", reason="Evidence resolved")
    return Plan.chain("research", "answer", reason="Start with the short path")


def build_graph():
    return DAOGraph(
        goal="Answer the question using consistent evidence",
        nodes=(Node("research", research), Node("verify", verify), Node("answer", answer)),
        planner=plan,
        situation=SituationEngine(assess),
    )


if __name__ == "__main__":
    for event in build_graph().stream({"question": "Which value is correct?"}):
        if event.kind == "plan":
            print(f"PLAN: {[step.id for step in event.plan.steps]} — {event.plan.reason}")
        if event.kind == "node_end":
            print(f"EXECUTED: {event.step.id}")
        if event.result:
            print(f"RESULT: {event.result.status}; {dict(event.result.state)}")
