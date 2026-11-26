// A probe MCP server built with an official TypeScript SDK, over stdio.
// Run it from a folder where one SDK is installed:
//   node ts_server.mjs v1   -> @modelcontextprotocol/sdk (1.x, the package most servers use)
//   node ts_server.mjs v2   -> @modelcontextprotocol/server (2.x)
// NODE_PATH names the node_modules folder that holds the SDK.
import { createRequire } from "node:module";
import path from "node:path";

const which = process.argv[2] || "v1";
const require = createRequire(path.join(process.env.NODE_PATH, "x.js"));

let McpServer, StdioServerTransport, z;
if (which === "v1") {
  ({ McpServer } = await import(require.resolve("@modelcontextprotocol/sdk/server/mcp.js")));
  ({ StdioServerTransport } = await import(require.resolve("@modelcontextprotocol/sdk/server/stdio.js")));
} else {
  ({ McpServer } = await import(require.resolve("@modelcontextprotocol/server")));
  ({ StdioServerTransport } = await import(require.resolve("@modelcontextprotocol/server/stdio")));
}
z = await import(require.resolve("zod"));

const server = new McpServer({ name: `probe-ts-${which}`, version: "1" });
const text = (t) => ({ content: [{ type: "text", text: t }] });
if (which === "v1") {
  server.tool("echo", "Return the text unchanged.", { text: z.string() }, async ({ text: t }) => text(t));
  server.tool("big", "Return a text of the given size.", { kilobytes: z.number() }, async ({ kilobytes }) =>
    text("x".repeat(kilobytes * 1024)),
  );
} else {
  server.registerTool(
    "echo",
    { description: "Return the text unchanged.", inputSchema: z.object({ text: z.string() }) },
    async ({ text: t }) => text(t),
  );
  server.registerTool(
    "big",
    { description: "Return a text of the given size.", inputSchema: z.object({ kilobytes: z.number() }) },
    async ({ kilobytes }) => text("x".repeat(kilobytes * 1024)),
  );
}
await server.connect(new StdioServerTransport());
