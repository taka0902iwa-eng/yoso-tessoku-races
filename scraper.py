"""
races.json 自動生成スクリプト
競馬: sanspo.com レース一覧
競艇: boatrace.jp 公式API
競輪: keirin.jp レース一覧
"""

import json
import re
from datetime import datetime, timezone, timedelta
import urllib.request
from html.parser import HTMLParser

JST = timezone(timedelta(hours=9))
today = datetime.now(JST)
today_str = today.strftime("%Y-%m-%d")
today_ymd = today.strftime("%Y%m%d")


# ─────────────────────────────────────────
# 競馬: sanspo.com からG1/G2/G3を取得
# ─────────────────────────────────────────
def fetch_horse():
    try:
        url = "https://race.sanspo.com/keiba/news/race_list/"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            html = res.read().decode("utf-8", errors="replace")

        # グレードレース抽出（G1/G2/G3/重賞）
        pattern = r'(\d{1,2}:\d{2}).*?(G[123]|重賞).*?([^\n　]{2,10}競馬場|東京|中山|阪神|京都|中京|小倉|福島|新潟|札幌|函館).*?([^\n]{4,20}(?:賞|杯|ステークス|カップ|記念))'
        matches = re.findall(pattern, html)

        if matches:
            t, grade, venue, name = matches[0]
            return {"sport": "horse", "name": name.strip(), "venue": venue.strip(),
                    "time": t, "grade": grade, "url": "keiba.html"}
    except Exception as e:
        print(f"[horse] error: {e}")

    # フォールバック
    return {"sport": "horse", "name": "本日の注目レース", "venue": "詳細はページ内",
            "time": "--:--", "grade": "", "url": "keiba.html"}


# ─────────────────────────────────────────
# 競艇: boatrace.jp 公式API
# ─────────────────────────────────────────
def fetch_boat():
    try:
        # 開催場一覧API
        url = f"https://www.boatrace.jp/owpc/pc/race/index?hd={today_ymd}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            html = res.read().decode("utf-8", errors="replace")

        # SG/G1開催場を探す
        sg_pattern = r'(SG|G1|G2|G3|プレミアム).*?<.*?>([^<]{2,8})</.*?>\s*(\d{2}:\d{2})'
        matches = re.findall(sg_pattern, html, re.DOTALL)

        if matches:
            grade, venue, time_ = matches[0]
            return {"sport": "boat", "name": f"{venue} 注目レース",
                    "venue": venue.strip(), "time": time_,
                    "grade": grade, "url": "kyotei.html"}

        # 開催場名だけでも取る
        venue_pattern = r'class="[^"]*venue[^"]*"[^>]*>([^<]{2,6})</'
        venues = re.findall(venue_pattern, html)
        if venues:
            return {"sport": "boat", "name": "本日の注目レース", "venue": venues[0],
                    "time": "--:--", "grade": "", "url": "kyotei.html"}

    except Exception as e:
        print(f"[boat] error: {e}")

    return {"sport": "boat", "name": "本日の注目レース", "venue": "詳細はページ内",
            "time": "--:--", "grade": "", "url": "kyotei.html"}


# ─────────────────────────────────────────
# 競輪: keirin.jp
# ─────────────────────────────────────────
def fetch_cycle():
    try:
        url = "https://www.keirin.jp/pc/racetop.do"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            html = res.read().decode("utf-8", errors="replace")

        # GP/G1/G2/G3開催を探す
        grade_pattern = r'(GP|G[123I]|FI|GIII).*?([^\n<]{2,6}(?:競輪場|バンク))'
        matches = re.findall(grade_pattern, html)

        if matches:
            grade, venue = matches[0]
            return {"sport": "cycle", "name": "本日の注目レース",
                    "venue": venue.strip(), "time": "--:--",
                    "grade": grade, "url": "keirin.html"}

        # 開催場だけ
        venue_pattern = r'([^\n<]{2,5}競輪場)'
        venues = re.findall(venue_pattern, html)
        if venues:
            return {"sport": "cycle", "name": "本日の注目レース", "venue": venues[0],
                    "time": "--:--", "grade": "", "url": "keirin.html"}

    except Exception as e:
        print(f"[cycle] error: {e}")

    return {"sport": "cycle", "name": "本日の注目レース", "venue": "詳細はページ内",
            "time": "--:--", "grade": "", "url": "keirin.html"}


# ─────────────────────────────────────────
# メイン
# ─────────────────────────────────────────
if __name__ == "__main__":
    print(f"[{today_str}] スクレイピング開始...")

    races = []
    races.append(fetch_horse())
    print(f"  競馬: {races[-1]}")
    races.append(fetch_boat())
    print(f"  競艇: {races[-1]}")
    races.append(fetch_cycle())
    print(f"  競輪: {races[-1]}")

    output = {"date": today_str, "races": races}

    with open("races.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("races.json 生成完了")
