.PHONY: test test-v test-swift test-live install run dev dmg release bump

test: test-swift
	./venv/bin/python -m pytest tests/ -q

test-v: test-swift
	./venv/bin/python -m pytest tests/ -v

# Live integration tests — hit real APIs (Google Workspace via gws,
# Gemini vision). Require auth + network. Run explicitly after:
#   - upgrading gws
#   - touching observers in src/deja/observations/
#   - touching the vision pipeline
test-live:
	./venv/bin/python -m pytest tests/ -m "live_gws or vision" -v

# Swift unit tests — currently just the iMessage attributedBody
# decoder regression test. See scripts/test_imessage_decoder.swift
# for context on the 2026-04-11 carpool bug.
test-swift:
	@swiftc -o /tmp/deja_decoder_test \
		scripts/test_imessage_decoder.swift \
		menubar/Sources/Services/AttributedBodyDecoder.swift
	@/tmp/deja_decoder_test

install:
	./venv/bin/python -m pip install -e .

run:
	./launch.sh

dev:
	xcodebuild -project Deja.xcodeproj -scheme Deja -configuration Release build SYMROOT=build -quiet
	@rsync -a --delete build/Release/Deja.app/ /Applications/Deja.app/
	@pkill -x Deja 2>/dev/null || true
	@sleep 1
	@open /Applications/Deja.app
	@echo "Rebuilt and relaunched Deja.app"

dmg:
	xcodebuild -project Deja.xcodeproj -scheme Deja -configuration Release build SYMROOT=build -quiet
	hdiutil create -volname "Deja" -srcfolder build/Release/Deja.app -ov -format UDZO Deja.dmg
	@echo "Built Deja.dmg"

release:
	@if [ -z "$(VERSION)" ]; then echo "Usage: make release VERSION=0.2.1"; exit 1; fi
	git tag v$(VERSION)
	git push origin v$(VERSION)
	@echo "Tagged v$(VERSION) — GitHub Actions will build and upload the DMG"

bump:
	@if [ -z "$(VERSION)" ]; then echo "Usage: make bump VERSION=0.3.0"; exit 1; fi
	@echo "Bumping version to $(VERSION)..."
	sed -i '' 's/^version = ".*"/version = "$(VERSION)"/' pyproject.toml
	sed -i '' 's/CFBundleVersion: ".*"/CFBundleVersion: "$(VERSION)"/' project.yml
	sed -i '' 's/CFBundleShortVersionString: ".*"/CFBundleShortVersionString: "$(VERSION)"/' project.yml
	sed -i '' 's|<string>[0-9]*\.[0-9]*\.[0-9]*</string>|<string>$(VERSION)</string>|g' Deja-Info.plist
	sed -i '' 's/version="[0-9]*\.[0-9]*\.[0-9]*"/version="$(VERSION)"/' src/deja/web/app.py src/deja/mcp_server.py server/app.py
	@echo "Updated version to $(VERSION) in all files"
	@echo "Run 'make release VERSION=$(VERSION)' to tag and push"
