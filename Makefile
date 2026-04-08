.PHONY: test test-v install run dmg release

test:
	./venv/bin/python -m pytest tests/ -q

test-v:
	./venv/bin/python -m pytest tests/ -v

install:
	./venv/bin/python -m pip install -e .

run:
	./launch.sh

dmg:
	menubar/build.sh
	hdiutil create -volname "Deja" -srcfolder Deja.app -ov -format UDZO Deja.dmg
	@echo "Built Deja.dmg"

release:
	@if [ -z "$(VERSION)" ]; then echo "Usage: make release VERSION=0.2.1"; exit 1; fi
	git tag v$(VERSION)
	git push origin v$(VERSION)
	@echo "Tagged v$(VERSION) — GitHub Actions will build and upload the DMG"
