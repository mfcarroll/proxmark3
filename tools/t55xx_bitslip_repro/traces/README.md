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

## Not represented: the psk1 residual

PSK1 on ICR 0 silicon still returns a whole-word inversion on roughly a tenth of reads, and **no trace
here reproduces it**. Forty consecutive `read -b 0` captures were taken on a tag that inverts during
`lf t55xx dump`, and not one of them differed between builds — so the fault appears to depend on field
state across a read sequence rather than on the samples of a single acquisition alone.

Capturing it needs the data-block reads of a dump sequence saved individually and scored against a
known payload, keeping the ones that come back complemented. Until that exists, that fault cannot be
worked on offline.
