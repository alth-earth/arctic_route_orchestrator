MAMBA_PREFIX := $(CURDIR)/.mamba-env
MAMBA_ROOT_PREFIX := $(CURDIR)/.mamba-root
UV := $(if $(wildcard $(MAMBA_PREFIX)/bin/uv),$(MAMBA_PREFIX)/bin/uv,uv)
export UV_CACHE_DIR ?= $(CURDIR)/.uv-cache
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.uv-python
export UV_PYTHON_DOWNLOADS ?= never
export ECCODES_DIR ?= $(MAMBA_PREFIX)

.PHONY: env-create env-update lock sync lint test integration check clean

env-create:
	mamba env create --root-prefix $(MAMBA_ROOT_PREFIX) --prefix $(MAMBA_PREFIX) -f environment.yml --yes

env-update:
	mamba env update --root-prefix $(MAMBA_ROOT_PREFIX) --prefix $(MAMBA_PREFIX) -f environment.yml --prune --yes

lock:
	$(UV) lock --python "$(if $(wildcard $(MAMBA_PREFIX)/bin/python),$(MAMBA_PREFIX)/bin/python,python3)"

sync:
	$(UV) sync --locked --python "$(if $(wildcard $(MAMBA_PREFIX)/bin/python),$(MAMBA_PREFIX)/bin/python,python3)"

lint:
	$(UV) run --locked ruff check src tests

test:
	$(UV) run --locked pytest -m "not integration and not real_artifact"

integration:
	$(UV) run --locked pytest -m integration

check: lint test integration
	$(UV) lock --check --python "$(if $(wildcard $(MAMBA_PREFIX)/bin/python),$(MAMBA_PREFIX)/bin/python,python3)"
	$(UV) sync --check --python "$(if $(wildcard $(MAMBA_PREFIX)/bin/python),$(MAMBA_PREFIX)/bin/python,python3)"
	$(UV) run --locked arctic-route-orchestrator --help

clean:
	rm -rf .venv .pytest_cache .ruff_cache .coverage htmlcov

