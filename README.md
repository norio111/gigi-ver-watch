# gigi-ver-watch

厚生労働省が公開する **疑義解釈検索ツール**（Excel マクロファイル）の更新を検知する watcher。

新しい疑義解釈が出たことを自動で拾い、医療コードDB（[med-code-map](https://github.com/norio111/med-code-map)）側の取り込み処理へ渡す。

---

## 背景：なぜ「監視」が要るのか

診療報酬のルールは、国が4種類の一次情報として出している。

| 情報源 | 形式 | 更新の規則性 |
|---|---|---|
| 点数表（告示） | PDF | 施行日は固定（原則2年ごと4/1）。発出日はブレる |
| 留意事項通知（保医発） | PDF | 告示と同時 |
| **疑義解釈（事務連絡）** | PDF | **完全に不定期** |
| 基本マスター | CSV | 定時改定＋随時改定 |

このうち **疑義解釈だけは、公表スケジュールが存在しない**。

令和8年度改定の実績を見ると、3〜5月は1〜2週おき、7月以降は月1回程度という減衰カーブを描くが、これは経験則であって規則ではない。「いつ出るか」が決まっていない以上、カレンダーで追うことはできず、**掲載状態の変化を検知するしかない**。

### 監視対象に xlsm を選んだ理由

厚労省は疑義解釈のPDFとは別に、平成18年度改定以降の疑義解釈を**問答単位で構造化した Excel ファイル**を公開している。

- 掲載先: [診療報酬関連情報](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryouhoken/newpage_21053.html)
- 収録: 7,629件（平成18年度改定〜令和8年度改定 その12、2026-09-02 時点）

PDFを1件ずつパースしてLLMで構造化するより、**このファイルを取り込む方が桁違いに確実で安い**。監視対象をPDF群ではなくこのファイル1つに絞れたのが、本 watcher の設計上の一番大きい判断。

### Ver番号が更新単位そのものだった

ファイル名にバージョンが入る（`Ver.1.1.4.xlsm`）。表紙シートの更新履歴を読むと、こう書かれている。

> Ver1.1.4.　令和８年度診療報酬改定の疑義解釈（その12）を追加

つまり **Ver が上がる = 疑義解釈が1回分追加された**。しかもそのVer番号はURLに含まれるため、**掲載ページのリンクを見るだけで新着が判定できる**。ファイル本体をダウンロードする必要すらない。

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

### 設計上の判断

**異常系は「変化なし」に倒さず必ず止める。**
掲載ページが改修されてリンクが消えたとき、黙って `no_change` を返すと、更新に気づかないまま古いデータを使い続けることになる。これが一番まずい失敗なので、リンク0個・複数・Ver退行はすべて error にして人間に投げる。

**Ver据え置きの差し替えを拾う経路を残した。**
厚労省がVer番号を上げずに中身だけ差し替える可能性は否定できない。その場合アナウンスは無いため、HEADのメタ情報差分でしか気づけない。ここを塞ぐと静かに腐る。

**毎回1.3MBを落とさない。**
本体を取得するのは「Verが動いた」か「HEADのメタ情報が動いた」ときだけ。通常日はHTML1回＋HEAD1回で済む。

**取り込みは自動化していない。**
新版検知後の `load_gigi.py` 実行は手動。分類の揺れや新しい件名パターンが混ざる可能性があるため、中身を人が確認してから取り込む運用にしている。

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
  "reason": "Ver が 1.1.4 → 1.1.5 に上がった（疑義解釈が1回分追加された）",
  "next_action": "python load_gigi.py --xlsm data/gigi/Ver.1.1.5.xlsm --db med_code_map.db",
  "payload": { "ver": "1.1.5", "prev_ver": "1.1.4" }
}
```

| status | severity | 通知 | 意味 |
|---|---|---|---|
| `first_run` | info | ○ | 初回。現在のVerを記録 |
| `new_version` | info | ○ | 疑義解釈が1回分追加された |
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

### n8n ワークフローについて

`workflow.gigi-ver-watch.json` は同じ判定を n8n 上で日次実行するための定義だが、**現状は手動実行で運用している**。疑義解釈の更新は月1回程度で、常時起動のコストに見合わないため。

n8nのCodeノードはn8n上でしかテストできないため、同じ判定ロジックを Python 側（`check_gigi_ver.py`）にも持たせ、そちらをフィクスチャで単体テストする構成にしている。JS側を変更したときは Python 側も併せて直す。

---

## 関連

- [med-code-map](https://github.com/norio111/med-code-map) — 取り込み先。`gigi_kaishaku` テーブルと `load_gigi.py`
- 中医協の議題監視 watcher — 別リポジトリ。同じ出力契約を共有し、将来統合可能にしてある

---

## 注意

厚労省は本ツールについて「検索結果はご参考です」「分類は参考です」「図や表については掲載していません」と明記している。**一次資料（事務連絡PDF）の代替ではない**。算定要件の判断に用いる場合は、必ず該当PDFを確認すること。

取得した xlsm 自体はリポジトリに含めない（`.gitignore`）。出所URLとVer番号は取り込み先の `gigi_source` テーブルに記録され、いつどの版を取り込んだかは追跡できる。
