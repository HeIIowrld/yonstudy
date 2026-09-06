# yonstudy

연세대 LearnUs 아카이버 + 강의 학습 도우미.

LearnUs 활동 메타데이터를 로컬에서 검색 가능하게 만들고, 새 강의자료와 첨부파일은
로컬 사본 없이 OneDrive로 직접 저장한다. 동영상 진도 자동 완성도 포함한다.

**현재 아카이브 상태** (2026-08-02 기준, 실측)

| 항목 | 수량 |
|---|---:|
| 강좌 | 49 (2022~2026, 12개 학기) |
| 활동 | 2,045 |
| 동영상 | 697 (진도 100% = 624) |
| 제출형 활동 | 377 (제출 확인 296) |
| 게시판·포럼 글 | 1,697 (111개 게시판) |
| 파일 | 1,406개 |
| 자막 | 150 |
| 디스크 | 1.5GB (논리 4.0GB, sha256 중복제거) |

---

## 설치

핵심 기능은 **파이썬 표준 라이브러리만** 쓴다. 선택 기능만 의존성이 필요하다.

```bash
cd /root/yonstudy
python3 -m venv .venv

# 슬라이드 분석용
.venv/bin/pip install pypdf

# 오디오 추출용
apt-get install -y ffmpeg

# 로컬 전사(STT)용
.venv/bin/pip install faster-whisper

# 자동수강용 — 설치 후 코드가 H.264/AAC 지원 여부를 실행 전에 검사한다
.venv/bin/pip install playwright && .venv/bin/playwright install chromium
```

## 사용

```bash
python3 cli.py login                       # 연세 SSO 로그인 (쿠키 저장)
python3 cli.py courses                     # 내 강좌 전체 목록
python3 cli.py archive                     # 전 학기 아카이빙 (영상 본체 제외)
python3 cli.py archive --year 2026         # 특정 연도만
python3 cli.py archive --course 285311     # 특정 강좌만
python3 cli.py status                      # 아카이브 현황

python3 cli.py audio --limit 10            # 동영상에서 오디오만 추출 (opus)
python3 cli.py analyze --cmid 4333924      # 슬라이드 ↔ 자막 정렬 분석

python3 cli.py plan                        # 자동수강 대상/우선순위
python3 cli.py watch --dry-run             # 재생 계획 확인
python3 cli.py watch                       # 실제 재생 (진도 채우기)

python3 cli.py report --sync               # 오늘 공개/완료/공지/할 일 확인
python3 cli.py report --sync --email-to me@example.com
python3 cli.py export-onedrive --dry-run    # 자료·Q&A 내보내기 계획
python3 cli.py automate --dry-run           # 백그라운드 작업 전체 점검
python3 cli.py scheduled-watch --dry-run    # 다음 자동수강 1편과 정상 마감 확인
python3 cli.py archive-only --dry-run       # 진도 비추적 VOD의 OneDrive 보관 계획
```

## 일일 리포트와 메일

`report`는 **한국시간(KST)** 기준으로 현재 학기를 가볍게 동기화한 뒤 아래를 구분해 보여준다.

- 오늘 공개된 활동
- 직전 동기화 이후 100%가 된 것으로 확인된 영상·완료된 제출
- 새 게시글을 Q&A·공지·자료·기타로 분류하고 본문 요약과 링크 표시
- 새 강의자료와 게시판 첨부(동일 파일 해시 중복 제거)
- 미완료 항목의 정상 마감·지각 마감·남은 영상시간·권장 처리일
- 이번 학기 과목별 동영상 수강률과 전체 강의별 완료·진도·남은 시간·공개 예정 목록
- 이번 학기 전체 과제·퀴즈 등 제출 활동의 제출 완료·미제출·공개 예정 목록
- 현재 남은 미수강 영상과 미제출 활동, 오늘 새로 확인된 완료 상세

세션이 만료됐으면 먼저 `python3 cli.py login`을 실행해야 한다. `--sync` 없이 오래된 DB를
읽으면 리포트 첫머리에 오래된 데이터라는 경고가 나온다.

메일은 `--email-to` 또는 `YONSTUDY_REPORT_TO`로 받는 주소를 지정한다. SMTP를 쓸 때는
아래 환경변수를 설정한다. 설정이 없으면 로컬 Postfix의 `/usr/sbin/sendmail`을 사용하지만,
Postfix의 외부 발송 설정 여부에 따라 실제 배달은 실패할 수 있다.

```bash
export YONSTUDY_REPORT_TO='me@example.com'
export YONSTUDY_MAIL_FROM='me@example.com'
export YONSTUDY_SMTP_HOST='smtp.example.com'
export YONSTUDY_SMTP_PORT='587'
export YONSTUDY_SMTP_USER='me@example.com'
export YONSTUDY_SMTP_PASSWORD='앱 비밀번호'
python3 cli.py report --sync
```

SMTP 비밀번호는 소스 코드나 DB에 저장하지 않는다. 백그라운드 서비스에서는 별도의
root 전용 환경 파일(권한 600)에만 직접 입력한다.

## OneDrive 자동 백업

일일 자동화는 현재 학기의 강의자료, 게시판·과제 첨부, ubboard/forum 글을 메모리에서
OneDrive로 직접 전송한다. 로컬 export 폴더를 만들지 않으며 기존 원격 파일은 삭제하지 않는다.

```text
<연도>-<학기코드>/<과목>/
├── W01-L01__강의자료__<파일명>__f<파일ID>.pdf
├── 게시판_첨부/<게시판>/
└── QNA_공지/<게시판>/<날짜>_<글번호>_<제목>.md
```

강의자료는 별도 폴더를 만들지 않고 과목 루트에 저장한다. 파일명은 주차·차시
`W01-L01`을 앞에 붙이며, 주차를 알 수 없으면 공개일 또는 수집일 `YYYYMMDD`를 붙인다.

학기 폴더는 `1학기=1`, `2학기=2`, `여름계절수업=S`, `겨울계절수업=W`로 표기한다.
회사 OneDrive 기준 업로드 루트는 `02_Personal/01_학교/10.학기`이며, 예를 들어
2026-1학기 자료는 `10.학기/2026-1/<과목>/...`에 들어간다.

Microsoft 계정 연결은 한 번 직접 승인해야 한다. 연세 OneDrive는 일반 `@yonsei.ac.kr`
메일과 별개의 Office 365 계정(`사용자ID@o365.yonsei.ac.kr`)일 수 있다.

```bash
# remote 이름은 반드시 yonstudy-onedrive 로 지정하고 Storage에서 OneDrive를 선택
rclone config
rclone lsd yonstudy-onedrive:

# 연결 후 즉시 한 번 실행
systemctl start yonstudy-daily.service
journalctl -u yonstudy-daily.service -n 100 --no-pager
```

헤드리스 인증에서 다른 PC의 `rclone authorize`를 쓸 때는 서버와 PC의 rclone 버전을
같게 맞춘다(현재 서버 `v1.75.0`). `config_token>`에는 일부 access token이 아니라
authorize 명령이 출력한 첫 `{`부터 마지막 `}`까지의 JSON 전체를 한 줄로 붙여넣는다.

등록된 `yonstudy-daily.timer`는 매일 08:10 KST(최대 10분 무작위 지연)에 현재 학기의
메타데이터·출석 상태를 동기화하고 새 파일을 OneDrive에 직접 저장한다. OneDrive가
응답하지 않으면 파일은 로컬로 대체 저장하지 않고 실패 상태를 남겨 다음 실행에서 재시도한다.
`yonstudy-report.timer`는 동기화가 끝난 뒤 매일 09:00 KST에 리포트를 별도로 발송한다.
SMTP 인증이 아직 없으면 오류를 내지 않고 발송만 보류한다. LearnUs 쿠키가 만료되면
`python3 cli.py login`을 다시 실행해야 한다.

`yonstudy-keepalive.timer`는 2시간마다 읽기 요청을 보내 세션의 유휴 만료를 연장한다.
세션이 만료되면 root 전용 `/etc/yonstudy/yonstudy.env`의 `LEARNUS_ID`와 `LEARNUS_PW`로
SSO 로그인을 자동 복구한다. 잘못된 비밀번호나 캡차로 계정이 잠기는 것을 막기 위해
실패 후 재시도는 15분, 1시간, 6시간, 24시간 간격으로 점차 늦춘다. MFA/캡차가 발생하면
자동 우회하지 않으며 브라우저에서 한 번 해제해야 한다.

`yonstudy-monitor.timer`는 매일 02:00 KST에 현재 학기의 새 VOD와 온라인출석부를
읽기 전용으로 갱신한다. 영상을 재생하거나 다운로드하지 않으며, 미완료 영상은 과목별 →
주차 → 차시 순서로 `store/monitor_state.json`에 저장한다. 사용자가 시청한 뒤 즉시 확인할
때는 `python3 cli.py monitor --course <course_id>`를 실행한다. 별도 호출이 없어도 08:10
일일 동기화가 최대 학습위치와 진도율을 다시 읽고 09:00 리포트에 반영한다.

`yonstudy-watch.timer`는 매일 새벽 02:30·04:00·05:30 KST(각 최대 10분 지연)에
정상 수강기간 안의 미완료 영상 또는 완료 추적이 없는 미재생 영상을 한 편씩 재생한다.
02:00 읽기 전용 모니터가 먼저
새 영상과 출석 상태를 갱신한다. 낮 시간에 누락 실행되지 않도록 catch-up은 사용하지
않는다. 모니터·자동수강·일일 동기화·리포트 작업은 같은 파일 잠금을 사용한다.

진도 추적이 꺼진 VOD(`is_progress=false`)는 출석 진도 큐에는 넣지 않지만, 실제 영상을
끝까지 한 번 재생한 뒤 `playback_once` 성공 기록을 남겨 중복 재생하지 않는다. 일일
자동화는 해당 영상 원본도 최대 4편씩 읽어 같은 학기·과목 OneDrive 폴더에 직접 보관하고
임시 로컬 파일은 즉시 삭제한다. `YONSTUDY_ARCHIVE_ONLY_LIMIT`로 일일 상한을 바꿀 수 있다.

설정 파일은 `/etc/yonstudy/yonstudy.env`(권한 600), 서비스는
`/etc/systemd/system/yonstudy-daily.service`, 타이머는
`/etc/systemd/system/yonstudy-daily.timer`다. `@yonsei.ac.kr` 메일은 Gmail SMTP를
사용하도록 준비되어 있으며 Google 앱 비밀번호를 아래 항목에 직접 넣어야 실제 메일이 발송된다.

```bash
sudoedit /etc/yonstudy/yonstudy.env
# YONSTUDY_SMTP_PASSWORD=발급한_앱_비밀번호
systemctl start yonstudy-daily.service
```

자동수강 상태는 `store/watch_state.json`, 일일 동기화·메일 상태는
`store/automation_state.json`에서 확인한다.

## 평면형 개인 아카이브

OneDrive 트리가 깊어지지 않도록 강의자료에는 주차·차시·종류를 파일명 접두어에 넣는
`flat-v2` 구조를 사용한다. 게시글과 향후 영상까지 포함한 전체 배치는 아래 명령으로
계획만 확인할 수 있다.

```text
2026-2/AIC2120_인공지능개론및응용/
  W01-L01__강의영상__Week 1-1__cmid4529929.mp4
  W01-L01__강의자료__Lecture01__f1408.pptx
  W01-L02__강의자료__Lecture02__f1407.pdf
  W00-L00__공지__20260901_Welcome to AIC 2120__p2289406.md
```

`W00-L00`은 특정 주차에 속하지 않는 공통 공지·Q&A다. 파일 끝에는 LearnUs/DB의
고정 식별자를 붙여 제목이 같아도 충돌하지 않는다. Windows 전체 경로 제한을 고려해
파일명은 UTF-8 150바이트 이하로 제한한다.

```bash
# 읽기 전용 계획 확인. 실제 파일은 만들지 않는다.
python3 cli.py layout-plan --year 2026 --semester 2학기
```

향후 활성화할 준비 흐름은 `과목 새로고침 → 자료/게시판 수집 → 평면 파일명 생성 →
사용자가 시청 → 원본 MP4 개인 아카이브 → 온라인출석부 읽기 전용 확인 → 리포트` 순서다.
영상 원본은 기존 HLS를 ffmpeg `-c copy`로 한 번만 읽어 저장하고, 오디오·프레임 같은
파생물은 기본 생성하지 않는다. 자동 출석 재생과 OneDrive 업로드에는 연결하지 않는다.

기본 첨부 한도는 512MB이며 `YONSTUDY_MAX_FILE_MB`로 조정할 수 있다.

주요 옵션

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--interval` | 0.4 | 요청 간 최소 간격(초) |
| `--per-minute` | 40 | 분당 요청 상한 |
| `--board-pages` | 3 | 게시판당 목록 페이지 수 (0=전체) |
| `--no-vod` / `--no-files` / `--no-boards` | | 해당 단계 생략 |

**로그인은 터미널에서 직접 실행**하세요. 비밀번호는 `getpass`로만 받고 디스크에 쓰지 않습니다.
저장되는 것은 쿠키 파일뿐이며 권한은 600입니다.

## 안전 원칙

- **읽기 전용.** 글쓰기·과제 제출·설정 변경 API는 코드에 넣지 않았다.
- **증분.** 이미 받은 것은 건너뛴다. 중단 후 같은 명령을 다시 실행하면 이어받는다.
- **영상 본체는 저장하지 않는다.** 전사가 필요하면 오디오만 뽑는다.
- **자동수강은 실제 재생만 한다.** 진도 로그를 직접 POST로 위조하지 않는다.
  자동 재생은 학칙상 부정수강으로 해석될 소지가 있고, 책임은 계정 주인에게 간다.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 모듈 구조, 데이터 모델, 저장소 레이아웃 |
| [docs/LEARNUS.md](docs/LEARNUS.md) | LearnUs/coursemos 리버스 엔지니어링 레퍼런스 — 엔드포인트·DOM·함정 |
| [docs/FINDINGS.md](docs/FINDINGS.md) | 실측 데이터 — 커버리지, 용량 벤치마크, ASR 성능 |
| [docs/STUDYKIT.md](docs/STUDYKIT.md) | 전사·슬라이드 정렬·hotword 설계와 한계 |
| [docs/AUTOPLAY.md](docs/AUTOPLAY.md) | 진도 처리 방식 분석과 자동수강 구현 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 검증된 것 / 안 된 것 / 다음 할 것 |
