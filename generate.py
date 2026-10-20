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

  3. lex.qiaomu.ai（Lex Fridman 播客中文精读）
     - 官方 RSS 只有最新 20 篇；全站篇目在 sitemap.xml（/ep/{slug}/）
     - 每篇另有官方 Markdown 版 /ep/{slug}/index.md（精读全文），抓取后
       缓存在 archive/lex-articles.json，历史文章不重复抓取，每日只抓新增

输出分两组：存档版（冻结后不再更新）与每日新增版（滚动更新）。
每个源有一份基线清单（冻结时的全部条目 ID，存 *-baseline.json）；
每日运行时以「当前目录 − 基线」得到新增，只写新增版 feed。
  site/pod-exp.xml          pod-exp 存档（冻结时全部篇目；最新 POD_ITEMS 篇全文，其余摘要）
  site/pod-exp-new.xml      pod-exp 每日新增（基线之后的新篇，全部全文）
  feeds/bidclub.xml         BidClub 存档（冻结时全部期数；2026 年以来带要点+摘要不截断，其余摘要）
  feeds/bidclub-zh.xml      BidClub 中文存档（同上，标题/摘要为中文）；
                            两者各 30+ MB 超过 Pages 上限，放仓库根 feeds/ 用 GitHub raw 托管；
                            详情增量缓存在 archive/bidclub-details.json（仓库根，不进 site/）
  feeds/bidclub-new.xml     BidClub 每日新增（新期全部带要点+摘要不截断；raw 托管）
  feeds/bidclub-zh-new.xml  BidClub 每日新增中文版
  site/lex.xml              Lex 存档（冻结时全部篇目；每篇封面图+中文精读全文+文末英文原稿链接）
  site/lex-new.xml          Lex 每日新增（同上规格，只含基线之后的新篇）
  site/index.html           订阅入口页
  site/archive/*.json       完整目录存档 / 正文缓存 / 冻结基线

只依赖 Python 3.10+ 标准库，无需 pip install。

可选环境变量：
  SITE_URL    部署后的根地址，如 https://rss-xxx.pages.dev
              仅用于在 feed 里写入 atom:self 自引用链接，不填则省略。
  POD_ITEMS   pod-exp feed 收录篇数（默认 30）
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
BID_DETAIL_SINCE = "2026-01-01"  # BidClub 自该日期起的期数带要点+摘要（全量，不截断）
RAW_BASE = "https://raw.githubusercontent.com/lukaschan/rss-feeds/main"  # 大文件 feed 的托管地址
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
        if it.get("enclosure"):
            parts.append(f'<enclosure url="{xml_escape(it["enclosure"])}" '
                         'type="image/jpeg" length="0" />')
        if it.get("content_html"):
            parts.append(f"<content:encoded>{cdata(it['content_html'])}</content:encoded>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "\n".join(parts) + "\n"


# -------------------------------------------------- 冻结基线（存档） ----
def _load_id_set(path):
    """读取冻结基线（一组条目 ID）；文件不存在返回 None（= 尚未冻结）。"""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:  # noqa: BLE001 - 基线损坏则当作未冻结，重新冻结
            return None
    return None


def _save_id_set(path, ids):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False)


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

    baseline_path = os.path.join(out_dir, "archive", "pod-exp-baseline.json")
    baseline = _load_id_set(baseline_path)
    if baseline is None:
        # 冻结日：生成最后一版存档 feed，并把当前目录记为基线，此后不再更新存档。
        picked = catalog[:POD_ITEMS]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            full_items = list(pool.map(make_item, picked))
        ok = sum(1 for it in full_items if "<hr />" in it["content_html"])
        print(f"[pod-exp] 全文成功 {ok}/{len(full_items)} 篇；其余 {len(catalog) - len(picked)} 篇为摘要条目")
        items = full_items + [make_summary_item(e) for e in catalog[POD_ITEMS:]]
        rss = build_rss({
            "title": "Field Notes｜播客中文文稿（全文·存档）",
            "link": base + "/",
            "description": "精选深度访谈、播客与技术大会中文文稿（自建 RSS，原站无 RSS）：存档版，含冻结时全部篇目（最新若干篇带全文，其余为摘要+原文链接），此后不再更新；新文章见 pod-exp-new.xml。",
            "language": "zh-CN",
            "self_url": (SITE_URL + "/pod-exp.xml") if SITE_URL else "",
        }, items)
        path = os.path.join(out_dir, "pod-exp.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(rss)
        print(f"[pod-exp] 写入存档 {path}（{len(rss.encode('utf-8')) / 1024:.0f} KB，{len(items)} 条）并冻结")
        _save_id_set(baseline_path, [x["id"] for x in catalog])
        new_items = []
    else:
        fresh = [e for e in catalog if e["id"] not in baseline]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            new_items = list(pool.map(make_item, fresh))
        print(f"[pod-exp] 基线后新增 {len(fresh)} 篇（全部全文）")

    rss_new = build_rss({
        "title": "Field Notes｜播客中文文稿（每日新增·全文）",
        "link": base + "/",
        "description": "Field Notes 每日新增文章（自建 RSS）：存档冻结后新发表的篇目，全部带全文；冻结前的全部篇目见 pod-exp.xml 存档版。",
        "language": "zh-CN",
        "self_url": (SITE_URL + "/pod-exp-new.xml") if SITE_URL else "",
    }, new_items)
    path_new = os.path.join(out_dir, "pod-exp-new.xml")
    with open(path_new, "w", encoding="utf-8") as f:
        f.write(rss_new)
    print(f"[pod-exp] 写入 {path_new}（{len(rss_new.encode('utf-8')) / 1024:.0f} KB，{len(new_items)} 条）")
    return len(catalog), len(new_items)


# ------------------------------------------------------------- BidClub ----
def build_bidclub(out_dir, root_dir):
    api = "https://bidclub.ai"
    index = fetch_json(f"{api}/api/feed-index")
    episodes = index.get("episodes", [])
    episodes.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    print(f"[bidclub] 目录共 {len(episodes)} 期；{BID_DETAIL_SINCE} 以来的带要点+摘要（增量缓存）")

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

    baseline_path = os.path.join(root_dir, "archive", "bidclub-baseline.json")
    baseline = _load_id_set(baseline_path)

    # 详情增量缓存放在仓库根 archive/（不进 site/，避免大文件拖垮 Pages 部署）：
    # 已抓过的期数不重抓，每日只补新期，抓完与旧的合并组装。
    cache_path = os.path.join(root_dir, "archive", "bidclub-details.json")
    details_cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                details_cache = json.load(f)
        except Exception:  # noqa: BLE001 - 缓存损坏则当作空缓存重抓
            details_cache = {}

    def ep_date(e):
        return (e.get("published_at") or e.get("date") or "")[:10]

    if baseline is None:
        # 冻结日：2026 年以来的全部期数都要详情（组装存档版）
        wanted = [e for e in episodes if ep_date(e) >= BID_DETAIL_SINCE]
        print(f"[bidclub] 冻结模式：{BID_DETAIL_SINCE} 以来共 {len(wanted)} 期需要详情")
    else:
        # 日常：只需要基线之后新增期数的详情
        wanted = [e for e in episodes if e["slug"] not in baseline]
        print(f"[bidclub] 基线后新增 {len(wanted)} 期")
    missing = [e for e in wanted if e["slug"] not in details_cache]
    print(f"[bidclub] 详情已缓存 {len(wanted) - len(missing)} 期，本次新抓 {len(missing)} 期")

    def get_detail(entry):
        try:
            data = fetch_json(f"{api}/api/v1/episodes/{entry['slug']}")
            ep = data.get("episode", data)
            # 文稿字段体积巨大且本 feed 不用，不进缓存
            return entry["slug"], {k: v for k, v in ep.items()
                                    if k not in ("transcript_md", "transcript")}
        except Exception as exc:  # noqa: BLE001 - 单期失败下次运行再补
            print(f"[bidclub] 详情抓取失败 {entry['slug']}: {exc}")
            return entry["slug"], None

    if missing:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for slug, ep in pool.map(get_detail, missing):
                if ep:
                    details_cache[slug] = ep
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(details_cache, f, ensure_ascii=False)
        got = sum(1 for e in missing if e["slug"] in details_cache)
        print(f"[bidclub] 新抓成功 {got}/{len(missing)} 期，详情缓存共 {len(details_cache)} 期")

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
            {"tldr": "要点 TL;DR", "digest": "摘要 Digest"}
            if zh else
            {"tldr": "TL;DR", "digest": "Digest"}
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
            chunks.append("<p>（本期详情抓取失败，仅显示摘要部分。）</p>")
        if tldr:
            chunks.append(f"<h2>{labels['tldr']}</h2>" + md_to_html(tldr))
        if digest:
            chunks.append(f"<h2>{labels['digest']}</h2>" + md_to_html(digest))
        if ep.get("source_url"):
            chunks.append(f'<p>来源：<a href="{xml_escape(ep["source_url"])}">'
                          f'{xml_escape(ep.get("source_label") or ep["source_url"])}</a></p>')
        link = f"{api}/{'zh/' if zh else ''}e/{slug}"
        chunks.append(f'<p>{"原文链接" if zh else "Read on BidClub"}: '
                      f'<a href="{link}">{link}</a></p>')
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

    def assemble(entries):
        m, z, det = [], [], 0
        for e in entries:
            detail = details_cache.get(e["slug"])
            if detail and (detail.get("tldr_md") or detail.get("digest_md")):
                m.append(make_item(e, detail, zh=False))
                z.append(make_item(e, detail, zh=True))
                det += 1
            else:
                m.append(make_summary_item(e, zh=False))
                z.append(make_summary_item(e, zh=True))
        return m, z, det

    # 两个存档 feed 各 30+ MB，超过 Cloudflare Pages 单文件 25 MB 上限，
    # 放仓库根 feeds/ 目录、用 GitHub raw 地址订阅（不进 site/）。
    feeds_dir = os.path.join(root_dir, "feeds")
    os.makedirs(feeds_dir, exist_ok=True)

    if baseline is None:
        # 冻结日：组装最后一版存档 feed（全部期数），并记录基线，此后不再更新存档。
        items_main, items_zh, detailed = assemble(episodes)
        print(f"[bidclub] 存档版带要点+摘要 {detailed} 期；其余 {len(episodes) - detailed} 期为摘要条目")
        rss_main = build_rss({
            "title": "BidClub — All shows (TL;DR + Digest) [Archive]",
            "link": api + "/",
            "description": f"BidClub 投资播客文稿存档版（自建 RSS；官方 RSS 仅 30 期且截断）：含冻结时全部期数，{BID_DETAIL_SINCE} 以来的带要点+摘要（不截断，原文链接在条目末尾），以前的为摘要+原文链接。此后不再更新；新节目见 bidclub-new.xml。",
            "language": "en",
            "self_url": RAW_BASE + "/feeds/bidclub.xml",
        }, items_main)
        with open(os.path.join(feeds_dir, "bidclub.xml"), "w", encoding="utf-8") as f:
            f.write(rss_main)
        print(f"[bidclub] 写入存档 feeds/bidclub.xml（{len(rss_main.encode('utf-8')) / 1024:.0f} KB）并冻结")
        rss_zh = build_rss({
            "title": "BidClub — 全部节目（中文）[存档]",
            "link": api + "/",
            "description": f"BidClub 投资播客中文存档版：含冻结时全部期数，{BID_DETAIL_SINCE} 以来的带中文要点+摘要（不截断，原文链接在条目末尾），以前的为中文摘要+原文链接。此后不再更新；新节目见 bidclub-zh-new.xml。",
            "language": "zh-CN",
            "self_url": RAW_BASE + "/feeds/bidclub-zh.xml",
        }, items_zh)
        with open(os.path.join(feeds_dir, "bidclub-zh.xml"), "w", encoding="utf-8") as f:
            f.write(rss_zh)
        print(f"[bidclub] 写入存档 feeds/bidclub-zh.xml（{len(rss_zh.encode('utf-8')) / 1024:.0f} KB）并冻结")
        _save_id_set(baseline_path, [e["slug"] for e in episodes])
        new_main, new_zh = [], []
    else:
        fresh = [e for e in episodes if e["slug"] not in baseline]
        new_main, new_zh, det_new = assemble(fresh)
        print(f"[bidclub] 新增 {len(fresh)} 期，其中带要点+摘要 {det_new} 期")

    rss_new = build_rss({
        "title": "BidClub — New episodes (TL;DR + Digest)",
        "link": api + "/",
        "description": "BidClub 每日新增节目（自建 RSS）：存档冻结后新出的期数，全部带要点+摘要（不截断，原文链接在条目末尾）；冻结前的全部期数见 bidclub.xml 存档版。",
        "language": "en",
        "self_url": RAW_BASE + "/feeds/bidclub-new.xml",
    }, new_main)
    with open(os.path.join(feeds_dir, "bidclub-new.xml"), "w", encoding="utf-8") as f:
        f.write(rss_new)
    print(f"[bidclub] 写入 feeds/bidclub-new.xml（{len(rss_new.encode('utf-8')) / 1024:.0f} KB）")
    rss_new_zh = build_rss({
        "title": "BidClub — 每日新增（中文）",
        "link": api + "/",
        "description": "BidClub 每日新增节目（中文版）：存档冻结后新出的期数，全部带中文要点+摘要（不截断，原文链接在条目末尾）；冻结前的全部期数见 bidclub-zh.xml 存档版。",
        "language": "zh-CN",
        "self_url": RAW_BASE + "/feeds/bidclub-zh-new.xml",
    }, new_zh)
    with open(os.path.join(feeds_dir, "bidclub-zh-new.xml"), "w", encoding="utf-8") as f:
        f.write(rss_new_zh)
    print(f"[bidclub] 写入 feeds/bidclub-zh-new.xml（{len(rss_new_zh.encode('utf-8')) / 1024:.0f} KB）")
    return len(episodes), len(new_main)


# ---------------------------------------------------- Lex 中文精读 ----
LEX_BASE = "https://lex.qiaomu.ai"


def parse_lex_md(md):
    """解析单篇 /ep/{slug}/index.md：标题、摘要、元信息、正文 Markdown。

    结构固定为：
      # 标题
      > 摘要（一句话导读）
      - 原标题 / 期数 / 发布日期 / 嘉宾 / 主题 / 网页版 / 官方页面 / 精读字数
      > 免责声明
      正文……
    """
    lines = md.replace("\r\n", "\n").split("\n")
    title, summary, meta = "", "", {}
    body_start = None
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("# ") and not title:
            title = s[2:].strip()
            i += 1
            continue
        m = re.match(r"^-\s*([^：:]+)[：:]\s*(.*)$", s)
        if m and body_start is None:
            meta[m.group(1).strip()] = m.group(2).strip()
            i += 1
            continue
        if s.startswith(">"):
            quote = s.lstrip("> ").strip()
            if "AI 改写" in quote or "非官方" in quote:
                body_start = i + 1  # 免责声明之后就是正文
                break
            if not summary and not meta:
                summary = quote
            i += 1
            continue
        i += 1
    if body_start is None:  # 兜底：跳过元信息区，余下皆正文
        body_start = 0
        while body_start < len(lines) and (
                not lines[body_start].strip()
                or lines[body_start].strip().startswith(("# ", ">", "- "))):
            body_start += 1
    body_md = "\n".join(lines[body_start:]).strip()
    num = ""
    m = re.search(r"#\s*(\d+)", meta.get("原标题", ""))
    if m:
        num = m.group(1)
    else:
        m = re.search(r"第\s*(\d+)\s*期", meta.get("期数", ""))
        if m:
            num = m.group(1)
    return {
        "title": title,
        "num": num,
        "summary": summary,
        "date": meta.get("发布日期", ""),
        "guest": meta.get("嘉宾 / 主题", ""),
        "page": meta.get("网页版", ""),
        "official": meta.get("官方页面", ""),
        "body_md": body_md,
    }


def _lex_transcript_paras(text, target=1200):
    """英文文稿是一整块无分段纯文本：按句末标点切句，再攒成约 target 字的段落。"""
    sentences = re.findall(r"[^.!?]+[.!?]+[\"'”’]?\s*|[^.!?]+$", text)
    paras, buf = [], ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(buf) + len(s) > target and buf:
            paras.append(buf)
            buf = s
        else:
            buf = f"{buf} {s}" if buf else s
    if buf:
        paras.append(buf)
    return paras


def parse_lex_html_fallback(html_text, slug):
    """个别早期节目没有 Markdown 精读版（index.md 返回 404），页面里只有
    英文文稿（div.transcript-text 纯文本）。把文稿切段做成正文兜底收录。"""
    tr = re.search(r'<div class="transcript-text"[^>]*>(.*?)</div>', html_text, re.S)
    if not tr:
        return None
    raw = re.sub(r"<[^>]+>", "", tr.group(1))
    from html import unescape as _unesc
    raw = _unesc(raw).strip()
    if len(raw) < 1000:
        return None
    og = ""
    m = re.search(r'property="og:title" content="([^"]+)"', html_text)
    if m:
        og = _unesc(m.group(1))
    zh_side = og.split("｜")[0].strip()
    num_m = re.match(r"#\s*(\d+)\s*(.*)", zh_side)
    num = num_m.group(1) if num_m else ""
    title = (num_m.group(2).strip() if num_m else zh_side) or slug
    official = ""
    for link in re.findall(r'href="(https://lexfridman\.com/[^"]*)"', html_text):
        if link.rstrip("/") != "https://lexfridman.com/podcast":
            official = link
            break
    date_m = re.search(r'"datePublished":\s*"([0-9-]{10})', html_text)
    desc_m = re.search(r'name="description" content="([^"]+)"', html_text)
    note = ("<p><em>本期站方未提供中文精读，以下为英文文稿"
            "（早期节目的自动转录存档，可能存在识别错误）。</em></p>")
    body_html = note + "\n".join(
        f"<p>{xml_escape(p)}</p>" for p in _lex_transcript_paras(raw))
    return {
        "title": title, "num": num,
        "summary": _unesc(desc_m.group(1)) if desc_m else "",
        "date": date_m.group(1) if date_m else "",
        "guest": "", "page": f"{LEX_BASE}/ep/{slug}/",
        "official": official, "body_md": "", "body_html": body_html,
    }


def _http_status(url, timeout=40):
    """只看状态码（404 用来区分『没有 Markdown 版』与临时抓取失败）。"""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001
        return 0


def build_lex(out_dir):
    """全量 feed：sitemap 里全部 /ep/ 文章；正文（官方 Markdown 精读）增量缓存
    在 archive/lex-articles.json —— 历史文章只抓一次，每日只抓新增。"""
    archive_dir = os.path.join(out_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    cache_path = os.path.join(archive_dir, "lex-articles.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:  # noqa: BLE001 - 缓存损坏则当作空缓存重抓
            cache = {}

    slugs = []
    try:
        sitemap = fetch_text(f"{LEX_BASE}/sitemap.xml", retries=4, timeout=90)
        for loc in re.findall(r"<loc>([^<]+)</loc>", sitemap):
            m = re.match(r"https://lex\.qiaomu\.ai/ep/([^/]+)/?$", loc.strip())
            if m and m.group(1) not in slugs:
                slugs.append(m.group(1))
    except Exception as exc:  # noqa: BLE001 - 站点偶发超时：有缓存就先用缓存出 feed，下次再补新篇
        print(f"[lex] sitemap 抓取失败（{exc}），回退到缓存目录 {len(cache)} 篇")
    if not slugs:
        slugs = sorted(cache.keys())
    print(f"[lex] sitemap 共 {len(slugs)} 篇")
    missing = [s for s in slugs if s not in cache]
    print(f"[lex] 已缓存 {len(cache)} 篇，本次需新抓 {len(missing)} 篇")

    def grab(slug):
        md_url = f"{LEX_BASE}/ep/{slug}/index.md"
        try:
            md = fetch_text(md_url, retries=3)
            rec = parse_lex_md(md)
            if not rec["title"] or not rec["body_md"]:
                return slug, None
            return slug, rec
        except Exception as exc:  # noqa: BLE001
            # 只有确认 index.md 不存在（404）才走英文文稿兜底；
            # 超时等临时失败留待下次运行重试 Markdown 版。
            if _http_status(md_url) == 404:
                try:
                    page = fetch_text(f"{LEX_BASE}/ep/{slug}/", retries=3)
                    rec = parse_lex_html_fallback(page, slug)
                    if rec:
                        print(f"[lex] {slug} 无 Markdown 版，已用英文文稿兜底")
                        return slug, rec
                except Exception as exc2:  # noqa: BLE001
                    print(f"[lex] 兜底抓取失败 {slug}: {exc2}")
                    return slug, None
            print(f"[lex] 抓取失败 {slug}: {exc}")
            return slug, None

    if missing:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for slug, rec in pool.map(grab, missing):
                if rec:
                    cache[slug] = rec
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        got = sum(1 for s in missing if s in cache)
        print(f"[lex] 新抓成功 {got}/{len(missing)} 篇，缓存共 {len(cache)} 篇")

    def make_item(slug):
        rec = cache[slug]
        page = rec.get("page") or f"{LEX_BASE}/ep/{slug}/"
        cover = f"{LEX_BASE}/og/{slug}.jpg"
        chunks = [f'<p><img src="{cover}" alt="" /></p>']
        meta = " · ".join(b for b in [
            rec.get("guest", ""),
            f"第 {rec['num']} 期" if rec.get("num") else "",
            rec.get("date", ""),
        ] if b)
        if meta:
            chunks.append(f"<p><em>{xml_escape(meta)}</em></p>")
        if rec.get("summary"):
            chunks.append(f"<p>{xml_escape(rec['summary'])}</p>")
        chunks.append(rec["body_html"] if rec.get("body_html")
                      else md_to_html(rec["body_md"]))
        footer = []
        if rec.get("official"):
            footer.append(f'英文原稿（Lex Fridman 官方页面）：<a href="{xml_escape(rec["official"])}">'
                          f'{xml_escape(rec["official"])}</a>')
        footer.append(f'中文精读网页版：<a href="{page}">{page}</a>')
        chunks.append("<hr />\n<p>" + "<br />".join(footer) + "</p>")
        title = f"#{rec['num']} {rec['title']}" if rec.get("num") else rec["title"]
        return {
            "title": title,
            "link": page, "guid": page,
            "pubdate": rec.get("date", ""),
            "description": rec.get("summary", ""),
            "content_html": "\n".join(chunks),
            "enclosure": cover,
            "categories": ["Lex Fridman"],
        }

    baseline_path = os.path.join(out_dir, "archive", "lex-baseline.json")
    baseline = _load_id_set(baseline_path)
    have = [s for s in slugs if s in cache]
    have.sort(key=lambda s: cache[s].get("date") or "", reverse=True)
    if baseline is None:
        # 冻结日：生成最后一版存档 feed，并把当前 sitemap 记为基线，此后不再更新存档。
        items = [make_item(s) for s in have]
        rss = build_rss({
            "title": "Lex Fridman 播客｜中文精读（全文·存档）",
            "link": LEX_BASE + "/",
            "description": "Lex Fridman 播客中文精读存档版（自建 RSS；官方 feed 仅最新 20 篇）：含冻结时全部文章，每篇带封面图与中文精读全文，文末附英文原稿链接。此后不再更新；新文章见 lex-new.xml。",
            "language": "zh-CN",
            "self_url": (SITE_URL + "/lex.xml") if SITE_URL else "",
        }, items)
        path = os.path.join(out_dir, "lex.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(rss)
        print(f"[lex] 写入存档 {path}（{len(rss.encode('utf-8')) / 1024:.0f} KB，{len(items)} 条）并冻结")
        _save_id_set(baseline_path, slugs)
        baseline = set(slugs)
        new_items = []
        archive_total = len(items)
    else:
        fresh = [s for s in have if s not in baseline]
        new_items = [make_item(s) for s in fresh]
        archive_total = len(baseline)
        print(f"[lex] 基线后新增 {len(fresh)} 篇")

    rss_new = build_rss({
        "title": "Lex Fridman 播客｜中文精读（每日新增）",
        "link": LEX_BASE + "/",
        "description": "Lex Fridman 播客每日新增中文精读（自建 RSS）：存档冻结后新发表的文章，每篇带封面图与中文精读全文，文末附英文原稿链接；冻结前的全部文章见 lex.xml 存档版。",
        "language": "zh-CN",
        "self_url": (SITE_URL + "/lex-new.xml") if SITE_URL else "",
    }, new_items)
    path_new = os.path.join(out_dir, "lex-new.xml")
    with open(path_new, "w", encoding="utf-8") as f:
        f.write(rss_new)
    print(f"[lex] 写入 {path_new}（{len(rss_new.encode('utf-8')) / 1024:.0f} KB，{len(new_items)} 条）")
    return archive_total, len(new_items)


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
<p>自建全文订阅源。日常追更请订阅「每日更新」区；「存量存档」是一次性冻结的大文件（几十 MB），订一次、读完即可，不会再变。</p>

<h2>每日更新</h2>

<div class="feed">
  <a href="pod-exp-new.xml">Field Notes｜播客中文文稿（每日新增·全文）</a><br>
  <code>pod-exp-new.xml</code><br>
  <small>存档冻结后新发表的篇目，全部带全文；目前已收录新增 {pod_new} 篇，持续更新。</small>
</div>

<div class="feed">
  <a href="https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub-new.xml">BidClub — New episodes (TL;DR + Digest)</a><br>
  <code>https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub-new.xml</code><br>
  <small>冻结后新出的期数，全部带要点 + 摘要（不截断），末尾附原文链接；目前已收录新增 {bid_new} 期，持续更新。托管于 GitHub raw。</small>
</div>

<div class="feed">
  <a href="https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub-zh-new.xml">BidClub — 每日新增（中文）</a><br>
  <code>https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub-zh-new.xml</code><br>
  <small>同上，中文标题 / 要点 / 摘要；目前已收录新增 {bid_new} 期。</small>
</div>

<div class="feed">
  <a href="lex-new.xml">Lex Fridman 播客｜中文精读（每日新增）</a><br>
  <code>lex-new.xml</code><br>
  <small>冻结后新发表的文章，每篇带封面图与中文精读全文，文末附英文原稿链接；目前已收录新增 {lex_new} 篇，持续更新。</small>
</div>

<h2>存量存档（已冻结，不再更新）</h2>

<div class="feed">
  <a href="pod-exp.xml">Field Notes｜播客中文文稿（全文·存档）</a><br>
  <code>pod-exp.xml</code><br>
  <small>含冻结时全部 {pod_total} 篇（最新若干篇带全文，其余为摘要 + 原文链接）。</small>
</div>

<div class="feed">
  <a href="https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub.xml">BidClub — All shows (TL;DR + Digest) [Archive]</a><br>
  <code>https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub.xml</code><br>
  <small>含冻结时全部 {bid_total} 期：2026 年以来的带要点 + 摘要（不截断），末尾附原文链接，其余为摘要 + 原文链接。文件约 37 MB，托管于 GitHub raw。</small>
</div>

<div class="feed">
  <a href="https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub-zh.xml">BidClub — 全部节目（中文）[存档]</a><br>
  <code>https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub-zh.xml</code><br>
  <small>同上，中文版。文件约 36 MB，托管于 GitHub raw。</small>
</div>

<div class="feed">
  <a href="lex.xml">Lex Fridman 播客｜中文精读（全文·存档）</a><br>
  <code>lex.xml</code><br>
  <small>含冻结时全部 {lex_total} 篇，每篇带封面图与中文精读全文，文末附英文原稿链接。</small>
</div>

<h2>完整目录存档</h2>
<p><a href="archive/pod-exp-catalog.json">pod-exp 全部目录 JSON</a> ·
   <a href="archive/bidclub-index.json">BidClub 全部目录 JSON</a> ·
   <a href="archive/lex-articles.json">Lex 精读正文缓存 JSON</a>
   <small>（BidClub 共 {bid_total} 期；旧条目即使滚出 feed 也能在这里找到。）</small></p>

<p><small>生成时间：{built_at}（UTC）。由 generate.py 自动生成。</small></p>
</body>
</html>
"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    pod_total, pod_new = build_pod_exp(OUT_DIR)
    bid_total, bid_new = build_bidclub(OUT_DIR, os.path.dirname(os.path.abspath(OUT_DIR)))
    lex_total, lex_new = build_lex(OUT_DIR)

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    index_html = (INDEX_TEMPLATE
                  .replace("{pod_new}", str(pod_new))
                  .replace("{pod_total}", str(pod_total))
                  .replace("{bid_new}", str(bid_new))
                  .replace("{bid_total}", str(bid_total))
                  .replace("{lex_total}", str(lex_total))
                  .replace("{lex_new}", str(lex_new))
                  .replace("{built_at}", built_at))
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)

    headers = (
        "/pod-exp.xml\n"
        "  Content-Type: application/rss+xml; charset=utf-8\n"
        "  Cache-Control: public, max-age=1800\n"
        "/pod-exp-new.xml\n"
        "  Content-Type: application/rss+xml; charset=utf-8\n"
        "  Cache-Control: public, max-age=1800\n"
        "/lex.xml\n"
        "  Content-Type: application/rss+xml; charset=utf-8\n"
        "  Cache-Control: public, max-age=1800\n"
        "/lex-new.xml\n"
        "  Content-Type: application/rss+xml; charset=utf-8\n"
        "  Cache-Control: public, max-age=1800\n"
    )
    with open(os.path.join(OUT_DIR, "_headers"), "w", encoding="utf-8") as f:
        f.write(headers)
    print(f"完成：index.html 与 _headers 已写入 {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
