#!/usr/bin/env python3
"""把 BA 原生 3D 模型匯出物搬進 Unity（聊天頁立繪那一格的 3D 切換，owner 2026-10-30）。

來源是 `blueagent/3d2/<tag>/`（`<tag>.fbx`＋`<tag>.glb`＋`textures/*.png`，見那邊的 README.md；v2 帶了嘴型圖集
與 GLB 材質上的 `extras.ba_texenv` 槽位表）。
產物：`BlueAgentUnity/Assets/Resources/models3d/<角色 id>/`
  model.fbx       ← 原樣複製（Unity 端的匯入設定見 `Editor/Stage3DModelPostprocessor.cs`）
  <貼圖>.png      ← 只複製「要留下的網格」用得到的那幾張
  stage3d.json    ← 執行期組裝表：留哪些節點、哪些掛到頭上、材質 → 貼圖、嘴在哪一層＋用哪張圖集
`Resources/models3d/<圖集>.png`（例 `Character_Mouth.png`）← 全角色共用的嘴型圖集（8×8 格），只放一份

為什麼要從 GLB 讀對照：這個專案**不進版控 `.meta`**（GUID 參照會斷），材質不能在編輯器裡接好貼圖，
只能執行期照名字接；而「材質名 → 貼圖名」不是一對一（`Mira_Newyear_Face` 用的是
`Mira_Original_Face`），這張表只在 GLB 裡有。

留下的節點：名字以 `_Body` 結尾（人形本體，有蒙皮）或 `_Halo` 結尾（光環）。武器、EX 道具、
`FX_MESH_*` 一律不帶——取景量的是留下來那些 Renderer 的 bounds，道具會把框撐大。

用法：python3 tools/sync_stage3d_models.py [來源目錄，預設 ../../3d2]
"""
import json
import pathlib
import shutil
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
UNITY = HERE.parent
DST_ROOT = UNITY / "BlueAgentUnity" / "Assets" / "Resources" / "models3d"

#: 角色 id（伺服器的 entity）→ 匯出物的 tag
CHARACTERS = {
    "mira": "mira_newyear",
    "aster": "ch0158",
    "nori": "ch0335",
}


def read_glb(path: pathlib.Path) -> dict:
    data = path.read_bytes()
    magic, _version, _length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise SystemExit(f"不是 GLB：{path}")
    chunk_len, chunk_type = struct.unpack_from("<I4s", data, 12)
    if chunk_type != b"JSON":
        raise SystemExit(f"GLB 第一塊不是 JSON：{path}")
    return json.loads(data[20:20 + chunk_len])


def kept(name: str) -> bool:
    return name.endswith("_Body") or name.endswith("_Halo")


def build(character: str, tag: str, src_root: pathlib.Path) -> None:
    src = src_root / tag
    gltf = read_glb(src / f"{tag}.glb")
    nodes, meshes = gltf["nodes"], gltf["meshes"]
    materials, textures, images = gltf["materials"], gltf.get("textures", []), gltf.get("images", [])

    keep, attach_head, material_textures, mouth = [], [], {}, None
    for node in nodes:
        name = node.get("name", "")
        if "mesh" not in node or not kept(name) or name in keep:
            continue
        keep.append(name)
        # 沒蒙皮的光環停在原始 Transform（不跟頭動）⇒ 執行期掛到頭骨底下
        if "skin" not in node:
            attach_head.append(name)
        for primitive in meshes[node["mesh"]]["primitives"]:
            if "material" not in primitive:
                continue
            material = materials[primitive["material"]]
            texture = material.get("pbrMetallicRoughness", {}).get("baseColorTexture")
            if texture is None:
                continue
            image = images[textures[texture["index"]]["source"]].get("name")
            if image:
                material_textures[material["name"]] = image
            # 嘴不在角色自己的貼圖上：BA 的 shader 用參數在共用圖集裡選格（README §5）
            atlas = material.get("extras", {}).get("ba_texenv", {}).get("_MouthTileTex")
            if atlas:
                mouth = {"layer": material["name"], "atlas": atlas}

    dst = DST_ROOT / character
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    shutil.copy2(src / f"{tag}.fbx", dst / "model.fbx")
    for image in sorted(set(material_textures.values())):
        source = src / "textures" / f"{image}.png"
        if not source.exists():
            raise SystemExit(f"貼圖不在 textures/：{source}")
        shutil.copy2(source, dst / f"{image}.png")
    if mouth:
        atlas = src / "textures" / f"{mouth['atlas']}.png"
        if not atlas.exists():
            raise SystemExit(f"嘴型圖集不在 textures/：{atlas}")
        shutil.copy2(atlas, DST_ROOT / f"{mouth['atlas']}.png")

    table = {
        "source": tag,
        "keep": keep,
        "attachHead": attach_head,
        "textures": material_textures,
    }
    if mouth:
        table["mouth"] = mouth            # 執行期把嘴搬到自己的子網格、畫圖集（見 Stage3DRig.SplitMouth）
    (dst / "stage3d.json").write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✓ {character} ⇐ {tag}：留 {keep}，貼圖 {len(set(material_textures.values()))} 張")


def main() -> None:
    src_root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else UNITY.parent.parent / "3d2"
    if not src_root.is_dir():
        raise SystemExit(f"找不到來源目錄：{src_root}")
    for character, tag in CHARACTERS.items():
        build(character, tag, src_root)


if __name__ == "__main__":
    main()
