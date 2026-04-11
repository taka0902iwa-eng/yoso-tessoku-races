"""
予想の鉄則 - 完全自動化スクリプト（オッズ取得＋EV計算版）
① スクレイピングでレース情報＋オッズ取得
② Claude APIでEV計算＋予想文生成
③ races.jsonを自動生成
④ ConoHaにFTP転送

取得元:
  競馬: netkeiba.com  ⚠️ 利用規約要確認
  競艇: boatrace.jp   ✅ 公式・比較的安全
  競輪: keirin.jp     ⚠️ 利用規約要確認

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
today_ym  = today.strftime("%Y%m")

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
# 競馬: netkeiba.com からレース情報＋オッズ取得
# ⚠️ netkeiba.comは利用規約でスクレイピング禁止の可能性あり
# ────────────────────────────────────────────────
def fetch_horse():
    try:
        base = "https://race.netkeiba.com"
        path = "/top/race_list.html"

        if not check_robots(base, path):
            print("[horse] robots.txtで禁止。JRA公式にフォールバック")
            return fetch_horse_jra()

        html = fetch_url(base + path)
        time.sleep(3)  # 丁寧に待機

        # グレードレース抽出
        grade_pattern = r'(G[123]|重賞)[^\n]{0,50}?([^\n]{4,20}(?:賞|杯|ステークス|カップ|記念))'
        venue_pattern = r'(札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉)'
        time_pattern  = r'(\d{2}:\d{2})'
        odds_pattern  = r'(\d+\.\d)'

        grades  = re.findall(grade_pattern, html)
        venues  = re.findall(venue_pattern, html)
        times   = re.findall(time_pattern, html)
        odds    = re.findall(odds_pattern, html)

        if grades and venues:
            grade, name = grades[0]
            venue = venues[0] + "競馬場"
            t     = times[0] if times else "--:--"
            # オッズリスト（上位5頭分）
            odds_list = [float(o) for o in odds[:10] if 1.0 <= float(o) <= 100.0][:5]
            return {
                "sport": "horse",
                "name":  name.strip(),
                "venue": venue,
                "time":  t,
                "grade": grade,
                "url":   "keiba.html",
                "odds_list": odds_list  # EV計算用
            }

    except Exception as e:
        print(f"[horse/netkeiba] エラー: {e}")

    return fetch_horse_jra()


def fetch_horse_jra():
    """netkeiba失敗時のフォールバック: JRA公式"""
    try:
        base = "https://www.jra.go.jp"
        path = "/race/thisweek/"
        if not check_robots(base, path):
            return fallback("horse")
        html = fetch_url(base + path)
        time.sleep(2)
        grade_pattern = r'(G[123])[^\n]*?([^\n]{3,20}(?:賞|杯|ステークス|カップ|記念|特別))'
        venue_pattern = r'(札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉)'
        time_pattern  = r'(\d{1,2}:\d{2})'
        grades = re.findall(grade_pattern, html)
        venues = re.findall(venue_pattern, html)
        times  = re.findall(time_pattern, html)
        if grades and venues:
            grade, name = grades[0]
            return {"sport":"horse","name":name.strip(),"venue":venues[0]+"競馬場",
                    "time":times[0] if times else "--:--","grade":grade,"url":"keiba.html","odds_list":[]}
    except Exception as e:
        print(f"[horse/jra] エラー: {e}")
    return fallback("horse")


# ────────────────────────────────────────────────
# 競艇: boatrace.jp 公式APIからオッズ取得
# ✅ 公式サイト・比較的安全
# ────────────────────────────────────────────────
def fetch_boat():
    try:
        base = "https://www.boatrace.jp"
        path = f"/owpc/pc/race/index?hd={today_ymd}"

        if not check_robots(base, "/owpc/pc/race/index"):
            return fallback("boat")

        html = fetch_url(base + path)
        time.sleep(2)

        sg_pattern   = r'(SG|G1|G2|G3)'
        time_pattern = r'(\d{2}:\d{2})'
        odds_pattern = r'(\d+\.\d)'

        sg_matches   = re.findall(sg_pattern, html)
        times        = re.findall(time_pattern, html)
        odds         = re.findall(odds_pattern, html)

        VENUES = ["桐生","戸田","江戸川","平和島","多摩川","浜名湖","蒲郡","常滑","津","三国",
                  "琵琶湖","住之江","尼崎","鳴門","丸亀","児島","宮島","徳山","下関","若松",
                  "芦屋","福岡","唐津","大村"]
        found_venues = [v for v in VENUES if v in html]

        if found_venues:
            grade     = sg_matches[0] if sg_matches else ""
            odds_list = [float(o) for o in odds[:12] if 1.0 <= float(o) <= 200.0][:6]

            # 注目レースのオッズを別途取得（SGの場合）
            if grade == "SG" and found_venues:
                try:
                    venue_code = _get_venue_code(found_venues[0])
                    if venue_code:
                        odds_url = f"{base}/owpc/pc/race/odds2tf?hd={today_ymd}&jcd={venue_code}&rno=6"
                        odds_html = fetch_url(odds_url)
                        time.sleep(1)
                        detailed_odds = re.findall(r'(\d+\.\d)', odds_html)
                        odds_list = [float(o) for o in detailed_odds[:12] if 1.0 <= float(o) <= 200.0][:6]
                except:
                    pass

            return {
                "sport": "boat",
                "name":  f"{found_venues[0]} 注目レース",
                "venue": found_venues[0],
                "time":  times[0] if times else "--:--",
                "grade": grade,
                "url":   "kyotei.html",
                "odds_list": odds_list
            }

    except Exception as e:
        print(f"[boat] エラー: {e}")

    return fallback("boat")


def _get_venue_code(venue_name):
    """競艇場名→場コード変換"""
    codes = {
        "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05",
        "浜名湖":"06","蒲郡":"07","常滑":"08","津":"09","三国":"10",
        "琵琶湖":"11","住之江":"12","尼崎":"13","鳴門":"14","丸亀":"15",
        "児島":"16","宮島":"17","徳山":"18","下関":"19","若松":"20",
        "芦屋":"21","福岡":"22","唐津":"23","大村":"24"
    }
    return codes.get(venue_name, "")


# ────────────────────────────────────────────────
# 競輪: keirin.jp からオッズ取得
# ⚠️ 利用規約要確認
# ────────────────────────────────────────────────
def fetch_cycle():
    try:
        base = "https://www.keirin.jp"
        path = "/pc/racetop.do"

        if not check_robots(base, path):
            return fallback("cycle")

        html = fetch_url(base + path)
        time.sleep(2)

        grade_pattern = r'(GP|G[123I]|FI|FII)'
        venue_pattern = r'([^\n<]{2,5}競輪場)'
        time_pattern  = r'(\d{1,2}:\d{2})'
        odds_pattern  = r'(\d+\.\d)'

        grades = re.findall(grade_pattern, html)
        venues = re.findall(venue_pattern, html)
        times  = re.findall(time_pattern, html)
        odds   = re.findall(odds_pattern, html)

        if venues:
            odds_list = [float(o) for o in odds[:9] if 1.0 <= float(o) <= 500.0][:7]
            return {
                "sport": "cycle",
                "name":  f"{venues[0]} 注目レース",
                "venue": venues[0],
                "time":  times[0] if times else "--:--",
                "grade": grades[0] if grades else "",
                "url":   "keirin.html",
                "odds_list": odds_list
            }

    except Exception as e:
        print(f"[cycle] エラー: {e}")

    return fallback("cycle")


def fallback(sport):
    labels = {"horse":"競馬","boat":"競艇","cycle":"競輪"}
    urls   = {"horse":"keiba.html","boat":"kyotei.html","cycle":"keirin.html"}
    return {"sport":sport,"name":f"{labels[sport]} 本日の注目レース",
            "venue":"詳細はページ内","time":"--:--","grade":"","url":urls[sport],"odds_list":[]}


# ────────────────────────────────────────────────
# Claude API: オッズ→EV計算＋予想文生成
# ────────────────────────────────────────────────
def generate_ev_prediction(races):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[Claude API] APIキー未設定。スキップします。")
        return races, ""

    # レース情報＋オッズをプロンプトに渡す
    races_text = ""
    for r in races:
        odds_str = "、".join([f"{i+1}番={o}倍" for i, o in enumerate(r.get("odds_list", []))])
        races_text += f"""
【{r['sport']} - {r['name']}】
  開催地：{r['venue']}　発走：{r['time']}　グレード：{r['grade']}
  オッズ（取得分）：{odds_str if odds_str else "取得できず"}
"""

    prompt = f"""あなたは競馬・競艇・競輪の予想専門家「たか」です。
今日（{today_str}）の以下のレース情報とオッズを元に、EV（期待値）分析に基づいた予想を生成してください。

{races_text}

【EV計算方法】
EV = 推定勝率 × オッズ
・EV > 1.0（100%超）→ 期待値プラス → 買い推奨
・EV < 1.0（100%未満）→ 期待値マイナス → 見送り推奨

以下の形式でJSONのみ返してください（説明文・コードブロック不要）:
{{
  "races": [
    {{
      "sport": "horse/boat/cycle",
      "name": "レース名",
      "venue": "開催地",
      "time": "発走時刻",
      "grade": "グレード",
      "url": "keiba.html/kyotei.html/keirin.html",
      "honmei": "本命の番号と名前",
      "ev": "+XX%（例：+18%）",
      "ev_calc": "推定勝率XX% × オッズX.X倍 = EV XX%",
      "reason": "60文字以内の予想根拠",
      "buy": "推奨買い目"
    }}
  ],
  "line_message": "LINE配信用テキスト（全競技まとめ・200文字程度・そのままコピペできる形式）"
}}

必須ルール：
- 「必ず勝てる」「確実」等の断定表現は絶対に使わない
- 「参考程度に」「自己責任で」の表現を含める
- オッズが取得できていない場合は一般的な分析で補完する
- line_messageはそのままLINEに送れる自然な日本語"""

    try:
        data = json.dumps({
            "model": "claude-opus-4-5",
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}]
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "Content-Type":    "application/json",
                "x-api-key":       api_key,
                "anthropic-version": "2023-06-01"
            },
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=30) as res:
            result = json.loads(res.read().decode("utf-8"))

        content = result["content"][0]["text"].strip()
        content = re.sub(r'^```json\s*', '', content)
        content = re.sub(r'\s*```$',     '', content)
        parsed  = json.loads(content)

        ev_races     = parsed.get("races", [])
        line_message = parsed.get("line_message", "")

        for i, race in enumerate(races):
            if i < len(ev_races):
                race.update({
                    "honmei":  ev_races[i].get("honmei",  ""),
                    "ev":      ev_races[i].get("ev",      ""),
                    "ev_calc": ev_races[i].get("ev_calc", ""),
                    "reason":  ev_races[i].get("reason",  ""),
                    "buy":     ev_races[i].get("buy",     "")
                })

        print("[Claude API] EV計算完了")
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
    print(f"[{today_str}] 完全自動化スクリプト開始（オッズ取得＋EV計算版）")

    # ① スクレイピング＋オッズ取得
    print("\n--- ① スクレイピング＋オッズ取得 ---")
    races = []
    races.append(fetch_horse())
    print(f"  競馬:  {races[-1]['name']} オッズ={races[-1].get('odds_list', [])}")
    races.append(fetch_boat())
    print(f"  競艇:  {races[-1]['name']} オッズ={races[-1].get('odds_list', [])}")
    races.append(fetch_cycle())
    print(f"  競輪:  {races[-1]['name']} オッズ={races[-1].get('odds_list', [])}")

    # ② Claude API でEV計算＋予想文生成
    print("\n--- ② EV計算（Claude API） ---")
    races, line_message = generate_ev_prediction(races)

    # ③ races.json生成（odds_listは除外して保存）
    print("\n--- ③ races.json生成 ---")
    for r in races:
        r.pop("odds_list", None)  # フロントには不要なので除外

    output = {
        "date":         today_str,
        "races":        races,
        "line_message": line_message
    }

    with open("races.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(json.dumps(output, ensure_ascii=False, indent=2))

    # LINE配信テキストを保存
    if line_message:
        with open("line_message.txt", "w", encoding="utf-8") as f:
            f.write(line_message)
        print(f"\n--- LINE配信テキスト ---\n{line_message}")

    # ④ FTPアップロード
    print("\n--- ④ FTPアップロード ---")
    upload_ftp()

    print("\n✅ 全処理完了")
