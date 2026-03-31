# Security Policy Matrix

This matrix defines minimum security policy controls for MeTTaClaw capabilities.

## Global policy baseline

- Default deny for all side-effecting capabilities.
- Allow by explicit policy only (per channel/user/role).
- Log every allow/deny decision with timestamp, actor, channel, capability, and target.
- Never log raw secrets.

## Capability policy matrix

| Capability | Required policy | Enforcement controls | Audit requirements |
|---|---|---|---|
| `shell` command execution | Restricted to trusted roles only | Command allowlist, arg validation, timeout, resource limits, block shell metachar expansion when possible | Command, actor, channel, exit status, duration |
| `read-file` | Read scope control | Path allowlist, deny secret paths, deny traversal (`..`), max file size | Path, actor, decision |
| `write-file` / `append-file` | Write scope control | Path allowlist, deny system dirs, content size limits, optional content scanning | Path, actor, bytes written |
| `metta` code execution | High-risk execution control | Trusted-role only, expression validation, sandboxed runtime, execution timeout | Expression hash, actor, result/exception |
| `search` / web egress | External access control | Domain allowlist, request timeout/retries, SSRF protections, rate limiting | Query, destination domain, latency |
| LLM requests (Ollama/OpenAI) | Model and data governance | Model allowlist, max tokens/context limits, prompt/output filtering, no secret injection | Model used, token/latency metrics, request class |
| Memory write (`remember`) | Data governance + privacy | PII policy, sensitive-content denylist, retention TTL, deletion pathway | Memory key/id, actor, retention class |
| Memory query (`query`) | Data access control | Per-user/role scope, relevance threshold, redact sensitive fields | Query text hash, returned item count |
| Outbound `send` to channels | Exfiltration control | Content length limits, secret redaction, destination policy checks | Destination channel, message hash/length |
| Inbound channel message processing | Identity and trust policy | Per-channel trust tier, anti-spam/rate limits, mention/command gating for untrusted channels | Sender identity, source channel, accepted/rejected |
| `join-channel` / `leave-channel` | Communication surface control | Channel allowlist, trusted-role only, max joined channels | Requested channel, action decision |
| Channel switching (`use telegram channel`, etc.) | Routing control | Allowed channel set by role/channel, fallback behavior defined, fail closed | Old->new channel switch, actor |
| Config reveal (`show ... channel config`) | Information disclosure control | Mask tokens/secrets, restrict who can call, minimize metadata shown | Capability use, fields returned |
| Secrets (`*_TOKEN`, API keys) | Secret management policy | Env/secret store only, rotation, no plaintext in logs/history, startup validation | Secret source/age, rotation events |
| Container runtime/network | Host hardening policy | Non-root runtime, read-only FS, minimal writable mounts, no privilege escalation, egress policy | Image/version, runtime security flags |

## Policy tiers by channel trust

- `trusted-admin`: full operational controls (excluding raw secret disclosure).
- `trusted-user`: safe read/query and normal chat operations.
- `untrusted/public`: no code execution, no shell, no write operations, no channel reconfiguration.

## Minimum denylist (always blocked)

- Commands that expose environment secrets or token files.
- Writes outside approved workspace paths.
- Arbitrary network pivoting/scanning behavior.
- Self-modification of security policy files unless approved by trusted-admin.

## Incident response hooks

- Trigger alert on repeated denied high-risk actions.
- Trigger alert on suspected secret leak patterns in outbound messages.
- Maintain tamper-evident audit log retention window (recommended: 30-90 days).

