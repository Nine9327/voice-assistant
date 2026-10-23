"""联网搜索：需要现成资料时，先去网上捞一段再让大模型开口。

本地 7B 模型的知识有截止日期，问今天天气、最新新闻、谁拿了这个奖、股价多少，
它只会一本正经地编。这个模块补上那一环。

后端全走普通 HTTP，不引入新依赖：
    bing    必应。用 cn.bing.com 的 RSS 输出（?format=rss），免 key、国内直连 —— 默认
    tavily  Tavily API，专为喂大模型设计的摘要，需要 key
    bocha   博查，国内服务，需要 key
    searx   自建 SearXNG 的 JSON 接口，需要填实例地址

对外只有三个函数：
    should_search(text)   这句话该不该去网上查
    web_search(query)     查一次，返回结构化结果
    build_context(res)    把结果压成一段塞进提示词的文字
"""
from __future__ import annotations

import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

import config

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# ---------------------------------------------------------------- 触发判断
# 分值制比「命中任一关键词就搜」靠谱得多：口语里「今天」「怎么样」出现得太频繁，
# 单看一个词会把「今天有点累」也送去搜一遍。命中不同类别加分不同，再扣掉闲聊，
# 过阈值才动手。
_VERB = ("查一下", "查查", "搜一下", "搜搜", "搜索", "帮我查", "帮我搜",
         "百度一下", "谷歌一下", "网上查", "网上搜")

# 强时效：答案每天都在变，模型脑子里那点训练数据必然过期
_TIME = ("今天", "今日", "现在", "目前", "最新", "最近", "实时", "刚才",
         "刚刚", "本周", "这周", "本月", "今年", "昨天", "明天", "后天", "昨晚",
         "去年同期", "截至目前", "新一轮")

# 强事实：不查就只能瞎猜的领域
_FACT = ("天气", "气温", "温度", "下雨", "降雨", "台风", "空气质量", "限号",
         "股价", "大盘", "汇率", "油价", "金价", "房价", "市值", "涨了", "跌了",
         "涨停", "跌停", "开盘", "收盘", "比分", "赛果", "赛程", "积分榜",
         "新闻", "热搜", "头条", "票房", "开奖", "航班", "高铁", "路况",
         "发布会", "上市", "召回", "疫情", "地震", "选举", "诺贝尔")

# 问句特征：单独不足以触发，用来加权
_ASK = ("是谁", "哪位", "哪一位", "什么时候", "哪一年", "哪年", "多少", "几个",
        "在哪", "哪里", "为什么", "怎么回事", "怎么样", "是什么", "有没有",
        "是多少", "多久", "多远", "多高", "多大", "排名", "区别")

# 这些问的是「此刻的钟表 / 日历」，本地注入时间就能答，不必联网
_ASK_LOCAL_TIME = ("几点", "几号", "星期几", "礼拜几", "今天是几月", "今天是周几",
                   "现在是什么时候", "日期")

# 闲聊白名单：命中就基本不搜（「你好」这种再去查一遍很蠢，也慢）
_CHITCHAT = ("你好", "您好", "谢谢", "多谢", "再见", "拜拜", "早安", "晚安",
             "你是谁", "你叫什么", "你会什么", "你能做什么", "讲个笑话",
             "唱首歌", "陪我聊", "聊聊天", "无聊", "我累了", "我好累",
             "你觉得", "你喜欢", "我爱你", "抱歉", "对不起", "没关系",
             "在吗", "嗨", "哈喽", "hello", "hi")

# 阈值定 4 而不是 3：单独一个「今天」只值 3 分，否则「今天有点累」也会触发搜索。
# 要让「时效词 / 事实词」里至少凑上一个、或者出现明确的「帮我查一下」才动手。
_SEARCH_THRESHOLD = 4


def should_search(text: str, mode: str | None = None) -> tuple[bool, str]:
    """判断这句话要不要联网。返回 (是否搜索, 给日志看的原因)。"""
    mode = (mode or getattr(config, "SEARCH_MODE", "auto")).lower()
    if mode == "off":
        return False, "联网已关闭"
    if mode == "always":
        return True, "总是搜索"

    q = (text or "").strip()
    if not q:
        return False, "空输入"
    for word in _ASK_LOCAL_TIME:
        if word in q:
            return False, f"问的是本地时间（{word}）"
    if len(q) <= 3:
        return False, "太短"

    score = 0
    hits: list[str] = []
    for word in _VERB:
        if word in q:
            score += 5
            hits.append(f"查({word})")
            break
    for word in _TIME:
        if word in q:
            score += 3
            hits.append(f"时效({word})")
            break
    for word in _FACT:
        if word in q:
            score += 3
            hits.append(f"事实({word})")
            break
    for word in _ASK:
        if word in q:
            score += 1
            hits.append(f"问句({word})")
            break
    for word in _CHITCHAT:
        if word in q:
            score -= 6
            hits.append(f"闲聊({word})")
            break

    if score >= _SEARCH_THRESHOLD:
        return True, f"得分 {score}：" + "、".join(hits)
    return False, f"得分 {score}：" + ("、".join(hits) or "无特征词")


# ---------------------------------------------------------------- 后端实现
class SearchError(RuntimeError):
    pass


def _client(timeout: float) -> httpx.Client:
    # 保留系统代理：Tavily / 博查在部分网络下需要走代理才通。
    # 必应国内站即使配了代理也能正常回，不必特殊处理。
    return httpx.Client(
        timeout=httpx.Timeout(connect=4.0, read=timeout, write=10.0, pool=4.0),
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
    )


def _clean(text: str) -> str:
    """去掉 HTML 标签、压缩空白 —— 搜索接口的摘要里常混着 <b> 高亮标签。"""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _bing(query: str, limit: int, timeout: float) -> tuple[list[dict], str]:
    """必应。用 RSS 而不是抓 HTML：结构稳定、无脚本、也不用担心反爬改版。"""
    url = "https://cn.bing.com/search"
    with _client(timeout) as client:
        resp = client.get(url, params={"q": query, "format": "rss"})
    if resp.status_code != 200:
        raise SearchError(f"必应返回 {resp.status_code}")
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        raise SearchError(f"必应结果解析失败：{exc}") from exc

    out: list[dict] = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title") or "")
        snippet = _clean(item.findtext("description") or "")
        link = (item.findtext("link") or "").strip()
        if not title and not snippet:
            continue
        out.append({"title": title, "url": link, "snippet": snippet})
        if len(out) >= limit:
            break
    return out, ""


def _tavily(query: str, limit: int, timeout: float) -> tuple[list[dict], str]:
    key = getattr(config, "SEARCH_KEY", "") or ""
    if not key:
        raise SearchError("还没填 Tavily 的 API Key")
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": key,
        "query": query,
        "max_results": limit,
        "include_answer": True,
        "search_depth": "basic",
    }
    with _client(timeout) as client:
        resp = client.post(url, json=payload)
    if resp.status_code != 200:
        raise SearchError(f"Tavily 返回 {resp.status_code}：{resp.text[:160]}")
    data = resp.json()
    out = [
        {
            "title": _clean(r.get("title") or ""),
            "url": r.get("url") or "",
            "snippet": _clean(r.get("content") or ""),
        }
        for r in data.get("results") or []
    ][:limit]
    return out, _clean(data.get("answer") or "")


def _bocha(query: str, limit: int, timeout: float) -> tuple[list[dict], str]:
    key = getattr(config, "SEARCH_KEY", "") or ""
    if not key:
        raise SearchError("还没填博查的 API Key")
    url = "https://api.bochaai.com/v1/web-search"
    payload = {"query": query, "summary": True, "count": limit, "freshness": "noLimit"}
    with _client(timeout) as client:
        resp = client.post(url, json=payload, headers={"Authorization": f"Bearer {key}"})
    if resp.status_code != 200:
        raise SearchError(f"博查返回 {resp.status_code}：{resp.text[:160]}")
    body = resp.json()
    if body.get("code") not in (200, None):
        raise SearchError(f"博查返回 {body.get('code')}：{body.get('msg') or ''}")
    pages = ((body.get("data") or {}).get("webPages") or {}).get("value") or []
    out = [
        {
            "title": _clean(p.get("name") or ""),
            "url": p.get("url") or "",
            "snippet": _clean(p.get("summary") or p.get("snippet") or ""),
        }
        for p in pages
    ][:limit]
    return out, ""


def _searx(query: str, limit: int, timeout: float) -> tuple[list[dict], str]:
    base = (getattr(config, "SEARCH_ENDPOINT", "") or "").rstrip("/")
    if not base:
        raise SearchError("还没填 SearXNG 实例地址")
    with _client(timeout) as client:
        resp = client.get(
            f"{base}/search",
            params={"q": query, "format": "json", "language": "zh-CN"},
        )
    if resp.status_code != 200:
        raise SearchError(f"SearXNG 返回 {resp.status_code}")
    out = [
        {
            "title": _clean(r.get("title") or ""),
            "url": r.get("url") or "",
            "snippet": _clean(r.get("content") or ""),
        }
        for r in (resp.json().get("results") or [])
    ][:limit]
    return out, ""


_BACKENDS = {
    "bing": _bing,
    "tavily": _tavily,
    "bocha": _bocha,
    "searx": _searx,
}


# ---------------------------------------------------------------- 对外接口
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 90.0   # 同一句话连着问两遍不必重复查，语音场景很容易发生


def web_search(query: str, limit: int | None = None, timeout: float | None = None) -> dict:
    """查一次。任何失败都以 ok=False 返回，绝不抛异常 —— 搜索挂了不该让对话挂掉。"""
    query = (query or "").strip()
    limit = int(limit or getattr(config, "SEARCH_RESULTS", 5))
    timeout = float(timeout or getattr(config, "SEARCH_TIMEOUT", 6))
    backend = (getattr(config, "SEARCH_BACKEND", "bing") or "bing").lower()
    if not query:
        return {"ok": False, "query": query, "backend": backend, "results": [],
                "answer": "", "error": "查询为空", "ms": 0}

    cache_key = f"{backend}|{limit}|{query}"
    now = time.time()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and now - hit[0] < _CACHE_TTL:
            return dict(hit[1], cached=True)

    fn = _BACKENDS.get(backend)
    t0 = time.perf_counter()
    if fn is None:
        return {"ok": False, "query": query, "backend": backend, "results": [],
                "answer": "", "error": f"未知搜索后端：{backend}", "ms": 0}
    try:
        results, answer = fn(query, limit, timeout)
    except SearchError as exc:
        return {"ok": False, "query": query, "backend": backend, "results": [],
                "answer": "", "error": str(exc),
                "ms": round((time.perf_counter() - t0) * 1000)}
    except httpx.HTTPError as exc:
        return {"ok": False, "query": query, "backend": backend, "results": [],
                "answer": "", "error": f"网络不通：{exc}",
                "ms": round((time.perf_counter() - t0) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "query": query, "backend": backend, "results": [],
                "answer": "", "error": f"搜索出错：{exc}",
                "ms": round((time.perf_counter() - t0) * 1000)}

    payload = {
        "ok": bool(results) or bool(answer),
        "query": query,
        "backend": backend,
        "results": results,
        "answer": answer,
        "error": "" if (results or answer) else "没有查到结果",
        "ms": round((time.perf_counter() - t0) * 1000),
    }
    with _cache_lock:
        _cache[cache_key] = (now, payload)
        if len(_cache) > 64:  # 简单限容，防止长时间运行后无限增长
            for key in list(_cache)[:32]:
                _cache.pop(key, None)
    return payload


def build_context(result: dict, max_chars: int = 1800) -> str:
    """把搜索结果压成一段喂给模型。控制长度：语音场景回答本来就短，资料给多了反而跑题。"""
    if not result.get("ok"):
        return ""
    lines: list[str] = []
    answer = (result.get("answer") or "").strip()
    if answer:
        lines.append(f"摘要：{answer}")
    for i, item in enumerate(result.get("results") or [], 1):
        title = (item.get("title") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        if not title and not snippet:
            continue
        piece = f"{i}. {title}" + (f"：{snippet}" if snippet else "")
        if len(piece) > 420:
            piece = piece[:420] + "…"
        lines.append(piece)
        if sum(len(x) for x in lines) > max_chars:
            break
    text = "\n".join(lines)
    return text[:max_chars]


def test(values: dict | None = None) -> dict:
    """配置页的「测一下」：用当前（或未保存的）配置查一次，返回原始结果。"""
    values = values or {}
    saved = {}
    touched = [k for k in ("SEARCH_BACKEND", "SEARCH_KEY", "SEARCH_ENDPOINT",
                           "SEARCH_RESULTS", "SEARCH_TIMEOUT") if k in values]
    for key in touched:  # 临时套用，查完立刻还原，不污染运行中的配置
        saved[key] = getattr(config, key, None)
        setattr(config, key, values[key])
    try:
        return web_search(values.get("q") or "今天的热点新闻")
    finally:
        for key, old in saved.items():
            setattr(config, key, old)
