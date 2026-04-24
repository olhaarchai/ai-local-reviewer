Output ONLY a raw JSON object. No markdown, no explanation, no code blocks.
Format: {"comments": [{"path": "file.ts", "line": 10, "body": "description"}]}
If a finding matches an ADDITIONAL PROJECT RULE, start the "body" field with the Rule ID in brackets, e.g., "[TS001] Use unknown instead of any".

SCHEMA (replace every <placeholder> with values derived from THIS diff — do NOT copy placeholders or any example verbatim):
{"comments": [{"path": "<path-present-in-this-diff>", "line": <line-from-this-diff's-+-set>, "body": "<finding specific to that exact line>"}]}
