# yonstudy

LearnUs에 흩어져 있는 강의자료, 게시글, 제출 현황, 동영상 진도를 로컬
SQLite에 모아 보는 명령행 도구다. 학기가 지난 뒤 자료를 찾을 때마다 강의실을
하나씩 열어 보는 게 번거로워서 만들었다.

아카이브는 중간에 끊겨도 다시 실행하면 이어서 진행한다. 수집한 파일은 로컬에
남겨도 되고, rclone을 통해 OneDrive, SMB, SFTP, WebDAV 저장소로 바로 보낼 수도
있다. Synology NAS는 SMB, SFTP, WebDAV 중 NAS에서 켜 둔 프로토콜을 사용하면 된다.

## 주요 기능

- 강좌, 활동, 첨부파일, 게시판·포럼 글 증분 수집
- VOD 재생 정보와 온라인 출석부 진도 확인
- 오늘 공개된 자료, 남은 과제, 미수강 영상을 묶은 일일 리포트
- 강의 자료와 게시글을 학기/과목 구조로 내보내기
- rclone remote로 로컬 staging 없이 업로드
- 선택 기능: 오디오 추출, 자막과 강의안 정렬, 브라우저 재생

코어 기능은 Python 표준 라이브러리만 사용한다. Python 3.10 이상과 Linux에서 주로
개발했다.

## 설치

```bash
git clone https://github.com/HeIIowrld/yonstudy.git
cd yonstudy
python3 -m venv .venv
. .venv/bin/activate
```

일반 아카이빙은 추가 Python 패키지 없이 돌아간다. 아래 항목은 필요한 기능만 설치하면
된다.

```bash
# 오디오·영상 추출
sudo apt install ffmpeg

# PDF 강의안 분석
python -m pip install pypdf

# 로컬 음성 전사
python -m pip install faster-whisper

# 실제 브라우저 재생
python -m pip install playwright
python -m playwright install chromium
```

저장 경로를 먼저 지정해 두면 다른 계정이나 설치 경로에서도 코드를 고칠 필요가 없다.

```bash
export YONSTUDY_STORE="$PWD/store"
export LEARNUS_COOKIES="$PWD/store/learnus-cookies.txt"
```

`store/`, 쿠키, `.env` 파일은 Git에 올라가지 않도록 `.gitignore`에 등록되어 있다.

## 처음 실행

```bash
python cli.py login
python cli.py courses
python cli.py archive --year 2026
python cli.py status
```

`login`은 비밀번호를 터미널에서만 받고 파일에 쓰지 않는다. 대신 로그인 쿠키를
저장하며 파일 권한은 600으로 설정한다. 세션이 만료되면 `login`을 다시 실행하면
된다.

자주 쓰는 명령은 다음과 같다.

```bash
python cli.py archive --course 285311       # 특정 강좌만 수집
python cli.py report --sync                  # 이번 학기 상태를 갱신한 뒤 리포트
python cli.py export --dry-run               # 로컬 내보내기 계획
python cli.py monitor                        # 재생 없이 VOD와 출석부 갱신
python cli.py download --limit 10            # 오디오와 슬라이드 프레임 추출
python cli.py analyze --cmid 4333924         # 강의안과 자막 정렬
```

전체 옵션은 `python cli.py <명령> --help`로 확인할 수 있다.

## 원격 저장소 연결

업로드 코드는 특정 서비스 API를 직접 사용하지 않고 rclone remote를 사용한다. 현재
사용하는 `lsjson`, `rcat`, `copyto`, `moveto`는 OneDrive, SMB, SFTP, WebDAV 백엔드에서
모두 제공되는 공통 명령이다.

먼저 rclone을 설치하고 remote 하나를 만든다.

```bash
sudo apt install rclone
rclone version
rclone config
rclone listremotes
rclone lsd <remote>:
```

SMB 백엔드는 rclone 1.60부터 들어 있으므로 그보다 오래된 버전이면
[rclone 공식 설치 안내](https://rclone.org/install/)에 따라 새 버전을 설치한다.

연결 형태에 따라 `rclone config`에서 아래 저장소를 고르면 된다.

| 사용처 | rclone 백엔드 | remote 경로 예시 | 메모 |
|---|---|---|---|
| OneDrive | `onedrive` | `onedrive:yonstudy` | 처음 한 번 Microsoft 로그인 승인 필요 |
| Synology/Windows 공유 | `smb` | `nas:home/yonstudy` | 첫 경로 요소는 SMB 공유 이름 |
| Synology/Linux 서버 | `sftp` | `nas-sftp:/volume1/archive/yonstudy` | 비밀번호보다 SSH 키 권장 |
| Synology/Nextcloud 등 | `webdav` | `nas-webdav:yonstudy` | HTTPS URL 사용 권장 |

설정할 때 필요한 값은 많지 않다. SMB는 서버 주소, 사용자, 비밀번호와 공유 이름이
필요하고 기본 포트는 445다. SFTP는 서버 주소, 사용자, SSH 키 경로가 기본이며 포트는
22다. WebDAV는 HTTPS URL, 사용자, 비밀번호를 넣고 일반 서버라면 vendor를 `other`로
고르면 된다. 비밀번호는 셸 명령 인자로 넘기지 말고 `rclone config` 안에서 입력한다.

Synology에서는 DSM의 파일 서비스(SMB/SFTP) 또는 WebDAV Server 패키지 중 하나를 먼저
켜야 한다. 접속 계정은 백업용 공유 폴더에만 쓰기 권한을 주는 편이 안전하다.
WebDAV는 가능하면 HTTP 대신 HTTPS를 사용한다.
SFTP는 `known_hosts_file`을 지정해 서버 키를 확인하는 편이 좋다. Synology SFTP에서
해시 계산 경로 오류가 나면 [rclone SFTP 안내](https://rclone.org/sftp/)의
`path_override` 또는 `disable_hashcheck` 설정을 확인한다.

연결을 확인했으면 yonstudy에 remote를 넘긴다.

```bash
export YONSTUDY_REMOTE='nas:home/yonstudy'

# 업로드 대상 개수만 확인. remote에 접속하지 않음
python cli.py upload --dry-run --year 2026 --semester 2학기

# 이미 수집한 로컬 자료와 게시글 업로드
python cli.py upload --year 2026 --semester 2학기

# 현재 학기 수집, 업로드, 리포트를 한 번에 실행
python cli.py automate --no-mail
```

`upload` 결과의 `missing_sources`가 0보다 크면 로컬 blob이 없는 항목이다. 예전에
다른 remote로 바로 보낸 자료일 수 있으므로 `archive`로 원본을 다시 받은 뒤
`upload`를 한 번 더 실행한다.

`--remote nas:home/yonstudy`처럼 명령행에서 바로 지정해도 된다. 예전 설정과의 호환을
위해 `YONSTUDY_ONEDRIVE_REMOTE`, `--no-onedrive`, `export-onedrive` 이름도 계속 받지만,
새 설정에서는 `YONSTUDY_REMOTE`, `--no-upload`, `export`를 쓰는 것을 권장한다.

rclone은 설정 파일의 비밀번호를 단순히 가려서 저장하며 강하게 암호화하지는 않는다.
`rclone.conf`의 권한을 600으로 유지하고, 여러 사용자가 같이 쓰는 서버에서는 계정별
설정 파일을 분리하는 것이 좋다.

## 저장 구조와 동기화 방식

원격에는 다음과 같은 구조로 저장된다.

```text
2026-2/AIC2120_인공지능개론및응용/
  W01-L01__강의자료__Lecture01__f1408.pdf
  W01-L02__강의영상__Week 1-2__cmid4529930.mp4
  게시판_첨부/
  QNA_공지/
  과제자료/
  제출물/
```

학기 코드는 `1학기=1`, `2학기=2`, `여름계절수업=S`, `겨울계절수업=W`다. 파일명은
Windows의 금지 문자와 경로 길이를 고려해 정리한다. 제목이 같은 파일도 겹치지 않도록
LearnUs의 고정 ID를 파일명 끝에 붙인다.

`제출물/`에는 본인이 낸 파일이 들어갈 수 있다. 이름, 학번, 과제 내용 같은 개인정보가
섞일 수 있으므로 remote를 공개 공유 폴더로 두면 안 된다.

업로드는 증분 방식이며 원격 파일을 지우지 않는다. 같은 경로에 같은 크기의 파일이
있으면 올리지 않는다. 파일이 변경됐는데 크기가 우연히 같은 특수한 경우에는 rclone으로
해당 원격 파일을 지운 뒤 다시 실행해야 한다.

원격 저장소가 응답하지 않으면 로컬로 몰래 대체 저장하지 않고 실패 상태를 남긴다.
다음 실행에서 다시 받거나 업로드한다. 실행 결과는 `store/automation_state.json`,
수동 플레이백 상태는 `store/watch_state.json`에서 확인할 수 있다.

`automate`는 진도 추적이 꺼진 VOD 원본도 한 번에 최대 4편까지 remote에 보관한다.
`YONSTUDY_ARCHIVE_ONLY_LIMIT`로 수량을 바꿀 수 있으며, 따로 실행하려면
`python cli.py archive-only --remote <remote>:경로`를 사용한다.

## 리포트 메일

`report`는 한국 시간을 기준으로 오늘 공개된 활동, 새 게시글, 완료된 제출, 남은
과제와 영상을 보여 준다. SMTP 정보가 있으면 메일로도 보낼 수 있다.

```bash
export YONSTUDY_REPORT_TO='me@example.com'
export YONSTUDY_MAIL_FROM='me@example.com'
export YONSTUDY_SMTP_HOST='smtp.example.com'
export YONSTUDY_SMTP_PORT='587'
export YONSTUDY_SMTP_USER='me@example.com'
export YONSTUDY_SMTP_PASSWORD='앱 비밀번호'
python cli.py report --sync
```

SMTP 비밀번호는 DB에 저장하지 않는다. 정기 실행에서는 일반 사용자가 읽을 수 없는
환경 파일에 넣는다. [systemd/yonstudy.env.example](systemd/yonstudy.env.example)을
복사해 시작해도 된다. SMTP 설정이 없으면 `/usr/sbin/sendmail`을 찾아 사용한다.

## 자동 실행

`systemd/`의 서비스 파일은 이 저장소가 `/root/yonstudy`에 설치된 서버용 예시다. 그대로
복사하기 전에 `WorkingDirectory`, `ExecStart`, `ReadWritePaths`, `Documentation`을 실제 설치
경로에 맞게 고쳐야 한다. rclone 설정 파일 경로도 서비스 실행 계정을 기준으로 확인한다.

`automate`는 현재 학기 동기화, 원격 업로드, 리포트 생성을 묶어서 실행한다.

```bash
python cli.py automate --dry-run
python cli.py automate --no-mail
python cli.py automate --no-upload   # 메타데이터와 게시글만 갱신
```

## 자동 재생에 대해

`watch` 및 `scheduled-watch`는 Playwright로 영상을 실제 재생한다. 서버의 진도 API를 직접
조작하지는 않지만, 자동 재생 자체가 수업 또는 학교 규정에 어긋날 수 있다. 관련 규정을
확인하고 본인 계정에서만 사용해야 한다. 처음에는 반드시 계획만 확인한다.

```bash
python cli.py plan
python cli.py watch --dry-run
```

## 사용 범위와 배포 전 확인할 것

자신이 접근 권한을 가진 강좌에서, 개인적으로 필요한 범위만 수집하는 것을 전제로 한다.
강의자료와 영상의 저작권은 각 권리자에게 있으므로, 생성된 아카이브를 재배포해서는 안 된다.
LearnUs 약관, 학교 규정, 수업별 안내를 우선한다.

코드 자체를 공개 배포하려면 아래도 한 번 확인하는 것이 좋다.

- `store/`, `exports/`, 쿠키, `.env`, 로그가 커밋에 들어가지 않았는지 확인
- `systemd/` 예시의 절대 경로와 메일 주소 같은 개인 설정 제거
- `LICENSE`와 사용한 외부 패키지의 라이선스 조건 확인
- 실제 계정 없이 실행할 수 있는 테스트와 환경별 설치 절차 확인

## 문서

- [구조와 데이터 모델](docs/ARCHITECTURE.md)
- [LearnUs 페이지·엔드포인트 메모](docs/LEARNUS.md)
- [아카이브 실측 결과](docs/FINDINGS.md)
- [전사와 강의안 정렬](docs/STUDYKIT.md)
- [재생과 진도 처리](docs/AUTOPLAY.md)
- [현재 상태와 남은 일](docs/ROADMAP.md)

## 라이선스

이 프로젝트의 코드는 [Apache License 2.0](LICENSE)으로 배포한다. 강의자료, 영상과 같이
프로그램으로 수집한 콘텐츠에는 이 라이선스가 적용되지 않는다.
