# Football Edge v5 Online

這版不再用 HTA，也不要求使用者安裝 Python。

真正的使用方式：
1. 把專案部署到 Render / Railway。
2. 平台會產生一個 https 網址。
3. 之後每天只開那個網址。

## v5 特色
- 支援俱樂部與國家隊
- 已加入 Denmark (8238) / Norway (8492) 國家隊 fallback
- 搜尋先用 /api/search/suggest，再 fallback /api/searchData
- server-side FotMob access，避開 HTA/瀏覽器跨域與權限問題
- H2H
- 近 5/10/15/20 場
- 主客場拆分
- Dixon–Coles corrected Poisson
- Top 3 / Top 10 比分
- 1X2
- Over 2.5
- BTTS
- 不敗機率
- 模型信心

## Render 部署
1. 把整個資料夾上傳 GitHub。
2. Render -> New -> Blueprint。
3. 選這個 GitHub repository。
4. 等部署完成。
5. Render 會給你一個 https://...onrender.com 網址。

## 注意
目前資料來源仍是 FotMob 的非官方網站 JSON endpoint。
線上後端可以避開 HTA 的權限限制，但 FotMob 未來仍可能改版或限制雲端 IP。
