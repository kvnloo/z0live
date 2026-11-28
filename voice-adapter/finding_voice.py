"""Finding pages start the conversation (Kevin 11-28: "go to the page and then highlight the result").

The TV server (atlas-engine apps/z0live-tv, contract in FINDING-PAGES.md) draws a `finding` page: headline, key
numbers, a ranked chart, root cause, next step and an "Ask Kevin" card with lettered choices. While a call is open:

  * ask:      when the TV shows a finding page (GET /api/now -> show.view == "finding"), the voice reads that page's
              spoken ask (GET /api/finding?slug= -> say) once per page per call, and the TV rings the Ask card (hl=ask).
              Never before the call's first exchange (the voice never speaks first), never over Kevin or a reply.
  * decision: Kevin's next answer (a letter, "option B", or his own words; a question or a bare "okay" keeps waiting)
              is the decision for that page: appended to DECISIONS and traced as finding.decision.
  * ring:     when the voice (or Kevin) talks about one item of the page on screen (a bar key like F3, a bar or number
              label, "the root cause", "the next step"), the TV rings it: a view line {"view": "finding", "args":
              {"slug", "hl"}} appended to the conductor's inbox (the conductor stays the one writer of now.json).

Everything here is best effort: the TV server or the conductor being down never touches the call.
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from aiohttp import ClientTimeout

LOG = logging.getLogger("hermes.mobile.finding_voice")
TV = os.environ.get("Z0LIVE_TV_URL", "http://127.0.0.1:8793")
ON = os.environ.get("Z0LIVE_FINDING_VOICE", "1") != "0"
POLL_S = float(os.environ.get("Z0LIVE_FINDING_POLL_S", "0.5"))
ANSWER_S = float(os.environ.get("Z0LIVE_FINDING_ANSWER_S", "180"))  # how long an asked page waits for its answer
HL_TTL = float(os.environ.get("Z0LIVE_FINDING_HL_TTL", "180"))
HL_GAP_S = 1.5  # one ring per this many seconds at most: the TV redraws on each
FINDINGS = Path(os.environ.get("Z0LIVE_FINDINGS", str(Path.home() / ".local/state/z0live/findings")))
DECISIONS = Path(os.environ.get("Z0LIVE_FINDING_DECISIONS", str(FINDINGS / "decisions.jsonl")))
INBOX = Path(os.environ.get("Z0LIVE_CONDUCTOR_INBOX",
                            str(Path.home() / ".local/share/z0live/conductor/inbox.jsonl")))
RX_SLUG = re.compile(r"[A-Za-z0-9_-]{1,40}")

LETTERS = "abcd"
SOUNDS = {"ay": "a", "eh": "a", "bee": "b", "be": "b", "see": "c", "sea": "c", "cee": "c", "dee": "d"}
FILLER = {"um", "uh", "hmm", "ok", "okay", "so", "i", "think", "lets", "let's", "go", "with", "say", "yeah", "the",
          "please", "id", "i'd", "ill", "i'll", "take", "pick", "choose", "option", "choice", "letter", "do", "well",
          "alright", "right", "answer", "is", "it's", "its", "definitely", "probably", "just", "then", "and"}
STOP = {"the", "a", "an", "and", "or", "to", "of", "for", "you", "your", "with", "first", "back", "per", "one", "in",
        "on", "is", "be", "than", "as", "so", "it", "that", "this", "start", "starting", "today"}
QUESTION = re.compile(r"^(?:what|why|how|which|who|where|when|can|could|would|is|are|does|do|did|explain|tell me|"
                      r"wait|hold on|what's|whats)\b", re.I)
RX_EXPLICIT = re.compile(r"\b(?:option|choice|letter|plan|answer|go with|going with|pick|take|choose|let'?s do|"
                         r"do|i(?:'d| would) say|i say)\s+(?:option\s+|choice\s+)?([a-d]|ay|bee|see|sea|cee|dee)\b"
                         r"(?![-'])", re.I)


def _words(text):
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())


def parse_answer(text, choices):
    """Kevin's words after an ask -> (letter or None, choice text or None, kind). kind: 'letter' (he named one),
    'words' (his words match one choice), 'free' (a decision in his own words), None (not an answer yet: a question,
    a backchannel)."""
    t = " ".join(str(text or "").split())
    low = t.lower()
    letters = LETTERS[:max(0, min(len(choices), 4))]

    def pick(ch):
        ch = SOUNDS.get(ch.lower(), ch.lower())
        return (ch, choices[letters.index(ch)], "letter") if ch in letters else None

    m = RX_EXPLICIT.search(low)
    if m and (got := pick(m.group(1))):
        return got
    m = re.match(r"^\s*([a-d])\s*[.,!:;)-]", low)  # "B. warm session", "C, both"
    if m and (got := pick(m.group(1))):
        return got
    rest = [w for w in _words(low) if w not in FILLER]
    if len(rest) == 1 and (got := pick(rest[0])):  # a lone "B" / "C." / "see"
        return got
    question = t.endswith("?") or bool(QUESTION.match(low))
    said = set(_words(low))
    scores = []
    for i, c in enumerate(choices):
        toks = {w for w in _words(c) if w not in STOP and len(w) > 2}
        scores.append(len(toks & said))
    best = max(scores) if scores else 0
    if best and scores.count(best) == 1 and not question:
        i = scores.index(best)
        return letters[i] if i < len(letters) else None, choices[i], "words"
    if question or len(_words(low)) <= 2:
        return None, None, None
    return None, None, "free"


def _key_rx(key):
    """A bar key like F3 as spoken: 'F3', 'F 3', 'F-3', 'F three'."""
    m = re.fullmatch(r"([A-Za-z]+)(\d+)", key)
    if not m:
        return None
    nums = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
    n = int(m.group(2))
    spoken = f"|{nums[n]}" if n < len(nums) else ""
    return re.compile(rf"\b{re.escape(m.group(1))}\s*-?\s*(?:{n}{spoken})\b", re.I)


def matchers(spec):
    """[(key, compiled patterns)] for the items a spec draws: chart bars, number tiles, cause, next."""
    out = []
    for b in ((spec.get("chart") or {}).get("bars") or []):
        key = str(b.get("key") or "")
        if not RX_SLUG.fullmatch(key):
            continue
        pats = [p for p in (_key_rx(key),) if p]
        label = " ".join(_words(str(b.get("label") or "")))
        if len(label.split()) >= 2:
            pats.append(re.compile(r"\b" + r"\W+".join(map(re.escape, label.split())) + r"\b", re.I))
        out.append((key, pats))
    for n in spec.get("numbers") or []:
        key = str(n.get("key") or "")
        label = " ".join(_words(str(n.get("label") or "")))
        if RX_SLUG.fullmatch(key) and len(label.split()) >= 2:
            out.append((key, [re.compile(r"\b" + r"\W+".join(map(re.escape, label.split())) + r"\b", re.I)]))
    out.append(("cause", [re.compile(r"\b(?:root|likely) cause\b", re.I)]))
    out.append(("next", [re.compile(r"\bnext step\b", re.I)]))
    return out


def last_item(text, ms):
    """The item the text talks about last (the one being talked about now), or None."""
    best = None
    for key, pats in ms:
        for p in pats:
            for m in p.finditer(text or ""):
                if best is None or m.start() > best[0]:
                    best = (m.start(), key)
    return best[1] if best else None


class Page:
    """Pure state for one call: what was asked, what waits for an answer, what is ringed."""

    def __init__(self):
        self.asked = set()  # (slug, spec mtime) read aloud this call
        self.pending = None  # {"slug", "question", "choices", "at"} waiting for Kevin's answer
        self.hl = {}  # slug -> key last ringed
        self.hl_at = 0.0
        self.reading = False  # the voice is reading the ask: its words are not "talking about" an item

    def want_ask(self, payload):
        if not payload or not payload.get("ok") or not payload.get("say"):
            return False
        return (payload.get("slug"), payload.get("mtime")) not in self.asked

    def mark_asked(self, payload, now):
        self.asked.add((payload.get("slug"), payload.get("mtime")))
        ask = payload.get("ask") or {}
        self.pending = {"slug": payload.get("slug"), "question": ask.get("question") or "",
                        "choices": list(ask.get("choices") or []), "at": now}
        self.reading = True

    def answer(self, text, now):
        """A finished Kevin turn -> a decision dict, or None (no ask waiting, or not an answer yet)."""
        self.reading = False
        p = self.pending
        if not p:
            return None
        if now - p["at"] > ANSWER_S:
            self.pending = None
            return None
        letter, choice, kind = parse_answer(text, p["choices"])
        if not kind:
            return None
        self.pending = None
        return {"slug": p["slug"], "question": p["question"], "letter": letter.upper() if letter else None,
                "choice": choice, "kind": kind, "words": " ".join(str(text).split())[:400]}

    def ring(self, slug, key, now):
        """True when key should be pushed now (new for this page, outside HL_GAP_S of the last ring)."""
        if not key or self.hl.get(slug) == key or now - self.hl_at < HL_GAP_S:
            return False
        self.hl[slug] = key
        self.hl_at = now
        return True


def load_spec(slug):
    if not slug or not RX_SLUG.fullmatch(slug):
        return None
    try:
        return json.loads((FINDINGS / f"{slug}.json").read_text())
    except (OSError, ValueError):
        return None


def push_hl(slug, key, ttl=HL_TTL, inbox=None):
    row = {"t": round(time.time(), 3), "view": "finding", "args": {"slug": slug, "hl": key}, "ttl": ttl,
           "by": "voice", "title": f"finding {slug} {key}"}
    path = inbox or INBOX
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
        return True
    except OSError as exc:
        LOG.warning("finding hl not pushed: %s", type(exc).__name__)
        return False


def record(decision, call_id, path=None):
    path = path or DECISIONS
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "call": call_id, **decision}, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        LOG.warning("finding decision not saved: %s", type(exc).__name__)
        return False


async def get_json(http, url):
    try:
        async with http.get(url, timeout=ClientTimeout(total=2)) as r:
            return await r.json() if r.status == 200 else None
    except Exception:  # noqa: BLE001 - the TV server is optional
        return None


async def speak(call, text):
    """Hand the ask to the voice to read aloud (same two paths as needs_voice)."""
    if call.leg:
        if not call.leg.ready or call.leg.ws is None or call.leg.ws.closed:
            return False
        await call.leg.note("Kevin's TV now shows a finding page that ends with a question for him. Read this aloud "
                            "once, as written, then stop and wait for his answer: " + text, call.trace.turn_id)
        await call.leg.raw({"type": "response.create"})
        return True
    if not call.provider_ready or call.provider_dc is None or call.provider_dc.readyState != "open":
        return False
    for event in call.protocol.from_mobile({"type": "session.commentary.append", "content": text}):
        call.send(event, to_provider=True)
    return True


def showing(now_payload):
    """The finding slug on the TV now ('' = the newest spec), or None when the TV shows something else."""
    show = (now_payload or {}).get("show") or {}
    if show.get("view") != "finding":
        return None
    slug = str((show.get("args") or {}).get("slug") or "")
    return slug if not slug or RX_SLUG.fullmatch(slug) else None


async def run(call):
    """One per call, until it closes."""
    if not ON:
        return
    st = Page()
    seen = set()  # id() of the protocol.exchanges tuples already handled
    specs = {}  # slug -> (matchers) for the page on screen
    while not call.closed:
        await asyncio.sleep(POLL_S)
        try:
            await step(call, st, seen, specs)
        except Exception as exc:  # noqa: BLE001 - never touch the call
            LOG.warning("finding step failed id=%s: %s", call.id, type(exc).__name__)


async def step(call, st, seen, specs):
    http, proto = call.manager.http, call.protocol
    now = time.time()
    fresh = [x for x in list(proto.exchanges) if id(x) not in seen]
    seen.clear()
    seen.update(id(x) for x in proto.exchanges)
    for role, said in fresh:  # finished turns first: Kevin's answer to an ask
        if role == "user":
            d = st.answer(said, now)
            if d:
                record(d, getattr(call, "ledger_call", None) or call.id)
                call.trace("finding.decision", **{k: v for k, v in d.items() if k != "question"})
                if st.ring(d["slug"], "ask", now) and push_hl(d["slug"], "ask"):
                    call.trace("finding.hl", slug=d["slug"], key="ask", by="decision")
    nowp = await get_json(http, TV + "/api/now")
    slug = showing(nowp)
    if slug is None:
        return
    page = await get_json(http, TV + "/api/finding" + (f"?slug={slug}" if slug else ""))
    if not page or not page.get("ok"):
        return
    slug = page.get("slug") or slug
    if st.want_ask(page) and proto.exchanges and not call.floor_busy():
        call.stamp_status(time.monotonic())  # the ask answers the page he opened; status lines keep their gap after it
        ok = await speak(call, page["say"])
        call.trace("finding.ask_spoken" if ok else "finding.ask_dropped", slug=slug, text=page["say"][:600],
                   choices=(page.get("ask") or {}).get("choices"))
        if ok:
            st.mark_asked(page, now)
            if st.ring(slug, "ask", now) and push_hl(slug, "ask"):
                call.trace("finding.hl", slug=slug, key="ask", by="ask")
        return
    if slug not in specs:
        spec = load_spec(slug)
        specs[slug] = matchers(spec) if spec else []
    ms = specs[slug]
    if not ms:
        return
    # What is being said now (the live turn text) and what was just said (finished turns), newest last.
    texts = [(r, s) for r, s in fresh] + [("user", proto.turn_text.get("user", "")),
                                          ("assistant", proto.turn_text.get("assistant", ""))]
    for role, text in reversed(texts):
        if not text or (role == "assistant" and st.reading):
            continue
        key = last_item(text, ms)
        if key:
            if st.ring(slug, key, now) and push_hl(slug, key):
                call.trace("finding.hl", slug=slug, key=key, by="kevin" if role == "user" else "voice",
                           words=" ".join(text.split())[-160:])
            break
