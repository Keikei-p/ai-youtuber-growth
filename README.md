# AI YouTuber Growth

人間が毎回操作しなくても、AIが **企画 → 脚本 → 音声 → Shorts動画 → 投稿 → 分析 → 改善** を繰り返す「成長型AI YouTuber」の実験プロジェクトです。

## 現在のv1

- 1回の実行で3本の企画を生成
- Ollamaが使える場合はローカルLLMで企画・脚本生成
- Ollamaがない場合もフォールバック企画で動作確認可能
- SQLiteに過去動画と学習メモを保存
- 過去動画の完全重複を品質チェック
- VOICEVOXで音声生成
- Pillow + FFmpegで1080x1920のShorts動画を生成
- YouTube Data APIで投稿可能
- YouTube Analytics APIから視聴データを取得
- 視聴維持率・高評価率・コメント率から次回用の学習メモを作成
- GitHub Actionsで基本動作を自動チェック

## 安全設定

初期状態は `DRY_RUN=true`、YouTube投稿設定は `private` です。
そのため、設定を変更するまで勝手に公開投稿されません。

## Windowsでの準備

1. Python 3.11以上をインストール
2. このリポジトリをclone
3. `python -m venv .venv`
4. `.venv\Scripts\activate`
5. `pip install -r requirements.txt`
6. `.env.example` を `.env` にコピー
7. Ollamaを使う場合はOllamaを起動し、設定したモデルを用意
8. 動画生成する場合はVOICEVOXとFFmpegをインストールして起動

## 実行

企画と脚本だけ:

```bash
python main.py --show
```

音声・Shorts動画まで:

```bash
python main.py --render
```

YouTube投稿まで:

```bash
python main.py --render --upload
```

投稿後の分析・学習:

```bash
python main.py --learn
```

## YouTube初回認証

Google Cloudで YouTube Data API v3 と YouTube Analytics API を有効化し、デスクトップアプリ用OAuthクライアントを作成します。
取得したJSONをリポジトリ直下へ `client_secret.json` として配置します。

初回の投稿・分析時だけブラウザ認証が開きます。認証後は `token.json` が保存され、その後は自動実行できます。
`client_secret.json` と `token.json` は `.gitignore` 済みです。

## 次の段階

- 投稿時間の自動分散
- 24時間/72時間/7日後の自動分析
- 過去動画平均との差による企画選択
- キャラクター立ち絵・表情差分
- 自動サムネイル
- コメント分析
- 1日3本を基準に投稿本数自体を最適化
