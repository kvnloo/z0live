from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .plan import load_plan, validate_plan
from .session import BrainstormSession, _home


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="z0live")
    sub = p.add_subparsers(dest="cmd", required=True)

    brainstorm = sub.add_parser("brainstorm", help="run one session-scoped realtime voice actor")
    brainstorm.add_argument("--plan", type=Path, required=True)
    brainstorm.add_argument(
        "--actor-command",
        help="override actor launch command; supports {host} {port} {ssl_dir} {python}",
    )
    brainstorm.add_argument("--ready-timeout", type=float, default=300.0)
    brainstorm.add_argument("--activity-threshold-bytes", type=int, default=2048)

    touch = sub.add_parser("touch", help="mark the active brainstorm session as busy")
    touch.add_argument("--home", type=Path)

    status = sub.add_parser("status", help="show current brainstorm runtime state")
    status.add_argument("--home", type=Path)

    smoke = sub.add_parser("smoke", help="run lifecycle/proxy smoke test with a fake actor")
    smoke.add_argument("--idle", type=int, default=1)

    return p


def _smoke_plan(port: int, idle: int) -> dict:
    return {
        "schema": "z0int.voice_plan.v1",
        "plan_id": "vp_smoke",
        "profile": "brainstorm",
        "harness": "smoke",
        "actor_id": "personaplex-7b-nf4",
        "provider": "local",
        "model": "fake",
        "model_revision": "fake",
        "adapter": "personaplex",
        "quantization": "nf4",
        "device": {
            "backend": "cuda",
            "index": 0,
            "name": "fake",
            "total_vram_mb": 12288,
            "free_vram_mb": 12288,
        },
        "resource": {
            "residency": "session",
            "reserved_vram_mb": 10240,
            "idle_unload_seconds": idle,
            "restore_previous_gpu_state": True,
        },
        "admission": {"status": "admitted", "admitted": True},
        "endpoint": {"host": "127.0.0.1", "port": port, "scheme": "tcp"},
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "brainstorm":
        try:
            plan = load_plan(args.plan)
            session = BrainstormSession(
                plan,
                actor_command=args.actor_command,
                ready_timeout_seconds=args.ready_timeout,
                proxy_activity_threshold_bytes=args.activity_threshold_bytes,
            )
            print(
                f"z0live: warming {plan.actor_id}; public endpoint "
                f"{plan.endpoint_host}:{plan.endpoint_port}",
                file=sys.stderr,
            )
            reason = session.run_foreground()
            print(f"z0live: stopped ({reason})", file=sys.stderr)
            return 0
        except Exception as exc:
            print(f"z0live: {exc}", file=sys.stderr)
            return 2

    if args.cmd == "touch":
        home = (args.home or _home()).expanduser()
        home.mkdir(parents=True, exist_ok=True)
        (home / "brainstorm.activity").touch()
        return 0

    if args.cmd == "status":
        home = (args.home or _home()).expanduser()
        path = home / "brainstorm.json"
        if not path.exists():
            print(json.dumps({"active": False}))
            return 1
        raw = json.loads(path.read_text(encoding="utf-8"))
        print(json.dumps({"active": True, **raw}, indent=2))
        return 0

    if args.cmd == "smoke":
        import socket
        import tempfile

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        actor_cmd = (
            f"{sys.executable} -m z0live.fake_actor --host {{host}} --port {{port}}"
        )
        plan = validate_plan(_smoke_plan(port, args.idle))
        old_home = os.environ.get("Z0LIVE_HOME")
        with tempfile.TemporaryDirectory(prefix="z0live-smoke-") as tmp:
            os.environ["Z0LIVE_HOME"] = tmp
            try:
                reason = BrainstormSession(
                    plan,
                    actor_command=actor_cmd,
                    ready_timeout_seconds=5,
                    poll_seconds=0.05,
                    proxy_activity_threshold_bytes=64,
                ).run_foreground()
            finally:
                if old_home is None:
                    os.environ.pop("Z0LIVE_HOME", None)
                else:
                    os.environ["Z0LIVE_HOME"] = old_home
        print(json.dumps({"ok": reason == "idle_timeout", "reason": reason}))
        return 0 if reason == "idle_timeout" else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
