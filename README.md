# 決算画像ジェネレーター v46 Deploy

このフォルダは、そのまま GitHub にアップロードして Streamlit Community Cloud でデプロイできる構成です。

## ファイル構成
- `app.py` : Streamlit本体
- `requirements.txt` : Python依存ライブラリ
- `packages.txt` : Linux用日本語フォント
- `.streamlit/config.toml` : Streamlit Cloud向け設定
- `.gitignore`

## デプロイ手順
1. GitHubで新しいリポジトリを作成  
   例: `earnings-image-generator`
2. このZIPを展開し、中身をすべてリポジトリ直下へアップロード
3. Streamlit Community Cloudへログイン
4. `Create app` を選択
5. 以下を指定
   - Repository: 作成したGitHubリポジトリ
   - Branch: `main`
   - Main file path: `app.py`
6. `Deploy` を押す

## 更新
今後はGitHubの`app.py`などを更新してpushすれば、オンライン版へ反映されます。

## 日本語フォント
Streamlit CloudはLinux環境なので、`packages.txt`で`fonts-noto-cjk`を導入します。
`app.py`もNoto Sans CJK JPを優先して使う設定です。

## ローカル実行
```bash
pip install -r requirements.txt
streamlit run app.py
```

## v47 の変更点
- セグメント売上高・セグメント利益の凡例文字を 16pt → 14pt に縮小
- 凡例の行数に応じてグラフ上部に余白を自動確保し、棒グラフと重なりにくい配置へ変更
- CSV の数値列を読み込み時に正規化
  - 通常の数値
  - 小数
  - `1,234` のような桁区切り
  - `１，２３４` のような全角数字
  - `(1,234)` のような負数表記
  - `￥2,500` などの通貨記号付き
- CSVアップロード後だけでなく、画面上で編集・貼り付けした数値も同じ処理を通すように変更

## v49 の変更点
- 会社全体・セグメント売上高・セグメント利益・受注高/受注残高でサブタイトルを自由入力可能
- サブタイトルを空欄にすると画像上でも非表示
- CSVメタデータ `__subtitle` でサブタイトルを保存・再利用可能
- 画像右上の「最新期（最新）」表示を全グラフから削除
