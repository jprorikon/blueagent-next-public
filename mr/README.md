# BlueAgent MR — Quest 3 客户端

Quest 3 / OpenXR 专有工程。共享客户端实现由 `app/client` 唯一持有，`mr/tools/sync_workspace.py` 在构建工作区组合两部分。

## 构建

```bash
LOCAL=1 bash mr/tools/build_mr_quest.sh
```

需要本机 Unity、Android SDK 与 Quest 对应依赖。公开版不提供 3D 角色模型、预编译原生库或签名密钥；没有可用模型时不能展示 MR 角色，但共享 2D / 接口代码仍可审查与测试。

## 身份与数据

登录 token 由开发者在本机提供，不能写入仓库或日志。运行数据、空间数据与设备信息不包含在演示版中。
