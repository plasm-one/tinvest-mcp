# vendor/

An optional local install path for the T-Invest gRPC SDK.

You need this only if `uv sync` cannot install `t-tech-investments` from an
index — the package has been served truncated in the past, which surfaces as a
hash or archive error rather than a clean 404.

## Using a vendored wheel

1. Obtain the wheel from T-Bank's documented distribution and **verify it**
   (size and hash) before use. A wheel is executable code that will run in the
   same process as your brokerage token.
2. Put it here: `vendor/t_tech_investments-<version>-py3-none-any.whl`
3. Uncomment the block at the end of `pyproject.toml`:

   ```toml
   [tool.uv.sources]
   t-tech-investments = { path = "vendor/t_tech_investments-1.49.1-py3-none-any.whl" }
   ```

4. `uv sync`

## The other fallback

`tinvest_mcp.sdk` imports `t_tech.invest` and falls back to the legacy
`tinkoff.invest`, which exposes the same API. If that package installs cleanly
for you, it is simpler than vendoring:

```bash
uv pip install tinkoff-investments
```

The `status` tool reports which package actually loaded, in `sdk_package`.

## Why wheels are not committed

`.gitignore` excludes `vendor/*.whl`. A binary wheel is not source: it cannot be
reviewed in a diff, it goes stale silently, and committing one would mean every
user of this repository runs a binary that a maintainer happened to download
once. Fetch and verify your own.
