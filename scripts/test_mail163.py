#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mail163.py 的离线回归测试: 启动一个最小 IMAP4rev1 服务端, 塞进几封"刁钻"邮件,
验证解析管线正确。

覆盖的坑:
  - 中文 Subject (RFC2047 base64)
  - quoted-printable 正文
  - base64 正文
  - 纯 HTML 正文 (无 text/plain)
  - multipart/alternative + 附件
  - 附件文件名 RFC2047 编码
  - 缺少 Date 头

用法: python test_mail163.py
"""

import base64
import email.utils
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mail163  # noqa: E402

HOST = "127.0.0.1"
PORT = 15963

SUBJ_B64 = base64.b64encode("关于示例模型的回复".encode("utf-8")).decode()
ATTACH_B64 = base64.b64encode("报告".encode("utf-8")).decode()

MAIL_1 = (
    "From: Example, Researcher <researcher@example.org>\r\n"
    "To: user@163.com\r\n"
    "Subject: =?UTF-8?B?" + SUBJ_B64 + "?=\r\n"
    "Date: Mon, 20 Oct 2026 09:12:33 +0800\r\n"
    "Message-ID: <abc123@mail.163.com>\r\n"
    "Content-Type: text/plain; charset=\"UTF-8\"\r\n"
    "Content-Transfer-Encoding: quoted-printable\r\n"
    "\r\n"
    "Hi,\r\n"
    "\r\n"
    "Thanks for the =E6=A0=B8=E5=BF=83=E6=A8=A1=E5=9E=8B progress.\r\n"
    "The Example model looks reasonable.\r\n"
    "\r\n"
    "Best,\r\n"
    "Researcher\r\n"
).encode("utf-8")

_B64_BODY = base64.b64encode(
    "这是一封 base64 编码的中文正文。\n第二行内容。\n".encode("utf-8")
).decode()

MAIL_2 = (
    "From: zhang.wei@example.com\r\n"
    "To: user@163.com\r\n"
    "Subject: Plain ASCII subject\r\n"
    "Date: Tue, 21 Oct 2026 14:00:00 +0800\r\n"
    "Content-Type: text/plain; charset=\"UTF-8\"\r\n"
    "Content-Transfer-Encoding: base64\r\n"
    "\r\n"
    + _B64_BODY + "\r\n"
).encode("utf-8")

MAIL_3 = (
    "From: noreply@service.com\r\n"
    "To: user@163.com\r\n"
    "Subject: HTML only mail\r\n"
    "Date: Wed, 22 Oct 2026 08:00:00 +0800\r\n"
    "Content-Type: text/html; charset=\"UTF-8\"\r\n"
    "\r\n"
    "<html><head><style>p{color:red}</style></head><body>"
    "<p>Hello &amp; welcome</p>"
    "<div>Line one<br>Line two</div>"
    "<script>alert('x')</script>"
    "</body></html>\r\n"
).encode("utf-8")

MAIL_4 = (
    "From: boss@corp.com\r\n"
    "To: user@163.com\r\n"
    "Subject: =?UTF-8?B?" + base64.b64encode("附件测试".encode("utf-8")).decode() + "?=\r\n"
    "Date: Thu, 23 Oct 2026 10:30:00 +0800\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: multipart/mixed; boundary=\"BB\"\r\n"
    "\r\n"
    "--BB\r\n"
    "Content-Type: text/plain; charset=\"UTF-8\"\r\n"
    "\r\n"
    "See attachment.\r\n"
    "--BB\r\n"
    "Content-Type: application/octet-stream; name=\"=?UTF-8?B?" + ATTACH_B64 + "?=\"\r\n"
    "Content-Transfer-Encoding: base64\r\n"
    "Content-Disposition: attachment; filename=\"=?UTF-8?B?" + ATTACH_B64 + "?=\"\r\n"
    "\r\n"
    + base64.b64encode(b"binary-data-here").decode() + "\r\n"
    "--BB--\r\n"
).encode("utf-8")

MAIL_5 = (
    "From: odd@nowhere.com\r\n"
    "To: user@163.com\r\n"
    "Subject: No date header\r\n"
    "Content-Type: text/plain; charset=\"UTF-8\"\r\n"
    "\r\n"
    "This mail has no Date header at all.\r\n"
).encode("utf-8")

MAILS = [MAIL_1, MAIL_2, MAIL_3, MAIL_4, MAIL_5]


def parse_cmd(line):
    """把 IMAP 命令行拆成 (name, rest)。

    注意: imaplib 的 tag 是随机的 base26 字符串(如 "AB3CD"), 不是 "a1"。
    所以这里不能假设 tag 以 "a" 开头, 必须按 "第一个 token 是 tag" 来切。
    """
    line = line.strip()
    if not line or line.startswith("*") or line.startswith("+"):
        return "", ""
    parts = line.split(" ", 1)
    rest = parts[1] if len(parts) > 1 else ""
    name = rest.split(" ")[0].upper() if rest else ""
    return name, rest


def handle(conn):
    f = conn.makefile("rwb")
    f.write(b"* OK mock imap ready\r\n")
    f.flush()
    while True:
        raw = f.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        name, rest = parse_cmd(line)
        tag = line.split(" ")[0]

        # imaplib 的 uid() 方法发的是 "UID FETCH ..." / "UID SEARCH ...",
        # wire 上的命令名是 UID, 真实命令在后面。这里剥掉 UID 前缀。
        uid_mode = False
        if name == "UID" and rest:
            uid_mode = True
            rest = rest[4:].lstrip() if rest.upper().startswith("UID ") else rest
            name = rest.split(" ")[0].upper() if rest else ""

        if name == "CAPABILITY":
            f.write(b"* CAPABILITY IMAP4rev1 ID UIDPLUS\r\n")
            f.write(("%s OK CAPABILITY done\r\n" % tag).encode())
        elif name == "ID":
            f.write(b'* ID ("name" "mock")\r\n')
            f.write(("%s OK ID done\r\n" % tag).encode())
        elif name == "LOGIN":
            f.write(("%s OK LOGIN done\r\n" % tag).encode())
        elif name == "LIST":
            f.write(b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n')
            f.write(("%s OK LIST done\r\n" % tag).encode())
        elif name in ("SELECT", "EXAMINE"):
            f.write(b"* %d EXISTS\r\n" % len(MAILS))
            f.write(b"* OK [UIDVALIDITY 1] UIDs valid\r\n")
            f.write(("%s OK [READ-WRITE] SELECT done\r\n" % tag).encode())
        elif name == "SEARCH":
            f.write(b"* SEARCH " + b" ".join(str(i + 1).encode() for i in range(len(MAILS))) + b"\r\n")
            f.write(("%s OK SEARCH done\r\n" % tag).encode())
        elif name == "FETCH":
            # 形如: FETCH 1,2,3 (BODY.PEEK[])  -> 每 UID 返回一条 FETCH 响应
            tokens = rest.split(" ")
            msg_set = tokens[1] if len(tokens) > 1 else ""
            hdr_only = "HEADER.FIELDS" in rest
            uids = []
            for chunk in msg_set.split(","):
                chunk = chunk.strip()
                if chunk.isdigit():
                    uids.append(int(chunk))
            if not uids:
                uids = [1]
            for uid in uids:
                idx = max(0, min(uid - 1, len(MAILS) - 1))
                raw_mail = MAILS[idx]
                if hdr_only:
                    payload = raw_mail.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                else:
                    payload = raw_mail
                f.write(("* %d FETCH (UID %d BODY[] {%d}\r\n" % (uid, uid, len(payload))).encode())
                f.write(payload)
                f.write(b")\r\n")
            f.write(("%s OK FETCH done\r\n" % tag).encode())
        elif name in ("LOGOUT",):
            f.write(b"* BYE\r\n")
            f.write(("%s OK LOGOUT done\r\n" % tag).encode())
            f.flush()
            break
        elif name == "CLOSE":
            f.write(("%s OK CLOSE done\r\n" % tag).encode())
        else:
            f.write(("%s OK %s done\r\n" % (tag, name)).encode())
        f.flush()
    f.close()


def serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(5)
    while True:
        conn, _addr = srv.accept()
        t = threading.Thread(target=handle, args=(conn,), daemon=True)
        t.start()


PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % label)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (label, detail))


def main():
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(0.4)

    # 把 mail163 指向 mock
    mail163.IMAP_HOST = HOST
    mail163.IMAP_PORT = PORT

    # mock 是明文, 需要一个不校验证书的 context
    orig_connect = mail163.connect

    def fake_connect(user, code, timeout=25):
        import imaplib as _il
        M = _il.IMAP4(HOST, PORT)
        M._simple_command("ID", '("name" "mail163" "version" "1.0")')
        M.login(user, code)
        return M, b"mock"

    mail163.connect = fake_connect

    print("\n[1] 解析: quoted-printable + RFC2047 中文主题")
    M, _ = mail163.connect("u", "p")
    m1 = mail163.fetch_message(M, 1)
    check("中文主题正确解码", m1["subject"] == "关于示例模型的回复", repr(m1["subject"]))
    check("QP 正文解码", "核心模型" in m1["body"], repr(m1["body"][:60]))
    check("发件人解析", "researcher@example.org" == m1["from_addr"], m1["from_addr"])
    check("日期解析为 ISO", (m1["date"] or "").startswith("2026-10-20"), m1["date"])

    print("\n[2] 解析: base64 正文")
    m2 = mail163.fetch_message(M, 2)
    check("base64 正文解码", "这是一封 base64 编码的中文正文" in m2["body"], repr(m2["body"][:60]))
    check("换行保留", "第二行内容" in m2["body"])

    print("\n[3] 解析: 纯 HTML 正文")
    m3 = mail163.fetch_message(M, 3)
    check("HTML 转纯文本", "Hello & welcome" in m3["body"], repr(m3["body"][:80]))
    check("script 被剥离", "alert" not in m3["body"], repr(m3["body"]))
    check("style 被剥离", "color:red" not in m3["body"])
    check("br 转换行", "Line one" in m3["body"] and "Line two" in m3["body"])

    print("\n[4] 解析: multipart + 附件")
    m4 = mail163.fetch_message(M, 4)
    check("正文取出", "See attachment." in m4["body"], repr(m4["body"]))
    check("附件被识别", len(m4["attachments"]) == 1, str(m4["attachments"]))
    if m4["attachments"]:
        check("附件名 RFC2047 解码", m4["attachments"][0]["filename"] == "报告",
              repr(m4["attachments"][0]["filename"]))
        check("附件大小正确", m4["attachments"][0]["size"] == len(b"binary-data-here"),
              str(m4["attachments"][0]["size"]))

    print("\n[5] 解析: 缺 Date 头不崩")
    m5 = mail163.fetch_message(M, 5)
    check("date 为 None 而非抛异常", m5["date"] is None, str(m5["date"]))
    check("body 正常", "no Date header" in m5["body"])

    print("\n[6] 列表 / 搜索")
    rows = mail163.list_headers(M, "INBOX", limit=10)
    check("列表返回 5 封", len(rows) == 5, str(len(rows)))
    check("列表含 uid", all("uid" in r for r in rows))
    found = mail163.search_messages(M, sender="example.org", limit=10)
    check("按发件人搜到 1 封", len(found) == 1, str(len(found)))
    found2 = mail163.search_messages(M, subject="HTML only", limit=10)
    check("按主题搜到 1 封", len(found2) == 1, str(len(found2)))

    print("\n[7] 只读保证: FETCH 用 BODY.PEEK[]")
    import io
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mail163.py"), encoding="utf-8").read()
    check("默认走 BODY.PEEK", "BODY.PEEK[]" in src)
    check("mark-read 需显式开启", "--mark-read" in src and "mark_read=args.mark_read" in src)

    print("\n[8] 凭据缺失时给出可操作报错")
    try:
        mail163.load_credentials(cred_file="__nonexistent_test_credentials__.env", user=None, authcode=None)
        os.environ.pop("MAIL163_USER", None)
        os.environ.pop("MAIL163_AUTHCODE", None)
        mail163.load_credentials(cred_file="__nonexistent_test_credentials__.env")
        check("应抛 CredentialError", False, "没有抛异常")
    except mail163.CredentialError as e:
        check("抛 CredentialError 且提示授权密码", "客户端授权密码" in str(e))

    try:
        M.logout()
    except Exception:
        pass

    print("\n[9] 命令行层(用户实际敲的那几条)")
    import contextlib
    import io as _io

    def run_cli(argv):
        buf, err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = mail163.main(argv)
        return code, buf.getvalue(), err.getvalue()

    os.environ["MAIL163_USER"] = "u@163.com"
    os.environ["MAIL163_AUTHCODE"] = "p"

    rc, out, _e = run_cli(["list", "--limit", "5", "--json"])
    check("list --json 退出码 0", rc == 0, str(rc))
    try:
        parsed = json.loads(out)
        check("list --json 是合法 JSON 且 5 条", len(parsed) == 5, str(len(parsed)))
        check("JSON 里中文主题未转义", "关于" in out or "Plain ASCII" in out)
    except Exception as exc:
        check("list --json 可解析", False, "%s: %r" % (exc, out[:120]))

    rc, out, _e = run_cli(["unread", "--limit", "3"])
    check("unread 退出码 0", rc == 0, str(rc))
    check("unread 输出含 uid", "uid=" in out, out[:80])

    rc, out, _e = run_cli(["read", "1"])
    check("read 退出码 0", rc == 0, str(rc))
    check("read 打印了主题与正文", "关于示例模型的回复" in out and "Example model" in out)

    rc, out, _e = run_cli(["read", "4", "--json"])
    obj = json.loads(out)
    check("read --json 带附件信息", len(obj["attachments"]) == 1, str(obj["attachments"]))

    rc, out, _e = run_cli(["search", "--from", "example.org", "--json"])
    check("search --json 命中 1 封", len(json.loads(out)) == 1, out[:120])

    print("\n" + "=" * 50)
    print("PASS=%d  FAIL=%d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
