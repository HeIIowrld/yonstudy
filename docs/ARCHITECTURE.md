# 구조

## 모듈

```
yonstudy/
├── cli.py                  명령행 진입점
├── yonstudy/
│   ├── client.py            SSO 로그인, HTTP 요청, 속도 제한
│   ├── parse.py             LearnUs HTML 파서
│   ├── store.py             SQLite, sha256 blob, 스키마 마이그레이션
│   ├── archive.py           읽기 전용 증분 수집
│   ├── remote.py            rclone 원격 업로드
│   ├── export.py            로컬 폴더 내보내기
│   ├── daily.py             일일 리포트와 메일
│   ├── vod.py               HLS 산출물 추출(ffmpeg)
│   ├── progress.py          진도율·최대 학습 위치 공통 완료 판정
│   ├── autoplay.py          진도 스케줄러와 재생 워커(Playwright)
│   ├── recordings.py        시간표 가져오기, 녹음시각 판정과 과목 자동 분류
│   └── studykit/
│       ├── slides.py         PDF 슬라이드 추출, 강의안 후보 탐색
│       ├── align.py          TF-IDF·DTW 정렬, hotword 추출, 언어 감지
│       └── report.py         슬라이드별 타임라인과 노트 생성
└── store/                  아카이브 실체
```

아카이빙은 별도 Python 패키지 없이 돌 수 있게 두고, 무거운 의존성은 오디오·전사·재생
기능에만 격리했다.

## 데이터 흐름

```
LearnUs ──client──▶ archive ──parse──▶ store (SQLite + blobs)
                                          │
                        ┌─────────────────┼──────────────────┬──────────────┐
                        ▼                 ▼                  ▼              ▼
                    vod.py            autoplay.py        studykit/      remote.py
                 HLS 산출물 추출      진도 스케줄러      슬라이드 정렬   rclone 업로드
```

## 저장소 레이아웃

```
store/
├── db.sqlite                       메타데이터 (WAL 모드)
├── blobs/sha256/ab/cd/abcd…        파일 실체. 중복은 하나만 저장
└── courses/<연도>-<학기>/<과목코드>_<과목명>/
    ├── course.json                 활동 목록 스냅샷
    ├── materials/                  강의안·자료 (blob 하드링크)
    ├── submissions/<모듈>/          내가 제출한 파일
    ├── subtitles/                  자막 VTT
    ├── audio/                      추출한 오디오 (opus)
    ├── recordings/                 외부 수업 녹음 (원본 형식 유지)
    ├── boards/                     게시판 첨부
    ├── notes/                      생성한 학습 노트
    └── forum_posts.json            내가 쓴 포럼 글
```

같은 파일이 여러 강좌나 역할에 걸쳐 있어도 blob은 sha256 하나만 남고 강좌 폴더에는
하드링크만 건다.

## 데이터 모델

```sql
course      (course_id PK, year, semester, kind, title, name, code, section, slug,
             archived_at, enrolled, unenrolled_at, detail_synced_at)

activity    (cmid PK, course_id, modname, title, url,
             section_idx, section_name, indent, completion,
             open_from, open_to, late_until, duration, restricted, seen_at,
             present, removed_at)

vod         (cmid PK, course_id, uuid, hls_url, poster, subtitle_langs,
             duration_sec, watched_sec, progress_pct,
             can_log_progress,      -- 지금 재생하면 진도가 잡히는가
             max_rate,              -- 서버가 허용하는 최대 배속
             seek_restricted, trackid, attempt, checker_url,
             status,                -- ok / empty / no_access / error
             probed_at)

submission  (cmid PK, course_id, modname, title,
             status, grading_status, due_at, last_modified, grade,
             fields_json, submitted, seen_at, instructions, instructions_html)
             -- assign·turnitintooltwo·vpl·quiz·feedback·choice와 명세가 확인된
             -- Gradescope LTI 과제를 한 테이블로 모은다

post        (id PK, course_id, cmid, modname, post_id, thread_id,
             no, subject, writer, written_at, hits, replies, url, body,
             fetched_at, checked_at)
             -- ubboard(공지·Q&A)와 forum 글. UNIQUE(cmid, modname, post_id)

file        (id PK, course_id, cmid, role, name, url, sha256, bytes, saved_at)
             -- role: submission / introattachment / resource / subtitle / post
             -- UNIQUE(url, role) 이 증분 크롤의 기준

transcript  (id PK, cmid, source, lang, path, segments, created_at)
             -- source: learnus_auto / whisper / vibevoice …

timetable_slot (id PK, course_id, weekday, starts_at, ends_at,
                valid_from, valid_to, location, source, imported_at)
             -- weekday는 월=0 .. 일=6. 자정을 넘는 수업도 표현 가능

recording   (id PK, sha256 UNIQUE, original_name, source_path, captured_at,
             metadata_title, source_bytes, source_mtime_ns, timestamp_source,
             duration_sec, course_id, timetable_slot_id, week, lesson,
             match_status, match_method, confidence, path, details_json,
             imported_at, updated_at)
             -- match_status: matched / ambiguous / unclassified

crawl_log   (id PK, at, kind, ref, ok, note)
```

강좌 목록에서 사라진 강좌는 삭제하지 않고 `enrolled=0`, 정상적으로 읽은 강좌
페이지에서 사라진 활동은 `present=0`으로 보존한다. 따라서 과거 자료와 NAS 파일은
남지만 현재 리포트·대기열·자동재생 후보에서는 제외된다. HTML 일부만 파싱된 경우에는
이 상태 전환을 하지 않고 강좌 동기화 자체를 실패시킨다.

### 설계상 결정 세 가지

**1. 제출형 활동을 한 테이블로 모은다.**
`assign`만 있는 게 아니라 `turnitintooltwo`(표절검사), `vpl`(코딩), `quiz`, `feedback`,
`choice`가 전부 "내가 한 활동"이다. 모듈별 테이블을 만들면 "지난 학기에 내가 뭘 냈지"를
질의할 때마다 UNION을 써야 한다. 파싱만 모듈별로 갈리고 저장은 하나로 모은다.

**2. `forum`은 제출 테이블에 넣지 않는다.**
포럼은 "제출" 개념이 없어 전부 미제출로 잡힌다. 대신 강좌 단위로
`/mod/forum/user.php?id={userid}&course={cid}`에서 **내가 쓴 글**을 모은다.

**3. 실패 사유를 NULL이 아니라 값으로 남긴다.**
`vod.status`가 그 예다. 재생 정보가 없을 때 NULL만 남기면 "파서 버그"와
"콘텐츠가 실제로 없음"이 구분되지 않는다. 실제로 이것 때문에 94편을 파서 실패로 오해했다.

## 스키마 마이그레이션

`CREATE TABLE IF NOT EXISTS`는 기존 테이블에 컬럼을 추가하지 않는다.
`Store._migrate()`가 SCHEMA를 파싱해 실제 테이블에 없는 컬럼을 `ALTER TABLE ADD COLUMN`으로
붙인다. 컬럼 추가만 지원하며, 타입 변경·삭제는 다루지 않는다.

## 동시성

SQLite를 WAL 모드 + `busy_timeout=30000`으로 연다. 긴 아카이빙을 백그라운드로 돌리면서
`status`·`analyze`를 동시에 실행하는 일이 흔하기 때문이다.
`Store.log()`는 쓰기 충돌 시 조용히 건너뛴다 — 진단 로그 한 줄 때문에 긴 작업이 죽으면 안 된다.
