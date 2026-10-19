# build.sh supplies a verified static BusyBox, applet links, and root identity files.
# The anchor owns shared-storage operations without mounting a share release.
FROM scratch
COPY rootfs/ /
ENV PATH=/bin
ENTRYPOINT ["/bin/sleep", "2147483647"]
