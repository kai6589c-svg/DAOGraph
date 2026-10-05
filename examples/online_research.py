"""Optional HTTPS demonstration with pinned sources and captured-response replay.

python examples/online_research.py --online --capture /tmp/daograph-replay
python examples/online_research.py --replay /tmp/daograph-replay

The default extractor recognizes one explicit approval-authentication statement.
For other questions, supply a claim extractor to Reader in your own application.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from research import Research, Source, Task, plain, write_trace

MAX_BYTES = 1024 * 1024
QUESTION = "Does an approval id authenticate the approver?"


def extract_approval(text):
    normalized = " ".join(text.split())
    match = re.search(r"An approval id is (not )?an authentication token\.", normalized)
    if not match:
        return {"claims": []}
    return {
        "claims": [
            {
                "key": "approval_authentication",
                "value": "no" if match[1] else "yes",
                "excerpt": match[0],
            }
        ]
    }


class AllowedRedirects(HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in self.hosts:
            raise OSError("Redirect leaves the configured HTTPS source hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Reader:
    """Read one bounded UTF-8 source; local replay verifies bytes and provenance."""

    def __init__(self, sources, ref, *, capture=None, replay=None, extractor=extract_approval):
        if capture is not None and replay is not None:
            raise ValueError("Choose capture or replay")
        self.sources = {source.id: source for source in sources}
        self.ref = ref
        self.capture = capture
        self.replay = replay
        self.extractor = extractor
        self.hosts = {urlparse(source.url).hostname for source in sources}
        self.opener = build_opener(AllowedRedirects(self.hosts))

    def __call__(self, source):
        if self.sources.get(source.id) != source:
            raise ValueError("Source is outside the configured list")
        parsed = urlparse(source.url)
        if parsed.scheme != "https" or parsed.hostname not in self.hosts:
            raise ValueError("Sources must use configured HTTPS hosts")
        if self.replay is not None:
            metadata = json.loads((self.replay / f"{source.id}.json").read_text())
            body = (self.replay / f"{source.id}.bin").read_bytes()
            if (
                metadata["sha256"] != hashlib.sha256(body).hexdigest()
                or metadata["source"] != asdict(source)
                or metadata["ref"] != self.ref
            ):
                raise ValueError("Replay integrity or source revision mismatch")
        else:
            request = Request(source.url, headers={"User-Agent": "DAOGraph/0.2 research-demo"})
            with self.opener.open(request, timeout=10) as response:
                body = response.read(MAX_BYTES + 1)
                if urlparse(response.url).hostname not in self.hosts:
                    raise OSError("Response leaves the configured source hosts")
            if self.capture is not None:
                self.capture.mkdir(parents=True, exist_ok=True)
                (self.capture / f"{source.id}.bin").write_bytes(body)
                metadata = {
                    "source": asdict(source),
                    "ref": self.ref,
                    "size": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
                (self.capture / f"{source.id}.json").write_text(
                    json.dumps(metadata, indent=2, sort_keys=True) + "\n"
                )
        if len(body) > MAX_BYTES:
            raise OSError("Source exceeds the one-MiB response limit")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise OSError("Source is not UTF-8 text") from error
        return self.extractor(text)


def resolve_revision(ref):
    """Resolve source identity before the run; this is one host metadata request."""
    default_sources(ref)  # Validate the revision before constructing a URL.
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return ref.lower()
    url = "https://api.github.com/repos/kai6589c-svg/DAOGraph/commits/" + quote(ref, safe="")
    opener = build_opener(AllowedRedirects({"api.github.com"}))
    request = Request(url, headers={"User-Agent": "DAOGraph/0.2 research-demo"})
    with opener.open(request, timeout=10) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise OSError("Revision metadata exceeds the response limit")
    sha = json.loads(body)["sha"]
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Revision metadata must contain a commit SHA")
    return sha


def default_sources(ref):
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", ref) or ".." in ref:
        raise ValueError("Use a Git revision with English letters, digits, _, ., /, or -")
    base = f"https://raw.githubusercontent.com/kai6589c-svg/DAOGraph/{quote(ref, safe='/')}"
    # Dates are unknown for this non-temporal question; no freshness claim is made.
    return (
        Source("api", "DAOGraph", f"{base}/docs/api.md", QUESTION, "1970-01-01", True),
        Source("readme", "DAOGraph", f"{base}/README.md", QUESTION, "1970-01-01", True),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--online", action="store_true", help="Opt into real HTTPS reads")
    mode.add_argument("--replay", type=Path, help="Read captured responses without network calls")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--ref", default="v0.1.0", help="Pinned source Git revision")
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    if args.replay:
        manifest = json.loads((args.replay / "run.json").read_text())
        if manifest["requested_ref"] != args.ref:
            raise ValueError("Replay requested revision mismatch; supply the original --ref")
        resolved_ref = manifest["resolved_ref"]
    else:
        resolved_ref = resolve_revision(args.ref)
    sources = default_sources(resolved_ref)
    if args.capture:
        args.capture.mkdir(parents=True, exist_ok=True)
        (args.capture / "run.json").write_text(
            json.dumps(
                {
                    "requested_ref": args.ref,
                    "resolved_ref": resolved_ref,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    reader = Reader(sources, resolved_ref, capture=args.capture, replay=args.replay)
    application = Research(Task(QUESTION, ("approval_authentication",)), sources, reader)
    if args.replay:
        # The same process guard used by the benchmark catches accidental socket reads.
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
        from run import deny_network

        deny_network()
    events = list(application.graph().stream(application.initial_state()))
    for event in events:
        if event.kind in ("plan", "plan_reused"):
            print(f"{event.kind}: {event.message}")
    result = events[-1].result
    print(
        json.dumps(
            {
                "status": result.status,
                "answer": plain(result.state.get("answer")),
                "attempts": plain(result.state["attempts"]),
                "source_revision": resolved_ref,
                "reason": result.reason,
            },
            indent=2,
        )
    )
    if args.trace:
        write_trace(events, args.trace)
    if result.status != "completed":
        raise SystemExit("Research demonstration did not complete")


if __name__ == "__main__":
    main()
