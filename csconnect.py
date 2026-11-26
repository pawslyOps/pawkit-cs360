#!/usr/bin/env python3
"""Pawkit Ops360: encrypt the CS Connect meeting notes for the Customer Service > CS Connect sub-tab.

Usage: python3 -I csconnect.py
Reads the notes from ~/.config/pawkit-cs360/csconnect.json and the CS Connect passcode from
~/.config/pawkit-cs360/csconnect-passcode (both private, never in this public repo).
Writes docs/csconnect.js (PBKDF2-SHA256 + AES-256-GCM, decrypted in the browser), then commit and push main.
The CS Connect passcode is separate from the team passcode, so the notes stay locked for anyone who only has that.
"""
import base64, json, os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.expanduser("~/.config/pawkit-cs360")
ITER = 310000


def main():
    notes = json.load(open(os.path.join(CONF, "csconnect.json")))
    pw = open(os.path.join(CONF, "csconnect-passcode")).read().strip()
    if not pw:
        raise SystemExit("csconnect-passcode is empty")
    plain = json.dumps(notes, separators=(",", ":")).encode()
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(pw.encode())
    ct = AESGCM(key).encrypt(iv, plain, None)
    b = lambda x: base64.b64encode(x).decode()
    out = {"v": 1, "iter": ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}
    with open(os.path.join(HERE, "docs", "csconnect.js"), "w") as f:
        f.write("window.CS_CONNECT=" + json.dumps(out) + ";\n")
    print("wrote docs/csconnect.js, %d meeting(s), %d bytes plain" % (len(notes.get("meetings", [])), len(plain)))


if __name__ == "__main__":
    main()
