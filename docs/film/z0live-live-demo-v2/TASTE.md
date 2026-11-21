# Taste rules for the take-2 film

| Source | Rule | Applied in v2 |
| --- | --- | --- |
| Local owner critique, Nov 19, 2026 (orb animation pacing) | Let the orbs settle. Avoid fast changes that compete with the call. | Keep the baked orb engine. Blend the listening and speaking states on the audio envelope and drop the flying hand-off pulse. |
| Local owner feedback, Nov 20, 2026 (orb animation and easing) | Treat easing as a visual choice, not a way to retime the evidence. | Ease the hand-off chip's opacity. Keep the playhead, bar tips, and answer markers on the recording clock. |
| `company-os-visual-language`, “When to Use” | Use instrument lettering for readouts, serif for the title, and color for meaning. Let the content occupy the largest area. | Put the six-row trace at the center. Use IBM Plex Mono for the readouts and use the same color for each actor across the film. |
| `fframes-video-via-fframestudio`, procedure 3 and 11 | Preserve the source clock. Keep the story readable without audio, and disclose stand-ins on screen. | Keep the transcript, TTS and alignment disclosures, and mark Hermes as a stand-in in the trace itself. |
| `dream-loop`, procedure 3 and 7 | Compare the whole frame at matching times before judging a detail. Distinguish sampled review from playback. | Compare v1 and v2 at 21 s, then inspect the second hand-off and the settled trace before exporting. |

History was searched locally with SQLite FTS in `mode=ro` across profile databases. Search terms were `animation`, `launch video`, `fframes`, `orbs`, `easing`, `too fast`, `ugly`, `font`, and `glide`. The table records inferred design rules, not owner quotes. Exact message references stay local because this repo is public.

## Reading the trace

Every row uses the same fixed scale in seconds since recording start. Bars on the three lane rows run from the source hand-off to the lane answer. Dots mark answers; the vertical dashed lines mark hand-offs. Hermes answers carry a stand-in duration in `demo.json`. Match by that duration, not by the most recent hand-off: the first Hermes answer arrives after the second hand-off has already begun. The stand-in bar length is not a measured production latency.

Caller and voice bars come from the supplied RMS envelopes, not invented speaker end timestamps. They mark detected audio activity, not request latency. Bar tips advance at the playhead while the bar is active. The chart never draws an answer dot before its recorded timestamp.
