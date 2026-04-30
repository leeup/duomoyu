/**
 * 多摸鱼 · Cloudflare Workers 热榜聚合 API
 * ============================================================
 * 免费部署，全球 CDN，HTTPS + CORS 默认就绪。
 *
 * 部署:
 *    cd worker
 *    npm install -g wrangler   (首次)
 *    wrangler login            (首次,浏览器登录 Cloudflare)
 *    wrangler deploy
 *
 * 部署后会得到一个 https://你的名字.workers.dev 的 URL,
 * 把它填进多摸鱼.html 的「自定义 → Worker URL」即可。
 *
 * 路由:
 *    GET /              查看 README / 平台列表
 *    GET /api/list      JSON 格式的平台列表
 *    GET /api/health    自检 (会真实请求每个上游一次)
 *    GET /api/{id}      获取某平台热榜
 *
 * 输出格式 (imsyy 风格,与多摸鱼.html 直接兼容):
 *    {
 *      "code": 200,
 *      "name": "知乎热榜",
 *      "data": [{title, url, hot}],
 *      "elapsed_ms": 388
 *    }
 *
 * 缓存:  默认 120 秒,通过 Cache API 全球边缘缓存
 * 容错:  上游失败时返回 502 + 详细 error 字段
 */

const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36';
const CACHE_TTL = 120;

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

const ok = (data, extra = {}) =>
  new Response(JSON.stringify({ code: 200, ...extra, data }, null, 0), {
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': `public, max-age=${CACHE_TTL}`, ...CORS },
  });

const err = (status, message, detail) =>
  new Response(JSON.stringify({ code: status, message, detail }, null, 2), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...CORS },
  });

const fetchJson = async (url, init = {}) => {
  const r = await fetch(url, { ...init, headers: { 'User-Agent': UA, ...(init.headers || {}) } });
  if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText} for ${url}`);
  return r.json();
};

const fetchText = async (url, init = {}) => {
  const r = await fetch(url, { ...init, headers: { 'User-Agent': UA, ...(init.headers || {}) } });
  if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText} for ${url}`);
  return r.text();
};

// 第三方聚合兜底链 - 当 Cloudflare Worker 出口 IP 被某些大站
// (知乎/微博/B站) 风控拉黑时使用。两层兜底:
//   1) imsyy 直连
//   2) vvhan 直连
// 任一成功就返回,都失败才抛异常。
//
// 字段差异处理:
//   imsyy:  { data: [{ title, url, mobileUrl, hot }] }
//   vvhan:  { success, data: [{ title, url, mobil_url, hot }] }
const normalizeAggItem = (it) => ({
  title: it.title || it.name || '',
  url:   it.url || it.mobileUrl || it.mobile_url || it.mobil_url || '',
  hot:   parseInt(String(it.hot ?? it.heat ?? it.score ?? 0).replace(/[^\d]/g, '')) || 0,
});

// directError 是直连失败时的错误信息,会被合并进最终错误链,方便诊断到底是哪一层挂了
const aggregatorFallback = async (imsyyId, vvhanId, directError = null) => {
  const errors = directError ? [`direct: ${directError}`] : [];

  // ① imsyy
  try {
    const j = await fetchJson(`https://api-hot.imsyy.top/${imsyyId}`);
    const list = j?.data;
    if (Array.isArray(list) && list.length) {
      return list.slice(0, 30).map(normalizeAggItem).filter(x => x.title);
    }
    errors.push('imsyy: empty data');
  } catch (e) {
    errors.push(`imsyy: ${e.message}`);
  }

  // ② vvhan
  if (vvhanId) {
    try {
      const j = await fetchJson(`https://api.vvhan.com/api/hotlist?type=${vvhanId}`);
      // vvhan 用 success 字段标记成功 (但有的版本没这个字段)
      const list = j?.data;
      if (Array.isArray(list) && list.length && j.success !== false) {
        return list.slice(0, 30).map(normalizeAggItem).filter(x => x.title);
      }
      errors.push('vvhan: empty data or success=false');
    } catch (e) {
      errors.push(`vvhan: ${e.message}`);
    }
  }

  throw new Error(`all sources failed [${imsyyId}/${vvhanId}] | ${errors.join(' | ')}`);
};

// ============================================================
// 各平台抓取实现
// 思路:全部直连各平台自己的 API/页面,不依赖第三方聚合站
// ============================================================

const PLATFORMS = {
  /* ---------- 知乎热榜 (Cloudflare 出口 IP 受风控,直接走 imsyy 兜底) ----------
     直连尝试: /billboard 公开页 → js-initialData JSON.
     现状: Cloudflare Workers 的 IP 段被知乎风控,返回 403 Forbidden.
     用户浏览器从家庭 IP 访问页面时,前端会自己走 imsyy 兜底,不影响体验.
  */
  zhihu: {
    name: '知乎热榜',
    fetch: async () => {
      try {
        const html = await fetchText('https://www.zhihu.com/billboard', {
          headers: { 'Referer': 'https://www.zhihu.com/' },
        });
        const m = html.match(/<script id="js-initialData"[^>]*>([\s\S]*?)<\/script>/);
        if (!m) throw new Error('zhihu: js-initialData not found');
        const j = JSON.parse(m[1]);
        const list = j?.initialState?.topstory?.hotList || [];
        if (!list.length) throw new Error('zhihu: empty hotList');
        return list.slice(0, 30).map(it => {
          const t = it.target || {};
          return {
            title: t.titleArea?.text || '',
            url:   t.link?.url || `https://www.zhihu.com/question/${t.id || ''}`,
            hot:   parseInt((t.metricsArea?.text || '').replace(/[^\d]/g, '')) || 0,
          };
        }).filter(x => x.title);
      } catch (e) {
        // 直连失败 → 第三方聚合 (imsyy → vvhan), 把直连错误也带上
        return aggregatorFallback('zhihu', 'zhihuHot', e.message);
      }
    },
  },

  /* ---------- 微博热搜 (同上,Cloudflare 出口 IP 被风控,走聚合兜底) ---------- */
  weibo: {
    name: '微博热搜',
    fetch: async () => {
      try {
        const containerId = '106003type=25&t=3&disable_hot=1&filter_type=realtimehot';
        const j = await fetchJson(
          `https://m.weibo.cn/api/container/getIndex?containerid=${encodeURIComponent(containerId)}`,
          { headers: { 'Referer': 'https://m.weibo.cn/', 'MWeibo-Pwa': '1' } },
        );
        const cards = j?.data?.cards?.[0]?.card_group || [];
        if (!cards.length) throw new Error('weibo: empty card_group');
        return cards.slice(0, 30).map(it => ({
          title: it.desc || '',
          url:   it.scheme || `https://s.weibo.com/weibo?q=${encodeURIComponent(it.desc || '')}`,
          hot:   parseInt(String(it.desc_extr || 0).replace(/[^\d]/g, '')) || 0,
        })).filter(x => x.title);
      } catch (e) {
        return aggregatorFallback('weibo', 'wbHot', e.message);
      }
    },
  },

  /* ---------- V2EX 热门 ---------- */
  v2ex: {
    name: 'V2EX 热门',
    fetch: async () => {
      const j = await fetchJson('https://www.v2ex.com/api/topics/hot.json');
      return j.slice(0, 20).map(it => ({
        title: it.title,
        url:   it.url,
        hot:   it.replies || 0,
      }));
    },
  },

  /* ---------- 百度热搜 (PC 站 HTML 解析) ---------- */
  baidu: {
    name: '百度热搜',
    fetch: async () => {
      // wise (移动端) API 现只返 1 条; PC 站把数据嵌在 <!--s-data:{...}--> 注释里
      const html = await fetchText('https://top.baidu.com/board?tab=realtime', {
        headers: { 'Referer': 'https://top.baidu.com/' },
      });
      const m = html.match(/<!--s-data:\s*([\s\S]*?)\s*-->/);
      if (!m) throw new Error('baidu: s-data comment not found in HTML');
      const j = JSON.parse(m[1]);
      const cards = j?.data?.cards?.[0]?.content || [];
      if (!cards.length) throw new Error('baidu: empty content array in s-data');
      return cards.slice(0, 30).map(it => ({
        title: it.word || it.query || '',
        url:   it.url || it.appUrl || `https://www.baidu.com/s?wd=${encodeURIComponent(it.word || '')}`,
        hot:   parseInt(it.hotScore || it.heatScore || 0),
      })).filter(x => x.title);
    },
  },

  /* ---------- B 站热门 (B 站全面 wbi 化,Cloudflare 出口几乎都被拦,走 imsyy) ---------- */
  bilibili: {
    name: 'B 站热门',
    fetch: async () => {
      try {
        const j = await fetchJson(
          'https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all',
          { headers: { 'Referer': 'https://www.bilibili.com/' } },
        );
        const list = j?.data?.list || [];
        if (!list.length) throw new Error('bilibili: empty ranking list');
        return list.slice(0, 20).map(it => ({
          title: it.title,
          url:   it.short_link_v2 || `https://www.bilibili.com/video/${it.bvid}`,
          hot:   it.stat?.view || 0,
        }));
      } catch (e) {
        return aggregatorFallback('bilibili', 'bili', e.message);
      }
    },
  },

  /* ---------- 豆瓣电影热映 ---------- */
  douban: {
    name: '豆瓣热映',
    fetch: async () => {
      const j = await fetchJson(
        'https://movie.douban.com/j/search_subjects?type=movie&tag=%E7%83%AD%E9%97%A8&page_limit=20&page_start=0',
        { headers: { 'Referer': 'https://movie.douban.com/' } },
      );
      return (j.subjects || []).map(it => ({
        title: `${it.title} · ${it.rate || '-'}`,
        url:   it.url,
        hot:   parseFloat(it.rate || 0) * 1000,
      }));
    },
  },

  /* ---------- 今日头条热榜 ---------- */
  toutiao: {
    name: '今日头条',
    fetch: async () => {
      const j = await fetchJson(
        'https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc',
        { headers: { 'Referer': 'https://www.toutiao.com/' } },
      );
      const list = j?.data || [];
      return list.slice(0, 30).map(it => ({
        title: it.Title || it.title,
        url:   it.Url || it.url,
        hot:   parseInt(it.HotValue || it.hot_value || 0),
      }));
    },
  },

  /* ---------- 少数派热门 ---------- */
  sspai: {
    name: '少数派热门',
    fetch: async () => {
      const j = await fetchJson(
        'https://sspai.com/api/v1/article/tag/page/get?limit=20&offset=0&tag=%E7%83%AD%E9%97%A8%E6%96%87%E7%AB%A0',
      );
      return (j.data || []).map(it => ({
        title: it.title,
        url:   `https://sspai.com/post/${it.id}`,
        hot:   it.like_count || it.comment_count || 0,
      }));
    },
  },

  /* ---------- 36 氪 24 小时热榜 ---------- */
  '36kr': {
    name: '36 氪热榜',
    fetch: async () => {
      const r = await fetch('https://gateway.36kr.com/api/mis/nav/home/nav/rank/hot', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'User-Agent': UA,
        },
        body: JSON.stringify({ partner_id: 'wap', param: { siteId: 1, platformId: 2 }, timestamp: Date.now() }),
      });
      const j = await r.json();
      const list = j?.data?.hotRankList || [];
      return list.slice(0, 20).map(it => ({
        title: it?.templateMaterial?.widgetTitle || it.title || '',
        url:   `https://www.36kr.com/p/${it.itemId}`,
        hot:   it?.templateMaterial?.statRead || 0,
      }));
    },
  },

  /* ---------- IT之家热榜 (m 站 HTML + 宽松正则,失败兜底 imsyy) ---------- */
  ithome: {
    name: 'IT 之家热榜',
    fetch: async () => {
      try {
        const html = await fetchText('https://m.ithome.com/rankm/', {
          headers: { 'Referer': 'https://m.ithome.com/' },
        });
        const items = [];
        const seen = new Set();
        const re = /<a[^>]+href="(https?:\/\/www\.ithome\.com\/0\/\d+\/\d+\.htm)"[^>]*>([\s\S]*?)<\/a>/g;
        let m;
        while ((m = re.exec(html)) && items.length < 25) {
          if (seen.has(m[1])) continue;
          const title = m[2].replace(/<[^>]+>/g, '').replace(/&[a-z#0-9]+;/g, ' ').trim();
          if (title && title.length > 5 && title.length < 200) {
            seen.add(m[1]);
            items.push({ title, url: m[1], hot: 25 - items.length });
          }
        }
        if (!items.length) throw new Error('ithome: HTML 解析 0 条');
        return items;
      } catch (e) {
        return aggregatorFallback('ithome', 'itHome', e.message);
      }
    },
  },
};

// ============================================================
// 路由
// ============================================================

async function handleRequest(request) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, '');

  if (request.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: CORS });
  }

  // 根路径:返回简易 HTML 说明页
  if (path === '' || path === '/') {
    return new Response(README_HTML(url.host), {
      headers: { 'Content-Type': 'text/html; charset=utf-8', ...CORS },
    });
  }

  // 平台列表
  if (path === '/api/list') {
    return ok(Object.entries(PLATFORMS).map(([id, p]) => ({ id, name: p.name })));
  }

  // 健康检查
  if (path === '/api/health') {
    const t0 = Date.now();
    const results = await Promise.all(
      Object.entries(PLATFORMS).map(async ([id, p]) => {
        const t = Date.now();
        try {
          const data = await p.fetch();
          return { id, name: p.name, ok: true, items: data.length, elapsed_ms: Date.now() - t };
        } catch (e) {
          return { id, name: p.name, ok: false, error: String(e), elapsed_ms: Date.now() - t };
        }
      }),
    );
    const okCount = results.filter(r => r.ok).length;
    return new Response(JSON.stringify({
      summary: `${okCount}/${results.length} 个平台正常`,
      total_elapsed_ms: Date.now() - t0,
      results,
    }, null, 2), {
      headers: { 'Content-Type': 'application/json; charset=utf-8', ...CORS },
    });
  }

  // /api/{platform}
  const match = path.match(/^\/api\/([\w.-]+)$/);
  if (match) {
    const id = match[1];
    const p = PLATFORMS[id];
    if (!p) return err(404, `unknown platform: ${id}`, { available: Object.keys(PLATFORMS) });

    // Cache API
    const cacheUrl = new URL(request.url);
    const cache = caches.default;
    let cached = await cache.match(cacheUrl);
    if (cached) {
      const h = new Headers(cached.headers);
      h.set('X-Cache', 'HIT');
      return new Response(cached.body, { headers: h });
    }

    const t = Date.now();
    try {
      const data = await p.fetch();
      const resp = ok(data, { name: p.name, elapsed_ms: Date.now() - t });
      // 写缓存(只写成功响应)
      const h = new Headers(resp.headers);
      h.set('X-Cache', 'MISS');
      const cacheable = new Response(resp.clone().body, { headers: h });
      await cache.put(cacheUrl, cacheable.clone());
      return cacheable;
    } catch (e) {
      return err(502, 'upstream failed', { platform: id, error: String(e), elapsed_ms: Date.now() - t });
    }
  }

  return err(404, 'not found', {
    paths: ['/api/list', '/api/health', '/api/{platform}'],
    platforms: Object.keys(PLATFORMS),
  });
}

function README_HTML(host) {
  const list = Object.entries(PLATFORMS).map(([id, p]) =>
    `<li><code><a href="/api/${id}">/api/${id}</a></code> — ${p.name}</li>`).join('');
  return `<!DOCTYPE html><html lang=zh><head><meta charset=utf-8><title>多摸鱼 · 热榜聚合 API</title>
<style>body{font-family:-apple-system,sans-serif;max-width:720px;margin:48px auto;padding:0 24px;line-height:1.7;color:#1f2937}
code{background:#f4f6f8;padding:2px 6px;border-radius:4px;font-size:13px}
a{color:#1b98c7}h1{margin-bottom:8px}h2{margin-top:32px;border-bottom:1px solid #e5e7eb;padding-bottom:6px}
.host{color:#94a3b8;font-size:14px;font-family:monospace}</style>
</head><body>
<h1>🐟 多摸鱼 · 热榜聚合 API</h1>
<p class="host">${host}</p>
<p>免费、CORS 友好、120 秒边缘缓存。直接对接各平台官方端点,不依赖第三方聚合站。</p>
<h2>可用平台</h2><ul>${list}</ul>
<h2>诊断</h2>
<ul>
<li><code><a href="/api/list">/api/list</a></code> — 平台列表 (JSON)</li>
<li><code><a href="/api/health">/api/health</a></code> — 全平台真实抓取自检</li>
</ul>
<h2>响应格式</h2>
<pre><code>{
  "code": 200,
  "name": "知乎热榜",
  "elapsed_ms": 388,
  "data": [
    { "title": "...", "url": "...", "hot": 1234567 },
    ...
  ]
}</code></pre>
<h2>对接多摸鱼.html</h2>
<p>把 <code>https://${host}</code> 填进「自定义 → Worker URL」即可,前端会优先用此源。</p>
</body></html>`;
}

export default {
  async fetch(request, env, ctx) {
    return handleRequest(request);
  },
};
