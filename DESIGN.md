# Network-Rules 设计

## 使用方式

维护一个独立的 `Network-Rules` 仓库。平时只需要编辑来源、个人例外规则和分流策略。构建器按客户端生成文件，GitHub Actions 定时拉取上游、验证变更并发布，客户端继续通过固定的 GitHub Raw URL 获取资源。首版提供 GitHub 上的订阅目录，以及本地的 `build`、`check`、`diff` 命令。个人使用场景可以直接在 GitHub 编辑配置、查看差异和手动触发更新。

## 公共规则来源

公共域名采用 [Nextin Hub](https://hub.nextinnet.com/) 使用的 [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat)，按分类映射 `meta` 分支的 Geosite 文件。Nextin 远程模板直接引用上游 MRS。定时构建固定该分支的一个提交，从同版 YAML 生成 QuanX、OpenClash 和 HomeProxy 规则，校验 Nextin MRS 的等效性，同时保留已有的仓库订阅地址。

Telegram 的 IP 网段由 Telegram 官方提供。个人网站、合并后的 PT、广告和白名单沿用仓库的规则来源与发布路径。DLC 发布仍用于 PT，下载时校验 SHA-256。上游必需资源校验失败时，仓库保留上一批制品。

## 文件组织与规则语义

| 位置 | 内容 |
| --- | --- |
| `main:sources.yml` | DLC 发布地址、制品名、校验文件和许可信息 |
| `main:policy.yml` | 分类名称、引用顺序、默认动作，以及各客户端的策略映射 |
| `main:overrides/` | 个人追加、排除和例外。个人域名或其他敏感配置单独存放 |
| `main:resources.yml` | QuanX 图标的来源与版本，以及策略组和图标的映射 |
| `main:tools/` | Python CLI，资源解析、验证、差异和导出。编译二进制时固定工具版本 |
| `publish:v1/` | 各客户端的发布文件、使用说明与构建清单，路径随版本保持稳定 |

策略表配置公共分类映射、顺序和各客户端的策略组。MetaCubeX 的精确域名和 `+.` 后缀分别转换为 QuanX 的 `host` 和 `host-suffix`。公共列表遇到未知匹配方式时停止发布，避免客户端收到覆盖范围不同的规则。有关键词或正则的旧来源继续记录各端省略项。

## 客户端接入

| 客户端 | 发布文件 | 应用与更新 |
| --- | --- | --- |
| OpenClash | `v1/openclash/<分类>.yaml`，另附覆写片段 | 首版发布 `classical` YAML。支持 YAML 覆写的版本用覆写模块加 `rule-providers` 和 `RULE-SET`，保留本地节点订阅。验收 OpenClash 最终生成的配置和策略顺序；客户端升级或订阅刷新后复查。 |
| Quantumult X | `v1/quanx/<分类>.list`，另附 `[filter_remote]` 片段 | 将各分类分别添加到 `[filter_remote]`。纯单动作分类可用 `force-policy` 绑定已有策略组。迁移带多个动作的原生列表时保留策略语义，并在真机检查资源刷新和匹配顺序。 |
| HomeProxy | `v1/homeproxy/<分类>.json` 或 `.srs` | 你当前使用自定义路由。通过 UI 添加远程规则集 URL、刷新间隔和下载出站，再绑定现有路由动作。首版使用匹配已装 sing-box 版本的 JSON，确认版本后可编译为 .srs。验证路由器重启和无缓存首次下载。 |
| Nextin | `v1/nextin/template-remote.yaml` 和按分类发布的远程 YAML | 将 `x-nextin` 模板应用到已有节点订阅，模板用 `rule-providers` 对分类内容设置 24 小时刷新。公共域名由 MetaCubeX MRS 提供，个人规则由本仓库提供。自动拉取与冷启动仍需在实际 App 版本验证。现有内嵌模板 `template.yaml` 仍可用于单独导入和诊断。 |

客户端依据：[OpenClash 覆写示例](https://github.com/vernesong/OpenClash/blob/master/luci-app-openclash/root/etc/openclash/overwrite/default) · [Mihomo 规则集合](https://wiki.metacubex.one/config/rule-providers/) · [QuanX 官方配置示例](https://github.com/crossutility/Quantumult-X/blob/master/sample.conf) · [HomeProxy 配置生成源码](https://github.com/immortalwrt/homeproxy/blob/master/root/etc/homeproxy/scripts/generate_client.uc) · [sing-box 规则集源格式](https://sing-box.sagernet.org/configuration/rule-set/source-format/) · [Nextin 自定义规则与覆写](https://wiki.nextinnet.com/guide/rules) · [Nextin 分流模板](https://wiki.nextinnet.com/guide/routing) · [Nextin Deep Link](https://wiki.nextinnet.com/guide/deeplink)

QuanX 图标随首版提供。在 `resources.yml` 维护策略组到图标的映射，生成 `[policy]` 的 `img-url` 补丁和图标索引。[Orz-3/mini](https://github.com/Orz-3/mini) 可作为候选，已确认有 GitHub、OpenAI、YouTube 图标。上游提供 Raw 引用方式，首版引用固定提交下的 Raw URL。确认再分发许可后可迁到自己的发布目录。现有 Rewrite 和 JS 继续由现有配置管理。

## 定时更新与分发

1. 在默认分支设置每日北京时间 04:17 的定时任务和 `workflow_dispatch`，两者共用发布并发锁。GitHub 调度可能延迟，公开仓库 60 天无活动也可能停用定时任务。[GitHub Actions 调度文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
2. 每次构建固定一个 MetaCubeX 提交和一个 DLC Release。发布清单记录分类来源、规则数与制品 SHA-256。无制品变化时只在 Actions 摘要记录检查结果。
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
4. 依据实际使用清单扩展国内直连、广告、流媒体等类别。先核对 MetaCubeX 分类覆盖，再添加必要的本地例外。

实施时用脱敏的现用策略组和规则片段完成映射。OpenClash 与 sing-box 的已装版本以及 Nextin 的导入刷新行为属于实测验收项。
