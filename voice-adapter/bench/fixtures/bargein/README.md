# Barge-in replay fixtures

Used by `../../bargein_replay.py` (frozen harness `bargein_replay/1`).

## Audio attribution

`2277-149896-0005.wav` and `2277-149896-0026.wav` are excerpts of
LibriSpeech (Vassil Panayotov et al.), [OpenSLR SLR12 dev-clean](https://www.openslr.org/12/),
licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
(see [LibriSpeech-LICENSE.txt](LibriSpeech-LICENSE.txt)).

- Source chapter: dev-clean/2277/149896
- `2277-149896-0005.wav`: from `2277-149896-0005.flac`, starting at 0.474 s
- `2277-149896-0026.wav`: from `2277-149896-0026.flac`, starting at 0.223 s
- Changes: each cropped to 0.85 s and stored as mono 16 kHz PCM16 WAV. The
  harness peak-normalizes them in memory at load time.

All other audio in the harness is synthesized locally. Do not modify these
files: the frozen reference numbers depend on their exact bytes.
