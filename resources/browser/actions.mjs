/** Thin browser actions using public Puppeteer APIs; policy lives in North. */
import assert from 'node:assert/strict';

export class BrowserInputError extends Error {}
function requireInput(condition, message) {
  if (!condition) throw new BrowserInputError(message);
}

export async function inspect(entry, params = {}) {
  const tree = await entry.page.accessibility.snapshot({interestingOnly: false});
  const lines = [];
  const limit = Math.min(Math.max(Number(params.limit) || 100, 1), 1000);
  async function visit(node, depth) {
    if (!node || lines.length >= limit) return;
    const handle = await node.elementHandle();
    let uid = '';
    if (handle) {
      uid = `n${entry.document}_${await handle.backendNodeId()}`;
      const previous = entry.nodes.get(uid);
      if (previous) await previous.dispose();
      entry.nodes.set(uid, handle);
    }
    const inForm = handle && await handle.evaluate(el => !!el.closest?.('form'));
    // AX omits unnamed forms. Preserve real DOM membership for the shared
    // approval classifier, so a plain "Next" can still be a submission.
    if (inForm) lines.push(`${'  '.repeat(depth)}uid=- form ""`);
    const name = String(node.name || '').trim().slice(0, 200);
    lines.push(`${'  '.repeat(depth + (inForm ? 1 : 0))}uid=${uid || '-'} ${node.role} ${JSON.stringify(name)}`);
    for (const child of node.children || []) await visit(child, depth + 1);
  }
  await visit(tree, 0);
  const snapshot = lines.join('\n');
  const previous = entry.snapshot;
  entry.snapshot = snapshot;
  return {snapshot: params.diff && previous === snapshot ? '' : snapshot, tree: snapshot, url: entry.page.url()};
}

async function element(entry, params) {
  if (params.uid) {
    const handle = entry.nodes.get(params.uid);
    requireInput(handle, 'Unknown or stale UID. Inspect this task page again before interacting.');
    requireInput(await handle.evaluate(el => el.isConnected), 'Element was removed; inspect again.');
    return handle;
  }
  requireInput(params.selector, 'An element UID or selector is required.');
  const handle = await entry.page.waitForSelector(params.selector);
  requireInput(handle, 'Element was not found.');
  return handle;
}

async function click(entry, params, clickCount = 1) {
  if (params.xy) {
    const point = String(params.xy).split(',').map(Number);
    requireInput(point.length === 2 && point.every(Number.isFinite), 'Invalid coordinates.');
    await entry.page.mouse.click(...point, {clickCount});
  } else await (await element(entry, params)).click({clickCount});
  return inspect(entry);
}

async function fill(entry, params) {
  const handle = await element(entry, params);
  // The SDK's locator owns waiting, visibility and supported input behaviour.
  await handle.asLocator().fill(String(params.value ?? ''));
  return inspect(entry);
}

async function type(entry, params) {
  if (params.selector || params.uid) await (await element(entry, params)).focus();
  await entry.page.keyboard.type(String(params.value ?? ''));
  return {};
}

async function setChecked(entry, params, checked) {
  const handle = await element(entry, params);
  const current = await handle.evaluate(el => {
    if (!['checkbox', 'radio'].includes(el.type)) throw new Error('Not a checkbox or radio button.');
    return el.checked;
  });
  if (current !== checked) await handle.click();
  return inspect(entry);
}

async function navigate(entry, params) {
  requireInput(params.url, 'A URL is required.');
  await entry.page.goto(params.url, {waitUntil: 'domcontentloaded'});
  return inspect(entry);
}

async function read(entry, params) {
  if (params.url) await navigate(entry, params);
  const article = await entry.page.evaluate(() => ({
    title: document.title,
    content: (document.querySelector('article, main, [role="main"]') || document.body)?.innerText || '',
  }));
  return {article, url: entry.page.url()};
}

async function extract(entry, params) {
  const items = await entry.page.evaluate(({limit, query}) => {
    const records = [];
    for (const table of document.querySelectorAll('table')) {
      const headers = [...table.querySelectorAll('tr th')].map(cell => cell.innerText.trim().slice(0, 200));
      for (const row of table.querySelectorAll('tr')) {
        const cells = [...row.querySelectorAll('td')];
        if (!cells.length || (query && !row.innerText.toLowerCase().includes(query.toLowerCase()))) continue;
        records.push(Object.fromEntries(cells.map((cell, index) => [
          headers[index] || `column_${index + 1}`, cell.innerText.trim().slice(0, 200),
        ])));
        if (records.length >= limit) return records;
      }
    }
    return records;
  }, {limit: Math.min(Math.max(Number(params.limit) || 25, 1), 100), query: String(params.query || '')});
  return {items, count: items.length, source: 'html_tables', note: 'For non-table listings, use read or inspect.'};
}

async function assertion(entry, params) {
  const kind = params.assert_type || 'text';
  const condition = params.assert_condition || 'contains';
  const handle = kind === 'url' ? null
    : params.uid || params.selector ? await element(entry, params) : null;
  let actual;
  if (kind === 'url') actual = entry.page.url();
  else if (kind === 'exists') actual = handle !== null;
  else if (kind === 'state') {
    assert.ok(handle, 'State assertions require an element.');
    actual = await handle.evaluate((el, state) => {
      const states = {
        checked: el.checked === true, unchecked: el.checked === false,
        selected: el.selected === true, enabled: !el.disabled, disabled: !!el.disabled,
        visible: el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden',
      };
      return states[state] ?? false;
    }, condition);
  } else if (handle) actual = await handle.evaluate((el, type) => type === 'value' ? el.value : el.innerText, kind);
  else actual = await entry.page.evaluate(() => document.body?.innerText || '');
  const expected = String(params.value ?? '');
  const held = ['state', 'exists'].includes(kind) ? actual === true
    : condition === 'equals' ? String(actual) === expected
    : condition === 'matches' ? new RegExp(expected).test(String(actual))
    : String(actual).includes(expected);
  return {held, assert_type: kind, assert_condition: condition};
}

async function wait(entry, params) {
  const condition = params.condition || 'network-idle';
  if (condition === 'network-idle') await entry.page.waitForNetworkIdle();
  else {
    assert.ok(params.pattern, 'Wait condition requires a pattern.');
    if (condition === 'selector') await entry.page.waitForSelector(params.pattern);
    else if (condition === 'url') await entry.page.waitForFunction(pattern => location.href.includes(pattern), {}, params.pattern);
    else await entry.page.waitForFunction(pattern => document.body?.innerText.includes(pattern), {}, params.pattern);
  }
  return {condition_met: true};
}

export const actions = {
  goto: navigate, navigate, inspect, read, extract, fill, type, assert: assertion, wait,
  click: (entry, params) => click(entry, params),
  dblclick: (entry, params) => click(entry, params, 2),
  check: (entry, params) => setChecked(entry, params, true),
  uncheck: (entry, params) => setChecked(entry, params, false),
  press: async (entry, params) => {await entry.page.keyboard.press(params.value || 'Enter'); return inspect(entry);},
  select: async (entry, params) => ({selected: await (await element(entry, params)).select(String(params.value ?? ''))}),
  eval: async (entry, params) => ({result: await entry.page.evaluate(params.js)}),
  screenshot: async (entry, params) => {
    assert.ok(params.filename, 'A workspace filename is required.');
    await entry.page.screenshot({path: params.filename}); return {filename: params.filename};
  },
  pdf: async (entry, params) => {
    assert.ok(params.filename, 'A workspace filename is required.');
    await entry.page.pdf({path: params.filename}); return {filename: params.filename};
  },
};
