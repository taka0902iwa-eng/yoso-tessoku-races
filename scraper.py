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


# ── 連勝式EV計算（ハーヴィル公式）────────────────
def calc_umaren_ev(probs, odds_map):
    """馬連のEV計算。probs: {馬番: 確率}, odds_map: {(i,j): オッズ}"""
    result = []
    keys = list(probs.keys())
    for i in range(len(keys)):
        for j in range(i+1, len(keys)):
            a, b = keys[i], keys[j]
            pa, pb = probs[a], probs[b]
            if pa + pb <= 0: continue
            # ハーヴィル公式: P(a1着,b2着) + P(b1着,a2着)
            p_ab = pa * (pb / (1 - pa)) if pa < 1 else 0
            p_ba = pb * (pa / (1 - pb)) if pb < 1 else 0
            hit_prob = p_ab + p_ba
            odds = odds_map.get((a,b), odds_map.get((b,a), 0))
            ev = round(hit_prob * odds, 4) if odds > 0 else 0.0
            result.append({"combo": f"{a}-{b}", "prob": round(hit_prob,4), "odds": odds, "ev": ev})
    return sorted(result, key=lambda x: x["ev"], reverse=True)

def calc_sanrenpuku_ev(probs, odds_map):
    """3連複のEV計算。probs: {馬番: 確率}, odds_map: {(i,j,k): オッズ}"""
    result = []
    keys = list(probs.keys())
    for i in range(len(keys)):
        for j in range(i+1, len(keys)):
            for k in range(j+1, len(keys)):
                a, b, c = keys[i], keys[j], keys[k]
                pa, pb, pc = probs[a], probs[b], probs[c]
                total = pa + pb + pc
                if total <= 0 or total >= 1: continue
                # 3連複的中確率（ハーヴィル近似）
                p = (pa*pb*(pc/(1-pa-pb)) + pa*pc*(pb/(1-pa-pc)) + pb*pc*(pa/(1-pb-pc))) / 3 * 6
                p = min(p, 1.0)
                odds = odds_map.get((a,b,c), 0)
                ev = round(p * odds, 4) if odds > 0 else 0.0
                result.append({"combo": f"{a}-{b}-{c}", "prob": round(p,4), "odds": odds, "ev": ev})
    return sorted(result, key=lambda x: x["ev"], reverse=True)

def build_horse_combo_summary(ev_results):
    """全出走馬のEV一覧と連勝式推奨買い目を生成する"""
    # 馬番→確率マップ
    probs = {}
    for h in ev_results:
        num = h.get("horse_num", h.get("frame_num", 0))
        if num:
            probs[int(num)] = h.get("prob", 0)

    # 馬連EV（デフォルトオッズなし → 確率のみ）
    umaren_list = []
    keys = sorted(probs.keys())
    for i in range(len(keys)):
        for j in range(i+1, len(keys)):
            a, b = keys[i], keys[j]
            pa, pb = probs[a], probs[b]
            if pa + pb <= 0 or pa >= 1 or pb >= 1: continue
            p_ab = pa * (pb / (1 - pa))
            p_ba = pb * (pa / (1 - pb))
            hit_prob = round(p_ab + p_ba, 4)
            umaren_list.append({"combo": f"{a}-{b}", "prob": hit_prob})
    umaren_top = sorted(umaren_list, key=lambda x: x["prob"], reverse=True)[:5]

    # 3連複EV
    sanrenpuku_list = []
    for i in range(len(keys)):
        for j in range(i+1, len(keys)):
            for k in range(j+1, len(keys)):
                a, b, c = keys[i], keys[j], keys[k]
                pa, pb, pc = probs.get(a,0), probs.get(b,0), probs.get(c,0)
                total = pa + pb + pc
                if total <= 0 or total >= 1: continue
                try:
                    p = (pa*pb*(pc/(1-pa-pb)) + pa*pc*(pb/(1-pa-pc)) + pb*pc*(pa/(1-pb-pc))) / 3 * 6
                    p = min(max(p, 0), 1.0)
                    sanrenpuku_list.append({"combo": f"{a}-{b}-{c}", "prob": round(p,4)})
                except ZeroDivisionError:
                    pass
    sanrenpuku_top = sorted(sanrenpuku_list, key=lambda x: x["prob"], reverse=True)[:3]

    return {
        "umaren_top": umaren_top,
        "sanrenpuku_top": sanrenpuku_top
    }

def build_horse_pace_summary(ev_results):
    """展開予想テキストを生成する（脚質分布から）"""
    style_counts = {}
    for h in ev_results:
        style = h.get("running_style", "先行")
        style_counts[style] = style_counts.get(style, 0) + 1

    escape = style_counts.get("逃げ", 0)
    front  = style_counts.get("先行", 0)
    diff   = style_counts.get("差し", 0)
    chase  = style_counts.get("追い込み", 0)

    if escape + front >= 5:
        pace_type = "ハイペース想定（前が多い）"
        advantage = "差し・追い込み有利"
    elif escape + front <= 2:
        pace_type = "スローペース想定（前が少ない）"
        advantage = "逃げ・先行有利"
    else:
        pace_type = "平均ペース想定"
        advantage = "展開は流動的"

    return f"{pace_type}｜{advantage}（逃{escape}先{front}差{diff}追{chase}）"


def calc_race_ev_horse(horses, history=None):
    if history is None: history = {"horse": []}
    scored = [{
"data": h, "score": calc_score_horse(h)} for h in horses]
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
    sorted_result = sorted(result, key=lambda x: x["ev"], reverse=True)

    # 全出走馬のEV一覧表（サイト表示用）
    all_horses_ev = [
        {
            "name":  h.get("name",""),
            "num":   h.get("horse_num", h.get("frame_num", 0)),
            "prob":  h.get("prob", 0),
            "odds":  h.get("odds", 0),
            "ev":    h.get("ev", 0),
            "judge": h.get("judge",""),
            "style": h.get("running_style",""),
            "jockey":h.get("jockey",""),
        }
        for h in sorted_result
    ]
    # 連勝式推奨買い目（確率ベース）
    combo_summary = build_horse_combo_summary(sorted_result)
    # 展開予想
    pace_summary = build_horse_pace_summary(sorted_result)

    # 先頭のレースにメタ情報を付加
    if sorted_result:
        sorted_result[0]["all_horses_ev"]   = all_horses_ev
        sorted_result[0]["combo_summary"]    = combo_summary
        sorted_result[0]["pace_summary"]     = pace_summary

    return sorted_result


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

    raw_score = role_rate * stability * market_edge * K * frame_adj * style_bank * trend_adj
    if stability < 0.3:
        raw_score *= 0.8
    return raw_score

def build_cycle_line_visual(ev_results):
    """競輪のライン構成をテキストで可視化する
    例: 「北日本ライン: 山田(先頭)→佐藤(番手) | 関東ライン: 田中(先頭)」
    """
    if not ev_results:
        return ""

    CIRCLES = "①②③④⑤⑥⑦⑧⑨"

    # ラインごとにまとめる
    lines_map = {}
    single    = []
    for r in ev_results:
        lg = r.get("line_group", "")
        if lg:
            if lg not in lines_map:
                lines_map[lg] = []
            lines_map[lg].append(r)
        else:
            single.append(r)

    parts = []
    for lg, members in lines_map.items():
        # 先頭→番手の順に並び替える
        ordered = sorted(members, key=lambda x: 0 if x.get("role") == "先頭" else 1)
        chain_parts = []
        for r in ordered:
            fn = int(r.get("frame_num", 1))
            circle = CIRCLES[fn-1] if 1 <= fn <= 9 else str(fn)
            role_str = ""
            if r.get("role") == "先頭":
                role_str = "(先頭)"
            elif r.get("role") == "番手":
                role_str = "(番手)"
            chain_parts.append(circle + r.get("name", "") + role_str)
        chain = "→".join(chain_parts)
        parts.append(f"{lg}: {chain}")

    for r in single:
        fn = int(r.get("frame_num", 1))
        circle = CIRCLES[fn-1] if 1 <= fn <= 9 else str(fn)
        parts.append(f"単騎: {circle}{r.get('name','')}")

    return " | ".join(parts)


def build_cycle_race_summary(ev_results):
    """競輪の展開予想テキストを生成する"""
    if not ev_results:
        return ""

    # 先頭選手の確率合計
    lead_prob   = sum(r.get("prob", 0) for r in ev_results if r.get("role") == "先頭")
    # 番手選手の確率合計
    second_prob = sum(r.get("prob", 0) for r in ev_results if r.get("role") == "番手")
    # 単騎選手の確率合計
    single_prob = sum(r.get("prob", 0) for r in ev_results if r.get("role") == "単騎")

    bt = ev_results[0].get("bank_type", 400) if ev_results else 400
    bank_note = "（333mバンク・先行有利）" if bt == 333 else ""

    # 展開パターン判定
    if lead_prob >= 0.50:
        pattern = f"先行有利展開{bank_note}"
    elif single_prob >= 0.40:
        pattern = "単騎有利展開（ライン戦崩れ注意）"
    elif second_prob >= 0.35:
        pattern = "番手マクリ有利展開"
    else:
        pattern = "流動展開（各ライン均衡）"

    top3 = sorted(ev_results, key=lambda x: x.get("prob", 0), reverse=True)[:3]
    top3_str = "・".join([
        f"{r.get('name','')}({r.get('role','')})→{int(r.get('prob',0)*100)}%"
        for r in top3
    ])
    return f"{pattern} | {top3_str}"


def calc_race_ev_cycle(riders, history=None):
    if history is None: history = {"cycle": []}

    # スコア計算
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

        # ── 先人の知恵を参考にした閾値設定 ──────────────────────────
        # 参考: たー坊（EV重視）・たけ@tipstar（ライン・競走得点重視）
        # 役割別EV閾値（先行選手が最も信頼度高い）
        if role == "先頭" and bt == 333:
            threshold_strong = 1.35   # 333mバンクは先行有利
            threshold_buy    = 1.15
        elif role == "先頭":
            threshold_strong = 1.40
            threshold_buy    = 1.20
        elif role == "番手":
            threshold_strong = 1.30
            threshold_buy    = 1.15
        else:  # 単騎
            threshold_strong = 1.50
            threshold_buy    = 1.30

        # EV異常値防止
        # - オッズ50倍超は計算精度が低い（先人: 高オッズは的中率低い）
        # - EV5.0超は計算バグの可能性（オッズ取得失敗によるダミー値）
        # - 勝率25%未満は見送り（先人: 実力差が明確なレースのみ狙う）
        # - オッズ未取得（odds==0）は見送り（ダミー値によるEV異常値防止）
        # バックテスト結果: EV1.00以上・勝率26%以上が最も回収率高（870%）
        if odds == 0:
            judge = "見送り"  # オッズ未取得は見送り
        elif odds > 50.0 or ev > 5.0 or prob < 0.25:
            judge = "見送り"
        else:
            judge = "強買い" if ev >= threshold_strong else "買い" if ev >= threshold_buy else "見送り"

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
    sorted_result = sorted(result, key=lambda x: x["ev"], reverse=True)

    # 全選手EV一覧（サイト表示用）
    all_riders_ev = [
        {
            "name":       r.get("name",""),
            "frame_num":  r.get("frame_num", 0),
            "prob":       r.get("prob", 0),
            "odds":       r.get("odds", 0),
            "ev":         r.get("ev", 0),
            "judge":      r.get("judge",""),
            "role":       r.get("role",""),
            "line_group": r.get("line_group",""),
            "score_rank": r.get("score_rank", 0),
        }
        for r in sorted_result
    ]

    # ライン構成テキスト可視化
    line_visual = build_cycle_line_visual(sorted_result)

    # 展開予想テキスト
    race_summary = build_cycle_race_summary(sorted_result)

    # 先頭のレコードにメタ情報を付加
    if sorted_result:
        sorted_result[0]["all_riders_ev"] = all_riders_ev
        sorted_result[0]["line_visual"]   = line_visual
        sorted_result[0]["race_summary"]  = race_summary

    return sorted_result


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

    # ── 日付フィルタリング: race_idの先頭8桁が今日の日付のものだけに絞る ──
    # race_id形式: YYYYMMDDCCRRXX → 先頭8桁=日付, CC=競馬場コード(01-10=JRA)
    today_ids = [rid for rid in unique_ids if len(rid) >= 8 and rid[:8] == today_ymd]
    if today_ids:
        unique_ids = today_ids
        print(f" 本日({today_ymd})のレースID: {len(unique_ids)}件（日付フィルタ適用）")
    else:
        print(f" 警告: 本日({today_ymd})のrace_idが0件。日付フィルタをスキップ")

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

        # 全出走馬一覧・連勝式・展開予想を先頭レコードから取り出す
        all_horses_ev  = ev_results[0].get("all_horses_ev", [])  if ev_results else []
        combo_summary  = ev_results[0].get("combo_summary", {})  if ev_results else {}
        pace_summary   = ev_results[0].get("pace_summary", "")   if ev_results else ""

        # 展開予想をreasonに組み込む
        reason_text = f"推定勝率{int(best['prob']*100)}%・EV{best['ev']:.2f}倍・{track_condition}" if best else ""
        if pace_summary:
            reason_text += f" / 展開: {pace_summary}"

        is_graded = grade in ["G1","G2","G3","重賞","JG1","JG2","JG3"]
        # 重賞は見送りでも本命・展開を表示するため judge を上書きしない
        # ただし is_graded フラグで index.html 側が「注目」表示を行う
        judge_val = best["judge"] if best else "見送り"
        honmei_val = best["name"] if best else (horses_data[0]["name"] if horses_data else "")
        ev_val = f"+{min(int((best['ev']-1)*100),999)}%" if best and best['ev'] > 1 else ""
        # 重賞で見送りの場合でも最有力馬を表示（EV計算上は見送りだが情報として提供）
        if is_graded and judge_val == "見送り" and horses_data:
            # EV最大の馬を本命として表示
            best_for_display = max(ev_results, key=lambda h: h.get("ev", 0)) if ev_results else None
            if best_for_display:
                honmei_val = best_for_display["name"]
                ev_val = f"+{min(int((best_for_display['ev']-1)*100),999)}%" if best_for_display['ev'] > 1 else ""
        return {
            "sport":         "horse",
            "name":          race_name,
            "venue":         venue,
            "time":          race_time,
            "grade":         grade,
            "is_graded":     is_graded,
            "url":           "keiba.html",
            "ev_detail":     ev_results[:5],
            "all_horses_ev": all_horses_ev,
            "combo_summary": combo_summary,
            "pace_summary":  pace_summary,
            "honmei":        honmei_val,
            "ev":            ev_val,
            "judge":         judge_val,
            "reason":        reason_text
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
                # 日付フィルタリング: 今日のrace_idのみに絞る
                today_ids_fb = [rid for rid in ids if len(rid) >= 8 and rid[:8] == today_ymd]
                if today_ids_fb:
                    ids = today_ids_fb
                    print(f" [horse/fallback] 日付フィルタ適用: {len(ids)}件")
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

# ── 競艇 全艦券種期待値計算（2連単・3連単）────────────────
def calc_boat_2rentan_ev(probs):
    """
    2連単の的中確率をハーヴィル公式で計算する。
    probs: {1: 0.35, 2: 0.20, ...} などの艦番→確率マップ
    返り値: [{combo, prob}] 上位10件
    """
    result = []
    keys = list(probs.keys())
    for i in range(len(keys)):
        for j in range(len(keys)):
            if i == j: continue
            a, b = keys[i], keys[j]
            pa, pb = probs[a], probs[b]
            if pa <= 0 or pb <= 0 or pa >= 1: continue
            # P(aが1着, bが2着) = pa * pb/(1-pa)
            p = pa * (pb / (1 - pa))
            result.append({"combo": f"{a}-{b}", "prob": round(p, 4)})
    return sorted(result, key=lambda x: x["prob"], reverse=True)[:10]

def calc_boat_3rentan_ev(probs):
    """
    3連単の的中確率をハーヴィル公式で計算する。
    返り値: [{combo, prob}] 上位10件
    """
    result = []
    keys = list(probs.keys())
    for i in range(len(keys)):
        for j in range(len(keys)):
            if i == j: continue
            for k in range(len(keys)):
                if k == i or k == j: continue
                a, b, c = keys[i], keys[j], keys[k]
                pa, pb, pc = probs[a], probs[b], probs[c]
                if pa <= 0 or pb <= 0 or pc <= 0: continue
                if pa >= 1 or (pa + pb) >= 1: continue
                # P(a1着, b2着, c3着) = pa * pb/(1-pa) * pc/(1-pa-pb)
                try:
                    p = pa * (pb / (1 - pa)) * (pc / (1 - pa - pb))
                    result.append({"combo": f"{a}-{b}-{c}", "prob": round(p, 5)})
                except ZeroDivisionError:
                    pass
    return sorted(result, key=lambda x: x["prob"], reverse=True)[:10]

def build_boat_combo_summary(ev_results):
    """競艇全選手のEV一覧と全艦券種推奨買い目を生成する"""
    probs = {}
    for r in ev_results:
        bn = int(float(r.get("frame_num", 0)))
        if bn:
            probs[bn] = r.get("prob", 0)

    rentan_2 = calc_boat_2rentan_ev(probs)
    rentan_3 = calc_boat_3rentan_ev(probs)

    return {
        "rentan_2_top": rentan_2[:5],
        "rentan_3_top": rentan_3[:5]
    }

def build_boat_race_summary(ev_results, venue_name=""):
    """競艇の展開予想テキストを生成する（コース別確率から）"""
    # 1号艦の確率が高い場合は「逃げ有利」、低い場合は「差し・まくり有利」
    if not ev_results:
        return ""
    sorted_by_course = sorted(ev_results, key=lambda x: float(x.get("frame_num",1)))
    probs_by_course  = [r.get("prob",0) for r in sorted_by_course]

    p1 = probs_by_course[0] if len(probs_by_course) > 0 else 0
    p2 = probs_by_course[1] if len(probs_by_course) > 1 else 0
    p3 = probs_by_course[2] if len(probs_by_course) > 2 else 0

    if p1 >= 0.40:
        pattern = "逃げ有利展開（1号艦が強い）"
    elif p2 + p3 >= 0.40:
        pattern = "差し・まくり展開（2・3号艦が強い）"
    elif p1 + p2 >= 0.55:
        pattern = "内果中心展開（1・2号艦有利）"
    else:
        pattern = "流動展開（各艦均衡）"

    top3 = sorted(ev_results, key=lambda x: x.get("prob",0), reverse=True)[:3]
    top3_str = "・".join([f"{int(float(r.get('frame_num',0)))}号{r.get('name','')}→{int(r.get('prob',0)*100)}%" for r in top3])
    return f"{pattern} | {top3_str}"


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

    sorted_result = sorted(result, key=lambda x: x["ev"] if x.get("real_odds") else x["prob"], reverse=True)

    # 全選手一覧表（サイト表示用）
    all_riders_ev = [
        {
            "name":      r.get("name",""),
            "frame_num": int(float(r.get("frame_num",0))),
            "prob":      r.get("prob",0),
            "odds":      r.get("odds",0),
            "ev":        r.get("ev",0),
            "judge":     r.get("judge",""),
            "win_rate":  r.get("win_rate",0),
            "motor_win_rate": r.get("motor_win_rate",0),
            "exhibition_time": r.get("exhibition_time",0),
        }
        for r in sorted_result
    ]
    # 全艦券種推奨買い目
    combo_summary = build_boat_combo_summary(sorted_result)
    # 展開予想
    race_summary  = build_boat_race_summary(sorted_result, venue_name)

    # 先頭のレコードにメタ情報を付加
    if sorted_result:
        sorted_result[0]["all_riders_ev"]  = all_riders_ev
        sorted_result[0]["combo_summary"]   = combo_summary
        sorted_result[0]["race_summary"]    = race_summary

    return sorted_result

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

                ev_results = calc_boat_race_ev_v2(riders, venue) if riders else []
                best       = next((r for r in ev_results if r["judge"] in ["強買い","買い"]),
                                  ev_results[0] if ev_results else None)

                # 全選手一覧・連勝式・展開予想を先頭レコードから取り出す
                all_riders_ev = ev_results[0].get("all_riders_ev", []) if ev_results else []
                combo_summary = ev_results[0].get("combo_summary", {}) if ev_results else {}
                race_summary  = ev_results[0].get("race_summary", "")  if ev_results else ""

                race = {
                    "sport": "boat",
                    "name":  f"{venue} {rno}R",
                    "venue": venue,
                    "time":  t,
                    "grade": grade,
                    "url":   "kyotei.html",
                    "all_riders_ev": all_riders_ev,
                    "combo_summary": combo_summary,
                    "race_summary":  race_summary,
                }
                if best and best.get("judge") in ["強買い", "買い"]:
                    reason_text = f"推定勝率{int(best['prob']*100)}%・EV{best['ev']:.2f}倍・{int(best.get('frame_num',1))}号艇"
                    if race_summary:
                        reason_text += f" / 展開: {race_summary.split('|')[0].strip()}"
                    race.update({
                        "honmei":    best.get("name",""),
                        "ev":        f"+{min(int((best['ev']-1)*100), 999)}%" if best and best['ev']>1 else "",
                        "judge":     best["judge"],
                        "reason":    reason_text,
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

                # 全選手一覧・ライン可視化・展開予想を先頭レコードから取り出す
                all_riders_ev = ev_results[0].get("all_riders_ev", []) if ev_results else []
                line_visual   = ev_results[0].get("line_visual", "")   if ev_results else ""
                race_summary  = ev_results[0].get("race_summary", "")  if ev_results else ""

                race = {
                    "sport":         "cycle",
                    "name":          f"{venue_name} 注目レース",
                    "venue":         venue_name,
                    "time":          t,
                    "grade":         grade,
                    "url":           "keirin.html",
                    "all_riders_ev": all_riders_ev,
                    "line_visual":   line_visual,
                    "race_summary":  race_summary,
                }
                if best and best.get("judge") in ["強買い", "買い"]:
                    reason_text = f"推定勝率{int(best['prob']*100)}%・EV{best['ev']:.2f}倍"
                    if race_summary:
                        reason_text += f" / 展開: {race_summary.split('|')[0].strip()}"
                    race.update({
                        "honmei":    best.get("name","予想公開中"),
                        "ev":        f"+{int((best['ev']-1)*100)}%" if best.get('ev',0)>1 else "",
                        "judge":     best["judge"],
                        "reason":    reason_text,
                        "ev_detail": ev_results[:9],
                    })
                elif best:
                    # 強買い・買いなしの場合は見送り表示
                    race.update({
                        "honmei":    "",
                        "ev":        "",
                        "judge":     "見送り",
                        "reason":    "本日は推奨レースなし",
                        "ev_detail": ev_results[:9],
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
            odds  = odds_list[i] if i < len(odds_list) else 0.0  # オッズ未取得時は0（EV計算を無効化）
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
                    odds      = valid_odds[i]  if i < len(valid_odds) else 0.0  # オッズ未取得時は0（EV計算を無効化）
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

            # 展開予想を追加（競馬: pace_summary, 競艇: race_summary, 競輪: race_summary）
            summary = r.get("pace_summary") or r.get("race_summary","")
            if summary:
                # 長すぎる場合は先頭部分のみ
                summary_short = summary.split("|")[0].strip()[:30]
                line += f"\n展開: {summary_short}"

            # 競馬: 馬連推奨買い目を追加
            if r.get("sport") == "horse":
                combo = r.get("combo_summary", {})
                umaren = combo.get("umaren_top", [])
                if umaren:
                    top2 = umaren[:2]
                    line += f"\n馬連推奨: {', '.join([u['combo'] for u in top2])}"

            # 競艇: 2連単推奨買い目を追加
            if r.get("sport") == "boat":
                combo = r.get("combo_summary", {})
                rentan2 = combo.get("rentan_2_top", [])
                if rentan2:
                    top2 = rentan2[:2]
                    line += f"\n2連単推奨: {', '.join([u['combo'] for u in top2])}"

            # 競輪: ライン構成を追加
            if r.get("sport") == "cycle":
                lv = r.get("line_visual","")
                if lv:
                    line += f"\nライン: {lv[:40]}"

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
        print("FTPアップロード失敗。処理を続行します。")


# ══════════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════════

# ══════════════════════════════════════════════════
# index.html 自動生成（base64）
# ══════════════════════════════════════════════════
INDEX_HTML_B64 = "PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImphIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLjAiPgo8bWV0YSBuYW1lPSJkZXNjcmlwdGlvbiIgY29udGVudD0i56u26aas44O756u26ImH44O756u26Lyq44Gu5pys5ZG95LqI5oOz44KS5pyf5b6F5YCk77yIRVbvvInph43oppbjgafmr47ml6XlhazplovjgILjg4fjg7zjgr/jgavln7rjgaXjgYTjgZ/moLnmi6DjgYLjgovkuojmg7PjgafplbfmnJ/lj47mlK/jg5fjg6njgrnjgpLnm67mjIfjgZfjgb7jgZnjgILnhKHmlpnjgadMSU5F6YWN5L+h44KC5Y+X44GR5Y+W44KM44G+44GZ44CCIj4KPG1ldGEgbmFtZT0icm9ib3RzIiBjb250ZW50PSJpbmRleCxmb2xsb3ciPgo8bWV0YSBwcm9wZXJ0eT0ib2c6dHlwZSIgY29udGVudD0id2Vic2l0ZSI+CjxtZXRhIHByb3BlcnR5PSJvZzp0aXRsZSIgY29udGVudD0i5LqI5oOz44Gu6YmE5YmHIHwg56u26aas44O756u26ImH44O756u26LyqIOacrOWRveS6iOaDsyI+CjxtZXRhIHByb3BlcnR5PSJvZzpkZXNjcmlwdGlvbiIgY29udGVudD0i56u26aas44O756u26ImH44O756u26Lyq44Gu5pys5ZG95LqI5oOz44KS5pyf5b6F5YCk77yIRVbvvInph43oppbjgafmr47ml6XlhazplovjgILjg4fjg7zjgr/jgavln7rjgaXjgYTjgZ/moLnmi6DjgYLjgovkuojmg7PjgafplbfmnJ/lj47mlK/jg5fjg6njgrnjgpLnm67mjIfjgZfjgb7jgZnjgIIiPgo8bWV0YSBwcm9wZXJ0eT0ib2c6dXJsIiBjb250ZW50PSJodHRwczovL295YXRvamlra2Eub25saW5lLyI+CjxtZXRhIHByb3BlcnR5PSJvZzpzaXRlX25hbWUiIGNvbnRlbnQ9IuS6iOaDs+OBrumJhOWJhyI+CjxtZXRhIHByb3BlcnR5PSJvZzppbWFnZSIgY29udGVudD0iaHR0cHM6Ly9veWF0b2ppa2thLm9ubGluZS9hdmF0YXIucG5nIj4KPG1ldGEgcHJvcGVydHk9Im9nOmxvY2FsZSIgY29udGVudD0iamFfSlAiPgo8bWV0YSBuYW1lPSJ0d2l0dGVyOmNhcmQiIGNvbnRlbnQ9InN1bW1hcnkiPgo8bWV0YSBuYW1lPSJ0d2l0dGVyOnRpdGxlIiBjb250ZW50PSLkuojmg7Pjga7piYTliYcgfCDnq7bppqzjg7vnq7boiYfjg7vnq7bovKog5pys5ZG95LqI5oOzIj4KPG1ldGEgbmFtZT0idHdpdHRlcjpkZXNjcmlwdGlvbiIgY29udGVudD0i56u26aas44O756u26ImH44O756u26Lyq44Gu5pys5ZG95LqI5oOz44KS5pyf5b6F5YCk77yIRVbvvInph43oppbjgafmr47ml6XlhazplovjgIIiPgo8bWV0YSBuYW1lPSJ0d2l0dGVyOmltYWdlIiBjb250ZW50PSJodHRwczovL295YXRvamlra2Eub25saW5lL2F2YXRhci5wbmciPgo8dGl0bGU+5LqI5oOz44Gu6YmE5YmHIHwg56u26aas44O756u26ImH44O756u26LyqIOacrOWRveS6iOaDszwvdGl0bGU+CjxsaW5rIHJlbD0icHJlY29ubmVjdCIgaHJlZj0iaHR0cHM6Ly9mb250cy5nb29nbGVhcGlzLmNvbSI+CjxsaW5rIHJlbD0icHJlY29ubmVjdCIgaHJlZj0iaHR0cHM6Ly9mb250cy5nc3RhdGljLmNvbSIgY3Jvc3NvcmlnaW4+CjxsaW5rIGhyZWY9Imh0dHBzOi8vZm9udHMuZ29vZ2xlYXBpcy5jb20vY3NzMj9mYW1pbHk9Tm90bytTZXJpZitKUDp3Z2h0QDcwMDs5MDAmZmFtaWx5PUJlYmFzK05ldWUmZmFtaWx5PU5vdG8rU2FucytKUDp3Z2h0QDMwMDs0MDA7NzAwJmRpc3BsYXk9c3dhcCIgcmVsPSJzdHlsZXNoZWV0Ij4KPHN0eWxlPgo6cm9vdCB7CiAgLS1iZzogICAgICAgICMwODBjMTQ7CiAgLS1iZy1jYXJkOiAgICMwYzE1MjU7CiAgLS1iZy1jYXJkMjogICMwZjFlMzY7CiAgLS1uYXZ5OiAgICAgICMwYTE2Mjg7CiAgLS1nb2xkOiAgICAgICNjOWE4NGM7CiAgLS1nb2xkLWw6ICAgICNmMGQwODA7CiAgLS1nb2xkLWRpbTogIHJnYmEoMjAxLDE2OCw3NiwwLjE1KTsKICAtLWJvcmRlcjogICAgcmdiYSgyMDEsMTY4LDc2LDAuMTgpOwogIC0taG9yc2U6ICAgICAjYzlhODRjOwogIC0taG9yc2UtYmc6ICByZ2JhKDIwMSwxNjgsNzYsMC4xMCk7CiAgLS1ob3JzZS1iZDogIHJnYmEoMjAxLDE2OCw3NiwwLjMwKTsKICAtLWJvYXQ6ICAgICAgIzAwYjRkODsKICAtLWJvYXQtYmc6ICAgcmdiYSgwLDE4MCwyMTYsMC4xMCk7CiAgLS1ib2F0LWJkOiAgIHJnYmEoMCwxODAsMjE2LDAuMzApOwogIC0tY3ljbGU6ICAgICAjZTgzMTNhOwogIC0tY3ljbGUtYmc6ICByZ2JhKDIzMiw0OSw1OCwwLjEwKTsKICAtLWN5Y2xlLWJkOiAgcmdiYSgyMzIsNDksNTgsMC4zMCk7CiAgLS1ncmVlbjogICAgICMyZWNjNzE7CiAgLS10ZXh0OiAgICAgICNlZWYwZjU7CiAgLS10ZXh0LWRpbTogICNhOGIwYzQ7CiAgLS10ZXh0LW11dGU6ICM1YTYyNzg7Cn0KKiwqOjpiZWZvcmUsKjo6YWZ0ZXJ7Ym94LXNpemluZzpib3JkZXItYm94O21hcmdpbjowO3BhZGRpbmc6MH0KYXtjb2xvcjppbmhlcml0O3RleHQtZGVjb3JhdGlvbjpub25lfQpodG1se3Njcm9sbC1iZWhhdmlvcjpzbW9vdGh9CmJvZHl7YmFja2dyb3VuZDp2YXIoLS1iZyk7Y29sb3I6dmFyKC0tdGV4dCk7Zm9udC1mYW1pbHk6J05vdG8gU2FucyBKUCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDA7bWluLWhlaWdodDoxMDB2aH0KOjotd2Via2l0LXNjcm9sbGJhcnt3aWR0aDozcHh9Cjo6LXdlYmtpdC1zY3JvbGxiYXItdHJhY2t7YmFja2dyb3VuZDp2YXIoLS1iZyl9Cjo6LXdlYmtpdC1zY3JvbGxiYXItdGh1bWJ7YmFja2dyb3VuZDp2YXIoLS1nb2xkKTtib3JkZXItcmFkaXVzOjJweH0KCi8qIOKVkOKVkCBIRUFERVIg4pWQ4pWQICovCmhlYWRlcnsKICBwb3NpdGlvbjpzdGlja3k7dG9wOjA7ei1pbmRleDoyMDA7CiAgYmFja2dyb3VuZDpyZ2JhKDgsMTIsMjAsMC45Nik7CiAgYm9yZGVyLWJvdHRvbToxcHggc29saWQgdmFyKC0tYm9yZGVyKTsKICBiYWNrZHJvcC1maWx0ZXI6Ymx1cigxMnB4KTsKICBwYWRkaW5nOjAgMjBweDtoZWlnaHQ6NTZweDsKICBkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuOwp9Ci5sb2dve2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpiYXNlbGluZTtnYXA6MTBweDt0ZXh0LWRlY29yYXRpb246bm9uZX0KLmxvZ28tamF7Zm9udC1mYW1pbHk6J05vdG8gU2VyaWYgSlAnLHNlcmlmO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MS4ycmVtO2NvbG9yOnZhcigtLWdvbGQtbCk7bGV0dGVyLXNwYWNpbmc6LjA2ZW07dGV4dC1zaGFkb3c6MCAwIDIwcHggcmdiYSgyMDEsMTY4LDc2LC4zKX0KLmxvZ28tZW57Zm9udC1mYW1pbHk6J0JlYmFzIE5ldWUnLHNhbnMtc2VyaWY7Zm9udC1zaXplOi43MnJlbTtjb2xvcjp2YXIoLS10ZXh0LW11dGUpO2xldHRlci1zcGFjaW5nOi4yMmVtfQpuYXYgYXtjb2xvcjp2YXIoLS10ZXh0LWRpbSk7dGV4dC1kZWNvcmF0aW9uOm5vbmU7Zm9udC1zaXplOi43NXJlbTtwYWRkaW5nOjZweCAxMnB4O3RyYW5zaXRpb246Y29sb3IgLjJzfQpuYXYgYTpob3Zlcntjb2xvcjp2YXIoLS1nb2xkLWwpfQoKLyog4pWQ4pWQIEhFUk8g4pWQ4pWQICovCi5oZXJvewogIHBvc2l0aW9uOnJlbGF0aXZlOwogIGJhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE2MGRlZywjMGQxYTJlIDAlLCMwODBjMTQgNTUlLCMwYTBjMTAgMTAwJSk7CiAgcGFkZGluZzo1MHB4IDIwcHggMDsKICBvdmVyZmxvdzpoaWRkZW47Cn0KLmhlcm86OmJlZm9yZXsKICBjb250ZW50OicnO3Bvc2l0aW9uOmFic29sdXRlO2luc2V0OjA7CiAgYmFja2dyb3VuZDpyYWRpYWwtZ3JhZGllbnQoZWxsaXBzZSA2MCUgNTAlIGF0IDUwJSAwJSxyZ2JhKDIwMSwxNjgsNzYsLjA2KSAwJSx0cmFuc3BhcmVudCA3MCUpOwogIHBvaW50ZXItZXZlbnRzOm5vbmU7Cn0KLmhlcm8tdGV4dHsKICBwb3NpdGlvbjpyZWxhdGl2ZTt6LWluZGV4OjI7CiAgdGV4dC1hbGlnbjpjZW50ZXI7bWF4LXdpZHRoOjcwMHB4O21hcmdpbjowIGF1dG87CiAgcGFkZGluZy1ib3R0b206MzZweDsKfQouaGVyby1iYWRnZXsKICBkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4OwogIHBhZGRpbmc6NHB4IDE2cHg7bWFyZ2luLWJvdHRvbToyMHB4OwogIGJvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4zNSk7Ym9yZGVyLXJhZGl1czoxcHg7CiAgZm9udC1zaXplOi42OHJlbTtsZXR0ZXItc3BhY2luZzouMmVtO2NvbG9yOnZhcigtLWdvbGQpOwogIGJhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4wNSk7Cn0KLmhlcm8tYmFkZ2U6OmJlZm9yZXtjb250ZW50OicnO3dpZHRoOjVweDtoZWlnaHQ6NXB4O2JhY2tncm91bmQ6dmFyKC0tY3ljbGUpO2JvcmRlci1yYWRpdXM6NTAlO2JveC1zaGFkb3c6MCAwIDhweCB2YXIoLS1jeWNsZSk7YW5pbWF0aW9uOmJsaW5rIDJzIGluZmluaXRlfQpAa2V5ZnJhbWVzIGJsaW5rezAlLDEwMCV7b3BhY2l0eToxfTUwJXtvcGFjaXR5Oi4yfX0KLmhlcm8tdGl0bGV7CiAgZm9udC1mYW1pbHk6J05vdG8gU2VyaWYgSlAnLHNlcmlmO2ZvbnQtd2VpZ2h0OjkwMDsKICBmb250LXNpemU6Y2xhbXAoMnJlbSw3dncsMy42cmVtKTsKICBsaW5lLWhlaWdodDoxLjE1O2xldHRlci1zcGFjaW5nOi4wNGVtO2NvbG9yOiNmZmY7CiAgdGV4dC1zaGFkb3c6MCAwIDQwcHggcmdiYSgyMDEsMTY4LDc2LC4xNSk7CiAgbWFyZ2luLWJvdHRvbToxMHB4Owp9Ci5oZXJvLXRpdGxlIGVte2NvbG9yOnZhcigtLWdvbGQtbCk7Zm9udC1zdHlsZTpub3JtYWx9Ci5oZXJvLXN1YnsKICBmb250LWZhbWlseTonQmViYXMgTmV1ZScsc2Fucy1zZXJpZjsKICBmb250LXNpemU6Y2xhbXAoLjhyZW0sMnZ3LDFyZW0pOwogIGxldHRlci1zcGFjaW5nOi4zNWVtO2NvbG9yOnZhcigtLXRleHQtbXV0ZSk7CiAgbWFyZ2luLWJvdHRvbToyNHB4Owp9Ci5oZXJvLWN0YXsKICBkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweDsKICBwYWRkaW5nOjE0cHggMjhweDsKICBiYWNrZ3JvdW5kOiMwNkM3NTU7Y29sb3I6I2ZmZjsKICBmb250LWZhbWlseTonTm90byBTYW5zIEpQJyxzYW5zLXNlcmlmO2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6Ljk1cmVtOwogIHRleHQtZGVjb3JhdGlvbjpub25lO2JvcmRlci1yYWRpdXM6NHB4O2xldHRlci1zcGFjaW5nOi4wNGVtOwogIGJveC1zaGFkb3c6MCA0cHggMjRweCByZ2JhKDYsMTk5LDg1LC4zNSk7dHJhbnNpdGlvbjpmaWx0ZXIgLjI1czsKfQouaGVyby1jdGE6aG92ZXJ7ZmlsdGVyOmJyaWdodG5lc3MoMS4xKX0KLmhlcm8tY3RhLW5vdGV7Zm9udC1zaXplOi42NnJlbTtjb2xvcjp2YXIoLS10ZXh0LW11dGUpO21hcmdpbi10b3A6OHB4fQoKLyog4pWQ4pWQIOertuaKgOOCv+ODliDilZDilZAgKi8KLnNwb3J0LXRhYnN7CiAgcG9zaXRpb246cmVsYXRpdmU7ei1pbmRleDoyOwogIGRpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDMsMWZyKTsKICBib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1ib3JkZXIpOwogIG1hcmdpbjowIC0yMHB4Owp9Ci5zcG9ydC10YWJ7CiAgcGFkZGluZzoxNnB4IDEycHg7dGV4dC1hbGlnbjpjZW50ZXI7CiAgdGV4dC1kZWNvcmF0aW9uOm5vbmU7CiAgYm9yZGVyLXJpZ2h0OjFweCBzb2xpZCB2YXIoLS1ib3JkZXIpOwogIHRyYW5zaXRpb246YmFja2dyb3VuZCAuMnM7CiAgcG9zaXRpb246cmVsYXRpdmU7Cn0KLnNwb3J0LXRhYjpsYXN0LWNoaWxke2JvcmRlci1yaWdodDpub25lfQouc3BvcnQtdGFiOjphZnRlcnsKICBjb250ZW50OicnO3Bvc2l0aW9uOmFic29sdXRlO2JvdHRvbTowO2xlZnQ6MDtyaWdodDowO2hlaWdodDozcHg7CiAgdHJhbnNpdGlvbjpvcGFjaXR5IC4ycztvcGFjaXR5OjA7Cn0KLnNwb3J0LXRhYi5ob3JzZTo6YWZ0ZXJ7YmFja2dyb3VuZDp2YXIoLS1ob3JzZSl9Ci5zcG9ydC10YWIuYm9hdDo6YWZ0ZXIge2JhY2tncm91bmQ6dmFyKC0tYm9hdCl9Ci5zcG9ydC10YWIuY3ljbGU6OmFmdGVye2JhY2tncm91bmQ6dmFyKC0tY3ljbGUpfQouc3BvcnQtdGFiOmhvdmVyOjphZnRlcntvcGFjaXR5OjF9Ci5zcG9ydC10YWI6aG92ZXJ7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMyl9Ci5zdC1pY29ue2ZvbnQtc2l6ZToxLjRyZW07ZGlzcGxheTpibG9jazttYXJnaW4tYm90dG9tOjRweH0KLnN0LWxhYmVse2ZvbnQtc2l6ZTouNzJyZW07Zm9udC13ZWlnaHQ6NzAwO2xldHRlci1zcGFjaW5nOi4wOGVtfQouc3BvcnQtdGFiLmhvcnNlIC5zdC1sYWJlbHtjb2xvcjp2YXIoLS1ob3JzZSl9Ci5zcG9ydC10YWIuYm9hdCAgLnN0LWxhYmVse2NvbG9yOnZhcigtLWJvYXQpfQouc3BvcnQtdGFiLmN5Y2xlIC5zdC1sYWJlbHtjb2xvcjp2YXIoLS1jeWNsZSl9Ci5zdC1jb3VudHsKICBmb250LWZhbWlseTonQmViYXMgTmV1ZScsc2Fucy1zZXJpZjtmb250LXNpemU6Ljg1cmVtOwogIGNvbG9yOnZhcigtLXRleHQtbXV0ZSk7bWFyZ2luLXRvcDoycHg7Cn0KCi8qIOKVkOKVkCBNQUlOIOKVkOKVkCAqLwoubWFpbnttYXgtd2lkdGg6OTAwcHg7bWFyZ2luOjAgYXV0bztwYWRkaW5nOjAgMCA4MHB4fQoKLyog44K744Kv44K344On44Oz5YWx6YCaICovCi5zZWN7cGFkZGluZzoyOHB4IDIwcHggMH0KLnNlYy1oZWFke2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7bWFyZ2luLWJvdHRvbToxNnB4fQouc2VjLWxpbmV7d2lkdGg6M3B4O2hlaWdodDoxNnB4O2JvcmRlci1yYWRpdXM6MnB4O2ZsZXgtc2hyaW5rOjB9Ci5zZWMtbGluZS5nb2xke2JhY2tncm91bmQ6dmFyKC0tZ29sZCl9Ci5zZWMtbGluZS5ob3JzZXtiYWNrZ3JvdW5kOnZhcigtLWhvcnNlKX0KLnNlYy1saW5lLmJvYXR7YmFja2dyb3VuZDp2YXIoLS1ib2F0KX0KLnNlYy1saW5lLmN5Y2xle2JhY2tncm91bmQ6dmFyKC0tY3ljbGUpfQouc2VjLXRpdGxle2ZvbnQtc2l6ZTouNzhyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07Zm9udC13ZWlnaHQ6NzAwfQouc2VjLXRpdGxlLmdvbGR7Y29sb3I6dmFyKC0tZ29sZCl9Ci5zZWMtdGl0bGUuaG9yc2V7Y29sb3I6dmFyKC0taG9yc2UpfQouc2VjLXRpdGxlLmJvYXR7Y29sb3I6dmFyKC0tYm9hdCl9Ci5zZWMtdGl0bGUuY3ljbGV7Y29sb3I6dmFyKC0tY3ljbGUpfQouc2VjLWJhZGdlewogIGZvbnQtc2l6ZTouNnJlbTtwYWRkaW5nOjJweCA4cHg7Ym9yZGVyLXJhZGl1czoxMHB4OwogIGZvbnQtd2VpZ2h0OjcwMDtsZXR0ZXItc3BhY2luZzouMDVlbTttYXJnaW4tbGVmdDphdXRvOwp9Ci5zZWMtYmFkZ2UubGl2ZXtiYWNrZ3JvdW5kOnJnYmEoNDYsMjA0LDExMywuMTUpO2NvbG9yOnZhcigtLWdyZWVuKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoNDYsMjA0LDExMywuMyl9CgovKiDilZDilZAg5a6f57i+44OQ44OK44O8IOKVkOKVkCAqLwouc3RhdHMtYmxvY2t7CiAgbWFyZ2luOjAgMjBweDsKICBib3JkZXI6MXB4IHNvbGlkIHZhcigtLWJvcmRlcik7CiAgb3ZlcmZsb3c6aGlkZGVuOwp9Ci5zdGF0cy10b3RhbHsKICBkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgzLDFmcik7CiAgYmFja2dyb3VuZDp2YXIoLS1iZy1jYXJkKTsKICBib3JkZXItYm90dG9tOjFweCBzb2xpZCB2YXIoLS1ib3JkZXIpOwp9Ci5zdGF0LWNlbGx7CiAgcGFkZGluZzoxNHB4IDhweDt0ZXh0LWFsaWduOmNlbnRlcjsKICBib3JkZXItcmlnaHQ6MXB4IHNvbGlkIHZhcigtLWJvcmRlcik7Cn0KLnN0YXQtY2VsbDpsYXN0LWNoaWxke2JvcmRlci1yaWdodDpub25lfQouc3RhdC1udW17CiAgZm9udC1mYW1pbHk6J0JlYmFzIE5ldWUnLHNhbnMtc2VyaWY7CiAgZm9udC1zaXplOjJyZW07bGluZS1oZWlnaHQ6MTsKfQouc3RhdC1sYWJlbHtmb250LXNpemU6LjU4cmVtO2NvbG9yOnZhcigtLXRleHQtbXV0ZSk7bWFyZ2luLXRvcDozcHh9Ci5zdGF0cy1zcG9ydHsKICBkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgzLDFmcik7CiAgYmFja2dyb3VuZDp2YXIoLS1iZy1jYXJkKTsKfQouc3BvcnQtc3RhdHsKICBwYWRkaW5nOjEycHggOHB4O3RleHQtYWxpZ246Y2VudGVyOwogIGJvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tYm9yZGVyKTsKICBwb3NpdGlvbjpyZWxhdGl2ZTsKfQouc3BvcnQtc3RhdDpsYXN0LWNoaWxke2JvcmRlci1yaWdodDpub25lfQouc3BvcnQtc3RhdDo6YmVmb3JlewogIGNvbnRlbnQ6Jyc7cG9zaXRpb246YWJzb2x1dGU7dG9wOjA7bGVmdDowO3JpZ2h0OjA7aGVpZ2h0OjNweDsKfQouc3BvcnQtc3RhdC5ob3JzZTo6YmVmb3Jle2JhY2tncm91bmQ6dmFyKC0taG9yc2UpfQouc3BvcnQtc3RhdC5ib2F0OjpiZWZvcmUge2JhY2tncm91bmQ6dmFyKC0tYm9hdCl9Ci5zcG9ydC1zdGF0LmN5Y2xlOjpiZWZvcmV7YmFja2dyb3VuZDp2YXIoLS1jeWNsZSl9Ci5zcy1sYWJlbHtmb250LXNpemU6LjYycmVtO2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tYm90dG9tOjZweH0KLnNwb3J0LXN0YXQuaG9yc2UgLnNzLWxhYmVse2NvbG9yOnZhcigtLWhvcnNlKX0KLnNwb3J0LXN0YXQuYm9hdCAgLnNzLWxhYmVse2NvbG9yOnZhcigtLWJvYXQpfQouc3BvcnQtc3RhdC5jeWNsZSAuc3MtbGFiZWx7Y29sb3I6dmFyKC0tY3ljbGUpfQouc3MtbnVte2ZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZToxLjNyZW07bGluZS1oZWlnaHQ6MX0KLnNzLXN1Yntmb250LXNpemU6LjU1cmVtO2NvbG9yOnZhcigtLXRleHQtbXV0ZSk7bWFyZ2luLXRvcDoxcHh9Ci5zdGF0cy1saW5re3RleHQtYWxpZ246Y2VudGVyO21hcmdpbjo4cHggMjBweCAwfQouc3RhdHMtbGluayBhe2ZvbnQtc2l6ZTouN3JlbTtjb2xvcjp2YXIoLS1nb2xkKTt0ZXh0LWRlY29yYXRpb246bm9uZX0KCi8qIOKVkOKVkCDnq7bmioDliKXkuojmg7Pjgrvjgq/jgrfjg6fjg7Mg4pWQ4pWQICovCi5zcG9ydC1zZWN0aW9uewogIG1hcmdpbjowIDIwcHg7CiAgYm9yZGVyOjFweCBzb2xpZCB0cmFuc3BhcmVudDsKICBib3JkZXItcmFkaXVzOjJweDsKICBvdmVyZmxvdzpoaWRkZW47Cn0KLnNwb3J0LXNlY3Rpb24uaG9yc2V7Ym9yZGVyLWNvbG9yOnZhcigtLWhvcnNlLWJkKX0KLnNwb3J0LXNlY3Rpb24uYm9hdCB7Ym9yZGVyLWNvbG9yOnZhcigtLWJvYXQtYmQpfQouc3BvcnQtc2VjdGlvbi5jeWNsZXtib3JkZXItY29sb3I6dmFyKC0tY3ljbGUtYmQpfQoKLnNwb3J0LXNlY3Rpb24taGVhZHsKICBkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4OwogIHBhZGRpbmc6MTJweCAxNnB4OwogIGJvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA1KTsKfQouc3BvcnQtc2VjdGlvbi5ob3JzZSAuc3BvcnQtc2VjdGlvbi1oZWFke2JhY2tncm91bmQ6dmFyKC0taG9yc2UtYmcpfQouc3BvcnQtc2VjdGlvbi5ib2F0ICAuc3BvcnQtc2VjdGlvbi1oZWFke2JhY2tncm91bmQ6dmFyKC0tYm9hdC1iZyl9Ci5zcG9ydC1zZWN0aW9uLmN5Y2xlIC5zcG9ydC1zZWN0aW9uLWhlYWR7YmFja2dyb3VuZDp2YXIoLS1jeWNsZS1iZyl9Cgouc3NoLWljb257Zm9udC1zaXplOjEuMnJlbX0KLnNzaC1uYW1le2ZvbnQtc2l6ZTouODJyZW07Zm9udC13ZWlnaHQ6NzAwO2xldHRlci1zcGFjaW5nOi4wOGVtfQouc3BvcnQtc2VjdGlvbi5ob3JzZSAuc3NoLW5hbWV7Y29sb3I6dmFyKC0taG9yc2UpfQouc3BvcnQtc2VjdGlvbi5ib2F0ICAuc3NoLW5hbWV7Y29sb3I6dmFyKC0tYm9hdCl9Ci5zcG9ydC1zZWN0aW9uLmN5Y2xlIC5zc2gtbmFtZXtjb2xvcjp2YXIoLS1jeWNsZSl9Ci5zc2gtZ3JhZGV7CiAgZm9udC1mYW1pbHk6J0JlYmFzIE5ldWUnLHNhbnMtc2VyaWY7Zm9udC1zaXplOi43MnJlbTsKICBwYWRkaW5nOjJweCA4cHg7Ym9yZGVyLXJhZGl1czoycHg7bGV0dGVyLXNwYWNpbmc6LjA1ZW07Cn0KLnNwb3J0LXNlY3Rpb24uaG9yc2UgLnNzaC1ncmFkZXtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMik7Y29sb3I6dmFyKC0tZ29sZC1sKX0KLnNwb3J0LXNlY3Rpb24uYm9hdCAgLnNzaC1ncmFkZXtiYWNrZ3JvdW5kOnJnYmEoMCwxODAsMjE2LC4yKTtjb2xvcjp2YXIoLS1ib2F0KX0KLnNwb3J0LXNlY3Rpb24uY3ljbGUgLnNzaC1ncmFkZXtiYWNrZ3JvdW5kOnJnYmEoMjMyLDQ5LDU4LC4yKTtjb2xvcjp2YXIoLS1jeWNsZSl9Ci5zc2gtdGltZXsKICBmb250LWZhbWlseTonQmViYXMgTmV1ZScsc2Fucy1zZXJpZjtmb250LXNpemU6MS4xcmVtOwogIGNvbG9yOnZhcigtLWdvbGQtbCk7bGV0dGVyLXNwYWNpbmc6LjA0ZW07bWFyZ2luLWxlZnQ6YXV0bzsKfQouc3NoLWFycm93e2ZvbnQtc2l6ZTouOXJlbTtjb2xvcjp2YXIoLS10ZXh0LW11dGUpO21hcmdpbi1sZWZ0OjhweH0KCi5zcG9ydC1zZWN0aW9uLWJvZHl7CiAgYmFja2dyb3VuZDp2YXIoLS1iZy1jYXJkKTsKICBwYWRkaW5nOjE0cHggMTZweDsKfQoKLyog5pys5ZG96KGo56S6ICovCi5ob25tZWktcm93ewogIGRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7CiAgbWFyZ2luLWJvdHRvbToxMHB4Owp9Ci5ob25tZWktbWFya3sKICBmb250LWZhbWlseTonTm90byBTZXJpZiBKUCcsc2VyaWY7Zm9udC13ZWlnaHQ6OTAwOwogIGZvbnQtc2l6ZToxLjRyZW07Y29sb3I6dmFyKC0tZ29sZC1sKTsKICBmbGV4LXNocmluazowO3dpZHRoOjI0cHg7dGV4dC1hbGlnbjpjZW50ZXI7Cn0KLmhvbm1laS1uYW1lewogIGZvbnQtZmFtaWx5OidOb3RvIFNlcmlmIEpQJyxzZXJpZjtmb250LXdlaWdodDo3MDA7CiAgZm9udC1zaXplOjEuMXJlbTtjb2xvcjp2YXIoLS10ZXh0KTtmbGV4OjE7Cn0KLmV2LWJhZGdlewogIGZvbnQtZmFtaWx5OidCZWJhcyBOZXVlJyxzYW5zLXNlcmlmOwogIGZvbnQtc2l6ZToxcmVtO3BhZGRpbmc6NHB4IDEycHg7Ym9yZGVyLXJhZGl1czoycHg7CiAgZm9udC13ZWlnaHQ6NzAwO2xldHRlci1zcGFjaW5nOi4wNGVtO2ZsZXgtc2hyaW5rOjA7Cn0KLmV2LWJhZGdlLnN0cm9uZ3tiYWNrZ3JvdW5kOnJnYmEoNDYsMjA0LDExMywuMik7Y29sb3I6dmFyKC0tZ3JlZW4pO2JvcmRlcjoxcHggc29saWQgcmdiYSg0NiwyMDQsMTEzLC40KX0KLmV2LWJhZGdlLmJ1eXtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMik7Y29sb3I6dmFyKC0tZ29sZC1sKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuNCl9Ci5ldi1iYWRnZS53YXRjaHtiYWNrZ3JvdW5kOnJnYmEoOTAsOTgsMTIwLC4xNSk7Y29sb3I6dmFyKC0tdGV4dC1tdXRlKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoOTAsOTgsMTIwLC4zKX0KLnJpLWdyYWRlLWJhZGdle2Rpc3BsYXk6aW5saW5lLWJsb2NrO21hcmdpbi1sZWZ0OjRweDtwYWRkaW5nOjFweCA1cHg7Ym9yZGVyLXJhZGl1czozcHg7Zm9udC1zaXplOjAuNjVyZW07Zm9udC13ZWlnaHQ6NzAwO2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4yNSk7Y29sb3I6dmFyKC0tZ29sZC1sKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuNCk7dmVydGljYWwtYWxpZ246bWlkZGxlfQoucmFjZS1pdGVtLmdyYWRlZHtib3JkZXItbGVmdDozcHggc29saWQgdmFyKC0tZ29sZC1sKSAhaW1wb3J0YW50fQoucmFjZS1pdGVtLmdyYWRlZCAucmktdGltZXtjb2xvcjp2YXIoLS1nb2xkLWwpfQoKLnJlYXNvbi10ZXh0ewogIGZvbnQtc2l6ZTouNzRyZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKTtsaW5lLWhlaWdodDoxLjc7CiAgcGFkZGluZzo4cHggMTJweDsKICBiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjAyKTsKICBib3JkZXItbGVmdDoycHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpOwogIG1hcmdpbi1ib3R0b206OHB4Owp9Ci5zcG9ydC1zZWN0aW9uLmhvcnNlIC5yZWFzb24tdGV4dHtib3JkZXItbGVmdC1jb2xvcjp2YXIoLS1ob3JzZSl9Ci5zcG9ydC1zZWN0aW9uLmJvYXQgIC5yZWFzb24tdGV4dHtib3JkZXItbGVmdC1jb2xvcjp2YXIoLS1ib2F0KX0KLnNwb3J0LXNlY3Rpb24uY3ljbGUgLnJlYXNvbi10ZXh0e2JvcmRlci1sZWZ0LWNvbG9yOnZhcigtLWN5Y2xlKX0KCi5kZXRhaWwtbGlua3sKICBkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4OwogIGZvbnQtc2l6ZTouNzRyZW07Zm9udC13ZWlnaHQ6NzAwOwogIHBhZGRpbmc6OHB4IDE2cHg7Ym9yZGVyLXJhZGl1czoycHg7CiAgdHJhbnNpdGlvbjpmaWx0ZXIgLjJzOwp9Ci5kZXRhaWwtbGluazpob3ZlcntmaWx0ZXI6YnJpZ2h0bmVzcygxLjIpfQouc3BvcnQtc2VjdGlvbi5ob3JzZSAuZGV0YWlsLWxpbmt7YmFja2dyb3VuZDp2YXIoLS1ob3JzZS1iZyk7Y29sb3I6dmFyKC0tZ29sZC1sKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWhvcnNlLWJkKX0KLnNwb3J0LXNlY3Rpb24uYm9hdCAgLmRldGFpbC1saW5re2JhY2tncm91bmQ6dmFyKC0tYm9hdC1iZyk7Y29sb3I6dmFyKC0tYm9hdCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1ib2F0LWJkKX0KLnNwb3J0LXNlY3Rpb24uY3ljbGUgLmRldGFpbC1saW5re2JhY2tncm91bmQ6dmFyKC0tY3ljbGUtYmcpO2NvbG9yOnZhcigtLWN5Y2xlKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWN5Y2xlLWJkKX0KCi5uby1waWNrewogIGZvbnQtc2l6ZTouOHJlbTtjb2xvcjp2YXIoLS10ZXh0LW11dGUpOwogIHBhZGRpbmc6MjBweCAwO3RleHQtYWxpZ246Y2VudGVyOwp9CgovKiDilZDilZAg5YWo44Os44O844K55LiA6Kan77yI44Kz44Oz44OR44Kv44OI77yJIOKVkOKVkCAqLwoucmFjZS1saXN0e2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47Z2FwOjFweDttYXJnaW4tdG9wOjhweH0KLnJhY2UtaXRlbXsKICBkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyOwogIHBhZGRpbmc6MTBweCAxNHB4O2dhcDoxMHB4OwogIGJhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDIpOwogIGJvcmRlci1sZWZ0OjNweCBzb2xpZCB0cmFuc3BhcmVudDsKICB0ZXh0LWRlY29yYXRpb246bm9uZTt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjJzOwp9Ci5yYWNlLWl0ZW0uaG9yc2V7Ym9yZGVyLWxlZnQtY29sb3I6dmFyKC0taG9yc2UpfQoucmFjZS1pdGVtLmJvYXQge2JvcmRlci1sZWZ0LWNvbG9yOnZhcigtLWJvYXQpfQoucmFjZS1pdGVtLmN5Y2xle2JvcmRlci1sZWZ0LWNvbG9yOnZhcigtLWN5Y2xlKX0KLnJhY2UtaXRlbTpob3ZlcntiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA0KX0KLnJpLXRpbWV7CiAgZm9udC1mYW1pbHk6J0JlYmFzIE5ldWUnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjFyZW07CiAgY29sb3I6dmFyKC0tZ29sZC1sKTtsZXR0ZXItc3BhY2luZzouMDRlbTt3aWR0aDo0NHB4O2ZsZXgtc2hyaW5rOjA7Cn0KLnJpLXZlbnVle2ZvbnQtc2l6ZTouN3JlbTtjb2xvcjp2YXIoLS10ZXh0LWRpbSk7d2lkdGg6NjBweDtmbGV4LXNocmluazowO3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KLnJpLW5hbWV7Zm9udC1zaXplOi44MnJlbTtjb2xvcjp2YXIoLS10ZXh0KTtmbGV4OjE7bGluZS1oZWlnaHQ6MS4zfQoucmktanVkZ2V7CiAgZm9udC1zaXplOi42MnJlbTtwYWRkaW5nOjJweCA4cHg7Ym9yZGVyLXJhZGl1czoycHg7CiAgZm9udC13ZWlnaHQ6NzAwO2ZsZXgtc2hyaW5rOjA7Cn0KLnJpLWp1ZGdlLnN0cm9uZ3tiYWNrZ3JvdW5kOnJnYmEoNDYsMjA0LDExMywuMTUpO2NvbG9yOnZhcigtLWdyZWVuKX0KLnJpLWp1ZGdlLmJ1eXtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMTUpO2NvbG9yOnZhcigtLWdvbGQtbCl9Ci5yaS1qdWRnZS53YXRjaHtiYWNrZ3JvdW5kOnJnYmEoOTAsOTgsMTIwLC4xKTtjb2xvcjp2YXIoLS10ZXh0LW11dGUpfQoucmktZXZ7CiAgZm9udC1mYW1pbHk6J0JlYmFzIE5ldWUnLHNhbnMtc2VyaWY7Zm9udC1zaXplOi45cmVtOwogIGNvbG9yOnZhcigtLWdyZWVuKTt3aWR0aDo0OHB4O3RleHQtYWxpZ246cmlnaHQ7ZmxleC1zaHJpbms6MDsKfQoucmktYXJye2NvbG9yOnZhcigtLXRleHQtbXV0ZSk7Zm9udC1zaXplOi43OHJlbTtmbGV4LXNocmluazowfQoKLyog4pWQ4pWQIExJTkUg44OQ44OK44O8IOKVkOKVkCAqLwoubGluZS1iYW5uZXJ7CiAgYmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTM1ZGVnLCMwNDJhMGEsIzA2MTgwOCk7CiAgYm9yZGVyOjFweCBzb2xpZCByZ2JhKDYsMTk5LDg1LC4yNSk7CiAgcGFkZGluZzoyNHB4IDIwcHg7bWFyZ2luOjI4cHggMjBweCAwO3RleHQtYWxpZ246Y2VudGVyOwp9Ci5sYi10YWd7Zm9udC1zaXplOi42NXJlbTtjb2xvcjojMDZDNzU1O2ZvbnQtd2VpZ2h0OjcwMDtsZXR0ZXItc3BhY2luZzouMTJlbTttYXJnaW4tYm90dG9tOjhweH0KLmxiLXRpdGxle2ZvbnQtZmFtaWx5OidOb3RvIFNlcmlmIEpQJyxzZXJpZjtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjFyZW07Y29sb3I6I2ZmZjttYXJnaW4tYm90dG9tOjZweH0KLmxiLWRlc2N7Zm9udC1zaXplOi43NXJlbTtjb2xvcjojYThiMGM0O21hcmdpbi1ib3R0b206MTZweDtsaW5lLWhlaWdodDoxLjd9Ci5sYi1idG57CiAgZGlzcGxheTppbmxpbmUtZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDsKICBwYWRkaW5nOjE0cHggMjhweDtiYWNrZ3JvdW5kOiMwNkM3NTU7Y29sb3I6I2ZmZjsKICBmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOi45cmVtO3RleHQtZGVjb3JhdGlvbjpub25lOwogIGJvcmRlci1yYWRpdXM6NHB4O2xldHRlci1zcGFjaW5nOi4wNGVtOwogIHRyYW5zaXRpb246ZmlsdGVyIC4yczsKfQoubGItYnRuOmhvdmVye2ZpbHRlcjpicmlnaHRuZXNzKDEuMSl9Ci5sYi1ub3Rle2ZvbnQtc2l6ZTouNjVyZW07Y29sb3I6IzVhNjI3ODttYXJnaW4tdG9wOjEwcHh9CgovKiDilZDilZAgU0hBUkUg4pWQ4pWQICovCi5zaGFyZS1ibG9ja3sKICBiYWNrZ3JvdW5kOnZhcigtLWJnLWNhcmQpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tYm9yZGVyKTsKICBwYWRkaW5nOjE0cHggMjBweDttYXJnaW46MTZweCAyMHB4IDA7Cn0KLnNoYXJlLXRpdGxle2ZvbnQtc2l6ZTouNjhyZW07Y29sb3I6dmFyKC0tdGV4dC1tdXRlKTttYXJnaW4tYm90dG9tOjEwcHg7bGV0dGVyLXNwYWNpbmc6LjA1ZW19Ci5zaGFyZS1idXR0b25ze2Rpc3BsYXk6ZmxleDtnYXA6OHB4O2ZsZXgtd3JhcDp3cmFwfQouc2hhcmUtYnRuewogIGRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjZweDsKICBwYWRkaW5nOjlweCAxNnB4O2JvcmRlci1yYWRpdXM6MnB4OwogIGZvbnQtc2l6ZTouNzhyZW07Zm9udC13ZWlnaHQ6NzAwO3RleHQtZGVjb3JhdGlvbjpub25lOwogIGJvcmRlcjpub25lO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidOb3RvIFNhbnMgSlAnLHNhbnMtc2VyaWY7CiAgdHJhbnNpdGlvbjpmaWx0ZXIgLjJzO3doaXRlLXNwYWNlOm5vd3JhcDsKfQouc2hhcmUtYnRuOmhvdmVye2ZpbHRlcjpicmlnaHRuZXNzKDEuMTUpfQouc2hhcmUteHtiYWNrZ3JvdW5kOiMwMDA7Y29sb3I6I2ZmZjtib3JkZXI6MXB4IHNvbGlkICMzMzN9Ci5zaGFyZS1saW5le2JhY2tncm91bmQ6IzA2Qzc1NTtjb2xvcjojZmZmfQouc2hhcmUtaWd7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTM1ZGVnLCNmMDk0MzMsI2U2NjgzYywjZGMyNzQzLCNjYzIzNjYsI2JjMTg4OCk7Y29sb3I6I2ZmZn0KLnNoYXJlLWNvcHl7YmFja2dyb3VuZDp2YXIoLS1iZy1jYXJkMik7Y29sb3I6dmFyKC0tdGV4dC1kaW0pO2JvcmRlcjoxcHggc29saWQgdmFyKC0tYm9yZGVyKX0KLnNoYXJlLWNvcGllZHtjb2xvcjp2YXIoLS1nb2xkLWwpIWltcG9ydGFudDtib3JkZXItY29sb3I6dmFyKC0tZ29sZCkhaW1wb3J0YW50fQoKLyog4pWQ4pWQIEZPT1RFUiDilZDilZAgKi8KZm9vdGVyewogIGJhY2tncm91bmQ6IzA1MDgxMDtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjA4KTsKICBwYWRkaW5nOjI4cHggMjBweDt0ZXh0LWFsaWduOmNlbnRlcjsKfQouZnQtbG9nb3tmb250LWZhbWlseTonTm90byBTZXJpZiBKUCcsc2VyaWY7Zm9udC13ZWlnaHQ6OTAwO2ZvbnQtc2l6ZToxcmVtO2NvbG9yOnZhcigtLWdvbGQtbCk7bWFyZ2luLWJvdHRvbTo4cHh9Ci5mdC1kaXNje2ZvbnQtc2l6ZTouNjZyZW07Y29sb3I6cmdiYSgxNjgsMTgwLDIwMCwuNDUpO2xpbmUtaGVpZ2h0OjEuNzttYXgtd2lkdGg6NDYwcHg7bWFyZ2luOjAgYXV0byAxMnB4fQouZnQtbGlua3N7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpjZW50ZXI7Z2FwOjE2cHg7ZmxleC13cmFwOndyYXB9Ci5mdC1saW5rcyBhe2ZvbnQtc2l6ZTouN3JlbTtjb2xvcjp2YXIoLS10ZXh0LW11dGUpO3RleHQtZGVjb3JhdGlvbjpub25lfQouZnQtbGlua3MgYTpob3Zlcntjb2xvcjp2YXIoLS1nb2xkLWwpfQoKLyog4pWQ4pWQIFJFU1BPTlNJVkUg4pWQ4pWQICovCkBtZWRpYShtYXgtd2lkdGg6NjAwcHgpewogIG5hdntkaXNwbGF5Om5vbmV9CiAgLnN0YXRzLXRvdGFsLC5zdGF0cy1zcG9ydHtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDMsMWZyKX0KICAuc3BvcnQtdGFic3tncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDMsMWZyKX0KICAuc2hhcmUtYnV0dG9uc3tnYXA6NnB4fQogIC5zaGFyZS1idG57Zm9udC1zaXplOi43MnJlbTtwYWRkaW5nOjhweCAxMnB4fQogIC5yaS12ZW51ZXtkaXNwbGF5Om5vbmV9Cn0KQG1lZGlhKG1heC13aWR0aDozODBweCl7CiAgLmhlcm8tdGl0bGV7Zm9udC1zaXplOjJyZW19Cn0KCi8qIOKVkOKVkCBFTVBUWSAvIExPQURJTkcg4pWQ4pWQICovCi5lbXB0eXt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjMycHg7Y29sb3I6dmFyKC0tdGV4dC1tdXRlKTtmb250LXNpemU6LjgycmVtfQoubG9hZGluZy1wdWxzZXthbmltYXRpb246cHVsc2UgMS41cyBlYXNlLWluLW91dCBpbmZpbml0ZX0KQGtleWZyYW1lcyBwdWxzZXswJSwxMDAle29wYWNpdHk6LjR9NTAle29wYWNpdHk6MX19Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+Cgo8IS0tIEhFQURFUiAtLT4KPGhlYWRlcj4KICA8YSBjbGFzcz0ibG9nbyIgaHJlZj0iIyI+CiAgICA8c3BhbiBjbGFzcz0ibG9nby1qYSI+5LqI5oOz44Gu6YmE5YmHPC9zcGFuPgogICAgPHNwYW4gY2xhc3M9ImxvZ28tZW4iPllPU08gTk8gVEVTU09LVTwvc3Bhbj4KICA8L2E+CiAgPG5hdj4KICAgIDxhIGhyZWY9ImtlaWJhLmh0bWwiPvCfkLQg56u26aasPC9hPgogICAgPGEgaHJlZj0ia3lvdGVpLmh0bWwiPvCfmqQg56u26ImHPC9hPgogICAgPGEgaHJlZj0ia2VpcmluLmh0bWwiPvCfmrQg56u26LyqPC9hPgogICAgPGEgaHJlZj0icmVzdWx0cy5odG1sIj7wn5OKIOWun+e4vjwvYT4KICAgIDxhIGhyZWY9InByZW1pdW0uaHRtbCIgc3R5bGU9ImNvbG9yOiMwNkM3NTU7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDYsMTk5LDg1LC4zKTtib3JkZXItcmFkaXVzOjJweDtwYWRkaW5nOjRweCAxMHB4Ij7wn5GRIOacieaWmeODl+ODqeODszwvYT4KICA8L25hdj4KPC9oZWFkZXI+Cgo8IS0tIEhFUk8gLS0+CjxkaXYgY2xhc3M9Imhlcm8iPgogIDxkaXYgY2xhc3M9Imhlcm8tdGV4dCI+CiAgICA8ZGl2IGNsYXNzPSJoZXJvLWJhZGdlIiBpZD0idXBkYXRlLWJhZGdlIj5FViBQUkVESUNUSU9OIMK3IOacn+W+heWApOOBp+WLneOBpDwvZGl2PgogICAgPGgxIGNsYXNzPSJoZXJvLXRpdGxlIj7mhJ/opprjgpLmjajjgabjgaY8YnI+PGVtPuODh+ODvOOCv+OBp+WLneOBpjwvZW0+PC9oMT4KICAgIDxwIGNsYXNzPSJoZXJvLXN1YiI+SE9SU0UgwrcgQk9BVCDCtyBDWUNMRSDCtyBFViBBTkFMWVNJUzwvcD4KICAgIDxhIGhyZWY9Imh0dHBzOi8vbGluZS5tZS9SL3RpL3AvQDQxNGlyaWt4IiB0YXJnZXQ9Il9ibGFuayIgcmVsPSJub29wZW5lciIgY2xhc3M9Imhlcm8tY3RhIj4KICAgICAg8J+SrCDnhKHmlplMSU5F55m76Yyy44Gn5LuK5pel44Gu5Y6z6YG4RVbjg6zjg7zjgrnjgpLlj5fjgZHlj5bjgosKICAgIDwvYT4KICAgIDxwIGNsYXNzPSJoZXJvLWN0YS1ub3RlIj7nmbvpjLLnhKHmlpkgwrcg44GE44Gk44Gn44KC6Kej57SET0sgwrcg5q+O5pyd6YWN5L+hPC9wPgogIDwvZGl2PgoKICA8IS0tIOertuaKgOOCv+ODlu+8iOOCr+ODquODg+OCr+OBp+WQhOODmuODvOOCuOOBuO+8iSAtLT4KICA8ZGl2IGNsYXNzPSJzcG9ydC10YWJzIj4KICAgIDxhIGNsYXNzPSJzcG9ydC10YWIgaG9yc2UiIGhyZWY9ImtlaWJhLmh0bWwiPgogICAgICA8c3BhbiBjbGFzcz0ic3QtaWNvbiI+8J+QtDwvc3Bhbj4KICAgICAgPHNwYW4gY2xhc3M9InN0LWxhYmVsIj7nq7bppqw8L3NwYW4+CiAgICAgIDxzcGFuIGNsYXNzPSJzdC1jb3VudCIgaWQ9InRhYi1ob3JzZS1jb3VudCI+4oCUPC9zcGFuPgogICAgPC9hPgogICAgPGEgY2xhc3M9InNwb3J0LXRhYiBib2F0IiBocmVmPSJreW90ZWkuaHRtbCI+CiAgICAgIDxzcGFuIGNsYXNzPSJzdC1pY29uIj7wn5qkPC9zcGFuPgogICAgICA8c3BhbiBjbGFzcz0ic3QtbGFiZWwiPuertuiJhzwvc3Bhbj4KICAgICAgPHNwYW4gY2xhc3M9InN0LWNvdW50IiBpZD0idGFiLWJvYXQtY291bnQiPuKAlDwvc3Bhbj4KICAgIDwvYT4KICAgIDxhIGNsYXNzPSJzcG9ydC10YWIgY3ljbGUiIGhyZWY9ImtlaXJpbi5odG1sIj4KICAgICAgPHNwYW4gY2xhc3M9InN0LWljb24iPvCfmrQ8L3NwYW4+CiAgICAgIDxzcGFuIGNsYXNzPSJzdC1sYWJlbCI+56u26LyqPC9zcGFuPgogICAgICA8c3BhbiBjbGFzcz0ic3QtY291bnQiIGlkPSJ0YWItY3ljbGUtY291bnQiPuKAlDwvc3Bhbj4KICAgIDwvYT4KICA8L2Rpdj4KPC9kaXY+Cgo8ZGl2IGNsYXNzPSJtYWluIj4KCiAgPCEtLSDilZDilZAg5a6f57i+44OQ44OK44O8IOKVkOKVkCAtLT4KICA8ZGl2IGNsYXNzPSJzZWMiIHN0eWxlPSJwYWRkaW5nLWJvdHRvbTowIj4KICAgIDxkaXYgY2xhc3M9InNlYy1oZWFkIj4KICAgICAgPGRpdiBjbGFzcz0ic2VjLWxpbmUgZ29sZCI+PC9kaXY+CiAgICAgIDxzcGFuIGNsYXNzPSJzZWMtdGl0bGUgZ29sZCI+57Sv6KiI5a6f57i+PC9zcGFuPgogICAgICA8YSBocmVmPSJyZXN1bHRzLmh0bWwiIGNsYXNzPSJzZWMtYmFkZ2UgbGl2ZSI+5YWo5bGl5q2044KS6KaL44KLIOKGkjwvYT4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0ic3RhdHMtYmxvY2siPgogICAgICA8ZGl2IGNsYXNzPSJzdGF0cy10b3RhbCI+CiAgICAgICAgPGRpdiBjbGFzcz0ic3RhdC1jZWxsIj4KICAgICAgICAgIDxkaXYgY2xhc3M9InN0YXQtbnVtIiBpZD0iaWR4LXJlY292ZXJ5IiBzdHlsZT0iY29sb3I6dmFyKC0tZ29sZC1sKSI+LS0lPC9kaXY+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJzdGF0LWxhYmVsIj7ntK/oqIjlm57lj47njoc8L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJzdGF0LWNlbGwiPgogICAgICAgICAgPGRpdiBjbGFzcz0ic3RhdC1udW0iIGlkPSJpZHgtaGl0cmF0ZSIgc3R5bGU9ImNvbG9yOiNmZmYiPi0tJTwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0ic3RhdC1sYWJlbCI+55qE5Lit546HPC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0ic3RhdC1jZWxsIj4KICAgICAgICAgIDxkaXYgY2xhc3M9InN0YXQtbnVtIiBpZD0iaWR4LXRvdGFsIiBzdHlsZT0iY29sb3I6I2ZmZiI+MDwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0ic3RhdC1sYWJlbCI+57Sv6KiI5LqI5oOz5pWwPC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJzdGF0cy1zcG9ydCI+CiAgICAgICAgPGRpdiBjbGFzcz0ic3BvcnQtc3RhdCBob3JzZSI+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJzcy1sYWJlbCI+8J+QtCDnq7bppqw8L2Rpdj4KICAgICAgICAgIDxkaXYgY2xhc3M9InNzLW51bSIgaWQ9InN0YXQtaG9yc2UtaGl0IiBzdHlsZT0iY29sb3I6I2ZmZiI+LS0lPC9kaXY+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJzcy1zdWIiPueahOS4reeOhzwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0ic3MtbnVtIiBpZD0ic3RhdC1ob3JzZS1yZWMiIHN0eWxlPSJjb2xvcjp2YXIoLS1nb2xkLWwpO21hcmdpbi10b3A6NHB4Ij4tLSU8L2Rpdj4KICAgICAgICAgIDxkaXYgY2xhc3M9InNzLXN1YiI+5Zue5Y+O546HPC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0ic3BvcnQtc3RhdCBib2F0Ij4KICAgICAgICAgIDxkaXYgY2xhc3M9InNzLWxhYmVsIj7wn5qkIOertuiJhzwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0ic3MtbnVtIiBpZD0ic3RhdC1ib2F0LWhpdCIgc3R5bGU9ImNvbG9yOiNmZmYiPi0tJTwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0ic3Mtc3ViIj7nmoTkuK3njoc8L2Rpdj4KICAgICAgICAgIDxkaXYgY2xhc3M9InNzLW51bSIgaWQ9InN0YXQtYm9hdC1yZWMiIHN0eWxlPSJjb2xvcjp2YXIoLS1nb2xkLWwpO21hcmdpbi10b3A6NHB4Ij4tLSU8L2Rpdj4KICAgICAgICAgIDxkaXYgY2xhc3M9InNzLXN1YiI+5Zue5Y+O546HPC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0ic3BvcnQtc3RhdCBjeWNsZSI+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJzcy1sYWJlbCI+8J+atCDnq7bovKo8L2Rpdj4KICAgICAgICAgIDxkaXYgY2xhc3M9InNzLW51bSIgaWQ9InN0YXQtY3ljbGUtaGl0IiBzdHlsZT0iY29sb3I6I2ZmZiI+LS0lPC9kaXY+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJzcy1zdWIiPueahOS4reeOhzwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0ic3MtbnVtIiBpZD0ic3RhdC1jeWNsZS1yZWMiIHN0eWxlPSJjb2xvcjp2YXIoLS1nb2xkLWwpO21hcmdpbi10b3A6NHB4Ij4tLSU8L2Rpdj4KICAgICAgICAgIDxkaXYgY2xhc3M9InNzLXN1YiI+5Zue5Y+O546HPC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0g4pWQ4pWQIOacrOaXpeOBruazqOebruS6iOaDs++8iOertuaKgOWIpe+8iSDilZDilZAgLS0+CiAgPGRpdiBjbGFzcz0ic2VjIiBzdHlsZT0icGFkZGluZy1ib3R0b206MCI+CiAgICA8ZGl2IGNsYXNzPSJzZWMtaGVhZCI+CiAgICAgIDxkaXYgY2xhc3M9InNlYy1saW5lIGdvbGQiPjwvZGl2PgogICAgICA8c3BhbiBjbGFzcz0ic2VjLXRpdGxlIGdvbGQiPuacrOaXpeOBruazqOebruS6iOaDszwvc3Bhbj4KICAgICAgPHNwYW4gY2xhc3M9InNlYy1iYWRnZSBsaXZlIiBpZD0idG9kYXktbGl2ZS1iYWRnZSIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+4pePIExJVkXmm7TmlrDkuK08L3NwYW4+CiAgICA8L2Rpdj4KCiAgICA8IS0tIOertummrOOCu+OCr+OCt+ODp+ODsyAtLT4KICAgIDxkaXYgY2xhc3M9InNwb3J0LXNlY3Rpb24gaG9yc2UiIGlkPSJzZWN0aW9uLWhvcnNlIiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMnB4Ij4KICAgICAgPGEgY2xhc3M9InNwb3J0LXNlY3Rpb24taGVhZCIgaHJlZj0ia2VpYmEuaHRtbCIgaWQ9ImhlYWQtaG9yc2UiPgogICAgICAgIDxzcGFuIGNsYXNzPSJzc2gtaWNvbiI+8J+QtDwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0ic3NoLW5hbWUiPuertummrDwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0ic3NoLWdyYWRlIiBpZD0iZ3JhZGUtaG9yc2UiIHN0eWxlPSJkaXNwbGF5Om5vbmUiPjwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0ic3NoLXRpbWUiIGlkPSJ0aW1lLWhvcnNlIj48L3NwYW4+CiAgICAgICAgPHNwYW4gY2xhc3M9InNzaC1hcnJvdyI+4oC6PC9zcGFuPgogICAgICA8L2E+CiAgICAgIDxkaXYgY2xhc3M9InNwb3J0LXNlY3Rpb24tYm9keSIgaWQ9ImJvZHktaG9yc2UiPgogICAgICAgIDxkaXYgY2xhc3M9ImVtcHR5IGxvYWRpbmctcHVsc2UiPuiqreOBv+i+vOOBv+S4rS4uLjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgoKICAgIDwhLS0g56u26ImH44K744Kv44K344On44OzIC0tPgogICAgPGRpdiBjbGFzcz0ic3BvcnQtc2VjdGlvbiBib2F0IiBpZD0ic2VjdGlvbi1ib2F0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMnB4Ij4KICAgICAgPGEgY2xhc3M9InNwb3J0LXNlY3Rpb24taGVhZCIgaHJlZj0ia3lvdGVpLmh0bWwiIGlkPSJoZWFkLWJvYXQiPgogICAgICAgIDxzcGFuIGNsYXNzPSJzc2gtaWNvbiI+8J+apDwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0ic3NoLW5hbWUiPuertuiJhzwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0ic3NoLWdyYWRlIiBpZD0iZ3JhZGUtYm9hdCIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+PC9zcGFuPgogICAgICAgIDxzcGFuIGNsYXNzPSJzc2gtdGltZSIgaWQ9InRpbWUtYm9hdCI+PC9zcGFuPgogICAgICAgIDxzcGFuIGNsYXNzPSJzc2gtYXJyb3ciPuKAujwvc3Bhbj4KICAgICAgPC9hPgogICAgICA8ZGl2IGNsYXNzPSJzcG9ydC1zZWN0aW9uLWJvZHkiIGlkPSJib2R5LWJvYXQiPgogICAgICAgIDxkaXYgY2xhc3M9ImVtcHR5IGxvYWRpbmctcHVsc2UiPuiqreOBv+i+vOOBv+S4rS4uLjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgoKICAgIDwhLS0g56u26Lyq44K744Kv44K344On44OzIC0tPgogICAgPGRpdiBjbGFzcz0ic3BvcnQtc2VjdGlvbiBjeWNsZSIgaWQ9InNlY3Rpb24tY3ljbGUiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjAiPgogICAgICA8YSBjbGFzcz0ic3BvcnQtc2VjdGlvbi1oZWFkIiBocmVmPSJrZWlyaW4uaHRtbCIgaWQ9ImhlYWQtY3ljbGUiPgogICAgICAgIDxzcGFuIGNsYXNzPSJzc2gtaWNvbiI+8J+atDwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0ic3NoLW5hbWUiPuertui8qjwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0ic3NoLWdyYWRlIiBpZD0iZ3JhZGUtY3ljbGUiIHN0eWxlPSJkaXNwbGF5Om5vbmUiPjwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0ic3NoLXRpbWUiIGlkPSJ0aW1lLWN5Y2xlIj48L3NwYW4+CiAgICAgICAgPHNwYW4gY2xhc3M9InNzaC1hcnJvdyI+4oC6PC9zcGFuPgogICAgICA8L2E+CiAgICAgIDxkaXYgY2xhc3M9InNwb3J0LXNlY3Rpb24tYm9keSIgaWQ9ImJvZHktY3ljbGUiPgogICAgICAgIDxkaXYgY2xhc3M9ImVtcHR5IGxvYWRpbmctcHVsc2UiPuiqreOBv+i+vOOBv+S4rS4uLjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIOKVkOKVkCDlhajjg6zjg7zjgrnkuIDopqcg4pWQ4pWQIC0tPgogIDxkaXYgY2xhc3M9InNlYyIgc3R5bGU9InBhZGRpbmctYm90dG9tOjAiPgogICAgPGRpdiBjbGFzcz0ic2VjLWhlYWQiPgogICAgICA8ZGl2IGNsYXNzPSJzZWMtbGluZSBnb2xkIj48L2Rpdj4KICAgICAgPHNwYW4gY2xhc3M9InNlYy10aXRsZSBnb2xkIj7mnKzml6Xjga7lhajjg6zjg7zjgrk8L3NwYW4+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJhY2UtbGlzdCIgaWQ9ImFsbC1yYWNlcy1saXN0Ij4KICAgICAgPGRpdiBjbGFzcz0iZW1wdHkgbG9hZGluZy1wdWxzZSI+6Kqt44G/6L6844G/5LitLi4uPC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSDilZDilZAgTElORSDjg5Djg4rjg7wg4pWQ4pWQIC0tPgogIDxkaXYgY2xhc3M9ImxpbmUtYmFubmVyIj4KICAgIDxwIGNsYXNzPSJsYi10YWciPvCfk6kgTElOReeEoeaWmeeZu+mMsjwvcD4KICAgIDxwIGNsYXNzPSJsYi10aXRsZSI+5LuK5pel44Gu5Y6z6YG4RVbjg6zjg7zjgrnjgpLnhKHmlpnjgaflj5fjgZHlj5bjgos8L3A+CiAgICA8cCBjbGFzcz0ibGItZGVzYyI+5oSf6Kaa44Gn44Gv44Gq44GP44OH44O844K/44Gn6YG444KT44Gg5pyf5b6F5YCk44OX44Op44K544Gu44Os44O844K544Gu44G/44KS5q+O5pyd6YWN5L+h44CCPGJyPueZu+mMsueEoeaWmeODu+OBhOOBpOOBp+OCguino+e0hE9L44CCPC9wPgogICAgPGEgaHJlZj0iaHR0cHM6Ly9saW5lLm1lL1IvdGkvcC9ANDE0aXJpa3giIHRhcmdldD0iX2JsYW5rIiByZWw9Im5vb3BlbmVyIiBjbGFzcz0ibGItYnRuIj4KICAgICAg8J+SrCDlj4vjgaDjgaHov73liqDvvIjnhKHmlpnvvIkKICAgIDwvYT4KICAgIDxwIGNsYXNzPSJsYi1ub3RlIj7jg5fjg6zjg5/jgqLjg6Djg5fjg6njg7Pjga8gPGEgaHJlZj0icHJlbWl1bS5odG1sIiBzdHlsZT0iY29sb3I6IzA2Qzc1NTt0ZXh0LWRlY29yYXRpb246bm9uZSI+44GT44Gh44KJPC9hPjwvcD4KICA8L2Rpdj4KCjwvZGl2PgoKPCEtLSBTSEFSRSAtLT4KPGRpdiBjbGFzcz0ic2hhcmUtYmxvY2siPgogIDxwIGNsYXNzPSJzaGFyZS10aXRsZSI+8J+ToyDku4rml6Xjga7kuojmg7PjgpLjgrfjgqfjgqLjgZnjgos8L3A+CiAgPGRpdiBjbGFzcz0ic2hhcmUtYnV0dG9ucyI+CiAgICA8YSBjbGFzcz0ic2hhcmUtYnRuIHNoYXJlLXgiIGlkPSJzaGFyZS14IiBocmVmPSIjIiB0YXJnZXQ9Il9ibGFuayIgcmVsPSJub29wZW5lciI+8J2VjyBY44Gn5oqV56i/PC9hPgogICAgPGEgY2xhc3M9InNoYXJlLWJ0biBzaGFyZS1saW5lIiBpZD0ic2hhcmUtbGluZSIgaHJlZj0iIyIgdGFyZ2V0PSJfYmxhbmsiIHJlbD0ibm9vcGVuZXIiPkxJTkUg44Gn6YCB44KLPC9hPgogICAgPGEgY2xhc3M9InNoYXJlLWJ0biBzaGFyZS1pZyIgaHJlZj0iaHR0cHM6Ly93d3cuaW5zdGFncmFtLmNvbS8iIHRhcmdldD0iX2JsYW5rIiByZWw9Im5vb3BlbmVyIj7wn5O3IEluc3RhZ3JhbeOBuDwvYT4KICAgIDxidXR0b24gY2xhc3M9InNoYXJlLWJ0biBzaGFyZS1jb3B5IiBpZD0ic2hhcmUtY29weSI+8J+UlyBVUkzjgrPjg5Tjg7w8L2J1dHRvbj4KICA8L2Rpdj4KPC9kaXY+Cgo8Zm9vdGVyPgogIDxwIGNsYXNzPSJmdC1sb2dvIj7kuojmg7Pjga7piYTliYc8L3A+CiAgPHAgY2xhc3M9ImZ0LWRpc2MiPuacrOOCteOCpOODiOOBruS6iOaDs+OBr+aDheWgseaPkOS+m+OCkuebrueahOOBqOOBl+OBpuOBhOOBvuOBmeOAguWFrOWWtuertuaKgOOBruaKleelqOOBr+iHquW3seiyrOS7u+OBp+OBiumhmOOBhOOBl+OBvuOBmeOAgjE45q2z5pyq5rqA44Gu5pa544Gu5oqV56Wo44Gv5rOV5b6L44Gn56aB44GY44KJ44KM44Gm44GE44G+44GZ44CCPC9wPgogIDxkaXYgY2xhc3M9ImZ0LWxpbmtzIj4KICAgIDxhIGhyZWY9ImluZGV4Lmh0bWwiPuODiOODg+ODlzwvYT4KICAgIDxhIGhyZWY9InJlc3VsdHMuaHRtbCI+5a6f57i+PC9hPgogICAgPGEgaHJlZj0icHJlbWl1bS5odG1sIj7jg5fjg6zjg5/jgqLjg6A8L2E+CiAgICA8YSBocmVmPSJ0b2t1c2hvaG8uaHRtbCI+54m55a6a5ZWG5Y+W5byV5rOV44Gr5Z+644Gl44GP6KGo6KiYPC9hPgogICAgPGEgaHJlZj0icHJpdmFjeS5odG1sIj7jg5fjg6njgqTjg5Djgrfjg7zjg53jg6rjgrfjg7w8L2E+CiAgPC9kaXY+CjwvZm9vdGVyPgoKPHNjcmlwdD4KLy8g4pWQ4pWQIOWumuaVsCDilZDilZAKY29uc3QgU1BPUlRfTUVUQSA9IHsKICBob3JzZToge2ljb246J/CfkLQnLCBsYWJlbDon56u26aasJywgdXJsOidrZWliYS5odG1sJywgIGNsczonaG9yc2UnfSwKICBib2F0OiAge2ljb246J/CfmqQnLCBsYWJlbDon56u26ImHJywgdXJsOidreW90ZWkuaHRtbCcsIGNsczonYm9hdCd9LAogIGN5Y2xlOiB7aWNvbjon8J+atCcsIGxhYmVsOifnq7bovKonLCB1cmw6J2tlaXJpbi5odG1sJywgY2xzOidjeWNsZSd9LAp9Owpjb25zdCBHUkFERV9QUklPUklUWSA9IHtHUDowLFNHOjAsRzE6MSxHMjoyLEczOjMsRkk6NCxGSUk6NH07CgpmdW5jdGlvbiBncmFkZVNjb3JlKHIpeyByZXR1cm4gR1JBREVfUFJJT1JJVFlbci5ncmFkZV0gPz8gNTsgfQpmdW5jdGlvbiBldk51bShyKXsgcmV0dXJuIHBhcnNlRmxvYXQoKHIuZXZ8fCcwJykucmVwbGFjZSgvW14wLTkuLV0vZywnJykpIHx8IDA7IH0KCi8vIOKVkOKVkCDnq7bmioDliKXjgrvjgq/jgrfjg6fjg7Pmj4/nlLsg4pWQ4pWQCmZ1bmN0aW9uIHJlbmRlclNwb3J0U2VjdGlvbihzcG9ydCwgcmFjZXMpIHsKICBjb25zdCBtID0gU1BPUlRfTUVUQVtzcG9ydF07CiAgY29uc3QgYm9keUVsID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoYGJvZHktJHtzcG9ydH1gKTsKICBjb25zdCB0aW1lRWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChgdGltZS0ke3Nwb3J0fWApOwogIGNvbnN0IGdyYWRlRWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChgZ3JhZGUtJHtzcG9ydH1gKTsKCiAgLy8g56u25oqA44Gu44Os44O844K544KS5oq95Ye644GX44GmRVbpoIbjgr3jg7zjg4gKICBjb25zdCBzcG9ydFJhY2VzID0gcmFjZXMuZmlsdGVyKHIgPT4gci5zcG9ydCA9PT0gc3BvcnQpOwogIGNvbnN0IHNvcnRlZCA9IFsuLi5zcG9ydFJhY2VzXS5zb3J0KChhLGIpID0+IHsKICAgIC8vIOW8t+iyt+OBhCA+IOiyt+OBhCA+IOimi+mAgeOCigogICAgY29uc3QgalNjb3JlID0ge+W8t+iyt+OBhDowLCDosrfjgYQ6MX1bYS5qdWRnZV0gPz8gMjsKICAgIGNvbnN0IGpTY29yZUIgPSB75by36LK344GEOjAsIOiyt+OBhDoxfVtiLmp1ZGdlXSA/PyAyOwogICAgaWYgKGpTY29yZSAhPT0galNjb3JlQikgcmV0dXJuIGpTY29yZSAtIGpTY29yZUI7CiAgICByZXR1cm4gZXZOdW0oYikgLSBldk51bShhKTsKICB9KTsKCiAgLy8g44K/44OW44Gu44Kr44Km44Oz44OI5pu05pawCiAgY29uc3QgYnV5Q291bnQgPSBzcG9ydFJhY2VzLmZpbHRlcihyID0+IHIuanVkZ2UgPT09ICflvLfosrfjgYQnIHx8IHIuanVkZ2UgPT09ICfosrfjgYQnKS5sZW5ndGg7CiAgY29uc3QgdGFiRWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChgdGFiLSR7c3BvcnR9LWNvdW50YCk7CiAgaWYgKHRhYkVsKSB0YWJFbC50ZXh0Q29udGVudCA9IGJ1eUNvdW50ID4gMCA/IGAke2J1eUNvdW50feS7tuaOqOWlqGAgOiBgJHtzcG9ydFJhY2VzLmxlbmd0aH3ku7ZgOwoKICBpZiAoIXNvcnRlZC5sZW5ndGgpIHsKICAgIGJvZHlFbC5pbm5lckhUTUwgPSAnPGRpdiBjbGFzcz0ibm8tcGljayI+5pys5pel44Gu5LqI5oOz44Gv44G+44Gg5rqW5YKZ5Lit44Gn44GZPC9kaXY+JzsKICAgIHJldHVybjsKICB9CgogIC8vIOacgOOCguazqOebruOBruODrOODvOOCue+8iOWFiOmgre+8iQogIGNvbnN0IHRvcCA9IHNvcnRlZFswXTsKCiAgLy8g44OY44OD44OA44O844Gr5pmC5Yi744O744Kw44Os44O844OJ6KGo56S6CiAgaWYgKHRvcC50aW1lKSB0aW1lRWwudGV4dENvbnRlbnQgPSB0b3AudGltZTsKICBpZiAodG9wLmdyYWRlKSB7CiAgICBjb25zdCBpc0dyYWRlZFRvcCA9IHRvcC5pc19ncmFkZWQgfHwgWydHMScsJ0cyJywnRzMnLCdTRycsJ0dQJywnSkcxJywnSkcyJywnSkczJ10uaW5jbHVkZXModG9wLmdyYWRlKTsKICAgIGdyYWRlRWwudGV4dENvbnRlbnQgPSBpc0dyYWRlZFRvcCA/IGAke3RvcC5ncmFkZX0g5rOo55uuYCA6IHRvcC5ncmFkZTsKICAgIGdyYWRlRWwuc3R5bGUuZGlzcGxheSA9ICcnOwogICAgaWYgKGlzR3JhZGVkVG9wKSB7CiAgICAgIGdyYWRlRWwuc3R5bGUuYmFja2dyb3VuZCA9ICdyZ2JhKDIwMSwxNjgsNzYsLjMpJzsKICAgICAgZ3JhZGVFbC5zdHlsZS5jb2xvciA9ICd2YXIoLS1nb2xkLWwpJzsKICAgICAgZ3JhZGVFbC5zdHlsZS5mb250V2VpZ2h0ID0gJzcwMCc7CiAgICB9CiAgfQoKICAvLyDliKTlrprjg5Djg4PjgrgKICBjb25zdCBqdWRnZUNsYXNzID0gdG9wLmp1ZGdlID09PSAn5by36LK344GEJyA/ICdzdHJvbmcnIDogdG9wLmp1ZGdlID09PSAn6LK344GEJyA/ICdidXknIDogJ3dhdGNoJzsKICBjb25zdCBqdWRnZUxhYmVsID0gdG9wLmp1ZGdlIHx8ICfigJQnOwoKICAvLyBFVuihqOekugogIGNvbnN0IGV2U3RyID0gdG9wLmV2IHx8ICcnOwogIGNvbnN0IGV2RGlzcGxheSA9IGV2U3RyID8gYEVWICR7ZXZTdHJ9YCA6ICcnOwoKICAvLyDmnKzlkb3ooajnpLoKICBjb25zdCBob25tZWkgPSB0b3AuaG9ubWVpIHx8ICcnOwoKICBsZXQgaHRtbCA9ICcnOwoKICBjb25zdCBpc0dyYWRlZCA9IHRvcC5pc19ncmFkZWQgfHwgWydHMScsJ0cyJywnRzMnLCdTRycsJ0dQJywnSkcxJywnSkcyJywnSkczJ10uaW5jbHVkZXModG9wLmdyYWRlKTsKICAvLyDph43os57jga/opovpgIHjgorjgafjgoLmnKzlkb3jg7vlsZXplovjgpLooajnpLrvvIjmg4XloLHmj5DkvpvjgajjgZfjgabvvIkKICBpZiAoaG9ubWVpICYmICh0b3AuanVkZ2UgIT09ICfopovpgIHjgoonIHx8IGlzR3JhZGVkKSkgewogICAgY29uc3QgZ3JhZGVkTm90ZSA9IChpc0dyYWRlZCAmJiB0b3AuanVkZ2UgPT09ICfopovpgIHjgoonKSA/ICc8c3BhbiBjbGFzcz0iZXYtYmFkZ2Ugd2F0Y2giIHN0eWxlPSJmb250LXNpemU6MC43cmVtO21hcmdpbi1sZWZ0OjRweCI+RVbmnaHku7blpJbjg7vlj4LogIM8L3NwYW4+JyA6ICcnOwogICAgaHRtbCArPSBgCiAgICAgIDxkaXYgY2xhc3M9Imhvbm1laS1yb3ciPgogICAgICAgIDxzcGFuIGNsYXNzPSJob25tZWktbWFyayI+4peOPC9zcGFuPgogICAgICAgIDxzcGFuIGNsYXNzPSJob25tZWktbmFtZSI+JHtob25tZWl9PC9zcGFuPgogICAgICAgICR7ZXZEaXNwbGF5ID8gYDxzcGFuIGNsYXNzPSJldi1iYWRnZSAke2p1ZGdlQ2xhc3N9Ij4ke2V2RGlzcGxheX08L3NwYW4+YCA6ICcnfQogICAgICAgIDxzcGFuIGNsYXNzPSJldi1iYWRnZSAke2p1ZGdlQ2xhc3N9Ij4ke2p1ZGdlTGFiZWx9PC9zcGFuPgogICAgICAgICR7Z3JhZGVkTm90ZX0KICAgICAgPC9kaXY+YDsKICAgIGlmICh0b3AucmVhc29uKSB7CiAgICAgIGh0bWwgKz0gYDxkaXYgY2xhc3M9InJlYXNvbi10ZXh0Ij4ke3RvcC5yZWFzb259PC9kaXY+YDsKICAgIH0KICB9IGVsc2UgewogICAgaHRtbCArPSBgPGRpdiBjbGFzcz0ibm8tcGljayI+5pys5pel44Gv5o6o5aWo44Os44O844K544Gq44GX77yI6KaL6YCB44KK77yJPC9kaXY+YDsKICB9CiAgLy8g6Kmz57Sw44Oq44Oz44KvCiAgaHRtbCArPSBgPGEgY2xhc3M9ImRldGFpbC1saW5rIiBocmVmPSIke20udXJsfSI+6Kmz57Sw44Gq5LqI5oOz44O76LK344GE55uu44KS6KaL44KLIOKAujwvYT5gOwoKICBib2R5RWwuaW5uZXJIVE1MID0gaHRtbDsKfQoKLy8g4pWQ4pWQIOWFqOODrOODvOOCueS4gOimp+aPj+eUuyDilZDilZAKZnVuY3Rpb24gcmVuZGVyQWxsUmFjZXMocmFjZXMpIHsKICBjb25zdCBlbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdhbGwtcmFjZXMtbGlzdCcpOwogIGlmICghcmFjZXMubGVuZ3RoKSB7CiAgICBlbC5pbm5lckhUTUwgPSAnPGRpdiBjbGFzcz0iZW1wdHkiPuacrOaXpeOBruODrOODvOOCueaDheWgseOCkuiqreOBv+i+vOOBv+S4reOBp+OBmTwvZGl2Pic7CiAgICByZXR1cm47CiAgfQoKICAvLyDjgrDjg6zjg7zjg4njg7tFVumghuOBp+OCveODvOODiAogIGNvbnN0IHNvcnRlZCA9IFsuLi5yYWNlc10uc29ydCgoYSxiKSA9PiB7CiAgICBjb25zdCBqU2NvcmUgPSB75by36LK344GEOjAsIOiyt+OBhDoxfVthLmp1ZGdlXSA/PyAyOwogICAgY29uc3QgalNjb3JlQiA9IHvlvLfosrfjgYQ6MCwg6LK344GEOjF9W2IuanVkZ2VdID8/IDI7CiAgICBpZiAoalNjb3JlICE9PSBqU2NvcmVCKSByZXR1cm4galNjb3JlIC0galNjb3JlQjsKICAgIHJldHVybiBncmFkZVNjb3JlKGEpIC0gZ3JhZGVTY29yZShiKSB8fCBldk51bShiKSAtIGV2TnVtKGEpOwogIH0pOwoKICBlbC5pbm5lckhUTUwgPSBzb3J0ZWQubWFwKHIgPT4gewogICAgY29uc3QgbSA9IFNQT1JUX01FVEFbci5zcG9ydF0gfHwge2ljb246J/Cfj4EnLCBsYWJlbDpyLnNwb3J0LCB1cmw6JyMnLCBjbHM6J2hvcnNlJ307CiAgICBjb25zdCBqdWRnZUNsYXNzID0gci5qdWRnZSA9PT0gJ+W8t+iyt+OBhCcgPyAnc3Ryb25nJyA6IHIuanVkZ2UgPT09ICfosrfjgYQnID8gJ2J1eScgOiAnd2F0Y2gnOwogICAgY29uc3QganVkZ2VMYWJlbCA9IHIuanVkZ2UgfHwgJ+KAlCc7CiAgICBjb25zdCBldlN0ciA9IHIuZXYgPyBgKyR7ci5ldi5yZXBsYWNlKCcrJywnJyl9YCA6ICcnOwogICAgY29uc3QgcmFjZU5hbWUgPSByLm5hbWUgfHwgci52ZW51ZSB8fCAnJzsKCiAgICBjb25zdCBpc0dyYWRlZFJhY2UgPSByLmlzX2dyYWRlZCB8fCBbJ0cxJywnRzInLCdHMycsJ1NHJywnR1AnLCdKRzEnLCdKRzInLCdKRzMnXS5pbmNsdWRlcyhyLmdyYWRlKTsKICAgIGNvbnN0IGdyYWRlQmFkZ2UgPSBpc0dyYWRlZFJhY2UgPyBgPHNwYW4gY2xhc3M9InJpLWdyYWRlLWJhZGdlIj4ke3IuZ3JhZGUgfHwgJ+mHjeiznid9PC9zcGFuPmAgOiAnJzsKICAgIHJldHVybiBgPGEgY2xhc3M9InJhY2UtaXRlbSAke20uY2xzfSR7aXNHcmFkZWRSYWNlID8gJyBncmFkZWQnIDogJyd9IiBocmVmPSIke20udXJsfSI+CiAgICAgIDxzcGFuIGNsYXNzPSJyaS10aW1lIj4ke3IudGltZSB8fCAnLS06LS0nfTwvc3Bhbj4KICAgICAgPHNwYW4gY2xhc3M9InJpLXZlbnVlIj4ke3IudmVudWUgfHwgJyd9PC9zcGFuPgogICAgICA8c3BhbiBjbGFzcz0icmktbmFtZSI+JHttLmljb259ICR7cmFjZU5hbWV9JHtncmFkZUJhZGdlfTwvc3Bhbj4KICAgICAgPHNwYW4gY2xhc3M9InJpLWp1ZGdlICR7anVkZ2VDbGFzc30iPiR7anVkZ2VMYWJlbH08L3NwYW4+CiAgICAgICR7ZXZTdHIgPyBgPHNwYW4gY2xhc3M9InJpLWV2Ij4ke2V2U3RyfTwvc3Bhbj5gIDogJzxzcGFuIGNsYXNzPSJyaS1ldiIgc3R5bGU9ImNvbG9yOnZhcigtLXRleHQtbXV0ZSkiPuKAlDwvc3Bhbj4nfQogICAgICA8c3BhbiBjbGFzcz0icmktYXJyIj7igLo8L3NwYW4+CiAgICA8L2E+YDsKICB9KS5qb2luKCcnKTsKfQoKLy8g4pWQ4pWQIOWun+e4vuODh+ODvOOCv+iqreOBv+i+vOOBvyDilZDilZAKYXN5bmMgZnVuY3Rpb24gbG9hZFN0YXRzKCkgewogIHRyeSB7CiAgICBjb25zdCByZXMgPSBhd2FpdCBmZXRjaCgncmVzdWx0cy5qc29uP3Y9JyArIERhdGUubm93KCkpOwogICAgaWYgKCFyZXMub2spIHJldHVybjsKICAgIGNvbnN0IGpzb24gPSBhd2FpdCByZXMuanNvbigpOwogICAgY29uc3QgcyA9IGpzb24uc3VtbWFyeTsKICAgIGlmIChzKSB7CiAgICAgIGNvbnN0IHJFbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdpZHgtcmVjb3ZlcnknKTsKICAgICAgckVsLnRleHRDb250ZW50ID0gcy5yZWNvdmVyeV9yYXRlICsgJyUnOwogICAgICByRWwuc3R5bGUuY29sb3IgPSBwYXJzZUZsb2F0KHMucmVjb3ZlcnlfcmF0ZSkgPj0gMTAwID8gJ3ZhcigtLWdyZWVuKScgOiAndmFyKC0tZ29sZC1sKSc7CiAgICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdpZHgtaGl0cmF0ZScpLnRleHRDb250ZW50ID0gcy5oaXRfcmF0ZSArICclJzsKICAgICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2lkeC10b3RhbCcpLnRleHRDb250ZW50ICAgPSBzLnRvdGFsOwogICAgfQogICAgY29uc3QgYnMgPSBqc29uLmJ5X3Nwb3J0OwogICAgaWYgKGJzKSB7CiAgICAgIGZvciAoY29uc3Qgc3Agb2YgWydob3JzZScsJ2JvYXQnLCdjeWNsZSddKSB7CiAgICAgICAgY29uc3QgZCA9IGJzW3NwXTsgaWYgKCFkKSBjb250aW51ZTsKICAgICAgICBjb25zdCBoRWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChgc3RhdC0ke3NwfS1oaXRgKTsKICAgICAgICBjb25zdCByRWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChgc3RhdC0ke3NwfS1yZWNgKTsKICAgICAgICBpZiAoaEVsKSB7IGhFbC50ZXh0Q29udGVudCA9IGQuaGl0X3JhdGUgKyAnJSc7IGhFbC5zdHlsZS5jb2xvciA9IHBhcnNlRmxvYXQoZC5oaXRfcmF0ZSkgPj0gNTAgPyAndmFyKC0tZ3JlZW4pJyA6ICcjZmZmJzsgfQogICAgICAgIGlmIChyRWwpIHsgckVsLnRleHRDb250ZW50ID0gZC5yZWNvdmVyeV9yYXRlICsgJyUnOyByRWwuc3R5bGUuY29sb3IgPSBwYXJzZUZsb2F0KGQucmVjb3ZlcnlfcmF0ZSkgPj0gMTAwID8gJ3ZhcigtLWdyZWVuKScgOiAndmFyKC0tZ29sZC1sKSc7IH0KICAgICAgfQogICAgfQogIH0gY2F0Y2gge30KfQoKLy8g4pWQ4pWQIHJhY2VzLmpzb24g6Kqt44G/6L6844G/IOKVkOKVkAphc3luYyBmdW5jdGlvbiBsb2FkUmFjZXMoKSB7CiAgdHJ5IHsKICAgIGNvbnN0IHJlcyA9IGF3YWl0IGZldGNoKCdyYWNlcy5qc29uP3Y9JyArIERhdGUubm93KCkpOwogICAgaWYgKCFyZXMub2spIHRocm93IG5ldyBFcnJvcignbm90IGZvdW5kJyk7CiAgICBjb25zdCBqc29uID0gYXdhaXQgcmVzLmpzb24oKTsKICAgIGlmICghanNvbiB8fCAhanNvbi5yYWNlcyB8fCAhanNvbi5yYWNlcy5sZW5ndGgpIHJldHVybjsKCiAgICBjb25zdCByYWNlcyA9IGpzb24ucmFjZXM7CgogICAgLy8g5pu05paw44OQ44OD44K4CiAgICBjb25zdCBiYWRnZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd1cGRhdGUtYmFkZ2UnKTsKICAgIGlmIChiYWRnZSAmJiBqc29uLmRhdGUpIHsKICAgICAgY29uc3QgZCA9IG5ldyBEYXRlKGpzb24uZGF0ZSk7CiAgICAgIGNvbnN0IHRkID0gbmV3IERhdGUoKTsgdGQuc2V0SG91cnMoMCwwLDAsMCk7CiAgICAgIGNvbnN0IGlzVG9kYXkgPSBkLnRvRGF0ZVN0cmluZygpID09PSB0ZC50b0RhdGVTdHJpbmcoKTsKICAgICAgYmFkZ2UuaW5uZXJIVE1MID0gaXNUb2RheQogICAgICAgID8gJzxzcGFuIHN0eWxlPSJkaXNwbGF5OmlubGluZS1ibG9jazt3aWR0aDo3cHg7aGVpZ2h0OjdweDtiYWNrZ3JvdW5kOnZhcigtLWdyZWVuKTtib3JkZXItcmFkaXVzOjUwJTttYXJnaW4tcmlnaHQ6NnB4O2FuaW1hdGlvbjpibGluayAycyBpbmZpbml0ZSI+PC9zcGFuPuacrOaXpeabtOaWsOa4iOOBvyDCtyBFVuS6iOaDsycKICAgICAgICA6IGA8c3BhbiBzdHlsZT0iZGlzcGxheTppbmxpbmUtYmxvY2s7d2lkdGg6N3B4O2hlaWdodDo3cHg7YmFja2dyb3VuZDp2YXIoLS10ZXh0LW11dGUpO2JvcmRlci1yYWRpdXM6NTAlO21hcmdpbi1yaWdodDo2cHgiPjwvc3Bhbj4ke2QuZ2V0TW9udGgoKSsxfS8ke2QuZ2V0RGF0ZSgpfSDmm7TmlrAgwrcgRVbkuojmg7NgOwoKICAgICAgaWYgKGlzVG9kYXkpIHsKICAgICAgICBjb25zdCBsaXZlRWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndG9kYXktbGl2ZS1iYWRnZScpOwogICAgICAgIGlmIChsaXZlRWwpIGxpdmVFbC5zdHlsZS5kaXNwbGF5ID0gJyc7CiAgICAgIH0KICAgIH0KCiAgICAvLyDnq7bmioDliKXjgrvjgq/jgrfjg6fjg7Pmj4/nlLsKICAgIGZvciAoY29uc3Qgc3BvcnQgb2YgWydob3JzZScsJ2JvYXQnLCdjeWNsZSddKSB7CiAgICAgIHJlbmRlclNwb3J0U2VjdGlvbihzcG9ydCwgcmFjZXMpOwogICAgfQoKICAgIC8vIOWFqOODrOODvOOCueS4gOimp+aPj+eUuwogICAgcmVuZGVyQWxsUmFjZXMocmFjZXMpOwoKICB9IGNhdGNoKGUpIHsKICAgIGNvbnNvbGUud2FybigncmFjZXMuanNvbiDoqq3jgb/ovrzjgb/jgqjjg6njg7w6JywgZSk7CiAgICBmb3IgKGNvbnN0IHNwb3J0IG9mIFsnaG9yc2UnLCdib2F0JywnY3ljbGUnXSkgewogICAgICBjb25zdCBib2R5RWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChgYm9keS0ke3Nwb3J0fWApOwogICAgICBpZiAoYm9keUVsKSBib2R5RWwuaW5uZXJIVE1MID0gJzxkaXYgY2xhc3M9Im5vLXBpY2siPuODh+ODvOOCv+iqreOBv+i+vOOBv+OCqOODqeODvDwvZGl2Pic7CiAgICB9CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYWxsLXJhY2VzLWxpc3QnKS5pbm5lckhUTUwgPSAnPGRpdiBjbGFzcz0iZW1wdHkiPuODh+ODvOOCv+OCkuiqreOBv+i+vOOCgeOBvuOBm+OCk+OBp+OBl+OBnzwvZGl2Pic7CiAgfQp9CgovLyDilZDilZAgU05T5YWx5pyJIOKVkOKVkApjb25zdCBQQUdFX1VSTCA9IGVuY29kZVVSSUNvbXBvbmVudChsb2NhdGlvbi5ocmVmKTsKY29uc3QgWF9URVhUID0gZW5jb2RlVVJJQ29tcG9uZW50KCfjgJDkuojmg7Pjga7piYTliYfjgJHmnKzml6Xjga7nq7bppqzjg7vnq7boiYfjg7vnq7bovKrms6jnm67jg6zjg7zjgrnkuojmg7PjgpLlhazplovkuK3vvIFFVuioiOeul+OBp+agueaLoOOBguOCi+S6iOaDs+OCkuODgeOCp+ODg+OCr/CfkYcgI+ertummrOS6iOaDsyAj56u26ImH5LqI5oOzICPnq7bovKrkuojmg7MgI+S6iOaDs+OBrumJhOWJhycpOwpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc2hhcmUteCcpLmhyZWYgPSBgaHR0cHM6Ly90d2l0dGVyLmNvbS9pbnRlbnQvdHdlZXQ/dGV4dD0ke1hfVEVYVH0mdXJsPSR7UEFHRV9VUkx9YDsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3NoYXJlLWxpbmUnKS5ocmVmID0gYGh0dHBzOi8vc29jaWFsLXBsdWdpbnMubGluZS5tZS9saW5laXQvc2hhcmU/dXJsPSR7UEFHRV9VUkx9YDsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3NoYXJlLWNvcHknKS5hZGRFdmVudExpc3RlbmVyKCdjbGljaycsICgpID0+IHsKICBuYXZpZ2F0b3IuY2xpcGJvYXJkLndyaXRlVGV4dChsb2NhdGlvbi5ocmVmKS50aGVuKCgpID0+IHsKICAgIGNvbnN0IGJ0biA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzaGFyZS1jb3B5Jyk7CiAgICBidG4udGV4dENvbnRlbnQgPSAn4pyFIOOCs+ODlOODvOOBl+OBvuOBl+OBnyc7CiAgICBidG4uY2xhc3NMaXN0LmFkZCgnc2hhcmUtY29waWVkJyk7CiAgICBzZXRUaW1lb3V0KCgpID0+IHsgYnRuLnRleHRDb250ZW50ID0gJ/CflJcgVVJM44Kz44OU44O8JzsgYnRuLmNsYXNzTGlzdC5yZW1vdmUoJ3NoYXJlLWNvcGllZCcpOyB9LCAyMDAwKTsKICB9KTsKfSk7CgovLyDilZDilZAg5Yid5pyf5YyWIOKVkOKVkApsb2FkU3RhdHMoKTsKbG9hZFJhY2VzKCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K"

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
    # races.jsonは全データを含める（all_horses_ev等も含む）
    output = {"date": today_str, "races": all_races, "line_message": line_message}
    with open("races.json","w",encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=True, indent=2)
    print(f"races.json生成完了（{len(all_races)}件）")

    # races_summary.json（軽量版・index.html用）
    try:
        races_summary = []
        for r in all_races:
            races_summary.append({
                "sport":        r.get("sport",""),
                "name":         r.get("name",""),
                "venue":        r.get("venue",""),
                "time":         r.get("time",""),
                "grade":        r.get("grade",""),
                "honmei":       r.get("honmei",""),
                "ev":           r.get("ev",""),
                "judge":        r.get("judge",""),
                "reason":       r.get("reason",""),
                "url":          r.get("url",""),
                "pace_summary": r.get("pace_summary",""),
                "race_summary": r.get("race_summary",""),
                "line_visual":  r.get("line_visual",""),
            })
        with open("races_summary.json","w",encoding="utf-8") as f:
            json.dump({"date": today_str, "races": races_summary}, f, ensure_ascii=True, indent=2)
        print(f"races_summary.json生成完了（{len(races_summary)}件）")
    except Exception as e:
        print(f"⚠️ races_summary.json生成エラー: {e}")

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
                    "sport":         r["sport"],
                    "icon":          sport_icon.get(r["sport"],"🏁"),
                    "sport_name":    sport_name.get(r["sport"],r["sport"]),
                    "venue":         r.get("venue",""),
                    "time":          r.get("time","--:--"),
                    "grade":         r.get("grade",""),
                    "honmei":        r.get("honmei",""),
                    "ev":            r.get("ev",""),
                    "judge":         r.get("judge",""),
                    "reason":        r.get("reason",""),
                    "url":           r.get("url",""),
                    # 全選手EV一覧（競技別）
                    "all_horses_ev": r.get("all_horses_ev", []),
                    "all_riders_ev": r.get("all_riders_ev", []),
                    # 連勝式推奨買い目
                    "combo_summary": r.get("combo_summary", {}),
                    # 展開予想
                    "pace_summary":  r.get("pace_summary",""),
                    "race_summary":  r.get("race_summary",""),
                    # 競輪ライン可視化
                    "line_visual":   r.get("line_visual",""),
                    # 直前情報フラグ（直前オッズ再取得が必要な場合true）
                    "needs_refresh": r.get("needs_refresh", False),
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
