SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

IMAGE ?= blast-radius-entry
DEV_IMAGE ?= $(IMAGE)-dev
NATIVE_PLATFORM = linux/$(shell docker version -f '{{.Server.Arch}}')
SAMPLE := $(CURDIR)/sample
OUT := $(CURDIR)/out
# Selections replayed by make schedule: saved (from make select) or ideal
# (the sample's known failures, read on the host only).
SOURCE ?= saved

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build e2e e2e-dev e2e-amd64 sample check select schedule test

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
e2e: build
	@mkdir -p $(OUT)
	docker run --platform linux/amd64 $(RUN_FLAGS) $(IMAGE) 2>&1 > $(OUT)/e2e.jsonl | tee $(OUT)/e2e.log >&2
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUT)/e2e.jsonl

# Builds and scores a native-arch image under the same scorer limits.
e2e-dev:
	docker build --platform $(NATIVE_PLATFORM) --build-arg BASE=python:3.13-slim --tag $(DEV_IMAGE) .
	@mkdir -p $(OUT)
	docker run --platform $(NATIVE_PLATFORM) $(RUN_FLAGS) $(DEV_IMAGE) 2>&1 > $(OUT)/e2e-dev.jsonl | tee $(OUT)/e2e-dev.log >&2
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUT)/e2e-dev.jsonl

sample: e2e

# Explicit alias for the judge-compatible amd64 run.
e2e-amd64: e2e

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make e2e counts the misses.
check: build
	@mkdir -p $(OUT)
	docker run --platform linux/amd64 $(RUN_FLAGS) $(IMAGE) 2>&1 > $(OUT)/e2e.jsonl | tee $(OUT)/check.log >&2
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUT)/e2e.jsonl > /dev/null && echo "format ok"

# Runs only the selector, in the image under the scorer's limits, and saves
# its picks for make schedule.
select: build
	@mkdir -p $(OUT)
	docker run --platform linux/amd64 $(RUN_FLAGS) $(IMAGE) python /app/main.py select 2>&1 > $(OUT)/picks.jsonl.tmp | tee $(OUT)/select.log >&2
	mv $(OUT)/picks.jsonl.tmp $(OUT)/picks.jsonl

# Runs only the scheduler, on the host, over fixed selections, and scores it.
schedule:
	@mkdir -p $(OUT)
	@case "$(SOURCE)" in \
		saved) set -- --picks $(OUT)/picks.jsonl ;; \
		ideal) set -- --ideal $(SAMPLE)/answers.json ;; \
		*) echo "SOURCE must be saved or ideal" >&2; exit 2 ;; \
	esac; \
	python3 main.py --data $(SAMPLE) schedule "$$@" > $(OUT)/schedule-$(SOURCE).jsonl.tmp
	mv $(OUT)/schedule-$(SOURCE).jsonl.tmp $(OUT)/schedule-$(SOURCE).jsonl
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUT)/schedule-$(SOURCE).jsonl

test:
	python3 -m unittest discover -s tests
