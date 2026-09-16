# IR Webcast Transcriber v13

Streamlit Cloud向けのIR Webcast文字起こしアプリです。

## v13の変更点
- VimeoのDASH/range配信で `trun track id unknown` / `no tfhd was found` が出るケースを修正。
- VimeoではCDNのrange断片URLをffmpegへ直接渡しません。
- `yt-dlp` のnative downloaderでHTTP/DASH/HLS断片をまずローカルへ完全取得・結合し、その完成済みローカルファイルをffmpegでMP3化します。
- YouTube / M3U8 / TS / MP3・MP4 / IR Webcasting / 一般IRページ / OpenAI文字起こしAPIの既存機能は維持しています。

## Streamlit Cloud
- Python 3.11推奨
- Secretsに `OPENAI_API_KEY = "..."` を設定
- `packages.txt` により ffmpeg を導入

## Vimeoの注意
公開動画を主対象にしています。ログイン必須、パスワード付き、埋め込み先限定、DRM等は取得できない場合があります。
