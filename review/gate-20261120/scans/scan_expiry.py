"""Session lifetime granted by the provider: expires_at minus the wall clock at session.started. Numbers only.

usage: python3 -I scan_expiry.py <trace root>
"""
import json
import sys
from pathlib import Path


def main(root):
    for path in sorted(Path(root).glob("**/*.jsonl")):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"expires_at"' not in line or '"session.started"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                session = ((e.get("event") or {}).get("session") or {})
                expires, wall = session.get("expires_at"), e.get("wall")
                if isinstance(expires, (int, float)) and isinstance(wall, (int, float)):
                    print(f"{path.parent.name}/{path.name[:6]} {e.get('ev')} lifetime_s={expires - wall:.0f}")


if __name__ == "__main__":
    main(sys.argv[1])
