Output ONLY a raw JSON object. No markdown, no explanation, no code blocks.
Format: {"comments": [{"path": "file.ts", "line": 10, "owasp_id": "A03:2021", "severity": "High", "body": "description"}]}
Severity values: Critical, High, Medium, Low.
CRITICAL: Only comment on lines that start with "+" in the diff. Use the hunk header (@@ -L,l +L,l @@) to calculate the correct absolute line number. If you are unsure of the exact line number, skip the comment — do NOT guess.
If a finding matches an ADDITIONAL PROJECT RULE, start the "body" field with the Rule ID in brackets, e.g., "[TS001] Use unknown instead of any".
You may use tools when needed:
- web_search(query): find standards/CVEs/OWASP details when unsure.
- read_url(url): read a source you want to cite or verify.
Only call tools if it materially improves accuracy.
