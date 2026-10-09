"""메일 작성 링크와 읽기 전용 IMAP으로 개인 과제 알림 설정을 받는다."""

from __future__ import annotations

import hashlib
import imaplib
import json
import os
import re
import secrets
import ssl
import time
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from urllib.parse import quote, urlencode

from .assignment_state import is_submission_required
from .store import _now


SUBJECT_MARKER = "[yonstudy-action]"
TOKEN_PATTERN = re.compile(r"^요청 코드:\s*([0-9a-f]{48})\s*$", re.MULTILINE)
MAX_MESSAGE_BYTES = 64 * 1024
TOKEN_LIFETIME = 90 * 24 * 60 * 60


@dataclass(frozen=True)
class MailActionConfig:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    mailbox: str


def mail_action_config() -> MailActionConfig | None:
    if os.environ.get("YONSTUDY_MAIL_ACTIONS", "").lower() not in {"1", "true", "yes"}:
        return None
    host = os.environ.get("YONSTUDY_IMAP_HOST")
    if not host and os.environ.get("YONSTUDY_SMTP_HOST") == "smtp.gmail.com":
        host = "imap.gmail.com"
    username = os.environ.get("YONSTUDY_IMAP_USER") or os.environ.get("YONSTUDY_SMTP_USER")
    password = os.environ.get("YONSTUDY_IMAP_PASSWORD") or os.environ.get("YONSTUDY_SMTP_PASSWORD")
    if not (host and username and password):
        raise ValueError("메일 알림 설정을 받으려면 IMAP 호스트·계정·비밀번호가 필요합니다")
    return MailActionConfig(
        host, int(os.environ.get("YONSTUDY_IMAP_PORT", "993")), username, password,
        os.environ.get("YONSTUDY_IMAP_MAILBOX", "INBOX"),
    )


def action_label(row: dict) -> str:
    return "알림 제외" if is_submission_required(row) else "알림 다시 받기"


def prepare_assignment_actions(store, rows: list[dict], *, recipient: str) -> dict[int, str]:
    """메일 발송 직전에 링크를 만든다. 표시만으로는 설정이 바뀌지 않는다."""
    config = mail_action_config()
    if config is None:
        return {}
    addresses = getaddresses([recipient])
    if len(addresses) != 1 or not addresses[0][1]:
        raise ValueError("과제 알림 설정 링크는 수신자 한 명에게만 발급할 수 있습니다")
    address = addresses[0][1].lower()
    now = int(time.time())
    links = {}
    with store.db:
        store.db.execute("DELETE FROM assignment_mail_action WHERE expires_at<?", (now,))
        for row in rows:
            cmid = int(row["cmid"])
            if cmid in links or (is_submission_required(row) and row.get("submitted") == 1):
                continue
            token = secrets.token_hex(24)
            requirement = "not_required" if is_submission_required(row) else "auto"
            store.db.execute(
                "INSERT INTO assignment_mail_action "
                "(token_hash,cmid,requirement,recipient,expires_at) VALUES (?,?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), cmid, requirement, address,
                 now + TOKEN_LIFETIME),
            )
            body = (
                f"{action_label(row)} 요청\n"
                f"과목: {row.get('course_name') or ''}\n"
                f"과제: {row.get('title') or ''}\n\n"
                f"요청 코드: {token}\n"
            )
            links[cmid] = "mailto:" + quote(config.username, safe="@") + "?" + urlencode({
                "subject": f"{SUBJECT_MARKER} {action_label(row)}", "body": body.replace("\n", "\r\n"),
            }, quote_via=quote)
    return links


def apply_action_message(store, raw: bytes, *, now: int | None = None, dry_run: bool = False) -> dict | None:
    """본인 발신·일회용 코드가 모두 일치하는 요청 한 건만 처리한다."""
    if len(raw) > MAX_MESSAGE_BYTES:
        return None
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if SUBJECT_MARKER not in str(message.get("Subject", "")).lower():
        return None
    if str(message.get("Auto-Submitted", "no")).lower() != "no":
        return None
    senders = getaddresses(message.get_all("From", []))
    if len(senders) != 1:
        return None
    part = message.get_body(preferencelist=("plain",))
    if part is None:
        return None
    try:
        tokens = TOKEN_PATTERN.findall(part.get_content())
    except (LookupError, UnicodeError):
        return None
    if len(tokens) != 1:
        return None
    digest = hashlib.sha256(tokens[0].encode()).hexdigest()
    row = store.db.execute(
        "SELECT * FROM assignment_mail_action WHERE token_hash=?", (digest,),
    ).fetchone()
    now = int(time.time()) if now is None else now
    if (row is None or row["used_at"] or row["expires_at"] < now
            or senders[0][1].lower() != row["recipient"]):
        return None
    result = {"cmid": row["cmid"], "requirement": row["requirement"]}
    if not dry_run:
        # 설정 변경과 코드 사용 처리를 한 트랜잭션에 묶는다.
        with store.db:
            claimed = store.db.execute(
                "UPDATE assignment_mail_action SET used_at=? WHERE token_hash=? AND used_at IS NULL",
                (_now(), digest),
            )
            if not claimed.rowcount:
                return None
            store.set_assignment_requirement(
                row["cmid"], row["requirement"],
                "본인이 메일로 알림 제외 요청" if row["requirement"] == "not_required" else "",
            )
    return result


def _ok(response, action: str):
    status, data = response
    if status != "OK":
        raise RuntimeError(f"IMAP {action} 실패")
    return data


def process_assignment_mail(store, *, dry_run: bool = False, limit: int = 100) -> dict:
    """설정 요청 제목만 검색한다. 읽음 표시·메일 삭제·메일 발송을 하지 않는다."""
    import fcntl

    with (store.root / "assignment_mail.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _process_assignment_mail(store, dry_run=dry_run, limit=limit)


def _process_assignment_mail(store, *, dry_run: bool, limit: int) -> dict:
    config = mail_action_config()
    if config is None:
        return {"status": "disabled", "applied": []}
    try:
        state = json.loads((store.root / "assignment_mail_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    scope = hashlib.sha256(
        f"{config.host}:{config.username}:{config.mailbox}".encode(),
    ).hexdigest()
    applied = []
    ignored = 0
    with imaplib.IMAP4_SSL(config.host, config.port, ssl_context=ssl.create_default_context(), timeout=30) as client:
        _ok(client.login(config.username, config.password), "login")
        _ok(client.select(config.mailbox, readonly=True), "select")
        validity = client.response("UIDVALIDITY")[1]
        if not validity or not validity[0]:
            raise RuntimeError("IMAP UIDVALIDITY를 확인하지 못했습니다")
        uidvalidity = validity[0].decode("ascii")
        last_uid = int(state.get("last_uid", 0)) if (
            state.get("scope") == scope and state.get("uidvalidity") == uidvalidity
        ) else 0
        data = _ok(client.uid("search", None, "UID", f"{last_uid + 1}:*",
                              "HEADER", "Subject", f'"{SUBJECT_MARKER}"'), "search")
        uids = sorted(int(uid) for uid in (data[0] or b"").split() if int(uid) > last_uid)
        state = {"scope": scope, "uidvalidity": uidvalidity, "last_uid": last_uid}
        for uid in uids[:limit]:
            metadata = _ok(client.uid("fetch", str(uid), "(RFC822.SIZE)"), "fetch size")
            size_match = re.search(rb"RFC822.SIZE\s+(\d+)", b" ".join(
                value for value in metadata if isinstance(value, bytes)
            ))
            if size_match is None:
                raise RuntimeError("설정 요청 메일의 크기를 확인하지 못했습니다")
            result = None
            if int(size_match[1]) <= MAX_MESSAGE_BYTES:
                parts = _ok(client.uid("fetch", str(uid), "(BODY.PEEK[])"), "fetch body")
                raw = [part[1] for part in parts if isinstance(part, tuple)]
                if len(raw) != 1:
                    raise RuntimeError("설정 요청 메일 본문을 확인하지 못했습니다")
                result = apply_action_message(store, raw[0], dry_run=dry_run)
            if result:
                applied.append(result)
            else:
                ignored += 1
            state.update(last_uid=uid, checked_at=_now())
            if not dry_run:
                store.write_json("assignment_mail_state.json", state)
        if not dry_run:
            state["checked_at"] = _now()
            store.write_json("assignment_mail_state.json", state)
    return {"status": "dry_run" if dry_run else "ok", "applied": applied,
            "ignored": ignored, "remaining": max(0, len(uids) - limit)}
