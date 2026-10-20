# Downstream orchestration contracts

Flotilla owns environment resources. A Harbor trial, evaluation runner or RL
framework owns phase order, verifier selection, scores and training admission.
Calling `inspect(task)` does not inspect or execute a verifier pipeline.

For each effective environment selected by the caller, use
`fleet.inspect_environment(definition)`. It consumes the same native
`EnvironmentDefinition` as `open_environment`; it does not create instances.
An inspection reports findings and resource requirements for that definition.
`inspection.reservation` provides the runtime's reservation copy count and total
instances. `fleet.client_reservation()` provides the client anchor overhead.
Consumers should use these values when budgeting multiple workers and phases.
These are local upper-bound calculations, not remote quota or schedulability checks.

`process.wait(timeout=...)` polls for completion. A wait deadline or cancellation
stops waiting and leaves the process owned by its environment.
`process.stop()` requests confirmed termination using the platform's supported
execution cancellation; an already-exited process is confirmed without side
effects. A vanished record or an unconfirmable stop invalidates the environment:
`stop()` releases it and raises `ExecOutcomeUnknown`, like foreground
cancellation. A return without error means the process is confirmed stopped;
call `environment.check()` before continuing dependent work.

`Environment.release()` and `Flotilla.close()` retain explicit cleanup results.
Downstream code must check `CleanupReport.clean`; a verifier score does not make
uncertain or failed cleanup successful.

For local, neutral contract tests, install the `testing` extra and import helpers
from `flotilla.testing`. This is an explicit testing surface, not a production
backend or isolation boundary. Downstream tests no longer import Flotilla's
private `tests` package or reach into the live manager directly. Testing helpers
may inject platform faults or alter fake deletion retry windows.
