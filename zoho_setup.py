#!/usr/bin/env python3
"""One-time Zoho sign-in for the CS360 refresh.

Start it, then in the Zoho API Console (Chrome) click DOWNLOAD in the "Generated Code" box.
The script picks up ~/Downloads/self_client.json (Client ID, Client Secret and code), swaps the
code for a long-lived read-only token, saves ~/.config/pawkit-cs360/zoho.json (readable only by
you) and deletes the downloaded file. Nothing secret is printed.

Fallback if the download doesn't land in ~/Downloads: click the three Copy buttons instead
(code, Client ID, Client Secret, in any order). The clipboard is read and then cleared.
"""
import glob, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

OUT = os.path.expanduser("~/.config/pawkit-cs360/zoho.json")
DL = os.environ.get("ZOHO_SETUP_DIR") or os.path.expanduser("~/Downloads")
WAIT = int(os.environ.get("ZOHO_SETUP_WAIT", "540"))
NEED = ["ZohoCRM.coql.READ", "ZohoCRM.modules.READ", "ZohoCRM.users.READ", "ZohoSheet.dataAPI.READ"]
SHAPES = {
    "code": r"1000\.[0-9a-f]{20,}\.[0-9a-f]{20,}",
    "cid": r"1000\.[A-Z0-9]{20,}",
    "sec": r"[0-9a-f]{30,}",
}
NAMES = {"code": "Generated code", "cid": "Client ID", "sec": "Client Secret"}


def say(msg):
    print(msg, flush=True)


def clip():
    try:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def classify(v):
    return next((k for k in ("code", "cid", "sec") if re.fullmatch(SHAPES[k], v or "")), None)


def from_download(since, seen):
    """Newest self_client*.json saved after `since`. Returns (path, values) or None."""
    paths = []
    for p in glob.glob(os.path.join(DL, "self_client*.json")):
        try:
            if p not in seen and os.path.getmtime(p) >= since:
                paths.append((os.path.getmtime(p), p))
        except OSError:
            pass
    for _, p in sorted(paths, reverse=True):
        try:
            with open(p) as f:
                j = json.load(f)
        except (OSError, ValueError):
            continue  # still being written
        vals = {"cid": j.get("client_id"), "sec": j.get("client_secret"), "code": j.get("code")}
        if all(isinstance(v, str) and v.strip() for v in vals.values()):
            return p, {k: v.strip() for k, v in vals.items()}
    return None


def exchange(got):
    data = urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": got["cid"],
                                   "client_secret": got["sec"], "code": got["code"]}).encode()
    try:
        req = urllib.request.Request("https://accounts.zoho.com/oauth/v2/token", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        return None, "HTTP %s" % e.code
    except Exception as e:
        return None, type(e).__name__
    if "refresh_token" not in j:
        return None, str(j.get("error", "no refresh token"))
    missing = [x for x in NEED if x not in (j.get("scope") or "").replace(",", " ").split()]
    if missing:   # saving this would break the dashboard: keep the sign-in that works
        return None, "the code was made without: " + ", ".join(missing)
    return j, None


def save(got, j):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    try:   # keep the sign-in being replaced, so a switch of client can be undone (same private folder, same permissions)
        with open(OUT) as f:
            old = f.read()
        bfd = os.open(OUT + ".prev", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(bfd, "w") as f:
            f.write(old)
    except OSError:
        pass
    fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"client_id": got["cid"], "client_secret": got["sec"], "refresh_token": j["refresh_token"],
                   "api_domain": j.get("api_domain", "https://www.zohoapis.com"),
                   "accounts": "https://accounts.zoho.com"}, f)
    try:   # the short-lived token kept by the refresh was made with the old access: drop it
        os.remove(os.path.join(os.path.dirname(OUT), "zoho_token.json"))
    except OSError:
        pass


def main():
    start = time.time() - 2
    got, seen, used_clip = {}, set(), False
    last_clip = clip()  # whatever is on the clipboard now is stale: ignore it
    try:   # a renewal: the Client ID and Secret are already saved, so only a new code is needed (DOWNLOAD or the code's COPY button)
        with open(OUT) as f:
            old = json.load(f)
        if old.get("client_id") and old.get("client_secret"):
            got.update(cid=old["client_id"], sec=old["client_secret"])
    except (OSError, ValueError):
        pass
    try:
        os.listdir(DL)
    except OSError:
        say("Note: can't read %s, so use the Copy buttons instead of DOWNLOAD." % DL)
    say("Waiting up to %d minutes. In Chrome, click DOWNLOAD in the 'Generated Code' box." % (WAIT // 60))
    deadline = time.time() + WAIT
    while time.time() < deadline:
        hit = from_download(start, seen)
        if hit:
            p, vals = hit
            seen.add(p)
            got.update(vals)
            try:
                os.remove(p)  # it holds the Client Secret: don't leave it in Downloads
            except OSError:
                pass
            say("  Got the downloaded file (and removed it from Downloads).")
        else:
            v = clip()
            if v and v != last_clip:
                last_clip = v
                k = classify(v)
                if k and got.get(k) != v:
                    got[k] = v
                    used_clip = True
                    say("  OK, %s received from the clipboard." % NAMES[k])
        if len(got) == 3:
            j, err = exchange(got)
            if j:
                save(got, j)
                if used_clip:
                    subprocess.run(["pbcopy"], input="", text=True)
                say("Saved. Zoho access is set up for the CS360 refresh.")
                return 0
            say("  Zoho refused it (%s). A code works once and lasts 10 minutes: a new code is needed." % err)
            got.pop("code", None)
        time.sleep(0.7)
    say("Timed out waiting. Nothing was saved.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
