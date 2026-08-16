# PDF delivery over HTTP transports

> Applies **only** when the server runs with `MCP_TRANSPORT=streamable-http` (or `sse`).
> In `stdio` mode (the default) the `download_*` tools behave exactly as before: they write the PDF to `save_path` on the local filesystem and return the path string. Nothing on this page affects stdio.

## The problem

In stdio mode the server is a child process of the MCP client, so both share the same filesystem: a tool that returns `/home/me/downloads/2106.12345.pdf` is directly usable by the agent.

Over HTTP the server and client are on different machines. The historical `download_*` tools still wrote the PDF to the **server's** filesystem and returned a server-local path, which the remote agent could not read. The PDF existed but was unreachable.

## The fix: three delivery modes

A new env var, **`MCP_PDF_DELIVERY`**, selects how a downloaded PDF is handed back to the client over HTTP. It is ignored under stdio.

| Mode | Value | What the tool returns | Best for |
|---|---|---|---|
| **Embedded** (default) | `embedded` | `[TextContent(summary), EmbeddedResource(BlobResourceContents)]` — the PDF bytes travel base64-encoded inside the JSON-RPC tool result. | Most clients. Works with any MCP client that renders `EmbeddedResource` (Claude Desktop, opencode, …). The LLM only sees the `TextContent` summary, so the context window is not flooded with base64. |
| **Resource link** | `resource` | `[TextContent(summary), ResourceLink]` pointing at `paper://{source}/{paper_id}`. The client fetches the bytes on demand via `resources/read`. | Clients that support `ResourceLink` + binary `resources/read`. Keeps the tool response tiny; the PDF is only transferred when the client actually needs it. Also the automatic fallback when a PDF is too large to embed (see below). |
| **Path** (legacy) | `path` | The server-local path string, unchanged. | Only useful when the client happens to share the server's filesystem (e.g. the server runs on `localhost` over HTTP for routing reasons, or both mount the same NFS volume). The PDF is never sent over the wire. |

### Option 3 — serving PDFs over HTTP (not implemented)

A fourth option — having the server expose the cached PDF over its own HTTP endpoint and returning a signed URL — is **documented but not implemented**. It would avoid the JSON-RPC size limits entirely (the PDF streams as a regular HTTP response), but requires:

- a second HTTP route on the same Starlette app (e.g. `GET /papers/{source}/{paper_id}`),
- a serving directory rooted at `save_path`,
- an ephemeral signed token so that a leaked `paper://` link does not become a permanent public URL,
- and a decision on whether the bearer auth (`MCP_AUTH_TOKEN`) also gates the PDF route.

If you need this, open an issue. The plumbing for the first two delivery modes above is already in place; a `/papers/...` route can be added to `FastMCP.streamable_http_app()`'s `routes` list without changing any tool.

## Size limits for the `embedded` mode

The PDF bytes are base64-encoded (~33 % inflation) and carried inside a single JSON-RPC message. The limits that matter:

| Layer | Default body limit | Notes |
|---|---|---|
| MCP Python SDK | none | Reads the full request body into memory via `request.body()`. |
| Starlette | none | No built-in body cap. |
| uvicorn | none | No `--limit-max-body-size` flag. |
| hypercorn | 16 MB (`--max-body-size`) | The most common cap you'll hit. |
| nginx in front | 1 MB (`client_max_body_size`) | **Almost always too small for embedded PDFs.** Raise it if you proxy. |
| MCP clients (Claude Desktop, opencode, …) | varies, often a few MB | Some clients reject very large tool results. |

To stay safe across all of these, the server caps the embedded payload with **`MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES`** (default **25 000 000**, i.e. ~25 MB raw → ~33 MB on the wire). When a downloaded PDF exceeds the gate:

- in `embedded` mode, the tool falls back to a `ResourceLink` (`paper://{source}/{paper_id}`) plus a `TextContent` notice telling the agent the file is cached on the server and how to fetch it;
- in `resource` mode, the link is returned as normal (the bytes are never embedded);
- in `path` mode, the path is returned as normal.

Raise the gate if your ASGI server and client both accept larger messages:

```bash
MCP_TRANSPORT=streamable-http \
MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES=50000000 \
paper-search-mcp
```

Set it to `0` (or garbage) to fall back to the 25 MB default.

## The `paper://{source}/{paper_id}` resource

For the `resource` mode (and the embedded-overflow fallback) the server registers an MCP resource template:

```
paper://{source}/{paper_id}
```

A client reads it with `resources/read`:

```jsonc
// request
{ "method": "resources/read", "params": { "uri": "paper://arxiv/2106.12345" } }

// response — FastMCP returns bytes resources as BlobResourceContents (base64)
{ "contents": [{ "uri": "paper://arxiv/2106.12345", "mimeType": "application/pdf", "blob": "<base64...>" }] }
```

The template looks for a cached PDF under `./downloads` (the default `save_path`) whose filename contains the (sanitised) `paper_id` and ends in `.pdf`, picking the most recently modified match. If you configured a custom `save_path` on the `download_*` call, the resource lookup may not find it — in that case use `embedded` mode or `path` mode.

## Configuration

All three vars are read through `config.get_env()`, so they can be set in the shell, in `~/.config/paper-search-mcp/.env`, or via the `PAPER_SEARCH_MCP_`-prefixed form (which takes precedence). See the main README's `.env` section.

| Variable | Default | Meaning |
|---|---|---|
| `MCP_PDF_DELIVERY` | `embedded` | `embedded` \| `resource` \| `path`. Only consulted under HTTP transports; ignored under stdio. |
| `MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES` | `25000000` | Size gate in bytes for the `embedded` mode. Larger PDFs fall back to a `ResourceLink`. Non-positive or unparseable values fall back to the default. |

### Examples

```bash
# default: embed PDFs up to 25 MB in the tool result
MCP_TRANSPORT=streamable-http paper-search-mcp

# return resource links instead of embedding bytes
MCP_TRANSPORT=streamable-http MCP_PDF_DELIVERY=resource paper-search-mcp

# embed up to 50 MB (make sure your ASGI server + client accept this)
MCP_TRANSPORT=streamable-http \
MCP_PDF_DELIVERY=embedded \
MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES=50000000 \
paper-search-mcp

# legacy behaviour: return server-local path (client must share FS)
MCP_TRANSPORT=streamable-http MCP_PDF_DELIVERY=path paper-search-mcp
```

```bash
# Docker
docker run --rm -p 8000:8000 \
  -e MCP_TRANSPORT=streamable-http \
  -e MCP_PDF_DELIVERY=resource \
  -e MCP_AUTH_TOKEN=your-shared-secret \
  paper-search-mcp
```

## Which mode should I pick?

- **You run the server on a remote VPS and the agent is on your laptop → `embedded` (default).** The PDF arrives in the tool result. No extra round-trip. The 25 MB gate covers the vast majority of papers; arXiv preprints are typically 0.5–5 MB.

- **You are behind a tight proxy (nginx 1 MB body limit you cannot change) or your client chokes on large tool results → `resource`.** The tool result stays small; the client fetches the PDF via `resources/read` only when needed. Requires a client that supports `ResourceLink` and binary resource reads.

- **The server and client share a filesystem (same host, NFS, …) → `path`.** Cheapest option: the PDF is never serialised. This is also exactly what stdio mode does.

- **You want the server to serve PDFs over plain HTTP URLs → not built yet.** See "Option 3" above.

## What does the agent (the LLM) actually see?

In all three HTTP modes the first element of the tool result is a `TextContent` summary such as:

> `Downloaded arxiv/2106.12345 (901758 bytes) as 2106.12345v1.pdf. The PDF is attached as an embedded resource.`

The binary blob (or the `ResourceLink`) is metadata for the **client host** to persist/offer to the user — it is not injected into the LLM's text context. This is by design: a 5 MB PDF as base64 would be ~6.7 MB of text, which would blow the context window. If you want the paper's *text*, call the `read_<source>_paper` tool instead, which extracts text via `pypdf` and returns a plain string.