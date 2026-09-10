# 자동 수강: 진도 처리 방식 분석과 구현

> 자동 재생으로 수강 기록을 미리 만드는 행위는 학칙상 부정 수강으로
> 해석될 수 있으며, 규정 위반의 책임은 계정 소유자에게 있습니다.
> 이 구현은 영상을 실제로 재생해 LMS가 기록하는 방식만 다룹니다.
> 진도 로그를 직접 POST로 위조하거나 재생 없이 시청 시간을 만드는 경로는 넣지 않았습니다.

## 진도가 어떻게 기록되는가

`mod_vod/vod` AMD 모듈 원본(`/lib/requirejs.php/…/mod_vod/vod.js`)을 받아 읽었다.
진도 갱신은 `timeupdate` 이벤트와 `ajax()` 호출로 처리한다.

```js
k.on("timeupdate", function(){
    var b = k.playbackRate();
    if(!s && b>1)      { k.playbackRate(1); alert("배속 기능은 이용하실 수 없습니다."); }
    else if(s && b>v)  { k.playbackRate(v); alert("{v} 배속 까지만 이용 가능합니다."); }
    l.previous = l.current;
    l.current  = k.currentTime();
    if (1 != k.paused() && l.current >= l.max) l.max = Math.floor(l.current);   // 최대 시청 위치 갱신
})

c.ajax = function(state, from, to){
    if (c.isProgress && c.cmid>0 && c.isLogin && c.isProgressPeriodCheck) {
        $.post("/mod/vod/action.php", {
            courseid, cmid, type:"vod_log", track, attempt,
            state, positionfrom:from, positionto:to, logtime
        })
    }
}
```

### 2배속 진도 인정 방식

진도는 **`video.currentTime`의 최댓값(`l.max`)** 하나로 결정되고,
서버로 가는 페이로드에 **실제 경과 시간(wall-clock time)이 들어가지 않는다.**

당초 설계에서는 "2배속이 절반만 인정될 수 있으니 A/B 파일럿이 필요하다"고 봤는데,
코드에서 진도 계산 방식을 확인해 이 파일럿은 진행하지 않았다.

### 같은 코드에서 확인한 제약

| 신호 | 의미 |
|---|---|
| `isProgressPeriodCheck` (`progress_period`) | `false`면 `ajax()` 자체가 no-op이다. 진도 처리 기간이 아니면 재생해도 기록되지 않는다 |
| `rate_allowed` | false면 배속 자체 금지 |
| `rate_max` | 서버가 정한 최대 배속(실측 603편이 2.0) |
| `swf_url` | **falsy일 때만** seek 제한 코드가 설치된다 |
| `checker_url` | 외부 `learningChecker-v2` 모듈이 별도 로드된다 |
| `interval_ms` | 주기 로그 간격(실측 60000) |
| state 코드 | `1`=진입, `3`=재생, `2`=일시정지/이동, `10`=종료, `99`=창닫기 |

## `progress()` 인자 매핑

뷰어 페이지의 `.progress(...)` 호출은 인자 26개를 위치로 넘긴다.
번들의 `c.progress=function(d,e,f,g,h,i,j,k,l,m,n,o,p,q,r,s,t,u,v,w,x,y,z,A,B,C)`와 일대일로 대응한다.

| # | 이름 | 의미 |
|---:|---|---|
| 1 | `vod_tag_id` | `"my-video"` |
| 2 | `is_progress` | 진도 집계 대상 여부 |
| 3 | `f_flag` | 0이면 이어보기/이동제한 분기 활성 |
| 6 | **`progress_period`** | **진도 처리 기간 여부** |
| 7 | `courseid` | |
| 8 | `cmid` | |
| 9 | `trackid` | |
| 10 | `attempt` | |
| 11 | **`max_position`** | **지금까지의 최대 시청 위치(초)** |
| 13 | `interval_ms` | 주기 로그 간격 |
| 15 | `swf_url` | falsy일 때만 seek 제한 설치 |
| 16 | `rate_allowed` | 배속 허용 |
| 19 | **`rate_max`** | **최대 허용 배속** |
| 21 | `checker_url` | 외부 학습 체커 경로 |
| 24 | `skip_offset` | `seek` 보정(실측 8초) |

파싱은 [`parse.py`](../yonstudy/parse.py)의 `_PROGRESS_PARAMS`.

## 헤드리스 구현 시 주의 사항

뷰어 코드에서 확인한 제한을 구현에 반영했다.

1. **브라우저의 H.264/AAC 지원을 실행 시 확인해야 한다.**
   LearnUs HLS는 H.264/AAC다. NAS 컨테이너는 시스템 Chrome을 설치하고
   `YONSTUDY_BROWSER_CHANNEL=chrome`을 기본값으로 사용한다. 이미지 빌드와 배포 직후
   `deploy/check_browser.py`가 브라우저 실행 및 코덱 지원을 검사한다. 로컬 환경은 이
   변수를 비워 두면 Playwright Chromium을 사용할 수 있지만 코덱 지원을 따로 확인해야 한다.
2. **개발자 도구 탐지 트랩이 있다.**
   ```js
   console.log(Object.defineProperties(new Error, { message: { get(){ F.submit() } } }))
   ```
   `message` getter가 읽히면 로그아웃 폼이 제출된다.
   따라서 Playwright의 `page.on("console")`을 구독하면 안 된다.
3. **`beforeunload`에서 확인창을 띄운다.** `page.on("dialog", d => d.accept())`가 필요하다.
4. 우클릭·F12·Ctrl+Shift+I 차단 코드가 있다(재생 자체에는 무해).
5. `--headless=new`는 `visibilityState`를 `visible`로 보고하므로 탭 포커스 체크는 통과한다.
   Xvfb까지 갈 필요 없다.

## 스케줄러

`build_plan(store)`는 다음 두 조건을 모두 만족하는 항목을 고른다.

1. 진도율 100%와 최대 학습 위치 완주 조건을 모두 충족하지 않음
2. 뷰어에서 읽은 `progress_period`가 참이고 강좌 페이지의 **정상 수강 기간** 안에 있음

지각 기한은 리포트에 표시하지만 기본 자동 재생에는 포함하지 않는다.

우선순위: `긴급`(D-1) → `임박`(D-3) → `여유` → `상시`, 같은 등급이면 마감이 이른 순.

실측 검증은 2026-03-08 시점을 재현해 진행했다.

```
[임박] Lecture 1-2: Overview 1  남은 45분  마감 03-10 23:59  2.0x → 25분
[임박] Lecture 1-1: Logistics   남은 33분  마감 03-10 23:59  2.0x → 18분
[임박] Lecture 1-3: Overview 2  남은 32분  마감 03-10 23:59  2.0x → 18분
합계 61분 (원본 110분)
예약(10일 내 열림) 2편
```

`upcoming(store, days=14)`은 아직 안 열린 영상을 열리는 날짜와 함께 예약해 둔다.

## 재생 워커

- Playwright Chromium의 격리 컨텍스트, `--mute-audio`(H.264/AAC 실행 전 확인)
- 한 번에 한 편만 재생(병렬 재생은 서버 로그상 비정상)
- 한 큐에서 여러 편을 처리하면 두 번째 영상부터 시작 전에 1~10분 무작위 대기
- 15초마다 `currentTime` 확인 → 45초간 정체하면 `play()` 재시도, 3회 실패 시 다음으로
- 종료 후 3초 대기(`state 10` 로그가 올라갈 시간)
- 끝나면 진도 리포트를 다시 읽어 100% 또는 완료 체크를 자동 검증

## 실제 서버 검증

2026-09-01 인공지능개론및응용 두 편을 2배속으로 실제 재생했다.

- Week 1 -1: 56:40 재생 후 진도 100%, 완료 체크 `y`
- Week 1-2: 47:14 재생 후 진도 100%, 완료 체크 `y`

예약 재생은 매일 새벽 02:30·04:00·05:30 KST 기준 최대 45분 지터를 두고
한 번에 한 편씩 실행한다. systemd 타이머와 Docker cron 실행 래퍼에 같은 범위를 적용한다.
Docker에서는 `YONSTUDY_WATCH_JITTER_SECONDS`로 범위를 조정할 수 있다.
02:00 읽기 전용 모니터가 새 영상을 먼저 동기화하며, 낮 시간에
누락분이 실행되지 않도록 `persistent catch-up`은 사용하지 않는다. `watch_state.json`에 마지막
실행과 검증 결과를 남긴다.

`is_progress=false`라 진도율·완료 체크가 없는 영상도 공개 후 실제로 끝까지 한 번 재생한다.
이 경우 LearnUs 진도 대신 로컬 `crawl_log`의 `playback_once` 성공 기록으로 완주를 확인하며,
성공 기록이 있는 영상은 다음 계획에서 제외한다.
