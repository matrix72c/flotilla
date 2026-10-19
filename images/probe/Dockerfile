# Public probe image. Override BASE_IMAGE with a digest-pinned Ubuntu image when needed.
ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends netcat-openbsd iproute2 \
    && rm -rf /var/lib/apt/lists/*
CMD ["sleep", "infinity"]
