"""Enable the dispatch Escape key in a personal layout; preview by default."""
import argparse
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys

SHIPPED = {'main', 'terminals', 'win98'}
BINDINGS = {'escape': 'dispatch-control.stop-escape'}


def walk(node):
    yield node
    for child in node.get('children', []):
        yield from walk(child)


def patch(layout, keys, name, python):
    layout, keys = deepcopy(layout), deepcopy(keys)
    if name in SHIPPED:
        raise ValueError('Clone the shipped layout before enabling dispatch controls')
    copies = [n for n in walk(layout) if n.get('content', {}).get('mod') == 'dispatch-control']
    if len(copies) > 1:
        raise ValueError('Keep exactly one dispatch-control block per layout')
    if not copies:
        if layout.get('type') != 'column':
            world_keys = ('cloned_from', 'name', 'terminal_padding', 'divider', 'titlebar', 'traffic_lights', 'preview', 'terminal_menu', 'theme')
            world = {key: layout.pop(key) for key in world_keys if key in layout}
            layout = {**world, 'id': 'dispatch-root', 'type': 'column', 'children': [layout]}
        layout.setdefault('children', []).append({'id': 'dispatch-controls', 'type': 'block', 'size': {'px': 0},
                                                  'content': {'mod': 'dispatch-control', 'props': {'python': python}}})
    for copy in walk(layout):
        if copy.get('content', {}).get('mod') == 'dispatch-control':
            copy['size'] = {'px': 0}
    section = keys.get('layout:' + name, {})
    if section.get('ctrl-c') == 'dispatch-control.stop-ctrl-c':
        del section['ctrl-c']
    for section in ('global', 'terminal', 'layout:' + name):
        for key, action in BINDINGS.items():
            existing = keys.get(section, {}).get(key)
            if existing is not None and existing != action:
                raise ValueError(f'Existing binding {section}/{key}: {existing}; review it before replacing')
    keys.setdefault('layout:' + name, {}).update(BINDINGS)
    return layout, keys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('layout', nargs='?', default='agent-progress')
    parser.add_argument('--herdr-home', type=Path, default=Path(os.environ.get('HERDR_APP_HOME', Path.home() / '.config/herdr-app')))
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if Path(args.layout).name != args.layout:
        raise ValueError('Expected a layout name')
    layout_path = args.herdr_home / 'layouts' / (args.layout + '.json')
    keys_path = args.herdr_home / 'keys.json'
    layout = json.loads(layout_path.read_text(encoding='utf-8'))
    keys = json.loads(keys_path.read_text(encoding='utf-8')) if keys_path.exists() else {}
    updated_layout, updated_keys = patch(layout, keys, args.layout, args.python)
    changes = [(layout_path, updated_layout), (keys_path, updated_keys)]
    changes = [(path, doc) for path, doc in changes if doc != (layout if path == layout_path else keys)]
    for path, _ in changes:
        print(path)
    if not args.apply:
        print(f'Preview: {len(changes)} file(s). Escape goes through dispatch-control in this layout; no toolbar is added.')
        return 0
    backup = args.herdr_home / 'data/dispatch-control-backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    for path, _ in changes:
        if path.exists():
            saved = backup / path.relative_to(args.herdr_home)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
    for path, doc in changes:
        temporary = path.with_suffix('.dispatch-tmp')
        temporary.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
    print(f'Enabled in {args.layout}; backups: {backup}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
