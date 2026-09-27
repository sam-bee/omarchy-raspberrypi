# Link Omarchy's shipped agent skills into the supported user agent trees.
#
# This leaf is intentionally sourced by user setup commands. Existing files,
# directories, and links that do not already point at the current Omarchy
# skill are user-owned and are left alone.

: "${HOME:?HOME must be set before installing agent skills}"
: "${OMARCHY_PATH:?OMARCHY_PATH must be set before installing agent skills}"

omarchy_agent_skill_link() {
  local source=$1 destination=$2

  if [[ -L $destination ]]; then
    if [[ $(readlink -- "$destination") == "$source" ]]; then
      return 0
    fi
    echo "Preserving existing agent skill link: $destination" >&2
    return 0
  elif [[ -e $destination ]]; then
    echo "Preserving existing agent skill path: $destination" >&2
    return 0
  fi

  # A concurrent writer may publish the path between the check and ln. Treat
  # that as another user-owned path, while still surfacing other failures.
  if ln -s -- "$source" "$destination" 2>/dev/null; then
    return 0
  elif [[ -L $destination || -e $destination ]]; then
    echo "Preserving existing agent skill path: $destination" >&2
    return 0
  else
    echo "Could not link Omarchy agent skill: $destination" >&2
    return 1
  fi
}

skill_roots=(
  "$HOME/.agents/skills"
  "$HOME/.claude/skills"
  "$HOME/.codex/skills"
  "$HOME/.pi/agent/skills"
  "$HOME/.gemini/config/skills"
  "$HOME/.hermes/skills"
)
for skill_root in "${skill_roots[@]}"; do
  mkdir -p -- "$skill_root"
done

for skill in "$OMARCHY_PATH"/default/agents/skills/*/; do
  [[ -d $skill ]] || continue
  skill=${skill%/}
  name=${skill##*/}

  for skill_root in "${skill_roots[@]}"; do
    omarchy_agent_skill_link "$skill" "$skill_root/$name"
  done

  if [[ -d $HOME/.hermes/profiles ]]; then
    for profile in "$HOME/.hermes/profiles"/*/; do
      [[ -d $profile ]] || continue
      profile_skills="$profile/skills"
      mkdir -p -- "$profile_skills"
      omarchy_agent_skill_link "$skill" "$profile_skills/$name"
    done
  fi
done
