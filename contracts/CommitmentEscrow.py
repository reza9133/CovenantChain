# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# CommitmentEscrow -- second of three linked contracts, holds the money
# ============================================================================
#
# CovenantRegistry only ever judges; it never touches a wei. This contract
# is what actually lets people put GEN behind a provider's promise, and it
# is deployed second, wired to the first one's finished address:
#
#     genlayer deploy --contract contracts/CovenantRegistry.py   -> REGISTRY_ADDR
#     genlayer deploy --contract contracts/CommitmentEscrow.py --args REGISTRY_ADDR
#
# Everything CommitmentEscrow does about whether a covenant was honored
# comes from asking CovenantRegistry directly, through its view() methods --
# get_covenant_status, get_ends_epoch, is_finalized, get_compliance_bps,
# get_provider. This file holds no opinion of its own about what "honored"
# even means; it just spends what CovenantRegistry reports.
#
# Think of a fund here as a shared subscription rather than a bet slip:
#   * fund(covenant_id) lets anyone add GEN to a covenant's pool at any time
#     before that covenant's window closes. Backers are pooling money on
#     the provider's behalf, not staking against one another.
#   * once CovenantRegistry has finalized the covenant, settle_fund() -- an
#     open call, costing O(1) regardless of how many backers exist, since it
#     never loops over them -- reads the compliance score and splits the
#     pool along it:
#
#         gross_provider = total_funded * compliance_bps / 10000
#         fee            = gross_provider * platform_fee_bps / 10000
#         provider_net   = gross_provider - fee
#         refund_pool    = total_funded - gross_provider
#
#     The platform fee only ever comes out of what the provider actually
#     earned, so a provider judged entirely non-compliant leaves the whole
#     pool available for refund and the platform collects nothing on it --
#     the fee scales with verified performance, not with raw volume.
#   * the provider withdraws with one call to claim_provider_share(); each
#     backer withdraws their own slice of refund_pool, sized to their own
#     share of the original pool, with claim_refund(). A little integer-
#     division dust can be left unclaimed once everyone has pulled their
#     share -- that is expected, not a leak, and matches any pro-rata split
#     done in fixed-point arithmetic.
#
# fund() is the single spot in this entire three-contract project that ever
# receives GEN nobody asked it to hold on to, which makes it the one method
# that is not allowed to fail loudly: raising a UserError unwinds storage
# changes but does nothing to the value that already arrived with the call,
# so every rejection path here pays that value straight back out and reports
# `{"ok": false, ...}` instead of reverting. That promise has to survive
# CovenantRegistry misbehaving too -- an address that stops answering, a
# view call that reverts, a registry repointed somewhere that doesn't even
# expose these methods -- which is why every registry read inside
# _fund_problem() is wrapped by _try_registry() rather than called plainly.
# settle_fund() and the two claim_*() methods carry no GEN of their own, so
# unlike fund() they are allowed to simply raise when something is wrong; a
# reverted read-only call costs nothing and the caller can try again.
#
# Backers are never left holding a covenant that quietly disappears:
#   * a normal FINALIZED covenant settles into a graded payout as above;
#   * a covenant CovenantRegistry marks VOIDED -- canceled early by its own
#     creator, or left with no decisive attestation at all -- voids the
#     fund too, and every backer gets their exact contribution back;
#   * a covenant that simply never gets finalized does not hold GEN
#     hostage forever: once FINALIZATION_GRACE_SECONDS has elapsed past its
#     own end time with no finalization in sight, settle_fund() voids the
#     fund on its own initiative and backers can reclaim in full.
# ============================================================================

FUND_OPEN = "OPEN"
FUND_SETTLED = "SETTLED"
FUND_VOID = "VOID"

BPS_DENOM = 10000
DEFAULT_PLATFORM_FEE_BPS = 250
MAX_PLATFORM_FEE_BPS = 1000

MIN_FUND_AMOUNT = 10 ** 15
MAX_FUND_AMOUNT = 100 * 10 ** 18

FINALIZATION_GRACE_SECONDS = 14 * 86400
SWEEP_DELAY_SECONDS = 3600

MAX_PAGE = 40


def _now_epoch() -> int:
	return int(datetime.now(timezone.utc).timestamp())


def _clamp(value: int, lo: int, hi: int) -> int:
	if value < lo:
		return lo
	if value > hi:
		return hi
	return value


def _ckey(covenant_id: int, backer) -> str:
	return str(int(covenant_id)) + ":" + str(backer)


@gl.evm.contract_interface
class _Wallet:
	class View:
		pass

	class Write:
		pass


@gl.contract_interface
class _CovenantRegistry:
	class View:
		def get_covenant_status(self, covenant_id: int) -> str: ...
		def get_ends_epoch(self, covenant_id: int) -> int: ...
		def is_finalized(self, covenant_id: int) -> bool: ...
		def get_compliance_bps(self, covenant_id: int) -> int: ...
		def get_provider(self, covenant_id: int) -> str: ...

	class Write:
		pass


@allow_storage
@dataclass
class Fund:
	covenant_id: u32
	status: str
	total_funded: u128
	provider: Address
	compliance_bps: u32
	provider_amount: u128
	refund_pool: u128
	fee_taken: u128
	opened_epoch: u64
	settled_epoch: u64
	backer_count: u32
	provider_claimed: bool


@allow_storage
@dataclass
class Contribution:
	covenant_id: u32
	backer: Address
	amount: u128
	funded_epoch: u64
	claimed: bool


@allow_storage
@dataclass
class ProviderRecord:
	settled_count: u32
	sum_compliance_bps: u128
	total_secured: u128
	last_settled_epoch: u64


class CommitmentEscrow(gl.Contract):
	owner: Address
	paused: bool
	registry: Address

	funds: TreeMap[u32, Fund]
	contributions: TreeMap[str, Contribution]
	covenant_backers: TreeMap[u32, DynArray[Address]]

	providers: TreeMap[Address, ProviderRecord]

	platform_fee_bps: u32
	platform_fees_accrued: u128
	platform_fees_withdrawn: u128

	funds_locked: u128
	total_paid_providers: u128
	total_refunded: u128
	last_out_epoch: u64

	count_funds: u32
	count_backers: u32
	count_settled: u32
	count_void: u32

	def __init__(self, registry_address: str, platform_fee_bps: int = DEFAULT_PLATFORM_FEE_BPS):
		self.owner = gl.message.sender_address
		self.registry = Address(str(registry_address))
		self.paused = False
		self.platform_fee_bps = u32(_clamp(int(platform_fee_bps), 0, MAX_PLATFORM_FEE_BPS))

	# ------------------------------------------------------------------ #
	# internal helpers
	# ------------------------------------------------------------------ #

	def _require_owner(self) -> None:
		if str(gl.message.sender_address) != str(self.owner):
			raise gl.vm.UserError("caller is not the owner")

	def _registry_contract(self):
		return _CovenantRegistry(self.registry)

	def _try_registry(self, step: str, fn):
		"""Wraps one outbound view() call to CovenantRegistry so that
		whatever it throws -- a dead address, a reverted method, a registry
		that was repointed somewhere without these methods at all -- comes
		back as a plain (False, reason) pair instead of an exception. This
		is what keeps that failure from ever reaching fund() as a raised
		error, which matters because fund() is holding someone's GEN at
		that point and a raise would strand it instead of refunding it."""
		try:
			return True, fn()
		except Exception as e:
			return False, step + " failed: " + str(getattr(e, "message", e))[:150]

	def _pay(self, to, amount: int) -> None:
		if amount <= 0:
			return
		_Wallet(Address(str(to))).emit_transfer(value=u256(int(amount)))
		self.last_out_epoch = u64(_now_epoch())

	def _reject(self, sender, value: int, reason: str) -> str:
		if value > 0:
			self._pay(sender, value)
		return json.dumps({"ok": False, "reason": reason, "refunded": str(value)})

	def _fund_problem(self, covenant_id: int, value: int, now: int) -> str:
		if self.paused:
			return "CommitmentEscrow is paused for new funding"
		if value < MIN_FUND_AMOUNT:
			return "funding amount below minimum of " + str(MIN_FUND_AMOUNT) + " wei"
		if value > MAX_FUND_AMOUNT:
			return "funding amount above maximum of " + str(MAX_FUND_AMOUNT) + " wei per call"

		ok, status = self._try_registry("checking covenant status",
			lambda: self._registry_contract().view().get_covenant_status(int(covenant_id)))
		if not ok:
			return status
		if status != "ACTIVE":
			return "the underlying covenant is " + str(status) + ", not open for funding"

		ok, ends = self._try_registry("checking covenant end time",
			lambda: self._registry_contract().view().get_ends_epoch(int(covenant_id)))
		if not ok:
			return ends
		if now >= int(ends):
			return "funding closed: the covenant's review window has already ended"

		return ""

	def _void_fund(self, fund: Fund, now: int) -> None:
		fund.status = FUND_VOID
		fund.settled_epoch = u64(now)
		self.count_void = u32(int(self.count_void) + 1)

	# ------------------------------------------------------------------ #
	# funding / settlement / payout
	# ------------------------------------------------------------------ #

	@gl.public.write.payable
	def fund(self, covenant_id: int) -> str:
		sender = gl.message.sender_address
		value = int(gl.message.value)
		cid = int(covenant_id)
		now = _now_epoch()

		fund = self.funds.get_or_insert_default(u32(cid))
		fund_is_new = str(fund.status) == ""
		if not fund_is_new and str(fund.status) != FUND_OPEN:
			return self._reject(sender, value, "the funding pool for this covenant is already "
				+ str(fund.status))

		problem = self._fund_problem(cid, value, now)
		if problem != "":
			return self._reject(sender, value, problem)

		if fund_is_new:
			# A fund records who its provider is exactly once, right here,
			# rather than asking the registry again on every later call.
			# That one-time snapshot means a future set_registry() cannot
			# quietly redirect an already-open fund's eventual payout by
			# pointing this contract at a registry with a different answer.
			ok, provider_addr = self._try_registry("reading the covenant's provider address",
				lambda: self._registry_contract().view().get_provider(cid))
			if not ok:
				return self._reject(sender, value, provider_addr)

			fund.covenant_id = u32(cid)
			fund.status = FUND_OPEN
			fund.total_funded = u128(0)
			fund.provider = Address(str(provider_addr))
			fund.compliance_bps = u32(0)
			fund.provider_amount = u128(0)
			fund.refund_pool = u128(0)
			fund.fee_taken = u128(0)
			fund.opened_epoch = u64(now)
			fund.settled_epoch = u64(0)
			fund.backer_count = u32(0)
			fund.provider_claimed = False
			self.count_funds = u32(int(self.count_funds) + 1)

		ckey = _ckey(cid, sender)
		contribution = self.contributions.get(ckey)
		if contribution is None:
			contribution = self.contributions.get_or_insert_default(ckey)
			contribution.covenant_id = u32(cid)
			contribution.backer = sender
			contribution.amount = u128(value)
			contribution.funded_epoch = u64(now)
			contribution.claimed = False
			fund.backer_count = u32(int(fund.backer_count) + 1)
			self.covenant_backers.get_or_insert_default(u32(cid)).append(sender)
			self.count_backers = u32(int(self.count_backers) + 1)
		else:
			contribution.amount = u128(int(contribution.amount) + value)

		fund.total_funded = u128(int(fund.total_funded) + value)
		self.funds_locked = u128(int(self.funds_locked) + value)

		return json.dumps({"ok": True, "covenant_id": cid, "amount_added": str(value),
			"total_contribution": str(int(self.contributions.get(ckey).amount)),
			"fund_total": str(int(fund.total_funded))})

	@gl.public.write
	def settle_fund(self, covenant_id: int) -> str:
		cid = int(covenant_id)
		fund = self.funds.get(u32(cid))
		if fund is None:
			raise gl.vm.UserError("no funding pool exists for this covenant -- nobody has funded it yet")
		if str(fund.status) != FUND_OPEN:
			raise gl.vm.UserError("funding pool is already " + str(fund.status))

		now = _now_epoch()
		status = self._registry_contract().view().get_covenant_status(cid)

		if status == "VOIDED":
			self._void_fund(fund, now)
			return json.dumps({"ok": True, "covenant_id": cid, "status": FUND_VOID,
				"reason": "the underlying covenant was voided"})

		if status == "FINALIZED":
			compliance = _clamp(int(self._registry_contract().view().get_compliance_bps(cid)),
				0, BPS_DENOM)
			total = int(fund.total_funded)
			gross_provider = (total * compliance) // BPS_DENOM
			fee = (gross_provider * int(self.platform_fee_bps)) // BPS_DENOM
			provider_net = gross_provider - fee
			refund_pool = total - gross_provider

			fund.status = FUND_SETTLED
			fund.compliance_bps = u32(compliance)
			fund.provider_amount = u128(provider_net)
			fund.refund_pool = u128(refund_pool)
			fund.fee_taken = u128(fee)
			fund.settled_epoch = u64(now)

			self.platform_fees_accrued = u128(int(self.platform_fees_accrued) + fee)
			self.count_settled = u32(int(self.count_settled) + 1)

			record = self.providers.get_or_insert_default(fund.provider)
			record.settled_count = u32(int(record.settled_count) + 1)
			record.sum_compliance_bps = u128(int(record.sum_compliance_bps) + compliance)
			record.last_settled_epoch = u64(now)

			# funds_locked needs to drop by `fee` right now rather than
			# waiting for withdraw_platform_fees() to actually move it
			# out, or it would forever overstate what is still owed to
			# backers and the provider: once both of them have claimed
			# their shares, the fee is the only GEN left in the contract,
			# yet funds_locked would still count it as spoken for and
			# free_balance would read zero forever. Deducting it here
			# instead lets funds_locked settle at zero (give or take a
			# little rounding dust) once every claim has gone out, which
			# is what makes the fee actually withdrawable afterward.
			locked = int(self.funds_locked)
			self.funds_locked = u128(locked - fee if locked >= fee else 0)

			return json.dumps({"ok": True, "covenant_id": cid, "status": FUND_SETTLED,
				"compliance_bps": compliance, "provider_amount": str(provider_net),
				"refund_pool": str(refund_pool), "fee_taken": str(fee)})

		ends = int(self._registry_contract().view().get_ends_epoch(cid))
		if now >= int(ends) + FINALIZATION_GRACE_SECONDS:
			self._void_fund(fund, now)
			return json.dumps({"ok": True, "covenant_id": cid, "status": FUND_VOID,
				"reason": "the covenant was never finalized within the grace period after its window"})

		raise gl.vm.UserError("underlying covenant is not yet finalized (status: " + str(status)
			+ "); try again after finalization, or after the grace period elapses")

	@gl.public.write
	def claim_provider_share(self, covenant_id: int) -> str:
		sender = gl.message.sender_address
		cid = int(covenant_id)

		fund = self.funds.get(u32(cid))
		if fund is None:
			raise gl.vm.UserError("no funding pool exists for this covenant")
		if str(fund.status) == FUND_OPEN:
			raise gl.vm.UserError("funding pool is not yet settled; call settle_fund first")
		if str(fund.provider) != str(sender):
			raise gl.vm.UserError("only this covenant's registered provider may claim its share")
		if str(fund.status) == FUND_VOID:
			raise gl.vm.UserError("this covenant was voided; there is no provider share, only backer refunds")
		if bool(fund.provider_claimed):
			raise gl.vm.UserError("the provider share has already been claimed")

		fund.provider_claimed = True
		amount = int(fund.provider_amount)
		if amount > 0:
			record = self.providers.get_or_insert_default(sender)
			record.total_secured = u128(int(record.total_secured) + amount)
			self.total_paid_providers = u128(int(self.total_paid_providers) + amount)
			locked = int(self.funds_locked)
			self.funds_locked = u128(locked - amount if locked >= amount else 0)
			self._pay(sender, amount)

		return json.dumps({"ok": True, "covenant_id": cid, "payout": str(amount),
			"compliance_bps": int(fund.compliance_bps)})

	@gl.public.write
	def claim_refund(self, covenant_id: int) -> str:
		sender = gl.message.sender_address
		cid = int(covenant_id)

		fund = self.funds.get(u32(cid))
		if fund is None:
			raise gl.vm.UserError("no funding pool exists for this covenant")
		if str(fund.status) == FUND_OPEN:
			raise gl.vm.UserError("funding pool is not yet settled; call settle_fund first")

		contribution = self.contributions.get(_ckey(cid, sender))
		if contribution is None:
			raise gl.vm.UserError("you have no contribution to this covenant")
		if bool(contribution.claimed):
			raise gl.vm.UserError("this contribution has already been claimed")

		contribution.claimed = True
		amount_in = int(contribution.amount)

		if str(fund.status) == FUND_VOID:
			payout = amount_in
			note = "covenant voided: full refund of principal"
		else:
			total = int(fund.total_funded)
			refund_pool = int(fund.refund_pool)
			payout = (refund_pool * amount_in) // total if total > 0 else 0
			note = "covenant settled: proportional refund of the unearned share"

		self.total_refunded = u128(int(self.total_refunded) + payout)
		locked = int(self.funds_locked)
		self.funds_locked = u128(locked - payout if locked >= payout else 0)
		self._pay(sender, payout)

		return json.dumps({"ok": True, "covenant_id": cid, "payout": str(payout),
			"contributed": str(amount_in), "note": note})

	# ------------------------------------------------------------------ #
	# admin
	# ------------------------------------------------------------------ #

	@gl.public.write
	def set_paused(self, paused: bool) -> None:
		self._require_owner()
		self.paused = bool(paused)

	@gl.public.write
	def set_platform_fee_bps(self, fee_bps: int) -> None:
		self._require_owner()
		self.platform_fee_bps = u32(_clamp(int(fee_bps), 0, MAX_PLATFORM_FEE_BPS))

	@gl.public.write
	def set_registry(self, new_registry: str) -> None:
		"""Points this contract at a different CovenantRegistry deployment
		going forward. Anything already SETTLED or VOID keeps the outcome
		it was given and is untouched by this call. Even for funds still
		OPEN, the provider they will eventually pay was fixed the moment
		they were opened -- see the snapshot note in fund() -- so a
		repoint only changes which registry answers status questions for
		brand-new funds, never who an existing one is owed to."""
		self._require_owner()
		self.registry = Address(str(new_registry))

	@gl.public.write
	def transfer_ownership(self, new_owner: str) -> None:
		self._require_owner()
		self.owner = Address(str(new_owner))

	@gl.public.write
	def withdraw_platform_fees(self, to: str, amount: int) -> None:
		self._require_owner()
		now = _now_epoch()
		if int(self.last_out_epoch) > 0 and now < int(self.last_out_epoch) + SWEEP_DELAY_SECONDS:
			raise gl.vm.UserError("please wait " + str(SWEEP_DELAY_SECONDS)
				+ "s after the last payout before sweeping fees")

		available = int(self.platform_fees_accrued) - int(self.platform_fees_withdrawn)
		free_balance = int(self.balance) - int(self.funds_locked)
		withdrawable = min(available, free_balance if free_balance > 0 else 0)
		amt = int(amount)
		if amt <= 0 or amt > withdrawable:
			raise gl.vm.UserError("amount must be 1.." + str(withdrawable) + " wei (currently withdrawable)")

		self.platform_fees_withdrawn = u128(int(self.platform_fees_withdrawn) + amt)
		self._pay(Address(str(to)), amt)

	# ------------------------------------------------------------------ #
	# views -- this is the surface ProviderStanding.py is built against
	# ------------------------------------------------------------------ #

	def _fund_json(self, fund: Fund) -> dict:
		return {"covenant_id": int(fund.covenant_id), "status": str(fund.status),
			"total_funded": str(int(fund.total_funded)), "provider": str(fund.provider),
			"compliance_bps": int(fund.compliance_bps),
			"provider_amount": str(int(fund.provider_amount)),
			"refund_pool": str(int(fund.refund_pool)), "fee_taken": str(int(fund.fee_taken)),
			"opened_epoch": int(fund.opened_epoch), "settled_epoch": int(fund.settled_epoch),
			"backer_count": int(fund.backer_count), "provider_claimed": bool(fund.provider_claimed)}

	@gl.public.view
	def get_fund(self, covenant_id: int) -> str:
		fund = self.funds.get(u32(int(covenant_id)))
		if fund is None:
			return json.dumps({"found": False})
		out = self._fund_json(fund)
		out["found"] = True
		return json.dumps(out)

	@gl.public.view
	def get_contribution(self, covenant_id: int, backer: str) -> str:
		record = self.contributions.get(_ckey(int(covenant_id), Address(str(backer))))
		if record is None:
			return json.dumps({"found": False})
		return json.dumps({"found": True, "covenant_id": int(record.covenant_id),
			"backer": str(record.backer), "amount": str(int(record.amount)),
			"funded_epoch": int(record.funded_epoch), "claimed": bool(record.claimed)})

	@gl.public.view
	def has_provider_track_record(self, address: str, min_settled: int, min_avg_compliance_bps: int) -> bool:
		record = self.providers.get(Address(str(address)))
		if record is None:
			return int(min_settled) <= 0 and int(min_avg_compliance_bps) <= 0
		settled = int(record.settled_count)
		if settled < int(min_settled):
			return False
		avg = (int(record.sum_compliance_bps) // settled) if settled > 0 else 0
		return avg >= int(min_avg_compliance_bps)

	@gl.public.view
	def get_provider_stats(self, address: str) -> str:
		record = self.providers.get(Address(str(address)))
		if record is None:
			return json.dumps({"address": str(address), "settled_count": 0,
				"avg_compliance_bps": 0, "total_secured": "0"})
		settled = int(record.settled_count)
		avg = (int(record.sum_compliance_bps) // settled) if settled > 0 else 0
		return json.dumps({"address": str(address), "settled_count": settled,
			"avg_compliance_bps": avg, "total_secured": str(int(record.total_secured))})

	@gl.public.view
	def get_platform_stats(self) -> str:
		fees_available = int(self.platform_fees_accrued) - int(self.platform_fees_withdrawn)
		return json.dumps({"funds_locked": str(int(self.funds_locked)),
			"on_chain_balance": str(int(self.balance)),
			"platform_fees_accrued": str(int(self.platform_fees_accrued)),
			"platform_fees_withdrawn": str(int(self.platform_fees_withdrawn)),
			"platform_fees_available": str(fees_available),
			"total_paid_providers": str(int(self.total_paid_providers)),
			"total_refunded": str(int(self.total_refunded)),
			"funds": int(self.count_funds), "backers": int(self.count_backers),
			"settled": int(self.count_settled), "void": int(self.count_void)})

	@gl.public.view
	def get_config(self) -> str:
		return json.dumps({"owner": str(self.owner), "registry": str(self.registry),
			"paused": bool(self.paused), "platform_fee_bps": int(self.platform_fee_bps),
			"max_platform_fee_bps": MAX_PLATFORM_FEE_BPS,
			"min_fund_amount": str(MIN_FUND_AMOUNT), "max_fund_amount": str(MAX_FUND_AMOUNT),
			"finalization_grace_seconds": FINALIZATION_GRACE_SECONDS,
			"sweep_delay_seconds": SWEEP_DELAY_SECONDS})
