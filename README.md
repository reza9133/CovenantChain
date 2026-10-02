# CovenantChain

CovenantChain is a set of three GenLayer Intelligent Contracts, deployed one
after another, where each contract is constructed with the on-chain address
of the one deployed just before it. There is no factory and nothing spawns
child contracts at runtime -- every link is a separate `genlayer deploy`
call, and the previous deployment's printed address is passed straight into
the next constructor.

The chain models a small, self-contained "reliability economy" for standing
public commitments -- the kind of promise a hosting provider, an API, or a
supplier might make about a status page staying in a particular state for
as long as a subscription or an order is open:

```
CovenantRegistry     (deployed first, stands alone)
      |  its finished address is passed into the next constructor
      v
CommitmentEscrow     (a pay-for-performance escrow built on CovenantRegistry)
      |  its finished address is passed into the next constructor
      v
ProviderStanding     (an expiring reliability rating built on CommitmentEscrow)
```

Each contract only ever talks to the one immediately below it in this list,
through a small, typed `@gl.contract_interface` `view()` surface -- the same
IC-to-IC pattern GenLayer's own docs describe for
[interacting with other Intelligent Contracts](https://docs.genlayer.com/developers/intelligent-contracts/features/interacting-with-intelligent-contracts).
`ProviderStanding` never calls `CovenantRegistry` directly and has no idea
what a "check URL" or an "attestation" even is -- it only sees whatever
`CommitmentEscrow` chooses to expose about a provider's settled history.

## What each link actually does

### 1. `contracts/CovenantRegistry.py` -- judging

Anyone can call `create_covenant(provider, commitment_text, check_url,
criteria, starts_epoch, ends_epoch)` to register a standing promise: a
short description of what must remain true, a public page expected to show
it, and a review window with a start and an end.

While that window is open, **any address** may call `attest(covenant_id)`,
as often as once every `MIN_ATTESTATION_GAP_SECONDS` and up to
`MAX_ATTESTATIONS_PER_COVENANT` times in total. Each call independently
loads the page and asks an LLM to classify the current moment as one of
exactly three verdicts -- `HONORED`, `BREACHED`, or `INCONCLUSIVE` -- using
GenLayer's [Equivalence Principle](https://docs.genlayer.com/developers/intelligent-contracts/equivalence-principle):
a leader proposes a verdict, and validators independently re-run the same
check and accept the round only if their own verdict matches.

Once the window has closed, anyone may call `finalize_covenant(covenant_id)`.
This is where the design differs most from a plain yes/no resolver: instead
of picking one label, it turns the whole history of individual verdicts into
a single graded score,

```
compliance_bps = honored_count * 10000 / (honored_count + breached_count)
```

with `INCONCLUSIVE` calls excluded from both sides of that ratio. A
covenant that never received a single decisive attestation is marked
`VOIDED` instead of finalized at 0%, since there is nothing there to
actually judge. A creator may also `cancel_covenant()` before the window
starts. `preview_check()` runs the exact same page-and-LLM check without
writing anything or ever raising, so a creator can sanity-check their
wording before opening the window to real attestations.

### 2. `contracts/CommitmentEscrow.py` -- money

`CommitmentEscrow` is where GEN actually moves. Anyone may call
`fund(covenant_id)` -- the one `@gl.public.write.payable` method in the
whole chain -- to add GEN to a covenant's pool at any time before its
review window ends. This is a pooled subscription behind the provider's
performance, not a bet against other backers: everyone who funds the same
covenant shares one pool.

Once `CovenantRegistry` has finalized the covenant, anyone may call
`settle_fund(covenant_id)`. This is a single, constant-cost bookkeeping step
that never loops over backers, however many there are. It reads the
covenant's `compliance_bps` and splits the pool along it:

```
gross_provider = total_funded * compliance_bps / 10000
fee            = gross_provider * platform_fee_bps / 10000
provider_net   = gross_provider - fee
refund_pool    = total_funded - gross_provider
```

The platform fee only ever comes out of what the provider actually earned --
a provider judged 0% compliant costs the platform nothing, and every backer
gets a full refund. The provider then calls `claim_provider_share()` once,
and each backer calls `claim_refund()` for their own proportional slice of
`refund_pool`; both are pull payments the recipient triggers themselves.

If `CovenantRegistry` instead marks the covenant `VOIDED` (an early
cancellation, or no decisive attestation ever recorded), the fund is voided
too and every backer reclaims their exact contribution. If a covenant is
simply never finalized, `settle_fund()` will void the fund on its own once
`FINALIZATION_GRACE_SECONDS` has passed its end time, so GEN can never be
trapped indefinitely behind a resolution that never happens.

Because `fund()` is payable, it is written to **never raise**: every
rejection path pays the sender's GEN straight back and returns
`{"ok": false, ...}` instead of reverting. That has to hold even when the
problem originates in `CovenantRegistry` -- an unreachable address, a
reverted view call, a registry repointed somewhere unexpected -- so every
outbound registry read inside `fund()`'s validation path is wrapped in a
helper that turns any exception into an ordinary rejection string first.

### 3. `contracts/ProviderStanding.py` -- reputation

`ProviderStanding` reads exactly two numbers about a provider from
`CommitmentEscrow` -- how many of their funds have settled, and their
average compliance score across those settlements -- and maps them onto a
short ladder of named tiers (`VERIFIED` / `RELIABLE` / `TRUSTED` / `ELITE`
by default, each configurable by the owner).

Eligibility and certification are kept deliberately separate:

* `is_currently_eligible(provider, tier)` always recomputes the answer live
  from `CommitmentEscrow`'s current numbers. It can never be faked, and it
  can never be taken away by anyone -- including this contract's own owner.
* `certify(tier)` is what a provider actually calls to write a dated record
  -- "eligibility was confirmed on this date" -- valid for
  `CERT_VALIDITY_SECONDS` (30 days by default). `is_certified(provider,
  tier)` checks only that stored record, not live numbers, so a certificate
  quietly stops counting once it expires; renewing one is just calling
  `certify()` again. The owner may also `revoke_certificate()` a specific
  record for administrative reasons, which has no effect whatsoever on live
  eligibility.

## Trust and administrative power

Every contract has an `owner` (the deploying address, transferable via
`transfer_ownership()`) and a `paused` switch that only blocks *new*
covenants, new funding, or new certificates -- it never blocks settling an
already-open fund or claiming money already owed. The owner can:

* pause/unpause each contract independently (`set_paused`);
* adjust `CommitmentEscrow`'s platform fee, within a hard-coded ceiling
  (`set_platform_fee_bps`, capped at `MAX_PLATFORM_FEE_BPS`);
* repoint `CommitmentEscrow` at a different registry, or `ProviderStanding`
  at a different escrow (`set_registry` / `set_escrow`) -- a fund's provider
  address is snapshotted once when the fund first opens, so a repoint can
  never retroactively change who an existing fund is owed to;
* sweep accrued platform fees out of `CommitmentEscrow`, but only the
  portion that is actually free (`self.balance` minus everything still
  earmarked for backers and providers), and only after a short cooldown
  since the last payout (`withdraw_platform_fees`, `SWEEP_DELAY_SECONDS`).

The owner can never touch a covenant's verdicts, a fund's settlement math,
or anyone's pull payment directly -- those are derived entirely from
attestations and view-method reads, not from an admin flag.

## Repository layout

```
CovenantChain/
  contracts/
    CovenantRegistry.py    # link 1: judges standing commitments
    CommitmentEscrow.py    # link 2: pools and pays out GEN
    ProviderStanding.py    # link 3: expiring reliability tiers
  test/
    genlayer_stub.py            # a small offline stand-in for the GenVM SDK
    test_covenant_registry.py   # unit tests against a real CovenantRegistry
    test_commitment_escrow.py   # unit tests against a hand-written fake registry
    test_provider_standing.py   # unit tests against a hand-written fake escrow
    test_chain_integration.py   # end-to-end test wiring all three real contracts
  README.md
```

## Testing

The contracts are exercised entirely offline, without Docker or a live
network, using `test/genlayer_stub.py` -- a small shim that stands in for
the parts of the GenVM SDK these contracts touch (`gl.public.*` decorators,
`TreeMap` / `DynArray` storage collections, `Address`, the payable-value
path, `gl.vm.run_nondet_unsafe`, and `gl.nondet.web.render` /
`gl.nondet.exec_prompt` as swappable hooks). It does not attempt to model
consensus, appeals, or fees; it exists purely to catch logic mistakes before
a single GEN is ever spent deploying.

```bash
cd CovenantChain/test
python3 -m unittest discover -s . -v
```

This runs 44 tests covering, among other things: window and gap validation
on `attest()`, the honored/breached compliance math on `finalize_covenant()`,
a `VOIDED` covenant when no decisive attestation was ever recorded,
`fund()` refunding instead of raising on every rejection path (including
when the registry itself misbehaves), the exact worked-example split
between a provider's net payout and each backer's proportional refund, the
grace-period void path in `settle_fund()`, and a certificate that is issued,
expires, and is successfully renewed. `test_chain_integration.py` repeats a
full happy path, a void path, and a certificate-expiry path against three
real deployed instances of the actual contracts, not fakes.

If you have the [GenLayer Skills](https://skills.genlayer.com/) plugin or a
local Python 3.12+ toolchain, you can additionally run the official linter
against each file before deploying:

```bash
genvm-lint check contracts/CovenantRegistry.py
genvm-lint check contracts/CommitmentEscrow.py
genvm-lint check contracts/ProviderStanding.py
```

## Deploying to Studionet

These contracts target [GenLayer Studio](https://studio.genlayer.com)'s
hosted **Studionet** network (chain ID `61999`). Using the
[GenLayer CLI](https://docs.genlayer.com/api-references/genlayer-cli):

```bash
genlayer network set studionet

# 1. CovenantRegistry takes no constructor arguments
genlayer deploy --contract contracts/CovenantRegistry.py
# -> deployed at REGISTRY=0x27800939A707d72C20603cEAc32e9d7703f0071a

# 2. CommitmentEscrow needs CovenantRegistry's address, plus an optional
#    platform fee in basis points (defaults to 250 = 2.5% if omitted)
genlayer deploy --contract contracts/CommitmentEscrow.py --args 0x27800939A707d72C20603cEAc32e9d7703f0071a 250
# -> deployed at ESCROW=0xEE07C95C2EAe46E62B4b395004EA7701E14a13D6

# 3. ProviderStanding needs CommitmentEscrow's address
genlayer deploy --contract contracts/ProviderStanding.py --args 0xEE07C95C2EAe46E62B4b395004EA7701E14a13D6
# -> deployed at STANDING=0xe7A46E44D8ea4C45Ed2A3BC070AA1182065a7085
```

### Trying it end to end

```bash
# register a covenant; starts_epoch must be at least 60s in the future and
# the window (ends_epoch - starts_epoch) must be at least 600s
genlayer write 0x27800939A707d72C20603cEAc32e9d7703f0071a create_covenant \
  --args 0xPROVIDER... "the status page reads operational" \
  "https://example.org/status" "use the official status page only" \
  1780000000 1780050000

# fund it -- note the --value flag, since fund() is the one payable
# method in this whole chain
genlayer write 0xEE07C95C2EAe46E62B4b395004EA7701E14a13D6 fund --args 1 --value 5gen

# once the review window opens, anyone can attest as often as the gap
# and cap constants allow
genlayer write 0x27800939A707d72C20603cEAc32e9d7703f0071a attest --args 1

# once the window ends, anyone can finalize it...
genlayer write 0x27800939A707d72C20603cEAc32e9d7703f0071a finalize_covenant --args 1
# ...and then settle the fund built on top of it
genlayer write 0xEE07C95C2EAe46E62B4b395004EA7701E14a13D6 settle_fund --args 1

# the provider and every backer can now pull their own share
genlayer write 0xEE07C95C2EAe46E62B4b395004EA7701E14a13D6 claim_provider_share --args 1
genlayer write 0xEE07C95C2EAe46E62B4b395004EA7701E14a13D6 claim_refund --args 1

# check whether that settlement is enough for a rating on the third contract
genlayer call 0xe7A46E44D8ea4C45Ed2A3BC070AA1182065a7085 is_currently_eligible \
  --args 0xPROVIDER... VERIFIED
```

## Why an AI resolver actually fits here

GenLayer's own guidance on when to reach for an Intelligent Contract calls
out cases with "a real on-chain consequence," an outcome that "requires
judgment," evidence "validators can independently check," and a decision
that "can be made explicit" as strong fits -- see
[When to Use GenLayer](https://docs.genlayer.com/developers/intelligent-contracts/when-to-use-genlayer).
Whether a status page currently reads "operational" is exactly that kind of
question: it is not something a deterministic contract can parse reliably
(pages are messy, differently worded, sometimes rendered client-side), it
has real money riding on the answer, and every validator can fetch the same
public URL and judge it independently. Sampling that judgment repeatedly
across a window, rather than trusting one snapshot, is what lets the escrow
pay out in proportion to how well a promise actually held up -- rather than
forcing every outcome into a single win-or-lose bet.
