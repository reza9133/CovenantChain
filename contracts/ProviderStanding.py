# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# ProviderStanding -- third of three linked contracts, turns a track record
# into a rating
# ============================================================================
#
# The chain finishes here. This contract is deployed last, wired to the
# escrow's address the same way the escrow was wired to the registry's:
#
#     genlayer deploy --contract contracts/CovenantRegistry.py    -> REGISTRY_ADDR
#     genlayer deploy --contract contracts/CommitmentEscrow.py  --args REGISTRY_ADDR   -> ESCROW_ADDR
#     genlayer deploy --contract contracts/ProviderStanding.py  --args ESCROW_ADDR
#
# It has no notion of check URLs, page fetches, or GEN, and it never talks
# to CovenantRegistry at all -- everything it needs comes from two view()
# calls into CommitmentEscrow: how many of a provider's funds have settled,
# and their average compliance score across those settlements. Those two
# numbers get mapped onto a short ladder of named tiers (VERIFIED / RELIABLE
# / TRUSTED / ELITE, by default). Nothing here ever moves value, so nothing
# here has to defend against a payable call reverting mid-transfer.
#
# The one design choice worth calling out is that a rating here has a
# shelf life. A provider's reliability is not a fact fixed in the past the
# way "this address has settled N times" is -- someone who earned a strong
# record a year ago and has since gone quiet shouldn't get to point at an
# old certificate as if it still describes them today. So the contract
# keeps two separate questions apart on purpose:
#   - is_currently_eligible() always recomputes the answer fresh from
#     whatever CommitmentEscrow reports right now. Nobody can pre-load it,
#     and nobody -- not even this contract's owner -- can take it away from
#     a provider whose numbers genuinely qualify.
#   - certify() is what actually writes something to storage: a dated
#     record saying eligibility was confirmed on that day, good for
#     CERT_VALIDITY_SECONDS. is_certified() only looks at that record, not
#     at live numbers, so once CERT_VALIDITY_SECONDS has passed the record
#     reads as not-certified even if the provider is still fully eligible
#     -- they just need to call certify() again, which costs nothing but a
#     transaction. The owner can also revoke_certificate() a specific
#     record for administrative reasons; that never touches
#     is_currently_eligible(), which stays purely a function of the
#     escrow's numbers.
# ============================================================================

MAX_TIER_NAME_LEN = 24
MAX_TIERS = 12
DEFAULT_TIERS = (
	("VERIFIED", 1, 0),
	("RELIABLE", 5, 6000),
	("TRUSTED", 15, 8000),
	("ELITE", 40, 9000),
)  # (name, min_settled_funds, min_avg_compliance_bps)

CERT_VALIDITY_SECONDS = 30 * 86400


def _now_epoch() -> int:
	return int(datetime.now(timezone.utc).timestamp())


def _ckey(provider, tier: str) -> str:
	return str(provider) + ":" + str(tier).strip().upper()


@gl.contract_interface
class _CommitmentEscrow:
	class View:
		def has_provider_track_record(self, address: str, min_settled: int,
			min_avg_compliance_bps: int) -> bool: ...
		def get_provider_stats(self, address: str) -> str: ...

	class Write:
		pass


@allow_storage
@dataclass
class Certificate:
	provider: Address
	tier: str
	avg_compliance_bps_at_issue: u32
	settled_count_at_issue: u32
	issued_epoch: u64
	expires_epoch: u64
	revoked: bool


class ProviderStanding(gl.Contract):
	owner: Address
	paused: bool
	escrow: Address

	tiers: DynArray[str]
	tier_min_settled: TreeMap[str, u32]
	tier_min_avg_bps: TreeMap[str, u32]

	certificates: TreeMap[str, Certificate]
	provider_tiers: TreeMap[Address, DynArray[str]]

	count_certificates: u32

	def __init__(self, escrow_address: str):
		self.owner = gl.message.sender_address
		self.escrow = Address(str(escrow_address))
		self.paused = False
		for name, min_settled, min_avg_bps in DEFAULT_TIERS:
			self.tiers.append(name)
			self.tier_min_settled[name] = u32(min_settled)
			self.tier_min_avg_bps[name] = u32(min_avg_bps)

	# ------------------------------------------------------------------ #
	# internal helpers
	# ------------------------------------------------------------------ #

	def _require_owner(self) -> None:
		if str(gl.message.sender_address) != str(self.owner):
			raise gl.vm.UserError("caller is not the owner")

	def _escrow_contract(self):
		return _CommitmentEscrow(self.escrow)

	def _require_known_tier(self, tier: str) -> str:
		t = str(tier).strip().upper()
		if self.tier_min_settled.get(t) is None:
			raise gl.vm.UserError("unknown tier '" + t + "'")
		return t

	def _stats(self, provider) -> dict:
		snapshot = self._escrow_contract().view().get_provider_stats(str(provider))
		try:
			data = json.loads(snapshot)
			return {"settled_count": int(data.get("settled_count", 0)),
				"avg_compliance_bps": int(data.get("avg_compliance_bps", 0))}
		except Exception:
			return {"settled_count": 0, "avg_compliance_bps": 0}

	# ------------------------------------------------------------------ #
	# certificates
	# ------------------------------------------------------------------ #

	@gl.public.write
	def certify(self, tier: str) -> str:
		if self.paused:
			raise gl.vm.UserError("ProviderStanding is paused for new certificates")

		provider = gl.message.sender_address
		t = self._require_known_tier(tier)
		required_settled = int(self.tier_min_settled.get(t))
		required_bps = int(self.tier_min_avg_bps.get(t))

		eligible = self._escrow_contract().view().has_provider_track_record(
			str(provider), required_settled, required_bps)
		if not eligible:
			stats = self._stats(provider)
			raise gl.vm.UserError("not eligible for " + t + ": needs " + str(required_settled)
				+ " settled funds at >=" + str(required_bps) + " avg compliance bps, currently has "
				+ str(stats["settled_count"]) + " at " + str(stats["avg_compliance_bps"]) + " bps")

		stats = self._stats(provider)
		now = _now_epoch()
		key = _ckey(provider, t)
		cert = self.certificates.get(key)
		is_new = cert is None
		cert = self.certificates.get_or_insert_default(key)
		cert.provider = provider
		cert.tier = t
		cert.avg_compliance_bps_at_issue = u32(stats["avg_compliance_bps"])
		cert.settled_count_at_issue = u32(stats["settled_count"])
		cert.issued_epoch = u64(now)
		cert.expires_epoch = u64(now + CERT_VALIDITY_SECONDS)
		cert.revoked = False

		if is_new:
			self.provider_tiers.get_or_insert_default(provider).append(t)
			self.count_certificates = u32(int(self.count_certificates) + 1)

		return json.dumps({"ok": True, "provider": str(provider), "tier": t,
			"issued_epoch": now, "expires_epoch": now + CERT_VALIDITY_SECONDS,
			"avg_compliance_bps_at_issue": stats["avg_compliance_bps"]})

	@gl.public.write
	def revoke_certificate(self, provider: str, tier: str) -> None:
		self._require_owner()
		t = self._require_known_tier(tier)
		cert = self.certificates.get(_ckey(Address(str(provider)), t))
		if cert is None:
			raise gl.vm.UserError("no such certificate on record")
		cert.revoked = True

	# ------------------------------------------------------------------ #
	# admin
	# ------------------------------------------------------------------ #

	@gl.public.write
	def add_or_update_tier(self, name: str, min_settled: int, min_avg_compliance_bps: int) -> None:
		self._require_owner()
		n = str(name).strip().upper()
		if len(n) == 0 or len(n) > MAX_TIER_NAME_LEN:
			raise gl.vm.UserError("tier name must be 1.." + str(MAX_TIER_NAME_LEN) + " characters")
		if int(min_settled) < 0:
			raise gl.vm.UserError("min_settled cannot be negative")
		if int(min_avg_compliance_bps) < 0 or int(min_avg_compliance_bps) > 10000:
			raise gl.vm.UserError("min_avg_compliance_bps must be 0..10000")
		if self.tier_min_settled.get(n) is None:
			if len(self.tiers) >= MAX_TIERS:
				raise gl.vm.UserError("at most " + str(MAX_TIERS) + " tiers are supported")
			self.tiers.append(n)
		self.tier_min_settled[n] = u32(int(min_settled))
		self.tier_min_avg_bps[n] = u32(int(min_avg_compliance_bps))

	@gl.public.write
	def set_paused(self, paused: bool) -> None:
		self._require_owner()
		self.paused = bool(paused)

	@gl.public.write
	def set_escrow(self, new_escrow: str) -> None:
		self._require_owner()
		self.escrow = Address(str(new_escrow))

	@gl.public.write
	def transfer_ownership(self, new_owner: str) -> None:
		self._require_owner()
		self.owner = Address(str(new_owner))

	# ------------------------------------------------------------------ #
	# views
	# ------------------------------------------------------------------ #

	@gl.public.view
	def is_currently_eligible(self, provider: str, tier: str) -> bool:
		t = self._require_known_tier(tier)
		required_settled = int(self.tier_min_settled.get(t))
		required_bps = int(self.tier_min_avg_bps.get(t))
		return bool(self._escrow_contract().view().has_provider_track_record(
			str(provider), required_settled, required_bps))

	@gl.public.view
	def is_certified(self, provider: str, tier: str) -> bool:
		"""Looks only at the STORED record, never at live numbers -- a
		certificate past its expiry (or one the owner revoked) reads as
		not-certified even for a provider who would still sail through
		is_currently_eligible(). Getting a fresh one just means calling
		certify() again."""
		t = self._require_known_tier(tier)
		cert = self.certificates.get(_ckey(Address(str(provider)), t))
		if cert is None or bool(cert.revoked):
			return False
		return _now_epoch() < int(cert.expires_epoch)

	@gl.public.view
	def best_tier_currently_eligible(self, provider: str) -> str:
		"""Whichever tier this address would qualify for right this
		moment, worked out fresh from CommitmentEscrow's numbers. A
		provider does not need to have ever called certify(), and an
		expired or revoked certificate has no bearing on the answer."""
		stats = self._stats(Address(str(provider)))
		best = ""
		# Rank on both requirement dimensions together (not just
		# min_settled) so that a custom tier added via add_or_update_tier
		# with a low min_settled but a high min_avg_compliance_bps is not
		# silently mis-ranked below an easier tier. DEFAULT_TIERS happens
		# to have both dimensions rise together, which is what let the
		# single-field comparison look correct before.
		best_rank = (-1, -1)
		i = 0
		while i < len(self.tiers):
			name = self.tiers[i]
			req_settled = int(self.tier_min_settled.get(name))
			req_bps = int(self.tier_min_avg_bps.get(name))
			rank = (req_settled, req_bps)
			if stats["settled_count"] >= req_settled and stats["avg_compliance_bps"] >= req_bps \
					and rank >= best_rank:
				best = name
				best_rank = rank
			i += 1
		return json.dumps({"provider": str(provider), "settled_count": stats["settled_count"],
			"avg_compliance_bps": stats["avg_compliance_bps"], "best_tier": best})

	@gl.public.view
	def get_certificate(self, provider: str, tier: str) -> str:
		t = self._require_known_tier(tier)
		cert = self.certificates.get(_ckey(Address(str(provider)), t))
		if cert is None:
			return json.dumps({"found": False})
		return json.dumps({"found": True, "provider": str(cert.provider), "tier": str(cert.tier),
			"avg_compliance_bps_at_issue": int(cert.avg_compliance_bps_at_issue),
			"settled_count_at_issue": int(cert.settled_count_at_issue),
			"issued_epoch": int(cert.issued_epoch), "expires_epoch": int(cert.expires_epoch),
			"revoked": bool(cert.revoked), "expired": _now_epoch() >= int(cert.expires_epoch)})

	@gl.public.view
	def get_certificates_by_provider(self, provider: str) -> str:
		p = Address(str(provider))
		tier_names = self.provider_tiers.get(p)
		if tier_names is None:
			return json.dumps({"provider": str(p), "certificates": []})
		out = []
		for t in tier_names:
			cert = self.certificates.get(_ckey(p, t))
			if cert is not None:
				out.append({"tier": str(cert.tier),
					"avg_compliance_bps_at_issue": int(cert.avg_compliance_bps_at_issue),
					"issued_epoch": int(cert.issued_epoch), "expires_epoch": int(cert.expires_epoch),
					"revoked": bool(cert.revoked), "expired": _now_epoch() >= int(cert.expires_epoch)})
		return json.dumps({"provider": str(p), "certificates": out})

	@gl.public.view
	def get_tiers(self) -> str:
		out = []
		for name in self.tiers:
			out.append({"tier": name, "min_settled": int(self.tier_min_settled.get(name)),
				"min_avg_compliance_bps": int(self.tier_min_avg_bps.get(name))})
		return json.dumps({"tiers": out})

	@gl.public.view
	def get_provider_snapshot(self, provider: str) -> str:
		return self._escrow_contract().view().get_provider_stats(str(provider))

	@gl.public.view
	def get_config(self) -> str:
		return json.dumps({"owner": str(self.owner), "escrow": str(self.escrow),
			"paused": bool(self.paused), "total_certificates": int(self.count_certificates),
			"cert_validity_seconds": CERT_VALIDITY_SECONDS, "max_tiers": MAX_TIERS})
