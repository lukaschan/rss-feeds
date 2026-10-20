#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全文 RSS 生成器：为没有（完整）RSS 的两个站点生成带全文的订阅源。

  1. pod-exp.pages.dev（Field Notes｜播客中文文稿）
     - 站点没有 RSS；全部文稿元数据在静态文件 assets/catalog-data.js
     - 每篇是静态 HTML，正文在 <article id="text"> 里，可直接提取
  2. bidclub.ai（BidClub）
     - 官方 RSS 只有最新 30 期且内容被截断
     - 但官方提供免 key 的公开 API：/api/feed-index（全部目录）
       与 /api/v1/episodes/{slug}（含 transcript_md 全文）

输出（默认写到 ./site/，这个目录就是要部署到 Cloudflare Pages 的内容）：
  site/pod-exp.xml          pod-exp feed（全部篇目；最新 POD_ITEMS 篇全文，其余摘要）
  site/bidclub.xml          BidClub feed（全部期数；最新 BID_ITEMS 期全文，其余摘要）
  site/bidclub-zh.xml       BidClub 中文 feed（同上，标题/摘要为中文，全文为原文）
  site/index.html           订阅入口页
  site/archive/*.json       两个站的完整目录存档（防止旧条目“丢失”）

只依赖 Python 3.10+ 标准库，无需 pip install。

可选环境变量：
  SITE_URL    部署后的根地址，如 https://rss-xxx.pages.dev
              仅用于在 feed 里写入 atom:self 自引用链接，不填则省略。
  POD_ITEMS   pod-exp feed 收录篇数（默认 30）
  BID_ITEMS   BidClub feed 收录期数（默认 50）
  OUT_DIR     输出目录（默认 site）
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, date
from email.utils import format_datetime
from html.parser import HTMLParser
from xml.sax.saxutils import escape as xml_escape

UA = {"User-Agent": "rss-feeds-generator/1.0 (personal full-text RSS builder)"}
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
POD_ITEMS = int(os.environ.get("POD_ITEMS", "25"))
BID_ITEMS = int(os.environ.get("BID_ITEMS", "30"))
OUT_DIR = os.environ.get("OUT_DIR", "site")
WORKERS = 8


# ---------------------------------------------------------------- HTTP ----
def fetch_bytes(url, retries=2, timeout=40):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - 记录后重试
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}")


def fetch_text(url, **kw):
    return fetch_bytes(url, **kw).decode("utf-8", errors="replace")


def fetch_json(url, **kw):
    return json.loads(fetch_text(url, **kw))


# ------------------------------------------------------------- Markdown ----
def _md_inline(text):
    """处理行内 Markdown：代码、图片、链接、粗体、斜体、删除线。"""
    text = xml_escape(text)
    # 代码段先占位，避免内部符号被二次处理
    codes = []

    def _stash(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash, text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", r'<img src="\2" alt="\1" />', text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)
    text = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<em>\1</em>", text)

    def _unstash(m):
        return f"<code>{codes[int(m.group(1))]}</code>"

    return re.sub(r"\x00(\d+)\x00", _unstash, text)


def md_to_html(md):
    """够用的 Markdown -> HTML 转换：标题/段落/列表/引用/代码块/表格/分隔线。"""
    if not md:
        return ""
    lines = md.replace("\r\n", "\n").split("\n")
    out, para, i = [], [], 0

    def flush_para():
        if para:
            out.append("<p>" + _md_inline(" ".join(s.strip() for s in para)) + "</p>")
            para.clear()

    def is_table_sep(s):
        return bool(re.match(r"^\s*\|?[\s:|-]+\|?\s*$", s)) and "-" in s

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):  # 代码块
            flush_para()
            buf = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + xml_escape("\n".join(buf)) + "</code></pre>")
            continue
        if not stripped:
            flush_para()
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            level = min(len(m.group(1)) + 1, 6)  # 整体降一级，避免和条目标题冲突
            out.append(f"<h{level}>" + _md_inline(m.group(2)) + f"</h{level}>")
            i += 1
            continue
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", stripped):
            flush_para()
            out.append("<hr />")
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and is_table_sep(lines[i + 1]):
            flush_para()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            out.append("<table><thead><tr>" + "".join(
                f"<th>{_md_inline(c)}</th>" for c in header) + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue
        if stripped.startswith(">"):
            flush_para()
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + md_to_html("\n".join(buf)) + "</blockquote>")
            continue
        if re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            flush_para()
            ordered = bool(re.match(r"^\s*\d+[.)]\s+", line))
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(lines) and re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[i]):
                items.append(re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", lines[i]))
                i += 1
                # 列表项的续行（缩进的普通文本）并入上一项
                while i < len(lines) and lines[i].strip() and re.match(r"^\s{2,}\S", lines[i]) \
                        and not re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[i]):
                    items[-1] += " " + lines[i].strip()
                    i += 1
            out.append(f"<{tag}>" + "".join(f"<li>{_md_inline(it)}</li>" for it in items) + f"</{tag}>")
            continue
        para.append(line)
        i += 1
    flush_para()
    return "\n".join(out)


# ----------------------------------------------------------------- RSS ----
def rfc822(dt):
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_datetime(dt.astimezone(timezone.utc))
    if isinstance(dt, date):
        return format_datetime(datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc))
    if isinstance(dt, str):
        try:
            return rfc822(datetime.fromisoformat(dt.replace("Z", "+00:00")))
        except ValueError:
            try:
                y, m, d = (int(x) for x in dt[:10].split("-"))
                return rfc822(date(y, m, d))
            except Exception:  # noqa: BLE001
                return format_datetime(datetime.now(timezone.utc))
    return format_datetime(datetime.now(timezone.utc))


def cdata(text):
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_rss(channel, items):
    """channel: dict(title, link, description, language, self_url?)
       items: list of dict(title, link, guid, pubdate, description, content_html, categories)"""
    now = format_datetime(datetime.now(timezone.utc))
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
             'xmlns:atom="http://www.w3.org/2005/Atom">',
             "<channel>",
             f"<title>{xml_escape(channel['title'])}</title>",
             f"<link>{xml_escape(channel['link'])}</link>",
             f"<description>{xml_escape(channel['description'])}</description>",
             f"<language>{xml_escape(channel.get('language', 'zh-CN'))}</language>",
             f"<lastBuildDate>{now}</lastBuildDate>",
             "<generator>rss-feeds-generator (personal)</generator>"]
    if channel.get("self_url"):
        parts.append(f'<atom:link href="{xml_escape(channel["self_url"])}" rel="self" '
                     'type="application/rss+xml" />')
    if channel.get("image"):
        parts.append(f"<image><url>{xml_escape(channel['image'])}</url>"
                     f"<title>{xml_escape(channel['title'])}</title>"
                     f"<link>{xml_escape(channel['link'])}</link></image>")
    for it in items:
        parts.append("<item>")
        parts.append(f"<title>{xml_escape(it['title'])}</title>")
        parts.append(f"<link>{xml_escape(it['link'])}</link>")
        parts.append(f'<guid isPermaLink="true">{xml_escape(it["guid"])}</guid>')
        parts.append(f"<pubDate>{rfc822(it['pubdate'])}</pubDate>")
        for cat in it.get("categories", []):
            if cat:
                parts.append(f"<category>{xml_escape(str(cat))}</category>")
        desc = it.get("description") or ""
        parts.append(f"<description>{xml_escape(desc[:800])}</description>")
        if it.get("content_html"):
            parts.append(f"<content:encoded>{cdata(it['content_html'])}</content:encoded>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "\n".join(parts) + "\n"


# ------------------------------------------------------- pod-exp 提取 ----
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}
KEEP_TAGS = ("a", "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
             "blockquote", "strong", "em", "b", "i", "code", "pre", "figure",
             "figcaption", "table", "thead", "tbody", "tr", "th", "td",
             "div", "section", "span")
SKIP_TAGS = ("script", "button", "input", "nav", "template", "style", "svg")


class ArticleExtractor(HTMLParser):
    """提取 pod-exp 文章页 <article id="text"> 的正文 HTML（清洗属性、 absolutize 链接）。"""

    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.buf = []
        self.capturing = False
        self.open = 0
        self.done = False
        self.skip_open = None

    def _emit(self, tag, attrs):
        d = dict(attrs)
        ka = {}
        if tag == "a" and d.get("href"):
            href = d["href"]
            if href.startswith("#"):
                href = self.page_url + href
            ka["href"] = href
        if tag == "img" and d.get("src"):
            src = d["src"]
            if src.startswith("./"):
                src = "https://pod-exp.pages.dev/" + src[2:]
            elif src.startswith("/"):
                src = "https://pod-exp.pages.dev" + src
            ka["src"] = src
            ka["alt"] = d.get("alt", "")
        if tag in ("h2", "h3", "h4", "section") and d.get("id"):
            ka["id"] = d["id"]
        self.buf.append("<" + tag + "".join(
            f' {k}="{xml_escape(v, {chr(34): "&quot;"})}"' for k, v in ka.items())
            + (" />" if tag in VOID_TAGS else ">"))

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        d = dict(attrs)
        if not self.capturing:
            if tag == "article" and d.get("id") == "text":
                self.capturing = True
                self.open = 1
            return
        if self.skip_open is not None:
            if tag not in VOID_TAGS:
                self.open += 1
            return
        if tag in SKIP_TAGS or "toolbar" in d.get("class", ""):
            if tag not in VOID_TAGS:
                self.open += 1
                self.skip_open = self.open
            return
        if tag in VOID_TAGS:
            self._emit(tag, attrs)
            return
        self.open += 1
        if tag in KEEP_TAGS:
            self._emit(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        if self.done or not self.capturing or self.skip_open is not None:
            return
        if tag in VOID_TAGS and tag not in SKIP_TAGS:
            self._emit(tag, attrs)

    def handle_endtag(self, tag):
        if self.done or not self.capturing or tag in VOID_TAGS:
            return
        self.open -= 1
        if self.skip_open is not None:
            if self.open < self.skip_open:
                self.skip_open = None
            return
        if self.open == 0:
            self.capturing = False
            self.done = True
            return
        if tag in KEEP_TAGS:
            self.buf.append(f"</{tag}>")

    def handle_data(self, data):
        if self.capturing and self.skip_open is None and not self.done:
            self.buf.append(xml_escape(data))

    def html(self):
        return "".join(self.buf)


def fmt_duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_pod_exp(out_dir):
    base = "https://pod-exp.pages.dev"
    raw = fetch_text(f"{base}/assets/catalog-data.js")
    catalog = json.loads(raw.split("=", 1)[1].strip().rstrip(";"))
    catalog.sort(key=lambda x: (x.get("addedAt") or "", x.get("updatedAt") or ""),
                 reverse=True)
    print(f"[pod-exp] 目录共 {len(catalog)} 篇，取最近 {POD_ITEMS} 篇生成全文 feed")

    # 完整目录存档
    archive = [{
        "id": x["id"], "title": x.get("title", ""), "channel": x.get("channel", ""),
        "guests": x.get("guests", ""), "published": x.get("published", ""),
        "addedAt": x.get("addedAt", ""), "updatedAt": x.get("updatedAt", ""),
        "duration": x.get("duration"), "topics": x.get("topics", []),
        "url": f"{base}/{x['id']}/",
    } for x in catalog]
    os.makedirs(os.path.join(out_dir, "archive"), exist_ok=True)
    with open(os.path.join(out_dir, "archive", "pod-exp-catalog.json"), "w",
              encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=1)

    def make_item(entry):
        url = f"{base}/{entry['id']}/"
        body_html, err = "", None
        try:
            page = fetch_text(url)
            ex = ArticleExtractor(url)
            ex.feed(page)
            body_html = ex.html()
        except Exception as exc:  # noqa: BLE001 - 单篇失败降级为摘要版
            err = str(exc)
        meta_bits = [entry.get("channel", "")]
        if entry.get("guests"):
            meta_bits.append(f"嘉宾：{entry['guests']}")
        if entry.get("host"):
            meta_bits.append(f"主持：{entry['host']}")
        meta_bits.append(f"播出 {entry.get('published', '')}")
        if entry.get("duration"):
            meta_bits.append(f"时长 {fmt_duration(entry['duration'])}")
        chunks = []
        cover = (entry.get("cover") or {}).get("m") or (entry.get("cover") or {}).get("s")
        if cover:
            chunks.append(f'<p><img src="{base}/{entry["id"]}/{cover}" alt="" /></p>')
        chunks.append(f"<p><em>{xml_escape(' · '.join(b for b in meta_bits if b))}</em></p>")
        if entry.get("summary"):
            chunks.append(f"<p>{xml_escape(entry['summary'])}</p>")
        chapters = entry.get("chapters") or []
        if chapters:
            lis = "".join(
                f"<li>{fmt_duration(c.get('t', 0))} {xml_escape(c.get('title', ''))}</li>"
                for c in chapters)
            chunks.append(f"<h2>章节</h2><ul>{lis}</ul>")
        if body_html:
            chunks.append("<hr />")
            chunks.append(body_html)
        elif err:
            chunks.append(f"<p>（本篇全文抓取失败，仅显示摘要：{xml_escape(err)}）</p>")
        return {
            "title": entry.get("title", "(无标题)"),
            "link": url, "guid": url,
            "pubdate": entry.get("addedAt") or entry.get("published") or "",
            "description": entry.get("summary", ""),
            "content_html": "\n".join(chunks),
            "categories": [entry.get("channel", "")] + (entry.get("topics") or []),
        }

    def make_summary_item(entry):
        """较旧的篇目：只放摘要 + 章节 + 原文链接（全文点开原站看），保证单个 feed 装下全部历史。"""
        url = f"{base}/{entry['id']}/"
        chunks = []
        cover = (entry.get("cover") or {}).get("m") or (entry.get("cover") or {}).get("s")
        if cover:
            chunks.append(f'<p><img src="{base}/{entry["id"]}/{cover}" alt="" /></p>')
        meta_bits = [entry.get("channel", ""), f"播出 {entry.get('published', '')}"]
        if entry.get("duration"):
            meta_bits.append(f"时长 {fmt_duration(entry['duration'])}")
        chunks.append(f"<p><em>{xml_escape(' · '.join(b for b in meta_bits if b))}</em></p>")
        if entry.get("summary"):
            chunks.append(f"<p>{xml_escape(entry['summary'])}</p>")
        chapters = entry.get("chapters") or []
        if chapters:
            lis = "".join(
                f"<li>{fmt_duration(c.get('t', 0))} {xml_escape(c.get('title', ''))}</li>"
                for c in chapters)
            chunks.append(f"<h2>章节</h2><ul>{lis}</ul>")
        chunks.append(f'<p>全文：<a href="{url}">在原站阅读本篇全文</a></p>')
        return {
            "title": entry.get("title", "(无标题)"),
            "link": url, "guid": url,
            "pubdate": entry.get("addedAt") or entry.get("published") or "",
            "description": entry.get("summary", ""),
            "content_html": "\n".join(chunks),
            "categories": [entry.get("channel", "")] + (entry.get("topics") or []),
        }

    picked = catalog[:POD_ITEMS]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        full_items = list(pool.map(make_item, picked))
    ok = sum(1 for it in full_items if "<hr />" in it["content_html"])
    print(f"[pod-exp] 全文成功 {ok}/{len(full_items)} 篇；其余 {len(catalog) - len(picked)} 篇为摘要条目")
    items = full_items + [make_summary_item(e) for e in catalog[POD_ITEMS:]]

    rss = build_rss({
        "title": "Field Notes｜播客中文文稿（全文）",
        "link": base + "/",
        "description": "精选深度访谈、播客与技术大会中文文稿（自建 RSS，原站无 RSS）：全部篇目都在这一个 feed 里，最新若干篇带全文，其余为摘要+原文链接。",
        "language": "zh-CN",
        "self_url": (SITE_URL + "/pod-exp.xml") if SITE_URL else "",
    }, items)
    path = os.path.join(out_dir, "pod-exp.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"[pod-exp] 写入 {path}（{len(rss.encode('utf-8')) / 1024:.0f} KB，{len(items)} 条）")
    return len(catalog), len(picked)


# ------------------------------------------------------------- BidClub ----
def build_bidclub(out_dir):
    api = "https://bidclub.ai"
    index = fetch_json(f"{api}/api/feed-index")
    episodes = index.get("episodes", [])
    episodes.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    print(f"[bidclub] 目录共 {len(episodes)} 期，取最近 {BID_ITEMS} 期生成全文 feed")

    archive = [{
        "slug": x["slug"], "title": x.get("title", ""), "title_alt": x.get("title_alt", ""),
        "show_id": x.get("show_id", ""), "show": x.get("show", ""),
        "lang": x.get("lang", ""), "date": x.get("date", ""),
        "published_at": x.get("published_at", ""), "duration_min": x.get("duration_min"),
        "url": f"{api}/e/{x['slug']}",
    } for x in episodes]
    os.makedirs(os.path.join(out_dir, "archive"), exist_ok=True)
    with open(os.path.join(out_dir, "archive", "bidclub-index.json"), "w",
              encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=1)

    picked = episodes[:BID_ITEMS]

    def get_detail(entry):
        try:
            data = fetch_json(f"{api}/api/v1/episodes/{entry['slug']}")
            return data.get("episode", data)
        except Exception as exc:  # noqa: BLE001
            print(f"[bidclub] 详情抓取失败 {entry['slug']}: {exc}")
            return None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        details = list(pool.map(get_detail, picked))

    def people_of(ep):
        names = []
        for chip in ep.get("chips") or []:
            if isinstance(chip, str) and chip.startswith("person:"):
                names.append(chip[len("person:"):])
        return names

    def make_item(entry, detail, zh):
        ep = detail or entry
        lang = (ep.get("lang") or entry.get("lang") or "").upper()
        slug = entry["slug"]
        # zh feed：原语言是英文时用 *_alt（中文）字段；原语言是中文时用主字段
        use_alt = zh and lang == "EN"
        title = (ep.get("title_alt") if use_alt else ep.get("title")) or entry.get("title", "")
        if zh and lang == "ZH":
            title = ep.get("title") or title
        dek = (ep.get("dek_alt") if use_alt else ep.get("dek")) or ""
        tldr = (ep.get("tldr_md_alt") if use_alt else ep.get("tldr_md")) or ""
        digest = (ep.get("digest_md_alt") if use_alt else ep.get("digest_md")) or ""
        transcript = ep.get("transcript_md") or ""
        show_name = ""
        shows = ep.get("shows") or {}
        if isinstance(shows, dict):
            show_name = shows.get("name") or ""
        if not show_name and isinstance(entry.get("show"), str):
            show_name = entry["show"]
        people = people_of(ep)
        date_str = ep.get("date") or entry.get("date") or ""
        dur = ep.get("duration_min") or entry.get("duration_min")
        labels = (
            {"tldr": "要点 TL;DR", "digest": "摘要 Digest",
             "transcript": "全文文稿（原文）",
             "note": "注：BidClub 仅提供原文全文文稿；中文版为标题、要点与摘要的译文。"}
            if zh else
            {"tldr": "TL;DR", "digest": "Digest",
             "transcript": "Full transcript",
             "note": ""}
        )
        chunks = []
        thumb = ep.get("thumbnail_url") or entry.get("thumbnail_url")
        if thumb:
            chunks.append(f'<p><img src="{xml_escape(thumb)}" alt="" /></p>')
        meta = " · ".join(b for b in [
            show_name,
            "、".join(people),
            date_str,
            f"{dur} min" if dur else "",
            f"原语言 {lang}" if lang else "",
        ] if b)
        if meta:
            chunks.append(f"<p><em>{xml_escape(meta)}</em></p>")
        if dek:
            chunks.append(f"<p>{xml_escape(dek)}</p>")
        if detail is None:
            chunks.append("<p>（本期全文抓取失败，仅显示摘要部分。）</p>")
        if tldr:
            chunks.append(f"<h2>{labels['tldr']}</h2>" + md_to_html(tldr))
        if digest:
            chunks.append(f"<h2>{labels['digest']}</h2>" + md_to_html(digest))
        if transcript:
            chunks.append(f"<h2>{labels['transcript']}</h2>" + md_to_html(transcript))
            if zh and labels["note"]:
                chunks.append(f"<p><em>{labels['note']}</em></p>")
        if ep.get("source_url"):
            chunks.append(f'<p>来源：<a href="{xml_escape(ep["source_url"])}">'
                          f'{xml_escape(ep.get("source_label") or ep["source_url"])}</a></p>')
        link = f"{api}/{'zh/' if zh else ''}e/{slug}"
        return {
            "title": title or slug,
            "link": link, "guid": link,
            "pubdate": ep.get("published_at") or entry.get("published_at") or date_str,
            "description": dek,
            "content_html": "\n".join(chunks),
            "categories": [show_name, entry.get("show_id", "")],
        }

    def make_summary_item(entry, zh):
        """较旧的期数：只放 dek 摘要 + 原文链接，保证单个 feed 装下全部 3000+ 期历史。"""
        lang = (entry.get("lang") or "").upper()
        use_alt = zh and lang == "EN"
        title = (entry.get("title_alt") if use_alt else entry.get("title")) or entry.get("title", "")
        if zh and lang == "ZH":
            title = entry.get("title") or title
        dek = (entry.get("dek_alt") if use_alt else entry.get("dek")) or ""
        show_name = entry.get("show") if isinstance(entry.get("show"), str) else ""
        people = people_of(entry)
        dur = entry.get("duration_min")
        chunks = []
        if entry.get("thumbnail_url"):
            chunks.append(f'<p><img src="{xml_escape(entry["thumbnail_url"])}" alt="" /></p>')
        meta = " · ".join(b for b in [
            show_name, "、".join(people), entry.get("date") or "",
            f"{dur} min" if dur else "", f"原语言 {lang}" if lang else "",
        ] if b)
        if meta:
            chunks.append(f"<p><em>{xml_escape(meta)}</em></p>")
        if dek:
            chunks.append(f"<p>{xml_escape(dek)}</p>")
        link = f"{api}/{'zh/' if zh else ''}e/{entry['slug']}"
        open_text = "在 BidClub 打开本期（要点 / 摘要 / 全文）" if zh else "Open on BidClub (TL;DR / digest / full transcript)"
        chunks.append(f'<p><a href="{link}">{open_text}</a></p>')
        return {
            "title": title or entry["slug"],
            "link": link, "guid": link,
            "pubdate": entry.get("published_at") or entry.get("date") or "",
            "description": dek,
            "content_html": "\n".join(chunks),
            "categories": [show_name, entry.get("show_id", "")],
        }

    pairs = list(zip(picked, details))
    rest = episodes[BID_ITEMS:]
    items_main = [make_item(e, d, zh=False) for e, d in pairs] + \
        [make_summary_item(e, zh=False) for e in rest]
    items_zh = [make_item(e, d, zh=True) for e, d in pairs] + \
        [make_summary_item(e, zh=True) for e in rest]
    ok = sum(1 for d in details if d and d.get("transcript_md"))
    print(f"[bidclub] 全文成功 {ok}/{len(pairs)} 期；其余 {len(rest)} 期为摘要条目")

    rss_main = build_rss({
        "title": "BidClub — All shows (Full text)",
        "link": api + "/",
        "description": "BidClub 投资播客文稿（自建 RSS；官方 RSS 仅 30 期且截断）：全部期数都在这一个 feed 里，最新若干期带要点+摘要+全文文稿，其余为摘要+原文链接。",
        "language": "en",
        "self_url": (SITE_URL + "/bidclub.xml") if SITE_URL else "",
    }, items_main)
    with open(os.path.join(out_dir, "bidclub.xml"), "w", encoding="utf-8") as f:
        f.write(rss_main)
    print(f"[bidclub] 写入 bidclub.xml（{len(rss_main.encode('utf-8')) / 1024:.0f} KB）")

    rss_zh = build_rss({
        "title": "BidClub — 全部节目（全文·中文）",
        "link": api + "/",
        "description": "BidClub 投资播客（中文版）：全部期数都在这一个 feed 里，最新若干期带中文要点/摘要+原文全文文稿，其余为中文摘要+原文链接。",
        "language": "zh-CN",
        "self_url": (SITE_URL + "/bidclub-zh.xml") if SITE_URL else "",
    }, items_zh)
    with open(os.path.join(out_dir, "bidclub-zh.xml"), "w", encoding="utf-8") as f:
        f.write(rss_zh)
    print(f"[bidclub] 写入 bidclub-zh.xml（{len(rss_zh.encode('utf-8')) / 1024:.0f} KB）")
    return len(episodes), len(pairs)


# -------------------------------------------------------------- index ----
INDEX_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>全文 RSS 订阅</title>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 640px; margin: 48px auto; padding: 0 20px; line-height: 1.7; color: #222; }
  h1 { font-size: 22px; } h2 { font-size: 17px; margin-top: 32px; }
  code { background: #f2f2f2; padding: 2px 6px; border-radius: 4px; word-break: break-all; }
  .feed { border: 1px solid #e3e3e3; border-radius: 10px; padding: 14px 18px; margin: 12px 0; }
  .feed a { font-weight: 600; }
  small { color: #777; }
</style>
</head>
<body>
<h1>全文 RSS 订阅</h1>
<p>自建全文订阅源，每日自动更新。把下面链接粘贴到你的 RSS 阅读器即可。</p>

<div class="feed">
  <a href="pod-exp.xml">Field Notes｜播客中文文稿（全文）</a><br>
  <code>pod-exp.xml</code><br>
  <small>原站 pod-exp.pages.dev 无 RSS；本 feed 含全部 {pod_total} 篇，其中最新 {pod_items} 篇带全文，其余为摘要 + 原文链接。</small>
</div>

<div class="feed">
  <a href="bidclub.xml">BidClub — All shows (Full text)</a><br>
  <code>bidclub.xml</code><br>
  <small>官方 RSS 仅 30 期且截断；本 feed 含全部 {bid_total} 期，其中最新 {bid_items} 期带要点 + 摘要 + 全文文稿，其余为摘要 + 原文链接。</small>
</div>

<div class="feed">
  <a href="bidclub-zh.xml">BidClub — 全部节目（全文·中文）</a><br>
  <code>bidclub-zh.xml</code><br>
  <small>全部 {bid_total} 期：最新 {bid_items} 期为中文标题 / 要点 / 摘要 + 原文全文文稿，其余为中文摘要 + 原文链接（BidClub 不提供中文全文）。</small>
</div>

<h2>完整目录存档</h2>
<p><a href="archive/pod-exp-catalog.json">pod-exp 全部目录 JSON</a> ·
   <a href="archive/bidclub-index.json">BidClub 全部目录 JSON</a>
   <small>（BidClub 共 {bid_total} 期；旧条目即使滚出 feed 也能在这里找到。）</small></p>

<p><small>生成时间：{built_at}（UTC）。由 generate.py 自动生成。</small></p>
</body>
</html>
"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    pod_total, pod_items = build_pod_exp(OUT_DIR)
    bid_total, bid_items = build_bidclub(OUT_DIR)

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    index_html = (INDEX_TEMPLATE
                  .replace("{pod_items}", str(pod_items))
                  .replace("{pod_total}", str(pod_total))
                  .replace("{bid_items}", str(bid_items))
                  .replace("{bid_total}", str(bid_total))
                  .replace("{built_at}", built_at))
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)

    headers = (
        "/pod-exp.xml\n"
        "  Content-Type: application/rss+xml; charset=utf-8\n"
        "  Cache-Control: public, max-age=1800\n"
        "/bidclub.xml\n"
        "  Content-Type: application/rss+xml; charset=utf-8\n"
        "  Cache-Control: public, max-age=1800\n"
        "/bidclub-zh.xml\n"
        "  Content-Type: application/rss+xml; charset=utf-8\n"
        "  Cache-Control: public, max-age=1800\n"
    )
    with open(os.path.join(OUT_DIR, "_headers"), "w", encoding="utf-8") as f:
        f.write(headers)
    print(f"完成：index.html 与 _headers 已写入 {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
