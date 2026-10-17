"""Нарізка документів на фрагменти й відбір найрелевантніших до запитання."""
import re
from collections import defaultdict


def stems(text: str) -> set[str]:
    # Груба "основа": перші 5 літер, щоб "внесок/внески/внеску" збігалися.
    return {w[:5] for w in re.findall(r"\w+", text.lower()) if len(w) >= 3}


def chunk_text(text: str, size: int = 900) -> list[str]:
    pieces = []
    for para in (p.strip() for p in text.split("\n")):
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
            continue
        for sent in re.split(r"(?<=[.!?])\s+", para):
            while len(sent) > size:
                pieces.append(sent[:size])
                sent = sent[size:]
            if sent:
                pieces.append(sent)
    chunks, cur, last = [], "", ""
    for p in pieces:
        if cur and len(cur) + len(p) + 1 > size:
            chunks.append(cur)
            # Перекриття: новий фрагмент починається з останнього шматка попереднього.
            cur = (last + "\n" + p) if len(last) + len(p) + 1 <= size else p
        else:
            cur = (cur + "\n" + p) if cur else p
        last = p
    if cur:
        chunks.append(cur)
    return chunks


def build_context(question: str, docs, budget: int, exclude: str = ""):
    """Повертає [(doc, [фрагменти в порядку документа])] в межах budget символів.

    Фрагменти ранжуються за кількістю спільних основ слів із запитанням
    (назва місця не враховується: вона однакова для всіх документів рівня).
    Нульові збіги теж беруться, поки є місце: так не губляться синоніми.
    """
    q = stems(question) - stems(exclude)
    cands = []
    for di, d in enumerate(docs):
        for ci, ch in enumerate(chunk_text(d["text"])):
            cands.append((len(q & stems(ch)), di, ci, ch))
    cands.sort(key=lambda c: (-c[0], c[1], c[2]))
    used, picked = 0, defaultdict(list)
    for _, di, ci, ch in cands:
        if used + len(ch) > budget:
            continue
        picked[di].append((ci, ch))
        used += len(ch)
    return [(docs[di], [c for _, c in sorted(v)]) for di, v in sorted(picked.items())]
