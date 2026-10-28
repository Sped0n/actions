# Actions

GitHub and Forgejo composite actions. Python 3.11+; standard library only. The BuildKit action also requires Node.js 24.

Forgejo references use `https://github.com/Sped0n/actions/<action>@<SHA>`.

## buildkit-build

```yaml
- uses: Sped0n/actions/buildkit-build@<SHA>
  with:
    outputs: type=image,name=registry.example.com/owner/app:latest,push=true
    cache-from: type=gha,scope=app-amd64
    cache-to: type=gha,scope=app-amd64,mode=max
```

[Inputs and defaults](buildkit-build/action.yml). Outputs: `digest` (when provided by the exporter) and `metadata` (JSON).

- Local build contexts; registry authentication uses existing Docker credentials, including `DOCKER_CONFIG`.
- Outputs and cache specifications are newline-separated. Build arguments and labels use newline-separated `NAME=value` entries; neither is suitable for secrets.
- Rootless BuildKit uses the native snapshotter and inherits job cgroup limits. No host Docker daemon or socket is used.
- Binary caching defaults to enabled. Archive sizes and SHA-256 hashes are verified before extraction, including cache hits. Invalid archives are downloaded again; cache-service failures do not fail the build. Cache entries are immutable and subject to eviction.
- Image caching is independent of binary caching. BuildKit cache backends pass through; `type=gha` requires compatible runner cache endpoints.

### Runner requirements

- Linux AMD64/ARM64, `newuidmap`, `newgidmap`, and subordinate UID/GID ranges for the invoking user.
- User namespaces, mounts, and compatible seccomp/AppArmor policies. Docker's default seccomp profile is insufficient. The action installs no prerequisites or security-policy changes.
- Process sandboxing requires compatible `/proc` mounts and PID namespace permissions. `no-process-sandbox: 'true'` shares the daemon PID namespace: `RUN` processes can signal the daemon; ptrace restrictions depend on seccomp. Trusted Dockerfiles only; default is `'false'`.
- Foreign-platform `RUN` instructions require separately configured emulation.
- Failures and termination trigger daemon cleanup. Forced termination can leave temporary storage; disposable job containers and runner cleanup remain required.

## registry-gc

```yaml
- uses: Sped0n/actions/registry-gc@<SHA>
  with:
    image: git.example.com/owner/app
    token: ${{ secrets.PAT }}
    keep-tag: '123abcd'
    retention-days: '30'
```

[Inputs and defaults](registry-gc/action.yml). Token requires Forgejo package read/write access.

- Retains `latest`, `keep-tag`, and digest-only architecture versions. Age-based cleanup also retains the newest dated tag and tags with missing/invalid timestamps.
- Age uses Forgejo package-version `created_at`. `content-tags-only: 'true'` restricts deletion to seven-character lowercase hexadecimal tags, excluding other cache tags. Required for `retention-days: none`.
- Listing is package-specific and paginated; redirects are rejected. Version IDs are rechecked before deletion, but the API has no atomic compare-and-delete. Publishers and cleanup require shared workflow concurrency; external publishers must not race cleanup.
- Forgejo handles dangling manifests and blob cleanup asynchronously.

## Development

`nix develop`: Python, prek, Ruff, actionlint, and nixfmt.

```text
prek install
prek run --all-files
python3 -W error::ResourceWarning .github/actions/run_tests.py
```

[CI](.github/workflows/test.yml): `ubuntu-latest`, default-shell hooks and Python tests, KVM when available, and Nix-store caching. Tests use fixture executables and loopback servers; no live container builds or registry mutations.

## Pins

- BuildKit/RootlessKit versions, archive sizes, and SHA-256 hashes: [releases.json](buildkit-build/releases.json).
- Dependency action SHAs: [build action](buildkit-build/action.yml) and [CI workflow](.github/workflows/test.yml).
- Release updates: check [BuildKit](https://github.com/moby/buildkit/releases/latest) and [RootlessKit](https://github.com/rootless-containers/rootlesskit/releases/latest), review runner requirements, and update both architectures' URLs, sizes, and hashes together. Runtime downloads never resolve `latest`.
