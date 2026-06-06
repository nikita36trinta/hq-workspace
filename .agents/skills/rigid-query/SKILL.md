---
name: rigid-query
description: Use when the user wants to query, inspect, or explore the workspace knowledge base — find entities, traverse relationships, or see its schema. Triggers on "query the knowledge base", "what does the KB know about …", "show the rigid schema", "find all <Kind>". Routes to the query skill owned by the knowledge base.
allowed-tools: Read, Glob, Bash(rigid *)
---

# Query the knowledge base

The query logic is owned by the knowledge base itself, kept next to its schema
and documents so it stays aligned with them. Route to it rather than
duplicating here.

## Route to it

1. Find the knowledge base: Glob for `**/rigid.config.yaml` (ignore
   `node_modules`); its directory is the rigid project root.
2. `cd` there and use that project's own `query` skill (under its
   `.agents/skills/`).
3. If no `query` skill is present there, fall back to `rigid help llms`, then
   `rigid graph schema -o graphql` to see what's queryable, then
   `rigid graph query '<graphql>'`.

Keeping query in the KB means it tracks the live schema and is available when
the knowledge base is used on its own.
