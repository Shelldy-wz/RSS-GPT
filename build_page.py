#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 RSS-GPT 生成的分源 xml 合并成一个直接能读的首页 docs/index.html。

用法：在仓库根目录执行  python build_page.py
依赖：feedparser（requirements.txt 里已有），其余全部标准库。

设计原则：
1. 只读 docs/*.xml，不改 main.py 的逻辑 —— 页面生成失败也不影响订阅源本身
2. 单个源出错就跳过，不让整页挂掉
3. 输出纯静态 HTML，零外部依赖，手机能看
"""

import os
import re
import html
import datetime
import configparser

import feedparser

BASE = "docs/"
CFG = "config.ini"
PER_SOURCE = 10          # 每个源最多显示几条

config = configparser.ConfigParser()
config.read(CFG)


def get_cfg(sec, name, default=None):
    value = config.get(sec, name, fallback=default)
    return value.strip('"') if value else value

EXCERPT_LEN = 150        # 没有可用摘要时显示多少字原文

# 源的中文显示名（没配就用 name 本身）
NICE = {
    "google-search": "Google 搜索中心",
    "sej": "Search Engine Journal",
    "seroundtable": "Search Engine Roundtable",
    "ahrefs": "Ahrefs 博客",
    "semrush": "Semrush 博客",
    "hackernews": "Hacker News 前排",
    "appinn": "小众软件",
}

# 这些源只给标题，不给正文摘录（原始 feed 里没有正文，摘录出来只有 "Points: 89"）
TITLE_ONLY = {"hackernews"}

COLORS = ["#3b82f6", "#8b5cf6", "#f59e0b", "#10b981", "#ef4444", "#06b6d4", "#ec4899"]

# ---------------------------------------------------------------- 脏摘要检测
# DeepSeek 偶尔不干活，而是把提示词复述一遍，或抱怨拿不到正文。
# 这类输出必须丢掉，否则页面上会赫然写着"请补充有效链接"。
ECHO_MARKERS = (
    "200字", "200 字", "分要点", "按顺序", "按你要求", "关键字",
    "关键词：", "关键词:", "html line break", "output a summary",
    "in the following format", "you must keep", "bitte",
    "<br", "无法", "请补充", "请把", "抱歉", "未提供", "没有看到",
    "请提供", "as an ai", "i cannot", "i can't",
)


def looks_like_echo(text):
    """判断这段文字是不是模型在复述指令/报错，而不是真摘要"""
    if not text:
        return True
    low = text.lower()
    for marker in ECHO_MARKERS:
        if marker.lower() in low:
            return True
    # 太短的正文摘要不可信
    if len(text) < 15:
        return True
    return False


def clean_tags(tags):
    """关键词标签要短、要像词；出现整句英文或超长串就整体丢弃"""
    if not tags:
        return []
    for t in tags:
        if len(t) > 20:
            return []
        if re.search(r"[A-Za-z]{4,}\s+[A-Za-z]{4,}", t):
            return []
        if "#" in t or ":" in t or "：" in t:
            return []
    return tags


# ---------------------------------------------------------------- 文本处理
def strip_tags(t):
    """HTML → 纯文本"""
    if not t:
        return ""
    t = re.sub(r"<script[\s\S]*?</script>", " ", t, flags=re.I)
    t = re.sub(r"<style[\s\S]*?</style>", " ", t, flags=re.I)
    t = re.sub(r"<br\s*/?>", " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def split_summary(block):
    """
    LLM 输出格式：<关键词，分号分隔><br><br>总结:<要点1>...
    → (关键词字符串, 总结正文)。注意不要删掉开头的 "1."，否则句子断头。
    """
    if not block:
        return "", ""
    s = block.strip()

    m = re.search(r"(?:<br\s*/?>\s*)+(?:总结|Summary)\s*[:：]?", s, flags=re.I)
    if m:
        kw_raw, body_raw = s[: m.start()], s[m.end():]
    else:
        m2 = re.search(r"(?:总结|Summary)\s*[:：]", s, flags=re.I)
        if not m2:
            return "", strip_tags(s)
        kw_raw, body_raw = s[: m2.start()], s[m2.end():]

    kw = strip_tags(kw_raw)
    kw = re.sub(r"^(?:关键词|Keywords?|关键字)\s*[:：]\s*", "", kw, flags=re.I).strip()
    kw = kw.lstrip("# ").strip()
    body = strip_tags(body_raw)
    return kw.strip(" ；;|，,"), body


def split_block(content):
    """content:encoded → (摘要块, 正文)；模板写作 '<div> 摘要 <div> 正文'"""
    if not content:
        return "", ""
    s = content.lstrip()
    if s.startswith("<div>"):
        i = s.find("<div>", 5)
        if i != -1:
            return s[5:i], s[i + 5:]
    return "", s


def kw_tags(kw):
    parts = [p.strip() for p in re.split(r"[；;、,，|/]", kw) if p.strip()]
    return clean_tags(parts[:6])


def fmt_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                d = datetime.datetime(*st[:6])
                if d.year == datetime.datetime.utcnow().year:
                    return d.strftime("%m-%d")
                return d.strftime("%Y-%m-%d")
            except Exception:
                pass
    return ""


# ---------------------------------------------------------------- 读取
def load_source(name):
    """读 docs/{name}.xml，返回已清洗并截断的条目列表"""
    path = os.path.join(BASE, name + ".xml")
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        feed = feedparser.parse(f.read())

    out = []
    for entry in feed.entries:
        link = (entry.get("link") or "").strip()
        title = strip_tags(entry.get("title", "")) or "(无标题)"
        if not link:
            continue

        content = ""
        if entry.get("content"):
            try:
                content = entry.content[0].value
            except Exception:
                content = ""
        if not content:
            content = entry.get("summary", "") or entry.get("description", "")

        block, article = split_block(content)
        kw, summary = split_summary(block)

        # 模型复述指令 / 抱怨没正文 → 当没有摘要
        if looks_like_echo(summary):
            summary = ""
            kw = ""
        tags = kw_tags(kw)

        excerpt = ""
        if not summary and name not in TITLE_ONLY:
            plain = strip_tags(article)
            if len(plain) > 12:
                excerpt = plain[:EXCERPT_LEN] + ("…" if len(plain) > EXCERPT_LEN else "")

        out.append(
            {
                "title": title,
                "link": link,
                "date": fmt_date(entry),
                "tags": tags,
                "summary": summary,
                "excerpt": excerpt,
                "has_sum": bool(summary),
            }
        )

    return out[:PER_SOURCE]


# ---------------------------------------------------------------- 渲染
CSS = """
*{box-sizing:border-box}
body{margin:0;padding:0;background:#f7f7f8;color:#1a1a1a;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
 line-height:1.7;-webkit-font-smoothing:antialiased}
.wrap{max-width:820px;margin:0 auto;padding:28px 20px 60px}
header h1{font-size:26px;margin:0 0 6px;letter-spacing:-.3px}
header .meta{color:#888;font-size:13px;margin:0 0 18px}
nav{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:30px}
nav a{font-size:12.5px;padding:5px 11px;border-radius:20px;background:#fff;border:1px solid #e5e5e7;
 color:#444;text-decoration:none;white-space:nowrap}
nav a:hover{background:#f0f0f2;border-color:#d4d4d8}
section{margin-bottom:38px;scroll-margin-top:20px}
h2{font-size:15px;margin:0 0 14px;display:flex;align-items:center;gap:8px;color:#111;font-weight:600}
h2 .dot{width:8px;height:8px;border-radius:50%;flex:none}
h2 .count{font-size:12px;color:#999;font-weight:400}
.card{background:#fff;border:1px solid #e8e8ea;border-radius:10px;padding:15px 17px;
 margin-bottom:10px;transition:border-color .15s}
.card:hover{border-color:#d0d0d5}
.card h3{font-size:15.5px;margin:0 0 7px;line-height:1.45;font-weight:600}
.card h3 a{color:#111;text-decoration:none}
.card h3 a:hover{color:#2563eb}
.card .info{font-size:12px;color:#999;margin-bottom:9px;display:flex;flex-wrap:wrap;
 gap:8px;align-items:center}
.card .info time{font-variant-numeric:tabular-nums}
.tag{background:#f2f3f5;color:#555;border-radius:4px;padding:1px 6px;font-size:11.5px}
.sum{margin:0;font-size:14px;color:#333}
.exc{margin:0;font-size:13.5px;color:#8a8a8a}
footer{margin-top:50px;padding-top:18px;border-top:1px solid #e8e8ea;
 font-size:12px;color:#aaa;line-height:2}
footer a{color:#888;text-decoration:none}
footer a:hover{text-decoration:underline}
@media(max-width:600px){
 .wrap{padding:20px 14px 44px}
 header h1{font-size:22px}
 .card{padding:13px 14px}
}
"""

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SEO 每日摘要 · {date}</title>
<style>{css}</style>
</head>
<body>
<div class="wrap">
<header>
<h1>SEO 每日摘要</h1>
<p class="meta">{stamp} 更新 · {nsrc} 个源 · {nitem} 条，其中 {nsum} 条带摘要</p>
<nav>{nav}</nav>
</header>
<main>
{body}
</main>
<footer>由 RSS-GPT 抓取 + DeepSeek 摘要自动生成 · 每天北京时间 09:00 更新</footer>
</div>
</body>
</html>
"""


def esc(t):
    return html.escape(t or "", quote=True)


def build():
    sections = [s for s in config.sections() if s != "cfg"]

    blocks, anchors = [], []
    total_items = total_sum = 0

    for idx, sec in enumerate(sections):
        name = get_cfg(sec, "name")
        if not name:
            continue
        try:
            items = load_source(name)
        except Exception as e:
            print("skip %s: %s" % (name, e))
            continue
        if not items:
            print("empty %s" % name)
            continue

        total_items += len(items)
        total_sum += sum(1 for i in items if i["has_sum"])

        title = NICE.get(name, name)
        color = COLORS[idx % len(COLORS)]
        anchor = "s%d" % (idx + 1)
        anchors.append('<a href="#%s">%s</a>' % (anchor, esc(title)))

        cards = []
        for it in items:
            tags = "".join('<span class="tag">%s</span>' % esc(t) for t in it["tags"])
            if it["has_sum"]:
                body = '<p class="sum">%s</p>' % esc(it["summary"])
            elif it["excerpt"]:
                body = '<p class="exc">%s</p>' % esc(it["excerpt"])
            else:
                body = ""
            cards.append(
                '<article class="card"><h3><a href="%s" target="_blank" rel="noopener">%s</a></h3>'
                '<div class="info">%s%s</div>%s</article>'
                % (
                    esc(it["link"]),
                    esc(it["title"]),
                    ('<time>%s</time>' % esc(it["date"])) if it["date"] else "",
                    tags,
                    body,
                )
            )

        blocks.append(
            '<section id="%s"><h2><span class="dot" style="background:%s"></span>%s'
            '<span class="count">%d 条</span></h2>%s</section>'
            % (anchor, color, esc(title), len(items), "".join(cards))
        )

    page = PAGE.format(
        date=datetime.date.today().strftime("%Y-%m-%d"),
        css=CSS,
        stamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        nsrc=len(blocks),
        nitem=total_items,
        nsum=total_sum,
        nav="".join(anchors),
        body="\n".join(blocks),
    )

    out = os.path.join(BASE, "index.html")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(tmp, out)
    print("wrote %s (%d bytes, %d sources, %d items, %d summarized)"
          % (out, len(page.encode("utf-8")), len(blocks), total_items, total_sum))


if __name__ == "__main__":
    build()
