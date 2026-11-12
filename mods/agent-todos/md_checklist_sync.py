"""Mirror a Markdown checklist into Claude Code's native task store so the agent-todos panel can show it.

For Claude Code sessions that have no TodoWrite/TaskCreate tool (the panel only reads those records), this writes
``~/.claude/tasks/<session-id>/<n>.json`` -- the same files ``collector.py`` already reads.

Checklist markers:  ``- [x]`` completed, ``- [~]`` in_progress, ``- [ ]`` pending.

Safety: files written here carry ``"_source": "md-checklist-sync"``. Only those are replaced. If the task directory
holds any other file (real native tasks), nothing is touched and the command fails.
"""
import argparse
import json
import re
import sys
from pathlib import Path

MARK = 'md-checklist-sync'
STATUS = {'x': 'completed', 'X': 'completed', '~': 'in_progress', ' ': 'pending'}


def parse(text):
    items = []
    for line in text.splitlines():
        m = re.match(r'\s*[-*] \[([xX~ ])\] (.+)', line)
        if m:
            items.append((STATUS[m.group(1)], m.group(2).strip()))
    return items


def task_dir(session, root):
    if not re.fullmatch(r'[\w-]+', session):
        raise ValueError(f'Invalid session id: {session!r}')
    return (root / 'tasks' / session)


def build(items):
    tasks = []
    for i, (status, text) in enumerate(items, 1):
        subject = text if len(text) <= 60 else text[:58] + '…'
        task = {'id': str(i), 'subject': subject, 'description': text, 'status': status,
                'blocks': [], 'blockedBy': [], '_source': MARK}
        if status == 'in_progress':
            task['activeForm'] = '正在：' + subject
        tasks.append(task)
    return tasks


def sync(markdown, session, claude_home):
    folder = task_dir(session, claude_home)
    folder.mkdir(parents=True, exist_ok=True)
    existing = list(folder.glob('*.json'))
    foreign = []
    for path in existing:
        try:
            if json.loads(path.read_text(encoding='utf-8')).get('_source') != MARK: foreign.append(path)
        except (OSError, ValueError):
            foreign.append(path)
    if foreign:
        raise RuntimeError(f'{folder} contains {len(foreign)} file(s) not written by this tool; refusing to touch it.')
    tasks = build(parse(markdown))
    for path in existing: path.unlink()
    for task in tasks:
        (folder / f"{task['id']}.json").write_text(json.dumps(task, ensure_ascii=False), encoding='utf-8')
    counts = {s: sum(1 for t in tasks if t['status'] == s) for s in ('completed', 'in_progress', 'pending')}
    return folder, len(tasks), counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('checklist', type=Path, help='Markdown file containing the checklist')
    parser.add_argument('--session', required=True, help='Claude Code session id (shown as "session" by collector.py)')
    parser.add_argument('--claude-home', type=Path, default=Path.home() / '.claude')
    args = parser.parse_args(argv)
    folder, count, counts = sync(args.checklist.read_text(encoding='utf-8'), args.session, args.claude_home)
    print(f'{count} task(s) -> {folder} {counts}')
    return 0


if __name__ == '__main__':
    try: sys.exit(main())
    except (OSError, ValueError, RuntimeError) as e:
        print(f'Sync failed: {e}', file=sys.stderr); sys.exit(1)
