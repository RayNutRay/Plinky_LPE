# Working notes

Shared context between machines: a cloud session cannot see a local session's
context, so anything worth keeping lives here and travels with `git pull`.

Firmware in use: `build/personal`, v0.4.3c. `main` is v0.4.0 and has none of the
clock, midi or touch settings below - check the branch before reasoning about
behaviour.

## Clock chain: Pamela -> Plinky -> FM-1 (FRASIL)

| Setting | Menu | Values |
|---|---|---|
| CV clock in PPQN | `CV` -> PPQN | 1, 2, 4, 8, 16, 24, 48 (default 4) |
| CV clock out PPQN | `CV out` -> PPQN | same (default 4), only while the sequencer plays |
| MIDI in clock mult | `Midi In` -> Clock mult | x1/2, x1, x2 = assume 48, 24, 12 ppqn in |
| MIDI out clock divider | `Clock` -> Midi out | /1 /2 /3 /4 = 24, 12, 8, 6 ppqn |
| MIDI out notes | `Clock` -> Notes | off makes midi out a clock and transport wire only |
| MIDI in filter | `Midi In` -> Filter | clock / transport / param CC toggles |
| MIDI out filter | `Midi Out` -> Filter 1 | clock / transport / param CC toggles |

### Resolved: FM-1 showed 22 BPM with Pamela at 90

Pamela's channel sends one pulse per quarter, `CV -> PPQN` was 4. Plinky treats
every pulse as a 16th, so its quarter was four times too long and it emitted
6 ppqn instead of 24. Fixed by matching the setting to what Pamela sends.

Diagnostic: the displayed BPM and the outgoing clock come from the same
accumulator, so the BPM on screen must equal Pamela's. The factor between them
is the factor the outgoing clock is wrong by.

### Resolved: plinky kept conducting after the external clock stopped

The outgoing clock had no condition on it, so when Pamela stopped, plinky fell
back to its internal clock and kept FRASIL running at the current preset's tempo
(120 on a default preset). It now only conducts while following an external
clock, or while playing its own sequencer **or arp** - the arp runs off the same
grid, and leaving it out meant an armed arp on the internal clock sent nothing.

Two bugs came out of that change and are fixed: the outgoing pulse index was not
reset in `clock_reset()`, which sent a second pulse right behind the downbeat
(the `FA F8 F8` in the usb capture) or swallowed the next one; and the per-string
midi stream can delay a pulse by a whole tick, because a chord fills the 16 byte
buffer and `cue_midi_out()` bails out on a busy uart - hence `Clock` -> `Notes`.

### FRASIL side (3.3.1, latest as of 14 Nov 2026)

- Follows MIDI clock whenever one arrives; `Start` (FA) only resets the grid.
- `Stop` (FC) is not documented as an incoming message in any version: its
  transport is its own PLAY button or CC 120. Plinky's Stop will not stop it,
  and nothing in 3.3 or 3.3.1 changed that - their midi work was outgoing only.
- Clock source: `GLO` -> MIDI, K4 `CLOCK` = AUTO / INTERNAL. Takes MIDI over both
  USB and TRS. Channels default to drones 1, GLINT 2, BASS 3.
- Plinky sends per-string CC 32-39 (finger position) and CC 40-47 (pressure),
  which FRASIL can MIDI-learn (hold GLO, move the control, up to 8 bindings).

## Touch: catching neighbouring pads

`System` -> `Tch Time` now reaches about 500ms through a table, and the settle
time is measured from the start of the press rather than by walking back through
the touch history. The history is eight frames and a frame is only 3 ticks when
a single finger is down, so it reached about 49ms: any setting past that ran off
the end of the history and made the check *less* strict, not more.

`Tch Range` is the other dial - how far the finger may move while settling - and
it costs no latency, unlike Tch Time, which delays every new note by its value.

## Encoder: the one-step drop

`hardware/encoder.c`: the detent snap `(v & ~0b11) | 2` always rounded up, and
the step was taken with `v >> 2`, which floors, so the threshold was +2
quarter-steps one way and -3 the other and two quarter-steps of drift became a
phantom step, always towards the same side. The snap now rounds to the nearest
detent with half-way resolving to the detent we sit in, and divides instead of
shifting. Not yet confirmed on hardware.

**The `& 127` in `edit_param_from_encoder`'s seam check is deliberate - do not
"fix" it.** It was reverted once already. The mask makes the correction repeat
every 128 raw: three extra steps per 128 is 1.0234 raw per tick, which is what
1024 raw = 100.0 asks for. Without it the correction fires at six values near
zero, 300 ticks land on raw 303 instead of 307, and a parameter tops out near
97.9 instead of 100.0.

## Preset bank batch editing

`tools/plinky_bank.py` - dump / build / verify for PRESETS.UF2.
`tools/test_plinky_bank.py` - self-test, no hardware needed.

Format (from `hardware/memory.c` and `bootloader/Core/Src/ghostfat.c`):
255 journal pages of 2048 bytes at `0x08080000`, each page = payload + 16 bytes
of SysParams + 8 byte PageFooter; hash over the first 2040 bytes; highest `seq`
per `idx` wins; idx 0-31 presets, 32-127 pattern quarters, 128-135 sample infos,
136 floating preset, 137-140 floating pattern. Page 255 is calibration and lies
outside the writable window, so it cannot be damaged.

A preset is 1552 bytes of params and tail, and newer firmware appends to it:
0.4.2c added `multi_params[33][7]` plus 2 padding, making it 2016. The tool
models the first 1552 bytes and keeps the remainder verbatim in `_rest_b64`, so
a build cannot wipe per-string values it does not understand; deleting
`_rest_b64` from a slot is how to clear them on purpose. Pages are filled with
0xff, which is what an erased page and the firmware both leave, so a rebuilt
page comes out byte-identical to one the device wrote.

Three parameter slots were put to use after v0.4.0 and are named for what they
are now - `P_ROOT`, `P_SETTINGS1`, `P_SETTINGS2` - with the old names still
accepted when reading a json dumped earlier.

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

Accelerometer tilt is wired to mod sources X and Y in firmware
(`synth/lfos.c`), and X/Y also leave on the expander CV outs. Its depth is
**not** per preset - only the global `Acc sens` scales it. Per-preset depth would
need a new `Accel` LFO shape, where `Depth` becomes the tilt amount.
