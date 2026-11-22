SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

IMAGE ?= blast-radius-entry
EXPERIMENT ?= baseline
CPU_DIAGNOSTICS ?= 0
DECISION_LOG ?= 0
DIAGNOSTIC_FLAGS = $(if $(filter 1,$(CPU_DIAGNOSTICS)),--env BR_CPU_DIAGNOSTICS=1) $(if $(filter 1,$(DECISION_LOG)),--env BR_DECISION_LOG=1)
DEV_IMAGE ?= $(IMAGE)-dev
NATIVE_PLATFORM = linux/$(shell docker version -f '{{.Server.Arch}}')
SAMPLE := $(CURDIR)/sample
OUT := $(CURDIR)/out
# Selections replayed by make schedule: saved (from make select) or ideal
# (the sample's known failures, read on the host only).
SOURCE ?= saved
# Held-out patch set (see WORKSPACE.md): generated from the pinned commit,
# labeled by running the full catalog, never mounted with its labels.
HOLDOUT := $(OUT)/holdout
HOLDOUT_COUNT ?= 150
HOLDOUT_BATCH ?= 30
HOLDOUT_JOBS ?= 6
# DEFERRAL=off scores the experiment with ImpactSelector's heavy-test deferral disabled.
DEFERRAL ?= on
HOLDOUT_SPEC = $(EXPERIMENT)$(if $(filter off,$(DEFERRAL)),:off)

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build build-dev e2e e2e-dev e2e-amd64 e2e-holdout e2e-full holdout check select schedule test sample

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed. NO_BUILD=1 reuses an already loaded $(IMAGE).
e2e: $(if $(NO_BUILD),,build)
	@mkdir -p $(OUT)
	docker run --platform linux/amd64 $(RUN_FLAGS) $(DIAGNOSTIC_FLAGS) $(IMAGE) python /app/local_cpu.py python /app/main.py --experiment $(EXPERIMENT) 2>&1 > $(OUT)/e2e.jsonl | tee $(OUT)/e2e.log >&2
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUT)/e2e.jsonl --cpu-log $(OUT)/e2e.log

build-dev:
	docker build --platform $(NATIVE_PLATFORM) --build-arg BASE=python:3.13-slim --tag $(DEV_IMAGE) .

# Builds and scores a native-arch image under the same scorer limits.
e2e-dev: build-dev
	@mkdir -p $(OUT)
	docker run --platform $(NATIVE_PLATFORM) $(RUN_FLAGS) $(DEV_IMAGE) python /app/local_cpu.py python /app/main.py --experiment $(EXPERIMENT) 2>&1 > $(OUT)/e2e-dev.jsonl | tee $(OUT)/e2e-dev.log >&2
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUT)/e2e-dev.jsonl --cpu-log $(OUT)/e2e-dev.log

# Explicit alias for the judge-compatible amd64 run.
e2e-amd64: e2e

# Generates the held-out set once and labels it by running the full catalog
# under pytest in the dev image. Takes hours; rerun to resume labeling.
holdout: $(if $(NO_BUILD),,build-dev)
	@mkdir -p $(HOLDOUT)
	@if [ ! -f $(HOLDOUT)/kinds.json ]; then \
		docker run --rm --network none -v "$(CURDIR)/tools:/tools:ro" -v "$(HOLDOUT):/holdout" $(DEV_IMAGE) \
			python /tools/holdout_mutants.py /opt/sg /holdout $(HOLDOUT_COUNT); \
	fi
	docker run --rm --network none --memory 7g -v "$(CURDIR)/tools/holdout_label:/label:ro" \
		-v "$(HOLDOUT):/holdout" -v "$(SAMPLE)/tests.json:/data/tests.json:ro" $(DEV_IMAGE) \
		python /label/label.py /holdout /data/tests.json $(HOLDOUT_JOBS)

# Scores EXPERIMENT on the labeled held-out set in batches of HOLDOUT_BATCH
# (native dev image, scorer's run flags). Needs make holdout first.
e2e-holdout: $(if $(NO_BUILD),,build-dev)
	DEV_IMAGE=$(DEV_IMAGE) python3 tools/holdout_eval.py $(HOLDOUT) $(HOLDOUT_BATCH) $(HOLDOUT_SPEC)

# The sample plus the held-out set on the same dev image, in one report.
e2e-full: $(if $(NO_BUILD),,build-dev)
	DEV_IMAGE=$(DEV_IMAGE) python3 tools/holdout_eval.py $(HOLDOUT) $(HOLDOUT_BATCH) $(HOLDOUT_SPEC) --sample $(SAMPLE)

# Removed target; fails so it cannot be mistaken for a passing run.
sample:
	@echo 'make sample was removed; use make e2e' >&2; exit 1

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make e2e counts the misses.
check: build
	@mkdir -p $(OUT)
	docker run --platform linux/amd64 $(RUN_FLAGS) $(IMAGE) python /app/main.py --experiment $(EXPERIMENT) 2>&1 > $(OUT)/e2e.jsonl | tee $(OUT)/check.log >&2
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUT)/e2e.jsonl > /dev/null && echo "format ok"

# Runs only the selector, in the image under the scorer's limits, and saves
# its picks for make schedule.
select: build
	@mkdir -p $(OUT)
	docker run --platform linux/amd64 $(RUN_FLAGS) $(IMAGE) python /app/main.py --experiment $(EXPERIMENT) select 2>&1 > $(OUT)/picks.jsonl.tmp | tee $(OUT)/select.log >&2
	mv $(OUT)/picks.jsonl.tmp $(OUT)/picks.jsonl

# Runs only the scheduler, on the host, over fixed selections, and scores it.
schedule:
	@mkdir -p $(OUT)
	@case "$(SOURCE)" in \
		saved) set -- --picks $(OUT)/picks.jsonl ;; \
		ideal) set -- --ideal $(SAMPLE)/answers.json ;; \
		*) echo "SOURCE must be saved or ideal" >&2; exit 2 ;; \
	esac; \
	python3 main.py --experiment $(EXPERIMENT) --data $(SAMPLE) schedule "$$@" > $(OUT)/schedule-$(SOURCE).jsonl.tmp
	mv $(OUT)/schedule-$(SOURCE).jsonl.tmp $(OUT)/schedule-$(SOURCE).jsonl
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUT)/schedule-$(SOURCE).jsonl

test:
	python3 -m unittest discover -s tests
