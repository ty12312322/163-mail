#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
163 邮箱 (imap.163.com) IMAP 读取器 —— 给 Codex / Agent 用的结构化取信工具。

为什么走 IMAP 而不是浏览器自动化:
  1. 163 网页版正文渲染在 iframe 里, DOM 抽取极不稳定;
  2. Agent 的浏览器工具常把邮箱/网银类站点判为高敏感站点并直接拒绝读取;
  3. IMAP 是官方协议, 稳定、可编程、可只读。

安全约束(硬编码, 不可通过命令行关闭):
  - 永远只做只读操作: 不删除、不移动、不标记已读(除非显式 --mark-read)
  - 默认不落盘正文, 只有 --save 才写入本地
  - 密码只从环境变量/凭据文件读取, 绝不回显、绝不写进日志或产物

用法:
  python mail163.py folders
  python mail163.py list --folder INBOX --limit 20
  python mail163.py search --from "advisor" --since 2026-10-01 --limit 10
  python mail163.py read <uid>            # 打印正文
  python mail163.py read <uid> --json     # 结构化输出
  python mail163.py read <uid> --save     # 正文存到 ./mail_out/
  python mail163.py unread --folder INBOX # 只看未读

凭据(优先级从高到低):
  1. 环境变量 MAIL163_USER / MAIL163_AUTHCODE
  2. --cred-file 指定的文件(格式: KEY=VALUE 每行)
  3. 默认 ~/.config/163-mail/credentials.env
"""

import argparse
import email
import email.header
import email.policy
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

IMAP_HOST = "imap.163.com"
IMAP_PORT = 993
DEFAULT_CRED_FILE = os.path.join(os.path.expanduser("~"), ".config", "163-mail", "credentials.env")

# 163 要求客户端在 LOGIN 之前自报身份(ID 命令)。
# Python 标准库 imaplib 的命令表里没有 ID, 且 ID 属于 NONAUTH 状态,
# 不注册就会抛 "command ID illegal in state NONAUTH"。
# 这两行是本工具能连上 163 的关键, 不要删。
import imaplib  # noqa: E402

imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH"))

# 只读操作白名单: 任何会改变服务器端状态的动作都必须显式开关
MUTATING_UID_COMMANDS = ("STORE", "EXPUNGE", "COPY", "APPEND", "MOVE")


# --------------------------------------------------------------------------
# 凭据
# --------------------------------------------------------------------------
class CredentialError(RuntimeError):
    pass


def load_credentials(cred_file=None, user=None, authcode=None):
    """读取 IMAP 凭据。授权密码(code)不是登录密码, 必须是 163 的「客户端授权密码」。"""
    env_user = os.environ.get("MAIL163_USER")
    env_code = os.environ.get("MAIL163_AUTHCODE")

    file_user = file_code = None
    path = cred_file or DEFAULT_CRED_FILE
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().upper()
                v = v.strip().strip('"').strip("'")
                if k == "MAIL163_USER":
                    file_user = v
                elif k == "MAIL163_AUTHCODE":
                    file_code = v

    final_user = user or env_user or file_user
    final_code = authcode or env_code or file_code

    if not final_user or not final_code:
        raise CredentialError(
            "缺少 IMAP 凭据。任选其一:\n"
            "  1) 设置环境变量 MAIL163_USER / MAIL163_AUTHCODE\n"
            "  2) 创建 %s, 内容两行:\n"
            "     MAIL163_USER=你的地址@163.com\n"
            "     MAIL163_AUTHCODE=客户端授权密码\n"
            "注意: MAIL163_AUTHCODE 填的是 163「设置 -> 常规设置 -> 客户端授权密码」"
            "里生成的授权码, 不是登录密码。" % path
        )
    return final_user, final_code


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------
def connect(user, code, timeout=25):
    """建立 IMAP4-SSL 连接, 返回 (imap, server_greeting)。"""
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED

    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx, timeout=timeout)

    # 163 特有: 先发 ID 再 LOGIN
    try:
        M._simple_command("ID", '("name" "mail163" "version" "1.0")')
    except Exception:
        # 老账号可能不强制 ID; 不阻断流程
        pass

    try:
        M.login(user, code)
    except imaplib.IMAP4.error as exc:
        msg = str(exc)
        if "authentication failed" in msg.lower() or "login error" in msg.lower():
            raise CredentialError(
                "IMAP 登录被拒(%s)。最常见原因:\n"
                "  1) 没在 163 设置里开启「IMAP/SMTP 服务」;\n"
                "  2) 用的是登录密码而不是「客户端授权密码」;\n"
                "  3) 授权码已过期需重新生成。" % msg
            ) from exc
        raise

    return M, M.welcome


# --------------------------------------------------------------------------
# 解码
# --------------------------------------------------------------------------
def decode_header_value(raw):
    """解码 MIME 头字段(Subject / From / To), 处理 RFC2047 编码。"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        # policy=default 下 msg[...] 返回 header 对象而非 str
        try:
            text = str(raw)
        except Exception:
            text = repr(raw)
    if not text:
        return ""
    try:
        parts = email.header.decode_header(text)
    except Exception:
        return text.strip()
    out = []
    for text_part, charset in parts:
        if isinstance(text_part, bytes):
            out.append(text_part.decode(charset or "utf-8", "replace"))
        else:
            out.append(text_part)
    return "".join(out).strip()


def extract_addr(raw):
    """从 From/To 头里抠出纯邮箱地址。

    不能依赖 email.utils.parseaddr —— 对 "Rigby, Frances E <a@b.com>" 这类
    显示名带逗号的写法会返回 ('', ''); 而 headerregistry 的结构化解析在这种情况下
    addr_spec 也可能只给出 'Rigby'。所以按"尖括号 > 邮箱正则 > 结构化解析"的顺序取。
    """
    if raw is None:
        return ""
    text = decode_header_value(raw)
    if text:
        m = re.search(r"<([^<>@\s]+@[^<>\s]+)>", text)
        if m:
            return m.group(1).strip()
        m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
        if m:
            return m.group(0)
    addrs = getattr(raw, "addresses", None)
    if addrs:
        for a in addrs:
            spec = getattr(a, "addr_spec", "") or ""
            if "@" in spec:
                return spec
    return ""


def _iter_fetch_results(fetch_data):
    """从 imaplib 的 FETCH 返回里稳定提取 (uid, bytes)。

    imaplib 对带 literal 的响应会把元信息和正文放在一个 tuple 里
    (b'1 (UID 5 BODY[] {123}', b'<payload>'), 并在其后插 b')' 碎片。
    直接 zip(uid列表, fetch_data) 会错位, 所以这里只认 tuple,
    并从元信息里抠 UID (UID FETCH 的响应一定回带 UID)。
    """
    for item in fetch_data or []:
        if not (isinstance(item, tuple) and len(item) >= 2):
            continue
        meta, payload = item[0], item[1]
        if not isinstance(payload, (bytes, bytearray)):
            continue
        uid = None
        if isinstance(meta, (bytes, bytearray)):
            m = re.search(rb"UID\s+(\d+)", meta)
            if m:
                uid = int(m.group(1))
        yield uid, bytes(payload)


def decode_payload(part):
    """解码单个 MIME part 的正文, 返回 str。"""
    payload = part.get_payload(decode=True)
    if payload is None:
        payload = part.get_payload()
        if isinstance(payload, str):
            return payload
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except (LookupError, UnicodeDecodeError):
        for fallback in ("utf-8", "gb18030", "latin-1"):
            try:
                return payload.decode(fallback)
            except (LookupError, UnicodeDecodeError):
                continue
    return payload.decode("utf-8", "replace")


def strip_html(text):
    """把 HTML 正文压成可读纯文本, 保留段落换行。"""
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_body_and_attachments(msg):
    """返回 (纯文本正文, 附件列表)。优先 text/plain, 没有则用 text/html 转换。"""
    plain_parts, html_parts, attachments = [], [], []

    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        ctype = part.get_content_type()

        if "attachment" in disposition or (part.get_filename() and "inline" not in disposition):
            fname = decode_header_value(part.get_filename()) or "unnamed"
            raw = part.get_payload(decode=True)
            size = len(raw) if isinstance(raw, (bytes, bytearray)) else 0
            attachments.append({"filename": fname, "content_type": ctype, "size": size})
            continue

        if ctype == "text/plain":
            plain_parts.append(decode_payload(part))
        elif ctype == "text/html":
            html_parts.append(strip_html(decode_payload(part)))

    body = "\n".join(p for p in plain_parts if p.strip())
    if not body.strip() and html_parts:
        body = "\n".join(html_parts)

    # 去掉转发/回复里重复的头部行。
    # 注意只匹配 "键: 值" 形式, 不能匹配任意包含"时间""主题"的句子,
    # 否则会静默吃掉正文里的正常句子(这是最隐蔽的一类错)。
    body = re.sub(
        r"(?mi)^\s*(发件人|寄件者|收件人|抄送|密送|主题|发送时间|日期|"
        r"from|sent|to|cc|bcc|subject|date)\s*[:：].*$",
        "", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, attachments


# --------------------------------------------------------------------------
# 邮件解析
# --------------------------------------------------------------------------
def parse_message(raw_bytes):
    # policy=default 更严格(会自动解码 RFC2047/2231), 但遇到畸形头可能抛异常;
    # 兜底退回 compat32, 保证"能读出来"优先于"读得漂亮"。
    try:
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        _ = msg["Subject"], msg["From"], msg["Date"]
    except Exception:
        msg = email.message_from_bytes(raw_bytes)

    body, attachments = extract_body_and_attachments(msg)

    dt = None
    try:
        date_hdr = msg["Date"]
        if date_hdr:
            dt = email.utils.parsedate_to_datetime(str(date_hdr))
            if dt and dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    except Exception:
        dt = None

    from_hdr = msg["From"]
    return {
        "subject": decode_header_value(msg["Subject"]),
        "from": decode_header_value(from_hdr),
        "from_addr": extract_addr(from_hdr),
        "to": decode_header_value(msg["To"]),
        "date": dt.isoformat() if dt else None,
        "message_id": decode_header_value(msg["Message-ID"]),
        "body": body,
        "attachments": attachments,
    }


def fetch_message(M, uid, folder="INBOX", mark_read=False):
    """按 UID 取一封邮件并解析。mark_read=False 时用 BODY.PEEK[], 不改服务器已读状态。"""
    typ, _ = M.select(folder, readonly=not mark_read)
    if typ != "OK":
        raise RuntimeError("无法打开文件夹 %s" % folder)

    part = "(BODY[])" if mark_read else "(BODY.PEEK[])"
    typ, data = M.uid("FETCH", str(uid), part)
    if typ != "OK" or not data:
        raise RuntimeError("FETCH uid=%s 失败: %s %s" % (uid, typ, data))

    raw = None
    for _u, payload in _iter_fetch_results(data):
        raw = payload
        break
    if raw is None:
        raise RuntimeError("FETCH uid=%s 没有返回原始邮件体" % uid)

    result = parse_message(raw)
    result["uid"] = int(uid)
    result["folder"] = folder
    return result


def _header_rows(fetch_data, fallback_uids):
    """把 FETCH 头部结果转成 [{uid, subject, from, ...}]。"""
    rows = []
    for i, (uid, hdr_bytes) in enumerate(_iter_fetch_results(fetch_data)):
        if uid is None:
            uid = fallback_uids[i] if i < len(fallback_uids) else -1
        hdr = email.message_from_bytes(hdr_bytes, policy=email.policy.default)
        dt = None
        try:
            if hdr["Date"]:
                dt = email.utils.parsedate_to_datetime(str(hdr["Date"]))
        except Exception:
            pass
        from_hdr = hdr["From"]
        rows.append({
            "uid": uid,
            "subject": decode_header_value(hdr["Subject"]),
            "from": decode_header_value(from_hdr),
            "from_addr": extract_addr(from_hdr),
            "date": dt.isoformat() if dt else None,
        })
    return rows


def list_headers(M, folder="INBOX", limit=20, unread_only=False):
    """列出邮件头部(不含正文), 快速且省流量。"""
    typ, _ = M.select(folder, readonly=True)
    if typ != "OK":
        raise RuntimeError("无法打开文件夹 %s" % folder)

    criteria = "UNSEEN" if unread_only else "ALL"
    typ, data = M.uid("SEARCH", None, criteria)
    if typ != "OK":
        return []

    uids = [int(u) for u in (data[0].split() if data and data[0] else [])]
    # 取最新 limit 封
    uids = uids[-limit:] if limit and limit > 0 else uids
    if not uids:
        return []

    seq = ",".join(str(u) for u in uids)
    typ, fetch_data = M.uid("FETCH", seq, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])")
    if typ != "OK" or not fetch_data:
        return []
    return _header_rows(fetch_data, uids)


def search_messages(M, sender=None, subject=None, since=None, keyword=None,
                    folder="INBOX", limit=20, unread_only=False):
    """按条件搜索。sender/subject/keyword 都做大小写不敏感的子串匹配(先粗筛再精筛)。"""
    typ, _ = M.select(folder, readonly=True)
    if typ != "OK":
        raise RuntimeError("无法打开文件夹 %s" % folder)

    criteria = []
    if unread_only:
        criteria += ["UNSEEN"]
    if since:
        since_dt = datetime.strptime(since, "%Y-%m-%d") - timedelta(days=1)
        criteria += ["SINCE", since_dt.strftime("%d-%b-%Y")]
    if sender:
        criteria += ["FROM", '"%s"' % sender.replace('"', "")]
    if subject:
        criteria += ["SUBJECT", '"%s"' % subject.replace('"', "")]
    if keyword:
        criteria += ["TEXT", '"%s"' % keyword.replace('"', "")]
    if not criteria:
        criteria = ["ALL"]

    typ, data = M.uid("SEARCH", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []

    uids = [int(u) for u in data[0].split()]
    uids = uids[-limit:] if limit and limit > 0 else uids
    if not uids:
        return []

    seq = ",".join(str(u) for u in uids)
    typ, fetch_data = M.uid("FETCH", seq, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
    if typ != "OK" or not fetch_data:
        return []

    out = []
    for row in _header_rows(fetch_data, uids):
        frm = row["from"]
        addr = row["from_addr"]
        subj = row["subject"]
        # 粗筛兜底: 服务器 SEARCH 对中文/编码邮箱可能漏命中, 这里再做一次子串匹配
        if sender and sender.lower() not in frm.lower() and sender.lower() not in addr.lower():
            continue
        if subject and subject.lower() not in subj.lower():
            continue
        out.append(row)
    return out


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def print_headers(rows):
    if not rows:
        print("(没有匹配的邮件)")
        return
    for r in rows:
        date = (r.get("date") or "")[:16].replace("T", " ")
        print("[uid=%s] %s  |  %s" % (r["uid"], date, r.get("subject", "")))
        print("    from: %s" % r.get("from", ""))


def print_message(msg, max_body=4000):
    print("=" * 70)
    print("uid      : %s" % msg["uid"])
    print("主题     : %s" % msg["subject"])
    print("发件人   : %s" % msg["from"])
    print("收件人   : %s" % msg["to"])
    print("时间     : %s" % msg["date"])
    if msg["attachments"]:
        print("附件     : " + ", ".join(
            "%s (%s, %dB)" % (a["filename"], a["content_type"], a["size"]) for a in msg["attachments"]))
    print("=" * 70)
    body = msg["body"]
    if len(body) > max_body:
        body = body[:max_body] + "\n...[正文截断, 总长 %d 字符, 用 --json 看全文]" % len(msg["body"])
    print(body)
    print("=" * 70)


def save_message(msg, outdir="mail_out"):
    os.makedirs(outdir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", msg["subject"])[:60] or "no_subject"
    path = os.path.join(outdir, "uid%s_%s.txt" % (msg["uid"], safe))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("uid: %s\n主题: %s\n发件人: %s\n收件人: %s\n时间: %s\n%s\n\n%s\n" % (
            msg["uid"], msg["subject"], msg["from"], msg["to"], msg["date"], "=" * 40, msg["body"]))
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="163 邮箱 IMAP 读取器(只读)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--cred-file", help="凭据文件路径(默认 %s)" % DEFAULT_CRED_FILE)
    p.add_argument("--user", help="邮箱地址(覆盖环境变量)")
    p.add_argument("--timeout", type=int, default=25, help="网络超时秒数(默认 25)")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("folders", help="列出所有邮箱文件夹")

    sp = sub.add_parser("list", help="列出最新邮件头部")
    sp.add_argument("--folder", default="INBOX")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--unread-only", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("unread", help="只看未读邮件")
    sp.add_argument("--folder", default="INBOX")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("search", help="条件搜索")
    sp.add_argument("--from", dest="sender")
    sp.add_argument("--subject")
    sp.add_argument("--keyword")
    sp.add_argument("--since", help="起始日期 YYYY-MM-DD")
    sp.add_argument("--folder", default="INBOX")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--unread-only", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("read", help="读取一封邮件正文")
    sp.add_argument("uid", type=int)
    sp.add_argument("--folder", default="INBOX")
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.add_argument("--save", action="store_true", help="正文写入 ./mail_out/")
    sp.add_argument("--mark-read", action="store_true", help="允许标记已读(默认只读不改状态)")
    sp.add_argument("--max-body", type=int, default=4000)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        user, code = load_credentials(args.cred_file, args.user, None)
        M, _greeting = connect(user, code, timeout=args.timeout)
    except CredentialError as exc:
        print("[凭据错误] %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:
        print("[连接失败] %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 3

    try:
        if args.cmd == "folders":
            typ, folders = M.list()
            if typ != "OK":
                print("列出文件夹失败: %s" % folders, file=sys.stderr)
                return 4
            for f in folders or []:
                print(f.decode("utf-8", "replace"))
            return 0

        if args.cmd in ("list", "unread"):
            unread_only = args.cmd == "unread" or args.unread_only
            rows = list_headers(M, args.folder, args.limit, unread_only)
            if getattr(args, "json", False):
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print_headers(rows)
                print("\n共 %d 封。取正文: python mail163.py read <uid>" % len(rows))
            return 0

        if args.cmd == "search":
            rows = search_messages(
                M, sender=args.sender, subject=args.subject, since=args.since,
                keyword=args.keyword, folder=args.folder, limit=args.limit,
                unread_only=args.unread_only)
            if getattr(args, "json", False):
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print_headers(rows)
                print("\n共 %d 封。取正文: python mail163.py read <uid>" % len(rows))
            return 0

        if args.cmd == "read":
            msg = fetch_message(M, args.uid, args.folder, mark_read=args.mark_read)
            if args.json:
                print(json.dumps(msg, ensure_ascii=False, indent=2))
            else:
                print_message(msg, args.max_body)
            if args.save:
                path = save_message(msg)
                print("[已保存] %s" % path)
            return 0

        print("未知命令", file=sys.stderr)
        return 1

    except Exception as exc:
        print("[执行失败] %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 4
    finally:
        try:
            M.close()
        except Exception:
            pass
        try:
            M.logout()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
