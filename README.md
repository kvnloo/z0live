# z0live

Realtime conversation runtime for Zer0. `z0intelligence` selects/admisses the voice actor; `z0live` owns the session lifecycle; harnesses such as OMP own tools and agent execution.

## Brainstorm Mode

Heavy local voice is **not resident**. The first slice loads PersonaPlex NF4 only for an admitted brainstorm session and releases it on exit or idle timeout.

```text
z0intelligence -> VoicePlan -> z0live -> OMP/Hermes
                  admission    lifecycle
```

### Install

```bash
python -m pip install -e .
```

Accept the NVIDIA PersonaPlex model license, export `HF_TOKEN`, then:

```bash
bash scripts/setup-personaplex-nf4.sh
```

The setup script installs the modified Moshi runtime but does **not** start the model. It skips the 6.98 GB pre-quantized pickle and uses the repo's on-the-fly `--quantize-4bit` path.

### Run from OMP

With sibling `z0intelligence`, `z0live`, and `oh-my-pi` checkouts:

```bash
cd ../oh-my-pi
bash scripts/z0live-brainstorm.sh
```

`z0intelligence` checks current free VRAM. On a busy GPU it refuses and prints the exact reclaim deficit instead of killing unrelated work.

When ready, the PersonaPlex endpoint remains `https://localhost:8998`. z0live transparently proxies it to an internal actor port and uses meaningful transport traffic as the idle heartbeat. Ctrl-C tears down the actor immediately.

### Smoke test without a GPU

```bash
python -m z0live smoke
```

This starts a fake actor and runs the same process/proxy/idle lifecycle without model weights.

## State / receipts

Runtime state lives under `~/.z0live/`:

- `brainstorm.json` while active
- `brainstorm.activity` heartbeat
- `receipts.jsonl` lifecycle receipts

No actor starts at import time or normal OMP startup.
