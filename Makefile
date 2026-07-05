# pr-sentinel — developer tasks.
# `make check` mirrors CI: shellcheck the watcher, then run the test suite.

.PHONY: check shellcheck test

check: shellcheck test

shellcheck:
	shellcheck scripts/pr-sentinel-watch.sh

test:
	python3 -m unittest discover tests
