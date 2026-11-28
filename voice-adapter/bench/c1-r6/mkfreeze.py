"""Write FREEZE.txt (sha256 of every frozen input). Run once, before the scored replay."""
import hashlib
import pathlib
root = pathlib.Path(__file__).resolve().parent
names = ['PREREG.md', 'c1_r6.py', 'fixtures.json', 'author_merged.json'] + \
    sorted(f'author/{p.name}' for p in (root / 'author').iterdir()) + \
    sorted(f'vendor/{p.name}' for p in (root / 'vendor').glob('*.py'))
with (root / 'FREEZE.txt').open('x') as f:
    for n in names:
        f.write(f'{hashlib.sha256((root / n).read_bytes()).hexdigest()}  {n}\n')
