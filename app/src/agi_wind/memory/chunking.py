from dataclasses import dataclass

@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    text: str

def estimate_tokens(text):
    # Conservative byte upper bound until a configured tokenizer is supplied.
    return len(text.encode("utf-8"))

def chunk_text(text, token_count=estimate_tokens, max_tokens=480, overlap=40):
    if max_tokens < 4 or overlap < 0: raise ValueError("invalid chunk settings")
    result, start = [], 0
    while start < len(text):
        low, high = start + 1, min(len(text), start + max_tokens)
        end = start
        while low <= high:
            middle = (low + high) // 2
            if token_count(text[start:middle]) <= max_tokens:
                end, low = middle, middle + 1
            else: high = middle - 1
        if end <= start: raise ValueError("one character exceeds tokenizer limit")
        result.append(Chunk(start, end, text[start:end]))
        if end == len(text): break
        start = max(start + 1, end - overlap)
    return result
