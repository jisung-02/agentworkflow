# Python 상태 전이 워크플로 엔진 명세 초안

상태: 설계 기준 · 구현은 [PLAN.md](PLAN.md)에 따라 진행 중

## 1. 목표와 제약

- 사용자는 PyPI 또는 GitHub에서 패키지를 설치하고 YAML과 Markdown 지침 파일로 워크플로를 정의한다.
- 실행은 상태 전이 그래프를 따른다. 조건 분기, 반복, 병렬 fork/join, 사람의 입력, 타이머를 지원한다.
- Slack, Discord, 자체 CLI, Codex 및 Claude Code 확장은 같은 애플리케이션 명령을 호출한다. 채널별로 실행 의미가 달라지지 않는다.
- 실행 상태와 일정은 SQLite에 영속화한다. 프로세스 재시작이나 모델 사용량 제한 후에도 이어서 실행한다.
- Codex와 Claude Code의 로컬 로그인 상태를 활용한다. 사용자 API 키를 필수로 요구하지 않는다.
- Python 패키지 관리에 `uv`, 린트와 포맷에 `ruff`, 정적 타입 검사에 `ty`를 사용한다.
- 계층 간 전달에는 명시적 DTO 또는 VO를 사용하고, 의존성은 인터페이스를 통해 주입한다.

### 범위 밖

- 임의의 Python 표현식을 YAML에서 실행하는 기능
- 실행 중인 프로세스를 강제로 중단한 뒤 정확히 같은 모델 토큰 위치에서 재개하는 기능
- Claude 구독 로그인으로 제3자 백그라운드 SDK를 무인 실행할 수 있다는 가정
- 다중 호스트 분산 실행의 완전한 지원. 첫 버전은 단일 호스트와 여러 로컬 워커를 대상으로 한다.

## 2. 용어와 실행 모델

| 용어 | 의미 |
| --- | --- |
| Definition | 검증된 YAML과 지침 파일의 불변 스냅샷 |
| State | 그래프의 노드. `task`, `fork`, `join`, `wait`, `end` 중 하나 |
| Transition | 실행 결과의 `outcome`을 다음 State로 매핑한 간선 |
| Run | Definition을 특정 입력으로 실행한 인스턴스 |
| Token | 현재 활성 상태를 가리키는 실행 포인터. 병렬 분기마다 하나씩 생김 |
| Attempt | State 실행의 한 번의 시도. 재시도 시 번호가 증가함 |
| Event | 실행 사실을 기록한 영속 이벤트 |
| Artifact | 작업 결과 파일 또는 구조화 데이터의 참조 |

State는 프롬프트 그 자체가 아니다. `task`는 어떤 실행기와 지침을 사용할지 정의하고, 실행 결과는 `outcome`, 출력 DTO, Artifact 참조로 표준화한다. 엔진만 Transition을 적용한다. 실행기가 다음 상태를 직접 정하지 않는다.

## 3. 설치와 배포

- 패키지 이름은 추후 결정한다. `src/` 레이아웃과 `pyproject.toml`을 사용한다.
- 기본 설치에는 엔진, SQLite, CLI, YAML 검증을 포함한다. Slack, Discord, Codex, Claude Code 통합은 선택 의존성 또는 별도 확장 패키지로 분리한다.
- 명령형 CLI는 `workflow validate`, `workflow run`, `workflow resume`, `workflow status`, `workflow graph`를 제공한다.
- 설치된 사용자의 워크플로 파일은 패키지 코드 밖에 둔다. 실행 시 YAML 및 참조 지침의 내용 해시와 스냅샷을 저장해, 이후 파일 수정이 기존 Run의 의미를 바꾸지 않게 한다.
- 확장 노드는 Python entry point로 등록한다. YAML에 임의의 모듈 경로나 import 문자열을 쓰지 않는다.

## 4. YAML 정의 형식 v1

```yaml
version: 1
id: example-workflow
name: 예제 개발 워크플로
entry: plan

inputs:
  request:
    type: string
    required: true

triggers:
  - type: manual
  - type: timer
    cron: "0 9 * * 1-5"
    timezone: Asia/Seoul
    input:
      request: "매일 점검"

states:
  plan:
    kind: task
    runner: codex
    instructions: instructions/plan.md
    output: plan
    outcomes: [completed, rejected]
    on:
      completed: parallel_work
      rejected: end_failed

  parallel_work:
    kind: fork
    branches:
      implementation: implement
      documentation: document
    join: gather

  implement:
    kind: task
    runner: codex
    instructions: instructions/implement.md
    output: implementation
    outcomes: [completed, failed]
    on:
      completed: gather
      failed: gather

  document:
    kind: task
    runner: codex
    instructions: instructions/document.md
    output: documentation
    outcomes: [completed, failed]
    on:
      completed: gather
      failed: gather

  gather:
    kind: join
    policy: all_success
    on:
      completed: review
      failed: end_failed

  review:
    kind: task
    runner: codex
    instructions: instructions/review.md
    output: review
    outcomes: [approved, revise, failed]
    max_visits: 3
    on:
      approved: end_success
      revise: parallel_work
      failed: end_failed

  end_success:
    kind: end
    result: success

  end_failed:
    kind: end
    result: failure
```

### YAML 규칙

1. `version`, `id`, `entry`, `states`는 필수다. 알 수 없는 필드는 기본적으로 오류다.
2. 각 `task`는 허용할 `outcomes`를 선언한다. 실행 결과는 그 집합에 속해야 하고, `on`은 선언된 모든 outcome을 빠짐없이 매핑해야 한다. `fork`와 `join`은 종류별로 정해진 outcome을 사용한다.
3. State ID와 branch ID는 해당 Definition 안에서 유일하다. `entry`와 모든 목적지는 실제 State를 가리켜야 한다.
4. `fork`의 각 branch는 같은 `join`으로 도달해야 한다. `join`은 해당 fork 인스턴스가 생성한 branch만 합친다. 반복 진입하면 새로운 fork 인스턴스를 만든다.
5. `all_success` join은 모든 branch가 도착한 뒤 모두 성공했을 때만 한 번 `completed`로 이동한다. 하나라도 실패했다면 한 번 `failed`로 이동한다. 각 branch의 마지막 outcome을 join 입력으로 저장한다.
6. 순환 경로에는 `max_visits` 또는 Run의 `deadline`이 필요하다. 검증기는 무한 반복 가능 경로를 거부한다.
7. 전이 조건은 v1에서 문자열 표현식이 아니다. 노드의 타입이 정해진 결과 `outcome`만 사용한다. 복잡한 판단은 등록된 결정 노드가 구조화된 outcome을 반환하게 한다.
8. 지침 파일 경로는 Definition 기준 상대 경로이며 루트 밖 경로 이동을 허용하지 않는다. 입력 치환은 명시적으로 선언한 필드만 허용한다.
9. `timer`는 채널이자 트리거다. cron과 시간대를 정의하고, 발화 시 일반 Run 생성 명령을 호출한다. 예약 실행의 중복 발화를 막을 고유 키를 저장한다.

## 5. 계층과 디렉터리

```text
src/workflow_engine/
  domain/
    model.py                 # State, Transition, Run, Token, Attempt
    value_objects.py         # 식별자, outcome, 시각, 해시, 경로 등
    events.py                # 도메인 이벤트
    errors.py
  application/
    dto.py                   # 계층 간 명령/응답 DTO
    ports.py                 # Repository, Runner, Channel, Clock 등 Protocol
    use_cases/
      validate_definition.py
      start_run.py
      advance_run.py
      resume_run.py
      deliver_message.py
      fire_timer.py
  infrastructure/
    yaml_definition.py       # YAML 파싱 및 외부 입력 검증
    sqlite/                  # Repository 구현, 트랜잭션, migration
    runners/                 # Codex, Claude host, 사용자 노드 어댑터
    channels/                # Slack, Discord, CLI, 확장 host
    scheduler.py
    unsafe_boundary.py       # 불완전한 외부 타입을 검증·정규화하는 격리 지점
  presentation/
    cli.py
    api.py                   # 필요한 경우에만 로컬 API
  bootstrap/
    container.py             # 설정과 구현체 연결, Composition Root
  py.typed
tests/
examples/
  example-workflow.yaml
  instructions/
```

의존 방향은 `presentation/infrastructure/bootstrap → application → domain`이다. Domain은 YAML, SQLite, SDK, 채널을 import하지 않는다. Application은 Protocol만 알고 어댑터 구현을 import하지 않는다. `bootstrap/container.py`만 구체 구현을 조립한다. CLI, 타이머, 채널 이벤트는 동일한 Use Case를 호출한다.

### Port와 DI 계약

- `DefinitionRepository`: Definition 스냅샷 저장·조회
- `RunRepository`: Run, Token, Attempt, Event의 원자적 읽기·쓰기
- `Runner`: `TaskRequestDTO → TaskResultDTO` 실행; 지원 가능한 runner ID와 capability 제공
- `Channel`: 사용자에게 요청 전달, 응답 수신, 메시지 idempotency key 처리
- `Scheduler`: 시각 기반 wake-up 등록·해제
- `Clock`: 현재 시각 공급
- `ArtifactStore`: 파일/구조화 결과 저장 및 참조 해석
- `LeaseManager`: 워커의 Run/Token 점유와 만료 관리

Port는 Python `Protocol`로 선언하고 생성자에서 주입한다. 전역 서비스 로케이터와 모듈 수준 싱글턴은 사용하지 않는다. 플러그인은 등록 시 계약에 맞는 팩토리를 제공하고 Composition Root에서 연결한다. 테스트는 In-memory Port 구현으로 Use Case를 검증한다.

### DTO와 VO 규칙

- VO는 `frozen dataclass` 또는 검증된 불변 타입으로 만든다. 예: `RunId`, `StateId`, `Outcome`, `WakeAt`, `DefinitionHash`. 값의 불변식은 생성 시 검사한다.
- DTO는 계층 간 요청과 결과를 표현한다. 예: `StartRunCommand`, `TaskRequestDTO`, `TaskResultDTO`, `RunStatusDTO`, `ChannelMessageDTO`. 필드는 명시적으로 타입을 선언하고 변경이 필요한 경우 새 DTO를 만든다.
- 외부 YAML/JSON/SDK 객체를 Domain이나 Application에 그대로 전달하지 않는다. Infrastructure 경계에서 구문과 스키마를 검증하고 DTO/VO로 변환한다.
- SQLite row, ORM 모델, Slack/Discord payload, SDK 응답은 Port 반환형이 될 수 없다.
- 결과의 확장 데이터는 임의의 중첩 dict 대신 선언된 출력 스키마와 Artifact 참조로 표현한다. 노드별 출력 스키마는 Definition 검증 시 등록된 타입과 대조한다.

## 6. `Any` 격리와 정적 검사

- 프로젝트 코드에서 `Any`, 무검증 `cast`, `# type: ignore`를 허용하는 기본 위치는 `infrastructure/unsafe_boundary.py` 한 파일이다. 외부 라이브러리의 불완전한 타입 또는 동적 payload를 이곳에서 `object`로 받고, 타입 좁히기와 검증을 거쳐 DTO로 반환한다.
- 어댑터가 `unsafe_boundary.py`의 역할을 지나치게 키우면 통합별 `unsafe_*.py` 파일로 분리할 수 있다. 그 경우에도 허용 파일 목록을 `pyproject.toml`에 명시하고, Port 경계 밖으로 `Any`가 새지 않는지 검사한다.
- 일반 코드에서 구조를 모르는 값은 `Any` 대신 `object`를 사용한다. 외부 JSON은 파서 경계에서 재귀 JSON 값 타입으로 좁힌 뒤 스키마 검증한다.
- Ruff에서 `ANN401`을 활성화하고 해당 경계 파일에만 `per-file-ignores`를 둔다. `ANN401`은 주로 함수 인자/반환값의 명시적 `Any`를 잡으므로, import 및 타입 별칭 사용은 별도 CI 검색과 코드 리뷰로 제한한다.
- `ty check`는 전체 `src`와 테스트를 검사한다. 파일 전체 배제나 전역 규칙 끄기는 사용하지 않는다. 특정 SDK에 필요한 예외는 경계 파일 안에 이유와 추적 이슈를 적는다.

개발 명령:

```bash
uv sync --all-extras --dev
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

`pyproject.toml`에는 `requires-python >= 3.11`, 런타임/선택/개발 의존성, Ruff 규칙과 경계 파일 예외, ty 설정을 둔다. `uv.lock`을 저장해 재현 가능한 개발 환경을 제공한다. 배포 패키지의 실제 의존성은 wheel 메타데이터에서 관리한다.

## 7. 영속성, 병렬성, 재개

SQLite는 WAL과 foreign key를 활성화한다. 주요 테이블은 `definitions`, `runs`, `tokens`, `attempts`, `events`, `artifacts`, `schedules`, `inbox_messages`, `outbox_messages`, `leases`다. 스키마 변경은 순서가 있는 migration으로 관리한다.

State 완료와 후속 Token 생성은 한 트랜잭션으로 처리한다. 같은 `(run_id, token_id, attempt_id, outcome)` 이벤트가 다시 들어오면 기존 결과를 반환한다. 이로써 전이는 중복 적용되지 않는다. 외부 효과(메시지 전송, 모델 호출, 파일 변경)의 정확히 한 번 실행은 보장하지 않으므로, 각각 idempotency key, 송신함, 작업 디렉터리 스냅샷, 확인 가능한 작업 결과를 사용한다.

워커는 만료 가능한 lease를 획득하고 Attempt를 시작한다. 프로세스가 중단되면 lease 만료 후 복구 워커가 결과를 조사한다. 결과가 확인되면 완료 처리하고, 불확실한 외부 효과는 사용자/어댑터의 확인 정책에 따라 재시도하거나 `needs_attention`으로 둔다. 무조건 다시 모델을 호출하지 않는다.

병렬 branch는 독립 Token과 Attempt를 가진다. 동일한 작업 디렉터리를 동시에 수정하는 runner는 지원하지 않는다. 코드 변경 branch는 서로 다른 worktree 또는 분리된 작업 공간을 사용하고, join 이후 명시적 통합 State에서 합친다. join은 fork 인스턴스 ID와 branch ID를 기준으로 도착 상태를 저장한다.

## 8. Runner와 채널

### Codex

로컬 로그인과 호환되는 Codex 실행 경로를 어댑터로 감싼다. 세션/작업 식별자와 작업 디렉터리, 결과 Artifact를 Attempt에 저장한다. 사용량 제한 시 `waiting_quota`로 전이하고 SDK/호스트가 제공하는 재설정 시각을 우선 사용한다. 시각을 알 수 없으면 보수적인 재확인 간격과 지수 백오프를 사용한다. 재개 시 동일 작업의 상태를 조회하고 이어서 처리한다.

### Claude Code

Claude Code 구독 로그인은 Claude Code 호스트의 skill/plugin 경로에서 사용한다. 독립 백그라운드 Python 프로세스가 Claude Agent SDK로 구독 세션을 무인 이용한다고 가정하지 않는다. 호스트가 실행 가능한 동안 요청·결과를 주고받고, 호스트가 없으면 작업을 `waiting_host`로 저장한다. 타이머는 작업을 예약·생성할 수 있지만 호스트 재연결 전 Claude 실행까지 보장하지 않는다. 무인 실행이 필수인 배포 환경은 별도 API 인증 방식을 명시적으로 선택해야 한다.

### 사람과 타이머

Slack/Discord/CLI/Codex/Claude 채널은 `run_id`, `token_id`, 메시지 ID를 가진 동일한 `ChannelMessageDTO`로 정규화한다. 사람의 응답은 `SubmitInputCommand`로 들어와 대기 State를 깨운다. 채널 인증·권한과 명령을 실행할 수 있는 주체는 어댑터에서 확인한다. Timer는 스케줄 발화 이벤트를 생성하는 채널이며 결과 메시지를 보내는 채널과 독립적이다.

## 9. 실패와 운영 정책

- 오류는 `retryable`, `terminal`, `quota`, `needs_input`, `host_unavailable` 등 타입이 있는 실패로 분류한다. YAML의 일반 `on` 간선은 노드의 업무 결과에 사용하고, 시스템 오류는 엔진의 공통 정책이 먼저 처리한다.
- 재시도는 State별 최대 횟수와 지수 백오프를 둔다. 제한을 넘으면 정의된 실패 간선 또는 `needs_attention`으로 이동한다.
- 사용량 제한 시 Run과 Attempt를 저장하고 wake-up을 예약한다. 재설정 시각 이후 자동으로 재개하되 로그인 만료나 호스트 부재는 별도 대기로 남긴다.
- 사용자가 워크플로 정의를 수정해도 진행 중 Run은 저장된 Definition 스냅샷을 사용한다. 명시적 migration 없이 실행 도중 그래프를 바꾸지 않는다.
- 각 전이와 외부 호출에는 상관 ID를 기록하고, 민감 정보는 Event/로그에 넣지 않는다. 로그는 입력과 결과의 요약 및 Artifact 참조를 남긴다.

## 10. 구현 순서와 완료 기준

1. YAML 스키마/검증기, DTO/VO, Domain 그래프 모델, Port 계약을 작성한다.
2. SQLite Repository, 트랜잭션 기반 전이, lease와 재개를 구현한다.
3. CLI와 Timer, Codex runner를 연결해 단일 작업·반복·병렬 fork/join을 완성한다.
4. 사람 입력 대기와 채널 어댑터, Claude Code host 연동을 추가한다.
5. 패키징, 예제 워크플로, 사용자 문서와 확장 노드 API를 공개한다.

첫 릴리스의 검증 기준:

- 잘못된 목적지, 모호한 outcome, 잘못된 fork/join, 무제한 순환을 실행 전에 거부한다.
- 프로세스를 State 완료 직후 중단해도 재시작 시 Transition이 한 번만 적용된다.
- 병렬 branch 완료 순서가 달라도 join 결과가 같다.
- 제한 감지 후 재설정 시각에 재개하며 이전 Artifact와 Attempt 이력을 유지한다.
- CLI와 채널에서 같은 Run을 조회·응답해도 권한 검사와 전이 규칙이 같다.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pytest`가 통과한다.

## 11. 결정이 필요한 항목

1. 공개 패키지 이름과 CLI 명령 이름
2. 최초 릴리스에서 Slack/Discord 중 어느 채널을 먼저 지원할지
3. Codex·Claude Code host와 연결할 때 사용할 공식 표면의 버전별 선택 및 호환성 검증
4. 공유 Run의 사용자 권한 모델: 한 사람 소유, 채널별 역할, 또는 프로젝트 단위 역할
5. Artifact 기본 저장 위치와 보존 기간

## 참고 문서

- [uv 프로젝트 의존성](https://docs.astral.sh/uv/concepts/projects/dependencies/)
- [Ruff 설정](https://docs.astral.sh/ruff/configuration/) 및 [ANN401](https://docs.astral.sh/ruff/rules/any-type/)
- [ty 설정](https://docs.astral.sh/ty/reference/configuration/)
- [Python Protocol](https://docs.python.org/3/library/typing.html#typing.Protocol)
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) 및 [Codex 인증](https://learn.chatgpt.com/docs/auth)
- [Claude Agent SDK 개요](https://code.claude.com/docs/en/agent-sdk/overview)
