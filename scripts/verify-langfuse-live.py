#!/usr/bin/env python3
"""Check AGR's Langfuse connector against a real server: version detection, project, listing, search and fetch.

Usage (keys come from the environment, never the command line):

    LANGFUSE_HOST=http://localhost:3000 LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \\
        python scripts/verify-langfuse-live.py [--days 7] [--limit 20]

It only reads. It prints what the connector found and which of Langfuse's optional fields came back, so a gap
(for example a server that does not return cost) is visible rather than silent. Exit status is 1 if listing or
fetching failed.

To get a server to test against, self-host Langfuse with the docker compose file in its repository
(https://github.com/langfuse/langfuse). A self-hosted 4.x server only serves observations v2 when
LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN=true is set; without it the connector uses the v1 trace endpoints.
"""
import argparse
from datetime import datetime, timedelta, timezone
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from agr import langfuse_source  # noqa: E402
from agr.imports import ImportProblem  # noqa: E402
from agr.ingest_langfuse import LANGFUSE_ADAPTER  # noqa: E402

FIELDS = ("name", "user_id", "session_id", "environment", "tags", "release", "version", "latency_s", "cost", "input_preview", "output_preview", "metadata")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=7, help="how far back to list (default 7)")
    parser.add_argument("--limit", type=int, default=20, help="traces to list (default 20)")
    args = parser.parse_args()
    host, public, secret = os.environ.get("LANGFUSE_HOST"), os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not (host and public and secret):
        print("Set LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.", file=sys.stderr)
        return 2
    keys = dict(host=host, public_key=public, secret_key=secret)
    now = datetime.now(timezone.utc)
    window = dict(from_time=(now - timedelta(days=args.days)).isoformat(), to_time=(now + timedelta(hours=1)).isoformat())

    info = langfuse_source.detect(host, refresh=True)
    print(f"server version : {info['server_version'] or 'not reported'}")
    print(f"project        : {langfuse_source.project(host, public, secret)}")
    try:
        listed = langfuse_source.list_traces(limit=args.limit, **keys, **window)
    except ImportProblem as exc:
        print(f"listing failed : {exc.code}: {exc}", file=sys.stderr)
        return 1
    info = langfuse_source.detect(host)
    print(f"API in use     : {langfuse_source.LABELS.get(info['api'], 'unknown')}")
    traces = listed["traces"]
    print(f"listed         : {len(traces)} traces (truncated={listed['truncated']}, scan_capped={listed['scan_capped']}, scanned={listed['scanned']})")
    if not traces:
        print("No traces in that range; send some, or widen --days.")
        return 0
    print("fields returned (traces that carry each one):")
    for field in FIELDS:
        count = sum(1 for t in traces if t.get(field) not in (None, "", [], {}))
        print(f"  {field:14} {count}/{len(traces)}")
    first = traces[0]
    print(f"first trace    : {first['trace_id']}  {first['name']!r}  {first['started_at']}")
    try:
        by_id = langfuse_source.list_traces(limit=5, trace_id=first["trace_id"][:12], **keys, **window)
        print(f"search by ID   : {len(by_id['traces'])} trace(s) for the first 12 characters")
        fetched = langfuse_source.fetch_trace(first["trace_id"], **keys, **window)
        doc = LANGFUSE_ADAPTER.convert(fetched).doc
        print(f"fetch + convert: ok, {len(doc['steps'])} steps, capture {doc.get('capture_completeness')}")
    except ImportProblem as exc:
        print(f"fetch failed   : {exc.code}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
