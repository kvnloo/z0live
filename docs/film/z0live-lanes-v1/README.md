# z0live lanes film v1

fframes 1.3 crate: one call, many minds, one shared context window. ~34 s, 1920x1080, 60 fps, silent-safe
captions. The orbs are the real thinking-orbs engine, baked per frame and drawn as vector dots (the same
pipeline as launch-videos `films/glide-hackathon-v3`).

Builds on the render host beside glide-hackathon-v3: copy its `media/`, `orbs/`, `local-fframes.toml`, `Cargo.lock` and
`build-local.sh` next to this `Cargo.toml`, then `bash build-local.sh` and `bash render.sh NAME` (-> out/NAME.mp4).

Honesty labels on screen: latencies come from real z0live traces (fast lane spoken 1.3-2.1 s; Grok 2.4 s after its
name); the question and the answers are scripted.
