# Third-Party / Rights Safety Notes

このファイルは法律意見ではなく、現在のMirai Production OSで使う第三者部品と運用上の確認事項を記録するものです。

## VOICEVOX

- 現在の既定 speaker ID: 3
- 話者: ずんだもん（ノーマル）
- 動画概要欄へ `VOICEVOX:ずんだもん` を自動付与する。
- VOICEVOX本体の規約だけでなく、各音声ライブラリ/キャラクターの規約にも従う。
- 話者IDを変更した場合、アプリはVOICEVOX Engineの `/speakers` からクレジット名を解決する。
- 必要クレジットを解決できない場合、自動YouTube投稿を停止する。

Official:
- https://voicevox.hiroshiba.jp/term/
- https://voicevox.hiroshiba.jp/qa/
- https://github.com/VOICEVOX/voicevox_vvm

## Stable Diffusion v1.5

Current model:
`stable-diffusion-v1-5/stable-diffusion-v1-5`

Model card indicates CreativeML OpenRAIL-M.
利用制限を含むため、モデル差し替え時は新しいモデルのライセンスを確認すること。

- 既存作品・既存キャラクターを再現するプロンプトを標準生成では使用しない。
- 生成物が既存作品に酷似した場合は公開しない。

## AnimateDiff

Current motion adapter:
`guoyww/animatediff-motion-adapter-v1-5-2`

AnimateDiff official repository code is presented under Apache-2.0.
ただしモデル重みはコードとは別に配布条件を確認する必要がある。
現在AI動画の自動生成は既定OFF。商用運用を拡大する前に使用する重みの配布ページ・ライセンスを再確認する。

## FFmpeg

現在はユーザーPCにインストール済みのFFmpegを外部実行する。
アプリと一緒にFFmpegバイナリを再配布する場合は、そのビルド構成に応じてLGPL/GPL等の条件を別途確認する。

## BGM

`BGM_FILE` を設定しても、`BGM_LICENSE_CONFIRMED=true` が明示されない限り動画へ混ぜない。

trueにするのは以下のいずれかを確認できる場合だけ:
- 自作BGM
- YouTube商用利用まで許可された素材
- 購入/契約したライセンスで対象チャンネル利用が許可されている素材

購入履歴・ライセンス文面・配布元URLなどの証拠は保存する。

## AI-generated content disclosure

MiraiはAI音声・AI画像/映像を使用するため、YouTube Data APIの
`status.containsSyntheticMedia` は安全側でtrueとして送信する。

## Real people / companies / reputation

自動企画では実在人物・企業・事件を原則として主役にしない。
犯罪・詐欺・不倫・違法等の信用を傷つけ得る断定、実在人物の声/顔の模倣、個別の医療・法律・投資助言などは公開前承認対象とする。

## Distribution of this application

現状はローカル運用を前提としている。
このアプリ自体を第三者へ販売・配布する段階では、Python依存パッケージ、モデル重み、VOICEVOX、FFmpeg、同梱素材のライセンス一覧を改めて監査する。
