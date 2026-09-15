#!/usr/bin/env python3
"""
疑義解釈検索ツール（厚労省 xlsm）の Ver 監視

掲載ページ:
    https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryouhoken/newpage_21053.html
    ページ内の .xlsm リンクは1個だけ（2026-09-15 時点で確認済み）。
    2個以上 or 0個になったらページ改修とみなして異常停止する。

    ファイル名に Ver 番号が入る（Ver.1.1.4.xlsm）。表紙シートの更新履歴に
    「Ver1.1.4. 令和８年度診療報酬改定の疑義解釈（その12）を追加」とあり、
    Ver 更新 = 疑義解釈1回分の追加 に対応する。

判定の考え方（2段構え）:
    1段目: リンクURLの Ver 番号を前回値と比較 → 上がっていれば新版
    2段目: Ver が同じでも中身が差し替わることがあるので HEAD で
           Last-Modified / Content-Length を見る。動いていたら本体を
           取得して sha256 を照合する（= silent_update の検知）

    1.3MB のファイルを毎日落とすのは無駄なので、フル取得するのは
    「Ver が変わった」or「HEAD のメタ情報が動いた」ときだけにする。

使い方:
    python check_gigi_ver.py --state state/gigi_ver.json
    python check_gigi_ver.py --state state/gigi_ver.json --html-file fixture.html  # オフライン

終了コード:
    0 = 変化なし / 新版検知（正常）
    1 = 異常（リンク消失・複数・Ver退行など、人間の確認が要る）
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

PAGE_URL = ("https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/"
            "kenkou_iryou/iryouhoken/newpage_21053.html")

WATCHER = "gigi"
SCHEMA_VERSION = "1.0"

DEFAULT_CONFIG = {
    "watcher": WATCHER,
    "source": {"page_url": PAGE_URL},
    "http": {"timeout_sec": 30, "user_agent": "gigi-ver-watch/1.0 (personal research)"},
    "storage": {"state_file": "state/gigi.json", "download_dir": "data/gigi"},
    "handoff": {
        "command": "python load_gigi.py --xlsm data/gigi/{filename} --db med_code_map.db",
        "note": "取込後、gigi_source テーブルの ver が検知したVerと一致することを確認する",
    },
}


def load_config(path):
    """config.yml を読む。無ければ既定値で動く（PyYAML未導入でも落とさない）。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if not path or not os.path.exists(path):
        return cfg
    try:
        import yaml
    except ImportError:
        print("警告: PyYAML が無いので既定設定で動かす", file=sys.stderr)
        return cfg
    with open(path, encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    for k, v in loaded.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


# href="/content/12400000/Ver.1.1.4.xlsm" / 絶対URL 両対応
XLSM_HREF_RE = re.compile(
    r'href\s*=\s*["\']((?:https?://www\.mhlw\.go\.jp)?/content/[^"\']*?\.xlsm)["\']',
    re.IGNORECASE)

VER_RE = re.compile(r"Ver\.?\s*(\d+(?:\.\d+)*)\.?", re.IGNORECASE)
# 状態ファイルには接頭辞なしの "1.1.4" で保存されるので、そちらも読めるように
BARE_VER_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*\.?\s*$")

JST = timezone(timedelta(hours=9))

# 判定結果
NO_CHANGE = "no_change"
NEW_VERSION = "new_version"
SILENT_UPDATE = "silent_update"       # Verそのまま・中身だけ差し替え
FIRST_RUN = "first_run"
LINK_MISSING = "link_missing"         # 異常
LINK_AMBIGUOUS = "link_ambiguous"     # 異常
VER_UNPARSABLE = "ver_unparsable"     # 異常
VER_DOWNGRADE = "ver_downgrade"       # 異常

ABNORMAL = {LINK_MISSING, LINK_AMBIGUOUS, VER_UNPARSABLE, VER_DOWNGRADE}


def now_iso():
    return datetime.now(JST).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 純関数（HTTPに触らない = オフラインでテストできる）
# --------------------------------------------------------------------------

def extract_xlsm_links(html):
    """HTMLから .xlsm リンクを全部拾って絶対URL化し、重複を潰す。"""
    out = []
    for href in XLSM_HREF_RE.findall(html or ""):
        url = href if href.startswith("http") else "https://www.mhlw.go.jp" + href
        if url not in out:
            out.append(url)
    return out


def parse_ver(text):
    """'Ver.1.1.4.xlsm' -> ('1.1.4', (1, 1, 4))。取れなければ (None, None)。"""
    m = VER_RE.search(text or "")
    if not m:
        return None, None
    s = m.group(1)
    return s, tuple(int(x) for x in s.split("."))


def parse_ver_loose(text):
    """ファイル名('Ver.1.1.4.xlsm')でも状態ファイルの値('1.1.4')でも読む。

    ファイル名のパースには parse_ver（Ver接頭辞が必須）を使い、
    こちらは前回状態の読み取り専用にする。混ぜると
    'Other.xlsm' のような無関係な名前を誤って通す恐れがあるため。
    """
    if not text:
        return None, None
    s, t = parse_ver(text)
    if t is not None:
        return s, t
    m = BARE_VER_RE.match(str(text))
    if not m:
        return None, None
    return m.group(1), tuple(int(x) for x in m.group(1).split("."))


def compare(ver_tuple, prev_ver_tuple):
    """バージョンの大小。'1.1.10' > '1.1.9' を正しく扱うためタプル比較。

    桁数が違う場合（1.2 と 1.1.4）は短い方を0埋めして比較する。
    """
    if prev_ver_tuple is None:
        return FIRST_RUN
    n = max(len(ver_tuple), len(prev_ver_tuple))
    a = ver_tuple + (0,) * (n - len(ver_tuple))
    b = prev_ver_tuple + (0,) * (n - len(prev_ver_tuple))
    if a > b:
        return NEW_VERSION
    if a < b:
        return VER_DOWNGRADE
    return NO_CHANGE


def decide(html, prev_state):
    """HTMLと前回状態から判定する。ここが監視の本体。

    戻り値: dict(status, url, ver, ver_tuple, prev_ver, reason, need_download)
    """
    links = extract_xlsm_links(html)
    if len(links) == 0:
        return {"status": LINK_MISSING, "reason": "ページに .xlsm リンクが無い",
                "need_download": False}
    if len(links) > 1:
        return {"status": LINK_AMBIGUOUS,
                "reason": f".xlsm リンクが{len(links)}個ある: {links}",
                "need_download": False}

    url = links[0]
    ver, ver_tuple = parse_ver(os.path.basename(url))
    if ver is None:
        return {"status": VER_UNPARSABLE, "url": url,
                "reason": f"ファイル名からVerが読めない: {url}",
                "need_download": False}

    prev_ver = (prev_state or {}).get("ver")
    _, prev_tuple = parse_ver_loose(prev_ver)
    status = compare(ver_tuple, prev_tuple)

    return {
        "status": status,
        "url": url,
        "ver": ver,
        "ver_tuple": list(ver_tuple),
        "prev_ver": prev_ver,
        "reason": {
            FIRST_RUN: "初回実行",
            NEW_VERSION: f"Ver が {prev_ver} → {ver} に上がった",
            NO_CHANGE: f"Ver は {ver} のまま",
            VER_DOWNGRADE: f"Ver が {prev_ver} → {ver} に下がった（要確認）",
        }[status],
        # 新版・初回は本体が要る。同Verのときは HEAD の結果次第で後から決める
        "need_download": status in (NEW_VERSION, FIRST_RUN),
    }


def head_changed(head, prev_state):
    """HEAD の Last-Modified / Content-Length が前回と違うか。

    Ver据え置きで中身だけ差し替えられたケースを拾うための安い判定。
    片方でも欠けていたら「判定できない」として True を返す（安全側）。
    """
    if not prev_state:
        return True
    for key in ("last_modified", "content_length"):
        prev = prev_state.get(key)
        cur = (head or {}).get(key)
        if prev is None or cur is None:
            return True
        if str(prev) != str(cur):
            return True
    return False


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------
# 共通契約（contracts/watch_result.schema.json）への変換
# --------------------------------------------------------------------------

# status ごとの重み付け。通知側は status ではなくこの2つで分岐する。
SEVERITY = {
    FIRST_RUN: ("info", True),
    NEW_VERSION: ("info", True),
    SILENT_UPDATE: ("warn", True),
    NO_CHANGE: ("info", False),
    LINK_MISSING: ("error", True),
    LINK_AMBIGUOUS: ("error", True),
    VER_UNPARSABLE: ("error", True),
    VER_DOWNGRADE: ("error", True),
}

TITLES = {
    FIRST_RUN: "疑義解釈検索ツール Ver.{ver} を初回記録",
    NEW_VERSION: "疑義解釈検索ツール Ver.{prev_ver} → {ver}",
    SILENT_UPDATE: "疑義解釈検索ツール Ver.{ver} の中身が差し替わった疑い",
    NO_CHANGE: "疑義解釈検索ツール Ver.{ver} 変化なし",
    LINK_MISSING: "疑義解釈検索ツールのリンクが見つからない",
    LINK_AMBIGUOUS: "疑義解釈検索ツールのリンクが複数ある",
    VER_UNPARSABLE: "疑義解釈検索ツールのVerが読み取れない",
    VER_DOWNGRADE: "疑義解釈検索ツールのVerが退行している",
}


def to_watch_result(result, cfg):
    """内部の判定結果を、watcher共通の WatchResult に変換する。

    トップレベルには集約側が解釈する項目だけを置き、
    gigi 固有のもの（ver, sha256, saved_to 等）は payload に逃がす。
    """
    status = result["status"]
    severity, actionable = SEVERITY[status]
    is_error = severity == "error"

    title = TITLES[status].format(
        ver=result.get("ver") or "?",
        prev_ver=result.get("prev_ver") or "?",
    )

    if status in (NEW_VERSION, FIRST_RUN, SILENT_UPDATE) and result.get("saved_to"):
        next_action = cfg["handoff"]["command"].format(
            filename=os.path.basename(result["saved_to"]))
    elif is_error:
        next_action = "掲載ページの構成が変わっていないか目視で確認する"
    elif status in (NEW_VERSION, FIRST_RUN, SILENT_UPDATE):
        next_action = "本体を取得して取り込む（--no-download を外して再実行）"
    else:
        next_action = None

    return {
        "schema_version": SCHEMA_VERSION,
        "watcher": cfg.get("watcher", WATCHER),
        # 異常時は「どこを見ればよいか」が要るので掲載ページを指す
        "source_url": result.get("url") or (
            cfg["source"]["page_url"] if is_error else None),
        "checked_at": result["checked_at"],
        "status": status,
        "severity": severity,
        "is_actionable": actionable,
        "title": title,
        "reason": result.get("reason"),
        "next_action": next_action,
        "error": result.get("reason") if is_error else None,
        "payload": {
            k: v for k, v in {
                "ver": result.get("ver"),
                "prev_ver": result.get("prev_ver"),
                "filename": (os.path.basename(result["url"])
                             if result.get("url") else None),
                "sha256": result.get("sha256"),
                "last_modified": result.get("last_modified"),
                "content_length": result.get("content_length"),
                "saved_to": result.get("saved_to"),
            }.items() if v is not None
        },
    }



# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

def load_state(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(path, state):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch(url, method="GET", timeout=30):
    import urllib.request
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": "gigi-ver-watch/1.0 (personal research; contact: local)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return {
            "status_code": r.status,
            "headers": dict(r.headers),
            "body": r.read() if method == "GET" else b"",
            "last_modified": r.headers.get("Last-Modified"),
            "content_length": r.headers.get("Content-Length"),
        }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yml")
    p.add_argument("--state", help="省略時は config の storage.state_file")
    p.add_argument("--html-file", help="オフライン検証用。指定時はHTTPを使わない")
    p.add_argument("--download-dir", help="省略時は config の storage.download_dir")
    p.add_argument("--no-download", action="store_true",
                   help="検知だけして本体は落とさない")
    p.add_argument("--raw", action="store_true",
                   help="共通スキーマではなく内部の判定結果をそのまま出す")
    p.add_argument("--quiet", action="store_true", help="人間向け表示を抑える")
    a = p.parse_args()

    cfg = load_config(a.config)
    state_path = a.state or cfg["storage"]["state_file"]
    download_dir = a.download_dir or cfg["storage"]["download_dir"]
    page_url = cfg["source"]["page_url"]
    timeout = cfg["http"].get("timeout_sec", 30)

    prev = load_state(state_path)

    if a.html_file:
        html = open(a.html_file, encoding="utf-8").read()
    else:
        html = fetch(page_url, timeout=timeout)["body"].decode("utf-8", errors="replace")

    result = decide(html, prev)
    result["checked_at"] = now_iso()

    # --- 異常系はここで止める -------------------------------------------
    if result["status"] in ABNORMAL:
        _output(result, cfg, a)
        return 1

    # --- Ver据え置きのとき: HEADで中身の差し替えを確認 -------------------
    if result["status"] == NO_CHANGE and not a.html_file:
        head = fetch(result["url"], method="HEAD", timeout=timeout)
        result["last_modified"] = head["last_modified"]
        result["content_length"] = head["content_length"]
        if head_changed(head, prev):
            body = fetch(result["url"], timeout=timeout)["body"]
            sha = sha256_bytes(body)
            if prev and prev.get("sha256") and sha != prev["sha256"]:
                result["status"] = SILENT_UPDATE
                result["reason"] = "Verは据え置きだが中身が差し替わっている"
                result["need_download"] = True
                result["_body"] = body
            result["sha256"] = sha
        else:
            result["sha256"] = (prev or {}).get("sha256")

    # --- 本体取得 --------------------------------------------------------
    if result.get("need_download") and not a.no_download and not a.html_file:
        body = result.pop("_body", None)
        if body is None:
            got = fetch(result["url"], timeout=timeout)
            body = got["body"]
            result["last_modified"] = got["last_modified"]
            result["content_length"] = got["content_length"]
        result["sha256"] = sha256_bytes(body)
        os.makedirs(download_dir, exist_ok=True)
        dest = os.path.join(download_dir, os.path.basename(result["url"]))
        with open(dest, "wb") as f:
            f.write(body)
        result["saved_to"] = dest
    else:
        result.pop("_body", None)

    # --- 状態保存 --------------------------------------------------------
    if not a.html_file:
        save_state(state_path, {
            "ver": result.get("ver"),
            "url": result.get("url"),
            "sha256": result.get("sha256"),
            "last_modified": result.get("last_modified"),
            "content_length": result.get("content_length"),
            "last_checked_at": result["checked_at"],
            "last_status": result["status"],
        })

    _output(result, cfg, a)
    return 0


def _output(result, cfg, args):
    """標準出力にはWatchResult(JSON)を1件だけ出す。

    人間向けの表示は stderr に回す。こうしておくと
    `check_gigi_ver.py | jq` や n8n の Execute Command でそのまま食える。
    """
    payload = result if args.raw else to_watch_result(result, cfg)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.quiet or args.raw:
        return
    w = payload
    print(f"[{w['severity']}] {w['title']}", file=sys.stderr)
    if w.get("reason"):
        print(f"  理由: {w['reason']}", file=sys.stderr)
    if w.get("next_action"):
        print(f"  次の手: {w['next_action']}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
