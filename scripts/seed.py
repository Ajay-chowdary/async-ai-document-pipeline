#!/usr/bin/env python3
"""Upload the four synthetic sample documents and print their job IDs.

Requires a running API (``make api``) and, for completion, a worker
(``make worker``). Uses the fake provider when the API is configured that way.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "sample_documents"

DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("invoice.txt", "invoice"),
    ("resume.txt", "resume"),
    ("support_ticket.txt", "support_ticket"),
    ("business_memo.txt", "generic"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="API base URL (default: http://127.0.0.1:8000)",
    )
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        health = client.get("/health")
        health.raise_for_status()

        print(f"seeding against {args.base_url}")
        for filename, document_type in DOCUMENTS:
            path = SAMPLES / filename
            if not path.is_file():
                print(f"missing sample: {path}", file=sys.stderr)
                return 1
            with path.open("rb") as handle:
                response = client.post(
                    "/api/v1/documents",
                    files={"file": (filename, handle, "text/plain")},
                    data={"document_type": document_type},
                )
            if response.status_code != 202:
                print(
                    f"upload failed for {filename}: {response.status_code} {response.text}",
                    file=sys.stderr,
                )
                return 1
            body = response.json()
            print(
                f"{filename:20} job_id={body['job_id']} "
                f"document_id={body['document_id']} status={body['status']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
