#!/usr/bin/env python3
"""
基地周遭鄰房白模產生器
資料來源：內政部國土測繪中心 三維建物模型（分棟版 LOD1）3D Tiles 免申請介接服務
  https://3dtiles.nlsc.gov.tw/building/tiles3d/<服務代碼>/tileset.json
  （服務代碼見 3dmaps.nlsc.gov.tw「介接服務說明」：分棟版 臺北市 30、新北市 35 …）

每棟建物帶 BUILD_H（建物高）、BUILD_NO（樓層數）、BUILD_ID，輸出：
  <name>_SketchUp.rb   SketchUp Ruby Console 載入 → 每棟一個群組、推拉成方正量體
  <name>_輪廓.dxf       AutoCAD 用，鄰房外框 + 樓層數字（TWD97 座標，公尺）
  <name>_白模.obj       Blender / 預覽用
  <name>_建物清單.csv   每棟 ID、樓層、高度、TWD97 中心

用法：
  python3 site_model.py --lat 25.0473 --lon 121.5167 --radius 200 --name 範例案
  python3 site_model.py --e 302130 --n 2771120 --radius 200 --name 範例案
  python3 site_model.py --site 範例案_基地範圍.json --radius 200 --name 範例案
    （--site：各宗地經緯度多邊形；以基地中心為原點，基地內既有建物放進隱藏的「基地內既有建物」標籤）
  python3 site_model.py --site 範例案_基地範圍.json --extra 範例案_補充建物.json --name 範例案
    （--extra：NLSC 缺漏、自行描繪補上的建物，TWD97 座標，放進「補充建物」標籤；
      同一檔的 height_fix 可依 BUILD_ID 修正 NLSC 的樓層與高度）

地形：預設加上內政部 20 m 網格 DTM（政府資料開放平臺，免費，依縣市下載一次後快取）。
  基地中心地面 = 模型 z=0，每棟建物落在外框範圍內最低的地面上；--flat 不加地形。
  縣市依 --code 判斷，其他縣市或跨縣市用 --county 臺北市,新北市
"""
import argparse, csv, gzip, json, math, os, struct, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
import numpy as np

BASE = "https://3dtiles.nlsc.gov.tw/building/tiles3d/{code}/"
# 不放同步資料夾（Synology Drive 會干擾寫入）
CACHE = (os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "nlsc3d") if os.name == "nt"
         else os.path.expanduser("~/Library/Caches/nlsc3d"))

# ---------- 座標：WGS84/GRS80 ↔ ECEF ↔ TWD97 TM2 ----------
A, F = 6378137.0, 1 / 298.257222101
E2 = F * (2 - F)


def geodetic_to_ecef(lat, lon, h=0.0):
    lat, lon = math.radians(lat), math.radians(lon)
    n = A / math.sqrt(1 - E2 * math.sin(lat) ** 2)
    return np.array([(n + h) * math.cos(lat) * math.cos(lon),
                     (n + h) * math.cos(lat) * math.sin(lon),
                     (n * (1 - E2) + h) * math.sin(lat)])


def ecef_to_geodetic(p):
    x, y, z = p[:, 0], p[:, 1], p[:, 2]
    lon = np.arctan2(y, x)
    r = np.hypot(x, y)
    lat = np.arctan2(z, r * (1 - E2))
    for _ in range(5):
        n = A / np.sqrt(1 - E2 * np.sin(lat) ** 2)
        h = r / np.cos(lat) - n
        lat = np.arctan2(z, r * (1 - E2 * n / (n + h)))
    n = A / np.sqrt(1 - E2 * np.sin(lat) ** 2)
    h = r / np.cos(lat) - n
    return lat, lon, h


def tm2(lat, lon, lon0=121.0, k0=0.9999, fe=250000.0):
    """TWD97 二度分帶（弧度輸入，回傳 E, N）"""
    ep2 = E2 / (1 - E2)
    n = A / np.sqrt(1 - E2 * np.sin(lat) ** 2)
    t = np.tan(lat) ** 2
    c = ep2 * np.cos(lat) ** 2
    a = (lon - math.radians(lon0)) * np.cos(lat)
    m = A * ((1 - E2 / 4 - 3 * E2**2 / 64 - 5 * E2**3 / 256) * lat
             - (3 * E2 / 8 + 3 * E2**2 / 32 + 45 * E2**3 / 1024) * np.sin(2 * lat)
             + (15 * E2**2 / 256 + 45 * E2**3 / 1024) * np.sin(4 * lat)
             - (35 * E2**3 / 3072) * np.sin(6 * lat))
    e = fe + k0 * n * (a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t**2 + 72 * c - 58 * ep2) * a**5 / 120)
    nn = k0 * (m + n * np.tan(lat) * (a**2 / 2 + (5 - t + 9 * c + 4 * c**2) * a**4 / 24
                                      + (61 - 58 * t + t**2 + 600 * c - 330 * ep2) * a**6 / 720))
    return e, nn


def tm2_inverse(e, n, lon0=121.0, k0=0.9999, fe=250000.0):
    """TWD97 E,N → 緯經度（度），用迭代反解，夠用於定位中心點"""
    lat, lon = math.radians(23.5), math.radians(lon0)
    for _ in range(20):
        ee, nn = tm2(np.array([lat]), np.array([lon]), lon0, k0, fe)
        lat += (n - nn[0]) / 6371000.0
        lon += (e - ee[0]) / (6371000.0 * math.cos(lat))
    return math.degrees(lat), math.degrees(lon)


# ---------- 下載 ----------
def valid(path, glb):
    """NLSC 伺服器偶爾回傳長度正確但內容全是 0 的壞檔 → 驗證後才收進快取"""
    try:
        data = read_maybe_gzip(path)
        if glb:
            jl = struct.unpack("<I", data[12:16])[0]
            return data[:4] == b"glTF" and json.loads(data[20:20 + jl]) is not None
        return json.loads(data) is not None
    except Exception:
        return False


def fetch(url, path):
    glb = path.endswith(".glb")
    if os.path.exists(path) and valid(path, glb):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    for _ in range(8):
        # 壞檔解壓失敗時 curl 回 61，一樣重抓
        r = subprocess.run(["curl", "-sf", "--compressed", "-m", "60", "--retry", "3", "-o", tmp, url])
        if r.returncode == 0 and valid(tmp, glb):
            os.replace(tmp, path)
            return path
    raise RuntimeError(f"下載內容一直損毀：{url}")


def read_maybe_gzip(path):
    data = open(path, "rb").read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


# ---------- GLB 解析 ----------
CT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def parse_glb(data):
    jl = struct.unpack("<I", data[12:16])[0]
    j = json.loads(data[20:20 + jl])
    bl = struct.unpack("<I", data[20 + jl:24 + jl])[0]
    binc = data[28 + jl:28 + jl + bl]

    def view(i):
        v = j["bufferViews"][i]
        o = v.get("byteOffset", 0)
        return binc[o:o + v["byteLength"]], v.get("byteStride")

    def acc(i):
        a = j["accessors"][i]
        buf, stride = view(a["bufferView"])
        dt, nc = np.dtype(CT[a["componentType"]]), NC[a["type"]]
        o = a.get("byteOffset", 0)
        if stride and stride != dt.itemsize * nc:
            raw = np.frombuffer(buf, np.uint8)
            rows = np.stack([raw[o + k * stride:o + k * stride + dt.itemsize * nc] for k in range(a["count"])])
            return rows.view(dt).reshape(a["count"], nc)
        return np.frombuffer(buf, dt, a["count"] * nc, o).reshape(a["count"], nc)

    # 屬性表（每棟一列）
    props = []
    sm = j.get("extensions", {}).get("EXT_structural_metadata")
    if sm:
        pt = sm["propertyTables"][0]
        cnt = pt["count"]
        cols = {}
        for k, p in pt["properties"].items():
            vals, _ = view(p["values"])
            offs = np.frombuffer(view(p["stringOffsets"])[0], np.uint32, cnt + 1)
            cols[k] = [vals[offs[i]:offs[i + 1]].decode("utf-8", "replace") for i in range(cnt)]
        props = [{k: cols[k][i] for k in cols} for i in range(cnt)]

    meshes = []
    for node in j["nodes"]:
        if "mesh" not in node:
            continue
        if "matrix" in node:
            mtx = np.array(node["matrix"]).reshape(4, 4).T
        else:
            mtx = np.eye(4)
            mtx[:3, 3] = node.get("translation", [0, 0, 0])
            if "rotation" in node or "scale" in node:
                raise RuntimeError("node rotation/scale not handled")
        for prim in j["meshes"][node["mesh"]]["primitives"]:
            at = prim["attributes"]
            pos = acc(at["POSITION"]).astype(np.float64)
            pos = pos @ mtx[:3, :3].T + mtx[:3, 3]
            # glTF y-up → 3D Tiles z-up (ECEF)
            ecef = np.column_stack([pos[:, 0], -pos[:, 2], pos[:, 1]])
            idx = acc(prim["indices"]).reshape(-1, 3).astype(np.int64) if "indices" in prim \
                else np.arange(len(pos)).reshape(-1, 3)
            fid = acc(at["_FEATURE_ID_0"]).ravel().astype(np.int64) if "_FEATURE_ID_0" in at \
                else np.zeros(len(pos), np.int64)
            meshes.append((ecef, idx, fid))
    return meshes, props


# ---------- 輪廓萃取 ----------
def boundary_loops(tris_xy, tol=0.005):
    """上表面三角形 → 外框/中庭的封閉迴圈（依頂點座標焊接）"""
    key = lambda p: (round(p[0] / tol), round(p[1] / tol))
    pts, kid = {}, []
    for t in tris_xy:
        ks = []
        for p in t:
            k = key(p)
            pts.setdefault(k, p)
            ks.append(k)
        kid.append(ks)
    edges = {}
    for a, b, c in kid:
        for u, v in ((a, b), (b, c), (c, a)):
            if u == v:
                continue
            if (v, u) in edges:
                del edges[(v, u)]
            else:
                edges[(u, v)] = True
    nxt = {}
    for u, v in edges:
        nxt.setdefault(u, []).append(v)
    loops = []
    while nxt:
        start = next(iter(nxt))
        loop, cur = [start], start
        while True:
            vs = nxt.get(cur)
            if not vs:
                break
            nv = vs.pop()
            if not vs:
                del nxt[cur]
            if nv == start:
                break
            loop.append(nv)
            cur = nv
        if len(loop) >= 3:
            loops.append([pts[k] for k in loop])
    return loops


def simplify(loop, tol=0.03):
    """去掉共線點（平直牆面上的多餘頂點）"""
    pts = [np.asarray(p) for p in loop]
    changed = True
    while changed and len(pts) > 3:
        changed = False
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            ab, ac = b - a, c - a
            L = np.linalg.norm(ac)
            if np.linalg.norm(ab) < tol or L < 1e-9 or abs(ab[0] * ac[1] - ab[1] * ac[0]) / L < tol:
                pts.pop(i)
                changed = True
                break
    return pts


def area(loop):
    x = np.array([p[0] for p in loop]); y = np.array([p[1] for p in loop])
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def inside(pts, poly):
    """射線法，pts (k,2) 是否落在 poly 內"""
    x, y = pts[:, 0][:, None], pts[:, 1][:, None]
    px, py = poly[:, 0][None], poly[:, 1][None]
    qx, qy = np.roll(px, -1, 1), np.roll(py, -1, 1)
    cross = ((py > y) != (qy > y)) & (x < (qx - px) * (y - py) / np.where(qy == py, 1e-12, qy - py) + px)
    return cross.sum(1) % 2 == 1


def site_share(outer, holes, parcels, step=0.3):
    """建物投影落在基地（各宗地聯集）內的比例，以 0.3 m 格點取樣"""
    o = np.array(outer)
    xs = np.arange(o[:, 0].min() + step / 2, o[:, 0].max(), step)
    ys = np.arange(o[:, 1].min() + step / 2, o[:, 1].max(), step)
    g = np.array(np.meshgrid(xs, ys)).reshape(2, -1).T
    if not len(g):
        return 0.0
    m = inside(g, o)
    for hl in holes:
        m &= ~inside(g, np.array(hl))
    g = g[m]
    if not len(g):
        return 0.0
    hit = np.zeros(len(g), bool)
    for p in parcels.values():
        hit |= inside(g, p)
    return float(hit.mean())


def load_site(path):
    """基地範圍 JSON：{"parcels": {"地號": [[lon,lat],...]}} → TWD97 多邊形"""
    d = json.load(open(path, encoding="utf-8"))
    out = {}
    for no, ring in d["parcels"].items():
        a = np.array(ring, float)
        e, n = tm2(np.radians(a[:, 1]), np.radians(a[:, 0]))
        out[no] = np.column_stack([e, n])
    return out


# ---------- 地形：內政部 20 m 網格 DTM（政府資料開放平臺，免費） ----------
DTM_INDEX = ("https://opdadm.moi.gov.tw/api/v1/no-auth/resource/api/dataset/"
             "A964612F-0D64-4C81-BFE5-6C1F2BA61DED/resource/A0B94F67-8ADF-48A1-8DF0-C60719AD2B28/download")
# 3dmaps.nlsc.gov.tw「介接服務說明」：分棟版 30~49；沒有分棟版的縣市只能用融合版 0~21
COUNTY_CODE = {"臺北市": "30", "新北市": "35", "宜蘭縣": "36", "桃園市": "37", "新竹縣": "39", "苗栗縣": "40",
               "南投縣": "41", "彰化縣": "42", "雲林縣": "44", "嘉義縣": "45", "屏東縣": "46", "花蓮縣": "47",
               "臺東縣": "48", "臺中市": "1", "基隆市": "2", "臺南市": "3", "高雄市": "4", "嘉義市": "8",
               "新竹市": "13", "澎湖縣": "19", "金門縣": "20", "連江縣": "21"}
CODE_COUNTY = {v: k for k, v in COUNTY_CODE.items()}


def lookup_place(lat, lon):
    """國土測繪中心 坐標回傳行政區（地政版）→ (縣市, 鄉鎮市區, 地段)；查不到回傳空字串"""
    import re
    r = subprocess.run(["curl", "-sf", "-m", "20",
                        f"https://api.nlsc.gov.tw/other/TownVillagePointQuery/{lon:.6f}/{lat:.6f}/4326"],
                       capture_output=True, encoding="utf-8", errors="replace")
    get = lambda k: (re.search(f"<{k}>(.*?)</{k}>", r.stdout) or [None, ""])[1]
    return get("ctyName"), get("townName"), get("sectName")


def load_dtm(counties, oe, on, half):
    """回傳 (xs, ys, H)：TWD97 20 m 格點（局部座標）與 TWVD2001 高程，涵蓋 ±half 公尺；缺值為 nan"""
    import urllib.parse, zipfile
    ddir = os.path.join(CACHE, "dtm")
    os.makedirs(ddir, exist_ok=True)
    idx = os.path.join(ddir, "index.csv")
    if not os.path.exists(idx) and subprocess.run(["curl", "-sfL", "-m", "60", "-o", idx, DTM_INDEX]).returncode:
        sys.exit("DTM 索引下載失敗")
    rows = list(csv.DictReader(open(idx, encoding="utf-8-sig")))
    x0, y0 = math.floor((oe - half) / 20) * 20, math.floor((on - half) / 20) * 20
    nx, ny = int((oe + half - x0) // 20) + 1, int((on + half - y0) // 20) + 1
    H = np.full((ny, nx), np.nan)
    for county in counties:
        row = next((r for r in rows if r["圖資名稱"].startswith(f"分幅_{county}")), None)
        if not row:
            sys.exit(f"DTM 索引裡找不到「{county}」，可用 --county 指定（例：--county 新北市）")
        url = row["連結網址"]
        i = url.rfind("/") + 1
        url = url[:i] + urllib.parse.quote(url[i:])
        zp = os.path.join(ddir, county + ".zip")
        if not os.path.exists(zp):
            print(f"下載 {row['圖資名稱']} …")
            if subprocess.run(["curl", "-sfL", "-m", "300", "-o", zp + ".part", url]).returncode:
                sys.exit("DTM 下載失敗")
            os.replace(zp + ".part", zp)
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                if not name.endswith(".grd"):
                    continue
                txt = z.read(name)
                e, n = map(float, txt.split(b"\n", 1)[0].split()[:2])     # 第一點 = 圖幅西南角
                if not (e - 3500 < oe < e + 6500 and n - 3500 < on < n + 6500):
                    continue
                a = np.array(txt.split(), float).reshape(-1, 3)
                c = np.round((a[:, 0] - x0) / 20).astype(int); r = np.round((a[:, 1] - y0) / 20).astype(int)
                ok = (c >= 0) & (c < nx) & (r >= 0) & (r < ny)
                H[r[ok], c[ok]] = a[ok, 2]
    if np.isnan(H).all():
        sys.exit("這個範圍沒有 DTM 資料，請確認 --county")
    # 高架道路被當成地面（1～2 格寬的高帶）：比 5×5 鄰域中位數高 1.5 m 以上的格點挖掉，再用其餘鄰點補回
    def nb_median(G, k=2):
        P = np.pad(G, k, constant_values=np.nan)
        with np.errstate(all="ignore"):
            return np.nanmedian(np.stack([P[k + dy:k + dy + ny, k + dx:k + dx + nx]
                                          for dy in range(-k, k + 1) for dx in range(-k, k + 1)]), axis=0)
    spike = H - nb_median(H) > 1.5
    G = H.copy(); G[spike] = np.nan
    H[spike] = nb_median(G)[spike]
    if np.isnan(H).any():   # 縣市邊界外的缺格：用最近格補
        print(f"  ⚠ DTM 有 {int(np.isnan(H).sum())} 格缺值（可能跨縣市，可加 --county 鄰近縣市），以最近格補")
        rr, cc = np.nonzero(~np.isnan(H))
        for r, c in zip(*np.nonzero(np.isnan(H))):
            k = np.argmin((rr - r) ** 2 + (cc - c) ** 2)
            H[r, c] = H[rr[k], cc[k]]
    print(f"地形 {nx}×{ny} 格（20 m），高程 {np.nanmin(H):.1f}～{np.nanmax(H):.1f} m，壓平高架凸起 {int(spike.sum())} 處")
    return x0 - oe + 20 * np.arange(nx), y0 - on + 20 * np.arange(ny), H


def dtm_at(dtm, x, y):
    """雙線性內插（局部座標）"""
    xs, ys, H = dtm
    fx = np.clip((np.asarray(x) - xs[0]) / 20, 0, len(xs) - 1.001)
    fy = np.clip((np.asarray(y) - ys[0]) / 20, 0, len(ys) - 1.001)
    i, j = fx.astype(int), fy.astype(int)
    u, v = fx - i, fy - j
    return (H[j, i] * (1 - u) * (1 - v) + H[j, i + 1] * u * (1 - v)
            + H[j + 1, i] * (1 - u) * v + H[j + 1, i + 1] * u * v)


def drape(ring, dtm, zref, step=4.0, lift=0.05):
    """宗地線加密後貼到地形上"""
    out = []
    for a, b in zip(ring, np.roll(ring, -1, axis=0)):
        k = max(1, int(math.hypot(*(b - a)) // step))
        for t in range(k):
            out.append(a + (b - a) * t / k)
    out = np.array(out)
    return np.column_stack([out, dtm_at(dtm, out[:, 0], out[:, 1]) - zref + lift])


# ---------- 主流程 ----------
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows 主控台預設 cp950，印不出 ⚠ 等符號
    ap = argparse.ArgumentParser(description="NLSC 三維建物 → 基地鄰房白模")
    ap.add_argument("--lat", type=float); ap.add_argument("--lon", type=float)
    ap.add_argument("--e", type=float, help="TWD97 E"); ap.add_argument("--n", type=float, help="TWD97 N")
    ap.add_argument("--radius", type=float, default=200, help="取樣半徑（公尺）")
    ap.add_argument("--code", help="NLSC 建物服務代碼（預設依座標自動判斷縣市；跨縣市自動全抓）")
    ap.add_argument("--name", help="案名（預設用行政區＋地段）")
    ap.add_argument("--out", default=".")
    ap.add_argument("--flat", action="store_true", help="不加地形，全部落在 z=0")
    ap.add_argument("--county", help="縣市（預設依座標自動判斷；跨縣市用逗號，例：臺北市,新北市）")
    ap.add_argument("--extra", help="補充建物 JSON（NLSC 缺漏的建物，TWD97 外框 + 樓層 + 高度）")
    ap.add_argument("--site", help="基地範圍 JSON（各宗地經緯度多邊形）；給了就以基地中心為原點，並標出基地內既有建物")
    args = ap.parse_args()

    site = load_site(args.site) if args.site else {}
    if args.e is not None and args.n is not None:
        lat, lon = tm2_inverse(args.e, args.n)
    elif args.lat is not None and args.lon is not None:
        lat, lon = args.lat, args.lon
    elif site:
        allp = np.vstack(list(site.values()))
        lat, lon = tm2_inverse(float(allp[:, 0].mean()), float(allp[:, 1].mean()))
    else:
        ap.error("請給 --lat --lon、--e --n 或 --site")
    ce, cn = tm2(np.radians([lat]), np.radians([lon]))
    oe, on = round(float(ce[0])), round(float(cn[0]))   # 局部原點（TWD97 整數公尺）
    site = {no: p - [oe, on] for no, p in site.items()}
    print(f"中心 {lat:.6f},{lon:.6f}  TWD97 E{oe} N{on}  半徑 {args.radius} m")

    # 縣市：中心＋半徑圓周 8 點查行政區，跨縣市就全部抓
    cty, town, sect = lookup_place(lat, lon)
    if args.county:
        counties = args.county.split(",")
    elif args.code:
        counties = [CODE_COUNTY.get(args.code, "")]
    else:
        counties = [cty] if cty else []
        for k in range(8):
            a = k * math.pi / 4
            la, lo = tm2_inverse(oe + args.radius * math.cos(a), on + args.radius * math.sin(a))
            c = lookup_place(la, lo)[0]
            if c and c not in counties:
                counties.append(c)
        if not counties:
            ap.error("查不到這個座標的縣市，請加 --county")
    codes = [args.code] if args.code else [COUNTY_CODE[c] for c in counties if c in COUNTY_CODE]
    name = args.name or (town + sect if town else "基地")
    print(f"位置 {cty}{town} {sect}  →  縣市 {'、'.join(counties)}（建物服務代碼 {'、'.join(codes)}）")
    for c in codes:
        if int(c) < 30:
            print(f"  ⚠ {CODE_COUNTY.get(c, c)} 沒有分棟版，只能用融合版（代碼 {c}），建物可能整條街廓連成一塊、未必有樓層屬性")

    center = geodetic_to_ecef(lat, lon, 30)
    paths, layers, sources = [], [], []
    for code in codes:
        base = BASE.format(code=code)
        cdir = os.path.join(CACHE, code)
        ts = json.loads(read_maybe_gzip(fetch(base + "tileset.json", os.path.join(cdir, "tileset.json"))))
        layers.append(ts.get("name")); sources.append(base + "tileset.json")
        picks = []

        def walk(nd):
            s = nd["boundingVolume"]["sphere"]
            if np.linalg.norm(np.array(s[:3]) - center) > s[3] + args.radius + 30:
                return
            if not nd.get("children"):
                picks.extend(c["uri"] for c in nd.get("contents", [nd.get("content")]) if c)
            for c in nd.get("children", []):
                walk(c)
        walk(ts["root"])
        print(f"{CODE_COUNTY.get(code, code)}（圖層 {ts.get('name')}）下載 {len(picks)} 個圖磚…")
        with ThreadPoolExecutor(4) as ex:
            paths += list(ex.map(lambda u: fetch(base + u, os.path.join(cdir, u)), picks))

    buildings = {}
    for path in paths:
        meshes, props = parse_glb(read_maybe_gzip(path))
        for ecef, idx, fid in meshes:
            la, lo, h = ecef_to_geodetic(ecef)
            e, n = tm2(la, lo)
            P = np.column_stack([e - oe, n - on, h])
            for f in np.unique(fid):
                pr = props[f] if f < len(props) else {}
                bid = pr.get("BUILD_ID", f"{os.path.basename(path)}#{f}")
                if bid in buildings:
                    continue
                tri = idx[fid[idx[:, 0]] == f]
                V = P[tri]                                     # (k,3,3)
                nrm = np.cross(V[:, 1] - V[:, 0], V[:, 2] - V[:, 0])
                ln = np.linalg.norm(nrm, axis=1)
                ok = ln > 1e-9
                up = np.zeros(len(V), bool)
                up[ok] = nrm[ok, 2] / ln[ok] > 0.95
                if not up.any():
                    continue
                buildings[bid] = dict(props=pr, top=V[up][:, :, :2], zmin=float(V[:, :, 2].min()),
                                      zmax=float(V[:, :, 2].max()))

    rows, solids = [], []
    for bid, b in buildings.items():
        loops = [simplify(l) for l in boundary_loops(b["top"])]
        loops = [l for l in loops if len(l) >= 3 and abs(area(l)) > 0.5]
        if not loops:
            continue
        loops.sort(key=lambda l: -abs(area(l)))
        outer = loops[0]
        if area(outer) < 0:
            outer = outer[::-1]
        holes = [l if area(l) < 0 else l[::-1] for l in loops[1:]]
        cx = float(np.mean([p[0] for p in outer])); cy = float(np.mean([p[1] for p in outer]))
        if math.hypot(cx, cy) > args.radius:
            continue
        pr = b["props"]
        try:
            h = float(pr.get("BUILD_H", "nan"))
        except ValueError:
            h = float("nan")
        if not (h > 0):
            h = b["zmax"] - b["zmin"]
        floors = pr.get("BUILD_NO", "")
        z0 = 0.0
        share = site_share(outer, holes, site) if site else 0.0
        solids.append(dict(id=bid, outer=outer, holes=holes, z0=z0, h=h, floors=floors, cx=cx, cy=cy,
                           area=abs(area(outer)) - sum(abs(area(x)) for x in holes),
                           share=share, onsite=share >= 0.5, manual=False))
    extra = json.load(open(args.extra, encoding="utf-8")) if args.extra else {}
    for s in solids:   # 高度修正：NLSC 與航照/地形圖判讀不符的棟
        fix = extra.get("height_fix", {}).get(s["id"])
        if fix:
            print(f"  ✎修正 {s['id']} {s['floors']}F {s['h']:.2f}m → {fix.get('floors', s['floors'])}F {fix['height']}m")
            s["h"], s["floors"], s["fixed"] = float(fix["height"]), str(fix.get("floors", s["floors"])), True
    if extra:
        for x in extra.get("buildings", []):
            ring = [(e - oe, n - on) for e, n in x["ring"]]
            if area(ring) < 0:
                ring = ring[::-1]
            share = site_share(ring, [], site) if site else 0.0
            solids.append(dict(id=x["id"], outer=[np.array(p) for p in ring], holes=[], z0=0.0, h=float(x["height"]),
                               floors=str(x["floors"]), cx=float(np.mean([p[0] for p in ring])),
                               cy=float(np.mean([p[1] for p in ring])), area=abs(area(ring)),
                               share=share, onsite=share >= 0.5, manual=True))
            print(f"  ＋補充 {x['id']} {x['floors']}F {x['height']}m")
    dtm, zref, parcels3d = None, 0.0, {}
    if not args.flat:
        dtm = load_dtm(counties, oe, on, args.radius + 20)
        zref = float(dtm_at(dtm, 0.0, 0.0))     # 基地中心地面 = 模型 z=0
        for s in solids:   # 建物底 = 外框各點地面最低處（坡地上不會懸空）
            p = np.vstack([np.array(s["outer"]), [[s["cx"], s["cy"]]]])
            s["z0"] = float(dtm_at(dtm, p[:, 0], p[:, 1]).min()) - zref
        parcels3d = {no: drape(p, dtm, zref) for no, p in site.items()}
        print(f"基地中心地面高程 {zref:.2f} m（TWVD2001）= 模型 z=0")
    solids.sort(key=lambda s: math.hypot(s["cx"], s["cy"]))
    n_on = sum(s["onsite"] for s in solids)
    print(f"建物 {len(solids)} 棟（半徑 {args.radius} m 內）" + (f"，其中基地內既有 {n_on} 棟" if site else ""))
    for s in solids:
        if site and 0.1 < s["share"] < 0.9:
            print(f"  ⚠ {s['id']} {s['floors']}F 跨基地線（{s['share']:.0%} 在基地內）→ {'算基地內' if s['onsite'] else '算鄰房'}")

    os.makedirs(args.out, exist_ok=True)
    stem = os.path.join(args.out, name)
    ter = terrain_mesh(dtm, zref) if dtm else None
    write_csv(stem + "_建物清單.csv", solids, oe, on, zref)
    write_rb(stem + "_SketchUp.rb", solids, name, oe, on, site, parcels3d, ter)
    write_dxf(stem + "_輪廓.dxf", solids, oe, on, site, ter, zref)
    write_obj(stem + "_白模.obj", solids, ter)
    meta = dict(name=name, lat=lat, lon=lon, origin_twd97=[oe, on], radius=args.radius,
                place=f"{cty}{town} {sect}", counties=counties,
                source=sources, layer=layers, buildings=len(solids),
                onsite=[s["id"] for s in solids if s["onsite"]],
                terrain=None if not dtm else dict(source="內政部 2025年版 20公尺網格 DTM（政府資料開放平臺）",
                                                   z0_TWVD2001=round(zref, 3)))
    json.dump(meta, open(stem + "_meta.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("輸出：", *(stem + s for s in ("_SketchUp.rb", "_輪廓.dxf", "_白模.obj", "_建物清單.csv")), sep="\n  ")


# ---------- 輸出 ----------
def terrain_mesh(dtm, zref):
    """20 m 格點 → 三角網（局部座標，z 相對基地中心地面）"""
    xs, ys, H = dtm
    X, Y = np.meshgrid(xs, ys)
    pts = np.column_stack([X.ravel(), Y.ravel(), H.ravel() - zref])
    nx = len(xs)
    tris = []
    for j in range(len(ys) - 1):
        for i in range(nx - 1):
            a = j * nx + i
            tris += [(a, a + 1, a + nx + 1), (a, a + nx + 1, a + nx)]
    return pts, tris


def write_csv(path, solids, oe, on, zref=0.0):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["BUILD_ID", "樓層", "高度m", "投影面積m2", "距中心m", "TWD97_E", "TWD97_N",
                    "基底高程m(TWVD2001)", "基地內", "在基地內比例", "來源"])
        for s in solids:
            w.writerow([s["id"], s["floors"], f"{s['h']:.2f}", f"{s['area']:.1f}",
                        f"{math.hypot(s['cx'], s['cy']):.1f}", f"{s['cx'] + oe:.2f}", f"{s['cy'] + on:.2f}",
                        f"{s['z0'] + zref:.2f}", "Y" if s["onsite"] else "", f"{s['share']:.2f}", "手動補充" if s["manual"] else "NLSC（高度修正）" if s.get("fixed") else "NLSC"])


def write_rb(path, solids, name, oe, on, site, parcels3d=None, ter=None):
    """SketchUp：每棟 add_face + pushpull，跟手拉的一樣乾淨；基地內既有建物另放一個隱藏的標籤"""
    def pts(loop, z):
        return "[" + ",".join(f"[{p[0]:.3f}.m,{p[1]:.3f}.m,{z:.3f}.m]" for p in loop) + "]"
    L = [
        "# 由 site_model.py 產生 — 資料：內政部國土測繪中心 三維建物模型（分棟版）",
        f"# 局部原點 = TWD97 E{oe} N{on}（模型 1 單位 = 1 公尺，紅軸=東、綠軸=北）",
        "# SketchUp：視窗 > Ruby Console，輸入  load '這個檔案的完整路徑'",
        "model = Sketchup.active_model",
        f"model.start_operation('鄰房白模 {name}', true)",
        "ents = model.active_entities",
        "tag = model.layers['鄰房白模'] || model.layers.add('鄰房白模')",
        "tag_on = model.layers['基地內既有建物'] || model.layers.add('基地內既有建物')",
        "tag_site = model.layers['基地範圍'] || model.layers.add('基地範圍')",
        "tag_man = model.layers['補充建物(手動)'] || model.layers.add('補充建物(手動)')",
        "tag_ter = model.layers['地形'] || model.layers.add('地形')",
        f"root = ents.add_group; root.name = '鄰房白模_{name}'; root.layer = tag",
        f"root_on = ents.add_group; root_on.name = '基地內既有建物_{name}'; root_on.layer = tag_on",
        f"root_man = ents.add_group; root_man.name = '補充建物_{name}'; root_man.layer = tag_man",
        "fails = 0",
        "B = [",
    ]
    for s in solids:
        holes = "[" + ",".join(pts(hl, s["z0"]) for hl in s["holes"]) + "]"
        L.append(f"  ['{s['id']}', '{s['floors']}', {s['h']:.2f}, {pts(s['outer'], s['z0'])}, {holes}, "
                 f"{'true' if s['onsite'] else 'false'}, {'true' if s['manual'] else 'false'}],")
    L += ["]", "SITE = ["]
    for no, p in site.items():
        if parcels3d:
            L.append(f"  ['{no}', [" + ",".join(f"[{q[0]:.3f}.m,{q[1]:.3f}.m,{q[2]:.3f}.m]" for q in parcels3d[no]) + "]],")
        else:
            L.append(f"  ['{no}', {pts(p, 0.02)}],")
    L += ["]", "TER_P = ["]
    if ter:
        L += [f"  [{q[0]:.2f},{q[1]:.2f},{q[2]:.3f}]," for q in ter[0]]
    L += ["]", "TER_T = ["]
    if ter:
        L += ["  " + ",".join(f"[{a + 1},{b + 1},{c + 1}]" for a, b, c in ter[1][k:k + 20]) + ","
              for k in range(0, len(ter[1]), 20)]
    L += [
        "]",
        "unless TER_P.empty?",
        f"  tg = ents.add_group; tg.name = '地形_{name}（內政部20m DTM）'; tg.layer = tag_ter",
        "  mesh = Geom::PolygonMesh.new(TER_P.size, TER_T.size)",
        "  TER_P.each { |x, y, z| mesh.add_point(Geom::Point3d.new(x.m, y.m, z.m)) }",
        "  TER_T.each { |a, b, c| mesh.add_polygon(a, b, c) }",
        "  mat = model.materials['地形'] || model.materials.add('地形')",
        "  mat.color = Sketchup::Color.new(214, 212, 204)",
        "  tg.entities.add_faces_from_mesh(mesh, Geom::PolygonMesh::AUTO_SOFTEN | Geom::PolygonMesh::SMOOTH_SOFT_EDGES, mat, mat)",
        "end",
        "unless SITE.empty?",
        f"  sg = ents.add_group; sg.name = '基地範圍_{name}'; sg.layer = tag_site",
        "  SITE.each do |no, ring|",
        "    sg.entities.add_edges(ring + [ring[0]])",
        "    c = ring.inject(Geom::Point3d.new(0, 0, 0)) { |a, p| a + Geom::Vector3d.new(p[0] / ring.size, p[1] / ring.size, p[2] / ring.size) }",
        "    sg.entities.add_text(no, c)",
        "  end",
        "end",
        "B.each do |bid, fl, h, outer, holes, onsite, manual|",
        "  begin",
        "    par, tg = onsite ? [root_on, tag_on] : manual ? [root_man, tag_man] : [root, tag]",
        "    g = par.entities.add_group",
        "    g.name = \"#{bid} #{fl}F #{h}m\"",
        "    g.layer = tg",
        "    f = g.entities.add_face(outer)",
        "    holes.each { |hl| hf = g.entities.add_face(hl); hf.erase! if hf && hf.valid? }",
        "    f = g.entities.grep(Sketchup::Face).max_by(&:area)",
        "    f.reverse! if f.normal.z < 0",
        "    f.pushpull(h.m)",
        "  rescue => e",
        "    fails += 1",
        "  end",
        "end",
        "tag_on.visible = false   # 基地內既有建物預設隱藏（要看就在標籤面板打開）",
        "model.commit_operation",
        "[root_on, root_man].each { |g| g.erase! if g.valid? && g.entities.length == 0 }   # 空群組（SketchUp 可能已自動清掉）",
        "puts \"鄰房白模：#{B.size - fails} 棟完成（基地內 #{B.count { |b| b[5] }} 棟已隱藏、手動補充 #{B.count { |b| b[6] }} 棟），#{fails} 棟失敗\"",
    ]
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")


def write_dxf(path, solids, oe, on, site, ter=None, zref=0.0):
    """R12 DXF：外框閉合聚合線（TWD97 公尺）+ 樓層數字 + 基地宗地線，可直接套到地籍/配置圖"""
    o = ["0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", "6", "0", "ENDSEC",
         "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER", "70", "6",
         "0", "LAYER", "2", "NB-OUTLINE", "70", "0", "62", "8", "6", "CONTINUOUS",
         "0", "LAYER", "2", "NB-FLOORS", "70", "0", "62", "1", "6", "CONTINUOUS",
         "0", "LAYER", "2", "NB-ONSITE", "70", "0", "62", "30", "6", "CONTINUOUS",
         "0", "LAYER", "2", "NB-MANUAL", "70", "0", "62", "4", "6", "CONTINUOUS",
         "0", "LAYER", "2", "SITE-PARCEL", "70", "0", "62", "6", "6", "CONTINUOUS",
         "0", "LAYER", "2", "TERRAIN", "70", "0", "62", "9", "6", "CONTINUOUS",
         "0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]

    def pline(loop, layer, dx=oe, dy=on):
        r = ["0", "POLYLINE", "8", layer, "66", "1", "70", "1"]
        for p in loop:
            r += ["0", "VERTEX", "8", layer, "10", f"{p[0] + dx:.3f}", "20", f"{p[1] + dy:.3f}", "30", "0"]
        return r + ["0", "SEQEND", "8", layer]

    for no, p in site.items():
        o += pline(p, "SITE-PARCEL")
        c = p.mean(0)
        o += ["0", "TEXT", "8", "SITE-PARCEL", "10", f"{c[0] + oe:.3f}", "20", f"{c[1] + on:.3f}", "30", "0",
              "40", "1.0", "1", no, "72", "1", "73", "2", "11", f"{c[0] + oe:.3f}", "21", f"{c[1] + on:.3f}", "31", "0"]
    for s in solids:
        for loop in [s["outer"]] + s["holes"]:
            o += pline(loop, "NB-ONSITE" if s["onsite"] else "NB-MANUAL" if s["manual"] else "NB-OUTLINE")
        if s["floors"]:
            o += ["0", "TEXT", "8", "NB-FLOORS", "10", f"{s['cx'] + oe:.3f}", "20", f"{s['cy'] + on:.3f}",
                  "30", "0", "40", "1.5", "1", f"{s['floors']}F", "72", "1", "73", "2",
                  "11", f"{s['cx'] + oe:.3f}", "21", f"{s['cy'] + on:.3f}", "31", "0"]
    if ter:   # 地形 3DFACE，Z = TWVD2001 高程
        P = ter[0]
        for a, b, c in ter[1]:
            o += ["0", "3DFACE", "8", "TERRAIN"]
            for k, v in enumerate((a, b, c, c)):
                o += [str(10 + k), f"{P[v][0] + oe:.3f}", str(20 + k), f"{P[v][1] + on:.3f}", str(30 + k), f"{P[v][2] + zref:.3f}"]
    o += ["0", "ENDSEC", "0", "EOF"]
    open(path, "w", encoding="ascii").write("\n".join(o) + "\n")


def write_obj(path, solids, ter=None):
    """OBJ 預覽用（Z-up 轉 Y-up）；中庭簡化為不挖空"""
    v, f, out = 1, [], ["# NLSC 分棟 LOD1 白模"]
    for s in solids:
        out.append(f"o {'ONSITE_' if s['onsite'] else 'MANUAL_' if s['manual'] else ''}{s['id']}")
        loop = s["outer"]; n = len(loop); z0, z1 = s["z0"], s["z0"] + s["h"]
        for z in (z0, z1):
            for p in loop:
                out.append(f"v {p[0]:.3f} {z:.3f} {-p[1]:.3f}")
        out.append("f " + " ".join(str(v + n + i) for i in range(n)))
        out.append("f " + " ".join(str(v + i) for i in reversed(range(n))))
        for i in range(n):
            j = (i + 1) % n
            out.append(f"f {v + i} {v + j} {v + n + j} {v + n + i}")
        v += 2 * n
    if ter:
        out.append("o TERRAIN")
        out += [f"v {p[0]:.3f} {p[2]:.3f} {-p[1]:.3f}" for p in ter[0]]
        out += [f"f {v + a} {v + b} {v + c}" for a, b, c in ter[1]]
    open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
