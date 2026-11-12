# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 本檔案以中文記錄。專案為 **NuTeach AI Studio v2.1.1 — Windows Only**：
> 在 Windows 本機瀏覽器完成「收集樣本 → 訓練 → Preview → 匯出 TFLite → 建成 M55M1 韌體」的教學工具。
>
> 回覆與工作紀錄一律使用繁體中文；程式中的既有命名、API 欄位與使用者可見的英文文案依專案現況維持一致。

## 常用指令

所有指令都用專案內的 `.venv`，不要用系統 Python：

```bat
rem 學生入口（唯一兩個 BAT，測試會強制檢查）
01_INSTALL.bat            建立/更新 .venv、安裝套件、跑真實 TF + 嚴格 INT8 驗證
02_START.bat              啟動 http://127.0.0.1:8765 並開瀏覽器

rem 開發用
.venv\Scripts\python.exe -m pytest                                   全部測試
.venv\Scripts\python.exe -m pytest tests/test_api.py -v              單一檔案
.venv\Scripts\python.exe -m pytest tests/test_api.py::test_health_and_project_crud   單一測試
.venv\Scripts\python.exe -m pytest -k "audio and not distribution"   關鍵字篩選
.venv\Scripts\python.exe -m pytest -m "not slow"                     跳過會真的建 encoder／訓練的端到端測試

.venv\Scripts\python.exe scripts\start_local.py --check              只 import/建立 FastAPI app 後離開
.venv\Scripts\python.exe scripts\start_local.py --no-browser --port 8766
.venv\Scripts\python.exe scripts\verify_install.py                   .keras 存讀 + 嚴格 INT8 端到端驗證
node --check web\app.js                                              前端語法檢查（測試也會跑，缺 node 則跳過）

rem MCU（韌體建置需要 Arm GNU Toolchain；缺工具鏈的測試會自己 skip）
.venv\Scripts\python.exe scripts\download_arm_toolchain.py           學生用：下載 14.2.rel1 + 驗 SHA-256 → runtime\arm-gnu-toolchain\
.venv\Scripts\python.exe scripts\vendor_mcu_toolkit.py manifest      維護者用：重寫 mcu_toolkit\manifest.json
rem 其餘子命令：toolkit --numl-root / cdc --app-builder-root / image-templates / known-sound / kws-template / licenses
```

`.venv` 損壞時：關閉服務 → 只刪除 `.venv` → 重跑 `01_INSTALL.bat`。**絕對不要刪 `workspace/`**（學生的圖片、錄音、模型都在裡面）。

## 架構總覽

```
瀏覽器 (web/) ──HTTP──> FastAPI (nuteach/app.py) ───> ProjectStore ──> workspace/projects/<id>/
                                    │                                     ├─ samples/<class_id>/
                                    ├─ JobManager (單一 worker thread)      ├─ thumbs/<class_id>/
                                    │    ├─ train  → training_dispatch     ├─ project.json
                                    │    ├─ export → export_service        └─ models/
                                    │    │               └─ subprocess: nuteach.conversion_worker
                                    │    └─ deploy → nuteach/mcu/deploy_service
                                    │                    └─ subprocess: vela-5_1_0.exe / arm-none-eabi-*
                                    │                       （樣板與 BSP 來自 mcu_toolkit/，
                                    │                         產物落在 models/mcu/<board>/）
                                    └─ ModelRuntime (Preview 用的 .keras / .tflite 快取)
```

### 關鍵設計決策（改動前務必理解）

**1. Train 與 Export 是兩個獨立 Job。**
訓練只產出 `.keras`（`training.state = "trained"`，Preview 立刻可用）。TFLite 只在按下 Export Model 時才產生。這是 v2.0.3 「訓練卡在 71%」問題的修正，`tests/test_distribution.py` 會斷言 pipeline 內不得出現 `convert_all(`。

**2. TFLite 轉換一律走「SavedModel + 獨立子行程」。**
`export_service._run_conversion_worker()` 用 `subprocess.Popen([sys.executable, "-m", "nuteach.conversion_worker", spec.json])`，透過檔案交換 spec / progress / result；主服務只輪詢 `progress.json` 心跳並顯示經過時間，逾時（`NUTEACH_EXPORT_TIMEOUT`，預設 1200 秒）就終止子行程。
禁止使用 `TFLiteConverter.from_keras_model`——必須先 `export_inference_saved_model()` 寫出明確 `serving_default` signature 再 `from_saved_model`。測試會斷言 `from_keras_model(` 不存在。
`progress.json` 只是顯示用：成功與否看子行程的結束碼與 `result.json`，所以 `ProgressHeartbeat` 的寫入一律排隊（`_write_lock`）且吞掉 `OSError`——Windows 不允許覆蓋一個正被別人開著的檔案，心跳執行緒、進度回呼與主服務的讀取會互撞（`tests/test_json_write_contention.py`）。

**3. 嚴格整數量化是硬性條件，不是盡力而為。**
`convert_selected()` 產生 int8/uint8 後會 `inspect_tflite()` 檢查 integer I/O、無 float tensor、無 Flex op；`strict_full_integer` 不成立就直接 `raise ExportError`，不會把混合 Float 模型當成 INT8 交出去。

**4. 任何樣本／設定變動都會作廢已訓練模型。**
`ProjectStore._invalidate_training()` 會清空 `training` 區塊並 `rmtree` 整個 `models/`。新增/刪除樣本、改 class、改 settings 都會呼叫它。訓練中的專案由 `_ensure_editable()` 擋住編輯。
在檔案總管手動刪掉的樣本檔也算：專案沒有 job 時，`GET /api/projects/{id}` 會經 `prune_missing_samples()` 把它們從 project.json 移除並作廢模型；`set_training_started()` 也會先丟掉找不到檔案的樣本。

**5. Windows CPU 是唯一 runtime，且是刻意的。**
`runtime_config.normalize_runtime_config()` 會強制覆寫舊版 config 為 `windows_tensorflow_cpu`，`apply_runtime_environment()` 設定 `CUDA_VISIBLE_DEVICES=-1` 與 thread 上限。原始碼中不得出現 `wsl.exe`、`nvidia-smi`、`tensorflow[and-cuda]`（測試強制）。

**6. `TF_USE_LEGACY_KERAS=1` 必須在 import tensorflow 之前設定。**
`app.py`、`scripts/start_local.py`、`02_START.bat`、conversion worker env 都各自設一次。所有 pipeline 都 **延遲 import tensorflow**（放在函式內），讓不需要 TF 的測試能快速跑完。

**7. 存模型只用 `model.save(path.keras)`，不得傳 `include_optimizer`。**
原生 `.keras` writer 不接受該 legacy 參數，會在訓練跑完後才炸掉。統一走 `training_common.save_native_keras_model()`。

**8. `known_sound` 是 multi-label，`abnormal_sound` 是 open-set；兩者不可互相取代。**
`known_sound`（Known Sound Project）= frozen YAMNet encoder + `Dense(n, sigmoid)` head，**命名**聲音，
每類分數獨立、**不加總為 1**，UI 與匯出 metadata 都必須這樣寫，且一律稱「信心分數」不稱「機率」。
`abnormal_sound` 只回答偏離程度，永遠不命名。兩者的資料語意也相反：前者每個類別平等且都要收，
後者只收 Normal。細節見 `docs/KNOWN_SOUND_zh-TW.md`。

**9. `known_sound` 的驗證一律 session-disjoint，這是它存在的主要理由之一。**
`known_sound_pipeline.split_sessions()` 以 `recording_session_id` 分組，同一段錄音切出的 clips
**永不**跨越 train/validation，而且沒有開關。`audio` kind 預設也依場次切分（設定鍵
`session_disjoint_validation`，可關閉；某一類只有一個場次時該類退回 clip-level，並記在訓練報告的
`dataset.split_warning`）。clip-level 的 `training_common.stratified_path_split()` 會讓相鄰的近重複片段
同時進訓練與驗證，報出來的 accuracy 系統性虛高，新的音訊 kind 不要再用它。

**10. MCU 建置只走 GCC，由 Python 直接驅動 `arm-none-eabi-gcc`。**
`nuteach/mcu/gcc_build.py` 自己讀 progen 的 `tools/records/*.yaml` 組出編譯／連結命令，
再用 `subprocess.run(..., cwd=...)` 呼叫 `arm-none-eabi-gcc/ld/objcopy/size`。
**不用** Keil／`UV4.exe`、**不用** GNU make、**不用** project-generator、**不 import**
NuML 的 `numl_tool.py`／`project_*.py`，也不使用 `os.chdir()`
（`tests/test_distribution.py::test_studio_never_touches_keil_or_make` 逐字串斷言）。
工具鏈**不打包**：`nuteach/mcu/toolchain.py` 依序找 `NUTEACH_ARM_GCC_BIN` → `runtime_config`
→ PATH → `runtime/arm-gnu-toolchain/bin` → Program Files。

**11. MCU 端的常數一律生成，禁止手動同步。**
韌體裡的標籤（`Model/Labels.cpp`）、模型陣列、`ACTIVATION_BUF_SZ`、量化參數、KWS 的
Hann／mel 稀疏表、Known Sound 的每類門檻與背景索引，全部在部署當下從
`training_report.json`（`settings`）／`conversion_report.json`／`audio_frontend.json`／
`yamnet_frontend.json`／專案 classes 與 Vela summary 生出來。
`nuteach/mcu/contract.py` 是唯一入口，`kws_codegen.kws_tables()` 直接呼叫訓練用的
`audio_frontend.mel_filterbank()`——所以 PC 與板子不可能算出不同的頻譜。
看到「在 C 檔案裡寫死一個模型常數」的修改，先確認它為什麼不能從契約生成。

**12. `deployment_target` 是訓練設定，不是匯出選項。**
它和其他設定一樣，改動就會 `_invalidate_training()`（模型與 `models/mcu/` 一起沒了）。
選了板子之後 `validate_*_settings()` 會鎖住會改變韌體輸入契約的設定
（`MCU_IMAGE_SIZES`、`MCU_AUDIO_FRONTEND_LOCK`、`KNOWN_SOUND_BOARD_MAX_DEPTH`），
部署時 `contract.validate()` 再檢查一次（專案可能先以 `pc` 訓練完才選板子）。
audio 的 `clip_seconds`（取樣長度）**不在鎖裡**，而是 `AUDIO_CLIP_SECONDS_CHOICES`
（1／1.5／2／3／4／5 秒，電腦與三塊板同一份）。上限 5 秒是 2026-09-19 用內附 Vela 實測出來的：
KWS 韌體的環形緩衝、clip 副本與 float log-mel 暫存跟 tensor arena 同住 1 MiB SRAM01，5 秒時
arena 485,376 B + 緩衝 404,832 B ≈ 892 KB；10 秒光緩衝就 805 KB，放不下。專案一旦有音訊樣本
就不能再改 `clip_seconds`（`ProjectStore._ensure_clip_seconds_changeable()`），因為片段檔已依原
長度切好、原始錄音不保留。部署時 `deploy_service.arena_budget_bytes()` 會用
`kws_codegen.kws_sram_buffer_bytes()` 把這些緩衝從 Vela 的 arena 預算扣掉，clip 太長時 Vela
會自己退到 Size 模式而不是在連結時才爆。

**13. 設定鍵只有一個來源。**
`config.py` 的 `<KIND>_DEFAULTS`（經 `DEFAULTS_BY_KIND` 註冊）與 `validate_<kind>_settings()`；
Advanced 面板每個控制項對應恰好一個鍵，訓練時寫進 `training_report.settings`。每個鍵的意義、範圍與
錯誤訊息寫在使用手冊第 5 章，新增或修改設定時兩本手冊要一起改。Image／Audio 匯出時 INT8 與 Float 的
top-1 一致率低於 95%（`export_service.INT8_AGREEMENT_MINIMUM`）只加警告、不擋匯出；`known_sound`
（`KNOWN_SOUND_PARITY_LIMITS`）與 `abnormal_sound`（`YAMNET_INTEGER_PARITY_LIMITS`）的 parity 則是硬性的。

### MCU 部署（v2.1.0）

```
mcu_toolkit/                     vendored，學生不改（`pyproject.toml` 的 ruff extend-exclude）
  boards.json                    板子註冊表（flash/SRAM/DMIC/深度上限/燒錄方式）
  manifest.json                  來源版本 + 每個檔案的 SHA-256
  NOTICE_third_party.md, LICENSES/, README_zh-TW.md   授權與「相對上游的修改」清單
  NuML_TFLM_Tool/                codegen、板子樣板、修剪過的 M55M1BSP 子集
  vela/vela-5_1_0.exe            Ethos-U55 編譯器
  apps/                          每 kind 的韌體來源（main.cpp.in、CDC、known_sound frontend）

nuteach/mcu/                     Studio 這一側；全部延遲 import，create_app() 不碰 mcu_toolkit
  boards / toolchain / contract / vela / codegen / kws_codegen /
  project_builder / gcc_build / flash / reports / housekeeping / deploy_service
  apps/{image,known_sound,kws}.py   每 kind 的 main.cpp 與來源 overlay
```

- **deploy job 的順序**（`deploy_service.run_deploy()`）：
  `ensure_export_artifacts(['int8'])` → `contract.collect/validate` → `vela.compile_model`
  （Performance，arena 超預算改 Size）→ flash 預檢（Vela flash + 256 KiB ≤ 2 MiB）→
  `project_builder.assemble` → `gcc_build.build` → 打包成
  `models/mcu/<board>/{firmware.bin, source.zip, README_FLASH_zh-TW.txt, deploy_report.json, …}`。
- **禁止檔案**：`mcu_toolkit/` 與 `reference/` 內不得有 `.bat`／`.uvprojx`／`.uvoptx`
  （`tests/test_distribution.py`），vendoring 腳本的 `verify_forbidden()` 也會擋。
  根目錄永遠只有兩個 BAT。
- **板子可以借用別的板子的 NuML template，並抽換掉不適用的檔案**：一塊沒有自己樣板的板子
  （VoiceAI 借用 GestureAI）用 `boards.json` 的 `extra_sources` / `remove_source_basenames`
  換掉樣板裡不適用的來源檔（例如 `BoardInit.cpp` → `BoardInit_VoiceAI.cpp`）；
  `project_builder._overlay_kwargs()` 把這兩個欄位併進韌體 app 自己的 add/remove 來源清單，
  宣告的移除在清單裡找不到對應檔案會直接報錯，不會悄悄什麼都不做。
- **Known Sound 深度上限**：X 板 12、GestureAI 11 是量測值
  （`config.KNOWN_SOUND_BOARD_MAX_DEPTH`，與 `boards.json` 由
  `tests/test_tunables_config.py` 綁在一起）；depth 13 的 Vela flash 2,066,832 B 加上
  `FIRMWARE_CODE_BASELINE_BYTES`（256 KiB）就超過 2 MiB，三塊板都放不下。VoiceAI 的上限也是
  11，但那是刻意選的判斷，不是量測值：VoiceAI 借用 GestureAI 的 template，同一條算式套在它
  自己身上其實推得出 12，但反過來套回 GestureAI 已經量過的數字也還原不出 GestureAI 現有的
  11——算式本來就解釋不了 GestureAI 的既有選擇，於是讓借樣板的一方對齊出樣板的一方，而不是
  採信一個站不住腳的算式。PC 預設仍是 14。
- **韌體 `.bin` 不進 git**：`workspace/deliverables/` 已在 `.gitignore`，測試產出的板子檔案
  放在那裡。

### 音訊前處理是跨三處的契約

同一套 log-mel 規格必須在三個地方保持位元級一致，改任何一處都要同步：

| 位置 | 用途 |
|---|---|
| `nuteach/audio_frontend.py:log_mel_spectrogram()` | 訓練／Preview／量化校正的實作 |
| `audio_pipeline.write_audio_frontend_reference()` 內嵌字串 | 匯出到 ZIP 的 PC 端 Python 參考實作 |
| `docs/MCU_AUDIO_INT8_zh-TW.md` | MCU 端 C 實作的逐步規格 |

預設：16 kHz mono、1 秒（audio kind 可選 1–5 秒，frames = 1 + (clip_seconds×16000 − 400)/160；abnormal_sound 與 known_sound 固定 1 秒）、25 ms window / 10 ms hop、512-point RFFT、40 mel bins、dB 減去自身最大值後截斷至 `[-80, 0]` 再映射到 `[0,1]`，輸出 shape `(98, 40, 1)`（1 秒時）。模型輸入**不是原始 PCM**。

### 關掉網頁就自動結束（`nuteach/auto_exit.py`）

每個 Studio 頁面每 15 秒 `POST /api/heartbeat`，關閉時用 `sendBeacon` 送 `leaving=true`。`AutoExitMonitor` 在最後一個頁面離開 8 秒後、或完全沒心跳 120 秒後（`NUTEACH_AUTO_EXIT_SECONDS`，0 = 停用；`start_local.py --keep-alive`）要求關機；**有 job 在跑絕不結束**（`JobManager.any_active()`），第一次心跳之前也不會（`--check`、API-only、測試）。`start_local.py` 改用 `uvicorn.Server`，把 `should_exit` 掛在 `app.state.request_shutdown`，uvicorn 正常返回、`02_START.bat` 拿到 0 就關視窗。

### 背景噪音混合是訓練期增強，不進契約

`audio_pipeline.train_audio_project()` 在特徵萃取階段，把**訓練集**的背景片段以 5～20 dB SNR
（`config.AUDIO_NOISE_MIX_SNR_DB`）混進每個**訓練集**關鍵字片段，各多產生
`AUDIO_NOISE_MIX_COPIES` 份（`audio_pipeline.mix_background()`，純 numpy、可測）。驗證片段不混、
噪音不來自驗證場次。設定鍵 `noise_mix` 預設開啟；專案沒有背景類別或背景片段是數位靜音時略過並在
進度列說明，報告記 `noise_mix_requested/applied/added/sources`。動機與量測數字在 `config.py` 該
常數的註解：學生錄音約 25 dB SNR、板子約 11～19 dB，長的全濁音關鍵字在這個落差裡會崩潰。前端、
匯出與 MCU 契約完全不變。

### 音訊收樣有兩種切片模式

`ProjectStore.add_audio_bytes(align=...)`：`grid`（預設）是從 t=0 固定格線切、尾端再補一段，**背景類別只能用這種**，安靜本身就是它要學的內容；`utterance` 是「錄一個字」——一次按鍵一段錄音，`audio_frontend.find_utterance()` 用 10 ms RMS 包絡找出最大聲的那個字（門檻取「噪音底 +10 dB」與「峰值 −30 dB」較大者、150 ms hang-over 跨過字內的塞音間隙），`center_utterance()` 以字的中點置中裁成**恰好一個** clip，沒聽到聲音就 `ProjectError` 不存。前端只對 `audio` kind 的非背景類別提供這個按鈕，錄 `clip_seconds + 1` 秒；每次按鍵是獨立的 `recording_session_id`。板子端不用改：韌體本來就是每 0.25 秒滑動視窗，模型的 GlobalAveragePooling 對字在視窗裡的位置本來就寬容。

### `tests/test_distribution.py` 是架構護欄

這個檔案不測邏輯，而是用字串斷言鎖住上述決策（只有兩個 BAT、BAT 必須是 ASCII + CRLF 無 BOM、Train/Export 分離、SavedModel 轉換路徑、無 WSL/CUDA 痕跡、前端必須含特定 DOM/函式名稱）。修改架構時它會先失敗——先確認變更是否真的該做，再更新斷言。

## 新增一種 project kind 需要動的地方

目前有四種 kind：`image` / `audio` / `abnormal_sound` / `known_sound`。

**先讀 `nuteach/config.py` 的 kind registry**（`PROJECT_KINDS`、`AUDIO_LIKE_KINDS`、`CLASSIFIER_KINDS`、
`MULTI_LABEL_KINDS`、`DEFAULTS_BY_KIND`、`PROJECT_KIND_LABELS`，以及 `normalize_kind()` /
`defaults_for_kind()` / `is_known_kind()` / `is_audio_like()` / `is_classifier()` / `is_multi_label()` /
`allowed_setting_keys()`）。新程式碼一律走 registry，不要再新增字串比較的二選一分支。

五個述詞問的是**不同**問題，不可互相取代：

| 述詞 | 問題 | 目前為真的 kind |
|---|---|---|
| `is_audio_like` | 樣本是不是音訊？（錄音／上傳／切片／縮圖） | audio, abnormal_sound, known_sound |
| `is_classifier` | 模型輸出長度是不是 == class 數？ | image, audio, known_sound |
| `is_multi_label` | 每類分數是否獨立、**不**加總為 1？ | known_sound |
| `is_mcu_deployable` | 有沒有開發板韌體？（`MCU_APPLICATION_BY_KIND` 不是 None） | image, audio, known_sound |

新增下一種 kind 時要改：

- `nuteach/config.py` — 新增 `<KIND>_DEFAULTS` 與 `validate_<kind>_settings()`，並註冊進上述 registry。
  沒有加進 `DEFAULTS_BY_KIND` 的設定鍵會被 `allowed_setting_keys()` 靜默丟棄。
  要不要上開發板由 `MCU_APPLICATION_BY_KIND` 決定（`None` = 只在電腦上跑）
- `nuteach/project_store.py` — `_validate_kind_settings()` 與 `create_project()` 的**顯式** `elif` 分支
  （**不可**留 implicit else：舊版的 else 會讓任何未知 kind 悄悄變成 abnormal_sound 專案）
- `nuteach/app.py` — `CreateProjectRequest.kind` 的 `Literal[...]`、`predict/*` 端點
- `nuteach/training_dispatch.py` — `normalized_kind == "<kind>"` 分支（結尾已 raise，沒有 else fallback）
- `nuteach/export_service.py` — `_model_prefix()`、`_representative_samples()`、runner 與 parity 驗證分支
- `nuteach/tflite_export.py` — `build_model_download()` 的 prefix、support 檔清單、`_input_note()`
- `web/app.js` — `PROJECT_KIND_META`、`isAudioLikeProject()`、`renderTrainingOptions()`、
  `readTrainingOptions()`、`renderClasses()`、preview 端點路由
- `web/index.html` — 首頁 `data-create-kind` 卡片
- `tests/test_distribution.py` — 相關斷言

要讓新 kind 也能部署到開發板，再加上：

- `nuteach/config.py` — `MCU_APPLICATION_BY_KIND` 給它一個 application id
  （`MCU_DEPLOYABLE_KINDS` 由它推導）
- `nuteach/mcu/apps/<app>.py` — `template_path()` / `prepare()` / `overlay()`，
  並註冊進 `project_builder._APP_MODULES`（沒註冊的 application 會以
  「此版本尚未支援 … 韌體應用」被擋在 export 與 Vela **之前**）
- `mcu_toolkit/apps/<app>/` — 韌體來源與 `main.cpp.in`（由 `scripts/vendor_*.py` 產生，
  更新後要重跑 `vendor_mcu_toolkit.py manifest`）
- `nuteach/mcu/contract.py` — 該 kind 的契約欄位與 `validate()` 分支
- `nuteach/mcu/deploy_service.py` — `_result_text()`／`_shrink_advice()` 的分支
  （告訴學生會看到什麼、模型太大時該調哪個設定）
- `web/app.js` — `MCU_FLASH_RESULT` 的對應項

## 慣例

- Python 3.13、`from __future__ import annotations`、ruff line-length 100（`pyproject.toml`）。
- 所有 JSON metadata 寫入走 `utils.atomic_write_json()`（目標檔暫時被開著時會自己重試約 1.6 秒，不要改回裸的 `os.replace`）；讀取專案路徑一律經過 `utils.path_within()` 或 `ProjectStore._project_dir()` 做 traversal 檢查。
- 專案資料夾名稱就是專案 id（新專案是「建立時間 + 8 位 hex」，`_new_project_id()`），不可放專案名稱：Windows 上 TF 2.21 的 SavedModel 與 TFLite Interpreter 都打不開含中文的路徑（實測），名稱只放在資料夾內的【名稱】.txt。
- Windows 路徑限制（中文路徑、MAX_PATH）集中在 `nuteach/path_limits.py`：安裝前檢查、啟動提醒，以及 job／API 錯誤訊息後面接的中文建議（`describe_error()`）。只用 stdlib：`install_local.py` 在 `.venv` 還不存在時就用基礎 Python 匯入它。
- ZIP 解壓一律用 `archive.safe_extract_zip()`（擋 `..`、絕對路徑、磁碟機代號、symlink、2 GiB 膨脹）。
- `mcu_toolkit/` 是 vendored 內容，不要直接改：改 `scripts/vendor_*.py`（或 `scripts/vendor_patches/`）重新產生，
  再跑 `vendor_mcu_toolkit.py manifest` 更新 `manifest.json`（`tests/test_mcu_vendoring.py` 逐檔比對 SHA-256）。
- 錯誤訊息會直接顯示給學生：`ProjectError` / `TrainingError` / `ExportError` / `ArchiveError` / `DeployError` 都在 `app.py` 被對應到 HTTP 400。MCU 這條線上的訊息一律繁體中文，而且要指出「該改哪一個設定」。
- 診斷 log（`/api/diagnostics/download`）只含 metadata，**絕不可**放入圖片或錄音內容。
- `logs/LATEST.log`（服務）與 `logs/LATEST_INSTALL.log`（安裝）是排查第一站。
- 文件分工：給學生的內容只寫在 `使用手冊_zh-TW.md` 與 `USER_GUIDE_en.md`（兩本內容對等，改一本就要改另一本），
  `README.md` 只是入口；`docs/` 只放設計／契約文件（`MCU_DEPLOY`、`MCU_AUDIO_INT8`、`KNOWN_SOUND`、
  `ABNORMAL_SOUND_YAMNET`）與 `RELEASE_NOTES_zh-TW.md`；授權與技術來源在 `NOTICE.md`。
  不要再另開快速開始、疑難排解之類的獨立文件。
