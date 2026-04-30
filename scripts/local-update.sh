#!/bin/bash
# =============================================================================
# 多摸鱼 · 本地一键抓取 + 推送 gh-pages
# =============================================================================
# 用法: bash scripts/local-update.sh
#       (在项目根目录,或任意子目录,都能跑)
#
# 干什么:
#   1. 从 GitHub Pages CDN 拉一份现有的 data.json 作为 last-good 输入
#   2. 用 scripts/fetch.py 在你 Mac 上抓 12 个平台 (家庭 IP, 反爬概率最低)
#   3. force-push 一个干净的 orphan commit 到 gh-pages 分支
#      (跟 GitHub Action 的 force_orphan: true 行为一致, 不会污染历史)
#
# 双轨说明:
#   GH Action 仍然每 5 分钟跑一次, 你本地推完之后的 5 分钟之内 Action 会再次刷新.
#   两边 force-push 互相覆盖, 但因为各自都拿"上一份 data.json"做 last-good 输入,
#   所以数据是叠加更新的, 不会丢. 你本地推后的下一次 Action run 会以你的数据为基线.
#
# 依赖: python3, git, curl
# =============================================================================

set -euo pipefail

# 切到项目根 (脚本所在目录的父目录)
cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
REPO_URL="$(git remote get-url origin)"

# 自动派生 GitHub Pages URL: 把 https://github.com/<owner>/<repo> 转成
# https://<owner>.github.io/<repo>/data.json
PAGES_DATA_URL=$(
  echo "$REPO_URL" \
    | sed -E 's#^git@github\.com:#https://github.com/#' \
    | sed -E 's#\.git$##' \
    | sed -E 's#https://github\.com/([^/]+)/(.+)#https://\1.github.io/\2/data.json#'
)

TMPDIR="$(mktemp -d -t duomoyu-ghpages-XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

echo "🐟 多摸鱼 · 本地抓取 + 推送"
echo "   仓库: $REPO_URL"
echo "   Pages: $PAGES_DATA_URL"
echo ""

# -----------------------------------------------------------------------------
# Step 1: 拉 last-good (CDN 上最新的 data.json)
# -----------------------------------------------------------------------------
echo "→ Step 1: 拉取 last-good (作为 fetch.py 的输入,失败的平台保留旧数据)"
if curl -fsSL --max-time 20 "$PAGES_DATA_URL" -o "$TMPDIR/data.json" 2>/dev/null; then
  size=$(wc -c < "$TMPDIR/data.json" | tr -d ' ')
  echo "   ✓ 拉到 ${size} 字节"
else
  echo "   ⚠ 拉不到 (Pages 还没生效, 或网络问题), 用空数据起步"
  echo '{"platforms":{}}' > "$TMPDIR/data.json"
fi
echo ""

# -----------------------------------------------------------------------------
# Step 2: 跑 fetch.py
# -----------------------------------------------------------------------------
echo "→ Step 2: 跑 scripts/fetch.py (12 平台并发抓取)"
echo ""

# 确保 requests 已装
python3 -c "import requests" 2>/dev/null || {
  echo "   ⚠ 未安装 requests, 现在 pip install"
  python3 -m pip install --user requests >/dev/null
}

python3 "$PROJECT_ROOT/scripts/fetch.py" \
  --input "$TMPDIR/data.json" \
  --output "$TMPDIR/data.json"
echo ""

# -----------------------------------------------------------------------------
# Step 3: 准备 gh-pages 内容 (orphan branch, 单 commit)
# -----------------------------------------------------------------------------
echo "→ Step 3: 准备 gh-pages 内容 (force_orphan 风格,单提交)"
cd "$TMPDIR"
git init -q -b gh-pages

# 取本地 git 配置 (跟 main 分支一致)
USER_NAME="$(git -C "$PROJECT_ROOT" config user.name || echo 'local-update')"
USER_EMAIL="$(git -C "$PROJECT_ROOT" config user.email || echo 'local-update@example.com')"
git config user.name  "$USER_NAME"
git config user.email "$USER_EMAIL"

# 简单 index.html 方便人手浏览
cat > index.html <<'EOF'
<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>多摸鱼 · 数据</title>
<style>body{font-family:-apple-system,sans-serif;max-width:600px;margin:48px auto;padding:0 24px;line-height:1.7;color:#1f2937}
code{background:#f4f6f8;padding:2px 6px;border-radius:4px;font-size:13px}
a{color:#1b98c7}h1{margin-bottom:8px}.note{color:#94a3b8;font-size:13px}</style></head><body>
<h1>🐟 多摸鱼 · 静态数据</h1>
<p class="note">由本地 Mac 手动推送 (manual)。GitHub Action 也会每 5 分钟自动覆盖一次。</p>
<ul><li><a href="data.json">data.json</a> — 12 平台聚合数据</li></ul>
</body></html>
EOF

git add data.json index.html
git commit -q -m "manual: refresh from local Mac at $(date '+%Y-%m-%dT%H:%M:%S%z')"
echo ""

# -----------------------------------------------------------------------------
# Step 4: force push 到 gh-pages
# -----------------------------------------------------------------------------
echo "→ Step 4: force-push 到 gh-pages 分支"
git remote add origin "$REPO_URL"
git push -f -q origin gh-pages
echo ""

echo "✓ 完成! 1-2 分钟后查看:"
echo "   $PAGES_DATA_URL"
