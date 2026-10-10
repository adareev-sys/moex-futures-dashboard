# -*- coding: utf-8 -*-
"""Сбор котировок и гибридные сигналы календарного спреда (MOEX ISS) для дашборда.

Логика сигналов — бэктест «Анализ календарного спреда», промт v2, без изменений:
  - режим СП (спокойный): вход P78 / выход P60, окно 40 ч, лимит 12 ч;
  - режим ВОЛ:          вход P85 / выход P60, окно 120 ч, лимит 96 ч;
  - переключение по sigma_rel(120) vs медиана sigma_rel (до 2000 баров, min 200);
  - rel = |far - near| / near; направление = знак спреда на входе;
  - издержки 16 RUB на круг; стоимость пункта по спецификациям MOEX.

Каждый запуск:
  1. Забирает действующие контракты FORTS RFUD и часовые свечи (12 недель).
  2. Для каждой соседней пары прогоняет детерминированный replay полной серии.
  3. Ведёт журнал позиций (positions.json): открытые и закрытые сделки с P&L.
  4. Формирует уведомления о НОВЫХ событиях входа/выхода (watchlist + throttle 2 ч).
  5. Пишет data.json — единый источник для index.html и notify.py.

ВАЖНО: окно истории дашборда — 84 дня (~600 баров), в бэктесте — с 2019 года.
Медиана sigma в дашборде считается по доступной истории (min 200 валидных
значений), поэтому режим ВОЛ может включаться чуть иначе, чем в бэктесте.
"""

import json
import os
import re
import statistics
import time
import urllib.request
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))

# ---------------------------------------------------------------- конфигурация
# Гибридные параметры — ЕДИНЫЕ для всех инструментов (промт v2).
QUIET = {"qe": 78, "qx": 60, "win": 40, "lim": 12}    # «СП» — спокойный режим
VOLA = {"qe": 85, "qx": 60, "win": 120, "lim": 96}    # «ВОЛ» — взвешенный режим
SIG_WIN = 120        # окно волатильности спреда, баров
SIG_MED = 2000       # окно медианы волатильности, баров
MIN_HIST = 20        # минимум истории для перцентиля
COST = 16.0          # издержки на круг (комиссия + проскальзывание), RUB
THROTTLE_MS = 2 * 3600 * 1000   # не чаще одного сообщения на пару в 2 часа

# Стоимость 1.0 цены контракта в RUB: ("fx", k) — k × USDRUB; ("rub", k) — k.
# USDRUB = цена ближнего Si / 1000 (fallback 65).
POINT_VALUE = {
    "BR": ("fx", 10.0),     # 10 USD × курс
    "NG": ("fx", 100.0),    # 100 USD × курс
    "GD": ("fx", 1.0),      # 1 USD × курс (GOLD)
    "Si": ("rub", 1000.0),  # лот 1000 USD: 1.0 цены = 1000 RUB
    "MX": ("rub", 1.0),     # MIX: 1 RUB / пункт
    "CR": ("rub", 1000.0),  # 1000 RUB за 1.0 цены
}

TICKER = {"BR": "BR", "NG": "NG", "Si": "Si", "MX": "MIX", "GD": "GOLD", "CR": "CNY"}

HISTORY_DAYS = 84        # 12 недель часовых свечей для всех контрактов
PAGE = 500
MAX_PAGES = 4

GROUPS = [
    ("BR", "Нефть Brent"),
    ("NG", "Природный газ"),
    ("Si", "USD/RUB"),
    ("MX", "Индекс МосБиржи"),
    ("GD", "Золото"),
    ("CR", "CNY/RUB"),
]

LIST_URL = (
    "https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/"
    "securities.json?iss.meta=off"
    "&securities.columns=SECID,SHORTNAME"
    "&marketdata.columns=SECID,LAST,LASTCHANGEPRCNT,VOLTODAY"
)
CANDLE_URL = (
    "https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}/"
    "candles.json?iss.meta=off&interval=60&from={date}&start={start}"
)

STATE_PATH = "signals_state.json"
DATA_PATH = "data.json"
POS_PATH = "positions.json"
WATCH_PATH = "watchlist.json"


# ------------------------------------------------------------------- утилиты
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "moex-futures-dashboard/2.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_expiry(shortname):
    m = re.search(r"-(\d{1,2})\.(\d{2})$", shortname or "")
    if not m:
        return (9999, 99)
    return (2000 + int(m.group(2)), int(m.group(1)))


def iso_to_ms(s):
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MSK)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError, OSError):
        return 0


def ms_to_str(ts):
    return datetime.fromtimestamp(ts / 1000, MSK).strftime("%d.%m %H:%M")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def hourly_closes(secid, days, max_pages):
    from_date = (datetime.now(MSK) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for page in range(max_pages):
        try:
            data = fetch_json(CANDLE_URL.format(secid=secid, date=from_date, start=page * PAGE)).get("candles", {})
        except Exception:
            break
        cols = {name: i for i, name in enumerate(data.get("columns", []))}
        ib, ic = cols.get("begin"), cols.get("close")
        rows = data.get("data", [])
        if ib is None or ic is None or not rows:
            break
        for row in rows:
            if len(row) <= max(ib, ic):
                continue
            close = to_num(row[ic])
            if close is None:
                continue
            out.append([iso_to_ms(row[ib]), close])
        if len(rows) < PAGE:
            break
        time.sleep(0.1)
    return out


def percentile(vals, p):
    """Линейная интерполяция перцентиля p (0–100) — как numpy.percentile."""
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


# ------------------------------------------------------- гибридный движок пар
def run_engine(series):
    """Детерминированный replay серии (ts, pn, pf) — как run_multi.py / промт v2.

    Возвращает (trades, open_pos, last_regime, last_thresholds, sigma, sigma_med),
    где trades — закрытые сделки, open_pos — открытая позиция на последнем баре
    (или None). Спред в долях (не %), пороги тоже.
    """
    rels, sig_rel = [], []
    pos = None
    trades = []
    for i, (ts, pn, pf) in enumerate(series):
        rel = abs(pf - pn) / pn if pn else 0.0
        last = i == len(series) - 1
        s_rel = statistics.pstdev(rels[-SIG_WIN:]) if len(rels) >= SIG_WIN else None
        sig_rel.append(s_rel)
        recent = [x for x in sig_rel[-SIG_MED:] if x is not None]
        cfg = VOLA if (s_rel is not None and len(recent) >= 200
                       and s_rel > statistics.median(recent)) else QUIET
        hist = rels[-cfg["win"]:]
        if pos is None:
            if (len(hist) >= MIN_HIST and rel >= percentile(hist, cfg["qe"])
                    and not last and i + cfg["lim"] < len(series) - 1):
                pos = {"i": i, "ts": ts, "s": pf - pn, "pn": pn, "pf": pf,
                       "bars": 0, "cfg": dict(cfg), "regime": "ВОЛ" if cfg is VOLA else "СП"}
        else:
            pos["bars"] += 1
            whist = rels[-pos["cfg"]["win"]:]
            thrx = percentile(whist, pos["cfg"]["qx"]) if len(whist) >= MIN_HIST else None
            reason = None
            if thrx is not None and rel < thrx:
                reason = "цель"
            elif pos["bars"] >= pos["cfg"]["lim"]:
                reason = f"{pos['cfg']['lim']} час"
            elif last:
                reason = "экспирация"
            if reason:
                direction = 1 if pos["s"] > 0 else -1
                trades.append({
                    "entry_i": pos["i"], "entry_ts": pos["ts"],
                    "entry_near": pos["pn"], "entry_far": pos["pf"],
                    "entry_spread": pos["s"], "direction": direction,
                    "regime": pos["regime"],
                    "exit_i": i, "exit_ts": ts,
                    "exit_near": pn, "exit_far": pf,
                    "exit_spread": pf - pn,
                    "bars": pos["bars"], "reason": reason,
                    "pnl_pts": direction * (pos["s"] - (pf - pn)),
                })
                pos = None
        rels.append(rel)
    # итоговое состояние на последнем баре
    last = len(series) - 1
    s_rel = sig_rel[last] if series else None
    recent = [x for x in sig_rel[-SIG_MED:] if x is not None] if series else []
    cfg = VOLA if (s_rel is not None and len(recent) >= 200
                   and s_rel > statistics.median(recent)) else QUIET
    thr_open = percentile(rels[-cfg["win"]:], cfg["qe"]) if len(rels) >= MIN_HIST else None
    thr_close = percentile(rels[-cfg["win"]:], cfg["qx"]) if len(rels) >= MIN_HIST else None
    regime = "ВОЛ" if cfg is VOLA else "СП"
    sigma_med = statistics.median(recent) if len(recent) >= MIN_HIST else None
    return trades, pos, regime, (thr_open, thr_close), s_rel, sigma_med


def point_value_rub(group, fx):
    kind, k = POINT_VALUE[group]
    return k * fx if kind == "fx" else k


# ----------------------------------------------------------------------- main
def main():
    data = fetch_json(LIST_URL)

    sec_cols = {name: i for i, name in enumerate(data["securities"]["columns"])}
    md_cols = {name: i for i, name in enumerate(data["marketdata"]["columns"])}

    def md_val(row, col):
        i = md_cols.get(col)
        return row[i] if i is not None and len(row) > i else None

    names = {}
    for row in data["securities"]["data"]:
        names[row[sec_cols["SECID"]]] = row[sec_cols["SHORTNAME"]]

    market = {}
    for row in data["marketdata"]["data"]:
        secid = md_val(row, "SECID")
        if secid:
            market[secid] = row

    prev_state = load_json(STATE_PATH, {})
    had_prev = bool(prev_state.get("bars"))
    prev_bars = prev_state.get("bars", {}) if isinstance(prev_state.get("bars"), dict) else {}
    last_notify = prev_state.get("last_notify", {}) if isinstance(prev_state.get("last_notify"), dict) else {}
    watch = load_json(WATCH_PATH, {}).get("pairs", [])
    watch_set = set(watch)

    positions = load_json(POS_PATH, {"open": {}, "closed": [], "seen": []})
    if not isinstance(positions.get("open"), dict):
        positions = {"open": {}, "closed": [], "seen": []}
    seen = set(positions.get("seen", []))
    closed_all = positions.get("closed", [])[-500:]

    new_notifications = []
    next_bars = {}
    groups_out = []
    engine_by_group = {}   # сырые результаты для постобработки P&L

    for prefix, title in GROUPS:
        contracts = []
        for secid, shortname in names.items():
            if not secid.startswith(prefix):
                continue
            row = market.get(secid)
            if not row:
                continue
            last = to_num(md_val(row, "LAST"))
            if last is None:
                continue
            contracts.append({
                "secid": secid,
                "shortname": shortname,
                "expiry": parse_expiry(shortname),
                "last": last,
                "changePct": to_num(md_val(row, "LASTCHANGEPRCNT")) or 0.0,
                "volume": to_num(md_val(row, "VOLTODAY")),
            })
        contracts.sort(key=lambda c: c["expiry"])

        candles = {}
        futures = []
        for c in contracts:
            closes = hourly_closes(c["secid"], HISTORY_DAYS, MAX_PAGES)
            candles[c["secid"]] = closes
            exp = c["shortname"].split("-")[-1] if "-" in c["shortname"] else ""
            futures.append({
                "symbol": c["secid"],
                "name": c["shortname"],
                "short": f"{TICKER.get(prefix, prefix)} {exp}",
                "expiry": exp,
                "close": closes[-1][1] if closes else c["last"],
                "last": c["last"],
                "changePct": c["changePct"],
                "volume": c["volume"] if c["volume"] is not None else 0,
            })
            time.sleep(0.1)

        pair_info = {}
        pair_signals = []
        for i in range(len(futures) - 1):
            a, b = futures[i]["symbol"], futures[i + 1]["symbol"]
            ca = {t: c for t, c in candles.get(a, [])}
            cb = {t: c for t, c in candles.get(b, [])}
            common = sorted(set(ca) & set(cb))
            if len(common) < 2:
                continue
            series = [(ts, ca[ts], cb[ts]) for ts in common]
            trades, open_pos, regime, (thr_o, thr_c), sigma, sigma_med = run_engine(series)
            pk = f"{a}|{b}"
            next_bars[f"{prefix}:{pk}"] = len(series)
            pair_info[pk] = {
                "a": a, "b": b, "sa": futures[i]["short"], "sb": futures[i + 1]["short"],
                "pair_label": f"{futures[i]['short']} / {futures[i + 1]['short'].split(' ', 1)[-1]}",
                "regime": regime, "sigma": sigma, "sigmaMed": sigma_med,
                "open": thr_o * 100 if thr_o is not None else None,
                "close": thr_c * 100 if thr_c is not None else None,
                "current": abs(series[-1][2] - series[-1][1]) / series[-1][1] * 100,
                "trades": trades, "open_pos": open_pos, "last_ts": series[-1][0],
                "last_pn": series[-1][1], "last_pf": series[-1][2],
                "len": len(series),
            }
            # события для уведомлений: новые сделки и открытия позиций
            if had_prev:
                pb = prev_bars.get(f"{prefix}:{pk}", 0)
                for t in trades:
                    if t["exit_i"] >= pb and t["entry_i"] >= pb - 1:
                        pair_signals.append({"kind": "close", "ts": t["exit_ts"], "trade": t})
                if open_pos is not None and open_pos["i"] >= pb:
                    pair_signals.append({"kind": "open", "ts": open_pos["ts"], "pos": open_pos})

        engine_by_group[prefix] = (title, futures, candles, pair_info)
        groups_out.append({"key": prefix, "title": title, "futures": futures,
                           "candles": candles, "pairs": None})  # pairs заполним позже

    # fx из ближайшего Si
    fx = 65.0
    for g in groups_out:
        if g["key"] == "Si" and g["futures"]:
            cl = g["candles"].get(g["futures"][0]["symbol"], [])
            if cl:
                fx = cl[-1][1] / 1000.0

    # ------------------------------------------------------- журнал позиций
    now_ms = int(datetime.now(MSK).timestamp() * 1000)
    for g in groups_out:
        prefix = g["key"]
        title, futures, candles, pair_info = engine_by_group[prefix]
        pv = point_value_rub(prefix, fx)
        g["pointValue"] = pv
        g["pairs"] = []
        for pk, info in pair_info.items():
            # --- закрытые сделки -> журнал
            for t in info["trades"]:
                tid = f"{prefix}:{pk}@{t['entry_ts']}"
                if tid in seen:
                    continue
                seen.add(tid)
                pnl_rub = t["pnl_pts"] * pv - COST
                closed_all.append({
                    "id": tid, "group": prefix, "pair": info['pair_label'],
                    "regime": t["regime"], "direction": t["direction"],
                    "entryTs": t["entry_ts"], "exitTs": t["exit_ts"],
                    "entrySpread": t["entry_spread"], "exitSpread": t["exit_spread"],
                    "entryPx": f"{t['entry_near']:g} / {t['entry_far']:g}",
                    "bars": t["bars"], "reason": t["reason"],
                    "pnlPts": round(t["pnl_pts"], 4), "pnlRub": round(pnl_rub, 0),
                })
                key = f"{prefix}:{pk}"
                if (not watch_set or key in watch_set):
                    prev_sent = last_notify.get(key, 0)
                    if now_ms - prev_sent >= THROTTLE_MS:
                        last_notify[key] = now_ms
                        new_notifications.append({
                            "group": prefix, "title": title, "type": "close",
                            "a": info["a"], "b": info["b"],
                            "pair": info['pair_label'],
                            "ts": t["exit_ts"], "regime": t["regime"],
                            "reason": t["reason"], "spreadPct": round(abs(t["exit_spread"]) / t["exit_near"] * 100, 3),
                            "pnlRub": round(pnl_rub, 0),
                            "entryPx": f"{t['entry_near']:g} / {t['entry_far']:g}",
                        })
            # --- открытая позиция -> журнал + плавающий P&L
            key = f"{prefix}:{pk}"
            if info["open_pos"] is not None:
                p = info["open_pos"]
                s_cur = info["last_pf"] - info["last_pn"]
                fl_pts = p["direction"] * (p["s"] - s_cur)
                open_rec = {
                    "id": f"{key}@{p['ts']}", "group": prefix,
                    "pair": info['pair_label'],
                    "regime": p["regime"], "direction": p["direction"],
                    "entryTs": p["ts"], "entrySpread": p["s"],
                    "entryPx": f"{p['pn']:g} / {p['pf']:g}",
                    "bars": p["bars"], "lim": p["cfg"]["lim"],
                    "curSpread": s_cur,
                    "floatPts": round(fl_pts, 4),
                    "floatRub": round(fl_pts * pv, 0),
                }
                positions["open"][key] = open_rec
                if had_prev and p["i"] >= prev_bars.get(key, 0) and (not watch_set or key in watch_set):
                    prev_sent = last_notify.get(key, 0)
                    if now_ms - prev_sent >= THROTTLE_MS:
                        last_notify[key] = now_ms
                        new_notifications.append({
                            "group": prefix, "title": title, "type": "open",
                            "a": info["a"], "b": info["b"],
                            "pair": info['pair_label'],
                            "ts": p["ts"], "regime": p["regime"],
                            "spreadPct": round(abs(p["s"]) / p["pn"] * 100, 3),
                            "entryPx": f"{p['pn']:g} / {p['pf']:g}",
                        })
            else:
                # позиции нет, а в журнале висит — закрыть по последнему бару (экспирация/конец данных)
                old = positions["open"].pop(key, None)
                if old:
                    s_cur = info["last_pf"] - info["last_pn"]
                    fl_pts = old["direction"] * (old["entrySpread"] - s_cur)
                    closed_all.append({
                        **{k: old[k] for k in ("id", "group", "pair", "regime", "direction",
                                                "entryTs", "entrySpread", "entryPx", "bars")},
                        "exitTs": info["last_ts"],
                        "exitSpread": s_cur, "reason": "экспирация",
                        "pnlPts": round(fl_pts, 4), "pnlRub": round(fl_pts * pv - COST, 0),
                    })
            # --- данные для отображения пары
            g["pairs"].append({
                "a": info["a"], "b": info["b"], "pair": info['pair_label'],
                "regime": info["regime"], "sigma": info["sigma"], "sigmaMed": info["sigmaMed"],
                "spread": round(info["current"], 3),
                "open": round(info["open"], 3) if info["open"] is not None else None,
                "close": round(info["close"], 3) if info["close"] is not None else None,
                "watched": (not watch_set) or (key in watch_set),
            })

    positions["open"] = positions.get("open", {})
    positions["closed"] = closed_all[-500:]
    positions["seen"] = list(seen)[-2000:]
    save_json(POS_PATH, positions)

    # ------------------------------------------------------------------ состояние
    payload = {"bars": next_bars, "last_notify": last_notify,
               "updatedAt": datetime.now(MSK).isoformat(timespec="seconds")}
    if not had_prev:
        new_notifications = []
    save_json(STATE_PATH, payload)

    now = datetime.now(MSK)
    artifact = {
        "updatedAt": now.strftime("%d.%m.%Y %H:%M MSK"),
        "source": "MOEX ISS (FORTS RFUD): часовые свечи",
        "historyWeeks": HISTORY_DAYS // 7,
        "scheme": "hybrid: СП P78/P60/окно40/лимит12 ↔ ВОЛ P85/P60/окно120/лимит96, переключение по σ_rel",
        "cost": COST, "fx": round(fx, 4),
        "watchlist": sorted(watch_set),
        "positions": {"open": list(positions["open"].values()), "closed": positions["closed"]},
        "groups": groups_out,
        "notifications": new_notifications,
    }
    save_json(DATA_PATH, artifact)
    print("OK:", now.strftime("%d.%m.%Y %H:%M MSK"),
          "| groups:", len(groups_out),
          "| open pos:", len(positions["open"]),
          "| closed total:", len(closed_all),
          "| notifications:", len(new_notifications))


if __name__ == "__main__":
    main()
