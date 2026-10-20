# 全文 RSS 订阅（Cloudflare Pages）

为两个「订阅不友好」的站点生成带全文的 RSS：

| Feed | 来源 | 说明 |
|---|---|---|
| **每日更新（追更用）** | | |
| `pod-exp-new.xml` | [pod-exp.pages.dev](https://pod-exp.pages.dev) | 存档冻结后新发表的篇目，**全部带全文**；每日更新 |
| `feeds/bidclub-new.xml`（GitHub raw） | [bidclub.ai](https://bidclub.ai) | 冻结后新出的期数，全部带**要点 TL;DR + 摘要 Digest**（不截断，末尾附原文链接）；每日更新。地址：`https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub-new.xml` |
| `feeds/bidclub-zh-new.xml`（GitHub raw） | [bidclub.ai](https://bidclub.ai) | 同上，中文版：`https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub-zh-new.xml` |
| `lex-new.xml` | [lex.qiaomu.ai](https://lex.qiaomu.ai) | 冻结后新发表的文章，每篇带**封面图 + 中文精读全文**，文末附英文原稿链接；每日更新 |
| **存量存档（已冻结，不再更新）** | | |
| `pod-exp.xml` | [pod-exp.pages.dev](https://pod-exp.pages.dev) | 冻结时**全部 220 篇**：最新 25 篇带全文，其余为摘要 + 原文链接 |
| `feeds/bidclub.xml`（GitHub raw） | [bidclub.ai](https://bidclub.ai) | 冻结时**全部 3385 期**：2026 年以来的带要点 + 摘要（不截断），其余为摘要 + 原文链接。约 37 MB，地址：`https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub.xml` |
| `feeds/bidclub-zh.xml`（GitHub raw） | [bidclub.ai](https://bidclub.ai) | 同上，中文版：`https://raw.githubusercontent.com/lukaschan/rss-feeds/main/feeds/bidclub-zh.xml` |
| `lex.xml` | [lex.qiaomu.ai](https://lex.qiaomu.ai) | 冻结时**全部 506 篇**中文精读全文 + 封面图 |

每个源有一份冻结基线清单（`*-baseline.json`）；每日运行以「当前目录 − 基线」得到新增，只更新「每日更新」组，存档文件不再改动。

`archive/` 下还有两个站点的**完整目录 JSON 存档**（pod-exp 全部 220 篇、BidClub 全部 3385 期），旧条目即使滚出 feed 也不会丢失。

> 为什么 BidClub 条目不放全文文稿？很多 RSS 应用对 XML 大小有限制；要点 + 摘要已能覆盖日常阅读，全文文稿请点条目末尾的原文链接打开。

## 托管步骤（Cloudflare Pages）

任选 A 或 B。成品 `site/` 目录已经生成好，部署后地址形如
`https://你的项目名.pages.dev/pod-exp.xml`。

### A. 最快：直接上传（无需 Git）

1. 下载 `rss-feeds-site.zip`（本包内已附），解压后里面是 `index.html`、`pod-exp.xml`、`lex.xml`、`archive/` 等。
2. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com) → 左侧 **Workers & Pages** → **Create** → **Pages** → **Upload assets**。
3. 项目名随便取（如 `my-fulltext-rss`）→ 把解压后的**文件**（不是外层文件夹）拖进上传框 → **Deploy site**。
4. 打开 `https://my-fulltext-rss.pages.dev/` 确认页面能打开，再把 `pod-exp.xml` 等三个 feed 地址粘贴进你的 RSS 阅读器。
5. 后续更新：本地跑 `python3 generate.py`（或用下面的 B 方案），重新上传一次。

### B. 自动更新：GitHub 仓库 + Action（推荐）

1. 在 GitHub 新建仓库（如 `rss-feeds`），把本项目全部文件上传（`generate.py`、`README.md`、`.github/workflows/update-feeds.yml`、`site/` 等）。本地有 git 的话：`git init && git add -A && git commit -m init && git push`；没有 git 也可用网页上传 ZIP 后解压上传。
2. （可选但建议）在仓库 **Settings → Secrets and variables → Actions → Variables** 里新建变量 `SITE_URL`，值为你的 Pages 地址，如 `https://my-fulltext-rss.pages.dev`。不填也能用，只是 feed 里的自引用链接会省略。
3. Cloudflare Dashboard → Workers & Pages → Create → Pages → **Connect to Git** → 选中该仓库；Framework preset 选 **None**，Build command 留空，Build output directory 填 **`site`** → Save and Deploy。这样发布出去的只有 `site/` 里的内容，feed 地址就是 `https://你的项目名.pages.dev/pod-exp.xml` 等。
4. 本仓库自带的 `.github/workflows/update-feeds.yml` 会**每天 06:17（北京时间）**自动运行 `generate.py`，有新内容就提交并推送 → Cloudflare Pages 检测到提交后自动重新部署。也可以在 Actions 页面手动点 **Run workflow** 立刻更新。

## 本地运行

只需 Python 3.10+，无第三方依赖：

```bash
python3 generate.py            # 输出到 ./site/
SITE_URL=https://xxx.pages.dev python3 generate.py   # 顺带写入 atom 自引用链接
POD_ITEMS=30 BID_ITEMS=50 python3 generate.py         # 调整 feed 收录条数
```

feed 体积较大（全文文稿是中文长文，单 feed 约 2MB）是正常的；自建阅读器（FreshRSS / Miniflux 等）都没问题，托管阅读器如遇大小限制可把条数调小。

## 数据来源说明

- pod-exp：抓取其公开静态数据文件 `assets/catalog-data.js` 与各文章页的静态 HTML 全文（无需登录、无 JS 渲染要求）。仅用于个人订阅阅读。
- BidClub：使用其官方公开（免 key）的 JSON API（见其 `llms.txt`：`/api/feed-index`、`/api/v1/episodes/{slug}`）。内容版权归 BidClub 所有，仅作个人阅读用途，请勿二次分发。
