# Mirai Native Models

目的は、Qwen / Stable Diffusion / AnimateDiff / VOICEVOX の
**学習済み第三者モデル重み**を段階的に外し、
Mirai自身の学習コードと自分で用意した権利クリアデータから作った重みに置き換えることです。

PyTorch / Pillow / FFmpeg は計算・画像・動画処理ライブラリとして利用します。
それらは「完成済みAIモデル」ではありません。

## 1. Brain v0

- 外部tokenizerなし
- UTF-8 byte tokenizer
- 自作decoder-only Transformer
- 学習データ: `data/native_training/brain/*.txt`
- 学習:

```powershell
.\.venv\Scripts\python.exe -m native_models.train_brain_v0 --steps 1000
```

`MIRAI_TEXT_PROVIDER=auto` では学習済みBrainがある時だけNativeを優先します。
`mirai_native` を指定すると外部LLMへフォールバックしません。

## 2. Image v0

`data/native_training/image/metadata.csv`

```text
images/0001.png|未来の青いAIスタジオ
images/0002.png|笑顔のミライ
```

最低20枚。権利が明確な画像だけを使います。

```powershell
.\.venv\Scripts\python.exe -m native_models.train_image_v0 --steps 2000
```

v0は64px前後のpixel diffusion研究版です。
`STUDIO_IMAGE_BACKEND=native` の時だけ使用し、Visual Quality Gateを通らなければ採用しません。
Native選択中にStable Diffusionへ黙ってフォールバックしません。

## 3. Video v0

`data/native_training/video/metadata.csv`

```text
clips/0001|ミライが軽く手を振る
clips/0002|未来のスタジオで話す
```

各clipフォルダには連番のPNG/JPGを2枚以上置きます。最低10クリップ。

```powershell
.\.venv\Scripts\python.exe -m native_models.train_video_v0 --steps 2000
```

`AI_VIDEO_BACKEND=native` で自作frame predictorだけを使用します。
外部Motion Adapterは使いません。

## 4. Voice v0

既存の `data/voice_training/metadata.csv` とWAVを使います。
自分で録音した音声、または学習/商用利用権が明確な音声だけを使用してください。

```powershell
.\.venv\Scripts\python.exe -m native_models.train_voice_v0 --epochs 8
```

自作Text Encoder → Spectrogram予測 → Griffin-LimでWAVを復元します。
学習済み `data/voice_models/mirai-native/model.pt` ができれば
Mirai Local TTSから推論できます。

## GTX 1070と大規模学習

GTX 1070 8GBではv0の小型実験は可能ですが、
現在の有名な大規模生成モデル級をゼロから学習するには計算量・データ量が不足します。

その場合でもコードを変える必要はありません。
同じ学習スクリプトと自分のデータを、より大きいGPUを持つ別PCまたは一時的なクラウドGPUへ移し、
完成した `model.pt` と `model.json` だけをMirai本体へ戻す設計です。

## 状態確認

```powershell
.\.venv\Scripts\python.exe native_model_lab.py --status
```

管理画面の **Mirai Native Model Lab** からも確認できます。

## 完成判定

- code_ready: 自作学習/推論コードがある
- dataset.ready: 学習素材が最低条件を満たす
- model.ready: 自分の学習済み重みが存在する
- production: 既存の品質ゲートを通過して初めて本番採用

「コードがある」だけでは完成扱いにしません。
