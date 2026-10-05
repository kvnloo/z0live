# OMP z0live extension

This is an **OMP extension**, not OMP core code.

## Install

Symlink the extension into OMP's user extension directory:

```bash
mkdir -p ~/.omp/agent/extensions
ln -sf "$PWD/integrations/omp/z0live.ts" ~/.omp/agent/extensions/z0live.ts
```

Install the companion CLIs first:

```bash
pip install -e ../z0intelligence
pip install -e .
```

Then in OMP:

```text
/brainstorm on
/brainstorm status
/brainstorm off
```

Or start OMP with `--brainstorm`.

## Boundary

- z0intelligence: hardware/resource admission + VoicePlan
- z0live: realtime actor/session/floor/attention
- this extension: OMP bridge + UI
- OMP core: tools, agents, approvals, task execution

The extension advertises `approvals: false` for remote resolution because the current public Extension API exposes approval events but no approval-resolution action. It still forwards approval-required events to z0live for attention.

Turn completion is emitted with `verified: false`; z0live must not announce it as verified success. A later verified-result hook can upgrade that when OMP exposes a stable verification event.
