import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
CONTRACTS_DIR = TEST_DIR.parent / "contracts"

sys.path.insert(0, str(TEST_DIR))
sys.path.insert(0, str(CONTRACTS_DIR))
import genlayer_stub as gs

ADDR_OWNER = gs.Address("0x1000000000000000000000000000000000000001")
ADDR_PROVIDER = gs.Address("0x2000000000000000000000000000000000000002")
ADDR_OTHER = gs.Address("0x3000000000000000000000000000000000000003")

NOW = 1_800_000_000


class RegistryTest(unittest.TestCase):
	def setUp(self):
		gs.reset()
		gs.message.sender_address = ADDR_OWNER
		mod = gs.load_contract(str(CONTRACTS_DIR / "CovenantRegistry.py"), "covenant_registry")
		self.mod = mod
		self._now = NOW
		mod._now_epoch = lambda: self._now
		self.reg = mod.CovenantRegistry()

	def _create(self, starts=None, ends=None, sender=ADDR_OWNER):
		gs.message.sender_address = sender
		if starts is None:
			starts = self._now + 100
		if ends is None:
			ends = starts + 1000
		return self.reg.create_covenant(str(ADDR_PROVIDER), "status page reads operational",
			"https://example.org/status", "use the official status page only", starts, ends)

	def test_create_covenant_happy_path(self):
		out = self._create()
		import json
		data = json.loads(out)
		self.assertTrue(data["ok"])
		self.assertEqual(data["covenant_id"], 1)
		self.assertEqual(self.reg.get_covenant_status(1), "ACTIVE")

	def test_create_rejects_short_lead(self):
		with self.assertRaises(gs.UserError):
			self._create(starts=self._now + 10, ends=self._now + 2000)

	def test_create_rejects_short_window(self):
		with self.assertRaises(gs.UserError):
			self._create(starts=self._now + 100, ends=self._now + 200)

	def test_create_rejects_empty_provider(self):
		gs.message.sender_address = ADDR_OWNER
		with self.assertRaises(gs.UserError):
			self.reg.create_covenant("", "x" * 10, "https://x", "", self._now + 100, self._now + 2000)

	def test_cancel_before_start_by_creator(self):
		self._create()
		gs.message.sender_address = ADDR_OWNER
		out = self.reg.cancel_covenant(1)
		import json
		self.assertEqual(json.loads(out)["status"], "VOIDED")
		self.assertEqual(self.reg.get_covenant_status(1), "VOIDED")

	def test_cancel_by_non_creator_raises(self):
		self._create()
		gs.message.sender_address = ADDR_OTHER
		with self.assertRaises(gs.UserError):
			self.reg.cancel_covenant(1)

	def test_attest_before_window_raises(self):
		self._create(starts=self._now + 500, ends=self._now + 2000)
		with self.assertRaises(gs.UserError):
			self.reg.attest(1)

	def test_attest_after_window_raises(self):
		self._create(starts=self._now + 100, ends=self._now + 1000)
		self._now += 10000
		with self.assertRaises(gs.UserError):
			self.reg.attest(1)

	def test_full_lifecycle_honored_majority(self):
		self._create(starts=self._now + 100, ends=self._now + 100000)
		self._now += 200  # inside window now

		# three attestations: HONORED, HONORED, BREACHED. The stub's
		# run_nondet_unsafe calls leader_fn() once for the proposal and
		# then again inside validator_fn() to check agreement, so each
		# attest() call drives exec_prompt twice; call_count // 2 picks
		# the verdict for the current attest() call so both calls agree.
		targets = ["HONORED", "HONORED", "BREACHED"]
		call_count = [0]

		def fake_render(url, mode, **kwargs):
			return "the status page currently shows operational"

		def fake_prompt(prompt, response_format):
			idx = call_count[0] // 2
			call_count[0] += 1
			return {"verdict": targets[idx], "confidence": 80, "note": "ok"}

		self.mod.gl.nondet.web.render = fake_render
		gs.NONDET_HOOKS["exec_prompt"] = fake_prompt

		self.reg.attest(1)
		self._now += self.mod.MIN_ATTESTATION_GAP_SECONDS + 1
		self.reg.attest(1)
		self._now += self.mod.MIN_ATTESTATION_GAP_SECONDS + 1
		self.reg.attest(1)

		self._now = self._now + 200000  # past ends_epoch
		out = self.reg.finalize_covenant(1)
		import json
		data = json.loads(out)
		self.assertEqual(data["status"], "FINALIZED")
		self.assertEqual(data["compliance_bps"], 6666)  # 2/3 honored
		self.assertEqual(self.reg.get_compliance_bps(1), 6666)

	def test_finalize_with_no_decisive_attestations_voids(self):
		self._create(starts=self._now + 100, ends=self._now + 1000)
		self._now += 2000
		out = self.reg.finalize_covenant(1)
		import json
		self.assertEqual(json.loads(out)["status"], "VOIDED")

	def test_attest_gap_enforced(self):
		self._create(starts=self._now + 100, ends=self._now + 100000)
		self._now += 200

		def fake_render(url, mode, **kwargs):
			return "operational"

		def fake_prompt(prompt, response_format):
			return {"verdict": "HONORED", "confidence": 90, "note": "ok"}

		self.mod.gl.nondet.web.render = fake_render
		gs.NONDET_HOOKS["exec_prompt"] = fake_prompt

		self.reg.attest(1)
		with self.assertRaises(gs.UserError):
			self.reg.attest(1)  # too soon

	def test_attest_cap_enforced(self):
		self._create(starts=self._now + 100, ends=self._now + 10_000_000)
		self._now += 200

		def fake_render(url, mode, **kwargs):
			return "operational"

		def fake_prompt(prompt, response_format):
			return {"verdict": "HONORED", "confidence": 90, "note": "ok"}

		self.mod.gl.nondet.web.render = fake_render
		gs.NONDET_HOOKS["exec_prompt"] = fake_prompt

		for _ in range(self.mod.MAX_ATTESTATIONS_PER_COVENANT):
			self.reg.attest(1)
			self._now += self.mod.MIN_ATTESTATION_GAP_SECONDS + 1

		with self.assertRaises(gs.UserError):
			self.reg.attest(1)

	def test_transient_fetch_error_raises_with_prefix(self):
		self._create(starts=self._now + 100, ends=self._now + 100000)
		self._now += 200

		def broken_render(url, mode, **kwargs):
			raise RuntimeError("dns failure")

		self.mod.gl.nondet.web.render = broken_render
		with self.assertRaises(gs.UserError) as ctx:
			self.reg.attest(1)
		self.assertTrue(str(ctx.exception.message).startswith(self.mod.ERR_TRANSIENT_FETCH))

	def test_malformed_llm_response_raises_with_prefix(self):
		self._create(starts=self._now + 100, ends=self._now + 100000)
		self._now += 200

		def fake_render(url, mode, **kwargs):
			return "some content"

		def bad_prompt(prompt, response_format):
			return {"verdict": "MAYBE", "confidence": 50, "note": "ok"}

		self.mod.gl.nondet.web.render = fake_render
		gs.NONDET_HOOKS["exec_prompt"] = bad_prompt
		with self.assertRaises(gs.UserError) as ctx:
			self.reg.attest(1)
		self.assertTrue(str(ctx.exception.message).startswith(self.mod.ERR_VERDICT_MALFORMED))

	def test_preview_check_never_raises_on_transient_error(self):
		self._create(starts=self._now + 100, ends=self._now + 100000)

		def broken_render(url, mode, **kwargs):
			raise RuntimeError("timeout")

		self.mod.gl.nondet.web.render = broken_render
		out = self.reg.preview_check(1)
		import json
		data = json.loads(out)
		self.assertEqual(data["preview_verdict"], "RETRY_LATER")
		self.assertFalse(data["binding"])

	def test_get_provider_and_pagination(self):
		self._create()
		self.assertEqual(self.reg.get_provider(1), str(ADDR_PROVIDER))
		out = self.reg.get_covenants_by_creator(str(ADDR_OWNER))
		import json
		data = json.loads(out)
		self.assertEqual(data["total"], 1)


if __name__ == "__main__":
	unittest.main()
