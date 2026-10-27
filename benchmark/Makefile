PYTHON ?= python
PACKAGE_INDEX_URL ?= https://pypi.org/simple
export PYTHONHASHSEED := 42
VENV_PYTHON := .venv/bin/python

.PHONY: setup env test smoke help

setup:
	@set -e; \
	if [ -e .venv ] || [ -L .venv ] || [ -x "$${PRSIM_BM_VENV_PYTHON}" ]; then \
		while :; do \
			printf '%s' 'Virtual environment already exists. Reinstall dependencies? [Y/n] '; \
			if ! read -r setup_answer; then printf '%s\n' 'Setup confirmation requires input.' >&2; exit 1; fi; \
			case "$$setup_answer" in \
				''|[Yy]|[Yy][Ee][Ss]) break ;; \
				[Nn]|[Nn][Oo]) printf '%s\n' 'Setup skipped.'; exit 0 ;; \
				*) printf '%s\n' 'Please answer Y or n.' ;; \
			esac; \
		done; \
	fi; \
	if [ ! -x "$${PRSIM_BM_VENV_PYTHON}" ]; then \
		"$${PRSIM_BM_SETUP_PYTHON}" -m venv .venv || "$${PRSIM_BM_SETUP_PYTHON}" -m venv --without-pip .venv; \
	fi; \
	"$${PRSIM_BM_VENV_PYTHON}" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 required"'; \
	if ! "$${PRSIM_BM_VENV_PYTHON}" -m pip --version >/dev/null 2>&1; then "$${PRSIM_BM_SETUP_PYTHON}" -m pip --python .venv install --quiet --no-input pip; fi; \
	PIP_INDEX_URL="$${PRSIM_BM_PACKAGE_INDEX_URL}" "$${PRSIM_BM_VENV_PYTHON}" -m pip install --upgrade pip; \
	PIP_INDEX_URL="$${PRSIM_BM_PACKAGE_INDEX_URL}" "$${PRSIM_BM_VENV_PYTHON}" -m pip install --require-hashes -r requirements-build.lock; \
	PIP_INDEX_URL="$${PRSIM_BM_PACKAGE_INDEX_URL}" "$${PRSIM_BM_VENV_PYTHON}" -m pip install --no-build-isolation --require-hashes -r requirements.lock; \
	"$${PRSIM_BM_VENV_PYTHON}" -m pip install --no-build-isolation --no-deps --config-settings editable_mode=strict -e .; \
	"$${PRSIM_BM_VENV_PYTHON}" -m benchmark.execution.python_runtime

env:
	@if [ -e .env ] || [ -L .env ]; then printf '%s\n' '.env already exists'; else install -m 600 .env.example .env; printf '%s\n' 'Created .env; fill in the needed tokens locally'; fi

test:
	@test -x "$${PRSIM_BM_VENV_PYTHON}" || { printf '%s\n' 'Run make setup first'; exit 2; }
	@"$${PRSIM_BM_VENV_PYTHON}" -m pytest tests

smoke:
	@test -x "$${PRSIM_BM_VENV_PYTHON}" || { printf '%s\n' 'Run make setup first'; exit 2; }
	@"$${PRSIM_BM_VENV_PYTHON}" -m pytest tests/test_bin_projection.py tests/test_package.py tests/test_config.py tests/test_agent_input.py tests/test_run_store.py tests/test_storage.py tests/test_basket.py::test_frozen_basket_without_golden_diff tests/test_workspace.py::test_source_contains_only_before_with_fresh_root tests/test_workspace.py::test_capture_tracked_untracked_empty_and_ignore tests/test_workspace.py::test_excluded_reference_stays_missed tests/test_scoring.py tests/test_metric_preflight.py tests/test_provisioning.py tests/test_auth.py tests/test_process.py tests/test_opencode.py tests/test_native_errors.py tests/test_sandbox.py tests/test_python_runtime.py tests/test_python_lifecycle.py tests/test_runner.py tests/test_rescore.py tests/test_make.py tests/test_telemetry.py tests/test_pricing.py tests/test_repricing.py tests/test_commands.py tests/test_progress.py tests/test_archive.py tests/test_publish.py tests/test_publication_progress.py tests/test_visualize.py tests/test_visualize_data.py tests/test_score_process.py tests/test_score_resume.py

CONFIG ?= examples/full.yaml
DATASET ?= basket.json
RESUME ?=
RUN ?=
WORKERS ?=
LOG_LEVEL ?= INFO
PROGRESS ?= auto
REPRICE ?= 0
PRICING ?=
AUTH_FLOW ?= headless

OPEN_BROWSER ?= 0
URL ?=
DRY_RUN ?= 0
OVERWRITE ?= 0
TOKEN_FILE ?=
TOKEN_ENV ?=
TOKEN_PROMPT ?= 0

# Pass values through the environment, never interpolate paths/JSON into shell code.
export PRSIM_BM_CONFIG = $(value CONFIG)
export PRSIM_BM_DATASET = $(value DATASET)
export PRSIM_BM_OUTPUT = $(if $(filter undefined,$(origin OUTPUT)),,$(value OUTPUT))
export PRSIM_BM_RESUME = $(value RESUME)
export PRSIM_BM_RUN = $(value RUN)
export PRSIM_BM_WORKERS = $(value WORKERS)
export PRSIM_BM_LOG_LEVEL = $(value LOG_LEVEL)
export PRSIM_BM_PROGRESS = $(value PROGRESS)
export PRSIM_BM_REPRICE = $(value REPRICE)
export PRSIM_BM_PRICING = $(value PRICING)
export PRSIM_BM_AUTH_FLOW = $(value AUTH_FLOW)
export PRSIM_BM_OPEN_BROWSER = $(value OPEN_BROWSER)
export PRSIM_BM_URL = $(value URL)
export PRSIM_BM_DRY_RUN = $(value DRY_RUN)
export PRSIM_BM_OVERWRITE = $(value OVERWRITE)
export PRSIM_BM_TOKEN_FILE = $(value TOKEN_FILE)
export PRSIM_BM_TOKEN_ENV = $(value TOKEN_ENV)
export PRSIM_BM_TOKEN_PROMPT = $(value TOKEN_PROMPT)
_SUPPORTED_MAKE_VARIABLES := CONFIG DATASET OUTPUT RESUME RUN WORKERS LOG_LEVEL PROGRESS REPRICE PRICING AUTH_FLOW OPEN_BROWSER URL DRY_RUN OVERWRITE TOKEN_FILE TOKEN_ENV TOKEN_PROMPT PYTHON PACKAGE_INDEX_URL VENV_PYTHON
export PRSIM_BM_UNSUPPORTED = $(foreach name,$(filter-out $(_SUPPORTED_MAKE_VARIABLES),$(.VARIABLES)),$(if $(filter command line,$(origin $(name))),$(name)))

.PHONY: run rescore reprice auth visualize publish

run rescore reprice auth visualize publish:
	@test -x "$${PRSIM_BM_VENV_PYTHON}" || { printf '%s\n' 'Run make setup first'; exit 2; }
	@"$${PRSIM_BM_VENV_PYTHON}" -m benchmark $@

# GNU Make implicitly exports command-line variables too; never expand those original values.
_COMMAND_LINE_VARIABLES := $(foreach name,$(.VARIABLES),$(if $(filter command line,$(origin $(name))),$(name)))
unexport $(_SUPPORTED_MAKE_VARIABLES) $(_COMMAND_LINE_VARIABLES)
export PRSIM_BM_SETUP_PYTHON = $(value PYTHON)
export PRSIM_BM_PACKAGE_INDEX_URL = $(value PACKAGE_INDEX_URL)
export PRSIM_BM_VENV_PYTHON = $(value VENV_PYTHON)

help:
	@printf '%s\n' \
	  'benchmark — evaluate OpenCode on frozen GitHub/GitLab coding tasks' \
	  '' \
	  'Run from this directory:' \
	  '  make setup        Create .venv and install pinned dependencies (Python 3.12)' \
	  '  make env          Create a private .env without replacing an existing file' \
	  '  make run          Run tasks; requires CONFIG and a frozen DATASET' \
	  '  make auth         Log in to ChatGPT subscription OAuth using CONFIG' \
	  '  make rescore      Recompute quality from saved triplets; requires RUN' \
	  '  make reprice      Recompute API-equivalent cost; requires RUN and PRICING' \
	  '  make visualize    Write local report.html; requires RUN' \
	  '  make publish      Validate/archive a run and upload to URL; DRY_RUN=1 skips upload' \
	  '  make test         Run the offline test suite' \
	  '  make smoke        Run offline integration and package checks' \
	  '  make help         Show this help; no setup or credentials needed' \
	  '' \
	  'Variables:' \
	  '  PYTHON=python3.12  Interpreter for setup (default: python)' \
	  '  PACKAGE_INDEX_URL  Setup package index (default: https://pypi.org/simple)' \
	  '  CONFIG=<path>     YAML config (default: examples/full.yaml); YAML paths relative to its file' \
	  '  DATASET=<path>    Frozen basket.json (default: basket.json)' \
	  '  OUTPUT=<path>     Parent directory for named runs (default: ./output)' \
	  '  RESUME=<run-dir>  Continue an existing run; do not combine with OUTPUT' \
	  '  RUN=<run-dir>     Saved run for report operations' \
	  '  WORKERS=<number>  Override worker count on run/resume' \
	  '  LOG_LEVEL=INFO    Console level: DEBUG/INFO/WARNING/ERROR; warnings stay visible' \
	  '  PROGRESS=auto     Run task rows or publish elapsed feedback; plain/off accepted' \
	  '  YAML tags         Setup labels; top-level tags: [] preserves order and duplicates' \
	  '  REPRICE=1         Price on run/resume; also requires PRICING=<policy.yaml>' \
	  '  AUTH_FLOW=headless  OAuth login flow; browser also accepted' \
	  '  OPEN_BROWSER=1   Open saved HTML in a browser (default: 0)' \
	  '  URL=<https-url>  Viewer origin for publish; DRY_RUN=1 validates locally' \
	  '  OVERWRITE=1      Explicitly replace an existing publication' \
	  '  TOKEN_ENV=<name> / TOKEN_FILE=<path> / TOKEN_PROMPT=1  Choose one publish credential source' \
	  '' \
	  'Examples (see README.md and examples/*.yaml):' \
	  '  make run CONFIG=examples/openai-api-key.yaml DATASET=/path/to/basket.json' \
	  '  make run CONFIG=examples/openai-tdd.yaml DATASET=/path/to/basket.json' \
	  '  make auth CONFIG=examples/chatgpt-oauth.yaml' \
	  '  make run CONFIG=examples/chatgpt-oauth.yaml DATASET=/path/to/basket.json' \
	  '  make run CONFIG=examples/full.yaml DATASET=basket.json RESUME=./output/RUN_ID' \
	  '  make visualize RUN=./output/RUN_ID' \
	  '  make publish RUN=./output/RUN_ID URL=https://viewer.example DRY_RUN=1' \
	  '' \
	  'OpenCode is downloaded at the exact configured version; no separate install needed.' \
	  'Live runs need Git/model access and may incur provider charges. Keep secrets in .env or the environment.'
