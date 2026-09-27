# AI agent provisioning on Raspberry Pi

Mise-backed agent launchers are part of the intended upstream desktop experience. The Pi profile uses Omarchy's existing `install/user/mise-work.sh` and `install/user/mise.sh` leaves rather than maintaining a separate list of agents or installing a preferred harness by hand. A fresh user remains free to choose their default agent.

## Provisioning

Run these as the desktop user, with `OMARCHY_PATH` pointing at the staged release through `~/.local/share/omarchy-pi/current`:

```bash
export OMARCHY_PATH="$HOME/.local/share/omarchy-pi/current"
bash "$OMARCHY_PATH/install/arm64/setup-mise.sh"
bash "$OMARCHY_PATH/install/arm64/setup-user-agents.sh"
```

The first command installs mise into `~/.local/bin` from the official Linux ARM64 release, checking a SHA-256 pinned alongside the release version. It retains an existing executable. ALARM currently has no mise package in the checked repositories, so the upstream x86 `mise-bin` package remains deferred in the package table; that does not defer the AI experience. Update the bootstrap version and digest together when changing the selected release.

The second command provisions Node through mise, the regular lazy launchers, and Omarchy's skills. It integrates the user shell without running Omarchy's full system or first-user provisioning. The Pi UWSM environment includes the mise shims and user bin directory. The upstream `Super + Shift + Ctrl + A` shortcut opens the agent picker until a default is selected.

Choose an agent with `omarchy default agent <name>` or its graphical picker. The initial install requires internet access; using a provider requires its normal authentication. Omarchy's own launcher approval modes are retained. No provider account or default is supplied by provisioning.

## Updates and acceptance

`mise self-update` updates this standalone mise installation. `omarchy-update-mise` (or `MISE_MINIMUM_RELEASE_AGE=0 mise up`) updates its managed tools. The Pi's general full-system Omarchy updater remains outside the accepted ARM update workflow.

Verify that a fresh Bash login finds mise and the stubs; run a selected agent's `--version` through its stub to exercise a real first-use install. Verify the picker in the running desktop and confirm that existing SSH and desktop services remain available. An installed CLI and working picker do not prove provider authentication, an agent task, or local-model performance. Usage-panel and crash-notification integration have separate acceptance requirements.
