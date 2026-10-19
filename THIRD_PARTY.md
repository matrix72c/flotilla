# Third-party components

The Flotilla wheel contains this project's Python modules and Apache-2.0 license. It does not vendor the Python dependencies or BusyBox. Dependency distributions retain their own notices; exact resolved versions and sources are recorded in `uv.lock`.

| Component | Distribution boundary | Source / version |
|---|---|---|
| HTTPX, Pydantic, PyYAML | Installed dependencies of the base package | PyPI distributions resolved in `uv.lock`; license files remain in their distributions |
| Harbor and its dependencies | Optional `harbor` extra | Harbor 0.24.0 and its locked dependency graph |
| BusyBox static | Anchor image and separately published share | Alpine `busybox-static-1.36.1-r31.apk`; binary SHA256 `6d4ae568988ee24beb9dac4afdac4df67f90bbaed6ca47628da35ff5eb632a4c` |
| Probe image packages | Optional operator-built diagnostic image | Base image digest and installed package inventory must be captured per build |

BusyBox uses GPL version 2. Binary distribution requires the corresponding source, including the applicable configuration and build material; a generic upstream URL is not a substitute for matching the distributed binary. See the [BusyBox license guidance](https://busybox.net/license.html). The full GPLv2 text is included at `third_party/GPL-2.0.txt`.

Before distributing the anchor/share binary, collect the BusyBox 1.36.1 source archive, the Alpine r31 packaging configuration and patches, and applicable static-library source/notices. Record their checksums and the exact aports revision alongside the binary. The recipe is in `images/anchor/build.sh`; Alpine packaging is available in the [official aports repository](https://gitlab.alpinelinux.org/alpine/aports). **This project does not yet publish a verified corresponding-source bundle for the BusyBox binary, so it does not distribute prebuilt anchor images or share releases.** Build them yourself with `images/anchor/build.sh`; if you redistribute the result, you are responsible for providing the corresponding source.

For each released image, retain its immutable image digest, package/version inventory, build context, component license texts, and source bundle. Publish these with the image release. The Python source/wheel release and operator-built images are separate artifacts and must be reviewed separately.
