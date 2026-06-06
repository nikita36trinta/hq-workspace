---
name: query
description: Use when the user wants to query, inspect, or explore this knowledge base — find entities, traverse relationships, or see its schema. Triggers on "query the knowledge base", "what does the KB know about …", "show the rigid schema", "find all <Kind>". (Surfaced as `rigid-query` from a composed workspace.)
allowed-tools: Read, Glob, Bash(rigid *)
---

# Query this knowledge base

This is a **rigid** project. `rigid` is self-describing, so this skill leans on
its own help and schema output instead of hard-coded commands, node kinds, or
paths — that is what keeps it from going stale.

## Orient

`rigid` commands run from the project root — the directory holding
`rigid.config.yaml`. If your working directory isn't already there, find it
(Glob for `**/rigid.config.yaml`, ignoring `node_modules`) and `cd` in.

    rigid help llms     # always-current API: documents, queries, commands

## Compile, then query — using whatever the help documents

`rigid help llms` (and `rigid help <command>`) define the exact subcommands;
follow them. The durable workflow:

1. Validate / compile the graph (rigid skips work when nothing changed).
2. Print the schema to see what is queryable — types, fields, edges:
   `rigid graph schema -o graphql`. Read this **every time**: the schema is
   defined by this KB's plugins and changes as they do.
3. Query against that schema with `rigid graph query '<graphql>'`.

Never assume node kinds, field names, or edge names — take them from the schema
output, not from memory.

## Sensitive data

Some fields are masked by default. Reveal real values only when the user
explicitly asks; `rigid help graph` shows the current flag.

## Report back

Answer from the results, and show the query you ran so the user can refine it.
