"""Exercise the real Herdr mod command loop in an isolated window with synthetic agents."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from pause_smoke import AGENT, until

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'plugins/herdr-personal-dispatch/skills/dispatch/scripts/dispatch.py'
spec = importlib.util.spec_from_file_location('dispatch', SCRIPT)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shot-dir', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='pd-controls-') as scratch:
        base = Path(scratch)
        previous = dict(os.environ)
        for key in list(os.environ):
            if key.startswith('HC_'):
                del os.environ[key]
        for name in ('run', 'config', 'state', 'work', 'app'):
            (base / name).mkdir(mode=0o700)
        os.environ.update(HC_SESSION='controls-test', XDG_RUNTIME_DIR=str(base / 'run'),
                          XDG_CONFIG_HOME=str(base / 'config'), XDG_STATE_HOME=str(base / 'state'),
                          PERSONAL_DISPATCH_HOME=str(base / 'data'), HC_SETTLE_MS='0',
                          HERDR_APP_HOME=str(base / 'app'), HERDR_TELEMETRY='0')
        # The compositor still lives in the original runtime directory.
        if previous.get('WAYLAND_DISPLAY'):
            display = Path(previous['WAYLAND_DISPLAY'])
            if not display.is_absolute():
                display = Path(previous.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))) / display
            os.environ['WAYLAND_DISPLAY'] = str(display)
        hc = d.Herdr()
        store, window = None, None
        app = base / 'app'

        def app_call(*argv, check=True):
            result = subprocess.run(['herdr-app', *argv], capture_output=True, text=True, timeout=15)
            if check and result.returncode:
                raise AssertionError(result.stderr or result.stdout)
            return result

        def status():
            result = app_call('status', check=False)
            return json.loads(result.stdout) if result.returncode == 0 else None

        try:
            fake = base / 'agent.py'
            fake.write_text(AGENT)
            lead = hc.call('run', '--bg', '--no-parent', '--json', sys.executable, str(fake))
            address = lead['address']
            os.environ['HC_TERMINAL_ID'] = address
            hc.call('label', address, 'herdr.space=fixture', 'herdr.tab=fixture', '--json')
            until(lambda: d.status_word(hc.get(address)) == 'working', 'controller startup')
            d.executable = lambda kind: hc.exe
            d.agent_argv = lambda *a, **k: [sys.executable, str(fake)]
            d.name_app_tab = lambda *a: None
            store = d.Store()
            r = d.init_run(store, hc, base / 'work', {'objective': 'synthetic window smoke', 'jobs': [
                {'id': 'one', 'task': 'synthetic', 'writes': [], 'acceptance': ['synthetic fixture']}
            ]}, 'codex', 1)
            d.sweep(store, hc, r['id'])
            original = store.run(r['id'])
            worker = store.job(r['id'], 'one')['address']
            until(lambda: d.status_word(hc.get(worker)) == 'working', 'worker startup')
            shutil.copytree(ROOT / 'mods/dispatch-control', app / 'mods/dispatch-control',
                            ignore=shutil.ignore_patterns('__pycache__', '*.d.ts', '*.error', 'jsconfig.json'))
            (app / 'layouts').mkdir()
            layout = {'id': 'root', 'type': 'column', 'children': [
                {'id': 'main', 'type': 'block', 'content': {'terminal': address}, 'size': {'weight': 1}},
                {'id': 'controls', 'type': 'block', 'size': {'px': 0}, 'content': {'mod': 'dispatch-control',
                 'props': {'python': sys.executable, 'script': str(SCRIPT)}}}
            ]}
            (app / 'layouts/smoke.json').write_text(json.dumps(layout))
            (app / 'shared.json').write_text(json.dumps({'frame.welcomed': 'finished', 'layout': 'smoke'}))
            (app / 'keys.json').write_text(json.dumps({'layout:smoke': {
                'escape': 'dispatch-control.stop-escape'}}))
            log = (base / 'window.log').open('w')
            window = subprocess.Popen(['herdr-app'], stdout=log, stderr=subprocess.STDOUT)
            until(lambda: status(), 'isolated Herdr window startup', timeout=20)
            app_call('do', 'layout.switch', '--name', 'smoke')
            app_call('do', 'pane.focus', '--terminal', address)
            until(lambda: status().get('layout') == 'smoke', 'custom layout')
            app_call('do', 'dispatch-control.stop-escape')
            until(lambda: store.run(r['id'])['status'] == 'paused', 'real mod stop command', timeout=25)
            assert d.status_word(hc.get(worker)) != 'working'
            assert not status()['errors'], status()['errors']
            if args.shot_dir:
                args.shot_dir.mkdir(parents=True, exist_ok=True)
                time.sleep(2)
                app_call('shot', str(args.shot_dir / 'dispatch-paused.png'))
            print('PASS: real mod command pauses controller and worker with no render errors', flush=True)
            # Exercise the same action as the rendered resume button through a dedicated mod command.
            turn_before_resume = hc.get(address).get('turn', 0)
            d.control(store, hc, address, 'resume')
            until(lambda: store.run(r['id'])['status'] == 'active', 'real mod resume command', timeout=25)
            restored = store.run(r['id'])
            assert restored['watcher'] != original['watcher']
            assert hc.get(restored['watcher'])['state'] == 'live'
            assert store.job(r['id'], 'one')['address'] == worker
            until(lambda: hc.get(address).get('turn', 0) > turn_before_resume, 'controller receives resume supervision')
            assert not status()['errors'], status()['errors']
            if args.shot_dir:
                time.sleep(2)
                app_call('shot', str(args.shot_dir / 'dispatch-resumed.png'))
            print('PASS: explicit resume restores original session and restarts watcher with no render errors', flush=True)
        finally:
            if window:
                app_call('do', 'window.close', check=False)
                try:
                    window.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    window.terminate()
                    window.wait(timeout=5)
            subprocess.run([hc.exe, 'server', 'stop'], capture_output=True, timeout=20)
            if store:
                store.db.close()
            os.environ.clear()
            os.environ.update(previous)


if __name__ == '__main__':
    main()
