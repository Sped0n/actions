# registry-gc

Forgejo container tag cleanup. Python 3.11+; standard library only.

```yaml
- uses: https://github.com/Sped0n/actions/registry-gc@<SHA>
  with:
    image: git.example.com/owner/app
    token: ${{ secrets.PAT }}
    keep-tag: '123abcd'
    retention-days: '30'
```

[Inputs and defaults](action.yml). Token requires Forgejo package read/write access.

- Retains `latest`, `keep-tag`, and digest-only architecture versions. Age-based cleanup also retains the newest dated tag and tags with missing/invalid timestamps.
- Age uses Forgejo package-version `created_at`. `content-tags-only: 'true'` restricts deletion to seven-character lowercase hexadecimal tags, excluding other cache tags. Required for `retention-days: none`.
- Listing is package-specific and paginated; redirects are rejected. Version IDs are rechecked before deletion, but the API has no atomic compare-and-delete. Publishers and cleanup require shared workflow concurrency; external publishers must not race cleanup.
- Forgejo handles dangling manifests and blob cleanup asynchronously.
