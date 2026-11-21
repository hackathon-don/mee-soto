# Generate the selector map from the pinned test source. Keep this stage
# independent from runtime files so runtime edits reuse the completed map.
ARG BASE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
FROM ${BASE} AS map-builder
WORKDIR /build
COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir -r /tmp/requirements.lock
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN python -c 'import tarfile; tarfile.open("/tmp/sqlglot.tar.gz", "r:gz").extractall("/opt/sqlglot", filter="data")'
RUN python -m compileall -q /opt/sqlglot
COPY tools/build_file_map.py /build/tools/build_file_map.py
COPY strategies/selectors/map_format.py /build/strategies/selectors/map_format.py
COPY sample/tests.json /build/sample/tests.json
ARG MAP_BUILD_LIMIT=0
RUN python /build/tools/build_file_map.py \
    --catalog /build/sample/tests.json \
    --source-root /opt/sqlglot/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    --output /out/file_map.json.gz \
    --jobs 4 --budget-seconds 360 --timeout-seconds 90 --limit "${MAP_BUILD_LIMIT}"

# The scorer runs linux/amd64 without network, with a read-only root and
# unprivileged user. Keep this runtime stage small and free of test tooling.
FROM ${BASE} AS runtime
COPY main.py config.py /app/
COPY strategies/ /app/strategies/
COPY --from=map-builder /out/file_map.json.gz /app/strategies/selectors/file_map.json.gz
CMD ["python", "/app/main.py"]
