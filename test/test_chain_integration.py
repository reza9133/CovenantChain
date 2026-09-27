"""
End-to-end test of the real three-contract chain: a real CovenantRegistry
instance, a real CommitmentEscrow instance constructed with that registry's
address, and a real ProviderStanding instance constructed with that escrow's
address -- each one calling the one before it exactly the way they would
on-chain, via stub.deploy_and_register() instead of a hand-written fake.
The per-file unit tests already cover edge cases against lightweight fakes;
this file's job is only to prove the three real contracts actually fit
together end to end, the same shape as `genlayer deploy` three times in a
row and wiring each printed address into the next constructor.

Run with:  python3 -m unittest discover -s test -v
"""
import json
import unittest
from pathlib import Path

import genlayer_stub as stub

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"

REGISTRY_ADDR = stub.Address("0xE000000000000000000000000000000000000E0")
ESCROW_ADDR = stub.Address("0xE000000000000000000000000000000000000E1")
STANDING_ADDR = stub.Address("0xE000000000000000000000000000000000000E2")

DEPLOYER = stub.Address("0x1111111111111111111111111111111111111111")
CREATOR = stub.Address("0x2222222222222222222222222222222222222222")
PROVIDER = stub.Address("0x3333333333333333333333333333333333333333")
BACKER_A = stub.Address("0x4444444444444444444444444444444444444444")
BACKER_B = stub.Address("0x5555555555555555555555555555555555555555")

GEN = 10 ** 18
DAY = 86400


def send_as(sender, value=0):
	stub.message.sender_address = sender
	stub.message.value = value


def mock_verdict(verdict, confidence=85, note="page checked"):
	payload = {"verdict": verdict, "confidence": confidence, "note": note}
	stub.NONDET_HOOKS["web_render"] = lambda url, mode: "the page shows a status"
	stub.NONDET_HOOKS["exec_prompt"] = lambda prompt, response_format: payload


class ChainIntegrationTestCase(unittest.TestCase):
	def setUp(self):
		stub.reset()

		self.registry_mod = stub.load_contract(CONTRACTS_DIR / "CovenantRegistry.py", "chain_registry")
		self.escrow_mod = stub.load_contract(CONTRACTS_DIR / "CommitmentEscrow.py", "chain_escrow")
		self.standing_mod = stub.load_contract(CONTRACTS_DIR / "ProviderStanding.py", "chain_standing")

		send_as(DEPLOYER)
		self.registry = stub.deploy_and_register(self.registry_mod.CovenantRegistry, REGISTRY_ADDR)

		send_as(DEPLOYER)
		self.escrow = stub.deploy_and_register(
			self.escrow_mod.CommitmentEscrow, ESCROW_ADDR, str(REGISTRY_ADDR))

		send_as(DEPLOYER)
		self.standing = stub.deploy_and_register(
			self.standing_mod.ProviderStanding, STANDING_ADDR, str(ESCROW_ADDR))

	def _freeze_clocks(self, now):
		self.registry_mod._now_epoch = lambda: now
		self.escrow_mod._now_epoch = lambda: now
		self.standing_mod._now_epoch = lambda: now

	def test_full_chain_happy_path_settles_and_certifies(self):
		base_now = self.registry_mod._now_epoch()
		self._freeze_clocks(base_now)

		# 1. a covenant is registered on CovenantRegistry (link 1)
		send_as(CREATOR)
		starts = base_now + 120
		ends = starts + DAY
		created = json.loads(self.registry.create_covenant(
			str(PROVIDER), "the status page reads operational", "https://example.org/status",
			"use the official status page only", starts, ends))
		covenant_id = created["covenant_id"]
		self.assertEqual(created["status"], "ACTIVE")

		# 2. two backers pool GEN behind it on CommitmentEscrow (link 2)
		send_as(BACKER_A, 3 * GEN)
		out_a = json.loads(self.escrow.fund(covenant_id))
		self.assertTrue(out_a["ok"])

		send_as(BACKER_B, 7 * GEN)
		out_b = json.loads(self.escrow.fund(covenant_id))
		self.assertTrue(out_b["ok"])

		# 3. the window opens; two HONORED attestations, one BREACHED
		self._freeze_clocks(starts + 10)
		mock_verdict("HONORED")
		self.registry.attest(covenant_id)

		self._freeze_clocks(starts + 10 + self.registry_mod.MIN_ATTESTATION_GAP_SECONDS + 1)
		mock_verdict("HONORED")
		self.registry.attest(covenant_id)

		self._freeze_clocks(starts + 10 + 2 * (self.registry_mod.MIN_ATTESTATION_GAP_SECONDS + 1))
		mock_verdict("BREACHED")
		self.registry.attest(covenant_id)

		# 4. the window ends; anyone finalizes on the registry
		self._freeze_clocks(ends + 10)
		finalized = json.loads(self.registry.finalize_covenant(covenant_id))
		self.assertEqual(finalized["status"], "FINALIZED")
		self.assertEqual(finalized["compliance_bps"], 6666)  # 2 honored / 3 decisive

		# 5. anyone settles the fund on the escrow -- it reads compliance
		#    straight from the real registry instance, no fake involved
		settled = json.loads(self.escrow.settle_fund(covenant_id))
		self.assertEqual(settled["status"], "SETTLED")
		self.assertEqual(settled["compliance_bps"], 6666)

		total = 10 * GEN
		gross_provider = (total * 6666) // 10000
		fee = (gross_provider * 250) // 10000
		provider_net = gross_provider - fee
		refund_pool = total - gross_provider

		# 6. the provider and both backers pull their share
		send_as(PROVIDER)
		payout = json.loads(self.escrow.claim_provider_share(covenant_id))
		self.assertEqual(payout["payout"], str(provider_net))

		send_as(BACKER_A)
		refund_a = json.loads(self.escrow.claim_refund(covenant_id))
		self.assertEqual(refund_a["payout"], str((refund_pool * 3 * GEN) // total))

		send_as(BACKER_B)
		refund_b = json.loads(self.escrow.claim_refund(covenant_id))
		self.assertEqual(refund_b["payout"], str((refund_pool * 7 * GEN) // total))

		# 7. ProviderStanding (link 3) reads the provider's live record
		#    straight from the real escrow instance and certifies a tier
		self.assertTrue(self.standing.is_currently_eligible(str(PROVIDER), "VERIFIED"))
		self.assertFalse(self.standing.is_currently_eligible(str(PROVIDER), "RELIABLE"))  # needs 5 settled

		send_as(PROVIDER)
		cert = json.loads(self.standing.certify("VERIFIED"))
		self.assertTrue(cert["ok"])
		self.assertTrue(self.standing.is_certified(str(PROVIDER), "VERIFIED"))

		snapshot = json.loads(self.standing.get_provider_snapshot(str(PROVIDER)))
		self.assertEqual(snapshot["settled_count"], 1)
		self.assertEqual(snapshot["avg_compliance_bps"], 6666)

	def test_voided_covenant_flows_through_to_full_refund(self):
		base_now = self.registry_mod._now_epoch()
		self._freeze_clocks(base_now)

		send_as(CREATOR)
		starts = base_now + 1000
		ends = starts + DAY
		created = json.loads(self.registry.create_covenant(
			str(PROVIDER), "the API stays reachable", "https://example.org/health", "", starts, ends))
		covenant_id = created["covenant_id"]

		send_as(BACKER_A, 4 * GEN)
		self.escrow.fund(covenant_id)

		# creator cancels before the window even opens
		send_as(CREATOR)
		voided = json.loads(self.registry.cancel_covenant(covenant_id))
		self.assertEqual(voided["status"], "VOIDED")

		settled = json.loads(self.escrow.settle_fund(covenant_id))
		self.assertEqual(settled["status"], "VOID")

		send_as(BACKER_A)
		refund = json.loads(self.escrow.claim_refund(covenant_id))
		self.assertEqual(refund["payout"], str(4 * GEN))

	def test_certificate_expires_and_requires_renewal_across_the_whole_chain(self):
		base_now = self.registry_mod._now_epoch()
		self._freeze_clocks(base_now)

		send_as(CREATOR)
		starts = base_now + 100
		ends = starts + self.registry_mod.MIN_WINDOW_SECONDS
		created = json.loads(self.registry.create_covenant(
			str(PROVIDER), "the dashboard reads green", "https://example.org/dash", "", starts, ends))
		covenant_id = created["covenant_id"]

		self._freeze_clocks(starts + 10)
		mock_verdict("HONORED")
		self.registry.attest(covenant_id)

		send_as(BACKER_A, GEN)
		self.escrow.fund(covenant_id)

		self._freeze_clocks(ends + 10)
		self.registry.finalize_covenant(covenant_id)

		self.escrow.settle_fund(covenant_id)  # records provider reputation on the escrow

		send_as(PROVIDER)
		self.standing.certify("VERIFIED")
		self.assertTrue(self.standing.is_certified(str(PROVIDER), "VERIFIED"))

		self._freeze_clocks(ends + 10 + self.standing_mod.CERT_VALIDITY_SECONDS + 1)
		self.assertFalse(self.standing.is_certified(str(PROVIDER), "VERIFIED"))
		self.assertTrue(self.standing.is_currently_eligible(str(PROVIDER), "VERIFIED"))

		send_as(PROVIDER)
		self.standing.certify("VERIFIED")  # renew
		self.assertTrue(self.standing.is_certified(str(PROVIDER), "VERIFIED"))


if __name__ == "__main__":
	unittest.main()
