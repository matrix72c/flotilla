# flotilla 锚点镜像（Architecture §5.6、§7.2；backends/opensandbox.md §3.6）。
#
# 只有一个静态 busybox，放在与单元的 share 相同的路径 /.flotilla/bin/busybox：后端每次执行都经它包一层
# `env -i`（`execd.wrapper`），锚点不挂 share，所以由镜像自带。锚点的目录操作（mkdir、rm、chown、tar、cp、ls）
# 与平台入口包装要的 /bin/sh 都由同一个 busybox 的 applet 提供。不含任何任务代码。
#
# rootfs 由 build.sh 在构建上下文里组装（busybox 取自 Alpine busybox-static，校验哈希；/bin 下是指向它的
# applet 链接），这里只整体复制：FROM scratch 里没有 shell，不能 RUN。
FROM scratch
COPY rootfs/ /
ENV PATH=/bin
ENTRYPOINT ["/bin/sleep", "2147483647"]
