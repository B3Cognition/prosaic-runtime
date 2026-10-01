"""Tiny deterministic CLI. Deliberately not a requirements-quality benchmark."""
import argparse
import json
from pathlib import Path
import re
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='prosaic-example-analyzer 1.0')
    parser.add_argument('spec', type=Path)
    parser.add_argument('--json', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        with args.spec.open('rb') as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise ValueError('input too large')
        requirements = []
        vague = []
        for line in raw.decode('utf-8').splitlines():
            match = re.match(r'^(REQ-\d{3}):\s*(.*)$', line)
            if match:
                requirements.append(match[1])
                if re.search(r'\b(fast|easy|soon)\b', match[2], re.IGNORECASE):
                    vague.append(match[1])
        print(json.dumps({'requirements': len(requirements), 'vague_ids': vague,
                          'passed': bool(requirements) and not vague}))
        return 0  # A failed quality check is report data, not an operational failure.
    except (OSError, UnicodeError, ValueError):
        print('Cannot analyze the specification.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
