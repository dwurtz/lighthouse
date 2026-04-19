# Mobile signal channel — iOS Shortcuts → cos

Turn your iPhone into a one-tap capture surface for cos. Speak into the
Action Button, double-tap the back of the phone, or auto-send every
screenshot — each message lands in cos's command pipeline and writes
to the same `~/Deja/conversations/` store as voice, email, and notch
chat. Unified memory.

## Architecture (text-only for now; images on the followup)

```
iPhone → POST /v1/inbox → Render proxy (SQLite queue)
                                 ↓
local Deja (deja mobile poll) → drains every 5s → cos command invocation
```

The proxy keeps a small queue per user. Local Deja drains via its
existing Google bearer auth. Your iPhone uses a long-lived "mobile
key" so you don't have to refresh OAuth on the phone.

## One-time setup

### 1. Generate a mobile key on your Mac

```
deja mobile create-key --label iphone
```

Copies the plaintext key to your screen once. It looks like
`deja_<32 chars>`. Store it somewhere secure (1Password, Apple Keychain) —
you'll paste it into the Shortcut once. You can create multiple keys
(label them `iphone-work`, `ipad`, etc. if you want to distinguish
devices later; revocation UI is a followup).

### 2. Start the local drain poller

```
deja mobile poll
```

Foreground process. For now, keep a terminal open running this while
you test. Once we're happy with the behavior, we'll wire a launchd
agent so it starts with your Mac. Ctrl+C to stop.

### 3. Create the iOS Shortcut — "Note to Deja"

On your iPhone, open **Shortcuts** (built-in app). Create a new
shortcut with these actions in order:

1. **Dictate Text** — "Language: English (United States)", "Stop
   Listening: On Tap" (or "After Short Pause" if you want hands-free
   auto-stop).
2. **Get Contents of URL**
   - URL: `https://deja-api.onrender.com/v1/inbox`
   - Method: `POST`
   - Headers:
     - `Content-Type`: `application/json`
     - `X-Deja-Mobile-Key`: `<paste the key from step 1>`
   - Request Body: **JSON**
     - `text`: (Magic Variable → Dictated Text from step 1)
     - `source`: `ios-shortcut`
3. **Show Notification** — title "Sent to Deja", body: `Dictated Text`
   (so you see what actually went).

Name it **"Note to Deja"**. Save.

### 4. Bind it to a fast trigger

**Option A — Action Button** (iPhone 15 Pro / 16 Pro / later):
Settings → Action Button → scroll right to **Shortcut** → pick **Note
to Deja**. Long-press the button; dictate; release. ~3 seconds
end-to-end.

**Option B — Back Tap** (every iPhone since XS):
Settings → Accessibility → Touch → Back Tap → **Double Tap** → scroll
to Shortcuts → pick **Note to Deja**. Double-tap the back of the
phone; dictate; release.

**Option C — Siri**:
"Hey Siri, note to Deja" fires the shortcut. Works from CarPlay,
AirPods, locked screen.

**Option D — Home Screen icon**:
In Shortcuts, tap the shortcut details → "Add to Home Screen" →
customize icon. Gives you a one-tap tile.

### 5. (Optional) Screenshot automation

This phase is text-only. Image uploads from screenshots are a followup
once we wire the photo receive path through Claude Vision.

## How replies land

Right now cos's reply is *only* written to `~/Deja/conversations/`
(plus any actions it takes via MCP). You won't get a push back to the
phone yet. Once we add an APNs or SMS channel for replies, the same
shortcut can show the answer. For now: send a note, let cos act,
check the conversations folder / your calendar / drafts when you're
back at your Mac.

## Diagnostics

- **`deja mobile poll` logs** — shows every drain + route. Look at
  stderr for errors.
- **`~/.deja/chief_of_staff/invocations.jsonl`** — every cos
  invocation from a mobile note is logged here with
  `cycle_id=command/mobile-*`.
- **`deja trail`** — MCP mutations (wiki writes, calendar creates,
  reminder adds) cos performs in response.
- **Server-side**: `/v1/inbox` hits are logged on the Render proxy
  with the user's email + source. Rate-limited to 60/minute.

## Security model

- The mobile key is a long opaque string, stored only as a SHA-256
  hash on the proxy side. Plaintext lives only in your iOS Shortcut
  and whatever password manager you save it in.
- The proxy authorizes POSTs by looking up the hash → mapping to your
  Google-authenticated email. No token expiry; revocable by deleting
  the row server-side (CLI for that is a followup).
- Only `/v1/inbox` accepts the mobile key. `/v1/inbox/drain` requires
  a Google bearer token — mobile can send but can't read back, so a
  leaked key lets an attacker send you notes but not read your cos
  responses or drain what's pending.
- If a key does leak: rotate by `deja mobile create-key --label iphone-v2`,
  paste the new one in, and we'll ship a revoke command soon.

## What still needs building (followups)

- **Revocation**: `deja mobile revoke-key --label iphone`.
- **Image uploads**: `POST /v1/inbox` accepting `image_base64`, routed
  to `~/.deja/raw_images/mobile/` + fired into Claude Vision.
- **Push replies**: APNs token on first POST → cos's final reply
  pushed back to the phone. Or a separate SMS channel via Twilio.
- **launchd agent**: auto-start `deja mobile poll` at login.
- **iOS Shortcut bundle**: ship a signed `.shortcut` file users can
  import with one tap instead of building manually.
