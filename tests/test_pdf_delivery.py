"""Tests for HTTP-transport PDF delivery in ``paper_search_mcp.server``.

When the server runs over stdio, the ``download_*`` tools return a server-local
file path string (unchanged historical behaviour). When the server runs over
``streamable-http`` or ``sse``, the path is meaningless to a remote client, so
``_wrap_download_result`` repackages the downloaded PDF into one of three
delivery modes selected by ``MCP_PDF_DELIVERY``:

- ``embedded`` (default): ``[TextContent, EmbeddedResource(BlobResourceContents)]``
- ``resource``: ``[TextContent, ResourceLink]`` → client fetches via ``resources/read``
- ``path``: legacy server-local path string (only useful if client shares FS)

These tests cover all three modes, the size-gate overflow fallback, the
stdio-no-op behaviour, and the ``paper://{source}/{paper_id}`` resource
template registered for on-demand reads.
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from unittest.mock import patch

import pytest

from paper_search_mcp import server
from paper_search_mcp.server import (
    _is_http_transport,
    _pdf_delivery_mode,
    _embedded_max_bytes,
    _wrap_download_result,
    _pdf_uri,
)
from mcp.types import TextContent, EmbeddedResource, ResourceLink, BlobResourceContents


# Keys that drive transport + delivery selection. Cleared between tests so the
# process environment never leaks into a hermetic test.
_DELIVERY_ENV_KEYS = [
    "MCP_TRANSPORT",
    "MCP_PDF_DELIVERY",
    "MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES",
    "PAPER_SEARCH_MCP_MCP_TRANSPORT",
    "PAPER_SEARCH_MCP_MCP_PDF_DELIVERY",
    "PAPER_SEARCH_MCP_MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES",
]


class _DeliveryEnvMixin(unittest.TestCase):
    """Snapshot/restore the delivery-related env vars between tests."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k) for k in _DELIVERY_ENV_KEYS if k in os.environ}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


class TestTransportDetection(_DeliveryEnvMixin):
    """``_is_http_transport`` and ``_pdf_delivery_mode`` read MCP_TRANSPORT."""

    def test_stdio_is_not_http(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "stdio"}, clear=False):
            self.assertFalse(_is_http_transport())
            self.assertEqual(_pdf_delivery_mode(), "path")

    def test_no_transport_defaults_to_stdio(self):
        # MCP_TRANSPORT absent
        self.assertFalse(_is_http_transport())
        self.assertEqual(_pdf_delivery_mode(), "path")

    def test_streamable_http_is_http(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "streamable-http"}, clear=False):
            self.assertTrue(_is_http_transport())

    def test_sse_is_http(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "sse"}, clear=False):
            self.assertTrue(_is_http_transport())

    def test_default_delivery_for_http_is_embedded(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "streamable-http"}, clear=False):
            self.assertEqual(_pdf_delivery_mode(), "embedded")

    def test_delivery_resource_mode(self):
        with patch.dict(
            os.environ,
            {"MCP_TRANSPORT": "streamable-http", "MCP_PDF_DELIVERY": "resource"},
            clear=False,
        ):
            self.assertEqual(_pdf_delivery_mode(), "resource")

    def test_delivery_path_mode(self):
        with patch.dict(
            os.environ,
            {"MCP_TRANSPORT": "streamable-http", "MCP_PDF_DELIVERY": "path"},
            clear=False,
        ):
            self.assertEqual(_pdf_delivery_mode(), "path")

    def test_invalid_delivery_falls_back_to_embedded(self):
        with patch.dict(
            os.environ,
            {"MCP_TRANSPORT": "streamable-http", "MCP_PDF_DELIVERY": "bogus"},
            clear=False,
        ):
            self.assertEqual(_pdf_delivery_mode(), "embedded")

    def test_prefixed_env_form_is_honored(self):
        # PAPER_SEARCH_MCP_MCP_PDF_DELIVERY takes precedence over the bare form.
        with patch.dict(
            os.environ,
            {
                "MCP_TRANSPORT": "streamable-http",
                "MCP_PDF_DELIVERY": "embedded",
                "PAPER_SEARCH_MCP_MCP_PDF_DELIVERY": "resource",
            },
            clear=False,
        ):
            self.assertEqual(_pdf_delivery_mode(), "resource")

    def test_stdio_ignores_delivery_var(self):
        # Even with MCP_PDF_DELIVERY=embedded, stdio always returns path.
        with patch.dict(
            os.environ,
            {"MCP_TRANSPORT": "stdio", "MCP_PDF_DELIVERY": "embedded"},
            clear=False,
        ):
            self.assertEqual(_pdf_delivery_mode(), "path")


class TestEmbeddedMaxBytes(_DeliveryEnvMixin):
    """``_embedded_max_bytes`` parses the size gate with a safe default."""

    def test_default_when_unset(self):
        self.assertEqual(_embedded_max_bytes(), 25_000_000)

    def test_explicit_value(self):
        with patch.dict(
            os.environ,
            {"MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES": "12345"},
            clear=False,
        ):
            self.assertEqual(_embedded_max_bytes(), 12345)

    def test_prefixed_form(self):
        with patch.dict(
            os.environ,
            {"PAPER_SEARCH_MCP_MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES": "999"},
            clear=False,
        ):
            self.assertEqual(_embedded_max_bytes(), 999)

    def test_non_positive_falls_back_to_default(self):
        with patch.dict(
            os.environ,
            {"MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES": "0"},
            clear=False,
        ):
            self.assertEqual(_embedded_max_bytes(), 25_000_000)

    def test_garbage_falls_back_to_default(self):
        with patch.dict(
            os.environ,
            {"MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES": "lots"},
            clear=False,
        ):
            self.assertEqual(_embedded_max_bytes(), 25_000_000)


class TestPdfUri(unittest.TestCase):
    """``_pdf_uri`` builds stable, sanitised paper:// URIs."""

    def test_simple(self):
        self.assertEqual(_pdf_uri("arxiv", "2106.12345"), "paper://arxiv/2106.12345")

    def test_slashes_in_id_preserved(self):
        # IACR ids look like '2009/101' — the slash is meaningful.
        self.assertEqual(_pdf_uri("iacr", "2009/101"), "paper://iacr/2009/101")

    def test_doi_colon(self):
        # Semantic Scholar 'DOI:10.18653/v1/N18-3011' form
        uri = _pdf_uri("semantic", "DOI:10.18653/v1/N18-3011")
        self.assertTrue(uri.startswith("paper://semantic/"))
        self.assertIn("10.18653", uri)

    def test_empty_source_falls_back(self):
        self.assertEqual(_pdf_uri("", "123"), "paper://paper/123")


class TestWrapDownloadResult(_DeliveryEnvMixin):
    """``_wrap_download_result`` packages the PDF per the active delivery mode."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pdf_path = os.path.join(self._tmp.name, "test_paper.pdf")
        self.pdf_bytes = b"%PDF-1.4\nfake pdf body\n%%EOF\n"
        with open(self.pdf_path, "wb") as fh:
            fh.write(self.pdf_bytes)

    def _set_http(self, delivery="embedded", max_bytes=None):
        env = {"MCP_TRANSPORT": "streamable-http", "MCP_PDF_DELIVERY": delivery}
        if max_bytes is not None:
            env["MCP_PDF_DELIVERY_EMBEDDED_MAX_BYTES"] = str(max_bytes)
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stdio_returns_path_unchanged(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "stdio"}, clear=False):
            out = _wrap_download_result(self.pdf_path, source="arxiv", paper_id="123")
            self.assertIs(out, self.pdf_path)

    def test_stdio_with_delivery_var_still_returns_path(self):
        # stdio ignores MCP_PDF_DELIVERY entirely.
        with patch.dict(
            os.environ,
            {"MCP_TRANSPORT": "stdio", "MCP_PDF_DELIVERY": "embedded"},
            clear=False,
        ):
            out = _wrap_download_result(self.pdf_path, source="arxiv", paper_id="123")
            self.assertEqual(out, self.pdf_path)

    def test_http_path_mode_returns_path_string(self):
        self._set_http(delivery="path")
        out = _wrap_download_result(self.pdf_path, source="arxiv", paper_id="123")
        self.assertEqual(out, self.pdf_path)

    def test_http_embedded_returns_text_plus_blob(self):
        self._set_http(delivery="embedded")
        out = _wrap_download_result(self.pdf_path, source="arxiv", paper_id="123")
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 2)
        self.assertIsInstance(out[0], TextContent)
        self.assertIsInstance(out[1], EmbeddedResource)
        blob_res = out[1].resource
        self.assertIsInstance(blob_res, BlobResourceContents)
        self.assertEqual(blob_res.mimeType, "application/pdf")
        self.assertEqual(blob_res.blob, base64.b64encode(self.pdf_bytes).decode("ascii"))
        self.assertIn("arxiv", str(blob_res.uri))
        self.assertIn("123", str(blob_res.uri))
        # The TextContent summary mentions the size and the embedded resource.
        self.assertIn(str(len(self.pdf_bytes)), out[0].text)
        self.assertIn("embedded", out[0].text.lower())

    def test_http_resource_mode_returns_text_plus_link(self):
        self._set_http(delivery="resource")
        out = _wrap_download_result(self.pdf_path, source="arxiv", paper_id="123")
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 2)
        self.assertIsInstance(out[0], TextContent)
        self.assertIsInstance(out[1], ResourceLink)
        link = out[1]
        self.assertEqual(link.mimeType, "application/pdf")
        self.assertEqual(link.size, len(self.pdf_bytes))
        self.assertIn("resources/read", out[0].text)
        self.assertIn("paper://arxiv/123", out[0].text)

    def test_http_embedded_overflow_falls_back_to_resource_link(self):
        # Force the size gate below the PDF size.
        self._set_http(delivery="embedded", max_bytes=4)
        out = _wrap_download_result(self.pdf_path, source="arxiv", paper_id="123")
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 2)
        self.assertIsInstance(out[0], TextContent)
        self.assertIsInstance(out[1], ResourceLink)
        self.assertIn("exceeds", out[0].text)
        self.assertIn("paper://arxiv/123", out[0].text)

    def test_http_embedded_error_string_returned_as_is(self):
        # When the downloader returns an error string (not a path), the wrapper
        # must surface it verbatim — there is nothing to embed.
        self._set_http(delivery="embedded")
        out = _wrap_download_result("Download failed: no PDF available", source="arxiv", paper_id="123")
        self.assertEqual(out, "Download failed: no PDF available")

    def test_http_embedded_nonexistent_path_returned_as_is(self):
        self._set_http(delivery="embedded")
        out = _wrap_download_result("/nonexistent/path.pdf", source="arxiv", paper_id="123")
        self.assertEqual(out, "/nonexistent/path.pdf")

    def test_http_resource_mode_error_string_returned_as_is(self):
        self._set_http(delivery="resource")
        out = _wrap_download_result("unsupported source", source="dblp", paper_id="123")
        self.assertEqual(out, "unsupported source")


class TestPaperResourceTemplate(_DeliveryEnvMixin):
    """The ``paper://{source}/{paper_id}`` resource template serves cached PDFs."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # The template reads from ./downloads relative to CWD; we point CWD at
        # the temp dir so the test is hermetic.
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        os.makedirs("downloads", exist_ok=True)
        self.addCleanup(self._restore_cwd)
        self.paper_id = "2106.99999"
        self.pdf_path = os.path.join("downloads", f"{self.paper_id}v1.pdf")
        self.pdf_bytes = b"%PDF-1.4\nresource template test\n%%EOF\n"
        with open(self.pdf_path, "wb") as fh:
            fh.write(self.pdf_bytes)

    def _restore_cwd(self):
        os.chdir(self._orig_cwd)

    def test_resource_read_returns_pdf_bytes(self):
        import asyncio

        async def go():
            return await server.mcp.read_resource(f"paper://arxiv/{self.paper_id}")

        contents = asyncio.run(go())
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0].mime_type, "application/pdf")
        # FastMCP returns bytes for a resource returning bytes; the lowlevel
        # layer converts to BlobResourceContents (base64) on the wire, but
        # read_resource() here yields the raw bytes.
        self.assertEqual(contents[0].content, self.pdf_bytes)

    def test_resource_read_missing_paper_raises(self):
        import asyncio

        async def go():
            return await server.mcp.read_resource("paper://arxiv/does-not-exist-999")

        # FastMCP's resource manager wraps the template's FileNotFoundError in
        # a ValueError ("Error creating resource from template: ..."). We only
        # care that the read fails for a missing paper.
        with self.assertRaises(Exception):
            try:
                asyncio.run(go())
            except ValueError as exc:
                self.assertIn("No cached PDF", str(exc))
                raise


if __name__ == "__main__":
    unittest.main()