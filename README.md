# 基地模自動生成

輸入基地經緯度，自動產生周邊 3D 鄰房白模＋地形（SketchUp .skp、AutoCAD .dxf、OBJ、建物清單 CSV）。

- 建物：內政部國土測繪中心 三維建物模型（分棟版 LOD1，免申請、免費）
- 地形：內政部 20 m 網格 DTM（政府資料開放平臺，免費）
- 預設半徑 500 m，模型單位公尺，原點 = 基地中心，z=0 = 基地中心地面

## 需求

- macOS 或 Windows 10 以上
- Python 3 ＋ `numpy`（Mac：`pip3 install numpy`；Windows：`pip install numpy`）
- SketchUp（自動找 `/Applications/SketchUp 20xx` 最新版；其他位置設環境變數 `SITE_MODEL_SKETCHUP`。沒裝也能跑，只是不產生 .skp，可用 .rb 在 SketchUp 重建）
- Blender（選用，只用來產生預覽圖；自動找預設安裝位置或 PATH，其他位置設 `SITE_MODEL_BLENDER`）

## 安裝成 Claude Code skill

Mac：
```bash
git clone https://github.com/TCLIN2311/site-model-auto.git ~/.claude/skills/site-model
```

Windows（PowerShell）：
```powershell
git clone https://github.com/TCLIN2311/site-model-auto.git "$env:USERPROFILE\.claude\skills\site-model"
```

之後在 Claude Code 輸入 `/site-model 25.047333, 121.516694`，或說「幫我做基地模」並附上座標。

## 直接執行

```bash
python3 run.py --lat 25.047333 --lon 121.516694 --out ~/Desktop/基地模
```

Windows 把 `python3` 換成 `python`。

預設輸出到下載資料夾 `~/Downloads/基地模製作/<案名>_輸出/`（Windows 為 `%USERPROFILE%\Downloads\基地模製作\`），可用 `--out` 或環境變數 `SITE_MODEL_OUT` 改。

常用參數：`--radius 500`、`--name 案名`、`--out 輸出資料夾`、`--flat`（不要地形）、`--county 臺北市,新北市`、`--site 宗地.json`、`--extra 補充建物.json`。詳見 `SKILL.md`。

## 支援縣市

| 類型 | 縣市 |
|---|---|
| 分棟版（每棟獨立、有樓層高度） | 臺北市、新北市、桃園市、宜蘭縣、新竹縣、苗栗縣、南投縣、彰化縣、雲林縣、嘉義縣、屏東縣、花蓮縣、臺東縣 |
| 只有融合版（街廓可能連成一塊） | 臺中市、臺南市、高雄市、基隆市、新竹市、嘉義市、澎湖縣、金門縣、連江縣 |

## 限制

- 建物高度為實測屋頂最高點（含頂樓加蓋），非樓層 × 層高。
- 資料有時間落差，近一兩年新完工建物可能沒有。
- 地形為 20 m 網格；高架道路已壓平，不在模型內。

## 授權

MIT License，見 [LICENSE](LICENSE)。資料來源（內政部國土測繪中心三維建物、內政部 DTM）依其各自的政府資料開放授權條款。
