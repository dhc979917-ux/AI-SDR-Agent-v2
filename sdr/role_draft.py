import re

LABEL_LINE = re.compile(r"^\s*(销售|客户|待确认|其他|存疑)\s*[:：]\s*(.*)$")
FILLER = re.compile(r"^[\s，,。.!！?？；;：:、“”\"'‘’、…（）()]*(?:[嗯啊哦噢呃唔喂哎诶欸唉]|对{2,})+[\s，,。.!！?？；;：:、“”\"'‘’、…（）()]*$")


def has_labels(text: str) -> bool:
    return any(LABEL_LINE.match(line) for line in text.splitlines() if line.strip())


def _is_filler(text: str) -> bool:
    return bool(FILLER.match(text or ""))


def draft_pending_count(text: str) -> int:
    return sum(1 for line in (text or "").splitlines() if line.strip().startswith("待确认："))


def draft_has_customer(text: str) -> bool:
    return any(line.strip().startswith("客户：") and line.split("：", 1)[-1].strip()
               for line in (text or "").splitlines())


def auto_label_draft(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return ""
    lines = body.splitlines()
    if not has_labels(body):
        return "客户：" + body
    output = []
    for line in lines:
        match = LABEL_LINE.match(line)
        if not match:
            output.append(line)
            continue
        role, content = match.groups()
        if role == "待确认" and _is_filler(content):
            role = "其他"
        output.append(f"{role}：{content}")
    if not draft_has_customer("\n".join(output)):
        candidates = [index for index, line in enumerate(output)
                      if line.startswith(("待确认：", "其他：")) and line.split("：", 1)[-1].strip()]
        if candidates:
            index = max(candidates, key=lambda value: len(output[value]))
            output[index] = "客户：" + output[index].split("：", 1)[-1]
        else:
            candidates = [index for index, line in enumerate(output) if line.strip()]
            if candidates:
                index = max(candidates, key=lambda value: len(output[value]))
                output[index] = "客户：" + re.sub(r"^\s*(?:销售|其他|存疑|待确认)\s*[:：]\s*", "", output[index])
            else:
                output = ["客户：" + body]
    return "\n".join(output)


def label_unlabeled(text: str) -> str:
    body = (text or "").strip()
    return "客户：" + body if body else ""
