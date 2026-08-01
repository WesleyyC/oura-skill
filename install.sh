#!/bin/sh
set -eu

usage() {
  printf 'Usage: %s --codex|--claude|--all\n' "$(basename "$0")" >&2
}

if [ "$#" -ne 1 ]; then
  usage
  exit 2
fi

install_codex=0
install_claude=0

case "$1" in
  --codex)
    install_codex=1
    ;;
  --claude)
    install_claude=1
    ;;
  --all)
    install_codex=1
    install_claude=1
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    printf 'Unknown option: %s\n' "$1" >&2
    usage
    exit 2
    ;;
esac

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
: "${HOME:?HOME must be set}"

if [ ! -f "$repo_dir/SKILL.md" ] || [ ! -r "$repo_dir/SKILL.md" ]; then
  printf 'Invalid Oura skill checkout: readable SKILL.md not found in %s\n' "$repo_dir" >&2
  exit 1
fi

codex_destination="$HOME/.agents/skills/oura"
claude_destination="$HOME/.claude/skills/oura"

preflight_destination() {
  destination=$1

  if [ -L "$destination" ]; then
    if [ -e "$destination" ] && [ "$destination" -ef "$repo_dir" ]; then
      return 0
    fi
    printf 'Refusing to replace symlink at %s\n' "$destination" >&2
    return 1
  fi

  if [ -e "$destination" ]; then
    printf 'Refusing to replace existing path at %s\n' "$destination" >&2
    return 1
  fi

  ancestor=$(dirname "$destination")
  while [ ! -e "$ancestor" ] && [ ! -L "$ancestor" ]; do
    next_ancestor=$(dirname "$ancestor")
    if [ "$next_ancestor" = "$ancestor" ]; then
      break
    fi
    ancestor=$next_ancestor
  done

  if [ ! -d "$ancestor" ]; then
    printf 'Cannot create %s because %s is not a directory\n' "$destination" "$ancestor" >&2
    return 1
  fi
}

install_destination() {
  destination=$1

  if [ -L "$destination" ] && [ -e "$destination" ] && [ "$destination" -ef "$repo_dir" ]; then
    printf 'Already installed: %s\n' "$destination"
    return 0
  fi

  mkdir -p "$(dirname "$destination")"
  ln -s "$repo_dir" "$destination"
  printf 'Installed: %s -> %s\n' "$destination" "$repo_dir"
}

if [ "$install_codex" -eq 1 ]; then
  preflight_destination "$codex_destination"
fi
if [ "$install_claude" -eq 1 ]; then
  preflight_destination "$claude_destination"
fi

if [ "$install_codex" -eq 1 ]; then
  install_destination "$codex_destination"
fi
if [ "$install_claude" -eq 1 ]; then
  install_destination "$claude_destination"
fi
