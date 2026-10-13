#!/usr/bin/env python3
"""在「容器内部」执行，采集这个镜像里真实存在的依赖信息，输出 JSON 到 stdout。

CI 里的调用方式（把本文件经 stdin 喂进容器）：

    docker run --rm -i -e SGLANG_DIR --entrypoint /bin/bash "$IMAGE" \
      -c 'python3 - "$SGLANG_DIR"' < .github/scripts/extract_image_data.py \
      > image-data.json

刻意不采集 pip 全量包清单 —— 那会让清单体积失控；
Dockerfile 里声明的固定版本由 generate_image_manifest.py 静态解析得到。
"""

import json
import subprocess
import sys


def sh(cmd):
    """跑命令并返回 stdout（失败/命令不存在都返回空串，不抛异常）。"""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout.strip()


def main():
    sglang_dir = sys.argv[1] if len(sys.argv) > 1 else "/sgl-workspace/sglang"

    data = {
        # 镜像里 sglang 的 git 状态 -> 这个镜像到底烧进了哪个 commit
        "sglang": {
            "dir": sglang_dir,
            "remote": sh(f"git -C {sglang_dir} remote get-url origin 2>/dev/null"),
            "branch": sh(f"git -C {sglang_dir} branch --show-current 2>/dev/null"),
            "commit": sh(f"git -C {sglang_dir} rev-parse HEAD 2>/dev/null"),
            "describe": sh(f"git -C {sglang_dir} describe --tags --always 2>/dev/null"),
            "commit_date": sh(f"git -C {sglang_dir} log -1 --format=%cI 2>/dev/null"),
        },
        "os": {
            "distro": sh("grep '^PRETTY_NAME=' /etc/os-release | cut -d= -f2 | tr -d '\"'"),
            "kernel": sh("uname -r"),
            "arch": sh("uname -m"),
        },
        "cann": {
            "toolkit": sh("ls -1 /usr/local/Ascend/ascend-toolkit/ 2>/dev/null | head -20 | tr '\n' ' '"),
            "version_files": sh(
                "find /usr/local/Ascend -maxdepth 2 -name 'version.info' 2>/dev/null | head -5 | tr '\n' ' '"
            ),
        },
    }

    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
