# 提交与推送安全门禁

本目录保存公开规则和图片复核记录。实际密钥、现场词表和私有 profile 必须保存在仓库外。门禁不启动应用，不调用模型或源系统，不写运行配置和数据库。

## 安装与日常使用

Mac 需要 Python 3.9+、Git 和 Gitleaks 8.30.1 或兼容的 8.x。应用依赖另见根 README。

```sh
brew install gitleaks
python3 scripts/install_security_hooks.py
git config --local --get core.hooksPath
```

最后一条应输出 `.githooks`。安装只修改当前副本的 local Git 配置；发现其他钩子时拒绝覆盖。Git 不会随 clone 自动安装钩子，新副本需要执行安装命令。

| 操作 | 自动检查范围 |
| --- | --- |
| `git commit` | 完整暂存树，读取索引中的字节和图片清单，工作区修复但未暂存仍会拦截 |
| `git push` | Git 提供的实际目标对象及全部祖先、各版本文件、文件名、ref 名称、commit/tag 元数据；包含非当前分支和多 ref |
| GitHub Actions | 完整检出的全部 refs 历史通用扫描，以及临时 Git 仓库的合成回归 |

手动检查与钩子使用同一入口：

```sh
python3 scripts/security_scan.py staged
python3 scripts/security_scan.py history
python3 -m unittest discover -s tests -p test_publication_security.py -v
git diff --cached
```

`push` 模式由 Git 传入 ref 更新信息，不手工拼接线上推送；纯删除 ref 没有新增内容，允许通过。推送及 history 使用当前 HEAD 中已提交的图片批准清单，暂存新清单不会提前放行推送。全部历史按对象去重，单对象上限 16 MiB、累计上限 128 MiB；浅克隆、读取不完整、未知对象/文件模式或超限会阻断。

## 检查内容与失败处理

- 强制 Gitleaks 默认密钥规则；不接受 `GITLEAKS_*` 配置覆盖、行内 `gitleaks:allow` 或 `SKIP` 放行。
- 补充私网 IPv4、个人 home 路径、URL/引号值中的内部域名、已知私有内容及常见编码变体检查。API 的 `/api/users`、Python 模块名和普通文件名不当成内部域名。
- 拒绝真实 `.env`、私有配置、凭据文件、数据库、压缩包、日志及运行证据目录。`.env.example` / `.env.sample` 仍检查实际内容。
- 未知二进制、嵌入媒体和未复核图片默认拒绝。不能凭扩展名或 HTTP 成功声明素材安全。
- 缺 Gitleaks、版本不兼容、已配置的私有来源失效、扫描器异常时拒绝提交/推送；不自动降级。

输出只有通过状态、规则、脱敏位置和计数，不显示匹配内容、源配置路径或原始异常。疑似敏感文件名可能显示为 `git-path-` 摘要标识。Git 自身仍可能在 stderr 显示用户指定的 ref；不要把命令行中的敏感 ref 当作安全日志。扫描器诊断仅在本机处理，不上传原始扫描输出或私有词表。

失败后从暂存内容和历史定位原因：仅删除最新文件不能清除历史泄露；新图先复核。确认误报时用不包含实际值的合成用例修正规则，不添加目录级放行，不绕过钩子。若密钥确实已公开，应先撤销/轮换，再处理历史。

## 私有来源精确比对

通用扫描无法识别所有任意名称和数字。当前开发副本可用仓库外、权限为 `0600` 或更严格的 JSON 描述实际私有来源。profile 不复制实际密钥；helper 每次只读读取来源，结果经捕获管道进入扫描器内存。已配置的 profile 失效会阻断。

下面仅展示结构，路径和端口由部署者填写，不保存到 Git：

```json
{
  "version": 1,
  "context_values": ["example-site-marker"],
  "text_sources": ["/absolute/external/admin-password"],
  "json_sources": [
    {"path": "/absolute/external/credentials.json", "pointer": "/ai/api_key"}
  ],
  "postgres": {
    "host": "127.0.0.1",
    "port": 5432,
    "database": "easyops",
    "user": "easyops",
    "password_source": {
      "path": "/absolute/external/config.json",
      "pointer": "/db_password"
    },
    "master_key_file": "/absolute/external/master.key"
  }
}
```

除 `version` 外各来源可选；不使用的段删除。现场词条至少 4 字符，私有值至少 8 字符。短数字仅按完整词边界匹配，避免把普通版本号误认为现场端口。来源文件需位于仓库外且不超过 2 MiB。JSON pointer 支持对象键；不使用通配符或执行命令。

```sh
python3 scripts/install_security_hooks.py --local-profile /absolute/external/security-check.json
```

启用私有比对需要当前副本的 `backend/.venv/bin/python`。数据库来源还需应用已锁定的 psycopg、cryptography 依赖；仅允许 IP 回环连接，并显式开启和核对 PostgreSQL 只读事务。读取 Vault 密文/明文、口令哈希、会话及配置实体标识；不使用应用初始化入口。无数据库段时不连接数据库。

CI 没有此私有 profile，不上传配置或传递真实密钥；本机精确比对是公开前的额外门禁。真实配置和现场发生变化时同步更新仓库外来源描述或词表。

## 图片复核

`reviewed-assets.json` 的 `png` 只接受相对路径对应的 SHA-256。现有三张 PNG 已完成此前的 OCR、目视及元数据复核；记录绑定已复核字节。新增或修改图片须先在本机检查画面、文字、元数据，确认无现场信息后再更新记录并一同暂存。

扫描器另外要求 PNG 块 CRC 正确，只含 IHDR、IDAT、IEND 且无尾随数据；清单不代替目视/OCR，也不能证明图片像素自动脱敏。其他媒体需另行设计可审查的安全流程，首版不自动放行。

## 边界

客户端钩子能被拥有本机控制权的人主动关闭；这里保护正常开发流程，不能声称绝对防泄露。GitHub Push Protection 保持启用，CI 是上传后的通用复核，不能替代推送前检查或人工审查，也不声称检测所有自定义业务名称、编码和隐写信息。

卸载/恢复需明确修改此副本的 `core.hooksPath` 和 `easyops.securityProfile`，恢复安装前设置；安装器不会修改全局设置、其他副本或运行实例。正常开发不得关闭门禁来消除检查失败。
