"""抖音直链候选排序

抖音的 ``url_list`` 会一次给多个等价直链（不同 CDN 节点 + 不同图片格式），
它们**内容相同但可用性不同**：

* 节点有差别 —— 实测 ``p3-pc-sign`` 对部分资源恒定 403，
  ``*-ex-gddgtc-*`` 是灰测节点，在部分线路/机房里也取不到；
* 格式有差别 —— ``:q75.webp``（约 0.5 MB）与 ``:q75.jpeg``（原图，约 1 MB）。

历史上这里用 ``random.choice`` 随机挑一条，一旦挑到坏节点就整张图下载失败
（会话里表现为「此项媒体下载失败」）。现在改为**确定性排序 + 依次回退**：
排第一的作主选，其余按序作为备用，主选失败时逐条重试。
"""

from __future__ import annotations

from urllib.parse import urlparse

#: 实测对部分资源恒定 403 的节点族
_BAD_HOST_PARTS = ("-pc-sign",)

#: 灰测节点，部分线路不可达，降级但不禁用
_SLOW_HOST_PARTS = ("ex-gddgtc",)


def _rank(url: str) -> tuple[int, int]:
    """排序权重，越小越优先"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path

    penalty = 0
    if any(part in host for part in _BAD_HOST_PARTS):
        penalty += 100
    if any(part in host for part in _SLOW_HOST_PARTS):
        penalty += 10
    # 原图（jpeg/jpg，无 tplv 格式后缀）优先，webp 次之
    fmt_penalty = 1 if ".webp" in path else 0
    return (penalty, fmt_penalty)


def order(urls: list[str] | tuple[str, ...] | None) -> list[str]:
    """去重 + 按可用性排序，返回候选直链列表（可能为空）

    Args:
        urls: 原始 ``url_list``

    Returns:
        list[str]: 排好序的候选直链；无可用项时返回空列表
    """
    if not urls:
        return []
    seen: set[str] = set()
    items: list[str] = []
    for url in urls:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        items.append(url)
    # 稳定排序：同权重保持原始顺序
    return sorted(items, key=_rank)


def primary(urls: list[str] | tuple[str, ...] | None) -> str | None:
    """取首选直链"""
    ordered = order(urls)
    return ordered[0] if ordered else None
