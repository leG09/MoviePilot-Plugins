# QB种子优化插件安装说明

## 安装步骤

### 1. 下载插件
将以下文件复制到MoviePilot的插件目录：

```
plugins.v2/qboptimizer/
├── __init__.py          # 插件主文件
├── README.md           # 使用说明
├── INSTALL.md          # 安装说明
├── simple_test.py      # 测试脚本
└── test_plugin.py      # 完整测试脚本
```

### 2. 更新package.v2.json
确保`package.v2.json`文件中包含以下配置：

```json
"QbOptimizer": {
  "name": "QB种子优化",
  "description": "优化qBittorrent种子管理：删除做种数为0的种子重新下载、提高做种数多的优先级、清除下载时间过长的种子",
  "labels": "下载管理,Qbittorrent",
  "version": "1.0",
  "icon": "Qbittorrent_A.png",
  "author": "Assistant",
  "level": 1,
  "history": {
    "v1.0": "首个版本，实现种子优化功能"
  }
}
```

### 3. 重启MoviePilot
重启MoviePilot服务以加载新插件。

### 4. 配置插件
1. 在MoviePilot插件管理页面找到"QB种子优化"插件
2. 点击配置按钮
3. 设置下载器和相关参数
4. 保存配置

### 5. 启用插件
1. 开启"启用插件"开关
2. 设置执行周期（建议：`0 */6 * * *`，每6小时执行一次）
3. 根据需要开启相应的功能开关

## 验证安装

运行测试脚本验证插件是否正确安装：

```bash
cd /path/to/MoviePilot-Plugins
python3 plugins.v2/qboptimizer/simple_test.py
```

如果看到"🎉 所有测试通过！插件结构正确。"，说明安装成功。

## 故障排除

### 插件不显示
1. 检查文件路径是否正确
2. 确认package.v2.json配置正确
3. 重启MoviePilot服务

### 插件报错
1. 检查Python语法是否正确
2. 确认所有依赖模块可用
3. 查看MoviePilot日志获取详细错误信息

### 功能不工作
1. 确认qBittorrent下载器配置正确
2. 检查插件配置参数
3. 查看插件日志输出

## 卸载插件

1. 在MoviePilot中禁用插件
2. 删除插件文件目录
3. 从package.v2.json中移除插件配置
4. 重启MoviePilot服务

## 技术支持

如遇到问题，请：
1. 查看README.md获取详细使用说明
2. 运行测试脚本检查插件状态
3. 查看MoviePilot日志获取错误信息
4. 提交Issue到项目仓库
