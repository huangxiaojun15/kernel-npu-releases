#!/usr/bin/env python3
"""生成「镜像依赖清单」JSON，供 gh-pages 上的看板使用。

把两路信息合并成一份清单：

1. --image-data：在 CI 里真实跑起镜像后采集到的信息（实际解析结果）
     sglang -> remote / branch / commit / describe / commit_date
     os     -> distro / kernel / arch
     cann   -> toolkit / version_files
2. --dockerfile：静态解析 Dockerfile，得到「声明的」配置
     ARG 默认值、git clone 来源、pip install 的固定版本（name==version）
3. --build-arg / --build-args-file：调用方真正传给 docker build 的 KEY=VALUE，
   会覆盖 Dockerfile 里的 ARG 默认值，所以清单里显示的是「这次到底用了什么」，
   而不是 Dockerfile 里那个可能被覆盖的默认值。

输出字段名与前端 app.js 一一对应，改前端之前不要改这里的 key 名字。

用法：
  python3 generate_image_manifest.py \
      --image           <registry/repo:tag> \
      --dockerfile      docker/npu.Dockerfile \
      --image-data      image-data.json \
      --build-args-file build-args.txt \
      --output          image-manifest.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

SCHEMA_VERSION = 1

# 这些 ARG 不进清单：
#   TARGETARCH 是 docker 在多架构构建时注入的，单个清单里没有唯一正确值
#   APTMIRROR 只是构建期 apt 源的临时开关
_HIDDEN_BUILD_ARGS = ("TARGETARCH", "APTMIRROR")

_ARG_RE = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s*=\s*(.*))?$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_CLONE_RE = re.compile(r"\bgit\s+clone\b([^&|;]*)")
_BRANCH_RE = re.compile(r"(?:--branch|-b)\s+(\S+)")
_PIN_RE = re.compile(r"([A-Za-z0-9_.\[\]+-]+(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9_.\[\]+*.-]+)")

# git clone 里这些选项是「带值」的，解析位置参数时要跳过它们的值，
# 否则 "--depth 1 URL dir" 会把 1 当成仓库地址。
_VALUE_FLAGS = {
    "--depth", "--branch", "-b", "--origin", "-o", "--config", "--reference",
    "--separate-git-dir", "--shallow-since", "--shallow-exclude", "--jobs", "-j",
    "--filter", "--template", "--upload-pack",
}


def parse_build_args(entries):
    """["KEY=VALUE", ...] -> dict，忽略空行与 # 注释。"""
    out = {}
    for raw in entries or []:
        line = (raw or "").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def load_build_args(path, extra):
    entries = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            entries.extend(fh.read().splitlines())
    entries.extend(extra or [])
    return parse_build_args(entries)


def _resolve(text, args, depth=5):
    """展开 ${VAR} / $VAR（有限次迭代，避免循环引用卡死）。"""
    def sub(match):
        name = match.group(1) or match.group(2)
        return args.get(name, match.group(0))

    for _ in range(depth):
        new = _VAR_RE.sub(sub, text)
        if new == text:
            break
        text = new
    return text


def _logical_lines(raw):
    """把 "\" 续行拼成逻辑行，Dockerfile 里 ARG / RUN 常这么写。"""
    out = []
    buf = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.endswith("\\"):
            buf += stripped[:-1].rstrip() + " "
            continue
        buf += stripped
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _strip_quotes(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _parse_clone_args(tokens):
    branch = ""
    positional = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--branch", "-b") and i + 1 < len(tokens):
            branch = _strip_quotes(tokens[i + 1])
            i += 2
            continue
        if tok == "--recurse-submodules" and i + 1 < len(tokens) and "=" not in tok:
            i += 2
            continue
        if tok in _VALUE_FLAGS and i + 1 < len(tokens):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        positional.append(_strip_quotes(tok))
        i += 1

    remote = positional[0] if positional else ""
    directory = positional[1] if len(positional) > 1 else ""
    return remote, branch, directory


def parse_dockerfile(path, overrides=None):
    """静态解析 Dockerfile：ARG 默认值 / git clone 来源 / pip 固定版本。"""
    result = {"args": {}, "git_clones": [], "pip_pins": []}
    if not path or not os.path.exists(path):
        return result

    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = _logical_lines(fh.read())

    # 第一遍：收集 ARG 默认值。
    # 注意 ARG 在 FROM 之后要重新声明一次才可见，而那次重声明通常不带默认值
    # （只有 "ARG DEVICE_TYPE"），不能让它把前面的 "ARG DEVICE_TYPE=a3" 清空。
    args = {}
    for line in lines:
        match = _ARG_RE.match(line)
        if not match:
            continue
        name, default = match.group(1), match.group(2)
        if default is None:
            args.setdefault(name, "")
        else:
            args[name] = _resolve(_strip_quotes(default), args)
    if overrides:
        args.update(overrides)
    result["args"] = args

    # 第二遍：git clone 与 pip 固定版本（跳过 ARG/ENV/LABEL 定义行）。
    seen_clones = set()
    for line in lines:
        if line.startswith(("ARG ", "ENV ", "LABEL ")):
            continue
        # 先把 ${VAR} 展开再匹配，否则 "torch==${PYTORCH_VERSION}" 这类会漏掉
        resolved = _resolve(line, args)
        for match in _CLONE_RE.finditer(resolved):
            remote, branch, directory = _parse_clone_args(match.group(1).split())
            if not remote:
                continue
            entry = {
                "remote": remote,
                "branch": branch,
                "dir": directory,
            }
            key = (entry["remote"], entry["branch"], entry["dir"])
            if key not in seen_clones:
                seen_clones.add(key)
                result["git_clones"].append(entry)
        for match in _PIN_RE.finditer(resolved):
            pin = match.group(1)
            if pin not in result["pip_pins"]:
                result["pip_pins"].append(pin)

    return result


def build_manifest(image, dockerfile, image_data, declared):
    reported_args = {
        k: v for k, v in declared["args"].items() if k not in _HIDDEN_BUILD_ARGS
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "image": image,
        "dockerfile": dockerfile or "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "build_args": reported_args,
        "git_clones": declared["git_clones"],
        "declared_pip_pins": declared["pip_pins"],
        "sglang": image_data.get("sglang", {}),
        "os": image_data.get("os", {}),
        "cann": image_data.get("cann", {}),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="完整镜像地址 registry/repo:tag")
    parser.add_argument("--dockerfile", default="", help="Dockerfile 路径")
    parser.add_argument("--image-data", default="", help="容器内采集出来的 JSON")
    parser.add_argument("--build-arg", action="append", default=[],
                        help="实际传给 docker build 的 KEY=VALUE，可重复")
    parser.add_argument("--build-args-file", default="",
                        help="每行一个 KEY=VALUE 的文件")
    parser.add_argument("--output", default="image-manifest.json")
    opts = parser.parse_args(argv)

    image_data = {}
    if opts.image_data and os.path.exists(opts.image_data) and os.path.getsize(opts.image_data) > 0:
        try:
            with open(opts.image_data, encoding="utf-8") as fh:
                image_data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"::warning::无法解析 {opts.image_data}: {exc}", file=sys.stderr)

    overrides = load_build_args(opts.build_args_file, opts.build_arg)
    declared = parse_dockerfile(opts.dockerfile, overrides)
    manifest = build_manifest(opts.image, opts.dockerfile, image_data, declared)

    with open(opts.output, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"写出清单: {opts.output}")
    print(f"  build args    : {len(manifest['build_args'])}")
    print(f"  git clones    : {len(manifest['git_clones'])}")
    print(f"  pip 固定版本  : {len(manifest['declared_pip_pins'])}")
    print(f"  sglang commit : {manifest['sglang'].get('commit', '—')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
