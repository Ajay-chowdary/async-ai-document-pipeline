#!/usr/bin/env python3
"""Measure end-to-end upload-to-terminal latency against a running stack.

Uploads N sample documents, polls until each job is terminal, then reports
submitted / completed / failed counts, mean / p50 / p95 latency and throughput
per minute. Numbers are only meaningful for the machine and configuration you
ran against — do not copy them into the README without stating that context.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = sorted((ROOT / "sample_documents").glob("*.txt"))


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile for a non-empty list."""
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10, help="Number of uploads")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    if args.count < 1:
        print("--count must be >= 1", file=sys.stderr)
        return 2
    if not SAMPLES:
        print(f"no sample documents in {ROOT / 'sample_documents'}", file=sys.stderr)
        return 1

    latencies_ms: list[float] = []
    completed = 0
    failed = 0
    submitted = 0

    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        client.get("/health").raise_for_status()
        started = time.perf_counter()

        jobs: list[tuple[str, float]] = []
        for index in range(args.count):
            sample = SAMPLES[index % len(SAMPLES)]
            with sample.open("rb") as handle:
                response = client.post(
                    "/api/v1/documents",
                    files={"file": (sample.name, handle, "text/plain")},
                )
            if response.status_code != 202:
                print(
                    f"upload failed: {response.status_code} {response.text}",
                    file=sys.stderr,
                )
                return 1
            body = response.json()
            jobs.append((body["job_id"], time.perf_counter()))
            submitted += 1

        pending = dict(jobs)
        deadline = time.perf_counter() + args.timeout
        while pending and time.perf_counter() < deadline:
            for job_id in list(pending):
                response = client.get(f"/api/v1/jobs/{job_id}")
                response.raise_for_status()
                body = response.json()
                if not body.get("is_terminal"):
                    continue
                elapsed_ms = (time.perf_counter() - pending.pop(job_id)) * 1000
                latencies_ms.append(elapsed_ms)
                if body["status"] == "completed":
                    completed += 1
                else:
                    failed += 1
            if pending:
                time.sleep(args.poll_interval)

        wall_seconds = time.perf_counter() - started

    timed_out = len(pending)
    print(f"base_url={args.base_url}")
    print(f"submitted={submitted}")
    print(f"completed={completed}")
    print(f"failed={failed}")
    print(f"timed_out={timed_out}")
    print(f"wall_seconds={wall_seconds:.3f}")
    if latencies_ms:
        mean = statistics.fmean(latencies_ms)
        print(f"mean_ms={mean:.2f}")
        print(f"p50_ms={percentile(latencies_ms, 50):.2f}")
        print(f"p95_ms={percentile(latencies_ms, 95):.2f}")
        throughput = (completed + failed) / wall_seconds * 60.0
        print(f"throughput_per_minute={throughput:.2f}")
    else:
        print("mean_ms=n/a")
        print("p50_ms=n/a")
        print("p95_ms=n/a")
        print("throughput_per_minute=0.00")

    return 0 if timed_out == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
