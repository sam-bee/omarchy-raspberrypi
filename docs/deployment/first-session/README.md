# First graphical session on a Raspberry Pi

This is a bounded smoke test for a Pi installation. It checks that the packaged Hyprland compositor can start a local logind session, create an output when the test environment has no connected display, and map a Foot client. It is not a complete Omarchy desktop deployment and it does not test reboot, automatic login, remote desktop or package upgrades.

Run the procedure against a disposable or explicitly designated Pi installation. Define the target account, home directory, numeric IDs, runtime directory and SSH endpoint in the operator's private run record. Do not put those values, credentials, recovery archives or machine evidence in this repository.

## Gates

1. Keep an independent recovery connection and a verified, encrypted backup of the target's current state. Confirm the account, mounts, boot configuration, network, SSH and service state before changing anything.
2. Resolve the complete package closure in an isolated package database. Record the exact package names, versions, repositories, architectures, archive hashes, signatures and install reasons in the private run record. Do not use a stale manifest as installation authority.
3. Review package archive paths, install scripts, hooks, service files and removal effects. Reject changes to the boot, encryption, firmware, kernel, storage, network, SSH or firewall substrate unless a separate task explicitly covers them.
4. Copy only the temporary session configuration required by this test. Refuse existing destinations and symlinks; never overwrite a user's configuration.
5. Confirm that the chosen local VT and logind seat are unused, the test service does not already exist, and the recovery connection remains available.

The package review must also check that dependencies do not silently enable services or add an existing user to a new group. A package-owned unit being installed does not authorize enabling or starting it.

## Bounded session

Run Hyprland through a transient service with an explicit user, PAM session, controlling VT and runtime limit. Use a namespaced configuration directory and a temporary headless output only when the test has no connected display. Capture the compositor log, output list, client list and configuration errors in the private run record. Report compositor and client startup separately from visual correctness unless a real frame was captured.

After the probe, stop only the named service and the recorded compositor/client processes, terminate only the matching test PAM session, restore the original VT and repeat the protected-state checks. Do not terminate the whole user manager or all processes for the account.

The reusable command sequence is in [commands.md](commands.md). Keep package manifests, archive reviews, session configuration and acceptance results in the private evidence tree for the particular machine. The runtime session templates used by the Pi deployment live under `install/arm64/session/`.

## Recovery boundary

If a preservation check fails, stop and retain the evidence. Do not reboot, rebuild the initramfs, restore a whole system archive over a live system, rewrite a recovery device or alter boot order as part of this smoke test. Use the separately reviewed physical or rescue procedure when remote access is unavailable.
