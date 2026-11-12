"""Add the Session panel to a personal Herdr layout. Preview by default.

Usage: python scripts/enable_session_changes.py [layout name] [--apply] [--herdr-home DIR]

Two edits, both idempotent: the tab bar block (id "tabs") uses `tabs-session`, and the
right panel stack (id "panel-body") gets a `session-changes` child next to Files and Changes.
Shipped layouts (main, terminals, win98) are refused: clone one first (layout.clone).
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = {'main', 'terminals', 'win98'}


def walk(node):
    yield node
    for child in node.get('children', []):
        yield from walk(child)


def patch(layout):
    notes = []
    nodes = list(walk(layout))
    for n in nodes:
        if n.get('id') == 'tabs' and n.get('content', {}).get('mod') == 'tabs':
            n['content']['mod'] = 'tabs-session'
            notes.append('tab bar: tabs -> tabs-session')
    body = next((n for n in nodes if n.get('id') == 'panel-body' and n.get('type') == 'stack'), None)
    if body is None:
        raise ValueError('No "panel-body" stack: this layout has no Files/Changes panel to join')
    if not any(c.get('content', {}).get('mod') == 'session-changes' for c in body['children']):
        body['children'].append({'id': 'session-changes', 'type': 'block', 'content': {'mod': 'session-changes'}})
        notes.append('panel-body: + session-changes')
    # A copy Herdr docked by itself elsewhere (first sight of the mod folder) is dropped.
    for parent in nodes:
        for child in list(parent.get('children', [])):
            if child is not body and child.get('content', {}).get('mod') == 'session-changes' and parent is not body:
                parent['children'].remove(child)
                notes.append(f'removed auto-docked block {child.get("id")}')
    return notes


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('layout', nargs='?', default='agent-progress')
    parser.add_argument('--herdr-home', type=Path, default=Path(os.environ.get('HERDR_APP_HOME', Path.home() / '.config/herdr-app')))
    parser.add_argument('--apply', action='store_true', help='Write the layout; default is preview only')
    args = parser.parse_args()
    if args.layout in SHIPPED:
        raise ValueError(f'"{args.layout}" is shipped by Herdr and replaced on start; clone it first')
    path = args.herdr_home / 'layouts' / f'{args.layout}.json'
    layout = json.loads(path.read_text(encoding='utf-8'))
    notes = patch(layout)
    for note in notes:
        print(note)
    if not notes:
        print('Already enabled. No files changed.')
        return 0
    if not args.apply:
        print(f'Preview: {path} would change. No files changed.')
        return 0
    backup = ROOT / '.local/backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f') / 'layouts' / path.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    temporary = path.with_name(path.name + '.install-tmp')
    temporary.write_text(json.dumps(layout, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temporary.replace(path)
    print(f'Updated {path}\nBackup: {backup}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as e:
        print(f'Failed: {e}', file=sys.stderr)
        sys.exit(1)
