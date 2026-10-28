import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import registry_gc

NOW = dt.datetime(2026, 10, 27, tzinfo=dt.UTC)


def package(identifier, tag, days, name="app"):
    return {
        "id": identifier,
        "type": "container",
        "name": name,
        "version": tag,
        "created_at": (
            (NOW - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")
            if days is not None
            else None
        ),
    }


class RegistryFixture:
    def __init__(self):
        self.packages = [
            package(1, "latest", 60),
            package(2, "1111111", 60),
            package(3, "2222222", 1),
            package(4, "7777777", 120),
            package(5, "bbbbbbb", 0),
            package(6, "5555555", None),
            package(7, "sha256:" + "a" * 64, 120),
            package(8, "manual-release", 60),
            package(9, "1111111", 120, name="app-other"),
        ]
        self.packages.append(package(10, "3333333", 14))
        self.deleted = set()
        self.replace_tag = None
        self.redirect = False
        self.redirect_followed = False
        self.authorizations = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, payload=None):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if payload is not None:
                    self.wfile.write(json.dumps(payload).encode())

            def do_GET(self):
                fixture.authorizations.append(self.headers.get("Authorization"))
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/leak":
                    fixture.redirect_followed = True
                    self.reply(200, [])
                elif parsed.path == "/api/v1/packages/owner":
                    if fixture.redirect:
                        self.send_response(302)
                        self.send_header("Location", "/leak")
                        self.end_headers()
                        return
                    page = int(urllib.parse.parse_qs(parsed.query)["page"][0])
                    # The server's page limit can be lower than the client's requested limit.
                    self.reply(200, fixture.packages[(page - 1) * 2 : page * 2])
                elif (
                    parsed.path.rsplit("/", 1)[0]
                    == "/api/v1/packages/owner/container/app"
                ):
                    tag = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
                    result = next(
                        p
                        for p in fixture.packages
                        if p["name"] == "app" and p["version"] == tag
                    )
                    if tag == fixture.replace_tag:
                        result = dict(result, id=1000, created_at=NOW.isoformat())
                    self.reply(200, result)
                else:
                    self.reply(404)

            def do_DELETE(self):
                fixture.authorizations.append(self.headers.get("Authorization"))
                path = urllib.parse.urlsplit(self.path).path
                if path.rsplit("/", 1)[0] != "/api/v1/packages/owner/container/app":
                    self.reply(404)
                    return
                tag = urllib.parse.unquote(path.rsplit("/", 1)[-1])
                fixture.deleted.add(tag)
                self.reply(204)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.client = registry_gc.ForgejoPackageClient(
            f"http://127.0.0.1:{self.server.server_port}", "owner", "app", "test-token"
        )
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


POLICIES = [
    (7, False, {"1111111", "3333333", "manual-release"}),
    (30, False, {"1111111", "manual-release"}),
    (None, True, {"1111111", "2222222", "3333333", "5555555", "bbbbbbb"}),
]


class RegistryCleanupTests(unittest.TestCase):
    def cleanup(self, fixture, days=30, content_only=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return registry_gc.cleanup(
                fixture.client,
                "7777777",
                NOW,
                retention_days=days,
                content_tags_only=content_only,
            )

    def test_pagination_retention_and_manifest_safety(self):
        for days, content_only, expected in POLICIES:
            with (
                self.subTest(days=days, content_only=content_only),
                RegistryFixture() as fixture,
            ):
                self.assertEqual(self.cleanup(fixture, days, content_only), 0)
                self.assertEqual(fixture.deleted, expected)
                self.assertTrue(
                    all(value == "token test-token" for value in fixture.authorizations)
                )

    def test_changed_tag_is_not_deleted(self):
        for days, content_only, expected in POLICIES:
            with (
                self.subTest(days=days, content_only=content_only),
                RegistryFixture() as fixture,
            ):
                fixture.replace_tag = "1111111"
                self.cleanup(fixture, days, content_only)
                self.assertEqual(fixture.deleted, expected - {"1111111"})

    def test_newest_tag_survives_even_when_every_tag_is_old(self):
        with RegistryFixture() as fixture:
            for entry in fixture.packages:
                if entry["created_at"] is not None:
                    age = 31 if entry["version"] == "bbbbbbb" else 60
                    entry["created_at"] = (NOW - dt.timedelta(days=age)).isoformat()
            self.cleanup(fixture)
            self.assertEqual(
                fixture.deleted, {"1111111", "2222222", "3333333", "manual-release"}
            )

    def test_redirect_is_rejected_before_credentials_are_forwarded(self):
        with RegistryFixture() as fixture:
            fixture.redirect = True
            with self.assertRaises(urllib.error.HTTPError):
                self.cleanup(fixture)
            self.assertFalse(fixture.redirect_followed)
            self.assertFalse(fixture.deleted)

    def test_age_retention_cannot_be_disabled_for_arbitrary_tags(self):
        with RegistryFixture() as fixture:
            with self.assertRaisesRegex(ValueError, "content-tags-only"):
                self.cleanup(fixture, days=None)
            self.assertFalse(fixture.deleted)

    def test_cli_rejects_invalid_inputs_without_logging_token(self):
        for changes in (
            {"GC_RETENTION_DAYS": "0"},
            {"GC_RETENTION_DAYS": "invalid"},
            {"GC_RETENTION_DAYS": "999999999"},
            {"GC_RETENTION_DAYS": "none"},
            {"GC_CONTENT_TAGS_ONLY": "maybe"},
            {"FORGEJO_TOKEN": "secret-token\n"},
        ):
            with self.subTest(changes=changes):
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).with_name("registry_gc.py"))],
                    check=False,
                    env={
                        **os.environ,
                        "IMAGE_NAME": "example.invalid/owner/app",
                        "KEEP_TAG": "7777777",
                        "FORGEJO_TOKEN": "secret-token",
                        "GC_RETENTION_DAYS": "30",
                        "GC_CONTENT_TAGS_ONLY": "false",
                        **changes,
                    },
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("Registry cleanup failed:", result.stderr)
                self.assertNotIn("secret-token", result.stderr)


if __name__ == "__main__":
    unittest.main()
