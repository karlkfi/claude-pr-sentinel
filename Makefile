# pr-sentinel — developer tasks.
# `make check` mirrors CI: shellcheck the scripts, run the test suite, lint the
# backlog store.

.PHONY: check shellcheck test backlog

check: shellcheck test backlog

shellcheck:
	shellcheck scripts/pr-sentinel-watch.sh scripts/alloc-queue-id.sh

test:
	python3 -m unittest discover tests

backlog:
	python3 scripts/queue.py lint
