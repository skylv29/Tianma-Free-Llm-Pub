# -*- coding: utf-8 -*-
"""
免费大模型渠道导航 —— 构建脚本

用法：
    python tools/build.py                 # 构建 HTML（自检 + 到期提醒 + 草稿池提示）

就一个用法，没有别的开关。想看「官方那边变了什么」跑 tools/check_live.py（免 Key 拉官方模型清单），
想看「我们自己改了什么」用 git diff —— 之前那套 --diff 快照基线和 --stale 保质期清单都删了：
    - --diff 的快照基线：git 已经在做同样的事，再多一份 .last_snapshot.json 纯属重复
    - --stale 的保质期清单：按 verified_at 做日期减法，只会回答「你多久没看了」，
      回答不了「政策动没动」。免费政策一两天内就会悄悄改，还会制造虚假安全感

新增一个渠道共三步：
    1. data/channels.json 加一条（id 与文件名保持一致）
    2. data/models/<id>.json 加一个文件（数组，字段见 data/meta.json 的 field_notes）
    3. tools/check_live.py 的 LIVE 或 SKIP 登记这个 id —— 漏了本文件的自检会当场告警
本文件（build.py）不用改。
"""
import ast
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))      # tools/，模板跟脚本放一起
ROOT = os.path.dirname(BASE)                           # 项目根，数据与产物都在这
DATA = os.path.join(ROOT, "data")
MODELS_DIR = os.path.join(DATA, "models")
TEMPLATE = os.path.join(BASE, "template.html")           # 模板跟脚本同在 tools/
OUTPUT = os.path.join(ROOT, "免费模型导航.html")
OUT = os.path.join(DATA, "out")                            # 机器产物统一放这里
os.makedirs(OUT, exist_ok=True)
PROBE = os.path.join(OUT, "probe_results.json")            # 探针结果（只有状态/延迟，无 Key）
INBOX = os.path.join(DATA, "inbox.json")                   # 新渠道草稿池，核实通过才升级进正式数据
LIVE_REPORT = os.path.join(OUT, ".live_report.json")       # 实况巡检产物，不入库
PLACEHOLDER = "/*__PAYLOAD__*/{}"                          # 模板里的数据注入点
CN_TZ = timezone(timedelta(hours=8))                       # 数据里的日期都按东八区口径写

# Key 的形状。产物里一旦出现这类串就中止构建 —— 页面是要转发出去的
KEY_RE = re.compile(r"(nvapi-[A-Za-z0-9_\-]{6,}|sk-[A-Za-z0-9_\-]{12,}|sk-ant-[A-Za-z0-9_\-]{12,})")

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=_dup_watch(path))


def _dup_watch(path):
    """JSON 里同名键出现两次时，dict() 静默留最后一份 —— 模型条目手滑写重就丢数据且自检查不出。
    解析时把重复键记进 JSON_DUPES，validate 统一报警"""
    def hook(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                try:
                    name = os.path.relpath(path, ROOT).replace("\\", "/")
                except ValueError:      # 跨盘符时 relpath 直接抛，退回绝对路径
                    name = path.replace("\\", "/")
                JSON_DUPES.append((name, k))
            d[k] = v
        return d
    return hook


JSON_DUPES = []


def norm_nl(s):
    """数据源里若误写成字面 \\n，这里归一成真空行（模板用 white-space:pre-wrap，只认真换行）"""
    return s.replace("\\n", "\n") if isinstance(s, str) else s


NL_FIELDS = ("notes", "rate_limit_note", "onboarding_note", "best_for")
NL_FIELDS_MODEL = ("specialty", "pitfall")
# vendors / changes / unverified 里同样可能出现字面 \\n，一并兜底
NL_FIELDS_VENDOR = ("intro", "benchmark", "free_note", "verdict")
NL_FIELDS_TEXT = ("content", "text")


def derive(m):
    """派生字段：避免手填导致与规格字段不一致"""
    modal_in = m.get("modal_in") or []
    usage = list(m.get("usage") or [])

    # 多模态：输入含图/音频/视频
    if any(x in modal_in for x in ("image", "audio", "video")) and "multimodal" not in usage:
        usage.append("multimodal")
    # 超长上下文：输入上限 >= 500K
    if (m.get("ctx_in") or 0) >= 500000 and "longctx" not in usage:
        usage.append("longctx")

    m["usage"] = usage
    # 语言模型判定看「输出里有没有 text」，不是「第一个输出是不是 text」。
    # stepaudio-3-chat-preview 的 modal_out 是 ["audio","text"]，按首位判会被打成
    # 图文模型，压到所有语言模型后面 —— 它明明能出文本
    m["is_text"] = "text" in (m.get("modal_out") or ["text"])
    return m


def collect_models(warns):
    models = []
    if not os.path.isdir(MODELS_DIR):
        return models
    for fn in sorted(os.listdir(MODELS_DIR)):
        if not fn.endswith(".json"):
            continue
        channel_id = fn[:-5]
        for m in load_json(os.path.join(MODELS_DIR, fn)):
            own = m.get("channel")
            if own and own != channel_id:
                # 「文件名即 channel id」是约定。手填值若被采信，free_model_count、筛选、
                # New API 导出会整条链算到别的渠道头上，而且自检查不出来
                warns.append("条目归属与文件名不符：models/%s 里的 %s 手填了 channel=%r，已按文件名归到 %s"
                             % (fn, m.get("id"), own, channel_id))
            m["channel"] = channel_id
            models.append(derive(m))
    return models


def norm_all(items, fields):
    """只归一真实存在的字符串字段；缺失的就让它缺失，别写出 null 键污染产物"""
    for it in items:
        for f in fields:
            if isinstance(it.get(f), str):
                it[f] = norm_nl(it[f])


def skill_path():
    """调研 skill 的路径。公开派生仓里恒返回空串，页面退化成「请自己填路径」。"""
    return ""


def build_payload():
    meta = load_json(os.path.join(DATA, "meta.json"))
    meta["built_at"] = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")
    # 任务台页签已删除，但 5 条门槛仍是 README 那张表的设计依据 —— 留在 data/meta.json 里，
    # 不再随产物分发（页面从不读它，内嵌进去就是每次构建多背一截死字节）
    meta.pop("task_profiles", None)
    collect_warns = []
    payload = {
        "meta": meta,
        "channels": load_json(os.path.join(DATA, "channels.json")),
        "models": collect_models(collect_warns),
        "vendors": load_json(os.path.join(DATA, "vendors.json")),
        "changes": load_json(os.path.join(DATA, "changes.json")),
        "unverified": load_json(os.path.join(DATA, "unverified.json")),
        # 优惠情报：官方有折扣 / 限时活动、但按口径不进收录的东西（付费订阅内的免费档、
        # 错峰折扣、已下架渠道的促销页）。纯文本，不参与任何计数与筛选
        "promos": load_json(os.path.join(DATA, "promos.json")),
        # 探针结果（只含状态/延迟/错误码）。没有跑过探针就是 null，页面会提示
        "probe": load_json(PROBE) if os.path.exists(PROBE) else None,
        # 实况巡检结果（tools/check_live.py 产出）：官方接口现在有什么 vs 我们记了什么。
        # 没跑过就是 null。它不依赖 verified_at，抓的是「一两天内悄悄发生的变化」
        "live": load_json(LIVE_REPORT) if os.path.exists(LIVE_REPORT) else None,
        # skill 不在项目目录里（装在外部 skill 目录），页面自己推导不出来，
        # 构建时注入绝对路径。「巡检更新」页生成的 prompt 靠它告诉 AI 去哪读流程。
        "skill_path": skill_path(),
        # 草稿池：全面复核 prompt 要读 status != done 的条目，顺带检查是否恢复免费
        "inbox": load_inbox(),
    }
    norm_all(payload["channels"], NL_FIELDS)
    norm_all(payload["models"], NL_FIELDS_MODEL)
    norm_all(payload["vendors"], NL_FIELDS_VENDOR)
    for group in ("changes", "unverified", "promos"):
        norm_all(payload[group], NL_FIELDS_TEXT)
    slim_inbox(payload)
    # 自检结果只给终端看：页面从不读它，塞进 payload 等于往分发出去的文件里装死字节
    return payload, collect_warns + validate(payload)


INBOX_SHIP_FIELDS = ("id", "name", "url", "seen_at", "status", "hint")


def slim_inbox(payload):
    """草稿池的调查全文只留在本地 `data/inbox.json`。

    产物 HTML 是要转发、存网盘的，而 inbox 的 `note` 是历轮调研的内部结论
    （谁被判过不实、账号里看到什么），全文嵌进去等于把调研笔记公开发布。
    页面和复核提示词真正需要的只有「回访哪一家、看哪句话」—— 那是 `hint`。
    实况报告里的 drafts 行同样带 note（老报告可能还有），一并剔掉。
    """
    inbox = payload.get("inbox")
    if inbox:
        inbox["items"] = [{k: v for k, v in it.items() if k in INBOX_SHIP_FIELDS}
                          for it in (inbox.get("items") or [])]
        for dead in ("_comment", "_usage"):
            inbox.pop(dead, None)
    for row in ((payload.get("live") or {}).get("drafts") or []):
        row.pop("note", None)


CN_NUM = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9}


def cn2int(s):
    """中文数字（1~99）转 int，够覆盖「N 个渠道」这种量级"""
    if not s:
        return 0
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        return (CN_NUM.get(a, 1) if a else 1) * 10 + CN_NUM.get(b, 0)
    return sum(CN_NUM.get(ch, 0) for ch in s) if len(s) > 1 else CN_NUM.get(s, 0)


RE_CH_COUNT = re.compile(r"([零一二三四五六七八九十]{1,3})个渠道")


def scan_channel_count(where, text, n, warns):
    """全称断言「N 个渠道」随渠道增减会整体过时，这里兜住"""
    for mo in RE_CH_COUNT.finditer(text or ""):
        v = cn2int(mo.group(1))
        if v and v != n:
            warns.append("渠道数硬编码已过时：%s 写「%s」，实际 %d 个渠道" % (where, mo.group(0), n))


def livecheck_registered():
    """check_live.py 里 LIVE + SKIP 登记过的渠道 id。

    用 ast 取字典顶层键，不用正则猜源码格式。返回 None = 源码没读到或结构不认识，
    调用方此时不该据此告警（免得脚本挪动/改写时误报一堆渠道没登记）。"""
    path = os.path.join(BASE, "check_live.py")
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError):
        return None
    out = set()
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if not any(isinstance(t, ast.Name) and t.id in ("LIVE", "SKIP") for t in node.targets):
            continue
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                out.add(k.value)
    return out


def validate(payload):
    """构建期自检：把原本要靠人肉复查才发现的问题当场报出来"""
    warns = []
    for fp, k in JSON_DUPES:
        warns.append("JSON 重复键：%s 里 \"%s\" 出现两次 —— 解析只保留后一份，前一份已静默丢失，"
                     "请手工合并" % (fp, k))
    scen = {s["id"] for s in payload["meta"]["scenarios"]}
    vids = {v["id"] for v in payload["vendors"]}
    chids = {c["id"] for c in payload["channels"]}
    n_ch = len(chids)
    # 调度字段的合法取值来自 meta 里的字典，避免渠道里写错值页面却不吭声
    lscopes = set(payload["meta"].get("limit_scopes") or {})
    qresets = set(payload["meta"].get("quota_resets") or {})
    total, avail = {}, {}
    for m in payload["models"]:
        cid = m["channel"]
        total[cid] = total.get(cid, 0) + 1
        # 只扣「模型条目自己写了 available_cn: false」的（如 Zen 对中国 403 的那两个）。
        # 不套用渠道级默认值：那是「国内要不要翻墙」的推定，用它派生会把 OpenRouter 的
        # 20 个免费模型算成 0 个，free_model_count 的语义就成了「国内可直连数」而不是「免费数」
        if m.get("available_cn") is not False:
            avail[cid] = avail.get(cid, 0) + 1
        if not m.get("usage"):
            warns.append("无 usage 标签（该模型在任何场景筛选里都不会出现）：%s :: %s" % (cid, m["id"]))
        for u in m.get("usage") or []:
            if u not in scen:
                warns.append("非法 usage %r：%s :: %s" % (u, cid, m["id"]))
        v = m.get("vendor")
        if v and v not in vids:
            warns.append("vendor 指向不存在的厂商 %r：%s :: %s" % (v, cid, m["id"]))
    for c in payload["channels"]:
        cid = c["id"]
        n_avail = avail.get(cid, 0)
        declared = c.get("free_model_count")
        if declared is None:
            c["free_model_count"] = n_avail          # 没填就直接派生
        elif declared != n_avail:
            warns.append("free_model_count 与实际不符：%s 声明 %s / 实际可用 %s（已按实际值渲染）"
                         % (cid, declared, n_avail))
            c["free_model_count_declared"] = declared
            c["free_model_count"] = n_avail
        if total.get(cid, 0) == 0:
            warns.append("渠道 %s 没有任何模型条目" % cid)

        # 调度三件套：决定「撞墙后换模型有没有用」和「能不能导进 New API」
        ls = c.get("limit_scope")
        if ls is None:
            warns.append("渠道 %s 缺 limit_scope —— 撞墙后换模型有没有用全靠它" % cid)
        elif lscopes and ls not in lscopes:
            warns.append("渠道 %s 的 limit_scope 取值非法 %r" % (cid, ls))
        qr = c.get("quota_reset")
        if qr is None:
            warns.append("渠道 %s 缺 quota_reset —— 决定该硬刚还是等重置" % cid)
        elif qresets and qr not in qresets:
            warns.append("渠道 %s 的 quota_reset 取值非法 %r" % (cid, qr))
        if c.get("available_cn") is not None and not c.get("available_cn_src"):
            # 渠道级默认值会被下面所有没填 available_cn 的模型继承，
            # 一个没写依据的布尔值能一次性污染几十条记录
            warns.append("渠道 %s 填了 available_cn=%s 但没有 available_cn_src —— "
                         "渠道默认值会被该渠道所有未填的模型继承，必须写清依据"
                         % (cid, c["available_cn"]))

        na = c.get("newapi")
        if not isinstance(na, dict) or "supported" not in na:
            warns.append("渠道 %s 缺 newapi.supported —— 导出 New API 配置时会漏掉它" % cid)
        elif na.get("supported") and c.get("access") == "client":
            warns.append("渠道 %s 标了 New API 可用，但 access 是 client（仅客户端），多半接不进去" % cid)
        if na and na.get("supported") and not c.get("base_url"):
            warns.append("渠道 %s 标了 New API 可用但没有 base_url" % cid)

    # 反方向：models/*.json 有条目，但 channels.json 没登记 → 页面上是孤儿条目
    for cid in sorted({m["channel"] for m in payload["models"]} - chids):
        warns.append("models/%s.json 有模型条目，但 channels.json 里没有这个渠道（会渲染成孤儿条目）" % cid)

    # 变更 / 未证实条目引用的渠道必须存在。已下架渠道在草稿池留有条目的算合法 ——
    # 它的政策变化记录要留在时间轴上当历史，重新放免费时还要靠这些记录判断
    draft_ids = {x.get("id") for x in ((payload.get("inbox") or {}).get("items") or [])}
    # note 不进产物（见 slim_inbox），页面与复核提示词靠 hint 说明「回访哪句话」
    for x in ((payload.get("inbox") or {}).get("items") or []):
        if x.get("status") != "done" and not (x.get("hint") or "").strip():
            warns.append("草稿池 %r（status=%s）没有 hint：产物里这一家的回访提示会是空的，"
                         "note 不会随产物分发 —— 去 data/inbox.json 补一句「本轮该重核官方哪句话」"
                         % (x.get("id"), x.get("status")))
    for key in ("changes", "unverified", "promos"):
        for cid in sorted({x["channel"] for x in payload[key] if x.get("channel")}
                          - chids - draft_ids):
            warns.append("%s.json 引用了不存在的渠道 %r（channels.json 和草稿池都没有它）" % (key, cid))

    # unverified 的 id 是手工递增的，撞号会误导引用
    seen, dup = set(), []
    for u in payload["unverified"]:
        if u["id"] in seen:
            dup.append(u["id"])
        seen.add(u["id"])
    if dup:
        warns.append("unverified.json 的 id 重复：%s" % sorted(set(dup)))

    # 评分口径（2026-09-24 判定，见 meta.json 字段字典）：同一模型跨渠道的 score_coding /
    # score_zh 必须全库一致。归一化 ID 分组 —— 去 @cf/ 与前缀 org、去 :free / -free 后缀、
    # 去四位批次尾号（0813 那类）。冲突多多半是新条目顺手填了个「看起来合理」的分
    def score_group_key(mid):
        s = mid.lower()
        s = re.sub(r"^@cf/", "", s)
        if "/" in s:
            s = s.split("/", 1)[1]
        s = s.replace(":free", "")
        s = re.sub(r"-free$", "", s)
        s = re.sub(r"-\d{4}$", "", s)
        return s
    sgroups = {}
    for m in payload["models"]:
        sgroups.setdefault(score_group_key(m["id"]), []).append(m)
    for k, ms in sorted(sgroups.items()):
        cv = {x.get("score_coding") for x in ms if x.get("score_coding") is not None}
        zv = {x.get("score_zh") for x in ms if x.get("score_zh") is not None}
        if len(cv) > 1 or len(zv) > 1:
            detail = "　".join(
                "%s::%s=%s/%s" % (x["channel"], x["id"],
                                  x.get("score_coding"), x.get("score_zh"))
                for x in ms if x.get("score_coding") is not None or x.get("score_zh") is not None)
            warns.append("同模型跨渠道评分冲突（归一组 %s）：%s —— 按 09-24 口径就低统一，"
                         "沿用/降档在 pitfall 注明出处" % (k, detail))

    # source_level 是「实/官/算/判」四档口径，按纪律必须由维护者判定。填错的后果是静默的：
    # 页面来源角标直接不渲染（srcChip 返回空串），复核保质期还回退成 30 天 —— 所以这里必须吭声
    slevels = set(payload["meta"].get("source_levels") or {})
    if slevels:
        for group in ("channels", "models", "changes", "unverified", "promos"):
            for it in payload[group]:
                sl = it.get("source_level")
                if sl and sl not in slevels:
                    warns.append("非法 source_level %r：%s :: %s（合法取值只有 %s）"
                                 % (sl, group, it.get("id") or it.get("channel"),
                                    " / ".join(sorted(slevels))))

    # 剩下的调度字段同样只认 meta 里的字典。漏了它们的后果是静默的：页面拿值去 meta 查名字，
    # 查不到就渲染成空白，构建一声不吭 —— 直到某次筛选「免费类型 = 促销」少了一条才发现
    for group, field, meta_key in (("channels", "access", "access_types"),
                                   ("channels", "onboarding", "onboarding_types"),
                                   ("models", "free_type", "free_types"),
                                   ("models", "reasoning", "reasoning_types")):
        legal = set(payload["meta"].get(meta_key) or {})
        if not legal:
            continue        # meta 里没这个字典时不据此告警，免得改字段名时报一堆假警
        for it in payload[group]:
            v = it.get(field)
            if v is not None and v not in legal:
                warns.append("非法 %s %r：%s :: %s（合法取值只有 %s；拿不准就填 null 留白）"
                             % (field, v, group, it.get("id"), " / ".join(sorted(legal))))

    # 实况巡检登记：漏登记比 SSL 报错危险 —— 报告会写「0 处变化」，看着像查过且没变。
    # check_live 自己也会在下一轮点名，但那是下次；这条把它提前到落库当场
    reg = livecheck_registered()
    if reg is not None:
        for cid in sorted(chids - reg):
            warns.append("渠道 %s 没登记进 tools/check_live.py 的 LIVE 或 SKIP —— 实况巡检每轮都会"
                         "静默跳过它。有免 Key 能拉的清单端点就进 LIVE（写清怎么判免费），"
                         "查不动的就进 SKIP 并写明为什么查不动" % cid)

    # 「N 个渠道」这类全称断言
    meta = payload["meta"]
    scan_channel_count("meta.subtitle", meta.get("subtitle"), n_ch, warns)
    scan_channel_count("meta.disclaimer", meta.get("disclaimer"), n_ch, warns)
    for c in payload["channels"]:
        for f in NL_FIELDS:
            scan_channel_count("channels[%s].%s" % (c["id"], f), c.get(f), n_ch, warns)
    for m in payload["models"]:
        for f in NL_FIELDS_MODEL:
            scan_channel_count("models[%s].%s" % (m["id"], f), m.get(f), n_ch, warns)
    for v in payload["vendors"]:
        for f in NL_FIELDS_VENDOR:
            scan_channel_count("vendors[%s].%s" % (v["id"], f), v.get(f), n_ch, warns)
    return warns


def check_secret_leak(payload, html):
    """产物里绝不能出现 Key —— 页面是要转发、存网盘的，这条是硬红线，命中即中止"""
    hits = []
    for label, text in (("payload", json.dumps(payload, ensure_ascii=False)), ("产物 HTML", html)):
        for m in KEY_RE.finditer(text):
            hits.append("%s 里疑似 Key：%s…" % (label, m.group(1)[:8]))
    return hits


def expiry_alerts(payload, days=30):
    """返回 (已过期未撤, days 天内将到期) 两段。已过期单独成段置顶 ——
    它不是提醒，是数据已经失真：页面还在教人白嫖一个下架的端点"""
    today = datetime.now(timezone(timedelta(hours=8))).date()
    gone, soon = [], []
    for m in payload["models"]:
        e = m.get("expiry")
        if not e:
            continue
        try:
            d = datetime.strptime(e, "%Y-%m-%d").date()
        except ValueError:
            soon.append("%s :: %s 的 expiry 日期格式非法：%s" % (m["channel"], m["id"], e))
            continue
        left = (d - today).days
        if left < 0:
            gone.append("%s :: %s 的官方下架日是 %s（已过 %d 天）—— 撤条或改 expiry，别让它装作还在"
                        % (m["channel"], m["id"], e, -left))
        elif left <= days:
            soon.append("%s :: %s 将在 %s 下线（还剩 %d 天）" % (m["channel"], m["id"], e, left))
    return gone, soon


def live_note(payload):
    """实况巡检报告自身的时效。基线是官方实况而不是核实日期，所以报告放凉了就是真凉了
    —— 没有一键脚本代为跑巡检，构建时提醒一句"""
    gen = (payload.get("live") or {}).get("generated_at")
    if not gen:
        return ("  ⚠ 没有实况巡检报告：页面看不到「官方清单里消失了什么 / 新上了什么免费货」。"
                "跑 python tools/check_live.py 再重新构建")
    try:
        d = datetime.strptime(gen, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
    except ValueError:
        return "  ⚠ 实况巡检报告的 generated_at 解析不了：%r，重新跑一次 check_live.py" % gen
    days = (datetime.now(CN_TZ) - d).days
    if days >= 3:
        return "  ⏱ 实况巡检报告是 %s 生成的（%d 天前）。免费政策一两天内就会悄悄改，建议重跑 check_live.py" % (gen, days)
    return "  ✓ 实况巡检报告：%s（%d 天前）" % (gen, max(days, 0))


def load_inbox():
    """草稿池：新渠道先落在 inbox.json（只存「在哪看到的」），核实通过才升级进 channels.json"""
    if not os.path.exists(INBOX):
        return None
    try:
        return load_json(INBOX)
    except Exception as e:
        print("  ⚠ inbox.json 解析失败：%s" % e)
        return None


def main():
    payload, warns = build_payload()

    with open(TEMPLATE, "r", encoding="utf-8") as f:
        html = f.read()
    if PLACEHOLDER not in html:
        # 不查的话会静默产出一个没有数据的页面，且退出码还是 0
        sys.exit("模板里找不到占位符 %s，构建中止（页面不会有数据）" % PLACEHOLDER)

    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("<", "\\u003c")          # 防止 </script> 提前闭合
    html = html.replace(PLACEHOLDER, blob)

    leak = check_secret_leak(payload, html)
    if leak:
        # 页面是要转发、存网盘的，Key 一旦进去就收不回来
        for x in leak[:10]:
            print("  ⛔ " + x)
        sys.exit("构建中止：产物里疑似含 API Key。请检查 data/*.json 是不是误粘了 Key。")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)

    lines = [
        "已生成 %s" % OUTPUT,
        "  渠道 %d 个 · 模型 %d 个 · 厂商 %d 家 · 政策变化 %d 条 · 未证实 %d 条 · 优惠情报 %d 条"
        % (len(payload["channels"]), len(payload["models"]), len(payload["vendors"]),
           len(payload["changes"]), len(payload["unverified"]), len(payload["promos"])),
    ]
    note = live_note(payload)
    if note:
        lines.append(note)

    if warns:
        lines.append("  ⚠ 数据自检 %d 条：" % len(warns))
        for w in warns[:20]:
            lines.append("    " + w)
        if len(warns) > 20:
            lines.append("    …另有 %d 条" % (len(warns) - 20))
    else:
        lines.append("  ✓ 数据自检通过（usage / vendor 引用 / 免费模型计数）")

    inbox = load_inbox()
    if inbox:
        todo = [x for x in (inbox.get("items") or []) if x.get("status") != "done"]
        if todo:
            lines.append("  📥 草稿池 %d 条待核实（check_live 每轮会点名；页面「巡检更新」列出它们，等官方恢复免费）：" % len(todo))
            for x in todo[:10]:
                lines.append("    - %s｜%s" % (x.get("name") or x.get("id"), x.get("url") or "无链接"))
            lines.append("    核实通过后再升级进 channels.json + models/<id>.json")

    gone, soon = expiry_alerts(payload)
    if gone:
        lines.append("  ⛔ 已过期未撤 %d 条（页面会标红「已过期」，但记录本身仍是错的）：" % len(gone))
        for a in gone:
            lines.append("    " + a)
    if soon:
        lines.append("  ⏳ 到期提醒：")
        for a in soon:
            lines.append("    " + a)

    print("\n".join(lines))


if __name__ == "__main__":
    main()
