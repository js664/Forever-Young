from collections import defaultdict
from functools import lru_cache
from itertools import permutations
import csv
import json
import os
from pathlib import Path
import re
from struct import iter_unpack, pack_into, unpack_from
import zlib

import zstandard as zstd

ROOT = Path(__file__).resolve().parent
GAME = Path(os.environ["LOCALAPPDATA"]) / "Programs/NX2navi/game"
ARCHIVE = GAME / "romfs"
INDEX = {e["path"]: e for e in json.loads((ARCHIVE / "index.json").read_text())["files"]}
BANK = "Sound/Resource/Player_{}.bars.zs"
STREAM = "Sound/Resource/Stream/Vo_Pl{}_{}.bwav"
RESTBL = "System/Resource/ResourceSizeTable.Product.111.Nin_Ounce_NVN2.rsizetable.zs"
DECOMPRESS = zstd.ZstdDecompressor()


@lru_cache(maxsize=350)
def read_range(path):
    entry = INDEX[path]
    cached = ROOT / "assets" / Path(path).name
    if cached.is_file() and cached.stat().st_size == entry["size"]:
        return cached.read_bytes()
    with (ARCHIVE / "romfs.bin").open("rb") as archive:
        archive.seek(entry["offset"])
        data = archive.read(entry["size"])
    if len(data) != entry["size"]:
        raise IOError(f"Short indexed read for {path}")
    return data


def u32(data, offset):
    return unpack_from("<I", data, offset)[0]


class Bank:
    def __init__(self, age, data):
        self.age = age
        self.data = data
        assert data[:4] == b"BARS" and data[8:12] == b"\xff\xfe\x02\x01"
        assert u32(data, 4) == len(data)
        count = u32(data, 12)
        self.offset_table = 16 + 4*count
        self.entries = list(iter_unpack("<II", data[self.offset_table:self.offset_table + 8*count]))
        offsets = sorted({wave for _, wave in self.entries})
        self.waves = {start: data[start:end] for start, end in zip(offsets, offsets[1:] + [len(data)])}
        assert all(start % 64 == 0 and wave.startswith(b"BWAV") for start, wave in self.waves.items())
        self.voices = {}
        pattern = re.compile(rb"Vo_Pl" + age.encode() + rb"_([A-Za-z0-9_]+)\0")
        for i, (meta, wave) in enumerate(self.entries):
            assert data[meta:meta+4] == b"AMTA"
            record = data[meta:meta + u32(data, meta+8)]
            match = pattern.search(record)
            if match:
                name = match[1].decode()
                assert name not in self.voices
                self.voices[name] = i, wave

    def clip(self, name, prefetch):
        _, offset = self.voices[name]
        embedded = self.waves[offset]
        path = STREAM.format(self.age, name)
        if prefetch and embedded[12] == 1:
            assert path in INDEX
            return embedded
        if embedded[12] == 1:
            full = read_range(path)
            assert full[:12] == embedded[:12] and full[12] == 0
            assert full[14:28] == embedded[14:28]
            return full
        return embedded


# Adult-only cues reuse a Young Link take from a comparable action.
FAMILIES = {
    "BS_JumpAttack": "JumpAttack", "H_JumpAttack": "JumpAttack",
    "Hammer": "SwordAttack_B", "HookShot": "Boomerang",
    "HorseGetoff": "Jump_S", "HorseJumpoff": "Jump_B",
    "HorseRide": "Jump_S", "HorseSpurs_B": "SwordAttack_M",
    "HorseSpurs_S": "SwordAttack_S", "JumpCliffs": "Jump_B", "GrabJump": "ClimbJump",
    "LavaDamage": "FireDamage_L", "NormalBlock": "Shield_S",
    "RollAttackDamage_S": "RollAttackDamage_B", "Shoes": "Satisfaction",
    "SilverGlove_LiftUp": "lifting", "SilverGlove_LiftUpStart": "lifting",
    "SilverGlove_LiftUpEnd": "Release", "SilverGlove_Throw": "ThrowingBom",
    "TreasureBox_B1": "TresureBox_B_OpenBox", "TreasureBox_B2": "TresureBox_B_RummageInBox",
    "ZeroMasterSword": "ZeroTool",
    "Dm_Gerudo": "Surprised", "Dm_InsideGanonCastle": "HeavyBbreathing",
    "Dm_MelodyGet_Spilit": "Surprised", "Dm_SageRoom": "Nod",
}
# Cutscene-only Gold Gauntlets clips have no Child take for the same scene.
GOLD_GAUNTLETS = {
    "B_00": "HeavyBbreathing_04",
    "B_01": "HeavyBbreathing_05",
    "B_02": "HeavyBbreathing_03",
    "C_00": "Push_03",
    "C_01": "Push_04",
    "C_02": "Release_02",
}
WAIT_FALLBACK = ("Wait_10", "Wait_11", "Wait_12", "Wait_22", "Wait_23", "Wait_24")


def pick(name, choices):
    if name in choices:
        return name, "exact"
    for spell, candidate in (("DinsFire", "DinsFire_"), ("FaroresWind", "FaroresWind"),
                             ("NayrsLove", "NayrsLove_")):
        match = re.fullmatch(spell + r"_(\d+)_(Charge|Shoot)", name)
        if match:
            name = candidate + match[2] + "_" + match[1]
            break
    if name in choices:
        return name, "renamed"
    if name + "_alt" in choices:
        return name + "_alt", "alternate take"
    family = re.sub(r"\d+(?:_\d+)*$", "", name).rstrip("_")
    if name.startswith(("TreasureBox_B1_", "TreasureBox_B2_")):
        family = name.rsplit("_", 1)[0]
    if name.startswith("BigSwordAttack_"):
        family = family.removeprefix("Big")
    if name.startswith("Dm_"):
        family = next(key for key in FAMILIES if key.startswith("Dm_") and name.startswith(key))
    target_family = FAMILIES.get(family, family)
    variants = [v for v in sorted(choices) if re.match(re.escape(target_family) + r"_?\d", v)]
    if family == "Wait":
        wait_number = name.split("_")[1]
        variants = [v for v in variants if v.split("_")[1] == wait_number] or list(WAIT_FALLBACK)
    assert variants, f"No Young Link fallback for {name} ({family})"
    ordinal = int(re.findall(r"\d+", name)[-1])
    return variants[(ordinal-1) % len(variants)], "approximate"


def build_bank(adult, child, mapping):
    first_wave = min(adult.waves)
    output = bytearray(adult.data[:first_wave])
    replacements = {adult.voices[name][0]: child.clip(source, adult.waves[adult.voices[name][1]][12] == 1)
                    for name, (source, _) in mapping.items()}
    relocated = {}
    for i, (_, offset) in enumerate(adult.entries):
        wave = replacements.get(i, adult.waves[offset])
        key = offset, wave if i in replacements else None
        if key not in relocated:
            output.extend(b"\0" * (-len(output) % 64))
            relocated[key] = len(output)
            # Padding from the original allocation may be present in embedded takes.
            output.extend(wave)
        pack_into("<I", output, adult.offset_table + 8*i + 4, relocated[key])
    pack_into("<I", output, 4, len(output))
    reparsed = Bank("Adult", bytes(output))
    assert reparsed.voices.keys() == adult.voices.keys()
    for i, (meta, offset) in enumerate(adult.entries):
        new_meta, new_offset = reparsed.entries[i]
        assert new_meta == meta
        expected = replacements.get(i, adult.waves[offset])
        assert reparsed.waves[new_offset].startswith(expected)
    return bytes(output)


def update_restbl(size):
    table = bytearray(DECOMPRESS.decompress(read_range(RESTBL)))
    assert table[:10] == b"RESTBL\1\0\0\0"
    count = u32(table, 14)
    crc = zlib.crc32(BANK.format("Adult").removesuffix(".zs").encode())
    for offset in range(22, 22+8*count, 8):
        if u32(table, offset) == crc:
            original = u32(table, offset+4)
            reserve = max(original, (size + 600 + 7) & ~7)
            pack_into("<I", table, offset+4, reserve)
            return bytes(table), original, reserve
    raise ValueError("Adult voice bank missing from RESTBL")


def write_file(root, relative, data, compress=False):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if compress:
        packed = zstd.ZstdCompressor().compress(data)
        assert DECOMPRESS.decompress(packed) == data
        data = packed
    path.write_bytes(data)


def timed_equipment_mapping(adult, child, mapping):
    """Match clip duration within the established action families."""
    def duration(clip):
        rate, samples = unpack_from("<II", clip, 20)
        return samples / rate

    groups = defaultdict(list)
    for name, (take, method) in mapping.items():
        if method != "approximate" or not name.startswith(("Hammer_", "HookShot_", "Horse", "SilverGlove_")):
            continue
        groups[re.sub(r"\d+$", "", name)].append((name, take))
    for rows in groups.values():
        family = re.sub(r"\d+$", "", rows[0][1])
        candidates = {cue: duration(child.clip(cue, False)) for cue in sorted(child.voices)
                      if re.fullmatch(re.escape(family) + r"\d+", cue)}
        if len(candidates) < len(rows):
            continue
        stock = [duration(adult.clip(name, False)) for name, _ in rows]
        previous = tuple(take for _, take in rows)
        best = min(permutations(candidates, len(rows)), key=lambda takes: (
            sum(abs(candidates[take] - seconds) for take, seconds in zip(takes, stock)),
            sum(take != old for take, old in zip(takes, previous)),
        ))
        for (name, old), take in zip(rows, best):
            if take != old:
                mapping[name] = take, "duration-matched approximation"


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline-candidate", action="store_true")
    args = parser.parse_args()
    adult, child = (Bank(age, DECOMPRESS.decompress(read_range(BANK.format(age))))
                    for age in ("Adult", "Child"))
    mapping = {name: pick(name, child.voices) for name in sorted(adult.voices)}
    if args.offline_candidate:
        timed_equipment_mapping(adult, child, mapping)
    rebuilt = build_bank(adult, child, mapping)
    table, original, reserve = update_restbl(len(rebuilt))
    streams = {STREAM.format("Adult", name): child.clip(source, False)
               for name, (source, _) in mapping.items() if STREAM.format("Adult", name) in INDEX}
    scene_streams = {
        f"Sound/Resource/Stream/Dm_Vo_Player_GoldGlove_{suffix}.bwav": take
        for suffix, take in GOLD_GAUNTLETS.items()
    }
    for path, take in scene_streams.items():
        original_clip = read_range(path)
        replacement = child.clip(take, False)
        assert original_clip[:8] == replacement[:8] == b"BWAV\xff\xfe\x01\0"
        assert original_clip[14:24] == replacement[14:24]
        assert len(replacement) <= len(original_clip)
        streams[path] = replacement
    folder = ROOT / "Forever Young Voice Candidate" if args.offline_candidate else ROOT
    output = (folder if args.offline_candidate else ROOT / "Forever Young") / "romfs"
    for path, clip in streams.items():
        assert clip[:4] == b"BWAV" and clip[12] == 0
        write_file(output, path, clip)
    write_file(output, BANK.format("Adult"), rebuilt, compress=True)
    if reserve > original:
        write_file(output, RESTBL, table, compress=True)
    with (folder / "voice-mapping.tsv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(("adult_cue", "child_take", "match"))
        for name, (source, method) in mapping.items():
            writer.writerow((name, source, method))
        for path, take in scene_streams.items():
            writer.writerow((Path(path).stem, take, "cutscene approximation"))
    print(f"{len(mapping)} bank cues, {len(streams)} loose BWAV replacements")
    print(f"Bank {len(adult.data):,} to {len(rebuilt):,} bytes; RESTBL {original:,} to {reserve:,}")


if __name__ == "__main__":
    main()
