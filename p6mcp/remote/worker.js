// MCP for Primavera P6: relay Worker of remote access (relay mode), uploaded to the user's own Cloudflare account
// by p6mcp.remote.cloudflare. https://p6mcp-<pc>.<sub>.workers.dev -> Workers VPC Service -> tunnel -> this PC,
// 127.0.0.1:<remote port>. It forwards every request unchanged, adds the relay secret (the remote access server
// refuses requests without it) and the caller's IP, and streams the answer back (SSE included). It keeps nothing:
// no storage, no logs of request bodies.
//
// When the PC cannot be reached (remote access off, PC asleep or off, app not running), an MCP request gets a
// JSON-RPC error that says so: claude.ai and ChatGPT show that message, where a bare HTTP 503 only shows as
// "server error" (found in the M6 end-to-end check). Other requests get the 503 with the same text.
const VERSION = "3.2.0-2";
const DOWN = "MCP for Primavera P6: the PC is not reachable. Remote access is off, the PC is asleep or off, or " +
  "MCP for Primavera P6 is not running on it. Turn it on, then try again.";

function unreachable(path, rpcId) {
  if (path === "/mcp" && rpcId !== undefined) {
    return new Response(JSON.stringify({ jsonrpc: "2.0", id: rpcId, error: { code: -32000, message: DOWN } }), {
      status: 200,
      headers: { "content-type": "application/json", "x-p6mcp-relay": VERSION },
    });
  }
  return new Response(DOWN, {
    status: 503,
    headers: { "content-type": "text/plain; charset=utf-8", "retry-after": "30", "x-p6mcp-relay": VERSION },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const headers = new Headers(request.headers);
    headers.set("X-P6MCP-Relay", env.RELAY_SECRET || "");
    headers.set("X-P6MCP-Client-IP", request.headers.get("CF-Connecting-IP") || "");
    // request bodies are small (an MCP call, a token form): read once, so the JSON-RPC id is known if the PC is down
    const body = request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer();
    let rpcId;
    if (body !== undefined && url.pathname === "/mcp") {
      try {
        const msg = JSON.parse(new TextDecoder().decode(body));
        if (msg && typeof msg === "object" && !Array.isArray(msg) && "id" in msg) rpcId = msg.id;
      } catch (e) {
        rpcId = undefined;
      }
    }
    const init = { method: request.method, headers, body, redirect: "manual" };
    let response;
    try {
      response = await env.P6.fetch("http://127.0.0.1:" + (env.P6_PORT || "39218") + url.pathname + url.search, init);
    } catch (e) {
      return unreachable(url.pathname, rpcId);
    }
    if ([502, 503, 504, 530].includes(response.status) && !response.headers.get("mcp-session-id")) {
      // the tunnel is up but nothing answers on the PC's port (the remote access server never answers 502 to 530)
      return unreachable(url.pathname, rpcId);
    }
    const out = new Response(response.body, response);
    out.headers.set("x-p6mcp-relay", VERSION);
    return out;
  },
};
