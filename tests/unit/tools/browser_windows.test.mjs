/** Window ownership without a real browser or external connections. */
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {test} from 'node:test';
import {TaskWindows} from '../../../resources/browser/windows.mjs';
import {BrowserOperations} from '../../../resources/browser/operations.mjs';
import {BrowserInputError} from '../../../resources/browser/actions.mjs';

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'north-window-unit-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const profile = join(root, 'Profile 3');
  await mkdir(profile);
  const pages = [];
  const options = [];
  const browser = {
    connected: true, disconnected: 0,
    async newPage(option) {
      options.push(option);
      const id = String(pages.length + 1);
      const page = {
        id, context: 'verified-profile-context', moved: false, closed: false, navigations: [],
        async goto(url) {this.navigations.push(url);},
        async $eval() {return profile;},
        async windowId() {return this.moved ? 'user-window' : id;},
        isClosed() {return this.closed;},
        async close() {this.closed = true;},
        on() {}, mainFrame() {return 'frame';},
        setDefaultTimeout() {},
        async createCDPSession() {
          const context = this.context;
          return {async send() {return {targetInfo: {targetId: id, browserContextId: context}};}, async detach() {}};
        },
      };
      pages.push(page);
      return page;
    },
    async pages() {return pages.filter(page => !page.closed);},
    target() {return {async createCDPSession() {
      return {async send(method) {
        if (method === 'Target.getTargets') return {targetInfos: []};
        if (method === 'Target.getBrowserContexts') return {browserContextIds: []};
        throw new Error('Unexpected browser metadata call');
      }, async detach() {}};
    }};},
    async disconnect() {this.disconnected += 1;},
  };
  const sdk = {async connect() {return browser;}};
  const windows = new TaskWindows(sdk, join(root, 'state'));
  const request = {task_id: 'one', profile_id: 'university', profile_config: 'version-one',
    endpoint: 'ws://localhost:9222/devtools/browser/test', expected_path: profile};
  return {windows, request, browser, pages, options, sdk, root};
}

test('one task reuses one window; different tasks get distinct windows', async t => {
  const f = await fixture(t);
  const first = await f.windows.acquire(f.request);
  const repeated = await f.windows.acquire(f.request);
  const second = await f.windows.acquire({...f.request, task_id: 'two'});
  assert.equal(first.page, repeated.page);
  assert.notEqual(first.ownership.window_id, second.ownership.window_id);
  assert.deepEqual(f.options, [{type: 'window', background: true}, {type: 'window', background: true}]);
});

test('a moved task tab is refused instead of following it into a user window', async t => {
  const f = await fixture(t);
  const entry = await f.windows.acquire(f.request);
  entry.page.moved = true;
  await assert.rejects(f.windows.acquire(f.request), /outside its owned window/);
  assert.equal(f.pages.length, 1);
});

test('a closed task tab is refused without creating another window', async t => {
  const f = await fixture(t);
  const entry = await f.windows.acquire(f.request);
  await entry.page.close();
  await assert.rejects(f.windows.acquire(f.request), /Task tab was closed/);
  assert.equal(f.pages.length, 1);
});

test('restart recovers the original task target without navigating its draft', async t => {
  const f = await fixture(t);
  const first = await f.windows.acquire(f.request);
  first.page.navigations.push('https://synthetic.example/draft');
  const restarted = new TaskWindows(f.sdk, join(f.root, 'state'));
  const restored = await restarted.acquire(f.request);
  assert.equal(restored.page, first.page);
  assert.equal(restored.page.navigations.at(-1), 'https://synthetic.example/draft');
  assert.equal(f.pages.length, 1);
});

test('profile or connection changes cannot reuse an old task window', async t => {
  const f = await fixture(t);
  await f.windows.acquire(f.request);
  await assert.rejects(f.windows.acquire({...f.request, profile_config: 'changed'}), /profile changed/);
});

test('interrupted window creation cannot automatically create a duplicate', async t => {
  const f = await fixture(t);
  const key = f.windows.key(f.request);
  const {createHash} = await import('node:crypto');
  const binding = createHash('sha256').update(JSON.stringify([
    f.request.endpoint, f.request.expected_path, f.request.profile_config,
  ])).digest('hex');
  await f.windows.saveOwnership(key, {binding, creating: true});
  await assert.rejects(f.windows.acquire(f.request), /creation was interrupted/);
  assert.equal(f.pages.length, 0);
});

test('close removes only its owned tab and disconnect never closes other work', async t => {
  const f = await fixture(t);
  const first = await f.windows.acquire(f.request);
  const other = await f.windows.acquire({...f.request, task_id: 'two'});
  await f.windows.closeWindow(first);
  await f.windows.disconnect();
  assert.ok(first.page.closed);
  assert.ok(!other.page.closed);
  assert.equal(f.browser.disconnected, 1);
});

test('wrong profile is closed without business navigation and can be retried safely', async t => {
  const f = await fixture(t);
  const wrong = join(f.root, 'Wrong profile');
  await mkdir(wrong);
  await assert.rejects(f.windows.acquire({...f.request, expected_path: wrong}), /Bring the requested profile window/);
  assert.ok(f.pages[0].closed);
  assert.deepEqual(f.pages[0].navigations, ['chrome://version']);
  const correct = await f.windows.acquire(f.request);
  assert.ok(!correct.page.closed);
});

test('native profile mismatch never probes or adopts other profile targets', async t => {
  const f = await fixture(t);
  const wrong = join(f.root, 'Default');
  await mkdir(wrong);
  f.browser.target = () => {throw new Error('Unrelated targets must not be probed');};
  await assert.rejects(f.windows.acquire({...f.request, expected_path: wrong}), /different profile/);
  assert.equal(f.pages.length, 1);
  assert.ok(f.pages[0].closed);
  assert.deepEqual(f.pages[0].navigations, ['chrome://version']);
});

test('restart refuses changed context identity even if the target ID matches', async t => {
  const f = await fixture(t);
  const first = await f.windows.acquire(f.request);
  first.page.context = 'different-profile';
  const restarted = new TaskWindows(f.sdk, join(f.root, 'state'));
  await assert.rejects(restarted.acquire(f.request), /browser profile changed/);
  assert.equal(f.pages.length, 1);
});

test('unknown mutation outcome persists across restart and is never replayed', async t => {
  const f = await fixture(t);
  let attempts = 0;
  const actionMap = {
    click: async () => {attempts += 1; throw new Error('Connection lost after sending the click');},
    inspect: async () => ({snapshot: 'synthetic'}),
  };
  const operations = new BrowserOperations(f.windows, actionMap);
  const request = {...f.request, params: {action: 'click'}};
  await assert.rejects(operations.execute(request), /Connection lost/);
  const restarted = new BrowserOperations(new TaskWindows(f.sdk, join(f.root, 'state')), actionMap);
  await assert.rejects(restarted.execute(request), /unknown outcome/);
  const inspected = await restarted.execute({...request, params: {action: 'inspect'}});
  assert.equal(inspected.snapshot, 'synthetic');
  assert.equal(attempts, 1);
});

test('validation errors before an action do not poison task recovery', async t => {
  const f = await fixture(t);
  const operations = new BrowserOperations(f.windows, {
    fill: async () => {throw new BrowserInputError('Stale UID');},
  });
  await assert.rejects(operations.execute({...f.request, params: {action: 'fill'}}), /Stale UID/);
  const stored = await f.windows.readOwnership(f.windows.key(f.request));
  assert.equal(stored.in_flight, false);
});

test('model-supplied purpose flags cannot turn an action into a description read', async t => {
  const f = await fixture(t);
  const operations = new BrowserOperations(f.windows, {eval: async () => ({result: 'ok'})});
  const request = {...f.request, params: {action: 'eval', purpose: 'description'}};
  const entry = await f.windows.acquire(request);
  entry.ownership.in_flight = true;
  await assert.rejects(operations.execute(request), /unknown outcome/);
  const readOnly = await operations.execute({...request, purpose: 'description'});
  assert.equal(readOnly.result, 'ok');
  assert.ok(entry.ownership.in_flight);
});

test('remote connections and prototype action names are refused before connecting', async t => {
  const f = await fixture(t);
  const operations = new BrowserOperations(f.windows);
  await assert.rejects(operations.execute({...f.request,
    endpoint: 'ws://remote.example/devtools/browser/test', params: {action: 'inspect'},
  }), /this machine/);
  await assert.rejects(operations.execute({...f.request, params: {action: 'constructor'}}), /Unsupported action/);
  assert.equal(f.pages.length, 0);
});
