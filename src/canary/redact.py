"""Credential redaction at every persistence boundary.

Journals, bundles, manifests, and error records pass through here before
touching disk. Best-effort tripwire for known-prefix secrets: it catches
accidental echoes, not adversarial exfiltration.
"""
from __future__ import annotations

import re

PATTERNS = {
    "muse_key": re.compile(rb"\b(MUSE|MODEL|META)_API_KEY\b\s*[:=]\s*\S*"),
    "github_token": re.compile(rb"\bGITHUB_TOKEN\b\s*[:=]\s*\S*|gh[opsu]_[A-Za-z0-9_]+"),
    "aws_key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "private_key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "canary_gate_url": re.compile(rb"canary-gate:\d+"),
}

TEXT_PATTERNS = {name: re.compile(rx.pattern.decode()) for name, rx in PATTERNS.items()}


def redact_text(text: str) -> str:
    for name, rx in TEXT_PATTERNS.items():
        text = rx.sub(f"[REDACTED:{name}]", text)
    return text


def redact_bytes(data: bytes) -> bytes:
    for name, rx in PATTERNS.items():
        data = rx.sub(f"[REDACTED:{name}]".encode(), data)
    return data


def find_secrets(data: bytes) -> list[str]:
    return [name for name, rx in PATTERNS.items() if rx.search(data)]
