#!/usr/bin/env python3
"""Pull the Pawkit CS360 data from Zoho CRM through the API and write raw/crm.json.

Needs ~/.config/pawkit-cs360/zoho.json from zoho_setup.py (read-only self client).
Same data shape as the first browser snapshot, so build.py works unchanged.
WhatsApp counts for older days are cached in raw/wa_cache.json; today and yesterday
are re-counted every run, older days once an hour, to keep API use low.
"""
import hmac
import calendar, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.expanduser("~/.config/pawkit-cs360/zoho.json")
TOKEN_CACHE = os.path.expanduser("~/.config/pawkit-cs360/zoho_token.json")   # private, never in the repo
SGT = timezone(timedelta(hours=8))
W = "woztellplatformintegration__"


def post(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


PAUSE = os.path.join(HERE, "raw", "zoho_pause.json")


class Zoho:
    def __init__(self):
        try:   # Zoho said the API limit was reached: make no calls at all until the pause ends
            with open(PAUSE) as f:
                p = json.load(f)
            if p.get("until", 0) > time.time():
                raise SystemExit("paused: Zoho API limit (%s), trying again at %s" % (p.get("why", "")[:80],
                                 datetime.fromtimestamp(p["until"], SGT).strftime("%H:%M")))
        except (OSError, ValueError):
            pass
        self.c = json.load(open(CFG))
        self.api = self.c.get("api_domain", "https://www.zohoapis.com")
        self.tok, self.exp = None, 0

    def token(self):
        # Zoho allows only about 10 new access tokens per 10 minutes, and a token lasts an hour: keep it in a
        # private file and reuse it across runs instead of asking for a new one every minute.
        if not self.tok:
            try:
                with open(TOKEN_CACHE) as f:
                    c = json.load(f)
                if c.get("blocked", 0) > time.time():   # Zoho refused a refresh recently: leave it alone until then
                    raise SystemExit("waiting for Zoho: too many sign-in refreshes, trying again at %s" %
                                     datetime.fromtimestamp(c["blocked"], SGT).strftime("%H:%M"))
                self.tok, self.exp = c["t"], c["exp"]
            except (OSError, ValueError, KeyError):
                pass
        if not self.tok or time.time() > self.exp - 120:
            try:
                j = self._new_token()
            except urllib.error.HTTPError as e:
                fd = os.open(TOKEN_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:      # back off for 11 minutes so the limit can clear
                    json.dump({"blocked": time.time() + 660}, f)
                raise SystemExit("Zoho token refresh failed: HTTP %s (too many refreshes in a short time, or access revoked)" % e.code)
            if "access_token" not in j:
                raise SystemExit("Zoho token refresh failed: %s" % j.get("error", j))
            self.tok, self.exp = j["access_token"], time.time() + int(j.get("expires_in", 3600))
            fd = os.open(TOKEN_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"t": self.tok, "exp": self.exp}, f)
        return self.tok

    def _new_token(self):
        return post(self.c.get("accounts", "https://accounts.zoho.com") + "/oauth/v2/token", {
            "grant_type": "refresh_token", "refresh_token": self.c["refresh_token"],
            "client_id": self.c["client_id"], "client_secret": self.c["client_secret"]})

    calls = {"n": 0, "rows": 0}

    def req(self, path, body=None):
        Zoho.calls["n"] += 1
        if os.environ.get("ZTRACE") and "coql" not in path:
            print("TRACE req  %s" % path[:60])
        h = {"Authorization": "Zoho-oauthtoken " + self.token()}
        data = None
        if body is not None:
            data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.api + path, data=data, headers=h, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return {}
            body = e.read()[:300]
            txt = body.decode("utf-8", "replace")
            if e.code == 429 or "LIMIT" in txt.upper():
                # credits used up -> pause an hour; too many calls at once -> pause 5 minutes
                secs = 300 if "TOO_MANY_REQUESTS" in txt.upper() else 3600
                with open(PAUSE, "w") as f:
                    json.dump({"until": time.time() + secs, "why": "%s %s" % (e.code, txt[:200]), "at": datetime.now(SGT).isoformat(timespec="seconds")}, f)
            raise SystemExit("Zoho %s failed: %s %s" % (path, e.code, body))

    def coql(self, q):
        j = self.req("/crm/v5/coql", {"select_query": q})
        Zoho.calls["rows"] += len(j.get("data", []) or [])
        if os.environ.get("ZTRACE"):   # debugging: one line per COQL call
            m = re.search(r" from (\S+)", q)
            print("TRACE coql %-40s %5d rows" % ((m.group(1) if m else "?")[-40:], len(j.get("data", []) or [])))
        return j

    def all(self, q):
        out, off = [], 0
        while True:
            j = self.coql("%s limit %d, 2000" % (q, off))
            out += j.get("data", [])
            if not j.get("info", {}).get("more_records"):
                return out
            off += 2000


# ---- WhatsApp reply status: which conversations are answered, waiting, or need urgent care ----
URGENT_AFTER_MIN = 30
URGENT_WORDS = ("urgent", "emergency", "asap", "immediately", "bleeding", "blood", "vomit", "seizure", "collapse", "not breathing",
                "can't breathe", "cant breathe", "dying", "injur", "poison", "swallow", "complain", "refund", "unacceptable",
                "disappointed", "angry", "no show", "didn't come", "didnt come", "never came", "still waiting", "no reply", "no response")
CLOSERS = {"ok", "okay", "okie", "k", "kk", "thanks", "thank", "you", "u", "so", "much", "very", "ty", "tq", "thx", "noted", "great", "sure",
           "alright", "got", "it", "will", "do", "no", "problem", "np", "yes", "yup", "yep", "ya", "yeah", "see", "bye", "welcome", "nice",
           "good", "perfect", "cool", "and", "for", "the", "your", "help", "lah", "la", "have", "a", "day", "night", "appreciate", "appreciated",
           "dokie", "worries", "youre", "you're", "re", "youb", "accommodating", "tks", "thankyou", "okk", "okok"}
# same "no reply needed" rules as the hourly Zoho job wa_waiting_tasks (urgent-alert/wa_waiting_tasks.dg)
AUTO_WORDS = ("parcel", "shopee", "spx", "ninja van", "ninjavan", "j&t", "lalamove", "grabexpress", "singpost", "qxpress", "otp",
              "verification code", "one-time", "tracking number")
SKIP_NUMBERS = {"639612931559", "14157959796"}   # Vince's test number, WOZTELL's own bot
AGENT_FIRST = {"6947079000008272057": "Winchelle", "6947079000008272068": "Stephanie", "6947079000008272114": "Dimple",
               "6947079000008272125": "Quynh", "6947079000025466018": "Vince"}
WA_TASKS = os.path.join(HERE, "raw", "wa_tasks.json")


def wa_tasks(z, now):
    """WhatsApp reply tasks touched in the last 3 days, by the last 8 digits of the number: open ones (owner, id) and the
    latest time a closed one was changed. Read at most every 3 minutes."""
    try:
        with open(WA_TASKS) as f:
            c = json.load(f)
        if time.time() - c.get("read", 0) < 180:
            return c["m"]
    except (OSError, ValueError, KeyError):
        c = {}
    try:
        since = (now - timedelta(days=3)).isoformat(timespec="seconds")
        rows = z.all("select id, Owner, Status, Mobile_Number, Modified_Time from Tasks where Subject like 'WhatsApp reply%%' and Modified_Time >= '%s'" % since)
    except SystemExit as e:
        print("WARNING WhatsApp reply tasks could not be read:", str(e)[:120])
        return c.get("m") or {}
    m = {}
    for r in rows:
        k = "".join(ch for ch in (r.get("Mobile_Number") or "") if ch.isdigit())[-8:]
        if len(k) < 8:
            continue
        e = m.setdefault(k, {"open": None, "closed": ""})
        if r.get("Status") not in ("Completed", "Cancelled"):
            o = r.get("Owner") or {}
            e["open"] = {"id": r.get("id"), "own": AGENT_FIRST.get(str(o.get("id")), o.get("name") or "")}
        elif (r.get("Modified_Time") or "") > e["closed"]:
            e["closed"] = r.get("Modified_Time") or ""
    with open(WA_TASKS, "w") as f:
        json.dump({"read": time.time(), "m": m}, f)
    return m
AUTO_TEXT = "thank you for contacting pawkit"   # WOZTELL's automatic greeting ("Hi there! Thank you for contacting Pawkit ...")


def is_auto(data):
    try:
        text = str((json.loads(data or "{}") or {}).get("text") or "")
    except (ValueError, AttributeError):
        text = str(data or "")
    return AUTO_TEXT in text.lower()


WA_ROWS = os.path.join(HERE, "raw", "wa_rows.json")
WA_FIELDS = "From, To, Direction, Message_Type, Message_Date_time, Member_Id, Message_Data, Lead.Full_Name, Channel_Type".split(", ")


def wa_rows(z, now, full):
    """The last 48 hours of one-to-one WhatsApp log rows. A full run reads all 48 hours from Zoho; a fast run asks
    only for rows since the last read (minus a 10-minute overlap for late logs) and merges them by id."""
    try:
        with open(WA_ROWS) as f:
            c = json.load(f)
        rows, upto = c["rows"], c["upto"]
    except (OSError, ValueError, KeyError):
        rows, upto, full = {}, None, True
    start48 = (now - timedelta(hours=48)).isoformat(timespec="seconds")
    since = start48 if full else (datetime.fromisoformat(upto) - timedelta(minutes=10)).isoformat(timespec="seconds")
    if since < start48:
        since = start48
    q = "select %s from %sWoztell_logs where %sMessage_Date_time between '%s' and '%s'" % (
        ", ".join(W + f for f in WA_FIELDS), W, W, since, now.isoformat(timespec="seconds"))
    new = z.all(q)
    if full:
        rows = {}
    for r in new:
        rows[r.get("id") or "%s|%s|%s" % (r.get(W + "Message_Date_time"), r.get(W + "From"), r.get(W + "Message_Type"))] = r
    rows = {k: r for k, r in rows.items() if (r.get(W + "Message_Date_time") or "") >= start48}
    tmp = WA_ROWS + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as f:
        json.dump({"upto": now.isoformat(timespec="seconds"), "rows": rows}, f)
    os.replace(tmp, WA_ROWS)
    return list(rows.values())


def whatsapp_status(z, now, rows=None):
    """Looks at the last 48 hours of one-to-one WhatsApp messages and groups them by customer. A conversation is
    'waiting' when the customer's latest message has no Pawkit message after it (a bare thanks/ok/emoji does not count
    as needing a reply). It is 'urgent' when it has waited URGENT_AFTER_MIN minutes or contains an urgent word.
    For waiting chats the unanswered messages themselves are published too (inside the encrypted data), so the
    team can read them from the page; answered chats publish no text."""
    if rows is None:
        rows = wa_rows(z, now, full=True)
    try:   # automated senders to leave out (private config)
        with open(os.path.expanduser("~/.config/pawkit-cs360/config.json")) as f:
            ignore = set(json.load(f).get("_wa_ignore_numbers") or [])
    except (OSError, ValueError):
        ignore = set()
    ignore |= SKIP_NUMBERS
    tmap = wa_tasks(z, now)
    conv = {}
    for r in sorted(rows, key=lambda r: r.get(W + "Message_Date_time") or ""):
        if (r.get(W + "From") if r.get(W + "Direction") == "In" else r.get(W + "To")) in ignore:
            continue
        t, typ, d = r.get(W + "Message_Date_time"), r.get(W + "Message_Type"), r.get(W + "Direction")
        if not t or not typ or typ in ("SMB_CONTACTS", "READ", "SENT", "DELIVERY") or d not in ("In", "Out"):
            continue
        if typ == "REACT" and d == "In":      # a customer's emoji reaction asks for nothing
            continue
        if (r.get(W + "Channel_Type") or "whatsapp-cloud") != "whatsapp-cloud":   # Instagram messages and comments are not WhatsApp chats
            continue
        key = r.get(W + "Member_Id") or (r.get(W + "From") if d == "In" else r.get(W + "To"))
        c = conv.setdefault(key, {"pend": [], "name": None, "tel": None, "ins": 0, "waits": [], "last_in": None, "auto": 0, "auto_pend": False})
        if not c["tel"]:
            c["tel"] = "".join(ch for ch in (r.get(W + "From") if d == "In" else r.get(W + "To")) or "" if ch.isdigit())
        if d == "In":
            text = ""
            if typ == "TEXT":
                try:
                    text = str(json.loads(r.get(W + "Message_Data") or "{}").get("text") or "")
                except (ValueError, AttributeError):
                    text = str(r.get(W + "Message_Data") or "")
            if typ != "TEXT":   # a photo, voice note, file and so on: say what it was, with its caption if any
                try:
                    cap = str((json.loads(r.get(W + "Message_Data") or "{}") or {}).get("text") or "")
                except (ValueError, AttributeError):
                    cap = ""
                text = "[%s]%s" % (typ.lower().replace("_", " "), " " + cap if cap else "")
            c["pend"].append((t, typ, text))
            c["ins"] += 1; c["last_in"] = t
            c["tel"] = "".join(ch for ch in (r.get(W + "From") or "") if ch.isdigit())
            c["name"] = r.get(W + "Lead.Full_Name") or c["name"]
        elif typ != "TEMPLATE" and is_auto(r.get(W + "Message_Data")):   # the auto-greeting is not a human reply
            c["auto"] += 1
            if c["pend"]:
                c["auto_pend"] = True
        elif typ != "TEMPLATE":            # templates are reminders and broadcasts, not replies
            c["auto_pend"] = False
            if c["pend"]:
                c["waits"].append((datetime.fromisoformat(t) - datetime.fromisoformat(c["pend"][0][0])).total_seconds() / 60)
            c["pend"] = []
    # service providers (groomers, vets, nurses) also write to this number: tell them apart by the Partner phone list
    ppath = os.path.join(HERE, "raw", "partner_phones.json")
    try:
        with open(ppath) as f:
            pc = json.load(f)
    except (OSError, ValueError):
        pc = {}
    if time.time() - pc.get("t", 0) > 3600:
        try:
            pr = z.all("select Name, Phone_Number from Partner where Phone_Number is not null")
            pc = {"t": time.time(), "p": {"".join(ch for ch in (r.get("Phone_Number") or "") if ch.isdigit())[-8:]: r.get("Name") for r in pr}}
            with open(ppath, "w") as f:
                json.dump(pc, f)
        except SystemExit as e:
            print("WARNING provider phone list could not be read:", str(e)[:120])
    provs = {k: v for k, v in (pc.get("p") or {}).items() if len(k) == 8}
    today = now.date().isoformat()
    out = {"answered": 0, "convs": 0, "waiting": [], "urgent_after": URGENT_AFTER_MIN, "window_h": 48,
           "seg": {"cust": {"convs": 0, "answered": 0, "auto": 0, "auto_msgs": 0}, "prov": {"convs": 0, "answered": 0, "auto": 0, "auto_msgs": 0}}}
    waits_today = []
    for c in conv.values():
        pname = provs.get((c["tel"] or "")[-8:])
        seg = out["seg"]["prov" if pname else "cust"]
        if c["auto"]:   # chats that got the automatic greeting, even when a person replied too or only a reaction set it off
            seg["auto"] += 1
            seg["auto_msgs"] += c["auto"]
        if not c["ins"]:
            continue
        out["convs"] += 1
        seg["convs"] += 1
        pend = c["pend"]
        # a closing "thanks / ok / emoji" needs no reply
        def closer(p):
            words = [w for w in "".join(ch.lower() if ch.isalnum() or ch == "'" else " " for ch in p[2]).split()]
            return p[1] == "STICKER" or (p[1] == "TEXT" and len(words) <= 8 and all(w in CLOSERS for w in words))
        while pend and closer(pend[-1]) and all(closer(p) for p in pend):
            pend = []
        k8 = (c["tel"] or "")[-8:]
        tk = tmap.get(k8) or {}
        if pend:
            ptxt = " ".join(p[2] for p in pend).lower()
            if all(p[1] in ("PAYLOAD", "BUTTON", "INTERACTIVE") for p in pend):
                pend = []        # only button taps on our reminders
            elif any(w in ptxt for w in AUTO_WORDS) and not any(w in ptxt for w in URGENT_WORDS):
                pend = []        # courier / OTP / automated notice
            elif tk.get("closed") and not tk.get("open") and tk["closed"] > pend[0][0]:
                pend = []        # its task was handled after the customer wrote
        if not pend:
            out["answered"] += 1
            seg["answered"] += 1
        else:
            mins = int((now - datetime.fromisoformat(pend[0][0])).total_seconds() // 60)
            txt = " ".join(p[2] for p in pend).lower()
            word = next((w for w in URGENT_WORDS if w in txt), None)
            name = (c["name"] or "").strip()
            if not name or name.lower().startswith("unknown") or name.replace(" ", "").replace("+", "").isdigit():
                name = None
            out["waiting"].append({"name": name, "tel": c["tel"], "since": pend[0][0], "mins": mins, "n": len(pend),
                                   "urgent": bool(word) or mins >= URGENT_AFTER_MIN, "word": bool(word), "prov": bool(pname),
                                   "auto": c["auto_pend"],
                                   "msgs": [{"t": p[0], "x": p[2][:1200]} for p in pend[-8:]]})
            # task column: the open task, or when the hourly job (09:45-20:45 at :45) or the 8:00 post will make one
            if tk.get("open"):
                out["waiting"][-1]["task"] = tk["open"]
            elif not pname:
                st = datetime.fromisoformat(pend[0][0]).astimezone(SGT)
                due = st + timedelta(minutes=15 if word else 60)
                day8 = now.replace(hour=8, minute=0, second=0, microsecond=0)
                nxt = None
                if st >= day8:
                    for hh in range(9, 21):
                        run = now.replace(hour=hh, minute=45, second=0, microsecond=0)
                        if run >= due and run >= now - timedelta(minutes=4):
                            nxt = "%02d:45" % hh
                            break
                out["waiting"][-1]["next"] = nxt or "8am"
            if pname:
                out["waiting"][-1]["name"] = pname
        if c["last_in"] and c["last_in"][:10] == today and not pname:
            waits_today += c["waits"][-3:]
    # a number the WhatsApp log has no name for: look it up in the customer list (found names are remembered,
    # misses are retried after an hour)
    npath = os.path.join(HERE, "raw", "wa_names.json")
    try:
        with open(npath) as f:
            names = json.load(f)
    except (OSError, ValueError):
        names = {}
    for w in out["waiting"]:
        tel = w["tel"]
        if w["name"] or len(tel) < 8:
            continue
        hit = names.get(tel)
        if not hit or (not hit.get("n") and time.time() - hit.get("t", 0) > 3600):
            t8 = tel[-8:]
            pats = [t8, t8[:4] + " " + t8[4:]]
            cond = "((Mobile_Number like '%%%s' or Mobile_Number like '%%%s') or (Alternative_contact like '%%%s' or Alternative_contact like '%%%s'))" % (pats[0], pats[1], pats[0], pats[1])
            try:
                j = z.coql("select Name from Customers_1 where %s limit 1" % cond)
                hit = {"n": ((j.get("data") or [{}])[0].get("Name") or "").strip(), "t": time.time()}
            except SystemExit as e:
                print("WARNING customer name lookup failed:", str(e)[:120])
                hit = {"n": "", "t": time.time()}
            names[tel] = hit
        if hit.get("n"):
            w["name"], w["src"] = hit["n"], "customer list"
    with open(npath, "w") as f:
        json.dump(names, f)
    out["waiting"].sort(key=lambda w: -w["mins"])
    waits_today.sort()
    out["median_reply_min"] = round(waits_today[len(waits_today) // 2], 1) if waits_today else None
    return out



def customer_history(z, today):
    """Per-customer history from every booking: first completed visit (for 'repeat'), and who is due
    for grooming or a vaccination. Heavy (all bookings), so it is cached and rebuilt every 6 hours (and on a new day)."""
    path = os.path.join(HERE, "raw", "hist_cache.json")
    try:
        with open(path) as f:
            c = json.load(f)
        if time.time() - c.get("_t", 0) < 6 * 3600 and c.get("day") == today.isoformat():
            return c
    except (OSError, ValueError):
        c = None
    try:
        rows = z.all("select Pet_Owner, Date_Time, Booking_Status, Service_Category, Next_grooming_appointment, "
                     "Next_vaccination_appointment, Customer_Clean_Name, Pet_Name, Area, Partner_Name.Name from Booking where Date_Time is not null")
    except SystemExit:
        if c:
            return c  # keep the last good copy if this big query fails
        raise
    t = today.isoformat()
    by = {}
    for r in rows:
        o = r.get("Pet_Owner")
        if isinstance(o, dict) and o.get("id"):
            by.setdefault(o["id"], []).append(r)
    first, repeat_owners = {}, 0
    groom, vacc = [], []
    first_name = lambda n: (n or "").strip().split(" ")[0][:20]
    for oid, rs in by.items():
        done = sorted((r for r in rs if r.get("Booking_Status") == "Completed"), key=lambda r: r["Date_Time"])
        if not done:
            continue
        first[oid] = done[0]["Date_Time"][:10]
        if len(done) >= 2:
            repeat_owners += 1
        future = [r for r in rs if r["Date_Time"][:10] > t and r.get("Booking_Status") not in ("Cancelled", "Not fulfilled", "Refund", "No show")]
        for cat, out, lo, hi, nxt in (("Grooming", groom, 35, 180, "Next_grooming_appointment"), ("Vaccination", vacc, 335, 450, "Next_vaccination_appointment")):
            d = [r for r in done if r.get("Service_Category") == cat]
            if not d or any(r.get("Service_Category") == cat for r in future):
                continue
            last = d[-1]
            days = (today - datetime.fromisoformat(last["Date_Time"][:10]).date()).days
            sugg = (last.get(nxt) or "")[:10] or None
            due_by_date = bool(sugg and sugg <= (today + timedelta(days=30)).isoformat())
            if not (lo <= days <= hi or (due_by_date and days >= 14 and days <= 450)):
                continue
            out.append({"n": first_name(last.get("Customer_Clean_Name")), "pet": (last.get("Pet_Name") or "")[:40], "last": last["Date_Time"][:10],
                        "days": days, "cnt": len(d), "all": len(done), "area": last.get("Area"), "prov": last.get("Partner_Name.Name"),
                        "next": sugg, "bid": last.get("id")})
    groom.sort(key=lambda x: (-x["cnt"], x["days"]))
    vacc.sort(key=lambda x: x["days"], reverse=True)
    c = {"_t": time.time(), "day": t, "first": first,
         "cust": {"asof": datetime.now(SGT).isoformat(timespec="minutes"), "owners": len(first), "repeat_owners": repeat_owners,
                  "groom": groom[:400], "vacc": vacc[:200], "groom_total": len(groom), "vacc_total": len(vacc)}}
    tmp = path + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(c, f)
    os.replace(tmp, path)
    return c


# ---- schedule: read the week tabs of the Schedule Tracker sheet, so the sheet is the one place to edit shifts ----
SHEET_CACHE = os.path.join(HERE, "raw", "sheet_roster.json")
_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def sheet_roster(z):
    """{date: {name: code as typed}} from every tab whose row 2 says 'Week of Mon 23 Nov to Sun 29 Nov 2026'.
    Needs the ZohoSheet.dataAPI.READ scope. On any failure the last good copy is used, and Zoho is left alone for 10 minutes."""
    try:
        with open(SHEET_CACHE) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    try:
        with open(os.path.expanduser("~/.config/pawkit-cs360/config.json")) as f:
            sid = json.load(f).get("_schedule_sheet")
    except (OSError, ValueError):
        sid = None
    if not sid or cache.get("retry_after", 0) > time.time():
        return cache.get("roster") or {}

    def call(**params):
        req = urllib.request.Request("https://sheet.zoho.com/api/v2/" + sid, data=urllib.parse.urlencode(params).encode(),
                                     headers={"Authorization": "Zoho-oauthtoken " + z.token()}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def grid(j):   # the rows of cells, wherever the response keeps them
        for v in j.values():
            if isinstance(v, list) and v and all(isinstance(x, list) for x in v):
                return v
        return []
    try:
        roster = {}
        for ws in call(method="worksheet.list").get("worksheet_names", []):
            name = ws.get("worksheet_name") if isinstance(ws, dict) else ws
            rows = grid(call(method="worksheet.content.get", worksheet_name=name, start_row=1, end_row=40, start_column=1, end_column=9,
                             response_type="array", major_dimension="rows"))
            cell = lambda r, c: str(rows[r][c]).strip() if r < len(rows) and c < len(rows[r]) and rows[r][c] is not None else ""
            wk = next((m for r in range(min(6, len(rows))) for m in [re.search(r"Week of \w+ (\d{1,2}) (\w{3})\w* to \w+ \d{1,2} (\w{3})\w* (\d{4})", cell(r, 0))] if m), None)
            head = next((r for r in range(len(rows)) if cell(r, 0).lower() == "team member"), None)
            if not wk or head is None or wk.group(2).lower() not in _MONTHS:
                continue   # not a week tab (the leave log, notes)
            year = int(wk.group(4)) - (1 if wk.group(2).lower() == "dec" and wk.group(3).lower() == "jan" else 0)
            mon = datetime(year, _MONTHS[wk.group(2).lower()], int(wk.group(1))).date()
            for r in range(head + 1, len(rows)):
                who = cell(r, 0)
                if not who or who.lower().startswith("agents on shift"):
                    break
                for i in range(7):
                    if cell(r, 2 + i):
                        roster.setdefault((mon + timedelta(days=i)).isoformat(), {})[who] = cell(r, 2 + i)
        if not roster:
            raise ValueError("no week tabs found")
        cache = {"roster": roster, "read": time.time()}
    except Exception as e:   # the schedule is an extra: nothing here may stop the rest of the update
        detail = e.read()[:120].decode("utf-8", "replace") if isinstance(e, urllib.error.HTTPError) else str(e)[:120]
        print("WARNING schedule sheet could not be read (%s %s); using the last copy" % (type(e).__name__, detail))
        cache["retry_after"] = time.time() + 600
    tmp = SHEET_CACHE + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, SHEET_CACHE)
    return cache.get("roster") or {}


TRK_CACHE = os.path.join(HERE, "raw", "tracker_rows.json")


def tracker_rows(z):
    """{task id: row number} from the Task Tracker sheet (tab Tasks, column headed "Record ID"), so the page can link
    each task to its row. Read at most every 5 minutes; on any failure the last good copy is used for 10 minutes."""
    try:
        with open(TRK_CACHE) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if time.time() - cache.get("read", 0) < 300 or cache.get("retry_after", 0) > time.time():
        return cache.get("rows") or {}
    try:
        with open(os.path.expanduser("~/.config/pawkit-cs360/config.json")) as f:
            url = (json.load(f).get("links") or {}).get("tracker") or ""
        m = re.search(r"/open/([A-Za-z0-9]+)", url)
        if not m:
            return cache.get("rows") or {}
        sid = m.group(1)

        def call(**params):
            req = urllib.request.Request("https://sheet.zoho.com/api/v2/" + sid, data=urllib.parse.urlencode(params).encode(),
                                         headers={"Authorization": "Zoho-oauthtoken " + z.token()}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)

        def grid(j):
            for v in j.values():
                if isinstance(v, list) and v and all(isinstance(x, list) for x in v):
                    return v
            return []
        head = grid(call(method="worksheet.content.get", worksheet_name="Tasks", start_row=1, end_row=1, start_column=1, end_column=45,
                         response_type="array", major_dimension="rows"))
        hdr = [str(x or "").strip() for x in (head[0] if head else [])]
        if "Record ID" not in hdr:
            raise ValueError("no Record ID column")
        col = hdr.index("Record ID") + 1
        cells = grid(call(method="worksheet.content.get", worksheet_name="Tasks", start_row=2, end_row=3000, start_column=col, end_column=col,
                          response_type="array", major_dimension="rows"))
        rows = {}
        for i, r in enumerate(cells):
            v = "".join(ch for ch in str((r[0] if r else "") or "") if ch.isdigit())
            if len(v) >= 15:
                rows[v] = i + 2
        cache = {"rows": rows, "read": time.time()}
    except Exception as e:   # an extra: nothing here may stop the rest of the update
        detail = e.read()[:120].decode("utf-8", "replace") if isinstance(e, urllib.error.HTTPError) else str(e)[:120]
        print("WARNING task tracker rows could not be read (%s %s); using the last copy" % (type(e).__name__, detail))
        cache["retry_after"] = time.time() + 600
    tmp = TRK_CACHE + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, TRK_CACHE)
    return cache.get("rows") or {}


def why(r):  # why a booking did not go ahead: Zoho's closure reason plus the staff's reason text and internal note
    if r.get("Booking_Status") in ("Scheduled", "Completed"):
        return {}
    seen, notes = set(), []
    for k in ("Reason", "Reason_Note", "Note"):
        t = " ".join((r.get(k) or "").split())
        if t and t.lower() not in seen:
            seen.add(t.lower()); notes.append(t)
    out = {}
    if r.get("Reason_Not_Fulfilled"):
        out["why"] = r["Reason_Not_Fulfilled"]
    if notes:
        out["whyn"] = " · ".join(notes)[:300]
    return out


# Booking fields the Live Status tab needs, and their short keys in the published visit records
LIVE_KEYS = (("v", "Visit_Status"), ("vt", "Status_Updated_At"), ("vb", "Status_Updated_By"), ("eta", "Provider_ETA"),
             ("dl", "Delay_Reason"), ("dm", "Delay_Minutes"), ("ve", "Visit_End_Reason"), ("ct", "Customer_Told_Of_Delay_At"),
             ("cw", "Customer_Told_On_The_Way_At"),
             ("cf", "Customer_Confirmed"), ("cr", "Customer_Rating"), ("cm", "Customer_Comment"),
             ("lc", "Late_Credit_Amount"), ("lcx", "Late_Credit_Expires"))


def fast():
    """Every ~2 minutes: only WhatsApp messages since the last read and the status fields of today's and tomorrow's
    visits, patched into the last full snapshot (raw/crm.json). Everything else waits for the 15-minute full run."""
    path = os.path.join(HERE, "raw", "crm.json")
    try:
        with open(path) as f:
            out = json.load(f)
    except (OSError, ValueError):
        raise SystemExit("fast: no full snapshot yet, waiting for the full run")
    now = datetime.now(SGT)
    z = Zoho()
    try:
        out["wa_now"] = whatsapp_status(z, now, wa_rows(z, now, full=False))
    except SystemExit as e:
        print("WARNING fast WhatsApp read failed:", str(e)[:160])
        if "paused" in str(e) or os.path.exists(PAUSE) and json.load(open(PAUSE)).get("until", 0) > time.time():
            raise
    d0, d1 = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()
    fields = ["Date_Time", "End_Time", "Booking_Status", "Partner_Name.Name", "Reason_Not_Fulfilled", "Reason", "Reason_Note", "Note",
              "Reminder_Provider_Sent", "Reminder_Owner_Sent"] + [f for _, f in LIVE_KEYS]
    rows = z.all("select %s from Booking where Date_Time between '%sT00:00:00+08:00' and '%sT23:59:59+08:00'" % (", ".join(fields), d0, d1))
    byid = {v.get("id"): v for v in out.get("visits") or []}
    patched = 0
    for r in rows:
        v = byid.get(r.get("id"))
        if not v:      # a booking made since the full run: it appears with the next full run
            continue
        v.update({"d": r.get("Date_Time"), "e": r.get("End_Time"), "s": r.get("Booking_Status"), "p": r.get("Partner_Name.Name")})
        for k, f in LIVE_KEYS:
            if r.get(f) in (None, ""):
                v.pop(k, None)
            else:
                v[k] = r[f]
        for k, f in (("rp", "Reminder_Provider_Sent"), ("ro", "Reminder_Owner_Sent")):
            if r.get(f):
                v[k] = 1
            else:
                v.pop(k, None)
        v.pop("why", None); v.pop("whyn", None); v.update(why(r))
        patched += 1
    out["generated"] = now.isoformat(timespec="seconds")
    out["fast"] = True
    tmp = path + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(out, f)
    os.replace(tmp, path)
    print(now.strftime("%Y-%m-%d %H:%M:%S"), "fast ok", patched, "visits patched,", len((out.get("wa_now") or {}).get("waiting") or []),
          "waiting chats,", Zoho.calls["n"], "zoho calls,", Zoho.calls["rows"], "coql rows")


SLOW = os.path.join(HERE, "raw", "slow_cache.json")


# Pet Owner Source: two older picklist values come back from Zoho under their stored names
SOURCE_NAMES = {"Instagram Social": "Instagram", "Facebook Social": "Facebook"}


def owner_source(v):
    v = " ".join(str(v or "").split())
    return SOURCE_NAMES.get(v, v) or None


def slow(name, ttl, fn):
    """Data that rarely changes (staff list, Xero stock, leave credits, app orders): read from Zoho at most once per
    ttl seconds; if a read fails, the last good copy is used."""
    try:
        with open(SLOW) as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    e = c.get(name)
    if e and time.time() - e.get("t", 0) < ttl:
        return e["v"]
    try:
        v = fn()
    except SystemExit:
        if e:
            return e["v"]
        raise
    c[name] = {"t": time.time(), "v": v}
    tmp = SLOW + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(c, f)
    os.replace(tmp, SLOW)
    return v


def rng(d):
    return "between '%sT00:00:00+08:00' and '%sT23:59:59+08:00'" % (d, d)


def main():
    z = Zoho()
    now = datetime.now(SGT)
    today = now.date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    start, end14 = days[0], days[-1]
    eom = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    next_sun = today + timedelta(days=13 - today.weekday())  # Sunday of next week (>= today+7)
    end_vis = max(eom, next_sun).isoformat()

    users = slow("users", 3600, lambda: {u["id"]: u["full_name"] for u in z.req("/crm/v5/users?type=AllUsers&per_page=200").get("users", [])})
    nm = lambda v: users.get(v["id"]) if isinstance(v, dict) and v.get("id") else None

    # WhatsApp per day (cached)
    cpath = os.path.join(HERE, "raw", "wa_cache.json")
    try:
        with open(cpath) as f:
            cache = json.load(f)
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}  # missing or damaged cache: just re-count all days
    # the 48 h of message rows: re-read in full once an hour (catches late logs), otherwise only what is new
    try:
        last_full = os.path.getmtime(os.path.join(HERE, "raw", "wa_full.stamp"))
    except OSError:
        last_full = 0
    full_wa = time.time() - last_full > 3600
    try:
        wrows = wa_rows(z, now, full=full_wa)
        if full_wa:
            open(os.path.join(HERE, "raw", "wa_full.stamp"), "w").close()
    except SystemExit as e:
        print("WARNING WhatsApp messages could not be read:", str(e)[:120])
        wrows = None
    yday = (today - timedelta(days=1)).isoformat()
    wa = {}
    for d in days:
        c = cache.get(d)
        if d >= yday and wrows is not None:   # (3) today and yesterday: count the saved rows, no Zoho call
            ins = [r for r in wrows if (r.get(W + "Message_Date_time") or "")[:10] == d]
            wa[d] = {"in": sum(1 for r in ins if r.get(W + "Direction") == "In"),
                     "out": sum(1 for r in ins if r.get(W + "Direction") == "Out"),
                     "chats": len({r.get(W + "From") for r in ins if r.get(W + "Direction") == "In"})}
            cache[d] = dict(wa[d], _t=time.time())
            continue
        # (1) a finished day never changes: keep a count taken at least 2 hours after that day ended
        ended = datetime.fromisoformat(d + "T00:00:00+08:00").timestamp() + 86400 + 7200
        if c and c.get("_t", 0) > ended:
            wa[d] = {k: c[k] for k in ("in", "out", "chats")}; continue
        a = z.coql("select %sDirection, COUNT(id) from %sWoztell_logs where %sMessage_Date_time %s group by %sDirection" % (W, W, W, rng(d), W))
        o = {x[W + "Direction"]: x["COUNT(id)"] for x in a.get("data", [])}
        u = z.coql("select %sFrom, COUNT(id) from %sWoztell_logs where %sMessage_Date_time %s and %sDirection = 'In' group by %sFrom limit 2000" % (W, W, W, rng(d), W, W))
        wa[d] = {"in": o.get("In", 0), "out": o.get("Out", 0), "chats": len(u.get("data", []))}
        cache[d] = dict(wa[d], _t=time.time())
    cache = {k: v for k, v in cache.items() if k >= start}
    ctmp = cpath + ".%d.tmp" % os.getpid()
    with open(ctmp, "w") as f:
        json.dump(cache, f)
    os.replace(ctmp, cpath)

    try:
        wa_now = whatsapp_status(z, now, wrows) if wrows is not None else None
    except SystemExit as e:
        print("WARNING WhatsApp reply status could not be read:", str(e)[:120])
        wa_now = None

    hist = customer_history(z, today)
    first = hist["first"]
    F = "Created_Time, Date_Time, End_Time, Booking_Status, Service_Category, Created_By, Partner_Name.Name, Area, Invoice_Status, Payment_Method, Base_Price, Pet_Owner, Reason_Not_Fulfilled, Reason, Reason_Note, Note, Booking_Code, Name, Reminder_Provider_Sent, Reminder_Owner_Sent, Visit_Status, Status_Updated_At, Status_Updated_By, Provider_ETA, Delay_Reason, Delay_Minutes, Visit_End_Reason, Customer_Told_Of_Delay_At, Customer_Told_On_The_Way_At, Customer_Confirmed, Customer_Rating, Customer_Comment, Late_Credit_Amount, Late_Credit_Expires, Survey_Sent_At, Late_Credit_Given_At, Late_Credit_Used, Customer_Clean_Name, Dissatisfaction_Reason"

    def owner(r):
        o = r.get("Pet_Owner")
        return o.get("id") if isinstance(o, dict) else None

    def repeat(r):  # the customer had a completed visit before this booking's date
        f = first.get(owner(r) or "")
        when = (r.get("Date_Time") or r.get("Created_Time") or "")[:10]
        return bool(f and when and f < when)
    slim = lambda r: {"id": r.get("id"), "c": r.get("Created_Time"), "d": r.get("Date_Time"), "e": r.get("End_Time"), "s": r.get("Booking_Status"),
                      "cat": r.get("Service_Category"), "src": None, "by": nm(r.get("Created_By")), "p": r.get("Partner_Name.Name"),
                      "a": r.get("Area"), "inv": r.get("Invoice_Status"), "pay": r.get("Payment_Method"),
                      "amt": r.get("Base_Price"), "o": (owner(r) or "")[-7:] or None, "rep": repeat(r), **why(r),
                      "ref": r.get("Name"), **{k: r[f] for k, f in LIVE_KEYS if r.get(f) not in (None, "")},
                      **({"rp": 1} if r.get("Reminder_Provider_Sent") else {}), **({"ro": 1} if r.get("Reminder_Owner_Sent") else {}),
                      **({"app": True} if r.get("Booking_Code") else {})}   # booked through the Pawkit app (it sets a Booking Code)
    start31 = (today - timedelta(days=13)).replace(day=1).isoformat()  # month of the oldest pickable day
    created = [slim(r) for r in z.all("select %s from Booking where Created_Time between '%sT00:00:00+08:00' and '%sT23:59:59+08:00'" % (F, start31, end14))]
    s30 = (today - timedelta(days=30)).isoformat()
    vrows = z.all("select %s, Paid_On from Booking where Date_Time between '%sT00:00:00+08:00' and '%sT23:59:59+08:00'" % (F, min(start31, s30), end_vis))
    visits = [slim(r) for r in vrows if (r.get("Date_Time") or "")[:10] >= start31]

    comp = [{"id": c.get("id"), "n": c.get("Name"), "cd": c.get("Complaint_Date"), "c": c.get("Created_Time"), "s": c.get("Complaint_Status"),
             "pr": c.get("Priority"), "cat": c.get("Complaint_Category"), "p": c.get("Provider.Name"), "f": c.get("Follow_up_Date"),
             "rf": c.get("Refund_Required"), "ra": c.get("Refund_Amount"),
             "o": nm(c.get("Owner")), "desc": " ".join((c.get("Complaint_Description") or "").split())[:400], "res": " ".join((c.get("Resolution") or "").split())[:250]}
            for c in z.all("select Name, Owner, Complaint_Date, Created_Time, Complaint_Status, Priority, Complaint_Category, Provider.Name, Follow_up_Date, Refund_Required, Refund_Amount, Complaint_Description, Resolution from Complaints where id is not null")]

    tasks = [{"id": t.get("id"), "sub": t.get("Subject"), "s": t.get("Status"), "pr": t.get("Priority"), "due": t.get("Due_Date"), "own": nm(t.get("Owner")),
              "by": nm(t.get("Created_By")), "type": t.get("Task_Type"), "svc": t.get("Service_Type"), "ho": t.get("Handover_To"),
              "hos": t.get("Handover_Status"), "dl": t.get("Action_Deadline"), "c": t.get("Created_Time"), "cl": t.get("Closed_Time"),
              "bid": t.get("Booking_ID"), "wait": t.get("Waiting_On"), "prov": t.get("Provider_Assigned"), "inv": t.get("Invoiced"),
              "paid": t.get("Paid"), "gcal": t.get("GCal_Updated"), "form": t.get("Form_Signed"), "comp": t.get("Compensation_Requested"),
              "conf": t.get("Confirmed_Date_Time"),
              # for the closure-rate card: when a Cancelled task was last changed, and whether the task has an Outcome note (no text)
              "m": t.get("Modified_Time"), "nt": bool((t.get("Outcome") or "").strip()),
              # WhatsApp reply tasks (made by the 8:00 AM after-hours digest) also carry the customer's number, name and
              # messages, so the WhatsApp page can list them; other tasks publish no description
              **({"tel": "".join(ch for ch in (t.get("Mobile_Number") or "") if ch.isdigit()), "who": t.get("Owner_Name"),
                  "desc": (t.get("Description") or "")[:2000], "out": (t.get("Outcome") or "")[:200]}
                 if (t.get("Subject") or "").startswith("WhatsApp reply") else {})}
             for t in z.all("select Subject, Status, Priority, Due_Date, Owner, Created_By, Task_Type, Service_Type, Handover_To, Handover_Status, Action_Deadline, Created_Time, Closed_Time, Booking_ID, Waiting_On, Provider_Assigned, Invoiced, Paid, GCal_Updated, Form_Signed, Compensation_Requested, Confirmed_Date_Time, Mobile_Number, Owner_Name, Description, Outcome, Modified_Time from Tasks where id is not null")]

    orders = [{"n": o.get("Name"), "d": o.get("Order_Date"), "type": o.get("Order_Type"), "s": o.get("Order_Status"), "pay": o.get("Payment_Status"),
               "ful": o.get("Fulfilment_Status"), "by": o.get("Processed_By"), "agent": nm(o.get("Assign_Agent")), "dm": o.get("Delivery_Method"),
               "urg": o.get("Urgency_level"), "tot": o.get("Total_Order_Amount"), "out": o.get("Outstanding_Amount"), "paid": o.get("Amount_Paid"),
               "disp": o.get("Dispatch_Date"), "exp": o.get("Expected_Delivery_Date"), "del": o.get("Delivered_Date"),
               "xi": o.get("Xero_Invoice_Number"), "c": o.get("Created_Time")}
              for o in z.all("select Name, Order_Date, Order_Type, Order_Status, Payment_Status, Fulfilment_Status, Processed_By, Assign_Agent, Delivery_Method, Urgency_level, Total_Order_Amount, Outstanding_Amount, Amount_Paid, Dispatch_Date, Expected_Delivery_Date, Delivered_Date, Xero_Invoice_Number, Created_Time from Order_Processing where id is not null")]

    # orders placed through the Pawkit app: the app writes each one into Booking with a Booking Code and the list of services bought
    app_orders = [{"id": o.get("id"), "ref": o.get("Name"), "code": o.get("Booking_Code"), "c": o.get("Created_Time"), "d": o.get("Date_Time"),
                   "s": o.get("Booking_Status"), "cat": o.get("Service_Category"), "svc": " ".join((o.get("Service_Breakdown") or "").split())[:400],
                   "p": o.get("Partner_Name.Name"), "inv": o.get("Invoice_Status"), "pay": o.get("Payment_Method"), "amt": o.get("Base_Price"),
                   "pets": o.get("Number_of_Pets"), "who": o.get("Pet_Owner.Name") or o.get("Customer_Clean_Name")}
                  for o in slow("app_orders", 3600, lambda: z.all("select Name, Booking_Code, Created_Time, Date_Time, Booking_Status, Service_Category, Service_Breakdown, Pet_Owner.Name, Customer_Clean_Name, Partner_Name.Name, Invoice_Status, Payment_Method, Base_Price, Number_of_Pets from Booking where Booking_Code is not null"))]

    # new pet owners and where they came from (Pet Owner field Source), read hourly, same window as the bookings.
    # Published: the created time, the Source and the end of the owner id (to match the owner's bookings), no name or number.
    try:
        owners = [{"o": (r.get("id") or "")[-7:] or None, "c": r.get("Created_Time"), "s": owner_source(r.get("Source"))}
                  for r in slow("owner_sources", 3600, lambda: z.all("select Source, Created_Time from Customers_1 where Created_Time >= '%sT00:00:00+08:00'" % start31))]
    except SystemExit as e:
        print("WARNING pet owner sources could not be read:", str(e)[:120])
        owners = None

    items = [{"n": i.get("Name"), "code": i.get("Item_Code"), "q": i.get("Qty_On_Hand"), "r": i.get("Reserved_Qty"),
              "up": i.get("Unit_Price"), "cp": i.get("Cost_Price"), "sync": i.get("Last_Synced")}
             for i in slow("xero_items", 3600, lambda: z.all("select Name, Item_Code, Qty_On_Hand, Reserved_Qty, Unit_Price, Cost_Price, Last_Synced from Xero_Items where id is not null"))]

    def half_of(r):
        # a half-day request made on the page carries "(half day, morning off)" or "(half day, afternoon off)" in its record name
        m = re.search(r"\(half day(?:, (morning off|afternoon off))?\)", r.get("Name") or "", re.I)
        return (m.group(1) or True) if m and (r.get("To_Date") or r.get("From_Date")) == r.get("From_Date") else None

    # time off requests (custom module Leave_Requests, shown in Zoho as "Time Off Requests"; the reason stays in Zoho, it is not published)
    try:
        lv = z.all("select Name, Team_Member, Leave_Type, From_Date, To_Date, Cover_Arranged, Leave_Status, Decision_Note, Created_Time, Modified_By, Modified_Time from Leave_Requests where id is not null")
    except SystemExit as e:
        print("WARNING leave requests could not be read:", str(e)[:120])
        lv = None
    leave = None if lv is None else [{"id": r.get("id"), "name": r.get("Team_Member"), "type": r.get("Leave_Type"), "from": r.get("From_Date"), "to": r.get("To_Date") or r.get("From_Date"),
              "cover": r.get("Cover_Arranged"), "status": r.get("Leave_Status") or "Pending", "note": r.get("Decision_Note"),
              "asked": r.get("Created_Time"), "by": nm(r.get("Modified_By")) if (r.get("Leave_Status") in ("Approved", "Rejected")) else None,
              "when": r.get("Modified_Time"), "half": half_of(r)} for r in lv]

    # is a file attached to the request? sick leave must have the MC; other types may carry a vet bill and so on
    if leave:
        dpath = os.path.join(HERE, "raw", "doc_cache.json")
        try:
            with open(dpath) as f:
                docs = json.load(f)
        except (OSError, ValueError):
            docs = {}
        fresh = (today - timedelta(days=14)).isoformat()
        for l in leave:
            got = docs.get(l["id"])
            if not got:
                # a request stops being re-checked once it is decided and two weeks old (one Zoho call each, every run)
                if l["status"] != "Pending" and (l.get("asked") or "")[:10] < fresh:
                    l["doc"] = False if got is False else None
                    continue
                try:
                    j = z.req("/crm/v5/Leave_Requests/%s/Attachments?fields=id,File_Name" % l["id"])
                    got = docs[l["id"]] = [a.get("File_Name") or "document" for a in (j.get("data") or [])] or False
                except SystemExit as e:
                    print("WARNING attachments could not be read:", str(e)[:120])
                    got = docs[l["id"]] = None
            if got is True:   # older cache kept only yes/no
                got = ["document"]
            l["doc"] = None if got is None else bool(got)
            if got:
                l["docn"] = ", ".join(got)
        with open(dpath, "w") as f:
            json.dump({k: v for k, v in docs.items() if v}, f)

    # decisions made from the buttons in the approver's email (module Time_Off_Decisions, written by the page's
    # confirm step). A decision counts only with the private approval key, and only while Zoho still says Pending.
    if leave:
        try:
            with open(os.path.expanduser("~/.config/pawkit-cs360/config.json")) as f:
                pc = json.load(f)
        except (OSError, ValueError):
            pc = {}
        akey = pc.get("_approval_key")
        dec = []
        if akey:
            try:
                dec = z.all("select Request_ID, Decision, Approval_Key, Created_Time from Time_Off_Decisions where id is not null")
            except SystemExit as e:
                print("WARNING time off decisions could not be read:", str(e)[:120])
        byid = {l["id"]: l for l in leave if l["status"] == "Pending"}
        for d in sorted(dec, key=lambda d: d.get("Created_Time") or ""):
            l = byid.get((d.get("Request_ID") or "").strip())
            if l and d.get("Decision") in ("Approved", "Rejected") and hmac.compare_digest(str(d.get("Approval_Key") or ""), akey):
                l.update(status=d["Decision"], by="%s (by email)" % (pc.get("_approver_own", "Charlene") if l["name"] in (pc.get("_approver_own_names") or ["Vince"]) else pc.get("_approver", "Approver")), when=d.get("Created_Time"))

        # people whose own requests need nobody's approval (private config _no_approval): recorded as approved
        for l in leave:
            if l["status"] == "Pending" and l["name"] in (pc.get("_no_approval") or []):
                l.update(status="Approved", by="Own request, no approval needed", when=l.get("asked"))

    inv = [{"d": b.get("Date_Time"), "s": b.get("Booking_Status"), "inv": b.get("Invoice_Status"), "po": b.get("Paid_On"),
            "pm": b.get("Payment_Method"), "amt": b.get("Base_Price"), "cat": b.get("Service_Category")}
           for b in vrows if s30 <= (b.get("Date_Time") or "")[:10] <= end14]

    # leave credits (custom module Leave_Balances): one record per person and leave type with the year's allowance and
    # an opening balance on a date; build.py takes off approved time off from that date on. Admins edit it in Zoho.
    try:
        bal = [{"id": r.get("id"), "name": r.get("Team_Member"), "type": r.get("Leave_Type"), "allow": r.get("Allowance"),
                "open": r.get("Opening_Balance"), "from": r.get("Opening_Date")}
               for r in slow("leave_balances", 3600, lambda: z.all("select Team_Member, Leave_Type, Allowance, Opening_Balance, Opening_Date from Leave_Balances where id is not null"))]
    except SystemExit as e:
        print("WARNING leave balances could not be read:", str(e)[:120])
        bal = None

    # Service Satisfaction (ratings) and late-arrival credits, all time, at almost no Zoho cost (Vince 23 Nov: keep API credits to
    # a minimum). Both lists live in raw/csat_store.json and are topped up on every refresh from the visit rows already read above,
    # so no extra calls. Once a week the full lists are re-read (2 calls) to pick up changes to visits older than the visit window,
    # such as Late Credit Used ticked on an old booking.
    rating_row = lambda r: {"id": r.get("id"), "ref": r.get("Name"), "d": r.get("Date_Time"), "p": r.get("Partner_Name.Name"), "cat": r.get("Service_Category"),
                            "at": r.get("Survey_Sent_At"), "cf": r.get("Customer_Confirmed"), "cr": r.get("Customer_Rating"), "cm": r.get("Customer_Comment"), "dr": r.get("Dissatisfaction_Reason")}
    credit_row = lambda r: {"id": r.get("id"), "ref": r.get("Name"), "d": r.get("Date_Time"), "p": r.get("Partner_Name.Name"),
                            "who": r.get("Pet_Owner.Name") or r.get("Customer_Clean_Name"), "amt": r.get("Late_Credit_Amount"), "at": r.get("Late_Credit_Given_At"),
                            "x": r.get("Late_Credit_Expires"), "used": bool(r.get("Late_Credit_Used")), "dm": r.get("Delay_Minutes"), "why": r.get("Delay_Reason")}
    store_path = os.path.join(HERE, "raw", "csat_store.json")
    try:
        with open(store_path) as f:
            store = json.load(f)
    except (OSError, ValueError):
        store = {}
    rs, cs = store.setdefault("ratings", {}), store.setdefault("credits", {})
    try:
        for r in slow("ratings_weekly", 7 * 86400, lambda: z.all("select Name, Date_Time, Partner_Name.Name, Service_Category, Survey_Sent_At, Customer_Confirmed, Customer_Rating, Customer_Comment, Dissatisfaction_Reason from Booking where Survey_Sent_At is not null")):
            rs[r["id"]] = rating_row(r)
        for r in slow("credits_weekly", 7 * 86400, lambda: z.all("select Name, Date_Time, Partner_Name.Name, Pet_Owner.Name, Customer_Clean_Name, Late_Credit_Amount, Late_Credit_Given_At, Late_Credit_Expires, Late_Credit_Used, Delay_Minutes, Delay_Reason from Booking where Late_Credit_Given_At is not null")):
            cs[r["id"]] = credit_row(r)
    except SystemExit as e:
        print("WARNING weekly ratings/credits re-read skipped:", str(e)[:120])
    for r in vrows:   # the visit rows read above: newest answers and credits, no extra call
        if r.get("Survey_Sent_At"):
            rs[r["id"]] = rating_row(r)
        if r.get("Late_Credit_Given_At"):
            cs[r["id"]] = credit_row(r)
    tmp = store_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f)
    os.replace(tmp, store_path)
    ratings, credits = list(rs.values()), list(cs.values())
    # running-late rules notice: who has it and who tapped "I have read this" (2 reads, cached 6 h)
    notice = {"sent": [], "read": []}
    try:
        def norm(n):
            d = "".join(ch for ch in str(n or "") if ch.isdigit())
            return "65" + d if len(d) == 8 else d
        reads = {norm(r.get("Name")) for r in slow("notice_reads", 6 * 3600, lambda: z.all("select Name from Provider_Messages where Booking_Ref = 'LATE2311' and Message_Kind = 'ReadAck'"))}
        for r in slow("notice_sent", 6 * 3600, lambda: z.all("select Name, Phone_Number, Lateness_Notice_Sent_At from Partner where Lateness_Notice_Sent_At is not null")):
            notice["sent"].append({"p": (r.get("Name") or "").strip(), "at": r.get("Lateness_Notice_Sent_At"), "read": norm(r.get("Phone_Number")) in reads})
        notice["read"] = len(reads)
    except SystemExit as e:
        print("WARNING notice read skipped:", str(e)[:120])

    out = {"generated": now.isoformat(timespec="seconds"), "visits_until": end_vis, "wa": wa, "wa_now": wa_now, "created": created, "visits": visits, "comp": comp, "cust": hist["cust"], "leave": leave,
           "bal": bal, "tasks": tasks, "orders": orders, "app_orders": app_orders, "sheet_roster": sheet_roster(z), "tracker_rows": tracker_rows(z), "items": items, "inv": inv, "ratings": ratings, "credits": credits, "notice": notice, "owners": owners}
    tmp = os.path.join(HERE, "raw", "crm.json.tmp")
    with open(tmp, "w") as f:
        json.dump(out, f)
    os.replace(tmp, os.path.join(HERE, "raw", "crm.json"))
    print(now.strftime("%Y-%m-%d %H:%M"), "ok", len(created), "made,", len(visits), "visits,", len(tasks), "tasks,", len(comp), "complaints,", Zoho.calls["n"], "zoho calls,", Zoho.calls["rows"], "coql rows")


if __name__ == "__main__":
    fast() if "--fast" in sys.argv else main()
