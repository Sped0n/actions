# Runner image

`ghcr.io/sped0n/runner:latest` supports AMD64 and ARM64, based on `ghcr.io/catthehacker/ubuntu:runner-latest`. It retains the non-root `runner` user and passwordless sudo. UID-map helpers use file capabilities instead of setuid root.

## Publishing

The workflow checks on pushes to `main` affecting `runner-image/**` or its workflow file, Mondays at 03:00 UTC, and manually from the default branch. It skips building and publishing when the upstream multi-architecture digest and Dockerfile are unchanged. Both architectures use the same resolved upstream digest. APT-only updates do not trigger builds.

Each publication updates `latest` and adds a tag containing the full multi-architecture image hash, without the `sha256:` prefix. Per-run staging tags are also retained. The job summary reports both references. Set the GHCR package visibility to public after its first publication.

```text
ghcr.io/sped0n/runner:<image-hash>
docker://ghcr.io/sped0n/runner:latest@sha256:<index-digest>
```

Publishing does not update digest-pinned runner labels. Seccomp/AppArmor policies remain host configuration; user namespaces and subordinate UID/GID ranges are required. Do not enable `no-new-privileges`, which prevents the helpers from gaining their file capabilities.
