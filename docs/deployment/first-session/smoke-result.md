# First-session acceptance record

The machine-specific smoke result, package manifest and session configuration are retained in the private deployment evidence for the run. This public file describes the checks a new run must record without publishing account names, home paths, network addresses, boot identifiers, device UUIDs, process IDs or recovery details.

Record privately:

- source revision and target architecture;
- exact package transaction and install reasons;
- protected-state and recovery checks before and after the probe;
- the local session, compositor instance, output and client identities;
- compositor configuration errors and renderer information;
- whether a real frame was captured;
- cleanup state and any unexpected difference.

The result passes only when the named local session and compositor/client checks pass, cleanup restores the original VT and protected-state comparisons pass. A compositor/client start without a captured frame is not visual acceptance.
