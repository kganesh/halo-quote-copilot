VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: install seed ingest eval test lint clean

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

test:
	$(VENV)/bin/pytest -q

lint:
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests

clean:
	rm -rf data/seed data/atlas.db
