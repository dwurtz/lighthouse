# Deja support runbook

Short operator-facing guide for triaging Deja bug reports. Everything
below runs locally against files under `~/.deja/` — nothing talks to a
server.

## A user reports a bug — what do I do?

Ask for one of the following, in order of preference:

1. **The request id** from the red toast in the menubar panel. It
   looks like `req_abc123def456` and shows up on every typed error.
   This is the fastest path — one id tells you everything.
2. **A support bundle.** Have them run:
   ```
   ./venv/bin/python tools/deja_support_bundle.py
   ```
   then attach the `~/Downloads/deja-support-<timestamp>.zip` to the
   ticket. If the bug report mentions personal contacts or email
   subjects they're worried about sharing, ask them to pass
   `--redact-emails`.

## I have a request id — now what?

```
./venv/bin/python tools/deja_support_lookup.py req_abc123def456
```

Prints the full timeline correlated across `deja.log`, `audit.jsonl`,
`errors.jsonl`, and any matching `integrate_shadow/*.json` cycle
record. Useful flags:

- `--json` — copy-paste friendly, good for ticket attachments.
- `--since 2026-04-10T12:00` — narrow a noisy window.
- `--limit 100` — cap the output.
- `--path /tmp/deja-support-xxx` — point at an unpacked support zip
  instead of your own `~/.deja` (use this when triaging a bundle the
  user uploaded).

Exit code: `0` if the id left a trace, `1` if there's no record of it
anywhere.

## Common error codes

| Code                  | Meaning                                        | First move                                                    |
| --------------------- | ---------------------------------------------- | ------------------------------------------------------------- |
| `proxy_unavailable`   | Deja's Render proxy is restarting or down.     | Wait ~1 minute, retry. Check Render dashboard if it persists. |
| `auth_failed`         | User's Google OAuth token expired or revoked.  | Have them open the Deja setup panel and reconnect.            |
| `rate_limited`        | Gemini throttling a burst of calls.            | Wait 1–2 minutes. Frequent? File an issue — we may need backoff tuning. |
| `llm_error`           | Gemini returned a 5xx or bad payload.          | Run `deja_support_lookup` on the id; include `details` field in the issue. |
| `config_error`        | Wiki directory missing, unreadable, or stale.  | Check the setup panel; a re-run of onboarding usually fixes it. |
| `tool_error`          | A subprocess (`gws`, `qmd`, `ffmpeg`) failed.  | Verify `gws` CLI is authed (`gws gmail +triage`); check PATH. |

## Privacy guarantees about a support bundle

Support bundles produced by `tools/deja_support_bundle.py` contain:

- `deja.log` — last 1000 lines of the runtime log.
- `errors.jsonl` — last 500 typed-error records.
- `audit.jsonl` — last 500 agent-action audit records.
- `feature_flags.json` — the user's active feature flags.
- `machine_info.txt` — OS version, hardware model, Python + Deja versions.
- `README.txt` — describing the above.

They explicitly **do not** contain:

- `observations.jsonl` — raw OCR, iMessage bodies, email bodies,
  clipboard contents. Excluded by design; the bundle tool has a
  hard-coded block to prevent it sneaking in.
- Wiki pages under `~/Deja/` (personal notes about people, projects).
- Screenshots, audio recordings, OAuth tokens.

With `--redact-emails`, email-shaped tokens in every included file get
replaced with `<email>` before the zip is sealed.

## Escalating to engineering

1. Run `deja_support_lookup.py req_xxx --json` and paste the output
   into a new GitHub issue (or attach it if it's long).
2. Include the user's Deja version (in `machine_info.txt` if you have
   a bundle; in `errors.jsonl` details in many cases).
3. Tag with the error code (`proxy_unavailable`, `tool_error`, ...).
4. If the bug involves a specific cycle, pass the cycle id you see in
   the audit rows to narrow down further with
   `jq 'select(.cycle == "c_abc123")' ~/.deja/audit.jsonl`.
