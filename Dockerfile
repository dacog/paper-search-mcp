# Multi-stage build for smaller image
FROM python:3.12-slim AS builder

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY paper_search_mcp/ paper_search_mcp/

RUN pip install --no-cache-dir build \
    && python -m build --wheel \
    && pip install --no-cache-dir dist/*.whl

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/paper-search-mcp /usr/local/bin/paper-search-mcp

# Environment variables (override at runtime with -e)
ENV PAPER_SEARCH_MCP_UNPAYWALL_EMAIL=""
ENV PAPER_SEARCH_MCP_CORE_API_KEY=""
ENV PAPER_SEARCH_MCP_SEMANTIC_SCHOLAR_API_KEY=""
ENV PAPER_SEARCH_MCP_ZENODO_ACCESS_TOKEN=""
ENV PAPER_SEARCH_MCP_DOAJ_API_KEY=""
ENV PAPER_SEARCH_MCP_GOOGLE_SCHOLAR_PROXY_URL=""
ENV PAPER_SEARCH_MCP_IEEE_API_KEY=""
ENV PAPER_SEARCH_MCP_ACM_API_KEY=""

# HTTP transport (override at runtime with -e). Defaults to stdio, the original
# behavior, so `docker run -i paper-search-mcp` still works unchanged. Set
# MCP_TRANSPORT=streamable-http (or sse) to expose the server over HTTP instead.
ENV MCP_TRANSPORT="stdio"
ENV MCP_HOST="0.0.0.0"
ENV MCP_PORT="8000"
ENV MCP_PATH="/mcp"
# Optional bearer token. When set, every HTTP request must carry
# `Authorization: Bearer <MCP_AUTH_TOKEN>`. Empty = open endpoint.
ENV MCP_AUTH_TOKEN=""
# How download_* tools hand a PDF back to a remote client over HTTP.
# embedded: base64 blob in the tool result (default).
# resource: ResourceLink the client fetches via resources/read.
# path:     legacy server-local path string (client must share FS).
# Ignored under stdio. See docs/http-pdf-delivery.md.
ENV MCP_PDF_DELIVERY="embedded"
# Size gate (bytes) for the embedded mode; larger PDFs fall back to a link.
ENV MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES="25000000"

# Document the HTTP port. Only actually listened on when MCP_TRANSPORT is
# streamable-http or sse; stdio mode does not bind any port.
EXPOSE 8000

# Use the entry point script
CMD ["paper-search-mcp"]
