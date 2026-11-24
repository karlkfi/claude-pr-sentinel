# pr-sentinel — developer tasks.
# `make check` mirrors CI: shellcheck the scripts, run the test suite, lint the
# backlog store, check Markdown line breaks.

.PHONY: check shellcheck test backlog mdreflow md-reflow md-reflow-check \
        md-reflow-diff md-reflow-explain

check: shellcheck test backlog md-reflow-check

shellcheck:
	shellcheck scripts/pr-sentinel-watch.sh scripts/alloc-queue-id.sh

test:
	python3 -m unittest discover tests

backlog:
	python3 scripts/queue.py lint

# Rebuilt on every run: go's cache makes that cheap, and a file target would
# keep serving the old binary after a bump in tools/go.mod. Not vendored,
# because installing the plugin clones this whole repository.
mdreflow:
	@command -v go >/dev/null || { \
	  echo "mdreflow is built from tools/go.mod and needs Go: https://go.dev/dl" >&2; \
	  exit 2; }
	@go build -C tools -o ../.build/mdreflow github.com/jbeda/mdreflow/cmd/mdreflow

md-reflow: mdreflow
	.build/mdreflow .

md-reflow-check: mdreflow
	@.build/mdreflow --check . || { rc=$$?; \
	  [ $$rc -ne 1 ] || echo "Run 'make md-reflow' and commit the result." >&2; \
	  exit $$rc; }

md-reflow-diff: mdreflow
	.build/mdreflow --diff .

# Not a gate: mdreflow declining a paragraph is a correct outcome.
md-reflow-explain: mdreflow
	.build/mdreflow --explain --check .
