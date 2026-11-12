"""Install only manifest-listed sources. Preview by default, explicit replacement with backups."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def plan(extension, herdr_home, pi_home):
    manifest = json.loads((ROOT / 'extensions.json').read_text(encoding='utf-8'))
    files = manifest[extension]['files']
    pairs = []
    for kind, entries in files.items():
        if kind == 'herdr':
            target_root, prefix = herdr_home, None
        elif kind == 'pi':
            target_root, prefix = pi_home / 'extensions', 'pi/extensions'
        elif kind == 'plugin':
            target_root, prefix = herdr_home / 'plugins', 'plugins'
        elif kind == 'bridge':
            target_root, prefix = herdr_home / 'data/codex-bridge', 'bridge'
        else:
            raise ValueError(f'Unknown file kind: {kind}')
        for entry in entries:
            source = (ROOT / entry).resolve()
            if not source.is_relative_to(ROOT) or not source.is_file():
                raise ValueError(f'Invalid source: {entry}')
            relative = Path(entry) if prefix is None else Path(entry).relative_to(prefix)
            target = (target_root / relative).resolve()
            if not target.is_relative_to(target_root.resolve()): raise ValueError(f'Invalid target: {target}')
            pairs.append((kind, source, target))
    return pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('extension', choices=list(json.loads((ROOT / 'extensions.json').read_text(encoding='utf-8'))))
    parser.add_argument('--herdr-home', type=Path, default=Path(os.environ.get('HERDR_APP_HOME', Path.home() / '.config/herdr-app')))
    parser.add_argument('--pi-home', type=Path, default=Path.home() / '.pi/agent')
    parser.add_argument('--apply', action='store_true', help='Copy files; default is preview only')
    parser.add_argument('--replace', action='store_true', help='Back up and replace conflicting files')
    args = parser.parse_args()
    pairs = plan(args.extension, args.herdr_home, args.pi_home)
    changed = [(kind, src, dst) for kind, src, dst in pairs if not dst.is_file() or src.read_bytes() != dst.read_bytes()]
    conflicts = [dst for _, _, dst in changed if dst.exists()]
    for _, src, dst in changed: print(f'{src.relative_to(ROOT)} -> {dst}')
    if not args.apply:
        print(f'Preview: {len(changed)} file(s), {len(conflicts)} conflict(s). No files changed.')
        return 0
    if conflicts and not args.replace:
        print('Existing files differ. Review them, then use --replace to back up and replace.', file=sys.stderr)
        return 1
    backup = ROOT / '.local/backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    # Finish all backups before replacing any file.
    for kind, src, dst in changed:
        if dst.exists():
            if not dst.is_file(): raise ValueError(f'Not a file: {dst}')
            saved = backup / kind / src.relative_to(ROOT)
            saved.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(dst, saved)
    for _, src, dst in changed:
        dst.parent.mkdir(parents=True, exist_ok=True)
        temporary = dst.with_name(dst.name + '.install-tmp')
        shutil.copyfile(src, temporary); temporary.replace(dst)
    print(f'Installed {len(changed)} file(s).')
    if conflicts: print(f'Backups: {backup}')
    if args.extension == 'agent-todos':
        print('Install psutil separately (see README), run herdr-app check, then /reload in Pi.')
    elif args.extension == 'session-changes':
        print('Add the panel to a layout: python scripts/enable_session_changes.py <layout> --apply, then herdr-app check.')
        print('The tab bar (mods/tabs-session) also opens the space-project menus: install space-project too.')
    elif args.extension == 'space-project':
        print('Needs the tabs-session tab bar (session-changes). Enable: python scripts/enable_space_project.py <layout> --apply, then herdr-app check.')
    elif args.extension == 'space-permissions':
        print('Add the launcher hook: python scripts/enable_permissions.py --apply, then herdr-app check the three mods.')
    elif args.extension == 'personal-dispatch':
        plugin = (args.herdr_home / 'plugins/herdr-personal-dispatch/install.py').resolve()
        print(f'Register with Codex and Claude: python "{plugin}" (or --host codex / --host claude).')
        print('Then open a new agent session in Herdr App. This skill plugin has no JavaScript mod to check.')
    print('Layout selection, keys, credentials and session data were not changed.')
    return 0


if __name__ == '__main__':
    try: sys.exit(main())
    except (OSError, ValueError, KeyError) as e:
        print(f'Install failed: {e}', file=sys.stderr); sys.exit(1)
