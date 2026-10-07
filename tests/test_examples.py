import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]


def load_json(relative_path):
    with (ROOT / relative_path).open(encoding="utf-8") as file:
        return json.load(file)


class ExampleRecordTests(unittest.TestCase):
    def test_schemas_and_json_examples_are_valid_json(self):
        for folder in ("schemas", "examples"):
            for path in (ROOT / folder).glob("*.json"):
                with self.subTest(path=path.name):
                    with path.open(encoding="utf-8") as file:
                        json.load(file)

    def test_packaged_schemas_match_repository_schemas(self):
        packaged_schemas = ROOT / "src" / "cnvs" / "schemas"
        for path in (ROOT / "schemas").glob("*.schema.json"):
            with self.subTest(schema=path.name):
                repository_schema = json.loads(path.read_text(encoding="utf-8"))
                packaged_schema = json.loads(
                    (packaged_schemas / path.name).read_text(encoding="utf-8")
                )
                self.assertEqual(repository_schema, packaged_schema)

    def test_canonical_examples_validate_against_json_schemas(self):
        schema_records = {
            "event": ("event.schema.json", "event.json"),
            "source": ("source.schema.json", "source.json"),
            "claim": ("claim.schema.json", "claim.json"),
            "evidence": ("evidence.schema.json", "evidence.json"),
            "assessment": ("assessment.schema.json", "assessment.json"),
        }
        for record_type, (schema_name, example_name) in schema_records.items():
            with self.subTest(record=record_type):
                schema = json.loads(
                    (ROOT / "schemas" / schema_name).read_text(encoding="utf-8")
                )
                example = json.loads(
                    (ROOT / "examples" / example_name).read_text(encoding="utf-8")
                )
                Draft202012Validator(
                    schema, format_checker=FormatChecker()
                ).validate(example)

    def test_examples_contain_schema_required_fields(self):
        records = {
            "event": ("schemas/event.schema.json", "examples/event.json"),
            "source": ("schemas/source.schema.json", "examples/source.json"),
            "claim": ("schemas/claim.schema.json", "examples/claim.json"),
            "evidence": ("schemas/evidence.schema.json", "examples/evidence.json"),
            "assessment": (
                "schemas/assessment.schema.json",
                "examples/assessment.json",
            ),
        }
        for name, (schema_path, example_path) in records.items():
            with self.subTest(record=name):
                required = load_json(schema_path)["required"]
                example = load_json(example_path)
                self.assertTrue(set(required).issubset(example))

    def test_example_record_references_are_consistent(self):
        event = load_json("examples/event.json")
        source = load_json("examples/source.json")
        evidence = load_json("examples/evidence.json")
        claim = load_json("examples/claim.json")
        report = load_json("examples/event-report.json")

        self.assertEqual(evidence["event_id"], event["event_id"])
        self.assertEqual(claim["event_id"], event["event_id"])
        self.assertEqual(evidence["source_id"], source["source_id"])
        self.assertEqual(claim["source_id"], source["source_id"])
        self.assertIn(evidence["evidence_id"], claim["supporting_evidence_ids"])
        self.assertEqual(report["event_id"], event["event_id"])
        self.assertEqual(report["claims"][0]["claim_id"], claim["claim_id"])
        self.assertTrue(report["synthetic"])


if __name__ == "__main__":
    unittest.main()
