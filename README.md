# haniel

부활시키는 천사. 설정 기반의 극단적으로 무지한 서비스 러너.

## 철학

haniel은 자기가 무엇을 돌리는지 모른다.
git 리포를 체크하고, 변경이 있으면 pull 받고, 명시된 명령어로 프로세스를 기동한다.
그것이 슬랙봇이든 MCP 서버든 웹 대시보드든, haniel에게는 전부 "프로세스"일 뿐이다.

### haniel이 아는 것

- 설정 파일(YAML)에 적힌 내용
- git fetch/pull 결과
- 프로세스가 살아있는지 여부

### haniel이 모르는 것

- `.env`의 내용과 의미 — **읽지도, 로드하지도, 자식에게 주입하지도 않는다**
- `.mcp.json`의 내용 — 설치 시 생성만 하고, 런타임에 건드리지 않는다
- 프로세스가 무슨 일을 하는지
- 프로세스 간의 비즈니스 의존성
- 포트 번호의 의미
- 호스트 시스템의 나머지 구성

## 설치

```bash
pip install haniel
```

개발 모드:
```bash
git clone https://github.com/eiaserinnys/Haniel.git
cd Haniel
pip install -e ".[dev]"
```

## 사용법

### 네 가지 모드

```bash
# 환경 설치 (디렉토리, venv, git clone 등)
haniel install haniel.yaml

# 서비스 실행 (poll 루프 시작)
haniel run haniel.yaml

# 상태 조회
haniel status

# 설정 검증
haniel validate haniel.yaml
```

### 기본 설정

```yaml
poll_interval: 60

repos:
  my-app:
    url: git@github.com:org/my-app.git
    branch: main
    path: ./projects/my-app

services:
  my-service:
    run: python -m my_app.server
    cwd: ./projects/my-app
    repo: my-app
    ready: port:8080
```

자세한 설정 방법은 [설계 문서](docs/haniel-spec.md)를 참조하세요.

## 개발

```bash
# 테스트 실행
pytest

# 커버리지와 함께
pytest --cov=src/haniel

# 린트
ruff check src/ tests/
ruff format src/ tests/
```

## 라이선스

MIT
