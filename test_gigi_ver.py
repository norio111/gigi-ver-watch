#!/usr/bin/env python3
"""check_gigi_ver.py の判定ロジックをネットワーク無しで検証する。

実データ（2026-09-15 時点の掲載ページ）を模したHTMLと、
起こりうる異常系をフィクスチャで通す。
"""

import sys
from check_gigi_ver import (
    decide, parse_ver, compare, extract_xlsm_links, head_changed,
    parse_ver_loose,
    NO_CHANGE, NEW_VERSION, FIRST_RUN, LINK_MISSING, LINK_AMBIGUOUS,
    VER_UNPARSABLE, VER_DOWNGRADE,
)

# 実ページの該当部分を再現（相対パス href / 太字リンク）
REAL = '''<ul>
<li><a href="/stf/newpage_32041.html">処方箋の使用期間にご留意ください</a></li>
<li><strong><a href="/content/12400000/Ver.1.1.4.xlsm">疑義解釈検索ツールはこちら</a></strong></li>
</ul>
<p>※本ツールはマクロを使用しております。</p>'''

ABS = '<a href="https://www.mhlw.go.jp/content/12400000/Ver.1.2.0.xlsm">x</a>'
NOLINK = '<p>疑義解釈検索ツールは準備中です</p>'
TWO = ('<a href="/content/12400000/Ver.1.1.4.xlsm">a</a>'
       '<a href="/content/12400000/Other.xlsm">b</a>')
NOVER = '<a href="/content/12400000/gigikaishaku.xlsm">x</a>'

results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{'OK ' if ok else 'NG '}] {name:<44} got={got!r}")
    if not ok:
        print(f"         want={want!r}")


print("■ parse_ver")
check("Ver.1.1.4.xlsm", parse_ver("Ver.1.1.4.xlsm")[1], (1, 1, 4))
check("末尾ドット付き Ver1.1.4.", parse_ver("Ver1.1.4.")[1], (1, 1, 4))
check("Verが無い", parse_ver("gigi.xlsm")[1], None)

print()
print('■ parse_ver_loose（状態ファイル側の値）')
check("接頭辞なし 1.1.4", parse_ver_loose("1.1.4")[1], (1, 1, 4))
check("Ver付きも読める", parse_ver_loose("Ver.1.1.4.xlsm")[1], (1, 1, 4))
check("Noneは None", parse_ver_loose(None)[1], None)
check("無関係な文字列は通さない", parse_ver_loose("Other.xlsm")[1], None)

print("\n■ compare（桁上がり・桁数違い）")
check("1.1.10 > 1.1.9", compare((1, 1, 10), (1, 1, 9)), NEW_VERSION)
check("1.2 > 1.1.4", compare((1, 2), (1, 1, 4)), NEW_VERSION)
check("1.1.4 == 1.1.4", compare((1, 1, 4), (1, 1, 4)), NO_CHANGE)
check("1.1.3 < 1.1.4 は退行", compare((1, 1, 3), (1, 1, 4)), VER_DOWNGRADE)
check("前回なしは初回", compare((1, 1, 4), None), FIRST_RUN)

print("\n■ extract_xlsm_links")
check("実ページHTMLから1件", extract_xlsm_links(REAL),
      ["https://www.mhlw.go.jp/content/12400000/Ver.1.1.4.xlsm"])
check("絶対URLも拾う", len(extract_xlsm_links(ABS)), 1)
check("リンク無し", extract_xlsm_links(NOLINK), [])

print("\n■ decide（正常系）")
check("初回実行", decide(REAL, None)["status"], FIRST_RUN)
check("同Ver → 変化なし", decide(REAL, {"ver": "1.1.4"})["status"], NO_CHANGE)
check("新Ver → 検知", decide(ABS, {"ver": "1.1.4"})["status"], NEW_VERSION)
check("新Verは本体取得が要る",
      decide(ABS, {"ver": "1.1.4"})["need_download"], True)
check("同Verは本体取得しない",
      decide(REAL, {"ver": "1.1.4"})["need_download"], False)

print("\n■ decide（異常系 = 止めるべきケース）")
check("リンク消失", decide(NOLINK, {"ver": "1.1.4"})["status"], LINK_MISSING)
check("リンク複数", decide(TWO, {"ver": "1.1.4"})["status"], LINK_AMBIGUOUS)
check("Ver読めず", decide(NOVER, {"ver": "1.1.4"})["status"], VER_UNPARSABLE)
check("Ver退行", decide(REAL, {"ver": "1.2.0"})["status"], VER_DOWNGRADE)

print("\n■ head_changed（Ver据え置き時の差し替え検知）")
prev = {"last_modified": "Tue, 02 Sep 2026 01:00:00 GMT", "content_length": "1393647"}
check("同じなら変化なし",
      head_changed({"last_modified": prev["last_modified"],
                    "content_length": "1393647"}, prev), False)
check("サイズが動いたら変化",
      head_changed({"last_modified": prev["last_modified"],
                    "content_length": "1400000"}, prev), True)
check("ヘッダ欠損は安全側でTrue",
      head_changed({"last_modified": None, "content_length": "1393647"}, prev), True)
check("前回状態なしはTrue", head_changed({}, None), True)



# ==========================================================================
# 共通契約（WatchResult）への適合
# ==========================================================================
import json as _json
from check_gigi_ver import to_watch_result, load_config, SEVERITY, SCHEMA_VERSION

print("\n■ WatchResult 契約への適合")

_schema = _json.load(open("watch_result.schema.json", encoding="utf-8"))
_cfg = load_config("config.yml")
_req = _schema["required"]
_allowed = set(_schema["properties"].keys())


def _mk(status, **kw):
    base = {"status": status, "checked_at": "2026-09-16T07:00:00+09:00",
            "url": "https://www.mhlw.go.jp/content/12400000/Ver.1.1.5.xlsm",
            "ver": "1.1.5", "prev_ver": "1.1.4", "reason": "テスト"}
    base.update(kw)
    return to_watch_result(base, _cfg)


# 全statusで必須項目・未知キー・enum値を検証する
_all_ok = True
for _st in SEVERITY:
    w = _mk(_st)
    miss = [k for k in _req if k not in w]
    extra = set(w) - _allowed
    sev_ok = w["severity"] in ("info", "warn", "error")
    ver_ok = w["schema_version"] == SCHEMA_VERSION
    title_ok = 0 < len(w["title"]) <= 200
    ok = not miss and not extra and sev_ok and ver_ok and title_ok
    _all_ok &= ok
    print(f"  [{'OK ' if ok else 'NG '}] {_st:<22} severity={w['severity']:<5} "
          f"actionable={str(w['is_actionable']):<5} {w['title'][:34]}")
    if miss:
        print(f"         必須欠落: {miss}")
    if extra:
        print(f"         未知キー: {extra}")
results.append(_all_ok)

check("設定のhandoffコマンドが展開される",
      "Ver.1.1.5.xlsm" in (_mk("new_version", saved_to="data/gigi/Ver.1.1.5.xlsm")
                           ["next_action"] or ""), True)
check("異常時はsource_urlが掲載ページを指す",
      _mk("link_missing", url=None)["source_url"].endswith("newpage_21053.html"), True)
check("no_changeは通知不要", _mk("no_change")["is_actionable"], False)
check("silent_updateはwarn", _mk("silent_update")["severity"], "warn")
check("payloadにNoneが混ざらない",
      any(v is None for v in _mk("new_version")["payload"].values()), False)

print(f"\n最終結果: {sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
