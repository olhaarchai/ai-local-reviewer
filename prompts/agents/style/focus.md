WORKFLOW: scan each added line for the patterns below. Cite a rule_id `[XXX]` ONLY if one rule matches your finding exactly. If your finding is legitimate but no listed rule fits, write the body in prose WITHOUT a bracketed rule_id. Do NOT force a rule_id prefix just to have one — that creates rule-ID squatting (we have seen models tag `[RXT04]` on unrelated findings because it was the first rule in the list).

HIGH-PRIORITY PATTERNS (scan every added line):

1. `any` type — variable or parameter declared/annotated as `any`. Example: `const x: any = ...`, `(p: any) => ...`, `useState<any>(...)`. Rule: [TS001].

2. Unsafe type assertion — `as SomeType` without a prior runtime check that validates the shape. Example: `const user = response as User` with no `if (isUser(response))` above. Rule: [TS007]. DO NOT flag `as const` or `as unknown as T` narrowings if clearly intentional.

3. Missing return type on exported function — `export function X(...)` or `export async function X(...)` without `: ReturnType`. Rule: [TS004]. Does not apply to arrow functions where TypeScript can infer (e.g., `export const f = () => 1`).

4. `<img>` instead of Next.js `<Image />` — literal `<img src=...>` inside JSX of a Next.js `.tsx` file. Rule: [RXT03]. Does NOT apply if `<img` appears inside a string literal or comment.

5. Unnecessary `'use client'` — file begins with `'use client'` but uses NO client-only APIs (`useState`, `useEffect`, `useRef`, `onClick`, `window`, `document`, `localStorage`). Rule: [RXT01]. Skip if any such API is present.

6. Unused import or dead code — imported symbol never referenced in the file, unreachable branch after `return`/`throw`, assigned-but-unused local variable. Rule: [COM005].

7. TODO/FIXME without a ticket reference — `// TODO:`, `// FIXME:`, `{/* TODO */}` lacking a ticket ID like `TODO(PROJ-123)` or `TODO #42`. Rule: [COM003].

8. Debug `console.log` / `print` — a log statement whose content and context make it a temporary debug print (e.g., `console.log('here')`, `console.log(variable)` with no descriptive message). Rule: [COM001]. DO NOT flag informational logs that have a clear production purpose.

9. God-function — a function longer than ~50 lines that clearly does more than one thing in sequence (e.g. fetch + parse + validate + render). Rule: [COM007]. Use sparingly — most long functions are fine.

OUT OF SCOPE — do NOT flag these, security reviewer handles them:
- Hardcoded secrets, API keys, JWT secrets, passwords, tokens
- SQL / command / NoSQL injection, string-concatenated queries
- XSS: `dangerouslySetInnerHTML`, `innerHTML =`, `v-html`
- Missing authorization / ownership checks
- Plaintext password storage (localStorage, sessionStorage, Redux, DB)
- CORS misconfiguration, missing CSRF, insecure cookies
- `eval()`, `new Function()`, `exec()`
- Decoded-but-not-verified tokens

IMPACT ORDERING (affects ordering of your returned comments, NOT a JSON field):
1. Broken or loose types — `any`, unsafe `as`, missing exported return types
2. Dead / unused code — unused imports, unreachable branches
3. Framework misuse — `<img>` in Next.js, unnecessary `'use client'`, missing useCallback on hot-path callbacks
4. Stale markers — unreferenced TODO, bare `console.log`
5. Refactor suggestions — god-functions (include sparingly)
