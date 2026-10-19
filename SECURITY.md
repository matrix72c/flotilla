# Security policy

## Reporting a vulnerability

Please do not report security vulnerabilities through public issues, discussions, or pull requests.

Report them privately through GitHub's [private vulnerability reporting](https://github.com/matrix72c/flotilla/security/advisories/new), or by email to **matrix72c@gmail.com**. Include:

- the affected version or commit,
- a description of the issue and its impact,
- a minimal reproduction, preferably with synthetic tasks and configuration.

Do not include real credentials, private endpoints, task contents, or instance identifiers. You should receive an acknowledgement within 7 days. We will keep you informed while a fix is prepared and credit you in the release notes unless you prefer otherwise.

## Supported versions

Flotilla is pre-1.0. Security fixes are made on the default branch and included in the next release; older releases are not patched.

## Scope

Flotilla relies on the sandbox platform for isolation. Capability declarations in `declared.json` are operator assertions, not certifications, and passing unit tests does not establish isolation on a real deployment. Security gaps accepted through `security.accept_insecure` remain visible in environment metadata. Reports about Flotilla weakening or misreporting the platform's isolation are in scope; vulnerabilities in OpenSandbox itself should be reported to that project.
