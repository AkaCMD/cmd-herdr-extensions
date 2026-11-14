"""Install a managed pointer to Herdr Todo guidance in native agent instruction files."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil

START = '<!-- herdr-agent-todos -->'
END = '<!-- /herdr-agent-todos -->'


def instruction_targets(home, environ):
    codex = Path(environ.get('CODEX_HOME') or home / '.codex')
    codex_file = codex / ('AGENTS.override.md' if (codex / 'AGENTS.override.md').is_file() else 'AGENTS.md')
    pi = Path(environ.get('PI_CODING_AGENT_DIR') or home / '.pi/agent')
    config = Path(environ.get('XDG_CONFIG_HOME') or home / '.config')
    claude = Path(environ.get('CLAUDE_CONFIG_DIR') or home / '.claude')
    candidates = [codex_file, claude / 'CLAUDE.md', pi / 'AGENTS.md', config / 'opencode/AGENTS.md']
    return [p for p in candidates if p.parent.is_dir()]


def updated_text(text, guidance, remove=False):
    if text.count(START) != text.count(END) or text.count(START) > 1:
        raise ValueError('Invalid Herdr Todo instruction markers')
    block = '' if remove else f'{START}\nWhen HC_TERMINAL_ID is set, read and follow {json.dumps(str(guidance), ensure_ascii=False)} for native Todo / Plan updates.\n{END}'
    if START in text:
        start = text.index(START)
        end = text.index(END, start) + len(END)
        return text[:start] + block + text[end:]
    if remove: return text
    return text + ('\n\n' if text and not text.endswith('\n\n') else '') + block + '\n'


def install(app_home, home=None, environ=None, apply=False, remove=False):
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    app_home = Path(app_home).resolve()
    guidance = app_home / 'mods/agent-todos/instructions.md'
    if not remove and not guidance.is_file(): raise ValueError(f'Missing guidance file: {guidance}')
    edits = []
    for target in instruction_targets(home, environ):
        before = target.read_text(encoding='utf-8') if target.is_file() else ''
        after = updated_text(before, guidance, remove)
        if before != after: edits.append((target, before, after))
    backup = app_home / 'data/agent-todos/instruction-backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    if apply:
        for index, (target, before, after) in enumerate(edits):
            if target.exists():
                backup.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup / f'{index}-{target.name}')
        for target, before, after in edits:
            current = target.read_text(encoding='utf-8') if target.is_file() else ''
            if current != before: raise ValueError(f'Instruction file changed during installation: {target}')
            temporary = target.with_name(target.name + '.herdr-todo-tmp')
            temporary.write_text(after, encoding='utf-8')
            temporary.replace(target)
    return [str(target) for target, _, _ in edits]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-home', type=Path, default=Path(os.environ.get('HERDR_APP_HOME') or Path.home() / '.config/herdr-app'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--remove', action='store_true')
    args = parser.parse_args()
    paths = install(args.app_home, apply=args.apply, remove=args.remove)
    print(json.dumps({'applied': args.apply, 'files': paths}, ensure_ascii=False))


if __name__ == '__main__': main()
