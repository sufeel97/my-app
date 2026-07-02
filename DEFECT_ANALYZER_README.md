# 불량 이미지 분석기

`defect_analyzer.py`는 이미지 파일명에 들어있는 랏, 불량 종류, 좌표를 파싱해서
랏별 패턴을 요약하고 원인 공정을 추천하는 CLI 프로그램입니다.

## Agent 구조

프로그램 내부는 기능별 agent 파이프라인으로 구성되어 있습니다.

- `ImageCollectionAgent`: 이미지 파일 또는 폴더에서 분석 대상 이미지 수집
- `MetadataExtractionAgent`: 파일명에서 랏, 불량 종류, X/Y 좌표 추출
- `LabelingAgent`: 학습 CSV의 원인 공정 라벨을 이미지 레코드에 연결
- `ModelTrainingAgent`: 라벨 데이터로 원인 공정 추천 모델 학습
- `RecommendationAgent`: 개별 이미지에 대해 원인 공정 추천
- `LocationPatternAgent`: 근거리 집중, 에지/중앙 집중, 구역 편중, 라인성/대각 분포 분석
- `SampleDataAgent`: 검증용 예시 이미지 파일명과 학습 라벨 생성
- `DashboardAgent`: 분석 결과를 HTML 대시보드로 렌더링
- `ReportAgent`: 이미지별 CSV와 랏별 JSON 요약 생성
- `DefectAnalysisPipeline`: 위 agent들을 순서대로 실행하는 오케스트레이터

## 지원 파일명 예시

```text
LOT123_scratch_x120_y340.png
lot-A123_defect-particle_x=120_y=340.jpg
A123__open__120x340.bmp
MFA651601500/MFA651601500X-1Y-489.png
MFA651601500/particle/MFA651601500X-1Y-489.png
```

기본 파서로 맞지 않는 현장 파일명은 `--filename-regex`를 사용하세요.
정규식에는 `lot`, `defect`, `x`, `y` named group이 필요합니다.

`MFA651601500/MFA651601500X-1Y-489.png` 형식은 다음처럼 처리합니다.

- `MFA651601500`: lot 폴더명
- `X-1`: X 좌표 1
- `Y-489`: Y 좌표 489
- 불량 종류가 파일명이나 폴더에 없으면 `unknown`

불량 종류까지 자동 분류하려면 `MFA651601500/particle/MFA651601500X-1Y-489.png`처럼 lot 폴더 아래에 불량 종류 폴더를 두거나, 학습 라벨 CSV를 사용하세요.

## 실행 예시

데스크톱 UI 실행:

```bash
python3 defect_analyzer.py --gui
```

UI 제공 기능:

- 이미지 폴더 선택
- 결과 저장 폴더 선택
- 학습 라벨 CSV 선택
- 파일명 정규식 입력
- 좌표계 폭/높이 입력
- 분석 시작 버튼
- 완료 후 HTML 대시보드 자동 열기

CLI 실행:

```bash
python3 defect_analyzer.py ./images \
  --labels labels.csv \
  --output defect_report.csv \
  --summary-json lot_summary.json \
  --dashboard-html dashboard.html \
  --x-min -1500 \
  --x-max 1500 \
  --y-min -1000 \
  --y-max 1000 \
  --wafer-width 4096 \
  --wafer-height 4096
```

예시 데이터 생성부터 대시보드 출력까지 한 번에 실행할 수도 있습니다.

```bash
python3 defect_analyzer.py \
  --generate-sample-data sample_defects \
  --sample-count 48 \
  --sample-lots 4 \
  --output defect_report.csv \
  --summary-json lot_summary.json \
  --dashboard-html dashboard.html
```

학습 CSV는 다음 컬럼을 사용합니다.

```csv
filename,root_cause_process
LOT123_scratch_x120_y340.png,CMP
LOT124_particle_x800_y720.jpg,Cleaning
```

`--labels`가 없으면 불량 종류와 위치 패턴 기반의 룰로 추천합니다.
`--labels`가 있으면 간단한 categorical Naive Bayes 모델로 학습한 뒤 추천합니다.

## 출력

- `defect_report.csv`: 이미지별 랏, 불량, 좌표, 위치 구역, 추천 공정, 신뢰도, 추천 사유
- `lot_summary.json`: 랏별 이미지 수, 주요 불량, 주요 위치, 추천 공정 집계
- `dashboard.html`: KPI, 사각 Wafer 좌표 분포, 위치 패턴 분석, 불량별 대표 이미지, 불량/공정 분포, 랏별 요약, 이미지별 추천 결과

대시보드 사각 Wafer 좌표 분포의 기본 표시 범위:

- X: `-1500` ~ `1500`
- Y: `-1000` ~ `1000`

HTML 대시보드에서 X/Y min/max 값을 직접 수정한 뒤 `범위 적용`을 누르면 좌표 분포가 즉시 다시 표시됩니다.
CLI에서는 `--x-min`, `--x-max`, `--y-min`, `--y-max` 옵션으로 초기 표시 범위를 지정할 수 있습니다.

위치 패턴 분석은 다음 유형을 대시보드에 표시합니다.

- 동일 위치 근거리 집중 발생
- edge 또는 center 영역 집중
- 특정 3x3 구역 편중
- 가로/세로 라인성 분포
- 상승/하강 대각 분포

## 로컬 LLM Q&A

대시보드의 `로컬 LLM 데이터 Q&A` 섹션은 기본적으로 Ollama 호환 API를 호출합니다.

기본 설정:

```text
Endpoint: http://localhost:11434/api/chat
Model: llama3.1
```

로컬에서 Ollama를 사용할 경우 예시는 다음과 같습니다.

```bash
ollama pull llama3.1
ollama serve
```

대시보드는 HTML 내부에 포함된 집계 데이터, 위치 패턴, 샘플 레코드를 로컬 LLM에 전달합니다.
원본 이미지 전체를 전송하지 않고, 브라우저에서 사용자가 입력한 endpoint로만 요청합니다.

## 추가 반영 권장 기능

- 분석 이력 저장: 날짜, 입력 폴더, 결과 폴더, 이미지 수, 주요 추천 공정을 로그로 누적
- 파일명 규칙 프리셋: 현장별 파일명 정규식을 저장/불러오기
- 불량명 매핑 편집: `scratch`, `particle` 등 alias를 UI에서 관리
- 패턴 민감도 설정: 근거리 반경, edge/center 집중 기준, 라인성 기준 조정
- 결과 필터링: Lot, 불량 종류, 추천 공정, 위치 패턴별 필터
- 에러 파일 목록 저장: 파싱 실패 파일을 별도 CSV로 출력
- HTML 자동 서빙: 로컬 LLM 호출 안정성을 위해 `localhost` 대시보드 서버 실행
- 모델 설정 저장: LLM endpoint/model 기본값 저장
- 이미지 미리보기 확대: 대표 이미지 클릭 시 원본 이미지 열기

## 테스트

```bash
python3 -m unittest test_defect_analyzer.py
```
