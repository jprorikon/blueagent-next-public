# BlueAgent Next — Unity 客户端

此目录包含手机端 Unity 工程、共享 C# 逻辑与 fixture。客户端只呈现状态与收集输入，可复用判断放在 `shared/BlueAgent.Logic`。

## 公开演示素材

所有角色均为原创虚构角色。头像、立绘、背景和表情包使用抽象几何占位图；不包含第三方游戏角色、模型或贴图。3D 加载代码保留，但未提供模型时会回退到 2D。

## 离线检查

```bash
dotnet test
bash tools/check_unity_compile.sh
```

完整 Unity 构建还需要本机 Unity、平台 SDK 与原生依赖。预编译 `.so` / `.a` 和 keystore 不随公开版分发。

## 服务端地址

`NextConfig.DefaultBaseUrl` 默认是 `http://127.0.0.1:8788`。连接自己的服务时再修改为本机或测试环境地址，不要提交真实 token 或内部域名。
