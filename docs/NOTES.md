# Working notes

Shared context between machines: a cloud session cannot see a local session's
context, so anything worth keeping lives here and travels with `git pull`.

Firmware in use: `build/personal` (v0.4.2c). `main` is v0.4.0 and does NOT have
the clock/MIDI settings below - check the branch before reasoning about behaviour.

## Clock chain: Pamela -> Plinky -> FM-1 (FRASIL)

Settings that exist (all in `build/personal`, none in v0.4.0):

| Setting | Menu | Values |
|---|---|---|
| CV clock in PPQN | `CV` -> PPQN | 1, 2, 4, 8, 16, 24, 48 (default 4) |
| CV clock out PPQN | `CV out` -> PPQN | same (default 4), only sent while the sequencer plays |
| MIDI in clock mult | `Midi In` -> Clock mult | x1/2, x1, x2 = assume 48, 24, 12 ppqn in (`MIDI_PPQN = 48 >> mult`) |
| MIDI in filter | `Midi In` -> Filter | clock / transport / param CC toggles |
| MIDI out filter | `Midi Out` -> Filter 1 | clock / transport / param CC toggles |

MIDI clock out is **fixed at 24 ppqn** - no divider, by design. It is derived
from `clock_32nds_q21`, so it inherits any error in the CV PPQN setting.

### Resolved: FM-1 showed 22 BPM with Pamela at 90

Pamela's channel sends 1 pulse per quarter, Plinky's `CV -> PPQN` was 4. Plinky
treats every pulse as a 16th, so its quarter is 4x too long and it emits 6 ppqn
instead of 24. Simulated against `clock_tick()`:

| Pamela sends | CV PPQN set | midi clocks per quarter | factor |
|---|---|---|---|
| 4/quarter | 4 | 23.8 | correct |
| 4/quarter | 8 | 11.9 | 2x slow |
| 4/quarter | 24 | 4.0 | 6x slow |
| 1/quarter | 4 | 6.0 | 4x slow |
| 1/quarter | 1 | 22.6 | correct |

Fix: set `CV -> PPQN` to the pulses per quarter the Pamela channel actually
sends. Diagnostic: Plinky's displayed BPM and the outgoing clock scale by the
same factor, so the BPM on screen must equal Pamela's.

### Open: Plinky keeps conducting after the external clock stops

`clock_tick()` emits the outgoing 24 ppqn with no check on `seq_playing()` or on
the clock source. When Pamela stops, Plinky falls back to its internal clock
(`P_TEMPO`, default 120.0) and keeps streaming, so FRASIL locks to 120 instead
of releasing to its own TEMPO. FRASIL is behaving correctly here.

Proposed change - do not conduct with the internal clock:

```c
if (midi_out_pulse != prev_midi_out_pulse) {
    if (clock_type != CLK_INTERNAL)
        midi_send_clock();
    prev_midi_out_pulse = midi_out_pulse;
}
```

Open question: make this a third state of the clock icon in `Filter 1`
(off / always / external only) rather than changing the behaviour outright.

### FRASIL side

- Follows MIDI clock whenever one arrives; `Start` (FA) only resets the grid.
- `Stop` (FC) is not documented as an incoming message: its transport is its own
  PLAY button or CC 120. So Plinky's Stop will not stop its sequences.
- Clock source: `GLO` -> MIDI, K4 `CLOCK` = AUTO / INTERNAL. Takes MIDI over
  both USB and TRS. Channels default to drones 1, GLINT 2, BASS 3.
- Plinky sends per-string CC 32-39 (finger position) and CC 40-47 (pressure),
  which FRASIL can MIDI-learn (hold GLO, move the control, up to 8 bindings).

## Bug: right encoder drops one step

Reported: after using the clicky right encoder, on release the value jumps one
step to the left. Never to the right. Two candidates found:

1. `synth/params.c`, `edit_param_from_encoder()`: `s8 pos = raw & 127;`
   The rounding-seam correction is meant to fire at raw +/-1, +/-43, +/-86. The
   mask makes the three negative comparisons dead code, aliases negatives onto
   the positive branches (`-42 & 127 == 86`, `-85 -> 43`, `-127 -> 1`) and also
   fires at every raw congruent to 1, 43 or 86 mod 128 (129, 171, 214, 257 ...)
   - about 48 points instead of 6. At those points one click moves 2 raw instead
   of 1, in a direction-dependent way, so turning down and back up does not
   return to the same value. Fix: `s16 pos = raw;` (drop the mask).
2. `hardware/encoder.c`: a detent is 4 quarter-steps, snapped to the detent
   centre with `(v & ~3) | 2`, and the step is taken with an arithmetic `>> 2`
   that floors. Net effect `diff = floor((2 + d) / 4)`: a step is emitted after
   +2 quarter-steps clockwise but only after -3 counter-clockwise. Symmetric
   jitter cancels (verified), but `enc_deltas` returns 0 for double transitions,
   silently dropping a quarter-step, and the accumulator then crosses the
   asymmetric thresholds - which would show up only in one direction.

To tell them apart: does it happen on indexed params (Scale, Octave - driver) or
only on the +/-100.0 scale (mask)? On releasing the knob or releasing the click?
By 0.1 or by a whole position?

## Settings added here

- `Clock` -> `Midi out`: divider for the outgoing midi clock, /1 /2 /3 /4 =
  24, 12, 8, 6 ppqn. New menu section, since both midi sections were full at
  eight items. Stored in the two spare SysParams bits, 0 = /1, no version bump.
- `System` -> `Tch Time` now reaches 194ms instead of 62ms (2ms steps to 40ms,
  then 14ms). Stored indices keep their meaning. Real ceiling is the 8 frame
  touch history, about 190ms.

Still untested: there is no arm-none-eabi toolchain in the cloud container, so
nothing here has been compiled, only reasoned through and simulated.

## Preset bank batch editing

`tools/plinky_bank.py` - dump / build / verify for PRESETS.UF2.

Format (from `hardware/memory.c` and `bootloader/Core/Src/ghostfat.c`):
255 journal pages of 2048 bytes at `0x08080000`, each page = payload + 16 bytes
of SysParams + 8 byte PageFooter; hash over the first 2040 bytes; highest `seq`
per `idx` wins; idx 0-31 presets, 32-127 pattern quarters, 128-135 sample infos,
136 floating preset, 137-140 floating pattern. Page 255 is calibration and lies
outside the writable window, so it cannot be damaged.

**The tool targets the v0.4.0 layout and must not run `build` against a 0.4.2c
bank yet.** There `Preset` is 2016 bytes: `params[96][8]` (1536) + 16 tail bytes
(same offsets) + `multi_params[33][7]` (462) + 2 padding. `build` re-encodes a
preset from scratch and would write the appended per-string values back as
zeroes. It must carry the full payload through and patch only what changes.

## Preset layout plan (32 slots)

Pads are column-major (`pad_id = strip * 8 + pad_y`), so presets are 4 columns
of 8, and preset rows line up with the sample column on the right.

| Col 1 | Col 2 | Col 3 | Col 4 |
|---|---|---|---|
| one-shot / loop, row = sample slot | granular, row = sample slot | 8 solo voices | 5 atmospheres + 3 utility |

Utility slots: `CV DUO` (silent, low note -> voice 1, high -> voice 2, plus gate,
trigger and pressure CV), `QUANT` (CV pitch in -> quantised out, accel on X/Y),
`IN FX` (synth off, input through delay/reverb, sampler armed from the input).

All 32 in D minor: `Scale = Minor`, `Pitch = +2` semitones (raw 171), and
`CV Quant = Scale` so the pitch CV out stays in key.

Samples: 8 slots, 3 orchestral + 2 other instrumental + 2 nature ambiences +
1 voices.

Accelerometer tilt is wired to mod sources X and Y in firmware (`synth/lfos.c`),
and X/Y also leave on the expander CV outs. Its depth is **not** per preset -
only the global `Acc sens` scales it. Per-preset depth would need a new `Accel`
LFO shape, where `Depth` becomes the tilt amount.
