"""Media-section ids (``a=mid``) as the Connect viewer expects them.

``webrtcbin`` names the offered media section ``video0`` (``a=mid:video0`` and
``a=group:BUNDLE video0``). Prusa Connect hands every ICE candidate of the camera
to the browser with ``sdpMid`` ``"0"`` (the section's index; the camera's own
candidate messages carry ``mid=str(mline_index)``). Chrome rejects a candidate
whose ``sdpMid`` matches no media section with ``OperationError: Error processing
ICE candidate``, so the browser never learned a single camera candidate, every
connectivity check failed and the stream never started.

So the offer leaves the camera with section ids ``"0"``, ``"1"``, ... and the
viewer's answer is mapped back before it reaches ``webrtcbin``, which keeps its
own names. Pure stdlib, so it is host-testable (``webrtc.py`` imports ``gi``).
"""
import re

_MID_LINE = re.compile(r'(?m)^(a=mid:)(\S+)')
_GROUP_LINE = re.compile(r'(?m)^(a=group:\S+)((?: \S+)+)')


def mids(sdp_text):
    """Return the ``a=mid`` values of ``sdp_text`` in section order."""
    return [m.group(2) for m in _MID_LINE.finditer(sdp_text or '')]


def _remap(sdp_text, mapping):
    """Rewrite ``a=mid`` lines and the ids listed in ``a=group`` lines."""
    if not mapping:
        return sdp_text
    text = _MID_LINE.sub(
        lambda m: m.group(1) + mapping.get(m.group(2), m.group(2)), sdp_text)

    def group(m):
        ids = ' '.join(mapping.get(i, i) for i in m.group(2).split())
        return f'{m.group(1)} {ids}'

    return _GROUP_LINE.sub(group, text)


def to_wire(sdp_text):
    """Number the section ids by position; return ``(sdp_text, mapping)``.

    ``mapping`` maps each changed internal id to its wire id and is empty when
    the ids already are ``"0"``, ``"1"``, ... (nothing is rewritten then).
    """
    found = mids(sdp_text)
    mapping = {mid: str(index) for index, mid in enumerate(found) if mid != str(index)}
    return _remap(sdp_text, mapping), mapping


def from_wire(sdp_text, mapping):
    """Undo :func:`to_wire` on the viewer's answer (``mapping`` is its result)."""
    return _remap(sdp_text, {wire: internal for internal, wire in (mapping or {}).items()})
