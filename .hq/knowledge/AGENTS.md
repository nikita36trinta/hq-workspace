# rigid knowledge base

This project is a knowledge base managed by **rigid** — a knowledge compiler.

**IMPORTANT: Before doing anything else, run `rigid help llms` to learn the full API — document format, query patterns, plugin authoring, and all CLI commands.**

## Quick reference

- `rigid graph schema -o graphql` — see available types, fields, edges (GraphQL SDL)
- `rigid graph query '<graphql>'` — query the knowledge graph
- `rigid check` — validate all documents (run before committing)
- `rigid compile` — compile documents → .rigid/graph.db
- `rigid doc add <Kind> <name>` — scaffold a new document
- `rigid migrate status` — check migration state
