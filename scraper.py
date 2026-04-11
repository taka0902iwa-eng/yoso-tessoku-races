"""
予想の鉄則 - 完全自動化スクリプト（EV計算式組み込み版）
① netkeibaから注目レースの出馬表・馬成績を取得
② 独自EV計算式でスコア・推定勝率・EVを算出
③ 競艇・競輪も全開催取得
④ Claude APIで予想文生成
⑤ races.jsonを自動生成してConoHaに転送

EV計算式:
  J = 0.4*tansho_return + 0.3*course_return + 0.3*recent5_stability
  score = (B/C) * (C/(C+3)) * ((F-E+1)/F) * J
  P_i = score_i / SUM(score_all)
  EV_i = odds_i * P_i
  judge: EV>1.25→強買い, EV>1.0→買い, else→見送り
"""

import json, re, time, ftplib, os, sys
import urllib.request, urllib.robotparser
from datetime import datetime, timezone, timedelta

JST     = timezone(timedelta(hours=9))
today   = datetime.now(JST)
today_str  = today.strftime("%Y-%m-%d")
today_ymd  = today.strftime("%Y%m%d")

HEADERS = {
    "User-Agent": "YosoNoTessoku-Bot/1.0 (予想の鉄則; 1日1回; contact: yoso-tessoku@oyatojikka.online)"
}

def check_robots(base, path):
    try:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        rp.read()
        return rp.can_fetch(HEADERS["User-Agent"], base + path)
    except:
        return True

def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ══════════════════════════════════════════════════
# EV計算ロジック（Python版）
# ══════════════════════════════════════════════════
def calc_J(tansho_return, course_return, recent5_stability):
    return 0.4 * tansho_return + 0.3 * course_return + 0.3 * recent5_stability

def calc_score(horse):
    B  = float(horse.get("B", 0))
    C  = float(horse.get("C", 0))
    E  = float(horse.get("E", 0))
    F  = float(horse.get("F", 0))
    tr = float(horse.get("tansho_return",    1.0))
    cr = float(horse.get("course_return",    1.0))
    rs = float(horse.get("recent5_stability",1.0))
    if C <= 0 or F <= 0 or E <= 0:
        return 0.0
    J          = calc_J(tr, cr, rs)
    base_rate  = B / C
    stability  = C / (C + 3)
    market_edge= (F - E + 1) / F
    return base_rate * stability * market_edge * J

def calc_race_ev(horses):
    scored = [{"data": h, "score": calc_score(h)} for h in horses]
    total  = sum(s["score"] for s in scored)
    result = []
    for s in scored:
        odds = float(s["data"].get("odds", 0))
        prob = s["score"] / total if total > 0 else 0
        ev   = odds * prob if odds > 0 else 0
        judge= "強買い" if ev > 1.25 else "買い" if ev > 1.0 else "見送り"
        result.append({
            **s["data"],
            "J":     round(calc_J(
                         float(s["data"].get("tansho_return",    1.0)),
                         float(s["data"].get("course_return",    1.0)),
                         float(s["data"].get("recent5_stability",1.0))
                     ), 4),
            "score": round(s["score"], 6),
            "prob":  round(prob, 4),
            "ev":    round(ev,   4),
            "judge": judge
        })
    return sorted(result, key=lambda x: x["ev"], reverse=True)


# ══════════════════════════════════════════════════
# netkeiba から注目レース・出馬表・馬成績を取得
# ⚠️ netkeibaは利用規約でスクレイピング禁止の可能性あり
# ══════════════════════════════════════════════════
def fetch_horse_with_ev():
    races = []
    try:
        base = "https://race.netkeiba.com"
        if not check_robots(base, "/top/race_list.html"):
            print("[horse] robots.txt禁止 → JRAフォールバック")
            return fetch_horse_fallback()

        # ① 本日の重賞レース一覧を取得
        html = fetch(base + "/top/race_list.html")
        time.sleep(3)

        # レースIDを抽出（形式: 2026XXXXXXXXXX）
        race_ids = re.findall(r'race_id=(\d{12})', html)
        # 本日のレースのみ絞り込み
        today_ids = [rid for rid in race_ids if rid.startswith(today_ymd[:8])]
        # 重複除去
        seen = set()
        unique_ids = []
        for rid in today_ids:
            if rid not in seen:
                seen.add(rid)
                unique_ids.append(rid)

        print(f"  本日のレースID: {len(unique_ids)}件")

        for race_id in unique_ids[:5]:  # 最大5レース
            try:
                race_info = fetch_race_details(base, race_id)
                if race_info:
                    races.append(race_info)
                time.sleep(2)
            except Exception as e:
                print(f"  [race/{race_id}] エラー: {e}")

    except Exception as e:
        print(f"[horse] エラー: {e}")

    if not races:
        return fetch_horse_fallback()

    print(f"  競馬: {len(races)}件取得（EV計算済み）")
    return races


def fetch_race_details(base, race_id):
    """出馬表ページから馬情報・オッズを取得してEV計算"""
    try:
        # 出馬表取得
        shutsuba_url = f"{base}/race/shutuba.html?race_id={race_id}"
        html = fetch(shutsuba_url)
        time.sleep(1)

        # レース名・場所・時刻を抽出
        name_match  = re.search(r'<title>([^<]+)</title>', html)
        venue_match = re.search(r'(札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉)', html)
        time_match  = re.search(r'(\d{2}:\d{2})', html)
        grade_match = re.search(r'(G[123]|重賞)', html)

        race_name = name_match.group(1).strip() if name_match else f"レース{race_id}"
        venue     = (venue_match.group(1) + "競馬場") if venue_match else "競馬場"
        race_time = time_match.group(1)  if time_match  else "--:--"
        grade     = grade_match.group(1) if grade_match else ""

        # 馬名・馬番を抽出
        horse_pattern = r'horse_id=(\d+)[^>]*>([^<]{2,20})</a>'
        horse_matches = re.findall(horse_pattern, html)

        # 頭数
        F = len(horse_matches) if horse_matches else 16

        # オッズページ取得
        odds_url  = f"https://race.netkeiba.com/odds/index.html?race_id={race_id}&type=b1"
        odds_html = fetch(odds_url)
        time.sleep(1)
        odds_list = re.findall(r'(\d+\.\d)', odds_html)
        odds_map  = {}
        for i, (horse_id, _) in enumerate(horse_matches):
            if i < len(odds_list):
                try:
                    odds_map[horse_id] = float(odds_list[i])
                except:
                    odds_map[horse_id] = 10.0

        # 各馬の成績データを取得してEV計算
        horses_data = []
        for horse_id, horse_name in horse_matches[:16]:
            hdata = fetch_horse_stats(horse_id, horse_name, F, odds_map.get(horse_id, 10.0))
            horses_data.append(hdata)
            time.sleep(1)

        if not horses_data:
            return None

        # EV計算実行
        ev_results = calc_race_ev(horses_data)

        # 本命（EV最高かつ買い以上）を選定
        best = next((h for h in ev_results if h["judge"] in ["強買い","買い"]), ev_results[0] if ev_results else None)

        return {
            "sport":     "horse",
            "name":      race_name,
            "venue":     venue,
            "time":      race_time,
            "grade":     grade,
            "url":       "keiba.html",
            "ev_detail": ev_results[:5],  # 上位5頭
            "honmei":    best["name"] if best else "",
            "ev":        f"+{int((best['ev']-1)*100)}%" if best and best['ev']>1 else f"{int((best['ev']-1)*100)}%" if best else "",
            "judge":     best["judge"] if best else "見送り",
            "reason":    f"推定勝率{int(best['prob']*100)}%・EV{best['ev']:.2f}倍" if best else ""
        }

    except Exception as e:
        print(f"  [race_details/{race_id}] エラー: {e}")
        return None


def fetch_horse_stats(horse_id, horse_name, F, odds):
    """馬の成績ページから勝利数・出走数・平均人気を取得"""
    try:
        url  = f"https://db.netkeiba.com/horse/{horse_id}/"
        html = fetch(url)
        time.sleep(0.5)

        # 出走数・勝利数を抽出
        race_rows = re.findall(r'<td[^>]*>(\d+)</td>', html)

        # 成績テーブルから集計
        wins   = len(re.findall(r'<td[^>]*>1着</td>', html))
        total  = len(re.findall(r'<td[^>]*>\d+着</td>', html))
        if total == 0:
            total = max(wins + 3, 5)

        # 勝利時の平均人気を抽出（簡易）
        pop_matches = re.findall(r'(\d+)番人気', html)
        win_pops    = []
        results_raw = re.findall(r'1着.*?(\d+)番人気', html[:5000])
        if results_raw:
            win_pops = [int(p) for p in results_raw[:wins] if int(p) <= 18]
        avg_pop = sum(win_pops)/len(win_pops) if win_pops else max(1, F//3)

        # 近5走の回収率（簡易推定）
        recent_odds = re.findall(r'(\d+\.\d)倍', html[:3000])
        recent5 = [float(o) for o in recent_odds[:5] if 1.0 <= float(o) <= 200]
        avg_recent_odds = sum(recent5)/len(recent5) if recent5 else 10.0
        recent5_stability = min(1.2, max(0.8, avg_recent_odds / 10.0))

        return {
            "name":               horse_name,
            "B":                  wins,
            "C":                  total,
            "E":                  round(avg_pop, 1),
            "F":                  F,
            "odds":               odds,
            "tansho_return":      1.05,  # デフォルト値（JRA-VAN取得まで）
            "course_return":      1.02,
            "recent5_stability":  round(recent5_stability, 3)
        }

    except Exception as e:
        print(f"    [horse_stats/{horse_id}] エラー: {e}")
        return {
            "name":               horse_name,
            "B":                  1, "C": 8, "E": float(F)//3,
            "F":                  F, "odds": odds,
            "tansho_return":      1.0, "course_return": 1.0,
            "recent5_stability":  1.0
        }


def fetch_horse_fallback():
    """netkeibaが取得できない場合のフォールバック"""
    try:
        base = "https://www.jra.go.jp"
        html = fetch(base + "/race/thisweek/")
        time.sleep(2)
        grades  = re.findall(r'(G[123])[^\n]*?([^\n]{3,20}(?:賞|杯|ステークス|カップ|記念|特別))', html)
        venues  = re.findall(r'(札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉)', html)
        times   = re.findall(r'(\d{1,2}:\d{2})', html)
        if grades and venues:
            grade, name = grades[0]
            return [{"sport":"horse","name":name.strip(),"venue":venues[0]+"競馬場",
                     "time":times[0] if times else "--:--","grade":grade,"url":"keiba.html"}]
    except Exception as e:
        print(f"[horse/fallback] エラー: {e}")
    return [fallback("horse")]


# ══════════════════════════════════════════════════
# 競艇: boatrace.jp から全開催場取得
# ══════════════════════════════════════════════════
BOAT_CODES = {
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
        html = fetch(base + f"/owpc/pc/race/index?hd={today_ymd}")
        time.sleep(2)
        sg_matches   = re.findall(r'(SG|G1|G2|G3)', html)
        found_venues = [v for v in BOAT_CODES if v in html]
        for i, venue in enumerate(found_venues):
            grade = sg_matches[i] if i < len(sg_matches) else ""
            code  = BOAT_CODES.get(venue, "")
            t     = "--:--"
            try:
                rhtml = fetch(base + f"/owpc/pc/race/raceindex?hd={today_ymd}&jcd={code}")
                time.sleep(1)
                ts = re.findall(r'(\d{2}:\d{2})', rhtml)
                if ts: t = ts[-1]
            except: pass
            races.append({"sport":"boat","name":f"{venue} 注目レース","venue":venue,
                          "time":t,"grade":grade,"url":"kyotei.html"})
    except Exception as e:
        print(f"[boat] エラー: {e}")
    print(f"  競艇: {len(races)}件取得")
    return races


# ══════════════════════════════════════════════════
# 競輪: keirin.jp から全開催場取得
# ══════════════════════════════════════════════════
def fetch_cycle_all():
    races = []
    try:
        base = "https://www.keirin.jp"
        html = fetch(base + "/pc/racetop.do")
        time.sleep(2)
        grades = re.findall(r'(GP|G[123I]|FI|FII)', html)
        venues = re.findall(r'([^\n<]{2,5}競輪場)', html)
        times  = re.findall(r'(\d{1,2}:\d{2})', html)
        seen   = set()
        for i, venue in enumerate(venues):
            venue = venue.strip()
            if venue in seen: continue
            seen.add(venue)
            races.append({"sport":"cycle","name":f"{venue} 注目レース","venue":venue,
                          "time":times[i] if i<len(times) else "--:--",
                          "grade":grades[i] if i<len(grades) else "","url":"keirin.html"})
    except Exception as e:
        print(f"[cycle] エラー: {e}")
    print(f"  競輪: {len(races)}件取得")
    return races


def fallback(sport):
    labels = {"horse":"競馬","boat":"競艇","cycle":"競輪"}
    urls   = {"horse":"keiba.html","boat":"kyotei.html","cycle":"keirin.html"}
    return {"sport":sport,"name":f"{labels[sport]} 本日の注目レース",
            "venue":"詳細はページ内","time":"--:--","grade":"","url":urls[sport]}


# ══════════════════════════════════════════════════
# Claude API: 予想文生成
# ══════════════════════════════════════════════════
def generate_prediction_text(races):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[Claude API] APIキー未設定。スキップします。")
        return races, ""

    # EV計算済み競馬レースの情報を整理
    races_text = ""
    for r in races:
        ev_info = ""
        if r.get("ev_detail"):
            top3 = r["ev_detail"][:3]
            ev_info = "　EV上位: " + "、".join([f'{h["name"]}(EV:{h["ev"]:.2f}/{h["judge"]})' for h in top3])
        races_text += f"・{r['sport']}：{r['name']}（{r['venue']} {r['time']} {r.get('grade','')}）{ev_info}\n"

    prompt = f"""あなたは競馬・競艇・競輪の予想専門家「たか」です。
今日（{today_str}）の以下のレース情報（EV計算済み）を元に、LINE配信用の予想テキストを生成してください。

{races_text}

以下の形式でJSONのみ返してください：
{{
  "line_message": "LINE配信用テキスト（300文字程度・競馬はEV値を含める・自己責任の表現を入れる）"
}}

ルール：断定表現禁止・「参考程度に」「自己責任で」を含める"""

    try:
        data = json.dumps({
            "model": "claude-opus-4-5",
            "max_tokens": 1000,
            "messages": [{"role":"user","content":prompt}]
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=data,
            headers={"Content-Type":"application/json",
                     "x-api-key":api_key,
                     "anthropic-version":"2023-06-01"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            result = json.loads(res.read().decode("utf-8"))
        content = result["content"][0]["text"].strip()
        content = re.sub(r'^```json\s*','',content)
        content = re.sub(r'\s*```$','',content)
        parsed  = json.loads(content)
        print("[Claude API] 予想文生成完了")
        return races, parsed.get("line_message","")
    except Exception as e:
        print(f"[Claude API] エラー: {e}")
        return races, ""


# ══════════════════════════════════════════════════
# FTPアップロード
# ══════════════════════════════════════════════════
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
                if not d: continue
                path += "/" + d
                try: ftp.mkd(path)
                except ftplib.error_perm: pass
            with open("races.json","rb") as f:
                ftp.storbinary(f"STOR {remote}", f)
            print(f"FTPアップロード完了: {remote}")
    except ftplib.all_errors as e:
        print(f"FTPエラー: {e}")
        sys.exit(1)


# ══════════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"[{today_str}] EV計算付き完全自動化スクリプト開始")

    # ① 全レース取得（競馬はEV計算付き）
    print("\n--- ① レース取得＋EV計算 ---")
    all_races = []
    horse_races = fetch_horse_with_ev()
    all_races.extend(horse_races)
    all_races.extend(fetch_boat_all())
    all_races.extend(fetch_cycle_all())
    print(f"  合計: {len(all_races)}件")

    # ② Claude APIで予想文生成
    print("\n--- ② 予想文生成（Claude API） ---")
    all_races, line_message = generate_prediction_text(all_races)

    # ③ races.json生成
    print("\n--- ③ races.json生成 ---")
    output = {
        "date":         today_str,
        "races":        all_races,
        "line_message": line_message
    }
    with open("races.json","w",encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"races.json生成完了（{len(all_races)}件）")

    if line_message:
        with open("line_message.txt","w",encoding="utf-8") as f:
            f.write(line_message)
        print(f"\n--- LINE配信テキスト ---\n{line_message}")

    # ④ FTPアップロード
    print("\n--- ④ FTPアップロード ---")
    upload_ftp()

    print(f"\n✅ 全処理完了（{len(all_races)}件）")
