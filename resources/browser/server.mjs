/** Private MCP bridge. Only North's approved BrowserTool invokes this server. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {join, resolve} from 'node:path';
import {parseArgs} from 'node:util';
import {TaskWindows} from './windows.mjs';
import {BrowserOperations} from './operations.mjs';

const {values} = parseArgs({options: {
  'sdk-root': {type: 'string'}, 'state-dir': {type: 'string'}, version: {type: 'string'},
}});
const require = createRequire(join(resolve(values['sdk-root']), 'package.json'));
const puppeteer = require('puppeteer-core');
const {Server} = require('@modelcontextprotocol/sdk/server/index.js');
const {StdioServerTransport} = require('@modelcontextprotocol/sdk/server/stdio.js');
const {CallToolRequestSchema, ListToolsRequestSchema} = require('@modelcontextprotocol/sdk/types.js');
const windows = new TaskWindows(puppeteer, values['state-dir']);
const operations = new BrowserOperations(windows);
const MAX_RESULT_BYTES = 25000;

function boundedResult(output) {
  const text = JSON.stringify(output);
  if (Buffer.byteLength(text, 'utf8') <= MAX_RESULT_BYTES) return text;
  return JSON.stringify({
    action: output.action, profile_verified: output.profile_verified,
    login_verified: output.login_verified, window_id: output.window_id,
    truncated: true, preview: Buffer.from(text).subarray(0, 5000).toString('utf8'),
  });
}

const server = new Server({name: 'north-existing-chrome', version: values.version}, {capabilities: {tools: {}}});
server.setRequestHandler(ListToolsRequestSchema, async () => ({tools: [{
  name: 'browser_action', description: 'Internal approved browser action in a task-owned window.',
  inputSchema: {type: 'object', additionalProperties: true},
}]}));
let queue = Promise.resolve();
server.setRequestHandler(CallToolRequestSchema, request => {
  const call = queue.then(async () => {
    const arguments_ = request.params.arguments;
    try {
      assert.equal(request.params.name, 'browser_action');
      return {content: [{type: 'text', text: boundedResult(await operations.execute(arguments_))}]};
    } catch (error) {
      const message = error.message.split('\n')[0]
        .replaceAll(arguments_?.endpoint || '\0', '[local Chrome]').slice(0, 500);
      return {isError: true, content: [{type: 'text', text: message}]};
    }
  });
  queue = call.catch(() => {});
  return call;
});
process.on('SIGTERM', async () => {await windows.disconnect(); process.exit(0);});
await server.connect(new StdioServerTransport());
