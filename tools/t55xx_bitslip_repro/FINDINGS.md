# What the probe found

Notes from the investigation this tool was built for: issue
[#3512](https://github.com/RfidResearchGroup/proxmark3/issues/3512), `lf t55xx dump` returning silently
rotated blocks. Kept here because the measurements are what justify the fixes, and because three
attempted fixes had to be abandoned — knowing which ones, and why, is worth more than knowing the
answer.

Measured on eight physical T5577s: five Invengo dual-frequency fobs (`0x45`, CID `0x01` ATA5577M1,
ICR 2), two Silicon Craft coins (`0x39`, ICR 0), and one Silicon Craft orange fob (`0x39`, ICR 2).

## Five faults, one family

Every one is the same mistake in a different demodulator: **the capture opens before the tag answers,
and the samples before the response were treated as data.**

| where | what |
|---|---|
| `nrzRawDemod` | Counted the samples before the first level change as bits. `(first_edge + clk/4) / clk`, and where no edge arrives within ten clocks the long-run flush fires first and invents ten bits out of the quiet region. `startIdx` was derived from the same rounded count, so it held `i % clk` and could not name a sample. |
| `pskRawDemod_ext` | Accepted a first phase shift found in the lead-in. |
| `pskFindFirstPhaseShift` | Judged the **first partial wave** as a phase shift. `waveStart` begins as wherever the caller started looking, so the first length measured is the gap from an arbitrary sample to the next peak — part of a wave, not a wave. |
| `aggregate_bits` (fsk) | Sized each run in bits then forced `if (n == 0) n = 1;`. Right mid-stream; on the leading run a single subcarrier wave rounds to zero bits and was forced to one, fabricating a bit. |
| `test_scan` (t55xx) | Not a demod fault. A block read repeats one 32-bit word, so every offset yields a rotation and several can pass the structural checks; the scan answered with whichever it met first. |

The measurements that pinned each one:

- **nrz** — the first edge lands on sample 438 or 439 between acquisitions, and `(438-319+8)/32` is 3
  while `(439-319+8)/32` is 4. **One sample** of jitter added or dropped a leading bit and rotated
  everything after it. Fixing it moved the t55xx anchor from alternating between samples 32 and 64 to
  holding 470/471.
- **psk lead-in** — over 54 block reads, every inverted word had exactly **one extra** `curPhase`
  toggle and a first shift at sample 19–25; every clean word had the expected count and a first shift
  at 52 or beyond. No overlap.
- **psk partial wave** — a matched pair of captures (same block, same tag, same configuration, two
  inverted and two not) differed in nothing but this: the clean ones walked thirteen ordinary
  two-sample waves before accepting a three-sample one at 63; the inverted ones judged their very
  first candidate, a three-sample gap at 32. Same clock, same fc, same seed, same amplitude decision.
- **fsk** — over 80 fields, every failure had a leading run of exactly one wave. Twelve reads hit that
  case; after the fix the one-wave leading run does not appear in the distribution at all.
- **t55xx rotation** — on a live 353-bit buffer, exactly two rotations passed the structural checks:
  `00080040` at offsets 20/52/84… and `00080001` (its `ror19`) at 1/33/65…. The scan starts at 28 and
  takes the first hit, so it landed on 33. **The old behaviour was scan-order luck, not correctness.**

## Three fixes that were tried and reverted

Each measured flat or ambiguous in paired A/B rounds. Recorded so nobody repeats them.

**1. Anchoring psk on `firstFullWave`** — the shape that worked for nrz. Alternating A/B, four rounds
each: corruption 97/175 → 97/172. *Identical.* It removed the `ror5`/`ror6` signature it targeted but
substituted `ror9`–`ror14` in one round of four, because a noise-triggered false shift is not a real
bit boundary — anchoring the grid on it turns a bounded error into an unbounded one.

**2. Removing the amplitude polarity override** (`if (lastAvgWaveVal > FSK_PSK_THRESHOLD) *curPhase ^= 1;`,
whose own comment says "could cause inverting"). Four rounds each: inversions 61 → 64. *Unchanged.*
The seed was never the cause. The clue was available beforehand and ignored: the override fired the
"wrong" way on 2 of 73 captures, ~3%, against a symptom rate of 39%.

**3. Extending the lead-in guard to reject a candidate at the search origin.** This *did* remove every
inversion — but replaced them with single-bit errors, always the top bit, at the same rate. Reverted:
a flipped MSB is equally wrong and *less* likely to be noticed than an inverted word.

## Self-consistency: an idea that cannot work

A block read repeats its word about eleven times per buffer, so a mid-stream phase flip would show as
`W W W ~W ~W ~W` — detectable, and correctable by taking the longest consistent run. Measured: inverted
reads have **eleven repetitions, all eleven identical**. The inversion is uniform across the capture,
not mid-stream. There is nothing to detect and no majority to take. Do not revisit this.

## Why there is no offline test

The faults are only visible where an absolute bit offset is carried **between** captures, which is what
`lf t55xx dump` does across twelve acquisitions. A single saved graph buffer cannot express that.

- `lf t55xx detect -1` **self-corrects** — it searches the buffer it just demodulated, so a rotated or
  inverted stream is resolved inside one capture. Verified: pre-fix and post-fix builds both read
  `00081040` from the same trace.
- The tag decoders — awid, hid prox, ioprox, paradox and the rest — **search for a preamble** and are
  therefore shift-insensitive.

Together that is why `tools/pm3_tests.sh` stays green whether or not these faults are present, and why
adding traces to it would pin arbitrary bitstreams rather than assert correct values. The captures in
`traces/` are offline material instead — see that README.

## Populations

Behaviour split cleanly by silicon, not by coupling as first assumed:

- **Invengo ATA5577M1 ICR 2** (dual fobs) — psk faults dominated by the lead-in shift. Large benefit.
- **Silicon Craft ICR 0** (coins) — residual inversions from the partial-wave fault, which the lead-in
  fix alone did not touch.
- **Silicon Craft ICR 2** (orange fob) — clean throughout. Blocks 3–6 are unimplemented on this
  silicon, which is also the only real-hardware exercise the probe's dead-block path has had.

## Method notes

- **Paired A/B rounds, alternating, in one sitting.** Single runs on a marginal tag swung between 23/37
  and 65/66 corrupted. Two of the three reverted fixes looked like clear wins on their first run.
- **Check a candidate cause is quantitatively big enough before coding.** Both wrong fixes came from
  skipping that arithmetic.
- **Get a reproducible offline pair before iterating on a demod fault.** Hours of live rounds failed at
  the partial-wave fault; the matched pair isolated it in one diff.
- **Score shifts and inversions, not just rotations.** A demod that opens a bit early pads with zero
  rather than wrapping, and psk can return a complemented word. Both arrived as `UNEXPLAINED` before
  the classifier was taught them, which hid one fault and made another look like a regression.
