"""静态清点：谁的代码在写文件，其中哪些没经过审计链。

背景：项目多处文档把 `workspace.append_event` 称为"哈希审计链唯一写入口"。
链内部成立，全系统不成立——11 处业务台账直接写 JSON，6 处恢复日志连事务
日志都绕过。本工具提供可重复的清点与回归闸门，不改任何业务行为。

用法（在 code/ 目录下）：

    python -X utf8 tools/check_write_paths.py --list
        列出全部文件写/删/移动站点，按文件分组，附行号与源码片段。

    python -X utf8 tools/check_write_paths.py --json out.json
        机器可读输出，便于逐条审阅。

    python -X utf8 tools/check_write_paths.py --write-allowlist tools/write_paths_allowlist.json
        把当前清点结果固化为白名单（先审再固化）。

    python -X utf8 tools/check_write_paths.py --check
        与白名单比对；出现白名单外的新站点则非零退出。

判定口径：
    - 只做静态识别，不判断"是否合法"。合法性由人审。
    - 识别 open(写模式) / write_text / write_bytes / json.dump / os.replace 等
      文件变更原语，以及 sqlite3 的写语句。
    - 链内写入（经 durable_storage 事务或 _mirror_* 镜像）不会被自动豁免——
      它同样要进白名单，因为"它现在经过链"这件事本身需要有人签字。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

W = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = W / "impl" / "panda_brain"

#: open() 的模式里出现任一字符即为写入（含 '+'：r+ 也能写）。
WRITE_MODE_CHARS = set("wax+")

#: Path / 文件对象上的数据变更方法。
#: 刻意不收 mkdir / rmdir / touch——目录操作不是业务数据写入，收进来只会淹没信号。
MUTATING_METHODS = {
    "write_text",
    "write_bytes",
    "unlink",
    "rename",
    "replace",
    "symlink_to",
    "hardlink_to",
}

#: os / shutil 上以调用形式出现的变更函数（取函数名）。
MUTATING_FUNCS = {
    "replace",
    "rename",
    "renames",
    "remove",
    "unlink",
    "rmdir",
    "removedirs",
    "copy",
    "copy2",
    "copyfile",
    "copymode",
    "copystat",
    "copytree",
    "move",
    "rmtree",
    "mkfifo",
    "mknod",
}

#: sqlite 写语句前缀。
SQL_WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER", "CREATE")


class Site:
    """一个文件变更站点。"""

    __slots__ = ("module", "line", "kind", "snippet", "ordinal")

    def __init__(self, module: str, line: int, kind: str, snippet: str) -> None:
        self.module = module
        self.line = line
        self.kind = kind
        self.snippet = snippet
        self.ordinal = 0

    def key(self) -> str:
        """稳定标识：不含行号。

        用行号作 key 会让"在文件上方加一行"就把整条白名单打乱。这里用
        （模块, 原语, 语句文本, 同名序号），只有写语句本身被改动或新增同文
        语句时 key 才变——那正是需要人看一眼的时刻。
        """
        return f"{self.module}|{self.kind}|{self.snippet}|{self.ordinal}"

    def as_dict(self) -> dict[str, object]:
        return {"module": self.module, "line": self.line, "kind": self.kind,
                "snippet": self.snippet, "ordinal": self.ordinal, "key": self.key()}


def attach_ordinals(sites: list[Site]) -> None:
    """为（模块, 原语, 语句文本）相同的站点按出现顺序编号。"""
    seen: dict[tuple[str, str, str], int] = {}
    for site in sites:
        bucket = (site.module, site.kind, site.snippet)
        seen[bucket] = seen.get(bucket, 0) + 1
        site.ordinal = seen[bucket]


def _open_mode_is_write(node: ast.Call) -> bool:
    """判断 open(...) 的 mode 参数是否包含写语义。mode 缺省为 'r'（只读）。"""
    mode: ast.expr | None = None
    if len(node.args) >= 2:
        mode = node.args[1]
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return bool(WRITE_MODE_CHARS & set(mode.value))
    # 变量模式（如 mode=flavour）无法静态判定，按潜在写入上报，交人审。
    return True


def _sql_is_write(text: str) -> bool:
    head = text.lstrip().upper()
    return head.startswith(SQL_WRITE_PREFIXES)


def _snippet(source: str, line: int) -> str:
    lines = source.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()[:160]
    return ""


def scan_file(path: Path, module_name: str) -> list[Site]:
    """解析单个文件，返回其中的变更站点。语法错误向上抛。"""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    found: list[Site] = []

    def add(node: ast.AST, kind: str) -> None:
        line = getattr(node, "lineno", 0)
        found.append(Site(module_name, line, kind, _snippet(source, line)))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # open(...)
        if isinstance(func, ast.Name) and func.id == "open":
            if _open_mode_is_write(node):
                add(node, "open")
            continue

        # json.dump(...)
        if isinstance(func, ast.Attribute) and func.attr == "dump":
            add(node, "json.dump")
            continue

        # Path.write_text / .write_bytes / .unlink / ...
        if isinstance(func, ast.Attribute) and func.attr in MUTATING_METHODS:
            add(node, f".{func.attr}")
            continue

        # os.replace / shutil.copytree / ...
        if isinstance(func, ast.Attribute) and func.attr in MUTATING_FUNCS:
            owner = func.value
            owner_name = owner.id if isinstance(owner, ast.Name) else ""
            if owner_name in {"os", "shutil"}:
                add(node, f"{owner_name}.{func.attr}")
                continue

        # sqlite3 写语句：.execute("INSERT ...") / .executescript(...)
        if isinstance(func, ast.Attribute) and func.attr in {"execute", "executescript"}:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                if _sql_is_write(node.args[0].value):
                    add(node, f"sqlite.{func.attr}")

    found.sort(key=lambda s: (s.line, s.kind))
    return found


def scan(root: Path) -> tuple[list[Site], list[str]]:
    """扫描目录下全部 .py。返回（站点，解析失败的模块）。"""
    sites: list[Site] = []
    broken: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = path.relative_to(root).as_posix()
        try:
            sites.extend(scan_file(path, module))
        except SyntaxError as exc:
            broken.append(f"{module}: {exc}")
    return sites, broken


def print_list(sites: list[Site], broken: list[str]) -> None:
    by_module: dict[str, list[Site]] = {}
    for site in sites:
        by_module.setdefault(site.module, []).append(site)
    for module in sorted(by_module):
        rows = by_module[module]
        print(f"\n{module}  ({len(rows)} 处)")
        for site in rows:
            print(f"  {site.line:>6}  {site.kind:<16} {site.snippet}")
    print(f"\n合计 {len(sites)} 处变更站点，分布在 {len(by_module)} 个模块。")
    if broken:
        print(f"\n解析失败 {len(broken)} 个：")
        for item in broken:
            print(f"  {item}")


def load_allowlist(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("sites", []))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清点文件变更站点，与白名单比对。")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="扫描根目录（默认 impl/panda_brain）")
    parser.add_argument("--list", action="store_true", help="打印全部站点")
    parser.add_argument("--json", type=Path, help="把结果写入 JSON 文件")
    parser.add_argument("--write-allowlist", type=Path, help="把当前站点固化为白名单")
    parser.add_argument("--check", action="store_true", help="与白名单比对，未登记则非零退出")
    parser.add_argument("--allowlist", type=Path, default=W / "tools" / "write_paths_allowlist.json",
                        help="白名单路径（默认 tools/write_paths_allowlist.json）")
    args = parser.parse_args(argv)

    if not args.root.is_dir():
        print(f"扫描根目录不存在：{args.root}", file=sys.stderr)
        return 2

    sites, broken = scan(args.root)
    attach_ordinals(sites)
    keys = sorted({site.key() for site in sites})

    if args.json:
        payload = {
            "root": str(args.root),
            "total": len(sites),
            "modules": len({site.module for site in sites}),
            "sites": [site.as_dict() for site in sites],
            "broken": broken,
        }
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {args.json}（{len(sites)} 处）")

    if args.list or not (args.check or args.write_allowlist):
        print_list(sites, broken)

    if broken:
        print("存在解析失败的文件，不能作为可信基线。", file=sys.stderr)
        return 2

    if args.write_allowlist:
        payload = {
            "note": "审阅过后固化；新增站点须显式登记，否则 --check 失败。",
            "root": str(args.root),
            "sites": keys,
        }
        args.write_allowlist.parent.mkdir(parents=True, exist_ok=True)
        args.write_allowlist.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"白名单已写入 {args.write_allowlist}（{len(keys)} 条）")

    if args.check:
        if not args.allowlist.is_file():
            print(f"白名单不存在：{args.allowlist}\n先跑 --list 审阅，再跑 --write-allowlist 固化。", file=sys.stderr)
            return 2
        allowed = load_allowlist(args.allowlist)
        current = set(keys)
        added = sorted(current - allowed)
        removed = sorted(allowed - current)
        if removed:
            print(f"白名单中已不存在的站点 {len(removed)} 条（陈旧条目，建议清理）：")
            for key in removed:
                print(f"  - {key}")
        if added:
            print(f"出现白名单外的新变更站点 {len(added)} 处：")
            for key in added:
                print(f"  + {key}")
            print("\n新增写文件必须经人审并登记，否则视为绕过审计链。")
            return 1
        print(f"通过：{len(current)} 处变更站点全部在白名单内。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
