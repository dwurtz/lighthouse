# Future Simplifications

Things we're intentionally doing in a more complex way today, waiting for upstream fixes.

## 1. Python-based local inference (waiting for mlx-swift + Xcode fix)

**Current state (complex):**
- Bundle Python 3.14 runtime in the app (~150MB)
- Bundle mlx-lm Python package for Qwen text inference (~360MB)
- Bundle mlx-vlm + torch + timm for FastVLM vision (~600MB)
- Total Python ML stack: ~1GB in the DMG
- Model weights downloaded on first launch (~7GB)
- Inference runs via IPC over Unix socket to a Python subprocess

**Desired state (simple):**
- No Python runtime for ML inference
- Pure Swift using `mlx-swift-lm` SPM package (Apple's official MLX bindings)
- Qwen3 and FastVLM both run in-process via `MLXLLM` / `MLXVLM` Swift APIs
- DMG size drops by ~1GB
- No subprocess, no IPC, no Python interpreter overhead
- Inference through direct Swift calls

**What's blocking this:**

### Blocker A: Xcode 26 "Explicit Modules" regression

**Symptom:** `error: unable to resolve module dependency: 'Numerics'` when building mlx-swift as an SPM dependency.

**Root cause:** Xcode 26 made "Swift Explicit Modules" the default build mode. mlx-swift's `Package.swift` declares a dependency on `swift-numerics` correctly, but Xcode 26's explicit module build system doesn't propagate the module search paths for transitive Swift dependencies to the mlx-swift target's compile commands.

**Evidence:**
- `swift-numerics` resolves successfully (all 15 packages in the resolution log)
- `Numerics.swiftmodule` is actually built and present in DerivedData
- `-I _NumericsShims/include` (C shim) is in the compile args
- But `-I` for the built Swift module is missing
- Error persists with `SWIFT_ENABLE_EXPLICIT_MODULES=NO`, `ONLY_ACTIVE_ARCH=YES`

**Known issue, not our bug:**
- Swift Forums thread: https://forums.swift.org/t/xcode-26-unable-to-find-module-dependency/80516
- swift-numerics issue: https://github.com/apple/swift-numerics/issues/174
- Multiple packages affected, not just mlx-swift

**What we need to wait for (either/or):**
1. Apple releases Xcode 26.1+ with the explicit modules regression fixed
2. mlx-swift team updates their `Package.swift` to work around the bug
3. swift-numerics releases a version that doesn't rely on the problematic module structure

**How to detect the fix:**
```bash
# Uncomment the MLXLM package in project.yml
# Run:
xcodegen generate && xcodebuild -project Deja.xcodeproj -scheme Deja -configuration Release build
# If this succeeds without the "Numerics" error, we're unblocked
```

### Blocker B: mlx-swift-lm VLM support maturity

Even if Blocker A is fixed, `mlx-swift-lm`'s VLM (vision) support may still be less mature than the Python `mlx-vlm` package. Need to verify FastVLM 0.5B works end-to-end in Swift before we remove the Python vision path.

## 2. mlx-lm slower than Ollama for text inference

**Current state:** Qwen3 8B via mlx-lm takes ~50 seconds per integration cycle.
**Ollama (llama.cpp backend)** takes ~14 seconds for the same prompt.

**Root cause:** llama.cpp has more optimized prefill batching and KV cache paths than mlx-lm's reference Python implementation.

**Acceptable for now:** Integration runs every 5 minutes, so 50s is fine. But ~3x faster would be nicer, especially for chat which is user-facing.

**Future options:**
- **mlx-swift** when available: should match or beat llama.cpp since it's Metal-native
- **Bundle llama.cpp directly:** compile as a library, sign as part of the app. ~20MB binary. Supports Qwen and vision models. Most engineering work but best perf.
- **Wait for mlx-lm optimizations:** The mlx team actively improves it

**Precedent for bundling llama.cpp:** This is a well-trodden path used by every major consumer local LLM app:
- **LM Studio** (closed-source, bundles llama.cpp under the hood)
- **Jan.ai** (open source, Electron, llama.cpp for all inference)
- **GPT4All** (Nomic AI, llama.cpp backend)
- **Msty** (polished UX, bundles llama.cpp)
- **Ollama** (literally a wrapper around llama.cpp)

So "bundle llama.cpp" is not experimental — it's what every local LLM app does. Typical footprint is ~20MB for the binary, Metal-accelerated on Apple Silicon, handles text + vision models.

**Trigger to reconsider:** If users report chat feels slow (>10s to first token) we should bundle llama.cpp.

## 3. Torch required for FastVLM's vision encoder

**Current state:** FastVLM's vision encoder is a PyTorch model. Even though MLX handles inference, torch must be loaded to convert the encoder. This pulls in 408MB of torch + 16MB of timm.

**Desired state:** Pure MLX implementation of FastVLM's vision encoder (FastViTHD), eliminating torch entirely. Would save ~425MB in the bundle.

**Blocking:** Waiting for either:
- Apple to publish a pure-MLX FastVLM implementation
- Someone in mlx-community to port FastViTHD to MLX

**How to detect the fix:**
```bash
# Check HuggingFace for pure-MLX FastVLM
huggingface-cli search "mlx-community FastVLM" | grep -v torch
```

## Review cadence

Check these every ~2 months or when major Xcode/mlx releases drop:
- Xcode 26.x release notes for module dependency fixes
- mlx-swift-lm releases for VLM improvements
- mlx-community HuggingFace for torch-free FastVLM
- User feedback on chat latency (trigger for llama.cpp migration)
