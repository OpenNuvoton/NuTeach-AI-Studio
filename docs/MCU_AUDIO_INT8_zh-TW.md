# Audio INT8 模型 MCU 整合規格

## 最重要的結論

```text
audio_classifier_spectrogram_int8.tflite
```

的輸入不是 DMIC 原始 PCM，而是：

```text
[1, frames, 40, 1] INT8 log-mel spectrogram      （預設 1 秒片段：[1, 98, 40, 1]）
```

實際 shape、scale 及 zero-point 請以輸出 ZIP 的 `conversion_report.json` 為準。

frame 數由專案的取樣長度 `clip_seconds`（訓練設定「取樣長度」，可選 1／1.5／2／3／4／5 秒，
預設 1 秒；`audio_frontend.json` 也會寫出這個值）決定：

```text
frames = 1 + ⌊(clip_seconds × 16000 − 400) ÷ 160⌋   （整數除法、無條件捨去）
```

| `clip_seconds` | PCM samples | frames | 輸入 shape（40 mel） |
|---:|---:|---:|---|
| 1 | 16,000 | 98 | `[1, 98, 40, 1]` |
| 1.5 | 24,000 | 148 | `[1, 148, 40, 1]` |
| 2 | 32,000 | 198 | `[1, 198, 40, 1]` |
| 3 | 48,000 | 298 | `[1, 298, 40, 1]` |
| 4 | 64,000 | 398 | `[1, 398, 40, 1]` |
| 5 | 80,000 | 498 | `[1, 498, 40, 1]` |

`mel_bins` 可以是 **40（預設）或 64**，兩種寬度都能部署到開發板；選 64 時輸入就是
`[1, frames, 64, 1]`（1 秒為 `[1, 98, 64, 1]`），其餘步驟完全相同。這份文件以 **1 秒、40 mel**
為例說明；換了取樣長度只有 samples 數與 frames 數不同，每一個 frame 的算法完全一樣。

> 想直接把模型建成 M55M1 韌體、不自己寫 C 的話，看 `docs/MCU_DEPLOY_zh-TW.md`：
> 下面這些表 Studio 會自己生成（見「Studio 會自動生成表」一節）。

## PCM 規格

- Mono
- 16,000 samples/second
- 每次推論 clip_seconds × 16,000 samples（預設 1 秒 = 16,000 samples；5 秒 = 80,000 samples）
- 音訊先轉為約 `[-1.0, +1.0]` 的 Float reference domain

MCU 可用 int16 PCM 保存，前處理時再轉換；或全程用 Q-format 固定點近似。

## 前處理順序

1. 取 clip_seconds × 16,000 個 PCM samples（1 秒為 16,000 個）。
2. 使用 400-sample Hann window（25 ms）。
3. 每次移動 160 samples（10 ms）。
4. 得到 frames = 1 + ⌊(samples − 400) ÷ 160⌋ 個 frames（整數除法；1 秒為 98、1.5 秒為 148、5 秒為 498；1.5 秒與 5 秒會多出 80 個尾端樣本不用，見上表）。
5. 每 frame 補零至 512 points。
6. 執行 512-point RFFT。
7. 計算 `real² + imag²` power spectrum，共 257 bins。
8. 乘上 40 個 mel triangular filters。
9. 計算 `10 × log10(max(mel_power, 1e-10))`。
10. 每一段 spectrogram 減去自己的最大 dB。
11. 截斷至 `[-80 dB, 0 dB]`。
12. 映射到 `[0, 1]`。
13. 使用 TFLite input 的 scale／zero-point 轉成 INT8：

```c
q = round(float_value / input_scale + input_zero_point);
q = clamp(q, -128, 127);
```

14. 以 NHWC 排列送入 `[1, frames, 40, 1]`（1 秒為 `[1, 98, 40, 1]`）。

## 必須一起帶到 MCU 的檔案

```text
audio_classifier_spectrogram_int8.tflite
labels.txt
audio_frontend.json
conversion_report.json
```

開發與驗證階段也保留：

```text
audio_frontend_reference.py
run_model.py
```

## Studio 會自動生成表

上面第 2 步的 Hann 視窗與第 8 步的 mel 濾波器組，**不需要自己算、也不該自己填**。
用 Studio 的「部署到開發板」建置韌體時，這些表是這樣來的：

```text
models\audio_frontend.json      （Export 時寫出的前端契約：取樣率、clip_seconds、視窗、hop、FFT、mel_bins、fmin、fmax、db_floor、frame_count）
        │
        ▼
nuteach\mcu\kws_codegen.py     kws_tables()：呼叫訓練用的同一支 nuteach.audio_frontend.mel_filterbank()
        │                       把 40×257（或 64×257）稀疏矩陣壓成每個濾波器的 start bin + 權重，
        │                       Hann 係數用同一組對稱視窗
        ▼
韌體的 main.cpp                  以巨集與陣列的形式直接編進 firmware.bin
```

因為表是從**訓練用的同一個函式**產生的，PC 與 MCU 不可能算出不同的頻譜；`mel_bins`
改成 64 時表會自動變寬，不必改任何 C 程式碼。片段長度也一樣：韌體的 `CLIP_SAMPLES`／
`FRAME_COUNT`／`COOLDOWN_HOPS` 是從 `audio_frontend.json` 的 `clip_seconds` 推出來的；推論 hop
仍固定 0.25 秒、平滑仍是 4 個視窗，所以板子上每個關鍵字還是每 0.25 秒評分一次，偵測到之後
的冷卻時間等於一個片段長度。觸發門檻、平滑視窗數等執行期參數同樣來自訓練報告，不是寫死在
韌體裡。

想看表的內容（例如要自己寫 C、或想核對 MCU 端的實作），直接印出來：

```bat
.venv\Scripts\python.exe reference\audio_kws\mcu_tables.py --frontend <匯出ZIP>\audio_frontend.json
```

加 `--out kws_tables.h` 可以存成檔案。這支腳本呼叫的就是 `nuteach.mcu.kws_codegen`，
和部署時生成的內容一致。

## 與 CMSIS-DSP 的對應

- Hann window：預先建立 400 個係數。
- RFFT：可使用 `arm_rfft_fast_f32` 作為第一個正確性版本；效能完成後再改 Q15/Q31。
- Mel filterbank：40×257 矩陣很稀疏，可預先只保存每個 filter 的 start/end bin 與權重。
- log10：第一版可用 Float；固定點版本可使用 LUT／近似函式。

## 記憶體與上限

取樣長度的上限是 5 秒，電腦與三塊板子都是同一份清單（1／1.5／2／3／4／5 秒）。上限來自 SRAM
而不是 flash：韌體的 DMIC 環形緩衝區、片段複製與 float log-mel 暫存，跟 Ethos-U 的 tensor arena
都放在同一塊 1 MiB（1,048,576 B）的 SRAM01。2026-09-19 用內附的 Vela 5.1.0（40 mel、3 個標籤）
實測的 tensor arena（Performance 模式）：

| `clip_seconds` | tensor arena |
|---:|---:|
| 1 | 101,376 B |
| 2 | 197,632 B |
| 3 | 293,888 B |
| 5 | 485,376 B |

音訊緩衝區 1 秒是 84,832 B，每多 1 秒多 80,000 B（5 秒 404,832 B）；5 秒時 SRAM01 合計約 892 KB。
10 秒光音訊緩衝區就要約 805 KB，再加上至少 250 KB 的 arena，放不下。前處理的 CPU 時間在
GestureAI 上量到每秒片段 29.2 ms／每個 hop（5 秒約 148 ms，仍低於 250 ms 的推論 hop）。Flash
不是瓶頸：INT8 模型 30 KB，1 秒的韌體約 269 KB。建置韌體時若被「tensor arena 超過板子預算」
擋下，先縮短取樣長度，再考慮把 Mel Bins 調回 40。

板子端的 DMIC DSP 增益是 +36 dB（`DMIC_SetDSPGainVolume`，GestureAI 實測值），而附贈的收音韌體錄音時是
+24 dB。前端會先減掉片段自己的最大值，所以這 12 dB 的差距不會改變特徵；只有序列埠記錄裡的 `rms` 與
RMS 閘門的絕對 dBFS 數字，會比收音韌體錄下來的片段高 12 dB（安靜房間在 GestureAI 上約 −40 dBFS，同一個房間用收音韌體錄下的片段約 −50 dBFS）。

## 驗證方法

準備同一個長度等於 clip_seconds 的 WAV（預設 1 秒）：

1. PC 執行 `audio_frontend_reference.py` 保存 spectrogram。
2. MCU 前處理後把 frames×40 數值（1 秒為 98×40）透過 UART／USB 傳回 PC。
3. 逐元素比較。
4. 建議先讓最大絕對誤差低於 0.02，再比較量化後 INT8 值是否大致一致。
5. 使用同一個 INT8 tensor 分別在 PC TFLite Interpreter 與 MCU runtime 推論，確認 output 差異。

只比較分類結果不足以找出 frontend 問題；必須先比較中間 spectrogram。
