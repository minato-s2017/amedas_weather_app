"""
weather newShun — アメダス ミニお天気ダッシュボード
=====================================================
気象庁アメダスの公開データを使った、自分のエリア向けの簡易ダッシュボード。

主な機能:
  1. 現在の観測値（気温・湿度・風・降水量）と簡易天気
  2. 過去24時間の気温の推移（折れ線グラフ）
  3. 降水ナウキャスト（雨雲）を地図に重ねて表示 + 公式ページを開くボタン

データ元（すべて気象庁の公開JSON/タイル。APIキー不要）:
  - 観測所一覧 :  amedastable.json
  - 最新時刻   :  latest_time.txt
  - 全国データ :  data/map/{timestamp}.json
  - 地点時系列 :  data/point/{code}/{yyyymmdd}_{block}.json   （10分間隔）
  - 雨雲タイル :  jmatile/data/nowc/...   （高解像度降水ナウキャスト hrpns）
"""

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import requests
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple

# ----------------- 設定 ----------------- #

# ダッシュボードの初期表示にする「ホームエリア」の観測所番号
#   例) 東京=44132 / 横浜=46106 / 千葉=45212 / 熊谷=43056 / 銚子=45148
#   変更したいときはこの1行を書き換えるか、起動後の「🏠 ホーム」ボタン/検索で選べます。
HOME_STATION_CODE = "44132"  # 東京（大手町）

AMEDAS_TABLE_URL = "https://www.jma.go.jp/bosai/amedas/const/amedastable.json"
LATEST_TIME_URL = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"
AMEDAS_MAP_URL = "https://www.jma.go.jp/bosai/amedas/data/map/{timestamp}.json"
POINT_URL = "https://www.jma.go.jp/bosai/amedas/data/point/{code}/{ymd}_{block}.json"
NOWCAST_TARGET_URL = "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json"

# データ取得のキャッシュ時間（秒）。アメダスは10分・ナウキャストは5分間隔で更新。
CACHE_TTL = 300


# ----------------- ユーティリティ関数 ----------------- #

def degmin_to_deg(values):
    """[度, 分] の形式を 10進法の度に変換"""
    if not isinstance(values, list) or len(values) != 2:
        return None
    return values[0] + values[1] / 60.0


def get_first_value(entry: Dict[str, Any], key: str):
    """アメダスJSONの [値, 品質コード] から値だけを取り出し、確実に数値(float)に変換する"""
    v = entry.get(key)
    if isinstance(v, list) and len(v) > 0 and v[0] is not None:
        try:
            return float(v[0])
        except (ValueError, TypeError):
            return None
    return None


def wind_dir_to_str(code: Any) -> str:
    """風向き(0〜16)を方位文字列に変換（アメダス仕様。0/16=静穏扱い）"""
    if code is None:
        return "不明"
    try:
        code_int = int(code)
    except (ValueError, TypeError):
        return "不明"

    dirs = [
        "北", "北北東", "北東", "東北東",
        "東", "東南東", "南東", "南南東",
        "南", "南南西", "南西", "西南西",
        "西", "西北西", "北西", "北北西",
    ]
    if code_int == 0:
        return "静穏"
    # アメダスの風向は 1〜16（1=北北東 … 16=北）で来ることがあるため両対応
    if 1 <= code_int <= 16:
        return dirs[code_int % 16]
    if 0 <= code_int < len(dirs):
        return dirs[code_int]
    return "不明"


def estimate_weather_label(point: Dict[str, Any]) -> Tuple[str, str]:
    """降水量・日照時間から「晴れ / くもり / 雨」をざっくり判定し、(ラベル, 絵文字) を返す"""
    precip1h = get_first_value(point, "precipitation1h")
    precip10m = get_first_value(point, "precipitation10m")
    sun1h = get_first_value(point, "sun1h")

    if (precip1h is not None and precip1h > 0) or \
       (precip10m is not None and precip10m > 0):
        return "雨", "🌧"

    if sun1h is not None:
        if sun1h >= 0.1:   # 1時間のうち6分以上日が当たっていれば晴れとみなす
            return "晴れ", "☀"
        return "くもり", "☁"

    return "くもり", "☁"


@st.cache_data(show_spinner=False)
def load_amedas_table() -> Dict[str, Any]:
    """観測所一覧（約1300地点）を取得してキャッシュ"""
    res = requests.get(AMEDAS_TABLE_URL, timeout=10)
    res.raise_for_status()
    return res.json()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def get_latest_amedas_map() -> Tuple[str, Dict[str, Any]]:
    """最新の全国アメダスデータを取得。(最新時刻ISO文字列, データ) を返す"""
    res = requests.get(LATEST_TIME_URL, timeout=10)
    res.raise_for_status()
    dt = datetime.fromisoformat(res.text.strip())  # 例: 2026-07-15T15:40:00+09:00
    ts = dt.strftime("%Y%m%d%H%M%S")
    res2 = requests.get(AMEDAS_MAP_URL.format(timestamp=ts), timeout=10)
    res2.raise_for_status()
    return dt.isoformat(), res2.json()


def _blocks_back(anchor: datetime, hours: int) -> List[Tuple[str, str]]:
    """anchor(JST naive) から過去hours時間をカバーする (yyyymmdd, block) を新しい順で返す。
    アメダス地点データは3時間単位のファイル(00,03,...,21)に格納されている。"""
    start = anchor.replace(minute=0, second=0, microsecond=0)
    start = start.replace(hour=(start.hour // 3) * 3)
    n_blocks = hours // 3 + 2  # 24hなら 現在ブロック + 過去8ブロック程度で確実にカバー
    out, seen = [], set()
    for i in range(n_blocks):
        b = start - timedelta(hours=3 * i)
        key = (b.strftime("%Y%m%d"), b.strftime("%H"))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def get_temp_timeseries(code: str, anchor_iso: str, hours: int = 24) -> List[Tuple[datetime, float]]:
    """指定観測所の過去hours時間の気温時系列 [(時刻, 気温), ...] を古い順で返す"""
    anchor = datetime.fromisoformat(anchor_iso).replace(tzinfo=None)
    cutoff = anchor - timedelta(hours=hours)
    rows: List[Tuple[datetime, float]] = []
    for ymd, block in _blocks_back(anchor, hours):
        url = POINT_URL.format(code=code, ymd=ymd, block=block)
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                continue
            data = res.json()
        except Exception:
            continue
        for ts, entry in data.items():
            try:
                t = datetime.strptime(ts, "%Y%m%d%H%M%S")
            except ValueError:
                continue
            if t < cutoff:
                continue
            temp = get_first_value(entry, "temp")
            if temp is not None:
                rows.append((t, temp))
    rows.sort(key=lambda x: x[0])
    return rows


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def get_nowcast_time() -> Tuple[str, str]:
    """降水ナウキャストの最新フレーム (basetime, validtime) を返す（いずれもUTCの文字列）"""
    res = requests.get(NOWCAST_TARGET_URL, timeout=10)
    res.raise_for_status()
    arr = res.json()
    latest = max(arr, key=lambda e: e["basetime"])
    return latest["basetime"], latest["validtime"]


def utc_str_to_jst_label(utc_str: str) -> str:
    """'yyyymmddHHMMSS'(UTC) を 'MM/DD HH:MM'(JST) に整形"""
    try:
        dt = datetime.strptime(utc_str, "%Y%m%d%H%M%S") + timedelta(hours=9)
        return dt.strftime("%m/%d %H:%M")
    except ValueError:
        return "不明"


def render_rain_map(lat: float, lon: float, name: str, basetime: str, validtime: str, zoom: int = 10):
    """国土地理院の地図に気象庁の雨雲ナウキャストを重ねた地図を表示（Leaflet）"""
    html = """
<div id="map" style="height:440px;border-radius:14px;overflow:hidden;"></div>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  var map = L.map('map', {zoomControl:true, attributionControl:true}).setView([__LAT__, __LON__], __ZOOM__);
  L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png',
    {maxZoom:18, attribution:'地理院タイル'}).addTo(map);
  L.tileLayer('https://www.jma.go.jp/bosai/jmatile/data/nowc/__BASE__/none/__VALID__/surf/hrpns/{z}/{x}/{y}.png',
    {opacity:0.72, maxZoom:12, attribution:'気象庁ナウキャスト'}).addTo(map);
  L.marker([__LAT__, __LON__]).addTo(map).bindPopup('__NAME__').openPopup();
</script>
"""
    html = (html
            .replace("__LAT__", f"{lat:.4f}")
            .replace("__LON__", f"{lon:.4f}")
            .replace("__ZOOM__", str(zoom))
            .replace("__BASE__", basetime)
            .replace("__VALID__", validtime)
            .replace("__NAME__", name.replace("'", "")))
    components.html(html, height=460)


# ----------------- アプリ本体 ----------------- #

st.set_page_config(page_title="weather newShun", page_icon="🌤", layout="wide")

# 観測所一覧
try:
    stations = load_amedas_table()
except Exception as e:
    st.error(f"観測所一覧の取得に失敗しました: {e}")
    st.stop()

codes_sorted = sorted(stations.keys(), key=lambda c: stations[c]["kjName"])

# 選択中の観測所を session_state で保持（初期値=ホーム）
if "code" not in st.session_state:
    st.session_state.code = HOME_STATION_CODE if HOME_STATION_CODE in stations else codes_sorted[0]

# ---- ヘッダー ---- #
st.title("🌤 weather newShun")
st.caption("気象庁アメダスの観測データによる、あなたのエリアのミニお天気ダッシュボード")

# ---- 観測所の選択・更新・ホーム ---- #
c_sel, c_home, c_refresh = st.columns([6, 1.3, 1.3])
with c_sel:
    st.session_state.code = st.selectbox(
        "観測所（入力で検索できます）",
        options=codes_sorted,
        index=codes_sorted.index(st.session_state.code),
        format_func=lambda c: f"{stations[c]['kjName']}（{c}）",
        label_visibility="collapsed",
    )
with c_home:
    if st.button("🏠 ホーム", use_container_width=True,
                 disabled=(HOME_STATION_CODE not in stations)):
        st.session_state.code = HOME_STATION_CODE
        st.rerun()
with c_refresh:
    if st.button("🔄 更新", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

selected_code = st.session_state.code
station_info = stations[selected_code]
lat = degmin_to_deg(station_info["lat"])
lon = degmin_to_deg(station_info["lon"])

# ---- 最新データ取得 ---- #
try:
    obs_iso, all_data = get_latest_amedas_map()
except Exception as e:
    st.error(f"アメダスデータの取得に失敗しました: {e}")
    st.stop()

obs_time = datetime.fromisoformat(obs_iso)
point = all_data.get(selected_code)

st.subheader(f"📍 {station_info['kjName']}（{station_info['enName']} / {selected_code}）")

if point is None:
    st.warning("この観測所の最新データが見つかりませんでした。別の観測所を選んでください。")
    st.stop()

# ---- 現在の観測値 ---- #
weather_label, weather_icon = estimate_weather_label(point)
temp = get_first_value(point, "temp")
humidity = get_first_value(point, "humidity")
wind_speed = get_first_value(point, "wind")
wind_dir = wind_dir_to_str(get_first_value(point, "windDirection"))
precip1h = get_first_value(point, "precipitation1h")
precip24h = get_first_value(point, "precipitation24h")

st.markdown(f"#### {weather_icon} 現在：{weather_label}　"
            f"<span style='color:gray;font-size:0.8em'>観測時刻 {obs_time.strftime('%m/%d %H:%M')}</span>",
            unsafe_allow_html=True)

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("気温", f"{temp:.1f} ℃" if temp is not None else "—")
m2.metric("湿度", f"{humidity:.0f} %" if humidity is not None else "—")
m3.metric("風速 / 風向", f"{wind_speed:.1f} m/s" if wind_speed is not None else "—", wind_dir)
m4.metric("1時間降水量", f"{precip1h:.1f} mm" if precip1h is not None else "—")
m5.metric("24時間降水量", f"{precip24h:.1f} mm" if precip24h is not None else "—")

st.divider()

# ---- 2カラム：左=気温グラフ / 右=雨雲レーダー ---- #
left, right = st.columns([1, 1])

with left:
    st.markdown("### 📈 過去24時間の気温")
    with st.spinner("気温の推移を取得中..."):
        series = get_temp_timeseries(selected_code, obs_iso, hours=24)
    if series:
        df = pd.DataFrame(series, columns=["時刻", "気温(℃)"]).set_index("時刻")
        st.line_chart(df, height=340)
        temps = [t for _, t in series]
        cmin, cmax = st.columns(2)
        cmin.metric("最低", f"{min(temps):.1f} ℃")
        cmax.metric("最高", f"{max(temps):.1f} ℃")
    else:
        st.info("この観測所には気温の時系列データがありません（雨量のみ等の観測所です）。")

with right:
    st.markdown("### 🌧 雨雲レーダー（降水ナウキャスト）")
    try:
        base, valid = get_nowcast_time()
        st.caption(f"雨雲の時刻：{utc_str_to_jst_label(valid)}（気象庁 高解像度降水ナウキャスト）")
        render_rain_map(lat, lon, station_info["kjName"], base, valid)
    except Exception as e:
        st.info(f"雨雲データの取得に失敗しました: {e}")
    nowc_url = (f"https://www.jma.go.jp/bosai/nowc/#zoom:10/lat:{lat:.4f}"
                f"/lon:{lon:.4f}/colordepth:normal/elements:hrpns")
    st.link_button("☔ 公式の雨雲ナウキャストを開く（動画・拡大）", nowc_url,
                   use_container_width=True)

st.divider()

# ---- 位置情報 ---- #
with st.expander("🗺 観測所の位置"):
    st.write(f"緯度 {lat:.4f}° / 経度 {lon:.4f}°")
    st.map(pd.DataFrame({"lat": [lat], "lon": [lon]}), zoom=9)

st.caption("データ出典：気象庁（アメダス／降水ナウキャスト）・地理院タイル。"
           "簡易判定の天気は目安です。")
