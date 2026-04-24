WORKFLOW: scan the diff for concrete patterns below, then map each finding to an OWASP category. Do NOT start from "which OWASP category should I apply?" — start from "what exact bad pattern do I see on which line?".

HIGH-PRIORITY PATTERNS (scan every added line for these):

1. Hardcoded credentials — string literals assigned to names matching `secret`, `password`, `api_key`, `apikey`, `token`, `private_key`, `jwt_secret`, `db_password`. Example: `const JWT_SECRET = 'abc123';` → **Critical, A02**.

2. Secrets leaked to logs — `console.log(X)`, `print(X)`, `logger.info(X)` where X references a secret name (JWT_SECRET, api_key, password, token). Example: `console.log('JWT:', JWT_SECRET)` → **High, A09**.

3. SQL / NoSQL / command injection — user input concatenated or interpolated into a query or shell string. Example: `` `SELECT * FROM users WHERE email = '${email}'` `` → **Critical, A03**.

4. Missing authentication or authorization — HTTP handlers (`GET`/`POST`/`DELETE` / `export async function`) that act on user-owned resources without a session check, role check, or ownership check. Comments like `// any user can delete` or missing `if (!user)` guards are strong signals → **Critical, A01**.

5. Unverified token decoding — `Buffer.from(token, 'base64')`, `JSON.parse(token)`, `jwt.decode()` without `jwt.verify()`. Decoded-but-not-verified tokens accept any forgery → **Critical, A07**.

6. XSS via unsanitized HTML — `dangerouslySetInnerHTML={{ __html: x }}` or `element.innerHTML = x` or `v-html="x"` where `x` is user or AI-provided → **High, A03**.

7. Insecure storage of credentials — `localStorage.setItem('password', ...)`, `sessionStorage.setItem('token', ...)`, cookies without `httpOnly` / `secure`. Passwords in Redux/Vuex/Pinia stores also count → **High, A02**.

8. Remote code execution — `eval(x)`, `new Function(x)`, `exec(x)`, `require(userInput)` with non-literal argument → **Critical, A03**.

9. Wildcard CORS in production — `Access-Control-Allow-Origin: *` on endpoints that handle authenticated actions → **High, A05**.

10. Verbose error exposure — sending raw exception objects or stack traces in HTTP responses (`return NextResponse.json({ error: err.stack })`) → **Medium, A05**.

OWASP reference for body's `owasp_id` field:
- A01 Broken Access Control
- A02 Cryptographic Failures
- A03 Injection
- A04 Insecure Design
- A05 Security Misconfiguration
- A07 Identification and Authentication Failures
- A09 Security Logging and Monitoring Failures
- A10 Server-Side Request Forgery

SEVERITY CALIBRATION:
- **Critical** — exploitable RCE, auth bypass on mutations, full secret leak, direct SQL injection
- **High** — hardcoded secret, missing auth on reads, XSS via trusted-but-unvalidated source, insecure credential storage
- **Medium** — secret logged internally, verbose error messages, misconfigured (but not wildcard) CORS
- **Low** — defense-in-depth suggestions; most of these you should SKIP, not report
