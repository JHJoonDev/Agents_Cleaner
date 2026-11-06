# Agents Cleaner

Codex, Claude Code, Gemini CLI, Orca, Herdr가 PC에 쌓아 둔 기록과 캐시를 프로젝트별 목록으로 보여 주고, 체크한 것만 백업 없이 정리하는 Windows용 프로그램입니다.

Herdr는 `%LOCALAPPDATA%\herdr-core\<프로필>`의 터미널 목록을 조회하고 `core.log`·`holder.log`를 정리합니다. 터미널 상태·화면·앱 설정은 보존하며, 종료된 터미널 제거는 Herdr의 `hc rm <ID>`를 사용하세요. Herdr에서 실행한 Codex·Claude 대화는 기존 원본 저장소에서 표시됩니다. Herdr 앱이나 core 서버가 실행 중이면 공용 AI 기록 삭제도 차단합니다. 창을 닫아도 서버는 남을 수 있습니다(`hc server stop`).

![메인 화면](design/qt-main.png)

## 실행

Python 3.11 이상이 필요합니다.

```
py -3.11 -m pip install -r requirements.txt
py -3.11 scripts/main.py              # 정리
py -3.11 scripts/main.py --read-only  # 목록만 보기
py -3.11 scripts/main.py --inventory  # 개수만 출력 (제목·내용 없음)
```

탐색기에서 `실행.cmd` 또는 `읽기전용.cmd`를 더블클릭해도 됩니다. PySide6가 없으면 기본 Tkinter 화면으로 실행됩니다.

## 지우는 방식

| 대상 | 방식 |
|---|---|
| Codex 대화 | 공식 `codex delete` 명령으로 영구 삭제 (백업 없음, 복구 불가) |
| Claude Code · Gemini 대화 | 백업 없이 Windows 휴지통으로 이동 |
| 캐시·로그 | 정해진 폴더만 백업 없이 휴지통으로 이동 |
| 작업 폴더 | '작업 폴더' 열에서 따로 체크한 경우에만, 그 폴더의 대화를 모두 지운 뒤 휴지통으로 이동 |

로그인 정보, 설정, Claude 프로젝트 메모리, Codex 프로젝트 등록, Orca 프로젝트 등록은 지우지 않습니다.

## 안전장치

- 해당 앱(Codex, Claude, Gemini, Orca)이 실행 중이면 그 앱의 기록은 삭제할 수 없습니다. '실행 중 앱'에서 닫을 프로그램을 확인하세요.
- 드라이브 루트, 사용자 폴더, 바탕화면·문서·다운로드 자체, 시스템·설치 폴더, AI 앱 저장소, 링크·junction은 작업 폴더로 지울 수 없습니다.
- 작업 폴더가 git 저장소이면 커밋하지 않은 변경, push하지 않은 커밋, 원격 없음을 경고합니다.
- 삭제 전 확인 창에서 확인 체크를 해야 진행됩니다.

휴지통을 비워야 이동한 파일의 디스크 공간이 실제로 늘어납니다. '백업 관리'에서는 이전 버전이 만든 백업을 정리할 수 있습니다.

## 테스트

```
py -3.11 -m unittest discover -s tests
```

`design/` 폴더에는 화면 시안과 디자인 조사 자료가 있습니다. 모든 시안과 캡처는 가짜 예시 데이터입니다.
