#!/usr/bin/env python3
"""Write fixtures/od_slice3_spec150_rows.json: SPEC150 rows (fake stages, virtual time) from the CURRENT
od_slice_bench.run_arm_turn. Run once at the OD-slice-3 commit (1d0e0e5) to freeze the golden rows that
test_od_slice_bench.EarlyCommit checks the OD-slice-4 spec150 / spec150b path against (byte-identical JSON).
Usage (from voice-adapter/): python bench/make_spec150_golden.py"""
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import test_od_slice_bench as t  # noqa: E402

out = HERE.parent / 'fixtures' / 'od_slice3_spec150_rows.json'
out.write_text(json.dumps({name: t.golden_spec_row(name) for name in t.GOLDEN_CASES}, indent=1) + '\n')
print(out.name, len(t.GOLDEN_CASES))
