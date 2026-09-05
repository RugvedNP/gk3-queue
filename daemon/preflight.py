"""
Pre-flight check on a sliced file, before it costs you eight hours and a vat.

The most common resin failure isn't mechanical - it's slicing with the wrong
resin profile, or for the wrong printer, and not finding out until the plate
comes up empty. Both are visible in the file header. This module reads it.

Two layers:

  1. A mechanical diff against a learned baseline. The first job you accept for
     a given resin becomes that resin's reference profile; every later job is
     compared against it. This works even on file formats we can't fully
     decode, because we diff the raw header words by offset and only overlay
     friendly names where we're sure of them.

  2. An optional plain-English verdict from Claude on top of that diff.

Preflight NEVER blocks a print on its own. It writes a status and notes to the
job row; the decision stays yours on the phone page.
"""

import json
import os
import struct

HEADER_BYTES = 256          # plenty for every slicer header we care about
FLOAT_EPSILON = 1e-4

# ChiTu container magics. .ctb v4+ and the encrypted variants have a different
# (and partly obfuscated) layout - those fall through to the raw-word diff,
# which still catches profile changes.
MAGIC_CBDDLP = 0x12FD0019
MAGIC_CTB = 0x12FD0086
MAGIC_CTB_V4 = 0x12FD0106

# Verified offsets for cbddlp / ctb v1-v3. Format: offset -> (name, kind)
CTB_FIELDS = {
    0x08: ("bed_x_mm", "f"),
    0x0C: ("bed_y_mm", "f"),
    0x10: ("bed_z_mm", "f"),
    0x1C: ("total_height_mm", "f"),
    0x20: ("layer_height_mm", "f"),
    0x24: ("exposure_s", "f"),
    0x28: ("bottom_exposure_s", "f"),
    0x2C: ("light_off_delay_s", "f"),
    0x30: ("bottom_layers", "I"),
    0x34: ("resolution_x", "I"),
    0x38: ("resolution_y", "I"),
}

# Fields where a mismatch means "this file is for a different machine".
IDENTITY_FIELDS = ("bed_x_mm", "bed_y_mm", "resolution_x", "resolution_y")

# Fields where a mismatch means "this is sliced for different resin".
CHEMISTRY_FIELDS = ("exposure_s", "bottom_exposure_s", "layer_height_mm",
                    "bottom_layers", "light_off_delay_s")

RELATIVE_TOLERANCE = 0.20   # 20% drift before we call it a real change


def _read_header(path):
    with open(path, "rb") as handle:
        return handle.read(HEADER_BYTES)


def _ascii_strings(blob, minimum=4):
    """Printable runs in the header - slicer name, printer name, profile name."""
    found, current = [], []
    for byte in blob:
        if 32 <= byte < 127:
            current.append(chr(byte))
        else:
            if len(current) >= minimum:
                found.append("".join(current))
            current = []
    if len(current) >= minimum:
        found.append("".join(current))
    return found


def parse(path):
    """
    Extract what we can from a sliced file.

    Always returns something usable. `named` is populated only for formats we
    decode confidently; `words` is always populated and is what makes the diff
    work on unknown formats.
    """
    blob = _read_header(path)
    result = {
        "file": os.path.basename(path),
        "bytes": os.path.getsize(path),
        "format": "unknown",
        "named": {},
        "words": {},
        "strings": _ascii_strings(blob)[:12],
    }

    if len(blob) < 64:
        result["format"] = "truncated"
        return result

    magic = struct.unpack_from("<I", blob, 0)[0]
    if magic in (MAGIC_CBDDLP, MAGIC_CTB):
        result["format"] = "cbddlp" if magic == MAGIC_CBDDLP else "ctb"
        result["named"]["version"] = struct.unpack_from("<I", blob, 4)[0]
        for offset, (name, kind) in CTB_FIELDS.items():
            if offset + 4 <= len(blob):
                result["named"][name] = struct.unpack_from("<" + kind, blob, offset)[0]
    elif magic == MAGIC_CTB_V4:
        # Layout differs from v1-v3; don't pretend we know the offsets.
        result["format"] = "ctb-v4"
    elif blob[:1] == b"V" and b"GOO" in blob[:64]:
        result["format"] = "goo"

    # Raw header words, always. This is what lets the diff work on ctb-v4,
    # .goo, and anything else UniFormation ships in a future slicer.
    for offset in range(0, min(len(blob), HEADER_BYTES) - 3, 4):
        as_int = struct.unpack_from("<I", blob, offset)[0]
        as_float = struct.unpack_from("<f", blob, offset)[0]
        # Only keep floats in a physically sensible range; the rest stay ints.
        sane = 1e-3 < abs(as_float) < 1e5
        result["words"][str(offset)] = {
            "i": as_int,
            "f": round(as_float, 4) if sane else None,
        }
    return result


def _changed(old, new):
    if old is None or new is None:
        return old != new
    if isinstance(old, float) or isinstance(new, float):
        scale = max(abs(old), abs(new), FLOAT_EPSILON)
        return abs(old - new) / scale > RELATIVE_TOLERANCE
    return old != new


def diff(baseline, current):
    """Compare a parsed file against a stored baseline profile."""
    changes = []

    for name in sorted(set(baseline.get("named", {})) | set(current.get("named", {}))):
        old = baseline.get("named", {}).get(name)
        new = current.get("named", {}).get(name)
        if _changed(old, new):
            changes.append({
                "field": name,
                "offset": None,
                "from": old,
                "to": new,
                "class": ("identity" if name in IDENTITY_FIELDS
                          else "chemistry" if name in CHEMISTRY_FIELDS
                          else "other"),
            })

    named_offsets = {str(o) for o in CTB_FIELDS}
    for offset in sorted(current.get("words", {}), key=int):
        if offset in named_offsets and current.get("named"):
            continue                                   # already reported by name
        old = baseline.get("words", {}).get(offset)
        new = current["words"][offset]
        if old is None:
            continue
        old_value = old["f"] if old["f"] is not None else old["i"]
        new_value = new["f"] if new["f"] is not None else new["i"]
        if _changed(old_value, new_value):
            changes.append({
                "field": "header+0x{:02X}".format(int(offset)),
                "offset": int(offset),
                "from": old_value,
                "to": new_value,
                "class": "unknown",
            })

    return changes


def mechanical_verdict(changes):
    """Rules that don't need a model. Returns (status, one-line summary)."""
    if not changes:
        return "ok", "Matches the stored profile."

    identity = [c for c in changes if c["class"] == "identity"]
    if identity:
        fields = ", ".join(c["field"] for c in identity)
        return "block", (
            "Machine geometry differs ({}). This file was almost certainly "
            "sliced for a different printer.".format(fields)
        )

    chemistry = [c for c in changes if c["class"] == "chemistry"]
    if chemistry:
        parts = ["{} {} -> {}".format(c["field"], c["from"], c["to"])
                 for c in chemistry]
        return "warn", "Exposure profile differs: " + "; ".join(parts)

    return "warn", "{} header field(s) differ from the stored profile.".format(
        len(changes)
    )


# ---------------------------------------------------------------------------
# Optional: a plain-English second opinion
# ---------------------------------------------------------------------------

def claude_verdict(current, baseline, changes, resin):
    """
    Ask Claude to read the diff and say what it means in a sentence or two.

    Returns None on any failure - a missing key, no network, a refusal. This is
    advisory garnish on top of the mechanical check, never a gate.
    """
    try:
        import anthropic
    except ImportError:
        return None

    payload = {
        "resin_in_vat": resin,
        "baseline_profile": baseline.get("named", {}),
        "this_file": current.get("named", {}),
        "file_format": current.get("format"),
        "header_strings": current.get("strings", []),
        "differences": changes,
    }

    system = (
        "You are checking a sliced resin 3D print file before it runs on a "
        "UniFormation GK3, an MSLA/LCD resin printer. You are given a parsed "
        "file header, a stored known-good baseline for the resin currently in "
        "the vat, and a computed diff.\n\n"
        "Answer in exactly this shape:\n"
        "VERDICT: OK|WARN|BLOCK\n"
        "then at most two sentences explaining what the operator should do.\n\n"
        "Guidance: exposure time changes of more than about 20 percent between "
        "resins are expected and normal - say so rather than alarming. Bed "
        "geometry or resolution changes mean the file is for a different "
        "machine and should be BLOCK. Fields labelled 'unknown' are raw header "
        "offsets we could not name; treat them as weak signals only. Be "
        "concrete and brief. Do not hedge."
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=4000,
            system=system,
            messages=[{
                "role": "user",
                "content": json.dumps(payload, indent=2, default=str),
            }],
        )
    except Exception:
        return None

    if getattr(response, "stop_reason", None) == "refusal":
        return None

    text = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    return text or None


# ---------------------------------------------------------------------------
# Baseline store
# ---------------------------------------------------------------------------

class Baselines:
    """Learned known-good profile per resin, kept in a small JSON file."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    self.data = json.load(handle)
            except (ValueError, OSError):
                self.data = {}

    def get(self, resin):
        return self.data.get(resin or "_default")

    def learn(self, resin, parsed):
        self.data[resin or "_default"] = parsed
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, default=str)
        os.replace(tmp, self.path)


def check(path, resin, baselines, use_claude=False):
    """
    Full preflight for one file.

    Returns (status, notes, parsed_params) where status is
    ok | warn | block | learned | skipped.
    """
    try:
        parsed = parse(path)
    except (OSError, struct.error) as exc:
        return "skipped", "Could not read header: {}".format(exc), {}

    baseline = baselines.get(resin)
    if baseline is None:
        baselines.learn(resin, parsed)
        return "learned", (
            "First job for resin '{}'. Stored as the reference profile; "
            "future jobs are compared against it.".format(resin or "default")
        ), parsed

    changes = diff(baseline, parsed)
    status, summary = mechanical_verdict(changes)

    if use_claude and changes:
        opinion = claude_verdict(parsed, baseline, changes, resin)
        if opinion:
            summary = summary + "\n\n" + opinion
            first = opinion.split("\n", 1)[0].upper()
            if "BLOCK" in first:
                status = "block"
            elif "WARN" in first and status == "ok":
                status = "warn"

    return status, summary, parsed
