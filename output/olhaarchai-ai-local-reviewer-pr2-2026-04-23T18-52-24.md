# Review — olhaarchai/ai-local-reviewer PR #2

Generated: `2026-04-23T18-52-24`

## HITL

- **Action:** `approve`
- **Critic iterations:** 1

## Models

- security: `qwen2.5:7b`
- style: `qwen2.5:3b`
- fast: `llama3.2:1b`

## Stack / context

```
PR files (14 total):
  - .env_example
  - prompts/security-format.md
  - prompts/style-format.md
  - requirements.txt
  - rules/common.md
  - src/core/config.py
  - src/integrations/retriever.py
  - src/integrations/sparse_index.py
  - src/review/graph.py
  - src/review/nodes.py
  - src/review/state.py
  - src/tools/static_analysis.py
  - tests/__init__.py
  - tests/test_retriever.py

Detected tech: python-general
```

## Retrieved guidelines (16)

**api-design** (4):
- [API02] Always version APIs via URL prefix (`/v1/...`) or `Accept` header version; never make breaking changes to an existing version
- [API06] Document all endpoints with OpenAPI/Swagger; every request body and response schema must be defined
- [API05] Return appropriate HTTP status codes: 201 for creation, 204 for successful deletion, 422 for validation errors
- [API04] Use a consistent JSON error structure: `{ "error": { "code": 400, "message": "Human-readable description" } }`

**common** (4):
- [COM005] Do not leave dead code (unreachable branches, unused imports, unused variables) in the diff
- [COM001] Remove all debug print statements and temporary logging before merging
- [COM007] Keep functions focused on a single responsibility; if a function does more than one thing, split it
- [COM003] Every TODO comment must reference a ticket or issue number, e.g. # TODO(PROJ-123)

**python-general** (4):
- [PY006] Use dataclasses or Pydantic models instead of plain dicts for structured data
- [PY005] Prefer f-strings over `.format()` or `%` formatting
- [PY007] Always close file handles with a `with` statement; never rely on garbage collection
- [PY002] Follow PEP 8: 4-space indentation, max 88 chars per line (Black default), snake_case for functions and variables

**security-owasp** (4):
- [SEC02] Remove all `console.log`, `print`, and debug statements from production code; use a proper logging framework with log levels
- [SEC06] Use Content-Security-Policy headers to mitigate XSS; disallow `unsafe-inline` scripts
- [SEC05] Validate and sanitize all user input at the boundary of the application; never trust client-supplied data
- [SEC04] Never store passwords in plaintext; always hash with bcrypt, argon2, or scrypt with a per-user salt

## Lint findings (10)

- src/core/config.py:2 - [ruff:invalid-syntax] Unexpected indentation
- src/core/config.py:15 - [ruff:W292] No newline at end of file
- src/integrations/retriever.py:60 - [ruff:E501] Line too long (90 > 88)
- src/integrations/retriever.py:98 - [ruff:invalid-syntax] Unexpected indentation
- src/integrations/retriever.py:187 - [ruff:W292] No newline at end of file
- src/integrations/sparse_index.py:122 - [ruff:W292] No newline at end of file
- src/review/graph.py:4 - [ruff:invalid-syntax] Unexpected indentation
- src/review/graph.py:7 - [ruff:W292] No newline at end of file
- src/review/nodes.py:54 - [ruff:invalid-syntax] Unexpected indentation
- src/review/nodes.py:65 - [ruff:E501] Line too long (100 > 88)

## Analyst raw responses (2)

#### Response 1

```json
{"comments": [{"path": "src/core/config.py", "line": 74, "owasp_id": "A02:2021", "severity": "High", "body": "[SEC04] Never store passwords in plaintext; always hash with bcrypt, argon2, or scrypt with a per-user salt."}, {"path": "src/core/config.py", "line": 78, "owasp_id": "A02:2021", "severity": "High", "body": "[SEC04] Never store passwords in plaintext; always hash with bcrypt, argon2, or scrypt with a per-user salt."}]}
```

#### Response 2

```json
{"comments": [{"path": "src/core/config.py", "line": 62, "body": "[COM005] Do not leave dead code (unreachable branches, unused imports, unused variables) in the diff"}, {"path": "src/integrations/sparse_index.py", "line": 13, "body": "[PY006] Use dataclasses or Pydantic models instead of plain dicts for structured data"}, {"path": "tests/test_retriever.py", "line": 245, "body": "[COM005] Do not leave dead code (unreachable branches, unused imports, unused variables) in the diff"}]}
```

## Critic rejections (3)

- **G1: line 74 not in '+'-set for 'src/core/config.py'** — src/core/config.py:74 — [SEC04] Never store passwords in plaintext; always hash with bcrypt, argon2, or scrypt with a per-user salt.
- **G1: line 62 not in '+'-set for 'src/core/config.py'** — src/core/config.py:62 — [COM005] Do not leave dead code (unreachable branches, unused imports, unused variables) in the diff
- **G1: line 245 not in '+'-set for 'tests/test_retriever.py'** — tests/test_retriever.py:245 — [COM005] Do not leave dead code (unreachable branches, unused imports, unused variables) in the diff

## Surviving comments (2)

- **src/core/config.py:78** (HIGH) [A02:2021] — [SEC04] Never store passwords in plaintext; always hash with bcrypt, argon2, or scrypt with a per-user salt.
- **src/integrations/sparse_index.py:13** — [PY006] Use dataclasses or Pydantic models instead of plain dicts for structured data

## Summary

Executive Summary

Found 2 issue(s) across 2 file(s).

Key findings:
- [HIGH] [A02:2021] src/core/config.py:78 — [SEC04] Never store passwords in plaintext; always hash with bcrypt, argon2, or scrypt with a per-user salt.
- src/integrations/sparse_index.py:13 — [PY006] Use dataclasses or Pydantic models instead of plain dicts for structured data

Recommendations:
- Address the findings above and re-run the review.

## Timings

| node | seconds |
|---|---|
| security_analyst | 141.54 |
| style_analyst | 134.06 |
