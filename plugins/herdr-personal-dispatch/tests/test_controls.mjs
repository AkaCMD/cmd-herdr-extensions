import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../../mods/dispatch-control/main.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/export default class /, 'class ').replace(/export /g, '');
let focus = { terminal: 'lead' };
const lead = { address: 'lead', labels: { 'dispatch.controller': 'run', 'dispatch.home': '/synthetic/custom data' } };
const worker = { address: 'worker', labels: { 'dispatch.run': 'run' } };
const shell = { address: 'shell', labels: {} };
const calls = [];
let release;
const context = vm.createContext({
  View: class {}, focused: () => focus, terminals: () => [lead, worker, shell],
  props: () => ({ python: '/python', script: '/dispatch.py' }), appHome: () => '/app',
  exec: async (...args) => { calls.push(args); if (release) await new Promise(resolve => { release = resolve; }); return { code: 0, stdout: '{"status":"paused"}' }; },
  hc: async args => { calls.push(args); return { code: 0 }; },
});
vm.runInContext(source + '\nglobalThis.api = { target, stop };', context);
const api = context.api;
release = true;
const stopping = api.stop(api.target());
focus = { terminal: 'shell' };
release();
await stopping;
assert.equal(calls[0][1][3], 'lead', 'focus switch must not redirect stop');
assert.equal(calls[0][1].at(-1), '/synthetic/custom data');
assert.equal(calls[0][1][7], 'esc');
calls.length = 0;
release = false;
await api.stop(api.target());
assert.deepEqual(Array.from(calls[0]), ['keys', 'shell', 'esc', '--json']);
console.log('PASS: captured Escape target, custom data home and unmanaged-terminal forwarding');
