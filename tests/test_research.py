import json
import socket
import subprocess
import sys
from dataclasses import asdict

import pytest
from online_research import MAX_BYTES, QUESTION, Reader, default_sources, extract_approval
from research import Research, Source, Task, plain, trace_record
from run import ROOT, corpus_hashes, make_application, run_case, score


def case(name):
    return json.loads((ROOT / "corpus" / f"{name}.json").read_text())


def test_frozen_integrity_and_balanced_splits():
    lock = json.loads((ROOT / "lock.json").read_text())
    assert corpus_hashes() == lock["hashes"]
    cases = [json.loads(path.read_text()) for path in (ROOT / "corpus").glob("*.json")]
    assert len(cases) == 48
    assert sum(c["split"] == "evaluation" for c in cases) == 36
    for family in {c["family"] for c in cases}:
        assert sum(c["family"] == family and c["split"] == "development" for c in cases) == 2


def test_no_scorer_labels_or_unretrieved_contents_in_policy_inputs():
    fixture = case("conflict-01")
    fixture["expected"] = {"secret_label": "Never expose this"}
    fixture["documents"]["s03"]["secret_label"] = "Only retrieved contents are visible"
    application, reads = make_application(fixture)
    assert not hasattr(application, "expected")
    assert "secret_label" not in repr(application.initial_state())
    assert "secret_label" not in repr(application.task)
    assert "secret_label" not in repr(application.sources)
    events = list(application.graph().stream(application.initial_state()))
    first = next(event for event in events if event.kind == "node_start")
    assert "secret_label" not in repr(first.context)
    assert reads == ["s01", "s02", "s03"]
    # Extraction accepts only claim records; extra document metadata is not copied into state.
    assert "secret_label" not in repr(events[-1].result.state)


@pytest.mark.parametrize(
    "family", ["consistent", "conflict", "stale", "duplicates", "unavailable", "insufficient"]
)
def test_development_outcomes_and_bounded_execution(family):
    fixture = case(f"{family}-01")
    expected = json.loads((ROOT / "expected.json").read_text())[fixture["id"]]
    application, reads = make_application(fixture)
    events = list(application.graph().stream(application.initial_state()))
    result = events[-1].result
    assert result.status == "completed"
    assert len(result.history) <= 8
    assert len(reads) <= 4 and len(reads) == len(set(reads))
    row = score(run_case(fixture, "selective"), fixture, expected)
    assert row["correct"]
    assert row["unsupported_assertions"] == row["invalid_citations"] == 0
    if family == "conflict":
        assert any(e.plan_change and "review" in e.plan_change.added for e in events)
        assert result.state["conflict_review"]["release_limit"] == ("20", "30")
    if family == "insufficient":
        assert result.state["answer"]["unresolved"] == ("release_limit",)
        assert result.goal_check.satisfied
    if family == "unavailable":
        assert [attempt["outcome"] for attempt in result.state["attempts"]] == [
            "TimeoutError",
            "empty",
            "ok",
        ]


def test_duplicate_observation_retains_plan_and_copies_do_not_count_as_independent():
    fixture = case("duplicates-02")  # Copy provenance is known; copy relationship is discovered.
    application, reads = make_application(fixture)
    events = list(application.graph().stream(application.initial_state()))
    duplicate_end = next(
        i for i, e in enumerate(events) if e.kind == "node_end" and e.step.id == "retrieve:1"
    )
    assert any(e.kind == "plan_reused" for e in events[duplicate_end + 1 : duplicate_end + 5])
    second_observation = events[duplicate_end].context.state
    assert application.resolution(second_observation)["resolved"] == {}
    assert reads == ["s01", "s02", "s03"]
    known, known_reads = make_application(case("duplicates-01"))
    assert known.graph().invoke(known.initial_state()).status == "completed"
    assert known_reads == ["s01", "s03"]


def test_peer_authorities_disagree_and_partial_coverage_is_not_completion():
    task = Task("Investigate limits and regions", ("limit", "region"), "2026-01-01")
    sources = tuple(
        Source(str(i), str(i), f"fixture://{i}", task.question, "2026-10-01", True)
        for i in range(2)
    )
    application = Research(
        task,
        sources,
        lambda s: {"claims": [{"key": "limit", "value": s.id, "excerpt": f"Limit {s.id}"}]},
    )
    result = application.graph().invoke(application.initial_state())
    assert result.state["answer"]["status"] == "insufficient"
    assert set(result.state["answer"]["unresolved"]) == {"limit", "region"}


@pytest.mark.parametrize(
    "read,expected",
    [
        (lambda s: None, "completed"),
        (lambda s: {"claims": []}, "completed"),
        (lambda s: (_ for _ in ()).throw(FileNotFoundError("Missing")), "completed"),
        (lambda s: (_ for _ in ()).throw(ValueError("Programming error")), "failed"),
        (lambda s: {}, "failed"),
        (lambda s: {"claims": [None]}, "failed"),
        (lambda s: {"claims": "invalid"}, "failed"),
    ],
)
def test_read_failures_are_observations_and_programming_errors_fail(read, expected):
    application = Research(
        Task("Question", ("value",)),
        (Source("one", "one", "fixture://one", "Question", "2026-10-01"),),
        read,
    )
    assert application.graph().invoke(application.initial_state()).status == expected


def test_unjustified_abstention_and_fabricated_answer_are_not_verified():
    application, _ = make_application(case("consistent-01"))
    state = application.initial_state()
    state.update(
        answer={"status": "insufficient", "claims": {}, "unresolved": ["release_limit"]},
        citations_valid=True,
    )
    from daograph import Context, Situation

    assert not application.verify_goal(Context("Q", state, Situation())).satisfied
    state["answer"]["claims"] = {"release_limit": {"value": "fabricated", "citations": []}}
    assert not application.verify_goal(Context("Q", state, Situation())).satisfied


def test_scorer_rejects_unread_citations_and_wrong_supported_answers():
    fixture = case("consistent-01")
    expected = json.loads((ROOT / "expected.json").read_text())[fixture["id"]]
    row = run_case(fixture, "selective")
    row["answer"]["claims"]["release_limit"]["value"] = "invented"
    scored = score(row, fixture, expected)
    assert not scored["correct"] and scored["unsupported_assertions"] == 1
    assert scored["invalid_citations"] > 0
    row = run_case(fixture, "selective")
    row["trace"] = []
    assert score(row, fixture, expected)["invalid_citations"] > 0


def test_network_guard_denies_connections_and_keeps_asyncio_functional():
    script = """from run import deny_network
import socket
from daograph import DAOGraph, Plan
deny_network()
for call in (lambda: socket.create_connection(("example.com", 443)),
             lambda: socket.getaddrinfo("example.com", 443),
             lambda: socket.socket().connect(("127.0.0.1", 9))):
    try:
        call()
    except RuntimeError:
        pass
    else:
        raise AssertionError("Network call allowed")
assert DAOGraph("Offline", (), lambda ctx: Plan()).invoke({}).status == "completed"
"""
    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True)


def test_online_extractor_and_verified_offline_replay(tmp_path, monkeypatch):
    source = default_sources("v0.1.0")[0]
    body = b"An approval id is not\nan authentication token."
    import hashlib

    (tmp_path / f"{source.id}.bin").write_bytes(body)
    (tmp_path / f"{source.id}.json").write_text(
        json.dumps(
            {"source": asdict(source), "ref": "v0.1.0", "sha256": hashlib.sha256(body).hexdigest()}
        )
    )
    monkeypatch.setattr(
        socket, "create_connection", lambda *args: pytest.fail("Replay went online")
    )
    reader = Reader((source,), "v0.1.0", replay=tmp_path)
    application = Research(Task(QUESTION, ("approval_authentication",)), (source,), reader)
    result = application.graph().invoke(application.initial_state())
    assert result.state["answer"]["claims"]["approval_authentication"]["value"] == "no"
    assert (
        extract_approval("An approval id is an authentication token.")["claims"][0]["value"]
        == "yes"
    )
    assert extract_approval("Unknown phrasing") == {"claims": []}
    (tmp_path / f"{source.id}.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        reader(source)


def test_online_read_size_utf8_redirect_and_source_list_guards(monkeypatch):
    from online_research import AllowedRedirects

    source = default_sources("v0.1.0")[0]
    reader = Reader((source,), "v0.1.0")

    class Response:
        url = source.url
        body = b"x" * (MAX_BYTES + 1)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            assert size == MAX_BYTES + 1
            return self.body

    monkeypatch.setattr(reader.opener, "open", lambda *args, **kwargs: Response())
    with pytest.raises(OSError, match="one-MiB"):
        reader(source)
    Response.body = b"\xff"
    with pytest.raises(OSError, match="UTF-8"):
        reader(source)
    with pytest.raises(OSError, match="Redirect"):
        AllowedRedirects({"raw.githubusercontent.com"}).redirect_request(
            None, None, 302, "", {}, "http://unapproved.example/file"
        )
    with pytest.raises(ValueError, match="configured list"):
        reader(default_sources("other")[0])
    with pytest.raises(ValueError):
        default_sources("../main")


def test_trace_is_serializable_and_contains_cause_and_plan_change():
    application, _ = make_application(case("conflict-01"))
    records = [trace_record(e) for e in application.graph().stream(application.initial_state())]
    json.dumps(records)
    assert any("observation" in record for record in records)
    assert any("review" in record.get("plan_change", {}).get("added", ()) for record in records)
    assert records[-1]["status"] == "completed"
    assert records[-1]["answer"] == plain(records[-1]["answer"])


def test_application_configuration_and_host_provenance_validation():
    with pytest.raises(ValueError):
        Task("Question", "value")
    with pytest.raises(ValueError):
        Task("Question", ("key",), "not-a-date")
    with pytest.raises(ValueError):
        Source("../unsafe", "owner", "https://example.com", "Title", "2026-10-01")
    with pytest.raises(ValueError):
        Source("safe", "owner", "https://example.com", "Title", "2026-10-01", 1)
    app, _ = make_application(case("consistent-01"))
    with pytest.raises(ValueError):
        Research(app.task, app.sources, app.read, budget=True)
    with pytest.raises(ValueError):
        Research(app.task, app.sources + (app.sources[0],), app.read)


def test_revision_identity_resolution_without_metadata_for_a_commit():
    from online_research import resolve_revision

    sha = "a" * 40
    assert resolve_revision(sha) == sha
