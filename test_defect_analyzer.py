import csv
import tempfile
import unittest
from pathlib import Path

from defect_analyzer import (
    DefectAnalysisPipeline,
    FilenameParser,
    LocationPatternAgent,
    ProcessRecommender,
    SampleDataAgent,
    build_arg_parser,
    load_records,
    main,
    read_label_map,
    run_analysis_to_output_dir,
)


class DefectAnalyzerTest(unittest.TestCase):
    def test_parses_lot_defect_and_coordinates_from_common_filename(self):
        parser = FilenameParser()
        record = parser.parse(Path("LOT123_scratch_x120_y340.png"), 1000, 1000)

        self.assertIsNotNone(record)
        self.assertEqual(record.lot, "123")
        self.assertEqual(record.defect_type, "scratch")
        self.assertEqual(record.x, 120)
        self.assertEqual(record.y, 340)
        self.assertEqual(record.zone, "ML")

    def test_custom_regex_supports_site_specific_filename(self):
        parser = FilenameParser(r"(?P<lot>L\d+)__(?P<defect>\w+)__(?P<x>\d+)x(?P<y>\d+)")
        record = parser.parse(Path("L9001__particle__810x120.jpg"), 1000, 1000)

        self.assertIsNotNone(record)
        self.assertEqual(record.lot, "L9001")
        self.assertEqual(record.defect_type, "particle")
        self.assertEqual(record.zone, "TR")

    def test_parses_lot_folder_and_xy_dash_filename(self):
        parser = FilenameParser()
        record = parser.parse(Path("MFA651601500/MFA651601500X-1Y-489.png"), 1000, 1000)

        self.assertIsNotNone(record)
        self.assertEqual(record.lot, "MFA651601500")
        self.assertEqual(record.x, 1)
        self.assertEqual(record.y, 489)
        self.assertEqual(record.defect_type, "unknown")

    def test_can_infer_defect_from_folder_between_lot_and_image(self):
        parser = FilenameParser()
        record = parser.parse(Path("MFA651601500/particle/MFA651601500X-1Y-489.png"), 1000, 1000)

        self.assertIsNotNone(record)
        self.assertEqual(record.lot, "MFA651601500")
        self.assertEqual(record.defect_type, "particle")

    def test_recommender_learns_from_labeled_records(self):
        parser = FilenameParser()
        first = parser.parse(Path("LOT777_particle_x100_y100.png"), 1000, 1000)
        second = parser.parse(Path("LOT777_particle_x130_y120.png"), 1000, 1000)
        target = parser.parse(Path("LOT888_particle_x150_y150.png"), 1000, 1000)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNotNone(target)

        trained = [
            first.__class__(**{**first.__dict__, "root_cause_process": "Cleaning"}),
            second.__class__(**{**second.__dict__, "root_cause_process": "Cleaning"}),
        ]
        recommender = ProcessRecommender()
        recommender.train(trained)

        recommendation = recommender.recommend(target)

        self.assertEqual(recommendation.process, "Cleaning")
        self.assertGreater(recommendation.confidence, 0.5)

    def test_cli_writes_report_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images"
            image_dir.mkdir()
            (image_dir / "LOT100_open_x20_y30.png").touch()
            (image_dir / "LOT200_particle_x900_y900.jpg").touch()

            labels_path = root / "labels.csv"
            with labels_path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=["filename", "root_cause_process"])
                writer.writeheader()
                writer.writerow({"filename": "LOT100_open_x20_y30.png", "root_cause_process": "Etch"})

            output_path = root / "report.csv"
            summary_path = root / "summary.json"
            result = main(
                [
                    str(image_dir),
                    "--labels",
                    str(labels_path),
                    "--output",
                    str(output_path),
                    "--summary-json",
                    str(summary_path),
                ]
            )

            self.assertEqual(result, 0)
            self.assertTrue(output_path.exists())
            self.assertTrue(summary_path.exists())
            self.assertIn("recommended_process", output_path.read_text(encoding="utf-8-sig"))


class LabelMapTest(unittest.TestCase):
    def test_read_label_map_requires_expected_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text("filename,process\nx.png,Cleaning\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                read_label_map(path)


class AgentPipelineTest(unittest.TestCase):
    def test_pipeline_runs_through_functional_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images"
            image_dir.mkdir()
            (image_dir / "LOT300_stain_x500_y500.png").touch()

            pipeline = DefectAnalysisPipeline.from_options(
                filename_regex=None,
                labels_path=None,
                wafer_width=1000,
                wafer_height=1000,
            )
            result = pipeline.run(image_dir, root / "report.csv", root / "summary.json")

            self.assertEqual(len(result.records), 1)
            self.assertEqual(result.records[0].defect_type, "stain")
            self.assertEqual(result.summary["lot_count"], 1)
            self.assertTrue(result.output_path.exists())
            self.assertTrue(result.summary_path.exists())

    def test_pipeline_writes_dashboard_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images"
            image_dir.mkdir()
            (image_dir / "LOT300_stain_x500_y500.png").touch()

            pipeline = DefectAnalysisPipeline.from_options(
                filename_regex=None,
                labels_path=None,
                wafer_width=1000,
                wafer_height=1000,
            )
            dashboard_path = root / "dashboard.html"
            result = pipeline.run(image_dir, root / "report.csv", root / "summary.json", dashboard_path)

            self.assertEqual(result.dashboard_path, dashboard_path)
            self.assertTrue(dashboard_path.exists())
            dashboard = dashboard_path.read_text(encoding="utf-8")
            self.assertIn("Defect Analysis Dashboard", dashboard)
            self.assertIn("LOT300", dashboard)
            self.assertIn("rectangular wafer defect scatter plot", dashboard)
            self.assertIn('<rect x="40" y="40"', dashboard)
            self.assertIn("불량별 대표 이미지", dashboard)
            self.assertIn("representative image", dashboard)
            self.assertIn("위치 패턴 분석", dashboard)
            self.assertIn("로컬 LLM 데이터 Q&amp;A", dashboard)
            self.assertIn("http://localhost:11434/api/chat", dashboard)
            self.assertIn("analysis-context", dashboard)
            self.assertIn("location_pattern_findings", dashboard)
            self.assertIn('id="x-min" type="number" value="-1500"', dashboard)
            self.assertIn('id="x-max" type="number" value="1500"', dashboard)
            self.assertIn('id="y-min" type="number" value="-1000"', dashboard)
            self.assertIn('id="y-max" type="number" value="1000"', dashboard)
            self.assertIn("axis-apply-button", dashboard)
            self.assertIn("dashboard_axis_range", dashboard)

    def test_gui_analysis_helper_writes_standard_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "images"
            output_dir = root / "output"
            image_dir.mkdir()
            (image_dir / "LOT500_particle_x100_y100.png").touch()

            result = run_analysis_to_output_dir(image_dir, output_dir)

            self.assertEqual(len(result.records), 1)
            self.assertTrue((output_dir / "defect_report.csv").exists())
            self.assertTrue((output_dir / "lot_summary.json").exists())
            self.assertTrue((output_dir / "dashboard.html").exists())


class SampleDataTest(unittest.TestCase):
    def test_sample_data_agent_generates_images_and_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels_path = SampleDataAgent().generate(root, count=10, lot_count=2, wafer_width=1000, wafer_height=1000)

            self.assertTrue(labels_path.exists())
            self.assertEqual(len(list(root.glob("*.png"))), 10)
            self.assertIn("root_cause_process", labels_path.read_text(encoding="utf-8"))

    def test_cli_generates_sample_data_and_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample_dir = root / "sample"
            dashboard_path = root / "dashboard.html"
            result = main(
                [
                    "--generate-sample-data",
                    str(sample_dir),
                    "--sample-count",
                    "12",
                    "--sample-lots",
                    "3",
                    "--output",
                    str(root / "report.csv"),
                    "--summary-json",
                    str(root / "summary.json"),
                    "--dashboard-html",
                    str(dashboard_path),
                ]
            )

            self.assertEqual(result, 0)
            self.assertTrue((sample_dir / "labels.csv").exists())
            self.assertTrue(dashboard_path.exists())
            self.assertIn("사각 Wafer 좌표 분포", dashboard_path.read_text(encoding="utf-8"))
            self.assertIn("불량별 대표 이미지", dashboard_path.read_text(encoding="utf-8"))
            self.assertIn("위치 패턴 분석", dashboard_path.read_text(encoding="utf-8"))
            self.assertIn("로컬 LLM 데이터 Q&amp;A", dashboard_path.read_text(encoding="utf-8"))


class LocationPatternTest(unittest.TestCase):
    def test_detects_nearby_cluster_pattern(self):
        parser = FilenameParser()
        records = [
            parser.parse(Path("LOT900_particle_x100_y100.png"), 1000, 1000),
            parser.parse(Path("LOT900_particle_x105_y103.png"), 1000, 1000),
            parser.parse(Path("LOT900_particle_x110_y108.png"), 1000, 1000),
            parser.parse(Path("LOT900_particle_x800_y820.png"), 1000, 1000),
        ]
        parsed = [record for record in records if record is not None]

        findings = LocationPatternAgent().analyze(parsed, 1000, 1000)

        self.assertTrue(any(finding["pattern"] == "근거리 집중 발생" for finding in findings))

    def test_detects_line_pattern(self):
        parser = FilenameParser()
        records = [
            parser.parse(Path("LOT901_scratch_x100_y100.png"), 1000, 1000),
            parser.parse(Path("LOT901_scratch_x300_y300.png"), 1000, 1000),
            parser.parse(Path("LOT901_scratch_x500_y500.png"), 1000, 1000),
            parser.parse(Path("LOT901_scratch_x700_y700.png"), 1000, 1000),
        ]
        parsed = [record for record in records if record is not None]

        findings = LocationPatternAgent().analyze(parsed, 1000, 1000)

        self.assertTrue(any("대각" in str(finding["pattern"]) for finding in findings))


class CliParserTest(unittest.TestCase):
    def test_gui_flag_is_supported(self):
        args = build_arg_parser().parse_args(["--gui"])

        self.assertTrue(args.gui)

    def test_dashboard_axis_defaults_are_site_defaults(self):
        args = build_arg_parser().parse_args(["images"])

        self.assertEqual(args.x_min, -1500)
        self.assertEqual(args.x_max, 1500)
        self.assertEqual(args.y_min, -1000)
        self.assertEqual(args.y_max, 1000)


if __name__ == "__main__":
    unittest.main()
