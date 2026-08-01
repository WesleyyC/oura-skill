#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
installer="$repo_dir/install.sh"
test_root=$(mktemp -d "${TMPDIR:-/tmp}/oura-skill-install.XXXXXX")
trap 'rm -rf "$test_root"' EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

assert_same_target() {
  local link_path=$1
  [[ -L "$link_path" ]] || fail "$link_path is not a symlink"
  [[ "$link_path" -ef "$repo_dir" ]] || fail "$link_path does not target the repository"
}

home_all="$test_root/all"
mkdir -p "$home_all"
HOME="$home_all" "$installer" --all
assert_same_target "$home_all/.agents/skills/oura"
assert_same_target "$home_all/.claude/skills/oura"
HOME="$home_all" "$installer" --all

home_codex="$test_root/codex"
mkdir -p "$home_codex"
HOME="$home_codex" "$installer" --codex
assert_same_target "$home_codex/.agents/skills/oura"
[[ ! -e "$home_codex/.claude/skills/oura" ]] || fail "--codex installed the Claude link"

home_claude="$test_root/claude"
mkdir -p "$home_claude"
HOME="$home_claude" "$installer" --claude
assert_same_target "$home_claude/.claude/skills/oura"
[[ ! -e "$home_claude/.agents/skills/oura" ]] || fail "--claude installed the Codex link"

home_collision="$test_root/collision"
mkdir -p "$home_collision/.claude/skills"
printf 'occupied\n' >"$home_collision/.claude/skills/oura"
if HOME="$home_collision" "$installer" --all >/dev/null 2>&1; then
  fail "--all accepted an occupied destination"
fi
[[ ! -e "$home_collision/.agents/skills/oura" ]] || fail "preflight mutated Codex before detecting the Claude collision"

home_directory_collision="$test_root/directory-collision"
mkdir -p "$home_directory_collision/.agents/skills/oura"
if HOME="$home_directory_collision" "$installer" --codex >/dev/null 2>&1; then
  fail "installer accepted an existing destination directory"
fi

home_ancestor_collision="$test_root/ancestor-collision"
mkdir -p "$home_ancestor_collision/.agents"
printf 'occupied\n' >"$home_ancestor_collision/.agents/skills"
if HOME="$home_ancestor_collision" "$installer" --codex >/dev/null 2>&1; then
  fail "installer accepted a non-directory ancestor"
fi

home_wrong_link="$test_root/wrong-link"
mkdir -p "$home_wrong_link/.agents/skills" "$home_wrong_link/elsewhere"
ln -s "$home_wrong_link/elsewhere" "$home_wrong_link/.agents/skills/oura"
if HOME="$home_wrong_link" "$installer" --codex >/dev/null 2>&1; then
  fail "installer accepted a symlink to another target"
fi

home_dangling="$test_root/dangling"
mkdir -p "$home_dangling/.claude/skills"
ln -s "$home_dangling/missing" "$home_dangling/.claude/skills/oura"
if HOME="$home_dangling" "$installer" --claude >/dev/null 2>&1; then
  fail "installer accepted a dangling symlink"
fi

if HOME="$test_root/unknown" "$installer" --unknown >/dev/null 2>&1; then
  fail "installer accepted an unknown option"
fi

if HOME="$test_root/mixed" "$installer" --codex --claude >/dev/null 2>&1; then
  fail "installer accepted multiple selectors"
fi

invalid_repo="$test_root/invalid-source"
invalid_home="$test_root/invalid-source-home"
mkdir -p "$invalid_repo" "$invalid_home"
cp "$installer" "$invalid_repo/install.sh"
chmod +x "$invalid_repo/install.sh"
if HOME="$invalid_home" "$invalid_repo/install.sh" --all >/dev/null 2>&1; then
  fail "installer accepted a source without SKILL.md"
fi
[[ ! -e "$invalid_home/.agents/skills/oura" ]] || fail "invalid source created the Codex link"
[[ ! -e "$invalid_home/.claude/skills/oura" ]] || fail "invalid source created the Claude link"

printf 'Installer tests passed.\n'
