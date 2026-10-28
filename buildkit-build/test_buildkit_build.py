import contextlib
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import buildkit_build

DIGEST = "sha256:" + "a" * 64
DAEMON = r"""
import json, os, signal, socket, subprocess, sys, time
from pathlib import Path
state = Path(os.environ["TEST_STATE"])
(state / "data").mkdir()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
(state / "pids.json").write_text(json.dumps([os.getpid(), child.pid]))
(state / "daemon-args.json").write_text(json.dumps(sys.argv[1:]))
def stop(*args):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
try:
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(os.environ["BUILDKIT_HOST"].removeprefix("unix://"))
        server.listen()
        while True:
            connection, _ = server.accept()
            connection.close()
finally:
    (state / "stopping").touch()
    if os.environ.get("DELAY_STOP"):
        time.sleep(0.3)
    child.terminate()
    child.wait()
    (state / "stopped").touch()
"""
CLIENT = r"""
import json, os, socket, sys, time
from pathlib import Path
state = Path(os.environ["TEST_STATE"])
if "workers" in sys.argv:
    with socket.socket(socket.AF_UNIX) as client:
        try:
            client.connect(os.environ["BUILDKIT_HOST"].removeprefix("unix://"))
        except OSError:
            raise SystemExit(1)
else:
    (state / "build-args.json").write_text(json.dumps(sys.argv[1:]))
    if os.environ.get("SLEEP_BUILD"):
        time.sleep(60)
    if os.environ.get("FAIL_BUILD"):
        raise SystemExit(23)
    metadata = {"containerimage.digest": "sha256:" + "a" * 64, "note": "value\ninjected=oops"}
    Path(sys.argv[sys.argv.index("--metadata-file") + 1]).write_text(json.dumps(metadata))
"""


def archive_bytes(members):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, contents in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
    return data.getvalue()


@contextlib.contextmanager
def archive_server(payload):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield (
                {
                    "url": f"http://127.0.0.1:{server.server_port}/archive",
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
                requests,
            )
        finally:
            server.shutdown()
            thread.join()


class BuildKitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_archive_cache_is_reverified_and_corruption_is_replaced(self):
        archive = self.root / "buildkit.tar.gz"
        outside = self.root / "private-file"
        outside.write_bytes(b"unchanged")
        archive.symlink_to(outside)
        with archive_server(b"verified archive") as (release, requests):
            buildkit_build.download_archive(archive, release)
            self.assertEqual(outside.read_bytes(), b"unchanged")
            buildkit_build.download_archive(archive, release)
            self.assertEqual(len(requests), 1)
            archive.write_bytes(b"corrupted cached archive")
            buildkit_build.download_archive(archive, release)
            self.assertEqual(archive.read_bytes(), b"verified archive")
            bad_release = {**release, "sha256": "0" * 64}
            with self.assertRaises(ValueError):
                buildkit_build.download_archive(archive, bad_release)
            self.assertFalse(archive.exists())

    def test_cache_directory_symlink_cannot_redirect_installation(self):
        outside = self.root / "outside"
        outside.mkdir()
        cache = self.root / "cache"
        cache.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            buildkit_build.install_tools(cache, self.root, {})
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_executable_is_rejected_without_writing_its_target(self):
        data = io.BytesIO()
        target = self.root / "outside"
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            member = tarfile.TarInfo("bin/buildctl")
            member.type = tarfile.SYMTYPE
            member.linkname = str(target)
            archive.addfile(member)
        binaries = self.root / "bin"
        binaries.mkdir()
        with (
            archive_server(data.getvalue()) as (release, _),
            self.assertRaises(ValueError),
        ):
            buildkit_build.install_tools(
                self.root / "cache", binaries, {"buildkit": release}
            )
        self.assertFalse(target.exists())
        self.assertEqual(list(binaries.iterdir()), [])

    def test_verified_release_members_install_without_archive_permissions(self):
        buildkit = archive_bytes(
            {
                "bin/buildctl": b"client",
                "bin/buildkitd": b"daemon",
                "bin/buildkit-runc": b"runtime",
                "../outside": b"not extracted",
            }
        )
        rootlesskit = archive_bytes({"rootlesskit": b"namespace helper"})
        binaries = self.root / "bin"
        binaries.mkdir()
        with (
            archive_server(buildkit) as (release, _),
            archive_server(rootlesskit) as (rootless, _),
        ):
            buildkit_build.install_tools(
                self.root / "cache",
                binaries,
                {"buildkit": release, "rootlesskit": rootless},
            )
        self.assertFalse((self.root / "outside").exists())
        self.assertEqual((binaries / "buildctl").read_bytes(), b"client")
        self.assertTrue(os.access(binaries / "rootlesskit", os.X_OK))

    def worker_environment(self, **extra):
        binaries = self.root / "bin"
        binaries.mkdir()
        for name, source in (("buildctl", CLIENT), ("buildkitd", DAEMON)):
            executable = binaries / name
            executable.write_text(f"#!{sys.executable}\n" + source)
            executable.chmod(0o755)
        (self.root / "Dockerfile").write_text("FROM scratch\n")
        return {
            **os.environ,
            "TEST_STATE": str(self.root),
            "BUILDKIT_HOST": f"unix://{self.root}/buildkit.sock",
            "GITHUB_OUTPUT": str(self.root / "outputs"),
            "INPUT_CONTEXT": str(self.root),
            "INPUT_FILE": "",
            "INPUT_OUTPUTS": "type=image,name=example.test/app,push=true",
            "INPUT_CACHE_FROM": "type=gha,scope=app-amd64",
            "INPUT_CACHE_TO": "type=gha,scope=app-amd64,mode=max",
            "INPUT_BUILD_ARGS": "MESSAGE=a b; touch injected",
            "INPUT_LABELS": "version=one two",
            "INPUT_NO_PROCESS_SANDBOX": "false",
            **extra,
        }

    def worker_command(self):
        return [
            sys.executable,
            str(Path(buildkit_build.__file__)),
            "worker",
            str(self.root),
        ]

    def assert_worker_stopped(self):
        self.assertTrue((self.root / "stopped").exists())
        self.assertFalse((self.root / "data").exists())
        for pid in json.loads((self.root / "pids.json").read_text()):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_worker_exports_metadata_and_cleans_up_daemon_and_children(self):
        result = subprocess.run(
            self.worker_command(),
            env=self.worker_environment(),
            check=False,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = dict(
            line.split("=", 1)
            for line in (self.root / "outputs").read_text().splitlines()
        )
        self.assertEqual(output["digest"], DIGEST)
        self.assertEqual(json.loads(output["metadata"])["note"], "value\ninjected=oops")
        self.assertNotIn("injected", output)
        arguments = json.loads((self.root / "build-args.json").read_text())
        self.assertIn("type=gha,scope=app-amd64,mode=max", arguments)
        self.assertIn("build-arg:MESSAGE=a b; touch injected", arguments)
        self.assertFalse((self.root / "injected").exists())
        self.assertNotIn(
            "--oci-worker-no-process-sandbox",
            json.loads((self.root / "daemon-args.json").read_text()),
        )
        self.assert_worker_stopped()

    def test_build_failure_cleans_up_without_echoing_secret_arguments(self):
        env = self.worker_environment(
            FAIL_BUILD="true",
            INPUT_NO_PROCESS_SANDBOX="true",
            INPUT_CACHE_FROM="type=registry,token=private-value",
        )
        result = subprocess.run(
            self.worker_command(),
            env=env,
            check=False,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("private-value", result.stdout + result.stderr)
        self.assertIn(
            "--oci-worker-no-process-sandbox",
            json.loads((self.root / "daemon-args.json").read_text()),
        )
        self.assert_worker_stopped()

    def test_termination_stops_daemon_and_its_children(self):
        with subprocess.Popen(
            self.worker_command(),
            env=self.worker_environment(SLEEP_BUILD="true", DELAY_STOP="true"),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            try:
                deadline = time.monotonic() + 10
                while not (self.root / "build-args.json").exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail("Worker did not start the build")
                    time.sleep(0.02)
                process.terminate()
                while not (self.root / "stopping").exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail("Worker did not begin daemon cleanup")
                    time.sleep(0.01)
                process.terminate()
                _, errors = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, errors)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        self.assert_worker_stopped()

    def test_missing_runtime_credentials_fail_before_downloading(self):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ACTIONS_")
        }
        env.update(INPUT_CACHE_FROM="type=gha,scope=app", RUNNER_TEMP=str(self.root))
        result = subprocess.run(
            [
                sys.executable,
                str(Path(buildkit_build.__file__)),
                "build",
                str(self.root / "cache"),
            ],
            env=env,
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime cache token and URL", result.stderr)
        self.assertEqual(list(self.root.iterdir()), [])
