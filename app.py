import os
import re
import sys
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="IR Webcast Transcriber v14", page_icon="🎧", layout="centered")
st.title("🎧 IR Webcast Transcriber v14")
st.caption("音声取得はStreamlit、文字起こしは外部API。YouTube / Vimeo / SmartVision IR / MP3 / M3U8 / TS / IR webcastページ / IR Webcasting、英・中・韓・日に対応。")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/151 Safari/537.36"
OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-6000:])
    return p


def kind(url):
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "m3u8"
    if path.endswith(".ts"):
        return "ts"
    if path.endswith((".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac")):
        return "audio"
    if path.endswith((".mp4", ".webm", ".mov", ".mkv")):
        return "video"
    return None


def is_youtube(url):
    host = urlparse(url).netloc.lower()
    return "youtube.com" in host or "youtu.be" in host


def is_vimeo(url):
    host = urlparse(url).netloc.lower()
    return host == "vimeo.com" or host.endswith(".vimeo.com") or host == "player.vimeo.com"


def yt_dlp_available():
    p = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode == 0


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def ffmpeg_get(url, out_path):
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-user_agent", UA, "-i", url,
        "-vn", "-c:a", "libmp3lame", "-b:a", "64k", str(out_path),
    ])
    return out_path


def yt_dlp_audio_get(url, out_path):
    """YouTube等: yt-dlpに音声抽出まで任せる従来ルート。"""
    template = str(out_path.with_suffix(".%(ext)s"))
    run([
        sys.executable, "-m", "yt_dlp", "--no-playlist", "-x",
        "--audio-format", "mp3", "--audio-quality", "5", "-o", template, url,
    ])
    candidates = list(out_path.parent.glob(out_path.stem + ".mp3"))
    if not candidates:
        raise RuntimeError("yt-dlpで音声ファイルを作成できませんでした。公開範囲・ログイン・埋め込み制限を確認してください。")
    return candidates[0]


def vimeo_audio_get(url, out_path):
    """Vimeo専用。

    VimeoのDASH/range配信では、CDNのrange断片URLをffmpegへ直接渡すと
    `trun track id unknown / no tfhd` になることがある。
    そこで、まずyt-dlpのnative downloaderで断片をローカルへ完全取得・結合し、
    その完成済みローカルファイルだけをffmpegへ渡してMP3化する。
    """
    work = out_path.parent / "vimeo_download"
    work.mkdir(exist_ok=True)
    template = str(work / "source.%(ext)s")

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--downloader", "http:native",
        "--downloader", "dash,m3u8:native",
        "--retries", "5",
        "--fragment-retries", "10",
        "--abort-on-unavailable-fragments",
        "-f", "bestaudio/best",
        "-o", template,
        url,
    ]
    run(cmd)

    # yt-dlpが作成した完成済みファイルのみを対象にする。
    files = [x for x in work.iterdir() if x.is_file() and not x.name.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError("Vimeoのメディアをローカルへ取得できませんでした。")
    src = max(files, key=lambda x: x.stat().st_size)

    # CDN URLではなくローカルファイルをffmpegに入力するのが重要。
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", "64k", str(out_path),
    ])
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("Vimeo音声のMP3変換に失敗しました。")
    return out_path


def looks_like_m3u8_response(r):
    if r.status_code >= 400:
        return False
    ct = (r.headers.get("content-type") or "").lower()
    head = r.text[:200]
    return "mpegurl" in ct or head.lstrip().startswith("#EXTM3U")


def infer_m3u8_candidates_from_ts(ts_url):
    parsed = urlparse(ts_url)
    path = parsed.path
    directory, filename = path.rsplit("/", 1) if "/" in path else ("", path)
    names = [re.sub(r"\.ts$", ".m3u8", filename, flags=re.I)]
    stem = re.sub(r"\.ts$", "", filename, flags=re.I)
    bases = [stem]
    for pat in (r"_part\d+$", r"_seg(?:ment)?[_-]?\d+$", r"[_-]\d+$"):
        b = re.sub(pat, "", stem, flags=re.I)
        if b != stem:
            bases.append(b)
    for b in bases:
        names.extend([f"{b}.m3u8", f"{b}_playlist.m3u8", f"{b}_index.m3u8"])
    for b in list(bases):
        b2 = re.sub(r"_audio$", "", b, flags=re.I)
        if b2 != b:
            names.extend([f"{b2}.m3u8", f"{b2}_audio.m3u8"])
    out = []
    for name in names:
        new_path = (directory + "/" + name) if directory else name
        candidate = parsed._replace(path=new_path).geturl()
        if candidate not in out:
            out.append(candidate)
    return out


def find_parent_m3u8_from_ts(ts_url):
    headers = {"User-Agent": UA, "Referer": ts_url}
    for candidate in infer_m3u8_candidates_from_ts(ts_url):
        try:
            r = requests.get(candidate, headers=headers, timeout=8)
            if looks_like_m3u8_response(r):
                return candidate
        except requests.RequestException:
            pass
    return None


def ts_sequence_get(first_ts_url, out_path, progress_text=None):
    m = re.search(r"^(.*?)(\d+)(\.ts(?:\?.*)?)$", first_ts_url, re.I)
    if not m:
        raise RuntimeError("TS断片URLから連番パターンを判定できませんでした。m3u8 URLを貼ってください。")
    prefix, start_num, suffix = m.group(1), int(m.group(2)), m.group(3)
    headers = {"User-Agent": UA, "Referer": first_ts_url}
    combined = out_path.with_suffix(".combined.ts")
    count = 0
    misses = 0
    max_segments = 3000
    with open(combined, "wb") as wf:
        for n in range(start_num, start_num + max_segments):
            seg_url = f"{prefix}{n}{suffix}"
            try:
                r = requests.get(seg_url, headers=headers, timeout=15)
            except requests.RequestException:
                r = None
            if r is None or r.status_code >= 400 or not r.content:
                misses += 1
                if count > 0 and misses >= 2:
                    break
                continue
            misses = 0
            wf.write(r.content)
            count += 1
            if progress_text is not None:
                progress_text.caption(f"TS断片を取得中… {count} ファイル")
    if count == 0:
        raise RuntimeError("TS断片を1つも取得できませんでした。")
    ffmpeg_get(str(combined), out_path)
    try:
        combined.unlink()
    except OSError:
        pass
    return out_path, count


def is_irwebcasting(url):
    return "irwebcasting.com" in urlparse(url).netloc.lower()


def extract_irwebcasting_metadata(page_url):
    """Best-effort extraction of company / period / event date from IR Webcasting HTML."""
    r = requests.get(page_url, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = "\n".join(x.strip() for x in soup.stripped_strings if x.strip())
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    company = ""
    period = ""
    event_date = ""
    # Company often appears first in the page title/body.
    if title:
        company = re.split(r"\s{2,}|\s+-\s+", title)[0].strip()
        company = re.sub(r"\s+(?:20\d{2}年|FY20\d{2}).*$", "", company).strip()
    if not company:
        for line in text.splitlines()[:10]:
            if any(k in line for k in ("株式会社", "Inc.", "Corporation", "Limited", "Ltd.")):
                company = line.strip(); break
    # Typical Japanese title: 2026年9月期 第3四半期 決算説明会
    m = re.search(r"(20\d{2}年\d{1,2}月期\s*(?:第?\s*[1-4１-４]四半期|通期|上期|下期)?)", text)
    if m:
        period = re.sub(r"\s+", " ", m.group(1)).strip()
    else:
        m = re.search(r"(FY\s*20\d{2}\s*(?:Q[1-4]|[1-4]Q)?)", text, re.I)
        if m: period = re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if m:
        event_date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return {"company": company, "period": period, "date": event_date}


def _media_urls_from_text(text, base_url):
    text = (text or "").replace("\\/", "/")
    found = []
    # absolute and protocol-relative media URLs
    abs_re = re.compile(r'(?:(?:https?:)?//)[^\s"\'<>\\]+?\.(?:m3u8|mp3|m4a|aac|mp4|webm|mov)(?:\?[^\s"\'<>\\]*)?', re.I)
    for x in abs_re.findall(text):
        if x.startswith("//"):
            x = urlparse(base_url).scheme + ":" + x
        found.append(x.replace("&amp;", "&"))
    rel_re = re.compile(r'["\']([^"\']+\.(?:m3u8|mp3|m4a|aac|mp4|webm|mov)(?:\?[^"\']*)?)["\']', re.I)
    found += [urljoin(base_url, x.replace("&amp;", "&")) for x in rel_re.findall(text)]
    return found


def discover_irwebcasting_media(page_url):
    """IR Webcasting-specific discovery: HTML + referenced same-site JS/config files."""
    headers = {"User-Agent": UA, "Referer": page_url}
    r = requests.get(page_url, headers=headers, timeout=25)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    found = _media_urls_from_text(html, page_url)

    # Inspect inline scripts plus external JS/config files. Many IR Webcasting pages
    # keep the movie path in player configuration rather than a visible <video> tag.
    assets = []
    for tag in soup.find_all("script"):
        src = tag.get("src")
        if src:
            assets.append(urljoin(page_url, src))
        else:
            found += _media_urls_from_text(tag.get_text(" ", strip=False), page_url)
    for tag in soup.find_all(["iframe", "source", "video", "audio", "a"]):
        for attr in ("src", "href", "data-src", "data-url", "data-file", "data-movie"):
            v = tag.get(attr)
            if v:
                full = urljoin(page_url, v)
                if kind(full): found.append(full)
                elif "irwebcasting" in urlparse(full).netloc.lower(): assets.append(full)

    # Keep crawling deliberately shallow to avoid scraping the whole site.
    seen_assets = set()
    for asset in assets[:30]:
        if asset in seen_assets: continue
        seen_assets.add(asset)
        try:
            rr = requests.get(asset, headers=headers, timeout=12)
            if rr.status_code < 400 and len(rr.content) < 5_000_000:
                found += _media_urls_from_text(rr.text, asset)
        except requests.RequestException:
            pass

    unique=[]
    for x in found:
        if x not in unique: unique.append(x)
    def score(x):
        lx=x.lower()
        if ".m3u8" in lx: return 0
        if re.search(r"\.(mp3|m4a|aac)", lx): return 1
        return 2
    return sorted(unique, key=score)


def is_smartvision_url(url):
    """SmartVision IR / iVision系のプレイヤー・配信URLをbest-effort判定。"""
    host = urlparse(url).netloc.lower()
    low = url.lower()
    return (
        "ivision.ne.jp" in host
        or "smartvision" in low
        or "smart-vision" in low
    )


def extract_generic_ir_metadata(page_url):
    """一般的な日本企業IR動画ページから企業名・決算期をbest-effort抽出。"""
    try:
        r = requests.get(page_url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
    except Exception:
        return {"company": "", "period": "", "date": ""}
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = "\n".join(soup.stripped_strings)
    company = ""
    # title末尾のサイト名を落とす。完全自動化より誤入力回避を優先するbest-effort。
    if "サンリオ" in title or "サンリオ" in text[:3000]:
        company = "サンリオ"
    else:
        m = re.search(r"(?:株式会社\s*)?([^|｜\-]{2,40}(?:株式会社|Inc\.|Corporation|Ltd\.))", title)
        if m: company = m.group(1).strip()
    period = ""
    m = re.search(r"(20\d{2}年\s*\d{1,2}月期\s*(?:第?\s*[1-4１-４]四半期|通期|上期|下期)?)", text)
    if m: period = re.sub(r"\s+", " ", m.group(1)).strip()
    event_date = ""
    m = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if m: event_date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return {"company": company, "period": period, "date": event_date}


def discover_nested_player_media(page_url, max_depth=2):
    """SmartVisionのようなiframe/JS埋め込みプレイヤーを浅く再帰探索する。

    親IRページ -> iframe -> player HTML/config/JS -> m3u8/mp4/mp3 の順で探索。
    Cookie/session/DRMが必要な配信は対象外。
    """
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    queue = [(page_url, 0, page_url)]
    seen = set()
    media = []
    player_pages = []
    while queue and len(seen) < 40:
        current, depth, referer = queue.pop(0)
        if current in seen: continue
        seen.add(current)
        try:
            rr = session.get(current, headers={"Referer": referer}, timeout=15)
            if rr.status_code >= 400 or len(rr.content) > 6_000_000:
                continue
        except requests.RequestException:
            continue
        ctype=(rr.headers.get("content-type") or "").lower()
        if "mpegurl" in ctype or rr.text.lstrip().startswith("#EXTM3U"):
            media.append(current); continue
        text=rr.text.replace("\\/", "/")
        media += _media_urls_from_text(text, current)
        if depth >= max_depth: continue
        soup=BeautifulSoup(text, "html.parser")
        links=[]
        for tag in soup.find_all(["iframe","script","source","video","audio"]):
            for attr in ("src","data-src","data-url","data-file","data-movie","data-player-url"):
                v=tag.get(attr)
                if not v: continue
                full=urljoin(current,v)
                if kind(full): media.append(full)
                elif full.startswith("http"):
                    links.append(full)
                    if tag.name == "iframe": player_pages.append(full)
        # JSON/config URLも拾う。SmartVision実装差分への保険。
        for v in re.findall(r'["\']([^"\']+\.(?:json|js)(?:\?[^"\']*)?)["\']', text, re.I):
            links.append(urljoin(current,v))
        for full in links[:25]:
            if full not in seen:
                queue.append((full, depth+1, current))
    unique=[]
    for x in media:
        if x not in unique: unique.append(x)
    def score(x):
        lx=x.lower()
        if ".m3u8" in lx: return 0
        if re.search(r"\.(mp3|m4a|aac)",lx): return 1
        return 2
    return sorted(unique,key=score), player_pages


def discover_media(page_url):
    if is_irwebcasting(page_url):
        special = discover_irwebcasting_media(page_url)
        if special:
            return special
    # SmartVision等のiframe埋め込みを先に再帰探索。
    nested, _player_pages = discover_nested_player_media(page_url, max_depth=2)
    if nested:
        return nested
    r = requests.get(page_url, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    html = r.text.replace("\\/", "/")
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for tag in soup.find_all(["audio", "video", "source", "a", "iframe", "script"]):
        for attr in ("src", "href", "data-src", "data-url", "data-file", "data-media", "data-audio"):
            value = tag.get(attr)
            if value:
                full = urljoin(page_url, value)
                if kind(full):
                    found.append(full)
    absolute_re = re.compile(r'https?://[^\s"\'<>\\]+?\.(?:m3u8|mp3|m4a|aac|mp4|webm)(?:\?[^\s"\'<>\\]*)?', re.I)
    found += [x.replace("&amp;", "&") for x in absolute_re.findall(html)]
    relative_re = re.compile(r'["\']([^"\']+\.(?:m3u8|mp3|m4a|aac|mp4|webm)(?:\?[^"\']*)?)["\']', re.I)
    found += [urljoin(page_url, x) for x in relative_re.findall(html)]
    unique = []
    for x in found:
        if x not in unique:
            unique.append(x)
    def score(x):
        lx = x.lower()
        if ".m3u8" in lx:
            return 0
        if re.search(r"\.(mp3|m4a|aac)", lx):
            return 1
        return 2
    return sorted(unique, key=score)


def get_duration_seconds(path):
    p = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    try:
        return float(p.stdout.strip())
    except Exception:
        return 0.0


def make_api_chunks(audio_path, out_dir, chunk_minutes=12):
    """Create small mono 16kHz MP3 chunks to keep API uploads and RAM usage modest."""
    pattern = str(out_dir / "chunk_%03d.mp3")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(audio_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(int(chunk_minutes * 60)),
        "-reset_timestamps", "1", pattern,
    ])
    chunks = sorted(out_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise RuntimeError("API送信用の音声分割に失敗しました。")
    return chunks


def secret_api_key():
    try:
        return str(st.secrets.get("OPENAI_API_KEY", "") or "").strip()
    except Exception:
        return ""


def transcribe_api_chunk(path, api_key, model, language, prompt="", retries=3):
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {"model": model, "response_format": "json"}
    if language != "auto":
        data["language"] = language
    if prompt.strip():
        data["prompt"] = prompt.strip()[:1000]

    last_error = None
    for attempt in range(retries):
        try:
            with open(path, "rb") as f:
                files = {"file": (path.name, f, "audio/mpeg")}
                r = requests.post(OPENAI_TRANSCRIBE_URL, headers=headers, data=data, files=files, timeout=900)
            if r.status_code == 200:
                payload = r.json()
                return (payload.get("text") or "").strip()
            msg = r.text[-3000:]
            last_error = RuntimeError(f"OpenAI API {r.status_code}: {msg}")
            if r.status_code not in (408, 409, 429, 500, 502, 503, 504):
                break
        except requests.RequestException as e:
            last_error = e
        if attempt < retries - 1:
            time.sleep(2 ** attempt * 2)
    raise RuntimeError(str(last_error or "文字起こしAPIへの送信に失敗しました。"))


def fmt_time(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds//3600:02d}:{(seconds%3600)//60:02d}:{seconds%60:02d}"


def transcribe_via_api(audio_path, api_key, model, language, timestamps, prompt, progress_bar, progress_text):
    duration = get_duration_seconds(audio_path)
    chunk_minutes = 12
    chunk_seconds = chunk_minutes * 60
    with tempfile.TemporaryDirectory() as cd:
        chunks = make_api_chunks(audio_path, Path(cd), chunk_minutes=chunk_minutes)
        lines = []
        for i, chunk in enumerate(chunks):
            start = i * chunk_seconds
            end = min(duration if duration > 0 else (i + 1) * chunk_seconds, (i + 1) * chunk_seconds)
            progress_text.caption(f"文字起こしAPIへ送信中… {i+1}/{len(chunks)}  |  {fmt_time(start)}〜{fmt_time(end)}")
            text = transcribe_api_chunk(chunk, api_key, model, language, prompt=prompt)
            if text:
                if timestamps:
                    lines.append(f"[{fmt_time(start)} - {fmt_time(end)}]\n{text}")
                else:
                    lines.append(text)
            progress_bar.progress(int(((i + 1) / len(chunks)) * 100))
        progress_text.caption(f"文字起こし 100%  |  API処理完了（{len(chunks)}分割）")
        return "\n\n".join(lines).strip() + "\n"


def safe_filename(text):
    text = (text or "").strip()
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    return text[:80] or "untagged"


url = st.text_input("URL", placeholder="YouTube / Vimeo / .mp3 / .m3u8 / .ts / IR Webcasting / Chorus Call / teletogether 等")

auto_meta = {"company":"", "period":"", "date":""}
if url.strip() and is_irwebcasting(url.strip()):
    try:
        auto_meta = extract_irwebcasting_metadata(url.strip())
        st.caption("IR Webcastingを検出：企業名・決算期・説明会日を自動取得します。")
    except Exception as e:
        st.caption(f"IR Webcastingのタグ自動取得に失敗しました（手入力は可能です）: {e}")
elif url.strip():
    # SmartVisionを含む一般IR動画ページでもタグをbest-effort取得。
    try:
        auto_meta = extract_generic_ir_metadata(url.strip())
        if any(auto_meta.values()):
            st.caption("IR動画ページから企業名・決算期などを自動取得しました（必要なら修正できます）。")
    except Exception:
        pass

st.subheader("決算説明会タグ")
m1, m2 = st.columns(2)
with m1:
    company = st.text_input("企業名", value=auto_meta.get("company", ""), placeholder="例: NVIDIA / SK hynix / Palantir")
with m2:
    earnings_period = st.text_input("決算期", value=auto_meta.get("period", ""), placeholder="例: FY2027 Q2 / 2026年2Q")
m3, m4 = st.columns(2)
with m3:
    earnings_date = st.text_input("説明会日", value=auto_meta.get("date", ""), placeholder="例: 2026-08-26")
with m4:
    event_type = st.selectbox("種別", ["決算説明会", "決算発表", "Investor Day", "その他"], index=0)

st.subheader("文字起こしAPI")
st.caption("APIキーはGitHubに書かず、Streamlit CloudのSecretsに OPENAI_API_KEY として保存するのがおすすめです。")
configured_key = secret_api_key()
api_key_input = st.text_input(
    "OpenAI API Key",
    type="password",
    placeholder="Secrets設定済みなら空欄でOK",
    help="この入力値はセッション内でのみ使います。GitHubへ保存しません。",
)
api_key = api_key_input.strip() or configured_key
if configured_key and not api_key_input.strip():
    st.success("Streamlit Secrets の OPENAI_API_KEY を使用します。")

c1, c2 = st.columns(2)
with c1:
    api_model = st.selectbox(
        "文字起こしモデル",
        ["gpt-4o-transcribe", "gpt-4o-mini-transcribe"],
        index=0,
        help="高精度なら gpt-4o-transcribe、軽量・低コスト寄りなら mini。",
    )
with c2:
    language = st.selectbox(
        "言語", ["en", "zh", "ko", "ja", "auto"], index=0,
        format_func=lambda x: {"en":"英語","zh":"中国語（標準中国語）","ko":"韓国語","ja":"日本語","auto":"自動判定"}[x],
    )

prompt = st.text_input(
    "固有名詞ヒント（任意）",
    placeholder="例: NVIDIA, Blackwell, CUDA, Jensen Huang",
    help="企業名・製品名などを入れると固有名詞認識の補助になります。",
)
timestamps = st.checkbox("タイムスタンプ（12分ごとのチャンク単位）", True)

with st.expander("環境チェック", expanded=False):
    st.write(f"Python: `{sys.version.split()[0]}`")
    st.write("ffmpeg:", "✅" if ffmpeg_available() else "❌")
    st.write("yt-dlp:", "✅" if yt_dlp_available() else "❌")
    st.write("API key:", "✅" if api_key else "❌")
    st.write("ローカルWhisper:", "使用しません")

if not ffmpeg_available():
    st.error("ffmpeg が見つかりません。Streamlit Cloudでは packages.txt に ffmpeg が必要です。")

if st.button("文字起こし開始", type="primary", use_container_width=True):
    if not url.strip():
        st.warning("URLを貼ってください。")
        st.stop()
    if not api_key:
        st.error("OpenAI API Key が必要です。画面に入力するか、Streamlit Secrets に OPENAI_API_KEY を設定してください。")
        st.stop()
    if (is_youtube(url) or is_vimeo(url)) and not yt_dlp_available():
        st.error("yt-dlp が見つかりません。requirements.txt を確認してください。")
        st.stop()

    status = st.empty()
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            audio = td / "webcast.mp3"
            source_kind = kind(url)

            if is_youtube(url):
                status.info("YouTubeから音声取得中…")
                audio = yt_dlp_audio_get(url, audio)
                source = "YouTube"
            elif is_vimeo(url):
                status.info("Vimeoから音声取得中…（断片をローカル結合してからMP3化）")
                try:
                    audio = vimeo_audio_get(url, audio)
                except Exception as e:
                    raise RuntimeError(
                        "Vimeoから音声を取得できませんでした。公開動画なら通常は取得できますが、"
                        "ログイン必須・パスワード付き・埋め込み先限定の動画ではCookie等が必要な場合があります。\n"
                        + str(e)
                    )
                source = "Vimeo"
            elif source_kind == "ts":
                status.info("TS断片URLを検出。親のM3U8を探索中…")
                parent = find_parent_m3u8_from_ts(url)
                if parent:
                    st.success("親のM3U8を自動検出しました。")
                    st.code(parent)
                    status.info("M3U8から音声取得中…")
                    audio = ffmpeg_get(parent, audio)
                    source = "TS → M3U8"
                else:
                    status.info("親M3U8が見つからないため、TS連番を直接取得して結合します…")
                    ts_progress = st.empty()
                    audio, seg_count = ts_sequence_get(url, audio, progress_text=ts_progress)
                    ts_progress.caption(f"TS断片の取得完了: {seg_count} ファイル")
                    source = f"TS連番 ({seg_count} segments)"
            elif source_kind:
                status.info("音声取得中…")
                audio = ffmpeg_get(url, audio)
                source = source_kind.upper()
            else:
                status.info("IR Webcasting / SmartVision / IRページ内の音声URLを探索中…")
                nested_candidates, player_pages = discover_nested_player_media(url, max_depth=2)
                candidates = nested_candidates or discover_media(url)
                if not candidates and player_pages and yt_dlp_available():
                    # プレイヤーHTML自体をyt-dlpが解決できる実装へのフォールバック。
                    last_player_error = None
                    for player_url in player_pages[:5]:
                        try:
                            status.info("SmartVision系プレイヤーをyt-dlpで解析中…")
                            audio = yt_dlp_audio_get(player_url, audio)
                            source = "SmartVision/iframe → yt-dlp"
                            break
                        except Exception as e:
                            last_player_error = e
                    else:
                        st.error("SmartVision/IRページからメディアを自動検出できませんでした。")
                        if last_player_error:
                            st.code(str(last_player_error))
                        st.info("Cookie・署名URL・DRM型の場合は、DevToolsのNetworkから m3u8 / mp3 / mp4 を取得して貼ってください。")
                        st.stop()
                    candidates = []
                elif not candidates:
                    st.error("自動検出できませんでした。JavaScript / Cookie / 認証型の可能性があります。")
                    st.info("この場合だけDevToolsのNetworkから m3u8 / mp3 / mp4 / ts を取得して貼ってください。")
                    st.stop()
                if candidates:
                    with st.expander("検出したメディアURL"):
                        for x in candidates[:20]:
                            st.code(x)
                    last_error = None
                    for x in candidates[:10]:
                        try:
                            audio = ffmpeg_get(x, audio)
                            source = "IRページ → " + (kind(x) or "media").upper()
                            break
                        except Exception as e:
                            last_error = e
                    else:
                        raise RuntimeError("候補は見つかりましたが音声取得に失敗しました。\n" + str(last_error))

            duration = get_duration_seconds(audio)
            status.info(f"{source}: 音声取得完了（約{duration/60:.1f}分）。外部APIで文字起こし中…")
            progress_text = st.empty()
            progress_bar = st.progress(0)
            text = transcribe_via_api(
                audio, api_key, api_model, language, timestamps, prompt,
                progress_bar=progress_bar, progress_text=progress_text,
            )

            tags = []
            if company.strip(): tags.append(f"企業名: {company.strip()}")
            if earnings_period.strip(): tags.append(f"決算期: {earnings_period.strip()}")
            if earnings_date.strip(): tags.append(f"説明会日: {earnings_date.strip()}")
            if event_type: tags.append(f"種別: {event_type}")
            tags.append(f"URL: {url}")
            tags.append(f"Transcription model: {api_model}")
            metadata = "# IR Webcast Metadata\n" + "\n".join(f"- {x}" for x in tags)
            transcript_with_tags = metadata + "\n\n# Transcript\n" + text

            company_part = safe_filename(company)
            period_part = safe_filename(earnings_period)
            base_name = f"{company_part}_{period_part}"
            if earnings_date.strip():
                base_name += f"_{safe_filename(earnings_date)}"

            elapsed = (time.time() - t0) / 60
            status.success(f"完了（{elapsed:.1f}分）")
            st.subheader("タグ")
            tag_cols = st.columns(2)
            with tag_cols[0]:
                st.write(f"**企業名:** {company.strip() or '未設定'}")
                st.write(f"**決算期:** {earnings_period.strip() or '未設定'}")
            with tag_cols[1]:
                st.write(f"**説明会日:** {earnings_date.strip() or '未設定'}")
                st.write(f"**種別:** {event_type}")

            st.text_area("Transcript", transcript_with_tags, height=450)
            st.download_button(
                "TXTをダウンロード", transcript_with_tags.encode("utf-8"),
                f"{base_name}_transcript.txt", "text/plain", use_container_width=True,
            )
            with open(audio, "rb") as af:
                audio_bytes = af.read()
            st.download_button(
                "MP3もダウンロード", audio_bytes, f"{base_name}.mp3", "audio/mpeg", use_container_width=True,
            )
    except Exception as e:
        st.error("処理に失敗しました。")
        st.code(str(e))

with st.expander("v14のポイント"):
    st.markdown("""
- **SmartVision IR / iframe埋め込み**を追加。親IRページ→iframe→プレイヤーHTML/JS/設定→m3u8/mp4/mp3を浅く再帰探索します。
- **IR Webcasting (`irwebcasting.com`) 専用解析**も継続。
- IR Webcastingでは **企業名・決算期・説明会日を自動入力**します。
- Streamlit Cloud上では **Whisperモデルを一切ロードしません**。
- 取得した音声を12分ごとの軽量MP3に分割し、1本ずつ文字起こしAPIへ送信します。
- そのため、v8/v9で問題になったローカルWhisperのRAM使用量を大幅に減らせます。
- `.ts` は親M3U8の自動探索 → 見つからなければ連番TS結合を試します。
- タイムスタンプはAPI版では **12分チャンク単位** です。
- APIキーはGitHubへコミットしないでください。Streamlit CloudのSecrets利用を推奨します。
""")
