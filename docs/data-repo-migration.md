# 数据与工具分离：迁移配置

本文件说明如何把训练语料放在**私有仓库**、工具放在**公开仓库**，以及 CI
如何在不泄漏密钥、不泄漏数据的前提下每轮生成。

仓库现状：

| 位置 | 内容 | 可见性 |
| - | - | - |
| `github.com/LightShi233/Traindata_Qingtian` | `traindata/`（schema、validator、audit、scripts、文档）、`data/taxonomy.json` | 公开，GPL-3.0 |
| `gitcode.com/Light233/data` | `chunks/`、`inbox/`、`rounds/`、`taxonomy.json` 副本 | 私有 |

公开仓库的 `.gitignore` 只放行 `data/taxonomy.json`，其余 `data/*` 一律忽略，
所以公开仓库的 `git add -A` 永远抓不到语料。

## 一次性配置

### 1. 私有仓库

已初始化：`git@gitcode.com:Light233/data.git`，`main` 分支含 130 个文件
（356 个 case / 18 个 round / 37 个 chunk）。

### 2. 在 GitCode 登记部署公钥

**CI 必须用一把专用密钥，绝不能用你 `~/.ssh/id_rsa` 这把账号密钥。**

```bash
ssh-keygen -t ed25519 -C "traindata-ci" -f ~/.ssh/traindata_ci -N ""
cat ~/.ssh/traindata_ci.pub
```

把这行公钥加到 `gitcode.com/Light233/data` → 设置 → **部署公钥**，权限选
**可写**（CI 要 push）。

为什么这条不能省：账号密钥能读写你 GitCode 名下**所有**仓库。把它放进
GitHub Secrets，等于让每次 `workflow_dispatch`、每条 PR 的 runner 都握着
你全账号的写权限——一次 action 供应链投毒、一次日志泄漏，暴露的就是全部。
专用部署密钥只能碰这一个仓库，出事的影响面被限制在语料上，而且吊销时
只影响 CI，不影响你自己 push。

如果 GitCode 的部署公钥不支持"可写"，退路是**再注册一个账号专门做 CI**，
用它生成密钥并只添加到这个仓库，而不是复用主账号的密钥。

> 本仓库 CI 读取的 secret 名是 `TRAINDATA_DATA_SSH_KEY`，与 `id_rsa`
> 无关。反过来也成立：GitHub 上没有、也不该有 `id_rsa` 这个 secret。

### 3. 在 GitHub 公开仓库配置

**Variables**（Settings → Variables）

| 名字 | 值 |
| - | - |
| `TRAINDATA_DATA_REPO` | `Light233/data` |

**Secrets**（Settings → Secrets and variables → Actions）

| 名字 | 值 |
| - | - |
| `TRAINDATA_DATA_SSH_KEY` | `~/.ssh/traindata_ci` 的**私钥全文**（含 `-----BEGIN OPENSSH PRIVATE KEY-----` 头尾） |

只此两条。

关于公钥：`.pub` 文件**不放进 GitHub**，但这不是因为"可以公开"，而是因为
它的去向只有一个——GitCode 仓库端的部署公钥列表。它是服务器用来验证你私钥
的凭据，本来就要登记在服务器上，再存一份到 GitHub 只是多一个泄漏面。

关于私钥：**任何私钥都不得进入公开仓库、不得写进日志、不得留在工作区**。
CI 里它只存在于 `~/.ssh/gitcode_key`（600），从不进入 `$GITHUB_WORKSPACE`，
job 结束无论成败都删除（见下文末的清理步骤）。

### 4. 校验连通性

```bash
ssh -i ~/.ssh/traindata_ci -o IdentitiesOnly=yes -T git@gitcode.com
git -c core.sshCommand='ssh -i ~/.ssh/traindata_ci -o IdentitiesOnly=yes' \
  ls-remote git@gitcode.com:Light233/data.git
```

## 每一轮 CI 做什么

```
1. checkout 公开仓库（工具）      -> $GITHUB_WORKSPACE/traindata, data/taxonomy.json
2. 把 secret 写成 ~/.ssh/gitcode_key（600），known_hosts 用 GitCode 公示的
   host key 静态写入（不 ssh-keyscan，避免 MITM）
3. git clone git@gitcode.com:<repo> -> ./data，在 data/ 内切出分支
4. 模型在 GITHUB_WORKSPACE 工作，语料写在 ./data 里（整个 ./data 就是私库
   checkout，含它自己的 data/taxonomy.json；公开仓库那份只是参考副本，CI 不用它）
5. Verify 步骤：cd data，用 ../traindata/scripts/* 重跑 validator / audit /
   salt / check，全部退出码 0 才 touch work/gates_ok
6. 有 gates_ok 才 cd data && git add -A && git commit && git push
7. always(): 删除 ~/.ssh/gitcode_key、~/.ssh/config、~/.ssh/known_hosts，
   并删除 ./data 整个 clone
```

## 日志里不出现问题

CI 的 Actions 日志是公开的，语料不是。三个可能泄漏的地方都已处理：

| 位置 | 风险 | 处理 |
| - | - | - |
| 私钥 | `echo`/`env` 误用会打印密钥 | 只 `printf` 写文件；另外对每一行 `::add-mask::`，即使将来误打印也显示 `***` |
| gate 报错 | `audit_diversity.py` 会引用 payload 前缀帮模型定位 | CI 调用时加 `--redact`：只留文件名、规则和 `<excerpt hidden, N chars, sha256:…>`，不引用原文 |
| Qoder transcript | 原实现把完整会话上传 artifact 并在失败时打日志，含所有 case 正文 | 只打印计数、工具名、退出状态；**artifact 上传已删除**；失败只显示 stderr 字节数和首行 |

Qoder 自己跑 gate 时**不带** `--redact`，它需要看到原文才能修数据；只有 CI 复核那一步隐去。
这意味着：同一份语料，模型看到的报错是完整的，公开日志里是脱敏的。

## 为什么不会泄漏

**密钥**

- 私钥只写进 `~/.ssh/gitcode_key`，从不写入工作区，因此不会被任何
  `git add -A` 抓到；第 7 步无论成功失败都会删掉它
- `known_hosts` 用 GitCode 公示的指纹静态写入，不跑 `ssh-keyscan`：后者会在
  运行时信任任何自称 gitcode.com 的服务器
- 密钥注释是 `traindata-ci`，与个人密钥可区分；吊销只需在仓库端删掉这把部署公钥

**数据**

- `./data` 是独立仓库，公开仓库根执行 `git add -A` 不会进入它（它是被
  `.gitignore` 忽略的嵌套仓库）
- 第 7 步把整个 `./data` 删掉，跑完的 runner 上不留语料副本
- CI 的所有 git 操作都在 `data/` 内（`cd data` 之后 `git ...`），不会误推到公开仓库
- `check --round` 会拒绝 `data/` 根下的未知文件，模型没法夹带私货

## 回滚到单仓库

如果以后想合并回去：

```bash
# 在公开仓库
git rm data/taxonomy.json && rm -rf data && git clone <私库> data
# 改 .gitignore，删掉 data/* 这条
# 改 .github/workflows/main.yml：删掉 Clone 与 Wipe 两步，把 Verify / Push 的
#   cd data 和 ../traindata 前缀去掉
```