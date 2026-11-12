"""Write a local Herdr plan when this Codex session has no update_plan tool.

Reads {"plan": [{"step": "...", "status": "pending"}], "explanation": "..."}
from stdin. Session identity comes from Codex's inherited environment, never
from the most recently modified rollout. No Codex logs are changed.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid


def write_plan(payload, app_home=None, environ=None):
    env = os.environ if environ is None else environ
    terminal = env.get('HC_TERMINAL_ID', '')
    session = env.get('CODEX_THREAD_ID') or env.get('CODEX_SESSION_ID', '')
    if not terminal or '/' in terminal or '\\' in terminal or not terminal.isalnum():
        raise ValueError('Run inside a local Herdr Codex terminal (HC_TERMINAL_ID).')
    try:
        session = str(uuid.UUID(session))
    except (ValueError, AttributeError):
        raise ValueError('Missing or invalid CODEX_THREAD_ID / CODEX_SESSION_ID.')
    if not isinstance(payload, dict) or not isinstance(payload.get('plan'), list):
        raise ValueError('Expected an object with a plan array.')
    plan = payload['plan']
    for item in plan:
        if not isinstance(item, dict) or not isinstance(item.get('step'), str) or not item['step'].strip():
            raise ValueError('Each plan item needs a non-empty step string.')
        if item.get('status') not in ('pending', 'in_progress', 'completed'):
            raise ValueError('Status must be pending, in_progress, or completed.')
    if sum(item['status'] == 'in_progress' for item in plan) > 1:
        raise ValueError('At most one plan item can be in_progress.')
    explanation = payload.get('explanation', '')
    if not isinstance(explanation, str):
        raise ValueError('Explanation must be a string.')
    app = Path(app_home or env.get('HERDR_APP_HOME') or Path.home() / '.config/herdr-app')
    target = app / 'data/agent-todos/codex' / (session + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {'sessionId': session, 'terminal': terminal, 'updatedAt': time.time(),
                'plan': [{'step': x['step'], 'status': x['status']} for x in plan],
                'explanation': explanation}
    temporary = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-home', type=Path)
    args = parser.parse_args()
    try:
        result = write_plan(json.load(sys.stdin), args.app_home)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
