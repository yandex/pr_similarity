PYTHON ?= python
CONFIG ?= config.yaml
OVERWRITE ?= 0
RETRY_FAILED ?= 0
LOG_LEVEL ?= INFO
PROGRESS ?= auto
export BASKET_CONSOLE_LOG_LEVEL = $(value LOG_LEVEL)
export BASKET_PROGRESS_MODE = $(value PROGRESS)
VENV_PYTHON := .venv/bin/python

.PHONY: setup env collect filter generate export basket test smoke smoke-live help

env:
	@if [ -e .env ] || [ -L .env ]; then printf '%s\n' '.env already exists'; else install -m 600 .env.example .env; printf '%s\n' 'Created .env; fill in the needed tokens locally'; fi

setup:
	@if [ ! -x "$(VENV_PYTHON)" ]; then \
		"$(PYTHON)" -m venv .venv || "$(PYTHON)" -m venv --without-pip .venv; \
	fi
	@"$(VENV_PYTHON)" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 required"'
	@if ! "$(VENV_PYTHON)" -m pip --version >/dev/null 2>&1; then "$(PYTHON)" -m pip --python .venv install --quiet --no-input pip; fi
	@"$(VENV_PYTHON)" -m pip install --require-hashes -r requirements.lock
	@"$(VENV_PYTHON)" -m pip install --no-build-isolation --no-deps --config-settings editable_mode=strict -e .

collect filter generate export basket:
	@test -x "$(VENV_PYTHON)" || { printf '%s\n' 'Run make setup first'; exit 2; }
	@"$(VENV_PYTHON)" -m basket_collector $@ --config "$(CONFIG)" --overwrite "$(OVERWRITE)" --retry-failed "$(RETRY_FAILED)" --log-level "$${BASKET_CONSOLE_LOG_LEVEL}" --progress "$${BASKET_PROGRESS_MODE}"

test:
	@test -x "$(VENV_PYTHON)" || { printf '%s\n' 'Run make setup first'; exit 2; }
	@"$(VENV_PYTHON)" -m pytest tests

smoke:
	@test -x "$(VENV_PYTHON)" || { printf '%s\n' 'Run make setup first'; exit 2; }
	@"$(VENV_PYTHON)" -m basket_collector smoke

smoke-live: CONFIG = config.smoke-live.yaml
smoke-live:
	@test -x "$(VENV_PYTHON)" || { printf '%s\n' 'Run make setup first'; exit 2; }
	@"$(VENV_PYTHON)" -m basket_collector smoke-live --config "$(CONFIG)" --overwrite "$(OVERWRITE)" --retry-failed "$(RETRY_FAILED)" --log-level "$${BASKET_CONSOLE_LOG_LEVEL}" --progress "$${BASKET_PROGRESS_MODE}"

help:
	@printf '%s\n' \
	  'basket_collector — collect coding tasks from merged GitHub/GitLab changes' \
	  '' \
	  'Run from this directory:' \
	  '  make setup        Create .venv and install dependencies (Python 3.12)' \
	  '  make env          Create a private .env without replacing an existing file' \
	  '  make basket       Run collect -> filter -> generate -> export' \
	  '  make collect      Fetch merged-change metadata and linked issues' \
	  '  make filter       Verify Git revisions and apply configured filters' \
	  '  make generate     Copy issue text or generate tasks with the configured LLM' \
	  '  make export       Write basket.json from generated tasks' \
	  '  make test         Run the offline test suite' \
	  '  make smoke        Run offline end-to-end checks' \
	  '  make smoke-live   Run a bounded live check (config.smoke-live.yaml by default)' \
	  '  make help         Show this help; no setup or credentials needed' \
	  '' \
	  'Variables:' \
	  '  PYTHON=python3.12  Interpreter for make setup (default: python)' \
	  '  CONFIG=<path>     YAML config (default: config.yaml; paths relative to YAML)' \
	  '  OVERWRITE=1       Rebuild stages; successful LLM tasks may incur new charges' \
	  '  RETRY_FAILED=1    Retry failed tasks while keeping successful ones' \
	  '  LOG_LEVEL=DEBUG   Console verbosity (default: INFO)' \
	  '  PROGRESS=auto     TTY bars or plain heartbeats; plain/off also accepted' \
	  '' \
	  'Examples (see README.md and examples/*.yaml):' \
	  '  make setup PYTHON=python3.12' \
	  '  make basket CONFIG=examples/github-llm-diff.yaml' \
	  '  make basket CONFIG=examples/github-llm-diff.yaml RETRY_FAILED=1' \
	  '' \
	  'Stage commands reuse saved progress. Resume with the same CONFIG.' \
	  'Live commands need repository API/Git access; LLM generation may be paid.'
