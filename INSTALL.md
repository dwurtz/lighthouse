# Installing Deja

## Quick install

```bash
curl -fsSL https://raw.githubusercontent.com/dwurtz/deja/main/install.sh | bash
```

That's it. The installer downloads the latest DMG, copies `Deja.app`
to `/Applications`, and launches it. Re-run the same one-liner later
to update.

## Prerequisites

The installer checks all of these and fails with a clear message if
anything's missing:

- **Apple Silicon Mac** (M1 or newer). Intel is not supported.
- **macOS 14 (Sonoma) or newer.**
- **[Claude Code](https://claude.com/code)** — the `claude` CLI must be
  on your `PATH`. Deja's integrate cycle (the every-five-minutes signal
  → wiki update pass) runs Claude Opus via a `claude -p` subprocess. If
  you haven't logged into Claude Code yet:

  ```bash
  claude /login
  ```

## What happens on first launch

1. Deja's icon appears in your menu bar (top-right, near the clock).
2. Click it to open the setup panel.
3. Grant the macOS permissions Deja needs — Screen Recording,
   Accessibility, Microphone, Contacts, Calendar, Full Disk Access,
   Notifications. Each is requested one at a time.
4. Sign in with your Google account to connect Workspace (Gmail,
   Calendar, Drive read access).
5. Deja runs a one-time 30-day backfill of your sent mail to
   bootstrap the wiki, then starts observing.

## Updating

Re-run the install command:

```bash
curl -fsSL https://raw.githubusercontent.com/dwurtz/deja/main/install.sh | bash
```

It compares your installed version against the latest GitHub release
and only downloads when newer. Use `--check` to see if an update is
available without installing it:

```bash
curl -fsSL https://raw.githubusercontent.com/dwurtz/deja/main/install.sh | bash -s -- --check
```

## Why we strip the quarantine flag

The installer runs `xattr -dr com.apple.quarantine /Applications/Deja.app`
after copying. This bypasses Gatekeeper's "Deja is damaged and can't be
opened" dialog, which appears for any app downloaded from the internet
that isn't notarized by Apple.

We're not enrolled in the Apple Developer Program ($99/year), so the
build isn't notarized. The trade-off:

- **Security**: you're trusting that the binary you downloaded from
  this repo's releases is what it claims to be. The DMG's hash is
  printed on the GitHub release page; you can verify it manually if
  you want.
- **Convenience**: skipping notarization means we can ship updates
  faster and you don't pay the Gatekeeper "first launch" tax.

If we ever enroll in the Developer Program, the installer will switch
to notarized builds and the quarantine-strip step will go away.

## Building from source

For developers who want to build from source instead of running the
installer:

```bash
# Prereqs (one-time)
xcode-select --install              # or full Xcode for the menubar app
brew install python@3.14

# Clone + setup
git clone https://github.com/dwurtz/deja
cd deja
python3.14 -m venv venv
./venv/bin/pip install -e .

# Bundle Python into the .app (Swift spawns subprocesses from
# Deja.app/Contents/Resources/python-env/, so this is required)
bash menubar/bundle-python.sh

# Build + install + launch
make dev
```

`make dev` builds with ad-hoc signing so you don't need a Developer ID
in your Keychain. TCC permission grants persist across rebuilds for
your local install (since your signature is stable for you), so the
build-from-source path is actually smoother than the DMG once you're
set up.

## Troubleshooting

**"Claude Code is required" error from the installer**

Install Claude Code from https://claude.com/code and run `claude /login`.
If `claude --version` works in your terminal, the installer should pass.

**App icon doesn't appear in the menu bar**

See [reference_tray_icon_debug.md](docs/tray-icon-debug.md) for the
diagnostic ladder. Most common cause: too many menu bar items on a
notched MacBook — macOS silently drops the icon behind the notch. Try
quitting one or two other tray apps.

**Updates aren't applying**

Deja's built-in Sparkle auto-update can fail because the build is
ad-hoc signed and signatures drift across releases. The reliable
update path is to re-run the install one-liner, which is idempotent.

**"Damaged" dialog despite the installer**

If you somehow get the Gatekeeper "damaged" dialog after the installer
ran, you can manually strip quarantine:

```bash
xattr -dr com.apple.quarantine /Applications/Deja.app
```

Then `open -a Deja`.
