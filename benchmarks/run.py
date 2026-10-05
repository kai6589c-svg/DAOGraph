"""Deterministic offline comparison. Policies receive no scorer labels.

Run from any directory: python benchmarks/run.py --split development
Freeze before evaluating: python benchmarks/run.py --freeze
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from research import Research, Source, Task, plain, trace_record  # noqa: E402

ROOT = Path(__file__).resolve().parent


def deny_network():
    """Deny network operations while allowing asyncio's internal socket pair."""
    import threading

    local = threading.local()
    original_socket = socket.socket
    original_pair = socket.socketpair

    def denied(*args, **kwargs):
        raise RuntimeError("Network access is forbidden in the offline benchmark")

    class OfflineSocket(original_socket):
        def connect(self, address):
            if not getattr(local, "internal_pair", False):
                denied()
            return super().connect(address)

        def connect_ex(self, address):
            if not getattr(local, "internal_pair", False):
                denied()
            return super().connect_ex(address)

        def sendto(self, *args):
            denied()

    def internal_pair(*args, **kwargs):
        local.internal_pair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            local.internal_pair = False

    socket.socket = OfflineSocket
    socket.socketpair = internal_pair
    socket.create_connection = denied
    socket.getaddrinfo = denied


def corpus_hashes():
    files = [*sorted((ROOT / "corpus").glob("*.json")), ROOT / "expected.json"]
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    }


def make_application(case, *, fixed_depth=None):
    # Only the read closure sees document contents. Metadata and requirements
    # contain no expected answer, expected abstention, or scorer field.
    reads = []

    def read(source):
        reads.append(source.id)
        record = case["documents"][source.id]
        failure = record.get("failure")
        if failure == "timeout":
            raise TimeoutError("Fixture read timed out")
        if failure == "missing":
            raise FileNotFoundError("Fixture source unavailable")
        if failure == "empty":
            return {"claims": []}
        return record

    return (
        Research(
            Task(**case["task"]),
            tuple(Source(**s) for s in case["sources"]),
            read,
            fixed_depth=fixed_depth,
        ),
        reads,
    )


def run_case(case, mode, fixed_depth=None):
    application, reads = make_application(case, fixed_depth=fixed_depth)
    started = time.perf_counter()
    events = list(application.graph(mode).stream(application.initial_state()))
    result = events[-1].result
    answer = plain(result.state.get("answer", {}))
    return {
        "id": case["id"],
        "family": case["family"],
        "mode": mode,
        "runtime_status": result.status,
        "answer": answer,
        "planner_calls": application.planner_calls,
        "retrieval_attempts": len(reads),
        "duplicate_retrievals": len(reads) - len(set(reads)),
        "elapsed_seconds": time.perf_counter() - started,
        "trace": [trace_record(event) for event in events],
    }


def score(row, case, expected):
    answer = row["answer"]
    values = {key: claim["value"] for key, claim in answer.get("claims", {}).items()}
    # Check references against actual reads, not just corpus membership.
    retrieved = {
        item["observation"]["source_id"]
        for item in row["trace"]
        if "observation" in item and item["observation"]["outcome"] == "ok"
    }
    invalid = 0
    for key, claim in answer.get("claims", {}).items():
        if not claim.get("citations"):
            invalid += 1
        for citation in claim.get("citations", ()):
            source_id = citation["source_id"]
            document = case["documents"].get(source_id, {})
            metadata = next((s for s in case["sources"] if s["id"] == source_id), {})
            if (
                source_id not in retrieved
                or citation["url"] != metadata.get("url")
                or {"key": key, "value": claim["value"], "excerpt": citation["excerpt"]}
                not in document.get("claims", ())
            ):
                invalid += 1
    unsupported = sum(expected["values"].get(key) != value for key, value in values.items())
    correct = (
        row["runtime_status"] == "completed"
        and answer.get("status") == expected["status"]
        and values == expected["values"]
        and invalid == 0
    )
    return {
        **row,
        "correct": correct,
        "correct_supported": correct and expected["status"] == "supported",
        "correct_abstention": correct and expected["status"] == "insufficient",
        "unsupported_assertions": unsupported,
        "invalid_citations": invalid,
    }


def aggregate(rows):
    return {
        key: sum(row[key] for row in rows)
        for key in (
            "correct",
            "correct_supported",
            "correct_abstention",
            "unsupported_assertions",
            "invalid_citations",
            "planner_calls",
            "retrieval_attempts",
            "duplicate_retrievals",
        )
    } | {"cases": len(rows)}


def deterministic(row):
    return {key: value for key, value in row.items() if key != "elapsed_seconds"}


def measure(cases, expected, depth):
    rows = []
    for case in cases:
        for mode in ("fixed", "always", "selective"):
            row = run_case(case, mode, depth if mode == "fixed" else None)
            repeat = run_case(case, mode, depth if mode == "fixed" else None)
            if deterministic(row) != deterministic(repeat):
                raise AssertionError(f"Nondeterministic run: {case['id']} {mode}")
            rows.append(score(row, case, expected[case["id"]]))
    return rows


def choose_depth(cases, expected):
    candidates = {}
    for depth in (2, 3, 4):
        candidates[str(depth)] = aggregate(
            [score(run_case(case, "fixed", depth), case, expected[case["id"]]) for case in cases]
        )
    depth = min(
        (2, 3, 4),
        key=lambda d: (-candidates[str(d)]["correct"], candidates[str(d)]["retrieval_attempts"], d),
    )
    return depth, candidates


def report(rows, split, depth, candidates):
    totals = {
        mode: aggregate([row for row in rows if row["mode"] == mode])
        for mode in ("fixed", "always", "selective")
    }
    selective, always, fixed = (totals[mode] for mode in ("selective", "always", "fixed"))
    reduction = 1 - selective["planner_calls"] / max(1, always["planner_calls"])
    gates = {
        "zero_incorrect_assertions": selective["unsupported_assertions"] == 0,
        "zero_invalid_citations": selective["invalid_citations"] == 0,
        "all_expected_outcomes": selective["correct"] == selective["cases"],
        "quality_matches_always": selective["correct"] == always["correct"],
        "quality_no_worse_than_fixed": selective["correct"] >= fixed["correct"],
        "planner_reduction_at_least_30_percent": reduction >= 0.30,
        "zero_duplicate_requests": selective["duplicate_retrievals"] == 0,
        "no_more_retrieval_than_always": selective["retrieval_attempts"]
        <= always["retrieval_attempts"],
        "repeated_runs_identical": True,
    }
    families = defaultdict(dict)
    for family in sorted({row["family"] for row in rows}):
        for mode in totals:
            families[family][mode] = aggregate(
                [r for r in rows if r["family"] == family and r["mode"] == mode]
            )
    return {
        "schema": 1,
        "split": split,
        "fixed_depth": depth,
        "development_fixed_candidates": candidates,
        "corpus_hashes": corpus_hashes(),
        "totals": totals,
        "families": dict(families),
        "planner_reduction": reduction,
        "gates": gates,
        "rows": rows,
    }


def markdown(data):
    text = [
        "# DAOGraph offline research benchmark",
        "",
        f"Split: {data['split']}. Development-selected fixed depth: {data['fixed_depth']}.",
        "",
        "## 📊 Measured outcomes",
        "",
        "| Mode | Correct outcomes | Planner calls | Retrieval attempts | Invalid citations |",
        "| --- | --- | --- | --- | --- |",
    ]
    for mode, total in data["totals"].items():
        text.append(
            f"| {mode} | {total['correct']}/{total['cases']} | {total['planner_calls']} | "
            f"{total['retrieval_attempts']} | {total['invalid_citations']} |"
        )
    text += [
        "",
        f"Measured planner-call reduction: {data['planner_reduction']:.1%}.",
        "",
        "## ✅ Release gates",
        "",
    ]
    text += [f"- {name}: {'PASS' if passed else 'FAIL'}" for name, passed in data["gates"].items()]
    text += [
        "",
        "## 🔎 Scope and limitations",
        "",
        "These project-authored structured scenarios test control behavior. They do not",
        "measure arbitrary language entailment, model token cost, web-search quality, or",
        "production workloads. No model is called. Planner-call reduction is not a measured",
        "monetary saving. JSON contains every case, family, trace, fixed-depth candidate,",
        "and integrity hash; elapsed time is informational. Scorer labels never enter the policy.",
    ]
    return "\n".join(text) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("development", "evaluation"), default="evaluation")
    parser.add_argument(
        "--freeze", action="store_true", help="Lock corpus and development baseline"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "results")
    args = parser.parse_args()
    deny_network()
    cases = [json.loads(path.read_text()) for path in sorted((ROOT / "corpus").glob("*.json"))]
    expected = json.loads((ROOT / "expected.json").read_text())
    development = [case for case in cases if case["split"] == "development"]
    lock_path = ROOT / "lock.json"
    if args.freeze:
        if lock_path.exists():
            raise SystemExit(
                "Corpus already frozen; use a new corpus version rather than relocking"
            )
        depth, candidates = choose_depth(development, expected)
        lock_path.write_text(
            json.dumps(
                {
                    "hashes": corpus_hashes(),
                    "fixed_depth": depth,
                    "development_fixed_candidates": candidates,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        print(f"Frozen corpus and fixed depth {depth}; evaluation not executed")
        return
    if args.split == "evaluation":
        lock = json.loads(lock_path.read_text())
        if lock["hashes"] != corpus_hashes():
            raise SystemExit("Frozen corpus integrity check failed")
        depth, candidates = lock["fixed_depth"], lock["development_fixed_candidates"]
    else:
        depth, candidates = choose_depth(development, expected)
    selected = [case for case in cases if case["split"] == args.split]
    data = report(measure(selected, expected, depth), args.split, depth, candidates)
    # Keep all fixed-depth evaluation candidates visible without retuning the chosen baseline.
    data["fixed_candidates_on_selected_split"] = {
        str(d): aggregate(
            [score(run_case(case, "fixed", d), case, expected[case["id"]]) for case in selected]
        )
        for d in (2, 3, 4)
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / f"{args.split}.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )
    (args.output / f"{args.split}.md").write_text(markdown(data))
    print(markdown(data))
    if not all(data["gates"].values()):
        raise SystemExit("Research release gates failed")


if __name__ == "__main__":
    main()
