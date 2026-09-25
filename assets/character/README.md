# Character

ミライの基準キャラクター画像は `mirai_reference.jpg` です。

通常の画像生成では、この基準画像を img2img の参照元にして、
顔・白銀髪・青い目・猫耳型ヘッドセット・青/シアン配色を維持します。

- `MIRAI_IDENTITY_LOCK_ENABLED=true`: ミライ固定キャラを優先
- `MIRAI_REFERENCE_STRENGTH=0.35`: 表情差分を作りつつ元の見た目を保持
- `MIRAI_IDENTITY_VIDEO_ENABLED=true`: 採用済みミライ画像を動画モーションの元にする

参照生成に失敗した場合は、無関係な別キャラを採用せず、
この基準画像を本編のキャラクター画像として使う安全側の設計です。
