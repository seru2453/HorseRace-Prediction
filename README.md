# 競馬予想支援アプリ MVP

JRAを対象にした個人利用向けの予想・分析・振り返り支援アプリです。馬券自動購入、投票連携、利益保証、一般向けデータ再配信は行いません。

## 技術構成

- Backend: Python, provider-neutral service layer, SQLAlchemy
- Intended API: FastAPI
- Local runnable API: Flask
- Frontend: mobile-first PWA
- Database: SQLite
- Tests: pytest

## 起動方法

```bash
make dev
```

Open `http://localhost:8000`.

`DEPLOYMENT_MODE=local` (default) keeps the local export/import and live provider features available. Set `DEPLOYMENT_MODE=public` to hide local administration controls and restrict public requests to saved analysis/results; public requests do not trigger upstream JRA fetches. Public mode is a deployment boundary, not an authentication system.

## 毎週土日の自動起動・停止（macOS）

macOSでログイン中のユーザー向けにLaunchAgentを登録すると、毎週土曜・日曜9:40にアプリを起動してデフォルトブラウザで `http://localhost:8000` を開き、16:30に最終pipelineを最大15分実行し、JSON backupを保存してからこのリポジトリのアプリサーバーを停止します。Macの電源が切れている場合やログインしていない場合、スケジュールどおりには実行されません。

土曜日・日曜日それぞれ9:40に起動、16:30に最終pipelineを実行して停止する2つのLaunchAgentを登録します。アプリが既に動いている場合は二重起動せず、ポート8000を使うプロセスの作業ディレクトリとコマンドを照合してからPIDを記録します。停止時も記録したPIDがこのリポジトリのFlaskプロセスであることを確認してからSIGTERMを送り、最大30秒待ちます。停止しない場合も強制終了はしません。他のPythonプロセスやブラウザは終了しません。

```bash
scripts/install_macos_autostart.sh
```

登録内容は `~/Library/LaunchAgents/com.horserace.saturday.plist` と `~/Library/LaunchAgents/com.horserace.saturday-stop.plist` に生成されます。スクリプトはリポジトリ位置からアプリを起動するため、ユーザー固有のパスをリポジトリへ書き込む必要はありません。手動で動作を確認する場合は次を実行します。

```bash
scripts/open_keiba.sh
```

停止スクリプトも必要に応じて手動実行できます。起動スクリプトが記録したアプリPIDだけを対象にします。

```bash
scripts/close_keiba.sh
```

アプリが応答済みならサーバーを増やさずブラウザだけを開き、対象プロセスを停止用PIDファイルへ記録します。起動直後は `/api/config` の応答を最大120秒待ちます。スケジュールの実行・停止結果は `/tmp/horse-race-saturday.log`、アプリのログは `/tmp/horse-race-app.log`、PIDファイルは `/tmp/horse-race-app-$(id -u).pid` です。アプリ側の定期処理は `make dev` に含まれるため、別途 `make worker` を起動しないでください。

自動起動を解除する場合:

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.horserace.saturday.plist"
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.horserace.saturday-stop.plist"
rm "$HOME/Library/LaunchAgents/com.horserace.saturday.plist"
rm "$HOME/Library/LaunchAgents/com.horserace.saturday-stop.plist"
```

## Docker

```bash
docker compose up --build
```

## デモモード

画面のモードで `デモ` を選び、以下の初期値で `分析開始` を押します。

- 開催日: `2026-08-16`
- 競馬場: `札幌`
- レース番号: `11R`

デモデータは完全に架空です。

## JRA取得モード

JRA公式の公開出馬表を、開催選択ページからPOST遷移でたどって取得します。確認済みの例:

- 開催日: `2026-08-16`
- 競馬場: `札幌`
- レース番号: `11R`

公開期間外、未掲載、掲載終了、JRA側の構造変更、ネットワーク制限がある場合は取得不能として表示します。開催日の全レースは開催一覧から低頻度で検出し、発走2時間前から未予測レースだけを自動分析します。

JRA公式の公開結果ページも、結果選択ページからPOST遷移でたどって取得します。確認済みの例:

- 開催日: `2026-08-08`
- 競馬場: `札幌`
- レース番号: `11R`

## データ保存場所

- SQLite: `data/db/keiba.sqlite3`
- Automatic JSON backups: `data/backups/horseprediction-YYYY-MM-DD.json` (latest 20 files)
- JRA fetch cache: `data/cache/jra_official`
- JRA fetch cache is refreshed after five minutes so the current race card is not masked by an older meeting page.
- While `make dev` is running, today's schedule, predictions due within two hours, and saved races awaiting results are checked every 30 minutes. The first check starts after 30 seconds. Each run logs schedule, predicted, already predicted, prediction pending, reviewed, result pending, and error counts.
- `make pipeline` runs the same automation once. `make worker` runs it continuously without the UI.
- `scripts/backup_database.py` writes the existing export format through an atomic temporary file, verifies it, and keeps at most 20 dated backups. Backups are local and ignored by Git; restore them with the app's `データ読込` action.
- Run `scripts/install_macos_autostart.sh` again after updating the repository to register the Saturday/Sunday schedule and final pipeline shutdown.

## テスト

```bash
make test
make check
```

Pull Request作成時と`main`へのpush時にGitHub Actionsの`CI / check`が`make check`を実行します。mainへのmerge前に必須にする場合は、GitHubの`Settings > Branches > main`で`CI / check`をrequired status checkに設定してください。

## 予想レポートCLI

```bash
make predict ARGS="--mode demo --date 2026-08-16 --course 札幌 --race 11 --iterations 1000 --no-save"
make predict ARGS="--mode jra --date 2026-08-16 --course 札幌 --race 11"
make predict ARGS="--mode demo --date 2026-08-16 --course 札幌 --race 11 --review-result --no-save"
make predict ARGS="--mode jra --review-saved-race-id 20260816-札幌-11"
make predict ARGS="--list-saved"
make predict ARGS="--list-saved --needs-review-only"
make predict ARGS="--mode jra --review-due"
make dataset
make dataset ARGS="--training-dataset-csv data/training/latest.csv"
make train
```

`mode jra` はJRA公式の公開出馬表を取得できる場合だけ動きます。取得不能時は理由を表示して終了します。
`--review-result` は結果を取得できるレースで、予想レポートに結果レビューを追加します。結果が未掲載の場合も予想レポートは出力します。
`--review-saved-race-id` は保存済みAI予測スナップショットを使って、出馬表を再取得せずに結果レビューします。
`--list-saved --needs-review-only` は、結果公開後にレビューすべき保存済み予想を確認するために使います。
`--review-due` は未レビューの保存済み予想をまとめて結果取得し、取得できたものだけレビュー保存します。

## 制限事項

- ライブJRA取得は `2026-08-16 札幌 11R` でスモーク確認済みです。
- ライブJRA結果取得は `2026-08-08 札幌 11R` でスモーク確認済みです。
- 障害戦は平地用モデルの予測・自動学習対象外です。
- 発走後に新しいAI予測は作成せず、発走前と確認できるスナップショットだけを学習に使用します。
- トップの `取得診断` から、分析保存前に出馬表取得、出走頭数、parse品質を確認できます。
- 取得不能時はAPIから具体的な失敗理由を返し、画面にも表示します。
- レビュー画面の `結果診断` から、結果保存前に結果ページ取得、結果行数、1着行数を確認できます。
- ParserはJRA公式出馬表HTMLの主要構造とfixtureベースの構造化HTMLに対応しています。
- JRA出馬表内の近走欄から、過去着順、勝ち切り率、凡走率、直近3走平均、上がり3F、同距離/同距離帯/同馬場/同競馬場複勝率、前走からの日数、距離変化量、馬場種別変化を特徴量化します。
- 近走の走破時計と距離から速度指標を作り、距離帯・馬場状態・競馬場で簡易補正した差分を直近重み付きでも特徴量化します。
- JRA出馬表内の近走欄から、同騎手での近走複勝率と継続騎乗も特徴量化します。
- JRA出馬表内の近走コーナー通過順から、脚質傾向と道中の押し上げ傾向を特徴量化します。
- JRA出馬表内の近走人気と着順から、人気以上に走る傾向と人気薄好走率を特徴量化します。
- 近走レース名から重賞経験、重賞好走率、前走からの格上/格下変化を補助的に評価します。
- 総賞金、通算勝率、通算複勝率は出走メンバー内の相対順位としても評価します。
- 今回斤量と前走斤量の差を特徴量化し、増量/減量をAI予想へ補助的に反映します。
- 近走馬体重から直近馬体重、平均馬体重、前走比増減を特徴量化し、急増/急減を補助的に評価します。
- 年齢を若駒/充実期/成熟期/ベテラン/高齢に分け、年齢帯を補助的に評価します。
- 前走からの日数と90日内出走数から、標準ローテ、余裕ローテ、短間隔、長期休養明け、使い詰め傾向を評価します。
- 出走馬全体の脚質から先行圧を推定し、前残り/差し届きの展開補正を行います。
- 札幌芝2000mなど既知の条件では、コース形状を踏まえた枠順補正を行います。
- 同馬場距離、同馬場距離帯、同競馬場距離での近走複勝率を補助評価します。
- 近走着順は出走頭数で補正し、上位半分に入った率も評価します。
- 近走評価は直近ほど重く扱う重み付き指標も併用します。
- 前走の着順、頭数補正着順、複勝圏/上位半分も個別に評価します。
- 近走着順の振れ幅から、安定型か波が大きいタイプかも補助評価します。
- 前走からの距離替わりは、変化量が大きいほどリスクを強く評価します。
- JRA出馬表に単勝オッズ/人気が表示されている場合は取得し、AI予想へ補助的に反映します。
- 単勝オッズは出走メンバー内の市場順位と支持シェアにも変換して評価します。
- JRA出馬表に通算成績/総賞金が表示されている場合は取得し、AI予想へ補助的に反映します。
- JRA馬場情報ページに対象競馬場が掲載されている場合は、天候と芝/ダートの馬場状態を取得してAI予想へ反映します。
- AI予想には、0.5刻みの星の強さ、信頼度、勝率/連対率/複勝圏、最大5件ずつのプラス/マイナス材料を表示します。
- 解析済み近走は `past_performances` に保存し、Providerが履歴を返さない場合はローカルDBから再利用します。
- AI予測は `prediction_snapshots` に、予測時点のモデル版・特徴量版・特徴量・AI確率・simulation seedを追記保存します。
- 直近のAI予測スナップショットは画面の `保存確認` タブと `/api/prediction-snapshots/<race_id>` で確認できます。
- レビュー時のAI評価には保存済みの発走前スナップショットだけを使用し、結果取得後の再計算は行いません。
- Simulationには本命寄り/混戦などのレース判定、本命候補、相手候補、穴候補、注意点の候補メモを表示します。購入連携や金額計算は行いません。
- Simulationには本命圏、相手圏、押さえ圏の候補セットを表示します。これは買い目ではなく確認対象の分類です。
- Simulationランキングには、首位差、複勝圏境界差、最頻着順を表示します。
- Simulationには、平均ペース、内有利、外有利、波乱含みの各シナリオで上位3頭に残る回数を展開一致度として表示します。
- 自分の予想では印だけを保存できます。勝率、自信度、騎手評価などの各評価、コメントは画面に出しません。
- 自分の予想では `AI下書き反映` で、AI候補セットから空欄の印を入れられます。保存は別操作です。
- 自分の予想の印は、レビュー比較用に裏側で暫定勝率へ換算します。
- レビュー時は保存済みの自分予想を自動利用し、画面入力がある場合は入力値を優先します。
- JRA結果を取得できない場合は、レビュー画面で着順を手入力して結果保存とレビューができます。重複着順と範囲外の着順は拒否します。
- 手入力結果はレビュー前に結果だけ保存することもできます。
- 保存済み結果を使って、JRA再取得や着順再入力なしでレビューできます。
- 保存済み分析を開いた場合、保存済み結果の着順を手入力結果フォームへ復元します。
- 保存履歴と詳細では、結果がJRA取得か手入力かを確認できます。
- レビュー結果はDBに保存され、学習状況タブで件数、平均Brier、競馬場・馬場・距離帯・馬場状態別、人気帯・枠・性別・年齢帯別のBrierを確認できます。
- レビュー結果にはAI/User比較、Brier差、AIの大きな外し、自分予想の大きな外し、AIと自分予想の大きなズレを含みます。
- レビュー結果には予測時点の評価ファクターも保存し、非勝ち馬を押し上げた要因や勝ち馬を下げた要因を後続の重み調整用に保持します。
- 評価ファクターは金額や補正量の違いを正規化し、同じ意味の要因として集計できる形で保存します。
- 学習データセットは `prediction_snapshots.raw_features` とレビュー結果ラベルを結合し、1行=1頭の教師データとして `/api/training-dataset` と `make dataset` から取得できます。
- ベースライン学習は `make train` で実行できます。現段階ではロジスティック回帰で、20頭行・4レース・勝ち/負けラベル数が不足する場合は不足理由を表示します。
- 学習状況ではAI優勢回数、自分予想優勢回数、平均Brier差も確認できます。
- 学習状況の裏側データにはファクター重みの調整候補も含め、件数と校正差が十分な `candidate` だけ次回予測へ小さく自動反映します。十分なレビュー済みレースがある場合は、対象レースを除外してロジスティック回帰の補助勝率も控えめにブレンドします。
- 学習状況の裏側データでは、ファクターごとの平均AI勝率と実勝率の差も集計します。
- ファクター調整候補は件数と校正差で `candidate` / `watch` / `monitor` に分け、`watch` / `monitor` は自動反映せず少数サンプルでの過学習を避けます。
- 学習状況では直近5レビューのAI/User Brier平均と、良化/悪化/横ばいの簡易トレンドを確認できます。
- 学習状況では解析件数、parse状態、provider別件数、特徴量充足率、過去走不足の解析品質サマリーを確認できます。
- 学習状況では欠損しがちな特徴量の上位件数を確認できます。
- 学習状況ではissuesが多い、または特徴量充足率が低い解析を `要確認解析` として確認できます。
- 学習状況ではAIと自分予想の確率校正を、確率帯ごとの平均予測勝率と実勝率で確認できます。
- 学習状況ではAI/User比較、校正ギャップ、条件別Brierから短い学習メモを表示します。
- 学習状況の直近レビューから保存済みレース詳細を開き、保存分析へ復元できます。
- 保存済みレースは `履歴` タブと `/api/races` で、スナップショット数、結果/レビュー有無、直近Brierを確認できます。
- 履歴は競馬場、結果有無、レビュー有無で絞り込めます。APIでは `/api/races?racecourse=札幌&has_result=true&has_review=true` のように指定できます。
- トップの `履歴を開く` から、分析前でも保存済みレースと学習状況を確認できます。
- トップの `データ出力` から、保存済み分析、予測、レビュー、自分予想、結果、過去走をJSONでバックアップできます。
- トップの `データ読込` から、出力済みJSONを既存DBへ非破壊で追加/更新できます。同一スナップショット、レビュー、自分予想は重複skipします。
- 履歴の詳細では `/api/races/<race_id>` から保存済み出馬表、直近AI予測、結果、直近レビューを再表示できます。
- 履歴詳細の `この保存分析を開く` から、保存済み分析を現在の出馬表・AI分析・Simulation表示へ復元できます。
- 保存済み分析を開いた場合、自分の予想フォームにも直近保存済みの印を復元します。
- 保存済み分析には解析品質、provider名、解析issues数を残し、履歴と詳細に表示します。
- 保存済み分析には特徴量欠損サマリーも残し、過去走不足頭数と特徴量充足率を表示します。
- 予測直前に未来情報混入ガードを実行し、結果系フィールドが特徴量に混ざった場合は分析を止めます。
- 予測はデータ不足時の簡易ルールベースです。
- Playwright E2Eは`make check`から実行します。CIはJRAへのライブ取得を行わず、fixtureとstubを使います。

## JRA-VAN移行方針

`services/data_provider/jra_van.py` の `JraVanProvider` を実装し、`RacingDataProvider` の共通形式へ変換します。Prediction、Simulation、UI、ReviewはProvider固有データに依存しません。
