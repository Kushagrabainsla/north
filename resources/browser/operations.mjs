/** Validate and execute one SDK action, preserving unknown-outcome evidence. */
import assert from 'node:assert/strict';
import {actions, BrowserInputError} from './actions.mjs';

const effects = new Set(['goto', 'navigate', 'read', 'click', 'dblclick', 'fill', 'type', 'press',
  'select', 'check', 'uncheck', 'eval', 'screenshot', 'pdf']);

function validate(request, actionMap) {
  assert.ok(request.task_id && request.profile_id && request.profile_config && request.expected_path,
    'A task and verified-profile configuration are required.');
  const endpoint = new URL(request.endpoint);
  assert.ok(['ws:', 'wss:'].includes(endpoint.protocol), 'A browser WebSocket is required.');
  assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname), 'CDP must stay on this machine.');
  assert.ok(!endpoint.username && !endpoint.password && !endpoint.search && !endpoint.hash,
    'CDP cannot contain credentials or query parameters.');
  assert.ok(endpoint.pathname.startsWith('/devtools/browser/'), 'A browser-level CDP endpoint is required.');
  assert.ok(['preflight', 'status', 'close'].includes(request.params.action)
    || Object.hasOwn(actionMap, request.params.action), 'Unsupported action in existing Chrome.');
  if (request.purpose === 'description') assert.ok(['eval', 'inspect'].includes(request.params.action));
}

export class BrowserOperations {
  constructor(windows, actionMap = actions) {
    this.windows = windows;
    this.actions = actionMap;
  }

  async closeOwnedWindow(request) {
    const ownership = await this.windows.readOwnership(this.windows.key(request));
    if (!ownership || ownership.closed) return {action: 'close', closed: true};
    const entry = await this.windows.acquire(request);
    await this.windows.closeWindow(entry);
    return {action: 'close', closed: true};
  }

  async execute(request) {
    validate(request, this.actions);
    const action = request.params.action;
    if (action === 'close') return this.closeOwnedWindow(request);
    const entry = await this.windows.acquire(request);
    const identity = {profile_verified: true, login_verified: false, window_id: entry.ownership.window_id};
    if (['preflight', 'status'].includes(action)) return {action, ...identity, uncertain_action: entry.ownership.in_flight};
    const changesPage = request.purpose !== 'description' && effects.has(action)
      && (action !== 'read' || !!request.params.url);
    assert.ok(!changesPage || !entry.ownership.in_flight,
      'An earlier browser action has an unknown outcome. Inspect and ask the user to review; do not replay it.');
    entry.page.setDefaultTimeout(Math.min(Math.max(Number(request.params.timeout_seconds) || 30, 1), 120) * 1000);
    const output = changesPage
      ? await this.changePage(entry, request.params)
      : await this.actions[action](entry, request.params);
    return {action, ...identity, ...output};
  }

  async changePage(entry, params) {
    entry.ownership.in_flight = true;
    await this.windows.saveOwnership(entry.key, entry.ownership);
    try {
      const output = await this.actions[params.action](entry, params);
      entry.ownership.in_flight = false;
      await this.windows.saveOwnership(entry.key, entry.ownership);
      return output;
    } catch (error) {
      if (error instanceof BrowserInputError) {
        entry.ownership.in_flight = false;
        await this.windows.saveOwnership(entry.key, entry.ownership);
      }
      throw error;
    }
  }
}
