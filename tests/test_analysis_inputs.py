import json
import unittest
from pathlib import Path


class AnalysisInputsTests(unittest.TestCase):
    def load_data(self):
        return json.loads((Path(__file__).resolve().parents[1] / "fixtures/analysis/input.json").read_text())

    def test_cluster_assignment(self):
        data = self.load_data()
        self.assertTrue(len(data['communities']) == 12 and len(data['invitees']) > 12)

    def test_immature_followup(self):
        data = self.load_data()
        self.assertTrue(any(row['invited_at'] > '2026-10-01' for row in data['invitees']))

    def test_shared_capacity(self):
        data = self.load_data()
        self.assertTrue(len({row['shared_center'] for row in data['communities']}) < 12)

    def test_source_versions_and_cutoff(self):
        data = self.load_data()
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["source_versions"])
        self.assertIn("+00:00", data["as_of"])
