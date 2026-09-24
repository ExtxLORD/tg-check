#!/usr/bin/env python3
"""Static verification: bot.py must contain no mail-mutating or sending code paths."""
import re
import sys
import warnings

warnings.simplefilter("error", SyntaxWarning)
sys.path.insert(0, ".")
import bot as b  # noqa: E402  (import must be clean: no SyntaxWarning)

src = open("bot.py", encoding="utf-8").read()
# Look only at real code: strip docstrings and comments
code = re.sub(r'r?"""[\s\S]*?"""', '""', src)
code = re.sub(r"#.*", "", code)

fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + f" {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        fails.append(name)


FORBIDDEN = {
    "smtplib": r"\bsmtplib\b",
    "SMTP()": r"\bSMTP\s*\(",
    "DELETE": r"['\"]DELETE['\"]|\bDELETE\s*\(",
    "EXPUNGE": r"['\"]EXPUNGE['\"]|\bEXPUNGE\s*\(",
    "MOVE": r"['\"]MOVE['\"]",
    "-FLAGS": r"-FLAGS",
    "APPEND": r"['\"]APPEND['\"]",
    "RENAME": r"['\"]RENAME['\"]",
}
for name, pat in FORBIDDEN.items():
    check(f"code has no {name}", not re.search(pat, code))

check("select readonly=True", "readonly=True" in code)
check("BODY.PEEK used", "BODY.PEEK[]" in code)
n_store = len(re.findall(r"uid\(\s*['\"]store['\"]", code))
check("exactly 1 uid(store) path", n_store == 1, f"found {n_store}")
check(
    "that store is +FLAGS (\\Seen) only",
    bool(re.search(
        r"uid\(\s*['\"]store['\"],\s*uid,\s*['\"]\+FLAGS['\"],\s*r?['\"]\(\\Seen\)['\"]",
        code,
    )),
)

tg_posts = set(re.findall(r'_post\(\s*["\'](\w+)["\']', code))
tg_posts.add("getUpdates")  # called via self.tg.get_updates -> requests.get
allowed_tg = {"sendMessage", "sendDocument", "getMe", "getUpdates"}
check("Telegram methods limited to read-out", tg_posts <= allowed_tg, str(tg_posts))

# no other chat allowed: sendMessage/sendDocument always use cfg.tg_chat_id
check("all TG sends use configured chat_id",
      code.count("self.cfg.tg_chat_id") >= 3 and "chat_id\": msg" not in code)

# parsing smoke (regression of earlier suite)
import base64
raw = "\r\n".join([
    "From: a@b.c", "Subject: =?UTF-8?B?" + base64.b64encode("Привет".encode()).decode() + "?=",
    "MIME-Version: 1.0", 'Content-Type: multipart/mixed; boundary="X"', "",
    "--X", "Content-Type: text/plain; charset=utf-8", "", "текст письма", "",
    "--X", 'Content-Type: application/pdf; name="f.pdf"',
    'Content-Disposition: attachment; filename="f.pdf"',
    "Content-Transfer-Encoding: base64", "",
    base64.b64encode(b"%PDF").decode(), "--X--",
]).encode()
m = b.parse_message(raw, "9")
check("parse: body", "текст письма" in m.body)
check("parse: subject", "Привет" in m.subject)
check("parse: attachment", len(m.attachments) == 1 and m.attachments[0].data == b"%PDF")

chunks = b.split_text("A" * 9000 + "\n" + "Б" * 9000, 4000)
check("split_text lossless", "".join(chunks) == "A" * 9000 + "\n" + "Б" * 9000)
check("split_text within limit", all(len(c) <= 4000 for c in chunks))

print()
print("ALL PASSED" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
