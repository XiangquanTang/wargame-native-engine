"""Prefix-stripping notebook proxy contract; no public network listener needed."""

import base64
import hashlib
import re
import shutil
import subprocess
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from wargame_native_engine.gateway import CommandGatewayError
from wargame_native_engine.viewer.realtime_control_server import (
    RealtimeDualControlRequestHandler,
)
from wargame_native_engine.viewer.server import ReplayViewerHTTPServer
from wargame_native_engine.viewer.static_resources import VIEWER_CONTENT_SECURITY_POLICY


@contextmanager
def serving(server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def proxy_for(upstream):
    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            self._forward()

        def do_POST(self):
            self._forward()

        def _forward(self):
            prefix = "/notebook/proxy/8003"
            if not self.path.startswith(prefix + "/"):
                self.send_error(404)
                return
            try:
                body = None
                if self.command == "POST":
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                request = Request(
                    upstream + self.path[len(prefix) :],
                    data=body,
                    headers={
                        "Content-Type": self.headers.get(
                            "Content-Type", "application/json"
                        )
                    },
                    method=self.command,
                )
                response = urlopen(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                self.send_response(response.code)
                self.send_header("Content-Type", response.headers.get("Content-Type"))
                for name in ("Content-Security-Policy", "X-Content-Type-Options"):
                    if response.headers.get(name):
                        self.send_header(name, response.headers[name])
                self.end_headers()
                self.wfile.write(response.read())

        def log_message(self, *_args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Proxy)


class ViewerProxyTests(unittest.TestCase):
    def assert_bootstrap_allowed(self, payload, policy):
        self.assertIsNotNone(policy)
        self.assertNotIn("unsafe-inline", policy)
        scripts = re.findall(r"<script>(.*?)</script>", payload.decode(), re.DOTALL)
        self.assertEqual(len(scripts), 1)
        digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
        self.assertIn("'sha256-" + digest + "'", policy)

    def test_all_page_bootstraps_are_explicitly_authorized_by_csp(self):
        static = (
            Path(__file__).resolve().parents[1]
            / "src/wargame_native_engine/viewer/static"
        )
        for name in ("index.html", "live_index.html", "dual_control_index.html"):
            self.assert_bootstrap_allowed(
                (static / name).read_bytes(), VIEWER_CONTENT_SECURITY_POLICY
            )

    @unittest.skipUnless(
        shutil.which("node"), "Node.js is needed for browser URL contract"
    )
    def test_browser_mount_derivation(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("viewer_proxy_urls.cjs"))],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_replay_html_assets_and_api_through_proxy(self):
        view = SimpleNamespace(replay_record=lambda: {"frames": 1})
        with (
            serving(ReplayViewerHTTPServer(("127.0.0.1", 0), view)) as upstream,
            serving(proxy_for(upstream)) as proxy,
        ):
            for base in (upstream, proxy + "/notebook/proxy/8003"):
                for route in (
                    "/",
                    "/static/viewer_urls.js",
                    "/static/app.js",
                    "/static/styles.css",
                    "/static/operator_registry_v1.json",
                    "/api/replay",
                ):
                    with urlopen(base + route, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        payload = response.read()
                        self.assertTrue(payload)
                        if route == "/":
                            self.assert_bootstrap_allowed(
                                payload, response.headers.get("Content-Security-Policy")
                            )

    def test_seat_token_boundaries_unchanged_through_proxy(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), RealtimeDualControlRequestHandler
        )

        def binding(token):
            if token not in {"red-secret", "blue-secret"}:
                raise CommandGatewayError("invalid_token", "invalid token")
            return token.split("-")[0], object()

        server.binding = binding
        server.status = lambda side, _binding: {"side": side}
        with (
            serving(server) as upstream,
            serving(proxy_for(upstream)) as proxy,
        ):
            for base in (upstream, proxy + "/notebook/proxy/8003"):
                for route in (
                    "/red?token=red-secret",
                    "/blue?token=blue-secret",
                    "/static/viewer_urls.js",
                    "/api/product/status?token=red-secret",
                ):
                    with urlopen(base + route, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        if route.startswith(("/red?", "/blue?")):
                            self.assert_bootstrap_allowed(
                                response.read(),
                                response.headers.get("Content-Security-Policy"),
                            )
                for route in (
                    "/red?token=blue-secret",
                    "/blue?token=red-secret",
                    "/red",
                    "/api/product/status?token=bad",
                ):
                    with self.assertRaises(HTTPError):
                        urlopen(base + route, timeout=5)
