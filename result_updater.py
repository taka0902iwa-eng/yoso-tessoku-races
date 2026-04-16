"""
result_updater.py
競馬・競艇・競輪のレース結果を取得してresults.jsonを自動更新するモジュール。
scraper.pyのメイン処理から呼び出す、または単独で実行する。

使い方:
  python result_updater.py          # 昨日のレース結果を更新
  python result_updater.py 2026-04-14  # 指定日のレース結果を更新

改善点 (v2):
  - 競輪の実際の配当を取得（固定値2.5倍から変更）
  - 競艇の照合を会場・レース番号で絞り込み（誤照合防止）
  - 競馬の照合にレースIDを使用（同名馬の誤照合防止）
  - EV補正フィードバック: 的中/外れ履歴をev_calibration.jsonに保存
  - スポーツ別・会場別の的中率を計算してEV計算に反映
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from collections import defaultdict

JST = timezone(timedelta(hours=9))

# ──────────────────────────────────────────────
# 共通ユーティリティ
# ──────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}

def fetch_html(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        for enc in ("utf-8", "shift_jis", "euc-jp"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [WARN] fetch_html({url}): {e}")
        return ""

def load_results_json(path: str = "results.json") -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "summary": {"total": 0, "hit": 0, "hit_rate": 0.0,
                    "bet": 0, "return": 0, "recovery_rate": 0.0,
                    "profit": 0, "streak": 0},
        "by_sport": {"horse": {"total": 0, "hit": 0, "bet": 0, "return": 0},
                     "boat":  {"total": 0, "hit": 0, "bet": 0, "return": 0},
                     "cycle": {"total": 0, "hit": 0, "bet": 0, "return": 0}},
        "records": []
    }

def save_results_json(data: dict, path: str = "results.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  results.json 保存完了（{len(data['records'])}件）")

def recalc_summary(data: dict) -> dict:
    """records から summary / by_sport を再計算"""
    records = data.get("records", [])
    total = len(records)
    hit = sum(1 for r in records if r.get("result") == "hit")
    bet = sum(r.get("bet_amount", 0) for r in records)
    ret = sum(r.get("return_amount", 0) for r in records)
    hit_rate = round(hit / total * 100, 1) if total > 0 else 0.0
    recovery_rate = round(ret / bet * 100, 1) if bet > 0 else 0.0
    profit = ret - bet

    # 連続的中/外れ
    streak = 0
    if records:
        last_result = records[-1].get("result")
        for r in reversed(records):
            if r.get("result") == last_result:
                streak += 1
            else:
                break
        if last_result != "hit":
            streak = -streak

    # スポーツ別集計
    by_sport = {}
    for sport in ("horse", "boat", "cycle"):
        sp_records = [r for r in records if r.get("sport") == sport]
        sp_total = len(sp_records)
        sp_hit = sum(1 for r in sp_records if r.get("result") == "hit")
        sp_bet = sum(r.get("bet_amount", 0) for r in sp_records)
        sp_ret = sum(r.get("return_amount", 0) for r in sp_records)
        by_sport[sport] = {
            "total": sp_total, "hit": sp_hit,
            "bet": sp_bet, "return": sp_ret,
            "hit_rate": round(sp_hit / sp_total * 100, 1) if sp_total > 0 else 0.0,
            "recovery_rate": round(sp_ret / sp_bet * 100, 1) if sp_bet > 0 else 0.0,
        }

    data["summary"] = {
        "total": total, "hit": hit, "hit_rate": hit_rate,
        "bet": bet, "return": ret, "recovery_rate": recovery_rate,
        "profit": profit, "streak": streak
    }
    data["by_sport"] = by_sport
    return data


# ──────────────────────────────────────────────
# EV補正フィードバック
# ──────────────────────────────────────────────
def load_ev_calibration(path: str = "ev_calibration.json") -> dict:
    """EV補正データを読み込む"""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "by_sport": {
            "horse": {"predicted_ev_sum": 0.0, "actual_return_sum": 0.0, "count": 0},
            "boat":  {"predicted_ev_sum": 0.0, "actual_return_sum": 0.0, "count": 0},
            "cycle": {"predicted_ev_sum": 0.0, "actual_return_sum": 0.0, "count": 0},
        },
        "by_venue": {},
        "calibration_factors": {
            "horse": 1.0,
            "boat":  1.0,
            "cycle": 1.0,
        },
        "last_updated": ""
    }

def save_ev_calibration(data: dict, path: str = "ev_calibration.json"):
    """EV補正データを保存する"""
    data["last_updated"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ev_calibration.json 保存完了")

def update_ev_calibration(records: list, calib_path: str = "ev_calibration.json"):
    """
    的中/外れ履歴からEV補正係数を更新する。
    補正係数 = 実際の回収率 / 予測EV平均
    この係数をscraper.pyのEV計算に掛けることで、過去の実績に基づいた補正が可能。
    """
    calib = load_ev_calibration(calib_path)

    for record in records:
        sport = record.get("sport", "")
        venue = record.get("venue", "")
        result = record.get("result", "")
        bet_amount = record.get("bet_amount", 1000)
        return_amount = record.get("return_amount", 0)
        ev_str = record.get("ev", "")

        if result not in ("hit", "miss"):
            continue

        # EV文字列を数値に変換（例: "+30%" → 1.30）
        ev_num = 1.0
        if ev_str:
            ev_m = re.search(r'([+-]?\d+)%', ev_str)
            if ev_m:
                ev_num = 1.0 + int(ev_m.group(1)) / 100.0

        # 実際の回収率（的中なら return/bet、外れなら0）
        actual_return_rate = return_amount / bet_amount if bet_amount > 0 else 0.0

        # スポーツ別集計
        if sport in calib["by_sport"]:
            calib["by_sport"][sport]["predicted_ev_sum"] += ev_num
            calib["by_sport"][sport]["actual_return_sum"] += actual_return_rate
            calib["by_sport"][sport]["count"] += 1

        # 会場別集計
        venue_key = f"{sport}_{venue}"
        if venue_key not in calib["by_venue"]:
            calib["by_venue"][venue_key] = {
                "predicted_ev_sum": 0.0, "actual_return_sum": 0.0, "count": 0
            }
        calib["by_venue"][venue_key]["predicted_ev_sum"] += ev_num
        calib["by_venue"][venue_key]["actual_return_sum"] += actual_return_rate
        calib["by_venue"][venue_key]["count"] += 1

    # 補正係数を再計算（最低10件以上のデータがある場合のみ）
    for sport in ("horse", "boat", "cycle"):
        sp = calib["by_sport"][sport]
        if sp["count"] >= 10 and sp["predicted_ev_sum"] > 0:
            actual_avg = sp["actual_return_sum"] / sp["count"]
            predicted_avg = sp["predicted_ev_sum"] / sp["count"]
            # 補正係数: 実績/予測（0.7〜1.3の範囲に制限）
            factor = max(0.7, min(1.3, actual_avg / predicted_avg))
            calib["calibration_factors"][sport] = round(factor, 3)
            print(f"  EV補正係数更新: {sport} = {factor:.3f} "
                  f"(実績{actual_avg:.3f} / 予測{predicted_avg:.3f}, {sp['count']}件)")

    save_ev_calibration(calib, calib_path)
    return calib


# ──────────────────────────────────────────────
# 競馬結果取得（netkeiba）
# ──────────────────────────────────────────────
def fetch_horse_results(date_str: str) -> list:
    """
    指定日の競馬レース結果を取得する。
    date_str: "2026-04-14" 形式
    返り値: [{"race_id": "...", "winner": "馬名", "odds": 3.5, "venue": "東京"}, ...]
    """
    ymd = date_str.replace("-", "")
    url = f"https://race.netkeiba.com/top/race_list.html?kaisai_date={ymd}"
    html = fetch_html(url)
    if not html:
        return []

    results = []
    # レースIDを抽出
    race_ids = re.findall(r'race/result\.html\?race_id=(\d{12})', html)
    race_ids = list(dict.fromkeys(race_ids))  # 重複除去

    for race_id in race_ids[:20]:  # 最大20レース
        result_url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}"
        result_html = fetch_html(result_url)
        if not result_html:
            continue
        time.sleep(0.5)

        # 1着馬名を取得（複数パターン対応）
        winner = ""
        # パターン1: HorseList クラスの1着行
        winner_m = re.search(
            r'<tr[^>]*class="[^"]*HorseList[^"]*"[^>]*>.*?<td[^>]*class="[^"]*Umaban[^"]*"[^>]*>\s*1\s*</td>.*?<span[^>]*class="[^"]*HorseName[^"]*"[^>]*>\s*([^<]+?)\s*</span>',
            result_html, re.DOTALL
        )
        if winner_m:
            winner = winner_m.group(1).strip()
        if not winner:
            # パターン2: Rank クラスの1着行
            winner_m = re.search(
                r'<td[^>]*class="[^"]*Rank[^"]*"[^>]*>\s*1\s*</td>.*?<a[^>]*>([^<]{2,10})</a>',
                result_html, re.DOTALL
            )
            if winner_m:
                winner = winner_m.group(1).strip()
        if not winner:
            # パターン3: 1着の直後の馬名
            winner_m = re.search(r'1着[^<]*<[^>]*>([^\d<]{2,10})</[^>]*>', result_html)
            if winner_m:
                winner = winner_m.group(1).strip()

        # 単勝オッズ
        odds = 0.0
        odds_m = re.search(
            r'<td[^>]*class="[^"]*Tansho[^"]*"[^>]*>.*?<span[^>]*>([0-9.]+)</span>',
            result_html, re.DOTALL
        )
        if odds_m:
            try:
                odds = float(odds_m.group(1))
            except:
                pass

        # 会場名（race_idの5-6文字目が場コード）
        venue_code = race_id[4:6]
        venue_map = {
            "01":"札幌","02":"函館","03":"福島","04":"新潟","05":"東京",
            "06":"中山","07":"中京","08":"京都","09":"阪神","10":"小倉"
        }
        venue = venue_map.get(venue_code, "")

        if winner:
            results.append({
                "race_id": race_id,
                "winner": winner,
                "odds": odds,
                "venue": venue,
                "url": result_url
            })

    print(f"  競馬結果取得: {len(results)}件 ({date_str})")
    return results


# ──────────────────────────────────────────────
# 競艇結果取得（ボートレース公式）
# ──────────────────────────────────────────────
def fetch_boat_results(date_str: str) -> list:
    """
    指定日の競艇レース結果を取得する（公式サイト）。
    返り値: [{"race_id": "...", "jcd": "01", "rno": "1", "winner_boat": "1",
              "winner_name": "選手名", "odds": 3.5, "venue": "桐生"}, ...]
    """
    ymd = date_str.replace("-", "")
    url = f"https://boatrace.jp/owpc/pc/race/resultlist?hd={ymd}"
    html = fetch_html(url)
    if not html:
        return []

    # 会場コードと会場名のマッピング
    venue_map = {
        "01":"桐生","02":"戸田","03":"江戸川","04":"平和島","05":"多摩川",
        "06":"浜名湖","07":"蒲郡","08":"常滑","09":"津","10":"三国",
        "11":"琵琶湖","12":"住之江","13":"尼崎","14":"鳴門","15":"丸亀",
        "16":"児島","17":"宮島","18":"徳山","19":"下関","20":"若松",
        "21":"芦屋","22":"福岡","23":"唐津","24":"大村"
    }

    results = []
    # レース結果リンクを抽出（jcd・rno・hdを取得）
    race_links = re.findall(
        r'href="[^"]*result[^"]*jcd=(\d{2})[^"]*rno=(\d{1,2})[^"]*hd=(\d{8})[^"]*"',
        html
    )
    if not race_links:
        # 別パターン
        race_links = re.findall(
            r'raceresult\?rno=(\d{1,2})&jcd=(\d{2})&hd=(\d{8})',
            html
        )
        race_links = [(jcd, rno, hd) for rno, jcd, hd in race_links]

    race_links = list(dict.fromkeys(race_links))[:60]  # 最大60レース

    for jcd, rno, hd in race_links:
        result_url = f"https://boatrace.jp/owpc/pc/race/raceresult?rno={rno}&jcd={jcd}&hd={hd}"
        result_html = fetch_html(result_url)
        if not result_html:
            continue
        time.sleep(0.3)

        # 1着艇番（複数パターン対応）
        winner_boat = ""
        # パターン1: is-rank1 クラス
        winner_m = re.search(
            r'class="[^"]*is-rank1[^"]*"[^>]*>.*?<span[^>]*>([1-6])</span>',
            result_html, re.DOTALL
        )
        if winner_m:
            winner_boat = winner_m.group(1)
        if not winner_boat:
            # パターン2: 1着の直後の艇番
            winner_m = re.search(r'1着.*?([1-6])号艇', result_html)
            if winner_m:
                winner_boat = winner_m.group(1)
        if not winner_boat:
            # パターン3: 結果テーブルの1行目
            winner_m = re.search(r'<tr[^>]*>.*?<td[^>]*>1</td>.*?<td[^>]*>([1-6])</td>', result_html, re.DOTALL)
            if winner_m:
                winner_boat = winner_m.group(1)

        # 1着選手名
        winner_name = ""
        name_m = re.search(
            r'class="[^"]*is-rank1[^"]*"[^>]*>.*?toban=\d+"[^>]*>([^<]+)</a>',
            result_html, re.DOTALL
        )
        if name_m:
            winner_name = re.sub(r'\s+', ' ', name_m.group(1).strip())

        # 単勝配当（円）
        odds = 0.0
        odds_m = re.search(r'単勝.*?([0-9,]+)円', result_html, re.DOTALL)
        if odds_m:
            try:
                odds = float(odds_m.group(1).replace(",", "")) / 100.0
            except:
                pass

        if winner_boat:
            results.append({
                "race_id": f"{jcd}_{rno}_{hd}",
                "jcd": jcd, "rno": rno, "hd": hd,
                "winner_boat": winner_boat,
                "winner_name": winner_name,
                "odds": odds,
                "venue": venue_map.get(jcd, ""),
                "url": result_url
            })

    print(f"  競艇結果取得: {len(results)}件 ({date_str})")
    return results


# ──────────────────────────────────────────────
# 競輪結果取得（Kドリームス）
# ──────────────────────────────────────────────
def fetch_cycle_results(date_str: str) -> list:
    """
    指定日の競輪レース結果を取得する（Kドリームス）。
    返り値: [{"race_id": "...", "slug": "hakodate", "rno": "01",
              "winner": "選手名", "odds": 2.5, "venue": "函館"}, ...]
    """
    ymd = date_str.replace("-", "")
    url = f"https://keirin.kdreams.jp/racecard/{ymd[:4]}/{ymd[4:6]}/{ymd[6:8]}/"
    html = fetch_html(url)
    if not html:
        return []

    # 開催場スラッグと会場名のマッピング
    slug_to_venue = {
        "hakodate":"函館","toride":"取手","matsudo":"松戸","takasaki":"高崎",
        "omiya":"大宮","keiokaku":"京王閣","izu":"伊豆","odawara":"小田原",
        "hiratsuka":"平塚","kofu":"甲府","nagaoka":"長岡","kanazawa":"金沢",
        "fukui":"福井","gifu":"岐阜","toyohashi":"豊橋","nagoya":"名古屋",
        "wakayama":"和歌山","kishiwada":"岸和田","nishimiya":"西宮","amagasaki":"尼崎",
        "takamatsu":"高松","matsuyama":"松山","kochi":"高知","kokura":"小倉",
        "kurume":"久留米","saga":"佐賀","kumamoto":"熊本","kagoshima":"鹿児島",
        "naha":"那覇","sendai":"仙台","fukushima":"福島","mito":"水戸",
        "utsunomiya":"宇都宮","maebashi":"前橋","kawasaki":"川崎","chiba":"千葉",
        "tachikawa":"立川","matsumoto":"松本","shizuoka":"静岡","hamamatsu":"浜松",
        "toyama":"富山","biwa":"びわこ","sakai":"堺","shimonoseki":"下関",
        "takamatsu":"高松","tokushima":"徳島","beppu":"別府","miyazaki":"宮崎",
    }

    results = []
    # 開催場スラッグを取得（相対パス・絶対URL両対応）
    venue_slugs = re.findall(r'href="(?:https://keirin\.kdreams\.jp)?/([a-z]+)/racecard/\d{8}/', html)
    venue_slugs = list(dict.fromkeys(venue_slugs))

    for slug in venue_slugs[:15]:
        venue_name = slug_to_venue.get(slug, slug)
        venue_url = f"https://keirin.kdreams.jp/{slug}/racecard/{ymd}/"
        venue_html = fetch_html(venue_url)
        if not venue_html:
            continue
        time.sleep(0.5)

        # レース結果URLを取得（result ページ）
        result_links = re.findall(
            rf'href="(?:https://keirin\.kdreams\.jp)?/{slug}/result/\d{{8}}/(\d{{2}})/"',
            venue_html
        )
        if not result_links:
            result_links = re.findall(
                rf'/{slug}/result/{ymd}/(\d{{2}})/',
                venue_html
            )
        result_links = list(dict.fromkeys(result_links))

        for rno in result_links[:12]:
            result_url = f"https://keirin.kdreams.jp/{slug}/result/{ymd}/{rno}/"
            result_html = fetch_html(result_url)
            if not result_html:
                continue
            time.sleep(0.3)

            # 1着選手名（複数パターン対応）
            winner = ""
            # パターン1: rank1 クラス
            winner_m = re.search(
                r'class="[^"]*rank1[^"]*"[^>]*>.*?<td[^>]*class="[^"]*rider[^"]*"[^>]*>([^<]+)</td>',
                result_html, re.DOTALL
            )
            if winner_m:
                winner = winner_m.group(1).strip()
            if not winner:
                # パターン2: 1着の直後の選手名
                winner_m = re.search(
                    r'1着[^<]*<[^>]*>([^\d<\s]{2,10})[^<]*</[^>]*>',
                    result_html
                )
                if winner_m:
                    winner = winner_m.group(1).strip()
            if not winner:
                # パターン3: テーブルの1行目の選手名
                tr_rows = re.findall(r'<tr[^>]*>(.*?)</tr>', result_html, re.DOTALL)
                for row in tr_rows[:5]:
                    cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                    cells_text = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                    if cells_text and cells_text[0] == '1' and len(cells_text) >= 2:
                        # 2列目以降に選手名らしい文字列を探す
                        for ct in cells_text[1:5]:
                            if re.search(r'[\u3040-\u9fff]{2,}', ct) and len(ct) >= 2:
                                winner = ct
                                break
                        if winner:
                            break

            # 単勝配当（円）
            odds = 0.0
            # パターン1: 単勝配当テーブル
            odds_m = re.search(r'単勝[^<]*<[^>]*>([0-9,]+)円', result_html)
            if odds_m:
                try:
                    odds = float(odds_m.group(1).replace(",", "")) / 100.0
                except:
                    pass
            if odds == 0.0:
                # パターン2: 配当テーブルの数値
                odds_m = re.search(r'([0-9,]{3,6})円', result_html)
                if odds_m:
                    try:
                        odds_val = float(odds_m.group(1).replace(",", ""))
                        if 100 <= odds_val <= 99900:
                            odds = odds_val / 100.0
                    except:
                        pass

            if winner:
                results.append({
                    "race_id": f"{slug}_{rno}_{ymd}",
                    "slug": slug, "rno": rno, "date": date_str,
                    "winner": winner,
                    "odds": odds,
                    "venue": venue_name,
                    "url": result_url
                })

    print(f"  競輪結果取得: {len(results)}件 ({date_str})")
    return results


# ──────────────────────────────────────────────
# races.jsonの予想と結果を照合してresults.jsonを更新
# ──────────────────────────────────────────────
def match_and_update(
    date_str: str,
    races_json_path: str = "races.json",
    results_json_path: str = "results.json",
    calib_path: str = "ev_calibration.json"
) -> dict:
    """
    races.jsonの予想データと実際の結果を照合し、results.jsonを更新する。
    改善点:
    - 競艇: 会場コード・レース番号で絞り込み（誤照合防止）
    - 競馬: レースIDで絞り込み（同名馬の誤照合防止）
    - 競輪: 実際の配当を取得（固定値から変更）
    - EV補正フィードバック: 的中/外れ履歴をev_calibration.jsonに保存
    """
    print(f"\n=== 結果照合・更新 ({date_str}) ===")

    # races.jsonを読み込む
    if not os.path.exists(races_json_path):
        print(f"  [SKIP] {races_json_path} が見つかりません")
        return {}

    with open(races_json_path, "r", encoding="utf-8") as f:
        races_data = json.load(f)

    races = races_data.get("races", [])
    if not races:
        print("  [SKIP] races が空です")
        return {}

    # 予想があるレースのみ対象
    target_races = [r for r in races if r.get("honmei") and r.get("judge") in ["強買い", "買い"]]
    if not target_races:
        print("  [SKIP] 対象予想なし")
        return {}

    print(f"  対象予想: {len(target_races)}件")

    # 各スポーツの結果を取得
    horse_results = fetch_horse_results(date_str)
    boat_results  = fetch_boat_results(date_str)
    cycle_results = fetch_cycle_results(date_str)

    # 競艇: 会場別・レース番号別にインデックスを作成
    boat_index = {}  # (jcd, rno) -> result
    for br in boat_results:
        key = (br["jcd"], br["rno"])
        boat_index[key] = br

    # 競艇: 会場名 -> jcd のマッピング
    venue_to_jcd = {
        "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05",
        "浜名湖":"06","蒲郡":"07","常滑":"08","津":"09","三国":"10",
        "琵琶湖":"11","住之江":"12","尼崎":"13","鳴門":"14","丸亀":"15",
        "児島":"16","宮島":"17","徳山":"18","下関":"19","若松":"20",
        "芦屋":"21","福岡":"22","唐津":"23","大村":"24"
    }

    # results.jsonを読み込む
    results_data = load_results_json(results_json_path)
    existing_ids = {r.get("race_id") for r in results_data.get("records", [])}

    new_records = []
    for race in target_races:
        sport = race.get("sport", "")
        venue = race.get("venue", "")
        honmei = race.get("honmei", "")
        race_name = race.get("name", "")
        ev = race.get("ev", "")
        judge = race.get("judge", "")
        race_time = race.get("time", "")

        # レースIDを生成
        race_id = f"{date_str}_{sport}_{venue}_{race_name}".replace(" ", "_")
        if race_id in existing_ids:
            print(f"  [SKIP] 既存レコード: {race_id}")
            continue

        result = "pending"
        return_amount = 0
        bet_amount = 1000  # デフォルト賭け金
        actual_odds = 0.0

        # ── 競馬の照合 ──────────────────────────────────────────────────────────
        if sport == "horse":
            # races.jsonにrace_idが含まれている場合はそれを使用
            race_id_in_json = race.get("race_id", "")
            for hr in horse_results:
                # レースIDが一致する場合（最優先）
                if race_id_in_json and hr.get("race_id") == race_id_in_json:
                    if honmei and honmei in hr.get("winner", ""):
                        result = "hit"
                        actual_odds = hr.get("odds", 0.0)
                        return_amount = int(actual_odds * bet_amount)
                    else:
                        result = "miss"
                    break
                # 会場名が一致する場合
                elif venue and hr.get("venue") == venue.replace("競馬場", ""):
                    if honmei and honmei in hr.get("winner", ""):
                        result = "hit"
                        actual_odds = hr.get("odds", 0.0)
                        return_amount = int(actual_odds * bet_amount)
                        break
            else:
                if horse_results:
                    result = "miss"

        # ── 競艇の照合 ──────────────────────────────────────────────────────────
        elif sport == "boat":
            # honmeiから艇番を抽出（例: "1号艇 山田太郎" → "1"）
            boat_num_m = re.search(r'(\d)号艇|(\d)番', honmei)
            boat_num = (boat_num_m.group(1) or boat_num_m.group(2)) if boat_num_m else ""

            # 会場名からjcdを取得
            venue_clean = venue.replace("競艇場", "").replace("ボートレース", "").strip()
            jcd = venue_to_jcd.get(venue_clean, "")

            # レース番号を抽出（race_nameから）
            rno_m = re.search(r'(\d{1,2})R', race_name)
            rno = rno_m.group(1).zfill(2) if rno_m else ""

            if jcd and rno:
                # 会場・レース番号で絞り込み
                br = boat_index.get((jcd, rno))
                if br:
                    if boat_num and boat_num == br.get("winner_boat", ""):
                        result = "hit"
                        actual_odds = br.get("odds", 0.0)
                        return_amount = int(actual_odds * bet_amount)
                    else:
                        result = "miss"
                elif boat_results:
                    result = "miss"
            else:
                # jcdまたはrnoが取得できない場合は全結果から検索
                for br in boat_results:
                    if boat_num and boat_num == br.get("winner_boat", ""):
                        result = "hit"
                        actual_odds = br.get("odds", 0.0)
                        return_amount = int(actual_odds * bet_amount)
                        break
                else:
                    if boat_results:
                        result = "miss"

        # ── 競輪の照合 ──────────────────────────────────────────────────────────
        elif sport == "cycle":
            # 会場名で絞り込み
            venue_clean = venue.replace("競輪場", "").strip()
            venue_results = [cr for cr in cycle_results
                           if cr.get("venue") == venue_clean or venue_clean in cr.get("venue", "")]

            if not venue_results:
                venue_results = cycle_results  # 会場が絞り込めない場合は全結果

            for cr in venue_results:
                if honmei and honmei in cr.get("winner", ""):
                    result = "hit"
                    actual_odds = cr.get("odds", 0.0)
                    # 実際の配当が取得できた場合はそれを使用、できない場合は推定
                    if actual_odds > 0:
                        return_amount = int(actual_odds * bet_amount)
                    else:
                        # 競輪の単勝平均配当（約2.5倍）で推定
                        return_amount = int(bet_amount * 2.5)
                    break
            else:
                if cycle_results:
                    result = "miss"

        if result == "pending":
            print(f"  [PENDING] {sport} {venue} {race_name} - 結果未取得")
            continue

        record = {
            "race_id": race_id,
            "date": date_str,
            "sport": sport,
            "name": race_name,
            "venue": venue,
            "honmei": honmei,
            "ev": ev,
            "judge": judge,
            "result": result,
            "bet_amount": bet_amount,
            "return_amount": return_amount if result == "hit" else 0,
            "actual_odds": actual_odds,
            "memo": race.get("reason", "")
        }
        new_records.append(record)
        print(f"  {'✅ 的中' if result == 'hit' else '❌ 外れ'}: {sport} {venue} {race_name} "
              f"({honmei}) 配当:{actual_odds:.1f}倍")

    if new_records:
        results_data["records"].extend(new_records)
        results_data = recalc_summary(results_data)
        save_results_json(results_data, results_json_path)
        print(f"\n  新規レコード追加: {len(new_records)}件")
        print(f"  累計: {results_data['summary']['total']}件 / "
              f"回収率: {results_data['summary']['recovery_rate']}%")

        # EV補正フィードバックを更新
        update_ev_calibration(new_records, calib_path)
    else:
        print("  新規レコードなし")

    return results_data


# ──────────────────────────────────────────────
# FTPアップロード
# ──────────────────────────────────────────────
def upload_results_ftp(results_json_path: str = "results.json",
                       calib_json_path: str = "ev_calibration.json"):
    """results.jsonとev_calibration.jsonをFTPでアップロードする"""
    import ftplib
    host = (os.environ.get("FTP_HOST", "") or "").strip().replace("\n","").replace("\r","").replace(" ","")
    user = (os.environ.get("FTP_USER", "") or "").strip().replace("\n","").replace("\r","").replace(" ","")
    passwd = (os.environ.get("FTP_PASS", "") or "").strip().replace("\n","").replace("\r","")
    remote_base = (os.environ.get("FTP_REMOTE",
        "/home/c9048134/public_html/oyatojikka.online/races.json") or "").strip()

    if not all([host, user, passwd]):
        print("  [SKIP] FTP環境変数が設定されていません")
        return

    parts = [p for p in remote_base.split("/") if p]
    remote_dir = "/" + "/".join(parts[:-1])

    try:
        ftp = ftplib.FTP()
        ftp.connect(host, 21, timeout=30)
        ftp.login(user, passwd)
        ftp.set_pasv(True)

        # results.json
        remote_path = remote_dir + "/results.json"
        with open(results_json_path, "rb") as f:
            ftp.storbinary(f"STOR {remote_path}", f)
        print(f"  results.json FTPアップロード完了: {remote_path}")

        # ev_calibration.json
        if os.path.exists(calib_json_path):
            calib_path = remote_dir + "/ev_calibration.json"
            with open(calib_json_path, "rb") as f:
                ftp.storbinary(f"STOR {calib_path}", f)
            print(f"  ev_calibration.json FTPアップロード完了: {calib_path}")

        ftp.quit()
    except Exception as e:
        print(f"  [ERROR] FTPアップロード失敗: {e}")


# ──────────────────────────────────────────────
# メインエントリポイント
# ──────────────────────────────────────────────
def run_result_update(target_date: str = None):
    """
    メイン処理。scraper.pyから呼び出す場合はこの関数を使う。
    target_date: "2026-04-14" 形式。Noneの場合は昨日の日付を使用。
    """
    if target_date is None:
        yesterday = datetime.now(JST) - timedelta(days=1)
        target_date = yesterday.strftime("%Y-%m-%d")

    print(f"\n{'='*50}")
    print(f"結果自動更新: {target_date}")
    print(f"{'='*50}")

    results = match_and_update(target_date)
    if results:
        upload_results_ftp()
    return results

if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run_result_update(date_arg)
