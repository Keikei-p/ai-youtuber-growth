# AI YouTuber Growth / Mirai Production OS

ミライが **企画 → 台本 → 音声設計 → 画像/動画素材 → 動画構成 → 編集 → 品質検査 → 投稿 → 分析 → 原因究明 → 改善** を循環させる、自律成長型AI YouTuber制作システムです。

## 現在の中核構成

外部ツールへ判断を任せず、制作判断は `mirai_engines/` に集約しています。

### Mirai Voice Engine

`mirai_engines/voice_engine.py`

- 台本を意味の切れ目で分割
- serious / surprised / positive / thinking / neutral を自動判定
- 話速・ピッチ・抑揚・音量・間をミライ側で決定
- セグメント別に波形生成providerへ指示
- 最終WAVをミライ側で結合
- `.voice.json` に実際の音声設計を保存

現在の波形生成providerは `VOICEVOX` です。
VOICEVOXは「音声をどう話すか」を決めず、Mirai Voice Engineが決めたパラメータで波形を生成する部品として扱います。

provider切替口は `voice/provider.py` にあり、`.env` の `MIRAI_VOICE_PROVIDER` で将来の `mirai_native` 等へ交換できる構造です。

### Mirai Composer

`mirai_engines/composer.py`

- 字幕の分割
- 各シーンの表示時間
- 背景の使い分け
- push / pan / hold のカメラモーション
- AI動画素材を使う場面

を決定します。

FFmpegはComposerの計画を実行してH.264/AACへ変換する実行器です。

### Mirai Quality Engine

`mirai_engines/quality_engine.py`

完成MP4を `ffprobe` で実測し、映像/音声ストリーム、縦横比、解像度、FPS、動画尺、ファイルサイズ、字幕長、シーン数/時間を検査します。

Quality FAILの動画はPCには残しますが、自動投稿キューには入りません。

### Mirai Debug Engine

`mirai_engines/debug_engine.py`

失敗ログからGPU競合、VRAM不足、音声provider接続、FFmpeg/ffprobe、YouTube/OAuth、SQLite競合、Ollama/JSON出力などの原因候補、確信度、安全な対応、承認要否を記録します。

### Mirai Improvement Engine

`mirai_engines/improvement_engine.py`

Quality / Debug / YouTube Analytics を見て、Ollamaがなくても決定論的な改善を行います。
Ollamaが利用できる場合は追加提案を行いますが、改善の土台はMirai Improvement Engineです。

## 通常制作フロー

1. Ollamaで企画・台本・メタデータを作成
2. OllamaのVRAMを解放
3. ミライ/ゲスト/背景画像を1枚ずつ生成
4. 必要時だけAI動画素材を直列生成
5. Mirai Voice Engineで音声設計・WAV生成
6. Mirai Composerで全シーンを設計
7. FFmpegで1080×1920 / 30fps Shortsへ編集
8. Mirai Quality Engineで完成品を検査
9. PASSだけ投稿キューへ
10. YouTubeへ投稿
11. 24h / 72h / 7dでAnalyticsを取得
12. Mirai Debug / Improvement / Growth Engineで次回へ反映

## 省負荷・自律運転

- 重いGPU工程は同時実行しない
- 画像生成前にOllama VRAMを解放
- PC操作中は重い自動生成を延期
- RAM/VRAM不足時は延期
- 投稿が近い時は軽量モード
- CUDA失敗が続けば画像数を自動削減
- AI動画失敗が続けばAI動画を自動OFF
- 同じ生成失敗を無制限に繰り返さない
- SQLiteはWAL + busy timeoutで管理画面/自動運転の並行アクセスを安定化

## 自律権限

### 自動実行

- 知識/学習メモ
- 台本/話し方改善
- 企画改善
- プロンプト改善
- 品質分析
- 負荷を下げる変更
- 安全な再試行調整

### テスト後に自動候補

- 小さい非破壊コード変更
- 軽微なUI修正
- 非破壊リファクタリング

### 承認必須

- 課金/購入/サブスク
- 大幅コード変更
- DB破壊的変更
- データ削除
- セキュリティ/認証変更
- 外部アカウント変更
- 公開範囲変更
- PC負荷を増やす変更
- 大型モデルの追加

未知の変更は安全側で承認必須になります。

## 管理画面

`http://127.0.0.1:8765`

自動運転、投稿キュー、完成動画プレビュー、生成素材、Qualityスコア、Mirai自作エンジン状態、GPU/RAM状態、Debug失敗履歴、Improvement提案、承認待ち、通常運転テスト、安全停止を確認できます。

## 外出先からの管理

管理画面自体は `127.0.0.1` のまま外部公開しません。
Tailscale Serveを使い、同じtailnet内のスマホからのみ状態確認、更新/再起動、自動運転、安全停止を行える構成です。

## Windows自動運転

`automation/install_windows_task.ps1` で WakeToRun / StartWhenAvailable / 多重起動防止を設定します。

## テスト

GitHub Actionsでは毎pushで以下を確認します。

- 全Pythonファイルのcompile
- Mirai Voice / Composer / Quality / Debug / Improvement Engine
- SQLite/投稿キュー/自律権限の回帰テスト
- 品質FAILの投稿禁止
- Ollamaなしでの自作改善
- FFmpegで実際に1080×1920 MP4を生成
- ffprobeで実MP4の映像/音声/FPS/解像度を検査
- PowerShell自動運転/リモート管理スクリプトの構文
- dry-run起動
- 通常運転Python経路の統合テスト

## Windowsでの準備

1. Python 3.11以上
2. `python -m venv .venv`
3. `.venv\Scripts\activate`
4. `pip install -r requirements.txt`
5. 画像生成を使う場合 `pip install -r requirements-studio.txt`
6. `.env.example` を `.env` へコピー
7. Ollamaを起動
8. 現在の音声providerとしてVOICEVOXを起動
9. FFmpegをインストール

## YouTube初回認証

`client_secret.json` を配置し、`python youtube_connect.py` を実行します。
`client_secret.json` / `token.json` / `.env` はGit管理対象外です。

## 現在の「自作」の定義

ミライ側の自作コード: 音声設計、動画構成、字幕設計、モーション選択、品質合否、原因究明、改善判断、成長戦略、PC負荷制御、権限制御、自動復旧/停止。

交換部品として利用: VOICEVOX（波形生成）、Diffusers/Stable Diffusion（画像モデル実行）、AnimateDiff（AI動画モデル実行）、FFmpeg（エンコード）、Ollama（ローカルLLM実行）。

これらを直接業務ロジックへ埋め込まず、交換可能なprovider/実行器として扱います。
