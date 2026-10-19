# Network validation

Use a dedicated test deployment and isolated launch IDs. Record policy configuration and results without credentials or private response bodies. Protocol setup is documented in [OpenSandbox](backends/opensandbox.md).

Verify a permitted peer connection first, then a denied peer connection. Check both directions and each protocol used by the task. Confirm that internal-only services cannot use external egress, that allowed destinations are reachable, and that disallowed destinations fail. An unavailable server or missing listener is not isolation evidence.

For **inbound isolation**, use an independent client whose outbound policy is explicitly known to allow the target subnet. Confirm that it reaches a comparable unisolated listener, confirm the target listener is healthy from an allowed peer, and then test denial from the independent client. Repeat after changing the intended isolation policy where deployment controls permit. Preserve both positive and negative results.

Do not use a sandbox with `external="any"` as proof of unrestricted outbound access to another sandbox. That policy excludes unlinked sandbox ranges. The built-in probe therefore retains inbound isolation as `declared`; it does not automatically promote that declaration to `probed`.

The standalone diagnostic script can help inspect a dedicated fixture:

```sh
python scripts/check_network.py --help
```

Review its selected endpoints and resource ownership before execution. A diagnostic is not a substitute for acceptance controls. Independently list the run's labeled instances and inspect its owned storage directories after cleanup. A timeout, partial result, or unavailable observer leaves the acceptance gate incomplete.
