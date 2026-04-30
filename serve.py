#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多摸鱼 · 本地服务器 + 热榜 API 反向代理 (v2)
=================================================================
启动:
    cd /path/to/多摸鱼
    python3 serve.py            # 默认 8000 端口
    python3 serve.py 8765       # 指定端口
    python3 serve.py 8000 -v    # 详细模式 (打印响应体预览)

打开:
    http://localhost:8000/多摸鱼.html

诊断:
    http://localhost:8000/api/health           # 网络/DNS 自检
    http://localhost:8000/api/debug/zhihu      # 逐源探测某平台,返回完整诊断 JSON

改动 (v2):
    1. 详细日志:每次上游请求都输出 DNS / 连接 / HTTP / 耗时 / 失败原因
    2. /api/debug/{platform} 接口:把全部上游都探一遍,返回结构化诊断
    3. 多上游扩展:imsyy / vvhan / V2EX 直连 (V2EX 官方接口更稳)
    4. 错误分类:DNS 失败 / 连接拒绝 / 超时 / 4xx / 5xx / 解析失败 区别清楚

要求: Python 3.7+
"""

import http.server
import socketserver
import socket
import urllib.request
import urllib.error
import urllib.parse
import json
import os
import sys
import time
import threading
import ssl

# =========================================================================
# 配置
# =========================================================================

PORT = 8000
DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_TTL = 120
TIMEOUT = 8
VERBOSE = False  # -v 打开

# 平台 id 映射 (统一 key → 各源各自 id)
# 列里值若为 None 表示该源不支持此平台
PLATFORMS = {
    # key            imsyy            vvhan          v2ex(直连)
    "zhihu":        ("zhihu",         "zhihuHot",    None),
    "weibo":        ("weibo",         "wbHot",       None),
    "v2ex":         ("v2ex",          "v2ex",        "self"),  # self = 用 V2EX 官方 API
    "baidu":        ("baidu",         "baiduRY",     None),
    "douyin":       ("douyin",        "douyinHot",   None),
    "bilibili":     ("bilibili",      "bili",        None),
    "douban":       ("douban-movie",  "douban",      None),
    "hupu":         ("hupu",          "huPu",        None),
    "toutiao":      ("toutiao",       "toutiao",     None),
    "36kr":         ("36kr",          "36Ke",        None),
    "ithome":       ("ithome",        "itHome",      None),
    "sspai":        ("sspai",         "sspai",       None),
    # 兼容前端直接传源风格 id
    "zhihuHot":     ("zhihu",         "zhihuHot",    None),
    "wbHot":        ("weibo",         "wbHot",       None),
    "baiduRY":      ("baidu",         "baiduRY",     None),
    "douyinHot":    ("douyin",        "douyinHot",   None),
    "bili":         ("bilibili",      "bili",        None),
    "douban-movie": ("douban-movie",  "douban",      None),
    "huPu":         ("hupu",          "huPu",        None),
    "36Ke":         ("36kr",          "36Ke",        None),
    "itHome":       ("ithome",        "itHome",      None),
}

# 各源的 URL 构造方式 (函数式,便于扩展)
UPSTREAMS = {
    "imsyy": {
        "host": "api-hot.imsyy.top",
        "url":  lambda pid: f"https://api-hot.imsyy.top/{pid}",
    },
    "vvhan": {
        "host": "api.vvhan.com",
        "url":  lambda pid: f"https://api.vvhan.com/api/hotlist?type={pid}",
    },
    "v2ex_self": {
        "host": "www.v2ex.com",
        "url":  lambda pid: "https://www.v2ex.com/api/topics/hot.json",
    },
}

# auto 模式优先级
AUTO_ORDER = ["imsyy", "vvhan", "v2ex_self"]

# =========================================================================
# 日志
# =========================================================================

COLOR = sys.stderr.isatty()
def c(code, txt):
    return f"\033[{code}m{txt}\033[0m" if COLOR else txt
def green(s): return c("32", s)
def red(s):   return c("31", s)
def yellow(s):return c("33", s)
def blue(s):  return c("36", s)
def gray(s):  return c("90", s)
def bold(s):  return c("1", s)

def log(msg):
    sys.stderr.write(f"{gray(time.strftime('%H:%M:%S'))} {msg}\n")
    sys.stderr.flush()

def log_indent(msg):
    sys.stderr.write(f"           {msg}\n")
    sys.stderr.flush()

# =========================================================================
# 缓存
# =========================================================================

_cache = {}
_cache_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        v = _cache.get(key)
        if v and v[0] > time.time():
            return v[1], v[2]
    return None, None

def cache_set(key, body, source):
    with _cache_lock:
        _cache[key] = (time.time() + CACHE_TTL, body, source)

# =========================================================================
# 上游请求 (带详细诊断)
# =========================================================================

class FetchResult:
    __slots__ = ("ok", "status", "body", "ct", "elapsed_ms", "error_kind", "error_msg", "url")
    def __init__(self):
        self.ok = False
        self.status = 0
        self.body = b""
        self.ct = ""
        self.elapsed_ms = 0
        self.error_kind = ""    # dns / connect / timeout / http / parse / unknown
        self.error_msg = ""
        self.url = ""

    def to_dict(self):
        d = {
            "ok": self.ok,
            "url": self.url,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "content_type": self.ct,
            "error_kind": self.error_kind,
            "error_msg": self.error_msg,
        }
        if self.body:
            try:
                preview = self.body[:240].decode("utf-8", errors="replace")
            except Exception:
                preview = repr(self.body[:240])
            d["body_preview"] = preview
            d["body_size"] = len(self.body)
        return d


def categorize_error(e):
    """把异常归类成易懂的一档"""
    if isinstance(e, urllib.error.HTTPError):
        return "http", f"HTTP {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        reason = e.reason
        if isinstance(reason, socket.gaierror):
            return "dns", f"DNS resolution failed: {reason}"
        if isinstance(reason, socket.timeout):
            return "timeout", "connect timeout"
        if isinstance(reason, ConnectionRefusedError):
            return "connect", "connection refused"
        if isinstance(reason, ssl.SSLError):
            return "ssl", f"TLS error: {reason}"
        return "connect", f"{type(reason).__name__}: {reason}"
    if isinstance(e, socket.timeout):
        return "timeout", "read timeout"
    return "unknown", f"{type(e).__name__}: {e}"


def fetch_upstream(url, label="?"):
    """发起 HTTP 请求,返回 FetchResult,带详细日志"""
    res = FetchResult()
    res.url = url
    t0 = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0 Safari/537.36 momoyu-proxy/2.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
            res.body = body
            res.ct = resp.headers.get("Content-Type", "")
            res.status = resp.status
            res.elapsed_ms = int((time.time() - t0) * 1000)
            res.ok = (200 <= resp.status < 300)
            return res
    except urllib.error.HTTPError as e:
        res.status = e.code
        try: res.body = e.read()[:1024]
        except Exception: pass
    except Exception as e:
        res.error_kind, res.error_msg = categorize_error(e)
        res.elapsed_ms = int((time.time() - t0) * 1000)
        return res
    res.elapsed_ms = int((time.time() - t0) * 1000)
    if not res.error_kind:
        res.error_kind = "http"
        res.error_msg = f"HTTP {res.status}"
    return res


def validate_payload(body, source):
    """校验响应是否是有效热榜数据"""
    try:
        j = json.loads(body)
    except Exception as e:
        return False, f"JSON parse error: {e}"

    if source == "v2ex_self":
        # V2EX 官方返回 array
        if isinstance(j, list) and j:
            return True, f"v2ex array, {len(j)} items"
        return False, "v2ex: not a non-empty array"

    if not isinstance(j, dict):
        return False, "response root is not an object"
    if source == "vvhan" and j.get("success") is False:
        return False, f"vvhan success=false, message={j.get('message','?')}"
    data = j.get("data")
    if not isinstance(data, list) or not data:
        return False, f"data field missing or empty (keys={list(j.keys())[:6]})"
    return True, f"{source} ok, {len(data)} items"


def normalize_v2ex(body):
    """把 V2EX 官方接口转成 imsyy 风格 {data: [{title, url, hot}, ...]}"""
    j = json.loads(body)
    items = []
    for it in j[:20]:
        items.append({
            "title": it.get("title", ""),
            "url":   it.get("url", "https://www.v2ex.com/"),
            "hot":   it.get("replies", 0),
        })
    return json.dumps({"code": 200, "name": "V2EX", "data": items}, ensure_ascii=False).encode("utf-8")


# =========================================================================
# 探测 (debug 接口用)
# =========================================================================

def probe(platform):
    """对所有上游探测一次,返回完整诊断"""
    if platform not in PLATFORMS:
        return {"error": f"unknown platform: {platform}",
                "available": sorted(set(k for k in PLATFORMS))}
    imsyy_id, vvhan_id, v2ex_self = PLATFORMS[platform]
    results = []

    if imsyy_id:
        url = UPSTREAMS["imsyy"]["url"](imsyy_id)
        log(f"   {blue('probe')} imsyy → {url}")
        r = fetch_upstream(url, "imsyy")
        ok, why = (False, "skipped") if not r.ok else validate_payload(r.body, "imsyy")
        d = r.to_dict(); d["source"] = "imsyy"; d["payload_ok"] = ok; d["payload_msg"] = why
        results.append(d)
        log_status("imsyy", r, ok, why)

    if vvhan_id:
        url = UPSTREAMS["vvhan"]["url"](vvhan_id)
        log(f"   {blue('probe')} vvhan → {url}")
        r = fetch_upstream(url, "vvhan")
        ok, why = (False, "skipped") if not r.ok else validate_payload(r.body, "vvhan")
        d = r.to_dict(); d["source"] = "vvhan"; d["payload_ok"] = ok; d["payload_msg"] = why
        results.append(d)
        log_status("vvhan", r, ok, why)

    if v2ex_self:
        url = UPSTREAMS["v2ex_self"]["url"](None)
        log(f"   {blue('probe')} v2ex_self → {url}")
        r = fetch_upstream(url, "v2ex_self")
        ok, why = (False, "skipped") if not r.ok else validate_payload(r.body, "v2ex_self")
        d = r.to_dict(); d["source"] = "v2ex_self"; d["payload_ok"] = ok; d["payload_msg"] = why
        results.append(d)
        log_status("v2ex_self", r, ok, why)

    return {
        "platform": platform,
        "tried": results,
        "summary": _summary(results),
    }


def _summary(results):
    ok = [r for r in results if r["ok"] and r.get("payload_ok")]
    if ok:
        return f"✓ {len(ok)}/{len(results)} 个上游正常,首选 {ok[0]['source']}"
    return f"✗ 0/{len(results)} 个上游可用; 详情看 'tried' 数组"


def log_status(name, r, payload_ok, payload_msg):
    if r.ok and payload_ok:
        log_indent(green(f"✓ {name}: HTTP {r.status} [{r.elapsed_ms}ms] {payload_msg}"))
    elif r.ok and not payload_ok:
        log_indent(yellow(f"⚠ {name}: HTTP {r.status} [{r.elapsed_ms}ms] but {payload_msg}"))
        if VERBOSE and r.body:
            preview = r.body[:300].decode("utf-8", errors="replace").replace("\n", " ")
            log_indent(gray(f"  body: {preview!r}"))
    else:
        kind = r.error_kind or f"http_{r.status}"
        msg = r.error_msg or f"HTTP {r.status}"
        log_indent(red(f"✗ {name}: [{kind}] {msg} [{r.elapsed_ms}ms]"))
        if VERBOSE and r.body:
            preview = r.body[:300].decode("utf-8", errors="replace").replace("\n", " ")
            log_indent(gray(f"  body: {preview!r}"))


# =========================================================================
# /api/* 主入口
# =========================================================================

def handle_api(path):
    """
    路由:
        /api/health
        /api/debug/{platform}
        /api/{source}/{platform}    source ∈ auto|imsyy|vvhan|v2ex_self
    """
    parts = [p for p in path.strip("/").split("/") if p]

    # /api/health
    if len(parts) == 2 and parts[1] == "health":
        return _health_check()

    # /api/debug/{platform}
    if len(parts) == 3 and parts[1] == "debug":
        log(bold(f"DEBUG probe → {parts[2]}"))
        result = probe(parts[2])
        body = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
        return 200, body, "application/json; charset=utf-8", "debug"

    # /api/{source}/{platform}
    if len(parts) >= 3:
        source = parts[1]
        platform = "/".join(parts[2:])
        return _do_proxy(source, platform)

    err = json.dumps({
        "error": "bad request",
        "usage": [
            "/api/auto/{platform}",
            "/api/{imsyy|vvhan|v2ex_self}/{platform}",
            "/api/debug/{platform}",
            "/api/health",
        ],
    }, ensure_ascii=False).encode()
    return 400, err, "application/json; charset=utf-8", "-"


def _health_check():
    """快速测试 DNS + 连通性"""
    log(bold("HEALTH check"))
    out = {"hosts": {}}
    for name, info in UPSTREAMS.items():
        host = info["host"]
        t0 = time.time()
        try:
            ip = socket.gethostbyname(host)
            ms = int((time.time() - t0) * 1000)
            out["hosts"][host] = {"ok": True, "ip": ip, "elapsed_ms": ms}
            log_indent(green(f"✓ {host} → {ip} [{ms}ms]"))
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            out["hosts"][host] = {"ok": False, "error": str(e), "elapsed_ms": ms}
            log_indent(red(f"✗ {host}: {e} [{ms}ms]"))
    body = json.dumps(out, ensure_ascii=False, indent=2).encode()
    return 200, body, "application/json; charset=utf-8", "health"


def _do_proxy(source, platform):
    cache_key = f"{source}:{platform}"
    body, src_used = cache_get(cache_key)
    if body:
        log(f"{blue('CACHE')}  {source}/{platform} → {src_used}")
        return 200, body, "application/json; charset=utf-8", f"{src_used}(cache)"

    if platform not in PLATFORMS:
        err = json.dumps({"error": f"unknown platform: {platform}"}, ensure_ascii=False).encode()
        return 400, err, "application/json; charset=utf-8", "-"

    imsyy_id, vvhan_id, v2ex_self_flag = PLATFORMS[platform]

    if source == "auto":
        order = []
        if imsyy_id:       order.append(("imsyy", imsyy_id))
        if vvhan_id:       order.append(("vvhan", vvhan_id))
        if v2ex_self_flag: order.append(("v2ex_self", None))
    elif source == "imsyy" and imsyy_id:
        order = [("imsyy", imsyy_id)]
    elif source == "vvhan" and vvhan_id:
        order = [("vvhan", vvhan_id)]
    elif source == "v2ex_self" and v2ex_self_flag:
        order = [("v2ex_self", None)]
    else:
        err = json.dumps({"error": f"source {source} 不支持平台 {platform}"}, ensure_ascii=False).encode()
        return 400, err, "application/json; charset=utf-8", "-"

    log(bold(f"PROXY  {source}/{platform}"))

    last_diag = []
    for src_name, pid in order:
        url = UPSTREAMS[src_name]["url"](pid)
        r = fetch_upstream(url, src_name)

        # 日志 + 校验
        if r.ok:
            ok, why = validate_payload(r.body, src_name)
        else:
            ok, why = False, "skipped (request failed)"
        log_status(src_name, r, ok, why)

        # 记录诊断
        d = r.to_dict(); d["source"] = src_name; d["payload_ok"] = ok; d["payload_msg"] = why
        last_diag.append(d)

        if r.ok and ok:
            body = r.body
            if src_name == "v2ex_self":
                try:
                    body = normalize_v2ex(body)
                except Exception as e:
                    log_indent(red(f"  ⚠ v2ex normalize 失败: {e}"))
                    continue
            cache_set(cache_key, body, src_name)
            return 200, body, "application/json; charset=utf-8", src_name

    # 全部失败
    err = json.dumps({
        "error": "all upstreams failed",
        "platform": platform,
        "tried": last_diag,
        "hint": "试试 /api/debug/" + platform + " 看完整诊断,或 /api/health 自检 DNS/网络",
    }, ensure_ascii=False, indent=2).encode("utf-8")
    log_indent(red(f"⛔ {platform} 全部上游失败"))
    return 502, err, "application/json; charset=utf-8", "-"


# =========================================================================
# HTTP Handler
# =========================================================================

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def log_message(self, fmt, *args):
        # 默认日志已经够用,只在 verbose 时打全
        if VERBOSE:
            sys.stderr.write(f"{gray(time.strftime('%H:%M:%S'))} {self.address_string()} - {fmt % args}\n")

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/"):
            try:
                status, body, ct, src = handle_api(self.path)
                self.send_response(status)
                self.send_header("Content-Type", ct)
                self.send_header("X-Source", src)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except BrokenPipeError:
                pass
            except Exception as e:
                log_indent(red(f"handler error: {e}"))
                try:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode())
                except Exception:
                    pass
            return
        return super().do_GET()


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# =========================================================================
# Entry
# =========================================================================

def find_html():
    for n in ("多摸鱼.html", "index.html"):
        if os.path.exists(os.path.join(DIR, n)):
            return n
    return None


def main():
    global PORT, VERBOSE
    args = sys.argv[1:]
    if "-v" in args or "--verbose" in args:
        VERBOSE = True
        args = [a for a in args if a not in ("-v", "--verbose")]
    if args:
        try:
            PORT = int(args[0])
        except ValueError:
            print(red(f"❌ 端口必须是数字,而不是 {args[0]!r}"))
            sys.exit(1)

    html = find_html()
    if not html:
        print(yellow(f"⚠️  当前目录 ({DIR}) 没找到 多摸鱼.html 或 index.html"))

    try:
        with ReusableTCPServer(("127.0.0.1", PORT), Handler) as httpd:
            url = f"http://localhost:{PORT}/{urllib.parse.quote(html) if html else ''}"
            print(bold("=" * 64))
            print(bold(f"🐟 多摸鱼本地服务器 v2"))
            print(f"  📁 静态目录: {DIR}")
            print(f"  🌐 访问页面: {blue(url)}")
            print(f"  🔬 诊断接口: {blue(f'http://localhost:{PORT}/api/debug/zhihu')}")
            print(f"  🩺 网络自检: {blue(f'http://localhost:{PORT}/api/health')}")
            print(f"  📡 上游缓存: {CACHE_TTL}s")
            print(f"  📝 详细模式: {'on' if VERBOSE else 'off (用 -v 打开,会打印响应体预览)'}")
            print(f"  ⏹  停止服务: Ctrl+C")
            print(bold("=" * 64))
            httpd.serve_forever()
    except OSError as e:
        if e.errno in (48, 98):
            print(red(f"❌ 端口 {PORT} 已被占用,换个端口: python3 serve.py 8765"))
        else:
            print(red(f"❌ 启动失败: {e}"))
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n👋 再见")


if __name__ == "__main__":
    main()
