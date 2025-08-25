#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QB种子优化插件测试脚本
用于测试插件的基本功能
"""

import sys
import os
import time
from datetime import datetime

# 添加项目根目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))
sys.path.insert(0, project_root)

def test_plugin_basic():
    """测试插件基本功能"""
    try:
        # 直接导入插件文件
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "qboptimizer", 
            os.path.join(current_dir, "__init__.py")
        )
        qboptimizer_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qboptimizer_module)
        QbOptimizer = qboptimizer_module.QbOptimizer
        
        # 创建插件实例
        plugin = QbOptimizer()
        
        # 测试插件基本信息
        print(f"插件名称: {plugin.plugin_name}")
        print(f"插件描述: {plugin.plugin_desc}")
        print(f"插件版本: {plugin.plugin_version}")
        print(f"插件作者: {plugin.plugin_author}")
        
        # 测试配置表单
        form, default_config = plugin.get_form()
        print(f"\n配置表单项数: {len(form)}")
        print(f"默认配置项数: {len(default_config)}")
        
        # 测试命令
        commands = plugin.get_command()
        print(f"\n交互命令数: {len(commands)}")
        for cmd in commands:
            print(f"  - {cmd['cmd']}: {cmd['desc']}")
        
        print("\n✅ 插件基本功能测试通过")
        return True
        
    except Exception as e:
        print(f"❌ 插件基本功能测试失败: {str(e)}")
        return False

def test_plugin_config():
    """测试插件配置功能"""
    try:
        # 直接导入插件文件
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "qboptimizer", 
            os.path.join(current_dir, "__init__.py")
        )
        qboptimizer_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qboptimizer_module)
        QbOptimizer = qboptimizer_module.QbOptimizer
        
        plugin = QbOptimizer()
        
        # 测试默认配置
        test_config = {
            "enabled": True,
            "notify": True,
            "onlyonce": False,
            "cron": "0 */6 * * *",
            "downloaders": ["qbittorrent"],
            "enable_zero_seed_clean": True,
            "enable_priority_boost": True,
            "enable_timeout_clean": True,
            "zero_seed_threshold": 0,
            "priority_seed_threshold": 10,
            "timeout_days": 7,
            "max_download_slots": 5,
            "priority_boost_ratio": 2
        }
        
        # 初始化插件
        plugin.init_plugin(test_config)
        
        # 检查配置是否正确加载
        assert plugin._enabled == test_config["enabled"]
        assert plugin._notify == test_config["notify"]
        assert plugin._downloaders == test_config["downloaders"]
        assert plugin._enable_zero_seed_clean == test_config["enable_zero_seed_clean"]
        assert plugin._enable_priority_boost == test_config["enable_priority_boost"]
        assert plugin._enable_timeout_clean == test_config["enable_timeout_clean"]
        
        print("✅ 插件配置功能测试通过")
        return True
        
    except Exception as e:
        print(f"❌ 插件配置功能测试失败: {str(e)}")
        return False

def test_utility_functions():
    """测试工具函数"""
    try:
        # 直接导入插件文件
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "qboptimizer", 
            os.path.join(current_dir, "__init__.py")
        )
        qboptimizer_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qboptimizer_module)
        QbOptimizer = qboptimizer_module.QbOptimizer
        
        plugin = QbOptimizer()
        
        # 模拟种子对象
        class MockTorrent:
            def __init__(self, name, tags="", ratio=1.0):
                self.name = name
                self.tags = tags
                self.ratio = ratio
        
        # 测试重要种子判断
        important_torrent = MockTorrent("Important.Movie.2023", "important", 15.0)
        normal_torrent = MockTorrent("Normal.Movie.2023", "", 1.0)
        
        assert plugin._is_important_torrent(important_torrent) == True
        assert plugin._is_important_torrent(normal_torrent) == False
        
        print("✅ 工具函数测试通过")
        return True
        
    except Exception as e:
        print(f"❌ 工具函数测试失败: {str(e)}")
        return False

def main():
    """主测试函数"""
    print("🚀 开始测试QB种子优化插件...")
    print("=" * 50)
    
    tests = [
        ("基本功能测试", test_plugin_basic),
        ("配置功能测试", test_plugin_config),
        ("工具函数测试", test_utility_functions),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"\n📋 {test_name}")
        print("-" * 30)
        if test_func():
            passed += 1
        else:
            print(f"❌ {test_name} 失败")
    
    print("\n" + "=" * 50)
    print(f"📊 测试结果: {passed}/{total} 通过")
    
    if passed == total:
        print("🎉 所有测试通过！插件基本功能正常。")
        return 0
    else:
        print("⚠️  部分测试失败，请检查插件代码。")
        return 1

if __name__ == "__main__":
    exit(main())
