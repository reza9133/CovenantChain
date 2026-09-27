import sys
import json
import unittest

sys.path.insert(0, "/home/claude/work/CovenantChain/test")
sys.path.insert(0, "/home/claude/work/CovenantChain/contracts")
import genlayer_stub as gs

ADDR_OWNER = gs.Address("0x1000000000000000000000000000000000000001")
ADDR_PROVIDER = gs.Address("0x2000000000000000000000000000000000000002")
ADDR_ESCROW = gs.Address("0x5000000000000000000000000000000000000005")

NOW = 1_800_000_000


class FakeEscrow:
	def __init__(self):
		self.settled_count = 0
		self.avg_bps = 0

	def view(self):
		return self

	def has_provider_track_record(self, address, min_settled, min_avg_compliance_bps):
		if self.settled_count < int(min_settled):
			return False
		return self.avg_bps >= int(min_avg_compliance_bps)

	def get_provider_stats(self, address):
		return json.dumps({"address": address, "settled_count": self.settled_count,
			"avg_compliance_bps": self.avg_bps, "total_secured": "0"})


class StandingTest(unittest.TestCase):
	def setUp(self):
		gs.reset()
		gs.message.sender_address = ADDR_OWNER
		mod = gs.load_contract("/home/claude/work/CovenantChain/contracts/ProviderStanding.py",
			"provider_standing")
		self.mod = mod
		self._now = NOW
		mod._now_epoch = lambda: self._now
		self.fake_escrow = FakeEscrow()
		gs.CONTRACT_REGISTRY[str(ADDR_ESCROW)] = self.fake_escrow
		self.standing = mod.ProviderStanding(str(ADDR_ESCROW))

	def test_default_tiers_present(self):
		tiers = json.loads(self.standing.get_tiers())["tiers"]
		names = [t["tier"] for t in tiers]
		self.assertEqual(names, ["VERIFIED", "RELIABLE", "TRUSTED", "ELITE"])

	def test_verified_tier_requires_almost_nothing(self):
		self.fake_escrow.settled_count = 1
		self.fake_escrow.avg_bps = 0
		gs.message.sender_address = ADDR_PROVIDER
		self.assertTrue(self.standing.is_currently_eligible(str(ADDR_PROVIDER), "VERIFIED"))
		self.assertFalse(self.standing.is_currently_eligible(str(ADDR_PROVIDER), "RELIABLE"))

	def test_certify_rejects_when_not_eligible(self):
		self.fake_escrow.settled_count = 0
		gs.message.sender_address = ADDR_PROVIDER
		with self.assertRaises(gs.UserError):
			self.standing.certify("RELIABLE")

	def test_certify_then_is_certified(self):
		self.fake_escrow.settled_count = 20
		self.fake_escrow.avg_bps = 8500
		gs.message.sender_address = ADDR_PROVIDER
		out = self.standing.certify("TRUSTED")
		data = json.loads(out)
		self.assertTrue(data["ok"])
		self.assertTrue(self.standing.is_certified(str(ADDR_PROVIDER), "TRUSTED"))

	def test_certificate_expires(self):
		self.fake_escrow.settled_count = 20
		self.fake_escrow.avg_bps = 8500
		gs.message.sender_address = ADDR_PROVIDER
		self.standing.certify("TRUSTED")
		self.assertTrue(self.standing.is_certified(str(ADDR_PROVIDER), "TRUSTED"))

		self._now += self.mod.CERT_VALIDITY_SECONDS + 1
		self.assertFalse(self.standing.is_certified(str(ADDR_PROVIDER), "TRUSTED"))
		# but live eligibility is untouched by the passage of time alone
		self.assertTrue(self.standing.is_currently_eligible(str(ADDR_PROVIDER), "TRUSTED"))

	def test_certificate_renewal_after_expiry(self):
		self.fake_escrow.settled_count = 20
		self.fake_escrow.avg_bps = 8500
		gs.message.sender_address = ADDR_PROVIDER
		self.standing.certify("TRUSTED")
		self._now += self.mod.CERT_VALIDITY_SECONDS + 1
		self.assertFalse(self.standing.is_certified(str(ADDR_PROVIDER), "TRUSTED"))

		self.standing.certify("TRUSTED")  # renew
		self.assertTrue(self.standing.is_certified(str(ADDR_PROVIDER), "TRUSTED"))

	def test_revoke_certificate_does_not_affect_live_eligibility(self):
		self.fake_escrow.settled_count = 20
		self.fake_escrow.avg_bps = 8500
		gs.message.sender_address = ADDR_PROVIDER
		self.standing.certify("TRUSTED")

		gs.message.sender_address = ADDR_OWNER
		self.standing.revoke_certificate(str(ADDR_PROVIDER), "TRUSTED")

		self.assertFalse(self.standing.is_certified(str(ADDR_PROVIDER), "TRUSTED"))
		self.assertTrue(self.standing.is_currently_eligible(str(ADDR_PROVIDER), "TRUSTED"))

	def test_best_tier_currently_eligible(self):
		self.fake_escrow.settled_count = 16
		self.fake_escrow.avg_bps = 8100
		out = json.loads(self.standing.best_tier_currently_eligible(str(ADDR_PROVIDER)))
		self.assertEqual(out["best_tier"], "TRUSTED")

	def test_add_or_update_tier_owner_only(self):
		gs.message.sender_address = ADDR_PROVIDER
		with self.assertRaises(gs.UserError):
			self.standing.add_or_update_tier("LEGENDARY", 100, 9900)

		gs.message.sender_address = ADDR_OWNER
		self.standing.add_or_update_tier("LEGENDARY", 100, 9900)
		tiers = [t["tier"] for t in json.loads(self.standing.get_tiers())["tiers"]]
		self.assertIn("LEGENDARY", tiers)


if __name__ == "__main__":
	unittest.main()
