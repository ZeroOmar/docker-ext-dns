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
ENTRYPOINT ["docker-ext-dns"]
