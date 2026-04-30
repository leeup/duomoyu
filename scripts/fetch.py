#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多摸鱼 · GitHub Actions 抓取脚本
=================================================================
设计目标:
  1. 在 GitHub Actions runner (Azure IP) 上跑,绕开 Cloudflare ASN 反爬
  2. 12 个平台并发抓取,失败的保留上一轮成功的旧数据 (last-good)
  3. 输出统一 data.json,前端直接消费

用法:
  python scripts/fetch.py [--input data.json] [--output data.json]

  默认 in/out 都是当前目录的 data.json
  Action 工作流: 先 curl 把 gh-pages 上的 data.json 下载到 ./data.json
                  再跑这个脚本读写它,最后推到 gh-pages

输出格式:
  {
    "version": 1,
    "updated_at": "2026-04-30T10:00:00Z",
    "summary": "11/12 platforms ok",
    "platforms": {
      "zhihu": {
        "name": "知乎热榜",
        "updated_at": "2026-04-30T10:00:00Z",
        "items": [{ "title", "url", "hot" }, ...]
      },
      ...
    }
  }
"""

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 15
# 完整模仿 Chrome 的 headers - 知乎/微博/B站 这些站点会校验 Sec-Fetch-* 等字段
HEADERS_BASE = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}


def _headers(extra=None):
    h = dict(HEADERS_BASE)
    if extra:
        h.update(extra)
    return h


def _new_session(homepage_url=None, referer=None):
    """新建一个 requests.Session, 可选先访问首页拿 cookie (针对 知乎/微博 这种要 d_c0 的)"""
    s = requests.Session()
    s.headers.update(HEADERS_BASE)
    if homepage_url:
        try:
            s.get(homepage_url, timeout=TIMEOUT, allow_redirects=True)
        except Exception:
            pass  # 首页拿不到 cookie 也无所谓,继续试目标
    if referer:
        s.headers["Referer"] = referer
    return s


def _to_int(v, default=0):
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else default


# =========================================================================
# 第三方聚合兜底 (imsyy → vvhan)
# 直连失败时调用; Azure runner IP 也被风控的平台 (知乎/微博/抖音/虎扑) 主要靠这层
# =========================================================================

def _normalize_agg_item(it):
    return {
        "title": it.get("title") or it.get("name") or "",
        "url": it.get("url") or it.get("mobileUrl") or it.get("mobile_url") or it.get("mobil_url") or "",
        "hot": _to_int(it.get("hot") if it.get("hot") is not None else (it.get("heat") if it.get("heat") is not None else it.get("score"))),
    }


def aggregator_fallback(imsyy_id, vvhan_id, direct_err=None):
    """imsyy → vvhan 二级兜底, 都失败抛异常 (会触发上层保留 last-good).
    direct_err: 直连失败的原因, 会被合并进最终错误链, 方便定位是哪一层挂了."""
    errors = [f"direct: {direct_err}"] if direct_err else []

    # ① imsyy
    try:
        r = requests.get(f"https://api-hot.imsyy.top/{imsyy_id}", headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        lst = j.get("data") if isinstance(j, dict) else None
        if isinstance(lst, list) and lst:
            return [_normalize_agg_item(it) for it in lst[:30] if it.get("title") or it.get("name")]
        errors.append("imsyy: empty data")
    except Exception as e:
        errors.append(f"imsyy: {type(e).__name__}: {e}")

    # ② vvhan
    if vvhan_id:
        try:
            r = requests.get(f"https://api.vvhan.com/api/hotlist?type={vvhan_id}", headers=_headers(), timeout=TIMEOUT)
            r.raise_for_status()
            j = r.json()
            lst = j.get("data") if isinstance(j, dict) else None
            if isinstance(lst, list) and lst and j.get("success") is not False:
                return [_normalize_agg_item(it) for it in lst[:30] if it.get("title") or it.get("name")]
            errors.append("vvhan: empty data or success=false")
        except Exception as e:
            errors.append(f"vvhan: {type(e).__name__}: {e}")

    raise RuntimeError(f"all sources failed [{imsyy_id}/{vvhan_id}] | {' | '.join(errors)}")


# =========================================================================
# 各平台抓取函数
# 每个函数返回 list of {title, url, hot}, 失败时 raise Exception
# =========================================================================

def fetch_zhihu():
    """知乎热榜 - 用 session 先访问首页拿 d_c0 cookie, 再访问 /billboard
       不带 cookie 的请求会被 403, 即使家庭 IP 也一样"""
    direct_err = None
    try:
        # 先访问首页, 让 zhihu 设置 d_c0/_zap/_xsrf 等设备指纹 cookie
        s = _new_session(homepage_url="https://www.zhihu.com/", referer="https://www.zhihu.com/")
        # 然后请求 /billboard, headers 跟首页连贯 (Sec-Fetch-Site: same-origin)
        s.headers["Sec-Fetch-Site"] = "same-origin"
        r = s.get("https://www.zhihu.com/billboard", timeout=TIMEOUT)
        r.raise_for_status()
        m = re.search(r'<script id="js-initialData"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if not m:
            raise RuntimeError("zhihu: js-initialData not found")
        j = json.loads(m.group(1))
        lst = j.get("initialState", {}).get("topstory", {}).get("hotList", []) or []
        items = []
        for it in lst[:30]:
            target = it.get("target") or {}
            title = (target.get("titleArea") or {}).get("text") or ""
            url = (target.get("link") or {}).get("url") or f"https://www.zhihu.com/question/{target.get('id', '')}"
            hot = _to_int((target.get("metricsArea") or {}).get("text"))
            if title:
                items.append({"title": title, "url": url, "hot": hot})
        if not items:
            raise RuntimeError("zhihu: empty result after parse")
        return items
    except Exception as e:
        direct_err = f"{type(e).__name__}: {e}"
    return aggregator_fallback("zhihu", "zhihuHot", direct_err)


def fetch_weibo():
    """微博热搜 - session 先访问 m.weibo.cn 拿 cookie 再请求 API
       (微博 API 不带 cookie 会重定向到登录页或返 HTML)"""
    direct_err = None
    try:
        s = _new_session(homepage_url="https://m.weibo.cn/", referer="https://m.weibo.cn/")
        s.headers["Sec-Fetch-Site"] = "same-origin"
        s.headers["X-Requested-With"] = "XMLHttpRequest"
        s.headers["MWeibo-Pwa"] = "1"
        s.headers["Accept"] = "application/json, text/plain, */*"
        container = "106003type=25&t=3&disable_hot=1&filter_type=realtimehot"
        url = f"https://m.weibo.cn/api/container/getIndex?containerid={requests.utils.quote(container)}"
        r = s.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        cards = (((j.get("data") or {}).get("cards") or [{}])[0].get("card_group")) or []
        items = []
        for it in cards[:30]:
            title = it.get("desc") or ""
            if not title:
                continue
            items.append({
                "title": title,
                "url": it.get("scheme") or f"https://s.weibo.com/weibo?q={requests.utils.quote(title)}",
                "hot": _to_int(it.get("desc_extr")),
            })
        if not items:
            raise RuntimeError("weibo: empty card_group")
        return items
    except Exception as e:
        direct_err = f"{type(e).__name__}: {e}"
    return aggregator_fallback("weibo", "wbHot", direct_err)


def fetch_v2ex():
    """V2EX 热门 - 官方 API"""
    r = requests.get("https://www.v2ex.com/api/topics/hot.json", headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, list) or not j:
        raise RuntimeError("v2ex: not a non-empty array")
    return [
        {"title": it.get("title", ""), "url": it.get("url", ""), "hot": _to_int(it.get("replies"))}
        for it in j[:20]
        if it.get("title")
    ]


def fetch_baidu():
    """百度热搜 - PC 站 HTML 注释里取 JSON"""
    r = requests.get(
        "https://top.baidu.com/board?tab=realtime",
        headers=_headers({"Referer": "https://top.baidu.com/"}),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    m = re.search(r"<!--s-data:\s*(.*?)\s*-->", r.text, re.DOTALL)
    if not m:
        raise RuntimeError("baidu: s-data comment not found")
    j = json.loads(m.group(1))
    cards = (((j.get("data") or {}).get("cards") or [{}])[0].get("content")) or []
    items = []
    for it in cards[:30]:
        title = it.get("word") or it.get("query") or ""
        if not title:
            continue
        items.append({
            "title": title,
            "url": it.get("url") or it.get("appUrl") or f"https://www.baidu.com/s?wd={requests.utils.quote(title)}",
            "hot": _to_int(it.get("hotScore") or it.get("heatScore")),
        })
    if not items:
        raise RuntimeError("baidu: empty content")
    return items


def fetch_douyin():
    """抖音热点 - PC API 需要 _signature 签名, 直接走聚合"""
    return aggregator_fallback("douyin", "douyinHot")


def fetch_bilibili():
    """B 站热门 - 排行榜端点 ranking/v2 (无需 wbi 签名)"""
    r = requests.get(
        "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",
        headers=_headers({"Referer": "https://www.bilibili.com/"}),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    j = r.json()
    lst = (j.get("data") or {}).get("list") or []
    if not lst:
        raise RuntimeError("bilibili: empty list (likely wbi rejection)")
    return [
        {
            "title": it.get("title", ""),
            "url": it.get("short_link_v2") or f"https://www.bilibili.com/video/{it.get('bvid', '')}",
            "hot": _to_int((it.get("stat") or {}).get("view")),
        }
        for it in lst[:20]
        if it.get("title")
    ]


def fetch_douban():
    """豆瓣热映 - 官方 search API"""
    r = requests.get(
        "https://movie.douban.com/j/search_subjects?type=movie&tag=%E7%83%AD%E9%97%A8&page_limit=20&page_start=0",
        headers=_headers({"Referer": "https://movie.douban.com/"}),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    j = r.json()
    subjects = j.get("subjects") or []
    if not subjects:
        raise RuntimeError("douban: empty subjects")
    return [
        {
            "title": f"{it.get('title', '')} · {it.get('rate', '-')}",
            "url": it.get("url", ""),
            "hot": int(float(it.get("rate") or 0) * 1000),
        }
        for it in subjects
        if it.get("title")
    ]


def fetch_hupu():
    """虎扑步行街 - 没找到稳定的公开 API, 直接走聚合"""
    return aggregator_fallback("hupu", "huPu")


def fetch_toutiao():
    """今日头条热榜 - 官方 API"""
    r = requests.get(
        "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        headers=_headers({"Referer": "https://www.toutiao.com/"}),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    j = r.json()
    lst = j.get("data") or []
    if not lst:
        raise RuntimeError("toutiao: empty data")
    return [
        {
            "title": it.get("Title") or it.get("title", ""),
            "url": it.get("Url") or it.get("url", ""),
            "hot": _to_int(it.get("HotValue") or it.get("hot_value")),
        }
        for it in lst[:30]
        if it.get("Title") or it.get("title")
    ]


def fetch_36kr():
    """36 氪 24 小时热榜 - 官方 API (POST)"""
    r = requests.post(
        "https://gateway.36kr.com/api/mis/nav/home/nav/rank/hot",
        headers=_headers({"Content-Type": "application/json"}),
        json={"partner_id": "wap", "param": {"siteId": 1, "platformId": 2}, "timestamp": int(time.time() * 1000)},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    j = r.json()
    lst = (j.get("data") or {}).get("hotRankList") or []
    if not lst:
        raise RuntimeError("36kr: empty hotRankList")
    items = []
    for it in lst[:20]:
        tm = it.get("templateMaterial") or {}
        title = tm.get("widgetTitle") or it.get("title", "")
        if not title:
            continue
        items.append({
            "title": title,
            "url": f"https://www.36kr.com/p/{it.get('itemId', '')}",
            "hot": _to_int(tm.get("statRead")),
        })
    return items


def fetch_ithome():
    """IT 之家热榜 - 直连 m 站 HTML,失败 fallback (页面结构常变)"""
    direct_err = None
    try:
        r = requests.get(
            "https://m.ithome.com/rankm/",
            headers=_headers({"Referer": "https://m.ithome.com/"}),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        items = []
        seen = set()
        for m in re.finditer(
            r'<a[^>]+href="(https?://(?:www|m)\.ithome\.com/[^"]*?\d+(?:/\d+)?\.html?)"[^>]*>(.*?)</a>',
            r.text,
            re.DOTALL,
        ):
            url, raw_title = m.group(1), m.group(2)
            if url in seen:
                continue
            title = re.sub(r"<[^>]+>", "", raw_title)
            title = re.sub(r"&[a-z#0-9]+;", " ", title).strip()
            if title and 5 < len(title) < 200:
                seen.add(url)
                items.append({"title": title, "url": url, "hot": 25 - len(items)})
            if len(items) >= 25:
                break
        if not items:
            raise RuntimeError("ithome: HTML 解析 0 条")
        return items
    except Exception as e:
        direct_err = f"{type(e).__name__}: {e}"
    return aggregator_fallback("ithome", "itHome", direct_err)


def fetch_sspai():
    """少数派热门"""
    r = requests.get(
        "https://sspai.com/api/v1/article/tag/page/get?limit=20&offset=0&tag=%E7%83%AD%E9%97%A8%E6%96%87%E7%AB%A0",
        headers=_headers(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    j = r.json()
    lst = j.get("data") or []
    if not lst:
        raise RuntimeError("sspai: empty data")
    return [
        {
            "title": it.get("title", ""),
            "url": f"https://sspai.com/post/{it.get('id', '')}",
            "hot": _to_int(it.get("like_count") or it.get("comment_count")),
        }
        for it in lst[:20]
        if it.get("title")
    ]


# 平台 id → (display name, fetch fn)
PLATFORMS = {
    "zhihu":    ("知乎热榜",   fetch_zhihu),
    "weibo":    ("微博热搜",   fetch_weibo),
    "v2ex":     ("V2EX 热门",  fetch_v2ex),
    "baidu":    ("百度热搜",   fetch_baidu),
    "douyin":   ("抖音热点",   fetch_douyin),
    "bilibili": ("B 站热门",   fetch_bilibili),
    "douban":   ("豆瓣热映",   fetch_douban),
    "hupu":     ("虎扑步行街", fetch_hupu),
    "toutiao":  ("今日头条",   fetch_toutiao),
    "36kr":     ("36 氪热榜",  fetch_36kr),
    "ithome":   ("IT 之家",    fetch_ithome),
    "sspai":    ("少数派",     fetch_sspai),
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_one(pid, name, fn, retries=2):
    """单个平台抓取,带 retry"""
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            items = fn()
            return {
                "ok": True,
                "items": items,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "attempt": attempt + 1,
            }
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 + attempt * 3)  # 2s, 5s
    return {
        "ok": False,
        "error": f"{type(last_err).__name__}: {last_err}",
        "elapsed_ms": int((time.time() - t0) * 1000),
        "attempt": retries + 1,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data.json", help="读取已有 data.json 用于 last-good 兜底")
    ap.add_argument("--output", default="data.json", help="写入新 data.json")
    args = ap.parse_args()

    # 读旧数据
    try:
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if "platforms" not in data:
        data["platforms"] = {}

    # 并发抓取
    print(f"=== 多摸鱼数据抓取 @ {now_iso()} ===", file=sys.stderr)
    success, failed = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_to_id = {
            ex.submit(fetch_one, pid, name, fn): (pid, name)
            for pid, (name, fn) in PLATFORMS.items()
        }
        for future in as_completed(future_to_id):
            pid, name = future_to_id[future]
            r = future.result()
            if r["ok"]:
                data["platforms"][pid] = {
                    "name": name,
                    "updated_at": now_iso(),
                    "items": r["items"][:30],
                }
                success.append((pid, len(r["items"]), r["elapsed_ms"], r["attempt"]))
                print(f"  ✓ {pid}: {len(r['items'])} items [{r['elapsed_ms']}ms, attempt {r['attempt']}]", file=sys.stderr)
            else:
                # 保留旧数据,但记录错误
                old = data["platforms"].get(pid)
                if old:
                    print(f"  ⚠ {pid}: {r['error']} [{r['elapsed_ms']}ms] - 保留 last-good ({old.get('updated_at', '?')})", file=sys.stderr)
                else:
                    # 旧数据也没有,确保字段存在
                    data["platforms"][pid] = {
                        "name": name,
                        "updated_at": None,
                        "items": [],
                        "error": r["error"],
                    }
                    print(f"  ✗ {pid}: {r['error']} [{r['elapsed_ms']}ms] - 没有 last-good", file=sys.stderr)
                failed.append((pid, r["error"]))

    # 元数据
    data["version"] = 1
    data["updated_at"] = now_iso()
    data["summary"] = f"{len(success)}/{len(PLATFORMS)} platforms fresh; {len(failed)} fell back to last-good"

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n=== 完成: {data['summary']} ===", file=sys.stderr)
    print(f"输出: {args.output} ({sum(len(p.get('items', [])) for p in data['platforms'].values())} 条总条目)", file=sys.stderr)


if __name__ == "__main__":
    main()
