from __future__ import annotations

import os


def deployment_mode() -> str:
    mode = os.getenv("DEPLOYMENT_MODE", "local").strip().lower()
    if mode not in {"local", "public"}:
        raise ValueError("DEPLOYMENT_MODE must be 'local' or 'public'.")
    return mode


def is_public_deployment() -> bool:
    return deployment_mode() == "public"
