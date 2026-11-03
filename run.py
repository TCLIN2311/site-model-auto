#!/usr/bin/env python3
"""
基地模一鍵產生：site_model.py（建物＋地形）→ SketchUp 存成 .skp → Blender 預覽圖

用法：
  python3 run.py --lat 25.047333 --lon 121.516694
  python3 run.py --lat 25.047333 --lon 121.516694 --radius 500 --name 範例案 --out ~/某資料夾
  其餘參數（--site --extra --flat --county …）原樣傳給 site_model.py
"""
import argparse, glob, json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
WIN = os.name == "nt"


def newest(patterns):
    """多個 glob 樣式中版本最新的一個（依含版本號的上層資料夾名排序）；都沒有回傳 None"""
    hits = [p for pat in patterns for p in glob.glob(pat)]
    return max(hits, key=lambda p: os.path.basename(os.path.dirname(p))) if hits else None


def find_sketchup():
    """環境變數 SITE_MODEL_SKETCHUP 優先，否則找已安裝版本最新的 SketchUp"""
    if os.environ.get("SITE_MODEL_SKETCHUP"):
        return os.environ["SITE_MODEL_SKETCHUP"]
    if WIN:
        pf = [os.environ.get(k) for k in ("ProgramFiles", "ProgramW6432") if os.environ.get(k)]
        return newest([os.path.join(d, "SketchUp", "SketchUp 20*", "SketchUp.exe") for d in pf])
    return newest([os.path.join(d, "SketchUp 20*", "SketchUp.app")
                   for d in ("/Applications", os.path.expanduser("~/Applications"))])


def find_blender():
    """環境變數 SITE_MODEL_BLENDER 優先，其次 PATH 上的 blender，再找預設安裝位置"""
    if os.environ.get("SITE_MODEL_BLENDER"):
        return os.environ["SITE_MODEL_BLENDER"]
    if shutil.which("blender"):
        return shutil.which("blender")
    if WIN:
        pf = [os.environ.get(k) for k in ("ProgramFiles", "ProgramW6432") if os.environ.get(k)]
        return newest([os.path.join(d, "Blender Foundation", "Blender*", "blender.exe") for d in pf])
    mac = "/Applications/Blender.app/Contents/MacOS/Blender"
    return mac if os.path.exists(mac) else None


def default_root():
    """環境變數 SITE_MODEL_OUT 優先；否則 ~/Downloads/基地模製作（Mac、Windows 都有下載資料夾）"""
    if os.environ.get("SITE_MODEL_OUT"):
        return os.path.expanduser(os.environ["SITE_MODEL_OUT"])
    dl = os.path.expanduser("~/Downloads")
    return os.path.join(dl if os.path.isdir(dl) else os.path.expanduser("~"), "基地模製作")


SKETCHUP = find_sketchup()
BLENDER = find_blender()
DEFAULT_ROOT = default_root()


def q(p):
    """路徑轉成 Ruby 單引號字串（Windows 反斜線改正斜線）"""
    return "'" + p.replace("\\", "/").replace("'", "\\'") + "'"


def copy_clipboard(text):
    """複製到剪貼簿（Mac pbcopy、Windows PowerShell Set-Clipboard）；失敗就算了"""
    try:
        if WIN:
            subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $input"],
                           input=text, encoding="utf-8", check=True)
        else:
            subprocess.run(["pbcopy"], input=text, encoding="utf-8", check=True,
                           env={**os.environ, "LANG": "en_US.UTF-8"})   # 沒設 LANG 時 pbcopy 會把中文當成別的編碼
        return True
    except Exception:
        return False


def make_skp(rb, skp, log):
    """另開一個 SketchUp（Mac 用 open -n，不影響使用者已開的視窗）載入 .rb、存檔、關閉。
    先存在暫存資料夾再搬過去：直接存進 Synology Drive 同步資料夾會卡住"""
    tmp = tempfile.mkdtemp()
    final_skp, final_log = skp, log
    skp, log = os.path.join(tmp, "model.skp"), os.path.join(tmp, "log.txt")
    shutil.copy(os.path.join(HERE, "template.skp"), os.path.join(tmp, "template.skp"))
    startup = os.path.join(tmp, "build.rb")
    open(startup, "w", encoding="utf-8").write(f"""UI.start_timer(3, false) {{
  begin
    m = Sketchup.active_model
    m.entities.clear!; m.layers.purge_unused; m.materials.purge_unused; m.definitions.purge_unused
    load {q(rb)}
    info = m.entities.grep(Sketchup::Group).map {{ |r|
      subs = r.entities.grep(Sketchup::Group)
      flat = subs.count {{ |g| g.entities.grep(Sketchup::Face).size < 5 }}
      "#{{r.name}} [#{{r.layer.name}}] 群組=#{{subs.size}} 面=#{{r.entities.grep(Sketchup::Face).size}} 線=#{{r.entities.grep(Sketchup::Edge).size}} 未推拉=#{{flat}}"
    }}
    ok = m.save({q(skp)})
    File.write({q(log)}, info.join("\\n") + "\\nsaved=#{{ok}}\\n")
  rescue => e
    File.write({q(log)}, "ERR #{{e.message}}\\n#{{e.backtrace.first(5).join("\\n")}}")
  end
  UI.start_timer(2, false) {{ Sketchup.quit }}
}}
""")
    args = ["-RubyStartup", startup, os.path.join(tmp, "template.skp")]
    if WIN:
        subprocess.Popen([SKETCHUP] + args)
    else:
        subprocess.run(["open", "-n", "-a", SKETCHUP, "--args"] + args)
    for _ in range(200):
        if os.path.exists(log):
            time.sleep(1)
            if os.path.exists(skp):
                shutil.copy(skp, final_skp)
            shutil.copy(log, final_log)
            return open(log, encoding="utf-8").read()
        time.sleep(3)
    return "ERR SketchUp 10 分鐘內沒有完成"


def render(obj, png, radius):
    """Workbench 鳥瞰預覽：白=鄰房、橘=基地內既有、藍=手動補充、灰綠=地形"""
    d = radius * 1.15
    py = os.path.join(tempfile.mkdtemp(), "render.py")
    open(py, "w", encoding="utf-8").write(f"""import bpy, mathutils
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.obj_import(filepath={obj!r})
for o in bpy.context.scene.objects:
    n = o.name
    o.color = (1.0,0.55,0.1,1) if n.startswith('ONSITE_') else (0.35,0.6,1.0,1) if n.startswith('MANUAL_') else (0.72,0.74,0.62,1) if n == 'TERRAIN' else (0.93,0.93,0.91,1)
    if n == 'TERRAIN':
        for p in o.data.polygons: p.use_smooth = True
sc = bpy.context.scene
cd = bpy.data.cameras.new('c'); cam = bpy.data.objects.new('c', cd); sc.collection.objects.link(cam); cd.lens = 30
cam.location = ({d * 1.0:.1f}, {-d * 1.15:.1f}, {d * 0.9:.1f})
cam.rotation_euler = (mathutils.Vector((0, 0, 0)) - cam.location).to_track_quat('-Z', 'Y').to_euler()
cd.clip_end = {d * 6:.0f}
sc.camera = cam; sc.render.engine = 'BLENDER_WORKBENCH'
sh = sc.display.shading; sh.light = 'STUDIO'; sh.color_type = 'OBJECT'; sh.show_shadows = True; sh.show_cavity = True
sc.render.resolution_x, sc.render.resolution_y = 1600, 1000; sc.render.filepath = {png!r}
bpy.ops.render.render(write_still=True)
""")
    r = subprocess.run([BLENDER, "-b", "--python", py], capture_output=True, encoding="utf-8", errors="replace")
    return os.path.exists(png) and "Saved" in r.stdout


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows 主控台預設 cp950，印不出 ▶ ⚠ 等符號
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius", type=float, default=500)
    ap.add_argument("--name")
    ap.add_argument("--out")
    ap.add_argument("--no-skp", action="store_true")
    ap.add_argument("--no-preview", action="store_true")
    a, rest = ap.parse_known_args()

    # 案名未給：用座標查行政區＋地段當資料夾名（site_model 也會用同一個名字當檔名）
    out = os.path.expanduser(a.out) if a.out else None
    if not out:
        tag = a.name
        if not tag and "--lat" in rest and "--lon" in rest:
            sys.path.insert(0, HERE)
            from site_model import lookup_place
            _, town, sect = lookup_place(float(rest[rest.index("--lat") + 1]), float(rest[rest.index("--lon") + 1]))
            tag = town + sect
        tag = tag or time.strftime("基地模_%Y%m%d_%H%M")
        out = os.path.join(DEFAULT_ROOT, f"{tag}_輸出")
    os.makedirs(out, exist_ok=True)
    cmd = [sys.executable, os.path.join(HERE, "site_model.py"), "--radius", str(a.radius), "--out", out] + rest
    if a.name:
        cmd += ["--name", a.name]
    print("▶", " ".join(cmd), flush=True)
    if subprocess.run(cmd).returncode:
        sys.exit("site_model.py 失敗")

    meta = max(glob.glob(os.path.join(out, "*_meta.json")), key=os.path.getmtime)
    stem = meta[:-len("_meta.json")]
    if not a.no_skp and not SKETCHUP:
        print("⚠ 找不到 SketchUp 20xx，略過 .skp；可設環境變數 SITE_MODEL_SKETCHUP 指定，"
              "或在 SketchUp「視窗 > Ruby 主控台」load 輸出的 _SketchUp.rb")
    elif not a.no_skp:
        print("▶ SketchUp 建模中（會另開一個 SketchUp 視窗，完成後自動關閉）…", flush=True)
        log = make_skp(stem + "_SketchUp.rb", stem + "_基地模.skp", stem + "_skp_log.txt")
        print(log)
    if not a.no_preview and BLENDER:
        print("▶ 預覽圖：", "OK" if render(stem + "_白模.obj", stem + "_預覽.png", a.radius) else "失敗")
    m = json.load(open(meta, encoding="utf-8"))
    print(f"\n完成：{m['place']}  建物 {m['buildings']} 棟  半徑 {m['radius']:.0f} m")
    print("輸出資料夾：", out)
    for f in sorted(os.listdir(out)):
        print("  ", f)

    # 一行貼進 SketchUp「視窗 > Ruby 主控台」就能建出模型（沒存成 .skp 時用）
    load = f"load {q(stem + '_SketchUp.rb')}"
    print("\nSketchUp「視窗 > Ruby 主控台」貼上這行即可建出模型"
          + ("（已複製到剪貼簿）" if copy_clipboard(load) else "") + "：")
    print(load)


if __name__ == "__main__":
    main()
