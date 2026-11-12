"""Enable per-space project folders in a personal Herdr layout. Preview by default.

Usage: python scripts/enable_space_project.py [layout name] [--apply] [--herdr-home DIR]

Idempotent edits:
  * layouts/<name>.json: the Spaces block (mod `spaces`) uses `spaces-project`.
  * keys.json: section `layout:<name>` binds ctrl-shift-t to `tabs-session.new-tab`, so the
    keyboard shortcut starts the new tab in the project folder too. A different existing
    binding of that key in the section is left alone and reported.
The layout's tab bar must already be `tabs-session` (run enable_session_changes.py first).
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
KEY, ACTION = 'ctrl-shift-t', 'tabs-session.new-tab'


def walk(node):
    yield node
    for child in node.get('children', []):
        yield from walk(child)


def patch_layout(layout):
    nodes = list(walk(layout))
    if not any(n.get('content', {}).get('mod') == 'tabs-session' for n in nodes):
        raise ValueError('This layout does not use the tabs-session tab bar: run enable_session_changes.py first')
    notes = []
    for n in nodes:
        if n.get('content', {}).get('mod') == 'spaces':
            n['content']['mod'] = 'spaces-project'
            notes.append(f'block {n.get("id")}: spaces -> spaces-project')
    return notes


def patch_keys(keys, layout_name):
    section = keys.setdefault(f'layout:{layout_name}', {})
    current = section.get(KEY)
    if current == ACTION:
        return []
    if current is not None:
        print(f'Kept existing binding {KEY} = {current!r} in layout:{layout_name}; bind {ACTION!r} yourself if wanted.')
        return []
    section[KEY] = ACTION
    return [f'keys.json layout:{layout_name}: {KEY} -> {ACTION}']


def write(path, data, backup_root):
    backup = backup_root / path.parent.name / path.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    temporary = path.with_name(path.name + '.install-tmp')
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temporary.replace(path)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('layout', nargs='?', default='agent-progress')
    parser.add_argument('--herdr-home', type=Path, default=Path(os.environ.get('HERDR_APP_HOME', Path.home() / '.config/herdr-app')))
    parser.add_argument('--apply', action='store_true', help='Write the files; default is preview only')
    args = parser.parse_args()
    if args.layout in SHIPPED:
        raise ValueError(f'"{args.layout}" is shipped by Herdr and replaced on start; clone it first')
    layout_path = args.herdr_home / 'layouts' / f'{args.layout}.json'
    keys_path = args.herdr_home / 'keys.json'
    layout = json.loads(layout_path.read_text(encoding='utf-8'))
    keys = json.loads(keys_path.read_text(encoding='utf-8')) if keys_path.is_file() else None
    layout_notes = patch_layout(layout)
    keys_notes = patch_keys(keys, args.layout) if keys is not None else []
    if keys is None:
        print('No keys.json: the shortcut keeps its default (new tabs follow the focused terminal).')
    for note in layout_notes + keys_notes:
        print(note)
    if not layout_notes and not keys_notes:
        print('Already enabled. No files changed.')
        return 0
    if not args.apply:
        print('Preview: no files changed.')
        return 0
    backup_root = ROOT / '.local/backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    if layout_notes:
        print(f'Updated {layout_path}\nBackup: {write(layout_path, layout, backup_root)}')
    if keys_notes:
        print(f'Updated {keys_path}\nBackup: {write(keys_path, keys, backup_root)}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as e:
        print(f'Failed: {e}', file=sys.stderr)
        sys.exit(1)
