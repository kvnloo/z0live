#!/usr/bin/env python3
"""Bounded read-only GGUFv3 tensor-name audit (for source-pinned Phonon q8).

This is deliberately NOT an alternative full REA parser: only handles no
metadata key-value records, validates record boundaries, and records whether
names satisfy the <=64-byte GGUF tensor-name specification. No inference.
"""
import argparse
import json
from pathlib import Path
import struct


def analyze(path: Path) -> dict:
    with path.open("rb") as f:
        head = f.read(24)
        if len(head) != 24 or head[:4] != b"GGUF":
            raise ValueError("not GGUF header")
        version, n_tensors, n_metadata = struct.unpack("<IQQ", head[4:])
        if version != 3 or n_metadata != 0 or n_tensors > 1_000_000:
            raise ValueError("only GGUFv3 metadata-free bounded tensor directory supported")
        violations = []
        max_length = 0
        for i in range(n_tensors):
            raw = f.read(8)
            if len(raw) != 8:
                raise ValueError("truncated GGUF tensor length")
            length, = struct.unpack("<Q", raw)
            if length > 1024 * 1024:
                raise ValueError("tensor name length exceeds safety cap")
            if len(f.read(length)) != length:
                raise ValueError("truncated GGUF tensor name")
            max_length = max(length, max_length)
            if length > 64:
                violations.append({"index": i, "bytes": length})
            dims_raw = f.read(4)
            if len(dims_raw) != 4:
                raise ValueError("truncated dimension count")
            n_dims, = struct.unpack("<I", dims_raw)
            if n_dims > 8:
                raise ValueError("unsupported dimension count")
            trailer = f.read(n_dims * 8 + 4 + 8)
            if len(trailer) != n_dims * 8 + 12:
                raise ValueError("truncated tensor record")
    return {
        "schema": "z0live.s25.gguf-name-audit.v1",
        "version": version,
        "declared_tensors": n_tensors,
        "declared_metadata_entries": n_metadata,
        "max_tensor_name_bytes": max_length,
        "names_exceeding_64_bytes": violations[:20],
        "total_names_exceeding_64_bytes": len(violations),
        "status": "GGUF_NAME_LIMIT_VIOLATED" if violations else "NAMES_WITHIN_SPEC",
        "note": "Independent name-length check only; not REA parsing, output correctness or executable-model proof",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("model", type=Path)
    args = p.parse_args()
    print(json.dumps(analyze(args.model), indent=2))


if __name__ == "__main__":
    main()
