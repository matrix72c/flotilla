# The caller must supply an immutable Python 3.12 Linux/amd64 base reference.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
USER 0:0
RUN python3 -I -c 'import sys; assert sys.version_info[:2] == (3, 12)' \
    && python3 -I -m venv --without-pip --copies /opt/flotilla
COPY site-packages/ /opt/flotilla/lib/python3.12/site-packages/
COPY busybox /.flotilla/bin/busybox
COPY snapshot.json verify.py /opt/flotilla/
COPY GPL-2.0.txt /.flotilla/licenses/busybox-GPL-2.0.txt
# Execd intentionally drops ambient image environment; its controlled PATH must
# still find the installed runtime, not a base-image or host Python environment.
# A direct symlink outside the venv loses pyvenv.cfg discovery under Python 3.12.
# Exec the venv interpreter instead, including when execd supplies its own PATH.
RUN chmod 0755 /.flotilla/bin/busybox \
    && printf '#!/bin/sh\nexec /opt/flotilla/bin/python "$@"\n' > /opt/flotilla/python-entrypoint \
    && chmod 0755 /opt/flotilla/python-entrypoint \
    && ln -sf /opt/flotilla/python-entrypoint /usr/bin/python3 \
    && ln -sf /opt/flotilla/python-entrypoint /usr/local/bin/python3 \
    && mkdir -p /tmp \
    && chmod 1777 /tmp
ENV PATH="/opt/flotilla/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
WORKDIR /
ENTRYPOINT ["/.flotilla/bin/busybox", "sleep", "2147483647"]
