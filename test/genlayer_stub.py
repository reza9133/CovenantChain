"""
A minimal, dependency-free stand-in for the real `genlayer` GenVM SDK.

This is NOT a reimplementation of GenVM's consensus, gas, or storage
semantics. Its only job is to let the actual contract files in
`contracts/EventOracle.py`, `contracts/PredictionPool.py`, and
`contracts/ForecasterRank.py` be imported and run as plain Python, so the
tests in this directory exercise the real shipped logic rather than a
separate reimplementation of it.

It covers exactly what those three files use: Address, the sized int type
aliases, DynArray/TreeMap with zero-initialised storage semantics matching
the documented GenVM defaults, allow_storage, gl.Contract, gl.public.view /
write / write.payable, gl.message, gl.vm.UserError / Return / VMError /
run_nondet_unsafe, gl.nondet.web.render / exec_prompt, gl.contract_interface,
and gl.evm.contract_interface. On top of that it adds two small conveniences
this project's chained-contract tests need that a two-contract project does
not: `deploy_and_register`, for wiring a real, live instance of one contract
into another's typed IC-to-IC calls (rather than a hand-written fake), and
`set_balance`, for exercising an on-chain-balance-dependent code path.

Run with:  python3 -m unittest discover -s test -v
"""
import dataclasses
import sys
import types
import importlib.util


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class Address:
	def __init__(self, value=ZERO_ADDRESS):
		self._value = str(value)

	def __str__(self):
		return self._value

	def __repr__(self):
		return "Address(" + self._value + ")"

	def __eq__(self, other):
		return str(self) == str(other)

	def __hash__(self):
		return hash(str(self))


# Every sized integer type in the real SDK behaves like a plain Python int
# outside of storage assignment (see the SDK's Primitive Types reference) --
# range checking only happens when a value is written into a storage field,
# which this stub does not model. A single alias to the builtin is enough
# for the pure logic these contracts run.
_INT_ALIASES = (
	["u" + str(n) for n in (8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96,
		104, 112, 120, 128, 136, 144, 152, 160, 168, 176, 184, 192, 200,
		208, 216, 224, 232, 240, 248, 256)]
	+ ["i" + str(n) for n in (8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96,
		104, 112, 120, 128, 136, 144, 152, 160, 168, 176, 184, 192, 200,
		208, 216, 224, 232, 240, 248, 256)]
	+ ["bigint"]
)


class DynArray(list):
	def __class_getitem__(cls, item):
		return cls


class _ParamTreeMap(dict):
	_value_type = None

	def get(self, key, default=None):
		return dict.get(self, key, default)

	def get_or_insert_default(self, key):
		if key not in self:
			self[key] = _zero_value(self.__class__._value_type)
		return self[key]


class TreeMap(_ParamTreeMap):
	def __class_getitem__(cls, item):
		key_type, value_type = item
		return type("TreeMap", (_ParamTreeMap,), {"_key_type": key_type, "_value_type": value_type})


def allow_storage(cls):
	return cls


def _zero_value(tp):
	"""Mirrors the documented GenVM storage defaults (u*/i* -> 0, bool ->
	False, str -> "", DynArray -> [], TreeMap -> {}) plus recursive
	zero-construction of nested @allow_storage dataclasses, since real GenVM
	zero-initialises a freshly-touched TreeMap value the same way it
	zero-initialises a freshly-deployed contract's own fields."""
	if tp is None:
		return None
	if tp is bool:
		return False
	if tp is int:
		return 0
	if tp is str:
		return ""
	if tp is Address:
		return Address(ZERO_ADDRESS)
	if isinstance(tp, type) and issubclass(tp, _ParamTreeMap):
		return tp()
	if isinstance(tp, type) and issubclass(tp, DynArray):
		return tp()
	if dataclasses.is_dataclass(tp):
		kwargs = {}
		for f in dataclasses.fields(tp):
			kwargs[f.name] = _zero_value(f.type)
		return tp(**kwargs)
	return None


class UserError(Exception):
	def __init__(self, message=""):
		super().__init__(message)
		self.message = message


class VMError(Exception):
	pass


class Return:
	def __init__(self, calldata):
		self.calldata = calldata


def run_nondet_unsafe(leader_fn, validator_fn):
	"""Single-process stand-in: there is no second validator process here to
	independently disagree with, so this runs the leader once, then runs the
	*real* validator_fn against that result (which itself re-runs leader_fn
	internally, per the contract's own logic) and only accepts the outcome
	if the contract's own consensus check agrees with itself. A regression
	in `_coherent_outcome` or the compared-field logic would show up here as
	a failed assertion, not as silently-accepted output."""
	result = leader_fn()
	agreed = validator_fn(Return(result))
	if not agreed:
		raise AssertionError(
			"validator_fn rejected the leader's own result inside "
			"run_nondet_unsafe -- the contract's consensus check "
			"disagreed with itself in a single-process run")
	return result


message = types.SimpleNamespace(
	sender_address=Address(ZERO_ADDRESS),
	value=0,
	contract_address=Address(ZERO_ADDRESS),
	chain_id=0,
)


def _identity_decorator(fn):
	return fn


def _write(fn):
	return fn


_write.payable = _identity_decorator

public = types.SimpleNamespace(view=_identity_decorator, write=_write)

vm = types.SimpleNamespace(UserError=UserError, VMError=VMError, Return=Return,
	Result=object, run_nondet_unsafe=run_nondet_unsafe)

# Per-test-overridable hooks for the two non-deterministic primitives the
# contracts call. Tests replace these two functions to control exactly what
# a "fetch" and an "LLM call" return, without touching contract code.
NONDET_HOOKS = {
	"web_render": lambda url, mode: "",
	"exec_prompt": lambda prompt, response_format: "{}",
}


class _Web:
	def render(self, url, mode="text", **kwargs):
		# Extra keyword args (e.g. wait_after_loaded) are accepted for
		# signature compatibility with the real SDK and intentionally
		# ignored -- tests only need to control the returned content.
		return NONDET_HOOKS["web_render"](url, mode)


class _Nondet:
	def __init__(self):
		self.web = _Web()

	def exec_prompt(self, prompt, response_format=None):
		return NONDET_HOOKS["exec_prompt"](prompt, response_format)


nondet = _Nondet()

# Outbound value transfers recorded here instead of actually moving GEN.
# Each entry is (recipient_address_str, amount).
PAYMENTS = []


class _PayHandle:
	def __init__(self, address):
		self.address = address

	def emit_transfer(self, value=0, **kwargs):
		PAYMENTS.append((str(self.address), int(value)))

	def emit(self, *args, **kwargs):
		return self

	def __getattr__(self, name):
		return lambda *a, **kw: None


def _evm_contract_interface(cls):
	return _PayHandle


evm = types.SimpleNamespace(contract_interface=_evm_contract_interface)

# Registry a test (or deploy_and_register) populates to control what a typed
# IC-to-IC view() call returns, keyed by the string form of the target
# contract's address.
CONTRACT_REGISTRY = {}


class _NullContract:
	def view(self):
		return self

	def __getattr__(self, name):
		def _missing(*args, **kwargs):
			raise AssertionError("no mock registered for oracle method " + name
				+ " -- register one in CONTRACT_REGISTRY, or use deploy_and_register(), first")
		return _missing


def contract_interface(cls):
	def factory(address):
		return CONTRACT_REGISTRY.get(str(address), _NullContract())
	return factory


class Contract:
	"""Zero-initialises every annotated storage field before the real
	__init__ runs, matching GenVM's documented "storage starts
	zero-initialized" behaviour -- the contracts under test rely on this for
	their TreeMap/DynArray fields, which they never assign in __init__."""

	def __new__(cls, *args, **kwargs):
		instance = object.__new__(cls)
		object.__setattr__(instance, "_stub_balance", 0)
		seen = set()
		for klass in reversed(cls.__mro__):
			ann = klass.__dict__.get("__annotations__", {})
			for name, tp in ann.items():
				if name in seen:
					continue
				seen.add(name)
				try:
					object.__setattr__(instance, name, _zero_value(tp))
				except Exception:
					pass
		return instance

	@property
	def balance(self):
		return self._stub_balance


gl = types.SimpleNamespace(
	message=message,
	public=public,
	vm=vm,
	nondet=nondet,
	evm=evm,
	contract_interface=contract_interface,
	Contract=Contract,
)

_module = types.ModuleType("genlayer")
_module.gl = gl
_module.Address = Address
_module.DynArray = DynArray
_module.TreeMap = TreeMap
_module.allow_storage = allow_storage
for _name in _INT_ALIASES:
	setattr(_module, _name, int)


def install():
	"""Idempotent: point sys.modules['genlayer'] at the stub so that
	`from genlayer import *` inside the real contract files resolves
	against it."""
	sys.modules["genlayer"] = _module


def load_contract(path, module_name):
	"""Import a contract file from disk as a real Python module, against the
	stub SDK, and return the module object."""
	install()
	spec = importlib.util.spec_from_file_location(module_name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def set_balance(instance, amount: int) -> None:
	"""Directly sets an already-constructed contract instance's on-chain GEN
	balance, for exercising code paths that read `self.balance`."""
	object.__setattr__(instance, "_stub_balance", int(amount))


class _LiveContractProxy:
	"""What a real `@gl.contract_interface`-generated proxy provides in
	GenVM -- a `.view()` (and, if ever needed, `.emit()`) entry point in
	front of the target contract -- but pointed at an actual, live Python
	instance of another loaded contract instead of going over the wire."""

	def __init__(self, instance):
		self._instance = instance

	def view(self):
		return self._instance

	def emit(self, *args, **kwargs):
		return self._instance

	def __getattr__(self, name):
		return getattr(self._instance, name)


def deploy_and_register(contract_class, address, *args, **kwargs):
	"""Constructs a real instance of `contract_class` (running its actual
	__init__, under whatever `message.sender_address` is set at the time)
	and registers it in CONTRACT_REGISTRY under `address`, so that a
	*different* contract's typed `@gl.contract_interface` call into that
	address routes to this real, live instance instead of a hand-written
	fake. This is what lets test_chain_integration.py exercise the full
	EventOracle -> PredictionPool -> ForecasterRank chain the way it would
	actually run on-chain. Returns the raw instance (not the proxy), so the
	test driving it can call its methods directly too."""
	instance = contract_class(*args, **kwargs)
	CONTRACT_REGISTRY[str(address)] = _LiveContractProxy(instance)
	return instance


def reset():
	"""Clear the shared, module-level test doubles between tests."""
	PAYMENTS.clear()
	CONTRACT_REGISTRY.clear()
	NONDET_HOOKS["web_render"] = lambda url, mode: ""
	NONDET_HOOKS["exec_prompt"] = lambda prompt, response_format: "{}"
	message.sender_address = Address(ZERO_ADDRESS)
	message.value = 0
