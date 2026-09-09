# yonstudy

LearnUs의 강좌 정보, 강의자료, 게시글, 제출 현황과 VOD 진도를 한곳에 모아 두는
명령행 도구다. 필요한 학기만 골라 수집하고, 결과를 일반 폴더로 내보내거나 Synology,
SFTP, WebDAV, OneDrive 같은 rclone 저장소에 증분 업로드할 수 있다.

## 기능 한눈에 보기

| 할 일 | 명령 | 결과 |
|---|---|---|
| 로그인 | `login` | 다음 명령에서 재사용할 쿠키 저장 |
| 강좌 목록 갱신 | `courses` | 연도, 학기, 강좌 ID 출력 |
| 자료 수집 | `archive` | SQLite, 첨부파일, 게시글, 자막 저장 |
| 수집 현황 확인 | `status` | 강좌·활동·파일·진도 개수 출력 |
| 오늘 할 일 확인 | `report` | 공개 자료, 과제, 게시글, 미수강 영상 정리 |
| 로컬 폴더 만들기 | `export` | 학기/과목별 일반 파일 트리 생성 |
| NAS·클라우드 업로드 | `upload` | rclone remote에 증분 업로드 |
| 일일 작업 한 번에 실행 | `automate` | 현재 학기 수집, 업로드, 리포트 생성 |
| VOD·출석부만 확인 | `monitor` | 영상을 재생하지 않고 시청 순서 갱신 |
| 재생 대상 확인/실행 | `plan`, `watch` | 현재 학기 미완료 영상 재생 및 진도 재확인 |
| 영상 파생 파일 만들기 | `download` | 오디오, 슬라이드 프레임, 선택적으로 MP4 생성 |
| macOS 한글 파일명 복구 | `normalize-names` | 분해된 자모를 Windows 호환 NFC 파일명으로 변경 |
| 빠진 자막 만들기 | `transcribe` | 폴더를 재귀 검색해 로컬 Whisper SRT 생성 |
| 강의안과 자막 연결 | `analyze` | PDF 페이지와 자막 구간을 정렬한 리포트 생성 |
| 시간표 가져오기 | `timetable-import` | TOML/JSON/CSV 수업 시간을 강좌와 연결해 저장 |
| 수업 녹음 정리 | `classify-recordings` | 녹음 시각을 시간표와 대조해 과목별로 자동 분류 |

Python 3.10 이상을 사용한다. 기본 수집과 리포트는 Python 표준 라이브러리만으로
동작하고, 영상 및 PDF 기능은 필요한 패키지만 추가하면 된다.

Python 3.10에서 TOML 시간표를 사용할 때만 `python -m pip install tomli`가 필요하다.
Python 3.11 이상과 배포 컨테이너에는 TOML 파서가 기본 포함되어 있다.

## 빠른 시작

```bash
git clone https://github.com/HeIIowrld/yonstudy.git
cd yonstudy

python3 -m venv .venv
. .venv/bin/activate

python cli.py login
python cli.py courses
python cli.py archive --year 2026 --semester 2학기
python cli.py status
```

`login`은 터미널에서 학번과 비밀번호를 받고 로그인 쿠키를 저장한다. 이후 명령은 같은
쿠키를 사용한다. 세션을 다시 만들 때는 `python cli.py login`을 다시 실행하면 된다.

기본 저장 위치는 저장소 안의 `store/`다. 다른 위치를 쓰려면 실행 전에 환경변수를
설정한다.

```bash
export YONSTUDY_STORE="$PWD/store"
export LEARNUS_COOKIES="$PWD/store/learnus-cookies.txt"
```

전역 옵션은 하위 명령보다 앞에 둔다.

```bash
python cli.py --store /data/yonstudy archive --year 2026
python cli.py --interval 0.7 --per-minute 30 courses
```

## 선택 설치 항목

아래 기능을 쓸 때만 설치하면 된다.

```bash
# NAS·클라우드 업로드
sudo apt install rclone

# 오디오, 슬라이드 프레임, MP4 생성
sudo apt install ffmpeg

# PDF 강의안 분석
python -m pip install pypdf

# 로컬 음성 전사
python -m pip install "faster-whisper==1.2.1"

# VOD 브라우저 재생
python -m pip install playwright
python -m playwright install chromium
```

Chrome을 직접 사용하려면 다음 값도 지정할 수 있다.

```bash
export YONSTUDY_BROWSER_CHANNEL=chrome
```

## 로그인과 기본 상태 확인

```bash
python cli.py login       # 로그인 쿠키 생성 또는 갱신
python cli.py keepalive   # 현재 세션 확인 및 유휴 시간 연장
python cli.py courses     # 강좌 ID와 학기 확인
python cli.py status      # 지금까지 수집한 데이터 요약
```

백그라운드 실행에서 세션 만료 시 자동 로그인이 필요하면 `LEARNUS_ID`, `LEARNUS_PW`를
서비스 환경 파일에 설정한다. 설정하지 않으면 저장된 쿠키만 사용한다.

## 강좌 자료 수집

`archive`는 기본적으로 다음 항목을 수집한다.

- 강좌와 주차별 활동 메타데이터
- 강의자료, 게시판 첨부, 본인 제출 파일
- LearnUs 과제 본문, Gradescope LTI 문항과 연결된 Yonsei-OJ 문제 명세(HTML/Markdown)
- 게시판·포럼 글과 본문
- VOD 주소, 재생 가능 기간, 온라인 출석 진도
- LearnUs에서 제공하는 자막

영상 MP4 본체는 단독 `archive`가 받지 않는다. 현재 학기 전체 원본 보관은
`automate`가 매일 수행하며, 수동 실행은 `archive-videos`를 사용한다.

```bash
# 모든 강좌
python cli.py archive

# 특정 연도와 학기
python cli.py archive --year 2026 --semester 1학기

# 여러 연도·학기를 한 번에 선택
python cli.py archive --year 2025 2026 --semester 2학기 겨울계절수업

# courses에서 확인한 강좌 ID로 선택
python cli.py archive --course 285311 291204

# 최근 정렬 결과에서 강좌 3개만 처리
python cli.py archive --limit 3
```

수집 범위는 다음 옵션으로 조절한다.

| 옵션 | 꺼지는 기능 |
|---|---|
| `--no-vod` | VOD 뷰어 및 재생 정보 조회 |
| `--no-subtitles` | 자막 수집 |
| `--no-files` | 강의자료, 첨부파일, 제출 파일 다운로드 |
| `--no-boards` | 게시판·포럼 목록과 본문 수집 |
| `--board-pages 1` | 게시판별 첫 페이지만 조회 |
| `--board-pages 0` | 페이지 제한 없이 전체 게시글 조회 |

예를 들어 메타데이터와 게시글만 빠르게 갱신하려면 다음처럼 실행한다.

```bash
python cli.py archive --year 2026 --no-vod --no-subtitles --no-files
```

같은 명령을 다시 실행하면 저장된 항목을 확인한 뒤 새 항목과 변경된 항목만 처리한다.

### macOS 한글 파일명 복구

새로 수집하거나 내보내는 파일명은 한글 자모가 분리되지 않도록 NFC 조합형으로
저장한다. 기존 아카이브나 macOS에서 옮겨 온 폴더도 파일과 하위 폴더 이름을 재귀적으로
복구할 수 있다. 먼저 변경 목록과 이름 충돌을 확인한 뒤 실행한다.

```bash
python cli.py normalize-names "/data/LearnUs" --dry-run
python cli.py normalize-names "/data/LearnUs"
```

Windows에서는 경로만 Windows 형식으로 지정하면 된다.

```powershell
python cli.py normalize-names "D:\LearnUs" --dry-run
python cli.py normalize-names "D:\LearnUs"
```

조합형 이름이 이미 있어 충돌하는 항목은 덮어쓰지 않고 건너뛰며 종료 코드 `1`을
반환한다.

## 리포트

`report`는 오늘 공개된 자료, 새 게시글, 제출 완료 상태, 남은 과제와 미수강 영상을
현재 학기 기준으로 정리한다. 매일 보내는 메일은 과목별 수강률, 현재 남은 항목,
14일 이내 공개 예정 항목과 오늘의 변경 사항만 간결하게 표시한다. 학기 전체 강의와
과제 상세 목록은 일요일 메일에만 추가한다.

```bash
python cli.py report                         # 저장된 DB로 출력
python cli.py report --sync                  # 먼저 현재 학기를 가볍게 갱신
python cli.py report --sync-if-stale         # 오늘 동기화 기록이 없을 때만 갱신
python cli.py report --days 21               # 앞으로 21일 일정 표시
python cli.py report --json                  # JSON 출력
python cli.py report --output report.txt     # 파일로도 저장
python cli.py report --date 2026-09-06       # 기준 날짜 지정
```

메일을 켜려면 수신자와 SMTP 값을 설정한다.

```bash
export YONSTUDY_REPORT_TO='me@example.com'
export YONSTUDY_MAIL_FROM='me@example.com'
export YONSTUDY_SMTP_HOST='smtp.example.com'
export YONSTUDY_SMTP_PORT='587'
export YONSTUDY_SMTP_STARTTLS='1'
export YONSTUDY_SMTP_USER='me@example.com'
export YONSTUDY_SMTP_PASSWORD='앱 비밀번호'

python cli.py report --sync --email-to me@example.com
```

SMTP를 사용하지 않으면 관련 환경변수를 두지 않는다. `--email-if-configured`는 메일
설정이 있을 때만 발송하고, 없으면 화면 및 파일 리포트만 만든다.

## 로컬 폴더로 내보내기

SQLite와 blob 대신 탐색기에서 바로 볼 수 있는 파일 구조가 필요할 때 `export`를 쓴다.

```bash
python cli.py export --dry-run
python cli.py export --destination /data/LearnUs --year 2026 --semester 2학기
```

`--dry-run`은 복사할 개수와 용량만 계산한다. 실제 내보내기는 기존 파일을 지우지 않고
변경된 파일만 복사하며, 루트에 `yonstudy-manifest.json`을 만든다. 기본 경로는
`exports/archive/`이고 `YONSTUDY_EXPORT_DIR`로 바꿀 수 있다.

## Synology와 원격 저장소

원격 업로드는 rclone remote를 사용한다. Synology에서는 SMB, SFTP, WebDAV 중 하나를
선택할 수 있다.

| 연결 방식 | rclone 백엔드 | 필요한 값 | remote 경로 예시 |
|---|---|---|---|
| Synology 공유 폴더 | `smb` | NAS 주소, 사용자, 비밀번호 | `nas:home/yonstudy` |
| Synology SSH | `sftp` | NAS 주소, 사용자, SSH 키 또는 비밀번호 | `nas-sftp:/volume1/archive/yonstudy` |
| Synology WebDAV | `webdav` | WebDAV URL, 사용자, 비밀번호 | `nas-webdav:yonstudy` |
| Microsoft OneDrive | `onedrive` | Microsoft 로그인 승인 | `cloud:yonstudy` |

DSM에서 사용할 파일 서비스를 켠 뒤 rclone remote를 만든다.

```bash
rclone config
rclone listremotes
rclone lsd nas:
```

SMB remote에서는 콜론 다음 첫 디렉터리가 공유 이름이다. 예를 들어 `home` 공유 아래
`archive/yonstudy`에 저장하려면 `nas:home/archive/yonstudy`를 지정한다. rclone 설정값과
백엔드별 추가 옵션은 [SMB](https://rclone.org/smb/),
[SFTP](https://rclone.org/sftp/), [WebDAV](https://rclone.org/webdav/) 문서에서 확인할 수 있다.

yonstudy가 사용할 위치를 환경변수로 연결한다.

```bash
export YONSTUDY_REMOTE='nas:home/archive/yonstudy'

# DB 기준 대상 개수 확인
python cli.py upload --dry-run --year 2026 --semester 2학기

# 실제 업로드
python cli.py upload --year 2026 --semester 2학기
```

환경변수 대신 명령마다 remote를 넘겨도 된다.

```bash
python cli.py upload \
  --remote 'nas:home/archive/yonstudy' \
  --year 2026 --semester 2학기
```

업로드는 학기와 과목 폴더를 만들고 강좌 활동 색인, 자료, 자막, 첨부파일, 과제
명세(HTML/Markdown), 게시글 Markdown을 저장한다. 같은 경로에 같은 크기의 파일이 있으면
건너뛰며 원격 파일은 자동으로 삭제하지 않는다.
`missing_sources`가 있으면 `archive`를 다시 실행해 로컬 원본을 채운 뒤 업로드한다.
게시판 글과 첨부는 작성자가 본인인지와 관계없이 강좌 기록으로 NAS에 보관한다. 철회된
강좌는 이후 업로드 대상에서 제외하지만 이미 저장한 원격 파일은 삭제하지 않는다.

```text
2026-2/AIC2120_인공지능개론및응용/
  강좌정보.md
  W01-L01__강의자료__Lecture01__f1408.pdf
  W01-L02__강의영상__Week 1-2__cmid4529930.mp4
  게시판_첨부/
  QNA_공지/
  과제자료/
    Assignment #1/
      W02-L00__과제명세__Assignment #1__cmid4550129.md
      W02-L00__과제명세__Assignment #1__cmid4550129.html
  제출물/
  자막/
```

학기 폴더는 `1학기=1`, `2학기=2`, `여름계절수업=S`, `겨울계절수업=W` 형식을 쓴다.

yonstudy 자체를 Synology Container Manager에서 실행하고 GitHub의 새 버전을 자동 배포하는
구성은 [docs/SYNOLOGY.md](docs/SYNOLOGY.md)에 정리되어 있다. 컨테이너는 기존 systemd
타이머와 같은 한국시간 일정을 실행하며, DB·쿠키·강의 파일은 이미지 밖의 NAS 볼륨에
보존한다.

## 일일 자동화

`automate`는 현재 학기를 대상으로 아래 순서대로 동작한다.

1. 로그인 세션 확인
2. 강좌, 활동, VOD 진도, 자료와 게시글 동기화
3. 새 파일과 게시글을 rclone remote에 업로드
4. 현재 학기에서 접근 가능한 모든 VOD 원본을 remote에 증분 보관
5. 텍스트 리포트 생성
6. 메일 설정이 있으면 리포트 발송

```bash
python cli.py automate --dry-run
python cli.py automate
```

단계별 스위치는 다음과 같다.

| 옵션 | 동작 |
|---|---|
| `--no-sync` | LearnUs 갱신 없이 현재 DB 내용으로 업로드와 리포트 실행 |
| `--no-upload` | 자료·과제 명세·자막·VOD 원본 보관을 끄고 리포트만 생성 |
| `--no-mail` | 메일 발송을 끄고 `store/reports/`에 리포트만 저장 |
| `--dry-run` | 로그인, 다운로드, 업로드, 파일 기록 없이 실행 계획 출력 |
| `--remote <remote>:<path>` | 이번 실행의 저장 대상만 변경 |

한 번에 보관할 VOD 수는 환경변수로 정한다. 기본값 `0`은 현재 학기의 접근 가능한
영상을 제한 없이 모두 처리한다. 큰 학기에서 실행 시간을 나누려면 양수로 제한한다.

```bash
export YONSTUDY_VIDEO_ARCHIVE_LIMIT=0  # 기본값: 현재 학기 전체
export YONSTUDY_VIDEO_ARCHIVE_LIMIT=4  # 선택: 실행당 4편
```

자동화 결과는 `store/automation_state.json`, 생성한 리포트는 `store/reports/`에서 볼 수
있다.

## VOD 기능

### 재생 없이 상태만 갱신

`monitor`는 현재 학기의 새 VOD와 온라인 출석부를 읽고 과목·주차·차시 순서의 대기열을
갱신한다. 영상 재생이나 다운로드는 하지 않는다.

```bash
python cli.py monitor --dry-run
python cli.py monitor
python cli.py monitor --course 285311 291204
```

결과는 `store/monitor_state.json`에 저장된다. 강좌 목록 동기화에서 철회 강좌가 확인되면
이 마지막 상태 파일의 해당 출석·대기열 행도 즉시 제거된다.

### 재생 계획과 실행

```bash
python cli.py plan                  # 현재 재생 대상과 예상 시간
python cli.py watch --dry-run       # 실행할 영상만 확인
python cli.py watch --limit 1       # 한 편 재생
python cli.py watch --rate 1.5      # 배속 지정
```

`watch`는 Playwright 브라우저로 영상을 재생하고, 종료 후 LearnUs 진도를 다시 읽어
완료 상태를 확인한다. `scheduled-watch`는 같은 작업을 systemd용으로 한 번에 소량 실행한다.

```bash
python cli.py scheduled-watch --dry-run
python cli.py scheduled-watch --limit 1
```

### 현재 학기 영상 원본 보관

`archive-videos`는 현재 학기의 접근 가능한 VOD를 진도 추적 여부와 관계없이 임시
MP4로 내려받아 remote에 올리고 임시 파일을 정리한다. HLS 원본 다운로드만 수행하며
LearnUs 진도 기록 API는 호출하지 않는다. 기존 명령 이름 `archive-only`도 호환된다.

```bash
python cli.py archive-videos --dry-run
python cli.py archive-videos --limit 2
python cli.py archive-videos --course 285311 --remote 'nas:home/archive/yonstudy'
```

### 오디오, 프레임과 MP4 만들기

`download`의 기본 출력은 오디오와 슬라이드 전환 프레임이다.

```bash
python cli.py download --dry-run
python cli.py download --limit 10
python cli.py download --video             # 오디오 + 프레임 + MP4
python cli.py download --no-audio          # 프레임만
python cli.py download --no-frames         # 오디오만
python cli.py download --video --no-audio --no-frames   # MP4만
python cli.py download --course 285311 --limit 3
```

`--timeout`으로 영상별 제한 시간을 지정할 수 있고, `--force`를 붙이면 디스크 예상 용량
검사를 통과하지 않아도 실행한다.

### 폴더에서 빠진 자막 만들기

Windows 데스크톱에서 가장 간단하게 쓰려면
[`desktop/강의_자막_생성.py`](desktop/강의_자막_생성.py) 파일 하나만 학기 폴더에 복사한다.
그 파일을 더블클릭하면 자신의 위치를 학기 루트로 삼아 바로 실행한다. 별도 경로 설정이나
명령 입력은 필요 없다.

```text
2026-2/
├── 강의_자막_생성.py          ← 더블클릭
├── BIZ4208_확률적재고관리모델/
└── 다른 과목/
```

처음 실행할 때 사용자 `%LOCALAPPDATA%/yonstudy-transcriber`에 `whisper.cpp` 런타임과
`medium-q5_0` 모델을 내려받는다. 이 모델은 다국어 `medium`을 양자화해 메모리 사용량을
낮춘 버전이다. 모델과 임시 WAV는 학기 폴더나 NAS에 넣지 않는다. 강의마다 언어를 자동
감지하여 한국어는 `.ko.srt`, 영어는 `.en.srt`로 저장한다. Python 3.10 이상이 Windows x64
데스크톱에 설치되어 있어야 한다.

장치는 `CUDA → Vulkan → CPU` 순서로 자동 선택한다. NVIDIA 드라이버가 있으면 CUDA를
먼저 사용하고, CUDA가 없거나 실행에 실패하면 AMD, Intel, NVIDIA GPU에서 사용할 수 있는
Vulkan을 시도한다. Vulkan도 사용할 수 없으면 CPU로 같은 파일을 다시 처리한다. 백엔드가
달라져도 하나의 모델 파일을 공유하므로 모델을 중복으로 내려받지 않는다. 기본 사용자는
장치를 설정할 필요가 없다.

`transcribe`는 DB나 LearnUs 로그인 없이 지정한 폴더 아래의 영상과 음성 파일을 재귀
검색한다.
원본과 같은 이름의 `.srt` 또는 `.vtt`가 하나라도 있으면 건너뛰고, 없을 때만 원본 옆에
확장자를 제외한 원본 파일명 뒤에 `.ko.srt`를 붙여 자막을 만든다. 먼저 실제로 처리될
파일을 확인한다.

```bash
python cli.py transcribe '/mnt/hyunjin/homes/hjpark/02_Personal/01_학교/10.학기/2026-2' --dry-run
python cli.py transcribe '/mnt/hyunjin/homes/hjpark/02_Personal/01_학교/10.학기/2026-2'
```

Windows에서 저장소를 실행한다면 UNC 경로도 그대로 지정할 수 있다.

```powershell
py cli.py transcribe "\\hyunjin\homes\hjpark\02_Personal\01_학교\10.학기\2026-2" --dry-run
```

저장소의 `transcribe` 명령과 Docker 전사기는 `faster-whisper` 기반이며, 기본값은
`medium` 모델과 강의별 언어 자동 감지다. CPU에서는 `int8`, NVIDIA GPU에서는
`float16`을 사용한다. 언어를 고정하려면 `--language ko`, 모델을 바꾸려면
`--model large-v3`처럼 지정한다. Vulkan 자동 선택은 위의 Windows 단일 파일에만
적용된다.
전문 용어를 한 줄에 하나씩 적은 파일은 `--hotwords-file terms.txt`로 전달할 수 있다.

마지막 수정 후 120초가 지나지 않은 파일은 복사 중일 수 있어 다음 실행으로 미룬다. 전사
도중 원본의 크기나 수정 시각이 달라져도 임시 자막을 버리고 재시도한다. SRT는 완성된 뒤
같은 디렉터리에서 이름을 바꾸므로 NAS 또는 동기화 클라이언트에 미완성 파일이 노출되지
않는다.
기존 자막을 의도적으로 다시 만들 때만 `--force`를 쓴다.

OneDrive와 Google Drive는 로컬 동기화 폴더 또는 `rclone mount` 경로를 지정한다. 온라인
전용 파일은 읽는 순간 내려받기가 시작될 수 있으므로 장시간 운용할 서버에서는 오프라인
보관 폴더나 NAS 마운트를 권장한다. Whisper는 자막까지만 만들며, 강의 요약은 별도 LLM 또는
추출 요약 단계를 나중에 연결해야 한다.

Docker Compose에서는 CPU 전사기를 선택적 프로필로 켠다. 호스트 경로는
`YONSTUDY_ARCHIVE_DIR`, 컨테이너 안에서 스캔할 학기는 `YONSTUDY_TRANSCRIBE_ROOT`에
`/archive/2026-2`처럼 지정한다. 30분 간격으로 한 번에 한 편씩 처리한다.

```bash
docker compose --profile transcription up -d --build transcriber
docker compose logs -f transcriber
```

### 강의안과 자막 정렬

```bash
python cli.py analyze --cmid 4333924
python cli.py analyze --cmid 4333924 --slides ./lecture.pdf
```

`--slides`를 생략하면 같은 주차의 PDF 강의안을 찾아 사용한다. 자세한 출력 구조는
[docs/STUDYKIT.md](docs/STUDYKIT.md)에 정리되어 있다.

### 시간표 매칭과 수업 녹음 자동 분류

먼저 `archive`로 현재 강좌를 받아 둔 뒤 시간표를 TOML, JSON 또는 CSV로 준비한다. 시간표의
`course`는 아카이브의 과목명·과목코드와 대조하며, 이름이 비슷한 강좌가 여럿이면
`course_id`를 쓰면 된다. `days`에는 요일을 여러 개 넣을 수 있다.

```json
{
  "year": "2026",
  "semester": "2학기",
  "valid_from": "2026-09-01",
  "valid_to": "2026-12-20",
  "classes": [
    {
      "course": "데이터베이스",
      "days": ["월", "수"],
      "start": "09:00",
      "end": "10:50",
      "location": "공학관 101"
    },
    {
      "course_id": 285311,
      "weekday": "금",
      "start": "13:00",
      "end": "14:50"
    }
  ]
}
```

CSV는 `course`(또는 `course_id`), `weekday`, `start`, `end`, `valid_from`,
`valid_to`, `location` 열을 사용한다. 유효기간을 생략하면 `--year`와 `--semester`의
학기 범위를 적용한다.

```bash
# 시간표만 먼저 저장
python cli.py timetable-import ./timetable.json

# 시간표 갱신과 분류 결과 미리보기
python cli.py classify-recordings ~/Recordings \
  --timetable ./timetable.json --dry-run

# 실제 보관. 원본은 그대로 남는다.
python cli.py import-recording ~/Recordings/lecture.m4a

# 보관 성공 뒤 입력 폴더의 원본도 지우려는 경우에만 명시
python cli.py classify-recordings ~/Recordings --move

# 핫폴더 스캔. 첫 발견 후 120초 이상 크기와 mtime이 같아야 처리한다.
python cli.py scan-recordings ./inbox/recordings --destination ./exports/archive
```

녹음 시각은 미디어 `creation_time` → `파일명의 YYYYMMDD_HHMMSS` → 파일 mtime
순으로 정한다. 기본적으로 수업 전후 20분까지 같은 시간표 슬롯으로 인정하며
`--grace-minutes`로 바꿀 수 있다. 같은 시간에 두 과목이 겹치면 파일명에 포함된 과목명이나
과목코드로 해소한다. 그래도 하나로 정할 수 없는 파일은 억지로 배정하지 않고 각각
`store/recordings/ambiguous`, `store/recordings/unclassified`에 둔다.

주차는 학기 시작일이 포함된 월요일부터 계산하고, 같은 과목의 주간 수업을 요일·시간순으로
정렬해 차시를 붙인다. 예를 들어 월·수 수업의 수요일 녹음은 `W02-L02`처럼 저장된다.
수업 사이 정중앙 시각(예: 앞 수업이 13:50에 끝나고 다음 수업이 14:00에 시작할 때
13:55)에 녹음을 시작했다면 다음 수업을 우선한다.

분류된 파일은 SHA-256 blob으로 중복 제거한다. `--destination`을 주면 기존 평면 아카이브의
과목 루트에 `W03-L01__강의녹음__20260915_1000__r8ab12c34.m4a` 형태로 연결하고,
생략하면 `store/courses/<학기>/<과목>/recordings/`에 둔다. 같은 녹음을 다시 실행해도
DB에는 한 건만 남는다. 메타데이터 제목, 녹음시각 출처, 주차·차시, 매칭 방법과 신뢰도도
`recording` 테이블에 함께 기록한다.
시간표 없이 먼저 `unmatched`로 들어간 녹음도 이후 시간표가 추가되면 저장된 blob을
다시 판정해 과목 폴더로 자동 이동한다.

컨테이너 배포에서는 호스트의 `YONSTUDY_RECORDING_HOST_DIR`을
`/data/inbox/recordings`로 마운트해 3분마다 스캔한다. 휴대폰에서 접근하기 쉬운 공유 폴더를
호스트 경로로 지정하면 된다. 첫 스캔에서는 파일을 대기시키고 다음 스캔까지 크기와 mtime이
120초 이상 같을 때만 가져오므로 SMB 업로드 중인 파일을 읽지 않는다. 성공한 원본만 정확한
바이트가 blob과 아카이브에 보관된 뒤 투입 폴더에서 제거되며, 실패한 파일은 재시도를 위해
남는다. `deploy/timetable.toml.example`을 `/config/timetable.toml`로 복사해 편집하고
`YONSTUDY_TIMETABLE=/config/timetable.toml`을 설정하면 매 스캔 전에 시간표도 갱신한다.

## systemd로 기능 켜고 끄기

`systemd/`에는 기능별 서비스와 타이머가 들어 있다.

| 타이머 | 기본 일정 | 실행 기능 |
|---|---|---|
| `yonstudy-keepalive.timer` | 2시간마다 | 로그인 세션 유지 |
| `yonstudy-monitor.timer` | 02:00 KST | VOD·출석부 읽기 전용 갱신 |
| `yonstudy-watch.timer` | 02:30, 04:00, 05:30 KST | 회차마다 영상 최대 1편 재생 |
| `yonstudy-daily.timer` | 08:10 KST | 현재 학기 수집과 remote 업로드 |
| `yonstudy-report.timer` | 09:00 KST | 최신 리포트 생성 및 선택적 메일 발송 |
| `yonstudy-transcribe.timer` | 완료 30분 뒤 | 자막 없는 미디어 최대 1편 전사 |

먼저 예제 환경 파일을 복사하고 설치 경로, remote, 로그인 및 메일 값을 채운다.

```bash
sudo install -d -m 700 /etc/yonstudy
sudo install -m 600 systemd/yonstudy.env.example /etc/yonstudy/yonstudy.env
sudo editor /etc/yonstudy/yonstudy.env
```

서비스 파일은 기본적으로 저장소가 `/root/yonstudy`에 있다고 가정한다. 다른 위치에
설치했다면 `WorkingDirectory`, `ExecStart`, `ReadWritePaths`, `Documentation` 경로를
해당 위치로 바꾼다.

```bash
sudo install -m 644 systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

필요한 기능만 골라 켠다.

```bash
# 자료 수집과 NAS 업로드
sudo systemctl enable --now yonstudy-daily.timer

# 리포트 메일
sudo systemctl enable --now yonstudy-report.timer

# 세션 유지와 VOD 상태 확인
sudo systemctl enable --now yonstudy-keepalive.timer yonstudy-monitor.timer

# 예약 영상 재생
sudo systemctl enable --now yonstudy-watch.timer

# 로컬 Whisper 자막 생성(환경 파일에 YONSTUDY_TRANSCRIBE_ROOT 설정 후)
sudo systemctl enable --now yonstudy-transcribe.timer
```

기능을 끌 때는 해당 타이머만 비활성화한다.

```bash
sudo systemctl disable --now yonstudy-watch.timer
sudo systemctl disable --now yonstudy-report.timer
```

수동 실행과 로그 확인은 서비스 이름으로 한다.

```bash
sudo systemctl start yonstudy-daily.service
journalctl -u yonstudy-daily.service -n 100 --no-pager
systemctl list-timers 'yonstudy-*'
```

## 환경변수 모음

| 이름 | 기본값/역할 |
|---|---|
| `YONSTUDY_STORE` | SQLite, blob, 상태 파일 저장 위치. 기본 `./store` |
| `LEARNUS_COOKIES` | 로그인 쿠키 파일. 기본 `store/learnus-cookies.txt` |
| `LEARNUS_ID`, `LEARNUS_PW` | 백그라운드 자동 재로그인 |
| `YONSEI_OJ_ID`, `YONSEI_OJ_PW` | Yonsei-OJ 로그인. 생략하면 LearnUs 자동 로그인 값을 재사용 |
| `YONSTUDY_OJ_AUTO_JOIN` | 새 Yonsei-OJ 콘테스트 자동 참여. 기본 `0`; `1`일 때만 참여 상태 변경 |
| `YONSTUDY_REMOTE` | `upload`, `automate`, `archive-videos`의 rclone 대상 |
| `YONSTUDY_EXPORT_DIR` | `export` 기본 출력 경로 |
| `YONSTUDY_MAX_FILE_MB` | 일반 첨부파일 한 개의 최대 수집 크기. 기본 512MB |
| `YONSTUDY_VIDEO_ARCHIVE_LIMIT` | 자동화 한 번에 보관할 VOD 수. `0`(기본)은 현재 학기 전체 |
| `YONSTUDY_BROWSER_CHANNEL` | Playwright 브라우저 채널. 예: `chrome` |
| `YONSTUDY_REPORT_TO` | 자동 리포트 수신 주소 |
| `YONSTUDY_MAIL_FROM` | 발신 주소 |
| `YONSTUDY_SMTP_HOST`, `YONSTUDY_SMTP_PORT` | SMTP 서버와 포트 |
| `YONSTUDY_SMTP_STARTTLS` | STARTTLS 사용 여부. 기본 `1` |
| `YONSTUDY_SMTP_USER`, `YONSTUDY_SMTP_PASSWORD` | SMTP 인증 값 |
| `YONSTUDY_RECORDING_INBOX` | 녹음 핫폴더. 컨테이너 기본 `/data/inbox/recordings` |
| `YONSTUDY_RECORDING_HOST_DIR` | Docker가 마운트할 NAS의 휴대폰용 녹음 투입 폴더. 생략하면 아카이브의 형제 `00.녹음_넣기` |
| `YONSTUDY_RECORDING_DESTINATION` | 분류된 녹음을 둘 평면 아카이브 루트 |
| `YONSTUDY_RECORDING_STABLE_SECONDS` | 두 스캔 사이 파일 안정화 시간. 기본 120초 |
| `YONSTUDY_TIMETABLE` | 매 핫폴더 스캔 전에 가져올 TOML/JSON/CSV 시간표 |
| `YONSTUDY_TRANSCRIBE_ROOT` | 자막 누락을 재귀 검색할 학기 또는 아카이브 폴더 |
| `YONSTUDY_TRANSCRIBE_MODEL` | faster-whisper 모델. 기본 `medium` |
| `YONSTUDY_TRANSCRIBE_DEVICE` | `auto`, `cpu`, `cuda`. 기본 `auto` |
| `YONSTUDY_TRANSCRIBE_COMPUTE_TYPE` | 기본 `auto`(CPU `int8`, CUDA `float16`) |
| `YONSTUDY_TRANSCRIBE_LANGUAGE` | 음성 언어 코드. 기본 `auto`, 한국어 고정은 `ko` |
| `YONSTUDY_TRANSCRIBE_STABLE_SECONDS` | 수정 직후 파일의 처리 유예. 기본 120초 |
| `YONSTUDY_TRANSCRIBE_MODEL_CACHE` | 다운로드한 모델을 보존할 경로 |

## 저장되는 파일

```text
store/
  db.sqlite                 수집한 강좌, 활동, 진도, 게시글 메타데이터
  blobs/                    로컬로 받은 원본 파일
  reports/                  날짜별 텍스트 리포트
  automation_state.json     마지막 자동화 결과
  monitor_state.json        VOD·출석부 확인 결과
  watch_state.json          예약 재생과 진도 확인 결과
  recording_scan_state.json 핫폴더 파일 크기·mtime 안정화 상태
  yonsei-oj-cookies.txt      Yonsei-OJ 문제 명세 수집용 로그인 쿠키
  recordings/               모호하거나 미분류된 외부 녹음
```

자료 구조와 내부 흐름은 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), LearnUs 파싱 메모는
[docs/LEARNUS.md](docs/LEARNUS.md), 재생 흐름은 [docs/AUTOPLAY.md](docs/AUTOPLAY.md)에 있다.

## 라이선스

코드는 [Apache License 2.0](LICENSE)으로 배포한다.
