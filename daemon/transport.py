"""
Talking to the GK3.

Two channels, deliberately split:

  SMB  - moves the bytes. Copy a sliced file into the printer's mounted share
         and it appears in the printer's file list. Needs firmware >= 1.2.9.
         This is the reliable half.

  ChiTu UDP - moves the commands. Start a print, ask for status. Needs the
         printer answering on :3000 or :3030. This is the half that may or may
         not work on your firmware, which is what gk3_probe.py tells you.

Splitting them this way means we never have to implement ChiTu's chunked
M28/M29 file upload, which is the fiddliest and least documented part of the
protocol. SMB does that job better anyway.
"""

import os
import shutil
import socket
import time

CHITU_PORT = 3000
RECV_BUF = 8192


class TransportError(Exception):
    pass


class NotSupported(TransportError):
    """The printer does not expose this capability. Caller should degrade."""


# ---------------------------------------------------------------------------
# Bytes: SMB
# ---------------------------------------------------------------------------

class SmbStager:
    def __init__(self, mount_point):
        self.mount = mount_point

    def available(self):
        return os.path.isdir(self.mount)

    def require(self):
        if not self.available():
            raise TransportError(
                "SMB share not mounted at {}. In Finder: Cmd-K, "
                "smb://<printer-ip>, and save the password to the keychain "
                "so it remounts itself.".format(self.mount)
            )

    def staged_files(self):
        self.require()
        return set(os.listdir(self.mount))

    def free_bytes(self):
        self.require()
        st = os.statvfs(self.mount)
        return st.f_bavail * st.f_frsize

    def stage(self, local_path, remote_name=None):
        """Copy a sliced file onto the printer. Returns the name it landed as."""
        self.require()
        if not os.path.isfile(local_path):
            raise TransportError("no such file: {}".format(local_path))

        name = remote_name or os.path.basename(local_path)
        size = os.path.getsize(local_path)

        free = self.free_bytes()
        if size > free:
            raise TransportError(
                "not enough room on the printer: {} needs {:.0f} MB, "
                "{:.0f} MB free. Delete old files from the printer.".format(
                    name, size / 1e6, free / 1e6
                )
            )

        # Copy to a temp name, then rename, so the printer never sees a
        # half-written file in its list if the transfer dies partway. Over
        # WiFi a dropped SMB copy is a question of when, not if.
        tmp = os.path.join(self.mount, "." + name + ".part")
        final = os.path.join(self.mount, name)

        last_error = None
        for attempt in range(2):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)                  # leftover from a failed run
                shutil.copyfile(local_path, tmp)
                if os.path.getsize(tmp) != size:
                    raise TransportError("short write: share may have dropped")
                os.replace(tmp, final)
                return name
            except (OSError, TransportError) as exc:
                last_error = exc
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                if attempt == 0:
                    time.sleep(2.0)

        raise TransportError(
            "copying {} to the printer failed twice: {}".format(name, last_error)
        )

    def remove(self, remote_name):
        self.require()
        target = os.path.join(self.mount, remote_name)
        if os.path.exists(target):
            os.remove(target)


# ---------------------------------------------------------------------------
# Commands: ChiTu UDP
# ---------------------------------------------------------------------------

class ChituControl:
    """
    Minimal ChiTu command channel.

    Replies are parsed defensively: the exact field set varies across firmware
    builds, so we pull out every `KEY:value` pair we can find and always keep
    the raw string for debugging. Do not assume a field exists.
    """

    def __init__(self, ip, port=CHITU_PORT, timeout=3.0):
        self.ip = ip
        self.port = port
        self.timeout = timeout

    def _send(self, command, retries=2):
        """
        Send a command and wait for the reply.

        This is UDP with no delivery guarantee, over WiFi, to a printer whose
        radio is a USB dongle. A single dropped packet is normal and means
        nothing. Retry before believing the printer is gone.
        """
        for attempt in range(retries + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            try:
                sock.sendto(command.encode("ascii"), (self.ip, self.port))
                data, _ = sock.recvfrom(RECV_BUF)
                return data.decode("utf-8", "replace").strip()
            except (socket.timeout, OSError):
                pass
            finally:
                sock.close()
            if attempt < retries:
                time.sleep(0.6)

        raise NotSupported(
            "printer at {} did not answer {} on UDP {} after {} tries".format(
                self.ip, command, self.port, retries + 1
            )
        )

    @staticmethod
    def _parse(reply):
        """Pull KEY:value pairs out of a reply, keeping the raw text."""
        fields = {"raw": reply}
        for token in reply.replace(",", " ").split():
            if ":" in token:
                key, _, value = token.partition(":")
                if key:
                    fields[key.strip().upper()] = value.strip()
        return fields

    def available(self):
        try:
            self._send("M99999")
            return True
        except (NotSupported, OSError):
            return False

    def identify(self):
        return self._parse(self._send("M99999"))

    def status(self):
        """
        Current printer state. M4000 is the ChiTu status command; different
        builds answer with different field sets, so callers should treat every
        field as optional.
        """
        fields = self._parse(self._send("M4000"))

        # B:current/total is the usual layer-progress encoding. Guard it - a
        # firmware that reports something else must not crash the daemon.
        layer = total = None
        b = fields.get("B")
        if b and "/" in b:
            current, _, whole = b.partition("/")
            try:
                layer, total = int(current), int(whole)
            except ValueError:
                layer = total = None

        pct = None
        if layer is not None and total:
            pct = round(100.0 * layer / total, 2)

        return {
            "layer": layer,
            "total_layers": total,
            "progress_pct": pct,
            "raw": fields.get("raw", ""),
            "fields": fields,
        }

    def start(self, remote_name):
        """
        Begin printing a file already staged on the printer.

        M6030 takes the filename in single quotes with a leading colon-slash
        path. We do NOT verify success from the reply - replies vary too much
        to trust. The caller should poll status() and confirm the layer counter
        actually moves before calling the job started.
        """
        reply = self._send("M6030 ':{}'".format(remote_name))
        return self._parse(reply)

    def wait_until_printing(self, timeout=45.0, interval=3.0):
        """Confirm a start actually took, by watching for layer movement."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st = self.status()
                if st["layer"] is not None and st["layer"] > 0:
                    return True
            except (NotSupported, OSError):
                pass
            time.sleep(interval)
        return False


class NullControl:
    """
    Stand-in for when the printer has no reachable control channel.

    Everything degrades to manual: the daemon still stages files, and you press
    Print on the touchscreen and tap "I started it" on the phone page.
    """

    def available(self):
        return False

    def identify(self):
        raise NotSupported("no control channel")

    def status(self):
        raise NotSupported("no control channel")

    def start(self, remote_name):
        raise NotSupported("no control channel")

    def wait_until_printing(self, timeout=45.0, interval=3.0):
        return False


def build_control(ip, enabled=True):
    """Pick a control channel, falling back to manual if the printer is mute."""
    if not enabled:
        return NullControl()
    chitu = ChituControl(ip)
    return chitu if chitu.available() else NullControl()
