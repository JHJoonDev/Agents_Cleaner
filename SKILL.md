---
name: agents-cleaner
description: Windows에서 Herdr로 돌린 Codex·Claude Code·Gemini 작업이 남긴 대화 기록, 앱 캐시·로그, 작업 부산물(에이전트 임시 폴더, node_modules·.venv·target 같은 빌드·의존성 폴더, herdr-app 백업)을 찾아 용량을 보고하고, 사용자가 Agents Cleaner 화면에서 고른 것만 정리하게 안내한다. "디스크 정리", "임시 파일 정리", "용량 확보", "에이전트 기록 정리" 요청에 사용한다.
---

# Agents Cleaner

프로그램 폴더: 이 스킬 폴더(`SKILL.md`가 있는 곳). Python 3.11, `py -3.11 -m pip install -r requirements.txt`(PySide6, psutil).

## 에이전트가 하는 일
1. 읽기 전용으로 현황을 본다. 둘 다 아무것도 지우지 않고, 대화 제목·내용을 출력하지 않는다.
   - `py -3.11 scripts/main.py --inventory` — 앱별 기록 수, 캐시 수, 작업 부산물 단계별 개수·용량
   - `py -3.11 scripts/main.py --artifacts` — 작업 부산물 목록(JSON): 종류, 단계, 정리 방식, 경로, 용량, 미사용 일수, 보존 사유
2. 결과를 한국어 표로 보고한다: 단계별 합계 → 큰 항목 순. 보존(blocked) 항목은 사유와 함께 따로 적는다.
3. 실제 정리는 사용자가 한다. 탐색기에서 `실행.cmd`를 실행하라고 안내한다(목록만 보려면 `읽기전용.cmd`). Herdr 터미널 안에서 GUI를 띄우지 않는다: 띄운 앱(Herdr)은 이 프로그램이 닫을 수 없다.
4. 에이전트는 파일을 직접 지우지 않고, 이 프로그램의 삭제 함수를 스크립트로 호출하지 않는다.

## 정리 대상과 방식
| 대상 | 방식 |
|---|---|
| Codex 대화 | 공식 `codex --no-daemon delete <UUID> --force`로 영구 삭제(복구 불가) |
| Claude Code·Gemini 대화 | 휴지통 |
| 앱 캐시·로그(정해진 폴더만) | 휴지통 |
| 작업 부산물 1단계 — 바로 정리: `%TEMP%\claude\…` 세션 임시 폴더, `%TEMP%`의 `codex_*`·`codex-*`·`herdr-jhj-*`·`ai-storage-db-*`, `__pycache__`·`.pytest_cache` (7일 넘게 수정 없음) | 영구 삭제 |
| 작업 부산물 2단계 — 확인 후 정리: `node_modules`, `.venv`, Rust `target`, `dist`·`build` (프로젝트를 30일 넘게 안 씀) | 휴지통, 다시 설치하는 명령을 함께 표시 |
| 작업 부산물 3단계 — 개수 제한: herdr-app `data\runtime-backups`·`profile-backups` | 최근 3개 보존, 나머지 휴지통 |
| 작업 폴더 자체 | '작업 폴더' 열에서 따로 체크했을 때만, 그 폴더의 대화를 모두 지운 뒤 휴지통 |

휴지통으로 보낸 것은 휴지통을 비워야 용량이 실제로 늘어난다.

## 지우지 않는 것
로그인·토큰(`auth.json`), 앱 설정, Codex 상태 DB(`state_*.sqlite` 등)와 계정 폴더의 `orchestration`·`.sandbox-bin`, Claude 프로젝트 메모리, Herdr 터미널 상태·화면, Aside 사용자 데이터, git에 올라간 파일, 프로젝트 결과물 폴더. 드라이브 루트·사용자 폴더·기본 폴더·시스템 폴더·AI 앱 저장소·이 프로그램 폴더·링크·junction은 작업 폴더로도 지울 수 없다.

## 안전 규칙
- 실행 중인 앱의 기록은 잠기고, 잠금은 2초마다 갱신된다. 실행 상태를 확인하지 못하면 모두 잠근다.
- Claude 기록은 `~/.claude`와 모든 `~/.claude-<id>`(그리고 `CLAUDE_CONFIG_DIR`)에서 찾는다. 세션 파일로 특정되는 대화만 잠그고, 특정할 수 없는 Claude CLI가 있으면 Claude 기록 전체를 잠근다.
- Codex 기록은 `~/.codex`와 `~/.codex/accounts/<ID>`(`shared-runtime` 제외)에서 찾는다.
- 작업 부산물은 `hc ls`의 실행 중인 Herdr 터미널 작업 폴더 안에 있거나, 실행 중인 세션의 것이거나, 24시간 안에 수정됐으면 보존한다. `hc ls`를 읽지 못하면 모두 보존한다. 삭제 직전에 이 검사를 다시 한다.
- Herdr 서버만 떠 있으면 AI 기록은 잠그지 않지만 작업 폴더 삭제는 막는다.
- 자동으로 프로세스를 종료하거나 계정을 전환하지 않는다. '실행 중 앱'의 종료 버튼은 확인 후 창 닫기와 `hc server stop`을 요청하고, 남은 프로세스는 다시 확인받아 강제 종료한다.
- 삭제 실패는 항목별로 보고한다.
