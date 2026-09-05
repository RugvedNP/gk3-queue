# GK3 print queue

Queue prints for a UniFormation GK3 from your phone. No USB stick walking.

The printer joins your LAN through its USB WiFi dongle. A small daemon on the
always-on Mac copies sliced files to the printer over SMB and starts them. A
Supabase-backed page on your phone shows the queue and holds the start button.

**Sliced files never leave your LAN.** Supabase stores queue metadata only —
names, order, state, progress. The bytes go straight from the Mac to the
printer. That keeps you far inside the free tier and makes staging fast.

## The one constraint that shapes all of this

Resin printers cannot chain prints. The build plate has to be physically
cleared between jobs. So this is **not** an unattended print farm — it's a
queue that pre-stages everything and gives you a one-tap start once you've
cleared the plate. The daemon will hold fifty jobs and never start the second
one on its own.

---

## Setup

### 0. Get the printer on the network

- Update firmware to **≥ 1.2.9**. That's the release with SMB folder sharing;
  1.2.4 added ChiTu Manager support. Firmware links are on UniFormation's
  [GK3 Ultra guide page](https://uniformation3d.com/pages/gk3-guide).
- Plug in the USB WiFi dongle and join your network. **Use the dongle that came
  in the box.** UniFormation publishes no compatible-chipset list, and the
  printer only carries drivers for a few specific radios, so a generic dongle
  is a coin flip. If you need one, ask support@uniformation3d.com first.
- If it won't connect, try **both the front and rear USB ports** — UniFormation's
  own FAQ lists port-specific failure as a known cause, and the dongle's
  indicator light tells you whether the port powered it at all.
- The dongle is 2.4 GHz. Make sure the network it joins is the **same subnet**
  your Mac is on — not a guest network or an IoT VLAN — and that client/AP
  isolation is off.
- The LAN IP appears under **Settings → File Sharing**. Give it a DHCP
  reservation in your router so it never moves.

Wired Ethernet is also supported on the GK3 line and is more reliable, but the
whole stack works fine over WiFi — the daemon is built to expect dropped
packets (see below). Nothing in the code cares how the printer reached the
network; it only wants the IP.

### 1. Find out what the printer actually exposes

```bash
python gk3_probe.py <printer-ip>
```

This decides which of two modes you get:

| Probe finds | You get |
|---|---|
| Port 3030 open, or a reply to `M99999` on 3000 | **Auto mode.** Daemon stages *and* starts prints. |
| Only port 445 (SMB) | **Manual mode.** Daemon stages; you press Print on the touchscreen and tap a button on the phone page. |

Manual mode is still most of the value — the transfer wait disappears and you
keep a real queue. Don't skip the build if the probe comes back SMB-only.

### 2. Supabase

Create a project, then paste [`supabase/schema.sql`](supabase/schema.sql) into
the SQL Editor and run it. Copy the project URL and the **anon** key from
Project Settings → API.

> The schema gives the anon key full access to the two tables. That's fine for
> a printer on your home LAN, but anyone holding the key can queue prints —
> don't commit it to a public repo. Swap `using (true)` for
> `using (auth.uid() is not null)` if you ever want real accounts.

### 3. The daemon

```bash
cd daemon
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml   # then fill it in
```

Mount the printer's share once in Finder (`Cmd-K`, `smb://<printer-ip>`) and
tick **Remember this password in my keychain** so it remounts itself. Confirm
the mount path matches `smb_mount` in the config.

Test a single pass before installing it as a service:

```bash
.venv/bin/python gk3d.py --config config.toml --once --verbose
```

Then install the launchd agent — edit the paths in
[`com.gk3.queue.plist`](daemon/com.gk3.queue.plist) first:

```bash
cp com.gk3.queue.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.gk3.queue.plist
```

**Sleep matters here.** The plist wraps the daemon in `caffeinate -is`, which
stops idle sleep — but it does *not* override lid-close sleep. If that MacBook
runs with the lid shut, keep it on power with an external display attached.
Otherwise the queue stalls whenever it naps, and the phone page will show the
daemon as offline rather than silently lying to you.

### 4. The phone page

Fill in `SUPABASE_URL` and `SUPABASE_ANON_KEY` at the top of the `<script>` in
[`web/index.html`](web/index.html), then drag the `web` folder onto
[Cloudflare Pages](https://pages.cloudflare.com) or Netlify Drop. Add it to
your home screen.

---

## Daily use

1. Slice, save into the inbox folder. Name it `thing__ResinName.ctb` so
   preflight knows which resin profile to compare against.
2. It appears in the queue within seconds, already preflight-checked.
3. The daemon copies the next couple of jobs to the printer in the background.
4. Clear the plate, tap **Plate is clear — arm next job**. It starts.

## Preflight

Wrong-resin-profile is the most common cause of a failed resin print, and it's
invisible until the plate comes up empty eight hours later. The header says so
in advance, so `preflight.py` reads it.

The first job you queue for a given resin becomes that resin's baseline. Every
later job is diffed against it. Bed geometry or resolution changes mean the
file was sliced for a *different printer* — that's a hard block. Exposure and
layer-height changes are flagged as warnings.

The diff works on file formats it can't fully decode: `.ctb`/`.cbddlp` v1–v3
get proper field names, and everything else falls back to comparing raw header
words by offset, which still catches profile changes. Set
`preflight.use_claude = true` to get a plain-English verdict on top of the
mechanical diff. Preflight never blocks a print by itself — it writes a status
to the job and the call stays yours.

Delete an entry from `baselines.json` to re-learn a resin.

## Running over WiFi

The printer's radio is a USB dongle and the control protocol is UDP, so dropped
packets are routine rather than exceptional. The daemon assumes this:

- **Commands retry.** Three attempts before the printer is declared mute.
- **No state flapping.** Three consecutive failed polls before the phone page
  says "unreachable" — a single miss changes nothing.
- **A garbled reply means unknown, never finished.** This one matters: a
  truncated status datagram used to look identical to "print complete," which
  would close out a job that was still running and re-lock the plate gate
  underneath it. Completion now requires either the layer counter reaching the
  total, or three consecutive confirmed-idle polls.
- **Interrupted copies can't leave junk on the printer.** Files stage to a
  hidden `.part` name, get size-checked, and are renamed into place only on
  success. A dropped SMB copy retries once, then fails the job loudly.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Phone page says "daemon not responding" | Mac asleep, or gk3d stopped. `tail -f /tmp/gk3d.log`. |
| "SMB share not mounted" | Finder unmounted it. Remount with `Cmd-K`; save the password to the keychain. |
| Jobs stage but never start | Manual mode — the probe found no control channel. Use the touchscreen and the phone button. |
| Start sent, nothing happens | The daemon watches the layer counter and won't claim a job started if it never moves. Check the printer screen. |
| Probe finds nothing | Wrong subnet (guest WiFi?), or firmware below 1.2.9. |

## What's verified and what isn't

Verified: firmware 1.2.9 adds SMB sharing and 1.2.4 adds ChiTu Manager; the
dongle puts the printer's IP in the File Sharing menu; the ChiTu protocol uses
`M99999` discovery and `M6030` to start a print on UDP 3000. The Python here
compiles clean, the probe runs end to end, preflight was exercised against
synthetic slice files (learns a baseline, passes an identical file, warns on a
changed exposure profile, blocks a file sliced for another machine), and the
WiFi debounce logic was tested against dropped packets, garbled replies, and a
mid-print blip.

Not verified: whether *your* GK3's firmware answers the control channel. That
is exactly what step 1 tells you, and both outcomes have a working path. The
`M4000` status field layout also varies between firmware builds, so
`transport.py` parses it defensively and always keeps the raw reply — if auto
mode connects but progress looks wrong, that raw string is in the `raw_status`
column and is the thing to read.

## Sources

- [UniFormation GK3 Ultra guide & firmware](https://uniformation3d.com/pages/gk3-guide)
- [UniFormation printing FAQ](https://uniformation3d.com/pages/printing-faq)
- [cassini — ChiTu/ELEGOO network client](https://github.com/vvuk/cassini)
- [ChiTu WiFi protocol notes](https://github.com/Photonsters/anycubic-photon-docs/blob/master/photon-blueprints/ChituClientWifiProtocol-translated.txt)
- [ChituManager / SDCP](https://docs.chitubox.com/en-US/chitu-manager/latest/introduction)
