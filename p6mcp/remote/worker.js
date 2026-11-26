// MCP for Primavera P6: relay Worker of remote access (relay mode), uploaded to the user's own Cloudflare account
// by p6mcp.remote.cloudflare. https://p6mcp-<pc>.<sub>.workers.dev -> Workers VPC Service -> tunnel -> this PC,
// 127.0.0.1:<remote port>. It forwards every request unchanged, adds the relay secret (the remote access server
// refuses requests without it) and the caller's IP, and streams the answer back (SSE included). It keeps nothing:
// no storage, no logs of request bodies.
const VERSION = "3.2.0-1";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const headers = new Headers(request.headers);
    headers.set("X-P6MCP-Relay", env.RELAY_SECRET || "");
    headers.set("X-P6MCP-Client-IP", request.headers.get("CF-Connecting-IP") || "");
    const init = {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
      redirect: "manual",
    };
    let response;
    try {
      response = await env.P6.fetch("http://127.0.0.1:" + (env.P6_PORT || "39218") + url.pathname + url.search, init);
    } catch (e) {
      return new Response("MCP for Primavera P6: the PC is not reachable (remote access is off, the PC is asleep or " +
        "MCP for Primavera P6 is not running).", {
        status: 503,
        headers: { "content-type": "text/plain; charset=utf-8", "retry-after": "30", "x-p6mcp-relay": VERSION },
      });
    }
    const out = new Response(response.body, response);
    out.headers.set("x-p6mcp-relay", VERSION);
    return out;
  },
};
