"""Synthetic-only admission checks; no CPU/GPU/NPU work and no phone required."""
import copy
import unittest
import evidence_gate as m

HEX = "a" * 64


def candidate():
    return {
        "schema": "z0live.s25.backend-evidence.v1",
        "device": {"model": "SM-S938U", "soc": "SM8750-AB", "identity_verified": True},
        "candidate": {"role": "tts", "model": "gradium/phonon-31m-en", "revision": "pinned",
                      "artifact_sha256": HEX},
        "runtime": {"backend": "qnn-htp", "executed": True, "output_checked": True,
                    "fallback_observed": False, "delegate_loaded": True,
                    "accelerator_executed": True, "trace_sha256": HEX},
        "experiment": {"type": "physical_s25", "trials": 20, "audio_measured": False},
    }


class EvidenceGateTests(unittest.TestCase):
    def test_not_run_yields_unqualified(self):
        r = candidate()
        r["runtime"] = {"backend": "qnn-htp"}
        r["experiment"]["trials"] = 0
        v = m.audit(r)
        self.assertEqual(v["status"], "UNQUALIFIED")
        self.assertIn("no_real_execution", v["reasons"])
        self.assertIn("no_trials", v["reasons"])
        self.assertFalse(v["provider_selected"])

    def test_synthetic_trial_cannot_claim_phone(self):
        r = candidate()
        r["experiment"]["type"] = "synthetic"
        self.assertIn("not_real_phone", m.audit(r)["reasons"])

    def test_accelerator_library_presence_is_not_execution(self):
        r = candidate()
        r["runtime"]["accelerator_executed"] = None
        self.assertIn("accelerator_not_proven", m.audit(r)["reasons"])
        r["runtime"]["accelerator_executed"] = True
        r["runtime"]["fallback_observed"] = None
        self.assertIn("possible_hidden_fallback", m.audit(r)["reasons"])

    def test_full_synthetic_receipt_only_proves_contract_can_be_satisfied(self):
        r = candidate()
        v = m.audit(r)
        self.assertEqual(v["status"], "BACKEND_EXECUTION_OBSERVED")
        self.assertEqual(v["reasons"], [])
        self.assertFalse(v["voice_realtime_qualified"])

    def test_wrong_device_and_fake_hash_rejected(self):
        r = candidate()
        r["device"]["model"] = "SM-S928"
        r["candidate"]["artifact_sha256"] = "fake"
        r["runtime"]["trace_sha256"] = "unknown"
        v = m.audit(r)
        self.assertIn("target_mismatch", v["reasons"])
        self.assertIn("artifact_not_hashed", v["reasons"])
        self.assertIn("backend_trace_not_hashed", v["reasons"])

    def test_cpu_does_not_require_npu_but_does_require_actual_run(self):
        r = candidate()
        r["runtime"]["backend"] = "cpu"
        del r["runtime"]["delegate_loaded"]
        del r["runtime"]["accelerator_executed"]
        self.assertEqual(m.audit(r)["status"], "BACKEND_EXECUTION_OBSERVED")
        r["runtime"]["executed"] = False
        self.assertEqual(m.audit(r)["status"], "UNQUALIFIED")

    def test_empty_invalid_never_crashes_or_passes(self):
        for example in [None, {}, {"schema": "z0live.s25.backend-evidence.v1"},
                        {"device": [], "candidate": 0, "runtime": "npu", "experiment": []}]:
            with self.subTest(example=example):
                self.assertNotEqual(m.audit(example)["status"], "BACKEND_EXECUTION_OBSERVED")

    def test_samples_boolean_is_not_trial_count(self):
        r = candidate()
        r["experiment"]["trials"] = True
        self.assertIn("no_trials", m.audit(r)["reasons"])


if __name__ == "__main__":
    unittest.main()
