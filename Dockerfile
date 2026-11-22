# The pinned test environment and SQLGlot source. The digest is the image
# the scorer labeled every commit in.
ARG BASE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
FROM ${BASE} AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install -r /tmp/requirements.lock
ARG FREE_CPU_BYTECODE=1
RUN if [ "${FREE_CPU_BYTECODE}" = "1" ]; then \
        python -m compileall -q -f -j 0 --invalidation-mode unchecked-hash /usr/local/lib/python3.13; \
    else \
        python -m compileall -q -j 0 /usr/local/lib/python3.13 >/dev/null || true; \
    fi
ARG SQLGLOT_REV=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
ADD https://github.com/tobymao/sqlglot/archive/${SQLGLOT_REV}.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /opt/sg && tar xzf /tmp/sqlglot.tar.gz -C /opt/sg --strip-components=1 && rm /tmp/sqlglot.tar.gz \
    && printf '%s\n' "$SQLGLOT_REV" > /opt/sg/.blast-radius-sqlglot-rev \
    && if [ "${FREE_CPU_BYTECODE}" = "1" ]; then \
        python -m compileall -q -j 0 --invalidation-mode checked-hash /opt/sg; \
    else \
        python -m compileall -q -j 0 /opt/sg; \
    fi \
    && chmod -R a+rX /opt/sg

# The scorer runs linux/amd64 without network, with a read-only root and
# unprivileged user. ImpactSelector runs tests against /opt/sg, so this stage
# keeps the pinned dependencies and source.
FROM deps AS runtime
COPY main.py config.py local_cpu.py /app/
COPY strategies/ /app/strategies/
WORKDIR /app
RUN python -m strategies.selectors.impact.static /opt/sg strategies/selectors/impact/maps/index.pkl.gz strategies/selectors/impact/maps/static.pkl.gz \
    && python -m compileall -q /app >/dev/null \
    && chmod -R a+rX /app
ARG FREE_CPU_MAPS=1
RUN if [ "${FREE_CPU_MAPS}" = "1" ]; then \
        python -c 'from gzip import decompress; from pathlib import Path; paths = [*Path("strategies/selectors/impact/maps").glob("*.pkl.gz"), Path("strategies/selectors/impact_tables.json.gz")]; [p.with_suffix("").write_bytes(decompress(p.read_bytes())) for p in paths]'; \
    fi
# The judge runs the image without flags, so the submitted experiment is set
# here. Local runs without --experiment still use DEFAULT_CONFIG (baseline).
CMD ["python", "/app/main.py", "--experiment", "impact-first-failure-tables-free-cpu-outcomes"]
