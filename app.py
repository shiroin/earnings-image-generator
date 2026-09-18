
import io
import re
import unicodedata
import requests
import numpy as np
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.colors as mcolors
from matplotlib.ticker import FuncFormatter
from matplotlib.patches import FancyBboxPatch, Rectangle

st.set_page_config(page_title="決算画像ジェネレーター v71 Deploy", layout="wide")

def set_japanese_font():
    candidates = [
        "Hiragino Sans","Hiragino Kaku Gothic ProN","Yu Gothic","YuGothic",
        "Meiryo","Noto Sans CJK JP","Noto Sans JP","IPAexGothic","IPAGothic"
    ]
    installed = {f.name for f in fm.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False

set_japanese_font()

THEME = {
    "bg":"#F3F4F6",
    "text":"#17325C","muted":"#61728A","grid":"#DCE3EC","axis":"#AEB9C7",
    "revenue":"#1675F8","profit":"#08A89C","margin":"#FF8A00",
    "revenue_bg":"#F1F6FF","profit_bg":"#F0FAF8","margin_bg":"#FFF7EE",
    "card_border":"#DCE3EC",
    "segment_palette":["#1675F8","#20B779","#FFA126","#F45B73","#8B5BD6",
                       "#0EA5E9","#84CC16","#D946EF","#64748B"]
}

# マスターの財務数値は、特記がない限り「百万通貨単位」で入力する前提。
# 例: JPY=百万円、USD=百万ドル、CNY=百万元、EUR=百万ユーロ。
# したがって百万→億は x0.01、百万→10億は x0.001 で表示変換する。
LOCAL_UNITS={
    "JPY":{"百万円":1.0,"億円":0.01,"十億円":0.001,"兆円":0.000001,"100兆円":0.00000001,"京円":0.0000000001},
    "USD":{"百万ドル":1.0,"億ドル":0.01,"10億ドル":0.001},
    "EUR":{"百万ユーロ":1.0,"億ユーロ":0.01,"10億ユーロ":0.001},
    "CNY":{"百万元":1.0,"億元":0.01,"10億元":0.001},
    "DKK":{"百万クローネ":1.0,"億クローネ":0.01,"10億クローネ":0.001},
    "KRW":{"百万ウォン":1.0,"億ウォン":0.01,"10億ウォン":0.001,"兆ウォン":0.000001,"100兆ウォン":0.00000001,"京ウォン":0.0000000001},
    "NOK":{"百万クローネ":1.0,"億クローネ":0.01,"10億クローネ":0.001},
    "SEK":{"百万クローナ":1.0,"億クローナ":0.01,"10億クローナ":0.001},
    "CHF":{"百万スイスフラン":1.0,"億スイスフラン":0.01,"10億スイスフラン":0.001},
    "TWD":{"百万台湾ドル":1.0,"億台湾ドル":0.01,"10億台湾ドル":0.001},
    "HKD":{"百万香港ドル":1.0,"億香港ドル":0.01,"10億香港ドル":0.001}
}
JPY_UNITS={"百万円":1.0,"億円":0.01,"十億円":0.001,"兆円":0.000001,"100兆円":0.00000001,"京円":0.0000000001}
DEFAULT_LOCAL={"JPY":"億円","USD":"億ドル","EUR":"億ユーロ","CNY":"億元","DKK":"億クローネ","KRW":"億ウォン","NOK":"億クローネ","SEK":"億クローナ","CHF":"億スイスフラン","TWD":"億台湾ドル","HKD":"億香港ドル"}
ASPECTS={"16:9":(16,9),"4:3":(12,9),"3:2":(15,10),"1:1":(10,10),"9:16":(9,16)}


META_PREFIX="__"
META_INPUT_CURRENCY="__input_currency"
META_DISPLAY_UNIT="__display_unit"
META_REVENUE_COLOR="__revenue_color"
META_OPERATING_PROFIT_COLOR="__operating_profit_color"
META_MARGIN_COLOR="__margin_color"  # optional / backward-compatible extension
META_ORDERS_COLOR="__orders_color"
META_BACKLOG_COLOR="__backlog_color"
META_SUBTITLE="__subtitle"
META_SEGMENT_PREFIX="__color_"

def _first_nonempty(series):
    for v in series:
        if pd.notna(v) and str(v).strip():
            return str(v).strip()
    return None

def normalize_color(value, fallback=None):
    if value is None or (isinstance(value,float) and pd.isna(value)):
        return fallback
    try:
        return mcolors.to_hex(str(value).strip(), keep_alpha=False).upper()
    except Exception:
        return fallback

def coerce_numeric_series(series):
    """CSV/表計算ソフト由来の数値・数値文字列を安全に数値化する。

    例: 1234 / 1234.5 / "1,234" / "１，２３４" / "(1,234)"
    """
    def _one(v):
        if pd.isna(v):
            return np.nan
        if isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool):
            return float(v)
        text=str(v).strip()
        if not text:
            return np.nan
        # Excel / Google Sheets などで見かける全角数字・全角カンマにも対応
        trans=str.maketrans("０１２３４５６７８９，．－＋％","0123456789,.-+%")
        text=text.translate(trans)
        text=text.replace("−","-").replace("–","-").replace("—","-")
        negative=text.startswith("(") and text.endswith(")")
        if negative:
            text=text[1:-1]
        # 桁区切り、空白、一般的な通貨記号を除去
        for token in (",", " ", "\u3000", "¥", "￥", "$", "€"):
            text=text.replace(token,"")
        # 万が一 % 付きでも文字列エラーにせず数値として読めるようにする
        text=text.replace("%","")
        num=pd.to_numeric(text,errors="coerce")
        if pd.isna(num):
            return np.nan
        return -float(num) if negative else float(num)
    return series.map(_one)

def normalize_numeric_columns(df):
    """period以外のデータ列を、CSVの表示形式に依存せず数値化する。"""
    out=df.copy()
    for c in out.columns:
        if str(c) != "period":
            out[c]=coerce_numeric_series(out[c])
    return out

def read_uploaded_csv(uploaded):
    """Read UTF-8/UTF-8-SIG/CP932 CSV and split reserved metadata columns."""
    if uploaded is None:
        return None, {}
    raw = uploaded.getvalue() if hasattr(uploaded, "getvalue") else uploaded
    last_error = None
    for enc in ("utf-8-sig","utf-8","cp932"):
        try:
            df = pd.read_csv(io.BytesIO(raw), encoding=enc)
            break
        except Exception as e:
            last_error = e
    else:
        raise ValueError(f"CSVを読み込めませんでした: {last_error}")

    meta = {}
    for c in df.columns:
        if str(c).startswith(META_PREFIX):
            meta[str(c)] = _first_nonempty(df[c])
    data_cols = [c for c in df.columns if not str(c).startswith(META_PREFIX)]
    data = df[data_cols].copy()
    data = normalize_numeric_columns(data)
    return data, meta

MASTER_SHEETS = {
    "company_quarterly": "会社全体_四半期",
    "company_annual": "会社全体_年度",
    "segment_revenue_quarterly": "セグメント売上高_四半期",
    "segment_revenue_annual": "セグメント売上高_年度",
    "segment_profit_quarterly": "セグメント利益_四半期",
    "segment_profit_annual": "セグメント利益_年度",
    "orders_quarterly": "受注_四半期",
    "orders_annual": "受注_年度",
    "arr_quarterly": "ARR_四半期",
    "arr_annual": "ARR_年度",
}

def master_period_key(section, ptype):
    suffix = "quarterly" if ptype == "四半期" else "annual"
    return f"{section}_{suffix}"

def _read_master_sheet_auto(xls, sheet_name, required_header):
    """マスターシートの見出し行を自動検出して読む。

    Google Sheets上で空行が削除・追加されても、固定の header=3 に依存しない。
    required_header は "period" または "setting"。データ行が0件でも安全に扱う。
    """
    raw = pd.read_excel(xls, sheet_name=sheet_name, header=None)
    if raw.empty:
        return pd.DataFrame()
    header_row = None
    target = str(required_header).strip()
    for i in range(min(len(raw), 20)):
        vals = [str(v).strip() for v in raw.iloc[i].tolist() if pd.notna(v)]
        if target in vals:
            header_row = i
            break
    if header_row is None:
        return pd.DataFrame()
    df = pd.read_excel(xls, sheet_name=sheet_name, header=header_row)
    df = df.dropna(how="all")
    # 完全空列や Unnamed 列は除外。ただし設定シート右側の可変カラーテーブルは保持する。
    keep = [c for c in df.columns if not (str(c).startswith("Unnamed:") and df[c].isna().all())]
    return df[keep] if keep else pd.DataFrame()


def read_master_excel(uploaded):
    """1社1ファイルのExcelマスターを読み込む。Google Sheetsから取得したxlsx bytesにも対応。"""
    if uploaded is None:
        return {}, {}
    raw = uploaded.getvalue() if hasattr(uploaded, "getvalue") else uploaded
    try:
        xls = pd.ExcelFile(io.BytesIO(raw))
    except Exception as e:
        raise ValueError(f"Excelマスターファイルを読み込めませんでした: {e}")

    settings = {}
    if "設定" in xls.sheet_names:
        cfg = _read_master_sheet_auto(xls, "設定", "setting")
        if {"setting","value"}.issubset(cfg.columns):
            for _, row in cfg.iterrows():
                k=row.get("setting"); v=row.get("value")
                if pd.notna(k) and str(k).strip() and pd.notna(v) and str(v).strip():
                    settings[str(k).strip()] = str(v).strip()

        # v77: 右側の可変設定表（種類 / 項目名 / 表示名 / カラー）は、
        # 左側の setting/value 表とは独立して raw シートから読む。
        # これにより列位置や空欄の影響を受けず、表示名だけの変更も確実に反映する。
        raw_cfg = pd.read_excel(xls, sheet_name="設定", header=None)
        right_header = None
        kind_col = item_col = display_col = color_col = None
        for ri in range(min(len(raw_cfg), 30)):
            vals = [str(v).strip() if pd.notna(v) else "" for v in raw_cfg.iloc[ri].tolist()]
            if "種類" in vals and "項目名" in vals:
                right_header = ri
                kind_col = vals.index("種類")
                item_col = vals.index("項目名")
                display_col = vals.index("表示名") if "表示名" in vals else None
                color_col = vals.index("カラー") if "カラー" in vals else None
                break
        if right_header is not None:
            for ri in range(right_header + 1, len(raw_cfg)):
                row = raw_cfg.iloc[ri]
                kind = row.iloc[kind_col] if kind_col < len(row) else None
                item = row.iloc[item_col] if item_col < len(row) else None
                display = row.iloc[display_col] if display_col is not None and display_col < len(row) else None
                color = row.iloc[color_col] if color_col is not None and color_col < len(row) else None
                if pd.isna(kind) or pd.isna(item):
                    continue
                kind = str(kind).strip(); item = str(item).strip()
                if not kind or not item:
                    continue
                prefix = "segment" if kind == "セグメント" else ("arr" if kind == "ARR" else None)
                if prefix is None:
                    continue
                # 表示名とカラーを独立して保存。カラーが空でも表示名は有効。
                if pd.notna(display) and str(display).strip():
                    settings[f"{prefix}_display:{item}"] = str(display).strip()
                if pd.notna(color) and str(color).strip():
                    settings[f"{prefix}_color:{item}"] = str(color).strip()

    sheets = {}
    for key, sheet_name in MASTER_SHEETS.items():
        if sheet_name in xls.sheet_names:
            df = _read_master_sheet_auto(xls, sheet_name, "period")
            if "period" in df.columns and len(df):
                df = normalize_numeric_columns(df)
                # periodだけで実データがない空テンプレートは読み込み済みデータとして扱わない
                value_cols = [c for c in df.columns if c != "period"]
                if value_cols and df[value_cols].notna().any().any():
                    sheets[key] = df
    return sheets, settings

def google_sheet_id(url):
    """Google Sheets / Google Drive の共有URLからファイルIDを抽出する。"""
    text=str(url or "").strip()
    patterns = [
        r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        m=re.search(pattern, text)
        if m:
            return m.group(1)
    raise ValueError("Google Sheets または Google Drive の共有URLを確認してください。/spreadsheets/d/... と /file/d/... の両方に対応しています。")

def _looks_like_excel_response(r):
    ctype=(r.headers.get("content-type") or "").lower()
    content=r.content or b""
    # xlsx は ZIP なので通常 PK で始まる。content-type が曖昧でも実体を優先する。
    return len(content) >= 1000 and content[:2] == b"PK" and "html" not in ctype

def read_google_sheet_master(url):
    """公開共有されたGoogle SheetsまたはDrive上のxlsxマスターを読み込む。

    まずGoogle Sheetsのxlsx exportを試し、DriveファイルURLの場合などは
    public downloadへフォールバックする。共有設定は外部閲覧可能である必要がある。
    """
    sid=google_sheet_id(url)
    candidates = [
        f"https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx",
        f"https://drive.google.com/uc?export=download&id={sid}",
    ]
    last_error=None
    for download_url in candidates:
        try:
            r=requests.get(download_url,timeout=25,allow_redirects=True)
            r.raise_for_status()
            if _looks_like_excel_response(r):
                return read_master_excel(r.content)
            last_error="取得結果がExcelファイルではありませんでした"
        except Exception as e:
            last_error=str(e)
    raise ValueError(
        "Googleマスターを取得できませんでした。Google Sheets / Drive のどちらのURLでも利用できますが、"
        "「リンクを知っている全員が閲覧可」など外部から閲覧できる共有設定が必要です。"
        + (f" 詳細: {last_error}" if last_error else "")
    )

def master_meta(settings, section):
    """設定シートを既存CSVメタデータ形式へ変換する。"""
    meta = {}
    common = {
        META_INPUT_CURRENCY: settings.get("input_currency"),
        META_DISPLAY_UNIT: settings.get("display_unit"),
    }
    meta.update({k:v for k,v in common.items() if v})
    subtitle_key = {
        "company":"company_subtitle", "segment_revenue":"segment_revenue_subtitle",
        "segment_profit":"segment_profit_subtitle", "orders":"orders_subtitle", "arr":"arr_subtitle"
    }[section]
    if settings.get(subtitle_key):
        subtitle = str(settings[subtitle_key]).strip()
        # v75: 旧マスターに残っていた既知の誤デフォルトを正規化する。
        # それ以外のユーザー指定サブタイトルはそのまま尊重する。
        legacy_subtitle_fixes = {
            "セグメント別 売上高の推": "セグメント別 売上高の推移",
            "セグメント別 営業利益の推移（円ベース）": "セグメント別 営業利益の推移",
            "セグメント別 営業利益の推移 (円ベース)": "セグメント別 営業利益の推移",
        }
        meta[META_SUBTITLE] = legacy_subtitle_fixes.get(subtitle, subtitle)
    if section=="company":
        for mk,sk in [(META_REVENUE_COLOR,"revenue_color"),(META_OPERATING_PROFIT_COLOR,"operating_profit_color"),(META_MARGIN_COLOR,"margin_color")]:
            if settings.get(sk): meta[mk]=settings[sk]
    elif section=="orders":
        for mk,sk in [(META_ORDERS_COLOR,"orders_color"),(META_BACKLOG_COLOR,"backlog_color")]:
            if settings.get(sk): meta[mk]=settings[sk]
    else:
        # v59: セグメント売上高・利益は共通の segment_color:<名称> を優先。
        # 旧v58形式も後方互換で読み込む。
        if section in ("segment_revenue", "segment_profit"):
            common_prefix = "segment_color:"
            legacy_prefix = "segment_revenue_color:" if section == "segment_revenue" else "segment_profit_color:"
            for k,v in settings.items():
                if k.startswith(legacy_prefix) and k[len(legacy_prefix):]:
                    meta[f"{META_SEGMENT_PREFIX}{k[len(legacy_prefix):]}"] = v
            for k,v in settings.items():
                if k.startswith(common_prefix) and k[len(common_prefix):]:
                    meta[f"{META_SEGMENT_PREFIX}{k[len(common_prefix):]}"] = v
        else:
            prefix = "arr_color:"
            for k,v in settings.items():
                if k.startswith(prefix) and k[len(prefix):]:
                    meta[f"{META_SEGMENT_PREFIX}{k[len(prefix):]}"] = v
    return meta

def _name_key(value):
    """Excel/Sheets由来の表記ゆれを吸収する比較キー。表示文字列自体は変更しない。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    # 改行・NBSP・全角空白を通常空白へ寄せ、連続空白を1つにする。
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()

def master_display_names(settings, section):
    """設定シートの表示名を正規化キー -> 画像上の表示名として返す。

    データ列名と設定の項目名に、大文字小文字・全角半角・余分な空白などの
    軽微な差があっても表示名を適用できるようにする。
    """
    if not settings:
        return {}
    prefix = "arr_display:" if section == "arr" else "segment_display:"
    result = {}
    for k, v in settings.items():
        if not k.startswith(prefix):
            continue
        item = k[len(prefix):]
        display = str(v).strip() if v is not None else ""
        if item and display:
            result[_name_key(item)] = display
    return result

def display_name_for(display_names, raw_name):
    """設定された表示名を返し、未設定時のみ元の列名へフォールバック。"""
    return display_names.get(_name_key(raw_name), str(raw_name))

def resolve_csv_display(meta, fallback_currency, fallback_mode, fallback_unit):
    """CSVの通貨・表示単位があれば優先。表示モードは表示単位から自動判定。"""
    currency = str(meta.get(META_INPUT_CURRENCY) or fallback_currency).upper()
    if currency not in LOCAL_UNITS:
        currency = fallback_currency

    unit = meta.get(META_DISPLAY_UNIT) or fallback_unit
    if currency == "JPY":
        mode = "現地通貨"
        if unit not in LOCAL_UNITS["JPY"]:
            unit = fallback_unit if fallback_unit in LOCAL_UNITS["JPY"] else DEFAULT_LOCAL["JPY"]
    else:
        if unit in JPY_UNITS:
            mode = "円換算"
        elif unit in LOCAL_UNITS[currency]:
            mode = "現地通貨"
        else:
            mode = fallback_mode
            valid = JPY_UNITS if mode=="円換算" else LOCAL_UNITS[currency]
            unit = fallback_unit if fallback_unit in valid else ("億円" if mode=="円換算" else DEFAULT_LOCAL[currency])
    return currency, mode, unit

def add_metadata_columns(df, meta_pairs):
    """設定値はCSVの先頭データ行だけに書く。再読込時は先頭の非空値を採用。"""
    out = df.copy()
    for key, value in meta_pairs.items():
        out[key] = ""
        if len(out):
            out.loc[out.index[0], key] = value
    return out

def company_csv_for_download(df, currency, unit, revenue_color, operating_profit_color, margin_color=None, subtitle=None):
    meta = {
        META_INPUT_CURRENCY: currency,
        META_DISPLAY_UNIT: unit,
        META_REVENUE_COLOR: normalize_color(revenue_color, THEME["revenue"]),
        META_OPERATING_PROFIT_COLOR: normalize_color(operating_profit_color, THEME["profit"]),
    }
    if margin_color is not None:
        meta[META_MARGIN_COLOR] = normalize_color(margin_color, THEME["margin"])
    if subtitle is not None:
        meta[META_SUBTITLE] = str(subtitle)
    return add_metadata_columns(df, meta)

def orders_csv_for_download(df, currency, unit, orders_color, backlog_color, subtitle=None):
    meta = {
        META_INPUT_CURRENCY: currency,
        META_DISPLAY_UNIT: unit,
        META_ORDERS_COLOR: normalize_color(orders_color, THEME["revenue"]),
        META_BACKLOG_COLOR: normalize_color(backlog_color, THEME["profit"]),
    }
    if subtitle is not None:
        meta[META_SUBTITLE] = str(subtitle)
    return add_metadata_columns(df, meta)

def segment_csv_for_download(df, currency, unit, segment_colors, subtitle=None):
    meta = {
        META_INPUT_CURRENCY: currency,
        META_DISPLAY_UNIT: unit,
    }
    for segment, color in segment_colors.items():
        meta[f"{META_SEGMENT_PREFIX}{segment}"] = normalize_color(color, "#64748B")
    if subtitle is not None:
        meta[META_SUBTITLE] = str(subtitle)
    return add_metadata_columns(df, meta)

def growth(a,b):
    if pd.isna(a) or pd.isna(b) or b==0: return None
    return (a/b-1)*100

def fmt(v):
    if pd.isna(v): return ""
    a=abs(v)
    if a>=100: return f"{v:,.0f}"
    if a>=10: return f"{v:,.1f}"
    return f"{v:,.2f}"

def choose_periods(periods,n):
    p=list(dict.fromkeys([str(x) for x in periods if pd.notna(x) and str(x).strip()]))
    return p[-n:] if len(p)>n else p

def period_labels(periods,ptype):
    latest_idx = len(periods) - 1
    if ptype=="四半期":
        # 最新四半期を必ず表示し、そこから4四半期ごと
        return [
            p if (latest_idx - i) % 4 == 0 else ""
            for i, p in enumerate(periods)
        ]
    # 年度は最新年度を必ず表示し、そこから2年ごと
    return [
        p if (latest_idx - i) % 2 == 0 else ""
        for i, p in enumerate(periods)
    ]

def fig_size(aspect,cw,ch):
    return (cw,ch) if aspect=="カスタム" else ASPECTS[aspect]

def convert(series,currency,mode,unit,fx):
    raw=pd.to_numeric(series,errors="coerce")
    if mode=="円換算":
        rate=1 if currency=="JPY" else fx[currency]
        return raw*rate*JPY_UNITS[unit]
    return raw*LOCAL_UNITS[currency][unit]

def currency_basis(currency,mode):
    if mode=="円換算":
        return "円換算"
    return {"JPY":"円ベース","USD":"USドルベース","EUR":"ユーロベース","CNY":"人民元ベース","DKK":"デンマーククローネベース","KRW":"韓国ウォンベース","NOK":"ノルウェークローネベース","SEK":"スウェーデンクローナベース","CHF":"スイスフランベース","TWD":"台湾ドルベース","HKD":"香港ドルベース"}[currency]

def style_axis(ax, labelsize=17):
    ax.set_facecolor(THEME["bg"])
    ax.grid(axis="y",color=THEME["grid"],linewidth=1,zorder=0)
    ax.grid(axis="x",visible=False)
    for s in ["top","right"]:
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color(THEME["axis"])
    ax.spines["bottom"].set_color(THEME["axis"])
    ax.tick_params(axis="both",colors=THEME["text"],labelsize=labelsize,length=0)

def note_text(fig, note, currency, mode, fx, y=0.025):
    parts=[]
    if note and str(note).strip():
        parts.append(str(note).strip())
    if mode=="円換算" and currency!="JPY":
        parts.append(f"換算レート：1 {currency} = {fx[currency]:g} 円")
    if parts:
        fig.text(0.06,y,"　".join(parts),ha="left",va="bottom",
                 fontsize=12.5,color=THEME["muted"])

def add_kpi_card(fig,x,y,w,h,title,value,delta,color,bg):
    fig.patches.append(FancyBboxPatch(
        (x,y),w,h,boxstyle="round,pad=0.004,rounding_size=0.007",
        transform=fig.transFigure,facecolor=bg,edgecolor=THEME["card_border"],
        linewidth=1.0,zorder=10
    ))
    fig.patches.append(Rectangle(
        (x,y),w*.045,h,transform=fig.transFigure,
        facecolor=color,edgecolor="none",zorder=11
    ))
    cx=x+w*.045+(w-w*.045)/2

    # KPIカード: 円・ウォンなど桁の大きい通貨でも指定単位を維持して収める。
    # 「15,345億円」「15,345億ウォン」は通常サイズ、さらに長い値だけ段階縮小。
    value_text = str(value)
    value_len = len(value_text)
    if value_len <= 11:
        value_fs = 25
    elif value_len <= 14:
        value_fs = 23
    elif value_len <= 18:
        value_fs = 21
    elif value_len <= 22:
        value_fs = 19
    else:
        value_fs = 17
    fig.text(cx,y+h*.72,title,fontsize=16,fontweight="bold",
             color=THEME["text"],ha="center",va="center",zorder=12)
    fig.text(cx,y+h*.42,value_text,fontsize=value_fs,fontweight="bold",
             color=color,ha="center",va="center",zorder=12)
    fig.text(cx,y+h*.17,delta,fontsize=14,fontweight="bold",
             color=color,ha="center",va="center",zorder=12)

def add_callout(ax,x,y,text,color,offset):
    ax.annotate(
        text,xy=(x,y),xytext=offset,textcoords="offset points",
        ha="center",va="center",fontsize=14,fontweight="bold",color="white",
        bbox=dict(boxstyle="round,pad=.28",fc=color,ec=color),
        arrowprops=dict(arrowstyle="-",color=color,lw=1.4),zorder=10
    )

def company_chart(df,company,currency,mode,unit,fx,ptype,n,
                  rc,oc,mc,aspect,cw,ch,show_margin,show_latest,dpi,note,subtitle):
    # Keep periods when at least one primary KPI is available.
    # Previously dropna() required BOTH revenue and operating profit, which
    # erased every Cambricon row when operating profit was blank.
    df=df.dropna(subset=["period"]).copy()
    df["revenue"]=pd.to_numeric(df.get("revenue"),errors="coerce")
    df["operating_profit"]=pd.to_numeric(df.get("operating_profit"),errors="coerce")
    df=df[df[["revenue","operating_profit"]].notna().any(axis=1)].copy()
    if df.empty:
        raise ValueError("売上高・営業利益の有効なデータを認識できません。会社全体シートの数値と列名を確認してください。")
    df["period"]=df["period"].astype(str)
    keep=choose_periods(df["period"],n)
    df=df[df["period"].isin(keep)].copy()
    df["period"]=pd.Categorical(df["period"],categories=keep,ordered=True)
    df=df.sort_values("period").reset_index(drop=True)

    rr=pd.to_numeric(df["revenue"],errors="coerce")
    oo=pd.to_numeric(df["operating_profit"],errors="coerce")
    has_revenue=bool(rr.notna().any())
    has_op=bool(oo.notna().any())
    rev=convert(rr,currency,mode,unit,fx)
    op=convert(oo,currency,mode,unit,fx)
    margin=np.where(rr!=0,oo/rr*100,np.nan)
    # Some companies/periods can have zero or otherwise unusable revenue,
    # leaving the entire margin series NaN/inf. Never let that break rendering.
    margin=np.asarray(margin,dtype=float)
    margin[~np.isfinite(margin)]=np.nan
    has_margin=bool(np.isfinite(margin).any())

    x=np.arange(len(df)); bw=.36
    fw,fh=fig_size(aspect,cw,ch)
    fig,ax=plt.subplots(figsize=(fw,fh))
    fig.patch.set_facecolor(THEME["bg"])
    style_axis(ax,11)

    handles=[]; labels=[]
    if has_revenue:
        b1=ax.bar(x-bw/2,rev,bw,color=rc,label="売上高（左軸）",zorder=3)
        handles.append(b1); labels.append("売上高（左軸）")
    if has_op:
        b2=ax.bar(x+bw/2,op,bw,color=oc,label="営業利益（左軸）",zorder=3)
        handles.append(b2); labels.append("営業利益（左軸）")

    ax2=None
    if show_margin and has_margin:
        ax2=ax.twinx()
        ax2.set_facecolor("none")
        line,=ax2.plot(x,margin,color=mc,marker="o",markersize=6,
                       markeredgecolor="white",markeredgewidth=.7,
                       linewidth=2.7,zorder=6,label="営業利益率（右軸）")
        ax2.yaxis.set_major_formatter(FuncFormatter(lambda v,pos:f"{v:.0f}%"))
        ax2.tick_params(axis="y",colors=THEME["text"],labelsize=11,length=0)
        for s in ["top","left"]: ax2.spines[s].set_visible(False)
        ax2.spines["right"].set_color(THEME["axis"])
        handles.append(line); labels.append("営業利益率（右軸）")

    periods=df["period"].astype(str).tolist()
    ax.set_xticks(x)
    ax.set_xticklabels(period_labels(periods,ptype),fontsize=12)
    ax.text(-.045,1.01,f"（{unit}）",transform=ax.transAxes,
            fontsize=13,color=THEME["text"])

    leg=ax.legend(handles,labels,loc="upper left",bbox_to_anchor=(0.015,0.985),
                  ncol=1,fontsize=13.5,frameon=True,fancybox=True,framealpha=1,
                  handlelength=2.7,handleheight=1.2,labelspacing=.8,borderpad=.8)
    leg.get_frame().set_facecolor(THEME["bg"])
    leg.get_frame().set_edgecolor(THEME["card_border"])

    # 1 Title / 2 Subtitle / 3 KPI / 4 Chart / 5 Notes
    fig.text(.04,.965,company,fontsize=36,fontweight="bold",
             color=THEME["text"],ha="left",va="top")
    if subtitle and str(subtitle).strip():
        fig.text(.04,.895,str(subtitle).strip(),fontsize=20,fontweight="bold",
                 color=THEME["muted"],ha="left",va="top")

    if show_latest and len(df):
        lag=4 if ptype=="四半期" else 1
        rg=og=md=None
        if len(df)>lag:
            rg=growth(rr.iloc[-1],rr.iloc[-1-lag])
            og=growth(oo.iloc[-1],oo.iloc[-1-lag])
            if np.isfinite(margin[-1]) and np.isfinite(margin[-1-lag]):
                md=margin[-1]-margin[-1-lag]
        margin_value=f"{margin[-1]:.1f}%" if np.isfinite(margin[-1]) else "—"
        rev_value=f"{fmt(rev.iloc[-1])} {unit}" if np.isfinite(rev.iloc[-1]) else "—"
        op_value=f"{fmt(op.iloc[-1])} {unit}" if np.isfinite(op.iloc[-1]) else "—"
        specs=[
            ("売上高",rev_value,f"前年比 {rg:+.1f}%" if rg is not None else "",rc,THEME["revenue_bg"]),
            ("営業利益",op_value,f"前年比 {og:+.1f}%" if og is not None else "",oc,THEME["profit_bg"]),
            ("営業利益率",margin_value,f"前年差 {md:+.1f}pt" if md is not None else "",mc,THEME["margin_bg"])
        ]
        xs=[.055,.365,.675]
        for x0,s in zip(xs,specs):
            add_kpi_card(fig,x0,.69,.27,.14,*s)
        if np.isfinite(rev.iloc[-1]):
            add_callout(ax,x[-1]-bw/2,rev.iloc[-1],fmt(rev.iloc[-1]),rc,(-18,30))
        if np.isfinite(op.iloc[-1]):
            add_callout(ax,x[-1]+bw/2,op.iloc[-1],fmt(op.iloc[-1]),oc,(30,16))
        if ax2 is not None and np.isfinite(margin[-1]):
            add_callout(ax2,x[-1],margin[-1],f"{margin[-1]:.1f}%",mc,(40,25))

    ymin,ymax=ax.get_ylim()
    if ymax>0: ax.set_ylim(ymin,ymax*1.14)
    if ax2 is not None:
        finite_margin=margin[np.isfinite(margin)]
        if finite_margin.size:
            ax2.set_ylim(0,max(10,float(finite_margin.max())*1.35))

    # Graph band leaves a dedicated note band below it.
    plt.subplots_adjust(left=.08,right=.92,bottom=.125,top=.65)
    note_text(fig,note,currency,mode,fx,y=.022)

    buf=io.BytesIO()
    fig.savefig(buf,format="png",dpi=dpi,bbox_inches=None,facecolor=THEME["bg"])
    buf.seek(0)
    return fig,buf

def orders_chart(df,company,currency,mode,unit,fx,ptype,n,
                 orders_color,backlog_color,aspect,cw,ch,show_latest,dpi,note,subtitle):
    df=df.dropna(subset=["period"]).copy()
    df["period"]=df["period"].astype(str)
    keep=choose_periods(df["period"],n)
    df=df[df["period"].isin(keep)].copy()
    df["period"]=pd.Categorical(df["period"],categories=keep,ordered=True)
    df=df.sort_values("period").reset_index(drop=True)

    raw_orders=pd.to_numeric(df["orders"],errors="coerce")
    raw_backlog=pd.to_numeric(df["backlog"],errors="coerce")
    orders=convert(raw_orders,currency,mode,unit,fx)
    backlog=convert(raw_backlog,currency,mode,unit,fx)

    x=np.arange(len(df)); bw=.36
    fw,fh=fig_size(aspect,cw,ch)
    fig,ax=plt.subplots(figsize=(fw,fh))
    fig.patch.set_facecolor(THEME["bg"])
    style_axis(ax,11)

    b1=ax.bar(x-bw/2,orders,bw,color=orders_color,label="受注高",zorder=3)
    b2=ax.bar(x+bw/2,backlog,bw,color=backlog_color,label="受注残高",zorder=3)

    periods=df["period"].astype(str).tolist()
    ax.set_xticks(x)
    ax.set_xticklabels(period_labels(periods,ptype),fontsize=12)
    ax.text(-.045,1.01,f"（{unit}）",transform=ax.transAxes,
            fontsize=13,color=THEME["text"])

    leg=ax.legend([b1,b2],["受注高","受注残高"],loc="upper left",
                  bbox_to_anchor=(0.015,0.985),ncol=1,fontsize=13,
                  frameon=True,fancybox=True,framealpha=1,
                  handlelength=2.7,handleheight=1.2,labelspacing=.7,borderpad=.8)
    leg.get_frame().set_facecolor(THEME["bg"])
    leg.get_frame().set_edgecolor(THEME["card_border"])

    fig.text(.04,.965,company,fontsize=30,fontweight="bold",
             color=THEME["text"],ha="left",va="top")
    if subtitle and str(subtitle).strip():
        fig.text(.04,.895,str(subtitle).strip(),
                 fontsize=17,fontweight="bold",color=THEME["muted"],ha="left",va="top")

    if show_latest and len(df):
        lag=4 if ptype=="四半期" else 1
        og=bg=None
        if len(df)>lag:
            og=growth(raw_orders.iloc[-1],raw_orders.iloc[-1-lag])
            bg=growth(raw_backlog.iloc[-1],raw_backlog.iloc[-1-lag])
        add_kpi_card(fig,.17,.69,.27,.14,"受注高",f"{fmt(orders.iloc[-1])} {unit}",
                     f"前年比 {og:+.1f}%" if og is not None else "",orders_color,"#F1F6FF")
        add_kpi_card(fig,.56,.69,.27,.14,"受注残高",f"{fmt(backlog.iloc[-1])} {unit}",
                     f"前年比 {bg:+.1f}%" if bg is not None else "",backlog_color,"#F0FAF8")
        if pd.notna(orders.iloc[-1]):
            add_callout(ax,x[-1]-bw/2,orders.iloc[-1],fmt(orders.iloc[-1]),orders_color,(-18,30))
        if pd.notna(backlog.iloc[-1]):
            add_callout(ax,x[-1]+bw/2,backlog.iloc[-1],fmt(backlog.iloc[-1]),backlog_color,(30,16))

    ymin,ymax=ax.get_ylim()
    if ymax>0: ax.set_ylim(ymin,ymax*1.14)
    plt.subplots_adjust(left=.08,right=.92,bottom=.16,top=.65)
    note_text(fig,note,currency,mode,fx,y=.025)

    buf=io.BytesIO()
    fig.savefig(buf,format="png",dpi=dpi,bbox_inches=None,facecolor=THEME["bg"])
    buf.seek(0)
    return fig,buf

def segment_chart(df,company,currency,mode,unit,fx,n,style,title,ptype,
                  aspect,cw,ch,labels_on,dpi,note,segment_colors,subtitle,
                  show_total=False,total_name="全社ARR",display_names=None):

    display_names = display_names or {}
    raw=df.copy()
    segs=[c for c in raw.columns if c!="period" and not raw[c].isna().all()]
    keep=choose_periods(raw["period"],n)
    raw=raw[raw["period"].astype(str).isin(keep)].copy()
    raw["period"]=pd.Categorical(raw["period"].astype(str),categories=keep,ordered=True)
    raw=raw.sort_values("period").reset_index(drop=True)

    disp=raw.copy()
    for c in segs:
        disp[c]=convert(raw[c],currency,mode,unit,fx)

    fw,fh=fig_size(aspect,cw,ch)
    fig,ax=plt.subplots(figsize=(fw,fh))
    fig.patch.set_facecolor(THEME["bg"])
    style_axis(ax,11)

    colors=[segment_colors.get(s,THEME["segment_palette"][i%len(THEME["segment_palette"])])
            for i,s in enumerate(segs)]
    x=np.arange(len(disp))
    latest_positions=[]

    if style=="積み上げ":
        pos=np.zeros(len(disp)); neg=np.zeros(len(disp))
        for i,s in enumerate(segs):
            vals=pd.to_numeric(disp[s],errors="coerce").fillna(0).values
            bottoms=np.where(vals>=0,pos,neg)
            ax.bar(x,vals,bottom=bottoms,width=.68,color=colors[i],label=display_name_for(display_names,s),zorder=3)
            if len(disp): latest_positions.append((s,vals[-1],bottoms[-1]+vals[-1]/2,colors[i]))
            pos+=np.where(vals>=0,vals,0); neg+=np.where(vals<0,vals,0)
    else:
        ns=max(len(segs),1); bw=.88/ns
        offsets=(np.arange(ns)-(ns-1)/2)*bw
        for i,s in enumerate(segs):
            vals=pd.to_numeric(disp[s],errors="coerce").fillna(0).values
            xpos=x+offsets[i]
            ax.bar(xpos,vals,width=bw*.9,color=colors[i],label=display_name_for(display_names,s),zorder=3)
            if len(disp): latest_positions.append((s,vals[-1],vals[-1],colors[i]))

    plist=disp["period"].astype(str).tolist()
    ax.set_xticks(x)
    ax.set_xticklabels(period_labels(plist,ptype),fontsize=16)
    ax.text(-.045,1.01,f"（{unit}）",transform=ax.transAxes,
            fontsize=17,color=THEME["text"])

    # For stacked bars, the visual stack is bottom=first segment, top=last segment.
    # Reverse only the legend so its top-to-bottom order matches the bar's top-to-bottom order.
    handles, legend_labels = ax.get_legend_handles_labels()
    if style=="積み上げ":
        handles = handles[::-1]
        legend_labels = legend_labels[::-1]
    leg=ax.legend(handles,legend_labels,loc="upper left",bbox_to_anchor=(0.01,0.99),ncol=1,
                  fontsize=14,frameon=True,handlelength=2.1,
                  labelspacing=.50,borderpad=.65)
    leg.get_frame().set_facecolor(THEME["bg"])
    leg.get_frame().set_edgecolor(THEME["card_border"])

    # 凡例が棒に重ならないよう、凡例の行数に応じて上側に余白を確保する。
    # 文字サイズも少し小さくして、縦型凡例の読みやすさは維持する。
    if legend_labels:
        ymin, ymax = ax.get_ylim()
        span = max(ymax - ymin, 1.0)
        legend_headroom = min(0.40, 0.06 + 0.035 * len(legend_labels))
        ax.set_ylim(ymin, ymax + span * legend_headroom)

    # Compact title/subtitle -> graph spacing
    fig.text(.035,.965,company,fontsize=38,fontweight="bold",
             color=THEME["text"],ha="left",va="top")
    if subtitle and str(subtitle).strip():
        fig.text(.035,.895,str(subtitle).strip(),fontsize=24,fontweight="bold",
                 color=THEME["muted"],ha="left",va="top")

    lag=4 if ptype=="四半期" else 1

    ratio = fw / fh
    portrait = ratio <= 0.80
    squareish = 0.80 < ratio <= 1.12

    if portrait:
        plt.subplots_adjust(left=.10,right=.94,bottom=.47,top=.80)
        if len(disp): ax.set_xlim(-0.65, len(disp)-0.25)
    elif squareish:
        # 1:1専用。右側の最新値パネルに十分な幅を確保。
        plt.subplots_adjust(left=.065,right=.69,bottom=.14,top=.80)
        if len(disp): ax.set_xlim(-0.65, len(disp)-0.05)
    else:
        plt.subplots_adjust(left=.07,right=.755,bottom=.14,top=.82)
        if len(disp): ax.set_xlim(-0.65, len(disp)-0.10)

    if labels_on and len(disp):
        ordered=list(reversed(segs))

        if portrait:
            fig.text(.39,.425,"最新値",ha="center",va="top",
                     fontsize=16.5,fontweight="bold",color=THEME["text"])
            fig.text(.39,.397,f"（{unit}）",ha="center",va="top",
                     fontsize=15.5,fontweight="bold",color=THEME["text"])
            fig.text(.68,.425,"前年比\n成長率",ha="center",va="top",
                     fontsize=16.5,fontweight="bold",color=THEME["text"],linespacing=1.05)
            y_top=.345; step=min(.058,.27/max(len(ordered),1))
            value_x=.39; yoy_x=.68; value_fs=18; yoy_fs=16
        elif squareish:
            # 1:1: ヘッダーとカードを中央寄せし、2列を明確に分離。
            fig.text(.775,.800,"最新値",ha="center",va="top",
                     fontsize=15.5,fontweight="bold",color=THEME["text"])
            fig.text(.775,.765,f"（{unit}）",ha="center",va="top",
                     fontsize=14.5,fontweight="bold",color=THEME["text"])
            fig.text(.915,.800,"前年比\n成長率",ha="center",va="top",
                     fontsize=15.5,fontweight="bold",color=THEME["text"],linespacing=1.02)
            y_top=.680; step=min(.088,.44/max(len(ordered),1))
            value_x=.775; yoy_x=.915; value_fs=16.5; yoy_fs=15
        else:
            fig.text(.835,.820,"最新値",ha="center",va="top",
                     fontsize=16.5,fontweight="bold",color=THEME["text"])
            fig.text(.835,.785,f"（{unit}）",ha="center",va="top",
                     fontsize=15.5,fontweight="bold",color=THEME["text"])
            fig.text(.925,.820,"前年比\n成長率",ha="center",va="top",
                     fontsize=16.5,fontweight="bold",color=THEME["text"],linespacing=1.05)
            y_top=.700; step=min(.092,.46/max(len(ordered),1))
            value_x=.835; yoy_x=.925; value_fs=19; yoy_fs=16

        for j,s in enumerate(ordered):
            i=segs.index(s)
            color=colors[i]
            v=pd.to_numeric(disp[s],errors="coerce").iloc[-1]
            yoy=None
            if len(raw)>lag:
                yoy=growth(pd.to_numeric(raw[s],errors="coerce").iloc[-1],
                           pd.to_numeric(raw[s],errors="coerce").iloc[-1-lag])
            y=y_top-j*step
            fig.text(value_x,y,f"{fmt(v)} {unit}",ha="center",va="center",
                     fontsize=value_fs,fontweight="bold",color="white",
                     bbox=dict(boxstyle="round,pad=.34",fc=color,ec=color))
            fig.text(yoy_x,y,f"{yoy:+.1f}%" if yoy is not None else "—",
                     ha="center",va="center",fontsize=yoy_fs,fontweight="bold",
                     color=color,
                     bbox=dict(boxstyle="round,pad=.28",fc=color+"18",ec="none"))

    # ARRなどの積み上げグラフでは、最新棒の上に全社合計とYoYを表示できる。
    if show_total and style=="積み上げ" and len(disp) and segs:
        total_disp=disp[segs].apply(pd.to_numeric,errors="coerce").fillna(0).sum(axis=1)
        total_raw=raw[segs].apply(pd.to_numeric,errors="coerce").fillna(0).sum(axis=1)
        total_yoy=None
        if len(raw)>lag:
            total_yoy=growth(total_raw.iloc[-1],total_raw.iloc[-1-lag])
        if pd.notna(total_disp.iloc[-1]):
            # 全社ARRカードは最新棒の真上ではなく、グラフ右上の専用領域に固定。
            # 右側のプロダクト別最新値パネルと重ならず、棒とは縦線で接続する。
            total_text=f"{total_name}\n{fmt(total_disp.iloc[-1])} {unit}"
            if total_yoy is not None:
                total_text += f"\nYoY {total_yoy:+.1f}%"

            if portrait:
                card_xytext=(0.78, 1.16)
                total_fs=17
            elif squareish:
                card_xytext=(0.78, 0.94)
                total_fs=19
            else:
                card_xytext=(0.82, 0.93)
                total_fs=20

            ax.annotate(
                total_text,
                xy=(x[-1],total_disp.iloc[-1]), xycoords="data",
                xytext=card_xytext, textcoords="axes fraction",
                ha="center",va="center",fontsize=total_fs,fontweight="bold",
                color="white",linespacing=1.28,
                bbox=dict(boxstyle="round,pad=.60",fc=THEME["text"],ec=THEME["text"]),
                arrowprops=dict(arrowstyle="-",color=THEME["text"],lw=1.4,
                                connectionstyle="arc3,rad=0"),
                zorder=12,clip_on=False,annotation_clip=False
            )

    # Give plot extra headroom when a total label is shown above the latest stacked bar.
    ymin,ymax=ax.get_ylim()
    if ymax>0:
        ax.set_ylim(ymin,ymax*(1.18 if show_total and style=="積み上げ" else 1.08))

    note_text(fig,note,currency,mode,fx,y=.022)

    buf=io.BytesIO()
    fig.savefig(buf,format="png",dpi=dpi,bbox_inches=None,facecolor=THEME["bg"])
    buf.seek(0)
    return fig,buf

st.title("決算画像ジェネレーター v81 Deploy")
st.caption("年度・四半期を完全分離した1社1マスター。Googleスプレッドシート／Excelマスター／従来CSVに対応します。")

st.subheader("企業マスター")
gsheet_url=st.text_input("Google Sheets / Drive URL",placeholder="https://docs.google.com/spreadsheets/d/... または https://drive.google.com/file/d/...")
col_g1,col_g2=st.columns([1,3])
with col_g1:
    load_gsheet=st.button("Google Sheetsから読み込む",type="primary",use_container_width=True)
with col_g2:
    st.caption("Google Sheets URL / Google DriveファイルURLの両方に対応。「リンクを知っている全員が閲覧可」など外部閲覧可能にしてください。編集権限は不要です。")

if load_gsheet:
    if not gsheet_url.strip():
        st.error("GoogleスプレッドシートURLを入力してください。")
    else:
        try:
            gs_sheets,gs_settings=read_google_sheet_master(gsheet_url)
            st.session_state["google_master_sheets"]=gs_sheets
            st.session_state["google_master_settings"]=gs_settings
            st.session_state["google_master_url"]=gsheet_url
            st.success(f"Google Sheetsマスターを読み込みました：{len(gs_sheets)}データシート")
        except Exception as e:
            st.error(str(e))

master_upload=st.file_uploader(
    "または会社マスターExcelを読み込む（.xlsx / 全タブ一括）", type=["xlsx"], key="master_excel_upload"
)
try:
    with open("company_master_template.xlsx","rb") as f:
        st.download_button("会社マスターExcel テンプレートをダウンロード", f.read(), "company_master_template.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
except FileNotFoundError:
    pass
master_sheets=st.session_state.get("google_master_sheets",{}).copy()
master_settings=st.session_state.get("google_master_settings",{}).copy()
if master_upload is not None:
    try:
        master_sheets, master_settings = read_master_excel(master_upload)
        st.success(f"Excelマスターを読み込みました：{len(master_sheets)}データシート")
    except Exception as e:
        st.error(str(e))
elif master_sheets:
    st.info("Google Sheetsマスターを使用中。更新後は「Google Sheetsから読み込む」を押すと最新データを再取得します。")

ptype=st.radio("期間区分",["四半期","年度"],horizontal=True)

# v76: 期間区分を切り替えたら、会社全体のサブタイトルも必ず連動させる。
# text_input は同じ key の値を session_state に保持するため、
# モード変更を検知して widget 描画前に値を更新する。
_mode_subtitle = "四半期業績" if ptype == "四半期" else "年度業績"
_prev_ptype = st.session_state.get("_company_subtitle_ptype")
if _prev_ptype is None:
    # 初回表示も期間区分に対応した標準タイトルを使う。
    st.session_state["company_subtitle"] = _mode_subtitle
elif _prev_ptype != ptype:
    st.session_state["company_subtitle"] = _mode_subtitle
st.session_state["_company_subtitle_ptype"] = ptype

with st.sidebar:
    company=st.text_input("企業名",master_settings.get("company_name", ""))
    note=st.text_input("注意書き",master_settings.get("note","※ 最新期は会社予想"))

    currency_options=["JPY","USD","EUR","CNY","DKK","KRW","NOK","SEK","CHF","TWD","HKD"]
    master_currency=str(master_settings.get("input_currency","JPY")).upper()
    if master_currency not in currency_options:
        master_currency="JPY"
    currency=st.selectbox("CSVの入力通貨",currency_options,index=currency_options.index(master_currency))

    # マスターの display_unit をUI初期値にも反映する。
    # 外貨で「億ドル」等なら現地通貨、「億円」等なら円換算を自動選択。
    master_display_unit=str(master_settings.get("display_unit","") or "").strip()
    if master_display_unit == "億（現地通貨）":
        master_display_unit = DEFAULT_LOCAL.get(currency, "億円")
    if currency=="JPY":
        master_mode="現地通貨"
    elif master_display_unit in JPY_UNITS:
        master_mode="円換算"
    else:
        master_mode="現地通貨"
    mode_options=["現地通貨","円換算"]
    mode=st.radio("グラフの通貨表示",mode_options,index=mode_options.index(master_mode),horizontal=True)
    usd=st.number_input("USD/JPY",0.01,value=150.0,step=.1)
    eur=st.number_input("EUR/JPY",0.01,value=165.0,step=.1)
    cny=st.number_input("CNY/JPY",0.01,value=21.0,step=.1)
    dkk=st.number_input("DKK/JPY",0.0001,value=23.5,step=.1)
    krw=st.number_input("KRW/JPY",0.0001,value=0.11,step=.001,format="%.4f")
    nok=st.number_input("NOK/JPY",0.0001,value=14.0,step=.1)
    sek=st.number_input("SEK/JPY",0.0001,value=15.5,step=.1)
    chf=st.number_input("CHF/JPY",0.0001,value=185.0,step=.1)
    twd=st.number_input("TWD/JPY",0.0001,value=4.9,step=.01)
    hkd=st.number_input("HKD/JPY",0.0001,value=19.2,step=.01)
    fx={"USD":usd,"EUR":eur,"CNY":cny,"DKK":dkk,"KRW":krw,"NOK":nok,"SEK":sek,"CHF":chf,"TWD":twd,"HKD":hkd,"JPY":1.0}

    units=list(JPY_UNITS.keys()) if mode=="円換算" else list(LOCAL_UNITS[currency].keys())
    default="億円" if mode=="円換算" else DEFAULT_LOCAL[currency]
    # 設定シートの display_unit が現在の通貨/表示モードで有効なら最優先。
    # 例: input_currency=USD, display_unit=億ドル → UIも億ドルで開始。
    preferred_unit=master_display_unit if master_display_unit in units else default
    unit=st.selectbox("表示単位",units,index=units.index(preferred_unit))

    aspect=st.selectbox("縦横比",["1:1","16:9","4:3","3:2","9:16","カスタム"])
    cw,ch=16.0,9.0
    if aspect=="カスタム":
        cw=st.number_input("横幅",5.0,30.0,16.0,.5)
        ch=st.number_input("高さ",5.0,30.0,9.0,.5)
    dpi=st.select_slider("解像度",[120,180,220,300],value=220)

if ptype=="四半期":
    periods=["2019/9","2019/12","2020/3","2020/6","2020/9","2020/12",
             "2021/3","2021/6","2021/9","2021/12","2022/3","2022/6","2022/9","2022/12",
             "2023/3","2023/6","2023/9","2023/12","2024/3","2024/6","2024/9","2024/12",
             "2025/3","2025/6","2025/9","2025/12","2026/3","2026/6","2026/9","2026/12"]
    mx_allowed=30
else:
    periods=[f"FY{y}" for y in range(1997,2027)]
    mx_allowed=30

t1,t2,t3,t4,t5=st.tabs(["会社全体","セグメント売上高","セグメント利益","受注高・受注残高","ARR"])

with t1:
    # v81: 起動時はサンプル数値を表示しない。マスター/CSV未読込なら空表から開始。
    sample_company=pd.DataFrame(columns=["period","revenue","operating_profit"])

    uploaded_company=st.file_uploader(
        "会社全体CSVを読み込む（設定列つきCSV対応）",
        type=["csv"], key="company_csv_upload"
    )

    company_meta=master_meta(master_settings,"company") if master_settings else {}
    company_master_key=master_period_key("company",ptype)
    company_source=master_sheets.get(company_master_key,sample_company)
    if uploaded_company is not None and company_master_key not in master_sheets:
        try:
            loaded, company_meta=read_uploaded_csv(uploaded_company)
            required={"period","revenue","operating_profit"}
            if required.issubset(set(loaded.columns)):
                company_source=loaded
            else:
                st.error("会社全体CSVには period / revenue / operating_profit 列が必要です。")
        except Exception as e:
            st.error(str(e))

    csv_currency,csv_mode,csv_unit=resolve_csv_display(
        company_meta,currency,mode,unit
    )

    if company_meta:
        st.caption(
            f"ファイル設定を優先：入力通貨 {csv_currency} / 表示単位 {csv_unit}"
        )

    ed=st.data_editor(company_source,use_container_width=True,num_rows="dynamic",key="company_editor")
    ed=normalize_numeric_columns(ed)
    av=max(len(ed.dropna(subset=["period"])),1)
    n=st.number_input("表示する期間数",1,min(mx_allowed,av),min(20,mx_allowed,av))

    default_rc=normalize_color(company_meta.get(META_REVENUE_COLOR),THEME["revenue"])
    default_oc=normalize_color(company_meta.get(META_OPERATING_PROFIT_COLOR),THEME["profit"])
    default_mc=normalize_color(company_meta.get(META_MARGIN_COLOR),THEME["margin"])

    c1,c2,c3=st.columns(3)
    with c1: rc=st.color_picker("売上高カラー",default_rc,key="company_revenue_color")
    with c2: oc=st.color_picker("営業利益カラー",default_oc,key="company_profit_color")
    with c3: mc=st.color_picker("営業利益率カラー",default_mc,key="company_margin_color")

    # CSVにカラー指定がある場合はCSVを最優先
    effective_rc=normalize_color(company_meta.get(META_REVENUE_COLOR),rc)
    effective_oc=normalize_color(company_meta.get(META_OPERATING_PROFIT_COLOR),oc)
    effective_mc=normalize_color(company_meta.get(META_MARGIN_COLOR),mc)

    sm=st.checkbox("営業利益率を表示",True)
    sl=st.checkbox("最新期ラベルを表示",True)
    # v76: UIの「四半期 / 年度」と会社全体サブタイトルを同期。
    # モード切替時は上で session_state を更新するため、古い入力値が残らない。
    default_company_subtitle = "四半期業績" if ptype == "四半期" else "年度業績"
    company_subtitle=st.text_input("サブタイトル",default_company_subtitle,key="company_subtitle")

    company_download=company_csv_for_download(
        ed,csv_currency,csv_unit,effective_rc,effective_oc,effective_mc,company_subtitle
    )

    cdl1,cdl2=st.columns(2)
    with cdl1:
        st.download_button(
            "会社全体CSVをダウンロード",
            company_download.to_csv(index=False).encode("utf-8-sig"),
            "company_financials.csv","text/csv",use_container_width=True
        )
    with cdl2:
        generate=st.button("会社全体グラフを生成",type="primary",use_container_width=True)

    if generate:
        fig,png=company_chart(
            ed,company,csv_currency,csv_mode,csv_unit,fx,ptype,int(n),
            effective_rc,effective_oc,effective_mc,
            aspect,cw,ch,sm,sl,dpi,note,company_subtitle
        )
        st.image(png.getvalue(), width="stretch")
        st.download_button("PNGをダウンロード",png.getvalue(),"financials.png","image/png")

def seg_tab(kind):
    if kind=="売上高":
        title="セグメント別 売上高"; key="rev"
    else:
        title="セグメント別 営業利益"; key="op"

    # v81: 起動時はダミーのセグメント値を表示しない。
    sample_seg=pd.DataFrame(columns=["period"])
    uploaded_seg=st.file_uploader(
        "CSVを読み込む（設定列・セグメントカラー列対応）",
        type=["csv"], key=f"{key}_csv_upload"
    )

    master_section="segment_revenue" if kind=="売上高" else "segment_profit"
    seg_meta=master_meta(master_settings,master_section) if master_settings else {}
    seg_master_key=master_period_key(master_section,ptype)
    seg_source=master_sheets.get(seg_master_key,sample_seg)
    if uploaded_seg is not None and seg_master_key not in master_sheets:
        try:
            loaded, seg_meta=read_uploaded_csv(uploaded_seg)
            if "period" in loaded.columns and len([c for c in loaded.columns if c!="period"])>=1:
                seg_source=loaded
            else:
                st.error("セグメントCSVには period 列と、1列以上のセグメント列が必要です。")
        except Exception as e:
            st.error(str(e))

    csv_currency,csv_mode,csv_unit=resolve_csv_display(
        seg_meta,currency,mode,unit
    )
    if seg_meta:
        st.caption(
            f"ファイル設定を優先：入力通貨 {csv_currency} / 表示単位 {csv_unit}"
        )

    ed=st.data_editor(
        seg_source,use_container_width=True,num_rows="dynamic",key=key
    )
    ed=normalize_numeric_columns(ed)

    segs=[c for c in ed.columns if c!="period"]
    seg_display_names=master_display_names(master_settings, "segment_revenue" if kind=="売上高" else "segment_profit")
    st.markdown("**セグメントカラー**")
    color_cols=st.columns(min(3,max(len(segs),1)))
    seg_colors={}
    for i,s in enumerate(segs):
        csv_color=normalize_color(
            seg_meta.get(f"{META_SEGMENT_PREFIX}{s}"),
            THEME["segment_palette"][i%len(THEME["segment_palette"])]
        )
        with color_cols[i%len(color_cols)]:
            picked=st.color_picker(
                display_name_for(seg_display_names,s),csv_color,key=f"color_{key}_{s}"
            )
        # CSV指定がある場合はCSVを優先
        seg_colors[s]=normalize_color(
            seg_meta.get(f"{META_SEGMENT_PREFIX}{s}"),picked
        )

    av=max(len(ed.dropna(subset=["period"])),1)
    n=st.number_input("表示する期間数",1,min(mx_allowed,av),min(20,mx_allowed,av),key="n"+key)
    style=st.radio("表示方法",["積み上げ","横並び"],horizontal=True,key="s"+key)
    lab=st.checkbox("最新期のデータラベル・前年比を表示",True,key="l"+key)
    default_seg_subtitle=seg_meta.get(META_SUBTITLE) or ("セグメント別 売上高の推移" if kind=="売上高" else "セグメント別 営業利益の推移")
    seg_subtitle=st.text_input("サブタイトル",default_seg_subtitle,key="subtitle_"+key)

    seg_download=segment_csv_for_download(
        ed,csv_currency,csv_unit,seg_colors,seg_subtitle
    )

    c1,c2=st.columns(2)
    with c1:
        st.download_button(
            "CSVをダウンロード",
            seg_download.to_csv(index=False).encode("utf-8-sig"),
            f"{key}.csv","text/csv",key="csv"+key,use_container_width=True
        )
    with c2:
        generate=st.button(
            title+"グラフを生成",type="primary",
            use_container_width=True,key="b"+key
        )

    if generate:
        fig,png=segment_chart(
            ed,company,csv_currency,csv_mode,csv_unit,fx,int(n),style,title,
            ptype,aspect,cw,ch,lab,dpi,note,seg_colors,seg_subtitle,
            display_names=seg_display_names
        )
        st.image(png.getvalue(), width="stretch")
        st.download_button(
            "PNGをダウンロード",png.getvalue(),key+".png",
            "image/png",key="d"+key
        )

with t2: seg_tab("売上高")
with t3: seg_tab("利益")

with t4:
    # v81: 起動時はダミーの受注データを表示しない。
    sample_orders=pd.DataFrame(columns=["period","orders","backlog"])
    uploaded_orders=st.file_uploader(
        "受注高・受注残高CSVを読み込む（設定列つきCSV対応）",
        type=["csv"],key="orders_csv_upload"
    )
    orders_meta=master_meta(master_settings,"orders") if master_settings else {}
    orders_master_key=master_period_key("orders",ptype)
    orders_source=master_sheets.get(orders_master_key,sample_orders)
    if uploaded_orders is not None and orders_master_key not in master_sheets:
        try:
            loaded,orders_meta=read_uploaded_csv(uploaded_orders)
            required={"period","orders","backlog"}
            if required.issubset(set(loaded.columns)):
                orders_source=loaded
            else:
                st.error("CSVには period / orders / backlog 列が必要です。")
        except Exception as e:
            st.error(str(e))

    csv_currency,csv_mode,csv_unit=resolve_csv_display(orders_meta,currency,mode,unit)
    if orders_meta:
        st.caption(f"ファイル設定を優先：入力通貨 {csv_currency} / 表示単位 {csv_unit}")

    oed=st.data_editor(orders_source,use_container_width=True,num_rows="dynamic",key="orders_editor")
    oed=normalize_numeric_columns(oed)
    av=max(len(oed.dropna(subset=["period"])),1)
    on=st.number_input("表示する期間数",1,min(mx_allowed,av),min(20,mx_allowed,av),key="norders")

    default_orders_color=normalize_color(orders_meta.get(META_ORDERS_COLOR),THEME["revenue"])
    default_backlog_color=normalize_color(orders_meta.get(META_BACKLOG_COLOR),THEME["profit"])
    oc1,oc2=st.columns(2)
    with oc1:
        orders_color=st.color_picker("受注高カラー",default_orders_color,key="orders_color")
    with oc2:
        backlog_color=st.color_picker("受注残高カラー",default_backlog_color,key="backlog_color")
    effective_orders_color=normalize_color(orders_meta.get(META_ORDERS_COLOR),orders_color)
    effective_backlog_color=normalize_color(orders_meta.get(META_BACKLOG_COLOR),backlog_color)
    show_orders_latest=st.checkbox("最新期ラベルを表示",True,key="orders_latest")
    default_orders_subtitle=orders_meta.get(META_SUBTITLE) or "受注高・受注残高の推移"
    orders_subtitle=st.text_input("サブタイトル",default_orders_subtitle,key="orders_subtitle")

    orders_download=orders_csv_for_download(
        oed,csv_currency,csv_unit,effective_orders_color,effective_backlog_color,orders_subtitle
    )
    od1,od2=st.columns(2)
    with od1:
        st.download_button("CSVをダウンロード",
            orders_download.to_csv(index=False).encode("utf-8-sig"),
            "orders_backlog.csv","text/csv",use_container_width=True)
    with od2:
        generate_orders=st.button("受注高・受注残高グラフを生成",
            type="primary",use_container_width=True,key="generate_orders")

    if generate_orders:
        fig,png=orders_chart(
            oed,company,csv_currency,csv_mode,csv_unit,fx,ptype,int(on),
            effective_orders_color,effective_backlog_color,
            aspect,cw,ch,show_orders_latest,dpi,note,orders_subtitle
        )
        st.image(png.getvalue(), width="stretch")
        st.download_button("PNGをダウンロード",png.getvalue(),
                           "orders_backlog.png","image/png",key="download_orders")


with t5:
    # ARRは複数プロダクトを積み上げ表示。CSVは period + 各プロダクト列。
    # v81: 起動時はダミーのARRデータを表示しない。
    sample_arr=pd.DataFrame(columns=["period"])
    uploaded_arr=st.file_uploader(
        "ARR CSVを読み込む（period + 各プロダクト列）",
        type=["csv"],key="arr_csv_upload"
    )
    arr_meta=master_meta(master_settings,"arr") if master_settings else {}
    arr_master_key=master_period_key("arr",ptype)
    arr_source=master_sheets.get(arr_master_key,sample_arr)
    if uploaded_arr is not None and arr_master_key not in master_sheets:
        try:
            loaded,arr_meta=read_uploaded_csv(uploaded_arr)
            if "period" in loaded.columns and len([c for c in loaded.columns if c!="period"])>=1:
                arr_source=loaded
            else:
                st.error("ARR CSVには period 列と、1列以上のプロダクト列が必要です。")
        except Exception as e:
            st.error(str(e))

    csv_currency,csv_mode,csv_unit=resolve_csv_display(arr_meta,currency,mode,unit)
    if arr_meta:
        st.caption(f"ファイル設定を優先：入力通貨 {csv_currency} / 表示単位 {csv_unit}")

    aed=st.data_editor(arr_source,use_container_width=True,num_rows="dynamic",key="arr_editor")
    aed=normalize_numeric_columns(aed)
    products=[c for c in aed.columns if c!="period"]
    arr_display_names=master_display_names(master_settings, "arr")

    st.markdown("**プロダクトカラー**")
    arr_color_cols=st.columns(min(3,max(len(products),1)))
    arr_colors={}
    for i,product in enumerate(products):
        csv_color=normalize_color(
            arr_meta.get(f"{META_SEGMENT_PREFIX}{product}"),
            THEME["segment_palette"][i%len(THEME["segment_palette"])]
        )
        with arr_color_cols[i%len(arr_color_cols)]:
            picked=st.color_picker(display_name_for(arr_display_names,product),csv_color,key=f"arr_color_{product}")
        arr_colors[product]=normalize_color(
            arr_meta.get(f"{META_SEGMENT_PREFIX}{product}"),picked
        )

    av=max(len(aed.dropna(subset=["period"])),1)
    an=st.number_input("表示する期間数",1,min(mx_allowed,av),min(20,mx_allowed,av),key="narr")
    arr_labels=st.checkbox("プロダクト別の最新ARR・前年比を表示",True,key="arr_latest")
    arr_total=st.checkbox("全社ARR・YoYを最新の積み上げ棒の上に表示",True,key="arr_total")
    default_arr_subtitle=arr_meta.get(META_SUBTITLE) or "ARRの推移"
    arr_subtitle=st.text_input("サブタイトル",default_arr_subtitle,key="arr_subtitle")

    arr_download=segment_csv_for_download(
        aed,csv_currency,csv_unit,arr_colors,arr_subtitle
    )
    ac1,ac2=st.columns(2)
    with ac1:
        st.download_button(
            "ARR CSVをダウンロード",
            arr_download.to_csv(index=False).encode("utf-8-sig"),
            "arr.csv","text/csv",key="arr_csv_download",use_container_width=True
        )
    with ac2:
        generate_arr=st.button(
            "ARRグラフを生成",type="primary",use_container_width=True,key="generate_arr"
        )

    if generate_arr:
        fig,png=segment_chart(
            aed,company,csv_currency,csv_mode,csv_unit,fx,int(an),"積み上げ","プロダクト別 ARR",
            ptype,aspect,cw,ch,arr_labels,dpi,note,arr_colors,arr_subtitle,
            show_total=arr_total,total_name="全社ARR",display_names=arr_display_names
        )
        st.image(png.getvalue(), width="stretch")
        st.download_button(
            "PNGをダウンロード",png.getvalue(),"arr.png",
            "image/png",key="download_arr"
        )
