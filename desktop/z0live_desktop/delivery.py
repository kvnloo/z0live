"""Offline batching via the supported Hermes send CLI; no Telegram token handling."""

import json
import logging
import os
from pathlib import Path
import subprocess
import time

LOG = logging.getLogger("z0live.delivery")


class Delivery:
    def __init__(self, store):
        self.store = store
        self.next_check = 0
        with store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO meta VALUES('telegram_cursor',?)",
                (
                    str(
                        db.execute("SELECT coalesce(max(id),0) FROM inbox").fetchone()[
                            0
                        ]
                    ),
                ),
            )

    def tick(self):
        target = os.environ.get("Z0LIVE_TELEGRAM_TARGET", "")
        if not target or time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + 60
        with self.store.connect() as db:
            if db.execute(
                "SELECT 1 FROM sessions WHERE ended=0 AND updated>? LIMIT 1",
                (time.time() - 120,),
            ).fetchone():
                # Online voice is the only delivery route; never replay these later to Telegram.
                db.execute(
                    "UPDATE meta SET value=(SELECT coalesce(max(id),0) FROM inbox) WHERE key='telegram_cursor'"
                )
                return
            cursor = int(
                db.execute(
                    "SELECT value FROM meta WHERE key='telegram_cursor'"
                ).fetchone()[0]
            )
            rows = db.execute(
                "SELECT id,say,source FROM inbox WHERE id>? ORDER BY id LIMIT 100",
                (cursor,),
            ).fetchall()
        if not rows:
            return
        # Non-request housekeeping is not an owner notification.
        messages = [
            r["say"]
            for r in rows
            if r["source"] is None
            or r["source"].startswith(("lookup:", "task-progress:", "delegation:"))
        ]
        with self.store.connect() as db:
            for r in rows:
                if (r["source"] or "").startswith("run:"):
                    job = db.execute(
                        "SELECT kind FROM jobs WHERE run_id=?", (r["source"][4:],)
                    ).fetchone()
                    if job and job[0] in {"question", "task", "direct", "feedback"}:
                        messages.append(r["say"])
        if messages:
            try:
                p = subprocess.run(
                    [
                        str(Path.home() / ".local/bin/hermes"),
                        "send",
                        "--to",
                        target,
                        "--json",
                    ],
                    input="z0live work updates\n\n" + "\n\n".join(messages)[:12000],
                    capture_output=True,
                    text=True,
                    timeout=25,
                    env={
                        **os.environ,
                        "HERMES_HOME": os.environ.get(
                            "Z0LIVE_DELIVERY_HOME",
                            os.environ.get(
                                "Z0LIVE_HERMES_HOME",
                                "/workspace/hermes-home/profiles/clean",
                            ),
                        ),
                    },
                )
                result = json.loads(p.stdout)
                if (
                    p.returncode
                    or result.get("error")
                    or result.get("success") is False
                ):
                    raise RuntimeError("Hermes delivery not confirmed")
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                LOG.warning("offline delivery unavailable; retaining cursor for retry")
                return
        with self.store.connect() as db:
            db.execute(
                "UPDATE meta SET value=? WHERE key='telegram_cursor'",
                (str(rows[-1]["id"]),),
            )
