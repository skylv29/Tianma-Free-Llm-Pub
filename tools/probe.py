# -*- coding: utf-8 -*-
"""免费渠道可用性探针（Key 只在本地，脚本不会把 Key 写进任何产物）

用法：
    python tools/probe.py              第一次跑会引导你录入 Key，之后直接跑探测
    python tools/probe.py --set nvidia 只录入/更新某个渠道的 Key
    python tools/probe.py --run        只探测，不询问
    python tools/probe.py --list       查看已配置的渠道（Key 打码）
    python tools/probe.py --skip zen   探测时跳过某渠道（可多次）

探测做什么：给每个渠道发一个 max_tokens=1 的最小请求，只记录
「通不通 / 多少毫秒 / 什么错误」，不含任何 Key。结果写进
data/out/probe_results.json，重新跑 python tools/build.py 后页面会显示。

安全边界：
    - Key 存在 data/.keys.local.json（已 gitignore，不入库）
    - 结果文件与最终 HTML 只含状态码与延迟
    - 本脚本从不读取、也从不打印 .keys.local.json 之外的内容
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))      # tools/
ROOT = os.path.dirname(BASE)                           # 项目根
DATA = os.path.join(ROOT, 'data')
KEYS = os.path.join(DATA, '.keys.local.json')
RESULT = os.path.join(DATA, 'out', 'probe_results.json')
CHANS = os.path.join(DATA, 'channels.json')
MODELS_DIR = os.path.join(DATA, 'models')

TIMEOUT = 12
PLACEHOLDER_RE = re.compile(r'<[A-Z_]+>')          # base_url 里的 <ACCOUNT_ID> 之类


def load_json(p, default=None):
    if not os.path.exists(p):
        return default
    with open(p, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_json(p, obj):
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def mask(k):
    """只露后 4 位：够认出是哪把 Key，又不会把前后 4 位拼成可用片段"""
    if not k:
        return '(空)'
    return '******' + k[-4:] if len(k) > 8 else '(已配置，长度 %d)' % len(k)


def read_keys():
    d = load_json(KEYS, {}) or {}
    d.setdefault('keys', {})
    d.setdefault('extra', {})       # 放 account_id 之类
    return d


def probe_model(c):
    """挑一个该渠道的文本模型当探针目标"""
    ms = load_json(os.path.join(MODELS_DIR, c['id'] + '.json'), []) or []
    txt = [m for m in ms if (m.get('modal_out') or ['text'])[0] == 'text']
    pool = txt or ms
    # 优先挑有评分的，接口更容易稳定
    pool = sorted(pool, key=lambda m: -(m.get('score_coding') or 0))
    return pool[0]['id'] if pool else None


def build_url(c, extra):
    base = (c.get('base_url') or '').rstrip('/')
    if PLACEHOLDER_RE.search(base):
        acct = (extra or {}).get('account_id', '')
        if not acct:
            return None, 'base_url 里有占位符（需要 account_id），先用 --set 补上'
        base = PLACEHOLDER_RE.sub(lambda m: acct, base)
    return base + '/chat/completions', None


def probe_one(c, key, extra, secrets):
    model = probe_model(c)
    if not model:
        return {'channel': c['id'], 'ok': False, 'http': None, 'latency_ms': None,
                'model': None, 'error': '该渠道没有可用的模型条目'}
    url, err = build_url(c, extra)
    if err:
        return {'channel': c['id'], 'ok': False, 'http': None, 'latency_ms': None,
                'model': model, 'error': err}

    body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'hi'}],
                       'max_tokens': 1, 'stream': False}).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + (key or ''),
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            r.read(2048)
            return {'channel': c['id'], 'ok': True, 'http': r.status,
                    'latency_ms': int((time.time() - t0) * 1000), 'model': model, 'error': None}
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read(600).decode('utf-8', 'replace')
        except Exception:
            pass
        return {'channel': c['id'], 'ok': False, 'http': e.code,
                'latency_ms': int((time.time() - t0) * 1000), 'model': model,
                'error': ('HTTP %s ' % e.code) + scrub(detail, secrets)[:220]}
    except Exception as e:
        return {'channel': c['id'], 'ok': False, 'http': None,
                'latency_ms': int((time.time() - t0) * 1000), 'model': model,
                'error': scrub('%s: %s' % (type(e).__name__, e), secrets)[:220]}


def scrub(text, secrets):
    """任何要落盘或打印的文本都要过这一道：把 Key 片段抹掉"""
    out = text or ''
    for k in secrets:
        if k and len(k) >= 6:
            out = out.replace(k, k[:4] + '***')
    return out


def main():
    argv = sys.argv[1:]
    chans = load_json(CHANS, [])
    kd = read_keys()

    if '--list' in argv:
        print('已配置的渠道（Key 已打码）：')
        any_ = False
        for c in chans:
            k = kd['keys'].get(c['id'], '')
            if k:
                any_ = True
                print('  %-12s %s' % (c['id'], mask(k)))
        if not any_:
            print('  （还没有配置任何 Key）')
        if kd['extra']:
            print('附加：%s' % {k: mask(str(v)) for k, v in kd['extra'].items()})
        return

    # 录入模式
    set_ids = []
    if '--set' in argv:
        i = argv.index('--set')
        set_ids = [a for a in argv[i + 1:] if not a.startswith('--')]

    need_input = set_ids or (not kd['keys'] and '--run' not in argv)
    if need_input:
        targets = [c for c in chans if c['id'] in set_ids] if set_ids else \
                  [c for c in chans if (c.get('newapi') or {}).get('supported')
                   and not kd['keys'].get(c['id'])]
        print('录入 Key（直接回车跳过）。输入不回显，且只写入 %s' % os.path.basename(KEYS))
        for c in targets:
            try:
                import getpass
                v = getpass.getpass('  %-12s : ' % c['id']).strip()
            except Exception:
                # 隐藏输入不可用时绝不回退到 input()：那会把 Key 原样打进终端与历史
                print('  %-12s : 本环境无法隐藏输入。请改为手工编辑 %s 里的 keys["%s"]，'
                      '此处直接跳过' % (c['id'], os.path.basename(KEYS), c['id']))
                v = ''
            if v:
                kd['keys'][c['id']] = v
        # Cloudflare 需要 account_id
        cf = [c for c in chans if '<ACCOUNT_ID>' in (c.get('base_url') or '')]
        if cf and not kd['extra'].get('account_id'):
            v = input('  Cloudflare account_id（base_url 里的 <ACCOUNT_ID>，回车跳过）: ').strip()
            if v:
                kd['extra']['account_id'] = v
        save_json(KEYS, kd)
        print('已保存（%s 已 gitignore，不会入库）\n' % os.path.basename(KEYS))

    # 探测
    skip = []
    for i, a in enumerate(argv):
        if a == '--skip' and i + 1 < len(argv):
            skip.append(argv[i + 1])
    secrets = list(kd['keys'].values()) + [str(v) for v in kd['extra'].values()]
    results, skipped = [], []
    for c in sorted(chans, key=lambda x: x.get('priority', 99)):
        cid = c['id']
        if cid in skip:
            skipped.append({'channel': cid, 'reason': '命令行 --skip'}); continue
        if not (c.get('newapi') or {}).get('supported'):
            skipped.append({'channel': cid,
                            'reason': (c.get('newapi') or {}).get('note') or '该渠道不支持裸 HTTP'}); continue
        key = kd['keys'].get(cid, '')
        if not key:
            skipped.append({'channel': cid, 'reason': '未配置 Key'}); continue
        r = probe_one(c, key, kd['extra'], secrets)
        results.append(r)
        flag = 'OK  ' if r['ok'] else 'FAIL'
        print('  %s %-12s %5sms  %s' % (flag, cid,
                                        r['latency_ms'] if r['latency_ms'] is not None else '-',
                                        scrub(r.get('error') or '', secrets)[:70]))

    save_json(RESULT, {
        'probed_at': time.strftime('%Y-%m-%d %H:%M'),
        'results': results,
        'skipped': skipped,
    })
    ok = sum(1 for r in results if r['ok'])
    print('\n探测完成：%d 个可用 / %d 个不通 / %d 个跳过' % (ok, len(results) - ok, len(skipped)))
    print('结果已写入 %s（只含状态与延迟，无 Key）。跑 python tools/build.py 后页面会显示。' % os.path.basename(RESULT))


if __name__ == '__main__':
    main()
