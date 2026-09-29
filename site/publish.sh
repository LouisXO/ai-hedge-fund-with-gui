#!/bin/zsh
# 生成公开站并(可选)推送触发 Netlify 部署。
# 默认只生成不推送 —— 设 PUBLISH=1 才对外发布。
set -e
cd /Users/louis/hedge-fund
DATE="${1:-$(date +%F)}"
~/.hedgefund-venv/bin/python site/build_site.py "$DATE"
PYTHONPATH=/Users/louis/hedge-fund ~/.hedgefund-venv/bin/python site/build_paper.py || echo "paper page failed (non-fatal)"

if [ "$PUBLISH" != "1" ]; then
  echo "PUBLISH!=1 → 只生成未推送(本地预览: open site/public/index.html)"
  exit 0
fi
if [ -z "$(git status --porcelain site/public)" ]; then
  echo "无变化,跳过推送"
  exit 0
fi
# 本仓库是公开 fork,推送的是整个 v2-rebuild 分支:推送前扫描待推送内容里的真实账户标记
# (site/publish_guard.py;私有词表 ~/.hedge-fund/publish_guard_terms.txt)。命中就中止,不推送。
# PUBLISH_GUARD=off 只供用户确认误报后手工运行,定时任务永远不设。
guard() {
  if [ "$PUBLISH_GUARD" = "off" ]; then
    echo "警告:PUBLISH_GUARD=off,跳过真实账户检查($*)"
    return 0
  fi
  PYTHONPATH=/Users/louis/hedge-fund ~/.hedgefund-venv/bin/python site/publish_guard.py "$@"
}
blocked() {   # the jobs treat a failed publish as non-fatal, so say it where the owner will see it
  echo "$1"
  TN=/opt/homebrew/bin/terminal-notifier
  [ -x "$TN" ] && "$TN" -title "公开站推送已中止" -message "发现疑似真实账户信息,见任务日志" -group publish_guard >/dev/null 2>&1
  exit 3
}
git add site/public
if ! guard --staged; then
  git reset -q site/public
  blocked "推送已中止:待提交的 site/public 里有疑似真实账户信息(见上)。已取消暂存,未提交;先修生成器再重跑。"
fi
git commit -q -m "site: daily public signals $DATE"
# another machine (or a manual push) may have moved the branch since the last run
git pull -q --rebase --autostash origin v2-rebuild
if ! guard --range origin/v2-rebuild..HEAD; then
  blocked "推送已中止:待推送的提交里有疑似真实账户信息(见上,<提交号>:<文件>)。提交留在本地未推送;补一个删除提交不够(推送会带上全部提交),要改写本地未推送的历史,见 docs/RUNBOOK.md 第 10 节。"
fi
git push -q origin v2-rebuild
echo "已推送 → Netlify 将自动部署"
