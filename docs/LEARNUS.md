# LearnUs/Coursemos 참조 문서

`ys.learnus.org`에서 직접 확인한 내용을 정리했다. 모든 셀렉터와 엔드포인트는 2026-08-02 기준이다.
DOM이 바뀌면 [`yonstudy/parse.py`](../yonstudy/parse.py) 한 곳만 고치면 된다.

## 플랫폼

- Moodle 기반, 테마 `coursemosv2`(유비온 Coursemos 커스텀)
- 세션 쿠키 `MoodleSession`(path=/, Secure, SameSite=None)
- Moodle Web Service가 켜져 있다. `/login/token.php`, `/webservice/rest/server.php`가
  정상 Moodle JSON 에러를 반환한다. SSO 계정에 토큰이 발급되면 HTML 파싱 대신 공식 API로
  전환할 수 있다. 발급 여부는 아직 확인하지 않았으며 [ROADMAP](ROADMAP.md)에 기록해 두었다.
- AJAX API `/lib/ajax/service.php`(sesskey 필요)

## SSO 인증 5단계

yontil 확장(`src/core/login/login-learnus.ts`)의 인증 흐름을 이식했다.
[`client.py`](../yonstudy/client.py)에 구현.

```
1  GET  ys.learnus.org/passni/sso/spLogin2.php          → S1 (512자)
     ※ Referer: https://ys.learnus.org 필수
2  POST infra.yonsei.ac.kr/sso/PmSSOService              → ssoChallenge + RSA 공개키
     app_id=ednetYonsei, retUrl/failUrl/baseUrl=https://ys.learnus.org, S1, refererUrl
3  POST infra.yonsei.ac.kr/sso/PmSSOAuthService          → E3, E4, S2, CLTID
     E2 = RSA_PKCS1v15({"userid","userpw","ssoChallenge"}) → 대문자 hex
4  POST ys.learnus.org/passni/sso/spLoginData.php        (E3/E4/S2/CLTID 전달)
5  GET  ys.learnus.org/passni/spLoginProcess.php         → MoodleSession 승격
```

### 확인한 사항

- RSA는 1,024비트이며 exponent는 10001이다. `E2`는 256자리 16진수(128바이트)다.
  PKCS#1 v1.5 패딩을 직접 구현하면 `node-forge`와 바이트 단위로 호환되므로 외부 라이브러리가 필요 없다.
- 확장은 콘텐츠 스크립트에서 `Referer`를 바꿀 수 없어 `declarative_net_request` 규칙을 썼지만,
  파이썬에서는 헤더를 직접 세팅하면 되므로 그 장치가 필요 없다.
- 인증 실패 시에도 서버가 `E3`와 `E4`를 빈 값으로 반환한다. `S2`와 `CLTID`의 존재 여부로 성공을 판단한다.
- 응답에 `captcha_yn`과 `botCheck` 필드가 있다. 로그인 실패를 반복하면 캡차가 걸리므로
  자동 재시도는 1회로 제한하고 캡차를 감지하면 중단한다.
- 메인 페이지 HTML에 `/login/logout.php` 문자열이 있으면 세션이 살아 있는 것으로 판단한다.
- `sesskey`는 `sesskey":"([^"]+)` 정규식으로 추출한다.

## 엔드포인트

### 강좌

| 경로 | 내용 |
|---|---|
| `/local/ubion/user/index.php?year={YYYY\|all}&semester={10\|11\|20\|21\|all}` | 내 강좌 목록 |
| `/course/view.php?id={cid}` | 강좌 페이지(활동 전체) |
| `/report/ubcompletion/user_progress.php?id={cid}` | 콘텐츠별 최대 학습 위치·진도율 |
| `/report/ubcompletion/user_progress_a.php?id={cid}` | 주차별 출석 요약 |
| `/grade/report/user/index.php?id={cid}` | 성적 |

학기 코드는 `10`=1학기, `11`=여름계절수업, `20`=2학기, `21`=겨울계절수업이다.

강좌 목록 DOM:
```html
<tbody class="my-course-lists">
  <tr><td>2022</td><td>1학기</td>
      <td><span class="badge badge-course">교과</span>
          <a href="…/course/view.php?id=208740" class="coursefullname">경제학개론 (ECO1002.03-00)</a></td></tr>
```

### 동영상

| 경로 | 내용 |
|---|---|
| `/mod/vod/viewer.php?id={cmid}` | 플레이어(실제 재생 정보 포함) |
| `/mod/vod/view.php?id={cmid}` | 래퍼 |
| `/mod/vod/action.php` | 진도 로그 POST (**아카이버는 쓰지 않음**) |
| `/mod/vod/subtitle_auto.php?uuid={uuid}&language={lang}` | **자동 생성 자막을 WEBVTT로 반환** |

뷰어에서는 다음 정보를 추출한다.
- `#my-video_html5_api[data-setup-lazy]` JSON → `sources.src` = HLS m3u8
  (호스트는 모두 `*.edge.naverncp.com`이며, URL 경로 서명으로 인증하므로 쿠키 없이 열린다.)
- `.progress(...)` 호출 인자 26개 → 진도 정책. [AUTOPLAY.md](AUTOPLAY.md) 참고
- `subtitle_auto.php?uuid=`에서 uuid, `poster=`에서 썸네일

### 활동 모듈

49개 강좌의 활동 2,045개를 전수 조사한 결과다.

| 모듈 | 개수 | 성격 | 수집 방법 |
|---|---:|---|---|
| `vod` | 697 | 동영상 강의 | 뷰어 파싱 |
| `ubfile` | 551 | 자료 파일 | view.php가 pluginfile로 직접 리다이렉트하거나 `local/ubdoc` 문서 뷰어로 이동. 뷰어는 `worker.php`의 허용 상태와 원본 URL을 확인한 뒤 다운로드 |
| `assign` | 229 | 과제 | `generaltable`의 Submission status |
| `ubboard` | 194 | 게시판·공지 | 목록 → 글(아래 참고) |
| `quiz` | 81 | 퀴즈 | `review.php?attempt=` 링크 수 |
| `feedback` | 74 | 설문형 출석 | 응답 완료 문구 |
| `folder` | 59 | 폴더 자료 | pluginfile 다중 |
| `zoom` | 45 | 실시간 강의 | 메타만 |
| `url` | 40 | 외부 링크 | 메타만 |
| `turnitintooltwo` | 26 | 표절 검사 제출 | 제출 테이블(아래) |
| `resource` | 26 | 표준 자료 | pluginfile |
| `forum` | 11 | 포럼 | 토론 목록 → 글 |
| `vpl` | 8 | 코딩 과제 | 2단계 조회(아래) |
| `lti`/`choice` | 2/2 | 외부 도구/선택 설문 | Gradescope LTI 과제는 OIDC 실행 후 공개 문항 명세를 수집한다. Yonsei-OJ 링크가 있으면 로그인 뒤 콘테스트 문제·제약·입출력·스켈레톤 코드도 합친다. `choice`는 제출 상태를 수집한다 |

Yonsei-OJ 콘테스트 참여는 계정 상태를 바꾸므로 기본값에서는 자동 실행하지 않는다.
이미 참여한 콘테스트는 로그인 쿠키 또는 `YONSEI_OJ_ID`, `YONSEI_OJ_PW`로 읽는다. 매주
새 콘테스트까지 자동 참여시켜 명세를 수집하려면 `YONSTUDY_OJ_AUTO_JOIN=1`을 명시한다.

`label`(82)은 설명 텍스트라 활동에서 제외한다.

`local/ubdoc`는 HTML 뷰어 자체를 파일로 저장하지 않는다. 같은 세션과 Referer로
`/local/ubdoc/worker.php`의 `checkState`를 호출하고 `file_download=1`인 경우에만 서버가
준 동일 출처 `file_url`을 원본 파일명으로 받는다. 다운로드 금지 응답과 외부 호스트 URL은
우회하지 않는다.

`mod/attendance`와 `mod/ubquiz`는 설치되어 있지 않다. 출석 기능은 `report/ubcompletion`이 담당한다.

### 제출형 활동

| 모듈 | 제출 판정 근거 | 주의 사항 |
|---|---|---|
| `assign` | `generaltable`의 `Submission status` | — |
| `turnitintooltwo` | 제출 테이블 행의 날짜 + `보고서 제출` | **숨김열 때문에 헤더와 데이터 위치가 어긋난다.** 위치가 아니라 값의 모양으로 찾아야 한다. 날짜 `2026/04/26`이 성적 `93/100`으로 오인되므로 날짜를 먼저 제거 |
| `vpl` | `views/downloadsubmission.php?…&submissionid=N` 존재 | **제출 화면이 JS 렌더**라 본문 텍스트로는 판정 불가. `view.php`에서 `userid`를 뽑아 `forms/submissionview.php`로 한 번 더 들어가야 한다. `&amp;` 이스케이프 주의 |
| `quiz` | `review.php?attempt=` 링크 수 | — |
| `feedback` | 응답 완료 문구 | — |
| `lti`(Gradescope) | `OnlineAssignmentSubmitter`의 공개 `outline` | LearnUs, Turnitin LTI 프록시, Gradescope OIDC POST를 순서대로 거친다. 인증 폼은 허용 호스트에만 제출한다. 답안, 명단, 사용자 ID, CSRF, 제출 URL은 저장하지 않고 제목, 문항, 배점, 안내, 입력 종류만 Markdown과 HTML로 보관한다 |

### 게시판(`ubboard`)

```
목록  /mod/ubboard/view.php?id={cmid}[&page=N]
글    /mod/ubboard/article.php?id={cmid}&bwid={글번호}
```

DOM: `table.ubboard_table` → `번호 / 제목 / 작성자 / 작성일 / 조회수`
글: `div.ubboard_view` → `.subject h3`, `.info .writer/.date/.hit`, `.content .text_to_html`

### 게시판 파싱 시 주의 사항

1. 2페이지부터 글 URL이 바뀐다. 1페이지는 `article.php?id=X&bwid=Y`, 2페이지부터는 `article.php?id=X&page=2&bwid=Y`다.
   `id=` 바로 뒤에 `bwid`가 온다고 가정하면 2페이지 이후가 통째로 0건이 된다.
2. 페이징 링크는 `&amp;page=`로 이스케이프되어 있다. `[?&]page=`로 찾으면 항상 1페이지만 반환된다.
3. 댓글 수는 제목 `<a>` 바깥의 `<span class="comment">[3]</span>`에 있다.
4. 본문 삽입 이미지는 `<img src>`에 있다. 링크 파서만 쓰면 첨부가 모두 누락된다.

### 포럼(`forum`)

```
목록      /mod/forum/view.php?id={cmid}       → discuss.php?d=N 링크들
토론      /mod/forum/discuss.php?d={토론id}   → div.forumpost 블록들
내가 쓴 글 /mod/forum/user.php?id={userid}&course={cid}
```

`forumpost` 블록은 중첩되므로 시작 위치로 잘라 써야 한다.

## 강좌 페이지 DOM

```html
<li class="activity vod modtype_vod" id="module-4333920">
  <div class="mod-indent mod-indent-1"></div>
  <div class="activityinstance">
    <a href="…/mod/vod/view.php?id=4333920">
      <span class="instancename">Lecture 1-1: Logistics<span class="accesshide"> VOD</span></span>
    </a>
    <span class="displayoptions">
      <span class="text-ubstrap">2026-03-04 00:00:00 ~ 2026-03-10 23:59:59</span>
      <span class="text-info">, 33:59</span>
    </span>
  </div>
  <img src="…/completion-auto-y" />
</li>
```

제목 마크업은 세 가지 형태로 나타난다. 이를 구분하지 않으면 `assign` 계열 제목을 제대로 읽을 수 없다.

- `vod`: 제목 뒤에 `<span class="accesshide"> VOD</span>`가 붙는다
- `assign` 등: accesshide 없이 제목만
- 제한됨(`dimmed`): `<a>` 자체가 없고 `<div class="dimmed dimmed_text">`로 감싸진다(113개)

`accesshide`를 먼저 제거한 뒤 `instancename`의 닫는 `</span>`까지 취하면 세 형태를 모두 처리할 수 있다.

### 그 밖에 확인한 사항

- `completion-(auto|manual)-(y|n)` 아이콘에서 이수 여부를 읽을 수 있다
- 학습기간이 **697개 중 94개는 아예 없다** (경제학개론은 52개 전부)
- 재생 시간은 `MM:SS`(657개)와 `HH:MM:SS`(40개) 두 형식으로 나타난다
- **394개 활동에 `(지각 : 2025-09-14 23:59:59)` 지각 인정 기한**이 정규 기간 뒤에 따로 붙는다

## HTTP 400의 원인: `ubboard_read` 쿠키

LearnUs는 "읽은 게시글" 목록을 쿠키에 모두 담는다. 글을 하나 열 때마다 약 60바이트씩 커진다.

```
시작      쿠키 8개, Cookie 헤더   382 B
10건 후   ubboard_read   610자
30건 후   ubboard_read  1814자
80건 후   Cookie 헤더  5,143 B
~130건    Apache LimitRequestFieldSize(8190) 초과 → 이후 모든 요청이 HTTP 400
```

원인을 확인하기 전까지 네 가지 가설을 검토했다.

| 가설 | 왜 그럴듯했나 | 왜 틀렸나 |
|---|---|---|
| 세션 만료 | `logout.php` 문자열이 없었다 | 세션은 멀쩡. 응답 이상을 만료로 오판 |
| 판정 로직 이중 오진 | `session_info()`도 HTTP 오류를 삼켰다 | 맞는 수정이지만 원인 아님 |
| 레이트 리밋 | 잠시 뒤 같은 주소가 열렸다 | 새 프로세스라 쿠키가 없었을 뿐 |
| 분당 요청량 상한 | 141건 직후 차단됐다 | 요청 수가 아니라 그 141건이 키운 쿠키가 문제 |

아카이버가 HTTP 400을 받는 순간 별도 프로세스로 같은 URL 4개를 열었더니 모두 HTTP 200을 반환했다.
서버 전체의 차단이 아니라 아카이버 프로세스의 클라이언트 상태가 원인이었다.

`ubboard_read` 같은 추적용 쿠키는 매 요청 후 버리고 세션 필수 쿠키만 남기도록 수정했다.
헤더가 4KB를 넘으면 비필수 쿠키를 큰 것부터 정리한다.
200건 연속 요청은 200/200 성공했고 헤더는 382 B로 유지됐다. 이후 25개 강좌 재수집에서도 실패와 재시도가 없었다.

## Coursemos 기능 지도(AMD 번들에서 추출)

| 네임스페이스 | 모듈 |
|---|---|
| `mod_*` | vod, assign, quiz, ubboard, ubfile, ubchat, **ubpeer**(동료평가), econtents(**progress** 보유), feedback, choice, survey, workshop, zoom, lti, laby |
| `local_*` | **ubattendance**(auto/offline/online), ubion(assign·classum·finalGrade·setting·user), ubonline, ubpoint, ubmessage, ubnotification, ubsubscription, ubassistant, ubgradecategory, ubcombine, ubeclass, timetable, **learning_analytics** |
| `report_*` | **ubcompletion**(진도), **ubstatistics**, progress, competency, insights |

쓰기/조회 API는 `action.php` 규약: `/mod/vod/action.php`, `/mod/econtents/action.php`,
`/mod/ubboard/ajax.php`, `/local/ubion/user/action.php`, `/local/ubion/classum/action.php`,
`/local/ubonline/action.php`, `/local/ubnotification/action.php`, `/local/ubcombine/action.php`.

> 아카이버는 이 중 **읽기 경로만** 쓴다. `action.php` 계열 쓰기 호출은 코드에 없다.
