import sys
import json
import unittest

sys.path.insert(0, "/home/claude/work/CovenantChain/test")
sys.path.insert(0, "/home/claude/work/CovenantChain/contracts")
import genlayer_stub as gs

ADDR_OWNER = gs.Address("0x1000000000000000000000000000000000000001")
ADDR_PROVIDER = gs.Address("0x2000000000000000000000000000000000000002")
ADDR_BACKER_A = gs.Address("0x3000000000000000000000000000000000000003")
ADDR_BACKER_B = gs.Address("0x4000000000000000000000000000000000000004")
ADDR_REGISTRY = gs.Address("0x5000000000000000000000000000000000000005")

NOW = 1_800_000_000
GEN = 10 ** 18


class FakeRegistry:
	"""Hand-written double for CovenantRegistry's view surface, with each
	call independently toggleable to raise -- mirrors the real project's
	fake-oracle testing approach for proving _try_registry() actually
	swallows every upstream failure before it can escape fund()."""

	def __init__(self):
		self.status = "ACTIVE"
		self.ends_epoch = NOW + 100000
		self.compliance_bps = 0
		self.provider = ADDR_PROVIDER
		self.raise_on_status = False
		self.raise_on_ends = False
		self.raise_on_provider = False

	def view(self):
		return self

	def get_covenant_status(self, covenant_id):
		if self.raise_on_status:
			raise gs.UserError("registry status view exploded")
		return self.status

	def get_ends_epoch(self, covenant_id):
		if self.raise_on_ends:
			raise gs.UserError("registry ends_epoch view exploded")
		return self.ends_epoch

	def is_finalized(self, covenant_id):
		return self.status == "FINALIZED"

	def get_compliance_bps(self, covenant_id):
		return self.compliance_bps

	def get_provider(self, covenant_id):
		if self.raise_on_provider:
			raise gs.UserError("registry provider view exploded")
		return str(self.provider)


class EscrowTest(unittest.TestCase):
	def setUp(self):
		gs.reset()
		gs.message.sender_address = ADDR_OWNER
		mod = gs.load_contract("/home/claude/work/CovenantChain/contracts/CommitmentEscrow.py",
			"commitment_escrow")
		self.mod = mod
		self._now = NOW
		mod._now_epoch = lambda: self._now
		self.fake_registry = FakeRegistry()
		gs.CONTRACT_REGISTRY[str(ADDR_REGISTRY)] = self.fake_registry
		self.escrow = mod.CommitmentEscrow(str(ADDR_REGISTRY))

	def _fund(self, covenant_id, sender, amount):
		gs.message.sender_address = sender
		gs.message.value = amount
		return self.escrow.fund(covenant_id)

	# -------------------------------------------------------------- #
	# money-safety: fund() must never raise
	# -------------------------------------------------------------- #

	def test_fund_below_minimum_refunds_instead_of_raising(self):
		out = self._fund(1, ADDR_BACKER_A, 10)
		data = json.loads(out)
		self.assertFalse(data["ok"])
		self.assertEqual(gs.PAYMENTS[-1], (str(ADDR_BACKER_A), 10))

	def test_fund_when_covenant_not_active_refunds(self):
		self.fake_registry.status = "VOIDED"
		out = self._fund(1, ADDR_BACKER_A, GEN)
		data = json.loads(out)
		self.assertFalse(data["ok"])
		self.assertEqual(gs.PAYMENTS[-1], (str(ADDR_BACKER_A), GEN))

	def test_fund_after_ends_epoch_refunds(self):
		self.fake_registry.ends_epoch = self._now - 10
		out = self._fund(1, ADDR_BACKER_A, GEN)
		self.assertFalse(json.loads(out)["ok"])

	def test_fund_refunds_when_registry_status_view_raises(self):
		self.fake_registry.raise_on_status = True
		out = self._fund(1, ADDR_BACKER_A, GEN)
		data = json.loads(out)
		self.assertFalse(data["ok"])
		self.assertEqual(gs.PAYMENTS[-1], (str(ADDR_BACKER_A), GEN))

	def test_fund_refunds_when_registry_ends_view_raises(self):
		self.fake_registry.raise_on_ends = True
		out = self._fund(1, ADDR_BACKER_A, GEN)
		self.assertFalse(json.loads(out)["ok"])
		self.assertEqual(gs.PAYMENTS[-1], (str(ADDR_BACKER_A), GEN))

	def test_fund_refunds_when_registry_provider_view_raises(self):
		self.fake_registry.raise_on_provider = True
		out = self._fund(1, ADDR_BACKER_A, GEN)
		self.assertFalse(json.loads(out)["ok"])
		self.assertEqual(gs.PAYMENTS[-1], (str(ADDR_BACKER_A), GEN))

	def test_fund_happy_path_accumulates(self):
		out1 = self._fund(1, ADDR_BACKER_A, 2 * GEN)
		self.assertTrue(json.loads(out1)["ok"])
		out2 = self._fund(1, ADDR_BACKER_A, 1 * GEN)
		data2 = json.loads(out2)
		self.assertTrue(data2["ok"])
		self.assertEqual(data2["total_contribution"], str(3 * GEN))

	# -------------------------------------------------------------- #
	# settlement math
	# -------------------------------------------------------------- #

	def test_settle_and_claim_full_worked_example(self):
		# Two backers fund a covenant: A puts in 3 GEN, B puts in 7 GEN.
		# The covenant is judged 80% compliant (8000 bps). Default fee is
		# 250 bps (2.5%), taken only out of the provider's earned share.
		self._fund(1, ADDR_BACKER_A, 3 * GEN)
		self._fund(1, ADDR_BACKER_B, 7 * GEN)

		self.fake_registry.status = "FINALIZED"
		self.fake_registry.compliance_bps = 8000

		gs.message.sender_address = ADDR_BACKER_A  # settle is permissionless
		out = self.escrow.settle_fund(1)
		data = json.loads(out)
		self.assertEqual(data["status"], "SETTLED")

		total = 10 * GEN
		gross_provider = (total * 8000) // 10000  # 8 GEN
		fee = (gross_provider * 250) // 10000     # 0.2 GEN
		provider_net = gross_provider - fee        # 7.8 GEN
		refund_pool = total - gross_provider       # 2 GEN

		self.assertEqual(int(data["compliance_bps"]), 8000)
		self.assertEqual(data["provider_amount"], str(provider_net))
		self.assertEqual(data["refund_pool"], str(refund_pool))
		self.assertEqual(data["fee_taken"], str(fee))

		gs.message.sender_address = ADDR_PROVIDER
		out = self.escrow.claim_provider_share(1)
		self.assertEqual(json.loads(out)["payout"], str(provider_net))
		self.assertEqual(gs.PAYMENTS[-1], (str(ADDR_PROVIDER), provider_net))

		gs.message.sender_address = ADDR_BACKER_A
		out_a = self.escrow.claim_refund(1)
		expected_a = (refund_pool * (3 * GEN)) // total
		self.assertEqual(json.loads(out_a)["payout"], str(expected_a))

		gs.message.sender_address = ADDR_BACKER_B
		out_b = self.escrow.claim_refund(1)
		expected_b = (refund_pool * (7 * GEN)) // total
		self.assertEqual(json.loads(out_b)["payout"], str(expected_b))

		# provider reputation was updated at settlement time
		stats = json.loads(self.escrow.get_provider_stats(str(ADDR_PROVIDER)))
		self.assertEqual(stats["settled_count"], 1)
		self.assertEqual(stats["avg_compliance_bps"], 8000)
		self.assertEqual(stats["total_secured"], str(provider_net))

	def test_zero_compliance_costs_no_fee_full_refund(self):
		self._fund(1, ADDR_BACKER_A, 5 * GEN)
		self.fake_registry.status = "FINALIZED"
		self.fake_registry.compliance_bps = 0

		gs.message.sender_address = ADDR_BACKER_A
		out = self.escrow.settle_fund(1)
		data = json.loads(out)
		self.assertEqual(data["provider_amount"], "0")
		self.assertEqual(data["fee_taken"], "0")
		self.assertEqual(data["refund_pool"], str(5 * GEN))

		out_refund = self.escrow.claim_refund(1)
		self.assertEqual(json.loads(out_refund)["payout"], str(5 * GEN))

	def test_settle_before_finalized_raises(self):
		self._fund(1, ADDR_BACKER_A, GEN)
		with self.assertRaises(gs.UserError):
			self.escrow.settle_fund(1)

	def test_settle_after_grace_period_voids(self):
		self._fund(1, ADDR_BACKER_A, GEN)
		self.fake_registry.status = "ACTIVE"
		self._now = self.fake_registry.ends_epoch + self.mod.FINALIZATION_GRACE_SECONDS + 10
		out = self.escrow.settle_fund(1)
		self.assertEqual(json.loads(out)["status"], "VOID")

		gs.message.sender_address = ADDR_BACKER_A
		out_refund = self.escrow.claim_refund(1)
		self.assertEqual(json.loads(out_refund)["payout"], str(GEN))

	def test_voided_covenant_gives_full_refund(self):
		self._fund(1, ADDR_BACKER_A, 4 * GEN)
		self.fake_registry.status = "VOIDED"
		out = self.escrow.settle_fund(1)
		self.assertEqual(json.loads(out)["status"], "VOID")

		gs.message.sender_address = ADDR_BACKER_A
		out_refund = self.escrow.claim_refund(1)
		self.assertEqual(json.loads(out_refund)["payout"], str(4 * GEN))

	def test_double_claim_raises(self):
		self._fund(1, ADDR_BACKER_A, GEN)
		self.fake_registry.status = "FINALIZED"
		self.fake_registry.compliance_bps = 5000
		self.escrow.settle_fund(1)

		gs.message.sender_address = ADDR_BACKER_A
		self.escrow.claim_refund(1)
		with self.assertRaises(gs.UserError):
			self.escrow.claim_refund(1)

	def test_non_provider_cannot_claim_provider_share(self):
		self._fund(1, ADDR_BACKER_A, GEN)
		self.fake_registry.status = "FINALIZED"
		self.fake_registry.compliance_bps = 9000
		self.escrow.settle_fund(1)

		gs.message.sender_address = ADDR_BACKER_A
		with self.assertRaises(gs.UserError):
			self.escrow.claim_provider_share(1)

	def test_provider_snapshotted_at_open_survives_set_registry(self):
		self._fund(1, ADDR_BACKER_A, GEN)  # provider snapshotted here
		self.fake_registry.provider = ADDR_BACKER_B  # registry now claims a different provider

		self.fake_registry.status = "FINALIZED"
		self.fake_registry.compliance_bps = 10000
		self.escrow.settle_fund(1)

		gs.message.sender_address = ADDR_PROVIDER  # the ORIGINAL provider still claims fine
		out = self.escrow.claim_provider_share(1)
		self.assertTrue(json.loads(out)["ok"])

	# -------------------------------------------------------------- #
	# fee accounting / sweeping
	# -------------------------------------------------------------- #

	def test_withdraw_platform_fees_respects_locked_funds(self):
		self._fund(1, ADDR_BACKER_A, 10 * GEN)
		self.fake_registry.status = "FINALIZED"
		self.fake_registry.compliance_bps = 10000  # 100% compliant
		self.escrow.settle_fund(1)
		gs.set_balance(self.escrow, 10 * GEN)

		fee = (10 * GEN * 250) // 10000
		gs.message.sender_address = ADDR_OWNER
		self.escrow.withdraw_platform_fees(str(ADDR_OWNER), fee)
		self.assertEqual(gs.PAYMENTS[-1], (str(ADDR_OWNER), fee))

		with self.assertRaises(gs.UserError):
			self.escrow.withdraw_platform_fees(str(ADDR_OWNER), 1)  # sweep delay


if __name__ == "__main__":
	unittest.main()
