# haniel

부활시키는 천사. 설정 기반의 극단적으로 무지한 서비스 러너.

## 철학

haniel은 자기가 무엇을 돌리는지 모른다.
git 리포를 체크하고, 변경이 있으면 pull 받고, 명시된 명령어로 프로세스를 기동한다.
그것이 슬랙봇이든 MCP 서버든 웹 대시보드든, haniel에게는 전부 "프로세스"일 뿐이다.

haniel이 아는 것:
- 설정 파일(YAML)에 적힌 내용
- git fetch/pull 결과
- 프로세스가 살아있는지 여부

haniel이 모르는 것:
- .env의 내용과 의미 — **읽지도, 로드하지도, 자식에게 주입하지도 않는다**
- .mcp.json의 내용 — 설치 시 생성만 하고, 런타임에 건드리지 않는다
- 프로세스가 무슨 일을 하는지
- 프로세스 간의 비즈니스 의존성
- 포트 번호의 의미
- 호스트 시스템의 나머지 구성

프로세스가 필요로 하는 환경변수, 설정 파일, 의존성은 **프로세스 자신이** 책임진다.
haniel은 명령어를 실행할 뿐이다.

## 네 가지 모드

### `haniel install` — 설치 모드

빈 PC에서 전체 실행 환경을 구축한다.
haniel이 기계적으로 처리할 수 있는 것(디렉토리, git clone, venv, npm)은 자기가 하고,
사람과의 대화가 필요한 것(시크릿 입력, 설정 선택)은 **Claude Code에게 위임**한다.

설치 흐름:
1. haniel이 자기 자신을 MCP 서버로 기동
2. 기계적 단계(디렉토리, clone, venv)를 먼저 처리
3. 대화형 단계가 필요하면 Claude Code를 호출하여 사용자와 대화
4. Claude Code가 haniel MCP 도구로 설정 값을 전달
5. haniel이 남은 단계(파일 생성, 서비스 등록) 완료

설치가 끝나면 `haniel run`으로 서비스를 기동할 수 있는 상태가 된다.

**재개 지원**: 설치가 중단된 경우 `haniel install`을 다시 실행하면 "처음부터 / 이어서" 선택 가능.

### `haniel run` — 런타임 모드

haniel.yaml의 `repos`와 `services` 섹션만 사용한다.
git poll → pull → 프로세스 재기동. 이것이 전부다.
.env, .mcp.json, venv 등은 일절 건드리지 않는다.

### `haniel status` — 상태 조회

현재 서비스 상태, 리포 상태, 마지막 poll 시간 등을 출력한다.
MCP 서버가 떠있으면 Claude Code에서도 조회 가능.

### `haniel validate` — 설정 검증

haniel.yaml의 유효성을 검사한다:
- YAML 문법 검사
- 필수 필드 존재 여부
- 순환 의존성 검사 (`after` 필드)
- 포트 충돌 검사 (`ready: port:*` 간 중복)
- 리포 경로 중복 검사

실행 전 사전 검증용으로 사용.

## 설정 파일 (`haniel.yaml`)

### 전체 구조

```yaml
poll_interval: 60

shutdown:
  timeout: 10
  kill_timeout: 30

backoff:
  base_delay: 5
  max_delay: 300
  circuit_breaker: 5
  circuit_window: 300

webhooks:
  - url: https://hooks.slack.com/services/T.../B.../...
    format: slack
  - url: https://discord.com/api/webhooks/...
    format: discord

mcp:
  enabled: true
  transport: sse
  port: 3200

repos:
  seosoyoung:
    url: git@github.com:org/seosoyoung.git
    branch: main
    path: ./.projects/seosoyoung

  soulstream:
    url: git@github.com:org/soulstream.git
    branch: main
    path: ./.projects/soulstream

  eb-lore:
    url: git@github.com:org/eb_lore.git
    branch: main
    path: ./.projects/eb_lore

services:
  mcp-seosoyoung:
    run: ./runtime/mcp_venv/Scripts/python.exe -X utf8 -m seosoyoung.mcp --transport=sse --port=3104
    cwd: ./workspace
    repo: seosoyoung
    ready: port:3104
    restart_delay: 3
    hooks:
      post_pull: ./runtime/mcp_venv/Scripts/pip.exe install -r ./runtime/mcp_requirements.txt

  bot:
    run: ./runtime/venv/Scripts/python.exe -X utf8 -m seosoyoung.slackbot
    cwd: ./workspace
    repo: seosoyoung
    after: mcp-seosoyoung
    shutdown:
      signal: SIGTERM
      timeout: 15
    hooks:
      post_pull: ./runtime/venv/Scripts/pip.exe install -r ./runtime/requirements.txt

  # ... (이하 services 생략, 뒤에 전체 예시 있음)

install:
  requirements:
    python: ">=3.11"
    node: ">=18"
    nssm: true
    claude-code: true

  directories:
    - ./runtime
    - ./runtime/logs
    - ./runtime/data
    - ./workspace
    - ./workspace/.local
    - ./workspace/.local/artifacts
    - ./workspace/.local/incoming
    - ./workspace/.local/tmp

  environments:
    main-venv:
      type: python-venv
      path: ./runtime/venv
      requirements:
        - ./runtime/requirements.txt

    mcp-venv:
      type: python-venv
      path: ./runtime/mcp_venv
      requirements:
        - ./runtime/mcp_requirements.txt

    soulstream-venv:
      type: python-venv
      path: ./soulstream_runtime/venv
      requirements:
        - ./soulstream_runtime/requirements.txt

    runtime-node:
      type: npm
      path: ./runtime

  configs:
    workspace-env:
      path: ./workspace/.env
      keys:
        # Slack
        - key: SLACK_BOT_TOKEN
          prompt: "Slack Bot Token (xoxb-...)"
        - key: SLACK_APP_TOKEN
          prompt: "Slack App Token (xapp-...)"
        - key: SLACK_MCP_XOXC_TOKEN
          prompt: "Slack MCP XOXC Token"
        - key: SLACK_MCP_XOXD_TOKEN
          prompt: "Slack MCP XOXD Token"
        - key: ALLOWED_USERS
          prompt: "허용 사용자 ID (쉼표 구분)"
        - key: NOTIFY_CHANNEL
          prompt: "알림 채널 ID"
        # API Keys
        - key: GEMINI_API_KEY
          prompt: "Gemini API Key"
        - key: OUTLINE_API_KEY
          prompt: "Outline API Key"
        - key: OUTLINE_API_URL
          prompt: "Outline API URL"
        - key: TRELLO_API_KEY
          prompt: "Trello API Key"
        - key: TRELLO_TOKEN
          prompt: "Trello Token"
        # Paths
        - key: LOG_PATH
          default: "{root}/runtime/logs"
        - key: SESSION_PATH
          default: "{root}/runtime/sessions"
        - key: MEMORY_PATH
          default: "{root}/runtime/memory"
        # Flags
        - key: DEBUG
          default: "false"

    workspace-mcp:
      path: ./workspace/.mcp.json
      content: |
        {
          "mcpServers": {
            "seosoyoung-mcp": {
              "url": "http://localhost:3104/sse"
            },
            "haniel": {
              "url": "http://localhost:3200/sse"
            },
            "slack": {
              "url": "http://localhost:3101/sse"
            },
            "trello": {
              "url": "http://localhost:3102/sse"
            },
            "outline": {
              "url": "http://localhost:3103/sse"
            }
          }
        }

    webhook-config:
      path: ./runtime/data/watchdog_config.json
      keys:
        - key: slackWebhookUrl
          prompt: "Slack Webhook URL (알림 발송용)"

  service:
    name: haniel
    display: "Haniel Service Runner"
    working_directory: "{root}"
    environment:
      PYTHONUTF8: "1"
```

## `install` 섹션 상세

### 핵심 설계: Claude Code 위임

설치 과정에는 두 종류의 작업이 있다.

**기계적 작업** — haniel이 직접 수행:
- 시스템 요구사항 검증
- 디렉토리 생성
- git clone
- venv 생성, pip install
- npm install
- 정적 파일 생성 (.mcp.json 등)
- NSSM 서비스 등록

**대화형 작업** — Claude Code에게 위임:
- 시크릿 수집 (Slack 토큰, API 키 등)
- 발급 방법 안내 ("이 URL에서 Bot Token을 발급받으세요")
- 설정 선택 ("알림을 받을 채널을 선택해주세요")
- 문제 해결 ("git clone이 실패했습니다. SSH 키가 설정되어 있나요?")
- 설정 값 검증 (`validate` 필드가 있는 경우)

haniel은 설치를 위한 CLI UI를 만들 필요가 없다.
사람과의 대화는 Claude Code가 담당하고,
haniel은 MCP 도구를 통해 값을 받아 파일에 쓸 뿐이다.

### 설치 전체 흐름

```
haniel install haniel.yaml
  │
  ├─ 이전 설치 상태 확인
  │     install.state 파일 존재?
  │     ├─ 존재하고 incomplete → "이어서 / 처음부터" 선택 프롬프트
  │     └─ 없거나 complete → 새로 시작
  │
  ├─ Phase 0: 부트스트랩
  │     Claude Code가 설치되어 있는가?
  │     ├─ 없으면: Claude Code 설치 안내 출력 후 중단
  │     └─ 있으면: 계속
  │
  ├─ Phase 1: 기계적 설치 (haniel 단독)
  │     haniel.yaml 로드
  │     requirements 검증 (python, node, nssm)
  │       미충족 항목 → 목록을 기록해두고 Phase 2에서 Claude Code가 안내
  │     directories 생성
  │     repos clone (가능한 것만)
  │     environments 구성 (venv, npm)
  │     정적 configs 생성 (content 방식)
  │     ↓
  │     기계적 설치 결과를 JSON으로 정리:
  │       - 성공한 단계
  │       - 실패한 단계와 에러 메시지
  │       - 대화형 입력이 필요한 configs 목록
  │     install.state 파일에 상태 저장
  │
  ├─ Phase 2: 대화형 설치 (Claude Code 위임)
  │     haniel이 자신을 MCP 서버로 기동 (install 전용 모드)
  │     Claude Code 세션을 시작:
  │       claude -p --mcp-config haniel-install-mcp.json \
  │         "haniel 설치를 이어서 진행해주세요. (설치 상태 JSON 전달)"
  │     ↓
  │     Claude Code가 사용자와 대화하면서:
  │       - 실패한 단계 해결 방법 안내
  │       - 시크릿/설정 값을 수집
  │       - validate 필드가 있으면 검증 수행 후 결과 안내
  │       - haniel MCP 도구로 값 전달:
  │           haniel_set_config(file="workspace-env", key="SLACK_BOT_TOKEN", value="xoxb-...")
  │           haniel_set_config(file="webhook-config", key="slackWebhookUrl", value="https://...")
  │       - 모든 값이 채워지면:
  │           haniel_finalize_install()
  │     ↓
  │     Claude Code 세션 종료 (finalize 호출 시 자동 종료)
  │
  ├─ Phase 3: 마무리 (haniel 단독)
  │     finalize 신호를 받으면:
  │       대화형으로 수집된 값으로 config 파일 생성
  │       NSSM 서비스 등록
  │       MCP 서버 중지 (install 모드 종료)
  │       install.state를 complete로 표시
  │
  └─ 완료
      "haniel install 완료. 'nssm start haniel'로 서비스를 시작하세요."
```

### 설치 전용 MCP 도구

install 모드에서만 활성화되는 MCP 도구. Claude Code가 이 도구를 사용해 설치를 진행한다.

```
haniel_install_status()
  → 현재 설치 진행 상황 반환
    {
      "phase": "interactive",
      "completed": ["directories", "repos", "environments"],
      "failed": [{"step": "requirements", "detail": "nssm not found"}],
      "pending_configs": [
        {
          "name": "workspace-env",
          "path": "./workspace/.env",
          "missing_keys": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", ...],
          "filled_keys": ["LOG_PATH", "DEBUG", ...]
        }
      ]
    }

haniel_set_config(config_name, key, value)
  → 특정 config의 키에 값을 설정
  → 예: haniel_set_config("workspace-env", "SLACK_BOT_TOKEN", "xoxb-1234...")

haniel_get_config(config_name)
  → 특정 config의 현재 상태 (채워진 키, 미입력 키) 반환

haniel_retry_step(step_name)
  → 실패한 설치 단계 재시도
  → 예: haniel_retry_step("requirements") — 사용자가 nssm을 설치한 후 재검증

haniel_finalize_install()
  → 모든 필수 값이 채워졌는지 확인
  → config 파일 생성, NSSM 등록, install 모드 종료
  → Claude Code 세션 종료 신호 전송
```

### Claude Code에게 전달하는 지침

haniel이 Claude Code를 호출할 때 다음 지침을 프롬프트로 전달한다:

```
당신은 haniel 설치 도우미입니다.
사용자와 대화하면서 서비스 실행에 필요한 설정 값을 수집해주세요.

haniel MCP 도구를 사용하여:
1. haniel_install_status()로 현재 상태를 확인하세요
2. 실패한 단계가 있으면 사용자에게 해결 방법을 안내하세요
3. missing_keys의 각 항목에 대해:
   - 값의 용도와 발급/확인 방법을 안내하세요 (guide 필드 참고)
   - 사용자에게 값을 물어보세요
   - validate 필드가 있으면 해당 명령어로 값을 검증하고 결과를 사용자에게 안내하세요
   - 검증 통과 시 haniel_set_config()으로 값을 설정하세요
4. 모든 값이 채워지면 haniel_finalize_install()을 호출하세요
   이 호출이 성공하면 세션이 자동으로 종료됩니다.

각 config의 keys에는 prompt 필드가 있어 사용자에게 어떻게 물어볼지 힌트가 됩니다.
default가 있는 키는 사용자에게 "기본값 {default}을 사용할까요?"라고 물어보세요.
validate가 있는 키는 값 입력 후 검증을 수행하고, 실패하면 재입력을 요청하세요.
```

### `requirements` — 시스템 요구사항

```yaml
install:
  requirements:
    python: ">=3.11"
    node: ">=18"
    nssm: true
    claude-code: true     # Phase 0에서 사전 검증 (필수 전제)
```

claude-code는 Phase 0에서 별도 검증한다 (없으면 설치 불가).
나머지는 Phase 1에서 검증하되, 실패해도 중단하지 않고 기록만 해둔다.
Phase 2에서 Claude Code가 사용자에게 해결 방법을 안내한다.

### `directories` — 디렉토리 생성

```yaml
install:
  directories:
    - ./runtime
    - ./runtime/logs
    - ./workspace
```

없는 디렉토리를 생성한다. 이미 있으면 건너뛴다.
Phase 1에서 기계적으로 처리.

### `environments` — 실행 환경 구성

```yaml
install:
  environments:
    {name}:
      type: python-venv | npm
      path: {directory}
      requirements:
        - {requirements.txt path}
```

Phase 1에서 기계적으로 처리.
실패하면 기록하고 Phase 2에서 Claude Code가 트러블슈팅.

### `configs` — 설정 파일 정의

두 가지 방식:

**정적 (`content`)** — Phase 1에서 자동 생성:
```yaml
configs:
  workspace-mcp:
    path: ./workspace/.mcp.json
    content: |
      { "mcpServers": { ... } }
```

**대화형 (`keys`)** — Phase 2에서 Claude Code가 수집:
```yaml
configs:
  workspace-env:
    path: ./workspace/.env
    keys:
      - key: SLACK_BOT_TOKEN
        prompt: "Slack Bot Token (xoxb-...)"
        guide: "https://api.slack.com/apps 에서 Bot User OAuth Token을 복사하세요"
        validate: "curl -s -H 'Authorization: Bearer {value}' https://slack.com/api/auth.test | jq -e '.ok'"
      - key: LOG_PATH
        default: "{root}/runtime/logs"
```

- `prompt`: Claude Code가 사용자에게 물어볼 때 참고하는 설명
- `guide`: 값을 발급/확인하는 방법 URL 또는 설명. Claude Code가 사용자에게 안내
- `validate`: 값 검증 명령어. `{value}`가 입력값으로 치환됨. Claude Code가 이 명령을 실행하고 결과를 사용자에게 안내
- `default`: 기본값. `{root}`는 haniel.yaml 위치의 절대 경로로 치환

파일이 이미 존재하면 기존 값을 유지하고, 새로 추가된 키만 missing_keys로 표시.

### `service` — NSSM 서비스 등록

```yaml
install:
  service:
    name: haniel
    display: "Haniel Service Runner"
    working_directory: "{root}"
    environment:
      PYTHONUTF8: "1"
```

Phase 3(finalize)에서 `nssm install`로 등록.

## `repos` 섹션

리포지토리를 정의한다. 설치 시 자동 clone, 런타임 시 자동 poll.

```yaml
repos:
  {name}:
    url: {git clone URL}           # 필수. clone 및 fetch 대상
    branch: {branch name}          # 필수. 추적할 브랜치
    path: {local path}             # 필수. 로컬 경로 (haniel.yaml 기준 상대 경로)
```

**자동 clone 흐름 (install 시):**
1. `path`가 존재하는지 확인
2. 없으면 `git clone --branch {branch} {url} {path}`
3. 있으면 `.git`이 유효한지, remote URL이 일치하는지 확인
4. 불일치하면 에러 (자동 수정하지 않음)

**런타임 poll:**
1. `git fetch origin {branch}`
2. `local_head != remote_head`이면 변경점 있음

## `services` 섹션

서비스를 정의한다. **YAML에 적힌 순서가 기동 순서**다.

```yaml
services:
  {name}:
    run: {command}                 # 필수. 실행 명령어
    cwd: {directory}               # 선택. 작업 디렉토리 (기본: haniel.yaml 위치)
    repo: {repo name}              # 선택. 이 리포에 변경이 있으면 재기동
    restart_delay: {seconds}       # 선택. 크래시 후 재시작 대기 (기본: backoff 적용)
    after: {service name}          # 선택. 이 서비스가 ready될 때까지 기동 대기
    ready: {condition}             # 선택. "준비됨" 판단 조건
    shutdown:                      # 선택. 종료 방식
      signal: SIGTERM              #   기본: SIGTERM
      timeout: 15                  #   graceful 대기 (기본: 전역 shutdown.timeout)
      method: http                 #   선택. http면 HTTP 엔드포인트로 종료 요청
      endpoint: /shutdown          #   method: http일 때 사용
    enabled: true                  # 선택. false면 건너뜀 (기본: true)
    hooks:                         # 선택. 라이프사이클 훅
      post_pull: {command}         #   git pull 후 실행
```

haniel은 `run` 명령어를 실행할 뿐이다.
프로세스가 필요로 하는 .env, 설정 파일, 환경변수는 프로세스가 자기 책임으로 로드한다.
haniel은 프로세스의 환경에 아무것도 주입하지 않는다.

### `ready` 조건 문법

```yaml
ready: port:{port}                # 해당 포트가 LISTEN 상태가 되면 ready
ready: delay:{seconds}            # 지정 시간 경과 후 ready
ready: log:{pattern}              # stdout/stderr에 패턴이 출력되면 ready
ready: http:{url}                 # GET 요청에 2xx 응답이 오면 ready
```

`ready`가 없고 `after`로 참조되는 경우, `delay:3`을 기본값으로 사용한다.

### `after` 기동 순서 문법

```yaml
# 단일 의존
after: mcp-seosoyoung

# 다중 의존 (모두 ready가 되어야 기동)
after:
  - mcp-seosoyoung
  - mcp-slack
```

`after`가 없는 서비스들은 YAML 순서대로 즉시 기동한다.

### `hooks` 라이프사이클 훅

```yaml
hooks:
  post_pull: {command}            # git pull 후 실행 (빌드, 의존성 설치 등)
```

haniel은 hook이 뭘 하는지 모른다.
0이 아닌 종료 코드가 나오면 웹훅으로 알리고, 서비스 기동은 계속 진행한다.

## 로그 처리

haniel은 각 서비스의 stdout/stderr를 캡처하여 서비스별 로그 파일로 저장한다.

```
{haniel.yaml 위치}/logs/
├── mcp-seosoyoung.log
├── mcp-slack.log
├── bot.log
└── ...
```

- 각 서비스가 자체적으로 로그 파일을 생성하는 것은 별개
- haniel이 캡처하는 건 stdout/stderr만
- `ready: log:{pattern}` 기능이 이 캡처된 로그를 사용
- 로그 로테이션은 향후 검토 (외부 도구 vs 내장)

## 런타임 동작 사이클

### 기동

```
haniel run
  ├─ haniel.yaml 로드
  ├─ repos 확인:
  │    경로 존재? → OK
  │    경로 없음? → git clone (install 누락 복구)
  │    clone 실패? → 웹훅 알림, 해당 repo 의존 서비스 skip
  ├─ services 순차 기동:
  │    YAML 순서대로:
  │      enabled == false? → skip
  │      after 있음? → 대상 서비스의 ready 조건 대기
  │      run 명령어로 프로세스 생성
  │      stdout/stderr 캡처 시작
  │      ready 조건 있으면 충족 대기 (타임아웃 시 경고, 기동은 계속)
  │      웹훅: "서비스 기동됨"
  └─ poll 루프 진입
```

### poll 루프

```
매 poll_interval마다:

  [Phase 1: 변경점 감지]
  각 repo에 대해:
    git fetch origin {branch}
    local_head vs remote_head
    변경 있음? → 변경 목록에 추가

  [Phase 2: 변경 반영]
  변경된 repo가 있으면:
    해당 repo에 의존하는 서비스 목록 산출

    a. 웹훅: "변경점 감지됨 — {repo}: {commits}"
    b. 의존 서비스들을 역순으로 graceful shutdown
       SIGTERM → timeout 대기 → SIGKILL
       대기가 길어지면 웹훅 알림
    c. 웹훅: "변경점 반영 중"
    d. git pull
    e. post_pull hook 실행 (있으면)
    f. 의존 서비스들을 정순으로 재기동
    g. 웹훅: "기동 완료"

  [Phase 3: 헬스체크]
  각 서비스에 대해:
    프로세스 살아있는가?

    살아있음 → pass
    죽어있음 →
      circuit breaker 확인:
        circuit_window 내 circuit_breaker 이상 → 기동 중단 + 웹훅
        아니면 → backoff 후 재기동 + 웹훅
```

### graceful shutdown

```
서비스 종료 요청:
  shutdown.method == http?
    → shutdown.endpoint로 HTTP 요청 전송
    → 응답 대기
  아니면:
    → shutdown.signal 전송 (기본 SIGTERM)

  shutdown.timeout만큼 대기
    ├─ timeout 내 종료 → 완료
    └─ timeout 초과 →
         웹훅: "graceful 종료 실패, 강제 종료"
         kill_timeout까지 SIGKILL
         ├─ 종료됨 → 완료
         └─ 안 죽음 → 웹훅: "강제 종료 실패, 수동 개입 필요"
```

## MCP 서버

haniel은 MCP 서버로도 동작하여 Claude Code가 현황 조회 및 제어를 할 수 있다.

### 리소스 (읽기)

```
haniel://status                    → 전체 서비스 상태 목록
haniel://status/{service}          → 특정 서비스 상태
haniel://repos                     → 리포 상태 (HEAD, 마지막 fetch 등)
haniel://logs/{service}?lines=50   → 최근 로그
```

### 도구 (쓰기)

```
haniel_restart(service)            → 특정 서비스 재기동
haniel_stop(service)               → 특정 서비스 중지
haniel_start(service)              → 특정 서비스 시작
haniel_pull(repo)                  → 특정 리포 즉시 pull + 의존 서비스 재기동
haniel_enable(service)             → circuit breaker로 중단된 서비스 재활성화
haniel_reload()                    → haniel.yaml 재로드 (프로세스 유지, 설정만 갱신)
```

## dry-run 모드

실제 실행 없이 계획만 확인:

```bash
haniel install --dry-run haniel.yaml
```

출력 예시:
```
[dry-run] Phase 1: 기계적 설치
  - requirements 검증: python >=3.11, node >=18, nssm, claude-code
  - directories 생성: ./runtime, ./runtime/logs, ./workspace, ...
  - repos clone: seosoyoung → ./.projects/seosoyoung
  - environments: main-venv (python-venv), runtime-node (npm)
  - configs (정적): workspace-mcp → ./workspace/.mcp.json

[dry-run] Phase 2: 대화형 설치 (Claude Code)
  - configs (대화형): workspace-env → ./workspace/.env
    - 수집 필요: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, ...
    - 기본값 사용: LOG_PATH, SESSION_PATH, DEBUG, ...

[dry-run] Phase 3: 마무리
  - NSSM 서비스 등록: haniel
```

## 사례: 현재 서소영 슬랙봇 구성

### 현행 → haniel 매핑

| 현행 | haniel |
|------|--------|
| watchdog.ps1 + supervisor 이중 구조 | NSSM → haniel 단일 프로세스 |
| config.py (프로세스별 하드코딩) | haniel.yaml `services` |
| git_poller.py (3개 고정) | haniel.yaml `repos` |
| deployer.py (상태 머신) | poll 루프 Phase 2 |
| session_monitor.py | 제거 (haniel은 세션을 모름) |
| notifier.py | haniel 웹훅 모듈 |
| dashboard.py | haniel MCP 서버 |
| supervisor가 .env 로드 → 자식 상속 | **제거**. 프로세스가 자기 .env를 직접 로드 |
| venv 수동 생성 | `haniel install`이 자동 생성 |
| .mcp.json 수동 작성 | `haniel install`이 자동 생성 |

### 전체 haniel.yaml

```yaml
poll_interval: 60

shutdown:
  timeout: 10
  kill_timeout: 30

backoff:
  base_delay: 5
  max_delay: 300
  circuit_breaker: 5
  circuit_window: 300

webhooks:
  - url: https://hooks.slack.com/services/T.../B.../...
    format: slack

mcp:
  enabled: true
  transport: sse
  port: 3200

repos:
  seosoyoung:
    url: git@github.com:org/seosoyoung.git
    branch: main
    path: ./.projects/seosoyoung

  soulstream:
    url: git@github.com:org/soulstream.git
    branch: main
    path: ./.projects/soulstream

  eb-lore:
    url: git@github.com:org/eb_lore.git
    branch: main
    path: ./.projects/eb_lore

services:
  # --- MCP 서버군 (after 없음, 병렬 기동) ---
  mcp-slack:
    run: npx -y supergateway --stdio "npx -y @anthropic/mcp-slack" --port 3101
    cwd: ./runtime
    restart_delay: 3

  mcp-trello:
    run: npx -y supergateway --stdio "npx -y @anthropic/mcp-trello" --port 3102
    cwd: ./runtime
    restart_delay: 3

  mcp-outline:
    run: npx -y supergateway --stdio "npx -y @anthropic/mcp-outline" --port 3103
    cwd: ./runtime
    restart_delay: 3

  mcp-seosoyoung:
    run: ./runtime/mcp_venv/Scripts/python.exe -X utf8 -m seosoyoung.mcp --transport=sse --port=3104
    cwd: ./workspace
    repo: seosoyoung
    ready: port:3104
    restart_delay: 3
    hooks:
      post_pull: ./runtime/mcp_venv/Scripts/pip.exe install -r ./runtime/mcp_requirements.txt

  mcp-eb-lore:
    run: ./runtime/mcp_venv/Scripts/python.exe -X utf8 -m lore_mcp --transport=sse --port=3108
    cwd: ./.projects/eb_lore
    repo: eb-lore
    ready: port:3108
    restart_delay: 3

  # --- 메인 서비스 ---
  bot:
    run: ./runtime/venv/Scripts/python.exe -X utf8 -m seosoyoung.slackbot
    cwd: ./workspace
    repo: seosoyoung
    after: mcp-seosoyoung
    shutdown:
      signal: SIGTERM
      timeout: 15
    hooks:
      post_pull: ./runtime/venv/Scripts/pip.exe install -r ./runtime/requirements.txt

  rescue-bot:
    run: ./runtime/venv/Scripts/python.exe -X utf8 -m seosoyoung.rescue
    cwd: ./workspace
    repo: seosoyoung
    restart_delay: 5

  # --- Soulstream ---
  soulstream-server:
    run: ./soulstream_runtime/venv/Scripts/python.exe -X utf8 -m soulstream.server
    cwd: ./soulstream_runtime
    repo: soulstream
    ready: port:4105
    restart_delay: 3
    hooks:
      post_pull: ./soulstream_runtime/venv/Scripts/pip.exe install -r ./soulstream_runtime/requirements.txt

  soulstream-dashboard:
    run: npx vite preview --port 4109
    cwd: ./soulstream_runtime/dashboard
    repo: soulstream
    after: soulstream-server
    hooks:
      post_pull: npm --prefix ./soulstream_runtime/dashboard install && npm --prefix ./soulstream_runtime/dashboard run build

install:
  requirements:
    python: ">=3.11"
    node: ">=18"
    nssm: true
    claude-code: true

  directories:
    - ./runtime
    - ./runtime/logs
    - ./runtime/data
    - ./runtime/sessions
    - ./runtime/memory
    - ./workspace
    - ./workspace/.local
    - ./workspace/.local/artifacts
    - ./workspace/.local/incoming
    - ./workspace/.local/tmp
    - ./workspace/.local/index
    - ./soulstream_runtime

  environments:
    main-venv:
      type: python-venv
      path: ./runtime/venv
      requirements:
        - ./runtime/requirements.txt

    mcp-venv:
      type: python-venv
      path: ./runtime/mcp_venv
      requirements:
        - ./runtime/mcp_requirements.txt

    soulstream-venv:
      type: python-venv
      path: ./soulstream_runtime/venv
      requirements:
        - ./soulstream_runtime/requirements.txt

    runtime-node:
      type: npm
      path: ./runtime

  configs:
    workspace-env:
      path: ./workspace/.env
      keys:
        # Slack
        - key: SLACK_BOT_TOKEN
          prompt: "Slack Bot Token (xoxb-...)"
          guide: "https://api.slack.com/apps → 앱 선택 → OAuth & Permissions → Bot User OAuth Token"
          validate: "curl -s -H 'Authorization: Bearer {value}' https://slack.com/api/auth.test | jq -e '.ok'"
        - key: SLACK_APP_TOKEN
          prompt: "Slack App Token (xapp-...)"
          guide: "https://api.slack.com/apps → 앱 선택 → Basic Information → App-Level Tokens"
        - key: SLACK_MCP_XOXC_TOKEN
          prompt: "Slack MCP XOXC Token"
          guide: "브라우저에서 Slack 웹 앱 → DevTools → Application → Cookies → xoxc 토큰"
        - key: SLACK_MCP_XOXD_TOKEN
          prompt: "Slack MCP XOXD Token"
          guide: "브라우저에서 Slack 웹 앱 → DevTools → Application → Cookies → xoxd 토큰"
        - key: ALLOWED_USERS
          prompt: "허용 사용자 ID (쉼표 구분)"
          guide: "Slack에서 사용자 프로필 → 더보기 → Member ID 복사"
        - key: NOTIFY_CHANNEL
          prompt: "알림 채널 ID"
          guide: "Slack 채널 → 채널 이름 클릭 → 하단의 Channel ID 복사"
        # API Keys
        - key: GEMINI_API_KEY
          prompt: "Gemini API Key"
          guide: "https://aistudio.google.com/apikey"
        - key: OUTLINE_API_KEY
          prompt: "Outline API Key"
          guide: "Outline → Settings → API → Create API Key"
        - key: OUTLINE_API_URL
          prompt: "Outline API URL"
          guide: "Outline 인스턴스의 URL (예: https://wiki.example.com)"
        - key: TRELLO_API_KEY
          prompt: "Trello API Key"
          guide: "https://trello.com/power-ups/admin → API Key"
        - key: TRELLO_TOKEN
          prompt: "Trello Token"
          guide: "API Key 발급 페이지에서 Token 생성 링크 클릭"
        - key: SLACK_MCP_ADD_MESSAGE_TOOL
          default: "false"
        # Paths — default 있으므로 Claude Code가 "기본값 사용할까요?"로 처리
        - key: LOG_PATH
          default: "{root}/runtime/logs"
        - key: SESSION_PATH
          default: "{root}/runtime/sessions"
        - key: MEMORY_PATH
          default: "{root}/runtime/memory"
        # Flags
        - key: DEBUG
          default: "false"
        - key: TRANSLATE_ENABLED
          default: "false"
        - key: RECALL_ENABLED
          default: "false"
        - key: OM_ENABLED
          default: "false"

    workspace-mcp:
      path: ./workspace/.mcp.json
      content: |
        {
          "mcpServers": {
            "seosoyoung-mcp": {
              "url": "http://localhost:3104/sse"
            },
            "haniel": {
              "url": "http://localhost:3200/sse"
            },
            "eb-lore": {
              "url": "http://localhost:3108/sse"
            },
            "slack": {
              "url": "http://localhost:3101/sse"
            },
            "trello": {
              "url": "http://localhost:3102/sse"
            },
            "outline": {
              "url": "http://localhost:3103/sse"
            }
          }
        }

    webhook-config:
      path: ./runtime/data/watchdog_config.json
      keys:
        - key: slackWebhookUrl
          prompt: "Slack Webhook URL (알림 발송용)"
          guide: "https://api.slack.com/apps → 앱 선택 → Incoming Webhooks → Webhook URL 복사"

  service:
    name: haniel
    display: "Haniel Service Runner"
    working_directory: "{root}"
    environment:
      PYTHONUTF8: "1"
```

## Sanity Check: 서소영 슬랙봇 구성

### 1. 설치 과정에서 .env를 올바르게 구성할 수 있는가?

Phase 1(기계적)에서는 .env를 건드리지 않는다.
Phase 2(Claude Code 위임)에서 Claude Code가 사용자와 대화하며 값을 수집한다.

| 키 | 수집 방식 | guide 제공 | validate |
|---|---|---|---|
| SLACK_BOT_TOKEN | Claude Code가 사용자에게 요청 | O (발급 URL) | O (API 호출 검증) |
| SLACK_APP_TOKEN | Claude Code가 사용자에게 요청 | O | - |
| SLACK_MCP_XOXC_TOKEN | Claude Code가 사용자에게 요청 | O (브라우저 쿠키 추출 방법) | - |
| SLACK_MCP_XOXD_TOKEN | Claude Code가 사용자에게 요청 | O | - |
| ALLOWED_USERS | Claude Code가 사용자에게 요청 | O (Member ID 확인 방법) | - |
| NOTIFY_CHANNEL | Claude Code가 사용자에게 요청 | O (Channel ID 확인 방법) | - |
| GEMINI_API_KEY | Claude Code가 사용자에게 요청 | O (aistudio URL) | - |
| OUTLINE_API_KEY | Claude Code가 사용자에게 요청 | O | - |
| OUTLINE_API_URL | Claude Code가 사용자에게 요청 | O | - |
| TRELLO_API_KEY | Claude Code가 사용자에게 요청 | O | - |
| TRELLO_TOKEN | Claude Code가 사용자에게 요청 | O | - |
| LOG_PATH | default: `{root}/runtime/logs` | Claude Code: "기본값 사용?" | - |
| SESSION_PATH | default | Claude Code: "기본값 사용?" | - |
| MEMORY_PATH | default | Claude Code: "기본값 사용?" | - |
| DEBUG 등 플래그 | default: `false` | Claude Code: "기본값 사용?" | - |

결론: **구성 가능**. Claude Code가 guide를 참고해 사용자에게 발급 방법까지 안내하므로,
단순 CLI 프롬프트보다 훨씬 자연스러운 설치 경험이 된다.

**장점:**
- 사용자가 "Slack Bot Token이 뭔지 모르겠어요"라고 하면 Claude Code가 설명 가능
- 토큰 형식이 잘못되면 ("xoxb-로 시작해야 합니다") Claude Code가 즉시 교정 가능
- validate가 있는 키는 실제로 유효한지 검증 가능
- 중간에 다른 질문이 생겨도 자연어로 해결 가능

### 2. 설치 과정에서 .mcp.json을 올바르게 생성할 수 있는가?

`content` 방식이므로 Phase 1에서 자동 생성. Claude Code 개입 불필요.

| MCP 서버 | 포트 | content에 포함 |
|----------|------|-------------|
| seosoyoung-mcp | 3104 | O |
| haniel | 3200 | O |
| eb-lore | 3108 | O |
| slack | 3101 | O |
| trello | 3102 | O |
| outline | 3103 | O |

결론: **생성 가능**.

**고려 사항:** 포트 번호가 services의 run과 configs의 content 양쪽에 존재하는 중복.
haniel은 포트의 의미를 모르므로, 이 중복은 haniel 바깥의 관심사.

### 3. 설치 과정에서 venv를 올바르게 구성할 수 있는가?

Phase 1에서 기계적으로 처리.

| 환경 | 경로 | requirements 위치 |
|------|------|------------------|
| main-venv | `./runtime/venv` | `./runtime/requirements.txt` |
| mcp-venv | `./runtime/mcp_venv` | `./runtime/mcp_requirements.txt` |
| soulstream-venv | `./soulstream_runtime/venv` | `./soulstream_runtime/requirements.txt` |
| runtime-node | `./runtime` | `./runtime/package.json` |

**미결: requirements.txt의 출처**

requirements.txt가 어디에서 오는지에 따라 방안이 갈린다:

- **방안 A**: runtime을 별도 리포로 등록 → `repos.seosoyoung-runtime`
- **방안 B**: 메인 리포에서 참조 → `.projects/seosoyoung/requirements.txt`
- **방안 C**: haniel.yaml에 직접 패키지 목록 명시

Phase 1에서 requirements.txt가 없어 실패하면,
Phase 2에서 Claude Code가 사용자에게 상황을 설명하고 해결을 유도할 수 있다.
이것이 Claude Code 위임의 강점이다. 에러가 나도 막히지 않는다.

### 4. 슬랙봇이 자체적으로 .env를 로드하는가?

haniel은 런타임에 .env를 절대 로드하지 않으므로,
각 프로세스가 자기 .env를 직접 로드해야 한다.

확인 필요:
- `seosoyoung.slackbot` — `dotenv.load_dotenv()` 호출 여부
- `seosoyoung.mcp` — 동일
- `seosoyoung.rescue` — 동일
- `soulstream.server` — 동일

없으면 각 진입점에 `dotenv.load_dotenv(dotenv_path)` 추가 필요.
이 변경은 프로세스 코드에서 해결하며, haniel의 관심사가 아니다.

### 5. 새 PC에서의 전체 부트스트랩 시나리오

```
새 PC (빈 상태)

1. 사전 준비: git, python, node 설치
2. pip install haniel (또는 git clone haniel 리포)
3. haniel.yaml 준비 (리포에 포함된 예시를 복사)

4. haniel install haniel.yaml 실행

   [Phase 0] Claude Code 확인
     ├─ 설치됨 → 계속
     └─ 미설치 → "npm install -g @anthropic-ai/claude-code 를 실행하세요" 출력 후 중단

   [Phase 1] 기계적 설치 (사용자 입력 없음, 자동 진행)
     requirements: python ✓, node ✓, nssm ✗ (기록)
     directories: 12개 생성 ✓
     repos: seosoyoung clone ✓, soulstream clone ✓, eb_lore clone ✓
     environments: main-venv ✓, mcp-venv ✓, soulstream-venv ✓, node ✓
     configs (content): .mcp.json ✓

   [Phase 2] Claude Code 세션 시작
     Claude Code: "안녕하세요! haniel 설치를 도와드리겠습니다."
     Claude Code: "먼저, nssm이 설치되지 않았습니다.
                   https://nssm.cc/download 에서 다운로드해주세요."
     사용자: "설치했어"
     Claude Code: → haniel_retry_step("requirements") → nssm ✓
     Claude Code: "이제 설정 값을 입력해주세요.
                   먼저 Slack Bot Token이 필요합니다.
                   https://api.slack.com/apps 에서 발급받을 수 있어요."
     사용자: "xoxb-1234..."
     Claude Code: → validate 실행 → 성공 ✓
     Claude Code: → haniel_set_config("workspace-env", "SLACK_BOT_TOKEN", "xoxb-1234...")
     ... (나머지 키 수집) ...
     Claude Code: "모든 설정이 완료됐습니다. 마무리할까요?"
     사용자: "네"
     Claude Code: → haniel_finalize_install()
     (세션 자동 종료)

   [Phase 3] 마무리
     .env 파일 생성 (수집된 값으로)
     watchdog_config.json 생성
     NSSM 서비스 등록

5. nssm start haniel → 서비스 시작
```

결론: **가능**. 사용자가 직접 타이핑해야 하는 것은 시크릿 값뿐이고,
나머지는 전부 자동이거나 Claude Code의 대화형 안내로 처리된다.

requirements.txt 출처 문제도 Phase 1에서 실패 → Phase 2에서 Claude Code가
사용자에게 상황을 설명하고 대안을 제시할 수 있으므로, 설치가 막히지 않는다.

## 현행 대비 누락 기능

| 현행 기능 | haniel 대응 | 비고 |
|----------|------------|------|
| 세션 모니터링 (배포 대기) | 없음 | graceful shutdown timeout으로 대체 |
| exit code 기반 재시작 정책 | 없음 | 0=정상, 나머지=크래시로 단순화 |
| 자동 롤백 | 없음 | MCP로 수동 제어 |
| Claude Code 비상 복구 | 없음 | rescue-bot이 대체 |
| supervisor가 .env 로드 | **의도적 제거** | 각 프로세스가 자체 로드 |

## 리포지토리 구조 (안)

```
haniel/
├── src/
│   └── haniel/
│       ├── __init__.py
│       ├── __main__.py          # CLI 진입점 (install / run / status / validate)
│       ├── config.py            # YAML 파싱, 검증
│       ├── installer/
│       │   ├── __init__.py
│       │   ├── orchestrator.py  # Phase 1-2-3 흐름 제어
│       │   ├── mechanical.py    # Phase 1: 디렉토리, clone, venv, npm
│       │   ├── interactive.py   # Phase 2: Claude Code 세션 관리
│       │   └── finalize.py      # Phase 3: 파일 생성, NSSM 등록
│       ├── runner.py            # run 모드: poll 루프
│       ├── process.py           # 프로세스 생성, 감시, 종료
│       ├── git.py               # git fetch, pull, clone
│       ├── webhook.py           # Slack/Discord/JSON 알림 (Block Kit)
│       ├── mcp_server.py        # MCP 서버 (run + install 양쪽)
│       ├── health.py            # 헬스체크, circuit breaker
│       ├── logs.py              # stdout/stderr 캡처, 로그 파일 관리
│       └── platform/
│           ├── __init__.py
│           ├── windows.py       # Windows Job Object, SIGTERM 에뮬레이션
│           └── posix.py         # Unix 시그널 처리
├── tests/
├── pyproject.toml
└── haniel.example.yaml
```

## 미결 사항

1. **requirements.txt 출처**: runtime이 별도 리포인지, 메인 리포에 포함인지에 따라 environments 구성 변경
2. **프로세스별 dotenv 자체 로드**: 현행 코드가 supervisor의 환경변수 상속에 의존하고 있다면 코드 변경 필요
3. **Windows SIGTERM**: CTRL_BREAK_EVENT 또는 HTTP shutdown 엔드포인트로 대체
4. **로그 로테이션**: haniel 내장 vs 외부 도구
5. **포트 번호 중복**: services의 run과 configs의 .mcp.json에 동일 포트가 명시되는 문제
6. **Claude Code 호출 방식**: `claude -p` 프롬프트 모드 vs `claude --mcp-config` 조합의 정확한 CLI 인터페이스 확인
7. **install 전용 MCP vs run MCP**: 같은 MCP 서버가 모드에 따라 다른 도구를 노출할지, 별도 서버로 분리할지
8. **다중 환경 지원**: 향후 dev/staging/prod 분리 필요 시 검토
