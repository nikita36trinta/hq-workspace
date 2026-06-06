---
name: rigid-ingest
description: Use when the user wants to import, ingest, or convert unstructured files or data into the workspace knowledge base. Triggers on "ingest this", "import into the knowledge base", "convert this to rigid". Routes to the ingest skill that lives inside the knowledge base.
allowed-tools: Read, Glob, Bash(rigid *)
---

# Ingest into the knowledge base

Do **not** carry ingest logic here. The knowledge base ships its own ingest
skill, kept in sync with its schema (and refreshed from its plugins by
rigid's own skill-sync). That skill — not this one — is the source of truth,
and routing to it is what keeps this from rotting.

## Route to it

1. Find the knowledge base: Glob for `**/rigid.config.yaml` (ignore
   `node_modules`); its directory is the rigid project root.
2. `cd` there and use that project's own `ingest` skill (under its
   `.agents/skills/`), passing the path to the data to import.
3. If no ingest skill is present there, fall back to `rigid help llms` and
   follow its ingest / document-authoring guidance.

Ingest stays inside the KB on purpose: it needs the live schema and existing
documents in context to map data onto the right types, and it updates
automatically as the schema evolves. A copy here would drift out of sync.
