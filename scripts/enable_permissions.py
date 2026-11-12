"""Enable per-space permissions for a launch bridge or native Herdr programs.

Two idempotent edits to `launch.py`; both are marked with
`herdr-agent-permissions` so a second run changes nothing:

  import permissions
  ...
  chosen = permissions.plan(agent, APP / 'data/agent-permissions.json')
  command, env = permissions.apply(agent, command, env, len(command) - len(args), chosen)

On Unix without that bridge, wrap the native agent entries in programs.json.
Preview by default. Install permissions.py first, then pass --apply to write.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys

MARK = 'herdr-agent-permissions'
IMPORT_LINE = 'import permissions  # ' + MARK
CALL = '    return subprocess.call(command, env=env)'
HOOK = '''    # {mark}: begin
    chosen = permissions.plan(agent, APP / 'data/agent-permissions.json')
    # Inserted before the person's own arguments, so they still win.
    command, env = permissions.apply(agent, command, env, len(command) - len(args), chosen)
    return subprocess.call(command, env=env)
    # {mark}: end
'''.format(mark=MARK)

ROOT = Path(__file__).resolve().parents[1]
PROGRAMS = {
    'codex': ('codex', ['codex', '--no-daemon']),
    'claude': ('claude', ['claude']),
    'dsh-tui': ('dsh', ['dsh-tui', '--backend', 'dsh']),
}


def patch(text):
    if MARK in text:
        return text, False
    lines = text.splitlines(keepends=True)
    if CALL + '\n' not in lines and CALL not in [line.rstrip('\n') for line in lines]:
        raise ValueError(f'Expected exactly one launch line: {CALL.strip()}')
    at = next(i for i, line in enumerate(lines) if line.rstrip('\n') == CALL)
    unique = [line for line in lines if line.rstrip('\n') == CALL]
    if len(unique) != 1:
        raise ValueError('The bridge has more than one launch line; patch it by hand.')
    if IMPORT_LINE not in text:
        anchors = [i for i, line in enumerate(lines) if line.startswith('import ') or line.startswith('from ')]
        if not anchors:
            raise ValueError('No import block found in the bridge.')
        lines.insert(anchors[-1] + 1, IMPORT_LINE + '\n')
        at += 1
    lines[at:at + 1] = [HOOK]
    return ''.join(lines), True


def patch_programs(document, module):
    if not isinstance(document, dict) or not isinstance(document.get('programs', {}), dict):
        raise ValueError('Expected a programs.json object with a programs map.')
    programs = dict(document.get('programs', {}))
    changed = []
    for key, (agent, default) in PROGRAMS.items():
        entry = programs.get(key, {})
        if not isinstance(entry, dict):
            continue
        command = entry.get('run', default)
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
            continue
        if len(command) >= 2 and command[1] == str(module):
            continue
        if not shutil.which(command[0]):
            continue
        wrapper = [sys.executable, str(module), agent, str(len(command))]
        programs[key] = {**entry, 'run': wrapper + command}
        changed.append(key)
    return {**document, 'programs': programs}, changed


def enable_native_programs(herdr_home, apply):
    module = herdr_home / 'data/codex-bridge/permissions.py'
    if not module.is_file():
        raise ValueError(f'Install the bridge module first: {module}')
    config = herdr_home / 'programs.json'
    document = json.loads(config.read_text(encoding='utf-8-sig')) if config.is_file() else {}
    updated, changed = patch_programs(document, module.resolve())
    if not changed:
        print(f'No native launcher changes: {config}')
        return 0
    print(f'{config}: enable per-space permissions for {", ".join(changed)}')
    if not apply:
        print('Preview only. No files changed.')
        return 0
    if config.is_file():
        backup = ROOT / '.local/backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f') / config.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config, backup)
        print(f'Backup: {backup}')
    temporary = config.with_name(config.name + '.permissions-tmp')
    temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if config.is_file():
        shutil.copymode(config, temporary)
    temporary.replace(config)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launch', type=Path, default=None, help='launch.py to patch')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    herdr_home = Path(os.environ.get('HERDR_APP_HOME', Path.home() / '.config/herdr-app'))
    launch = args.launch or herdr_home / 'data/codex-bridge/launch.py'
    if not args.launch and not launch.is_file() and os.name != 'nt':
        return enable_native_programs(herdr_home, args.apply)
    if not launch.is_file():
        raise ValueError(f'No launch bridge at {launch}')
    module = launch.parent / 'permissions.py'
    if not module.is_file():
        raise ValueError(f'Install the bridge module first: {module}')
    before = launch.read_text(encoding='utf-8')
    after, changed = patch(before)
    if not changed:
        print(f'Already hooked: {launch}')
        return 0
    print(f'{launch}: add the permission hook ({len(before.splitlines())} -> {len(after.splitlines())} lines)')
    if not args.apply:
        print('Preview only. No files changed.')
        return 0
    temporary = launch.with_name(launch.name + '.permissions-tmp')
    temporary.write_text(after, encoding='utf-8')
    temporary.replace(launch)
    print('Patched. New launches of Claude Code, Codex and dsh follow the space.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(f'Patch failed: {error}', file=sys.stderr)
        sys.exit(1)
