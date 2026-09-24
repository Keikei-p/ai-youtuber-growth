# Assets

AI YouTuberの見た目に使う素材置き場です。

## キャラクター

`assets/character/default.png`

透過PNGのキャラクター立ち絵を置くと、Shorts内へ自動で合成します。
ファイルが無い場合でも動画生成は継続します。

## 背景

`assets/backgrounds/`

PNG / JPG / JPEG / WebP を複数入れると、場面ごとに順番に背景を切り替えます。
画像が無ければ自動生成グラデーション背景を使います。

## BGM

`.env` の `BGM_FILE` にファイルパスを指定すると、小音量で音声へミックスします。
YouTubeで利用する権利がある音源だけを使用してください。

この構成はWindowsでもLinux/Dockerでも共通です。
