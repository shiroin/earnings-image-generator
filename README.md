# IR Webcast Transcriber v14 — SmartVision IR対応

Streamlit Cloud向け。文字起こしはOpenAI APIを使い、ローカルWhisperはロードしません。

## v14の追加点
- SmartVision IR / iVision系を想定した **iframe再帰探索** を追加
- 親IRページ → iframe → player HTML / JS / JSON設定 → `.m3u8` / `.mp4` / `.mp3` を探索
- メディアURLがHTMLから取れない場合、iframe player URLを `yt-dlp` でも試行
- 一般IR動画ページから企業名・決算期・説明会日をbest-effort自動入力
- v13のVimeo native downloader修正を維持

## 対応
YouTube / Vimeo / SmartVision IR / IR Webcasting / m3u8 / ts / mp3・m4a / mp4 / 一般IRページ

## Streamlit Cloud
リポジトリ直下に `app.py`, `requirements.txt`, `packages.txt`, `README.md` を置き、Python 3.11を選択してください。
Secrets:

```toml
OPENAI_API_KEY = "sk-..."
```

## SmartVisionの使い方
一覧ページではなく、原則として **個別の決算説明会動画ページURL** を貼るのが最も確実です。
SmartVisionは企業サイトに直接埋め込まれる構成があるため、v14はiframeとプレイヤー設定を追跡します。

Cookie / ログイン / 署名付きセッション / DRMが必要な配信は自動取得できない場合があります。その場合のみDevTools Networkで `.m3u8` / `.mp4` 等を取得してください。
