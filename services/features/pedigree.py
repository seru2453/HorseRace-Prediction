"""Conservative pedigree name normalization; unresolved ancestors remain unknown."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


LINEAGE_MAPPING_VERSION = "explicit-aliases-v1"
_LINEAGE_ALIASES = {
    "sundaysilence": "SUNDAY_SILENCE",
    "サンデーサイレンス": "SUNDAY_SILENCE",
    "deepimpact": "SUNDAY_SILENCE",
    "ディープインパクト": "SUNDAY_SILENCE",
    "mrprospector": "MR_PROSPECTOR",
    "ミスタープロスペクター": "MR_PROSPECTOR",
    "northerndancer": "NORTHERN_DANCER",
    "ノーザンダンサー": "NORTHERN_DANCER",
    "roberto": "ROBERTO",
    "ロベルト": "ROBERTO",
}


def normalize_ancestor_name(value: object) -> str | None:
    if value in (None, ""):
        return None
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[\s　・･.（）()]+", "", normalized) or None


def lineage_family(value: object) -> str | None:
    normalized = normalize_ancestor_name(value)
    if normalized is None:
        return None
    return _LINEAGE_ALIASES.get(normalized)


def pedigree_metadata(horse: dict[str, Any]) -> dict[str, Any]:
    ancestors = {
        key: _value(horse.get(key))
        for key in ("sire", "dam", "damsire", "sire_sire", "sire_dam", "dam_dam")
    }
    known = [value for value in ancestors.values() if value]
    depth = 0 if not known else 1 if len(known) <= 2 else 2
    sire_line = lineage_family(ancestors["sire"])
    damsire_line = lineage_family(ancestors["damsire"])
    resolved = int(sire_line is not None) + int(damsire_line is not None)
    return {
        "pedigree_depth_available": depth,
        "pedigree_ancestor_count": len(known),
        "pedigree_sire_line": sire_line,
        "pedigree_damsire_line": damsire_line,
        "lineage_mapping_version": LINEAGE_MAPPING_VERSION,
        "lineage_resolution_status": "RESOLVED" if resolved == 2 else "PARTIAL" if resolved else "UNKNOWN",
    }


def _value(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    normalized = normalize_ancestor_name(value)
    return str(value).strip() if normalized else None
