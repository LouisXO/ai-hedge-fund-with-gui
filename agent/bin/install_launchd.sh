#!/bin/zsh
# Compare the launchd jobs kept in the repositories with the installed ones and PRINT the commands that
# would install them. It never runs launchctl bootstrap/bootout, never copies a file: the owner reads the
# output and pastes the lines they want (AGENT.md §8: changing launchd is the owner's call).
#
#   zsh agent/bin/install_launchd.sh            status of every plist + the commands
#   zsh agent/bin/install_launchd.sh --diff     also the content difference for each plist that differs
#
# Sources: hedge-fund agent/launchd/*.plist and optradar launchd/*.plist. Installed: ~/Library/LaunchAgents
# (a job stopped on purpose is kept there as <label>.plist.disabled and is reported, not re-enabled).
# The only launchctl call is `launchctl list` (read-only), to show which jobs are loaded.
# Exit status: 0 when every repo copy matches the installed file, 1 when any differs or is not installed.
# Overridable for tests: HF_DIR, OPTRADAR_DIR, LAUNCH_DIR.
set -u
HF_DIR="${HF_DIR:-${0:A:h:h:h}}"
OPTRADAR_DIR="${OPTRADAR_DIR:-/Users/louis/optradar}"
LAUNCH_DIR="${LAUNCH_DIR:-$HOME/Library/LaunchAgents}"
SHOW_DIFF=0
[ "${1:-}" = "--diff" ] && SHOW_DIFF=1

norm() {   # the plist as canonical XML, so tabs vs spaces or binary vs XML do not count as a difference
  plutil -convert xml1 -o - "$1" 2>/dev/null || cat "$1"
}

LOADED="$(launchctl list 2>/dev/null | awk '{print $3}')"
n_diff=0
cmds=()
print "launchd 任务:仓库副本 vs 已安装($LAUNCH_DIR)"
for src in "$HF_DIR"/agent/launchd/*.plist(N) "$OPTRADAR_DIR"/launchd/*.plist(N); do
  f="${src:t}"
  label="$(plutil -extract Label raw -o - "$src" 2>/dev/null || print -r -- "${f%.plist}")"
  inst="$LAUNCH_DIR/$f"
  loaded="未加载"
  print -r -- "$LOADED" | grep -qx -- "$label" && loaded="已加载"
  if [ -f "$inst" ]; then
    if [ "$(norm "$src")" = "$(norm "$inst")" ]; then
      state="一致"
    else
      state="不同"; n_diff=$((n_diff + 1))
    fi
  elif [ -f "$inst.disabled" ]; then
    state="已停用(.disabled)"
    [ "$(norm "$src")" = "$(norm "$inst.disabled")" ] || state="已停用(.disabled),且内容不同"
  else
    state="未安装"; n_diff=$((n_diff + 1))
  fi
  printf '  %-34s %-26s %s  ← %s\n' "$label" "$state" "$loaded" "$src"
  if [ $SHOW_DIFF = 1 ] && [ "$state" = "不同" ]; then
    diff <(norm "$inst") <(norm "$src") | sed 's/^/      /'
  fi
  case "$state" in
    一致) ;;
    已停用*)
      cmds+=("# $label 是有意停用的;恢复时才运行:mv \"$inst.disabled\" \"$inst\" && launchctl bootstrap gui/\$(id -u) \"$inst\"") ;;
    *)
      cmds+=("launchctl bootout gui/\$(id -u)/$label 2>/dev/null; cp \"$src\" \"$inst\" && launchctl bootstrap gui/\$(id -u) \"$inst\"") ;;
  esac
done
for inst in "$LAUNCH_DIR"/com.louis.*.plist(N); do
  f="${inst:t}"
  [ -f "$HF_DIR/agent/launchd/$f" ] || [ -f "$OPTRADAR_DIR/launchd/$f" ] || print "  $f 已安装但不在任何仓库里"
done
print
if [ ${#cmds[@]} = 0 ]; then
  print "全部一致,不需要任何命令。"
else
  print "要安装或更新,复制对应的一行到终端运行(本脚本不执行任何一行):"
  for c in "${cmds[@]}"; do print -r -- "  $c"; done
fi
print
print "仓库副本和已安装不一致或未安装:$n_diff 个"
[ $n_diff = 0 ]
