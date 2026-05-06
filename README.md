# 🐟 多摸鱼

一站摸尽全网热榜的极简单文件 Web 应用,参考 [momoyu.cc](https://momoyu.cc) 复刻。

**在线访问**: <https://leeup.github.io/duomoyu/> (由 GitHub Pages 托管,5 分钟自动刷新数据)

打开页面 → 12 个平台热榜实时聚合,无需登录、可双击打开本地 HTML 文件就用。

## 数据源(按优先级,自动 fallback)

| 优先级 | 来源 | 说明 |
|---|---|---|
| ⭐ | **GitHub Action 静态 JSON** | GitHub Actions 每 5 分钟跑 [`scripts/fetch.py`](scripts/fetch.py) 抓数据,推到 `gh-pages` 分支,经 GitHub Pages CDN 静态分发。**最稳**,99% 情况下用户访问就是这一档 |
| ⓪ | 本地代理 `serve.py` | `python3 serve.py` 启动后访问 `http://localhost:8000/多摸鱼.html`,后端反向代理 imsyy/vvhan(开发用) |
| ① | imsyy 直连 (浏览器) | 浏览器直接 fetch `api-hot.imsyy.top` |
| ② | vvhan fetch / JSONP | 同上,JSONP 兜底 file:// 协议下的 CORS |
| ③ | 内置样例数据 | 全部失败时显示 mock 数据,保证不黑屏 |

每张卡右下角会显示当前生效的数据源,如 `实时 · GH Action ⭐`、`实时 · imsyy`、`样例数据` 等。

## 项目结构

```
duomoyu/
├── 多摸鱼.html                  ← 单文件前端
├── serve.py                     ← 本地反向代理 + 静态服务器 (开发用)
├── scripts/
│   ├── fetch.py                 ← GitHub Action 抓取脚本
│   └── local-update.sh          ← 本地手动抓取并推送 gh-pages
└── .github/workflows/
    └── fetch-hotlist.yml        ← Action 每 5 分钟跑 fetch.py
```

## 三种使用方式

### 方式一:直接打开 (推荐)

打开 <https://leeup.github.io/duomoyu/> 即可。或者双击本地的 `多摸鱼.html`,效果一样,数据都从 GitHub Pages 拉。

### 方式二:本地服务器(开发用)

```bash
python3 serve.py            # 默认 8000
python3 serve.py 8765 -v    # 自定义端口 + verbose 日志
```

打开 http://localhost:8000/多摸鱼.html。诊断接口:
- `/api/health` — DNS / 网络自检
- `/api/debug/zhihu` — 逐源探测,完整诊断 JSON

## GitHub Action 数据流

```
cron 每 5 分钟
    ↓
GitHub Actions runner (Azure IP)
    ↓
执行 scripts/fetch.py (12 平台并发抓取,失败保留 last-good)
    ↓
推到 gh-pages 分支
    ↓
GitHub Pages 提供静态访问 https://<user>.github.io/duomoyu/data.json
    ↓
前端 fetch 这个 URL 拿全部平台数据
```

## 也可以本地手动推一次 (双轨)

GitHub Action 在云端 5 分钟跑一次,如果你想立刻拿到一次更新(尤其是想**绕过 Azure IP 的反爬**,用家庭宽带抓数据),在 Mac 终端跑:

```bash
bash scripts/local-update.sh
```

脚本会:拉 CDN 上现有 `data.json` → 本地跑 `fetch.py` → force-push 到 `gh-pages` 分支。1-2 分钟后 Pages 上的 JSON 就刷新了。

家庭 IP 的优势是反爬命中率几乎 100%,知乎/微博/B站 都能直连不走兜底。下一次 GH Action 跑时会拿你刚推的数据当 last-good 输入,所以即使 Action 抓不到的平台,数据也保留。

## 为什么这么设计

国内主流站点(知乎/微博/B站)对 IP 反爬很激进。早期版本用 Cloudflare Worker 做 backend 实时抓取,但 Cloudflare ASN 段几乎全被风控。GitHub Actions runner 的 Azure IP 池更大、影响面更广,反爬强度低,因此成为主力数据源。

但 Action 也不是 100% 可靠,所以保留了多层浏览器端兜底:任何一层挂了,前端会自动尝试下一层。最坏情况显示样例数据,不会出现"卡片空白"。

## License

MIT
