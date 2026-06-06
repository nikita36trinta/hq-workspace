---
name: ingest
description: Use when the user wants to import, ingest, or convert unstructured files or directories into rigid documents. Triggers on requests like "ingest this data", "import these files into the knowledge base", "convert this to rigid format", or when pointing at external data that should become rigid documents.
allowed-tools: Read, Grep, Glob, Bash(rigid *), Edit(documents/**), Write(documents/**), Edit(plugins/**), Write(plugins/**)
---

# Ingest — convert unstructured data into rigid documents

Analyze input data, map it to existing schemas (or suggest new ones),
and generate rigid documents.

## Input

$ARGUMENTS — path to a file or directory to ingest. If empty, ask the user.

## Step 0: Learn rigid

Run `rigid help llms` and read the output — it contains the schema format,
document structure, CLI commands, and plugin authoring guide.

## Step 1: Understand the project

- Read `rigid.config.yaml` to find plugin paths and document directories
- Read each plugin's `plugin.ts` and all node/edge definition files
- Scan existing documents to understand what's already in the knowledge base

## Step 2: Analyze the input

- Read files at the provided path (recurse into directories)
- Identify entities, relationships, and structured data
- Note patterns: tables, lists, key-value pairs, repeated structures, prose

## Step 3: Match against existing schemas

For each identified entity:
- Can an existing node type represent it? Which fields map?
- Do existing edge types capture the relationships?
- What data is left unmapped?

## Step 4: Present findings

Use this format:

```
── Mapping ──────────────────────────────────

✓ <entity> × <count>  →  <NodeType> (full match)
~ <entity> × <count>  →  <NodeType> (needs extension)
✗ <entity> × <count>  →  no matching schema

── Extend existing schemas ──────────────────

<NodeType>:
  + <fieldName>: <type>                    ← <why>
  + <fieldName>: <type> (optional)         ← <why>

── New schemas needed ───────────────────────

<NewNodeType>: "<description>"
  <fieldName>: <type>
  <fieldName>: <type> (optional)
  <fieldName>: sensitive(<type>)           ← <why sensitive>
  edges:
    <edgeName> → <TargetType>              ← "<description>"

── Documents to generate (<N> total) ────────

documents/<kind-plural>/<name>.rigid.yaml   × <count per kind>
```

## Step 5: Get confirmation

Ask the user what to proceed with:
1. Which schema changes to apply (all, some, none)
2. Whether to generate documents
3. Any adjustments to the mapping

## Step 6: Implement

On confirmation:
- Extend existing node/edge definitions in plugin files
- Create new node/edge definitions and register them in `plugin.ts`
- Generate `.rigid.yaml` (or `.rigid.md` if prose body is present) for each entity
- When generating many documents, spin up parallel agents — one per document group (by kind).
  The `allowed-tools` frontmatter above grants Write/Edit access to `documents/**` and `plugins/**`,
  so agents can write without prompting the user for each file.
- Run `rigid check` to validate everything compiles

## Rules

- Prefer extending existing schemas over creating new node types
- Use `sensitive()` for personal data, financial info, credentials
- Add `.describe()` only on non-obvious fields
- Use kebab-case for document names (`metadata.name`)
- Use camelCase for schema fields
- Import `definePlugin` and `ContextOf` from `@rigid/plugin`, `schema` extension and `z`/`sensitive` from `@rigid/schema`
- Never import `zod` directly — always use `z` from `@rigid/schema`
- Node/edge/rule registration uses `ctx.addNode()`, `ctx.addEdge()`, `ctx.addRule()` (provided by the `schema` extension)
- Edge `kind` option: `"hierarchy"` (containment, single parent), `"dependency"` (ordering, drives DAG), `"association"` (default, informational)
- Only include fields that have actual data in generated documents
- For ambiguous mappings, present options and ask
- Keep edge metadata minimal — only when relationship carries data beyond the link itself
- Enum values must be valid GraphQL identifiers: `[_a-zA-Z][_a-zA-Z0-9]*` — no hyphens, dots, or spaces. Use UPPER_SNAKE_CASE (e.g. `z.enum(["ACTIVE", "IN_PROGRESS"])`)
- When generating `.rigid.md` documents: data that is captured in frontmatter (spec fields, edges) must NOT be repeated in the markdown body. The body is for prose context that doesn't fit into structured fields — background, rationale, free-form notes. If all data is structured, use `.rigid.yaml` instead.
- If no suitable plugin exists, suggest creating a new one with the needed schemas
