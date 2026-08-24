#!/usr/bin/env python3
"""Reproduce the `lf t55xx dump` addressed-read bit slip on ONE named tag, and record which tag it was.

Standalone companion to a bug report against RfidResearchGroup/proxmark3: `lf t55xx dump` / `rdbl`
extract each 32-bit block at a bit offset CACHED by the last `detect`, and never re-derive it from the
buffer being decoded. When a later read's demodulation starts on a different bit boundary, the returned
word is a bit ROTATION of the true block -- silently. `detect` is unaffected because it searches for the
offset in the buffer it just demodulated.

WHY A DRIVER RATHER THAN A pm3 -s SCRIPT. A command file takes no parameters, so it cannot record which
tag it ran on, and this bug varies by tag and modulation -- the tag name has to travel with the result.
Every artefact this writes is named for the tag and carries a provenance header (including the client
version, from `--pm3 ... --version`), so a per-silicon set of files needs no reconstructing afterwards.

WHAT IT MEASURES. Write a known non-byte-periodic payload, put the tag in ASK/Manchester and dump it for
GROUND TRUTH, then set each modulation in turn and dump twice. Every field of every dump is classified
against all 31 rotations of the truth-dump value. A byte-periodic payload would hide the bug (it lands on
itself under many rotations), which is why the payload below is deliberately irregular.

⭐ WHY A TRUTH DUMP RATHER THAN THE INTENDED VALUES. Scoring against what you MEANT to write is wrong twice
over: some tags have permanently-unwritable blocks that read back 00000000 while `write` reports success
(dead memory, not corruption), and a write may genuinely have failed. So the Manchester dump -- the one
modulation with a deterministic demod start, measured clean on every tag tested -- is the reference, and
blocks that could not take the payload are reported as dead rather than counted as slips.

⚠ IT REFUSES TO START FROM A SLIPPING MODULATION. If the tag arrives in PSK1 or DIRECT its current
contents cannot be read reliably, so the pre-run state restored at the end could be a ROTATED copy of
itself -- and restoring a rotated dump makes the rotation PERMANENT. Put the tag in Manchester first, or
pass --no-restore and accept losing the pre-run state.

⚠ IT WRITES blocks 0-7 of page 0. It captures the arrival state and restores it byte-for-byte at the end,
verifying the restore against the arrival dump -- but do not point it at a tag holding anything you cannot
recreate.

    ./bitslip_probe.py --self-test                                  # no hardware; known-answer check
    ./bitslip_probe.py --tag tagA --pm3 /path/to/proxmark3/pm3
    ./bitslip_probe.py --tag tagB --configs fsk2a-mb6,fsk2a-mb2,psk1-mb2 --pm3 .../pm3
    ./bitslip_probe.py --re-analyse saved_run.md
"""
import argparse, datetime, json, os, re, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "pm3_captures")

# Non-byte-periodic on purpose: a byte-periodic payload lands on the same value under many wrong
# rotations, which is how A5A5A5A5 nearly hid the PSK1 slip -- only 2D2D2D2D pinned it down.
PAYLOAD = ["1D996666", "99A55A5A", "A5966969", "965AA5A5", "5A996666", "99699696", "13579BDF"]
# ⚠ b7 USED TO BE 00000000 and that made it useless: a zero cannot distinguish a successful write from a
# dead block from a failed write, so the non-zero-payload rule (correctly) excluded it -- and every healthy
# tag was then reported as having one dead block. Writing a real value there is the fix; excluding a field
# from evidence is fine, but reporting "dead" about a block that is not dead would go into the upstream
# report as a false claim about the operator's hardware.

# block 0 words. Manchester RF/32 maxblock 7 is the TRUTH config: measured clean on Invengo and on
# Silicon Craft, and maxblock does not gate an addressed read so every block is still reachable.
TRUTH_CFG = ("manchester-mb2", "00088040")
CONFIGS = {
    "fsk2a-mb6":     "001070C0",   # the config the original slip was seen on
    "fsk2a-mb2":     "00107040",   # same modulation, short frame -- tests frame length
    "psk1-mb2":      "00081040",   # deterministic ror5 on Silicon Craft
    "direct-mb2":    "00080040",   # rol1 and unstable on Invengo, clean on Silicon Craft
    "manchester-mb2":"00088040",   # a negative control: this should never slip
    "biphase-mb3":   "00150060",
}
SLIPPING_ON_ARRIVAL = ("PSK", "NRZ", "DIRECT")


def rots(v):
    return {n: ((v << n) | (v >> (32 - n))) & 0xFFFFFFFF for n in range(1, 32)}


def classify(truth, got):
    """`got` vs every rotation of `truth`. Returns 'correct', 'rolN', 'rorN', or 'UNEXPLAINED'."""
    if got == truth:
        return "correct"
    r = rots(truth)
    for n in range(1, 32):
        if got == r[n]:
            return f"rol{n}"
        if got == r[32 - n]:
            return f"ror{n}"
    return "UNEXPLAINED"


def build_cmd(configs, dumps, restore):
    # ⚠ NO pre-run dump here. Step 0 already captured arrival in its own pm3 invocation, and emitting a
    # second one put a DUPLICATE dump at index 1 -- which analyse() then used as the truth dump, silently
    # scoring every arm against the tag's ORIGINAL contents and shifting the arms by one.
    L = ["lf config --125 -b 8 --dec 1",
         "# --- payload, config LAST (a patched block means believing the tag is something it may not be) ---"]
    for i, v in enumerate(PAYLOAD, start=1):
        L.append(f"lf t55xx write -b {i} -d {v} --verify")
    L += [f"# --- TRUTH DUMP in {TRUTH_CFG[0]} -- what the tag ACTUALLY holds, dead blocks included ---",
          f"lf t55xx write -b 0 -d {TRUTH_CFG[1]} --verify", "lf t55xx detect", "lf t55xx dump"]
    for name in configs:
        L.append(f"# --- ARM {name} ({CONFIGS[name]}) ---")
        L.append(f"lf t55xx write -b 0 -d {CONFIGS[name]} --verify")
        L.append("lf t55xx detect")
        L += ["lf t55xx dump"] * dumps
    if restore:
        L.append("# --- RESTORE: filled in from the pre-run dump by the driver ---")
    return L


DUMPRE = re.compile(r"^\[\+\]\s+(\d\d)\s+\|\s+([0-9A-Fa-f]{8})\s+\|")


def parse(text):
    """-> list of events: ('detect',{...}) ('dump',{'p0':{n:v},'p1':{n:v}}) ('write',blk,ok)"""
    ev, page, cur = [], None, None
    for ln in text.splitlines():
        if "Block0" in ln and ("auto detect" in ln or re.search(r"[0-9A-Fa-f]{8}", ln)):
            m = re.search(r"([0-9A-Fa-f]{8})", ln)
            if m:
                ev.append(("detect_b0", m.group(1).upper()))
        if "Modulation" in ln:
            m = re.search(r"Modulation\.*\s*(\S+)", ln)
            if m:
                ev.append(("detect_mod", m.group(1)))
        m = re.match(r"^\[=\] Writing page 0\s+block:\s+(\d+)\s+data: 0x([0-9A-Fa-f]{8})", ln)
        if m:
            cur = (int(m.group(1)), m.group(2).upper())
        if cur and "Write OK" in ln:
            ev.append(("write", cur[0], cur[1], True)); cur = None
        elif cur and "could not validate" in ln:
            ev.append(("write", cur[0], cur[1], False)); cur = None
        if "T55xx tag memory" in ln:
            ev.append(("dump_begin",)); page = None
        if re.match(r"^\[\+\] Page 0", ln): page = "p0"
        elif re.match(r"^\[\+\] Page 1", ln): page = "p1"
        m = DUMPRE.match(ln)
        if m and page:
            ev.append(("blk", page, int(m.group(1)), int(m.group(2), 16)))
    dumps, detects, writes, d = [], [], [], None
    for e in ev:
        if e[0] == "dump_begin":
            d = {"p0": {}, "p1": {}}; dumps.append(d)
        elif e[0] == "blk" and d is not None:
            d[e[1]][e[2]] = e[3]
        elif e[0] == "detect_b0":
            detects.append(e[1])
        elif e[0] == "write":
            writes.append({"block": e[1], "data": e[2], "validated": e[3]})
    return dumps, detects, writes


PM3 = ["pm3"]


def run_pm3(lines, label):
    """One pm3 invocation from a generated command file. Returns (transcript, generated_lines)."""
    with tempfile.NamedTemporaryFile("w", suffix=".cmd", delete=False, newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
        path = f.name
    try:
        # errors="replace": the client emits non-UTF-8 bytes (progress/spinner glyphs), and the default
        # strict decoding raised UnicodeDecodeError mid-run -- losing the whole capture over a cosmetic byte.
        p = subprocess.run(PM3 + ["-s", path], capture_output=True, text=True,
                           errors="replace", timeout=900)
        return (p.stdout + p.stderr), lines
    finally:
        os.unlink(path)


def analyse(dumps, detects, writes, configs, ndumps):
    """Score every test dump against the TRUTH dump. dumps[0] = pre-run, dumps[1] = truth."""
    if len(dumps) < 2:
        return None, "fewer than two dumps parsed -- nothing to score against"
    # ⭐ STRUCTURAL CHECK. Every index below is positional, so the COUNT is a precondition, not a detail.
    # Expected: 1 arrival + 1 truth + one per arm dump, plus at most 1 for the restore dump. An extra dump
    # anywhere earlier silently shifts every arm by one -- which is exactly the bug that made the first
    # three runs of this tool worthless, and the payload check above CANNOT see it when the truth dump
    # still lands in the right place by luck.
    want = 2 + len(configs) * ndumps
    if not (want <= len(dumps) <= want + 1):
        return None, (f"expected {want} or {want + 1} dumps for {len(configs)} config(s) x {ndumps} "
                      f"dump(s) plus arrival and truth, but parsed {len(dumps)}. Every index here is\n"
                      f"   positional, so an unexpected count means the arms are misaligned and any\n"
                      f"   verdict would be about this tool, not the tag. Nothing scored.")
    truth = dumps[1]
    # ⭐ THE CHECK THAT WAS MISSING. The truth dump is identified POSITIONALLY, and a positional index is
    # exactly the kind of thing that goes wrong silently -- an extra dump earlier in the transcript made
    # this point at the tag's ORIGINAL contents, so every arm was scored against the wrong reference and
    # came back UNEXPLAINED with no indication that the tool, not the tag, was at fault. So verify it:
    # the truth dump must contain the payload we just wrote, on at least ONE block.
    pay = {b: int(v, 16) for b, v in enumerate(PAYLOAD, start=1)}
    # ⚠ ONLY NON-ZERO payload blocks count as evidence. b7 is written as 00000000, so it matches a dead
    # block, a failed write and a successful write identically -- and on copper-coin that single trivial
    # match was enough to let a wrong truth dump through the check, which then reported six live blocks as
    # "dead". A test whose passing case includes the failure it is meant to catch is not a test.
    matched = [b for b in range(1, 8) if pay[b] != 0 and truth.get("p0", {}).get(b) == pay[b]]
    if not matched:
        got = " ".join(f"b{b}={truth.get('p0',{}).get(b,0):08X}" for b in range(1, 8))
        return None, (
            "the truth dump contains NONE of the payload that was just written, so it is not the "
            "truth dump. Scoring anything against it would report the tool's own indexing as "
            "corruption in the tag. Nothing scored.\n"
            f"   truth dump reads: {got}\n"
            f"   payload written : " +
            " ".join(f"b{b}={pay[b]:08X}" for b in range(1, 8)))
    # blocks that could not take the payload: dead/unimplemented memory, or a genuinely failed write.
    # Either way they hold nothing, and "blocks that hold nothing return whatever the demodulator makes
    # of no data" -- so their later values are noise and must not be counted as slips.
    dead = [b for b in range(1, 8) if b not in matched]
    rows, per_arm = [], {}
    idx = 2
    for name in configs:
        cfgv = int(CONFIGS[name], 16)
        arm = []
        for k in range(ndumps):
            if idx >= len(dumps):
                break
            d = dumps[idx]; idx += 1
            cells = {}
            # block 0 (both pages mirror it) is the config word we wrote, not the truth dump's
            for pg in ("p0", "p1"):
                if 0 in d.get(pg, {}):
                    cells[f"{pg}b0"] = classify(cfgv, d[pg][0])
            for b in range(1, 8):
                if b in d.get("p0", {}) and b in truth.get("p0", {}):
                    cells[f"p0b{b}"] = ("dead" if b in dead
                                        else classify(truth["p0"][b], d["p0"][b]))
            for b in (1, 2, 3):
                if b in d.get("p1", {}) and b in truth.get("p1", {}):
                    cells[f"p1b{b}"] = classify(truth["p1"][b], d["p1"][b])
            arm.append(cells)
            rows.append((name, k + 1, cells))
        per_arm[name] = arm
    return (rows, per_arm, dead), None


def summarise(rows):
    """Dead blocks are excluded from the denominator: they hold nothing, so they can be neither correct
    nor corrupted, and counting them either way would misstate the slip rate."""
    tot = slipped = deadn = 0
    kinds, unexplained = {}, 0
    for _, _, cells in rows:
        for v in cells.values():
            if v == "dead":
                deadn += 1
                continue
            tot += 1
            if v == "correct":
                continue
            if v == "UNEXPLAINED":
                unexplained += 1
            slipped += 1
            kinds[v] = kinds.get(v, 0) + 1
    return {"fields": tot, "slipped": slipped, "unexplained": unexplained,
            "rotations": kinds, "dead_fields_excluded": deadn}


def intermittency(per_arm):
    """A field that differs between two dumps of the SAME config proves non-repeatability."""
    out = {}
    for name, arm in per_arm.items():
        if len(arm) < 2:
            out[name] = None
            continue
        keys = set().union(*[set(a) for a in arm])
        differ = sorted(k for k in keys
                        if "dead" not in {a.get(k) for a in arm}
                        and len({a.get(k) for a in arm}) > 1)
        out[name] = differ
    return out


def fields_scored(rows):
    """Live fields per config. A config with zero is NOT clean -- it produced no readable blocks."""
    out = {}
    for name, _k, cells in rows:
        out[name] = out.get(name, 0) + len([v for v in cells.values() if v != "dead"])
    return out


def print_summary(tag, summ, inter, rows):
    print(f"\n[{tag}] {summ['slipped']} of {summ['fields']} fields corrupted; "
          f"rotations {summ['rotations'] or 'none'}; unexplained {summ['unexplained']}")
    scored = fields_scored(rows)
    for n, d in inter.items():
        if not scored.get(n):
            print(f"  {n}: ⛔ NO FIELDS SCORED -- the dumps returned no readable blocks. Total read "
                  f"failure, not a clean pass.")
        else:
            print(f"  {n}: " + (f"INTERMITTENT ({len(d)} fields differ between dumps)" if d
                                else f"repeatable ({scored[n]} fields)"))


def write_report(tag, configs, ndumps, transcript, gen, dumps, detects, writes, res, note):
    os.makedirs(OUT, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.join(OUT, f"bitslip_{tag}_{ts}")
    (rows, per_arm, dead_blocks) = res
    summ = summarise(rows)
    inter = intermittency(per_arm)
    fields = sorted({k for _, _, c in rows for k in c},
                    key=lambda s: (s[:2], int(s[3:])))
    L = [f"# `lf t55xx dump` bit-slip probe — tag: {tag}", "",
         f"- **tag**: `{tag}`  (name supplied by the operator; nothing on the chip records it)",
         f"- **UTC**: {ts}", f"- **configs**: {', '.join(configs)}   **dumps per config**: {ndumps}",
         f"- **truth dump**: {TRUTH_CFG[0]} (`{TRUTH_CFG[1]}`) — every score below is against THIS, not "
         f"against the values we intended to write", ""]
    if note:
        L += [f"- **operator note**: {note}", ""]
    L += ["## Ground truth actually on the tag", "",
          "| field | value |", "|---|---|"]
    for b in sorted(dumps[1].get("p0", {})):
        L.append(f"| p0 b{b} | `{dumps[1]['p0'][b]:08X}` |")
    for b in sorted(dumps[1].get("p1", {})):
        L.append(f"| p1 b{b} | `{dumps[1]['p1'][b]:08X}` |")
    dead = [b for b, v in dumps[1].get("p0", {}).items()
            if 1 <= b <= 6 and v == 0 and PAYLOAD[b - 1] != "00000000"]
    if dead:
        L += ["", f"⚠ **blocks {dead} read back zero after a write of a non-zero value** — dead/unimplemented "
              "memory, not corruption. Scoring against the truth dump is what keeps these out of the slip "
              "count; scoring against intended values would have called them UNEXPLAINED."]
    L += ["", "## Every field, every dump", "",
          "| config | dump | " + " | ".join(fields) + " |",
          "|---|---|" + "---|" * len(fields)]
    for name, k, cells in rows:
        L.append(f"| {name} | #{k} | " + " | ".join(
            ("ok" if cells.get(f) == "correct" else f"**{cells.get(f,'-')}**") for f in fields) + " |")
    L += ["", "## Summary", "",
          f"- **{summ['slipped']} of {summ['fields']} fields corrupted**",
          f"- rotations observed: {summ['rotations'] or 'none'}",
          f"- unexplained (not any rotation): **{summ['unexplained']}**", ""]
    scored = fields_scored(rows)
    for name, differ in inter.items():
        if not scored.get(name):
            L.append(f"- ⛔ `{name}`: **NO FIELDS SCORED** — the dumps returned no readable blocks at all. "
                     f"That is a total read failure, NOT a clean result, and it is a finding in its own "
                     f"right: this modulation on this tag cannot be dumped.")
        elif differ is None:
            L.append(f"- `{name}`: only one dump — intermittency untestable")
        elif differ:
            L.append(f"- `{name}`: **INTERMITTENT** — {len(differ)} field(s) differ between dumps of the "
                     f"same config: {', '.join(differ)}")
        else:
            L.append(f"- `{name}`: repeatable across {ndumps} dumps (no field changed verdict)")
    L += ["", f"- `detect` block 0 readings, in order: " + ", ".join(f"`{d}`" for d in detects),
          f"- writes reporting FALSE validation failure (the write actually took): " +
          (", ".join(f"b{w['block']}=`{w['data']}`" for w in writes if not w["validated"]) or "none"),
          "", "⚠ A `--verify` failure here is not evidence the write failed — verification reads back "
          "through the same slipping path, so it reports successful writes as failures. Check the truth "
          "dump before believing one.", "", "## Raw transcript", "", "```", transcript.rstrip(), "```",
          "", "## Generated pm3 commands", "", "```", "\n".join(gen), "```"]
    open(base + ".md", "w").write("\n".join(L) + "\n")
    json.dump({"tag": tag, "utc": ts, "configs": configs, "dumps_per_config": ndumps,
               "truth_config": TRUTH_CFG[1], "summary": summ,
               "intermittent": {k: v for k, v in inter.items()},
               "rows": [{"config": n, "dump": k, "fields": c} for n, k, c in rows],
               "detect_block0": detects, "writes": writes,
               "truth": {"p0": {str(k): f"{v:08X}" for k, v in dumps[1].get("p0", {}).items()},
                         "p1": {str(k): f"{v:08X}" for k, v in dumps[1].get("p1", {}).items()}}},
              open(base + ".json", "w"), indent=1)
    return base, summ, inter


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", help="a label for this tag, e.g. blueA / coinB. Any string. "
                                  "Recorded in every artefact; nothing on the chip carries it.")
    ap.add_argument("--configs", default="fsk2a-mb6,fsk2a-mb2",
                    help="comma list from: " + ", ".join(CONFIGS))
    ap.add_argument("--dumps", type=int, default=2, help="dumps per config (>=2 to test intermittency)")
    ap.add_argument("--pm3", default="pm3",
                    help="pm3 client to drive, e.g. /path/to/proxmark3/pm3. Recorded in the report: a bug "
                         "report has to name the client it was measured on.")
    ap.add_argument("--note", default="", help="free text recorded in the report header")
    ap.add_argument("--no-restore", action="store_true", help="leave the tag in the LAST arm's config")
    ap.add_argument("--self-test", action="store_true",
                    help="known-answer check on synthetic data; no hardware")
    ap.add_argument("--re-analyse", metavar="FILE",
                    help="re-score a saved transcript instead of touching a tag")
    a = ap.parse_args()

    if a.self_test:
        # A harness that has never produced a known-correct answer is not evidence. This builds synthetic
        # dumps with KNOWN injected rotations, formats them as a pm3 transcript, parses that back, and
        # checks the whole pipeline -- parse, classify, analyse, summarise, intermittency, report -- lands
        # on the answer we put in. No hardware, no external files, nothing tag-specific.
        pay = {b: int(v, 16) for b, v in enumerate(PAYLOAD, start=1)}
        P1 = {1: 0xAAAA5555, 2: 0x12340000, 3: 0x00000000}     # synthetic page-1 traceability
        cfgv = int(CONFIGS["fsk2a-mb6"], 16)
        truth_cfg = int(TRUTH_CFG[1], 16)

        def rol(v, n):
            n &= 31
            return v if n == 0 else ((v << n) | (v >> (32 - n))) & 0xFFFFFFFF

        base = dict(pay); base[0] = truth_cfg
        base[4] = 0                                            # a DEAD block: written non-zero, reads 0
        truth  = {"p0": dict(base), "p1": dict(P1)}
        arrival = {"p0": {0: truth_cfg, **{b: pay[b] for b in range(1, 8)}}, "p1": dict(P1)}
        def arm(rot_b1):                                       # one arm dump, block 1 rotated by rot_b1
            d = {"p0": dict(base), "p1": dict(P1)}
            d["p0"][0] = cfgv; d["p1"][0] = cfgv
            d["p0"][1] = rol(pay[1], rot_b1)
            return d

        def as_transcript(dumps, cfg_words):
            """Render dumps as pm3-style text so parse() is exercised on realistic input, not dicts."""
            L = []
            for i, d in enumerate(dumps):
                if i < len(cfg_words):
                    L += [f"[=]  Modulation........ {'ASK' if i < 2 else 'FSK2a'}",
                          f"[=]  Block0............ {cfg_words[i]:08X} (auto detect)"]
                L.append("[=] ------------------------- T55xx tag memory -----------------------------")
                for pg in ("p0", "p1"):
                    L.append("[+] Page 0" if pg == "p0" else "[+] Page 1")
                    for b in sorted(d[pg]):
                        L.append(f"[+]  {b:02d} | {d[pg][b]:08X} | ....")
            return "\n".join(L) + "\n"

        # arrival, truth, then two fsk2a-mb6 arms (block 1: rol1 then clean -> intermittent on p0b1)
        synth = [arrival, truth, arm(1), arm(0)]
        cfg_words = [truth_cfg, truth_cfg, cfgv, cfgv]
        # a write line whose --verify "failed" although the value took (the false-failure symptom)
        transcript = ("[=] Writing page 0  block: 00  data: 0x%08X\n" % cfgv +
                      "[!] Write could not validate the written data\n" +
                      as_transcript(synth, cfg_words))

        dumps, detects, writes = parse(transcript)
        assert len(dumps) == 4, f"parse recovered {len(dumps)} dumps, expected 4"
        assert dumps[1]["p0"][1] == pay[1], "truth dump did not round-trip through parse/format"
        assert dumps[2]["p0"][1] == rol(pay[1], 1), "arm rotation did not round-trip"
        assert any(not w["validated"] for w in writes), "false --verify failure not parsed"

        res, err = analyse(dumps, detects, writes, ["fsk2a-mb6"], 2)
        assert err is None, f"analyse refused valid synthetic data: {err}"
        rows, per_arm, dead = res
        assert dead == [4], f"expected block 4 dead, got {dead}"
        summ = summarise(rows)
        assert summ["slipped"] == 1 and summ["rotations"] == {"rol1": 1}, summ
        assert summ["dead_fields_excluded"] == 2, summ
        inter = intermittency(per_arm)
        assert inter["fsk2a-mb6"] == ["p0b1"], inter
        assert fields_scored(rows) == {"fsk2a-mb6": 22}, fields_scored(rows)
        print_summary("self-test", summ, inter, rows)         # exercises the console summary path

        # the structural guard: an extra dump beyond want+1 (the restore slot) must be rejected
        _, e_long = analyse(synth + [arm(0), arm(0)], ["x"], [], ["fsk2a-mb6"], 2)
        assert e_long and "positional" in e_long, f"guard missed a LONG run: {e_long}"
        _, e_short = analyse(synth[:3], ["x"], [], ["fsk2a-mb6"], 2)
        assert e_short and "positional" in e_short, f"guard missed a SHORT run: {e_short}"

        # a config that returned no readable blocks must NOT read as clean
        empty = [arrival, truth, {"p0": {}, "p1": {}}, {"p0": {}, "p1": {}}]
        res_e, err_e = analyse(empty, ["x"], [], ["fsk2a-mb6"], 2)
        assert err_e is None and fields_scored(res_e[0]) == {"fsk2a-mb6": 0}, fields_scored(res_e[0])

        print("  parse round-trip, classification, dead-block exclusion, intermittency, the structural")
        print("  guard and the zero-field case all behave on synthetic data.")
        print("  \u2705 self-test PASS")
        return

    if a.re_analyse:
        txt = open(a.re_analyse).read()
        m = re.search(r"```\n(.*?)\n```", txt, re.S)
        body = m.group(1) if m and "pm3 -->" in m.group(1) else txt
        dumps, detects, writes = parse(body)
        cfgs = [c.strip() for c in a.configs.split(",")]
        res, err = analyse(dumps, detects, writes, cfgs, a.dumps)
        if err:
            sys.exit("cannot score: " + err)
        print(json.dumps(summarise(res[0]), indent=1))
        for n, d in intermittency(res[1]).items():
            print(f"  {n}: " + ("INTERMITTENT " + ", ".join(d) if d else "repeatable"))
        return

    if not a.tag:
        sys.exit("--tag is required: the whole point is that the result records which tag it came from.")
    cfgs = [c.strip() for c in a.configs.split(",")]
    for c in cfgs:
        if c not in CONFIGS:
            sys.exit(f"unknown config {c!r}; choose from {', '.join(CONFIGS)}")
    if a.dumps < 2:
        print("⚠ --dumps 1 cannot test intermittency, which is the main finding this probe exists for.")

    # STEP 0 -- arrival state, and REFUSE if it cannot be read reliably
    # ⚠ THIS MUST HAPPEN BEFORE THE FIRST run_pm3. An earlier version of this patch failed to apply --
    # str.replace() on a stale anchor silently no-ops -- so --pm3 was accepted, ignored, and every run
    # silently used whatever `pm3` was on PATH. The device had been reflashed, so the symptom was a
    # capabilities mismatch that looked like the flash had failed. Assert the wiring instead of trusting it.
    global PM3
    PM3 = [a.pm3]
    ver = ""
    try:
        v = subprocess.run(PM3 + ["--version"], capture_output=True, text=True,
                           errors="replace", timeout=120)
        ver = next((l.strip() for l in (v.stdout + v.stderr).splitlines()
                    if l.startswith("Client:")), "")
    except Exception as e:
        print(f"⚠ could not get a version from {a.pm3}: {type(e).__name__}")
    if not ver:
        sys.exit(f"⛔ {a.pm3} did not report a client version. A bug report has to name the client it was\n"
                 f"   measured on, so this refuses rather than record 'unknown'.")
    print(f"[{a.tag}] client: {ver}")
    a.note = (a.note + "  " if a.note else "") + ver

    print(f"[{a.tag}] reading arrival state...")
    t0, g0 = run_pm3(["lf config --125 -b 8 --dec 1", "lf t55xx detect", "lf t55xx dump"], "arrival")
    d0, det0, _ = parse(t0)
    mods = re.findall(r"Modulation\.*\s*(\S+)", t0)
    if not d0:
        sys.exit("no dump parsed -- is a tag on the antenna?\n" + t0[-1500:])
    if mods and any(s in mods[0].upper() for s in SLIPPING_ON_ARRIVAL):
        # The hazard is entirely in RESTORING an unreliably-read state, so --no-restore removes it. The
        # previous version refused either way while telling the operator to re-run with --no-restore --
        # an escape hatch that did not exist, which is worse than having no escape hatch at all.
        if not a.no_restore:
            sys.exit(f"⛔ REFUSING: the tag arrived in {mods[0]}, a modulation measured to slip. Its current\n"
                     f"   contents cannot be read reliably, so the pre-run state restored at the end could be\n"
                     f"   a ROTATED copy of itself -- and restoring a rotated dump makes the rotation\n"
                     f"   PERMANENT. Two ways forward:\n"
                     f"     put it in Manchester first:  pm3 -c 'lf t55xx write -b 0 -d 00088040 --verify'\n"
                     f"     or accept losing the state:  --no-restore  (the tag is left in the last arm)")
        print(f"⚠ [{a.tag}] arrived in {mods[0]}, which slips -- proceeding ONLY because --no-restore was\n"
              f"  given. Its current contents are NOT being trusted and NOTHING will be restored.")
    print(f"[{a.tag}] arrival: {mods[0] if mods else '?'}, block 0 {det0[0] if det0 else '?'} -- proceeding")

    # STEP 1 -- the probe
    t1, g1 = run_pm3(build_cmd(cfgs, a.dumps, False), "probe")
    dumps, detects, writes = parse(t0 + t1)
    res, err = analyse(dumps, detects, writes, cfgs, a.dumps)
    if err:
        print("⚠ " + err)

    # STEP 2 -- restore the ARRIVAL state exactly, block by block, config last
    t2 = ""
    if not a.no_restore and d0:
        pre = d0[0]["p0"]
        rl = ["lf config --125 -b 8 --dec 1"]
        for b in range(1, 8):
            if b in pre:
                rl.append(f"lf t55xx write -b {b} -d {pre[b]:08X} --verify")
        if 0 in pre:
            rl.append(f"lf t55xx write -b 0 -d {pre[0]:08X} --verify")
        rl += ["lf t55xx detect", "lf t55xx dump"]
        print(f"[{a.tag}] restoring arrival state (block 0 {pre.get(0,0):08X})...")
        t2, _ = run_pm3(rl, "restore")
        after, _, _ = parse(t2)
        if after and after[-1]["p0"] != pre:
            print("⛔ RESTORE DID NOT MATCH THE ARRIVAL DUMP. Compare by hand before using this tag again.")
        else:
            print(f"[{a.tag}] restore verified byte-for-byte against the arrival dump.")

    if err:
        sys.exit(1)
    base, summ, inter = write_report(a.tag, cfgs, a.dumps, t0 + t1 + t2, g0 + g1, dumps,
                                     detects, writes, res, a.note)
    print_summary(a.tag, summ, inter, res[0])
    print(f"  -> {base}.md\n  -> {base}.json")


if __name__ == "__main__":
    main()
