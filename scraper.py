"""
予想の鉄則 - 完全自動化スクリプト（全レース取得版）
競馬・競艇・競輪の全開催レースを取得してEV計算まで自動化

① スクレイピングで全レース情報＋オッズ取得
② Claude APIでEV計算＋予想文生成
③ races.jsonを自動生成
④ ConoHaにFTP転送

環境変数（GitHub Secrets）:
  ANTHROPIC_API_KEY
  FTP_HOST / FTP_USER / FTP_PASS / FTP_REMOTE
"""

import json
import re
import time
import ftplib
import os
import sys
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
today = datetime.now(JST)
today_str = today.strftime("%Y-%m-%d")
today_ymd = today.strftime("%Y%m%d")

HEADERS = {
    "User-Agent": "YosoNoTessoku-Bot/1.0 (予想の鉄則 情報収集bot; 1日1回アクセス; contact: yoso-tessoku@oyatojikka.online)"
}

def check_robots(base_url, path):
    try:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(base_url + "/robots.txt")
        rp.read()
        return rp.can_fetch(HEADERS["User-Agent"], base_url + path)
    except:
        return True

def fetch_url(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", errors="replace")


# ────────────────────────────────────────────────
# 競馬: JRA公式から全重賞レース取得
# ────────────────────────────────────────────────
def fetch_horse_all():
    races = []
    try:
        base = "https://www.jra.go.jp"
        path = "/race/thisweek/"
        if not check_robots(base, path):
            return races
        html = fetch_url(base + path)
        time.sleep(2)

        # 全重賞レースを抽出
        grade_pattern = r'(G[123])[^\n]*?([^\n]{3,20}(?:賞|杯|ステークス|カップ|記念|特別))'
        venue_pattern = r'(札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉)'
        time_pattern  = r'(\d{1,2}:\d{2})'

        grades = re.findall(grade_pattern, html)
        venues = re.findall(venue_pattern, html)
        times  = re.findall(time_pattern, html)

        for i, (grade, name) in enumerate(grades):
            venue = venues[i] + "競馬場" if i < len(venues) else "競馬場"
            t     = times[i] if i < len(times) else "--:--"
            races.append({
                "sport": "horse",
                "name":  name.strip(),
                "venue": venue,
                "time":  t,
                "grade": grade,
                "url":   "keiba.html",
                "odds_list": []
            })

        # 重賞がない場合は一般戦も取得
        if not races:
            race_pattern = r'(\d{1,2}:\d{2}).*?([^\n]{4,20}(?:賞|杯|ステークス|記念|特別))'
            matches = re.findall(race_pattern, html)
            for t, name in matches[:5]:
                venue = venues[0] + "競馬場" if venues else "競馬場"
                races.append({
                    "sport": "horse",
                    "name":  name.strip(),
                    "venue": venue,
                    "time":  t,
                    "grade": "",
                    "url":   "keiba.html",
                    "odds_list": []
                })

    except Exception as e:
        print(f"[horse] エラー: {e}")

    print(f"  競馬: {len(races)}件取得")
    return races


# ────────────────────────────────────────────────
# 競艇: boatrace.jp から全開催場・全レース取得
# ────────────────────────────────────────────────
BOAT_VENUE_CODES = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05",
    "浜名湖":"06","蒲郡":"07","常滑":"08","津":"09","三国":"10",
    "琵琶湖":"11","住之江":"12","尼崎":"13","鳴門":"14","丸亀":"15",
    "児島":"16","宮島":"17","徳山":"18","下関":"19","若松":"20",
    "芦屋":"21","福岡":"22","唐津":"23","大村":"24"
}

def fetch_boat_all():
    races = []
    try:
        base = "https://www.boatrace.jp"
        path = f"/owpc/pc/race/index?hd={today_ymd}"
        if not check_robots(base, "/owpc/pc/race/index"):
            return races

        html = fetch_url(base + path)
        time.sleep(2)

        # 全開催場を抽出
        found_venues = [v for v in BOAT_VENUE_CODES.keys() if v in html]
        sg_pattern   = r'(SG|G1|G2|G3)'
        sg_matches   = re.findall(sg_pattern, html)

        for i, venue in enumerate(found_venues):
            grade = sg_matches[i] if i < len(sg_matches) else ""
            venue_code = BOAT_VENUE_CODES.get(venue, "")

            # 各場の注目レース（メインレース）を取得
            if venue_code:
                try:
                    race_url = f"{base}/owpc/pc/race/raceindex?hd={today_ymd}&jcd={venue_code}"
                    race_html = fetch_url(race_url)
                    time.sleep(1)

                    # レース番号と時刻を抽出
                    race_times = re.findall(r'(\d{2}:\d{2})', race_html)
                    race_nums  = re.findall(r'(\d+)R', race_html)

                    # 最終レース（メインレース）を取得
                    if race_times:
                        main_time = race_times[-1]
                        main_rno  = race_nums[-1] if race_nums else "12"
                        races.append({
                            "sport": "boat",
                            "name":  f"{venue} {main_rno}R",
                            "venue": venue,
                            "time":  main_time,
                            "grade": grade,
                            "url":   "kyotei.html",
                            "odds_list": []
                        })
                    else:
                        races.append({
                            "sport": "boat",
                            "name":  f"{venue} 注目レース",
                            "venue": venue,
                            "time":  "--:--",
                            "grade": grade,
                            "url":   "kyotei.html",
                            "odds_list": []
                        })
                except Exception as e:
                    print(f"  [boat/{venue}] エラー: {e}")
                    races.append({
                        "sport": "boat",
                        "name":  f"{venue} 注目レース",
                        "venue": venue,
                        "time":  "--:--",
                        "grade": grade,
                        "url":   "kyotei.html",
                        "odds_list": []
                    })
            else:
                races.append({
                    "sport": "boat",
                    "name":  f"{venue} 注目レース",
                    "venue": venue,
                    "time":  "--:--",
                    "grade": grade,
                    "url":   "kyotei.html",
                    "odds_list": []
                })

    except Exception as e:
        print(f"[boat] エラー: {e}")

    print(f"  競艇: {len(races)}件取得")
    return races


# ────────────────────────────────────────────────
# 競輪: keirin.jp から全開催場取得
# ────────────────────────────────────────────────
def fetch_cycle_all():
    races = []
    try:
        base = "https://www.keirin.jp"
        path = "/pc/racetop.do"
        if not check_robots(base, path):
            return races

        html = fetch_url(base + path)
        time.sleep(2)

        grade_pattern = r'(GP|G[123I]|FI|FII)'
        venue_pattern = r'([^\n<]{2,5}競輪場)'
        time_pattern  = r'(\d{1,2}:\d{2})'

        grades = re.findall(grade_pattern, html)
        venues = re.findall(venue_pattern, html)
        times  = re.findall(time_pattern, html)

        # 重複を除去しながら全開催場を取得
        seen = set()
        for i, venue in enumerate(venues):
            venue = venue.strip()
            if venue in seen:
                continue
            seen.add(venue)
            grade = grades[i] if i < len(grades) else ""
            t     = times[i]  if i < len(times)  else "--:--"
            races.append({
                "sport": "cycle",
                "name":  f"{venue} 注目レース",
                "venue": venue,
                "time":  t,
                "grade": grade,
                "url":   "keirin.html",
                "odds_list": []
            })

    except Exception as e:
        print(f"[cycle] エラー: {e}")

    print(f"  競輪: {len(races)}件取得")
    return races


# ────────────────────────────────────────────────
# Claude API: EV計算＋予想文生成
# ────────────────────────────────────────────────
def generate_ev_prediction(races):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[Claude API] APIキー未設定。スキップします。")
        return races, ""

    # 注目レースのみEV計算（グレードレース優先・最大9件）
    priority_races = sorted(races, key=lambda r: (
        0 if r.get("grade") in ["G1","SG","GP"] else
        1 if r.get("grade") in ["G2","G3"] else 2
    ))[:9]

    races_text = ""
    for r in priority_races:
        races_text += f"・{r['sport']}：{r['name']}（{r['venue']} {r['time']} {r['grade']}）\n"

    prompt = f"""あなたは競馬・競艇・競輪の予想専門家「たか」です。
今日（{today_str}）の以下のレースについてEV（期待値）分析に基づいた予想を生成してください。

【本日の注目レース】
{races_text}

以下の形式でJSONのみ返してください（説明文・コードブロック不要）:
{{
  "predictions": [
    {{
      "sport": "horse/boat/cycle",
      "name": "レース名",
      "venue": "開催地",
      "honmei": "本命の番号と名前",
      "ev": "+XX%",
      "reason": "60文字以内の予想根拠",
      "buy": "推奨買い目"
    }}
  ],
  "line_message": "LINE配信用テキスト（本日の注目レースをまとめた自然な日本語300文字程度）"
}}

必須ルール：
- 「必ず勝てる」等の断定表現は絶対に使わない
- 「参考程度に」「自己責任で」の表現を含める
- グレードレース（G1/SG/GP）を優先して解説する
- line_messageはそのままLINEにコピペできる形式"""

    try:
        data = json.dumps({
            "model": "claude-opus-4-5",
            "max_tokens": 3000,
            "messages": [{"role": "user", "content": prompt}]
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01"
            },
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=60) as res:
            result = json.loads(res.read().decode("utf-8"))

        content = result["content"][0]["text"].strip()
        content = re.sub(r'^```json\s*', '', content)
        content = re.sub(r'\s*```$',     '', content)
        parsed  = json.loads(content)

        predictions  = parsed.get("predictions", [])
        line_message = parsed.get("line_message", "")

        # races にEV情報をマージ（名前・場所で照合）
        for race in races:
            for pred in predictions:
                if pred.get("venue") == race.get("venue") and pred.get("sport") == race.get("sport"):
                    race.update({
                        "honmei": pred.get("honmei", ""),
                        "ev":     pred.get("ev",     ""),
                        "reason": pred.get("reason", ""),
                        "buy":    pred.get("buy",    "")
                    })
                    break

        print(f"[Claude API] EV計算完了 ({len(predictions)}件)")
        return races, line_message

    except Exception as e:
        print(f"[Claude API] エラー: {e}")
        return races, ""


# ────────────────────────────────────────────────
# FTPアップロード
# ────────────────────────────────────────────────
def upload_ftp():
    host     = os.environ.get("FTP_HOST")
    user     = os.environ.get("FTP_USER")
    password = os.environ.get("FTP_PASS")
    remote   = os.environ.get("FTP_REMOTE",
               "/home/c9048134/public_html/oyatojikka.online/races.json")

    if not all([host, user, password]):
        print("FTP環境変数が未設定。スキップします。")
        return

    try:
        with ftplib.FTP(host, timeout=30) as ftp:
            ftp.login(user, password)
            ftp.set_pasv(True)
            path = ""
            for d in "/".join(remote.split("/")[:-1]).split("/"):
                if not d:
                    continue
                path += "/" + d
                try:
                    ftp.mkd(path)
                except ftplib.error_perm:
                    pass
            with open("races.json", "rb") as f:
                ftp.storbinary(f"STOR {remote}", f)
            print(f"FTPアップロード完了: {remote}")
    except ftplib.all_errors as e:
        print(f"FTPエラー: {e}")
        sys.exit(1)


# ────────────────────────────────────────────────
# メイン
# ────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[{today_str}] 全レース取得・完全自動化スクリプト開始")

    # ① 全レース取得
    print("\n--- ① 全レース取得 ---")
    all_races = []
    all_races.extend(fetch_horse_all())
    all_races.extend(fetch_boat_all())
    all_races.extend(fetch_cycle_all())
    print(f"  合計: {len(all_races)}件")

    # ② Claude API でEV計算＋予想文生成
    print("\n--- ② EV計算（Claude API） ---")
    all_races, line_message = generate_ev_prediction(all_races)

    # ③ races.json生成（odds_listは除外）
    print("\n--- ③ races.json生成 ---")
    for r in all_races:
        r.pop("odds_list", None)

    output = {
        "date":         today_str,
        "races":        all_races,
        "line_message": line_message
    }

    with open("races.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"races.json生成完了 ({len(all_races)}件)")

    # LINE配信テキストを保存
    if line_message:
        with open("line_message.txt", "w", encoding="utf-8") as f:
            f.write(line_message)
        print(f"\n--- LINE配信テキスト ---\n{line_message}")

    # ④ FTPアップロード
    print("\n--- ④ FTPアップロード ---")
    upload_ftp()

    print(f"\n✅ 全処理完了（{len(all_races)}件）")
