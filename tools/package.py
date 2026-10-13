"""一键打包脚本

把插件源码打成可直接安装的 zip：

    python tools/package.py

产物：``dist/万能解析.zip``，内部顶层目录即插件包名（与 metadata.yaml 的 name 一致），
解压到 AstrBot 的 ``data/plugins/`` 下即可使用。

说明：
- 包名自动读取 ``metadata.yaml`` 的 ``name`` 字段，避免改名后脚本与插件不同步；
- 自动排除开发期目录（``outputs`` / ``.workbuddy`` / ``_dshprobe`` 等）与字节码缓存。
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# 需要排除的目录名（任意层级命中即跳过）
EXCLUDE_DIRS = {
    "__pycache__",
    ".git",
    ".github",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "dist",
    "outputs",
    "tools",
    "_dshprobe",
    "_fixprobe",
    ".workbuddy",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

# 需要排除的文件后缀
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd", ".log", ".zip"}

# 需要排除的文件名
EXCLUDE_FILES = {".DS_Store", "Thumbs.db", ".gitignore"}


def read_plugin_name() -> str:
    """从 metadata.yaml 读取插件包名（不依赖 PyYAML）"""
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    matched = re.search(r"^name:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not matched:
        raise SystemExit("metadata.yaml 中未找到 name 字段")
    return matched.group(1).strip().strip("\"'")


def iter_files():
    """按稳定顺序遍历需要打包的文件"""
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue

        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]):
            continue
        if rel.name in EXCLUDE_FILES or path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue

        yield path, rel


def main() -> int:
    name = read_plugin_name()
    DIST.mkdir(exist_ok=True)
    target = DIST / f"{name}.zip"

    files = list(iter_files())
    if not files:
        print("没有可打包的文件")
        return 1

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, rel in files:
            # 顶层目录用插件包名，保证解压后目录名与包名一致
            zf.write(path, arcname=str(Path(name) / rel))

    size_kb = target.stat().st_size / 1024
    print(f"打包完成: {target}")
    print(f"文件数: {len(files)}    大小: {size_kb:.1f} KB")
    for _, rel in files[:15]:
        print(f"  {rel}")
    if len(files) > 15:
        print(f"  ... 其余 {len(files) - 15} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
