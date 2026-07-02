#!/usr/bin/env python3
"""Classify defect image files and recommend likely root-cause processes.

The program assumes image filenames contain at least:
  - lot id
  - defect type
  - x/y coordinates

Example filenames:
  LOTA123_scratch_x120_y340.png
  lot-A123_defect-particle_x=120_y=340.jpg
  A123__open__120x340.bmp

Optional supervised learning is supported with a CSV file containing
`filename,root_cause_process`. Without labels the recommender falls back to
domain rules based on defect type and coordinate zone.
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
DEFAULT_X_MIN = -1500.0
DEFAULT_X_MAX = 1500.0
DEFAULT_Y_MIN = -1000.0
DEFAULT_Y_MAX = 1000.0

DEFECT_ALIASES = {
    "scratch": "scratch",
    "스크래치": "scratch",
    "긁힘": "scratch",
    "particle": "particle",
    "particles": "particle",
    "dust": "particle",
    "foreign": "particle",
    "이물": "particle",
    "파티클": "particle",
    "stain": "stain",
    "watermark": "stain",
    "mark": "stain",
    "얼룩": "stain",
    "오염": "stain",
    "crack": "crack",
    "broken": "crack",
    "크랙": "crack",
    "파손": "crack",
    "open": "open",
    "오픈": "open",
    "short": "short",
    "쇼트": "short",
    "bridge": "short",
    "void": "void",
    "보이드": "void",
    "missing": "missing",
    "미형성": "missing",
}

RULE_BASED_PROCESS = {
    "scratch": ("Handling / Polishing / CMP", "스크래치 계열은 이송, 연마, CMP 접촉 조건을 우선 점검"),
    "particle": ("Cleaning / Photo / Deposition", "이물 계열은 세정, 포토, 증착 전후 청정도를 우선 점검"),
    "stain": ("Wet Clean / Dry / Chemical", "얼룩 계열은 세정액, 린스, 건조 조건을 우선 점검"),
    "crack": ("Dicing / Backgrind / Handling", "크랙 계열은 다이싱, 백그라인드, 이송 충격을 우선 점검"),
    "open": ("Lithography / Etch / Metallization", "오픈 계열은 패턴, 식각, 배선 형성 조건을 우선 점검"),
    "short": ("Lithography / Etch / Metallization", "쇼트 계열은 패턴 브리지, 식각 잔류, 배선 조건을 우선 점검"),
    "void": ("Deposition / Plating / Cure", "보이드 계열은 증착, 도금, 경화 조건을 우선 점검"),
    "missing": ("Lithography / Print / Mount", "미형성 계열은 노광, 인쇄, 마운트 조건을 우선 점검"),
}


@dataclass(frozen=True)
class DefectRecord:
    path: str
    filename: str
    lot: str
    defect_type: str
    x: float
    y: float
    zone: str
    distance_bucket: str
    root_cause_process: str = ""


@dataclass(frozen=True)
class Recommendation:
    process: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class PipelineResult:
    records: List[DefectRecord]
    skipped: List[str]
    summary: Dict[str, object]
    output_path: Path
    summary_path: Optional[Path]
    dashboard_path: Optional[Path]


class FilenameParser:
    """Extract lot, defect type, and coordinates from filename text."""

    COORD_PATTERNS = [
        re.compile(r"x[=_-]?(?P<x>-?\d+(?:\.\d+)?)\s*y[=_-]?(?P<y>-?\d+(?:\.\d+)?)", re.I),
        re.compile(r"(?:^|[_\-\s])x[=_-]?(?P<x>-?\d+(?:\.\d+)?)[_\-\s,]*y[=_-]?(?P<y>-?\d+(?:\.\d+)?)", re.I),
        re.compile(r"(?P<x>-?\d+(?:\.\d+)?)x(?P<y>-?\d+(?:\.\d+)?)(?:$|[_\-\s])", re.I),
        re.compile(r"(?:^|[_\-\s])(?P<x>-?\d+(?:\.\d+)?)[_,](?P<y>-?\d+(?:\.\d+)?)(?:$|[_\-\s])", re.I),
    ]

    LOT_PATTERNS = [
        re.compile(r"(?:^|[_\-\s])lot[=_-]?(?P<lot>[A-Za-z0-9]+)", re.I),
        re.compile(r"(?P<lot>[A-Za-z]{1,4}\d{3,}[A-Za-z0-9]*)"),
    ]

    def __init__(self, custom_regex: Optional[str] = None) -> None:
        self.custom_regex = re.compile(custom_regex, re.I) if custom_regex else None

    def parse(self, image_path: Path, wafer_width: float, wafer_height: float) -> Optional[DefectRecord]:
        stem = image_path.stem

        if self.custom_regex:
            match = self.custom_regex.search(stem)
            if not match:
                return None
            groups = match.groupdict()
            lot = groups.get("lot") or "UNKNOWN"
            defect = normalize_defect(groups.get("defect") or "unknown")
            x = float(groups["x"])
            y = float(groups["y"])
        else:
            coord = self._find_coordinates(stem)
            if not coord:
                return None
            x, y = coord
            lot = self._find_lot_from_path(image_path) or self._find_lot(stem) or "UNKNOWN"
            defect = self._find_defect(stem) or self._find_defect_from_path(image_path) or "unknown"

        zone = coordinate_zone(x, y, wafer_width, wafer_height)
        distance_bucket = radial_bucket(x, y, wafer_width, wafer_height)
        return DefectRecord(
            path=str(image_path),
            filename=image_path.name,
            lot=lot,
            defect_type=defect,
            x=x,
            y=y,
            zone=zone,
            distance_bucket=distance_bucket,
        )

    def _find_coordinates(self, text: str) -> Optional[Tuple[float, float]]:
        for pattern in self.COORD_PATTERNS:
            match = pattern.search(text)
            if match:
                return float(match.group("x")), float(match.group("y"))
        return None

    def _find_lot(self, text: str) -> Optional[str]:
        for pattern in self.LOT_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group("lot")
        return None

    def _find_lot_from_path(self, image_path: Path) -> Optional[str]:
        parent_name = image_path.parent.name
        if looks_like_lot_name(parent_name):
            return parent_name
        if normalize_known_defect(parent_name) != "unknown":
            grandparent_name = image_path.parent.parent.name
            if looks_like_lot_name(grandparent_name):
                return grandparent_name
        return None

    def _find_defect(self, text: str) -> Optional[str]:
        lowered = text.lower()

        explicit = re.search(r"(?:defect|불량)[=_-]?(?P<defect>[^_\-\s]+)", lowered, re.I)
        if explicit:
            return normalize_defect(explicit.group("defect"))

        tokens = re.split(r"[_\-\s]+", lowered)
        for token in tokens:
            normalized = normalize_known_defect(token)
            if normalized != "unknown":
                return normalized
        return None

    def _find_defect_from_path(self, image_path: Path) -> Optional[str]:
        for part in reversed(image_path.parts[:-1]):
            normalized = normalize_known_defect(part)
            if normalized != "unknown":
                return normalized
        return None


def normalize_defect(value: str) -> str:
    cleaned = value.strip().lower()
    return DEFECT_ALIASES.get(cleaned, cleaned if cleaned else "unknown")


def normalize_known_defect(value: str) -> str:
    cleaned = value.strip().lower()
    return DEFECT_ALIASES.get(cleaned, "unknown")


def looks_like_lot_name(value: str) -> bool:
    lowered = value.lower()
    if lowered in {"images", "image", "imgs", "defects", "defect", "sample_defects"}:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9]{6,}", value) and re.search(r"[A-Za-z]", value) and re.search(r"\d", value))


def coordinate_zone(x: float, y: float, width: float, height: float) -> str:
    col = "L" if x < width / 3 else "C" if x < width * 2 / 3 else "R"
    row = "T" if y < height / 3 else "M" if y < height * 2 / 3 else "B"
    return f"{row}{col}"


def radial_bucket(x: float, y: float, width: float, height: float) -> str:
    cx = width / 2
    cy = height / 2
    max_distance = math.hypot(cx, cy) or 1.0
    ratio = math.hypot(x - cx, y - cy) / max_distance
    if ratio < 0.33:
        return "center"
    if ratio < 0.66:
        return "middle"
    return "edge"


class ProcessRecommender:
    """Small categorical Naive Bayes model with rule-based fallback."""

    def __init__(self) -> None:
        self.class_counts: Counter[str] = Counter()
        self.feature_counts: Dict[str, Counter[Tuple[str, str]]] = defaultdict(Counter)
        self.feature_values: Dict[str, set[str]] = defaultdict(set)

    def train(self, records: Iterable[DefectRecord]) -> None:
        for record in records:
            if not record.root_cause_process:
                continue
            label = record.root_cause_process
            self.class_counts[label] += 1
            for name, value in self._features(record):
                self.feature_counts[label][(name, value)] += 1
                self.feature_values[name].add(value)

    def recommend(self, record: DefectRecord) -> Recommendation:
        if self.class_counts:
            return self._predict_with_model(record)
        return self._predict_with_rules(record)

    def _predict_with_model(self, record: DefectRecord) -> Recommendation:
        total = sum(self.class_counts.values())
        labels = list(self.class_counts.keys())
        scores: Dict[str, float] = {}

        for label in labels:
            score = math.log(self.class_counts[label] / total)
            for name, value in self._features(record):
                possible = max(len(self.feature_values[name]), 1)
                count = self.feature_counts[label][(name, value)]
                score += math.log((count + 1) / (self.class_counts[label] + possible))
            scores[label] = score

        max_score = max(scores.values())
        exp_scores = {label: math.exp(score - max_score) for label, score in scores.items()}
        normalizer = sum(exp_scores.values()) or 1.0
        best_label = max(exp_scores, key=exp_scores.get)
        confidence = exp_scores[best_label] / normalizer
        reason = f"학습 데이터 기준: 불량={record.defect_type}, 위치={record.zone}, 반경={record.distance_bucket}"
        return Recommendation(best_label, round(confidence, 4), reason)

    def _predict_with_rules(self, record: DefectRecord) -> Recommendation:
        process, reason = RULE_BASED_PROCESS.get(
            record.defect_type,
            ("Process Review Required", "알려진 불량명이 아니므로 공정 이력과 검사 이미지를 함께 확인"),
        )
        if record.distance_bucket == "edge":
            reason += "; 에지 집중 패턴이면 클램프, 코팅 균일도, 외곽 세정 조건도 확인"
        return Recommendation(process, 0.55 if record.defect_type != "unknown" else 0.25, reason)

    def _features(self, record: DefectRecord) -> List[Tuple[str, str]]:
        return [
            ("defect_type", record.defect_type),
            ("zone", record.zone),
            ("distance_bucket", record.distance_bucket),
        ]


def find_images(root: Path) -> List[Path]:
    if root.is_file() and root.suffix.lower() in IMAGE_EXTENSIONS:
        return [root]
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)


def read_label_map(csv_path: Optional[Path]) -> Dict[str, str]:
    if not csv_path:
        return {}

    labels: Dict[str, str] = {}
    with csv_path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required = {"filename", "root_cause_process"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"labels CSV missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            filename = (row.get("filename") or "").strip()
            process = (row.get("root_cause_process") or "").strip()
            if filename and process:
                labels[filename] = process
    return labels


def load_records(
    image_root: Path,
    parser: FilenameParser,
    labels: Dict[str, str],
    wafer_width: float,
    wafer_height: float,
) -> Tuple[List[DefectRecord], List[str]]:
    records: List[DefectRecord] = []
    skipped: List[str] = []

    for image_path in find_images(image_root):
        record = parser.parse(image_path, wafer_width, wafer_height)
        if not record:
            skipped.append(str(image_path))
            continue
        process = labels.get(record.filename) or labels.get(str(image_path))
        if process:
            record = DefectRecord(**{**asdict(record), "root_cause_process": process})
        records.append(record)
    return records, skipped


def write_report(records: Sequence[DefectRecord], recommender: ProcessRecommender, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "filename",
        "lot",
        "defect_type",
        "x",
        "y",
        "zone",
        "distance_bucket",
        "actual_root_cause_process",
        "recommended_process",
        "confidence",
        "reason",
        "path",
    ]

    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            recommendation = recommender.recommend(record)
            writer.writerow(
                {
                    "filename": record.filename,
                    "lot": record.lot,
                    "defect_type": record.defect_type,
                    "x": record.x,
                    "y": record.y,
                    "zone": record.zone,
                    "distance_bucket": record.distance_bucket,
                    "actual_root_cause_process": record.root_cause_process,
                    "recommended_process": recommendation.process,
                    "confidence": recommendation.confidence,
                    "reason": recommendation.reason,
                    "path": record.path,
                }
            )


def lot_summary(records: Sequence[DefectRecord], recommender: ProcessRecommender) -> Dict[str, object]:
    lots: Dict[str, Dict[str, object]] = {}
    for lot, lot_records in group_by_lot(records).items():
        defect_counts = Counter(record.defect_type for record in lot_records)
        zone_counts = Counter(record.zone for record in lot_records)
        process_counts = Counter(recommender.recommend(record).process for record in lot_records)
        lots[lot] = {
            "image_count": len(lot_records),
            "top_defects": defect_counts.most_common(5),
            "top_zones": zone_counts.most_common(5),
            "recommended_processes": process_counts.most_common(3),
        }
    return {"lot_count": len(lots), "image_count": len(records), "lots": lots}


def group_by_lot(records: Sequence[DefectRecord]) -> Dict[str, List[DefectRecord]]:
    grouped: Dict[str, List[DefectRecord]] = defaultdict(list)
    for record in records:
        grouped[record.lot].append(record)
    return dict(grouped)


class SampleDataAgent:
    """Create deterministic sample defect image filenames and labels."""

    PNG_1X1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )

    DEFECT_PROCESS_HINTS = {
        "scratch": "CMP",
        "particle": "Cleaning",
        "stain": "Wet Clean",
        "crack": "Dicing",
        "open": "Etch",
        "short": "Lithography",
        "void": "Deposition",
        "missing": "Print",
    }

    def generate(
        self,
        output_dir: Path,
        count: int,
        lot_count: int,
        wafer_width: float,
        wafer_height: float,
        seed: int = 17,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        labels_path = output_dir / "labels.csv"
        rng = random.Random(seed)
        defects = list(self.DEFECT_PROCESS_HINTS.keys())
        lots = [f"LOT{idx:03d}" for idx in range(1, lot_count + 1)]

        with labels_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=["filename", "root_cause_process"])
            writer.writeheader()

            for index in range(count):
                lot = lots[index % len(lots)]
                defect = defects[(index + rng.randrange(len(defects))) % len(defects)]
                x, y = self._sample_coordinate(defect, wafer_width, wafer_height, rng)
                filename = f"{lot}_{defect}_x{int(x)}_y{int(y)}.png"
                (output_dir / filename).write_bytes(self.PNG_1X1)
                writer.writerow(
                    {
                        "filename": filename,
                        "root_cause_process": self.DEFECT_PROCESS_HINTS[defect],
                    }
                )

        return labels_path

    def _sample_coordinate(
        self,
        defect: str,
        wafer_width: float,
        wafer_height: float,
        rng: random.Random,
    ) -> Tuple[float, float]:
        if defect in {"particle", "stain"}:
            center_x = wafer_width * 0.5
            center_y = wafer_height * 0.5
            x = rng.gauss(center_x, wafer_width * 0.18)
            y = rng.gauss(center_y, wafer_height * 0.18)
        elif defect in {"scratch", "crack"}:
            edge = rng.choice(["left", "right", "top", "bottom"])
            if edge == "left":
                x, y = rng.uniform(0, wafer_width * 0.15), rng.uniform(0, wafer_height)
            elif edge == "right":
                x, y = rng.uniform(wafer_width * 0.85, wafer_width), rng.uniform(0, wafer_height)
            elif edge == "top":
                x, y = rng.uniform(0, wafer_width), rng.uniform(0, wafer_height * 0.15)
            else:
                x, y = rng.uniform(0, wafer_width), rng.uniform(wafer_height * 0.85, wafer_height)
        else:
            x = rng.uniform(wafer_width * 0.1, wafer_width * 0.9)
            y = rng.uniform(wafer_height * 0.1, wafer_height * 0.9)

        return clamp(x, 0, wafer_width), clamp(y, 0, wafer_height)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def format_common(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return ", ".join(f"{label}({count})" for label, count in items[:3])


class ImageCollectionAgent:
    """Collect image files from a file or directory input."""

    def collect(self, image_root: Path) -> List[Path]:
        return find_images(image_root)


class MetadataExtractionAgent:
    """Parse lot, defect type, and coordinates from image filenames."""

    def __init__(self, filename_regex: Optional[str], wafer_width: float, wafer_height: float) -> None:
        self.parser = FilenameParser(filename_regex)
        self.wafer_width = wafer_width
        self.wafer_height = wafer_height

    def extract(self, image_paths: Iterable[Path]) -> Tuple[List[DefectRecord], List[str]]:
        records: List[DefectRecord] = []
        skipped: List[str] = []

        for image_path in image_paths:
            record = self.parser.parse(image_path, self.wafer_width, self.wafer_height)
            if not record:
                skipped.append(str(image_path))
                continue
            records.append(record)

        return records, skipped


class LabelingAgent:
    """Load process labels and attach them to parsed defect records."""

    def __init__(self, labels_path: Optional[Path]) -> None:
        self.labels = read_label_map(labels_path)

    def apply(self, records: Iterable[DefectRecord]) -> List[DefectRecord]:
        labeled: List[DefectRecord] = []

        for record in records:
            process = self.labels.get(record.filename) or self.labels.get(record.path)
            if process:
                record = DefectRecord(**{**asdict(record), "root_cause_process": process})
            labeled.append(record)

        return labeled


class ModelTrainingAgent:
    """Train the process recommendation model from labeled records."""

    def train(self, records: Iterable[DefectRecord]) -> ProcessRecommender:
        recommender = ProcessRecommender()
        recommender.train(records)
        return recommender


class RecommendationAgent:
    """Generate process recommendations for individual records."""

    def __init__(self, recommender: ProcessRecommender) -> None:
        self.recommender = recommender

    def recommend(self, record: DefectRecord) -> Recommendation:
        return self.recommender.recommend(record)


class LocationPatternAgent:
    """Detect spatial defect patterns from parsed coordinates."""

    def analyze(self, records: Sequence[DefectRecord], wafer_width: float, wafer_height: float) -> List[Dict[str, object]]:
        findings: List[Dict[str, object]] = []
        groups: Dict[str, List[DefectRecord]] = defaultdict(list)
        groups["전체"] = list(records)
        for record in records:
            groups[record.defect_type].append(record)

        for defect_type, group_records in groups.items():
            if len(group_records) < 3:
                continue
            findings.extend(self._cluster_findings(defect_type, group_records, wafer_width, wafer_height))
            findings.extend(self._distribution_findings(defect_type, group_records))
            line_finding = self._line_finding(defect_type, group_records, wafer_width, wafer_height)
            if line_finding:
                findings.append(line_finding)

        findings.sort(key=lambda item: (-float(item["score"]), str(item["defect_type"]), str(item["pattern"])))
        return findings[:12]

    def _cluster_findings(
        self,
        defect_type: str,
        records: Sequence[DefectRecord],
        wafer_width: float,
        wafer_height: float,
    ) -> List[Dict[str, object]]:
        diagonal = math.hypot(wafer_width, wafer_height) or 1.0
        radius = diagonal * 0.08
        best_record: Optional[DefectRecord] = None
        best_neighbors: List[DefectRecord] = []

        for record in records:
            neighbors = [
                other
                for other in records
                if math.hypot(record.x - other.x, record.y - other.y) <= radius
            ]
            if len(neighbors) > len(best_neighbors):
                best_record = record
                best_neighbors = neighbors

        minimum_cluster = max(3, math.ceil(len(records) * 0.25))
        if not best_record or len(best_neighbors) < minimum_cluster:
            return []

        center_x = sum(record.x for record in best_neighbors) / len(best_neighbors)
        center_y = sum(record.y for record in best_neighbors) / len(best_neighbors)
        ratio = len(best_neighbors) / len(records)
        return [
            {
                "defect_type": defect_type,
                "pattern": "근거리 집중 발생",
                "score": round(ratio, 3),
                "evidence": f"{len(records)}건 중 {len(best_neighbors)}건이 반경 {radius:.0f} 이내 집중, 중심 좌표 ({center_x:.0f}, {center_y:.0f})",
                "action": "해당 좌표 주변 반복 결함이면 stage 정렬, mask/fixture 접촉, 국부 오염원을 우선 확인",
            }
        ]

    def _distribution_findings(self, defect_type: str, records: Sequence[DefectRecord]) -> List[Dict[str, object]]:
        findings: List[Dict[str, object]] = []
        bucket_counts = Counter(record.distance_bucket for record in records)
        zone_counts = Counter(record.zone for record in records)

        edge_count = bucket_counts.get("edge", 0)
        if edge_count >= 3 and edge_count / len(records) >= 0.55:
            findings.append(
                {
                    "defect_type": defect_type,
                    "pattern": "에지 집중 분포",
                    "score": round(edge_count / len(records), 3),
                    "evidence": f"{len(records)}건 중 {edge_count}건이 edge 영역",
                    "action": "외곽 클램프, 코팅 균일도, 에지 세정/건조 조건을 확인",
                }
            )

        center_count = bucket_counts.get("center", 0)
        if center_count >= 3 and center_count / len(records) >= 0.55:
            findings.append(
                {
                    "defect_type": defect_type,
                    "pattern": "중앙 집중 분포",
                    "score": round(center_count / len(records), 3),
                    "evidence": f"{len(records)}건 중 {center_count}건이 center 영역",
                    "action": "중앙부 압력, 노광/증착 균일도, chuck 평탄도를 확인",
                }
            )

        zone, zone_count = zone_counts.most_common(1)[0]
        if zone_count >= 3 and zone_count / len(records) >= 0.45:
            findings.append(
                {
                    "defect_type": defect_type,
                    "pattern": "특정 구역 편중",
                    "score": round(zone_count / len(records), 3),
                    "evidence": f"{len(records)}건 중 {zone_count}건이 {zone} 구역",
                    "action": "해당 구역의 장비 좌표계, 이송 방향, 공정 recipe zone 보정값을 확인",
                }
            )

        return findings

    def _line_finding(
        self,
        defect_type: str,
        records: Sequence[DefectRecord],
        wafer_width: float,
        wafer_height: float,
    ) -> Optional[Dict[str, object]]:
        if len(records) < 4:
            return None

        xs = [record.x for record in records]
        ys = [record.y for record in records]
        x_span = max(xs) - min(xs)
        y_span = max(ys) - min(ys)

        if x_span <= wafer_width * 0.08:
            return {
                "defect_type": defect_type,
                "pattern": "세로 라인성 분포",
                "score": round(1 - x_span / (wafer_width or 1), 3),
                "evidence": f"X 좌표 폭 {x_span:.0f}, Y 방향으로 {len(records)}건 분포",
                "action": "스캔 라인, 노즐/슬릿, 세로 방향 stage 이동 이상을 확인",
            }
        if y_span <= wafer_height * 0.08:
            return {
                "defect_type": defect_type,
                "pattern": "가로 라인성 분포",
                "score": round(1 - y_span / (wafer_height or 1), 3),
                "evidence": f"Y 좌표 폭 {y_span:.0f}, X 방향으로 {len(records)}건 분포",
                "action": "스캔 라인, 롤러/블레이드, 가로 방향 이송 흔적을 확인",
            }

        correlation = pearson_correlation(xs, ys)
        if abs(correlation) >= 0.82:
            direction = "상승 대각" if correlation > 0 else "하강 대각"
            return {
                "defect_type": defect_type,
                "pattern": f"{direction} 분포",
                "score": round(abs(correlation), 3),
                "evidence": f"좌표 상관계수 {correlation:.2f}, {len(records)}건이 대각 방향으로 정렬",
                "action": "대각 방향 scratch/flow mark 가능성, stage skew, 세정 유체 흐름 방향을 확인",
            }

        return None


def pearson_correlation(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denominator_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    denominator_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    denominator = denominator_x * denominator_y
    return numerator / denominator if denominator else 0.0


class DashboardAgent:
    """Render a standalone HTML dashboard from analysis results."""

    def write(
        self,
        records: Sequence[DefectRecord],
        recommender: ProcessRecommender,
        summary: Dict[str, object],
        dashboard_path: Optional[Path],
        wafer_width: float,
        wafer_height: float,
        x_min: float = DEFAULT_X_MIN,
        x_max: float = DEFAULT_X_MAX,
        y_min: float = DEFAULT_Y_MIN,
        y_max: float = DEFAULT_Y_MAX,
    ) -> None:
        if not dashboard_path:
            return

        dashboard_path.parent.mkdir(parents=True, exist_ok=True)
        dashboard_path.write_text(
            self._render(records, recommender, summary, dashboard_path, wafer_width, wafer_height, x_min, x_max, y_min, y_max),
            encoding="utf-8",
        )

    def _render(
        self,
        records: Sequence[DefectRecord],
        recommender: ProcessRecommender,
        summary: Dict[str, object],
        dashboard_path: Path,
        wafer_width: float,
        wafer_height: float,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
    ) -> str:
        defect_counts = Counter(record.defect_type for record in records)
        lot_counts = Counter(record.lot for record in records)
        zone_counts = Counter(record.zone for record in records)
        process_counts = Counter(recommender.recommend(record).process for record in records)
        avg_confidence = self._average_confidence(records, recommender)
        top_lot = lot_counts.most_common(1)[0][0] if lot_counts else "-"
        top_process = process_counts.most_common(1)[0][0] if process_counts else "-"

        lot_rows = self._render_lot_rows(summary)
        recent_rows = self._render_record_rows(records, recommender)
        representative_images = self._render_representative_images(records, recommender, dashboard_path)
        location_patterns = self._render_location_patterns(records, wafer_width, wafer_height)
        llm_context = self._llm_context_json(
            records,
            recommender,
            summary,
            defect_counts,
            zone_counts,
            process_counts,
            wafer_width,
            wafer_height,
            x_min,
            x_max,
            y_min,
            y_max,
        )
        scatter = self._render_scatter(records, x_min, x_max, y_min, y_max)

        return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Defect Analysis Dashboard</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #667085;
      --line: #d8dee8;
      --accent: #0f766e;
      --accent-2: #b42318;
      --accent-3: #475467;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Arial, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
    }}
    header {{
      padding: 24px 28px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{ margin: 0; font-size: 24px; font-weight: 700; }}
    .subtitle {{ margin-top: 6px; color: var(--muted); font-size: 13px; }}
    main {{ padding: 20px 28px 32px; display: grid; gap: 18px; }}
    .kpis {{ display: grid; grid-template-columns: repeat(5, minmax(140px, 1fr)); gap: 12px; }}
    .kpi, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    .kpi .label {{ color: var(--muted); font-size: 12px; }}
    .kpi .value {{ margin-top: 8px; font-size: 22px; font-weight: 700; }}
    .grid {{ display: grid; grid-template-columns: 1.05fr 0.95fr; gap: 18px; align-items: start; }}
    h2 {{ margin: 0 0 12px; font-size: 16px; }}
    .bar {{ display: grid; grid-template-columns: 120px 1fr 42px; gap: 10px; align-items: center; margin: 9px 0; }}
    .bar-label {{ color: var(--accent-3); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .bar-track {{ height: 10px; background: #edf1f5; border-radius: 999px; overflow: hidden; }}
    .bar-fill {{ height: 100%; background: var(--accent); }}
    .bar-value {{ text-align: right; color: var(--muted); font-size: 12px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{ padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 700; background: #fafbfc; }}
    .wafer-wrap {{ display: flex; justify-content: center; }}
    .wafer-map {{ width: min(100%, 560px); height: auto; }}
    .axis-controls {{ display: grid; grid-template-columns: repeat(5, minmax(90px, 1fr)); gap: 8px; margin-bottom: 12px; }}
    .axis-control label {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
    .axis-control input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 8px;
      font: inherit;
    }}
    .axis-controls button {{
      align-self: end;
      border: 0;
      border-radius: 6px;
      padding: 8px 10px;
      background: var(--accent-3);
      color: #ffffff;
      font-weight: 700;
      cursor: pointer;
    }}
    .representatives {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
    .representative {{
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: #ffffff;
    }}
    .representative img {{
      display: block;
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: contain;
      background: #eef2f6;
      image-rendering: pixelated;
    }}
    .representative-body {{ padding: 10px; }}
    .representative-title {{ font-size: 13px; font-weight: 700; margin-bottom: 4px; }}
    .representative-meta {{ color: var(--muted); font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; }}
    .score {{
      display: inline-block;
      min-width: 44px;
      padding: 3px 7px;
      border-radius: 999px;
      background: #ecfdf3;
      color: #027a48;
      font-size: 12px;
      font-weight: 700;
      text-align: center;
    }}
    .llm-panel {{ display: grid; gap: 12px; }}
    .llm-controls {{ display: grid; grid-template-columns: 1.2fr 0.8fr; gap: 10px; }}
    .llm-control label {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 5px; }}
    .llm-control input, .llm-panel textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
      color: var(--ink);
      background: #ffffff;
    }}
    .llm-panel textarea {{ min-height: 82px; resize: vertical; }}
    .llm-actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .llm-actions button {{
      border: 0;
      border-radius: 6px;
      padding: 9px 13px;
      background: var(--accent);
      color: #ffffff;
      font-weight: 700;
      cursor: pointer;
    }}
    .llm-actions button:disabled {{ opacity: 0.55; cursor: wait; }}
    .llm-answer {{
      min-height: 84px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fafbfc;
      white-space: pre-wrap;
      line-height: 1.5;
      font-size: 13px;
    }}
    .small {{ color: var(--muted); font-size: 12px; }}
    @media (max-width: 980px) {{
      .kpis, .grid, .llm-controls, .axis-controls {{ grid-template-columns: 1fr; }}
      main, header {{ padding-left: 16px; padding-right: 16px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Defect Analysis Dashboard</h1>
    <div class="subtitle">파일명 좌표 기반 불량 분류, 랏별 집계, 원인 공정 추천 결과</div>
  </header>
  <main>
    <div class="kpis">
      {self._kpi("이미지", len(records))}
      {self._kpi("랏", summary.get("lot_count", 0))}
      {self._kpi("평균 신뢰도", f"{avg_confidence:.1%}")}
      {self._kpi("주요 랏", top_lot)}
      {self._kpi("주요 추천 공정", top_process)}
    </div>
    <div class="grid">
      <section>
        <h2>사각 Wafer 좌표 분포</h2>
        <div class="axis-controls">
          <div class="axis-control"><label for="x-min">X min</label><input id="x-min" type="number" value="{x_min:g}"></div>
          <div class="axis-control"><label for="x-max">X max</label><input id="x-max" type="number" value="{x_max:g}"></div>
          <div class="axis-control"><label for="y-min">Y min</label><input id="y-min" type="number" value="{y_min:g}"></div>
          <div class="axis-control"><label for="y-max">Y max</label><input id="y-max" type="number" value="{y_max:g}"></div>
          <button id="axis-apply-button" type="button">범위 적용</button>
        </div>
        <div class="wafer-wrap">{scatter}</div>
      </section>
      <section>
        <h2>불량 종류 분포</h2>
        {self._render_bars(defect_counts)}
        <h2 style="margin-top:18px;">추천 공정 분포</h2>
        {self._render_bars(process_counts)}
      </section>
    </div>
    <div class="grid">
      <section>
        <h2>랏별 요약</h2>
        <table>
          <thead><tr><th>Lot</th><th>이미지</th><th>주요 불량</th><th>주요 위치</th><th>추천 공정</th></tr></thead>
          <tbody>{lot_rows}</tbody>
        </table>
      </section>
      <section>
        <h2>좌표 구역 분포</h2>
        {self._render_bars(zone_counts)}
      </section>
    </div>
    <section>
      <h2>불량별 대표 이미지</h2>
      <div class="representatives">{representative_images}</div>
    </section>
    <section>
      <h2>위치 패턴 분석</h2>
      {location_patterns}
    </section>
    <section>
      <h2>로컬 LLM 데이터 Q&amp;A</h2>
      <div class="llm-panel">
        <div class="llm-controls">
          <div class="llm-control">
            <label for="llm-endpoint">Endpoint</label>
            <input id="llm-endpoint" value="http://localhost:11434/api/chat">
          </div>
          <div class="llm-control">
            <label for="llm-model">Model</label>
            <input id="llm-model" value="llama3.1">
          </div>
        </div>
        <textarea id="llm-question" placeholder="예: 가장 의심되는 원인 공정과 근거를 요약해줘"></textarea>
        <div class="llm-actions">
          <button id="llm-ask-button" type="button">질문하기</button>
          <span class="small">Ollama 호환 로컬 API를 사용합니다. 답변은 이 HTML에 포함된 집계 데이터 기준입니다.</span>
        </div>
        <div id="llm-answer" class="llm-answer">질문을 입력한 뒤 질문하기를 누르세요.</div>
      </div>
    </section>
    <section>
      <h2>이미지별 추천 결과</h2>
      <table>
        <thead><tr><th>파일명</th><th>Lot</th><th>불량</th><th>좌표</th><th>Zone</th><th>추천 공정</th><th>신뢰도</th></tr></thead>
        <tbody>{recent_rows}</tbody>
      </table>
    </section>
  </main>
  <script id="analysis-context" type="application/json">{llm_context}</script>
  <script>
    const analysisContext = JSON.parse(document.getElementById("analysis-context").textContent);
    const endpointInput = document.getElementById("llm-endpoint");
    const modelInput = document.getElementById("llm-model");
    const questionInput = document.getElementById("llm-question");
    const askButton = document.getElementById("llm-ask-button");
    const answerBox = document.getElementById("llm-answer");
    const axisButton = document.getElementById("axis-apply-button");

    function applyAxisRange() {{
      const xMin = Number(document.getElementById("x-min").value);
      const xMax = Number(document.getElementById("x-max").value);
      const yMin = Number(document.getElementById("y-min").value);
      const yMax = Number(document.getElementById("y-max").value);
      if (![xMin, xMax, yMin, yMax].every(Number.isFinite) || xMax <= xMin || yMax <= yMin) {{
        alert("좌표 범위를 확인하세요. max 값은 min 값보다 커야 합니다.");
        return;
      }}
      document.querySelectorAll(".defect-point").forEach((point) => {{
        const rawX = Number(point.dataset.x);
        const rawY = Number(point.dataset.y);
        const cx = 40 + ((rawX - xMin) / (xMax - xMin)) * 420;
        const cy = 40 + ((rawY - yMin) / (yMax - yMin)) * 420;
        point.setAttribute("cx", String(Math.max(40, Math.min(460, cx))));
        point.setAttribute("cy", String(Math.max(40, Math.min(460, cy))));
        point.style.opacity = rawX < xMin || rawX > xMax || rawY < yMin || rawY > yMax ? "0.18" : "1";
      }});
    }}

    function buildPrompt(question) {{
      return [
        "너는 반도체/디스플레이 불량 분석 엔지니어다.",
        "아래 JSON 데이터만 근거로 답변하고, 모르는 내용은 추정이라고 명시한다.",
        "답변은 한국어로, 근거와 추천 확인 공정을 짧게 구분해서 작성한다.",
        "",
        "분석 데이터 JSON:",
        JSON.stringify(analysisContext, null, 2),
        "",
        "질문:",
        question
      ].join("\\n");
    }}

    async function askLocalLlm() {{
      const question = questionInput.value.trim();
      if (!question) {{
        answerBox.textContent = "질문을 입력하세요.";
        return;
      }}

      askButton.disabled = true;
      answerBox.textContent = "로컬 LLM 응답 대기 중...";
      try {{
        const response = await fetch(endpointInput.value.trim(), {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            model: modelInput.value.trim() || "llama3.1",
            stream: false,
            messages: [
              {{ role: "system", content: "You answer manufacturing defect analysis questions using only the supplied dashboard data." }},
              {{ role: "user", content: buildPrompt(question) }}
            ]
          }})
        }});
        if (!response.ok) {{
          throw new Error(`HTTP ${{response.status}} ${{response.statusText}}`);
        }}
        const data = await response.json();
        answerBox.textContent = data?.message?.content || data?.response || "응답 본문을 찾지 못했습니다.";
      }} catch (error) {{
        answerBox.textContent = [
          "로컬 LLM 호출 실패:",
          String(error),
          "",
          "확인:",
          "- Ollama가 실행 중인지 확인: ollama serve",
          "- 모델이 설치되어 있는지 확인: ollama pull llama3.1",
          "- 브라우저에서 localhost API 접근이 허용되는지 확인"
        ].join("\\n");
      }} finally {{
        askButton.disabled = false;
      }}
    }}

    askButton.addEventListener("click", askLocalLlm);
    axisButton.addEventListener("click", applyAxisRange);
    questionInput.addEventListener("keydown", (event) => {{
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {{
        askLocalLlm();
      }}
    }});
  </script>
</body>
</html>
"""

    def _kpi(self, label: str, value: object) -> str:
        return f'<div class="kpi"><div class="label">{escape(label)}</div><div class="value">{escape(value)}</div></div>'

    def _render_bars(self, counts: Counter[str]) -> str:
        if not counts:
            return '<div class="small">데이터 없음</div>'
        maximum = max(counts.values()) or 1
        rows = []
        for label, value in counts.most_common(8):
            width = value / maximum * 100
            rows.append(
                '<div class="bar">'
                f'<div class="bar-label" title="{escape(label)}">{escape(label)}</div>'
                '<div class="bar-track">'
                f'<div class="bar-fill" style="width:{width:.1f}%"></div>'
                "</div>"
                f'<div class="bar-value">{value}</div>'
                "</div>"
            )
        return "".join(rows)

    def _render_lot_rows(self, summary: Dict[str, object]) -> str:
        lots = summary.get("lots", {})
        if not isinstance(lots, dict) or not lots:
            return '<tr><td colspan="5">데이터 없음</td></tr>'

        rows = []
        for lot, lot_data in sorted(lots.items()):
            if not isinstance(lot_data, dict):
                continue
            rows.append(
                "<tr>"
                f"<td>{escape(lot)}</td>"
                f"<td>{escape(lot_data.get('image_count', 0))}</td>"
                f"<td>{escape(format_common(lot_data.get('top_defects', [])))}</td>"
                f"<td>{escape(format_common(lot_data.get('top_zones', [])))}</td>"
                f"<td>{escape(format_common(lot_data.get('recommended_processes', [])))}</td>"
                "</tr>"
            )
        return "".join(rows)

    def _render_record_rows(self, records: Sequence[DefectRecord], recommender: ProcessRecommender) -> str:
        if not records:
            return '<tr><td colspan="7">데이터 없음</td></tr>'

        rows = []
        for record in records[:200]:
            recommendation = recommender.recommend(record)
            rows.append(
                "<tr>"
                f"<td>{escape(record.filename)}</td>"
                f"<td>{escape(record.lot)}</td>"
                f"<td>{escape(record.defect_type)}</td>"
                f"<td>{record.x:.0f}, {record.y:.0f}</td>"
                f"<td>{escape(record.zone)}</td>"
                f"<td>{escape(recommendation.process)}</td>"
                f"<td>{recommendation.confidence:.1%}</td>"
                "</tr>"
            )
        return "".join(rows)

    def _render_representative_images(
        self,
        records: Sequence[DefectRecord],
        recommender: ProcessRecommender,
        dashboard_path: Path,
    ) -> str:
        representatives: Dict[str, DefectRecord] = {}
        for record in records:
            representatives.setdefault(record.defect_type, record)

        if not representatives:
            return '<div class="small">데이터 없음</div>'

        cards = []
        for defect_type, record in sorted(representatives.items()):
            recommendation = recommender.recommend(record)
            image_src = self._image_src(record.path, dashboard_path)
            cards.append(
                '<div class="representative">'
                f'<img src="{escape(image_src)}" alt="{escape(defect_type)} representative image">'
                '<div class="representative-body">'
                f'<div class="representative-title">{escape(defect_type)}</div>'
                '<div class="representative-meta">'
                f"파일: {escape(record.filename)}<br>"
                f"Lot: {escape(record.lot)} / 좌표: {record.x:.0f}, {record.y:.0f}<br>"
                f"추천 공정: {escape(recommendation.process)}"
                "</div>"
                "</div>"
                "</div>"
            )
        return "".join(cards)

    def _image_src(self, image_path: str, dashboard_path: Path) -> str:
        path = Path(image_path)
        try:
            return path.resolve().relative_to(dashboard_path.parent.resolve()).as_posix()
        except ValueError:
            return path.resolve().as_uri()

    def _render_location_patterns(
        self,
        records: Sequence[DefectRecord],
        wafer_width: float,
        wafer_height: float,
    ) -> str:
        findings = LocationPatternAgent().analyze(records, wafer_width, wafer_height)
        if not findings:
            return '<div class="small">탐지된 위치 패턴 없음</div>'

        rows = []
        for finding in findings:
            rows.append(
                "<tr>"
                f"<td>{escape(finding['defect_type'])}</td>"
                f"<td>{escape(finding['pattern'])}</td>"
                f"<td><span class=\"score\">{float(finding['score']):.0%}</span></td>"
                f"<td>{escape(finding['evidence'])}</td>"
                f"<td>{escape(finding['action'])}</td>"
                "</tr>"
            )
        return (
            "<table>"
            "<thead><tr><th>대상</th><th>패턴</th><th>강도</th><th>근거</th><th>확인 포인트</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
        )

    def _llm_context_json(
        self,
        records: Sequence[DefectRecord],
        recommender: ProcessRecommender,
        summary: Dict[str, object],
        defect_counts: Counter[str],
        zone_counts: Counter[str],
        process_counts: Counter[str],
        wafer_width: float,
        wafer_height: float,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
    ) -> str:
        location_findings = LocationPatternAgent().analyze(records, wafer_width, wafer_height)
        sample_records = []
        for record in records[:80]:
            recommendation = recommender.recommend(record)
            sample_records.append(
                {
                    "filename": record.filename,
                    "lot": record.lot,
                    "defect_type": record.defect_type,
                    "x": record.x,
                    "y": record.y,
                    "zone": record.zone,
                    "distance_bucket": record.distance_bucket,
                    "actual_root_cause_process": record.root_cause_process,
                    "recommended_process": recommendation.process,
                    "confidence": recommendation.confidence,
                }
            )

        context = {
            "image_count": len(records),
            "lot_count": summary.get("lot_count", 0),
            "wafer_shape": "rectangle",
            "coordinate_system": {"width": wafer_width, "height": wafer_height},
            "dashboard_axis_range": {
                "x_min": x_min,
                "x_max": x_max,
                "y_min": y_min,
                "y_max": y_max,
            },
            "defect_counts": dict(defect_counts.most_common()),
            "zone_counts": dict(zone_counts.most_common()),
            "recommended_process_counts": dict(process_counts.most_common()),
            "location_pattern_findings": location_findings,
            "lot_summary": summary.get("lots", {}),
            "sample_records": sample_records,
            "limits": {
                "sample_records": len(sample_records),
                "note": "sample_records는 HTML payload를 줄이기 위해 최대 80건만 포함합니다.",
            },
        }
        return json.dumps(context, ensure_ascii=False).replace("</", "<\\/")

    def _render_scatter(
        self,
        records: Sequence[DefectRecord],
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
    ) -> str:
        points = []
        colors = {
            "scratch": "#b42318",
            "particle": "#0f766e",
            "stain": "#b54708",
            "crack": "#7a271a",
            "open": "#175cd3",
            "short": "#5925dc",
            "void": "#667085",
            "missing": "#027a48",
        }
        x_span = x_max - x_min if x_max > x_min else 1.0
        y_span = y_max - y_min if y_max > y_min else 1.0
        for record in records:
            raw_x = 40 + ((record.x - x_min) / x_span) * 420
            raw_y = 40 + ((record.y - y_min) / y_span) * 420
            x = clamp(raw_x, 40, 460)
            y = clamp(raw_y, 40, 460)
            opacity = "0.18" if raw_x < 40 or raw_x > 460 or raw_y < 40 or raw_y > 460 else "1"
            color = colors.get(record.defect_type, "#344054")
            points.append(
                f'<circle class="defect-point" data-x="{record.x:.6g}" data-y="{record.y:.6g}" '
                f'cx="{x:.1f}" cy="{y:.1f}" r="4.2" fill="{color}" opacity="{opacity}">'
                f"<title>{escape(record.filename)} | {escape(record.defect_type)} | {record.x:.0f},{record.y:.0f}</title>"
                "</circle>"
            )
        return (
            '<svg class="wafer-map" viewBox="0 0 500 500" role="img" aria-label="rectangular wafer defect scatter plot">'
            '<rect x="40" y="40" width="420" height="420" rx="0" fill="#f8fafc" stroke="#98a2b3" stroke-width="2"/>'
            '<line x1="180" y1="40" x2="180" y2="460" stroke="#d0d5dd" stroke-width="1"/>'
            '<line x1="320" y1="40" x2="320" y2="460" stroke="#d0d5dd" stroke-width="1"/>'
            '<line x1="40" y1="180" x2="460" y2="180" stroke="#d0d5dd" stroke-width="1"/>'
            '<line x1="40" y1="320" x2="460" y2="320" stroke="#d0d5dd" stroke-width="1"/>'
            f"{''.join(points)}"
            "</svg>"
        )

    def _average_confidence(self, records: Sequence[DefectRecord], recommender: ProcessRecommender) -> float:
        if not records:
            return 0.0
        return sum(recommender.recommend(record).confidence for record in records) / len(records)


class ReportAgent:
    """Write image-level and lot-level analysis outputs."""

    def write(
        self,
        records: Sequence[DefectRecord],
        recommender: ProcessRecommender,
        output_path: Path,
        summary_path: Optional[Path],
    ) -> Dict[str, object]:
        write_report(records, recommender, output_path)
        summary = lot_summary(records, recommender)

        if summary_path:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        return summary


class DefectAnalysisPipeline:
    """Coordinate the functional agents for end-to-end defect analysis."""

    def __init__(
        self,
        image_collection_agent: ImageCollectionAgent,
        metadata_extraction_agent: MetadataExtractionAgent,
        labeling_agent: LabelingAgent,
        model_training_agent: ModelTrainingAgent,
        report_agent: ReportAgent,
        dashboard_agent: DashboardAgent,
        wafer_width: float,
        wafer_height: float,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
    ) -> None:
        self.image_collection_agent = image_collection_agent
        self.metadata_extraction_agent = metadata_extraction_agent
        self.labeling_agent = labeling_agent
        self.model_training_agent = model_training_agent
        self.report_agent = report_agent
        self.dashboard_agent = dashboard_agent
        self.wafer_width = wafer_width
        self.wafer_height = wafer_height
        self.x_min = x_min
        self.x_max = x_max
        self.y_min = y_min
        self.y_max = y_max

    @classmethod
    def from_options(
        cls,
        filename_regex: Optional[str],
        labels_path: Optional[Path],
        wafer_width: float,
        wafer_height: float,
        x_min: float = DEFAULT_X_MIN,
        x_max: float = DEFAULT_X_MAX,
        y_min: float = DEFAULT_Y_MIN,
        y_max: float = DEFAULT_Y_MAX,
    ) -> "DefectAnalysisPipeline":
        return cls(
            image_collection_agent=ImageCollectionAgent(),
            metadata_extraction_agent=MetadataExtractionAgent(filename_regex, wafer_width, wafer_height),
            labeling_agent=LabelingAgent(labels_path),
            model_training_agent=ModelTrainingAgent(),
            report_agent=ReportAgent(),
            dashboard_agent=DashboardAgent(),
            wafer_width=wafer_width,
            wafer_height=wafer_height,
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
        )

    def run(
        self,
        image_root: Path,
        output_path: Path,
        summary_path: Optional[Path],
        dashboard_path: Optional[Path] = None,
    ) -> PipelineResult:
        image_paths = self.image_collection_agent.collect(image_root)
        records, skipped = self.metadata_extraction_agent.extract(image_paths)
        records = self.labeling_agent.apply(records)
        recommender = self.model_training_agent.train(records)
        summary = self.report_agent.write(records, recommender, output_path, summary_path)
        self.dashboard_agent.write(
            records,
            recommender,
            summary,
            dashboard_path,
            self.wafer_width,
            self.wafer_height,
            self.x_min,
            self.x_max,
            self.y_min,
            self.y_max,
        )

        return PipelineResult(
            records=records,
            skipped=skipped,
            summary=summary,
            output_path=output_path,
            summary_path=summary_path,
            dashboard_path=dashboard_path,
        )


def run_analysis_to_output_dir(
    image_root: Path,
    output_dir: Path,
    labels_path: Optional[Path] = None,
    filename_regex: Optional[str] = None,
    wafer_width: float = 1000.0,
    wafer_height: float = 1000.0,
    x_min: float = DEFAULT_X_MIN,
    x_max: float = DEFAULT_X_MAX,
    y_min: float = DEFAULT_Y_MIN,
    y_max: float = DEFAULT_Y_MAX,
) -> PipelineResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    pipeline = DefectAnalysisPipeline.from_options(
        filename_regex=filename_regex,
        labels_path=labels_path,
        wafer_width=wafer_width,
        wafer_height=wafer_height,
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
    )
    return pipeline.run(
        image_root=image_root,
        output_path=output_dir / "defect_report.csv",
        summary_path=output_dir / "lot_summary.json",
        dashboard_path=output_dir / "dashboard.html",
    )


def launch_gui() -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        print(f"GUI를 실행할 수 없습니다: tkinter 미설치 ({exc})", file=sys.stderr)
        return 2

    root = tk.Tk()
    root.title("Defect Analyzer")
    root.geometry("760x520")

    image_dir_var = tk.StringVar()
    output_dir_var = tk.StringVar()
    labels_var = tk.StringVar()
    regex_var = tk.StringVar()
    wafer_width_var = tk.StringVar(value="1000")
    wafer_height_var = tk.StringVar(value="1000")
    x_min_var = tk.StringVar(value=f"{DEFAULT_X_MIN:g}")
    x_max_var = tk.StringVar(value=f"{DEFAULT_X_MAX:g}")
    y_min_var = tk.StringVar(value=f"{DEFAULT_Y_MIN:g}")
    y_max_var = tk.StringVar(value=f"{DEFAULT_Y_MAX:g}")
    status_var = tk.StringVar(value="이미지 폴더와 결과 저장 폴더를 선택하세요.")

    def choose_image_dir() -> None:
        selected = filedialog.askdirectory(title="이미지 폴더 선택")
        if selected:
            image_dir_var.set(selected)

    def choose_output_dir() -> None:
        selected = filedialog.askdirectory(title="결과 저장 폴더 선택")
        if selected:
            output_dir_var.set(selected)

    def choose_labels_file() -> None:
        selected = filedialog.askopenfilename(
            title="학습 라벨 CSV 선택",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if selected:
            labels_var.set(selected)

    def open_dashboard(path: Path) -> None:
        import webbrowser

        webbrowser.open(path.resolve().as_uri())

    def start_analysis() -> None:
        image_dir = Path(image_dir_var.get().strip())
        output_text = output_dir_var.get().strip()
        output_dir = Path(output_text)
        labels_text = labels_var.get().strip()
        labels_path = Path(labels_text) if labels_text else None
        filename_regex = regex_var.get().strip() or None

        if not image_dir.exists() or not image_dir.is_dir():
            messagebox.showerror("입력 오류", "유효한 이미지 폴더를 선택하세요.")
            return
        if not output_text:
            messagebox.showerror("입력 오류", "결과 저장 폴더를 선택하세요.")
            return
        if labels_path and not labels_path.exists():
            messagebox.showerror("입력 오류", "학습 라벨 CSV 경로를 찾을 수 없습니다.")
            return

        try:
            wafer_width = float(wafer_width_var.get())
            wafer_height = float(wafer_height_var.get())
            if wafer_width <= 0 or wafer_height <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("입력 오류", "좌표계 폭/높이는 0보다 큰 숫자여야 합니다.")
            return
        try:
            x_min = float(x_min_var.get())
            x_max = float(x_max_var.get())
            y_min = float(y_min_var.get())
            y_max = float(y_max_var.get())
            if x_max <= x_min or y_max <= y_min:
                raise ValueError
        except ValueError:
            messagebox.showerror("입력 오류", "대시보드 X/Y 범위는 max가 min보다 커야 합니다.")
            return

        analyze_button.configure(state="disabled")
        status_var.set("분석 중...")
        root.update_idletasks()

        try:
            result = run_analysis_to_output_dir(
                image_root=image_dir,
                output_dir=output_dir,
                labels_path=labels_path,
                filename_regex=filename_regex,
                wafer_width=wafer_width,
                wafer_height=wafer_height,
                x_min=x_min,
                x_max=x_max,
                y_min=y_min,
                y_max=y_max,
            )
        except Exception as exc:
            status_var.set("분석 실패")
            messagebox.showerror("분석 실패", str(exc))
            return
        finally:
            analyze_button.configure(state="normal")

        skipped = f"\n파일명 파싱 실패: {len(result.skipped)}개" if result.skipped else ""
        status_var.set(f"완료: {len(result.records)}개 이미지, {result.summary['lot_count']}개 랏")
        messagebox.showinfo(
            "분석 완료",
            "\n".join(
                [
                    f"분석 이미지: {len(result.records)}개",
                    f"랏 수: {result.summary['lot_count']}개",
                    f"CSV: {result.output_path}",
                    f"JSON: {result.summary_path}",
                    f"대시보드: {result.dashboard_path}",
                    skipped.strip(),
                ]
            ).strip(),
        )
        if result.dashboard_path:
            open_dashboard(result.dashboard_path)

    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    ttk.Label(frame, text="불량 이미지 분석", font=("", 18, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(frame, text="이미지 파일명에서 랏/불량/좌표를 추출하고 CSV, JSON, HTML 대시보드를 생성합니다.").grid(
        row=1, column=0, columnspan=3, sticky="w", pady=(4, 18)
    )

    ttk.Label(frame, text="이미지 폴더").grid(row=2, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=image_dir_var).grid(row=2, column=1, sticky="ew", padx=8)
    ttk.Button(frame, text="선택", command=choose_image_dir).grid(row=2, column=2, sticky="ew")

    ttk.Label(frame, text="결과 저장 폴더").grid(row=3, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=output_dir_var).grid(row=3, column=1, sticky="ew", padx=8)
    ttk.Button(frame, text="선택", command=choose_output_dir).grid(row=3, column=2, sticky="ew")

    ttk.Label(frame, text="학습 라벨 CSV").grid(row=4, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=labels_var).grid(row=4, column=1, sticky="ew", padx=8)
    ttk.Button(frame, text="선택", command=choose_labels_file).grid(row=4, column=2, sticky="ew")

    ttk.Label(frame, text="파일명 정규식").grid(row=5, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=regex_var).grid(row=5, column=1, columnspan=2, sticky="ew", padx=(8, 0))

    ttk.Label(frame, text="좌표계 폭/높이").grid(row=6, column=0, sticky="w", pady=6)
    size_frame = ttk.Frame(frame)
    size_frame.grid(row=6, column=1, columnspan=2, sticky="w", padx=8)
    ttk.Entry(size_frame, textvariable=wafer_width_var, width=12).pack(side="left")
    ttk.Label(size_frame, text=" x ").pack(side="left")
    ttk.Entry(size_frame, textvariable=wafer_height_var, width=12).pack(side="left")

    ttk.Label(frame, text="대시보드 X/Y 범위").grid(row=7, column=0, sticky="w", pady=6)
    axis_frame = ttk.Frame(frame)
    axis_frame.grid(row=7, column=1, columnspan=2, sticky="w", padx=8)
    ttk.Label(axis_frame, text="X").pack(side="left")
    ttk.Entry(axis_frame, textvariable=x_min_var, width=9).pack(side="left", padx=(4, 2))
    ttk.Label(axis_frame, text="~").pack(side="left")
    ttk.Entry(axis_frame, textvariable=x_max_var, width=9).pack(side="left", padx=(2, 10))
    ttk.Label(axis_frame, text="Y").pack(side="left")
    ttk.Entry(axis_frame, textvariable=y_min_var, width=9).pack(side="left", padx=(4, 2))
    ttk.Label(axis_frame, text="~").pack(side="left")
    ttk.Entry(axis_frame, textvariable=y_max_var, width=9).pack(side="left", padx=(2, 0))

    analyze_button = ttk.Button(frame, text="분석 시작", command=start_analysis)
    analyze_button.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(22, 10))

    ttk.Label(frame, textvariable=status_var).grid(row=9, column=0, columnspan=3, sticky="w")
    ttk.Label(
        frame,
        text="추천 추가 기능: 분석 이력 저장, 임계값 설정, 불량명 매핑 편집, 결과 필터링, 대시보드 자동 열기/내보내기",
        foreground="#667085",
    ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(24, 0))

    root.mainloop()
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="불량 이미지 파일명을 분석하여 랏/불량/좌표별로 분류하고 원인 공정을 추천합니다."
    )
    parser.add_argument("image_root", type=Path, nargs="?", help="이미지 파일 또는 이미지 폴더")
    parser.add_argument("--gui", action="store_true", help="이미지 폴더/저장 폴더를 선택하는 데스크톱 UI 실행")
    parser.add_argument("--labels", type=Path, help="filename,root_cause_process 컬럼을 가진 학습 CSV")
    parser.add_argument("--output", type=Path, default=Path("defect_report.csv"), help="분석 결과 CSV 경로")
    parser.add_argument("--summary-json", type=Path, help="랏별 요약 JSON 저장 경로")
    parser.add_argument("--dashboard-html", type=Path, help="대시보드 HTML 저장 경로")
    parser.add_argument("--generate-sample-data", type=Path, help="예시 이미지와 labels.csv를 생성할 폴더")
    parser.add_argument("--sample-count", type=int, default=48, help="생성할 예시 이미지 수")
    parser.add_argument("--sample-lots", type=int, default=4, help="생성할 예시 랏 수")
    parser.add_argument("--wafer-width", type=float, default=1000.0, help="좌표계 최대 X값")
    parser.add_argument("--wafer-height", type=float, default=1000.0, help="좌표계 최대 Y값")
    parser.add_argument("--x-min", type=float, default=DEFAULT_X_MIN, help="대시보드 X축 최소값")
    parser.add_argument("--x-max", type=float, default=DEFAULT_X_MAX, help="대시보드 X축 최대값")
    parser.add_argument("--y-min", type=float, default=DEFAULT_Y_MIN, help="대시보드 Y축 최소값")
    parser.add_argument("--y-max", type=float, default=DEFAULT_Y_MAX, help="대시보드 Y축 최대값")
    parser.add_argument(
        "--filename-regex",
        help="사용자 파일명 정규식. named group lot, defect, x, y를 포함해야 합니다.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.gui:
        return launch_gui()
    if args.x_max <= args.x_min or args.y_max <= args.y_min:
        print("대시보드 X/Y 범위는 max가 min보다 커야 합니다.", file=sys.stderr)
        return 2

    image_root = args.image_root
    labels_path = args.labels
    if args.generate_sample_data:
        if args.sample_count <= 0:
            print("--sample-count는 1 이상이어야 합니다.", file=sys.stderr)
            return 2
        if args.sample_lots <= 0:
            print("--sample-lots는 1 이상이어야 합니다.", file=sys.stderr)
            return 2
        labels_path = SampleDataAgent().generate(
            output_dir=args.generate_sample_data,
            count=args.sample_count,
            lot_count=args.sample_lots,
            wafer_width=args.wafer_width,
            wafer_height=args.wafer_height,
        )
        image_root = args.generate_sample_data
        print(f"예시 데이터 생성: {image_root}")
        print(f"예시 라벨 CSV: {labels_path}")

    if not image_root:
        print("이미지 경로를 입력하거나 --generate-sample-data를 지정하세요.", file=sys.stderr)
        return 2
    if not image_root.exists():
        print(f"이미지 경로를 찾을 수 없습니다: {image_root}", file=sys.stderr)
        return 2

    pipeline = DefectAnalysisPipeline.from_options(
        filename_regex=args.filename_regex,
        labels_path=labels_path,
        wafer_width=args.wafer_width,
        wafer_height=args.wafer_height,
        x_min=args.x_min,
        x_max=args.x_max,
        y_min=args.y_min,
        y_max=args.y_max,
    )
    result = pipeline.run(image_root, args.output, args.summary_json, args.dashboard_html)

    print(f"분석 완료: {len(result.records)}개 이미지, {result.summary['lot_count']}개 랏")
    print(f"결과 CSV: {result.output_path}")
    if result.summary_path:
        print(f"랏별 요약 JSON: {result.summary_path}")
    if result.dashboard_path:
        print(f"대시보드 HTML: {result.dashboard_path}")
    if result.skipped:
        print(f"파일명 파싱 실패: {len(result.skipped)}개", file=sys.stderr)
    return 0 if result.records else 1


if __name__ == "__main__":
    raise SystemExit(main())
