# Audio Project（關鍵字辨識 / KWS）參考腳本

用 Studio 自己的程式，在命令列看清楚「WAV 怎麼變成 log-mel」、重跑訓練、用 INT8 模型辨識一段
聲音，以及把開發板韌體用的 C 表印出來。邏輯全部在 `nuteach/`，這裡只負責參數與列印。

## 資料流

```
麥克風／WAV（16 kHz mono）
      │  audio_frontend.read_wav_file() → fix_length(clip_samples)
      ▼
  log_mel_spectrogram()：
      分幀 400 樣本（25 ms）／位移 160 樣本（10 ms）→ 對稱 Hann → 512 點 RFFT → 功率
      → 40 個三角 mel 濾波器（20-8000 Hz）→ 10*log10 → 減掉自身最大值
      → 截斷到 [-80, 0] → 映射到 [0, 1]
      ▼ 形狀 (frames, 40, 1)，frames = 1 + (clip_seconds × 16000 − 400) ÷ 160，1 秒為 (98, 40, 1)
        ← 模型吃的是這個，不是原始 PCM
  小型 CNN → Dense(n, softmax)
      ▼
  models/<專案>.keras      ← Train 到此為止
      ▼  （Export Model，是另一個 job）
  audio_classifier_spectrogram_int8.tflite ＋ audio_frontend.json ＋ labels.txt
      ▼  （「部署到開發板」頁籤）
  Vela → flash 預算檢查 → 組專案 → GCC → firmware.bin
      韌體用 C 重算同一份 log-mel，表由 mcu_tables.py（kws_codegen.kws_tables()）產生
```

同一套 log-mel 規格必須在三個地方位元級一致：`nuteach/audio_frontend.py` 的實作、
匯出 ZIP 內的 `audio_frontend_reference.py`、以及 `docs/MCU_AUDIO_INT8_zh-TW.md` 描述的 C 實作。

## 可調參數

預設值出自 `nuteach/config.py` 的 `AUDIO_DEFAULTS`，合法範圍出自 `validate_audio_settings()`。

| 參數 | 預設 | 範圍 | 怎麼影響準確率 | 改了會牽動開發板嗎 |
|---|---|---|---|---|
| `augmentation_level` | `medium` | `off`／`light`／`medium`／`strong` | 在**已算好的 log-mel 頻譜**上做時間軸位移（最多 1/24、1/12、1/6 個 frame）、整體增益（±5%、±10%、±20%）與加噪（標準差 0.006、0.012、0.020），再截回 [0, 1]。前處理本身完全沒動，所以 MCU 的 C 版仍然位元相容。錄音環境單一時往上調 | 否（`training`） |
| `spec_augment` | `false` | true／false | 在頻譜上隨機遮一段時間與一段頻帶（各約 1/10），避免模型只背固定位置 | 否（`training`） |
| `session_disjoint_validation` | `true` | true／false | 驗證片段不與訓練片段共用同一段錄音。關掉（clip-level 切分）會讓準確率系統性虛高；某類 session 不足 2 個時會自動退回並在報告寫 `split_warning` | 否，但直接決定報表可不可信 |
| `noise_mix` | `true` | true／false | 訓練時把訓練集的背景片段以 5～20 dB 訊噪比混進每個訓練集關鍵字片段，各多產生 2 份（`AUDIO_NOISE_MIX_COPIES`／`AUDIO_NOISE_MIX_SNR_DB`）；驗證片段不混。沒有背景類別或背景片段是數位靜音時略過並在進度列說明 | 否（`training`） |
| `background_weight` | `1.0` | 0.25 ~ 4 | 背景類的 `class_weight` 倍率；誤觸發多就調高 | 否（`training`） |
| `background_class_id` | `""` | 類別 id | 指定「沒有人說話」的類別；空字串時用名稱關鍵字比對（`background`、`silence`、`noise`、`背景`、`安靜`…） | 是（韌體用它做抑制與 trigger margin 基準） |
| `dropout` | `0.2` | 0 ~ 0.6 | 過擬合時調高 | 否（`training`） |
| `detection_threshold` | `0.5` | 0.05 ~ 0.99 | 觸發門檻。只改判定，不必重訓權重 | 是（`runtime`，燒成韌體常數） |
| `mel_bins` | `40` | 40 或 64 | 頻率解析度。64 比較細，但模型輸入與 C 表都會變 | **是**（`frontend-contract`） |
| `sample_rate` | `16000` | 上板固定 16000 | 契約值 | **是**（`MCU_AUDIO_FRONTEND_LOCK`） |
| `clip_seconds` | `1.0` | 1／1.5／2／3／4／5 秒，電腦與三塊板子同一份清單（`AUDIO_CLIP_SECONDS_CHOICES`） | 每個片段與分析視窗的長度。單一關鍵字 1 秒夠用，短句或聲音事件 2～3 秒；越長同一段錄音切出的片段越少（20 秒錄音：1 秒 20 個、5 秒 4 個）。**專案已有音訊樣本後不能改**（存好的片段是用舊長度切的） | **是**（`frontend-contract`，不在 `MCU_AUDIO_FRONTEND_LOCK` 裡；韌體從 `audio_frontend.json` 推出 CLIP_SAMPLES／FRAME_COUNT／COOLDOWN_HOPS，上限 5 秒是 SRAM 量測結果） |
| `window_ms`／`hop_ms` | `25.0`／`10.0` | 上板固定 25／10 | 與 `clip_seconds` 一起決定 frames = 1 + (clip_seconds × 16000 − 400) ÷ 160（1 秒 98、2 秒 198、5 秒 498） | **是**（同上） |
| `fft_size` | `512` | 上板固定 512 | 決定頻譜 bin 數 257 | **是**（同上） |
| `fmin`／`fmax` | `20.0`／`8000.0` | 上板固定 20／8000 | mel 濾波器的範圍 | **是**（同上） |
| `db_floor` | `-80.0` | 上板固定 -80 | 動態範圍下限 | **是**（同上） |
| `epochs`／`batch_size`／`learning_rate`／`validation_split` | 40／16／0.001／0.20 | 1-300／1-128／1e-6-0.1／0-0.45 | 常見訓練參數 | 否 |
| `early_stopping` | `true` | true／false | 驗證分數不再進步就停 | 否 |
| `minimum_samples_per_class` | `8` | — | 每類片段不足時 Train 拒絕並說明 | 否 |
| `deployment_target` | `pc` | `pc`／`NuMaker-M55M1`／`NuGestureAI-M55M1`／`NuMaker-VoiceAI-M55M1` | 不影響學習，但會把上表的前處理幾何鎖死（`clip_seconds` 除外：板子上同樣可選 1～5 秒） | **是**（`lock`） |

### 韌體端的執行參數

`nuteach/config.py` 的 `KWS_RUNTIME_DEFAULTS`（由 `nuteach/mcu/kws_codegen.py` 的
`kws_tokens()` 換成韌體常數）：

| 參數 | 預設 | 意思 |
|---|---|---|
| `inference_hop_seconds` | `0.25` | 每 0.25 秒推論一次，同一個關鍵字會被約 4 個重疊視窗評分 |
| `smooth_windows` | `4` | 平滑視窗數 |
| `required_hits` | `3` | 連續幾個 hop（每 0.25 秒一次）同一個標籤都通過門檻才觸發，中途任一 hop 失敗就重數（不可大於 `smooth_windows`）；觸發後也要連續同樣多個背景 hop 才會重新武裝 |
| `trigger_margin` | `0.20` | 要贏過背景類多少才算數 |
| `rearm_score` | `0.40` | 分數掉回這個值以下才重新武裝 |
| `rms_gate_dbfs` | `-55.0` | 比這個安靜就不推論 |
| `log_epsilon` | `1e-10` | 與訓練前處理相同的功率下限，**不可更改**（`kws_codegen.py` 會擋） |

## 怎麼跑

```bat
rem 只看設定
.venv\Scripts\python.exe reference\audio_kws\train.py --project "我的聲音專案" --dry-run

rem 訓練並調參
.venv\Scripts\python.exe reference\audio_kws\train.py --project "我的聲音專案" ^
    --augmentation-level strong --spec-augment true --background-weight 2.0

rem 看一段 WAV 變成什麼張量，順便存成 PNG
.venv\Scripts\python.exe reference\audio_kws\preprocess.py --wav yes.wav --png yes.png

rem 用 INT8 模型辨識
.venv\Scripts\python.exe reference\audio_kws\run_tflite.py ^
    --model audio_classifier_spectrogram_int8.tflite --labels labels.txt ^
    --frontend audio_frontend.json --input yes.wav

rem 印出韌體內嵌的 C 表（也可以 --out kws_tables.h）
.venv\Scripts\python.exe reference\audio_kws\mcu_tables.py --frontend audio_frontend.json
```

## 對應的程式

| 這裡做的事 | Studio 的程式 |
|---|---|
| 讀 WAV、算 log-mel、畫縮圖 | `nuteach/audio_frontend.py`：`read_wav_file()`、`log_mel_spectrogram()`、`mel_filterbank()`、`spectrogram_thumbnail()`、`AudioFrontendConfig`、`config_from_mapping()` |
| 分派訓練 | `nuteach/training_dispatch.py`：`train_project()` |
| 真正的訓練 | `nuteach/audio_pipeline.py`：`train_audio_project()`、`_make_dataset()`、`background_class_index()`、`write_audio_runner()` |
| 參數預設與驗證 | `nuteach/config.py`：`AUDIO_DEFAULTS`、`validate_audio_settings()`、`MCU_MEL_BINS`、`MCU_AUDIO_FRONTEND_LOCK`、`KWS_RUNTIME_DEFAULTS` |
| C 表與韌體常數 | `nuteach/mcu/kws_codegen.py`：`kws_tables()`、`kws_tokens()`、`c_array()`、`float_literal()` |
| 上開發板 | `nuteach/mcu/deploy_service.py`：`run_deploy()`（Vela → flash 預算 → 組專案 → GCC） |
