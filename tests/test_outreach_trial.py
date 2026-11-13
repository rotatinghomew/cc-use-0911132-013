from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from colorectal_cascade import outreach_trial
from colorectal_cascade.service import ApplicationService

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_INPUT = REPO_ROOT / "fixtures" / "analysis" / "input.json"

INVITED_EARLY = "2026-01-10T00:00:00+00:00"
AS_OF = "2026-03-01T00:00:00+00:00"


def make_community(cid, arm, block, center="center-x"):
    return {
        "community_id": cid,
        "assigned_arm": arm,
        "randomization_block": block,
        "assigned_at": "2026-01-01T00:00:00+00:00",
        "shared_center": center,
        "inclusion_probability": 0.5,
    }


def make_invitee(pid, cid, invited=INVITED_EARLY, reminded=False):
    return {
        "person_id": pid,
        "community_id": cid,
        "invited_at": invited,
        "reminder_received": reminded,
        "available_at": invited,
    }


def make_stage(pid, stage, occurred, available=None, outcome="complete"):
    return {
        "person_id": pid,
        "stage": stage,
        "occurred_at": occurred,
        "available_at": available or occurred,
        "outcome": outcome,
    }


def make_input(communities, invitees, stages, eligibility, as_of=AS_OF):
    return {
        "schema_version": 1,
        "as_of": as_of,
        "source_versions": {"operations": "t1"},
        "communities": communities,
        "invitees": invitees,
        "stages": stages,
        "referral_eligibility": eligibility,
        "study": {
            "horizon_days": 30,
            "randomization_unit": "community",
            "primary_endpoint": "attendance",
            "operational_outcome": "referral_completion_per_invitee",
        },
        "baseline_case_id": "demo-001",
    }


def two_block_communities():
    return [
        make_community("comm-a", "usual", "b1"),
        make_community("comm-b", "extra", "b1"),
        make_community("comm-c", "usual", "b2"),
        make_community("comm-d", "extra", "b2"),
    ]


class OutreachTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_input(self, payload):
        path = self.tmp / "input.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run_to_file(self, payload, db=None):
        input_path = self.write_input(payload)
        output = self.tmp / "effects.json"
        code = outreach_trial.main([
            "--input", str(input_path),
            "--database", str(db or self.tmp / "missing.sqlite3"),
            "--output", str(output),
        ])
        self.assertEqual(code, 0)
        return json.loads(output.read_text(encoding="utf-8"))

    def test_composition_change_not_confused_with_assignment_effect(self):
        # 只改变参与人群构成：两组每受邀人转介完成率相同（0.5），
        # 但有资格者中的条件完成率不同（1.0 vs 0.5）。
        communities = two_block_communities()
        invitees, stages, eligibility = [], [], []
        for cid, arm in (("comm-a", "usual"), ("comm-c", "usual"),
                         ("comm-b", "extra"), ("comm-d", "extra")):
            for i in range(4):
                pid = f"{cid}-{i}"
                invitees.append(make_invitee(pid, cid))
                attend = i < 2 if arm == "usual" else True
                if attend:
                    stages.append(make_stage(pid, "attended", "2026-01-12T00:00:00+00:00"))
                    stages.append(make_stage(pid, "sample", "2026-01-15T00:00:00+00:00"))
                    eligibility.append({
                        "person_id": pid,
                        "eligible": True,
                        "available_at": "2026-01-16T00:00:00+00:00",
                    })
                # 两组均恰好 2 人完成转介：usual 2/2 有资格者，extra 2/4 有资格者
                if i < 2:
                    stages.append(make_stage(pid, "referral", "2026-01-25T00:00:00+00:00"))
        report = self.run_to_file(make_input(communities, invitees, stages, eligibility))
        referral = report["stage_effects"]["referral"]
        self.assertEqual(referral["effect"]["estimate"], 0.0)
        conditional = report["stage_effects"]["descriptive_only"][
            "referral_completion_among_eligible"]
        self.assertFalse(conditional["randomized"])
        self.assertEqual(conditional["arms"]["usual"]["rate"], 1.0)
        self.assertEqual(conditional["arms"]["extra"]["rate"], 0.5)
        self.assertIn("不能解释为随机分配的效果",
                      report["stage_effects"]["descriptive_only"]["note"])

    def test_immature_followup_not_counted_as_failure(self):
        communities = two_block_communities()
        invitees, stages = [], []
        for cid in ("comm-a", "comm-b", "comm-c", "comm-d"):
            for i in range(3):
                pid = f"{cid}-{i}"
                invitees.append(make_invitee(
                    pid, cid, invited="2026-02-20T00:00:00+00:00"))
        # 一条截止后才可用（且发生）的转介记录：只能隔离，不能计为成功或失败
        stages.append(make_stage("comm-b-0", "referral",
                                 "2026-03-05T00:00:00+00:00",
                                 "2026-03-06T00:00:00+00:00"))
        report = self.run_to_file(make_input(communities, invitees, stages, []))
        support = report["followup_support"]["stage_support"]["referral"]
        self.assertEqual(support["pending"], 12)
        self.assertEqual(support["failure"], 0)
        self.assertEqual(support["evaluable"], 0)
        self.assertEqual(support["deferred_facts"], 1)
        self.assertEqual(report["stage_effects"]["referral"]["verdict"],
                         "insufficient_data")
        self.assertIsNone(report["stage_effects"]["referral"]["effect"])
        self.assertEqual(report["followup_support"]["deferred_facts"][0]["person_id"],
                         "comm-b-0")

    def test_zero_events_handled_without_correction(self):
        communities = two_block_communities()
        invitees = [make_invitee(f"{cid}-{i}", cid)
                    for cid in ("comm-a", "comm-b", "comm-c", "comm-d")
                    for i in range(3)]
        report = self.run_to_file(make_input(communities, invitees, [], []))
        attended = report["stage_effects"]["attended"]
        self.assertEqual(attended["effect"]["estimate"], 0.0)
        self.assertEqual(attended["effect"]["raw_p"], 1.0)
        self.assertEqual(attended["verdict"], "inconclusive")
        self.assertTrue(any("零事件" in note for note in attended["notes"]))

    def test_exact_permutation_p_value(self):
        # 两个区组、每组处理社区全到、对照全不到：4 种分配下 |T|>=1 占 2 种
        communities = two_block_communities()
        invitees, stages = [], []
        for cid, arm in (("comm-a", "usual"), ("comm-b", "extra"),
                         ("comm-c", "usual"), ("comm-d", "extra")):
            for i in range(2):
                pid = f"{cid}-{i}"
                invitees.append(make_invitee(pid, cid))
                if arm == "extra":
                    stages.append(make_stage(pid, "attended", "2026-01-12T00:00:00+00:00"))
        report = self.run_to_file(make_input(communities, invitees, stages, []))
        effect = report["stage_effects"]["attended"]["effect"]
        self.assertEqual(effect["estimate"], 1.0)
        self.assertEqual(effect["raw_p"], 0.5)
        self.assertEqual(report["stage_effects"]["inference_method"]["permutations"], 4)
        self.assertEqual(report["stage_effects"]["inference_method"]["method"],
                         "exact_block_rerandomization")

    def test_replay_deterministic_on_fixture(self):
        first = outreach_trial.run_analysis(FIXTURE_INPUT, self.tmp / "none.sqlite3")
        second = outreach_trial.run_analysis(FIXTURE_INPUT, self.tmp / "none.sqlite3")
        self.assertEqual(first, second)
        self.assertEqual(first["trial_analysis_id"], second["trial_analysis_id"])
        self.assertEqual(first["assignment_checks"]["baseline_crosscheck"]["status"],
                         "database_unavailable")

    def test_fixture_known_values(self):
        report = outreach_trial.run_analysis(FIXTURE_INPUT, self.tmp / "none.sqlite3")
        attended = report["stage_effects"]["attended"]
        self.assertEqual(attended["effect"]["estimate"], 0.5)
        self.assertEqual(attended["effect"]["raw_p"], 0.25)
        referral = report["stage_effects"]["referral"]
        self.assertAlmostEqual(referral["effect"]["estimate"], 0.095238, places=6)
        support = report["followup_support"]["stage_support"]["referral"]
        self.assertEqual((support["evaluable"], support["pending"],
                          support["deferred_facts"]), (80, 40, 6))
        fidelity = report["assignment_checks"]["delivery_fidelity"]
        self.assertEqual(fidelity["assigned_extra_without_any_reminder"],
                         ["community-3", "community-9"])
        self.assertEqual(sorted(fidelity["assigned_usual_with_any_reminder"]),
                         ["community-10", "community-2", "community-4", "community-8"])
        self.assertEqual(report["interference"]["centers_mixing_arms"], 3)

    def test_crosscheck_consistent_with_real_database(self):
        db = self.tmp / "cascade.sqlite3"
        service = ApplicationService(db)
        service.execute({"case_id": "demo-001"}, request_key="baseline")
        report = outreach_trial.run_analysis(FIXTURE_INPUT, db)
        crosscheck = report["assignment_checks"]["baseline_crosscheck"]
        self.assertEqual(crosscheck["status"], "consistent")
        self.assertTrue(crosscheck["cohort_stage_completed"])
        self.assertTrue(crosscheck["referral_stage_completed"])

    def test_crosscheck_baseline_not_found(self):
        db = self.tmp / "cascade.sqlite3"
        ApplicationService(db).execute({"case_id": "other-case"}, request_key="x")
        report = outreach_trial.run_analysis(FIXTURE_INPUT, db)
        self.assertEqual(report["assignment_checks"]["baseline_crosscheck"]["status"],
                         "baseline_not_found")

    def test_cli_writes_required_fields(self):
        output = self.tmp / "out.json"
        code = outreach_trial.main([
            "--input", str(FIXTURE_INPUT),
            "--database", str(self.tmp / "missing.sqlite3"),
            "--output", str(output),
        ])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text(encoding="utf-8"))
        for field in ("trial_analysis_id", "assignment_checks", "stage_effects",
                      "followup_support", "interference", "limitations"):
            self.assertIn(field, report)

    def test_invalid_input_rejected(self):
        bad = self.write_input({"schema_version": 2})
        code = outreach_trial.main([
            "--input", str(bad),
            "--database", str(self.tmp / "missing.sqlite3"),
            "--output", str(self.tmp / "out.json"),
        ])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
