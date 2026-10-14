SHELLCHECK ?= docker run --rm -v "$(CURDIR)":/mnt -w /mnt koalaman/shellcheck:v0.10.0
PYTHON ?= python3
SHELL_SCRIPTS = ai/ai-mode ai/claude-local scripts/check-compose.sh

.PHONY: test compose shellcheck python demo

test: compose shellcheck python

compose:
	./scripts/check-compose.sh

shellcheck:
	$(SHELLCHECK) $(SHELL_SCRIPTS)

python:
	$(PYTHON) -m py_compile ai/status.py ai/bench.py
	$(PYTHON) -m pytest -q tests

demo:
	$(PYTHON) ai/status.py --mock
