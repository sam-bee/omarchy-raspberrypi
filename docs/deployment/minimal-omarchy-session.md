# Minimal Pi UWSM and Quickshell session

The minimal session is an incremental porting mechanism for Raspberry Pi hardware. It is not a separate desktop product or the final Omarchy user experience. Use it only while validating the package, session, shell and recovery gates needed for the normal upstream desktop.

The reusable procedures are the [package stage](minimal-session/package-stage.md) and [bounded session stage](minimal-session/session-stage.md). They take the target account, home directory, runtime directory and source revision from a private run record. Keep package manifests, machine observations, screenshots and recovery evidence outside this repository.

`install/arm64/stage-user-session.sh` runs as the target user from a clean committed checkout. It archives the committed revision into a versioned user release and publishes managed configuration files without overwriting existing destinations. That archive currently contains the whole tracked tree, so every tracked document must be suitable for deployment until archive scope is narrowed separately.

The prior one-machine draft and its acceptance observations are retained in the project's private deployment evidence directory. Use the current session templates and tests as implementation authority; this overview does not approve a package transaction, boot change or unattended startup.
