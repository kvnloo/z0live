from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from .actors.fake import FakeActor
from .fixtures import load_corpus
from .gateway import GatewayAddress
from .plan import load_plan, validate_plan
from .replay import replay_corpus
from .runtime import ConversationRuntime, RuntimeHooks
from .service import LiveService, home


def _parse_hostport(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host:
        raise argparse.ArgumentTypeError("expected HOST:PORT")
    return host, int(port)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="z0live")
    sub = p.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser(
        "serve",
        help="run a selected VoicePlan and local plugin gateway",
    )
    serve.add_argument("--plan", type=Path, required=True)
    serve.add_argument(
        "--listen",
        type=_parse_hostport,
        default=("127.0.0.1", 8765),
    )
    serve.add_argument("--harness", type=_parse_hostport)
    serve.add_argument("--actor-command")
    serve.add_argument(
        "--ready-timeout",
        type=float,
        default=300.0,
    )

    brainstorm = sub.add_parser(
        "brainstorm",
        help="compatibility alias for serve",
    )
    brainstorm.add_argument(
        "--plan",
        type=Path,
        required=True,
    )
    brainstorm.add_argument(
        "--listen",
        type=_parse_hostport,
        default=("127.0.0.1", 8765),
    )
    brainstorm.add_argument("--harness", type=_parse_hostport)
    brainstorm.add_argument("--actor-command")
    brainstorm.add_argument(
        "--ready-timeout",
        type=float,
        default=300.0,
    )

    replay = sub.add_parser(
        "replay",
        help="run deterministic conversational fixtures",
    )
    replay.add_argument("fixture", type=Path)
    replay.add_argument("--json", action="store_true")

    spec_replay = sub.add_parser(
        "speculation-replay",
        help="replay frozen partial/final authority fixtures",
    )
    spec_replay.add_argument("fixture", type=Path)
    spec_replay.add_argument("--json", action="store_true")

    speculation = sub.add_parser(
        "speculation-eval",
        help="evaluate measured side-effect-free prewarm traces",
    )
    speculation.add_argument("input", type=Path)
    speculation.add_argument("--min-p50-gain-ms", type=float, default=50.0)
    speculation.add_argument("--json", action="store_true")

    warm_probe = sub.add_parser(
        "warm-probe",
        help="headlessly load/handshake/unload the selected actor",
    )
    warm_probe.add_argument("--plan", type=Path, required=True)
    warm_probe.add_argument("--actor-command")
    warm_probe.add_argument("--ready-timeout", type=float, default=300.0)
    warm_probe.add_argument("--settle-seconds", type=float, default=1.0)
    warm_probe.add_argument("--output", type=Path)

    status = sub.add_parser(
        "status",
        help="show active session state",
    )
    status.add_argument("--home", type=Path)

    smoke = sub.add_parser(
        "smoke",
        help="exercise the portable runtime with a fake actor",
    )
    smoke.add_argument(
        "--seconds",
        type=float,
        default=0.05,
    )

    return p


def _run_service(args) -> int:
    plan = load_plan(args.plan)
    host, port = args.listen
    service = LiveService(
        plan,
        listen=GatewayAddress(host, port),
        harness_address=args.harness,
        actor_command=args.actor_command,
        ready_timeout_seconds=args.ready_timeout,
    )

    async def run() -> str:
        task = asyncio.create_task(service.run())
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (
                NotImplementedError,
                RuntimeError,
            ):
                pass
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            {task, stopper},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stopper in done and not task.done():
            await service.close("signal")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return "signal"
        stopper.cancel()
        return await task

    try:
        reason = asyncio.run(run())
        print(
            json.dumps(
                {"ok": True, "reason": reason}
            )
        )
        return 0
    except Exception as exc:
        print(f"z0live: {exc}", file=sys.stderr)
        return 2


def _smoke(seconds: float) -> int:
    raw = {
        "schema": "z0int.voice_plan.v1",
        "plan_id": "vp_smoke",
        "profile": "brainstorm",
        "harness": "smoke",
        "actor_id": "fake",
        "provider": "local",
        "model": "fake",
        "adapter": "fake",
        "resource": {
            "residency": "session",
            "idle_unload_seconds": 0,
        },
        "admission": {
            "admitted": True,
            "status": "admitted",
        },
    }
    plan = validate_plan(raw)
    actor = FakeActor(plan.actor_id)

    async def go() -> bool:
        runtime = ConversationRuntime(
            actor,
            hooks=RuntimeHooks(),
        )
        await runtime.start()
        await asyncio.sleep(max(0, seconds))
        ok = actor.started and not actor.closed
        await runtime.close()
        return ok and actor.closed

    ok = asyncio.run(go())
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.cmd in ("serve", "brainstorm"):
        return _run_service(args)

    if args.cmd == "replay":
        result = replay_corpus(
            load_corpus(args.fixture)
        )
        payload = result.to_dict()
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            passed = sum(
                1 for c in result.cases
                if c.passed
            )
            print(
                "z0live replay: "
                f"{passed}/{len(result.cases)} passed; "
                f"revision={result.revision}"
            )
            for case in result.cases:
                if not case.passed:
                    print(
                        f"FAIL {case.fixture_id}: "
                        f"expected={case.expected} "
                        f"observed={case.observed}"
                    )
        return 0 if result.passed else 1

    if args.cmd == "speculation-replay":
        from .speculation_replay import (
            load_speculation_corpus,
            replay_speculation_corpus,
        )

        result = replay_speculation_corpus(
            load_speculation_corpus(args.fixture)
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(
                "z0live speculation replay: "
                f"{result['cases_passed']}/{result['cases_total']} passed; "
                f"unauthorized_mutations={result['unauthorized_mutations']}; "
                f"revision={result['revision']}"
            )
        return 0 if result["passed"] else 1

    if args.cmd == "speculation-eval":
        from .speculation import SpeculationMeasurement, evaluate_speculation

        rows = []
        for line in args.input.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            rows.append(SpeculationMeasurement(**raw))
        result = evaluate_speculation(
            rows,
            min_p50_gain_ms=args.min_p50_gain_ms,
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(
                "z0live speculation: "
                f"p50_gain_ms={result['p50_useful_gain_ms']} "
                f"mutations={result['speculative_mutations']} "
                f"waste_rate={result['waste_rate']} "
                f"pass={result['pass']}"
            )
        return 0 if result["pass"] else 1

    if args.cmd == "warm-probe":
        from .probe import probe_actor

        plan = load_plan(args.plan)
        result = asyncio.run(
            probe_actor(
                plan,
                actor_command=args.actor_command,
                ready_timeout_seconds=args.ready_timeout,
                settle_seconds=max(0.0, args.settle_seconds),
            )
        )
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")
        return 0 if result["ok"] else 1

    if args.cmd == "status":
        root = (
            args.home or home()
        ).expanduser()
        path = root / "session.json"
        if not path.exists():
            print(json.dumps({"active": False}))
            return 1
        print(
            path.read_text(encoding="utf-8"),
            end="",
        )
        return 0

    if args.cmd == "smoke":
        return _smoke(args.seconds)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
