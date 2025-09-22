# syntax=docker/dockerfile:1
FROM alpine:3.20

# Install Python3, pip, build deps, CA certs, and utilities
RUN apk add --no-cache \
    python3 \
    py3-pip \
    python3-dev \
    libev-dev \
    git \
    curl \
    bash \
    build-base \
    ca-certificates && \
    update-ca-certificates

# Install Go 1.23.x (official tarball), architecture-aware
ARG TARGETARCH
ENV GO_VERSION=1.23.1
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) GOARCH=amd64 ;; \
      arm64) GOARCH=arm64 ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    curl -fsSL https://go.dev/dl/go${GO_VERSION}.linux-${GOARCH}.tar.gz -o /tmp/go.tgz; \
    tar -C /usr/local -xzf /tmp/go.tgz; \
    rm -f /tmp/go.tgz

# Create a default workspace
RUN mkdir -p /workspace

WORKDIR /workspace

# Environment: ensure python and Go are in PATH
ENV PATH="/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
# Prefer libev reactor for cassandra-driver under Python 3.12+
ENV CASSANDRA_DRIVER_REACTOR=libev
# Ensure Go can fetch modules reliably and allow toolchain auto-upgrade if needed
ENV GOPROXY=https://proxy.golang.org,direct
ENV GOTOOLCHAIN=auto
# Disable CGO to avoid gcc toolchain issues on arm64 when not needed
ENV CGO_ENABLED=0

# Default command keeps the container alive for interactive use
CMD ["/bin/sh", "-c", "sleep infinity"]
