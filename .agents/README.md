# Agent skills

Workspace-level skills an agent uses when working here. Tool-agnostic under
`.agents/skills/`; `../.claude/skills` symlinks to it so Claude Code discovers
them.

## Keep skills anti-fragile

These skills must survive API changes, renames, and refactors **without
edits**. When adding or changing one, follow the same rules the existing
skills do:

- **Route through stable indirection, not paths.** Invoke the workspace
  `package.json` scripts (e.g. `bun run pipeline`) and discover components by
  finding their marker files (e.g. Glob for `rigid.config.yaml`) — never
  hard-code a directory like `.hq/pipelines`.
- **Don't restate the API.** Point to the tool's own always-current
  self-description (`--help`, print-schema, `rigid help llms`,
  `rigid graph schema`). No memorized flags, subcommand names, or node kinds.
- **Don't enumerate what rots.** No file lists, example names, category
  paths, or gotcha lists. Defer to the component's living guide
  (`CLAUDE.md`) and have the agent read it.
- **Describe intent and the discovery path**, not literal commands, wherever a
  literal would pin a detail that can change.

A skill that needs editing when the layout or API shifts is a bug.

## Component-owned skills

A component under `.hq/` can ship its own skills at
`.hq/<component>/.agents/skills/<skill>/SKILL.md`. Claude Code only discovers
skills one level deep, so a grouping subdirectory (`.claude/skills/<group>/…`)
is invisible — `make sync-skills` (also run by `make install`) bridges this by
creating a flat, prefixed symlink `.agents/skills/<component>-<skill>` for each
one. Hand-authored skills here (real directories) are left untouched; the
knowledge base is excluded (its skills run from inside the KB and are surfaced
via the `rigid-*` router skills).

Components own their skills; the workspace only links them. Re-run
`make sync-skills` after a component adds or removes a skill.
