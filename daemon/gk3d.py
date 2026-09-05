#!/usr/bin/env python3
"""
gk3d - the print queue daemon.

Runs on the always-on Mac. One loop, three jobs:

  1. Watch the inbox folder. New sliced file -> new queued job (+ preflight).
  2. Stage the next few queued jobs onto the printer over SMB, so there is
     never a transfer wait when you tap start.
  3. Drive the printer: poll status, and start the next job when - and only
     when - a human has confirmed the build plate is clear.

That last condition is the whole safety story. Resin printers cannot chain
prints; the plate has to be physically cleared between jobs. The daemon will
happily queue fifty jobs and will never start the second one on its own.

    python gk3d.py --config config.toml
    python gk3d.py --config config.toml --once    # single pass, for testing
"""

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import preflight
import transport

try:
    import tomllib
except ImportError:                                  # Python 3.10 and older
    import tomli as tomllib

from supabase import create_client

LOG = logging.getLogger("gk3d")

POLL_SECONDS = 5
SLICE_EXTENSIONS = (".ctb", ".cbddlp", ".goo", ".pwmx", ".pws", ".photon")
TERMINAL_STATES = ("done", "failed", "canceled")

# The printer talks over a USB WiFi dongle, so single dropped polls are normal.
# Nothing that matters is decided on one sample.
UNREACHABLE_AFTER = 3      # consecutive failed polls before we cry wolf
IDLE_CONFIRMATIONS = 3     # consecutive idle polls before calling a job done

_running = True


def _stop(signum, frame):
    global _running
    _running = False
    LOG.info("signal %s received, finishing current pass", signum)


def now():
    return datetime.now(timezone.utc).isoformat()


class Daemon:
    def __init__(self, config):
        self.cfg = config
        self.inbox = os.path.expanduser(config["watch"]["inbox"])
        self.stage_ahead = int(config["watch"].get("stage_ahead", 2))

        self.sb = create_client(
            config["supabase"]["url"], config["supabase"]["anon_key"]
        )
        self.stager = transport.SmbStager(config["printer"]["smb_mount"])
        self.control = transport.build_control(
            config["printer"]["ip"],
            enabled=bool(config["printer"].get("remote_start", True)),
        )
        self.auto = not isinstance(self.control, transport.NullControl)

        pf = config.get("preflight", {})
        self.preflight_on = bool(pf.get("enabled", True))
        self.use_claude = bool(pf.get("use_claude", False))
        baseline_path = pf.get("baselines", "baselines.json")
        if not os.path.isabs(baseline_path):
            baseline_path = os.path.join(os.path.dirname(__file__), baseline_path)
        self.baselines = preflight.Baselines(baseline_path)

        # Debounce counters - see UNREACHABLE_AFTER / IDLE_CONFIRMATIONS.
        self.poll_failures = 0
        self.idle_streak = 0

        os.makedirs(self.inbox, exist_ok=True)
        LOG.info(
            "control channel: %s",
            "ChiTu (auto start enabled)" if self.auto
            else "none - staging only, you press Print on the touchscreen",
        )

    # -- database helpers ---------------------------------------------------

    def jobs_in(self, states):
        return (
            self.sb.table("jobs")
            .select("*")
            .in_("state", list(states))
            .order("position")
            .execute()
            .data
        )

    def update_job(self, job_id, **fields):
        self.sb.table("jobs").update(fields).eq("id", job_id).execute()

    def known_paths(self):
        rows = self.sb.table("jobs").select("local_path").execute().data
        return {row["local_path"] for row in rows}

    def printer_row(self):
        rows = self.sb.table("printer").select("*").eq("id", 1).execute().data
        return rows[0] if rows else {}

    def update_printer(self, **fields):
        fields["updated_at"] = now()
        fields["daemon_seen_at"] = now()
        self.sb.table("printer").update(fields).eq("id", 1).execute()

    # -- 1. intake ----------------------------------------------------------

    def scan_inbox(self):
        seen = self.known_paths()
        try:
            entries = sorted(os.listdir(self.inbox))
        except OSError as exc:
            LOG.warning("cannot read inbox: %s", exc)
            return

        for entry in entries:
            path = os.path.join(self.inbox, entry)
            if path in seen or not os.path.isfile(path):
                continue
            if not entry.lower().endswith(SLICE_EXTENSIONS):
                continue

            # Skip files still being written by the slicer: require the size to
            # hold steady across two reads.
            first = os.path.getsize(path)
            time.sleep(1.0)
            if os.path.getsize(path) != first:
                LOG.info("%s still being written, will pick it up next pass", entry)
                continue

            resin = self.infer_resin(entry)
            row = {
                "name": os.path.splitext(entry)[0],
                "local_path": path,
                "file_bytes": first,
                "resin": resin,
                "position": time.time(),
                "state": "queued",
            }

            if self.preflight_on:
                status, notes, parsed = preflight.check(
                    path, resin, self.baselines, self.use_claude
                )
                row["preflight_status"] = status
                row["preflight_notes"] = notes
                row["slice_params"] = parsed.get("named") or {}
                LOG.info("preflight %s: %s", entry, status)

            self.sb.table("jobs").insert(row).execute()
            LOG.info("queued %s (%.0f MB)", entry, first / 1e6)

    @staticmethod
    def infer_resin(filename):
        """
        Resin name from the filename, by convention: put it after a double
        underscore, e.g. "dragon-bust__ABSLike.ctb" -> "ABSLike". Rename the
        file or edit the job on the phone page if you slice differently.
        """
        stem = os.path.splitext(filename)[0]
        if "__" in stem:
            return stem.rsplit("__", 1)[1]
        return None

    # -- 2. staging ---------------------------------------------------------

    def stage_next(self):
        if not self.stager.available():
            LOG.warning("SMB share not mounted; skipping staging this pass")
            return

        already = len(self.jobs_in(["staged"]))
        budget = max(0, self.stage_ahead - already)
        if budget == 0:
            return

        for job in self.jobs_in(["queued"])[:budget]:
            if job.get("preflight_status") == "block":
                LOG.info("not staging %s - preflight says block", job["name"])
                continue
            try:
                name = self.stager.stage(job["local_path"])
                self.update_job(
                    job["id"],
                    state="staged",
                    staged_at=now(),
                    printer_filename=name,
                    error=None,
                )
                LOG.info("staged %s -> printer", name)
            except transport.TransportError as exc:
                self.update_job(job["id"], state="failed", error=str(exc))
                LOG.error("staging %s failed: %s", job["name"], exc)

    # -- 3. driving the printer --------------------------------------------

    def poll_printer(self):
        if not self.auto:
            self.update_printer(state="manual")
            return None
        try:
            status = self.control.status()
        except transport.TransportError as exc:
            self.poll_failures += 1
            LOG.warning("status poll failed (%d in a row): %s",
                        self.poll_failures, exc)
            if self.poll_failures >= UNREACHABLE_AFTER:
                self.update_printer(state="unreachable")
            else:
                self.update_printer()                  # heartbeat only, no state flap
            return None

        self.poll_failures = 0

        # A reply we could not parse means "unknown", NOT "finished". Over WiFi
        # a truncated datagram is routine, and treating it as completion would
        # close out a job that is still running.
        if status["layer"] is None:
            LOG.debug("unparseable status reply: %s", status["raw"][:120])
            self.update_printer(raw_status=status["raw"][:500])
            return None

        busy = status["layer"] > 0
        self.idle_streak = 0 if busy else self.idle_streak + 1

        self.update_printer(
            state="printing" if busy else "idle",
            layer=status["layer"],
            total_layers=status["total_layers"],
            progress_pct=status["progress_pct"],
            raw_status=status["raw"][:500],
        )
        return status

    def reconcile(self, status):
        """
        Close out a job the printer has finished.

        Two ways to be sure, and neither of them is "one poll looked quiet":

          - the layer counter reached the total, which is definitive; or
          - the printer reported idle on several consecutive polls, after
            having been printing.

        A failed or unparseable poll arrives here as status=None and decides
        nothing at all.
        """
        running = self.jobs_in(["printing"])
        if not running or status is None:
            return
        job = running[0]

        reached_end = bool(
            status["total_layers"] and status["layer"] >= status["total_layers"]
        )
        sustained_idle = status["layer"] == 0 and self.idle_streak >= IDLE_CONFIRMATIONS

        if not (reached_end or sustained_idle):
            return

        LOG.info("job %s complete (%s)", job["name"],
                 "reached final layer" if reached_end
                 else "printer idle for {} polls".format(self.idle_streak))

        self.update_job(job["id"], state="done", finished_at=now())
        # Plate now has a part on it. Lock the gate again.
        self.update_printer(state="idle", plate_clear=False, current_job_id=None)
        LOG.info("job %s finished; plate gate re-locked", job["name"])

    def maybe_start_next(self, status):
        if not self.auto or status is None:
            return
        if self.jobs_in(["printing"]):
            return
        if status["layer"] > 0:
            return                                    # printer already busy

        printer = self.printer_row()
        if not printer.get("plate_clear"):
            return                                    # the safety interlock

        staged = self.jobs_in(["staged"])
        if not staged:
            return
        job = staged[0]

        LOG.info("starting %s", job["printer_filename"])
        try:
            self.control.start(job["printer_filename"])
        except transport.TransportError as exc:
            self.update_job(job["id"], state="failed", error=str(exc))
            LOG.error("start failed: %s", exc)
            return

        if self.control.wait_until_printing():
            self.idle_streak = 0        # a fresh job must earn its own idle run
            self.update_job(job["id"], state="printing", started_at=now())
            self.update_printer(
                state="printing", current_job_id=job["id"], plate_clear=False
            )
            LOG.info("%s is printing", job["name"])
        else:
            self.update_job(
                job["id"],
                error="Start command sent but the layer counter never moved. "
                      "Check the printer screen.",
            )
            LOG.warning("start of %s not confirmed", job["name"])

    # -- loop ---------------------------------------------------------------

    def pass_once(self):
        self.scan_inbox()
        self.stage_next()
        status = self.poll_printer()
        self.reconcile(status)
        self.maybe_start_next(status)
        self.update_printer()                          # heartbeat

    def run(self):
        LOG.info("gk3d up; watching %s", self.inbox)
        while _running:
            try:
                self.pass_once()
            except Exception:
                # A daemon that dies on one bad pass is worse than useless.
                LOG.exception("pass failed; continuing")
            for _ in range(POLL_SECONDS):
                if not _running:
                    break
                time.sleep(1)
        LOG.info("gk3d stopped")


def main():
    parser = argparse.ArgumentParser(description="GK3 print queue daemon")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--once", action="store_true",
                        help="run a single pass and exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    config_path = args.config
    if not os.path.isabs(config_path):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   config_path)
    if not os.path.exists(config_path):
        LOG.error("no config at %s - copy config.example.toml and fill it in",
                  config_path)
        return 1

    with open(config_path, "rb") as handle:
        config = tomllib.load(handle)

    daemon = Daemon(config)
    if args.once:
        daemon.pass_once()
        return 0
    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
