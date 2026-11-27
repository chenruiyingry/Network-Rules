# Network-Rules

与 [Nextin Hub](https://hub.nextinnet.com/) 共用 [MetaCubeX](https://github.com/MetaCubeX/meta-rules-dat) 的公共域名数据，定时生成 OpenClash、Quantumult X、HomeProxy 和 Nextin 的静态规则。[发布目录](https://github.com/chenruiyingry/Network-Rules/tree/publish/v1) 包含应用净化、TikTok、Binance、OpenAI、GitHub、YouTube、Disney+、国外媒体、Telegram、Google、国外网站、Apple 和 PT。长期设计见 [DESIGN.md](DESIGN.md)。

## QuanX 迁移

订阅目录的 `quanx/gfw.list` 由 Hub 同源的 `geosite/gfw` 生成。将 AppleTV 和 OpenAI 放在 GFW 之前，国内域名和国内 IP 放在其后，可用 `FINAL, 漏网之鱼` 自选兜底策略。

从发布目录的 `quanx/filter-remote.conf` 复制需要的条目。公共分类从 MetaCubeX 的 `meta` 分支转换。国内网站采用 `cn`，国外媒体采用 Hub 的 `category-media`，Binance、Apple 和国外网站采用同仓库的 `binance`、`apple` 和 `geolocation-!cn`。Telegram 另合并 [官方 IPv4 和 IPv6 网段](https://core.telegram.org/resources/cidr.txt)。AppleTV 使用 `apple-tvplus` 域名集。合并后的 PT、个人网站、广告和白名单继续使用仓库文件。应用净化仍在目录中单独提供。每次构建记录实际使用的 MetaCubeX 提交。

Disney+ 文件已提供，独立策略的订阅条目默认关闭。应用净化和 PT 直连在通用规则之前，YouTube 在 Google 和国外媒体之前，国外网站排在最后。Nextin 提供国外网站规则集文件，默认模板暂缓加载这个大型集合。

## Nextin 个人分流

在「设置 → 分流规则覆写」导入手机和电视共用的 [个人分流模板](https://raw.githubusercontent.com/chenruiyingry/Network-Rules/publish/v1/nextin/quanx-routing.yaml)，再应用到已有节点订阅。公共域名采用 Nextin Hub 的 Builder 和内置 `GEOSITE`，个人网站、PT、Telegram IP、中国 IP 和广告白名单直接写进 YAML。广告拦截使用 Hub 内置分类。发布前验证文件小于 4 MB。

模板先匹配私网、PT 和个人网站，再匹配广告、服务专用规则、GFW、国内域名和中国 IP，最后由「漏网之鱼」选择直连或通用分流。AppleTV 只从美国节点测速选用。OpenAI 只从名称带 AI 的美国节点测速选用。通用分流排除名称带 `x1`、`x2`、`1.5x` 等倍率标记的节点。

白名单来自 [217heidai/adblockfilters](https://github.com/217heidai/adblockfilters)。命中白名单的请求继续按后续规则分流。个人例外和策略以仓库发布的 YAML 为准。GitHub Actions 会定时更新模板，已保存的模板需要重新导入以更新规则。

## 本地预览

构建 MRS 需要 Mihomo。将 `mihomo` 放在 `PATH` 中，或通过 `MIHOMO` 指定可执行文件的路径。GitHub Actions 使用固定版本并校验下载文件。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tools/network_rules.py build --output dist
.venv/bin/python tools/network_rules.py check dist
```

`build` 默认读取最新 DLC 发布作为 PT 的来源之一。可通过 `--release 20261118003829` 指定 DLC 版本，MetaCubeX 公共规则仍解析当前上游提交。使用独立的新目录生成候选，例如 `--output dist-candidate`。`diff PREVIOUS CANDIDATE` 比较分类条数，详细转换记录在各自的 `v1/manifest.json`。仓库路径变化时传入 `--repository owner/name`。

产物中的策略组名为试点示例，首次接入请映射到自己的节点组、规则顺序和兜底。Nextin 的 `template-remote.yaml` 引用远程规则集，由内核刷新分类内容。不同版本的 Nextin 需实测首次下载和后续刷新。模板中的策略组、优先级和规则集 URL 变更仍需重新导入。HomeProxy JSON 使用规则集版本 2，需 sing-box 1.10 或以上。QuanX 图标见 `v1/quanx/icons.json`，把对应的 `img-url` 加入现有策略组。

## 更改规则

- 在 `policy.yml` 用 `meta_geosite` 映射公共分类，配置顺序和客户端策略组。`dlc` 支持单个分类或分类数组，数组内按匹配类型和条件去重。可为分类设置 `attributes: {require: [], exclude: [ads]}`。
- 如需编辑本地或 DLC 分类的规则，可新建 `overrides/<分类>.yml`，填入 `add` 或 `remove` 数组，使用 `full:example.com`、`domain:example.org` 等 DLC 格式。会发布到 Raw 的文件请只使用可公开的数据。
- 各端自动跳过无法准确表达的条目，并在发布清单 `omitted` 和 Actions 摘要中报告。未命中的请求由客户端后续规则或兜底接管。

## 定时发布

工作流在北京时间每天 04:17 自动构建、校验和发布，也支持手动触发。通过校验的更新提交到 `publish` 分支，通过 Raw 提供 `v1/` 下的文件。校验失败时保留上一版，下次计划运行自动重试。数量变化达到 30% 会写入摘要，校验条件包括上游数据完整、分类可生成及版本单调。首次启用需把代码放到 GitHub 默认分支，并授予流程对发布分支的写权限。
