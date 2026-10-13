"""一键打包脚本

把插件源码打成可直接安装的 zip：

    python tools/package.py

产物（均在 ``dist/`` 下）：

- ``万能解析.zip`` —— 主产物，直接在 AstrBot 面板「插件 → 从文件安装」里选它；
- ``wanneng-jiexi.zip`` —— 同一内容的 ASCII 名副本。GitHub Release 的附件名不接受中文
  （上传会返回 422），需要发 Release 时用这个文件。

zip 内部顶层目录即插件包名（与 metadata.yaml 的 name 一致），
解压到 AstrBot 的 ``data/plugins/`` 下即可使用。

说明：
- 包名自动读取 ``metadata.yaml`` 的 ``name`` 字段，避免改名后脚本与插件不同步；
- ASCII 别名自动取自 ``metadata.yaml`` 的 ``repo`` 仓库名，没有则退化为 ``plugin``；
- 自动排除开发期目录（``outputs`` / ``.workbuddy`` / ``_dshprobe`` 等）与字节码缓存；
- ``tools/`` 整体排除，但**随包附带上部署自检脚本**（``diagnose_douyin.py`` 与
  ``setup_browser.sh``）——服务器上装了插件就能直接 `python tools/diagnose_douyin.py --install`。
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
    "_albumprobe",
    ".workbuddy",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

# 需要排除的文件后缀
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd", ".log", ".zip"}

# 需要排除的文件名
EXCLUDE_FILES = {".DS_Store", "Thumbs.db", ".gitignore", ".gitattributes"}

# 尽管在 EXCLUDE_DIRS 里，但这些路径要随包发出去（服务器上直接就能跑）
INCLUDE_PATHS = {
    "tools/diagnose_douyin.py",
    "tools/setup_browser.sh",
}


def read_metadata_field(field: str) -> str | None:
    """从 metadata.yaml 读取指定字段（不依赖 PyYAML）"""
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    matched = re.search(rf"^{field}:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    return matched.group(1).strip().strip("\"'") if matched else None


def read_plugin_name() -> str:
    """从 metadata.yaml 读取插件包名"""
    name = read_metadata_field("name")
    if not name:
        raise SystemExit("metadata.yaml 中未找到 name 字段")
    return name


def read_ascii_alias() -> str:
    """ASCII 别名：优先用 repo 的仓库名，否则退化为 plugin"""
    repo = read_metadata_field("repo") or ""
    slug = repo.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"[^0-9A-Za-z._-]", "", slug)
    return f"{slug or 'plugin'}.zip"


def iter_files():
    """按稳定顺序遍历需要打包的文件"""
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue

        rel = path.relative_to(ROOT)
        posix = rel.as_posix()
        if posix in INCLUDE_PATHS:
            yield path, rel
            continue
        if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]):
            continue
        if rel.name in EXCLUDE_FILES or path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue

        yield path, rel


def main() -> int:
    name = read_plugin_name()
    DIST.mkdir(exist_ok=True)
    target = DIST / f"{name}.zip"
    alias = DIST / read_ascii_alias()

    files = list(iter_files())
    if not files:
        print("没有可打包的文件")
        return 1

    def write_zip(dest: Path) -> None:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for path, rel in files:
                # 顶层目录用插件包名，保证解压后目录名与包名一致
                zf.write(path, arcname=str(Path(name) / rel))

    write_zip(target)
    if alias != target:
        write_zip(alias)

    size_kb = target.stat().st_size / 1024
    print(f"打包完成: {target}")
    if alias != target:
        print(f"ASCII 副本（用于 GitHub Release 附件）: {alias}")
    print(f"文件数: {len(files)}    大小: {size_kb:.1f} KB")
    for _, rel in files[:15]:
        print(f"  {rel}")
    if len(files) > 15:
        print(f"  ... 其余 {len(files) - 15} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
