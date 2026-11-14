import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

let elements = 0, deferred = false;
const element = () => {
  elements++;
  const proxy = new Proxy({}, { get: (_, key) => (...args) => {
    if (key === 'when' && args[0]) return args[1](proxy);
    if (key === 'on_click' && deferred) throw new Error('Handler registered during virtual rendering');
    return proxy;
  } });
  return proxy;
};
const source = readFileSync(new URL('./main.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace('export default class', 'globalThis.Panel = class');
const context = vm.createContext({ View: class {}, div: element, h_flex: element, v_flex: element,
  Scrollbar: { vertical: element }, theme: () => new Proxy({}, { get: () => '#ffffff' }),
  settings: () => ({}), props: () => ({}), agents: () => [],
  v_virtual_list: (_id, count, _height, _key, render) => {
    deferred = true;
    try { render({ start: 0, end: Math.min(20, count) }); } finally { deferred = false; }
    return element();
  }
});
vm.runInContext(source, context);
const panel = new context.Panel();
panel.followedAgent = () => ({ address: 'fixture' });
Object.assign(panel, { expanded: true, panelWidth: 1400, mode: 'auto', wrap: false, context: false,
  address: 'fixture', open: 'fixture.py', info: { agent: 'Fixture', session: 'fixture' },
  scroll: {}, files: [{ path: 'fixture.py', rel: 'fixture.py', add: 1500, del: 0,
    review: { blocks: [{}] }, rows: [['@', null, '@@ -0,0 +1,1500 @@'],
      ...Array.from({ length: 1500 }, (_, i) => ['+', i + 1, `const value = "line ${i}";`])] }] });
panel.render({});
console.log(`expanded diff creates ${elements} elements for 1500 lines`);
assert.ok(elements < 1000, 'Expanded diff must render only visible rows');
