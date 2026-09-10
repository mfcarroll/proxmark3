# Saved captures — offline material, not tests

Each `.pm3` is one 12000-sample graph buffer from a `lf t55xx read -b 0` on a real T5577. Replay with:

```sh
pm3 -c 'data load -f <trace>.pm3; data rawdemod --nr'     # or --fs, --p1
```

The demod is deterministic on a saved buffer, so these reproduce a specific demodulation without
hardware or a tag. They exist so demod work can be iterated offline.

## Why these are material and not tests

They are deliberately **not** wired into `tools/pm3_tests.sh`. The only thing assertable here is a raw
bitstream — the first 32 bits are not the configuration word, because the stream starts wherever the
demodulator started — so a test would pin an arbitrary string rather than assert a correct value. Every
LF test in that suite asserts a semantic result (`"AWID ID found"`), and a bitstream pin would be out
of keeping and brittle.

There is also nothing semantic left to assert. `lf t55xx detect -1` searches the buffer it just
demodulated, so it self-corrects a rotated or inverted stream within one capture. And the tag decoders
— awid, hid prox, ioprox, paradox and the rest — search for a preamble, so they are shift-insensitive.
That is exactly why the offline suite stays green whether or not the lead-in bugs are present.

The faults these traces carry are only visible where an absolute bit offset is carried **between**
captures, which is what `lf t55xx dump` does over twelve acquisitions. One saved buffer cannot express
that, so `dump` has no offline test either.

## What each one carries

| trace | modulation | flag | what it shows |
|---|---|---|---|
| `t55xx_direct_leadin_a/b` | DIRECT/NRZ, rf/32 | `--nr` | Leading samples counted as bits. Output changes once `nrzRawDemod` starts the bitstream at the first level change. Every direct capture taken showed this, so any of them serves. |
| `t55xx_fsk2a_onewave_leadin_a/b` | FSK2a, rf/50 | `--fs` | A leading run of a single subcarrier wave, rounded to zero bits and then forced to one. The post-fix stream is exactly the pre-fix stream shifted left by one — the fabricated bit removed. Two of fourteen captures showed it. |
| `t55xx_psk1_clean` | PSK1, rf/32 | `--p1` | A control. Demodulates identically on every build; useful for confirming a change is targeted rather than global. |

## The psk1 inversion — matched pair

| trace | flag | first 32 bits demodulated |
|---|---|---|
| `t55xx_psk1_notinverted_a/b` | `--p1` | `99699696` — exactly the word written to block 6 |
| `t55xx_psk1_inverted_a/b` | `--p1` | `B34B34B4`, which is `complement(99699696) ror1` |

All four are reads of **the same block, on the same tag, in the same PSK1 configuration**, taken
minutes apart in one command sequence. Two invert and two do not, so they isolate the fault to the
samples and nothing else — the inversion reproduces on replay, with no hardware.

Capturing these needed the read sequence a dump performs, not a loop of block 0 reads. Block 0
re-anchors against its own known value on every read (`t55xx_stream_holds` in `cmdlft55xx.c`), so a
block 0 capture self-corrects and can never show the fault: forty consecutive block 0 reads on this
same tag produced not one difference between builds. It is the **data** block reads that carry it, and
only when block 0 has been read first to set the anchor, as `lf t55xx dump` does.

PSK1 on ICR 0 silicon returns a whole-word inversion on roughly a tenth of reads. As of these traces
the cause is localised to the first emitted bit: `dest[numBits++] = curPhase` asserts a bit at the
anchor that need not sit on a true bit boundary, and the next real transition re-syncs the remainder.
