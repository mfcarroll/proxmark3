# `bitslip_probe.py` — a reproducer for the `lf t55xx dump` bit-slip bug

Standalone companion to an issue against
[RfidResearchGroup/proxmark3](https://github.com/RfidResearchGroup/proxmark3): `lf t55xx dump` and
`lf t55xx rdbl` extract each 32-bit block at a bit offset **cached from the last `detect`**, and never
re-derive it from the buffer being decoded. When a later read's demodulation starts on a different bit
boundary, the returned word is a **bit rotation** of the true block — silently. `detect` is unaffected
because it searches for the offset in the buffer it just demodulated.

This script measures the effect systematically on one tag at a time.

## What it does

For a tag on the antenna, it: writes a known non-byte-periodic payload; sets ASK/Manchester and dumps to
establish ground truth (Manchester has a deterministic demod start and is clean); then sets each
modulation in turn and dumps twice. Every field of every dump is classified against all 31 rotations of
the truth-dump value. It restores the tag's arrival state at the end and verifies the restore.

It writes a `.md` report and a `.json` beside itself, both naming the tag and the exact client version.

## Requirements

- Python 3.8+
- a built `pm3` client — pass it with `--pm3 /path/to/proxmark3/pm3` (defaults to `pm3` on PATH)
- a **blank / spare** T5577 you can overwrite (it writes blocks 0–7 of page 0)

## Use

```sh
# no hardware -- known-answer check on synthetic data
./bitslip_probe.py --self-test

# one tag, the two FSK2a configs (default)
./bitslip_probe.py --tag tagA --pm3 /path/to/proxmark3/pm3

# the full six-modulation sweep
./bitslip_probe.py --tag tagA --pm3 /path/to/proxmark3/pm3 \
    --configs manchester-mb2,fsk2a-mb6,fsk2a-mb2,psk1-mb2,direct-mb2,biphase-mb3

# re-score a saved report without touching a tag
./bitslip_probe.py --re-analyse bitslip_tagA_<utc>.md
```

## Safety

- ⚠ **It writes.** Use a spare tag, not a credential you cannot recreate.
- It **refuses to start** if the tag arrives in PSK1 or DIRECT (a slipping modulation), because the
  arrival state it would restore could be a rotated copy of itself — restoring which makes the rotation
  permanent. Put the tag in ASK/Manchester first, or pass `--no-restore`.
- The payload must **not** be byte-periodic. `A5A5A5A5` lands on itself under many rotations and hides the
  bug; the built-in payload is irregular for this reason.

## Trusting it before you point it at a tag

`--self-test` builds synthetic dumps with known injected rotations, formats them as a `pm3` transcript,
parses that back, and checks the whole pipeline lands on the answer it put in — parse, classify,
dead-block exclusion, intermittency, the structural guard, and the zero-field case. No hardware, no
external files.
