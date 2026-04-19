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

def fetch(url, timeout=15, retries=2):
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
    ("芝", "短距離", "良"):   (1.10, 0.95),
    ("芝", "短距離", "稍重"): (1.12, 0.97),
    ("芝", "短距離", "重"):   (1.15, 1.00),
    ("芝", "マイル", "良"):   (1.10, 0.95),
    ("芝", "中距離", "良"):   (1.10, 0.95),
    ("芝", "長距離", "良"):   (1.12, 0.97),
    ("ダ", "短距離", "良"):   (1.10, 0.95),
    ("ダ", "中距離", "良"):   (1.10, 0.95),
    ("ダ", "長距離", "良"):   (1.12, 0.97),
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

        # EV異常値防止（超大穴・計算異常を除外）
        if odds > 30.0 or ev > 3.0:
            judge = "見送り"
        else:
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
            time.sleep(1)
            ids = re.findall(r'race_id=(\d{12})', html)
            all_ids.extend(ids)
            print(f"  └ {url.split('?')[0].split('/')[-1]}: {len(ids)}件")
        except Exception as e:
            print(f"  └ エラー: {e}")
    unique_ids = sorted(list(dict.fromkeys(all_ids)))
    print(f" 本日のレースID: {len(unique_ids)}件（全件取得）")
    if not unique_ids:
        return fetch_horse_fallback()

    # JRAレースのみ・週末（土日）のみに絞る
    # race_id形式: YYYYMMDDCCRRXX → CC=競馬場コード(01-10=JRA)
    jra_ids = [rid for rid in unique_ids if len(rid) >= 10 and rid[8:10] in
               ['01','02','03','04','05','06','07','08','09','10']]
    print(f" JRAレースID: {len(jra_ids)}件（地方除外）")

    # 週末（土日）のみ取得（平日はスキップ）
    dow = today.weekday()  # 0=月 ... 5=土 6=日
    if dow in [5, 6]:  # 土日
        unique_ids = jra_ids
        print(f" 週末開催: {len(unique_ids)}件取得")
    else:
        # 平日はG1・重賞のみ（レース名でフィルタ）
        # 平日でも海外G1や特別レースがある場合のため最小限だけ取得
        print(f" 平日のため競馬取得スキップ")
        return []

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

    # ── G1・重賞を優先して上位に絞り込む ──────────────────
    GRADE_PRI = {"G1":0, "G2":1, "G3":2, "重賞":2}
    def horse_sort_key(r):
        gp = GRADE_PRI.get(r.get("grade",""), 3)
        ev = float((r.get("ev","0%").replace("+","").replace("%","")) or 0)
        return (gp, -ev)

    races.sort(key=horse_sort_key)

    # G1・G2・G3があればそれだけ返す（最大10件）
    graded = [r for r in races if r.get("grade","") in ["G1","G2","G3","重賞"]]
    if graded:
        races = graded[:10]
        print(f" 競馬: {len(races)}件（G1/G2/G3優先）")
    else:
        races = races[:10]
        print(f" 競馬: {len(races)}件（上位10件）")

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
            "ev":     f"+{min(int((best['ev']-1)*100),99)}%" if best and best['ev'] > 1 else "",
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
            time.sleep(1)
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
# 競艇 EV計算関数（v2: コース統計+場別補正+実オッズ対応）
# ══════════════════════════════════════════════════

# コース別1着率（全国平均・公式長期統計）
COURSE_STATS = {
    1: {"win": 0.542, "q2": 0.733, "q3": 0.852},
    2: {"win": 0.194, "q2": 0.379, "q3": 0.567},
    3: {"win": 0.120, "q2": 0.260, "q3": 0.432},
    4: {"win": 0.079, "q2": 0.185, "q3": 0.341},
    5: {"win": 0.042, "q2": 0.115, "q3": 0.230},
    6: {"win": 0.023, "q2": 0.070, "q3": 0.162},
}

# 競艇場別1コース勝率
VENUE_1ST_RATE = {
    "桐生":0.467,"戸田":0.395,"江戸川":0.398,"平和島":0.443,"多摩川":0.517,
    "浜名湖":0.529,"蒲郡":0.558,"常滑":0.572,"津":0.559,"三国":0.533,
    "琵琶湖":0.536,"住之江":0.556,"尼崎":0.548,"鳴門":0.562,"丸亀":0.590,
    "児島":0.579,"宮島":0.560,"徳山":0.558,"下関":0.548,"若松":0.521,
    "芦屋":0.582,"福岡":0.558,"唐津":0.563,"大村":0.605,
}

DEFAULT_ODDS_MAP = {1:3.5, 2:5.0, 3:7.0, 4:10.0, 5:14.0, 6:18.0}

def get_venue_course_adj(venue_name, course_num):
    base_1st     = VENUE_1ST_RATE.get(venue_name, 0.542)
    ratio        = base_1st / 0.542
    return ratio if course_num == 1 else max(0.7, 2.0 - ratio)

def calc_boat_base_prob(course_num, win_rate_national, venue_name=""):
    cn          = clamp(int(course_num), 1, 6)
    course_base = COURSE_STATS[cn]["win"]
    venue_adj   = get_venue_course_adj(venue_name, cn)
    strength    = clamp(win_rate_national / 0.167, 0.4, 2.5)
    return round(clamp(course_base * venue_adj * strength, 0.005, 0.90), 4)

def calc_motor_adj_v2(motor_2ren_rate, field_avg_motor):
    """motor_2ren_rate: %形式（例:33.0）"""
    if motor_2ren_rate <= 0: return 1.0
    avg  = field_avg_motor if field_avg_motor > 0 else 33.0
    diff = (motor_2ren_rate - avg) / avg
    return round(clamp(1.0 + diff * 0.4, 0.85, 1.15), 3)

def calc_exhibition_adj_v2(exh_time, field_avg):
    try:
        ex  = float(exh_time)
        avg = float(field_avg) if field_avg and float(field_avg) > 0 else ex
        if ex <= 0 or avg <= 0: return 1.0
        return round(clamp(1.0 + (avg - ex) * 0.6, 0.85, 1.15), 3)
    except:
        return 1.0

def is_default_odds(odds, frame_num):
    return abs(odds - DEFAULT_ODDS_MAP.get(int(frame_num), 10.0)) < 0.01

def calc_boat_rider_score_v2(rider, venue_name=""):
    course_num    = int(float(rider.get("frame_num", 1) or 1))
    win_rate      = float(rider.get("win_rate", 0.07) or 0.07)
    motor_rate    = float(rider.get("motor_win_rate", 33.0) or 33.0)
    field_avg_m   = float(rider.get("field_avg_motor", 33.0) or 33.0)
    exh_time      = float(rider.get("exhibition_time", 0) or 0)
    field_avg_exh = rider.get("field_avg_exhibition", None)
    form_adj      = float(rider.get("form_adj", 1.0) or 1.0)

    base_prob = calc_boat_base_prob(course_num, win_rate, venue_name)
    motor_adj = calc_motor_adj_v2(motor_rate, field_avg_m)
    exh_adj   = calc_exhibition_adj_v2(exh_time, field_avg_exh) if field_avg_exh and float(field_avg_exh) > 0 and exh_time > 0 else 1.0
    form_adj  = clamp(form_adj, 0.90, 1.10)

    return round(max(0.001, base_prob * motor_adj * exh_adj * form_adj), 6)

def calc_boat_race_ev_v2(riders, venue_name=""):
    scored = [{"data": r, "score": calc_boat_rider_score_v2(r, venue_name)} for r in riders]
    total  = sum(s["score"] for s in scored)
    if total <= 0: return []

    result = []
    for s in scored:
        odds      = float(s["data"].get("odds", 0) or 0)
        prob      = s["score"] / total
        course_n  = int(float(s["data"].get("frame_num", 1)))
        real_odds = odds > 0 and not is_default_odds(odds, course_n)
        ev        = round(odds * prob, 4) if odds > 0 else 0.0

        # 判定: オッズ上限あり（大穴は除外）
        if not real_odds:
            course_exp = COURSE_STATS.get(course_n, {}).get("win", 0.1)
            judge = "買い" if prob / (course_exp + 0.001) > 1.20 else "見送り"
        elif odds > 30.0:
            judge = "見送り"  # 超大穴は除外
        elif ev > 1.30 and odds <= 20.0:
            judge = "強買い"
        elif ev > 1.05 and odds <= 25.0:
            judge = "買い"
        else:
            judge = "見送り"

        result.append({
            **s["data"],
            "score":     round(s["score"], 6),
            "prob":      round(prob, 4),
            "ev":        ev,
            "judge":     judge,
            "real_odds": real_odds,
            "course_exp":round(COURSE_STATS.get(course_n, {}).get("win", 0.1), 3),
        })

    return sorted(result, key=lambda x: x["ev"] if x.get("real_odds") else x["prob"], reverse=True)

# (後方互換関数は削除済み)

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

                # 発走時刻（race_closed_atから取得）
                # フォーマット例: "2026-04-15 20:45:00" (スペース区切り)
                t = "--:--"
                try:
                    closed = main_prog.get("race_closed_at","")
                    if closed and " " in closed:
                        t = closed.split(" ")[1][:5]
                    elif closed and "T" in closed:
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
                    motor_rate = float(entry.get("racer_assigned_motor_top_2_percent", 33.0) or 33.0)  # %形式のまま
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

                # オッズ取得（無効化中・デフォルト値を使用）
                # boatrace.jpのオッズページは取得が不安定なためスキップ
                # TODO: 安定したオッズ取得方法が確立したら有効化
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
                if best and best.get("judge") in ["強買い", "買い"]:
                    race.update({
                        "honmei":    best.get("name",""),
                        "ev":        f"+{min(int((best['ev']-1)*100), 99)}%" if best and best['ev']>1 else "",
                        "judge":     best["judge"],
                        "reason":    f"推定勝率{int(best['prob']*100)}%・EV{best['ev']:.2f}倍・{int(best.get('frame_num',1))}号艇",
                        "ev_detail": ev_results[:6]
                    })
                elif best:
                    race.update({"honmei":"","ev":"","judge":"見送り","reason":"本日は推奨レースなし","ev_detail":ev_results[:6]})
                races.append(race)

        # Open APIが使えない場合はboatrace.jpから直接取得
        if not races:
            print("  [boat] Open API未取得 → boatrace.jpから取得")
            html = fetch(base + f"/owpc/pc/race/index?hd={today_ymd}")
            time.sleep(1)
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
        # Kドリームスの開催一覧ページ（複数URLを試す）
        base     = "https://keirin.kdreams.jp"
        html     = ""
        for url_path in [
            f"/racecard/{today.year}/{str(today.month).zfill(2)}/{str(today.day).zfill(2)}/",
            f"/kaisai/{today.year}/{str(today.month).zfill(2)}/{str(today.day).zfill(2)}/",
            "/racecard/",
        ]:
            try:
                html = fetch(base + url_path)
                if html and len(html) > 1000:
                    print(f"  [cycle] URL取得成功: {url_path}")
                    break
            except Exception as e:
                print(f"  [cycle] URL失敗: {url_path} {e}")
        time.sleep(1)

        if not html:
            return [fallback("cycle")]

        # 開催場情報を抽出（場名・グレード・一覧URL・最終レースURL）
        # パターン: <p class="stadium">函館競輪</p> ... icon_grade gr2 ... href="https://keirin.kdreams.jp/hakodate/racecard/11202604150100/"
        venues_info = re.findall(
            r'<p class="stadium">([^<]+)</p>.*?icon_grade\s+(\w+).*?'
            r'href="(https://keirin\.kdreams\.jp/([a-z]+)/racecard/(\d+)/)',
            html, re.DOTALL
        )

        # 重複除去・場名抽出
        seen_venues = set()
        venue_list  = []  # (slug, venue_name_jp, grade, list_url, race_id)
        for name_jp, grade_cls, list_url, slug, race_id in venues_info:
            if slug not in seen_venues and slug not in ["racecard","kaisai","gamboo","keirin"]:
                seen_venues.add(slug)
                grade_map_cls = {"gr1":"G1","gr2":"G2","gr3":"G3","grgp":"GP","grfi":"FI","grfii":"FII"}
                grade = grade_map_cls.get(grade_cls, "")
                venue_list.append((slug, name_jp.replace("競輪",""), grade, list_url, race_id))

        # パターンマッチ失敗時のフォールバック（旧パターン）
        if not venue_list:
            old_urls = re.findall(r'href="(/([a-z]+)/racecard/\d+/)"', html)
            for full_url, slug in old_urls:
                if slug not in seen_venues and slug not in ["racecard","kaisai","gamboo","keirin"]:
                    seen_venues.add(slug)
                    venue_list.append((slug, slug, "", base + full_url, ""))

        print(f"  [cycle] 開催場候補: {len(venue_list)}件 {[s for s,_,_,_,_ in venue_list[:5]]}")

        # 場スラッグ→日本語名マップ（Kdrスラッグ完全対応）
        SLUG_TO_NAME = {
            # 北海道
            "hakodate":"函館","aomori":"青森","iwakitaira":"いわき平","obihiro":"帯広",
            # 関東
            "yahiko":"弥彦","maebashi":"前橋","toride":"取手","utsunomiya":"宇都宮",
            "omiya":"大宮","seibuen":"西武園","keiokaku":"京王閣","tachikawa":"立川",
            "matsudo":"松戸","chiba":"千葉","kawasaki":"川崎","hiratsuka":"平塚",
            "odawara":"小田原","ito":"伊東",
            # 中部
            "shizuoka":"静岡","nagoya":"名古屋","gifu":"岐阜","ogaki":"大墓",
            "toyohashi":"豊橋","toyama":"富山","matsusaka":"松阪","yokkaichi":"四日市",
            "fukui":"福井",
            # 近畿
            "nara":"奈良","mukomachi":"向日町","wakayama":"和歌山","kishiwada":"岸和田",
            # 中国・四国
            "tamano":"玉野","hiroshima":"広島","hofu":"防府",
            "takamatsu":"高松","komatsushima":"小松島","kochi":"高知","matsuyama":"松山",
            # 九州
            "kokura":"小倉","kurume":"久留米","takeo":"武雄","sasebo":"佐世保",
            "beppu":"別府","kumamoto":"熊本","yamaguchi":"山口",
            # その他
            "otsu":"大津","takasaki":"高崎","sendai":"仙台",
        }

        grades_map = {"GP":"GP","G1":"G1","G2":"G2","G3":"G3","FI":"FI","FII":"FII"}

        for slug, name_jp, grade, list_url, race_id in venue_list[:15]:
            # 日本語場名（既に抽出済み）
            venue_name = (name_jp or SLUG_TO_NAME.get(slug, slug)) + "競輪場"
            try:
                # 開催一覧ページから最終レースURLを取得
                v_html = fetch(list_url)
                time.sleep(1)

                # 発走時刻取得（最終レース時刻）
                times_m  = re.findall(r'(\d{1,2}:\d{2})', v_html)
                valid_ts = [t for t in times_m if 8 <= int(t.split(":")[0]) <= 21]
                if valid_ts:
                    t = sorted(valid_ts, key=lambda x: int(x.split(":")[0])*60+int(x.split(":")[1]))[-1]
                else:
                    t = "--:--"

                # レース詳細URLを取得（最終レース）
                # 形式: /hakodate/racedetail/1120260415010012/
                detail_urls = re.findall(
                    rf'href="(https://keirin\.kdreams\.jp/{slug}/racedetail/(\d+)/)',
                    v_html
                )
                if not detail_urls:
                    # 相対パスでも探す
                    detail_urls_rel = re.findall(rf'href="(/{slug}/racedetail/(\d+)/)', v_html)
                    detail_urls = [(base + u, rid) for u, rid in detail_urls_rel]

                riders = []
                if detail_urls:
                    # 重複除去してレース番号最大（最終レース）のURLを選択
                    unique_detail = list(dict.fromkeys(detail_urls))
                    last_race_url = max(unique_detail, key=lambda x: int(x[1][-2:]))[0]
                    riders = fetch_cycle_riders_kdreams(last_race_url, venue_name)
                    print(f"  [cycle/{slug}] {len(riders)}選手取得 (URL: {last_race_url})")

                ev_results = calc_race_ev_cycle(riders, history) if riders else []
                best       = next((r for r in ev_results if r["judge"] in ["強買い","買い"]),
                                  ev_results[0] if ev_results else None)

                race = {"sport":"cycle","name":f"{venue_name} 注目レース","venue":venue_name,
                        "time":t,"grade":grade,"url":"keirin.html"}
                if best and best.get("judge") in ["強買い", "買い"]:
                    race.update({
                        "honmei": best.get("name","予想公開中"),
                        "ev":     f"+{min(int((best['ev']-1)*100),99)}%" if best.get('ev',0)>1 else "",
                        "judge":  best["judge"],
                        "reason": f"推定勝率{int(best['prob']*100)}%・EV{best['ev']:.2f}倍"
                    })
                elif best:
                    # 強買い・買いなしの場合は見送り表示
                    race.update({
                        "honmei": "",
                        "ev":     "",
                        "judge":  "見送り",
                        "reason": "本日は推奨レースなし"
                    })
                races.append(race)

            except Exception as e:
                print(f"  [cycle/{slug}] エラー: {e}")
                import traceback; traceback.print_exc()
                races.append({"sport":"cycle","name":f"{venue_name} 注目レース",
                              "venue":venue_name,"time":"--:--","grade":grade,"url":"keirin.html"})

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
        # 形式: <td class="rider bdr_r">\n\t\t\t\t\t\t\t\t小林 泰正<br><span class="home">群　馬/31/113/S1</span>
        names = re.findall(r'<td class="rider bdr_r">\s*([^\n<]+)<br>', html)
        # 重複除去（最初の7名のみ）
        seen_n = set()
        unique_names = []
        for n in names:
            n = n.strip()
            if n and n not in seen_n:
                seen_n.add(n)
                unique_names.append(n)
        names = unique_names[:9]
        if not names:
            # フォールバック: 旧パターン
            names = re.findall(r'([^\s<]{2,5}\s[^\s<]{1,5})(?:\s+[^\s]+\s+S[12]|A[123])', html)
        if not names:
            names = re.findall(r'([^\d\s<>]{2,5})\s*\d+-\d+-\d+-\d+', html)

        # 着度数（1着-2着-3着-着外）
        chakudo_list = re.findall(r'(\d+)-(\d+)-(\d+)-(\d+)', html)

        # オッズ
        odds_list = [float(o) for o in re.findall(r'(\d+\.\d)', html)
                     if 1.5 <= float(o) <= 99.9][:9]

        # 脚質（各選手の脚質を順番に取得）
        # Kdrの出走表では選手行内に脚質情報が含まれる
        # テーブルヘッダ: 逃・捧・差・マ の各列の数値から脚質を推定
        # 各選手行: <tr class="n1 ">...<td>0</td><td class="bdr_r">2</td><td>0</td><td>4</td>...
        # 列順: 車番, 番号, 選手名, 競走得点, S, B, 逃, 捧, 差, マ
        style_rows = re.findall(r'<tr class="n(\d)[^"]*">(.*?)</tr>', html, re.DOTALL)
        # 重複除去（最初の各車番のみ）
        seen_rows = set()
        styles_by_num = {}  # 車番 -> 脚質
        for car_num, row_html in style_rows:
            if car_num in seen_rows:
                continue
            seen_rows.add(car_num)
            # 各列の数値を取得: 逃・捧・差・マ
            td_vals = re.findall(r'<td[^>]*>\s*([\d.]+)\s*</td>', row_html)
            # 選手行の構造: 車番(1), 番号(2), 選手名(3), 競走得点(4), S(5), B(6), 逃(7), 捧(8), 差(9), マ(10)
            # td_valsは数値のみなのでインデックスがずれる場合あり
            if len(td_vals) >= 6:
                # 逃・捧・差・マの値を取得（得点後の4列）
                # 得点の位置を特定（小数点を含む値）
                score_idx = next((i for i, v in enumerate(td_vals) if '.' in v), 2)
                style_vals = td_vals[score_idx+1:score_idx+5] if score_idx+4 < len(td_vals) else td_vals[-4:]
                if style_vals:
                    max_idx = max(range(len(style_vals)), key=lambda i: float(style_vals[i]) if style_vals[i].replace('.','').isdigit() else 0)
                    style_names = ["逃げ","捲り","差し","マーク"]  # 逃・捧・差・マ（競輪の脚質列）
                    styles_by_num[car_num] = style_names[max_idx] if max_idx < len(style_names) else "差し"
        # 車番順に脚質リストを構築
        styles = [styles_by_num.get(str(i+1), "差し") for i in range(9)]
        # フォールバック: テキストパターン
        if not any(styles_by_num.values()):
            styles = re.findall(r'(逃げ|捲り|差し|追込|自在|マーク)', html)
            if not styles:
                styles = ["差し"] * 9

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
    """EV値フィルタリング付きテンプレート予想文生成"""
    try:
        today_jp  = f"{today.month}月{today.day}日"
        weekday   = today.weekday()  # 0=月, 5=土, 6=日
        is_weekday= weekday < 5

        # 平日は高EVのみ・土日はやや緩める
        ev_threshold = 1.3 if is_weekday else 1.15

        lines = [f"🎯【{today_jp}の予想】予想の鉄則"]
        if is_weekday:
            lines[0] += "（平日厳選）"

        # EV値でフィルタリング
        def get_ev_num(r):
            ev_str = r.get("ev","")
            try:
                return float(ev_str.replace("%","").replace("+","")) / 100 + 1
            except:
                return 0.0

        # 「強買い」かつEV閾値以上のレースを抽出
        high_ev = [r for r in races
                   if r.get("judge") == "強買い"
                   and get_ev_num(r) >= ev_threshold
                   and r.get("honmei","")]

        # EV値の高い順にソート
        high_ev_sorted = sorted(high_ev, key=get_ev_num, reverse=True)

        # グレード優先で上位5件
        priority_grade = sorted(high_ev_sorted, key=lambda r: (
            0 if r.get("grade") in ["G1","SG","GP"] else
            1 if r.get("grade") in ["G2","G3"]      else 2
        ))[:5]

        # 高EVが少ない場合は「買い」も含める
        if len(priority_grade) < 3:
            buy_races = [r for r in races
                         if r.get("judge") in ["強買い","買い"]
                         and r.get("honmei","")
                         and r not in priority_grade]
            buy_sorted = sorted(buy_races, key=get_ev_num, reverse=True)
            priority_grade += buy_sorted[:5 - len(priority_grade)]

        # それでも少ない場合はグレードレースで補完
        if len(priority_grade) < 3:
            fallback_races = sorted(
                [r for r in races if r.get("honmei","") and r not in priority_grade],
                key=lambda r: (
                    0 if r.get("grade") in ["G1","SG","GP"] else
                    1 if r.get("grade") in ["G2","G3"] else 2
                )
            )[:5 - len(priority_grade)]
            priority_grade += fallback_races

        priority   = priority_grade[:5]
        sport_icon = {"horse":"🐴","boat":"🚤","cycle":"🚴"}
        sport_name = {"horse":"競馬","boat":"競艇","cycle":"競輪"}

        for r in priority:
            icon  = sport_icon.get(r["sport"], "🏁")
            sname = sport_name.get(r["sport"], r["sport"])
            grade = f"【{r['grade']}】" if r.get("grade") else ""
            honmei= r.get("honmei","")
            ev    = r.get("ev","")
            judge = r.get("judge","")

            line = f"\n{icon}{sname} {grade}{r['venue']} {r['time']}"
            if honmei:
                line += f"\n◎ {honmei}"
            if ev:
                line += f" EV{ev}"
            if judge in ["強買い","買い"]:
                line += f"（{judge}）"
            lines.append(line)

        ev_count = len([r for r in races if r.get("judge") == "強買い"])
        lines.append(f"\n本日の強買い候補: {ev_count}件（EV閾値{int(ev_threshold*100-100)}%以上）")
        lines.append("※参考程度に。投票は自己責任でお願いします🙏")
        lines.append("詳細→ oyatojikka.online")

        line_message = "\n".join(lines)
        print(f"[テンプレート] 予想文生成完了（高EV:{len(high_ev)}件→表示:{len(priority)}件）")
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
    _ftp_base = os.environ.get("FTP_REMOTE_BASE", "/home/c9048134/public_html/oyatojikka.online")
    remote   = (os.environ.get("FTP_REMOTE", f"{_ftp_base}/races.json") or "").strip()

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

# ══════════════════════════════════════════════════
# index.html 自動生成（base64）
# ══════════════════════════════════════════════════
INDEX_HTML_B64 = "PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImphIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KICA8bWV0YSBuYW1lPSJkZXNjcmlwdGlvbiIgY29udGVudD0i56u26aas44O756u26ImH44O756u26Lyq44Gu5pys5ZG95LqI5oOz44KS5pyf5b6F5YCk77yIRVbvvInph43oppbjgafmr47ml6XlhazplovjgILjg4fjg7zjgr/jgavln7rjgaXjgYTjgZ/moLnmi6DjgYLjgovkuojmg7PjgafplbfmnJ/lj47mlK/jg5fjg6njgrnjgpLnm67mjIfjgZfjgb7jgZnjgILnhKHmlpnjgadMSU5F6YWN5L+h44KC5Y+X44GR5Y+W44KM44G+44GZ44CCIj4KICA8bWV0YSBuYW1lPSJyb2JvdHMiIGNvbnRlbnQ9ImluZGV4LGZvbGxvdyI+CiAgPG1ldGEgcHJvcGVydHk9Im9nOnR5cGUiICAgICAgICBjb250ZW50PSJ3ZWJzaXRlIj4KICA8bWV0YSBwcm9wZXJ0eT0ib2c6dGl0bGUiICAgICAgIGNvbnRlbnQ9IuS6iOaDs+OBrumJhOWJhyB8IOertummrOODu+ertuiJh+ODu+ertui8qiDmnKzlkb3kuojmg7MiPgogIDxtZXRhIHByb3BlcnR5PSJvZzpkZXNjcmlwdGlvbiIgY29udGVudD0i56u26aas44O756u26ImH44O756u26Lyq44Gu5pys5ZG95LqI5oOz44KS5pyf5b6F5YCk77yIRVbvvInph43oppbjgafmr47ml6XlhazplovjgILjg4fjg7zjgr/jgavln7rjgaXjgYTjgZ/moLnmi6DjgYLjgovkuojmg7PjgafplbfmnJ/lj47mlK/jg5fjg6njgrnjgpLnm67mjIfjgZfjgb7jgZnjgIIiPgogIDxtZXRhIHByb3BlcnR5PSJvZzp1cmwiICAgICAgICAgY29udGVudD0iaHR0cHM6Ly9veWF0b2ppa2thLm9ubGluZS8iPgogIDxtZXRhIHByb3BlcnR5PSJvZzpzaXRlX25hbWUiICAgY29udGVudD0i5LqI5oOz44Gu6YmE5YmHIj4KICA8bWV0YSBwcm9wZXJ0eT0ib2c6aW1hZ2UiICAgICAgIGNvbnRlbnQ9Imh0dHBzOi8vb3lhdG9qaWtrYS5vbmxpbmUvYXZhdGFyLnBuZyI+CiAgPG1ldGEgcHJvcGVydHk9Im9nOmxvY2FsZSIgICAgICBjb250ZW50PSJqYV9KUCI+CiAgPG1ldGEgbmFtZT0idHdpdHRlcjpjYXJkIiAgICAgICAgY29udGVudD0ic3VtbWFyeSI+CiAgPG1ldGEgbmFtZT0idHdpdHRlcjp0aXRsZSIgICAgICAgY29udGVudD0i5LqI5oOz44Gu6YmE5YmHIHwg56u26aas44O756u26ImH44O756u26LyqIOacrOWRveS6iOaDsyI+CiAgPG1ldGEgbmFtZT0idHdpdHRlcjpkZXNjcmlwdGlvbiIgY29udGVudD0i56u26aas44O756u26ImH44O756u26Lyq44Gu5pys5ZG95LqI5oOz44KS5pyf5b6F5YCk77yIRVbvvInph43oppbjgafmr47ml6XlhazplovjgIIiPgogIDxtZXRhIG5hbWU9InR3aXR0ZXI6aW1hZ2UiICAgICAgIGNvbnRlbnQ9Imh0dHBzOi8vb3lhdG9qaWtrYS5vbmxpbmUvYXZhdGFyLnBuZyI+CiAgPG1ldGEgbmFtZT0iZGVzY3JpcHRpb24iIGNvbnRlbnQ9IuertummrOODu+ertuiJh+ODu+ertui8quOBruacrOWRveS6iOaDs+OCkuacn+W+heWApO+8iEVW77yJ6YeN6KaW44Gn5q+O5pel5YWs6ZaL44CC44OH44O844K/44Gr5Z+644Gl44GE44Gf5qC55oug44GC44KL5LqI5oOz44Gn6ZW35pyf5Y+O5pSv44OX44Op44K544KS55uu5oyH44GX44G+44GZ44CC54Sh5paZ44GnTElORemFjeS/oeOCguWPl+OBkeWPluOCjOOBvuOBmeOAgiI+CiAgPG1ldGEgbmFtZT0icm9ib3RzIiBjb250ZW50PSJpbmRleCxmb2xsb3ciPgogIDwhLS0gT0dQIC0tPgogIDxtZXRhIHByb3BlcnR5PSJvZzp0eXBlIiAgICAgICAgY29udGVudD0id2Vic2l0ZSI+CiAgPG1ldGEgcHJvcGVydHk9Im9nOnRpdGxlIiAgICAgICBjb250ZW50PSLkuojmg7Pjga7piYTliYcgfCDnq7bppqzjg7vnq7boiYfjg7vnq7bovKog5pys5ZG95LqI5oOzIj4KICA8bWV0YSBwcm9wZXJ0eT0ib2c6ZGVzY3JpcHRpb24iIGNvbnRlbnQ9IuertummrOODu+ertuiJh+ODu+ertui8quOBruacrOWRveS6iOaDs+OCkuacn+W+heWApO+8iEVW77yJ6YeN6KaW44Gn5q+O5pel5YWs6ZaL44CC44OH44O844K/44Gr5Z+644Gl44GE44Gf5qC55oug44GC44KL5LqI5oOz44Gn6ZW35pyf5Y+O5pSv44OX44Op44K544KS55uu5oyH44GX44G+44GZ44CC54Sh5paZ44GnTElORemFjeS/oeOCguWPl+OBkeWPluOCjOOBvuOBmeOAgiI+CiAgPG1ldGEgcHJvcGVydHk9Im9nOnVybCIgICAgICAgICBjb250ZW50PSJodHRwczovL295YXRvamlra2Eub25saW5lLyI+CiAgPG1ldGEgcHJvcGVydHk9Im9nOnNpdGVfbmFtZSIgICBjb250ZW50PSLkuojmg7Pjga7piYTliYciPgogIDxtZXRhIHByb3BlcnR5PSJvZzppbWFnZSIgICAgICAgY29udGVudD0iaHR0cHM6Ly9veWF0b2ppa2thLm9ubGluZS9hdmF0YXIucG5nIj4KICA8bWV0YSBwcm9wZXJ0eT0ib2c6bG9jYWxlIiAgICAgIGNvbnRlbnQ9ImphX0pQIj4KICA8IS0tIFR3aXR0ZXIgQ2FyZCAtLT4KICA8bWV0YSBuYW1lPSJ0d2l0dGVyOmNhcmQiICAgICAgICBjb250ZW50PSJzdW1tYXJ5Ij4KICA8bWV0YSBuYW1lPSJ0d2l0dGVyOnRpdGxlIiAgICAgICBjb250ZW50PSLkuojmg7Pjga7piYTliYcgfCDnq7bppqzjg7vnq7boiYfjg7vnq7bovKog5pys5ZG95LqI5oOzIj4KICA8bWV0YSBuYW1lPSJ0d2l0dGVyOmRlc2NyaXB0aW9uIiBjb250ZW50PSLnq7bppqzjg7vnq7boiYfjg7vnq7bovKrjga7mnKzlkb3kuojmg7PjgpLmnJ/lvoXlgKTvvIhFVu+8iemHjeimluOBp+avjuaXpeWFrOmWi+OAguODh+ODvOOCv+OBq+WfuuOBpeOBhOOBn+agueaLoOOBguOCi+S6iOaDs+OBp+mVt+acn+WPjuaUr+ODl+ODqeOCueOCkuebruaMh+OBl+OBvuOBmeOAgueEoeaWmeOBp0xJTkXphY3kv6HjgoLlj5fjgZHlj5bjgozjgb7jgZnjgIIiPgogIDxtZXRhIG5hbWU9InR3aXR0ZXI6aW1hZ2UiICAgICAgIGNvbnRlbnQ9Imh0dHBzOi8vb3lhdG9qaWtrYS5vbmxpbmUvYXZhdGFyLnBuZyI+CjxtZXRhIG5hbWU9InZpZXdwb3J0IiBjb250ZW50PSJ3aWR0aD1kZXZpY2Utd2lkdGgsIGluaXRpYWwtc2NhbGU9MS4wIj4KPHRpdGxlPuS6iOaDs+OBrumJhOWJhyB8IOertummrOODu+ertuiJh+ODu+ertui8qiDmnKzlkb3kuojmg7M8L3RpdGxlPgo8bGluayByZWw9InByZWNvbm5lY3QiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ29vZ2xlYXBpcy5jb20iPgo8bGluayByZWw9InByZWNvbm5lY3QiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ3N0YXRpYy5jb20iIGNyb3Nzb3JpZ2luPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PU5vdG8rU2VyaWYrSlA6d2dodEA3MDA7OTAwJmZhbWlseT1CZWJhcytOZXVlJmZhbWlseT1Ob3RvK1NhbnMrSlA6d2dodEAzMDA7NDAwOzcwMCZkaXNwbGF5PXN3YXAiIHJlbD0ic3R5bGVzaGVldCI+CjxzdHlsZT4KOnJvb3QgewogIC0tYmc6ICAgICAgICAjMDgwYzE0OwogIC0tYmctY2FyZDogICAjMGMxNTI1OwogIC0tYmctY2FyZDI6ICAjMGYxZTM2OwogIC0tbmF2eTogICAgICAjMGExNjI4OwogIC0tZ29sZDogICAgICAjYzlhODRjOwogIC0tZ29sZC1sOiAgICAjZjBkMDgwOwogIC0tZ29sZC1kaW06ICByZ2JhKDIwMSwxNjgsNzYsMC4xNSk7CiAgLS1ib3JkZXI6ICAgIHJnYmEoMjAxLDE2OCw3NiwwLjE4KTsKICAtLWhvcnNlOiAgICAgI2M5YTg0YzsgICAvKiDph5EgKi8KICAtLWJvYXQ6ICAgICAgIzAwYjRkODsgICAvKiDpnZIgKi8KICAtLWN5Y2xlOiAgICAgI2U4MzEzYTsgICAvKiDotaQgKi8KICAtLXRleHQ6ICAgICAgI2VlZjBmNTsKICAtLXRleHQtZGltOiAgI2E4YjBjNDsKICAtLXRleHQtbXV0ZTogIzVhNjI3ODsKfQoqLCo6OmJlZm9yZSwqOjphZnRlcntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowfQphe2NvbG9yOmluaGVyaXQ7dGV4dC1kZWNvcmF0aW9uOm5vbmV9Cmh0bWx7c2Nyb2xsLWJlaGF2aW9yOnNtb290aH0KYm9keXtiYWNrZ3JvdW5kOnZhcigtLWJnKTtjb2xvcjp2YXIoLS10ZXh0KTtmb250LWZhbWlseTonTm90byBTYW5zIEpQJyxzYW5zLXNlcmlmO2ZvbnQtd2VpZ2h0OjMwMDttaW4taGVpZ2h0OjEwMHZofQo6Oi13ZWJraXQtc2Nyb2xsYmFye3dpZHRoOjNweH0KOjotd2Via2l0LXNjcm9sbGJhci10cmFja3tiYWNrZ3JvdW5kOnZhcigtLWJnKX0KOjotd2Via2l0LXNjcm9sbGJhci10aHVtYntiYWNrZ3JvdW5kOnZhcigtLWdvbGQpO2JvcmRlci1yYWRpdXM6MnB4fQoKLyog4pWQ4pWQIEhFQURFUiDilZDilZAgKi8KaGVhZGVyewogIHBvc2l0aW9uOnN0aWNreTt0b3A6MDt6LWluZGV4OjIwMDsKICBiYWNrZ3JvdW5kOnJnYmEoOCwxMiwyMCwwLjk2KTsKICBib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1ib3JkZXIpOwogIGJhY2tkcm9wLWZpbHRlcjpibHVyKDEycHgpOwogIHBhZGRpbmc6MCAyMHB4O2hlaWdodDo1NnB4OwogIGRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Cn0KLmxvZ297ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmJhc2VsaW5lO2dhcDoxMHB4O3RleHQtZGVjb3JhdGlvbjpub25lfQoubG9nby1qYXtmb250LWZhbWlseTonTm90byBTZXJpZiBKUCcsc2VyaWY7Zm9udC13ZWlnaHQ6OTAwO2ZvbnQtc2l6ZToxLjJyZW07Y29sb3I6dmFyKC0tZ29sZC1sKTtsZXR0ZXItc3BhY2luZzouMDZlbTt0ZXh0LXNoYWRvdzowIDAgMjBweCByZ2JhKDIwMSwxNjgsNzYsLjMpfQoubG9nby1lbntmb250LWZhbWlseTonQmViYXMgTmV1ZScsc2Fucy1zZXJpZjtmb250LXNpemU6LjcycmVtO2NvbG9yOnZhcigtLXRleHQtbXV0ZSk7bGV0dGVyLXNwYWNpbmc6LjIyZW19Cm5hdiBhe2NvbG9yOnZhcigtLXRleHQtZGltKTt0ZXh0LWRlY29yYXRpb246bm9uZTtmb250LXNpemU6Ljc1cmVtO3BhZGRpbmc6NnB4IDEycHg7dHJhbnNpdGlvbjpjb2xvciAuMnN9Cm5hdiBhOmhvdmVye2NvbG9yOnZhcigtLWdvbGQtbCl9CgovKiDilZDilZAgSEVSTyDilZDilZAgKi8KLmhlcm97CiAgcG9zaXRpb246cmVsYXRpdmU7CiAgYmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTYwZGVnLCMwZDFhMmUgMCUsIzA4MGMxNCA1NSUsIzBhMGMxMCAxMDAlKTsKICBwYWRkaW5nOjYwcHggMjBweCAwOwogIG92ZXJmbG93OmhpZGRlbjsKfQouaGVybzo6YmVmb3JlewogIGNvbnRlbnQ6Jyc7cG9zaXRpb246YWJzb2x1dGU7aW5zZXQ6MDsKICBiYWNrZ3JvdW5kOnJhZGlhbC1ncmFkaWVudChlbGxpcHNlIDYwJSA1MCUgYXQgNTAlIDAlLHJnYmEoMjAxLDE2OCw3NiwuMDYpIDAlLHRyYW5zcGFyZW50IDcwJSk7CiAgcG9pbnRlci1ldmVudHM6bm9uZTsKfQouaGVyby10ZXh0ewogIHBvc2l0aW9uOnJlbGF0aXZlO3otaW5kZXg6MjsKICB0ZXh0LWFsaWduOmNlbnRlcjttYXgtd2lkdGg6NzAwcHg7bWFyZ2luOjAgYXV0bzsKICBwYWRkaW5nLWJvdHRvbTo0MHB4Owp9Ci5oZXJvLWJhZGdlewogIGRpc3BsYXk6aW5saW5lLWZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7CiAgcGFkZGluZzo0cHggMTZweDttYXJnaW4tYm90dG9tOjI0cHg7CiAgYm9yZGVyOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjM1KTtib3JkZXItcmFkaXVzOjFweDsKICBmb250LXNpemU6LjY4cmVtO2xldHRlci1zcGFjaW5nOi4yZW07Y29sb3I6dmFyKC0tZ29sZCk7CiAgYmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjA1KTsKfQouaGVyby1iYWRnZTo6YmVmb3Jle2NvbnRlbnQ6Jyc7d2lkdGg6NXB4O2hlaWdodDo1cHg7YmFja2dyb3VuZDp2YXIoLS1jeWNsZSk7Ym9yZGVyLXJhZGl1czo1MCU7Ym94LXNoYWRvdzowIDAgOHB4IHZhcigtLWN5Y2xlKTthbmltYXRpb246YmxpbmsgMnMgaW5maW5pdGV9CkBrZXlmcmFtZXMgYmxpbmt7MCUsMTAwJXtvcGFjaXR5OjF9NTAle29wYWNpdHk6LjJ9fQouaGVyby10aXRsZXsKICBmb250LWZhbWlseTonTm90byBTZXJpZiBKUCcsc2VyaWY7Zm9udC13ZWlnaHQ6OTAwOwogIGZvbnQtc2l6ZTpjbGFtcCgyLjJyZW0sN3Z3LDRyZW0pOwogIGxpbmUtaGVpZ2h0OjEuMTU7bGV0dGVyLXNwYWNpbmc6LjA0ZW07Y29sb3I6I2ZmZjsKICB0ZXh0LXNoYWRvdzowIDAgNDBweCByZ2JhKDIwMSwxNjgsNzYsLjE1KTsKICBtYXJnaW4tYm90dG9tOjEycHg7Cn0KLmhlcm8tdGl0bGUgZW17Y29sb3I6dmFyKC0tZ29sZC1sKTtmb250LXN0eWxlOm5vcm1hbH0KLmhlcm8tc3ViewogIGZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmOwogIGZvbnQtc2l6ZTpjbGFtcCguOHJlbSwydncsMXJlbSk7CiAgbGV0dGVyLXNwYWNpbmc6LjM1ZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKTsKfQoKLyog44OS44O844Ot44O844Kr44O844OJ576kICovCi5oZXJvLWNhcmRzewogIHBvc2l0aW9uOnJlbGF0aXZlO3otaW5kZXg6MjsKICBkaXNwbGF5OmdyaWQ7CiAgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgzLDFmcik7CiAgZ2FwOjA7CiAgbWFyZ2luOjAgLTIwcHg7CiAgYm9yZGVyLXRvcDoxcHggc29saWQgdmFyKC0tYm9yZGVyKTsKfQouaGVyby1jYXJkewogIHBhZGRpbmc6MThweCAyMHB4IDIwcHg7CiAgYm9yZGVyLXJpZ2h0OjFweCBzb2xpZCB2YXIoLS1ib3JkZXIpOwogIGJhY2tncm91bmQ6cmdiYSg4LDEyLDIwLC44NSk7CiAgdGV4dC1kZWNvcmF0aW9uOm5vbmU7CiAgZGlzcGxheTpibG9jazsKICB0cmFuc2l0aW9uOmJhY2tncm91bmQgLjI1czsKICBwb3NpdGlvbjpyZWxhdGl2ZTsKICBvdmVyZmxvdzpoaWRkZW47Cn0KLmhlcm8tY2FyZDpsYXN0LWNoaWxke2JvcmRlci1yaWdodDpub25lfQouaGVyby1jYXJkOjpiZWZvcmV7CiAgY29udGVudDonJzsKICBwb3NpdGlvbjphYnNvbHV0ZTtsZWZ0OjA7dG9wOjA7Ym90dG9tOjA7CiAgd2lkdGg6M3B4Owp9Ci5oZXJvLWNhcmQuaG9yc2U6OmJlZm9yZXtiYWNrZ3JvdW5kOnZhcigtLWhvcnNlKX0KLmhlcm8tY2FyZC5ib2F0OjpiZWZvcmUge2JhY2tncm91bmQ6dmFyKC0tYm9hdCl9Ci5oZXJvLWNhcmQuY3ljbGU6OmJlZm9yZXtiYWNrZ3JvdW5kOnZhcigtLWN5Y2xlKX0KLmhlcm8tY2FyZDpob3ZlcntiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMDQpfQouaGNhcmQtc3BvcnR7CiAgZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4OwogIG1hcmdpbi1ib3R0b206OHB4Owp9Ci5oY2FyZC1pY29ue2ZvbnQtc2l6ZTouOTVyZW19Ci5oY2FyZC1sYWJlbHsKICBmb250LXNpemU6LjY1cmVtO2xldHRlci1zcGFjaW5nOi4xZW07CiAgZm9udC13ZWlnaHQ6NzAwOwp9Ci5oZXJvLWNhcmQuaG9yc2UgLmhjYXJkLWxhYmVse2NvbG9yOnZhcigtLWhvcnNlKX0KLmhlcm8tY2FyZC5ib2F0ICAuaGNhcmQtbGFiZWx7Y29sb3I6dmFyKC0tYm9hdCl9Ci5oZXJvLWNhcmQuY3ljbGUgLmhjYXJkLWxhYmVse2NvbG9yOnZhcigtLWN5Y2xlKX0KLmhjYXJkLWdyYWRlewogIGZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmOwogIGZvbnQtc2l6ZTouNzJyZW07cGFkZGluZzoxcHggN3B4O2JvcmRlci1yYWRpdXM6MnB4OwogIG1hcmdpbi1sZWZ0OmF1dG87bGV0dGVyLXNwYWNpbmc6LjA1ZW07Cn0KLmhlcm8tY2FyZC5ob3JzZSAuaGNhcmQtZ3JhZGV7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjIpO2NvbG9yOnZhcigtLWdvbGQtbCl9Ci5oZXJvLWNhcmQuYm9hdCAgLmhjYXJkLWdyYWRle2JhY2tncm91bmQ6cmdiYSgwLDE4MCwyMTYsLjIpO2NvbG9yOnZhcigtLWJvYXQpfQouaGVyby1jYXJkLmN5Y2xlIC5oY2FyZC1ncmFkZXtiYWNrZ3JvdW5kOnJnYmEoMjMyLDQ5LDU4LC4yKTtjb2xvcjp2YXIoLS1jeWNsZSl9Ci5oY2FyZC12ZW51ZXtmb250LXNpemU6LjcycmVtO2NvbG9yOnZhcigtLXRleHQtZGltKTttYXJnaW4tYm90dG9tOjRweH0KLmhjYXJkLW5hbWV7CiAgZm9udC1mYW1pbHk6J05vdG8gU2VyaWYgSlAnLHNlcmlmO2ZvbnQtd2VpZ2h0OjcwMDsKICBmb250LXNpemU6LjkycmVtO2NvbG9yOnZhcigtLXRleHQpOwogIGxpbmUtaGVpZ2h0OjEuMzttYXJnaW4tYm90dG9tOjEwcHg7Cn0KLmhjYXJkLXRpbWV7CiAgZm9udC1mYW1pbHk6J0JlYmFzIE5ldWUnLHNhbnMtc2VyaWY7CiAgZm9udC1zaXplOjEuNHJlbTtsZXR0ZXItc3BhY2luZzouMDVlbTtjb2xvcjp2YXIoLS1nb2xkLWwpOwogIGxpbmUtaGVpZ2h0OjE7Cn0KLmhjYXJkLXRpbWUtbGFiZWx7Zm9udC1zaXplOi42cmVtO2NvbG9yOnZhcigtLXRleHQtbXV0ZSl9CgovKiDilZDilZAgTUFJTiDilZDilZAgKi8KLm1haW57bWF4LXdpZHRoOjkwMHB4O21hcmdpbjowIGF1dG87cGFkZGluZzowIDAgODBweH0KCi8qIOOCu+OCr+OCt+ODp+ODs+WFsemAmiAqLwouc2Vje3BhZGRpbmc6MjRweCAyMHB4IDB9Ci5zZWMtaGVhZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxNHB4fQouc2VjLWxpbmV7d2lkdGg6M3B4O2hlaWdodDoxNXB4O2JhY2tncm91bmQ6dmFyKC0tZ29sZCk7Ym9yZGVyLXJhZGl1czoycHg7ZmxleC1zaHJpbms6MH0KLnNlYy10aXRsZXtmb250LXNpemU6Ljc2cmVtO2xldHRlci1zcGFjaW5nOi4xMmVtO2NvbG9yOnZhcigtLWdvbGQpO2ZvbnQtd2VpZ2h0OjcwMH0KCi8qIOKVkOKVkCBXRUVLIENBTEVOREFSIOKVkOKVkCAqLwoud2Vlay10YWJzewogIGRpc3BsYXk6ZmxleDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWJvcmRlcik7Ym9yZGVyLXJhZGl1czozcHg7b3ZlcmZsb3c6aGlkZGVuOwp9Ci53ZWVrLXRhYnsKICBmbGV4OjE7cGFkZGluZzoxMHB4IDJweDt0ZXh0LWFsaWduOmNlbnRlcjsKICBjdXJzb3I6cG9pbnRlcjtib3JkZXItcmlnaHQ6MXB4IHNvbGlkIHZhcigtLWJvcmRlcik7CiAgYmFja2dyb3VuZDp2YXIoLS1uYXZ5KTt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjJzO3VzZXItc2VsZWN0Om5vbmU7Cn0KLndlZWstdGFiOmxhc3QtY2hpbGR7Ym9yZGVyLXJpZ2h0Om5vbmV9Ci53ZWVrLXRhYiAud2R7Zm9udC1zaXplOi42MnJlbTtjb2xvcjp2YXIoLS10ZXh0LW11dGUpO2Rpc3BsYXk6YmxvY2s7bWFyZ2luLWJvdHRvbToycHh9Ci53ZWVrLXRhYiAuZGR7Zm9udC1mYW1pbHk6J0JlYmFzIE5ldWUnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjEuMnJlbTtjb2xvcjp2YXIoLS10ZXh0KTtsaW5lLWhlaWdodDoxO2Rpc3BsYXk6YmxvY2t9Ci53ZWVrLXRhYiAuY250e2ZvbnQtc2l6ZTouNTVyZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKTttYXJnaW4tdG9wOjJweDtkaXNwbGF5OmJsb2NrfQoud2Vlay10YWIuYWN0aXZle2JhY2tncm91bmQ6dmFyKC0tZ29sZCl9Ci53ZWVrLXRhYi5hY3RpdmUgLndkLC53ZWVrLXRhYi5hY3RpdmUgLmRkLC53ZWVrLXRhYi5hY3RpdmUgLmNudHtjb2xvcjojMDgwYzE0fQoud2Vlay10YWIudG9kYXkgLmRke2NvbG9yOnZhcigtLWdvbGQtbCl9Ci53ZWVrLXRhYi50b2RheS5hY3RpdmUgLmRke2NvbG9yOiMwODBjMTR9Ci53ZWVrLXRhYi5zYXQgLndke2NvbG9yOnZhcigtLWJvYXQpfQoud2Vlay10YWIuc3VuIC53ZHtjb2xvcjp2YXIoLS1jeWNsZSl9Ci53ZWVrLXRhYi5hY3RpdmUuc2F0IC53ZCwud2Vlay10YWIuYWN0aXZlLnN1biAud2R7Y29sb3I6IzA4MGMxNH0KLndlZWstdGFiOmhvdmVyOm5vdCguYWN0aXZlKXtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMDUpfQoKLyog4pWQ4pWQIOazqOebruODrOODvOOCuSDilZDilZAgKi8KLmZvY3VzLWxpc3R7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6MXB4fQouZm9jdXMtaXRlbXsKICBkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyOwogIHBhZGRpbmc6MTFweCAxNHB4O2dhcDoxMHB4OwogIGJhY2tncm91bmQ6dmFyKC0tYmctY2FyZCk7CiAgYm9yZGVyLWxlZnQ6M3B4IHNvbGlkIHRyYW5zcGFyZW50OwogIHRleHQtZGVjb3JhdGlvbjpub25lO3RyYW5zaXRpb246YWxsIC4yczsKfQouZm9jdXMtaXRlbS5ob3JzZXtib3JkZXItbGVmdC1jb2xvcjp2YXIoLS1ob3JzZSl9Ci5mb2N1cy1pdGVtLmJvYXQge2JvcmRlci1sZWZ0LWNvbG9yOnZhcigtLWJvYXQpfQouZm9jdXMtaXRlbS5jeWNsZXtib3JkZXItbGVmdC1jb2xvcjp2YXIoLS1jeWNsZSl9Ci5mb2N1cy1pdGVtOmhvdmVye2JhY2tncm91bmQ6dmFyKC0tYmctY2FyZDIpfQouZmktZGF0ZXtmb250LXNpemU6LjY4cmVtO2NvbG9yOnZhcigtLXRleHQtZGltKTt3aGl0ZS1zcGFjZTpub3dyYXA7d2lkdGg6NzJweDtmbGV4LXNocmluazowfQouZmktdmVudWV7Zm9udC1zaXplOi43MnJlbTtjb2xvcjp2YXIoLS10ZXh0LWRpbSk7d2hpdGUtc3BhY2U6bm93cmFwO3dpZHRoOjU2cHg7ZmxleC1zaHJpbms6MH0KLmZpLW5hbWV7Zm9udC1mYW1pbHk6J05vdG8gU2VyaWYgSlAnLHNlcmlmO2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6Ljg4cmVtO2NvbG9yOnZhcigtLXRleHQpO2ZsZXg6MX0KLmZpLWdyYWRlewogIGZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTouNzZyZW07CiAgcGFkZGluZzoycHggOHB4O2JvcmRlci1yYWRpdXM6MnB4O2xldHRlci1zcGFjaW5nOi4wNWVtO2ZsZXgtc2hyaW5rOjA7Cn0KLmZnLWcxe2JhY2tncm91bmQ6dmFyKC0tZ29sZCk7Y29sb3I6IzA4MGMxNH0KLmZnLWcye2JhY2tncm91bmQ6IzZhNmE4YTtjb2xvcjojZmZmfQouZmctZzN7YmFja2dyb3VuZDojM2E2YTNhO2NvbG9yOiNmZmZ9Ci5mZy1zZ3tiYWNrZ3JvdW5kOnZhcigtLWN5Y2xlKTtjb2xvcjojZmZmfQouZmctZ3B7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTM1ZGVnLHZhcigtLWdvbGQpLHZhcigtLWN5Y2xlKSk7Y29sb3I6I2ZmZn0KLmZpLWFycntjb2xvcjp2YXIoLS10ZXh0LW11dGUpO2ZvbnQtc2l6ZTouNzhyZW07ZmxleC1zaHJpbms6MH0KCi8qIOKVkOKVkCDplovlgqzlnLAg4pWQ4pWQICovCi52ZW51ZXMtZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgzLDFmcik7Z2FwOjhweH0KLnZlbnVlLWJsb2Nre2JhY2tncm91bmQ6dmFyKC0tYmctY2FyZCk7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgdmFyKC0tYm9yZGVyKX0KLnZiLWhlYWR7CiAgcGFkZGluZzo4cHggMTJweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHg7CiAgYm9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDUpOwp9Ci52Yi1oZWFkLmhvcnNle2JvcmRlci10b3A6MnB4IHNvbGlkIHZhcigtLWhvcnNlKX0KLnZiLWhlYWQuYm9hdCB7Ym9yZGVyLXRvcDoycHggc29saWQgdmFyKC0tYm9hdCl9Ci52Yi1oZWFkLmN5Y2xle2JvcmRlci10b3A6MnB4IHNvbGlkIHZhcigtLWN5Y2xlKX0KLnZiLWljb257Zm9udC1zaXplOi45cmVtfQoudmItbmFtZXsKICBmb250LXNpemU6LjdyZW07Zm9udC13ZWlnaHQ6NzAwO2xldHRlci1zcGFjaW5nOi4wNmVtOwp9Ci52Yi1oZWFkLmhvcnNlIC52Yi1uYW1le2NvbG9yOnZhcigtLWhvcnNlKX0KLnZiLWhlYWQuYm9hdCAgLnZiLW5hbWV7Y29sb3I6dmFyKC0tYm9hdCl9Ci52Yi1oZWFkLmN5Y2xlIC52Yi1uYW1le2NvbG9yOnZhcigtLWN5Y2xlKX0KLnZlbnVlLWxpc3R7bGlzdC1zdHlsZTpub25lO3BhZGRpbmc6NHB4IDB9Ci52ZW51ZS1saXN0IGxpewogIHBhZGRpbmc6NXB4IDEycHg7Zm9udC1zaXplOi43NXJlbTtjb2xvcjp2YXIoLS10ZXh0KTsKICBkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuOwogIGJvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjAzKTsKICBjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjE1czsKfQoudmVudWUtbGlzdCBsaTpsYXN0LWNoaWxke2JvcmRlci1ib3R0b206bm9uZX0KLnZlbnVlLWxpc3QgbGk6aG92ZXJ7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMyl9Ci52dGFne2ZvbnQtc2l6ZTouNThyZW07cGFkZGluZzoxcHggNXB4O2JvcmRlci1yYWRpdXM6MnB4fQoudnRhZy1nb2xke2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4xOCk7Y29sb3I6dmFyKC0tZ29sZCl9Ci52dGFnLXJlZCB7YmFja2dyb3VuZDpyZ2JhKDIzMiw0OSw1OCwuMTgpO2NvbG9yOnZhcigtLWN5Y2xlKX0KCi8qIOKVkOKVkCBQSUNLUyDilZDilZAgKi8KLnBpY2tzLWxpc3R7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6OHB4fQoucGljay1jYXJkewogIGJhY2tncm91bmQ6dmFyKC0tYmctY2FyZCk7CiAgYm9yZGVyLWxlZnQ6NHB4IHNvbGlkIHRyYW5zcGFyZW50OwogIGRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpzdHJldGNoOwogIHRleHQtZGVjb3JhdGlvbjpub25lO3RyYW5zaXRpb246YmFja2dyb3VuZCAuMnM7Cn0KLnBpY2stY2FyZC5ob3JzZXtib3JkZXItbGVmdC1jb2xvcjp2YXIoLS1ob3JzZSl9Ci5waWNrLWNhcmQuYm9hdCB7Ym9yZGVyLWxlZnQtY29sb3I6dmFyKC0tYm9hdCl9Ci5waWNrLWNhcmQuY3ljbGV7Ym9yZGVyLWxlZnQtY29sb3I6dmFyKC0tY3ljbGUpfQoucGljay1jYXJkOmhvdmVye2JhY2tncm91bmQ6dmFyKC0tYmctY2FyZDIpfQoucGMtbGVmdHtwYWRkaW5nOjE0cHggMTZweDtmbGV4OjF9Ci5wYy1tZXRhe2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDttYXJnaW4tYm90dG9tOjZweH0KLnBjLXNwb3J0e2ZvbnQtc2l6ZTouNjNyZW07cGFkZGluZzoycHggOHB4O2JvcmRlci1yYWRpdXM6MnB4O2ZvbnQtd2VpZ2h0OjcwMDtsZXR0ZXItc3BhY2luZzouMDVlbX0KLnBjLXNwb3J0LmhvcnNle2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4xNSk7Y29sb3I6dmFyKC0tZ29sZC1sKX0KLnBjLXNwb3J0LmJvYXQge2JhY2tncm91bmQ6cmdiYSgwLDE4MCwyMTYsLjE1KTtjb2xvcjp2YXIoLS1ib2F0KX0KLnBjLXNwb3J0LmN5Y2xle2JhY2tncm91bmQ6cmdiYSgyMzIsNDksNTgsLjE1KTtjb2xvcjp2YXIoLS1jeWNsZSl9Ci5wYy1yYWNle2ZvbnQtc2l6ZTouNjhyZW07Y29sb3I6dmFyKC0tdGV4dC1kaW0pfQoucGMtbmFtZXtmb250LWZhbWlseTonTm90byBTZXJpZiBKUCcsc2VyaWY7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZTouOTVyZW07Y29sb3I6dmFyKC0tdGV4dCk7bWFyZ2luLWJvdHRvbTo2cHh9Ci5wYy1ib3R0b217ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweH0KLnBjLXZlbnVle2ZvbnQtc2l6ZTouN3JlbTtjb2xvcjp2YXIoLS10ZXh0LWRpbSl9Ci5wYy10aW1le2ZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZToxLjE1cmVtO2NvbG9yOnZhcigtLWdvbGQtbCk7bGV0dGVyLXNwYWNpbmc6LjA0ZW19Ci5wYy1yaWdodHsKICBkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyOwogIHBhZGRpbmc6MTRweCAxOHB4O2JvcmRlci1sZWZ0OjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNSk7Z2FwOjZweDttaW4td2lkdGg6NzZweDsKfQoucGMtYXJye2ZvbnQtc2l6ZToxLjNyZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKX0KLnBjLWxpbmt7Zm9udC1zaXplOi42OHJlbTtjb2xvcjp2YXIoLS1nb2xkKTt3aGl0ZS1zcGFjZTpub3dyYXB9CgouZW1wdHl7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzozNnB4O2NvbG9yOnZhcigtLXRleHQtbXV0ZSk7Zm9udC1zaXplOi44MnJlbX0KCi8qIOKVkOKVkCBGT09URVIg4pWQ4pWQICovCmZvb3RlcnsKICBiYWNrZ3JvdW5kOiMwNTA4MTA7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4wOCk7CiAgcGFkZGluZzoyOHB4IDIwcHg7dGV4dC1hbGlnbjpjZW50ZXI7Cn0KLmZ0LWxvZ297Zm9udC1mYW1pbHk6J05vdG8gU2VyaWYgSlAnLHNlcmlmO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MXJlbTtjb2xvcjp2YXIoLS1nb2xkLWwpO21hcmdpbi1ib3R0b206OHB4fQouZnQtZGlzY3tmb250LXNpemU6LjY2cmVtO2NvbG9yOnJnYmEoMTY4LDE4MCwyMDAsLjQ1KTtsaW5lLWhlaWdodDoxLjc7bWF4LXdpZHRoOjQ2MHB4O21hcmdpbjowIGF1dG8gMTJweH0KLmZ0LWxpbmtze2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2dhcDoxNnB4O2ZsZXgtd3JhcDp3cmFwfQouZnQtbGlua3MgYXtmb250LXNpemU6LjdyZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKTt0ZXh0LWRlY29yYXRpb246bm9uZX0KLmZ0LWxpbmtzIGE6aG92ZXJ7Y29sb3I6dmFyKC0tZ29sZC1sKX0KCi8qIOKVkOKVkCBTSEFSRSDilZDilZAgKi8KLnNoYXJlLWJsb2Nre2JhY2tncm91bmQ6dmFyKC0tYmctY2FyZCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1ib3JkZXIpO3BhZGRpbmc6MTRweCAyMHB4O21hcmdpbjoxNnB4IDIwcHggMH0KLnNoYXJlLXRpdGxle2ZvbnQtc2l6ZTouNjhyZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKTttYXJnaW4tYm90dG9tOjEwcHg7bGV0dGVyLXNwYWNpbmc6LjA1ZW19Ci5zaGFyZS1idXR0b25ze2Rpc3BsYXk6ZmxleDtnYXA6OHB4O2ZsZXgtd3JhcDp3cmFwfQouc2hhcmUtYnRue2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjZweDtwYWRkaW5nOjlweCAxNnB4O2JvcmRlci1yYWRpdXM6MnB4O2ZvbnQtc2l6ZTouNzhyZW07Zm9udC13ZWlnaHQ6NzAwO3RleHQtZGVjb3JhdGlvbjpub25lO2JvcmRlcjpub25lO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidOb3RvIFNhbnMgSlAnLHNhbnMtc2VyaWY7dHJhbnNpdGlvbjpmaWx0ZXIgLjJzO3doaXRlLXNwYWNlOm5vd3JhcH0KLnNoYXJlLWJ0bjpob3ZlcntmaWx0ZXI6YnJpZ2h0bmVzcygxLjE1KX0KLnNoYXJlLXh7YmFja2dyb3VuZDojMDAwO2NvbG9yOiNmZmY7Ym9yZGVyOjFweCBzb2xpZCAjMzMzfQouc2hhcmUtbGluZXtiYWNrZ3JvdW5kOiMwNkM3NTU7Y29sb3I6I2ZmZn0KLnNoYXJlLWlne2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjZjA5NDMzLCNlNjY4M2MsI2RjMjc0MywjY2MyMzY2LCNiYzE4ODgpO2NvbG9yOiNmZmZ9Ci5zaGFyZS1jb3B5e2JhY2tncm91bmQ6dmFyKC0tYmctY2FyZDIpO2NvbG9yOnZhcigtLXRleHQtZGltKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWJvcmRlcil9Ci5zaGFyZS1jb3BpZWR7Y29sb3I6dmFyKC0tZ29sZC1sKSFpbXBvcnRhbnQ7Ym9yZGVyLWNvbG9yOnZhcigtLWdvbGQpIWltcG9ydGFudH0KCi8qIOKVkOKVkCBSRVNQT05TSVZFIOKVkOKVkCAqLwpAbWVkaWEobWF4LXdpZHRoOjYwMHB4KXsKICAudmVudWVzLWdyaWR7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmcn0KICAuaGVyby1jYXJkc3tncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyfQogIC5oZXJvLWNhcmR7Ym9yZGVyLXJpZ2h0Om5vbmU7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgdmFyKC0tYm9yZGVyKX0KICAuaGVyby1jYXJkOmxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTpub25lfQogIG5hdntkaXNwbGF5Om5vbmV9CiAgLmZpLXZlbnVle2Rpc3BsYXk6bm9uZX0KICAuc2hhcmUtYnV0dG9uc3tnYXA6NnB4fQogIC5zaGFyZS1idG57Zm9udC1zaXplOi43MnJlbTtwYWRkaW5nOjhweCAxMnB4fQp9CkBtZWRpYShtYXgtd2lkdGg6MzgwcHgpewogIC5oZXJvLXRpdGxle2ZvbnQtc2l6ZToycmVtfQp9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+Cgo8IS0tIEhFQURFUiAtLT4KPGhlYWRlcj4KICA8YSBjbGFzcz0ibG9nbyIgaHJlZj0iIyI+CiAgICA8c3BhbiBjbGFzcz0ibG9nby1qYSI+5LqI5oOz44Gu6YmE5YmHPC9zcGFuPgogICAgPHNwYW4gY2xhc3M9ImxvZ28tZW4iPllPU08gTk8gVEVTU09LVTwvc3Bhbj4KICA8L2E+CiAgPG5hdj4KICAgIDxhIGhyZWY9ImtlaWJhLmh0bWwiPvCfkLQg56u26aasPC9hPjxhIGhyZWY9Imt5b3RlaS5odG1sIj7wn5qkIOertuiJhzwvYT48YSBocmVmPSJrZWlyaW4uaHRtbCI+8J+atCDnq7bovKo8L2E+CiAgICA8YSBocmVmPSJyZXN1bHRzLmh0bWwiPvCfk4og5a6f57i+PC9hPgogICAgPGEgaHJlZj0icHJlbWl1bS5odG1sIiBzdHlsZT0iY29sb3I6IzA2Qzc1NTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoNiwxOTksODUsLjMpO2JvcmRlci1yYWRpdXM6MnB4O3BhZGRpbmc6NHB4IDEwcHgiPvCfkZEg5pyJ5paZ44OX44Op44OzPC9hPgogIDwvbmF2Pgo8L2hlYWRlcj4KCjwhLS0gSEVSTyAtLT4KPGRpdiBjbGFzcz0iaGVybyI+CiAgPGRpdiBjbGFzcz0iaGVyby10ZXh0Ij4KICAgIDxkaXYgY2xhc3M9Imhlcm8tYmFkZ2UiIGlkPSJ1cGRhdGUtYmFkZ2UiPkVWIFBSRURJQ1RJT04gwrcg5pyf5b6F5YCk44Gn5Yud44GkPC9kaXY+CiAgICA8aDEgY2xhc3M9Imhlcm8tdGl0bGUiPuaEn+immuOCkuaNqOOBpuOBpjxicj48ZW0+44OH44O844K/44Gn5Yud44GmPC9lbT48L2gxPgogICAgPHAgY2xhc3M9Imhlcm8tc3ViIj5IT1JTRSDCtyBCT0FUIMK3IENZQ0xFIMK3IEVWIEFOQUxZU0lTPC9wPgogICAgPCEtLSBMSU5FIENUQSAtLT4KICAgIDxkaXYgc3R5bGU9Im1hcmdpbi10b3A6MjhweDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweCI+CiAgICAgIDxhIGhyZWY9Imh0dHBzOi8vbGluZS5tZS9SL3RpL3AvQDQxNGlyaWt4IiB0YXJnZXQ9Il9ibGFuayIgcmVsPSJub29wZW5lciIKICAgICAgICBzdHlsZT0iZGlzcGxheTppbmxpbmUtZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7cGFkZGluZzoxNnB4IDMycHg7YmFja2dyb3VuZDojMDZDNzU1O2NvbG9yOiNmZmY7Zm9udC1mYW1pbHk6J05vdG8gU2FucyBKUCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjFyZW07dGV4dC1kZWNvcmF0aW9uOm5vbmU7Ym9yZGVyLXJhZGl1czo0cHg7bGV0dGVyLXNwYWNpbmc6LjA0ZW07Ym94LXNoYWRvdzowIDRweCAyNHB4IHJnYmEoNiwxOTksODUsLjM1KTt0cmFuc2l0aW9uOmZpbHRlciAuMjVzIj4KICAgICAgICDwn5KsIOeEoeaWmUxJTkXnmbvpjLLjgafku4rml6Xjga7ljrPpgbhFVuODrOODvOOCueOCkuWPl+OBkeWPluOCiwogICAgICA8L2E+CiAgICAgIDxwIHN0eWxlPSJmb250LXNpemU6LjY4cmVtO2NvbG9yOnZhcigtLXRleHQtbXV0ZSkiPueZu+mMsueEoeaWmSDCtyDjgYTjgaTjgafjgoLop6PntIRPSyDCtyDmr47mnJ3phY3kv6E8L3A+CiAgICA8L2Rpdj4KICA8L2Rpdj4KICA8ZGl2IGNsYXNzPSJoZXJvLWNhcmRzIiBpZD0iaGVyby1jYXJkcyI+CiAgICA8IS0tIEpT5o+P55S7IC0tPgogIDwvZGl2Pgo8L2Rpdj4KCjxkaXYgY2xhc3M9Im1haW4iPgoKICA8IS0tIOOCq+ODrOODs+ODgOODvCAtLT4KICA8ZGl2IGNsYXNzPSJzZWMiIHN0eWxlPSJwYWRkaW5nLWJvdHRvbToyNHB4Ij4KICAgIDxkaXYgY2xhc3M9InNlYy1oZWFkIj48ZGl2IGNsYXNzPSJzZWMtbGluZSI+PC9kaXY+PHNwYW4gY2xhc3M9InNlYy10aXRsZSIgaWQ9InBpY2tzLXRpdGxlIj7mnKzml6Xjga7kuojmg7M8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwaWNrcy1saXN0IiBpZD0icGlja3MtbGlzdCI+PGRpdiBjbGFzcz0iZW1wdHkiPuiqreOBv+i+vOOBv+S4rS4uLjwvZGl2PjwvZGl2PgogIDwvZGl2PgoKICA8IS0tIEVW5a6f57i+44OQ44OK44O8IC0tPgogIDxkaXYgc3R5bGU9Im1hcmdpbjowIDIwcHgiPgogIDxkaXYgc3R5bGU9ImJhY2tncm91bmQ6dmFyKC0tYmctY2FyZCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1ib3JkZXIpO2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDMsMWZyKTtnYXA6MDt0ZXh0LWFsaWduOmNlbnRlcjtib3JkZXItYm90dG9tOm5vbmUiPgogICAgPGRpdiBzdHlsZT0iYm9yZGVyLXJpZ2h0OjFweCBzb2xpZCB2YXIoLS1ib3JkZXIpO3BhZGRpbmc6MTBweCA4cHgiPgogICAgICA8ZGl2IHN0eWxlPSJmb250LWZhbWlseTonQmViYXMgTmV1ZScsc2Fucy1zZXJpZjtmb250LXNpemU6MS44cmVtO2NvbG9yOnZhcigtLWdvbGQtbCkiIGlkPSJpZHgtcmVjb3ZlcnkiPi0tJTwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6LjU4cmVtO2NvbG9yOnZhcigtLXRleHQtbXV0ZSk7bWFyZ2luLXRvcDoxcHgiPue0r+ioiOWbnuWPjueOhzwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJib3JkZXItcmlnaHQ6MXB4IHNvbGlkIHZhcigtLWJvcmRlcik7cGFkZGluZzoxMHB4IDhweCI+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZToxLjhyZW07Y29sb3I6I2ZmZiIgaWQ9ImlkeC1oaXRyYXRlIj4tLSU8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOi41OHJlbTtjb2xvcjp2YXIoLS10ZXh0LW11dGUpO21hcmdpbi10b3A6MXB4Ij7nmoTkuK3njoc8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBzdHlsZT0icGFkZGluZzoxMHB4IDhweCI+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZToxLjhyZW07Y29sb3I6I2ZmZiIgaWQ9ImlkeC10b3RhbCI+MDwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6LjU4cmVtO2NvbG9yOnZhcigtLXRleHQtbXV0ZSk7bWFyZ2luLXRvcDoxcHgiPue0r+ioiOS6iOaDs+aVsDwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBzdHlsZT0iYmFja2dyb3VuZDp2YXIoLS1iZy1jYXJkKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWJvcmRlcik7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoMywxZnIpO2dhcDowO3RleHQtYWxpZ246Y2VudGVyIj4KICAgIDxkaXYgc3R5bGU9ImJvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tYm9yZGVyKTtwYWRkaW5nOjEwcHggOHB4Ij4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOi42MnJlbTtjb2xvcjp2YXIoLS1ob3JzZSk7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi1ib3R0b206NHB4Ij7wn5C0IOertummrDwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJmb250LWZhbWlseTonQmViYXMgTmV1ZScsc2Fucy1zZXJpZjtmb250LXNpemU6MS4ycmVtO2NvbG9yOiNmZmYiIGlkPSJzdGF0LWhvcnNlLWhpdCI+LS0lPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTouNTVyZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKSI+55qE5Lit546HPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZToxLjJyZW07Y29sb3I6dmFyKC0tZ29sZC1sKTttYXJnaW4tdG9wOjJweCIgaWQ9InN0YXQtaG9yc2UtcmVjIj4tLSU8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOi41NXJlbTtjb2xvcjp2YXIoLS10ZXh0LW11dGUpIj7lm57lj47njoc8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBzdHlsZT0iYm9yZGVyLXJpZ2h0OjFweCBzb2xpZCB2YXIoLS1ib3JkZXIpO3BhZGRpbmc6MTBweCA4cHgiPgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6LjYycmVtO2NvbG9yOnZhcigtLWJvYXQpO2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tYm90dG9tOjRweCI+8J+apCDnq7boiYc8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1mYW1pbHk6J0JlYmFzIE5ldWUnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjEuMnJlbTtjb2xvcjojZmZmIiBpZD0ic3RhdC1ib2F0LWhpdCI+LS0lPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTouNTVyZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKSI+55qE5Lit546HPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZToxLjJyZW07Y29sb3I6dmFyKC0tZ29sZC1sKTttYXJnaW4tdG9wOjJweCIgaWQ9InN0YXQtYm9hdC1yZWMiPi0tJTwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6LjU1cmVtO2NvbG9yOnZhcigtLXRleHQtbXV0ZSkiPuWbnuWPjueOhzwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJwYWRkaW5nOjEwcHggOHB4Ij4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOi42MnJlbTtjb2xvcjp2YXIoLS1jeWNsZSk7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi1ib3R0b206NHB4Ij7wn5q0IOertui8qjwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJmb250LWZhbWlseTonQmViYXMgTmV1ZScsc2Fucy1zZXJpZjtmb250LXNpemU6MS4ycmVtO2NvbG9yOiNmZmYiIGlkPSJzdGF0LWN5Y2xlLWhpdCI+LS0lPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTouNTVyZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKSI+55qE5Lit546HPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZToxLjJyZW07Y29sb3I6dmFyKC0tZ29sZC1sKTttYXJnaW4tdG9wOjJweCIgaWQ9InN0YXQtY3ljbGUtcmVjIj4tLSU8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOi41NXJlbTtjb2xvcjp2YXIoLS10ZXh0LW11dGUpIj7lm57lj47njoc8L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2Pgo8L2Rpdj4KICA8ZGl2IHN0eWxlPSJ0ZXh0LWFsaWduOmNlbnRlcjttYXJnaW46NnB4IDIwcHggMCI+CiAgICA8YSBocmVmPSJyZXN1bHRzLmh0bWwiIHN0eWxlPSJmb250LXNpemU6LjdyZW07Y29sb3I6dmFyKC0tZ29sZCk7dGV4dC1kZWNvcmF0aW9uOm5vbmUiPuWFqOS6iOaDs+WxpeattOOCkuimi+OCiyDihpI8L2E+CiAgPC9kaXY+CgogIDwhLS0g54Sh5paZ5LqI5oOzIC0tPgogIDxkaXYgY2xhc3M9InNlYyIgc3R5bGU9InBhZGRpbmctYm90dG9tOjAiPgogICAgPGRpdiBjbGFzcz0ic2VjLWhlYWQiPjxkaXYgY2xhc3M9InNlYy1saW5lIj48L2Rpdj48c3BhbiBjbGFzcz0ic2VjLXRpdGxlIj7mnKzml6Xjga7nhKHmlpnkuojmg7PvvIhFVuioiOeul+a4iOOBv++8iTwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgaWQ9ImZyZWUtcHJlZGljdGlvbnMiIHN0eWxlPSJkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2dhcDo4cHg7bWFyZ2luLWJvdHRvbTowIj4KICAgICAgPGRpdiBjbGFzcz0iZW1wdHkiPuiqreOBv+i+vOOBv+S4rS4uLjwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gTElOReeZu+mMsuODkOODiuODvO+8iOODoeOCpOODs++8iSAtLT4KICA8ZGl2IHN0eWxlPSJiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMzVkZWcsIzA0MmEwYSwjMDYxODA4KTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoNiwxOTksODUsLjI1KTtwYWRkaW5nOjIwcHg7bWFyZ2luOjE2cHggMjBweCAwO3RleHQtYWxpZ246Y2VudGVyIj4KICAgIDxwIHN0eWxlPSJmb250LXNpemU6LjY1cmVtO2NvbG9yOiMwNkM3NTU7Zm9udC13ZWlnaHQ6NzAwO2xldHRlci1zcGFjaW5nOi4xMmVtO21hcmdpbi1ib3R0b206OHB4Ij7wn5OpIExJTkXnhKHmlpnnmbvpjLI8L3A+CiAgICA8cCBzdHlsZT0iZm9udC1mYW1pbHk6J05vdG8gU2VyaWYgSlAnLHNlcmlmO2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MXJlbTtjb2xvcjojZmZmO21hcmdpbi1ib3R0b206NnB4Ij7ku4rml6Xjga7ljrPpgbhFVuODrOODvOOCueOCkueEoeaWmeOBp+WPl+OBkeWPluOCizwvcD4KICAgIDxwIHN0eWxlPSJmb250LXNpemU6Ljc1cmVtO2NvbG9yOiNhOGIwYzQ7bWFyZ2luLWJvdHRvbToxNnB4O2xpbmUtaGVpZ2h0OjEuNyI+5oSf6Kaa44Gn44Gv44Gq44GP44OH44O844K/44Gn6YG444KT44Gg5pyf5b6F5YCk44OX44Op44K544Gu44Os44O844K544Gu44G/44KS5q+O5pyd6YWN5L+h44CCPGJyPueZu+mMsueEoeaWmeODu+OBhOOBpOOBp+OCguino+e0hE9L44CCPC9wPgogICAgPGEgaHJlZj0iaHR0cHM6Ly9saW5lLm1lL1IvdGkvcC9ANDE0aXJpa3giIHRhcmdldD0iX2JsYW5rIiByZWw9Im5vb3BlbmVyIgogICAgICBzdHlsZT0iZGlzcGxheTppbmxpbmUtZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDtwYWRkaW5nOjE0cHggMjhweDtiYWNrZ3JvdW5kOiMwNkM3NTU7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOi45cmVtO3RleHQtZGVjb3JhdGlvbjpub25lO2JvcmRlci1yYWRpdXM6NHB4O2xldHRlci1zcGFjaW5nOi4wNGVtIj4KICAgICAg8J+SrCDlj4vjgaDjgaHov73liqDvvIjnhKHmlpnvvIkKICAgIDwvYT4KICAgIDxwIHN0eWxlPSJmb250LXNpemU6LjY1cmVtO2NvbG9yOiM1YTYyNzg7bWFyZ2luLXRvcDoxMHB4Ij7jg5fjg6zjg5/jgqLjg6Djg5fjg6njg7Pjga8gPGEgaHJlZj0icHJlbWl1bS5odG1sIiBzdHlsZT0iY29sb3I6IzA2Qzc1NTt0ZXh0LWRlY29yYXRpb246bm9uZSI+44GT44Gh44KJPC9hPjwvcD4KICA8L2Rpdj4KCjwvZGl2PgoKPGZvb3Rlcj4KICA8cCBjbGFzcz0iZnQtbG9nbyI+5LqI5oOz44Gu6YmE5YmHPC9wPgogIDxwIGNsYXNzPSJmdC1kaXNjIj7mnKzjgrXjgqTjg4jjga7kuojmg7Pjga/mg4XloLHmj5DkvpvjgpLnm67nmoTjgajjgZfjgabjgYTjgb7jgZnjgILlhazllrbnq7bmioDjga7mipXnpajjga/oh6rlt7Hosqzku7vjgafjgYrpoZjjgYTjgZfjgb7jgZnjgIIxOOats+acqua6gOOBruaWueOBruaKleelqOOBr+azleW+i+OBp+emgeOBmOOCieOCjOOBpuOBhOOBvuOBmeOAgjwvcD4KICA8ZGl2IGNsYXNzPSJmdC1saW5rcyI+CiAgICA8YSBocmVmPSJpbmRleC5odG1sIj7jg4jjg4Pjg5c8L2E+CiAgICA8YSBocmVmPSJyZXN1bHRzLmh0bWwiPuWun+e4vjwvYT4KICAgIDxhIGhyZWY9InByZW1pdW0uaHRtbCI+44OX44Os44Of44Ki44OgPC9hPgogICAgPGEgaHJlZj0idG9rdXNob2hvLmh0bWwiPueJueWumuWVhuWPluW8leazleOBq+WfuuOBpeOBj+ihqOiomDwvYT4KICAgIDxhIGhyZWY9InByaXZhY3kuaHRtbCI+44OX44Op44Kk44OQ44K344O844Od44Oq44K344O8PC9hPgogIDwvZGl2Pgo8L2Zvb3Rlcj4KCjxzY3JpcHQ+Ci8vIOKUgOKUgCDjg4Djg5/jg7zjg4fjg7zjgr8g4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSACmNvbnN0IERCID0gewogIDA6e2ZvY3VzOltdLHZlbnVlczp7aG9yc2U6W10sYm9hdDpbXSxjeWNsZTpbXX0scGlja3M6W119LAogIDE6e2ZvY3VzOltdLHZlbnVlczp7aG9yc2U6W10sYm9hdDpbXSxjeWNsZTpbXX0scGlja3M6W119LAogIDI6e2ZvY3VzOltdLHZlbnVlczp7aG9yc2U6W10sYm9hdDpbXSxjeWNsZTpbXX0scGlja3M6W119LAogIDM6e2ZvY3VzOltdLHZlbnVlczp7aG9yc2U6W10sYm9hdDpbXSxjeWNsZTpbXX0scGlja3M6W119LAogIDQ6e2ZvY3VzOltdLHZlbnVlczp7aG9yc2U6W10sYm9hdDpbXSxjeWNsZTpbXX0scGlja3M6W119LAogIDU6e2ZvY3VzOltdLHZlbnVlczp7aG9yc2U6W10sYm9hdDpbXSxjeWNsZTpbXX0scGlja3M6W119LAogIDY6e2ZvY3VzOltdLHZlbnVlczp7aG9yc2U6W10sYm9hdDpbXSxjeWNsZTpbXX0scGlja3M6W119LAp9OwoKY29uc3QgREFZUyA9IFsi5pelIiwi5pyIIiwi54GrIiwi5rC0Iiwi5pyoIiwi6YeRIiwi5ZyfIl07CmNvbnN0IEdSQURFX0NMUyA9IHtHMToiZmctZzEiLEcyOiJmZy1nMiIsRzM6ImZnLWczIixTRzoiZmctc2ciLEdQOiJmZy1ncCJ9Owpjb25zdCBTUE9SVF9NRVRBID0gewogIGhvcnNlOntpY29uOiLwn5C0IixsYWJlbDoi56u26aasIn0sCiAgYm9hdDoge2ljb246IvCfmqQiLGxhYmVsOiLnq7boiYcifSwKICBjeWNsZTp7aWNvbjoi8J+atCIsbGFiZWw6Iuertui8qiJ9LAp9OwoKY29uc3QgdG9kYXkgPSBuZXcgRGF0ZSgpOyB0b2RheS5zZXRIb3VycygwLDAsMCwwKTsKbGV0IHNlbCA9IDA7CgpmdW5jdGlvbiBsYWJlbChpKXtyZXR1cm4gaT09PTA/J+acrOaXpSc6aT09PTE/J+aYjuaXpSc6KCgpPT57Y29uc3QgZD1uZXcgRGF0ZSh0b2RheSk7ZC5zZXREYXRlKHRvZGF5LmdldERhdGUoKStpKTtyZXR1cm4gYCR7ZC5nZXRNb250aCgpKzF9LyR7ZC5nZXREYXRlKCl9YDt9KSgpfQoKZnVuY3Rpb24gcmVuZGVyKGkpewogIGNvbnN0IGQ9REJbaV18fG51bGw7CiAgcmVuZGVySGVybyhEQlswXSk7CiAgcmVuZGVyUGlja3MoZCk7Cn0KCi8vIOODkuODvOODreODvOOCq+ODvOODiQpmdW5jdGlvbiByZW5kZXJIZXJvKGQpewogIGNvbnN0IGVsPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdoZXJvLWNhcmRzJyk7CiAgaWYoIWR8fCFkLnBpY2tzLmxlbmd0aCl7ZWwuaW5uZXJIVE1MPScnO3JldHVybn0KICBlbC5pbm5lckhUTUw9ZC5waWNrcy5tYXAocD0+ewogICAgY29uc3QgbT1TUE9SVF9NRVRBW3Auc3BvcnRdOwogICAgY29uc3QgZ2M9R1JBREVfQ0xTW3AucmFjZS5tYXRjaCgvR1xkfFNHfEdQLyk/LlswXV18fCdmZy1nMic7CiAgICBjb25zdCBncmFkZT1wLnJhY2UubWF0Y2goL0dcZHxTR3xHUC8pPy5bMF18fCcnOwogICAgcmV0dXJuIGA8YSBjbGFzcz0iaGVyby1jYXJkICR7cC5zcG9ydH0iIGhyZWY9IiR7cC51cmx9Ij4KICAgICAgPGRpdiBjbGFzcz0iaGNhcmQtc3BvcnQiPgogICAgICAgIDxzcGFuIGNsYXNzPSJoY2FyZC1pY29uIj4ke20uaWNvbn08L3NwYW4+CiAgICAgICAgPHNwYW4gY2xhc3M9ImhjYXJkLWxhYmVsIj4ke20ubGFiZWx9PC9zcGFuPgogICAgICAgICR7Z3JhZGU/YDxzcGFuIGNsYXNzPSJoY2FyZC1ncmFkZSI+JHtncmFkZX08L3NwYW4+YDonJ30KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImhjYXJkLXZlbnVlIj4ke3AudmVudWV9PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImhjYXJkLW5hbWUiPiR7cC5uYW1lfTwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJoY2FyZC10aW1lIj4ke3AudGltZX08YnI+PHNwYW4gY2xhc3M9ImhjYXJkLXRpbWUtbGFiZWwiPueZuui1sOaZguWIuzwvc3Bhbj48L2Rpdj4KICAgIDwvYT5gOwogIH0pLmpvaW4oJycpOwp9CgpmdW5jdGlvbiByZW5kZXJQaWNrcyhkKXsKICBjb25zdCBlbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncGlja3MtbGlzdCcpOwogIGlmKCFkfHwhZC5waWNrcy5sZW5ndGgpewogICAgZWwuaW5uZXJIVE1MPSc8ZGl2IGNsYXNzPSJlbXB0eSIgc3R5bGU9InBhZGRpbmc6MzJweDt0ZXh0LWFsaWduOmNlbnRlcjtjb2xvcjp2YXIoLS10ZXh0LW11dGUpO2ZvbnQtc2l6ZTouODJyZW0iPuacrOaXpeOBruS6iOaDs+OCkuiqreOBv+i+vOOBv+S4reOBp+OBmeOAgjxicj7jgZfjgbDjgonjgY/jgYrlvoXjgaHjgY/jgaDjgZXjgYTjgII8L2Rpdj4nOwogICAgcmV0dXJuOwogIH0KICBlbC5pbm5lckhUTUw9ZC5waWNrcy5tYXAocD0+ewogICAgY29uc3QgbT1TUE9SVF9NRVRBW3Auc3BvcnRdOwogICAgcmV0dXJuIGA8YSBjbGFzcz0icGljay1jYXJkICR7cC5zcG9ydH0iIGhyZWY9IiR7cC51cmx9Ij4KICAgICAgPGRpdiBjbGFzcz0icGMtbGVmdCI+CiAgICAgICAgPGRpdiBjbGFzcz0icGMtbWV0YSI+CiAgICAgICAgICA8c3BhbiBjbGFzcz0icGMtc3BvcnQgJHtwLnNwb3J0fSI+JHttLmljb259ICR7bS5sYWJlbH08L3NwYW4+CiAgICAgICAgICA8c3BhbiBjbGFzcz0icGMtcmFjZSI+JHtwLnJhY2V9PC9zcGFuPgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9InBjLW5hbWUiPiR7cC5uYW1lfTwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9InBjLWJvdHRvbSI+CiAgICAgICAgICA8c3BhbiBjbGFzcz0icGMtdmVudWUiPiR7cC52ZW51ZX08L3NwYW4+CiAgICAgICAgICA8c3BhbiBjbGFzcz0icGMtdGltZSI+JHtwLnRpbWV9PC9zcGFuPgogICAgICAgIDwvZGl2PgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0icGMtcmlnaHQiPgogICAgICAgIDxzcGFuIGNsYXNzPSJwYy1hcnIiPuKAujwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0icGMtbGluayI+5LqI5oOz44KS6KaL44KLPC9zcGFuPgogICAgICA8L2Rpdj4KICAgIDwvYT5gOwogIH0pLmpvaW4oJycpOwp9CgovLyDliJ3mnJ/ljJYKcmVuZGVyKDApOwoKLy8gcmFjZXMuanNvbiDihpIgRELoh6rli5Xmp4vnr4kKZmV0Y2goJ3JhY2VzLmpzb24/dj0nK0RhdGUubm93KCkpCiAgLnRoZW4ocj0+ci5vaz9yLmpzb24oKTpudWxsKQogIC50aGVuKGpzb249PnsKICAgIGlmKCFqc29ufHwhanNvbi5yYWNlc3x8IWpzb24ucmFjZXMubGVuZ3RoKSByZXR1cm47CiAgICBjb25zdCBHUD17R1A6MCxTRzowLEcxOjEsRzI6MixHMzozLEZJOjQsRklJOjR9OwogICAgY29uc3QgU1U9e2hvcnNlOidrZWliYS5odG1sJyxib2F0OidreW90ZWkuaHRtbCcsY3ljbGU6J2tlaXJpbi5odG1sJ307CiAgICBjb25zdCBWVD17R1A6J3Z0YWctZ29sZCcsU0c6J3Z0YWctcmVkJyxHMTondnRhZy1nb2xkJyxHMjondnRhZy1nb2xkJyxHMzondnRhZy1ncmVlbid9OwogICAgZnVuY3Rpb24gZ3Mocil7cmV0dXJuIEdQW3IuZ3JhZGVdPz81fQogICAgZnVuY3Rpb24gZW4ocil7cmV0dXJuIHBhcnNlRmxvYXQoKHIuZXZ8fCcnKS5yZXBsYWNlKCclJywnJykucmVwbGFjZSgnKycsJycpKXx8MH0KICAgIGNvbnN0IHNmPShhLGIpPT5ncyhhKS1ncyhiKXx8ZW4oYiktZW4oYSk7CiAgICBjb25zdCBhbGw9anNvbi5yYWNlczsKICAgIGxldCBzQj1hbGwuZmlsdGVyKHI9PnIuanVkZ2U9PT0n5by36LK344GEJyk7CiAgICBsZXQgYnU9YWxsLmZpbHRlcihyPT5yLmp1ZGdlPT09J+iyt+OBhCcpOwogICAgbGV0IGVwPWFsbC5maWx0ZXIocj0+IXIuanVkZ2UmJmVuKHIpPjApOwogICAgW3NCLGJ1LGVwXS5mb3JFYWNoKGE9PmEuc29ydChzZikpOwogICAgbGV0IHBrPVtdOwogICAgZm9yKGNvbnN0IHNwIG9mIFsnaG9yc2UnLCdib2F0JywnY3ljbGUnXSl7CiAgICAgIGZvcihjb25zdCBwbCBvZiBbc0IsYnUsZXAsYWxsXSl7CiAgICAgICAgY29uc3QgZj1wbC5maW5kKHI9PnIuc3BvcnQ9PT1zcCYmIXBrLmluY2x1ZGVzKHIpKTsKICAgICAgICBpZihmKXtway5wdXNoKGYpO2JyZWFrO30KICAgICAgfQogICAgfQogICAgZm9yKGNvbnN0IHBsIG9mIFtzQixidSxlcCxhbGxdKXsKICAgICAgZm9yKGNvbnN0IHIgb2YgcGwpe2lmKHBrLmxlbmd0aD49NilicmVhaztpZighcGsuaW5jbHVkZXMocikpcGsucHVzaChyKTt9CiAgICAgIGlmKHBrLmxlbmd0aD49NilicmVhazsKICAgIH0KICAgIERCWzBdLnBpY2tzPXBrLm1hcChyPT4oewogICAgICBzcG9ydDpyLnNwb3J0LHJhY2U6YCR7ci52ZW51ZX3vvI8ke3IuZ3JhZGV8fCcnfWAudHJpbSgpLAogICAgICBuYW1lOnIubmFtZSx2ZW51ZTpyLnZlbnVlLHRpbWU6ci50aW1lLAogICAgICB1cmw6U1Vbci5zcG9ydF18fCcjJyxldjpyLmV2fHwnJyxqdWRnZTpyLmp1ZGdlfHwnJwogICAgfSkpOwogICAgaWYoc2VsPT09MClyZW5kZXIoMCk7ZWxzZSByZW5kZXJIZXJvKERCWzBdKTsKICAgIGNvbnN0IGJhZGdlPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd1cGRhdGUtYmFkZ2UnKTsKICAgIGlmKGJhZGdlJiZqc29uLmRhdGUpewogICAgICBjb25zdCBkPW5ldyBEYXRlKGpzb24uZGF0ZSk7CiAgICAgIGNvbnN0IHRkPW5ldyBEYXRlKCk7dGQuc2V0SG91cnMoMCwwLDAsMCk7CiAgICAgIGNvbnN0IGlzVD1kLnRvRGF0ZVN0cmluZygpPT09dGQudG9EYXRlU3RyaW5nKCk7CiAgICAgIGJhZGdlLmlubmVySFRNTD1pc1QKICAgICAgICA/JzxzcGFuIHN0eWxlPSJkaXNwbGF5OmlubGluZS1ibG9jazt3aWR0aDo3cHg7aGVpZ2h0OjdweDtiYWNrZ3JvdW5kOiMyZWNjNzE7Ym9yZGVyLXJhZGl1czo1MCU7bWFyZ2luLXJpZ2h0OjZweDthbmltYXRpb246YmxpbmsgMnMgaW5maW5pdGUiPjwvc3Bhbj7mnKzml6Xmm7TmlrDmuIjjgb8gwrcgRVbkuojmg7MnCiAgICAgICAgOmA8c3BhbiBzdHlsZT0iZGlzcGxheTppbmxpbmUtYmxvY2s7d2lkdGg6N3B4O2hlaWdodDo3cHg7YmFja2dyb3VuZDp2YXIoLS10ZXh0LW11dGUpO2JvcmRlci1yYWRpdXM6NTAlO21hcmdpbi1yaWdodDo2cHgiPjwvc3Bhbj4ke2QuZ2V0TW9udGgoKSsxfS8ke2QuZ2V0RGF0ZSgpfSDmm7TmlrAgwrcgRVbkuojmg7NgOwogICAgfQogIH0pLmNhdGNoKCgpPT57fSk7CgovLyDnhKHmlpnkuojmg7Poqq3jgb/ovrzjgb8KYXN5bmMgZnVuY3Rpb24gbG9hZEZyZWVQcmVkaWN0aW9ucygpIHsKICBjb25zdCBlbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdmcmVlLXByZWRpY3Rpb25zJyk7CiAgdHJ5IHsKICAgIGNvbnN0IHJlcyA9IGF3YWl0IGZldGNoKCdwdWJsaWNfcHJlZGljdGlvbnMuanNvbj92PScgKyBEYXRlLm5vdygpKTsKICAgIGlmICghcmVzLm9rKSB0aHJvdyBuZXcgRXJyb3IoJ25vdCBmb3VuZCcpOwogICAgY29uc3QganNvbiA9IGF3YWl0IHJlcy5qc29uKCk7CiAgICBjb25zdCBwcmVkcyA9IGpzb24ucHJlZGljdGlvbnMgfHwgW107CiAgICBjb25zdCB0b2RheSA9IG5ldyBEYXRlKCkudG9JU09TdHJpbmcoKS5zcGxpdCgnVCcpWzBdOwoKICAgIGlmICghcHJlZHMubGVuZ3RoIHx8IGpzb24uZGF0ZSAhPT0gdG9kYXkpIHsKICAgICAgZWwuaW5uZXJIVE1MID0gJzxkaXYgY2xhc3M9ImVtcHR5IiBzdHlsZT0icGFkZGluZzoyNHB4O2NvbG9yOnZhcigtLXRleHQtbXV0ZSk7Zm9udC1zaXplOi44MnJlbTt0ZXh0LWFsaWduOmNlbnRlciI+5pys5pel44Gu54Sh5paZ5LqI5oOz44Gv5rqW5YKZ5Lit44Gn44GZPC9kaXY+JzsKICAgICAgcmV0dXJuOwogICAgfQoKICAgIGNvbnN0IFNQT1JUX01FVEEgPSB7CiAgICAgIGhvcnNlOntpY29uOifwn5C0JyxsYWJlbDon56u26aasJyxjbHM6J2hvcnNlJ30sCiAgICAgIGJvYXQ6IHtpY29uOifwn5qkJyxsYWJlbDon56u26ImHJyxjbHM6J2JvYXQnfSwKICAgICAgY3ljbGU6e2ljb246J/CfmrQnLGxhYmVsOifnq7bovKonLGNsczonY3ljbGUnfQogICAgfTsKCiAgICBlbC5pbm5lckhUTUwgPSBwcmVkcy5tYXAocCA9PiB7CiAgICAgIGNvbnN0IG0gPSBTUE9SVF9NRVRBW3Auc3BvcnRdIHx8IHtpY29uOifwn4+BJyxsYWJlbDpwLnNwb3J0LGNsczonaG9yc2UnfTsKICAgICAgY29uc3QgZXZOdW0gPSBwYXJzZUZsb2F0KChwLmV2fHwnMCcpLnJlcGxhY2UoL1teMC05Li1dL2csJycpKTsKICAgICAgY29uc3QgZXZDb2xvciA9IGV2TnVtID49IDAgPyAnIzJlY2M3MScgOiAndmFyKC0tY3ljbGUpJzsKICAgICAgcmV0dXJuIGA8YSBjbGFzcz0icGljay1jYXJkICR7cC5zcG9ydH0iIGhyZWY9IiR7cC51cmx8fCcjJ30iIHN0eWxlPSJ0ZXh0LWRlY29yYXRpb246bm9uZSI+CiAgICAgICAgPGRpdiBjbGFzcz0icGljay1pbm5lciI+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJwaWNrLWxlZnQiPgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJwYy1tZXRhIj4KICAgICAgICAgICAgICA8c3BhbiBjbGFzcz0icGMtc3BvcnQgJHttLmNsc30iPiR7bS5pY29ufSAke20ubGFiZWx9PC9zcGFuPgogICAgICAgICAgICAgIDxzcGFuIGNsYXNzPSJwYy1yYWNlIj4ke3AudmVudWV8fCcnfTwvc3Bhbj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9InBjLW5hbWUiPiR7cC5uYW1lfHwnJ308L2Rpdj4KICAgICAgICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOi43OHJlbTtjb2xvcjp2YXIoLS10ZXh0LWRpbSk7bWFyZ2luLWJvdHRvbTo0cHgiPuKXjiAke3AuaG9ubWVpfHwn4oCUJ308L2Rpdj4KICAgICAgICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweCI+CiAgICAgICAgICAgICAgPHNwYW4gc3R5bGU9ImZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZToxLjJyZW07Y29sb3I6JHtldkNvbG9yfSI+JHtwLmV2fHwn4oCUJ308L3NwYW4+CiAgICAgICAgICAgICAgPHNwYW4gc3R5bGU9ImZvbnQtc2l6ZTouNjhyZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKSI+5pyf5b6F5YCkPC9zcGFuPgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgJHtwLnJlYXNvbiA/IGA8ZGl2IHN0eWxlPSJmb250LXNpemU6LjcycmVtO2NvbG9yOnZhcigtLXRleHQtbXV0ZSk7bWFyZ2luLXRvcDo0cHg7bGluZS1oZWlnaHQ6MS42Ij4ke3AucmVhc29ufTwvZGl2PmAgOiAnJ30KICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0icGMtcmlnaHQiPgogICAgICAgICAgICA8c3BhbiBjbGFzcz0icGMtYXJyIj7igLo8L3NwYW4+CiAgICAgICAgICAgIDxzcGFuIGNsYXNzPSJwYy1saW5rIj7oqbPntLDjgpLopovjgos8L3NwYW4+CiAgICAgICAgICA8L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgPC9hPmA7CiAgICB9KS5qb2luKCcnKTsKCiAgICAvLyBMSU5F6KqY5bCOCiAgICBlbC5pbm5lckhUTUwgKz0gYDxkaXYgc3R5bGU9ImJhY2tncm91bmQ6cmdiYSg2LDE5OSw4NSwuMDUpO2JvcmRlcjoxcHggc29saWQgcmdiYSg2LDE5OSw4NSwuMTUpO3BhZGRpbmc6MTJweCAxNnB4O2ZvbnQtc2l6ZTouNzVyZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKTtsaW5lLWhlaWdodDoxLjciPgogICAgICDwn5KsIOWFqOODrOODvOOCueOBruips+e0sOOBquiyt+OBhOebruODu0VW6KiI566X5qC55oug44GvPGEgaHJlZj0iaHR0cHM6Ly9saW5lLm1lL1IvdGkvcC9ANDE0aXJpa3giIHN0eWxlPSJjb2xvcjojMDZDNzU1O3RleHQtZGVjb3JhdGlvbjpub25lO2ZvbnQtd2VpZ2h0OjcwMCI+TElOReWFrOW8jzwvYT7jgafphY3kv6HkuK0KICAgIDwvZGl2PmA7CgogIH0gY2F0Y2ggewogICAgZWwuaW5uZXJIVE1MID0gJzxkaXYgY2xhc3M9ImVtcHR5IiBzdHlsZT0icGFkZGluZzoyNHB4O2NvbG9yOnZhcigtLXRleHQtbXV0ZSk7Zm9udC1zaXplOi44MnJlbTt0ZXh0LWFsaWduOmNlbnRlciI+5pys5pel44Gu54Sh5paZ5LqI5oOz44Gv5rqW5YKZ5Lit44Gn44GZPC9kaXY+JzsKICB9Cn0KbG9hZEZyZWVQcmVkaWN0aW9ucygpOwoKLy8g5a6f57i+44OH44O844K/6Kqt44G/6L6844G/CmFzeW5jIGZ1bmN0aW9uIGxvYWRTdGF0cygpIHsKICB0cnkgewogICAgY29uc3QgcmVzID0gYXdhaXQgZmV0Y2goJ3Jlc3VsdHMuanNvbj92PScgKyBEYXRlLm5vdygpKTsKICAgIGlmIChyZXMub2spIHsKICAgICAgY29uc3QganNvbiA9IGF3YWl0IHJlcy5qc29uKCk7CiAgICAgIGNvbnN0IHMgPSBqc29uLnN1bW1hcnk7CiAgICAgIGlmIChzKSB7CiAgICAgICAgY29uc3QgckVsID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2lkeC1yZWNvdmVyeScpOwogICAgICAgIHJFbC50ZXh0Q29udGVudCA9IHMucmVjb3ZlcnlfcmF0ZSArICclJzsKICAgICAgICByRWwuc3R5bGUuY29sb3IgPSBwYXJzZUZsb2F0KHMucmVjb3ZlcnlfcmF0ZSkgPj0gMTAwID8gJyMyZWNjNzEnIDogJ3ZhcigtLWdvbGQtbCknOwogICAgICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdpZHgtaGl0cmF0ZScpLnRleHRDb250ZW50ID0gcy5oaXRfcmF0ZSArICclJzsKICAgICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnaWR4LXRvdGFsJykudGV4dENvbnRlbnQgICA9IHMudG90YWw7CiAgICAgIH0KICAgICAgY29uc3QgYnMgPSBqc29uLmJ5X3Nwb3J0OwogICAgICBpZiAoYnMpIHsKICAgICAgICBmb3IgKGNvbnN0IHNwIG9mIFsnaG9yc2UnLCdib2F0JywnY3ljbGUnXSkgewogICAgICAgICAgY29uc3QgZCA9IGJzW3NwXTsgaWYgKCFkKSBjb250aW51ZTsKICAgICAgICAgIGNvbnN0IGhFbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZChgc3RhdC0ke3NwfS1oaXRgKTsKICAgICAgICAgIGNvbnN0IHJFbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZChgc3RhdC0ke3NwfS1yZWNgKTsKICAgICAgICAgIGlmKGhFbCl7aEVsLnRleHRDb250ZW50PWQuaGl0X3JhdGUrJyUnO2hFbC5zdHlsZS5jb2xvcj1wYXJzZUZsb2F0KGQuaGl0X3JhdGUpPj01MD8nIzJlY2M3MSc6JyNmZmYnO30KICAgICAgICAgIGlmKHJFbCl7ckVsLnRleHRDb250ZW50PWQucmVjb3ZlcnlfcmF0ZSsnJSc7ckVsLnN0eWxlLmNvbG9yPXBhcnNlRmxvYXQoZC5yZWNvdmVyeV9yYXRlKT49MTAwPycjMmVjYzcxJzondmFyKC0tZ29sZC1sKSc7fQogICAgICAgIH0KICAgICAgfQogICAgfQogIH0gY2F0Y2ggewogICAgdHJ5IHsKICAgICAgY29uc3QgciA9IGF3YWl0IHdpbmRvdy5zdG9yYWdlLmdldCgneW9zby1yZXN1bHRzJyk7CiAgICAgIGlmIChyKSB7CiAgICAgICAgY29uc3QgcmVjb3JkcyA9IEpTT04ucGFyc2Uoci52YWx1ZSk7CiAgICAgICAgY29uc3QgdG90YWwgPSByZWNvcmRzLmxlbmd0aDsKICAgICAgICBjb25zdCBoaXQgICA9IHJlY29yZHMuZmlsdGVyKHIgPT4gci5yZXN1bHQgPT09ICdoaXQnKS5sZW5ndGg7CiAgICAgICAgY29uc3QgYmV0ICAgPSByZWNvcmRzLnJlZHVjZSgocyxyKSA9PiBzICsgKHIuYmV0X2Ftb3VudHx8MCksIDApOwogICAgICAgIGNvbnN0IHJldCAgID0gcmVjb3Jkcy5yZWR1Y2UoKHMscikgPT4gcyArIChyLnJldHVybl9hbW91bnR8fDApLCAwKTsKICAgICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnaWR4LXJlY292ZXJ5JykudGV4dENvbnRlbnQgPSBiZXQgPyAocmV0L2JldCoxMDApLnRvRml4ZWQoMSkrJyUnIDogJy0tJSc7CiAgICAgICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2lkeC1oaXRyYXRlJykudGV4dENvbnRlbnQgID0gdG90YWwgPyAoaGl0L3RvdGFsKjEwMCkudG9GaXhlZCgxKSsnJScgOiAnLS0lJzsKICAgICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnaWR4LXRvdGFsJykudGV4dENvbnRlbnQgICAgPSB0b3RhbDsKICAgICAgfQogICAgfSBjYXRjaCB7fQogIH0KfQpsb2FkU3RhdHMoKTsKCi8vIFNOU+WFseaciQpjb25zdCBQQUdFX1VSTCA9IGVuY29kZVVSSUNvbXBvbmVudChsb2NhdGlvbi5ocmVmKTsKY29uc3QgWF9URVhUID0gZW5jb2RlVVJJQ29tcG9uZW50KCfjgJDkuojmg7Pjga7piYTliYfjgJHmnKzml6Xjga7nq7bppqzjg7vnq7boiYfjg7vnq7bovKrms6jnm67jg6zjg7zjgrnkuojmg7PjgpLlhazplovkuK3vvIFFVuioiOeul+OBp+agueaLoOOBguOCi+S6iOaDs+OCkuODgeOCp+ODg+OCr/CfkYcgI+ertummrOS6iOaDsyAj56u26ImH5LqI5oOzICPnq7bovKrkuojmg7MgI+S6iOaDs+OBrumJhOWJhycpOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc2hhcmUteCcpLmhyZWYgPSBgaHR0cHM6Ly90d2l0dGVyLmNvbS9pbnRlbnQvdHdlZXQ/dGV4dD0ke1hfVEVYVH0mdXJsPSR7UEFHRV9VUkx9YDsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3NoYXJlLWxpbmUnKS5ocmVmID0gYGh0dHBzOi8vc29jaWFsLXBsdWdpbnMubGluZS5tZS9saW5laXQvc2hhcmU/dXJsPSR7UEFHRV9VUkx9YDsKZnVuY3Rpb24gY29weVVSTCgpIHsKICBuYXZpZ2F0b3IuY2xpcGJvYXJkLndyaXRlVGV4dChsb2NhdGlvbi5ocmVmKS50aGVuKCgpID0+IHsKICAgIGNvbnN0IGJ0biA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzaGFyZS1jb3B5Jyk7CiAgICBidG4udGV4dENvbnRlbnQgPSAn4pyFIOOCs+ODlOODvOOBl+OBvuOBl+OBnyc7CiAgICBidG4uY2xhc3NMaXN0LmFkZCgnc2hhcmUtY29waWVkJyk7CiAgICBzZXRUaW1lb3V0KCgpID0+IHsgYnRuLnRleHRDb250ZW50ID0gJ/CflJcgVVJM44Kz44OU44O8JzsgYnRuLmNsYXNzTGlzdC5yZW1vdmUoJ3NoYXJlLWNvcGllZCcpOyB9LCAyMDAwKTsKICB9KTsKfQo8L3NjcmlwdD4KCjwhLS0gU0hBUkUgLS0+CjxkaXYgY2xhc3M9InNoYXJlLWJsb2NrIj4KICA8ZGl2IGNsYXNzPSJzaGFyZS10aXRsZSI+8J+ToyDku4rml6Xjga7kuojmg7PjgpLjgrfjgqfjgqLjgZnjgos8L2Rpdj4KICA8ZGl2IGNsYXNzPSJzaGFyZS1idXR0b25zIj4KICAgIDxhIGNsYXNzPSJzaGFyZS1idG4gc2hhcmUteCIgaWQ9InNoYXJlLXgiIGhyZWY9IiMiIHRhcmdldD0iX2JsYW5rIiByZWw9Im5vb3BlbmVyIj7wnZWPIFjjgafmipXnqL88L2E+CiAgICA8YSBjbGFzcz0ic2hhcmUtYnRuIHNoYXJlLWxpbmUiIGlkPSJzaGFyZS1saW5lIiBocmVmPSIjIiB0YXJnZXQ9Il9ibGFuayIgcmVsPSJub29wZW5lciI+TElORSDjgafpgIHjgos8L2E+CiAgICA8YSBjbGFzcz0ic2hhcmUtYnRuIHNoYXJlLWlnIiBocmVmPSJodHRwczovL3d3dy5pbnN0YWdyYW0uY29tLyIgdGFyZ2V0PSJfYmxhbmsiIHJlbD0ibm9vcGVuZXIiPvCfk7cgSW5zdGFncmFt44G4PC9hPgogICAgPGJ1dHRvbiBjbGFzcz0ic2hhcmUtYnRuIHNoYXJlLWNvcHkiIGlkPSJzaGFyZS1jb3B5IiBvbmNsaWNrPSJjb3B5VVJMKCkiPvCflJcgVVJM44Kz44OU44O8PC9idXR0b24+CiAgPC9kaXY+CjwvZGl2PgoKPC9ib2R5Pgo8L2h0bWw+Cg=="

def generate_index_html():
    import base64 as _b64
    try:
        html = _b64.b64decode(INDEX_HTML_B64).decode("utf-8")
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("✅ index.html生成完了")
        return True
    except Exception as err:
        print(f"⚠️ index.html生成エラー: {err}")
        return False

def upload_ftp_file(local_file, remote_path):
    """任意のファイルをFTPアップロード"""
    host     = (os.environ.get("FTP_HOST","") or "").strip().replace("\n","").replace("\r","").replace(" ","")
    user     = (os.environ.get("FTP_USER","") or "").strip().replace("\n","").replace("\r","").replace(" ","")
    password = (os.environ.get("FTP_PASS","") or "").strip().replace("\n","").replace("\r","")
    if not all([host, user, password]):
        return
    try:
        with ftplib.FTP(timeout=30) as ftp:
            ftp.connect(host, 21)
            ftp.login(user, password)
            ftp.set_pasv(True)
            dirs   = remote_path.split("/")
            fname  = dirs[-1]
            dpath  = "/".join(dirs[:-1])
            try:
                ftp.cwd(dpath)
            except:
                path = ""
                for d in dirs[:-1]:
                    if not d: continue
                    path += "/" + d
                    try: ftp.mkd(path)
                    except: pass
                ftp.cwd(dpath)
            with open(local_file, "rb") as f:
                ftp.storbinary(f"STOR {fname}", f)
            print(f"✅ FTPアップロード完了: {remote_path}")
    except Exception as e:
        print(f"⚠️ FTPエラー ({local_file}): {e}")


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

    # ④.5 public_predictions.json生成（index.htmlの無料予想セクション用）
    print("\n--- ④.5 public_predictions.json生成 ---")
    try:
        sport_icon = {"horse":"🐴","boat":"🚤","cycle":"🚴"}
        sport_name = {"horse":"競馬","boat":"競艇","cycle":"競輪"}
        predictions = []
        for r in all_races:
            if r.get("honmei") and r.get("judge") in ["強買い","買い"]:
                predictions.append({
                    "sport":    r["sport"],
                    "icon":     sport_icon.get(r["sport"],"🏁"),
                    "sport_name": sport_name.get(r["sport"],r["sport"]),
                    "venue":    r.get("venue",""),
                    "time":     r.get("time","--:--"),
                    "grade":    r.get("grade",""),
                    "honmei":   r.get("honmei",""),
                    "ev":       r.get("ev",""),
                    "judge":    r.get("judge",""),
                    "reason":   r.get("reason",""),
                    "url":      r.get("url","")
                })
        # EV順にソートし上位5件のみ公開
        def get_ev_num(r):
            try: return float(r.get("ev","").replace("%","").replace("+","")) / 100 + 1
            except: return 0.0
        predictions_sorted = sorted(predictions, key=get_ev_num, reverse=True)[:5]
        pub_pred = {"date": today_str, "predictions": predictions_sorted}
        with open("public_predictions.json","w",encoding="utf-8") as f:
            json.dump(pub_pred, f, ensure_ascii=True, indent=2)
        print(f"public_predictions.json生成完了（{len(predictions_sorted)}件）")
    except Exception as e:
        print(f"⚠️ public_predictions.json生成エラー: {e}")

    # ⑤ FTPアップロード
    print("\n--- ⑤ FTPアップロード ---")
    upload_ftp()

    # ⑤.5 public_predictions.json FTPアップロード
    if os.path.exists("public_predictions.json"):
        remote_base = os.environ.get("FTP_REMOTE",
            os.environ.get("FTP_REMOTE", os.environ.get("FTP_REMOTE_BASE", "/home/c9048134/public_html/oyatojikka.online") + "/races.json"))
        parts = [p for p in remote_base.split("/") if p]
        remote_dir = "/" + "/".join(parts[:-1])
        remote_pred = remote_dir + "/public_predictions.json"
        print("\n--- ⑤.5 public_predictions.json FTPアップロード ---")
        upload_ftp_file("public_predictions.json", remote_pred)

    # ⑥ index.html生成＋FTPアップロード
    generate_index_html()
    if os.path.exists("index.html"):
        remote_base = os.environ.get("FTP_REMOTE",
            os.environ.get("FTP_REMOTE", os.environ.get("FTP_REMOTE_BASE", "/home/c9048134/public_html/oyatojikka.online") + "/races.json"))
        # パスを確実に構築（先頭の/が消えないよう修正）
        parts = [p for p in remote_base.split("/") if p]
        remote_dir = "/" + "/".join(parts[:-1])
        remote_index = remote_dir + "/index.html"
        print("\n--- ⑦ index.html FTPアップロード ---")
        upload_ftp_file("index.html", remote_index)
    else:
        print("\n--- ⑦ index.html スキップ（ファイルなし）---")

    print(f"\n✅ 全処理完了（{len(all_races)}件）")
