# -*- coding: utf-8 -*-
"""
tools/check_live.py —— 实况巡检：拿「现在官方接口里有什么」对比「我们记了什么」

为什么要有它
------------
按 verified_at 减今天的日期，回答的是「你多久没查了」。
但免费政策可以在一两天内悄悄改掉，不预告、不发公告 —— 核实日期新鲜不代表数据没变。
所以真正的基线不该是「上次核实的日期」，而是「上次看到的实况」。
本脚本把各家官方的公开模型清单拉下来，与 data/models/*.json 做机器对比，
逐条报出：消失了什么、新出现了什么免费货、规格变了什么、下架时间变了什么。

它不做什么
----------
- 不读任何 Key（data/.keys.local.json 本脚本完全不碰）
- 不修改任何数据文件，只写 data/out/.live_report.json 与 data/out/.live_snapshot.json
- 不判断「该不该收录」 —— 那是人的决定
- 不绕过证书校验：拉的是「官方口径」，校验一关就等于把官方交给中间人

网页锚点与 SKIP 回访
--------------------
清单接口只认 id：实测 nvidia / modelscope 的匿名 `/v1/models` 字段只有
`created/id/object/owned_by`，一个价格字段都没有。也就是说「官方把某个限免模型改成付费」
这件事，光比清单是**完全静默**的。所以补了两条：

- `DOC_CHECK`：抓官方定价页 / 活动页正文，只看「我们这条记录所依据的逐字串」还在不在、
  出现几次。次数掉了说明官方在那页加/撤了同类条目，即使模型 ID 还留着
- `nvidia_page_check`：NVIDIA 的免费口径挂在 build.nvidia.com 模型页的徽章上，
  清单接口看不见。09-23 实测该页可匿名抓（带 Accept: text/html 的 RSC 正文），
  于是每轮逐模型页核「徽章还在不在 + 红色弃用徽章有没有挂上 + 内嵌 OpenAPI 的输出上限有没有漂移」。
  徽章在 ≠ 值得收：官方对旧代模型会同时挂 Free Endpoint 与 Deprecated 两个徽章
  （2026-09-23 逐页核实，10 条「徽章新增」里 9 条如此）——
  已判定不收的挂 `NVIDIA_BADGE_NOT_COLLECTED` 降噪，已收录的新挂弃用徽章会红色点名。
  nvidia 从 DOC_CHECK_BLIND 摘出
- `skip_probe`：每轮拿 SKIP 那几家的 `base_url` 再试一次匿名拉清单。
  否则 SKIP 里那句「X 月 X 日实测 401」会变成永久记忆，官方哪天开放也没人知道

⚠ 锚点命中 **不等于** 政策没变：页面上留着旧句子、政策其实已改口的情况它看不见。
它只把「该联网重核」从「靠人记得去浏览」变成「每轮出差异」。

加新渠道时要记得什么
--------------------
`channels.json` 里的每个渠道都必须二选一：要么有免 Key 的清单端点、登记进 `LIVE`；
要么确实查不动、写进 `SKIP` 并说明原因。两边都没登记 = 静默盲区，
报告会把它列进 `skipped`（原因写「未登记」），终端也会点名 —— 别让它悄悄变成「查过了，没变化」。

第三步（免费口径不在清单里的那几家才要）：往 `DOC_CHECK` 挂一个网页锚点。
判据很简单 —— `LIVE` 里 `free` 为 `None` 或整个渠道在 `SKIP`，就意味着「官方把它改成付费」
这件事机器比清单比不出来。做法：用真实请求确认哪个官方页能匿名抓到正文
（Mintlify 类的文档站通常在 URL 末尾加 `.md` 即原文），从里面挑**我们记录所依据的逐字串**当锚点。
覆盖不到的就写进 `DOC_CHECK_BLIND` 说明为什么，别假装有覆盖。

渠道下架（免费政策没了）时同理：从 `channels.json` 移出的同时要在 `data/inbox.json` 留一条
`status: todo` 的草稿，本脚本每轮都会把它列进 `drafts` 并点名。只删不记 = 这家从此没人看，
哪天官方重新放免费也不会被发现。

用法
----
    python tools/check_live.py             # 拉取 + 对比 + 打印摘要
    python tools/check_live.py --quiet     # 只写文件，不打印明细
"""
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))      # tools/
ROOT = os.path.dirname(BASE)                           # 项目根
DATA = os.path.join(ROOT, "data")
MODELS_DIR = os.path.join(DATA, "models")
OUT = os.path.join(DATA, "out")          # 机器产物统一放这里，不混在数据目录根
os.makedirs(OUT, exist_ok=True)
REPORT = os.path.join(OUT, ".live_report.json")
SNAP = os.path.join(OUT, ".live_snapshot.json")
INBOX = os.path.join(DATA, "inbox.json")

CTX = ssl.create_default_context()   # 证书校验必须开着：这里拉的是「官方实况」，
                                     # 一旦关掉，中间人就能伪造下架/新增清单并顺着流程写进数据
UA = {"User-Agent": "Mozilla/5.0 (compatible; free-llm-nav-livecheck/1.0)"}
TIMEOUT = 25

# 免 Key 可拉官方模型清单的渠道。free 字段说明怎么判定「免费」：
#   pricing — 看 pricing.prompt / completion 是否都为 0（OpenRouter 官方口径）
#   suffix  — 看 id 是否以指定后缀结尾（默认 :free，可用 free_suffix 换掉）
#   None    — 接口不返回价格，判定不了，只能报 id 层面的增删
LIVE = {
    "openrouter": {"url": "https://openrouter.ai/api/v1/models", "free": "pricing"},
    "nous":       {"url": "https://inference-api.nousresearch.com/v1/models", "free": "suffix"},
    "nvidia":     {"url": "https://integrate.api.nvidia.com/v1/models", "free": None},
    "modelscope": {"url": "https://api-inference.modelscope.cn/v1/models", "free": None},
    # Agnes 的匿名清单是「碰运气兜底」，不是稳定来源：2026-09-20 用两个出口各测约 25 次，
    # 只有 ~8% 拿到 200（同一 UA 既 200 又 401），且官方快速开始原文写的是「你将使用它来认证
    # 所有 API 请求」—— 从没承诺 /v1/models 可匿名。所以留在 LIVE（通了能交叉验证 12 条），
    # 但 401 才是常态；这 7 个模型的免费口径改由下面的 DOC_CHECK 定价页锚点兜
    "agnes":      {"url": "https://apihub.agnes-ai.com/v1/models", "free": None},
    # Zen 2026-09-21 实测匿名 200、返回 74 个 ID，从 SKIP 挪进来（旧记录「无公开清单端点」作废）。
    # ⚠️ 该清单字段只有 id / owned_by，一个价格字段都没有。后缀判出来的「新增免费」只是信号，
    # 官方免费清单页不逐个点名（`deepseek-v4-flash-free` 在清单里但页上没列），落库前仍需联网核
    "zen":        {"url": "https://opencode.ai/zen/v1/models", "free": "suffix",
                   "free_suffix": "-free"},
}

# 判定为免费但本项目有意不收的条目。不滤掉的话每次巡检都会重复出现，把真信号淹了。
# 每条都写明原因 —— 以后政策变了要重新收，看理由就知道当初为什么排除。
# 2026-09-21 本项目改收两条（`liquid/lfm-2.5-2.6b:free`、`nvidia/nemotron-3.5-content-safety:free`），
# 已从本表移除 —— 排除与否是收录判断，归维护者，本表只登记「当前有意不收」的
NOT_COLLECTED = {
    "openrouter/free": "路由器本体，不是具体模型",
    # 2026-09-21 本项目撤下：上游 DeepSeek 已退役 deepseek-v4-flash，官方 docs/zen.md 的
    # 免费清单与定价表都不点它的名，免费依据只剩 ID 后缀 —— 撤出收录，但端点里仍在册，
    # 所以必须挂这里，否则每轮巡检都会把它当「新增免费」报回来
    "deepseek-v4-flash-free": "Zen 侧官方免费清单与定价表均未点名，上游模型已退役（2026-09-21 撤下）",
    "google/lyria-3-pro-preview": "音乐生成，不是文本模型",
    "google/lyria-3-clip-preview": "音乐生成，不是文本模型",
}

# 本工具查不了的渠道。写清原因，不假装查过
SKIP = {
    "sensenova": "匿名请求返回 401，必须带 Key；本脚本不读 Key",
    "bai": "匿名请求需带 Key（2026-09-15 实测 401「无效的令牌」）；本脚本不读 Key",
    "cloudflare": "base_url 里含 <ACCOUNT_ID>，无法匿名构造端点",
    "qoder": "无 OpenAI 兼容端点，免费额度只能在 Qoder 客户端内选模型，没有可匿名拉的清单"
             "（zen 曾以同样理由待在这里，2026-09-21 实测它的 /zen/v1/models 匿名可拉，已挪进 LIVE）",
    "trae": "纯客户端渠道（Trae 国内版 AI IDE）：无 base_url、无可导出 Key，内置模型只能在客户端内凭积分调用。"
            "免费口径是积分赠送条款，写在 docs 计费页 —— 挂在下面 DOC_CHECK 的锚点兜（2026-09-25 实测该页匿名可抓）",
    "stepfun": "匿名请求 /v1/models 返回 401 invalid_api_key（2026-09-20 实测）；本脚本不读 Key。"
               "且它的免费口径根本不在清单接口里 —— 4 个限免音频模型是「定价与限速」页把单价整行"
               "标成「限时免费」得来的，只能联网核该页。⚠️ 2026-09-22 起本地记录里有两种免费口径：4 个限免音频模型（定价页）+ 1 个 `step-5-preview`（Step Plan 订阅体验档，依据在活动页通知与账号状态里，锚点和清单都查不到，只能浏览器回访）",
}

# SKIP 的理由是一次性实测快照（「X 月 X 日实测 401」）。官方哪天开放匿名清单，
# 这条会永久静默。所以每轮拿 base_url 再试一次匿名拉清单：
#   200 且结构像清单  → 报「这家可以移进 LIVE 了」
#   仍然非 200        → 把状态码和日期刷新，SKIP 的理由不再靠回忆
# 纯客户端渠道（没有 base_url）和端点含占位符的（Cloudflare 的 <ACCOUNT_ID>）试不了，
# 报告里会写明为什么试不了 —— 而不是悄悄不出现。
PROBE_FROM_SKIP = True

# 网页锚点：那些「免费口径只写在官方网页、接口清单里根本没有价格字段」的渠道。
# nvidia / modelscope 的匿名清单只有 id（实测 keys = created/id/object/owned_by，无价格），
# OpenRouter 与 Nous 的免费判定走 LIVE 的 pricing / :free 字段 —— 剩下这些家的「免费」
# 过去只能等 AI 下次浏览才发现官方改了。锚点把它提前到每轮出差异：
#   must  —— 官方页上必须还出现的逐字串（我们记录所依据的模型 ID / 活动原文）
#   count —— 逐字串出现几次。次数变了就说明官方在该页加/撤了同类条目，即使 ID 还在
# 只报「那句话还在不在」，不解析成结论。命中也不代表政策没变：页面上还写着旧句子、
# 政策已经改口的情况，锚点看不见，仍然要按提示词联网核原文。
DOC_CHECK = {
    "stepfun": {
        "page": "https://platform.stepfun.com/docs/zh/guides/pricing/details.md",
        "must": ["stepaudio-3-chat-preview", "stepaudio-3-realtime-preview",
                 "stepaudio-3-gen-preview", "stepaudio-3-music-preview", "限时免费"],
        # 「限时免费」整页 5 处 = 4 个限免模型 + step-2x-large 那句「已于 6/12 结束限时免费」。
        # 掉了就说明官方在定价表里撤了或加了免费行，而模型 ID 本身可能照旧留着
        "count": {"限时免费": 5},
        "note": "阶跃的 4 个限免音频模型只存在于这张定价表：「限时免费」整行没有单价",
    },
    "qoder": {
        "page": "https://docs.qoder.com/zh/events/flashoffer.md",
        "must": ["Qwen3.8-Flash", "0.0", "2026 年 9 月 30 日"],
        "count": {},
        "note": "Qoder 没有可拉的清单端点，免费只在活动页；活动窗口写在同一页",
    },
    "trae": {
        # Trae 的「免费」根本不在模型页（那页只列 17 个名字、无任何规格），在计费页的赠分条款里。
        # 锚点字串取 2026-09-25 匿名实测命中的逐字片段（整句「每月登录赠送 500 积分」在 HTML 里被
        # 标签拆开、数 0 命中，所以锚不带数字的半句 + 独立可数的那段）
        "page": "https://docs.trae.cn/ide_plans-and-billing",
        "must": ["每月登录赠送", "4000 积分", "150 积分", "调用自定义模型不会消耗积分"],
        "count": {},
        "note": "赠送积分条款（500/月 + 150/日 + 4000 一次性）是 Trae 唯一的免费路线依据；"
                "官方哪天删掉赠分条款，这三串立刻 missing。内置模型清单变化锚不住 —— 模型页无免费标注，只能浏览器回访",
        # 2026-09-26 全站枚举 docs.trae.cn 的 261 个文档页后找到的活动专页：Trae 个人版没有任何
        # 「模型限时免费」，只有这一页的限时折扣（免费用户也享，但仍扣积分）。
        # 锚点用 fetch_text 实测命中：整页正文在 HTML 里重复渲染一次，所以那句 1 折文案计 4 次。
        "more": [{
            "page": "https://docs.trae.cn/ide_limited-time-discount-for-builtin-models",
            "must": ["限时 1 折，积分消耗速度仅 0.08 倍。10 月 15 日前有效",
                     "DeepSeek-V4-Flash 正式版", "免费用户折扣表"],
            "count": {"限时 1 折，积分消耗速度仅 0.08 倍。10 月 15 日前有效": 4},
            "why": "唯一带明确截止日（10 月 15 日）的官方活动；文案消失或次数掉了 = 活动撤了或改档",
        }],
    },
    "cloudflare": {
        "page": "https://developers.cloudflare.com/workers-ai/platform/pricing/",
        "must": ["@cf/qwen/qwen2.5-coder-32b-instruct", "@cf/zai-org/glm-4.7-flash",
                 "@cf/openai/gpt-oss-120b", "@cf/nvidia/nemotron-3-120b-a12b",
                 "@cf/qwen/qwen3-30b-a3b-fp8", "@cf/qwen/qwen3.8-27b",
                 "@cf/ibm-granite/granite-4.0-h-micro", "@cf/google/gemma-4-26b-a4b-it",
                 "@cf/openai/gpt-oss-20b", "@cf/mistralai/mistral-small-3.1-24b-instruct",
                 "@cf/meta/llama-4-scout-17b-16e-instruct",
                 "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                 "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b"],
        "count": {},
        "note": "Workers AI 按 Neurons 计价，免费层 = 定价页里标 Free 的行；端点含占位符所以拉不了清单",
    },
    "agnes": {
        "page": "https://wiki.agnes-ai.com/zh-Hans/docs/pricing.md",
        "must": ["agnes-3.0-flash", "agnes-2.5-flash", "agnes-image-2.0-flash",
                 "agnes-image-2.1-flash", "agnes-image-2.5-flash", "agnes-video-v2.0",
                 "agnes-video-2.5-flash"],
        "count": {},
        "note": "Agnes 的清单端点官方从没承诺可匿名（约 8% 请求随机放行），定价页是这 7 条的稳定来源",
    },
    "zen": {
        # 走 .md：Zen 文档站是 Mintlify，末尾加 .md 拿到的是纯文本原文（含整张定价表），
        # 不像 /docs/zen/ 那样要靠渲染。定价表按行写 `| Big Pickle | Free | Free | ... |`
        "page": "https://opencode.ai/docs/zen.md",
        # ⚠️ 正文会被 normalize_doc 压掉连续空白并按标签切段，所以锚点必须按压缩后的形态写：
        #    原文 `| Big Pickle                        | Free   | Free    |` → 下面这样
        # 逐个模型锚「这一行的输入/输出都写着 Free」。官方把某行改成收费 → 这条直接 missing，
        # 比只锚模型名强：名字在价格变成 $ 之后照样留在页上
        "must": ["| Big Pickle | Free | Free |",
                 "| MiMo-V2.6-Flash Free | Free | Free |",
                 "| MiMo-V2.5 Free | Free | Free |",
                 "| Ling 3.0 Flash Fin Free | Free | Free |",
                 "| Nemotron 3 Ultra Free | Free | Free |",
                 "| Nemotron 3.5 Lightning Free | Free | Free |",
                 "| Muse Spark 1.3 Contributor Free | Free | Free |",
                 "| Jev 1.13 Free | Free | Free |",
                 "zero-retention policy"],
        # 8 个行锚各命中 1 次。不再用「Free | Free」计数当锚：压缩后该字串在整页出现 12 次
        # （含表头分隔与其他列组合），会把非免费行也数进去
        "count": {},
        "note": "Zen 的 /zen/v1/models 字段只有 id/owned_by、不含价格，免费口径只写在文档定价表里，"
                "所以这 8 行是它唯一的机器可查免费锚点（`muse-spark-1.2-contributor-free` 挂不上锚：官方"
                "定价表与「The free models」段都不点它的名，只有 ID 后缀，见本地条目 pitfall）。"
                "⚠️ 官方若只改表格排版（多加空格之外的改动），"
                "整片锚点会同时 missing —— 那是排版变了不是政策变了，要重新对形态",
    },
}

# 锚点覆盖不到的渠道，在报告里点名说清为什么覆盖不到 —— 不然读者会以为它们也被查过了
# 2026-09-23 起 nvidia 已不再是盲区（见 NVIDIA_PAGE_CHECK 段），这里只剩真查不动的两家
DOC_CHECK_BLIND = {
    "sensenova": "文档站正文靠 JS 渲染（实测匿名抓 /docs 只能拿到壳，模型 ID 命中 0 个），"
                 "正文在带 hash 的 bundle 里，锚点每次都要重新定位，不适合挂进巡检",
    "modelscope": "「魔粒」扣费口径在 modelscope.cn 的 docs 页，实测同一 URL 两次抓到 1098 / 7550 字"
                  "（正文靠 JS 注入，不稳定），锚点会假报警",
}

# NVIDIA 模型页实况巡检（徽章 + 输出上限）
# ------------------------------------------------------------
# 免费口径不在 /v1/models 清单里（字段只有 created/id/object/owned_by），只在 build.nvidia.com
# 的模型卡片页上。09-23 前它挂在 DOC_CHECK_BLIND 里被当作「只有联网浏览一条路」；当天实测翻案：
#   - 带 `Accept: text/html` 匿名抓 https://build.nvidia.com/<org>/<model> 返回 200，
#     正文内嵌 RSC payload（页面预取的 JSON）
#   - 免费徽章的**实体级**锚是 `nimType:endpoint:nim_type_preview`：17 个已收录免费页各命中；
#     非免费页（实测 yi-large 等）只在筛选器配置里出现 nim_type_preview 而没有这个实体标签 ——
#     所以只搜 `nim_type_preview` 会假报警，必须用完整实体锚
#   - 输出上限在同一 payload 内嵌的 OpenAPI 里：`"max_tokens"` 对象的 `maximum`（API 参数硬上限，
#     不是 default）。部分模型页确实没有该 schema（super / gemma-4），查不到就当留白维持
# 每轮对清单里全部 id 逐页抓（实测 82 个）：已收录的看徽章是否还在、有没有新挂红色弃用徽章、
# ctx_out 是否漂移；未收录的看是否冒出免费徽章（新放免费但没进清单的 ID 层差异，LIVE 对比看不见）。
# ⚠ 徽章在 ≠ 值得收：09-23 逐页核实当天那 10 条「徽章新增」时，发现 9 条同时挂着 Deprecated
# 徽章（fuyu/starcoder2/phi-3-vision 页内还有明文弃用日期），1 条（cosmos）整页变
# 「unavailable in your location」—— 与 09-20 删 deepseek-v4-pro-0813 同一情形，维护者判定不收，
# 全部登记进 NVIDIA_BADGE_NOT_COLLECTED 降噪
NVIDIA_PAGE_ORG = "nvidia"
NVIDIA_LIST_URL = "https://integrate.api.nvidia.com/v1/models"
NVIDIA_BADGE_ANCHOR = "nimType:endpoint:nim_type_preview"
# 红色弃用徽章的实体锚（页面 HTML 里 <span class="nv-badge ...">Deprecated</span>）。
# 2026-09-23 逐页核实发现：官方对「已弃用但仍挂着 Free Endpoint 徽章」的模型两个徽章同时显示，
# 徽章在 ≠ 还值得收录 —— 所以要单独查这一条，已收录模型新挂上弃用徽章等同于徽章消失
NVIDIA_DEPRECATED_ANCHOR = 'nv-badge">Deprecated<'

# 挂着免费徽章、但本项目判定不收的条目 —— 与 LIVE 侧的 NOT_COLLECTED 同理：
# 不登记的话每轮巡检都会把这 10 条重新报成「新增免费」，把真信号淹了。
# 2026-09-23 本项目判定「按既有口径不收」（与 09-20 删 deepseek-v4-pro-0813 同一判据：
# 官方标 Deprecated 就不收）。逐页原文核实：9 条页面同时挂红色 Deprecated 徽章，
# 1 条（cosmos）整页变「unavailable in your location」连徽章都抓不到。
NVIDIA_BADGE_NOT_COLLECTED = {
    "adept/fuyu-8b": "官方已弃用（页内原文 deprecated as of 2025-10-10，no longer supported）",
    "aisingapore/sea-lion-7b-instruct": "官方已弃用（页上红色 Deprecated 徽章）",
    "bigcode/starcoder2-15b": "官方已弃用（页内原文 deprecated as of 2025-07-24，no longer supported）",
    "microsoft/phi-3-vision-128k-instruct": "官方已弃用（页内原文 deprecated as of 2025-07-24，no longer supported）",
    "nvidia/cosmos-reason2-8b": "2026-09-23 复核整页为「This NIM is unavailable in your location」，"
                                "徽章抓不到（上一轮曾抓到过一次徽章，疑似出口/页面波动）—— 页面恢复后再议",
    "nvidia/embed-qa-4": "官方已弃用（页上红色 Deprecated 徽章）",
    "nvidia/nemotron-4-340b-reward": "官方已弃用（页上红色 Deprecated 徽章）",
    "nvidia/neva-22b": "官方已弃用（页上红色 Deprecated 徽章）",
    "nvidia/riva-translate-4b-instruct": "官方已弃用（页上红色 Deprecated 徽章；已收录的是 -v2 版）",
    "nvidia/vila": "官方已弃用（页上红色 Deprecated 徽章）",
}


def _rsc_unescape(body):
    """RSC payload 是字符串里再套一层的转义 JSON，先把引号还原才能正则"""
    return body.replace("\\u0022", '"').replace('\\"', '"')


def _balanced_obj(text, start):
    """text[start] 必须是 '{'，返回配平的整段；不配平返回 None"""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def max_tokens_caps(body):
    """页面内嵌 OpenAPI 里所有 max_tokens 对象的 maximum 集合（多值=不唯一，交给人判）。
    过滤两类确定不是输出上限的值：<8 的（1/2 这类标志位形状的值）和 int64/uint64 全量程
    哨兵（2^63-1、2^64-1，实测多个模型页的通用整数 schema 带着它们）。
    合法小值不受影响：llama-guard-4-12b 的真实上限就是 30"""
    t = _rsc_unescape(body)
    caps = set()
    for m in re.finditer(r'"max_tokens"\s*:\s*', t):
        j = t.find("{", m.end())
        seg = _balanced_obj(t, j) if j != -1 else None
        if seg:
            caps.update(int(x) for x in re.findall(r'"maximum"\s*:\s*(\d+)', seg))
    return {c for c in caps if 8 <= c <= 2 ** 31}


def nvidia_page_check():
    """逐模型页核徽章与输出上限。返回 None = 渠道未登记，不跑"""
    res = {"ok": False, "error": None, "url_list": NVIDIA_LIST_URL,
           "checked": 0, "page_errors": [],
           "badge_removed": [],      # 🔴 本地记为免费、官方页徽章已消失
           "deprecated_collected": [],  # 🔴 已收录模型新挂上红色弃用徽章（等同徽章消失对待）
           "badge_new_free": [],     # 🟢 清单里有、我们没收录、但页上挂着免费徽章
           "badge_known_noise": [],  # ⚪ 挂着免费徽章但本项目判定不收（附原因），不再重复报警
           "ctx_out_changed": [],    # 🟡 本地 ctx_out 与页面 maximum 不符
           "ctx_out_fillable": [],   # ⚪ 本地留白、页面查得到值（提示可回填）
           "ctx_out_ambiguous": []}  # ⚪ 页面给了多个 maximum，不据此下结论
    try:
        j = fetch(NVIDIA_LIST_URL)
    except Exception as e:
        res["error"] = "清单拉取失败：%s: %s" % (type(e).__name__, str(e)[:80])
        return res
    data = j.get("data") if isinstance(j, dict) else j
    ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")] \
        if isinstance(data, list) else []
    if not ids:
        res["error"] = "清单返回结构不认识"
        return res
    local = {m["id"]: m for m in load_local(NVIDIA_PAGE_ORG)}
    headers = dict(UA)
    headers["Accept"] = "text/html"
    for mid in sorted(ids):
        org, _, model = mid.partition("/")
        if not model:
            continue
        url = "https://build.nvidia.com/%s/%s" % (org, model)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception as e:
            code = "HTTP %s" % e.code if isinstance(e, urllib.error.HTTPError) \
                else "%s" % type(e).__name__
            res["page_errors"].append({"id": mid, "error": code})
            continue
        res["checked"] += 1
        badged = NVIDIA_BADGE_ANCHOR in body
        deprecated = NVIDIA_DEPRECATED_ANCHOR in body
        if mid in local:
            m = local[mid]
            if deprecated:
                res["deprecated_collected"].append(mid)
            if not badged:
                res["badge_removed"].append(mid)
                continue          # 徽章没了，再比输出上限没意义
            caps = max_tokens_caps(body)
            co = m.get("ctx_out")
            if len(caps) > 1:
                res["ctx_out_ambiguous"].append({"id": mid, "caps": sorted(caps)})
            elif not caps:
                if co is not None:
                    pass          # 页面查不到值：留白维持原记录，不报警
                # co 为 null 且页面无值 = 现状正确，什么都不报
            else:
                cap = next(iter(caps))
                if co is None:
                    res["ctx_out_fillable"].append({"id": mid, "cap": cap})
                elif int(co) != cap:
                    res["ctx_out_changed"].append({"id": mid, "old": co, "new": cap})
        elif badged:
            if mid in NVIDIA_BADGE_NOT_COLLECTED:
                res["badge_known_noise"].append({"id": mid,
                                                 "reason": NVIDIA_BADGE_NOT_COLLECTED[mid]})
            else:
                res["badge_new_free"].append(mid)
    res["ok"] = True
    return res


CN = {}   # channel id -> 中文名
DOC = {}  # channel id -> 官方文档链接。机器拉不到清单的那几家要靠它去联网核实，
          # 不写出来的话「去核官方文档」这一步连从哪儿开始都得再翻一遍 channels.json
CH_BASE = {}  # channel id -> base_url，SKIP 回访时要靠它拼出 /models 试一次
DRAFTS = {}   # channel id -> 草稿池条目（已移出正式清单 / 待核实，且 status != done）


def to_num(v):
    """接口有时给字符串有时给数字，统一成 float 比大小"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_text(url):
    """抓官方网页正文，去标签压空白，只用于「某个逐字串还在不在」的锚点判断。

    不解析语义：表格结构、上下文一概不看，只做子串命中 —— 解析就等于让脚本替官方说话"""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
        body = r.read().decode("utf-8", "replace")
    text = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def doc_check(cid, cfg):
    """网页锚点：官方页上我们记录所依据的那些原话，还在不在、出现几次

    一个渠道可以有多个页：`page` 是主页，`more` 再挂若干 `{page, must, count, why}`。
    纯客户端渠道的「免费 / 折扣」常同时写在计费页和活动页上，只锚一页就会漏掉活动到期。"""
    specs = [(cfg["page"], cfg.get("must") or [], cfg.get("count") or {}, None)]
    for extra in cfg.get("more") or []:
        specs.append((extra["page"], extra.get("must") or [], extra.get("count") or {},
                      extra.get("why")))

    row = {"id": cid, "name": CN.get(cid, cid), "page": cfg["page"],
           "ok": False, "error": None, "note": cfg.get("note"),
           "missing": [], "count_ok": True, "counts": {}, "pages": [],
           "page_errors": [], "expected": {}}
    for _, _, count, _ in specs:
        row["expected"].update(count)
    for page, must, count, why in specs:
        sub = {"page": page, "why": why, "ok": False, "error": None,
               "missing": [], "count_ok": True, "counts": {}}
        try:
            text = fetch_text(page)
        except urllib.error.HTTPError as e:
            sub["error"] = "HTTP %s" % e.code
            row["pages"].append(sub)
            continue
        except Exception as e:
            sub["error"] = "%s: %s" % (type(e).__name__, str(e)[:80])
            row["pages"].append(sub)
            continue
        sub["ok"] = True
        sub["missing"] = [n for n in must if n not in text]
        for needle, expected in count.items():
            actual = text.count(needle)
            sub["counts"][needle] = actual
            if actual != expected:
                sub["count_ok"] = False
        row["pages"].append(sub)

        if page == cfg["page"]:          # 主页的结论继续留在 row 顶层（报告与页面按旧字段读）
            # 拷贝而非共享：row 后面还会 update，不能把 sub 的字典改脏
            row["ok"], row["missing"], row["count_ok"] = True, list(sub["missing"]), sub["count_ok"]
            row["counts"] = dict(sub["counts"])

    if not row["ok"] and row["pages"]:
        row["error"] = row["pages"][0]["error"]
    # 附加页的结论并进顶层字段（报告与页面按旧字段读），抓取失败单独列，不冒充「没变化」
    for sub in row["pages"][1:]:
        if not sub["ok"]:
            row["page_errors"].append("%s：%s" % (sub["page"], sub["error"]))
            continue
        row["missing"] = row["missing"] + sub["missing"]
        row["counts"].update(sub["counts"])
        row["count_ok"] = row["count_ok"] and sub["count_ok"]
    return row


def skip_probe(cid):
    """SKIP 回访：拿 base_url 再试一次匿名拉清单，看这家有没有开放到能机器核。

    试不了要写清为什么试不了，不能悄悄不出现"""
    name = CN.get(cid, cid)
    base = CH_BASE.get(cid, "")
    if not base:
        return {"id": cid, "name": name, "tried": False,
                "why": "没有 base_url（纯客户端渠道），构造不出端点"}
    if not base.startswith("https://") and not base.startswith("http://"):
        # 纯客户端渠道的 base_url 里放的是一句人话（「仅客户端内可用」），不是地址
        return {"id": cid, "name": name, "tried": False,
                "why": "base_url 不是端点地址：%s" % base}
    if "<" in base or "{" in base:
        return {"id": cid, "name": name, "tried": False,
                "why": "base_url 含占位符 %s，匿名构造不出可用端点" % base}
    url = base.rstrip("/") + "/models"
    row = {"id": cid, "name": name, "tried": True, "url": url, "status": None,
           "now_free": False, "error": None}
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        arr = body.get("data") if isinstance(body, dict) else body
        row["status"] = r.status
        row["now_free"] = isinstance(arr, list)
        row["total_live"] = len(arr) if isinstance(arr, list) else None
    except urllib.error.HTTPError as e:
        row["status"] = e.code
        row["error"] = "HTTP %s" % e.code
    except Exception as e:
        row["error"] = "%s: %s" % (type(e).__name__, str(e)[:80])
    return row



def egress():
    """交代「这一轮是从哪个网络出口看官方的」。

    urllib 会自动采用系统代理，所以本地挂着代理时，拉到的是代理落地那侧的清单 ——
    而官方清单本身按地区发（Zen 官方就对中国返回 403 RegionError），出口不同、结果就不同。
    不交代的话，两次巡检的差异可能只是换了个出口，而不是政策变了。
    探测失败不影响巡检，只是少一行交代。"""
    px = urllib.request.getproxies()
    info = {"proxy": {k: px[k] for k in ("http", "https") if k in px} or None}
    try:
        req = urllib.request.Request("https://ipinfo.io/json", headers=UA)
        with urllib.request.urlopen(req, timeout=10, context=CTX) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        info.update(ip=j.get("ip"), city=j.get("city"), country=j.get("country"),
                    org=(j.get("org") or "")[:48])
    except Exception as e:
        info["echo_error"] = type(e).__name__
    return info


def egress_note(e):
    """报告里那句人话出口说明"""
    if e.get("proxy"):
        where = "%s / %s" % (e.get("city") or "?", e.get("country") or "?") if e.get("ip") \
            else "落地位置未知"
        return ("经系统代理出口 %s（%s%s）—— 看到的是代理那侧的官方清单，"
                "大陆直连结果可能不同" % (
                    "、".join(sorted(set(e["proxy"].values()))), where,
                    "，%s" % e["org"] if e.get("org") else ""))
    if e.get("ip"):
        return "直连出口 %s（%s / %s%s）" % (
            e["ip"], e.get("city") or "?", e.get("country") or "?",
            "，%s" % e["org"] if e.get("org") else "")
    return "出口未能确认（%s）—— 差异解读时把它当未知量" % (e.get("echo_error") or "无代理但探测失败")


def is_free(m, cfg):
    """返回 True/False；判定不了返回 None"""
    mode = cfg.get("free")
    if mode == "pricing":
        p = m.get("pricing") or {}
        a, b = to_num(p.get("prompt")), to_num(p.get("completion"))
        if a is None or b is None:
            return None
        return a == 0 and b == 0
    if mode == "suffix":
        return str(m.get("id", "")).endswith(cfg.get("free_suffix", ":free"))
    return None


def ctx_of(m):
    """(输入上限, 输出上限)。不同渠道字段名不同的地方在这里兜住"""
    c = to_num(m.get("context_length"))
    tp = m.get("top_provider") or {}
    o = to_num(tp.get("max_completion_tokens"))
    if o is None:
        o = to_num(m.get("max_completion_tokens"))
    return c, o


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_channels():
    for c in load_json(os.path.join(DATA, "channels.json"), []):
        CN[c["id"]] = c.get("name", c["id"])
        DOC[c["id"]] = (c.get("source_url") or "").strip()
        CH_BASE[c["id"]] = (c.get("base_url") or "").strip()


def load_inbox():
    """草稿池 items[]：免费活动结束被移出正式清单的渠道、以及待核实的新平台都在这一份里。
    它们不在 channels.json，本脚本也匿名拉不到清单，但不能因此从巡检里消失 ——
    免费活动是阶段性的，官方哪天重新放免费，必须在这一轮就被点名。"""
    for x in (load_json(INBOX, {}) or {}).get("items") or []:
        if x.get("id") and x.get("status") != "done":
            DRAFTS[x["id"]] = x


def coverage():
    """每个渠道要么查得动，要么写明为什么查不动 —— 两边都不沾就是静默盲区。
    新加渠道时漏登记最要命：报告会显示「0 处变化」，看着像查过且没变。

    返回 (未登记的渠道, 既不在渠道清单也不在草稿池的残留登记)"""
    blind = sorted(cid for cid in CN if cid not in LIVE and cid not in SKIP)
    ghost = sorted(k for k in list(LIVE) + list(SKIP) + list(DOC_CHECK)
                   if k not in CN and k not in DRAFTS)
    return blind, ghost


def load_local(cid):
    return load_json(os.path.join(MODELS_DIR, cid + ".json"), [])


def check(cid, cfg, prev):
    """拉一家，对比本地，返回结果 dict。网络出错也要有交代"""
    res = {
        "id": cid, "name": CN.get(cid, cid), "url": cfg["url"],
        "ok": False, "error": None,
        "total_live": None, "total_local": None, "prev_total": None,
        "removed": [],           # 本地有、官方没了 —— 最高信号
        "added_free": [],        # 官方新增且判定为免费 —— 真信号
        "known_noise": [],       # 新增且免费，但本项目有意不收（附原因）
        "added_unknown": [],     # 官方新增但判定不了是否免费（付费的也在这，只看数量）
        "spec_changed": [],      # 上下限变了
        "expiry_changed": [],    # 下架时间变了
        "note": None,
    }
    local = load_local(cid)
    res["total_local"] = len(local)
    lmap = {m["id"]: m for m in local}

    try:
        j = fetch(cfg["url"])
    except urllib.error.HTTPError as e:
        res["error"] = "HTTP %s" % e.code
        return res
    except Exception as e:
        res["error"] = "%s: %s" % (type(e).__name__, str(e)[:80])
        return res

    data = j.get("data") if isinstance(j, dict) else j
    if not isinstance(data, list):
        res["error"] = "返回结构不是 {data:[...]}"
        return res

    res["ok"] = True
    res["total_live"] = len(data)
    live = {m.get("id"): m for m in data if isinstance(m, dict) and m.get("id")}
    if prev and prev.get("total_live") is not None:
        res["prev_total"] = prev["total_live"]

    # 1) 消失了什么：本地记了、官方清单里没了
    res["removed"] = sorted(i for i in lmap if i not in live)

    # 2) 新增了什么
    added = [i for i in live if i not in lmap]
    mode = cfg.get("free")
    for i in sorted(added):
        f = is_free(live[i], cfg)
        if f is not True:
            res["added_unknown"].append(i)
        elif i in NOT_COLLECTED:
            res["known_noise"].append({"id": i, "reason": NOT_COLLECTED[i]})
        else:
            res["added_free"].append(i)
    if mode is None:
        res["note"] = "该接口不返回价格，新增的 %d 个无法判定是否免费，需人工核对官方文档" % len(added)

    # 3) 规格 / 下架时间变动（只在接口给得出来时比）
    for i, m in lmap.items():
        lm = live.get(i)
        if not lm:
            continue
        c, o = ctx_of(lm)
        if c is not None and m.get("ctx_in") is not None and int(c) != int(m["ctx_in"]):
            res["spec_changed"].append({"id": i, "field": "ctx_in",
                                        "old": m["ctx_in"], "new": int(c)})
        if o is not None and m.get("ctx_out") is not None and int(o) != int(m["ctx_out"]):
            res["spec_changed"].append({"id": i, "field": "ctx_out",
                                        "old": m["ctx_out"], "new": int(o)})
        # 只有接口真的带这个字段才比。nvidia / modelscope / agnes 的清单里没有 expiration_date，
        # 拿 None 去比本地 expiry 会把每条都误报成「下架时间变动 → 无」
        if "expiration_date" in lm:
            le = lm.get("expiration_date") or None
            if le != (m.get("expiry") or None):
                res["expiry_changed"].append({"id": i, "old": m.get("expiry"), "new": le})

    return res


def main():
    quiet = "--quiet" in sys.argv
    load_channels()
    load_inbox()
    blind, ghost = coverage()
    prev_snap = load_json(SNAP, {}) or {}
    eg = egress()

    results = [check(cid, cfg, prev_snap.get(cid)) for cid, cfg in LIVE.items() if cid in CN]
    doc_rows = [doc_check(cid, cfg) for cid, cfg in sorted(DOC_CHECK.items()) if cid in CN]
    probe_rows = [skip_probe(cid) for cid in sorted(SKIP) if cid in CN] if PROBE_FROM_SKIP else []
    nv_rows = nvidia_page_check() if "nvidia" in CN else None

    # 「查不了」= 登记过的（SKIP）+ 漏登记的（盲区）。后者也要出现在报告里，
    # 否则页面只列 SKIP，读者会以为清单是完整的
    BLIND = "未登记：既没有免 Key 的清单端点，也没说明为什么查不了 —— 请补进 check_live.py 的 LIVE 或 SKIP"
    skipped_rows = [{"id": k, "name": CN.get(k, k), "reason": v, "doc": DOC.get(k, "")}
                    for k, v in sorted(SKIP.items()) if k in CN]
    skipped_rows += [{"id": cid, "name": CN.get(cid, cid), "reason": BLIND,
                      "doc": DOC.get(cid, "")} for cid in blind]

    # 草稿池：移出正式清单但每轮都要重新看的渠道。不列出来的话，「B.AI 重新放免费」
    # 这种事发现在只能靠人记得去翻 inbox.json
    # ⚠ 只带 hint（一句话回访提示），不带 note —— 后者是历轮调研的内部结论，
    #   报告会被 build.py 嵌进产物 HTML，而产物是要转发、存网盘的（slim_inbox 也会兜一遍）
    draft_rows = [{"id": k, "name": x.get("name") or k, "url": x.get("url"),
                   "seen_at": x.get("seen_at"), "hint": (x.get("hint") or "").strip(),
                   "reason": SKIP.get(k) or "未登记：本脚本匿名拉不到它的模型清单"}
                  for k, x in sorted(DRAFTS.items())]

    now = datetime.now(timezone(timedelta(hours=8)))
    report = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "why": "官方接口实况与本项目记录的差异。基线是「上次看到的实况」，不是「上次核实的日期」",
        "channels": results,
        "skipped": skipped_rows,
        "doc_checks": doc_rows,
        "doc_blind": [{"id": k, "name": CN.get(k, k), "why": v}
                      for k, v in sorted(DOC_CHECK_BLIND.items())],
        "nvidia_pages": nv_rows,
        "skip_probe": probe_rows,
        "drafts": draft_rows,
        "egress": eg,
        "egress_note": egress_note(eg),
        "summary": {},
    }
    s = report["summary"]
    s["removed"] = sum(len(r["removed"]) for r in results)
    s["added_free"] = sum(len(r["added_free"]) for r in results)
    s["spec_changed"] = sum(len(r["spec_changed"]) for r in results)
    s["expiry_changed"] = sum(len(r["expiry_changed"]) for r in results)
    s["errors"] = [r["name"] for r in results if r["error"]]
    s["doc_missing"] = sum(len(r["missing"]) for r in doc_rows if r["ok"])
    s["doc_errors"] = [r["name"] for r in doc_rows
                       if r["error"] or r.get("page_errors")]
    s["doc_count_changed"] = [r["name"] for r in doc_rows if r["ok"] and not r["count_ok"]]
    s["probe_open"] = [r["name"] for r in probe_rows if r.get("now_free")]
    if nv_rows:
        s["nvidia_badge_removed"] = len(nv_rows["badge_removed"])
        s["nvidia_deprecated_collected"] = len(nv_rows["deprecated_collected"])
        s["nvidia_badge_new_free"] = len(nv_rows["badge_new_free"])
        s["nvidia_ctx_changed"] = len(nv_rows["ctx_out_changed"])

    # 推进快照：供下次对比「悄悄发生的变化」
    snap = {}
    for r in results:
        if r["ok"]:
            snap[r["id"]] = {"total_live": r["total_live"],
                             "at": report["generated_at"]}
        elif r["id"] in prev_snap:
            snap[r["id"]] = prev_snap[r["id"]]
    save_json(SNAP, snap)
    save_json(REPORT, report)

    if quiet:
        return

    lines = ["实况巡检 %s" % report["generated_at"], ""]
    lines.append("本轮网络出口：%s" % report["egress_note"])
    if eg.get("proxy"):
        lines.append("  ⚠ 挂着代理：下面所有「消失 / 新增」都可能是代理落地那侧的清单口径，"
                     "与大陆直连看到的不是同一份")
    lines.append("")
    for r in results:
        head = "%s（本地 %s 条 / 官方 %s 条%s）" % (
            r["name"], r["total_local"],
            r["total_live"] if r["total_live"] is not None else "?",
            "" if r["prev_total"] in (None, r["total_live"])
            else "，上次 %s 条" % r["prev_total"])
        if r["error"]:
            lines.append("  ⚠ %s —— %s（这次没查到，不算「没变化」）" % (r["name"], r["error"]))
            continue
        lines.append("  " + head)
        if r["removed"]:
            lines.append("    🔴 消失了 %d 个：%s" % (len(r["removed"]), "、".join(r["removed"][:6])
                                                    + ("…" if len(r["removed"]) > 6 else "")))
        if r["added_free"]:
            lines.append("    🟢 新增免费 %d 个：%s" % (len(r["added_free"]),
                                                    "、".join(r["added_free"][:6])))
        if r["known_noise"]:
            lines.append("    ⚪ 新增免费但本项目不收 %d 个（%s）" % (
                len(r["known_noise"]),
                "、".join("%s：%s" % (x["id"], x["reason"]) for x in r["known_noise"][:3])
                + ("…" if len(r["known_noise"]) > 3 else "")))
        if r["added_unknown"]:
            lines.append("    ⚪ 新增但免费与否未知 %d 个%s" % (
                len(r["added_unknown"]),
                "（%s）" % "、".join(r["added_unknown"][:4]) if len(r["added_unknown"]) <= 4 else ""))
        for x in r["spec_changed"][:8]:
            lines.append("    🟡 规格变动 %s · %s：%s → %s" % (x["id"], x["field"], x["old"], x["new"]))
        for x in r["expiry_changed"][:8]:
            lines.append("    ⏳ 下架时间 %s：%s → %s" % (x["id"], x["old"] or "无", x["new"] or "无"))
        if not (r["removed"] or r["added_free"] or r["spec_changed"] or r["expiry_changed"]):
            lines.append("    ✓ 与记录一致")
    lines.append("")
    lines.append("查不了的 %d 家（本脚本给不出结果，必须联网核官方说明页，不代表它们没变）："
                 % len(skipped_rows))
    for row in skipped_rows:
        lines.append("    %s — %s" % (row["name"], row["reason"]))
        if row.get("doc"):
            lines.append("      官方文档：%s" % row["doc"])
    if probe_rows:
        lines.append("")
        lines.append("SKIP 回访（每轮重测一次，免得「X 月 X 日实测 401」变成永久记忆）：")
        for p in probe_rows:
            if not p["tried"]:
                lines.append("    %s — 试不了：%s" % (p["name"], p["why"]))
            elif p["now_free"]:
                lines.append("    🟢 %s —— %s 现在能匿名拉到清单了（%s 条）！"
                             "该把它从 SKIP 移进 LIVE" % (p["name"], p["url"], p.get("total_live")))
            else:
                lines.append("    %s — 仍然查不了（%s）｜%s" % (
                    p["name"], p.get("error") or "返回结构不是清单", p["url"]))
    if doc_rows:
        lines.append("")
        lines.append("网页锚点（免费口径只写在官方网页、接口清单里没有价格的那几家）：")
        for r in doc_rows:
            if r["error"]:
                lines.append("    ⚠ %s — 页面这次没抓到（%s），不算「没变化」" % (r["name"], r["error"]))
                continue
            for msg in r.get("page_errors") or []:
                lines.append("    ⚠ %s 的附加页没抓到（%s），不算「没变化」" % (r["name"], msg))
            if r["missing"]:
                lines.append("    🔴 %s — 官方页上已经找不到：%s —— 记录所依据的原文没了，必须联网重核"
                             % (r["name"], "、".join(r["missing"])))
            elif not r["count_ok"]:
                exp = r.get("expected") or {}
                lines.append("    🟡 %s — 字串还在，但次数变了：%s —— 官方可能在这页加/撤了同类条目"
                             % (r["name"], "，".join("「%s」%d 次（记的 %d 次）" % (k, v, exp.get(k))
                                                     for k, v in sorted(r["counts"].items())
                                                     if v != exp.get(k))))
            else:
                cfg = DOC_CHECK[r["id"]]
                n_anchors = len(cfg.get("must") or []) + sum(
                    len(e.get("must") or []) for e in (cfg.get("more") or []))
                lines.append("    ✓ %s — %d 个锚点都在（%d 页）" % (r["name"], n_anchors, len(r["pages"])))
        if DOC_CHECK_BLIND:
            lines.append("    ⚪ 锚点覆盖不到：%s" % "；".join(
                "%s：%s" % (CN.get(k, k), v) for k, v in sorted(DOC_CHECK_BLIND.items())))
        lines.append("    ⚠ 锚点只回答「那句话还在不在」，命中不代表政策没变 —— 页面上留着旧句子、"
                     "政策已经改口的情况它看不见，仍要按提示词联网核原文")
    if nv_rows:
        lines.append("")
        lines.append("NVIDIA 模型页实况（逐页抓 build.nvidia.com：免费徽章 + 输出上限。"
                     "清单接口不含价格，这一节才是它免费的真实口径）：")
        if nv_rows["error"]:
            lines.append("    ⚠ 这一轮没查出结果（%s）—— 不算「没变化」" % nv_rows["error"])
        else:
            lines.append("    抓通 %d 页（清单共 %d 个 ID）" % (
                nv_rows["checked"],
                nv_rows["checked"] + len(nv_rows["page_errors"])))
            if nv_rows["page_errors"]:
                lines.append("    ⚪ 页面抓不通 %d 个：%s" % (
                    len(nv_rows["page_errors"]),
                    "、".join("%s(%s)" % (x["id"], x["error"]) for x in nv_rows["page_errors"][:6])
                    + ("…" if len(nv_rows["page_errors"]) > 6 else "")))
            for x in nv_rows["badge_removed"]:
                lines.append("    🔴 徽章消失（本地仍记为免费）：%s" % x)
            for x in nv_rows["deprecated_collected"]:
                lines.append("    🔴 已收录模型挂着官方弃用徽章：%s —— 与徽章消失同级，提请复核是否撤条" % x)
            for x in nv_rows["badge_new_free"]:
                lines.append("    🟢 未收录但挂着免费徽章：%s —— 该提请维护者复核是否收录" % x)
            for x in nv_rows["badge_known_noise"]:
                lines.append("    ⚪ 免费徽章在但不收录（已判定）：%s —— %s" % (x["id"], x["reason"]))
            for x in nv_rows["ctx_out_changed"]:
                lines.append("    🟡 输出上限漂移 %s：%s → %s" % (x["id"], x["old"], x["new"]))
            for x in nv_rows["ctx_out_fillable"]:
                lines.append("    ⚪ 本地留白但页面查得到输出上限：%s = %s —— 可回填" % (x["id"], x["cap"]))
            for x in nv_rows["ctx_out_ambiguous"]:
                lines.append("    ⚪ 页面给了多个 max_tokens.maximum，不据此下结论：%s = %s" % (x["id"], x["caps"]))
            if not (nv_rows["badge_removed"] or nv_rows["deprecated_collected"]
                    or nv_rows["badge_new_free"] or nv_rows["ctx_out_changed"]):
                lines.append("    ✓ 已收录 %d 条徽章都在，输出上限无漂移"
                             % len(load_local(NVIDIA_PAGE_ORG)))
    if draft_rows:
        lines.append("")
        lines.append("📥 草稿池 %d 家（已不在正式清单，但每轮都要重新核官方有没有恢复免费）：" % len(draft_rows))
        for row in draft_rows:
            lines.append("    %s — %s" % (row["name"], row["reason"]))
            lines.append("      官方链接：%s｜上次登记 %s" % (row["url"] or "无", row["seen_at"] or "未记"))
            if row["hint"]:
                lines.append("      回访要点：%s" % row["hint"])
    if ghost:
        lines.append("")
        lines.append("⚠ check_live.py 里登记着既不在 channels.json、也不在草稿池的渠道：%s "
                     "—— 要么补进 inbox.json，要么从 LIVE / SKIP 删掉" % "、".join(ghost))
    lines.append("")
    lines.append("合计：消失 %d · 新增免费 %d · 规格变动 %d · 下架时间变动 %d · 锚点丢失 %d"
                 % (s["removed"], s["added_free"], s["spec_changed"], s["expiry_changed"],
                    s["doc_missing"]))
    if s["doc_errors"]:
        lines.append("⚠ 锚点页这次没抓到的：%s —— 这一项没有结果，不等于锚点都还在"
                     % "、".join(s["doc_errors"]))
    if s["probe_open"]:
        lines.append("🟢 回访发现这些渠道已经能匿名拉清单：%s —— 该从 SKIP 挪进 LIVE 了"
                     % "、".join(s["probe_open"]))
    lines.append("明细已写入 data/out/.live_report.json（本脚本不改任何数据文件）")
    lines.append("下一步：python tools/build.py 重新构建，这次的结果才会出现在页面上")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
