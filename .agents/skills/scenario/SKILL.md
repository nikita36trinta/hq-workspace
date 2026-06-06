---
name: scenario
description: Use whenever delivering what the user asked for takes more than one pipeline working together — whether they spell out the steps ("do A then B", "run these in order", "build a workflow that …") or phrase it as one plain request whose outcome happens to need several pipelines ("turn this call into a brief", "from this recording give me a summary plus action items", "take X and produce Y", "sort this whole thing out"). Both forms trigger it equally; recognize a scenario from the GOAL, not the wording: if no single pipeline fully produces the deliverable (you would run one pipeline, then feed its output into another, sometimes with parallel branches), it is a scenario. Also fits when several existing pipelines together handle a meaningful part of a larger task. Reach for this BEFORE running pipelines ad hoc. For something a single pipeline fully covers, use the run skill; to discover what pipelines exist, use the explore skill.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
---

# Compose a scenario — a chain of pipelines

A **scenario** wires existing pipelines into one multi-step run: steps in
sequence, independent steps in parallel, with later steps consuming earlier
outputs. Reach for it whenever a request needs more than one pipeline, or when a
chain of pipelines handles part of a bigger task.

Discovering the right pipelines is step one, not the finish line: a scenario
means you then **actually run them** through `scenario.ts` — never hand-produce
their outputs just because you could. If two catalog pipelines together make the
deliverable, running them is the job.

Not every multi-step ask is a scenario: a single pipeline → use the `run` skill.
Build a scenario only when **two or more** pipeline runs must be coordinated.

## 1. Check the catalog, then plan

1. Look in the **scenario catalog** (`.hq/scenarios/`) for a reference recipe
   that already describes this flow — the same "reach for what exists first"
   habit you'd apply to a single pipeline. If one fits, its mermaid diagram is
   your blueprint: it tells you which steps are sequential and which run in
   parallel. Reuse it instead of re-deriving the shape. (See that directory's
   README for what an entry looks like.)
2. Discover the pipelines and their input/output contracts with the `explore`
   skill — don't assume ids or shapes. The catalog gives the flow; the schemas
   give the details.
3. Map the goal to an **ordered** list of steps: which pipeline each step runs,
   what input it takes, where its output goes, and which steps depend on a prior
   step's output (the rest run in parallel).
4. Show the user the plan before kicking off anything expensive.

## 2. Give the scenario a home

Everything for one scenario lives in its own directory under the workspace's
ephemeral runtime area:

    .hq/_runtime/scenarios/<dd-mm-hhmm-slug>/

(`.hq/_runtime/` is the workspace's gitignored runtime directory — if it has
moved, the README / `.gitignore` names the current one.) Use day-month, then the
hour-minute it started, then a short kebab slug, e.g.
`31-05-1437-call-to-published-summary`. The time matters: the run name each step
derives from this slug is keyed to it, so a timestamp keeps a second run of the
*same* flow on the *same* day from colliding with the first — both as a
directory and in the artifact store. This dir is yours: put the script,
intermediate outputs, and the report here. It's ephemeral — never rely on it
surviving across machines, and never commit into it.

## 3. Write `scenario.ts` so it can resume

A scenario *will* fail partway the first few times. Write the script so
re-running it **skips the steps that already succeeded** and continues from the
one that failed — the cheapest possible resume mechanism. Write it as a **bun
script**, not shell: scenarios feed pipelines JSON input and thread one step's
output into the next, and building/escaping JSON in a real language beats the
quoting games shell forces on you. The pattern:

- **Shell out to the runner; don't import its internals.** Invoke pipelines
  through `bun run pipeline …` via Bun's `$` — the same indirection the `run`
  skill uses — so the runner stays in charge of resolution, images, and nix, and
  the script keeps working regardless of layout. Reaching into the runner's code
  couples the scenario to internals that rot.
- **Build inputs as real objects and `JSON.stringify` them.** Never hand-assemble
  input JSON as strings — that's the whole reason this is a bun script and not a
  shell one.
- **Bun's `$` throws on a non-zero exit** (your `set -e`), and an uncaught throw
  at top level exits non-zero — so a failed step naturally aborts the run.
- **Checkpoint each step**: write a marker on success; skip the step on re-run if
  its marker exists. A failed step leaves no marker, so the next run retries
  exactly there.
- **Idempotent steps**: writing into a fresh per-step output path keeps re-runs
  safe.
- **Run independent steps in parallel** with `Promise.all`, then continue.
  Checkpoint each branch separately so a re-run only retries the branch that
  failed.
- **Give each pipeline run a unique, stable run name.** Artifacts are keyed by
  run name and the unset default is shared, so without this, parallel branches,
  other scenarios, and re-runs silently overwrite each other's outputs. The run
  name is part of the pipeline **input** (not a flag) — set it from this scenario
  plus the step (e.g. `<slug>-<step>`): unique across steps/branches/scenarios,
  yet **stable across re-runs** so a resumed step reuses its own artifact dir
  rather than spawning a new one. (See the `run` skill on run names; confirm the
  exact input field from the pipeline's own `--print-schema`.)

A minimal, adaptable skeleton (the steps are yours to fill from the plan):

    #!/usr/bin/env bun
    import { $ } from "bun";
    import { existsSync, mkdirSync } from "node:fs";

    const DIR = import.meta.dir;                       // this scenario's own dir
    const SLUG = DIR.split("/").pop();                 // its <dd-mm-hhmm-slug>
    const WS = (await $`git rev-parse --show-toplevel`.text()).trim();
    mkdirSync(`${DIR}/state`, { recursive: true });
    mkdirSync(`${DIR}/outputs`, { recursive: true });

    // Exit code the runner uses when a pipeline pauses at a human-decision gate
    // (see §5). NOT a failure — the run produced everything up to the gate and
    // printed how to resume. The runner documents this code; treat it specially.
    const GATE_PENDING = 75;
    class GatePaused extends Error {}

    // run once, checkpointed; if fn throws (a failure OR a gate pause) no marker
    // is written, so a re-run retries/resumes exactly there and skips the done.
    async function step(name, fn) {
      const marker = `${DIR}/state/${name}.done`;
      if (existsSync(marker)) return void console.log(`skip ${name}`);
      console.log(`run  ${name}`);
      await fn();
      await Bun.write(marker, "");
      console.log(`ok   ${name}`);
    }

    // run a pipeline through the runner. The run name rides in the INPUT and is
    // derived from scenario+step (`SLUG-step`): unique across branches/scenarios/
    // re-runs, stable per step so a resume reuses its own artifact dir. Input is
    // a real object, JSON.stringify'd — no shell quoting. `.nothrow()` lets us
    // read the exit code: a gate pause is its own signal, not a failure.
    async function pipe(id, step, args) {
      const input = JSON.stringify({ args, run: `${SLUG}-${step}` });
      const { exitCode } =
        await $`bun run pipeline run ${id} --input=${input}`.cwd(WS).nothrow();
      if (exitCode === GATE_PENDING) throw new GatePaused();  // runner printed how to resume
      if (exitCode !== 0) throw new Error(`pipeline "${id}" failed (exit ${exitCode})`);
    }

    try {
      // sequential: each depends on the previous
      await step("transcribe", () => pipe("<id>", "transcribe", { /* … */ }));
      await step("summarize",  () => pipe("<id>", "summarize",  { /* … */ }));

      // parallel: independent branches, each checkpointed, then a barrier
      await Promise.all([
        step("branch_a", () => pipe("<id>", "branch_a", { /* … */ })),
        step("branch_b", () => pipe("<id>", "branch_b", { /* … */ })),
      ]);

      console.log("scenario complete");
    } catch (err) {
      if (err instanceof GatePaused) {
        // A pipeline paused for a human decision (§5). Not a failure: the runner
        // already printed what it needs and the resume command. The paused step
        // left no checkpoint, so once the decision is supplied (as that
        // pipeline's input) re-running this script resumes right at the gate.
        console.log("scenario paused at a human-decision gate — resume after deciding");
        process.exit(GATE_PENDING);
      }
      throw err;
    }

Don't memorize pipeline ids, input field names, or input shapes here — take them
from the `explore`/`run` skills and each pipeline's own `--print-schema`. The
script is a generated artifact; the skill stays free of details that rot.

## 4. Run it, and resume on failure

Run `bun scenario.ts`. If a step fails, fix the cause (bad input, missing
credential, wrong order) and just **re-run the same script** — completed steps
are skipped and it picks up where it broke. Edit the script freely between runs;
that's expected.

## 5. Human-in-the-loop gates

Some pipelines have a **gate** that needs a human decision (approve a draft,
pick an option) before they finish. A scenario runs its pipelines **standalone**
— the live decision channel is only connected when something external drives the
run (the bot/API), not when `scenario.ts` calls the runner. The runner handles
this for you, and a gate behaves one of two ways standalone:

- **Auto-default gate** — proceeds with a built-in default (e.g. auto-approve
  your own generated draft) and the run completes. Nothing to do; just know it
  happened and note it in the report.
- **Decision-required gate** — the runner **pauses the run**: it writes a
  request file beside the gate step (what's being asked + the answer shape),
  prints the exact command to **resume from that step with the decision as
  input**, and exits with a **distinct "awaiting decision" status — not a
  failure**. Every parallel branch still finishes before it pauses, so nothing
  is left half-done.

What this means for your script:

- **Don't treat the pause as an error.** A failed step and a paused gate exit
  differently on purpose — the §3 skeleton's `pipe()` already tells them apart
  (the gate-pending exit code → a `GatePaused` signal) and **stops the scenario
  cleanly** without checkpointing or retrying it (a blind retry just pauses
  again). Keep that handling; the runner has already printed the request and the
  resume command for the user.
- **Resume with the answer.** Once the user decides, supply that decision as the
  gated pipeline's input and re-run — the runner picks up from the gate step
  (earlier work is read from disk), and the gate consumes the supplied answer
  instead of pausing again. The completed pre-gate steps stay checkpointed, so
  this slots into the same resume machinery from §3.
- **Pre-decide to stay unattended.** If you know the decision up front and want
  the scenario to run start-to-finish without stopping, pass that decision in the
  pipeline's input from the **first** call (the gate consumes it and never
  pauses). Use this only when answering unattended is genuinely fine.

Check for a decision-required gate while planning (§1), the same way you check
input shapes — a chain with one can't run fully unattended unless you pre-decide.
And **name the gate in the report** (§6 below): which gate, whether it
auto-resolved, paused, or was pre-decided, and what the decision was.

## 6. Leave a report — this is the point

When the scenario finishes (or once you've nursed it to working), write a report
beside the script — `report.md` in the same dir. Capture **what actually
happened**, not the tidy plan:

- the goal and the final step sequence (with the parallel/sequential shape);
- per step: which pipeline, the input used, the outcome, where output landed;
- **every difficulty, fix, and workaround** — the failures and what made them
  pass. This is the highest-value part.
- what to change next time, and whether this chain is run often enough to
  deserve graduating into a single purpose-built pipeline.

Scenarios start rough; the reports are how they harden into reliable runbooks.
Leave one even when the run only half-worked — a record of the dead end is still
worth more than nothing.

## 7. Promote a working flow to the catalog

When a scenario proves itself — or you had to fight it into working — capture it
as a reference in the **scenario catalog** (`.hq/scenarios/`) so the next run
starts from a recipe, not from scratch. The catalog entry is the durable
counterpart to the per-run report: it **must** carry a mermaid flow diagram
(parallel vs. sequential), plus whatever notes save the next person time. Update
an existing entry rather than duplicating one; follow that directory's README
for the convention. (A flow you reach for constantly is a further candidate to
collapse into a single purpose-built pipeline.)
