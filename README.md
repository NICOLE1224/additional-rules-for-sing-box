# additional-rules-for-sing-box

使用 GitHub Actions 将 [Accademia/Additional_Rule_For_Clash](https://github.com/Accademia/Additional_Rule_For_Clash) 的域名规则转换为 sing-box JSON 规则集，再通过官方编译器生成 `.srs`；同时生成 Shadowrocket 可订阅的 `.list` RULE-SET。

仅提取域名匹配条件。IP、GEOIP、进程等非域名条件不输出；**DIRECT、REJECT、REJECT-DROP 等策略不包含在规则集中，需要在自己的客户端配置中单独设置**。尤其 FakeLocation 等混合策略文件，不能通过给整份规则集指定一个代理来复现原始行为。

## 分支和产物

| 分支 | 内容 | 示例路径 |
| --- | --- | --- |
| `main` | 转换代码、Actions、测试和文档 | `scripts/convert.py` |
| `json` | 编译前的 sing-box JSON 规则集 | `Gemini/Gemini_Domain.json` |
| `srs` | 编译后的二进制规则集 | `Gemini/Gemini_Domain.srs` |
| `shadowrocket` | 文本 RULE-SET 和可选通配符补充 | `Gemini/Gemini_Domain.list` / `Gemini/Gemini_Domain.wildcard.list` |

输出保留上游目录和文件名。递归扫描全部 `.yaml` / `.yml`，包括历史备份文件中的域名规则；不只扫描 `_Domain.yaml`。没有域名条件的文件不生成空规则集。`json`、`srs`、`shadowrocket` 是由自动化管理的产物分支，请勿在其中手动维护文件。

每个产物分支同时保存：

- `INDEX.md`：全部规则集索引。
- `manifest.json`：上游提交、编译器版本、构建指纹、文件 SHA-256、输入与输出条目统计、忽略的非域名规则类型、被省略的策略和异常条目。
- `LICENSE.upstream`：上游 MIT 许可证及版权声明。

三份产物来自同一上游提交，使用 `git push --atomic` 一次更新三个分支。转换、检查或推送失败时不发布部分更新；原分支历史保留，不强制推送。上游删除的文件会在下一次成功更新时从产物分支删除。

发布后，`scripts/verify_published.py` 检查三个远端分支的提交均为本次发布提交，并逐一比较分支中的全部文件与构建目录的 Git blob 哈希、路径和文件模式。缺少文件、多出文件、内容不同或某个分支未发布，都会使工作流失败。运行摘要列出各分支的规则文件数量和提交；下载产物明确包含 `json/`、`srs/`、`shadowrocket/` 三个目录，包括可选通配符补充文件。

工作流中的 Action 固定到经过核对的完整提交 SHA，当前版本为 `actions/checkout v7.0.1`、`actions/setup-python v7.0.0`、`actions/upload-artifact v7.0.2`，均声明使用 Node 24，运行于 GitHub 托管的 `ubuntu-latest`。GitHub 已于 2026-09-23 移除 Actions 的 Node 20 运行时，版本迁移依据见 [官方公告](https://github.blog/changelog/2026-09-23-node-20-is-no-longer-available-in-github-actions/)。升级 Action 时，应核对其稳定发布、`action.yml` 的 `runs.using`、输入兼容性及对应提交 SHA，再实际运行完整发布流程。

## 检测上游更新

Actions 每 6 小时检查上游 `main` 的提交；**上游提交和转换器构建指纹均未变化时，跳过依赖安装、转换、编译和发布，不创建空提交**。转换器代码或配置变化也会触发重建。首次运行或任一产物分支缺失时会构建。

这是对外部仓库的轮询检测：上游提交不会直接触发本仓库的 Actions。定时任务可能被 GitHub 延迟；可以在 Actions → **Update domain rule-sets** → **Run workflow** 手动立即检查。手动检查也会在没有变化时跳过。

定时表达式在 `.github/workflows/update.yml`，使用 UTC。当前每逢 UTC 00:23、06:23、12:23、18:23 检查。GitHub 公共仓库长时间无活动可能暂停定时工作流，届时需重新启用；详情见 [GitHub scheduled workflows 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

## 创建自己的 GitHub 仓库

1. 建立一个 GitHub 仓库，将本项目源码推送至默认分支 `main`。公开仓库便于客户端直接订阅 Raw 文件。
2. 在仓库 Actions 页面启用工作流，首次手动运行 **Update domain rule-sets**。
3. 如组织策略或分支保护限制写入，为工作流允许 `contents: write`，并允许更新 `json` / `srs` / `shadowrocket` 三个产物分支。工作流使用内置 `GITHUB_TOKEN`，不需要自备 PAT。
4. 等首次运行成功，即可使用下面的订阅地址。Actions artifact 也包含三份产物，可下载核对。

无需将本地上游克隆、虚拟环境、编译器或 `dist/` 上传到源码分支；这些路径已加入 `.gitignore`。

## 在 sing-box 中使用

本项目预设仓库为 `NICOLE1224/additional-rules-for-sing-box`；部署到其他账号时替换 URL 中的仓库路径。替换 `proxy` 为已有出站的 tag。下面是配置片段，需合并到自己的配置中：

```json
{
  "route": {
    "rule_set": [
      {
        "type": "remote",
        "tag": "gemini",
        "format": "binary",
        "url": "https://raw.githubusercontent.com/NICOLE1224/additional-rules-for-sing-box/srs/Gemini/Gemini_Domain.srs",
        "update_interval": "6h"
      }
    ],
    "rules": [
      {
        "rule_set": ["gemini"],
        "action": "route",
        "outbound": "proxy"
      }
    ]
  },
  "experimental": {
    "cache_file": {
      "enabled": true
    }
  }
}
```

JSON 地址对应为 `https://raw.githubusercontent.com/NICOLE1224/additional-rules-for-sing-box/json/Gemini/Gemini_Domain.json`，使用它时将 `format` 改为 `source`。这些地址需待 GitHub 仓库及产物分支上传成功后才能使用。优先订阅 `_Domain` 版本；其他版本虽然也仅输出域名条件，但可能包含原始混合规则中的不同策略。规则集之间的优先顺序仍需由调用方配置。

产物采用 rule-set version 2，二进制要求 sing-box ≥ 1.10；上面的 `action` 写法要求 ≥ 1.11。规则格式和编译方式见 [sing-box 官方文档](https://sing-box.sagernet.org/configuration/rule-set/source-format/)。

## 在 Shadowrocket 中使用

全部可订阅文件见 [shadowrocket 分支索引](https://github.com/NICOLE1224/additional-rules-for-sing-box/blob/shadowrocket/INDEX.md)。在配置的 `[Rule]` 节加入，例如：

```ini
[Rule]
RULE-SET,https://raw.githubusercontent.com/NICOLE1224/additional-rules-for-sing-box/shadowrocket/Gemini/Gemini_Domain.list,PROXY
```

将 `PROXY` 替换为你需要的策略或已有策略组。主 `.list` 是 UTF-8 文本，包含 `DOMAIN,example.com`、`DOMAIN-SUFFIX,example.com`、`DOMAIN-KEYWORD,example` 这样的两列规则，不附带策略；使用 `RULE-SET` 引用。没有可输出条件的文件不会生成空 `.list`。

Shadowrocket 的域名正则支持和前导点后缀的等价行为尚未确认。因此主文件**排除 `domain_regex`（含 Clash 通配符转换的正则）和前导点 `domain_suffix`**。Clash 通配符原文另转换为同名 `.wildcard.list`，使用 `DOMAIN-WILDCARD`，供用户选择是否补充订阅，例如：

```ini
# 可选：确认接受匹配范围差异后，再加入 [Rule]
RULE-SET,https://raw.githubusercontent.com/NICOLE1224/additional-rules-for-sing-box/shadowrocket/Gemini/Gemini_Domain.wildcard.list,PROXY
```

补充映射为：`*.example.com` → `DOMAIN-WILDCARD,*.example.com`；`.example.com` → `DOMAIN-WILDCARD,*.example.com`；`+.example.*` → `DOMAIN-WILDCARD,example.*` 和 `DOMAIN-WILDCARD,*.example.*` 两条，覆盖本域及子域。**补充文件不保证与 Clash 等价，可能扩大匹配范围**：Clash 的 `*` 仅匹配一个非空标签，本项目尚未确认 Shadowrocket 通配符具有相同边界限制。补充文件的注释也标明该差异，主文件不会自动引用它。

任意的经典 `DOMAIN-REGEX` 无法普遍还原为 glob；原文含字面 `?` 的模式也不转换成补充通配符，以免改变字面含义。这些条目仍有明确的未输出记录。普通 `DOMAIN` / `DOMAIN-SUFFIX` 中的字面 `?` 保留。完整匹配条件继续保存在 sing-box JSON/SRS 中。

`manifest.json` 的 `shadowrocket` 汇总分别给出主文件和补充文件数、条目数、主文件排除数、补充覆盖条件数、仍未表示的条件数。`issues` 中 `excluded_shadowrocket_primary` 逐条记录来源、值、原因及 `wildcard_supplement_available`；每个源文件也有对应统计和 SHA-256。索引分开列出主文件和可选补充。不要将三个分支的匹配范围视为完全相同。

格式参考：[LOWERTOP 编写的 Shadowrocket 社区手册：规则类型](https://github.com/LOWERTOP/Shadowrocket#规则类型)、[blackmatrix7 发布的 Shadowrocket RULE-SET 示例](https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Shadowrocket/Gemini/Gemini.list)。这些是社区作者的资料，不是客户端官方解析器。本项目检查生成文本的类型、条目和统计，并测试发布流程；尚未在 Shadowrocket 客户端实机导入验证。

## 转换语义与上游异常

| 输入 | 输出 |
| --- | --- |
| `DOMAIN,example.com` / 裸 `example.com` | `domain`，仅完整域名 |
| `DOMAIN-SUFFIX,example.com` / `+.example.com` | `domain_suffix`，本域及子域 |
| `.example.com` | 带前导点的 `domain_suffix`，仅子域 |
| `DOMAIN-KEYWORD,example` | `domain_keyword` |
| `DOMAIN-REGEX,...` | `domain_regex`，由官方编译器检查 Go 正则语法 |
| `*.example.com` / `+.example.*` | 锚定的 `domain_regex`，`*` 仅匹配一个非空标签 |

注释和被注释掉的条目不参与转换。条目按类型去重、排序，避免重复生成或无意义提交。`?` 在 Mihomo 域名树中是普通字符，本项目保留其字面语义，**不会擅自转为通配符**。

上游当前包含一些部分标签通配符、末尾点、重复 `+`，以及历史备份中的 `+.full:` / `+.regexp:` 等异常写法。已检查的异常条目在 `known_invalid.json` 中逐项列出，并在产物 `manifest.json` 中记录排除原因。本项目不猜测这些条目的预期用途，也不声称它们已成功转换。出现新的未知异常、未支持的规则类型、损坏的 YAML 或输出路径冲突时，构建失败并保留已有产物，维护者检查后再处理。

语义依据：[Mihomo 域名树实现](https://github.com/MetaCubeX/mihomo/blob/Meta/component/trie/domain.go)、[DOMAIN-SUFFIX 实现](https://github.com/MetaCubeX/mihomo/blob/Meta/rules/common/domain_suffix.go)。转换器不保证上游规则本身的准确性，也不修复原文件中的策略混合或误写。

## 本地构建和检查

需要 Python ≥ 3.10、Git；附带下载脚本针对 Linux amd64。编译器版本和官方发布包 SHA-256 固定在 `toolchain.json`，升级时必须同时核对并修改两项。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/install_sing_box.py
git clone --depth 1 https://github.com/Accademia/Additional_Rule_For_Clash upstream
SING_BOX_BINARY="$PWD/.tools/sing-box" python -m unittest discover -s tests -v
python scripts/convert.py --source upstream --output dist
```

结果分别在 `dist/json/`、`dist/srs/`、`dist/shadowrocket/`。输出目录必须不存在，重建请指定新的 `--output` 路径；脚本不会覆盖已有文件。每份 SRS 检查文件头及版本，再将全部产物交给 `sing-box check` 检查；测试另外用官方 `rule-set match` 验证正反匹配，检查 Shadowrocket 文本及排除记录，以及用本地 Git 远端验证无变化跳过、删除同步、新分支补建和三个分支的原子发布。

## 许可与来源

上游规则来自 Accademia，以 [MIT 协议](https://github.com/Accademia/Additional_Rule_For_Clash/blob/main/LICENSE) 发布。生成的分支保留上游版权及许可声明。转换代码与原始规则的来源应分别识别，不将上游规则归为本项目原创。
