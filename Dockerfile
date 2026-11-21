# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock
RUN python -m compileall -q -j 0 /usr/local/lib/python3.13 >/dev/null || true

ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sg.tar.gz
RUN mkdir -p /opt/sg && tar xzf /tmp/sg.tar.gz -C /opt/sg --strip-components=1 && rm /tmp/sg.tar.gz \
    && (python -m compileall -q -j 0 /opt/sg/sqlglot /opt/sg/tests >/dev/null || true) \
    && chmod -R a+rX /opt/sg

COPY main.py config.py /app/
COPY strategies/ /app/strategies/
WORKDIR /app
RUN python -m strategies.selectors.impact.static /opt/sg strategies/selectors/impact/maps/index.pkl.gz strategies/selectors/impact/maps/static.pkl.gz \
    && python -m compileall -q /app/strategies >/dev/null \
    && chmod -R a+rX /app
CMD ["python", "/app/main.py"]
