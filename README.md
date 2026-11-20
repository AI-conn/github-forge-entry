# Build a GitHub-compatible git forge

[![Build a GitHub-compatible git forge on make.fun](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/AI-conn/github-forge-entry/rank.svg)](https://make.fun/bounty/github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/AI-conn/github-forge-entry/reward.svg)](https://make.fun/bounty/github-core-clone-01)

A standalone GitHub-compatible git forge in a single Python stdlib file: git smart HTTP (protocol v0 + v2), REST v3, GraphQL, and MCP surfaces, with deterministic state.

## Run it locally

```sh
python3 main.py
```

---

An entry to [Build a GitHub-compatible git forge](https://make.fun/bounty/github-core-clone-01) on [make.fun](https://make.fun).


## What this is

A standalone GitHub-compatible git forge in one file, `main.py`, Python
standard library only — no dependencies, no git binary, no network access
beyond the trial lifecycle endpoints.

Surfaces:

- **Git smart HTTP** at `/OWNER/REPO.git` — upload-pack protocol v2
  (ls-refs, fetch incl. shallow/deepen, annotated tags) and receive-pack
  protocol v0 with report-status.
- **REST v3** under `/api/v3` — viewer, repos CRUD, branches, commits,
  contents, git refs, labels, issues + comments, pull requests with merge
  and squash (3-way tree merge).
- **GraphQL** at `/api/graphql` — repository queries and mutations.
- **MCP** at `/mcp` — six tools: create_repo, get_repo, create_issue,
  list_issues, create_pull_request, merge_pull_request.

## Trial contract

The process binds an ephemeral port (never 8080), POSTs `{"port": N}` to
`$MAKEFUN_ENV_URL/register`, serves until the environment answers
`{"done": true}` on the `/done` long-poll, then exits 0. API calls require
`Authorization: Bearer <$MAKEFUN_SHOP_TOKEN>`; git smart-HTTP and
`/healthz` are open. All timestamps derive from `MAKEFUN_TRIAL_NOW` plus
per-object sequence counters — no wall clock, no unseeded randomness — so
replayed trials reproduce identical state.
