# Pre-launch checklist

Running list of things that must be done before Deja ships commercially as a paid monthly service. Update in-place as items land; don't delete — strike through ~~completed items~~ and add a commit hash + date.

## Signing & distribution

- [ ] **Apple Developer ID Application certificate.** Current dev builds use a self-signed "Deja Dev" cert with no TeamIdentifier, which causes macOS TCC to anchor permission grants to the binary's cdhash instead of the code-signature designated requirement. Every rebuild invalidates the grant and re-prompts the user, even though System Settings shows the permission as already granted (a "ghost toggle" pointing at the old cdhash). A proper Developer ID cert ($99/yr Apple Developer Program) fixes this — grants survive rebuilds, no ghost toggles, no Gatekeeper warnings on first launch. Required for notarization too. Update `CODE_SIGN_IDENTITY` in `project.yml` from `"Deja Dev"` to `"Developer ID Application: <Name> (<TEAM_ID>)"`.
- [ ] **Notarization.** Run `xcrun notarytool submit` on the DMG before distributing. Needed so macOS doesn't show "unidentified developer" warnings when users install from the website download.
- [ ] **Sparkle EdDSA signing.** Releases need to be signed with the Ed25519 private key so Sparkle verifies updates. Already partially wired — verify the key is safe and the signing script runs as part of `make release`.
- [ ] **`trydeja.com` with download page.** Simple static site with the latest `.dmg` link and an appcast URL that Sparkle can poll for updates.

## Subscription & billing

- [ ] **Stripe (or Lemon Squeezy) integration on the Render proxy.** Monthly plan, free trial, cancel anytime. Subscription status keyed on the user's Google OAuth email.
- [ ] **Proxy gates on subscription status.** Every `/v1/generate`, `/v1/chat`, `/v1/transcribe` call checks subscription. Expired/free users get HTTP 402 with an `upgrade_url` payload.
- [ ] **Swift 402 handler.** App catches the 402 response and surfaces an upgrade prompt in the notch panel. No new UI component — reuse the error toast with an action button.

## Product polish

- [ ] **Onboarding flow: handle all 4 permissions in setup.** Currently the setup wizard asks for Screen Recording, Accessibility, Microphone, and Full Disk Access. Verify each still has correct TCC prompting behavior under Developer ID signing (once that lands).
- [ ] **Claude Code plugin marketplace repo.** Once Anthropic's registry stabilizes, publish a `.claude-plugin/` manifest pointing at the installed `Deja.app/Contents/Resources/python-env/bin/python -m deja mcp`. Discovery-only — app is still the primary install path.
- [ ] **First-run welcome explaining the wiki + Obsidian.** Most users won't know what Obsidian is or why there's a `~/Deja/` folder. Brief explanation in the setup wizard.

## Cleanup / risk

- [ ] **WhatsApp outbound capture asymmetry.** iMessage now captures both directions (attributedBody decoder landed); WhatsApp still only captures inbound. Parity fix.
- [ ] **Contact buffer hot-reload.** Today the contacts index is built once at startup. Add mtime-watcher so new contacts added to macOS Contacts are picked up without a restart.
- [ ] **Delete fabricated/damaged page detection.** One-off scrub tool the user can run if the vision feedback loop ever corrupts their wiki again (like the josh-eleven incident).
- [ ] **Automation eval harness.** Fixture set of `signal batch → expected goal_action` pairs that CI can replay on every prompt change. Without this, automation-matching regressions from prompt edits go silent.

## Support surface

- [ ] **`support@tryDeja.com` inbox actually works.** The error toast and health panel both open `mailto:support@tryDeja.com` pre-filled with request ID. Today the domain's MX records aren't set up, so those mails bounce. Provision the mailbox (Google Workspace or similar) and confirm with a live send before any user can hit this path. Without this, every "loud" error surface we built is a dead end.
- [ ] **Send Logs to Support** action actually sends somewhere. Today it's a mailto: link or similar; wire it to a support intake endpoint on the Render proxy so logs land in a queue you can triage.
- [ ] **Crash reporting.** Hook up Swift crash reports + Python uncaught-exception logging to the same intake endpoint.
- [ ] **Basic FAQ / docs site.** Linked from the Settings panel's Support section.

## Known debts (deferred, not blockers)

- `MonitorState.swift` is 950+ lines (god object). Split when it starts hurting.
- Two parallel retrieval codepaths (`wiki_retriever.build_analysis_context` + `mcp_server._get_context`) — worth unifying but not urgent.
- `integration_fixtures/` grows unboundedly; add rotation eventually.
- Goals.md is a hand-parsed Markdown file; consider structured storage if the parser starts breaking.
