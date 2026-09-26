from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PredictionEvidence:
    source_type: str
    source_name: str
    race_id: str
    horse_id: str
    raw_text: str
    normalized_text: str
    content_hash: str
    source_url: str | None = None
    published_at: datetime | None = None
    author: str | None = None
    confidence: float | None = None


class QualitativeEvidenceCollector:
    """Normalizes caller-supplied evidence; it deliberately performs no crawling."""

    def normalize(
        self,
        *,
        source_type: str,
        source_name: str,
        race_id: str,
        horse_id: str,
        raw_text: str,
        source_url: str | None = None,
        published_at: datetime | None = None,
        author: str | None = None,
        confidence: float | None = None,
    ) -> PredictionEvidence:
        normalized = re.sub(r"\s+", " ", raw_text).strip()
        if not normalized:
            raise ValueError("Evidence text is empty")
        digest_input = "\n".join((source_type, source_name, source_url or "", normalized))
        content_hash = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        bounded_confidence = None if confidence is None else max(0.0, min(1.0, float(confidence)))
        return PredictionEvidence(
            source_type=source_type,
            source_name=source_name,
            source_url=source_url,
            published_at=published_at,
            author=author,
            race_id=race_id,
            horse_id=horse_id,
            raw_text=raw_text,
            normalized_text=normalized,
            confidence=bounded_confidence,
            content_hash=content_hash,
        )
