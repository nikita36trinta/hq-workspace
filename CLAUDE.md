# This workspace

A composed **workspace**: one clone stitches together shared, org-wide
infrastructure and the per-project repositories you're working on, so the whole
picture lives in a single tree. Composition is by **git submodules** — every
part that has its own upstream is a submodule pinned to a commit, and the
top-level repo only records which parts exist and at what version. Treat this
repo as a manifest of other repos, not as a place to write code directly.

## Layout, by role

Two regions, both filled in by submodules:

- **Shared infrastructure** — components any project may draw on. Chief among
  them: a **pipelines** component (a catalog of purpose-built, runnable
  automations), a **knowledge base** (a compiled, queryable store of curated
  facts), a **scenario catalog** (reference recipes for chaining pipelines into
  multi-step flows), and one **runtime directory** for everything ephemeral —
  artifacts, staged credentials, scratch. The runtime directory is gitignored:
  never commit into it, and never assume its contents survive across machines.
- **Projects** — each is an umbrella for one repo's **long-lived branches, one
  submodule per branch**, so multiple branches coexist on disk. Only long-lived
  branches are promoted to submodules; short-lived working branches stay
  uncommitted.

Exact directory names live in the submodule manifest and the README — read those
rather than hard-coding paths, since they can move.

## Reach for a pipeline before doing it by hand

The habit that matters most here. When a request sounds generic or routine —
"transcribe this", "summarize that", "produce a brief", "come up with X" —
**assume a purpose-built pipeline may already exist for it, and look before
improvising.** The pipelines component is a catalog of exactly such tasks,
packaged to run reproducibly with the right models, tools, and guardrails.

So the first move for any non-trivial, repeatable-sounding request is to
**explore the pipeline catalog** — the workspace provides skills to explore,
run, and manage pipelines, and exploring is read-only and cheap.

**Exploring is not the goal — running is.** If the catalog has a pipeline (or a
chain of them) that produces the deliverable, you must **execute it through the
runner**; do not reproduce its output by hand, even when you easily could.
Hand-producing what a pipeline already makes throws away its reproducibility,
guardrails, and provenance — and your by-hand version is a worse, less
consistent one. So "fall back to ad-hoc only after nothing fits" means **the
catalog has no pipeline for this deliverable** — it never means "I could just do
it myself faster." Being able to do the task by hand is not a reason to skip the
pipeline; it is exactly the temptation the pipeline exists to remove. A pipeline
labelled a test or example still counts: if it produces the right kind of
output, run it.

Only if nothing in the catalog genuinely fits do you do it by hand — and if you
keep doing the same generic task by hand, that's the signal to add it as a
pipeline.

## When one pipeline isn't enough: scenarios

Some requests need *several* pipelines run together — in sequence, sometimes
with independent branches in parallel — or a larger task where a chain of
pipelines does part of the work. That composition is a **scenario**. A catalog
of reference recipes records flows that have worked before, each with a diagram
of what runs in parallel versus in sequence — consult it first, the same way you
reach for an existing pipeline. To build one: plan the steps, then script them
so the run is **resumable**, meaning a step that fails can be retried without
redoing the ones that already succeeded. The script and its working files live
in the workspace's ephemeral runtime area, isolated per run. When the scenario
finishes — or once you've nursed it to working — leave a short **report** of
what actually happened beside it, including every fix and workaround, and distil
a proven flow back into the recipe catalog. Scenarios are expected to be rough
the first time; those reports and recipes are how they harden into reliable
runbooks, and a flow you reach for repeatedly is a candidate to graduate into a
single purpose-built pipeline. The workspace provides a skill for this — prefer
it over re-improvising the orchestration.

## Use the provided skills

The workspace surfaces agent skills for its routine operations — among them
exploring/running/managing pipelines, querying or ingesting into the knowledge
base, and adding projects or worktrees (check your available skills for the
current set). Prefer them over re-deriving a workflow: they encode the current,
correct way and are written to stay current. Some skills are owned by the
component they describe and merely surfaced here — treat the component's own copy
as the source of truth.

## This file defers to living docs

Specifics — commands, flags, schemas, directory names, conventions — live next
to the things they describe and are kept current there: each component's own
orientation doc and README, each tool's own help and self-describing schema
output, and this workspace's README. This file deliberately doesn't restate them
so it can't fall out of sync. When you need a specific, read the source beside
it; when a tool can describe itself, ask it instead of trusting memory.

## Visibility is a submodule property — a real gotcha

Adding a submodule **commits its URL into the manifest**, which is visible to
anyone who can clone the workspace. Git read access is per-repository and total;
branches and subdirectories conceal nothing. So the only way to keep a project
hidden from an audience is to **not add its submodule to their workspace**. What
someone can *read* is enforced by repository grants (an inaccessible submodule
just stays empty after bootstrap); what they *know exists* is decided by which
submodules are committed. Don't conflate the two, and never add a sensitive repo
to a workspace whose audience shouldn't know it exists.

## Operations live in the Makefile

The workspace's `Makefile` is the entry point for workspace-level operations —
notably bootstrapping a fresh clone (initializing every submodule you can
access, silently skipping the rest) and aggregating component-owned skills into
the workspace skill set. Read it for the current targets rather than memorizing
them; it is the living source of truth for *how* to operate the workspace, the
same way each tool's help is for its own surface.

A fresh clone has empty submodules until bootstrapped, so run the bootstrap
target after cloning and after adding any submodule.
