"""校验打包产物是否符合 AstrBot「从文件安装」的要求

用法：

    python tools/package.py
    python tools/verify_archive.py [zip 路径]

本脚本把 AstrBot 安装流程里真正会用到的校验逻辑按原样复刻了一遍
（astrbot/core/star/updater.py + astrbot/core/zip_updater.py + star_manager.py），
用于在本地提前发现「压缩包不是合法的 AstrBot 插件」这类问题：

1. ``_resolve_archive_root_dir`` —— 从 zip 条目推断仓库根目录
2. ``find_plugin_metadata_entry`` —— 在根目录下找 metadata.yaml / metadata.yml
3. ``validate_plugin_metadata`` —— 必需字段 name/desc/version/author 非空
4. ``_get_plugin_dir_name_from_metadata`` —— name 必须是合法 Python 标识符
   且非关键字（安装后的目录名就是它）

注意：本脚本只做「能否装进去」的静态校验，不负责依赖安装与运行期加载。
"""

from __future__ import annotations

import keyword
import os
import sys
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

PLUGIN_METADATA_FILENAMES = ("metadata.yaml", "metadata.yml")
PLUGIN_METADATA_REQUIRED_FIELDS = ("name", "desc", "version", "author")
PLUGIN_METADATA_MAX_BYTES = 1024 * 1024


def resolve_archive_root_dir(entries: list[str]) -> str:
    """复刻 zip_updater._resolve_archive_root_dir"""
    normalized_entries = [os.path.normpath(entry) for entry in entries]
    portable_entries = [entry.replace("\\", "/") for entry in normalized_entries]
    root_candidates: list[str] = []

    for raw_entry, normalized_entry, portable_entry in zip(
        entries, normalized_entries, portable_entries
    ):
        if normalized_entry == ".":
            continue

        has_children = any(
            other_entry != portable_entry
            and other_entry.startswith(f"{portable_entry}/")
            for other_entry in portable_entries
        )
        if raw_entry.endswith(("/", "\\")) or has_children:
            root_candidates.append(normalized_entry)
            continue

        parent_portable, _, _ = portable_entry.rpartition("/")
        if not parent_portable:
            return ""
        root_candidates.append(parent_portable.replace("/", os.sep))

    if not root_candidates:
        return ""
    return os.path.commonpath(root_candidates)


def find_plugin_metadata_entry(entries: list[str]) -> str | None:
    """复刻 updater.find_plugin_metadata_entry"""
    update_dir = resolve_archive_root_dir(entries)
    portable_update_dir = os.path.normpath(update_dir).replace("\\", "/")
    if portable_update_dir == ".":
        portable_update_dir = ""

    entries_by_portable_path: dict[str, str] = {}
    for entry in entries:
        portable_entry = os.path.normpath(entry).replace("\\", "/")
        if portable_entry in ("", "."):
            continue
        entries_by_portable_path[portable_entry] = entry

    candidates = (
        [f"{portable_update_dir}/{name}" for name in PLUGIN_METADATA_FILENAMES]
        if portable_update_dir
        else list(PLUGIN_METADATA_FILENAMES)
    )
    for candidate in candidates:
        if candidate in entries_by_portable_path:
            return entries_by_portable_path[candidate]
    return None


def validate_plugin_metadata(metadata: object) -> list[str]:
    """复刻 updater.validate_plugin_metadata，返回错误列表（为空即通过）"""
    if not isinstance(metadata, dict):
        return ["metadata 不是 YAML 映射"]
    return [
        f"缺少必需字段 {field}"
        for field in PLUGIN_METADATA_REQUIRED_FIELDS
        if field not in metadata
    ] + [
        f"字段 {field} 必须是非空字符串"
        for field in PLUGIN_METADATA_REQUIRED_FIELDS
        if field in metadata
        and (not isinstance(metadata[field], str) or not metadata[field].strip())
    ]


def plugin_dir_name(metadata: dict) -> str:
    """复刻 star_manager._get_plugin_dir_name_from_metadata 的名称校验部分"""
    name = metadata.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("metadata 中缺少 name 字段")
    name = name.strip()
    if "/" in name or "\\" in name:
        raise ValueError("name 含有路径分隔符，不可用于 importlib 加载")
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError("name 不是合法的模块名称")
    return name


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if target is None:
        candidates = sorted((ROOT / "dist").glob("*.zip"))
        if not candidates:
            print("dist/ 下没有 zip，请先运行 python tools/package.py")
            return 1
        target = candidates[-1]

    print(f"校验文件: {target}")
    print("=" * 56)
    problems: list[str] = []

    with zipfile.ZipFile(target) as z:
        entries = z.namelist()
        print(f"[1] 条目总数: {len(entries)}")

        # 2. 根目录与 metadata 定位
        root_dir = resolve_archive_root_dir(entries)
        metadata_entry = find_plugin_metadata_entry(entries)
        print(f"[2] 推断仓库根目录: {root_dir or '(zip 根)'}")
        print(f"[3] metadata 条目: {metadata_entry}")
        if metadata_entry is None:
            problems.append("未找到 metadata.yaml / metadata.yml，AstrBot 会拒绝安装")
            metadata = None
        else:
            raw = z.read(metadata_entry)
            print(f"    metadata 体积: {len(raw)} 字节（上限 {PLUGIN_METADATA_MAX_BYTES}）")
            if len(raw) > PLUGIN_METADATA_MAX_BYTES:
                problems.append("metadata.yaml 超过 1MB")
            try:
                metadata = yaml.safe_load(raw.decode("utf-8"))
            except UnicodeDecodeError:
                problems.append("metadata 必须使用 UTF-8 编码")
                metadata = None
            except yaml.YAMLError as exc:
                problems.append(f"metadata YAML 解析失败: {exc}")
                metadata = None

        # 4. 必需字段
        if metadata is not None:
            errors = validate_plugin_metadata(metadata)
            print(f"[4] 必需字段检查: {'通过' if not errors else errors}")
            problems.extend(errors)

    # 5. 目录名合法性（装到 data/plugins/ 下的目录名）
    if metadata is not None:
        try:
            dir_name = plugin_dir_name(metadata)
            print(f"[5] 安装后目录名: {dir_name}  （合法 Python 标识符）")
            if dir_name != (metadata_entry or "").split("/")[0] and "/" in (
                metadata_entry or ""
            ):
                inner = metadata_entry.split("/")[0]
                print(f"    提示: zip 内层目录为 {inner!r}，AstrBot 会按 metadata 的 name 拍平重命名")
        except ValueError as exc:
            problems.append(str(exc))
            print(f"[5] 目录名校验: 失败 - {exc}")

    print("=" * 56)
    if problems:
        print("结果: 不通过")
        for p in problems:
            print(f"  ✗ {p}")
        return 1

    print("结果: 通过 —— 可以直接在 AstrBot 面板「插件 → 从文件安装」中选择该 zip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
