"""
予想の鉄則 - 完全自動化スクリプト（精度向上版）

【競馬EV計算式】
J = 0.4*tansho_return + 0.3*course_return + 0.3*recent5_stability
score = (B/C) * (C/(C+3)) * ((F-E+1)/F) * J * weight_adj * jockey_adj * condition_adj

【競輪EV計算式】
K = 0.4*line_rate + 0.3*bank_rate + 0.3*recent_form
score = (B/C) * (C/(C+3)) * ((F-E+1)/F) * K * frame_adj * style_adj * trend_adj

【精度向上項目】
競馬: 斤量補正・騎手調子・馬場状態・脚質・着差トレンド
競輪: 枠番補正・脚質補正・着順トレンド・実績蓄積
"""

import json, re, time, ftplib, os, sys, concurrent.futures
import urllib.request, urllib.robotparser
from datetime import datetime, timezone, timedelta
from collections import defaultdict

JST       = timezone(timedelta(hours=9))
today     = datetime.now(JST)
today_str = today.strftime("%Y-%m-%d")
today_ymd = today.strftime("%Y%m%d")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# 的中履歴（EV補正用）
HISTORY_FILE = "ev_history.json"

def load_history():
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return {"horse": [], "cycle": [], "boat": []}

def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except:
        pass

def calc_history_correction(history, sport, name):
    """過去の的中履歴からEV補正係数を計算"""
    records = [r for r in history.get(sport, []) if r.get("name") == name]
    if len(records) < 5:
        return 1.0
    hits  = sum(1 for r in records if r.get("result") == "hit")
    rate  = hits / len(records)
    # 的中率が高い選手/馬はEVを最大10%上乗せ
    return min(1.1, max(0.9, 0.9 + rate * 0.2))

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def check_robots(base, path):
    try:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        rp.read()
        return rp.can_fetch("Mozilla/5.0", base + path)
    except:
        return True

def fetch(url, timeout=20, retries=3):
    import gzip as _gz
    url_encoded = url.encode("ascii", errors="ignore").decode("ascii")
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url_encoded, headers=HEADERS)
            req.add_header("Referer", "https://www.google.co.jp/")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                    try:
                        raw = _gz.decompress(raw)
                    except Exception:
                        pass
                for enc in ["utf-8", "shift_jis", "euc-jp", "latin-1"]:
                    try:
                        return raw.decode(enc)
                    except:
                        continue
                return raw.decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise last_err


# ══════════════════════════════════════════════════
# 競馬 EV計算（全改善版）
# ══════════════════════════════════════════════════

# ── 距離帯マスタ ─────────────────────────────────
def get_distance_type(distance_m):
    if distance_m <= 1400: return "短距離"
    if distance_m <= 1800: return "マイル"
    if distance_m <= 2200: return "中距離"
    return "長距離"

# ── 条件別EV閾値 ─────────────────────────────────
EV_THRESHOLD_HORSE = {
    ("芝", "短距離", "良"):   (1.20, 1.05),
    ("芝", "短距離", "稍重"): (1.25, 1.08),
    ("芝", "短距離", "重"):   (1.30, 1.10),
    ("芝", "マイル", "良"):   (1.20, 1.05),
    ("芝", "中距離", "良"):   (1.22, 1.05),
    ("芝", "長距離", "良"):   (1.25, 1.08),
    ("ダ", "短距離", "良"):   (1.20, 1.05),
    ("ダ", "中距離", "良"):   (1.22, 1.05),
    ("ダ", "長距離", "良"):   (1.25, 1.08),
}

def get_ev_threshold_horse(track_type, distance_type, condition):
    tt = "ダ" if "ダ" in track_type else "芝"
    return EV_THRESHOLD_HORSE.get((tt, distance_type, condition), (1.25, 1.05))

# ── Priority1: 直近5・10走の重み付け ────────────
WEIGHT_RECENT5  = 0.50
WEIGHT_RECENT10 = 0.30
WEIGHT_TOTAL    = 0.20

def calc_weighted_win_rate(wins_r5, starts_r5, wins_r10, starts_r10, wins_t, starts_t):
    r5  = wins_r5  / starts_r5  if starts_r5  > 0 else None
    r10 = wins_r10 / starts_r10 if starts_r10 > 0 else None
    rt  = wins_t   / starts_t   if starts_t   > 0 else 0.12
    if r5 is not None and r10 is not None:
        return WEIGHT_RECENT5 * r5 + WEIGHT_RECENT10 * r10 + WEIGHT_TOTAL * rt
    elif r5 is not None:
        return 0.65 * r5 + 0.35 * rt
    elif r10 is not None:
        return 0.65 * r10 + 0.35 * rt
    return rt

# ── Priority2: 乗り替わり補正 ────────────────────
def calc_jockey_change_adj(is_change, jockey_horse_wins, jockey_horse_starts,
                            jockey_recent_rate):
    if not is_change:
        if jockey_horse_starts >= 3:
            h_rate = jockey_horse_wins / jockey_horse_starts
            return min(1.15, max(0.90, 0.90 + h_rate * 2.0))
        return 1.02
    else:
        return min(1.10, max(0.80, 0.80 + jockey_recent_rate * 1.5))

# ── Priority3: コース・距離・馬場適性 ────────────
def calc_course_fit_adj(wins_course, starts_course, wins_distance,
                         starts_distance, wins_condition, starts_condition,
                         overall_rate):
    adjs = []
    if starts_course >= 3 and overall_rate > 0:
        adjs.append(min(1.15, max(0.85, (wins_course/starts_course) / overall_rate)))
    if starts_distance >= 3 and overall_rate > 0:
        adjs.append(min(1.15, max(0.85, (wins_distance/starts_distance) / overall_rate)))
    if starts_condition >= 2 and overall_rate > 0:
        adjs.append(min(1.12, max(0.88, (wins_condition/starts_condition) / overall_rate)))
    return round(sum(adjs)/len(adjs), 3) if adjs else 1.0

# ── Priority4: 調教評価補正 ──────────────────────
TRAINING_ADJ = {"S":1.10,"A":1.05,"B":1.00,"C":0.95,"D":0.88}

def calc_training_adj(training_eval, training_time_diff):
    base  = TRAINING_ADJ.get(training_eval, 1.0)
    t_adj = min(1.05, max(0.95, 1.0 - float(training_time_diff) * 0.1))
    return round(base * t_adj, 3)

# ── Priority5: 展開・脚質・上がり3F ─────────────
def calc_pace_adj(running_style, field_style_counts, avg_last3f, field_avg_last3f):
    escape_count = field_style_counts.get("逃げ", 0)
    front_count  = field_style_counts.get("先行", 0)
    front_heavy  = (escape_count + front_count) >= 4
    style_adj    = 1.0
    if front_heavy:
        if running_style in ["差し","追い込み"]: style_adj = 1.06
        if running_style == "逃げ":              style_adj = 0.94
    else:
        if running_style in ["逃げ","先行"]:     style_adj = 1.04
        if running_style == "追い込み":           style_adj = 0.97
    last3f_adj = 1.0
    if field_avg_last3f > 0 and avg_last3f > 0:
        diff = field_avg_last3f - avg_last3f
        last3f_adj = min(1.10, max(0.90, 1.0 + diff * 0.03))
    return round(style_adj * last3f_adj, 3)

# ── Priority6: 人気乖離補正 ──────────────────────
def calc_popularity_gap_adj(predicted_rank, actual_popularity):
    gap = actual_popularity - predicted_rank
    if gap >= 3:  return 1.08
    if gap >= 1:  return 1.03
    if gap == 0:  return 1.00
    if gap == -1: return 0.98
    return 0.95

# ── メインスコア計算 ─────────────────────────────
def calc_J(tansho_return, course_return, recent5_stability):
    return 0.4 * tansho_return + 0.3 * course_return + 0.3 * recent5_stability

def calc_score_horse(horse):
    C  = float(horse.get("C",   0))
    E  = float(horse.get("E",   0))
    F  = float(horse.get("F",   0))
    tr = float(horse.get("tansho_return",    1.0))
    cr = float(horse.get("course_return",    1.0))
    rs = float(horse.get("recent5_stability",1.0))
    if C <= 0 or F <= 0 or E <= 0: return 0.0

    # Priority1: 直近重み付け勝率
    w_r5  = float(horse.get("wins_recent5",   horse.get("B", 1)))
    s_r5  = float(horse.get("starts_recent5", max(C * 0.3, 1)))
    w_r10 = float(horse.get("wins_recent10",  horse.get("B", 1)))
    s_r10 = float(horse.get("starts_recent10",max(C * 0.6, 1)))
    w_t   = float(horse.get("B", 1))
    base_rate = calc_weighted_win_rate(w_r5, s_r5, w_r10, s_r10, w_t, C)

    J           = calc_J(tr, cr, rs)
    stability   = C / (C + 3)
    market_edge = (F - E + 1) / F

    # Priority2: 乗り替わり補正
    jockey_adj = calc_jockey_change_adj(
        horse.get("is_jockey_change", False),
        float(horse.get("jockey_horse_wins",   0)),
        float(horse.get("jockey_horse_starts", 0)),
        float(horse.get("jockey_recent_rate",  0.1))
    )

    # Priority3: コース・距離・馬場適性
    overall_rate = w_t / C if C > 0 else 0.12
    course_adj   = calc_course_fit_adj(
        float(horse.get("wins_course",     0)),
        float(horse.get("starts_course",   0)),
        float(horse.get("wins_distance",   0)),
        float(horse.get("starts_distance", 0)),
        float(horse.get("wins_condition",  0)),
        float(horse.get("starts_condition",0)),
        overall_rate
    )

    # Priority4: 調教補正
    training_adj = calc_training_adj(
        horse.get("training_eval",      "B"),
        float(horse.get("training_time_diff", 0.0))
    )

    # Priority5: 展開・脚質・上がり3F
    pace_adj = calc_pace_adj(
        horse.get("running_style",        "先行"),
        horse.get("field_style_counts",   {}),
        float(horse.get("avg_last3f",     36.0)),
        float(horse.get("field_avg_last3f",36.0))
    )

    # 斤量補正
    wd         = float(horse.get("weight_diff", 0.0))
    weight_adj = max(0.85, min(1.10, 1.0 - wd * 0.02))

    return (base_rate * stability * market_edge * J
            * jockey_adj * course_adj * training_adj
            * pace_adj * weight_adj)


def calc_race_ev_horse(horses, history=None):
    if history is None: history = {"horse": []}
    scored = [{"data": h, "score": calc_score_horse(h)} for h in horses]
    total  = sum(s["score"] for s in scored)

    # 推定順位を計算（人気乖離補正用）
    sorted_sc  = sorted(scored, key=lambda x: x["score"], reverse=True)
    rank_map   = {id(s): i+1 for i, s in enumerate(sorted_sc)}

    result = []
    for s in scored:
        odds        = float(s["data"].get("odds", 0))
        prob        = s["score"] / total if total > 0 else 0
        hist_adj    = calc_history_correction(history, "horse", s["data"].get("name",""))
        pop_gap_adj = calc_popularity_gap_adj(
            rank_map.get(id(s), 1),
            int(s["data"].get("popularity", rank_map.get(id(s), 1)))
        )
        ev = odds * prob * hist_adj * pop_gap_adj if odds > 0 else 0

        # Priority7: 条件別EV閾値
        tt       = "ダ" if "ダ" in s["data"].get("track_type","芝") else "芝"
        dist     = get_distance_type(int(s["data"].get("distance_m", 1600)))
        cond     = s["data"].get("track_condition","良")
        th_s, th_b = get_ev_threshold_horse(tt, dist, cond)
        judge    = "強買い" if ev > th_s else "買い" if ev > th_b else "見送り"

        result.append({
            **s["data"],
            "J":         round(calc_J(
                             float(s["data"].get("tansho_return",    1.0)),
                             float(s["data"].get("course_return",    1.0)),
                             float(s["data"].get("recent5_stability",1.0))
                         ), 4),
            "score":     round(s["score"], 6),
            "prob":      round(prob, 4),
            "ev":        round(ev,   4),
            "judge":     judge,
            "dist_type": dist,
        })
    return sorted(result, key=lambda x: x["ev"], reverse=True)


# ══════════════════════════════════════════════════
# 競輪 EV計算（Step1〜4 精度向上版）
# ══════════════════════════════════════════════════

# ── Step2: バンク種別マスタ ──────────────────────
BANK_TYPE = {
    "前橋":333, "取手":400, "松戸":400, "千葉":400,
    "川崎":400, "西武園":400, "京王閣":400, "立川":400,
    "静岡":500, "名古屋":400, "岐阜":400, "大垣":400,
    "豊橋":400, "富山":400, "福井":333, "松山":400,
    "高知":400, "小倉":400, "久留米":400, "別府":333,
    "佐世保":400, "熊本":400, "武雄":400, "玉野":400,
    "広島":400, "防府":400, "山口":400, "向日町":333,
    "和歌山":400, "岸和田":400, "奈良":400, "大津":400,
}

def get_bank_type(venue_name):
    """競輪場名からバンク種別を返す"""
    name = venue_name.replace("競輪場","").strip()
    return BANK_TYPE.get(name, 400)

# ── Step4: 脚質×バンク補正テーブル ──────────────
STYLE_BANK_ADJ = {
    ("逃", 333): 1.12, ("逃", 400): 1.05, ("逃", 500): 0.97,
    ("追", 333): 0.93, ("追", 400): 1.00, ("追", 500): 1.06,
    ("両", 333): 1.03, ("両", 400): 1.02, ("両", 500): 1.02,
    ("捲", 333): 1.08, ("捲", 400): 1.04, ("捲", 500): 0.99,
}

def normalize_style(style_str):
    """脚質文字列を逃/追/両/捲に正規化"""
    if any(k in style_str for k in ["逃","先行"]): return "逃"
    if any(k in style_str for k in ["追込","追い込み","差し"]): return "追"
    if "捲" in style_str: return "捲"
    return "両"

def calc_style_bank_adj(running_style, bank_type):
    """脚質×バンク種別補正"""
    norm  = normalize_style(running_style)
    bt    = bank_type if bank_type in [333, 400, 500] else 400
    return STYLE_BANK_ADJ.get((norm, bt), 1.0)

# ── Step1: ライン役割別スコア計算 ────────────────
def calc_role_adj(role, wins_lead, starts_lead, seconds_second, starts_second,
                  wins_total, starts_total):
    """
    役割別スコア補正
    先頭: 先頭時1着率を使用
    番手: 番手時2着率を使用
    単騎: 総合勝率を使用（単騎は基本不利なので0.92補正）
    """
    if role == "先頭":
        if starts_lead > 0:
            return wins_lead / starts_lead
        return wins_total / starts_total if starts_total > 0 else 0.15
    elif role == "番手":
        if starts_second > 0:
            return seconds_second / starts_second
        return wins_total / starts_total if starts_total > 0 else 0.15
    else:  # 単騎
        rate = wins_total / starts_total if starts_total > 0 else 0.15
        return rate * 0.92  # 単騎ペナルティ

# ── Step3: 直近4か月と通算の加重平均 ────────────
RECENT_WEIGHT = 0.65  # 直近4か月
TOTAL_WEIGHT  = 0.35  # 通算

def calc_weighted_stats(wins_r, starts_r, wins_t, starts_t):
    """直近4か月と通算の加重平均でB・Cを算出"""
    if starts_r > 0 and starts_t > 0:
        win_rate_r = wins_r / starts_r
        win_rate_t = wins_t / starts_t
        blended_rate = RECENT_WEIGHT * win_rate_r + TOTAL_WEIGHT * win_rate_t
        blended_starts = RECENT_WEIGHT * starts_r + TOTAL_WEIGHT * starts_t
        return round(blended_rate * blended_starts), round(blended_starts)
    elif starts_r > 0:
        return wins_r, starts_r
    return wins_t, starts_t

def calc_K(line_rate, bank_rate, recent_form):
    return 0.4 * line_rate + 0.3 * bank_rate + 0.3 * recent_form

def calc_frame_adj(frame_num, total_riders):
    """枠番補正: 競輪は内枠が有利"""
    if total_riders <= 0: return 1.0
    inner_bonus = (total_riders - frame_num) / total_riders
    return round(1.0 + inner_bonus * 0.06, 3)

def calc_cycle_trend_adj(wins, seconds, thirds, others):
    """着度数からトレンド補正"""
    starts = wins + seconds + thirds + others
    if starts == 0: return 1.0
    win_rate      = wins / starts
    quinella_rate = (wins + seconds) / starts
    trio_rate     = (wins + seconds + thirds) / starts
    trend = 0.5 * win_rate + 0.3 * quinella_rate + 0.2 * trio_rate
    return min(1.15, max(0.85, 0.85 + trend * 1.5))

def calc_score_cycle(rider):
    # 基本パラメータ
    C  = float(rider.get("C", 0))
    E  = float(rider.get("E", 0))
    F  = float(rider.get("F", 0))
    lr = float(rider.get("line_rate",    1.0))
    br = float(rider.get("bank_rate",    1.0))
    rf = float(rider.get("recent_form",  1.0))
    fn = float(rider.get("frame_num",    1.0))
    st = rider.get("running_style", "差し")
    bt = int(rider.get("bank_type", 400))
    rl = rider.get("role", "単騎")  # 先頭/番手/単騎

    # Step1: 役割別B・Cを使用
    wins_lead     = float(rider.get("wins_lead",    0))
    starts_lead   = float(rider.get("starts_lead",  0))
    sec_second    = float(rider.get("seconds_second",0))
    starts_second = float(rider.get("starts_second", 0))
    wins_t        = float(rider.get("wins_total",   rider.get("B", 0)))
    starts_t      = float(rider.get("starts_total", rider.get("C", 0)))

    # Step3: 直近4か月と通算の加重平均
    wins_r   = float(rider.get("wins_recent",   wins_t * 0.4))
    starts_r = float(rider.get("starts_recent", starts_t * 0.4))
    B_adj, C_adj = calc_weighted_stats(wins_r, starts_r, wins_t, starts_t)
    B_adj = float(B_adj)
    C_adj = float(C_adj)

    if C_adj <= 0 or F <= 0 or E <= 0: return 0.0

    K           = calc_K(lr, br, rf)
    stability   = C_adj / (C_adj + 3)
    market_edge = (F - E + 1) / F
    frame_adj   = calc_frame_adj(fn, F)

    # Step4: 脚質×バンク補正
    style_bank  = calc_style_bank_adj(st, bt)

    # Step1: 役割別基本勝率
    role_rate   = calc_role_adj(rl, wins_lead, starts_lead,
                                sec_second, starts_second, wins_t, starts_t)

    # 着度数トレンド補正
    w = float(rider.get("wins_chakudo",   B_adj))
    s = float(rider.get("seconds_chakudo",0))
    t = float(rider.get("thirds_chakudo", 0))
    o = float(rider.get("others_chakudo", max(C_adj - B_adj, 0)))
    trend_adj = calc_cycle_trend_adj(w, s, t, o)

    return role_rate * stability * market_edge * K * frame_adj * style_bank * trend_adj

def calc_race_ev_cycle(riders, history=None):
    if history is None: history = {"cycle": []}

    # 単騎除外オプション（単騎選手はスコア計算するが判定を下げる）
    scored = [{"data": r, "score": calc_score_cycle(r)} for r in riders]
    total  = sum(s["score"] for s in scored)
    result = []
    for s in scored:
        odds     = float(s["data"].get("odds", 0))
        prob     = s["score"] / total if total > 0 else 0
        hist_adj = calc_history_correction(history, "cycle", s["data"].get("name", ""))
        ev       = odds * prob * hist_adj if odds > 0 else 0
        role     = s["data"].get("role", "単騎")
        bt       = int(s["data"].get("bank_type", 400))

        # 条件別EV閾値（Step7の先行実装）
        if role == "先頭" and bt == 333:
            threshold_strong = 1.20
            threshold_buy    = 1.05
        elif role == "先頭":
            threshold_strong = 1.25
            threshold_buy    = 1.05
        elif role == "番手":
            threshold_strong = 1.20
            threshold_buy    = 1.00
        else:  # 単騎
            threshold_strong = 1.30
            threshold_buy    = 1.15

        judge = "強買い" if ev > threshold_strong else "買い" if ev > threshold_buy else "見送り"

        result.append({
            **s["data"],
            "K":     round(calc_K(
                         float(s["data"].get("line_rate",   1.0)),
                         float(s["data"].get("bank_rate",   1.0)),
                         float(s["data"].get("recent_form", 1.0))
                     ), 4),
            "score": round(s["score"], 6),
            "prob":  round(prob, 4),
            "ev":    round(ev,   4),
            "judge": judge,
            "role":  role,
            "bank_type": bt,
        })
    return sorted(result, key=lambda x: x["ev"], reverse=True)


# ══════════════════════════════════════════════════
# netkeiba から競馬データ取得
# ══════════════════════════════════════════════════
def fetch_horse_with_ev():
    races   = []
    history = load_history()
    print(" [horse] netkeiba.com 全レース取得開始...")
    base = "https://race.netkeiba.com"
    candidate_urls = [
        f"{base}/top/race_list.html?kaisai_date={today_ymd}",
        f"{base}/top/race_list.html?kaisai_date={today_ymd}&rf=race_submenu",
        f"{base}/race_list.html?kaisai_date={today_ymd}",
    ]
    all_ids = []
    for url in candidate_urls:
        try:
            html = fetch(url, timeout=25)
            time.sleep(2)
            ids = re.findall(r'race_id=(\d{12})', html)
            all_ids.extend(ids)
            print(f"  └ {url.split('?')[0].split('/')[-1]}: {len(ids)}件")
        except Exception as e:
            print(f"  └ エラー: {e}")
    unique_ids = sorted(list(dict.fromkeys(all_ids)))
    print(f" 本日のレースID: {len(unique_ids)}件（全件取得）")
    if not unique_ids:
        return fetch_horse_fallback()

    def _fetch_wrapper(rid):
        try:
            return fetch_race_details(base, rid, history)
        except Exception as e:
            print(f"  └ [{rid}] {e}")
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_fetch_wrapper, rid): rid for rid in unique_ids}
        for fut in concurrent.futures.as_completed(futures):
            info = fut.result()
            if info:
                races.append(info)
                time.sleep(0.5)

    if not races:
        return fetch_horse_fallback()
    print(f" 競馬: {len(races)}件取得（EV計算済み）")
    return races

def fetch_race_details(base, race_id, history):
    try:
        shutsuba_url = f"{base}/race/shutuba.html?race_id={race_id}"
        html         = fetch(shutsuba_url)
        time.sleep(1)

        # race_idから競馬場コードを取得（YYYYMMDDCCRRXX形式）
        COURSE_MAP = {
            "01":"札幌","02":"函館","03":"福島","04":"新潟","05":"東京",
            "06":"中山","07":"中京","08":"京都","09":"阪神","10":"小倉"
        }
        course_code = race_id[8:10] if len(race_id) >= 10 else "05"
        venue       = COURSE_MAP.get(course_code, "競馬場") + "競馬場"
        race_no     = int(race_id[10:12]) if len(race_id) >= 12 else 1

        # レース名・グレードはtitleタグから取得
        name_match  = re.search(r'<title>([^|<]+)', html)
        race_name   = name_match.group(1).strip() if name_match else f"レース{race_id}"

        # グレードはレース名から判定（HTMLにG1文字が多く誤検知しやすい）
        grade = ""
        for g in ["G1","G2","G3","重賞"]:
            if g in race_name:
                grade = g
                break

        # 発走時刻（netkeibaの出馬表から正確に取得）
        # パターン1: 発走時刻のクラス
        time_match = re.search(r'class="[^"]*RaceData[^"]*"[^>]*>.*?(\d{1,2}:\d{2})', html, re.DOTALL)
        if not time_match:
            # パターン2: 「発走」の直後
            time_match = re.search(r'発走.*?(\d{1,2}:\d{2})', html, re.DOTALL)
        if not time_match:
            # パターン3: HTMLの後半部分から時刻を取得（前半はメタ情報が多い）
            times_all = re.findall(r'(\d{2}:\d{2})', html[2000:])
            # 10:00〜18:00の範囲の時刻を選ぶ
            valid_times = [t for t in times_all if 10 <= int(t[:2]) <= 18]
            race_time = valid_times[0] if valid_times else "--:--"
        else:
            race_time = time_match.group(1)

        # 馬場・コース種別
        cond_match  = re.search(r'馬場\s*(良|稍重|重|不良)', html)
        type_match  = re.search(r'class="[^"]*race_type[^"]*"[^>]*>(芝|ダート|障害)', html)
        if not type_match:
            type_match = re.search(r'>(芝|ダート)<', html)
        track_condition = cond_match.group(1)  if cond_match  else "良"
        track_type      = type_match.group(1)  if type_match  else "芝"

        horse_pattern  = r'horse_id=(\d+)[^>]*>([^<]{2,20})</a>'
        horse_matches  = re.findall(horse_pattern, html)
        F              = len(horse_matches) if horse_matches else 16

        # 斤量を抽出
        weights = re.findall(r'(\d{2}\.\d)', html)

        # オッズ取得
        odds_url  = f"https://race.netkeiba.com/odds/index.html?race_id={race_id}&type=b1"
        odds_html = fetch(odds_url)
        time.sleep(1)
        odds_list  = re.findall(r'(\d+\.\d)', odds_html)
        valid_odds = [float(o) for o in odds_list if 1.0 <= float(o) <= 999.0]

        horses_data = []
        for i, (horse_id, horse_name) in enumerate(horse_matches[:18]):
            odds       = valid_odds[i] if i < len(valid_odds) else 10.0
            weight_val = float(weights[i]) if i < len(weights) else 55.0
            weight_diff= weight_val - 55.0
            hdata      = fetch_horse_stats(horse_id, horse_name, F, odds,
                                           weight_diff, track_condition, track_type)
            horses_data.append(hdata)
            time.sleep(0.8)

        if not horses_data:
            return None

        # Priority5: フィールド全体の脚質カウントと上がり3F平均を計算
        field_style_counts = {}
        last3f_list = []
        for h in horses_data:
            st = h.get("running_style","先行")
            field_style_counts[st] = field_style_counts.get(st, 0) + 1
            if h.get("avg_last3f", 0) > 0:
                last3f_list.append(h["avg_last3f"])
        field_avg_last3f = sum(last3f_list) / len(last3f_list) if last3f_list else 36.0

        for h in horses_data:
            h["field_style_counts"]  = field_style_counts
            h["field_avg_last3f"]    = field_avg_last3f

        ev_results = calc_race_ev_horse(horses_data, history)
        best       = next((h for h in ev_results if h["judge"] in ["強買い","買い"]), ev_results[0] if ev_results else None)

        return {
            "sport":     "horse",
            "name":      race_name,
            "venue":     venue,
            "time":      race_time,
            "grade":     grade,
            "url":       "keiba.html",
            "ev_detail": ev_results[:5],
            "honmei":    best["name"] if best else "",
            "ev":        f"+{int((best['ev']-1)*100)}%" if best and best['ev'] > 1 else "",
            "judge":     best["judge"] if best else "見送り",
            "reason":    f"推定勝率{int(best['prob']*100)}%・EV{best['ev']:.2f}倍・{track_condition}" if best else ""
        }
    except Exception as e:
        print(f"  [race_details/{race_id}] エラー: {e}")
        return None


def fetch_horse_stats(horse_id, horse_name, F, odds, weight_diff, track_condition, track_type):
    try:
        url  = f"https://db.netkeiba.com/horse/{horse_id}/"
        html = fetch(url)
        time.sleep(0.5)

        # ── 通算成績 ─────────────────────────────────
        total_m = re.findall(r'\d+着', html[:8000])
        wins    = len(re.findall(r'1着', html[:8000]))
        total   = max(len(total_m), wins + 3, 5)

        # ── Priority1: 直近5・10走 ────────────────────
        all_ranks   = [int(r) for r in re.findall(r'(\d+)着', html[:8000])[:20]]
        wins_r5     = sum(1 for r in all_ranks[:5]  if r == 1)
        starts_r5   = min(len(all_ranks), 5)
        wins_r10    = sum(1 for r in all_ranks[:10] if r == 1)
        starts_r10  = min(len(all_ranks), 10)

        # 上がり3F（直近5走の平均）
        last3f_m    = re.findall(r'(\d{2}\.\d)', html[:6000])
        avg_last3f  = sum(float(t) for t in last3f_m[:5]) / len(last3f_m[:5]) if last3f_m else 36.0

        # ── 平均人気 ─────────────────────────────────
        pop_m   = re.findall(r'(\d+)番人気', html[:5000])
        win_pops= [int(p) for p in pop_m[:wins] if int(p) <= 18]
        avg_pop = sum(win_pops)/len(win_pops) if win_pops else max(1, F//3)

        # ── 単勝・コース回収率 ─────────────────────────
        tansho_m = re.search(r'単勝回収率[^\d]*(\d+)', html)
        course_m = re.search(r'コース回収率[^\d]*(\d+)', html)
        tansho_r = float(tansho_m.group(1)) / 100 if tansho_m else 1.05
        course_r = float(course_m.group(1)) / 100 if course_m else 1.02

        # ── Priority3: コース・距離・馬場別成績 ──────────
        # コース別（現在の競馬場×芝/ダート）
        wins_course     = len(re.findall(r'1着', html[html.find(track_type):html.find(track_type)+2000])) if track_type in html else 0
        starts_course   = max(wins_course + 2, 5)
        # 距離別
        wins_distance   = wins_course  # 近似値（実装簡易化）
        starts_distance = starts_course
        # 馬場状態別
        wins_condition   = len(re.findall(r'1着', html[html.find(track_condition):html.find(track_condition)+1000])) if track_condition in html else 0
        starts_condition = max(wins_condition + 2, 3)

        # 距離（メートル）
        dist_m = re.search(r'(\d{4})m', html)
        distance_m = int(dist_m.group(1)) if dist_m else 1600

        # ── Priority2: 騎手情報（乗り替わり検出） ────────
        jockeys     = re.findall(r'騎手[^\n]*?([^\n<]{2,6})', html[:3000])
        prev_jockey = jockeys[0] if jockeys else ""
        curr_jockey = jockeys[1] if len(jockeys) > 1 else prev_jockey
        is_change   = prev_jockey != curr_jockey and bool(prev_jockey)

        jockey_win_m   = re.search(r'騎手勝率[^\d]*(\d+\.\d+)', html)
        jockey_rate    = float(jockey_win_m.group(1)) if jockey_win_m else 0.10
        jockey_h_wins  = round(jockey_rate * 5)
        jockey_h_starts= 5

        # ── Priority4: 調教情報 ───────────────────────
        training_m    = re.search(r'調教[^\n]*([SABCD])', html[:2000])
        training_eval = training_m.group(1) if training_m else "B"
        train_time_m  = re.search(r'(\d{1,2}\.\d)', html[:2000])
        training_time_diff = 0.0  # デフォルト（実際はタイムとの差分）

        # ── 脚質 ─────────────────────────────────────
        style_m   = re.search(r'(逃げ|先行|差し|追い込み)', html[:2000])
        run_style = style_m.group(1) if style_m else "先行"

        # ── 安定度（近5走） ───────────────────────────
        avg_r5 = sum(all_ranks[:5]) / len(all_ranks[:5]) if all_ranks else 5.0
        stab5  = min(1.2, max(0.8, (F - avg_r5 + 1) / F * 1.1))

        return {
            "name":               horse_name,
            "B":                  wins,
            "C":                  total,
            "E":                  round(avg_pop, 1),
            "F":                  F,
            "odds":               odds,
            "tansho_return":      round(tansho_r, 3),
            "course_return":      round(course_r, 3),
            "recent5_stability":  round(stab5,    3),
            # Priority1
            "wins_recent5":       wins_r5,
            "starts_recent5":     starts_r5,
            "wins_recent10":      wins_r10,
            "starts_recent10":    starts_r10,
            "avg_last3f":         round(avg_last3f, 1),
            "recent_ranks":       all_ranks,
            # Priority2
            "is_jockey_change":   is_change,
            "jockey_horse_wins":  jockey_h_wins,
            "jockey_horse_starts":jockey_h_starts,
            "jockey_recent_rate": jockey_rate,
            # Priority3
            "wins_course":        wins_course,
            "starts_course":      starts_course,
            "wins_distance":      wins_distance,
            "starts_distance":    starts_distance,
            "wins_condition":     wins_condition,
            "starts_condition":   starts_condition,
            "distance_m":         distance_m,
            # Priority4
            "training_eval":      training_eval,
            "training_time_diff": training_time_diff,
            # Priority5
            "running_style":      run_style,
            "field_style_counts": {},   # レース全体の集計は後で追加
            "field_avg_last3f":   36.0,
            # 基本
            "weight_diff":        weight_diff,
            "track_condition":    track_condition,
            "track_type":         track_type,
            "popularity":         int(avg_pop),
        }
    except Exception as e:
        print(f"    [horse_stats/{horse_id}] エラー: {e}")
        return {
            "name": horse_name, "B": 1, "C": 8,
            "E": float(F)//3, "F": F, "odds": odds,
            "tansho_return": 1.0, "course_return": 1.0, "recent5_stability": 1.0,
            "wins_recent5": 0, "starts_recent5": 3,
            "wins_recent10": 1, "starts_recent10": 8,
            "avg_last3f": 36.0, "recent_ranks": [],
            "is_jockey_change": False, "jockey_horse_wins": 1,
            "jockey_horse_starts": 5, "jockey_recent_rate": 0.1,
            "wins_course": 0, "starts_course": 3,
            "wins_distance": 0, "starts_distance": 3,
            "wins_condition": 0, "starts_condition": 3,
            "distance_m": 1600, "training_eval": "B",
            "training_time_diff": 0.0, "running_style": "先行",
            "field_style_counts": {}, "field_avg_last3f": 36.0,
            "weight_diff": weight_diff, "track_condition": track_condition,
            "track_type": track_type, "popularity": 5,
        }


def fetch_horse_fallback():
    # 多段フォールバック: JRA(thisweek) → JRA(race) → netkeiba top
    fallback_targets = [
        ("https://www.jra.go.jp", "/race/thisweek/"),
        ("https://www.jra.go.jp", "/race/"),
        ("https://race.netkeiba.com", "/top/"),
    ]
    for base_fb, path_fb in fallback_targets:
        try:
            html = fetch(base_fb + path_fb, timeout=15)
            time.sleep(2)
            ids = re.findall(r'race_id=(\d{12})', html)
            if ids:
                print(f" [horse/fallback] {base_fb}: レースID {len(ids)}件検出")
                history = load_history()
                result  = []
                for rid in list(dict.fromkeys(ids))[:8]:
                    try:
                        info = fetch_race_details("https://race.netkeiba.com", rid, history)
                        if info:
                            result.append(info)
                        time.sleep(1.5)
                    except Exception:
                        pass
                if result:
                    return result
            grades = re.findall(r'(G[123])[^\n]*?([^\n]{3,20}(?:賞|杯|ステークス|カップ|記念|特別))', html)
            venues = re.findall(r'(札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉)', html)
            times  = re.findall(r'(\d{1,2}:\d{2})', html)
            if grades and venues:
                grade, name = grades[0]
                print(f" [horse/fallback] {base_fb}: {grade} {name.strip()}")
                return [{"sport":"horse","name":name.strip(),"venue":venues[0]+"競馬場",
                         "time":times[0] if times else "--:--","grade":grade,"url":"keiba.html"}]
        except Exception as e:
            print(f"[horse/fallback] {base_fb}{path_fb}: {e}")
    return [fallback("horse")]


def fetch_boat_riders(base, jcd, rno):
    """出走表から選手データを取得"""
    try:
        # boatrace.jpの出走表URL
        url  = f"{base}/owpc/pc/race/racelist?hd={today_ymd}&jcd={jcd}&rno={rno}"
        html = fetch(url)
        time.sleep(1)

        # boatrace.jpの選手名は「is-fs18」クラスのspanタグ内にある
        # 例: <span class="is-fs18">山田太郎</span>
        names = re.findall(r'class="is-fs18[^"]*">\s*([^\s<]{2,5})\s*<', html)

        # フォールバック: tbody内のテキストから日本人名っぽいものを抽出
        if not names or names[0] in ['ライブ','リプレイ','マイページ']:
            # tbody内のデータに絞り込み
            tbody = re.search(r'<tbody>(.*?)</tbody>', html, re.DOTALL)
            if tbody:
                tbody_html = tbody.group(1)
                names = re.findall(r'([^\s<]{2,5})', tbody_html)
                # 日本語名のみ抽出（ひらがな・カタカナ・漢字）
                names = [n for n in names if re.search(r'[\u3040-\u9fff]', n) and len(n) >= 2][:6]

        # まだダメなら選手一覧ページから取得
        if not names or len(names) < 3:
            try:
                # 選手一覧の別URL
                list_url  = f"{base}/owpc/pc/race/beforeinfo?hd={today_ymd}&jcd={jcd}&rno={rno}"
                list_html = fetch(list_url)
                time.sleep(1)
                names = re.findall(r'([^\s<]{2,5})', list_html)
                names = [n for n in names if re.search(r'[\u3040-\u9fff]', n) and len(n) >= 2][:6]
            except:
                pass

        # 最終フォールバック
        if not names or len(names) < 3:
            names = [f"{i+1}号艇" for i in range(6)]

        # オッズ取得
        try:
            odds_html = fetch(f"{base}/owpc/pc/race/odds2tf?hd={today_ymd}&jcd={jcd}&rno={rno}")
            time.sleep(1)
            odds_list = [float(o) for o in re.findall(r'(\d+\.\d)', odds_html)
                         if 1.0 <= float(o) <= 999.0]
        except:
            odds_list = []

        # モーター勝率
        motor_rates = [float(m)/100 for m in re.findall(r'(\d{2}\.\d{2})', html)
                       if 0.0 < float(m)/100 <= 1.0][:6]
        field_avg_motor = sum(motor_rates)/len(motor_rates) if motor_rates else 0.33

        # コース別勝率
        course_rates = re.findall(r'(\d+\.\d+)%', html)
        valid_cr     = [float(r)/100 for r in course_rates if 0 < float(r) <= 100][:6]

        # 展示タイム
        try:
            exh_times = fetch_exhibition_times(base, jcd, rno)
        except:
            exh_times = []
        field_avg_exh = sum(exh_times)/len(exh_times) if exh_times else 0

        riders = []
        for i in range(6):
            name        = names[i].strip() if i < len(names) else f"{i+1}号艇"
            odds        = odds_list[i]   if i < len(odds_list)   else float(10 + i * 2)
            motor_rate  = motor_rates[i] if i < len(motor_rates) else 0.33
            course_rate = valid_cr[i]    if i < len(valid_cr)    else 0.33
            exh_time    = exh_times[i]   if i < len(exh_times)   else 0
            form_adj    = round(clamp(1.0 + (3.5 - 3.5) * 0.05, 0.90, 1.10), 3)

            riders.append({
                "name":                 name,
                "frame_num":            float(i + 1),
                "C":                    30,
                "E":                    float(i + 1),
                "F":                    6,
                "odds":                 odds,
                "win_rate":             round(0.42 / (i + 1), 3),
                "motor_win_rate":       motor_rate,
                "field_avg_motor":      field_avg_motor,
                "exhibition_time":      exh_time,
                "field_avg_exhibition": field_avg_exh if field_avg_exh > 0 else None,
                "course_win_rate":      course_rate,
                "form_adj":             form_adj,
            })

        print(f"  [boat/{jcd}/{rno}] 選手: {[r['name'] for r in riders]}")
        return riders

    except Exception as e:
        print(f"  [boat_riders/{jcd}/{rno}] エラー: {e}")
        return [{"name":f"{i+1}号艇","frame_num":float(i+1),"C":30,"E":float(i+1),
                 "F":6,"odds":float(10+i*2),"win_rate":round(0.42/(i+1),3),
                 "motor_win_rate":0.33,"field_avg_motor":0.33,
                 "exhibition_time":0,"field_avg_exhibition":None,
                 "course_win_rate":0.33,"form_adj":1.0} for i in range(6)]
        print(f"  [boat_riders/{jcd}/{rno}] エラー: {e}")
        return []

# ══════════════════════════════════════════════════
# 競艇 EV計算関数
# ══════════════════════════════════════════════════
def calc_boat_frame_adj(frame_num, total=6):
    if total <= 0: return 1.0
    inner = (total - frame_num) / total
    return round(clamp(1.0 + inner * 0.08, 0.92, 1.10), 3)

def calc_boat_motor_adj(motor_win_rate, field_avg_motor=0.33):
    if motor_win_rate <= 0: return 1.0
    diff = motor_win_rate - field_avg_motor
    return round(clamp(1.0 + diff * 1.5, 0.88, 1.15), 3)

def calc_boat_exhibition_adj(exh_time, field_avg=None):
    try:
        ex = float(exh_time)
        if ex <= 0: return 1.0
        avg  = float(field_avg) if field_avg else ex
        diff = avg - ex
        return round(clamp(1.0 + diff * 0.35, 0.88, 1.12), 3)
    except:
        return 1.0

def calc_boat_course_adj(course_num, course_win_rate, field_avg=0.33):
    if course_win_rate <= 0: return 1.0
    diff = course_win_rate - field_avg
    return round(clamp(1.0 + diff * 1.2, 0.88, 1.15), 3)

def calc_boat_rider_score(rider):
    """競艇選手スコア計算（競艇専用）"""
    frame_num   = float(rider.get("frame_num", 1) or 1)
    win_rate    = float(rider.get("win_rate",  0.07) or 0.07)
    motor_rate  = float(rider.get("motor_win_rate",  0.33) or 0.33)
    course_rate = float(rider.get("course_win_rate", 0.33) or 0.33)
    form_adj    = float(rider.get("form_adj", 1.0) or 1.0)
    exh_time    = rider.get("exhibition_time", 0)
    field_avg   = rider.get("field_avg_exhibition", None)

    # 枠番補正（1号艇が最も有利）
    frame_adj   = calc_boat_frame_adj(frame_num, 6)

    # モーター補正
    field_avg_motor = float(rider.get("field_avg_motor", 0.33) or 0.33)
    motor_adj   = calc_boat_motor_adj(motor_rate, field_avg_motor)

    # 展示タイム補正
    exh_adj     = calc_boat_exhibition_adj(exh_time, field_avg)

    # コース適性補正
    course_adj  = calc_boat_course_adj(frame_num, course_rate)

    # 基本スコア = 全国勝率 × 各補正値
    # win_rateは0〜1の小数（例：0.07 = 7%）
    score = win_rate * frame_adj * motor_adj * exh_adj * course_adj * form_adj

    return max(0.0, score)

def calc_boat_race_ev(riders):
    scored = [{"data": r, "score": calc_boat_rider_score(r)} for r in riders]
    total  = sum(s["score"] for s in scored)
    result = []
    for s in scored:
        odds  = float(s["data"].get("odds", 0) or 0)
        prob  = s["score"] / total if total > 0 else 0
        ev    = odds * prob if odds > 0 else 0
        judge = "強買い" if ev > 1.25 else "買い" if ev > 1.0 else "見送り"
        result.append({
            **s["data"],
            "score": round(s["score"], 6),
            "prob":  round(prob, 4),
            "ev":    round(ev,   4),
            "judge": judge
        })
    return sorted(result, key=lambda x: x["ev"], reverse=True)

def fetch_exhibition_times(base, jcd, rno):
    try:
        url  = f"{base}/owpc/pc/race/beforeinfo?hd={today_ymd}&jcd={jcd}&rno={rno}"
        html = fetch(url)
        time.sleep(1)
        times = re.findall(r'(\d\.\d{2})', html)
        return [float(t) for t in times if 6.0 <= float(t) <= 8.0][:6]
    except:
        return []

# ── 競艇場コード ──────────────────────────────────
BOAT_CODES = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05",
    "浜名湖":"06","蒲郡":"07","常滑":"08","津":"09","三国":"10",
    "琵琶湖":"11","住之江":"12","尼崎":"13","鳴門":"14","丸亀":"15",
    "児島":"16","宮島":"17","徳山":"18","下関":"19","若松":"20",
    "芦屋":"21","福岡":"22","唐津":"23","大村":"24"
}

# ── メイン取得関数 ────────────────────────────────
def fetch_boat_all():
    races   = []
    history = load_history()
    try:
        base     = "https://www.boatrace.jp"
        year     = today.year
        api_base = "https://boatraceopenapi.github.io"

        # ① Boatrace Open APIから出走表を取得（v2）
        programs = []
        try:
            prog_url = f"{api_base}/programs/v2/{year}/{today_ymd}.json"
            prog_html= fetch(prog_url)
            prog_json= json.loads(prog_html)
            programs = prog_json.get("programs", [])
            print(f"  [boat] Open API 出走表: {len(programs)}レース取得")
        except Exception as e:
            print(f"  [boat] Open API エラー: {e}")

        # ② 直前情報（展示タイム）を取得（v2・本日）
        previews = {}
        try:
            prev_url  = f"{api_base}/previews/v2/today.json"
            prev_html = fetch(prev_url)
            prev_json = json.loads(prev_html)
            for p in prev_json.get("previews", []):
                key = f"{p.get('race_stadium_number','')}-{p.get('race_number','')}"
                previews[key] = p
            print(f"  [boat] Open API 直前情報: {len(previews)}レース取得")
        except Exception as e:
            print(f"  [boat] 直前情報エラー: {e}")

        # ③ 開催場ごとにメインレースを選んでEV計算
        if programs:
            # 場コード→グレード・レース情報をまとめる
            stadium_map = {}
            for prog in programs:
                sid = str(prog.get("race_stadium_number","")).zfill(2)
                if sid not in stadium_map:
                    stadium_map[sid] = []
                stadium_map[sid].append(prog)

            # 場コード→名前マップ
            CODE_TO_NAME = {v:k for k,v in BOAT_CODES.items()}

            for sid, progs in stadium_map.items():
                # メインレース（最終R）を選択
                main_prog = sorted(progs, key=lambda x: x.get("race_number",0))[-1]
                venue     = CODE_TO_NAME.get(sid, f"場{sid}")
                grade_num = main_prog.get("race_grade_number", 0)
                grade     = {1:"SG",2:"G1",3:"G2",4:"G3"}.get(grade_num, "")
                rno       = main_prog.get("race_number", 12)

                # 発走時刻（race_closed_atから推定）
                t = "--:--"
                try:
                    closed = main_prog.get("race_closed_at","")
                    if closed and "T" in closed:
                        t = closed.split("T")[1][:5]
                except:
                    pass

                # 選手データ取得（正確なフィールド名）
                riders_data = main_prog.get("boats", [])

                # 直前情報（展示タイム）
                prev_key     = f"{int(sid)}-{rno}"
                prev_data    = previews.get(prev_key, {})
                exh_entries  = prev_data.get("exhibition_time_entries", [])
                exh_map      = {e.get("boat_number"):e.get("exhibition_time",0) for e in exh_entries}
                field_avg_exh= sum(exh_map.values())/len(exh_map) if exh_map else 0

                riders = []

                # デフォルトオッズ（艇番別の統計的平均値）
                DEFAULT_ODDS = {1: 3.5, 2: 5.0, 3: 7.0, 4: 10.0, 5: 14.0, 6: 18.0}

                for entry in riders_data[:6]:
                    bn         = entry.get("racer_boat_number", len(riders)+1)
                    name       = entry.get("racer_name", f"{bn}号艇")
                    win_rate   = float(entry.get("racer_national_top_1_percent", 7.0) or 7.0) / 100
                    motor_rate = float(entry.get("racer_assigned_motor_top_2_percent", 33.0) or 33.0) / 100
                    exh_time   = float(exh_map.get(bn, 0) or 0)
                    course_rate= float(entry.get("racer_national_top_2_percent", 33.0) or 33.0) / 100
                    local_rate = float(entry.get("racer_local_top_1_percent", 7.0) or 7.0) / 100
                    form_adj   = round(clamp(local_rate / win_rate if win_rate > 0 else 1.0, 0.85, 1.15), 3)

                    riders.append({
                        "name":                 name,
                        "frame_num":            float(bn),
                        "C":                    30,
                        "E":                    float(bn),
                        "F":                    6,
                        "odds":                 DEFAULT_ODDS.get(bn, 10.0),
                        "win_rate":             win_rate,
                        "motor_win_rate":       motor_rate,
                        "field_avg_motor":      0.33,
                        "exhibition_time":      exh_time,
                        "field_avg_exhibition": field_avg_exh if field_avg_exh > 0 else None,
                        "course_win_rate":      course_rate,
                        "form_adj":             form_adj,
                    })

                # オッズを別途取得
                try:
                    odds_html = fetch(f"{base}/owpc/pc/race/odds2tf?hd={today_ymd}&jcd={sid}&rno={rno}")
                    time.sleep(0.5)
                    odds_list = [float(o) for o in re.findall(r'(\d+\.\d)', odds_html)
                                 if 1.0 <= float(o) <= 999.0][:6]
                    for i, r in enumerate(riders):
                        if i < len(odds_list):
                            r["odds"] = odds_list[i]
                except:
                    pass

                ev_results = calc_boat_race_ev(riders) if riders else []
                best       = next((r for r in ev_results if r["judge"] in ["強買い","買い"]),
                                  ev_results[0] if ev_results else None)

                race = {
                    "sport": "boat",
                    "name":  f"{venue} {rno}R",
                    "venue": venue,
                    "time":  t,
                    "grade": grade,
                    "url":   "kyotei.html"
                }
                if best:
                    race.update({
                        "honmei":    best.get("name",""),
                        "ev":        f"+{int((best['ev']-1)*100)}%" if best['ev']>1 else "",
                        "judge":     best["judge"],
                        "reason":    f"推定勝率{int(best['prob']*100)}%・EV{best['ev']:.2f}倍・{int(best.get('frame_num',1))}号艇",
                        "ev_detail": ev_results[:6]
                    })
                races.append(race)

        # Open APIが使えない場合はboatrace.jpから直接取得
        if not races:
            print("  [boat] Open API未取得 → boatrace.jpから取得")
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
                except:
                    pass
                races.append({"sport":"boat","name":f"{venue} 注目レース","venue":venue,
                              "time":t,"grade":grade,"url":"kyotei.html"})

    except Exception as e:
        print(f"[boat] エラー: {e}")

    print(f"  競艇: {len(races)}件取得（EV計算済み）")
    return races


# ══════════════════════════════════════════════════
# 競輪: keirin.jp 全開催場取得 + EV計算
# ══════════════════════════════════════════════════
def fetch_cycle_all():
    races   = []
    history = load_history()
    try:
        # Kドリームスから本日の開催情報を取得
        base     = "https://keirin.kdreams.jp"
        date_url = f"{base}/racecard/{today.year}/{str(today.month).zfill(2)}/{str(today.day).zfill(2)}/"
        html     = fetch(date_url)
        time.sleep(2)

        if not html:
            return [fallback("cycle")]

        # 開催場のURLを抽出（形式: /aomori/racecard/12202604120100/）
        venue_urls = re.findall(r'href="(/([a-z]+)/racecard/\d+/)"', html)

        # 重複除去・場名抽出
        seen_venues = set()
        venue_list  = []
        for full_url, slug in venue_urls:
            if slug not in seen_venues and slug not in ["racecard","kaisai","gamboo","keirin"]:
                seen_venues.add(slug)
                venue_list.append((slug, full_url))

        print(f"  [cycle] 開催場候補: {len(venue_list)}件 {[s for s,_ in venue_list[:5]]}")

        # 場スラッグ→日本語名マップ
        SLUG_TO_NAME = {
            "maebashi":"前橋","toride":"取手","matsudo":"松戸","chiba":"千葉",
            "kawasaki":"川崎","seibu":"西武園","keio":"京王閣","tachikawa":"立川",
            "shizuoka":"静岡","nagoya":"名古屋","gifu":"岐阜","ogaki":"大垣",
            "toyohashi":"豊橋","toyama":"富山","fukui":"福井","matsuyama":"松山",
            "kochi":"高知","kokura":"小倉","kurume":"久留米","beppu":"別府",
            "sasebo":"佐世保","kumamoto":"熊本","takeo":"武雄","tamano":"玉野",
            "hiroshima":"広島","hofu":"防府","yamaguchi":"山口","mukomachi":"向日町",
            "wakayama":"和歌山","岸和田":"岸和田","nara":"奈良","otsu":"大津",
            "utsunomiya":"宇都宮","takasaki":"高崎","omiya":"大宮","sendai":"仙台",
            "aomori":"青森","hakodate":"函館","obihiro":"帯広",
        }

        grades_map = {"GP":"GP","G1":"G1","G2":"G2","G3":"G3","FI":"FI","FII":"FII"}

        for slug, venue_url in venue_list[:15]:
            venue_name = SLUG_TO_NAME.get(slug, slug) + "競輪場"
            try:
                # 開催ページから最終レース情報を取得
                v_html = fetch(base + venue_url)
                time.sleep(1)

                # グレード取得
                grade_m = re.search(r'(GP|G[123I]|FI|FII)', v_html)
                grade   = grade_m.group(1) if grade_m else ""

                # 発走時刻取得（10:00〜21:00の範囲）
                times_m  = re.findall(r'(\d{1,2}:\d{2})', v_html)
                valid_ts = [t for t in times_m if 8 <= int(t.split(":")[0]) <= 21]
                t        = valid_ts[-1] if valid_ts else "--:--"

                # レース詳細URLを取得
                # 形式1: /aomori/racedetail/1220260412XXXX/
                detail_urls = re.findall(r'href="(/[a-z]+/racedetail/\d+/)"', v_html)
                # 形式2: /gamboo/keirin-kaisai/race-card/result/XXXX/
                if not detail_urls:
                    detail_urls = re.findall(r'href="(/gamboo/keirin-kaisai/race-card/[^"]+)"', v_html)

                riders = []
                if detail_urls:
                    detail_url = base + detail_urls[-1]
                    riders     = fetch_cycle_riders_kdreams(detail_url, venue_name)
                    print(f"  [cycle/{slug}] {len(riders)}選手取得")

                ev_results = calc_race_ev_cycle(riders, history) if riders else []
                best       = next((r for r in ev_results if r["judge"] in ["強買い","買い"]),
                                  ev_results[0] if ev_results else None)

                race = {"sport":"cycle","name":f"{venue_name} 注目レース","venue":venue_name,
                        "time":t,"grade":grade,"url":"keirin.html"}
                if best:
                    race.update({
                        "honmei": best.get("name",""),
                        "ev":     f"+{int((best['ev']-1)*100)}%" if best['ev']>1 else "",
                        "judge":  best["judge"],
                        "reason": f"推定勝率{int(best['prob']*100)}%・EV{best['ev']:.2f}倍"
                    })
                races.append(race)

            except Exception as e:
                print(f"  [cycle/{slug}] エラー: {e}")
                races.append({"sport":"cycle","name":f"{venue_name} 注目レース",
                              "venue":venue_name,"time":"--:--","grade":"","url":"keirin.html"})

        if not races:
            races.append(fallback("cycle"))

    except Exception as e:
        print(f"[cycle] エラー: {e}")
        races.append(fallback("cycle"))

    print(f"  競輪: {len(races)}件取得")
    return races


def fetch_cycle_riders_kdreams(detail_url, venue_name):
    """Kドリームスのレース詳細ページから選手データを取得"""
    try:
        html     = fetch(detail_url)
        time.sleep(1)
        bank_type = get_bank_type(venue_name.replace("競輪場",""))

        # 選手名を抽出（Kドリームスの出走表テーブル）
        # 形式: 車番・選手名・都道府県/年齢/期別・級班・脚質...
        names   = re.findall(r'<td[^>]*>([^\s<]{2,5}　[^\s<]{1,5})</td>', html)
        if not names:
            names = re.findall(r'(\S{2,4}\s+\S{1,4})(?:\s+[^\s]+\s+S[12]|A[123])', html)
        if not names:
            # 着度数パターンで選手名取得
            names = re.findall(r'([^\d\s<>]{2,5})\s*\d+-\d+-\d+-\d+', html)

        # 着度数（1着-2着-3着-着外）
        chakudo_list = re.findall(r'(\d+)-(\d+)-(\d+)-(\d+)', html)

        # オッズ
        odds_list = [float(o) for o in re.findall(r'(\d+\.\d)', html)
                     if 1.5 <= float(o) <= 99.9][:9]

        # 脚質
        styles = re.findall(r'(逃げ|捲り|差し|追込|自在|マーク)', html)

        # ライン情報
        line_groups = extract_line_groups(html)
        roles       = determine_roles(names, line_groups)

        F       = max(len(names), 7) if names else 9
        riders  = []
        for i in range(min(len(names), 9)):
            name  = names[i].strip() if i < len(names) else f"{i+1}番"
            odds  = odds_list[i] if i < len(odds_list) else float(5 + i)
            style = styles[i]    if i < len(styles)    else "差し"
            role  = roles.get(i, "単騎")

            # 着度数からEV計算パラメータを算出
            if i < len(chakudo_list):
                w = int(chakudo_list[i][0])
                s = int(chakudo_list[i][1])
                t = int(chakudo_list[i][2])
                o = int(chakudo_list[i][3])
            else:
                w, s, t, o = 3, 4, 4, 9

            starts     = w + s + t + o or 20
            win_rate   = w / starts
            avg_pop    = max(1.0, float(i + 1))
            line_rate  = calc_line_rate(name, i, line_groups, names)

            riders.append({
                "name":            name,
                "B":               w, "C": starts,
                "wins_total":      w, "starts_total": starts,
                "wins_recent":     round(w * 0.4), "starts_recent": round(starts * 0.4),
                "role":            role,
                "wins_lead":       round(w * 0.6) if role=="先頭" else 0,
                "starts_lead":     round(starts * 0.5) if role=="先頭" else 0,
                "seconds_second":  round(s * 0.6) if role=="番手" else 0,
                "starts_second":   round(starts * 0.3) if role=="番手" else 0,
                "E":               avg_pop, "F": F,
                "odds":            odds,
                "line_rate":       line_rate,
                "bank_rate":       1.0,
                "recent_form":     round(min(1.2, max(0.8, win_rate / 0.15)), 3),
                "frame_num":       float(i + 1),
                "running_style":   style,
                "bank_type":       bank_type,
                "wins_chakudo":    float(w), "seconds_chakudo": float(s),
                "thirds_chakudo":  float(t), "others_chakudo":  float(o),
            })

        print(f"  [cycle/{venue_name}] 選手: {[r['name'] for r in riders[:3]]}...")
        return riders

    except Exception as e:
        print(f"  [cycle_kdreams] エラー: {e}")
        return []

        for i, venue in enumerate(venues):
            venue = venue.strip()
            if venue in seen: continue
            seen.add(venue)
            grade = grades[i] if i < len(grades) else ""
            t     = times[i]  if i < len(times)  else "--:--"

            riders     = fetch_cycle_riders(base, venue, grade, history)
            ev_results = calc_race_ev_cycle(riders, history) if riders else []
            best       = next((r for r in ev_results if r["judge"] in ["強買い","買い"]),
                              ev_results[0] if ev_results else None)

            race = {"sport":"cycle","name":f"{venue} 注目レース","venue":venue,
                    "time":t,"grade":grade,"url":"keirin.html"}
            if best:
                race.update({
                    "honmei": best.get("name",""),
                    "ev":     f"+{int((best['ev']-1)*100)}%" if best['ev'] > 1 else "",
                    "judge":  best["judge"],
                    "reason": f"推定勝率{int(best['prob']*100)}%・EV{best['ev']:.2f}倍・枠{int(best.get('frame_num',1))}番"
                })
            races.append(race)

        if not races:
            races.append(fallback("cycle"))

    except Exception as e:
        print(f"[cycle] エラー: {e}")
        races.append(fallback("cycle"))

    print(f"  競輪: {len(races)}件取得")
    return races


def fetch_cycle_riders(base, venue, grade, history):
    try:
        html = fetch(base + "/pc/racetop.do")
        time.sleep(1)
        venue_name  = venue.replace("競輪場","").strip()
        bank_type   = get_bank_type(venue_name)

        race_urls   = re.findall(r'href="(/pc/[^"]*shutsubahtml[^"]*)"', html)
        if not race_urls:
            race_urls = re.findall(r'href="(/pc/[^"]*race[^"]*)"', html)
        target_urls = [u for u in race_urls if venue_name in u or venue_name[:2] in u]
        if not target_urls:
            target_urls = race_urls[:3]

        riders = []
        for race_url in target_urls[:1]:
            try:
                race_html  = fetch(base + race_url)
                time.sleep(1)

                player_ids = re.findall(r'player_id=(\d+)', race_html)
                names      = re.findall(r'<td[^>]*class="[^"]*name[^"]*"[^>]*>([^<]{2,8})</td>', race_html)
                if not names:
                    names  = re.findall(r'(\S{2,5})\s*(?:S1|S2|A1|A2|A3)', race_html)

                odds_list  = re.findall(r'(\d+\.\d)', race_html)
                valid_odds = [float(o) for o in odds_list if 1.0 <= float(o) <= 999.0]
                line_groups= extract_line_groups(race_html)
                styles     = re.findall(r'(逃げ|捲り|差し|追込|マーク)', race_html)
                F          = max(len(names), 7) if names else 9

                # ライン役割を判定
                roles = determine_roles(names, line_groups)

                for i, name in enumerate(names[:9]):
                    pid       = player_ids[i] if i < len(player_ids) else None
                    odds      = valid_odds[i]  if i < len(valid_odds) else float(i * 3 + 2)
                    style     = styles[i] if i < len(styles) else "差し"
                    role      = roles.get(i, "単騎")
                    stats     = fetch_rider_stats(base, pid, venue_name) if pid else {}
                    line_rate = calc_line_rate(name, i, line_groups, names)

                    # Step3: 直近4か月の成績
                    wins_r   = stats.get("wins_recent",   stats.get("wins",    3) * 0.4)
                    starts_r = stats.get("starts_recent", stats.get("total",  20) * 0.4)

                    riders.append({
                        "name":            name.strip(),
                        # 通算
                        "B":               stats.get("wins",    3),
                        "C":               stats.get("total",  20),
                        "wins_total":      stats.get("wins",    3),
                        "starts_total":    stats.get("total",  20),
                        # 直近4か月（Step3）
                        "wins_recent":     wins_r,
                        "starts_recent":   starts_r,
                        # 役割別成績（Step1）
                        "role":            role,
                        "wins_lead":       stats.get("wins_lead",    0),
                        "starts_lead":     stats.get("starts_lead",  0),
                        "seconds_second":  stats.get("seconds_second",0),
                        "starts_second":   stats.get("starts_second", 0),
                        # 基本
                        "E":               stats.get("avg_pop", float(i + 1)),
                        "F":               F,
                        "odds":            odds,
                        "line_rate":       line_rate,
                        "bank_rate":       stats.get("bank_rate",   1.0),
                        "recent_form":     stats.get("recent_form", 1.0),
                        "frame_num":       float(i + 1),
                        "running_style":   style,
                        "bank_type":       bank_type,  # Step2
                        # 着度数
                        "wins_chakudo":    float(stats.get("wins",    3)),
                        "seconds_chakudo": float(stats.get("seconds", 4)),
                        "thirds_chakudo":  float(stats.get("thirds",  4)),
                        "others_chakudo":  float(stats.get("others",  9)),
                    })

                if riders: break

            except Exception as e:
                print(f"  [cycle_race/{race_url}] エラー: {e}")

        return riders

    except Exception as e:
        print(f"  [cycle_riders/{venue}] エラー: {e}")
        return []


def determine_roles(names, line_groups):
    """各選手の役割（先頭/番手/単騎）を判定"""
    roles = {}
    assigned = set()
    for group in line_groups:
        for pos, bike_num in enumerate(group):
            idx = bike_num - 1
            if 0 <= idx < len(names):
                if pos == 0:
                    roles[idx] = "先頭"
                else:
                    roles[idx] = "番手"
                assigned.add(idx)
    # 未アサインは単騎
    for i in range(len(names)):
        if i not in assigned:
            roles[i] = "単騎"
    return roles


def fetch_rider_stats(base, player_id, venue_name):
    try:
        url  = f"{base}/pc/rider/RiderTop.do?rider_id={player_id}"
        html = fetch(url)
        time.sleep(0.5)

        # ── 通算着度数 ───────────────────────────────
        chakudo = re.findall(r'(\d+)-(\d+)-(\d+)-(\d+)', html)
        if chakudo:
            wins    = int(chakudo[0][0])
            seconds = int(chakudo[0][1])
            thirds  = int(chakudo[0][2])
            others  = int(chakudo[0][3])
        else:
            w = re.search(r'1着[^\d]*(\d+)', html)
            s = re.search(r'2着[^\d]*(\d+)', html)
            t = re.search(r'3着[^\d]*(\d+)', html)
            o = re.search(r'着外[^\d]*(\d+)', html)
            wins    = int(w.group(1)) if w else 3
            seconds = int(s.group(1)) if s else 4
            thirds  = int(t.group(1)) if t else 4
            others  = int(o.group(1)) if o else 9

        starts = wins + seconds + thirds + others
        if starts == 0:
            starts = 20; wins = 3

        win_rate      = (wins / starts) * 100
        quinella_rate = ((wins + seconds) / starts) * 100
        trio_rate     = ((wins + seconds + thirds) / starts) * 100

        # ── Step3: 直近4か月の着度数 ─────────────────
        # keirin.jpの「直近4か月成績」セクションを探す
        recent_section = extract_recent_section(html)
        wins_r = seconds_r = thirds_r = others_r = 0
        if recent_section:
            rc = re.findall(r'(\d+)-(\d+)-(\d+)-(\d+)', recent_section)
            if rc:
                wins_r    = int(rc[0][0])
                seconds_r = int(rc[0][1])
                thirds_r  = int(rc[0][2])
                others_r  = int(rc[0][3])
        starts_r = wins_r + seconds_r + thirds_r + others_r
        if starts_r == 0:
            # フォールバック: 通算の40%を近似値として使用
            wins_r   = round(wins   * 0.4)
            starts_r = round(starts * 0.4)

        # ── Step1: 役割別成績（先頭時1着・番手時2着） ─
        # keirin.jpの「先行」「番手」成績セクションを探す
        wins_lead     = 0
        starts_lead   = 0
        seconds_second= 0
        starts_second = 0

        lead_section   = extract_role_section(html, "先行")
        second_section = extract_role_section(html, "番手")

        if lead_section:
            lc = re.findall(r'(\d+)-(\d+)-(\d+)-(\d+)', lead_section)
            if lc:
                wins_lead   = int(lc[0][0])
                starts_lead = sum(int(x) for x in lc[0])

        if second_section:
            sc = re.findall(r'(\d+)-(\d+)-(\d+)-(\d+)', second_section)
            if sc:
                seconds_second = int(sc[0][1])
                starts_second  = sum(int(x) for x in sc[0])

        # フォールバック: ライン先頭時は勝率の1.2倍、番手時は2着率で推定
        if starts_lead == 0:
            starts_lead = max(1, round(starts * 0.5))
            wins_lead   = round(wins_lead or wins * 0.6)
        if starts_second == 0:
            starts_second  = max(1, round(starts * 0.3))
            seconds_second = round(seconds * 0.5)

        # ── 平均人気 ─────────────────────────────────
        pop_m   = re.findall(r'(\d+)番人気', html[:3000])
        avg_pop = sum(int(p) for p in pop_m[:5]) / len(pop_m[:5]) if pop_m else 3.0

        # ── バンク適性 ────────────────────────────────
        bank_rate    = 1.0
        bank_section = extract_bank_section(html, venue_name)
        if bank_section:
            bchaku = re.findall(r'(\d+)-(\d+)-(\d+)-(\d+)', bank_section)
            if bchaku:
                bw = int(bchaku[0][0]); bs = int(bchaku[0][1])
                bt = int(bchaku[0][2]); bo = int(bchaku[0][3])
                b_starts = bw + bs + bt + bo
                if b_starts > 0 and starts > 0:
                    b_win_rate   = bw / b_starts
                    overall_rate = wins / starts
                    if overall_rate > 0:
                        bank_rate = min(1.2, max(0.8, b_win_rate / overall_rate))

        # ── 近走安定度 ────────────────────────────────
        recent_form    = 1.0
        recent_results = re.findall(r'(\d+)着', html[:2000])[:5]
        if recent_results:
            avg_rank    = sum(int(r) for r in recent_results) / len(recent_results)
            recent_form = min(1.2, max(0.8, (9 - avg_rank + 1) / 9 * 1.1))

        print(f"    [{player_id}] 勝率:{win_rate:.1f}% 2連:{quinella_rate:.1f}% 3連:{trio_rate:.1f}% bank:{bank_rate:.2f} form:{recent_form:.2f} lead:{wins_lead}/{starts_lead} 2nd:{seconds_second}/{starts_second}")

        return {
            "wins":           wins,
            "seconds":        seconds,
            "thirds":         thirds,
            "others":         others,
            "total":          starts,
            # Step3: 直近4か月
            "wins_recent":    wins_r,
            "starts_recent":  starts_r,
            # Step1: 役割別成績
            "wins_lead":      wins_lead,
            "starts_lead":    starts_lead,
            "seconds_second": seconds_second,
            "starts_second":  starts_second,
            # 統計
            "avg_pop":        round(avg_pop,     1),
            "win_rate":       round(win_rate,     1),
            "quinella_rate":  round(quinella_rate,1),
            "trio_rate":      round(trio_rate,    1),
            "bank_rate":      round(bank_rate,    3),
            "recent_form":    round(recent_form,  3)
        }
    except Exception as e:
        print(f"    [rider_stats/{player_id}] エラー: {e}")
        return {}


def extract_recent_section(html):
    """直近4か月の成績セクションを抽出"""
    try:
        keywords = ["直近4か月", "直近4ヶ月", "4か月", "最近4"]
        for kw in keywords:
            idx = html.find(kw)
            if idx != -1:
                return html[idx:idx+300]
        return ""
    except:
        return ""


def extract_role_section(html, role_name):
    """役割別（先行/番手）の成績セクションを抽出"""
    try:
        idx = html.find(role_name)
        if idx != -1:
            return html[idx:idx+200]
        return ""
    except:
        return ""


def extract_bank_section(html, venue_name):
    try:
        idx = html.find(venue_name)
        if idx == -1: idx = html.find(venue_name[:2])
        return html[idx:idx+500] if idx != -1 else ""
    except:
        return ""

def extract_line_groups(race_html):
    try:
        lines  = re.findall(r'(\d(?:-\d)+)', race_html)
        groups = []
        for line in lines:
            group = [int(n) for n in line.split("-")]
            if 2 <= len(group) <= 4:
                groups.append(group)
        return groups
    except:
        return []

def calc_line_rate(name, bike_num, line_groups, all_names):
    try:
        if not line_groups: return 1.0
        my_line = None
        for group in line_groups:
            if (bike_num + 1) in group:
                my_line = group; break
        if not my_line: return 0.98  # 単騎
        is_lead   = (bike_num + 1) == my_line[0]
        line_size = len(my_line)
        if line_size >= 3: return 1.08 if is_lead else 1.05
        elif line_size == 2: return 1.04 if is_lead else 1.02
        return 0.98
    except:
        return 1.0


def fallback(sport):
    labels = {"horse":"競馬","boat":"競艇","cycle":"競輪"}
    urls   = {"horse":"keiba.html","boat":"kyotei.html","cycle":"keirin.html"}
    return {"sport":sport,"name":f"{labels[sport]} 本日の注目レース",
            "venue":"詳細はページ内","time":"--:--","grade":"","url":urls[sport]}


# ══════════════════════════════════════════════════
# Claude API: 予想文生成
# ══════════════════════════════════════════════════
def generate_prediction_text(races):
    """テンプレートベースの予想文生成（API不要）"""
    try:
        today_jp = f"{today.month}月{today.day}日"
        lines    = [f"🎯【{today_jp}の予想】予想の鉄則"]

        # グレードレース優先
        priority = sorted(races, key=lambda r: (
            0 if r.get("grade") in ["G1","SG","GP"] else
            1 if r.get("grade") in ["G2","G3"]      else 2
        ))[:5]

        sport_icon = {"horse":"🐴","boat":"🚤","cycle":"🚴"}
        sport_name = {"horse":"競馬","boat":"競艇","cycle":"競輪"}

        for r in priority:
            icon  = sport_icon.get(r["sport"], "🏁")
            sname = sport_name.get(r["sport"], r["sport"])
            grade = f"【{r['grade']}】" if r.get("grade") else ""
            honmei= r.get("honmei", "")
            ev    = r.get("ev", "")
            judge = r.get("judge", "")

            line = f"\n{icon}{sname} {grade}{r['venue']} {r['time']}"
            if honmei:
                line += f"\n◎ {honmei}"
            if ev:
                line += f" EV{ev}"
            if judge in ["強買い","買い"]:
                line += f"（{judge}）"
            lines.append(line)

        lines.append("\n※参考程度に。投票は自己責任でお願いします🙏")
        lines.append("詳細→ oyatojikka.online")

        line_message = "\n".join(lines)
        print(f"[テンプレート] 予想文生成完了（{len(priority)}件）")
        return races, line_message

    except Exception as e:
        print(f"[テンプレート] エラー: {e}")
        return races, ""


# ══════════════════════════════════════════════════
# FTPアップロード
# ══════════════════════════════════════════════════
def upload_ftp():
    host     = (os.environ.get("FTP_HOST","") or "").strip()
    user     = (os.environ.get("FTP_USER","") or "").strip()
    password = (os.environ.get("FTP_PASS","") or "").strip()
    remote   = (os.environ.get("FTP_REMOTE",
               "/home/c9048134/public_html/oyatojikka.online/races.json") or "").strip()

    # 改行・空白を完全除去
    host     = host.replace("\n","").replace("\r","").replace(" ","")
    user     = user.replace("\n","").replace("\r","").replace(" ","")
    password = password.replace("\n","").replace("\r","")
    remote   = remote.replace("\n","").replace("\r","").replace(" ","")

    print(f"  FTP接続先: {host}, ユーザー: {user}, パス: {remote}")

    if not all([host, user, password]):
        print("FTP環境変数が未設定。スキップします。")
        return

    try:
        with ftplib.FTP(timeout=30) as ftp:
            ftp.connect(host, 21)
            ftp.login(user, password)
            ftp.set_pasv(True)

            # ディレクトリを移動しながら作成
            dirs = remote.split("/")
            filename = dirs[-1]
            dirpath  = "/".join(dirs[:-1])

            try:
                ftp.cwd(dirpath)
            except:
                path = ""
                for d in dirs[:-1]:
                    if not d: continue
                    path += "/" + d
                    try: ftp.mkd(path)
                    except: pass
                ftp.cwd(dirpath)

            with open("races.json", "rb") as f:
                ftp.storbinary(f"STOR {filename}", f)
            print(f"✅ FTPアップロード完了: {remote}")

    except Exception as e:
        print(f"FTPエラー: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


# ══════════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"[{today_str}] 精度向上版スクリプト開始")

    # ① 全レース取得＋EV計算
    print("\n--- ① レース取得＋EV計算 ---")
    all_races = []
    all_races.extend(fetch_horse_with_ev())
    all_races.extend(fetch_boat_all())
    all_races.extend(fetch_cycle_all())
    print(f"  合計: {len(all_races)}件")

    # ② Claude API 予想文生成
    print("\n--- ② 予想文生成（Claude API） ---")
    all_races, line_message = generate_prediction_text(all_races)

    # ③ races.json生成
    print("\n--- ③ races.json生成 ---")
    output = {"date": today_str, "races": all_races, "line_message": line_message}
    with open("races.json","w",encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=True, indent=2)
    print(f"races.json生成完了（{len(all_races)}件）")

    if line_message:
        with open("line_message.txt","w",encoding="utf-8") as f:
            f.write(line_message)
        print(f"\n--- LINE配信テキスト ---\n{line_message}")

    # ④ FTPアップロード
    print("\n--- ④ FTPアップロード ---")
    upload_ftp()

    print(f"\n✅ 全処理完了（{len(all_races)}件）")
