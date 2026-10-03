# -*- coding: utf-8 -*-
"""Сбор котировок фьючерсов МосБиржа (бесплатный ISS API) для GitHub-дашборда.

Каждый запуск:
  1. Забирает список действующих контрактов FORTS RFUD и текущие цены.
  2. Загружает часовые свечи (ближайшая пара каждой группы — 12 недель,
     остальные — 2 недели).
  3. Считает спреды соседних пар (1-2, 2-3, ...): >2% — «open», <1% — «close».
  4. Сравнивает состояния с прошлым запуском (signals_state.json) и
     складывает новые сигналы в data.json для notify.py.
  5. Пишет data.json — единый источник для index.html.
"""

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))

OPEN_PCT = 2.0
CLOSE_PCT = 1.0
FRONT_DAYS = 84        # 12 недель для ближайшей пары
OTHER_DAYS = 14
PAGE = 500
MAX_PAGES_FRONT = 4
MAX_PAGES_OTHER = 1

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


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "moex-futures-dashboard/1.0"})
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


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


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


def current_spread(series_near, series_far):
    near = {t: c for t, c in series_near}
    far = {t: c for t, c in series_far}
    common = sorted(set(near) & set(far))
    if not common:
        return None
    ts = common[-1]
    if not near[ts]:
        return None
    return abs(far[ts] - near[ts]) / near[ts] * 100.0


def spread_signals(series_near, series_far):
    """События перехода спреда по общим часам и финальное состояние."""
    near = {t: c for t, c in series_near}
    far = {t: c for t, c in series_far}
    ts_all = sorted(set(near) & set(far))
    if len(ts_all) < 2:
        return [], "none"
    events = []
    state = "none"
    for i, ts in enumerate(ts_all):
        if not near[ts]:
            continue
        sp = abs(far[ts] - near[ts]) / near[ts] * 100.0
        new = state
        if sp > OPEN_PCT:
            new = "open"
        elif sp < CLOSE_PCT:
            new = "close"
        if new != state:
            events.append({"type": new, "bar": i, "ts": ts, "spreadPct": round(sp, 3)})
            state = new
    return events, state


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

    prev_state = load_state()
    had_prev = bool(prev_state.get("states"))
    new_notifications = []
    next_states = {}
    groups_out = []

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
        for idx, c in enumerate(contracts):
            deep = idx < 2
            closes = hourly_closes(c["secid"],
                                   FRONT_DAYS if deep else OTHER_DAYS,
                                   MAX_PAGES_FRONT if deep else MAX_PAGES_OTHER)
            candles[c["secid"]] = closes
            close = closes[-1][1] if closes else c["last"]
            futures.append({
                "symbol": c["secid"],
                "name": c["shortname"],
                "expiry": c["shortname"].split("-")[-1] if "-" in c["shortname"] else "",
                "close": close,
                "last": c["last"],
                "changePct": c["changePct"],
                "volume": c["volume"] if c["volume"] is not None else 0,
                "signal": "",
                "deep": deep,
            })
            time.sleep(0.1)

        # сигналы для ВСЕХ соседних пар; окраска строк — по текущему спреду
        signals = []
        pair_states = {}
        disp = [set() for _ in futures]
        for i in range(len(futures) - 1):
            a = futures[i]["symbol"]
            b = futures[i + 1]["symbol"]
            ca = candles.get(a, [])
            cb = candles.get(b, [])
            events, st = spread_signals(ca, cb)
            for e in events:
                signals.append({"type": e["type"], "a": a, "b": b,
                                "bar": e["bar"], "ts": e["ts"], "spreadPct": e["spreadPct"]})
            pair_states[a + "|" + b] = st
            cur = current_spread(ca, cb)
            if cur is not None:
                if cur > OPEN_PCT:
                    disp[i].add("open")
                    disp[i + 1].add("open")
                elif cur < CLOSE_PCT:
                    disp[i].add("close")
                    disp[i + 1].add("close")
        for i, s in enumerate(disp):
            if "open" in s:
                futures[i]["signal"] = "open"
            elif "close" in s:
                futures[i]["signal"] = "close"

        # уведомления: переход состояния любой соседней пары
        prev_pairs = prev_state.get("states", {}).get(prefix, {})
        if isinstance(prev_pairs, str):
            prev_pairs = {}
        next_states[prefix] = pair_states
        if had_prev:
            for pk, st in pair_states.items():
                if st != "none" and prev_pairs.get(pk) != st:
                    a, b = pk.split("|")
                    cur = current_spread(candles.get(a, []), candles.get(b, []))
                    new_notifications.append({
                        "group": prefix, "title": title, "type": st,
                        "a": a, "b": b,
                        "spreadPct": round(cur, 3) if cur is not None else None,
                    })

        groups_out.append({
            "key": prefix, "title": title,
            "futures": futures, "candles": candles, "signals": signals,
        })

    now = datetime.now(MSK)
    payload = {
        "states": next_states,
        "updatedAt": now.isoformat(timespec="seconds"),
    }
    if had_prev:
        payload["notify"] = new_notifications
        save_state(payload)
    else:
        save_state({"states": next_states, "updatedAt": payload["updatedAt"]})

    artifact = {
        "updatedAt": now.strftime("%d.%m.%Y %H:%M MSK"),
        "source": "MOEX ISS (FORTS RFUD): часовые свечи",
        "historyWeeks": FRONT_DAYS // 7,
        "groups": groups_out,
        "notifications": new_notifications if had_prev else [],
    }
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False)
    print("OK:", now.strftime("%d.%m.%Y %H:%M MSK"),
          "| groups:", len(groups_out),
          "| notifications:", len(artifact["notifications"]))


if __name__ == "__main__":
    main()
