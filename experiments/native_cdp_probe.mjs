/**
 * Existing-Chrome experiment, not a production browser backend.
 * Uses public puppeteer-core; never launches Chrome, reads cookies, or submits.
 * Required flags: --user-data-dir, --profile-directory, --sdk-root.
 * sdk-root is a disposable npm prefix containing puppeteer-core@25.11.0.
 */
import assert from 'node:assert/strict';
import {readFile, realpath} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {join, resolve} from 'node:path';
import {parseArgs} from 'node:util';
import {pathToFileURL} from 'node:url';

const {values} = parseArgs({
  options: {
    'user-data-dir': {type: 'string'},
    'profile-directory': {type: 'string'},
    'sdk-root': {type: 'string'},
  },
});

function required(name) {
  const value = values[name];
  assert.ok(value, `Missing --${name}`);
  return value;
}

async function endpointFor(root) {
  const [port, socketPath] = (await readFile(join(root, 'DevToolsActivePort'), 'utf8'))
    .trim().split(/\r?\n/);
  assert.match(port, /^\d+$/);
  assert.ok(Number(port) > 0 && Number(port) <= 65535);
  assert.match(socketPath, /^\/devtools\/browser\/[a-zA-Z0-9-]+$/);
  return `ws://127.0.0.1:${port}${socketPath}`;
}

async function withVerifiedProfile(page, expected, action) {
  await page.goto('chrome://version', {timeout: 15000});
  const actual = await page.$eval('#profile_path', element => element.textContent.trim());
  assert.equal(await realpath(actual), expected, 'Browser profile mismatch');
  return action();
}

const form = `<!doctype html><html><title>North synthetic form probe</title>
<form id="application">
  <label>Name <input id="name"></label>
  <label>Email <input id="email" type="email"></label>
  <label>Salary <input id="salary"></label>
  <button type="submit">Submit</button>
</form></html>`;

async function formTrial(page, trial) {
  await page.goto('about:blank');
  await page.setContent(form);
  await page.evaluate(() => {
    window.northSubmissions = 0;
    document.querySelector('form').addEventListener('submit', event => {
      event.preventDefault();
      window.northSubmissions += 1;
    });
  });
  await page.locator('#name').fill('Synthetic Test User');
  await page.locator('#email').fill('north-test@example.invalid');
  const state = await page.evaluate(() => ({
    name: document.querySelector('#name').value,
    email: document.querySelector('#email').value,
    salary: document.querySelector('#salary').value,
    submissions: window.northSubmissions,
  }));
  assert.deepEqual(state, {
    name: 'Synthetic Test User', email: 'north-test@example.invalid', salary: '', submissions: 0,
  });
  return {trial, filled_values_verified: true, unknown_field_left_blank: true, submissions: 0};
}

async function probe() {
  const root = await realpath(required('user-data-dir'));
  const directory = required('profile-directory');
  assert.ok(directory !== '.' && directory !== '..' && !/[\\/]/.test(directory));
  const expected = await realpath(join(root, directory));
  const require = createRequire(join(resolve(required('sdk-root')), 'package.json'));
  const {default: puppeteer} = await import(pathToFileURL(require.resolve('puppeteer-core')).href);
  const browser = await puppeteer.connect({
    browserWSEndpoint: await endpointFor(root), defaultViewport: null, protocolTimeout: 20000,
  });
  let page;
  let tabClosed = false;
  const results = [];
  let wrongProfileInteractions = 0;
  try {
    page = await browser.newPage();
    await assert.rejects(withVerifiedProfile(page, `${expected}-wrong-profile`, async () => {
      wrongProfileInteractions += 1;
      await formTrial(page, 0);
    }), /Browser profile mismatch/);
    assert.equal(wrongProfileInteractions, 0);
    for (let trial = 1; trial <= 3; trial += 1) {
      results.push(await withVerifiedProfile(page, expected, () => formTrial(page, trial)));
    }
  } finally {
    try {
      if (page) {
        await page.close();
        tabClosed = true;
      }
    } finally {
      await browser.disconnect(); // Never browser.close(): this is the user's Chrome.
    }
  }
  return {
    existing_browser: true,
    profile_verified: true,
    wrong_profile_rejected_before_form_interaction: wrongProfileInteractions === 0,
    form_trials: results,
    owned_test_tab_closed: tabClosed,
    connection_trials: 1,
  };
}

try {
  console.log(JSON.stringify(await probe(), null, 2));
} catch (error) {
  // Do not print personal paths or the native debugging WebSocket token.
  console.error(JSON.stringify({completed: false, error_type: error.name}));
  process.exitCode = 1;
}
