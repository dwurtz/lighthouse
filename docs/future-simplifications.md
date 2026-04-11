# Future Simplifications

Things we're intentionally doing in a more complex way today, waiting for upstream fixes.

## 1. Python-based FastVLM (waiting for mlx-swift + Xcode fix)

**Current state (complex):**
- Bundle Python 3.14 runtime in the app (~150MB)
- Bundle mlx-vlm + mlx + transformers + torch + tokenizers for FastVLM (~425MB)
- Total Python ML stack: ~575MB added to the .app
- FastVLM 0.5B weights downloaded on first launch (~1.4GB)
- Inference runs in-process inside the bundled Python subprocess

**Desired state (simple):**
- No Python ML stack
- Pure Swift using `mlx-swift-lm` SPM package (Apple's official MLX bindings)
- FastVLM runs in-process via `MLXVLM.FastVLM` Swift APIs
- DMG drops by ~575MB
- No transformers / torch / Python ML baggage

**What's blocking this: Xcode 26 "Explicit Modules" regression**

**Symptom:** `error: unable to resolve module dependency: 'Numerics'` when building mlx-swift as an SPM dependency. mlx-swift → swift-numerics transitive dep fails to resolve under Xcode 26's default explicit-modules build mode.

**Workarounds attempted in 2026-04 and failed:**

| Attempt | Result |
|---|---|
| `SWIFT_ENABLE_EXPLICIT_MODULES=NO` + `CLANG_ENABLE_EXPLICIT_MODULES=NO` | Error changes to `no such module 'Numerics'` at compile time. `Numerics.swiftmodule` IS built under `Intermediates.noindex/swift-numerics.build/...` but is never staged into `PackageFrameworks/` or a location on the cross-target module search path. With explicit modules off, Xcode 26 doesn't wire sibling SPM targets' module outputs together. |
| Pinning `swift-numerics` to 1.0.2 (pre-Embedded-support, pre-Sendable) | Same failure — not version-specific. |
| Pinning `swift-numerics` to 1.0.3 (sendable Complex, pre-1.1) | Same failure. |
| Check Xcode 26.5 beta release notes for SPM/explicit-modules fixes | Release notes are thin (1823 chars total, mostly Instruments + StoreKit). No mention of Swift Package Manager, explicit modules, cross-package resolution, or Numerics. `xcodes` CLI's catalog doesn't even see 26.5 beta yet. |

**Confirmed test environment:** Xcode 26.4 (17E192), macOS 26.3.1, mlx-swift-lm 2.31.3, mlx-swift 0.31.3, swift-numerics 1.0.2 / 1.0.3 / 1.1.1.

**Real path forward (either/or):**
1. Apple releases an Xcode 26.x that fixes cross-package explicit-modules module resolution
2. mlx-swift team restructures `Package.swift` to avoid the Numerics transitive dep path that trips the bug
3. Install Xcode 16.4 side-by-side — but Apple's compatibility matrix says 16.4 only supports macOS 15.3 – 26.1.x, and we're on 26.3.1 (outside the documented range)

**How to detect the fix:**
```bash
# Uncomment the MLXLM package in project.yml, then:
xcodegen generate && xcodebuild -project Deja.xcodeproj -scheme Deja -configuration Release build
# If this succeeds without the "Numerics" error, we're unblocked — rip
# out the Python ML stack (see src/deja/vision_local.py and
# menubar/bundle-python.sh exclusion list for what to remove).
```

**Related:**
- Swift Forums thread: https://forums.swift.org/t/xcode-26-unable-to-find-module-dependency/80516

## 2. Local text integration model — RESOLVED (moved to cloud)

Previously we ran Qwen3 8B locally via bundled llama.cpp for the
integration cycle. As of 2026-04, we rely on Gemini Flash-Lite via the
Render proxy (`DEJA_API_URL`) for integration. Rationale:
- Flash-Lite hit 80% must-contain on the eval vs. Qwen3's lower ceiling
- Flash-Lite prices drop ~30-60% across Gemini generations; cost trends down
- Removed: 5GB Qwen3 GGUF download, `llama-cli` binary, ~7GB bundled binaries path
- Kept: Vision is still on-device (FastVLM via mlx-vlm) so screenshots never leave the Mac

## 3. llama.cpp migration — REVERTED

The April 2026 llama.cpp migration (vendored `llama-cli` + `llama-mtmd-cli`
binaries, Qwen3 8B for text, Qwen2.5-VL 3B for vision) shipped briefly
then was reverted after benchmarking revealed Qwen2.5-VL 3B couldn't hit
the 6s screenshot budget (35s/call in subprocess mode; 28s even with
`llama-server` warm; `--image-max-tokens 512` got to 5-6s but started
hallucinating). The eval memory's original winner — FastVLM 0.5B via
mlx-vlm at 3.5s — turned out to be the only local model that fit both
the latency and accuracy budget.

## Review cadence

Check every ~2 months or when major Xcode releases drop:
- Xcode 26.x release notes for module dependency fixes (unblocks #1)
- New `mlx-vlm` / `mlx-swift-lm` releases (track FastVLM perf improvements)
- Gemini Flash-Lite pricing (affects the cost argument for #2's decision)
