"""Verified session text baselines and reversible review operations."""
import difflib
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from structured_changes import restore_text_baseline


def digest(data):
    return hashlib.sha256(data).hexdigest()


def review(file, record, session, app_home=None):
    if file.get('structured') or file.get('asset') or not record:
        return
    path = Path(file['path'])
    if path.is_symlink() or not path.is_file():
        return
    current = path.read_bytes()
    text = current.decode('utf-8')
    if '\0' in text:
        return
    root = Path(app_home or os.environ.get('HERDR_APP_HOME', Path.home() / '.config/herdr-app'))
    folder = root / 'data/session-review' / digest(session.encode())
    folder.mkdir(parents=True, exist_ok=True)
    baseline = folder / (digest(str(path).encode()) + '.json')
    if baseline.exists():
        saved = json.loads(baseline.read_text())
        before = saved['before']
    else:
        before = None if record['created'] else restore_text_baseline(current, record['ops']).decode('utf-8')
        if before is not None:
            newline = '\r\n' if '\r\n' in text else '\n'
            before = before.replace('\r\n', '\n').replace('\n', newline)
            if text.endswith('\n') and not before.endswith('\n'):
                before += newline
        saved = {'path': str(path), 'before': before}
        baseline.write_text(json.dumps(saved), encoding='utf-8')
    old = (before or '').splitlines(keepends=True)
    new = text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    blocks = []
    for group in matcher.get_grouped_opcodes(3):
        start, end = group[0][3], group[-1][4]
        replacement = ''.join(old[group[0][1]:group[-1][2]])
        blocks.append({'start': start, 'end': end, 'before': replacement})
    def rows(context):
        result = []
        for line in difflib.unified_diff(old, new, n=context):
            result.append(line.rstrip('\r\n'))
        from session_changes import unified_rows
        return unified_rows('\n'.join(result))
    file.update(rows=rows(3), full_rows=rows(max(len(old), len(new))), cut=False,
                add=sum(b[4]-b[3] for b in matcher.get_opcodes() if b[0] in ('insert', 'replace')),
                **{'del': sum(b[2]-b[1] for b in matcher.get_opcodes() if b[0] in ('delete', 'replace'))},
                review={'baseline': str(baseline), 'hash': digest(current), 'blocks': blocks})


def undo(payload):
    meta = payload['review']
    saved = json.loads(Path(meta['baseline']).read_text(encoding='utf-8'))
    path = Path(saved['path'])
    if path.is_symlink():
        raise ValueError('Cannot undo a symbolic link')
    current = path.read_bytes()
    if digest(current) != meta['hash']:
        raise ValueError('文件已更新，请刷新后重试')
    block = payload.get('block')
    if block is None:
        result = saved['before']
    else:
        lines = current.decode('utf-8').splitlines(keepends=True)
        part = meta['blocks'][block]
        lines[part['start']:part['end']] = [part['before']]
        result = ''.join(lines)
    backup = Path(meta['baseline']).parent / (digest(current) + '.backup')
    backup.write_bytes(current)
    if result is None:
        path.unlink()
    else:
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as target:
                target.write(result.encode('utf-8'))
            os.chmod(temporary, path.stat().st_mode)
            if path.read_bytes() != current:
                raise ValueError('文件已更新，请刷新后重试')
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {'ok': True}


if __name__ == '__main__':
    try:
        print(json.dumps(undo(json.loads(sys.argv[1])), ensure_ascii=False))
    except (OSError, ValueError, KeyError, IndexError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
