import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

RELEASES_PATH = Path(__file__).with_name("releases.json")


def architecture():
    machine = platform.machine()
    if platform.system() != "Linux" or machine not in {"x86_64", "aarch64"}:
        raise ValueError("BuildKit requires a Linux AMD64 or ARM64 runner")
    return {"x86_64": "amd64", "aarch64": "arm64"}[machine]


def boolean_input(name, default):
    value = os.environ.get(name, default)
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def write_outputs(values):
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError("Action outputs cannot contain literal newlines")
            output.write(f"{name}={value}\n")


def prepare():
    arch = architecture()
    boolean_input("INPUT_CACHE_BINARY", "true")
    boolean_input("INPUT_NO_PROCESS_SANDBOX", "false")
    for helper in ("newuidmap", "newgidmap"):
        if shutil.which(helper) is None:
            raise RuntimeError(
                f"Runner image must provide {helper} and subordinate UID/GID mappings"
            )
    manifest_hash = hashlib.sha256(RELEASES_PATH.read_bytes()).hexdigest()
    cache = Path(os.environ["RUNNER_TEMP"]).resolve() / "buildkit-archives"
    write_outputs(
        {
            "cache-path": str(cache),
            "cache-key": f"buildkit-archives-v1-linux-{arch}-{manifest_hash}",
            "script": str(Path(__file__).resolve()),
        }
    )


def archive_matches(path, release):
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size != release["size"]
    ):
        return False
    with path.open("rb") as archive:
        return hashlib.file_digest(archive, "sha256").hexdigest() == release["sha256"]


def download_archive(path, release):
    if archive_matches(path, release):
        return
    path.unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as download:
        temporary = Path(download.name)
        try:
            with urllib.request.urlopen(release["url"], timeout=30) as response:
                remaining = release["size"]
                while chunk := response.read(min(1024 * 1024, remaining + 1)):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ValueError("Release archive exceeds its pinned size")
                    download.write(chunk)
            download.flush()
            if not archive_matches(temporary, release):
                raise ValueError(
                    "Release archive does not match its pinned size and SHA-256"
                )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def install_tools(cache, binary_directory, releases):
    if cache.is_symlink():
        raise ValueError("Binary cache directory cannot be a symlink")
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    members = {
        "buildkit": {
            "bin/buildctl": "buildctl",
            "bin/buildkitd": "buildkitd",
            "bin/buildkit-runc": "buildkit-runc",
        },
        "rootlesskit": {"rootlesskit": "rootlesskit"},
    }
    for tool, expected in members.items():
        archive_path = cache / f"{tool}.tar.gz"
        download_archive(archive_path, releases[tool])
        extracted = set()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                if member.name not in expected:
                    continue
                if not member.isfile() or member.name in extracted:
                    raise ValueError(
                        "Release executable must be a unique regular archive member"
                    )
                destination = binary_directory / expected[member.name]
                with (
                    archive.extractfile(member) as source,
                    destination.open("xb") as output,
                ):
                    shutil.copyfileobj(source, output)
                destination.chmod(0o755)
                extracted.add(member.name)
        if extracted != expected.keys():
            raise ValueError("Release archive is missing required executables")


def input_lines(name):
    return [
        line.strip() for line in os.environ.get(name, "").splitlines() if line.strip()
    ]


def build_command(binary_directory, state):
    context = Path(os.environ.get("INPUT_CONTEXT", ".")).resolve(strict=True)
    raw_file = os.environ.get("INPUT_FILE", "")
    dockerfile = (Path(raw_file) if raw_file else context / "Dockerfile").resolve(
        strict=True
    )
    if not context.is_dir() or not dockerfile.is_file():
        raise ValueError(
            "Build context must be a directory and Dockerfile must be a file"
        )
    outputs = input_lines("INPUT_OUTPUTS")
    if not outputs:
        raise ValueError("At least one output specification is required")
    command = [
        str(binary_directory / "buildctl"),
        "build",
        "--frontend=dockerfile.v0",
        "--progress=plain",
        "--local",
        f"context={context}",
        "--local",
        f"dockerfile={dockerfile.parent}",
        "--opt",
        f"filename={dockerfile.name}",
        "--opt",
        f"platform={os.environ.get('INPUT_PLATFORMS') or 'linux/' + architecture()}",
        "--metadata-file",
        str(state / "metadata.json"),
    ]
    for name, prefix in (
        ("INPUT_BUILD_ARGS", "build-arg:"),
        ("INPUT_LABELS", "label:"),
    ):
        for value in input_lines(name):
            key, separator, _ = value.partition("=")
            if not separator or not key:
                raise ValueError(f"{name} entries must have a nonempty NAME=value")
            command.extend(("--opt", prefix + value))
    for name, option in (
        ("INPUT_OUTPUTS", "--output"),
        ("INPUT_CACHE_FROM", "--import-cache"),
        ("INPUT_CACHE_TO", "--export-cache"),
    ):
        for value in input_lines(name):
            command.extend((option, value))
    return command


@contextlib.contextmanager
def managed_process(command, *, shutdown_timeout):
    process = subprocess.Popen(command, start_new_session=True)
    try:
        yield process
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=shutdown_timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def wait_for_daemon(process, buildctl):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "BuildKit exited during startup; check runner security permissions"
            )
        try:
            result = subprocess.run(
                [str(buildctl), "--timeout=2", "debug", "workers"],
                check=False,
                capture_output=True,
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            pass
        else:
            if result.returncode == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("BuildKit did not become ready within 30 seconds")


def worker(state):
    binaries = state / "bin"
    command = build_command(binaries, state)
    daemon = [
        str(binaries / "buildkitd"),
        "--rootless",
        "--root",
        str(state / "data"),
        "--addr",
        os.environ["BUILDKIT_HOST"],
        "--containerd-worker=false",
        "--oci-worker=true",
        "--oci-worker-net=host",
    ]
    if boolean_input("INPUT_NO_PROCESS_SANDBOX", "false"):
        daemon.append("--oci-worker-no-process-sandbox")
    try:
        with managed_process(daemon, shutdown_timeout=10) as process:
            wait_for_daemon(process, binaries / "buildctl")
            subprocess.run(command, check=True)
    finally:
        # Snapshot files can be owned by subordinate IDs; remove them while still mapped root.
        if (state / "data").exists():
            shutil.rmtree(state / "data")
    metadata = json.loads((state / "metadata.json").read_text())
    if not isinstance(metadata, dict):
        raise TypeError("BuildKit exporter metadata must be a JSON object")
    digest = metadata.get("containerimage.digest", "")
    if not isinstance(digest, str) or (
        digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    ):
        raise ValueError("BuildKit returned an invalid image digest")
    write_outputs(
        {"digest": digest, "metadata": json.dumps(metadata, separators=(",", ":"))}
    )


def build(cache):
    arch = architecture()
    # JavaScript actions receive these variables from the runner; never put tokens in CLI arguments.
    caches = input_lines("INPUT_CACHE_FROM") + input_lines("INPUT_CACHE_TO")
    if any("type=gha" in spec for spec in caches) and (
        not os.environ.get("ACTIONS_RUNTIME_TOKEN")
        or not (
            os.environ.get("ACTIONS_CACHE_URL") or os.environ.get("ACTIONS_RESULTS_URL")
        )
    ):
        raise ValueError(
            "GHA caching requires the runner's runtime cache token and URL"
        )
    state = Path(tempfile.mkdtemp(prefix="buildkit-", dir=os.environ["RUNNER_TEMP"]))
    try:
        binaries = state / "bin"
        binaries.mkdir(mode=0o700)
        # Validate inputs before network access or daemon startup.
        build_command(binaries, state)
        releases = json.loads(RELEASES_PATH.read_text())[arch]
        install_tools(cache, binaries, releases)
        runtime = state / "runtime"
        runtime.mkdir(mode=0o700)
        os.environ["XDG_RUNTIME_DIR"] = str(runtime)
        os.environ["BUILDKIT_HOST"] = f"unix://{runtime}/buildkitd.sock"
        os.environ["PATH"] = str(binaries) + os.pathsep + os.environ.get("PATH", "")
        command = [
            str(binaries / "rootlesskit"),
            "--state-dir",
            str(state / "rootlesskit"),
            sys.executable,
            str(Path(__file__).resolve()),
            "worker",
            str(state),
        ]
        # The worker must have time to stop its daemon and delete mapped-UID storage first.
        with managed_process(command, shutdown_timeout=30) as process:
            result = process.wait()
            if result:
                raise subprocess.CalledProcessError(result, command)
    finally:
        try:
            shutil.rmtree(state)
        except OSError as error:
            print(
                f"Warning: job cleanup must remove BuildKit temporary storage: {error}",
                file=sys.stderr,
            )


def terminate(signum, frame):
    # RootlessKit forwards signals that the worker may also receive from its process group.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def main():
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        if sys.argv[1:] == ["prepare"]:
            prepare()
        elif len(sys.argv) == 3 and sys.argv[1] == "build":
            build(Path(sys.argv[2]))
        elif len(sys.argv) == 3 and sys.argv[1] == "worker":
            worker(Path(sys.argv[2]))
        else:
            raise ValueError(
                "Usage: buildkit_build.py prepare | build CACHE | worker STATE"
            )
    except subprocess.CalledProcessError as error:
        # Cache specifications or build arguments may contain secrets; do not log argv.
        print(
            f"BuildKit command failed with exit code {error.returncode}",
            file=sys.stderr,
        )
        return 1
    except (
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
        tarfile.TarError,
    ) as error:
        print(f"BuildKit action failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
