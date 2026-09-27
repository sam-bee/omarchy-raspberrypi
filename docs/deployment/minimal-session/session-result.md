# Minimal-session acceptance record

The machine-specific session attempts, captures, logs and process identifiers are retained in the private deployment evidence for the run. This public record defines the acceptance boundary without publishing personal or host-specific values.

Record privately:

- source revision and package closure;
- target user and session identity;
- UWSM/compositor unit and process identity;
- staged release and user configuration hashes;
- output, shell, terminal and frame checks;
- cleanup, protected-state and fresh-SSH checks.

Pass only when one intended local session starts, the compositor and minimal shell use the reviewed release, the terminal client maps, no unexpected unit or application starts, and cleanup restores the original state. Reboot and persistent startup require the separate runbook in this directory.
