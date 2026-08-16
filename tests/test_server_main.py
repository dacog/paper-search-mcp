"""Tests for the transport selection and HTTP auth wiring in ``paper_search_mcp.server``.

These tests cover ``main()``'s env-var-driven routing (stdio / streamable-http / sse /
invalid) and the ``_apply_http_settings()`` helper that mutates the module-level
``mcp`` instance in place — including the optional bearer-token auth path enabled by
``MCP_AUTH_TOKEN``.

The live end-to-end test (``TestHttpTransportLive``) actually boots the server on a
free localhost port and connects via the MCP streamable-http client to confirm that
the auth middleware rejects unauthenticated requests and accepts valid ones. It is
skipped automatically when the ``mcp.client.streamable_http`` module is unavailable
(older MCP SDK builds) or when no free port can be bound.
"""

import asyncio
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from paper_search_mcp import config, server


def _free_port() -> int:
    """Return a free localhost TCP port (best-effort)."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class _TransportSnapshotMixin(unittest.TestCase):
    """Snapshot and restore the module-level ``mcp`` instance state between tests.

    ``_apply_http_settings`` mutates ``mcp.settings`` and ``mcp._token_verifier`` in
    place; without restoration, later tests in the same process would see leftover
    auth config and fail nondeterministically.
    """

    def setUp(self):
        self._orig = {
            "host": server.mcp.settings.host,
            "port": server.mcp.settings.port,
            "streamable_http_path": server.mcp.settings.streamable_http_path,
            "sse_path": server.mcp.settings.sse_path,
            "auth": server.mcp.settings.auth,
            "token_verifier": server.mcp._token_verifier,
            "transport_security": server.mcp.settings.transport_security,
        }
        # Replace mcp.run with a recorder so main() never actually starts a server.
        self._orig_run = server.mcp.run
        self._calls = []

        def _record_run(transport="stdio", **kwargs):
            self._calls.append((transport, kwargs))

        server.mcp.run = _record_run

    def tearDown(self):
        server.mcp.settings.host = self._orig["host"]
        server.mcp.settings.port = self._orig["port"]
        server.mcp.settings.streamable_http_path = self._orig["streamable_http_path"]
        server.mcp.settings.sse_path = self._orig["sse_path"]
        server.mcp.settings.auth = self._orig["auth"]
        server.mcp._token_verifier = self._orig["token_verifier"]
        server.mcp.settings.transport_security = self._orig["transport_security"]
        server.mcp.run = self._orig_run


class TestMainTransportRouting(_TransportSnapshotMixin):
    """``main()`` selects the transport from ``MCP_TRANSPORT`` and calls ``mcp.run``."""

    _ENV_TRANSPORTS = {
        "stdio": "stdio",
        "streamable-http": "streamable-http",
        "sse": "sse",
    }

    def test_default_transport_is_stdio(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_TRANSPORT", None)
            server.main()
        self.assertEqual(self._calls, [("stdio", {})])

    def test_stdio_explicit(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "stdio"}, clear=False):
            server.main()
        self.assertEqual(self._calls, [("stdio", {})])

    def test_streamable_http_calls_run_with_transport(self):
        with patch.dict(
            os.environ,
            {"MCP_TRANSPORT": "streamable-http"},
            clear=False,
        ):
            server.main()
        self.assertEqual(len(self._calls), 1)
        self.assertEqual(self._calls[0][0], "streamable-http")

    def test_sse_calls_run_with_transport(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "sse"}, clear=False):
            server.main()
        self.assertEqual(len(self._calls), 1)
        self.assertEqual(self._calls[0][0], "sse")

    def test_invalid_transport_raises_value_error(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "bogus"}, clear=False):
            with self.assertRaises(ValueError) as ctx:
                server.main()
        self.assertIn("Unknown transport: bogus", str(ctx.exception))
        self.assertEqual(self._calls, [])


class TestApplyHttpSettings(_TransportSnapshotMixin):
    """``_apply_http_settings`` mutates ``mcp.settings`` from env vars."""

    def test_streamable_http_path_default(self):
        with patch.dict(
            os.environ,
            {
                "MCP_TRANSPORT": "streamable-http",
                "MCP_HOST": "127.0.0.1",
                "MCP_PORT": "9090",
            },
            clear=False,
        ):
            os.environ.pop("MCP_PATH", None)
            os.environ.pop("MCP_AUTH_TOKEN", None)
            server.main()
        self.assertEqual(server.mcp.settings.host, "127.0.0.1")
        self.assertEqual(server.mcp.settings.port, 9090)
        self.assertEqual(server.mcp.settings.streamable_http_path, "/mcp")
        self.assertIsNone(server.mcp.settings.auth)
        self.assertIsNone(server.mcp._token_verifier)

    def test_streamable_http_path_override(self):
        with patch.dict(
            os.environ,
            {
                "MCP_TRANSPORT": "streamable-http",
                "MCP_HOST": "0.0.0.0",
                "MCP_PORT": "8000",
                "MCP_PATH": "/papers",
            },
            clear=False,
        ):
            os.environ.pop("MCP_AUTH_TOKEN", None)
            server.main()
        self.assertEqual(server.mcp.settings.host, "0.0.0.0")
        self.assertEqual(server.mcp.settings.port, 8000)
        self.assertEqual(server.mcp.settings.streamable_http_path, "/papers")

    def test_sse_path_default_and_override(self):
        with patch.dict(
            os.environ,
            {"MCP_TRANSPORT": "sse", "MCP_HOST": "127.0.0.1", "MCP_PORT": "8001"},
            clear=False,
        ):
            os.environ.pop("MCP_PATH", None)
            server.main()
        self.assertEqual(server.mcp.settings.sse_path, "/sse")

        with patch.dict(
            os.environ,
            {"MCP_TRANSPORT": "sse", "MCP_PATH": "/events"},
            clear=False,
        ):
            server.main()
        self.assertEqual(server.mcp.settings.sse_path, "/events")

    def test_stdio_does_not_touch_settings(self):
        # stdio branch must not mutate host/port/path, so callers that rely on the
        # original FastMCP defaults are unaffected.
        original_host = self._orig["host"]
        with patch.dict(os.environ, {"MCP_TRANSPORT": "stdio"}, clear=False):
            server.main()
        self.assertEqual(server.mcp.settings.host, original_host)
        self.assertIsNone(server.mcp.settings.auth)
        self.assertIsNone(server.mcp._token_verifier)


class TestBearerAuthWiring(_TransportSnapshotMixin):
    """``MCP_AUTH_TOKEN`` enables bearer auth; absent leaves the endpoint open."""

    def test_token_set_wires_verifier_and_auth_settings(self):
        with patch.dict(
            os.environ,
            {
                "MCP_TRANSPORT": "streamable-http",
                "MCP_HOST": "127.0.0.1",
                "MCP_PORT": "9090",
                "MCP_AUTH_TOKEN": "secret-xyz",
            },
            clear=False,
        ):
            server.main()
        self.assertIsNotNone(server.mcp._token_verifier)
        self.assertIsNotNone(server.mcp.settings.auth)
        self.assertEqual(server.mcp.settings.auth.required_scopes, [])

    def test_token_absent_leaves_endpoint_open(self):
        with patch.dict(
            os.environ,
            {
                "MCP_TRANSPORT": "streamable-http",
                "MCP_HOST": "127.0.0.1",
                "MCP_PORT": "9090",
            },
            clear=False,
        ):
            os.environ.pop("MCP_AUTH_TOKEN", None)
            server.main()
        self.assertIsNone(server.mcp._token_verifier)
        self.assertIsNone(server.mcp.settings.auth)

    def test_verifier_accepts_only_correct_token(self):
        with patch.dict(
            os.environ,
            {"MCP_AUTH_TOKEN": "correct-token"},
            clear=False,
        ):
            server._apply_http_settings()
        verifier = server.mcp._token_verifier
        self.assertIsNotNone(verifier)

        loop = asyncio.new_event_loop()
        try:
            ok = loop.run_until_complete(verifier.verify_token("correct-token"))
            bad = loop.run_until_complete(verifier.verify_token("wrong-token"))
        finally:
            loop.close()
        self.assertIsNotNone(ok, "correct token must verify")
        self.assertIsNone(bad, "wrong token must not verify")
        # AccessToken carries the token back to the caller
        self.assertEqual(ok.token, "correct-token")
        self.assertEqual(ok.client_id, "mcp-client")
        self.assertEqual(ok.scopes, [])

    def test_auth_only_applied_for_http_transports(self):
        # stdio transport with MCP_AUTH_TOKEN set must NOT install auth — stdio has
        # no HTTP headers, so installing RequireAuthMiddleware would just break.
        with patch.dict(
            os.environ,
            {"MCP_TRANSPORT": "stdio", "MCP_AUTH_TOKEN": "secret"},
            clear=False,
        ):
            server.main()
        self.assertIsNone(server.mcp._token_verifier)
        self.assertIsNone(server.mcp.settings.auth)


class TestEnvFileLoading(_TransportSnapshotMixin):
    """Transport and auth vars are picked up from ``~/.config/paper-search-mcp/.env``.

    Confirms the values flow through :func:`paper_search_mcp.config.get_env` (which
    loads the ``.env`` and honors the ``PAPER_SEARCH_MCP_`` prefix) rather than only
    from the live process environment. This is what makes ``MCP_AUTH_TOKEN`` work
    when set solely in the ``.env`` file with no shell export.
    """

    def _env_patch(self, env_file_path: str):
        """Return a context that points ``PAPER_SEARCH_MCP_ENV_FILE`` at a custom
        path and removes any conflicting live ``MCP_*`` vars, so the test is hermetic
        against the real environment.

        We *delete* the keys rather than setting them to ``""`` because
        :func:`config._load_env_from_file` uses ``os.environ.setdefault``, which only
        writes when the key is *absent* — an empty-string value would block the
        ``.env`` value from landing.
        """
        keys_to_clear = [
            "MCP_TRANSPORT",
            "MCP_HOST",
            "MCP_PORT",
            "MCP_PATH",
            "MCP_AUTH_TOKEN",
            "PAPER_SEARCH_MCP_MCP_TRANSPORT",
            "PAPER_SEARCH_MCP_MCP_HOST",
            "PAPER_SEARCH_MCP_MCP_PORT",
            "PAPER_SEARCH_MCP_MCP_PATH",
            "PAPER_SEARCH_MCP_MCP_AUTH_TOKEN",
        ]
        saved = {k: os.environ.pop(k) for k in keys_to_clear if k in os.environ}
        os.environ["PAPER_SEARCH_MCP_ENV_FILE"] = env_file_path

        class _Restorer:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                os.environ.pop("PAPER_SEARCH_MCP_ENV_FILE", None)
                for k, v in saved.items():
                    os.environ[k] = v

        return _Restorer()

    def test_mcp_transport_read_from_env_file(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False
        ) as tmp:
            tmp.write("MCP_TRANSPORT=streamable-http\n")
            tmp.write("MCP_HOST=10.0.0.1\n")
            tmp.write("MCP_PORT=7777\n")
            tmp.write("MCP_PATH=/papers\n")
            tmp.flush()
            env_path = tmp.name
        try:
            with self._env_patch(env_path):
                config.load_env_file(force=True)
                server.main()
            self.assertEqual(server.mcp.settings.host, "10.0.0.1")
            self.assertEqual(server.mcp.settings.port, 7777)
            self.assertEqual(server.mcp.settings.streamable_http_path, "/papers")
            self.assertEqual(self._calls, [("streamable-http", {})])
        finally:
            Path(env_path).unlink(missing_ok=True)
            config.load_env_file(force=True)

    def test_mcp_auth_token_read_from_env_file_enables_auth(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False
        ) as tmp:
            tmp.write("MCP_TRANSPORT=streamable-http\n")
            tmp.write("MCP_AUTH_TOKEN=secret-from-dotenv\n")
            tmp.flush()
            env_path = tmp.name
        try:
            with self._env_patch(env_path):
                config.load_env_file(force=True)
                server.main()
            self.assertIsNotNone(
                server.mcp._token_verifier,
                "MCP_AUTH_TOKEN in .env must enable bearer auth",
            )
            self.assertIsNotNone(server.mcp.settings.auth)
            # And the verifier accepts the .env-supplied token.
            loop = asyncio.new_event_loop()
            try:
                ok = loop.run_until_complete(
                    server.mcp._token_verifier.verify_token("secret-from-dotenv")
                )
                bad = loop.run_until_complete(
                    server.mcp._token_verifier.verify_token("wrong")
                )
            finally:
                loop.close()
            self.assertIsNotNone(ok)
            self.assertIsNone(bad)
        finally:
            Path(env_path).unlink(missing_ok=True)
            config.load_env_file(force=True)

    def test_empty_mcp_auth_token_in_env_file_keeps_endpoint_open(self):
        # An empty value (or absent key) in the .env must NOT enable auth.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False
        ) as tmp:
            tmp.write("MCP_TRANSPORT=streamable-http\n")
            tmp.write("MCP_AUTH_TOKEN=\n")
            tmp.flush()
            env_path = tmp.name
        try:
            with self._env_patch(env_path):
                config.load_env_file(force=True)
                server.main()
            self.assertIsNone(
                server.mcp._token_verifier,
                "Empty MCP_AUTH_TOKEN in .env must not enable auth",
            )
            self.assertIsNone(server.mcp.settings.auth)
        finally:
            Path(env_path).unlink(missing_ok=True)
            config.load_env_file(force=True)

    def test_prefixed_form_also_works(self):
        # The config loader checks PAPER_SEARCH_MCP_<NAME> first; confirm that the
        # prefixed form of the transport vars also drives _apply_http_settings.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False
        ) as tmp:
            tmp.write("PAPER_SEARCH_MCP_MCP_TRANSPORT=streamable-http\n")
            tmp.write("PAPER_SEARCH_MCP_MCP_AUTH_TOKEN=prefixed-secret\n")
            tmp.flush()
            env_path = tmp.name
        try:
            with self._env_patch(env_path):
                config.load_env_file(force=True)
                server.main()
            self.assertEqual(self._calls, [("streamable-http", {})])
            self.assertIsNotNone(server.mcp._token_verifier)
            loop = asyncio.new_event_loop()
            try:
                ok = loop.run_until_complete(
                    server.mcp._token_verifier.verify_token("prefixed-secret")
                )
            finally:
                loop.close()
            self.assertIsNotNone(ok)
        finally:
            Path(env_path).unlink(missing_ok=True)
            config.load_env_file(force=True)

    def test_live_env_overrides_env_file(self):
        # A value exported in the real environment must win over the .env file
        # (config.py uses os.environ.setdefault, so explicit env wins).
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False
        ) as tmp:
            tmp.write("MCP_TRANSPORT=streamable-http\n")
            tmp.write("MCP_PORT=7777\n")
            tmp.flush()
            env_path = tmp.name
        try:
            with patch.dict(
                os.environ,
                {
                    "PAPER_SEARCH_MCP_ENV_FILE": env_path,
                    "MCP_TRANSPORT": "sse",  # live value should win
                    "MCP_PORT": "8888",
                },
                clear=False,
            ):
                # Clear the prefixed form so it doesn't shadow the live value.
                for k in (
                    "PAPER_SEARCH_MCP_MCP_TRANSPORT",
                    "PAPER_SEARCH_MCP_MCP_PORT",
                ):
                    os.environ.pop(k, None)
                config.load_env_file(force=True)
                server.main()
            self.assertEqual(self._calls, [("sse", {})])
            self.assertEqual(server.mcp.settings.port, 8888)
            self.assertEqual(server.mcp.settings.sse_path, "/sse")
        finally:
            Path(env_path).unlink(missing_ok=True)
            config.load_env_file(force=True)


class TestStaticTokenVerifierContract(unittest.TestCase):
    """The verifier we install satisfies the ``TokenVerifier`` protocol shape."""

    def setUp(self):
        # Build a verifier in isolation without touching the module-level mcp state.
        with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "t"}, clear=False):
            # Capture the class by constructing it via _apply_http_settings, then
            # detach it from the module so this test is hermetic.
            self._orig_tv = server.mcp._token_verifier
            self._orig_auth = server.mcp.settings.auth
            server._apply_http_settings()
            self.verifier = server.mcp._token_verifier

    def tearDown(self):
        server.mcp._token_verifier = self._orig_tv
        server.mcp.settings.auth = self._orig_auth

    def test_verify_token_is_async(self):
        import inspect

        self.assertTrue(inspect.iscoroutinefunction(self.verifier.verify_token))

    def test_returns_none_for_empty_token(self):
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(self.verifier.verify_token(""))
        finally:
            loop.close()
        self.assertIsNone(result)


@pytest.mark.live
class TestHttpTransportLive(unittest.TestCase):
    """Live end-to-end test: boot the server over HTTP and connect via the MCP SDK.

    Skipped automatically when the MCP client SDK isn't importable or no port can be
    bound. Kept separate from the routing tests so the rest of the suite still runs
    on CI environments that block loopback bind. Marked ``@pytest.mark.live`` so it
    can be excluded from fast deterministic runs (e.g. CI publish gate) with
    ``-m "not live"`` while still running in the full suite.
    """

    def setUp(self):
        try:
            from mcp.client.streamable_http import streamablehttp_client  # noqa: F401
        except Exception:
            self.skipTest("mcp.client.streamable_http not available")
        try:
            self.port = _free_port()
        except OSError:
            self.skipTest("no free loopback port available")
        self.base_url = f"http://127.0.0.1:{self.port}/mcp"
        self._thread = None
        self._stop_env_patch = None

    def tearDown(self):
        # The server thread is a daemon and will die with the process; we don't
        # need to (and can't cleanly) stop uvicorn from here.
        pass

    def _start_server(self, auth_token=None):
        env = {
            "MCP_TRANSPORT": "streamable-http",
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(self.port),
        }
        if auth_token is not None:
            env["MCP_AUTH_TOKEN"] = auth_token
        else:
            env.pop("MCP_AUTH_TOKEN", None)
        # Patch env for the server thread (it reads os.environ at main() time).
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Restore mcp state after the thread mutates it. Crucially, also reset
        # ``_session_manager`` — FastMCP creates it lazily on the first
        # ``streamable_http_app()`` call and caches it, so without a reset the
        # second live test in the same process hits
        # ``RuntimeError: StreamableHTTPSessionManager.run() can only be called
        # once per instance``.
        self.addCleanup(self._restore_mcp_state)
        orig = {
            "host": server.mcp.settings.host,
            "port": server.mcp.settings.port,
            "streamable_http_path": server.mcp.settings.streamable_http_path,
            "sse_path": server.mcp.settings.sse_path,
            "auth": server.mcp.settings.auth,
            "token_verifier": server.mcp._token_verifier,
            "transport_security": server.mcp.settings.transport_security,
        }
        self._orig_state = orig
        # Force a fresh session manager on the next streamable_http_app() call.
        server.mcp._session_manager = None

        self._thread = threading.Thread(target=server.main, daemon=True)
        self._thread.start()
        # Give uvicorn a moment to bind.
        time.sleep(2.0)

    def _restore_mcp_state(self):
        server.mcp.settings.host = self._orig_state["host"]
        server.mcp.settings.port = self._orig_state["port"]
        server.mcp.settings.streamable_http_path = self._orig_state["streamable_http_path"]
        server.mcp.settings.sse_path = self._orig_state["sse_path"]
        server.mcp.settings.auth = self._orig_state["auth"]
        server.mcp._token_verifier = self._orig_state["token_verifier"]
        server.mcp.settings.transport_security = self._orig_state["transport_security"]
        # Drop the cached session manager so a later live test can boot fresh.
        server.mcp._session_manager = None

    def test_open_endpoint_accepts_unauthenticated_client(self):
        self._start_server(auth_token=None)
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession

        async def go():
            async with streamablehttp_client(self.base_url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return [t.name for t in result.tools]

        names = asyncio.run(go())
        self.assertIsInstance(names, list)
        self.assertIn("search_papers", names)

    def test_auth_protected_endpoint_rejects_unauthenticated_client(self):
        self._start_server(auth_token="valid-bearer-token")
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession

        async def go_no_token():
            async with streamablehttp_client(self.base_url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

        with self.assertRaises(Exception):
            asyncio.run(go_no_token())

        async def go_wrong_token():
            async with streamablehttp_client(
                self.base_url,
                headers={"Authorization": "Bearer wrong"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

        with self.assertRaises(Exception):
            asyncio.run(go_wrong_token())

    def test_auth_protected_endpoint_accepts_correct_token(self):
        self._start_server(auth_token="valid-bearer-token")
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession

        async def go():
            async with streamablehttp_client(
                self.base_url,
                headers={"Authorization": "Bearer valid-bearer-token"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return [t.name for t in result.tools]

        names = asyncio.run(go())
        self.assertIn("search_papers", names)


if __name__ == "__main__":
    unittest.main()