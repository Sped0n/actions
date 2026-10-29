# actions

## Actions

```text
.
├── buildkit-build/   # Rootless BuildKit image builds
├── registry-gc/      # Forgejo container tag cleanup
└── sg_rules/         # Composite action URL policy
```

## Development

```text
prek install
prek run --all-files
python3 -W error::ResourceWarning .github/actions/run_tests.py
```
