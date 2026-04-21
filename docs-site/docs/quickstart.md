# Quickstart

Deja ships as a signed macOS app (Sparkle auto-updates) and as a Python backend you can run from source. Most people will want the DMG.

!!! warning "Early technical preview"
    Deja is currently being proven out on a single user. If you're reading this, you've been sent the link directly. The setup flow works, but expect rough edges — this isn't a public launch.

## Install from the DMG

1. Download the latest release from [trydeja.com/download](https://trydeja.com/download) (or the GitHub releases page if you have the link).
2. Open the DMG, drag **Deja** into `/Applications`, and launch it.
3. The first-run setup panel opens. It walks you through four things:

```mermaid
flowchart LR
    A[Google OAuth] --> B[TCC grants]
    B --> C[30-day backfill]
    C --> D[First monitor cycle]

    classDef source  fill:#1a365d,stroke:#2c5282,color:#f7fafc
    classDef wiki    fill:#22543d,stroke:#2f855a,color:#f7fafc
    classDef process fill:#744210,stroke:#975a16,color:#fefcbf
    classDef cos     fill:#975a16,stroke:#d69e2e,color:#fefcbf
    classDef aside   fill:#3d3d3d,stroke:#555,color:#ccc
    class A,B,C,D process
```

### Google OAuth

Deja needs scoped access to Gmail, Calendar, Drive, and Tasks under your Google Workspace account. The setup panel pops a browser tab for consent, and the resulting token is written to your macOS Keychain (with `~/.deja/google_token.json` as a fallback).

### TCC grants

macOS will prompt for four permissions. Each opens System Settings → Privacy & Security; grant and return.

| Permission | Why |
| ---------- | --- |
| **Full Disk Access** (Deja.app + bundled Python) | Read iMessage and WhatsApp databases |
| **Screen Recording** | Capture screenshots for the vision path |
| **Accessibility** | Read focused-app metadata for context |
| **Microphone** | Push-to-talk voice commands |

### 30-day backfill

The final setup step runs `deja onboard --days 30` in a subprocess: ingests 30 days of sent email, iMessage, WhatsApp, calendar, and Meet transcripts to bootstrap [the wiki](wiki.md). Takes 3–10 minutes depending on volume. You can close the setup panel and the backfill keeps running; the monitor will start the 3-second [observe](pipelines.md#observe) loop as soon as it's done.

### First monitor cycle

After setup completes, the Swift app spawns `deja monitor` (the observe/[integrate](pipelines.md#integrate)/[reflect](pipelines.md#reflect) pipelines) and `deja web` (the FastAPI backend on a Unix socket). The first integrate call fires about 5 minutes later.

The app lives in the menu bar — a notch-docked panel is available by clicking the icon. The notch shows a **Now** tab (most-recent observation narrative), a **Tasks** tab (from [`goals.md`](goals-file.md)), and a chat input.

## Install from source

If you want to hack on it:

```bash
git clone https://github.com/dwurtz/deja.git
cd deja
python -m venv venv && ./venv/bin/pip install -e .
./venv/bin/python -m deja configure   # OAuth + permissions
./venv/bin/python -m deja monitor     # run the backend
```

For the Swift menu-bar app, open `Deja.xcodeproj` in Xcode, or `make dev` from the repo root.

!!! note "Signing identity matters"
    TCC grants are keyed to the signing identity's code digest. If you rebuild with ad-hoc signing (`--sign -`), each rebuild re-prompts for Screen Recording. Use the stable `Deja Dev` identity configured in `menubar/build.sh` and keep the grants.

## After setup: the basics

### Voice and chat

- **Hold Option (⌥)** anywhere and speak. Release to send. The transcript classifies into one of five types (action, goal, automation, context, query).
- **Click the menubar icon** to open the notch panel. Chat input at the bottom; types the same five things.

### Debugging

```bash
deja trail                   # recent audit entries
deja status                  # liveness summary
deja cos tail                # live cos invocation log
deja cos test                # manual cos reflective fire
tail -f ~/.deja/deja.log
```

The wiki is at `~/Deja/`. Open it in Obsidian to browse by hand. Everything is a git repo — `git log` in that folder shows every agent write.

### Disabling things

- `deja cos disable` — stops [cos](cos.md) from firing. Observe + integrate continue.
- Edit `~/.deja/config.yaml` → set `screenshot_enabled: false` to pause screen capture.
- Quit the app and `deja monitor` / `deja web` stop too (PID files in `~/.deja/`).

### Auto-registering with AI clients

During setup, `mcp_install.py` detects installed AI clients and writes [MCP](mcp.md) configs into each:

- Claude Desktop
- Claude Code
- Cursor
- Windsurf
- VS Code (if detected)

Each client gets a stdio entry that runs `python -m deja mcp` against Deja's wiki. From your normal coding / writing surface, you can ask Claude or Cursor "what did I tell Jane last week?" and get a real answer from your own wiki.


## Support

This is a preview. If something breaks:

1. `deja health` — see which permissions and config are missing.
2. Check `~/.deja/deja.log` and `~/.deja/chief_of_staff/invocations.jsonl`.
3. Ping David directly.

Known rough edges are tracked in the repo issues tab.
