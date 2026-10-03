# -*- coding: utf-8 -*-
"""Отправка Telegram-уведомлений о сигналах (запускается после update.py).

Читает data.json, для каждого нового сигнала:
  1. Строит PNG-график часовых закрытий пары (matplotlib).
  2. Отправляет фото с подписью через Telegram Bot API.

CHAT_ID: если секрет CHAT_ID пуст, берётся последний chat.id из getUpdates
(пользователь должен один раз написать боту) и сохраняется в chat_id.txt,
который workflow коммитит в репозиторий.
"""

import json
import os
import urllib.request

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
CHAT_FILE = "chat_id.txt"
DATA_PATH = "data.json"
CHART_POINTS = 300  # последние часовые бары на графике

TYPE_LABEL = {"open": "🟢 РАСХОЖДЕНИЕ — сигнал ОТКРЫТИЕ",
              "close": "🔴 СУЖЕНИЕ — сигнал ЗАКРЫТИЕ"}


def api(method, payload=None, timeout=30):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def discover_chat_id():
    """Возвращает (chat_id, fresh): fresh=True, если id найден через getUpdates."""
    try:
        with open(CHAT_FILE, encoding="utf-8") as f:
            saved = f.read().strip()
            if saved:
                return saved, False
    except OSError:
        pass
    upd = api("getUpdates")
    chats = []
    for u in upd.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        cid = (msg.get("chat") or {}).get("id")
        if cid is not None:
            chats.append(cid)
    if not chats:
        # диагностика: если у бота настроен webhook, getUpdates всегда пуст
        try:
            wh = api("getWebhookInfo").get("result", {})
            print("getUpdates пуст. Webhook бота:", wh.get("url") or "(не задан)",
                  "| pending_updates:", wh.get("pending_update_count"))
        except Exception as e:
            print("getUpdates пуст, getWebhookInfo недоступен:", e)
        return "", False
    cid = str(chats[-1])
    with open(CHAT_FILE, "w", encoding="utf-8") as f:
        f.write(cid)
    return cid, True


def make_chart(group, sig, candles, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from datetime import datetime

    a, b = sig["a"], sig["b"]
    sa = candles.get(a, [])[-CHART_POINTS:]
    sb = candles.get(b, [])[-CHART_POINTS:]
    if not sa or not sb:
        return False

    fig, ax = plt.subplots(figsize=(10, 5), dpi=110)
    color_a, color_b = "#2196f3", "#ff9800"
    ta = [datetime.fromtimestamp(t / 1000) for t, _ in sa]
    tb = [datetime.fromtimestamp(t / 1000) for t, _ in sb]
    ax.plot(ta, [c for _, c in sa], color=color_a, lw=1.4, label=a)
    ax.plot(tb, [c for _, c in sb], color=color_b, lw=1.4, label=b)

    # стрелка сигнала в последней общей точке
    common = sorted({t for t, _ in sa} & {t for t, _ in sb})
    if common:
        ts = common[-1]
        va = next((c for t, c in sa if t == ts), None)
        vb = next((c for t, c in sb if t == ts), None)
        if va is not None and vb is not None:
            tsd = datetime.fromtimestamp(ts / 1000)
            marker = "^" if sig["type"] == "open" else "v"
            ax.scatter([tsd], [(va + vb) / 2], marker=marker, s=220, zorder=5,
                       color="#2e7d32" if sig["type"] == "open" else "#c62828")
            ax.annotate(f'{sig.get("spreadPct", 0):.2f}%', (tsd, (va + vb) / 2),
                        textcoords="offset points", xytext=(0, 14), ha="center",
                        fontsize=10, fontweight="bold")

    ax.set_title(f'{group["title"]}: {a} / {b}', fontsize=11)
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return True


def send_photo(chat_id, path, caption):
    boundary = "----moexdash"
    with open(path, "rb") as f:
        png = f.read()
    body = b""
    for name, value in (("chat_id", chat_id), ("caption", caption)):
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{name}"\r\n\r\n{value}\r\n').encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
             f'filename="chart.png"\r\nContent-Type: image/png\r\n\r\n').encode("utf-8")
    body += png + f"\r\n--{boundary}--\r\n".encode("utf-8")

    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    if not TOKEN:
        print("TELEGRAM_TOKEN не задан — пропускаю уведомления.")
        return

    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)

    fresh_discover = False
    if CHAT_ID:
        chat_id = CHAT_ID
    else:
        chat_id, fresh_discover = discover_chat_id()
    if not chat_id:
        print("CHAT_ID неизвестен: напишите боту любое сообщение в Telegram "
              "и перезапустите workflow.")
        return
    if fresh_discover:
        api("sendMessage", {"chat_id": chat_id,
            "text": "✅ Бот подключен к дашборду «Фьючерсы МосБиржа». "
                    "Сигналы расхождения (>2%) и сужения (<1%) спреда "
                    "будут приходить сюда с графиком."})
        print("chat_id обнаружен, тестовое сообщение отправлено.")

    notes = data.get("notifications", [])
    if not notes:
        print("Новых сигналов нет.")
        return

    group_by_key = {g["key"]: g for g in data.get("groups", [])}
    for sig in notes:
        g = group_by_key.get(sig["group"], {})
        candles = g.get("candles", {})
        caption = (f'{TYPE_LABEL.get(sig["type"], sig["type"])}\n'
                   f'{sig["title"]}: {sig["a"]} / {sig["b"]}\n'
                   f'Спред: {sig.get("spreadPct", "?")}%')
        chart = "chart.png"
        try:
            if make_chart(g, sig, candles, chart):
                send_photo(chat_id, chart, caption)
            else:
                api("sendMessage", {"chat_id": chat_id, "text": caption})
            print("Отправлено:", sig["a"], "/", sig["b"])
        except Exception as e:
            print("Ошибка отправки:", e)


if __name__ == "__main__":
    main()
