import re

def terms(text):
    parts = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*|[\u3400-\u9fff]+", text.lower())
    tokens = []
    for part in parts:
        if re.fullmatch(r"[\u3400-\u9fff]+", part):
            tokens.extend(part[n:n+2] for n in range(max(1, len(part)-1)))
        else: tokens.append(part)
    return list(dict.fromkeys(tokens))
