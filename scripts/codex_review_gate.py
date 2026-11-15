"""Fail-closed evaluation of the latest Codex review summary for a PR head."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


CODEX_REVIEW_BOT = "chatgpt-codex-connector[bot]"
REVIEW_STATE_PATTERN = re.compile(r"<!-- codex-review-state:v1\s+(\{.*?\})\s*-->", re.DOTALL)


def evaluate_review_state(head_sha: str, comments: list[dict[str, Any]]) -> dict[str, str]:
    """Return passed, pending, or blocked; only an exact-head completed clean review passes."""
    matching: list[tuple[str, dict[str, Any]]] = []
    for comment in comments:
        if (comment.get("user") or {}).get("login") != CODEX_REVIEW_BOT:
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        marker = REVIEW_STATE_PATTERN.search(body)
        if marker is None:
            continue
        try:
            state = json.loads(marker.group(1))
        except json.JSONDecodeError:
            continue
        code_review = state.get("code_review") if isinstance(state, dict) else None
        if not isinstance(code_review, dict) or code_review.get("commit_sha") != head_sha:
            continue
        timestamp = str(comment.get("updated_at") or comment.get("created_at") or "")
        matching.append((timestamp, code_review))

    if not matching:
        return {"status": "pending", "reason": "No Codex review summary exists for the current PR head."}

    _, latest = max(matching, key=lambda item: item[0])
    if latest.get("status") != "completed":
        return {"status": "pending", "reason": "Codex review for the current PR head is not completed."}
    if latest.get("has_unresolved_findings") is not False:
        return {"status": "blocked", "reason": "Codex review has unresolved or unknown findings."}
    return {"status": "passed", "reason": "Codex review completed for the current PR head with zero unresolved findings."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head-sha", required=True, help="Exact PR head SHA being evaluated")
    args = parser.parse_args(argv)
    try:
        comments = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as error:
        print(json.dumps({"status": "blocked", "reason": f"Cannot read review state: {error}"}))
        return 0
    if not isinstance(comments, list):
        print(json.dumps({"status": "blocked", "reason": "Review comments payload is not a list."}))
        return 0
    print(json.dumps(evaluate_review_state(args.head_sha, comments)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
