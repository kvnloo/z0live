"""Owner-initiated ring diagnostics. Raw sensor data never leaves NOOP via this API."""
import json
import re
import time
import uuid


def tool(name, description, properties, required=()):
    return dict(type="function", name=name, description=description,
                parameters=dict(type="object", properties=properties,
                                required=list(required), additionalProperties=False))


TOOLS = [
    tool("ring_recording_start", "Start private ring recording during this call. 'Recording now' starts labelled; 'record baseline' starts baseline. Confirm in 1–3 words, only as confirmed by result. Tap actions are suppressed, never end the call.",
         {"kind": {"type": "string", "enum": ["labelled", "baseline"]}, "note": {"type": "string"}}, ["kind"]),
    tool("ring_label", "Label from now until the next label: 'double', 'doing triples', 'nothing / baseline / walking now' = none. 'That was a miss' stores a note, NOT a fabricated detection or repetition count; undo removes the last label. Confirm in 1–3 words.",
         {"label": {"type": "string", "enum": ["double", "triple", "none", "undo"]}, "note": {"type": "string"}}, ["label"]),
    tool("ring_recording_stop", "Stop ring recording, save labels, restore normal tap actions. Confirm in 1–3 words.", {}),
    tool("ring_recording_status", "Report device-confirmed detection counts, not recall: spoken segment labels do not count attempted repetitions.", {}),
]
NAMES = {t["name"] for t in TOOLS}


def voice_command(value, active=False, current_label=None):
    """Narrow Bidi fast path; unrelated conversation must not become diagnostic controls."""
    value = re.sub(r"[.!?,]+$", "", value.strip().lower())
    if value in {"recording now", "start recording", "start ring recording"}:
        return "ring_recording_start", {"kind": "labelled"}
    if value in {"record baseline", "start baseline recording"}:
        return "ring_recording_start", {"kind": "baseline"}
    if value in {"stop recording", "stop ring recording"}:
        return "ring_recording_stop", {}
    if value in {"how many doubles caught", "recording status", "how many triples caught"}:
        return "ring_recording_status", {}
    if not active:
        return None
    labels = {"double": "double", "doubles": "double", "doing doubles": "double",
              "triple": "triple", "triples": "triple", "doing triples": "triple",
              "nothing": "none", "baseline": "none", "walking now": "none", "undo": "undo"}
    if value in labels:
        return "ring_label", {"label": labels[value], "note": value}
    if value == "that was a miss":
        return "ring_label", {"label": current_label or "none", "note": "owner-reported miss (not an attempted-repetition count)"}
    return None


class RingRecordings:
    def __init__(self, store):
        self.store = store
        self.clock = time.time
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS ring_recordings(
                id TEXT PRIMARY KEY, sid TEXT NOT NULL, source TEXT NOT NULL,
                start REAL NOT NULL, expires REAL NOT NULL, revision INTEGER NOT NULL,
                desired TEXT NOT NULL, payload TEXT NOT NULL, report TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS ring_tool_receipts(sid TEXT NOT NULL, call_id TEXT NOT NULL,
                request TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(sid,call_id));
            """)

    def current(self, sid):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM ring_recordings WHERE sid=? ORDER BY start DESC,rowid DESC LIMIT 1", (sid,)).fetchone()
        return dict(row) if row else None

    def execute(self, device, sid, name, args, call_id=None):
        allowed = {"ring_recording_start": {"kind", "note"}, "ring_label": {"label", "note"},
                   "ring_recording_stop": set(), "ring_recording_status": set()}
        if name not in allowed or set(args) - allowed[name]:
            raise ValueError("invalid recording arguments")
        note = args.get("note", "")
        if not isinstance(note, str) or len(note) > 160 or any(ord(c) < 32 for c in note):
            raise ValueError("note must be at most 160 printable characters")
        if call_id is not None and (not isinstance(call_id, str) or not 1 <= len(call_id) <= 200):
            raise ValueError("invalid recording call_id")
        now = self.clock()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            request = json.dumps([name, args], sort_keys=True)
            def respond(result):
                if call_id:
                    db.execute("INSERT INTO ring_tool_receipts VALUES(?,?,?,?)", (sid, call_id, request, json.dumps(result)))
                return result
            session = self.store.owned(db, sid, device)
            # owned validates identity; explicitly reject ended and superseded sessions too.
            latest = db.execute("SELECT id,ended FROM sessions WHERE device=? ORDER BY created DESC,rowid DESC LIMIT 1", (device,)).fetchone()
            if not latest or latest["id"] != sid or latest["ended"]:
                raise ValueError("latest active voice session required")
            if call_id:
                receipt = db.execute("SELECT request,result FROM ring_tool_receipts WHERE sid=? AND call_id=?", (sid, call_id)).fetchone()
                if receipt:
                    if receipt["request"] != request:
                        raise ValueError("recording call_id arguments changed")
                    return json.loads(receipt["result"])
            row = db.execute("SELECT * FROM ring_recordings WHERE sid=? ORDER BY start DESC,rowid DESC LIMIT 1", (sid,)).fetchone()
            if name == "ring_recording_start":
                if args.get("kind") not in {"labelled", "baseline"}:
                    raise ValueError("kind must be labelled or baseline")
                existing = db.execute("SELECT 1 FROM ring_recordings WHERE expires>? AND desired!='done'", (now,)).fetchone()
                if existing:
                    raise ValueError("recording already active; stop it first")
                lease = db.execute("SELECT device FROM gesture_listener WHERE until>?", (now,)).fetchone()
                if not lease:
                    return respond(dict(ok=False, status="unavailable", say="Ring unavailable"))
                db.execute("UPDATE gesture_commands SET status='denied' WHERE status='queued'")
                rid = uuid.uuid4().hex
                payload = dict(id=rid, kind=args["kind"], note=note, broker_start=now,
                               expires=now+600, labels=[dict(ts=now, label="none", note=note)])
                db.execute("INSERT INTO ring_recordings(id,sid,source,start,expires,revision,desired,payload) VALUES(?,?,?,?,?,1,'recording',?)",
                           (rid, sid, lease["device"], now, now+600, json.dumps(payload)))
                return respond(dict(ok=True, status="queued", id=rid, say="Starting"))
            if not row:
                return respond(dict(ok=False, status="idle", say="Not recording"))
            report = json.loads(row["report"])
            if name == "ring_recording_status":
                return respond(dict(ok=True, desired=row["desired"], expired=now >= row["expires"], device=report,
                            say="Device unconfirmed" if not report else "Device counts", id=row["id"]))
            if row["expires"] <= now or row["desired"] != "recording":
                return respond(dict(ok=False, status="inactive", say="Not recording"))
            payload = json.loads(row["payload"])
            if name == "ring_label":
                label = args.get("label")
                if label not in {"double", "triple", "none", "undo"}:
                    raise ValueError("invalid ring label")
                if payload["kind"] == "baseline" and label in {"double", "triple"}:
                    raise ValueError("baseline stays none; stop and start labelled to record taps")
                if label == "undo":
                    if len(payload["labels"]) > 1:
                        payload["labels"].pop()
                else:
                    if len(payload["labels"]) >= 100:
                        raise ValueError("label limit reached")
                    payload["labels"].append(dict(ts=now, label=label, note=note))
                    if len(json.dumps(payload, ensure_ascii=False).encode()) > 24000:
                        raise ValueError("label byte limit reached; stop and start a new recording")
                say, desired = "Labelled", "recording"
            else:
                payload["broker_stop"] = now
                say, desired = "Stopping", "stop"
            db.execute("UPDATE ring_recordings SET desired=?,revision=revision+1,payload=? WHERE id=?",
                       (desired, json.dumps(payload), row["id"]))
            return respond(dict(ok=True, status="queued", say=say, id=row["id"]))

    def poll(self, source):
        now = self.clock()
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM ring_recordings WHERE source=? AND desired!='done' AND expires>? ORDER BY start DESC LIMIT 1", (source, now)).fetchone()
        if not row:
            return dict(broker_ts=now, recording=None)
        return dict(broker_ts=now, recording={**json.loads(row["payload"]), "revision": row["revision"], "desired": row["desired"]})

    def report(self, source, body):
        if not isinstance(body, dict) or set(body) != {"id", "status", "counts", "frames"}:
            raise ValueError("id, status, counts, frames required")
        if body["status"] not in {"recording", "saved", "failed"} or not isinstance(body["counts"], dict):
            raise ValueError("invalid device report")
        if set(body["counts"]) != {"double", "triple", "quadruple"} or any(type(n) is not int or not 0 <= n <= 10000000 for n in [body["frames"], *body["counts"].values()]):
            raise ValueError("invalid recording counts")
        report = {**body, "received": self.clock()}
        with self.store.connect() as db:
            row = db.execute("UPDATE ring_recordings SET report=?,desired=CASE WHEN ? IN ('saved','failed') THEN 'done' ELSE desired END WHERE id=? AND source=?",
                             (json.dumps(report), body["status"], body["id"], source))
        return dict(ok=row.rowcount == 1)

    def suppresses(self, source):
        with self.store.connect() as db:
            return db.execute("SELECT 1 FROM ring_recordings WHERE source=? AND expires>? AND desired!='done'", (source, self.clock())).fetchone() is not None
