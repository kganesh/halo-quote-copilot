VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: install seed ingest eval redteam gate up down test lint clean

install:
	python3 -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e ".[dev]"

seed:
	$(PY) -m halo.seed.generate

ingest:
	$(PY) -m halo.rag.ingest

# Live: 20 questions against Bedrock, roughly $0.15 a run.
eval:
	$(VENV)/bin/halo eval

# Offline: 20 hostile notes through the real sourcing loop, no spend.
redteam:
	$(VENV)/bin/halo redteam

# Offline: the evaluation gates CI runs — fixtures, retrieval, grounding, red
# team, teardown.
gate:
	$(VENV)/bin/halo gate

# Live: stands the stack up. Nothing in it bills while idle; see infra/terraform.
up:
	cd infra/terraform && terraform init && terraform apply

# Live: takes it down, then checks what the account is still being charged.
down:
	cd infra/terraform && terraform destroy
	$(PY) -m halo.cli teardown --live

test:
	$(VENV)/bin/pytest -q

lint:
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests

clean:
	rm -rf data/seed data/atlas.db
