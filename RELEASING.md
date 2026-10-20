# Releasing

This checklist is for maintainers. Releases are cut from commits already merged into `main` through reviewed pull requests.

1. **Behavior.** CI is green on the release commit. Run prolonged prepare/renew/release workloads on a dedicated deployment and confirm that capacity is retained whenever cleanup is unconfirmed.
2. **Real-platform contracts.** Run `python -m tests.contract.docker_metadata` (add `--sudo` only if your Docker setup needs it). Then run the contracts for **each** execd protocol, either locally or through the manual `contract` CI job (`workflow_dispatch` with `run_contracts`). Verify foreground and background commands, cancellation including lost IDs and delivery races, binary transfer, task preparation, multi-service networking, real Docker numeric metadata, and cleanup through an independent observer. The full gate list is in [Platform requirements](docs/Platform_Requirements.md#formal-release-gates).
3. **Distributions.** Run `uv build` and `python scripts/check_release.py dist`. Run the tests from the extracted sdist, and install the wheel into separate base and Harbor environments. Importing `flotilla` and running the CLI must not require Harbor, and the wheel must not contain the repository test suite. The public `flotilla.testing` helpers may be packaged, but importing the normal runtime must not import them or require pytest.
4. **Content review.** Scan the release commit, wheel, sdist, and image build contexts for credentials, private domains or registries, personal paths, run logs, and instance IDs. Review findings manually; synthetic example addresses are expected.
5. **Third-party material.** Review [THIRD_PARTY.md](THIRD_PARTY.md). Binary image releases must ship with the corresponding source and license material.
6. **Changelog.** Move the `Unreleased` entries in [CHANGELOG.md](CHANGELOG.md) under the new version and date, through a pull request.
7. **Tag.** Tag the merged commit `vX.Y.Z` (the version is derived from the tag by hatch-vcs), push the tag, and create a GitHub release with the changelog entry and the acceptance evidence. Published tags are never moved; a correction is a new version.

If anything changes after step 2, repeat the affected checks before tagging.
