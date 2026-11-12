# audio_kws — live DMIC → Log-Mel → Ethos-U KWS firmware template

`main.cpp.in` is generated once by `scripts/vendor_kws_template.py` from NuML App Builder
v0.1.8 `tools/audio_kws_runtime.py::_render_main`, then filled per deploy by
`nuteach/mcu/kws_codegen.py::render_kws_main()` (35 `@@TOKEN@@` placeholders) and staged by
`nuteach/mcu/apps/kws.py`. Nothing here is edited by hand: re-run the vendoring instead.

What differs from upstream, and why:

* The mel filterbank and the Hann window are **not** computed on the board. Both are exported
  from `nuteach/audio_frontend.py` — the same objects training, Preview and the INT8
  calibration use — as `s_melStart/s_melCount/s_melOffset/s_melWeights` (row-normalised, so
  there is no inverse-sum normaliser) and `s_hannTable`. Upstream's own `_mel_filters()` uses
  different bin edges; keeping it would give the board a plausible but systematically wrong
  front-end.
* `NUML_BACKGROUND_INDEX` is only read inside `#if NUML_HAS_BACKGROUND`. A project without a
  background/silence class (`["yes", "no"]`) sets the flag to 0; there every label may trigger
  and the trigger-margin baseline is the runner-up score instead of the background score.
* **Re-arming is per arm.** After a detection the trigger stays disarmed until `released` goes
  true. With a background class that is upstream's rule (the smoothed winner falls back below
  `NUML_REARM_SCORE`). Without one it cannot be: the scores are one softmax, so with two labels
  the winner never drops below 0.5 and only the RMS gate could ever re-arm — the firmware would
  fire once per silence instead of once per word. The no-background arm therefore re-arms as
  soon as the score or margin test stops holding.
* The USB-CDC build waits (bounded, `NUML_BOARD_LOG_WAIT_MS`, default 10 s) for a terminal to
  open the port before the banner prints, so the student does not lose the first lines while
  Windows enumerates. The UART build compiles that away to `((void)0)`.
* The smoothing window, trigger margin, re-arm score and RMS gate are tokens, not constants,
  so the project's `kws_runtime` settings actually reach the firmware.
* `printf()` goes over USB CDC when `NUML_USE_USB_CDC` is defined (the LCD-less
  NuGestureAI-M55M1 board); `nuteach/mcu/apps/kws.py` stages
  `mcu_toolkit/apps/common/cdc/cdc_only/` and adds that define.
* The clip length is not assumed to be 1 s / 98 frames. `CLIP_SAMPLES`, `FRAME_COUNT` and
  `COOLDOWN_HOPS` are derived from the project's `audio_frontend.json` (`clip_seconds`, one of
  1 / 1.5 / 2 / 3 / 4 / 5 s; frames = 1 + (clip_seconds * 16000 - 400) / 160). The inference hop
  stays 0.25 s and smoothing stays 4 windows, so every keyword is still scored every 0.25 s and
  the cooldown after a detection is one clip length. The 5 s ceiling is an SRAM limit: the ring
  buffer, clip copy and float log-mel scratch share the 1 MiB SRAM01 window with the Ethos-U
  tensor arena (about 892 KB used at 5 s with 40 mel and 3 labels). The boot banner prints the
  real window length (`Collecting first %.2f-second audio window`) for the same reason.

The DMIC pin macros and channel mask come from `mcu_toolkit/boards.json` via
`boards.AudioDmic`, never from this file.

The DMIC DSP gain is the one number that is deliberately not a token:
`DMIC_SetDSPGainVolume(..., 36)`, +36 dB, the value hardware-tested on GestureAI. The collection
firmware bundled for students (`1_Collect_Firmware_bin`) records at +24 dB. The log-mel front end
subtracts each clip's own maximum, so the 12 dB offset does not change the features the model
sees; it only shifts the absolute dBFS numbers in the serial log (`rms=`) and the RMS gate by
12 dB relative to clips recorded with the collection firmware.
