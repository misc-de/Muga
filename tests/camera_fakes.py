"""Shared GStreamer double for the camera tests.

camera.py reaches GStreamer exclusively through ``self._Gst``, so a fake
module is enough to drive the pipeline builders headlessly. Elements are
recording mocks, so tests can assert on the properties and links that were
actually requested; the FakeGst constructor makes individual elements
unavailable or unlinkable to exercise the failure ladders.
"""

from __future__ import annotations

from types import SimpleNamespace


class FakePad:
    def __init__(self, name: str, link_ok: bool = True) -> None:
        self.name = name
        self.link_ok = link_ok
        self.linked_to = None
        self.probes: list = []
        self.removed_probes: list = []

    def add_probe(self, probe_type, handler):
        self.probes.append((probe_type, handler))
        return len(self.probes)

    def remove_probe(self, probe_id):
        self.removed_probes.append(probe_id)

    def link(self, other):
        self.linked_to = other
        return FakeGst.PadLinkReturn.OK if self.link_ok else FakeGst.PadLinkReturn.REFUSED


class FakeStateChange(str):
    """Gst.StateChangeReturn is an enum: hashable (camera.py uses it as a dict
    key) and it carries value_nick. A plain SimpleNamespace is neither."""

    def __new__(cls, value):
        return super().__new__(cls, value)

    @property
    def value_nick(self):
        return str(self).lower()


class FakeCaps(str):
    """A caps string that also answers to_string(), like Gst.Caps."""

    def __new__(cls, text):
        return super().__new__(cls, f"caps:{text}")

    def to_string(self):
        return str(self)


class FakeFactory:
    def __init__(self, name: str) -> None:
        self._name = name

    def get_name(self):
        return self._name


class FakeElement:
    def __init__(self, factory: str, name: str, *, link_ok=True, pads=None,
                 preset_props=None) -> None:
        self.factory = factory
        self.name = name
        self.props: dict = dict(preset_props or {})
        self.links: list = []
        self.signals: list = []
        self.handlers: dict = {}
        self.disconnected: list = []
        self._link_ok = link_ok
        self._pads = pads if pads is not None else {"src": FakePad("src"), "sink": FakePad("sink")}

    def set_property(self, key, value):
        self.props[key] = value

    def get_property(self, key):
        return self.props.get(key)

    def link(self, other):
        self.links.append(other.name)
        return self._link_ok

    def get_static_pad(self, name):
        return self._pads.get(name)

    def request_pad_simple(self, name):
        return self._pads.get(f"request:{name}")

    def get_pad_template_list(self):
        return []

    def connect(self, signal, handler):
        self.signals.append(signal)
        self.handlers[len(self.signals)] = handler
        return len(self.signals)

    def disconnect(self, signal_id):
        self.disconnected.append(signal_id)

    def get_name(self):
        return self.name

    def get_factory(self):
        return FakeFactory(self.factory)


class FakePipeline(FakeElement):
    def __init__(self, name: str) -> None:
        super().__init__("pipeline", name)
        self.added: list[FakeElement] = []
        self.states: list = []
        self.state_waits: list = []
        self.bus = FakeElement("bus", "bus")
        self.bus.watch_added = 0
        self.bus.watch_removed = 0

        def _add():
            self.bus.watch_added += 1

        def _remove():
            self.bus.watch_removed += 1

        self.bus.add_signal_watch = _add
        self.bus.remove_signal_watch = _remove

    def add(self, element):
        self.added.append(element)

    def get_bus(self):
        return self.bus

    def set_state(self, state):
        self.states.append(state)
        return FakeStateChange(FakeGst.StateChangeReturn.SUCCESS)

    def get_by_name(self, name):
        return next((e for e in self.added if e.name == name), None)

    def get_state(self, timeout):
        self.state_waits.append(timeout)
        return (FakeGst.StateChangeReturn.SUCCESS, self.states[-1] if self.states else None, None)

    def element_names(self):
        return [e.name for e in self.added]

    def factories(self):
        return [e.factory for e in self.added]


class FakeGst:
    """Stands in for gi.repository.Gst."""

    class State:
        NULL, READY, PAUSED, PLAYING = "NULL", "READY", "PAUSED", "PLAYING"

    class StateChangeReturn:
        FAILURE, SUCCESS, ASYNC, NO_PREROLL = "FAILURE", "SUCCESS", "ASYNC", "NO_PREROLL"

    class PadProbeType:
        BUFFER = "BUFFER"

    class PadProbeReturn:
        OK, REMOVE = "OK", "REMOVE"

    class MapFlags:
        READ = "READ"

    SECOND = 1_000_000_000

    class PadLinkReturn:
        OK, REFUSED = "OK", "REFUSED"

    class FlowReturn:
        OK, ERROR = "FLOW_OK", "FLOW_ERROR"

    def __init__(self, *, missing=(), absent_factories=(), link_failures=(),
                 element_pads=None, preset_props=None) -> None:
        self.missing = set(missing)                    # make() returns None
        self.absent = set(absent_factories) | set(missing)   # find() returns None
        self.link_failures = set(link_failures)
        self.element_pads = element_pads or {}
        self.preset_props = preset_props or {}
        self.parse_error: Exception | None = None
        self.parsed_descriptions: list[str] = []
        self.made: list[FakeElement] = []
        self.pipelines: list[FakePipeline] = []
        outer = self

        class _Pipeline:
            @staticmethod
            def new(name):
                p = FakePipeline(name)
                outer.pipelines.append(p)
                return p

        class _ElementFactory:
            @staticmethod
            def make(factory, name=None):
                if factory in outer.missing:
                    return None
                el = FakeElement(
                    factory, name or factory,
                    link_ok=factory not in outer.link_failures,
                    pads=outer.element_pads.get(factory),
                    preset_props=outer.preset_props.get(factory),
                )
                outer.made.append(el)
                return el

            @staticmethod
            def find(factory):
                return None if factory in outer.absent else object()

        class _Caps:
            @staticmethod
            def from_string(text):
                return FakeCaps(text)

        class _ParsedBin(FakeElement):
            pass

        def _parse_bin(description, ghost):
            if outer.parse_error is not None:
                raise outer.parse_error
            outer.parsed_descriptions.append(description)
            bin_ = _ParsedBin("bin", "downstream")
            outer.made.append(bin_)
            return bin_

        self.Pipeline = _Pipeline
        self.ElementFactory = _ElementFactory
        self.Caps = _Caps
        self.parse_bin_from_description = _parse_bin

    def made_factories(self):
        return [e.factory for e in self.made]

    def element(self, factory):
        return next((e for e in self.made if e.factory == factory), None)


def gst_win(gst: "FakeGst", **attrs) -> SimpleNamespace:
    """A stand-in ``self`` wired to *gst*, with a pass-through translator."""
    attrs.setdefault("_", lambda s: s)
    attrs["_Gst"] = gst
    return SimpleNamespace(**attrs)


def bind(win: SimpleNamespace, cls, *names) -> SimpleNamespace:
    """Bind real methods of *cls* onto a SimpleNamespace ``self``.

    Used where the method under test dispatches to siblings and the
    dispatching itself is what the test is about.
    """
    for name in names:
        setattr(win, name, getattr(cls, name).__get__(win, type(win)))
    return win
