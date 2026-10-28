# 主動 ETF 持股雷達

每天 09:00 自動抓取主動式 ETF 的每日持股、現金、附買回、期貨與單位數，連動成分股股價（含日、美、韓、中國 A 股），推算平均成本，比對公開說明書規則，並彙整成網頁儀表板（GitHub Pages）。

目前追蹤：00412A 主動安聯亞洲半導體、00981A 主動統一台股增長、00997A 主動群益美國增長。

## 一次性設定（約 10 分鐘）

1. **建立儲存庫**：GitHub → New repository，名稱例如 `etf-tracker`，選 **Public**（持股是公開資料；你自己的持股只存在瀏覽器，不會進儲存庫）。私人儲存庫要 GitHub Pro 才能開 Pages。
2. **上傳程式**（擇一）：
   - 終端機：
     ```bash
     cd etf-tracker
     git init && git add . && git commit -m "init"
     git branch -M main
     git remote add origin https://github.com/<你的帳號>/etf-tracker.git
     git push -u origin main
     ```
   - 網頁上傳：在儲存庫頁面按 *uploading an existing file*，把解壓後的資料夾內容全部拖進去。macOS Finder 預設隱藏 `.github` 資料夾，先按 `⌘ + Shift + .` 顯示隱藏檔再拖。
3. **開啟 Pages**：Settings → Pages → Build and deployment → Source 選 **GitHub Actions**。
4. **開啟寫入權限**：Settings → Actions → General → Workflow permissions 選 **Read and write permissions**。
5. **第一次執行**：Actions → daily → Run workflow → mode 選 `full`。約 3–5 分鐘後網站在 `https://<你的帳號>.github.io/etf-tracker/`。

之後每週一至週五 09:00（台北）自動更新，12:00 再補抓一次。

## 一鍵更新

網頁右上角「一鍵更新」會觸發 GitHub Actions：

- 只更新股價：約 1–2 分鐘。
- 完整更新：重抓持股＋股價，約 3–5 分鐘。

第一次使用要建立權杖：GitHub 頭像 → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token。Repository access 只選這個儲存庫，Permissions 的 **Actions** 設為 Read and write。把權杖貼到網頁的「GitHub 設定」，它只存在你那台裝置的瀏覽器。

## 推播提醒（選用）

Settings → Secrets and variables → Actions 新增：

- Email：`SMTP_HOST`（Gmail 為 smtp.gmail.com）、`SMTP_PORT`（465）、`SMTP_USER`、`SMTP_PASS`（Gmail 應用程式密碼）、`ALERT_EMAIL_TO`
- LINE Messaging API：`LINE_CHANNEL_TOKEN`、`LINE_USER_ID`

只推播「高」等級（新增／剔除持股、現金比例大變動、規則未符合、資料過期、抓取失敗）。門檻在 `config/alerts.json`。

## 新增追蹤的 ETF

- 安聯、統一、群益旗下：在 `config/etfs.json` 複製一筆，改 `code`、`name`、`params`。代號對照在 `config/registry.json`（例如 00993A 安聯 `E0002`、00403A 統一 `63YTW`、00992A 群益 `500`）。
- 其他投信：網站格式不同，要新增 `tracker/adapters/<投信>.py`。
- 公開說明書規則：`config/rules.json`；主題分類：`config/themes.json`（未分類的股票會列在「資料健康」頁）。

## 資料來源

| 投信 | 來源 | 公布時間 |
|---|---|---|
| 安聯 | `etf.allianzgi.com.tw/webapi/api/Fund/GetFundAssets`（申購買回清單） | 前一晚 |
| 統一 | `ezmoney.com.tw/ETF/Fund/Info` 頁面內嵌資料 | 當天約 16:40 |
| 群益 | `capitalfund.com.tw/CFWeb/api/etf/buyback`（申購買回清單） | 前一晚 |

股價與匯率：Yahoo Finance 公開報價（開源套件 yfinance 使用的同一來源）。若投信網站擋下一般請求，程式會自動改用無頭瀏覽器（Playwright）重抓。

## 本機執行

```bash
pip install -r requirements.txt
python -m tracker.run --mode full      # 抓持股＋股價＋產生網站資料
python -m tracker.run --demo           # 不連網，用 tests/fixtures 的真實擷取資料產生示範
python -m pytest -q                    # 測試
python scripts/build_pages.py && python -m http.server -d _site 8000
```

## 名詞與限制

- **現金及其他** = 100% − 股票% − 債券%（含現金、附買回、期貨保證金、應收付）。安聯只揭露股票與附買回，現金以差額推算。
- **有效股票曝險** = 股票% ＋ 期貨多單名目% − 空單名目%。
- **主動調整** = 今日股數 − 前一日股數 × (今日單位數 ÷ 前一日單位數)，剔除申購贖回的被動買賣。
- **推算平均成本**：投信不公布成本，以每日增加股數 × 當日典型價 (H+L+C)/3 累積，追蹤第一天以當日價格為起始基準。
- 主動 ETF 歷史短，現金比例與行情的關係要累積資料後再判斷。本工具不構成投資建議。
