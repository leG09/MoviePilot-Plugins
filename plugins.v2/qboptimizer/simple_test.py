#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QB种子优化插件简化测试脚本
"""

import os
import sys

def test_file_structure():
    """测试文件结构"""
    print("📋 测试文件结构")
    print("-" * 30)
    
    # 检查插件文件是否存在
    plugin_file = os.path.join(os.path.dirname(__file__), "__init__.py")
    if os.path.exists(plugin_file):
        print("✅ 插件主文件存在")
    else:
        print("❌ 插件主文件不存在")
        return False
    
    # 检查README文件
    readme_file = os.path.join(os.path.dirname(__file__), "README.md")
    if os.path.exists(readme_file):
        print("✅ README文件存在")
    else:
        print("❌ README文件不存在")
        return False
    
    # 检查package.v2.json中是否包含插件配置
    package_file = os.path.join(os.path.dirname(__file__), "..", "..", "package.v2.json")
    if os.path.exists(package_file):
        with open(package_file, 'r', encoding='utf-8') as f:
            content = f.read()
            if "QbOptimizer" in content:
                print("✅ package.v2.json包含插件配置")
            else:
                print("❌ package.v2.json不包含插件配置")
                return False
    else:
        print("❌ package.v2.json文件不存在")
        return False
    
    return True

def test_syntax():
    """测试语法"""
    print("\n📋 测试语法")
    print("-" * 30)
    
    try:
        # 尝试编译插件文件
        plugin_file = os.path.join(os.path.dirname(__file__), "__init__.py")
        with open(plugin_file, 'r', encoding='utf-8') as f:
            code = f.read()
        
        # 编译代码检查语法
        compile(code, plugin_file, 'exec')
        print("✅ 插件代码语法正确")
        return True
        
    except SyntaxError as e:
        print(f"❌ 语法错误: {e}")
        return False
    except Exception as e:
        print(f"❌ 其他错误: {e}")
        return False

def test_imports():
    """测试导入"""
    print("\n📋 测试导入")
    print("-" * 30)
    
    try:
        # 检查必要的导入
        required_imports = [
            'threading',
            'time',
            'datetime',
            'typing',
            'pytz',
            'apscheduler',
            'app.core.config',
            'app.helper.downloader',
            'app.log',
            'app.plugins',
            'app.schemas',
            'app.schemas.types',
            'app.core.event',
            'app.utils.string'
        ]
        
        plugin_file = os.path.join(os.path.dirname(__file__), "__init__.py")
        with open(plugin_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        missing_imports = []
        for imp in required_imports:
            if imp not in content:
                missing_imports.append(imp)
        
        if missing_imports:
            print(f"❌ 缺少导入: {missing_imports}")
            return False
        else:
            print("✅ 所有必要的导入都存在")
            return True
            
    except Exception as e:
        print(f"❌ 导入检查失败: {e}")
        return False

def test_class_structure():
    """测试类结构"""
    print("\n📋 测试类结构")
    print("-" * 30)
    
    try:
        plugin_file = os.path.join(os.path.dirname(__file__), "__init__.py")
        with open(plugin_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 检查必要的类和方法
        required_items = [
            'class QbOptimizer',
            'plugin_name',
            'plugin_desc',
            'plugin_icon',
            'plugin_version',
            'plugin_author',
            'init_plugin',
            'get_state',
            'get_command',
            'get_service',
            'get_form',
            'optimize_torrents'
        ]
        
        missing_items = []
        for item in required_items:
            if item not in content:
                missing_items.append(item)
        
        if missing_items:
            print(f"❌ 缺少必要的方法或属性: {missing_items}")
            return False
        else:
            print("✅ 类结构完整")
            return True
            
    except Exception as e:
        print(f"❌ 类结构检查失败: {e}")
        return False

def main():
    """主测试函数"""
    print("🚀 开始测试QB种子优化插件...")
    print("=" * 50)
    
    tests = [
        ("文件结构测试", test_file_structure),
        ("语法测试", test_syntax),
        ("导入测试", test_imports),
        ("类结构测试", test_class_structure),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        if test_func():
            passed += 1
        else:
            print(f"❌ {test_name} 失败")
    
    print("\n" + "=" * 50)
    print(f"📊 测试结果: {passed}/{total} 通过")
    
    if passed == total:
        print("🎉 所有测试通过！插件结构正确。")
        return 0
    else:
        print("⚠️  部分测试失败，请检查插件代码。")
        return 1

if __name__ == "__main__":
    exit(main())
