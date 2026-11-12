# v2.1.1 發行說明 — Windows Only

## 產品更名：NuTeach AI Studio

- **工具改名為 NuTeach AI Studio**（原名 Teachable Machine Local Studio），避免與 Google 的
  Teachable Machine 混淆。網頁標題、啟動／安裝視窗、手冊、匯出的模型說明與韌體來源註解都改用新名稱；
  網頁左上角換成 Nuvoton logo（`web/nuvoton_logo.png`），README 與兩本手冊開頭也放了同一個 logo。
  首頁底部原本的 Google 免責聲明拿掉，法律聲明統一留在 `NOTICE.md`。
- **程式套件從 `tm_local/` 改名為 `nuteach/`**，環境變數前綴從 `TM_LOCAL_*`／`TM_*` 改成
  `NUTEACH_*`（例如 `NUTEACH_WORKSPACE`、`NUTEACH_EXPORT_TIMEOUT`、`NUTEACH_ARM_GCC_BIN`）。
  舊名稱的環境變數不再讀取；有在教室電腦設定過的，請改用新名稱。
- **建議的解壓位置改成 `C:\NuTeach AI Studio`**（路徑裡的空白不影響安裝、訓練與韌體建置）。
- 既有專案不受影響：`workspace/` 的資料格式沒有變，搬過去照樣開得起來。瀏覽器記住的介面語言與
  麥克風選擇因為儲存鍵改名，第一次開啟時會回到預設值。

## 開發板實測後的修正（2026-11-12）

- **VoiceAI 的 pyocd 燒錄等待上限從 300 秒放寬為 20 分鐘**（`flash.PYOCD_TIMEOUT_SECONDS`）。
  pyocd 經 Nu-Link2 實測約每秒 3 KB：關鍵字韌體（0.27 MB）96 秒，Known Sound 韌體（1.46 MB）
  479 秒，原本的 300 秒一定逾時。燒錄中的提示也改成提醒「可能要好幾分鐘」。
- **範例專案 Sound Events Demo 改用 Encoder 深度 11**（原本 14 只能在電腦上跑），三塊板都建得起來，
  和教學簡報一致。
- 實機確認（GestureAI、VoiceAI 各接一個 Nu-Link2）：Image 上 GestureAI、關鍵字與 Known Sound 上兩塊板，
  燒錄後都持續在執行推論，NPU 每一輪都有被呼叫。

## 本版重點：Audio 取樣長度可調、錄一個字、關閉網頁自動結束、KWS 觸發條件、背景噪音混合

- **KWS 韌體的觸發條件從「連續 2 個 hop 同意」改成「連續 3 個」**
  （`config.KWS_RUNTIME_DEFAULTS["required_hits"]`，`smooth_windows` 仍是 4，上限檢查不變）。
  原因是韌體的重新武裝機制：觸發之後 `s_triggerArmed` 會被設成 false，並且靜音一個片段長度的
  冷卻期，所以**第一個湊滿 required_hits 的標籤就吃掉整次發話**，排在它後面的真正關鍵字完全
  不會觸發。而一個字滑進分析視窗的過程中，分數會先經過一段「只有半個字」的階段，那段時間傾向
  判給兩個關鍵字裡**比較短**的那一個，這個假象只持續 1～2 個 hop，在 2 的門檻下剛好足夠搶走觸發。
  2026-09-29 在一個三類別專案（背景 + 0.62 秒的詞 + 0.92 秒的詞）上，用 58 次真實發話 × 3 個
  隨機種子完整重放 `NuML_ProcessKwsScores`（含重新武裝與冷卻）量測：

  | required_hits | 短的詞會觸發 | 長的詞會觸發 | 誤觸發另一個詞 |
  |---|---|---|---|
  | 2（原本） | 97% | 64% | 20% |
  | 3（現在） | 95% | 92% | 6% |

  代價是延遲多一個 hop（預設 0.25 秒），觸發後也要多一個背景 hop 才會重新武裝（韌體的
  s_backgroundHits 用同一個常數）。**只對更新後重新訓練的專案生效**：更新前已訓練的 Audio 專案，
  training_report.json 裡仍記著訓練當時的 required_hits=2，要重新 Train 再 Export／建置韌體才會拿到 3
  （板子開機橫幅的 `KWS trigger: ... hits=` 可以確認）。另外量測過但**不採用**的兩個方向：再要求贏家比對手高出
  0.20（觸發率掉到 79～86%），以及在訓練時加時間伸縮增強讓兩個詞的長度分布重疊（截斷字的辨識
  確實變好，但端到端只是把錯誤從一個詞搬到另一個詞，誤觸發沒有減少）。
- **Audio（關鍵字）訓練新增「背景噪音混合」（`noise_mix`），預設開啟。** 訓練時把專案自己的背景
  片段以 5～20 dB 訊噪比混進每個關鍵字片段，各多產生 2 份（`config.AUDIO_NOISE_MIX_COPIES`／
  `AUDIO_NOISE_MIX_SNR_DB`），只用訓練集的背景、只加在訓練集的關鍵字上，驗證片段不混。動機是
  量測到的訊噪比落差：學生錄音約 25 dB SNR（底噪約 −53 dBFS），板子實際運作約 11～19 dB；
  在這個落差裡，較長、全濁音的關鍵字會崩潰而短的、有摩擦音的不會。2026-09-30 在同一個三類別
  專案上 4 折交叉驗證 × 2 種子，測試噪音取自訓練沒用到的另一半背景錄音：

  | 講較長的詞 | 乾淨 | 20 dB | 15 dB | 10 dB SNR |
  |---|---|---|---|---|
  | 只有特徵層增強（原本） | 99% | 89% | 38% | 5% |
  | 混入背景噪音 | 98% | 100% | 99% | 95% |

  原本的 `noise` 增強是在正規化後的 log-mel 特徵上加高斯雜訊，前端會先減掉片段自己的最大值，
  所以那種雜訊改變不了頻譜形狀，混進波形的真實房間聲才會。專案沒有背景類別（或背景片段是數位
  靜音）時會略過並在進度列說明，訓練報告記錄 `noise_mix_requested`／`noise_mix_applied`／
  `noise_mix_added`／`noise_mix_sources`。匯出與板子契約完全不變。
- 順帶提醒的資料陷阱（不是程式修正）：板子跑 KWS 韌體時不是 USB 麥克風，瀏覽器會默默錄到全零
  （rms −100 dBFS）；「錄一個字」會拒絕這種全零的錄音，「連續錄音」不會檢查。要再收音請先燒回收音
  韌體，並確認瀏覽器選到的是「GestureAI DMIC Mic」。

- Audio（關鍵字）專案新增訓練設定「取樣長度」（`clip_seconds`）：可選 **1／1.5／2／3／4／5 秒**
  （`config.AUDIO_CLIP_SECONDS_CHOICES`），預設仍是 1 秒；電腦與三塊板子（X 板、GestureAI、VoiceAI）
  都是同一份清單。`clip_seconds` 已從 `MCU_AUDIO_FRONTEND_LOCK` 移除，鎖定清單保留
  sample_rate／window_ms／hop_ms／fft_size／fmin／fmax／db_floor。Known Sound 與 Abnormal Sound
  維持固定 1 秒。
- 模型輸入的 frame 數跟著變：frames = 1 + (clip_seconds × 16000 − 400) ÷ 160，1 秒 98、1.5 秒 148、
  2 秒 198、3 秒 298、4 秒 398、5 秒 498（輸入 `[1, frames, 40, 1]`）。韌體的 CLIP_SAMPLES／
  FRAME_COUNT／COOLDOWN_HOPS 由 `audio_frontend.json` 生成；推論 hop 仍是 0.25 秒、平滑仍是 4 個
  視窗，板子上每個關鍵字還是每 0.25 秒評分一次，偵測到之後的冷卻時間等於一個片段長度。
- 上限 5 秒是量測結果（2026-09-19，內附 Vela 5.1.0，40 mel、3 類）：韌體的環形緩衝區、片段複製、
  float log-mel 暫存跟 Ethos-U tensor arena 全在同一塊 1 MiB 的 SRAM01。arena（Performance）
  1 秒 101,376 B、2 秒 197,632 B、3 秒 293,888 B、5 秒 485,376 B；音訊緩衝區 1 秒 84,832 B、
  每多 1 秒多 80,000 B（5 秒 404,832 B）；5 秒時 SRAM01 合計約 892 KB／1,048,576 B。10 秒光音訊
  緩衝區就要約 805 KB，再加上 ≥ 250 KB 的 arena，放不下。GestureAI 上前處理的 CPU 時間是每秒
  片段 29.2 ms／每個 hop（5 秒約 148 ms，仍在 250 ms 的 hop 內）。Flash 不是瓶頸（INT8 模型 30 KB、
  1 秒的韌體約 269 KB）。
- **專案一旦有了音訊樣本就不能再改取樣長度**（存好的片段是用舊長度切的）：畫面上的控制項會變灰，
  API 回「這個專案已經有音訊樣本，不能再更改取樣長度（clip_seconds）…請先清除全部音訊樣本，
  或另開一個新專案」。改動它跟其他設定一樣會作廢已訓練的模型。
- 部署 Audio 韌體時若 tensor arena 超過板子預算，錯誤訊息現在會建議「縮短取樣長度或調低 Mel Bins」。
- 給學生的建議：單一關鍵字用 1 秒（「nuvoton」約 0.7 秒）；短句或聲音事件用 2～3 秒。「字被 1 秒
  邊界切成兩半」是固定格線切片造成的，拉長片段只能減少、不能根除——關鍵字類別改用下面的
  「錄一個字」，或連續錄音時重複之間留 ≥ 1 個片段長度的安靜，才是可靠的解法。片段越長訓練資料越少：20 秒錄音
  在 1 秒切出 20 個片段，5 秒只有 4 個，而每類至少要 8 個片段。
- Audio（關鍵字）專案的關鍵字類別新增主要按鈕「錄一個字（N 秒）」（N = 專案的取樣長度）：按一下，
  畫面提示「請說：<類別名>」，Studio 錄 clip_seconds + 1 秒（1 秒片段錄 2 秒），伺服器依能量包絡
  （10 ms RMS frame；門檻 = max(底噪 + 10 dB, 峰值 − 30 dB)；150 ms hang-over，字內的爆音空隙不會
  把一個字切成兩段）找出最大聲的那一個字，**置中**後存成**剛好一個**片段，字前後用同一次錄音的房間聲
  填滿（只有錄音比片段短時才補零）。動機：固定格線從 t=0 切片會在任意位置切斷字（「nuvoton」變成
  「nu」／「voton」），而從按下按鈕起固定錄一個片段長度又會被學生的反應時間（≈0.3～0.8 秒）吃掉字尾；
  2026-09-19 在維護者的專案上量測，連續 20 秒錄音切出的「on」片段有 58% 字尾貼著片段結尾、39% 字頭
  貼著片段開頭。置中後兩個問題都消失。
- 沒有聽到聲音的一次（峰值低於 −45 dBFS，或沒有比底噪高 10 dB）**不會**加入樣本，toast 顯示
  「沒有聽到聲音（峰值 … dBFS），這次沒有加入樣本；請靠近麥克風、按下按鈕後再說一次。」。
  **每按一次就是一次獨立錄音場次**（session-disjoint 驗證改為依按下次數切分），建議每個關鍵字
  按 30 次以上並變換語氣、距離、音量。舊的「連續錄音 20 秒」在關鍵字類別上保留為次要按鈕（仍是固定
  格線切片），而且是**背景類別唯一的模式**——背景要學的就是安靜，不做能量門檻也不置中；對明確的
  背景類別用「錄一個字」會被伺服器拒絕。Known Sound 與 Abnormal Sound 不受影響。
- API：既有的 `POST /api/projects/{id}/classes/{class_id}/audio` 新增 form 欄位 `align`
  = `grid`（預設）｜`utterance`。樣本 metadata 新增 `alignment`（`grid`／`utterance`）、
  `utterance_start_sample`／`utterance_end_sample`（字在片段內的位置）、`utterance_peak_dbfs`、
  `noise_floor_dbfs`、`utterance_truncated`（字比片段還長）。板子韌體不變（本來就每 0.25 秒滑一個
  視窗），模型的 GlobalAveragePooling 讓它不在意字落在片段的哪個位置。
- **收音面板也有「取樣長度」**：Audio（關鍵字）專案按下類別卡片的「Mic」之後，收音面板最上方、
  「錄一個字（N 秒）」按鈕的上面，現在也有同一個「取樣長度」下拉（1／1.5／2／3／4／5 秒），讓學生在
  錄第一個樣本之前就看到它。它跟 Training → Advanced 裡的是同一個設定，改任一邊另一邊都會跟著變；
  專案一旦有了音訊樣本就變灰並顯示「已有樣本，取樣長度固定為 N 秒；清除全部樣本後才能更改。」。
  建議順序：先決定片段長度，再錄背景與關鍵字。
- **關閉網頁後自動結束**（`nuteach/auto_exit.py`）：每個 Studio 網頁每 15 秒送一次
  `POST /api/heartbeat`，關閉時再用 beacon 送一次 `leaving`。最後一個 Studio 網頁關掉約 8 秒後，
  主控台程式（`02_START.bat` → `scripts/start_local.py`）會自己結束；完全收不到心跳 120 秒
  （瀏覽器當掉、分頁被丟棄）也會結束。訓練、匯出、建置韌體進行中絕不結束——工作先跑完，之後沒有人
  回來才結束；重新整理網頁不會觸發；電腦睡眠醒來後重新計時，不會因為睡眠期間沒有心跳就結束。結果：下次雙擊 `02_START.bat` 不會再只因為上一個主控台視窗沒關
  就出現「Port 8765 已被其他程式使用」。要在沒有網頁的情況下持續執行：
  `.venv\Scripts\python.exe scripts\start_local.py --keep-alive`（或 `--auto-exit-seconds N`，
  `0` = 停用；環境變數 `NUTEACH_AUTO_EXIT_SECONDS`）。主控台橫幅的「Stop」那一行會註明這件事；
  `GET /api/health` 新增 `auto_exit`（是否啟用、連線中的網頁數、距上次心跳秒數）。
- 疑難排解文件補充：`netstat -ano | findstr :8765` 通常會列出很多 `TIME_WAIT`／`CLOSE_WAIT`，那是
  已關閉、還在逾時的連線，不是多開的伺服器；只有 `LISTENING` 那一行是 Studio，它的 PID 才是該看的程式。
- **修正：Export Model／建置韌體偶爾以「Conversion worker failed: PermissionError: [WinError 5]
  存取被拒」失敗，模型其實已經轉換完成。** 轉換子行程每秒把進度寫進 `progress.json`，轉換本身
  回報進度時也寫同一個檔，主服務又每 0.75 秒讀它一次；Windows 不允許覆蓋一個正被開啟的檔案，
  兩條執行緒同時覆蓋時也會有一方被拒絕（在這台電腦上實測約四分之一）。只要撞上一次，整個匯出
  就被當成失敗。現在進度檔的寫入會排隊、而且永遠不會讓轉換失敗（成功與否本來就只看子行程的
  結束碼與結果檔）；`utils.atomic_write_json()`——`project.json`、各種報告都走它——在目標檔被讀取
  或被防毒軟體掃描而暫時無法覆蓋時，會重試約 1.6 秒才報錯，轉好的 `.tflite` 換上去時也一樣。
- **修正：`scripts\download_arm_toolchain.py` 對 Arm 官方的真實下載會失敗。** v2.1.0 的腳本下載完約
  305 MB 之後會停在「checksum for … not found in .sha256asc contents」：Arm 實際發佈的校驗檔是
  `<sha256> *<檔名>`（檔名前有星號），而且 14.2.rel1 的 Windows zip 把 `bin\`、`lib\`… 直接放在最上層，
  沒有外層資料夾。現在兩種校驗檔寫法、兩種 zip 版面都接受。安裝流程同時改成「先在旁邊解壓完整，
  再整個換上去」：
  - 重裝時，舊的工具鏈在新的就緒之前不會被動到；裡面有檔案正在使用（例如正在建置韌體）會直接
    說明並停下，不會留下半套。換上去的那一步失敗或被 Ctrl+C 中斷時，舊的工具鏈會立刻放回原位；
    連視窗都被直接關掉的話，下一次執行會先把它放回去，並清掉解壓到一半的暫存資料夾與沒下載完
    的檔案。同一個資料夾裡一次只允許一個安裝在跑：再開一個視窗執行會直接說明並結束，所以這些
    清理不會動到另一個視窗正在進行的安裝。按 Ctrl+C 中斷時，得到的是一句「現在磁碟上是什麼
    狀態」的說明（工具鏈可以用、舊的還沒放回去、或什麼都沒裝），而不是 Python 的錯誤訊息。
  - `--dest` 是工具鏈資料夾本身，必須是新的、空的，或「只裝了工具鏈」的資料夾。最上層或 `bin\`
    裡只要有不屬於工具鏈的東西（例如同時放了其他工具的 `D:\tools`，就算它的 `bin\` 裡也有
    `arm-none-eabi-gcc.exe`）、是磁碟機的最上層、或命令視窗正開在裡面，都會在下載前就拒絕——以前
    會把那個資料夾自己的 `bin\`、`lib\`、`include\`、`share\` 蓋掉。更深的子資料夾不逐一檢查，
    所以要換掉一份既有的工具鏈之前，會先說明整個資料夾都會被換掉。防毒軟體把
    `arm-none-eabi-gcc.exe` 移走之後的資料夾仍然認得出是工具鏈，可以直接重裝。
  - 這些情況也都在下載 305 MB 之前就說明原因：安裝路徑太長（最長的檔案會超過 Windows 的 260 字元
    上限）、磁碟可用空間不足（約需 1.6 GB）、路徑裡有 Windows 不能用的字元（常見的原因是用引號
    包住的路徑結尾多了一個反斜線）、`--dest` 的上一層不能建立檔案（例如 `C:\arm-gnu-toolchain`：
    一般帳號不能直接在 `C:\` 建立檔案，下載檔放不進去）。
  - 驗證先比對腳本內記錄的 SHA-256（Arm 官方公布的值），相符就不必再下載校驗檔；不相符才去抓
    Arm 的 `.sha256asc`。學校或公司的網路把下載換成登入／封鎖頁面時，會直接這樣說明並提示離線
    安裝的做法，不再顯示看不懂的解碼錯誤；這台電腦自己的問題（檔案被別的程式占用、磁碟滿了）
    也不再被說成「連不上 developer.arm.com」。
  - 裝完之後下載檔刪不掉（例如防毒軟體正在掃描）只會提醒；以前會被當成安裝失敗，連剛裝好的
    工具鏈一起刪掉。下載失敗、或網路把下載換成登入頁面時，都不會動到學生自己事先放在同一個
    資料夾的 zip：新下載的檔案要先通過驗證，才會取代它。
  - 解壓後會逐一核對檔案是否完整。防毒軟體在解壓途中把檔案移走時，會指出少了哪一個檔案並保留
    原本的工具鏈，不會換上一份不完整的。
  - 沒有網路的教室：把官方 zip 原封不動放在 `runtime\`（`--dest` 的上一層）再執行腳本，SHA-256
    與釘死的 14.2.rel1 相符就直接使用、完全不連網，也不會刪掉這個檔案；下載失敗時的訊息會說明
    這個做法。下載並驗證過之後才失敗、或被 Ctrl+C 中斷時，壓縮檔同樣會保留（下載到的就是腳本
    記錄的那一份時），排除問題後再跑一次不必重新下載 305 MB。常見的小差錯也有對應的說明：檔名後面多了瀏覽器加的「 (1)」照樣認得；
    壓縮檔放進了 `arm-gnu-toolchain\` 裡面、壓縮檔還在複製中、路徑有空白卻沒用引號包住，都會
    直接說明該怎麼做。
  - 解壓不再經過 `tempfile` 資料夾：那種資料夾只有建立它的帳號讀得到，裝出來的工具鏈在同一台
    電腦的其他 Windows 帳號下會讀不到。
  - 裝在自訂位置、或電腦上另有一份工具鏈排在前面時，結尾會印出設定 `NUTEACH_ARM_GCC_BIN` 的現成指令
    （用 `setx`：只在命令視窗裡 `set` 的變數，雙擊 `02_START.bat` 啟動的 Studio 讀不到）；剛裝好的
    gcc 無法執行（例如被防毒軟體擋下）時會直接這樣說，而不是叫你去設環境變數。

  2026-10-01 以這一版腳本實測：向官方伺服器實際下載 305 MB、SHA-256 相符、覆蓋重裝成功；再用留在
  旁邊的壓縮檔離線重裝一次（不連網，約 18 秒）。另外用縮小的壓縮檔做了三種壓力測試——在每一個
  檔案系統步驟注入中斷或錯誤（六種情境共約 1,700 次）、在隨機時刻中斷（模擬按 Ctrl+C，約 9,000 次）、
  兩到三個行程同時安裝（125 輪）——都沒有出現工具鏈不見、訊息與磁碟上的狀態不符、或 Python
  錯誤訊息。沒有做的：真的按鍵盤上的 Ctrl+C、真的防毒軟體（用「解壓後刪掉檔案」模擬）、網路磁碟。
  裝好的 GCC 14.2.Rel1 跑過自動測試裡的韌體建置（X 板與 GestureAI × Image／Audio／Known Sound），
  並另外建置 VoiceAI 的 Audio 與 Known Sound 韌體，全部成功。
- `nuteach/mcu/toolchain.py`：候選資料夾讀不到（例如另一個 Windows 帳號裝的工具鏈）時，視為
  「這裡沒有工具鏈」繼續往下找，不再讓 `/api/mcu/status` 丟出 PermissionError。
- 「部署到開發板」頁籤與建置時的「缺少／找不到 Arm GNU Toolchain」訊息改成直接給出安裝指令，並說明
  裝好後切換一次頁籤（或再按一次「建置韌體」）就會偵測到；以前叫學生重新執行 `01_INSTALL.bat`，
  那一步其實不需要。（用 `NUTEACH_ARM_GCC_BIN`／`PATH` 指定位置的工具鏈則要重新啟動 Studio，文件與
  下載腳本的結尾提示都補上了這一點。）
- **修正：`mcu_toolkit\` 少了檔案時，訊息叫學生「重新執行 01_INSTALL.bat」，但那修不好。**
  `mcu_toolkit\` 是隨 Studio 一起附的，`01_INSTALL.bat` 只會探測它、不會把它補回來。「找不到
  Vela 編譯器／韌體樣板／板子註冊表」這幾個訊息與部署頁籤的提示，現在一律說明真正有效的做法：
  從 Studio 的 ZIP 把 `mcu_toolkit` 資料夾重新解壓縮、蓋回去（防毒軟體有時會把 Vela 編譯器移走）。
- 修正：Abnormal Sound 專案打開「部署到開發板」頁籤時，除了「此專案類型無法部署到開發板」之外，
  還會多出一行「缺少開發板工具鏈」——其實什麼都沒缺。同一個頁籤在問不到伺服器狀態時，也不再
  把三樣工具都列為缺少，而是說明服務沒有回應。
- 修正：Audio 專案「資料增強」的說明寫「強：再加雜訊混入」。實際上除了「關閉」以外每一級都有
  時間位移、音量變化與少量雜訊，「強」是把三項的幅度都加大；畫面提示與兩份使用手冊已改正。
- **Known Sound 訓練補上固定亂數種子**（`tf.keras.utils.set_random_seed(1337)`，Image 與 Audio
  本來就有）。以前分類頭的初始權重、片段的洗牌順序與 Dropout 遮罩每次都不一樣，同一份錄音、
  同一組設定重新訓練，會得到略有不同的模型與驗證數字；現在在同一台電腦上重新訓練會得到同一個
  模型。2026-10-01 實測：同一個專案用預設設定、以及「Dropout 0.25 + 波形增強」各重新訓練 8 次
  （分散在 4～5 個各自新開的行程裡），分類頭的權重與每個 epoch 的 loss 完全相同；拿掉種子後
  訓練 3 次得到 3 個不同的模型。Abnormal Sound 的訓練沒有隨機步驟，不受影響。已經訓練好的專案
  不必重新訓練。
- 文件修正：序列埠那一行的 `drop` 是開機以來的累計值（比較晚才連上終端機時會停在一個不是 0 的數字，
  不代表故障）；GestureAI／VoiceAI 的 COM port 在按 Reset 時會重新列舉，要在它重新出現後 10 秒內連上
  才看得到開機訊息；Studio 的「燒錄到板子」不能用來燒 `1_Collect_Firmware_bin` 的收樣本韌體；收樣本時
  Studio 沒有攝影機選單（由瀏覽器決定用哪一台）；收音時畫面不會顯示 dBFS。
- **安裝前先檢查 Studio 資料夾的路徑**（`nuteach/path_limits.py`）。路徑含中文時（「桌面」「下載」、中文
  使用者名稱），2026-10-29 實測 TensorFlow 2.21 在 Windows 上寫不出 SavedModel、TFLite Interpreter 也打不開
  模型，匯出與部署一定失敗；Windows 未開啟長路徑支援時，`.venv` 裡最深的 TensorFlow 標頭檔是 187 字元，
  資料夾本身超過 68 字元 pip 就裝不下。`01_INSTALL.bat` 現在遇到這兩種情況會在下載之前就停下、用中文
  說明原因；超過 78 字元（部署會被擋）或 `mcu_toolkit` 少了檔案（Windows「解壓縮全部」略過了太深的
  檔案）則印出 `[WARN]`，`02_START.bat` 啟動時也會提醒。安裝驗證改在 Studio 自己的 `workspace\tmp` 裡做，
  中文使用者名稱的 `%TEMP%` 不再讓安裝失敗。
- **路徑造成的錯誤會說出來**：訓練、匯出、部署失敗，或請求遇到檔案系統錯誤時，只要錯誤裡的路徑達
  240 字元以上、Windows 回報檔名太長，或路徑含中文，訊息後面就會接上「這很可能是 Windows 的路徑長度上限
  造成的……」或「出錯的路徑含有中文……」與處理方式。請求裡的檔案系統錯誤也不再只顯示
  「Internal Server Error」。
- **在檔案總管刪掉的樣本會自動從專案移除**：開啟專案、按「重新整理」或開始訓練時，找不到檔案的樣本
  會從 `project.json` 移除（縮圖一起刪），已訓練的模型作廢，跟按 × 一樣。以前 Known Sound 會在訓練時停在
  `FileNotFoundError`，其他種類則默默略過、畫面上的樣本數卻不變。
- **修正：匯入的 Known Sound 專案無法訓練。** 匯入會把樣本與類別重新編號，檔案卻留在原本的路徑；
  Known Sound 用新編號去組路徑，一個片段都找不到。現在和其他種類一樣依樣本記錄的 `relative_path` 讀檔。
- **音訊樣本的縮圖點一下就播放**（再點一下停止），挑出錄錯的片段不必再開檔案總管。
- **新專案的資料夾以建立時間命名**（例如 `20261029-153045-3f9a2c1d`），在檔案總管依名稱排序就是依建立
  時間排序；專案名稱仍在資料夾裡的 `【專案名稱】.txt`。名稱刻意不放進資料夾名稱，原因同上（含中文的
  路徑 TensorFlow 打不開）。已經存在的專案資料夾不會自動改名。首頁的專案卡片與專案頁上方會顯示資料夾名稱，
  專案頁點一下資料夾名稱就用檔案總管開啟它（`POST /api/projects/{id}/open-folder`）。
- **在 ZIP 裡直接雙擊 `01_INSTALL.bat`**（沒有先解壓縮）時，改成明說要先「全部解壓縮」；以前會誤報
  「Compatible Python was not found」。使用手冊 2.4 也補上下載的 ZIP 解壓後會跳出的 Windows 安全性提示
  怎麼處理（「其他資訊」→「仍要執行」，或解壓前在 ZIP 的「內容」勾選「解除封鎖」）。
- **文件整併**：快速開始、疑難排解、功能對照表與資料夾說明併進兩本使用手冊（2.5、3.4、第 7 章），
  技術來源併進 `NOTICE.md`，架構說明與 `AGENTS.md` 的規則併進 `CLAUDE.md`。移除的檔案：
  `AGENTS.md`、`README_zh-TW.md`、`docs/README_en.md`、`docs/ARCHITECTURE_zh-TW.md`、
  `docs/FEATURE_MATRIX_zh-TW.md`、`docs/QUICK_START_zh-TW.txt`、`docs/TROUBLESHOOTING_zh-TW.md`、
  `docs/SOURCES.md`，以及過期的 `docs/BUILD_VALIDATION.txt`、`docs/MCU_DEPLOY_FINAL_REPORT_zh-TW.md`、
  `docs/PROJECT_MANIFEST_SHA256.txt`。

# v2.1.0 發行說明 — Windows Only

## 新增：部署到 Nuvoton M55M1 開發板

- Export Model 視窗新增「部署到開發板」頁籤：把 Strict INT8 模型建成韌體 `firmware.bin`，
  可下載 ZIP 或直接燒錄。建置是和 Train／Export 同一套機制的背景工作。
- 支援 **Image**、**Audio（關鍵字／聲音分類）** 與 **Known Sound** 三種專案，
  目標板為 **NuMaker-M55M1（X 板）**、**NuGestureAI-M55M1** 與 **NuMaker-VoiceAI-M55M1** 三塊。
  VoiceAI 沒有相機也沒有 LCD，只支援 **Audio** 與 **Known Sound** 兩種聲音專案；
  Image 專案仍只能選 X 板或 GestureAI。
  `Abnormal Sound` 維持電腦上使用，沒有開發板韌體。
- 建置**只用 Arm GNU Toolchain（GCC）**：不需要 Keil、不需要 GNU make、不需要
  project-generator。韌體樣板、M55M1 BSP 子集與 Arm Vela 5.1.0 都內附在 `mcu_toolkit\`。
- **Arm GNU Toolchain 不隨 Studio 打包**，請自行安裝一次；`scripts\download_arm_toolchain.py`
  會下載釘死的 14.2.rel1 並用 Arm 官方的 `.sha256asc` 驗證後解壓到 `runtime\arm-gnu-toolchain\`。
  `01_INSTALL.bat` 會探測工具鏈並寫進 `runtime_config.json`，缺少時只停用部署，不會讓安裝失敗。
- **Nu-Link Command Tool 不打包**，只偵測你自己安裝的版本（X 板燒錄用）。
  NuGestureAI 用 USB 隨身碟 bootloader 拖放燒錄；VoiceAI 沒有板載 Nu-Link，改用外接
  Nu-Link2 走 `pyocd load`（不含 reset 步驟，燒完請自己按一下 Reset），一次性設定見
  `scripts\setup_voiceai_flash.py`。三種燒錄方式都不需要額外購買 Nuvoton 的付費工具。
- 韌體裡所有跟模型有關的常數（標籤、tensor arena、量化參數、KWS 的 mel／Hann 表、
  Known Sound 的每類門檻）都由匯出契約檔生成，不靠人工同步。
- Known Sound 的 `encoder_depth` 上限：**X 板 12、GestureAI 11、VoiceAI 11**（X 板與 GestureAI
  是量測後的 flash 預算結果；VoiceAI 的 11 是刻意保守的判斷值，這塊板從未實際燒錄驗證過）；
  depth 13 以上三塊板都放不下，只能留在電腦上跑。

## 新增：更多可調訓練參數

- 共同：**目標裝置**（電腦／X 板／GestureAI／VoiceAI）。選了板子之後，會改變韌體輸入契約的設定
  （Image Size、音訊前處理幾何、Encoder 深度上限）會被鎖成韌體支援的值。
- Image：資料增強強度、Dropout、MobileNetV2 微調層數（微調會讓訓練時間大約多 50%）。
- Audio：資料增強強度、SpecAugment、**Session-disjoint 驗證（預設開啟）**、背景類別與其權重、
  Dropout、偵測門檻、Mel Bins（40／64）。
- Known Sound：波形增強、分類頭 Dropout、**每一類獨立門檻**、背景類別與其權重、Encoder 深度。
- Image／Audio 匯出後若 INT8 與 Float 模型的 top-1 一致率低於 95%，會顯示黃色提醒（不阻擋匯出）。

## 新增：`reference\` 參考程式資料夾

四種專案各有一份可直接執行的精簡 Python 腳本（訓練／前處理／用匯出的 INT8 模型推論），
Audio 另有 `mcu_tables.py` 可印出韌體實際使用的 mel／Hann 表。
總覽與參數說明見 `reference\README_zh-TW.md`。

## 已知限制與尚未實測的項目

- Image 兩塊板（X、GestureAI）＋ Audio／Known Sound 三塊板（X、GestureAI、VoiceAI）共 8 種
  組合都已在電腦上建置、連結並通過大小檢查，**但都還沒有在真實硬體上跑過**。
- 待實測：非快取記憶體區段的修補、GestureAI 的 DMIC1 channel 2 接線、
  GestureAI 的 UVC + CDC 複合裝置、X 板的除錯 UART 埠位、沒有背景類別時的 KWS 重新武裝規則、
  USB CDC 開機等待、X 板 depth 12 的 Known Sound 韌體、VoiceAI 的 DMIC0（PA.4／PA.5）收音與
  pyocd 燒錄。
- 本版不做：SD 卡 + HyperRAM 載入模型（因此 `encoder_depth ≥ 13` 無法部署）、
  物件偵測（YOLOX-Nano）、Keil 專案輸出、手動指定 tensor arena。
- 詳見 `docs\MCU_DEPLOY_zh-TW.md`。

## 從 v2.0.7 更新

可以保留 `.venv` 與 `workspace`，重新執行 `01_INSTALL.bat` 即可（安裝步驟最後會多一步
MCU 工具鏈探測）。既有專案的行為不變：所有新參數的預設值都等於原本的做法，只有 Audio 的
**Session-disjoint 驗證預設開啟**會讓驗證分數變得比較誠實（通常會比舊版低一些）。

---

# v2.0.7 發行說明 — Windows Only

## 新增 Known Sound Project（YAMNet 分類器）

- 新增第四種 `known_sound` project：frozen YAMNet 1024-D encoder + 可訓練的
  `Dense(n, sigmoid)` head，**會說出聽到的是哪一種聲音**（例如槍聲／狗叫／玻璃破裂）。
- **多標籤**：每類一個獨立分數，**不會加總成 100%**，兩種聲音可以同時被偵測到。
  一律稱「信心分數」，不稱「機率」。
- 驗證改為 **session-disjoint**：同一段錄音切出的 clips 永不跨越 train／validation，
  避免既有 Audio Project 的 clip-level leakage 讓 accuracy 虛高。
- 每類至少 2 個獨立錄音場次、20 個片段，不足會擋下訓練並逐類說明還差什麼。
- **混音增強**：訓練時把兩個不同類別的波形相加，讓模型真的學到同時發生的事件。
- Preview 採 **峰值保持**（預設 1.5 秒）而非時間投票——槍聲只持續 100–200 ms，
  投票會抑制正確偵測。
- 匯出為**單一** TFLite（`96×64 log-mel patch → n 個獨立分數`）加 `labels.txt`，
  strict INT8／UINT8 維持零 float tensor、零 Flex、零 custom op，
  並新增以 `detection_agreement` 為主的分類器 parity gate。
- 新增**錄音健檢**：錄音／上傳後用官方 YAMNet 521 類 head 回報 top-3 標籤，
  純資訊性，用來在收資料當下就抓出「錄到的不是你以為的東西」。
- 新增 **`encoder_depth`**（Training 面板可選）：保留 YAMNet 14 個 block 的前 N 個。
  YAMNet 參數嚴重後段集中，因此這是唯一能有效縮小模型的旋鈕 ——
  完整 14 層 Vela 後約 2.97 MB，depth 10 只有約 1.05 MB，可放進 M55M1 的 2 MB 內部 flash。
  tensor arena 在每個深度都約 151 KB，所以換的是 flash 不是 RAM。
  預設仍是 14，既有專案行為完全不變；非完整深度的匯出檔名會帶 `_dN`。
  各深度的實測大小與準確率見 `docs/KNOWN_SOUND_zh-TW.md`。

## 新增 Abnormal Sound Project（YAMNet）

- 新增第三種 `abnormal_sound` project，不會再落入一般 Audio classifier 分支。
- 使用 SHA-256 固定的官方 YAMNet 權重、frozen 1024-D embedding 與 Normal-only scorer。
- `Normal baseline` 依 recording session 分割；`anomaly_eval` 僅評估，禁止進入訓練、threshold 與量化代表資料。
- 新增 GestureAI／DMIC 麥克風選擇、device settings 與 session metadata 保存。
- Preview 顯示 embedding／RMS anomaly ratio，採 5-window temporal vote；未達 calibration gate 時保持 `Uncertain`。
- Export 為每個 Keras／TFLite runtime 分別建立 reference、threshold、實測 FPR confidence 與 SHA binding。
- Strict INT8／UINT8 會重新檢查實際檔案，拒絕 Float、Flex、custom op、錯誤 I/O dtype 或無來源綁定的舊快取。

## 安裝流程簡化

- 移除所有 Linux／WSL GPU 安裝與提示。
- 不會要求重開機後啟動其他作業系統。
- 不會執行 `wsl.exe`、不會安裝 CUDA，也不會建立 Linux virtual environment。
- 只使用 Windows 64-bit CPython 3.13 與專案內 `.venv`。
- Runtime 固定顯示 `Windows CPU`。

## 移除的檔案

```text
requirements-wsl-gpu.txt
scripts/training_worker.py
docs/GPU_SETUP_zh-TW.md
```

## 保留功能

- Image／Audio 資料收集與訓練。
- PC 即時 Preview。
- 清楚的分類百分比和彩色 bar。
- Float32／Dynamic／strict INT8／strict UINT8 TFLite 匯出。
- Train Model 與 Export Model 分離。
- 單一「下載 Log」。
- 根目錄只有 `01_INSTALL.bat` 與 `02_START.bat`。
- Windows BAT 固定使用純 ASCII、CRLF、無 BOM。

## 舊版更新注意

從 v2.0.5／v2.0.6 更新時，可保留：

```text
.venv
workspace
```

重新執行 `01_INSTALL.bat` 後，舊的 GPU runtime 設定會自動覆寫成 Windows CPU，不需要刪除學生專案。
