#!/bin/bash

set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/base-test.sh"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT

test_home="$test_tmp/home"
stub_bin="$test_tmp/bin"
release="$test_tmp/release"
mkdir -p "$test_home/.local/share/omarchy-pi/releases" "$stub_bin" "$release/bin"
ln -s "$ROOT/install" "$release/install"
ln -s "$ROOT/default" "$release/default"
ln -s "$release" "$test_home/.local/share/omarchy-pi/current"

log="$test_tmp/mise.log"
cat >"$stub_bin/mise" <<'SH'
#!/bin/bash
printf 'mise:%s\n' "$*" >>"$OMARCHY_AGENT_TEST_LOG"
SH
cat >"$stub_bin/omarchy-mise-install" <<'SH'
#!/bin/bash
printf 'install:%s\n' "$*" >>"$OMARCHY_AGENT_TEST_LOG"
SH
cat >"$stub_bin/omarchy-cmd-missing" <<'SH'
#!/bin/bash
exit 0
SH
cat >"$stub_bin/omarchy-install-hermes-cli" <<'SH'
#!/bin/bash
printf 'hermes:%s\n' "$*" >>"$OMARCHY_AGENT_TEST_LOG"
SH
chmod +x "$stub_bin"/*
for command in omarchy-mise-install omarchy-cmd-missing omarchy-install-hermes-cli; do
  cp -- "$stub_bin/$command" "$release/bin/$command"
done

# Foreign agent skill paths are user-owned and must survive the setup.
mkdir -p "$test_home/.agents/skills/omarchy" "$test_home/.claude/skills" \
  "$test_home/.hermes/profiles/james/skills"
printf 'foreign directory\n' >"$test_home/.agents/skills/omarchy/README"
ln -s /opt/user-managed-diagnose-crash "$test_home/.claude/skills/diagnose-crash"

printf 'custom bashrc\n' >"$test_home/.bashrc"
printf '[[ -f ~/.bashrc ]] && . ~/.bashrc\ncustom profile\n' >"$test_home/.bash_profile"

run_setup() {
  HOME="$test_home" \
    OMARCHY_PATH="$test_home/.local/share/omarchy-pi/current" \
    OMARCHY_AGENT_TEST_LOG="$log" \
    PATH="$stub_bin:/usr/bin:/bin" \
    "$ROOT/install/arm64/setup-user-agents.sh" >/dev/null
}

run_setup

grep -Fxq 'mise:use -g node@latest' "$log" || fail "setup reuses the upstream Mise work leaf"
grep -Fxq 'mise:settings set upgrade.auto_prune false' "$log" || fail "setup reuses the upstream Mise agent leaf"
grep -Fq 'install:codex' "$log" || fail "setup installs the upstream agent wrappers"

for root in .agents/skills .codex/skills .pi/agent/skills .gemini/config/skills .hermes/skills; do
  link="$test_home/$root/diagnose-crash"
  [[ -L $link && $(readlink "$link") == "$test_home/.local/share/omarchy-pi/current/default/agents/skills/diagnose-crash" ]] ||
    fail "setup links diagnose-crash into $root"
done
profile_link="$test_home/.hermes/profiles/james/skills/omarchy"
[[ -L $profile_link && $(readlink "$profile_link") == "$test_home/.local/share/omarchy-pi/current/default/agents/skills/omarchy" ]] ||
  fail "setup links skills into an existing Hermes profile"
[[ -f $test_home/.agents/skills/omarchy/README && ! -L $test_home/.agents/skills/omarchy ]] ||
  fail "setup preserves a foreign skill directory"
[[ -L $test_home/.claude/skills/diagnose-crash && $(readlink "$test_home/.claude/skills/diagnose-crash") == /opt/user-managed-diagnose-crash ]] ||
  fail "setup preserves a foreign skill link"

fragment="$test_home/.config/omarchy/pi-agent-shell.sh"
[[ -f $fragment ]] || fail "setup writes the managed shell fragment"
grep -Fxq 'export OMARCHY_PATH="$HOME/.local/share/omarchy-pi/current"' "$fragment" ||
  fail "shell fragment uses the stable Pi current path"
grep -Fxq "  alias cy='codex --approve-for-me'" "$fragment" ||
  fail "shell fragment keeps the upstream coding-agent shortcut"

[[ $(sed -n '1p' "$test_home/.bashrc") == '# >>> omarchy-pi-agent-shell >>>' ]] ||
  fail "shell fragment is sourced before the bashrc guard"
grep -Fxq 'custom bashrc' "$test_home/.bashrc" || fail "setup preserves bashrc content"
[[ -f $test_home/.bashrc.omarchy-pi-agent.bak ]] || fail "setup backs up bashrc before editing"
cmp -s <(printf '[[ -f ~/.bashrc ]] && . ~/.bashrc\ncustom profile\n') "$test_home/.bash_profile" ||
  fail "setup leaves a profile that already sources bashrc unchanged"
[[ ! -e $test_home/.bash_profile.omarchy-pi-agent.bak ]] ||
  fail "setup does not rewrite a profile that already sources bashrc"
[[ ! -e $test_home/.config/omarchy/defaults/agent ]] || fail "setup does not choose a default agent"

# A second run does not duplicate startup blocks or replace user-owned skills.
run_setup
[[ $(grep -Fc '# >>> omarchy-pi-agent-shell >>>' "$test_home/.bashrc") == 1 ]] ||
  fail "setup is idempotent for bashrc"
[[ $(grep -Fc '# >>> omarchy-pi-agent-shell >>>' "$test_home/.bash_profile") == 0 ]] ||
  fail "setup does not add a redundant profile block"
[[ -f $test_home/.agents/skills/omarchy/README ]] || fail "rerun preserves the foreign skill directory"

# A profile without a bashrc source receives an appended block and a backup.
printf 'custom login profile\n' >"$test_home/.bash_profile"
run_setup
grep -Fxq 'custom login profile' "$test_home/.bash_profile" || fail "setup preserves custom profile content"
[[ $(grep -Fc '# >>> omarchy-pi-agent-shell >>>' "$test_home/.bash_profile") == 1 ]] ||
  fail "setup appends one profile source block"
[[ -f $test_home/.bash_profile.omarchy-pi-agent.bak ]] || fail "setup backs up bash_profile before editing"

# Mise is a prerequisite and failure happens before any user startup edit.
missing_home="$test_tmp/missing-mise"
mkdir -p "$missing_home/.local/share/omarchy-pi"
ln -s "$release" "$missing_home/.local/share/omarchy-pi/current"
if HOME="$missing_home" \
  OMARCHY_PATH="$missing_home/.local/share/omarchy-pi/current" \
  PATH=/usr/bin:/bin \
  "$ROOT/install/arm64/setup-user-agents.sh" >/dev/null 2>&1; then
  fail "setup refuses to run without mise"
fi
[[ ! -e $missing_home/.bashrc ]] || fail "missing mise leaves startup files untouched"

pass "Pi agent setup reuses Mise, preserves user skills, and configures idempotent shell paths"
