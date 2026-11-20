# z0live as an official Hermes plugin: maintainer request (DRAFT, NOT POSTED)

Prepared 2026-11-20 for Kevin. Nothing in this file has been posted anywhere.

Goal: get z0live into the plugin catalog using public seams only, and land the smallest piece of
RFC NousResearch/hermes-agent#77111 that unblocks z0live and the other duplex-voice plugins.

Inputs read: Hermes `origin/main` at `514e2cbda6`; RFC #77111 and all 28 comments; catalog PR #158091
(pins `8cdb912`); `website/docs/developer-guide/plugins/catalog-submission.md`; tek's
`z0live-review/docs/REVIEW-S-TIER.md`; `z0live-trace-v2/docs/TRACE-BOUNDARY.md`; z0live
`feat/lane-status` at `f55cd88`.

Labels: **measured** = I ran it or read it from code/`gh` today. **inferred** = follows from what I read
but untested. **guess** = judgment.

---

## 0. What changed since the last round (read first)

1. **Both core ABC PRs died unmerged.** #95147 (yours) closed 2026-09-30. #101808 (TheSmokeDev)
   closed 2026-10-23 "to clean up my open PRs". #77111 is still open, labelled `needs-decision`, `P3`.
   The last substantive question on the thread is lazmo88's: "is anyone taking this forward?" (measured)
2. **Main shipped GPT-Live in the meantime**, and it already has the shape the thread converged on.
   kshitijk4poor wrote it (`tools/voice_live.py` and `voice-engine-rows.tsx`; he is the only author of
   the picker, measured by `git log`).
   - The renderer holds WebRTC and the `oai-events` data channel (`apps/desktop/src/lib/voice-live.ts:314`).
   - Core does one server-side step, the SDP exchange with a host-held key
     (`tools/voice_live.py:162`, called from `hermes_cli/web_routers/audio.py:193-213`).
   - Each `session.delegation.created` becomes `prompt.submit` with surface `voice-live`. Hermes keeps
     tools, approvals and history. (measured, code read)
   - This is tuckercoffey's "surface-hosted" topology from the thread, already in production.
3. **Only the engine is hard-coded.** `voice_chat_mode()` returns `chained` or `gpt-live`
   (`tools/voice_live.py:107`). The route imports `create_webrtc_session` directly (`audio.py:201`).
   The picker accepts only those two values (`voice-engine-rows.tsx:41`). Issue #153488 (jacebenson,
   +1 ClaytonYoung) asks for plugin engines from the Desktop side. (measured)
4. **z0live currently works around this with a reverse proxy.** `voice-adapter/dashboard_proxy.py`
   (173 lines) sits in front of the Hermes dashboard and takes over `/api/audio/voice-live/status`
   and `/session` (`:12-16`). That is the "own front door" the catalog PR body mentions. A listed
   plugin cannot take those routes over in-process (rule 9). (measured)
5. **The validator is no longer failing.** `hermes plugins validate .` at `f55cd88` (local Hermes
   v0.23.2) passes, with 12 private-import warnings and 2 `caution` findings (`context_exfil`,
   `README.md:33` and `plugin.yaml:5`). The `phone_bench.sh` `dangerous` finding in tek's review is
   gone. (measured)
6. **You wrote 13 of the 28 comments on #77111, about 29k characters.** (measured, `gh api`) The next
   comment has to be short, answer the open question, and ask for one decision.

**Conclusion (inferred):** the smallest #77111 slice is no longer an ABC plus coordinator. It is one
engine slot on the route core already ships, plus an id-only hook and one credential row.

### Where the 12 warnings go

| Site (at `f55cd88`) | Warnings | What clears it | Core change? |
|---|---|---|---|
| `voice-adapter/xai_secret.py:12,17` | 2 | Seam 3 (`xai-oauth` row), mint from the plugin's engine | Yes, about 15 lines |
| `desktop/hermes_host.py:70,72` (+ `native_policy.py` private `_create_agent` subclass, not flagged) | 2 | Stock `api_server` on a dedicated profile, with the request body's `require_model_lock: true` (exists on main, `gateway/platforms/api_server.py:1886`) | No (inferred; verify fail-closed parity) |
| `desktop/hermes_relay.py:36-38` | 5 | Maintainer answer: is `hermes_cli.gateway_client` a supported out-of-process client? If yes, a docs line. If no, they name the route. | Docs only, or none |
| `voice-adapter/lane_server.py:16,17`, `desktop/liaison_host.py:42` | 3 | Move lane calls behind `ctx.llm.acomplete(provider=, model=)` (exists; gated by `plugins.entries.z0live.llm.allow_*_override`) | No |

Streaming does not justify a seam. Over 1,637 lane calls in `~/Library/Logs/z0live/*` the gap between
first token and full answer is p50 0.01 s, p90 0.67 s, max 2.35 s. (measured, timings only, script at
`~/.hermes/profiles/burn-ultra/cache/scratch/lane_timing.py`) The p50 is near zero because the Codex
route answers in one piece even when asked to stream (`lane_server.py:29`). So a non-streaming
`acomplete` costs about 10 ms at the median. I dropped a planned `ctx.llm.stream` ask because of this.

Rule 11 (credentials) also needs work, and it is z0live-side only:
`mobile_live.py:343` reads `~/.codex/auth.json` and sends `originator: Codex Desktop`
(`:366-367`, also `desktop/z0live_desktop/bidi_protocol.py:21,69-71`). `credentialed_provider_request("openai-codex", ...)`
already allows `chatgpt.com` and `api.openai.com` (`hermes_cli/plugin_provider_requests.py:71`). Move to
it first. If the Codex live endpoint then refuses Hermes's own headers, raise that privately with
Teknium. Do not raise it on the public thread. (measured for the reads; endpoint behavior unknown)

---

## (a) Comment for RFC #77111 (paste-ready)

Post only after steps 1 and 2 in section (c). Visible text is about 330 words, and the sketches are
collapsed.

````markdown
@lazmo88 asked whether anyone is taking this forward now that #95147 and #101808 are closed. I'd like to, with something much smaller than either.

Main now ships GPT-Live (`tools/voice_live.py`, thanks @kshitijk4poor), and it already has the shape this thread converged on. The renderer holds WebRTC and the `oai-events` channel. Core does one server-side step, the SDP exchange with a key that stays on the host. Each `session.delegation.created` becomes an ordinary `prompt.submit` on surface `voice-live`, so Hermes keeps tools, approvals and history and the voice model executes nothing. That is @tuckercoffey's surface-hosted topology, in production.

What's missing is a way for a plugin to supply that one server-side step. Today z0live runs a reverse proxy in front of the dashboard so it can take over `/api/audio/voice-live/*`, which a listed plugin can't do in-process (rule 9). #153488 asks for the same slot from the Desktop side. Three small seams would cover it:

1. **`ctx.register_voice_live_engine`**: one registrar row and a ~20-line base class. Core's GPT-Live becomes the first engine behind the interface. An engine returns an SDP answer for a peer that speaks the `oai-events` client-delegation vocabulary the renderer already drives. Vendors with a different wire (xAI, Gemini, a local model) translate in their own process. Core gets no new protocol, coordinator or vendor code.
2. **`on_voice_live_session` observer hook**, fired where core sees a call: `phase` (`minted` | `failed`), `engine`, `session_id`, `profile`, `error_class`, `setup_s`. IDs and timings only, never SDP, audio or transcript text.
3. **An `xai-oauth` row in `credentialed_provider_request`**, so an engine can mint a short-lived Grok Voice secret from the user's Hermes xAI sign-in instead of importing `hermes_cli.auth`.

Out of scope: core-hosted audio and the full `RealtimeVoiceProvider` coordinator. These seams don't block that work, and meanwhile hermes-talk's browser lane, Synero's live voice, @ClaytonYoung's voice-lab and z0live all get a supported way in.

If @teknium1 and @kshitijk4poor are happy with this boundary, I'll open (1)+(2) as one PR with tests and docs, and (3) as its own small PR. The Desktop picker change for #153488 would follow (1).

One question with no patch: is `hermes_cli.gateway_client` meant to be a supported client for an out-of-process tool that steers a live session (`session.steer`, `prompt.submit`)? If so, a docs line covers it. If not, which route should it use?

<details><summary>Sketch (1): <code>register_voice_live_engine</code></summary>

```python
# agent/voice_live_engine.py (new)
class VoiceLiveEngine(ABC):
    """The server-side step of a surface-hosted voice session. The surface owns media and the
    `oai-events` channel; every session.delegation.created still becomes
    prompt.submit(surface="voice-live"). An engine never runs tools or answers approvals."""
    name: str   # matched by voice.voice_chat_mode; "chained" and "gpt-live" are reserved
    label: str  # shown in the Desktop picker

    @abstractmethod
    def status(self) -> dict: ...  # non-secret {"available": bool, "reason": str}

    @abstractmethod
    def create_session(self, sdp_offer: str, history: list | None) -> dict: ...
        # -> {"session": {"id": str}, "transport": {"type": "webrtc", "sdp": str}}
        # ValueError -> 503 (not configured), RuntimeError -> 502 (upstream refused), as today

# agent/voice_live_registry.py (new): same shape as agent/tts_registry.py

# hermes_cli/plugins_registrars.py, _SCOPED_PROVIDER_REGISTRARS (beside register_tts_provider)
("register_voice_live_engine", "voice_live_engine", "agent.voice_live_registry",
 "agent.voice_live_engine:VoiceLiveEngine", "voice-live engine",
 "Register an :class:`agent.voice_live_engine.VoiceLiveEngine`; ``engine.name`` is matched by "
 "``voice.voice_chat_mode``. Reserved names (``chained``, ``gpt-live``) are rejected.",
 {"normalize": "lower"}),

# hermes_cli/plugins_context.py (beside register_delegation_provider)
def register_voice_live_engine(self, engine):
    return register_scoped_provider(self, "register_voice_live_engine", engine)

# tools/voice_live.py: the built-in becomes engine #1
class GptLiveEngine(VoiceLiveEngine):
    name, label = GPT_LIVE_MODE, "GPT-Live"
    def status(self): return resolve_gpt_live_status()
    def create_session(self, sdp_offer, history): return create_webrtc_session(sdp_offer, history)

def voice_chat_mode(voice=None) -> str:  # only the fall-through changes
    ...
    if mode in {GPT_LIVE_MODE, "gptlive", "live"}:
        return GPT_LIVE_MODE
    return mode if voice_live_registry.get(mode) else CHAINED_MODE

def active_engine() -> VoiceLiveEngine | None:
    mode = voice_chat_mode()
    return GptLiveEngine() if mode == GPT_LIVE_MODE else voice_live_registry.get(mode)

# hermes_cli/web_routers/audio.py, create_voice_live_session (replaces the direct import)
engine = active_engine()
if engine is None:
    raise HTTPException(status_code=409, detail="voice.voice_chat_mode is not a live engine")
result = await _run_config_scoped(profile, lambda: engine.create_session(sdp, payload.history))
# get_voice_live_status adds engines=[{name, label, available}] for the picker (#153488)
```

Tests: a fake engine registers, is selected by `voice_chat_mode`, answers the route, and a reserved name is rejected. Existing GPT-Live tests pass unchanged.
</details>

<details><summary>Sketch (2): <code>on_voice_live_session</code></summary>

```python
# hermes_cli/plugins.py, VALID_HOOKS
# on_voice_live_session: after a voice-live engine answered or refused a session
# (hermes_cli/web_routers/audio.py::create_voice_live_session). Observer; returns ignored.
# Kwargs: phase ("minted" | "failed"), engine, session_id (None on failure), profile,
# error_class, setup_s. IDs and timings only: never SDP, audio or transcript text.
"on_voice_live_session",

# hermes_cli/web_routers/audio.py
t0 = time.monotonic()
try:
    result = await _run_config_scoped(profile, lambda: engine.create_session(sdp, payload.history))
except (ValueError, RuntimeError) as exc:
    _voice_live_event("failed", engine.name, profile, None, type(exc).__name__, time.monotonic() - t0)
    raise  # existing 503/502 mapping unchanged
_voice_live_event("minted", engine.name, profile, (result.get("session") or {}).get("id"), None,
                  time.monotonic() - t0)

def _voice_live_event(phase, engine, profile, session_id, error_class, setup_s):
    from hermes_cli.plugins import has_hook, invoke_hook
    if has_hook("on_voice_live_session"):
        invoke_hook("on_voice_live_session", phase=phase, engine=engine, profile=profile,
                    session_id=session_id, error_class=error_class, setup_s=round(setup_s, 3))
```

Docs: a row in "Where each hook fires" (`website/docs/developer-guide/plugins/index.md`) and `website/docs/user-guide/features/hooks.md`. Test: the kwargs are exactly these and no SDP reaches a subscriber. There's no `ended` phase because core never sees call end; that would need a renderer change and is out of scope. If `invoke_hook` shouldn't run on the request path, I'll use the queued observer path that `on_room_member_activity` uses.
</details>

<details><summary>Sketch (3): <code>xai-oauth</code> in <code>credentialed_provider_request</code></summary>

```python
# hermes_cli/plugin_provider_requests.py, beside _codex_auth
def _xai_auth(url: str) -> tuple[dict[str, str], str]:
    from hermes_cli.auth_constants import AuthError
    from hermes_cli.auth_xai import resolve_xai_oauth_runtime_credentials
    try:
        creds = resolve_xai_oauth_runtime_credentials()
    except AuthError as exc:
        if not exc.relogin_required:
            raise
        creds = {}
    token = str(creds.get("api_key") or "").strip()
    if not token:
        raise ProviderNotSignedIn("Not signed in to xAI in Hermes. Run `hermes auth add xai-oauth --type oauth`.")
    return {"Authorization": f"Bearer {token}"}, str(creds.get("base_url") or "")

_PROVIDERS = {
    "openai-codex": (...),  # unchanged
    "xai-oauth": (frozenset({("https", "api.x.ai", 443)}), _xai_auth),
}

# plugin side, with requires_auth: [xai-oauth] in plugin.yaml
resp = credentialed_provider_request(
    "xai-oauth", "POST", "https://api.x.ai/v1/realtime/client_secrets",
    json={"expires_after": {"seconds": 600}})
```

Tests (in `tests/hermes_cli/test_plugin_provider_requests.py`): a non-`api.x.ai` origin is refused, no sign-in raises `ProviderNotSignedIn`, and the token never appears in the returned response.
</details>
````

---

## (b) Note for catalog PR #158091 (paste-ready)

Post on the PR as a comment, before anyone reviews `8cdb912`. Fill the bracketed items first.

```markdown
Status before review: please hold this pin. I'll re-pin to a corrected commit.

- The manifest at `8cdb912` says "Nothing leaves the machine" and `license: MIT`. The first is wrong for the voice stack, which streams audio and context to OpenAI and xAI. The second contradicts the repo's GPL-3.0 `LICENSE`. The PR body is accurate for the plugin's own tools, but the manifest text is what users see.
- The re-pin will have: a corrected description and a README privacy disclosure (already on the branch), [the reconciled license], `version: "0.3.0"`, and the fourth tool `z0live_say` declared. It will also declare `Z0LIVE_PROGRESS_TOKEN` in `requires_env`, read it through `agent.secret_scope.get_secret` (rule 11), and disclose its loopback POST to the voice adapter (rule 13).
- `hermes plugins validate` passes on the branch head. It reports 12 private-import warnings, all in z0live's separately installed services and none on the plugin load path, plus 2 `caution` findings that point at the privacy disclosure text. On #77111 [link to comment] I've proposed the seams that would retire them, and the seam that lets z0live stop proxying `/api/audio/voice-live/*`. Until those land, the warnings stay disclosed here.
```

Before posting (Kevin decides):
- **License.** MIT or GPL-3.0. `plugin.yaml:7-8` still carries `TODO(Kevin)` over `license: MIT`. (measured)
- **Subdir move.** Moving the plugin to `hermes-plugin/` with catalog `subdir:` (tek's item 15) would
  pin only what Hermes loads. If you do it, add one line to the note saying so and keep the services'
  warnings on #77111. That way the move reads as packaging, not hiding.
- **Fixes the re-pin needs** (z0live-side, `__init__.py` is not in another worker's region):
  `__init__.py:214` and `:294` read `os.environ.get("Z0LIVE_PROGRESS_TOKEN")`. Rule 11 wants
  `get_secret`. `plugin.yaml:11` has `requires_env: []`. The catalog entry's
  `capabilities.provides_tools` lacks `z0live_say`. (measured)
- The `hermes-agent-z0live` worktree has someone's uncommitted edit to `plugin-catalog/z0live.yaml`.
  I did not touch it. Reconcile it before re-pinning.

---

## (c) Who to ask, in order

Order rule: talk privately to the people whose code changes before posting publicly, then ask the
decider, then the reviewers. Don't open any core PR before Teknium gives the boundary call. #95147 and
#101808 show what happens to large unrequested voice PRs.

1. **alt-glitch (Siddharth Balyan): a friendly pre-read.** Send a DM with the comment text and one
   question: "Is a voice-live engine slot the right first slice, or will it read as ABC attempt #3?"
   - He built tern for you, and he posted the triage note on #77111 that points at #95147.
   - He has 95 commits on main, including plugin and provider reload work this month (measured).
   - Cheapest way to catch a bad pitch before it becomes your 14th public comment.
2. **kshitijk4poor: the owner of the code being rewired.** Ask privately: "OK for GPT-Live to become the
   first registered engine? Is there anything in the renderer's `oai-events` handling that an external
   engine must honor?"
   - He is the only author of `voice-engine-rows.tsx` and an author of `tools/voice_live.py`, and you
     tagged him on #77111 in August.
   - He committed 11 of your commits since October (measured).
   - Posting a plan to restructure his feature before he has seen it is the one avoidable misstep here.
3. **Teknium (@teknium1, "tek"): the decision and the merge.** Post the RFC comment tagging him and
   kshitijk4poor. Ask for a yes or no on seams 1+2 and seam 3, and for an answer on `gateway_client`.
   Then post the catalog note and re-pin.
   - He clears `needs-decision` items and owns `plugin-catalog/README.md` (24 commits).
   - He wrote 3 of the 5 commits on `plugin_provider_requests.py`, the file seam 3 edits.
   - He committed 61 of your 143 main commits since October, including all 13 sol-pi and glide catalog
     commits this month (measured).
4. **Mark S. (@unsupportedpastels): review of the hook and registrar shape, and the framing.**
   - His "everything is a plugin" direction is the pitch: core keeps one route and plugins supply
     engines.
   - He documented every `VALID_HOOKS` fire site (`d7827e025be`, 11-09) and co-owns
     `agent/plugin_llm.py` (measured).
   - Ask him to review the hook docs row once the PR is up. Also ask him to confirm that
     `ctx.llm.acomplete` with provider and model overrides is the intended route for z0live's lanes.
     That needs no new seam, given the measured 10 ms p50 streaming gain.
   - Backup lander if Teknium is busy: Maciej Lisiewski committed 60 of your commits since October and
     has touched `voice_live.py` and `plugin_llm.py` (measured).
5. **The RFC's consumers, through the comment itself, not separate pings.**
   - TheSmokeDev, the RFC author. hermes-talk's browser lane is already surface-hosted, so it is the
     natural engine #2. Credit him in the PR body.
   - jacebenson, author of #153488 (the Desktop half).
   - ClaytonYoung, who offered voice-lab as a reference local provider.
   - Don't re-ping the six original PR authors. Your August community note already did.

Wait about 48 hours for steps 1 and 2 (guess). If both are silent, post anyway, because the thread
asks a direct open question.

---

## Appendix: claims and how they were checked

| Claim | Label | How |
|---|---|---|
| #95147, #101808, #101034, #106640 closed unmerged | measured | `gh pr view` |
| Kevin wrote 13 of 28 comments, about 29k chars | measured | `gh api .../issues/77111/comments` |
| GPT-Live shape (renderer media, core SDP, `prompt.submit`) | measured | `tools/voice_live.py:1-20,162`, `audio.py:193-213`, `lib/voice-live.ts:314,397-514` |
| Engine hard-coded in 3 places | measured | `voice_live.py:107`, `audio.py:201`, `voice-engine-rows.tsx:41` |
| z0live proxies the voice-live routes | measured | `dashboard_proxy.py:12-16`, `mobile_live.py:1726-1728` |
| Validator passes, 12 warnings, 2 caution | measured | `hermes plugins validate .` at `f55cd88`, Hermes v0.23.2 (not main) |
| Lane stream gain p50 0.01 s, p90 0.67 s | measured | 1,637 `lane ... first= total=` log lines. Limiter: the Codex route answers whole. Caveat: a log line, not a client-side clock. |
| `require_model_lock` replaces `native_policy.py` | inferred | body field exists (`api_server.py:1886,2831`). Fail-closed parity not tested. |
| Seam 1 lets z0live delete `dashboard_proxy.py` | inferred | Needs the phone client to reach the gateway's route directly. Not tested. |
| Owners and commit counts | measured | `git log origin/main` by author and committer |
| 48-hour wait | guess | |

Not checked: whether `invoke_hook` is safe on the request path (TRACE-BOUNDARY flagged the same gap),
whether the Hermes phone client can reach the gateway's voice-live route without z0live's proxy, and the
rule-12 approval review of the chat path (tek's open item).
