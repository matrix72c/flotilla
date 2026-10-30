"""Loadtest driver for flotilla-vs-alternative comparison runs (repo-internal tooling).

Lives outside the published package; entrypoints take deployment/secret/task paths via
args or environment (FLOTILLA_DEPLOYMENT, OPENSANDBOX_SECRET_FILE, OPENSANDBOX_ENDPOINT,
OPENSANDBOX_PROJECT), so no deployment-specific values are committed here.
"""
