# 불량 이미지 분석 프로그램 개발 요약

## 현재 상태

`defect_analyzer.py`를 중심으로 불량 이미지 폴더 분석, lot별/좌표별 집계, 원인 공정 추천, HTML 대시보드, 로컬 LLM Q&A, 데스크톱 GUI까지 구현되어 있습니다.

검증 상태:

```bash
python3 -m unittest test_defect_analyzer.py
# Ran 16 tests ... OK
```

## 핵심 실행 파일

- `defect_analyzer.py`: 메인 프로그램, CLI, GUI, agent 파이프라인, 대시보드 생성 포함
- `test_defect_analyzer.py`: 전체 기능 테스트
- `DEFECT_ANALYZER_README.md`: 실행 방법과 기능 설명

## 주요 기능

- 이미지 폴더에서 이미지 파일 자동 수집
- 파일명/폴더명 기반 lot, 불량 종류, X/Y 좌표 추출
- 학습 CSV 기반 원인 공정 추천
- 학습 데이터가 없을 경우 룰 기반 원인 공정 추천
- lot별 요약 JSON 생성
- 이미지별 분석 CSV 생성
- HTML 대시보드 생성
- 로컬 LLM Q&A 패널 제공
- tkinter 기반 데스크톱 GUI 제공
- 예시 이미지/라벨 데이터 생성

## Agent 구조

- `ImageCollectionAgent`: 이미지 파일 수집
- `MetadataExtractionAgent`: lot, 불량 종류, 좌표 추출
- `LabelingAgent`: 학습 라벨 CSV 연결
- `ModelTrainingAgent`: 추천 모델 학습
- `RecommendationAgent`: 원인 공정 추천
- `LocationPatternAgent`: 좌표 패턴 분석
- `SampleDataAgent`: 예시 데이터 생성
- `DashboardAgent`: HTML 대시보드 생성
- `ReportAgent`: CSV/JSON 리포트 생성
- `DefectAnalysisPipeline`: 전체 실행 흐름 조율

## 지원 파일명/폴더 구조

기존 예시:

```text
LOT123_scratch_x120_y340.png
lot-A123_defect-particle_x=120_y=340.jpg
A123__open__120x340.bmp
```

현장 lot 폴더 구조:

```text
MFA651601500/
  MFA651601500X-1Y-489.png
```

파싱 결과:

- `lot`: `MFA651601500`
- `x`: `1`
- `y`: `489`
- `defect_type`: 파일명/폴더에 없으면 `unknown`

불량 종류 폴더 포함 구조:

```text
MFA651601500/
  particle/
    MFA651601500X-1Y-489.png
```

## 대시보드 기능

`dashboard.html`에 포함된 기능:

- KPI
- 사각 Wafer 좌표 분포
- X/Y 표시 범위 변경
- 불량 종류 분포
- 추천 공정 분포
- lot별 요약
- 불량별 대표 이미지
- 위치 패턴 분석
- 이미지별 추천 결과
- 로컬 LLM 데이터 Q&A

대시보드 기본 표시 범위:

- X: `-1500` ~ `1500`
- Y: `-1000` ~ `1000`

위치 패턴 분석 유형:

- 동일 위치 근거리 집중 발생
- edge 또는 center 영역 집중
- 특정 3x3 구역 편중
- 가로/세로 라인성 분포
- 상승/하강 대각 분포

## 로컬 LLM Q&A

기본 설정:

```text
Endpoint: http://localhost:11434/api/chat
Model: llama3.1
```

Ollama 사용 예:

```bash
ollama pull llama3.1
ollama serve
```

## GUI 실행

```bash
python3 defect_analyzer.py --gui
```

GUI 제공 기능:

- 이미지 폴더 선택
- 결과 저장 폴더 선택
- 학습 라벨 CSV 선택
- 파일명 정규식 입력
- 좌표계 폭/높이 입력
- 대시보드 X/Y 표시 범위 입력
- 분석 시작
- 완료 후 HTML 대시보드 자동 열기

## CLI 실행 예시

```bash
python3 defect_analyzer.py ./images \
  --labels labels.csv \
  --output defect_report.csv \
  --summary-json lot_summary.json \
  --dashboard-html dashboard.html \
  --x-min -1500 \
  --x-max 1500 \
  --y-min -1000 \
  --y-max 1000
```

예시 데이터 생성:

```bash
python3 defect_analyzer.py \
  --generate-sample-data sample_defects \
  --sample-count 48 \
  --sample-lots 4 \
  --output defect_report.csv \
  --summary-json lot_summary.json \
  --dashboard-html dashboard.html
```

## 현재 산출물

- `defect_report.csv`: 이미지별 분석 결과
- `lot_summary.json`: lot별 요약
- `dashboard.html`: HTML 대시보드
- `sample_defects/`: 예시 이미지 및 `labels.csv`

## 향후 추천 개선

- 분석 이력 저장
- 파일명 규칙 프리셋 저장/불러오기
- 불량명 alias 매핑 UI
- 위치 패턴 민감도 설정
- lot/불량/공정별 결과 필터
- 파싱 실패 파일 CSV 저장
- HTML 대시보드 로컬 서버 자동 실행
- LLM endpoint/model 기본값 저장
- 대표 이미지 클릭 시 원본 확대 보기
