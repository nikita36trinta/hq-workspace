# Workspace template

Template for a composed **workspace** — a meta-repo that stitches shared
components and per-project repos together with **git submodules**, so one clone
gives you the full picture. Mirrors the conventions of the digiterium workspace.

This template carries only the **shared baseline** every tenant may see (currently the
`pipelines` component, the knowledge base, and the scenario catalog, under `.hq/`).
Tenant-specific projects are added *after* instantiation, never to the template.

`.hq/` holds the shared infrastructure: components (e.g. `pipelines`), a committed
`.hq/knowledge/` base (durable notes/runbooks), a `.hq/scenarios/` catalog of reference
recipes for chaining pipelines into multi-step flows, and `.hq/_runtime/` — the single
canonical location every component writes its runtime output to (artifacts, staged
credentials, scratch). `.hq/_runtime/` is gitignored except for a `.gitkeep`, so the
path always exists after a fresh clone but its contents never get committed.
Multi-pipeline **scenarios** are built from the catalog recipes; each run keeps its
orchestration script, working files, and post-run report together under
`.hq/_runtime/scenarios/<dd-mm-hhmm-slug>/` — see the scenario skill.

## Instantiate a tenant workspace

Create the tenant repo from this template with a **fresh history** — use GitHub's
"Use this template" button, **not** a fork (a fork would carry this repo's
history). Then add the tenant's own submodules in the new repo.

## Access model — read this before adding anything

Two independent mechanisms, do not confuse them:

- **What a tenant can *read*** is enforced by **repo grants on GitHub**. `make
  install` skips any submodule the user can't read (it stays empty) and still
  initializes the rest — graceful degradation, no broken bootstrap.
- **What a tenant *knows exists*** is controlled by **which submodules are
  committed in their repo**. `.gitmodules` is committed, so every submodule's
  name + URL is visible to anyone who can clone that workspace. To keep a project
  hidden from a tenant, **do not add its submodule to their workspace** — that is
  the only way to conceal its existence. Branches and subdirectories do **not**
  hide anything: git read access is per-repository and total.

So: shared baseline → here in the template. Secret/tenant-specific projects →
only in the specific tenant workspace that is allowed to know about them.

## Bootstrap a checkout

```sh
make install     # init every submodule you can access; skip the rest silently,
                 # then sync-skills (see below)
bun install      # pull workspace tooling (@rigid/cli) into node_modules
```

`make install` ends by running `make sync-skills`, which aggregates any
component-owned skills (`.hq/<component>/.agents/skills/`) into the workspace
skill set as flat, prefixed symlinks under `.agents/skills/`. Re-run it after a
component gains or drops a skill. See `.agents/README.md` for the why.

Run pipelines from the workspace root with `bun run pipeline` — the script wraps
the `pipelines` component's own runner and directs its output to `.hq/_runtime/`.
`rigid` is available via `bunx rigid …` or a global install of `@rigid/cli`.

## Layout

```
.hq/                                               # shared infrastructure
.hq/pipelines/                                     # shared component (submodule)
.hq/knowledge/                                      # committed knowledge base (notes, runbooks)
.hq/scenarios/                                     # catalog of reference scenario recipes (pipeline chains)
.hq/_runtime/                                      # gitignored; canonical runtime output dir (incl. scenarios/<slug>/ instances)
projects/<name>/                                   # a project — umbrella for its repo(s)
projects/<name>/worktrees/<branch>/                # single-product: one submodule per long-lived branch
projects/<name>/<product>/worktrees/<branch>/      # multi-product variant: worktrees grouped under each product
```

Shared, org-wide infrastructure components sit under `.hq/`. Project-specific repos
sit under `projects/<name>/`. Anything with an upstream repo is a submodule; for a
project, add one submodule per long-lived branch under `worktrees/<branch>/` (one dir
per branch) so branches coexist on disk. A project that spans **multiple sub-products**
inserts a `<product>/` level first (`projects/<name>/<product>/worktrees/<branch>/`); a
single-product project omits it. Only long-lived branches (e.g. `main`) are promoted to
submodules — local working branches stay uncommitted.

## Add a component or project

```sh
# shared infrastructure component, under .hq/
git submodule add <repo-url> .hq/<component>

# a project repo, one submodule per long-lived branch (start with main)
git submodule add -b main <repo-url> projects/<name>/worktrees/main

# multi-product project: nest the worktrees under the product
git submodule add -b main <repo-url> projects/<name>/<product>/worktrees/main
```
