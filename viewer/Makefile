PYTHON ?= python3
VENV_PYTHON ?= .venv/bin/python
CONFIG ?= config.local.json
PACKAGE_INDEX_URL ?= https://pypi.org/simple
URL ?=
LOGIN ?=
ROLE ?=
RUN_ID ?=
ORDER_JSON ?=

export PRSIM_VIEWER_SETUP_PYTHON = $(value PYTHON)
export PRSIM_VIEWER_PYTHON = $(value VENV_PYTHON)
export PRSIM_VIEWER_CONFIG = $(value CONFIG)
export PRSIM_VIEWER_INDEX = $(value PACKAGE_INDEX_URL)
export PRSIM_VIEWER_URL = $(value URL)
export PRSIM_VIEWER_LOGIN = $(value LOGIN)
export PRSIM_VIEWER_ROLE = $(value ROLE)
export PRSIM_VIEWER_RUN_ID = $(value RUN_ID)
export PRSIM_VIEWER_ORDER_JSON = $(value ORDER_JSON)
_COMMAND_LINE_VARIABLES := $(foreach name,$(.VARIABLES),$(if $(filter command line,$(origin $(name))),$(name)))
unexport $(_COMMAND_LINE_VARIABLES) PYTHON VENV_PYTHON CONFIG PACKAGE_INDEX_URL URL LOGIN ROLE RUN_ID ORDER_JSON

.PHONY: help setup browser-setup test serve start stop status logs admin-profile-create admin-role-set admin-profile-password-set admin-user-create admin-user-enable admin-user-disable admin-password-set admin-basket-order admin-run-delete admin-refresh

setup:
	@set -e; \
	if [ -e .venv ] || [ -L .venv ] || [ -x "$${PRSIM_VIEWER_PYTHON}" ]; then \
	  while :; do \
	    printf '%s' 'Virtual environment already exists. Reinstall dependencies? [Y/n] '; \
	    if ! IFS= read -r setup_answer; then printf '\n%s\n' 'Setup cancelled: no answer received.' >&2; exit 1; fi; \
	    case "$${setup_answer}" in \
	      ''|[Yy]|[Yy][Ee][Ss]) break ;; \
	      [Nn]|[Nn][Oo]) printf '%s\n' 'Setup skipped.'; exit 0 ;; \
	      *) printf '%s\n' 'Please answer yes or no.' ;; \
	    esac; \
	  done; \
	fi; \
	if [ ! -x "$${PRSIM_VIEWER_PYTHON}" ]; then "$${PRSIM_VIEWER_SETUP_PYTHON}" -m venv .venv || "$${PRSIM_VIEWER_SETUP_PYTHON}" -m venv --without-pip .venv; fi; \
	"$${PRSIM_VIEWER_PYTHON}" -c 'import sys; assert sys.version_info >= (3, 12), "Python >=3.12 required"'; \
	if ! "$${PRSIM_VIEWER_PYTHON}" -m pip --version >/dev/null 2>&1; then PIP_INDEX_URL="$${PRSIM_VIEWER_INDEX}" "$${PRSIM_VIEWER_SETUP_PYTHON}" -m pip --python .venv install pip; fi; \
	PIP_INDEX_URL="$${PRSIM_VIEWER_INDEX}" "$${PRSIM_VIEWER_PYTHON}" -m pip install --upgrade pip; \
	PIP_INDEX_URL="$${PRSIM_VIEWER_INDEX}" "$${PRSIM_VIEWER_PYTHON}" -m pip install --require-hashes -r requirements-build.lock; \
	PIP_INDEX_URL="$${PRSIM_VIEWER_INDEX}" "$${PRSIM_VIEWER_PYTHON}" -m pip install -r requirements.lock; \
	"$${PRSIM_VIEWER_PYTHON}" -m pip install --no-build-isolation --no-deps -e .

browser-setup:
	@"$${PRSIM_VIEWER_PYTHON}" -m playwright install chromium

test:
	@"$${PRSIM_VIEWER_PYTHON}" -m pytest tests

serve start stop status logs admin-profile-create admin-role-set admin-profile-password-set admin-user-create admin-user-enable admin-user-disable admin-password-set admin-basket-order admin-run-delete admin-refresh:
	@"$${PRSIM_VIEWER_PYTHON}" -m viewer $@

help:
	@printf '%s\n' \
	  'PR Similarity Viewer - standalone web UI and run publication API' \
	  '' \
	  'Run from this directory:' \
	  '  make help                 Show this help; no setup or credentials needed' \
	  '  make setup                Create .venv and install dependencies (Python >=3.12)' \
	  '  make serve                Run in the foreground for a process manager' \
	  '  make start                Start a local background service' \
	  '  make status               Show the local background service status' \
	  '  make logs                 Show its log' \
	  '  make stop                 Stop the local background service' \
	  '  make browser-setup        Download Chromium for browser tests' \
	  '  make test                 Run the offline test suite, including browser tests' \
	  '' \
	  'Local provisioning (on the server as the runtime owner):' \
	  '  make admin-profile-create LOGIN=admin' \
	  '  make admin-profile-password-set LOGIN=admin  Recover a password locally' \
	  '  make admin-role-set LOGIN=student ROLE=admin' \
	  '' \
	  'Administration (also available through Users and Settings):' \
	  '  make admin-user-create LOGIN=student' \
	  '  make admin-user-enable LOGIN=student' \
	  '  make admin-user-disable LOGIN=student' \
	  '  make admin-password-set LOGIN=student' \
	  '  make admin-basket-order   Prompt for a JSON array of basket hashes' \
	  '  make admin-run-delete RUN_ID=<uuid>' \
	  '  make admin-refresh' \
	  '' \
	  'Variables:' \
	  '  CONFIG=<path>             JSON config (default: config.local.json)' \
	  '  PYTHON=<interpreter>      Setup interpreter (default: python3; must be >=3.12)' \
	  '  VENV_PYTHON=<path>        Runtime interpreter (default: .venv/bin/python)' \
	  '  PACKAGE_INDEX_URL=<url>   Setup package index (default: https://pypi.org/simple)' \
	  '  URL=<https-origin>       Optional origin for HTTP admin commands' \
	  '  LOGIN / ROLE / RUN_ID / ORDER_JSON select the administrative operation' \
	  '  ROLE=user|admin; ORDER_JSON is a shell-quoted JSON array of basket hashes' \
	  '' \
	  'Quick local start:' \
	  '  make setup PYTHON=python3.12' \
	  '  cp examples/mock.json config.local.json' \
	  '  make start' \
	  '  make admin-profile-create LOGIN=admin' \
	  '' \
	  'For a new deployment use examples/s3.json, private S3 storage and HTTPS.' \
	  'Passwords use hidden prompts. See README.md for setup and advanced settings.'
