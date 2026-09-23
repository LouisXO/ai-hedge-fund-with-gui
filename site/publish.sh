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
git add site/public
git commit -q -m "site: daily public signals $DATE"
# another machine (or a manual push) may have moved the branch since the last run
git pull -q --rebase origin v2-rebuild
git push -q origin v2-rebuild
echo "已推送 → Netlify 将自动部署"
