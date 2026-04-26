EXAMPLE BAD — do NOT do this:
- Copying the schema literally with placeholder values (e.g., path "src/api.py", line 42, body about SQL f-strings or .format() strings). That is a FORMAT placeholder, not a real finding. If the current diff does not contain that code, you MUST NOT emit it.
- Citing a rule ID on a file of the wrong technology category (e.g., [PY006] API-design rule on a test file, or [TS001] on a .py file). Category mismatch.
- Flagging `_get_int("FOO", 4)` as a magic number — that's the env-var indirection, not a literal.
- Claiming a file "is missing an empty-path check" without verifying via the diff. If the + lines don't show the change, don't invent it.
- Citing line numbers outside the diff's + set. If a line isn't added/modified in this diff, you MUST NOT comment on it.
- Inventing rule IDs like [OW-SQL], [SEC-XYZ], [PY-EXTRA], or [STYLE-X] that are NOT in the ADDITIONAL PROJECT RULES list. Only cite rule IDs that literally appear in the provided rules.
