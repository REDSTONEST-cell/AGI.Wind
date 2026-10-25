# 已锁版本与证据

- Python 3.12.14（复用已有 uv 安装）；Node v24.21.0；pnpm 11.7.0。
- Python 直接依赖精确版本见 app/pyproject.toml，全部传递依赖见 app/uv.lock；JS 依赖见 pnpm-lock.yaml。
- PostgreSQL Windows 开发版：17.6，EDB 二进制包 postgresql-17.6-1-windows-x64-binaries.zip。
- 包下载 SHA256：d378882abd001a186735acd6f6ba716bca6ccd192e800412d4fd15ed25376b3e。此为本地记录哈希，并非厂商签名验证。
- 来源：[PostgreSQL Windows 官方入口](https://www.postgresql.org/download/windows/)、[EDB 二进制下载](https://www.enterprisedb.com/download-postgresql-binaries)。开发包只用于合成数据事务测试；部署版独立锁定。
- 协议 1.0：Pydantic 生成 JSON Schema，再由 json-schema-to-typescript 15.0.4 生成 TypeScript；pnpm contracts:check 检查漂移。
- Letta/GPT-SoVITS/SenseVoice/BGE/EverOS 尚未安装或锁模型权重；未运行任何真实厂商/GPU 用例。
