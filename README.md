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
