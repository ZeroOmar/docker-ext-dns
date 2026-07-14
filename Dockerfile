FROM python:3.11-slim AS builder
WORKDIR /build
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir .

FROM python:3.11-slim
RUN useradd -r -u 1001 -g root extdns
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/docker-ext-dns /usr/local/bin/docker-ext-dns
COPY src/ext_dns/web/static/ /app/static/
USER extdns
ENV EXT_DNS_CONFIG=""
EXPOSE 8080

# Reports the container healthy while the app itself is answering (HTTP 200 from
# /api/health). Provider/Docker-socket problems are surfaced in the response body
# (healthy=false, per-component detail) but do NOT flip the container unhealthy —
# those are external dependencies, not a reason to restart the app. Override the
# port with EXT_DNS_HEALTHCHECK_PORT if the web port is not the default 8080.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; port=os.environ.get('EXT_DNS_HEALTHCHECK_PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=8).status==200 else 1)"

ENTRYPOINT ["docker-ext-dns"]
