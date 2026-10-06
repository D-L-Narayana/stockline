# StockLine developer shortcuts.  Every Python-side target delegates to the portable task
# runner scripts/dev.py, which selects the project interpreter itself ($STOCKLINE_PYTHON,
# else ./.venv/bin/python when present, else the interpreter running it).  PYTHON only
# chooses the interpreter that launches the runner.
PYTHON ?= python3
DEV := $(PYTHON) scripts/dev.py
SITE_DIR ?= site
BENCH_ARGS ?=
BROWSER_CHECK_OUT ?= /tmp/stockline-browser-check

.DEFAULT_GOAL := help
.PHONY: help install test cov lint compile run site smoke bench browser-check docker docker-run

help:  ## list the available targets
	@printf 'StockLine targets (make <target>):\n'
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-14s %s\n", $$1, $$2}'

install:  ## install runtime + dev dependencies into the project interpreter
	$(DEV) install

test:  ## run the test suite
	$(DEV) test

cov:  ## test suite with the coverage gate (>= 90 %)
	$(DEV) cov

lint:  ## ruff check app tests scripts
	$(DEV) lint

compile:  ## byte-compile app, tests and scripts
	$(DEV) compile

run:  ## run the API locally with auto-reload (http://127.0.0.1:8000)
	$(DEV) serve --reload

site:  ## build the static demo site into $(SITE_DIR)
	$(DEV) site --out $(SITE_DIR)

smoke:  ## start a temporary server and exercise the API end to end
	$(DEV) smoke

bench:  ## latency/throughput benchmark (scripts/bench.py; BENCH_ARGS="--n 500 --concurrency 8")
	$(DEV) py scripts/bench.py $(BENCH_ARGS)

browser-check:  ## Playwright workflow check against a self-started server
	node scripts/browser_check.mjs --out $(BROWSER_CHECK_OUT)

docker:  ## build the container image
	docker build -t stockline .

docker-run:  ## run the image with seeded demo data and a named data volume
	docker run --rm -p 8000:8000 -e STOCKLINE_SEED=1 -v stockline-data:/data stockline
