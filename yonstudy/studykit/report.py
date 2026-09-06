"""한 강의의 슬라이드와 전사를 맞춰 마크다운 노트를 만든다."""

from __future__ import annotations

from pathlib import Path

from . import align as A
from . import slides as S


def _mmss(sec: float) -> str:
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def analyze_lecture(store, cmid: int, slides: str | None = None, window: float = 45.0) -> int:
    meta = store.query(
        """
        SELECT a.title, a.section_name, c.name AS course, c.year, c.semester, c.slug
          FROM activity a JOIN course c ON c.course_id=a.course_id WHERE a.cmid=?
        """,
        (cmid,),
    )
    if not meta:
        print(f"cmid={cmid} 활동을 아카이브에서 찾지 못했습니다.")
        return 1
    m = meta[0]

    tr = store.query(
        "SELECT path, source, lang FROM transcript WHERE cmid=? ORDER BY "
        "CASE source WHEN 'whisper' THEN 0 WHEN 'vibevoice' THEN 1 ELSE 2 END",
        (cmid,),
    )
    if not tr:
        print(f"cmid={cmid} 의 자막/전사가 없습니다. 먼저 archive를 실행하세요.")
        return 1
    vtt_path = store.root / tr[0]["path"]
    if not vtt_path.exists():
        print(f"자막 파일이 없습니다: {vtt_path}")
        return 1

    cues = A.parse_vtt(vtt_path.read_text(encoding="utf-8", errors="ignore"))
    chunks = A.chunk_cues(cues, window)
    print(f"\n=== {m['course']} / {m['title']} ===")
    print(f"  전사: {len(cues)}줄 → {len(chunks)}청크 ({tr[0]['source']}/{tr[0]['lang']})")

    transcript_text = " ".join(c.text for c in cues)

    if slides:
        pdf, pdf_name, pick_score = Path(slides), Path(slides).name, None
    else:
        # 제목 규칙이 일정하지 않아 후보의 본문을 전사 내용과 대조한다.
        best = A.pick_slides(S.slide_candidates(store, cmid), transcript_text)
        if not best:
            print("  강의안 PDF 후보가 없습니다. 전사만으로 요약합니다.")
            _transcript_only(chunks)
            return 0
        pdf, pdf_name, pick_score = best
        print(f"  강의안 선택: {pdf_name} (내용 일치도 {pick_score:.2f})")
        if pick_score < 0.08:
            print("  ! 일치도가 너무 낮습니다 — 이 강의의 강의안이 아카이브에 없을 수 있습니다.")
            print("    --slides 로 직접 지정하거나, 전사만으로 요약합니다.")
            _transcript_only(chunks)
            return 0
    if not pdf or not Path(pdf).exists():
        print("  강의안 PDF를 찾지 못했습니다. 전사만으로 요약합니다.")
        _transcript_only(chunks)
        return 0

    pages = [p for p in S.extract_pages(pdf)]
    nonempty = [i for i, p in enumerate(pages) if len(A.tokenize(p)) >= 3]
    print(f"  강의안: {pdf_name} — {len(pages)}쪽 (텍스트 있는 쪽 {len(nonempty)})")
    if not nonempty:
        print("  PDF에서 텍스트를 뽑지 못했습니다(이미지 슬라이드일 수 있음). 전사만 사용합니다.")
        _transcript_only(chunks)
        return 0

    texts = [pages[i] for i in nonempty]

    # 음성과 슬라이드의 문자 체계가 다르면 텍스트 정렬 결과를 사용하지 않는다.
    s_script, t_script = A.script_of("\n".join(texts)), A.script_of(transcript_text)
    if s_script != t_script and "mixed" not in (s_script, t_script):
        print(f"  ! 언어 불일치: 슬라이드={s_script}, 전사={t_script}")
        print("    교차언어 텍스트 정렬은 신뢰할 수 없어 타임라인을 생성하지 않습니다.")
        print("    (해결책: 영상 프레임의 슬라이드 전환 감지 — 언어와 무관하게 동작)")
        _transcript_only(chunks)
        return 0

    spans = A.align_slides(texts, chunks)
    deltas = A.slide_deltas(texts)
    slide_all = "\n".join(texts)

    lines = [
        f"# {m['course']} — {m['title']}",
        "",
        f"- 학기: {m['year']} {m['semester']}  ·  주차: {m['section_name']}",
        f"- 강의안: `{pdf_name}` ({len(pages)}쪽)",
        f"- 전사: {len(cues)}줄 / {cues[-1].end/60:.0f}분" if cues else "",
        "",
        "## 슬라이드별 타임라인",
        "",
    ]
    print("\n  슬라이드     구간              일치도  새 용어")
    for sp in spans:
        page_no = nonempty[sp.slide_idx] + 1
        d = deltas[sp.slide_idx]
        spoken = " ".join(chunks[j].text for j in sp.chunk_indices)
        only = A.spoken_only(d["added"], spoken, slide_all)
        added = ", ".join(d["added"][:6])
        print(
            f"  p.{page_no:<3} {_mmss(sp.start)}~{_mmss(sp.end)}  {sp.score:5.2f}  {added[:46]}"
        )
        lines += [
            f"### p.{page_no} · {_mmss(sp.start)} ~ {_mmss(sp.end)}",
            "",
            f"- 일치도 `{sp.score:.2f}`",
            f"- 슬라이드가 새로 꺼낸 것: {added or '—'}",
            f"- 💬 말로만 나온 것: {', '.join(only) or '—'}",
            "",
            "> " + (spoken[:600].replace("\n", " ") or "—"),
            "",
        ]

    out = store.root / "courses" / f"{m['year']}-{m['semester']}" / m["slug"] / "notes"
    out.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in m["title"] if ch.isalnum() or ch in " -_")[:60].strip()
    path = out / f"{safe}.md"
    path.write_text("\n".join(x for x in lines if x is not None), encoding="utf-8")
    print(f"\n  노트 작성: {path}")

    weak = [sp for sp in spans if sp.score < 0.05]
    if weak:
        print(f"  ! 일치도 낮은 슬라이드 {len(weak)}개 — 이미지 위주이거나 순서가 다를 수 있습니다.")
    return 0


def _transcript_only(chunks: list[A.Cue]) -> None:
    corpus = A.TfIdf([c.text for c in chunks])
    print("\n  구간별 핵심어:")
    for c, vec in zip(chunks[:20], corpus.vecs):
        top = sorted(vec.items(), key=lambda x: -x[1])[:6]
        print(f"    {_mmss(c.start)}~{_mmss(c.end)}  {', '.join(w for w, _ in top)}")
