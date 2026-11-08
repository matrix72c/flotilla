# Trusted platform-side Harbor compiler with Docker Engine for an isolated Kata builder.
# All three base references must be immutable repo@sha256 references.
ARG PYTHON_IMAGE
ARG DOCKER_ENGINE_IMAGE
ARG ANCHOR_IMAGE
FROM ${DOCKER_ENGINE_IMAGE} AS docker_engine
FROM ${ANCHOR_IMAGE} AS anchor
FROM ${PYTHON_IMAGE}

ARG FLOTILLA_VERSION
ARG UV_VERSION=0.13.6
# The official DinD distribution contains static Engine/container runtime binaries.
COPY --from=docker_engine /usr/local/bin/docker /usr/local/bin/dockerd /usr/local/bin/containerd /usr/local/bin/containerd-shim-runc-v2 /usr/local/bin/ctr /usr/local/bin/runc /usr/local/bin/docker-init /usr/local/bin/docker-proxy /usr/local/bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends iptables iproute2 e2fsprogs kmod xz-utils mount procps \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker_engine /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/lib/docker/cli-plugins/docker-buildx
COPY --from=anchor /.flotilla/bin/busybox /.flotilla/bin/busybox
COPY docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
RUN echo '7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4  /usr/local/lib/docker/cli-plugins/docker-compose' | sha256sum -c - \
    && chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose \
    && python -m pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /opt/flotilla
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
RUN SETUPTOOLS_SCM_PRETEND_VERSION="${FLOTILLA_VERSION}" \
    uv sync --frozen --no-dev --extra harbor --no-editable \
    && uv cache clean \
    && printf '#!/bin/sh\nexec /opt/flotilla/.venv/bin/python "$@"\n' > /usr/bin/python3 \
    && chmod 0755 /usr/bin/python3 \
    && ln -s /usr/local/bin/docker /usr/bin/docker
ENV PATH="/opt/flotilla/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Execd intentionally discards ambient image environment. Test its controlled PATH.
RUN env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin python3 -I -c 'import flotilla, harbor; import flotilla.integrations.workspace' \
    && env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin docker compose version --short | grep -Fx '2.39.4' \
    && env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin docker buildx version \
    && dockerd --version && containerd --version && runc --version \
    && /.flotilla/bin/busybox stat /opt/flotilla
WORKDIR /
ENTRYPOINT ["/.flotilla/bin/busybox", "sleep", "2147483647"]
