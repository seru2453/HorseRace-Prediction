import json

from scripts.codex_review_gate import CODEX_REVIEW_BOT, evaluate_review_state


def _comment(head_sha, status="completed", unresolved=False, timestamp="2026-11-15T00:00:00Z", login=None):
    state = {
        "code_review": {
            "commit_sha": head_sha,
            "has_any_findings": unresolved,
            "has_unresolved_findings": unresolved,
            "status": status,
        }
    }
    return {
        "user": {"login": login or CODEX_REVIEW_BOT},
        "created_at": timestamp,
        "body": f"<!-- codex-review-state:v1 {json.dumps(state)} -->",
    }


def test_review_gate_passes_only_for_clean_review_of_exact_head():
    result = evaluate_review_state("abc123", [_comment("old-sha"), _comment("abc123")])

    assert result == {
        "status": "passed",
        "reason": "Codex review completed for the current PR head with zero unresolved findings.",
    }


def test_review_gate_waits_when_summary_is_missing_stale_or_incomplete():
    assert evaluate_review_state("abc123", []) ["status"] == "pending"
    assert evaluate_review_state("abc123", [_comment("old-sha")])["status"] == "pending"
    assert evaluate_review_state("abc123", [_comment("abc123", status="in_progress")])["status"] == "pending"
    assert evaluate_review_state("abc123", [_comment("abc123", login="untrusted-user")])["status"] == "pending"


def test_review_gate_blocks_unresolved_unknown_and_newer_findings():
    assert evaluate_review_state("abc123", [_comment("abc123", unresolved=True)])["status"] == "blocked"

    unknown = _comment("abc123")
    state_start = unknown["body"].index("<!-- codex-review-state:v1 ")
    state = json.loads(unknown["body"][state_start + len("<!-- codex-review-state:v1 "):-4])
    state["code_review"]["has_unresolved_findings"] = None
    unknown["body"] = f"<!-- codex-review-state:v1 {json.dumps(state)} -->"
    assert evaluate_review_state("abc123", [unknown])["status"] == "blocked"

    earlier_clean = _comment("abc123", timestamp="2026-11-15T00:00:00Z")
    newer_unresolved = _comment("abc123", unresolved=True, timestamp="2026-11-15T00:01:00Z")
    assert evaluate_review_state("abc123", [earlier_clean, newer_unresolved])["status"] == "blocked"


def test_review_gate_does_not_fall_back_to_an_older_clean_summary():
    earlier_clean = _comment("abc123", timestamp="2026-11-15T00:00:00Z")
    newer_malformed = {
        "user": {"login": CODEX_REVIEW_BOT},
        "created_at": "2026-11-15T00:01:00Z",
        "body": "<!-- codex-pull-request-review-summary --><!-- codex-review-state:v1 {invalid} -->",
    }
    newer_stale = _comment("old-sha", timestamp="2026-11-15T00:02:00Z")

    assert evaluate_review_state("abc123", [earlier_clean, newer_malformed])["status"] == "blocked"
    assert evaluate_review_state("abc123", [earlier_clean, newer_stale])["status"] == "pending"
