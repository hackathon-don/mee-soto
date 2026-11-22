# The pinned test environment and SQLGlot source, shared by the map builder
# and the runtime image so dependencies install once. The digest is the image
# the scorer labeled every commit in.
ARG BASE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
FROM ${BASE} AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install -r /tmp/requirements.lock
RUN python -m compileall -q -j 0 /usr/local/lib/python3.13 >/dev/null || true
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /opt/sg && tar xzf /tmp/sqlglot.tar.gz -C /opt/sg --strip-components=1 && rm /tmp/sqlglot.tar.gz \
    && python -m compileall -q -j 0 /opt/sg \
    && chmod -R a+rX /opt/sg

# Generate the MapSelector file map from the pinned test source. Keep this
# stage independent from runtime files so runtime edits reuse the completed map.
FROM deps AS map-builder
WORKDIR /build
COPY tools/build_file_map.py /build/tools/build_file_map.py
COPY strategies/selectors/map_format.py /build/strategies/selectors/map_format.py
COPY sample/tests.json /build/sample/tests.json
ARG MAP_BUILD_LIMIT=0
RUN python /build/tools/build_file_map.py \
    --catalog /build/sample/tests.json \
    --source-root /opt/sg \
    --output /out/file_map.json.gz \
    --jobs 4 --budget-seconds 360 --timeout-seconds 90 --limit "${MAP_BUILD_LIMIT}"
COPY strategies/build_dialect_entry_map.py strategies/trace_dialect_entry.py /build/strategies/
COPY strategies/selectors/dialect_entry_format.py /build/strategies/selectors/
ARG DIALECT_ENTRY_MAP_BUDGET=360
RUN python -m strategies.build_dialect_entry_map \
    --catalog /build/sample/tests.json \
    --source-root /opt/sg \
    --output /out/dialect_entry_map.json.gz \
    --jobs 4 --budget-seconds "${DIALECT_ENTRY_MAP_BUDGET}" --timeout-seconds 90

# The scorer runs linux/amd64 without network, with a read-only root and
# unprivileged user. ImpactSelector runs tests against /opt/sg, so this stage
# keeps the pinned dependencies and source. The map is copied first so the
# name-index build below waits for the collector instead of competing with it.
FROM deps AS runtime
COPY --from=map-builder /out/file_map.json.gz /app/strategies/selectors/file_map.json.gz
COPY --from=map-builder /out/dialect_entry_map.json.gz /app/strategies/selectors/dialect_entry_map.json.gz
COPY main.py config.py local_cpu.py /app/
COPY strategies/ /app/strategies/
WORKDIR /app
RUN python -m strategies.selectors.impact.static /opt/sg strategies/selectors/impact/maps/index.pkl.gz strategies/selectors/impact/maps/static.pkl.gz \
    && python -m compileall -q /app >/dev/null \
    && chmod -R a+rX /app
# The judge runs the image without flags, so the submitted experiment is set
# here. Local runs without --experiment still use DEFAULT_CONFIG (baseline).
CMD ["python", "/app/main.py", "--experiment", "impact-variable-workers-matched"]
