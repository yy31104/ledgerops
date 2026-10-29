# Runtime/build inputs are immutable registry digests and hash-locked wheels.
FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS wheel
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements-build.txt /locks/build.txt
RUN python -m pip install --require-hashes --no-deps -r /locks/build.txt
COPY source/ /build/
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels /build

FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
ARG SOURCE_REVISION
ARG SOURCE_DIRTY
LABEL org.opencontainers.image.title="LedgerOps synthetic local service" \
      org.opencontainers.image.revision=$SOURCE_REVISION \
      io.ledgerops.source_dirty=$SOURCE_DIRTY
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/opt
COPY requirements-runtime.txt /locks/runtime.txt
RUN python -m pip install --require-hashes --no-deps -r /locks/runtime.txt
COPY --from=wheel /wheels/ /wheels/
RUN python -m pip install --no-deps /wheels/*.whl && python -m pip check && rm -rf /wheels \
    && groupadd --gid 10001 ledgerops \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent ledgerops
COPY provider_sim/ /opt/provider_sim/
COPY scripts/ /opt/ledgerops-ops/
COPY source-provenance.json /opt/ledgerops-provenance.json
# The PG administration target needs this optional Python extension's system lib.
RUN mkdir /opt/python-compat && cp /usr/lib/*-linux-gnu/libsqlite3.so.0 /opt/python-compat/
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "/opt/ledgerops-ops/launcher.py"]
CMD ["service"]

FROM postgres@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722 AS administrator
ARG SOURCE_REVISION
ARG SOURCE_DIRTY
LABEL org.opencontainers.image.revision=$SOURCE_REVISION io.ledgerops.source_dirty=$SOURCE_DIRTY
COPY --from=runtime /usr/local/ /usr/local/
COPY --from=runtime /opt/ /opt/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt \
    LD_LIBRARY_PATH=/usr/local/lib:/opt/python-compat
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "/opt/ledgerops-ops/launcher.py"]
CMD ["backup"]

FROM caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f AS proxy
ARG SOURCE_REVISION
ARG SOURCE_DIRTY
LABEL org.opencontainers.image.revision=$SOURCE_REVISION io.ledgerops.source_dirty=$SOURCE_DIRTY
# Official Caddy has a file capability for port443. This stack uses unprivileged
# ports8443/9443, so remove it rather than retain a runtime capability.
RUN setcap -r /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
USER 10001:10001
ENV HOME=/tmp XDG_DATA_HOME=/data XDG_CONFIG_HOME=/tmp
