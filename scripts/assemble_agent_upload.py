#!/usr/bin/env python3
"""Assemble README.md and main.py from .agent_upload/*.part* chunks."""
from pathlib import Path

def join(prefix: str, dest: Path) -> None:
    parts = sorted(Path('.agent_upload').glob(prefix + '.part*'))
    if not parts:
        raise SystemExit(f'no parts for {prefix}')
    data = ''.join(p.read_text(encoding='utf-8') for p in parts)
    dest.write_text(data, encoding='utf-8')
    print(f'wrote {dest} ({len(data.encode())} bytes) from {len(parts)} parts')

if __name__ == '__main__':
    join('README.md', Path('README.md'))
    join('main.py', Path('main.py'))
