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

## Multi-speaker set (`multi/`, used only with `--clip-set multi`, bargein_replay/22)

`multi/*.wav` are excerpts of the same corpus and license (LibriSpeech,
Vassil Panayotov et al., OpenSLR SLR12 dev-clean, CC BY 4.0; see
[LibriSpeech-LICENSE.txt](LibriSpeech-LICENSE.txt)): two utterances from each
of six speakers other than 2277. Sex is the corpus `SPEAKERS.TXT` label.

| File | Speaker (sex) | Source | Excerpt start |
|---|---|---|---|
| `multi/1272-135031-0011.wav` | 1272 (M) | dev-clean/1272/135031/1272-135031-0011.flac | 0.570 s |
| `multi/1272-135031-0012.wav` | 1272 (M) | dev-clean/1272/135031/1272-135031-0012.flac | 0.580 s |
| `multi/174-50561-0013.wav` | 174 (M) | dev-clean/174/50561/174-50561-0013.flac | 2.980 s |
| `multi/174-50561-0015.wav` | 174 (M) | dev-clean/174/50561/174-50561-0015.flac | 3.070 s |
| `multi/2803-161169-0014.wav` | 2803 (M) | dev-clean/2803/161169/2803-161169-0014.flac | 0.360 s |
| `multi/2803-161169-0015.wav` | 2803 (M) | dev-clean/2803/161169/2803-161169-0015.flac | 0.210 s |
| `multi/84-121123-0018.wav` | 84 (F) | dev-clean/84/121123/84-121123-0018.flac | 0.170 s |
| `multi/84-121123-0019.wav` | 84 (F) | dev-clean/84/121123/84-121123-0019.flac | 0.310 s |
| `multi/1462-170138-0016.wav` | 1462 (F) | dev-clean/1462/170138/1462-170138-0016.flac | 0.240 s |
| `multi/1462-170138-0017.wav` | 1462 (F) | dev-clean/1462/170138/1462-170138-0017.flac | 0.600 s |
| `multi/5338-284437-0010.wav` | 5338 (F) | dev-clean/5338/284437/5338-284437-0010.flac | 0.590 s |
| `multi/5338-284437-0011.wav` | 5338 (F) | dev-clean/5338/284437/5338-284437-0011.flac | 0.470 s |

- Changes: each decoded losslessly from FLAC, cropped to 0.85 s (13600 samples)
  and stored as mono 16 kHz PCM16 WAV, like the 2277 clips. The harness
  peak-normalizes them in memory at load time.
- Onset and level match: the excerpt is the first 0.85 s window (scanned in
  10 ms steps) that starts at speech onset and whose statistics after that peak
  normalization reach the minimum of the two 2277 clips: one of its first two
  10 ms frames has RMS >= 1360 (both 2277 clips start within 10 ms of onset),
  RMS >= 3645, and >= 71 % of 10 ms frames with RMS >= 1360. Per speaker, the
  first two utterances (in corpus order) with such a window are used.

All other audio in the harness is synthesized locally. Do not modify these
files: the frozen reference numbers depend on their exact bytes.
