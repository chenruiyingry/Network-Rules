# Network-Rules 设计

## 使用方式

维护一个独立的 `Network-Rules` 仓库。平时只需要编辑来源、个人例外规则和分流策略。构建器按客户端生成文件，GitHub Actions 定时拉取上游、验证变更并发布，客户端继续通过固定的 GitHub Raw URL 获取资源。首版提供 GitHub 上的订阅目录，以及本地的 `build`、`check`、`diff` 命令。个人使用场景可以直接在 GitHub 编辑配置、查看差异和手动触发更新。

## 唯一域名上游

选择 [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community) 作为全部共享分流域名的唯一上游。它提供按服务和地区归类的域名，分流动作由自己的策略表指定。自动化每轮选择一个正式发布版本，下载该版本的 `dlc.dat_plain.yml` 和校验文件，核验 SHA-256 后按分类提取。YAML 已展开上游的 include，仍保留匹配类型和属性。

已实查 [发布 20261118003829](https://github.com/v2fly/domain-list-community/releases/tag/20261118003829)，发布时间为 2026-11-18 08:38（北京时间），同日 08:36 还有一次发布。下载文件校验值匹配，共有 1,557 个分类；GitHub 64 条、OpenAI 25 条、YouTube 178 条、哔哩哔哩 53 条、Telegram 21 条。发布频率以实查结果为例，由上游提交节奏决定。

已确认采用 DLC 单域名上游。该版本还有 `anthropic`、`google-gemini`、`perplexity`、`github-copilot`、`netflix`、`disney`、`hbo`、`primevideo`、`spotify`、`tiktok`、`whatsapp` 和 `discord` 等分类。微信、淘宝若需单独分流，应核对 `tencent`、`alibaba` 的范围，再补充所需的本地规则。迁移国内外分流时保留原策略优先级，分别评估 `cn`、`geolocation-cn` 和 `geolocation-!cn` 的命中范围。广告分类 `category-ads-all` 用于域名拦截，现有重写继续按原配置管理。

[Loyalsoldier/domain-list-custom](https://github.com/Loyalsoldier/domain-list-custom) 在 DLC 上增加了自己的分类和数据，[MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat) 提供按内核发布的成品。两者可用于比较覆盖范围。自动构建始终从同一个 DLC 发布版本生成全套域名规则，缺项由你在 `overrides/` 明确增补，上游故障时保留上一批。IP / GeoIP 保持各设备现有设定，后续需要统一时另行确定数据来源。

[MyRules](https://github.com/chenruiyingry/MyRules) 的规则位于 `master:Clash/`，默认分支是 `rm`；[Rules-For-Quantumult-X](https://github.com/chenruiyingry/Rules-For-Quantumult-X) 的规则位于 `develop:Rules/`。两个 fork 用于盘点原 URL、策略、规则顺序和你自己的例外；语言或命名相同的分类也要比较实际命中范围。新仓库的工作流放在默认分支，制品发布到专用分支。

## 文件组织与规则语义

| 位置 | 内容 |
| --- | --- |
| `main:sources.yml` | DLC 发布地址、制品名、校验文件和许可信息 |
| `main:policy.yml` | 分类名称、引用顺序、默认动作，以及各客户端的策略映射 |
| `main:overrides/` | 个人追加、排除和例外。个人域名或其他敏感配置单独存放 |
| `main:resources.yml` | QuanX 图标的来源与版本，以及策略组和图标的映射 |
| `main:tools/` | Python CLI，资源解析、验证、差异和导出。编译二进制时固定工具版本 |
| `publish:v1/` | 各客户端的发布文件、使用说明与构建清单，路径随版本保持稳定 |

策略表为每个发布分类配置 DLC 分类名、属性筛选、优先级和四个客户端的策略组。规则模型保留精确域名、域名后缀、关键词、正则和属性。去重限制在语义完全相同的条目，跨分类重叠进入报告，保留现有规则的命中顺序和兜底行为。

试点需要显式处理两个情况。YouTube 列表含 `@ads` 和 `@cn`，这些属性仅用作筛选，是否拦截或直连由策略决定。OpenAI 的 25 条中有一条 Azure Web PubSub 主机名正则。构建器为各端导出可准确表达的条目。暂时无法转换的条目由工具自动跳过，按客户端记录在发布清单和 Actions 摘要中。这些请求由客户端后续规则或兜底接管。当前 OpenAI 正则保留在 OpenClash 和 HomeProxy，QuanX 和 Nextin 按该策略处理。

## 客户端接入

| 客户端 | 发布文件 | 应用与更新 |
| --- | --- | --- |
| OpenClash | `v1/openclash/<分类>.yaml`，另附覆写片段 | 首版发布 `classical` YAML。支持 YAML 覆写的版本用覆写模块加 `rule-providers` 和 `RULE-SET`，保留本地节点订阅。验收 OpenClash 最终生成的配置和策略顺序；客户端升级或订阅刷新后复查。 |
| Quantumult X | `v1/quanx/<分类>.list`，另附 `[filter_remote]` 片段 | 将各分类分别添加到 `[filter_remote]`。纯单动作分类可用 `force-policy` 绑定已有策略组。迁移带多个动作的原生列表时保留策略语义，并在真机检查资源刷新和匹配顺序。 |
| HomeProxy | `v1/homeproxy/<分类>.json` 或 `.srs` | 你当前使用自定义路由。通过 UI 添加远程规则集 URL、刷新间隔和下载出站，再绑定现有路由动作。首版使用匹配已装 sing-box 版本的 JSON，确认版本后可编译为 .srs。验证路由器重启和无缓存首次下载。 |
| Nextin | `v1/nextin/template-remote.yaml` 和按分类发布的远程 YAML | 将 `x-nextin` 模板应用到已有节点订阅，模板用 `rule-providers` 对分类内容设置 24 小时刷新。已发布规则从同版 DLC 生成；自动拉取与冷启动仍需在用户的 App 版本验证。现有内嵌模板 `template.yaml` 仍可用于单独导入和诊断。 |

客户端依据：[OpenClash 覆写示例](https://github.com/vernesong/OpenClash/blob/master/luci-app-openclash/root/etc/openclash/overwrite/default) · [Mihomo 规则集合](https://wiki.metacubex.one/config/rule-providers/) · [QuanX 官方配置示例](https://github.com/crossutility/Quantumult-X/blob/master/sample.conf) · [HomeProxy 配置生成源码](https://github.com/immortalwrt/homeproxy/blob/master/root/etc/homeproxy/scripts/generate_client.uc) · [sing-box 规则集源格式](https://sing-box.sagernet.org/configuration/rule-set/source-format/) · [Nextin 自定义规则与覆写](https://wiki.nextinnet.com/guide/rules) · [Nextin 分流模板](https://wiki.nextinnet.com/guide/routing) · [Nextin Deep Link](https://wiki.nextinnet.com/guide/deeplink)

QuanX 图标随首版提供。在 `resources.yml` 维护策略组到图标的映射，生成 `[policy]` 的 `img-url` 补丁和图标索引。[Orz-3/mini](https://github.com/Orz-3/mini) 可作为候选，已确认有 GitHub、OpenAI、YouTube 图标。上游提供 Raw 引用方式，首版引用固定提交下的 Raw URL。确认再分发许可后可迁到自己的发布目录。现有 Rewrite 和 JS 继续由现有配置管理。

## 定时更新与分发

1. 在默认分支设置每日北京时间 04:17 的定时任务和 `workflow_dispatch`，两者共用发布并发锁。GitHub 调度可能延迟，公开仓库 60 天无活动也可能停用定时任务。[GitHub Actions 调度文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
2. 每次构建固定一个 DLC Release，下载同版本的 YAML 和校验文件。记录 Release 标签、制品 SHA-256、分类及各端条目数、编译器版本和策略提交。无制品变化时只在 Actions 摘要记录检查结果。
3. 拉取失败可有限重试。检查下载文件、SHA-256、分类结构、空规则集及生成物完整性。同类规则数量变动达到 30% 时写入摘要。各端自动记录省略项，结构有效的候选自动发布。任何必需产物校验失败时，本轮保留上一版整批制品，下次计划运行自动重试。
4. 生成物和清单以一个 Git 提交发布到 `publish`，发布只需要该流程的最小写权限。客户端分别拉取多个文件时仍可能在刷新窗口读到不同版本，因此保持既有文件路径和格式兼容，破坏性调整使用 `v2/`。发布后从 Raw 回读并核验关键文件。需回滚时恢复上一版制品并暂停自动发布或锁定问题来源，防止下一轮重新引入。
5. 订阅目录给出固定链接和导入片段。以下为拟定 URL，仓库创建和发布后生效。

       https://raw.githubusercontent.com/chenruiyingry/Network-Rules/publish/v1/openclash/openai.yaml
       https://raw.githubusercontent.com/chenruiyingry/Network-Rules/publish/v1/quanx/openai.list
       https://raw.githubusercontent.com/chenruiyingry/Network-Rules/publish/v1/homeproxy/openai.json
       https://raw.githubusercontent.com/chenruiyingry/Network-Rules/publish/v1/nextin/template-remote.yaml

默认使用 Raw。迁移时分别用手机数据网络、OpenClash 实际网络和 OpenWrt 无缓存启动测试下载。如果客户端必须先启用代理才能取到启动必需规则，可以为同一发布目录配置一个能直达的静态镜像，例如家中 HomeProxy 使用内网 HTTP 镜像，外部设备使用自己的 HTTPS 域名。自有域名便于以后切换托管，接入条件是证明该入口比 Raw 更容易直达。需要稳定复现时，可临时改用固定提交 SHA 的 Raw URL。公开目录只发布可公开的资源，私有订阅、节点凭据和内网细节保留在用户设备。

## 迁移与验收

1. 导出已启用的远程资源与分流顺序，记录客户端版本、原 URL、目标分类和特殊语义。首批用 GitHub、OpenAI、YouTube 试点，并用保留现状的内网直连样例验收。应用范围随当前启用情况调整。
2. 先用 QuanX 或 Nextin 验证试点；验证规则命中、主动刷新和旧 URL 恢复，再迁移其他客户端。HomeProxy 最后切换，切换前保留路由配置备份。
3. 手动跑一次 Actions，模拟上游空文件和语法错误，确认旧发布仍可获取。模拟数量骤变，确认有效更新发布时摘要包含数量差异。再小幅改动一个试点分类并分别验证各客户端刷新。Nextin 记录重新导入或自动刷新实测结果，HomeProxy 记录重启后的冷启动结果。
4. 依据实际使用清单扩展国内直连、广告、流媒体等类别。先以 DLC 的类别和属性核对覆盖，再添加必要的本地例外。

实施时用脱敏的现用策略组和规则片段完成映射。OpenClash 与 sing-box 的已装版本、Nextin 的导入刷新行为，以及 OpenAI 正则的跨端等效性属于实测验收项。
