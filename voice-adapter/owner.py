"""Who the voice works for, as the prompts the models read name them.

Prompts are written with "the owner" ("The owner" at a sentence start). Set Z0LIVE_OWNER_NAME (e.g. a first
name) and fill() swaps that name in, so the voice can address its owner by name; unset, the prompts stay neutral.
LABEL is the owner's speaker label in call transcripts handed to a model ("User: ..." by default).
Modules loaded by file path from outside this directory (jobs, ask_bot, ledger) say "the owner" without this.
"""
from __future__ import annotations

import os
import re

NAME = os.environ.get("Z0LIVE_OWNER_NAME", "").strip()
LABEL = NAME or "User"


def fill(text: str) -> str:
    """text with "the owner"/"The owner" replaced by Z0LIVE_OWNER_NAME when it is set."""
    return re.sub(r"\b[Tt]he owner\b", lambda _: NAME, text) if NAME else text
