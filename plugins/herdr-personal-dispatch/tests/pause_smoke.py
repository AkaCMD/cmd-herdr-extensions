"""Real hc terminals with synthetic agents; isolated core, no provider calls or user sessions."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/dispatch/scripts/dispatch.py'
spec = importlib.util.spec_from_file_location('dispatch', SCRIPT)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

AGENT = r'''
import os, sys, time
if os.name != 'nt':
    import tty
    tty.setraw(sys.stdin.fileno())
def status(word):
    sys.stdout.write('\033]7501;state=' + word + ':app=codex\033\\')
    sys.stdout.flush()
status('working')
while True:
    b = os.read(sys.stdin.fileno(), 1)
    if not b:
        break
    if b in (b'\x1b', b'\x03'):
        status('idle')
    elif b == b'\r':
        status('working')
        time.sleep(.02)
        status('done')
'''


def until(fn, label, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(.1)
    raise AssertionError(label)


def main():
    with tempfile.TemporaryDirectory(prefix='pd-stop-') as scratch:
        base = Path(scratch)
        old = dict(os.environ)
        for key in list(os.environ):
            if key.startswith('HC_'):
                del os.environ[key]
        for name in ('run', 'config', 'state', 'work'):
            (base / name).mkdir(mode=0o700)
        os.environ.update(HC_SESSION='pause-test', XDG_RUNTIME_DIR=str(base / 'run'),
                          XDG_CONFIG_HOME=str(base / 'config'), XDG_STATE_HOME=str(base / 'state'),
                          PERSONAL_DISPATCH_HOME=str(base / 'data'), HC_SETTLE_MS='0')
        hc = d.Herdr()
        store = None
        try:
            fake = base / 'agent.py'
            fake.write_text(AGENT)
            lead = hc.call('run', '--bg', '--no-parent', '--json', sys.executable, str(fake))
            os.environ['HC_TERMINAL_ID'] = lead['address']
            until(lambda: d.status_word(hc.get(lead['address'])) == 'working', 'controller startup')
            d.executable = lambda kind: hc.exe
            d.agent_argv = lambda *a, **k: [sys.executable, str(fake)]
            d.name_app_tab = lambda *a: None
            store = d.Store()
            r = d.init_run(store, hc, base / 'work', {'objective': 'synthetic pause smoke', 'jobs': [
                {'id': 'one', 'task': 'synthetic', 'writes': [], 'acceptance': ['synthetic fixture']}
            ]}, 'codex', 1)
            d.sweep(store, hc, r['id'])
            before = store.run(r['id'])
            worker = store.job(r['id'], 'one')['address']
            until(lambda: d.status_word(hc.get(worker)) == 'working', 'worker startup')
            paused = d.control(store, hc, lead['address'], 'stop')
            assert paused['status'] == 'paused', paused['note']
            assert d.status_word(hc.get(worker)) != 'working'
            assert hc.get(before['watcher'])['state'] == 'finished'
            for _ in range(3):
                d.supervise(store, hc, r['id'])
            assert d.status_word(hc.get(lead['address'])) != 'working'
            print('PASS: stop pauses real terminals; repeated supervision does not wake controller')
            resumed = d.control(store, hc, lead['address'], 'resume')
            assert resumed['status'] == 'active'
            assert resumed['watcher'] != before['watcher']
            assert hc.get(resumed['watcher'])['state'] == 'live'
            assert store.job(r['id'], 'one')['address'] == worker
            print('PASS: resume starts a new watcher and reuses original worker')
            hc.call('kill', lead['address'], '--json')
            until(lambda: store.run(r['id'])['status'] == 'paused', 'watcher pauses after controller exit')
            assert store.run(r['id'])['restarts'] == 0
            print('PASS: real event watcher pauses after controller exit without replacing it')
        finally:
            subprocess.run([hc.exe, 'server', 'stop'], capture_output=True, timeout=20)
            if store:
                store.db.close()
            os.environ.clear()
            os.environ.update(old)


if __name__ == '__main__':
    main()
