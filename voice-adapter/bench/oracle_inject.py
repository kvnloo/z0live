"""Paired GPT-Live probe on public facts; stdout is one JSON receipt per turn.

Run from voice-adapter with edge-tts on PATH and the adapter venv:
    PROBE_HERMES_DELAY=12 python bench/oracle_inject.py > oracle.jsonl
"""
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from array import array
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Question:
    key: str
    ask: str
    answer: str
    match: str


QUESTIONS = (
    Question("capital", "Can you look up the capital of Australia?", "Australia's capital is Canberra.", r"canberra"),
    Question("moons", "Can you check the name of the larger moon of Mars?", "The larger moon of Mars is Phobos.", r"phobos"),
    Question("element", "Look up which element has the symbol W.", "W is tungsten.", r"tungsten|wolfram"),
    Question("river", "Can you find out what river runs through Budapest?", "The Danube runs through Budapest.", r"danube"),
    Question("author", "Can you check who wrote The Left Hand of Darkness?", "Ursula K. Le Guin wrote The Left Hand of Darkness.", r"le\s*guin"),
    Question("nobel", "Look up the first woman to win a Nobel Prize.", "The first woman to win a Nobel Prize was Marie Curie.", r"(?:marie|maria)\s+(?:skłodowska[ -])?curie|madame curie"),
    Question("jupiter", "Can you look up the largest moon of Jupiter?", "Jupiter's largest moon is Ganymede.", r"ganymede"),
    Question("painter", "Can you check who painted Guernica?", "Pablo Picasso painted Guernica.", r"picasso"),
    Question("unit", "Find out the SI unit of electrical capacitance.", "The SI unit of capacitance is the farad.", r"farads?"),
    Question("island", "Can you look up the largest island in the Mediterranean Sea?", "Sicily is the largest island in the Mediterranean Sea.", r"sicily"),
)
FILLER = re.compile(
    r"^(?:(?:sure|of course|okay|yep|yeah|right|alright|all right|absolutely|got it|mm(?:-hm+)?)[.! ,—-]+)*"
    r"(?:let me (?:check|look|find|see|verify|pull|ask|get)|i(?:'ll| will| can) "
    r"(?:check|look|find|see|verify|pull|ask|get|have (?:it|that|hermes|the chat))|"
    r"(?:on it|one (?:sec(?:ond)?|moment)|just a moment|hold on))\b",
    re.I,
)


def speech_end(pcm):
    """Index of the first 20-ms frame after the last audible input frame."""
    for offset in range((len(pcm) - 1) // 1920 * 1920, -1, -1920):
        if any(abs(sample) >= 800 for sample in array("h", pcm[offset:offset + 1920])):
            return (offset + 1920) // 2
    raise ValueError("synthetic caller has no speech")


def score(question, arm, trace, receipt):
    end = next((e["t"] for e in trace if e["ev"] == "oracle.caller_end"), None)

    def since_end(at):
        return round(at - end, 3) if at is not None and end is not None else None

    def events(name, **where):
        return [e for e in trace if e["ev"] == name and all(e.get(k) == v for k, v in where.items())]

    audio = events("audio.on", dir="output")
    post_end = next((e["t"] for e in audio if end is not None and e["t"] >= end), None)
    response = ""
    answer_at = None
    pattern = re.compile(rf"\b(?:{question.match})\b", re.I)
    for e in events("tx", to="mobile", type="session.output_transcript.delta"):
        response += e["event"].get("delta", "")
        if answer_at is None and pattern.search(response):
            answer_at = e["t"]
    response = response.strip()
    first = re.split(r"(?<!\b[A-Z])[.!?](?:\s|$)", response, maxsplit=1)[0]
    injected = events("context.sent", type="session.context.append", channel="speakable")
    errors = [e["event"] for e in events("rx", src="provider", type="error")]
    user_end = next((e["t"] for e in events("rx", src="provider", type="turn.done")
                     if e["event"].get("turn", {}).get("role") == "user"), None)
    handoff = next((e["t"] for e in events("delegation.start")), None)
    return {
        "question": question.key, "arm": arm,
        "first_audio_s": since_end(audio[0]["t"]) if audio else None,
        "first_post_end_audio_s": since_end(post_end),
        "filler": bool(FILLER.match(response.replace("’", "'"))) if response else None,
        "answer_in_first_sentence": bool(pattern.search(first)),
        "first_answer_transcript_s": since_end(answer_at),
        "transcript": response, "heard": receipt["heard"], "delegated": receipt["delegated"],
        "provider_user_done_s": since_end(user_end), "handoff_s": since_end(handoff),
        "oracle_wire_s": since_end(injected[0]["t"]) if injected else None,
        "tiers": [{k: e.get(k) for k in ("tier", "model", "errors", "text")} for e in events("lanes.tier")],
        "tier_spoken_s": [since_end(e["t"]) for e in events("lanes.spoken")],
        "hermes_done_s": [since_end(e["t"]) for e in events("delegation.done")],
        "provider_errors": errors,
    }


async def run(questions, root):
    # Set before importing context_probe/mobile_live. Empty history alone would still load latest.md.
    os.environ.update({
        "HERMES_VOICE_BRIEF_DIR": str(root / "no-briefs"), "HERMES_MOBILE_VOICE_CHAT": "0",
        "HERMES_MOBILE_VOICE_LANES": "0", "HERMES_MOBILE_VOICE_CLIENT_DELEGATION": "0",
        "HERMES_VOICE_HANDOFF": "off", "VOICE_WARM_HAIKU": "0", "HERMES_VOICE_ENGINE": "gpt-live",
        "Z0LIVE_GUEST": "off", "Z0LIVE_TIERS": "on", "PROBE_HERMES_DELAY": "12", "PROBE_LISTEN": "22",
        "HERMES_MOBILE_VOICE_TRACE": "1", "HERMES_MOBILE_VOICE_TRACE_DIR": str(root / "traces"),
        "Z0LIVE_TRACE_AUDIO": "0", "PROBE_SAVE": "", "PROBE_SAVE_IN": "",
    })
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import context_probe as probe

    class Turn(probe.Prompt):
        call: probe.live.Call

        def __init__(self, pcm, end, answer):
            super().__init__(pcm)
            self.end, self.answer = end, answer

        def prepare(self, call):
            self.call = call

        async def recv(self):
            frame = await super().recv()
            if frame.pts == self.end:
                self.call.trace("oracle.caller_end")
                if self.answer is not None:
                    self.call.speak_for(None, self.answer)
            return frame

    for i, question in enumerate(questions):
        ask = f"Ask the Hermes chat to verify this, not from memory. {question.ask}"
        pcm = probe.speech(ask, root / "probe.mp3")
        end = speech_end(pcm)
        arms = ("baseline", "oracle") if i % 2 == 0 else ("oracle", "baseline")
        for arm in arms:
            track = Turn(pcm, end, question.answer if arm == "oracle" else None)
            try:
                receipt = await probe.probe([], prompt=track, prepare=track.prepare, hermes_answer=question.answer)
                path = track.call.trace.path
                trace = [json.loads(line) for line in path.read_text().splitlines()]
                row = score(question, arm, trace, receipt)
                row.update(trace=str(path), model=probe.live.MODEL, adapter_rev=probe.live.ADAPTER_REV)
            except Exception as exc:
                row = {"question": question.key, "arm": arm, "error": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(row), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", choices=[q.key for q in QUESTIONS], action="append")
    args = parser.parse_args()
    selected = [q for q in QUESTIONS if not args.question or q.key in args.question]
    root = Path(tempfile.mkdtemp(prefix="oracle-inject-"))
    asyncio.run(run(selected, root))
    os._exit(0)  # Match context_probe's aiortc thread teardown.


if __name__ == "__main__":
    main()
