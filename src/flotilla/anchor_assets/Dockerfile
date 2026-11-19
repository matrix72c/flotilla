# The build context supplies a hash-verified static BusyBox in rootfs/; the anchor runs no
# interpreter. Modes and symlinks come from the context: scratch has no shell to fix them up.
FROM scratch
COPY rootfs/ /
ENV PATH=/usr/sbin:/usr/bin:/sbin:/bin
ENTRYPOINT ["/bin/sleep", "2147483647"]
