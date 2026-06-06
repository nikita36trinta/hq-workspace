---
name: add-project
description: Use when the user wants to add a project to this workspace, clone/onboard an existing repo as a project, or add another worktree of a project's repo (e.g. a long-lived feature branch). Triggers on "add a project", "clone <repo> into the workspace", "onboard <repo>", "add a worktree for <branch>", "add the <feature> branch as a worktree", "set up a feature branch", and similar.
allowed-tools: Read, Glob, Grep, Bash(git submodule *), Bash(git status*), Bash(git add *), Bash(make install*)
---

# Add a project or worktree

Projects in this workspace are composed from **git submodules** — one submodule
per long-lived branch — under:

    projects/<name>/worktrees/<branch>/

e.g. `projects/acme/worktrees/main`, `projects/acme/worktrees/feature-billing`.
Each worktree directory is a separate submodule clone of the same repo pinned to
a different branch, so branches coexist on disk.

A project that spans **multiple sub-products** inserts a `<product>/` level first
(`projects/<name>/<product>/worktrees/<branch>/`); a single-product project omits
it. The README says which layout a given project uses — match the siblings.

## Read the workspace conventions first

The workspace `README.md` (beside `.gitmodules` at the workspace root — Glob for
it) is the living source of truth for both the layout and the **access /
visibility model**. Read it before adding anything — don't rely on a copy here,
it can change.

The access model is the part that bites: a submodule's URL is committed to
`.gitmodules` and becomes visible to everyone who can clone this workspace. So
**never add a repo the workspace's audience shouldn't even know exists** — to
hide a project, you simply don't add its submodule here. The README explains why
branches and subdirectories do not hide anything.

## Gather the inputs (ask or infer, then confirm)

- **repo URL** — the git remote to add. Don't assume an org or host; take it
  from the user or an existing worktree of the same project.
- **project name** `<name>` — the `projects/<name>/` umbrella. Default to the
  repo name unless told otherwise.
- **branch** — which branch this worktree tracks: `main` for a project's first
  worktree, or a long-lived feature branch for an additional one.

## Add it

A worktree is just a submodule pinned to a branch, at the conventional path:

    git submodule add -b <branch> <repo-url> projects/<name>/worktrees/<branch>

- **New project / clone a repo** → its first worktree is usually `main`.
- **Another worktree** of an existing project → reuse the same `<repo-url>`,
  pick a new `<branch>` and matching path; existing worktrees stay untouched
  (look at a sibling `worktrees/*` entry to copy the URL).

Then initialize it (or run the workspace bootstrap, which inits every submodule
it can access — see the `Makefile`'s install target):

    git submodule update --init --recursive projects/<name>/worktrees/<branch>

## Long-lived branches only

Only **long-lived** branches are promoted to committed worktree submodules.
Short-lived local working branches stay uncommitted — don't add a submodule for
a throwaway branch.

## Wrap up

`git submodule add` stages `.gitmodules` plus the new submodule. Show the user
the staged change and let them commit — don't commit or push on their behalf
unless asked. If a submodule can't be cloned (no read access), that's the access
model working as intended: report it rather than forcing it.
