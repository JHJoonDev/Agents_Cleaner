# AI Storage Manager GUI 시안

조사일: 2026-10-28. 현재 `scripts/manager/ui.py`와 루트 `SKILL.md` 기준. `scripts/`·`tests/`는 수정하지 않음.

## 참고 사례

- [Fluent 2](https://fluent2.microsoft.design/), [Windows Dialog 지침](https://learn.microsoft.com/en-us/windows/apps/develop/ui/controls/dialogs-and-flyouts/dialogs): 중립적인 표면·명령 모음·짧고 명확한 확인 창. A의 참고.
- [Files](https://files.community/): 사이드바·도구 모음·파일 열과 Git 상태를 나누는 Windows 파일 관리자. A의 정보 배치 참고.
- [VS Code UI](https://code.visualstudio.com/docs/editing/getting-started/userinterface): 활동 표시줄·탭·트리·상태 영역의 역할 구분. B의 밀도와 구조 참고.
- [DaisyDisk](https://daisydiskapp.com/): 용량을 숫자와 시각적 요약으로 먼저 파악하는 스토리지 도구. C의 요약 카드 참고.
- [Dribbble / Pati Rutecka](https://dribbble.com/shots/20086914-File-Manager-Dashboard): 여백·분류·가벼운 파일 관리 화면. C의 밝은 구성 참고.
- [Dribbble / Umais](https://dribbble.com/shots/26141373-Dark-Mode-File-Manager-Dashboard-UI): 어두운 바탕의 정보 계층·스토리지 지표. B의 명도 구분 참고.

## 시안

- **A / Fluent Desk**: Windows 도구에 익숙한 사이드바·명령 모음·표 중심. `ttk` 테마와 Treeview 스타일 유지·개선, 난이도 낮음~중간.
- **B / Developer Console**: 어두운 활동 표시줄·탭·조밀한 표, 확인 창은 대상/설정의 2열. PySide6의 QTreeView delegate·QDialog로 전환, 난이도 높음.
- **C / Clear Space**: 세이지 색·용량 카드·부드러운 선택 요약, 확인 창은 단계 안내를 곁들임. CustomTkinter + ttk.Treeview 혼합, 난이도 중간.

공통: 메인 1280×800 + 아래 확인 창 1280×800, PNG 전체 1280×1600. 각 HTML은 CSS/JS/SVG 인라인·시스템 폰트만 사용하며 독립 실행됨. 검색·프로젝트/대화/작업 폴더 체크·선택 해제·보조 창·확인 체크 2개 동작을 제공함. 모든 이름·경로·용량·PID는 가짜 예시이며 실제 파일을 조회하거나 삭제하지 않음.

현재 동작 반영: 작업 폴더 삭제는 별도 선택, 용량·Git 경고와 결과물 이동 확인 필요, 폴더는 별도 백업 없이 휴지통 이동. 캐시·로그 백업은 기본 해제, 계정·설정·Claude 메모리·Codex 프로젝트 등록 보존. Orca 등록은 조회 전용. 백업 관리에서는 백업 영구 삭제를 별도로 표현함. 구현 난이도는 기존 Tkinter 코드 대비 디자인 판단임.

## B2 — 일반 데스크톱 유틸리티 방식

2026-10-28 조사. 공식 스크린샷을 직접 확인하고 문서와 대조함. 아래는 관찰한 배치와 B2에 적용한 디자인 판단임.

- [WizTree 화면](https://antibodysoftware-17031.kxcdn.com/images/wiztree_treeviewwithmap4.jpg), [TreeSize Visual Tour](https://www.jam-software.com/treesize/screens.shtml), [WinDirStat 화면](https://windirstat.net/images/windirstat.jpg): 트리와 정렬 열·우측 숫자·크기 비율 막대를 가까이 배치. B2는 별도 KPI 대신 용량 열 안에 얇은 막대를 둠.
- [Everything 화면·사용 문서](https://www.voidtools.com/support/everything/using_everything/), [SpaceSniffer 문서](https://www.uderzo.it/main_products/space_sniffer/): 검색·필터는 목록 바로 위에 배치. Everything의 메뉴 → 검색 → 열 머리글 → 결과 → 하단 상태줄 구성을 참고. SpaceSniffer의 트리맵은 이번 시안에 넣지 않음.
- [System Informer 화면](https://systeminformer.io/include/screenshots/theme_support.png), [Process Explorer 문서](https://learn.microsoft.com/en-us/sysinternals/downloads/process-explorer): 조밀한 트리·전체 행 선택과 별도 상세 영역. 평상시 상태는 비우고 실행 중·보호·조회 전용만 회색 문구와 아이콘으로 표시.
- [JetBrains New UI](https://www.jetbrains.com/help/idea/new-ui.html), [Sublime Merge](https://www.sublimemerge.com/), [Fork Windows 화면](https://git-fork.com/images/carousel/carousel_mainWin.jpg): 회색 표면과 구분선, 제한된 선택색, 목록과 상세 패널의 분리 참고.
- [BleachBit 화면](https://www.bleachbit.org/screenshots), [Windows 확인 대화상자 지침](https://learn.microsoft.com/en-us/windows/apps/develop/ui/controls/dialogs-and-flyouts/dialogs): 정리 항목 체크와 구체적 대상·동작 확인을 중심으로 구성.

B에서 바꾼 점:
- 보라색·영문 슬로건·KPI 타일·행별 배지·활동 표시줄을 제거. 한국어 앱 이름, 메뉴·도구 모음·하단 상태줄로 재배치.
- 7개 열은 클릭 정렬 지원. 숫자는 tabular/우측 정렬, 크기 막대는 전체 기록 대비, 체크한 항목은 전체 행 강조.
- 중성 회색 + 절제된 파란 선택색, 2~3px 반경, 최소 그림자. 삭제 버튼도 일반 사각 버튼으로 변경.
- 확인 창은 대상·Git 경고·백업 위치·캐시 백업·필수 확인 2개만 순서대로 표시. 설명은 필요한 위치에 한 번씩만 둠. 데이터는 전부 가짜 예시이며 기존 기능은 유지.

산출물: `option-b2.html`, Chrome headless `option-b2.png`(1280×1600, 메인 1280×800 + 확인 창 1280×800). 기존 A/B/C 및 `scripts/`, `tests/`는 유지.
