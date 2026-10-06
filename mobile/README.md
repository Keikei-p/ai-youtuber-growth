# Mirai Mobile v0.1

Mirai Production OSの軽量コントローラーです。

重い動画・画像・音声生成はスマホ内で行いません。PCまたはCloud上のMiraiへ接続し、完全自動運用の開始/停止、投稿状態確認、投稿判定を行います。

## 初回接続

1. PC版ミライの「スマホアプリ接続」で接続コードを発行
2. Tailscale Serve等でPC版へ到達できるHTTPS URLを用意
3. Mirai MobileへURLと接続コードを入力
4. 接続コード平文はPC側DBへ保存されません

## 開発

Node.js 22以上を使用します。

```bash
cd mobile
npm install
npm run cap:add:android
npm run cap:sync
npm run cap:open:android
```

iOSはmacOS + Xcode環境で `npm run cap:add:ios` / `npm run cap:open:ios` を使用します。

## ストア提出前

- appId `com.yorokobi.miraiproduction` は仮。正式Bundle ID / Application IDを確定する
- Google Playは2026-08-31以降、新規/更新アプリでAndroid 16 / API 36以上をターゲットにする
- Apple App StoreにはプライバシーポリシーURLを設定する
- 将来Miraiアカウントを導入する場合、Apple/Googleのアカウント削除要件を実装する
- Android署名 / Apple Signing / ストア画像 / 審査情報は別途必要
- 現在は端末ペアリング方式で、Mirai独自アカウントは作成しない

## セキュリティ

モバイルAPIはBearer接続コード必須です。接続コードはPC側にはSHA-256のみ保存します。スマホ側はAndroid Keystore / iOS Keychainへ保存し、通常のlocalStorageへは保存しません。端末紛失時はPC版から「接続を解除」で即時無効化できます。


## CIでのAndroid実ビルド

Pull RequestごとにNode 24 + Java 21 + Android SDK 36でCapacitor Androidプロジェクトを生成し、`assembleDebug` まで実行します。生成されたdebug APKはCI artifactとして保存します。本番販売には別途release署名が必要です。
