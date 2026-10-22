import re
from .models import Turn

LABEL = re.compile(r"^\s*(销售|客户|SDR|顾客|销售人员|客服|用户|其他|存疑|待确认)\s*[:：]\s*(.*)$", re.I)

_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 18-digit resident ID: plausible birthdate plus a checksummed final digit.
_ID_CARD = re.compile(r"(?<![0-9Xx])\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9Xx])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# 13-19 digits, optionally spaced/dashed as printed on cards; Luhn-checked before redaction.
_CARD_NUMBER = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_DIGITS = "10X98765432"


def _id_checksum_ok(value):
    if len(value) != 18:
        return False
    try:
        total = sum(int(value[i]) * _ID_WEIGHTS[i] for i in range(17))
    except ValueError:
        return False
    return _ID_CHECK_DIGITS[total % 11] == value[17].upper()


def _luhn_ok(digits):
    total = 0
    for i, digit in enumerate(reversed(digits)):
        number = int(digit)
        if i % 2:
            number = number * 2 - 9 if number > 4 else number * 2
        total += number
    return total % 10 == 0


def split_turns(text: str) -> list[Turn]:
    """Preserve utterances, repetitions and speakers; never rewrite customer text."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = LABEL.match(line)
        if match:
            speaker = {"其他":"other", "存疑":"excluded", "待确认":"pending"}.get(match[1]) or ("customer" if match[1] in ("客户", "顾客", "用户") else "sales")
            rows.append((speaker, match[2].strip()))
        elif rows:
            speaker, old = rows[-1]
            rows[-1] = (speaker, old + "\n" + line.strip())
        else:
            rows.append(("unknown", line.strip()))
    return [Turn(id=f"T{i:02}", speaker=s, text=t) for i, (s, t) in enumerate(rows, 1)]


def redact_for_model(turns: list[Turn]):
    """Redact phones, resident IDs, bank cards and emails, preserving reversible evidence mapping.

    Resident IDs require a valid checksum; card numbers require a valid Luhn pass, so
    business figures like 门店数 or 设计部人数 are never rewritten as PII.
    """
    substitutions = {}
    safe = []
    for turn in turns:
        text = turn.text
        # ID cards first: an 18-digit ID must not be mistaken for a card number.
        for pattern, kind in ((_ID_CARD, "IDCARD"), (_PHONE, "PHONE"), (_CARD_NUMBER, "BANKCARD"), (_EMAIL, "EMAIL")):
            def _replace(m, pattern=pattern, kind=kind):
                raw = m[0]
                if kind == "IDCARD" and not _id_checksum_ok(raw):
                    return raw
                if kind == "BANKCARD" and not _luhn_ok(re.sub(r"[ -]", "", raw)):
                    return raw
                used = sum(1 for k in substitutions if k.startswith("[" + kind + "_"))
                key = f"[{kind}_{used + 1}]"
                substitutions[key] = raw
                return key
            text = pattern.sub(_replace, text)
        safe.append({**turn.model_dump(), "text": text})
    return safe, substitutions


def restore_placeholders(value, substitutions):
    if isinstance(value, str):
        for key, original in substitutions.items():
            value = value.replace(key, original)
        return value
    if isinstance(value, list):
        return [restore_placeholders(v, substitutions) for v in value]
    if isinstance(value, dict):
        return {k: restore_placeholders(v, substitutions) for k, v in value.items()}
    return value
