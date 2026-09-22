# gigi-ver-watch

厚生労働省が公開する **疑義解釈検索ツール**（Excelマクロファイル）の更新を検知する watcher。

検索ツールの更新を検知し、医療コードDB（[med-code-map](https://github.com/norio111/med-code-map)）への取り込み判断につなげる。

---

## 背景：なぜ「監視」が必要なのか

診療報酬改定では、告示や通知の公表後も、算定方法や運用上の判断を補う疑義解釈が事務連絡として順次追加される。

| 情報源 | 形式 | 公表・更新時期 |
|---|---|---|
| 点数表（告示） | PDF | 改定時に公表 |
| 留意事項通知（保医発） | PDF | 告示にあわせて公表 |
| **疑義解釈（事務連絡）** | PDF | **事前日程なく随時公表** |
| 基本マスター | CSV | 定時改定＋随時改定 |

疑義解釈は改定直後に短い間隔で続き、その後は間隔が空く傾向にある。しかし、次回の公表日を予測できる規則はなく、カレンダーによる管理だけでは更新を見落とす可能性がある。

そこで本ツールでは、疑義解釈検索ツールの掲載状態を継続的に確認し、ファイルの更新を検知する。

### 監視対象に xlsm を選んだ理由

厚生労働省は疑義解釈のPDFとは別に、平成18年度改定以降の疑義解釈を問答単位で収録したExcelファイルを公開している。

- 掲載先: [診療報酬関連情報](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryouhoken/newpage_21053.html)
- 収録: 7,629件（平成18年度改定〜令和8年度改定 その12、2026-09-02時点）

このファイルはすでに問答単位で整理されているため、個別のPDFを解析するよりも取り込みやすい。そこで本 watcher では、複数のPDFではなく、このファイルの更新を監視対象とした。

### Ver番号による更新判定

ファイル名にはバージョン番号が含まれている（例：`Ver.1.1.4.xlsm`）。表紙シートの更新履歴には、次のように追加内容が記録されている。

> Ver1.1.4.　令和８年度診療報酬改定の疑義解釈（その12）を追加

このため、掲載ページにある `.xlsm` のURLからVer番号を取得し、前回値との差分で更新を判定する。通常の確認ではファイル本体をダウンロードせず、Ver番号が変わった場合にのみ取得する。

---

## 判定フロー

```
毎回の実行
    │
    ▼
掲載ページを取得
    │
    ▼
.xlsm リンクを抽出 ──┬─ 0個     → error（掲載場所が変わった）
    │                ├─ 2個以上 → error（判定不能）
    │                └─ Ver不明 → error
    ▼
Ver番号を前回値と比較
    │
    ├─ 上がった ──→ new_version   本体を取得・保存し、取込コマンドを提示
    │
    ├─ 下がった ──→ error         差し戻しか誤検知。自動処理せず人へ
    │
    └─ 同じ ──→ HEAD で Last-Modified / Content-Length を確認
                     │
                     ├─ 動いた → sha256を照合 → silent_update（warn）
                     └─ 同じ   → no_change（通知しない）
```

### 判定時の扱い

- リンクが見つからない、複数ある、Ver番号を読めない場合は `error` とする
- Ver番号が下がった場合は自動処理せず、確認対象とする
- Verが同じ場合も、`Last-Modified` と `Content-Length` の変化を確認する
- ファイル本体は、Verまたはメタ情報が変化した場合のみ取得する
- `load_gigi.py` によるDB取り込みは、中身を確認してから手動で実行する

---

## 出力

標準出力に、watcher共通の契約（[`watch_result.schema.json`](watch_result.schema.json)）に沿ったJSONを1件だけ出す。人間向けの表示は stderr に回しているので、`| jq` やパイプにそのまま流せる。

```json
{
  "schema_version": "1.0",
  "watcher": "gigi",
  "source_url": "https://www.mhlw.go.jp/content/12400000/Ver.1.1.5.xlsm",
  "checked_at": "2026-09-16T07:00:00+09:00",
  "status": "new_version",
  "severity": "info",
  "is_actionable": true,
  "title": "疑義解釈検索ツール Ver.1.1.4 → 1.1.5",
  "reason": "疑義解釈検索ツールのVerが1.1.4から1.1.5に更新された",
  "next_action": "python load_gigi.py --xlsm data/gigi/Ver.1.1.5.xlsm --db med_code_map.db",
  "payload": { "ver": "1.1.5", "prev_ver": "1.1.4" }
}
```

| status | severity | 通知 | 意味 |
|---|---|---|---|
| `first_run` | info | ○ | 初回。現在のVerを記録 |
| `new_version` | info | ○ | 疑義解釈のVerが更新された |
| `silent_update` | warn | ○ | Ver据え置きで中身が差し替わった疑い |
| `no_change` | info | − | 変化なし |
| `link_missing` | error | ○ | リンクが見つからない |
| `link_ambiguous` | error | ○ | リンクが複数ある |
| `ver_unparsable` | error | ○ | ファイル名からVerが読めない |
| `ver_downgrade` | error | ○ | Verが退行している |

`status` の語彙は watcher ごとに自由とし、**集約側が解釈するのは `severity` と `is_actionable` だけ**という約束にしている。これにより、将来ほかの watcher（中医協の議題監視など）と1リポジトリに統合したとき、通知処理を1本化できる。watcher固有の値はすべて `payload` に隔離する。

---

## 使い方

```bash

pip install pyyaml

# 監視を1回実行
python check_gigi_ver.py

# 検知だけして本体は落とさない
python check_gigi_ver.py --no-download

# ネットワークを使わずロジックだけ確認
python check_gigi_ver.py --html-file fixture.html

```

新版が検知されたら、med-code-map 側で取り込む。

```bash
cd ../med-code-map
python load_gigi.py --xlsm data/gigi/Ver.1.1.5.xlsm --db med_code_map.db
```

URL・保存先・引き渡しコマンドは [`config.yml`](config.yml) にあり、コードにハードコードしていない。

### テスト

```bash
python test_gigi_ver.py
```

判定ロジックはすべて純関数に切り出してあり、**ネットワークなしで全分岐を検証できる**（34ケース）。実ページのHTMLを模したフィクスチャに加え、リンク消失・複数・Ver退行といった異常系も含む。

---

## 構成

```
check_gigi_ver.py            監視本体。判定ロジックは純関数
test_gigi_ver.py             オフライン単体テスト（34ケース）
config.yml                   URL・スケジュール・保存先・引き渡し設定
watch_result.schema.json     watcher共通の出力契約
workflow.gigi-ver-watch.json n8nワークフロー（後述）
state/gigi.json              前回のVer・ハッシュ（gitignore）
data/gigi/                   取得したxlsm（gitignore）
```

### n8nワークフロー

`workflow.gigi-ver-watch.json` は、同じ監視をn8nから実行するための試作ワークフロー。現状は自動スケジュールを設定せず、手動で実行している。
判定ロジックのテストはPython側で行い、n8nのCodeノードを変更した場合はPython側にも同じ変更を反映する。

---

## 関連

- [med-code-map](https://github.com/norio111/med-code-map) — 取り込み先。`gigi_kaishaku` テーブルと `load_gigi.py`
- 中医協の議題監視 watcher — 別リポジトリ。同じ出力契約を共有し、将来統合可能にしてある

---

## 注意

厚労省は本ツールについて「検索結果はご参考です」「分類は参考です」「図や表については掲載していません」と明記している。**一次資料（事務連絡PDF）の代替ではない**。算定要件の判断に用いる場合は、必ず該当PDFを確認すること。

取得した xlsm 自体はリポジトリに含めない（`.gitignore`）。出所URLとVer番号は取り込み先の `gigi_source` テーブルに記録され、いつどの版を取り込んだかは追跡できる。
