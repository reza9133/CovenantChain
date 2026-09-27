# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# CovenantRegistry -- first of three linked contracts
# ============================================================================
#
# Picture a hosting provider that publicly promises "our status page will
# read operational" for the length of a subscription period, or a supplier
# that promises "our tracking page will read in-transit or delivered" until
# an order is closed out. Those are the kind of standing promises this
# contract exists to audit: a provider (or anyone acting on their behalf)
# opens a "covenant" naming the promise, the page that should demonstrate
# it, and a start/end window during which the promise is expected to hold.
#
# Where a typical resolver settles one question exactly once, a covenant is
# built to be sampled repeatedly. While the window is open, any address may
# call attest() -- no permission needed, no cooldown beyond a fixed minimum
# gap between calls -- and each call independently loads the page and hands
# it to an LLM, which answers with exactly one of HONORED, BREACHED, or
# INCONCLUSIVE for that instant. Nothing is decided from a single call.
# Only once the window closes does finalize_covenant() collapse the whole
# history of individual answers into one number: a compliance score in
# basis points, equal to honored calls divided by (honored + breached)
# calls. INCONCLUSIVE calls sit out of that division entirely, since they
# represent moments the page simply didn't answer the question either way.
#
# That graded, repeated-sampling design is the whole point: it hands the
# next contract in the chain something a single yes/no verdict cannot --
# a percentage a payment can scale against, rather than a binary win/lose.
#
# CovenantRegistry itself never sees or moves a single unit of GEN. It is
# meant to be read from, not paid into, and two further contracts are
# deployed on top of it in sequence, each one wired to the address of the
# contract immediately before it:
#
#   1. CovenantRegistry      (this file -- deploy it first)
#   2. CommitmentEscrow.py   (built with CovenantRegistry's address; this is
#                             where GEN actually changes hands)
#   3. ProviderStanding.py   (built with CommitmentEscrow's address; turns a
#                             provider's track record into a tiered rating)
#
# Because no write method here is `@gl.public.write.payable`, nothing in
# this file has to worry about a value-bearing transaction reverting --
# an ordinary UserError just costs the caller a retry. CommitmentEscrow's
# fund() cannot afford that luxury, since it does carry GEN; see its own
# header for how it copes.
# ============================================================================

STATUS_ACTIVE = "ACTIVE"
STATUS_FINALIZED = "FINALIZED"
STATUS_VOIDED = "VOIDED"

VERDICT_HONORED = "HONORED"
VERDICT_BREACHED = "BREACHED"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
ALLOWED_VERDICTS = (VERDICT_HONORED, VERDICT_BREACHED, VERDICT_INCONCLUSIVE)

MAX_COMMITMENT_LEN = 220
MAX_CRITERIA_LEN = 500
MAX_URL_LEN = 400
MAX_NOTE_CHARS = 200
MAX_RENDER_CHARS = 6000

MIN_LEAD_SECONDS = 60
MAX_LEAD_SECONDS = 2 * 365 * 86400
MIN_WINDOW_SECONDS = 600
MAX_WINDOW_SECONDS = 180 * 86400

MIN_ATTESTATION_GAP_SECONDS = 300
MAX_ATTESTATIONS_PER_COVENANT = 60

RENDER_WAIT_AFTER_LOADED = "3s"

MAX_SCAN = 200
MAX_PAGE = 40
MAX_RECENT = 20

# attest() can fail for reasons that deserve different treatment once it
# reaches consensus, so failures are tagged with a prefix instead of being
# left as plain strings:
#   - an ordinary UserError with no prefix means the caller did something
#     wrong (unknown covenant, window not open yet, called again too soon)
#     and re-running the same call will not help;
#   - ERR_TRANSIENT_FETCH / ERR_TRANSIENT_LLM mean the page or the model
#     provider itself misbehaved -- a leader and a validator both hitting
#     the same outage is ordinary bad luck, not a disagreement, so the
#     validator function below treats two matching transient tags as
#     agreement rather than as a reason to reject the round;
#   - ERR_VERDICT_MALFORMED means the model answered but not usefully --
#     here disagreement is the correct outcome, since accepting a broken
#     verdict into permanent state would be worse than rotating leaders.
ERR_TRANSIENT_FETCH = "[TRANSIENT_FETCH]"
ERR_TRANSIENT_LLM = "[TRANSIENT_LLM]"
ERR_VERDICT_MALFORMED = "[VERDICT_MALFORMED]"
_TRANSIENT_PREFIXES = (ERR_TRANSIENT_FETCH, ERR_TRANSIENT_LLM)


def _now_epoch() -> int:
	return int(datetime.now(timezone.utc).timestamp())


def _clamp(value: int, lo: int, hi: int) -> int:
	if value < lo:
		return lo
	if value > hi:
		return hi
	return value


def _clean_json(text: str):
	first = text.find("{")
	last = text.rfind("}")
	if first == -1 or last == -1 or last < first:
		return None
	try:
		return json.loads(text[first:last + 1])
	except Exception:
		return None


def _starts_with_any(text: str, prefixes) -> bool:
	for p in prefixes:
		if text.startswith(p):
			return True
	return False


def _judge_check(commitment_text: str, check_url: str, criteria: str) -> dict:
	"""Called only from within a non-deterministic block. Loads the page a
	covenant points at and asks a model to classify the commitment against
	it right now; returns a plain dict, touches no contract state."""
	try:
		content = gl.nondet.web.render(check_url, mode="text",
			wait_after_loaded=RENDER_WAIT_AFTER_LOADED)
	except Exception as e:
		raise gl.vm.UserError(ERR_TRANSIENT_FETCH + " " + str(e)[:150])

	if not isinstance(content, str):
		content = str(content)
	content_len = len(content)
	if content_len == 0:
		return {"verdict": VERDICT_INCONCLUSIVE, "confidence": 0,
			"note": "the checked page returned no readable content", "content_len": 0}
	if content_len > MAX_RENDER_CHARS:
		content = content[:MAX_RENDER_CHARS]

	prompt = (
		"You are an impartial auditor checking whether a provider is currently\n"
		"honoring one narrow, specific commitment about a public page's state.\n"
		"Judge only what the fetched page text actually shows right now; do not\n"
		"assume history, do not assume intent, and do not guess when the page\n"
		"genuinely does not settle the question either way.\n\n"
		"COMMITMENT (what must currently be true):\n" + commitment_text[:MAX_COMMITMENT_LEN] + "\n\n"
		"ADDITIONAL CHECKING CRITERIA (may be empty):\n" + criteria[:MAX_CRITERIA_LEN] + "\n\n"
		"FETCHED PAGE TEXT:\n" + content + "\n\n"
		"Respond with exactly one of: HONORED, BREACHED, INCONCLUSIVE.\n"
		"Use HONORED only if the page text clearly shows the commitment holds\n"
		"right now. Use BREACHED only if the page text clearly shows it does\n"
		"not hold right now. Use INCONCLUSIVE if the page is empty, broken,\n"
		"paywalled, unrelated, or does not clearly settle the question either\n"
		"way.\n\n"
		"Respond as JSON only, with exactly these keys:\n"
		"{\"verdict\": \"HONORED\" or \"BREACHED\" or \"INCONCLUSIVE\",\n"
		" \"confidence\": integer from 0 to 100,\n"
		" \"note\": short string, under 200 characters}"
	)
	try:
		raw = gl.nondet.exec_prompt(prompt, response_format="json")
	except Exception as e:
		raise gl.vm.UserError(ERR_TRANSIENT_LLM + " " + str(e)[:150])

	data = raw if isinstance(raw, dict) else _clean_json(str(raw))
	if not isinstance(data, dict):
		raise gl.vm.UserError(ERR_VERDICT_MALFORMED + " unparseable llm response")

	verdict = str(data.get("verdict", "")).strip().upper()
	if verdict not in ALLOWED_VERDICTS:
		raise gl.vm.UserError(ERR_VERDICT_MALFORMED
			+ " verdict missing or not one of HONORED/BREACHED/INCONCLUSIVE")

	try:
		confidence = _clamp(int(data.get("confidence", 0)), 0, 100)
	except Exception:
		raise gl.vm.UserError(ERR_VERDICT_MALFORMED + " confidence field missing or invalid")

	note = str(data.get("note", ""))
	if len(note) > MAX_NOTE_CHARS:
		note = note[:MAX_NOTE_CHARS]

	return {"verdict": verdict, "confidence": confidence, "note": note, "content_len": content_len}


def _coherent_verdict(obs) -> bool:
	"""Shape-checks a leader's proposed observation before a validator
	spends the effort of re-fetching anything itself. The set of valid
	verdicts never depends on the covenant, so this needs no context beyond
	the dict itself to decide whether it is even worth comparing against."""
	if not isinstance(obs, dict):
		return False
	verdict = obs.get("verdict")
	if not isinstance(verdict, str) or verdict not in ALLOWED_VERDICTS:
		return False
	confidence = obs.get("confidence")
	if not isinstance(confidence, int) or confidence < 0 or confidence > 100:
		return False
	note = obs.get("note")
	if not isinstance(note, str) or len(note) > MAX_NOTE_CHARS:
		return False
	return True


@allow_storage
@dataclass
class Covenant:
	covenant_id: u32
	creator: Address
	provider: Address
	commitment_text: str
	check_url: str
	criteria: str
	starts_epoch: u64
	ends_epoch: u64
	status: str
	honored_count: u32
	breached_count: u32
	inconclusive_count: u32
	attestation_count: u32
	last_attested_epoch: u64
	compliance_bps: u32
	finalized_epoch: u64


@allow_storage
@dataclass
class Attestation:
	covenant_id: u32
	verdict: str
	confidence: u32
	note: str
	attested_epoch: u64


class CovenantRegistry(gl.Contract):
	owner: Address
	paused: bool

	covenants: TreeMap[u32, Covenant]
	covenant_ids: DynArray[u32]
	creator_covenants: TreeMap[Address, DynArray[u32]]
	next_covenant_id: u32

	attestations: TreeMap[str, Attestation]
	covenant_attestation_keys: TreeMap[u32, DynArray[str]]

	count_covenants: u32
	count_finalized: u32
	count_voided: u32

	def __init__(self):
		self.owner = gl.message.sender_address
		self.paused = False
		self.next_covenant_id = u32(0)

	# ------------------------------------------------------------------ #
	# internal helpers
	# ------------------------------------------------------------------ #

	def _require_owner(self) -> None:
		if str(gl.message.sender_address) != str(self.owner):
			raise gl.vm.UserError("caller is not the owner")

	def _get(self, covenant_id: int) -> Covenant:
		covenant = self.covenants.get(u32(int(covenant_id)))
		if covenant is None:
			raise gl.vm.UserError("unknown covenant_id")
		return covenant

	def _adjudicate(self, commitment_text: str, check_url: str, criteria: str) -> dict:
		def leader_fn() -> dict:
			return _judge_check(commitment_text, check_url, criteria)

		def validator_fn(leaders_res: gl.vm.Result) -> bool:
			if not isinstance(leaders_res, gl.vm.Return):
				leader_msg = str(getattr(leaders_res, "message", leaders_res))
				try:
					leader_fn()
				except gl.vm.UserError as e:
					validator_msg = str(getattr(e, "message", e))
					if _starts_with_any(leader_msg, _TRANSIENT_PREFIXES) and \
							_starts_with_any(validator_msg, _TRANSIENT_PREFIXES):
						return True
					return False
				except Exception:
					return False
				return False

			theirs = leaders_res.calldata
			if not _coherent_verdict(theirs):
				return False
			mine = leader_fn()
			return str(mine.get("verdict")) == str(theirs.get("verdict"))

		return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

	# ------------------------------------------------------------------ #
	# covenant lifecycle
	# ------------------------------------------------------------------ #

	@gl.public.write
	def create_covenant(self, provider: str, commitment_text: str, check_url: str,
			criteria: str, starts_epoch: int, ends_epoch: int) -> str:
		if self.paused:
			raise gl.vm.UserError("CovenantRegistry is paused for new covenants")

		sender = gl.message.sender_address
		p = str(provider).strip()
		if len(p) == 0:
			raise gl.vm.UserError("provider address must not be empty")
		provider_addr = Address(p)

		text = str(commitment_text)
		if len(text) == 0 or len(text) > MAX_COMMITMENT_LEN:
			raise gl.vm.UserError("commitment_text must be 1.." + str(MAX_COMMITMENT_LEN) + " characters")

		url = str(check_url)
		if len(url) == 0 or len(url) > MAX_URL_LEN:
			raise gl.vm.UserError("check_url must be 1.." + str(MAX_URL_LEN) + " characters")

		c = str(criteria)
		if len(c) > MAX_CRITERIA_LEN:
			c = c[:MAX_CRITERIA_LEN]

		now = _now_epoch()
		starts = int(starts_epoch)
		ends = int(ends_epoch)
		if starts < now + MIN_LEAD_SECONDS:
			raise gl.vm.UserError("starts_epoch must be at least " + str(MIN_LEAD_SECONDS)
				+ "s in the future")
		if starts > now + MAX_LEAD_SECONDS:
			raise gl.vm.UserError("starts_epoch is too far in the future")

		window = ends - starts
		if window < MIN_WINDOW_SECONDS:
			raise gl.vm.UserError("the review window (ends_epoch - starts_epoch) must be at least "
				+ str(MIN_WINDOW_SECONDS) + "s")
		if window > MAX_WINDOW_SECONDS:
			raise gl.vm.UserError("the review window is too long")

		cid = int(self.next_covenant_id) + 1
		self.next_covenant_id = u32(cid)

		covenant = self.covenants.get_or_insert_default(u32(cid))
		covenant.covenant_id = u32(cid)
		covenant.creator = sender
		covenant.provider = provider_addr
		covenant.commitment_text = text
		covenant.check_url = url
		covenant.criteria = c
		covenant.starts_epoch = u64(starts)
		covenant.ends_epoch = u64(ends)
		covenant.status = STATUS_ACTIVE
		covenant.honored_count = u32(0)
		covenant.breached_count = u32(0)
		covenant.inconclusive_count = u32(0)
		covenant.attestation_count = u32(0)
		covenant.last_attested_epoch = u64(0)
		covenant.compliance_bps = u32(0)
		covenant.finalized_epoch = u64(0)

		self.covenant_ids.append(u32(cid))
		self.creator_covenants.get_or_insert_default(sender).append(u32(cid))
		self.count_covenants = u32(int(self.count_covenants) + 1)

		return json.dumps({"ok": True, "covenant_id": cid, "provider": str(provider_addr),
			"starts_epoch": starts, "ends_epoch": ends, "status": STATUS_ACTIVE})

	@gl.public.write
	def cancel_covenant(self, covenant_id: int) -> str:
		sender = gl.message.sender_address
		covenant = self._get(covenant_id)
		if str(covenant.creator) != str(sender):
			raise gl.vm.UserError("only the creator can cancel this covenant")
		if str(covenant.status) != STATUS_ACTIVE:
			raise gl.vm.UserError("covenant is " + str(covenant.status) + ", not ACTIVE")
		now = _now_epoch()
		if now >= int(covenant.starts_epoch):
			raise gl.vm.UserError("cannot cancel after the review window has started")

		covenant.status = STATUS_VOIDED
		covenant.finalized_epoch = u64(now)
		self.count_voided = u32(int(self.count_voided) + 1)
		return json.dumps({"ok": True, "covenant_id": int(covenant_id), "status": STATUS_VOIDED})

	@gl.public.write
	def attest(self, covenant_id: int) -> str:
		"""Open to anyone, any number of times, subject only to the gap and
		cap checks below. Since this method never carries value, a genuine
		fetch or model outage is allowed to simply revert the transaction
		-- there is nothing at stake but the caller's time, and they can
		try again once the underlying service recovers."""
		covenant = self._get(covenant_id)
		if str(covenant.status) != STATUS_ACTIVE:
			raise gl.vm.UserError("covenant is " + str(covenant.status) + ", not open for attestation")

		now = _now_epoch()
		if now < int(covenant.starts_epoch):
			raise gl.vm.UserError("the review window has not started yet")
		if now >= int(covenant.ends_epoch):
			raise gl.vm.UserError("the review window has ended; call finalize_covenant instead")

		count = int(covenant.attestation_count)
		if count > 0 and now < int(covenant.last_attested_epoch) + MIN_ATTESTATION_GAP_SECONDS:
			raise gl.vm.UserError("please wait at least " + str(MIN_ATTESTATION_GAP_SECONDS)
				+ "s between attestations of the same covenant")
		if count >= MAX_ATTESTATIONS_PER_COVENANT:
			raise gl.vm.UserError("this covenant has reached its attestation cap of "
				+ str(MAX_ATTESTATIONS_PER_COVENANT) + "; wait for the window to end and finalize it")

		result = self._adjudicate(str(covenant.commitment_text), str(covenant.check_url),
			str(covenant.criteria))
		verdict = str(result.get("verdict", VERDICT_INCONCLUSIVE))
		confidence = int(result.get("confidence", 0))
		note = str(result.get("note", ""))[:MAX_NOTE_CHARS]

		covenant.attestation_count = u32(count + 1)
		covenant.last_attested_epoch = u64(now)
		if verdict == VERDICT_HONORED:
			covenant.honored_count = u32(int(covenant.honored_count) + 1)
		elif verdict == VERDICT_BREACHED:
			covenant.breached_count = u32(int(covenant.breached_count) + 1)
		else:
			covenant.inconclusive_count = u32(int(covenant.inconclusive_count) + 1)

		akey = str(int(covenant_id)) + ":" + str(count)
		record = self.attestations.get_or_insert_default(akey)
		record.covenant_id = u32(int(covenant_id))
		record.verdict = verdict
		record.confidence = u32(confidence)
		record.note = note
		record.attested_epoch = u64(now)
		self.covenant_attestation_keys.get_or_insert_default(u32(int(covenant_id))).append(akey)

		return json.dumps({"ok": True, "covenant_id": int(covenant_id), "verdict": verdict,
			"confidence": confidence, "note": note, "attestation_count": count + 1})

	@gl.public.write
	def finalize_covenant(self, covenant_id: int) -> str:
		covenant = self._get(covenant_id)
		if str(covenant.status) != STATUS_ACTIVE:
			raise gl.vm.UserError("covenant is " + str(covenant.status) + ", not open for finalization")

		now = _now_epoch()
		if now < int(covenant.ends_epoch):
			raise gl.vm.UserError("cannot finalize before the covenant's review window ends")

		honored = int(covenant.honored_count)
		breached = int(covenant.breached_count)
		decisive = honored + breached

		if decisive == 0:
			covenant.status = STATUS_VOIDED
			covenant.finalized_epoch = u64(now)
			self.count_voided = u32(int(self.count_voided) + 1)
			return json.dumps({"ok": True, "covenant_id": int(covenant_id), "status": STATUS_VOIDED,
				"reason": "no decisive attestation was ever recorded for this covenant"})

		bps = (honored * 10000) // decisive
		covenant.status = STATUS_FINALIZED
		covenant.compliance_bps = u32(bps)
		covenant.finalized_epoch = u64(now)
		self.count_finalized = u32(int(self.count_finalized) + 1)

		return json.dumps({"ok": True, "covenant_id": int(covenant_id), "status": STATUS_FINALIZED,
			"compliance_bps": bps, "honored_count": honored, "breached_count": breached,
			"inconclusive_count": int(covenant.inconclusive_count)})

	@gl.public.write
	def preview_check(self, covenant_id: int) -> str:
		"""A dry run of the same judging step attest() would perform,
		callable at any time regardless of the window, so a creator can
		sanity-check their wording before ever opening the window to real
		attestations. Writes nothing and settles nothing; if the fetch or
		the model call fails here, that failure is folded into a
		non-committal RETRY_LATER answer rather than raised, since a
		preview has no business reverting anyone's transaction."""
		covenant = self._get(covenant_id)
		try:
			result = self._adjudicate(str(covenant.commitment_text), str(covenant.check_url),
				str(covenant.criteria))
		except gl.vm.UserError as e:
			return json.dumps({"ok": True, "covenant_id": int(covenant_id),
				"preview_verdict": "RETRY_LATER", "confidence": 0,
				"note": str(getattr(e, "message", e))[:MAX_NOTE_CHARS], "binding": False})

		return json.dumps({"ok": True, "covenant_id": int(covenant_id),
			"preview_verdict": str(result.get("verdict", VERDICT_INCONCLUSIVE)),
			"confidence": int(result.get("confidence", 0)),
			"note": str(result.get("note", ""))[:MAX_NOTE_CHARS], "binding": False})

	# ------------------------------------------------------------------ #
	# admin
	# ------------------------------------------------------------------ #

	@gl.public.write
	def set_paused(self, paused: bool) -> None:
		self._require_owner()
		self.paused = bool(paused)

	@gl.public.write
	def transfer_ownership(self, new_owner: str) -> None:
		self._require_owner()
		self.owner = Address(str(new_owner))

	# ------------------------------------------------------------------ #
	# views -- this is the surface CommitmentEscrow.py is built against
	# ------------------------------------------------------------------ #

	def _covenant_json(self, covenant: Covenant) -> dict:
		return {"covenant_id": int(covenant.covenant_id), "creator": str(covenant.creator),
			"provider": str(covenant.provider), "commitment_text": str(covenant.commitment_text),
			"check_url": str(covenant.check_url), "criteria": str(covenant.criteria),
			"starts_epoch": int(covenant.starts_epoch), "ends_epoch": int(covenant.ends_epoch),
			"status": str(covenant.status), "honored_count": int(covenant.honored_count),
			"breached_count": int(covenant.breached_count),
			"inconclusive_count": int(covenant.inconclusive_count),
			"attestation_count": int(covenant.attestation_count),
			"compliance_bps": int(covenant.compliance_bps),
			"finalized_epoch": int(covenant.finalized_epoch)}

	@gl.public.view
	def get_covenant(self, covenant_id: int) -> str:
		covenant = self.covenants.get(u32(int(covenant_id)))
		if covenant is None:
			return json.dumps({"found": False})
		out = self._covenant_json(covenant)
		out["found"] = True
		return json.dumps(out)

	@gl.public.view
	def get_covenant_status(self, covenant_id: int) -> str:
		covenant = self.covenants.get(u32(int(covenant_id)))
		if covenant is None:
			return "UNKNOWN"
		return str(covenant.status)

	@gl.public.view
	def get_starts_epoch(self, covenant_id: int) -> int:
		return int(self._get(covenant_id).starts_epoch)

	@gl.public.view
	def get_ends_epoch(self, covenant_id: int) -> int:
		return int(self._get(covenant_id).ends_epoch)

	@gl.public.view
	def get_provider(self, covenant_id: int) -> str:
		return str(self._get(covenant_id).provider)

	@gl.public.view
	def is_finalized(self, covenant_id: int) -> bool:
		covenant = self.covenants.get(u32(int(covenant_id)))
		if covenant is None:
			return False
		return str(covenant.status) == STATUS_FINALIZED

	@gl.public.view
	def get_compliance_bps(self, covenant_id: int) -> int:
		covenant = self._get(covenant_id)
		if str(covenant.status) != STATUS_FINALIZED:
			raise gl.vm.UserError("covenant is " + str(covenant.status) + ", not FINALIZED")
		return int(covenant.compliance_bps)

	@gl.public.view
	def get_recent_attestations(self, covenant_id: int, limit: int = MAX_RECENT) -> str:
		keys = self.covenant_attestation_keys.get(u32(int(covenant_id)))
		if keys is None:
			return json.dumps({"covenant_id": int(covenant_id), "total": 0, "attestations": []})
		lim = _clamp(int(limit), 1, MAX_RECENT)
		out = []
		i = len(keys) - 1
		while i >= 0 and len(out) < lim:
			record = self.attestations.get(keys[i])
			if record is not None:
				out.append({"verdict": str(record.verdict), "confidence": int(record.confidence),
					"note": str(record.note), "attested_epoch": int(record.attested_epoch)})
			i -= 1
		return json.dumps({"covenant_id": int(covenant_id), "total": len(keys), "attestations": out})

	@gl.public.view
	def get_covenants_by_creator(self, creator: str, offset: int = 0, limit: int = MAX_PAGE) -> str:
		ids = self.creator_covenants.get(Address(str(creator)))
		if ids is None:
			return json.dumps({"creator": str(creator), "total": 0, "covenants": []})
		off = max(0, int(offset))
		lim = _clamp(int(limit), 1, MAX_PAGE)
		out = []
		scanned = 0
		i = off
		while i < len(ids) and len(out) < lim and scanned < MAX_SCAN:
			covenant = self.covenants.get(ids[i])
			if covenant is not None:
				out.append(self._covenant_json(covenant))
			i += 1
			scanned += 1
		return json.dumps({"creator": str(creator), "total": len(ids), "covenants": out})

	@gl.public.view
	def get_platform_stats(self) -> str:
		return json.dumps({"total_covenants": int(self.count_covenants),
			"finalized": int(self.count_finalized), "voided": int(self.count_voided)})

	@gl.public.view
	def get_config(self) -> str:
		return json.dumps({"owner": str(self.owner), "paused": bool(self.paused),
			"min_window_seconds": MIN_WINDOW_SECONDS, "max_window_seconds": MAX_WINDOW_SECONDS,
			"min_attestation_gap_seconds": MIN_ATTESTATION_GAP_SECONDS,
			"max_attestations_per_covenant": MAX_ATTESTATIONS_PER_COVENANT,
			"min_lead_seconds": MIN_LEAD_SECONDS, "max_lead_seconds": MAX_LEAD_SECONDS})
