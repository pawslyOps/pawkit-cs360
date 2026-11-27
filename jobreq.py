"""Job requests: how long it takes to find a provider for a customer's request, captured from the WhatsApp logs.

CS sends each job to providers from the Pawkit line as a free-text message ("$100 bf pawkit coms ... #4314"), the
provider answers in words ("Tomorrow at 12", "Cannot"), CS sends "CONFIRMED AND INVITE SENT" to the one who takes it,
and a Booking is made in Zoho. Nothing is typed in by hand: this reads those messages and builds one row per job.

The "#4314" in a job request is the last 4 digits of the customer's WhatsApp number. It links the job to the Booking
(through the booking's Pet Owner mobile). A job request with no #number is grouped by its postcode instead and
cannot be matched to a booking.
"""
import json, re
from datetime import datetime, timedelta

W = "woztellplatformintegration__"
DAYS = 45                 # how far back to read job requests
GROUP_DAYS = 7            # messages with the same #number or postcode within this many days are one job
REPLY_HOURS = 24          # a provider's messages within this many hours of a job request count as the answer
JOB_RX = re.compile(r"pawkit\s*com", re.I)
CONF_RX = re.compile(r"confirmed", re.I)
NUM_RX = re.compile(r"#\s?(\d{4})(?![\d-])")
POST_RX = re.compile(r"(?:S\.?\s?|Singapore\s?)(\d{6})\b|\b(\d{6})\b")
NO_RX = re.compile(r"^\s*(i\s+)?(no|nope|cannot|can't|cant|can not|not available|not free|sorry|pass|full|no slot|unable|busy)\b", re.I)
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def text_of(r):
    try:
        return (json.loads(r.get(W + "Message_Data") or "null") or {}).get("text") or ""
    except (ValueError, AttributeError):
        return ""


def when(r):
    return datetime.fromisoformat(r.get(W + "Message_Date_time") or r.get("Created_Time"))


def digits(s):
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def key_of(t):
    m = NUM_RX.search(t)
    if m:
        return "#" + m.group(1)
    m = POST_RX.search(t)
    if m:
        return "S" + (m.group(1) or m.group(2))
    return None


def service_of(t):
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    for l in lines[1:]:
        if re.match(r"^\d+\s*x\b", l, re.I) or "groom" in l.lower() or "shower" in l.lower():
            return re.sub(r"\s*#\s?\d{4}\b", "", l)[:80]
    return (lines[1] if len(lines) > 1 else lines[0] if lines else "")[:80]


def parse_slot(txt, sent):
    """Best-effort reading of a provider's answer ('Tomorrow at 12', 'Sat 2pm', '2 Dec 11am', '4.30') into a date and
    time. Returns an ISO string, or None when there is no clear day or time. The words are always shown beside it."""
    t = txt.lower().replace("tmr", "tomorrow").replace("tml", "tomorrow").replace("weekwnd", "weekend")
    day = None
    base = sent.date()
    if "today" in t or "tonight" in t:
        day = base
    elif "tomorrow" in t:
        day = base + timedelta(days=1)
    elif "weekend" in t:
        day = base + timedelta(days=(5 - base.weekday()) % 7 or 7)
    else:
        m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", t) or \
            re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s*(\d{1,2})\b", t)
        if m:
            a, b = m.groups()
            d, mon = (int(a), MONTHS[b]) if a.isdigit() else (int(b), MONTHS[a])
            try:
                day = base.replace(month=mon, day=d)
                if day < base - timedelta(days=30):
                    day = day.replace(year=day.year + 1)
            except ValueError:
                day = None
        elif re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", t):
            d = int(re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", t).group(1))
            for add in range(0, 62):          # the next date with that day of the month
                c = base + timedelta(days=add)
                if c.day == d:
                    day = c
                    break
        else:
            for i, w in enumerate(WEEKDAYS):
                if re.search(r"\b" + w, t):
                    day = base + timedelta(days=(i - base.weekday()) % 7 or 7)
                    break
    rest = re.sub(r"\b\d{1,2}\s*(?:st|nd|rd|th)?\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*|\b\d{1,2}\s*(?:st|nd|rd|th)\b|\b\d+\s*(?:mins?|minutes?|hrs?|hours?|cats?|dogs?|pax|\$)", "", t)
    rest = re.sub(r"\$\s?\d+", "", rest)
    m = re.search(r"\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm|noon)?\b", rest)
    hh = mm = None
    if m and (m.group(2) or m.group(3) or re.search(r"\b(at|@|from|after|by|around)\s*" + re.escape(m.group(0).strip()), rest)):
        h = int(m.group(1)); mm = int(m.group(2) or 0); ap = m.group(3)
        if h <= 23 and mm < 60:
            if ap == "pm" and h < 12:
                h += 12
            elif not ap and 1 <= h <= 7:      # "at 2" for a home visit means 2pm
                h += 12
            hh = h
    if day is None and hh is None:
        return None
    if day is None:
        day = base if (sent.hour < hh if hh is not None else False) else base + timedelta(days=1)
    return datetime(day.year, day.month, day.day, hh if hh is not None else 0, mm or 0, tzinfo=sent.tzinfo).isoformat(timespec="minutes") + ("" if hh is not None else "|day")


PET_SKIP = {"cat", "dog", "cats", "dogs", "baby", "boy", "girl", "rabbit", "bunny", "puppy", "kitty", "test", "na", "nil", "none", "pet"}


def pet_in(name, low):
    """The booking's pet name, as a whole word, in the job request (each pet of 'Tum Tum & Dobby' counts)."""
    for n in re.split(r"\s*(?:&|,|/|\band\b)\s*", (name or "").lower()):
        n = n.strip()
        if len(n) >= 3 and n not in PET_SKIP and re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", low):
            return True
    return False


def job_requests(z, now):
    since = (now - timedelta(days=DAYS)).isoformat(timespec="seconds")
    sel = "select %sFrom, %sTo, %sDirection, %sMessage_Data, %sMessage_Date_time from %sWoztell_logs" % (W, W, W, W, W, W)
    out_rows = []
    for word in ("pawkit com", "ONFIRMED"):
        out_rows += z.all("%s where ((%sDirection = 'Out' and %sMessage_Date_time >= '%s') and %sMessage_Data like '%%%s%%')" % (sel, W, W, since, W, word))
    reqs = [r for r in out_rows if JOB_RX.search(text_of(r))]
    confs = [r for r in out_rows if CONF_RX.search(text_of(r)) and not JOB_RX.search(text_of(r))]
    provs = sorted({r.get(W + "To") for r in reqs if r.get(W + "To")})
    replies = {}
    for i in range(0, len(provs), 20):   # the providers' own messages in the same window
        part = provs[i:i + 20]
        cond = "%sFrom in (%s)" % (W, ", ".join("'%s'" % p for p in part))
        for r in z.all("%s where ((%sDirection = 'In' and %sMessage_Date_time >= '%s') and %s)" % (sel, W, W, since, cond)):
            replies.setdefault(r.get(W + "From"), []).append(r)
    for v in replies.values():
        v.sort(key=when)

    # provider names from the Partner records (phone -> name)
    names = {}
    for r in z.all("select Name, Phone_Number from Partner where Phone_Number is not null"):
        d = digits(r.get("Phone_Number"))
        d = "65" + d if len(d) == 8 else d
        names[d] = (r.get("Name") or "").strip()

    # bookings made in the window, keyed by the last 4 digits of the customer's mobile
    # and by the service address postcode, for job requests sent without the #number
    bk, bypc, allbk = {}, {}, []
    for r in z.all("select Name, Date_Time, Created_Time, Booking_Status, Partner_Name.Name, Service_Category, Postal_Code, Pet_Name, Pet_Owner.Mobile_Number from Booking where Created_Time >= '%s'"
                   % (now - timedelta(days=DAYS + 14)).isoformat(timespec="seconds")):
        d = digits(r.get("Pet_Owner.Mobile_Number"))
        if len(d) >= 4:
            bk.setdefault(d[-4:], []).append(r)
        allbk.append(r)
        pc = digits(r.get("Postal_Code"))
        if pc:
            bypc.setdefault(pc.zfill(6), []).append(r)

    # group job requests into jobs
    jobs = []
    for r in sorted(reqs, key=when):
        t, at = text_of(r), when(r)
        k = key_of(t) or "T" + re.sub(r"\W+", "", t.lower())[:60]
        j = next((j for j in jobs if j["key"] == k and (at - j["_first"]).days < GROUP_DAYS), None)
        if not j:
            j = {"key": k, "_first": at, "first": at.isoformat(timespec="minutes"), "svc": service_of(t),
                 "price": (re.search(r"\$\s?(\d+)", t) or [None, None])[1], "num": k[1:] if k[0] == "#" else None, "p": {},
                 "pc": next((a or b for a, b in POST_RX.findall(t)), None), "_t": t}
            jobs.append(j)
        to = r.get(W + "To")
        j["p"].setdefault(to, {"n": names.get(to) or "…" + (to or "")[-4:], "sent": at.isoformat(timespec="minutes"), "_sent": at})

    # each provider's answer: their messages after the request, before the next request to them, within REPLY_HOURS
    sends = {}
    for j in jobs:
        for to, p in j["p"].items():
            sends.setdefault(to, []).append(p["_sent"])
    for v in sends.values():
        v.sort()
    for j in jobs:
        for to, p in j["p"].items():
            s = p["_sent"]
            nxt = next((x for x in sends[to] if x > s), None)
            end = min(s + timedelta(hours=REPLY_HOURS), nxt) if nxt else s + timedelta(hours=REPLY_HOURS)
            ans = [m for m in replies.get(to, []) if s < when(m) <= end and text_of(m).strip()]
            if not ans:
                p["st"] = "no reply"
                continue
            p["rep"] = when(ans[0]).isoformat(timespec="minutes")
            p["mins"] = round((when(ans[0]) - s).total_seconds() / 60)
            words = " / ".join(text_of(m).strip() for m in ans[:4])[:160]
            p["say"] = words
            slot = None
            for m in ans[:4]:
                slot = parse_slot(text_of(m), when(m))
                if slot:
                    break
            p["st"] = "offered" if slot else "declined" if any(NO_RX.search(text_of(m)) for m in ans[:4]) else "replied"
            if slot:
                p["slot"] = slot

    # the provider CS confirmed, and the booking that came out of it
    for j in jobs:
        f = j["_first"]
        for c in sorted(confs, key=when):
            t, at = text_of(c), when(c)
            if not (f <= at <= f + timedelta(days=GROUP_DAYS)) or c.get(W + "To") not in j["p"]:
                continue
            if j["key"][1:] in digits(t) or (j["key"][0] == "#" and len(j["p"]) == 1) or j["key"][0] == "S" and j["key"][1:] in t:
                j["conf"] = at.isoformat(timespec="minutes")
                j["cp"] = j["p"][c.get(W + "To")]["n"]
                m = re.search(r"Date\s*&?\s*Time:?\s*\*?([^\n*]+)", t, re.I)
                if m:
                    j["cw"] = m.group(1).strip()[:60]
                break
        # the booking: first by the #number (customer's phone), else by the postcode in the job request. CS often makes the
        # booking record before asking providers, so a booking made up to 2 days before the first job request counts.
        low = j["_t"].lower()
        pets = [b for b in allbk if pet_in(b.get("Pet_Name"), low)]
        for how, pool, before in (("#number", bk.get(j["num"]) if j["num"] else None, 2), ("postcode", bypc.get(j["pc"]) if j.get("pc") else None, 2),
                                  ("pet name", pets, 7)):
            cand = [b for b in (pool or []) if f - timedelta(days=before) <= datetime.fromisoformat(b["Created_Time"]) <= f + timedelta(days=GROUP_DAYS + 7)]
            if cand:
                b = min(cand, key=lambda b: (0 if j.get("cp") and b.get("Partner_Name.Name") == j["cp"] else 1,
                                             abs((datetime.fromisoformat(b["Created_Time"]) - f).total_seconds())))
                j["bk"] = {"id": b["id"], "ref": b.get("Name"), "d": b.get("Date_Time"), "s": b.get("Booking_Status"),
                           "p": b.get("Partner_Name.Name"), "made": b.get("Created_Time"), "by": how}
                break

    res = []
    for j in jobs:
        ps = sorted(j["p"].values(), key=lambda p: p["sent"])
        for p in ps:
            p.pop("_sent", None)
        j.pop("_t", None)
        offers = sorted(p["slot"] for p in ps if p.get("slot"))
        firstrep = min((p["rep"] for p in ps if p.get("rep")), default=None)
        row = {"num": j["num"], "key": j["key"] if j["key"][0] != "T" else None, "svc": j["svc"], "price": j["price"], "first": j["first"],
               "n": len(ps), "p": ps, "first_reply": firstrep, "earliest": offers[0] if offers else None,
               "first_offer": next((p["slot"] for p in sorted((p for p in ps if p.get("slot")), key=lambda p: p["rep"])), None)}
        for k in ("conf", "cp", "cw", "bk"):
            if k in j:
                row[k] = j[k]
        res.append(row)
    res.sort(key=lambda r: r["first"], reverse=True)
    return {"asof": now.isoformat(timespec="minutes"), "days": DAYS, "jobs": res}


if __name__ == "__main__":   # quick look: python3 jobreq.py
    import refresh
    from datetime import timezone
    r = job_requests(refresh.Zoho(), datetime.now(timezone(timedelta(hours=8))))
    print(json.dumps(r, indent=1)[:6000])
    js = r["jobs"]
    print(len(js), "jobs;", sum(1 for j in js if j["num"]), "with #number;", sum(1 for j in js if j.get("bk")), "matched to a booking;",
          sum(1 for j in js if j.get("conf")), "confirmed;", sum(1 for j in js if j.get("earliest")), "with an offered slot")
