# Network-Rules

从同一份 v2fly/domain-list-community 官方发布生成 OpenClash、Quantumult X、HomeProxy 和 Nextin 的静态规则。首版试点为 OpenAI、GitHub 和 YouTube。长期设计见 [DESIGN.md](DESIGN.md)。

## 本地预览

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tools/network_rules.py build --output dist
.venv/bin/python tools/network_rules.py check dist
```

`build` 默认读取最新官方发布，`--release 20261118003829` 可以复现试点版本。使用独立的新目录生成候选，例如 `--output dist-candidate`。`diff PREVIOUS CANDIDATE` 比较分类条数，详细转换记录在各自的 `v1/manifest.json`。仓库路径变化时传入 `--repository owner/name`。

产物中的策略组名为试点示例，首次接入请映射到自己的节点组、规则顺序和兜底。Nextin 的 `template-remote.yaml` 引用三个远程规则集，期望一次导入后由内核刷新分类内容。不同版本的 Nextin 需实测首次下载和后续刷新。模板中的策略组、优先级和规则集 URL 变更仍需重新导入。HomeProxy JSON 使用规则集版本 2，需 sing-box 1.10 或以上。QuanX 图标见 `v1/quanx/icons.json`，把对应的 `img-url` 加入现有策略组。

## 更改规则

- 在 `policy.yml` 映射 DLC 分类、顺序和客户端策略组。可为分类设置 `attributes: {require: [], exclude: [ads]}`。
- 如需增加或排除具体规则，新建 `overrides/<分类>.yml`，填入 `add` 或 `remove` 数组，使用 `full:example.com`、`domain:example.org` 等 DLC 格式。会发布到 Raw 的文件请只使用可公开的数据。
- 各端自动跳过无法准确表达的条目，并在发布清单 `omitted` 和 Actions 摘要中报告。未命中的请求由客户端后续规则或兜底接管。

## 定时发布

工作流在北京时间每天 04:17 自动构建、校验和发布，也支持手动触发。通过校验的更新提交到 `publish` 分支，通过 Raw 提供 `v1/` 下的文件。校验失败时保留上一版，下次计划运行自动重试。数量变化达到 30% 会写入摘要，校验条件包括上游数据完整、分类可生成及版本单调。首次启用需把代码放到 GitHub 默认分支，并授予流程对发布分支的写权限。
