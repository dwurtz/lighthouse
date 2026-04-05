.PHONY: test test-v install run

test:
	./venv/bin/python -m pytest tests/ -q

test-v:
	./venv/bin/python -m pytest tests/ -v

install:
	./venv/bin/python -m pip install -e .

run:
	./launch.sh
