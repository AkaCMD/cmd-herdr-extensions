"""Per-space agent permission profiles, applied by the launch bridge.

A Herdr space has no permission setting of its own. A Herdr mod keeps the
person's choice in ``<herdr-home>/data/agent-permissions.json`` and this module
turns it into launch arguments for the agents the bridge starts.

The space is found from the terminal the bridge runs in: herdr-core exports
``HC_TERMINAL_ID``, and ``hc get <id>`` returns the terminal's labels,
including ``herdr.space``.

Profiles, from most to least restrained:

``default``  leave the agent alone (its own configuration decides)
``plan``     read-only: look and read, never write
``auto``     writable, no approval prompts
``yolo``     no sandbox, no approval prompts

Agents without an adaptation below are started unchanged and their space's
profile does nothing for them.
"""
import argparse
import atexit
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PROFILES = ('default', 'plan', 'auto', 'yolo')

# What each agent understands. Values are argv fragments; `dsh` is configured
# by a patch layer instead of flags, because its profile hardcodes the sandbox
# on Windows (danger-full-access, never) and flags cannot change that.
CLAUDE = {
    'plan': ['--permission-mode', 'plan'],
    'auto': ['--permission-mode', 'acceptEdits'],
    'yolo': ['--permission-mode', 'bypassPermissions'],
}
CODEX = {
    'plan': ['-s', 'read-only', '-a', 'never'],
    'auto': ['-s', 'workspace-write', '-a', 'never'],
    'yolo': ['--dangerously-bypass-approvals-and-sandbox'],
}
# The dsh profile already forces approval 'never' on Windows, where the TUI has
# no one to ask: keep that and vary the sandbox alone.
DSH = {
    'plan': ('read-only', 'never'),
    'auto': ('workspace-write', 'never'),
    'yolo': ('danger-full-access', 'never'),
}

_temporary = []


def _remove_temporary():
    while _temporary:
        path = _temporary.pop()
        try:
            os.unlink(path)
        except OSError:
            pass


atexit.register(_remove_temporary)


def load_policy(path):
    """The policy document, or an empty one when it is missing or invalid."""
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {'version': 1, 'spaces': {}}
    spaces = document.get('spaces') if isinstance(document, dict) else None
    return {'version': 1, 'spaces': spaces if isinstance(spaces, dict) else {}}


def space_of(environ=None, run=None):
    """The space the current terminal belongs to, or None outside Herdr."""
    environ = os.environ if environ is None else environ
    terminal = environ.get('HC_TERMINAL_ID') or ''
    if not terminal:
        return None
    hc = shutil.which(environ.get('HC_BIN', 'hc'))
    if not hc:
        return None
    run = subprocess.run if run is None else run
    try:
        finished = run([hc, 'get', terminal], capture_output=True, text=True, timeout=5)
        labels = json.loads(finished.stdout).get('labels') or {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    space = labels.get('herdr.space')
    return space if isinstance(space, str) and space else None


def setting(spaces, space):
    """The applied profile for a space, or None when nothing applies."""
    entry = spaces.get(space) if isinstance(spaces, dict) else None
    if not isinstance(entry, dict):
        return None
    profile = entry.get('profile')
    if entry.get('override') is not True or profile not in PROFILES or profile == 'default':
        return None
    return {'profile': profile}


def adapt(agent, profile):
    """How to start `agent` under `profile`, or None when it cannot be set."""
    if profile not in PROFILES or profile == 'default':
        return None
    if agent == 'claude' and profile in CLAUDE:
        return {'profile': profile, 'argv': list(CLAUDE[profile])}
    if agent == 'codex' and profile in CODEX:
        return {'profile': profile, 'argv': list(CODEX[profile])}
    if agent == 'dsh' and profile in DSH:
        sandbox, approval = DSH[profile]
        return {'profile': profile, 'patch': [
            {'id': 'sandbox-policy', 'config': {'mode': sandbox}},
            {'id': 'approval', 'config': {'policy': approval}},
        ]}
    return None


def plan(agent, policy_path, environ=None, run=None):
    """What to add to this launch, or None (no space, no policy, not adapted)."""
    spaces = load_policy(policy_path)['spaces']
    if not spaces:
        return None
    chosen = setting(spaces, space_of(environ, run))
    return adapt(agent, chosen['profile']) if chosen else None


def _patch_file(entries):
    handle, path = tempfile.mkstemp(prefix='herdr-permissions-', suffix='.yml')
    os.close(handle)
    _temporary.append(path)
    # A JSON list is a valid patch layer and needs no YAML writer.
    Path(path).write_text(json.dumps(entries, indent=2) + '\n', encoding='utf-8')
    return path


def apply(agent, command, environ, base_length, chosen):
    """Insert the profile's arguments before the person's own, in place.

    Arguments already given win over the profile: they follow the inserted
    ones. `base_length` is where the bridge's own command ends and the
    person's arguments begin.
    """
    if not chosen:
        return command, environ
    inserted = list(chosen.get('argv') or [])
    if chosen.get('patch'):
        inserted += ['--patch', _patch_file(chosen['patch'])]
    if not inserted:
        return command, environ
    return command[:base_length] + inserted + command[base_length:], environ


def native_launch(agent, command, base_length, policy_path):
    args = command[1:command.index('--')] if '--' in command else command[1:]
    options = {arg.split('=', 1)[0] for arg in args}
    explicit = {
        'codex': {'-s', '--sandbox', '-a', '--ask-for-approval', '--approve-for-me',
                  '--dangerously-bypass-approvals-and-sandbox', '--yolo'},
        'claude': {'--permission-mode', '--dangerously-skip-permissions'},
    }
    if options & explicit.get(agent, set()):
        return subprocess.call(command)
    chosen = plan(agent, policy_path)
    if agent == 'dsh':
        # dsh-tui reads host patches before the workspace and prompt arguments.
        base_length = next((index + 1 for index, arg in enumerate(command[:base_length])
                            if Path(arg).name in {'dsh-tui', 'dsh-tui.js', 'dst', 'dsh'}), base_length)
    elif '--' in command[:base_length]:
        base_length = command.index('--')
    command, environ = apply(agent, command, os.environ, base_length, chosen)
    return subprocess.call(command, env=environ)


def main():
    parser = argparse.ArgumentParser(description='Launch an agent with its Herdr space permission profile.')
    parser.add_argument('agent', choices=('codex', 'claude', 'dsh'))
    parser.add_argument('base_length', type=int)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 1 <= args.base_length <= len(args.command):
        parser.error('expected the original launcher and its arguments')
    return native_launch(args.agent, args.command, args.base_length,
                         Path(__file__).resolve().parents[1] / 'agent-permissions.json')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except OSError as error:
        print(f'Agent launch failed: {error}', file=sys.stderr)
        sys.exit(1)
