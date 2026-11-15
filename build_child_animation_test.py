"""Stage Young Link animation resources on the Adult actor for isolated testing."""

import sys
from struct import iter_unpack, pack_into, unpack_from
from zlib import crc32

from build_voices import DECOMPRESS, INDEX, RESTBL, ROOT, read_range, u32

sys.path.insert(0, str(ROOT / "reference/tools"))
import byml
import zsmod
from asbtool import Asb, COMMAND, check
from zstool import Sarc


def replace_pack_member(raw, archive, name, data):
    """Grow one member while retaining the original eight-byte archive alignment."""
    start, end = archive.files[name]
    suffix = (end + 7) & ~7
    assert start % 8 == 0 and not any(raw[end:suffix])
    assert all(offset % 8 == 0 for offset, _ in archive.files.values())
    padded = data + bytes(-len(data) % 8)
    delta = len(padded) - (suffix - start)
    out = bytearray(raw[:start] + padded + raw[suffix:])
    pack_into("<I", out, 8, len(out))
    count = unpack_from("<H", raw, 0x1A)[0]
    for i in range(count):
        node = 0x20 + i * 16
        first, last = unpack_from("<II", raw, node + 8)
        absolute = archive.data_at + first
        if absolute == start:
            pack_into("<I", out, node + 12, first + len(data))
        elif absolute >= suffix:
            pack_into("<II", out, node + 8, first + delta, last + delta)
    return bytes(out)


def build(child_camera=False, adult_fallback=False, equipment_fallback=False,
          equipment_slot_test=False, native_child_model=False):
    dictionaries = zsmod.dicts_by_id(ROOT / "assets/ZsDic.pack.zs")
    folder = "Forever Young Child Animation Test"
    if child_camera:
        folder = "Forever Young Child Framing Test"
    if adult_fallback:
        folder = "Forever Young Adult Animation Fallback Test"
    if equipment_fallback:
        folder = "Forever Young Equipment Animation Test"
    if equipment_slot_test:
        folder = "Forever Young Equipment Slot Test"
    if native_child_model:
        folder = "Forever Young Native Child Model Test"
    output = ROOT / folder / "romfs"
    adult_pack = "Pack/Actor/Player_Adult.pack.zs"
    child_pack = "Pack/Actor/Player_Child.pack.zs"
    adult_data, pack_dictionary = zsmod.unzs(read_range(adult_pack), dictionaries)
    child_data, _ = zsmod.unzs(read_range(child_pack), dictionaries)
    adult, child = Sarc(adult_data), Sarc(child_data)
    param = "Component/AnimationParam/Player.engine__component__AnimationParam.bgyml"
    child_param = param.replace("/Player.", "/Player_Child.")
    adult_anim = byml.loads(adult.read(param))
    child_anim = byml.loads(child.read(child_param))
    adult_projects = adult_anim["AnimationResources"]
    child_projects = child_anim["AnimationResources"]
    child_names = {entry["ModelProjectName"] for entry in child_projects}
    for name in child_names:
        assert f"Model/{name}.anim.bfres.zs" in INDEX
    fallback = adult_projects if adult_fallback else [
        entry for entry in adult_projects
        if entry["ModelProjectName"].replace("Player_Adult_", "Player_Child_") not in child_names
        or (equipment_fallback or equipment_slot_test) and entry["ModelProjectName"] == "Player_Adult_01_Animation"
    ]
    if equipment_slot_test:
        fallback = [entry for entry in fallback if entry["ModelProjectName"] != "Player_Dark_Animation"]
    adult_anim["AnimationResources"] = [*child_projects, *fallback]
    files = {name: adult.read(name) for name in adult.names()}
    files[param] = byml.dumps(adult_anim)
    if native_child_model:
        model_info = "Component/ModelInfo/Player.engine__component__ModelInfo.bgyml"
        model = byml.loads(adult.read(model_info))
        assert model == {"FmdbName": "Player_Adult", "ModelProjectName": "Player_Adult"}
        assert "Model/Player_Child.Player_Child.bfres.zs" in INDEX
        files[model_info] = byml.dumps({key: "Player_Child" for key in model})
    if child_camera:
        camera = "Component/PlayerCameraTargetParam/Player.game__component__PlayerCameraTargetParam.bgyml"
        child_camera_param = camera.replace("/Player.", "/Player_Child.")
        original_camera = byml.loads(adult.read(camera))
        stock_child_camera = byml.loads(child.read(child_camera_param))
        for key in ("TargetHeight", "TargetRadius"):
            original_camera[key] = stock_child_camera[key]
        files[camera] = byml.dumps(original_camera)
    if equipment_fallback or equipment_slot_test:
        rebuilt = replace_pack_member(adult_data, adult, param, files[param])
    else:
        rebuilt = zsmod.sarc_pack(files, like=adult_data)
    reread = Sarc(rebuilt)
    assert set(reread.names()) == set(files)
    assert all(reread.read(name) == data for name, data in files.items())
    assert byml.loads(reread.read(param)) == adult_anim
    size_table = bytearray(DECOMPRESS.decompress(read_range(RESTBL)))
    assert size_table[:10] == b"RESTBL\1\0\0\0"
    allocations = iter_unpack("<II", size_table[22:22+8*u32(size_table, 14)])
    pack_crc = crc32(adult_pack.removesuffix(".zs").encode())
    (entry,) = [(i, size) for i, (key, size) in enumerate(allocations) if key == pack_crc]
    index, reserve = entry
    if len(rebuilt) > reserve:
        assert (adult_fallback or equipment_fallback) and reserve >= len(adult_data)
        new_reserve = reserve + len(rebuilt) - len(adult_data)
        pack_into("<I", size_table, 26 + 8 * index, new_reserve)
        table = output / RESTBL
        table.parent.mkdir(parents=True, exist_ok=True)
        table.write_bytes(zsmod.zs(size_table, 0, dictionaries, level=3))
    elif equipment_fallback:
        (output / RESTBL).unlink(missing_ok=True)
    pack_path = output / adult_pack
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    frame = zsmod.zs(rebuilt, pack_dictionary, dictionaries, level=3)
    assert zsmod.unzs(frame, dictionaries) == (rebuilt, pack_dictionary)
    pack_path.write_bytes(frame)
    print("Animation projects:", [entry["ModelProjectName"] for entry in adult_anim["AnimationResources"]])

    adult_graph = "AS/Player_Adult.root.asb.zs"
    child_graph = "AS/Player_Child.root.asb.zs"
    old_graph, graph_dictionary = zsmod.unzs(read_range(adult_graph), dictionaries)
    child_graph_data, child_dictionary = zsmod.unzs(read_range(child_graph), dictionaries)
    old_name, new_name = b"Player_Child.root\0", b"Player_Adult.root\0"
    assert child_graph_data.count(old_name) == old_graph.count(new_name) == 1
    assert len(child_graph_data) == len(old_graph) and graph_dictionary == child_dictionary
    graph = child_graph_data.replace(old_name, new_name, 1)
    source_graph, patched_graph = Asb(old_graph), Asb(graph)
    for controller in (source_graph, patched_graph):
        assert not check(controller)
    def command_names(controller):
        return {controller.string(controller.u32(controller.commands + i * COMMAND))
                for i in range(controller.n_commands)}
    assert command_names(source_graph) == command_names(patched_graph)
    graph_frame = zsmod.zs(graph, graph_dictionary, dictionaries, level=3)
    assert zsmod.unzs(graph_frame, dictionaries) == (graph, graph_dictionary)
    graph_path = output / adult_graph
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_bytes(graph_frame)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    variants = parser.add_mutually_exclusive_group()
    variants.add_argument("--child-camera", action="store_true")
    variants.add_argument("--adult-fallback", action="store_true")
    variants.add_argument("--equipment-fallback", action="store_true")
    variants.add_argument("--equipment-slot-test", action="store_true")
    variants.add_argument("--native-child-model", action="store_true")
    args = parser.parse_args()
    build(args.child_camera, args.adult_fallback, args.equipment_fallback,
          args.equipment_slot_test, args.native_child_model)
