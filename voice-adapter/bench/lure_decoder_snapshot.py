"""R8-5: opt-in decoder-state snapshot for lure_mlx 0.1.0 MLXBackend (no site-packages edits).

MLXBackend.generate (stream branch) runs, per call:
    dec.reset_streaming_state(); if icl: self._prime_decoder(dec, req.voice)
Priming streams every ICL reference frame through the codec decoder (output discarded) and is a pure function of
(reference codes, prime_frames, decoder weights). This module installs an INSTANCE override of `_prime_decoder`:
  - miss: run the library priming, then deep-copy the primed decoder streaming state (8-layer KVCache keys/values +
    offset, every stateful module's `_buffer` / `_overflow`) into a per-key snapshot (mx.array copies + one mx.eval);
  - hit: install FRESH copies of the snapshot (one mx.eval), so in-place KV writes never touch the stored snapshot.
Key = (voice_id, wav_sha256 or sha256(ref_codes), n_ref_frames, effective prime_frames, model_key). model_key holds the
model dir and the id of the decoder Module, so a reloaded model never matches; `invalidate()` and backend.close() clear.
Off by default: nothing changes until enable_decoder_snapshot(backend) is called; disable restores the class method.

API: enable_decoder_snapshot(be) -> DecoderSnapshotCache; disable_decoder_snapshot(be); is_enabled(be); model_key(be)
"""
import hashlib
import time
from collections import OrderedDict

import numpy as np

STATE_KEYS = ("_buffer", "_overflow")
_ATTR = "_r85_decoder_snapshot"


def _real_module(dec):
    """speech_tokenizer.decoder is an mlx `_gc_func` proxy; the bound method's __self__ is the Module."""
    return dec.streaming_step.__self__


def _sget(m, k):
    if k in m:
        return m[k]
    return vars(m).get(k, None)


def _state_modules(mod):
    return [(n, m) for n, m in mod.named_modules() if hasattr(m, "reset_state")]


def model_key(backend):
    dec = backend.model.speech_tokenizer.decoder
    return ("model", str(backend.model_dir), id(_real_module(dec)))


def _effective_prime(backend, voice):
    n = backend.opts.get("prime_frames")
    T = voice.n_ref_frames
    return T if n is None else max(0, min(int(n), T))


class DecoderSnapshotCache:
    def __init__(self, backend, max_voices=8, verify=False):
        self.backend = backend
        self.verify = bool(verify)
        self.mx = backend.mx
        self.max_voices = int(max_voices)
        self._snaps = OrderedDict()
        self._stats = {"hits": 0, "misses": 0, "captures": 0, "evictions": 0, "invalidations": 0,
                       "last_capture_ms": None, "last_prime_ms": None, "last_restore_ms": None}
        self._orig = type(backend)._prime_decoder  # unbound class method (library code)

    # ---------------------------------------------------------------- keys
    def key_for(self, voice):
        w = voice.meta.get("wav_sha256") if getattr(voice, "meta", None) else None
        if not w:
            w = "codes:" + hashlib.sha256(np.ascontiguousarray(voice.ref_codes).tobytes()).hexdigest()
        return (voice.voice_id, w, int(voice.n_ref_frames), _effective_prime(self.backend, voice),
                model_key(self.backend))

    def __len__(self):
        return len(self._snaps)

    def stats(self):
        return dict(self._stats, entries=len(self._snaps))

    def invalidate(self):
        self._snaps.clear()
        self._stats["invalidations"] += 1

    # ---------------------------------------------------------------- state copy
    def _capture(self, dec):
        mx = self.mx
        mod = _real_module(dec)
        tc = _sget(mod, "_transformer_cache")
        kv = [(mx.array(c.keys) if c.keys is not None else None,
               mx.array(c.values) if c.values is not None else None, int(c.offset)) for c in (tc or [])]
        mods = {}
        for n, m in _state_modules(mod):
            d = {}
            for k in STATE_KEYS:
                if k in m or k in vars(m):
                    v = _sget(m, k)
                    d[k] = mx.array(v) if v is not None else None
            mods[n] = d
        arrs = [a for t in kv for a in t[:2] if a is not None] + [v for d in mods.values() for v in d.values()
                                                                   if v is not None]
        mx.eval(*arrs)
        return {"kv": kv, "mods": mods, "has_cache": tc is not None}

    def _restore(self, dec, s):
        from mlx_audio.lm.models.cache import KVCache
        mx = self.mx
        mod = _real_module(dec)
        arrs = []
        if s["has_cache"]:
            cache = []
            for k, v, off in s["kv"]:
                c = KVCache()
                if k is not None:
                    c.keys, c.values = mx.array(k), mx.array(v)
                    arrs += [c.keys, c.values]
                c.offset = off
                cache.append(c)
            mod._transformer_cache = cache
        else:
            mod._transformer_cache = None
        mods = dict(_state_modules(mod))
        for n, d in s["mods"].items():
            m = mods[n]
            for k, v in d.items():
                if v is None:
                    setattr(m, k, None)
                else:
                    cp = mx.array(v)
                    setattr(m, k, cp)
                    arrs.append(cp)
        mx.eval(*arrs)

    # ---------------------------------------------------------------- integrity (tests / harness only)
    def snapshot_sha(self, key):
        s = self._snaps[key]
        mx = self.mx
        h = hashlib.sha256()
        items = []
        for i, (k, v, off) in enumerate(s["kv"]):
            items += [(f"kv{i}.keys", k), (f"kv{i}.values", v), (f"kv{i}.offset", off)]
        for n in sorted(s["mods"]):
            for k in sorted(s["mods"][n]):
                items.append((f"{n}.{k}", s["mods"][n][k]))
        for name, a in items:
            h.update(name.encode())
            if a is None:
                h.update(b"None")
            elif isinstance(a, int):
                h.update(str(a).encode())
            else:
                h.update(str(a.dtype).encode()); h.update(str(a.shape).encode())
                h.update(np.array(a.astype(mx.float32) if a.dtype == mx.bfloat16 else a).tobytes())
        return h.hexdigest()

    def built_sha(self, key):
        return self._snaps[key].get("sha")

    # ---------------------------------------------------------------- the override
    def prime(self, dec, voice):
        if _effective_prime(self.backend, voice) == 0:
            return self._orig(self.backend, dec, voice)
        key = self.key_for(voice)
        s = self._snaps.get(key)
        if s is not None:
            t = time.perf_counter()
            self._restore(dec, s)
            self._stats["last_restore_ms"] = (time.perf_counter() - t) * 1000
            self._stats["hits"] += 1
            self._snaps.move_to_end(key)
            return None
        self._stats["misses"] += 1
        t = time.perf_counter()
        self._orig(self.backend, dec, voice)
        t1 = time.perf_counter()
        s = self._capture(dec)
        t2 = time.perf_counter()
        self._stats["last_prime_ms"] = (t1 - t) * 1000
        self._stats["last_capture_ms"] = (t2 - t1) * 1000
        self._stats["captures"] += 1
        self._snaps[key] = s
        s["sha"] = self.snapshot_sha(key) if self.verify else None   # outside the timed capture
        while len(self._snaps) > self.max_voices:
            self._snaps.popitem(last=False)
            self._stats["evictions"] += 1
        return None

    def record_built_sha(self, key):
        self._snaps[key]["sha"] = self.snapshot_sha(key)


def is_enabled(backend):
    return "_prime_decoder" in vars(backend) and getattr(backend, _ATTR, None) is not None


def enable_decoder_snapshot(backend, max_voices=8, verify=False):
    """Opt in. Returns the cache (idempotent: a second call returns the existing cache)."""
    cur = getattr(backend, _ATTR, None)
    if cur is not None and "_prime_decoder" in vars(backend):
        return cur
    cache = DecoderSnapshotCache(backend, max_voices=max_voices, verify=verify)
    setattr(backend, _ATTR, cache)
    backend._prime_decoder = cache.prime                     # instance attribute shadows the class method
    orig_close = type(backend).close

    def close():
        disable_decoder_snapshot(backend)
        return orig_close(backend)
    backend.close = close
    return cache


def disable_decoder_snapshot(backend):
    cache = getattr(backend, _ATTR, None)
    if cache is not None:
        cache.invalidate()
    for a in ("_prime_decoder", "close", _ATTR):
        if a in vars(backend):
            delattr(backend, a)
