# Installing Deja on a corporate-managed Mac

Self-contained walkthrough for installing Deja on a work laptop where you don't own the MDM profile. Hand this file to Claude Code running on the target machine — it has enough detail to execute every step and diagnose common failures.

## TL;DR

1. Get `Deja.dmg` onto the target Mac (AirDrop from your personal Mac is easiest).
2. Install, right-click → Open to bypass Gatekeeper.
3. Run through the setup panel: OAuth with your work Google account, grant TCC, disable personal-only sources (iMessage, WhatsApp).
4. If any step is blocked by policy, see the **Failure modes** section at the bottom.

---

## Before you start

The user will have told you:
- Which Google account to sign in with (`you@your-company.com` or personal fallback)
- Which observation sources to enable (usually: calendar, email, screenshots, browser, meetings — NOT iMessage or WhatsApp on a work Mac)
- Whether they have Claude Code + an active Pro/Max subscription on this machine (required for cos and `browser_ask`)

## Step 1 — prereqs check

Run these and report what's present:

```bash
sw_vers                       # macOS version (14+ recommended)
claude --version              # Claude Code CLI — needed for cos
python3 --version             # informational; DMG ships its own Python env
```

Deja ships as a standalone app with its own bundled Python. You do NOT need Python, Homebrew, Xcode, or command line tools installed on the target machine to run from the DMG.

If `claude` is missing: install Claude Code separately via `https://claude.com/claude-code` before proceeding — cos and the `browser_ask` MCP tool both shell out to it.

## Step 2 — get the DMG onto the machine

Options, in order of preference:

1. **AirDrop** from a personal Mac that has `Deja.dmg` (fastest, no network involved).
2. **Download URL** — if the user has a release URL (typically `https://trydeja.com/download` or a GitHub releases link), `curl -L -o ~/Downloads/Deja.dmg <URL>`.
3. **USB or shared drive** — fall back if AirDrop is blocked by MDM.

The DMG lives at `site/Deja.dmg` in the source repo. If you have repo access on the work machine (rare on a corp Mac), you can also clone and grab it from there — but do NOT try to `make dev` build; that needs full Xcode.

## Step 3 — install

```bash
# Mount the DMG
open ~/Downloads/Deja.dmg

# A Finder window opens. Drag Deja.app into /Applications.
# Then eject the mounted image.
hdiutil detach /Volumes/Deja 2>/dev/null || true
```

## Step 4 — first launch (Gatekeeper)

```bash
open /Applications/Deja.app
```

**Expected behaviors:**

- **"Deja.app is from an identified developer but has not been notarized. Open anyway?"** → this is fine. Click Open. (Alternatively: right-click Deja.app in Finder → Open → Open in the dialog.)
- **"Deja can't be opened because Apple cannot check it for malicious software"** → same fix as above: right-click → Open, confirm.
- **"Deja.app can't be opened. Contact your system administrator"** → **MDM is blocking by app allowlist.** See Failure modes. Stop here.
- **No dialog, app silently fails to launch** → check `Console.app` for messages about Deja; may be a quarantine issue. Try `xattr -cr /Applications/Deja.app` and relaunch.

If the app launches, the setup panel appears.

## Step 5 — Google OAuth

The setup panel pops a browser tab for Google consent. The OAuth client requests Gmail + Calendar + Drive + Tasks read scopes plus a couple of write scopes.

**Sign in with the user-specified account** (typically their work email).

Watch for one of:

- **Normal consent screen** listing the scopes → approve, token lands in Keychain. ✓
- **"This app is blocked by your organization"** or **"This app isn't verified / wasn't reviewed by your admin"** → Workspace admin policy is blocking third-party OAuth. See Failure modes.
- **Consent succeeds but with fewer scopes than requested** → Workspace may be restricting scopes. Note which ones came through; email/calendar alone still gets Deja 80% of the way.

## Step 6 — TCC permissions

The setup panel prompts for four macOS permissions in sequence:

| Prompt | What to do |
|---|---|
| Full Disk Access | **Skip on a work Mac** if iMessage/WhatsApp will be disabled (it's only needed to read their SQLite DBs). If the toggle is grayed out in System Settings, MDM has locked it — skipping is fine here. |
| Screen Recording | Required for the vision pipeline. If grayed out, stop and escalate to IT (needed for Deja's core feature). |
| Accessibility | Required for window-title context. If grayed out, Deja still works but with reduced grounding. |
| Microphone | Required for push-to-talk voice. If grayed out, voice is disabled; chat input still works. |

Each prompt opens System Settings → Privacy & Security. If any toggle is grayed out, that's MDM restriction — note which ones, continue past them.

## Step 7 — scope decisions

Before completing setup, edit `~/.deja/config.yaml` (the setup panel may create it already; if not, it'll be created on first launch):

```yaml
# Recommended work-Mac defaults
screenshot_enabled: true
email_enabled: true
calendar_enabled: true
browser_enabled: true
drive_enabled: true
tasks_enabled: true

# Usually off on work machines:
imessage_enabled: false
whatsapp_enabled: false
clipboard_enabled: false   # contains sensitive paste content
```

## Step 8 — 30-day backfill

The setup panel runs `deja onboard --days 30` at the end. This ingests 30 days of sent email, calendar, Drive activity, and Meet transcripts from the user's work Google account to bootstrap the wiki.

- Takes 3-10 minutes depending on volume.
- Runs in a subprocess; the setup panel can be closed.
- Monitor starts the 3-second observe loop immediately after backfill finishes.

## Step 9 — sanity checks

```bash
deja status                 # should show all green: monitor + web alive
deja trail | head -20       # first integrate cycle entries
ls ~/Deja/                  # wiki dir: should have people/, projects/, events/, goals.md
cat ~/.deja/deja.log | tail # no FATAL lines, OAuth errors, etc.
```

If `deja status` shows all green AND `~/Deja/index.md` exists with at least a few entries from backfill, you're done.

## Step 10 — cos sanity

```bash
deja cos enable             # if not already enabled
deja cos test               # fires cos once with a synthetic payload
deja cos tail               # shows recent cos invocations
```

`deja cos test` will spawn `claude -p` and run cos end-to-end. If it fails with "claude CLI not found," the `claude` binary isn't on the PATH Deja inherits — symlink it:

```bash
sudo ln -s "$(which claude)" /usr/local/bin/claude
```

---

## Failure modes and what to do

### Gatekeeper refuses to open (normal, not MDM)

Right-click Deja.app → Open. If the option is missing, `xattr -cr /Applications/Deja.app` to strip quarantine, then retry.

### MDM blocks app launch ("administrator has restricted...")

Signing your own build won't help — this is an allowlist at the bundle ID or team ID level. Options:

1. **Request allowlist** — file a ticket with corp IT for `com.deja.app`. Low probability of success for a personal side project.
2. **Run on a non-corp machine** with just work-account OAuth. Deja ingests work Gmail/Calendar via API without running on the corp laptop at all. Cleanest fallback.
3. **Abandon on this laptop**, revisit if policy changes.

### Google OAuth blocked by Workspace admin

Error reads something like "This app wasn't reviewed by your administrator."

Options:

1. **Request admin allowlist** — file a ticket asking to allowlist the Deja OAuth client. Low probability.
2. **Fall back to personal Google account** — sign in with the user's personal `@gmail.com` or personal Workspace account. Deja observes the personal account on the work Mac (weird but functional).
3. **Skip email/calendar entirely** — Deja still works with just screenshots + browser + voice + the wiki. The OAuth-dependent sources are powerful but not required. Set `email_enabled: false` and `calendar_enabled: false` in `~/.deja/config.yaml`.

### TCC toggles grayed out

MDM has locked the permission. Options per permission:

- **Screen Recording locked** → core vision doesn't work. Escalate to IT.
- **Accessibility locked** → reduced grounding; app still functions.
- **Microphone locked** → no voice; chat input still works.
- **Full Disk Access locked** → irrelevant if iMessage/WhatsApp disabled.

### Render proxy unreachable

All LLM calls go through `https://deja-api.onrender.com`. If the work network blocks it:

```bash
curl -I https://deja-api.onrender.com/v1/health
```

If this fails, check corp VPN / proxy settings. Usually resolvable; sometimes not.

### Claude CLI not working

cos and `browser_ask` both shell out to `claude -p`. Verify:

```bash
claude --version                # should print
claude -p "say hello"           # should respond in ~5s
```

If `claude -p` fails with auth errors, run `claude` interactively once to complete login. The token persists to `~/.claude/` and subprocess invocations use it.

### browser_ask fails specifically on sites with Cloudflare

Known limitation. TeamSnap, some other sites behind Cloudflare bot challenges, won't respond to automated Chrome extension access. Cos reports the auth wall rather than retrying.

---

## After successful install

The user's personal Mac Deja wiki is **NOT** synced to this work Mac's wiki. They're separate installations; separate `~/Deja/` directories; separate audit trails. Two cos instances that happen to share the user's Google Workspace login.

To check that the two instances aren't conflicting on shared resources:

- OAuth tokens are per-machine (stored in Keychain); fine.
- Gmail history cursor is per-machine; fine.
- Calendar writes: if both instances write to the primary calendar, the user sees `[Deja] ...` entries from both. Usually not an issue because different integrate cycles surface different signals, but watch for double-creates.

Report install outcome to the user: which gates cleared, which blocked, what's enabled/disabled.
