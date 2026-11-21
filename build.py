#!/usr/bin/env python3
"""Pawkit CS360: package the CRM snapshot + roster and encrypt it for the hosted page.

Usage: python3 -I build.py RAW_JSON
  RAW_JSON = output of refresh.py (or the browser snapshot): keys wa, created, visits, comp, generated.
The passcode is read from ~/.config/pawkit-cs360/passcode (never stored in the repo).
Writes docs/data.js (encrypted: PBKDF2-SHA256 + AES-256-GCM, decrypted in the browser).
"""
import base64, json, os, sys
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

HERE = os.path.dirname(os.path.abspath(__file__))
ITER = 310000
REFRESH_SECONDS = 120  # 21 Nov: fast job (WhatsApp + Live Status) every ~2 min; full run every 15 min. 1/min used up the Zoho API credits.
SGT = timezone(timedelta(hours=8))


def leave_balances(raw, cfg):
    """Leave credits per person and type: the opening balance in Zoho (module Leave_Balances) minus the days of
    time off requested on the page (Leave_Requests) from the opening date on. Approved requests count as used,
    pending ones as held. Rest days (OFF) in the schedule are not counted. The last good copy of the Zoho
    records is kept in raw/bal_cache.json for runs where Zoho can't be read."""
    cache = os.path.join(HERE, "raw", "bal_cache.json")
    rows = raw.get("bal")
    if rows is None:
        try:
            rows = json.load(open(cache))
        except (OSError, ValueError):
            return None
    else:
        with open(cache, "w") as f:
            json.dump(rows, f)
    roster = cfg.get("roster") or {}

    def days(name, a, b):
        if not a:
            return 0
        d, end, n = datetime.strptime(a, "%Y-%m-%d"), datetime.strptime(b or a, "%Y-%m-%d"), 0
        while d <= end:
            if (roster.get(d.strftime("%Y-%m-%d")) or {}).get(name) != "OFF":
                n += 1
            d += timedelta(days=1)
        return n

    reqs = [l for l in (raw.get("leave") or []) if l.get("status") in ("Approved", "Pending")]
    people = {}
    for r in rows:
        if not r.get("name") or not r.get("type"):
            continue
        start = r.get("from") or "2000-01-01"
        used = held = 0
        for l in reqs:
            if l.get("name") == r["name"] and l.get("type") == r["type"] and (l.get("to") or l.get("from") or "") >= start:
                n = days(r["name"], max(l.get("from") or start, start), l.get("to") or l.get("from"))
                if l["status"] == "Approved":
                    used += n
                else:
                    held += n
        opening = float(r.get("open") or 0)
        people.setdefault(r["name"], []).append({"type": r["type"], "of": r.get("allow"), "left": round(opening - used, 2),
                                                 "used": used, "held": held, "since": start})
    order = {"Annual leave": 0, "Sick leave": 1, "Pet care leave": 2, "Childcare leave": 3, "Unpaid leave": 4}
    for v in people.values():
        v.sort(key=lambda b: order.get(b["type"], 9))
    return {"synced": raw.get("generated"), "src": "Zoho Leave Balances", "people": people}


def main():
    raw = json.load(open(sys.argv[1]))
    # config (roster, complaint summaries) is private: it lives outside the public repo.
    # A typo in it must never stop the data: fall back to the last copy that loaded, and only warn on unknown codes.
    cfg_path = os.path.expanduser("~/.config/pawkit-cs360/config.json")
    last_good = cfg_path + ".last-good"
    try:
        cfg = json.load(open(cfg_path))
        codes = cfg["shift_codes"]
        # shifts typed in the Schedule Tracker sheet win over the hand-kept roster, for people shown on the dashboard
        shown = {q["name"] for q in cfg["people"]}
        alias = {"9AM-6PM": "OFFICE", **(cfg.get("_sheet_aliases") or {})}
        for d, row in (raw.get("sheet_roster") or {}).items():
            for n, c in row.items():
                c = " ".join(str(c).upper().split())
                if n in shown and c:
                    cfg["roster"].setdefault(d, {})[n] = alias.get(c, c)
        for d, r in cfg["roster"].items():
            for n, c in list(r.items()):
                if isinstance(c, str) and c.strip() in codes:
                    r[n] = c.strip()
                elif c:
                    print("WARNING unknown shift code %r for %s on %s (shown as typed, counted as not on shift)" % (c, n, d))
        fd = os.open(last_good + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        os.replace(last_good + ".tmp", last_good)
    except (OSError, ValueError, KeyError, AttributeError) as e:
        print("WARNING config.json could not be used (%s: %s); using the last good copy" % (type(e).__name__, e))
        cfg = json.load(open(last_good))
    pw = open(os.path.expanduser("~/.config/pawkit-cs360/passcode")).read().strip()
    # approved leave copied from Payboy (kept in the private config) is listed with the requests made on this page
    pb = (cfg.get("_payboy_leave") or {})
    leave = raw.get("leave")
    if pb.get("rows"):
        leave = (leave or []) + [{"id": "pb%d" % i, "name": r["name"], "type": r["type"], "from": r["from"], "to": r["to"], "status": "Approved",
                                  "note": r.get("note"), "by": "Payboy", "src": "Payboy", "cover": None, "asked": None, "when": None}
                                 for i, r in enumerate(pb["rows"])]
    leave_bal = leave_balances(raw, cfg)
    # running number of this update (kept in raw/seq.txt); it picks the rotating branch the update is published to
    seq_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw", "seq.txt")
    try:
        seq = int(open(seq_path).read().strip()) + 1
    except (OSError, ValueError):
        seq = 1
    with open(seq_path, "w") as f:
        f.write(str(seq))
    payload = {
        "generated": raw.get("generated") or datetime.now(SGT).isoformat(timespec="minutes"),
        "visits_until": raw.get("visits_until"),
        "refresh_seconds": REFRESH_SECONDS,
        "slots": 10,   # publish.sh also writes each update to branch d<seq % 10>; the page reads update seq+1 from there
        "seq": seq,
        "wa": raw["wa"],
        "wa_now": raw.get("wa_now"),
        "created": raw["created"],
        "visits": raw["visits"],
        "comp": raw["comp"],
        "cust": raw.get("cust"),
        "leave": leave,
        "tasks": raw.get("tasks", []),
        "orders": raw.get("orders", []),
        "app_orders": raw.get("app_orders", []),
        "items": raw.get("items", []),
        "inv": raw.get("inv", []),
        "config": {k: v for k, v in cfg.items() if not k.startswith("_")},
        "payboy_synced": pb.get("synced"),
        "leave_bal": leave_bal,
    }
    plain = json.dumps(payload, separators=(",", ":")).encode()
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(pw.encode())
    ct = AESGCM(key).encrypt(iv, plain, None)
    b = lambda x: base64.b64encode(x).decode()
    out = {"v": 1, "iter": ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}
    path = os.path.join(HERE, "docs", "data.js")
    if "--no-bundle" not in sys.argv:  # the 5-minute job only refreshes the live data branch
        with open(path, "w") as f:
            f.write("window.CS360_DATA=" + json.dumps(out) + ";\n")
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    with open(os.path.join(HERE, "out", "data.json"), "w") as f:
        json.dump(out, f)
    wrote = "out/data.json" if "--no-bundle" in sys.argv else "docs/data.js and out/data.json"
    print("wrote %s, %d bytes plain, generated %s" % (wrote, len(plain), payload["generated"]))


if __name__ == "__main__":
    main()
