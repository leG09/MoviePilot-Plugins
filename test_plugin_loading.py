#!/usr/bin/env python3
"""
测试插件加载的脚本
"""

import sys
import os

def test_plugin_loading():
    """测试插件加载"""
    try:
        # 添加当前目录到Python路径
        current_dir = os.getcwd()
        sys.path.insert(0, current_dir)
        print(f"当前目录: {current_dir}")
        print(f"Python路径: {sys.path[0]}")
        
        # 尝试导入插件
        print("正在测试插件加载...")
        
        # 测试导入
        import plugins.v2.GDCloudLinkMonitor
        GDCloudLinkMonitor = plugins.v2.GDCloudLinkMonitor.GDCloudLinkMonitor
        print("✅ 插件导入成功")
        
        # 测试实例化
        plugin = GDCloudLinkMonitor()
        print("✅ 插件实例化成功")
        
        # 测试基本属性
        print(f"插件名称: {plugin.plugin_name}")
        print(f"插件版本: {plugin.plugin_version}")
        print(f"插件作者: {plugin.plugin_author}")
        print(f"插件描述: {plugin.plugin_desc}")
        
        print("🎉 插件加载测试完全成功！")
        return True
        
    except ImportError as e:
        print(f"❌ 导入错误: {e}")
        return False
    except Exception as e:
        print(f"❌ 其他错误: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_plugin_loading()
    sys.exit(0 if success else 1)
