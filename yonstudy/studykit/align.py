"""TF-IDF와 단조 DTW로 슬라이드 순서와 음성 전사를 맞춘다."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

# VTT/SRT 파싱


@dataclass
class Cue:
    start: float
    end: float
    text: str


_TS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})")


def _sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_vtt(content: str) -> list[Cue]:
    """WEBVTT / SRT 둘 다 읽는다. LearnUs 자동자막은 WEBVTT로 내려온다."""
    cues: list[Cue] = []
    block: list[str] = []
    span: tuple[float, float] | None = None
    for line in content.splitlines():
        m = _TS.search(line)
        if m:
            if span and block:
                cues.append(Cue(span[0], span[1], " ".join(block).strip()))
            span = (_sec(*m.group(1, 2, 3, 4)), _sec(*m.group(5, 6, 7, 8)))
            block = []
        elif span is not None:
            t = line.strip()
            if t and not t.isdigit() and not t.startswith("WEBVTT"):
                block.append(t)
    if span and block:
        cues.append(Cue(span[0], span[1], " ".join(block).strip()))
    return cues


def chunk_cues(cues: list[Cue], window_sec: float = 45.0) -> list[Cue]:
    """짧은 자막 줄을 window_sec 단위로 묶어 의미 있는 비교 단위로 만든다."""
    out: list[Cue] = []
    buf: list[str] = []
    start = None
    for c in cues:
        if start is None:
            start = c.start
        buf.append(c.text)
        if c.end - start >= window_sec:
            out.append(Cue(start, c.end, " ".join(buf)))
            buf, start = [], None
    if buf and start is not None:
        out.append(Cue(start, cues[-1].end, " ".join(buf)))
    return out


# 의존성 없는 TF-IDF

_WORD = re.compile(r"[A-Za-z]+|[가-힣]{2,}|\d+")
_STOP = {
    "the", "and", "for", "that", "this", "with", "are", "you", "your", "have", "has",
    "was", "were", "not", "but", "can", "will", "from", "they", "there", "here",
    "what", "when", "which", "into", "about", "just", "like", "than", "then", "some",
    "so", "we", "is", "it", "of", "to", "in", "on", "a", "an", "be", "as", "at", "by",
    "or", "if", "do", "does", "our", "its", "his", "her", "them", "these", "those",
    "그리고", "그래서", "하지만", "이것", "저것", "그것", "때문", "합니다", "입니다",
    "있습니다", "됩니다", "해서", "하는", "있는", "되는", "같은", "위해", "대해",
}


def tokenize(s: str) -> list[str]:
    return [w for w in (t.lower() for t in _WORD.findall(s)) if w not in _STOP and len(w) > 1]


class TfIdf:
    def __init__(self, docs: list[str]):
        self.tf = [Counter(tokenize(d)) for d in docs]
        df: Counter = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = max(len(docs), 1)
        self.idf = {w: math.log((n + 1) / (v + 1)) + 1 for w, v in df.items()}
        self.vecs = [self._vec(c) for c in self.tf]

    def _vec(self, counts: Counter) -> dict[str, float]:
        v = {w: (1 + math.log(c)) * self.idf.get(w, 1.0) for w, c in counts.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {w: x / norm for w, x in v.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(w, 0.0) for w, x in a.items())


# 단조 DTW 정렬


@dataclass
class SlideSpan:
    slide_idx: int  # 0-based
    start: float
    end: float
    score: float
    chunk_indices: list[int] = field(default_factory=list)


def align_slides(
    slide_texts: list[str], chunks: list[Cue], skip_penalty: float = 0.12
) -> list[SlideSpan]:
    """슬라이드 i와 전사 청크 j를 단조 증가 경로로 맞춘다.

    각 청크는 정확히 하나의 슬라이드에 배정되고, 슬라이드 순서는 뒤집히지 않는다.
    """
    if not slide_texts or not chunks:
        return []

    corpus = TfIdf(slide_texts + [c.text for c in chunks])
    sv = corpus.vecs[: len(slide_texts)]
    cv = corpus.vecs[len(slide_texts):]

    n, m = len(slide_texts), len(chunks)
    sim = [[cosine(sv[i], cv[j]) for j in range(m)] for i in range(n)]

    NEG = float("-inf")
    dp = [[NEG] * m for _ in range(n)]
    back = [[0] * m for _ in range(n)]  # 0=같은 슬라이드 유지, 1=다음 슬라이드로
    dp[0][0] = sim[0][0]
    for j in range(1, m):
        dp[0][j] = dp[0][j - 1] + sim[0][j]
    for i in range(1, n):
        for j in range(i, m):
            stay = dp[i][j - 1] if j > 0 and dp[i][j - 1] > NEG else NEG
            adv = dp[i - 1][j - 1] - skip_penalty if dp[i - 1][j - 1] > NEG else NEG
            if adv >= stay:
                dp[i][j], back[i][j] = adv + sim[i][j], 1
            else:
                dp[i][j], back[i][j] = stay + sim[i][j], 0

    # 역추적
    assign = [0] * m
    i, j = n - 1, m - 1
    while dp[i][j] == NEG and i > 0:
        i -= 1
    while j >= 0:
        assign[j] = i
        if back[i][j] == 1 and i > 0:
            i -= 1
        j -= 1

    spans: dict[int, SlideSpan] = {}
    for j, i in enumerate(assign):
        sp = spans.get(i)
        if sp is None:
            spans[i] = SlideSpan(i, chunks[j].start, chunks[j].end, sim[i][j], [j])
        else:
            sp.end = chunks[j].end
            sp.score = max(sp.score, sim[i][j])
            sp.chunk_indices.append(j)
    return [spans[k] for k in sorted(spans)]


# 슬라이드 간 변화분


_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")


def script_of(s: str) -> str:
    """대략의 문자 체계. 한국어 음성 + 영어 슬라이드 조합을 감지하는 데 쓴다."""
    ko, en = len(_HANGUL.findall(s)), len(_LATIN.findall(s))
    if ko > en * 2:
        return "ko"
    if en > ko * 2:
        return "en"
    return "mixed"


def neutral_tokens(s: str) -> set[str]:
    """언어가 달라도 공유되는 라틴 약어, 전문 용어, 숫자 토큰을 찾는다."""
    return {
        w.lower()
        for w in re.findall(r"[A-Za-z][A-Za-z0-9]{1,}|\d{2,}", s)
        if w.lower() not in _STOP
    }


def match_score(slide_text: str, transcript_text: str) -> float:
    """강의안과 전사의 내용 일치도를 0~1 사이로 점수화한다.

    같은 언어면 TF-IDF 코사인, 다른 언어면 라틴 약어·숫자 교집합 비율을 쓴다.
    """
    if script_of(slide_text) == script_of(transcript_text) != "mixed":
        v = TfIdf([slide_text, transcript_text]).vecs
        return cosine(v[0], v[1])
    a, b = neutral_tokens(slide_text), neutral_tokens(transcript_text)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def pick_slides(candidates: list, transcript_text: str) -> tuple[object, str, float] | None:
    """후보 강의안 중 전사와 가장 잘 맞는 것을 골라 경로, 이름, 점수를 반환한다."""
    best = None
    for path, name in candidates:
        try:
            from .slides import extract_pages

            text_all = "\n".join(extract_pages(path))
        except Exception:
            continue
        score = match_score(text_all, transcript_text)
        if best is None or score > best[2]:
            best = (path, name, score)
    return best


def extract_hotwords(slide_text: str, extra: str = "", limit: int = 60) -> str:
    """강의안에서 ASR에 넘길 약어와 반복 용어를 고른다.

    영문 약어는 강의안의 대소문자 표기를 유지하고, 한글은 두 번 이상 나온
    2~6자 용어를 우선한다.
    """
    counts: Counter = Counter()
    case_of: dict[str, str] = {}

    for w in re.findall(r"[A-Za-z][A-Za-z0-9+#.]{1,}", slide_text):
        key = w.lower()
        if key in _STOP or len(w) < 2:
            continue
        counts[key] += 1
        # 강의안이 쓰는 표기를 유지한다(vba가 아니라 VBA).
        if key not in case_of or w.isupper():
            case_of[key] = w

    for w in re.findall(r"[가-힣]{2,6}", slide_text):
        if w in _STOP:
            continue
        counts[w] += 1
        case_of.setdefault(w, w)

    first_line = slide_text.strip().splitlines()[0] if slide_text.strip() else ""
    title_terms = {
        t for t in re.findall(r"[A-Za-z]{2,}|[가-힣]{2,6}", first_line) if t not in _STOP
    }

    picked = [
        case_of[w]
        for w, c in counts.most_common(limit * 3)
        if c >= 2 or case_of[w] in title_terms
    ]
    if extra:
        picked = [x.strip() for x in extra.split(",") if x.strip()] + picked

    seen, out = set(), []
    for w in picked:
        if w.lower() in seen:
            continue
        seen.add(w.lower())
        out.append(w)
        if len(out) >= limit:
            break
    return ", ".join(out)


def slide_deltas(slide_texts: list[str]) -> list[dict]:
    """연속한 슬라이드 사이에 새로 등장하거나 사라진 용어를 찾는다.

    강의 슬라이드는 앞 장을 조금씩 덧붙이며 진행하는 경우가 많아, 이 '증분'이
    그 장의 실제 주제다. 전사와 비교할 때 이 증분만 보면 신호 대 잡음비가 훨씬 좋다.
    """
    out = []
    prev: set[str] = set()
    for i, t in enumerate(slide_texts):
        cur = set(tokenize(t))
        out.append(
            {
                "slide": i,
                "added": sorted(cur - prev),
                "removed": sorted(prev - cur),
                "kept": len(cur & prev),
            }
        )
        prev = cur
    return out


def spoken_only(delta_added: list[str], spoken: str, slide_all: str) -> list[str]:
    """전사에는 있지만 슬라이드에는 없는 용어를 말로만 한 설명의 후보로 반환한다."""
    slide_vocab = set(tokenize(slide_all))
    spoken_terms = Counter(tokenize(spoken))
    return [
        w for w, c in spoken_terms.most_common(40)
        if w not in slide_vocab and c >= 2 and len(w) > 2
    ][:15]
