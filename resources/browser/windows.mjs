/** Task-owned windows in an existing Chrome profile. No launch or cookie copying. */
import assert from 'node:assert/strict';
import {createHash, randomUUID} from 'node:crypto';
import {mkdir, readFile, realpath, rename, unlink, writeFile} from 'node:fs/promises';
import {join} from 'node:path';

const digest = value => createHash('sha256').update(value).digest('hex');
class ProfileMismatch extends Error {}

export class TaskWindows {
  constructor(puppeteer, stateDirectory) {
    this.puppeteer = puppeteer;
    this.stateDirectory = stateDirectory;
    this.connections = new Map();
    this.windows = new Map();
  }

  async connection(endpoint) {
    let browser = this.connections.get(endpoint);
    if (!browser?.connected) {
      browser = await this.puppeteer.connect({
        browserWSEndpoint: endpoint, defaultViewport: null, protocolTimeout: 20000,
      });
      this.connections.set(endpoint, browser);
    }
    return browser;
  }

  key(request) {
    return digest(JSON.stringify([request.task_id, request.profile_id]));
  }

  async readOwnership(key) {
    try {
      return JSON.parse(await readFile(join(this.stateDirectory, `${key}.json`), 'utf8'));
    } catch (error) {
      if (error.code === 'ENOENT') return null;
      throw error;
    }
  }

  async saveOwnership(key, ownership) {
    await mkdir(this.stateDirectory, {recursive: true, mode: 0o700});
    const path = join(this.stateDirectory, `${key}.json`);
    const temporary = `${path}.${randomUUID()}.tmp`;
    await writeFile(temporary, JSON.stringify(ownership), {mode: 0o600});
    await rename(temporary, path);
  }

  async verifyIdentity(page, expected) {
    await page.goto('chrome://version', {timeout: 15000});
    const actual = await page.$eval('#profile_path', element => element.textContent.trim());
    if (await realpath(actual) !== await realpath(expected)) {
      throw new ProfileMismatch('Connection reached a different profile. No fallback is allowed.');
    }
  }

  async newWindow(browser, expected) {
    // Native Chrome only creates windows in its last-used regular profile.
    // Enumerating other target context IDs does not make them selectable.
    const page = await browser.newPage({type: 'window', background: true});
    try {
      await this.verifyIdentity(page, expected);
      await page.goto('about:blank');
      return page;
    } catch (error) {
      await page.close();
      if (error instanceof ProfileMismatch) {
        throw new ProfileMismatch('Chrome selected a different profile. Bring the requested profile window '
          + 'to the front, then retry preflight through the same approval layer. No other account was used.');
      }
      throw error;
    }
  }

  async targetInfo(page) {
    const session = await page.createCDPSession();
    try {
      return (await session.send('Target.getTargetInfo')).targetInfo;
    } finally {
      await session.detach();
    }
  }

  async restoreWindow(browser, ownership) {
    // The recorded target/context was verified before use. Check its immutable
    // identity on reconnect without creating a window or navigating the draft.
    assert.ok(ownership.browser_context_id, 'Task lacks saved profile evidence; review before continuing.');
    for (const page of await browser.pages()) {
      const info = await this.targetInfo(page);
      if (info.targetId !== ownership.target_id) continue;
      assert.equal(info.browserContextId, ownership.browser_context_id, 'Task browser profile changed.');
      return page;
    }
    throw new Error('The task tab was closed. North will not adopt a user tab or recreate a lost draft.');
  }

  async acquire(request) {
    const key = this.key(request);
    const browser = await this.connection(request.endpoint);
    const binding = digest(JSON.stringify([request.endpoint, request.expected_path, request.profile_config]));
    const entry = this.windows.get(key);
    if (entry?.browser === browser) {
      assert.equal(entry.ownership.binding, binding, 'Task profile changed; use a new task after review.');
      await this.verifyWindow(entry);
      return entry;
    }
    const stored = await this.prepareOwnership(key, binding);
    const page = await this.acquirePage(browser, request, stored);
    const restored = await this.bindWindow({browser, page, binding, key, stored});
    this.windows.set(key, restored);
    return restored;
  }

  async prepareOwnership(key, binding) {
    const stored = await this.readOwnership(key);
    if (stored) {
      assert.equal(stored.binding, binding, 'Chrome connection or task profile changed; review the old task window.');
      assert.ok(!stored.creating && !stored.closed,
        'Task window creation was interrupted, or the window was closed. Review before starting a new task.');
    } else {
      // A crash after creating a window must not silently create a second one.
      await this.saveOwnership(key, {binding, creating: true});
    }
    return stored;
  }

  async acquirePage(browser, request, stored) {
    try {
      return stored
        ? await this.restoreWindow(browser, stored)
        : await this.newWindow(browser, request.expected_path);
    } catch (error) {
      // newWindow closes a rejected profile window before it rethrows. Keep
      // interrupted/uncertain creation markers, but allow a known mismatch retry.
      if (!stored && error instanceof ProfileMismatch) {
        await unlink(join(this.stateDirectory, `${this.key(request)}.json`));
      }
      throw error;
    }
  }

  async bindWindow({browser, page, binding, key, stored}) {
    const info = await this.targetInfo(page);
    assert.ok(info.browserContextId, 'Chrome did not provide task profile-context evidence.');
    const ownership = stored ?? {
      binding, target_id: info.targetId, browser_context_id: info.browserContextId, window_id: await page.windowId(),
      creating: false, closed: false, in_flight: false,
    };
    const entry = {browser, page, ownership, key, nodes: new Map(), document: randomUUID(), snapshot: ''};
    page.on('framenavigated', frame => {
      if (frame === page.mainFrame()) {
        entry.nodes.clear();
        entry.document = randomUUID();
        entry.snapshot = '';
      }
    });
    await this.verifyWindow(entry);
    await this.saveOwnership(key, ownership);
    return entry;
  }

  async verifyWindow(entry) {
    assert.ok(!entry.page.isClosed(), 'Task tab was closed; North will not switch to a user tab.');
    assert.equal(await entry.page.windowId(), entry.ownership.window_id,
      'Task tab moved outside its owned window. Move it back before continuing.');
  }

  async closeWindow(entry) {
    // Closing a user-created tab in this window would destroy work we do not own.
    await entry.page.close();
    entry.ownership.closed = true;
    await this.saveOwnership(entry.key, entry.ownership);
    this.windows.delete(entry.key);
  }

  async disconnect() {
    for (const browser of this.connections.values()) await browser.disconnect();
    this.connections.clear();
    this.windows.clear();
  }
}
